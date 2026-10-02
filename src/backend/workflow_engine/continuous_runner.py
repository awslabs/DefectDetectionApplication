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
"""The Continuous_Runner (rtsp-rtmp-stream-cameras Requirement 11;
design component 14).

:class:`ContinuousRunnerManager` is a watcher registrations listener. It
owns one :class:`ContinuousRunner` thread per ``registered`` registration
whose stream node runs in ``continuous`` mode, and stops a runner when its
registration is removed, superseded or invalidated. Stopping lets the run
in progress finish; the StreamLeaseKeeper releases the camera's lease.

A runner keeps a monotonic schedule of Sampling_Ticks,
``next_tick += 1 / frames_per_second``. At each tick:

========================================  ===================================
Condition                                 Action
========================================  ===================================
Paused                                    Wait; resuming ticks at once.
Session not ``streaming``                 Record one ``streamUnavailable``
                                          per outage; tick at once when
                                          streaming resumes.
A model the workflow uses is not          Record one ``modelUnavailable``
``READY`` in Triton (the                  per wait, then re-check every 3 s
``model_gate.ModelGate`` returns a        at most; insert no run row and
wait)                                     clear the schedule, so the first
                                          tick after the wait is immediate.
                                          A wait past 600 s is stalled.
No frame newer than the last one run      Count ``skippedNoNewFrame``.
Otherwise                                 Insert a pending run with
                                          ``{"source": "continuous",
                                          "frameSeq", "frameAcquiredAtMs",
                                          "tickAtMs"}``, hand the frame over,
                                          and execute the run on the runner
                                          thread.
========================================  ===================================

Ticks never queue: every tick that elapses while a run is in progress is
counted as ``skippedBusy`` and the schedule moves to the next future tick.
The executor is called directly and returns at the run's terminal state,
so the achievable rate is bounded by run duration, not by status polling.
Each run executes inside the ``continuous_run`` logging context, which
keeps its INFO lines out of the component log; the runner itself logs
state changes and at most one summary line per minute.

The operator pause and the counters persist in ``workflow_continuous_state``
(the counters are snapshotted by the run retention housekeeping, and when
a runner stops). A superseded registration's row is deleted.
"""
import collections
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from dda_logging.run_context import continuous_run
from workflow_engine.model_gate import STALL_AFTER_S, ModelGate, ModelWait
from workflow_engine.stream_feed import (
    CONTINUOUS_SOURCE,
    FRAME_HANDOFF,
    StreamFeed,
    StreamFeedError,
    load_configured_stream_cameras,
    plan_stream_feeds,
)

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_WAITING = "waiting_for_stream"
STATE_WAITING_MODEL = "waiting_for_model"

#: The per-registration counters (Requirement 12.5), in report order.
#: ``modelUnavailable`` is last: :func:`new_counters` accepts a stored
#: snapshot that predates it.
COUNTER_KEYS = ("started", "completed", "failed", "skippedBusy", "skippedNoNewFrame",
                "notable", "outputsSent", "streamUnavailable", "modelUnavailable")

RATE_WINDOW_S = 60.0
SUMMARY_INTERVAL_S = 60.0
#: How often a paused or waiting runner checks again.
IDLE_POLL_S = 0.5
#: How often the model gate is consulted, at most (Requirement 11.11).
MODEL_POLL_S = 3.0
#: How often a stalled model wait is logged again.
STALL_LOG_INTERVAL_S = 300.0

_STREAMING = "streaming"
_COMPLETED = "completed"
_FAILED = "failed"


#: The ``emltriton`` element factory a compiled document runs models with.
_MODEL_FACTORY = "emltriton"


def document_model_names(document: Any) -> Tuple[str, ...]:
    """The distinct Triton model names a compiled document uses.

    The ``args["model"]`` of every ``emltriton`` element, in document
    order. A document without one gets no model gate, and so makes no
    Triton call at all.
    """
    models: list = []
    if not isinstance(document, dict):
        return ()
    for segment in document.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for element in segment.get("elements") or []:
            if not isinstance(element, dict) or element.get("factory") != _MODEL_FACTORY:
                continue
            args = element.get("args")
            if not isinstance(args, dict):
                continue
            model = args.get("model")
            if model and model not in models:
                models.append(model)
    return tuple(models)


