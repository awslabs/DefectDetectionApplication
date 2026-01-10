"""
Model Import Lambda functions
Implements BYOM (Bring Your Own Model) functionality
Allows importing pre-trained models that conform to DDA format
"""
import json
import os
import logging
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
    check_user_access, validate_required_fields
)

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


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access"""
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


def find_pt_model_file(export_artifacts_dir: str) -> str:
    """
    Find .pt model file in export_artifacts directory
    Returns: filename of the .pt file
    """
    pt_files = []
    
    for file in os.listdir(export_artifacts_dir):
        if file.endswith('.pt'):
            pt_files.append(file)
    
    if not pt_files:
        raise ModelValidationError(
            "No .pt model file found in export_artifacts/. "
            "Model must include a PyTorch model file (.pt extension)."
        )
    
    if len(pt_files) > 1:
        logger.warning(f"Multiple .pt files found: {pt_files}. Using first one: {pt_files[0]}")
    
    return pt_files[0]


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
        
        # Create S3 client with assumed role credentials
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
        
        # Find .pt model file
        pt_file = find_pt_model_file(export_artifacts_dir)
        
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
                'pt_file': pt_file,
                'framework': 'PYTORCH',
                'framework_version': '1.8'
            },
            'files_found': list(REQUIRED_FILES.keys()) + [f'export_artifacts/{pt_file}'],
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
            'x86_64-cpu',
            'x86_64-cuda',
            'arm64-cpu'
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
        elif http_method == 'GET' and '/models/format-spec' in path:
            return get_model_format_spec(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
