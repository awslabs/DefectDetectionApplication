"""
Authentication handler for Edge CV Portal
"""
import json
import logging
import os
import boto3
from botocore.exceptions import ClientError
from shared_utils import create_response, get_user_from_event, is_super_user

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cognito client for token operations
cognito_client = boto3.client('cognito-idp')
USER_POOL_ID = os.environ.get('USER_POOL_ID')


def handler(event, context):
    """
    Handle authentication-related requests
    
    GET /api/v1/auth/me - Get current user information
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        logger.info(f"Auth request: {http_method} {path}")
        
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
        
        if http_method == 'GET' and path.endswith('/me'):
            return get_current_user(event)
        elif http_method == 'POST' and path.endswith('/refresh'):
            return refresh_token(event)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in auth handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def get_current_user(event):
    """Get current user information from JWT token"""
    try:
        user = get_user_from_event(event)
        
        # Check if user is super user
        user['is_super_user'] = is_super_user(user['user_id'])
        
        logger.info(f"User info retrieved: {user['user_id']}")
        
        return create_response(200, {
            'user': user,
            'message': 'User information retrieved successfully'
        })
        
    except Exception as e:
        logger.error(f"Error getting current user: {str(e)}")
        return create_response(500, {'error': 'Failed to retrieve user information'})


def refresh_token(event):
    """
    Refresh JWT tokens using Cognito refresh token
    POST /api/v1/auth/refresh
    Body: { "refresh_token": "..." }
    """
    try:
        body = json.loads(event.get('body', '{}'))
        refresh_token = body.get('refresh_token')
        
        if not refresh_token:
            return create_response(400, {'error': 'refresh_token is required'})
        
        # Get client ID from user pool configuration
        # Note: In production, this should be passed as environment variable
        # For now, we'll extract it from the event context or use a default
        client_id = body.get('client_id')
        if not client_id:
            return create_response(400, {'error': 'client_id is required'})
        
        # Call Cognito to refresh the token
        response = cognito_client.initiate_auth(
            ClientId=client_id,
            AuthFlow='REFRESH_TOKEN_AUTH',
            AuthParameters={
                'REFRESH_TOKEN': refresh_token
            }
        )
        
        auth_result = response.get('AuthenticationResult', {})
        
        # Return new tokens
        return create_response(200, {
            'access_token': auth_result.get('AccessToken'),
            'id_token': auth_result.get('IdToken'),
            'token_type': 'Bearer',
            'expires_in': auth_result.get('ExpiresIn', 3600),
            'message': 'Token refreshed successfully'
        })
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', 'Unknown error')
        
        logger.error(f"Cognito error refreshing token: {error_code} - {error_message}")
        
        if error_code == 'NotAuthorizedException':
            return create_response(401, {'error': 'Invalid or expired refresh token'})
        elif error_code == 'UserNotFoundException':
            return create_response(404, {'error': 'User not found'})
        else:
            return create_response(500, {'error': f'Token refresh failed: {error_message}'})
        
    except json.JSONDecodeError:
        return create_response(400, {'error': 'Invalid JSON in request body'})
    except Exception as e:
        logger.error(f"Error refreshing token: {str(e)}")
        return create_response(500, {'error': 'Failed to refresh token'})