def new_counters(stored: Any = None) -> Dict[str, int]:
    """Zeroed counters, overlaid by the valid entries of ``stored``."""
    counters = {key: 0 for key in COUNTER_KEYS}
    if isinstance(stored, dict):
        for key in COUNTER_KEYS:
            value = stored.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counters[key] = value
    return counters


class ExecutionStore:
    """Inserts a continuous run's pending ``workflow_executions`` row, the
    way the trigger runtime does, and reads a run's status back."""

    def __init__(self, session_factory: Optional[Callable] = None):
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal
            self._session_factory = SessionLocal
        return self._session_factory()

    def insert(self, registration_id: str, trigger_context: Dict[str, Any]) -> str:
        from workflow_engine.models import WorkflowExecution
        from workflow_engine.trigger_runtime import EXECUTION_STATUS_PENDING, serialize_trigger_context
        from workflow_engine.watcher import new_execution_id

        session = self._session()
        try:
            execution = WorkflowExecution(
                id=new_execution_id(), registration_id=registration_id, started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING, trigger_context_json=serialize_trigger_context(trigger_context))
            session.add(execution)
            session.commit()
            return execution.id
        finally:
            session.close()

    def status(self, execution_id: str) -> Optional[str]:
        from workflow_engine.models import WorkflowExecution

        session = self._session()
        try:
            execution = session.get(WorkflowExecution, execution_id)
            return execution.status if execution is not None else None
        finally:
            session.close()


