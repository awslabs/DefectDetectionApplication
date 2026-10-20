"""Writing debug records to CloudWatch Logs (workflow-debug-mode design §6.2
step 6, §7.1).

- ``stream_name(device, workflow_id)`` is computed in one place: IoT thing
  names may contain ``:`` while CloudWatch Logs stream names may not, so the
  colon is mapped to ``_``. The API stores the result on the ``PAIR#`` item
  at creation and reads it back; the ingest calls the same function.
- ``ensure_group`` is idempotent: ``CreateLogGroup`` (an existing group is
  fine), ``PutRetentionPolicy`` and ``TagResource`` with
  ``dda-portal:usecase-id``.
- ``put_records`` stamps every record with ``id = "<sessionId>:<instance>:<seq>:<index>"``,
  ``receivedMs`` and **one** ``writeTs`` per batch (the event timestamp, so a
  late or retried write always lands at the end of the stream), writes within
  the 1 MB / 10,000-event batch limits, retries a throttled ``PutLogEvents``
  three times (0.2, 0.5, 1 s) and then raises, and recreates a hand-deleted
  group inline (``ResourceNotFoundException`` -> ``DescribeLogStreams`` tells
  the group from the stream -> ``ensure_group``/``ensure_stream`` -> one more
  attempt; a failed recreation raises :class:`LogGroupMissing`).
"""
import json
import os
import threading
import time
from collections import namedtuple
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from botocore.exceptions import ClientError

__all__ = [
    "LOG_GROUP_PREFIX",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_EVENTS",
    "MAX_EVENT_BYTES",
    "EVENT_OVERHEAD_BYTES",
    "RETRY_DELAYS",
    "USECASE_TAG",
    "LogGroupMissing",
    "PutThrottled",
    "PutResult",
    "stream_name",
    "log_group_name",
    "log_group_arn",
    "ensure_group",
    "ensure_stream",
    "forget_streams",
    "stamp_records",
    "build_events",
    "batches",
    "put_records",
]

LOG_GROUP_PREFIX = "/dda/workflow-debug/"
USECASE_TAG = "dda-portal:usecase-id"

#: CloudWatch Logs ``PutLogEvents`` limits: 1 MB per batch including 26 bytes
#: of overhead per event, 10,000 events per batch, 256 KB per event.
MAX_BATCH_BYTES = 1_048_576
MAX_BATCH_EVENTS = 10_000
EVENT_OVERHEAD_BYTES = 26
MAX_EVENT_BYTES = 256 * 1024

RETRY_DELAYS = (0.2, 0.5, 1.0)

_THROTTLING_CODES = frozenset({
    "ThrottlingException", "Throttling", "ThrottledException",
    "TooManyRequestsException", "ServiceUnavailableException", "RequestLimitExceeded",
})

PutResult = namedtuple("PutResult", ["events", "bytes", "batches", "recreated_group", "replaced"])


class LogGroupMissing(Exception):
    """The log group is missing and could not be recreated inline."""


class PutThrottled(Exception):
    """``PutLogEvents`` was throttled through every retry."""


def stream_name(device: str, workflow_id: str) -> str:
    """``<device with ':' as '_'>/<workflow_id>``, valid for CloudWatch Logs."""
    return "{0}/{1}".format(str(device).replace(":", "_"), workflow_id)


def log_group_name(usecase_id: str) -> str:
    return LOG_GROUP_PREFIX + str(usecase_id)


def log_group_arn(group: str, *, region: Optional[str] = None, account: Optional[str] = None) -> str:
    """The group's ARN for ``TagResource`` (the function's region and the
    Portal account from the environment unless given)."""
    region = region or os.environ.get("AWS_REGION") or "us-east-1"
    account = account or os.environ.get("PORTAL_ACCOUNT_ID") or ""
    return "arn:aws:logs:{0}:{1}:log-group:{2}".format(region, account, group)


def _code(error: ClientError) -> str:
    return str(((error.response or {}).get("Error") or {}).get("Code") or "")


def ensure_group(logs: Any, group: str, retention_days: int, usecase_id: str, *,
                 group_arn: Optional[str] = None) -> None:
    """Idempotent ``CreateLogGroup`` + ``PutRetentionPolicy`` + ``TagResource``."""
    try:
        logs.create_log_group(logGroupName=group)
    except ClientError as error:
        if _code(error) != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=group, retentionInDays=int(retention_days))
    logs.tag_resource(resourceArn=group_arn or log_group_arn(group),
                      tags={USECASE_TAG: str(usecase_id)})


_known_streams: Set[Tuple[str, str]] = set()
_known_lock = threading.Lock()


def ensure_stream(logs: Any, group: str, stream: str, *, force: bool = False) -> None:
    """``CreateLogStream`` once per container per (group, stream); an existing
    stream is fine."""
    key = (group, stream)
    with _known_lock:
        if key in _known_streams and not force:
            return
    try:
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
    except ClientError as error:
        if _code(error) != "ResourceAlreadyExistsException":
            raise
    with _known_lock:
        _known_streams.add(key)


