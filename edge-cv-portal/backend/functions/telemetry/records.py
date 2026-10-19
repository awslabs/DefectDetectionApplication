"""Record layout of the workflow telemetry stores (workflow-telemetry-dashboards
design §4.4, §6.5, §7.1, §7.2, §7.5; tasks.md 3.2).

- ``event_time(kind, fields, received_ms)``: the ``eventMs`` rule by kind with
  ``tsSource``. A Summary's ``intervalEnd`` is used when it lies within
  ``SUMMARY_EVENT_TIME_TOLERANCE_MS`` (5 minutes, shared with ``fold.py``)
  before or after the receive time ``r``, else ``r`` with ``tsSource:
  received``; an Event's ``ts`` is used when within 24 h before and 5 min
  after ``r``; a Live_Frame's ``ts`` is informational (the ``LIVE`` pointer
  orders by arrival).
- The Summary_Store keys (``P#<uc>#<device>#<wf>`` / ``S#<received13>#<m8>``,
  ``LAST#<producer>``, ``LASTHB#<producer>``, ``LIVE``), the Event_Store keys
  (``E#<eventMs13>#<eventId>``, the ``by-workflow`` GSI attributes, the
  Clip_Index key ``C#<uc>#<wf>`` / ``<startTs13>#<device>#<clipId>``).
- The item builders with their TTLs (§7.6) and the lake record of §7.5.
"""
import os
import re
from typing import Any, Dict, Optional, Tuple

from telemetry.fold import SUMMARY_EVENT_TIME_TOLERANCE_MS

__all__ = [
    "SUMMARY_EVENT_TIME_TOLERANCE_MS", "EVENT_PAST_TOLERANCE_MS", "EVENT_FUTURE_TOLERANCE_MS",
    "TS_SOURCE_DEVICE", "TS_SOURCE_RECEIVED",
    "SUMMARY_TTL_SECONDS", "LIVE_TTL_SECONDS", "EVENT_TTL_SECONDS", "DEFAULT_CLIP_RETENTION_DAYS",
    "LIVE_SK",
    "event_time", "pair_pk", "pair_of_pk", "summary_sk", "event_sk", "last_sk", "lasthb_sk",
    "by_workflow_pk", "by_workflow_sk", "clip_index_pk", "clip_index_sk", "clip_retention_days",
    "frame_token", "summary_item", "event_item", "clip_index_item", "live_pointer_values", "lake_record",
    "snake_case",
]

EVENT_PAST_TOLERANCE_MS = 24 * 60 * 60 * 1000
EVENT_FUTURE_TOLERANCE_MS = 5 * 60 * 1000
TS_SOURCE_DEVICE = "device"
TS_SOURCE_RECEIVED = "received"

SUMMARY_TTL_SECONDS = 48 * 3600
LIVE_TTL_SECONDS = 24 * 3600
EVENT_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_CLIP_RETENTION_DAYS = 90

LIVE_SK = "LIVE"

_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


# --------------------------------------------------------------------------- event time
def event_time(kind: str, fields: Dict[str, Any], received_ms: int) -> Tuple[int, str]:
    """``(eventMs, tsSource)`` per §4.4."""
    received_ms = int(received_ms)
    if kind == "summary":
        candidate = fields.get("intervalEnd")
        if isinstance(candidate, int) and abs(candidate - received_ms) <= SUMMARY_EVENT_TIME_TOLERANCE_MS:
            return int(candidate), TS_SOURCE_DEVICE
        return received_ms, TS_SOURCE_RECEIVED
    if kind == "event":
        candidate = fields.get("ts")
        if (isinstance(candidate, int)
                and received_ms - EVENT_PAST_TOLERANCE_MS <= candidate <= received_ms + EVENT_FUTURE_TOLERANCE_MS):
            return int(candidate), TS_SOURCE_DEVICE
        return received_ms, TS_SOURCE_RECEIVED
    candidate = fields.get("ts")
    if isinstance(candidate, int):
        return int(candidate), TS_SOURCE_DEVICE
    return received_ms, TS_SOURCE_RECEIVED


