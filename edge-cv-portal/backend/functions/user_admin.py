"""
User Admin handler for Edge CV Portal
PortalAdmin-only Cognito account management: listing, password change,
forgot-password (temporary password), role change, and edge account sync.

Routed under /api/v1/admin/* behind the existing jwt_authorizer (requests
without a valid JWT are rejected before this Lambda runs). Every handler
additionally asserts the PortalAdmin role and returns 403 otherwise.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import string
import time
import uuid
from functools import wraps
from typing import Dict, Any, List, Optional, Set
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

from shared_utils import (
    USER_ACCOUNT_RESOURCE_TYPE,
    create_response,
    finalize_audit_event,
    get_user_from_event,
    record_audit_event_strict,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment configuration
USER_POOL_ID = os.environ.get('USER_POOL_ID')
EDGE_CREDENTIALS_TABLE = os.environ.get(
    'EDGE_CREDENTIALS_TABLE', 'dda-portal-edge-credentials')
SES_SENDER_ADDRESS = os.environ.get('SES_SENDER_ADDRESS')
# Account_Sync_Service: per-device sync-state table and the account_sync
# Lambda invoked for an immediate sync attempt after staging (task 3.4
# creates the function; absence is tolerated - the 5-minute schedule
# picks up staged pending changes regardless).
ACCOUNT_SYNC_TABLE = os.environ.get(
    'ACCOUNT_SYNC_TABLE', 'dda-portal-account-sync')
DEVICES_TABLE = os.environ.get('DEVICES_TABLE')
ACCOUNT_SYNC_FUNCTION = os.environ.get('ACCOUNT_SYNC_FUNCTION')

# AWS clients
cognito_client = boto3.client('cognito-idp')
dynamodb = boto3.resource('dynamodb')
ses_client = boto3.client('ses')
lambda_client = boto3.client('lambda')

# --- Pure credential functions -------------------------------------------

# Password policy (AuthStack): minimum length 12, requires lowercase,
# uppercase, digits, and symbols.
PASSWORD_MIN_LENGTH = 12
PASSWORD_SYMBOLS = '!@#$%^&*()-_=+[]{}'

# PBKDF2 verifier parameters (design decision D4)
VERIFIER_ALGORITHM = 'pbkdf2-sha256'
VERIFIER_ITERATIONS = 210000
VERIFIER_SALT_BYTES = 16
VERIFIER_HASH_BYTES = 32

# The five defined Portal_Role values (Requirement 5.2)
PORTAL_ROLES = ('PortalAdmin', 'UseCaseAdmin', 'DataScientist',
                'Operator', 'Viewer')


def generate_temp_password(length: int = 16) -> str:
    """
    Generate a temporary password conforming to the pool Password_Policy:
    length >= 12 with at least one lowercase, uppercase, digit, and symbol.

    Characters are picked with secrets.choice and the result is shuffled
    with secrets.SystemRandom().shuffle so class positions are not predictable.
    """
    if length < PASSWORD_MIN_LENGTH:
        raise ValueError(
            f'length must be >= {PASSWORD_MIN_LENGTH}, got {length}'
        )

    classes = [
        string.ascii_lowercase,
        string.ascii_uppercase,
        string.digits,
        PASSWORD_SYMBOLS,
    ]

    # One guaranteed character from each required class
    chars = [secrets.choice(cls) for cls in classes]

    # Fill the remainder from the union of all classes
    alphabet = ''.join(classes)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))

    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


def make_verifier(password: str, iterations: int = VERIFIER_ITERATIONS) -> Dict[str, Any]:
    """
    Compute a salted one-way credential verifier for a plaintext password.

    Returns {algorithm, iterations, salt (b64), hash (b64)} using
    PBKDF2-HMAC-SHA256 with a fresh 16-byte random salt. The iteration
    count is parameterizable for tests; production callers use the default.
    """
    salt = secrets.token_bytes(VERIFIER_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt,
        iterations,
        dklen=VERIFIER_HASH_BYTES,
    )
    return {
        'algorithm': VERIFIER_ALGORITHM,
        'iterations': iterations,
        'salt': base64.b64encode(salt).decode('ascii'),
        'hash': base64.b64encode(derived).decode('ascii'),
    }


# --- Pure sync-document builder --------------------------------------------

# Shadow dda-user-accounts document schema version (design data model).
SYNC_DOCUMENT_VERSION = 1

# AWS IoT named-shadow document size limit the rendered desired state
# must fit within (design: validate against the 8 KB shadow limit).
SHADOW_SIZE_LIMIT_BYTES = 8 * 1024


class SyncDocumentTooLarge(ValueError):
    """The rendered sync document exceeds the 8 KB shadow size limit."""


def build_sync_document(accounts: Dict[str, Dict[str, Any]],
                        sync_id: str) -> Dict[str, Any]:
    """
    Build the complete desired sync document for one device from a staged
    account set (pure function, design data model for the
    dda-user-accounts shadow).

    Each record carries only {email, role, enabled, deleted?, verifier?}
    - the fields are copied by an explicit whitelist so plaintext
    passwords can never appear in a sync payload no matter what the
    input carries (Req 7.3). Disabled or deleted accounts are marked
    `enabled: false` and are never dropped from the document (Req 7.8).

    Raises SyncDocumentTooLarge when the rendered desired state exceeds
    the 8 KB shadow limit, with an explicit reason.
    """
    doc_accounts = {}
    for username, record in (accounts or {}).items():
        record = record or {}
        deleted = bool(record.get('deleted', False))
        enabled = bool(record.get('enabled', False)) and not deleted

        entry: Dict[str, Any] = {
            'email': record.get('email', ''),
            'role': record.get('role') or 'Viewer',
            'enabled': enabled,
        }
        if deleted:
            entry['deleted'] = True

        verifier = record.get('verifier')
        if verifier:
            entry['verifier'] = {
                'algorithm': verifier.get('algorithm'),
                'iterations': int(verifier.get('iterations', 0)),
                'salt': verifier.get('salt'),
                'hash': verifier.get('hash'),
            }

        doc_accounts[username] = entry

    document = {
        'syncId': sync_id,
        'version': SYNC_DOCUMENT_VERSION,
        'accounts': doc_accounts,
    }

    rendered = json.dumps({'state': {'desired': document}},
                          separators=(',', ':'))
    size = len(rendered.encode('utf-8'))
    if size > SHADOW_SIZE_LIMIT_BYTES:
        raise SyncDocumentTooLarge(
            f'The rendered sync document is {size} bytes, exceeding the '
            f'{SHADOW_SIZE_LIMIT_BYTES}-byte (8 KB) IoT shadow limit; '
            f'reduce the number of selected accounts'
        )
    return document


# --- PortalAdmin gate ------------------------------------------------------

def require_portal_admin(func):
    """
    Decorator asserting the caller's validated JWT role is PortalAdmin.
    Returns 403 without performing the operation otherwise (Requirement 1.5).
    """
    @wraps(func)
    def wrapper(event, *args, **kwargs):
        user = get_user_from_event(event)
        if user.get('role') != 'PortalAdmin':
            logger.warning(
                f"PortalAdmin gate rejected user {user.get('user_id')} "
                f"with role {user.get('role')}"
            )
            return create_response(403, {
                'error': 'Access denied',
                'message': 'PortalAdmin role required'
            })
        return func(event, *args, **kwargs)
    return wrapper


# --- Router ----------------------------------------------------------------

def handler(event, context):
    """
    Handle user admin requests

    GET  /api/v1/admin/users - List Cognito accounts
    POST /api/v1/admin/users/{username}/password - Set account password
    POST /api/v1/admin/users/{username}/forgot-password - Email a temporary password
    PUT  /api/v1/admin/users/{username}/role - Change account role
    GET  /api/v1/admin/edge-sync/devices - Per-device sync status
    POST /api/v1/admin/edge-sync/devices/{deviceId} - Stage and trigger a sync
    """
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '')

        logger.info(f"User admin request: {http_method} {path}")

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return create_response(200, '', {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                'Access-Control-Max-Age': '86400'
            })

        if http_method == 'GET' and path.endswith('/admin/users'):
            return list_accounts(event)
        elif http_method == 'GET' and path.endswith('/edge-sync/devices'):
            return list_sync_devices(event)
        elif http_method == 'POST' and '/edge-sync/devices/' in path:
            return sync_device(event)
        elif http_method == 'POST' and path.endswith('/password'):
            return set_password(event)
        elif http_method == 'POST' and path.endswith('/forgot-password'):
            return forgot_password(event)
        elif http_method == 'PUT' and path.endswith('/role'):
            return change_role(event)

        return create_response(404, {'error': 'Not found'})

    except Exception as e:
        logger.error(f"Error in user admin handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})


# --- Endpoint handlers (implemented in subsequent tasks) --------------------

def _list_all_pool_users() -> List[Dict[str, Any]]:
    """Paginate Cognito list_users fully and return every user in the pool."""
    users = []
    params = {'UserPoolId': USER_POOL_ID, 'Limit': 60}
    while True:
        response = cognito_client.list_users(**params)
        users.extend(response.get('Users', []))
        token = response.get('PaginationToken')
        if not token:
            return users
        params['PaginationToken'] = token


def _load_edge_capable_usernames() -> Set[str]:
    """
    Scan the edge-credentials table for usernames that have a captured
    credential verifier. Keys are stored normalized (lowercase).
    """
    table = dynamodb.Table(EDGE_CREDENTIALS_TABLE)
    usernames = set()
    scan_kwargs = {
        'ProjectionExpression': '#u',
        'ExpressionAttributeNames': {'#u': 'username'},
    }
    while True:
        page = table.scan(**scan_kwargs)
        usernames.update(
            item['username'] for item in page.get('Items', []))
        last_key = page.get('LastEvaluatedKey')
        if not last_key:
            return usernames
        scan_kwargs['ExclusiveStartKey'] = last_key


def _account_row(user: Dict[str, Any],
                 edge_capable_usernames: Set[str]) -> Dict[str, Any]:
    """Map a Cognito list_users record to the account listing shape."""
    attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
    username = user.get('Username', '')
    return {
        'username': username,
        'email': attrs.get('email', ''),
        'email_verified': attrs.get('email_verified') == 'true',
        'role': attrs.get('custom:role') or 'Viewer',
        'user_status': user.get('UserStatus', ''),
        'enabled': bool(user.get('Enabled', False)),
        'edge_capable': username.lower() in edge_capable_usernames,
    }


@require_portal_admin
def list_accounts(event):
    """
    GET /api/v1/admin/users

    List all User_Pool accounts (Cognito list_users paginated fully),
    joined with the edge-credentials table for the edge_capable flag
    (Requirements 2.1). Accounts without a custom:role default to Viewer.
    """
    try:
        users = _list_all_pool_users()
        edge_capable_usernames = _load_edge_capable_usernames()
    except ClientError as e:
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"Failed to retrieve account list: {message}")
        return create_response(502, {
            'error': 'Failed to retrieve account list',
            'message': message,
        })

    accounts = [_account_row(u, edge_capable_usernames) for u in users]
    return create_response(200, {
        'users': accounts,
        'total_count': len(accounts),
    })


def _username_from_path(event) -> str:
    """Extract the {username} path parameter for /admin/users/{username}/...

    Prefers API Gateway pathParameters; falls back to parsing the raw
    path (the segment following 'users'), URL-decoding either way.
    """
    params = event.get('pathParameters') or {}
    if params.get('username'):
        return unquote(params['username'])
    segments = [s for s in event.get('path', '').split('/') if s]
    try:
        return unquote(segments[segments.index('users') + 1])
    except (ValueError, IndexError):
        return ''


def _store_verifier(username: str, password: str):
    """
    Capture a credential verifier at password-set time (design D3).

    Stored in the edge-credentials table keyed by the normalized
    (lowercase) username with an updatedAt timestamp, so the account
    becomes edge-login-capable. Never stores the plaintext (Req 7.3).

    The fresh verifier is a synchronized account attribute, so its
    capture also refreshes every device's staged account set and marks
    those devices as having pending changes (Req 7.2).
    """
    verifier = make_verifier(password)
    table = dynamodb.Table(EDGE_CREDENTIALS_TABLE)
    table.put_item(Item={
        'username': username.lower(),
        'verifier': verifier,
        'updatedAt': int(time.time() * 1000),
    })
    _mark_account_change_pending(username, {'verifier': verifier})


def _mark_account_change_pending(username: str, changes: Dict[str, Any]):
    """
    Attribute-change hook (Req 7.2): when a synchronized account
    attribute changes (credential verifier, role, enabled/disabled
    state), refresh the account's record in every device's staged set
    in `dda-portal-account-sync` and mark the device as having pending
    changes so the next sync (immediate or scheduled) delivers it.

    A fresh syncId is assigned so an in-flight ack of the previously
    staged content cannot mark the refreshed content as delivered.

    Failures are logged, never raised: the primary account action has
    already succeeded, and staged sets are retried by the 5-minute
    schedule regardless.
    """
    try:
        table = dynamodb.Table(ACCOUNT_SYNC_TABLE)
        scan_kwargs: Dict[str, Any] = {}
        while True:
            page = table.scan(**scan_kwargs)
            for row in page.get('Items', []):
                staged = row.get('accounts') or {}
                # Staged sets key accounts by the Cognito username;
                # match case-insensitively (the credentials table
                # normalizes to lowercase).
                key = next((k for k in staged
                            if k.lower() == username.lower()), None)
                if key is None:
                    continue
                record = dict(staged[key])
                record.update(changes)
                table.update_item(
                    Key={'device_id': row['device_id']},
                    UpdateExpression=(
                        'SET accounts.#u = :r, syncId = :s, '
                        'pendingChanges = :p, #st = :pending'),
                    ExpressionAttributeNames={
                        '#u': key, '#st': 'status'},
                    ExpressionAttributeValues={
                        ':r': record,
                        ':s': str(uuid.uuid4()),
                        ':p': True,
                        ':pending': 'pending',
                    },
                )
            last_key = page.get('LastEvaluatedKey')
            if not last_key:
                return
            scan_kwargs['ExclusiveStartKey'] = last_key
    except Exception as e:
        logger.error(
            f"Failed to mark staged syncs pending after an attribute "
            f"change for {username}: {e}")


@require_portal_admin
def set_password(event):
    """
    POST /api/v1/admin/users/{username}/password

    Body {password, permanent: bool}. Flow (audit-before-effect, D10):
    audit-pending -> admin_set_user_password(Permanent=permanent) ->
    verifier capture -> audit-final.

    Error mapping: InvalidPasswordException -> 400 with the policy
    message passed through and no verifier write (3.3);
    UserNotFoundException -> 404; other Cognito errors -> 502
    "password change failed" (3.5). A pending-audit write failure
    -> 500 "action not applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 3.1, 3.3, 3.5, 6.1, 6.4_
    """
    username = _username_from_path(event)
    if not username:
        return create_response(400, {'error': 'Username is required'})

    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {'error': 'Invalid JSON body'})

    password = body.get('password')
    permanent = body.get('permanent')
    if not isinstance(password, str) or not password:
        return create_response(400, {'error': 'password is required'})
    if not isinstance(permanent, bool):
        return create_response(
            400, {'error': 'permanent must be a boolean'})

    acting_user = get_user_from_event(event)

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'password_change',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            details={'permanent': permanent},
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; password change for "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    try:
        cognito_client.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=username,
            Password=password,
            Permanent=permanent,
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', '')
        message = error.get('Message', str(e))

        if code == 'InvalidPasswordException':
            # Policy violation: pass the policy message through, leave
            # the existing password unchanged, write no verifier (3.3).
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': message})
            return create_response(400, {
                'error': 'Password policy violation',
                'message': message,
            })
        if code == 'UserNotFoundException':
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'user not found'})
            return create_response(404, {'error': 'User not found'})

        # Any other Cognito failure: account untouched (3.5).
        logger.error(
            f"admin_set_user_password failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message})
        return create_response(502, {'error': 'password change failed'})

    _store_verifier(username, password)

    finalize_audit_event(audit_event_id, 'success',
                         {'permanent': permanent})

    return create_response(200, {
        'message': f'Password changed for {username}',
        'username': username,
        'permanent': permanent,
    })


def _send_temp_password_email(recipient: str, username: str, password: str):
    """Deliver a temporary password to the account's registered email
    address via SES from the configured sender (Req 4.1)."""
    if not SES_SENDER_ADDRESS:
        raise RuntimeError('SES_SENDER_ADDRESS is not configured')
    ses_client.send_email(
        Source=SES_SENDER_ADDRESS,
        Destination={'ToAddresses': [recipient]},
        Message={
            'Subject': {
                'Data': 'Your Edge CV Portal temporary password',
            },
            'Body': {
                'Text': {
                    'Data': (
                        f'A temporary password was issued for your Edge CV '
                        f'Portal account "{username}".\n\n'
                        f'Temporary password: {password}\n\n'
                        f'You will be required to set a new password at '
                        f'your next sign-in.'
                    ),
                },
            },
        },
    )


@require_portal_admin
def forgot_password(event):
    """
    POST /api/v1/admin/users/{username}/forgot-password

    Flow (audit-before-effect, D10): verified-email check (400 before
    anything is generated when email_verified != 'true', 4.4) ->
    generate_temp_password -> audit-pending -> SES SendEmail from the
    configured sender -> admin_set_user_password(Permanent=False) ->
    verifier capture -> audit-final.

    The SES send happens before the password set so a delivery failure
    leaves the account's existing credentials untouched (4.5). If the
    password set fails after a successful send, the emailed password is
    inert (it never became valid) and the action reports failure. The
    response never contains the temporary password value (4.3).

    _Requirements: 4.1, 4.3, 4.4, 4.5, 6.1, 6.3_
    """
    username = _username_from_path(event)
    if not username:
        return create_response(400, {'error': 'Username is required'})

    # Verified-email check before anything is generated (4.4).
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        if error.get('Code') == 'UserNotFoundException':
            return create_response(404, {'error': 'User not found'})
        message = error.get('Message', str(e))
        logger.error(f"admin_get_user failed for {username}: {message}")
        return create_response(502, {'error': 'forgot-password failed'})

    attrs = {a['Name']: a['Value'] for a in user.get('UserAttributes', [])}
    if attrs.get('email_verified') != 'true':
        return create_response(400, {
            'error': 'No verified email address',
            'message': f'The account {username} has no verified email '
                       f'address',
        })
    email = attrs.get('email')

    temp_password = generate_temp_password()

    acting_user = get_user_from_event(event)

    # Audit-before-effect: the pending entry must be recorded before
    # anything is sent or applied (Req 6.4, 6.5). Details never carry
    # the temporary password value (6.3).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'forgot_password',
            USER_ACCOUNT_RESOURCE_TYPE, username,
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; forgot-password for "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    # SES send before the password set: a delivery failure leaves the
    # account's existing credentials untouched (4.5).
    try:
        _send_temp_password_email(email, username, temp_password)
    except Exception as e:
        message = str(e)
        if isinstance(e, ClientError):
            message = e.response.get('Error', {}).get('Message', message)
        logger.error(
            f"Temporary password delivery failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': 'email delivery failed'})
        return create_response(502, {
            'error': 'temporary password was not sent',
            'message': 'The temporary password was not sent; the '
                       'account credentials are unchanged',
        })

    try:
        cognito_client.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=username,
            Password=temp_password,
            Permanent=False,
        )
    except ClientError as e:
        # The emailed password never became valid; the account's
        # existing credentials remain in effect.
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(
            f"admin_set_user_password failed for {username} after the "
            f"temporary password email was sent: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message})
        return create_response(502, {
            'error': 'forgot-password failed',
            'message': 'The emailed temporary password was not applied '
                       'and is not valid',
        })

    _store_verifier(username, temp_password)

    finalize_audit_event(audit_event_id, 'success')

    return create_response(200, {
        'message': f'Temporary password sent to the registered email '
                   f'address for {username}',
        'username': username,
    })


def _count_enabled_portal_admins() -> int:
    """Count enabled accounts whose custom:role is PortalAdmin.

    Cognito list_users cannot filter on custom attributes, so the
    last-PortalAdmin guard paginates the whole pool (design, Req 5.3).
    """
    count = 0
    for user in _list_all_pool_users():
        if not user.get('Enabled'):
            continue
        attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
        if attrs.get('custom:role') == 'PortalAdmin':
            count += 1
    return count


@require_portal_admin
def change_role(event):
    """
    PUT /api/v1/admin/users/{username}/role

    Body {role}. Flow (design): validate against the five defined
    Portal_Role values (5.2) -> last-PortalAdmin guard (5.3, 5.5) ->
    audit-pending -> admin_update_user_attributes on custom:role (5.1)
    -> audit-final recording the previous and new role (5.4).

    Guard: when the change would remove the PortalAdmin role from the
    last remaining enabled PortalAdmin account, reject with 409 + the
    reason and record the rejected attempt in the audit log.

    Error mapping: UserNotFoundException -> 404; other Cognito failures
    -> 502 "role change failed" with the role unchanged and the audit
    entry finalized to failure (5.6). A pending-audit write failure ->
    500 "action not applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_
    """
    username = _username_from_path(event)
    if not username:
        return create_response(400, {'error': 'Username is required'})

    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {'error': 'Invalid JSON body'})

    new_role = body.get('role')
    if new_role not in PORTAL_ROLES:
        return create_response(400, {
            'error': 'Invalid role',
            'message': f"role must be one of: {', '.join(PORTAL_ROLES)}",
        })

    # Current state: previous role for the audit record (5.4) and the
    # enabled flag for the last-PortalAdmin guard (5.3).
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        if error.get('Code') == 'UserNotFoundException':
            return create_response(404, {'error': 'User not found'})
        message = error.get('Message', str(e))
        logger.error(f"admin_get_user failed for {username}: {message}")
        return create_response(502, {'error': 'role change failed'})

    attrs = {a['Name']: a['Value'] for a in user.get('UserAttributes', [])}
    previous_role = attrs.get('custom:role') or 'Viewer'
    target_enabled = bool(user.get('Enabled', False))

    acting_user = get_user_from_event(event)

    # Last-PortalAdmin guard (5.3): only a change that takes PortalAdmin
    # away from an enabled PortalAdmin account can reduce the enabled-
    # PortalAdmin count.
    if (previous_role == 'PortalAdmin' and target_enabled
            and new_role != 'PortalAdmin'):
        try:
            admin_count = _count_enabled_portal_admins()
        except ClientError as e:
            message = e.response.get('Error', {}).get('Message', str(e))
            logger.error(
                f"last-PortalAdmin guard count failed for {username}: "
                f"{message}")
            return create_response(502, {'error': 'role change failed'})

        if admin_count <= 1:
            reason = (f'{username} is the last remaining enabled '
                      f'PortalAdmin account; the portal must retain at '
                      f'least one enabled PortalAdmin')
            # The rejected attempt is itself audited (5.5); if it cannot
            # be recorded, the action is reported as not applied (6.4).
            try:
                record_audit_event_strict(
                    acting_user['user_id'], 'role_change',
                    USER_ACCOUNT_RESOURCE_TYPE, username,
                    result='rejected',
                    details={
                        'reason': reason,
                        'previous_role': previous_role,
                        'requested_role': new_role,
                    },
                )
            except Exception as e:
                logger.error(
                    f"Rejected-attempt audit write failed for "
                    f"{username}: {e}")
                return create_response(500, {
                    'error': 'Audit log unavailable',
                    'message': 'The action was not applied',
                })
            return create_response(409, {
                'error': 'Role change rejected',
                'message': reason,
            })

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'role_change',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            details={'previous_role': previous_role, 'new_role': new_role},
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; role change for "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    try:
        cognito_client.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[{'Name': 'custom:role', 'Value': new_role}],
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', '')
        message = error.get('Message', str(e))

        if code == 'UserNotFoundException':
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'user not found'})
            return create_response(404, {'error': 'User not found'})

        # Any other Cognito failure: the role is unchanged (5.6).
        logger.error(
            f"admin_update_user_attributes failed for {username}: "
            f"{message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message})
        return create_response(502, {'error': 'role change failed'})

    # The role is a synchronized account attribute: refresh every
    # device's staged set and mark it pending (Req 7.2).
    _mark_account_change_pending(username, {'role': new_role})

    # Audit-final records the previous and new role (5.4).
    finalize_audit_event(audit_event_id, 'success', {
        'previous_role': previous_role,
        'new_role': new_role,
    })

    return create_response(200, {
        'message': f'Role changed for {username}',
        'username': username,
        'previous_role': previous_role,
        'role': new_role,
    })


def _scan_all_items(table, **scan_kwargs) -> List[Dict[str, Any]]:
    """Paginate a DynamoDB table scan fully."""
    items = []
    kwargs = dict(scan_kwargs)
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get('Items', []))
        last_key = page.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


@require_portal_admin
def list_sync_devices(event):
    """
    GET /api/v1/admin/edge-sync/devices

    Devices table joined with the dda-portal-account-sync sync-state
    table: per device the last sync status, last sync timestamp, and
    whether undelivered pending changes exist (Req 7.4 display data).
    Devices without a sync row report null status ("never synced").

    _Requirements: 7.1 (device list for initiating syncs), 7.4_
    """
    if not DEVICES_TABLE:
        return create_response(
            500, {'error': 'Devices table not configured'})

    try:
        device_items = _scan_all_items(
            dynamodb.Table(DEVICES_TABLE),
            ProjectionExpression='device_id',
        )
        sync_items = _scan_all_items(dynamodb.Table(ACCOUNT_SYNC_TABLE))
    except ClientError as e:
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"Failed to retrieve edge-sync device list: {message}")
        return create_response(502, {
            'error': 'Failed to retrieve edge-sync device list',
            'message': message,
        })

    sync_by_device = {row['device_id']: row for row in sync_items
                      if row.get('device_id')}
    device_ids = {item['device_id'] for item in device_items
                  if item.get('device_id')}
    # Devices with staged sync state are listed even if their devices-
    # table record has gone, so pending changes stay visible.
    device_ids.update(sync_by_device.keys())

    devices = []
    for device_id in sorted(device_ids):
        row = sync_by_device.get(device_id, {})
        devices.append({
            'device_id': device_id,
            'lastSyncStatus': row.get('status'),
            'lastSyncAt': row.get('lastSyncAt'),
            'pendingChanges': bool(row.get('pendingChanges', False)),
            'failureReason': row.get('failureReason'),
        })

    return create_response(200, {
        'devices': devices,
        'count': len(devices),
    })


def _device_id_from_path(event) -> str:
    """Extract {deviceId} for /admin/edge-sync/devices/{deviceId}."""
    params = event.get('pathParameters') or {}
    for key in ('deviceId', 'device_id', 'id'):
        if params.get(key):
            return unquote(params[key])
    segments = [s for s in event.get('path', '').split('/') if s]
    try:
        return unquote(segments[segments.index('devices') + 1])
    except (ValueError, IndexError):
        return ''


def _load_verifier(username: str) -> Optional[Dict[str, Any]]:
    """The captured credential verifier for a username, or None."""
    table = dynamodb.Table(EDGE_CREDENTIALS_TABLE)
    item = table.get_item(
        Key={'username': username.lower()}).get('Item') or {}
    return item.get('verifier')


def _invoke_sync_lambda(device_id: str, sync_id: str) -> bool:
    """
    Ask the account_sync Lambda for an immediate sync attempt.

    Absence of the function (env var unset) or an invoke failure is
    tolerated: staged pending changes are picked up by the 5-minute
    schedule regardless (Req 7.7).
    """
    if not ACCOUNT_SYNC_FUNCTION:
        logger.info(
            'ACCOUNT_SYNC_FUNCTION not configured; staged sync for '
            f'{device_id} awaits the scheduled attempt')
        return False
    try:
        lambda_client.invoke(
            FunctionName=ACCOUNT_SYNC_FUNCTION,
            InvocationType='Event',
            Payload=json.dumps({
                'action': 'sync_attempt',
                'device_id': device_id,
                'syncId': sync_id,
            }),
        )
        return True
    except Exception as e:
        logger.error(
            f"Failed to invoke the account sync Lambda for {device_id}: "
            f"{e}")
        return False


@require_portal_admin
def sync_device(event):
    """
    POST /api/v1/admin/edge-sync/devices/{deviceId}

    Body {usernames: [...]}. Stages the selected accounts as the
    device's complete staged account set - each record carrying
    username, email, Portal_Role, enabled/disabled state, and the
    captured credential verifier when one exists (Req 7.1, 7.3) - with
    a fresh syncId and pendingChanges=true, then invokes the sync
    Lambda for an immediate attempt.

    Disabled accounts are staged marked `enabled: false`, never
    dropped (Req 7.8). The rendered document is validated against the
    8 KB shadow limit before staging and the request fails with an
    explicit reason when it does not fit.

    _Requirements: 7.1, 7.2, 7.3, 7.8_
    """
    device_id = _device_id_from_path(event)
    if not device_id:
        return create_response(400, {'error': 'Device id is required'})

    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {'error': 'Invalid JSON body'})

    usernames = body.get('usernames')
    if (not isinstance(usernames, list) or not usernames
            or not all(isinstance(u, str) and u for u in usernames)):
        return create_response(400, {
            'error': 'usernames must be a non-empty list of usernames'})

    # Resolve the selected accounts' current attributes from the pool.
    try:
        pool_users = {u.get('Username'): u for u in _list_all_pool_users()}
    except ClientError as e:
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"Failed to resolve accounts for sync: {message}")
        return create_response(502, {
            'error': 'Failed to resolve the selected accounts',
            'message': message,
        })

    unknown = sorted(set(usernames) - set(pool_users))
    if unknown:
        return create_response(400, {
            'error': 'Unknown usernames',
            'message': f"Not found in the user pool: {', '.join(unknown)}",
        })

    # Stage the complete selected account set (Req 7.1): disabled
    # accounts are included marked enabled=false, never dropped (7.8);
    # credential material only as the captured one-way verifier (7.3).
    try:
        staged_accounts = {}
        for username in sorted(set(usernames)):
            user = pool_users[username]
            attrs = {a['Name']: a['Value']
                     for a in user.get('Attributes', [])}
            record: Dict[str, Any] = {
                'email': attrs.get('email', ''),
                'role': attrs.get('custom:role') or 'Viewer',
                'enabled': bool(user.get('Enabled', False)),
            }
            verifier = _load_verifier(username)
            if verifier:
                record['verifier'] = verifier
            staged_accounts[username] = record
    except ClientError as e:
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"Failed to load credential verifiers: {message}")
        return create_response(502, {
            'error': 'Failed to load credential verifiers',
            'message': message,
        })

    sync_id = str(uuid.uuid4())

    # Validate the rendered document against the 8 KB shadow limit
    # before anything is staged; fail with the explicit reason.
    try:
        build_sync_document(staged_accounts, sync_id)
    except SyncDocumentTooLarge as e:
        return create_response(400, {
            'error': 'Sync document too large',
            'message': str(e),
        })

    # Stage atomically on the device's sync-state row, preserving any
    # prior lastSyncAt; a stale failureReason is cleared with the new
    # staging.
    try:
        dynamodb.Table(ACCOUNT_SYNC_TABLE).update_item(
            Key={'device_id': device_id},
            UpdateExpression=(
                'SET syncId = :s, accounts = :a, #st = :pending, '
                'pendingChanges = :p, stagedAt = :now '
                'REMOVE failureReason'),
            ExpressionAttributeNames={'#st': 'status'},
            ExpressionAttributeValues={
                ':s': sync_id,
                ':a': staged_accounts,
                ':pending': 'pending',
                ':p': True,
                ':now': int(time.time() * 1000),
            },
        )
    except ClientError as e:
        message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(
            f"Failed to stage account sync for {device_id}: {message}")
        return create_response(502, {
            'error': 'Failed to stage the account sync',
            'message': message,
        })

    sync_invoked = _invoke_sync_lambda(device_id, sync_id)

    return create_response(200, {
        'message': f'Account sync to {device_id} staged for '
                   f'{len(staged_accounts)} account(s)',
        'device_id': device_id,
        'syncId': sync_id,
        'accountCount': len(staged_accounts),
        'pendingChanges': True,
        'syncInvoked': sync_invoked,
    })
