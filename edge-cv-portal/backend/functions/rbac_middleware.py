"""
RBAC Middleware for Edge CV Portal Lambda Functions
Provides decorators and utilities for role-based access control
"""
import json
import logging
from functools import wraps
from typing import Dict, Any, List, Optional
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    rbac_manager, Role, Permission
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def extract_usecase_id_from_event(event: Dict, usecase_param: str = 'usecase_id') -> Optional[str]:
    """
    Extract use case ID from various parts of the API Gateway event
    
    Args:
        event: API Gateway event
        usecase_param: Parameter name containing the use case ID
        
    Returns:
        Use case ID if found, None otherwise
    """
    # Try path parameters first
    path_params = event.get('pathParameters', {}) or {}
    if usecase_param in path_params:
        return path_params[usecase_param]
    
    # Try query parameters
    query_params = event.get('queryStringParameters', {}) or {}
    if usecase_param in query_params:
        return query_params[usecase_param]
    
    # Try request body
    try:
        body = json.loads(event.get('body', '{}'))
        if usecase_param in body:
            return body[usecase_param]
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Try resource path for nested resources
    resource_path = event.get('resource', '')
    if '/usecases/{usecase_id}' in resource_path:
        path_params = event.get('pathParameters', {}) or {}
        return path_params.get('usecase_id')
    
    return None


def rbac_check(required_permissions: List[Permission], 
               usecase_param: str = 'usecase_id',
               allow_global: bool = False):
    """
    Decorator for RBAC permission checking
    
    Args:
        required_permissions: List of required permissions
        usecase_param: Parameter name containing the use case ID
        allow_global: Whether to allow global permissions for endpoints that don't require specific use case
    """
    def decorator(func):
        @wraps(func)
        def wrapper(event, context):
            try:
                # Extract user information
                user = get_user_from_event(event)
                user_id = user['user_id']
                
                # Extract use case ID
                usecase_id = extract_usecase_id_from_event(event, usecase_param)
                
                # For endpoints that don't require a specific use case
                if not usecase_id and allow_global:
                    usecase_id = 'global'
                
                if not usecase_id:
                    return create_response(400, {
                        'error': 'Use case ID required',
                        'parameter': usecase_param
                    })
                
                # Check if user has any of the required permissions.
                # Thread the JWT-derived user dict as user_info so the
                # Cognito custom:role claim reaches role resolution. This
                # matters most at the 'global' scope (allow_global routes,
                # e.g. build-fleet endpoints): there is no per-usecase
                # UserRoles row for 'global', so without user_info the role
                # defaulted to Viewer and JWT PortalAdmins got 403s.
                # Known residual gap (by design of the resolution order in
                # shared_utils.RBACManager.get_user_role): per-usecase-only
                # users (no JWT role, only per-usecase UserRoles rows) still
                # resolve to Viewer at 'global' scope — acceptable for now.
                has_permission = False
                for permission in required_permissions:
                    if rbac_manager.has_permission(user_id, usecase_id, permission,
                                                   user_info=user):
                        has_permission = True
                        break
                
                if not has_permission:
                    # Log unauthorized access attempt
                    log_audit_event(
                        user_id=user_id,
                        action='unauthorized_access',
                        resource_type='api_endpoint',
                        resource_id=event.get('resource', 'unknown'),
                        result='denied',
                        details={
                            'required_permissions': [p.value for p in required_permissions],
                            'usecase_id': usecase_id,
                            'user_role': rbac_manager.get_user_role(user_id, usecase_id, user_info=user).value if rbac_manager.get_user_role(user_id, usecase_id, user_info=user) else 'none',
                            'method': event.get('httpMethod'),
                            'path': event.get('path')
                        }
                    )
                    
                    return create_response(403, {
                        'error': 'Insufficient permissions',
                        'required_permissions': [p.value for p in required_permissions],
                        'usecase_id': usecase_id
                    })
                
                # Add RBAC context to event for use in the handler
                if 'rbac_context' not in event:
                    event['rbac_context'] = {}
                
                event['rbac_context'].update({
                    'user_id': user_id,
                    'usecase_id': usecase_id,
                    'user_role': rbac_manager.get_user_role(user_id, usecase_id, user_info=user),
                    'permissions': rbac_manager.get_user_permissions(user_id, usecase_id, user_info=user),
                    'is_super_user': rbac_manager.is_portal_admin(user_id, user_info=user)
                })
                
                # Call the original function
                return func(event, context)
                
            except Exception as e:
                logger.error(f"Error in RBAC check: {str(e)}", exc_info=True)
                return create_response(500, {'error': 'Authorization check failed'})
        
        return wrapper
    return decorator


