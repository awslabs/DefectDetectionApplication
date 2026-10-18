"""Writing the Control_Shadow ``dda-workflow-telemetry``
(workflow-telemetry-dashboards design §5.4, §9.3, §10.2; tasks.md 3.4).

- ``write_desired(usecase, device_id, settings)`` builds the §5.4 document
  without ``live`` (``v``, ``usecaseId``, ``enabled``, ``summaryIntervalS``,
  ``idleHeartbeatS``, ``personClasses`` = the constant ``PERSON_CLASSES``,
  ``updatedAt``), calls ``UpdateThingShadow`` through
  ``credentials.client('iot-data', U, 'api-control')`` and records
  ``DEVSHADOW#<device>`` (the desired hash, the result, the error text) in
  the config table;
- ``write_lease(usecase, device_id, workflow_id, entry_or_null)`` merges one
  ``desired.live.<workflowId>`` entry (``null`` removes it; the device learns
  of the removal through ``update/documents``);
- ``lease_entry`` is the lease's shadow entry with ``region`` from
  ``media_location.region`` (the bucket's own region, §9.3);
- the writes of one request run on the shared ``shadow_pool``.
"""
import hashlib
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from edge_telemetry_common import credentials, shadow_pool
from telemetry import configdb, constants, media

__all__ = [
    "SHADOW_NAME", "RESULT_OK", "RESULT_FAILED", "PERSON_CLASSES", "desired_document", "desired_hash",
    "lease_entry", "lease_document", "classify_error", "update_shadow", "record_devshadow", "write_desired",
    "write_all_desired", "write_lease", "write_all_leases", "read_reported",
]

SHADOW_NAME = constants.SHADOW_NAME
PERSON_CLASSES = constants.PERSON_CLASSES
RESULT_OK = "ok"
RESULT_FAILED = "failed"

_DENIED = frozenset({"AccessDeniedException", "AccessDenied", "UnauthorizedException", "ForbiddenException", "Forbidden"})
_THROTTLED = frozenset({"ThrottlingException", "Throttling", "ThrottledException", "TooManyRequestsException"})
_NOT_FOUND = frozenset({"ResourceNotFoundException", "ResourceNotFound"})


