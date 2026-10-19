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

"""The ``dda.event.v1`` helpers of the Telemetry_Agent
(workflow-telemetry-dashboards design §4.2, §5.2 ``events.py``).

- :class:`RunErrorCoalescer`: the ``engine`` producer emits ``run_error``
  only, coalesced per (workflow, failing node, first 64 characters of the
  redacted error): the first failure of a key is emitted at once, later
  failures within the 10 s window are counted and emitted as one Event with
  ``count``, ``firstTs`` and ``lastTs`` when the window closes, so at most
  one Event leaves per key per 10 s. The error text is cut to 1,024
  characters and redacted with
  ``workflow_engine.vendor.workflow_core.stream_url.redact(text, secrets)``
  where ``secrets = dda_logging.redaction.credential_store_secrets()`` (URL
  user information, secret query parameters, Credential_Store values).
- :class:`EventRateLimiter`: 60 Events per minute per Device; the excess is
  dropped and counted (``dropped`` feeds the next Summary's
  ``telemetry.eventsDropped``).
- :func:`build_event`: the §4.2 envelope (``eventId`` UUID, ``ts``,
  ``deviceId``, ``workflowId``, ``workflowVersion``, ``producer``, optional
  ``executionId``, ``severity``) under 16 KB; Events publish at QoS 1.
"""

import collections
import json
import logging
import time
import uuid
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from workflow_engine.vendor.workflow_core.stream_url import redact

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA",
    "EVENT_QOS",
    "EVENT_TYPES",
    "MAX_EVENT_BYTES",
    "MAX_ERROR_CHARS",
    "RunErrorCoalescer",
    "EventRateLimiter",
    "build_event",
    "redact_error",
]

SCHEMA = "dda.event.v1"
EVENT_QOS = 1
TYPE_RUN_ERROR = "run_error"
TYPE_CLIP_READY = "clip_ready"
TYPE_VIOLATION_CONFIRMED = "violation_confirmed"
EVENT_TYPES = (TYPE_VIOLATION_CONFIRMED, TYPE_CLIP_READY, TYPE_RUN_ERROR)
DEFAULT_SEVERITY = {TYPE_RUN_ERROR: "error", TYPE_CLIP_READY: "info",
                    TYPE_VIOLATION_CONFIRMED: "warning"}
SEVERITIES = ("info", "warning", "error")
MAX_EVENT_BYTES = 16384
MAX_ERROR_CHARS = 1024
MAX_NODE_ID_CHARS = 128
MAX_EXECUTION_ID_CHARS = 128
#: The coalescing key uses this many leading characters of the error.
KEY_PREFIX_CHARS = 64
COALESCE_WINDOW_S = 10.0
RATE_LIMIT_PER_MINUTE = 60
RATE_WINDOW_S = 60.0


def _default_secrets() -> List[str]:
    try:
        from dda_logging.redaction import credential_store_secrets

        return list(credential_store_secrets())
    except Exception:  # noqa: BLE001 - the URL rules still apply
        return []


def redact_error(text: Any, secrets: Optional[Iterable[str]] = None) -> str:
    """The error text as it may leave the device: a string, redacted, at
    most 1,024 characters. Never raises."""
    if text is None:
        return ""
    try:
        value = text if isinstance(text, str) else str(text)
    except Exception:  # noqa: BLE001
        value = "<unprintable error>"
    try:
        value = redact(value, list(secrets) if secrets is not None else _default_secrets())
    except Exception:  # noqa: BLE001 - never leak more than the raw text would
        logger.debug("Telemetry events: redaction failed; withholding the text", exc_info=True)
        return "[withheld]"
    return value[:MAX_ERROR_CHARS]


