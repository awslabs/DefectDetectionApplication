"""
Workflow debug API (workflow-debug-mode design §7.2): the ``/debug/*`` routes.

| Method | Path                                                  | Permission        |
|--------|-------------------------------------------------------|-------------------|
| POST   | /debug/usecases/{usecase_id}/sessions                 | MANAGE_DEVICES    |
| GET    | /debug/usecases/{usecase_id}/sessions?state=&next=    | VIEW_DEVICE_LOGS  |
| GET    | /debug/usecases/{usecase_id}/log?device_id=&workflow_id=&from=&to=&next=&limit=&execution_id=&node_id=&level=&type=&ts_from=&ts_to= | VIEW_DEVICE_LOGS |
| GET    | /debug/sessions/{session_id}                          | VIEW_DEVICE_LOGS (the stored use case) |
| DELETE | /debug/sessions/{session_id}                          | MANAGE_DEVICES    |
| GET    | /debug/sessions/{session_id}/images/{execution_id}?device_id= | VIEW_DEVICE_LOGS and the registry-enforcement guard |
| GET    | /debug/sessions/{session_id}/console-link?device_id=  | PortalAdmin       |

Handler order on every route: parse -> resolve the use case (from the path,
or from the stored session) -> ``rbac_manager.has_permission`` (403
``INSUFFICIENT_PERMISSIONS`` with an ``unauthorized_access`` audit row; 500
``registry_unavailable`` when the registry cannot be read) -> the work. Every
refusal is ``{error, error_code, remedy?}`` plus the detail keys the design
names (``limit``, ``active``, ``session_id``, ``retry_after_ms``,
``no_deployment``, ``no_device_record``).
"""
import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import shared_utils
from shared_utils import (
    Permission,
    RegistryUnavailable,
    caller_is_portal_admin,
    create_response,
    get_user_from_event,
    log_audit_event,
    rbac_manager,
    registry_enforcement_enabled,
)
from edge_telemetry_common import devices as device_records
from workflow_debug import console_link, log_filters, log_reads, logs_writer, media, sessions, shadow, targets

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- deployment values
DEFAULTS = {
    "DEBUG_LOG_RETENTION_DAYS": 14,
    "DEBUG_MAX_SESSIONS_PER_USECASE": 4,
    "DEBUG_MAX_DEVICES_PER_SESSION": 20,
    "DEBUG_MAX_MESSAGES_PER_SECOND": 10,
    "DEBUG_MAX_BYTES_PER_SECOND": 262144,
}
DURATION_RANGE = (5, 1440)
DEFAULT_DURATION_MINUTES = 60
IMAGE_MAX_KB_RANGE = (50, 300)
DEFAULT_IMAGE_MAX_KB = 150
LEVELS = ("info", "debug")
DEFAULT_LEVEL = "debug"
LOG_WINDOW_DEFAULT_MS = 15 * 60 * 1000
LOG_WINDOW_MAX_MS = 24 * 60 * 60 * 1000
LOG_LIMIT_RANGE = (1, 10000)
LOG_LIMIT_DEFAULT = 2000
LOG_CACHE_SECONDS = 2.0
LOG_VIEW_AUDIT_SECONDS = 3600
REPORTED_CACHE_SECONDS = 10.0
PAGE_SIZE = 50

_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_NODE_RE = re.compile(r"^.{1,128}$", re.DOTALL)
_THING_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")

DEVICE_RECORD_REMEDY = (
    "A UseCaseAdmin or PortalAdmin sets the device's Target architecture on its device page "
    "(PUT /devices/{id} with {usecase_id, target_architecture}), or re-run quick setup; this needs "
    "node_designer:manage, which Operators do not hold."
)
ENFORCEMENT_REMEDY = (
    "Turn on registry enforcement (PORTAL_REGISTRY_ENFORCED) once the registry backfill is complete."
)
IMAGES_REMEDY = (
    "Apply the DDATelemetryDebugImages lifecycle rule to the use case's results bucket with "
    "edge-cv-portal/scripts/telemetry_lifecycle.py --bucket <name> --apply (operator credentials)."
)
SESSION_LIMIT_REMEDY = "Stop a running debug session, or wait for one to expire."
DEVICES_PER_SESSION_REMEDY = "Start the session on one device, or raise debugMaxDevicesPerSession (at most 32)."
SINK_REMEDY = "The API role could not create the CloudWatch Logs group; check the deployment's logs grants."


def _value(name: str) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]


# --------------------------------------------------------------------------- clients
_lock = threading.Lock()
_logs_client = None
_dynamodb_resource = None
_dynamodb_client = None


def _logs():
    global _logs_client
    with _lock:
        if _logs_client is None:
            _logs_client = boto3.client("logs")
        return _logs_client


