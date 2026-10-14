"""
Device_Registration handler for Station Quick Setup (JWT-authenticated).

Portal users with the manage-devices permission register a pending Station
here; the handler validates the request, verifies the device name is unique in
the Use_Case account, mints a single-use Setup_Token (see token_service), and
returns a one-line Setup_Command the operator runs on the station.

Follows the devices.py handler conventions: shared_utils imports, CORS
preflight, create_response, cross-account role assumption for IoT lookups.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 2.1, 2.2, 2.3, 2.7, 6.3
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import token_service
from shared_utils import (
    Permission,
    assume_cross_account_role,
    check_user_access,
    create_boto3_client,
    create_response,
    get_user_from_event,
    get_usecase,
    is_super_user,
    log_audit_event,
    rbac_manager,
    record_audit_event_strict,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')

# Registrations table (PK registration_id) with the usecase-device-index GSI
# (PK usecase_id, SK device_name) used for the per-Use_Case uniqueness check.
REGISTRATIONS_TABLE = os.environ.get('REGISTRATIONS_TABLE')
USECASE_DEVICE_INDEX = 'usecase-device-index'

# SHA-256 of the bootstrap script, baked into the Lambda environment at deploy
# time. It is the integrity anchor embedded in the Setup_Command so the station
# can verify the bootstrap it downloads before executing it (Req 4.8 chain).
QUICK_SETUP_BOOTSTRAP_SHA256 = os.environ.get('QUICK_SETUP_BOOTSTRAP_SHA256', '')

# Valid IoT Thing / Thing Group name (Req 1.2): 1-128 chars of the IoT-allowed
# alphabet. Shared with the frontend field validation and tested directly.
IOT_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9:_-]{1,128}$')

# The device-name and Device_Group fields validated against IOT_NAME_PATTERN.
_PATTERN_FIELDS = ('device_name', 'device_group')

# All fields required to be present and non-empty (Req 1.9).
_REQUIRED_FIELDS = ('device_name', 'device_group', 'usecase_id')

# Upper bound on the generated Setup_Command length (Req 2.2).
MAX_COMMAND_LENGTH = 2048


def _registrations_table():
    """The registrations DynamoDB table resource."""
    return dynamodb.Table(REGISTRATIONS_TABLE)


def handler(event, context):
    """Handle device-registration requests.

    POST /device-registrations - register a device and return a Setup_Command.
    GET  /device-registrations?usecase_id= - list registrations (status +
         token_expires_at, never token material).
    GET  /device-registrations/thing-groups?usecase_id= - list existing IoT
         Thing Groups in the Use_Case account for Device_Group selection.
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')
        query_params = event.get('queryStringParameters') or {}
        logger.info(f"DeviceRegistrations request: {http_method} {path}")

        # CORS preflight (matches devices.py).
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400',
                },
                'body': '',
            }

        user = get_user_from_event(event)

        path_parameters = event.get('pathParameters') or {}

        if http_method == 'POST' and path.endswith('/device-registrations'):
            body = json.loads(event.get('body') or '{}')
            return create_registration(user, body, event)

        # POST /device-registrations/{id}/command - regenerate the Setup_Command.
        if http_method == 'POST' and path.endswith('/command'):
            body = json.loads(event.get('body') or '{}')
            return regenerate_command(
                user, path_parameters.get('id'), body, event)

        # DELETE /device-registrations/{id} - delete a (non-completed) registration.
        if http_method == 'DELETE':
            return delete_registration(user, path_parameters.get('id'))

        if http_method == 'GET' and path.endswith('/device-registrations/thing-groups'):
            return list_thing_groups(user, query_params)

        if http_method == 'GET' and path.endswith('/device-registrations'):
            return list_registrations(user, query_params)

        return create_response(404, {'error': 'Not found'})

    except Exception as e:
        logger.error(f"Error in device_registrations handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


def create_registration(user, body, event):
    """POST /device-registrations

    Body: {"device_name": str, "device_group": str, "usecase_id": str}

    1. Collect ALL missing/empty fields (Req 1.9).
    2. Collect ALL pattern-invalid fields (Req 1.2).
    3. RBAC Permission.MANAGE_DEVICES; on denial record a strict audit event
       and fail the whole operation if that write raises (Req 1.4, 1.5).
    4. Verify device-name uniqueness via cross-account iot.describe_thing and a
       GSI query; reject on conflict (Req 1.3) or on any lookup failure
       (Req 1.10).
    5. Generate the Setup_Token and conditionally put the item so no token-less
       registration ever persists (Req 2.7).
    6. Build and return the one-line HTTPS Setup_Command (Req 2.1-2.3).
    """
    # 1. Presence of all required fields (Req 1.9).
    missing_fields = [
        field for field in _REQUIRED_FIELDS
        if not str(body.get(field) or '').strip()
    ]
    if missing_fields:
        return create_response(400, {
            'error': 'Missing required fields',
            'missing_fields': missing_fields,
        })

    device_name = body['device_name'].strip()
    device_group = body['device_group'].strip()
    usecase_id = body['usecase_id'].strip()

    # 2. Pattern validation of the IoT-name fields (Req 1.2).
    invalid_fields = [
        field for field in _PATTERN_FIELDS
        if not IOT_NAME_PATTERN.match(body[field].strip())
    ]
    if invalid_fields:
        return create_response(400, {
            'error': 'Invalid field values',
            'invalid_fields': invalid_fields,
            'pattern': IOT_NAME_PATTERN.pattern,
        })

    # 3. RBAC: manage-devices for this Use_Case (Req 1.4). On denial, record a
    #    strict audit event; if the audit write raises, fail the whole
    #    operation (Req 1.5).
    if not is_super_user(user['user_id']) and not rbac_manager.has_permission(
            user['user_id'], usecase_id, Permission.MANAGE_DEVICES,
            user_info=user):
        try:
            record_audit_event_strict(
                user['user_id'], 'create_device_registration',
                'device_registration', device_name, result='rejected',
                details={'reason': 'access_denied', 'usecase_id': usecase_id,
                         'required_permission': Permission.MANAGE_DEVICES.value},
            )
        except Exception as audit_error:
            logger.error(
                f"Audit write failed for denied device registration: {audit_error}",
                exc_info=True)
            return create_response(500, {
                'error': 'Registration failed: audit event could not be recorded'})
        return create_response(403, {'error': 'Access denied'})

    # Resolve the Use_Case for cross-account uniqueness verification.
    usecase = get_usecase(usecase_id)
    if not usecase:
        return create_response(404, {'error': 'Use case not found'})

    # 4. Uniqueness verification (Req 1.3, 1.10).
    conflict, verification_error = _verify_device_name_available(
        usecase, usecase_id, device_name)
    if verification_error:
        return verification_error
    if conflict:
        return create_response(409, {
            'error': 'A device with this name already exists',
            'device_name': device_name,
        })

    # 5. Generate the token and persist the registration atomically. No
    #    token-less registration may ever persist (Req 2.7).
    registration_id = str(uuid.uuid4())
    now = int(datetime.utcnow().timestamp())
    try:
        token, token_hash, token_expires_at = token_service.generate_token(
            registration_id, now=now)
    except Exception as token_error:
        logger.error(f"Setup_Token generation failed: {token_error}", exc_info=True)
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    item = {
        'registration_id': registration_id,
        'usecase_id': usecase_id,
        'device_name': device_name,       # GSI SK
        'device_group': device_group,     # Req 1.8 (new group name accepted as-is)
        'status': 'pending',              # Req 1.1
        'created_by': user['user_id'],    # Req 1.6
        'created_at': now,                # Req 1.6
        'updated_at': now,
        'token_hash': token_hash,         # hash ONLY (Req 3.6)
        'token_expires_at': token_expires_at,
        'token_generation': 1,
        'consumed_at': 0,
    }

    try:
        _registrations_table().put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(registration_id)',
        )
    except Exception as put_error:
        # Storage failure -> nothing persisted, no Setup_Command issued
        # (Req 2.7).
        logger.error(f"Registration persistence failed: {put_error}", exc_info=True)
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    # 6. Build the one-line Setup_Command (Req 2.1-2.3).
    try:
        setup_command = _build_setup_command(event, token)
    except ValueError as command_error:
        logger.error(f"Setup_Command build failed: {command_error}")
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    return create_response(201, {
        'registration': _public_registration(item),
        'setup_command': setup_command,
        'token_expires_at': token_expires_at,
    })


