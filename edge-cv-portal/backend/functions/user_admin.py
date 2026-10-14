"""
User Admin handler for Edge CV Portal
PortalAdmin-only Cognito account management: listing, creation, password
change, forgot-password (temporary password), role change,
disable/enable, and edge account sync.

Routed under /api/v1/admin/* behind the existing jwt_authorizer (requests
without a valid JWT are rejected before this Lambda runs). Every handler
additionally asserts the PortalAdmin role and returns 403 otherwise.

This module is also the **writer of the Portal_Identity registry**
(`dda-portal-user-roles`), which is the source of portal privilege after
the portal-jwt-role-privilege-escalation fix: creating an account writes
its global registry row, a role change updates it, disable/enable set its
`status`, a deletion removes it, and the last-PortalAdmin guard counts
registry rows once enforcement is on (Requirement 3). A Cognito account
this module never provisioned therefore holds no portal privilege, no
matter what its `custom:role` attribute claims.
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
    GLOBAL_SCOPE,
    REGISTRY_STATUS_DISABLED,
    REGISTRY_STATUS_ENABLED,
    USER_ACCOUNT_RESOURCE_TYPE,
    RegistryUnavailable,
    attribution_from,
    caller_is_portal_admin,
    create_response,
    finalize_audit_event,
    get_user_from_event,
    log_audit_event,
    record_audit_event_strict,
    registry_enforcement_enabled,
)
# The canonical audit action for a request that could not be authorized
# because the Portal_Identity registry was unreadable. Shared with
# rbac_check so an outage looks the same whichever gate saw it.
from rbac_middleware import AUTHORIZATION_UNAVAILABLE_ACTION

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
# Portal_Identity registry (portal-jwt-role-privilege-escalation, Req 3).
# `dda-portal-user-roles` is the source of portal privilege after the fix
# and the User Manager is its writer: rows are keyed (user_id = Cognito
# `sub`, usecase_id), and usecase_id='global' is the account-level
# provisioning record this module maintains. Per-Use_Case rows stay owned
# by Team Management (user_management.py); this module only removes them
# when the account itself is deleted.
USER_ROLES_TABLE = os.environ.get('USER_ROLES_TABLE',
                                  'dda-portal-user-roles')

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

# The defined Portal_Role values (Requirement 5.2), plus the restricted
# DataLabeler role (dda-data-labeling, Req 2.1: assigned/revoked through
# the existing user administration functions).
PORTAL_ROLES = ('PortalAdmin', 'UseCaseAdmin', 'DataScientist',
                'Operator', 'Viewer', 'DataLabeler')


def validate_create_request(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Pure validation gate for account creation (Requirements 12.6-12.8).

    Returns None when the payload is valid, otherwise a rejection
    {'field', 'message'} identifying the offending field:

    - username, email, and role must all be present and non-empty (12.7)
    - the email must consist of a non-empty local part, an '@'
      separator, and a non-empty domain containing at least one dot (12.6)
    - the role must be one of the defined Portal_Role values (12.8)

    Only a payload passing every check may reach admin_create_user; a
    rejection performs no User_Pool call (callers return before any
    Cognito interaction).
    """
    body = body or {}
    for field in ('username', 'email', 'role'):
        value = body.get(field)
        if not isinstance(value, str) or not value:
            return {
                'field': field,
                'message': f'{field} is required and must be non-empty',
            }

    parts = body['email'].split('@')
    if len(parts) != 2 or not parts[0] or not parts[1] or '.' not in parts[1]:
        return {
            'field': 'email',
            'message': 'email address is invalid: it must consist of a '
                       'non-empty local part, an @ separator, and a '
                       'non-empty domain containing at least one dot',
        }

    if body['role'] not in PORTAL_ROLES:
        return {
            'field': 'role',
            'message': f"role must be one of: {', '.join(PORTAL_ROLES)}",
        }

    return None


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


# --- Portal_Identity registry writes ---------------------------------------
#
# portal-jwt-role-privilege-escalation, Requirement 3: the User Manager is
# the registry's writer. A Cognito account only becomes a portal principal
# once this module has written its global Portal_Identity row, which is what
# makes an account created directly with `admin-create-user` (the recorded
# incident) resolve to no role at all under enforcement.
#
# Every row this module writes carries `role`, `username`, `email`, and
# `status` (design.md Decision 1), so an audit reader can name the account
# after its Cognito user is gone, and so the read path
# (`shared_utils._lookup_identity`) can deny a disabled account exactly as
# it denies an absent one (Requirement 1.6).


def _sub_of(attributes: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """The Cognito `sub` in an Attributes / UserAttributes list, or None."""
    for attribute in attributes or []:
        if attribute.get('Name') == 'sub' and attribute.get('Value'):
            return attribute['Value']
    return None


def _resolve_registry_key(username: str,
                          created: Optional[Dict[str, Any]] = None,
                          user: Optional[Dict[str, Any]] = None
                          ) -> Optional[str]:
    """The account's Cognito `sub` — its Portal_Identity key.

    Read from a Cognito response the caller already holds
    (`admin_create_user`'s `User.Attributes` or `admin_get_user`'s
    `UserAttributes`, both of which carry `sub`), falling back to one
    `admin_get_user` call.

    Returns None only when nothing names a sub. A key is never invented:
    the read path looks rows up by the token's `sub`, so a row under a
    fabricated key would decide nothing while looking like provisioning.
    """
    sub = _sub_of(((created or {}).get('User') or {}).get('Attributes'))
    if sub:
        return sub
    sub = _sub_of((user or {}).get('UserAttributes'))
    if sub:
        return sub
    try:
        fetched = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=username)
        return _sub_of((fetched or {}).get('UserAttributes'))
    except Exception as e:
        logger.warning(
            f"Could not resolve the Cognito sub of {username}, so its "
            f"Portal_Identity registry entry could not be addressed: {e}")
        return None