def super_user_only(func):
    """
    Decorator to require PortalAdmin (super user) access
    """
    @wraps(func)
    def wrapper(event, context):
        try:
            user = get_user_from_event(event)
            user_id = user['user_id']
            
            # Pass user_info so the JWT custom:role claim (PortalAdmin)
            # reaches role resolution (same gap as rbac_check above).
            if not rbac_manager.is_portal_admin(user_id, user_info=user):
                log_audit_event(
                    user_id=user_id,
                    action='unauthorized_super_user_access',
                    resource_type='api_endpoint',
                    resource_id=event.get('resource', 'unknown'),
                    result='denied',
                    details={
                        'user_role': rbac_manager.get_user_role(user_id, 'global', user_info=user).value if rbac_manager.get_user_role(user_id, 'global', user_info=user) else 'none',
                        'method': event.get('httpMethod'),
                        'path': event.get('path')
                    }
                )
                
                return create_response(403, {
                    'error': 'Super user access required',
                    'required_role': 'PortalAdmin'
                })
            
            # Add RBAC context
            if 'rbac_context' not in event:
                event['rbac_context'] = {}
            
            event['rbac_context'].update({
                'user_id': user_id,
                'is_super_user': True,
                'user_role': Role.PORTAL_ADMIN
            })
            
            return func(event, context)
            
        except Exception as e:
            logger.error(f"Error in super user check: {str(e)}", exc_info=True)
            return create_response(500, {'error': 'Authorization check failed'})
    
    return wrapper


def get_rbac_context(event: Dict) -> Dict[str, Any]:
    """
    Get RBAC context from event (populated by decorators)
    
    Args:
        event: API Gateway event
        
    Returns:
        RBAC context dictionary
    """
    return event.get('rbac_context', {})


def validate_usecase_access_middleware(event: Dict) -> Dict[str, Any]:
    """
    Middleware function to validate use case access and return context
    
    Args:
        event: API Gateway event
        
    Returns:
        Validation result with user context
    """
    try:
        user = get_user_from_event(event)
        user_id = user['user_id']
        
        usecase_id = extract_usecase_id_from_event(event)
        if not usecase_id:
            return {
                'valid': False,
                'error': 'Use case ID required',
                'error_code': 'MISSING_USECASE_ID'
            }
        
        # Check if user has access to the use case
        user_role = rbac_manager.get_user_role(user_id, usecase_id)
        
        if not user_role:
            return {
                'valid': False,
                'error': 'Access denied: User not assigned to use case',
                'error_code': 'NO_ACCESS'
            }
        
        # Get use case details
        from shared_utils import get_usecase
        try:
            usecase = get_usecase(usecase_id)
        except ValueError:
            return {
                'valid': False,
                'error': 'Use case not found',
                'error_code': 'USECASE_NOT_FOUND'
            }
        
        return {
            'valid': True,
            'user_id': user_id,
            'usecase_id': usecase_id,
            'user_role': user_role.value,
            'is_super_user': user_role == Role.PORTAL_ADMIN,
            'usecase': usecase,
            'permissions': [p.value for p in rbac_manager.get_user_permissions(user_id, usecase_id)]
        }
        
    except Exception as e:
        logger.error(f"Error validating use case access: {str(e)}")
        return {
            'valid': False,
            'error': 'Access validation failed',
            'error_code': 'VALIDATION_ERROR'
        }


