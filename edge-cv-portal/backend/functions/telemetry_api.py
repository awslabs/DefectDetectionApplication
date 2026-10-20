"""
Workflow telemetry API (workflow-telemetry-dashboards design §8.3, §8.4, §9,
§10, §11, §12, §18; tasks.md 3.5): every ``/telemetry/*`` route.

| Method | Path | Permission |
|---|---|---|
| GET | /telemetry/schemas | any authenticated user |
| GET, PUT | /telemetry/usecases/{usecase_id}/settings | telemetry:manage |
| GET | /telemetry/usecases/{usecase_id}/templates?workflow_id=&device_ids= | dashboards:manage |
| GET | /telemetry/usecases/{usecase_id}/rejections | telemetry:manage |
| GET | /telemetry/rejections?account_id= | PortalAdmin |
| GET | /telemetry/dashboards?usecase_id= | dashboards:read |
| POST | /telemetry/dashboards | dashboards:manage |
| GET, PATCH, DELETE | /telemetry/dashboards/{dashboard_id} | read, manage, manage |
| GET | .../{dashboard_id}/now, /series, /events, /clips | dashboards:read |
| GET | .../{dashboard_id}/clips/{clip_ref}/media | dashboards:read and the guard |
| POST | .../{dashboard_id}/live-lease | dashboards:read, the caps and the guard |
| GET | .../{dashboard_id}/live?device_id=&have= | dashboards:read and the guard |

Handler order on every route: parse -> the use case from the path, query,
body or the stored record -> ``rbac_manager.has_permission`` (403
``INSUFFICIENT_PERMISSIONS`` with an ``unauthorized_access`` audit row, 500
``registry_unavailable``) -> the registry-enforcement guard on the three
guarded routes -> the work. Every refusal is ``{error, error_code, remedy?}``
with a lowercase code except the Portal-wide ``INSUFFICIENT_PERMISSIONS``.
Logs and audits never carry a presigned URL, a token or a payload body.
"""
import base64
import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

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
from edge_telemetry_common import credentials, shadow_pool
from edge_telemetry_common import devices as device_records
from edge_telemetry_common.topology import CROSS, telemetry_topology
from telemetry import configdb, constants, fold, health, leases, media, readiness, records, retention, schemas, series, shadow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- deployment values
DEFAULTS = {
    "TELEMETRY_NOW_POLL_SECONDS": 5,
    "TELEMETRY_LIVE_MAX_LEASES": 5,
    "TELEMETRY_LIVE_MAX_VIEW_MINUTES": 30,
    "TELEMETRY_CLIP_RETENTION_DAYS": 90,
}
CADENCE = {"now": None, "series": dict(series.REFRESH_SECONDS), "events": 15, "clips": 15, "liveFrame": 2,
           "liveLease": 20, "manualRefreshMin": 10}
EVENTS_WINDOW_DEFAULT_MS = 24 * 3600 * 1000
EVENTS_WINDOW_MAX_MS = 30 * 24 * 3600 * 1000
EVENTS_READ_LIMIT = 200
EVENTS_PAGE = 50
CLIPS_PAGE = 25
NOW_EVENTS_WINDOW_MS = 15 * 60 * 1000
NOW_EVENTS_LIMIT = 20
HEAD_BUDGET_SECONDS = 2.0
HEAD_PARALLEL = 8
CLIP_PRESIGN_SECONDS = 15 * 60
FRAME_PRESIGN_SECONDS = 120
LIVE_POINTER_WINDOW_MS = 24 * 3600 * 1000
GG_CACHE_SECONDS = 30
CLIP_AUDIT_SECONDS = 3600
NAME_RANGE = (1, 100)
EVENT_TYPES = ("violation_confirmed", "clip_ready", "run_error")

_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_THING_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{36}:\d{1,20}$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/.+$")
_CLIP_FILE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

ENFORCEMENT_REMEDY = "Turn on registry enforcement (PORTAL_REGISTRY_ENFORCED) once the registry backfill is complete."
LEASE_FAILED_REMEDY = "check that the device is online and the use case's role is in place"
DEVICE_RECORD_REMEDY = readiness.DEVICE_RECORD_REMEDY
MEDIA_REMEDY = "The dashboards read media from the use case's inference-results bucket only; the location is not a setting."


def _value(name: str) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]


# --------------------------------------------------------------------------- clients
_lock = threading.Lock()
_dynamodb_resource = None
_dynamodb_client = None
_serializer = TypeSerializer()


def _dynamodb():
    global _dynamodb_resource
    with _lock:
        if _dynamodb_resource is None:
            _dynamodb_resource = boto3.resource("dynamodb")
        return _dynamodb_resource


def _ddb_client():
    global _dynamodb_client
    with _lock:
        if _dynamodb_client is None:
            _dynamodb_client = boto3.client("dynamodb")
        return _dynamodb_client


def _config():
    return _dynamodb().Table(os.environ["TELEMETRY_CONFIG_TABLE"])


def _summaries():
    return _dynamodb().Table(os.environ["TELEMETRY_SUMMARIES_TABLE"])


def _events():
    return _dynamodb().Table(os.environ["TELEMETRY_EVENTS_TABLE"])


def _aggregates():
    return _dynamodb().Table(os.environ["TELEMETRY_AGGREGATES_TABLE"])


# --------------------------------------------------------------------------- per-container caches
_gg_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_clip_audits: Dict[Tuple[str, str], float] = {}


def reset_caches() -> None:
    """Forget the per-container caches and clients (tests)."""
    global _dynamodb_resource, _dynamodb_client
    with _lock:
        _gg_cache.clear()
        _clip_audits.clear()
        _dynamodb_resource = None
        _dynamodb_client = None
    device_records.clear_cache()
    readiness.clear_cache()
    leases.reset_clients()


# --------------------------------------------------------------------------- answers
def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _dynamo(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo(v) for v in value]
    return value


def _error(status: int, code: str, message: str, remedy: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"error": message, "error_code": code}
    if remedy:
        body["remedy"] = remedy
    body.update(extra)
    return create_response(status, body)


def _ok(status: int, body: Any) -> Dict[str, Any]:
    return create_response(status, _plain(body))


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str, remedy: Optional[str] = None, **extra: Any) -> None:
        super().__init__(message)
        self.response = _error(status, code, message, remedy, **extra)


