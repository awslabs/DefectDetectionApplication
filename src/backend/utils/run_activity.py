#  Copyright 2026 Amazon Web Services, Inc.
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
"""Workflow runs in flight, for the Triton model-load self-heal
(triton-model-cleanup-wedge, design D7).

Every workflow run path registers a ticket when the run starts, before it
captures a frame or prepares a feed, and finishes it when the run ends. The
self-heal action reads the registrations to decide whether a backend restart
would cut off a run that the wedge does not block.

* **No run is ever held.** :func:`start_run` returns at once in every state.
  During a quiesce a new run is accepted and counts as in flight; only in
  RESTARTING, after the self-heal commit, is it refused.
* **States.** ``IDLE --begin_quiesce()--> QUIESCING --commit_restart() ==
  "committed"--> RESTARTING``; ``QUIESCING --end_quiesce(), or QUIESCE_MAX_S
  after begin_quiesce()--> IDLE``; ``QUIESCING or RESTARTING
  --abort_restart()--> IDLE``. One ``threading.Condition`` guards every
  registration and the state, so the commit's check of the tickets and the
  switch to RESTARTING form one critical section with every registration.
* **The current ticket** lives in a ``contextvars.ContextVar``: per thread
  for the executor, runner and DIO threads, per task for coroutines (the
  classic run handler and the capture tasks). A ``start_run`` in a context
  that already holds a live ticket returns a no-op ticket, so a nested
  registration counts once.

The module never raises into its callers: an internal error is logged and
the run proceeds as it did before this module existed. Every log line is
written after the condition's lock is released. Standard library only.
"""
import contextlib
import contextvars
import itertools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

STATE_IDLE = "IDLE"
STATE_QUIESCING = "QUIESCING"
STATE_RESTARTING = "RESTARTING"

#: A quiesce that has not reached its commit after this long reverts to IDLE
#: by itself, even when the watchdog thread died or hung mid-attempt.
QUIESCE_MAX_S = 120.0
#: A ticket outside its gate for this long, with a stalled model, counts as
#: blocked by the wedge (a gate that failed open, or a digital-input run).
RUN_BLOCKED_AFTER_S = 900.0
#: A continuous workflow that started an execution this recently is running.
CONTINUOUS_ACTIVE_S = 60.0

KIND_EXECUTION = "execution"
KIND_PIPELINE = "pipeline"
KIND_CAPTURE = "capture"
#: The ticket ``gate_wait`` registers when its context holds none.
KIND_GATE = "gate"

#: The refusal while RESTARTING when the committer gave no text.
DEFAULT_REFUSAL = (
    "The LocalServer backend is restarting to clear a wedged Triton model load; run the "
    "workflow again in a few minutes."
)

#: The models of an ``emltriton`` element in a GStreamer launch string.
_LAUNCH_MODEL = re.compile(r'emltriton\b[^!]*?\bmodel="?([^\s"!]+)')


def models_from_launch(pipeline_str: Any) -> Tuple[str, ...]:
    """The distinct ``emltriton`` models named in a launch string."""
    try:
        return tuple(dict.fromkeys(_LAUNCH_MODEL.findall(str(pipeline_str or ""))))
    except Exception:  # noqa: BLE001 - instrumentation never fails a run
        return ()


@dataclass(eq=False)
class RunTicket:
    """One registered run. ``refused`` is the refusal text in RESTARTING,
    else None. A no-op ticket (nested registration) is never counted."""

    kind: str
    run_id: str
    models: Tuple[str, ...] = ()
    workflow: Optional[str] = None
    continuous: bool = False
    on_interrupt: Optional[Callable[[str], Any]] = None
    refused: Optional[str] = None
    noop: bool = False
    gate_only: bool = False
    workflow_label: Optional[str] = None
    registered_at: float = 0.0
    outside_gate_since: float = 0.0
    gate_depth: int = 0
    seq: int = 0
    token: Any = field(default=None, repr=False)

    @property
    def label(self) -> Optional[str]:
        return self.workflow_label or self.workflow


@dataclass(frozen=True)
class GateTicket:
    """What :func:`gate_wait` yields: ``refused`` is the refusal text when
    the gate is entered in RESTARTING, else None."""

    refused: Optional[str] = None