class _PendingKey:
    __slots__ = ("window_started", "count", "first_ts", "last_ts", "execution_id", "error",
                 "node_id", "workflow_id", "workflow_version")

    def __init__(self, now: float, ts_ms: int, execution_id: Optional[str], error: str,
                 node_id: Optional[str], workflow_id: str, workflow_version: str) -> None:
        self.window_started = now
        self.count = 0
        self.first_ts = ts_ms
        self.last_ts = ts_ms
        self.execution_id = execution_id
        self.error = error
        self.node_id = node_id
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version


class RunErrorCoalescer:
    """See the module docstring. ``clock`` is monotonic seconds, ``wall``
    epoch seconds (both injectable); ``secrets`` returns the literals to
    mask (the Credential_Store values by default)."""

    def __init__(
        self,
        *,
        window_s: float = COALESCE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        secrets: Optional[Callable[[], Iterable[str]]] = None,
    ) -> None:
        self._window_s = float(window_s)
        self._clock = clock
        self._wall = wall
        self._secrets = secrets or _default_secrets
        self._pending: Dict[Tuple[str, Optional[str], str], _PendingKey] = {}
        self.observed = 0
        self.emitted = 0
        self.coalesced = 0

    @staticmethod
    def key_for(workflow_id: str, failing_node_id: Optional[str], error: str) -> Tuple[str, Optional[str], str]:
        return (str(workflow_id), failing_node_id, (error or "")[:KEY_PREFIX_CHARS])

    def observe(self, record: Any) -> Optional[Dict[str, Any]]:
        """Fold one failed run. Returns the ``data`` of an Event to publish
        now (the first failure of an idle key) or ``None`` (coalesced into
        the key's open window)."""
        self.observed += 1
        now = self._clock()
        ts_ms = int(getattr(record, "finished_ms", None) or self._wall() * 1000)
        error = redact_error(getattr(record, "error", None) or "run failed", self._secrets())
        node_id = getattr(record, "failing_node_id", None)
        node_id = str(node_id)[:MAX_NODE_ID_CHARS] if node_id is not None else None
        workflow_id = str(getattr(record, "workflow_id", "") or "")
        version = str(getattr(record, "workflow_version", "") or "")
        execution_id = getattr(record, "execution_id", None)
        execution_id = str(execution_id)[:MAX_EXECUTION_ID_CHARS] if execution_id else None
        key = self.key_for(workflow_id, node_id, error)
        pending = self._pending.get(key)
        elapsed = pending is not None and now - pending.window_started >= self._window_s
        if pending is None or (elapsed and pending.count == 0):
            # An idle key: the failure is emitted at once and opens a window.
            self._pending[key] = _PendingKey(now, ts_ms, execution_id, error, node_id, workflow_id, version)
            self.emitted += 1
            return self._event_data(workflow_id, version, execution_id, error, node_id, 1, ts_ms, ts_ms)
        self.coalesced += 1
        if pending.count == 0:
            pending.first_ts = ts_ms
        pending.count += 1
        pending.last_ts = max(pending.last_ts, ts_ms)
        pending.execution_id = execution_id or pending.execution_id
        pending.error = error
        if elapsed:
            # The window closed with coalesced failures and was not flushed
            # yet: this failure joins them in the one Event of the window,
            # and the next window starts now.
            return self._close(key, pending, now)
        return None

    def flush(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Close every window that elapsed. Returns the coalesced Events'
        ``data``; a key whose window gathered nothing is forgotten (its next
        failure is emitted at once), a key that emits starts a new window."""
        now = self._clock() if now is None else now
        due: List[Dict[str, Any]] = []
        for key, pending in list(self._pending.items()):
            if now - pending.window_started >= self._window_s:
                closed = self._close(key, pending, now)
                if closed is not None:
                    due.append(closed)
        return due

    def _close(self, key: Tuple[str, Optional[str], str], pending: _PendingKey,
               now: float) -> Optional[Dict[str, Any]]:
        if pending.count <= 0:
            self._pending.pop(key, None)
            return None
        self.emitted += 1
        data = self._event_data(pending.workflow_id, pending.workflow_version, pending.execution_id,
                                pending.error, pending.node_id, pending.count, pending.first_ts,
                                pending.last_ts)
        # The next window starts at this emission, so the key emits at most
        # once per window.
        self._pending[key] = _PendingKey(now, pending.last_ts, pending.execution_id, pending.error,
                                         pending.node_id, pending.workflow_id, pending.workflow_version)
        return data

    @staticmethod
    def _event_data(workflow_id: str, version: str, execution_id: Optional[str], error: str,
                    node_id: Optional[str], count: int, first_ts: int, last_ts: int) -> Dict[str, Any]:
        return {
            "type": TYPE_RUN_ERROR,
            "workflowId": workflow_id,
            "workflowVersion": version,
            "executionId": execution_id,
            "data": {
                "error": error,
                "failingNodeId": node_id,
                "count": int(count),
                "firstTs": int(first_ts),
                "lastTs": int(last_ts),
            },
        }

    def pending_keys(self) -> int:
        return len(self._pending)


class EventRateLimiter:
    """At most ``limit`` Events per ``window_s`` per Device (a sliding
    window); the excess is refused by :meth:`allow` and counted in
    ``dropped``."""

    def __init__(self, *, limit: int = RATE_LIMIT_PER_MINUTE, window_s: float = RATE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._clock = clock
        self._stamps: Deque[float] = collections.deque()
        self.allowed = 0
        self.dropped = 0

    def allow(self, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else now
        while self._stamps and now - self._stamps[0] >= self.window_s:
            self._stamps.popleft()
        if len(self._stamps) >= self.limit:
            self.dropped += 1
            return False
        self._stamps.append(now)
        self.allowed += 1
        return True


def build_event(
    event_type: str,
    *,
    device_id: str,
    workflow_id: str,
    workflow_version: Any,
    producer: Dict[str, Any],
    data: Dict[str, Any],
    ts: Optional[int] = None,
    execution_id: Optional[str] = None,
    severity: Optional[str] = None,
    event_id: Optional[str] = None,
    wall: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """The ``dda.event.v1`` envelope (§4.2), serializable under 16 KB.

    ``producer`` is ``{name, version, instance}``; ``severity`` defaults by
    type (``run_error`` -> ``error``, ``violation_confirmed`` -> ``warning``,
    ``clip_ready`` -> ``info``). A ``run_error`` whose text makes the
    envelope too large has its ``data.error`` shortened; any other oversize
    envelope raises ``ValueError``.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError("unknown event type: {0!r}".format(event_type))
    if severity is None:
        severity = DEFAULT_SEVERITY[event_type]
    if severity not in SEVERITIES:
        raise ValueError("unknown severity: {0!r}".format(severity))
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    event: Dict[str, Any] = {
        "schema": SCHEMA,
        "type": event_type,
        "eventId": str(event_id) if event_id else str(uuid.uuid4()),
        "ts": int(ts if ts is not None else wall() * 1000),
        "deviceId": str(device_id),
        "workflowId": str(workflow_id),
        "workflowVersion": str(workflow_version if workflow_version is not None else ""),
        "producer": {
            "name": str(producer.get("name", "")),
            "version": str(producer.get("version", "")),
            "instance": str(producer.get("instance", "")),
        },
        "severity": severity,
        "data": data,
    }
    if execution_id:
        event["executionId"] = str(execution_id)[:MAX_EXECUTION_ID_CHARS]
    size = len(encode_event(event))
    if size > MAX_EVENT_BYTES:
        error = data.get("error") if event_type == TYPE_RUN_ERROR else None
        if isinstance(error, str) and error:
            excess = size - MAX_EVENT_BYTES
            data["error"] = error[: max(0, len(error) - excess - 16)]
            size = len(encode_event(event))
        if size > MAX_EVENT_BYTES:
            raise ValueError("event of {0} bytes exceeds {1}".format(size, MAX_EVENT_BYTES))
    return event


def encode_event(event: Dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), default=str).encode("utf-8")
