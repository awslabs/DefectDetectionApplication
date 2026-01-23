"""
Model Registry Lambda functions
Manages models from training jobs and BYOM imports
"""
import json
import os
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError

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
MODELS_TABLE = os.environ.get('MODELS_TABLE')
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE')


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types from DynamoDB"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj) if obj % 1 else int(obj)
        return super().default(obj)


def decimal_to_native(obj):
    """Convert Decimal objects to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_native(i) for i in obj]
    return obj


def list_models(event: Dict, context: Any) -> Dict:
    """
    List models for a use case
    GET /api/v1/models?usecase_id=xxx&stage=xxx
    
    Combines models from:
    1. Models table (explicit model registry entries)
    2. Training jobs table (completed training jobs and imported models)
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get query parameters
        params = event.get('queryStringParameters') or {}
        usecase_id = params.get('usecase_id')
        stage_filter = params.get('stage')
        source_filter = params.get('source')  # 'trained', 'imported', 'marketplace'
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id is required'})
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        models = []
        
        # Query training jobs table for completed jobs
        training_table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        # Pre-fetch all deployments for this usecase to get deployed devices per component
        deployment_device_map = {}  # component_arn -> set of device_ids
        try:
            deployments_table = dynamodb.Table(DEPLOYMENTS_TABLE)
            deploy_response = deployments_table.query(
                IndexName='usecase-deployments-index',
                KeyConditionExpression='usecase_id = :uid',
                ExpressionAttributeValues={':uid': usecase_id}
            )
            
            for deployment in deploy_response.get('Items', []):
                # Only count active/completed deployments
                if deployment.get('deployment_status') not in ['COMPLETED', 'ACTIVE', 'completed', 'active']:
                    continue
                    
                components = deployment.get('components', [])
                target_devices = deployment.get('target_devices', [])
                
                for comp in components:
                    comp_name = comp.get('component_name', '')
                    if comp_name not in deployment_device_map:
                        deployment_device_map[comp_name] = set()
                    deployment_device_map[comp_name].update(target_devices)
        except Exception as e:
            logger.warning(f"Error fetching deployments: {str(e)}")
        
        try:
            response = training_table.query(
                IndexName='usecase-training-index',
                KeyConditionExpression='usecase_id = :uid',
                FilterExpression='#status = :completed',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':uid': usecase_id,
                    ':completed': 'Completed'
                }
            )
            
            for item in response.get('Items', []):
                item = decimal_to_native(item)
                
                # Determine source
                source = item.get('source', 'trained')
                if source_filter and source != source_filter:
                    continue
                
                # Determine stage (default to candidate for new models)
                stage = item.get('stage', 'candidate')
                if stage_filter and stage != stage_filter:
                    continue
                
                # Extract metrics from training job
                metrics = item.get('metrics', {})
                if not metrics and item.get('validation_result'):
                    # For imported models, metrics might be in validation_result
                    metrics = item.get('validation_result', {}).get('metadata', {})
                
                # Get deployment info by checking component_arns against deployments
                deployed_devices = set()
                component_arns = item.get('component_arns', {})
                
                # Check each published component for deployments
                for platform, comp_arn in component_arns.items():
                    # Extract component name from ARN
                    # ARN format: arn:aws:greengrass:region:account:components:name:versions:version
                    if comp_arn:
                        parts = comp_arn.split(':')
                        if len(parts) >= 7:
                            comp_name = parts[6]  # component name
                            if comp_name in deployment_device_map:
                                deployed_devices.update(deployment_device_map[comp_name])
                
                model = {
                    'model_id': item['training_id'],
                    'usecase_id': usecase_id,
                    'name': item.get('model_name', 'Unnamed Model'),
                    'version': item.get('model_version', '1.0.0'),
                    'stage': stage,
                    'source': source,
                    'training_job_id': item['training_id'],
                    'model_type': item.get('model_type', 'classification'),
                    'metrics': metrics,
                    'artifact_s3': item.get('artifact_s3'),
                    'component_arns': item.get('component_arns', {}),
                    'deployed_devices': list(deployed_devices),
                    'created_by': item.get('created_by', 'unknown'),
                    'created_at': item.get('created_at', 0),
                    'updated_at': item.get('updated_at', item.get('created_at', 0)),
                    'description': item.get('description', ''),
                    'compilation_status': item.get('compilation_status'),
                    'packaging_status': item.get('packaging_status'),
                }
                
                models.append(model)
                
        except ClientError as e:
            logger.error(f"Error querying training jobs: {str(e)}")
        
        # Sort by created_at descending
        models.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        return create_response(200, {
            'models': models,
            'count': len(models),
            'usecase_id': usecase_id
        })
        
    except Exception as e:
        logger.error(f"Error listing models: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def get_model(event: Dict, context: Any) -> Dict:
    """
    Get model details
    GET /api/v1/models/{model_id}
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get model ID from path
        path_params = event.get('pathParameters') or {}
        model_id = path_params.get('id')
        
        if not model_id:
            return create_response(400, {'error': 'model_id is required'})
        
        # Get model from training jobs table
        training_table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        response = training_table.get_item(Key={'training_id': model_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Model not found'})
        
        item = decimal_to_native(response['Item'])
        usecase_id = item.get('usecase_id')
        
        # Check user access
        if not check_user_access(user_id, usecase_id):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Get deployment info
        deployed_devices = []
        try:
            deployments_table = dynamodb.Table(DEPLOYMENTS_TABLE)
            deploy_response = deployments_table.query(
                IndexName='usecase-deployments-index',
                KeyConditionExpression='usecase_id = :uid',
                ExpressionAttributeValues={':uid': usecase_id}
            )
            
            for deployment in deploy_response.get('Items', []):
                components = deployment.get('components', [])
                for comp in components:
                    # Check if this model's component is in the deployment
                    component_arns = item.get('component_arns', {})
                    for platform, arn in component_arns.items():
                        if comp.get('component_arn') == arn:
                            target_devices = deployment.get('target_devices', [])
                            deployed_devices.extend(target_devices)
        except Exception as e:
            logger.warning(f"Error getting deployment info: {str(e)}")
        
        # Remove duplicates
        deployed_devices = list(set(deployed_devices))
        
        # Build model response
        source = item.get('source', 'trained')
        stage = item.get('stage', 'candidate')
        
        metrics = item.get('metrics', {})
        if not metrics and item.get('validation_result'):
            metrics = item.get('validation_result', {}).get('metadata', {})
        
        model = {
            'model_id': model_id,
            'usecase_id': usecase_id,
            'name': item.get('model_name', 'Unnamed Model'),
            'version': item.get('model_version', '1.0.0'),
            'stage': stage,
            'source': source,
            'training_job_id': model_id,
            'training_job_name': item.get('training_job_name'),
            'model_type': item.get('model_type', 'classification'),
            'description': item.get('description', ''),
            'metrics': metrics,
            'artifact_s3': item.get('artifact_s3'),
            'component_arns': item.get('component_arns', {}),
            'deployed_devices': deployed_devices,
            'created_by': item.get('created_by', 'unknown'),
            'created_at': item.get('created_at', 0),
            'updated_at': item.get('updated_at', item.get('created_at', 0)),
            'completed_at': item.get('completed_at'),
            'promoted_at': item.get('promoted_at'),
            'promoted_by': item.get('promoted_by'),
            'compilation_status': item.get('compilation_status'),
            'compilation_jobs': item.get('compilation_jobs', []),
            'packaging_status': item.get('packaging_status'),
            'packaged_components': item.get('packaged_components', []),
            'validation_result': item.get('validation_result'),
            'hyperparameters': item.get('hyperparameters', {}),
            'instance_type': item.get('instance_type'),
            'dataset_manifest_s3': item.get('dataset_manifest_s3'),
        }
        
        return create_response(200, {'model': model})
        
    except Exception as e:
        logger.error(f"Error getting model: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def update_model_stage(event: Dict, context: Any) -> Dict:
    """
    Update model stage (promote/demote)
    PUT /api/v1/models/{model_id}/stage
    
    Request body:
    {
        "stage": "staging" | "production" | "candidate"
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get model ID from path
        path_params = event.get('pathParameters') or {}
        model_id = path_params.get('id')
        
        if not model_id:
            return create_response(400, {'error': 'model_id is required'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        new_stage = body.get('stage')
        
        valid_stages = ['candidate', 'staging', 'production']
        if not new_stage or new_stage not in valid_stages:
            return create_response(400, {
                'error': f'Invalid stage. Must be one of: {", ".join(valid_stages)}'
            })
        
        # Get model from training jobs table
        training_table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        response = training_table.get_item(Key={'training_id': model_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Model not found'})
        
        item = response['Item']
        usecase_id = item.get('usecase_id')
        current_stage = item.get('stage', 'candidate')
        
        # Check user access (require DataScientist or higher for stage changes)
        if not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Update stage
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        update_expr = 'SET stage = :stage, updated_at = :updated'
        expr_values = {
            ':stage': new_stage,
            ':updated': timestamp
        }
        
        # Track promotion info
        if new_stage in ['staging', 'production'] and current_stage != new_stage:
            update_expr += ', promoted_at = :promoted, promoted_by = :promoter'
            expr_values[':promoted'] = timestamp
            expr_values[':promoter'] = user['email']
        
        training_table.update_item(
            Key={'training_id': model_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values
        )
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='update_model_stage',
            resource_type='model',
            resource_id=model_id,
            result='success',
            details={
                'previous_stage': current_stage,
                'new_stage': new_stage,
                'usecase_id': usecase_id
            }
        )
        
        logger.info(f"Model {model_id} stage updated from {current_stage} to {new_stage}")
        
        return create_response(200, {
            'model_id': model_id,
            'previous_stage': current_stage,
            'stage': new_stage,
            'message': f'Model stage updated to {new_stage}'
        })
        
    except Exception as e:
        logger.error(f"Error updating model stage: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def delete_model(event: Dict, context: Any) -> Dict:
    """
    Delete a model (only if not deployed)
    DELETE /api/v1/models/{model_id}
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get model ID from path
        path_params = event.get('pathParameters') or {}
        model_id = path_params.get('id')
        
        if not model_id:
            return create_response(400, {'error': 'model_id is required'})
        
        # Get model from training jobs table
        training_table = dynamodb.Table(TRAINING_JOBS_TABLE)
        
        response = training_table.get_item(Key={'training_id': model_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Model not found'})
        
        item = response['Item']
        usecase_id = item.get('usecase_id')
        
        # Check user access (require Admin for deletion)
        if not check_user_access(user_id, usecase_id, 'Admin'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Check if model is deployed
        deployed_devices = item.get('deployed_devices', [])
        if deployed_devices:
            return create_response(400, {
                'error': f'Cannot delete model that is deployed to {len(deployed_devices)} device(s). Undeploy first.'
            })
        
        # Delete the model
        training_table.delete_item(Key={'training_id': model_id})
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='delete_model',
            resource_type='model',
            resource_id=model_id,
            result='success',
            details={
                'model_name': item.get('model_name'),
                'usecase_id': usecase_id
            }
        )
        
        logger.info(f"Model {model_id} deleted")
        
        return create_response(200, {
            'model_id': model_id,
            'message': 'Model deleted successfully'
        })
        
    except Exception as e:
        logger.error(f"Error deleting model: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


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
        path_params = event.get('pathParameters') or {}
        model_id = path_params.get('id')
        
        if http_method == 'GET':
            if model_id:
                return get_model(event, context)
            else:
                return list_models(event, context)
        elif http_method == 'PUT' and model_id and '/stage' in path:
            return update_model_stage(event, context)
        elif http_method == 'DELETE' and model_id:
            return delete_model(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