# --------------------------------------------------------------------------- keys
def _ms13(value: int) -> str:
    return "{0:013d}".format(int(value))


def pair_pk(usecase_id: str, device_id: str, workflow_id: str) -> str:
    return "P#{0}#{1}#{2}".format(usecase_id, device_id, workflow_id)


def pair_of_pk(pk: str) -> Optional[Tuple[str, str, str]]:
    """``(usecase_id, device_id, workflow_id)`` of a ``P#`` key, else None."""
    if not isinstance(pk, str) or not pk.startswith("P#"):
        return None
    parts = pk[2:].split("#")
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _m8(message_id: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", str(message_id))[:8].lower().ljust(8, "0")


def summary_sk(received_ms: int, message_id: str) -> str:
    return "S#{0}#{1}".format(_ms13(received_ms), _m8(message_id))


def event_sk(event_ms: int, event_id: str) -> str:
    return "E#{0}#{1}".format(_ms13(event_ms), event_id)


def last_sk(producer_name: str) -> str:
    return "LAST#{0}".format(producer_name)


def lasthb_sk(producer_name: str) -> str:
    return "LASTHB#{0}".format(producer_name)


def by_workflow_pk(usecase_id: str, workflow_id: str) -> str:
    return "W#{0}#{1}".format(usecase_id, workflow_id)


def by_workflow_sk(event_ms: int, device_id: str, event_id: str) -> str:
    return "{0}#{1}#{2}".format(_ms13(event_ms), device_id, event_id)


def clip_index_pk(usecase_id: str, workflow_id: str) -> str:
    return "C#{0}#{1}".format(usecase_id, workflow_id)


def clip_index_sk(start_ts: int, device_id: str, clip_id: str) -> str:
    return "{0}#{1}#{2}".format(_ms13(start_ts), device_id, clip_id)


def frame_token(instance: str, seq: int) -> str:
    """The live tile's opaque ``have`` token (§4.3, §9.3)."""
    return "{0}:{1}".format(instance, int(seq))


def clip_retention_days() -> int:
    raw = os.environ.get("TELEMETRY_CLIP_RETENTION_DAYS")
    try:
        value = int(raw) if raw not in (None, "") else DEFAULT_CLIP_RETENTION_DAYS
    except ValueError:
        value = DEFAULT_CLIP_RETENTION_DAYS
    return max(1, value)


# --------------------------------------------------------------------------- items
def _expires(received_ms: int, ttl_seconds: int) -> int:
    return int(received_ms) // 1000 + int(ttl_seconds)


def summary_item(record: Any, gap: int = 0) -> Dict[str, Any]:
    """The Summary_Store item of an accepted Summary or Heartbeat (§7.1)."""
    fields = record.fields
    return {
        "pk": pair_pk(record.usecase_id, record.device_id, record.workflow_id),
        "sk": summary_sk(record.received_ms, record.message_id),
        "summary": fields,
        "receivedMs": int(record.received_ms),
        "eventMs": int(record.event_ms),
        "tsSource": record.ts_source,
        "producer": record.producer,
        "instance": record.instance,
        "seq": int(record.seq),
        "gap": int(gap),
        "idle": bool(fields.get("idle", False)),
        "messageId": record.message_id,
        "workflowVersion": record.workflow_version,
        "expires_at": _expires(record.received_ms, SUMMARY_TTL_SECONDS),
    }


def event_item(record: Any) -> Dict[str, Any]:
    """The Event_Store item of an accepted Event with its ``by-workflow`` GSI
    attributes (§7.2)."""
    fields = record.fields
    event_id = str(fields["eventId"])
    return {
        "pk": pair_pk(record.usecase_id, record.device_id, record.workflow_id),
        "sk": event_sk(record.event_ms, event_id),
        "event": fields,
        "type": fields["type"],
        "eventId": event_id,
        "deviceId": record.device_id,
        "receivedMs": int(record.received_ms),
        "eventMs": int(record.event_ms),
        "tsSource": record.ts_source,
        "messageId": record.message_id,
        "gsi1pk": by_workflow_pk(record.usecase_id, record.workflow_id),
        "gsi1sk": by_workflow_sk(record.event_ms, record.device_id, event_id),
        "expires_at": _expires(record.received_ms, EVENT_TTL_SECONDS),
    }


def clip_index_item(record: Any, media: Dict[str, Any]) -> Dict[str, Any]:
    """The Clip_Index entry of a ``clip_ready`` Event (§6.5, §7.2); ``media`` is
    the bound ``{bucket, key, sidecarKey, state, reason}`` the ingest computed."""
    data = record.fields["data"]
    return {
        "pk": clip_index_pk(record.usecase_id, record.workflow_id),
        "sk": clip_index_sk(data["startTs"], record.device_id, data["clipId"]),
        "clipId": data["clipId"],
        "deviceId": record.device_id,
        "workflowId": record.workflow_id,
        "workflowVersion": record.workflow_version,
        "startTs": int(data["startTs"]),
        "endTs": int(data["endTs"]),
        "durationS": data.get("durationS"),
        "reasons": data.get("reasons") or {},
        "totals": data.get("totals") or {},
        "file": data.get("file") or {},
        "media": media,
        "revision": int(data["revision"]),
        "s3Error": data.get("s3Error"),
        "eventId": record.fields["eventId"],
        "receivedMs": int(record.received_ms),
        "expires_at": _expires(record.received_ms, clip_retention_days() * 86400),
    }


def live_pointer_values(record: Any) -> Dict[str, Any]:
    """The ``LIVE`` pointer's attributes (§7.1): the newest frame by arrival."""
    fields = record.fields
    frame = fields["frame"]
    values: Dict[str, Any] = {
        "instance": record.instance,
        "seq": int(record.seq),
        "frameToken": frame_token(record.instance, record.seq),
        "ts": int(fields["ts"]),
        "receivedMs": int(record.received_ms),
        "bucket": frame["bucket"],
        "key": frame["key"],
        "width": int(frame["width"]),
        "height": int(frame["height"]),
        "bytes": int(frame["bytes"]),
        "expires_at": _expires(record.received_ms, LIVE_TTL_SECONDS),
    }
    for name in ("perClass", "people", "violation", "reasons"):
        if name in fields:
            values[name] = fields[name]
    return values


# --------------------------------------------------------------------------- the lake record
def snake_case(name: str) -> str:
    return _SNAKE_RE.sub("_", str(name)).lower()


_IDENTITY_FIELDS = ("schema", "deviceId", "workflowId", "workflowVersion", "producer", "seq", "eventId")


def lake_record(record: Any, raw_text: str) -> Dict[str, Any]:
    """The §7.5 lake record: ``v: 1``, the identity fields, the validated
    top-level fields flattened one level (``runs_completed``, ``per_class``
    (map), ``ext`` (map), ...), and ``raw`` (the payload text)."""
    fields = record.fields
    out: Dict[str, Any] = {
        "v": 1,
        "message_id": record.message_id,
        "usecase_id": record.usecase_id,
        "device_id": record.device_id,
        "workflow_id": record.workflow_id,
        "workflow_version": record.workflow_version,
        "kind": record.kind,
        "schema": record.schema,
        "producer": record.producer,
        "instance": record.instance,
        "seq": int(record.seq) if record.seq is not None else None,
        "received_ms": int(record.received_ms),
        "event_ms": int(record.event_ms),
        "ts_source": record.ts_source,
    }
    for name, value in fields.items():
        if name in _IDENTITY_FIELDS:
            continue
        if name == "ext" or name == "data":
            out[name] = value
            continue
        if isinstance(value, dict):
            prefix = snake_case(name)
            for inner, inner_value in value.items():
                if inner == "perClass":
                    out["per_class"] = inner_value
                else:
                    out["{0}_{1}".format(prefix, snake_case(inner))] = inner_value
            continue
        out[snake_case(name)] = value
    out["raw"] = raw_text
    return out
