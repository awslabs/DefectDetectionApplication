"""
Workflow debug maintenance (workflow-debug-mode design §7.4), run every
5 minutes by EventBridge Scheduler.

For each session in state ``pending`` or ``active``:

- **expired** (``expiresAt < now``): write ``null`` to each device's shadow
  entry (best effort), set ``expired`` on META and the ``PAIR#`` items,
  delete the ``PAIRLOCK`` markers and decrement the counters in one
  transaction, audit ``debug_session_expired``;
- otherwise retry the ``pending`` shadow writes that failed (the entry is
  rebuilt from META's options and media, so it equals the one the API wrote)
  and move the session to ``active`` once every write is ``ok``; and recreate
  the session's log group when it is missing (``CreateLogGroup`` answers
  ``ResourceAlreadyExistsException`` for a present group, which costs
  nothing; a created group gets the retention policy and the use-case tag).

``SEQ#`` items expire by TTL. No log-stream housekeeping: the retention
policy expires every event and an empty stream is free. Nothing else runs
when no session exists.

Live sessions are found with a filtered ``Scan`` of the (small) sessions
table: the ``by-usecase`` index needs a use case id to query, and no table
the maintenance role may read lists the use cases that hold sessions.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

import shared_utils
from shared_utils import log_audit_event
from workflow_debug import logs_writer, sessions, shadow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SYSTEM_ACTOR = "system"

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


def _ddb_client():
    global _dynamodb_client
    with _lock:
        if _dynamodb_client is None:
            _dynamodb_client = boto3.client("dynamodb")
        return _dynamodb_client


def _table():
    return _dynamodb().Table(os.environ["WORKFLOW_DEBUG_SESSIONS_TABLE"])


def _table_name() -> str:
    return os.environ["WORKFLOW_DEBUG_SESSIONS_TABLE"]


def reset_clients() -> None:
    global _logs_client, _dynamodb_resource, _dynamodb_client
    with _lock:
        _logs_client = None
        _dynamodb_resource = None
        _dynamodb_client = None


def _retention_days() -> int:
    try:
        return int(os.environ.get("DEBUG_LOG_RETENTION_DAYS") or 14)
    except ValueError:
        return 14


# --------------------------------------------------------------------------- reads
def live_sessions() -> List[Dict[str, Any]]:
    """Every META item in a live state (a filtered, paginated Scan)."""
    table = _table()
    kwargs: Dict[str, Any] = {
        "FilterExpression": Attr("sk").eq(sessions.META_SK) & Attr("state").is_in(list(sessions.LIVE_STATES)),
    }
    found: List[Dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        found.extend(response.get("Items", []) or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    found.sort(key=lambda m: (int(m.get("expiresAt") or 0), str(m.get("session_id"))))
    return found


def _pairs(session_id: str) -> List[Dict[str, Any]]:
    response = _table().query(
        KeyConditionExpression=Key("pk").eq(sessions.session_pk(session_id)) & Key("sk").begins_with("PAIR#"))
    pairs = list(response.get("Items", []) or [])
    pairs.sort(key=lambda p: str(p.get("device_id") or p.get("sk")))
    return pairs


def _usecase(usecase_id: str):
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        return None


def _device_of(pair: Dict[str, Any]) -> str:
    return str(pair.get("device_id") or sessions.device_of_pair_sk(pair.get("sk")))


# --------------------------------------------------------------------------- the three duties
def expire_session(meta: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    session_id = str(meta["session_id"])
    usecase_id = str(meta.get("usecase_id"))
    workflow_id = str(meta.get("workflow_id"))
    pairs = _pairs(session_id)
    devices = [_device_of(p) for p in pairs]
    usecase = _usecase(usecase_id)
    shadow_results: Dict[str, Any] = {}
    if usecase is not None and devices:
        try:
            shadow_results = shadow.write_all_sessions(usecase, [(d, workflow_id, None) for d in devices])
        except Exception as error:  # noqa: BLE001 - best effort; the state change still happens
            logger.warning("shadow entries of expired session %s not cleared: %s", session_id, error.__class__.__name__)
    elif usecase is None:
        logger.warning("use case %s of expired session %s no longer exists; shadow entries not cleared",
                       usecase_id, session_id)
    try:
        _ddb_client().transact_write_items(TransactItems=sessions.serialize_actions(
            sessions.stop_actions(_table_name(), meta, pairs, new_state=sessions.EXPIRED, now_ms=now_ms)))
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code == "TransactionCanceledException":
            logger.info("session %s was stopped meanwhile; nothing to expire", session_id)
            return {"session_id": session_id, "expired": False, "reason": "not_live"}
        raise
    log_audit_event(
        user_id=SYSTEM_ACTOR, action="debug_session_expired", resource_type="debug_session",
        resource_id=session_id, result="success",
        details={"usecase_id": usecase_id, "workflow_id": workflow_id, "devices": devices,
                 "expiresAt": int(meta.get("expiresAt") or 0),
                 "shadow": {d: r.get("result") for d, r in shadow_results.items()}})
    logger.info("debug session %s expired (%d devices)", session_id, len(devices))
    return {"session_id": session_id, "expired": True, "devices": devices}


def retry_pending_writes(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite the shadow entry of every device whose write is not ``ok``;
    activate the session once every device is ``ok``."""
    session_id = str(meta["session_id"])
    pairs = _pairs(session_id)
    failed = [p for p in pairs if (p.get("shadow") or {}).get("result") != shadow.RESULT_OK]
    if not failed:
        return {"session_id": session_id, "retried": 0}
    usecase = _usecase(str(meta.get("usecase_id")))
    if usecase is None:
        return {"session_id": session_id, "retried": 0, "reason": "usecase_missing"}
    options = meta.get("options") or {}
    media = meta.get("media") or {}
    workflow_id = str(meta.get("workflow_id"))
    items = []
    for pair in failed:
        device_id = _device_of(pair)
        entry = shadow.session_entry(
            session_id=session_id, usecase_id=str(meta.get("usecase_id")), device_id=device_id,
            workflow_id=workflow_id, enabled_at=int(meta.get("enabledAt") or 0),
            expires_at=int(meta.get("expiresAt") or 0), level=str(options.get("level") or "debug"),
            images=bool(options.get("images")), image_max_kb=int(options.get("imageMaxKb") or 150),
            bucket=str(media.get("bucket") or ""), region=str(media.get("region") or shared_utils.get_usecase_region(usecase)),
            max_messages_per_second=int(options.get("maxMessagesPerSecond") or 10),
            max_bytes_per_second=int(options.get("maxBytesPerSecond") or 262144))
        items.append((device_id, workflow_id, entry))
    results = shadow.write_all_sessions(usecase, items)
    ok_devices = {_device_of(p) for p in pairs if (p.get("shadow") or {}).get("result") == shadow.RESULT_OK}
    ok_devices.update(d for d, r in results.items() if r.get("result") == shadow.RESULT_OK)
    activate = meta.get("state") == sessions.PENDING and ok_devices == {_device_of(p) for p in pairs}
    try:
        _ddb_client().transact_write_items(TransactItems=sessions.serialize_actions(
            sessions.shadow_result_actions(_table_name(), session_id, results, activate=activate,
                                           all_devices=[_device_of(p) for p in pairs])))
    except ClientError as error:
        logger.warning("shadow retry results not recorded for session %s: %s", session_id,
                       (error.response.get("Error") or {}).get("Code"))
        activate = False
    logger.info("debug session %s: %d shadow writes retried, %d ok%s", session_id, len(results),
                sum(1 for r in results.values() if r.get("result") == shadow.RESULT_OK),
                ", now active" if activate else "")
    return {"session_id": session_id, "retried": len(results), "activated": activate,
            "results": {d: r.get("result") for d, r in results.items()}}


