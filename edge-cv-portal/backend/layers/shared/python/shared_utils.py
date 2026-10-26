"""
Shared utilities for Edge CV Portal Lambda functions
"""
import json
import os
import logging
import uuid
import time
from typing import Dict, Any, Optional, List, Set, FrozenSet, Tuple, Union
from datetime import datetime
from decimal import Decimal
import boto3
import botocore
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from enum import Enum
from functools import wraps
# Re-exported so handlers import the internal-invocation helpers from
# shared_utils like everything else (portal-authorization-and-iam-scoping,
# design I2); the module itself is standard-library only.
from internal_invocation import INTERNAL_MARKER_KEY, internal_marker, is_internal_invocation

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The SDK is vendored into this layer (layers/shared/requirements.txt) so the
# S3 bucket ABAC / tagging operations the use-case bucket configuration needs
# do not depend on the Lambda-managed runtime's bundled copy. Logged once per
# cold start so the deployed version is readable from any function's log
# stream.
logger.info("shared layer botocore %s", botocore.__version__)

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


def get_usecase_region(usecase: Dict) -> str:
    """
    Get AWS region from use case configuration.
    
    Priority:
    1. Use case 'region' field (from DynamoDB)
    2. Lambda environment variable AWS_REGION
    3. Default to us-east-1
    
    Args:
        usecase: Use case dict from DynamoDB
    
    Returns:
        AWS region string (e.g., 'us-east-1', 'us-east-2')
    """
    region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))
    logger.info(f"Using region {region} for use case {usecase.get('usecase_id', 'unknown')}")
    return region


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


def _display_identity(email: Optional[str], username: Optional[str], user_id: Optional[str]) -> str:
    """The value handlers persist as `created_by`.

    Prefer the email, but never hand downstream the literal 'unknown' when a
    real identifier exists: a Cognito user created without an `email`
    attribute (e.g. the bootstrap `admin` user) issues ID tokens with no email
    claim, and the JWT authorizer forwards the string 'unknown' in its place —
    which is how every record such a user creates ended up showing
    "Created By: unknown". Fall back to the username, then the sub.
    """
    for candidate in (email, username, user_id):
        if candidate and str(candidate).strip() and str(candidate).strip().lower() != 'unknown':
            return str(candidate).strip()
    return 'unknown'


