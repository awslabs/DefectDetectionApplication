"""
Authentication and authorization middleware for Lambda functions.

This module provides middleware functions to handle JWT validation,
user context extraction, and permission checking for API Gateway Lambda functions.
"""

import json
import logging
import os
from functools import wraps
from typing import Dict, Any, Optional, Callable

from rbac_utils import (
    RBACManager, UserContext, Permission, Role,
    extract_user_context_from_jwt
)

logger = logging.getLogger(__name__)

def auth_required(func: Callable) -> Callable:
    """
    Decorator that requires authentication for a Lambda function.
    
    Extracts user context from JWT and adds it to the event.
    """
    @wraps(func)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        # Extract user context from JWT
        user_context = extract_user_context_from_jwt(event)
        
        if not user_context:
            logger.warning("Authentication failed - no valid user context")
            return {
                'statusCode': 401,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
                'body': json.dumps({
                    'error': 'Authentication required',
                    'message': 'Valid JWT token required'
                })
            }
        
        # Add user context to event for use by the handler
        event['user_context'] = user_context
        
        # Log the authenticated request
        logger.info(f"Authenticated request from user {user_context.user_id} "
                   f"with roles {[role.value for role in user_context.roles]}")
        
        return func(event, context)
    
    return wrapper

def require_permission(permission: Permission, usecase_id_param: Optional[str] = None):
    """
    Decorator that requires a specific permission for a Lambda function.
    
    Args:
        permission: Required permission
        usecase_id_param: Parameter name containing use case ID (for use case-specific permissions)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            # Get user context (should be set by auth_required decorator)
            user_context = event.get('user_context')
            if not user_context:
                return {
                    'statusCode': 401,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Authentication required'
                    })
                }
            
            # Extract use case ID if specified
            usecase_id = None
            if usecase_id_param:
                usecase_id = _extract_usecase_id(event, usecase_id_param)
            
            # Initialize RBAC manager
            rbac = RBACManager(os.environ.get('USER_ROLES_TABLE', ''))
            
            # Check permission
            if not rbac.has_permission(user_context, permission, usecase_id):
                logger.warning(f"Permission denied for user {user_context.user_id}: "
                             f"required {permission.value}, usecase_id={usecase_id}")
                
                return {
                    'statusCode': 403,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Insufficient permissions',
                        'required_permission': permission.value,
                        'usecase_id': usecase_id,
                        'message': f'This action requires {permission.value} permission'
                    })
                }
            
            # Log the authorized request
            logger.info(f"Authorized request from user {user_context.user_id} "
                       f"for permission {permission.value}, usecase_id={usecase_id}")
            
            return func(event, context)
        
        return wrapper
    return decorator

def require_usecase_access(usecase_id_param: str = 'usecase_id'):
    """
    Decorator that requires access to a specific use case.
    
    Args:
        usecase_id_param: Parameter name containing use case ID
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            # Get user context
            user_context = event.get('user_context')
            if not user_context:
                return {
                    'statusCode': 401,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Authentication required'
                    })
                }
            
            # Extract use case ID
            usecase_id = _extract_usecase_id(event, usecase_id_param)
            if not usecase_id:
                return {
                    'statusCode': 400,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Use case ID required',
                        'parameter': usecase_id_param
                    })
                }
            
            # Initialize RBAC manager
            rbac = RBACManager(os.environ.get('USER_ROLES_TABLE', ''))
            
            # Check use case access
            if not rbac.check_usecase_access(user_context, usecase_id):
                logger.warning(f"Use case access denied for user {user_context.user_id}: "
                             f"usecase_id={usecase_id}")
                
                return {
                    'statusCode': 403,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    'body': json.dumps({
                        'error': 'Use case access denied',
                        'usecase_id': usecase_id,
                        'message': 'You do not have access to this use case'
                    })
                }
            
            return func(event, context)
        
        return wrapper
    return decorator

def super_user_required(func: Callable) -> Callable:
    """
    Decorator that requires super user (PortalAdmin) privileges.
    """
    @wraps(func)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        # Get user context
        user_context = event.get('user_context')
        if not user_context:
            return {
                'statusCode': 401,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
                'body': json.dumps({
                    'error': 'Authentication required'
                })
            }
        
        # Check if user is super user
        if not user_context.is_super_user:
            logger.warning(f"Super user access denied for user {user_context.user_id}")
            
            return {
                'statusCode': 403,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
                'body': json.dumps({
                    'error': 'Super user privileges required',
                    'message': 'This action requires PortalAdmin role'
                })
            }
        
        # Log super user action
        logger.info(f"Super user action by {user_context.user_id}")
        
        return func(event, context)
    
    return wrapper

