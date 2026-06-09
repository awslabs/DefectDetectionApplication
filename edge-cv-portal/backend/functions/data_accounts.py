"""
Data Accounts Management for Edge CV Portal

This module handles registration and management of Data Accounts.
Data Accounts are separate AWS accounts that store training data,
allowing usecases to access data cross-account.

Only PortalAdmin users can manage Data Accounts.
"""
import json
import os
import logging
from typing import Dict, Any
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
import uuid

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    validate_required_fields, assume_cross_account_role as assume_role,
    require_super_user
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
DATA_ACCOUNTS_TABLE = os.environ.get('DATA_ACCOUNTS_TABLE')


def is_portal_admin(user: Dict) -> bool:
    """Check if user is a PortalAdmin"""
    return user.get('role') == 'PortalAdmin' or 'PortalAdmin' in user.get('groups', [])


def test_data_account_connection(
    role_arn: str,
    external_id: str
) -> Dict:
    """
    Test connection to Data Account by assuming role.
    
    Returns:
        dict with status and details
    """
    try:
        # Assume Data Account role
        credentials = assume_role(role_arn, external_id, 'test-connection')
        
        # Create S3 client with Data Account credentials to verify access
        s3_data = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # List buckets to verify S3 access
        s3_data.list_buckets()
        
        return {
            'status': 'success',
            'message': 'Successfully connected to Data Account'
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        
        if error_code == 'AccessDenied':
            return {
                'status': 'failed',
                'error': 'Access denied. Check role ARN and external ID.',
                'details': error_message
            }
        else:
            return {
                'status': 'failed',
                'error': f"{error_code}: {error_message}"
            }
    except Exception as e:
        return {
            'status': 'failed',
            'error': str(e)
        }


def handler(event: Dict, context: Any) -> Dict:
    """
    Lambda handler for Data Accounts management.
    
    GET    /api/v1/data-accounts           - List Data Accounts (All authenticated users - read-only)
    POST   /api/v1/data-accounts           - Register Data Account (PortalAdmin only)
    GET    /api/v1/data-accounts/{id}      - Get Data Account details (PortalAdmin only)
    PUT    /api/v1/data-accounts/{id}      - Update Data Account (PortalAdmin only)
    DELETE /api/v1/data-accounts/{id}      - Delete Data Account (PortalAdmin only)
    POST   /api/v1/data-accounts/{id}/test - Test connection (PortalAdmin only)
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_params = event.get('pathParameters') or {}
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        
        # List Data Accounts is allowed for all authenticated users (read-only for dropdown)
        # All other operations require PortalAdmin
        is_list_operation = http_method == 'GET' and not path_params.get('id')
        
        if not is_list_operation and not is_portal_admin(user):
            return create_response(403, {'error': 'PortalAdmin access required'})
        
        # Route to appropriate handler
        if http_method == 'GET' and not path_params.get('id'):
            return list_data_accounts(event, user)
        elif http_method == 'POST' and not path_params.get('id'):
            return create_data_account(event, user)
        elif http_method == 'GET' and path_params.get('id'):
            return get_data_account(event, user, path_params['id'])
        elif http_method == 'PUT' and path_params.get('id'):
            return update_data_account(event, user, path_params['id'])
        elif http_method == 'DELETE' and path_params.get('id'):
            return delete_data_account(event, user, path_params['id'])
        elif http_method == 'POST' and '/test' in path:
            return test_connection(event, user, path_params['id'])
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_data_accounts(event: Dict, user: Dict) -> Dict:
    """
    List all registered Data Accounts.
    
    This endpoint is accessible to all authenticated users (read-only)
    to populate the Data Account dropdown in UseCase onboarding.
    Only PortalAdmin can create/update/delete Data Accounts.
    """
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.scan()
        
        data_accounts = response.get('Items', [])
        
        # Sort by created_at descending
        data_accounts.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        return create_response(200, {
            'data_accounts': data_accounts,
            'count': len(data_accounts)
        })
        
    except Exception as e:
        logger.error(f"Error listing data accounts: {str(e)}")
        return create_response(500, {'error': 'Failed to list data accounts'})


def create_data_account(event: Dict, user: Dict) -> Dict:
    """Register a new Data Account"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = [
            'data_account_id',
            'name',
            'role_arn',
            'external_id'
        ]
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        data_account_id = body['data_account_id']
        
        # Check if Data Account already exists
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' in existing:
            return create_response(409, {'error': 'Data Account already registered'})
        
        # Test connection before registering
        test_result = test_data_account_connection(
            role_arn=body['role_arn'],
            external_id=body['external_id']
        )
        
        if test_result['status'] != 'success':
            return create_response(400, {
                'error': 'Failed to connect to Data Account',
                'details': test_result
            })
        
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            'data_account_id': data_account_id,
            'name': body['name'],
            'description': body.get('description', ''),
            'role_arn': body['role_arn'],
            'external_id': body['external_id'],
            'region': body.get('region', 'us-east-1'),
            'status': 'active',
            'created_at': timestamp,
            'created_by': user['user_id'],
            'updated_at': timestamp,
            'tags': body.get('tags', {}),
            'connection_test': test_result
        }
        
        table.put_item(Item=item)
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='create_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success',
            details={'name': body['name']}
        )
        
        return create_response(201, {
            'message': 'Data Account registered successfully',
            'data_account': item
        })
        
    except Exception as e:
        logger.error(f"Error creating data account: {str(e)}")
        return create_response(500, {'error': f'Failed to create data account: {str(e)}'})