@dataclass(frozen=True)
class TicketInfo:
    """A registered ticket as :func:`snapshot` and ``commit_restart``'s
    ``is_blocked`` see it."""

    kind: str
    run_id: str
    models: Tuple[str, ...]
    workflow: Optional[str]
    started_at: float
    in_gate: bool
    gate_only: bool
    outside_gate_since: Optional[float]
    #: Seconds outside the gate at the time of the snapshot (0 inside it).
    outside_gate_for_s: float
    has_callback: bool
    continuous: bool


@dataclass(frozen=True)
class ActivitySnapshot:
    tickets: Tuple[TicketInfo, ...]
    #: workflow -> monotonic time of its latest continuous execution start.
    continuous_starts: Dict[str, float]
    now: float
    state: str
    #: workflow -> its display label (``<workflow_id> v<version>``), once set.
    continuous_labels: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InterruptReport:
    """The outcome of :func:`interrupt_all`: the run ids whose callback
    returned, those whose callback raised, and how many calls the budget
    left out."""

    marked: List[str]
    failed: List[str]
    skipped: int


def is_blocked(info: TicketInfo, stalled_models: Sequence[str]) -> bool:
    """Whether the wedge blocks ``info``: in its gate, a gate-only ticket,
    or outside its gate for at least :data:`RUN_BLOCKED_AFTER_S` with one
    of its models stalled."""
    if info.gate_only or info.in_gate:
        return True
    if info.outside_gate_for_s >= RUN_BLOCKED_AFTER_S:
        stalled = set(stalled_models or ())
        return any(model in stalled for model in info.models)
    return False


def running_continuous(snapshot: ActivitySnapshot,
                       active_s: float = CONTINUOUS_ACTIVE_S) -> List[str]:
    """The continuous workflows (their labels once known) that started an
    execution in the last ``active_s`` seconds, sorted."""
    labels = snapshot.continuous_labels or {}
    return sorted(labels.get(workflow, workflow)
                  for workflow, started in snapshot.continuous_starts.items()
                  if snapshot.now - started < active_s)


_CURRENT: "contextvars.ContextVar[Optional[RunTicket]]" = contextvars.ContextVar(
    "dda_run_activity_ticket", default=None)


def _noop_ticket(kind: str, run_id: Any) -> RunTicket:
    return RunTicket(kind=str(kind), run_id=str(run_id), noop=True)


