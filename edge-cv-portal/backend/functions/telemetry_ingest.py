"""
Workflow telemetry ingest (workflow-telemetry-dashboards design §6; tasks.md
3.3): the Lambda action target of the ``dda_tel_ingest`` rules, one alias
``acct-<account>`` per account, one rule message per invocation.

The rule delivers ``{"p": <base64 payload>, "t": <topic>, "pr": <principal>,
"c": <client id>, "r": <timestamp ms>, "m": <message id>}``. For each message:

1. ``attribution.decide`` with the real lookups (``get_usecase`` cached 60 s,
   the settings item for ``enabled``, ``identity.resolve_thing``, the Devices
   and Workflows ``GetItem``s cached); a retryable identity failure raises so
   Lambda retries the event;
2. a Summary: the idempotent ``PutItem`` keyed ``S#<received13>#<m8>``; the
   ``LAST#``/``LASTHB#`` conditional ``UpdateItem`` (``lastRunMs`` only when the
   Summary carried runs); the ``duplicate`` diagnostic from the pointer
   comparison with the item still written; the per-message ``gap`` estimate
   and the ``Gaps`` metric; the ``SEEN`` marker; the lake record (Heartbeats
   excepted);
3. an Event: the idempotent ``PutItem`` keyed ``E#<eventMs13>#<eventId>``;
   ``run_error.error`` and ``clip_ready.s3Error`` redacted again with
   ``telemetry.redaction.redact(text, ())``; the Clip_Index conditional
   ``UpdateItem`` on ``revision`` with the media reference bound first; the
   lake record;
4. a Live_Frame: the ``LIVE`` pointer ``UpdateItem`` conditioned on arrival
   order (``attribute_not_exists(receivedMs) OR receivedMs < :r``);
5. Firehose ``PutRecordBatch`` within the 500-record / 4 MiB limits, two
   retries, best-effort;
6. rejection counters ``REJ#<minute>`` and one 1 KB ``SAMPLE#`` per (scope,
   reason, minute) with TTL; EMF metrics in ``DDA/Telemetry``.

Logs carry ids, counts and reasons, never a payload.
"""
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import shared_utils
from edge_telemetry_common import identity
from telemetry import attribution, configdb, media, records
from telemetry.redaction import redact

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "DDA/Telemetry"

USECASE_CACHE_SECONDS = 60
SETTINGS_CACHE_SECONDS = 60
DEVICE_CACHE_SECONDS = 5 * 60
WORKFLOW_CACHE_SECONDS = 10 * 60

FIREHOSE_MAX_RECORDS = 500
FIREHOSE_MAX_BYTES = 4 * 1024 * 1024
FIREHOSE_RETRIES = 2

_ALIAS_RE = re.compile(r"^acct-(\d{12})$")

RESULT_ACCEPTED = "accepted"
RESULT_REJECTED = "rejected"


# --------------------------------------------------------------------------- clients
_clients_lock = threading.Lock()
_dynamodb_resource = None
_firehose_client = None


def _dynamodb():
    global _dynamodb_resource
    with _clients_lock:
        if _dynamodb_resource is None:
            _dynamodb_resource = boto3.resource("dynamodb")
        return _dynamodb_resource


def _firehose():
    global _firehose_client
    with _clients_lock:
        if _firehose_client is None:
            _firehose_client = boto3.client("firehose")
        return _firehose_client


def _summaries_table():
    return _dynamodb().Table(os.environ["TELEMETRY_SUMMARIES_TABLE"])


def _events_table():
    return _dynamodb().Table(os.environ["TELEMETRY_EVENTS_TABLE"])


def _config_table():
    return _dynamodb().Table(os.environ["TELEMETRY_CONFIG_TABLE"])


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
_settings = _TtlCache(SETTINGS_CACHE_SECONDS)
_devices = _TtlCache(DEVICE_CACHE_SECONDS)
_workflows = _TtlCache(WORKFLOW_CACHE_SECONDS)
_rate_counter = attribution.EventRateCounter()


def reset_caches() -> None:
    """Forget every per-container cache and client (tests)."""
    global _dynamodb_resource, _firehose_client
    for cache in (_usecases, _settings, _devices, _workflows):
        cache.clear()
    _rate_counter.clear()
    with _clients_lock:
        _dynamodb_resource = None
        _firehose_client = None
    identity.clear_cache()


