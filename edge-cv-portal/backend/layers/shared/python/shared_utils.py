"""
Shared utilities for Edge CV Portal Lambda functions
"""
import json
import os
import logging
from typing import Dict, Any, Optional, List, Set
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError
from enum import Enum
from functools import wraps

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# DynamoDB client
dynamodb = boto3.resource('dynamodb')

# Table names from environment
USECASES_TABLE = os.environ.get('USECASES_TABLE')
USER_ROLES_TABLE = os.environ.get('USER_ROLES_TABLE')
DEVICES_TABLE = os.environ.get('DEVICES_TABLE')
AUDIT_LOG_TABLE = os.environ.get('AUDIT_LOG_TABLE')
TRAINING_JOBS_TABLE = os.environ.get('TRAINING_JOBS_TABLE')
LABELING_JOBS_TABLE = os.environ.get('LABELING_JOBS_TABLE')
PRE_LABELED_DATASETS_TABLE = os.environ.get('PRE_LABELED_DATASETS_TABLE')
MODELS_TABLE = os.environ.get('MODELS_TABLE')
DEPLOYMENTS_TABLE = os.environ.get('DEPLOYMENTS_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')
COMPONENTS_TABLE = os.environ.get('COMPONENTS_TABLE')


def decimal_default(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def create_response(status_code: int, body: Any, headers: Optional[Dict] = None) -> Dict:
    """Create API Gateway response"""
    default_headers = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
    }
    
    if headers:
        default_headers.update(headers)
    
    return {
        'statusCode': status_code,
        'headers': default_headers,
        'body': json.dumps(body, default=decimal_default) if not isinstance(body, str) else body
    }


def get_user_from_event(event: Dict) -> Dict[str, str]:
    """Extract user information from API Gateway event"""
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        
        # Check if using Cognito User Pools authorizer (claims)
        claims = authorizer.get('claims', {})
        if claims:
            return {
                'user_id': claims.get('sub', 'unknown'),
                'email': claims.get('email', 'unknown'),
                'username': claims.get('cognito:username', 'unknown'),
                'role': claims.get('custom:role', 'Viewer')
            }
        
        # Check if using Lambda JWT authorizer (context)
        context = authorizer
        if context.get('userId'):
            return {
                'user_id': context.get('userId', 'unknown'),
                'email': context.get('email', 'unknown'),
                'username': context.get('username', 'unknown'),
                'role': context.get('role', 'Viewer')
            }
        
        # Fallback for unknown authorizer type
        logger.warning("No valid user information found in event")
        return {
            'user_id': 'unknown',
            'email': 'unknown',
            'username': 'unknown',
            'role': 'Viewer'
        }
        
    except Exception as e:
        logger.error(f"Error extracting user from event: {str(e)}")
        return {
            'user_id': 'unknown',
            'email': 'unknown',
            'username': 'unknown',
            'role': 'Viewer'
        }


def log_audit_event(user_id: str, action: str, resource_type: str, 
                   resource_id: str, result: str, details: Optional[Dict] = None):
    """Log audit event to DynamoDB"""
    try:
        table = dynamodb.Table(AUDIT_LOG_TABLE)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            'event_id': f"{user_id}_{timestamp}",
            'timestamp': timestamp,
            'user_id': user_id,
            'action': action,
            'resource_type': resource_type,
            'resource_id': resource_id,
            'result': result,
            'details': details or {},
            'ttl': timestamp + (90 * 24 * 60 * 60 * 1000)  # 90 days retention
        }
        
        table.put_item(Item=item)
        logger.info(f"Audit event logged: {action} on {resource_type}/{resource_id}")
    except Exception as e:
        logger.error(f"Error logging audit event: {str(e)}")


# RBAC Authorization System
class Role(Enum):
    """User roles with hierarchical permissions"""
    VIEWER = "Viewer"
    OPERATOR = "Operator"
    DATA_SCIENTIST = "DataScientist"
    USECASE_ADMIN = "UseCaseAdmin"
    PORTAL_ADMIN = "PortalAdmin"


