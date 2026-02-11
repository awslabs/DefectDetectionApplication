"""
Device Logs handler for Edge CV Portal
Fetches Greengrass component logs from CloudWatch Logs in UseCase Account
"""
import json
import logging
import os
from datetime import datetime, timedelta
import boto3
from botocore.exceptions import ClientError
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, assume_cross_account_role, get_usecase,
    create_boto3_client
)

# Import the analyzer function
try:
    from device_logs_analyzer import analyze_device_logs
except ImportError:
    # Fallback if analyzer is not available
    analyze_device_logs = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Handle device logs requests
    
    GET /api/v1/devices/{id}/logs                    - List available log groups for device
    GET /api/v1/devices/{id}/logs/{component}        - Get logs for specific component
    POST /api/v1/devices/{id}/logs/analyze           - Analyze device logs with AI
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}
        
        logger.info(f"Device logs request: {http_method} {path}")
        
        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        device_id = path_parameters.get('id')
        component_name = path_parameters.get('component')
        
        if not device_id:
            return create_response(400, {'error': 'Device ID is required'})
        
        # Route to analyzer for POST requests to /analyze
        if http_method == 'POST' and path.endswith('/analyze'):
            if analyze_device_logs:
                return analyze_device_logs(device_id, user, query_parameters)
            else:
                return create_response(503, {'error': 'Log analyzer not available'})
        
        if http_method == 'GET':
            if component_name:
                return get_component_logs(device_id, component_name, user, query_parameters)
            else:
                return list_log_groups(device_id, user, query_parameters)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in device logs handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_log_groups(device_id, user, query_params):
    """
    List available CloudWatch log groups for a Greengrass device.
    Also includes installed components that may have logs.
    
    Log group patterns:
    - System: /aws/greengrass/GreengrassSystemComponent/{region}/{thingName}
    - User components: /aws/greengrass/UserComponent/{region}/{thingName}/{componentName}
    """
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'list_device_logs', 'device', device_id,
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
        
        # Create CloudWatch Logs client with assumed role
        logs_client = create_boto3_client('logs', credentials, region)
        
        # Create Greengrass client to get installed components
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        
        log_groups = []
        existing_log_group_names = set()
        
        # Search for system component logs
        system_prefix = f'/aws/greengrass/GreengrassSystemComponent/{region}/{device_id}'
        try:
            response = logs_client.describe_log_groups(logGroupNamePrefix=system_prefix)
            for lg in response.get('logGroups', []):
                existing_log_group_names.add(lg['logGroupName'])
                log_groups.append({
                    'log_group_name': lg['logGroupName'],
                    'component_type': 'system',
                    'component_name': 'GreengrassSystemComponent',
                    'creation_time': lg.get('creationTime'),
                    'stored_bytes': lg.get('storedBytes', 0),
                    'retention_days': lg.get('retentionInDays'),
                    'has_logs': True
                })
        except ClientError as e:
            logger.warning(f"Could not list system log groups: {e}")
        
        # Search for user component logs
        user_prefix = f'/aws/greengrass/UserComponent/{region}/{device_id}'
        try:
            paginator = logs_client.get_paginator('describe_log_groups')
            for page in paginator.paginate(logGroupNamePrefix=user_prefix):
                for lg in page.get('logGroups', []):
                    existing_log_group_names.add(lg['logGroupName'])
                    # Extract component name from log group path
                    # Pattern: /aws/greengrass/UserComponent/{region}/{thingName}/{componentName}
                    parts = lg['logGroupName'].split('/')
                    component_name = parts[-1] if len(parts) > 5 else 'Unknown'
                    
                    log_groups.append({
                        'log_group_name': lg['logGroupName'],
                        'component_type': 'user',
                        'component_name': component_name,
                        'creation_time': lg.get('creationTime'),
                        'stored_bytes': lg.get('storedBytes', 0),
                        'retention_days': lg.get('retentionInDays'),
                        'has_logs': True
                    })
        except ClientError as e:
            logger.warning(f"Could not list user component log groups: {e}")
        
        # If no log groups found, get installed components and show them as potential log sources
        # This helps users understand what components exist even if logging isn't configured
        if len(log_groups) == 0:
            try:
                comp_response = greengrass_client.list_installed_components(
                    coreDeviceThingName=device_id,
                    maxResults=100
                )
                
                # Add system component placeholder
                log_groups.append({
                    'log_group_name': f'/aws/greengrass/GreengrassSystemComponent/{region}/{device_id}',
                    'component_type': 'system',
                    'component_name': 'GreengrassSystemComponent',
                    'creation_time': None,
                    'stored_bytes': 0,
                    'retention_days': None,
                    'has_logs': False,
                    'note': 'CloudWatch logging may not be configured'
                })
                
                for comp in comp_response.get('installedComponents', []):
                    comp_name = comp.get('componentName', '')
                    # Skip AWS-managed components that typically don't have user logs
                    if comp_name.startswith('aws.greengrass.'):
                        continue
                    
                    expected_log_group = f'/aws/greengrass/UserComponent/{region}/{device_id}/{comp_name}'
                    
                    log_groups.append({
                        'log_group_name': expected_log_group,
                        'component_type': 'user',
                        'component_name': comp_name,
                        'creation_time': None,
                        'stored_bytes': 0,
                        'retention_days': None,
                        'has_logs': False,
                        'note': 'CloudWatch logging may not be configured'
                    })
            except ClientError as e:
                logger.warning(f"Could not list installed components: {e}")
        
        log_audit_event(
            user['user_id'], 'list_device_logs', 'device', device_id,
            'success', {'usecase_id': usecase_id, 'log_group_count': len(log_groups)}
        )
        
        return create_response(200, {
            'device_id': device_id,
            'log_groups': log_groups,
            'count': len(log_groups)
        })
        
    except ClientError as e:
        logger.error(f"AWS error listing log groups: {str(e)}")
        return create_response(500, {'error': f'Failed to list log groups: {str(e)}'})
    except Exception as e:
        logger.error(f"Error listing log groups: {str(e)}")
        return create_response(500, {'error': 'Failed to list log groups'})