def list_registrations(user, query_params):
    """GET /device-registrations?usecase_id=

    List the Device_Registrations for a Use_Case with their Setup_Status and
    token expiration, so the devices view can display each registration and,
    while pending/in_progress, its expiry time (Req 6.3). Token material is
    never returned — only ``token_expires_at`` and the hash-free public view.
    """
    usecase_id = (query_params.get('usecase_id') or '').strip()
    if not usecase_id:
        return create_response(400, {'error': 'usecase_id parameter required'})

    # Access check: a user must have access to the Use_Case to view its
    # registrations (matches devices.py list_devices).
    if not is_super_user(user['user_id']) and not check_user_access(
            user['user_id'], usecase_id):
        log_audit_event(
            user['user_id'], 'list_device_registrations', 'device_registration',
            'all', 'failure',
            {'reason': 'access_denied', 'usecase_id': usecase_id},
        )
        return create_response(403, {'error': 'Access denied'})

    registrations = []
    try:
        query_kwargs = {
            'IndexName': USECASE_DEVICE_INDEX,
            'KeyConditionExpression': Key('usecase_id').eq(usecase_id),
        }
        while True:
            response = _registrations_table().query(**query_kwargs)
            for item in response.get('Items', []):
                registrations.append(_public_registration(item))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            query_kwargs['ExclusiveStartKey'] = last_key
    except Exception as e:
        logger.error(f"Failed to list registrations for {usecase_id}: {e}",
                     exc_info=True)
        return create_response(500, {'error': 'Failed to list device registrations'})

    return create_response(200, {
        'registrations': registrations,
        'count': len(registrations),
    })