def _dynamodb():
    global _dynamodb_resource
    with _lock:
        if _dynamodb_resource is None:
            _dynamodb_resource = boto3.resource("dynamodb")
        return _dynamodb_resource


def _table():
    return _dynamodb().Table(os.environ["WORKFLOW_DEBUG_SESSIONS_TABLE"])


def _table_name() -> str:
    return os.environ["WORKFLOW_DEBUG_SESSIONS_TABLE"]


def _transact_client():
    """A low-level client for ``TransactWriteItems``: the resource's client
    carries boto3's attribute-value transformers, which would serialize the
    already typed items a second time."""
    global _dynamodb_client
    with _lock:
        if _dynamodb_client is None:
            _dynamodb_client = boto3.client("dynamodb")
        return _dynamodb_client


# --------------------------------------------------------------------------- per-container caches
_log_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_log_view_audits: Dict[Tuple[str, str], float] = {}
_reported_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}


def reset_caches() -> None:
    """Forget the per-container caches and clients (tests)."""
    global _logs_client, _dynamodb_resource, _dynamodb_client
    with _lock:
        _log_cache.clear()
        _log_view_audits.clear()
        _reported_cache.clear()
        _logs_client = None
        _dynamodb_resource = None
        _dynamodb_client = None
    device_records.clear_cache()


# --------------------------------------------------------------------------- answers
def _error(status: int, code: str, message: str, remedy: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"error": message, "error_code": code}
    if remedy:
        body["remedy"] = remedy
    body.update(extra)
    return create_response(status, body)


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str, remedy: Optional[str] = None, **extra: Any) -> None:
        super().__init__(message)
        self.response = _error(status, code, message, remedy, **extra)


# --------------------------------------------------------------------------- authorization
def _authorize(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str,
               permission: Permission) -> Optional[Dict[str, Any]]:
    """None when allowed; the 403 or 500 answer otherwise."""
    try:
        allowed = rbac_manager.has_permission(user["user_id"], usecase_id, permission, user_info=user)
    except RegistryUnavailable:
        logger.error("Portal_Identity registry unavailable during a debug route authorization")
        return _error(500, "registry_unavailable", "The Portal identity registry could not be read")
    if allowed:
        return None
    log_audit_event(
        user_id=user["user_id"], action="unauthorized_access", resource_type="debug_session",
        resource_id=event.get("resource") or event.get("path") or "unknown", result="denied",
        details={"required_permission": permission.value, "usecase_id": usecase_id,
                 "method": event.get("httpMethod"), "path": event.get("path"),
                 "user_role": user.get("role", "unknown")},
        event=event)
    return _error(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions",
                  required_permission=permission.value)


