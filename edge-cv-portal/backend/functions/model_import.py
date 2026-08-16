"""
Model Import Lambda functions
Implements BYOM (Bring Your Own Model) functionality
Allows importing pre-trained models that conform to DDA format
"""
import json
import os
import re
import logging
from dataclasses import asdict
from decimal import Decimal
from typing import Dict, Any, List, Tuple
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid
import tarfile
import tempfile
import shutil
from urllib.parse import urlparse
import yaml

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, get_usecase_client
)

# Preflight Fit_Check (pure sizing module bundled in this functions dir)
from vllm_fit_check import estimate_weights, evaluate_fit

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')

# Environment variables
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# Required files for DDA-compatible model
REQUIRED_FILES = {
    'config.yaml': 'Configuration file with image dimensions',
    'mochi.json': 'Model graph definition with input shape',
    'export_artifacts/manifest.json': 'Model metadata and compilable models info'
}

# ---------------------------------------------------------------------------
# vLLM model registration — pure validation and defaults
# ---------------------------------------------------------------------------

# Hugging Face model identifier: {org}/{name} (e.g. "facebook/opt-125m")
HF_MODEL_ID_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*)/[A-Za-z0-9._-]+$')

# vLLM_Engine_Configuration settings, defaults, and accepted ranges.
# The stored/serialized configuration always contains every key below;
# unknown supplied keys are rejected (fail closed).
ENGINE_DEFAULTS = {
    'dtype': 'auto',
    'gpu_memory_utilization': 0.5,
    'max_model_len': 2048,
    'tensor_parallel_size': 1,
    'enforce_eager': True
}

ENGINE_DTYPE_VALUES = ('auto', 'float16', 'bfloat16', 'float32')


def _validate_engine_setting(key: str, value: Any) -> str:
    """Validate one known engine setting. Returns a reason string when the
    value is outside its accepted range, or '' when the value is valid."""
    if key == 'dtype':
        if value not in ENGINE_DTYPE_VALUES:
            return f"dtype must be one of {'|'.join(ENGINE_DTYPE_VALUES)}"
    elif key == 'gpu_memory_utilization':
        # bool is an int subclass — reject explicitly
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 'gpu_memory_utilization must be a number in (0.0, 1.0]'
        if not (0.0 < float(value) <= 1.0):
            return 'gpu_memory_utilization must be in (0.0, 1.0]'
    elif key == 'max_model_len':
        if isinstance(value, bool) or not isinstance(value, int):
            return 'max_model_len must be an integer >= 1'
        if value < 1:
            return 'max_model_len must be an integer >= 1'
    elif key == 'tensor_parallel_size':
        if isinstance(value, bool) or not isinstance(value, int):
            return 'tensor_parallel_size must be an integer >= 1'
        if value < 1:
            return 'tensor_parallel_size must be an integer >= 1'
    elif key == 'enforce_eager':
        if not isinstance(value, bool):
            return 'enforce_eager must be a boolean'
    return ''


def validate_vllm_registration(body: Dict) -> List[Dict]:
    """Validate a vLLM model registration request body.

    Returns the complete list of validation findings; [] means valid.
    - exactly one of huggingface_model_id / s3_model_artifact (1.1, 1.6, 1.9)
    - huggingface_model_id matches HF_MODEL_ID_RE when present (1.11)
    - s3_model_artifact is an s3:// URI ending in .tar.gz when present
    - every supplied engine setting is a known key within its accepted
      range (1.10) — unknown keys rejected (fail closed)
    Each finding carries {field, value, reason}.
    """
    findings = []

    hf_model_id = body.get('huggingface_model_id')
    s3_artifact = body.get('s3_model_artifact')

    # Source XOR: exactly one of huggingface_model_id / s3_model_artifact
    if not hf_model_id and not s3_artifact:
        findings.append({
            'field': 'huggingface_model_id | s3_model_artifact',
            'value': None,
            'reason': 'exactly one source is required: provide either '
                      'huggingface_model_id or s3_model_artifact'
        })
    elif hf_model_id and s3_artifact:
        findings.append({
            'field': 'huggingface_model_id | s3_model_artifact',
            'value': {'huggingface_model_id': hf_model_id,
                      's3_model_artifact': s3_artifact},
            'reason': 'exactly one source must be provided, not both'
        })

    # Malformed Hugging Face model ID
    if hf_model_id:
        if not isinstance(hf_model_id, str) or not HF_MODEL_ID_RE.match(hf_model_id):
            findings.append({
                'field': 'huggingface_model_id',
                'value': hf_model_id,
                'reason': 'malformed Hugging Face model ID: expected '
                          '{organization}/{model_name} (e.g. facebook/opt-125m)'
            })

    # S3 artifact must be an s3:// URI ending in .tar.gz
    if s3_artifact:
        if not isinstance(s3_artifact, str) or \
                not s3_artifact.startswith('s3://') or \
                not s3_artifact.endswith('.tar.gz'):
            findings.append({
                'field': 's3_model_artifact',
                'value': s3_artifact,
                'reason': 's3_model_artifact must be an s3:// URI ending in .tar.gz'
            })

    # Engine configuration: unknown keys rejected fail-closed,
    # known keys validated against their accepted ranges
    engine_configuration = body.get('engine_configuration') or {}
    if not isinstance(engine_configuration, dict):
        findings.append({
            'field': 'engine_configuration',
            'value': engine_configuration,
            'reason': 'engine_configuration must be an object'
        })
    else:
        for key, value in engine_configuration.items():
            if key not in ENGINE_DEFAULTS:
                findings.append({
                    'field': f'engine_configuration.{key}',
                    'value': value,
                    'reason': f'unknown engine setting: {key}'
                })
                continue
            reason = _validate_engine_setting(key, value)
            if reason:
                findings.append({
                    'field': f'engine_configuration.{key}',
                    'value': value,
                    'reason': reason
                })

    return findings


