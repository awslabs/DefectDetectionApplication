"""
Role-Based Access Control (RBAC) utilities for the DDA Portal.

This module provides functions for checking user permissions and enforcing
access controls based on user roles and use case assignments.
"""

import json
import logging
from typing import Dict, List, Optional, Set
from enum import Enum
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

class Role(Enum):
    """User roles in the DDA Portal."""
    PORTAL_ADMIN = "PortalAdmin"
    USECASE_ADMIN = "UseCaseAdmin"
    DATA_SCIENTIST = "DataScientist"
    OPERATOR = "Operator"
    VIEWER = "Viewer"

class Permission(Enum):
    """Permissions for different actions in the system."""
    # Use case management
    CREATE_USECASE = "create_usecase"
    UPDATE_USECASE = "update_usecase"
    DELETE_USECASE = "delete_usecase"
    VIEW_USECASE = "view_usecase"
    
    # Labeling workflow
    CREATE_LABELING_JOB = "create_labeling_job"
    VIEW_LABELING_JOB = "view_labeling_job"
    DELETE_LABELING_JOB = "delete_labeling_job"
    
    # Training workflow
    CREATE_TRAINING_JOB = "create_training_job"
    VIEW_TRAINING_JOB = "view_training_job"
    DELETE_TRAINING_JOB = "delete_training_job"
    
    # Model registry
    VIEW_MODEL = "view_model"
    PROMOTE_MODEL = "promote_model"
    DELETE_MODEL = "delete_model"
    
    # Deployment management
    CREATE_DEPLOYMENT = "create_deployment"
    VIEW_DEPLOYMENT = "view_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    DELETE_DEPLOYMENT = "delete_deployment"
    
    # Device management
    VIEW_DEVICE = "view_device"
    RESTART_DEVICE = "restart_device"
    REBOOT_DEVICE = "reboot_device"
    BROWSE_DEVICE_FILES = "browse_device_files"
    DOWNLOAD_DEVICE_FILE = "download_device_file"
    VIEW_DEVICE_LOGS = "view_device_logs"
    UPDATE_DEVICE_CONFIG = "update_device_config"
    
    # Admin functions
    VIEW_AUDIT_LOGS = "view_audit_logs"
    MANAGE_USERS = "manage_users"
    MANAGE_SETTINGS = "manage_settings"

@dataclass
class UserContext:
    """User context containing identity and permissions."""
    user_id: str
    email: str
    roles: List[Role]
    assigned_usecases: Set[str]
    is_super_user: bool = False
    
    def __post_init__(self):
        """Set super user flag if user has PortalAdmin role."""
        self.is_super_user = Role.PORTAL_ADMIN in self.roles