def _registry_table():
    """The Portal_Identity registry table handle."""
    return dynamodb.Table(USER_ROLES_TABLE)


def _put_registry_identity(user_id: str, role: str, username: str,
                           email: str, assigned_by: str,
                           status: str = REGISTRY_STATUS_ENABLED) -> None:
    """Write an account's global Portal_Identity row (Requirement 3.1).

    Raises on failure so the caller can report the partial state: a
    Cognito account without this row is inert (Requirement 1.1 denies it),
    which is the fail-closed half of Requirement 3.2.
    """
    _registry_table().put_item(Item={
        'user_id': user_id,
        'usecase_id': GLOBAL_SCOPE,
        'role': role,
        'username': username,
        'email': email or '',
        'status': status,
        'assigned_by': assigned_by,
        'assigned_at': int(time.time()),
    })


def _update_registry_identity(user_id: str,
                              updates: Dict[str, Any]) -> None:
    """SET attributes on an account's global Portal_Identity row.

    Creates the row when absent: a PortalAdmin changing an account's role
    or state through the portal IS the provisioning act, and before the
    backfill (task 3.1) most accounts have no row yet. Raises on failure.
    """
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    assignments: List[str] = []
    for index, (key, value) in enumerate(updates.items()):
        names[f'#k{index}'] = key
        values[f':v{index}'] = value
        assignments.append(f'#k{index} = :v{index}')

    _registry_table().update_item(
        Key={'user_id': user_id, 'usecase_id': GLOBAL_SCOPE},
        UpdateExpression='SET ' + ', '.join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _delete_registry_identity(user_id: str) -> None:
    """Remove every Portal_Identity row of a deleted account (Req 3.4).

    The global row goes first — it is the provisioning record privilege is
    resolved from, so removing it is what stops a token minted before the
    deletion from being privileged on its next request. Any per-Use_Case
    rows follow, so a deleted `sub` leaves no registry trace behind (they
    already decide nothing without a global row, but an orphan row naming
    a role is misleading to a reader).

    Raises on failure.
    """
    table = _registry_table()
    table.delete_item(Key={'user_id': user_id, 'usecase_id': GLOBAL_SCOPE})

    query_kwargs: Dict[str, Any] = {
        'KeyConditionExpression': 'user_id = :user_id',
        'ExpressionAttributeValues': {':user_id': user_id},
        'ProjectionExpression': 'usecase_id',
    }
    while True:
        page = table.query(**query_kwargs)
        for item in page.get('Items', []):
            usecase_id = item.get('usecase_id')
            if usecase_id and usecase_id != GLOBAL_SCOPE:
                table.delete_item(
                    Key={'user_id': user_id, 'usecase_id': usecase_id})
        last_key = page.get('LastEvaluatedKey')
        if not last_key:
            return
        query_kwargs['ExclusiveStartKey'] = last_key


def _count_registry_portal_admins() -> int:
    """Count enabled global Portal_Identity rows naming PortalAdmin.

    The registry is small (one global row per account plus per-Use_Case
    grants), so the guard scans it with a filter rather than depending on
    the `usecase-users-index` GSI, which the deployed table has but test
    fixtures do not.

    A row with no `status` counts as enabled, matching the read path
    (`shared_utils._identity_is_enabled`): rows written before this spec
    carry none.
    """
    scan_kwargs: Dict[str, Any] = {
        'FilterExpression': '#usecase = :global AND #role = :role',
        'ExpressionAttributeNames': {'#usecase': 'usecase_id',
                                     '#role': 'role'},
        'ExpressionAttributeValues': {':global': GLOBAL_SCOPE,
                                      ':role': 'PortalAdmin'},
    }
    count = 0
    while True:
        page = _registry_table().scan(**scan_kwargs)
        for item in page.get('Items', []):
            status = item.get('status')
            if (status is None
                    or str(status).strip().lower() == REGISTRY_STATUS_ENABLED):
                count += 1
        last_key = page.get('LastEvaluatedKey')
        if not last_key:
            return count
        scan_kwargs['ExclusiveStartKey'] = last_key


# --- PortalAdmin gate ------------------------------------------------------

def require_portal_admin(func):
    """
    Decorator asserting the caller's **Effective_Role** is PortalAdmin.

    Returns 403 without performing the operation otherwise (Requirement
    1.5). The decision comes from `caller_is_portal_admin`, i.e. from the
    Portal_Identity registry through `RBACManager` — NOT from the token's
    `custom:role` claim, which is Claimed_Role only and grants nothing
    (design.md Decision 2).

    This gate used to compare `get_user_from_event(event)['role']`
    directly, which made every route below it claim-driven and left the
    escalation open on the whole user-administration surface even with
    PORTAL_REGISTRY_ENFORCED on (measured on account 164152369890,
    2026-09-22: a claim-only account with no registry row read
    `GET /admin/users` in full while the enforced `rbac_check` path
    correctly denied it `POST /builds`).

    An unreadable registry answers 500, never 403: an availability failure
    is not a privilege decision (design.md Decision 3).
    """
    @wraps(func)
    def wrapper(event, *args, **kwargs):
        user = get_user_from_event(event)
        user_id = user.get('user_id', 'unknown')
        try:
            is_admin = caller_is_portal_admin(user)
        except RegistryUnavailable as error:
            logger.error(
                f"Portal_Identity registry unavailable during the "
                f"PortalAdmin gate: {str(error)}", exc_info=True)
            log_audit_event(
                user_id=user_id,
                action=AUTHORIZATION_UNAVAILABLE_ACTION,
                resource_type='api_endpoint',
                resource_id=event.get('resource', 'unknown'),
                result='failure',
                details={
                    'required_role': 'PortalAdmin',
                    'usecase_id': GLOBAL_SCOPE,
                    'method': event.get('httpMethod'),
                    'path': event.get('path'),
                    'claimed_role': user.get('role', 'unknown'),
                    'error': str(error),
                },
                identity=attribution_from(event, user,
                                          usecase_id=GLOBAL_SCOPE)
            )
            return create_response(500, {'error': 'Authorization check failed'})

        if not is_admin:
            # Attributable after the Cognito account is deleted, and the
            # Claimed_Role is kept as metadata: a claim of PortalAdmin on a
            # denied request is the escalation attempt's signature
            # (Requirements 4.1, 4.2, 4.4).
            logger.warning(
                f"PortalAdmin gate rejected user {user_id} "
                f"(claimed_role={user.get('role')})"
            )
            log_audit_event(
                user_id=user_id,
                action='unauthorized_access',
                resource_type='api_endpoint',
                resource_id=event.get('resource', 'unknown'),
                result='denied',
                details={
                    'required_role': 'PortalAdmin',
                    'usecase_id': GLOBAL_SCOPE,
                    'method': event.get('httpMethod'),
                    'path': event.get('path'),
                    'claimed_role': user.get('role', 'unknown'),
                },
                identity=attribution_from(event, user,
                                          usecase_id=GLOBAL_SCOPE)
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
    POST /api/v1/admin/users - Create a Cognito account
    POST /api/v1/admin/users/{username}/password - Set account password
    POST /api/v1/admin/users/{username}/forgot-password - Email a temporary password
    PUT  /api/v1/admin/users/{username}/role - Change account role
    POST /api/v1/admin/users/{username}/disable - Disable an account
    POST /api/v1/admin/users/{username}/enable - Enable an account
    DELETE /api/v1/admin/users/{username} - Delete an account
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
        elif http_method == 'POST' and path.endswith('/admin/users'):
            return create_account(event)
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
        elif http_method == 'POST' and path.endswith('/disable'):
            return disable_account(event)
        elif http_method == 'POST' and path.endswith('/enable'):
            return enable_account(event)
        elif http_method == 'DELETE' and '/admin/users/' in path:
            return delete_account(event)

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


@require_portal_admin
def create_account(event):
    """
    POST /api/v1/admin/users

    Body {username, email, role}. Flow (audit-before-effect, D10):
    validate_create_request (12.6, 12.7, 12.8 - a rejection performs no
    User_Pool call) -> audit-pending (account_create) ->
    admin_create_user with custom:role, email, email_verified=true and
    the Cognito-native email invitation (D12 - default MessageAction, no
    SES, no portal-generated password, no verifier capture) -> the
    account's global Portal_Identity registry row keyed on its new `sub`
    (portal-jwt-role-privilege-escalation Req 3.1) -> audit-final
    carrying the created account's {username, email, role} (12.11).

    Error mapping: UsernameExistsException -> 409 "username already
    exists" with no account created or modified (12.5); other Cognito
    errors -> 502 "account was not created" with no partial record
    (creation is atomic on the Cognito side, 12.9), audit-final failure.
    A registry write that fails after a successful Cognito create -> 502
    "account was not provisioned" with the partial state audited: the
    account exists but has no portal access, because an absent registry
    entry is denied (Req 3.2 / 1.1). A pending-audit write failure ->
    500 "action not applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 12.1, 12.3, 12.5, 12.6, 12.7, 12.8, 12.9, 12.11;
    portal-jwt-role-privilege-escalation 3.1, 3.2, 4.1_
    """
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {'error': 'Invalid JSON body'})

    if not isinstance(body, dict):
        return create_response(400, {'error': 'Invalid JSON body'})

    # Pure validation gate: a rejection returns before any User_Pool
    # call, so no account or partial record can exist (12.6-12.8).
    rejection = validate_create_request(body)
    if rejection is not None:
        return create_response(400, {
            'error': 'Invalid account request',
            'field': rejection['field'],
            'message': rejection['message'],
        })

    username = body['username']
    email = body['email']
    role = body['role']

    acting_user = get_user_from_event(event)
    identity = attribution_from(event, acting_user)

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'account_create',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            details={'email': email, 'role': role},
            identity=identity,
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; account creation for "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    # Cognito-native invitation (D12): the default MessageAction sends
    # the invitation email with a Cognito-generated policy-conformant
    # temporary password (12.3); the portal never holds it, so no
    # verifier is captured at creation.
    try:
        created = cognito_client.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[
                {'Name': 'email', 'Value': email},
                {'Name': 'email_verified', 'Value': 'true'},
                {'Name': 'custom:role', 'Value': role},
            ],
            DesiredDeliveryMediums=['EMAIL'],
        )
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', '')
        message = error.get('Message', str(e))

        if code == 'UsernameExistsException':
            # Duplicate username: nothing was created or modified (12.5).
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'username already exists'},
                                 identity=identity)
            return create_response(409, {
                'error': 'username already exists',
                'message': f'An account with the username {username} '
                           f'already exists',
            })

        # Any other Cognito failure: creation is atomic, so no account
        # or partial record remains in the User_Pool (12.9).
        logger.error(f"admin_create_user failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message}, identity=identity)
        return create_response(502, {'error': 'account was not created'})

    # Portal_Identity provisioning (Requirement 3.1): the account is a
    # portal principal only once the registry names it, so the global row
    # is written immediately after the Cognito account exists, keyed on
    # the new account's `sub`.
    registry_entry = 'written'
    created_sub = _resolve_registry_key(username, created=created)
    if created_sub:
        try:
            _put_registry_identity(
                created_sub, role=role, username=username, email=email,
                assigned_by=acting_user['user_id'])
        except Exception as e:
            # Cognito holds an account the registry does not name, so the
            # account is inert (Requirement 1.1 denies it). Report the
            # failure and record the partial state (Requirement 3.2).
            logger.error(
                f"Portal_Identity registry write failed for {username} "
                f"({created_sub}) after a successful Cognito create: {e}")
            finalize_audit_event(audit_event_id, 'failure', {
                'username': username,
                'email': email,
                'role': role,
                'created_user_id': created_sub,
                'partial_state': 'the Cognito account was created but its '
                                 'Portal_Identity registry entry was not '
                                 'written, so the account has no portal '
                                 'access',
                'reason': str(e),
            }, identity=identity)
            return create_response(502, {
                'error': 'account was not provisioned',
                'message': f'The Cognito account {username} was created '
                           f'but its portal registry entry was not '
                           f'written, so it has no portal access; delete '
                           f'the account and retry',
            })
    else:
        # Only reachable when Cognito named no `sub` for the account it
        # just created (an unexpected response shape): there is no key to
        # write the row under. Loud, recorded, and fail-closed — the
        # account has no portal access until it is provisioned.
        registry_entry = 'skipped: the created account reported no sub'
        logger.error(
            f"No Cognito sub for the newly created account {username}: no "
            f"Portal_Identity registry entry was written, so the account "
            f"has no portal access")

    # Audit-final carries the created account's username, email, and
    # role (12.11), plus whether the registry entry landed (Req 3.1).
    finalize_audit_event(audit_event_id, 'success', {
        'username': username,
        'email': email,
        'role': role,
        'created_user_id': created_sub or 'unknown',
        'registry_entry': registry_entry,
    }, identity=identity)

    return create_response(201, {
        'message': f'Account created for {username}; an invitation with '
                   f'a temporary password was sent to {email}',
        'username': username,
        'email': email,
        'role': role,
    })


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
    identity = attribution_from(event, acting_user)

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'password_change',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            details={'permanent': permanent},
            identity=identity,
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
                                 {'reason': message}, identity=identity)
            return create_response(400, {
                'error': 'Password policy violation',
                'message': message,
            })
        if code == 'UserNotFoundException':
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'user not found'},
                                 identity=identity)
            return create_response(404, {'error': 'User not found'})

        # Any other Cognito failure: account untouched (3.5).
        logger.error(
            f"admin_set_user_password failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message}, identity=identity)
        return create_response(502, {'error': 'password change failed'})

    _store_verifier(username, password)

    finalize_audit_event(audit_event_id, 'success',
                         {'permanent': permanent}, identity=identity)

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
    identity = attribution_from(event, acting_user)

    # Audit-before-effect: the pending entry must be recorded before
    # anything is sent or applied (Req 6.4, 6.5). Details never carry
    # the temporary password value (6.3).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'forgot_password',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            identity=identity,
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
                             {'reason': 'email delivery failed'},
                             identity=identity)
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
                             {'reason': message}, identity=identity)
        return create_response(502, {
            'error': 'forgot-password failed',
            'message': 'The emailed temporary password was not applied '
                       'and is not valid',
        })

    _store_verifier(username, temp_password)

    finalize_audit_event(audit_event_id, 'success', identity=identity)

    return create_response(200, {
        'message': f'Temporary password sent to the registered email '
                   f'address for {username}',
        'username': username,
    })


