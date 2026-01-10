"""
Model Converter Lambda functions
Auto-generates DDA-compatible metadata from raw PyTorch models
Enables easy BYOM by accepting just a .pt file and user-provided dimensions
"""
import json
import os
import logging
from typing import Dict, Any, List, Tuple, Optional
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

# Supported model types
MODEL_TYPES = {
    'classification': {
        'description': 'Image classification (binary or multi-class)',
        'output_format': '[batch, num_classes]'
    },
    'object_detection': {
        'description': 'Object detection (YOLO, SSD, etc.)',
        'output_format': '[batch, detections, attributes]'
    },
    'segmentation': {
        'description': 'Semantic segmentation',
        'output_format': '[batch, num_classes, height, width]'
    },
    'anomaly_detection': {
        'description': 'Anomaly detection (normal vs anomaly)',
        'output_format': '[batch, 2]'
    }
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


def inspect_pytorch_model(model_path: str) -> Dict:
    """
    Inspect a PyTorch model file to extract metadata.
    Returns detected information about the model.
    """
    try:
        import torch
        
        # Load the model
        model_data = torch.load(model_path, map_location='cpu')
        
        info = {
            'type': 'unknown',
            'is_state_dict': False,
            'is_jit': False,
            'is_full_model': False,
            'layers': [],
            'input_channels': None,
            'num_classes': None,
            'architecture_hints': []
        }
        
        # Check if it's a JIT model
        if hasattr(model_data, 'graph'):
            info['is_jit'] = True
            info['type'] = 'jit_model'
            return info
        
        # Check if it's a state dict
        if isinstance(model_data, dict):
            # Could be a state dict or a checkpoint
            if 'model' in model_data:
                # Checkpoint format (common in YOLO, etc.)
                state_dict = model_data.get('model', {})
                if hasattr(state_dict, 'state_dict'):
                    state_dict = state_dict.state_dict()
                info['is_checkpoint'] = True
            elif 'state_dict' in model_data:
                state_dict = model_data['state_dict']
                info['is_checkpoint'] = True
            else:
                # Assume it's a raw state dict
                state_dict = model_data
                info['is_state_dict'] = True
            
            # Analyze layer names
            layer_names = list(state_dict.keys()) if isinstance(state_dict, dict) else []
            info['layers'] = layer_names[:20]  # First 20 layers
            info['total_layers'] = len(layer_names)
            
            # Try to detect architecture from layer names
            layer_str = ' '.join(layer_names).lower()
            
            # Detect common architectures
            if 'yolo' in layer_str or 'detect' in layer_str:
                info['architecture_hints'].append('YOLO-like object detection')
                info['suggested_type'] = 'object_detection'
            elif 'classifier' in layer_str or 'fc' in layer_str:
                info['architecture_hints'].append('Classification network')
                info['suggested_type'] = 'classification'
            elif 'decoder' in layer_str and 'encoder' in layer_str:
                info['architecture_hints'].append('Encoder-Decoder (segmentation)')
                info['suggested_type'] = 'segmentation'
            elif 'resnet' in layer_str:
                info['architecture_hints'].append('ResNet architecture')
                info['suggested_type'] = 'classification'
            elif 'efficientnet' in layer_str:
                info['architecture_hints'].append('EfficientNet architecture')
                info['suggested_type'] = 'classification'
            elif 'vit' in layer_str or 'transformer' in layer_str:
                info['architecture_hints'].append('Vision Transformer')
                info['suggested_type'] = 'classification'
            
            # Try to detect input channels from first conv layer
            for name, param in state_dict.items() if isinstance(state_dict, dict) else []:
                if 'conv' in name.lower() and 'weight' in name.lower():
                    if hasattr(param, 'shape') and len(param.shape) == 4:
                        info['input_channels'] = param.shape[1]
                        break
            
            # Try to detect num_classes from last layer
            for name in reversed(layer_names):
                if 'fc' in name.lower() or 'classifier' in name.lower() or 'head' in name.lower():
                    if 'weight' in name.lower():
                        param = state_dict.get(name)
                        if hasattr(param, 'shape') and len(param.shape) == 2:
                            info['num_classes'] = param.shape[0]
                            break
        
        else:
            # Full model object
            info['is_full_model'] = True
            info['type'] = 'full_model'
        
        return info
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return {
            'type': 'unknown',
            'error': str(e),
            'architecture_hints': ['Could not inspect model']
        }


def generate_dda_package(
    model_path: str,
    model_name: str,
    model_type: str,
    image_width: int,
    image_height: int,
    num_classes: Optional[int] = None,
    class_names: Optional[List[str]] = None,
    output_path: str = None
) -> str:
    """
    Generate a DDA-compatible package from a raw .pt file.
    Creates config.yaml, mochi.json, and manifest.json automatically.
    """
    temp_dir = None
    
    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="dda_convert_")
        export_dir = os.path.join(temp_dir, "export_artifacts")
        os.makedirs(export_dir, exist_ok=True)
        
        # Determine input shape (assume RGB)
        input_shape = [1, 3, image_height, image_width]
        
        # Determine output shape based on model type
        if model_type == 'classification':
            output_shape = [1, num_classes or 2]
        elif model_type == 'object_detection':
            # YOLO-style output
            output_shape = [1, (num_classes or 80) + 4, 8400]
        elif model_type == 'segmentation':
            output_shape = [1, num_classes or 2, image_height, image_width]
        elif model_type == 'anomaly_detection':
            output_shape = [1, 2]
            num_classes = 2
        else:
            output_shape = [1, num_classes or 2]
        
        # 1. Create config.yaml
        config = {
            'dataset': {
                'image_width': image_width,
                'image_height': image_height
            }
        }
        if num_classes:
            config['dataset']['num_classes'] = num_classes
        if class_names:
            config['dataset']['class_names'] = class_names
        
        with open(os.path.join(temp_dir, 'config.yaml'), 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        
        # 2. Create mochi.json
        mochi = {
            'stages': [
                {
                    'type': model_type,
                    'input_shape': input_shape,
                    'output_shape': output_shape
                }
            ],
            'model_info': {
                'name': model_name,
                'version': '1.0.0',
                'framework': 'pytorch',
                'auto_generated': True,
                'generated_at': datetime.utcnow().isoformat()
            }
        }
        if num_classes:
            mochi['stages'][0]['num_classes'] = num_classes
        
        with open(os.path.join(temp_dir, 'mochi.json'), 'w') as f:
            json.dump(mochi, f, indent=2)
        
        # 3. Create manifest.json
        pt_filename = f"{model_name}.pt"
        manifest = {
            'model_graph': {
                'stages': [
                    {
                        'type': model_type,
                        'input_shape': input_shape,
                        'output_shape': output_shape
                    }
                ]
            },
            'input_shape': input_shape,
            'compilable_models': [
                {
                    'filename': pt_filename,
                    'data_input_config': {
                        'input': input_shape
                    },
                    'framework': 'PYTORCH'
                }
            ],
            'preprocessing': {
                'resize': [image_width, image_height],
                'normalize': {
                    'mean': [0.485, 0.456, 0.406],
                    'std': [0.229, 0.224, 0.225]
                },
                'channel_order': 'RGB'
            }
        }
        
        with open(os.path.join(export_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2)
        
        # 4. Copy the model file
        shutil.copy(model_path, os.path.join(export_dir, pt_filename))
        
        # 5. Create tar.gz archive
        if not output_path:
            output_path = os.path.join(temp_dir, f"{model_name}.tar.gz")
        
        with tarfile.open(output_path, 'w:gz') as tar:
            for item in os.listdir(temp_dir):
                item_path = os.path.join(temp_dir, item)
                if item != os.path.basename(output_path):  # Don't include the output file itself
                    tar.add(item_path, arcname=item)
        
        logger.info(f"Generated DDA package: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating DDA package: {str(e)}")
        raise
    finally:
        # Don't cleanup if output_path is in temp_dir
        pass


def convert_model(event: Dict, context: Any) -> Dict:
    """
    Convert a raw PyTorch model to DDA-compatible format.
    POST /api/v1/models/convert
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt",  // Raw .pt file
        "model_name": "string",
        "model_type": "classification" | "object_detection" | "segmentation" | "anomaly_detection",
        "image_width": 224,
        "image_height": 224,
        "num_classes": 10,  // optional
        "class_names": ["class1", "class2"],  // optional
        "auto_import": true  // optional, auto-import after conversion
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['usecase_id', 'model_s3_uri', 'model_name', 'model_type', 'image_width', 'image_height']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        usecase_id = body['usecase_id']
        model_s3_uri = body['model_s3_uri'].strip()
        model_name = body['model_name'].strip()
        model_type = body['model_type']
        image_width = int(body['image_width'])
        image_height = int(body['image_height'])
        num_classes = body.get('num_classes')
        class_names = body.get('class_names')
        auto_import = body.get('auto_import', False)
        
        # Validate model type
        if model_type not in MODEL_TYPES:
            return create_response(400, {
                'error': f"Invalid model_type. Must be one of: {', '.join(MODEL_TYPES.keys())}"
            })
        
        # Validate dimensions
        if image_width <= 0 or image_height <= 0:
            return create_response(400, {'error': 'Image dimensions must be positive integers'})
        
        # Check user access (DataScientist role required)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate S3 URI format
        if not model_s3_uri.startswith('s3://'):
            return create_response(400, {
                'error': 'Invalid model_s3_uri. Must be an S3 URI (s3://bucket/path/model.pt)'
            })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"convert-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Create S3 client with assumed role
        s3_client = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        source_bucket = parsed.netloc
        source_key = parsed.path.lstrip('/')
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_convert_")
        
        try:
            # Download the model file
            local_model = os.path.join(temp_dir, 'model.pt')
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(source_bucket, source_key, local_model)
            
            # Inspect the model
            logger.info("Inspecting model...")
            model_info = inspect_pytorch_model(local_model)
            
            # Generate DDA package
            logger.info("Generating DDA-compatible package...")
            safe_model_name = model_name.replace(' ', '_').replace('-', '_').lower()
            output_tar = os.path.join(temp_dir, f"{safe_model_name}.tar.gz")
            
            generate_dda_package(
                model_path=local_model,
                model_name=safe_model_name,
                model_type=model_type,
                image_width=image_width,
                image_height=image_height,
                num_classes=num_classes,
                class_names=class_names,
                output_path=output_tar
            )
            
            # Upload converted package to S3
            output_key = f"converted-models/{safe_model_name}-{uuid.uuid4().hex[:8]}.tar.gz"
            output_s3_uri = f"s3://{usecase['s3_bucket']}/{output_key}"
            
            logger.info(f"Uploading converted model to {output_s3_uri}")
            s3_client.upload_file(output_tar, usecase['s3_bucket'], output_key)
            
            # Log audit event
            log_audit_event(
                user_id=user_id,
                action='convert_model',
                resource_type='model',
                resource_id=safe_model_name,
                result='success',
                details={
                    'source_uri': model_s3_uri,
                    'output_uri': output_s3_uri,
                    'model_type': model_type,
                    'dimensions': f"{image_width}x{image_height}"
                }
            )
            
            result = {
                'converted_model_s3_uri': output_s3_uri,
                'model_name': safe_model_name,
                'model_type': model_type,
                'input_shape': [1, 3, image_height, image_width],
                'model_info': model_info,
                'message': 'Model converted successfully'
            }
            
            # Auto-import if requested
            if auto_import:
                # Invoke model import Lambda
                lambda_client = boto3.client('lambda')
                import_function_name = os.environ.get('MODEL_IMPORT_FUNCTION_NAME')
                
                if import_function_name:
                    import_event = {
                        'httpMethod': 'POST',
                        'path': '/api/v1/models/import',
                        'body': json.dumps({
                            'usecase_id': usecase_id,
                            'model_name': model_name,
                            'model_version': '1.0.0',
                            'model_s3_uri': output_s3_uri,
                            'description': f'Auto-converted from {model_s3_uri}'
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
                    
                    # Invoke synchronously to get result
                    response = lambda_client.invoke(
                        FunctionName=import_function_name,
                        InvocationType='RequestResponse',
                        Payload=json.dumps(import_event)
                    )
                    
                    import_result = json.loads(response['Payload'].read())
                    if import_result.get('statusCode') == 201:
                        import_body = json.loads(import_result.get('body', '{}'))
                        result['import_result'] = import_body
                        result['training_id'] = import_body.get('training_id')
                        result['message'] = 'Model converted and imported successfully'
                    else:
                        result['import_error'] = 'Auto-import failed'
            
            return create_response(200, result)
            
        finally:
            # Cleanup temp directory
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error: {str(e)}")
        return create_response(500, {'error': f"Failed to convert model: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def inspect_model_endpoint(event: Dict, context: Any) -> Dict:
    """
    Inspect a PyTorch model file to detect its architecture.
    POST /api/v1/models/inspect
    
    Request body:
    {
        "usecase_id": "string",
        "model_s3_uri": "s3://bucket/path/model.pt"
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
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            f"inspect-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        )
        
        # Create S3 client
        s3_client = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Parse S3 URI
        parsed = urlparse(model_s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')
        
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix="model_inspect_")
        
        try:
            # Download the model file
            local_model = os.path.join(temp_dir, 'model.pt')
            logger.info(f"Downloading model from {model_s3_uri}")
            s3_client.download_file(bucket, key, local_model)
            
            # Inspect the model
            model_info = inspect_pytorch_model(local_model)
            
            return create_response(200, {
                'model_s3_uri': model_s3_uri,
                'inspection_result': model_info,
                'supported_model_types': MODEL_TYPES
            })
            
        finally:
            # Cleanup
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
    except Exception as e:
        logger.error(f"Error inspecting model: {str(e)}")
        return create_response(500, {'error': f"Failed to inspect model: {str(e)}"})


def get_supported_types(event: Dict, context: Any) -> Dict:
    """
    Get supported model types for conversion.
    GET /api/v1/models/types
    """
    return create_response(200, {
        'model_types': MODEL_TYPES,
        'common_dimensions': {
            'classification': [224, 256, 299, 384, 512],
            'object_detection': [320, 416, 512, 640, 1280],
            'segmentation': [256, 512, 768, 1024],
            'anomaly_detection': [224, 256, 512]
        },
        'supported_frameworks': ['PYTORCH'],
        'framework_versions': ['1.8', '1.9', '1.10', '1.11', '1.12', '1.13', '2.0']
    })


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
        if http_method == 'POST' and '/models/convert' in path:
            return convert_model(event, context)
        elif http_method == 'POST' and '/models/inspect' in path:
            return inspect_model_endpoint(event, context)
        elif http_method == 'GET' and '/models/types' in path:
            return get_supported_types(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
