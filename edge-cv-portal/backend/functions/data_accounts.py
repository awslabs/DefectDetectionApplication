"""
Data Accounts Management for Edge CV Portal

This module handles registration and management of Data Accounts.
Data Accounts are separate AWS accounts that store training data,
allowing usecases to access data cross-account.

Only PortalAdmin users can manage Data Accounts.

Bedrock_Configuration (workflow-manager Requirement 10.6):
This handler also serves the Bedrock_Configuration settings API used by
the Workflow_Generator. No dedicated /settings API Gateway route exists
and no new routes may be added, so the configuration rides the existing
PortalAdmin-only /data-accounts/{id} GET/PUT routes using the reserved
id 'bedrock-configuration' (which can never collide with a real Data
Account id - those are 12-digit AWS account ids). This handler already
backs the portal Settings page, making it the natural carrier. Access
is restricted to PortalAdmin via Permission.BEDROCK_CONFIG_WRITE. The
stored item shape matches exactly what workflow_generator.py reads:
    {setting_key: 'bedrock_configuration',
     value: {model_id, region, max_tokens, temperature, top_p,
             timeout_seconds}}
"""
import json
import math
import os
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError
import uuid

# Import shared utilities
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    validate_required_fields, assume_cross_account_role as assume_role,
    require_super_user, rbac_manager, Permission
)
# Model_Image_Limit resolution (llm-autolabel-prompt-tuning Requirement 7.1).
# The same shared-layer function the Preview_API and the Auto_Labeler use, so
# the model dropdown reports exactly the bound the request paths apply.
# Model_Token_Limit resolution (llm-model-token-and-image-sizing Requirement
# 1.6): the same resolver both request paths use, so the budget the wizard
# pre-fills equals the maxTokens every Converse request carries.
from dda_llm_request import (
    resolve_model_image_limit, resolve_token_budget,
    MODEL_TOKEN_LIMIT_DEFAULT, MODEL_TOKEN_LIMIT_CEILING,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

# Environment variables
DATA_ACCOUNTS_TABLE = os.environ.get('DATA_ACCOUNTS_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# --------------------------------------------------------------------------
# Bedrock_Configuration (workflow-manager Requirements 10.6, 10.7)
# --------------------------------------------------------------------------

# Reserved path id on /data-accounts/{id} that routes to the Bedrock
# configuration handlers instead of the Data Account CRUD.
BEDROCK_CONFIG_RESOURCE_ID = 'bedrock-configuration'

# Settings-table key read by workflow_generator.get_bedrock_configuration().
BEDROCK_CONFIG_SETTING_KEY = 'bedrock_configuration'

# --------------------------------------------------------------------------
# Model_Token_Limits (llm-model-token-and-image-sizing Requirement 4)
# --------------------------------------------------------------------------

# Settings-table key of the Model_Token_Limits item. Held entirely
# independently of BEDROCK_CONFIG_SETTING_KEY: no operation on either item
# reads or writes the other (Requirements 4.4, 4.7).
LLM_MODEL_TOKEN_LIMITS_SETTING_KEY = 'llm_model_token_limits'

# Submission bounds (Requirements 4.1, 4.2).
MODEL_TOKEN_LIMITS_MAX_ENTRIES = 200
MODEL_TOKEN_LIMITS_MAX_KEY_LENGTH = 256

# --------------------------------------------------------------------------
# Camera_Registry Staleness_Threshold (camera-registry-sync Requirement 4.3)
# --------------------------------------------------------------------------

# Reserved path id on /data-accounts/{id} that routes to the Camera_Registry
# configuration handlers instead of the Data Account CRUD (same carrier as
# 'bedrock-configuration' above; can never collide with a real Data Account
# id - those are 12-digit AWS account ids).
CAMERA_REGISTRY_CONFIG_RESOURCE_ID = 'camera-registry-configuration'

# Settings-table key read by camera_registry.staleness_threshold_hours()
# and deployments._camera_staleness_threshold_ms(). Stored item shape:
#     {setting_key: 'camera_registry.staleness_threshold_hours',
#      value: <positive number of hours>}
CAMERA_STALENESS_SETTING_KEY = 'camera_registry.staleness_threshold_hours'

# Must mirror the readers' fallback so reads return the effective value
# even before anything is stored (camera-registry-sync Req 4.3).
DEFAULT_CAMERA_STALENESS_THRESHOLD_HOURS = 24

# Requirement 10.7: invocation timeout is configurable up to 240 seconds
# (raised from 60: large-output generations regularly exceed 60 s).
MAX_BEDROCK_TIMEOUT_SECONDS = 240

# Must mirror workflow_generator.DEFAULT_BEDROCK_CONFIG so reads return
# the effective configuration even before anything is stored.
DEFAULT_BEDROCK_CONFIG = {
    'model_id': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
    'region': os.environ.get('AWS_REGION', 'us-east-1'),
    'max_tokens': 4096,
    # Sampling parameters are unset by default: they are sent to Bedrock
    # only when explicitly configured (or overridden per-request). Recent
    # Anthropic models reject requests setting temperature at all, and
    # never accept temperature AND top_p together, so the generators omit
    # None values and send at most one of the two
    # (see workflow_generator.invoke_generation).
    'temperature': None,
    'top_p': None,
    'timeout_seconds': MAX_BEDROCK_TIMEOUT_SECONDS,
}

# bedrock control-plane clients (list-foundation-models /
# list-inference-profiles) cached per region for warm invocations.
_bedrock_control_clients: Dict[str, Any] = {}

# Per-invocation memo of the effective Model_Token_Limits mapping and the
# source it came from, as (mapping, source). Cleared at the top of handler()
# so it is never shared across invocations of a warm container - see
# _llm_model_token_limits().
_model_token_limits_cache: Optional[tuple] = None


def is_portal_admin(user: Dict) -> bool:
    """Check if user is a PortalAdmin"""
    return user.get('role') == 'PortalAdmin' or 'PortalAdmin' in user.get('groups', [])


def test_data_account_connection(
    role_arn: str,
    external_id: str
) -> Dict:
    """
    Test connection to Data Account by assuming role.
    
    Returns:
        dict with status and details
    """
    try:
        # Assume Data Account role
        credentials = assume_role(role_arn, external_id, 'test-connection')
        
        # Create S3 client with Data Account credentials to verify access
        s3_data = boto3.client(
            's3',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )
        
        # List buckets to verify S3 access
        s3_data.list_buckets()
        
        return {
            'status': 'success',
            'message': 'Successfully connected to Data Account'
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        
        if error_code == 'AccessDenied':
            return {
                'status': 'failed',
                'error': 'Access denied. Check role ARN and external ID.',
                'details': error_message
            }
        else:
            return {
                'status': 'failed',
                'error': f"{error_code}: {error_message}"
            }
    except Exception as e:
        return {
            'status': 'failed',
            'error': str(e)
        }


def handler(event: Dict, context: Any) -> Dict:
    """
    Lambda handler for Data Accounts management.
    
    GET    /api/v1/data-accounts           - List Data Accounts (All authenticated users - read-only)
    POST   /api/v1/data-accounts           - Register Data Account (PortalAdmin only)
    GET    /api/v1/data-accounts/{id}      - Get Data Account details (PortalAdmin only)
    PUT    /api/v1/data-accounts/{id}      - Update Data Account (PortalAdmin only)
    DELETE /api/v1/data-accounts/{id}      - Delete Data Account (PortalAdmin only)
    POST   /api/v1/data-accounts/{id}/test - Test connection (PortalAdmin only)

    Reserved id 'bedrock-configuration' (workflow-manager Requirement 10.6,
    PortalAdmin only via bedrock-config:write):
    GET    /api/v1/data-accounts/bedrock-configuration        - Read Bedrock_Configuration
    PUT    /api/v1/data-accounts/bedrock-configuration        - Update Bedrock_Configuration
    GET    /api/v1/data-accounts/bedrock-configuration/models - List invokable model options
    GET    /api/v1/data-accounts/bedrock-configuration/token-limits - Read Model_Token_Limits
    PUT    /api/v1/data-accounts/bedrock-configuration/token-limits - Replace Model_Token_Limits

    Reserved id 'camera-registry-configuration' (camera-registry-sync
    Requirement 4.3, PortalAdmin only):
    GET    /api/v1/data-accounts/camera-registry-configuration - Read Staleness_Threshold
    PUT    /api/v1/data-accounts/camera-registry-configuration - Update Staleness_Threshold
    """
    # The Model_Token_Limits memo is per invocation, never per container, so
    # a warm container can never serve a mapping written by an earlier
    # invocation (llm-model-token-and-image-sizing Requirement 4.1).
    _reset_model_token_limits_cache()
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        path_params = event.get('pathParameters') or {}
        
        # Handle CORS preflight
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS'
                },
                'body': ''
            }
        
        user = get_user_from_event(event)
        
        # Reserved id: Bedrock_Configuration settings API (Requirement 10.6).
        # Handled before Data Account CRUD; PortalAdmin-only via
        # Permission.BEDROCK_CONFIG_WRITE.
        if path_params.get('id') == BEDROCK_CONFIG_RESOURCE_ID:
            return handle_bedrock_configuration(event, user, http_method)

        # Reserved id: Camera_Registry Staleness_Threshold settings API
        # (camera-registry-sync Requirement 4.3). PortalAdmin-only.
        if path_params.get('id') == CAMERA_REGISTRY_CONFIG_RESOURCE_ID:
            return handle_camera_registry_configuration(event, user, http_method)
        
        # List Data Accounts is allowed for all authenticated users (read-only for dropdown)
        # All other operations require PortalAdmin
        is_list_operation = http_method == 'GET' and not path_params.get('id')
        
        if not is_list_operation and not is_portal_admin(user):
            return create_response(403, {'error': 'PortalAdmin access required'})
        
        # Route to appropriate handler
        if http_method == 'GET' and not path_params.get('id'):
            return list_data_accounts(event, user)
        elif http_method == 'POST' and not path_params.get('id'):
            return create_data_account(event, user)
        elif http_method == 'GET' and path_params.get('id'):
            return get_data_account(event, user, path_params['id'])
        elif http_method == 'PUT' and path_params.get('id'):
            return update_data_account(event, user, path_params['id'])
        elif http_method == 'DELETE' and path_params.get('id'):
            return delete_data_account(event, user, path_params['id'])
        elif http_method == 'POST' and '/test' in path:
            return test_connection(event, user, path_params['id'])
        
        return create_response(404, {'error': 'Not found'})
        
    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def list_data_accounts(event: Dict, user: Dict) -> Dict:
    """
    List all registered Data Accounts.
    
    This endpoint is accessible to all authenticated users (read-only)
    to populate the Data Account dropdown in UseCase onboarding.
    Only PortalAdmin can create/update/delete Data Accounts.
    """
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.scan()
        
        data_accounts = response.get('Items', [])
        
        # Sort by created_at descending
        data_accounts.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        
        return create_response(200, {
            'data_accounts': data_accounts,
            'count': len(data_accounts)
        })
        
    except Exception as e:
        logger.error(f"Error listing data accounts: {str(e)}")
        return create_response(500, {'error': 'Failed to list data accounts'})


def create_data_account(event: Dict, user: Dict) -> Dict:
    """Register a new Data Account"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        # Validate required fields
        required_fields = [
            'data_account_id',
            'name',
            'role_arn',
            'external_id'
        ]
        error = validate_required_fields(body, required_fields)
        if error:
            return create_response(400, {'error': error})
        
        data_account_id = body['data_account_id']
        
        # Check if Data Account already exists
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' in existing:
            return create_response(409, {'error': 'Data Account already registered'})
        
        # Test connection before registering
        test_result = test_data_account_connection(
            role_arn=body['role_arn'],
            external_id=body['external_id']
        )
        
        if test_result['status'] != 'success':
            return create_response(400, {
                'error': 'Failed to connect to Data Account',
                'details': test_result
            })
        
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            'data_account_id': data_account_id,
            'name': body['name'],
            'description': body.get('description', ''),
            'role_arn': body['role_arn'],
            'external_id': body['external_id'],
            'region': body.get('region', 'us-east-1'),
            'status': 'active',
            'created_at': timestamp,
            'created_by': user['user_id'],
            'updated_at': timestamp,
            'tags': body.get('tags', {}),
            'connection_test': test_result
        }
        
        table.put_item(Item=item)
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='create_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success',
            details={'name': body['name']}
        )
        
        return create_response(201, {
            'message': 'Data Account registered successfully',
            'data_account': item
        })
        
    except Exception as e:
        logger.error(f"Error creating data account: {str(e)}")
        return create_response(500, {'error': f'Failed to create data account: {str(e)}'})


def get_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Get Data Account details"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.get_item(Key={'data_account_id': data_account_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Data Account not found'})
        
        return create_response(200, {'data_account': response['Item']})
        
    except Exception as e:
        logger.error(f"Error getting data account: {str(e)}")
        return create_response(500, {'error': 'Failed to get data account'})


def update_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Update Data Account"""
    try:
        body = json.loads(event.get('body', '{}'))
        
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        
        # Check if exists
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' not in existing:
            return create_response(404, {'error': 'Data Account not found'})
        
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        
        # Build update expression
        update_expr = 'SET updated_at = :updated_at'
        expr_values = {':updated_at': timestamp}
        
        if 'name' in body:
            update_expr += ', #name = :name'
            expr_values[':name'] = body['name']
        
        if 'description' in body:
            update_expr += ', description = :description'
            expr_values[':description'] = body['description']
        
        if 'role_arn' in body:
            update_expr += ', role_arn = :role_arn'
            expr_values[':role_arn'] = body['role_arn']
        
        if 'external_id' in body:
            update_expr += ', external_id = :external_id'
            expr_values[':external_id'] = body['external_id']
        
        if 'status' in body:
            update_expr += ', #status = :status'
            expr_values[':status'] = body['status']
        
        if 'tags' in body:
            update_expr += ', tags = :tags'
            expr_values[':tags'] = body['tags']
        
        # Update item
        response = table.update_item(
            Key={'data_account_id': data_account_id},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames={'#name': 'name', '#status': 'status'} if 'name' in body or 'status' in body else None,
            ReturnValues='ALL_NEW'
        )
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='update_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success',
            details=body
        )
        
        return create_response(200, {
            'message': 'Data Account updated successfully',
            'data_account': response['Attributes']
        })
        
    except Exception as e:
        logger.error(f"Error updating data account: {str(e)}")
        return create_response(500, {'error': f'Failed to update data account: {str(e)}'})


