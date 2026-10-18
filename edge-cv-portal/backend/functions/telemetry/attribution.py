"""The attribution algorithm of the telemetry ingest
(workflow-telemetry-dashboards design §6.4; tasks.md 3.2).

``decide(message, lookups, ...)`` is pure over its injected lookups and
decides, for one rule message ``{p, t, pr, c, r, m}`` delivered to the alias
of account ``A``:

1. **Account**: ``A`` is the alias account (the ingest reads it from
   ``invoked_function_arn``); ``R`` is the Portal's region.
2. **Topic**: ``t`` parses as ``dda/telemetry/<usecase UUID>/<workflow UUID>/<kind>``;
   a malformed id is ``attribution_mismatch``, an unknown kind ``schema_unknown``.
3. **Use case**: ``U`` must exist, ``U.account_id == A`` and ``U.region == R``
   (else ``attribution_mismatch``); telemetry must be enabled (else
   ``usecase_disabled``).
4. **Identity**: ``pr`` is 64 hex (else ``unattributed``); ``resolve_thing``
   gives the thing ``D`` (``LookupRetryable`` propagates so Lambda retries;
   ``LookupDenied`` is terminal ``identity_unavailable``; no thing is
   ``unattributed``); ``c`` less a ``#<n>`` suffix equals ``D`` (else
   ``attribution_mismatch``).
5. **Device record** (fail-closed): the Device_Record of ``D`` names ``U``
   (else ``attribution_mismatch``).
6. **Workflow**: the Workflow record names ``U`` (else ``workflow_not_in_usecase``).
7. **Schema**: decode and validate (§4.4); the payload's ``deviceId`` equals
   ``D`` and its ``workflowId`` the topic's (else ``attribution_mismatch``).
   7b. Events only: the 121st accepted Event of the Pair in the current
   minute is ``rate_exceeded``.
8. **Result**: the accepted record.

Block ownership (§4.1) is applied to Summaries: blocks a producer does not
own are dropped from the typed fields and named in ``blocks_dropped``, so
the ingest counts ``block_not_owned`` per block while the message stands.
"""
import collections
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from edge_telemetry_common.identity import LookupDenied, LookupRetryable, strip_client_suffix
from edge_telemetry_common.topics import TELEMETRY_ROOT, TopicError, parse_convention_topic
from telemetry import records, schemas

__all__ = [
    "Lookups", "Decision", "AcceptedRecord", "EventRateCounter", "EVENT_RATE_LIMIT_PER_MINUTE",
    "RATE_WINDOW_MS", "REJECTION_REASONS", "decide", "LookupRetryable", "LookupDenied",
]

#: Twice the agent's 60-per-minute cap (§6.4 step 7b): the 121st Event of a
#: Pair in a minute is ``rate_exceeded``.
EVENT_RATE_LIMIT_PER_MINUTE = 120
RATE_WINDOW_MS = 60_000

REJECTION_REASONS = (
    "invalid_json", "too_large", "schema_invalid", "schema_unknown", "usecase_disabled", "unattributed",
    "identity_unavailable", "attribution_mismatch", "workflow_not_in_usecase", "rate_exceeded",
)

_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: The injected lookups: ``usecase(usecase_id) -> dict | None``,
#: ``telemetry_enabled(usecase) -> bool``, ``resolve_thing(pr, usecase) -> str | None``
#: (raising LookupRetryable / LookupDenied), ``device_record(device_id) -> dict | None``,
#: ``workflow_record(workflow_id) -> dict | None``.
Lookups = collections.namedtuple("Lookups", ["usecase", "telemetry_enabled", "resolve_thing",
                                             "device_record", "workflow_record"])

#: ``accepted`` with the ``record`` (an :class:`AcceptedRecord`), or a
#: ``reason`` from ``REJECTION_REASONS`` with a ``detail`` naming the step (never
#: a payload value). ``usecase_id`` is filled as soon as the topic parsed, so a
#: rejection can be counted per use case.
Decision = collections.namedtuple("Decision", ["accepted", "reason", "detail", "record", "usecase_id", "account"])

AcceptedRecord = collections.namedtuple("AcceptedRecord", [
    "usecase_id", "device_id", "workflow_id", "workflow_version", "kind", "schema", "producer", "instance",
    "seq", "event_ms", "received_ms", "ts_source", "message_id", "fields", "dropped", "blocks_dropped", "raw",
    "raw_text", "usecase", "account",
])


class Rejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else "{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


class EventRateCounter:
    """A per-container sliding one-minute counter per Pair (§6.4 step 7b).
    ``accept(pair_key, now_ms)`` is True for the first ``limit`` Events of the
    window and False for the next one."""

    def __init__(self, limit: int = EVENT_RATE_LIMIT_PER_MINUTE, window_ms: int = RATE_WINDOW_MS) -> None:
        self.limit = int(limit)
        self.window_ms = int(window_ms)
        self._lock = threading.Lock()
        self._windows: Dict[str, "collections.deque[int]"] = {}

    def accept(self, pair_key: str, now_ms: int) -> bool:
        now_ms = int(now_ms)
        with self._lock:
            window = self._windows.setdefault(pair_key, collections.deque())
            while window and window[0] <= now_ms - self.window_ms:
                window.popleft()
            if len(window) >= self.limit:
                return False
            window.append(now_ms)
            return True

    def count(self, pair_key: str) -> int:
        with self._lock:
            return len(self._windows.get(pair_key, ()))

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()