class Permission(Enum):
    """System permissions"""
    # Use Case Management
    VIEW_USECASES = "view_usecases"
    CREATE_USECASES = "create_usecases"
    UPDATE_USECASES = "update_usecases"
    DELETE_USECASES = "delete_usecases"
    MANAGE_USECASE_USERS = "manage_usecase_users"
    
    # Data Labeling
    VIEW_LABELING_JOBS = "view_labeling_jobs"
    CREATE_LABELING_JOBS = "create_labeling_jobs"
    MANAGE_LABELING_JOBS = "manage_labeling_jobs"
    
    # Dataset Management
    VIEW_DATASETS = "view_datasets"
    UPLOAD_DATASETS = "upload_datasets"
    MANAGE_DATASETS = "manage_datasets"
    
    # Model Training
    VIEW_TRAINING_JOBS = "view_training_jobs"
    CREATE_TRAINING_JOBS = "create_training_jobs"
    MANAGE_TRAINING_JOBS = "manage_training_jobs"
    
    # Model Registry
    VIEW_MODELS = "view_models"
    PROMOTE_MODELS = "promote_models"
    DELETE_MODELS = "delete_models"
    
    # Deployments
    VIEW_DEPLOYMENTS = "view_deployments"
    CREATE_DEPLOYMENTS = "create_deployments"
    MANAGE_DEPLOYMENTS = "manage_deployments"
    ROLLBACK_DEPLOYMENTS = "rollback_deployments"
    
    # Device Management
    VIEW_DEVICES = "view_devices"
    MANAGE_DEVICES = "manage_devices"
    RESTART_DEVICES = "restart_devices"
    REBOOT_DEVICES = "reboot_devices"
    BROWSE_DEVICE_FILES = "browse_device_files"
    VIEW_DEVICE_LOGS = "view_device_logs"
    UPDATE_DEVICE_CONFIG = "update_device_config"
    
    # System Administration
    VIEW_AUDIT_LOGS = "view_audit_logs"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_USERS = "manage_users"
    
    # Super User Permissions
    ALL_USECASES_ACCESS = "all_usecases_access"
    SYSTEM_ADMIN = "system_admin"