def _authorize_portal_admin(event: Dict[str, Any], user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        allowed = caller_is_portal_admin(user)
    except RegistryUnavailable:
        logger.error("Portal_Identity registry unavailable during the console-link authorization")
        return _error(500, "registry_unavailable", "The Portal identity registry could not be read")
    if allowed:
        return None
    log_audit_event(
        user_id=user["user_id"], action="unauthorized_access", resource_type="debug_session",
        resource_id=event.get("resource") or "unknown", result="denied",
        details={"required_role": "PortalAdmin", "method": event.get("httpMethod"), "path": event.get("path"),
                 "user_role": user.get("role", "unknown")},
        event=event)
    return _error(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions", required_role="PortalAdmin")


def _usecase_or_404(usecase_id: str) -> Dict[str, Any]:
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        raise Refusal(404, "usecase_not_found", "Use case not found")


# --------------------------------------------------------------------------- stored sessions
def _load_session(session_id: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(META, [PAIR#...])`` of a stored session, or ``(None, [])``."""
    if not isinstance(session_id, str) or not _ID_RE.match(session_id):
        return None, []
    response = _table().query(KeyConditionExpression=Key("pk").eq(sessions.session_pk(session_id)))
    meta = None
    pairs = []
    for item in response.get("Items", []) or []:
        if item.get("sk") == sessions.META_SK:
            meta = item
        elif str(item.get("sk", "")).startswith("PAIR#"):
            pairs.append(item)
    pairs.sort(key=lambda p: str(p.get("device_id") or p.get("sk")))
    return meta, pairs


def _pair_for(usecase_id: str, device_id: str, workflow_id: str) -> Optional[Dict[str, Any]]:
    """The newest ``PAIR#`` item of the Pair in any state (within the 30-day TTL)."""
    response = _table().query(
        IndexName="by-pair",
        KeyConditionExpression=Key("gsi_pair").eq(sessions.by_pair_pk(usecase_id, device_id, workflow_id)),
        ScanIndexForward=False, Limit=1)
    items = response.get("Items", []) or []
    return items[0] if items else None


def _session_view(meta: Dict[str, Any], pairs: List[Dict[str, Any]],
                  reported: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    view = {
        "session_id": meta.get("session_id"), "usecase_id": meta.get("usecase_id"),
        "workflow_id": meta.get("workflow_id"), "workflow_version": meta.get("workflow_version"),
        "device_ids": list(meta.get("device_ids") or []), "all_devices": bool(meta.get("all_devices")),
        "state": meta.get("state"), "enabledAt": meta.get("enabledAt"), "expiresAt": meta.get("expiresAt"),
        "stoppedAt": meta.get("stoppedAt"), "actor": meta.get("actor"), "options": meta.get("options") or {},
        "counters": meta.get("counters") or {}, "logGroup": meta.get("logGroup"),
        "media": meta.get("media") or {},
        "devices": [],
    }
    for pair in pairs:
        device_id = str(pair.get("device_id") or sessions.device_of_pair_sk(pair.get("sk")))
        row = {
            "device_id": device_id, "state": pair.get("state"), "shadow": pair.get("shadow") or {},
            "logStream": pair.get("logStream"),
            "reported": (reported or {}).get(device_id, pair.get("reported")),
        }
        view["devices"].append(row)
    return view


# --------------------------------------------------------------------------- POST sessions
def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        raise Refusal(400, "invalid_session", "The request body is not a JSON object", field="body")
    if not isinstance(body, dict):
        raise Refusal(400, "invalid_session", "The request body is not a JSON object", field="body")
    return body


def _int_in_range(body: Dict[str, Any], field: str, bounds: Tuple[int, int], default: int) -> int:
    value = body.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refusal(400, "invalid_session", "{0} must be an integer".format(field), field=field)
    if not bounds[0] <= value <= bounds[1]:
        raise Refusal(400, "invalid_session", "{0} must be between {1} and {2}".format(field, *bounds), field=field)
    return value


def _validate_creation(body: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    workflow_id = body.get("workflow_id")
    if not isinstance(workflow_id, str) or not _ID_RE.match(workflow_id):
        raise Refusal(400, "invalid_session", "workflow_id is required", field="workflow_id")
    workflow = _dynamodb().Table(os.environ["WORKFLOWS_TABLE"]).get_item(Key={"workflow_id": workflow_id}).get("Item")
    if not workflow or str(workflow.get("usecase_id") or "") != usecase_id:
        raise Refusal(400, "invalid_session", "workflow_id is not a workflow of this use case", field="workflow_id")
    device_id = body.get("device_id")
    all_devices = body.get("all_devices", False)
    if all_devices not in (True, False):
        raise Refusal(400, "invalid_session", "all_devices must be a boolean", field="all_devices")
    if (device_id is None) == (not all_devices):
        raise Refusal(400, "invalid_session", "exactly one of device_id and all_devices: true is required",
                      field="device_id")
    if device_id is not None and (not isinstance(device_id, str) or not _THING_RE.match(device_id)):
        raise Refusal(400, "invalid_session", "device_id must be a thing name", field="device_id")
    duration = _int_in_range(body, "duration_minutes", DURATION_RANGE, DEFAULT_DURATION_MINUTES)
    level = body.get("level", DEFAULT_LEVEL)
    if level not in LEVELS:
        raise Refusal(400, "invalid_session", "level must be info or debug", field="level")
    images = body.get("images", False)
    if images not in (True, False):
        raise Refusal(400, "invalid_session", "images must be a boolean", field="images")
    image_max_kb = _int_in_range(body, "image_max_kb", IMAGE_MAX_KB_RANGE, DEFAULT_IMAGE_MAX_KB)
    return {
        "workflow_id": workflow_id,
        "workflow_version": str(workflow.get("current_version") or workflow.get("version") or "") or None,
        "device_id": device_id, "all_devices": bool(all_devices), "duration_minutes": duration,
        "level": level, "images": bool(images), "image_max_kb": image_max_kb,
    }


def _resolve_targets(usecase: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    recorded = device_records.usecase_devices(usecase)
    if spec["device_id"] is not None:
        if spec["device_id"] not in recorded:
            raise Refusal(400, "device_not_recorded",
                          "The device has no record naming this use case", DEVICE_RECORD_REMEDY,
                          device_id=spec["device_id"])
        return [spec["device_id"]]
    deployments_table_name = os.environ.get("DEPLOYMENTS_TABLE")
    if not deployments_table_name:
        raise Refusal(500, "configuration_error", "DEPLOYMENTS_TABLE is not configured", key="DEPLOYMENTS_TABLE")
    registered = targets.registered_devices(_dynamodb().Table(deployments_table_name),
                                            str(usecase["usecase_id"]), spec["workflow_id"])
    result = targets.target_set(registered, recorded, max_devices=_value("DEBUG_MAX_DEVICES_PER_SESSION"))
    if not result.devices:
        raise Refusal(400, "no_target_devices", "No recorded device runs this workflow",
                      DEVICE_RECORD_REMEDY if result.no_device_record else None,
                      no_deployment=result.no_deployment, no_device_record=result.no_device_record)
    if result.over_cap:
        raise Refusal(409, "debug_session_limit", "Too many devices for one session", DEVICES_PER_SESSION_REMEDY,
                      limit="devices_per_session", active=len(result.devices),
                      max=_value("DEBUG_MAX_DEVICES_PER_SESSION"))
    return result.devices


def _images_guard(usecase: Dict[str, Any], bucket: str) -> None:
    if not registry_enforcement_enabled():
        raise Refusal(409, "registry_enforcement_required",
                      "Debug images are refused while registry enforcement is off", ENFORCEMENT_REMEDY)
    readiness = media.images_ready(usecase, bucket)
    if not readiness.ready:
        raise Refusal(409, "debug_images_unavailable",
                      "The results bucket has no DDATelemetryDebugImages lifecycle rule", IMAGES_REMEDY,
                      reason=readiness.reason)


def _ensure_group(usecase_id: str, group: str) -> None:
    try:
        logs_writer.ensure_group(_logs(), group, _value("DEBUG_LOG_RETENTION_DAYS"), usecase_id)
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        logger.error("debug log group %s could not be prepared: %s", group, code)
        raise Refusal(502, "debug_sink_unavailable", "The debug log group could not be created", SINK_REMEDY,
                      reason=code)


def create_session(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    body = _parse_body(event)
    usecase = _usecase_or_404(usecase_id)
    spec = _validate_creation(body, usecase_id)
    devices = _resolve_targets(usecase, spec)
    bucket = media.results_bucket(usecase)
    if spec["images"]:
        _images_guard(usecase, bucket)
    probe = media.probe_region(usecase, bucket)
    group = logs_writer.log_group_name(usecase_id)
    _ensure_group(usecase_id, group)
    now_ms = int(time.time() * 1000)
    session_id = str(uuid.uuid4())
    enabled_at = now_ms
    expires_at = now_ms + spec["duration_minutes"] * 60 * 1000
    options = {"level": spec["level"], "images": spec["images"], "imageMaxKb": spec["image_max_kb"],
               "maxMessagesPerSecond": _value("DEBUG_MAX_MESSAGES_PER_SECOND"),
               "maxBytesPerSecond": _value("DEBUG_MAX_BYTES_PER_SECOND"),
               "durationMinutes": spec["duration_minutes"]}
    media_record = {"bucket": bucket, "region": probe.region, "region_source": probe.region_source}
    actions = sessions.creation_actions(
        _table_name(), session_id=session_id, usecase_id=usecase_id, workflow_id=spec["workflow_id"],
        workflow_version=spec["workflow_version"], devices=devices, all_devices=spec["all_devices"],
        enabled_at=enabled_at, expires_at=expires_at, actor=user.get("email") or user["user_id"],
        options=options, log_group=group, max_sessions_per_usecase=_value("DEBUG_MAX_SESSIONS_PER_USECASE"),
        media=media_record)
    try:
        _transact_client().transact_write_items(TransactItems=sessions.serialize_actions(actions))
    except ClientError as error:
        if str((error.response.get("Error") or {}).get("Code") or "") != "TransactionCanceledException":
            raise
        reasons = error.response.get("CancellationReasons") or []
        logger.info("debug session creation refused for use case %s: cancellation codes %s",
                    usecase_id, [r.get("Code") for r in reasons if isinstance(r, dict)])
        code, detail = sessions.cancellation_to_error(reasons, actions, devices)
        if code == "debug_session_exists":
            raise Refusal(409, code, "A debug session already covers this device and workflow",
                          SESSION_LIMIT_REMEDY, **detail)
        limit = detail.get("limit")
        maximum = _value("DEBUG_MAX_SESSIONS_PER_USECASE") if limit == "usecase" else sessions.DEVICE_SESSION_CAP
        raise Refusal(409, code, "The debug session cap is reached", SESSION_LIMIT_REMEDY, max=maximum, **detail)
    # The shadow writes, one entry per device (the prefix names the device).
    items = []
    for device_id in devices:
        entry = shadow.session_entry(
            session_id=session_id, usecase_id=usecase_id, device_id=device_id, workflow_id=spec["workflow_id"],
            enabled_at=enabled_at, expires_at=expires_at, level=spec["level"], images=spec["images"],
            image_max_kb=spec["image_max_kb"], bucket=bucket, region=probe.region,
            max_messages_per_second=options["maxMessagesPerSecond"],
            max_bytes_per_second=options["maxBytesPerSecond"])
        items.append((device_id, spec["workflow_id"], entry))
    results = shadow.write_all_sessions(usecase, items)
    activate = all(r.get("result") == shadow.RESULT_OK for r in results.values()) and len(results) == len(devices)
    try:
        _transact_client().transact_write_items(TransactItems=sessions.serialize_actions(
            sessions.shadow_result_actions(_table_name(), session_id, results, activate=activate)))
    except ClientError as error:
        logger.warning("shadow results not recorded for session %s: %s", session_id,
                       (error.response.get("Error") or {}).get("Code"))
        activate = False
    state = sessions.ACTIVE if activate else sessions.PENDING
    device_rows = []
    for device_id in devices:
        result = results.get(device_id) or {"result": shadow.RESULT_FAILED, "error": "unknown"}
        row = {"device_id": device_id, "shadow": result.get("result")}
        if result.get("error"):
            row["error"] = result["error"]
        device_rows.append(row)
    log_audit_event(
        user_id=user["user_id"], action="debug_session_started", resource_type="debug_session",
        resource_id=session_id, result="success",
        details={"usecase_id": usecase_id, "workflow_id": spec["workflow_id"], "devices": devices,
                 "all_devices": spec["all_devices"], "duration_minutes": spec["duration_minutes"],
                 "level": spec["level"], "images": spec["images"], "state": state},
        event=event)
    answer: Dict[str, Any] = {"session_id": session_id, "state": state, "devices": device_rows,
                              "region_source": probe.region_source, "expiresAt": expires_at,
                              "enabledAt": enabled_at, "logGroup": group}
    if probe.region_source == "usecase":
        answer["warning"] = ("The results bucket's region could not be read from S3; the use case's region "
                             "was written to the device instead")
    return create_response(201, answer)


# --------------------------------------------------------------------------- GET sessions
def _decode_page_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError):
        raise Refusal(400, "invalid_token", "The page token is not valid")


def _encode_page_token(key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key, default=str).encode("utf-8")).decode("ascii")


def list_sessions(event: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    wanted_state = params.get("state")
    if wanted_state is not None and wanted_state not in (sessions.PENDING, sessions.ACTIVE, sessions.STOPPED, sessions.EXPIRED):
        raise Refusal(400, "invalid_query", "state must be pending, active, stopped or expired", field="state")
    kwargs: Dict[str, Any] = {
        "IndexName": "by-usecase",
        "KeyConditionExpression": Key("gsi_usecase").eq(sessions.by_usecase_pk(usecase_id)),
        "ScanIndexForward": False,
        "Limit": PAGE_SIZE,
    }
    start = _decode_page_token(params.get("next"))
    if start:
        kwargs["ExclusiveStartKey"] = start
    response = _table().query(**kwargs)
    views = []
    for meta in response.get("Items", []) or []:
        if wanted_state and meta.get("state") != wanted_state:
            continue
        _, pairs = _load_session(str(meta.get("session_id")))
        views.append(_session_view(meta, pairs))
    return create_response(200, {"sessions": views, "next": _encode_page_token(response.get("LastEvaluatedKey"))})


# --------------------------------------------------------------------------- GET / DELETE session
def _reported_for(usecase: Dict[str, Any], meta: Dict[str, Any], pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    workflow_id = str(meta.get("workflow_id"))
    reported: Dict[str, Any] = {}
    client = None
    for pair in pairs:
        device_id = str(pair.get("device_id") or sessions.device_of_pair_sk(pair.get("sk")))
        key = (device_id, workflow_id)
        with _lock:
            cached = _reported_cache.get(key)
        if cached is not None and cached[0] > now:
            reported[device_id] = cached[1]
            continue
        try:
            if client is None:
                client = shadow.credentials.client("iot-data", usecase, "debug-control")
            value = shadow.read_reported(usecase, device_id, workflow_id, client=client)
        except Exception as error:  # noqa: BLE001 - a shadow read never fails the page
            logger.warning("reported state unavailable for %s: %s", device_id, error.__class__.__name__)
            value = None
        with _lock:
            _reported_cache[key] = (now + REPORTED_CACHE_SECONDS, value)
        reported[device_id] = value
    return reported


def get_session(event: Dict[str, Any], user: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    meta, pairs = _load_session(session_id)
    if meta is None:
        return _error(404, "no_debug_session", "No debug session with that id")
    usecase_id = str(meta.get("usecase_id"))
    denied = _authorize(event, user, usecase_id, Permission.VIEW_DEVICE_LOGS)
    if denied:
        return denied
    usecase = _usecase_or_404(usecase_id)
    reported = _reported_for(usecase, meta, pairs)
    return create_response(200, _session_view(meta, pairs, reported))


def stop_session(event: Dict[str, Any], user: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    meta, pairs = _load_session(session_id)
    if meta is None:
        return _error(404, "no_debug_session", "No debug session with that id")
    usecase_id = str(meta.get("usecase_id"))
    denied = _authorize(event, user, usecase_id, Permission.MANAGE_DEVICES)
    if denied:
        return denied
    if not sessions.is_live(meta.get("state")):
        raise Refusal(409, "debug_session_not_live", "The debug session is not running", state=meta.get("state"))
    usecase = _usecase_or_404(usecase_id)
    workflow_id = str(meta.get("workflow_id"))
    device_ids = [str(p.get("device_id") or sessions.device_of_pair_sk(p.get("sk"))) for p in pairs]
    results = shadow.write_all_sessions(usecase, [(d, workflow_id, None) for d in device_ids])
    now_ms = int(time.time() * 1000)
    try:
        _transact_client().transact_write_items(TransactItems=sessions.serialize_actions(
            sessions.stop_actions(_table_name(), meta, pairs, new_state=sessions.STOPPED, now_ms=now_ms)))
    except ClientError as error:
        if str((error.response.get("Error") or {}).get("Code") or "") != "TransactionCanceledException":
            raise
        raise Refusal(409, "debug_session_not_live", "The debug session was already stopped", state="stopped")
    rows = []
    for device_id in device_ids:
        result = results.get(device_id) or {}
        row = {"device_id": device_id, "shadow": result.get("result", shadow.RESULT_FAILED)}
        if result.get("error"):
            row["error"] = result["error"]
        rows.append(row)
    log_audit_event(
        user_id=user["user_id"], action="debug_session_stopped", resource_type="debug_session",
        resource_id=session_id, result="success",
        details={"usecase_id": usecase_id, "workflow_id": workflow_id, "devices": device_ids}, event=event)
    return create_response(200, {"session_id": session_id, "state": sessions.STOPPED, "devices": rows,
                                 "stoppedAt": now_ms})


# --------------------------------------------------------------------------- GET log
def _int_param(params: Dict[str, Any], name: str) -> Optional[int]:
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise Refusal(400, "invalid_query", "{0} must be an integer".format(name), field=name)


def _validate_log_query(params: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    device_id = params.get("device_id")
    workflow_id = params.get("workflow_id")
    if not isinstance(device_id, str) or not _THING_RE.match(device_id):
        raise Refusal(400, "invalid_query", "device_id is required", field="device_id")
    if not isinstance(workflow_id, str) or not _ID_RE.match(workflow_id):
        raise Refusal(400, "invalid_query", "workflow_id is required", field="workflow_id")
    window_to = _int_param(params, "to")
    window_from = _int_param(params, "from")
    if window_to is None:
        window_to = now_ms
    if window_from is None:
        window_from = window_to - LOG_WINDOW_DEFAULT_MS
    if window_from < 0 or window_to <= window_from:
        raise Refusal(400, "invalid_query", "from must be before to", field="from")
    if window_to - window_from > LOG_WINDOW_MAX_MS:
        raise Refusal(400, "invalid_query", "the window may span at most 24 hours", field="to")
    limit = _int_param(params, "limit")
    if limit is None:
        limit = LOG_LIMIT_DEFAULT
    if not LOG_LIMIT_RANGE[0] <= limit <= LOG_LIMIT_RANGE[1]:
        raise Refusal(400, "invalid_query", "limit must be between 1 and 10000", field="limit")
    execution_id = params.get("execution_id") or None
    if execution_id is not None and not _ID_RE.match(str(execution_id)):
        raise Refusal(400, "invalid_query", "execution_id is invalid", field="execution_id")
    node_id = params.get("node_id") or None
    if node_id is not None and not _NODE_RE.match(str(node_id)):
        raise Refusal(400, "invalid_query", "node_id is too long", field="node_id")
    level = params.get("level") or None
    if level is not None and str(level).upper() not in log_filters.LEVELS:
        raise Refusal(400, "invalid_query", "level must be DEBUG, INFO, WARNING or ERROR", field="level")
    kind = params.get("type") or None
    if kind is not None and kind not in log_filters.TYPES:
        raise Refusal(400, "invalid_query", "type must be run, log, drop or session", field="type")
    ts_from = _int_param(params, "ts_from")
    ts_to = _int_param(params, "ts_to")
    token = params.get("next") or None
    if token is not None and (not isinstance(token, str) or len(token) > 2048):
        raise Refusal(400, "invalid_token", "The page token is not valid")
    return {"device_id": device_id, "workflow_id": workflow_id, "from": window_from, "to": window_to,
            "limit": limit, "execution_id": execution_id, "node_id": node_id,
            "level": str(level).upper() if level else None, "type": kind, "ts_from": ts_from, "ts_to": ts_to,
            "next": token}


def _audit_log_view(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str, session_id: str,
                    device_id: str, workflow_id: str) -> None:
    key = (user["user_id"], session_id)
    now = time.time()
    with _lock:
        last = _log_view_audits.get(key)
        if last is not None and now - last < LOG_VIEW_AUDIT_SECONDS:
            return
        _log_view_audits[key] = now
    log_audit_event(user_id=user["user_id"], action="debug_log_viewed", resource_type="debug_session",
                    resource_id=session_id, result="success",
                    details={"usecase_id": usecase_id, "device_id": device_id, "workflow_id": workflow_id},
                    event=event)


def read_log(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    now_ms = int(time.time() * 1000)
    query = _validate_log_query(params, now_ms)
    pair = _pair_for(usecase_id, query["device_id"], query["workflow_id"])
    if pair is None:
        return _error(404, "no_debug_session", "No debug session has been recorded for this device and workflow")
    group = logs_writer.log_group_name(usecase_id)
    stream = str(pair.get("logStream") or logs_writer.stream_name(query["device_id"], query["workflow_id"]))
    _audit_log_view(event, user, usecase_id, str(pair.get("sessionId")), query["device_id"], query["workflow_id"])
    cache_key = json.dumps({"uc": usecase_id, **query}, sort_keys=True)
    now = time.time()
    with _lock:
        cached = _log_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return create_response(200, cached[1])
    api = log_reads.FILTER if query["execution_id"] else log_reads.GET
    try:
        log_reads.take(api)
    except log_reads.ReadThrottled as throttled:
        return _error(429, "log_read_throttled", "Log reads are rate-limited; retry shortly",
                      retry_after_ms=throttled.retry_after_ms)
    try:
        if api == log_reads.GET:
            kwargs: Dict[str, Any] = {"logGroupName": group, "logStreamName": stream, "startTime": query["from"],
                                      "endTime": query["to"], "startFromHead": True, "limit": query["limit"]}
            if query["next"]:
                kwargs["nextToken"] = query["next"]
            response = _logs().get_log_events(**kwargs)
        else:
            kwargs = {"logGroupName": group, "logStreamNames": [stream],
                      "filterPattern": '{ $.executionId = "%s" }' % query["execution_id"],
                      "startTime": query["from"], "endTime": query["to"], "limit": query["limit"]}
            if query["next"]:
                kwargs["nextToken"] = query["next"]
            response = _logs().filter_log_events(**kwargs)
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code == "ResourceNotFoundException":
            answer = {"events": [], "next": None}
            with _lock:
                _log_cache[cache_key] = (now + LOG_CACHE_SECONDS, answer)
            return create_response(200, answer)
        if code == "InvalidParameterException":
            if query["next"]:
                return _error(400, "invalid_token", "The page token was rejected; repeat the window without it")
            return _error(400, "invalid_query", "The log query was rejected", reason=code)
        throttled = log_reads.map_client_error(error, api)
        if throttled is not None:
            logger.warning("CloudWatch Logs %s throttled despite the pacing bucket", api)
            return _error(429, "log_read_throttled", "CloudWatch Logs is throttling reads; retry shortly",
                          retry_after_ms=throttled.retry_after_ms)
        raise
    events = log_filters.apply(log_filters.to_answer_events(response.get("events") or []),
                               node_id=query["node_id"], level=query["level"], type=query["type"],
                               ts_from=query["ts_from"], ts_to=query["ts_to"])
    answer = {"events": events, "next": log_filters.next_token(query["next"], response, api)}
    with _lock:
        _log_cache[cache_key] = (now + LOG_CACHE_SECONDS, answer)
    return create_response(200, answer)


# --------------------------------------------------------------------------- GET image
def get_image(event: Dict[str, Any], user: Dict[str, Any], session_id: str, execution_id: str) -> Dict[str, Any]:
    meta, pairs = _load_session(session_id)
    if meta is None:
        return _error(404, "no_debug_session", "No debug session with that id")
    usecase_id = str(meta.get("usecase_id"))
    denied = _authorize(event, user, usecase_id, Permission.VIEW_DEVICE_LOGS)
    if denied:
        return denied
    if not registry_enforcement_enabled():
        raise Refusal(409, "registry_enforcement_required",
                      "Debug images are refused while registry enforcement is off", ENFORCEMENT_REMEDY)
    if not isinstance(execution_id, str) or not _ID_RE.match(execution_id):
        raise Refusal(400, "invalid_query", "execution_id is invalid", field="execution_id")
    params = event.get("queryStringParameters") or {}
    device_id = params.get("device_id")
    device_ids = {str(p.get("device_id") or sessions.device_of_pair_sk(p.get("sk"))) for p in pairs}
    if device_id is None and len(device_ids) == 1:
        device_id = next(iter(device_ids))
    if not isinstance(device_id, str) or device_id not in device_ids:
        raise Refusal(400, "invalid_query", "device_id must name a device of the session", field="device_id")
    usecase = _usecase_or_404(usecase_id)
    bucket = str((meta.get("media") or {}).get("bucket") or media.results_bucket(usecase))
    key = shadow.prefix_for(usecase_id, device_id, str(meta.get("workflow_id")), session_id) + execution_id + ".jpg"
    client = media.credentials.client("s3", usecase, "image-read", bucket=bucket)
    if not media.head_image(usecase, bucket, key, client=client):
        return _error(404, "image_not_found", "No image was uploaded for this run")
    url = media.presign_image(usecase, bucket, key, client=client)
    return create_response(200, {"url": url, "expires_in": media.PRESIGN_SECONDS, "key": key})


# --------------------------------------------------------------------------- GET console link
def get_console_link(event: Dict[str, Any], user: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    denied = _authorize_portal_admin(event, user)
    if denied:
        return denied
    meta, pairs = _load_session(session_id)
    if meta is None:
        return _error(404, "no_debug_session", "No debug session with that id")
    params = event.get("queryStringParameters") or {}
    device_id = params.get("device_id")
    by_device = {str(p.get("device_id") or sessions.device_of_pair_sk(p.get("sk"))): p for p in pairs}
    if device_id is None and len(by_device) == 1:
        device_id = next(iter(by_device))
    if not isinstance(device_id, str) or device_id not in by_device:
        raise Refusal(400, "invalid_query", "device_id must name a device of the session", field="device_id")
    region = os.environ.get("AWS_REGION", "us-east-1")
    group = str(meta.get("logGroup") or logs_writer.log_group_name(str(meta.get("usecase_id"))))
    stream = str(by_device[device_id].get("logStream"))
    return create_response(200, {"url": console_link.build(region, group, stream), "region": region,
                                 "logGroup": group, "logStream": stream,
                                 "note": "The console's timestamp column is the sink's write time; the record's "
                                         "own ts and receivedMs are inside each event."})


# --------------------------------------------------------------------------- dispatch
def _route(event: Dict[str, Any]) -> Dict[str, Any]:
    method = str(event.get("httpMethod") or "").upper()
    resource = str(event.get("resource") or "")
    path_params = event.get("pathParameters") or {}
    if method == "OPTIONS":
        return create_response(200, "")
    user = get_user_from_event(event)
    if resource == "/debug/usecases/{usecase_id}/sessions":
        usecase_id = str(path_params.get("usecase_id") or "")
        if not _ID_RE.match(usecase_id):
            return _error(400, "invalid_query", "usecase_id is invalid", field="usecase_id")
        if method == "POST":
            denied = _authorize(event, user, usecase_id, Permission.MANAGE_DEVICES)
            return denied or create_session(event, user, usecase_id)
        if method == "GET":
            denied = _authorize(event, user, usecase_id, Permission.VIEW_DEVICE_LOGS)
            return denied or list_sessions(event, usecase_id)
    elif resource == "/debug/usecases/{usecase_id}/log":
        usecase_id = str(path_params.get("usecase_id") or "")
        if not _ID_RE.match(usecase_id):
            return _error(400, "invalid_query", "usecase_id is invalid", field="usecase_id")
        if method == "GET":
            denied = _authorize(event, user, usecase_id, Permission.VIEW_DEVICE_LOGS)
            return denied or read_log(event, user, usecase_id)
    elif resource == "/debug/sessions/{session_id}":
        session_id = str(path_params.get("session_id") or "")
        if method == "GET":
            return get_session(event, user, session_id)
        if method == "DELETE":
            return stop_session(event, user, session_id)
    elif resource == "/debug/sessions/{session_id}/images/{execution_id}":
        if method == "GET":
            return get_image(event, user, str(path_params.get("session_id") or ""),
                             str(path_params.get("execution_id") or ""))
    elif resource == "/debug/sessions/{session_id}/console-link":
        if method == "GET":
            return get_console_link(event, user, str(path_params.get("session_id") or ""))
    return _error(404, "not_found", "Not found")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        return _route(event)
    except Refusal as refusal:
        return refusal.response
    except Exception as error:  # noqa: BLE001 - the one catch-all of the entry point
        logger.exception("debug API failure: %s", error.__class__.__name__)
        return _error(500, "internal_error", "The debug API failed")