# --------------------------------------------------------------------------- metrics
def _emit_metrics(metrics: Dict[str, int], dimensions: Dict[str, str], now_ms: int) -> None:
    """One embedded-metric-format line on stdout."""
    document: Dict[str, Any] = {
        "_aws": {
            "Timestamp": int(now_ms),
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [list(dimensions.keys())],
                "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
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
        return ""
    match = _ALIAS_RE.match(parts[7])
    return match.group(1) if match else ""


def _load_usecase(usecase_id: str) -> Optional[Dict[str, Any]]:
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        return None


def _lookups(now: float) -> attribution.Lookups:
    def usecase(usecase_id: str) -> Optional[Dict[str, Any]]:
        return _usecases.get(usecase_id, now, lambda: _load_usecase(usecase_id))

    def telemetry_enabled(u: Dict[str, Any]) -> bool:
        usecase_id = str(u["usecase_id"])

        def load() -> bool:
            item = _config_table().get_item(Key=configdb.settings_key(usecase_id)).get("Item")
            return bool(item and item.get("enabled") is True)

        return _settings.get(usecase_id, now, load)

    def resolve_thing(pr: str, u: Dict[str, Any]) -> Optional[str]:
        return identity.resolve_thing(pr, u, clock=lambda: now)

    def device_record(device_id: str) -> Optional[Dict[str, Any]]:
        def load():
            return _dynamodb().Table(os.environ["DEVICES_TABLE"]).get_item(Key={"device_id": device_id}).get("Item")

        return _devices.get(device_id, now, load)

    def workflow_record(workflow_id: str) -> Optional[Dict[str, Any]]:
        def load():
            return _dynamodb().Table(os.environ["WORKFLOWS_TABLE"]).get_item(Key={"workflow_id": workflow_id}).get("Item")

        return _workflows.get(workflow_id, now, load)

    return attribution.Lookups(usecase, telemetry_enabled, resolve_thing, device_record, workflow_record)


# --------------------------------------------------------------------------- writes
def _dynamo(value: Any) -> Any:
    """Floats to Decimals (the DynamoDB resource refuses floats), recursively."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo(v) for v in value]
    return value


def _set_expression(values: Dict[str, Any]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """``SET`` clause, names and values for a flat attribute map, aliased by
    position (``#a<i>`` / ``:v<i>``) so a reserved attribute name is safe."""
    ordered = sorted(values)
    names = {"#a{0}".format(index): name for index, name in enumerate(ordered)}
    expression_values = {":v{0}".format(index): _dynamo(values[name]) for index, name in enumerate(ordered)}
    clause = "SET " + ", ".join("#a{0} = :v{0}".format(index) for index in range(len(ordered)))
    return clause, names, expression_values


def _is_condition_failure(error: ClientError) -> bool:
    return str((error.response.get("Error") or {}).get("Code") or "") == "ConditionalCheckFailedException"


def _pointer(pk: str, sk: str) -> Optional[Dict[str, Any]]:
    try:
        return _summaries_table().get_item(Key={"pk": pk, "sk": sk}).get("Item")
    except ClientError as error:
        logger.warning("pointer read failed for %s: %s", sk, (error.response.get("Error") or {}).get("Code"))
        return None


def _gap_estimate(pk: str, record: Any) -> Tuple[int, bool]:
    """``(gap, duplicate)`` from the two pointers of the producer: the higher
    ``seq`` for the same ``instance`` is the reference; a new instance is
    neither a gap nor a duplicate (§6.5)."""
    last = _pointer(pk, records.last_sk(record.producer))
    lasthb = _pointer(pk, records.lasthb_sk(record.producer))
    reference = None
    for pointer in (last, lasthb):
        if pointer and str(pointer.get("instance") or "") == record.instance and pointer.get("seq") is not None:
            seq = int(pointer["seq"])
            reference = seq if reference is None else max(reference, seq)
    if reference is None or record.seq is None:
        return 0, False
    if record.seq <= reference:
        return 0, True
    return max(0, record.seq - reference - 1), False


def _update_pointer(pk: str, sk: str, record: Any, item: Dict[str, Any], *, with_summary: bool) -> bool:
    """The conditional ``UpdateItem`` of a ``LAST#``/``LASTHB#`` pointer; True
    when the pointer moved."""
    names = {"#rm": "receivedMs", "#em": "eventMs", "#in": "instance", "#sq": "seq", "#ex": "expires_at",
             "#wv": "workflowVersion"}
    values: Dict[str, Any] = {":r": int(record.received_ms), ":e": int(record.event_ms), ":i": record.instance,
                              ":s": int(record.seq), ":x": int(item["expires_at"]), ":w": record.workflow_version}
    sets = ["#rm = :r", "#em = :e", "#in = :i", "#sq = :s", "#ex = :x", "#wv = :w"]
    if with_summary:
        names["#su"] = "summary"
        values[":su"] = _dynamo(record.fields)
        sets.append("#su = :su")
        runs = record.fields.get("runs") or {}
        if int(runs.get("completed", 0) or 0) + int(runs.get("failed", 0) or 0) > 0:
            names["#lr"] = "lastRunMs"
            sets.append("#lr = :r")
    try:
        _summaries_table().update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(#rm) OR #rm < :r",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values)
        return True
    except ClientError as error:
        if _is_condition_failure(error):
            return False
        raise


def _store_summary(record: Any, now_ms: int) -> Dict[str, Any]:
    pk = records.pair_pk(record.usecase_id, record.device_id, record.workflow_id)
    gap, duplicate = _gap_estimate(pk, record)
    item = records.summary_item(record, gap=gap)
    _summaries_table().put_item(Item=_dynamo(item))
    idle = bool(record.fields.get("idle"))
    if duplicate:
        # A retransmission or a late retry (seq at or below the pointer's for
        # the same instance): stored for the lake and the fold's in-memory
        # de-duplication, counted as a diagnostic, the pointer left alone.
        moved = False
    elif idle:
        moved = _update_pointer(pk, records.lasthb_sk(record.producer), record, item, with_summary=False)
    else:
        moved = _update_pointer(pk, records.last_sk(record.producer), record, item, with_summary=True)
    _config_table().put_item(Item=configdb.seen_item(record.usecase_id, record.received_ms))
    return {"gap": gap, "duplicate": duplicate, "pointer_moved": moved, "idle": idle, "sk": item["sk"]}


def _redact_event_text(record: Any) -> None:
    data = record.fields.get("data")
    if not isinstance(data, dict):
        return
    for key in ("error", "s3Error"):
        if isinstance(data.get(key), str):
            data[key] = redact(data[key], ())


def _clip_media(record: Any) -> Dict[str, Any]:
    """The bound media reference of a ``clip_ready`` Event (§12.2, ``audit=False``)."""
    data = record.fields["data"]
    s3 = data.get("s3")
    if not isinstance(s3, dict):
        return {"bucket": None, "key": None, "sidecarKey": None, "state": "upload_failed", "reason": None}
    binding = media.bind_key(record.usecase, "clips", record.usecase_id, record.device_id, record.workflow_id,
                             s3.get("bucket"), s3.get("key"))
    if not binding.ok:
        return {"bucket": s3.get("bucket") if binding.reason != "invalid_s3_uri" else None, "key": None,
                "sidecarKey": None, "state": "not_allowed", "reason": binding.reason}
    sidecar = s3.get("sidecarKey")
    if isinstance(sidecar, str) and not media.bind_key(record.usecase, "clips", record.usecase_id, record.device_id,
                                                        record.workflow_id, binding.bucket, sidecar).ok:
        sidecar = None
    return {"bucket": binding.bucket, "key": binding.key, "sidecarKey": sidecar, "state": "bound", "reason": None}


def _update_clip_index(record: Any) -> bool:
    item = records.clip_index_item(record, _clip_media(record))
    key = {"pk": item.pop("pk"), "sk": item.pop("sk")}
    clause, names, values = _set_expression(item)
    names["#rev"] = "revision"
    values[":rev"] = int(record.fields["data"]["revision"])
    try:
        _events_table().update_item(
            Key=key, UpdateExpression=clause,
            ConditionExpression="attribute_not_exists(#rev) OR #rev < :rev",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except ClientError as error:
        if _is_condition_failure(error):
            return False
        raise


def _store_event(record: Any) -> Dict[str, Any]:
    _redact_event_text(record)
    item = records.event_item(record)
    _events_table().put_item(Item=_dynamo(item))
    outcome: Dict[str, Any] = {"sk": item["sk"], "type": record.fields["type"]}
    if record.fields["type"] == "clip_ready":
        outcome["clip_index_updated"] = _update_clip_index(record)
    return outcome


def _store_live(record: Any) -> Dict[str, Any]:
    pk = records.pair_pk(record.usecase_id, record.device_id, record.workflow_id)
    clause, names, expression_values = _set_expression(records.live_pointer_values(record))
    names["#rm"] = "receivedMs"
    expression_values[":r"] = int(record.received_ms)
    try:
        _summaries_table().update_item(
            Key={"pk": pk, "sk": records.LIVE_SK}, UpdateExpression=clause,
            ConditionExpression="attribute_not_exists(#rm) OR #rm < :r",
            ExpressionAttributeNames=names, ExpressionAttributeValues=expression_values)
        return {"pointer_moved": True}
    except ClientError as error:
        if _is_condition_failure(error):
            return {"pointer_moved": False}
        raise


# --------------------------------------------------------------------------- the lake
def _lake_text(record: Any) -> str:
    raw_text = record.raw_text
    if record.kind == "event":
        raw_text = redact(raw_text, ())
    return json.dumps(records.lake_record(record, raw_text), default=str, separators=(",", ":")) + "\n"


def put_lake_records(stream: str, lines: List[str], *, client: Any = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """``PutRecordBatch`` within 500 records / 4 MiB per call, failed records
    retried twice; returns the number of records that failed for good.
    Never raises: the lake is best-effort (§6.5)."""
    client = client if client is not None else _firehose()
    pending = [line.encode("utf-8") for line in lines]
    failed_for_good = 0
    attempt = 0
    while pending and attempt <= FIREHOSE_RETRIES:
        batches: List[List[bytes]] = []
        current: List[bytes] = []
        size = 0
        for data in pending:
            if current and (len(current) >= FIREHOSE_MAX_RECORDS or size + len(data) > FIREHOSE_MAX_BYTES):
                batches.append(current)
                current, size = [], 0
            current.append(data)
            size += len(data)
        if current:
            batches.append(current)
        retry: List[bytes] = []
        for batch in batches:
            try:
                response = client.put_record_batch(DeliveryStreamName=stream, Records=[{"Data": d} for d in batch])
            except (ClientError, BotoCoreError) as error:
                logger.warning("firehose %s batch failed (attempt %d): %s", stream, attempt + 1, error.__class__.__name__)
                retry.extend(batch)
                continue
            if int(response.get("FailedPutCount") or 0):
                for data, result in zip(batch, response.get("RequestResponses") or []):
                    if result.get("ErrorCode"):
                        retry.append(data)
        pending = retry
        attempt += 1
        if pending and attempt <= FIREHOSE_RETRIES:
            sleep(0.1 * attempt)
    if pending:
        failed_for_good = len(pending)
        logger.warning("firehose %s: %d record(s) not delivered after %d retries (best effort)",
                       stream, failed_for_good, FIREHOSE_RETRIES)
    return failed_for_good


# --------------------------------------------------------------------------- rejections
def _count_rejection(scope_pk: str, received_ms: int, reasons: Dict[str, int], sample: Optional[Dict[str, Any]],
                     reason: str) -> None:
    table = _config_table()
    try:
        table.update_item(**configdb.rejection_update(scope_pk, received_ms, reasons))
    except ClientError as error:
        logger.warning("rejection counter not updated for %s: %s", scope_pk, (error.response.get("Error") or {}).get("Code"))
    if sample is None:
        return
    try:
        table.put_item(Item=_dynamo(configdb.sample_item(scope_pk, received_ms, reason, sample)),
                       ConditionExpression="attribute_not_exists(pk)")
    except ClientError as error:
        if not _is_condition_failure(error):
            logger.warning("rejection sample not written for %s: %s", scope_pk, (error.response.get("Error") or {}).get("Code"))


def _reject(decision: attribution.Decision, message: Dict[str, Any], account: str, now_ms: int) -> Dict[str, Any]:
    received_ms = _int(message.get("r"), now_ms)
    reason = str(decision.reason)
    dimensions = {"Account": account or "unknown", "Reason": reason}
    if decision.usecase_id:
        dimensions["UseCaseId"] = decision.usecase_id
    _emit_metrics({"Received": 1, "Rejected": 1}, dimensions, now_ms)
    if reason == "identity_unavailable":
        _emit_metrics({"IdentityUnavailable": 1}, {}, now_ms)
    scope_pk = configdb.usecase_pk(decision.usecase_id) if decision.usecase_id else configdb.account_pk(account or "unknown")
    sample = {"reason": reason, "detail": (decision.detail or "")[:200], "topic": str(message.get("t") or "")[:256],
              "clientId": str(message.get("c") or "")[:128], "messageId": str(message.get("m") or "")[:64],
              "receivedMs": received_ms}
    _count_rejection(scope_pk, received_ms, {reason: 1}, sample, reason)
    logger.info("telemetry message rejected: reason=%s detail=%s account=%s usecase=%s",
                reason, decision.detail or "-", account or "-", decision.usecase_id or "-")
    return {"result": RESULT_REJECTED, "reason": reason, "usecase_id": decision.usecase_id}


# --------------------------------------------------------------------------- the algorithm
def process(event: Dict[str, Any], context: Any, *, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """One message: returns ``{result, ...}``; raises on a retryable failure so
    Lambda retries the event."""
    now = clock()
    now_ms = int(now * 1000)
    account = _sending_account(context)
    region = os.environ.get("AWS_REGION", "us-east-1")
    if not account:
        decision = attribution.Decision(False, "attribution_mismatch", "invocation not through an account alias",
                                        None, None, "")
        return _reject(decision, event, "", now_ms)
    decision = attribution.decide(event, _lookups(now), account=account, portal_region=region, now_ms=now_ms,
                                  rate_counter=_rate_counter)
    if not decision.accepted:
        return _reject(decision, event, account, now_ms)
    record = decision.record
    diagnostics: Dict[str, int] = {}
    outcome: Dict[str, Any] = {"result": RESULT_ACCEPTED, "kind": record.kind, "usecase_id": record.usecase_id,
                               "device_id": record.device_id, "workflow_id": record.workflow_id}
    lake_lines: List[str] = []
    if record.kind == "summary":
        stored = _store_summary(record, now_ms)
        outcome.update(stored)
        if stored["gap"]:
            _emit_metrics({"Gaps": stored["gap"]}, {"UseCaseId": record.usecase_id}, now_ms)
        if stored["duplicate"]:
            diagnostics["duplicate"] = 1
        if not stored["idle"]:
            lake_lines.append(_lake_text(record))
    elif record.kind == "event":
        outcome.update(_store_event(record))
        lake_lines.append(_lake_text(record))
    else:
        outcome.update(_store_live(record))
    for block in record.blocks_dropped:
        _emit_metrics({"BlockNotOwned": 1}, {"Block": block, "UseCaseId": record.usecase_id}, now_ms)
    if record.blocks_dropped:
        diagnostics["block_not_owned"] = len(record.blocks_dropped)
    if record.dropped:
        _emit_metrics({"FieldDropped": len(record.dropped)}, {"Schema": record.schema}, now_ms)
        logger.info("telemetry fields dropped: schema=%s fields=%s usecase=%s", record.schema,
                    ",".join(record.dropped)[:512], record.usecase_id)
    if diagnostics:
        for name, count in diagnostics.items():
            _emit_metrics({"Diagnostic": count}, {"Reason": name, "UseCaseId": record.usecase_id}, now_ms)
        _count_rejection(configdb.usecase_pk(record.usecase_id), record.received_ms, diagnostics, None, "")
    if lake_lines:
        stream = os.environ.get("TELEMETRY_FIREHOSE_SUMMARIES" if record.kind == "summary" else "TELEMETRY_FIREHOSE_EVENTS")
        if stream:
            outcome["lake_failed"] = put_lake_records(stream, lake_lines, sleep=sleep)
    _emit_metrics({"Received": 1, "Accepted": 1}, {"Account": account}, now_ms)
    _emit_metrics({"Accepted": 1}, {"UseCaseId": record.usecase_id}, now_ms)
    logger.info("telemetry message accepted: kind=%s usecase=%s device=%s workflow=%s seq=%s gap=%s duplicate=%s",
                record.kind, record.usecase_id, record.device_id, record.workflow_id, record.seq,
                outcome.get("gap", 0), outcome.get("duplicate", False))
    outcome["diagnostics"] = diagnostics
    return outcome


def _int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point: one rule message per invocation."""
    if not isinstance(event, dict):
        now_ms = int(time.time() * 1000)
        decision = attribution.Decision(False, "invalid_json", "event is not an object", None, None, "")
        return _reject(decision, {}, _sending_account(context), now_ms)
    return process(event, context)
