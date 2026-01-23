"""
Audit Logs Lambda Handler

Provides API endpoints for viewing audit logs with role-based access control:
- PortalAdmin: Can view all logs across all usecases
- UseCaseAdmin/DataScientist/Operator/Viewer: Can only view logs for their assigned usecases
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key, Attr

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import shared utilities
from shared_utils import (
    create_response,
    get_user_from_event,
    rbac_manager,
    Permission,
    AUDIT_LOG_TABLE,
)

# DynamoDB client
dynamodb = boto3.resource('dynamodb')


def decimal_default(obj):
    """JSON serializer for Decimal objects"""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda handler for audit logs API"""
    http_method = event.get('httpMethod', '')
    path = event.get('path', '')
    
    logger.info(f"Audit logs request: {http_method} {path}")
    
    try:
        if http_method == 'GET':
            return get_audit_logs(event)
        elif http_method == 'OPTIONS':
            return create_response(200, {})
        else:
            return create_response(405, {'error': f'Method {http_method} not allowed'})
    except Exception as e:
        logger.error(f"Error handling audit logs request: {str(e)}")
        return create_response(500, {'error': str(e)})


def get_audit_logs(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get audit logs with role-based filtering
    
    Query Parameters:
    - usecase_id: Filter by specific usecase (optional for PortalAdmin, required for others)
    - action: Filter by action type (optional)
    - user_id: Filter by user (optional, PortalAdmin only)
    - start_time: Start timestamp in milliseconds (optional)
    - end_time: End timestamp in milliseconds (optional)
    - limit: Maximum number of results (default 100, max 500)
    - next_token: Pagination token (optional)
    """
    try:
        # Get user info and check permissions
        user = get_user_from_event(event)
        user_id = user['user_id']
        user_email = user.get('email', user_id)
        
        # Check if user has permission to view audit logs
        is_portal_admin = rbac_manager.is_portal_admin(user_id, user)
        
        # Get query parameters
        query_params = event.get('queryStringParameters') or {}
        usecase_id = query_params.get('usecase_id')
        action_filter = query_params.get('action')
        user_filter = query_params.get('user_id')
        start_time = query_params.get('start_time')
        end_time = query_params.get('end_time')
        limit = min(int(query_params.get('limit', 100)), 500)
        next_token = query_params.get('next_token')
        
        # Get accessible usecases for non-admin users
        if not is_portal_admin:
            accessible_usecases = rbac_manager.get_accessible_usecases(user_id, user)
            
            if not accessible_usecases:
                return create_response(200, {
                    'logs': [],
                    'count': 0,
                    'message': 'No accessible usecases'
                })
            
            # If usecase_id specified, verify access
            if usecase_id and usecase_id not in accessible_usecases:
                return create_response(403, {
                    'error': 'Access denied to this usecase'
                })
            
            # Non-admin users can only filter by user_id if it's themselves
            if user_filter and user_filter != user_email:
                user_filter = None  # Ignore user filter for non-admins
        
        # Query audit logs from DynamoDB
        table = dynamodb.Table(AUDIT_LOG_TABLE)
        
        # Build scan/query parameters
        # Note: For production with large datasets, consider using GSI on usecase_id or timestamp
        scan_kwargs = {
            'Limit': limit,
        }
        
        # Build filter expression
        filter_expressions = []
        expression_values = {}
        expression_names = {}
        
        # Filter by usecase_id
        if usecase_id:
            filter_expressions.append('#usecase_id = :usecase_id')
            expression_values[':usecase_id'] = usecase_id
            expression_names['#usecase_id'] = 'usecase_id'
        elif not is_portal_admin:
            # Non-admin users: filter to their accessible usecases
            usecase_conditions = []
            for i, uc_id in enumerate(accessible_usecases):
                usecase_conditions.append(f'#usecase_id = :usecase_{i}')
                expression_values[f':usecase_{i}'] = uc_id
            if usecase_conditions:
                filter_expressions.append(f"({' OR '.join(usecase_conditions)})")
                expression_names['#usecase_id'] = 'usecase_id'
        
        # Filter by action
        if action_filter:
            filter_expressions.append('#action = :action')
            expression_values[':action'] = action_filter
            expression_names['#action'] = 'action'
        
        # Filter by user
        if user_filter:
            filter_expressions.append('#user_id = :user_filter')
            expression_values[':user_filter'] = user_filter
            expression_names['#user_id'] = 'user_id'
        
        # Filter by time range
        if start_time:
            filter_expressions.append('#timestamp >= :start_time')
            expression_values[':start_time'] = int(start_time)
            expression_names['#timestamp'] = 'timestamp'
        
        if end_time:
            filter_expressions.append('#timestamp <= :end_time')
            expression_values[':end_time'] = int(end_time)
            if '#timestamp' not in expression_names:
                expression_names['#timestamp'] = 'timestamp'
        
        # Apply filter expression
        if filter_expressions:
            scan_kwargs['FilterExpression'] = ' AND '.join(filter_expressions)
            scan_kwargs['ExpressionAttributeValues'] = expression_values
            if expression_names:
                scan_kwargs['ExpressionAttributeNames'] = expression_names
        
        # Handle pagination
        if next_token:
            try:
                import base64
                decoded = base64.b64decode(next_token).decode('utf-8')
                scan_kwargs['ExclusiveStartKey'] = json.loads(decoded)
            except Exception as e:
                logger.warning(f"Invalid pagination token: {e}")
        
        # Execute scan
        response = table.scan(**scan_kwargs)
        
        logs = response.get('Items', [])
        
        # Sort by timestamp descending (most recent first)
        logs.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        
        # Build response
        result = {
            'logs': logs,
            'count': len(logs),
            'scanned_count': response.get('ScannedCount', 0),
        }
        
        # Add pagination token if there are more results
        if 'LastEvaluatedKey' in response:
            import base64
            result['next_token'] = base64.b64encode(
                json.dumps(response['LastEvaluatedKey'], default=decimal_default).encode('utf-8')
            ).decode('utf-8')
        
        # Add available actions for filtering (for UI dropdown)
        result['available_actions'] = get_available_actions()
        
        # For PortalAdmin, include list of users who have logs
        if is_portal_admin:
            result['is_admin'] = True
        
        return create_response(200, result)
        
    except Exception as e:
        logger.error(f"Error getting audit logs: {str(e)}")
        return create_response(500, {'error': f'Failed to get audit logs: {str(e)}'})


def get_available_actions() -> list:
    """Return list of available action types for filtering"""
    return [
        'create_usecase',
        'update_usecase',
        'delete_usecase',
        'create_training_job',
        'stop_training_job',
        'create_labeling_job',
        'stop_labeling_job',
        'create_deployment',
        'cancel_deployment',
        'create_component',
        'delete_component',
        'publish_component',
        'import_model',
        'delete_model',
        'promote_model',
        'assign_user_role',
        'remove_user_role',
        'provision_shared_components',
        'unauthorized_access',
        'unauthorized_super_user_access',
    ]