class RBACManager:
    """Role-Based Access Control Manager
    
    Uses IDP (Identity Provider) as the single source of truth for user roles.
    Roles come from JWT claims (custom:role or custom:groups).
    
    Supported role sources:
    - Cognito User Pools: custom:role attribute
    - SAML/OIDC IdP: mapped to custom:role or custom:groups
    - Lambda authorizer: role in context
    """
    
    def __init__(self):
        self.role_permissions = self._initialize_role_permissions()
    
    def _initialize_role_permissions(self) -> Dict[Role, Set[Permission]]:
        """Initialize role-permission mappings"""
        return {
            Role.VIEWER: {
                Permission.VIEW_USECASES,
                Permission.VIEW_LABELING_JOBS,
                Permission.VIEW_DATASETS,
                Permission.VIEW_TRAINING_JOBS,
                Permission.VIEW_MODELS,
                Permission.VIEW_DEPLOYMENTS,
                Permission.VIEW_DEVICES,
                Permission.VIEW_DEVICE_LOGS,
            },
            
            Role.OPERATOR: {
                # Inherit Viewer permissions
                Permission.VIEW_USECASES,
                Permission.VIEW_LABELING_JOBS,
                Permission.VIEW_DATASETS,
                Permission.VIEW_TRAINING_JOBS,
                Permission.VIEW_MODELS,
                Permission.VIEW_DEPLOYMENTS,
                Permission.VIEW_DEVICES,
                Permission.VIEW_DEVICE_LOGS,
                # Add Operator-specific permissions
                Permission.CREATE_DEPLOYMENTS,
                Permission.MANAGE_DEPLOYMENTS,
                Permission.ROLLBACK_DEPLOYMENTS,
                Permission.MANAGE_DEVICES,
                Permission.RESTART_DEVICES,
                Permission.REBOOT_DEVICES,
                Permission.BROWSE_DEVICE_FILES,
                Permission.UPDATE_DEVICE_CONFIG,
            },
            
            Role.DATA_SCIENTIST: {
                # Inherit Viewer permissions
                Permission.VIEW_USECASES,
                Permission.VIEW_LABELING_JOBS,
                Permission.VIEW_DATASETS,
                Permission.VIEW_TRAINING_JOBS,
                Permission.VIEW_MODELS,
                Permission.VIEW_DEPLOYMENTS,
                Permission.VIEW_DEVICES,
                Permission.VIEW_DEVICE_LOGS,
                # Add DataScientist-specific permissions
                Permission.CREATE_LABELING_JOBS,
                Permission.MANAGE_LABELING_JOBS,
                Permission.UPLOAD_DATASETS,
                Permission.MANAGE_DATASETS,
                Permission.CREATE_TRAINING_JOBS,
                Permission.MANAGE_TRAINING_JOBS,
                Permission.PROMOTE_MODELS,
                Permission.DELETE_MODELS,
            },
            
            Role.USECASE_ADMIN: {
                # Inherit DataScientist and Operator permissions
                Permission.VIEW_USECASES,
                Permission.VIEW_LABELING_JOBS,
                Permission.VIEW_DATASETS,
                Permission.VIEW_TRAINING_JOBS,
                Permission.VIEW_MODELS,
                Permission.VIEW_DEPLOYMENTS,
                Permission.VIEW_DEVICES,
                Permission.VIEW_DEVICE_LOGS,
                Permission.CREATE_LABELING_JOBS,
                Permission.MANAGE_LABELING_JOBS,
                Permission.UPLOAD_DATASETS,
                Permission.MANAGE_DATASETS,
                Permission.CREATE_TRAINING_JOBS,
                Permission.MANAGE_TRAINING_JOBS,
                Permission.PROMOTE_MODELS,
                Permission.DELETE_MODELS,
                Permission.CREATE_DEPLOYMENTS,
                Permission.MANAGE_DEPLOYMENTS,
                Permission.ROLLBACK_DEPLOYMENTS,
                Permission.MANAGE_DEVICES,
                Permission.RESTART_DEVICES,
                Permission.REBOOT_DEVICES,
                Permission.BROWSE_DEVICE_FILES,
                Permission.UPDATE_DEVICE_CONFIG,
                # Add UseCaseAdmin-specific permissions
                Permission.UPDATE_USECASES,
                Permission.MANAGE_USECASE_USERS,
                Permission.VIEW_AUDIT_LOGS,
            },
            
            Role.PORTAL_ADMIN: {
                # All permissions
                *[permission for permission in Permission],
            }
        }
    
    def get_user_role(self, user_id: str, usecase_id: str, user_info: Optional[Dict] = None) -> Optional[Role]:
        """Get user's role for a specific usecase
        
        Role resolution order:
        1. PortalAdmin from JWT claims (global admin access)
        2. UseCase-specific role from DynamoDB (assigned via Team Management)
        3. Default role from JWT claims (Cognito custom:role attribute)
        4. Default to Viewer if nothing found
        
        Args:
            user_id: User ID (for logging/fallback)
            usecase_id: Use case ID for usecase-specific role lookup
            user_info: User info dict from get_user_from_event() containing role from JWT
            
        Returns:
            Role enum or None
        """
        try:
            jwt_role = user_info.get('role') if user_info else None
            
            # 1. Check if user is PortalAdmin from JWT (global admin)
            if jwt_role == 'PortalAdmin':
                return Role.PORTAL_ADMIN
            
            user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
            
            # 2. Check for PortalAdmin in DynamoDB (global role)
            response = user_roles_table.get_item(
                Key={'user_id': user_id, 'usecase_id': 'global'}
            )
            
            if response.get('Item', {}).get('role') == 'PortalAdmin':
                logger.info(f"User {user_id} has PortalAdmin from DynamoDB")
                return Role.PORTAL_ADMIN
            
            # 3. Check usecase-specific role in DynamoDB (assigned via Team Management)
            # This takes precedence over JWT default role for non-admin users
            if usecase_id and usecase_id != 'global':
                response = user_roles_table.get_item(
                    Key={'user_id': user_id, 'usecase_id': usecase_id}
                )
                
                if response.get('Item'):
                    role_str = response['Item'].get('role')
                    try:
                        usecase_role = Role(role_str)
                        logger.info(f"User {user_id} has role {role_str} for usecase {usecase_id} from DynamoDB")
                        return usecase_role
                    except ValueError:
                        logger.warning(f"Invalid role found in DynamoDB: {role_str}")
            
            # 4. Fall back to JWT role (Cognito custom:role attribute)
            if jwt_role:
                try:
                    return Role(jwt_role)
                except ValueError:
                    logger.warning(f"Invalid role from JWT: {jwt_role}, defaulting to Viewer")
                    return Role.VIEWER
            
            # Default to Viewer if no role found
            return Role.VIEWER
            
        except Exception as e:
            logger.error(f"Error getting user role: {str(e)}")
            return Role.VIEWER
    
    def get_user_permissions(self, user_id: str, usecase_id: str, user_info: Optional[Dict] = None) -> Set[Permission]:
        """Get all permissions for a user based on their IDP role"""
        role = self.get_user_role(user_id, usecase_id, user_info)
        if not role:
            return set()
        
        return self.role_permissions.get(role, set())
    
    def has_permission(self, user_id: str, usecase_id: str, permission: Permission, user_info: Optional[Dict] = None) -> bool:
        """Check if user has a specific permission based on IDP role"""
        user_permissions = self.get_user_permissions(user_id, usecase_id, user_info)
        return permission in user_permissions
    
    def assign_user_role(self, user_id: str, usecase_id: str, role: Role, assigned_by: str) -> bool:
        """Assign a user to a usecase with a specific role in DynamoDB
        
        This creates a usecase-specific role assignment that supplements the IDP role.
        Used for granting users access to specific usecases.
        
        Args:
            user_id: User ID (email) to assign
            usecase_id: Use case ID to grant access to
            role: Role to assign (UseCaseAdmin, DataScientist, Operator, Viewer)
            assigned_by: User ID of the person making the assignment
            
        Returns:
            True if successful, False otherwise
        """
        try:
            user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
            timestamp = int(datetime.utcnow().timestamp())
            
            user_roles_table.put_item(
                Item={
                    'user_id': user_id,
                    'usecase_id': usecase_id,
                    'role': role.value,
                    'assigned_at': timestamp,
                    'assigned_by': assigned_by
                }
            )
            
            logger.info(f"Assigned user {user_id} to usecase {usecase_id} with role {role.value}")
            return True
            
        except Exception as e:
            logger.error(f"Error assigning user role: {str(e)}")
            return False
    
    def remove_user_role(self, user_id: str, usecase_id: str, removed_by: str) -> bool:
        """Remove a user's role assignment from a usecase
        
        Args:
            user_id: User ID to remove
            usecase_id: Use case ID to remove access from
            removed_by: User ID of the person removing the assignment
            
        Returns:
            True if successful, False otherwise
        """
        try:
            user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
            
            user_roles_table.delete_item(
                Key={
                    'user_id': user_id,
                    'usecase_id': usecase_id
                }
            )
            
            logger.info(f"Removed user {user_id} from usecase {usecase_id} by {removed_by}")
            return True
            
        except Exception as e:
            logger.error(f"Error removing user role: {str(e)}")
            return False
    
    def is_portal_admin(self, user_id: str, user_info: Optional[Dict] = None) -> bool:
        """Check if user is a PortalAdmin based on IDP role"""
        role = self.get_user_role(user_id, 'global', user_info)
        return role == Role.PORTAL_ADMIN
    
    def get_accessible_usecases(self, user_id: str, user_info: Optional[Dict] = None) -> List[str]:
        """Get list of use cases the user has access to
        
        PortalAdmin (from IDP) has access to all use cases.
        Other users need explicit usecase assignments in DynamoDB.
        """
        try:
            # If user is PortalAdmin (from IDP), they have access to all use cases
            if self.is_portal_admin(user_id, user_info):
                # Get all use cases
                usecases_table = dynamodb.Table(USECASES_TABLE)
                response = usecases_table.scan()
                return [item['usecase_id'] for item in response.get('Items', [])]
            
            # For non-admin users, get specific use case assignments from DynamoDB
            # This is the only place we use DynamoDB for RBAC - to track which
            # usecases a non-admin user has been granted access to
            user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
            response = user_roles_table.query(
                KeyConditionExpression='user_id = :user_id',
                ExpressionAttributeValues={':user_id': user_id}
            )
            
            usecases = []
            for item in response.get('Items', []):
                usecase_id = item.get('usecase_id')
                if usecase_id and usecase_id != 'global':
                    usecases.append(usecase_id)
            
            return usecases
            
        except Exception as e:
            logger.error(f"Error getting accessible use cases: {str(e)}")
            return []