def list_thing_groups(user, query_params):
    """GET /device-registrations/thing-groups?usecase_id=

    Return the existing IoT Thing Groups from the Use_Case account so the
    portal can present them for Device_Group selection (Req 1.7). A
    cross-account ``iot.list_thing_groups`` pass-through.
    """
    usecase_id = (query_params.get('usecase_id') or '').strip()
    if not usecase_id:
        return create_response(400, {'error': 'usecase_id parameter required'})

    if not is_super_user(user['user_id']) and not check_user_access(
            user['user_id'], usecase_id):
        log_audit_event(
            user['user_id'], 'list_thing_groups', 'thing_group', 'all',
            'failure', {'reason': 'access_denied', 'usecase_id': usecase_id},
        )
        return create_response(403, {'error': 'Access denied'})

    usecase = get_usecase(usecase_id)
    if not usecase:
        return create_response(404, {'error': 'Use case not found'})

    region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))

    try:
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'], usecase['external_id'])
        iot_client = create_boto3_client('iot', credentials, region)

        thing_groups = []
        next_token = None
        while True:
            params = {'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token
            response = iot_client.list_thing_groups(**params)
            for group in response.get('thingGroups', []):
                thing_groups.append({
                    'group_name': group.get('groupName'),
                    'group_arn': group.get('groupArn'),
                })
            next_token = response.get('nextToken')
            if not next_token:
                break
    except ClientError as e:
        logger.error(f"AWS error listing thing groups for {usecase_id}: {e}")
        return create_response(502, {'error': 'Failed to list thing groups'})
    except Exception as e:
        logger.error(f"Failed to list thing groups for {usecase_id}: {e}",
                     exc_info=True)
        return create_response(502, {'error': 'Failed to list thing groups'})

    return create_response(200, {
        'thing_groups': thing_groups,
        'count': len(thing_groups),
    })


def regenerate_command(user, registration_id, body, event):
    """POST /device-registrations/{id}/command

    Issue a fresh Setup_Command for an existing Device_Registration.

    Reject when the Setup_Status is ``completed`` (Req 2.8). Otherwise mint a
    new Setup_Token and atomically replace ``token_hash``/``token_expires_at``
    in a single ``UpdateItem`` so at most one Setup_Token is ever valid per
    registration (Req 2.5). The prior token's hash is overwritten in the same
    write, invalidating it. ``consumed_at`` is cleared so the new token is
    usable, and an ``expired``/``failed`` registration is reset to ``pending``.
    """
    if not registration_id:
        return create_response(400, {'error': 'registration_id required'})

    registration = _get_registration(registration_id)
    if not registration:
        return create_response(404, {'error': 'Device registration not found'})

    usecase_id = registration['usecase_id']

    # RBAC: manage-devices for this Use_Case. On denial, record a strict audit
    # event and fail the operation if that write raises (Req 1.4/1.5 pattern).
    denied = _require_manage_devices(
        user, usecase_id, 'regenerate_setup_command',
        registration['device_name'])
    if denied:
        return denied

    # Reject regeneration for a completed registration (Req 2.8).
    if registration['status'] == 'completed':
        return create_response(409, {
            'error': 'Registration is already completed',
            'registration_id': registration_id,
        })

    now = int(datetime.utcnow().timestamp())
    try:
        token, token_hash, token_expires_at = token_service.generate_token(
            registration_id, now=now)
    except Exception as token_error:
        logger.error(f"Setup_Token generation failed: {token_error}",
                     exc_info=True)
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    # Reset expired/failed registrations back to pending (Req 2.5); leave any
    # other non-completed status untouched.
    new_status = ('pending' if registration['status'] in ('expired', 'failed')
                  else registration['status'])

    # Atomically replace the token material in a single UpdateItem so at most
    # one Setup_Token is valid at any time (Req 2.5). The condition guards
    # against a concurrent transition to completed (Req 2.8).
    try:
        response = _registrations_table().update_item(
            Key={'registration_id': registration_id},
            UpdateExpression=(
                'SET token_hash = :th, token_expires_at = :exp, '
                'token_generation = token_generation + :one, '
                'consumed_at = :zero, updated_at = :now, #s = :status '
                'REMOVE report_secret_hash'
            ),
            ConditionExpression=(
                'attribute_exists(registration_id) AND #s <> :completed'
            ),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':th': token_hash,
                ':exp': token_expires_at,
                ':one': 1,
                ':zero': 0,
                ':now': now,
                ':status': new_status,
                ':completed': 'completed',
            },
            ReturnValues='ALL_NEW',
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            # Raced with a completed transition (Req 2.8) or a deletion.
            return create_response(409, {
                'error': 'Registration is already completed',
                'registration_id': registration_id,
            })
        logger.error(f"Setup_Command regeneration failed: {e}", exc_info=True)
        return create_response(500, {
            'error': 'Setup command could not be generated'})
    except Exception as e:
        logger.error(f"Setup_Command regeneration failed: {e}", exc_info=True)
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    updated_item = response['Attributes']

    try:
        setup_command = _build_setup_command(event, token)
    except ValueError as command_error:
        logger.error(f"Setup_Command build failed: {command_error}")
        return create_response(500, {
            'error': 'Setup command could not be generated'})

    return create_response(200, {
        'registration': _public_registration(updated_item),
        'setup_command': setup_command,
        'token_expires_at': token_expires_at,
    })


def delete_registration(user, registration_id):
    """DELETE /device-registrations/{id}

    Delete a Device_Registration whose Setup_Status is not ``completed``
    (Req 6.6). Deleting the item invalidates the associated Setup_Token,
    because token validation resolves through the registration item — once it
    is gone, the embedded registration id no longer loads (Req 6.6).

    Reject completed registrations (Req 6.9); they must remain unchanged.
    """
    if not registration_id:
        return create_response(400, {'error': 'registration_id required'})

    registration = _get_registration(registration_id)
    if not registration:
        return create_response(404, {'error': 'Device registration not found'})

    usecase_id = registration['usecase_id']

    denied = _require_manage_devices(
        user, usecase_id, 'delete_device_registration',
        registration['device_name'])
    if denied:
        return denied

    # Reject deletion of a completed registration (Req 6.9).
    if registration['status'] == 'completed':
        return create_response(409, {
            'error': 'Completed registrations cannot be deleted',
            'registration_id': registration_id,
        })

    # Delete the item, invalidating the token via the item lookup (Req 6.6).
    # The condition guards against a concurrent transition to completed so a
    # completed registration is never removed (Req 6.9).
    try:
        _registrations_table().delete_item(
            Key={'registration_id': registration_id},
            ConditionExpression=(
                'attribute_exists(registration_id) AND #s <> :completed'
            ),
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':completed': 'completed'},
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            # Raced with a completed transition (Req 6.9) or a prior deletion.
            return create_response(409, {
                'error': 'Completed registrations cannot be deleted',
                'registration_id': registration_id,
            })
        logger.error(f"Registration deletion failed: {e}", exc_info=True)
        return create_response(500, {
            'error': 'Failed to delete device registration'})
    except Exception as e:
        logger.error(f"Registration deletion failed: {e}", exc_info=True)
        return create_response(500, {
            'error': 'Failed to delete device registration'})

    return create_response(200, {
        'deleted': True,
        'registration_id': registration_id,
    })


def _get_registration(registration_id):
    """Primary-key read of a single registration item, or ``None`` if absent."""
    response = _registrations_table().get_item(
        Key={'registration_id': registration_id})
    return response.get('Item')


def _require_manage_devices(user, usecase_id, action, device_name):
    """Enforce the manage-devices permission for a Use_Case.

    Returns an access-denied response when the user lacks the permission
    (recording a strict audit event first, and failing the operation if that
    write raises, per the Req 1.4/1.5 pattern), or ``None`` when access is
    allowed.
    """
    if is_super_user(user['user_id']) or rbac_manager.has_permission(
            user['user_id'], usecase_id, Permission.MANAGE_DEVICES,
            user_info=user):
        return None

    try:
        record_audit_event_strict(
            user['user_id'], action, 'device_registration', device_name,
            result='rejected',
            details={'reason': 'access_denied', 'usecase_id': usecase_id,
                     'required_permission': Permission.MANAGE_DEVICES.value},
        )
    except Exception as audit_error:
        logger.error(
            f"Audit write failed for denied {action}: {audit_error}",
            exc_info=True)
        return create_response(500, {
            'error': 'Operation failed: audit event could not be recorded'})
    return create_response(403, {'error': 'Access denied'})


def _verify_device_name_available(usecase, usecase_id, device_name):
    """Verify the device name is unused in the Use_Case account and among
    existing registrations.

    Returns ``(conflict, error_response)``:
    - ``(True, None)``  the name is already taken (Req 1.3).
    - ``(False, None)`` the name is available.
    - ``(False, <500-ish response>)`` uniqueness could not be verified
      (Req 1.10) — the caller must reject without persisting anything.
    """
    region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))

    # (a) Cross-account IoT Thing lookup. describe_thing must raise
    #     ResourceNotFoundException for the name to be available; a successful
    #     lookup is a conflict, and any other failure (role assumption or the
    #     lookup itself) means uniqueness is unverifiable (Req 1.10).
    try:
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'], usecase['external_id'])
        iot_client = create_boto3_client('iot', credentials, region)
        iot_client.describe_thing(thingName=device_name)
        # The thing exists -> conflict (Req 1.3).
        return True, None
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
            logger.error(f"IoT uniqueness lookup failed for {device_name}: {e}")
            return False, create_response(502, {
                'error': 'Could not verify device name uniqueness in the use case account'})
        # ResourceNotFoundException -> the name is free in the account.
    except Exception as e:
        logger.error(f"Cross-account access failed during uniqueness check: {e}",
                     exc_info=True)
        return False, create_response(502, {
            'error': 'Could not verify device name uniqueness in the use case account'})

    # (b) Existing-registration lookup via the usecase-device-index GSI. Any
    #     non-deleted registration with the same (usecase_id, device_name) is a
    #     conflict; a query failure means uniqueness is unverifiable (Req 1.10).
    try:
        response = _registrations_table().query(
            IndexName=USECASE_DEVICE_INDEX,
            KeyConditionExpression=(
                Key('usecase_id').eq(usecase_id)
                & Key('device_name').eq(device_name)
            ),
        )
    except Exception as e:
        logger.error(f"Registration uniqueness query failed for {device_name}: {e}",
                     exc_info=True)
        return False, create_response(502, {
            'error': 'Could not verify device name uniqueness'})

    if response.get('Items'):
        return True, None

    return False, None