def delete_data_account(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Delete Data Account"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        
        # Check if exists
        existing = table.get_item(Key={'data_account_id': data_account_id})
        if 'Item' not in existing:
            return create_response(404, {'error': 'Data Account not found'})
        
        # Delete item
        table.delete_item(Key={'data_account_id': data_account_id})
        
        # Log audit event
        log_audit_event(
            user_id=user['user_id'],
            action='delete_data_account',
            resource_type='data_account',
            resource_id=data_account_id,
            result='success'
        )
        
        return create_response(200, {'message': 'Data Account deleted successfully'})
        
    except Exception as e:
        logger.error(f"Error deleting data account: {str(e)}")
        return create_response(500, {'error': 'Failed to delete data account'})


def test_connection(event: Dict, user: Dict, data_account_id: str) -> Dict:
    """Test connection to Data Account"""
    try:
        table = dynamodb.Table(DATA_ACCOUNTS_TABLE)
        response = table.get_item(Key={'data_account_id': data_account_id})
        
        if 'Item' not in response:
            return create_response(404, {'error': 'Data Account not found'})
        
        data_account = response['Item']
        
        # Test connection
        test_result = test_data_account_connection(
            role_arn=data_account['role_arn'],
            external_id=data_account['external_id']
        )
        
        # Update connection test result
        table.update_item(
            Key={'data_account_id': data_account_id},
            UpdateExpression='SET connection_test = :test, last_tested_at = :tested_at',
            ExpressionAttributeValues={
                ':test': test_result,
                ':tested_at': int(datetime.utcnow().timestamp() * 1000)
            }
        )
        
        return create_response(200, {
            'message': 'Connection test complete',
            'result': test_result
        })
        
    except Exception as e:
        logger.error(f"Error testing connection: {str(e)}")
        return create_response(500, {'error': 'Failed to test connection'})


# --------------------------------------------------------------------------
# Bedrock_Configuration settings API (workflow-manager Requirement 10.6)
# --------------------------------------------------------------------------

def _decimal_to_native(obj):
    """Convert Decimal objects from DynamoDB to native Python types"""
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_native(i) for i in obj]
    return obj


