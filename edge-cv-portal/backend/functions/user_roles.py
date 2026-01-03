"""
Lambda function for managing user roles and use case assignments.

This function handles CRUD operations for user-to-use-case assignments
and role management in the DDA Portal.
"""

import json
import logging
import os
from typing import Dict, Any

# Import shared utilities
import sys
sys.path.append('/opt/python')

from auth_middleware import (
    auth_required, admin_required, super_user_required,
    create_response, handle_cors_preflight, log_request
)
from user_roles_dao import UserRolesDAO
from rbac_utils import Role

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize DAO
user_roles_dao = UserRolesDAO(os.environ['USER_ROLES_TABLE'])

@handle_cors_preflight
@log_request
@auth_required
def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main handler for user roles API endpoints.
    
    Routes:
    - GET /api/v1/users/{user_id}/usecases - List user's use case assignments
    - POST /api/v1/users/{user_id}/usecases - Assign user to use case
    - DELETE /api/v1/users/{user_id}/usecases/{usecase_id} - Remove assignment
    - PUT /api/v1/users/{user_id}/usecases/{usecase_id} - Update user role
    - GET /api/v1/usecases/{usecase_id}/users - List use case users
    - GET /api/v1/users/roles/{role} - List users with specific role
    """
    try:
        method = event['httpMethod']
        path = event['path']
        path_params = event.get('pathParameters', {}) or {}
        
        # Route to appropriate handler
        if '/users/' in path and '/usecases' in path:
            if method == 'GET' and path.endswith('/usecases'):
                return get_user_usecases(event, context)
            elif method == 'POST' and path.endswith('/usecases'):
                return assign_user_to_usecase(event, context)
            elif method == 'DELETE' and 'usecase_id' in path_params:
                return remove_user_from_usecase(event, context)
            elif method == 'PUT' and 'usecase_id' in path_params:
                return update_user_role(event, context)
        
        elif '/usecases/' in path and '/users' in path:
            if method == 'GET':
                return get_usecase_users(event, context)
        
        elif '/users/roles/' in path:
            if method == 'GET':
                return get_users_by_role(event, context)
        
        return create_response(404, {
            'error': 'Not found',
            'message': f'Route not found: {method} {path}'
        })
        
    except Exception as e:
        logger.error(f"Error in user roles handler: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

def get_user_usecases(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Get all use cases assigned to a user."""
    try:
        user_id = event['pathParameters']['user_id']
        user_context = event['user_context']
        
        # Users can view their own assignments, admins can view any
        if user_id != user_context.user_id and not user_context.is_super_user:
            # Check if user has admin role for any shared use case
            user_usecases = set(user_roles_dao.get_user_usecases(user_id))
            admin_usecases = user_context.assigned_usecases
            
            if not (user_usecases & admin_usecases) and Role.USECASE_ADMIN not in user_context.roles:
                return create_response(403, {
                    'error': 'Access denied',
                    'message': 'Cannot view other user assignments'
                })
        
        assignments = user_roles_dao.get_user_usecases(user_id)
        
        return create_response(200, {
            'user_id': user_id,
            'assignments': assignments
        })
        
    except Exception as e:
        logger.error(f"Error getting user use cases: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@admin_required()
def assign_user_to_usecase(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Assign a user to a use case with a specific role."""
    try:
        user_id = event['pathParameters']['user_id']
        user_context = event['user_context']
        
        body = json.loads(event['body'])
        usecase_id = body['usecase_id']
        role = body['role']
        
        # Validate role
        try:
            Role(role)
        except ValueError:
            return create_response(400, {
                'error': 'Invalid role',
                'message': f'Role must be one of: {[r.value for r in Role]}'
            })
        
        # Non-super users can only assign to their own use cases
        if not user_context.is_super_user:
            if usecase_id not in user_context.assigned_usecases:
                return create_response(403, {
                    'error': 'Access denied',
                    'message': 'Cannot assign users to use cases you do not have access to'
                })
            
            # UseCaseAdmins cannot assign PortalAdmin role
            if role == Role.PORTAL_ADMIN.value and Role.PORTAL_ADMIN not in user_context.roles:
                return create_response(403, {
                    'error': 'Access denied',
                    'message': 'Cannot assign PortalAdmin role'
                })
        
        success = user_roles_dao.assign_user_to_usecase(
            user_id, usecase_id, role, user_context.user_id
        )
        
        if success:
            return create_response(201, {
                'message': 'User assigned successfully',
                'user_id': user_id,
                'usecase_id': usecase_id,
                'role': role
            })
        else:
            return create_response(500, {
                'error': 'Assignment failed',
                'message': 'Failed to assign user to use case'
            })
        
    except json.JSONDecodeError:
        return create_response(400, {
            'error': 'Invalid JSON',
            'message': 'Request body must be valid JSON'
        })
    except KeyError as e:
        return create_response(400, {
            'error': 'Missing required field',
            'message': f'Required field missing: {str(e)}'
        })
    except Exception as e:
        logger.error(f"Error assigning user to use case: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@admin_required('usecase_id')
def remove_user_from_usecase(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Remove a user's assignment from a use case."""
    try:
        user_id = event['pathParameters']['user_id']
        usecase_id = event['pathParameters']['usecase_id']
        user_context = event['user_context']
        
        # Users cannot remove themselves if they are the only admin
        if user_id == user_context.user_id:
            usecase_users = user_roles_dao.get_usecase_users(usecase_id)
            admin_count = sum(1 for u in usecase_users 
                            if u['role'] in [Role.PORTAL_ADMIN.value, Role.USECASE_ADMIN.value])
            
            if admin_count <= 1:
                return create_response(400, {
                    'error': 'Cannot remove last admin',
                    'message': 'Cannot remove the last admin from a use case'
                })
        
        success = user_roles_dao.remove_user_from_usecase(user_id, usecase_id)
        
        if success:
            return create_response(200, {
                'message': 'User removed successfully',
                'user_id': user_id,
                'usecase_id': usecase_id
            })
        else:
            return create_response(500, {
                'error': 'Removal failed',
                'message': 'Failed to remove user from use case'
            })
        
    except Exception as e:
        logger.error(f"Error removing user from use case: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@admin_required('usecase_id')
def update_user_role(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Update a user's role for a specific use case."""
    try:
        user_id = event['pathParameters']['user_id']
        usecase_id = event['pathParameters']['usecase_id']
        user_context = event['user_context']
        
        body = json.loads(event['body'])
        new_role = body['role']
        
        # Validate role
        try:
            Role(new_role)
        except ValueError:
            return create_response(400, {
                'error': 'Invalid role',
                'message': f'Role must be one of: {[r.value for r in Role]}'
            })
        
        # Non-super users cannot assign PortalAdmin role
        if (new_role == Role.PORTAL_ADMIN.value and 
            not user_context.is_super_user):
            return create_response(403, {
                'error': 'Access denied',
                'message': 'Cannot assign PortalAdmin role'
            })
        
        success = user_roles_dao.update_user_role(
            user_id, usecase_id, new_role, user_context.user_id
        )
        
        if success:
            return create_response(200, {
                'message': 'User role updated successfully',
                'user_id': user_id,
                'usecase_id': usecase_id,
                'role': new_role
            })
        else:
            return create_response(500, {
                'error': 'Update failed',
                'message': 'Failed to update user role'
            })
        
    except json.JSONDecodeError:
        return create_response(400, {
            'error': 'Invalid JSON',
            'message': 'Request body must be valid JSON'
        })
    except KeyError as e:
        return create_response(400, {
            'error': 'Missing required field',
            'message': f'Required field missing: {str(e)}'
        })
    except Exception as e:
        logger.error(f"Error updating user role: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@admin_required('usecase_id')
def get_usecase_users(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Get all users assigned to a use case."""
    try:
        usecase_id = event['pathParameters']['usecase_id']
        
        users = user_roles_dao.get_usecase_users(usecase_id)
        
        return create_response(200, {
            'usecase_id': usecase_id,
            'users': users
        })
        
    except Exception as e:
        logger.error(f"Error getting use case users: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })

@super_user_required
def get_users_by_role(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Get all users with a specific role (super user only)."""
    try:
        role = event['pathParameters']['role']
        
        # Validate role
        try:
            Role(role)
        except ValueError:
            return create_response(400, {
                'error': 'Invalid role',
                'message': f'Role must be one of: {[r.value for r in Role]}'
            })
        
        users = user_roles_dao.get_users_by_role(role)
        
        return create_response(200, {
            'role': role,
            'users': users
        })
        
    except Exception as e:
        logger.error(f"Error getting users by role: {str(e)}")
        return create_response(500, {
            'error': 'Internal server error',
            'message': str(e)
        })