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

Stream Camera_Sources (rtsp-rtmp-stream-cameras task 8.1). The ``RTSP``
and ``RTMP`` types are typed rather than free-form:
``validate_stream_camera_body`` checks their ``params`` against the
settings and value domains of Requirement 4.1 and their ``url`` against
the shared Stream_URL rules, and rejects credential material and
server-managed keys inside ``params`` with a 400 naming the field
(Req 5.2). Validation of every other type is untouched. On the way out,
``camera_view`` never returns credential material (Reqs 5.7, 6.1): a
stored ``url`` is redacted, the Credential_Reference is omitted, and a
stream entry reports ``credentials: {configured, updatedAt}`` instead.

Portal-managed Stream_Credentials (task 8.2). A stream create or update
may carry a write-only top-level ``credentials`` object (and a
``clearCredentials`` flag); the credentials themselves go to the
Credential_Vault of the device's Use_Case_Account through
``stream_credentials.py``, and only the Credential_Reference reaches the
desired change, the registry item, the pending content, and the audit
event (Req 5.3). The route order is design component 7's: authorize and
validate, ensure the device read grant, store the credentials, set the
reference, write the desired change, and only then mark the entry pending
and audit. A desired-change failure withdraws the stored version before
returning the existing 502, so nothing references it (Req 5.4), and
``clearCredentials`` or a delete schedules the secret's deletion *after*
the change is delivered (Req 5.8). If the Use_Case_Account does not grant
the Portal those capabilities, the credentialed create or update is
rejected with 409 ``STREAM_CREDENTIALS_UNAVAILABLE`` naming the missing
capability, with the registry, the shadow, and the audit log untouched;
credential-free stream cameras are still accepted (Req 5.9, task 8.3).
Authorization and the audit events are the existing camera registry ones
throughout (Req 5.10).

