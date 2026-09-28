"""
Camera_Registry API Lambda (camera-registry-sync).

Serves the per-device Camera_Registry over the routes registered by
CameraRegistryApiStack (all under /devices/{id}/cameras):

Read routes (task 6.1):
    GET    /devices/{id}/cameras            Registry entries + META (Viewer).
        Per-entry ``stale`` is computed against the Staleness_Threshold
        (Req 4.1), absent entries carry ``absent_since`` (Req 4.4), and the
        response attaches the IoT connectivity status from the existing
        device-status lookup (Req 4.2). Devices that never completed a
        synchronization return ``{"state": "never-synced"}`` rather than a
        bare empty list (Req 1.6).
    GET    /devices/{id}/cameras/conflicts  Conflict events, newest first
        (Viewer, Req 6.3).

Mutation, conflict re-apply, and refresh routes (task 6.2):
    POST   /devices/{id}/cameras            Create origin ``portal-created``
        (Operator, Reqs 5.1, 5.7). The shadow ``desired.changes`` entry is
        written FIRST; only then is the registry entry marked ``pending``
        with a fresh ``portal_change_id`` — a shadow-client failure returns
        502 with the registry untouched.
    PUT    /devices/{id}/cameras/{csid}     Update (Operator). Rejects
        origin ``edge-discovered`` with ``DISCOVERY_MANAGED`` (Req 5.6).
    DELETE /devices/{id}/cameras/{csid}     Pending delete (Operator);
        same discovery-managed rejection.
    POST   /devices/{id}/cameras/conflicts/{cid}/reapply  Re-issue the
        overridden portal version as a new pending change (Operator,
        Req 6.4).
    POST   /devices/{id}/cameras/refresh    On-demand GetThingShadow pull
        through get_usecase_client, run through the same reducer as the
        SQS ingest path (Viewer).

All mutating routes log an audit event carrying the acting user, the
device, the camera source, and the timestamp (Reqs 12.2, 12.3).

Authorization follows the existing use-case permission pattern
(rbac_manager checks against the device's usecase_id — Reqs 1.5, 12.1):
the Use_Case is resolved from the device's own registry items when they
exist, so a caller cannot re-scope another tenant's device by query
parameter; the query parameter is only the fallback for devices the
registry has never seen. Out-of-scope requests get the standard 403 with
an ``unauthorized_access`` audit event.

Storage (design "Data Models"): DynamoDB table ``dda-portal-camera-registry``
with PK ``device_id`` and item-type-prefixed SK — ``CAMERA#{csid}``,
``META``, ``CONFLICT#{ts}#{uuid}`` — written by the Portal_Sync_Service
(camera_sync.py).
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from shared_utils import (
    create_response, get_user_from_event, log_audit_event,
    get_usecase, get_usecase_client, get_usecase_region,
    assume_cross_account_role, create_boto3_client,
    rbac_manager, Permission,
)

# The refresh route runs the exact same reduction as the SQS ingest path
# (camera_sync.py is bundled into the same Lambda code asset).
import camera_sync

# Pin_Request lifecycle core (cloud-static-camera-provisioning), bundled
# into the same Lambda code asset like camera_sync.py.
import pin_requests

# Video_Validation core (static-camera-video-loop): a byte-identical copy of
# the device's src/backend/utils/video_loop.py. Stdlib-only at import, so
# importing it here costs nothing on functions without the video layer;
# only the Portal_Video_Pin_API's child-process probe loads OpenCV.
import video_loop

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')

CAMERA_REGISTRY_TABLE = os.environ.get('CAMERA_REGISTRY_TABLE')
SETTINGS_TABLE = os.environ.get('SETTINGS_TABLE')

# Item-type SK prefixes (design: dda-portal-camera-registry layout).
SK_META = 'META'
SK_CAMERA_PREFIX = 'CAMERA#'
SK_CONFLICT_PREFIX = 'CONFLICT#'

# Staleness_Threshold: settings-table entry, PortalAdmin-editable through
# the existing settings API (task 6.5); read here with the default-24
# fallback (Reqs 4.1, 4.3).
STALENESS_SETTING_KEY = 'camera_registry.staleness_threshold_hours'
DEFAULT_STALENESS_THRESHOLD_HOURS = 24

# Viewer-held permission gating the read (and refresh) routes (Reqs 1.3,
# 12.1); Operator-held permission gating the mutation routes (Reqs 5.7,
# 12.2) — the same permission the existing device-mutation routes use.
VIEW_PERMISSION = Permission.VIEW_DEVICES
MUTATE_PERMISSION = Permission.MANAGE_DEVICES

# Sync_Channel shadow written by the mutation routes and pulled by the
# refresh route (design: named shadow per thing).
SHADOW_NAME = 'dda-camera-registry'

# Machine-readable rejection code for mutations of discovery-managed
# sources (Req 5.6).
DISCOVERY_MANAGED = 'DISCOVERY_MANAGED'

ORIGIN_EDGE_DISCOVERED = 'edge-discovered'
ORIGIN_PORTAL_CREATED = 'portal-created'

# ---------------------------------------------------------------------------
# Portal_Pin_API constants (cloud-static-camera-provisioning task 2.1)
# ---------------------------------------------------------------------------

# Image_Transport: the Portal component bucket (the usecases.py /
# shared_components.py convention — every onboarded device account's TES
# role can already read it).
COMPONENT_BUCKET = os.environ.get('COMPONENT_BUCKET')

# S3 layout (design "Data Models"): presigned-PUT staging keys under
# static-image-pins/staging/ (1-day lifecycle expiry, task 4.1) and
# canonical per-request keys written only by CopyObject after validation.
STATIC_IMAGE_PIN_PREFIX = 'static-image-pins'
STATIC_IMAGE_STAGING_PREFIX = f'{STATIC_IMAGE_PIN_PREFIX}/staging/'
UPLOAD_URL_TTL_SECONDS = 15 * 60

# Mirror the device-side StaticImageStore constants
# (src/backend/utils/static_image_camera.py) so portal validation accepts
# exactly what the device pin operation accepts (Reqs 1.1, 1.3, 1.4).
# MAX_PIN_IMAGE_BYTES is read at call time so tests can inject a small
# boundary-straddling limit.
MAX_PIN_IMAGE_BYTES = 50 * 1024 * 1024
SUPPORTED_PIN_FORMATS = ('JPEG', 'PNG', 'BMP')

# Every field a desired.staticImagePin document may carry. The shadow
# write always sends the full field set with absent fields as explicit
# nulls, so the IoT shadow merge replaces the single slot wholesale — a
# remove document clears the previous pin's reference fields instead of
# merging with them (Decision 1: newest request structurally replaces
# the previous one).
DESIRED_PIN_FIELDS = ('requestId', 'op', 'requestedAtEpochMs', 'bucket',
                      'key', 'sha256', 'sizeBytes', 'format', 'fileName')

# ---------------------------------------------------------------------------
# Portal_Video_Pin_API constants (static-camera-video-loop)
# ---------------------------------------------------------------------------

# The Static_Video_Camera's own Sync_Channel slot (design Decision 7). It
# carries the same field set as the image slot (DESIRED_PIN_FIELDS).
DESIRED_VIDEO_PIN_SECTION = 'staticVideoPin'

# Canonical video keys live under the existing prefix (design Decision 9):
# static-image-pins/{deviceId}/video/{pinRequestId}; staging is shared with
# images (same upload-url issuance, lifecycle rule, and CORS).
STATIC_VIDEO_PIN_SUBPREFIX = 'video'

# The device's video pin limit (video_loop.MAX_PIN_VIDEO_BYTES, 100 MB),
# read at call time so tests can inject a small boundary-straddling limit.
MAX_PIN_VIDEO_BYTES = video_loop.MAX_PIN_VIDEO_BYTES

# Video_Validation runs in a child process so a slow or stuck native decode
# can be abandoned (Requirement 8.4), inside the asynchronous validation job
# (the API Gateway integration timeout of 29 s cannot hold a 60 s decode).
# The function's 120 s timeout leaves room for the download before it and
# the copy and shadow write after it.
VIDEO_VALIDATION_TIMEOUT_SECONDS = 60
VIDEO_VALIDATION_TIMEOUT_MESSAGE = (
    f'The video took too long to validate (over '
    f'{VIDEO_VALIDATION_TIMEOUT_SECONDS} seconds); use a lower resolution '
    f'or more frequent keyframes.')

# The function the pin route hands the validation job to (asynchronous
# Event invocation): the CameraVideoPinHandler itself, by its fixed name.
VIDEO_VALIDATION_FUNCTION = os.environ.get('VIDEO_VALIDATION_FUNCTION')

# Event key marking a validation-job invocation of camera_video_pin.handler.
VIDEO_VALIDATION_EVENT_KEY = 'videoValidation'

STAGED_VIDEO_NOT_FOUND_MESSAGE = (
    'Staged upload not found; request a new upload URL and upload the '
    'video again')

# Validated_Metadata fields recorded on a Video_Pin_Request item.
VALIDATED_VIDEO_METADATA_FIELDS = ('codec', 'width', 'height', 'fps',
                                   'frameCount', 'durationMs')

_VIDEO_DOWNLOAD_CHUNK_BYTES = 1 << 20


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


# ---------------------------------------------------------------------------
# Registry reads
# ---------------------------------------------------------------------------

def query_device_items(device_id: str) -> List[Dict[str, Any]]:
    """All registry items of one device (single-partition read)."""
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        'KeyConditionExpression': Key('device_id').eq(device_id),
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
        kwargs['ExclusiveStartKey'] = last_key
    return items


def device_usecase_id(items: List[Dict[str, Any]]) -> Optional[str]:
    """The device's Use_Case as recorded on its own registry items.

    META is authoritative; any other item's scoping attribute serves when
    META has not been written yet (e.g. only portal-created entries).
    """
    meta = next((item for item in items if item.get('sk') == SK_META), None)
    if meta and meta.get('usecase_id'):
        return meta['usecase_id']
    for item in items:
        if item.get('usecase_id'):
            return item['usecase_id']
    return None


def staleness_threshold_hours() -> float:
    """The configured Staleness_Threshold, defaulting to 24 hours.

    The settings entry is PortalAdmin-editable through the existing
    settings API (data_accounts.py, reserved id
    'camera-registry-configuration'); when unset (or on any read failure)
    the default keeps the route functional (Req 4.1).
    """
    if not SETTINGS_TABLE:
        return DEFAULT_STALENESS_THRESHOLD_HOURS
    try:
        response = dynamodb.Table(SETTINGS_TABLE).get_item(
            Key={'setting_key': STALENESS_SETTING_KEY}
        )
        value = (response.get('Item') or {}).get('value')
        if value is not None:
            hours = float(value)
            if hours > 0:
                return hours
    except (ClientError, TypeError, ValueError) as e:
        logger.warning(f"Could not read staleness threshold setting: {e}")
    return DEFAULT_STALENESS_THRESHOLD_HOURS


# ---------------------------------------------------------------------------
# Authorization (Reqs 1.5, 12.1)
# ---------------------------------------------------------------------------

def authorize(user: Dict, event: Dict, device_id: str,
              usecase_id: Optional[str],
              permission: Permission) -> Optional[Dict]:
    """Use-case permission check for a registry route.

    Returns an error response, or None when authorized. Denials log the
    standard ``unauthorized_access`` audit event (Req 1.5).
    """
    if not usecase_id:
        return create_response(400, {'error': 'usecase_id parameter required'})
    if rbac_manager.has_permission(user['user_id'], usecase_id, permission,
                                   user_info=user):
        return None
    log_audit_event(
        user['user_id'], 'unauthorized_access', 'camera_registry', device_id,
        'denied',
        {
            'required_permission': permission.value,
            'usecase_id': usecase_id,
            'method': event.get('httpMethod'),
            'path': event.get('path'),
        }
    )
    return create_response(403, {
        'error': 'Access denied',
        'required_permission': permission.value,
    })


# ---------------------------------------------------------------------------
# Device connectivity (Req 4.2) — the existing device-status lookup
# (devices.py pattern: assumed use-case role + Greengrass core-device status)
# ---------------------------------------------------------------------------

def device_connectivity_status(usecase_id: str, device_id: str) -> str:
    """The device's reported status, 'UNKNOWN' when the lookup fails.

    The camera inventory must stay readable when the status lookup is
    unavailable (offline use-case account, missing role), so every failure
    degrades to 'UNKNOWN' instead of failing the request.
    """
    try:
        usecase = get_usecase(usecase_id)
        credentials = assume_cross_account_role(
            usecase['cross_account_role_arn'], usecase['external_id']
        )
        region = usecase.get('region', os.environ.get('AWS_REGION', 'us-east-1'))
        greengrass_client = create_boto3_client('greengrassv2', credentials, region)
        response = greengrass_client.get_core_device(coreDeviceThingName=device_id)
        return response.get('status', 'UNKNOWN')
    except Exception as e:  # noqa: BLE001 — availability over precision here
        logger.warning(f"Device status lookup failed for {device_id}: {e}")
        return 'UNKNOWN'


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def camera_view(item: Dict[str, Any], now: int,
                threshold_ms: float) -> Dict[str, Any]:
    """One registry camera item in API shape, with computed ``stale``."""
    sk = item.get('sk') or ''
    last_reported_at = item.get('last_reported_at')
    # Older than the Staleness_Threshold — strictly (Req 4.1); entries the
    # device has never reported (portal-created, still pending) carry no
    # last-reported timestamp and staleness does not apply to them.
    stale = (last_reported_at is not None
             and (now - int(last_reported_at)) > threshold_ms)
    view = {
        'camera_source_id': item.get('camera_source_id')
                            or sk[len(SK_CAMERA_PREFIX):],
        'name': item.get('name'),
        'type': item.get('type'),
        'params': item.get('params') or {},
        'capabilities': item.get('capabilities') or {},
        'origin': item.get('origin'),
        'version': item.get('version'),
        'last_reported_at': last_reported_at,
        'sync_status': item.get('sync_status'),
        'absent': bool(item.get('absent', False)),
        'stale': stale,
    }
    if item.get('failure_reason') is not None:
        view['failure_reason'] = item['failure_reason']
    if view['absent'] and item.get('absent_since') is not None:
        view['absent_since'] = item['absent_since']
    return view


def conflict_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """One conflict-event item in API shape (Req 6.3)."""
    sk = item.get('sk') or ''
    view = {
        # SK is CONFLICT#{ts}#{uuid}; the uuid segment is the event's
        # URL-safe identifier (the {cid} of the re-apply route).
        'conflict_id': sk.rsplit('#', 1)[-1],
        'camera_source_id': item.get('camera_source_id'),
        'edge_version': item.get('edge_version'),
        'portal_version': item.get('portal_version'),
        'resolution': item.get('resolution'),
        'created_at': item.get('created_at'),
    }
    if item.get('reapplied_as') is not None:
        view['reapplied_as'] = item['reapplied_as']
    return view


# ---------------------------------------------------------------------------
# Read routes (task 6.1)
# ---------------------------------------------------------------------------

def get_cameras(device_id: str, user: Dict, event: Dict,
                query_params: Dict) -> Dict:
    """GET /devices/{id}/cameras (Viewer — Reqs 1.3, 1.6, 4.1, 4.2, 4.4)."""
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, VIEW_PERMISSION)
    if error:
        return error

    meta = next((item for item in items if item.get('sk') == SK_META), None)
    never_synced = meta is None or bool(meta.get('never_synced', True))

    threshold_hours = staleness_threshold_hours()
    threshold_ms = threshold_hours * 3600 * 1000
    now = now_ms()
    cameras = [
        camera_view(item, now, threshold_ms)
        for item in items
        if (item.get('sk') or '').startswith(SK_CAMERA_PREFIX)
    ]
    cameras.sort(key=lambda c: (c.get('name') or '', c['camera_source_id']))

    return create_response(200, {
        'device_id': device_id,
        'usecase_id': usecase_id,
        # Never-completed synchronization is an explicit state, never a
        # bare empty list (Req 1.6). Portal-created pending entries (if
        # any) are still listed so operators see what they queued.
        'state': 'never-synced' if never_synced else 'synced',
        'last_report_at': (meta or {}).get('last_report_at'),
        'staleness_threshold_hours': threshold_hours,
        # IoT connectivity from the existing device-status lookup (Req 4.2)
        'device_status': device_connectivity_status(usecase_id, device_id),
        'cameras': cameras,
        'count': len(cameras),
    })


def get_conflicts(device_id: str, user: Dict, event: Dict,
                  query_params: Dict) -> Dict:
    """GET /devices/{id}/cameras/conflicts (Viewer — Req 6.3), newest first."""
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, VIEW_PERMISSION)
    if error:
        return error

    conflicts = [
        conflict_view(item)
        for item in items
        if (item.get('sk') or '').startswith(SK_CONFLICT_PREFIX)
    ]
    conflicts.sort(
        key=lambda c: (int(c['created_at']) if c.get('created_at') is not None else 0,
                       c['conflict_id']),
        reverse=True,
    )

    return create_response(200, {
        'device_id': device_id,
        'usecase_id': usecase_id,
        'conflicts': conflicts,
        'count': len(conflicts),
    })


# ---------------------------------------------------------------------------
# Sync_Channel shadow access (task 6.2)
# ---------------------------------------------------------------------------

def iot_data_client(usecase_id: str):
    """Assumed-role (or single-account) iot-data client for the Use_Case."""
    usecase = get_usecase(usecase_id)
    return get_usecase_client('iot-data', usecase,
                              region=get_usecase_region(usecase))


def write_desired_change(usecase_id: str, device_id: str, csid: str,
                         change: Dict[str, Any]) -> Optional[Dict]:
    """Write one desired.changes entry to the device's registry shadow.

    Returns an error response on failure, None on success. Callers write
    the shadow FIRST and touch the registry only afterwards, so a shadow
    client failure leaves the registry state untouched (task 6.2 / design
    portal→edge flow).
    """
    try:
        client = iot_data_client(usecase_id)
        client.update_thing_shadow(
            thingName=device_id,
            shadowName=SHADOW_NAME,
            payload=json.dumps(
                {'state': {'desired': {'changes': {csid: change}}}},
                default=lambda o: float(o) if isinstance(o, Decimal) else o,
            ),
        )
        return None
    except Exception as e:  # noqa: BLE001 — any shadow-path failure is a 502
        logger.error(f"Shadow desired write failed for {device_id}/{csid}: {e}")
        return create_response(502, {
            'error': 'Failed to deliver the change to the device sync channel',
        })


# ---------------------------------------------------------------------------
# Mutation routes (task 6.2 — Reqs 5.1, 5.6, 5.7, 12.2, 12.3)
# ---------------------------------------------------------------------------

def new_change_id() -> str:
    return f"pc-{uuid.uuid4()}"


def find_camera_item(items: List[Dict[str, Any]],
                     csid: str) -> Optional[Dict[str, Any]]:
    sk = f"{SK_CAMERA_PREFIX}{csid}"
    return next((item for item in items if item.get('sk') == sk), None)


def discovery_managed_rejection(csid: str) -> Dict:
    """Reject mutations of origin edge-discovered sources (Req 5.6)."""
    return create_response(409, {
        'error': f"Camera source '{csid}' is discovery-managed and cannot "
                 "be modified from the Portal",
        'code': DISCOVERY_MANAGED,
        'camera_source_id': csid,
    })


def validate_camera_body(body: Any) -> Optional[Dict]:
    """Minimal shape validation for create/update bodies."""
    if not isinstance(body, dict):
        return create_response(400, {'error': 'JSON object body required'})
    if not body.get('name') or not isinstance(body.get('name'), str):
        return create_response(400, {'error': 'name is required'})
    if not body.get('type') or not isinstance(body.get('type'), str):
        return create_response(400, {'error': 'type is required'})
    if 'params' in body and not isinstance(body['params'], dict):
        return create_response(400, {'error': 'params must be an object'})
    return None


def audit_mutation(user: Dict, action: str, device_id: str, csid: str,
                   usecase_id: str, portal_change_id: str,
                   extra: Optional[Dict] = None) -> None:
    """Audit event for a mutating route (Reqs 12.2, 12.3).

    log_audit_event stamps the acting user, timestamp, and result; the
    details carry the affected device and camera source.
    """
    details = {
        'device_id': device_id,
        'camera_source_id': csid,
        'usecase_id': usecase_id,
        'portal_change_id': portal_change_id,
    }
    if extra:
        details.update(extra)
    log_audit_event(user['user_id'], action, 'camera_registry', device_id,
                    'success', details)


def mark_pending(device_id: str, usecase_id: str, csid: str,
                 portal_change_id: str, pending_content: Dict[str, Any],
                 existing: Optional[Dict[str, Any]],
                 body: Optional[Dict[str, Any]] = None) -> None:
    """Upsert the registry entry into sync_status=pending (Req 5.1).

    Existing entries keep their last-reported edge state as the effective
    content (the portal version travels in pending_content until the
    device acknowledges); newly created entries carry the portal content
    directly so they are visible in the cameras view while pending.
    """
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    if existing:
        item = dict(existing)
    else:
        item = {
            'name': (body or {}).get('name'),
            'type': (body or {}).get('type'),
            'params': (body or {}).get('params') or {},
            'capabilities': {},
            'origin': ORIGIN_PORTAL_CREATED,
            'version': 0,
            'absent': False,
        }
    item.update({
        'device_id': device_id,
        'sk': f"{SK_CAMERA_PREFIX}{csid}",
        'camera_source_id': csid,
        'usecase_id': usecase_id,
        'sync_status': 'pending',
        'portal_change_id': portal_change_id,
        'pending_content': pending_content,
    })
    item.pop('failure_reason', None)  # a fresh change supersedes old failures
    table.put_item(Item={k: v for k, v in item.items() if v is not None})


def create_camera(device_id: str, user: Dict, event: Dict,
                  query_params: Dict, body: Any) -> Dict:
    """POST /devices/{id}/cameras (Operator — Reqs 5.1, 5.7)."""
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items)
    if not usecase_id and isinstance(body, dict):
        usecase_id = body.get('usecase_id')
    if not usecase_id:
        usecase_id = query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error
    error = validate_camera_body(body)
    if error:
        return error

    csid = body.get('camera_source_id') or f"portal-{uuid.uuid4().hex[:12]}"
    if find_camera_item(items, csid) is not None:
        return create_response(409, {
            'error': f"Camera source '{csid}' already exists",
        })

    portal_change_id = new_change_id()
    change = {
        'op': 'create',
        'portalChangeId': portal_change_id,
        'name': body['name'],
        'type': body['type'],
        'params': body.get('params') or {},
    }
    # Shadow FIRST; a failure returns 502 with the registry untouched.
    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        return error

    pending_content = {'op': 'create', 'name': body['name'],
                       'type': body['type'],
                       'params': body.get('params') or {}}
    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 pending_content, existing=None, body=body)
    audit_mutation(user, 'create_camera_source', device_id, csid,
                   usecase_id, portal_change_id)
    return create_response(201, {
        'device_id': device_id,
        'camera_source_id': csid,
        'origin': ORIGIN_PORTAL_CREATED,
        'sync_status': 'pending',
        'portal_change_id': portal_change_id,
    })


def update_camera(device_id: str, csid: str, user: Dict, event: Dict,
                  query_params: Dict, body: Any) -> Dict:
    """PUT /devices/{id}/cameras/{csid} (Operator — Reqs 5.1, 5.6, 5.7)."""
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error
    entry = find_camera_item(items, csid)
    if entry is None:
        return create_response(404, {
            'error': f"Camera source '{csid}' not found"})
    if entry.get('origin') == ORIGIN_EDGE_DISCOVERED:
        return discovery_managed_rejection(csid)
    error = validate_camera_body(body)
    if error:
        return error

    portal_change_id = new_change_id()
    change = {
        'op': 'update',
        'portalChangeId': portal_change_id,
        'baseVersion': entry.get('version'),
        'name': body['name'],
        'type': body['type'],
        'params': body.get('params') or {},
    }
    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        return error

    pending_content = {'op': 'update', 'name': body['name'],
                       'type': body['type'],
                       'params': body.get('params') or {}}
    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 pending_content, existing=entry)
    audit_mutation(user, 'update_camera_source', device_id, csid,
                   usecase_id, portal_change_id)
    return create_response(200, {
        'device_id': device_id,
        'camera_source_id': csid,
        'sync_status': 'pending',
        'portal_change_id': portal_change_id,
    })


def delete_camera(device_id: str, csid: str, user: Dict, event: Dict,
                  query_params: Dict) -> Dict:
    """DELETE /devices/{id}/cameras/{csid} (Operator) — pending delete."""
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error
    entry = find_camera_item(items, csid)
    if entry is None:
        return create_response(404, {
            'error': f"Camera source '{csid}' not found"})
    if entry.get('origin') == ORIGIN_EDGE_DISCOVERED:
        return discovery_managed_rejection(csid)

    portal_change_id = new_change_id()
    change = {
        'op': 'delete',
        'portalChangeId': portal_change_id,
        'baseVersion': entry.get('version'),
    }
    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        return error

    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 {'op': 'delete'}, existing=entry)
    audit_mutation(user, 'delete_camera_source', device_id, csid,
                   usecase_id, portal_change_id)
    return create_response(200, {
        'device_id': device_id,
        'camera_source_id': csid,
        'sync_status': 'pending',
        'portal_change_id': portal_change_id,
    })


# ---------------------------------------------------------------------------
# Conflict re-apply (task 6.2 — Req 6.4)
# ---------------------------------------------------------------------------

def reapply_conflict(device_id: str, cid: str, user: Dict, event: Dict,
                     query_params: Dict) -> Dict:
    """POST /devices/{id}/cameras/conflicts/{cid}/reapply (Operator).

    Re-issues the conflict's overridden portal version as a new pending
    change with a fresh portal_change_id and marks the conflict event
    ``reapplied_as`` (Req 6.4).
    """
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error

    conflict = next(
        (item for item in items
         if (item.get('sk') or '').startswith(SK_CONFLICT_PREFIX)
         and (item['sk'].rsplit('#', 1)[-1] == cid)),
        None)
    if conflict is None:
        return create_response(404, {
            'error': f"Conflict event '{cid}' not found"})

    portal_version = conflict.get('portal_version') or {}
    csid = conflict.get('camera_source_id')
    if not portal_version or not csid:
        return create_response(400, {
            'error': 'Conflict event carries no portal version to re-apply'})

    entry = find_camera_item(items, csid)
    if entry is not None and entry.get('origin') == ORIGIN_EDGE_DISCOVERED:
        return discovery_managed_rejection(csid)

    # The overridden portal version becomes a new pending change: an
    # update against the current edge-retained entry, or a re-create
    # when the deletion was retained (Req 6.5 aftermath).
    op = portal_version.get('op') or ('create' if entry is None else 'update')
    if op == 'update' and entry is None:
        op = 'create'
    if op == 'delete' and entry is None:
        return create_response(409, {
            'error': f"Camera source '{csid}' no longer exists; the "
                     "portal deletion is already effective"})

    portal_change_id = new_change_id()
    change: Dict[str, Any] = {'op': op, 'portalChangeId': portal_change_id}
    if op != 'delete':
        change.update({
            'name': portal_version.get('name'),
            'type': portal_version.get('type'),
            'params': portal_version.get('params') or {},
        })
    if entry is not None:
        change['baseVersion'] = entry.get('version')

    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        return error

    pending_content = {'op': op}
    if op != 'delete':
        pending_content.update({
            'name': portal_version.get('name'),
            'type': portal_version.get('type'),
            'params': portal_version.get('params') or {},
        })
    body_for_create = {
        'name': portal_version.get('name'),
        'type': portal_version.get('type'),
        'params': portal_version.get('params') or {},
    }
    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 pending_content, existing=entry, body=body_for_create)

    dynamodb.Table(CAMERA_REGISTRY_TABLE).update_item(
        Key={'device_id': device_id, 'sk': conflict['sk']},
        UpdateExpression='SET reapplied_as = :pc',
        ExpressionAttributeValues={':pc': portal_change_id},
    )
    audit_mutation(user, 'reapply_camera_conflict', device_id, csid,
                   usecase_id, portal_change_id, {'conflict_id': cid})
    return create_response(200, {
        'device_id': device_id,
        'camera_source_id': csid,
        'conflict_id': cid,
        'sync_status': 'pending',
        'portal_change_id': portal_change_id,
    })


# ---------------------------------------------------------------------------
# On-demand refresh (task 6.2) — GetThingShadow pull through the same reducer
# ---------------------------------------------------------------------------

def refresh_cameras(device_id: str, user: Dict, event: Dict,
                    query_params: Dict) -> Dict:
    """POST /devices/{id}/cameras/refresh (Viewer).

    Pulls the device's dda-camera-registry shadow via get_usecase_client
    and runs the exact same reduction the SQS ingest path uses
    (camera_sync._process_report), then returns the refreshed inventory.
    """
    items = query_device_items(device_id)
    usecase_id = device_usecase_id(items) or query_params.get('usecase_id')
    error = authorize(user, event, device_id, usecase_id, VIEW_PERMISSION)
    if error:
        return error

    try:
        client = iot_data_client(usecase_id)
        response = client.get_thing_shadow(
            thingName=device_id, shadowName=SHADOW_NAME)
        payload = json.loads(response['payload'].read(),
                             parse_float=Decimal)
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ResourceNotFoundException':
            return create_response(404, {
                'error': 'Device has no camera registry shadow to refresh from',
            })
        logger.error(f"Shadow refresh pull failed for {device_id}: {e}")
        return create_response(502, {
            'error': 'Failed to read the device sync channel'})
    except Exception as e:  # noqa: BLE001 — assumed-role/parse failures
        logger.error(f"Shadow refresh pull failed for {device_id}: {e}")
        return create_response(502, {
            'error': 'Failed to read the device sync channel'})

    reported = ((payload.get('state') or {}).get('reported'))
    if isinstance(reported, dict):
        camera_sync._process_report(device_id, reported,
                                    usecase_id=usecase_id)

    # Return the refreshed inventory in the same shape as the GET route.
    return get_cameras(device_id, user, event, query_params)


# ---------------------------------------------------------------------------
# Portal_Pin_API routes (cloud-static-camera-provisioning task 2.1)
#
#   POST   /devices/{id}/cameras/static-image/upload-url   (MANAGE_DEVICES)
#   POST   /devices/{id}/cameras/static-image/pin          (MANAGE_DEVICES)
#   DELETE /devices/{id}/cameras/static-image/pin          (MANAGE_DEVICES)
#   GET    /devices/{id}/cameras/static-image              (VIEW_DEVICES)
# ---------------------------------------------------------------------------

_pin_s3_client = None


#: Presigned staging PUTs must be SigV4. Without an explicit signature
#: version, botocore presigns S3 URLs in us-east-1 with legacy SigV2, whose
#: StringToSign includes the Content-Type header: the upload-url route
#: presigns with no content type while the browser's ``fetch(url, {method:
#: 'PUT', body: file})`` sends the File's ``image/*`` type, so S3 answered
#: every pin upload with 403 SignatureDoesNotMatch (2026-09-14). A SigV4
#: query-string presign signs only ``host``, so the browser-chosen header
#: cannot invalidate it.
PIN_S3_CLIENT_CONFIG = Config(signature_version='s3v4')


def pin_s3_client():
    """Lazy S3 client for the Image_Transport (component bucket).

    A swappable module seam (the iot_data_client pattern) so tests can
    install recording/failing fakes.
    """
    global _pin_s3_client
    if _pin_s3_client is None:
        _pin_s3_client = boto3.client('s3', config=PIN_S3_CLIENT_CONFIG)
    return _pin_s3_client


def resolve_pin_usecase_id(device_id: str,
                           items: Optional[List[Dict[str, Any]]] = None
                           ) -> Optional[str]:
    """The Target_Device's Use_Case from Portal-side records ONLY (Req 8.5).

    Devices table first (the camera_sync._resolve_usecase_id pattern), the
    device's own registry items second. Caller-supplied scoping parameters
    are never consulted. None means the device has no Portal record —
    rejected with 404 before any side effect (Reqs 1.8, 8.7).
    """
    devices_table = os.environ.get('DEVICES_TABLE')
    if devices_table:
        response = dynamodb.Table(devices_table).get_item(
            Key={'device_id': device_id})
        usecase_id = (response.get('Item') or {}).get('usecase_id')
        if usecase_id:
            return str(usecase_id)
    if items is None:
        items = query_device_items(device_id)
    return device_usecase_id(items)


def pin_device_not_registered(device_id: str) -> Dict:
    """404 for a device with no Portal record (Reqs 1.8, 8.7)."""
    return create_response(404, {
        'error': f"Device '{device_id}' is not registered in the Portal",
    })


def pin_size_limit_response(limit: int) -> Dict:
    """400 naming the size limit (Req 1.4 — '50 MB' for the default)."""
    return create_response(400, {
        'error': 'Image file exceeds the maximum pin size of '
                 f'{limit} bytes ({limit / (1024 * 1024):g} MB)',
    })


def pin_unsupported_format_response() -> Dict:
    """400 enumerating the Supported_Image_Formats (Req 1.3), mirroring
    the StaticImageStore rejection message."""
    return create_response(400, {
        'error': 'The submitted file could not be decoded as a supported '
                 'image format. Supported formats: {}.'.format(
                     ', '.join(SUPPORTED_PIN_FORMATS)),
    })


def validate_pin_image(data: bytes):
    """(image_format, error_response) for a staged upload's bytes.

    Pillow via the imaging-layer convention (lazy import, the
    synthetic_data.py precedent); the default decompression-bomb guard
    stays enabled, and a bomb rejection surfaces as the standard
    unsupported-format 400. Full decode (img.load) so truncated files
    are rejected exactly like the device-side pin would reject them.
    """
    import io

    from PIL import Image  # lazy import — the imaging-layer convention

    try:
        with Image.open(io.BytesIO(data)) as img:
            image_format = img.format
            if image_format not in SUPPORTED_PIN_FORMATS:
                return None, pin_unsupported_format_response()
            img.load()
    except Exception:  # noqa: BLE001 — any decode failure is a 400
        return None, pin_unsupported_format_response()
    return image_format, None


def desired_pin_section(document: Dict[str, Any]) -> Dict[str, Any]:
    """The full-width desired.staticImagePin section for a shadow write:
    every known field explicit (absent ones null) so the slot is replaced
    wholesale under IoT shadow merge semantics."""
    return {field: document.get(field) for field in DESIRED_PIN_FIELDS}


def write_desired_pin(usecase_id: str, device_id: str,
                      section: Dict[str, Any]) -> Optional[Dict]:
    """Replace the desired.staticImagePin slot on the device's registry
    shadow (top-level merge — never clobbers desired.changes).

    The SAME update also clears the device's stale reported.staticImagePin
    echo. Without the clear, IoT's per-field delta computation starves the
    device of any desired field equal to the previous echo (observed on
    hardware: a pin→pin replace delivered a delta with no `op`/`bucket`,
    hanging the request pending forever). Clearing loses nothing: the
    device re-echoes on confirmation, the portal has already ingested the
    prior confirmation through the documents event, and the ingest treats
    an absent reported section as a no-op.

    Returns an error response on failure, None on success (Req 1.9's
    delivery-initiation failure surface).
    """
    try:
        client = iot_data_client(usecase_id)
        client.update_thing_shadow(
            thingName=device_id,
            shadowName=SHADOW_NAME,
            payload=json.dumps(
                {'state': {'desired': {'staticImagePin': section},
                           'reported': {'staticImagePin': None}}},
                default=lambda o: float(o) if isinstance(o, Decimal) else o,
            ),
        )
        return None
    except Exception as e:  # noqa: BLE001 — any shadow-path failure is a 502
        logger.error(f"Pin desired write failed for {device_id}: {e}")
        return create_response(502, {
            'error': 'Failed to initiate delivery of the pin request '
                     'through the device sync channel',
        })


def delete_staged_pin_object(s3, staging_key: str) -> None:
    """Best-effort staging cleanup (the 1-day lifecycle rule is the
    backstop)."""
    try:
        s3.delete_object(Bucket=COMPONENT_BUCKET, Key=staging_key)
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        logger.warning(
            f"Best-effort delete of staged pin object {staging_key} failed")


def audit_pin_request(user: Dict, action: str, device_id: str,
                      usecase_id: str, pin_request_id: str, op: str,
                      extra: Optional[Dict] = None) -> None:
    """Acceptance audit event (Req 8.4): acting user and timestamp are
    stamped by log_audit_event; details carry the device, operation type,
    and Pin_Request identifier."""
    details = {
        'device_id': device_id,
        'usecase_id': usecase_id,
        'pin_request_id': pin_request_id,
        'operation': op,
    }
    if extra:
        details.update(extra)
    log_audit_event(user['user_id'], action, 'camera_registry', device_id,
                    'success', details)


def fail_pin_request(table, item: Dict[str, Any], reason: str, s3) -> None:
    """Transition a just-written pending item to failed (Req 1.9)."""
    pin_requests.transition_pin_request(
        table, item, pin_requests.STATUS_FAILED,
        completed_at_ms=now_ms(), failure_reason=reason, s3_client=s3)


def get_static_image_upload_url(device_id: str, user: Dict, event: Dict) -> Dict:
    """POST /devices/{id}/cameras/static-image/upload-url (Operator).

    Presigned PUT for a fresh staging key (15-minute TTL). The staged
    object is validated and server-side-copied by the pin submit route;
    clients can never write the canonical object (Decision 2).
    """
    usecase_id = resolve_pin_usecase_id(device_id)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error
    if not COMPONENT_BUCKET:
        return create_response(500, {'error': 'Component bucket not configured'})

    staging_key = f"{STATIC_IMAGE_STAGING_PREFIX}{uuid.uuid4().hex}"
    upload_url = pin_s3_client().generate_presigned_url(
        'put_object',
        Params={'Bucket': COMPONENT_BUCKET, 'Key': staging_key},
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    return create_response(200, {
        'deviceId': device_id,
        'uploadUrl': upload_url,
        'stagingKey': staging_key,
        'bucket': COMPONENT_BUCKET,
        'expiresInSeconds': UPLOAD_URL_TTL_SECONDS,
    })


def pin_static_image(device_id: str, user: Dict, event: Dict,
                     body: Any) -> Dict:
    """POST /devices/{id}/cameras/static-image/pin (Operator).

    Validates the staged object (Reqs 1.1, 1.3, 1.4), then runs the
    submission flow: supersede any pending Pin_Request (Req 5.3), write
    the new item as pending, copy the content to the canonical key
    strictly before any Sync_Channel write (Req 2.1), replace the
    desired.staticImagePin slot wholesale (Reqs 2.2–2.4), audit (Req 8.4).
    A store- or shadow-step failure transitions the item to failed with an
    error identifying the failing step (Reqs 1.9, 2.5).
    """
    usecase_id = resolve_pin_usecase_id(device_id)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error

    if not isinstance(body, dict):
        return create_response(400, {'error': 'JSON object body required'})
    staging_key = body.get('stagingKey')
    if (not staging_key or not isinstance(staging_key, str)
            or not staging_key.startswith(STATIC_IMAGE_STAGING_PREFIX)):
        return create_response(400, {
            'error': 'stagingKey (a key issued by the upload-url route, '
                     f'under {STATIC_IMAGE_STAGING_PREFIX}) is required',
        })
    file_name = body.get('fileName')
    if not file_name or not isinstance(file_name, str):
        return create_response(400, {'error': 'fileName is required'})
    if not COMPONENT_BUCKET:
        return create_response(500, {'error': 'Component bucket not configured'})

    s3 = pin_s3_client()
    limit = MAX_PIN_IMAGE_BYTES

    # HeadObject size check before downloading (design error table).
    try:
        head = s3.head_object(Bucket=COMPONENT_BUCKET, Key=staging_key)
    except ClientError:
        return create_response(400, {
            'error': 'Staged upload not found; request a new upload URL '
                     'and upload the image again',
        })
    if int(head.get('ContentLength') or 0) > limit:
        delete_staged_pin_object(s3, staging_key)
        return pin_size_limit_response(limit)

    data = s3.get_object(Bucket=COMPONENT_BUCKET,
                         Key=staging_key)['Body'].read()
    if len(data) > limit:  # defense in depth over the HeadObject check
        delete_staged_pin_object(s3, staging_key)
        return pin_size_limit_response(limit)

    image_format, error = validate_pin_image(data)
    if error:
        delete_staged_pin_object(s3, staging_key)
        return error
    sha256 = hashlib.sha256(data).hexdigest()

    now = now_ms()
    pin_request_id = pin_requests.new_pin_request_id(now)
    canonical_key = f"{STATIC_IMAGE_PIN_PREFIX}/{device_id}/{pin_request_id}"
    item = pin_requests.build_pin_request_item(
        device_id, usecase_id, pin_requests.OP_PIN, now,
        pin_request_id=pin_request_id,
        s3_bucket=COMPONENT_BUCKET, s3_key=canonical_key, sha256=sha256,
        size_bytes=len(data), image_format=image_format,
        file_name=file_name,
    )
    section = desired_pin_section(pin_requests.build_desired_document(item))

    # The ≤ 1024 B bound on the serialized section, enforced before any
    # side effect (Reqs 2.3, 2.4; Decision 1's shadow budget).
    if pin_requests.desired_document_size_bytes(section) > \
            pin_requests.MAX_DESIRED_SECTION_BYTES:
        delete_staged_pin_object(s3, staging_key)
        return create_response(400, {
            'error': 'Pin request sync document would exceed the size '
                     f'limit of {pin_requests.MAX_DESIRED_SECTION_BYTES} '
                     'bytes',
        })

    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    pin_requests.supersede_pending_requests(table, device_id, now,
                                            s3_client=s3)
    pin_requests.insert_pin_request(table, item)

    # Content stored in the Image_Transport strictly BEFORE any
    # Sync_Channel write (Req 2.1). CopyObject after validation: the
    # canonical object is never client-writable (Decision 2).
    try:
        s3.copy_object(
            Bucket=COMPONENT_BUCKET, Key=canonical_key,
            CopySource={'Bucket': COMPONENT_BUCKET, 'Key': staging_key},
        )
    except Exception as e:  # noqa: BLE001 — any store failure is Req 2.5
        logger.error(f"Pin canonical copy failed for {device_id}: {e}")
        fail_pin_request(table, item,
                         'image transport storage failed', s3)
        return create_response(502, {
            'error': 'Failed to store the pin image content for device '
                     'delivery',
        })

    error = write_desired_pin(usecase_id, device_id, section)
    if error:
        fail_pin_request(table, item,
                         'sync channel delivery initiation failed', s3)
        return error

    delete_staged_pin_object(s3, staging_key)
    audit_pin_request(user, 'pin_static_image', device_id, usecase_id,
                      pin_request_id, pin_requests.OP_PIN,
                      {'file_name': file_name, 'size_bytes': len(data),
                       'format': image_format, 'sha256': sha256})
    return create_response(201, {
        'pinRequestId': pin_request_id,
        'deviceId': device_id,
        'status': pin_requests.STATUS_PENDING,
    })


def remove_static_image_pin(device_id: str, user: Dict, event: Dict) -> Dict:
    """DELETE /devices/{id}/cameras/static-image/pin (Operator).

    Removal Pin_Request (op: remove, no Image_Transport object) through
    the same lifecycle and desired-slot replacement (Req 7.2).
    """
    usecase_id = resolve_pin_usecase_id(device_id)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error

    now = now_ms()
    pin_request_id = pin_requests.new_pin_request_id(now)
    item = pin_requests.build_pin_request_item(
        device_id, usecase_id, pin_requests.OP_REMOVE, now,
        pin_request_id=pin_request_id,
    )
    section = desired_pin_section(pin_requests.build_desired_document(item))

    s3 = pin_s3_client()
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    pin_requests.supersede_pending_requests(table, device_id, now,
                                            s3_client=s3)
    pin_requests.insert_pin_request(table, item)

    error = write_desired_pin(usecase_id, device_id, section)
    if error:
        fail_pin_request(table, item,
                         'sync channel delivery initiation failed', s3)
        return error

    audit_pin_request(user, 'remove_static_image', device_id, usecase_id,
                      pin_request_id, pin_requests.OP_REMOVE)
    return create_response(200, {
        'pinRequestId': pin_request_id,
        'deviceId': device_id,
        'status': pin_requests.STATUS_PENDING,
    })


def get_static_image_status(device_id: str, user: Dict, event: Dict) -> Dict:
    """GET /devices/{id}/cameras/static-image (Viewer).

    The provisioning status view (Reqs 1.7, 1.10, 4.4–4.8, 5.7):
    latest non-superseded request, history, deviceReported state from the
    CAMERA#static-image-camera registry entry, and connectivity (mapped
    to exactly connected/disconnected) while the latest request is
    pending.
    """
    items = query_device_items(device_id)
    usecase_id = resolve_pin_usecase_id(device_id, items=items)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, VIEW_PERMISSION)
    if error:
        return error

    camera_entry = next(
        (item for item in items
         if item.get('sk') == pin_requests.SK_STATIC_IMAGE_CAMERA), None)
    view = pin_requests.build_status_view(
        device_id, items,
        usecase_id=usecase_id,
        camera_entry=camera_entry,
        connectivity_provider=lambda: device_connectivity_status(
            usecase_id, device_id),
    )
    return create_response(200, view)


# ---------------------------------------------------------------------------
# Portal_Video_Pin_API routes (static-camera-video-loop)
#
#   POST   /devices/{id}/cameras/static-video/upload-url   (MANAGE_DEVICES)
#   POST   /devices/{id}/cameras/static-video/pin          (MANAGE_DEVICES)
#   DELETE /devices/{id}/cameras/static-video/pin          (MANAGE_DEVICES)
#   GET    /devices/{id}/cameras/static-video              (VIEW_DEVICES)
#
# Served by the CameraVideoPinHandler function, which carries the video
# layer (OpenCV) that Video_Validation needs. They mirror the image routes
# above, with the same authorization, use-case resolution, and audit
# behavior, over the parallel VIDEO_PIN_REQUEST# item family and the
# desired.staticVideoPin slot, so video and image requests never interact
# (Requirement 8.9). The image routes are unchanged.
#
# One difference: Video_Validation (up to 60 s of decoding) runs in an
# asynchronous job on the same function (run_video_validation), tracked by
# VIDEO_VALIDATION# records, because the API Gateway integration timeout
# (29 s) cannot hold it. The pin route answers 202 and the status view
# reports the validation outcome.
# ---------------------------------------------------------------------------


class VideoProbeTimeout(Exception):
    """Video_Validation did not finish within the time budget."""


def _video_probe_child(path: str) -> Dict[str, Any]:
    """Run ``video_loop.probe_video`` on ``path`` in a child process.

    The child is ``python video_loop.py probe <path>`` (the vendored
    module's script entry point), with this interpreter and this process's
    import path, so it sees the video layer's OpenCV. It prints one JSON
    object: ``{"ok": true, "info": {...}}`` or ``{"ok": false, "error":
    "..."}``. Raises :class:`VideoProbeTimeout` after
    :data:`VIDEO_VALIDATION_TIMEOUT_SECONDS` (the child is killed). A child
    that crashes or prints no result is reported as undecodable with the
    codec unknown."""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        entry for entry in sys.path if isinstance(entry, str) and entry)
    env.setdefault('OPENCV_FFMPEG_LOGLEVEL', '8')
    try:
        completed = subprocess.run(
            [sys.executable, video_loop.__file__, 'probe', path],
            capture_output=True,
            timeout=VIDEO_VALIDATION_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProbeTimeout(str(exc)) from exc
    try:
        result = json.loads(completed.stdout.decode('utf-8', 'replace'))
    except (ValueError, AttributeError):
        result = None
    if completed.returncode != 0 or not isinstance(result, dict):
        logger.error(
            'Video validation child exited %s without a result; stderr: %s',
            completed.returncode,
            completed.stderr.decode('utf-8', 'replace')[-2000:])
        return {'ok': False, 'error': video_loop.undecodable_message('unknown')}
    if result.get('internal'):
        logger.error('Video validation child reported an internal error: %s',
                     result['internal'])
    return result


#: The Video_Validation runner — a swappable module seam (the
#: iot_data_client pattern): tests install an in-process or stub runner;
#: the child-process runner is exercised directly.
run_video_probe = _video_probe_child


def video_validation_verdict(path: str):
    """(validated_metadata, error_message) for a staged video at ``path``.

    The metadata is what the probe determined (container ``format`` plus
    codec, displayed width/height, fps, frameCount, durationMs). A
    rejection carries the Video_Validation message, the timeout message
    (Requirement 8.4), or the undecodable message for a crashed child."""
    try:
        result = run_video_probe(path)
    except VideoProbeTimeout:
        return None, VIDEO_VALIDATION_TIMEOUT_MESSAGE
    except Exception as e:  # noqa: BLE001 — any runner failure rejects
        logger.error(f"Video validation failed to run: {e}")
        return None, video_loop.undecodable_message('unknown')
    if not isinstance(result, dict) or not result.get('ok'):
        message = (result or {}).get('error') if isinstance(result, dict) \
            else None
        return None, message or video_loop.undecodable_message('unknown')
    info = result.get('info')
    if not isinstance(info, dict) or not info.get('format'):
        return None, video_loop.undecodable_message('unknown')
    return dict(info), None


def validate_pin_video(path: str):
    """(validated_metadata, error_response): :func:`video_validation_verdict`
    with a rejection as a 400 response."""
    info, message = video_validation_verdict(path)
    if message is not None:
        return None, create_response(400, {'error': message})
    return info, None


def pin_video_size_limit_response(size: int, limit: int) -> Dict:
    """400 naming the video size limit (Requirement 8.3), in the device
    store's wording."""
    return create_response(400, {
        'error': video_loop.oversize_message(size, limit)})


def write_desired_video_pin(usecase_id: str, device_id: str,
                            section: Dict[str, Any]) -> Optional[Dict]:
    """Replace the desired.staticVideoPin slot (and clear the stale
    reported.staticVideoPin echo) — :func:`write_desired_pin` for the video
    slot, for the same partial-delta reason. Never touches the image slot.

    Returns an error response on failure, None on success."""
    try:
        client = iot_data_client(usecase_id)
        client.update_thing_shadow(
            thingName=device_id,
            shadowName=SHADOW_NAME,
            payload=json.dumps(
                {'state': {
                    'desired': {DESIRED_VIDEO_PIN_SECTION: section},
                    'reported': {DESIRED_VIDEO_PIN_SECTION: None}}},
                default=lambda o: float(o) if isinstance(o, Decimal) else o,
            ),
        )
        return None
    except Exception as e:  # noqa: BLE001 — any shadow-path failure is a 502
        logger.error(f"Video pin desired write failed for {device_id}: {e}")
        return create_response(502, {
            'error': 'Failed to initiate delivery of the video pin request '
                     'through the device sync channel',
        })


def _download_staged_video(s3, staging_key: str, limit: int, handle):
    """Stream the staged object into ``handle``; returns ``(size, sha256)``,
    or ``(size, None)`` as soon as the stream exceeds ``limit``."""
    body = s3.get_object(Bucket=COMPONENT_BUCKET, Key=staging_key)['Body']
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = body.read(_VIDEO_DOWNLOAD_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            return size, None
        hasher.update(chunk)
        handle.write(chunk)
    handle.flush()
    return size, hasher.hexdigest()


def get_static_video_upload_url(device_id: str, user: Dict, event: Dict) -> Dict:
    """POST /devices/{id}/cameras/static-video/upload-url (Operator).

    Staging is shared with images (design Decision 9), so this issues the
    same presigned staging PUT as the image route."""
    return get_static_image_upload_url(device_id, user, event)


def pin_static_video(device_id: str, user: Dict, event: Dict,
                     body: Any) -> Dict:
    """POST /devices/{id}/cameras/static-video/pin (Operator).

    Accepts a staged video for asynchronous Video_Validation (Requirements
    8.2–8.5): the checks that need no decoding run here (authorization,
    the body, the 100 MB limit, the staged object's presence) and reject
    immediately. Then any submission still validating for the device is
    superseded, a ``validating`` record is written, and the validation job
    is handed to this function asynchronously (:func:`run_video_validation`,
    outside the API Gateway integration timeout). The response is a 202
    with the validation id; the video status view reports the outcome.
    A rejected upload records no Video_Pin_Request and writes nothing to
    the transport or the sync channel (Requirement 8.3)."""
    usecase_id = resolve_pin_usecase_id(device_id)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error

    if not isinstance(body, dict):
        return create_response(400, {'error': 'JSON object body required'})
    staging_key = body.get('stagingKey')
    if (not staging_key or not isinstance(staging_key, str)
            or not staging_key.startswith(STATIC_IMAGE_STAGING_PREFIX)):
        return create_response(400, {
            'error': 'stagingKey (a key issued by the upload-url route, '
                     f'under {STATIC_IMAGE_STAGING_PREFIX}) is required',
        })
    file_name = body.get('fileName')
    if not file_name or not isinstance(file_name, str):
        return create_response(400, {'error': 'fileName is required'})
    if not COMPONENT_BUCKET:
        return create_response(500, {'error': 'Component bucket not configured'})

    s3 = pin_s3_client()
    limit = MAX_PIN_VIDEO_BYTES
    try:
        head = s3.head_object(Bucket=COMPONENT_BUCKET, Key=staging_key)
    except ClientError:
        return create_response(400, {'error': STAGED_VIDEO_NOT_FOUND_MESSAGE})
    declared = int(head.get('ContentLength') or 0)
    if declared > limit:
        delete_staged_pin_object(s3, staging_key)
        return pin_video_size_limit_response(declared, limit)

    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    now = now_ms()
    supersede_video_validations(table, device_id, now, s3)
    record = pin_requests.build_video_validation_item(
        device_id, usecase_id, now,
        staging_key=staging_key, file_name=file_name, size_bytes=declared,
        requested_by=user['user_id'],
    )
    table.put_item(Item=record)
    job = {'deviceId': device_id, 'validationId': record['validation_id']}
    try:
        dispatch_video_validation(job)
    except Exception as e:  # noqa: BLE001 — any dispatch failure is a 502
        logger.error(f"Video validation dispatch failed for {device_id}: {e}")
        message = 'Video validation could not be started; submit the video again'
        pin_requests.transition_video_validation(
            table, record, pin_requests.VALIDATION_REJECTED,
            completed_at_ms=now_ms(), error=message)
        delete_staged_pin_object(s3, staging_key)
        return create_response(502, {'error': message})
    return create_response(202, {
        'validationId': record['validation_id'],
        'deviceId': device_id,
        'status': pin_requests.VALIDATION_VALIDATING,
    })


def supersede_video_validations(table, device_id: str, now: int, s3) -> None:
    """Supersede the device's submissions still validating (a newer
    submission or a removal replaces them) and drop their staged objects;
    their jobs find the record superseded and stop."""
    for record in pin_requests.supersede_validating_video_validations(
            table, device_id, now):
        if record.get('staging_key'):
            delete_staged_pin_object(s3, record['staging_key'])


_lambda_client = None


def _dispatch_video_validation_event(job: Dict[str, Any]) -> None:
    """Invoke the validation job asynchronously (InvocationType Event) on
    the CameraVideoPinHandler (VIDEO_VALIDATION_FUNCTION, its fixed name)."""
    global _lambda_client
    if not VIDEO_VALIDATION_FUNCTION:
        raise RuntimeError('VIDEO_VALIDATION_FUNCTION is not configured')
    if _lambda_client is None:
        _lambda_client = boto3.client('lambda')
    _lambda_client.invoke(
        FunctionName=VIDEO_VALIDATION_FUNCTION,
        InvocationType='Event',
        Payload=json.dumps({VIDEO_VALIDATION_EVENT_KEY: job}).encode('utf-8'),
    )


#: Hands a validation job to the asynchronous runner — a swappable module
#: seam (the run_video_probe pattern): tests run the job inline or queue it.
dispatch_video_validation = _dispatch_video_validation_event


def run_video_validation(job: Any) -> Dict[str, Any]:
    """The asynchronous validation job for one submission (Requirements
    8.2–8.5), invoked with ``{"videoValidation": {"deviceId",
    "validationId"}}``. Returns ``{"status": ...}`` (for logs and tests).

    Idempotent and supersession-aware: a record that already left
    ``validating`` (a second run, or a newer submission or removal
    superseded it) is left alone; a record whose submission was replaced
    since (a newer validation record or Video_Pin_Request exists) is
    superseded, before the download and again before the submission flow.
    Otherwise the staged video is downloaded, checked against the limit,
    and decoded with a 60 s budget. A rejection records the message on the
    record and deletes the staged object, recording no Video_Pin_Request
    (8.3, 8.4); an acceptance runs the submission flow
    (:func:`submit_validated_video`) and records the Video_Pin_Request it
    created (8.5)."""
    job = job if isinstance(job, dict) else {}
    device_id = job.get('deviceId')
    validation_id = job.get('validationId')
    if not device_id or not validation_id or not CAMERA_REGISTRY_TABLE:
        logger.error(f"Malformed video validation job: {job!r}")
        return {'status': 'ignored'}
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    record = pin_requests.get_video_validation_item(table, device_id,
                                                    validation_id)
    if record is None or \
            record.get('status') != pin_requests.VALIDATION_VALIDATING:
        return {'status': 'ignored'}
    s3 = pin_s3_client()
    staging_key = record['staging_key']

    def finish(status: str, **fields) -> Dict[str, Any]:
        pin_requests.transition_video_validation(
            table, record, status, completed_at_ms=now_ms(), **fields)
        if status != pin_requests.VALIDATION_ACCEPTED:
            delete_staged_pin_object(s3, staging_key)
        return {'status': status}

    def superseded() -> bool:
        return pin_requests.newer_video_activity(table, device_id,
                                                 validation_id)

    try:
        created_at = int(record.get('created_at') or 0)
        if now_ms() - created_at > pin_requests.VIDEO_VALIDATION_EXPIRY_MS:
            return finish(pin_requests.VALIDATION_REJECTED,
                          error=pin_requests.VIDEO_VALIDATION_EXPIRED_MESSAGE)
        if superseded():
            return finish(pin_requests.VALIDATION_SUPERSEDED)

        limit = MAX_PIN_VIDEO_BYTES
        # OpenCV needs a path: stream the staged object to /tmp (1 GiB
        # ephemeral storage), hashing as it arrives; always removed.
        fd, local_path = tempfile.mkstemp(prefix='pin-video-',
                                          dir=tempfile.gettempdir())
        try:
            with os.fdopen(fd, 'wb') as handle:
                try:
                    size, sha256 = _download_staged_video(
                        s3, staging_key, limit, handle)
                except ClientError:
                    return finish(pin_requests.VALIDATION_REJECTED,
                                  error=STAGED_VIDEO_NOT_FOUND_MESSAGE)
            if sha256 is None:  # grew past the limit since the HeadObject
                return finish(pin_requests.VALIDATION_REJECTED,
                              error=video_loop.oversize_message(size, limit))
            info, message = video_validation_verdict(local_path)
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass
        if message is not None:
            return finish(pin_requests.VALIDATION_REJECTED, error=message)
        # The decode may take up to a minute: re-check before delivering.
        if superseded():
            return finish(pin_requests.VALIDATION_SUPERSEDED)

        outcome = submit_validated_video(record, info, size, sha256, s3,
                                         table)
        if outcome.get('error') is not None:
            return finish(pin_requests.VALIDATION_REJECTED,
                          error=outcome['error'])
        return finish(pin_requests.VALIDATION_ACCEPTED,
                      pin_request_id=outcome['pinRequestId'],
                      validated_metadata=outcome['validatedMetadata'])
    except Exception:  # noqa: BLE001 — never leave the record validating
        logger.exception(
            f"Video validation job failed for {device_id} ({validation_id})")
        return finish(pin_requests.VALIDATION_REJECTED,
                      error='Video validation failed unexpectedly; submit '
                            'the video again')


def submit_validated_video(record: Dict[str, Any], info: Dict[str, Any],
                           size: int, sha256: str, s3, table
                           ) -> Dict[str, Any]:
    """The image route's submission flow on the video family, for a
    validated staged video: supersede pending video requests only, write
    the pending item with the validated metadata, copy to the canonical
    key, replace desired.staticVideoPin, delete the staged object, audit
    (as the submitting user).

    Returns ``{"pinRequestId", "validatedMetadata"}``, or ``{"error"}``
    when the desired document would exceed its bound (nothing written). A
    store or delivery failure after the item was written fails that
    Video_Pin_Request (the status view shows why) and still returns its
    id."""
    device_id = record['device_id']
    usecase_id = record['usecase_id']
    staging_key = record['staging_key']
    file_name = record['file_name']
    container = info['format']
    validated = {field: info.get(field)
                 for field in VALIDATED_VIDEO_METADATA_FIELDS}
    now = now_ms()
    pin_request_id = pin_requests.new_pin_request_id(now)
    canonical_key = (f"{STATIC_IMAGE_PIN_PREFIX}/{device_id}/"
                     f"{STATIC_VIDEO_PIN_SUBPREFIX}/{pin_request_id}")
    prefix = pin_requests.SK_VIDEO_PIN_REQUEST_PREFIX
    item = pin_requests.build_pin_request_item(
        device_id, usecase_id, pin_requests.OP_PIN, now,
        pin_request_id=pin_request_id,
        s3_bucket=COMPONENT_BUCKET, s3_key=canonical_key, sha256=sha256,
        size_bytes=size, image_format=container, file_name=file_name,
        sk_prefix=prefix, validated_metadata=validated,
    )
    section = desired_pin_section(pin_requests.build_desired_document(item))
    if pin_requests.desired_document_size_bytes(section) > \
            pin_requests.MAX_DESIRED_SECTION_BYTES:
        return {'error': 'Pin request sync document would exceed the size '
                         f'limit of {pin_requests.MAX_DESIRED_SECTION_BYTES} '
                         'bytes'}
    metadata = dict(validated, format=container)

    pin_requests.supersede_pending_requests(table, device_id, now,
                                            s3_client=s3, sk_prefix=prefix)
    pin_requests.insert_pin_request(table, item)

    try:
        s3.copy_object(
            Bucket=COMPONENT_BUCKET, Key=canonical_key,
            CopySource={'Bucket': COMPONENT_BUCKET, 'Key': staging_key},
        )
    except Exception as e:  # noqa: BLE001 — any store failure fails the item
        logger.error(f"Video pin canonical copy failed for {device_id}: {e}")
        fail_pin_request(table, item, 'image transport storage failed', s3)
        delete_staged_pin_object(s3, staging_key)
        return {'pinRequestId': pin_request_id, 'validatedMetadata': metadata}

    if write_desired_video_pin(usecase_id, device_id, section) is not None:
        fail_pin_request(table, item,
                         'sync channel delivery initiation failed', s3)
        delete_staged_pin_object(s3, staging_key)
        return {'pinRequestId': pin_request_id, 'validatedMetadata': metadata}

    delete_staged_pin_object(s3, staging_key)
    # The audit details map is stored as-is, and DynamoDB rejects floats.
    audited = {key: Decimal(str(value)) if isinstance(value, float) else value
               for key, value in validated.items()}
    audit_pin_request({'user_id': record.get('requested_by') or 'unknown'},
                      'pin_static_video', device_id, usecase_id,
                      pin_request_id, pin_requests.OP_PIN,
                      {'file_name': file_name, 'size_bytes': size,
                       'format': container, 'sha256': sha256,
                       'validated_metadata': audited,
                       'validation_id': record['validation_id']})
    return {'pinRequestId': pin_request_id, 'validatedMetadata': metadata}


def remove_static_video_pin(device_id: str, user: Dict, event: Dict) -> Dict:
    """DELETE /devices/{id}/cameras/static-video/pin (Operator).

    Removal Video_Pin_Request (op: remove) through the same lifecycle and
    the desired.staticVideoPin slot."""
    usecase_id = resolve_pin_usecase_id(device_id)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, MUTATE_PERMISSION)
    if error:
        return error

    now = now_ms()
    pin_request_id = pin_requests.new_pin_request_id(now)
    prefix = pin_requests.SK_VIDEO_PIN_REQUEST_PREFIX
    item = pin_requests.build_pin_request_item(
        device_id, usecase_id, pin_requests.OP_REMOVE, now,
        pin_request_id=pin_request_id, sk_prefix=prefix,
    )
    section = desired_pin_section(pin_requests.build_desired_document(item))

    s3 = pin_s3_client()
    table = dynamodb.Table(CAMERA_REGISTRY_TABLE)
    # A removal also replaces a submission still validating.
    supersede_video_validations(table, device_id, now, s3)
    pin_requests.supersede_pending_requests(table, device_id, now,
                                            s3_client=s3, sk_prefix=prefix)
    pin_requests.insert_pin_request(table, item)

    error = write_desired_video_pin(usecase_id, device_id, section)
    if error:
        fail_pin_request(table, item,
                         'sync channel delivery initiation failed', s3)
        return error

    audit_pin_request(user, 'remove_static_video', device_id, usecase_id,
                      pin_request_id, pin_requests.OP_REMOVE)
    return create_response(200, {
        'pinRequestId': pin_request_id,
        'deviceId': device_id,
        'status': pin_requests.STATUS_PENDING,
    })


def get_static_video_status(device_id: str, user: Dict, event: Dict) -> Dict:
    """GET /devices/{id}/cameras/static-video (Viewer).

    The image status view over the video family: latest non-superseded
    Video_Pin_Request (with its validated metadata), history,
    deviceReported from CAMERA#static-video-camera, the device-reported
    metadata once applied, and connectivity while pending. ``validation``
    reports the latest submission's asynchronous Video_Validation
    (validating, accepted with its pinRequestId, rejected with the
    message, superseded, or expired), when there is one."""
    items = query_device_items(device_id)
    usecase_id = resolve_pin_usecase_id(device_id, items=items)
    if not usecase_id:
        return pin_device_not_registered(device_id)
    error = authorize(user, event, device_id, usecase_id, VIEW_PERMISSION)
    if error:
        return error

    camera_entry = next(
        (item for item in items
         if item.get('sk') == pin_requests.SK_STATIC_VIDEO_CAMERA), None)
    view = pin_requests.build_status_view(
        device_id, items,
        usecase_id=usecase_id,
        camera_entry=camera_entry,
        connectivity_provider=lambda: device_connectivity_status(
            usecase_id, device_id),
        sk_prefix=pin_requests.SK_VIDEO_PIN_REQUEST_PREFIX,
    )
    validation = pin_requests.video_validation_view(items, now_ms())
    if validation is not None:
        view['validation'] = validation
    return create_response(200, view)


# ---------------------------------------------------------------------------
# Handler / routing
# ---------------------------------------------------------------------------

def handler(event, context):
    """Route Camera_Registry API requests (CameraRegistryApiStack routes)."""
    try:
        http_method = event.get('httpMethod')
        path = event.get('path', '') or ''
        path_parameters = event.get('pathParameters') or {}
        query_parameters = event.get('queryStringParameters') or {}

        logger.info(f"Camera_Registry request: {http_method} {path}")

        if http_method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
                    'Access-Control-Max-Age': '86400',
                },
                'body': ''
            }

        device_id = path_parameters.get('id')
        if not device_id:
            return create_response(400, {'error': 'device id required'})
        if not CAMERA_REGISTRY_TABLE:
            return create_response(500, {'error': 'Camera registry table not configured'})

        user = get_user_from_event(event)
        csid = path_parameters.get('csid')
        cid = path_parameters.get('cid')

        body: Any = None
        if event.get('body'):
            try:
                # parse_float=Decimal: camera params may carry non-integral
                # numbers and DynamoDB rejects Python floats.
                body = json.loads(event['body'], parse_float=Decimal)
            except (json.JSONDecodeError, ValueError):
                return create_response(400, {'error': 'Invalid JSON body'})

        # Portal_Pin_API static-image routes (cloud-static-camera-
        # provisioning task 2.1) — most specific paths first.
        if http_method == 'POST' and \
                path.endswith('/cameras/static-image/upload-url'):
            return get_static_image_upload_url(device_id, user, event)
        if http_method == 'POST' and \
                path.endswith('/cameras/static-image/pin'):
            return pin_static_image(device_id, user, event, body)
        if http_method == 'DELETE' and \
                path.endswith('/cameras/static-image/pin'):
            return remove_static_image_pin(device_id, user, event)
        if http_method == 'GET' and path.endswith('/cameras/static-image'):
            return get_static_image_status(device_id, user, event)

        # Portal_Video_Pin_API static-video routes (static-camera-video-loop).
        if http_method == 'POST' and \
                path.endswith('/cameras/static-video/upload-url'):
            return get_static_video_upload_url(device_id, user, event)
        if http_method == 'POST' and \
                path.endswith('/cameras/static-video/pin'):
            return pin_static_video(device_id, user, event, body)
        if http_method == 'DELETE' and \
                path.endswith('/cameras/static-video/pin'):
            return remove_static_video_pin(device_id, user, event)
        if http_method == 'GET' and path.endswith('/cameras/static-video'):
            return get_static_video_status(device_id, user, event)

        # Static segments before path params (conflicts/refresh), reads
        # before mutations.
        if http_method == 'GET' and path.endswith('/cameras/conflicts'):
            return get_conflicts(device_id, user, event, query_parameters)
        if http_method == 'GET' and path.endswith('/cameras'):
            return get_cameras(device_id, user, event, query_parameters)

        # Mutation / re-apply / refresh routes (task 6.2).
        if http_method == 'POST' and path.endswith('/reapply'):
            if not cid:
                return create_response(400, {'error': 'conflict id required'})
            return reapply_conflict(device_id, cid, user, event,
                                    query_parameters)
        if http_method == 'POST' and path.endswith('/cameras/refresh'):
            return refresh_cameras(device_id, user, event, query_parameters)
        if http_method == 'POST' and path.endswith('/cameras'):
            return create_camera(device_id, user, event, query_parameters,
                                 body)
        if csid and http_method == 'PUT':
            return update_camera(device_id, csid, user, event,
                                 query_parameters, body)
        if csid and http_method == 'DELETE':
            return delete_camera(device_id, csid, user, event,
                                 query_parameters)

        return create_response(404, {'error': 'Not found'})

    except Exception as e:
        logger.error(f"Error in camera_registry handler: {str(e)}", exc_info=True)
        return create_response(500, {'error': 'Internal server error'})
