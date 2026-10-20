#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""The per-session batcher (workflow-debug-mode design §4.1, §5.1
``batcher.py``, §5.4).

One :class:`Batcher` per active session: a drop-oldest queue of records
(``queueDrops``), :meth:`drain` packing them into ``dda.debug.v1`` envelopes
of up to 100 KB (hard cap 120 KB, 1-200 records, ``seq`` per (session,
instance)), the §5.4 truncation order for a single record over budget (each
cut adds a ``{field, originalBytes}`` marker), two continuously refilled token
buckets (``maxMessagesPerSecond`` and ``maxBytesPerSecond``, one-second
burst; a message is handed over only when both hold enough, otherwise the
records stay queued and ``rateDeferred``/``bytesDeferred`` count the
deferrals), the hand-over through the agent's own ``IpcPublisher`` at QoS 1,
and the spooler back-off: every ``spool_full`` rejection the publisher
reports doubles the drain interval (0.5 -> 1 -> 2 -> 4 -> 8 -> 10 s) and every
successful hand-over halves it back towards 0.5 s; a rejected batch is gone
(the publisher dropped it), counted ``publishFailures`` and ``spoolFull``,
never re-sent.
"""

import collections
import json
import logging
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["Batcher", "TokenBucket", "truncate_record", "serialize"]

SCHEMA = "dda.debug.v1"
MESSAGE_SOFT_BYTES = 100 * 1024
MESSAGE_HARD_BYTES = 120 * 1024
MAX_RECORDS_PER_MESSAGE = 200
BASE_DRAIN_INTERVAL_S = 0.5
MAX_DRAIN_INTERVAL_S = 10.0

DETECTIONS_KEEP = 100
TEXT_CAP = 1024
PAYLOAD_CAP = 1024
TRACEBACK_TAIL = 2048
DETAIL_CAP = 200
ERROR_CAP = 1024

_COUNTER_KEYS = ("records", "bytes", "queueDrops", "truncations", "rateDeferred",
                 "bytesDeferred", "spoolFull", "publishFailures")


def serialize(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")


class TokenBucket:
    """A continuously refilled token bucket. ``capacity`` is one second's
    worth of tokens (the burst) unless ``minimum_capacity`` is larger, so a
    single message of the hard cap can always be sent eventually; the bucket
    starts with one second's worth."""

    def __init__(self, rate_per_second: float, clock: Callable[[], float],
                 minimum_capacity: float = 0.0) -> None:
        self.rate = max(0.0, float(rate_per_second))
        self.capacity = max(self.rate, float(minimum_capacity))
        self._clock = clock
        self._level = min(self.rate, self.capacity)
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._level = min(self.capacity, self._level + elapsed * self.rate)

    def level(self) -> float:
        self._refill()
        return self._level

    def allows(self, amount: float) -> bool:
        self._refill()
        return self._level >= amount

    def take(self, amount: float) -> bool:
        self._refill()
        if self._level < amount:
            return False
        self._level -= amount
        return True

    def seconds_until(self, amount: float) -> float:
        self._refill()
        deficit = amount - self._level
        if deficit <= 0:
            return 0.0
        if self.rate <= 0:
            return float("inf")
        return deficit / self.rate