def _reject(reason: str, detail: str, usecase_id: str, account: str) -> Decision:
    return Decision(False, reason, detail, None, usecase_id or None, account)


def _region_of(usecase: Dict[str, Any]) -> str:
    region = usecase.get("region")
    if not region and isinstance(usecase.get("regions"), list) and usecase["regions"]:
        region = usecase["regions"][0]
    return str(region or "")


def decide(message: Dict[str, Any], lookups: Lookups, *, account: str, portal_region: str, now_ms: int,
           rate_counter: Optional[EventRateCounter] = None) -> Decision:
    """Steps 1-8 for one rule message. Raises :class:`LookupRetryable` when the
    identity lookup must be retried; every other outcome is a Decision."""
    usecase_id = ""
    try:
        # 2. topic
        try:
            parsed = parse_convention_topic(message.get("t"))
        except TopicError as error:
            raise Rejected("schema_unknown" if error.reason == "unknown_kind" else "attribution_mismatch", "topic")
        if parsed.root != TELEMETRY_ROOT:
            raise Rejected("attribution_mismatch", "topic root")
        if not _UUID_RE.match(parsed.usecase_id) or not _UUID_RE.match(parsed.workflow_id):
            raise Rejected("attribution_mismatch", "topic ids")
        usecase_id, workflow_id, kind = parsed.usecase_id.lower(), parsed.workflow_id.lower(), parsed.kind
        # 3. use case
        usecase = lookups.usecase(usecase_id)
        if not usecase or str(usecase.get("account_id") or "") != str(account):
            raise Rejected("attribution_mismatch", "use case")
        if _region_of(usecase) != portal_region:
            raise Rejected("attribution_mismatch", "region")
        if not lookups.telemetry_enabled(usecase):
            raise Rejected("usecase_disabled")
        # 4. identity
        pr = message.get("pr")
        if not isinstance(pr, str) or not _THUMBPRINT_RE.match(pr):
            raise Rejected("unattributed", "principal")
        try:
            thing = lookups.resolve_thing(pr, usecase)
        except LookupDenied as error:
            raise Rejected("identity_unavailable", str(error)[:200])
        if not thing:
            raise Rejected("unattributed", "no thing")
        if strip_client_suffix(message.get("c")) != thing:
            raise Rejected("attribution_mismatch", "client id")
        # 5. device record (fail-closed)
        device = lookups.device_record(thing)
        if not device or str(device.get("usecase_id") or "") != usecase_id:
            raise Rejected("attribution_mismatch", "device record")
        # 6. workflow
        workflow = lookups.workflow_record(workflow_id)
        if not workflow or str(workflow.get("usecase_id") or "") != usecase_id:
            raise Rejected("workflow_not_in_usecase")
        # 7. schema
        try:
            document, text = schemas.decode_payload(message.get("p"))
            validated = schemas.validate_message(document, text, kind=kind, usecase_id=usecase_id)
        except schemas.SchemaInvalid as error:
            raise Rejected(error.reason, error.detail)
        fields = validated.fields
        if str(fields.get("deviceId")) != thing or str(fields.get("workflowId")) != workflow_id:
            raise Rejected("attribution_mismatch", "payload ids")
        received_ms = _int(message.get("r"), now_ms)
        message_id = str(message.get("m") or "")
        if not message_id:
            raise Rejected("invalid_json", "message id")
        producer = fields.get("producer") or {}
        producer_name = str(producer.get("name") or "")
        instance = str(producer.get("instance") or fields.get("instance") or "")
        seq = fields.get("seq")
        blocks_dropped: List[str] = []
        if kind == "summary":
            fields, blocks_dropped = schemas.drop_unowned_blocks(fields, producer_name)
        elif kind == "live":
            producer_name = producer_name or "live"
        # 7b. the Event rate
        if kind == "event" and rate_counter is not None:
            if not rate_counter.accept(records.pair_pk(usecase_id, thing, workflow_id), now_ms):
                raise Rejected("rate_exceeded")
        # 8. the record
        event_ms, ts_source = records.event_time(kind, fields, received_ms)
        record = AcceptedRecord(
            usecase_id=usecase_id, device_id=thing, workflow_id=workflow_id,
            workflow_version=str(fields.get("workflowVersion") or ""), kind=kind, schema=validated.schema,
            producer=producer_name, instance=instance, seq=int(seq) if seq is not None else None,
            event_ms=event_ms, received_ms=received_ms, ts_source=ts_source, message_id=message_id,
            fields=fields, dropped=list(validated.dropped), blocks_dropped=blocks_dropped, raw=validated.raw,
            raw_text=text, usecase=usecase, account=str(account))
        return Decision(True, None, "", record, usecase_id, str(account))
    except Rejected as error:
        return _reject(error.reason, error.detail, usecase_id, str(account))


def _int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        return int(default)