def _native_to_dynamo(obj):
    """Convert native Python floats to Decimal for DynamoDB storage"""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: _native_to_dynamo(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_native_to_dynamo(i) for i in obj]
    return obj


def read_stored_bedrock_configuration() -> Dict:
    """
    Effective Bedrock_Configuration: stored values merged over defaults,
    timeout clamped to at most 240 seconds. Mirrors the read logic in
    workflow_generator.get_bedrock_configuration() so the settings UI
    shows exactly what the Workflow_Generator will use.
    """
    config = dict(DEFAULT_BEDROCK_CONFIG)
    if SETTINGS_TABLE:
        try:
            response = dynamodb.Table(SETTINGS_TABLE).get_item(
                Key={'setting_key': BEDROCK_CONFIG_SETTING_KEY}
            )
            item = response.get('Item')
            if item:
                stored = item.get('value') if isinstance(item.get('value'), dict) else item
                stored = _decimal_to_native(stored)
                for key in DEFAULT_BEDROCK_CONFIG:
                    if key in ('temperature', 'top_p'):
                        # Sampling parameters may be explicitly stored as
                        # null (unset); carry the null through so it reads
                        # back as unset instead of being masked by the
                        # default. Mirrors
                        # workflow_generator.get_bedrock_configuration().
                        if key in stored:
                            config[key] = stored[key]
                    elif stored.get(key) is not None:
                        config[key] = stored[key]
        except ClientError as e:
            logger.warning(f"Could not read Bedrock configuration, using defaults: {str(e)}")

    try:
        timeout = int(config['timeout_seconds'])
    except (TypeError, ValueError):
        timeout = MAX_BEDROCK_TIMEOUT_SECONDS
    config['timeout_seconds'] = max(1, min(timeout, MAX_BEDROCK_TIMEOUT_SECONDS))
    return config