class ContinuousRunner:
    """One continuous registration's Sampling_Tick loop (see the module
    docstring). :meth:`step` makes one scheduling decision and returns how
    long to wait before the next, so tests drive it with a fake clock."""

    def __init__(self, registration_id: str, feed: StreamFeed, *,
                 execute: Callable[[str], None],
                 stream_manager: Any,
                 store: Any,
                 retention: Any = None,
                 output_ids: Sequence[str] = (),
                 handoff: Any = FRAME_HANDOFF,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 paused: bool = False,
                 paused_at_ms: Optional[int] = None,
                 counters: Optional[Dict[str, int]] = None,
                 models: Sequence[str] = (),
                 engine_started_at: Optional[float] = None,
                 gate: Any = None,
                 on_exit: Optional[Callable[["ContinuousRunner"], None]] = None):
        self.registration_id = registration_id
        self.feed = feed
        self.output_ids = tuple(output_ids)
        self.models = tuple(models)
        self._execute = execute
        self._stream_manager = stream_manager
        self._store = store
        self._retention = retention
        self._handoff = handoff
        self._clock = clock
        self._wall = wall
        self._on_exit = on_exit
        # The model gate (Requirement 11.11): none at all for a document
        # without an ``emltriton`` element, which therefore never consults
        # Triton.
        if gate is None and self.models:
            gate = ModelGate(self.models, clock=clock, wall=wall,
                             engine_started_at=engine_started_at)
        self._gate = gate
        self._model_wait: Optional[ModelWait] = None
        self._gate_checked_at: Optional[float] = None
        #: The gate is consulted at the runner's start and after any run
        #: that did not complete; while waiting it is polled instead.
        self._recheck_gate = True
        self._stall_logged_at: Optional[float] = None
        self._period = 1.0 / max(0.05, float(feed.frames_per_second))
        self._lock = threading.Lock()
        self._counters = new_counters(counters)
        self._paused = bool(paused)
        self._paused_at_ms = paused_at_ms if paused else None
        self._next_tick: Optional[float] = None
        self._last_seq = 0
        self._outage = False
        self._in_flight: Optional[str] = None
        self._run_starts = collections.deque()
        started = self._clock()
        self._rate_since = started
        self._last_summary = started
        self._summary_base = dict(self._counters)
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- identity --------------------------------------------------------------

    @property
    def fingerprint(self) -> Tuple:
        return runner_fingerprint(self.feed, self.output_ids, self.models)

    @property
    def camera(self) -> str:
        return self.feed.camera_source_id or self.feed.camera_key

    # -- control ---------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="continuous-{0}".format(self.registration_id))
        self._thread.start()

    def stop(self) -> None:
        """Start no more runs; a run in progress finishes."""
        self._stopping.set()
        self._wake.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stopping.is_set()

    def pause(self) -> None:
        with self._lock:
            if self._paused:
                return
            self._paused = True
            self._paused_at_ms = int(self._wall() * 1000)
        logger.info("Continuous workflow %s paused", self.registration_id)
        self._wake.set()

    def resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._paused, self._paused_at_ms = False, None
            self._rate_since = self._clock()
            self._run_starts.clear()
        logger.info("Continuous workflow %s resumed", self.registration_id)
        self._wake.set()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def paused_at_ms(self) -> Optional[int]:
        with self._lock:
            return self._paused_at_ms

    # -- the schedule ----------------------------------------------------------

    def _count(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] += amount

    def _health(self) -> Dict[str, Any]:
        try:
            health = self._stream_manager.health(self.feed.camera_key)
        except Exception:  # noqa: BLE001 - an unreadable camera is not streaming
            logger.debug("Stream health of %s unavailable", self.feed.camera_key, exc_info=True)
            health = None
        return health if isinstance(health, dict) else {}

    def step(self) -> Optional[float]:
        """One scheduling decision; the seconds until the next one, or None
        once stopped."""
        if self._stopping.is_set():
            return None
        now = self._clock()
        self._maybe_summarize(now)
        if self.paused:
            self._next_tick = None
            return IDLE_POLL_S
        health = self._health()
        if health.get("state") != _STREAMING:
            if not self._outage:
                self._outage = True
                self._count("streamUnavailable")
                logger.warning("Continuous workflow %s: stream camera %s is %s; waiting for it",
                               self.registration_id, self.camera, health.get("state") or "not connected")
            self._next_tick = None
            return min(IDLE_POLL_S, self._period)
        if self._outage:
            self._outage = False
            logger.info("Continuous workflow %s: stream camera %s is streaming; resuming",
                        self.registration_id, self.camera)
        if self._model_gate(now) is not None:
            # Requirement 11.11: no run is started, and no row is inserted,
            # while a model the workflow uses is not READY. The schedule is
            # cleared so the first tick after the wait is immediate.
            self._next_tick = None
            return MODEL_POLL_S
        if self._next_tick is None:
            self._next_tick = now
        if now < self._next_tick:
            return self._next_tick - now
        due = self._next_tick
        # Ticks this loop reached too late for were not processed at their
        # time: they are skipped, never queued.
        while due + self._period <= now:
            self._count("skippedBusy")
            due += self._period
        self._next_tick = due + self._period
        frame = self._stream_manager.latest_frame(self.feed.camera_key, after_seq=self._last_seq,
                                                  max_age_ms=self.feed.max_frame_age_ms, wait_ms=0)
        if frame is None:
            self._count("skippedNoNewFrame")
            return max(0.0, self._next_tick - self._clock())
        self._run(frame)
        now = self._clock()
        # Every tick that elapsed while the run was in progress is skipped.
        while self._next_tick <= now:
            self._count("skippedBusy")
            self._next_tick += self._period
        return max(0.0, self._next_tick - now)

    def _model_gate(self, now: float) -> Optional[ModelWait]:
        """The outstanding model wait, or None when the models are ready.

        Consulted at the runner's start, after any run that did not
        complete, and every :data:`MODEL_POLL_S` while waiting — never more
        often than that, and never at all for a document without a model.
        """
        if self._gate is None:
            return None
        waiting = self._model_wait is not None
        if not waiting and not self._recheck_gate:
            return None
        if self._gate_checked_at is not None and now - self._gate_checked_at < MODEL_POLL_S:
            return self._model_wait
        self._gate_checked_at = now
        self._recheck_gate = False
        try:
            wait = self._gate.check()
        except Exception:  # noqa: BLE001 - fail open: never stop runs on the gate itself
            logger.warning("Continuous workflow %s: the model gate failed; running anyway",
                           self.registration_id, exc_info=True)
            wait = None
        self._note_model_wait(wait, now)
        return wait

    def _note_model_wait(self, wait: Optional[ModelWait], now: float) -> None:
        """One WARNING when a wait starts, one INFO when it ends, and a
        WARNING every :data:`STALL_LOG_INTERVAL_S` while stalled — never a
        line per poll (Requirement 11.11)."""
        with self._lock:
            previous, self._model_wait = self._model_wait, wait
        if wait is not None:
            if previous is None:
                self._count("modelUnavailable")
                self._stall_logged_at = None
                logger.warning(
                    "Continuous workflow %s: model %s (%s) is %s; waiting for it%s",
                    self.registration_id, wait.model, wait.triton_model, wait.state,
                    ": {0}".format(wait.reason) if wait.reason else "")
            elif wait.stalled and (self._stall_logged_at is None
                                   or now - self._stall_logged_at >= STALL_LOG_INTERVAL_S):
                self._stall_logged_at = now
                logger.warning(
                    "Continuous workflow %s: model %s (%s) is still %s after %d s; no run has "
                    "started. Restart the backend once the model components are running%s",
                    self.registration_id, wait.model, wait.triton_model, wait.state,
                    int(STALL_AFTER_S), ": {0}".format(wait.reason) if wait.reason else "")
        elif previous is not None:
            logger.info("Continuous workflow %s: model %s (%s) is READY; resuming",
                        self.registration_id, previous.model, previous.triton_model)

    def _run(self, frame) -> None:
        self._last_seq = max(self._last_seq, int(frame.seq))
        context = {"source": CONTINUOUS_SOURCE, "frameSeq": int(frame.seq),
                   "frameAcquiredAtMs": int(frame.acquired_at_ms), "tickAtMs": int(self._wall() * 1000)}
        try:
            execution_id = self._store.insert(self.registration_id, context)
        except Exception:  # noqa: BLE001 - the next tick tries again
            logger.exception("Continuous workflow %s: could not record a run", self.registration_id)
            return
        self._handoff.put(execution_id, frame)
        with self._lock:
            self._counters["started"] += 1
            self._run_starts.append(self._clock())
            self._in_flight = execution_id
        try:
            with continuous_run(self.registration_id):
                self._execute(execution_id)
        except Exception:  # noqa: BLE001 - the executor contains its own failures
            logger.exception("Continuous workflow %s: run %s raised", self.registration_id, execution_id)
        finally:
            self._handoff.discard(execution_id)
            with self._lock:
                self._in_flight = None
        self._complete(execution_id)

    def _complete(self, execution_id: str) -> None:
        outcome = None
        if self._retention is not None:
            try:
                outcome = self._retention.on_run_complete(
                    execution_id, self.feed.keep_recent_runs, self.feed.keep_notable_runs,
                    output_ids=self.output_ids, stream_node_id=self.feed.node_id)
            except Exception:  # noqa: BLE001 - retention retries on the next run
                logger.exception("Continuous workflow %s: retention failed for run %s",
                                 self.registration_id, execution_id)
        status = getattr(outcome, "status", None)
        if outcome is None:
            try:
                status = self._store.status(execution_id)
            except Exception:  # noqa: BLE001
                status = None
        with self._lock:
            if status == _COMPLETED:
                self._counters["completed"] += 1
            else:
                self._counters["failed"] += 1
            if outcome is not None:
                self._counters["notable"] += 1 if outcome.notable else 0
                self._counters["outputsSent"] += int(outcome.outputs_sent or 0)
        if status != _COMPLETED:
            # A run that did not complete may have been a model that is not
            # loaded: ask the gate once more before the next tick.
            self._recheck_gate = True
        processed = getattr(outcome, "processed_seq", None)
        if isinstance(processed, int) and processed > self._last_seq:
            self._last_seq = processed

    # -- reporting -------------------------------------------------------------

    def counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def effective_fps(self, now: Optional[float] = None) -> float:
        now = self._clock() if now is None else now
        with self._lock:
            while self._run_starts and self._run_starts[0] <= now - RATE_WINDOW_S:
                self._run_starts.popleft()
            count = len(self._run_starts)
            elapsed = min(RATE_WINDOW_S, max(1.0, now - self._rate_since))
        return round(count / elapsed, 2)

    def status(self) -> Dict[str, Any]:
        """The Continuous status document (design "Continuous status")."""
        health = self._health()
        with self._lock:
            paused, paused_at_ms, in_flight = self._paused, self._paused_at_ms, self._in_flight
            counters = dict(self._counters)
            model_wait = self._model_wait
        # paused > waiting_for_stream > waiting_for_model > running.
        if paused:
            state = STATE_PAUSED
        elif health.get("state") != _STREAMING:
            state = STATE_WAITING
        elif model_wait is not None:
            state = STATE_WAITING_MODEL
        else:
            state = STATE_RUNNING
        return {
            "registrationId": self.registration_id,
            "state": state,
            "configuredFps": self.feed.frames_per_second,
            "effectiveFps": 0.0 if paused else self.effective_fps(),
            "counters": counters,
            "streamHealth": health or None,
            "pausedAtMs": paused_at_ms,
            "cameraSourceId": self.feed.camera_source_id,
            "runInProgress": in_flight is not None,
            "modelReadiness": model_wait.as_document() if model_wait is not None else None,
        }

    def _maybe_summarize(self, now: float) -> None:
        if now - self._last_summary < SUMMARY_INTERVAL_S:
            return
        with self._lock:
            counters = dict(self._counters)
            base, self._summary_base = self._summary_base, dict(counters)
            paused = self._paused
        self._last_summary = now
        if paused:
            return
        delta = {key: counters[key] - base.get(key, 0) for key in COUNTER_KEYS}
        logger.info(
            "Continuous workflow %s, last minute: %d runs (%d completed, %d failed), %d notable, "
            "%d outputs sent, %d ticks skipped busy, %d without a new frame; %.2f runs/s of %g",
            self.registration_id, delta["started"], delta["completed"], delta["failed"],
            delta["notable"], delta["outputsSent"], delta["skippedBusy"], delta["skippedNoNewFrame"],
            self.effective_fps(now), self.feed.frames_per_second)

    # -- the thread ------------------------------------------------------------

    def _loop(self) -> None:
        logger.info("Continuous workflow %s started: stream camera %s at %g runs/s%s",
                    self.registration_id, self.camera, self.feed.frames_per_second,
                    " (paused)" if self.paused else "")
        try:
            while True:
                self._wake.clear()
                try:
                    delay = self.step()
                except Exception:  # noqa: BLE001 - keep sampling
                    logger.exception("Continuous workflow %s: tick failed", self.registration_id)
                    delay = IDLE_POLL_S
                if delay is None:
                    break
                self._wake.wait(delay)
        finally:
            logger.info("Continuous workflow %s stopped", self.registration_id)
            if self._on_exit is not None:
                try:
                    self._on_exit(self)
                except Exception:  # noqa: BLE001
                    logger.exception("Continuous workflow %s: exit hook failed", self.registration_id)


