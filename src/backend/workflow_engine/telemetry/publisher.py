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

"""The per-agent IPC publisher (dashboards design §5.2 ``publisher.py``).

:class:`IpcPublisher` owns one bounded queue and one daemon thread. Every
agent builds its own instance (``IpcPublisher("telemetry")``,
``IpcPublisher("workflow-debug")``), because an IPC publish can block for up
to 10 s on a stalled connection and one agent's traffic must never delay or
evict another's. The thread calls ``output_bindings.publish_via_ipc`` with
``retain=False``; it never dies: every exception is caught, counted in
``publish_failures`` and ``failures_by_reason`` and logged at most once per
minute per topic.

Two failure shapes are recognised (debug design §5.6, §5.7):

- **denied**: a ``RuntimeError`` whose ``__cause__`` is the nucleus
  ``UnauthorizedError`` (what ``publish_via_ipc`` re-raises). The device
  cannot know its own LocalServer variant, so the fixed ERROR line names the
  missing ``mqttproxy:2`` statement and the one recipe known to lack it.
- **spool_full**: a nucleus ``ServiceError`` from the publish operation,
  the shape the shared MQTT spooler rejects new messages with when full.
  Nothing is re-queued; the debug batcher reads ``failures_by_reason`` for
  its back-off.
"""

import collections
import logging
import sys
import threading
import time
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["IpcPublisher"]

#: Rate limit of the failure lines, per (topic, reason), in seconds.
_LOG_INTERVAL_S = 60.0

#: The fixed denial line (dashboards R5.4; debug design §5.6). Every current
#: LocalServer recipe carries the statement, so a denial points at the
#: deployed component's recipe rather than at a variant.
_DENIAL_LINE = (
    "publish to %s denied by Greengrass IPC: this LocalServer's deployed "
    "recipe lacks mqttproxy:2 (aws.greengrass#PublishToIoTCore on *). "
    "Original diagnosis: %s"
)

_SPOOL_FULL_LINE = (
    "publish to %s rejected by the Greengrass nucleus: its MQTT spooler "
    "(shared by every component, mqtt.spooler.maxSizeInBytes, 2.5 MB in "
    "memory by default) is full; either a debug session's byte cap exceeds "
    "the uplink or the device is offline. The message is dropped: %s"
)

REASON_DENIED = "denied"
REASON_SPOOL_FULL = "spool_full"
REASON_OTHER = "other"


def _ipc_model_class(name: str) -> Optional[type]:
    """``awsiot.greengrasscoreipc.model.<name>`` when the SDK (or its test
    stub) is importable, else ``None``."""
    module = sys.modules.get("awsiot.greengrasscoreipc.model")
    if module is None:
        try:
            import awsiot.greengrasscoreipc.model as module  # type: ignore[no-redef]
        except Exception:  # noqa: BLE001 - no SDK on this host
            return None
    cls = getattr(module, name, None)
    return cls if isinstance(cls, type) else None


def _is_instance_of(obj: Any, class_name: str) -> bool:
    """``isinstance`` against the SDK class when importable, else a match on
    the class name (the SDK is stubbed per test on the host)."""
    if obj is None:
        return False
    cls = _ipc_model_class(class_name)
    if cls is not None and isinstance(obj, cls):
        return True
    return type(obj).__name__ == class_name


def classify_failure(error: BaseException) -> str:
    """``denied`` for the ``RuntimeError``-from-``UnauthorizedError`` shape,
    ``spool_full`` for a nucleus ``ServiceError``, else ``other``."""
    if isinstance(error, RuntimeError) and _is_instance_of(
        error.__cause__, "UnauthorizedError"
    ):
        return REASON_DENIED
    if _is_instance_of(error, "ServiceError") or _is_instance_of(
        error.__cause__, "ServiceError"
    ):
        return REASON_SPOOL_FULL
    return REASON_OTHER