class RunActivity:
    """The registry. One instance serves the process (:func:`instance`);
    tests replace it with :func:`install`."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._state = STATE_IDLE
        self._quiesce_started: Optional[float] = None
        self._refusal = DEFAULT_REFUSAL
        self._tickets: Dict[int, RunTicket] = {}
        self._continuous_starts: Dict[str, float] = {}
        self._continuous_labels: Dict[str, str] = {}
        self._seq = itertools.count(1)

    # -- locking and logging ---------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[List[Tuple[int, str, tuple]]]:
        """The condition's lock with the expiry applied; the yielded list
        collects log lines, written after the lock is released."""
        pending: List[Tuple[int, str, tuple]] = []
        try:
            with self._cond:
                self._expire_locked(pending)
                yield pending
        finally:
            for level, message, args in pending:
                try:
                    logger.log(level, message, *args)
                except Exception:  # noqa: BLE001 - logging never fails a run
                    pass

    def _expire_locked(self, pending) -> None:
        if (self._state == STATE_QUIESCING and self._quiesce_started is not None
                and self._clock() - self._quiesce_started > QUIESCE_MAX_S):
            self._state = STATE_IDLE
            self._quiesce_started = None
            self._cond.notify_all()
            pending.append((logging.WARNING,
                            "The self-heal quiesce expired after %ds without a commit; continuous "
                            "workflows resume and workflow runs proceed normally",
                            (int(QUIESCE_MAX_S),)))

    @staticmethod
    def _refusal_line(pending, ticket_kind: str, run_id: str, refusal: str) -> None:
        what = "capture task" if ticket_kind == KIND_CAPTURE else "workflow run"
        pending.append((logging.INFO, "Refusing %s %s: %s", (what, run_id, refusal)))

    def _live(self, ticket: Optional[RunTicket]) -> bool:
        return (ticket is not None and not ticket.noop and ticket.refused is None
                and self._tickets.get(ticket.seq) is ticket)

    # -- registration (run paths; never raise, never wait) ---------------------

    def start_run(self, kind: str, run_id: Any, models: Sequence[str] = (),
                  workflow: Optional[str] = None, continuous: bool = False,
                  on_interrupt: Optional[Callable[[str], Any]] = None,
                  bind: bool = True) -> RunTicket:
        try:
            current = _CURRENT.get()
            with self._locked() as pending:
                if self._live(current):
                    return _noop_ticket(kind, run_id)
                if self._state == STATE_RESTARTING:
                    refusal = self._refusal
                    self._refusal_line(pending, str(kind), str(run_id), refusal)
                    return RunTicket(kind=str(kind), run_id=str(run_id),
                                     models=tuple(models or ()), workflow=workflow,
                                     continuous=bool(continuous), refused=refusal)
                now = self._clock()
                ticket = RunTicket(kind=str(kind), run_id=str(run_id),
                                   models=tuple(m for m in (models or ()) if m),
                                   workflow=workflow, continuous=bool(continuous),
                                   on_interrupt=on_interrupt, registered_at=now,
                                   outside_gate_since=now, seq=next(self._seq))
                self._tickets[ticket.seq] = ticket
                if continuous and workflow is not None:
                    self._continuous_starts[str(workflow)] = now
            if bind:
                ticket.token = _CURRENT.set(ticket)
            return ticket
        except Exception:  # noqa: BLE001 - the run proceeds as today
            logger.exception("Run activity: could not register %s %s", kind, run_id)
            return _noop_ticket(kind, run_id)

    @contextlib.contextmanager
    def bound(self, ticket: Optional[RunTicket]) -> Iterator[None]:
        token = None
        try:
            if ticket is not None and not ticket.noop and ticket.refused is None:
                token = _CURRENT.set(ticket)
        except Exception:  # noqa: BLE001
            logger.exception("Run activity: could not bind ticket")
            token = None
        try:
            yield
        finally:
            if token is not None:
                try:
                    _CURRENT.reset(token)
                except Exception:  # noqa: BLE001 - a foreign context; clear it instead
                    try:
                        _CURRENT.set(None)
                    except Exception:  # noqa: BLE001
                        pass

    def set_details(self, ticket: Optional[RunTicket], models: Optional[Sequence[str]] = None,
                    workflow_label: Optional[str] = None) -> None:
        try:
            if ticket is None or ticket.noop or ticket.refused is not None:
                return
            with self._locked():
                if models is not None:
                    ticket.models = tuple(m for m in models if m)
                if workflow_label is not None:
                    ticket.workflow_label = str(workflow_label)
                    if ticket.continuous and ticket.workflow is not None:
                        self._continuous_labels[str(ticket.workflow)] = ticket.workflow_label
        except Exception:  # noqa: BLE001
            logger.exception("Run activity: could not update ticket details")

    def finish_run(self, ticket: Optional[RunTicket]) -> None:
        try:
            if ticket is None or ticket.noop or ticket.refused is not None:
                return
            with self._locked():
                if self._tickets.get(ticket.seq) is ticket:
                    del self._tickets[ticket.seq]
                    self._cond.notify_all()
            token, ticket.token = ticket.token, None
            if token is not None:
                try:
                    _CURRENT.reset(token)
                except Exception:  # noqa: BLE001 - finished in another context
                    if _CURRENT.get() is ticket:
                        _CURRENT.set(None)
        except Exception:  # noqa: BLE001
            logger.exception("Run activity: could not finish ticket")

    @contextlib.contextmanager
    def gate_wait(self, model: str) -> Iterator[GateTicket]:
        entered: Optional[RunTicket] = None
        gate_only: Optional[RunTicket] = None
        refused: Optional[str] = None
        try:
            current = _CURRENT.get()
            with self._locked() as pending:
                live = self._live(current)
                if self._state == STATE_RESTARTING:
                    refused = self._refusal
                    run_id = current.run_id if live else str(model)
                    kind = current.kind if live else KIND_PIPELINE
                    self._refusal_line(pending, kind, run_id, refused)
                elif live:
                    current.gate_depth += 1
                    entered = current
                else:
                    now = self._clock()
                    gate_only = RunTicket(kind=KIND_GATE, run_id=str(model),
                                          models=(str(model),), gate_only=True,
                                          registered_at=now, outside_gate_since=now,
                                          gate_depth=1, seq=next(self._seq))
                    self._tickets[gate_only.seq] = gate_only
        except Exception:  # noqa: BLE001 - the gate proceeds unregistered
            logger.exception("Run activity: could not enter the gate of %s", model)
        try:
            yield GateTicket(refused=refused)
        finally:
            try:
                with self._locked():
                    if entered is not None:
                        entered.gate_depth = max(0, entered.gate_depth - 1)
                        if entered.gate_depth == 0:
                            entered.outside_gate_since = self._clock()
                    if gate_only is not None and self._tickets.get(gate_only.seq) is gate_only:
                        del self._tickets[gate_only.seq]
                    self._cond.notify_all()
            except Exception:  # noqa: BLE001
                logger.exception("Run activity: could not leave the gate of %s", model)

    def after_gate(self, ticket: Optional[RunTicket]) -> Optional[str]:
        try:
            with self._locked() as pending:
                if self._state != STATE_RESTARTING:
                    return None
                refusal = self._refusal
                kind = ticket.kind if ticket is not None else KIND_PIPELINE
                run_id = ticket.run_id if ticket is not None else "<unknown>"
                self._refusal_line(pending, kind, run_id, refusal)
                return refusal
        except Exception:  # noqa: BLE001
            logger.exception("Run activity: after_gate failed")
            return None

    # -- state (the self-heal action) -------------------------------------------

    def begin_quiesce(self) -> bool:
        with self._locked():
            if self._state != STATE_IDLE:
                return False
            self._state = STATE_QUIESCING
            self._quiesce_started = self._clock()
            return True

    def end_quiesce(self) -> None:
        with self._locked():
            if self._state == STATE_QUIESCING:
                self._state = STATE_IDLE
                self._quiesce_started = None
                self._cond.notify_all()

    def abort_restart(self) -> None:
        with self._locked():
            if self._state in (STATE_QUIESCING, STATE_RESTARTING):
                self._state = STATE_IDLE
                self._quiesce_started = None
                self._refusal = DEFAULT_REFUSAL
                self._cond.notify_all()

    def quiescing(self) -> bool:
        try:
            with self._locked():
                return self._state in (STATE_QUIESCING, STATE_RESTARTING)
        except Exception:  # noqa: BLE001 - continuous runners tick as today
            logger.exception("Run activity: quiescing() failed")
            return False

    def state(self) -> str:
        with self._locked():
            return self._state

    def commit_restart(self, is_blocked_fn: Callable[[TicketInfo], bool],
                       refusal: Optional[str] = None) -> str:
        """``"committed"`` (now RESTARTING), ``"runs"`` (an unblocked ticket
        is registered) or ``"not_quiescing"``. ``is_blocked_fn`` runs under
        the lock and must not call this module."""
        with self._locked():
            if self._state != STATE_QUIESCING:
                return "not_quiescing"
            now = self._clock()
            for ticket in self._tickets.values():
                if not is_blocked_fn(self._info(ticket, now)):
                    return "runs"
            self._state = STATE_RESTARTING
            self._quiesce_started = None
            self._refusal = refusal or DEFAULT_REFUSAL
            return "committed"

    def interrupt_all(self, error_text: str, budget_s: float) -> InterruptReport:
        """Call every registered ticket's ``on_interrupt(error_text)``,
        outside the lock, within ``budget_s`` seconds in total."""
        with self._locked():
            calls = [(t.run_id, t.on_interrupt) for t in sorted(self._tickets.values(),
                                                               key=lambda t: t.seq)
                     if t.on_interrupt is not None]
        marked: List[str] = []
        failed: List[str] = []
        stop = threading.Event()
        lock = threading.Lock()

        def _work():
            for run_id, callback in calls:
                if stop.is_set():
                    return
                try:
                    callback(error_text)
                except Exception as exc:  # noqa: BLE001 - that run stays as it is
                    with lock:
                        if not stop.is_set():
                            failed.append(run_id)
                    logger.warning(
                        "SELF-HEAL could not mark workflow run %s failed (%s); it may show as "
                        "pending or running after the restart", run_id, exc)
                    continue
                with lock:
                    if not stop.is_set():
                        marked.append(run_id)

        if calls:
            worker = threading.Thread(target=_work, name="self-heal-interrupt", daemon=True)
            worker.start()
            worker.join(max(0.0, float(budget_s)))
            with lock:
                stop.set()
                done_marked, done_failed = list(marked), list(failed)
        else:
            done_marked, done_failed = [], []
        skipped = len(calls) - len(done_marked) - len(done_failed)
        return InterruptReport(marked=done_marked, failed=done_failed, skipped=skipped)

    def _info(self, ticket: RunTicket, now: float) -> TicketInfo:
        in_gate = ticket.gate_depth > 0
        return TicketInfo(
            kind=ticket.kind, run_id=ticket.run_id, models=tuple(ticket.models),
            workflow=ticket.label, started_at=ticket.registered_at, in_gate=in_gate,
            gate_only=ticket.gate_only,
            outside_gate_since=None if in_gate else ticket.outside_gate_since,
            outside_gate_for_s=0.0 if in_gate else max(0.0, now - ticket.outside_gate_since),
            has_callback=ticket.on_interrupt is not None, continuous=ticket.continuous)

    def snapshot(self) -> ActivitySnapshot:
        with self._locked():
            now = self._clock()
            tickets = tuple(self._info(t, now)
                            for t in sorted(self._tickets.values(), key=lambda t: t.seq))
            return ActivitySnapshot(tickets=tickets, continuous_starts=dict(self._continuous_starts),
                                    now=now, state=self._state,
                                    continuous_labels=dict(self._continuous_labels))

    def current(self) -> Optional[RunTicket]:
        """The calling context's live ticket, or None."""
        ticket = _CURRENT.get()
        with self._locked():
            return ticket if self._live(ticket) else None


