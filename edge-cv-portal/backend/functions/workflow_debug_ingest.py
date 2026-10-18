"""
Workflow debug ingest (workflow-debug-mode design §6.2): the Lambda action
target of the ``dda_dbg_ingest`` rules, one alias ``acct-<account>`` per
account.

The rule delivers ``{"p": <base64 payload>, "t": <topic>, "pr": <principal>,
"c": <client id>, "r": <timestamp ms>, "m": <message id>}``. For each message:

1. decode ``p`` (<= 128 KB) and parse ``t`` as ``dda/debug/<uc>/<wf>/record``;
2. attribute it exactly as the dashboards' ingest does: the sending account
   from the alias in ``invoked_function_arn``; the use case exists in that
   account and the Portal's region; ``pr`` resolves through
   ``ListPrincipalThings`` to the thing ``c`` names (less a ``#<n>`` suffix);
   the Device_Record names the topic's use case (fail-closed); the Workflow
   belongs to it; the envelope's ``deviceId``/``workflowId`` match;
3. a ``pending``/``active`` session with the envelope's ``sessionId`` covers
   the Pair (``by-pair`` index, cached 30 s), and the Pair stays under
   ``2 x maxMessagesPerSecond`` over 10 s;
4. the envelope is valid; the dedup **read** (``GetItem SEQ#<instance>#<seq>``,
   consistent) finds nothing;
5. every record is redacted again with the shared module;
6. the records are written to the ``PAIR#`` item's ``logStream`` with one
   write-time stamp per batch; a throttled write raises so Lambda retries, a
   hand-deleted group is recreated inline;
7. only after the write: the conditional ``SEQ#`` mark (10-minute TTL) and the
   session counters;
8. metrics in embedded metric format, namespace ``DDA/WorkflowDebug``.

Rejections are counted and never logged with a payload.
"""
import collections
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import shared_utils
from edge_telemetry_common import identity
from edge_telemetry_common.topics import DEBUG_ROOT, TopicError, parse_convention_topic
from workflow_debug import envelope as envelope_module
from workflow_debug import logs_writer, sessions
from workflow_debug.redact import redact_structure

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "DDA/WorkflowDebug"

USECASE_CACHE_SECONDS = 60
DEVICE_CACHE_SECONDS = 5 * 60
WORKFLOW_CACHE_SECONDS = 10 * 60
PAIR_CACHE_SECONDS = 30
RATE_WINDOW_SECONDS = 10
RATE_FACTOR = 2
DEFAULT_MAX_MESSAGES_PER_SECOND = 10

_ALIAS_RE = re.compile(r"^acct-(\d{12})$")

RESULT_ACCEPTED = "accepted"
RESULT_REJECTED = "rejected"
RESULT_DUPLICATE = "duplicate"


