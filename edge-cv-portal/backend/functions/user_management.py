"""
User Management handler for Edge CV Portal
Handles user role assignments and RBAC operations
"""
import json
import logging
from typing import Dict, Any, List
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    rbac_manager, Role, Permission, require_permission, require_super_user,
    validate_required_fields
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Handle user management requests
    
    GET /api/v1/users - List users and their roles
    POST /api/v1/users/assign-role - Assign role to user
    DELETE /api/v1/users/{user_id}/roles/{usecase_id} - Remove user role
    GET /api/v1/users/{user_id}/permissions - Get user permissions
    GET /api/v1/users/me/usecases - Get accessible use cases for current user
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        
        logger.info(f"User management request: {http_method} {path}")
        
        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return create_response(200, '', {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                'Access-Control-Max-Age': '86400'
            })
        
        if http_method == 'GET' and path.endswith('/users'):
            return list_users(event)
        elif http_method == 'POST' and path.endswith('/assign-role'):
            return assign_user_role(event)
        elif http_method == 'DELETE' and '/roles/' in path:
            return remove_user_role(event)
        elif http_method == 'GET' and '/permissions' in path:
            return get_user_permissions(event)
        elif http_method == 'GET' and path.endswith('/me/usecases'):
            return get_my_usecases(event)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in user management handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_users(event):
    """
    List all users and their role assignments
    GET /api/v1/users?usecase_id=xxx
    
    - PortalAdmins can list all users or filter by usecase_id
    - UseCaseAdmins can only list users for their assigned usecases
    """
    try:
        current_user = get_user_from_event(event)
        query_params = event.get('queryStringParameters', {}) or {}
        usecase_id = query_params.get('usecase_id')
        
        # Get all user role assignments
        import boto3
        import os
        dynamodb = boto3.resource('dynamodb')
        user_roles_table = dynamodb.Table(os.environ.get('USER_ROLES_TABLE'))
        
        # Check permissions
        is_portal_admin = rbac_manager.is_portal_admin(current_user['user_id'], current_user)
        
        if not is_portal_admin:
            # Non-portal admins must specify a usecase_id
            if not usecase_id:
                return create_response(403, {
                    'error': 'Access denied',
                    'message': 'You must specify a usecase_id to list users'
                })
            
            # Check if user has admin access to this usecase
            user_role = rbac_manager.get_user_role(current_user['user_id'], usecase_id, current_user)
            if user_role not in [Role.USECASE_ADMIN, Role.PORTAL_ADMIN]:
                return create_response(403, {
                    'error': 'Access denied',
                    'message': 'You must be a UseCaseAdmin to view team members'
                })
        
        if usecase_id:
            # Get users for specific use case
            response = user_roles_table.query(
                IndexName='usecase-users-index',
                KeyConditionExpression='usecase_id = :usecase_id',
                ExpressionAttributeValues={':usecase_id': usecase_id}
            )
        else:
            # Get all user assignments (PortalAdmin only)
            response = user_roles_table.scan()
        
        # Group by user
        users = {}
        for item in response.get('Items', []):
            user_id = item['user_id']
            if user_id not in users:
                users[user_id] = {
                    'user_id': user_id,
                    'roles': []
                }
            
            users[user_id]['roles'].append({
                'usecase_id': item['usecase_id'],
                'role': item['role'],
                'assigned_at': item.get('assigned_at'),
                'assigned_by': item.get('assigned_by')
            })
        
        return create_response(200, {
            'users': list(users.values()),
            'total_count': len(users)
        })
        
    except Exception as e:
        logger.error(f"Error listing users: {str(e)}")
        return create_response(500, {'error': 'Failed to list users'})


@require_permission(Permission.MANAGE_USERS)
def assign_user_role(event):
    """
    Assign a role to a user for a specific use case
    POST /api/v1/users/assign-role
    Body: {
        "user_id": "user123",
        "usecase_id": "usecase456", 
        "role": "DataScientist"
    }
    """
    try:
        current_user = get_user_from_event(event)
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        error = validate_required_fields(body, ['user_id', 'usecase_id', 'role'])
        if error:
            return create_response(400, {'error': error})
        
        user_id = body['user_id']
        usecase_id = body['usecase_id']
        role_str = body['role']
        
        # Validate role
        try:
            role = Role(role_str)
        except ValueError:
            return create_response(400, {'error': f'Invalid role: {role_str}'})
        
        # Check if current user can assign this role
        current_user_role = rbac_manager.get_user_role(current_user['user_id'], usecase_id, current_user)
        
        # Only PortalAdmin can assign PortalAdmin role
        if role == Role.PORTAL_ADMIN and current_user_role != Role.PORTAL_ADMIN:
            return create_response(403, {'error': 'Only PortalAdmin can assign PortalAdmin role'})
        
        # UseCaseAdmin can only assign roles within their use case (except PortalAdmin)
        if current_user_role == Role.USECASE_ADMIN and role == Role.PORTAL_ADMIN:
            return create_response(403, {'error': 'UseCaseAdmin cannot assign PortalAdmin role'})
        
        # Assign the role
        success = rbac_manager.assign_user_role(
            user_id=user_id,
            usecase_id=usecase_id,
            role=role,
            assigned_by=current_user['user_id']
        )
        
        if success:
            return create_response(200, {
                'message': f'Successfully assigned {role.value} role to user {user_id}',
                'user_id': user_id,
                'usecase_id': usecase_id,
                'role': role.value
            })
        else:
            return create_response(500, {'error': 'Failed to assign role'})
        
    except json.JSONDecodeError:
        return create_response(400, {'error': 'Invalid JSON in request body'})
    except Exception as e:
        logger.error(f"Error assigning user role: {str(e)}")
        return create_response(500, {'error': 'Failed to assign role'})


