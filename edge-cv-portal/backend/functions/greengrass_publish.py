"""
Greengrass component publishing Lambda functions
Implements component creation and publishing to AWS IoT Greengrass
Based on DDA_Greengrass_Component_Creator.ipynb Phase 3
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import re
import time

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
MODELS_TABLE = os.environ.get('MODELS_TABLE')
USECASES_TABLE = os.environ.get('USECASES_TABLE')

# Platform mapping for DDA LocalServer dependencies
PLATFORM_DEPENDENCIES = {
    'aarch64': 'aws.edgeml.dda.LocalServer.arm64',
    'amd64': 'aws.edgeml.dda.LocalServer.amd64'
}

# Target to platform mapping
TARGET_TO_PLATFORM = {
    'jetson-xavier': 'aarch64',
    'arm64-cpu': 'aarch64',
    'x86_64-cpu': 'amd64',
    'x86_64-cuda': 'amd64'
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


def get_training_job_details(training_id: str) -> Dict:
    """Get training job details from DynamoDB"""
    try:
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        response = table.get_item(Key={'training_id': training_id})
        
        if 'Item' not in response:
            raise ValueError(f"Training job {training_id} not found")
        
        return response['Item']
    except Exception as e:
        logger.error(f"Error getting training job details: {str(e)}")
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


def validate_component_name(name: str) -> bool:
    """Validate component name starts with 'model-'"""
    return name.startswith('model-')


def validate_component_version(version: str) -> bool:
    """Validate component version format x.0.0"""
    pattern = r'^\d+\.0+\.0+$'
    return bool(re.match(pattern, version))


def generate_component_recipe(
    component_name: str,
    component_version: str,
    friendly_name: str,
    platform: str,
    artifact_s3_uri: str,
    model_unarchived_path: str
) -> Dict:
    """
    Generate Greengrass component recipe
    Phase 3: Component Creation from DDA notebook
    """
    
    # Determine DDA LocalServer dependency based on platform
    local_server_component = PLATFORM_DEPENDENCIES.get(platform, 'aws.edgeml.dda.LocalServer')
    
    recipe = {
        'RecipeFormatVersion': '2020-01-25',
        'ComponentName': component_name,
        'ComponentVersion': component_version,
        'ComponentType': 'aws.greengrass.generic',
        'ComponentPublisher': 'Amazon Lookout for Vision',
        'ComponentConfiguration': {
            'DefaultConfiguration': {
                'Autostart': False,
                'PYTHONPATH': '/usr/bin/python3.9',
                'ModelName': friendly_name
            }
        },
        'ComponentDependencies': {
            local_server_component: {
                'VersionRequirement': '^1.0.0',
                'DependencyType': 'HARD'
            }
        },
        'Manifests': [
            {
                'Platform': {
                    'os': 'linux',
                    'architecture': platform
                },
                'Lifecycle': {
                    'Startup': {
                        'Script': f'python3 /aws_dda/model_convertor.py --unarchived_model_path {{artifacts:decompressedPath}}/{model_unarchived_path}/ --model_version {component_version} --model_name {component_name}',
                        'Timeout': 900,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    },
                    'Shutdown': {
                        'Script': f'python3 /aws_dda/convert_model_cleanup.py --model_name {component_name}',
                        'Timeout': 900,
                        'requiresPrivilege': True,
                        'runWith': {
                            'posixUser': 'root'
                        }
                    }
                },
                'Artifacts': [
                    {
                        'Uri': artifact_s3_uri,
                        'Digest': '',  # Greengrass will calculate
                        'Algorithm': 'SHA-256',
                        'Unarchive': 'ZIP',
                        'Permission': {
                            'Read': 'ALL',
                            'Execute': 'ALL'
                        }
                    }
                ]
            }
        ],
        'Lifecycle': {}
    }
    
    return recipe


def publish_component(event: Dict, context: Any) -> Dict:
    """
    Publish Greengrass component
    POST /api/v1/training/{training_id}/publish
    
    Request body:
    {
        "component_name": "model-defect-classifier",
        "component_version": "1.0.0",
        "friendly_name": "Defect Classifier",  // Optional
        "targets": ["jetson-xavier", "x86_64-cpu"]  // Optional, defaults to all packaged
    }
    """
    try:
        # Extract user info
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        # Get path parameters
        training_id = event.get('pathParameters', {}).get('id')
        if not training_id:
            return create_response(400, {'error': 'training_id is required'})
        
        # Parse request body
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['component_name', 'component_version']
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        component_name = body['component_name']
        component_version = body['component_version']
        friendly_name = body.get('friendly_name', component_name)
        requested_targets = body.get('targets')
        
        # Validate component name
        if not validate_component_name(component_name):
            return create_response(400, {
                'error': 'Component name must start with "model-" (e.g., model-defect-classifier)'
            })
        
        # Validate component version
        if not validate_component_version(component_version):
            return create_response(400, {
                'error': 'Component version must be in format x.0.0 (e.g., 1.0.0, 2.0.0)'
            })
        
        # Get training job details
        training_job = get_training_job_details(training_id)
        usecase_id = training_job['usecase_id']
        
        # Check user access (DataScientist role required)
        # Allow 'system' user for auto-triggered publishing
        if user_id != 'system' and not check_user_access(user_id, usecase_id, 'DataScientist'):
            return create_response(403, {'error': 'Insufficient permissions'})
        
        # Check packaged components exist
        packaged_components = training_job.get('packaged_components', [])
        if not packaged_components:
            return create_response(400, {
                'error': 'No packaged components found. Run packaging first.'
            })
        
        # Filter to successfully packaged components
        packaged = [c for c in packaged_components if c.get('status') == 'packaged']
        if not packaged:
            return create_response(400, {'error': 'No successfully packaged components found'})
        
        # Filter by requested targets if specified
        if requested_targets:
            packaged = [c for c in packaged if c['target'] in requested_targets]
            if not packaged:
                return create_response(400, {
                    'error': f"No packaged components for requested targets: {requested_targets}"
                })
        
        # Get use case details
        usecase = get_usecase_details(usecase_id)
        
        # Assume cross-account role (session name max 64 chars)
        session_name = f"gg-pub-{user_id[:20]}-{int(datetime.utcnow().timestamp())}"[:64]
        credentials = assume_usecase_role(
            usecase['cross_account_role_arn'],
            usecase['external_id'],
            session_name
        )
        
        # Create Greengrass client with assumed role
        greengrass = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # Publish component for each target
        # Each target gets its own component with target suffix in the name
        published_components = []
        
        for component in packaged:
            target = component['target']
            artifact_s3_uri = component.get('component_package_s3')
            
            if not artifact_s3_uri:
                logger.warning(f"No artifact S3 URI for target {target}, skipping")
                continue
            
            # Determine platform
            platform = TARGET_TO_PLATFORM.get(target, 'amd64')
            
            # Create unique component name per target
            # e.g., model-defect-classifier-jetson-xavier, model-defect-classifier-x86-64-cpu
            target_suffix = target.replace('_', '-')
            target_component_name = f"{component_name}-{target_suffix}"
            
            # Extract model unarchived path from S3 URI
            # Format: s3://bucket/model_artifacts/model-uuid/uuid_greengrass_model_component.zip
            model_unarchived_path = artifact_s3_uri.split('/')[-1].replace('.zip', '')
            
            logger.info(f"Publishing component {target_component_name} for target {target} (platform: {platform})")
            
            try:
                # Generate component recipe with target-specific name
                recipe = generate_component_recipe(
                    component_name=target_component_name,
                    component_version=component_version,
                    friendly_name=f"{friendly_name} ({target})",
                    platform=platform,
                    artifact_s3_uri=artifact_s3_uri,
                    model_unarchived_path=model_unarchived_path
                )
                
                logger.info(f"Creating Greengrass component: {target_component_name} v{component_version}")
                
                # Create component version with portal tag for filtering
                # Tag: dda-portal:managed=true allows filtering via Resource Groups Tagging API
                response = greengrass.create_component_version(
                    inlineRecipe=json.dumps(recipe),
                    tags={
                        'dda-portal:managed': 'true',
                        'dda-portal:usecase-id': usecase_id,
                        'dda-portal:training-id': training_id,
                        'dda-portal:model-name': friendly_name,
                        'dda-portal:created-by': user_id
                    }
                )
                
                component_arn = response['arn']
                logger.info(f"Component created: {component_arn}")
                
                # Monitor component status until DEPLOYABLE
                max_attempts = 30
                attempt = 0
                component_status = 'REQUESTED'
                
                while attempt < max_attempts and component_status in ['REQUESTED', 'IN_PROGRESS']:
                    time.sleep(2)  # Wait 2 seconds between checks
                    
                    status_response = greengrass.describe_component(arn=component_arn)
                    component_status = status_response['status']['componentState']
                    
                    logger.info(f"Component status: {component_status}")
                    attempt += 1
                
                if component_status == 'DEPLOYABLE':
                    published_components.append({
                        'target': target,
                        'platform': platform,
                        'component_name': target_component_name,
                        'component_version': component_version,
                        'component_arn': component_arn,
                        'status': 'published'
                    })
                    logger.info(f"Component {target_component_name} published successfully for {target}")
                else:
                    error_msg = status_response['status'].get('message', 'Unknown error')
                    published_components.append({
                        'target': target,
                        'platform': platform,
                        'component_name': target_component_name,
                        'component_version': component_version,
                        'status': 'failed',
                        'error': f"Component status: {component_status}. {error_msg}"
                    })
                    logger.error(f"Component {target_component_name} failed to become DEPLOYABLE: {component_status}")
                
            except ClientError as e:
                error_msg = str(e)
                logger.error(f"Error publishing component {target_component_name} for {target}: {error_msg}")
                published_components.append({
                    'target': target,
                    'platform': platform,
                    'component_name': target_component_name,
                    'component_version': component_version,
                    'status': 'failed',
                    'error': error_msg
                })
        
        # Store published components in Models table
        if published_components:
            models_table = dynamodb.Table(MODELS_TABLE)
            timestamp = int(datetime.utcnow().timestamp() * 1000)
            
            # Create model record
            model_id = f"{training_id}-{component_version}"
            
            # Build component ARNs map
            component_arns = {}
            for comp in published_components:
                if comp['status'] == 'published':
                    component_arns[comp['target']] = comp['component_arn']
            
            model_item = {
                'model_id': model_id,
                'usecase_id': usecase_id,
                'name': component_name,
                'version': component_version,
                'stage': 'candidate',
                'training_job_id': training_id,
                'dataset_manifest_id': training_job.get('dataset_manifest_s3', ''),
                'metrics': training_job.get('metrics', {}),
                'component_arns': component_arns,
                'deployed_devices': [],
                'created_by': user_id,
                'created_at': timestamp
            }
            
            models_table.put_item(Item=model_item)
            logger.info(f"Model record created: {model_id}")
        
        # Update training job with published components
        table = dynamodb.Table(TRAINING_JOBS_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        table.update_item(
            Key={'training_id': training_id},
            UpdateExpression='SET published_components = :components, updated_at = :updated',
            ExpressionAttributeValues={
                ':components': published_components,
                ':updated': timestamp
            }
        )
        
        # Log audit event
        log_audit_event(
            user_id=user_id,
            action='publish_greengrass_component',
            resource_type='training_job',
            resource_id=training_id,
            result='success',
            details={
                'component_name': component_name,
                'component_version': component_version,
                'targets': [c['target'] for c in published_components],
                'published_count': len([c for c in published_components if c['status'] == 'published'])
            }
        )
        
        success_count = len([c for c in published_components if c['status'] == 'published'])
        logger.info(f"Published {success_count}/{len(published_components)} components for training {training_id}")
        
        return create_response(200, {
            'training_id': training_id,
            'component_name': component_name,
            'component_version': component_version,
            'published_components': published_components,
            'message': f'Published {success_count} component(s) successfully'
        })
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return create_response(400, {'error': str(e)})
    except ClientError as e:
        logger.error(f"AWS error publishing component: {str(e)}")
        return create_response(500, {'error': f"Failed to publish component: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})


def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler"""
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
        if http_method == 'POST' and '/publish' in path:
            return publish_component(event, context)
        else:
            return create_response(404, {'error': 'Not found'})
            
    except Exception as e:
        logger.error(f"Handler error: {str(e)}")
        return create_response(500, {'error': 'Internal server error'})