_instance = RunActivity()


def instance() -> RunActivity:
    return _instance


def install(activity: RunActivity) -> RunActivity:
    """Replace the process instance (tests); returns the previous one."""
    global _instance
    previous, _instance = _instance, activity
    return previous


# -- module-level API (delegates to the current instance) ---------------------

def start_run(kind: str, run_id: Any, models: Sequence[str] = (), workflow: Optional[str] = None,
              continuous: bool = False, on_interrupt: Optional[Callable[[str], Any]] = None,
              bind: bool = True) -> RunTicket:
    return _instance.start_run(kind, run_id, models=models, workflow=workflow,
                               continuous=continuous, on_interrupt=on_interrupt, bind=bind)


def bound(ticket: Optional[RunTicket]):
    return _instance.bound(ticket)


def set_details(ticket: Optional[RunTicket], models: Optional[Sequence[str]] = None,
                workflow_label: Optional[str] = None) -> None:
    _instance.set_details(ticket, models=models, workflow_label=workflow_label)


def finish_run(ticket: Optional[RunTicket]) -> None:
    _instance.finish_run(ticket)


def gate_wait(model: str):
    return _instance.gate_wait(model)


def after_gate(ticket: Optional[RunTicket]) -> Optional[str]:
    return _instance.after_gate(ticket)


def begin_quiesce() -> bool:
    return _instance.begin_quiesce()


def end_quiesce() -> None:
    _instance.end_quiesce()


def abort_restart() -> None:
    _instance.abort_restart()


def quiescing() -> bool:
    return _instance.quiescing()


def state() -> str:
    return _instance.state()


def commit_restart(is_blocked_fn: Callable[[TicketInfo], bool],
                   refusal: Optional[str] = None) -> str:
    return _instance.commit_restart(is_blocked_fn, refusal)


def interrupt_all(error_text: str, budget_s: float) -> InterruptReport:
    return _instance.interrupt_all(error_text, budget_s)


def snapshot() -> ActivitySnapshot:
    return _instance.snapshot()


def current() -> Optional[RunTicket]:
    return _instance.current()
