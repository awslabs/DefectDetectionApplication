"""
Build Infrastructure Configuration API Lambda function (portal build fleet)

Read and update the build infrastructure configuration (design §7),
following the portal handler conventions (error envelope
{error: {code, message, details}}, get_user_from_event, log_audit_event,
RBAC via rbac_middleware).

Every configuration decision is delegated to the pure module
build_domain.py (effective_build_config for read-time defaults,
validate_build_config / apply_config_update for update validation and
the atomic reject); this handler does I/O and wiring only.

Routes (API Gateway REST):
    GET /build-config   Effective configuration: the stored values
                        merged over the documented defaults (ARM64
                        instance type m6g.4xlarge, x86_64 instance type
                        m6i.4xlarge, volume size 100 GB, region
                        us-east-1, max runtime 4 hours) applied per
                        field on read (Req 9.1, 9.2). Permission:
                        builds:read.
    PUT /build-config   Partial configuration update validated by
                        build_domain.validate_build_config; a rejected
                        update is discarded in full and the prior
                        configuration values are retained (atomic
                        reject, Req 9.5). Every applied change records
                        an Audit_Log entry with the changed parameter,
                        the prior value, the new value, the acting
                        user, and the time of the change (Req 9.4).
                        PortalAdmin only (Req 9.6); non-PortalAdmin
                        requests are rejected with an authorization
                        error and a denied-access Audit_Log entry by
                        the RBAC decorator.

The configuration is stored in the PortalSettings table under the key
`build_infrastructure_config` (design §7). Build_Jobs snapshot the
effective configuration at creation (config_snapshot); the dispatcher
and agent only ever read the snapshot, so a change here applies only to
Build_Jobs created and Dedicated_Build_Server launches initiated after
the change (Req 9.3, enforced in build_jobs.py / build_planner.py).

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
Requirements: 9.1, 9.2, 9.4, 9.5, 9.6
"""
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Import shared utilities (Lambda layer)
import sys
sys.path.append('/opt/python')
from shared_utils import (
    create_response, get_user_from_event, log_audit_event
)
from rbac_middleware import require_builds_read, super_user_only

# Pure decision module (no AWS clients): read-time defaults, update
# validation, and the atomic-reject application come from build_domain.
import build_domain

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables (build-fleet-stack.ts lambdaEnvironment)
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# ---------------------------------------------------------------- constants

#: PortalSettings item key holding the build infrastructure configuration
#: (design §7).
BUILD_CONFIG_SETTING_KEY = 'build_infrastructure_config'

#: The configurable parameters (design §7 table). Fields outside this set
#: are ignored on update so arbitrary junk never reaches the stored
#: configuration; build_domain.DEFAULT_BUILD_CONFIG is the authoritative
#: parameter list and default table (Req 9.2).
KNOWN_PARAMETERS = tuple(build_domain.DEFAULT_BUILD_CONFIG)

#: PortalSettings item attributes that are storage metadata, not
#: configuration values (only relevant for the flat item shape).
ITEM_METADATA_KEYS = ('setting_key', 'updated_by', 'updated_at')


# ------------------------------------------------------------ pure helpers

def error_response(status_code: int, code: str, message: str,
                   details: Optional[Dict] = None) -> Dict:
    """Build the portal error envelope: {error: {code, message, details}}"""
    return create_response(status_code, {
        'error': {
            'code': code,
            'message': message,
            'details': details or {},
        }
    })


def now_ms() -> int:
    """Current epoch milliseconds"""
    return int(time.time() * 1000)