# Role-based permissions mapping
ROLE_PERMISSIONS = {
    Role.PORTAL_ADMIN: {
        # Portal admins have all permissions
        Permission.CREATE_USECASE,
        Permission.UPDATE_USECASE,
        Permission.DELETE_USECASE,
        Permission.VIEW_USECASE,
        Permission.CREATE_LABELING_JOB,
        Permission.VIEW_LABELING_JOB,
        Permission.DELETE_LABELING_JOB,
        Permission.CREATE_TRAINING_JOB,
        Permission.VIEW_TRAINING_JOB,
        Permission.DELETE_TRAINING_JOB,
        Permission.VIEW_MODEL,
        Permission.PROMOTE_MODEL,
        Permission.DELETE_MODEL,
        Permission.CREATE_DEPLOYMENT,
        Permission.VIEW_DEPLOYMENT,
        Permission.ROLLBACK_DEPLOYMENT,
        Permission.DELETE_DEPLOYMENT,
        Permission.VIEW_DEVICE,
        Permission.RESTART_DEVICE,
        Permission.REBOOT_DEVICE,
        Permission.BROWSE_DEVICE_FILES,
        Permission.DOWNLOAD_DEVICE_FILE,
        Permission.VIEW_DEVICE_LOGS,
        Permission.UPDATE_DEVICE_CONFIG,
        Permission.VIEW_AUDIT_LOGS,
        Permission.MANAGE_USERS,
        Permission.MANAGE_SETTINGS,
    },
    Role.USECASE_ADMIN: {
        # Use case admins can manage everything within their assigned use cases
        Permission.VIEW_USECASE,
        Permission.UPDATE_USECASE,
        Permission.CREATE_LABELING_JOB,
        Permission.VIEW_LABELING_JOB,
        Permission.DELETE_LABELING_JOB,
        Permission.CREATE_TRAINING_JOB,
        Permission.VIEW_TRAINING_JOB,
        Permission.DELETE_TRAINING_JOB,
        Permission.VIEW_MODEL,
        Permission.PROMOTE_MODEL,
        Permission.DELETE_MODEL,
        Permission.CREATE_DEPLOYMENT,
        Permission.VIEW_DEPLOYMENT,
        Permission.ROLLBACK_DEPLOYMENT,
        Permission.DELETE_DEPLOYMENT,
        Permission.VIEW_DEVICE,
        Permission.RESTART_DEVICE,
        Permission.REBOOT_DEVICE,
        Permission.BROWSE_DEVICE_FILES,
        Permission.DOWNLOAD_DEVICE_FILE,
        Permission.VIEW_DEVICE_LOGS,
        Permission.UPDATE_DEVICE_CONFIG,
    },
    Role.DATA_SCIENTIST: {
        # Data scientists focus on labeling, training, and model management
        Permission.VIEW_USECASE,
        Permission.CREATE_LABELING_JOB,
        Permission.VIEW_LABELING_JOB,
        Permission.DELETE_LABELING_JOB,
        Permission.CREATE_TRAINING_JOB,
        Permission.VIEW_TRAINING_JOB,
        Permission.DELETE_TRAINING_JOB,
        Permission.VIEW_MODEL,
        Permission.PROMOTE_MODEL,
        Permission.VIEW_DEVICE,  # Read-only device access
    },
    Role.OPERATOR: {
        # Operators focus on deployments and device management
        Permission.VIEW_USECASE,
        Permission.VIEW_LABELING_JOB,
        Permission.VIEW_TRAINING_JOB,
        Permission.VIEW_MODEL,
        Permission.CREATE_DEPLOYMENT,
        Permission.VIEW_DEPLOYMENT,
        Permission.ROLLBACK_DEPLOYMENT,
        Permission.VIEW_DEVICE,
        Permission.RESTART_DEVICE,
        Permission.REBOOT_DEVICE,
        Permission.BROWSE_DEVICE_FILES,
        Permission.DOWNLOAD_DEVICE_FILE,
        Permission.VIEW_DEVICE_LOGS,
        Permission.UPDATE_DEVICE_CONFIG,
    },
    Role.VIEWER: {
        # Viewers have read-only access
        Permission.VIEW_USECASE,
        Permission.VIEW_LABELING_JOB,
        Permission.VIEW_TRAINING_JOB,
        Permission.VIEW_MODEL,
        Permission.VIEW_DEPLOYMENT,
        Permission.VIEW_DEVICE,
    },
}