# Global RBAC manager instance
rbac_manager = RBACManager()


def require_permission(permission: Permission, usecase_param: str = 'usecase_id'):
    """
    Decorator to require specific permission for API endpoints.
    Uses IDP (JWT claims) as the source of truth for user roles.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(event, context):
            try:
                # Extract user information from JWT claims
                user = get_user_from_event(event)
                user_id = user['user_id']
                
                # Extract use case ID from event
                usecase_id = None
                
                # Try path parameters first
                path_params = event.get('pathParameters', {}) or {}
                if usecase_param in path_params:
                    usecase_id = path_params[usecase_param]
                
                # Try query parameters
                if not usecase_id:
                    query_params = event.get('queryStringParameters', {}) or {}
                    if usecase_param in query_params:
                        usecase_id = query_params[usecase_param]
                
                # Try request body
                if not usecase_id:
                    try:
                        body = json.loads(event.get('body', '{}'))
                        if usecase_param in body:
                            usecase_id = body[usecase_param]
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # For endpoints that don't require a specific use case
                if not usecase_id and permission in [
                    Permission.VIEW_USECASES, 
                    Permission.CREATE_USECASES,
                    Permission.MANAGE_USERS,
                    Permission.SYSTEM_ADMIN
                ]:
                    usecase_id = 'global'
                
                if not usecase_id:
                    return create_response(400, {'error': 'Use case ID required'})
                
                # Check permission using IDP role from JWT
                if not rbac_manager.has_permission(user_id, usecase_id, permission, user):
                    # Log unauthorized access attempt
                    log_audit_event(
                        user_id=user_id,
                        action='unauthorized_access',
                        resource_type='api_endpoint',
                        resource_id=event.get('path', 'unknown'),
                        result='denied',
                        details={
                            'required_permission': permission.value,
                            'usecase_id': usecase_id,
                            'user_role': user.get('role', 'unknown')
                        }
                    )
                    
                    return create_response(403, {
                        'error': 'Insufficient permissions',
                        'required_permission': permission.value
                    })
                
                # Call the original function
                return func(event, context)
                
            except Exception as e:
                logger.error(f"Error in permission check: {str(e)}")
                return create_response(500, {'error': 'Authorization check failed'})
        
        return wrapper
    return decorator


def require_super_user(func):
    """
    Decorator to require PortalAdmin (super user) access.
    Uses IDP (JWT claims) as the source of truth for user roles.
    """
    @wraps(func)
    def wrapper(event, context):
        try:
            user = get_user_from_event(event)
            user_id = user['user_id']
            
            if not rbac_manager.is_portal_admin(user_id, user):
                log_audit_event(
                    user_id=user_id,
                    action='unauthorized_super_user_access',
                    resource_type='api_endpoint',
                    resource_id=event.get('path', 'unknown'),
                    result='denied',
                    details={'user_role': user.get('role', 'unknown')}
                )
                
                return create_response(403, {
                    'error': 'Super user access required',
                    'required_role': 'PortalAdmin'
                })
            
            return func(event, context)
            
        except Exception as e:
            logger.error(f"Error in super user check: {str(e)}")
            return create_response(500, {'error': 'Authorization check failed'})
    
    return wrapper


def check_user_access(user_id: str, usecase_id: str, required_role: Optional[str] = None, user_info: Optional[Dict] = None) -> bool:
    """
    Legacy function for backward compatibility
    Check if user has access to a use case
    
    Args:
        user_id: User ID
        usecase_id: Use case ID
        required_role: Optional minimum role required (e.g., 'DataScientist')
        user_info: Optional user info dict from get_user_from_event() containing role from JWT
    """
    try:
        # Use the new RBAC system with user_info for JWT role lookup
        user_role = rbac_manager.get_user_role(user_id, usecase_id, user_info)
        
        if not user_role:
            return False
        
        if required_role:
            # Convert string role to Role enum for comparison
            try:
                required_role_enum = Role(required_role)
                role_hierarchy = {
                    Role.VIEWER: 1,
                    Role.OPERATOR: 2,
                    Role.DATA_SCIENTIST: 3,
                    Role.USECASE_ADMIN: 4,
                    Role.PORTAL_ADMIN: 5
                }
                return role_hierarchy.get(user_role, 0) >= role_hierarchy.get(required_role_enum, 0)
            except ValueError:
                logger.warning(f"Invalid required role: {required_role}")
                return False
        
        return True
    except Exception as e:
        logger.error(f"Error checking user access: {str(e)}")
        return False


def is_super_user(user_id: str) -> bool:
    """Check if user is a PortalAdmin (super user)"""
    return rbac_manager.is_portal_admin(user_id)


def validate_required_fields(data: Dict, required_fields: list) -> Optional[str]:
    """Validate that required fields are present in data"""
    missing_fields = [field for field in required_fields if field not in data]
    if missing_fields:
        return f"Missing required fields: {', '.join(missing_fields)}"
    return None


def get_usecase(usecase_id: str) -> Dict:
    """Get use case details from DynamoDB"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            raise ValueError(f"Use case not found: {usecase_id}")
        
        return response['Item']
    except ClientError as e:
        logger.error(f"Error getting use case: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error getting use case: {str(e)}")
        raise


