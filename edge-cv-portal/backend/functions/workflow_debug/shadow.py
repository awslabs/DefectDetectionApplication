"""Writing and reading the Debug_Shadow ``dda-workflow-debug``
(workflow-debug-mode design §3, §7.3).

The Portal writes one ``desired.sessions.<workflowId>`` entry per (session,
device); ``null`` removes it (the stop). Shadow updates merge nested maps, so
one write never disturbs another workflow's session. Every call goes through
``credentials.client('iot-data', U, 'debug-control')`` (the function's own
role in the ``portal`` topology, ``DDAPortalAccessRole`` with the
``debug-control`` session policy in ``cross``), and the writes of one request
run on the shared ``shadow_pool``.
"""
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from edge_telemetry_common import credentials, shadow_pool

__all__ = [
    "SHADOW_NAME", "DEBUG_IMAGE_PREFIX", "ShadowResult", "prefix_for", "session_entry",
    "desired_document", "classify_error", "write_session", "write_all_sessions", "read_reported",
]

SHADOW_NAME = "dda-workflow-debug"
DEBUG_IMAGE_PREFIX = "dda-telemetry/debug/"
RESULT_OK = "ok"
RESULT_FAILED = "failed"

_DENIED = frozenset({"AccessDeniedException", "AccessDenied", "UnauthorizedException", "ForbiddenException", "Forbidden"})
_THROTTLED = frozenset({"ThrottlingException", "Throttling", "ThrottledException", "TooManyRequestsException"})
_NOT_FOUND = frozenset({"ResourceNotFoundException", "ResourceNotFound"})

ShadowResult = Dict[str, Any]


def prefix_for(usecase_id: str, device_id: str, workflow_id: str, session_id: str) -> str:
    """The image prefix the device validates its entry against (§3)."""
    return "{0}{1}/{2}/{3}/{4}/".format(DEBUG_IMAGE_PREFIX, usecase_id, device_id, workflow_id, session_id)


def session_entry(*, session_id: str, usecase_id: str, device_id: str, workflow_id: str, enabled_at: int,
                  expires_at: int, level: str, images: bool, image_max_kb: int, bucket: str, region: str,
                  max_messages_per_second: int, max_bytes_per_second: int) -> Dict[str, Any]:
    """One ``desired.sessions.<workflowId>`` entry of §3; the Portal fills
    ``usecaseId``, ``bucket``, ``region`` and ``prefix`` itself."""
    return {
        "sessionId": session_id,
        "usecaseId": usecase_id,
        "enabledAt": int(enabled_at),
        "expiresAt": int(expires_at),
        "level": level,
        "images": bool(images),
        "imageMaxKb": int(image_max_kb),
        "bucket": bucket,
        "region": region,
        "prefix": prefix_for(usecase_id, device_id, workflow_id, session_id),
        "maxMessagesPerSecond": int(max_messages_per_second),
        "maxBytesPerSecond": int(max_bytes_per_second),
    }


def desired_document(workflow_id: str, entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``{"state": {"desired": {"v": 1, "sessions": {<wf>: entry | null}}}}``."""
    return {"state": {"desired": {"v": 1, "sessions": {workflow_id: entry}}}}


def classify_error(error: BaseException) -> str:
    """``thing_not_found`` | ``access_denied`` | ``throttled`` | the error text (<= 200 characters)."""
    if isinstance(error, ClientError):
        code = str(((error.response or {}).get("Error") or {}).get("Code") or "")
        if code in _NOT_FOUND:
            return "thing_not_found"
        if code in _DENIED:
            return "access_denied"
        if code in _THROTTLED:
            return "throttled"
        return (code or str(error))[:200]
    return str(error)[:200] or error.__class__.__name__


def write_session(usecase: Dict[str, Any], device_id: str, workflow_id: str, entry: Optional[Dict[str, Any]],
                  *, client: Any = None, sleep=time.sleep) -> ShadowResult:
    """``UpdateThingShadow`` of the device's entry (or ``null``); a throttled
    call is retried once. Returns ``{result: 'ok'}`` or
    ``{result: 'failed', error: <reason>}`` with ``writtenAt``."""
    client = client if client is not None else credentials.client("iot-data", usecase, "debug-control")
    payload = json.dumps(desired_document(workflow_id, entry), separators=(",", ":")).encode("utf-8")
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


def write_all_sessions(usecase: Dict[str, Any], items: Iterable[Tuple[str, str, Optional[Dict[str, Any]]]],
                       *, client: Any = None) -> Dict[str, ShadowResult]:
    """``{device_id: ShadowResult}`` for ``(device_id, workflow_id, entry_or_null)``
    items, written through the shared pool (8 threads, 2 s per write)."""
    item_list = list(items)
    if not item_list:
        return {}
    client = client if client is not None else credentials.client("iot-data", usecase, "debug-control")

    def one(item):
        device_id, workflow_id, entry = item
        return write_session(usecase, device_id, workflow_id, entry, client=client)

    results: Dict[str, ShadowResult] = {}
    for outcome in shadow_pool.write_all(item_list, one):
        device_id = outcome.item[0]
        if outcome.ok and isinstance(outcome.value, dict):
            results[device_id] = outcome.value
        else:
            results[device_id] = {"result": RESULT_FAILED, "error": (outcome.error or "unknown")[:200],
                                  "writtenAt": int(time.time() * 1000)}
    return results


def read_reported(usecase: Dict[str, Any], device_id: str, workflow_id: str, *,
                  client: Any = None) -> Optional[Dict[str, Any]]:
    """The device's ``reported.sessions.<workflowId>`` from ``GetThingShadow``,
    or ``None`` when the shadow, the ``reported`` map or the entry is absent.
    Raises nothing: a failed read is ``None``."""
    client = client if client is not None else credentials.client("iot-data", usecase, "debug-control")
    try:
        response = client.get_thing_shadow(thingName=device_id, shadowName=SHADOW_NAME)
        body = response.get("payload")
        raw = body.read() if hasattr(body, "read") else body
        document = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ClientError, BotoCoreError, ValueError, AttributeError, TypeError):
        return None
    reported = ((document.get("state") or {}).get("reported") or {})
    sessions = reported.get("sessions") or {}
    entry = sessions.get(workflow_id)
    if not isinstance(entry, dict):
        return None
    result = dict(entry)
    for key in ("instance", "agentVersion"):
        if key in reported and key not in result:
            result[key] = reported[key]
    return result