def _is_number(value) -> bool:
    """True for int/float but not bool (bool is a subclass of int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_bedrock_configuration(config: Dict) -> List[str]:
    """
    Validate a complete Bedrock_Configuration value. Returns a list of
    human-readable validation errors (empty when valid).
    """
    errors = []

    model_id = config.get('model_id')
    if not isinstance(model_id, str) or not model_id.strip():
        errors.append('model_id must be a non-empty string')

    region = config.get('region')
    if not isinstance(region, str) or not region.strip():
        errors.append('region must be a non-empty string')

    max_tokens = config.get('max_tokens')
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        errors.append('max_tokens must be a positive integer')

    # None is a valid stored state for the sampling parameters (unset:
    # the parameter is omitted at invocation); non-None values must be
    # numbers in [0, 1].
    temperature = config.get('temperature')
    if temperature is not None and (
            not _is_number(temperature) or not (0 <= temperature <= 1)):
        errors.append('temperature must be a number between 0 and 1')

    top_p = config.get('top_p')
    if top_p is not None and (not _is_number(top_p) or not (0 <= top_p <= 1)):
        errors.append('top_p must be a number between 0 and 1')

    timeout_seconds = config.get('timeout_seconds')
    if (not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
            or not (1 <= timeout_seconds <= MAX_BEDROCK_TIMEOUT_SECONDS)):
        errors.append(
            f'timeout_seconds must be an integer between 1 and {MAX_BEDROCK_TIMEOUT_SECONDS}')

    return errors


def handle_bedrock_configuration(event: Dict, user: Dict, http_method: str) -> Dict:
    """
    Route Bedrock_Configuration requests. Restricted to PortalAdmin via
    Permission.BEDROCK_CONFIG_WRITE (Requirement 10.6); denied attempts
    are audit-logged.
    """
    if not rbac_manager.has_permission(
            user['user_id'], 'global', Permission.BEDROCK_CONFIG_WRITE, user_info=user):
        log_audit_event(
            user_id=user['user_id'],
            action='unauthorized_access',
            resource_type='setting',
            resource_id=BEDROCK_CONFIG_SETTING_KEY,
            result='denied',
            details={
                'required_permissions': [Permission.BEDROCK_CONFIG_WRITE.value],
                'method': http_method,
                'path': event.get('path'),
            }
        )
        return create_response(403, {
            'error': 'PortalAdmin access required',
            'required_permissions': [Permission.BEDROCK_CONFIG_WRITE.value],
        })

    # GET /data-accounts/bedrock-configuration/models: invokable model
    # options for the settings-page model dropdown. Read-gated exactly like
    # the configuration GET above (same PortalAdmin permission check).
    is_models_path = (event.get('path') or '').rstrip('/').endswith('/models')
    if http_method == 'GET' and is_models_path:
        return list_bedrock_model_options(event, user)

    # GET/PUT /data-accounts/bedrock-configuration/token-limits: the
    # Model_Token_Limits item (llm-model-token-and-image-sizing Requirement
    # 4). A sibling of the /models dispatch above and ahead of the bare
    # GET/PUT, so it inherits the PortalAdmin gate and its denied-attempt
    # audit entry exactly as an unauthorized configuration write does
    # (Requirement 4.3).
    is_token_limits_path = (
        (event.get('path') or '').rstrip('/').endswith('/token-limits'))
    if is_token_limits_path:
        return handle_model_token_limits(event, user, http_method)

    if http_method == 'GET':
        return get_bedrock_configuration_setting(event, user)
    if http_method == 'PUT':
        return update_bedrock_configuration_setting(event, user)
    return create_response(404, {'error': 'Not found'})


def get_bedrock_configuration_setting(event: Dict, user: Dict) -> Dict:
    """Return the effective Bedrock_Configuration (stored over defaults)."""
    try:
        if not SETTINGS_TABLE:
            return create_response(500, {'error': 'Settings storage is not configured'})
        return create_response(200, {
            'bedrock_configuration': read_stored_bedrock_configuration(),
            'defaults': DEFAULT_BEDROCK_CONFIG,
            'max_timeout_seconds': MAX_BEDROCK_TIMEOUT_SECONDS,
        })
    except Exception as e:
        logger.error(f"Error reading Bedrock configuration: {str(e)}")
        return create_response(500, {'error': 'Failed to read Bedrock configuration'})


def update_bedrock_configuration_setting(event: Dict, user: Dict) -> Dict:
    """
    Update the Bedrock_Configuration. Accepts any subset of the known
    keys; the provided values are merged over the current effective
    configuration, the merged result is validated, and the complete
    value is written in the exact shape workflow_generator.py reads:
        {setting_key: 'bedrock_configuration', value: {...}}
    """
    try:
        if not SETTINGS_TABLE:
            return create_response(500, {'error': 'Settings storage is not configured'})

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {'error': 'Request body is not valid JSON'})
        if not isinstance(body, dict):
            return create_response(400, {'error': 'Request body must be a JSON object'})

        # Merge provided keys over the current effective configuration;
        # unknown keys are ignored.
        config = read_stored_bedrock_configuration()
        for key in DEFAULT_BEDROCK_CONFIG:
            if key in body:
                config[key] = body[key]

        errors = validate_bedrock_configuration(config)
        if errors:
            return create_response(400, {
                'error': 'Invalid Bedrock configuration',
                'validation_errors': errors,
            })

        value = {key: config[key] for key in DEFAULT_BEDROCK_CONFIG}
        value['model_id'] = value['model_id'].strip()
        value['region'] = value['region'].strip()

        timestamp = int(datetime.utcnow().timestamp() * 1000)
        dynamodb.Table(SETTINGS_TABLE).put_item(Item={
            'setting_key': BEDROCK_CONFIG_SETTING_KEY,
            'value': _native_to_dynamo(value),
            'updated_at': timestamp,
            'updated_by': user['user_id'],
        })

        log_audit_event(
            user_id=user['user_id'],
            action='update_bedrock_configuration',
            resource_type='setting',
            resource_id=BEDROCK_CONFIG_SETTING_KEY,
            result='success',
            details=_native_to_dynamo(value),
        )

        return create_response(200, {
            'message': 'Bedrock configuration updated successfully',
            'bedrock_configuration': value,
        })

    except Exception as e:
        logger.error(f"Error updating Bedrock configuration: {str(e)}")
        return create_response(500, {'error': 'Failed to update Bedrock configuration'})


# --------------------------------------------------------------------------
# Model_Token_Limits settings API
# (llm-model-token-and-image-sizing Requirements 1.6, 1.8, 4.1 - 4.8)
# --------------------------------------------------------------------------

def _reset_model_token_limits_cache() -> None:
    """Drop the per-invocation Model_Token_Limits memo.

    Called at the top of handler() and after a successful write, so the
    mapping is re-read at the start of every invocation and immediately
    after it changes.
    """
    global _model_token_limits_cache
    _model_token_limits_cache = None


def _read_stored_model_token_limits() -> Optional[Dict[str, Any]]:
    """The persisted Model_Token_Limits mapping, or None when there is none.

    Returns None - meaning "fall back to the environment bootstrap" - when
    the settings table is not configured, the item is absent, the read
    fails, or the item's value is not a mapping. An empty persisted mapping
    is a real mapping and is returned as {} (Requirement 4.8), not as None.

    DynamoDB returns every number as Decimal; the value is run through
    _decimal_to_native so resolve_token_budget - which rejects non-int
    types by design (Requirement 2.8) - sees native ints.
    """
    if not SETTINGS_TABLE:
        return None
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': LLM_MODEL_TOKEN_LIMITS_SETTING_KEY}
        )
    except Exception as e:  # ClientError, table missing, throttling
        logger.warning(f"Could not read model token limits, falling back to "
                       f"the environment bootstrap: {str(e)}")
        return None

    item = response.get('Item')
    if not item:
        return None
    value = item.get('value')
    if not isinstance(value, dict):
        return None
    return _decimal_to_native(value)


def _env_model_token_limits() -> Dict[str, Any]:
    """The LLM_MODEL_TOKEN_LIMITS deploy-time bootstrap mapping.

    An absent, blank, malformed or non-object value resolves to an empty
    mapping, in which case every model resolves Model_Token_Limit_Default
    rather than erroring.
    """
    raw = (os.environ.get('LLM_MODEL_TOKEN_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_TOKEN_LIMITS is not valid JSON; using the '
                       'default Model_Token_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_model_token_limits() -> tuple:
    """(mapping, source) for the effective Model_Token_Limits, memoized.

    Source of truth is the persisted `llm_model_token_limits` settings item
    (Requirements 1.6, 4.1). When that item is absent, unreadable, or its
    value is not a mapping, the LLM_MODEL_TOKEN_LIMITS environment variable
    is used instead - the deploy-time bootstrap for an environment where no
    PortalAdmin has written the item yet. `source` is 'settings' or
    'environment' accordingly, so an administrator can see which is in
    force.

    WHOLE-MAPPING precedence, never a per-key merge: a merge would let an
    environment entry survive a deletion from the persisted mapping, which
    would contradict Requirement 4.1 ("retain no entry that the submitted
    mapping omits") and Requirement 4.8 (an empty mapping makes every model
    resolve the default). An empty persisted mapping is therefore honored
    as empty.

    Memoized PER INVOCATION - a module-level cache keyed by nothing and
    cleared at the top of handler() - not per container. Per-invocation is
    exactly the span over which the resolution must be self-consistent;
    caching across invocations would let a warm container serve a stale
    mapping after an administrator's write.
    """
    global _model_token_limits_cache
    if _model_token_limits_cache is None:
        stored = _read_stored_model_token_limits()
        if stored is not None:
            _model_token_limits_cache = (stored, 'settings')
        else:
            _model_token_limits_cache = (_env_model_token_limits(),
                                         'environment')
    return _model_token_limits_cache


def _llm_model_token_limits() -> Dict[str, Any]:
    """The effective Model_Token_Limits mapping (see _load_model_token_limits).

    The same loader shape the Preview_API and the Auto_Labeler carry, so all
    three read equal entries for equal persisted configuration
    (Requirements 1.6, 1.8).
    """
    return _load_model_token_limits()[0]


def validate_model_token_limits(value) -> List[str]:
    """Validate a submitted Model_Token_Limits mapping (Requirement 4.2).

    Rules, with every violation reported (nothing short-circuits past the
    mapping check, which is the one violation that makes the rest
    unevaluable):
      - the value is a mapping
      - at most MODEL_TOKEN_LIMITS_MAX_ENTRIES (200) entries
      - every key a non-empty string of at most 256 characters
      - every value a non-bool integer in [1, MODEL_TOKEN_LIMIT_CEILING]

    Booleans are classified as non-integers, consistently with
    resolve_token_budget. Model_Token_Limit_Ceiling applies here and to no
    field of the Bedrock_Configuration.

    Returns [] when valid.
    """
    if not isinstance(value, dict):
        return ['model_token_limits must be an object mapping model '
                'identifiers to integer token limits']

    errors: List[str] = []

    if len(value) > MODEL_TOKEN_LIMITS_MAX_ENTRIES:
        errors.append(
            f'model_token_limits must contain at most '
            f'{MODEL_TOKEN_LIMITS_MAX_ENTRIES} entries '
            f'(received {len(value)})')

    for model_identifier, limit in value.items():
        if (not isinstance(model_identifier, str)
                or not model_identifier
                or len(model_identifier) > MODEL_TOKEN_LIMITS_MAX_KEY_LENGTH):
            errors.append(
                f'model identifier must be a non-empty string of at most '
                f'{MODEL_TOKEN_LIMITS_MAX_KEY_LENGTH} characters')
        # Evaluated for every entry regardless of the key's validity, so a
        # single response enumerates every invalid element.
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not (1 <= limit <= MODEL_TOKEN_LIMIT_CEILING)):
            errors.append(
                f"limit for '{model_identifier}' must be an integer between "
                f"1 and {MODEL_TOKEN_LIMIT_CEILING}")

    return errors


def handle_model_token_limits(event: Dict, user: Dict,
                              http_method: str) -> Dict:
    """GET / PUT the Model_Token_Limits item.

    Routed from handle_bedrock_configuration, so the PortalAdmin gate
    (Permission.BEDROCK_CONFIG_WRITE) and its denied-attempt audit entry are
    inherited unchanged (Requirement 4.3).

    A PUT REPLACES the persisted mapping in its entirety - a plain put_item
    of the whole value, never an update expression that merges - so no entry
    the submission omits survives (Requirement 4.1), and the empty mapping
    persists as empty (Requirement 4.8). Nothing here reads or writes the
    `bedrock_configuration` item, which is how Requirement 4.4 holds; and
    update_bedrock_configuration_setting is not touched, which is how
    Requirement 4.7 holds.
    """
    if http_method == 'GET':
        return get_model_token_limits_setting(event, user)
    if http_method == 'PUT':
        return update_model_token_limits_setting(event, user)
    return create_response(404, {'error': 'Not found'})


def get_model_token_limits_setting(event: Dict, user: Dict) -> Dict:
    """Return the effective Model_Token_Limits plus its bounds and source."""
    try:
        limits, source = _load_model_token_limits()
        return create_response(200, {
            'model_token_limits': limits,
            'default': MODEL_TOKEN_LIMIT_DEFAULT,
            'ceiling': MODEL_TOKEN_LIMIT_CEILING,
            'source': source,
        })
    except Exception as e:
        logger.error(f"Error reading model token limits: {str(e)}")
        return create_response(500, {'error': 'Failed to read model token limits'})


def update_model_token_limits_setting(event: Dict, user: Dict) -> Dict:
    """Replace the persisted Model_Token_Limits with the submitted mapping.

    Request body is the wrapped form {"model_token_limits": {...}}. The
    whole item is written with put_item, so the persisted mapping is exactly
    the submitted mapping - no entry the submission omits survives
    (Requirement 4.1), and {} persists as empty (Requirement 4.8).
    """
    try:
        if not SETTINGS_TABLE:
            return create_response(500, {'error': 'Settings storage is not configured'})

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {'error': 'Request body is not valid JSON'})
        if not isinstance(body, dict):
            return create_response(400, {'error': 'Request body must be a JSON object'})

        limits = body.get('model_token_limits')

        errors = validate_model_token_limits(limits)
        if errors:
            # The entire change is rejected and nothing is written, so the
            # persisted mapping is left unchanged (Requirement 4.2).
            return create_response(400, {
                'error': 'Invalid model token limits',
                'validation_errors': errors,
            })

        value = dict(limits)
        timestamp = int(datetime.utcnow().timestamp() * 1000)
        # Whole-item replacement, never a merging update expression.
        dynamodb.Table(SETTINGS_TABLE).put_item(Item={
            'setting_key': LLM_MODEL_TOKEN_LIMITS_SETTING_KEY,
            'value': value,
            'updated_at': timestamp,
            'updated_by': user['user_id'],
        })
        # The mapping just changed; drop the per-invocation memo so any
        # later read in this invocation sees the write.
        _reset_model_token_limits_cache()

        log_audit_event(
            user_id=user['user_id'],
            action='update_model_token_limits',
            resource_type='setting',
            resource_id=LLM_MODEL_TOKEN_LIMITS_SETTING_KEY,
            result='success',
            details={
                'model_token_limits': value,
                'entry_count': len(value),
            }
        )

        return create_response(200, {
            'message': 'Model token limits updated successfully',
            'model_token_limits': value,
        })

    except Exception as e:
        logger.error(f"Error updating model token limits: {str(e)}")
        return create_response(500, {'error': 'Failed to update model token limits'})


# --------------------------------------------------------------------------
# Camera_Registry Staleness_Threshold settings API
# (camera-registry-sync task 6.5, Requirement 4.3)
# --------------------------------------------------------------------------

def read_stored_camera_staleness_threshold() -> float:
    """The effective Staleness_Threshold in hours (stored over default).

    Mirrors the read logic in camera_registry.staleness_threshold_hours()
    and deployments._camera_staleness_threshold_ms() so the settings API
    shows exactly what the cameras route will use.
    """
    if SETTINGS_TABLE:
        try:
            response = dynamodb.Table(SETTINGS_TABLE).get_item(
                Key={'setting_key': CAMERA_STALENESS_SETTING_KEY}
            )
            value = (response.get('Item') or {}).get('value')
            if value is not None:
                hours = float(value)
                if hours > 0:
                    return hours
        except (ClientError, TypeError, ValueError) as e:
            logger.warning(f"Could not read staleness threshold setting: {e}")
    return DEFAULT_CAMERA_STALENESS_THRESHOLD_HOURS


def validate_camera_staleness_threshold(hours) -> List[str]:
    """Positive finite number of hours; returns human-readable errors."""
    if (not _is_number(hours)
            or not math.isfinite(hours)
            or hours <= 0):
        return ['staleness_threshold_hours must be a positive number of hours']
    return []


def handle_camera_registry_configuration(event: Dict, user: Dict,
                                         http_method: str) -> Dict:
    """
    Route Camera_Registry configuration requests. Restricted to
    PortalAdmin (camera-registry-sync Requirement 4.3); denied attempts
    are audit-logged.
    """
    if not is_portal_admin(user):
        log_audit_event(
            user_id=user['user_id'],
            action='unauthorized_access',
            resource_type='setting',
            resource_id=CAMERA_STALENESS_SETTING_KEY,
            result='denied',
            details={
                'required_role': 'PortalAdmin',
                'method': http_method,
                'path': event.get('path'),
            }
        )
        return create_response(403, {'error': 'PortalAdmin access required'})

    if http_method == 'GET':
        return get_camera_registry_configuration_setting(event, user)
    if http_method == 'PUT':
        return update_camera_registry_configuration_setting(event, user)
    return create_response(404, {'error': 'Not found'})


def get_camera_registry_configuration_setting(event: Dict, user: Dict) -> Dict:
    """Return the effective Staleness_Threshold (stored over default)."""
    try:
        if not SETTINGS_TABLE:
            return create_response(500, {'error': 'Settings storage is not configured'})
        return create_response(200, {
            'staleness_threshold_hours': read_stored_camera_staleness_threshold(),
            'default_staleness_threshold_hours':
                DEFAULT_CAMERA_STALENESS_THRESHOLD_HOURS,
        })
    except Exception as e:
        logger.error(f"Error reading camera registry configuration: {str(e)}")
        return create_response(500, {'error': 'Failed to read camera registry configuration'})


def update_camera_registry_configuration_setting(event: Dict, user: Dict) -> Dict:
    """
    Update the Staleness_Threshold. Validates a positive number of hours
    and writes the exact item shape read by
    camera_registry.staleness_threshold_hours():
        {setting_key: 'camera_registry.staleness_threshold_hours',
         value: <hours>}
    """
    try:
        if not SETTINGS_TABLE:
            return create_response(500, {'error': 'Settings storage is not configured'})

        try:
            body = json.loads(event.get('body') or '{}')
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {'error': 'Request body is not valid JSON'})
        if not isinstance(body, dict):
            return create_response(400, {'error': 'Request body must be a JSON object'})

        hours = body.get('staleness_threshold_hours')
        errors = validate_camera_staleness_threshold(hours)
        if errors:
            return create_response(400, {
                'error': 'Invalid camera registry configuration',
                'validation_errors': errors,
            })

        timestamp = int(datetime.utcnow().timestamp() * 1000)
        dynamodb.Table(SETTINGS_TABLE).put_item(Item={
            'setting_key': CAMERA_STALENESS_SETTING_KEY,
            'value': _native_to_dynamo(hours),
            'updated_at': timestamp,
            'updated_by': user['user_id'],
        })

        log_audit_event(
            user_id=user['user_id'],
            action='update_camera_registry_configuration',
            resource_type='setting',
            resource_id=CAMERA_STALENESS_SETTING_KEY,
            result='success',
            details={'staleness_threshold_hours': _native_to_dynamo(hours)},
        )

        return create_response(200, {
            'message': 'Camera registry configuration updated successfully',
            'staleness_threshold_hours': hours,
        })

    except Exception as e:
        logger.error(f"Error updating camera registry configuration: {str(e)}")
        return create_response(500, {'error': 'Failed to update camera registry configuration'})


# --------------------------------------------------------------------------
# Bedrock model options (settings-page model dropdown)
# --------------------------------------------------------------------------

def _get_bedrock_control_client(region: str):
    """bedrock control-plane client (list APIs), cached per region."""
    client = _bedrock_control_clients.get(region)
    if client is None:
        client = boto3.client('bedrock', region_name=region)
        _bedrock_control_clients[region] = client
    return client


def _llm_model_image_limits() -> Dict[str, Any]:
    """
    The Model_Image_Limit configuration mapping from LLM_MODEL_IMAGE_LIMITS
    (llm-autolabel-prompt-tuning Requirement 7.1).

    Read per call so the environment stays authoritative. An absent, blank,
    malformed, or non-object value resolves to an empty mapping, in which case
    every model resolves the shared default of 20 rather than erroring.
    """
    raw = (os.environ.get('LLM_MODEL_IMAGE_LIMITS') or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('LLM_MODEL_IMAGE_LIMITS is not valid JSON; '
                       'using the default Model_Image_Limit for every model')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_access_denied(error: ClientError) -> bool:
    code = error.response.get('Error', {}).get('Code', '')
    return code in ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation')


def _list_inference_profiles(client) -> List[Dict]:
    """All system-defined inference profile summaries (paginated)."""
    summaries: List[Dict] = []
    next_token = None
    while True:
        kwargs = {'maxResults': 100}
        if next_token:
            kwargs['nextToken'] = next_token
        response = client.list_inference_profiles(**kwargs)
        summaries.extend(response.get('inferenceProfileSummaries', []))
        next_token = response.get('nextToken')
        if not next_token:
            return summaries


def list_bedrock_model_options(event: Dict, user: Dict) -> Dict:
    """
    GET /data-accounts/bedrock-configuration/models

    Invokable model options for the settings-page model dropdown, resolved
    for the configured region (or ?region=... override):

    - Inference profiles (bedrock:ListInferenceProfiles): these are the
      invokable ids for current Anthropic models (prefixed us./global./...).
      Listed as {id: inferenceProfileId, label: inferenceProfileName}.
    - Foundation models (bedrock:ListFoundationModels) with
      modelLifecycle.status == ACTIVE, included only when directly
      invokable (inferenceTypesSupported contains ON_DEMAND).

    Deduplicated (an inference profile wins over the foundation model it
    fronts) and sorted anthropic-first, then alphabetically. When the
    Lambda lacks the bedrock list permissions, returns an empty list plus
    a 'permissions' hint so the UI can fall back to free-text entry.

    Each option also carries an additive 'image_limit': the model's
    Model_Image_Limit resolved from LLM_MODEL_IMAGE_LIMITS through the
    shared resolve_model_image_limit (default 20), for the labeling-job
    wizard's few-shot attach/omit hint (llm-autolabel-prompt-tuning
    Requirements 7.1, 7.5).

    And an additive 'token_limit': the model's Effective_Token_Budget with
    no Token_Budget_Selection, resolved through the shared
    resolve_token_budget against the effective Model_Token_Limits (default
    10000), which is what the labeling-job wizard pre-fills
    (llm-model-token-and-image-sizing Requirements 1.6, 3.1).
    """
    try:
        query = event.get('queryStringParameters') or {}
        region = (query.get('region') or '').strip() \
            or read_stored_bedrock_configuration()['region']
        client = _get_bedrock_control_client(region)

        access_denied = False
        profile_options: List[Dict] = []
        try:
            for profile in _list_inference_profiles(client):
                profile_id = profile.get('inferenceProfileId')
                if profile_id:
                    profile_options.append({
                        'id': profile_id,
                        'label': profile.get('inferenceProfileName') or profile_id,
                    })
        except ClientError as e:
            if not _is_access_denied(e):
                raise
            access_denied = True

        # Foundation model ids fronted by a listed inference profile are
        # dropped: the profile id (e.g. us.anthropic....) is the invokable
        # one for those models. Profile ids are '<prefix>.<model-id>'.
        profile_base_ids = {
            option['id'].split('.', 1)[1]
            for option in profile_options if '.' in option['id']
        }

        model_options: List[Dict] = []
        try:
            response = client.list_foundation_models()
            for summary in response.get('modelSummaries', []):
                model_id = summary.get('modelId')
                if not model_id:
                    continue
                if summary.get('modelLifecycle', {}).get('status') != 'ACTIVE':
                    continue
                if 'ON_DEMAND' not in (summary.get('inferenceTypesSupported') or []):
                    continue
                if model_id in profile_base_ids:
                    continue
                model_options.append({
                    'id': model_id,
                    'label': summary.get('modelName') or model_id,
                })
        except ClientError as e:
            if not _is_access_denied(e):
                raise
            access_denied = True

        def sort_key(option: Dict):
            model_id = option['id']
            base_id = model_id.split('.', 1)[1] if '.' in model_id else model_id
            is_anthropic = (base_id.startswith('anthropic.')
                            or model_id.startswith('anthropic.'))
            return (0 if is_anthropic else 1, option['label'].lower(), model_id)

        # Deduplicate by id (profiles first so they win), then sort.
        seen = set()
        options = []
        for option in profile_options + model_options:
            if option['id'] not in seen:
                seen.add(option['id'])
                options.append(option)
        options.sort(key=sort_key)

        # Additive per-option Model_Image_Limit so the labeling-job wizard's
        # few-shot attach/omit hint reads the same configuration the request
        # paths resolve (llm-autolabel-prompt-tuning Requirements 7.1, 7.5).
        # Every other field of every option is left exactly as it was, so
        # consumers that ignore image_limit are unaffected.
        # Additive per-option Effective_Token_Budget with no selection - what
        # the labeling-job wizard pre-fills for the selected model, resolved
        # from the same persisted Model_Token_Limits the request paths read,
        # so the displayed budget equals every request's maxTokens
        # (llm-model-token-and-image-sizing Requirements 1.6, 3.1).
        image_limits = _llm_model_image_limits()
        token_limits = _llm_model_token_limits()
        for option in options:
            option['image_limit'] = resolve_model_image_limit(
                option['id'], image_limits)
            option['token_limit'] = resolve_token_budget(
                option['id'], None, token_limits)

        payload: Dict[str, Any] = {'models': options, 'region': region}
        if access_denied:
            payload['permissions'] = (
                'Missing bedrock:ListInferenceProfiles and/or '
                'bedrock:ListFoundationModels permission; enter the model '
                'id manually.'
            )
        return create_response(200, payload)

    except Exception as e:
        logger.error(f"Error listing Bedrock model options: {str(e)}")
        return create_response(500, {'error': 'Failed to list Bedrock model options'})
