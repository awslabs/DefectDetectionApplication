"""
Deployments handler for Edge CV Portal
Manages Greengrass deployments to edge devices
"""
import json
import logging
import os
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, assume_cross_account_role, get_usecase,
    create_boto3_client
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE', 'dda-portal-deployments')

# Minimum Greengrass Nucleus version required for DDA components
# Using 2.13.0 instead of 2.14.0 due to LogManager version constraint (<2.14.0)
# This is auto-included in deployments to prevent version mismatch errors
MIN_NUCLEUS_VERSION = '2.13.0'

# CloudWatch log manager for device logging
# Version 2.3.9 supports Nucleus >=2.1.0 <2.14.0
LOG_MANAGER_VERSION = '2.3.9'

# Components that require Nucleus to be explicitly included in deployment
# Model components (model-*) also need Nucleus since they depend on DDA components
DDA_COMPONENTS_REQUIRING_NUCLEUS = [
    'aws.edgeml.dda.LocalServer',
    'aws.edgeml.dda.InferenceApp',
    'model-',  # Model components created by the portal
]


def handler(event, context):
    """
    Handle deployment management requests
    
    GET /api/v1/deployments              - List deployments
    GET /api/v1/deployments/{id}         - Get deployment details
    POST /api/v1/deployments             - Create deployment
    DELETE /api/v1/deployments/{id}      - Cancel deployment
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Deployments request: {http_method} {path}")
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return create_response(200, {})
        
        user = get_user_from_event(event)
        
        if http_method == 'GET' and not path_parameters.get('id'):
            return list_deployments(user, query_parameters)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_deployment(path_parameters['id'], user, query_parameters)
        elif http_method == 'POST':
            body = json.loads(event.get('body') or '{}')
            return create_deployment(body, user)
        elif http_method == 'DELETE' and path_parameters.get('id'):
            return cancel_deployment(path_parameters['id'], user, query_parameters)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in deployments handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_deployments(user, query_params):
    """List Greengrass deployments for a use case"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        deployments = []
        next_token = None
        
        # List all deployments
        while True:
            params = {'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_deployments(**params)
            
            for dep in response.get('deployments', []):
                # Convert datetime to ISO string
                creation_ts = dep.get('creationTimestamp')
                if creation_ts and hasattr(creation_ts, 'isoformat'):
                    creation_ts = creation_ts.isoformat()
                
                deployments.append({
                    'deployment_id': dep.get('deploymentId'),
                    'deployment_name': dep.get('deploymentName', ''),
                    'target_arn': dep.get('targetArn', ''),
                    'revision_id': dep.get('revisionId', ''),
                    'deployment_status': dep.get('deploymentStatus', 'UNKNOWN'),
                    'is_latest_for_target': dep.get('isLatestForTarget', False),
                    'creation_timestamp': creation_ts,
                    'usecase_id': usecase_id
                })
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        logger.info(f"Found {len(deployments)} deployments")
        
        return create_response(200, {
            'deployments': deployments,
            'count': len(deployments)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing deployments: {str(e)}")
        return create_response(500, {'error': f'Failed to list deployments: {str(e)}'})
    except Exception as e:
        logger.error(f"Error listing deployments: {str(e)}")
        return create_response(500, {'error': 'Failed to list deployments'})


def get_deployment(deployment_id, user, query_params):
    """Get detailed information about a deployment"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        # Get deployment details
        response = greengrass_client.get_deployment(deploymentId=deployment_id)
        
        # Convert datetime fields
        creation_ts = response.get('creationTimestamp')
        if creation_ts and hasattr(creation_ts, 'isoformat'):
            creation_ts = creation_ts.isoformat()
        
        # Get components in deployment
        components = response.get('components', {})
        component_list = []
        for comp_name, comp_config in components.items():
            component_list.append({
                'component_name': comp_name,
                'component_version': comp_config.get('componentVersion', 'latest'),
                'configuration_update': comp_config.get('configurationUpdate', {})
            })
        
        deployment = {
            'deployment_id': response.get('deploymentId'),
            'deployment_name': response.get('deploymentName', ''),
            'target_arn': response.get('targetArn', ''),
            'revision_id': response.get('revisionId', ''),
            'deployment_status': response.get('deploymentStatus', 'UNKNOWN'),
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'is_latest_for_target': response.get('isLatestForTarget', False),
            'creation_timestamp': creation_ts,
            'components': component_list,
            'deployment_policies': response.get('deploymentPolicies', {}),
            'tags': response.get('tags', {}),
            'usecase_id': usecase_id
        }
        
        # Get effective deployments for target devices to show per-device status
        # This is especially useful for failed deployments to see which devices failed
        effective_deployments = []
        target_arn = response.get('targetArn', '')
        
        try:
            # Extract target name from ARN to get effective deployments
            if ':thing/' in target_arn:
                # Single device target
                thing_name = target_arn.split(':thing/')[-1]
                eff_response = greengrass_client.list_effective_deployments(
                    coreDeviceThingName=thing_name
                )
                for eff_dep in eff_response.get('effectiveDeployments', []):
                    if eff_dep.get('deploymentId') == deployment_id:
                        # Convert timestamps
                        modified_ts = eff_dep.get('modifiedTimestamp')
                        if modified_ts and hasattr(modified_ts, 'isoformat'):
                            modified_ts = modified_ts.isoformat()
                        
                        effective_deployments.append({
                            'core_device': thing_name,
                            'deployment_status': eff_dep.get('coreDeviceExecutionStatus', 'UNKNOWN'),
                            'reason': eff_dep.get('reason', ''),
                            'description': eff_dep.get('description', ''),
                            'status_details': eff_dep.get('statusDetails', {}),
                            'modified_timestamp': modified_ts
                        })
                        break
            elif ':thinggroup/' in target_arn:
                # Thing group target - list core devices in the group and get their status
                thing_group_name = target_arn.split(':thinggroup/')[-1]
                
                # List core devices (we'll check effective deployments for each)
                core_devices_response = greengrass_client.list_core_devices(
                    thingGroupArn=target_arn,
                    maxResults=50
                )
                
                for device in core_devices_response.get('coreDevices', []):
                    thing_name = device.get('coreDeviceThingName')
                    if thing_name:
                        try:
                            eff_response = greengrass_client.list_effective_deployments(
                                coreDeviceThingName=thing_name
                            )
                            for eff_dep in eff_response.get('effectiveDeployments', []):
                                if eff_dep.get('deploymentId') == deployment_id:
                                    modified_ts = eff_dep.get('modifiedTimestamp')
                                    if modified_ts and hasattr(modified_ts, 'isoformat'):
                                        modified_ts = modified_ts.isoformat()
                                    
                                    effective_deployments.append({
                                        'core_device': thing_name,
                                        'deployment_status': eff_dep.get('coreDeviceExecutionStatus', 'UNKNOWN'),
                                        'reason': eff_dep.get('reason', ''),
                                        'description': eff_dep.get('description', ''),
                                        'status_details': eff_dep.get('statusDetails', {}),
                                        'modified_timestamp': modified_ts
                                    })
                                    break
                        except ClientError as e:
                            logger.warning(f"Could not get effective deployments for {thing_name}: {e}")
        except ClientError as e:
            logger.warning(f"Could not get effective deployments: {e}")
        
        deployment['effective_deployments'] = effective_deployments
        
        # Extract error information from effective deployments
        error_messages = []
        for eff_dep in effective_deployments:
            if eff_dep.get('deployment_status') in ['FAILED', 'REJECTED', 'TIMED_OUT']:
                error_info = {
                    'device': eff_dep.get('core_device'),
                    'status': eff_dep.get('deployment_status'),
                    'reason': eff_dep.get('reason', ''),
                    'description': eff_dep.get('description', '')
                }
                # Add status details if available
                status_details = eff_dep.get('status_details', {})
                if status_details:
                    error_info['detailed_status'] = status_details.get('detailedStatus', '')
                    error_info['detailed_status_reason'] = status_details.get('detailedStatusReason', '')
                error_messages.append(error_info)
        
        deployment['error_messages'] = error_messages
        
        log_audit_event(
            user['user_id'], 'get_deployment', 'deployment', deployment_id, 'success'
        )
        
        return create_response(200, {'deployment': deployment})
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return create_response(404, {'error': 'Deployment not found'})
        logger.error(f"AWS error getting deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to get deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to get deployment'})


def create_deployment(body, user):
    """Create a new Greengrass deployment"""
    try:
        usecase_id = body.get('usecase_id')
        components = body.get('components', [])  # List of {component_name, component_version}
        target_devices = body.get('target_devices', [])
        target_thing_group = body.get('target_thing_group')
        deployment_name = body.get('deployment_name', '')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id required'})
        
        if not components:
            return create_response(400, {'error': 'At least one component required'})
        
        if not target_devices and not target_thing_group:
            return create_response(400, {'error': 'Either target_devices or target_thing_group required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        account_id = usecase.get('account_id', '')
        
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        # Build components map for deployment
        components_map = {}
        needs_nucleus = False
        
        for comp in components:
            comp_name = comp.get('component_name')
            comp_version = comp.get('component_version')
            if comp_name:
                components_map[comp_name] = {
                    'componentVersion': comp_version if comp_version else None
                }
                # Remove None values
                if components_map[comp_name]['componentVersion'] is None:
                    del components_map[comp_name]['componentVersion']
                
                # Check if this component requires Nucleus to be included
                for dda_comp in DDA_COMPONENTS_REQUIRING_NUCLEUS:
                    if comp_name.startswith(dda_comp):
                        needs_nucleus = True
                        break
        
        # Auto-include Greengrass Nucleus if deploying DDA components
        # This prevents "FAILED_NO_STATE_CHANGE" errors when Nucleus version needs updating
        auto_included = []
        if needs_nucleus and 'aws.greengrass.Nucleus' not in components_map:
            components_map['aws.greengrass.Nucleus'] = {
                'componentVersion': MIN_NUCLEUS_VERSION
            }
            auto_included.append({
                'component_name': 'aws.greengrass.Nucleus',
                'component_version': MIN_NUCLEUS_VERSION,
                'reason': 'Required for DDA component dependencies'
            })
            logger.info(f"Auto-included aws.greengrass.Nucleus {MIN_NUCLEUS_VERSION} for DDA component deployment")
        
        # Auto-include CloudWatch log manager for device logging
        if needs_nucleus and 'aws.greengrass.LogManager' not in components_map:
            # Configure LogManager to upload system and component logs
            log_manager_config = {
                'logsUploaderConfiguration': {
                    'systemLogsConfiguration': {
                        'uploadToCloudWatch': True,
                        'minimumLogLevel': 'INFO',
                        'diskSpaceLimit': 25,
                        'diskSpaceLimitUnit': 'MB',
                        'deleteLogFileAfterCloudUpload': False
                    },
                    'componentLogsConfigurationMap': {
                        # Upload all component logs
                        'com.aws.greengrass': {
                            'minimumLogLevel': 'INFO',
                            'diskSpaceLimit': 10,
                            'diskSpaceLimitUnit': 'MB',
                            'deleteLogFileAfterCloudUpload': False
                        },
                        # DDA components
                        'aws.edgeml.dda': {
                            'minimumLogLevel': 'INFO',
                            'diskSpaceLimit': 10,
                            'diskSpaceLimitUnit': 'MB',
                            'deleteLogFileAfterCloudUpload': False
                        },
                        # Model components
                        'model': {
                            'minimumLogLevel': 'INFO',
                            'diskSpaceLimit': 10,
                            'diskSpaceLimitUnit': 'MB',
                            'deleteLogFileAfterCloudUpload': False
                        }
                    },
                    'periodicUploadIntervalSec': 300  # Upload every 5 minutes
                }
            }
            
            components_map['aws.greengrass.LogManager'] = {
                'componentVersion': LOG_MANAGER_VERSION,
                'configurationUpdate': {
                    'merge': json.dumps(log_manager_config)
                }
            }
            auto_included.append({
                'component_name': 'aws.greengrass.LogManager',
                'component_version': LOG_MANAGER_VERSION,
                'reason': 'Required for CloudWatch logging from devices'
            })
            logger.info(f"Auto-included aws.greengrass.LogManager {LOG_MANAGER_VERSION} with configuration for device logging")
        
        # Auto-include InferenceUploader for automatic S3 upload of inference results
        # Only include if explicitly enabled in UseCase configuration (opt-in)
        enable_inference_uploader = usecase.get('enable_inference_uploader', False)
        
        if enable_inference_uploader and needs_nucleus and 'aws.edgeml.dda.InferenceUploader' not in components_map:
            # Build S3 configuration for InferenceUploader
            s3_bucket = usecase.get('inference_uploader_s3_bucket') or f"dda-inference-results-{account_id}"
            
            # Get configurable upload interval (default 5 minutes, can be immediate, hourly, etc.)
            upload_interval = usecase.get('inference_uploader_interval_seconds', 300)  # Default 5 minutes
            
            inference_uploader_config = {
                's3Bucket': s3_bucket,
                'uploadIntervalSeconds': upload_interval,
                'batchSize': usecase.get('inference_uploader_batch_size', 100),
                'localRetentionDays': usecase.get('inference_uploader_retention_days', 7),
                'uploadImages': usecase.get('inference_uploader_upload_images', True),
                'uploadMetadata': usecase.get('inference_uploader_upload_metadata', True),
                'inferenceResultsPath': '/aws_dda/inference-results',
                'awsRegion': region
            }
            
            components_map['aws.edgeml.dda.InferenceUploader'] = {
                'componentVersion': '1.0.0',
                'configurationUpdate': {
                    'merge': json.dumps(inference_uploader_config)
                }
            }
            auto_included.append({
                'component_name': 'aws.edgeml.dda.InferenceUploader',
                'component_version': '1.0.0',
                'reason': f'Automatic upload of inference results to S3 (interval: {upload_interval}s)'
            })
            logger.info(f"Auto-included aws.edgeml.dda.InferenceUploader with S3 bucket {s3_bucket}, interval {upload_interval}s")
        elif not enable_inference_uploader:
            logger.info("InferenceUploader not included - disabled in UseCase configuration")
        
        # Determine target ARN
        if target_thing_group:
            target_arn = f"arn:aws:iot:{region}:{account_id}:thinggroup/{target_thing_group}"
        else:
            # For single device deployment, target the thing directly
            # Note: Greengrass deployments typically target thing groups
            # For single device, we'll create deployment targeting the device's thing
            target_arn = f"arn:aws:iot:{region}:{account_id}:thing/{target_devices[0]}"
        
        # Generate deployment name if not provided
        if not deployment_name:
            timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
            deployment_name = f"portal-deployment-{timestamp}"
        
        # Create deployment
        deployment_params = {
            'targetArn': target_arn,
            'deploymentName': deployment_name,
            'components': components_map,
            'tags': {
                'dda-portal:managed': 'true',
                'dda-portal:usecase-id': usecase_id,
                'dda-portal:created-by': user['user_id']
            }
        }
        
        # Add deployment policies for rollout configuration
        rollout_config = body.get('rollout_config', {})
        if rollout_config:
            deployment_params['deploymentPolicies'] = {
                'failureHandlingPolicy': 'ROLLBACK' if rollout_config.get('auto_rollback', True) else 'DO_NOTHING',
                'componentUpdatePolicy': {
                    'timeoutInSeconds': rollout_config.get('timeout_seconds', 60),
                    'action': 'NOTIFY_COMPONENTS'
                }
            }
        
        response = greengrass_client.create_deployment(**deployment_params)
        
        deployment_id = response.get('deploymentId')
        
        log_audit_event(
            user['user_id'], 'create_deployment', 'deployment', deployment_id,
            'success', {
                'usecase_id': usecase_id,
                'components': list(components_map.keys()),
                'target_arn': target_arn
            }
        )
        
        logger.info(f"Created deployment {deployment_id} for usecase {usecase_id}")
        
        # Build response with full component list
        deployed_components = [
            {'component_name': name, 'component_version': config.get('componentVersion', 'latest')}
            for name, config in components_map.items()
        ]
        
        return create_response(201, {
            'deployment_id': deployment_id,
            'iot_job_id': response.get('iotJobId', ''),
            'iot_job_arn': response.get('iotJobArn', ''),
            'components': deployed_components,
            'auto_included': auto_included,
            'message': 'Deployment created successfully'
        })
        
    except ClientError as e:
        logger.error(f"AWS error creating deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to create deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error creating deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to create deployment'})


def cancel_deployment(deployment_id, user, query_params):
    """Cancel a Greengrass deployment"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        # Cancel deployment
        greengrass_client.cancel_deployment(deploymentId=deployment_id)
        
        log_audit_event(
            user['user_id'], 'cancel_deployment', 'deployment', deployment_id,
            'success', {'usecase_id': usecase_id}
        )
        
        logger.info(f"Cancelled deployment {deployment_id}")
        
        return create_response(200, {
            'message': 'Deployment cancelled successfully',
            'deployment_id': deployment_id
        })
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return create_response(404, {'error': 'Deployment not found'})
        logger.error(f"AWS error cancelling deployment: {str(e)}")
        return create_response(500, {'error': f'Failed to cancel deployment: {str(e)}'})
    except Exception as e:
        logger.error(f"Error cancelling deployment: {str(e)}")
        return create_response(500, {'error': 'Failed to cancel deployment'})


def list_public_components(greengrass_client):
    """List AWS public Greengrass components"""
    try:
        components = []
        next_token = None
        
        while True:
            params = {'scope': 'PUBLIC', 'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_components(**params)
            
            for comp in response.get('components', []):
                components.append({
                    'component_name': comp.get('componentName'),
                    'arn': comp.get('arn'),
                    'latest_version': comp.get('latestVersion', {}).get('componentVersion'),
                    'scope': 'PUBLIC'
                })
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        return components
    except ClientError as e:
        logger.warning(f"Could not list public components: {e}")
        return []