# --------------------------------------------------------------------------- authorization
def _authorize(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str, permission: Permission) -> Optional[Dict[str, Any]]:
    try:
        allowed = rbac_manager.has_permission(user["user_id"], usecase_id, permission, user_info=user)
    except RegistryUnavailable:
        logger.error("Portal_Identity registry unavailable during a telemetry route authorization")
        return _error(500, "registry_unavailable", "The Portal identity registry could not be read")
    if allowed:
        return None
    log_audit_event(
        user_id=user["user_id"], action="unauthorized_access", resource_type="telemetry",
        resource_id=event.get("resource") or event.get("path") or "unknown", result="denied",
        details={"required_permission": permission.value, "usecase_id": usecase_id, "method": event.get("httpMethod"),
                 "path": event.get("path"), "user_role": user.get("role", "unknown")},
        event=event)
    return _error(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions", required_permission=permission.value)


def _authorize_portal_admin(event: Dict[str, Any], user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        allowed = caller_is_portal_admin(user)
    except RegistryUnavailable:
        return _error(500, "registry_unavailable", "The Portal identity registry could not be read")
    if allowed:
        return None
    log_audit_event(
        user_id=user["user_id"], action="unauthorized_access", resource_type="telemetry",
        resource_id=event.get("resource") or "unknown", result="denied",
        details={"required_role": "PortalAdmin", "method": event.get("httpMethod"), "path": event.get("path"),
                 "user_role": user.get("role", "unknown")},
        event=event)
    return _error(403, "INSUFFICIENT_PERMISSIONS", "Insufficient permissions", required_role="PortalAdmin")


def _guard() -> None:
    if not registry_enforcement_enabled():
        raise Refusal(409, "registry_enforcement_required",
                      "Clip playback and live frames are refused while registry enforcement is off", ENFORCEMENT_REMEDY)


def _usecase_or_404(usecase_id: str) -> Dict[str, Any]:
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        raise Refusal(404, "usecase_not_found", "Use case not found")


def _portal_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- settings
def _settings_item(usecase_id: str) -> Optional[Dict[str, Any]]:
    return _config().get_item(Key=configdb.settings_key(usecase_id)).get("Item")


def _settings_view(usecase: Dict[str, Any], item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    item = item or {}
    location = _plain(item.get("media_location")) or media.media_location(usecase)
    return {
        "usecase_id": str(usecase["usecase_id"]),
        "enabled": bool(item.get("enabled", False)),
        "summaryIntervalS": int(item.get("summaryIntervalS") or constants.DEFAULT_SUMMARY_INTERVAL_S),
        "idleHeartbeatS": int(item.get("idleHeartbeatS") or constants.DEFAULT_IDLE_HEARTBEAT_S),
        "personClasses": list(constants.PERSON_CLASSES),
        "media_location": location,
        "topology": telemetry_topology(usecase),
        "updatedAt": _plain(item.get("updatedAt")),
        "updatedBy": item.get("updatedBy"),
        "shadowSyncPending": bool(item.get("shadowSyncPending", False)),
    }


def _checklist(usecase: Dict[str, Any], item: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return readiness.checklist(usecase, _plain(item) or {}, config_table=_config(),
                               devices_table=_dynamodb().Table(os.environ["DEVICES_TABLE"]),
                               portal_region=_portal_region(), clip_days=_value("TELEMETRY_CLIP_RETENTION_DAYS"))


def get_settings(usecase_id: str) -> Dict[str, Any]:
    usecase = _usecase_or_404(usecase_id)
    item = _settings_item(usecase_id)
    return _ok(200, {"settings": _settings_view(usecase, item), "checklist": _checklist(usecase, item)})


def _parse_body(event: Dict[str, Any], code: str) -> Dict[str, Any]:
    raw = event.get("body")
    if raw in (None, ""):
        return {}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        raise Refusal(400, code, "The request body is not a JSON object", field="body")
    if not isinstance(body, dict):
        raise Refusal(400, code, "The request body is not a JSON object", field="body")
    return body


def _int_in_range(body: Dict[str, Any], field: str, bounds: Tuple[int, int], default: int, code: str) -> int:
    value = body.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refusal(400, code, "{0} must be an integer".format(field), field=field)
    if not bounds[0] <= value <= bounds[1]:
        raise Refusal(400, code, "{0} must be between {1} and {2}".format(field, *bounds), field=field)
    return value


def put_settings(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    usecase = _usecase_or_404(usecase_id)
    body = _parse_body(event, "invalid_settings")
    if "personClasses" in body:
        raise Refusal(400, "invalid_settings", "personClasses is not a setting in this phase", field="personClasses")
    for key in ("media_location", "bucket", "inference_uploader_s3_bucket"):
        if key in body:
            raise Refusal(400, "bucket_not_allowed", "The media location is read-only", MEDIA_REMEDY, field=key)
    enabled = body.get("enabled")
    if enabled not in (True, False):
        raise Refusal(400, "invalid_settings", "enabled must be a boolean", field="enabled")
    current = _settings_item(usecase_id) or {}
    summary_s = _int_in_range(body, "summaryIntervalS", constants.SUMMARY_INTERVAL_RANGE,
                              int(current.get("summaryIntervalS") or constants.DEFAULT_SUMMARY_INTERVAL_S), "invalid_settings")
    heartbeat_s = _int_in_range(body, "idleHeartbeatS", constants.IDLE_HEARTBEAT_RANGE,
                                int(current.get("idleHeartbeatS") or constants.DEFAULT_IDLE_HEARTBEAT_S), "invalid_settings")
    for key in body:
        if key not in ("enabled", "summaryIntervalS", "idleHeartbeatS"):
            raise Refusal(400, "invalid_settings", "unknown setting", field=str(key)[:64])
    location = media.media_location(usecase)
    if enabled:
        if shared_utils.get_usecase_region(usecase) != _portal_region():
            raise Refusal(400, "region_not_supported", "Telemetry is not supported for use cases outside the Portal's region",
                          readiness.REMEDIES["region"])
        if telemetry_topology(usecase) == CROSS and not readiness.role_arn_valid(usecase):
            raise Refusal(400, "usecase_role_invalid", "The use case's role ARN is not a DDAPortalAccessRole ARN",
                          readiness.REMEDIES["role_form"])
        probe = readiness.probe_media_location(usecase, location["bucket"])
        if not probe.ok:
            remedy = readiness.REMEDIES["media_location_missing"] if probe.error == "NoSuchBucket" \
                else readiness.REMEDIES["media_location_denied"]
            raise Refusal(400, "media_location_not_writable", "The use case's media location cannot be reached", remedy,
                          reason=probe.error, bucket=location["bucket"])
        location.update({"region": probe.region, "region_source": probe.region_source})
    elif current.get("media_location"):
        location = _plain(current["media_location"])
    now_ms = _now_ms()
    item = {
        "pk": configdb.usecase_pk(usecase_id), "sk": configdb.SETTINGS_SK, "usecase_id": usecase_id,
        "enabled": enabled, "summaryIntervalS": summary_s, "idleHeartbeatS": heartbeat_s,
        "media_location": location, "updatedAt": now_ms, "updatedBy": user.get("email") or user["user_id"],
        "account_id": str(usecase.get("account_id") or ""), "region": shared_utils.get_usecase_region(usecase),
        "gsi2pk": configdb.BY_TYPE_USECASE, "gsi2sk": configdb.usecase_pk(usecase_id),
    }
    devices = device_records.usecase_devices(usecase)
    pending = len(devices) > shadow_pool.SHADOW_WRITE_SYNC_LIMIT
    item["shadowSyncPending"] = pending
    _config().put_item(Item=_dynamo(item))
    readiness.clear_cache()
    rows: List[Dict[str, Any]] = []
    if not pending:
        results = shadow.write_all_desired(usecase, devices, item, table=_config(), now_ms=now_ms)
        for device_id in devices:
            result = results.get(device_id) or {"result": shadow.RESULT_FAILED, "error": "unknown"}
            row = {"device_id": device_id, "shadow": result.get("result")}
            if result.get("error"):
                row["error"] = result["error"]
            rows.append(row)
    checklist = _checklist(usecase, item)
    unrecorded = next((i.get("unrecorded") for i in checklist if i["id"] == "device_records"), []) or []
    was_enabled = bool(current.get("enabled", False))
    action = "telemetry_settings_changed"
    if enabled and not was_enabled:
        action = "telemetry_enabled"
    elif was_enabled and not enabled:
        action = "telemetry_disabled"
    log_audit_event(user_id=user["user_id"], action=action, resource_type="telemetry_settings", resource_id=usecase_id,
                    result="success", details={"usecase_id": usecase_id, "enabled": enabled, "summaryIntervalS": summary_s,
                                               "idleHeartbeatS": heartbeat_s, "devices": len(devices),
                                               "pending": pending}, event=event)
    answer = {"settings": _settings_view(usecase, item), "devices": rows, "unrecorded_devices": unrecorded,
              "checklist": checklist}
    if pending:
        answer["pending_devices"] = len(devices)
        answer["message"] = "The device shadows are written by the maintenance function within 5 minutes"
    return _ok(202 if pending else 200, answer)


# --------------------------------------------------------------------------- schemas and templates
def get_schemas() -> Dict[str, Any]:
    return _ok(200, {
        "registry": {"version": schemas.REGISTRY["version"], "kinds": schemas.SCHEMA_BY_KIND,
                     "modelStates": list(schemas.MODEL_STATES), "modelStateOrder": list(schemas.MODEL_STATE_ORDER)},
        "catalog": schemas.CATALOG,
        "templates": schemas.TEMPLATES,
        "telemetryNowPollSeconds": _value("TELEMETRY_NOW_POLL_SECONDS"),
        "cadence": dict(CADENCE, now=_value("TELEMETRY_NOW_POLL_SECONDS")),
        "caps": {"devicesPerDashboard": constants.MAX_DEVICES_PER_DASHBOARD,
                 "dashboardsPerUsecase": constants.MAX_DASHBOARDS_PER_USECASE,
                 "liveMaxLeases": _value("TELEMETRY_LIVE_MAX_LEASES"),
                 "liveMaxViewMinutes": _value("TELEMETRY_LIVE_MAX_VIEW_MINUTES"),
                 "clipRetentionDays": _value("TELEMETRY_CLIP_RETENTION_DAYS"),
                 "deviceLiveLimit": constants.DEVICE_LIVE_LIMIT,
                 "summaryIntervalRange": list(constants.SUMMARY_INTERVAL_RANGE),
                 "idleHeartbeatRange": list(constants.IDLE_HEARTBEAT_RANGE)},
        "personClasses": list(constants.PERSON_CLASSES),
    })


def _pointers(pk: str, now_s: float) -> Dict[str, Dict[str, Any]]:
    """``{sk: item}`` of the Pair's ``LAST*`` pointers (one Query)."""
    items = configdb.query_all(_summaries(), KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("LAST"),
                               FilterExpression=Attr("expires_at").gt(int(now_s)))
    return {str(item["sk"]): item for item in items}


def _template_availability(template: Dict[str, Any], newest: List[Dict[str, Any]], now_ms: int) -> Dict[str, Any]:
    """``available`` | ``partial`` | ``unavailable`` from the newest Summaries of
    the Pair's producers (within 24 h)."""
    recent = [p for p in newest if now_ms - int(p.get("receivedMs") or 0) <= 24 * 3600 * 1000]
    producers = {str(p.get("sk", "")).split("#", 1)[-1] for p in recent}
    reported_panels = []
    unreported_panels = []
    for panel in template.get("panels", []):
        reported = False
        for measure_id in panel.get("measures", []):
            measure = schemas.measure_by_id(measure_id)
            if measure is None:
                continue
            if measure["source"] == "event":
                reported = reported or "ppe-clipper" in producers
                continue
            for pointer in recent:
                value = schemas.resolve_path(_plain(pointer.get("summary") or {}), measure["path"])
                if value not in (None, {}):
                    reported = True
                    break
        (reported_panels if reported else unreported_panels).append(panel["id"])
    if not recent or not reported_panels:
        state = "unavailable"
    elif unreported_panels:
        state = "partial"
    else:
        state = "available"
    return {"id": template["id"], "version": template["version"], "availability": state,
            "reported_panels": reported_panels, "unreported_panels": unreported_panels,
            "producers": sorted(producers)}


def get_templates(event: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    usecase = _usecase_or_404(usecase_id)
    params = event.get("queryStringParameters") or {}
    workflow_id = params.get("workflow_id")
    device_ids = [d for d in str(params.get("device_ids") or "").split(",") if d]
    now_ms = _now_ms()
    newest: List[Dict[str, Any]] = []
    if workflow_id and _UUID_RE.match(workflow_id):
        for device_id in device_ids[: constants.MAX_DEVICES_PER_DASHBOARD]:
            if not _THING_RE.match(device_id):
                continue
            pointers = _pointers(records.pair_pk(usecase_id, device_id, workflow_id), now_ms / 1000.0)
            newest.extend(item for sk, item in pointers.items() if sk.startswith("LAST#"))
    templates = [_template_availability(template, newest, now_ms) for template in schemas.TEMPLATES.values()]
    return _ok(200, {"usecase_id": usecase["usecase_id"], "templates": templates})


# --------------------------------------------------------------------------- rejections
def _rejections(pk: str, now_ms: int) -> Dict[str, Any]:
    start = configdb.minute_of(now_ms - 3600 * 1000)
    rows = configdb.query_all(_config(), KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
        configdb.rej_sk(start), configdb.rej_sk(now_ms)), FilterExpression=Attr("expires_at").gt(now_ms // 1000))
    counts: Dict[str, int] = {}
    minutes = []
    for row in rows:
        minute = {"minute": _plain(row.get("minuteMs")), "counts": {}}
        for key, value in row.items():
            if key in ("pk", "sk", "expires_at", "minuteMs"):
                continue
            count = int(value)
            counts[key] = counts.get(key, 0) + count
            minute["counts"][key] = count
        minutes.append(minute)
    samples = configdb.query_all(_config(), KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
        configdb.sample_sk(start, ""), configdb.sample_sk(now_ms, "~")), FilterExpression=Attr("expires_at").gt(now_ms // 1000))
    return {"counts": counts, "minutes": minutes,
            "samples": [{"reason": s.get("reason"), "receivedMs": _plain(s.get("receivedMs")), "sample": _plain(s.get("sample"))}
                        for s in samples][-50:],
            "note": "The rate_exceeded guard is per ingest container; the fleet-wide bound is 120 Events per minute "
                    "per device and workflow times the number of containers."}


def get_usecase_rejections(usecase_id: str) -> Dict[str, Any]:
    _usecase_or_404(usecase_id)
    return _ok(200, dict(_rejections(configdb.usecase_pk(usecase_id), _now_ms()), usecase_id=usecase_id, scope="usecase"))


def get_account_rejections(event: Dict[str, Any]) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    account_id = str(params.get("account_id") or "")
    if not _ACCOUNT_RE.match(account_id):
        raise Refusal(400, "invalid_query", "account_id must be a 12-digit account id", field="account_id")
    return _ok(200, dict(_rejections(configdb.account_pk(account_id), _now_ms()), account_id=account_id, scope="account"))


# --------------------------------------------------------------------------- dashboards
def _dashboard_view(item: Dict[str, Any]) -> Dict[str, Any]:
    return _plain({
        "dashboard_id": item.get("dashboard_id"), "usecase_id": item.get("usecase_id"), "workflow_id": item.get("workflow_id"),
        "device_ids": list(item.get("device_ids") or []), "template_id": item.get("template_id"),
        "template_version": item.get("template_version"), "name": item.get("name"), "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"), "actor": item.get("actor"),
    })


def _load_dashboard(dashboard_id: str) -> Dict[str, Any]:
    if not _ID_RE.match(dashboard_id or ""):
        raise Refusal(404, "dashboard_not_found", "Dashboard not found")
    item = _config().get_item(Key={"pk": configdb.dash_pk(dashboard_id), "sk": configdb.META_SK}).get("Item")
    if not item:
        raise Refusal(404, "dashboard_not_found", "Dashboard not found")
    return item


def list_dashboards(event: Dict[str, Any], usecase_id: str) -> Dict[str, Any]:
    _usecase_or_404(usecase_id)
    items = configdb.query_all(_config(), IndexName="by-usecase",
                               KeyConditionExpression=Key("gsi1pk").eq(configdb.usecase_pk(usecase_id)),
                               ScanIndexForward=False)
    return _ok(200, {"usecase_id": usecase_id, "dashboards": [_dashboard_view(i) for i in items]})


def _valid_name(value: Any) -> str:
    if not isinstance(value, str):
        raise Refusal(400, "invalid_dashboard", "name must be a string", field="name")
    name = unicodedata.normalize("NFKC", value).strip()
    if not NAME_RANGE[0] <= len(name) <= NAME_RANGE[1]:
        raise Refusal(400, "invalid_dashboard", "name must be 1 to 100 characters", field="name")
    return name


def _valid_devices(value: Any, usecase: Dict[str, Any]) -> List[str]:
    if not isinstance(value, list) or not value:
        raise Refusal(400, "invalid_dashboard", "device_ids must be a non-empty list", field="device_ids")
    if len(value) > constants.MAX_DEVICES_PER_DASHBOARD:
        raise Refusal(400, "invalid_dashboard", "a dashboard holds at most {0} devices".format(constants.MAX_DEVICES_PER_DASHBOARD),
                      field="device_ids", max=constants.MAX_DEVICES_PER_DASHBOARD)
    devices: List[str] = []
    for device_id in value:
        if not isinstance(device_id, str) or not _THING_RE.match(device_id):
            raise Refusal(400, "invalid_dashboard", "device_ids must be thing names", field="device_ids")
        if device_id in devices:
            raise Refusal(400, "invalid_dashboard", "device_ids must be distinct", field="device_ids")
        devices.append(device_id)
    recorded = set(device_records.usecase_devices(usecase))
    missing = [d for d in devices if d not in recorded]
    if missing:
        raise Refusal(400, "device_not_recorded", "A device has no record naming this use case", DEVICE_RECORD_REMEDY,
                      device_ids=missing, device_id=missing[0])
    return devices


def _valid_workflow(workflow_id: Any, usecase_id: str) -> Dict[str, Any]:
    if not isinstance(workflow_id, str) or not _UUID_RE.match(workflow_id):
        raise Refusal(400, "invalid_dashboard", "workflow_id must be a workflow UUID", field="workflow_id")
    workflow = _dynamodb().Table(os.environ["WORKFLOWS_TABLE"]).get_item(Key={"workflow_id": workflow_id}).get("Item")
    if not workflow or str(workflow.get("usecase_id") or "") != usecase_id:
        raise Refusal(400, "invalid_dashboard", "workflow_id is not a workflow of this use case", field="workflow_id")
    return workflow


def _valid_template(template_id: Any) -> Dict[str, Any]:
    if not isinstance(template_id, str) or template_id not in schemas.TEMPLATES:
        raise Refusal(400, "invalid_dashboard", "template_id is not a committed template", field="template_id",
                      templates=sorted(schemas.TEMPLATES))
    return schemas.TEMPLATES[template_id]


def _typed(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _serializer.serialize(v) for k, v in _dynamo(item).items()}


def _transact(items: List[Dict[str, Any]]) -> Optional[List[Any]]:
    try:
        _ddb_client().transact_write_items(TransactItems=items)
        return None
    except ClientError as error:
        if str((error.response.get("Error") or {}).get("Code") or "") != "TransactionCanceledException":
            raise
        return list(error.response.get("CancellationReasons") or [])


def _write_shadows(usecase: Dict[str, Any], devices: List[str]) -> List[Dict[str, Any]]:
    settings = _plain(_settings_item(str(usecase["usecase_id"]))) or {}
    results = shadow.write_all_desired(usecase, devices, settings, table=_config())
    rows = []
    for device_id in devices:
        result = results.get(device_id) or {"result": shadow.RESULT_FAILED, "error": "unknown"}
        row = {"device_id": device_id, "shadow": result.get("result")}
        if result.get("error"):
            row["error"] = result["error"]
        rows.append(row)
    return rows


def create_dashboard(event: Dict[str, Any], user: Dict[str, Any], body: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    usecase_id = str(usecase["usecase_id"])
    name = _valid_name(body.get("name"))
    workflow = _valid_workflow(body.get("workflow_id"), usecase_id)
    devices = _valid_devices(body.get("device_ids"), usecase)
    template = _valid_template(body.get("template_id"))
    now_ms = _now_ms()
    dashboard_id = str(uuid.uuid4())
    table_name = os.environ["TELEMETRY_CONFIG_TABLE"]
    meta = {"pk": configdb.dash_pk(dashboard_id), "sk": configdb.META_SK, "dashboard_id": dashboard_id,
            "usecase_id": usecase_id, "workflow_id": workflow["workflow_id"], "device_ids": devices,
            "template_id": template["id"], "template_version": template["version"], "name": name,
            "createdAt": now_ms, "updatedAt": now_ms, "actor": user.get("email") or user["user_id"],
            "gsi1pk": configdb.usecase_pk(usecase_id), "gsi1sk": configdb.by_usecase_sk(now_ms, dashboard_id)}
    marker = {"pk": configdb.usecase_pk(usecase_id), "sk": configdb.dashname_sk(name), "dashboard_id": dashboard_id,
              "name": name}
    reasons = _transact([
        {"Put": {"TableName": table_name, "Item": _typed(marker), "ConditionExpression": "attribute_not_exists(pk)"}},
        {"Update": {"TableName": table_name, "Key": _typed({"pk": configdb.usecase_pk(usecase_id), "sk": configdb.DASHCOUNT_SK}),
                    "UpdateExpression": "ADD #c :one",
                    "ConditionExpression": "attribute_not_exists(#c) OR #c < :max",
                    "ExpressionAttributeNames": {"#c": "count"},
                    "ExpressionAttributeValues": {":one": {"N": "1"}, ":max": {"N": str(constants.MAX_DASHBOARDS_PER_USECASE)}}}},
        {"Put": {"TableName": table_name, "Item": _typed(meta), "ConditionExpression": "attribute_not_exists(pk)"}},
    ])
    if reasons is not None:
        codes = [str((r or {}).get("Code") or "") for r in reasons]
        if len(codes) > 0 and codes[0] == "ConditionalCheckFailed":
            raise Refusal(409, "dashboard_name_taken", "A dashboard of this use case already has that name", name=name)
        if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
            raise Refusal(409, "dashboard_limit_reached", "The use case holds its maximum of {0} dashboards".format(
                constants.MAX_DASHBOARDS_PER_USECASE), max=constants.MAX_DASHBOARDS_PER_USECASE)
        raise Refusal(409, "dashboard_conflict", "The dashboard could not be created; retry")
    rows = _write_shadows(usecase, devices)
    log_audit_event(user_id=user["user_id"], action="dashboard_created", resource_type="dashboard", resource_id=dashboard_id,
                    result="success", details={"usecase_id": usecase_id, "workflow_id": workflow["workflow_id"],
                                               "devices": devices, "template_id": template["id"], "name": name}, event=event)
    return _ok(201, {"dashboard": _dashboard_view(meta), "devices": rows})


def patch_dashboard(event: Dict[str, Any], user: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    body = _parse_body(event, "invalid_dashboard")
    usecase_id = str(usecase["usecase_id"])
    dashboard_id = str(item["dashboard_id"])
    table_name = os.environ["TELEMETRY_CONFIG_TABLE"]
    for key in body:
        if key not in ("name", "device_ids"):
            raise Refusal(400, "invalid_dashboard", "only name and device_ids may change", field=str(key)[:64])
    if not body:
        raise Refusal(400, "invalid_dashboard", "nothing to change", field="body")
    new_name = _valid_name(body["name"]) if "name" in body else None
    new_devices = _valid_devices(body["device_ids"], usecase) if "device_ids" in body else None
    now_ms = _now_ms()
    sets = ["#u = :u"]
    names = {"#u": "updatedAt"}
    values: Dict[str, Any] = {":u": {"N": str(now_ms)}}
    actions: List[Dict[str, Any]] = []
    old_name = str(item.get("name") or "")
    if new_name is not None and configdb.fold_name(new_name) != configdb.fold_name(old_name):
        actions.append({"Delete": {"TableName": table_name,
                                   "Key": _typed({"pk": configdb.usecase_pk(usecase_id), "sk": configdb.dashname_sk(old_name)})}})
        actions.append({"Put": {"TableName": table_name,
                                "Item": _typed({"pk": configdb.usecase_pk(usecase_id), "sk": configdb.dashname_sk(new_name),
                                                "dashboard_id": dashboard_id, "name": new_name}),
                                "ConditionExpression": "attribute_not_exists(pk)"}})
    if new_name is not None:
        sets.append("#n = :n")
        names["#n"] = "name"
        values[":n"] = {"S": new_name}
    if new_devices is not None:
        sets.append("#d = :d")
        names["#d"] = "device_ids"
        values[":d"] = _serializer.serialize(new_devices)
    actions.append({"Update": {"TableName": table_name, "Key": _typed({"pk": configdb.dash_pk(dashboard_id), "sk": configdb.META_SK}),
                               "UpdateExpression": "SET " + ", ".join(sets), "ConditionExpression": "attribute_exists(pk)",
                               "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}})
    reasons = _transact(actions)
    if reasons is not None:
        codes = [str((r or {}).get("Code") or "") for r in reasons]
        if new_name is not None and len(codes) >= 2 and codes[1] == "ConditionalCheckFailed":
            raise Refusal(409, "dashboard_name_taken", "A dashboard of this use case already has that name", name=new_name)
        raise Refusal(409, "dashboard_conflict", "The dashboard could not be updated; retry")
    updated = dict(item, updatedAt=now_ms)
    if new_name is not None:
        updated["name"] = new_name
    added: List[str] = []
    if new_devices is not None:
        added = [d for d in new_devices if d not in set(item.get("device_ids") or [])]
        updated["device_ids"] = new_devices
    rows = _write_shadows(usecase, added) if added else []
    log_audit_event(user_id=user["user_id"], action="dashboard_updated", resource_type="dashboard", resource_id=dashboard_id,
                    result="success", details={"usecase_id": usecase_id, "name": updated.get("name"),
                                               "device_ids": list(updated.get("device_ids") or []), "added_devices": added}, event=event)
    return _ok(200, {"dashboard": _dashboard_view(updated), "devices": rows})


def delete_dashboard(event: Dict[str, Any], user: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    usecase_id = str(item["usecase_id"])
    dashboard_id = str(item["dashboard_id"])
    table_name = os.environ["TELEMETRY_CONFIG_TABLE"]
    reasons = _transact([
        {"Delete": {"TableName": table_name, "Key": _typed({"pk": configdb.dash_pk(dashboard_id), "sk": configdb.META_SK}),
                    "ConditionExpression": "attribute_exists(pk)"}},
        {"Delete": {"TableName": table_name,
                    "Key": _typed({"pk": configdb.usecase_pk(usecase_id), "sk": configdb.dashname_sk(str(item.get("name") or ""))})}},
        {"Update": {"TableName": table_name, "Key": _typed({"pk": configdb.usecase_pk(usecase_id), "sk": configdb.DASHCOUNT_SK}),
                    "UpdateExpression": "ADD #c :minus", "ConditionExpression": "attribute_exists(#c) AND #c > :zero",
                    "ExpressionAttributeNames": {"#c": "count"},
                    "ExpressionAttributeValues": {":minus": {"N": "-1"}, ":zero": {"N": "0"}}}},
    ])
    if reasons is not None:
        raise Refusal(409, "dashboard_conflict", "The dashboard could not be deleted; retry")
    log_audit_event(user_id=user["user_id"], action="dashboard_deleted", resource_type="dashboard", resource_id=dashboard_id,
                    result="success", details={"usecase_id": usecase_id, "name": item.get("name")}, event=event)
    return _ok(200, {"dashboard_id": dashboard_id, "deleted": True})


# --------------------------------------------------------------------------- now
def _core_device_status(usecase: Dict[str, Any], device_id: str, now: float) -> Optional[Dict[str, Any]]:
    key = (str(usecase["usecase_id"]), device_id)
    with _lock:
        cached = _gg_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    table = _config()
    row = table.get_item(Key={"pk": configdb.gg_pk(key[0], device_id), "sk": configdb.GG_STATUS_SK}).get("Item")
    status: Optional[Dict[str, Any]] = None
    if configdb.is_unexpired(row, now) and row.get("fetchedAt") and now - float(row["fetchedAt"]) < GG_CACHE_SECONDS:
        status = _plain(row.get("status"))
    else:
        try:
            gg = credentials.client("greengrassv2", usecase, "api-control")
            answer = gg.get_core_device(coreDeviceThingName=device_id)
            status = {"status": answer.get("status"), "lastStatusUpdate": str(answer.get("lastStatusUpdateTimestamp") or ""),
                      "coreVersion": answer.get("coreVersion"), "platform": answer.get("platform")}
        except (ClientError, BotoCoreError) as error:
            code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else error.__class__.__name__
            status = {"status": "UNKNOWN", "error": str(code)[:64]}
        try:
            table.put_item(Item=_dynamo({"pk": configdb.gg_pk(key[0], device_id), "sk": configdb.GG_STATUS_SK, "status": status,
                                         "fetchedAt": now, "expires_at": int(now) + configdb.GG_TTL_SECONDS}))
        except ClientError:
            pass
    with _lock:
        _gg_cache[key] = (now + GG_CACHE_SECONDS, status)
    return status


def _last_hour(pk: str, now_ms: int) -> Dict[str, Any]:
    start = fold.bucket_minute("event", now_ms - 3600 * 1000)
    items = configdb.query_all(_aggregates(), KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
        "M#" + fold.minute_key(start), "M#" + fold.minute_key(fold.bucket_minute("event", now_ms))),
        FilterExpression=Attr("expires_at").gt(now_ms // 1000))
    items.sort(key=lambda i: str(i.get("sk")))
    folded = fold.fold_items(_plain(items))
    return {"measures": folded["measures"], "events": folded["events"], "gaps": folded["gaps"], "minutes": len(items)}


def get_now(user: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    usecase_id = str(usecase["usecase_id"])
    workflow_id = str(item["workflow_id"])
    now_ms = _now_ms()
    now_s = now_ms / 1000.0
    settings = _plain(_settings_item(usecase_id)) or {}
    devices_table = _dynamodb().Table(os.environ["DEVICES_TABLE"])
    answer_devices: Dict[str, Any] = {}
    for device_id in item.get("device_ids") or []:
        pk = records.pair_pk(usecase_id, device_id, workflow_id)
        pointers = _pointers(pk, now_s)
        last = pointers.get(records.last_sk("engine"))
        lasthb = pointers.get(records.lasthb_sk("engine"))
        flow_state = health.flow(last, lasthb, settings, now_ms)
        events = _events().query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
            "E#{0:013d}#".format(now_ms - NOW_EVENTS_WINDOW_MS), "E#~"), ScanIndexForward=False, Limit=NOW_EVENTS_LIMIT,
            FilterExpression=Attr("expires_at").gt(int(now_s))).get("Items", [])
        live = _summaries().get_item(Key={"pk": pk, "sk": records.LIVE_SK}).get("Item")
        hour = _last_hour(pk, now_ms)
        record = devices_table.get_item(Key={"device_id": device_id}).get("Item") or {}
        publish_failures = hour["measures"].get("telemetry_publish_failures", 0)
        entry: Dict[str, Any] = {
            "flow": flow_state,
            "ages": health.ages(last, lasthb, now_ms),
            "producers": sorted({sk.split("#", 1)[-1] for sk in pointers}),
            "last": {sk: {"receivedMs": _plain(p.get("receivedMs")), "eventMs": _plain(p.get("eventMs")), "seq": _plain(p.get("seq")),
                          "instance": p.get("instance"), "workflowVersion": p.get("workflowVersion"),
                          "lastRunMs": _plain(p.get("lastRunMs")), "summary": _plain(p.get("summary"))}
                     for sk, p in pointers.items()},
            "events": [{"eventId": e.get("eventId"), "type": e.get("type"), "eventMs": _plain(e.get("eventMs")),
                        "receivedMs": _plain(e.get("receivedMs")), "severity": _plain((e.get("event") or {}).get("severity")),
                        "data": _plain((e.get("event") or {}).get("data"))} for e in events],
            "live": None if not configdb.is_unexpired(live, now_s) else
            {"ageS": max(0, (now_ms - int(live.get("receivedMs") or 0)) // 1000), "frameToken": live.get("frameToken")},
            "lastHour": hour,
            "coreDevice": _core_device_status(usecase, device_id, now_s),
            "targetArchitecture": record.get("target_architecture"),
            "variantHint": health.variant_hint(flow_state, publish_failures, record.get("target_architecture")),
        }
        answer_devices[device_id] = entry
    answer: Dict[str, Any] = {"dashboard_id": item.get("dashboard_id"), "usecase_id": usecase_id, "workflow_id": workflow_id,
                              "enabled": bool(settings.get("enabled", False)), "devices": answer_devices,
                              "updatedAt": now_ms, "pollSeconds": _value("TELEMETRY_NOW_POLL_SECONDS")}
    try:
        if rbac_manager.has_permission(user["user_id"], usecase_id, Permission.TELEMETRY_MANAGE, user_info=user):
            answer["rejections"] = _rejections(configdb.usecase_pk(usecase_id), now_ms)
    except RegistryUnavailable:
        pass
    return _ok(200, answer)


# --------------------------------------------------------------------------- series
def get_series(event: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    template = schemas.TEMPLATES.get(str(item.get("template_id")))
    if template is None:
        raise Refusal(500, "configuration_error", "The dashboard names an unknown template", key="template_id")
    try:
        query = series.validate(event.get("queryStringParameters") or {}, _plain(item), template)
    except series.RangeTooLong:
        raise Refusal(400, "range_too_long", "The range may span at most 400 days", max_days=400)
    except series.InvalidQuery as error:
        raise Refusal(400, error.code, str(error), **error.extra)
    now_ms = _now_ms()
    usecase_id = str(usecase["usecase_id"])
    devices: Dict[str, Any] = {}
    for device_id in query["device_ids"]:
        pk = records.pair_pk(usecase_id, device_id, str(item["workflow_id"]))
        rows = series.query_device(_aggregates(), pk, query["resolution"], query["from"], query["to"],
                                   None if query["all_measures"] else query["measures"], now_ms / 1000.0)
        devices[device_id] = {"points": series.points(rows, query["resolution"])}
    return _ok(200, {"resolution": query["resolution"], "devices": devices, "updatedAt": now_ms,
                     "from": query["from"], "to": query["to"], "refreshSeconds": series.refresh_seconds(query["resolution"])})


# --------------------------------------------------------------------------- events
def _decode_cursor(token: Optional[str], partition_attr: str, partition: str) -> Optional[Dict[str, Any]]:
    """The base64url ``LastEvaluatedKey``, checked to belong to the partition."""
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode("ascii") + b"=" * (-len(token) % 4)).decode("utf-8"))
    except (ValueError, TypeError):
        raise Refusal(400, "invalid_cursor", "The cursor is not valid")
    if not isinstance(key, dict) or str(key.get(partition_attr) or "") != partition:
        raise Refusal(400, "invalid_cursor", "The cursor does not belong to this dashboard")
    return key


def _encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(_plain(key), default=str).encode("utf-8")).decode("ascii").rstrip("=")


def _int_query(params: Dict[str, Any], name: str, code: str) -> Optional[int]:
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise Refusal(400, code, "{0} must be an integer".format(name), field=name)


def _window(params: Dict[str, Any], now_ms: int, default_ms: int, max_ms: int) -> Tuple[int, int]:
    to_ms = _int_query(params, "to", "invalid_range")
    from_ms = _int_query(params, "from", "invalid_range")
    if to_ms is None:
        to_ms = now_ms
    if from_ms is None:
        from_ms = to_ms - default_ms
    if from_ms < 0 or to_ms < from_ms:
        raise Refusal(400, "invalid_range", "to must not be before from", field="from")
    if to_ms - from_ms > max_ms:
        raise Refusal(400, "invalid_range", "the range may span at most {0} days".format(max_ms // (24 * 3600 * 1000)), field="to")
    return from_ms, to_ms


def _device_filter(params: Dict[str, Any], item: Dict[str, Any]) -> List[str]:
    device_ids = list(item.get("device_ids") or [])
    wanted = params.get("device_id")
    if wanted in (None, ""):
        return device_ids
    if wanted not in device_ids:
        raise Refusal(400, "invalid_device", "device_id is not a device of this dashboard", device_id=str(wanted)[:128])
    return [wanted]


def _event_view(row: Dict[str, Any]) -> Dict[str, Any]:
    event = _plain(row.get("event") or {})
    return {"eventId": row.get("eventId"), "type": row.get("type"), "ts": event.get("ts"), "eventMs": _plain(row.get("eventMs")),
            "receivedMs": _plain(row.get("receivedMs")), "tsSource": row.get("tsSource"), "deviceId": row.get("deviceId"),
            "workflowVersion": event.get("workflowVersion"), "severity": event.get("severity"),
            "executionId": event.get("executionId"), "data": event.get("data")}


def _paged_query(table: Any, kwargs: Dict[str, Any], page: int, start_key: Optional[Dict[str, Any]],
                 accept: Callable[[Dict[str, Any]], bool]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Read at most 200 items (one Query), keep the accepted ones up to a
    page; the cursor is the key of the last returned item."""
    key_names = kwargs.pop("_key_names", ("pk", "sk"))
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    kwargs["Limit"] = EVENTS_READ_LIMIT
    response = table.query(**kwargs)
    rows = [r for r in response.get("Items", []) or [] if accept(r)]
    last_key = response.get("LastEvaluatedKey")
    if len(rows) > page:
        rows = rows[:page]
        last = rows[-1]
        last_key = {name: last[name] for name in key_names if name in last}
    return rows, last_key


def get_events(event: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    now_ms = _now_ms()
    from_ms, to_ms = _window(params, now_ms, EVENTS_WINDOW_DEFAULT_MS, EVENTS_WINDOW_MAX_MS)
    device_ids = set(_device_filter(params, item))
    event_type = params.get("type")
    if event_type not in (None, "") and event_type not in EVENT_TYPES:
        raise Refusal(400, "invalid_type", "type must be one of {0}".format(", ".join(EVENT_TYPES)), field="type")
    partition = records.by_workflow_pk(str(usecase["usecase_id"]), str(item["workflow_id"]))
    start_key = _decode_cursor(params.get("cursor"), "gsi1pk", partition)
    kwargs: Dict[str, Any] = {
        "IndexName": "by-workflow",
        "KeyConditionExpression": Key("gsi1pk").eq(partition) & Key("gsi1sk").between("{0:013d}".format(from_ms), "{0:013d}~".format(to_ms)),
        "ScanIndexForward": False,
        "FilterExpression": Attr("expires_at").gt(now_ms // 1000),
        "_key_names": ("pk", "sk", "gsi1pk", "gsi1sk"),
    }

    def accept(row: Dict[str, Any]) -> bool:
        if row.get("deviceId") not in device_ids:
            return False
        return not event_type or row.get("type") == event_type

    rows, last_key = _paged_query(_events(), kwargs, EVENTS_PAGE, start_key, accept)
    return _ok(200, {"events": [_event_view(r) for r in rows], "cursor": _encode_cursor(last_key), "from": from_ms, "to": to_ms})


# --------------------------------------------------------------------------- clips
def _clip_ref(sk: str) -> str:
    return base64.urlsafe_b64encode(sk.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_clip_ref(ref: str) -> str:
    try:
        return base64.urlsafe_b64decode(ref.encode("ascii") + b"=" * (-len(ref) % 4)).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        raise Refusal(404, "clip_not_found", "Clip not found")


def _clip_state(entry: Dict[str, Any], head: Optional[str]) -> Tuple[str, Optional[str]]:
    media_ref = _plain(entry.get("media") or {})
    if media_ref.get("state") == "upload_failed":
        return "upload_failed", None
    if media_ref.get("state") == "not_allowed":
        return "not_allowed", media_ref.get("reason")
    if head in (None, "found", "unchecked"):
        return "playable", None
    if head == "missing":
        return "expired_or_missing", None
    return "unavailable", head


def _head_clips(usecase: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, str]:
    """At most 8 HEADs in parallel within a 2 s budget; ``found`` / ``missing`` /
    ``forbidden`` / ``error`` per clip sort key; unchecked entries are absent."""
    candidates = [e for e in entries if _plain(e.get("media") or {}).get("state") == "bound"][:HEAD_PARALLEL]
    if not candidates:
        return {}
    bucket = media.results_bucket(usecase)
    try:
        s3 = credentials.client("s3", usecase, "media-read", bucket=bucket)
    except Exception as error:  # noqa: BLE001 - no credential, no HEADs: entries stay unchecked
        logger.warning("clip HEAD credentials unavailable: %s", error.__class__.__name__)
        return {}

    def head(entry: Dict[str, Any]) -> Tuple[str, str]:
        key = _plain(entry["media"])["key"]
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return str(entry["sk"]), "found"
        except ClientError as error:
            code = str((error.response.get("Error") or {}).get("Code") or "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return str(entry["sk"]), "missing"
            if code in ("403", "AccessDenied", "Forbidden"):
                try:
                    listed = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
                    if not listed.get("KeyCount"):
                        return str(entry["sk"]), "missing"
                except (ClientError, BotoCoreError):
                    pass
                return str(entry["sk"]), "forbidden"
            return str(entry["sk"]), "error"
        except BotoCoreError:
            return str(entry["sk"]), "error"

    results: Dict[str, str] = {}
    executor = ThreadPoolExecutor(max_workers=HEAD_PARALLEL, thread_name_prefix="clip-head")
    try:
        futures = [executor.submit(head, entry) for entry in candidates]
        done, _pending = wait(futures, timeout=HEAD_BUDGET_SECONDS)
        for future in done:
            try:
                sk, state = future.result()
                results[sk] = state
            except Exception:  # noqa: BLE001 - an unchecked entry shows as playable
                continue
    finally:
        executor.shutdown(wait=False)
    return results


def _clip_view(entry: Dict[str, Any], state: str, reason: Optional[str]) -> Dict[str, Any]:
    media_ref = _plain(entry.get("media") or {})
    view = {"clip_ref": _clip_ref(str(entry["sk"])), "clipId": entry.get("clipId"), "deviceId": entry.get("deviceId"),
            "workflowVersion": entry.get("workflowVersion"), "startTs": _plain(entry.get("startTs")),
            "endTs": _plain(entry.get("endTs")), "durationS": _plain(entry.get("durationS")),
            "reasons": _plain(entry.get("reasons") or {}), "totals": _plain(entry.get("totals") or {}),
            "file": _plain(entry.get("file") or {}), "revision": _plain(entry.get("revision")), "state": state,
            "receivedMs": _plain(entry.get("receivedMs")), "hasSidecar": bool(media_ref.get("sidecarKey"))}
    if state == "upload_failed":
        view["s3Error"] = entry.get("s3Error")
    if reason:
        view["reason"] = reason
    if state == "unavailable":
        view["note"] = ("An override bucket is readable only when it is tagged dda-portal:managed=true (cross-account) or "
                        "once the data-plane grant is wired (Portal account).")
    return view


def get_clips(event: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    now_ms = _now_ms()
    from_ms, to_ms = _window(params, now_ms, _value("TELEMETRY_CLIP_RETENTION_DAYS") * 24 * 3600 * 1000,
                             400 * 24 * 3600 * 1000)
    device_ids = set(_device_filter(params, item))
    reason = params.get("reason")
    if reason not in (None, "") and (not isinstance(reason, str) or len(reason) > 64):
        raise Refusal(400, "invalid_query", "reason is too long", field="reason")
    partition = records.clip_index_pk(str(usecase["usecase_id"]), str(item["workflow_id"]))
    start_key = _decode_cursor(params.get("cursor"), "pk", partition)
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(partition) & Key("sk").between("{0:013d}".format(from_ms), "{0:013d}~".format(to_ms)),
        "ScanIndexForward": False,
        "FilterExpression": Attr("expires_at").gt(now_ms // 1000),
        "_key_names": ("pk", "sk"),
    }

    def accept(row: Dict[str, Any]) -> bool:
        if row.get("deviceId") not in device_ids:
            return False
        return not reason or reason in (_plain(row.get("reasons")) or {})

    rows, last_key = _paged_query(_events(), kwargs, CLIPS_PAGE, start_key, accept)
    heads = _head_clips(usecase, rows)
    clips = []
    for row in rows:
        state, detail = _clip_state(row, heads.get(str(row["sk"])))
        clips.append(_clip_view(row, state, detail))
    return _ok(200, {"clips": clips, "cursor": _encode_cursor(last_key), "from": from_ms, "to": to_ms})


def _audit_clip_media(event: Dict[str, Any], user: Dict[str, Any], usecase_id: str, dashboard_id: str, clip_sk: str) -> None:
    key = (user["user_id"], clip_sk)
    now = time.time()
    with _lock:
        last = _clip_audits.get(key)
        if last is not None and now - last < CLIP_AUDIT_SECONDS:
            return
        _clip_audits[key] = now
    log_audit_event(user_id=user["user_id"], action="clip_media_minted", resource_type="dashboard", resource_id=dashboard_id,
                    result="success", details={"usecase_id": usecase_id, "clip": clip_sk}, event=event)


def get_clip_media(event: Dict[str, Any], user: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any], clip_ref: str) -> Dict[str, Any]:
    _guard()
    usecase_id = str(usecase["usecase_id"])
    sk = _decode_clip_ref(clip_ref)
    partition = records.clip_index_pk(usecase_id, str(item["workflow_id"]))
    entry = _events().get_item(Key={"pk": partition, "sk": sk}).get("Item")
    if not entry or entry.get("deviceId") not in set(item.get("device_ids") or []) or not configdb.is_unexpired(entry):
        raise Refusal(404, "clip_not_found", "Clip not found")
    media_ref = _plain(entry.get("media") or {})
    if media_ref.get("state") != "bound":
        raise Refusal(409, "clip_media_unavailable", "The clip has no readable media", reason=media_ref.get("reason") or media_ref.get("state"))
    device_id = str(entry["deviceId"])
    record = {"usecase_id": usecase_id, "device_id": device_id, "workflow_id": str(item["workflow_id"])}
    binding = media.bind(usecase, record, "s3://{0}/{1}".format(media_ref.get("bucket"), media_ref.get("key")), audit=True,
                         actor=user, event=event)
    if not binding.ok:
        raise Refusal(409, "clip_media_unavailable", "The clip's media reference is not allowed", reason=binding.reason)
    clip_id = str(entry.get("clipId") or "clip")
    if not _CLIP_FILE_RE.match(clip_id):
        clip_id = "clip"
    s3 = credentials.client("s3", usecase, "media-read", bucket=binding.bucket)
    video_url = s3.generate_presigned_url("get_object", Params={"Bucket": binding.bucket, "Key": binding.key,
                                                                "ResponseContentType": "video/mp4"}, ExpiresIn=CLIP_PRESIGN_SECONDS)
    download_url = s3.generate_presigned_url("get_object", Params={
        "Bucket": binding.bucket, "Key": binding.key, "ResponseContentType": "video/mp4",
        "ResponseContentDisposition": 'attachment; filename="{0}.mp4"'.format(clip_id)}, ExpiresIn=CLIP_PRESIGN_SECONDS)
    sidecar_url = None
    sidecar_key = media_ref.get("sidecarKey")
    if isinstance(sidecar_key, str) and media.bind_key(usecase, "clips", usecase_id, device_id, str(item["workflow_id"]),
                                                       binding.bucket, sidecar_key).ok:
        sidecar_url = s3.generate_presigned_url("get_object", Params={"Bucket": binding.bucket, "Key": sidecar_key,
                                                                      "ResponseContentType": "application/json"},
                                                ExpiresIn=CLIP_PRESIGN_SECONDS)
    _audit_clip_media(event, user, usecase_id, str(item["dashboard_id"]), sk)
    return _ok(200, {"video_url": video_url, "download_url": download_url, "sidecar_url": sidecar_url,
                     "expires_in": CLIP_PRESIGN_SECONDS, "clipId": entry.get("clipId")})


# --------------------------------------------------------------------------- live lease
def _dashboard_device(body_or_params: Dict[str, Any], item: Dict[str, Any]) -> str:
    device_id = body_or_params.get("device_id")
    if not isinstance(device_id, str) or device_id not in set(item.get("device_ids") or []):
        raise Refusal(400, "invalid_device", "device_id must be a device of this dashboard", field="device_id")
    return device_id


def _media_retention_state(usecase: Dict[str, Any], settings: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    items = {i["id"]: i for i in _checklist(usecase, settings)}
    item = items.get("media_retention") or {}
    if item.get("ready"):
        return True, None
    return False, item.get("reason") or retention.MEDIA_RETENTION_UNSET


def post_live_lease(event: Dict[str, Any], user: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    _guard()
    body = _parse_body(event, "invalid_device")
    device_id = _dashboard_device(body, item)
    usecase_id = str(usecase["usecase_id"])
    workflow_id = str(item["workflow_id"])
    dashboard_id = str(item["dashboard_id"])
    settings = _plain(_settings_item(usecase_id)) or {}
    if not settings.get("enabled"):
        raise Refusal(409, "telemetry_disabled", "Telemetry is not enabled for this use case",
                      "A UseCaseAdmin enables telemetry on the use case's telemetry settings page.")
    ready, reason = _media_retention_state(usecase, settings)
    if not ready:
        raise Refusal(409, "live_unavailable", "Live frames need the media lifecycle rules on an unversioned bucket",
                      readiness.REMEDIES["media_retention"], reason=reason)
    now_ms = _now_ms()
    lease = leases.acquire(_config(), usecase_id, dashboard_id, user["user_id"], device_id, workflow_id, now_ms)
    if not lease.granted:
        if lease.reason == "live_viewing_limit":
            raise Refusal(409, "live_viewing_limit", "The live tile pauses after {0} continuous minutes".format(leases.max_view_minutes()),
                          "Resume the tile to start a new viewing period.", max_minutes=leases.max_view_minutes())
        raise Refusal(429, "live_capacity_reached", "No live slot is available", "Wait for another viewer's lease to lapse.",
                      reason=lease.reason, max=leases.max_leases() if lease.reason == "usecase_slots" else leases.DEVICE_LIVE_LIMIT)
    location = settings.get("media_location") or media.media_location(usecase)
    region = location.get("region")
    if not region:
        probe = readiness.probe_media_location(usecase, location["bucket"])
        region = probe.region or shared_utils.get_usecase_region(usecase)
    entry = shadow.lease_entry(usecase_id, device_id, workflow_id, lease.pair_expires_ms, location["bucket"], region)
    result = shadow.write_lease(usecase, device_id, workflow_id, entry)
    if result.get("result") != shadow.RESULT_OK:
        leases.release(_config(), usecase_id, dashboard_id, user["user_id"])
        raise Refusal(502, "live_lease_failed", "The device's telemetry shadow could not be written", LEASE_FAILED_REMEDY,
                      reason=result.get("error"))
    if lease.first_grant:
        log_audit_event(user_id=user["user_id"], action="live_lease_granted", resource_type="dashboard", resource_id=dashboard_id,
                        result="success", details={"usecase_id": usecase_id, "device_id": device_id, "workflow_id": workflow_id,
                                                   "slot": lease.slot}, event=event)
    return _ok(200, {"device_id": device_id, "workflow_id": workflow_id, "slot": lease.slot, "startedAt": lease.started_at,
                     "leaseExpiresAt": lease.lease_expires_ms, "expiresAt": lease.pair_expires_ms,
                     "renewSeconds": CADENCE["liveLease"], "region": region, "region_source": location.get("region_source")})


# --------------------------------------------------------------------------- live frames
def get_live(event: Dict[str, Any], item: Dict[str, Any], usecase: Dict[str, Any]) -> Dict[str, Any]:
    _guard()
    params = event.get("queryStringParameters") or {}
    device_id = _dashboard_device(params, item)
    have = params.get("have")
    if have not in (None, "") and (not isinstance(have, str) or len(have) > 80 or not _TOKEN_RE.match(have)):
        raise Refusal(400, "invalid_token", "have must be the previous answer's frame_token", field="have")
    usecase_id = str(usecase["usecase_id"])
    workflow_id = str(item["workflow_id"])
    now_ms = _now_ms()
    pk = records.pair_pk(usecase_id, device_id, workflow_id)
    pointer = _summaries().get_item(Key={"pk": pk, "sk": records.LIVE_SK}).get("Item")
    if not pointer or not configdb.is_unexpired(pointer, now_ms / 1000.0) \
            or now_ms - int(pointer.get("receivedMs") or 0) > LIVE_POINTER_WINDOW_MS:
        return _ok(200, {"frame": None, "frame_token": None, "state": "no_live_source"})
    token = str(pointer.get("frameToken") or records.frame_token(str(pointer.get("instance")), int(pointer.get("seq") or 0)))
    age_s = max(0, (now_ms - int(pointer.get("receivedMs") or 0)) // 1000)
    if have and have == token:
        return _ok(200, {"frame": None, "frame_token": token, "age_s": age_s})
    binding = media.bind_key(usecase, "live", usecase_id, device_id, workflow_id, pointer.get("bucket"), pointer.get("key"))
    if not binding.ok:
        return _ok(200, {"frame": None, "frame_token": have, "age_s": age_s, "unavailable": "not_allowed", "reason": binding.reason})
    s3 = credentials.client("s3", usecase, "media-read", bucket=binding.bucket)
    try:
        head = s3.head_object(Bucket=binding.bucket, Key=binding.key)
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        logger.info("live frame HEAD refused for %s: %s", device_id, code)
        return _ok(200, {"frame": None, "frame_token": have, "age_s": age_s})
    seq_meta = str((head.get("Metadata") or {}).get("seq", ""))
    if seq_meta != str(int(pointer.get("seq") or 0)):
        return _ok(200, {"frame": None, "frame_token": have, "age_s": age_s})
    if int(head.get("ContentLength") or 0) > constants.LIVE_LEASE_DEFAULTS["maxBytes"] or str(head.get("ContentType") or "") != "image/jpeg":
        return _ok(200, {"frame": None, "frame_token": token, "age_s": age_s, "unavailable": "frame_too_large"})
    url = s3.generate_presigned_url("get_object", Params={"Bucket": binding.bucket, "Key": binding.key,
                                                          "ResponseContentType": "image/jpeg", "ResponseCacheControl": "no-store"},
                                    ExpiresIn=FRAME_PRESIGN_SECONDS)
    frame = {"url": url, "width": _plain(pointer.get("width")), "height": _plain(pointer.get("height")), "ts": _plain(pointer.get("ts")),
             "age_s": age_s, "perClass": _plain(pointer.get("perClass") or {}), "people": _plain(pointer.get("people")),
             "violation": _plain(pointer.get("violation")), "reasons": _plain(pointer.get("reasons") or {}), "expires_in": FRAME_PRESIGN_SECONDS}
    return _ok(200, {"frame": frame, "frame_token": token, "age_s": age_s})


# --------------------------------------------------------------------------- dispatch
def _route(event: Dict[str, Any]) -> Dict[str, Any]:
    method = str(event.get("httpMethod") or "").upper()
    resource = str(event.get("resource") or "")
    path_params = event.get("pathParameters") or {}
    if method == "OPTIONS":
        return create_response(200, "")
    user = get_user_from_event(event)
    if resource == "/telemetry/schemas" and method == "GET":
        return get_schemas()
    if resource == "/telemetry/rejections" and method == "GET":
        return _authorize_portal_admin(event, user) or get_account_rejections(event)
    if resource.startswith("/telemetry/usecases/{usecase_id}/"):
        usecase_id = str(path_params.get("usecase_id") or "")
        if not _UUID_RE.match(usecase_id) and not _ID_RE.match(usecase_id):
            return _error(400, "invalid_query", "usecase_id is invalid", field="usecase_id")
        if resource.endswith("/settings"):
            denied = _authorize(event, user, usecase_id, Permission.TELEMETRY_MANAGE)
            if denied:
                return denied
            if method == "GET":
                return get_settings(usecase_id)
            if method == "PUT":
                return put_settings(event, user, usecase_id)
        elif resource.endswith("/templates") and method == "GET":
            return _authorize(event, user, usecase_id, Permission.DASHBOARDS_MANAGE) or get_templates(event, usecase_id)
        elif resource.endswith("/rejections") and method == "GET":
            return _authorize(event, user, usecase_id, Permission.TELEMETRY_MANAGE) or get_usecase_rejections(usecase_id)
    elif resource == "/telemetry/dashboards":
        if method == "GET":
            usecase_id = str((event.get("queryStringParameters") or {}).get("usecase_id") or "")
            if not usecase_id:
                return _error(400, "invalid_query", "usecase_id is required", field="usecase_id")
            return _authorize(event, user, usecase_id, Permission.DASHBOARDS_READ) or list_dashboards(event, usecase_id)
        if method == "POST":
            body = _parse_body(event, "invalid_dashboard")
            usecase_id = body.get("usecase_id")
            if not isinstance(usecase_id, str) or not usecase_id:
                return _error(400, "invalid_dashboard", "usecase_id is required", field="usecase_id")
            denied = _authorize(event, user, usecase_id, Permission.DASHBOARDS_MANAGE)
            if denied:
                return denied
            return create_dashboard(event, user, body, _usecase_or_404(usecase_id))
    elif resource.startswith("/telemetry/dashboards/{dashboard_id}"):
        item = _load_dashboard(str(path_params.get("dashboard_id") or ""))
        usecase_id = str(item["usecase_id"])
        tail = resource[len("/telemetry/dashboards/{dashboard_id}"):]
        if tail == "":
            if method == "GET":
                return _authorize(event, user, usecase_id, Permission.DASHBOARDS_READ) or _ok(200, {"dashboard": _dashboard_view(item)})
            if method in ("PATCH", "DELETE"):
                denied = _authorize(event, user, usecase_id, Permission.DASHBOARDS_MANAGE)
                if denied:
                    return denied
                usecase = _usecase_or_404(usecase_id)
                return patch_dashboard(event, user, item, usecase) if method == "PATCH" else delete_dashboard(event, user, item)
        else:
            denied = _authorize(event, user, usecase_id, Permission.DASHBOARDS_READ)
            if denied:
                return denied
            usecase = _usecase_or_404(usecase_id)
            if tail == "/now" and method == "GET":
                return get_now(user, item, usecase)
            if tail == "/series" and method == "GET":
                return get_series(event, item, usecase)
            if tail == "/events" and method == "GET":
                return get_events(event, item, usecase)
            if tail == "/clips" and method == "GET":
                return get_clips(event, item, usecase)
            if tail == "/clips/{clip_ref}/media" and method == "GET":
                return get_clip_media(event, user, item, usecase, str(path_params.get("clip_ref") or ""))
            if tail == "/live-lease" and method == "POST":
                return post_live_lease(event, user, item, usecase)
            if tail == "/live" and method == "GET":
                return get_live(event, item, usecase)
    return _error(404, "not_found", "Not found")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        return _route(event)
    except Refusal as refusal:
        return refusal.response
    except Exception as error:  # noqa: BLE001 - the one catch-all of the entry point
        logger.exception("telemetry API failure: %s", error.__class__.__name__)
        return _error(500, "internal_error", "The telemetry API failed")
