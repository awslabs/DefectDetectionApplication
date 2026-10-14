"""
Synthetic defect data generation Lambda (synthetic-defect-data-generation).

Single Lambda serving both the /api/v1/synthetic API routes and the async
generation worker (dispatch on an ``internal_action`` key in the event,
mirroring the portal's self-invocation pattern). Pure logic (placeholder
resolution, generation planning, approval filtering, bounding boxes,
manifest records/append) lives in ``synthetic_core.py``; this module
provides the I/O around it: DynamoDB persistence, RBAC + audit, Bedrock
image-model invocation, and the ETag-conditional S3 manifest write that
makes integration atomic.

Route matrix (all RBAC-gated with Data_Scientist_Access, Req 9.1/9.2):

    GET    /synthetic/models                              1.1, 1.3
    GET    /synthetic/prompt-templates                    2.2, 2.3
    PUT    /synthetic/prompt-templates                    2.1, 2.4
    POST   /synthetic/sessions                            10.1, 9.4
    GET    /synthetic/sessions                            10.4
    GET    /synthetic/sessions/{id}                       10.2, 5.2, 5.6
    PATCH  /synthetic/sessions/{id}                       1.2, 3.2-3.4
    POST   /synthetic/sessions/{id}/generate              2.5-2.6, 3.6, 4.1-4.4, 5.3
    POST   /synthetic/sessions/{id}/previews/approval     6.1, 6.2
    POST   /synthetic/sessions/{id}/integrate             6.3-6.6, 7.1-7.8, 9.4
    POST   /synthetic/sessions/{id}/retrain               8.2, 8.3
"""
import base64
import io
import json
import logging
import os
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, get_usecase,
    assume_usecase_role, create_boto3_client
)
from synthetic_core import (
    MODEL_CATALOG, DEFAULT_PROMPT_TEMPLATE,
    UnresolvedPlaceholderError, ValidationError,
    filter_available_models, invocation_model_id, resolve_prompt,
    validate_generation_request, build_generation_plan,
    select_generation_method, derive_mask_rect,
    build_amazon_request_body, build_stability_inpaint_request_body,
    extract_stability_result, classify_bedrock_invocation_error,
    select_approved, bbox_from_diff,
    build_manifest_record, append_manifest_lines,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Module-level resource (patched by moto in tests).
dynamodb = boto3.resource('dynamodb')

# Cached portal-account Bedrock clients (created lazily so tests can run
# without ever touching Bedrock).
_bedrock = None
_bedrock_runtime = None

STAGING_PREFIX = 'synthetic-staging'
MANIFEST_WRITE_RETRIES = 3
DIFF_THRESHOLD = 10

MODELS_EMPTY_GUIDANCE = (
    "No image generation models are available. Enable model access for "
    "Amazon Nova Canvas (amazon.nova-canvas-v1:0), Amazon Titan Image "
    "Generator v2 (amazon.titan-image-generator-v2:0), or Stability "
    "Stable Image Inpaint (stability.stable-image-inpaint-v1:0) in the "
    "Amazon Bedrock console (Model access) for the portal region."
)

VALID_SESSION_STATUSES = (
    'draft', 'generating', 'awaiting_review', 'approved', 'integrated',
    'failed',
)

# META fields a PATCH may update (Req 1.2, 3.2-3.4).
PATCHABLE_FIELDS = (
    'generation_model_id', 'object_type', 'defect_type',
    'prompt_template_text', 'source_class', 'source_images',
    'generation_params', 'target_dataset_prefix', 'target_manifest_key',
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sessions_table():
    return dynamodb.Table(os.environ.get('SYNTHETIC_SESSIONS_TABLE',
                                         'SyntheticSessionsTable'))


def _templates_table():
    return dynamodb.Table(os.environ.get('PROMPT_TEMPLATES_TABLE',
                                         'PromptTemplatesTable'))


def _now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def _to_ddb(value):
    """Recursively convert floats to Decimal for DynamoDB storage."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_ddb(v) for v in value]
    return value


def _from_ddb(value):
    """Recursively convert Decimals back to int/float."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client('bedrock')
    return _bedrock


def _bedrock_runtime_client():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client('bedrock-runtime')
    return _bedrock_runtime


def _list_available_models() -> List[Dict]:
    """IMAGE-modality model summaries in the portal region with
    lifecycle status (Req 5.1, 6.2). A missing ``modelLifecycle``
    defaults to ACTIVE (fails open rather than emptying the dropdown)."""
    response = _bedrock_client().list_foundation_models(
        byOutputModality='IMAGE')
    return [{
        'model_id': summary['modelId'],
        'lifecycle_status': summary.get('modelLifecycle', {}).get(
            'status', 'ACTIVE'),
    } for summary in response.get('modelSummaries', [])]


def _model_entry(model_id: str) -> Optional[Dict]:
    for entry in MODEL_CATALOG:
        if entry['model_id'] == model_id:
            return entry
    return None


def _invoke_worker_async(payload: Dict) -> None:
    """Self-invoke this Lambda asynchronously for the generation worker."""
    function_name = (os.environ.get('SYNTHETIC_DATA_FUNCTION_NAME')
                     or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
    boto3.client('lambda').invoke(
        FunctionName=function_name,
        InvocationType='Event',
        Payload=json.dumps(payload).encode('utf-8'),
    )


# ---------------------------------------------------------------------------
# Cross-account data bucket access
#
# Local copy of the datasets.py logic (deliberately NOT imported from
# datasets.py: the parallel data-labeling branch may touch that file, and
# this feature must not couple to it).
# ---------------------------------------------------------------------------

def get_data_bucket_and_credentials(usecase):
    """
    Get the appropriate bucket and credentials for data access.
    Uses Data Account if configured, otherwise falls back to UseCase Account.
    """
    # Check if separate data account is configured
    data_role_arn = usecase.get('data_account_role_arn')

    if data_role_arn:
        # Use Data Account - external ID is required for production
        external_id = usecase.get('data_account_external_id')
        if not external_id:
            raise ValueError(
                "data_account_external_id is required when using a separate "
                "Data Account. Please update the UseCase configuration with "
                "the external ID."
            )
        credentials = assume_usecase_role(
            data_role_arn,
            external_id,
            'data-access'
        )
        bucket = usecase.get('data_s3_bucket') or usecase.get('s3_bucket')
    else:
        # Use UseCase Account (this one requires external_id)
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            'data-access'
        )
        bucket = usecase['s3_bucket']

    return bucket, None, credentials


def _data_s3_client(usecase) -> Tuple[Any, str]:
    """(s3_client, bucket) for the Use_Case data bucket.

    Kept as a single seam so tests can wrap the client (e.g. with a
    failure-injecting proxy for the integration atomicity property).
    """
    bucket, _, credentials = get_data_bucket_and_credentials(usecase)
    return create_boto3_client('s3', credentials), bucket


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _load_session(session_id: str) -> Tuple[Optional[Dict], List[Dict]]:
    """(META item, PREVIEW items) for a session; (None, []) if absent."""
    from boto3.dynamodb.conditions import Key
    response = _sessions_table().query(
        KeyConditionExpression=Key('session_id').eq(session_id))
    meta, previews = None, []
    for item in response.get('Items', []):
        if item.get('sk') == 'META':
            meta = item
        elif str(item.get('sk', '')).startswith('PREVIEW#'):
            previews.append(item)
    previews.sort(key=lambda p: (p.get('created_at', 0), str(p.get('sk'))))
    return meta, previews


def _store_preview(item: Dict) -> None:
    _sessions_table().put_item(Item=_to_ddb(item))


def _record_last_failure(session_id: str, reason: str) -> None:
    """Record a failure reason on the session META (Req 1.4, 7.7)."""
    try:
        _sessions_table().update_item(
            Key={'session_id': session_id, 'sk': 'META'},
            UpdateExpression='SET last_failure = :f, updated_at = :t',
            ExpressionAttributeValues={
                ':f': {'reason': str(reason)[:2000], 'at': _now_ms()},
                ':t': _now_ms(),
            },
        )
    except ClientError:
        logger.exception('Failed to record last_failure on session %s',
                         session_id)


# ---------------------------------------------------------------------------
# RBAC gate (Req 9.1, 9.2)
# ---------------------------------------------------------------------------

def _authorize(event: Dict, usecase_id: Optional[str], resource_type: str,
               resource_id: str) -> Tuple[Dict, Optional[Dict]]:
    """(user, error_response). Every synthetic route calls this before any
    handler logic. Denial returns 403 and logs an unauthorized_access audit
    event (Req 9.2). check_user_access treats UseCaseAdmin / PortalAdmin as
    satisfying DataScientist via the role hierarchy (Req 9.1)."""
    user = get_user_from_event(event)
    if not usecase_id:
        return user, create_response(400, {'error': 'usecase_id is required'})
    if not check_user_access(user['user_id'], usecase_id, 'DataScientist',
                             user_info=user):
        log_audit_event(
            user_id=user['user_id'],
            action='unauthorized_access',
            resource_type=resource_type,
            resource_id=resource_id,
            result='denied',
            details={
                'usecase_id': usecase_id,
                'required_role': 'DataScientist',
                'route': f"{event.get('httpMethod')} {event.get('resource')}",
            },
        )
        return user, create_response(
            403, {'error': 'DataScientist access required'})
    return user, None


# ---------------------------------------------------------------------------
# Model catalog endpoint (Req 1.1, 1.3)
# ---------------------------------------------------------------------------

def get_models(event: Dict) -> Dict:
    """GET /synthetic/models?usecase_id="""
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    user, denial = _authorize(event, usecase_id, 'synthetic_models', 'catalog')
    if denial:
        return denial

    try:
        available_models = _list_available_models()
    except Exception as exc:  # Bedrock unavailable -> treat as empty catalog
        logger.error('ListFoundationModels failed: %s', exc)
        available_models = []

    models = filter_available_models(MODEL_CATALOG, available_models)
    body: Dict[str, Any] = {'models': models}
    if not models:
        body['guidance'] = MODELS_EMPTY_GUIDANCE
    return create_response(200, body)


# ---------------------------------------------------------------------------
# Prompt template endpoints (Req 2.1-2.4)
# ---------------------------------------------------------------------------

def _template_key(object_type: str, defect_type: str) -> str:
    return f"{object_type}#{defect_type}"


def get_prompt_template(event: Dict) -> Dict:
    """GET /synthetic/prompt-templates?usecase_id=&object_type=&defect_type="""
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    object_type = params.get('object_type')
    defect_type = params.get('defect_type')
    user, denial = _authorize(event, usecase_id, 'prompt_template',
                              _template_key(object_type or '',
                                            defect_type or ''))
    if denial:
        return denial
    if not object_type or not defect_type:
        return create_response(
            400, {'error': 'object_type and defect_type are required'})

    response = _templates_table().get_item(Key={
        'usecase_id': usecase_id,
        'template_key': _template_key(object_type, defect_type),
    })
    item = response.get('Item')
    if item:
        return create_response(200, {
            'template_text': item['template_text'],
            'object_type': object_type,
            'defect_type': defect_type,
            'is_default': False,
        })
    # No stored template: return the default containing both placeholder
    # variables (Req 2.3).
    return create_response(200, {
        'template_text': DEFAULT_PROMPT_TEMPLATE,
        'object_type': object_type,
        'defect_type': defect_type,
        'is_default': True,
    })


def put_prompt_template(event: Dict) -> Dict:
    """PUT /synthetic/prompt-templates"""
    body = json.loads(event.get('body') or '{}')
    usecase_id = body.get('usecase_id')
    user, denial = _authorize(event, usecase_id, 'prompt_template',
                              _template_key(body.get('object_type', ''),
                                            body.get('defect_type', '')))
    if denial:
        return denial
    error = validate_required_fields(
        body, ['usecase_id', 'object_type', 'defect_type', 'template_text'])
    if error:
        return create_response(400, {'error': error})

    object_type = body['object_type']
    defect_type = body['defect_type']
    _templates_table().put_item(Item=_to_ddb({
        'usecase_id': usecase_id,
        'template_key': _template_key(object_type, defect_type),
        'object_type': object_type,
        'defect_type': defect_type,
        'template_text': body['template_text'],
        'updated_by': user['user_id'],
        'updated_at': _now_ms(),
    }))
    return create_response(200, {
        'template_text': body['template_text'],
        'object_type': object_type,
        'defect_type': defect_type,
        'is_default': False,
    })


# ---------------------------------------------------------------------------
# Generation_Session endpoints (Req 10.1, 10.2, 10.4, 9.4)
# ---------------------------------------------------------------------------

def create_session(event: Dict) -> Dict:
    """POST /synthetic/sessions"""
    body = json.loads(event.get('body') or '{}')
    usecase_id = body.get('usecase_id')
    user, denial = _authorize(event, usecase_id, 'synthetic_session', 'new')
    if denial:
        return denial
    error = validate_required_fields(body, ['usecase_id'])
    if error:
        return create_response(400, {'error': error})

    session_id = str(uuid.uuid4())
    timestamp = _now_ms()
    meta = {
        'session_id': session_id,
        'sk': 'META',
        'usecase_id': usecase_id,
        'status': 'draft',
        'generation_model_id': body.get('generation_model_id'),
        'object_type': body.get('object_type'),
        'defect_type': body.get('defect_type'),
        'prompt_template_text': body.get('prompt_template_text'),
        'source_class': body.get('source_class'),
        'source_images': body.get('source_images', []),
        'generation_params': body.get('generation_params', {}),
        'generation_pass': 0,
        'target_dataset_prefix': body.get('target_dataset_prefix'),
        'target_manifest_key': body.get('target_manifest_key'),
        'created_by': user['user_id'],
        'created_at': timestamp,
        'updated_at': timestamp,
    }
    _sessions_table().put_item(Item=_to_ddb(meta))

    # Session-created audit event (Req 9.4).
    log_audit_event(
        user_id=user['user_id'],
        action='create_generation_session',
        resource_type='synthetic_session',
        resource_id=session_id,
        result='success',
        details={'usecase_id': usecase_id, 'session_id': session_id},
    )
    return create_response(201, {'session': _from_ddb(meta)})


def list_sessions(event: Dict) -> Dict:
    """GET /synthetic/sessions?usecase_id="""
    params = event.get('queryStringParameters') or {}
    usecase_id = params.get('usecase_id')
    user, denial = _authorize(event, usecase_id, 'synthetic_session', 'list')
    if denial:
        return denial

    from boto3.dynamodb.conditions import Key
    response = _sessions_table().query(
        IndexName='usecase-index',
        KeyConditionExpression=Key('usecase_id').eq(usecase_id),
        ScanIndexForward=False,
    )
    sessions = [
        {
            'session_id': item['session_id'],
            'status': item.get('status'),
            'created_at': item.get('created_at'),
            'object_type': item.get('object_type'),
            'defect_type': item.get('defect_type'),
            'generation_model_id': item.get('generation_model_id'),
        }
        for item in response.get('Items', [])
        if item.get('sk') == 'META'
    ]
    return create_response(200, {'sessions': _from_ddb(sessions),
                                 'count': len(sessions)})


def _presign_staging_url(s3_client, bucket: str, key: str) -> Optional[str]:
    try:
        return s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=3600)
    except Exception:
        logger.exception('Failed to presign %s', key)
        return None


def get_session(event: Dict) -> Dict:
    """GET /synthetic/sessions/{id} - META + previews with presigned
    thumbnails and per-preview resolved prompt text (Req 10.2, 5.2, 5.6)."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, previews = _load_session(session_id)
    if meta is None:
        # Still authorize on the (unknown) session so probing without
        # access does not leak existence -- but we have no usecase to gate
        # on; return 404 only to authorized users of *some* usecase is not
        # possible, so 404 here.
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    meta = _from_ddb(meta)
    previews = _from_ddb(previews)
    try:
        usecase = get_usecase(meta['usecase_id'])
        s3_client, bucket = _data_s3_client(usecase)
        for preview in previews:
            staging_key = preview.get('staging_key')
            if staging_key and preview.get('status') == 'completed':
                preview['thumbnail_url'] = _presign_staging_url(
                    s3_client, bucket, staging_key)
    except Exception:
        logger.exception('Presigning thumbnails failed for session %s',
                         session_id)
    return create_response(200, {'session': meta, 'previews': previews})


def patch_session(event: Dict) -> Dict:
    """PATCH /synthetic/sessions/{id} - update model selection, source
    images / classification, generation params (Req 1.2, 3.2-3.4)."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, _ = _load_session(session_id)
    if meta is None:
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    body = json.loads(event.get('body') or '{}')
    updates = {k: body[k] for k in PATCHABLE_FIELDS if k in body}
    if not updates:
        return create_response(400, {'error': 'No updatable fields supplied'})
    if 'source_class' in updates and updates['source_class'] not in (
            'defect', 'normal'):
        return create_response(400, {
            'error': "Source images must be classified as 'defect' or "
                     "'normal'"})

    expression_parts = []
    values = {':t': _now_ms()}
    names = {}
    for index, (key, value) in enumerate(updates.items()):
        placeholder = f":v{index}"
        name = f"#f{index}"
        expression_parts.append(f"{name} = {placeholder}")
        values[placeholder] = _to_ddb(value)
        names[name] = key
    names['#u'] = 'updated_at'
    update_expression = 'SET ' + ', '.join(expression_parts) + ', #u = :t'

    _sessions_table().update_item(
        Key={'session_id': session_id, 'sk': 'META'},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=values,
        ExpressionAttributeNames=names,
    )
    meta, _ = _load_session(session_id)
    return create_response(200, {'session': _from_ddb(meta)})


# ---------------------------------------------------------------------------
# Generate endpoint (Req 2.5, 2.6, 3.6, 4.1-4.4, 5.1, 5.3)
# ---------------------------------------------------------------------------

def _source_key(source_ref) -> str:
    """S3 key of a Source_Image reference (dict with 'key' or plain str)."""
    if isinstance(source_ref, dict):
        return source_ref.get('key', '')
    return str(source_ref)


def _scoped_sources(meta: Dict, previews: List[Dict], body: Dict):
    """(source_images, variation_count_override) for the regeneration
    scope: all | source_image | preview (Req 5.3)."""
    scope = body.get('scope', 'all')
    sources = body.get('source_images') or meta.get('source_images') or []
    if scope == 'source_image':
        wanted = body.get('source_image_key')
        sources = [s for s in sources if _source_key(s) == wanted]
        return sources, None
    if scope == 'preview':
        preview_id = body.get('preview_id')
        match = next((p for p in previews
                      if p.get('preview_id') == preview_id), None)
        if match is None:
            return [], None
        wanted = match.get('source_image_key')
        matched = [s for s in sources if _source_key(s) == wanted]
        return (matched or [wanted]), 1
    return sources, None


def generate(event: Dict) -> Dict:
    """POST /synthetic/sessions/{id}/generate

    Validates, resolves placeholders, persists the plan under an
    incremented generation_pass, self-invokes the async worker, and
    returns 202 (Req 5.1)."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, previews = _load_session(session_id)
    if meta is None:
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    meta = _from_ddb(meta)
    body = json.loads(event.get('body') or '{}')

    model_id = body.get('generation_model_id') or meta.get(
        'generation_model_id')
    if not model_id or _model_entry(model_id) is None:
        return create_response(400, {
            'error': 'A generation model from the catalog must be selected'})

    # Edited prompt (regeneration) takes precedence over the stored one.
    template = body.get('prompt_template_text') \
        or meta.get('prompt_template_text') or DEFAULT_PROMPT_TEMPLATE
    context = {
        'object_type': meta.get('object_type'),
        'defect_type': body.get('defect_type') or meta.get('defect_type'),
    }
    context.update(body.get('prompt_context') or {})
    context = {k: v for k, v in context.items() if v is not None}

    # Resolve placeholders (Req 2.5); reject listing every unresolved
    # name (Req 2.6).
    try:
        resolved_prompt = resolve_prompt(template, context)
    except UnresolvedPlaceholderError as exc:
        return create_response(400, {
            'error': str(exc),
            'unresolved_placeholders': exc.names,
        })

    source_class = body.get('source_class') or meta.get('source_class')
    defect_type = body.get('defect_type') or meta.get('defect_type')
    sources, count_override = _scoped_sources(meta, _from_ddb(previews), body)
    variation_count = count_override if count_override is not None else (
        body.get('variation_count')
        or (meta.get('generation_params') or {}).get('variation_count'))

    # Validate sources / classification / count (Req 3.2, 3.3, 3.6,
    # 4.1, 4.4) and the model's support for the required generation
    # method (Req 3.1, 3.5) -- both rejected with 400 before any plan
    # persists.
    try:
        validated = validate_generation_request(
            sources, source_class, defect_type, variation_count)
        select_generation_method(
            source_class, _model_entry(model_id).get('capabilities', {}))
    except ValidationError as exc:
        return create_response(400, {'error': str(exc)})

    params = dict(meta.get('generation_params') or {})
    params.update(body.get('generation_params') or {})
    params['variation_count'] = validated['variation_count']

    plan = build_generation_plan(
        {'generation_model_id': model_id},
        validated['source_images'],
        validated['variation_count'],
        resolved_prompt,
        {k: v for k, v in params.items() if k != 'variation_count'},
    )

    generation_pass = int(meta.get('generation_pass', 0)) + 1
    _sessions_table().update_item(
        Key={'session_id': session_id, 'sk': 'META'},
        UpdateExpression=(
            'SET #s = :status, generation_pass = :pass, '
            'generation_plan = :plan, generation_model_id = :model, '
            'generation_params = :params, prompt_template_text = :template, '
            'resolved_prompt = :prompt, source_class = :cls, '
            'defect_type = :defect, updated_at = :t '
            'REMOVE last_failure'
        ),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues=_to_ddb({
            ':status': 'generating',
            ':pass': generation_pass,
            ':plan': plan,
            ':model': model_id,
            ':params': params,
            ':template': template,
            ':prompt': resolved_prompt,
            ':cls': source_class,
            ':defect': defect_type,
            ':t': _now_ms(),
        }),
    )

    _invoke_worker_async({
        'internal_action': 'generation_worker',
        'session_id': session_id,
        'generation_pass': generation_pass,
    })
    return create_response(202, {
        'session_id': session_id,
        'status': 'generating',
        'generation_pass': generation_pass,
        'task_count': len(plan),
    })


# ---------------------------------------------------------------------------
# Async generation worker (Req 1.4, 4.2, 4.5, 5.2, 5.4)
# ---------------------------------------------------------------------------

def _render_mask_png(rect: Dict, width: int, height: int) -> bytes:
    """Binary mask PNG matching the source dimensions (Req 3.2): white
    (255) inside ``rect`` (the region to inpaint), black (0) elsewhere."""
    from PIL import Image, ImageDraw
    mask = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(mask)
    # ImageDraw.rectangle is inclusive of both corners.
    draw.rectangle(
        [rect['left'], rect['top'],
         rect['left'] + rect['width'] - 1,
         rect['top'] + rect['height'] - 1],
        fill=255,
    )
    buffer = io.BytesIO()
    mask.save(buffer, format='PNG')
    return buffer.getvalue()


def _source_image_dimensions(image_bytes: bytes,
                             source_key: str = '') -> Tuple[int, int]:
    """(width, height) of a source image via Pillow; raises naming the
    unreadable source image on decode failure."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.size
    except Exception as exc:
        raise RuntimeError(
            f"Source image {source_key or '<unknown>'} could not be "
            f"decoded to determine its dimensions: {exc}") from exc


def _invoke_image_model(model_id: str, request_body: Dict) -> bytes:
    """One Bedrock invoke_model call returning the generated image bytes."""
    response = _bedrock_runtime_client().invoke_model(
        modelId=model_id,
        contentType='application/json',
        accept='application/json',
        body=json.dumps(request_body),
    )
    payload = json.loads(response['body'].read())
    images = payload.get('images') or []
    if not images:
        raise RuntimeError(
            payload.get('error') or 'Model returned no images')
    return base64.b64decode(images[0])


def _invoke_stability_model(model_id: str, request_body: Dict) -> bytes:
    """One Bedrock invoke_model call against the Stability inference
    profile, returning the generated image bytes (Req 2.5, 4.2). Response
    parsing is delegated to synthetic_core.extract_stability_result."""
    response = _bedrock_runtime_client().invoke_model(
        modelId=model_id,
        contentType='application/json',
        accept='application/json',
        body=json.dumps(request_body),
    )
    payload = json.loads(response['body'].read())
    return base64.b64decode(extract_stability_result(payload))


def execute_generation_tasks(tasks: List[Dict], invoke_task,
                             on_result=None) -> Tuple[List[Dict], List[Dict]]:
    """Run the generation plan task-by-task with per-task failure
    isolation (Req 4.5): a failing task records its failure reason and the
    loop continues. Returns (completed_previews, failed_previews), which
    exactly partition the plan.

    ``invoke_task(task) -> dict`` performs the model invocation + staging
    write and returns extra preview fields (staging_key,
    generation_method, mask_region, ...). ``on_result(preview)`` is called
    for every preview as it completes (incremental persistence, Req 5.2).
    """
    completed: List[Dict] = []
    failed: List[Dict] = []
    for task in tasks:
        preview = {
            'preview_id': str(uuid.uuid4()),
            'source_image_key': _source_key(task.get('source_image')),
            'variation_index': task.get('variation_index'),
            'resolved_prompt': task.get('resolved_prompt'),
            'seed': task.get('seed'),
            'approval_state': 'pending',
        }
        try:
            extra = invoke_task(task)
            preview.update(extra or {})
            preview['status'] = 'completed'
            completed.append(preview)
        except Exception as exc:  # per-task isolation (Req 4.5)
            preview['status'] = 'failed'
            preview['failure_reason'] = str(exc) or type(exc).__name__
            failed.append(preview)
        if on_result is not None:
            on_result(preview)
    return completed, failed


def run_generation_worker(event: Dict) -> Dict:
    """Async worker: processes the persisted plan one Bedrock call per
    task, writing each preview as it completes. Status updates are guarded
    by the generation_pass conditional so a stale worker never overwrites
    a newer pass (Req 5.4)."""
    session_id = event['session_id']
    generation_pass = int(event['generation_pass'])
    meta, _ = _load_session(session_id)
    if meta is None:
        logger.error('Worker: session %s not found', session_id)
        return {'status': 'error', 'reason': 'session not found'}
    meta = _from_ddb(meta)
    tasks = meta.get('generation_plan') or []

    usecase = get_usecase(meta['usecase_id'])
    s3_client, bucket = _data_s3_client(usecase)
    model_entry = _model_entry(meta.get('generation_model_id')) or {}
    source_class = meta.get('source_class') or 'defect'
    method = select_generation_method(source_class,
                                      model_entry.get('capabilities', {}))
    mask_prompt = None
    if method == 'inpainting':
        mask_prompt = (
            f"the {meta.get('defect_type', 'defect')} region on the "
            f"{meta.get('object_type', 'part')}")

    def invoke_task(task):
        source_key = _source_key(task.get('source_image'))
        source_obj = s3_client.get_object(Bucket=bucket, Key=source_key)
        source_bytes = source_obj['Body'].read()
        source_b64 = base64.b64encode(source_bytes).decode()
        invoke_id = (invocation_model_id(model_entry) if model_entry
                     else task['model_id'])

        # Provider dispatch on the model-id prefix (Req 2.1).
        if task['model_id'].split('.', 1)[0] == 'stability':
            width, height = _source_image_dimensions(source_bytes,
                                                     source_key)
            rect = derive_mask_rect(task.get('seed'), width, height)
            mask_b64 = base64.b64encode(
                _render_mask_png(rect, width, height)).decode()
            request_body = build_stability_inpaint_request_body(
                task.get('resolved_prompt', ''), source_b64, mask_b64,
                task.get('seed'))
            extra_fields = {'generation_method': 'inpainting',
                            'mask_region': rect}
            invoke = _invoke_stability_model
        else:
            request_body = build_amazon_request_body(
                model_entry, method, task.get('resolved_prompt', ''),
                source_b64, task.get('seed'), task.get('params') or {},
                mask_prompt)
            extra_fields = {'generation_method': method}
            if mask_prompt:
                extra_fields['mask_prompt'] = mask_prompt
            invoke = _invoke_image_model

        try:
            image_bytes = invoke(invoke_id, request_body)
        except ClientError as exc:
            error = exc.response.get('Error', {})
            raise RuntimeError(classify_bedrock_invocation_error(
                error.get('Code', ''), error.get('Message', ''),
                invoke_id)) from exc

        preview_id = str(uuid.uuid4())
        staging_key = f"{STAGING_PREFIX}/{session_id}/{preview_id}.png"
        s3_client.put_object(Bucket=bucket, Key=staging_key,
                             Body=image_bytes, ContentType='image/png')
        extra = {
            'preview_id': preview_id,
            'staging_key': staging_key,
        }
        extra.update(extra_fields)
        return extra

    def on_result(preview):
        item = dict(preview)
        item['session_id'] = session_id
        item['sk'] = f"PREVIEW#{item['preview_id']}"
        item['generation_pass'] = generation_pass
        item['created_at'] = _now_ms()
        _store_preview(item)
        if item['status'] == 'failed':
            # Per-variation failure recorded on the session too (Req 1.4).
            _record_last_failure(session_id, item['failure_reason'])

    completed, failed = execute_generation_tasks(tasks, invoke_task,
                                                 on_result)

    # generation_pass-conditional status update: stale workers never
    # overwrite a newer pass (Req 5.4).
    try:
        _sessions_table().update_item(
            Key={'session_id': session_id, 'sk': 'META'},
            UpdateExpression='SET #s = :status, updated_at = :t',
            ConditionExpression='generation_pass = :pass',
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':status': 'awaiting_review',
                ':pass': generation_pass,
                ':t': _now_ms(),
            },
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        logger.info('Worker for stale pass %s of session %s: status left '
                    'untouched', generation_pass, session_id)
    return {'status': 'done', 'completed': len(completed),
            'failed': len(failed)}


# ---------------------------------------------------------------------------
# Approval endpoint (Req 6.1, 6.2, 9.4)
# ---------------------------------------------------------------------------

def set_preview_approval(event: Dict) -> Dict:
    """POST /synthetic/sessions/{id}/previews/approval

    Sets the approval state for the listed preview ids or all previews."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, previews = _load_session(session_id)
    if meta is None:
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    body = json.loads(event.get('body') or '{}')
    approval_state = body.get('approval_state')
    if approval_state not in ('approved', 'rejected', 'pending'):
        return create_response(400, {
            'error': "approval_state must be 'approved', 'rejected' or "
                     "'pending'"})
    if body.get('all'):
        targets = [p for p in previews if p.get('status') == 'completed']
    else:
        wanted = set(body.get('preview_ids') or [])
        if not wanted:
            return create_response(400, {
                'error': "preview_ids or all: true is required"})
        targets = [p for p in previews if p.get('preview_id') in wanted]

    for preview in targets:
        _sessions_table().update_item(
            Key={'session_id': session_id, 'sk': preview['sk']},
            UpdateExpression='SET approval_state = :a',
            ExpressionAttributeValues={':a': approval_state},
        )

    if approval_state == 'approved':
        # Session-approved audit event (Req 9.4).
        log_audit_event(
            user_id=user['user_id'],
            action='approve_generation_session',
            resource_type='synthetic_session',
            resource_id=session_id,
            result='success',
            details={'usecase_id': meta['usecase_id'],
                     'session_id': session_id,
                     'preview_count': len(targets)},
        )
    return create_response(200, {'updated': len(targets),
                                 'approval_state': approval_state})


# ---------------------------------------------------------------------------
# Integration endpoint (Req 6.3-6.6, 7.1-7.8, 9.4)
# ---------------------------------------------------------------------------

def _decode_image_pixels(image_bytes: bytes):
    """Pixel grid (list of rows of RGB tuples) via Pillow; None when the
    bytes cannot be decoded (bbox falls back to the full image)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert('RGB')
            width, height = rgb.size
            data = list(rgb.getdata())
        return [data[row * width:(row + 1) * width] for row in range(height)]
    except Exception:
        return None


def _annotate_preview(s3_client, bucket: str, preview: Dict,
                      generated_bytes: bytes) -> Tuple[Dict, Dict, str]:
    """(bbox, image_size, bbox_source) for one approved preview: mask
    region when the generation constrained the defect region (Req 7.2),
    else pixel diff against the source, else the full image (Req 7.1)."""
    generated_px = _decode_image_pixels(generated_bytes)
    if generated_px:
        image_size = {'width': len(generated_px[0]),
                      'height': len(generated_px)}
    else:
        image_size = {'width': 1024, 'height': 1024}

    mask_region = preview.get('mask_region')
    if mask_region:
        bbox = {k: int(mask_region[k])
                for k in ('left', 'top', 'width', 'height')}
        return bbox, image_size, 'inpainting_mask'

    source_px = None
    source_key = preview.get('source_image_key')
    if source_key and generated_px:
        try:
            source_obj = s3_client.get_object(Bucket=bucket, Key=source_key)
            source_px = _decode_image_pixels(source_obj['Body'].read())
        except Exception:
            source_px = None
    if source_px and generated_px:
        bbox = bbox_from_diff(source_px, generated_px,
                              threshold=DIFF_THRESHOLD)
        return bbox, image_size, 'image_diff'

    return ({'left': 0, 'top': 0, 'width': image_size['width'],
             'height': image_size['height']}, image_size, 'full_image')


def integrate_session(event: Dict) -> Dict:
    """POST /synthetic/sessions/{id}/integrate

    Copies approved images under the target dataset prefix, auto-annotates
    them, and appends manifest records with an ETag-conditional write. Any
    failure before/at the manifest write leaves the manifest untouched
    (Req 7.7) and returns 502 with the reason."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, previews = _load_session(session_id)
    if meta is None:
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    meta = _from_ddb(meta)
    previews = _from_ddb(previews)

    # Exactly the approved subset; zero approved rejects (Req 6.3, 6.5).
    try:
        approved = select_approved(
            [p for p in previews if p.get('status') == 'completed'])
    except ValidationError as exc:
        return create_response(400, {'error': str(exc)})

    target_prefix = (event and json.loads(event.get('body') or '{}').get(
        'target_dataset_prefix')) or meta.get('target_dataset_prefix')
    manifest_key = (json.loads(event.get('body') or '{}').get(
        'target_manifest_key')) or meta.get('target_manifest_key')
    if not target_prefix or not manifest_key:
        return create_response(400, {
            'error': 'target_dataset_prefix and target_manifest_key are '
                     'required (on the session or the request)'})
    if not target_prefix.endswith('/'):
        target_prefix += '/'

    defect_type = meta.get('defect_type') or 'defect'
    session_meta = {
        'session_id': session_id,
        'generation_model_id': meta.get('generation_model_id') or 'unknown',
    }

    usecase = get_usecase(meta['usecase_id'])
    s3_client, bucket = _data_s3_client(usecase)
    manifest_uri = f"s3://{bucket}/{manifest_key}"

    try:
        # 1. Copy approved images under the target dataset prefix
        #    (Req 7.3) and build their annotations + manifest records.
        records = []
        for preview in approved:
            staging_key = preview['staging_key']
            target_key = (f"{target_prefix}synthetic/{session_id}/"
                          f"{preview['preview_id']}.png")
            staged = s3_client.get_object(Bucket=bucket, Key=staging_key)
            generated_bytes = staged['Body'].read()
            s3_client.put_object(Bucket=bucket, Key=target_key,
                                 Body=generated_bytes,
                                 ContentType='image/png')
            bbox, image_size, bbox_source = _annotate_preview(
                s3_client, bucket, preview, generated_bytes)
            records.append(build_manifest_record(
                image_s3_uri=f"s3://{bucket}/{target_key}",
                defect_type=defect_type,
                bbox=bbox,
                image_size=image_size,
                session_meta=session_meta,
                resolved_prompt=preview.get('resolved_prompt', ''),
                timestamp=datetime.utcnow().isoformat(),
                bbox_source=bbox_source,
            ))

        # 2. Read manifest with its ETag, append in memory, and write
        #    back conditionally: the manifest write is the single commit
        #    point (Req 7.4, 7.5, 7.7). Retries re-read + re-append on
        #    concurrent modification.
        last_error = None
        committed = False
        for _ in range(MANIFEST_WRITE_RETRIES):
            etag = None
            existing = ''
            try:
                manifest_obj = s3_client.get_object(Bucket=bucket,
                                                    Key=manifest_key)
                existing = manifest_obj['Body'].read().decode('utf-8')
                etag = manifest_obj['ETag']
            except ClientError as exc:
                if exc.response['Error']['Code'] not in ('NoSuchKey',
                                                         '404'):
                    raise
            new_content = append_manifest_lines(existing, records)
            put_kwargs = {'Bucket': bucket, 'Key': manifest_key,
                          'Body': new_content.encode('utf-8')}
            if etag is not None:
                put_kwargs['IfMatch'] = etag
            else:
                put_kwargs['IfNoneMatch'] = '*'
            try:
                s3_client.put_object(**put_kwargs)
                committed = True
                break
            except ClientError as exc:
                code = exc.response['Error']['Code']
                if code in ('PreconditionFailed', '412',
                            'ConditionalRequestConflict'):
                    last_error = exc
                    continue
                raise
        if not committed:
            raise RuntimeError(
                'Manifest write failed after '
                f'{MANIFEST_WRITE_RETRIES} attempts due to concurrent '
                f'modification: {last_error}')
    except Exception as exc:
        # Any failure before/at the manifest write leaves the manifest in
        # its pre-integration state (Req 7.7): the conditional write is
        # all-or-nothing and nothing after it can fail into the manifest.
        reason = str(exc) or type(exc).__name__
        logger.exception('Integration failed for session %s', session_id)
        _record_last_failure(session_id, reason)
        return create_response(502, {'error': reason})

    # 3. Mark non-approved previews rejected so they are excluded from the
    #    dataset and the manifest (Req 6.6).
    for preview in previews:
        if preview.get('approval_state') != 'approved':
            _sessions_table().update_item(
                Key={'session_id': session_id, 'sk': preview['sk']},
                UpdateExpression='SET approval_state = :r',
                ExpressionAttributeValues={':r': 'rejected'},
            )

    integration_result = {
        'manifest_uri': manifest_uri,
        'appended_count': len(records),
        'at': _now_ms(),
    }
    _sessions_table().update_item(
        Key={'session_id': session_id, 'sk': 'META'},
        UpdateExpression=('SET #s = :status, integration_result = :r, '
                          'updated_at = :t'),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues=_to_ddb({
            ':status': 'integrated',
            ':r': integration_result,
            ':t': _now_ms(),
        }),
    )

    # Session-integrated audit event (Req 9.4).
    log_audit_event(
        user_id=user['user_id'],
        action='integrate_generation_session',
        resource_type='synthetic_session',
        resource_id=session_id,
        result='success',
        details={'usecase_id': meta['usecase_id'],
                 'session_id': session_id,
                 'manifest_uri': manifest_uri,
                 'appended_count': len(records)},
    )

    # Confirmation with the manifest URI and appended count (Req 7.6).
    return create_response(200, {
        'manifest_uri': manifest_uri,
        'appended_count': len(records),
        'session_id': session_id,
        'status': 'integrated',
    })


# ---------------------------------------------------------------------------
# Retrain endpoint (Req 8.2, 8.3, 8.4)
# ---------------------------------------------------------------------------

def retrain_session(event: Dict) -> Dict:
    """POST /synthetic/sessions/{id}/retrain

    Creates the training job through the existing Training_Subsystem
    contract with dataset_manifest_s3 pre-populated from the integration
    result and generation_session_id supplied (Req 8.2, 8.3). Creation
    failures are surfaced while integration_result stays intact for retry
    (Req 8.4)."""
    session_id = (event.get('pathParameters') or {}).get('id')
    meta, _ = _load_session(session_id)
    if meta is None:
        return create_response(404, {'error': 'Session not found'})
    user, denial = _authorize(event, meta['usecase_id'], 'synthetic_session',
                              session_id)
    if denial:
        return denial

    meta = _from_ddb(meta)
    body = json.loads(event.get('body') or '{}')
    integration_result = meta.get('integration_result') or {}
    manifest_uri = body.get('dataset_manifest_s3') or integration_result.get(
        'manifest_uri')
    if not manifest_uri:
        return create_response(400, {
            'error': 'The session has no integrated manifest; run '
                     'integration first or supply dataset_manifest_s3'})

    training_body = dict(body)
    training_body['usecase_id'] = meta['usecase_id']
    training_body['dataset_manifest_s3'] = manifest_uri
    training_body['generation_session_id'] = session_id

    training_event = {
        'httpMethod': 'POST',
        'resource': '/training',
        'path': '/api/v1/training',
        'body': json.dumps(training_body),
        'requestContext': event.get('requestContext', {}),
    }
    # Lazy import: the Training_Subsystem contract is reused as-is
    # (Req 8.2); training.py is only touched additively for
    # generation_session_id (Req 8.3).
    import training
    return training.create_training_job(training_event, None)


# ---------------------------------------------------------------------------
# Routing (dispatch on internal_action, then the route matrix)
# ---------------------------------------------------------------------------

# (method, resource) -> handler. The route matrix from the design; every
# handler is RBAC-gated via _authorize (Req 9.1, 9.2).
ROUTES = {
    ('GET', '/synthetic/models'): get_models,
    ('GET', '/synthetic/prompt-templates'): get_prompt_template,
    ('PUT', '/synthetic/prompt-templates'): put_prompt_template,
    ('POST', '/synthetic/sessions'): create_session,
    ('GET', '/synthetic/sessions'): list_sessions,
    ('GET', '/synthetic/sessions/{id}'): get_session,
    ('PATCH', '/synthetic/sessions/{id}'): patch_session,
    ('POST', '/synthetic/sessions/{id}/generate'): generate,
    ('POST', '/synthetic/sessions/{id}/previews/approval'):
        set_preview_approval,
    ('POST', '/synthetic/sessions/{id}/integrate'): integrate_session,
    ('POST', '/synthetic/sessions/{id}/retrain'): retrain_session,
}


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler: async worker dispatch, then API routing."""
    try:
        # Async self-invocation path (generation worker).
        if isinstance(event, dict) and event.get('internal_action') == \
                'generation_worker':
            return run_generation_worker(event)

        http_method = event.get('httpMethod')
        resource = event.get('resource', '')

        # CORS preflight.
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers':
                        'Content-Type,Authorization,X-Amz-Date,X-Api-Key,'
                        'X-Amz-Security-Token',
                    'Access-Control-Allow-Methods':
                        'GET,POST,PUT,PATCH,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400',
                },
                'body': '',
            }

        route = ROUTES.get((http_method, resource))
        if route is None:
            return create_response(404, {'error': 'Not found'})
        return route(event)

    except Exception as exc:
        logger.exception('Handler error: %s', exc)
        return create_response(500, {'error': 'Internal server error'})