def runner_fingerprint(feed: StreamFeed, output_ids: Sequence[str],
                       models: Sequence[str] = ()) -> Tuple:
    """What a runner's behavior depends on: a change restarts it.

    ``models`` is defaulted so a two-argument call still works.
    """
    return (feed.node_id, feed.camera_key, feed.frames_per_second, feed.max_frame_age_ms,
            feed.keep_recent_runs, feed.keep_notable_runs, tuple(output_ids), tuple(models))


def _default_stream_manager():
    from stream_ingest.manager import get_stream_ingest_manager
    return get_stream_ingest_manager()


def _default_execute_provider():
    from workflow_engine import executor
    return executor.get_executor()


class ContinuousRunnerManager:
    """The registrations listener owning the runners (see the module
    docstring)."""

    def __init__(self, session_factory: Optional[Callable] = None,
                 resolution_provider: Optional[Callable[[str], Any]] = None,
                 execute_provider: Callable[[], Optional[Callable[[str], None]]] = _default_execute_provider,
                 manager_provider: Callable[[], Any] = _default_stream_manager,
                 stream_camera_resolver: Callable = load_configured_stream_cameras,
                 retention: Any = None,
                 handoff: Any = FRAME_HANDOFF,
                 runner_factory: Callable[..., ContinuousRunner] = ContinuousRunner,
                 start_threads: bool = True,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 engine_started_at: Optional[float] = None):
        if session_factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal
            session_factory = SessionLocal
        self._session_factory = session_factory
        self._resolution_provider = resolution_provider
        self._execute_provider = execute_provider
        self._manager_provider = manager_provider
        self._stream_camera_resolver = stream_camera_resolver
        self._retention = retention
        self._handoff = handoff
        self._runner_factory = runner_factory
        self._start_threads = start_threads
        self._clock = clock
        self._wall = wall
        # The engine's start (runtime.py builds the manager at engine
        # start): the model gate treats the repository rewrite that follows
        # a LocalServer deployment as expected for a while after it.
        self._engine_started_at = wall() if engine_started_at is None else float(engine_started_at)
        self._store = ExecutionStore(session_factory)
        self._lock = threading.RLock()
        self._runners: Dict[str, ContinuousRunner] = {}
        self._warned_no_executor = False

    # -- queries and operator actions --------------------------------------------

    def runner(self, registration_id: str) -> Optional[ContinuousRunner]:
        with self._lock:
            return self._runners.get(registration_id)

    def status(self, registration_id: str) -> Optional[Dict[str, Any]]:
        runner = self.runner(registration_id)
        return runner.status() if runner is not None else None

    def pause(self, registration_id: str) -> Optional[Dict[str, Any]]:
        """Pause a continuous registration; the pause persists across
        restarts until :meth:`resume`. None when it has no runner."""
        runner = self.runner(registration_id)
        if runner is None:
            return None
        runner.pause()
        self._write_state(runner)
        return runner.status()

    def resume(self, registration_id: str) -> Optional[Dict[str, Any]]:
        runner = self.runner(registration_id)
        if runner is None:
            return None
        runner.resume()
        self._write_state(runner)
        return runner.status()

    def notable_execution_ids(self, registration_id: str) -> Optional[list]:
        """The registration's retained Notable_Runs, newest first; None when
        run retention is not running."""
        if self._retention is None:
            return None
        return self._retention.notable_ids(registration_id)

    def persist_counters(self) -> None:
        """Snapshot every runner's counters (retention housekeeping)."""
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            self._write_state(runner)

    def stop_all(self) -> None:
        with self._lock:
            runners, self._runners = list(self._runners.values()), {}
        for runner in runners:
            runner.stop()
            self._write_state(runner)

    # -- reconciliation ------------------------------------------------------------

    def on_registrations_changed(self) -> None:
        """Start, keep, restart or stop runners to match the registrations
        (watcher listener)."""
        with self._lock:
            desired, superseded = self._desired()
            for registration_id, runner in list(self._runners.items()):
                wanted = desired.get(registration_id)
                if wanted is None or runner_fingerprint(*wanted) != runner.fingerprint:
                    self._stop_runner_locked(registration_id)
            for registration_id, (feed, output_ids, models) in sorted(desired.items()):
                if registration_id not in self._runners:
                    self._start_runner_locked(registration_id, feed, output_ids, models)
        for registration_id in superseded:
            self._delete_state(registration_id)

    def _desired(self):
        """``({registration id: (feed, output ids, model names)}, superseded
        ids with a state row)``."""
        from workflow_engine.discovery import STATUS_REGISTERED, STATUS_SUPERSEDED
        from workflow_engine.models import WorkflowContinuousState, WorkflowRegistration
        from workflow_engine.run_retention import output_node_ids
        from workflow_engine.stream_leases import _read_document

        desired: Dict[str, Tuple[StreamFeed, Tuple[str, ...], Tuple[str, ...]]] = {}
        session = self._session_factory()
        try:
            rows = session.query(WorkflowRegistration).filter(
                WorkflowRegistration.status == STATUS_REGISTERED).all()
            cameras = None

            def configured_cameras():
                nonlocal cameras
                if cameras is None:
                    cameras = self._stream_camera_resolver(session)
                return cameras

            for row in rows:
                resolution = None
                if self._resolution_provider is not None:
                    try:
                        resolution = self._resolution_provider(row.id)
                    except Exception:  # noqa: BLE001 - plan from the on-disk document
                        resolution = None
                document = getattr(resolution, "document", None)
                if not isinstance(document, dict):
                    document = _read_document(row.artifact_path)
                if document is None:
                    continue
                try:
                    feeds = plan_stream_feeds(document, resolution, configured_cameras=configured_cameras)
                except StreamFeedError:
                    continue
                if feeds and feeds[0].continuous:
                    desired[row.id] = (feeds[0], output_node_ids(document),
                                       document_model_names(document))
            stated = {state.registration_id for state in session.query(WorkflowContinuousState).all()}
            superseded = []
            if stated:
                superseded = [row.id for row in session.query(WorkflowRegistration).filter(
                    WorkflowRegistration.id.in_(sorted(stated)),
                    WorkflowRegistration.status == STATUS_SUPERSEDED).all()]
        finally:
            session.close()
        return desired, superseded

    def _start_runner_locked(self, registration_id: str, feed: StreamFeed, output_ids,
                             models: Sequence[str] = ()) -> None:
        execute = self._execute_provider()
        if execute is None:
            if not self._warned_no_executor:
                self._warned_no_executor = True
                logger.error("No workflow executor is registered; continuous workflows cannot run")
            return
        state = self._read_state(registration_id)
        runner = self._runner_factory(
            registration_id, feed, execute=execute, stream_manager=self._manager_provider(),
            store=self._store, retention=self._retention, output_ids=output_ids, handoff=self._handoff,
            clock=self._clock, wall=self._wall,
            paused=bool(state and state.get("paused")),
            paused_at_ms=state.get("pausedAt") if state else None,
            counters=state.get("counters") if state else None,
            models=models, engine_started_at=self._engine_started_at,
            on_exit=self._update_state_on_exit)
        self._runners[registration_id] = runner
        if self._retention is not None and self._start_threads:
            self._retention.start()
        if self._start_threads:
            runner.start()

    def _stop_runner_locked(self, registration_id: str) -> None:
        runner = self._runners.pop(registration_id, None)
        if runner is None:
            return
        runner.stop()
        self._write_state(runner)

    # -- the state table -------------------------------------------------------------

    def _read_state(self, registration_id: str) -> Optional[Dict[str, Any]]:
        from workflow_engine.models import WorkflowContinuousState

        session = self._session_factory()
        try:
            row = session.get(WorkflowContinuousState, registration_id)
            if row is None:
                return None
            try:
                counters = json.loads(row.counters_json) if row.counters_json else None
            except ValueError:
                counters = None
            return {"paused": bool(row.paused), "pausedAt": row.paused_at, "counters": counters}
        finally:
            session.close()

    def _update_state_on_exit(self, runner: ContinuousRunner) -> None:
        """A stopped runner's final counters (its last run may have
        finished after the stop), without re-creating the row of a
        registration that was superseded meanwhile."""
        self._write_state(runner, create=False)

    def _write_state(self, runner: ContinuousRunner, create: bool = True) -> None:
        from workflow_engine.models import WorkflowContinuousState

        session = self._session_factory()
        try:
            row = session.get(WorkflowContinuousState, runner.registration_id)
            if row is None:
                if not create:
                    return
                row = WorkflowContinuousState(registration_id=runner.registration_id)
                session.add(row)
            row.paused = runner.paused
            row.paused_at = runner.paused_at_ms
            row.counters_json = json.dumps(runner.counters(), sort_keys=True)
            row.updated_at = int(self._wall())
            session.commit()
        except Exception:  # noqa: BLE001 - the next snapshot retries
            session.rollback()
            logger.exception("Could not save the continuous state of %s", runner.registration_id)
        finally:
            session.close()

    def _delete_state(self, registration_id: str) -> None:
        from workflow_engine.models import WorkflowContinuousState

        session = self._session_factory()
        try:
            row = session.get(WorkflowContinuousState, registration_id)
            if row is not None:
                session.delete(row)
                session.commit()
                logger.info("Removed the continuous state of superseded registration %s", registration_id)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Could not remove the continuous state of %s", registration_id)
        finally:
            session.close()