def assume_usecase_role(role_arn: str, external_id: str, session_name: str) -> Dict:
    """Assume cross-account role for UseCase Account access
    
    Args:
        role_arn: ARN of the role to assume
        external_id: External ID for role assumption (can be None if role doesn't require it)
        session_name: Name for the assumed role session
    """
    try:
        sts_client = boto3.client('sts')
        
        # Build assume role parameters
        assume_params = {
            'RoleArn': role_arn,
            'RoleSessionName': session_name,
            'DurationSeconds': 3600
        }
        
        # Only include ExternalId if provided (some roles don't require it)
        if external_id:
            assume_params['ExternalId'] = external_id
        
        response = sts_client.assume_role(**assume_params)
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error assuming role: {str(e)}")
        raise


def assume_cross_account_role(role_arn: str, external_id: str, session_name: str = None) -> Dict:
    """
    Assume cross-account role - alias for assume_usecase_role for backward compatibility
    """
    if not session_name:
        session_name = f"portal-session-{int(datetime.utcnow().timestamp())}"
    return assume_usecase_role(role_arn, external_id, session_name)


def cors_headers() -> Dict[str, str]:
    """Return standard CORS headers for API responses"""
    return {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
    }


def get_current_user(event: Dict) -> Dict[str, str]:
    """Alias for get_user_from_event for backward compatibility"""
    return get_user_from_event(event)