def resolve_engine_configuration(supplied: Dict) -> Dict:
    """Overlay supplied engine settings on ENGINE_DEFAULTS.

    The result contains every defined setting: supplied values keep their
    values, omitted settings get their documented defaults (1.2, 1.3).
    """
    resolved = dict(ENGINE_DEFAULTS)
    for key, value in (supplied or {}).items():
        if key in ENGINE_DEFAULTS:
            resolved[key] = value
    return resolved


def _to_dynamo_compatible(value: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB storage."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_compatible(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo_compatible(v) for v in value]
    return value


def _decimal_to_native(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals back to native numbers."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_native(v) for v in value]
    return value


# JP5 vLLM support flag: off by default (JP5 vLLM support is not shipped;
# the env var exists so a future JP5 enablement is a deploy-time flag flip,
# never a code change here). Mirrors packaging.py / greengrass_publish.py.
JP5_VLLM_ENABLED = os.environ.get('JP5_VLLM_ENABLED', 'false').lower() == 'true'


def vllm_supported_architectures() -> List[str]:
    """Supported Target_Architecture set for vLLM_Model_Components:
    always arm64_jp6 and arm64_jp7, arm64_jp5 only when JP5 support is
    flagged on, never arm64_jp4. Mirrors
    packaging.vllm_supported_architectures."""
    archs = ['arm64_jp6', 'arm64_jp7']
    if JP5_VLLM_ENABLED:
        archs.append('arm64_jp5')
    return archs


def evaluate_fit_check(record: Dict) -> Dict:
    """Evaluate the non-blocking preflight Fit_Check for a vLLM_Model_Record.

    Estimates the on-GPU weight size (Hugging Face metadata or S3 artifact
    size) and evaluates the fit against every supported Target_Architecture
    (Requirements 3.1, 3.5). Never raises and never blocks: any estimation
    failure degrades to status 'unverified' (Requirement 3.4).

    Returns {status: 'passed'|'warnings'|'unverified',
             estimate: {total_bytes, method, detail} | None,
             findings: [{arch, fits, budget_bytes, required_bytes, message}]}.
    """
    try:
        s3_head = None
        model_source = record.get('model_source') or {}
        if isinstance(model_source, dict) and model_source.get('s3_model_artifact'):
            # S3-sourced records need a Use_Case-account client for HEAD
            usecase = get_usecase_details(record['usecase_id'])
            s3_head = get_usecase_client('s3', usecase).head_object

        estimate = estimate_weights(record, s3_head=s3_head)
        if estimate is None:
            return {
                'status': 'unverified',
                'estimate': None,
                'findings': [],
                'message': 'Model weight size could not be estimated; '
                           'the fit could not be verified.'
            }

        findings = evaluate_fit(
            record.get('engine_configuration') or {},
            estimate,
            vllm_supported_architectures()
        )
        status = 'warnings' if any(not f.fits for f in findings) else 'passed'
        return {
            'status': status,
            'estimate': {
                'total_bytes': estimate.total_bytes,
                'method': estimate.method,
                'detail': estimate.detail
            },
            'findings': [asdict(f) for f in findings]
        }
    except Exception as e:  # noqa: BLE001 — the fit check never blocks (3.4)
        logger.warning(f"Fit check evaluation failed, reporting unverified: {e}")
        return {
            'status': 'unverified',
            'estimate': None,
            'findings': [],
            'message': 'Model weight size could not be estimated; '
                       'the fit could not be verified.'
        }


def register_vllm_model(event: Dict, context: Any) -> Dict:
    """
    Register a vLLM model record (no labeling, no training)
    POST /api/v1/models/vllm

    Request body:
    {
        "usecase_id": "string",
        "model_name": "string",
        "model_version": "string",
        "huggingface_model_id": "org/name",              // XOR
        "s3_model_artifact": "s3://bucket/path.tar.gz",  // XOR
        "engine_configuration": { ... },  // optional, partial
        "description": "string"  // optional
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']

        # Parse request body
        body = json.loads(event.get('body', '{}'))

        # Validate required fields
        required_fields = ['usecase_id', 'model_name', 'model_version']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})

        usecase_id = body['usecase_id']
        model_name = body['model_name'].strip()
        model_version = body['model_version'].strip()
        description = body.get('description', '')

        # Check user access (DataScientist role required), matching import_model
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})

        # Validate the registration request — any finding means no record
        # is written and nothing is marked publish-eligible (1.5)
        findings = validate_vllm_registration(body)
        if findings:
            return create_response(400, {
                'error': 'vLLM model registration validation failed',
                'findings': findings
            })

        hf_model_id = body.get('huggingface_model_id')
        s3_artifact = body.get('s3_model_artifact')

        # Get use case details
        usecase = get_usecase_details(usecase_id)

        # For S3-sourced registrations, verify the artifact is readable from
        # the Use_Case account BEFORE any write (1.7)
        if s3_artifact:
            parsed = urlparse(s3_artifact)
            bucket = parsed.netloc
            key = parsed.path.lstrip('/')
            try:
                s3_client = get_usecase_client('s3', usecase)
                s3_client.head_object(Bucket=bucket, Key=key)
            except ClientError as e:
                logger.error(f"S3 model artifact not readable: {s3_artifact}: {str(e)}")
                return create_response(400, {
                    'error': f'S3 model artifact is not readable from the '
                             f'use case account: {s3_artifact}',
                    's3_model_artifact': s3_artifact
                })

        # Complete engine configuration: supplied values overlaid on defaults (1.2)
        engine_configuration = resolve_engine_configuration(
            body.get('engine_configuration') or {})

        # Exactly one source (already validated)
        model_source = ({'huggingface_model_id': hf_model_id} if hf_model_id
                        else {'s3_model_artifact': s3_artifact})

        # DynamoDB-compatible copy (floats as Decimal) used for both the
        # record write and the audit event details
        engine_configuration_ddb = _to_dynamo_compatible(engine_configuration)

        # Store the vLLM_Model_Record in the training-jobs table (1.3)
        training_id = str(uuid.uuid4())
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)

        training_item = {
            'training_id': training_id,
            'usecase_id': usecase_id,
            'model_name': model_name,
            'model_version': model_version,
            'model_type': 'vllm',
            'source': 'vllm',
            'description': description,
            'status': 'Completed',  # publish-eligible immediately, like BYOM
            'publish_eligible': True,
            'model_source': model_source,
            'engine_configuration': engine_configuration_ddb,
            'created_by': user['email'],
            'created_at': timestamp,
            'updated_at': timestamp
        }

        table.put_item(Item=training_item)

        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='register_vllm_model',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'model_name': model_name,
                'model_version': model_version,
                'model_source': model_source,
                'engine_configuration': engine_configuration_ddb
            }
        )

        logger.info(f"vLLM model registered successfully: {training_id}")

        # Non-blocking preflight fit check over the just-written record
        # against every supported Target_Architecture (Requirements 3.4,
        # 3.5). evaluate_fit_check never raises and never blocks —
        # registration has already succeeded regardless of the outcome.
        fit_record = dict(training_item)
        fit_record['engine_configuration'] = engine_configuration
        fit_check = evaluate_fit_check(fit_record)

        # Publish-eligible with zero labeling and zero training steps (1.4)
        return create_response(201, {
            'training_id': training_id,
            'publish_eligible': True,
            'labeling_steps': 0,
            'training_steps': 0,
            'fit_check': fit_check
        })

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_vllm_engine_spec(event: Dict, context: Any) -> Dict:
    """
    Get the vLLM engine configuration specification
    GET /api/v1/models/vllm/engine-spec
    """
    spec = {
        'description': 'vLLM engine configuration settings, defaults, and '
                       'accepted ranges. Omitted settings receive their '
                       'documented default values at registration time.',
        'settings': {
            'dtype': {
                'default': ENGINE_DEFAULTS['dtype'],
                'type': 'string',
                'accepted_values': list(ENGINE_DTYPE_VALUES),
                'description': 'Data type for model weights and activations.'
            },
            'gpu_memory_utilization': {
                'default': ENGINE_DEFAULTS['gpu_memory_utilization'],
                'type': 'number',
                'range': '(0.0, 1.0]',
                'description': 'Fraction of GPU memory the vLLM engine may '
                               'use. Defaults conservatively because the GPU '
                               'is shared with vision model inference.'
            },
            'max_model_len': {
                'default': ENGINE_DEFAULTS['max_model_len'],
                'type': 'integer',
                'range': '>= 1',
                'description': 'Maximum model context length (prompt plus '
                               'generated tokens).'
            },
            'tensor_parallel_size': {
                'default': ENGINE_DEFAULTS['tensor_parallel_size'],
                'type': 'integer',
                'range': '>= 1',
                'description': 'Number of GPUs for tensor-parallel execution.'
            },
            'enforce_eager': {
                'default': ENGINE_DEFAULTS['enforce_eager'],
                'type': 'boolean',
                'range': 'true | false',
                'description': 'Disable CUDA graph capture and always execute '
                               'the model in eager mode.'
            }
        },
        'source': {
            'description': 'Exactly one source must be provided.',
            'huggingface_model_id': {
                'type': 'string',
                'format': '{organization}/{model_name}',
                'example': 'facebook/opt-125m'
            },
            's3_model_artifact': {
                'type': 'string',
                'format': 's3:// URI ending in .tar.gz',
                'example': 's3://bucket/path/llm.tar.gz'
            }
        }
    }

    return create_response(200, spec)


# Path shape for the engine-configuration update endpoint (routing + fallback
# training_id extraction when pathParameters is absent).
VLLM_ENGINE_CONFIG_PATH_RE = re.compile(
    r'/models/vllm/([^/]+)/engine-configuration/?$')


def update_vllm_engine_configuration(event: Dict, context: Any) -> Dict:
    """
    Update the stored Engine_Configuration of a registered vLLM model
    PUT /api/v1/models/vllm/{training_id}/engine-configuration

    Request body: either the partial engine settings object directly, or
    wrapped as {"engine_configuration": {...}}. Every supplied setting is
    validated with the registration rules (unknown keys rejected fail
    closed, Requirement 2.2); valid settings are overlaid onto the stored
    configuration and written back (Requirement 2.1). The response carries
    the complete updated configuration, a re-package/publish notice, and a
    non-blocking fit-check result (Requirements 2.4, 3.5).
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']

        # training_id from the path
        path_params = event.get('pathParameters') or {}
        training_id = path_params.get('training_id')
        if not training_id:
            match = VLLM_ENGINE_CONFIG_PATH_RE.search(event.get('path', ''))
            training_id = match.group(1) if match else None
        if not training_id:
            return create_response(400, {
                'error': 'training_id path parameter is required'})

        # Supplied settings: accept the settings object directly or wrapped
        # under an engine_configuration key
        body = json.loads(event.get('body') or '{}')
        if not isinstance(body, dict):
            return create_response(400, {
                'error': 'Request body must be a JSON object of engine settings'})
        supplied = body.get('engine_configuration') \
            if isinstance(body.get('engine_configuration'), dict) else body

        # Load the record
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        if 'Item' not in response:
            return create_response(404, {
                'error': f'Model {training_id} not found'})
        record = response['Item']

        # Reject non-vLLM records (Requirement 2.3)
        if record.get('model_type') != 'vllm' and record.get('source') != 'vllm':
            return create_response(400, {
                'error': f'Model {training_id} is not a vLLM model record; '
                         f'an engine configuration can only be updated on '
                         f'vLLM models'})

        # RBAC: DataScientist on the use case, mirroring registration
        if not check_user_access(user_id, record.get('usecase_id'), 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})

        # Validate every supplied setting with the registration rules —
        # unknown keys rejected fail closed (Requirement 2.2); any finding
        # leaves the stored configuration unchanged
        findings = []
        if not supplied:
            findings.append({
                'field': 'engine_configuration',
                'value': supplied,
                'reason': 'at least one engine setting must be supplied'
            })
        else:
            for key, value in supplied.items():
                if key not in ENGINE_DEFAULTS:
                    findings.append({
                        'field': f'engine_configuration.{key}',
                        'value': value,
                        'reason': f'unknown engine setting: {key}'
                    })
                    continue
                reason = _validate_engine_setting(key, value)
                if reason:
                    findings.append({
                        'field': f'engine_configuration.{key}',
                        'value': value,
                        'reason': reason
                    })
        if findings:
            return create_response(400, {
                'error': 'Engine configuration update validation failed',
                'findings': findings
            })

        # Overlay the supplied settings onto the stored configuration
        # (Requirement 2.1). resolve_engine_configuration backfills any
        # missing setting with its documented default, so the result is
        # always the complete configuration.
        previous = resolve_engine_configuration(
            _decimal_to_native(record.get('engine_configuration') or {}))
        updated = dict(previous)
        updated.update(supplied)

        updated_ddb = _to_dynamo_compatible(updated)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET engine_configuration = :cfg, updated_at = :ts',
            ExpressionAttributeValues={':cfg': updated_ddb, ':ts': timestamp}
        )

        # Audit with before/after values (Requirement 2.6)
        log_audit_event(
            user_id=user_id,
            action='update_vllm_engine_configuration',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'model_name': record.get('model_name'),
                'model_version': record.get('model_version'),
                'previous_engine_configuration': _to_dynamo_compatible(previous),
                'updated_engine_configuration': updated_ddb
            }
        )

        logger.info(f"vLLM engine configuration updated: {training_id}")

        # Non-blocking fit check against the updated configuration (3.5)
        updated_record = dict(record)
        updated_record['engine_configuration'] = updated
        fit_check = evaluate_fit_check(updated_record)

        return create_response(200, {
            'training_id': training_id,
            'engine_configuration': updated,
            'notice': 'Engine configuration updated. The change takes '
                      'effect only after the model is packaged and '
                      'published again.',
            'fit_check': fit_check
        })

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access.

    Single-account setups store the account *root* ARN
    (arn:aws:iam::ACCOUNT_ID:root), which is not an assumable role — assuming it
    fails with AccessDenied. In that case use the Lambda's own execution role
    (same-account access) via the default credential chain.
    """
    if role_arn and role_arn.endswith(':root'):
        logger.info("Single-account setup (root ARN) — using Lambda execution role credentials")
        return {'is_default_credentials': True}
    try:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=3600
        )
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise


def get_usecase_details(usecase_id: str) -> Dict:
    """Get use case details from DynamoDB"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            raise ValueError(f"Use case {usecase_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting use case details: {str(e)}")
        raise


class ModelValidationError(Exception):
    """Custom exception for model validation errors"""
    def __init__(self, message: str, details: List[str] = None):
        self.message = message
        self.details = details or []
        super().__init__(self.message)


def validate_config_yaml(config_path: str) -> Tuple[int, int]:
    """
    Validate config.yaml structure and extract image dimensions
    Returns: (image_width, image_height)
    """
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        if not config:
            raise ModelValidationError("config.yaml is empty")
        
        if 'dataset' not in config:
            raise ModelValidationError("config.yaml missing 'dataset' section")
        
        dataset = config['dataset']
        
        if 'image_width' not in dataset:
            raise ModelValidationError("config.yaml missing 'dataset.image_width'")
        
        if 'image_height' not in dataset:
            raise ModelValidationError("config.yaml missing 'dataset.image_height'")
        
        image_width = dataset['image_width']
        image_height = dataset['image_height']
        
        # Validate dimensions are positive integers
        if not isinstance(image_width, int) or image_width <= 0:
            raise ModelValidationError(f"Invalid image_width: {image_width}. Must be positive integer.")
        
        if not isinstance(image_height, int) or image_height <= 0:
            raise ModelValidationError(f"Invalid image_height: {image_height}. Must be positive integer.")
        
        logger.info(f"Validated config.yaml: {image_width}x{image_height}")
        return image_width, image_height
        
    except yaml.YAMLError as e:
        raise ModelValidationError(f"Invalid YAML in config.yaml: {str(e)}")


def validate_mochi_json(mochi_path: str) -> Tuple[List[int], str]:
    """
    Validate mochi.json structure and extract input shape
    Returns: (input_shape, model_type)
    """
    try:
        with open(mochi_path, 'r') as f:
            mochi = json.load(f)
        
        if not mochi:
            raise ModelValidationError("mochi.json is empty")
        
        if 'stages' not in mochi:
            raise ModelValidationError("mochi.json missing 'stages' array")
        
        stages = mochi['stages']
        if not stages or not isinstance(stages, list):
            raise ModelValidationError("mochi.json 'stages' must be a non-empty array")
        
        first_stage = stages[0]
        
        if 'input_shape' not in first_stage:
            raise ModelValidationError("mochi.json missing 'stages[0].input_shape'")
        
        if 'type' not in first_stage:
            raise ModelValidationError("mochi.json missing 'stages[0].type'")
        
        input_shape = first_stage['input_shape']
        model_type = first_stage['type']
        
        # Validate input_shape format [N, C, H, W]
        if not isinstance(input_shape, list) or len(input_shape) != 4:
            raise ModelValidationError(
                f"Invalid input_shape: {input_shape}. Must be [batch, channels, height, width]"
            )
        
        for i, dim in enumerate(input_shape):
            if not isinstance(dim, int) or dim <= 0:
                raise ModelValidationError(
                    f"Invalid input_shape dimension at index {i}: {dim}. Must be positive integer."
                )
        
        logger.info(f"Validated mochi.json: input_shape={input_shape}, type={model_type}")
        return input_shape, model_type
        
    except json.JSONDecodeError as e:
        raise ModelValidationError(f"Invalid JSON in mochi.json: {str(e)}")


def validate_manifest_json(manifest_path: str) -> Dict:
    """
    Validate export_artifacts/manifest.json structure
    Returns: manifest data
    """
    try:
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        if not manifest:
            raise ModelValidationError("manifest.json is empty")
        
        if 'model_graph' not in manifest:
            raise ModelValidationError("manifest.json missing 'model_graph'")
        
        # Check for input_shape in manifest or model_graph
        input_shape = manifest.get('input_shape')
        if not input_shape:
            # Try to get from model_graph stages
            stages = manifest.get('model_graph', {}).get('stages', [])
            if stages:
                input_shape = stages[0].get('input_shape')
        
        if not input_shape:
            raise ModelValidationError(
                "manifest.json missing 'input_shape'. Must be in root or model_graph.stages[0]"
            )
        
        logger.info(f"Validated manifest.json: input_shape={input_shape}")
        return manifest
        
    except json.JSONDecodeError as e:
        raise ModelValidationError(f"Invalid JSON in manifest.json: {str(e)}")


def find_model_artifact_file(export_artifacts_dir: str) -> Tuple[str, str]:
    """
    Find the model weight file in export_artifacts. Accepts a PyTorch model
    (.pt, legacy DLR/Neo path) or an ONNX model (.onnx, pluggable ONNX Runtime
    engine — used for BYO detection/segmentation models).

    Returns: (filename, framework) where framework is 'PYTORCH' or 'ONNX'.
    """
    pt_files = []
    onnx_files = []

    for file in os.listdir(export_artifacts_dir):
        if file.endswith('.pt'):
            pt_files.append(file)
        elif file.endswith('.onnx'):
            onnx_files.append(file)

    # Prefer .pt (legacy path); fall back to .onnx.
    if pt_files:
        if len(pt_files) > 1:
            logger.warning(f"Multiple .pt files found: {pt_files}. Using first: {pt_files[0]}")
        return pt_files[0], 'PYTORCH'
    if onnx_files:
        if len(onnx_files) > 1:
            logger.warning(f"Multiple .onnx files found: {onnx_files}. Using first: {onnx_files[0]}")
        return onnx_files[0], 'ONNX'

    raise ModelValidationError(
        "No model weight file found in export_artifacts/. "
        "Model must include a PyTorch (.pt) or ONNX (.onnx) model file."
    )


def validate_dimensions_match(
    config_width: int, 
    config_height: int, 
    input_shape: List[int]
) -> None:
    """
    Validate that config.yaml dimensions match input_shape
    input_shape format: [batch, channels, height, width]
    """
    shape_height = input_shape[2]
    shape_width = input_shape[3]
    
    if config_width != shape_width or config_height != shape_height:
        raise ModelValidationError(
            f"Dimension mismatch: config.yaml has {config_width}x{config_height}, "
            f"but input_shape is [_, _, {shape_height}, {shape_width}]. "
            "Image dimensions must match."
        )


def validate_model_artifact(model_s3_uri: str, credentials: Dict) -> Dict:
    """
    Download and validate model artifact structure
    Returns: validation result with extracted metadata
    """
    temp_dir = None
    validation_errors = []
    validation_warnings = []
    
    try:
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        # Create S3 client (assumed role for multi-account; Lambda execution
        # role for single-account setups where the root ARN can't be assumed).
        if credentials.get('is_default_credentials'):
            s3_client = boto3.client('s3')
        else:
            s3_client = boto3.client(
                's3',
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_validation_")
        
        # Download model artifact
        local_tar = os.path.join(temp_dir, 'model.tar.gz')
        logger.info(f"Downloading model from {model_s3_uri}")
        
        try:
            s3_client.download_file(bucket, key, local_tar)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == '404' or error_code == 'NoSuchKey':
                raise ModelValidationError(f"Model artifact not found at {model_s3_uri}")
            elif error_code == 'AccessDenied':
                raise ModelValidationError(
                    f"Access denied to {model_s3_uri}. "
                    "Ensure the UseCase role has permission to read from this bucket."
                )
            raise
        
        # Verify it's a valid tar.gz
        if not tarfile.is_tarfile(local_tar):
            raise ModelValidationError(
                "Model artifact is not a valid tar.gz file. "
                "Please provide a gzipped tar archive."
            )
        
        # Extract tar.gz
        extract_dir = os.path.join(temp_dir, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)
        
        logger.info("Extracting model archive")
        with tarfile.open(local_tar, 'r:gz') as tar:
            tar.extractall(extract_dir)
        
        # Check required files exist
        missing_files = []
        for required_file, description in REQUIRED_FILES.items():
            file_path = os.path.join(extract_dir, required_file)
            if not os.path.exists(file_path):
                missing_files.append(f"  - {required_file}: {description}")
        
        if missing_files:
            raise ModelValidationError(
                "Missing required files in model artifact:\n" + "\n".join(missing_files),
                details=missing_files
            )
        
        # Validate each file and extract metadata
        config_path = os.path.join(extract_dir, 'config.yaml')
        mochi_path = os.path.join(extract_dir, 'mochi.json')
        manifest_path = os.path.join(extract_dir, 'export_artifacts', 'manifest.json')
        export_artifacts_dir = os.path.join(extract_dir, 'export_artifacts')
        
        # Validate config.yaml
        image_width, image_height = validate_config_yaml(config_path)
        
        # Validate mochi.json
        input_shape, model_type = validate_mochi_json(mochi_path)
        
        # Validate manifest.json
        manifest = validate_manifest_json(manifest_path)
        
        # Find the model weight file (.pt PyTorch or .onnx ONNX Runtime)
        model_file, framework = find_model_artifact_file(export_artifacts_dir)
        
        # Validate dimensions match
        validate_dimensions_match(image_width, image_height, input_shape)
        
        # Build validation result
        result = {
            'valid': True,
            'model_s3_uri': model_s3_uri,
            'metadata': {
                'image_width': image_width,
                'image_height': image_height,
                'input_shape': input_shape,
                'model_type': model_type,
                'pt_file': model_file,
                'model_file': model_file,
                'framework': framework,
                'framework_version': '1.8' if framework == 'PYTORCH' else 'onnx'
            },
            'files_found': list(REQUIRED_FILES.keys()) + [f'export_artifacts/{model_file}'],
            'warnings': validation_warnings
        }
        
        logger.info(f"Model validation successful: {result['metadata']}")
        return result
        
    except ModelValidationError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during validation: {str(e)}")
        raise ModelValidationError(f"Validation failed: {str(e)}")
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def validate_model(event: Dict, context: Any) -> Dict:
    """
    Validate a model artifact without importing
    POST /api/v1/models/validate
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.tar.gz"
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate S3 URI format
        if not model_s3_uri.startswith('s3://'):
            return create_response(400, {
                'error': 'Invalid model_s3_uri. Must be an S3 URI (s3://bucket/path/model.tar.gz)'
            })
        
        if not model_s3_uri.endswith('.tar.gz'):
            return create_response(400, {
                'error': 'Model artifact must be a .tar.gz file'
            })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"validate-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Validate model artifact
        validation_result = validate_model_artifact(model_s3_uri, credentials)
        
        return create_response(200, validation_result)
        
    except ModelValidationError as e:
        logger.error(f"Model validation failed: {e.message}")
        return create_response(400, {
            'valid': False,
            'error': e.message,
            'details': e.details
        })
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def import_model(event: Dict, context: Any) -> Dict:
    """
    Import a pre-trained model (BYOM)
    POST /api/v1/models/import
    
    Request body:
    {
        "usecase_id": "string",
        "model_name": "string",
        "model_version": "string",
        "model_s3_uri": "s3://bucket/path/model.tar.gz",
        "description": "string",  // optional
        "auto_compile": true,  // optional, default false
        "compilation_targets": ["x86_64-cpu", "jetson-xavier"]  // optional
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_name', 'model_version', 'model_s3_uri']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_name = body['model_name'].strip()
        model_version = body['model_version'].strip()
        model_s3_uri = body['model_s3_uri'].strip()
        description = body.get('description', '')
        auto_compile = body.get('auto_compile', False)
        compilation_targets = body.get('compilation_targets', [])
        
        # Check user access (DataScientist role required)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate S3 URI format
        if not model_s3_uri.startswith('s3://'):
            return create_response(400, {
                'error': 'Invalid model_s3_uri. Must be an S3 URI (s3://bucket/path/model.tar.gz)'
            })
        
        if not model_s3_uri.endswith('.tar.gz'):
            return create_response(400, {
                'error': 'Model artifact must be a .tar.gz file'
            })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"import-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Validate model artifact
        logger.info(f"Validating model artifact: {model_s3_uri}")
        validation_result = validate_model_artifact(model_s3_uri, credentials)
        
        if not validation_result.get('valid'):
            return create_response(400, {
                'error': 'Model validation failed',
                'validation_result': validation_result
            })
        
        # Generate unique training ID (we reuse training_jobs table for imported models)
        training_id = str(uuid.uuid4())
        
        # Determine model type from validation
        metadata = validation_result['metadata']
        model_type = metadata.get('model_type', 'imported')
        
        # Map model type to standard types if possible
        model_type_mapping = {
            'anomaly_detection': 'classification',
            'segmentation': 'segmentation',
            'classification': 'classification'
        }
        normalized_model_type = model_type_mapping.get(model_type.lower(), model_type)
        
        # Store imported model in training jobs table
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        training_item = {
            'training_id': training_id,
            'usecase_id': usecase_id,
            'model_name': model_name,
            'model_version': model_version,
            'model_type': normalized_model_type,
            'description': description,
            'source': 'imported',  # Mark as imported model
            'artifact_s3': model_s3_uri,
            'status': 'Completed',  # Imported models are already "trained"
            'progress': 100,
            'validation_result': validation_result,
            'metadata': metadata,
            'created_by': user['email'],
            'created_at': timestamp,
            'updated_at': timestamp,
            'completed_at': timestamp,
            'auto_compile': auto_compile,
            'compilation_targets': compilation_targets
        }
        
        table.put_item(Item=training_item)
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='import_model',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'model_name': model_name,
                'model_version': model_version,
                'model_s3_uri': model_s3_uri,
                'model_type': normalized_model_type,
                'auto_compile': auto_compile
            }
        )
        
        logger.info(f"Model imported successfully: {training_id}")
        
        # If auto_compile is enabled, trigger compilation
        if auto_compile and compilation_targets:
            try:
                logger.info(f"Auto-compile enabled, triggering compilation for targets: {compilation_targets}")
                
                # Invoke compilation Lambda
                lambda_client = boto3.client('lambda')
                compilation_function_name = os.environ.get('COMPILATION_FUNCTION_NAME')
                
                if compilation_function_name:
                    compilation_event = {
                        'httpMethod': 'POST',
                        'path': f'/api/v1/training/{training_id}/compile',
                        'pathParameters': {'id': training_id},
                        'body': json.dumps({
                            'targets': compilation_targets,
                            'auto_triggered': True
                        }),
                        'requestContext': {
                            'authorizer': {
                                'claims': {
                                    'sub': user_id,
                                    'email': user['email'],
                                    'cognito:username': user.get('username', user_id)
                                }
                            }
                        }
                    }
                    
                    lambda_client.invoke(
                        FunctionName=compilation_function_name,
                        InvocationType='Event',  # Async
                        Payload=json.dumps(compilation_event)
                    )
                    
                    logger.info(f"Triggered compilation for imported model {training_id}")
                else:
                    logger.warning("COMPILATION_FUNCTION_NAME not set, skipping auto-compile")
                    
            except Exception as e:
                logger.error(f"Error triggering auto-compile: {str(e)}")
                # Don't fail the import if compilation trigger fails
        
        return create_response(201, {
            'training_id': training_id,
            'model_name': model_name,
            'model_version': model_version,
            'status': 'Completed',
            'source': 'imported',
            'validation_result': validation_result,
            'message': 'Model imported successfully',
            'auto_compile_triggered': auto_compile and bool(compilation_targets)
        })
        
    except ModelValidationError as e:
        logger.error(f"Model validation failed: {e.message}")
        return create_response(400, {
            'error': f'Model validation failed: {e.message}',
            'details': e.details
        })
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_model_format_spec(event: Dict, context: Any) -> Dict:
    """
    Get the required model format specification
    GET /api/v1/models/format-spec
    """
    spec = {
        'description': 'DDA-compatible model artifact format specification',
        'format': 'tar.gz',
        'framework': 'PyTorch 1.8',
        'required_structure': {
            'config.yaml': {
                'description': 'Configuration file with image dimensions',
                'required_fields': {
                    'dataset.image_width': 'Positive integer - input image width',
                    'dataset.image_height': 'Positive integer - input image height'
                },
                'example': '''dataset:
  image_width: 224
  image_height: 224'''
            },
            'mochi.json': {
                'description': 'Model graph definition with input shape',
                'required_fields': {
                    'stages[0].type': 'Model type (e.g., "anomaly_detection")',
                    'stages[0].input_shape': 'Array [batch, channels, height, width]'
                },
                'example': '''{
  "stages": [{
    "type": "anomaly_detection",
    "input_shape": [1, 3, 224, 224]
  }]
}'''
            },
            'export_artifacts/manifest.json': {
                'description': 'Model metadata and compilable models info',
                'required_fields': {
                    'model_graph': 'Model graph structure',
                    'input_shape': 'Input shape array (can be in root or model_graph.stages[0])'
                }
            },
            'export_artifacts/*.pt': {
                'description': 'PyTorch model file',
                'notes': 'Single .pt file containing the trained model weights'
            }
        },
        'validation_rules': [
            'Image dimensions in config.yaml must match input_shape[2] (height) and input_shape[3] (width)',
            'input_shape must be [batch, channels, height, width] format',
            'All dimension values must be positive integers',
            'Model file must be PyTorch 1.8 compatible'
        ],
        'supported_compilation_targets': [
            'jetson-xavier',
            'jetson-xavier-jp5',
            'jetson-xavier-jp6',
            'x86_64-cpu',
            'x86_64-cuda',
            'arm64-cpu',
            'onnx'
        ]
    }
    
    return create_response(200, spec)


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to appropriate function"""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }
        
        # Route to appropriate handler
        if http_method == 'POST' and '/models/validate' in path:
            return validate_model(event, context)
        elif http_method == 'POST' and '/models/import' in path:
            return import_model(event, context)
        elif http_method == 'POST' and '/models/vllm' in path:
            return register_vllm_model(event, context)
        elif http_method == 'GET' and '/models/vllm/engine-spec' in path:
            return get_vllm_engine_spec(event, context)
        elif http_method == 'PUT' and VLLM_ENGINE_CONFIG_PATH_RE.search(path):
            return update_vllm_engine_configuration(event, context)
        elif http_method == 'GET' and '/models/format-spec' in path:
            return get_model_format_spec(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