def _build_setup_command(event, token):
    """Build the one-line HTTPS Setup_Command embedding the Setup_Token.

    The endpoint is derived from the incoming request so the command always
    points at the deployment that generated it (Req 2.1). The bootstrap SHA-256
    is baked in at deploy time, making the command the integrity anchor for the
    download chain. Emitted as a single line, HTTPS-only (Req 2.2, 2.3).
    """
    api_base = _api_base_url(event)
    bootstrap_url = f"{api_base}/quick-setup/bootstrap"
    endpoint = f"{api_base}/quick-setup"

    command = (
        f"curl -fsSL {bootstrap_url} -o /tmp/dda-qs.sh && "
        f'echo "{QUICK_SETUP_BOOTSTRAP_SHA256}  /tmp/dda-qs.sh" | sha256sum -c - && '
        f"sudo bash /tmp/dda-qs.sh --endpoint {endpoint} --token {token}"
    )

    if len(command) > MAX_COMMAND_LENGTH:
        raise ValueError(
            f"Setup_Command length {len(command)} exceeds {MAX_COMMAND_LENGTH}")
    return command


def _api_base_url(event):
    """Resolve the HTTPS base URL of this deployment from the request context.

    Uses ``requestContext.domainName`` and ``requestContext.stage`` so the URL
    reflects the deployment that served the request (no config circularity,
    Req 2.1). An explicit ``QUICK_SETUP_API_URL`` override wins when set.
    """
    override = os.environ.get('QUICK_SETUP_API_URL')
    if override:
        return override.rstrip('/')

    request_context = event.get('requestContext') or {}
    domain_name = request_context.get('domainName')
    stage = request_context.get('stage')
    if not domain_name:
        raise ValueError('Cannot resolve API endpoint from request context')

    base = f"https://{domain_name}"
    if stage:
        base = f"{base}/{stage}"
    return base


def _public_registration(item):
    """A registration dict safe to return to the portal user — never token
    material (Req 8.3). Only the hash is stored, but it is dropped from
    responses defensively."""
    return {
        'registration_id': item['registration_id'],
        'usecase_id': item['usecase_id'],
        'device_name': item['device_name'],
        'device_group': item['device_group'],
        'status': item['status'],
        'created_by': item['created_by'],
        'created_at': item['created_at'],
        'updated_at': item['updated_at'],
        'token_expires_at': item['token_expires_at'],
        'token_generation': item['token_generation'],
    }