def forget_streams() -> None:
    """Drop the per-container stream cache (tests)."""
    with _known_lock:
        _known_streams.clear()


def stamp_records(records: Iterable[Dict[str, Any]], *, session_id: str, instance: str, seq: int,
                  received_ms: int, write_ts: int) -> List[Dict[str, Any]]:
    """Each record extended by its stable ``id``, ``receivedMs`` and the
    batch's one ``writeTs``; the inputs are not modified."""
    stamped = []
    for index, record in enumerate(records):
        item = dict(record)
        item["id"] = "{0}:{1}:{2}:{3}".format(session_id, instance, seq, index)
        item["receivedMs"] = received_ms
        item["writeTs"] = write_ts
        stamped.append(item)
    return stamped


def _event_size(message: str) -> int:
    return len(message.encode("utf-8")) + EVENT_OVERHEAD_BYTES


def build_events(stamped: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """``([{timestamp, message}], replaced)``: one event per record with the
    record's ``writeTs`` as the timestamp. A record whose serialization would
    exceed the per-event limit (not expected: a message is at most 128 KB on
    the wire) is replaced by an ``ingest_error`` marker carrying its stamps."""
    events = []
    replaced = 0
    for item in stamped:
        message = json.dumps(item, separators=(",", ":"), default=str)
        if _event_size(message) > MAX_EVENT_BYTES:
            replaced += 1
            marker = {"type": "ingest_error", "reason": "record_too_large",
                      "originalBytes": len(message.encode("utf-8")),
                      "originalType": item.get("type"), "ts": item.get("ts"),
                      "id": item.get("id"), "receivedMs": item.get("receivedMs"),
                      "writeTs": item.get("writeTs")}
            message = json.dumps(marker, separators=(",", ":"), default=str)
        events.append({"timestamp": int(item["writeTs"]), "message": message})
    return events, replaced


def batches(events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split within 1 MB (with the 26-byte overhead per event) and 10,000 events."""
    result: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0
    for event in events:
        event_size = _event_size(event["message"])
        if current and (size + event_size > MAX_BATCH_BYTES or len(current) >= MAX_BATCH_EVENTS):
            result.append(current)
            current, size = [], 0
        current.append(event)
        size += event_size
    if current:
        result.append(current)
    return result


def _missing_component(logs: Any, group: str, stream: str) -> str:
    """``'group'`` when the group itself is missing, else ``'stream'``."""
    try:
        logs.describe_log_streams(logGroupName=group, logStreamNamePrefix=stream, limit=1)
    except ClientError as error:
        if _code(error) == "ResourceNotFoundException":
            return "group"
        raise
    return "stream"


def _put_batch(logs: Any, group: str, stream: str, batch: List[Dict[str, Any]], *,
               sleep: Callable[[float], None], retention_days: int, usecase_id: Optional[str],
               group_arn: Optional[str]) -> bool:
    """One batch with the retry and recreation rules; returns whether the
    group was recreated."""
    attempt = 0
    recreated = False
    tried_recreation = False
    while True:
        try:
            logs.put_log_events(logGroupName=group, logStreamName=stream, logEvents=batch)
            return recreated
        except ClientError as error:
            code = _code(error)
            if code in _THROTTLING_CODES:
                if attempt < len(RETRY_DELAYS):
                    sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                raise PutThrottled("PutLogEvents throttled after {0} retries".format(len(RETRY_DELAYS))) from error
            if code == "ResourceNotFoundException" and not tried_recreation:
                tried_recreation = True
                if _missing_component(logs, group, stream) == "group":
                    try:
                        ensure_group(logs, group, retention_days, usecase_id or "", group_arn=group_arn)
                    except ClientError as recreate_error:
                        raise LogGroupMissing(
                            "log group {0} is missing and could not be recreated".format(group)
                        ) from recreate_error
                    recreated = True
                ensure_stream(logs, group, stream, force=True)
                continue
            if code == "DataAlreadyAcceptedException":
                return recreated
            raise


def put_records(logs: Any, group: str, stream: str, records: Iterable[Dict[str, Any]],
                received_ms: int, *, session_id: str, instance: str, seq: int,
                clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                retention_days: int = 14, usecase_id: Optional[str] = None,
                group_arn: Optional[str] = None) -> PutResult:
    """Write the records of one message as events of the Pair's stream."""
    ensure_stream(logs, group, stream)
    write_ts = int(clock() * 1000)
    stamped = stamp_records(records, session_id=session_id, instance=instance, seq=seq,
                            received_ms=received_ms, write_ts=write_ts)
    events, replaced = build_events(stamped)
    total_bytes = sum(len(event["message"].encode("utf-8")) for event in events)
    recreated = False
    groups = batches(events)
    for batch in groups:
        if _put_batch(logs, group, stream, batch, sleep=sleep, retention_days=retention_days,
                      usecase_id=usecase_id, group_arn=group_arn):
            recreated = True
    return PutResult(len(events), total_bytes, len(groups), recreated, replaced)