def _extract_usecase_id(event: Dict[str, Any], param_name: str) -> Optional[str]:
    """Extract use case ID from various parts of the event."""
    # Try path parameters first
    path_params = event.get('pathParameters') or {}
    if param_name in path_params:
        return path_params[param_name]
    
    # Try query string parameters
    query_params = event.get('queryStringParameters') or {}
    if param_name in query_params:
        return query_params[param_name]
    
    # Try request body
    try:
        body = json.loads(event.get('body', '{}'))
        if param_name in body:
            return body[param_name]
    except (json.JSONDecodeError, TypeError):
        pass
    
    return None

def create_response(status_code: int, body: Any, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Create a standardized API Gateway response.
    
    Args:
        status_code: HTTP status code
        body: Response body (will be JSON serialized)
        headers: Additional headers
        
    Returns:
        API Gateway response dictionary
    """
    default_headers = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
    }
    
    if headers:
        default_headers.update(headers)
    
    return {
        'statusCode': status_code,
        'headers': default_headers,
        'body': json.dumps(body) if not isinstance(body, str) else body
    }

def handle_cors_preflight(func: Callable) -> Callable:
    """
    Decorator to handle CORS preflight requests.
    """
    @wraps(func)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        # Handle OPTIONS request for CORS preflight
        if event.get('httpMethod') == 'OPTIONS':
            return create_response(200, '')
        
        return func(event, context)
    
    return wrapper

def log_request(func: Callable) -> Callable:
    """
    Decorator to log incoming requests for audit purposes.
    """
    @wraps(func)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        # Extract request info
        method = event.get('httpMethod', 'UNKNOWN')
        path = event.get('path', 'UNKNOWN')
        user_context = event.get('user_context')
        
        # Log request
        user_id = user_context.user_id if user_context else 'anonymous'
        logger.info(f"Request: {method} {path} by user {user_id}")
        
        # Call function and log response
        try:
            response = func(event, context)
            status_code = response.get('statusCode', 'UNKNOWN')
            logger.info(f"Response: {status_code} for {method} {path}")
            return response
        except Exception as e:
            logger.error(f"Error in {method} {path}: {str(e)}")
            raise
    
    return wrapper

# Convenience decorators for common permission combinations
def data_scientist_required(usecase_id_param: str = 'usecase_id'):
    """Require DataScientist role or higher with use case access."""
    def decorator(func: Callable) -> Callable:
        @auth_required
        @require_usecase_access(usecase_id_param)
        @wraps(func)
        def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            user_context = event['user_context']
            allowed_roles = {Role.PORTAL_ADMIN, Role.USECASE_ADMIN, Role.DATA_SCIENTIST}
            
            if not any(role in allowed_roles for role in user_context.roles):
                return create_response(403, {
                    'error': 'Insufficient role',
                    'message': 'DataScientist role or higher required'
                })
            
            return func(event, context)
        return wrapper
    return decorator

def operator_required(usecase_id_param: str = 'usecase_id'):
    """Require Operator role or higher with use case access."""
    def decorator(func: Callable) -> Callable:
        @auth_required
        @require_usecase_access(usecase_id_param)
        @wraps(func)
        def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            user_context = event['user_context']
            allowed_roles = {Role.PORTAL_ADMIN, Role.USECASE_ADMIN, Role.OPERATOR}
            
            if not any(role in allowed_roles for role in user_context.roles):
                return create_response(403, {
                    'error': 'Insufficient role',
                    'message': 'Operator role or higher required'
                })
            
            return func(event, context)
        return wrapper
    return decorator

def admin_required(usecase_id_param: Optional[str] = None):
    """Require Admin role (PortalAdmin or UseCaseAdmin)."""
    def decorator(func: Callable) -> Callable:
        @auth_required
        @wraps(func)
        def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
            user_context = event['user_context']
            
            # PortalAdmin has access to everything
            if Role.PORTAL_ADMIN in user_context.roles:
                return func(event, context)
            
            # UseCaseAdmin needs use case access if specified
            if Role.USECASE_ADMIN in user_context.roles:
                if usecase_id_param:
                    usecase_id = _extract_usecase_id(event, usecase_id_param)
                    if usecase_id and usecase_id in user_context.assigned_usecases:
                        return func(event, context)
                else:
                    return func(event, context)
            
            return create_response(403, {
                'error': 'Insufficient role',
                'message': 'Admin role required'
            })
        
        return wrapper
    return decorator