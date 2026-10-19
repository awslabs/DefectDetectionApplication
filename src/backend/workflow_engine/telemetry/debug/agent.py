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

"""The Debug_Agent (workflow-debug-mode design §5.1 ``agent.py``, §5.3).

:meth:`DebugAgent.start` builds the agent's own ``IpcPublisher("workflow-debug")``,
the shared ``ShadowControlAgent`` on the ``dda-workflow-debug`` named shadow
(thing name from ``AWS_IOT_THING_NAME``), the process-wide ``RegistrationView``
(``registry_view.get_or_create()``), the session manager, the log tap, the
level guard, the image uploader and one batcher per active session; registers
the run, drop, continuous and model observers of the shared hooks
(enqueue-and-return); and runs the daemon thread ``workflow-debug-agent``
whose tick is: apply queued shadow documents -> drain the tap inbox ->
assemble the run records that arrived two ticks ago -> flush the coalesced
drops -> (every second) expiry and registration re-evaluation, the level
guard's re-assert, the heartbeats -> drain the batchers -> report.

``session`` records are emitted at activation (``started``, or ``resumed``
when the session was enabled before this agent instance started), every 30 s
(``heartbeat``) and at the end (``expired``/``stopped``), with the §4.2
counters; ``drop`` records are coalesced per (node, reason) per second.
Everything the agent does is contained: an observer that raises never
propagates (the hooks guarantee it), a tick that raises is logged and the
next one runs.
"""

import collections
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from workflow_engine.telemetry import hooks as telemetry_hooks
from workflow_engine.telemetry.debug import sessions as sessions_module
from workflow_engine.telemetry.debug.batcher import Batcher
from workflow_engine.telemetry.debug.images import ImageUploader
from workflow_engine.telemetry.debug.logtap import DebugLevelGuard, LogTap
from workflow_engine.telemetry.debug.recorder import assemble_run_record
from workflow_engine.telemetry.debug.sessions import SessionManager, Transition

logger = logging.getLogger(__name__)

__all__ = ["DebugAgent", "AGENT_VERSION", "SHADOW_NAME", "get_agent", "set_agent",
           "debug_status_for"]

AGENT_VERSION = "1.0.0"
SHADOW_NAME = "dda-workflow-debug"
PUBLISHER_NAME = "workflow-debug"
TICK_S = 0.1
SECOND_S = 1.0
HEARTBEAT_S = 30.0
REPORT_CHECK_S = 1.0
#: Readiness events of an execution whose run never finished are forgotten
#: after this long (seconds).
READINESS_TTL_S = 600.0
#: A RunRecord is assembled this many ticks after the one that saw it arrive
#: (design §5.1 ``logtap.py``, review F16).
ASSEMBLE_AFTER_TICKS = 2

_CONTINUOUS_DROP_REASONS = {"skippedBusy": "skipped_busy", "skippedNoNewFrame": "skipped_no_new_frame"}


def _default_secrets() -> List[str]:
    try:
        from dda_logging.redaction import credential_store_secrets

        return credential_store_secrets()
    except Exception:  # noqa: BLE001 - redaction of patterns still applies
        return []