def get_user_use_cases(user_id: str) -> List[str]:
    """Get list of use cases the user has access to"""
    return rbac_manager.get_accessible_usecases(user_id)


def handle_error(error: Exception, message_or_headers: Any = "Operation failed") -> Dict:
    """Handle errors and return formatted error response"""
    # Support both old signature (error, message) and new signature (error, headers)
    if isinstance(message_or_headers, dict):
        # New signature: handle_error(error, headers)
        headers = message_or_headers
        message = "Operation failed"
    else:
        # Old signature: handle_error(error, message)
        message = message_or_headers
        headers = cors_headers() if 'cors_headers' in dir() else {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
        }
    
    logger.error(f"{message}: {str(error)}")
    
    if isinstance(error, ValueError):
        return {
            'statusCode': 400,
            'headers': headers,
            'body': json.dumps({'error': str(error)})
        }
    elif isinstance(error, ClientError):
        error_code = error.response.get('Error', {}).get('Code', 'Unknown')
        error_message = error.response.get('Error', {}).get('Message', str(error))
        if error_code == 'AccessDenied':
            return {
                'statusCode': 403,
                'headers': headers,
                'body': json.dumps({'error': f'Access denied: {error_message}'})
            }
        elif error_code == 'ResourceNotFoundException':
            return {
                'statusCode': 404,
                'headers': headers,
                'body': json.dumps({'error': 'Resource not found'})
            }
        else:
            return {
                'statusCode': 500,
                'headers': headers,
                'body': json.dumps({'error': f"{message}: {error_code} - {error_message}"})
            }
    else:
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({'error': f"{message}: {str(error)}"})
        }


# S3 Path Management Utilities
import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class S3PathConfig:
    """Configuration for S3 path structure"""
    bucket: str
    prefix: str = ""
    training_folder: str = "models/training"
    compilation_folder: str = "models/compilation"
    dataset_folder: str = "datasets"
    deployment_folder: str = "deployments"