def _count_cognito_portal_admins() -> int:
    """Count enabled accounts whose custom:role is PortalAdmin.

    Cognito list_users cannot filter on custom attributes, so this
    paginates the whole pool (portal-user-manager design, Req 5.3).
    """
    count = 0
    for user in _list_all_pool_users():
        if not user.get('Enabled'):
            continue
        attrs = {a['Name']: a['Value'] for a in user.get('Attributes', [])}
        if attrs.get('custom:role') == 'PortalAdmin':
            count += 1
    return count


def _count_enabled_portal_admins() -> int:
    """Count the principals that can still administer the portal.

    The guard exists to keep at least one working PortalAdmin, so it must
    count whatever currently decides privilege
    (portal-jwt-role-privilege-escalation Req 3.5):

    * registry enforcement ON — enabled global Portal_Identity rows naming
      PortalAdmin. Scanning Cognito attributes there would count accounts
      that can administer nothing (a `custom:role` claim grants no
      privilege once the registry is authoritative) and would miss
      accounts the registry does name.
    * enforcement OFF (the deployed default until the backfill has run,
      design.md Decision 4) — the pool scan, unchanged, because the claim
      is still what decides privilege in that state. Counting an
      near-empty registry instead would reject every PortalAdmin role
      change, disable, and deletion as "the last admin".
    """
    if registry_enforcement_enabled():
        return _count_registry_portal_admins()
    return _count_cognito_portal_admins()