The camera's own secret (task 29, finding 22; Reqs 5.2, 5.8). The device
re-keys a Portal create to ``cfg-<imageSourceId>``, so the routes record
the secret's ARN on the entry (``credential_secret_arn``, through
``mark_pending``), resolve a camera's secrets from the record, the pending
and the reported Credential_Reference and the name of its id
(``credential_secret_ids``), and update, clear and delete by them, never
scheduling a secret another stream entry of the device still resolves.
Only secrets under the device's own prefix, in the use case's account and
region, are ever named. A stream create's body id is validated, an update
or delete of a stream create mirror is refused with 409
``CAMERA_SOURCE_ALIAS``, and a create no longer schedules anything. No
response carries ``credential_secret_arn`` or ``alias_of``: the views are
built field by field.
"""
import hashlib
import json
import logging
import os
import re
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
# Portal-managed Stream_Credentials (rtsp-rtmp-stream-cameras task 8.2),
# bundled into the same Lambda code asset. It imports nothing beyond
# boto3 and shared_utils, so it is safe at module scope on the camera_sync
# ingest path too (unlike workflow_core, which stays a lazy import).
import stream_credentials

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

# Machine-readable rejection code for an update or delete of a stream
# create mirror (rtsp-rtmp-stream-cameras task 29, design component 7
# "Create mirrors"): the entry only mirrors a Portal create the device
# acknowledged under an id of its own, so it owns no secret and is never
# the camera to change.
CAMERA_SOURCE_ALIAS = 'CAMERA_SOURCE_ALIAS'

# The prefix of the ids the device gives the Image_Sources it creates
# (``cfg-<imageSourceId>``), which the mirror shape rule reads.
CONFIGURED_ID_PREFIX = 'cfg-'

# A stream create's body ``camera_source_id`` (task 29, Requirement 5.2):
# 1 to 128 of the characters a Secrets Manager name allows, without '/',
# so the derived secret name is one segment under the device's prefix.
# Applied with ``fullmatch``: ``$`` also matches before a final newline.
STREAM_CREATE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.@+=-]{1,128}")

# Machine-readable rejection code for a credentialed stream mutation the
# Use_Case_Account does not let the Portal carry out (Req 5.9, design
# component 7 "Missing permissions" and the error-handling table).
STREAM_CREDENTIALS_UNAVAILABLE = 'STREAM_CREDENTIALS_UNAVAILABLE'

# The remediation the 409 names, alongside the missing capability itself.
STREAM_CREDENTIALS_REMEDIATION = (
    'update the use-case account stack to enable Portal-managed stream '
    'camera credentials')

ORIGIN_EDGE_DISCOVERED = 'edge-discovered'
ORIGIN_PORTAL_CREATED = 'portal-created'

# ---------------------------------------------------------------------------
# Stream Camera_Source constants (rtsp-rtmp-stream-cameras task 8.1)
# ---------------------------------------------------------------------------

# The ``params`` keys a stream Camera_Source of each type may carry, in
# the order Requirement 4.1 lists them. Transport and latency are RTSP
# only: nothing in the RTMP ingest path has either notion.
#
# These are literals rather than a projection of
# ``workflow_core.stream_url.SCHEMES_BY_SOURCE_TYPE`` for the reason
# deployments.py keeps its compatibility map literal: this module is
# bundled with the SQS sync path and stays importable without the
# workflow_core layer, so workflow_core is imported lazily, at the one
# call site that needs it. Membership of this map is what makes a type a
# *stream* type here; the accepted schemes of that type always come from
# the shared module, so the scheme rule cannot drift from the catalog,
# validator rule V11, the Deployment_Service or the device.
STREAM_PARAMS_BY_TYPE: Dict[str, tuple] = {
    'RTSP': ('url', 'transport', 'latencyMs', 'decoder',
             'maxFrameDimension', 'stallTimeoutS'),
    'RTMP': ('url', 'decoder', 'maxFrameDimension', 'stallTimeoutS'),
}

# Server-managed ``params`` keys: the Portal writes these itself when it
# stores Stream_Credentials (task 8.2), so a request may never set them
# (Req 5.3 — the registry carries only the Credential_Reference, and only
# one the Portal itself produced).
STREAM_SERVER_MANAGED_PARAMS = ('credentialRef', 'credentialsConfigured',
                                'credentialsUpdatedAt')

# Credential-like ``params`` keys. Stream_Credentials travel in the
# write-only top-level ``credentials`` object and are stored in the
# Credential_Vault, never in ``params`` — which is echoed into the
# desired shadow, the registry item, the pending content and audit
# events (Reqs 5.2, 6.1).
STREAM_CREDENTIAL_PARAMS = ('username', 'user', 'password', 'secret',
                            'token', 'urlSecret')

# Value domains of Requirement 4.1.
STREAM_TRANSPORTS = ('tcp', 'udp', 'auto')
STREAM_DECODER_POLICIES = ('auto', 'hardware', 'software')
STREAM_LATENCY_MS_RANGE = (0, 5000)
STREAM_MAX_FRAME_DIMENSION_RANGE = (320, 4096)
STREAM_STALL_TIMEOUT_S_RANGE = (2, 60)

# The single ``params`` key that holds a Stream_URL, and the
# Credential_Reference key the view omits (Req 5.7).
PARAM_URL = 'url'
PARAM_CREDENTIAL_REF = 'credentialRef'
PARAM_CREDENTIALS_CONFIGURED = 'credentialsConfigured'
PARAM_CREDENTIALS_UPDATED_AT = 'credentialsUpdatedAt'

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

def is_stream_camera_type(source_type: Any) -> bool:
    """Whether ``source_type`` is a stream Camera_Source type.

    The single spelling of "this is an RTSP or RTMP Camera_Source", used
    by the body validation and by the view (rtsp-rtmp-stream-cameras
    Reqs 5.2, 5.7). Total: a non-string type is not a stream type.
    """
    return isinstance(source_type, str) and source_type in STREAM_PARAMS_BY_TYPE


#: What a credential-like ``params`` value of a stream row is shown as:
#: the mask ``redact`` puts in place of URL user information.
CREDENTIAL_VALUE_MASK = '***'


def view_params(params: Any, source_type: Any = None) -> Any:
    """The ``params`` of a camera view: redacted URL, no reference.

    Two rules, for Camera_Sources of *every* type, including stream rows
    the device reported and legacy rows created before this feature
    (rtsp-rtmp-stream-cameras Reqs 5.7, 6.1):

    - ``url`` is passed through the shared redaction filter, so a stored
      URL that embeds user information is returned with the user
      information masked. ``redact`` preserves secret-free text byte for
      byte, so the overwhelming majority of rows — every URL that holds
      no secret, and every entry with no ``url`` at all — are returned
      exactly as before (Req 18.3). It also masks a
      Secret_Query_Parameter *value*, which Requirement 6.1 forbids in a
      Portal API response and which a legacy row can hold even though
      ``validate_stream_camera_body`` now rejects one on the way in.
    - ``credentialRef`` is omitted: the Credential_Reference is an
      internal pointer into the Credential_Vault, and the view reports
      the credential *state* instead (see :func:`camera_view`).

    And one rule for a stream row (``source_type`` ``RTSP`` or ``RTMP``):
    the value of every credential-like key (``STREAM_CREDENTIAL_PARAMS``)
    is masked in place, like URL user information. The body validation
    rejects those keys on the way in, but a row written before this
    feature, when the Cameras tab took any JSON, can still hold one, and
    Requirement 5.7 forbids returning a credential value. Every other
    type keeps them exactly as before (Req 18.3): a ``password`` on a
    ``Camera`` is not a Stream_Credential.

    Total over malformed storage: a non-dict ``params`` and a non-string
    ``url`` are returned untouched rather than raising.
    """
    if not isinstance(params, dict):
        return params
    view = {key: value for key, value in params.items()
            if key != PARAM_CREDENTIAL_REF}
    url = view.get(PARAM_URL)
    if isinstance(url, str) and url:
        # Imported lazily: only the stream rules need the workflow_core
        # layer, so every other camera_registry path (and the camera_sync
        # ingest path bundled beside it) stays importable without it.
        from workflow_core.stream_url import redact
        view[PARAM_URL] = redact(url)
    if is_stream_camera_type(source_type):
        for key in STREAM_CREDENTIAL_PARAMS:
            if key in view:
                view[key] = CREDENTIAL_VALUE_MASK
    return view


def version_view(version: Any) -> Any:
    """A conflict event's recorded version in API shape.

    Its ``params`` go through :func:`view_params` with the version's own
    type, so a conflict read never returns what a camera read would not:
    a version recorded from a legacy stream row can hold a credentialed
    URL or a credential-like key, and a stream version holds the
    Credential_Reference (Reqs 5.7, 6.1). Secret-free content is returned
    unchanged. A version without ``params`` (a deletion is None) is
    returned as it is.
    """
    if not isinstance(version, dict) or 'params' not in version:
        return version
    return {**version,
            'params': view_params(version['params'], version.get('type'))}


def credentials_view(params: Any) -> Dict[str, Any]:
    """The credential *state* of a stream Camera_Source (Req 5.7).

    Never a credential value: only whether credentials are configured and
    when they were last updated, read from the non-secret flags the
    Portal writes when it stores them (task 8.2) and the device echoes
    back in its report.
    """
    if not isinstance(params, dict):
        params = {}
    return {
        'configured': bool(params.get(PARAM_CREDENTIALS_CONFIGURED, False)),
        'updatedAt': params.get(PARAM_CREDENTIALS_UPDATED_AT),
    }


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
    params = item.get('params') or {}
    source_type = item.get('type')
    view = {
        'camera_source_id': item.get('camera_source_id')
                            or sk[len(SK_CAMERA_PREFIX):],
        'name': item.get('name'),
        'type': source_type,
        # Credential-free and URL-redacted (Reqs 5.7, 6.1).
        'params': view_params(params, source_type),
        'capabilities': item.get('capabilities') or {},
        'origin': item.get('origin'),
        'version': item.get('version'),
        'last_reported_at': last_reported_at,
        'sync_status': item.get('sync_status'),
        'absent': bool(item.get('absent', False)),
        'stale': stale,
    }
    # The credential state, for the stream types only: no other type has
    # Stream_Credentials, and adding the key for them would change a view
    # Requirement 18.3 keeps as it was.
    if is_stream_camera_type(source_type):
        view['credentials'] = credentials_view(params)
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
        # Redacted like a camera view (Reqs 5.7, 6.1).
        'edge_version': version_view(item.get('edge_version')),
        'portal_version': version_view(item.get('portal_version')),
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

    body = {
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
    }
    # Device_Stream_Capabilities, stored by the sync reducer from the
    # device's report (rtsp-rtmp-stream-cameras Req 16.5). Present only for
    # a device that reported them, so every other response is unchanged.
    stream_capabilities = (meta or {}).get('stream_capabilities')
    if isinstance(stream_capabilities, dict):
        body['stream_capabilities'] = stream_capabilities
    return create_response(200, body)


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


#: Every ``params`` key a stream change can carry, across both stream
#: types, including the server-managed credential keys.
STREAM_PARAM_KEYS = tuple(sorted(
    set().union(*STREAM_PARAMS_BY_TYPE.values())
    | set(STREAM_SERVER_MANAGED_PARAMS)))


def shadow_change_payload(change: Dict[str, Any]) -> Dict[str, Any]:
    """The desired change as written to the shadow.

    AWS IoT merges a desired update into the stored document field by
    field, and only an explicit ``null`` removes a field. While the device
    has not yet consumed an earlier change for the same Camera_Source, a
    key the new change leaves out would therefore survive from the old
    one. For a stream camera that could bring back a ``credentialRef``
    the new change cleared (Req 5.8). So for the stream types, every
    stream ``params`` key the change does not carry is written as
    ``null``, which removes it from the merged document; the device never
    sees the ``null``. Every other type's change is written exactly as
    before (Req 18.3).
    """
    params = change.get('params')
    if not (is_stream_camera_type(change.get('type'))
            and isinstance(params, dict)):
        return change
    tombstoned = dict(params)
    for key in STREAM_PARAM_KEYS:
        tombstoned.setdefault(key, None)
    return {**change, 'params': tombstoned}


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
                {'state': {'desired': {'changes': {
                    csid: shadow_change_payload(change)}}}},
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


def alias_rejection(csid: str, entry: Optional[Dict[str, Any]]) -> Dict:
    """Reject an update or delete of a stream create mirror: 409
    ``CAMERA_SOURCE_ALIAS`` (task 29, design component 7 "Create
    mirrors").

    Names the created camera's id when the registry linked the mirror to
    it (``alias_of``), under ``created_camera_source_id``; the link itself
    is a Portal-owned key that no response carries.
    """
    created = entry.get('alias_of') if isinstance(entry, dict) else None
    known = isinstance(created, str) and bool(created)
    body: Dict[str, Any] = {
        'error': (f"Camera source '{csid}' only mirrors a camera the device "
                  "created for a Portal create; edit or delete the created "
                  "camera" + (f" '{created}'" if known else '')
                  + " instead"),
        'code': CAMERA_SOURCE_ALIAS,
        'camera_source_id': csid,
    }
    if known:
        body['created_camera_source_id'] = created
    return create_response(409, body)


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


def _stream_field_rejection(field: str, message: str,
                            code: Optional[str] = None) -> Dict:
    """A 400 that identifies the offending field (Reqs 4.2, 5.2).

    The message names the field and, for a URL problem, what is wrong
    with it — never the offending *value*, so a rejection can never echo
    credential material back into a response or a log (Req 6.1).
    """
    body = {'error': message, 'field': field}
    if code is not None:
        body['code'] = code
    return create_response(400, body)


def _is_stream_integer(value: Any, low: int, high: int) -> bool:
    """Whether ``value`` is an integer within ``[low, high]`` inclusive.

    ``bool`` is not an integer here (``isinstance(True, int)`` is True in
    Python, and ``latencyMs: true`` is not a latency). A float or Decimal
    that is exactly integral is accepted, because a JSON body may spell
    ``200`` as ``200.0``. Total over every value a parsed body can hold:
    a non-number, and the non-integral floats ``nan`` and ``inf``, are
    rejected rather than raising.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():  # False for nan and inf
            return False
        number = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return False
        number = int(value)
    else:
        return False
    return low <= number <= high


def validate_stream_camera_body(body: Any) -> Optional[Dict]:
    """Validate a stream Camera_Source create/update body.

    Applies to the stream types only (Req 5.2 — "validation of every
    other type SHALL stay unchanged"): a body of any other type, or a
    body :func:`validate_camera_body` would already reject, returns
    ``None`` here, so callers can run this unconditionally after the
    existing check and every non-stream flow keeps exactly the validation
    it had (Req 18.3).

    For an ``RTSP`` or ``RTMP`` body it enforces, in order:

    1. No credential-like ``params`` key (Req 5.2). ``params`` is echoed
       into the desired shadow, the registry item, the pending content
       and audit events, so credential material must never enter it;
       Stream_Credentials travel in the write-only top-level
       ``credentials`` object, which task 8.2 stores in the
       Credential_Vault.
    2. No server-managed ``params`` key: the Portal writes
       ``credentialRef``, ``credentialsConfigured`` and
       ``credentialsUpdatedAt`` itself, so a request cannot forge a
       Credential_Reference or a credential state.
    3. Only the settings the type has (Requirement 4.1): transport and
       latency are RTSP only.
    4. ``url`` is a Stream_URL for the type's schemes, checked with the
       shared ``check_stream_url`` — the same function the catalog
       constraint, validator rule V11, the Deployment_Service override
       check and the device use, so the rule cannot drift between them.
       It is required: a body with no ``url`` is rejected as an invalid
       Stream_URL naming the field.
    5. The value domains of Requirement 4.1 for each supplied setting.
       A setting a body omits is left absent rather than defaulted: the
       device applies its own documented defaults.

    Returns an error response, or ``None`` when the body is acceptable.
    Keys are examined in sorted order, so the reported field is a
    deterministic function of the body rather than of dict ordering.
    """
    if not isinstance(body, dict):
        return None  # validate_camera_body owns the body shape
    source_type = body.get('type')
    allowed = (STREAM_PARAMS_BY_TYPE.get(source_type)
               if is_stream_camera_type(source_type) else None)
    if allowed is None:
        return None  # not a stream type: nothing here applies
    params = body.get('params')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return create_response(400, {'error': 'params must be an object'})

    credential_keys = {key.lower() for key in STREAM_CREDENTIAL_PARAMS}
    server_managed_keys = {key.lower() for key in STREAM_SERVER_MANAGED_PARAMS}
    for key in sorted(params):
        lowered = key.lower() if isinstance(key, str) else key
        if lowered in credential_keys:
            return _stream_field_rejection(
                f'params.{key}',
                f"params.{key} must not carry credential material; stream "
                "camera credentials are submitted in the write-only "
                "'credentials' object and are never stored in the registry")
        if lowered in server_managed_keys:
            return _stream_field_rejection(
                f'params.{key}',
                f"params.{key} is managed by the Portal and cannot be set on "
                "a request")
        if key not in allowed:
            return _stream_field_rejection(
                f'params.{key}',
                f"params.{key} is not a {source_type} camera setting; the "
                f"accepted settings are {', '.join(allowed)}")

    # The Stream_URL rules, from the shared module: imported lazily so
    # that every non-stream path stays importable without the
    # workflow_core layer.
    from workflow_core.stream_url import (
        SCHEMES_BY_SOURCE_TYPE, check_stream_url,
    )
    problem = check_stream_url(params.get(PARAM_URL),
                               SCHEMES_BY_SOURCE_TYPE[source_type])
    if problem is not None:
        return _stream_field_rejection(f'params.{PARAM_URL}',
                                       problem.message, problem.code)

    if 'transport' in params and params['transport'] not in STREAM_TRANSPORTS:
        return _stream_field_rejection(
            'params.transport',
            "params.transport must be one of {0}".format(
                ', '.join(STREAM_TRANSPORTS)))
    if 'decoder' in params and params['decoder'] not in STREAM_DECODER_POLICIES:
        return _stream_field_rejection(
            'params.decoder',
            "params.decoder must be one of {0}".format(
                ', '.join(STREAM_DECODER_POLICIES)))
    for key, (low, high) in (
            ('latencyMs', STREAM_LATENCY_MS_RANGE),
            ('maxFrameDimension', STREAM_MAX_FRAME_DIMENSION_RANGE),
            ('stallTimeoutS', STREAM_STALL_TIMEOUT_S_RANGE)):
        if key in params and not _is_stream_integer(params[key], low, high):
            return _stream_field_rejection(
                f'params.{key}',
                f"params.{key} must be an integer between {low} and {high}")
    return None


# ---------------------------------------------------------------------------
# Portal-managed Stream_Credentials (task 8.2 — Reqs 5.3, 5.4, 5.8)
# ---------------------------------------------------------------------------

def validate_stream_credentials_body(body: Any) -> Optional[Dict]:
    """Validate the write-only ``credentials`` object and the
    ``clearCredentials`` flag of a stream create/update body (Req 5.2).

    Applies to the stream types only, so every other type keeps exactly
    the validation it had (Req 18.3) and a ``credentials`` key on a
    non-stream body is ignored exactly as it was before this feature.
    Returns an error response naming the offending field — never its
    value — or ``None``.
    """
    if not isinstance(body, dict) or not is_stream_camera_type(
            body.get('type')):
        return None
    problem = stream_credentials.validate_credentials_request(body)
    if problem is None:
        return None
    field, message = problem
    return _stream_field_rejection(field, message)


def validate_stream_create_id(body: Any) -> Optional[Dict]:
    """Validate the ``camera_source_id`` a stream create's body carries
    (task 29, Requirement 5.2; design component 7 "Body validation").

    Applies to the stream types only, and only when the key is present and
    its value is not null: a missing key or ``null`` gets a generated
    ``portal-<hex>`` id. Any other value must be a string that
    :data:`STREAM_CREATE_ID_PATTERN` matches in full, must not be made only
    of dots (``.`` and ``..`` are URL dot segments), and must not be a
    ``cfg-``, ``disc-`` or ``arv-`` id, ``static-image-camera`` or
    ``static-video-camera`` (``camera_sync.is_cfg_or_discovery_managed``,
    the device's own list). So the derived secret name is one segment
    under the device's prefix, and every stream create stays outside the
    ids the device names itself. Returns a 400 naming the field, never
    echoing the value, or ``None``. Every other type keeps its validation
    and id handling (Req 18.3).
    """
    if not (isinstance(body, dict)
            and is_stream_camera_type(body.get('type'))):
        return None
    value = body.get('camera_source_id')
    if value is None:
        return None
    if not (isinstance(value, str)
            and STREAM_CREATE_ID_PATTERN.fullmatch(value)
            and set(value) != {'.'}):
        return _stream_field_rejection(
            'camera_source_id',
            "camera_source_id must be a string of 1 to 128 of the "
            "characters A-Z, a-z, 0-9 and _.@+=-, not made only of dots; "
            "omit it to get a generated id")
    if camera_sync.is_cfg_or_discovery_managed(value):
        return _stream_field_rejection(
            'camera_source_id',
            "camera_source_id must not be a cfg-, disc- or arv- id, "
            "static-image-camera or static-video-camera: the device names "
            "those cameras itself")
    return None


def credentials_usecase(usecase_id: str) -> Dict[str, Any]:
    """The Use_Case record the Credential_Vault calls are made against.

    Resolved before anything is stored: a Use_Case that cannot be read
    raises here, so a credential can never be written into the wrong
    account (the same lookup ``iot_data_client`` makes for the shadow).
    """
    usecase = get_usecase(usecase_id)
    if isinstance(usecase, dict) and not usecase.get('usecase_id'):
        usecase = {**usecase, 'usecase_id': usecase_id}
    return usecase


def carried_credential_params(existing: Optional[Dict[str, Any]]
                             ) -> Dict[str, Any]:
    """The credential bookkeeping an update that does not mention
    credentials must carry forward.

    An update replaces ``params`` wholesale, and a request may not set the
    server-managed keys itself, so an unrelated settings edit would
    otherwise silently drop a working Credential_Reference and leave the
    camera unable to authenticate. The latest portal intent wins over the
    last reported state, so two updates in a row keep the reference the
    first one delivered.

    The reported reference is carried only while the device reports
    ``credentialsConfigured: true`` (task 29, Requirement 5.8; design
    component 7 "Updates that do not mention credentials"). After a clear,
    the device's report leaves ``credentialRef`` out but never nulls it,
    so the merged shadow, and the item built from it, still hold the old
    reference while ``credentialsConfigured`` turns false; delivering it
    would make the device fetch a secret the clear scheduled for deletion.
    """
    if not isinstance(existing, dict):
        return {}

    def credential_keys(source):
        return {key: source[key]
                for key in (PARAM_CREDENTIAL_REF,
                            PARAM_CREDENTIALS_CONFIGURED,
                            PARAM_CREDENTIALS_UPDATED_AT)
                if key in source}

    pending = existing.get('pending_content')
    pending_params = pending.get('params') if isinstance(pending, dict) \
        else None
    if isinstance(pending_params, dict):
        if pending_params.get(PARAM_CREDENTIAL_REF):
            return credential_keys(pending_params)
        if pending_params.get(PARAM_CREDENTIALS_CONFIGURED) is False:
            # The latest portal intent cleared the credentials. The
            # reference the device last reported must not come back just
            # because the device has not acknowledged the clear yet.
            return {}
    reported = existing.get('params')
    if (isinstance(reported, dict) and reported.get(PARAM_CREDENTIAL_REF)
            and reported.get(PARAM_CREDENTIALS_CONFIGURED) is True):
        return credential_keys(reported)
    return {}


def prepare_stream_params(body: Any, usecase_id: str, device_id: str,
                          csid: str,
                          existing: Optional[Dict[str, Any]] = None):
    """The ``params`` a stream change delivers, plus the credential work
    the route must finish afterwards.

    Steps 2 to 4 of design component 7, run *before* the desired change is
    written:

    2. ``ensure_device_read_grant`` so the device can read what is about
       to be stored (Reqs 6.6, 6.7).
    3. ``store_stream_credentials`` writes the value to the
       Credential_Vault of the device's Use_Case_Account (Req 5.3).
    4. ``params`` gains the Credential_Reference and the non-secret
       ``credentialsConfigured`` / ``credentialsUpdatedAt`` flags.

    Returns ``(params, stored, cleared)``: ``stored`` is the vault write to
    withdraw if delivery fails (Req 5.4), and ``cleared`` marks a
    ``clearCredentials`` request whose secret is scheduled for deletion
    once the change is delivered (Req 5.8). Raises
    ``stream_credentials.CredentialStorageUnavailable`` when the
    Use_Case_Account does not grant the Portal the capability (Req 5.9);
    the routes turn that into the 409 of
    :func:`credentials_unavailable_rejection`, before anything is written.

    Which secret step 3 writes (task 29, design component 7 "Secret
    record"): a create (``existing`` is None) writes
    ``secret_name(device_id, csid)`` and resolves no account. An update
    resolves the use case's account and region first
    (``stream_credentials.secret_scope``), then the camera's secrets
    (:func:`credential_secret_ids`), and writes into the first that
    exists. When no candidate resolves, the derived name included, it
    raises ``stream_credentials.CameraIdCannotHoldCredentials`` before
    step 2, so nothing at all is written; the route turns that into a 400.

    Every non-stream body returns its own ``params`` untouched, so no
    other type's flow changes (Req 18.3).
    """
    params = dict((body or {}).get('params') or {}) \
        if isinstance(body, dict) else {}
    if not (isinstance(body, dict)
            and is_stream_camera_type(body.get('type'))):
        return params, None, False

    credentials = stream_credentials.credentials_from_body(body)
    if credentials:
        usecase = credentials_usecase(usecase_id)
        if existing is None:
            stream_credentials.ensure_device_read_grant(usecase)
            stored = stream_credentials.store_stream_credentials(
                usecase, device_id, csid, credentials, create=True)
        else:
            account_id, region = stream_credentials.secret_scope(usecase)
            secret_ids = [secret_id for secret_id, _ in credential_secret_ids(
                existing, device_id, csid, account_id, region)]
            if not secret_ids:
                raise stream_credentials.CameraIdCannotHoldCredentials()
            stream_credentials.ensure_device_read_grant(usecase)
            stored = stream_credentials.store_stream_credentials(
                usecase, device_id, csid, credentials, secret_ids,
                create=False)
        params[PARAM_CREDENTIAL_REF] = \
            stream_credentials.credential_reference(stored)
        params[PARAM_CREDENTIALS_CONFIGURED] = True
        params[PARAM_CREDENTIALS_UPDATED_AT] = now_ms()
        return params, stored, False

    if stream_credentials.clear_requested(body):
        # Delivered without a reference: the device drops the credentials
        # it holds, and the secret is scheduled for deletion afterwards.
        params[PARAM_CREDENTIALS_CONFIGURED] = False
        params[PARAM_CREDENTIALS_UPDATED_AT] = now_ms()
        return params, None, True

    carried = carried_credential_params(existing)
    if carried:
        params.update(carried)
    return params, None, False


def credentials_unavailable_rejection(
        error: 'stream_credentials.CredentialStorageUnavailable',
        csid: Optional[str] = None) -> Dict:
    """Reject a credentialed stream mutation the Use_Case_Account does not
    let the Portal carry out: 409 ``STREAM_CREDENTIALS_UNAVAILABLE``
    (Req 5.9, design component 7 "Missing permissions").

    The message names the missing capability and the remediation, and
    carries no credential material: ``CredentialStorageUnavailable`` holds
    only the capability and the AWS error *code* that denied it, never a
    value from the request (Req 6.1).

    The caller returns this *before* writing anything — no shadow desired
    change, no registry item, no audit event — so the registry and the
    shadow are left exactly as they were, and a credential-free stream
    camera (which never reaches the Credential_Vault) is still accepted.
    """
    capability = getattr(error, 'capability', None) \
        or 'permission to manage stream camera credentials'
    body: Dict[str, Any] = {
        'error': f"The use-case account does not grant the Portal "
                 f"{capability}; {STREAM_CREDENTIALS_REMEDIATION}",
        'code': STREAM_CREDENTIALS_UNAVAILABLE,
        'capability': capability,
    }
    if csid is not None:
        body['camera_source_id'] = csid
    logger.warning(
        f"Rejected a credentialed stream camera mutation: the use-case "
        f"account does not grant the Portal {capability}")
    return create_response(409, body)


def entry_is_stream_camera(entry: Optional[Dict[str, Any]]) -> bool:
    """Whether a stored registry entry is a stream Camera_Source, from its
    reported type or, for an entry the device has not reported yet, from
    the pending portal content."""
    if not isinstance(entry, dict):
        return False
    if is_stream_camera_type(entry.get('type')):
        return True
    pending = entry.get('pending_content')
    return isinstance(pending, dict) and is_stream_camera_type(
        pending.get('type'))


def is_create_mirror(entry: Optional[Dict[str, Any]], csid: Any) -> bool:
    """Whether a registry entry is a stream create mirror (task 29, design
    component 7 "Create mirrors").

    For one report, the device mirrors a Portal create under the Portal's
    id onto the camera it created, and the next report retires the mirror.
    A stream entry is a mirror when the reducer linked it (``alias_of``),
    or when it is ``synced`` under an id that is not a ``cfg-`` id: the
    device stores every create under ``cfg-<imageSourceId>``, and a Portal
    create that owns a secret is ``pending`` or ``failed``. The shape rule
    also covers a mirror a replayed documents event brought back without
    ``alias_of``. A mirror owns no secret. Entries of other types are
    never mirrors here (Req 18.3).
    """
    if not entry_is_stream_camera(entry):
        return False
    if entry.get('alias_of'):
        return True
    return (entry.get('sync_status') == camera_sync.SYNC_STATUS_SYNCED
            and not (isinstance(csid, str)
                     and csid.startswith(CONFIGURED_ID_PREFIX)))


#: Stands for a ``credentialRef`` that is present but not an object:
#: ``device_secret_id`` rejects it, whatever the value is.
_MALFORMED_REFERENCE = object()


def _reference_arn(source: Any) -> Any:
    """The ``credentialRef.secretArn`` a ``params`` dict carries: the value
    when present, None when absent (no ``params``, no ``credentialRef``, or
    a reference without ``secretArn``). A ``credentialRef`` that is present
    but not an object gives :data:`_MALFORMED_REFERENCE`, so the caller
    rejects it."""
    if not isinstance(source, dict):
        return None
    reference = source.get(PARAM_CREDENTIAL_REF)
    if reference is None:
        return None
    if not isinstance(reference, dict):
        return _MALFORMED_REFERENCE
    return reference.get('secretArn')


def credential_secret_ids(entry: Optional[Dict[str, Any]], device_id: str,
                          csid: str, account_id: Optional[str],
                          region: Optional[str]) -> List[tuple]:
    """A camera's Credential_Vault secrets, as ``(secret_id, name)`` pairs
    in resolution order (task 29, Requirement 5.8; design component 7
    "Secret record"):

    1. the record, ``credential_secret_arn``;
    2. ``pending_content.params.credentialRef.secretArn``, the reference
       the pending change delivers;
    3. ``params.credentialRef.secretArn``, the reference the device last
       reported (or, before its first report, the one the Portal stored);
    4. ``secret_name(device_id, csid)``.

    Each passes ``stream_credentials.device_secret_id``: candidates 1-3
    only as complete ARNs of ``account_id`` and ``region``, and every name
    only as the device's prefix plus one segment. Duplicates are dropped
    by SecretId, in candidate order (fifth design review, N3), so an ARN
    and the derived name of the same secret both stay. A candidate that is
    present and rejected is logged at WARNING with the camera id and its
    position, never its value; an absent candidate (or a reference without
    ``secretArn``) and a duplicate are silent.
    """
    entry = entry if isinstance(entry, dict) else {}
    pending = entry.get('pending_content')
    pending_params = pending.get('params') if isinstance(pending, dict) \
        else None
    candidates = (
        ('record', entry.get('credential_secret_arn'), False),
        ('pending', _reference_arn(pending_params), False),
        ('reported', _reference_arn(entry.get('params')), False),
        ('name', stream_credentials.secret_name(device_id, csid), True),
    )
    pairs: List[tuple] = []
    seen = set()
    for position, candidate, derived in candidates:
        if candidate is None:
            continue
        secret_id = stream_credentials.device_secret_id(
            candidate, device_id, account_id, region, derived=derived)
        if secret_id is None:
            logger.warning(
                f"Ignored the {position} secret candidate of camera source "
                f"{csid!r}: it is not a secret of this device in the use "
                "case's account and region")
            continue
        if secret_id in seen:
            continue
        seen.add(secret_id)
        pairs.append((secret_id, stream_credentials.secret_id_name(
            secret_id)))
    return pairs


def referenced_secret_names(items: List[Dict[str, Any]], device_id: str,
                            exclude_csid: str, account_id: Optional[str],
                            region: Optional[str]) -> set:
    """The secret names the device's other stream entries resolve through
    any of their four candidates (task 29, design component 7 "Clear and
    delete"). Create mirrors, linked or recognized by shape, and entries
    pending a delete do not count as users. Names, not SecretIds, so a
    secret one entry names by ARN and another by name counts once."""
    names = set()
    for item in items or []:
        sk = item.get('sk') or ''
        if not sk.startswith(SK_CAMERA_PREFIX):
            continue
        other = sk[len(SK_CAMERA_PREFIX):]
        if other == exclude_csid or not entry_is_stream_camera(item):
            continue
        if is_create_mirror(item, other):
            continue
        pending = item.get('pending_content')
        if (item.get('sync_status') == camera_sync.SYNC_STATUS_PENDING
                and isinstance(pending, dict)
                and pending.get('op') == 'delete'):
            continue
        names.update(name for _, name in credential_secret_ids(
            item, device_id, other, account_id, region))
    return names


def schedule_credential_deletion(usecase_id: str, device_id: str,
                                 csid: str, entry: Optional[Dict[str, Any]],
                                 items: List[Dict[str, Any]]) -> None:
    """Schedule the camera's Credential_Vault secrets for deletion
    (Req 5.8), after the change of a clearing update or of a delete has
    been delivered (task 29, design component 7 "Clear and delete").

    The camera's resolved secrets (:func:`credential_secret_ids`), minus
    the names another stream entry of the device still resolves
    (:func:`referenced_secret_names`), each skip logged at INFO. When
    nothing is left, ``schedule_secret_deletion`` is not called at all, so
    a secret another camera uses is never scheduled, by name included.

    Best-effort and never raising: the change is already written, so a
    vault failure must not turn a delivered clear or delete into an error
    response. When the use case's account cannot be resolved, nothing is
    scheduled, with a WARNING.
    """
    try:
        usecase = credentials_usecase(usecase_id)
        account_id, region = stream_credentials.secret_scope(usecase)
    except Exception as e:  # noqa: BLE001 — the change is already delivered
        logger.warning(
            f"Could not resolve the use case's account for "
            f"{device_id}/{csid}, so no Credential_Vault secret is "
            f"scheduled for deletion: {e}")
        return
    try:
        in_use = referenced_secret_names(items, device_id, csid,
                                         account_id, region)
        secret_ids = []
        for secret_id, name in credential_secret_ids(
                entry, device_id, csid, account_id, region):
            if name in in_use:
                logger.info(
                    f"Kept Credential_Vault secret {name}: another camera "
                    f"of {device_id} still uses it")
                continue
            secret_ids.append(secret_id)
        if not secret_ids:
            return
        stream_credentials.schedule_secret_deletion(
            usecase, device_id, csid, secret_ids=secret_ids)
    except Exception as e:  # noqa: BLE001 — the change is already delivered
        logger.warning(
            f"Could not schedule credential deletion for "
            f"{device_id}/{csid}: {e}")


def camera_id_cannot_hold_credentials_rejection(csid: str) -> Dict:
    """400 for a credentialed update of a camera whose id cannot name a
    secret and that has none (task 29, design component 7 "Update with
    credentials"). Nothing is written, except at most the idempotent
    device read grant."""
    logger.warning(
        f"Rejected a credentialed update of camera source {csid!r}: "
        f"{stream_credentials.CameraIdCannotHoldCredentials.MESSAGE}")
    return _stream_field_rejection(
        'camera_source_id',
        stream_credentials.CameraIdCannotHoldCredentials.MESSAGE)


def credential_audit_details(stored: Optional[Dict[str, Any]],
                             cleared: bool) -> Optional[Dict[str, Any]]:
    """Audit details for a mutation that touched credentials (Req 5.3).

    Never credential material: only whether the request configured or
    cleared them. A mutation that did not mention credentials — every
    non-stream mutation, and every stream mutation without them — adds
    nothing, so its audit event stays exactly as it was (Req 18.3).
    """
    if stored is not None:
        return {'credentials_configured': True}
    if cleared:
        return {'credentials_configured': False}
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
                 body: Optional[Dict[str, Any]] = None,
                 credential_secret_arn: Optional[str] = None) -> None:
    """Upsert the registry entry into sync_status=pending (Req 5.1).

    Existing entries keep their last-reported edge state as the effective
    content (the portal version travels in pending_content until the
    device acknowledges); newly created entries carry the portal content
    directly so they are visible in the cameras view while pending.

    ``credential_secret_arn`` (task 29, Requirement 5.8): the ARN of the
    secret this change's credentials were stored in, recorded on a new
    and on a copied item when given. Without it, a copied item keeps its
    record, so a credential-free update, a clear and a delete keep it.
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
    if credential_secret_arn is not None:
        item['credential_secret_arn'] = credential_secret_arn
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
    error = validate_stream_camera_body(body)
    if error:
        return error
    error = validate_stream_credentials_body(body)
    if error:
        return error
    # A stream create's body id (task 29, Req 5.2): a missing key or null
    # gets a generated id below, and anything else must pass the check.
    error = validate_stream_create_id(body)
    if error:
        return error

    csid = body.get('camera_source_id') or f"portal-{uuid.uuid4().hex[:12]}"
    if find_camera_item(items, csid) is not None:
        return create_response(409, {
            'error': f"Camera source '{csid}' already exists",
        })

    # Steps 2-4: the device read grant, the Credential_Vault write, and
    # the Credential_Reference in params (task 8.2). A Use_Case_Account
    # that does not grant the Portal those capabilities is a 409 here,
    # before the shadow, the registry, or the audit log is touched
    # (Req 5.9, task 8.3).
    try:
        params, stored, cleared = prepare_stream_params(
            body, usecase_id, device_id, csid, existing=None)
    except stream_credentials.CredentialStorageUnavailable as e:
        return credentials_unavailable_rejection(e, csid)

    portal_change_id = new_change_id()
    change = {
        'op': 'create',
        'portalChangeId': portal_change_id,
        'name': body['name'],
        'type': body['type'],
        'params': params,
    }
    # Shadow FIRST; a failure returns 502 with the registry untouched and
    # the stored credential version withdrawn (Req 5.4).
    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        if stored is not None:
            stream_credentials.withdraw_stream_credentials(
                credentials_usecase(usecase_id), stored)
        return error

    pending_content = {'op': 'create', 'name': body['name'],
                       'type': body['type'],
                       'params': params}
    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 pending_content, existing=None,
                 body={**body, 'params': params},
                 credential_secret_arn=(stored['secretArn']
                                        if stored is not None else None))
    # A create owns no secret yet, so a create with clearCredentials
    # schedules nothing (task 29, Req 5.8).
    audit_mutation(user, 'create_camera_source', device_id, csid,
                   usecase_id, portal_change_id,
                   credential_audit_details(stored, cleared))
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
    if is_create_mirror(entry, csid):
        # A stream create mirror owns no secret and is not the camera to
        # change; nothing is written (task 29).
        return alias_rejection(csid, entry)
    error = validate_camera_body(body)
    if error:
        return error
    error = validate_stream_camera_body(body)
    if error:
        return error
    error = validate_stream_credentials_body(body)
    if error:
        return error

    # Steps 2-4: an update with credentials writes a new secret version
    # into the camera's own secret and delivers the new reference
    # (Req 5.8); one that mentions neither credentials nor clearCredentials
    # carries the reference it already delivered forward. A
    # Use_Case_Account that does not grant the Portal the credential
    # capabilities is a 409 with the entry, the shadow, and the audit log
    # untouched (Req 5.9, task 8.3), and a camera whose id cannot name a
    # secret, and that has none, is a 400 (task 29).
    try:
        params, stored, cleared = prepare_stream_params(
            body, usecase_id, device_id, csid, existing=entry)
    except stream_credentials.CredentialStorageUnavailable as e:
        return credentials_unavailable_rejection(e, csid)
    except stream_credentials.CameraIdCannotHoldCredentials:
        return camera_id_cannot_hold_credentials_rejection(csid)

    portal_change_id = new_change_id()
    change = {
        'op': 'update',
        'portalChangeId': portal_change_id,
        'baseVersion': entry.get('version'),
        'name': body['name'],
        'type': body['type'],
        'params': params,
    }
    error = write_desired_change(usecase_id, device_id, csid, change)
    if error:
        if stored is not None:
            stream_credentials.withdraw_stream_credentials(
                credentials_usecase(usecase_id), stored)
        return error

    pending_content = {'op': 'update', 'name': body['name'],
                       'type': body['type'],
                       'params': params}
    mark_pending(device_id, usecase_id, csid, portal_change_id,
                 pending_content, existing=entry,
                 credential_secret_arn=(stored['secretArn']
                                        if stored is not None else None))
    audit_mutation(user, 'update_camera_source', device_id, csid,
                   usecase_id, portal_change_id,
                   credential_audit_details(stored, cleared))
    if cleared:
        schedule_credential_deletion(usecase_id, device_id, csid, entry,
                                     items)
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
    if is_create_mirror(entry, csid):
        # Deleting a stream create mirror would leave the created camera
        # and schedule nothing; the operator deletes the created camera
        # (task 29). A mirror of another type keeps its delete (Req 18.3).
        return alias_rejection(csid, entry)

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
    # Req 5.8: the camera's secrets are scheduled for deletion only after
    # the delete change has been delivered, so a delivery failure never
    # destroys credentials the device is still using, and only those no
    # other camera of the device still uses (task 29). A stream camera that
    # never had credentials has no secret, which is not an error.
    if entry_is_stream_camera(entry):
        schedule_credential_deletion(usecase_id, device_id, csid, entry,
                                     items)
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
    if (entry is not None and op in ('update', 'delete')
            and is_create_mirror(entry, csid)):
        # Never re-issue an update or delete to a stream create mirror's
        # id, such as a ConflictEvent recorded in the alias race (task 29).
        return alias_rejection(csid, entry)
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
