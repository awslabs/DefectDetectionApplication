"""
Use Cases handler for Edge CV Portal
"""
import json
import logging
import os
from datetime import datetime
import uuid
import boto3
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    check_user_access, is_super_user, validate_required_fields,
    rbac_manager, Role, Permission, require_permission, require_super_user,
    validate_usecase_access
)

# Import shared components provisioning
try:
    from shared_components import provision_shared_components_for_usecase
except ImportError:
    # Fallback if shared_components module not available
    def provision_shared_components_for_usecase(*args, **kwargs):
        return {'status': 'skipped', 'reason': 'shared_components module not available'}

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
USECASES_TABLE = os.environ.get('USECASES_TABLE')


def handler(event, context):
    """
    Handle use case management requests
    
    GET    /api/v1/usecases       - List use cases
    POST   /api/v1/usecases       - Create use case
    GET    /api/v1/usecases/{id}  - Get use case details
    PUT    /api/v1/usecases/{id}  - Update use case
    DELETE /api/v1/usecases/{id}  - Delete use case
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_parameters = event.get('pathParameters') or {}
        
        logger.info(f"UseCases request: {http_method} {path}")
        
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
        
        user = get_user_from_event(event)
        
        if http_method == 'GET' and not path_parameters.get('id'):
            return list_usecases(user)
        elif http_method == 'POST':
            return create_usecase(event, user)
        elif http_method == 'GET' and path_parameters.get('id'):
            return get_usecase(path_parameters['id'], user)
        elif http_method == 'PUT' and path_parameters.get('id'):
            return update_usecase(path_parameters['id'], event, user)
        elif http_method == 'DELETE' and path_parameters.get('id'):
            return delete_usecase(path_parameters['id'], user)
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Error in usecases handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_usecases(user):
    """List all use cases accessible to the user"""
    try:
        table = dynamodb.Table(USECASES_TABLE)
        user_id = user['user_id']
        
        # Get accessible use cases using RBAC manager (role from IDP)
        accessible_usecases = rbac_manager.get_accessible_usecases(user_id, user)
        
        if rbac_manager.is_portal_admin(user_id, user):
            # Super user gets all use cases
            response = table.scan()
            usecases = response.get('Items', [])
            
            log_audit_event(
                user_id, 'list_usecases', 'usecase', 'all',
                'success', {'is_super_user': True, 'count': len(usecases)}
            )
        else:
            # Regular users get only their assigned use cases
            usecases = []
            for usecase_id in accessible_usecases:
                try:
                    response = table.get_item(Key={'usecase_id': usecase_id})
                    if 'Item' in response:
                        usecase = response['Item']
                        # Add user's role for this use case
                        usecase['user_role'] = user.get('role', 'Viewer')
                        usecases.append(usecase)
                except Exception as e:
                    logger.warning(f"Error getting use case {usecase_id}: {str(e)}")
            
            log_audit_event(
                user_id, 'list_usecases', 'usecase', 'assigned',
                'success', {'count': len(usecases)}
            )
        
        return create_response(200, {
            'usecases': usecases,
            'count': len(usecases),
            'is_super_user': rbac_manager.is_portal_admin(user_id, user)
        })
        
    except Exception as e:
        logger.error(f"Error listing use cases: {str(e)}")
        log_audit_event(
            user['user_id'], 'list_usecases', 'usecase', 'all', 'failure'
        )
        return create_response(500, {'error': 'Failed to list use cases'})


def create_usecase(event, user):
    """Create a new use case"""
    try:
        # Check permission using RBAC (role from IDP/JWT)
        if not rbac_manager.has_permission(user['user_id'], 'global', Permission.CREATE_USECASES, user):
            log_audit_event(
                user['user_id'], 'create_usecase', 'usecase', 'new',
                'failure', {'reason': 'insufficient_permissions', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Insufficient permissions to create use cases'})
        
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = ['name', 'account_id', 's3_bucket', 'cross_account_role_arn', 'sagemaker_execution_role_arn']
        validation_error = validate_required_fields(body, required_fields)
        if validation_error:
            return create_response(400, {'error': validation_error})
        
        table = dynamodb.Table(USECASES_TABLE)
        usecase_id = str(uuid.uuid4())
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            'usecase_id': usecase_id,
            'name': body['name'],
            'account_id': body['account_id'],
            's3_bucket': body['s3_bucket'],
            's3_prefix': body.get('s3_prefix', ''),
            'cross_account_role_arn': body['cross_account_role_arn'],
            'sagemaker_execution_role_arn': body['sagemaker_execution_role_arn'],
            'external_id': body.get('external_id', str(uuid.uuid4())),
            'owner': body.get('owner', user['email']),
            'cost_center': body.get('cost_center', ''),
            'default_device_group': body.get('default_device_group', ''),
            'created_at': timestamp,
            'updated_at': timestamp,
            'tags': body.get('tags', {}),
            'shared_components_provisioned': False
        }
        
        # Add optional Data Account fields if provided
        if body.get('data_account_id'):
            item['data_account_id'] = body['data_account_id']
        if body.get('data_account_role_arn'):
            item['data_account_role_arn'] = body['data_account_role_arn']
        if body.get('data_account_external_id'):
            item['data_account_external_id'] = body['data_account_external_id']
        if body.get('data_s3_bucket'):
            item['data_s3_bucket'] = body['data_s3_bucket']
        if body.get('data_s3_prefix'):
            item['data_s3_prefix'] = body['data_s3_prefix']
        
        table.put_item(Item=item)
        
        # Provision shared components (dda-LocalServer) to the usecase account
        # This creates read-only copies of portal-managed components
        shared_components_result = None
        provision_shared = body.get('provision_shared_components', True)
        
        if provision_shared:
            try:
                shared_components_result = provision_shared_components_for_usecase(
                    usecase_id=usecase_id,
                    usecase_account_id=body['account_id'],
                    cross_account_role_arn=body['cross_account_role_arn'],
                    external_id=item['external_id'],
                    user_id=user['user_id']
                )
                
                # Update usecase with provisioning status
                table.update_item(
                    Key={'usecase_id': usecase_id},
                    UpdateExpression='SET shared_components_provisioned = :provisioned, shared_components = :components',
                    ExpressionAttributeValues={
                        ':provisioned': True,
                        ':components': shared_components_result
                    }
                )
                item['shared_components_provisioned'] = True
                item['shared_components'] = shared_components_result
                
                logger.info(f"Provisioned shared components for usecase {usecase_id}")
                
            except Exception as e:
                logger.warning(f"Failed to provision shared components for usecase {usecase_id}: {str(e)}")
                # Don't fail usecase creation if shared component provisioning fails
                shared_components_result = {'error': str(e), 'status': 'failed'}
        
        log_audit_event(
            user['user_id'], 'create_usecase', 'usecase', usecase_id,
            'success', {
                'name': body['name'],
                'shared_components_provisioned': item.get('shared_components_provisioned', False)
            }
        )
        
        logger.info(f"Use case created: {usecase_id}")
        
        return create_response(201, {
            'usecase': item,
            'shared_components': shared_components_result,
            'message': 'Use case created successfully'
        })
        
    except Exception as e:
        logger.error(f"Error creating use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'create_usecase', 'usecase', 'new', 'failure'
        )
        return create_response(500, {'error': 'Failed to create use case'})


def get_usecase(usecase_id, user):
    """Get use case details"""
    try:
        # Check access using RBAC (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.VIEW_USECASES, user):
            log_audit_event(
                user['user_id'], 'get_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'access_denied', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Access denied to use case'})
        
        table = dynamodb.Table(USECASES_TABLE)
        response = table.get_item(Key={'usecase_id': usecase_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Use case not found'})
        
        log_audit_event(
            user['user_id'], 'get_usecase', 'usecase', usecase_id, 'success'
        )
        
        return create_response(200, {'usecase': response['Item']})
        
    except Exception as e:
        logger.error(f"Error getting use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'get_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to get use case'})


def update_usecase(usecase_id, event, user):
    """Update use case"""
    try:
        # Check permission using RBAC (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.UPDATE_USECASES, user):
            log_audit_event(
                user['user_id'], 'update_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'insufficient_permissions', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Insufficient permissions to update use case'})
        
        body = json.loads(event.get('body', '{}'))
        table = dynamodb.Table(USECASES_TABLE)
        
        # Build update expression
        update_expr = "SET updated_at = :updated_at"
        expr_values = {':updated_at': int(datetime.utcnow().timestamp() * 1000)}
        
        updatable_fields = [
            'name', 's3_bucket', 's3_prefix', 'owner', 'cost_center', 'default_device_group',
            'cross_account_role_arn', 'account_id',
            # Data Account fields
            'data_account_id', 'data_account_role_arn', 'data_account_external_id',
            'data_s3_bucket', 'data_s3_prefix'
        ]
        for field in updatable_fields:
            if field in body:
                update_expr += f", {field} = :{field}"
                expr_values[f":{field}"] = body[field]
        
        table.update_item(
            Key={'usecase_id': usecase_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values
        )
        
        log_audit_event(
            user['user_id'], 'update_usecase', 'usecase', usecase_id,
            'success', {'updated_fields': list(body.keys())}
        )
        
        return create_response(200, {'message': 'Use case updated successfully'})
        
    except Exception as e:
        logger.error(f"Error updating use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'update_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to update use case'})


def delete_usecase(usecase_id, user):
    """Delete use case"""
    try:
        # Check permission using RBAC - only PortalAdmin can delete use cases (role from IDP)
        if not rbac_manager.has_permission(user['user_id'], usecase_id, Permission.DELETE_USECASES, user):
            log_audit_event(
                user['user_id'], 'delete_usecase', 'usecase', usecase_id,
                'failure', {'reason': 'insufficient_permissions', 'user_role': user.get('role', 'unknown')}
            )
            return create_response(403, {'error': 'Insufficient permissions to delete use case'})
        
        table = dynamodb.Table(USECASES_TABLE)
        
        # TODO: Check if use case has active resources before deletion
        
        table.delete_item(Key={'usecase_id': usecase_id})
        
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'success'
        )
        
        return create_response(200, {'message': 'Use case deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting use case: {str(e)}")
        log_audit_event(
            user['user_id'], 'delete_usecase', 'usecase', usecase_id, 'failure'
        )
        return create_response(500, {'error': 'Failed to delete use case'})
