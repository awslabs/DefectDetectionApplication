"""
Devices handler for Edge CV Portal
"""
import json
import logging
import os
import boto3
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
DEVICES_TABLE = os.environ.get('DEVICES_TABLE')


def handler(event, context):
    """
    Handle device management requests
    
    GET /api/v1/devices       - List devices
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
            return get_device(path_parameters['id'], user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in devices handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_devices(user, query_params):
    """List all devices accessible to the user"""
    try:
        table = dynamodb.Table(DEVICES_TABLE)
        usecase_id = query_params.get('usecase_id')
        
        # Check access if usecase_id is specified
        if usecase_id and not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'list_devices', 'device', 'all',
                'failure', {'reason': 'access_denied', 'usecase_id': usecase_id}
            )
            return create_response(403, {'error': 'Access denied'})
        
        # If usecase_id is provided, query by GSI
        if usecase_id:
            response = table.query(
                IndexName='usecase-devices-index',
                KeyConditionExpression='usecase_id = :usecase_id',
                ExpressionAttributeValues={':usecase_id': usecase_id}
            )
        else:
            # If super user, return all devices
            if is_super_user(user['user_id']):
                response = table.scan()
            else:
                # For regular users, return empty list (they should specify usecase_id)
                response = {'Items': []}
        
        devices = response.get('Items', [])
        
        log_audit_event(
            user['user_id'], 'list_devices', 'device', 'all',
            'success', {'usecase_id': usecase_id, 'count': len(devices)}
        )
        
        return create_response(200, {
            'devices': devices,
            'count': len(devices)
        })
        
    except Exception as e:
        logger.error(f"Error listing devices: {str(e)}")
        log_audit_event(
            user['user_id'], 'list_devices', 'device', 'all', 'failure'
        )
        return create_response(500, {'error': 'Failed to list devices'})


def get_device(device_id, user):
    """Get device details"""
    try:
        table = dynamodb.Table(DEVICES_TABLE)
        response = table.get_item(Key={'device_id': device_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Device not found'})
        
        device = response['Item']
        usecase_id = device.get('usecase_id')
        
        # Check access
        if not is_super_user(user['user_id']) and not check_user_access(user['user_id'], usecase_id):
            log_audit_event(
                user['user_id'], 'get_device', 'device', device_id,
                'failure', {'reason': 'access_denied'}
            )
            return create_response(403, {'error': 'Access denied'})
        
        log_audit_event(
            user['user_id'], 'get_device', 'device', device_id, 'success'
        )
        
        return create_response(200, {'device': device})
        
    except Exception as e:
        logger.error(f"Error getting device: {str(e)}")
        log_audit_event(
            user['user_id'], 'get_device', 'device', device_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to get device'})