def recreate_group_if_missing(meta: Dict[str, Any]) -> bool:
    """``CreateLogGroup`` for the session's group; a present group answers
    ``ResourceAlreadyExistsException`` and nothing else happens. A created
    (so, previously missing) group gets the retention policy and the tag."""
    usecase_id = str(meta.get("usecase_id"))
    group = str(meta.get("logGroup") or logs_writer.log_group_name(usecase_id))
    logs = _logs()
    try:
        logs.create_log_group(logGroupName=group)
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code == "ResourceAlreadyExistsException":
            return False
        logger.error("log group %s could not be recreated: %s", group, code)
        return False
    logs.put_retention_policy(logGroupName=group, retentionInDays=_retention_days())
    logs.tag_resource(resourceArn=logs_writer.log_group_arn(group), tags={logs_writer.USECASE_TAG: usecase_id})
    logger.warning("log group %s was missing and has been recreated by maintenance", group)
    return True


# --------------------------------------------------------------------------- entry point
def run(now_ms: int = None) -> Dict[str, Any]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    summary: Dict[str, Any] = {"live": 0, "expired": [], "retried": 0, "activated": [],
                               "recreated_groups": 0, "errors": 0}
    for meta in live_sessions():
        summary["live"] += 1
        session_id = str(meta.get("session_id"))
        try:
            if int(meta.get("expiresAt") or 0) < now_ms:
                outcome = expire_session(meta, now_ms)
                if outcome.get("expired"):
                    summary["expired"].append(session_id)
                continue
            retried = retry_pending_writes(meta)
            summary["retried"] += int(retried.get("retried") or 0)
            if retried.get("activated"):
                summary["activated"].append(session_id)
            if recreate_group_if_missing(meta):
                summary["recreated_groups"] += 1
        except Exception as error:  # noqa: BLE001 - one session's failure never stops the others
            summary["errors"] += 1
            logger.error("maintenance step failed for session %s: %s", session_id, error.__class__.__name__)
    return summary


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    return run()