@require_portal_admin
def change_role(event):
    """
    PUT /api/v1/admin/users/{username}/role

    Body {role}. Flow (design): validate against the defined
    Portal_Role values (5.2) -> last-PortalAdmin guard (5.3, 5.5) ->
    audit-pending -> admin_update_user_attributes on custom:role (5.1)
    -> the account's global Portal_Identity row updated to the new role
    (portal-jwt-role-privilege-escalation Req 3.3: the registry value is
    the one that takes effect) -> audit-final recording the previous and
    new role (5.4).

    Guard: when the change would remove the PortalAdmin role from the
    last remaining enabled PortalAdmin account, reject with 409 + the
    reason and record the rejected attempt in the audit log.

    Error mapping: UserNotFoundException -> 404; other Cognito failures
    -> 502 "role change failed" with the role unchanged and the audit
    entry finalized to failure (5.6). A registry update that fails after
    a successful Cognito update -> 502 "role change incomplete" with the
    partial state audited, because the registry value is the effective
    one (Req 3.3). A pending-audit write failure -> 500 "action not
    applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6;
    portal-jwt-role-privilege-escalation 3.3, 4.1_
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
    identity = attribution_from(event, acting_user)

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
                    identity=identity,
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
            identity=identity,
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
                                 {'reason': 'user not found'},
                                 identity=identity)
            return create_response(404, {'error': 'User not found'})

        # Any other Cognito failure: the role is unchanged (5.6).
        logger.error(
            f"admin_update_user_attributes failed for {username}: "
            f"{message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message}, identity=identity)
        return create_response(502, {'error': 'role change failed'})

    # The registry value is the role that takes effect (Requirement 3.3),
    # so the global Portal_Identity row is updated to match the account's
    # new role. `status` is written from the account's current Cognito
    # enabled state, which keeps the registry in step with Cognito even
    # when the row predates this spec (Property 6). `username` / `email`
    # keep the row human-readable after the Cognito user is deleted.
    registry_user_id = _resolve_registry_key(username, user=user)
    if registry_user_id:
        try:
            _update_registry_identity(registry_user_id, {
                'role': new_role,
                'username': username,
                'email': attrs.get('email', ''),
                'status': (REGISTRY_STATUS_ENABLED if target_enabled
                           else REGISTRY_STATUS_DISABLED),
                'updated_at': int(time.time()),
                'updated_by': acting_user['user_id'],
            })
        except Exception as e:
            # Cognito carries the new role but the registry — the value
            # that actually takes effect under enforcement — does not.
            logger.error(
                f"Portal_Identity registry role update failed for "
                f"{username} ({registry_user_id}) after a successful "
                f"Cognito update: {e}")
            _mark_account_change_pending(username, {'role': new_role})
            finalize_audit_event(audit_event_id, 'failure', {
                'previous_role': previous_role,
                'new_role': new_role,
                'partial_state': 'the Cognito custom:role attribute was '
                                 'updated but the Portal_Identity registry '
                                 'entry was not, so the effective role is '
                                 'unchanged',
                'reason': str(e),
            }, identity=identity)
            return create_response(502, {
                'error': 'role change incomplete',
                'message': f'The role attribute of {username} was updated '
                           f'but its portal registry entry was not, so the '
                           f'effective role is unchanged; retry the role '
                           f'change',
            })
    else:
        logger.error(
            f"No Cognito sub for {username}: its Portal_Identity registry "
            f"entry was not updated to {new_role}, so the effective role "
            f"is unchanged")

    # The role is a synchronized account attribute: refresh every
    # device's staged set and mark it pending (Req 7.2).
    _mark_account_change_pending(username, {'role': new_role})

    # Audit-final records the previous and new role (5.4).
    finalize_audit_event(audit_event_id, 'success', {
        'previous_role': previous_role,
        'new_role': new_role,
    }, identity=identity)

    return create_response(200, {
        'message': f'Role changed for {username}',
        'username': username,
        'previous_role': previous_role,
        'role': new_role,
    })


def _set_account_enabled(event, target_enabled: bool):
    """
    Shared implementation for the disable/enable endpoints (task 13.3).

    Flow (design): admin_get_user reads the current Enabled state first
    - already in the requested state -> 200 no-op returning the current
    state with no Cognito mutation, no audit-pending write, and no sync
    staging (13.6). Disable additionally runs the last-PortalAdmin
    guard (shared predicate, D14): disabling the last remaining enabled
    PortalAdmin -> 409 + the reason with the rejected attempt audited
    before any mutation (13.9). Otherwise: audit-pending
    (account_disable / account_enable) -> admin_disable_user /
    admin_enable_user (13.2, 13.3) -> the account's global
    Portal_Identity row's `status` set to disabled / enabled
    (portal-jwt-role-privilege-escalation Req 3.4, which is what makes a
    token minted before a disable stop being privileged) -> mark sync
    staging pending with the new enabled state (7.2; disable also
    satisfies 7.8's mark-as-disabled-on-next-sync) -> audit-final.

    Error mapping: UserNotFoundException -> 404; other Cognito failures
    -> 502 "action failed" with the state unchanged and the audit entry
    finalized to failure (13.7). A registry status write that fails after
    a successful Cognito mutation -> 502 "<verb> incomplete" with the
    partial state audited (Req 3.4). A pending-audit write failure -> 500
    "action not applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 13.2, 13.3, 13.6, 13.7, 13.9, 7.2, 7.8;
    portal-jwt-role-privilege-escalation 3.4, 4.1_
    """
    action = 'account_enable' if target_enabled else 'account_disable'
    verb = 'enable' if target_enabled else 'disable'
    state_word = 'enabled' if target_enabled else 'disabled'

    username = _username_from_path(event)
    if not username:
        return create_response(400, {'error': 'Username is required'})

    # Current state first (13.6): the enabled flag, plus the role for
    # the last-PortalAdmin guard on disable.
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        if error.get('Code') == 'UserNotFoundException':
            return create_response(404, {'error': 'User not found'})
        message = error.get('Message', str(e))
        logger.error(f"admin_get_user failed for {username}: {message}")
        return create_response(502, {'error': 'action failed'})

    current_enabled = bool(user.get('Enabled', False))

    if current_enabled == target_enabled:
        # Already in the requested state: 200 no-op returning the
        # current state - no Cognito mutation, no audit-pending write,
        # no sync staging (13.6).
        return create_response(200, {
            'message': f'{username} is already {state_word}',
            'username': username,
            'enabled': current_enabled,
            'changed': False,
        })

    acting_user = get_user_from_event(event)
    identity = attribution_from(event, acting_user)

    # Last-PortalAdmin guard on disable (D14, 5.3, 13.9): disabling
    # reduces the enabled-PortalAdmin count exactly like a role change
    # away from PortalAdmin. Here current_enabled is True (the states
    # differ), so the target counts toward the enabled pool iff its
    # role is PortalAdmin.
    if not target_enabled:
        attrs = {a['Name']: a['Value']
                 for a in user.get('UserAttributes', [])}
        if (attrs.get('custom:role') or 'Viewer') == 'PortalAdmin':
            try:
                admin_count = _count_enabled_portal_admins()
            except ClientError as e:
                message = e.response.get('Error', {}).get(
                    'Message', str(e))
                logger.error(
                    f"last-PortalAdmin guard count failed for "
                    f"{username}: {message}")
                return create_response(502, {'error': 'action failed'})

            if admin_count <= 1:
                reason = (f'{username} is the last remaining enabled '
                          f'PortalAdmin account; the portal must retain '
                          f'at least one enabled PortalAdmin')
                # The rejected attempt is audited before any mutation
                # (13.9); if it cannot be recorded, the action is
                # reported as not applied (6.4).
                try:
                    record_audit_event_strict(
                        acting_user['user_id'], action,
                        USER_ACCOUNT_RESOURCE_TYPE, username,
                        result='rejected',
                        details={'reason': reason},
                        identity=identity,
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
                    'error': 'Disable rejected',
                    'message': reason,
                })

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], action,
            USER_ACCOUNT_RESOURCE_TYPE, username,
            identity=identity,
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; {verb} for "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    try:
        if target_enabled:
            cognito_client.admin_enable_user(
                UserPoolId=USER_POOL_ID, Username=username)
        else:
            cognito_client.admin_disable_user(
                UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', '')
        message = error.get('Message', str(e))

        if code == 'UserNotFoundException':
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'user not found'},
                                 identity=identity)
            return create_response(404, {'error': 'User not found'})

        # Any other Cognito failure: the state is unchanged (13.7).
        logger.error(
            f"admin_{verb}_user failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message}, identity=identity)
        return create_response(502, {'error': 'action failed'})

    # The account's Portal_Identity status follows its Cognito state
    # (Requirement 3.4): a disabled row is denied exactly as an absent one
    # (Requirement 1.6), so disabling stops a token minted before the
    # change from being privileged on its next request — Cognito's own
    # disable only stops NEW sign-ins. Enabling restores it.
    registry_user_id = _resolve_registry_key(username, user=user)
    if registry_user_id:
        target_status = (REGISTRY_STATUS_ENABLED if target_enabled
                         else REGISTRY_STATUS_DISABLED)
        account_attrs = {a['Name']: a['Value']
                         for a in user.get('UserAttributes', [])}
        try:
            _update_registry_identity(registry_user_id, {
                'status': target_status,
                'username': username,
                'email': account_attrs.get('email', ''),
                'updated_at': int(time.time()),
                'updated_by': acting_user['user_id'],
            })
        except Exception as e:
            # The Cognito state changed but the registry status did not,
            # so the account's portal access does not match the state the
            # administrator asked for. Report it rather than claiming
            # success.
            logger.error(
                f"Portal_Identity registry status update failed for "
                f"{username} ({registry_user_id}) after a successful "
                f"Cognito {verb}: {e}")
            _mark_account_change_pending(username,
                                         {'enabled': target_enabled})
            finalize_audit_event(audit_event_id, 'failure', {
                'enabled': target_enabled,
                'partial_state': f'the Cognito account was {state_word} '
                                 f'but its Portal_Identity registry entry '
                                 f'was not updated, so its portal access '
                                 f'is unchanged',
                'reason': str(e),
            }, identity=identity)
            return create_response(502, {
                'error': f'{verb} incomplete',
                'message': f'The account {username} was {state_word} in '
                           f'the user pool but its portal registry entry '
                           f'was not updated, so its portal access is '
                           f'unchanged; retry the {verb}',
            })
    else:
        logger.error(
            f"No Cognito sub for {username}: its Portal_Identity registry "
            f"entry was not marked {state_word}, so its portal access is "
            f"unchanged")

    # The enabled/disabled state is a synchronized account attribute:
    # refresh every device's staged set and mark it pending (7.2;
    # disable also satisfies 7.8's mark-as-disabled-on-next-sync).
    _mark_account_change_pending(username, {'enabled': target_enabled})

    finalize_audit_event(audit_event_id, 'success',
                         {'enabled': target_enabled}, identity=identity)

    return create_response(200, {
        'message': f'{username} has been {state_word}',
        'username': username,
        'enabled': target_enabled,
        'changed': True,
    })


@require_portal_admin
def disable_account(event):
    """
    POST /api/v1/admin/users/{username}/disable

    _Requirements: 13.2, 13.6, 13.7, 13.9, 7.2, 7.8_
    """
    return _set_account_enabled(event, target_enabled=False)


@require_portal_admin
def enable_account(event):
    """
    POST /api/v1/admin/users/{username}/enable

    _Requirements: 13.3, 13.6, 13.7, 7.2_
    """
    return _set_account_enabled(event, target_enabled=True)


@require_portal_admin
def delete_account(event):
    """
    DELETE /api/v1/admin/users/{username}

    Flow (D13 ordering): admin_get_user captures the username, email,
    and role for the audit entry (14.8) and maps UserNotFoundException
    -> 404 with nothing modified (14.11) -> last-PortalAdmin guard
    (shared predicate, D14): deleting the last remaining enabled
    PortalAdmin -> 409 + the reason with the rejected attempt audited
    (14.3, 14.4) -> audit-pending (account_delete) -> admin_delete_user
    (14.2) -> remove the account's Portal_Identity registry rows
    (portal-jwt-role-privilege-escalation Req 3.4: an already-minted
    token stops being privileged on its next request) -> delete the
    edge-credentials verifier record (14.5) -> mark sync staging pending
    with enabled=false, deleted=true (7.8) -> audit-final.

    Error mapping: a Cognito failure aborts before the verifier record
    is touched - account and verifier record unchanged, audit-final
    failure (14.6). A verifier-delete failure after a successful
    Cognito delete retains the record for a subsequent attempt,
    finalizes the audit entry with a partial-cleanup detail, and
    returns an error stating the account was deleted but its verifier
    record was not removed (14.10); a registry-row removal failure is
    reported the same way, and both are attempted so one failure does
    not skip the other. A pending-audit write failure -> 500 "action not
    applied" with Cognito untouched (6.4, 6.5).

    _Requirements: 14.2, 14.3, 14.4, 14.5, 14.6, 14.8, 14.10, 14.11, 7.8;
    portal-jwt-role-privilege-escalation 3.4, 4.1_
    """
    username = _username_from_path(event)
    if not username:
        return create_response(400, {'error': 'Username is required'})

    # Current state first: username/email/role for the audit entry
    # (14.8), the role and enabled flag for the last-PortalAdmin guard.
    # A missing account -> 404 with nothing modified (14.11).
    try:
        user = cognito_client.admin_get_user(
            UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        if error.get('Code') == 'UserNotFoundException':
            return create_response(404, {
                'error': 'User not found',
                'message': f'The account {username} was not found',
            })
        message = error.get('Message', str(e))
        logger.error(f"admin_get_user failed for {username}: {message}")
        return create_response(502, {'error': 'deletion failed'})

    attrs = {a['Name']: a['Value'] for a in user.get('UserAttributes', [])}
    email = attrs.get('email', '')
    role = attrs.get('custom:role') or 'Viewer'
    target_enabled = bool(user.get('Enabled', False))

    acting_user = get_user_from_event(event)
    identity = attribution_from(event, acting_user)

    # The Portal_Identity key must be read BEFORE the account is deleted:
    # afterwards the pool can no longer resolve the username to its `sub`
    # (exactly what the recorded incident exploited).
    registry_user_id = _resolve_registry_key(username, user=user)

    # Last-PortalAdmin guard (D14, 14.3): deleting an enabled
    # PortalAdmin reduces the enabled-PortalAdmin count exactly like a
    # role change away from PortalAdmin or a disable.
    if role == 'PortalAdmin' and target_enabled:
        try:
            admin_count = _count_enabled_portal_admins()
        except ClientError as e:
            message = e.response.get('Error', {}).get('Message', str(e))
            logger.error(
                f"last-PortalAdmin guard count failed for {username}: "
                f"{message}")
            return create_response(502, {'error': 'deletion failed'})

        if admin_count <= 1:
            reason = (f'{username} is the last remaining enabled '
                      f'PortalAdmin account; the portal must retain at '
                      f'least one enabled PortalAdmin')
            # The rejected attempt is itself audited (14.4); if it
            # cannot be recorded, the action is reported as not
            # applied (6.4).
            try:
                record_audit_event_strict(
                    acting_user['user_id'], 'account_delete',
                    USER_ACCOUNT_RESOURCE_TYPE, username,
                    result='rejected',
                    details={'reason': reason},
                    identity=identity,
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
                'error': 'Deletion rejected',
                'message': reason,
            })

    # Audit-before-effect: the pending entry must be recorded before
    # Cognito is touched; if it cannot be, the action is not applied
    # (Req 6.4, 6.5). The entry carries the account's email and role
    # at the time of deletion (14.8).
    try:
        audit_event_id = record_audit_event_strict(
            acting_user['user_id'], 'account_delete',
            USER_ACCOUNT_RESOURCE_TYPE, username,
            details={'email': email, 'role': role},
            identity=identity,
        )
    except Exception as e:
        logger.error(
            f"Pending audit write failed; deletion of "
            f"{username} not applied: {e}")
        return create_response(500, {
            'error': 'Audit log unavailable',
            'message': 'The action was not applied',
        })

    # Cognito delete first (D13): a failure here aborts before the
    # verifier record is touched, leaving the account and its
    # Edge_Credential_Verifier record unchanged (14.6).
    try:
        cognito_client.admin_delete_user(
            UserPoolId=USER_POOL_ID, Username=username)
    except ClientError as e:
        error = e.response.get('Error', {})
        code = error.get('Code', '')
        message = error.get('Message', str(e))

        if code == 'UserNotFoundException':
            finalize_audit_event(audit_event_id, 'failure',
                                 {'reason': 'user not found'},
                                 identity=identity)
            return create_response(404, {
                'error': 'User not found',
                'message': f'The account {username} was not found',
            })

        logger.error(f"admin_delete_user failed for {username}: {message}")
        finalize_audit_event(audit_event_id, 'failure',
                             {'reason': message}, identity=identity)
        return create_response(502, {'error': 'deletion failed'})

    # Portal_Identity removal (Requirement 3.4), first of the cleanups
    # because it is the one that carries privilege: until the row is gone,
    # a token minted before the deletion still resolves to its role on its
    # next request. Both cleanups are attempted so one failure does not
    # skip the other.
    registry_cleanup_error = None
    if registry_user_id:
        try:
            _delete_registry_identity(registry_user_id)
        except Exception as e:
            registry_cleanup_error = e
            logger.error(
                f"Portal_Identity registry removal failed for {username} "
                f"({registry_user_id}) after a successful Cognito delete; "
                f"the deleted account keeps its portal role until the row "
                f"is removed: {e}")
    else:
        logger.error(
            f"No Cognito sub for {username}: its Portal_Identity registry "
            f"rows could not be addressed and may still name a role")

    # The account is deleted from the User_Pool: mark it disabled and
    # deleted in every device's staged sync set regardless of what the
    # verifier cleanup does next (7.8; failures inside are logged,
    # never raised).
    _mark_account_change_pending(username, {'enabled': False,
                                            'deleted': True})

    # Verifier record cleanup (14.5). A failure after the successful
    # Cognito delete retains the record for a subsequent attempt,
    # finalizes the audit entry with a partial-cleanup detail, and
    # reports that the account was deleted but its verifier record was
    # not removed (14.10).
    verifier_cleanup_error = None
    try:
        dynamodb.Table(EDGE_CREDENTIALS_TABLE).delete_item(
            Key={'username': username.lower()})
    except Exception as e:
        verifier_cleanup_error = e
        logger.error(
            f"Edge-credentials record delete failed for {username} "
            f"after a successful Cognito delete: {e}")

    if registry_cleanup_error is not None or verifier_cleanup_error is not None:
        # The account is gone from the pool but something it owned is
        # not: report the partial state and record it (14.10, Req 3.4).
        audit_leftovers = []
        response_leftovers = []
        if registry_cleanup_error is not None:
            audit_leftovers.append('its Portal_Identity registry entry')
            response_leftovers.append('its portal registry entry')
        if verifier_cleanup_error is not None:
            audit_leftovers.append('its credential record')
            response_leftovers.append('its verifier record')

        finalize_audit_event(audit_event_id, 'success', {
            'email': email,
            'role': role,
            'partial_cleanup': f"the account was deleted from the user "
                               f"pool but {' and '.join(audit_leftovers)} "
                               f"was not removed; it is retained for a "
                               f"subsequent removal attempt",
        }, identity=identity)
        return create_response(502, {
            'error': 'partial deletion',
            'message': f"The account {username} was deleted but "
                       f"{' and '.join(response_leftovers)} was not "
                       f"removed; it will be removed on a subsequent "
                       f"attempt",
        })

    # Audit-final records the deleted account's username, email, and
    # role at the time of deletion (14.8).
    finalize_audit_event(audit_event_id, 'success', {
        'email': email,
        'role': role,
    }, identity=identity)

    return create_response(200, {
        'message': f'Account {username} has been deleted',
        'username': username,
        'email': email,
        'role': role,
        'deleted': True,
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