# Common permission sets for different types of operations
class CommonPermissions:
    """Common permission sets for different operation types"""
    
    # Use Case Management
    VIEW_USECASES = [Permission.VIEW_USECASES]
    MANAGE_USECASES = [Permission.CREATE_USECASES, Permission.UPDATE_USECASES, Permission.DELETE_USECASES]
    
    # Data Operations
    VIEW_DATA = [Permission.VIEW_DATASETS, Permission.VIEW_LABELING_JOBS]
    MANAGE_DATA = [Permission.UPLOAD_DATASETS, Permission.MANAGE_DATASETS, Permission.CREATE_LABELING_JOBS, Permission.MANAGE_LABELING_JOBS]
    
    # Training Operations
    VIEW_TRAINING = [Permission.VIEW_TRAINING_JOBS, Permission.VIEW_MODELS]
    MANAGE_TRAINING = [Permission.CREATE_TRAINING_JOBS, Permission.MANAGE_TRAINING_JOBS, Permission.PROMOTE_MODELS]
    
    # Deployment Operations
    VIEW_DEPLOYMENTS = [Permission.VIEW_DEPLOYMENTS, Permission.VIEW_DEVICES]
    MANAGE_DEPLOYMENTS = [Permission.CREATE_DEPLOYMENTS, Permission.MANAGE_DEPLOYMENTS, Permission.MANAGE_DEVICES]
    
    # Device Operations
    VIEW_DEVICES = [Permission.VIEW_DEVICES, Permission.VIEW_DEVICE_LOGS]
    CONTROL_DEVICES = [Permission.RESTART_DEVICES, Permission.REBOOT_DEVICES, Permission.UPDATE_DEVICE_CONFIG]
    
    # Administrative Operations
    ADMIN_OPERATIONS = [Permission.MANAGE_USERS, Permission.MANAGE_SETTINGS, Permission.VIEW_AUDIT_LOGS]

    # Workflow Manager Operations
    # Role mapping (see shared_utils.RBACManager):
    #   workflow:read                          -> Viewer, Operator, DataScientist, UseCaseAdmin
    #   workflow:create/edit/save/delete/test  -> DataScientist, UseCaseAdmin
    #   workflow:package/deploy                -> Operator, UseCaseAdmin
    #   bedrock-config:write                   -> PortalAdmin only
    VIEW_WORKFLOWS = [Permission.WORKFLOW_READ]
    EDIT_WORKFLOWS = [
        Permission.WORKFLOW_CREATE,
        Permission.WORKFLOW_EDIT,
        Permission.WORKFLOW_SAVE,
        Permission.WORKFLOW_DELETE,
    ]
    TEST_WORKFLOWS = [Permission.WORKFLOW_TEST]
    DEPLOY_WORKFLOWS = [Permission.WORKFLOW_PACKAGE, Permission.WORKFLOW_DEPLOY]
    MANAGE_BEDROCK_CONFIG = [Permission.BEDROCK_CONFIG_WRITE]

    # Node Designer Operations (custom-node-designer, Requirement 13)
    # Role mapping (see shared_utils.RBACManager):
    #   node-designer:read              -> Viewer, Operator, DataScientist,
    #                                      UseCaseAdmin, PortalAdmin (13.3)
    #   node-designer:create/generate/import/simulate/register/
    #                 promote-demote/manage
    #                                   -> UseCaseAdmin (own Use_Case),
    #                                      PortalAdmin (13.1)
    #   node-designer:security-review   -> PortalAdmin only (13.2)
    # Denials return the standard authorization error envelope (13.4).
    VIEW_NODE_DESIGNER = [Permission.NODE_DESIGNER_READ]
    MANAGE_NODE_DESIGNER = [
        Permission.NODE_DESIGNER_CREATE,
        Permission.NODE_DESIGNER_GENERATE,
        Permission.NODE_DESIGNER_IMPORT,
        Permission.NODE_DESIGNER_SIMULATE,
        Permission.NODE_DESIGNER_REGISTER,
        Permission.NODE_DESIGNER_PROMOTE_DEMOTE,
        Permission.NODE_DESIGNER_MANAGE,
    ]
    REVIEW_NODE_DESIGNER = [Permission.NODE_DESIGNER_SECURITY_REVIEW]

    # Build Fleet Operations (portal-build-fleet-and-workflow-gates)
    # Role mapping (see shared_utils.RBACManager):
    #   builds:submit/cancel/read -> DataScientist, UseCaseAdmin, PortalAdmin
    #                                (the Build_Operator capability)
    # Builds are not Use_Case-scoped: check these permissions with
    # rbac_check(..., allow_global=True) so the scope resolves to
    # 'global'. Denials return the standard authorization error and
    # record a denied-access Audit_Log entry (Req 1.6, 4.10, 9.6).
    SUBMIT_BUILDS = [Permission.BUILDS_SUBMIT]
    CANCEL_BUILDS = [Permission.BUILDS_CANCEL]
    VIEW_BUILDS = [Permission.BUILDS_READ]


