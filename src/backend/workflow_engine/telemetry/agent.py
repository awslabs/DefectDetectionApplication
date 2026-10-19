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

"""The Telemetry_Agent (workflow-telemetry-dashboards design §5.2 ``agent.py``,
§5.4, §5.5): wiring only.

:meth:`TelemetryAgent.start` builds the agent's **own** ``IpcPublisher("telemetry")``,
the shared ``ShadowControlAgent`` on the ``dda-workflow-telemetry`` named shadow
(thing name from ``AWS_IOT_THING_NAME``; ``desired_state.apply`` as
``on_desired``), the process-wide ``RegistrationView``
(``registry_view.get_or_create()``), the ``IntervalSummarizer``, the
``RunErrorCoalescer``, the ``EventRateLimiter`` and the ``LiveFrameSender``;
registers the run, drop, continuous and model observers of the shared hooks
(enqueue and return) and a continuous status provider (``paused`` and
``effective_fps()`` of each ``ContinuousRunner`` through the continuous
manager); and publishes Summaries on
``dda/telemetry/<usecaseId>/<workflowId>/summary`` (QoS 0), Events on
``.../event`` (QoS 1) and live messages on ``.../live`` (QoS 0). Nothing is
published while ``usecaseId`` is absent or ``enabled`` is false.

The agent's daemon thread ``workflow-telemetry-agent`` applies the queued
shadow documents, publishes the coalesced ``run_error`` Events, drops lapsed
leases and writes ``reported`` through the shadow agent's cadence; the
summarizer and the sender run their own threads. Everything is contained: an
observer that raises never propagates (the hooks guarantee it), a tick that
raises is logged and the next one runs, a failed start leaves LocalServer
without telemetry (``utils/server_setup.py``).
"""

import collections
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from workflow_engine.telemetry import hooks as telemetry_hooks
from workflow_engine.telemetry import topics
from workflow_engine.telemetry.desired_state import TelemetryDesiredState
from workflow_engine.telemetry.events import (
    EVENT_QOS, TYPE_RUN_ERROR, EventRateLimiter, RunErrorCoalescer, build_event, encode_event,
)
from workflow_engine.telemetry.live_frames import LiveFrameSender
from workflow_engine.telemetry.summarizer import (
    Emission, IntervalSummarizer, PRODUCER_NAME, SummarizerSettings,
)

logger = logging.getLogger(__name__)

__all__ = ["TelemetryAgent", "AGENT_VERSION", "SHADOW_NAME", "PUBLISHER_NAME", "get_agent",
           "set_agent"]

AGENT_VERSION = "1.0.0"
SHADOW_NAME = "dda-workflow-telemetry"
PUBLISHER_NAME = "telemetry"
SUMMARY_QOS = 0
TICK_S = 0.25
REPORT_CHECK_S = 1.0
#: ``reported`` keys that ride along without triggering a shadow write
#: (``ShadowControlAgent.report`` strips them for its change detection).
REPORT_COUNTER_KEYS = ("counters", "lastPublishAt", "lastSeq", "framesTooLarge")
ERROR_INBOX_MAXLEN = 2000


def _default_secrets() -> List[str]:
    try:
        from dda_logging.redaction import credential_store_secrets

        return list(credential_store_secrets())
    except Exception:  # noqa: BLE001 - the URL rules still apply
        return []


def _default_status_provider(registration_id: str) -> Optional[Dict[str, Any]]:
    """``{paused, effectiveFps}`` of the registration's ``ContinuousRunner``,
    ``None`` for a registration without one."""
    from workflow_engine import runtime

    manager = runtime.get_continuous_manager()
    if manager is None:
        return None
    runner = manager.runner(registration_id)
    if runner is None:
        return None
    return {"paused": bool(runner.paused), "effectiveFps": float(runner.effective_fps())}