def get_component_logs(device_id, component_name, user, query_params):
    """
    Get logs for a specific component on a device.
    
    Query parameters:
    - usecase_id: Required
    - start_time: Unix timestamp in milliseconds (default: 1 hour ago)
    - end_time: Unix timestamp in milliseconds (default: now)
    - limit: Max number of log events (default: 100, max: 1000)
    - next_token: Pagination token for fetching more logs
    - filter_pattern: CloudWatch Logs filter pattern
    """
    try:
        usecase_id = query_params.get('usecase_id')
        
        if not usecase_id:
            return create_response(400, {'error': 'usecase_id parameter required'})
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'get_device_logs', 'device', device_id,
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
        
        # Create CloudWatch Logs client with assumed role
        logs_client = create_boto3_client('logs', credentials, region)
        
        # Parse query parameters
        now = int(datetime.utcnow().timestamp() * 1000)
        one_hour_ago = int((datetime.utcnow() - timedelta(hours=1)).timestamp() * 1000)
        
        start_time = int(query_params.get('start_time', one_hour_ago))
        end_time = int(query_params.get('end_time', now))
        limit = min(int(query_params.get('limit', 100)), 1000)
        next_token = query_params.get('next_token')
        filter_pattern = query_params.get('filter_pattern', '')
        
        # Determine log group name based on component
        if component_name == 'GreengrassSystemComponent' or component_name == 'system':
            log_group_name = f'/aws/greengrass/GreengrassSystemComponent/{region}/{device_id}'
        else:
            log_group_name = f'/aws/greengrass/UserComponent/{region}/{device_id}/{component_name}'
        
        # Check if log group exists
        try:
            logs_client.describe_log_groups(logGroupNamePrefix=log_group_name, limit=1)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return create_response(404, {
                    'error': f'Log group not found for component {component_name}',
                    'log_group_name': log_group_name
                })
            raise
        
        # Fetch logs using filter_log_events for better filtering
        params = {
            'logGroupName': log_group_name,
            'startTime': start_time,
            'endTime': end_time,
            'limit': limit,
        }
        
        if filter_pattern:
            params['filterPattern'] = filter_pattern
        
        if next_token:
            params['nextToken'] = next_token
        
        try:
            response = logs_client.filter_log_events(**params)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return create_response(200, {
                    'device_id': device_id,
                    'component_name': component_name,
                    'log_group_name': log_group_name,
                    'logs': [],
                    'count': 0,
                    'message': 'No logs found for this time range'
                })
            raise
        
        logs = []
        for event in response.get('events', []):
            logs.append({
                'timestamp': event.get('timestamp'),
                'message': event.get('message'),
                'log_stream_name': event.get('logStreamName'),
                'ingestion_time': event.get('ingestionTime')
            })
        
        log_audit_event(
            user['user_id'], 'get_device_logs', 'device', device_id,
            'success', {
                'usecase_id': usecase_id,
                'component_name': component_name,
                'log_count': len(logs)
            }
        )
        
        result = {
            'device_id': device_id,
            'component_name': component_name,
            'log_group_name': log_group_name,
            'logs': logs,
            'count': len(logs),
            'start_time': start_time,
            'end_time': end_time
        }
        
        if response.get('nextToken'):
            result['next_token'] = response['nextToken']
        
        return create_response(200, result)
        
    except ClientError as e:
        logger.error(f"AWS error getting logs: {str(e)}")
        return create_response(500, {'error': f'Failed to get logs: {str(e)}'})
    except Exception as e:
        logger.error(f"Error getting logs: {str(e)}")
        return create_response(500, {'error': 'Failed to get logs'})