class IpcPublisher:
    """A bounded, drop-oldest publish queue with its own IPC thread.

    ``publish(topic, payload: bytes, qos)`` enqueues (an eviction of the
    oldest entry is counted in ``queue_drops``); ``publish_message(topic,
    payload: str)`` is the QoS 0 convenience ``mqtt.SubscriptionHandler``
    calls on its ``publish_handler``, so an instance can be handed to it
    directly. ``publish_failures`` counts every failed hand-over and
    ``failures_by_reason`` splits it into ``denied``, ``spool_full`` and
    ``other``. ``clock`` is injectable for the log cadence (tests).
    """

    def __init__(
        self, name: str, maxlen: int = 1000, *, clock=time.monotonic
    ) -> None:
        self.name = str(name)
        self.maxlen = int(maxlen)
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: Deque[Tuple[str, bytes, int]] = collections.deque(maxlen=self.maxlen)
        self._in_flight = 0
        self._closed = False
        self.queue_drops = 0
        self.publish_failures = 0
        self.publish_successes = 0
        self.failures_by_reason: Dict[str, int] = {
            REASON_DENIED: 0, REASON_SPOOL_FULL: 0, REASON_OTHER: 0,
        }
        #: (topic, reason) -> monotonic time of the last logged line.
        self._last_logged: Dict[Tuple[str, str], float] = {}
        self._thread = threading.Thread(
            target=self._run, name="{0}-publisher".format(self.name), daemon=True
        )
        self._thread.start()

    # -- producers -----------------------------------------------------------

    def publish(self, topic: str, payload: bytes, qos: int) -> None:
        """Queue one message; drop-oldest when the queue is full."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        with self._cond:
            if self._closed:
                return
            if len(self._queue) >= self.maxlen:
                self.queue_drops += 1  # the deque evicts the oldest entry
            self._queue.append((str(topic), bytes(payload), int(qos)))
            self._cond.notify()

    def publish_message(self, topic: str, payload: str) -> None:
        """The ``SubscriptionHandler.publish_handler`` shape: QoS 0, ``str``."""
        self.publish(topic, (payload or "").encode("utf-8"), 0)

    # -- diagnostics -------------------------------------------------------

    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + self._in_flight

    def status(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "name": self.name,
                "queued": len(self._queue),
                "queueDrops": self.queue_drops,
                "publishFailures": self.publish_failures,
                "publishSuccesses": self.publish_successes,
                "failuresByReason": dict(self.failures_by_reason),
                "threadAlive": self._thread.is_alive(),
            }

    def close(self) -> None:
        """Stop the thread after the queued messages are handed over
        (tests and shutdown; nothing in production needs it)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty and nothing is in flight (tests)."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._queue and self._in_flight == 0, timeout
            )

    # -- the thread ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue and self._closed:
                    return
                topic, payload, qos = self._queue.popleft()
                self._in_flight = 1
            try:
                self._deliver(topic, payload, qos)
            except Exception:  # noqa: BLE001 - the thread never dies
                try:
                    logger.debug("IpcPublisher %s: delivery bookkeeping failed",
                                 self.name, exc_info=True)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._cond:
                    self._in_flight = 0
                    self._cond.notify_all()

    def _deliver(self, topic: str, payload: bytes, qos: int) -> None:
        try:
            # Late-bound so tests replace output_bindings.publish_via_ipc.
            from workflow_engine import output_bindings

            output_bindings.publish_via_ipc(topic, payload, qos)
        except Exception as error:  # noqa: BLE001 - counted, logged, dropped
            reason = classify_failure(error)
            with self._cond:
                self.publish_failures += 1
                self.failures_by_reason[reason] = self.failures_by_reason.get(reason, 0) + 1
            self._log_failure(topic, reason, error)
            return
        with self._cond:
            self.publish_successes += 1

    def _log_failure(self, topic: str, reason: str, error: BaseException) -> None:
        try:
            now = self._clock()
            key = (topic, reason)
            last = self._last_logged.get(key)
            if last is not None and now - last < _LOG_INTERVAL_S:
                return
            self._last_logged[key] = now
            if reason == REASON_DENIED:
                logger.error(_DENIAL_LINE, topic, error)
            elif reason == REASON_SPOOL_FULL:
                logger.warning(_SPOOL_FULL_LINE, topic, _describe(error))
            else:
                logger.warning(
                    "IpcPublisher %s: publish to %s failed (%s); the message is "
                    "dropped", self.name, topic, _describe(error),
                )
        except Exception:  # noqa: BLE001 - never let a log line kill the thread
            pass


def _describe(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:  # noqa: BLE001
        text = "<unprintable>"
    return "{0}: {1}".format(type(error).__name__, text) if text else type(error).__name__
