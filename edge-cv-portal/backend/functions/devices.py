"""
Devices handler for Edge CV Portal
Queries IoT Core Things tagged with dda-portal:managed=true
"""
import json
import logging
import os
import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, assume_cross_account_role, get_usecase
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
USECASES_TABLE = os.environ.get('USECASES_TABLE')


def handler(event, context):
    """
    Handle device management requests
    
    GET /api/v1/devices       - List devices (IoT Things tagged with dda-portal:managed=true)
    GET /api/v1/devices/{id}  - Get device details
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Devices request: {http_method} {path}")
        
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
        
        user = get_user_from_event(event)
        
        if http_method == 'GET' and not path_parameters.get('id'):
            return list_devices(user, query_parameters)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_device(path_parameters['id'], user, query_parameters)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in devices handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_devices(user, query_params):
    """
    List Greengrass Core Devices tagged with dda-portal:managed=true.
    Only shows devices set up via setup_station.sh script.
    """
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'list_devices', 'device', 'all',
                'failure', {'reason': 'access_denied', 'usecase_id': usecase_id}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details for cross-account access
        usecase = get_usecase(usecase_id)
        if not usecase:
            return create_response(404, {'error': 'Use case not found'})
        
        # Assume cross-account role
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'],
            usecase['external_id']
        )
        
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        # Create clients with assumed role
        greengrass_client = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=region
        )
        
        iot_client = boto3.client(
            'iot',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=region
        )
        
        devices = []
        next_token = None
        account_id = usecase.get('account_id', '')
        
        # List all Greengrass core devices, then filter by tag on core device
        while True:
            params = {'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            
            response = greengrass_client.list_core_devices(**params)
            
            for device in response.get('coreDevices', []):
                thing_name = device.get('coreDeviceThingName')
                
                # Check if this Greengrass Core Device has the dda-portal:managed tag
                try:
                    gg_arn = f"arn:aws:greengrass:{region}:{account_id}:coreDevices:{thing_name}"
                    gg_tags_response = greengrass_client.list_tags_for_resource(resourceArn=gg_arn)
                    tags = gg_tags_response.get('tags', {})
                    
                    # Only include devices with dda-portal:managed=true tag
                    if tags.get('dda-portal:managed') == 'true':
                        # Get IoT Thing ARN
                        thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{thing_name}"
                        try:
                            thing_response = iot_client.describe_thing(thingName=thing_name)
                            thing_arn = thing_response.get('thingArn', thing_arn)
                        except ClientError:
                            pass
                        
                        # Convert datetime to ISO string
                        last_status = device.get('lastStatusUpdateTimestamp')
                        if last_status:
                            last_status = last_status.isoformat() if hasattr(last_status, 'isoformat') else str(last_status)
                        
                        devices.append({
                            'device_id': thing_name,
                            'thing_name': thing_name,
                            'thing_arn': thing_arn,
                            'status': device.get('status', 'UNKNOWN'),
                            'last_status_update': last_status,
                            'tags': tags,
                            'usecase_id': usecase_id
                        })
                except ClientError as e:
                    logger.warning(f"Could not check tags for {thing_name}: {e}")
            
            next_token = response.get('nextToken')
            if not next_token:
                break
        
        logger.info(f"Found {len(devices)} portal-managed Greengrass core devices")
        
        log_audit_event(
            user['user_id'], 'list_devices', 'device', 'all',
            'success', {'usecase_id': usecase_id, 'count': len(devices)}
        )
        
        return create_response(200, {
            'devices': devices,
            'count': len(devices)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing devices: {str(e)}")
        return create_response(500, {'error': f'Failed to list devices: {str(e)}'})
    except Exception as e:
        logger.error(f"Error listing devices: {str(e)}")
        return create_response(500, {'error': 'Failed to list devices'})


def get_device(device_id, user, query_params):
    """Get detailed information about a specific device (Greengrass Core Device)"""
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'get_device', 'device', device_id,
                'failure', {'reason': 'access_denied'}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # Get usecase details for cross-account access
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
        
        # Create clients with assumed role
        iot_client = boto3.client(
            'iot',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=region
        )
        
        greengrass_client = boto3.client(
            'greengrassv2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=region
        )
        
        # Get thing details
        thing_arn = f"arn:aws:iot:{region}:{account_id}:thing/{device_id}"
        thing_type = ''
        attributes = {}
        version = 0
        try:
            thing_details = iot_client.describe_thing(thingName=device_id)
            thing_arn = thing_details.get('thingArn', thing_arn)
            thing_type = thing_details.get('thingTypeName', '')
            attributes = thing_details.get('attributes', {})
            version = thing_details.get('version', 0)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return create_response(404, {'error': 'Device not found'})
            logger.warning(f"Could not get IoT thing details: {e}")
        
        # Get Greengrass Core Device tags
        tags = {}
        try:
            gg_arn = f"arn:aws:greengrass:{region}:{account_id}:coreDevices:{device_id}"
            gg_tags_response = greengrass_client.list_tags_for_resource(resourceArn=gg_arn)
            tags = gg_tags_response.get('tags', {})
        except ClientError as e:
            logger.warning(f"Could not get Greengrass tags: {e}")
        
        # Get Greengrass core device status
        gg_status = get_greengrass_status(greengrass_client, device_id)
        
        # Get installed components
        installed_components = get_installed_components(greengrass_client, device_id)
        
        # Get effective deployments
        deployments = get_device_deployments(greengrass_client, device_id)
        
        # Convert datetime to ISO string
        last_status = gg_status.get('lastStatusUpdateTimestamp')
        if last_status:
            last_status = last_status.isoformat() if hasattr(last_status, 'isoformat') else str(last_status)
        
        device = {
            'device_id': device_id,
            'thing_name': device_id,
            'thing_arn': thing_arn,
            'thing_type': thing_type,
            'attributes': attributes,
            'version': version,
            'tags': tags,
            'status': gg_status.get('status', 'UNKNOWN'),
            'last_status_update': last_status,
            'greengrass_version': gg_status.get('coreVersion', ''),
            'platform': gg_status.get('platform', ''),
            'architecture': gg_status.get('architecture', ''),
            'installed_components': installed_components,
            'deployments': deployments,
            'usecase_id': usecase_id
        }
        
        log_audit_event(
            user['user_id'], 'get_device', 'device', device_id, 'success'
        )
        
        return create_response(200, {'device': device})
        
    except ClientError as e:
        logger.error(f"AWS error getting device: {str(e)}")
        return create_response(500, {'error': f'Failed to get device: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting device: {str(e)}")
        return create_response(500, {'error': 'Failed to get device'})


def get_greengrass_status(greengrass_client, thing_name):
    """Get Greengrass core device status"""
    try:
        response = greengrass_client.get_core_device(coreDeviceThingName=thing_name)
        return {
            'status': response.get('status', 'UNKNOWN'),
            'lastStatusUpdateTimestamp': response.get('lastStatusUpdateTimestamp'),
            'coreVersion': response.get('coreVersion', ''),
            'platform': response.get('platform', ''),
            'architecture': response.get('architecture', ''),
            'tags': response.get('tags', {})
        }
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            return {'status': 'NOT_GREENGRASS'}
        logger.warning(f"Could not get Greengrass status for {thing_name}: {e}")
        return {'status': 'UNKNOWN'}


def get_installed_components(greengrass_client, thing_name):
    """Get list of installed components on a Greengrass core device"""
    try:
        components = []
        paginator = greengrass_client.get_paginator('list_installed_components')
        
        for page in paginator.paginate(coreDeviceThingName=thing_name):
            for comp in page.get('installedComponents', []):
                # Convert datetime fields to ISO strings
                last_status_change = comp.get('lastStatusChangeTimestamp')
                if last_status_change and hasattr(last_status_change, 'isoformat'):
                    last_status_change = last_status_change.isoformat()
                
                last_reported = comp.get('lastReportedTimestamp')
                if last_reported and hasattr(last_reported, 'isoformat'):
                    last_reported = last_reported.isoformat()
                
                components.append({
                    'componentName': comp.get('componentName'),
                    'componentVersion': comp.get('componentVersion'),
                    'lifecycleState': comp.get('lifecycleState'),
                    'lifecycleStateDetails': comp.get('lifecycleStateDetails'),
                    'isRoot': comp.get('isRoot', False),
                    'lastStatusChangeTimestamp': last_status_change,
                    'lastInstallationSource': comp.get('lastInstallationSource'),
                    'lastReportedTimestamp': last_reported
                })
        
        return components
    except ClientError as e:
        logger.warning(f"Could not get installed components for {thing_name}: {e}")
        return []


def get_device_deployments(greengrass_client, thing_name):
    """Get deployments targeting this device"""
    try:
        deployments = []
        
        # List deployments targeting this core device
        response = greengrass_client.list_effective_deployments(
            coreDeviceThingName=thing_name,
            maxResults=50
        )
        
        for dep in response.get('effectiveDeployments', []):
            # Convert datetime fields to ISO strings
            creation_ts = dep.get('creationTimestamp')
            if creation_ts and hasattr(creation_ts, 'isoformat'):
                creation_ts = creation_ts.isoformat()
            
            modified_ts = dep.get('modifiedTimestamp')
            if modified_ts and hasattr(modified_ts, 'isoformat'):
                modified_ts = modified_ts.isoformat()
            
            deployments.append({
                'deploymentId': dep.get('deploymentId'),
                'deploymentName': dep.get('deploymentName'),
                'iotJobId': dep.get('iotJobId'),
                'iotJobArn': dep.get('iotJobArn'),
                'targetArn': dep.get('targetArn'),
                'coreDeviceExecutionStatus': dep.get('coreDeviceExecutionStatus'),
                'reason': dep.get('reason'),
                'creationTimestamp': creation_ts,
                'modifiedTimestamp': modified_ts
            })
        
        return deployments
    except ClientError as e:
        logger.warning(f"Could not get deployments for {thing_name}: {e}")
        return []