class RBACManager:
    """Manages role-based access control for the DDA Portal."""
    
    def __init__(self, dynamodb_table_name: str):
        """Initialize RBAC manager with DynamoDB table name."""
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(dynamodb_table_name)
    
    def get_user_context(self, user_id: str, jwt_claims: Dict) -> UserContext:
        """
        Get user context including roles and use case assignments.
        
        Args:
            user_id: User identifier
            jwt_claims: JWT token claims containing user info
            
        Returns:
            UserContext with user's roles and permissions
        """
        try:
            # Extract roles from JWT claims (mapped from SSO groups)
            roles = self._extract_roles_from_jwt(jwt_claims)
            
            # Get use case assignments from DynamoDB
            assigned_usecases = self._get_user_usecases(user_id)
            
            return UserContext(
                user_id=user_id,
                email=jwt_claims.get('email', ''),
                roles=roles,
                assigned_usecases=assigned_usecases
            )
            
        except Exception as e:
            logger.error(f"Error getting user context for {user_id}: {str(e)}")
            # Return minimal context for error cases
            return UserContext(
                user_id=user_id,
                email=jwt_claims.get('email', ''),
                roles=[Role.VIEWER],
                assigned_usecases=set()
            )
    
    def _extract_roles_from_jwt(self, jwt_claims: Dict) -> List[Role]:
        """Extract user roles from JWT claims."""
        roles = []
        
        # Check custom:role attribute (single role)
        if 'custom:role' in jwt_claims:
            try:
                role = Role(jwt_claims['custom:role'])
                roles.append(role)
            except ValueError:
                logger.warning(f"Unknown role in JWT: {jwt_claims['custom:role']}")
        
        # Check custom:groups attribute (multiple groups mapped to roles)
        if 'custom:groups' in jwt_claims:
            groups = jwt_claims['custom:groups'].split(',')
            for group in groups:
                group = group.strip()
                role_mapping = {
                    'portal-admins': Role.PORTAL_ADMIN,
                    'cv-usecase-admins': Role.USECASE_ADMIN,
                    'cv-data-scientists': Role.DATA_SCIENTIST,
                    'cv-operators': Role.OPERATOR,
                    'cv-viewers': Role.VIEWER,
                }
                if group in role_mapping:
                    roles.append(role_mapping[group])
        
        # Default to Viewer if no roles found
        if not roles:
            roles = [Role.VIEWER]
            
        return roles
    
    def _get_user_usecases(self, user_id: str) -> Set[str]:
        """Get use cases assigned to a user from DynamoDB."""
        try:
            response = self.table.query(
                KeyConditionExpression='user_id = :user_id',
                ExpressionAttributeValues={':user_id': user_id}
            )
            
            return {item['usecase_id'] for item in response['Items']}
            
        except ClientError as e:
            logger.error(f"Error querying user use cases: {str(e)}")
            return set()
    
    def has_permission(self, user_context: UserContext, permission: Permission, 
                      usecase_id: Optional[str] = None) -> bool:
        """
        Check if user has a specific permission.
        
        Args:
            user_context: User context with roles and assignments
            permission: Permission to check
            usecase_id: Use case ID (required for use case-specific permissions)
            
        Returns:
            True if user has permission, False otherwise
        """
        # Super users (PortalAdmin) have access to everything
        if user_context.is_super_user:
            return True
        
        # Check if any of the user's roles have this permission
        has_role_permission = any(
            permission in ROLE_PERMISSIONS.get(role, set())
            for role in user_context.roles
        )
        
        if not has_role_permission:
            return False
        
        # For use case-specific permissions, check assignment
        if usecase_id and not self._is_usecase_specific_permission(permission):
            return True
        
        if usecase_id:
            return usecase_id in user_context.assigned_usecases
        
        return True
    
    def _is_usecase_specific_permission(self, permission: Permission) -> bool:
        """Check if a permission is use case-specific."""
        global_permissions = {
            Permission.CREATE_USECASE,
            Permission.VIEW_AUDIT_LOGS,
            Permission.MANAGE_USERS,
            Permission.MANAGE_SETTINGS,
        }
        return permission not in global_permissions
    
    def check_usecase_access(self, user_context: UserContext, usecase_id: str) -> bool:
        """
        Check if user has access to a specific use case.
        
        Args:
            user_context: User context
            usecase_id: Use case ID to check
            
        Returns:
            True if user has access, False otherwise
        """
        # Super users have access to all use cases
        if user_context.is_super_user:
            return True
        
        # Check if user is assigned to this use case
        return usecase_id in user_context.assigned_usecases
    
    def get_accessible_usecases(self, user_context: UserContext) -> Set[str]:
        """
        Get all use cases accessible to the user.
        
        Args:
            user_context: User context
            
        Returns:
            Set of use case IDs the user can access
        """
        if user_context.is_super_user:
            # Super users can access all use cases - return special marker
            return {'*'}  # Will be handled by calling code to fetch all
        
        return user_context.assigned_usecases

def require_permission(permission: Permission, usecase_id_param: Optional[str] = None):
    """
    Decorator to require a specific permission for a Lambda function.
    
    Args:
        permission: Required permission
        usecase_id_param: Parameter name containing use case ID (optional)
    """
    def decorator(func):
        def wrapper(event, context):
            # Extract user context from event
            user_context = event.get('user_context')
            if not user_context:
                return {
                    'statusCode': 401,
                    'body': json.dumps({'error': 'Authentication required'})
                }
            
            # Get use case ID if specified
            usecase_id = None
            if usecase_id_param:
                if usecase_id_param in event.get('pathParameters', {}):
                    usecase_id = event['pathParameters'][usecase_id_param]
                elif usecase_id_param in event.get('queryStringParameters', {}):
                    usecase_id = event['queryStringParameters'][usecase_id_param]
                elif usecase_id_param in json.loads(event.get('body', '{}')):
                    usecase_id = json.loads(event['body'])[usecase_id_param]
            
            # Initialize RBAC manager
            rbac = RBACManager(os.environ['USER_ROLES_TABLE'])
            
            # Check permission
            if not rbac.has_permission(user_context, permission, usecase_id):
                return {
                    'statusCode': 403,
                    'body': json.dumps({
                        'error': 'Insufficient permissions',
                        'required_permission': permission.value,
                        'usecase_id': usecase_id
                    })
                }
            
            # Call the original function
            return func(event, context)
        
        return wrapper
    return decorator

def extract_user_context_from_jwt(event: Dict) -> Optional[UserContext]:
    """
    Extract user context from JWT claims in API Gateway event.
    
    Args:
        event: API Gateway event
        
    Returns:
        UserContext if successful, None otherwise
    """
    try:
        # Get JWT claims from authorizer context
        authorizer_context = event.get('requestContext', {}).get('authorizer', {})
        
        user_id = authorizer_context.get('sub') or authorizer_context.get('userId')
        if not user_id:
            return None
        
        # Initialize RBAC manager
        rbac = RBACManager(os.environ.get('USER_ROLES_TABLE', ''))
        
        # Create user context from JWT claims
        return rbac.get_user_context(user_id, authorizer_context)
        
    except Exception as e:
        logger.error(f"Error extracting user context: {str(e)}")
        return None