class S3PathBuilder:
    """Centralized service for constructing S3 paths consistently"""
    
    def __init__(self, bucket: str, prefix: str = ""):
        self.bucket = bucket
        self.prefix = prefix.rstrip('/') if prefix else ""
        self.config = S3PathConfig(bucket=bucket, prefix=prefix)
    
    def _build_path(self, *parts: str) -> str:
        """Build a clean S3 path from parts, avoiding double slashes"""
        # Filter out empty parts and join with single slashes
        clean_parts = [part.strip('/') for part in parts if part.strip('/')]
        if self.prefix:
            clean_parts.insert(0, self.prefix)
        return '/'.join(clean_parts)
    
    def _validate_s3_key(self, key: str) -> bool:
        """Validate that the key meets S3 object key requirements"""
        if not key or len(key) > 1024:
            return False
        
        # S3 key cannot contain certain characters
        invalid_chars = ['\\', '{', '^', '}', '%', '`', ']', '"', '>', '[', '~', '<', '#', '|']
        if any(char in key for char in invalid_chars):
            return False
        
        # Cannot have consecutive slashes or start/end with slash
        if '//' in key or key.startswith('/') or key.endswith('/'):
            return False
        
        return True
    
    def training_output_path(self, job_name: str) -> str:
        """Generate S3 path for training job outputs"""
        if not job_name or not job_name.strip():
            raise ValueError("Job name cannot be empty")
        
        # Sanitize job name for S3 compatibility
        safe_job_name = re.sub(r'[^a-zA-Z0-9\-_.]', '-', job_name.strip())
        path = self._build_path(self.config.training_folder, safe_job_name)
        
        if not self._validate_s3_key(path):
            raise ValueError(f"Generated path is not a valid S3 key: {path}")
        
        return path
    
    def compilation_output_path(self, job_name: str, target: Optional[str] = None) -> str:
        """Generate S3 path for compilation job outputs"""
        if not job_name or not job_name.strip():
            raise ValueError("Job name cannot be empty")
        
        # Sanitize job name for S3 compatibility
        safe_job_name = re.sub(r'[^a-zA-Z0-9\-_.]', '-', job_name.strip())
        
        if target:
            # Sanitize target name
            safe_target = re.sub(r'[^a-zA-Z0-9\-_.]', '-', target.strip())
            path = self._build_path(self.config.compilation_folder, safe_job_name, safe_target)
        else:
            path = self._build_path(self.config.compilation_folder, safe_job_name)
        
        if not self._validate_s3_key(path):
            raise ValueError(f"Generated path is not a valid S3 key: {path}")
        
        return path
    
    def dataset_path(self, dataset_type: str = "raw") -> str:
        """Generate S3 path for datasets"""
        if not dataset_type or not dataset_type.strip():
            raise ValueError("Dataset type cannot be empty")
        
        # Sanitize dataset type
        safe_type = re.sub(r'[^a-zA-Z0-9\-_.]', '-', dataset_type.strip())
        path = self._build_path(self.config.dataset_folder, safe_type)
        
        if not self._validate_s3_key(path):
            raise ValueError(f"Generated path is not a valid S3 key: {path}")
        
        return path
    
    def deployment_path(self, deployment_name: str) -> str:
        """Generate S3 path for deployment artifacts"""
        if not deployment_name or not deployment_name.strip():
            raise ValueError("Deployment name cannot be empty")
        
        # Sanitize deployment name
        safe_name = re.sub(r'[^a-zA-Z0-9\-_.]', '-', deployment_name.strip())
        path = self._build_path(self.config.deployment_folder, safe_name)
        
        if not self._validate_s3_key(path):
            raise ValueError(f"Generated path is not a valid S3 key: {path}")
        
        return path
    
    def get_s3_uri(self, path: str) -> str:
        """Convert a path to a full S3 URI"""
        return f"s3://{self.bucket}/{path}"
    
    def get_training_output_uri(self, job_name: str) -> str:
        """Get full S3 URI for training output"""
        path = self.training_output_path(job_name)
        return self.get_s3_uri(path)
    
    def get_compilation_output_uri(self, job_name: str, target: Optional[str] = None) -> str:
        """Get full S3 URI for compilation output"""
        path = self.compilation_output_path(job_name, target)
        return self.get_s3_uri(path)


class PathResolver:
    """Provides backward compatibility and path resolution during migration"""
    
    def __init__(self, path_builder: S3PathBuilder):
        self.path_builder = path_builder
    
    def is_legacy_path(self, s3_uri: str) -> bool:
        """Check if an S3 URI uses the legacy path structure"""
        try:
            parsed = urlparse(s3_uri)
            path = parsed.path.lstrip('/')
            
            # Legacy patterns to detect
            legacy_patterns = [
                r'datasets/.*training-output',  # Old training output in datasets
                r'datasets/.*compilation-output',  # Old compilation output in datasets
                r'.*//.*',  # Double slashes
                r'training-output(?!/models/)',  # training-output not under models/
                r'compilation-output(?!/models/)',  # compilation-output not under models/
            ]
            
            return any(re.search(pattern, path) for pattern in legacy_patterns)
        except Exception:
            return False
    
    def convert_legacy_path(self, legacy_path: str) -> str:
        """Convert a legacy S3 path to the new structure"""
        try:
            parsed = urlparse(legacy_path)
            old_path = parsed.path.lstrip('/')
            
            # Extract job name from legacy path
            # Pattern: datasets/*/training-output/{job-name}/output/model.tar.gz
            training_match = re.search(r'training-output/([^/]+)', old_path)
            if training_match:
                job_name = training_match.group(1)
                new_path = self.path_builder.training_output_path(job_name)
                return self.path_builder.get_s3_uri(new_path)
            
            # Pattern: datasets/*/compilation-output/{job-name}/output/model.tar.gz
            compilation_match = re.search(r'compilation-output/([^/]+)', old_path)
            if compilation_match:
                job_name = compilation_match.group(1)
                new_path = self.path_builder.compilation_output_path(job_name)
                return self.path_builder.get_s3_uri(new_path)
            
            # If no pattern matches, return original
            return legacy_path
            
        except Exception as e:
            logger.warning(f"Failed to convert legacy path {legacy_path}: {str(e)}")
            return legacy_path