@require_permission(Permission.MANAGE_USERS)
def remove_user_role(event):
    """
    Remove a user's role for a specific use case
    DELETE /api/v1/users/{user_id}/roles/{usecase_id}
    """
    try:
        current_user = get_user_from_event(event)
        path_params = event.get('pathParameters', {})
        
        user_id = path_params.get('user_id')
        usecase_id = path_params.get('usecase_id')
        
        if not user_id or not usecase_id:
            return create_response(400, {'error': 'user_id and usecase_id are required'})
        
        # Check if current user can remove this role
        current_user_role = rbac_manager.get_user_role(current_user['user_id'], usecase_id, current_user)
        target_user_role = rbac_manager.get_user_role(user_id, usecase_id, current_user)
        
        # Only PortalAdmin can remove PortalAdmin role
        if target_user_role == Role.PORTAL_ADMIN and current_user_role != Role.PORTAL_ADMIN:
            return create_response(403, {'error': 'Only PortalAdmin can remove PortalAdmin role'})
        
        # Remove the role
        success = rbac_manager.remove_user_role(
            user_id=user_id,
            usecase_id=usecase_id,
            removed_by=current_user['user_id']
        )
        
        if success:
            return create_response(200, {
                'message': f'Successfully removed role for user {user_id}',
                'user_id': user_id,
                'usecase_id': usecase_id
            })
        else:
            return create_response(500, {'error': 'Failed to remove role'})
        
    except Exception as e:
        logger.error(f"Error removing user role: {str(e)}")
        return create_response(500, {'error': 'Failed to remove role'})


def get_user_permissions(event):
    """
    Get permissions for a specific user
    GET /api/v1/users/{user_id}/permissions?usecase_id=xxx
    """
    try:
        current_user = get_user_from_event(event)
        path_params = event.get('pathParameters', {})
        query_params = event.get('queryStringParameters', {}) or {}
        
        target_user_id = path_params.get('user_id')
        usecase_id = query_params.get('usecase_id', 'global')
        
        if not target_user_id:
            return create_response(400, {'error': 'user_id is required'})
        
        # Users can only view their own permissions unless they're admin
        if (target_user_id != current_user['user_id'] and 
            not rbac_manager.has_permission(current_user['user_id'], usecase_id, Permission.MANAGE_USERS)):
            return create_response(403, {'error': 'Cannot view other user permissions'})
        
        # Get user role and permissions
        user_role = rbac_manager.get_user_role(target_user_id, usecase_id, current_user)
        user_permissions = rbac_manager.get_user_permissions(target_user_id, usecase_id)
        
        return create_response(200, {
            'user_id': target_user_id,
            'usecase_id': usecase_id,
            'role': user_role.value if user_role else None,
            'permissions': [p.value for p in user_permissions],
            'is_super_user': user_role == Role.PORTAL_ADMIN if user_role else False
        })
        
    except Exception as e:
        logger.error(f"Error getting user permissions: {str(e)}")
        return create_response(500, {'error': 'Failed to get user permissions'})


def get_my_usecases(event):
    """
    Get accessible use cases for the current user
    GET /api/v1/users/me/usecases
    """
    try:
        current_user = get_user_from_event(event)
        user_id = current_user['user_id']
        
        # Get accessible use cases
        accessible_usecases = rbac_manager.get_accessible_usecases(user_id)
        
        # Get detailed information for each use case
        import boto3
        import os
        dynamodb = boto3.resource('dynamodb')
        usecases_table = dynamodb.Table(os.environ.get('USECASES_TABLE'))
        
        usecase_details = []
        for usecase_id in accessible_usecases:
            try:
                response = usecases_table.get_item(Key={'usecase_id': usecase_id})
                if 'Item' in response:
                    usecase = response['Item']
                    user_role = rbac_manager.get_user_role(user_id, usecase_id, current_user)
                    
                    usecase_details.append({
                        'usecase_id': usecase_id,
                        'name': usecase.get('name'),
                        'description': usecase.get('description'),
                        'owner': usecase.get('owner'),
                        'user_role': user_role.value if user_role else None,
                        'created_at': usecase.get('created_at'),
                        'account_id': usecase.get('account_id')
                    })
            except Exception as e:
                logger.warning(f"Error getting details for use case {usecase_id}: {str(e)}")
        
        return create_response(200, {
            'usecases': usecase_details,
            'total_count': len(usecase_details),
            'is_super_user': rbac_manager.is_portal_admin(user_id, current_user)
        })
        
    except Exception as e:
        logger.error(f"Error getting user's use cases: {str(e)}")
        return create_response(500, {'error': 'Failed to get accessible use cases'})


def get_role_permissions_mapping():
    """
    Get the mapping of roles to permissions for frontend reference
    GET /api/v1/users/role-permissions
    """
    try:
        role_permissions = {}
        
        for role in Role:
            permissions = rbac_manager.role_permissions.get(role, set())
            role_permissions[role.value] = [p.value for p in permissions]
        
        return create_response(200, {
            'role_permissions': role_permissions,
            'available_roles': [role.value for role in Role],
            'available_permissions': [permission.value for permission in Permission]
        })
        
    except Exception as e:
        logger.error(f"Error getting role permissions mapping: {str(e)}")
        return create_response(500, {'error': 'Failed to get role permissions mapping'})