def get_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Get Data Account details"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.get_item(Key={'data_account_id': data_account_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Data Account not found'})
        
        return create_response(200, {'data_account': response['Item']})
        
    except Exception as e:
        logger.error(f"Error getting data account: {str(e)}")
        return create_response(500, {'error': 'Failed to get data account'})


def update_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Update Data Account"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        
        # Check if exists
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' not in existing:
            return create_response(404, {'error': 'Data Account not found'})
        
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        # Build update expression
        update_expr = 'SET updated_at = :updated_at'
        expr_values = {':updated_at': timestamp}
        
        if 'name' in body:
            update_expr += ', #name = :name'
            expr_values[':name'] = body['name']
        
        if 'description' in body:
            update_expr += ', description = :description'
            expr_values[':description'] = body['description']
        
        if 'role_arn' in body:
            update_expr += ', role_arn = :role_arn'
            expr_values[':role_arn'] = body['role_arn']
        
        if 'external_id' in body:
            update_expr += ', external_id = :external_id'
            expr_values[':external_id'] = body['external_id']
        
        if 'status' in body:
            update_expr += ', #status = :status'
            expr_values[':status'] = body['status']
        
        if 'tags' in body:
            update_expr += ', tags = :tags'
            expr_values[':tags'] = body['tags']
        
        # Update item
        response = table.update_item(
            Key={'data_account_id': data_account_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames={'#name': 'name', '#status': 'status'} if 'name' in body or 'status' in body else None,
            ReturnValues='ALL_NEW'
        )
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='update_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success',
            details=body
        )
        
        return create_response(200, {
            'message': 'Data Account updated successfully',
            'data_account': response['Attributes']
        })
        
    except Exception as e:
        logger.error(f"Error updating data account: {str(e)}")
        return create_response(500, {'error': f'Failed to update data account: {str(e)}'})


def delete_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Delete Data Account"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        
        # Check if exists
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' not in existing:
            return create_response(404, {'error': 'Data Account not found'})
        
        # Delete item
        table.delete_item(Key={'data_account_id': data_account_id})
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='delete_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success'
        )
        
        return create_response(200, {'message': 'Data Account deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting data account: {str(e)}")
        return create_response(500, {'error': 'Failed to delete data account'})


def test_connection(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Test connection to Data Account"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.get_item(Key={'data_account_id': data_account_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Data Account not found'})
        
        data_account = response['Item']
        
        # Test connection
        test_result = test_data_account_connection(
            role_arn=data_account['role_arn'],
            external_id=data_account['external_id']
        )
        
        # Update connection test result
        table.update_item(
            Key={'data_account_id': data_account_id},
            UpdateExpression='SET connection_test = :test, last_tested_at = :tested_at',
            ExpressionAttributeValues={
                ':test': test_result,
                ':tested_at': int(datetime.utcnow().timestamp() * 1000)
            }
        )
        
        return create_response(200, {
            'message': 'Connection test complete',
            'result': test_result
        })
        
    except Exception as e:
        logger.error(f"Error testing connection: {str(e)}")
        return create_response(500, {'error': 'Failed to test connection'})