def create_s3_path_builder(bucket: str, prefix: str = "") -> S3PathBuilder:
    """Factory function to create S3PathBuilder instance"""
    return S3PathBuilder(bucket, prefix)


def create_path_resolver(bucket: str, prefix: str = "") -> PathResolver:
    """Factory function to create PathResolver instance"""
    path_builder = create_s3_path_builder(bucket, prefix)
    return PathResolver(path_builder)


# Storage Management Utilities (Stub implementations for compatibility)
def create_storage_manager(*args, **kwargs):
    """Stub for storage manager - not implemented in this version"""
    logger.warning("Storage manager functionality not implemented")
    return None


def create_streaming_extractor(*args, **kwargs):
    """Stub for streaming extractor - not implemented in this version"""
    logger.warning("Streaming extractor functionality not implemented")
    return None


def create_incremental_zipper(*args, **kwargs):
    """Stub for incremental zipper - not implemented in this version"""
    logger.warning("Incremental zipper functionality not implemented")
    return None


def create_cleanup_context(*args, **kwargs):
    """Stub for cleanup context - not implemented in this version"""
    logger.warning("Cleanup context functionality not implemented")
    return None


def create_storage_manager_with_logging(*args, **kwargs):
    """Stub for storage manager with logging - not implemented in this version"""
    logger.warning("Storage manager with logging functionality not implemented")
    return None


def create_retry_manager(*args, **kwargs):
    """Stub for retry manager - not implemented in this version"""
    logger.warning("Retry manager functionality not implemented")
    return None


def create_processing_strategy_manager(*args, **kwargs):
    """Stub for processing strategy manager - not implemented in this version"""
    logger.warning("Processing strategy manager functionality not implemented")
    return None


def create_operation_logger(*args, **kwargs):
    """Stub for operation logger - not implemented in this version"""
    logger.warning("Operation logger functionality not implemented")
    return None


# Custom Exceptions for Storage Management
class InsufficientStorageError(Exception):
    """Raised when there is not enough storage space"""
    def __init__(self, message="Insufficient storage space", required=0, available=0):
        self.required = required
        self.available = available
        super().__init__(message)


class CleanupFailedError(Exception):
    """Raised when cleanup operation fails"""
    pass


class ExtractionError(Exception):
    """Raised when file extraction fails"""
    pass


class CompressionError(Exception):
    """Raised when file compression fails"""
    pass


# Component Discovery Stubs (for compatibility)
class ComponentType:
    """Stub for component type"""
    pass


class ComponentFilter:
    """Stub for component filter"""
    def __init__(self, *args, **kwargs):
        pass


class ComponentStatus:
    """Stub for component status"""
    pass


class ComponentMetadata:
    """Stub for component metadata"""
    pass


class ComponentDiscoveryService:
    """Stub for component discovery service"""
    pass


class ComponentRegistry:
    """Stub for component registry"""
    pass


def validate_usecase_access(user_id: str, usecase_id: str, required_permissions: List[Permission] = None) -> Dict[str, Any]:
    """
    Middleware function for use case access validation
    
    Args:
        user_id: User identifier
        usecase_id: Use case identifier
        required_permissions: List of required permissions (optional)
    
    Returns:
        Dictionary with validation result and user context
    """
    try:
        # Check if user has access to the use case
        user_role = rbac_manager.get_user_role(user_id, usecase_id)
        
        if not user_role:
            return {
                'valid': False,
                'error': 'Access denied: User not assigned to use case',
                'error_code': 'NO_ACCESS'
            }
        
        # Check specific permissions if required
        if required_permissions:
            user_permissions = rbac_manager.get_user_permissions(user_id, usecase_id)
            missing_permissions = [p for p in required_permissions if p not in user_permissions]
            
            if missing_permissions:
                return {
                    'valid': False,
                    'error': f'Insufficient permissions: {[p.value for p in missing_permissions]}',
                    'error_code': 'INSUFFICIENT_PERMISSIONS',
                    'missing_permissions': [p.value for p in missing_permissions]
                }
        
        # Get use case details
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