class Rejected(Exception):
    """A message that is counted under ``reason`` and dropped."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else "{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- clients
_clients_lock = threading.Lock()
_logs_client = None
_dynamodb_resource = None


def _logs():
    global _logs_client
    with _clients_lock:
        if _logs_client is None:
            _logs_client = boto3.client("logs")
        return _logs_client


def _dynamodb():
    global _dynamodb_resource
    with _clients_lock:
        if _dynamodb_resource is None:
            _dynamodb_resource = boto3.resource("dynamodb")
        return _dynamodb_resource


def _sessions_table():
    return _dynamodb().Table(os.environ["WORKFLOW_DEBUG_SESSIONS_TABLE"])


# --------------------------------------------------------------------------- caches
class _TtlCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._entries: Dict[Any, Tuple[float, Any]] = {}

    def get(self, key: Any, now: float, loader: Callable[[], Any]) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
        value = loader()
        with self._lock:
            self._entries[key] = (now + self.ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_usecases = _TtlCache(USECASE_CACHE_SECONDS)
_devices = _TtlCache(DEVICE_CACHE_SECONDS)
_workflows = _TtlCache(WORKFLOW_CACHE_SECONDS)
_pairs = _TtlCache(PAIR_CACHE_SECONDS)
_rate_lock = threading.Lock()
_rate_windows: Dict[str, "collections.deque[float]"] = {}


def reset_caches() -> None:
    """Forget every per-container cache and client (tests)."""
    global _logs_client, _dynamodb_resource
    for cache in (_usecases, _devices, _workflows, _pairs):
        cache.clear()
    with _rate_lock:
        _rate_windows.clear()
    with _clients_lock:
        _logs_client = None
        _dynamodb_resource = None
    identity.clear_cache()
    logs_writer.forget_streams()


# --------------------------------------------------------------------------- metrics
def _emit_metrics(metrics: Dict[str, int], dimensions: Dict[str, str], now_ms: int,
                  units: Optional[Dict[str, str]] = None) -> None:
    """One embedded-metric-format line on stdout."""
    units = units or {}
    document: Dict[str, Any] = {
        "_aws": {
            "Timestamp": now_ms,
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [list(dimensions.keys())],
                "Metrics": [{"Name": name, "Unit": units.get(name, "Count")} for name in metrics],
            }],
        },
    }
    document.update(dimensions)
    document.update(metrics)
    print(json.dumps(document, separators=(",", ":")))


# --------------------------------------------------------------------------- lookups
def _sending_account(context: Any) -> str:
    arn = str(getattr(context, "invoked_function_arn", "") or "")
    parts = arn.split(":")
    if len(parts) != 8:
        raise Rejected("attribution_mismatch", "unqualified invocation")
    match = _ALIAS_RE.match(parts[7])
    if not match:
        raise Rejected("attribution_mismatch", "invocation not through an account alias")
    return match.group(1)


def _load_usecase(usecase_id: str) -> Optional[Dict[str, Any]]:
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        return None


def _usecase(usecase_id: str, now: float) -> Optional[Dict[str, Any]]:
    return _usecases.get(usecase_id, now, lambda: _load_usecase(usecase_id))


def _device_record(device_id: str, now: float) -> Optional[Dict[str, Any]]:
    def load():
        response = _dynamodb().Table(os.environ["DEVICES_TABLE"]).get_item(Key={"device_id": device_id})
        return response.get("Item")
    return _devices.get(device_id, now, load)


def _workflow_record(workflow_id: str, now: float) -> Optional[Dict[str, Any]]:
    def load():
        response = _dynamodb().Table(os.environ["WORKFLOWS_TABLE"]).get_item(Key={"workflow_id": workflow_id})
        return response.get("Item")
    return _workflows.get(workflow_id, now, load)


def _live_pairs(usecase_id: str, device_id: str, workflow_id: str, now: float) -> List[Dict[str, Any]]:
    """The ``PAIR#`` items of the Pair's live sessions with their META options
    (cached 30 s per Pair)."""
    def load():
        table = _sessions_table()
        response = table.query(
            IndexName="by-pair",
            KeyConditionExpression=Key("gsi_pair").eq(sessions.by_pair_pk(usecase_id, device_id, workflow_id)),
            ScanIndexForward=False, Limit=10)
        found = []
        for item in response.get("Items", []) or []:
            if not sessions.is_live(item.get("state")):
                continue
            meta = table.get_item(Key={"pk": sessions.session_pk(str(item["sessionId"])),
                                       "sk": sessions.META_SK}).get("Item") or {}
            entry = dict(item)
            entry["options"] = meta.get("options") or {}
            entry["metaState"] = meta.get("state")
            found.append(entry)
        return found
    return _pairs.get((usecase_id, device_id, workflow_id), now, load)


def _session_for(envelope: Dict[str, Any], usecase_id: str, device_id: str, workflow_id: str,
                 now_ms: int) -> Dict[str, Any]:
    wanted = str(envelope.get("sessionId") or "")
    for pair in _live_pairs(usecase_id, device_id, workflow_id, now_ms / 1000.0):
        if str(pair.get("sessionId")) != wanted:
            continue
        state = pair.get("metaState") or pair.get("state")
        if not sessions.is_live(state):
            continue
        if int(pair.get("expiresAt") or 0) <= now_ms:
            continue
        return pair
    raise Rejected("session_inactive")


def _check_rate(pair_key: str, max_per_second: int, now: float) -> None:
    limit = RATE_FACTOR * int(max_per_second) * RATE_WINDOW_SECONDS
    with _rate_lock:
        window = _rate_windows.setdefault(pair_key, collections.deque())
        while window and window[0] <= now - RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            raise Rejected("rate_exceeded")
        window.append(now)


def _is_duplicate(session_id: str, instance: str, seq: int) -> bool:
    response = _sessions_table().get_item(
        Key={"pk": sessions.session_pk(session_id), "sk": sessions.seq_sk(instance, seq)},
        ConsistentRead=True)
    return "Item" in response


def _mark_written(session_id: str, instance: str, seq: int, now_ms: int) -> bool:
    try:
        _sessions_table().put_item(Item=sessions.seq_item(session_id, instance, seq, now_ms),
                                   ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.debug("dedup mark already present for session %s instance %s seq %s",
                         session_id, instance, seq)
            return False
        raise


def _add_counters(session_id: str, records: int, written_bytes: int) -> None:
    try:
        _sessions_table().update_item(
            Key={"pk": sessions.session_pk(session_id), "sk": sessions.META_SK},
            UpdateExpression="ADD counters.records :r, counters.bytes :b",
            ExpressionAttributeValues={":r": int(records), ":b": int(written_bytes)})
    except ClientError as error:
        logger.warning("session counters not updated for %s: %s",
                       session_id, error.response.get("Error", {}).get("Code"))


# --------------------------------------------------------------------------- the algorithm
def process(event: Dict[str, Any], context: Any, *, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Steps 1-8 for one message; returns ``{result, reason?, events?}``.
    Raises on a retryable failure so Lambda retries the event."""
    now = clock()
    now_ms = int(now * 1000)
    account = ""
    usecase_id = ""
    try:
        account = _sending_account(context)
        # 1. decode and parse
        document = envelope_module.decode_payload(event.get("p"))
        try:
            parsed = parse_convention_topic(event.get("t"))
        except TopicError as error:
            raise Rejected("schema_unknown" if error.reason == "unknown_kind" else "attribution_mismatch",
                           error.reason)
        if parsed.root != DEBUG_ROOT:
            raise Rejected("attribution_mismatch", "root")
        usecase_id, workflow_id = parsed.usecase_id, parsed.workflow_id
        # 2. attribution
        usecase = _usecase(usecase_id, now)
        if usecase is None or str(usecase.get("account_id") or "") != account:
            raise Rejected("attribution_mismatch", "use case")
        if shared_utils.get_usecase_region(usecase) != os.environ.get("AWS_REGION", "us-east-1"):
            raise Rejected("attribution_mismatch", "region")
        thing = identity.resolve_thing(event.get("pr"), usecase, clock=clock)
        if not thing:
            raise Rejected("unattributed")
        if identity.strip_client_suffix(event.get("c")) != thing:
            raise Rejected("attribution_mismatch", "client id")
        device = _device_record(thing, now)
        if not device or str(device.get("usecase_id") or "") != usecase_id:
            raise Rejected("attribution_mismatch", "device record")
        workflow = _workflow_record(workflow_id, now)
        if not workflow or str(workflow.get("usecase_id") or "") != usecase_id:
            raise Rejected("workflow_not_in_usecase")
        if str(document.get("deviceId")) != thing or str(document.get("workflowId")) != workflow_id:
            raise Rejected("attribution_mismatch", "envelope ids")
        # 3. session check and rate
        pair = _session_for(document, usecase_id, thing, workflow_id, now_ms)
        max_per_second = int((pair.get("options") or {}).get("maxMessagesPerSecond")
                             or DEFAULT_MAX_MESSAGES_PER_SECOND)
        _check_rate(sessions.by_pair_pk(usecase_id, thing, workflow_id), max_per_second, now)
        # 4. validation and the dedup read
        validated = envelope_module.validate_envelope(document)
        session_id = str(validated["sessionId"])
        instance = str(validated["instance"])
        seq = int(validated["seq"])
        if _is_duplicate(session_id, instance, seq):
            _emit_metrics({"Received": 1, "Rejected": 1},
                          {"Account": account, "UseCaseId": usecase_id, "Reason": "duplicate"}, now_ms)
            logger.info("debug message dropped as a duplicate: session=%s seq=%s", session_id, seq)
            return {"result": RESULT_DUPLICATE, "reason": "duplicate"}
        # 5. redaction
        records = [redact_structure(record, frozenset()) for record in validated["records"]]
        # 6. the write
        stream = str(pair.get("logStream") or logs_writer.stream_name(thing, workflow_id))
        group = logs_writer.log_group_name(usecase_id)
        received_ms = int(event.get("r") or now_ms)
        retention = int(os.environ.get("DEBUG_LOG_RETENTION_DAYS") or 14)
        try:
            written = logs_writer.put_records(
                _logs(), group, stream, records, received_ms, session_id=session_id, instance=instance,
                seq=seq, clock=clock, sleep=sleep, retention_days=retention, usecase_id=usecase_id)
        except logs_writer.LogGroupMissing:
            _emit_metrics({"Received": 1, "Rejected": 1},
                          {"Account": account, "UseCaseId": usecase_id, "Reason": "log_group_missing"}, now_ms)
            logger.error("log group %s is missing and could not be recreated; raising for retry", group)
            raise
        if written.recreated_group:
            logger.warning("log group %s was missing and has been recreated inline", group)
        # 7. the dedup mark and the counters
        _mark_written(session_id, instance, seq, now_ms)
        _add_counters(session_id, written.events, written.bytes)
        # 8. metrics
        _emit_metrics({"Received": 1, "Accepted": 1, "BytesWritten": written.bytes},
                      {"Account": account, "UseCaseId": usecase_id}, now_ms,
                      units={"BytesWritten": "Bytes"})
        return {"result": RESULT_ACCEPTED, "events": written.events, "bytes": written.bytes,
                "stream": stream, "group": group}
    except envelope_module.EnvelopeError as error:
        return _reject(error.reason, error.detail, account, usecase_id, now_ms)
    except identity.LookupDenied as error:
        logger.error("identity lookup denied for account %s; terminal", account)
        return _reject("identity_unavailable", str(error), account, usecase_id, now_ms)
    except Rejected as error:
        return _reject(error.reason, error.detail, account, usecase_id, now_ms)


def _reject(reason: str, detail: str, account: str, usecase_id: str, now_ms: int) -> Dict[str, Any]:
    dimensions = {"Account": account or "unknown", "Reason": reason}
    if usecase_id:
        dimensions["UseCaseId"] = usecase_id
    _emit_metrics({"Received": 1, "Rejected": 1}, dimensions, now_ms)
    logger.info("debug message rejected: reason=%s detail=%s account=%s usecase=%s",
                reason, detail or "-", account or "-", usecase_id or "-")
    return {"result": RESULT_REJECTED, "reason": reason}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point: one rule message per invocation."""
    if not isinstance(event, dict):
        return _reject("invalid_json", "event is not an object", "", "", int(time.time() * 1000))
    return process(event, context)