class Batcher:
    """See the module docstring.

    ``publisher`` is the agent's ``IpcPublisher`` (``publish(topic, payload,
    qos)``, ``failures_by_reason``, ``publish_failures``,
    ``publish_successes``); ``envelope`` holds the fixed envelope fields
    (``sessionId``, ``instance``, ``deviceId``, ``workflowId``,
    ``workflowVersion``); ``counters`` is the session's counter map (the
    batcher's keys are created in it when absent). ``wall`` stamps
    ``sentAt`` in epoch milliseconds.
    """

    def __init__(
        self,
        queue_maxlen: int = 500,
        publisher: Any = None,
        clock: Callable[[], float] = time.monotonic,
        *,
        topic: str = "",
        envelope: Optional[dict] = None,
        max_messages_per_second: float = 10.0,
        max_bytes_per_second: float = 262144.0,
        counters: Optional[Dict[str, int]] = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.queue_maxlen = int(queue_maxlen)
        self.publisher = publisher
        self._clock = clock
        self._wall = wall
        self.topic = topic
        self.envelope = dict(envelope or {})
        self.counters: Dict[str, int] = counters if counters is not None else {}
        for key in _COUNTER_KEYS:
            self.counters.setdefault(key, 0)
        self._queue: Deque[dict] = collections.deque(maxlen=self.queue_maxlen)
        self.seq = 0
        self.messages_bucket = TokenBucket(max_messages_per_second, clock, minimum_capacity=1.0)
        self.bytes_bucket = TokenBucket(max_bytes_per_second, clock,
                                        minimum_capacity=float(MESSAGE_HARD_BYTES))
        self.drain_interval_s = BASE_DRAIN_INTERVAL_S
        self._next_drain_at = clock()
        self._spool_full_seen = self._publisher_count("failures_by_reason", "spool_full")
        self._failures_seen = self._publisher_int("publish_failures")
        self._successes_seen = self._publisher_int("publish_successes")
        self.messages_sent = 0

    # -- producers ----------------------------------------------------------------

    def enqueue(self, record: dict) -> None:
        if len(self._queue) >= self.queue_maxlen:
            self.counters["queueDrops"] += 1
        self._queue.append(record)

    def pending(self) -> int:
        return len(self._queue)

    # -- the drain ----------------------------------------------------------------

    def drain(self, force: bool = False) -> int:
        """Pack and hand over as many messages as the queue and the buckets
        allow; honours the back-off interval unless ``force``. Returns the
        number of messages handed over."""
        now = self._clock()
        self._observe_publisher()
        if not force and now < self._next_drain_at:
            return 0
        sent = 0
        while self._queue:
            if not self.messages_bucket.allows(1):
                self.counters["rateDeferred"] += 1
                break
            records, payload = self._pack()
            if not records:
                break
            size = len(payload)
            if not self.bytes_bucket.allows(size):
                self.counters["bytesDeferred"] += 1
                break
            self.messages_bucket.take(1)
            self.bytes_bucket.take(size)
            for _ in records:
                self._queue.popleft()
            self.seq += 1
            try:
                self.publisher.publish(self.topic, payload, 1)
            except Exception:  # noqa: BLE001 - the publisher never raises; belt and braces
                logger.warning("Debug batcher: hand-over failed", exc_info=True)
                self.counters["publishFailures"] += 1
                continue
            self.counters["records"] += len(records)
            self.counters["bytes"] += size
            self.messages_sent += 1
            sent += 1
        self._next_drain_at = now + self.drain_interval_s
        return sent

    def _pack(self) -> Tuple[List[dict], bytes]:
        """The next message from the head of the queue (records are not
        popped): the envelope and its serialized bytes."""
        envelope = dict(self.envelope)
        envelope["schema"] = SCHEMA
        envelope["seq"] = self.seq + 1  # committed on hand-over, so seq never gaps
        envelope["sentAt"] = int(self._wall() * 1000)
        envelope["records"] = []
        overhead = len(serialize(envelope))
        budget = MESSAGE_SOFT_BYTES - overhead
        records: List[dict] = []
        used = 0
        for index, record in enumerate(self._queue):
            if len(records) >= MAX_RECORDS_PER_MESSAGE:
                break
            blob = serialize(record)
            separator = 1 if records else 0
            if used + separator + len(blob) > budget:
                if records:
                    break  # the next message takes it
                record, markers = truncate_record(record, budget)
                if markers:
                    self.counters["truncations"] += len(markers)
                    self._queue[index] = record
                blob = serialize(record)
            records.append(record)
            used += separator + len(blob)
        if not records:
            return [], b""
        envelope["records"] = records
        payload = serialize(envelope)
        if len(payload) > MESSAGE_HARD_BYTES and len(records) > 1:
            # Overhead accounting drifted; send fewer records.
            envelope["records"] = records[:-1]
            records = records[:-1]
            payload = serialize(envelope)
        return records, payload

    # -- publisher observation ----------------------------------------------------

    def _publisher_count(self, attribute: str, key: str) -> int:
        mapping = getattr(self.publisher, attribute, None) or {}
        try:
            return int(mapping.get(key, 0))
        except Exception:  # noqa: BLE001
            return 0

    def _publisher_int(self, attribute: str) -> int:
        try:
            return int(getattr(self.publisher, attribute, 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _observe_publisher(self) -> None:
        """Fold the publisher's counters since the last drain into the
        session counters and adapt the drain interval."""
        spool_full = self._publisher_count("failures_by_reason", "spool_full")
        failures = self._publisher_int("publish_failures")
        successes = self._publisher_int("publish_successes")
        new_spool_full = max(0, spool_full - self._spool_full_seen)
        new_failures = max(0, failures - self._failures_seen)
        new_successes = max(0, successes - self._successes_seen)
        self._spool_full_seen, self._failures_seen, self._successes_seen = (
            spool_full, failures, successes)
        if new_failures:
            self.counters["publishFailures"] += new_failures
        if new_spool_full:
            self.counters["spoolFull"] += new_spool_full
            for _ in range(new_spool_full):
                self.drain_interval_s = min(MAX_DRAIN_INTERVAL_S, self.drain_interval_s * 2)
            logger.info("Debug batcher: the nucleus spooler rejected %d batch(es); drain "
                        "interval now %.1f s", new_spool_full, self.drain_interval_s)
        if new_successes:
            for _ in range(new_successes):
                if self.drain_interval_s <= BASE_DRAIN_INTERVAL_S:
                    break
                self.drain_interval_s = max(BASE_DRAIN_INTERVAL_S, self.drain_interval_s / 2)

    def status(self) -> Dict[str, Any]:
        return {
            "queued": len(self._queue),
            "seq": self.seq,
            "drainIntervalS": self.drain_interval_s,
            "messagesSent": self.messages_sent,
            "counters": dict(self.counters),
        }


# ---------------------------------------------------------------------------
# Truncation (§5.4)
# ---------------------------------------------------------------------------


def _bytes(value: Any) -> int:
    return len(serialize(value))


def _cut_logs(record: dict, src: str, budget: int, markers: List[dict]) -> None:
    logs = record.get("logs")
    if not isinstance(logs, list) or not logs:
        return
    excess = _bytes(record) - budget
    if excess <= 0:
        return
    removed_bytes = 0
    kept: List[dict] = []
    removed_any = False
    for entry in logs:  # oldest first
        if excess > 0 and isinstance(entry, dict) and entry.get("src") == src:
            size = _bytes(entry) + 1
            removed_bytes += size
            excess -= size
            removed_any = True
            continue
        kept.append(entry)
    if removed_any:
        record["logs"] = kept
        markers.append({"field": "logs." + src, "originalBytes": removed_bytes})


def _cut_detections(record: dict, markers: List[dict]) -> None:
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return
    detections = outputs.get("detections")
    if isinstance(detections, list) and len(detections) > DETECTIONS_KEEP:
        original = _bytes(detections)
        outputs["detections"] = detections[:DETECTIONS_KEEP]
        markers.append({"field": "outputs.detections", "originalBytes": original})


def _cut_texts(record: dict, markers: List[dict]) -> None:
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return
    for key in ("llm", "bedrock"):
        group = outputs.get(key)
        if not isinstance(group, dict):
            continue
        for node_id, entry in group.items():
            if isinstance(entry, dict) and isinstance(entry.get("text"), str) \
                    and len(entry["text"].encode("utf-8")) > TEXT_CAP:
                original = len(entry["text"].encode("utf-8"))
                entry["text"] = entry["text"][:TEXT_CAP]
                markers.append({"field": "outputs.{0}.{1}.text".format(key, node_id),
                                "originalBytes": original})


def _cut_payload(record: dict, markers: List[dict]) -> None:
    trigger = record.get("trigger")
    if isinstance(trigger, dict) and isinstance(trigger.get("payload"), str) \
            and len(trigger["payload"].encode("utf-8")) > PAYLOAD_CAP:
        original = len(trigger["payload"].encode("utf-8"))
        trigger["payload"] = trigger["payload"][:PAYLOAD_CAP]
        markers.append({"field": "trigger.payload", "originalBytes": original})


def _cut_traceback(record: dict, markers: List[dict]) -> None:
    text = record.get("traceback")
    if isinstance(text, str) and len(text.encode("utf-8")) > TRACEBACK_TAIL:
        original = len(text.encode("utf-8"))
        record["traceback"] = text[-TRACEBACK_TAIL:]
        markers.append({"field": "traceback", "originalBytes": original})


def _cut_details(record: dict, field: str, markers: List[dict]) -> None:
    items = record.get(field)
    if not isinstance(items, list):
        return
    original = 0
    cut = False
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("detail"), str) \
                and len(item["detail"]) > DETAIL_CAP:
            original += len(item["detail"].encode("utf-8"))
            item["detail"] = item["detail"][:DETAIL_CAP]
            cut = True
    if cut:
        markers.append({"field": field + "[].detail", "originalBytes": original})


def _cut_error(record: dict, markers: List[dict]) -> None:
    text = record.get("error")
    if isinstance(text, str) and len(text.encode("utf-8")) > ERROR_CAP:
        original = len(text.encode("utf-8"))
        record["error"] = text[:ERROR_CAP]
        markers.append({"field": "error", "originalBytes": original})


def truncate_record(record: dict, budget: int) -> Tuple[dict, List[dict]]:
    """Cut ``record`` in the §5.4 order until its serialization fits
    ``budget`` bytes. Returns the (new) record and the markers added to its
    ``truncated`` list; the input is not modified."""
    record = json.loads(serialize(record).decode("utf-8"))  # a deep copy
    markers: List[dict] = []
    budget = max(0, min(int(budget), MESSAGE_HARD_BYTES))
    if _bytes(record) <= budget:
        return record, markers
    steps = (
        lambda: _cut_logs(record, "tap", budget, markers),
        lambda: _cut_logs(record, "runlog", budget, markers),
        lambda: _cut_detections(record, markers),
        lambda: _cut_texts(record, markers),
        lambda: _cut_payload(record, markers),
        lambda: _cut_traceback(record, markers),
        lambda: _cut_details(record, "nodes", markers),
        lambda: _cut_details(record, "bindings", markers),
        lambda: _cut_error(record, markers),
    )
    for step in steps:
        step()
        truncated = record.get("truncated")
        if not isinstance(truncated, list):
            truncated = []
        record["truncated"] = truncated + markers
        if _bytes(record) <= budget:
            return record, markers
        record["truncated"] = truncated  # markers re-appended on the next pass
    replacement = {
        "type": record.get("type", "run"),
        "ts": record.get("ts"),
        "executionId": record.get("executionId"),
        "status": record.get("status"),
        "error": "[record too large]",
        "truncated": (record.get("truncated") or []) + markers + [
            {"field": "record", "originalBytes": _bytes(record)}],
    }
    markers.append({"field": "record", "originalBytes": _bytes(record)})
    return replacement, markers