def parse_body(event: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Parse the request body; returns (body, None) or (None, error_response)"""
    try:
        body = json.loads(event.get('body') or '{}')
    except (json.JSONDecodeError, TypeError):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be valid JSON')
    if not isinstance(body, dict):
        return None, error_response(400, 'INVALID_BODY',
                                    'Request body must be a JSON object')
    return body, None


def to_native(value: Any) -> Any:
    """Convert DynamoDB Decimals to native ints/floats (deep)"""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_native(v) for v in value]
    return value


def to_ddb(value: Any) -> Any:
    """Convert native floats to Decimals for DynamoDB storage (deep)"""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    return value


# ------------------------------------------------------------- persistence

def settings_table():
    """PortalSettings DynamoDB table accessor"""
    return dynamodb.Table(SETTINGS_TABLE)


def read_stored_config() -> Dict[str, Any]:
    """The raw stored build_infrastructure_config object from
    PortalSettings ({} when never written). Accepts both the nested
    ({setting_key, value: {...}}) and the flat item shape, converting
    Decimals to native numbers."""
    response = settings_table().get_item(
        Key={'setting_key': BUILD_CONFIG_SETTING_KEY})
    item = response.get('Item')
    if not item:
        return {}
    if isinstance(item.get('value'), dict):
        stored = item['value']
    else:
        stored = {k: v for k, v in item.items()
                  if k not in ITEM_METADATA_KEYS}
    return to_native(stored)


def write_stored_config(stored: Dict[str, Any], user_id: str,
                        updated_at: int) -> None:
    """Persist the stored configuration under the PortalSettings key
    `build_infrastructure_config` in the nested item shape used by the
    portal's other settings (design §7)."""
    settings_table().put_item(Item={
        'setting_key': BUILD_CONFIG_SETTING_KEY,
        'value': to_ddb(stored),
        'updated_by': user_id,
        'updated_at': updated_at,
    })


# -------------------------------------------------------- GET /build-config

@require_builds_read()
def get_build_config(event: Dict, context: Any) -> Dict:
    """GET /build-config — the effective build infrastructure
    configuration: stored values merged over the documented per-field
    defaults applied on read (Req 9.1, 9.2). Permission: builds:read."""
    stored = read_stored_config()
    effective = build_domain.effective_build_config(stored)
    return create_response(200, {'config': effective})


# -------------------------------------------------------- PUT /build-config

def audit_config_changes(user_id: str, changed_at: int,
                         prior_effective: Dict[str, Any],
                         new_effective: Dict[str, Any],
                         update: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Record one Audit_Log entry per applied configuration change: the
    changed parameter, the prior value, the new value, the acting user,
    and the time of the change (Req 9.4). A supplied parameter whose
    effective value did not change is not a change and is not audited.
    Returns the list of changes."""
    changes: List[Dict[str, Any]] = []
    for parameter in update:
        prior_value = prior_effective.get(parameter)
        new_value = new_effective.get(parameter)
        if prior_value == new_value:
            continue
        change = {
            'parameter': parameter,
            'prior_value': prior_value,
            'new_value': new_value,
            'changed_at': changed_at,
        }
        changes.append(change)
        log_audit_event(
            user_id=user_id,
            action='build_config_changed',
            resource_type='build_config',
            resource_id=BUILD_CONFIG_SETTING_KEY,
            result='success',
            details=change,
        )
    return changes


@super_user_only
def update_build_config(event: Dict, context: Any) -> Dict:
    """PUT /build-config — apply a partial configuration update.

    The update is validated by build_domain.validate_build_config
    (instance-family → architecture lookup table, positive volume size,
    positive max runtime); a rejected update is discarded in full and
    every prior configuration value is retained (atomic reject,
    Req 9.5). Every applied change records an Audit_Log entry with the
    parameter, prior value, new value, user, and time (Req 9.4).
    PortalAdmin only (Req 9.6, enforced by the decorator with a
    denied-access Audit_Log entry on denial)."""
    body, err = parse_body(event)
    if err:
        return err
    user = get_user_from_event(event)

    # Accept the partial configuration either as the body itself or
    # under a 'config' wrapper; only known parameters are considered.
    candidate = body.get('config') if isinstance(body.get('config'), dict) \
        else body
    update = {k: candidate[k] for k in KNOWN_PARAMETERS if k in candidate}
    if not update:
        return error_response(
            400, 'CONFIG_UPDATE_EMPTY',
            'The update supplies no configuration parameter. Supported '
            'parameters: ' + ', '.join(sorted(KNOWN_PARAMETERS)) + '.',
            {'supported_parameters': sorted(KNOWN_PARAMETERS)})

    stored = read_stored_config()

    # Atomic validate-and-apply (pure): a rejected update returns the
    # stored configuration unchanged (Req 9.5).
    new_stored, result = build_domain.apply_config_update(stored, update)
    if not result.valid:
        return error_response(
            400, 'CONFIG_INVALID',
            'The configuration update is invalid and was rejected in '
            'full; the prior configuration values are retained. '
            + ' '.join(e['message'] for e in result.errors),
            {'errors': [dict(e) for e in result.errors]})

    prior_effective = build_domain.effective_build_config(stored)
    new_effective = build_domain.effective_build_config(new_stored)

    changed_at = now_ms()
    try:
        write_stored_config(new_stored, user['user_id'], changed_at)
    except ClientError as e:
        logger.error(f"Build configuration write failed: {e}", exc_info=True)
        return error_response(
            502, 'CONFIG_WRITE_FAILED',
            'Storing the configuration update failed; the prior '
            'configuration values are retained.')

    changes = audit_config_changes(
        user['user_id'], changed_at, prior_effective, new_effective, update)

    return create_response(200, {
        'config': new_effective,
        'changes': changes,
    })


# ------------------------------------------------------------------ routing

def handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler - routes to the appropriate operation"""
    try:
        http_method = event.get('httpMethod')

        # Handle CORS preflight requests
        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS',
                    'Access-Control-Max-Age': '86400'
                },
                'body': ''
            }

        resource = event.get('resource', '')

        if resource == '/build-config':
            if http_method == 'GET':
                return get_build_config(event, context)
            if http_method == 'PUT':
                return update_build_config(event, context)

        return error_response(404, 'NOT_FOUND', 'Not found')

    except Exception as e:
        logger.error(f"Handler error: {str(e)}", exc_info=True)
        return error_response(500, 'INTERNAL_ERROR', 'Internal server error')
