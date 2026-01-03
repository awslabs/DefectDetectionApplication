"""
RBAC Initialization Script for Edge CV Portal
Sets up default roles and permissions in DynamoDB
"""
import json
import logging
import os
import boto3
from datetime import datetime
from shared_utils import rbac_manager, Role

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Initialize RBAC system with default roles and permissions
    This is typically called during deployment or setup
    """
    try:
        logger.info("Initializing RBAC system...")
        
        # Initialize default PortalAdmin user if provided
        admin_user_id = event.get('admin_user_id')
        admin_email = event.get('admin_email')
        
        if admin_user_id:
            success = setup_default_admin(admin_user_id, admin_email)
            if success:
                logger.info(f"Default admin user setup completed: {admin_user_id}")
            else:
                logger.error("Failed to setup default admin user")
        
        # Create role permissions documentation
        role_permissions_doc = generate_role_permissions_documentation()
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'RBAC system initialized successfully',
                'admin_user_setup': bool(admin_user_id),
                'role_permissions': role_permissions_doc
            })
        }
        
    except Exception as e:
        logger.error(f"Error initializing RBAC system: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Failed to initialize RBAC system'})
        }


def setup_default_admin(user_id: str, email: str = None) -> bool:
    """
    Set up a default PortalAdmin user
    
    Args:
        user_id: User identifier
        email: User email (optional)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Assign PortalAdmin role to the user
        success = rbac_manager.assign_user_role(
            user_id=user_id,
            usecase_id='global',
            role=Role.PORTAL_ADMIN,
            assigned_by='system'
        )
        
        if success:
            logger.info(f"Successfully assigned PortalAdmin role to {user_id}")
            
            # Log the system initialization
            from shared_utils import log_audit_event
            log_audit_event(
                user_id='system',
                action='initialize_admin',
                resource_type='user_role',
                resource_id=f"{user_id}:global",
                result='success',
                details={
                    'admin_user_id': user_id,
                    'admin_email': email,
                    'role': Role.PORTAL_ADMIN.value
                }
            )
            
            return True
        else:
            logger.error(f"Failed to assign PortalAdmin role to {user_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error setting up default admin: {str(e)}")
        return False


def generate_role_permissions_documentation() -> dict:
    """
    Generate documentation of role permissions for reference
    
    Returns:
        Dictionary mapping roles to their permissions
    """
    role_permissions = {}
    
    for role in Role:
        permissions = rbac_manager.role_permissions.get(role, set())
        role_permissions[role.value] = {
            'permissions': [p.value for p in permissions],
            'permission_count': len(permissions),
            'description': get_role_description(role)
        }
    
    return role_permissions


def get_role_description(role: Role) -> str:
    """Get human-readable description for a role"""
    descriptions = {
        Role.VIEWER: "Read-only access to assigned use cases. Can view jobs, models, devices, and logs.",
        Role.OPERATOR: "Device and deployment management. Can deploy models, manage devices, restart services.",
        Role.DATA_SCIENTIST: "Data and model management. Can create labeling jobs, train models, manage datasets.",
        Role.USECASE_ADMIN: "Full management of assigned use cases. Can manage users, update settings, perform all operations within use case.",
        Role.PORTAL_ADMIN: "System administrator with access to all use cases and system settings. Can create use cases, manage all users."
    }
    return descriptions.get(role, "Unknown role")


def validate_rbac_setup() -> dict:
    """
    Validate that RBAC system is properly configured
    
    Returns:
        Validation results
    """
    try:
        validation_results = {
            'valid': True,
            'issues': [],
            'warnings': [],
            'role_count': len(Role),
            'permission_count': len(Permission)
        }
        
        # Check that all roles have permissions defined
        for role in Role:
            permissions = rbac_manager.role_permissions.get(role)
            if not permissions:
                validation_results['issues'].append(f"Role {role.value} has no permissions defined")
                validation_results['valid'] = False
            elif len(permissions) == 0:
                validation_results['warnings'].append(f"Role {role.value} has empty permission set")
        
        # Check role hierarchy
        role_hierarchy = [Role.VIEWER, Role.OPERATOR, Role.DATA_SCIENTIST, Role.USECASE_ADMIN, Role.PORTAL_ADMIN]
        for i, role in enumerate(role_hierarchy[:-1]):  # Exclude PortalAdmin from this check
            current_permissions = rbac_manager.role_permissions.get(role, set())
            next_role = role_hierarchy[i + 1]
            next_permissions = rbac_manager.role_permissions.get(next_role, set())
            
            # Higher roles should have at least the same permissions as lower roles
            if not current_permissions.issubset(next_permissions):
                validation_results['warnings'].append(
                    f"Role hierarchy issue: {next_role.value} doesn't include all {role.value} permissions"
                )
        
        # Check for PortalAdmin having all permissions
        portal_admin_permissions = rbac_manager.role_permissions.get(Role.PORTAL_ADMIN, set())
        all_permissions = set(Permission)
        if portal_admin_permissions != all_permissions:
            validation_results['issues'].append("PortalAdmin role doesn't have all permissions")
            validation_results['valid'] = False
        
        return validation_results
        
    except Exception as e:
        logger.error(f"Error validating RBAC setup: {str(e)}")
        return {
            'valid': False,
            'error': str(e),
            'issues': ['Validation failed due to exception']
        }


if __name__ == '__main__':
    # For local testing
    test_event = {
        'admin_user_id': 'test-admin-user',
        'admin_email': 'admin@example.com'
    }
    
    result = handler(test_event, None)
    print(json.dumps(result, indent=2))