# Convenience decorators for common permission patterns
def require_data_scientist_or_admin(usecase_param: str = 'usecase_id'):
    """Require DataScientist, UseCaseAdmin, or PortalAdmin role"""
    return rbac_check([Permission.CREATE_TRAINING_JOBS, Permission.MANAGE_TRAINING_JOBS], usecase_param)


def require_operator_or_admin(usecase_param: str = 'usecase_id'):
    """Require Operator, UseCaseAdmin, or PortalAdmin role"""
    return rbac_check([Permission.CREATE_DEPLOYMENTS, Permission.MANAGE_DEPLOYMENTS], usecase_param)


def require_usecase_admin_or_portal_admin(usecase_param: str = 'usecase_id'):
    """Require UseCaseAdmin or PortalAdmin role"""
    return rbac_check([Permission.UPDATE_USECASES, Permission.MANAGE_USECASE_USERS], usecase_param)


def require_view_access(usecase_param: str = 'usecase_id'):
    """Require any role with view access"""
    return rbac_check([Permission.VIEW_USECASES], usecase_param)


def require_workflow_read(usecase_param: str = 'usecase_id'):
    """Require workflow:read (Viewer, Operator, DataScientist, UseCaseAdmin, PortalAdmin)"""
    return rbac_check(CommonPermissions.VIEW_WORKFLOWS, usecase_param)


def require_workflow_edit(usecase_param: str = 'usecase_id'):
    """Require workflow create/edit/save/delete (DataScientist, UseCaseAdmin, PortalAdmin)"""
    return rbac_check(CommonPermissions.EDIT_WORKFLOWS, usecase_param)


def require_workflow_test(usecase_param: str = 'usecase_id'):
    """Require workflow:test (DataScientist, UseCaseAdmin, PortalAdmin)"""
    return rbac_check(CommonPermissions.TEST_WORKFLOWS, usecase_param)


def require_workflow_deploy(usecase_param: str = 'usecase_id'):
    """Require workflow package/deploy (Operator, UseCaseAdmin, PortalAdmin)"""
    return rbac_check(CommonPermissions.DEPLOY_WORKFLOWS, usecase_param)


def require_node_designer_read(usecase_param: str = 'usecase_id'):
    """Require node-designer:read (every role of the Use_Case, 13.3)"""
    return rbac_check(CommonPermissions.VIEW_NODE_DESIGNER, usecase_param)


def require_node_designer_manage(usecase_param: str = 'usecase_id'):
    """Require node-designer create/generate/import/simulate/register/
    promote-demote/manage (UseCaseAdmin within own Use_Case, PortalAdmin, 13.1)"""
    return rbac_check(CommonPermissions.MANAGE_NODE_DESIGNER, usecase_param)


def require_node_designer_security_review(usecase_param: str = 'usecase_id'):
    """Require node-designer:security-review (PortalAdmin only, 13.2)"""
    return rbac_check(CommonPermissions.REVIEW_NODE_DESIGNER, usecase_param)


def require_builds_submit():
    """Require builds:submit (DataScientist, UseCaseAdmin, PortalAdmin).

    Global scope: builds are not Use_Case-scoped (Req 1.6)."""
    return rbac_check(CommonPermissions.SUBMIT_BUILDS, allow_global=True)


def require_builds_cancel():
    """Require builds:cancel (DataScientist, UseCaseAdmin, PortalAdmin).

    Global scope: builds are not Use_Case-scoped (Req 4.10)."""
    return rbac_check(CommonPermissions.CANCEL_BUILDS, allow_global=True)


def require_builds_read():
    """Require builds:read (DataScientist, UseCaseAdmin, PortalAdmin).

    Global scope: builds are not Use_Case-scoped. Read is restricted to
    Build_Operators too (build logs may contain account identifiers)."""
    return rbac_check(CommonPermissions.VIEW_BUILDS, allow_global=True)