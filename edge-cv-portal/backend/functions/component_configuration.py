"""
Component Configuration Handler for Edge CV Portal

Manages component parameter configuration and creates deployments with custom settings.
Supports configurable components like InferenceUploader with parameters like upload interval, batch size, etc.
"""

import json
import logging
import os
from datetime import datetime
import uuid
import boto3
from botocore.exceptions import ClientError

from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, validate_required_fields, rbac_manager, Permission
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
gg_client = boto3.client('greengrassv2')

DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE')
COMPONENTS_TABLE = os.environ.get('COMPONENTS_TABLE')

# Component configuration schemas
COMPONENT_SCHEMAS = {
    'aws.edgeml.dda.InferenceUploader': {
        'displayName': 'Inference Uploader',
        'description': 'Uploads inference results to S3',
        'parameters': {
            'uploadIntervalSeconds': {
                'name': 'Upload Interval',
                'type': 'number',
                'default': 10,
                'description': 'How often to upload files (seconds)',
                'required': True,
                'validation': {'min': 1, 'max': 3600},
                'envVar': 'UPLOAD_INTERVAL_SECONDS'
            },
            'batchSize': {
                'name': 'Batch Size',
                'type': 'number',
                'default': 100,
                'description': 'Maximum files per upload batch',
                'required': True,
                'validation': {'min': 1, 'max': 1000},
                'envVar': 'BATCH_SIZE'
            },
            'localRetentionDays': {
                'name': 'Retention Days',
                'type': 'number',
                'default': 7,
                'description': 'Keep local files for N days',
                'required': True,
                'validation': {'min': 0, 'max': 365},
                'envVar': 'LOCAL_RETENTION_DAYS'
            },
            'uploadImages': {
                'name': 'Upload Images',
                'type': 'boolean',
                'default': True,
                'description': 'Upload image files (.jpg, .png)',
                'required': False,
                'envVar': 'UPLOAD_IMAGES'
            },
            'uploadMetadata': {
                'name': 'Upload Metadata',
                'type': 'boolean',
                'default': True,
                'description': 'Upload metadata files (.json, .jsonl)',
                'required': False,
                'envVar': 'UPLOAD_METADATA'
            },
            's3Bucket': {
                'name': 'S3 Bucket',
                'type': 'string',
                'default': '',
                'description': 'S3 bucket for uploads',
                'required': True,
                'envVar': 'S3_BUCKET'
            },
            'awsRegion': {
                'name': 'AWS Region',
                'type': 'select',
                'default': 'us-east-1',
                'description': 'AWS region',
                'required': True,
                'options': [
                    {'label': 'us-east-1', 'value': 'us-east-1'},
                    {'label': 'us-west-2', 'value': 'us-west-2'},
                    {'label': 'eu-west-1', 'value': 'eu-west-1'},
                    {'label': 'ap-southeast-1', 'value': 'ap-southeast-1'},
                    {'label': 'ap-northeast-1', 'value': 'ap-northeast-1'},
                ],
                'envVar': 'AWS_REGION'
            }
        }
    }
}


def get_component_schema(component_name: str) -> dict:
    """Get configuration schema for a component"""
    return COMPONENT_SCHEMAS.get(component_name, {})


def validate_parameter(param_name: str, value: any, schema: dict) -> tuple[bool, str]:
    """Validate a single parameter against its schema"""
    param_schema = schema.get('parameters', {}).get(param_name)
    
    if not param_schema:
        return False, f"Unknown parameter: {param_name}"
    
    # Check required
    if param_schema.get('required') and value is None:
        return False, f"{param_schema['name']} is required"
    
    # Type validation
    param_type = param_schema.get('type')
    if param_type == 'number':
        if not isinstance(value, (int, float)):
            return False, f"{param_schema['name']} must be a number"
        
        # Range validation
        validation = param_schema.get('validation', {})
        if 'min' in validation and value < validation['min']:
            return False, f"{param_schema['name']} must be >= {validation['min']}"
        if 'max' in validation and value > validation['max']:
            return False, f"{param_schema['name']} must be <= {validation['max']}"
    
    elif param_type == 'boolean':
        if not isinstance(value, bool):
            return False, f"{param_schema['name']} must be a boolean"
    
    elif param_type == 'string':
        if not isinstance(value, str):
            return False, f"{param_schema['name']} must be a string"
    
    elif param_type == 'select':
        options = param_schema.get('options', [])
        valid_values = [opt['value'] for opt in options]
        if value not in valid_values:
            return False, f"{param_schema['name']} must be one of: {', '.join(str(v) for v in valid_values)}"
    
    return True, "Valid"