def desired_document(usecase_id: str, settings: Dict[str, Any], now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The ``desired`` map of §5.4 without ``live``."""
    return {
        "v": 1,
        "usecaseId": str(usecase_id),
        "enabled": bool(settings.get("enabled", False)),
        "summaryIntervalS": int(settings.get("summaryIntervalS") or constants.DEFAULT_SUMMARY_INTERVAL_S),
        "idleHeartbeatS": int(settings.get("idleHeartbeatS") or constants.DEFAULT_IDLE_HEARTBEAT_S),
        "personClasses": list(PERSON_CLASSES),
        "updatedAt": int(now_ms if now_ms is not None else time.time() * 1000),
    }


def desired_hash(desired: Dict[str, Any]) -> str:
    """SHA-256 of the desired map without ``updatedAt`` (the settings hash the
    re-sync compares, §10.3)."""
    stable = {k: v for k, v in desired.items() if k != "updatedAt"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def lease_entry(usecase_id: str, device_id: str, workflow_id: str, expires_ms: int, bucket: str, region: str) -> Dict[str, Any]:
    """``desired.live.<workflowId>`` of §9.3."""
    entry = {"expiresAt": int(expires_ms)}
    entry.update(constants.LIVE_LEASE_DEFAULTS)
    entry.update({"bucket": bucket, "region": region,
                  "prefix": media.pair_prefix("live", usecase_id, device_id, workflow_id)})
    return entry


def lease_document(workflow_id: str, entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"state": {"desired": {"live": {workflow_id: entry}}}}


def classify_error(error: BaseException) -> str:
    """``thing_not_found`` | ``access_denied`` | ``throttled`` | the error code (<= 200 chars)."""
    if isinstance(error, ClientError):
        code = str(((error.response or {}).get("Error") or {}).get("Code") or "")
        if code in _NOT_FOUND:
            return "thing_not_found"
        if code in _DENIED:
            return "access_denied"
        if code in _THROTTLED:
            return "throttled"
        return (code or str(error))[:200]
    return (str(error) or error.__class__.__name__)[:200]


def update_shadow(client: Any, device_id: str, document: Dict[str, Any], *, sleep=time.sleep) -> Dict[str, Any]:
    """``UpdateThingShadow`` with one retry on throttling; ``{result, error?, writtenAt}``."""
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    error_text = None
    for attempt in range(2):
        try:
            client.update_thing_shadow(thingName=device_id, shadowName=SHADOW_NAME, payload=payload)
            return {"result": RESULT_OK, "writtenAt": int(time.time() * 1000)}
        except (ClientError, BotoCoreError) as error:
            error_text = classify_error(error)
            if error_text == "throttled" and attempt == 0:
                sleep(0.2)
                continue
            break
    return {"result": RESULT_FAILED, "error": error_text, "writtenAt": int(time.time() * 1000)}


def record_devshadow(table: Any, usecase_id: str, device_id: str, desired: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    item = {"pk": configdb.usecase_pk(usecase_id), "sk": configdb.devshadow_sk(device_id), "device_id": device_id,
            "desiredHash": desired_hash(desired), "writtenAt": int(result.get("writtenAt") or time.time() * 1000),
            "result": result.get("result", RESULT_FAILED)}
    if result.get("error"):
        item["error"] = str(result["error"])[:200]
    table.put_item(Item=item)
    return item


def write_desired(usecase: Dict[str, Any], device_id: str, settings: Dict[str, Any], *, table: Any = None,
                  client: Any = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Write the device's desired document and record ``DEVSHADOW#``."""
    client = client if client is not None else credentials.client("iot-data", usecase, "api-control")
    desired = desired_document(str(usecase["usecase_id"]), settings, now_ms)
    result = update_shadow(client, device_id, {"state": {"desired": desired}})
    if table is not None:
        record_devshadow(table, str(usecase["usecase_id"]), device_id, desired, result)
    result["desiredHash"] = desired_hash(desired)
    return result


def write_all_desired(usecase: Dict[str, Any], device_ids: Iterable[str], settings: Dict[str, Any], *,
                      table: Any = None, client: Any = None, now_ms: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """``{device_id: result}`` through the shared pool (8 threads, 2 s per write)."""
    devices = list(device_ids)
    if not devices:
        return {}
    client = client if client is not None else credentials.client("iot-data", usecase, "api-control")
    results: Dict[str, Dict[str, Any]] = {}
    for outcome in shadow_pool.write_all(devices, lambda d: write_desired(usecase, d, settings, table=table,
                                                                          client=client, now_ms=now_ms)):
        if outcome.ok and isinstance(outcome.value, dict):
            results[outcome.item] = outcome.value
        else:
            result = {"result": RESULT_FAILED, "error": (outcome.error or "unknown")[:200],
                      "writtenAt": int(time.time() * 1000)}
            results[outcome.item] = result
            if table is not None:
                record_devshadow(table, str(usecase["usecase_id"]), outcome.item,
                                 desired_document(str(usecase["usecase_id"]), settings, now_ms), result)
    return results


def write_lease(usecase: Dict[str, Any], device_id: str, workflow_id: str, entry: Optional[Dict[str, Any]], *,
                client: Any = None) -> Dict[str, Any]:
    client = client if client is not None else credentials.client("iot-data", usecase, "api-control")
    return update_shadow(client, device_id, lease_document(workflow_id, entry))


def write_all_leases(usecase: Dict[str, Any], items: Iterable[Tuple[str, str, Optional[Dict[str, Any]]]], *,
                     client: Any = None) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """``{(device_id, workflow_id): result}`` for ``(device, workflow, entry_or_null)`` items."""
    item_list = list(items)
    if not item_list:
        return {}
    client = client if client is not None else credentials.client("iot-data", usecase, "api-control")
    results: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for outcome in shadow_pool.write_all(item_list, lambda i: write_lease(usecase, i[0], i[1], i[2], client=client)):
        key = (outcome.item[0], outcome.item[1])
        if outcome.ok and isinstance(outcome.value, dict):
            results[key] = outcome.value
        else:
            results[key] = {"result": RESULT_FAILED, "error": (outcome.error or "unknown")[:200],
                            "writtenAt": int(time.time() * 1000)}
    return results


def read_reported(usecase: Dict[str, Any], device_id: str, *, client: Any = None) -> Optional[Dict[str, Any]]:
    """The device's ``reported`` document, or ``None`` (never raises)."""
    client = client if client is not None else credentials.client("iot-data", usecase, "api-control")
    try:
        response = client.get_thing_shadow(thingName=device_id, shadowName=SHADOW_NAME)
        body = response.get("payload")
        raw = body.read() if hasattr(body, "read") else body
        document = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ClientError, BotoCoreError, ValueError, AttributeError, TypeError):
        return None
    reported = (document.get("state") or {}).get("reported")
    return reported if isinstance(reported, dict) else None