def get_user_from_event(event: Dict) -> Dict[str, str]:
    """Extract user information from API Gateway event

    The returned `role` is the **Claimed_Role**: the token's
    `custom:role` attribute (or the Lambda authorizer's `role` context
    value). It is descriptive metadata about what the caller claims to
    be — it is NOT authoritative and grants nothing once
    PORTAL_REGISTRY_ENFORCED is on; privilege comes from the
    Portal_Identity registry (see RBACManager). The shape is unchanged so
    every existing caller keeps working
    (portal-jwt-role-privilege-escalation, design.md Decision 2).

    The event is also remembered as the request audit context (see
    `_remember_request_event`), so audit entries written later in the same
    invocation without `identity`/`event` still carry the MCP marker.
    """
    _remember_request_event(event)
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        
        # Check if using Cognito User Pools authorizer (claims)
        claims = authorizer.get('claims', {})
        if claims:
            user_id = claims.get('sub', 'unknown')
            username = claims.get('cognito:username', claims.get('username', 'unknown'))
            return {
                'user_id': user_id,
                'email': _display_identity(claims.get('email'), username, user_id),
                'username': username,
                'role': claims.get('custom:role', 'Viewer')
            }
        
        # Check if using Lambda JWT authorizer (context)
        context = authorizer
        if context.get('userId'):
            user_id = context.get('userId', 'unknown')
            username = context.get('username', 'unknown')
            return {
                'user_id': user_id,
                'email': _display_identity(context.get('email'), username, user_id),
                'username': username,
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


# ---------------------------------------------------------------------------
# Durable audit attribution (portal-jwt-role-privilege-escalation,
# Requirement 4, design.md Decision 7)
# ---------------------------------------------------------------------------
# The recorded incident deleted its Cognito user 2 seconds after acting, so
# the surviving audit row named a `sub` that resolves to nothing in any of
# the account's user pools. Attribution is therefore captured FROM THE
# REQUEST at write time and never resolved from Cognito later — resolving
# later is exactly what the incident made impossible.
#
# The five Attribution_Fields are TOP-LEVEL audit attributes (not `details`
# entries), so they are additive to the existing schema, cannot collide
# with the `details` denylist, and cannot be redacted by it.

# Recorded when a value is not present in the request. Never fabricated,
# never resolved from elsewhere (Requirement 4.3).
UNKNOWN_ATTRIBUTION = 'unknown'

# The Attribution_Fields, in the order they are documented (design.md
# Glossary; Requirement 4.1).
ATTRIBUTION_FIELDS = ('username', 'email', 'source_ip', 'user_agent',
                      'identity_source')

# The MCP_Audit_Marker (mcp-servers design, "Shared-layer and audit-log
# changes"): `channel` is written on every audit entry ('api' or 'mcp'); the
# four `mcp_*` attributes only on entries written during an MCP-originated
# invocation. The source is the `mcp` block the Portal MCP Lambda adds to
# the event it dispatches; API Gateway events never carry that block.
MCP_MARKER_FIELDS = ('channel', 'mcp_tool', 'mcp_server', 'mcp_client',
                     'mcp_request_id')
AUDIT_CHANNEL_API = 'api'
AUDIT_CHANNEL_MCP = 'mcp'

# marker attribute -> key of the event's `mcp` block
_MCP_BLOCK_KEYS = (('mcp_tool', 'tool'), ('mcp_server', 'server'),
                   ('mcp_client', 'client'), ('mcp_request_id', 'request_id'))

# Request audit context (mcp-servers design D25). A Lambda process serves one
# invocation at a time, so every audit write between a handler's
# `get_user_from_event(event)` and its return belongs to that invocation.
# The remembered event is bound to the invocation's `_X_AMZN_TRACE_ID`, so a
# context left by an earlier invocation is inert.
_REQUEST_AUDIT_CONTEXT: Optional[dict] = None


def _mcp_block(event: Optional[Dict]) -> Optional[Dict]:
    """The event's `mcp` block when it is a dict, else None."""
    if not isinstance(event, dict):
        return None
    block = event.get('mcp')
    return block if isinstance(block, dict) else None


def _remember_request_event(event: Optional[Dict]) -> None:
    """Store `event` as the request audit context of this invocation.

    Every call overwrites the context, except when the stored event carries
    an `mcp` block, the new one does not, and the trace id is unchanged: a
    handler that builds an in-process event within an MCP-originated
    invocation (for example `synthetic_data.retrain_session` calling
    `training.create_training_job`) keeps the marker. Never raises.
    """
    global _REQUEST_AUDIT_CONTEXT
    try:
        invocation = os.environ.get('_X_AMZN_TRACE_ID')
        stored = _REQUEST_AUDIT_CONTEXT
        if (stored is not None
                and _mcp_block(stored.get('event')) is not None
                and _mcp_block(event) is None
                and stored.get('invocation') == invocation):
            return
        _REQUEST_AUDIT_CONTEXT = {'event': event, 'invocation': invocation}
    except Exception as error:  # noqa: BLE001 - audit context never fails a request
        logger.warning(f"Could not record the request audit context: {error}")


def _current_request_mcp_event() -> Optional[Dict]:
    """The remembered event when it belongs to the current invocation and
    carries an `mcp` block; otherwise None."""
    stored = _REQUEST_AUDIT_CONTEXT
    if stored is None:
        return None
    if stored.get('invocation') != os.environ.get('_X_AMZN_TRACE_ID'):
        return None
    event = stored.get('event')
    return event if _mcp_block(event) is not None else None


def _identifying_value(*candidates) -> str:
    """First candidate that actually identifies something, else 'unknown'.

    The literal 'unknown' a claim-less token produces (see
    `get_user_from_event`) is not an identity, so it never wins over a
    later candidate (Requirement 4.3).
    """
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text and text.lower() != UNKNOWN_ATTRIBUTION:
            return text
    return UNKNOWN_ATTRIBUTION


def _authorizer_identity_inputs(event: Optional[Dict]) -> tuple:
    """(claims, authorizer_context, requestContext.identity) of an event.

    All three default to {} so a non-proxy / partially shaped invocation
    records 'unknown' instead of failing the request.
    """
    request_context = (event or {}).get('requestContext') or {}
    authorizer = request_context.get('authorizer') or {}
    claims = authorizer.get('claims') or {}
    identity = request_context.get('identity') or {}
    return claims, authorizer, identity


def _claimed_email(user: Optional[Dict]) -> Optional[str]:
    """The email of a `get_user_from_event` dict, or None.

    `get_user_from_event` substitutes the username and then the `sub` when
    a token carries no `email` claim (`_display_identity`, so records never
    read "Created By: unknown"). Attribution must not repeat that
    substitution as if it were an email address, so a value equal to the
    username or the sub is treated as absent.
    """
    email = (user or {}).get('email')
    if not email:
        return None
    if email in ((user or {}).get('username'), (user or {}).get('user_id')):
        return None
    return email


def _identity_source_for(user_id: Optional[str],
                         usecase_id: Optional[str] = None) -> str:
    """The Identity_Source to record: 'registry' when the caller holds a
    Portal_Identity that decides this scope, 'absent' when it does not.

    'unknown' is recorded when the registry itself could not be read: an
    availability failure decided no role, so claiming either 'registry' or
    'absent' would be a fabricated statement about the request
    (Requirement 4.1 names the two deciding values; 4.3 is the convention
    for a value the request cannot supply). Never raises — attribution
    must never be the reason an audit entry is lost.
    """
    try:
        identity = _lookup_identity(user_id, usecase_id)
    except RegistryUnavailable as error:
        logger.warning(f"Identity_Source undetermined for user {user_id} / "
                       f"scope {usecase_id!r}: {error}")
        return UNKNOWN_ATTRIBUTION
    except Exception as error:  # noqa: BLE001 - attribution never fails a write
        logger.warning(f"Identity_Source lookup failed for user {user_id} / "
                       f"scope {usecase_id!r}: {error}")
        return UNKNOWN_ATTRIBUTION
    return (IDENTITY_SOURCE_REGISTRY if identity is not None
            else IDENTITY_SOURCE_ABSENT)


def attribution_from(event: Optional[Dict], user: Optional[Dict] = None,
                     usecase_id: Optional[str] = None,
                     identity_source: Optional[str] = None) -> Dict[str, str]:
    """The Attribution_Fields for a request, taken from the request itself.

    This is the ONE place that reads `requestContext.identity.sourceIp` /
    `.userAgent` (the precedent being `quick_setup.py:_source_ip`), so the
    shape of that read is fixed in a single spot.

    Args:
        event: the API Gateway event being audited (claims + identity)
        user: the `get_user_from_event` dict, when the caller already has
            it; derived from the event otherwise
        usecase_id: scope of the authorization decision, so the recorded
            Identity_Source reflects the row that decided it
        identity_source: pass explicitly to record a known value without a
            registry read (e.g. a decision already resolved)

    Returns:
        dict with exactly the five Attribution_Fields, every value a
        non-empty string ('unknown' where the request carries nothing).
        When the event carries a dict `mcp` block (an MCP-originated
        dispatch) the dict also holds the five MCP_MARKER_FIELDS; for any
        other event the result is unchanged.
        Never raises: losing attribution must not fail a request, and a
        write with 'unknown' fields is still an attributable-by-`sub`
        record (Requirement 4.3).
    """
    try:
        claims, authorizer, identity_block = _authorizer_identity_inputs(event)
        if user is None:
            user = get_user_from_event(event) if event is not None else {}

        user_id = (user or {}).get('user_id')
        username = _identifying_value(
            (user or {}).get('username'),
            claims.get('cognito:username'), claims.get('username'),
            authorizer.get('username'))
        mcp = _mcp_block(event)
        email_candidates = [claims.get('email'), authorizer.get('email'),
                            _claimed_email(user)]
        if mcp is not None:
            # An access token carries no email claim; the MCP Lambda resolves
            # it and passes it in the block.
            email_candidates.insert(1, mcp.get('principal_email'))
        email = _identifying_value(*email_candidates)
        source_ip = _identifying_value(identity_block.get('sourceIp'))
        user_agent = _identifying_value(identity_block.get('userAgent'))

        if identity_source is None and mcp is not None \
                and mcp.get('identity_source'):
            # Decided by the MCP Lambda's authorization step; no registry read.
            identity_source = mcp.get('identity_source')
        if identity_source is None:
            identity_source = _identity_source_for(user_id, usecase_id)

        attribution = {
            'username': username,
            'email': email,
            'source_ip': source_ip,
            'user_agent': user_agent,
            'identity_source': _identifying_value(identity_source),
        }
        if mcp is not None:
            attribution['channel'] = AUDIT_CHANNEL_MCP
            for field, key in _MCP_BLOCK_KEYS:
                attribution[field] = _identifying_value(mcp.get(key))
        return attribution
    except Exception as error:  # noqa: BLE001 - attribution never raises
        logger.error(f"Error building audit attribution: {error}")
        return {field: UNKNOWN_ATTRIBUTION for field in ATTRIBUTION_FIELDS}


def _attribution_attributes(identity: Optional[Dict] = None,
                            event: Optional[Dict] = None,
                            usecase_id: Optional[str] = None) -> Dict[str, str]:
    """The top-level attribution attributes of an audit item.

    Every audit entry carries all five Attribution_Fields (Requirement 4.1):
    an explicit `identity` mapping wins, an `event` is read through
    `attribution_from`, and anything neither supplies records 'unknown'.
    When neither is passed, the request audit context of the current
    invocation is used if it carries an `mcp` block (D25).

    Every entry also carries `channel` ('api' unless the attribution is
    MCP-marked); the four `mcp_*` attributes are added only for 'mcp'.
    """
    if identity is None and event is None:
        event = _current_request_mcp_event()
    if identity is None and event is not None:
        identity = attribution_from(event, usecase_id=usecase_id)
    identity = identity or {}
    attributes = {field: _identifying_value(identity.get(field))
                  for field in ATTRIBUTION_FIELDS}
    channel = identity.get('channel') or AUDIT_CHANNEL_API
    attributes['channel'] = channel
    if channel == AUDIT_CHANNEL_MCP:
        for field in MCP_MARKER_FIELDS[1:]:
            attributes[field] = _identifying_value(identity.get(field))
    return attributes


def log_audit_event(user_id: str, action: str, resource_type: str, 
                   resource_id: str, result: str, details: Optional[Dict] = None,
                   identity: Optional[Dict] = None,
                   event: Optional[Dict] = None):
    """Log audit event to DynamoDB.

    `identity` carries the Attribution_Fields (build it with
    `attribution_from`), or pass the raw `event` to have them derived.
    They are written as top-level attributes alongside today's nine keys,
    which keep their meaning — including `event_id`'s
    f"{user_id}_{timestamp}" shape, which both audit GSIs and existing
    readers depend on (design.md Decision 7). Missing values record
    'unknown' and the entry is still written (Requirement 4.3).

    Failures are still swallowed: audit loss must not fail a request.
    """
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
        item.update(_attribution_attributes(identity, event))
        
        table.put_item(Item=item)
        logger.info(f"Audit event logged: {action} on {resource_type}/{resource_id}")
    except Exception as e:
        logger.error(f"Error logging audit event: {str(e)}")


# Strict (raising) audit helpers — portal-user-manager
#
# Unlike log_audit_event above (which swallows errors), these helpers
# implement the two-phase audit-before-effect protocol: a 'pending' entry
# is written (and any failure RAISED) before the guarded operation runs,
# then the entry is finalized to its terminal result afterwards.

# Action types recorded against user accounts by the User Manager.
USER_ACCOUNT_AUDIT_ACTIONS = ('password_change', 'forgot_password', 'role_change',
                              'account_create', 'account_disable',
                              'account_enable', 'account_delete')

# Resource type used for user-account audit entries.
USER_ACCOUNT_RESOURCE_TYPE = 'user_account'

# Valid audit results for the two-phase protocol.
AUDIT_RESULT_PENDING = 'pending'
AUDIT_RESULTS = ('pending', 'success', 'failure', 'rejected')
AUDIT_FINAL_RESULTS = ('success', 'failure', 'rejected')

# Denylist for audit details: 'temp*' is a prefix match (temporary
# passwords), the others match anywhere in the key (password_hash,
# new_password, credential_verifier, ...).
_AUDIT_DETAILS_DENY_SUBSTRINGS = ('password', 'verifier', 'hash')
_AUDIT_DETAILS_DENY_PREFIXES = ('temp',)


def _is_denied_audit_detail_key(key: Any) -> bool:
    """True when a details key must never reach the audit log."""
    normalized = str(key).lower()
    return (
        any(term in normalized for term in _AUDIT_DETAILS_DENY_SUBSTRINGS)
        or normalized.startswith(_AUDIT_DETAILS_DENY_PREFIXES)
    )


def sanitize_audit_details(details: Optional[Dict]) -> Dict:
    """Drop denylisted keys (password / verifier / hash / temp*) from
    audit details, recursively through nested dicts and lists.

    Passwords, password hashes, credential verifiers, and temporary
    password values must never be written to the audit log (Req 6.3).
    """
    def _sanitize(value):
        if isinstance(value, dict):
            return {
                key: _sanitize(nested)
                for key, nested in value.items()
                if not _is_denied_audit_detail_key(key)
            }
        if isinstance(value, (list, tuple)):
            return [_sanitize(nested) for nested in value]
        return value

    return _sanitize(details or {})


def record_audit_event_strict(user_id: str, action: str, resource_type: str,
                              resource_id: str, result: str = AUDIT_RESULT_PENDING,
                              details: Optional[Dict] = None,
                              identity: Optional[Dict] = None,
                              event: Optional[Dict] = None) -> str:
    """Write an audit entry and RAISE on failure (strict variant of
    log_audit_event).

    First phase of the audit-before-effect protocol: call with
    result='pending' BEFORE performing the guarded operation; if this
    raises, the caller must abort the operation (Req 6.4, 6.5).

    `identity` / `event` carry the Attribution_Fields exactly as for
    log_audit_event, written as top-level attributes so the actor stays
    named after the Cognito user is deleted (Requirement 4.1, 4.2).

    Returns the event_id used to finalize_audit_event() afterwards.
    """
    if result not in AUDIT_RESULTS:
        raise ValueError(f"Invalid audit result: {result!r} "
                         f"(expected one of {AUDIT_RESULTS})")

    table = dynamodb.Table(AUDIT_LOG_TABLE)
    timestamp = int(datetime.utcnow().timestamp() * 1000)
    event_id = f"{user_id}_{timestamp}_{uuid.uuid4().hex[:8]}"

    item = {
        'event_id': event_id,
        'timestamp': timestamp,
        'user_id': user_id,
        'action': action,
        'resource_type': resource_type,
        'resource_id': resource_id,
        'result': result,
        'details': sanitize_audit_details(details),
        'ttl': timestamp + (90 * 24 * 60 * 60 * 1000)  # 90 days retention
    }
    item.update(_attribution_attributes(identity, event))

    table.put_item(Item=item)
    logger.info(f"Strict audit event recorded ({result}): "
                f"{action} on {resource_type}/{resource_id}")
    return event_id


def finalize_audit_event(event_id: str, result: str,
                         details: Optional[Dict] = None,
                         identity: Optional[Dict] = None,
                         event: Optional[Dict] = None):
    """Second phase of the audit-before-effect protocol: move a pending
    entry to its terminal result ('success' | 'failure' | 'rejected'),
    merging sanitized details and stamping the completion time (Req 6.1).

    `identity` / `event` may supply the Attribution_Fields when the
    pending phase could not (they are only written when supplied here, so
    a finalize without them leaves the recorded attribution alone).

    Raises on lookup/update failure so callers can surface the problem.
    """
    if result not in AUDIT_FINAL_RESULTS:
        raise ValueError(f"Invalid final audit result: {result!r} "
                         f"(expected one of {AUDIT_FINAL_RESULTS})")

    table = dynamodb.Table(AUDIT_LOG_TABLE)

    # The table key is (event_id, timestamp); recover the range key from
    # the partition so callers only need the event_id handle.
    response = table.query(
        KeyConditionExpression=Key('event_id').eq(event_id)
    )
    items = response.get('Items', [])
    if not items:
        raise ValueError(f"Audit event not found: {event_id}")
    entry = items[0]

    completed_at = int(datetime.utcnow().timestamp() * 1000)
    merged_details = dict(entry.get('details') or {})
    merged_details.update(sanitize_audit_details(details))

    update_expression = ('SET #result = :result, #details = :details, '
                        'completed_at = :completed_at')
    names = {'#result': 'result', '#details': 'details'}
    values = {
        ':result': result,
        ':details': merged_details,
        ':completed_at': completed_at,
    }

    if identity is not None or event is not None:
        for field, value in _attribution_attributes(identity, event).items():
            update_expression += f", #{field} = :{field}"
            names[f"#{field}"] = field
            values[f":{field}"] = value

    table.update_item(
        Key={'event_id': event_id, 'timestamp': entry['timestamp']},
        UpdateExpression=update_expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    logger.info(f"Audit event finalized ({result}): {event_id}")


# RBAC Authorization System
class Role(Enum):
    """User roles with hierarchical permissions"""
    VIEWER = "Viewer"
    OPERATOR = "Operator"
    DATA_SCIENTIST = "DataScientist"
    USECASE_ADMIN = "UseCaseAdmin"
    PORTAL_ADMIN = "PortalAdmin"
    # Restricted labeler role (dda-data-labeling, Req 2.1): grants only
    # the labeler APIs (labeling:tasks-self) and nothing else, so every
    # other endpoint 403s through the standard @rbac_check path.
    DATA_LABELER = "DataLabeler"


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
    # DDA Data Labeling (dda-data-labeling, Req 2.3, 3.7)
    # labeling:tasks-self: the labeler APIs — a Data_Labeler may access
    # only their own Task_Assignments (handlers additionally filter by
    # assignment ownership).
    LABELING_TASKS_SELF = "labeling:tasks-self"
    # labeling-teams:manage: create/modify Labeling_Teams and membership.
    MANAGE_LABELING_TEAMS = "labeling-teams:manage"
    
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
    
    # Workflow Manager
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_CREATE = "workflow:create"
    WORKFLOW_EDIT = "workflow:edit"
    WORKFLOW_SAVE = "workflow:save"
    WORKFLOW_DELETE = "workflow:delete"
    WORKFLOW_TEST = "workflow:test"
    WORKFLOW_PACKAGE = "workflow:package"
    WORKFLOW_DEPLOY = "workflow:deploy"
    BEDROCK_CONFIG_WRITE = "bedrock-config:write"
    
    # Node Designer (custom-node-designer, Requirement 13)
    NODE_DESIGNER_READ = "node-designer:read"
    NODE_DESIGNER_CREATE = "node-designer:create"
    NODE_DESIGNER_GENERATE = "node-designer:generate"
    NODE_DESIGNER_IMPORT = "node-designer:import"
    NODE_DESIGNER_SIMULATE = "node-designer:simulate"
    NODE_DESIGNER_REGISTER = "node-designer:register"
    NODE_DESIGNER_PROMOTE_DEMOTE = "node-designer:promote-demote"
    NODE_DESIGNER_MANAGE = "node-designer:manage"
    NODE_DESIGNER_SECURITY_REVIEW = "node-designer:security-review"
    
    # Build Fleet (portal-build-fleet-and-workflow-gates, Req 1.6, 4.10, 9.6)
    # Build_Operator capability: submit/cancel/read Build_Jobs. Builds are
    # not Use_Case-scoped; these permissions are checked at the 'global'
    # scope (the allow_global pattern in rbac_middleware.py).
    BUILDS_SUBMIT = "builds:submit"
    BUILDS_CANCEL = "builds:cancel"
    BUILDS_READ = "builds:read"
    
    # Workflow telemetry dashboards (workflow-telemetry-dashboards design
    # §11.1): dashboards:read views dashboards and their tiles;
    # dashboards:manage creates, edits and deletes them; telemetry:manage
    # enables telemetry for a use case and tunes its settings.
    DASHBOARDS_READ = "dashboards:read"
    DASHBOARDS_MANAGE = "dashboards:manage"
    TELEMETRY_MANAGE = "telemetry:manage"
    
    # System Administration
    VIEW_AUDIT_LOGS = "view_audit_logs"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_USERS = "manage_users"
    
    # Super User Permissions
    ALL_USECASES_ACCESS = "all_usecases_access"
    SYSTEM_ADMIN = "system_admin"


# ---------------------------------------------------------------------------
# Portal_Identity registry (portal-jwt-role-privilege-escalation)
#
# Portal privilege comes from a row in USER_ROLES_TABLE
# (dda-portal-user-roles) keyed (user_id = Cognito sub, usecase_id), which
# only the portal's own User Manager writes. `usecase_id='global'` is the
# account-level entry; any other value is a Use_Case assignment. The
# `custom:role` token claim (Claimed_Role) is descriptive metadata only:
# a single cognito-idp:AdminCreateUser call must not mint a privileged
# portal principal (bugfix.md Bug Condition C1, Requirement 1).
#
# Enforcement is gated by PORTAL_REGISTRY_ENFORCED, which defaults to
# FALSE: until the registry has been backfilled from the pool
# (Requirement 2), turning it on would lock every user out, including the
# bootstrap `admin`. With the flag off, resolution is byte-for-byte
# today's order plus a `would_deny` WARNING naming every request
# enforcement would have denied — a dry run that quantifies the
# backfill's completeness against real traffic (design.md Decision 4).
# ---------------------------------------------------------------------------

# The account-level registry scope (the 'global' sort key).
GLOBAL_SCOPE = 'global'

# Which input decided the Effective_Role, recorded on audit entries
# (Identity_Source, Requirement 4.1).
IDENTITY_SOURCE_REGISTRY = 'registry'
IDENTITY_SOURCE_ABSENT = 'absent'

# Portal_Identity.status (design.md Decision 1). Rows written before this
# spec (Team Management grants) carry no `status` at all and are treated
# as enabled; anything other than 'enabled' denies, so an unrecognized
# value fails closed (Requirement 1.6).
REGISTRY_STATUS_ENABLED = 'enabled'
REGISTRY_STATUS_DISABLED = 'disabled'

# The single enforcement switch (design.md Decision 4). Read from the
# environment on every call, not cached at import, so a redeploy or a
# test can flip it without re-importing the layer.
PORTAL_REGISTRY_ENFORCED_ENV = 'PORTAL_REGISTRY_ENFORCED'
_ENFORCEMENT_TRUE_VALUES = ('1', 'true', 'yes', 'on', 'enabled')


class RegistryUnavailable(Exception):
    """The Portal_Identity registry could not be read.

    Distinct from "this principal has no registry row" (which is a
    permission decision, answered 403): a failed lookup is an
    availability problem and must surface as HTTP 500 with an audited
    `result='failure'`, never as a permission denial and never as a
    silent downgrade to Viewer (Requirement 1.5, design.md Decision 3).
    A DynamoDB outage must not be recorded as thousands of privilege
    decisions.
    """


def registry_enforcement_enabled() -> bool:
    """True when the Portal_Identity registry is authoritative.

    Defaults to False (Requirement 2.4): enforcement is a single
    explicit configuration value and stays off until the backfill has
    run.
    """
    raw = os.environ.get(PORTAL_REGISTRY_ENFORCED_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() in _ENFORCEMENT_TRUE_VALUES


def _identity_is_enabled(identity: Optional[Dict]) -> bool:
    """True when a Portal_Identity row exists and is not disabled.

    A missing `status` means enabled: rows written before this spec
    (Team Management's per-Use_Case grants) do not carry one. Any other
    value than 'enabled' is treated as disabled, so an unexpected value
    denies rather than grants (Requirement 1.6).
    """
    if not identity:
        return False
    status = identity.get('status')
    if status is None:
        return True
    return str(status).strip().lower() == REGISTRY_STATUS_ENABLED


def _identity_role(identity: Optional[Dict]) -> Optional[Role]:
    """The `Role` a Portal_Identity row names, or None when the row is
    absent or names a value that is not a Role (corrupt data must not
    grant anything)."""
    if not identity:
        return None
    role_str = identity.get('role')
    try:
        return Role(role_str)
    except ValueError:
        logger.warning(
            f"Portal_Identity row for user {identity.get('user_id')} / "
            f"usecase {identity.get('usecase_id')} names an unknown role "
            f"{role_str!r}; treating the row as carrying no role")
        return None


def _lookup_identity(user_id: str,
                     usecase_id: Optional[str] = None) -> Optional[Dict]:
    """Read the Portal_Identity that decides this request, or None.

    Reads the caller's global row and, when `usecase_id` names a
    Use_Case (anything other than 'global'), that Use_Case row too, then
    applies the precedence rules of design.md's Expected Behavior table:

    1. no enabled global row -> None. The caller is an
       Unprovisioned_Principal and is denied (Requirement 1.1). The
       global row is the account's provisioning record: it is what the
       User Manager writes at create time and what the backfill
       populates, so requiring it is what makes "created directly in
       Cognito" resolve to nothing.
    2. an enabled Use_Case row naming a valid role, when the request is
       scoped to that Use_Case -> that row (it overrides the global row,
       today's step-3 precedence, Requirement 1.3).
    3. otherwise -> the global row.

    A disabled (or role-less) Use_Case row is treated exactly as an
    absent one and falls back to the global row; a disabled global row
    denies exactly as an absent one (Requirement 1.6).

    Raises:
        RegistryUnavailable: the lookup itself failed. NOT swallowed —
            the caller must answer 500, not 403 (Requirement 1.5).
    """
    if not user_id or user_id == 'unknown':
        # Nothing to look up: an unidentifiable caller can hold no
        # Portal_Identity, and this is not an availability failure.
        return None

    try:
        user_roles_table = dynamodb.Table(USER_ROLES_TABLE)
        global_identity = user_roles_table.get_item(
            Key={'user_id': user_id, 'usecase_id': GLOBAL_SCOPE}
        ).get('Item')

        usecase_identity = None
        if usecase_id and usecase_id != GLOBAL_SCOPE:
            usecase_identity = user_roles_table.get_item(
                Key={'user_id': user_id, 'usecase_id': usecase_id}
            ).get('Item')
    except ClientError as error:
        raise RegistryUnavailable(
            f"Portal_Identity lookup failed for user {user_id} / usecase "
            f"{usecase_id}: {error}") from error
    except Exception as error:  # noqa: BLE001 - fail closed, never silent
        raise RegistryUnavailable(
            f"Portal_Identity lookup failed for user {user_id} / usecase "
            f"{usecase_id}: {error}") from error

    if not _identity_is_enabled(global_identity):
        return None

    if (_identity_is_enabled(usecase_identity)
            and _identity_role(usecase_identity) is not None):
        return usecase_identity

    return global_identity


class RBACManager:
    """Role-Based Access Control Manager

    The **Portal_Identity registry** (USER_ROLES_TABLE, written only by
    the portal's own User Manager) is the source of truth for portal
    privilege — not the IdP. The `custom:role` claim a token carries is
    Claimed_Role: descriptive metadata, recorded in audit details, never
    granted (portal-jwt-role-privilege-escalation, design.md Decision 2).

    Enforcement of that rule is gated by PORTAL_REGISTRY_ENFORCED, which
    defaults to off until the registry has been backfilled from the pool;
    with it off, roles resolve exactly as they did before the fix (the
    IdP-as-truth order documented on `_resolve_role_legacy`) and every
    request enforcement would have denied is logged as `would_deny`.

    Role sources, in the order they are consulted under enforcement:
    - enabled global Portal_Identity row (the account's role)
    - enabled Use_Case Portal_Identity row (overrides it for that
      Use_Case; written by Team Management)
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
                # Workflow Manager: read-only view of workflows and deployment status
                Permission.WORKFLOW_READ,
                # Node Designer: read-only view of Custom_Node_Types and
                # Plugin_Records (13.3)
                Permission.NODE_DESIGNER_READ,
                # Workflow telemetry dashboards: read-only (§11.1)
                Permission.DASHBOARDS_READ,
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
                # Workflow Manager: Operators package and deploy workflows
                Permission.WORKFLOW_READ,
                Permission.WORKFLOW_PACKAGE,
                Permission.WORKFLOW_DEPLOY,
                # Node Designer: read-only view of Custom_Node_Types and
                # Plugin_Records (13.3)
                Permission.NODE_DESIGNER_READ,
                # Workflow telemetry dashboards: view and manage (§11.1)
                Permission.DASHBOARDS_READ,
                Permission.DASHBOARDS_MANAGE,
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
                # DDA Data Labeling: labeler APIs (harmless for
                # non-labelers; handlers filter by assignment ownership)
                Permission.LABELING_TASKS_SELF,
                Permission.UPLOAD_DATASETS,
                Permission.MANAGE_DATASETS,
                Permission.CREATE_TRAINING_JOBS,
                Permission.MANAGE_TRAINING_JOBS,
                Permission.PROMOTE_MODELS,
                Permission.DELETE_MODELS,
                # Workflow Manager: DataScientists create, edit, save, delete, and test workflows
                Permission.WORKFLOW_READ,
                Permission.WORKFLOW_CREATE,
                Permission.WORKFLOW_EDIT,
                Permission.WORKFLOW_SAVE,
                Permission.WORKFLOW_DELETE,
                Permission.WORKFLOW_TEST,
                # Node Designer: read-only view of Custom_Node_Types and
                # Plugin_Records (13.3)
                Permission.NODE_DESIGNER_READ,
                # Build Fleet: DataScientists are Build_Operators
                # (submit/cancel/read Build_Jobs, global scope;
                # portal-build-fleet-and-workflow-gates Req 1.6, 4.10)
                Permission.BUILDS_SUBMIT,
                Permission.BUILDS_CANCEL,
                Permission.BUILDS_READ,
                # Workflow telemetry dashboards: view and manage (§11.1)
                Permission.DASHBOARDS_READ,
                Permission.DASHBOARDS_MANAGE,
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
                # DDA Data Labeling: labeler APIs plus Labeling_Team
                # management (dda-data-labeling, Req 3.7)
                Permission.LABELING_TASKS_SELF,
                Permission.MANAGE_LABELING_TEAMS,
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
                # Workflow Manager: UseCaseAdmins hold all workflow permissions
                # (create/edit/save/delete/test like DataScientist, plus
                # package/deploy like Operator). bedrock-config:write remains
                # PortalAdmin-only.
                Permission.WORKFLOW_READ,
                Permission.WORKFLOW_CREATE,
                Permission.WORKFLOW_EDIT,
                Permission.WORKFLOW_SAVE,
                Permission.WORKFLOW_DELETE,
                Permission.WORKFLOW_TEST,
                Permission.WORKFLOW_PACKAGE,
                Permission.WORKFLOW_DEPLOY,
                # Node Designer: UseCaseAdmins create scaffolds, use the
                # Node_Generator, import plugins, run the Plugin_Simulator,
                # register Custom_Node_Types, promote/demote between dev and
                # test, and manage Plugin_Records within their own Use_Case
                # (13.1). node-designer:security-review remains
                # PortalAdmin-only (13.2).
                Permission.NODE_DESIGNER_READ,
                Permission.NODE_DESIGNER_CREATE,
                Permission.NODE_DESIGNER_GENERATE,
                Permission.NODE_DESIGNER_IMPORT,
                Permission.NODE_DESIGNER_SIMULATE,
                Permission.NODE_DESIGNER_REGISTER,
                Permission.NODE_DESIGNER_PROMOTE_DEMOTE,
                Permission.NODE_DESIGNER_MANAGE,
                # Build Fleet: UseCaseAdmins are Build_Operators
                # (submit/cancel/read Build_Jobs, global scope;
                # portal-build-fleet-and-workflow-gates Req 1.6, 4.10)
                Permission.BUILDS_SUBMIT,
                Permission.BUILDS_CANCEL,
                Permission.BUILDS_READ,
                # Workflow telemetry dashboards: view and manage dashboards,
                # and enable and tune telemetry for the use case (§11.1)
                Permission.DASHBOARDS_READ,
                Permission.DASHBOARDS_MANAGE,
                Permission.TELEMETRY_MANAGE,
                # Add UseCaseAdmin-specific permissions
                Permission.UPDATE_USECASES,
                Permission.DELETE_USECASES,
                Permission.MANAGE_USECASE_USERS,
                Permission.VIEW_AUDIT_LOGS,
            },
            
            Role.PORTAL_ADMIN: {
                # All permissions
                *[permission for permission in Permission],
            },
            
            Role.DATA_LABELER: {
                # DDA Data Labeling (dda-data-labeling, Req 2.3): the
                # labeler APIs ONLY — no other permission, so every
                # non-labeler endpoint 403s through the standard
                # @rbac_check path with an unauthorized_access audit
                # event (user, resource, timestamp).
                Permission.LABELING_TASKS_SELF,
            },
        }
    
    def get_user_role(self, user_id: str, usecase_id: str, user_info: Optional[Dict] = None) -> Optional[Role]:
        """Get the user's Effective_Role for a scope.

        Two modes, selected by PORTAL_REGISTRY_ENFORCED (default off,
        design.md Decision 4):

        **Enforcement on** — the Portal_Identity registry is the only
        source of privilege (design.md Expected Behavior):

        1. enabled global registry row -> that row's role
        2. enabled Use_Case row, when the scope is that Use_Case ->
           that row's role (overrides 1)
        3. no enabled row -> None (the caller is denied 403 with
           Identity_Source 'absent')
        4. the lookup raised -> `RegistryUnavailable` propagates, so the
           decorator answers 500 and audits a failure

        The `custom:role` claim in `user_info['role']` (Claimed_Role)
        grants nothing in this mode; it is recorded in audit details only
        (Requirements 1.1-1.6, design.md Decision 2).

        **Enforcement off (default)** — today's resolution order,
        unchanged, so nothing moves before the registry is backfilled;
        additionally a `would_deny` WARNING is logged for every request
        enforcement would have denied, which is how the backfill's
        completeness is measured against real traffic (Requirement 2.4).

        Args:
            user_id: Cognito `sub` of the caller
            usecase_id: scope of the decision ('global' or a Use_Case id)
            user_info: dict from get_user_from_event(); its 'role' is the
                Claimed_Role, no longer authoritative

        Returns:
            Role, or None when the caller holds no role (denied)

        Raises:
            RegistryUnavailable: enforcement mode only — the registry
                lookup failed (never downgraded to Viewer).
        """
        if registry_enforcement_enabled():
            return self._resolve_role_from_registry(user_id, usecase_id,
                                                    user_info)
        return self._resolve_role_legacy(user_id, usecase_id, user_info)

    def _resolve_role_from_registry(self, user_id: str, usecase_id: str,
                                    user_info: Optional[Dict] = None) -> Optional[Role]:
        """Resolve the Effective_Role from the Portal_Identity registry.

        `RegistryUnavailable` is deliberately NOT caught here: an
        availability failure must not be answered as a permission
        decision and must not fall back to Viewer (Requirement 1.5).
        """
        identity = _lookup_identity(user_id, usecase_id)

        if identity is None:
            logger.info(
                f"Registry denial: user {user_id} has no enabled "
                f"Portal_Identity for scope {usecase_id!r} "
                f"(identity_source={IDENTITY_SOURCE_ABSENT}, claimed_role="
                f"{(user_info or {}).get('role', 'unknown')!r})")
            return None

        role = _identity_role(identity)
        if role is None:
            # The deciding row exists but names no usable role: deny
            # rather than guess (corrupt data must not grant).
            logger.warning(
                f"Registry denial: Portal_Identity for user {user_id} / "
                f"scope {usecase_id!r} names no usable role "
                f"({identity.get('role')!r})")
            return None

        logger.info(
            f"User {user_id} resolved role {role.value} for scope "
            f"{usecase_id!r} from the Portal_Identity registry "
            f"(row usecase_id={identity.get('usecase_id')!r})")
        return role

    def _resolve_role_legacy(self, user_id: str, usecase_id: str,
                             user_info: Optional[Dict] = None) -> Optional[Role]:
        """Today's resolution order, unchanged (enforcement off).

        Kept verbatim so nothing moves before the backfill has run; the
        only addition is the `would_deny` dry-run WARNING.

        Role resolution order:
        1. PortalAdmin from JWT claims (global admin access)
        2. UseCase-specific role from DynamoDB (assigned via Team Management)
        3. Default role from JWT claims (Cognito custom:role attribute)
        4. Default to Viewer if nothing found
        """
        role = self._legacy_role(user_id, usecase_id, user_info)
        self._log_would_deny(user_id, usecase_id, user_info, role)
        return role

    def _legacy_role(self, user_id: str, usecase_id: str,
                     user_info: Optional[Dict] = None) -> Optional[Role]:
        """The pre-fix resolution order (see _resolve_role_legacy)."""
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

    def _log_would_deny(self, user_id: str, usecase_id: str,
                        user_info: Optional[Dict],
                        legacy_role: Optional[Role]) -> None:
        """Dry run of enforcement: WARN about requests the registry would
        deny (Requirement 2.4, design.md Decision 4).

        Read from the deployed logs before the flag is flipped: every
        `would_deny` line names a principal the backfill has not covered
        yet. Never raises and never changes the decision — including when
        the registry itself is unavailable (that is a `would_deny` we
        cannot determine, logged and dropped).

        Cost: one or two extra `get_item` calls per role resolution while
        the flag is off. That is deliberate and temporary — the dry run
        only has to survive until enforcement is enabled (design.md
        Decision 4 retires the flag afterwards).
        """
        try:
            if _lookup_identity(user_id, usecase_id) is not None:
                return
            logger.warning(
                f"would_deny: user {user_id} would be denied scope "
                f"{usecase_id!r} under PORTAL_REGISTRY_ENFORCED — no enabled "
                f"Portal_Identity registry row "
                f"(identity_source={IDENTITY_SOURCE_ABSENT}, claimed_role="
                f"{(user_info or {}).get('role', 'unknown')!r}, "
                f"claimed_username={(user_info or {}).get('username', 'unknown')!r}, "
                f"legacy_role={legacy_role.value if legacy_role else 'none'}) "
                f"— backfill this user before enabling enforcement")
        except RegistryUnavailable as error:
            logger.warning(
                f"would_deny undetermined for user {user_id} / scope "
                f"{usecase_id!r}: {error}")
        except Exception as error:  # noqa: BLE001 - dry run must never fail
            logger.warning(
                f"would_deny dry run failed for user {user_id} / scope "
                f"{usecase_id!r}: {error}")
    
    def get_user_permissions(self, user_id: str, usecase_id: str, user_info: Optional[Dict] = None) -> Set[Permission]:
        """Get all permissions for a user's Effective_Role.

        No role (an Unprovisioned_Principal under enforcement) means no
        permissions — the None from get_user_role propagates as an empty
        set, which the decorators answer 403 for. `RegistryUnavailable`
        is deliberately not caught: it escapes to the decorator, which
        answers 500 and audits a failure (Requirement 1.5).
        """
        role = self.get_user_role(user_id, usecase_id, user_info)
        if not role:
            return set()
        
        return self.role_permissions.get(role, set())
    
    def has_permission(self, user_id: str, usecase_id: str, permission: Permission, user_info: Optional[Dict] = None) -> bool:
        """Check whether the user's Effective_Role carries a permission.

        False for a caller with no role; `RegistryUnavailable` escapes to
        the decorator rather than being reported as a denial.
        """
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
        """Check whether the user's global Effective_Role is PortalAdmin.

        `RegistryUnavailable` escapes (the caller answers 500), so an
        unreadable registry is never reported as "not an admin".
        """
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


def is_cross_account_setup(usecase: Dict) -> bool:
    """
    Detect if a use case is configured for cross-account access.
    
    Single-account setup: cross_account_role_arn = 'arn:aws:iam::ACCOUNT_ID:root'
    Multi-account setup: cross_account_role_arn = 'arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME'
    
    Args:
        usecase: Use case details dict from DynamoDB
    
    Returns:
        True if multi-account setup, False if single-account
    """
    cross_account_role_arn = usecase.get('cross_account_role_arn')
    account_id = usecase.get('account_id')
    
    # Must have both fields and the ARN must contain ':role/' to be multi-account
    return (
        cross_account_role_arn and 
        account_id and 
        ':role/' in cross_account_role_arn
    )


def get_usecase_client(service_name: str, usecase: Dict, session_name: str = None, region: str = None,
                       config: Any = None) -> Any:
    """
    Get a boto3 client for accessing use case resources.
    
    Automatically handles both single-account and multi-account scenarios:
    - Single-account: Returns client using Lambda's own credentials
    - Multi-account: Returns client with assumed cross-account role credentials
    
    Args:
        service_name: AWS service name (e.g., 's3', 'sagemaker', 'iot')
        usecase: Use case details dict from DynamoDB
        session_name: Optional session name for role assumption (auto-generated if not provided)
        region: Optional AWS region
        config: Optional botocore.config.Config passed to boto3.client; when None
            the client is created exactly as before (no config argument)
    
    Returns:
        boto3 client for the specified service
    
    Raises:
        ClientError: If role assumption fails in multi-account setup
    """
    if not session_name:
        session_name = f"{service_name}-{int(datetime.utcnow().timestamp())}"
    
    # Check if this is a cross-account scenario
    if is_cross_account_setup(usecase):
        # Multi-account: assume role
        logger.info(f"Cross-account setup detected: assuming role {usecase.get('cross_account_role_arn')}")
        credentials = assume_usecase_role(
            usecase.get('cross_account_role_arn'),
            usecase.get('external_id'),
            session_name
        )
        if config is not None:
            return create_boto3_client(service_name, credentials, region, config=config)
        return create_boto3_client(service_name, credentials, region)
    else:
        # Single-account: use Lambda's own credentials
        logger.info(f"Single-account setup detected: using Lambda execution role for {service_name}")
        extra = {'config': config} if config is not None else {}
        if region:
            return boto3.client(service_name, region_name=region, **extra)
        else:
            return boto3.client(service_name, **extra)


def assume_usecase_role(role_arn: str, external_id: str, session_name: str,
                        session_policy: Optional[Dict] = None) -> Dict:
    """Assume cross-account role for UseCase Account access
    
    For single-account setups (where role_arn is the root ARN), returns current credentials
    instead of attempting to assume the root role (which is not possible).
    
    Args:
        role_arn: ARN of the role to assume
        external_id: External ID for role assumption (can be None if role doesn't require it)
        session_name: Name for the assumed role session
        session_policy: Optional IAM policy document narrowing the assumed
            session (workflow-telemetry-dashboards design §14.4). Sent to
            STS as ``Policy`` only when given; without it the call is
            byte-identical to before this keyword existed.
    
    Returns:
        Credentials dict with AccessKeyId, SecretAccessKey, SessionToken
    """
    try:
        # Check if this is a single-account setup (role_arn is root ARN)
        # Root ARN format: arn:aws:iam::ACCOUNT_ID:root
        if role_arn and role_arn.endswith(':root'):
            logger.info(f"Single-account setup detected (root ARN). Using Lambda execution role credentials")
            # For single-account, return a special marker that tells boto3 to use default credentials
            # We return empty credentials dict which signals to use the default chain
            return {
                'AccessKeyId': None,
                'SecretAccessKey': None,
                'SessionToken': None,
                'is_default_credentials': True
            }
        
        # Multi-account setup - assume the role
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
        
        # Only include the session policy when the caller narrows the session
        if session_policy is not None:
            assume_params['Policy'] = json.dumps(session_policy)
        
        response = sts_client.assume_role(**assume_params)
        return response['Credentials']
    except ClientError as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error assuming role {role_arn}: {str(e)}")
        raise


def assume_cross_account_role(role_arn: str, external_id: str, session_name: str = None) -> Dict:
    """
    Assume cross-account role - alias for assume_usecase_role for backward compatibility
    """
    if not session_name:
        session_name = f"portal-session-{int(datetime.utcnow().timestamp())}"
    return assume_usecase_role(role_arn, external_id, session_name)


def create_boto3_client(service_name: str, credentials: Dict, region: str = None, config: Any = None) -> Any:
    """
    Create a boto3 client with the given credentials.
    
    Handles the special case where credentials indicate default credentials should be used
    (for single-account setups where role_arn is root ARN).
    
    Args:
        service_name: AWS service name (e.g., 's3', 'sagemaker', 'iot')
        credentials: Credentials dict from assume_usecase_role
        region: AWS region (optional)
        config: Optional botocore.config.Config passed to boto3.client; when None
            the client is created exactly as before (no config argument)
    
    Returns:
        boto3 client for the specified service
    """
    extra = {'config': config} if config is not None else {}
    # Check if this is the special marker for default credentials
    if credentials.get('is_default_credentials'):
        # Use default credentials (Lambda execution role)
        if region:
            return boto3.client(service_name, region_name=region, **extra)
        else:
            return boto3.client(service_name, **extra)
    
    # Use the provided credentials
    client_params = {
        'aws_access_key_id': credentials['AccessKeyId'],
        'aws_secret_access_key': credentials['SecretAccessKey'],
    }
    
    if credentials.get('SessionToken'):
        client_params['aws_session_token'] = credentials['SessionToken']
    
    if region:
        client_params['region_name'] = region
    
    return boto3.client(service_name, **client_params, **extra)


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


# ---------------------------------------------------------------------------
# Use-case bucket configuration (portal-usecase-bucket-config, design §1)
#
# The S3 buckets a use case may use are an exact-name property of its record:
# `s3_bucket`, `data_s3_bucket` and the `additional_s3_buckets` list, minus
# the portal's own buckets (`portal_owned_buckets`). `allowed_s3_buckets`
# computes that set, `assert_bucket_allowed` enforces it and is the FIRST
# statement of `get_s3_client_for_bucket`, so no bucket-selected code path
# reaches STS or S3 for a bucket outside the set. Attaching a bucket to a use
# case goes through `check_bucket_attachable` (further below, after the
# PortalAdmin gate it depends on).
# ---------------------------------------------------------------------------

# Membership and URI parsing: permissive and exact-match safe. Buckets created
# before March 2018 in us-east-1 may carry uppercase letters, underscores and
# up to 255 characters, and a use case whose configured bucket has such a name
# must keep working. The class still excludes '@', ':', '/', whitespace and
# control characters, so a URI can never smuggle a second host or a port into
# the compared string (design §1.1).
MEMBERSHIP_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,255}$')
# Strict S3 naming rules, applied only where the portal is about to CREATE a
# bucket (POST /s3-buckets).
BUCKET_NAME_RE = re.compile(
    r'^(?!\d+\.\d+\.\d+\.\d+$)(?!.*\.\.)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
MAX_S3_URI_LENGTH = 2048
S3_URI_PREFIX = 's3://'

# Bucket tags the portal reads and writes (design §1.6, §5.5).
PORTAL_MANAGED_TAG = 'dda-portal:managed'
PORTAL_CREATED_BY_TAG = 'dda-portal:created-by'
PORTAL_USECASE_ID_TAG = 'dda-portal:usecase-id'
PORTAL_CREATED_BY_VALUE = 'portal'

# Remedy texts are constants so tests and the frontend never depend on prose
# (design §1.2). Every tagging command is the S3 Control form — in AWS CLI 2.x
# the bucket tag write for the ABAC model is `aws s3control tag-resource`.
REMEDY_NOT_ALLOWED = (
    "A UseCaseAdmin can allow this bucket for the use case under Additional "
    "S3 buckets, or place the object in the use case's configured bucket.")
REMEDY_PORTAL_BUCKET = (
    "The portal's own buckets (artifacts, components) cannot be a target for "
    "this operation and cannot be allowed for a use case.")
REMEDY_TAG = (
    "An AWS administrator must tag the bucket dda-portal:managed=true with the "
    "S3 Control TagResource API (console, or `aws s3control tag-resource "
    "--account-id <bucket owner account> --resource-arn arn:aws:s3:::<bucket> "
    "--tags Key=dda-portal:managed,Value=true`), then retry.")
REMEDY_TAGGING_UNREADABLE = (
    "The portal's role could not read this bucket's tags. Tag the bucket "
    "dda-portal:managed=true with the S3 Control TagResource API (`aws "
    "s3control tag-resource --account-id <bucket owner account> --resource-arn "
    "arn:aws:s3:::<bucket> --tags Key=dda-portal:managed,Value=true`) if it is "
    "not tagged (through the use case's role an untagged bucket also answers "
    "AccessDenied); if it is tagged, enable ABAC on it (`aws s3api "
    "put-bucket-abac --bucket <bucket> --abac-status Status=Enabled`) or grant "
    "s3:GetBucketTagging to the portal's role in the bucket policy; on a "
    "single-account portal deployed with a data-bucket allowlist, add the "
    "bucket to that list and redeploy; then retry.")
REMEDY_NOT_FOUND = (
    "The bucket does not exist or is not visible to the portal's role for "
    "this use case.")
REMEDY_CREDENTIALS = (
    "The portal could not assume the role configured for this bucket; check "
    "the use case's role ARN, external id and the role's trust policy.")
REMEDY_ACCOUNT = (
    "Buckets created in the portal account can only be allowed for "
    "single-account use cases.")
REMEDY_OTHER_USECASE = (
    "This bucket is configured on, or tagged for, another use case. A "
    "PortalAdmin can allow it for this use case too.")
REMEDY_STALE_CLAIM = (
    "This bucket is tagged for a use case that no longer exists. A PortalAdmin "
    "can allow it for this use case, or an AWS administrator removes the "
    "dda-portal:usecase-id tag (`aws s3control untag-resource --account-id "
    "<bucket owner account> --resource-arn arn:aws:s3:::<bucket> --tag-keys "
    "dda-portal:usecase-id`).")
REMEDY_TOO_MANY = (
    "A use case can have at most 100 additional buckets; remove one first.")

# Remedy per eligibility `reason` (design §1.6 table). The
# `bucket_configured_on_other_usecase` reason is absent on purpose: its remedy
# (REMEDY_OTHER_USECASE or REMEDY_STALE_CLAIM) is decided by
# `check_bucket_attachable` from the claim it found.
REMEDY_FOR_REASON = {
    'tag_missing': REMEDY_TAG,
    'no_tags': REMEDY_TAG,
    'tagging_unreadable': REMEDY_TAGGING_UNREADABLE,
    'bucket_not_found': REMEDY_NOT_FOUND,
    'credentials_unavailable': REMEDY_CREDENTIALS,
    'portal_bucket': REMEDY_PORTAL_BUCKET,
    'account_mismatch': REMEDY_ACCOUNT,
    'too_many_buckets': REMEDY_TOO_MANY,
}


class BucketConfigError(ValueError):
    """A bucket-configuration decision the caller receives as a 400.

    Subclasses `ValueError` so handlers whose `except ValueError` already maps
    to 400 keep working if a call site forgets the explicit catch; the
    explicit catch (`bucket_config_error_response`) produces the Requirement 5
    body shape.
    """
    error_code = 'bucket_config_error'

    def __init__(self, message: str, *, bucket: Any = None,
                 usecase_id: Optional[str] = None, purpose: Optional[str] = None,
                 reason: Optional[str] = None, remedy: Optional[str] = None):
        super().__init__(message)
        self.message = message
        # A non-string bucket (a body value that was null, a number, a list)
        # is reported by error_code alone, never echoed into the body.
        self.bucket = bucket if isinstance(bucket, str) else None
        self.usecase_id = usecase_id
        self.purpose = purpose
        self.reason = reason
        self.remedy = remedy

    def to_body(self) -> Dict[str, Any]:
        body = {
            'error': self.message,
            'error_code': self.error_code,
            'bucket': self.bucket,
            'usecase_id': self.usecase_id,
            'purpose': self.purpose,
            'reason': self.reason,
            'remedy': self.remedy,
        }
        return {key: value for key, value in body.items() if value is not None}


class InvalidS3Uri(BucketConfigError):
    error_code = 'invalid_s3_uri'


class InvalidBucketName(BucketConfigError):
    error_code = 'invalid_bucket_name'


class BucketNotAllowed(BucketConfigError):
    error_code = 'bucket_not_allowed'


class BucketNotEligible(BucketConfigError):
    error_code = 'bucket_not_eligible'


def bucket_config_error_response(exc: BucketConfigError, status: int = 400) -> Dict:
    """The API Gateway response for a refused bucket-configuration decision."""
    return create_response(status, exc.to_body())


def is_membership_name(name: Any) -> bool:
    """True when `name` is a `str` the membership grammar accepts.

    `fullmatch`, so the whole string is the name: a trailing newline (which
    `$` alone would tolerate) is a control character and fails.
    """
    return isinstance(name, str) and MEMBERSHIP_NAME_RE.fullmatch(name) is not None


def is_valid_bucket_name(name: Any) -> bool:
    """True when `name` is a `str` S3 would accept for a NEW bucket."""
    return isinstance(name, str) and BUCKET_NAME_RE.fullmatch(name) is not None


def parse_s3_uri(uri: Any, *, require_key: bool = True) -> Tuple[str, str]:
    """Split an `s3://bucket/key` URI into `(bucket, key)` exactly.

    Requires a `str` of at most MAX_S3_URI_LENGTH characters with the literal
    lowercase `s3://` prefix; the remainder is split at the FIRST '/', the
    bucket must match MEMBERSHIP_NAME_RE and the key must be non-empty when
    `require_key`. No urlparse (whose netloc for `s3://bucket@other/key` is
    `bucket@other`), no lowercasing, stripping or percent-decoding: the key is
    returned as given. Any failure raises InvalidS3Uri (design §1.1).
    """
    if not isinstance(uri, str):
        raise InvalidS3Uri("S3 URI must be a string")
    if len(uri) > MAX_S3_URI_LENGTH:
        raise InvalidS3Uri(f"S3 URI exceeds {MAX_S3_URI_LENGTH} characters")
    if not uri.startswith(S3_URI_PREFIX):
        raise InvalidS3Uri("S3 URI must start with s3://")
    remainder = uri[len(S3_URI_PREFIX):]
    bucket, separator, key = remainder.partition('/')
    if not is_membership_name(bucket):
        raise InvalidS3Uri("S3 URI does not name a valid bucket",
                           bucket=bucket if bucket else None)
    if require_key and not key:
        raise InvalidS3Uri("S3 URI must name an object key", bucket=bucket)
    return bucket, key


def portal_owned_buckets() -> FrozenSet[str]:
    """Buckets the portal owns for all use cases; never a member of any
    allowed set, never attachable (design §1.3).

    The artifacts bucket (PORTAL_ARTIFACTS_BUCKET), the Greengrass
    component bucket (COMPONENT_BUCKET, or the `{prefix}-{region}-{account}`
    derivation of shared_components.py when PORTAL_ACCOUNT_ID is set) and
    the workflow telemetry lake `edgecv-portal-telemetry-{account}-{region}`
    (workflow-telemetry-dashboards design §7.5, tasks.md decision Q6: derived
    from the same environment, so no new key is needed). Empty names are
    omitted. The component name comes from `component_bucket_name()`, called
    only when COMPONENT_BUCKET or PORTAL_ACCOUNT_ID is set, so with neither
    set no component bucket is contributed.
    """
    artifacts = os.environ.get('PORTAL_ARTIFACTS_BUCKET', '')
    component = (component_bucket_name()
                 if os.environ.get('COMPONENT_BUCKET') or os.environ.get('PORTAL_ACCOUNT_ID')
                 else '')
    telemetry_lake = (
        f"edgecv-portal-telemetry-{os.environ.get('PORTAL_ACCOUNT_ID', '')}-"
        f"{os.environ.get('AWS_REGION', 'us-east-1')}"
        if os.environ.get('PORTAL_ACCOUNT_ID') else '')
    return frozenset(name for name in (artifacts, component, telemetry_lake) if name)


def allowed_s3_buckets(usecase: Dict) -> FrozenSet[str]:
    """The exact bucket names a use case may use (design §1.3).

    Entries failing the membership grammar and the portal's own buckets are
    dropped with a WARNING rather than raising: a corrupt record must never
    widen access and must not take the use case's valid buckets offline.
    """
    candidates = [usecase.get('s3_bucket'), usecase.get('data_s3_bucket'),
                  *(usecase.get('additional_s3_buckets') or [])]
    owned = portal_owned_buckets()
    allowed = set()
    for name in candidates:
        if not name or (isinstance(name, str) and not name.strip()):
            continue
        if not is_membership_name(name):
            logger.warning("Ignoring configured bucket with invalid name on use case %s",
                           usecase.get('usecase_id'))
            continue
        if name in owned:
            logger.warning("Ignoring portal-owned bucket configured on use case %s",
                           usecase.get('usecase_id'))
            continue
        allowed.add(name)
    return frozenset(allowed)


def _audit_bucket_refused(usecase: Dict, bucket: str, purpose: str, reason: Optional[str],
                          actor: Optional[Dict], event: Optional[Dict]) -> None:
    """The one `s3_bucket_refused` audit row every refusal writes (design
    §1.4, §1.6). `log_audit_event` swallows its own failures, so audit loss
    never changes the decision."""
    usecase_id = usecase.get('usecase_id') or 'unknown'
    details = {'bucket': bucket, 'purpose': purpose}
    if reason is not None:
        details['reason'] = reason
    user_id = (actor or {}).get('user_id') or 'system'
    log_audit_event(
        user_id=user_id, action='s3_bucket_refused', resource_type='usecase',
        resource_id=usecase_id, result='denied', details=details,
        identity=attribution_from(event, actor, usecase_id) if event else None)


def assert_bucket_allowed(usecase: Dict, bucket: Any, purpose: str, *,
                          actor: Optional[Dict] = None, event: Optional[Dict] = None) -> None:
    """Refuse any bucket outside `allowed_s3_buckets(usecase)` (design §1.4).

    (1) `bucket` must be a `str` matching MEMBERSHIP_NAME_RE, else
    InvalidBucketName (no audit row: nothing was decided). (2) Membership;
    a refusal logs a WARNING (never a key), writes one `s3_bucket_refused`
    audit row and raises BucketNotAllowed — with REMEDY_PORTAL_BUCKET when the
    bucket is one of the portal's own, REMEDY_NOT_ALLOWED otherwise. There is
    no opt-in parameter of any kind.
    """
    if not is_membership_name(bucket):
        raise InvalidBucketName("Invalid S3 bucket name", bucket=bucket,
                                usecase_id=usecase.get('usecase_id'), purpose=purpose)
    if bucket in allowed_s3_buckets(usecase):
        return
    usecase_id = usecase.get('usecase_id')
    logger.warning("S3 bucket refused: usecase=%s bucket=%s purpose=%s",
                   usecase_id, bucket, purpose)
    _audit_bucket_refused(usecase, bucket, purpose, None, actor, event)
    portal_owned = bucket in portal_owned_buckets()
    raise BucketNotAllowed(
        "S3 bucket is not allowed for this use case",
        bucket=bucket, usecase_id=usecase_id, purpose=purpose,
        remedy=REMEDY_PORTAL_BUCKET if portal_owned else REMEDY_NOT_ALLOWED)


def _select_s3_role_for_bucket(usecase: Dict, bucket_name: str) -> Tuple[Optional[str], Optional[str]]:
    """The `(role_arn, external_id)` the portal assumes for a bucket.

    Data Account role iff the use case has a separate Data Account and the
    bucket is its `data_s3_bucket`; the use case's cross-account role
    (the execution role, for a root ARN) otherwise — so every
    `additional_s3_buckets` entry is reached with the cross-account role.
    Private: returns ARNs, not a client, so the eligibility probe can reuse
    the credential path without a way to obtain an unchecked client.
    """
    data_s3_bucket = usecase.get('data_s3_bucket')
    data_account_id = usecase.get('data_account_id')
    usecase_account_id = usecase.get('account_id')
    
    is_data_account_bucket = (
        data_account_id and
        data_account_id != usecase_account_id and
        bucket_name == data_s3_bucket
    )
    
    if is_data_account_bucket:
        role_arn = usecase.get('data_account_role_arn')
        external_id = usecase.get('data_account_external_id')
    else:
        role_arn = usecase.get('cross_account_role_arn')
        external_id = usecase.get('external_id')
    return role_arn, external_id


def get_s3_client_for_bucket(usecase: Dict, bucket_name: str, session_name: str = 'portal-s3-access',
                             session_policy: Optional[Dict] = None):
    """
    Get an S3 client with the correct credentials for accessing a bucket.
    
    Binds the bucket to the use case's configured buckets FIRST
    (`assert_bucket_allowed`, raising BucketConfigError before any STS or S3
    call), then determines whether the bucket is in the Data Account,
    UseCase Account, or Portal Account and assumes the appropriate role.
    
    Args:
        usecase: UseCase dict from DynamoDB
        bucket_name: S3 bucket name to access
        session_name: STS session name for role assumption (recorded as the
            `purpose` of a refusal)
        session_policy: Optional IAM policy document forwarded to
            `assume_usecase_role` (sent to STS only when given). Without it
            the `assume_usecase_role` call is the pre-keyword three-argument
            call, so every pre-existing caller and test double of that
            function keeps working unchanged.
    
    Returns:
        boto3 S3 client with appropriate credentials
    """
    assert_bucket_allowed(usecase, bucket_name, purpose=session_name)
    role_arn, external_id = _select_s3_role_for_bucket(usecase, bucket_name)
    if session_policy is None:
        credentials = assume_usecase_role(role_arn, external_id, session_name)
    else:
        credentials = assume_usecase_role(role_arn, external_id, session_name,
                                          session_policy=session_policy)
    return create_boto3_client('s3', credentials)


# ---------------------------------------------------------------------------
# PortalAdmin gate (portal-jwt-role-privilege-escalation, Requirement 1.5)
#
# `get_user_from_event(event)['role']` is the Claimed_Role — what the token
# asserted — and grants nothing (design.md Decision 2). Several handlers
# nonetheless gated PortalAdmin-only routes on `user.get('role') ==
# 'PortalAdmin'`, a second authorization path that never reached
# `RBACManager` and so stayed claim-driven after PORTAL_REGISTRY_ENFORCED
# was turned on: verified on account 164152369890 on 2026-09-22, where a
# pool account carrying only `custom:role=PortalAdmin` and no registry row
# was correctly denied `POST /builds` (the enforced `rbac_check` path) yet
# still read the whole user directory through `GET /admin/users`. The
# reverse also held: a registry PortalAdmin with no claim was denied.
#
# `caller_is_portal_admin` is the single helper those gates now share. It
# resolves the caller's global Effective_Role through `rbac_manager`, so
# enforcement, the legacy fallback while the flag is off, and
# `RegistryUnavailable` all behave exactly as they do for `rbac_check`.
# ---------------------------------------------------------------------------

def caller_is_portal_admin(user: Optional[Dict]) -> bool:
    """True when the caller's global Effective_Role is PortalAdmin.

    `user` is the `get_user_from_event(event)` dict; it is threaded through
    as `user_info` so the Claimed_Role still feeds the legacy resolution
    order while PORTAL_REGISTRY_ENFORCED is off, and is recorded as
    descriptive metadata once it is on.

    Raises:
        RegistryUnavailable: the Portal_Identity lookup failed. Deliberately
            NOT caught — the caller must answer 500, never 403, so a
            DynamoDB outage is never reported as a privilege decision
            (Requirement 1.5, design.md Decision 3).
    """
    user_id = (user or {}).get('user_id') or 'unknown'
    return rbac_manager.is_portal_admin(user_id, user_info=user)


# ---------------------------------------------------------------------------
# Use-case bucket configuration: eligibility probe and attach validator
# (portal-usecase-bucket-config, design §1.6). Placed after the PortalAdmin
# gate because the validator's sharing override is `caller_is_portal_admin`.
# ---------------------------------------------------------------------------

ELIGIBILITY_RETRY_ATTEMPTS = 3
ELIGIBILITY_RETRY_DELAY_SECONDS = 1
ELIGIBILITY_SESSION_NAME = 'portal-bucket-eligibility'


@dataclass
class EligibilityResult:
    """What `probe_bucket_eligibility` read, plus what `check_bucket_attachable`
    decided on top of it.

    `shared_with_usecases` and `stale_claim` are filled by the validator only
    when the PortalAdmin override admitted a bucket that its configuration
    Scan or its tag-claim step would otherwise have refused, so every record
    writer emits one and the same `s3_bucket_allowed` audit row without
    recomputing anything.
    """
    eligible: bool
    reason: str
    tags: Dict[str, str]
    portal_created: bool
    claimed_by: Optional[str]
    shared_with_usecases: int = 0
    stale_claim: bool = False


def _tags_from_tagging_response(response: Dict) -> Dict[str, str]:
    return {tag['Key']: tag.get('Value') for tag in response.get('TagSet', [])
            if isinstance(tag, dict) and 'Key' in tag}


def _tags_qualify(tags: Dict[str, str]) -> bool:
    return (tags.get(PORTAL_MANAGED_TAG) == 'true'
            or tags.get(PORTAL_CREATED_BY_TAG) == PORTAL_CREATED_BY_VALUE)


def _client_error_code_and_status(error: ClientError) -> Tuple[str, str]:
    response = getattr(error, 'response', None) or {}
    code = str((response.get('Error') or {}).get('Code') or '')
    status = str((response.get('ResponseMetadata') or {}).get('HTTPStatusCode') or '')
    return code, status


def _eligibility_result(reason: str, tags: Dict[str, str]) -> EligibilityResult:
    return EligibilityResult(
        eligible=(reason == 'eligible'), reason=reason, tags=dict(tags),
        portal_created=(tags.get(PORTAL_CREATED_BY_TAG) == PORTAL_CREATED_BY_VALUE),
        claimed_by=tags.get(PORTAL_USECASE_ID_TAG) or None)


def read_bucket_tags(s3_client: Any, bucket: str, *, retry: bool = False) -> Tuple[Dict[str, str], str]:
    """One `get_bucket_tagging` read classified as `eligible` (a qualifying
    portal tag is present), `tag_missing` (tags, none qualifying) or
    `no_tags` (NoSuchTagSet); returns `(tags, reason)`.

    With `retry=True` the two non-qualifying outcomes are re-read up to
    ELIGIBILITY_RETRY_ATTEMPTS times, ELIGIBILITY_RETRY_DELAY_SECONDS apart
    (bucket tagging reads are not read-after-write consistent); with the
    default the first read concludes and nothing sleeps. Any other
    ClientError propagates unchanged so the caller classifies it. The one
    implementation behind the eligibility probe and `create_s3_bucket`'s
    read-after-write of the tags it sent (design §1.6, §5.5 step 5).
    """
    attempts = ELIGIBILITY_RETRY_ATTEMPTS if retry else 1
    attempt = 0
    while True:
        attempt += 1
        try:
            tags = _tags_from_tagging_response(s3_client.get_bucket_tagging(Bucket=bucket))
            reason = 'eligible' if _tags_qualify(tags) else 'tag_missing'
        except ClientError as error:
            code, _ = _client_error_code_and_status(error)
            if code != 'NoSuchTagSet':
                raise
            tags, reason = {}, 'no_tags'
        if reason == 'eligible' or attempt >= attempts:
            return tags, reason
        time.sleep(ELIGIBILITY_RETRY_DELAY_SECONDS)


def probe_bucket_eligibility(usecase_like: Dict, bucket: str, *, retry: bool = False) -> EligibilityResult:
    """Read a bucket's tags with the credentials the portal would later use
    for it and classify the outcome (design §1.6 table). Never raises.

    `usecase_like` is the use case record or, at creation, the item about to
    be written with its role fields resolved. Outcomes, all fail-closed:
    `eligible` (dda-portal:managed=true or dda-portal:created-by=portal),
    `tag_missing`, `no_tags`, `tagging_unreadable` (AccessDenied / 403, or a
    PermanentRedirect the one region retry could not resolve),
    `bucket_not_found` (NoSuchBucket / 404) and `credentials_unavailable`
    (the STS call or anything else failed).

    `retry=True` re-reads `no_tags` / `tag_missing` up to
    ELIGIBILITY_RETRY_ATTEMPTS times, ELIGIBILITY_RETRY_DELAY_SECONDS apart
    (tag read-after-write); with the default the first read concludes and the
    probe never sleeps. A `PermanentRedirect` / 301 is followed ONCE with a
    client bound to the `x-amz-bucket-region` response header (same
    credentials, no second STS call); no header, a second redirect or any
    other read failure on that client concludes `tagging_unreadable`.
    """
    tags: Dict[str, str] = {}
    reason = 'credentials_unavailable'
    try:
        role_arn, external_id = _select_s3_role_for_bucket(usecase_like, bucket)
        credentials = assume_usecase_role(role_arn, external_id, ELIGIBILITY_SESSION_NAME)
        s3_client = create_boto3_client('s3', credentials)
        redirected = False
        while True:
            try:
                tags, reason = read_bucket_tags(s3_client, bucket, retry=retry)
                break
            except ClientError as error:
                code, status = _client_error_code_and_status(error)
                tags = {}
                if code in ('PermanentRedirect', '301') or status == '301':
                    headers = ((error.response or {}).get('ResponseMetadata') or {}).get('HTTPHeaders') or {}
                    region = headers.get('x-amz-bucket-region')
                    if redirected or not region:
                        reason = 'tagging_unreadable'
                        break
                    logger.info("Bucket %s tagging read redirected to region %s", bucket, region)
                    redirected = True
                    s3_client = create_boto3_client('s3', credentials, region=region)
                    continue
                if redirected or code in ('AccessDenied', '403') or status == '403':
                    reason = 'tagging_unreadable'
                elif code in ('NoSuchBucket', '404') or status == '404':
                    reason = 'bucket_not_found'
                else:
                    reason = 'credentials_unavailable'
                break
    except Exception as error:  # noqa: BLE001 - the probe never raises; it fails closed
        logger.info("Bucket %s eligibility probe could not read tags: %s", bucket, error)
        tags = {}
        reason = 'credentials_unavailable'
    logger.info("Bucket %s eligibility: %s", bucket, reason)
    return _eligibility_result(reason, tags)


def _usecases_configuring_bucket(bucket: str) -> Set[str]:
    """usecase_ids whose record names `bucket` as `s3_bucket`, `data_s3_bucket`
    or an `additional_s3_buckets` entry (paginated Scan). A Scan error is an
    outage, not a decision: it propagates."""
    table = dynamodb.Table(USECASES_TABLE)
    scan_kwargs = {
        'ProjectionExpression': 'usecase_id',
        'FilterExpression': 's3_bucket = :b OR data_s3_bucket = :b OR contains(additional_s3_buckets, :b)',
        'ExpressionAttributeValues': {':b': bucket},
    }
    found: Set[str] = set()
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get('Items', []):
            if item.get('usecase_id'):
                found.add(item['usecase_id'])
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return found
        scan_kwargs['ExclusiveStartKey'] = last_key


def _usecase_exists(usecase_id: str) -> bool:
    """One projected get_item; a DynamoDB error propagates (same rule as the Scan)."""
    table = dynamodb.Table(USECASES_TABLE)
    response = table.get_item(Key={'usecase_id': usecase_id}, ProjectionExpression='usecase_id')
    return 'Item' in response


def check_bucket_attachable(*, target_usecase_id: Optional[str], usecase_like: Dict, bucket: Any,
                            user: Dict, event: Optional[Dict], purpose: str,
                            evidence: Optional[EligibilityResult] = None,
                            retry: bool = False) -> EligibilityResult:
    """The single attach validator every use case record writer calls before
    it writes a bucket into `s3_bucket`, `data_s3_bucket` or
    `additional_s3_buckets` (design §1.6). Order:

    0. portal-owned bucket → BucketNotEligible(`portal_bucket`), before any
       Scan or probe, whatever its tags say — PortalAdmin included;
    1. `bucket` must be a `str` matching MEMBERSHIP_NAME_RE, else
       InvalidBucketName (a non-string body value is a 400, never a 500);
    2. Unclaimed (configuration): a bucket configured on another use case is
       attachable only by a PortalAdmin (`bucket_configured_on_other_usecase`,
       REMEDY_OTHER_USECASE);
    3. eligibility: `evidence` when the caller read the tags itself in the
       same request, else `probe_bucket_eligibility(..., retry=retry)`;
    4. Unclaimed (tag claim): a `dda-portal:usecase-id` naming another use
       case is a claim whether or not that use case still exists; one
       get_item picks REMEDY_OTHER_USECASE (exists) or REMEDY_STALE_CLAIM
       (deleted); a PortalAdmin proceeds;
    5. the returned result carries `shared_with_usecases` and `stale_claim`
       for the writer's `s3_bucket_allowed` audit row.

    Every refusal writes `s3_bucket_refused` (details bucket, purpose, reason)
    before raising. `caller_is_portal_admin` may raise RegistryUnavailable and
    a Scan / get_item error propagates: an outage is a 500, never a decision.
    """
    usecase_id = target_usecase_id or usecase_like.get('usecase_id') or 'unknown'
    audit_usecase = {'usecase_id': usecase_id}

    def _refuse(reason: str, remedy: str) -> None:
        logger.warning("S3 bucket not attachable: usecase=%s bucket=%s purpose=%s reason=%s",
                       usecase_id, bucket, purpose, reason)
        _audit_bucket_refused(audit_usecase, bucket, purpose, reason, user, event)
        raise BucketNotEligible(
            "S3 bucket is not eligible for this use case", bucket=bucket,
            usecase_id=usecase_id, purpose=purpose, reason=reason, remedy=remedy)

    # 0. The portal's own buckets are not attachable by anyone.
    if isinstance(bucket, str) and bucket in portal_owned_buckets():
        _refuse('portal_bucket', REMEDY_PORTAL_BUCKET)

    # 1. Grammar.
    if not is_membership_name(bucket):
        logger.info("S3 bucket name rejected: usecase=%s purpose=%s", usecase_id, purpose)
        raise InvalidBucketName("Invalid S3 bucket name", bucket=bucket,
                                usecase_id=usecase_id, purpose=purpose)

    # 2. Unclaimed (configuration).
    others = _usecases_configuring_bucket(bucket)
    others.discard(target_usecase_id)
    is_portal_admin: Optional[bool] = None
    if others:
        is_portal_admin = caller_is_portal_admin(user)
        if not is_portal_admin:
            _refuse('bucket_configured_on_other_usecase', REMEDY_OTHER_USECASE)

    # 3. Eligibility.
    result = evidence if evidence is not None else probe_bucket_eligibility(
        usecase_like, bucket, retry=retry)
    if not result.eligible:
        _refuse(result.reason, REMEDY_FOR_REASON.get(result.reason))

    # 4. Unclaimed (tag claim).
    claimed_elsewhere = bool(result.claimed_by) and result.claimed_by != target_usecase_id
    stale_claim = False
    if claimed_elsewhere:
        stale_claim = not _usecase_exists(result.claimed_by)
        if is_portal_admin is None:
            is_portal_admin = caller_is_portal_admin(user)
        if not is_portal_admin:
            _refuse('bucket_configured_on_other_usecase',
                    REMEDY_STALE_CLAIM if stale_claim else REMEDY_OTHER_USECASE)

    # 5. What the PortalAdmin override admitted, for the writer's audit row.
    result.shared_with_usecases = len(others) + (
        1 if claimed_elsewhere and result.claimed_by not in others else 0)
    result.stale_claim = claimed_elsewhere and stale_claim
    return result


def audit_bucket_allowed(user: Dict, usecase_id: str, bucket: str, attribute: str,
                         source: str, result: EligibilityResult,
                         event: Optional[Dict]) -> None:
    """The one `s3_bucket_allowed` audit row every record writer emits AFTER
    its write succeeded (design §1.6): `attribute` names the record field the
    bucket entered (`s3_bucket` | `data_s3_bucket` | `additional_s3_buckets`),
    `source` the writer (`api` | `create` | `configure` | `auto_add`), and the
    validator's `shared_with_usecases` / `stale_claim` ride along only when
    the PortalAdmin override admitted a shared or claimed bucket.
    `log_audit_event` swallows its own failures.
    """
    details: Dict[str, Any] = {'bucket': bucket, 'attribute': attribute, 'source': source}
    if result.shared_with_usecases > 0:
        details['shared_with_usecases'] = result.shared_with_usecases
    if result.stale_claim:
        details['stale_claim'] = True
    log_audit_event(user['user_id'], 's3_bucket_allowed', 'usecase', usecase_id,
                    'success', details, event=event)


# ---------------------------------------------------------------------------
# Portal authorization and IAM scoping (portal-authorization-and-iam-scoping,
# design I1, I5).
#
# The account grammar and the trusted-account check the use-case writers run
# before any STS call; the one Component_Bucket_Name derivation; the bucket
# policy size bound; the tag-namespace check every portal tag list passes;
# the Data_Plane_Scoping accessor; and the Attach_Step (`attach_bucket`) the
# four bucket record writers call where they called `check_bucket_attachable`.
# ---------------------------------------------------------------------------

def data_plane_scoping() -> str:
    """The Data_Plane_Scoping the CDK deployed ('tag', 'tag-objects-only' or
    'allowlist'), read from the DATA_PLANE_SCOPING variable the compute stack
    sets on every handler. The one accessor every Python reader of the
    scoping uses; the default exists so unit tests and the host lane run
    without the variable."""
    return os.environ.get('DATA_PLANE_SCOPING', 'tag')


# Account grammar (Requirement 3.1). re.ASCII so `\d` and `\w` are the ASCII
# classes IAM uses; the validators call fullmatch, so a trailing newline
# (which `$` alone tolerates) fails.
ACCOUNT_ID_RE = re.compile(r'^\d{12}$', re.ASCII)
ROLE_ARN_RE = re.compile(r'^arn:aws:iam::(\d{12}):role/[\w+=,.@/-]+$', re.ASCII)


def validate_account_id(value: Any) -> bool:
    """True when `value` is a `str` of exactly twelve ASCII digits."""
    return isinstance(value, str) and ACCOUNT_ID_RE.fullmatch(value) is not None


def validate_role_arn(value: Any, account_id: Any, role_name: Optional[str] = None) -> bool:
    """True when `value` is an IAM role ARN in `account_id`.

    With `role_name` the ARN must be exactly
    `arn:aws:iam::<account_id>:role/<role_name>` (no path; the names the
    account templates create, e.g. DDAPortalAccessRole). Without it the
    generic ROLE_ARN_RE applies and its account segment must equal
    `account_id`. Pure; never raises.
    """
    if not isinstance(value, str) or not validate_account_id(account_id):
        return False
    match = ROLE_ARN_RE.fullmatch(value)
    if match is None or match.group(1) != account_id:
        return False
    if role_name is not None:
        return value == f"arn:aws:iam::{account_id}:role/{role_name}"
    return True


TRUSTED_USECASE_ACCOUNT_IDS_ENV = 'TRUSTED_USECASE_ACCOUNT_IDS'


class TrustedAccountsNotConfigured(Exception):
    """TRUSTED_USECASE_ACCOUNT_IDS is unset or empty. Callers answer 500 with
    the exception's message; it is a deployment fault, never a refusal of the
    caller's account."""

    def __init__(self, message: str = 'Trusted account list is not configured'):
        super().__init__(message)


def account_trusted(account_id: Any) -> bool:
    """True when `account_id` is in the portal's Trusted_Account_List, the
    comma-separated TRUSTED_USECASE_ACCOUNT_IDS the compute stack exports
    (entries trimmed, empty entries ignored) — the same list that bounds the
    portal's own sts:AssumeRole grant.

    Raises:
        TrustedAccountsNotConfigured: the variable is unset or holds no id.
    """
    raw = os.environ.get(TRUSTED_USECASE_ACCOUNT_IDS_ENV) or ''
    trusted = {entry.strip() for entry in raw.split(',') if entry.strip()}
    if not trusted:
        raise TrustedAccountsNotConfigured()
    return isinstance(account_id, str) and account_id in trusted


def component_bucket_name() -> str:
    """The Greengrass component bucket's name: COMPONENT_BUCKET when set, else
    the `{prefix}-{region}-{account}` derivation of shared_components.py, every
    read defaulted. Always a string; the one definition the UseCases and
    SharedComponents handlers and `portal_owned_buckets()` share
    (Requirement 3.5)."""
    return os.environ.get('COMPONENT_BUCKET') or (
        f"{os.environ.get('COMPONENT_BUCKET_PREFIX', 'dda-component')}-"
        f"{os.environ.get('AWS_REGION', 'us-east-1')}-{os.environ.get('PORTAL_ACCOUNT_ID', '')}")


# Upper bound for a serialized component bucket policy (Requirement 3.3):
# deliberately under S3's 20,480-byte bucket policy limit, so a writer refuses
# before S3 would reject the PutBucketPolicy.
COMPONENT_BUCKET_POLICY_MAX_BYTES = 20_000

PORTAL_TAG_NAMESPACE = 'dda-portal:'


def assert_portal_tags(tags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return `tags` unchanged when every entry's `Key` is in the `dda-portal:`
    namespace; raise ValueError otherwise, before any S3 call (Requirement
    7.7). Pure."""
    for tag in tags:
        key = tag.get('Key') if isinstance(tag, dict) else None
        if not isinstance(key, str) or not key.startswith(PORTAL_TAG_NAMESPACE):
            raise ValueError(f"Tag key outside the {PORTAL_TAG_NAMESPACE} namespace: {key!r}")
    return tags


# ---------------------------------------------------------------------------
# The Attach_Step (design I5 "The Attach_Step — one wrapper, four writers").
#
# `attach_bucket` runs the sibling's attach validator unchanged; once the
# validator has passed, it enables ABAC on a tagged Portal_Account_Bucket and
# probes whether the role the selector picks reaches the bucket, and it never
# raises: every failure after the validator degrades to a report field.
# Every client comes from the module-global `create_boto3_client`.
# ---------------------------------------------------------------------------

# Distinct from ELIGIBILITY_SESSION_NAME, so the Attach_Step's AssumeRole is
# never counted as an eligibility probe.
ATTACH_SESSION_NAME = 'portal-bucket-attach'
# The one bounded wait: a just-enabled bucket whose first probe is denied is
# probed once more after this many seconds (time.sleep on this module's
# `time`), because the condition's evaluation after PutBucketAbac is not
# documented as synchronous.
ATTACH_PROBE_RETRY_DELAY_SECONDS = 1
_ATTACH_REASON_MAX_CHARS = 200
_UNKNOWN_BUCKET_ACCOUNT = '<bucket owner account>'

# Registration_Report remedies (Requirement 7.11), formatted with the bucket
# (and the account for the tag command). Prefixed REGISTRATION_ so they stay
# apart from the REMEDY_* refusal remedies of the attach validator above.
REGISTRATION_REMEDY_TAG = (
    "Tag the bucket dda-portal:managed=true: `aws s3control tag-resource "
    "--account-id {account} --resource-arn arn:aws:s3:::{bucket} "
    "--tags Key=dda-portal:managed,Value=true`")
REGISTRATION_REMEDY_ENABLE_ABAC = (
    "Enable ABAC on the bucket: `aws s3api put-bucket-abac --bucket {bucket} "
    "--abac-status Status=Enabled`")
REGISTRATION_REMEDY_CROSS_ACCOUNT = (
    "Ask the bucket's account administrator to enable ABAC on it (`aws s3api "
    "put-bucket-abac --bucket {bucket} --abac-status Status=Enabled`); the "
    "portal's cross-account role evaluates its tag condition only then.")
REGISTRATION_REMEDY_REDEPLOY = (
    "Object access works now; redeploy the portal to browse this bucket.")


class RegionUnknown(Exception):
    """HeadBucket answered without an `x-amz-bucket-region` header, so the
    bucket's region for the two ABAC calls is unknown."""


@dataclass
class AttachOutcome:
    """What `attach_bucket` decided and observed after the validator passed."""
    eligibility: EligibilityResult       # the validator's result, unchanged
    tagged: bool                         # carries dda-portal:managed=true (not `eligibility.eligible`)
    abac_enabled: Union[bool, str]       # True | False | 'unknown' (not a Portal_Account_Bucket)
    iam_reachable: bool
    remedy: Optional[str]
    abac_action: str                     # 'already_enabled' | 'enabled' | 'enable_failed' | 'tag_missing' | 'not_applicable'

    def report(self) -> Dict[str, Any]:
        """The Registration_Report entry for this bucket (Requirement 7.11)."""
        return {'tagged': self.tagged, 'abac_enabled': self.abac_enabled,
                'iam_reachable': self.iam_reachable, 'remedy': self.remedy}


def _failure_reason(error: BaseException) -> str:
    return f'{type(error).__name__}: {error}'[:_ATTACH_REASON_MAX_CHARS]


def _arn_account(arn: Any) -> Optional[str]:
    """The account segment of an ARN, or None."""
    if not isinstance(arn, str):
        return None
    parts = arn.split(':')
    return parts[4] if len(parts) > 5 and parts[4] else None


def _is_portal_account_bucket(usecase_like: Dict, bucket: str) -> bool:
    """True when the sibling's selector would reach `bucket` with the
    execution role: its role ARN is the account root, the exact test
    `assume_usecase_role` makes before returning the execution role's own
    credentials. A `None` ARN is not a Portal_Account_Bucket."""
    role_arn, _ = _select_s3_role_for_bucket(usecase_like, bucket)
    return isinstance(role_arn, str) and role_arn.endswith(':root')


def _execution_role_client(region: Optional[str] = None) -> Any:
    """An S3 client on the calling handler's execution role, through the
    sibling's default-credentials marker."""
    return create_boto3_client('s3', {'is_default_credentials': True}, region=region)


def _selector_client(usecase_like: Dict, bucket: str) -> Any:
    """An S3 client on the role the sibling's selector picks for `bucket`."""
    role_arn, external_id = _select_s3_role_for_bucket(usecase_like, bucket)
    return create_boto3_client('s3', assume_usecase_role(role_arn, external_id, ATTACH_SESSION_NAME))


def _s3_client_for_bucket_region(bucket: str, cache: Dict[Optional[str], Any]) -> Any:
    """An execution-role S3 client bound to `bucket`'s region.

    One HeadBucket on the default-region execution-role client; the region is
    the `x-amz-bucket-region` header of the 200 response, or of the error
    response's HTTPHeaders (301, 403). There is no GetBucketLocation fallback:
    that action is conditioned under 'tag' and would be denied on the tagged
    ABAC-off bucket this resolver exists for. `cache` maps a region (None for
    the default) to its client, so one client is built per region per call.

    Raises:
        RegionUnknown: the header is absent.
    """
    probe_client = cache.get(None)
    if probe_client is None:
        probe_client = cache[None] = _execution_role_client()
    try:
        response = probe_client.head_bucket(Bucket=bucket)
        headers = (response.get('ResponseMetadata') or {}).get('HTTPHeaders') or {}
        answered = '200'
    except ClientError as error:
        headers = ((error.response or {}).get('ResponseMetadata') or {}).get('HTTPHeaders') or {}
        answered = _client_error_code_and_status(error)[1] or 'an error'
    region = headers.get('x-amz-bucket-region')
    if not region:
        raise RegionUnknown(f"HeadBucket answered {answered} without x-amz-bucket-region")
    if region not in cache:
        cache[region] = _execution_role_client(region)
    return cache[region]


def _abac_status(s3_client: Any, bucket: str) -> str:
    """'Enabled' iff GetBucketAbac reports `AbacStatus.Status == 'Enabled'`,
    else 'Disabled' (an empty AbacStatus included). A bucket that never had
    PutBucketAbac answers 200 with `Status: Disabled` (read-only check in the
    implementation run), so no error code is mapped: every error reaches the
    caller's guard."""
    response = s3_client.get_bucket_abac(Bucket=bucket)
    status = (response.get('AbacStatus') or {}).get('Status')
    return 'Enabled' if status == 'Enabled' else 'Disabled'


def registration_remedy(tagged: bool, abac_enabled: Union[bool, str], iam_reachable: bool,
                        account_kind: str, scoping: str, *, bucket: str,
                        account_id: Optional[str] = None) -> Optional[str]:
    """The Registration_Report remedy (Requirement 7.11): None iff
    `iam_reachable`, else the earliest unmet step as a copy-ready command.

    Not tagged (eligible or not) → tag it; tagged Portal_Account_Bucket
    (`account_kind` 'portal') with ABAC not enabled → enable ABAC; any other
    bucket (`'cross_account'`) → the bucket's account administrator enables
    ABAC; a portal bucket under 'tag-objects-only' → object access works now,
    redeploy to browse; every step met yet unreachable → enable ABAC. The
    account in the tag command is `account_id`, else the portal account
    (PORTAL_ACCOUNT_ID) for a portal bucket — the deployment's own
    configuration — else a placeholder. Pure.
    """
    if iam_reachable:
        return None
    if not tagged:
        account = account_id or (
            os.environ.get('PORTAL_ACCOUNT_ID') if account_kind == 'portal' else None)
        return REGISTRATION_REMEDY_TAG.format(account=account or _UNKNOWN_BUCKET_ACCOUNT,
                                              bucket=bucket)
    if account_kind == 'portal' and abac_enabled is not True:
        return REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=bucket)
    if account_kind != 'portal':
        return REGISTRATION_REMEDY_CROSS_ACCOUNT.format(bucket=bucket)
    if scoping == 'tag-objects-only':
        return REGISTRATION_REMEDY_REDEPLOY
    return REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=bucket)


def _bucket_reachable(s3_client: Any, bucket: str) -> bool:
    """HeadBucket plus a one-key ListObjectsV2; False on any exception."""
    if s3_client is None:
        return False
    try:
        s3_client.head_bucket(Bucket=bucket)
        s3_client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        return True
    except Exception as error:
        logger.info("Bucket %s not reachable by the probe: %s", bucket, _failure_reason(error))
        return False


def _probe(bucket: str, *, s3_client: Any, tagged: bool, abac_enabled: Union[bool, str],
           account_kind: str, scoping: str, just_enabled: bool,
           account_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """`(iam_reachable, remedy)` for `bucket` on `s3_client`. Never raises.

    When the caller has just enabled ABAC and the first probe fails, it waits
    ATTACH_PROBE_RETRY_DELAY_SECONDS once (time.sleep on this module's `time`)
    and probes again, so the whole probe stays under two seconds of waiting.
    `s3_client=None` (the client could not be built) is unreachable without a
    call. `account_id` is the bucket's account for the tag remedy.
    """
    reachable = _bucket_reachable(s3_client, bucket)
    if not reachable and just_enabled and s3_client is not None:
        time.sleep(ATTACH_PROBE_RETRY_DELAY_SECONDS)
        reachable = _bucket_reachable(s3_client, bucket)
    return reachable, registration_remedy(tagged, abac_enabled, reachable, account_kind, scoping,
                                          bucket=bucket, account_id=account_id)


def attach_bucket(*, target_usecase_id: Optional[str], usecase_like: Dict, bucket: Any,
                  user: Dict, event: Optional[Dict], purpose: str,
                  evidence: Optional[EligibilityResult] = None, retry: bool = False,
                  source: str) -> AttachOutcome:
    """The Attach_Step: `check_bucket_attachable` with every keyword forwarded
    (a refusal propagates untouched and nothing else runs), then the ABAC
    registration and the reachability probe (Requirements 7.6, 7.11).

    A tagged (dda-portal:managed=true) Portal_Account_Bucket gets ABAC enabled
    on the calling handler's execution role when it is not already, inside one
    guard around region resolution, GetBucketAbac and PutBucketAbac; every
    enable attempt writes one `bucket_abac_enabled` audit row. An untagged one
    gets no ABAC call (`tag_missing`). Any other bucket is never enabled
    (`abac_enabled: 'unknown'`, `not_applicable`) and is probed on the role
    the selector picks. After the validator has passed nothing here raises.
    """
    result = check_bucket_attachable(
        target_usecase_id=target_usecase_id, usecase_like=usecase_like, bucket=bucket,
        user=user, event=event, purpose=purpose, evidence=evidence, retry=retry)
    tagged = result.tags.get(PORTAL_MANAGED_TAG) == 'true'
    scoping = data_plane_scoping()

    if not _is_portal_account_bucket(usecase_like, bucket):
        role_arn, _ = _select_s3_role_for_bucket(usecase_like, bucket)
        try:
            probe_client = _selector_client(usecase_like, bucket)
        except Exception as error:
            logger.warning("Attach probe for %s has no client for the selected role: %s",
                           bucket, _failure_reason(error))
            probe_client = None
        reachable, remedy = _probe(bucket, s3_client=probe_client, tagged=tagged,
                                   abac_enabled='unknown', account_kind='cross_account',
                                   scoping=scoping, just_enabled=False,
                                   account_id=_arn_account(role_arn))
        return AttachOutcome(result, tagged, 'unknown', reachable, remedy, 'not_applicable')

    action, abac_enabled, reason = 'tag_missing', False, None
    if tagged:
        try:
            s3_client = _s3_client_for_bucket_region(bucket, {})
            if _abac_status(s3_client, bucket) == 'Enabled':
                action, abac_enabled = 'already_enabled', True
            else:
                s3_client.put_bucket_abac(Bucket=bucket, AbacStatus={'Status': 'Enabled'})
                action, abac_enabled = 'enabled', True
        except Exception as error:
            action, abac_enabled, reason = 'enable_failed', False, _failure_reason(error)
            logger.warning("ABAC enable failed for %s: %s", bucket, reason)
        if action != 'already_enabled':
            details: Dict[str, Any] = {'bucket': bucket, 'source': source}
            if reason:
                details['reason'] = reason
            log_audit_event((user or {}).get('user_id') or UNKNOWN_ATTRIBUTION,
                            'bucket_abac_enabled', 'usecase',
                            target_usecase_id or usecase_like.get('usecase_id') or 'unknown',
                            'success' if action == 'enabled' else 'failure', details, event=event)

    try:
        probe_client = _execution_role_client()
    except Exception as error:
        logger.warning("Attach probe for %s has no execution-role client: %s",
                       bucket, _failure_reason(error))
        probe_client = None
    reachable, remedy = _probe(bucket, s3_client=probe_client, tagged=tagged,
                               abac_enabled=abac_enabled, account_kind='portal', scoping=scoping,
                               just_enabled=(action == 'enabled'))
    return AttachOutcome(result, tagged, abac_enabled, reachable, remedy, action)