class DebugAgent:
    """See the module docstring. Every collaborator is injectable for the
    host tests (``publisher``, ``shadow_factory``, ``registry_view``,
    ``uploader``, ``clock``/``wall``); ``run_thread=False`` lets a test drive
    :meth:`tick` itself."""

    def __init__(
        self,
        *,
        thing_name: Optional[str] = None,
        publisher: Any = None,
        shadow_factory: Optional[Callable] = None,
        registry_view: Any = None,
        uploader: Any = None,
        secrets: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        run_thread: bool = True,
    ) -> None:
        self.thing_name = thing_name if thing_name is not None else os.environ.get("AWS_IOT_THING_NAME", "")
        self._publisher = publisher
        self._shadow_factory = shadow_factory
        self._view = registry_view
        self._uploader = uploader
        self._secrets = secrets or _default_secrets
        self._clock = clock
        self._wall = wall
        self._run_thread = run_thread
        self.instance = str(uuid.uuid4())
        self.started_ms: Optional[int] = None
        self.publisher = None
        self.shadow = None
        self.view = None
        self.sessions: Optional[SessionManager] = None
        self.tap: Optional[LogTap] = None
        self.guard: Optional[DebugLevelGuard] = None
        self.uploader = None
        self.tap_counters: Dict[str, int] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._tick = 0
        self._desired_queue: Deque[Tuple[dict, int]] = collections.deque()
        self._run_inbox: Deque[Tuple[Any, int]] = collections.deque(maxlen=2000)
        self._drop_inbox: Deque[Tuple[float, Optional[str], Optional[str], str]] = collections.deque(
            maxlen=10000)
        self._drop_coalesce: Dict[Tuple[str, Optional[str], str], Tuple[int, int]] = {}
        self._readiness: Dict[str, Tuple[float, List[dict]]] = {}
        self._model_wait: Dict[str, Optional[dict]] = {}
        self._registrations_changed = threading.Event()
        #: Batchers of ended sessions still handing over their last records.
        self._closing: List[Tuple[Batcher, float]] = []
        self._last_second_at: Optional[float] = None
        self._last_report_at: Optional[float] = None
        self.ticks_run = 0
        self.records_emitted = 0
        self.started = False

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> "DebugAgent":
        if self.started:
            return self
        if not self.thing_name:
            raise RuntimeError("AWS_IOT_THING_NAME is not set; the debug agent needs the thing name")
        self.started_ms = int(self._wall() * 1000)
        if self._publisher is None:
            from workflow_engine.telemetry.publisher import IpcPublisher

            self._publisher = IpcPublisher(PUBLISHER_NAME)
        self.publisher = self._publisher
        if self._view is None:
            from workflow_engine.telemetry import registry_view

            self._view = registry_view.get_or_create()
        self.view = self._view
        self.sessions = SessionManager(self.thing_name, self.view, clock=self._clock, wall=self._wall,
                                       agent_started_ms=self.started_ms)
        self.tap = LogTap(self.sessions, self.tap_counters, self._secrets, wake=self._wake)
        self.guard = DebugLevelGuard(self.tap.handler)
        self.uploader = self._uploader if self._uploader is not None else ImageUploader(clock=self._clock)
        if self._shadow_factory is None:
            from workflow_engine.telemetry.control_shadow import ShadowControlAgent

            self._shadow_factory = ShadowControlAgent
        self.shadow = self._shadow_factory(self.thing_name, SHADOW_NAME, self._on_desired, self.publisher)
        try:
            self.view.add_listener(self._on_registrations)
        except Exception:  # noqa: BLE001 - a view without listeners (tests)
            logger.debug("Debug agent: the registration view takes no listeners", exc_info=True)
        telemetry_hooks.register_run_observer(self._on_run_finished)
        telemetry_hooks.register_drop_observer(self._on_trigger_dropped)
        telemetry_hooks.register_continuous_observer(self._on_continuous_tick)
        telemetry_hooks.register_model_observer(self._on_model_event)
        self.shadow.start()
        self.started = True
        if self._run_thread:
            self._thread = threading.Thread(target=self._loop, name="workflow-debug-agent", daemon=True)
            self._thread.start()
        logger.info("Workflow debug agent started (instance %s, thing %s)", self.instance, self.thing_name)
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
        if self.tap is not None:
            self.tap.detach()
        if self.guard is not None:
            self.guard.restore()
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
                logger.exception("Workflow debug agent: tick failed")

    # -- observers (enqueue and return) ------------------------------------------------

    def _on_desired(self, desired: dict, version: int) -> None:
        with self._lock:
            self._desired_queue.append((desired if isinstance(desired, dict) else {}, version))
        self._wake.set()

    def _on_registrations(self, added, removed) -> None:
        self._registrations_changed.set()
        self._wake.set()

    def _on_run_finished(self, record: Any) -> None:
        with self._lock:
            self._run_inbox.append((record, self._tick))

    def _on_trigger_dropped(self, registration_id: Optional[str], node_id: Optional[str], reason: str) -> None:
        self._drop_inbox.append((self._wall(), registration_id, node_id, str(reason)))

    def _on_continuous_tick(self, registration_id: str, key: str, amount: int) -> None:
        reason = _CONTINUOUS_DROP_REASONS.get(key)
        if reason is None:
            return
        now = self._wall()
        for _ in range(max(1, int(amount or 1))):
            self._drop_inbox.append((now, registration_id, None, reason))

    def _on_model_event(self, kind: str, *args: Any) -> None:
        if kind == "readiness":
            registration_id, execution_id, model, state, waited_ms, ready = args
            with self._lock:
                bucket = self._readiness.get(execution_id)
                if bucket is None:
                    bucket = (self._clock(), [])
                    self._readiness[execution_id] = bucket
                bucket[1].append({"model": model, "state": state, "waitedMs": int(waited_ms or 0),
                                  "ready": bool(ready)})
        elif kind == "wait":
            registration_id, document = args
            with self._lock:
                self._model_wait[registration_id] = dict(document) if isinstance(document, dict) else None

    # -- the tick ---------------------------------------------------------------------

    def tick(self) -> None:
        """One agent tick (the order of the module docstring)."""
        self._tick += 1
        self.ticks_run += 1
        now = self._clock()
        transitions: List[Transition] = []
        for desired, version in self._take_desired():
            transitions.extend(self.sessions.apply_desired(
                desired.get("sessions") if isinstance(desired.get("sessions"), dict) else {},
                version, v=desired.get("v", 1) if desired else 1))
        if self._registrations_changed.is_set():
            self._registrations_changed.clear()
            transitions.extend(self.sessions.tick())
        self._handle_transitions(transitions)
        if self.tap.attached or self.tap.handler.inbox:
            for session, record in self.tap.drain_inbox():
                self._enqueue(session, record)
        self._assemble_pending()
        self._flush_drops(force=False)
        if self._last_second_at is None or now - self._last_second_at >= SECOND_S:
            self._last_second_at = now
            self._handle_transitions(self.sessions.tick())
            self.guard.reassert()
            self._heartbeats()
            self._expire_readiness(now)
        for session in self.sessions.live_sessions():
            if session.batcher is not None:
                session.batcher.drain()
        if self._closing:
            still_open = []
            for batcher, deadline in self._closing:
                batcher.drain(force=True)
                if batcher.pending() and now < deadline:
                    still_open.append((batcher, deadline))
            self._closing = still_open
        if transitions or self._last_report_at is None or now - self._last_report_at >= REPORT_CHECK_S:
            self._last_report_at = now
            self._report()

    def _take_desired(self) -> List[Tuple[dict, int]]:
        with self._lock:
            items = list(self._desired_queue)
            self._desired_queue.clear()
        return items

    def _handle_transitions(self, transitions: List[Transition]) -> None:
        for transition in transitions:
            try:
                self._handle_transition(transition)
            except Exception:  # noqa: BLE001 - one transition never blocks the others
                logger.exception("Workflow debug agent: transition %s -> %s failed",
                                 transition.previous, transition.state)
        if transitions:
            self._settle_tap_and_guard()

    def _handle_transition(self, transition: Transition) -> None:
        session = transition.session
        if transition.state == sessions_module.ACTIVE:
            if session.batcher is None:
                session.batcher = Batcher(
                    queue_maxlen=500, publisher=self.publisher, clock=self._clock,
                    topic=session.topic,
                    envelope={"sessionId": session.session_id, "instance": self.instance,
                              "deviceId": self.thing_name, "workflowId": session.workflow_id,
                              "workflowVersion": session.workflow_version},
                    max_messages_per_second=session.entry.get("maxMessagesPerSecond", 10),
                    max_bytes_per_second=session.entry.get("maxBytesPerSecond", 262144),
                    counters=session.counters, wall=self._wall,
                )
            else:
                session.batcher.envelope["workflowVersion"] = session.workflow_version
            if not session.started_announced:
                session.started_announced = True
                session.last_heartbeat_ms = self.sessions.now_ms()
                self._settle_tap_and_guard()
                self._enqueue(session, self._session_record(session, "active",
                                                            self.sessions.first_record_reason(session)))
            logger.info("Workflow debug session %s for workflow %s is active (registration %s)",
                        session.session_id, session.workflow_id, session.registration_id)
        elif transition.state in (sessions_module.EXPIRED, sessions_module.STOPPED):
            if session.batcher is not None:
                self._enqueue(session, self._session_record(session, transition.state, transition.reason))
                self._settle_tap_and_guard()
                batcher, session.batcher = session.batcher, None
                if batcher.drain(force=True) == 0 or batcher.pending():
                    # The buckets may hold the last records back; keep
                    # draining for a while so the final record gets out.
                    self._closing.append((batcher, self._clock() + 10.0))
            logger.info("Workflow debug session %s for workflow %s ended: %s",
                        session.session_id, session.workflow_id, transition.reason)
        elif transition.state == sessions_module.NO_REGISTRATION:
            logger.info("Workflow debug session %s: no registered workflow %s on this device (%s)",
                        session.session_id, session.workflow_id, transition.reason)

    def _settle_tap_and_guard(self) -> None:
        active = self.sessions.active_sessions()
        if active:
            self.tap.attach()
        else:
            self.tap.detach()
        if any(s.level == "debug" for s in active):
            self.guard.apply()
        else:
            self.guard.restore()

    # -- records ----------------------------------------------------------------------

    def _enqueue(self, session: Any, record: dict) -> None:
        if session is None or session.batcher is None:
            return
        session.batcher.enqueue(record)
        self.records_emitted += 1

    def _session_record(self, session: Any, state: str, reason: str) -> dict:
        counters = dict(session.counters)
        for key in ("tapDrops", "unattributedLines", "otherPairLines"):
            counters[key] = self.tap_counters.get(key, 0)
        record = {
            "type": "session",
            "ts": self.sessions.now_ms(),
            "state": state,
            "reason": reason,
            "instance": self.instance,
            "counters": counters,
            "agentVersion": AGENT_VERSION,
            "level": session.level,
            "images": session.images,
        }
        if session.level == "debug" and self.guard is not None and self.guard.level_downgraded:
            record["levelDowngraded"] = True
        return record

    def _heartbeats(self) -> None:
        now_ms = self.sessions.now_ms()
        for session in self.sessions.active_sessions():
            last = session.last_heartbeat_ms
            if last is None or now_ms - last >= HEARTBEAT_S * 1000:
                session.last_heartbeat_ms = now_ms
                if last is not None:
                    self._enqueue(session, self._session_record(session, "active", "heartbeat"))

    def _assemble_pending(self) -> None:
        ready: List[Any] = []
        with self._lock:
            keep: Deque[Tuple[Any, int]] = collections.deque(maxlen=self._run_inbox.maxlen)
            for record, arrival_tick in self._run_inbox:
                if self._tick - arrival_tick >= ASSEMBLE_AFTER_TICKS:
                    ready.append(record)
                else:
                    keep.append((record, arrival_tick))
            self._run_inbox = keep
        for run_record in ready:
            try:
                self._assemble(run_record)
            except Exception:  # noqa: BLE001
                logger.debug("Workflow debug agent: could not assemble a run record", exc_info=True)

    def _assemble(self, run_record: Any) -> None:
        buffer = self.tap.take_run(run_record.execution_id)
        with self._lock:
            readiness = self._readiness.pop(run_record.execution_id, (0.0, []))[1]
            wait = self._model_wait.get(run_record.registration_id)
        session = self.sessions.session_for_registration(run_record.registration_id)
        if session is None:
            session = self.sessions.session_for_workflow(run_record.workflow_id)
        if session is None or not session.is_active or session.batcher is None:
            return
        record = assemble_run_record(run_record, session, buffer, readiness=readiness, model_wait=wait,
                                     secrets=self.tap._safe_secrets())
        try:
            record["image"] = self.uploader.maybe_upload(session, run_record)
        except Exception:  # noqa: BLE001
            record["image"] = None
        self._enqueue(session, record)

    def _flush_drops(self, force: bool) -> None:
        now_ms = self.sessions.now_ms()
        current_second = now_ms // 1000
        while True:
            try:
                stamp, registration_id, node_id, reason = self._drop_inbox.popleft()
            except IndexError:
                break
            session = self.sessions.session_for_registration(registration_id)
            if session is None or not session.is_active:
                continue
            key = (session.workflow_id, node_id, reason)
            second = int(stamp)
            count, _ = self._drop_coalesce.get(key, (0, second))
            self._drop_coalesce[key] = (count + 1, second)
        for key, (count, second) in list(self._drop_coalesce.items()):
            if force or second < current_second:
                workflow_id, node_id, reason = key
                session = self.sessions.session_for_workflow(workflow_id)
                del self._drop_coalesce[key]
                if session is None or not session.is_active:
                    continue
                self._enqueue(session, {"type": "drop", "ts": second * 1000 + 999, "nodeId": node_id,
                                        "reason": reason, "count": count})

    def _expire_readiness(self, now: float) -> None:
        with self._lock:
            stale = [eid for eid, (seen, _) in self._readiness.items() if now - seen > READINESS_TTL_S]
            for eid in stale:
                del self._readiness[eid]

    def _report(self) -> None:
        try:
            self.shadow.report(self.sessions.reported(self.instance, AGENT_VERSION))
        except Exception:  # noqa: BLE001
            logger.debug("Workflow debug agent: report failed", exc_info=True)

    # -- diagnostics --------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        sessions = self.sessions.status() if self.sessions is not None else {}
        return {
            "instance": self.instance,
            "agentVersion": AGENT_VERSION,
            "started": self.started,
            "threadAlive": bool(self._thread is not None and self._thread.is_alive()),
            "sessions": sessions,
            "tapAttached": bool(self.tap is not None and self.tap.attached),
            "levelGuardActive": bool(self.guard is not None and self.guard.active),
            "counters": dict(self.tap_counters),
            "publisher": self.publisher.status() if hasattr(self.publisher, "status") else None,
            "ticks": self.ticks_run,
        }


# ---------------------------------------------------------------------------
# The process-wide agent
# ---------------------------------------------------------------------------

_agent_lock = threading.Lock()
_agent: Optional[DebugAgent] = None


def set_agent(agent: Optional[DebugAgent]) -> None:
    global _agent
    with _agent_lock:
        _agent = agent


def get_agent() -> Optional[DebugAgent]:
    return _agent


def debug_status_for(workflow_id: str) -> Optional[Dict[str, Any]]:
    """The device run API's ``debug`` field for a registration's workflow:
    ``{active, expiresAt}`` (``{active: False, expiresAt: None}`` without a
    session); ``None`` when no agent runs, so the field stays absent and the
    listing payload is exactly the pre-feature one."""
    agent = get_agent()
    if agent is None:
        return None
    try:
        if agent.sessions is None:
            return {"active": False, "expiresAt": None}
        status = agent.sessions.status().get(workflow_id)
        if status is None:
            return {"active": False, "expiresAt": None}
        return {"active": bool(status.get("active")), "expiresAt": status.get("expiresAt")}
    except Exception:  # noqa: BLE001 - the API never fails over telemetry
        return {"active": False, "expiresAt": None}