def validate_configuration(component_name: str, configuration: dict) -> tuple[bool, str]:
    """Validate entire configuration against component schema"""
    schema = get_component_schema(component_name)
    
    if not schema:
        return False, f"No configuration schema found for {component_name}"
    
    # Validate each parameter
    for param_name, param_value in configuration.items():
        is_valid, error_msg = validate_parameter(param_name, param_value, schema)
        if not is_valid:
            return False, error_msg
    
    # Check required parameters
    for param_name, param_schema in schema.get('parameters', {}).items():
        if param_schema.get('required') and param_name not in configuration:
            return False, f"{param_schema['name']} is required"
    
    return True, "Configuration is valid"


def build_environment_variables(component_name: str, configuration: dict) -> dict:
    """Build environment variables from configuration"""
    schema = get_component_schema(component_name)
    env_vars = {}
    
    for param_name, param_value in configuration.items():
        param_schema = schema.get('parameters', {}).get(param_name)
        if param_schema:
            env_var_name = param_schema.get('envVar')
            if env_var_name:
                # Convert boolean to string
                if isinstance(param_value, bool):
                    env_vars[env_var_name] = 'true' if param_value else 'false'
                else:
                    env_vars[env_var_name] = str(param_value)
    
    return env_vars


def handler(event, context):
    """
    Handle component configuration requests
    
    GET    /api/v1/components/{arn}/schema       - Get configuration schema
    POST   /api/v1/components/{arn}/configure    - Configure and deploy component
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        
        logger.info(f"Component Configuration request: {http_method} {path}")
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        
        if http_method == 'GET' and '/schema' in path:
            return get_configuration_schema(event, user)
        elif http_method == 'POST' and '/configure' in path:
            return configure_component(event, user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in component configuration handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def get_configuration_schema(event, user):
    """Get configuration schema for a component"""
    try:
        query_params = event.get('queryStringParameters') or {}
        component_name = query_params.get('component_name')
        
        if not component_name:
            return create_response(400, {'error': 'component_name parameter required'})
        
        schema = get_component_schema(component_name)
        
        if not schema:
            return create_response(404, {'error': f'No configuration schema for {component_name}'})
        
        logger.info(f"Retrieved schema for {component_name}")
        
        return create_response(200, {
            'component_name': component_name,
            'displayName': schema.get('displayName'),
            'description': schema.get('description'),
            'parameters': schema.get('parameters', {})
        })
        
    except Exception as e:
        logger.error(f"Error getting configuration schema: {str(e)}")
        return create_response(500, {'error': 'Failed to get configuration schema'})


def configure_component(event, user):
    """Configure component and create deployment"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['component_name', 'usecase_id', 'configuration', 'target_devices']
        validation_error = validate_required_fields(body, required_fields)
        if validation_error:
            return create_response(400, {'error': validation_error})
        
        component_name = body['component_name']
        usecase_id = body['usecase_id']
        configuration = body['configuration']
        target_devices = body['target_devices']
        deployment_name = body.get('deployment_name', f"{component_name}-config-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}")
        
        # Check user access
        if not check_user_access(user['user_id'], usecase_id, 'UseCaseAdmin', user):
            log_audit_event(
                user['user_id'], 'configure_component', 'component', component_name,
                'failure', {'reason': 'insufficient_permissions'}
            )
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Validate configuration
        is_valid, error_msg = validate_configuration(component_name, configuration)
        if not is_valid:
            logger.warning(f"Configuration validation failed: {error_msg}")
            return create_response(400, {'error': f'Configuration validation failed: {error_msg}'})
        
        # Build environment variables
        env_vars = build_environment_variables(component_name, configuration)
        
        logger.info(f"Configuration validated for {component_name}")
        logger.info(f"Environment variables: {env_vars}")
        
        # Create deployment with configuration
        deployment_id = str(uuid.uuid4())
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        deployment_item = {
            'deployment_id': deployment_id,
            'usecase_id': usecase_id,
            'component_name': component_name,
            'deployment_name': deployment_name,
            'status': 'PENDING',
            'target_devices': target_devices,
            'configuration': configuration,
            'environment_variables': env_vars,
            'created_by': user['user_id'],
            'created_at': timestamp,
            'updated_at': timestamp,
            'tags': {
                'component-configuration': 'true',
                'configured-by': user['user_id']
            }
        }
        
        # Save deployment
        table = dynamodb.Table(DEPLOYMENTS_TABLE)
        table.put_item(Item=deployment_item)
        
        logger.info(f"Deployment created: {deployment_id}")
        
        # Log audit event
        log_audit_event(
            user['user_id'], 'configure_component', 'component', component_name,
            'success', {
                'deployment_id': deployment_id,
                'target_devices': len(target_devices),
                'configuration': configuration
            }
        )
        
        return create_response(201, {
            'status': 'success',
            'deployment_id': deployment_id,
            'component_name': component_name,
            'configuration': configuration,
            'environment_variables': env_vars,
            'target_devices': target_devices,
            'message': f'Deployment created with configuration for {component_name}'
        })
        
    except Exception as e:
        logger.error(f"Error configuring component: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Failed to configure component'})