class TelemetryAgent:
    """See the module docstring. Every collaborator is injectable for the host
    tests (``publisher``, ``shadow_factory``, ``registry_view``,
    ``client_factory`` for S3, ``status_provider``, ``secrets``, ``clock``
    (monotonic) and ``wall`` (epoch seconds)); ``run_thread=False`` lets a
    test drive :meth:`tick` itself (the summarizer and the sender then run
    inside it too)."""

    def __init__(
        self,
        *,
        thing_name: Optional[str] = None,
        publisher: Any = None,
        shadow_factory: Optional[Callable] = None,
        registry_view: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        status_provider: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        secrets: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        run_thread: bool = True,
    ) -> None:
        self.thing_name = thing_name if thing_name is not None else os.environ.get("AWS_IOT_THING_NAME", "")
        self._publisher = publisher
        self._shadow_factory = shadow_factory
        self._view = registry_view
        self._client_factory = client_factory
        self._status_provider = status_provider or _default_status_provider
        self._secrets = secrets or _default_secrets
        self._clock = clock
        self._wall = wall
        self._run_thread = run_thread
        self.instance = str(uuid.uuid4())
        self.started_ms: Optional[int] = None
        self.publisher = None
        self.shadow = None
        self.view = None
        self.state: Optional[TelemetryDesiredState] = None
        self.summarizer: Optional[IntervalSummarizer] = None
        self.coalescer: Optional[RunErrorCoalescer] = None
        self.rate_limiter: Optional[EventRateLimiter] = None
        self.sender: Optional[LiveFrameSender] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._desired_queue: Deque[Tuple[dict, int]] = collections.deque()
        self._error_inbox: Deque[Any] = collections.deque(maxlen=ERROR_INBOX_MAXLEN)
        self._last_report_at: Optional[float] = None
        self.published = 0
        self.summaries_published = 0
        self.events_published = 0
        self.events_suppressed = 0
        self.last_publish_at: Optional[int] = None
        self.ticks_run = 0
        self.started = False

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> "TelemetryAgent":
        if self.started:
            return self
        if not self.thing_name:
            raise RuntimeError("AWS_IOT_THING_NAME is not set; the telemetry agent needs the thing name")
        self.started_ms = int(self._wall() * 1000)
        if self._publisher is None:
            from workflow_engine.telemetry.publisher import IpcPublisher

            self._publisher = IpcPublisher(PUBLISHER_NAME)
        self.publisher = self._publisher
        if self._view is None:
            from workflow_engine.telemetry import registry_view

            self._view = registry_view.get_or_create()
        self.view = self._view
        self.state = TelemetryDesiredState(self.thing_name, wall=self._wall, on_change=self._reconfigure)
        self.coalescer = RunErrorCoalescer(clock=self._clock, wall=self._wall, secrets=self._secrets)
        self.rate_limiter = EventRateLimiter(clock=self._clock)
        self.summarizer = IntervalSummarizer(
            SummarizerSettings(enabled=False), self._wall, self.view,
            device_id=self.thing_name, instance=self.instance, producer_version=AGENT_VERSION,
            sink=self._publish_summary, status_provider=self._status_provider,
            counters_provider=self._telemetry_counters,
        )
        self.sender = LiveFrameSender(
            instance=self.instance, device_id=self.thing_name, publish=self._publish_live,
            topic_for=self._live_topic, person_classes=lambda: self.state.person_classes,
            client_factory=self._client_factory, clock=self._wall, run_thread=self._run_thread,
        )
        if self._shadow_factory is None:
            from workflow_engine.telemetry.control_shadow import ShadowControlAgent

            self._shadow_factory = ShadowControlAgent
        self.shadow = self._shadow_factory(self.thing_name, SHADOW_NAME, self._on_desired, self.publisher,
                                           counter_keys=REPORT_COUNTER_KEYS)
        try:
            self.view.add_listener(self._on_registrations)
        except Exception:  # noqa: BLE001 - a view without listeners (tests)
            logger.debug("Telemetry agent: the registration view takes no listeners", exc_info=True)
        telemetry_hooks.register_run_observer(self._on_run_finished)
        telemetry_hooks.register_drop_observer(self._on_trigger_dropped)
        telemetry_hooks.register_continuous_observer(self._on_continuous_tick)
        telemetry_hooks.register_model_observer(self._on_model_event)
        self.shadow.start()
        self.started = True
        if self._run_thread:
            self.summarizer.start()
            self._thread = threading.Thread(target=self._loop, name="workflow-telemetry-agent", daemon=True)
            self._thread.start()
        logger.info("Workflow telemetry agent started (instance %s, thing %s); it publishes once the "
                    "%s shadow enables it with a use case id", self.instance, self.thing_name, SHADOW_NAME)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            telemetry_hooks._unregister(telemetry_hooks._run_observers, self._on_run_finished)
            telemetry_hooks._unregister(telemetry_hooks._drop_observers, self._on_trigger_dropped)
            telemetry_hooks._unregister(telemetry_hooks._continuous_observers, self._on_continuous_tick)
            telemetry_hooks._unregister(telemetry_hooks._model_observers, self._on_model_event)
        except Exception:  # noqa: BLE001
            pass
        if self.view is not None:
            try:
                self.view.remove_listener(self._on_registrations)
            except Exception:  # noqa: BLE001
                pass
        if self.summarizer is not None:
            self.summarizer.stop()
        if self.sender is not None:
            self.sender.stop()
        if self.shadow is not None:
            try:
                self.shadow.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(TICK_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the next tick runs
                logger.exception("Workflow telemetry agent: tick failed")

    # -- observers (enqueue and return) ------------------------------------------------

    def _on_desired(self, desired: dict, version: int) -> None:
        with self._lock:
            self._desired_queue.append((desired if isinstance(desired, dict) else {}, version))
        self._wake.set()

    def _on_registrations(self, added, removed) -> None:
        if self.summarizer is not None:
            self.summarizer.wake()
        self._wake.set()

    def _on_run_finished(self, record: Any) -> None:
        self.summarizer.observe_run(record)
        if getattr(record, "status", None) == "completed":
            self.sender.on_run_finished(record)
        elif self.state is not None and self.state.active:
            self._error_inbox.append(record)
            self._wake.set()

    def _on_trigger_dropped(self, registration_id: Optional[str], node_id: Optional[str], reason: str) -> None:
        self.summarizer.observe_drop(registration_id, node_id, reason)

    def _on_continuous_tick(self, registration_id: str, key: str, amount: int) -> None:
        self.summarizer.observe_continuous(registration_id, key, amount)

    def _on_model_event(self, kind: str, *args: Any) -> None:
        self.summarizer.observe_model(kind, *args)

    # -- the tick ---------------------------------------------------------------------

    def tick(self) -> None:
        """Apply the queued shadow documents, publish the due Events, drop
        lapsed leases and report. Without the threads (tests) the summarizer
        and the sender run here too."""
        self.ticks_run += 1
        now = self._clock()
        for desired, version in self._take_desired():
            self.state.apply(desired, version)
        if not self._run_thread:
            self.summarizer.tick()
        self._publish_events(now)
        self.sender.tick()
        if not self._run_thread:
            self.sender.drain()
        if self._last_report_at is None or now - self._last_report_at >= REPORT_CHECK_S:
            self._last_report_at = now
            self._report()

    def _take_desired(self) -> List[Tuple[dict, int]]:
        with self._lock:
            items = list(self._desired_queue)
            self._desired_queue.clear()
        return items

    def _reconfigure(self, state: TelemetryDesiredState) -> None:
        """``desired_state.on_change``: re-tune the summarizer and the sender."""
        self.summarizer.configure(state.summarizer_settings())
        self.sender.set_leases(state.current_leases())
        if not state.active:
            self._error_inbox.clear()
        self._last_report_at = None  # report the change at this tick

    # -- publishing -----------------------------------------------------------------------

    def _producer(self) -> Dict[str, str]:
        return {"name": PRODUCER_NAME, "version": AGENT_VERSION, "instance": self.instance}

    def _topic(self, workflow_id: str, kind: str) -> Optional[str]:
        state = self.state
        if state is None or not state.active:
            return None
        try:
            return topics.convention_topic(topics.TELEMETRY_ROOT, state.usecase_id, workflow_id, kind)
        except topics.TopicError:
            logger.warning("Telemetry agent: no convention topic for workflow %r; nothing published",
                           workflow_id)
            return None

    def _live_topic(self, workflow_id: str) -> Optional[str]:
        return self._topic(workflow_id, "live")

    def _publish_summary(self, emission: Emission) -> None:
        """The summarizer's sink: one Summary or Heartbeat on ``.../summary``."""
        topic = self._topic(emission.workflow_id, "summary")
        if topic is None:
            return
        payload = json.dumps(emission.payload, separators=(",", ":"), default=str).encode("utf-8")
        self.publisher.publish(topic, payload, SUMMARY_QOS)
        with self._lock:
            self.published += 1
            self.summaries_published += 1
            self.last_publish_at = int(emission.payload.get("emittedAt") or self._wall() * 1000)

    def _publish_live(self, topic: str, payload: bytes, qos: int) -> None:
        self.publisher.publish(topic, payload, qos)
        with self._lock:
            self.published += 1
            self.last_publish_at = int(self._wall() * 1000)

    def _publish_events(self, now: float) -> None:
        records = []
        while True:
            try:
                records.append(self._error_inbox.popleft())
            except IndexError:
                break
        due: List[Dict[str, Any]] = []
        if self.state.active:
            for record in records:
                try:
                    data = self.coalescer.observe(record)
                except Exception:  # noqa: BLE001 - one bad record never stops the others
                    logger.debug("Telemetry agent: could not coalesce a run error", exc_info=True)
                    continue
                if data is not None:
                    due.append(data)
        due.extend(self.coalescer.flush(now))
        if not due:
            return
        if not self.state.active:
            self.events_suppressed += len(due)
            return
        now_ms = int(self._wall() * 1000)
        for data in due:
            topic = self._topic(data["workflowId"], "event")
            if topic is None:
                self.events_suppressed += 1
                continue
            if not self.rate_limiter.allow(now):
                continue  # counted in rate_limiter.dropped -> telemetry.eventsDropped
            try:
                event = build_event(
                    TYPE_RUN_ERROR, device_id=self.thing_name, workflow_id=data["workflowId"],
                    workflow_version=data["workflowVersion"], producer=self._producer(),
                    data=data["data"], ts=now_ms, execution_id=data.get("executionId"), wall=self._wall,
                )
            except ValueError:
                logger.warning("Telemetry agent: run_error event for workflow %s could not be built",
                               data["workflowId"], exc_info=True)
                continue
            self.publisher.publish(topic, encode_event(event), EVENT_QOS)
            with self._lock:
                self.published += 1
                self.events_published += 1
                self.last_publish_at = now_ms

    def _telemetry_counters(self) -> Dict[str, int]:
        """The cumulative counters the Summaries carry as deltas."""
        publisher = self.publisher
        return {
            "queueDrops": int(getattr(publisher, "queue_drops", 0) or 0),
            "publishFailures": int(getattr(publisher, "publish_failures", 0) or 0),
            "eventsDropped": int(self.rate_limiter.dropped if self.rate_limiter is not None else 0),
        }

    def _report(self) -> None:
        try:
            publisher = self.publisher
            counters = {
                "published": self.published,
                "queueDrops": int(getattr(publisher, "queue_drops", 0) or 0),
                "publishFailures": int(getattr(publisher, "publish_failures", 0) or 0),
            }
            self.shadow.report(self.state.reported(
                agent_version=AGENT_VERSION, counters=counters, last_publish_at=self.last_publish_at,
                live_status=self.sender.live_status(),
            ))
        except Exception:  # noqa: BLE001
            logger.debug("Workflow telemetry agent: report failed", exc_info=True)

    # -- diagnostics --------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        state = self.state.status() if self.state is not None else {}
        summarizer = self.summarizer.status() if self.summarizer is not None else {}
        sender = self.sender.status() if self.sender is not None else {}
        return {
            "instance": self.instance,
            "agentVersion": AGENT_VERSION,
            "started": self.started,
            "threadAlive": bool(self._thread is not None and self._thread.is_alive()),
            "enabled": bool(state.get("enabled", False)),
            "usecaseId": state.get("usecaseId"),
            "active": bool(self.state is not None and self.state.active),
            "trackedRegistrations": list(summarizer.get("trackedRegistrations", [])),
            "seq": summarizer.get("seq", 0),
            "counters": {
                "published": self.published,
                "summariesPublished": self.summaries_published,
                "eventsPublished": self.events_published,
                "eventsSuppressed": self.events_suppressed,
                "eventsDropped": int(self.rate_limiter.dropped if self.rate_limiter is not None else 0),
                "queueDrops": int(getattr(self.publisher, "queue_drops", 0) or 0),
                "publishFailures": int(getattr(self.publisher, "publish_failures", 0) or 0),
            },
            "framesTooLarge": int(sender.get("framesTooLarge", 0)),
            "lastPublishAt": self.last_publish_at,
            "desired": state,
            "summarizer": summarizer,
            "live": sender,
            "publisher": self.publisher.status() if hasattr(self.publisher, "status") else None,
            "ticks": self.ticks_run,
        }


# ---------------------------------------------------------------------------
# The process-wide agent
# ---------------------------------------------------------------------------

_agent_lock = threading.Lock()
_agent: Optional[TelemetryAgent] = None


def set_agent(agent: Optional[TelemetryAgent]) -> None:
    global _agent
    with _agent_lock:
        _agent = agent


def get_agent() -> Optional[TelemetryAgent]:
    return _agent
