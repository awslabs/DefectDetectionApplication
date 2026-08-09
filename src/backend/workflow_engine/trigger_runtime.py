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

"""Trigger activation runtime core (trigger-activation-runtime, design C4).

The device-side activation machinery for trigger-driven workflows: the
Run_Activation record, the priority-then-FIFO Activation_Queue, per-trigger
enqueue-time Concurrency_Policy gates (``queue`` / ``drop`` / ``debounce``),
and the per-workflow Activation_Dispatcher that runs activations
sequentially through the same executor path the manual trigger endpoint
uses (Requirements 7.1–7.7, 6.8).

Alongside the activation core, this module hosts the
:class:`TriggerSubscriptionManager` lifecycle layer (Requirements 6.1,
6.2, 9.1, 12.4): it diffs the registered trigger-driven artifact sets the
``WorkflowWatcher`` maintains (``workflow_registrations`` rows plus each
set's ``compiled_pipeline.json``), parses their Trigger_Bindings
(``executorBindings`` entries whose binding is ``mqtt_subscribe`` /
``opcua_subscribe``), and starts/stops one :class:`WorkflowTriggerGroup`
per registration — one :class:`WorkflowActivationCore` plus one transport
worker per binding, built through the injected transport factories.
Transport code itself lives in later tasks and plugs into the seams
defined here:

- Tasks 7.x/8.x's transport workers (Greengrass IPC / paho MQTT /
  python-opcua) are produced by the injected
  ``mqtt_transport_factory`` / ``opcua_transport_factory`` (see the
  factory contract on :class:`TriggerSubscriptionManager`); they deliver
  firings through the ``on_delivery`` callback (routed to
  :meth:`WorkflowActivationCore.fire`, where the node's
  Concurrency_Policy gates enqueueing) and update their node's
  :class:`TriggerHealth` handle.
- The dispatch/run-start seam is the injectable ``run_starter`` callable
  (``run_starter(activation) -> None``, synchronous — it returns when the
  run reached a terminal state). The production default
  (:func:`default_run_starter`) inserts a ``WorkflowExecution`` row (status
  ``pending``, ``trigger_context_json`` set to the JSON-serialized
  Trigger_Context) and hands it to ``executor.dispatch`` exactly like
  ``api.trigger_workflow``, then blocks until the run leaves
  pending/running — giving the dispatcher its at-most-one-in-flight-run
  guarantee (Requirement 7.6). Tests substitute a stub ``run_starter`` and
  need no database or executor.

Everything heavier than the standard library is imported lazily inside
:func:`default_run_starter`, so this module is importable without the
``COMPONENT_WORK_PATH`` environment the DAO layer requires (same
discipline as ``watcher.py``).
"""

import heapq
import json
import logging
import os
import threading
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Concurrency_Policy values (per-trigger-node ``concurrency_policy``
# parameter; catalog defaults mirrored here so bindings compiled with
# defaults applied and hand-built policies behave identically).
POLICY_QUEUE = "queue"
POLICY_DROP = "drop"
POLICY_DEBOUNCE = "debounce"

DEFAULT_CONCURRENCY_POLICY = POLICY_QUEUE
DEFAULT_QUEUE_DEPTH = 10
DEFAULT_DEBOUNCE_MS = 500
DEFAULT_PRIORITY = 100
#: ``retry_limit`` catalog default — ``0`` is the documented "retry
#: forever" sentinel (Requirement 8.1).
DEFAULT_RETRY_LIMIT = 0

#: Reconnect backoff schedule (Requirement 8.1): initial delay 1 s,
#: doubling, capped at 60 s.
RECONNECT_INITIAL_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 60.0

#: Execution statuses (mirroring api.py / pipeline_executor.py — not
#: imported from there to keep this module import-light).
EXECUTION_STATUS_PENDING = "pending"
EXECUTION_STATUS_RUNNING = "running"

#: Statuses that mean "the run is still in flight" for the sequential
#: dispatcher's completion wait.
ACTIVE_EXECUTION_STATUSES = (EXECUTION_STATUS_PENDING, EXECUTION_STATUS_RUNNING)

#: How often the production run_starter re-reads the execution row while
#: waiting for the run to finish.
DEFAULT_RUN_POLL_INTERVAL_SECONDS = 0.5

#: How long the dispatcher loop blocks on an empty queue before re-checking
#: its stop event.
DEFAULT_DISPATCH_POLL_TIMEOUT_SECONDS = 0.25


# ---------------------------------------------------------------------------
# Run_Activation and the firing-sequence allocator
# ---------------------------------------------------------------------------


@dataclass
class RunActivation:
    """One request to start a workflow run, produced by a trigger firing.

    ``activation_group`` equals ``trigger_node_id`` in this feature — every
    trigger node forms its own implicit single-member group (OR semantics).
    The field is the reserved extension point for future AND/correlation
    semantics (Requirement 7.1). ``firing_seq`` is a monotonic counter
    assigned at firing time so equal priorities dispatch FIFO
    (Requirement 7.5). ``context`` is the Trigger_Context dict the transport
    built (Requirement 6.8).
    """

    trigger_node_id: str
    activation_group: str
    priority: int
    firing_seq: int
    context: Dict[str, Any] = field(default_factory=dict)


class FiringSequence:
    """Thread-safe monotonic counter for ``RunActivation.firing_seq``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0

    def next(self) -> int:
        with self._lock:
            value = self._next
            self._next += 1
            return value


# ---------------------------------------------------------------------------
# Activation_Queue
# ---------------------------------------------------------------------------


class ActivationQueue:
    """Pending Run_Activations ordered by (priority, firing_seq).

    Lower ``priority`` value dispatches first; ties are FIFO by
    ``firing_seq`` (Requirement 7.5). Thread-safe; ``pop`` blocks with a
    timeout so the dispatcher can watch its stop event. Per-trigger pending
    counts back the ``queue`` bound and ``drop`` pending check
    (Requirements 7.2, 7.3).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        # Heap entries are (priority, firing_seq, activation); the
        # (priority, firing_seq) prefix is unique, so activations are never
        # compared.
        self._heap: List = []
        self._pending_by_node: Dict[str, int] = {}

    def push(self, activation: RunActivation) -> None:
        with self._not_empty:
            heapq.heappush(
                self._heap,
                (activation.priority, activation.firing_seq, activation),
            )
            node = activation.trigger_node_id
            self._pending_by_node[node] = self._pending_by_node.get(node, 0) + 1
            self._not_empty.notify()

    def pop(self, timeout: Optional[float] = None) -> Optional[RunActivation]:
        """The pending activation with the lowest (priority, firing_seq),
        or None when the queue stays empty for ``timeout`` seconds."""
        with self._not_empty:
            if not self._heap and timeout:
                self._not_empty.wait(timeout)
            if not self._heap:
                return None
            return self._pop_locked()

    def pop_nowait(self) -> Optional[RunActivation]:
        with self._lock:
            if not self._heap:
                return None
            return self._pop_locked()

    def _pop_locked(self) -> RunActivation:
        _, _, activation = heapq.heappop(self._heap)
        node = activation.trigger_node_id
        remaining = self._pending_by_node.get(node, 1) - 1
        if remaining > 0:
            self._pending_by_node[node] = remaining
        else:
            self._pending_by_node.pop(node, None)
        return activation

    def pending_count(self, trigger_node_id: str) -> int:
        """Pending (not yet dispatched) activations for one trigger node."""
        with self._lock:
            return self._pending_by_node.get(trigger_node_id, 0)

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)


# ---------------------------------------------------------------------------
# Per-trigger Concurrency_Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerPolicy:
    """The policy parameter family of one trigger node, as compiled into
    its Trigger_Binding parameters (catalog defaults applied)."""

    trigger_node_id: str
    concurrency_policy: str = DEFAULT_CONCURRENCY_POLICY
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    priority: int = DEFAULT_PRIORITY
    #: Reconnect_Limit — bounds the reconnect engine's attempts
    #: (Requirement 8.1); ``0`` = retry forever.
    retry_limit: int = DEFAULT_RETRY_LIMIT

    @classmethod
    def from_parameters(
        cls, trigger_node_id: str, parameters: Optional[Dict[str, Any]]
    ) -> "TriggerPolicy":
        """Policy from a Trigger_Binding's ``parameters`` dict; missing or
        malformed values fall back to the catalog defaults."""
        parameters = parameters or {}
        return cls(
            trigger_node_id=trigger_node_id,
            concurrency_policy=str(
                parameters.get("concurrency_policy") or DEFAULT_CONCURRENCY_POLICY
            ),
            queue_depth=_coerce_int(
                parameters.get("queue_depth"), DEFAULT_QUEUE_DEPTH
            ),
            debounce_ms=_coerce_int(
                parameters.get("debounce_ms"), DEFAULT_DEBOUNCE_MS
            ),
            priority=_coerce_int(parameters.get("priority"), DEFAULT_PRIORITY),
            retry_limit=_coerce_int(
                parameters.get("retry_limit"), DEFAULT_RETRY_LIMIT
            ),
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class TriggerGate:
    """Enqueue-time Concurrency_Policy application for one trigger node
    (Requirements 7.2, 7.3, 7.4).

    Transport workers (tasks 7.x/8.x) call :meth:`fire` with the built
    Trigger_Context; the gate decides whether the firing becomes a pending
    Run_Activation:

    - ``queue``: append unless this trigger already has ``queue_depth``
      pending activations — then discard and log, naming the trigger node.
    - ``drop``: discard whenever this trigger has a pending or in-flight
      activation.
    - ``debounce``: trailing timer — every firing (re)arms a ``debounce_ms``
      timer and replaces the stored context; on expiry exactly one
      activation carrying the MOST RECENT context is enqueued.

    ``timer_factory`` (``(delay_seconds, callback) -> timer`` with
    ``start()``/``cancel()``) and ``clock`` are injectable so tests control
    time without sleeping.
    """

    def __init__(
        self,
        policy: TriggerPolicy,
        queue: ActivationQueue,
        sequence: FiringSequence,
        in_flight_probe: Callable[[str], bool],
        timer_factory: Callable = threading.Timer,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._queue = queue
        self._sequence = sequence
        self._in_flight_probe = in_flight_probe
        self._timer_factory = timer_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._debounce_timer = None
        self._debounce_context: Optional[Dict[str, Any]] = None
        self._closed = False

    def fire(self, context: Dict[str, Any]) -> bool:
        """Apply the node's Concurrency_Policy to one firing.

        Returns True when the firing was accepted (enqueued, or coalesced
        into the armed debounce window), False when it was discarded.
        """
        node_id = self.policy.trigger_node_id
        policy = self.policy.concurrency_policy

        if policy == POLICY_DEBOUNCE:
            return self._fire_debounce(context)

        if policy == POLICY_DROP:
            if self._queue.pending_count(node_id) > 0 or self._in_flight_probe(
                node_id
            ):
                logger.debug(
                    "Trigger '%s' fired while an activation is pending or in "
                    "flight (concurrency_policy=drop); discarding",
                    node_id,
                )
                return False
            self._enqueue(context)
            return True

        # ``queue`` (the default; unknown values degrade to queue semantics).
        if self._queue.pending_count(node_id) >= self.policy.queue_depth:
            logger.warning(
                "Trigger '%s' activation queue is full (queue_depth=%d); "
                "discarding firing",
                node_id,
                self.policy.queue_depth,
            )
            return False
        self._enqueue(context)
        return True

    def close(self) -> None:
        """Stop the gate: cancel any armed debounce timer and refuse
        further firings (called when the workflow's group stops)."""
        with self._lock:
            self._closed = True
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self._debounce_context = None

    # ------------------------------------------------------------------

    def _enqueue(self, context: Dict[str, Any]) -> None:
        node_id = self.policy.trigger_node_id
        self._queue.push(
            RunActivation(
                trigger_node_id=node_id,
                # One implicit single-member Activation_Group per trigger
                # node — the reserved AND/correlation extension point
                # (Requirement 7.1).
                activation_group=node_id,
                priority=self.policy.priority,
                firing_seq=self._sequence.next(),
                context=context,
            )
        )

    def _fire_debounce(self, context: Dict[str, Any]) -> bool:
        with self._lock:
            if self._closed:
                return False
            # Trailing debounce: keep the most recent context and re-arm the
            # timer, so the single activation fires debounce_ms after the
            # LAST firing of the burst (Requirement 7.4).
            self._debounce_context = context
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            timer = self._timer_factory(
                self.policy.debounce_ms / 1000.0, self._on_debounce_expiry
            )
            if hasattr(timer, "daemon"):
                timer.daemon = True
            self._debounce_timer = timer
            timer.start()
            return True

    def _on_debounce_expiry(self) -> None:
        with self._lock:
            self._debounce_timer = None
            context = self._debounce_context
            self._debounce_context = None
            if self._closed or context is None:
                return
        self._enqueue(context)


# ---------------------------------------------------------------------------
# Activation_Dispatcher
# ---------------------------------------------------------------------------


class ActivationDispatcher:
    """Per-workflow sequential dispatcher (Requirements 7.1, 7.6, 7.7).

    One daemon thread drains the Activation_Queue in (priority, firing_seq)
    order, running at most one activation at a time: ``run_starter`` is
    synchronous and returns when the run reached a terminal state, so the
    next activation cannot start while a run is in flight. A failing run
    (``run_starter`` raising, or the run itself failing — recorded on that
    execution row only) never blocks the next activation: the exception is
    contained and logged, and the loop continues.
    """

    def __init__(
        self,
        queue: ActivationQueue,
        run_starter: Callable[[RunActivation], None],
        name: str = "workflow",
        poll_timeout: float = DEFAULT_DISPATCH_POLL_TIMEOUT_SECONDS,
    ) -> None:
        self._queue = queue
        self._run_starter = run_starter
        self._name = name
        self._poll_timeout = poll_timeout
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._in_flight_lock = threading.Lock()
        self._in_flight_node: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"workflow-activation-dispatcher-{self._name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def is_in_flight(self, trigger_node_id: Optional[str] = None) -> bool:
        """Whether an activation is currently running — for a specific
        trigger node when given, or any at all otherwise. Backs the
        ``drop`` policy's in-flight check (Requirement 7.3)."""
        with self._in_flight_lock:
            if trigger_node_id is None:
                return self._in_flight_node is not None
            return self._in_flight_node == trigger_node_id

    def run_activation(self, activation: RunActivation) -> None:
        """Run one activation synchronously with failure containment
        (the loop body; public so tests can drive dispatch directly)."""
        with self._in_flight_lock:
            self._in_flight_node = activation.trigger_node_id
        try:
            self._run_starter(activation)
        except Exception:  # noqa: BLE001 - containment: never block the next
            # activation (Requirement 7.7); the failure belongs to that run
            # alone.
            logger.exception(
                "Triggered run for trigger '%s' (workflow %s) failed; "
                "continuing with the next activation",
                activation.trigger_node_id,
                self._name,
            )
        finally:
            with self._in_flight_lock:
                self._in_flight_node = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            activation = self._queue.pop(timeout=self._poll_timeout)
            if activation is None:
                continue
            self.run_activation(activation)


# ---------------------------------------------------------------------------
# Production run_starter (the api.trigger_workflow-mirroring default)
# ---------------------------------------------------------------------------


def serialize_trigger_context(context: Optional[Dict[str, Any]]) -> str:
    """The Trigger_Context as JSON for ``trigger_context_json``
    (Requirement 6.8). Non-JSON-native values (e.g. OPC UA variants)
    degrade to their string form rather than failing the run."""
    import json

    return json.dumps(context or {}, default=str)


def default_run_starter(
    registration_id: str,
    session_factory: Optional[Callable] = None,
    poll_interval: float = DEFAULT_RUN_POLL_INTERVAL_SECONDS,
) -> Callable[[RunActivation], None]:
    """The production ``run_starter`` for one workflow registration.

    Mirrors ``api.trigger_workflow``: inserts a ``WorkflowExecution`` row
    (status ``pending``, ``started_at`` now, ``trigger_context_json`` set)
    and hands the execution id to ``executor.dispatch`` — the exact same
    executor path the manual trigger endpoint uses (Requirement 7.6). It
    then blocks until the run leaves pending/running (or no executor is
    registered, in which case the run stays pending exactly as a manual
    trigger would), giving the dispatcher sequential, at-most-one-in-flight
    semantics.

    All engine/DAO imports are deferred so the module imports without the
    ``COMPONENT_WORK_PATH`` environment.
    """

    def run_starter(activation: RunActivation) -> None:
        from workflow_engine import executor
        from workflow_engine.models import WorkflowExecution
        from workflow_engine.watcher import new_execution_id

        factory = session_factory
        if factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            factory = SessionLocal

        session = factory()
        try:
            execution = WorkflowExecution(
                id=new_execution_id(),
                registration_id=registration_id,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
                trigger_context_json=serialize_trigger_context(
                    activation.context
                ),
            )
            session.add(execution)
            session.commit()
            execution_id = execution.id
        finally:
            session.close()

        if not executor.dispatch(execution_id):
            # No executor registered: the run stays pending, exactly like a
            # manual trigger today; there is nothing to wait on.
            return

        _wait_for_terminal_status(factory, execution_id, poll_interval)

    return run_starter


def _wait_for_terminal_status(
    session_factory: Callable, execution_id: str, poll_interval: float
) -> None:
    """Block until the execution row leaves pending/running (or vanishes).

    The run's own failure is recorded on its row by the executor (existing
    containment, Requirement 7.7) — this wait only sequences dispatch; it
    never re-raises."""
    from workflow_engine.models import WorkflowExecution

    while True:
        session = session_factory()
        try:
            execution = session.get(WorkflowExecution, execution_id)
            if execution is None or execution.status not in (
                ACTIVE_EXECUTION_STATUSES
            ):
                return
        finally:
            session.close()
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Per-workflow activation core (the seam task 6.2's manager composes)
# ---------------------------------------------------------------------------


class WorkflowActivationCore:
    """Queue + policy gates + sequential dispatcher for ONE workflow.

    This is the activation heart of design C4's ``WorkflowTriggerGroup``:
    task 6.2's ``TriggerSubscriptionManager`` builds one core per registered
    trigger-driven workflow (adding one gate per Trigger_Binding via
    :meth:`add_trigger`) and tasks 7.x/8.x's transport workers deliver
    firings through :meth:`fire`. ``run_starter``, ``clock``, and
    ``timer_factory`` are injectable with production defaults
    (Requirement 6.2's injection discipline), so tests exercise the full
    fire→policy→queue→dispatch path with no network, database, or executor.
    """

    def __init__(
        self,
        registration_id: str,
        policies: Iterable[TriggerPolicy] = (),
        run_starter: Optional[Callable[[RunActivation], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable = threading.Timer,
        dispatch_poll_timeout: float = DEFAULT_DISPATCH_POLL_TIMEOUT_SECONDS,
    ) -> None:
        self.registration_id = registration_id
        self.queue = ActivationQueue()
        self._sequence = FiringSequence()
        self._clock = clock
        self._timer_factory = timer_factory
        if run_starter is None:
            run_starter = default_run_starter(registration_id)
        self.dispatcher = ActivationDispatcher(
            self.queue,
            run_starter,
            name=registration_id,
            poll_timeout=dispatch_poll_timeout,
        )
        self._gates: Dict[str, TriggerGate] = {}
        for policy in policies:
            self.add_trigger(policy)

    def add_trigger(self, policy: TriggerPolicy) -> TriggerGate:
        """Register one trigger node's policy gate (idempotent per node)."""
        gate = TriggerGate(
            policy,
            self.queue,
            self._sequence,
            self.dispatcher.is_in_flight,
            timer_factory=self._timer_factory,
            clock=self._clock,
        )
        self._gates[policy.trigger_node_id] = gate
        return gate

    def gate(self, trigger_node_id: str) -> Optional[TriggerGate]:
        return self._gates.get(trigger_node_id)

    def fire(self, trigger_node_id: str, context: Dict[str, Any]) -> bool:
        """One trigger firing (OR semantics — any single trigger activates
        a run, Requirement 7.1). Returns True when the firing was accepted
        by the node's Concurrency_Policy."""
        gate = self._gates.get(trigger_node_id)
        if gate is None:
            logger.warning(
                "Firing for unknown trigger '%s' on workflow %s ignored",
                trigger_node_id,
                self.registration_id,
            )
            return False
        return gate.fire(context)

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self, timeout: float = 5.0) -> None:
        for gate in self._gates.values():
            gate.close()
        self.dispatcher.stop(timeout=timeout)


# ---------------------------------------------------------------------------
# Trigger_Bindings (compiled-document parsing)
# ---------------------------------------------------------------------------

BINDING_MQTT_SUBSCRIBE = "mqtt_subscribe"
BINDING_OPCUA_SUBSCRIBE = "opcua_subscribe"

#: Executor-binding kinds that are Trigger_Bindings — the device detects a
#: trigger-driven compiled document by the presence of one of these
#: (design D2; no new top-level compiled-document field exists).
TRIGGER_BINDING_KINDS = frozenset(
    {BINDING_MQTT_SUBSCRIBE, BINDING_OPCUA_SUBSCRIBE}
)

#: File name of the compiled document inside an artifact set (mirrors
#: ``discovery.COMPILED_PIPELINE_FILE``; kept local so this module stays
#: importable with the standard library alone).
COMPILED_PIPELINE_FILE = "compiled_pipeline.json"


@dataclass(frozen=True)
class TriggerBinding:
    """One parsed Trigger_Binding from a compiled document's
    ``executorBindings`` (compiler task 3.1 emits ``activates`` on the two
    trigger kinds only)."""

    node_id: str
    kind: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    activates: Tuple[str, ...] = ()


def parse_trigger_bindings(compiled_document: Optional[Dict[str, Any]]) -> List[TriggerBinding]:
    """The Trigger_Bindings of one compiled document, in emission order.

    Non-trigger binding kinds (outputs, inference, ``digital_input``, …)
    are ignored, so zero-trigger documents yield an empty list and the
    manager does nothing for them (Requirement 12.4).
    """
    bindings: List[TriggerBinding] = []
    if not isinstance(compiled_document, dict):
        return bindings
    entries = compiled_document.get("executorBindings")
    if not isinstance(entries, list):
        return bindings
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("binding")
        node_id = entry.get("nodeId")
        if kind not in TRIGGER_BINDING_KINDS or not node_id:
            continue
        parameters = entry.get("parameters")
        activates = entry.get("activates")
        bindings.append(
            TriggerBinding(
                node_id=str(node_id),
                kind=str(kind),
                parameters=dict(parameters) if isinstance(parameters, dict) else {},
                activates=tuple(
                    str(target) for target in activates
                ) if isinstance(activates, list) else (),
            )
        )
    return bindings


def bindings_fingerprint(bindings: Iterable[TriggerBinding]) -> str:
    """Stable identity of a registration's trigger surface; the manager
    restarts a group when its registration's fingerprint changes."""
    return json.dumps(
        [
            [b.node_id, b.kind, b.parameters, list(b.activates)]
            for b in bindings
        ],
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------------------
# Trigger_Health (Requirement 9.1)
# ---------------------------------------------------------------------------

HEALTH_CONNECTING = "connecting"
HEALTH_SUBSCRIBED = "subscribed"
HEALTH_POLLING = "polling"
HEALTH_RECONNECTING = "reconnecting"
HEALTH_FAILED = "failed"

MECHANISM_SUBSCRIBE = "subscribe"
MECHANISM_POLL = "poll"


class TriggerHealth:
    """The mutable Trigger_Health record of ONE trigger node — the small
    health-handle object the manager passes to the transport factory so
    workers report state without knowing about the registry
    (Requirement 9.1).

    Thread-safe: transport callbacks, the reconnect engine (task 7.3), and
    API reads touch it from different threads. ``mechanism`` applies to
    OPC UA nodes only and is omitted from the wire form otherwise.
    """

    def __init__(
        self,
        node_id: str,
        kind: str,
        mechanism: Optional[str] = None,
    ) -> None:
        self.node_id = node_id
        self.kind = kind
        self._lock = threading.Lock()
        self._state = HEALTH_CONNECTING
        self._mechanism = mechanism
        self._auto_fallback = False
        self._reconnect_attempts = 0
        self._last_error: Optional[str] = None

    # -- worker-facing updates ------------------------------------------

    def set_state(self, state: str, error: Optional[str] = None) -> None:
        """Record a state transition; a non-None ``error`` replaces the
        last error, and entering ``subscribed``/``polling`` clears it."""
        with self._lock:
            self._state = state
            if error is not None:
                self._last_error = error
            elif state in (HEALTH_SUBSCRIBED, HEALTH_POLLING):
                self._last_error = None

    def set_mechanism(self, mechanism: str, auto_fallback: bool = False) -> None:
        """The active OPC UA mechanism (``subscribe``/``poll``), flagged
        when entered by auto-fallback (Requirement 8.5's seam)."""
        with self._lock:
            self._mechanism = mechanism
            self._auto_fallback = auto_fallback

    def record_reconnect_attempt(self) -> int:
        """Increment and return the reconnect attempt count
        (task 7.3's backoff engine drives this)."""
        with self._lock:
            self._reconnect_attempts += 1
            return self._reconnect_attempts

    def reset_reconnect_attempts(self) -> None:
        with self._lock:
            self._reconnect_attempts = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def reconnect_attempts(self) -> int:
        with self._lock:
            return self._reconnect_attempts

    @property
    def mechanism(self) -> Optional[str]:
        """The active OPC UA mechanism (``subscribe``/``poll``), or None
        for MQTT nodes — the reconnect engine derives its default restored
        state from this (Requirement 8.2)."""
        with self._lock:
            return self._mechanism

    # -- API-facing wire form -------------------------------------------

    def to_wire(self) -> Dict[str, Any]:
        """The record in the additive ``triggerHealth`` API shape
        (design Data Models; task 9.1 surfaces it)."""
        with self._lock:
            record: Dict[str, Any] = {
                "nodeId": self.node_id,
                "state": self._state,
                "autoFallback": self._auto_fallback,
                "reconnectAttempts": self._reconnect_attempts,
                "lastError": self._last_error,
            }
            if self._mechanism is not None:
                record["mechanism"] = self._mechanism
            return record


# ---------------------------------------------------------------------------
# ReconnectEngine (Requirements 8.1, 8.2, 8.3, 9.1 — design C4/C5)
# ---------------------------------------------------------------------------


def describe_trigger_target(
    kind: str, parameters: Optional[Dict[str, Any]]
) -> str:
    """The human-readable target a trigger connects to — the ``topic``
    for MQTT triggers, the ``endpoint`` for OPC UA triggers — used in the
    reconnect engine's actionable failure message (Requirement 8.3)."""
    parameters = parameters or {}
    if kind == BINDING_OPCUA_SUBSCRIBE:
        return str(parameters.get("endpoint") or "")
    return str(parameters.get("topic") or "")


#: Sentinel a worker's ``reconnect`` callable may return to report a
#: PERMANENT failure (e.g. a Greengrass authorization denial,
#: Requirement 8.7): the engine parks immediately — no further attempts
#: and no health restoration or overwrite (the worker already set its own
#: ``failed`` health with the actionable diagnostic before returning it).
#: Distinct from ``False`` (a retryable failed attempt) and from any
#: success value.
RECONNECT_PARK = object()


class ReconnectEngine:
    """The shared reconnect/backoff engine every transport worker
    composes (Requirements 8.1–8.3; design C4's reconnect bullet).

    One engine per trigger node. The worker routes its transport-drop
    signal to :meth:`on_connection_lost`; the engine then drives, on its
    own (injectable) execution context:

    - Trigger_Health → ``reconnecting`` with the attempt count
      (Requirement 8.2).
    - Exponential backoff: initial delay 1 s, doubling, capped at 60 s
      (Requirement 8.1).
    - One call to the worker-supplied ``reconnect`` callable per attempt.
      Contract: return ``False`` to signal a failed attempt without
      raising; raise to signal a failed attempt (the raised error becomes
      the last error); return :data:`RECONNECT_PARK` to signal a
      PERMANENT failure (the engine parks without touching health — the
      worker has already set its own ``failed`` record, Requirement 8.7);
      any other return (including ``None``) counts as success.
    - Attempts are counted against ``retry_limit`` (``0`` = retry
      forever). On success the attempt count resets and health is restored
      to ``subscribed``/``polling`` per ``restored_state`` (Requirement
      8.2). On exhaustion (``retry_limit`` ≥ 1 reached) health turns
      ``failed`` with the actionable message
      ``"Trigger '<node_id>' failed after N reconnect attempts:
      <topic-or-endpoint> — <error>"`` and the engine parks — no further
      attempts until the workflow's group restarts (Requirement 8.3).

    ``restored_state`` is a state string, a zero-arg callable returning
    one (workers whose mechanism can change — OPC UA auto-fallback —
    supply a callable), or ``None`` to derive it from the health handle's
    mechanism (``poll`` → ``polling``, else ``subscribed``).

    Injection seams (all with production defaults):

    - ``reconnect`` may be supplied at construction or later via
      :meth:`bind` (the manager builds the engine before the factory
      returns the worker). An unbound engine keeps the pre-7.3 minimal
      behavior: mark ``reconnecting`` + record the error, no attempts.
    - ``clock`` (monotonic seconds) for elapsed-time logging.
    - ``waiter(delay_seconds) -> bool`` performs the backoff wait and
      returns True when the wait was cancelled; the default waits on the
      engine's internal stop event, so :meth:`stop` (reached through the
      worker's ``stop()``) wakes a parked 60 s backoff immediately.
    - ``spawn(target)`` runs the reconnect loop; the default starts a
      daemon thread, tests pass ``lambda fn: fn()`` for synchronous,
      deterministic runs.
    """

    def __init__(
        self,
        node_id: str,
        target: str,
        health: TriggerHealth,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        reconnect: Optional[Callable[[], Any]] = None,
        restored_state: Any = None,
        clock: Callable[[], float] = time.monotonic,
        waiter: Optional[Callable[[float], bool]] = None,
        spawn: Optional[Callable[[Callable[[], None]], Any]] = None,
    ) -> None:
        self.node_id = node_id
        self.target = target
        self._health = health
        self._retry_limit = max(0, _coerce_int(retry_limit, DEFAULT_RETRY_LIMIT))
        self._reconnect = reconnect
        self._restored_state = restored_state
        self._clock = clock
        self._cancel_event = threading.Event()
        # threading.Event.wait(timeout) returns True when set — exactly
        # the "cancelled?" answer the waiter contract asks for.
        self._waiter = waiter if waiter is not None else self._cancel_event.wait
        self._spawn = spawn if spawn is not None else self._default_spawn
        self._lock = threading.Lock()
        self._active = False
        self._parked = False
        self._stopped = False
        self._last_error: Any = None

    # -- worker-facing surface ------------------------------------------

    def bind(
        self,
        reconnect: Callable[[], Any],
        restored_state: Any = None,
    ) -> None:
        """Late-bind the worker's ``reconnect`` callable (and optionally
        its restored-state value/callable) — the manager calls this after
        the transport factory returns the worker."""
        with self._lock:
            self._reconnect = reconnect
            if restored_state is not None:
                self._restored_state = restored_state

    def on_connection_lost(self, error: BaseException) -> None:
        """The transport-drop entry point (the factory contract's
        ``on_connection_lost``). Idempotent while a reconnect loop is
        already running; a no-op once parked or stopped."""
        with self._lock:
            self._last_error = error
            if self._stopped or self._parked:
                return
            if self._reconnect is None:
                # Unbound engine: minimal marking only (the pre-engine
                # default behavior; production workers always bind).
                self._health.set_state(HEALTH_RECONNECTING, error=str(error))
                return
            if self._active:
                return
            self._active = True
        logger.warning(
            "Trigger '%s' lost its connection to %s: %s; reconnecting",
            self.node_id,
            self.target,
            error,
        )
        self._spawn(self._reconnect_loop)

    def stop(self) -> None:
        """Cancel the engine: wakes any in-progress backoff wait and
        refuses further reconnect attempts (reached through the worker's
        ``stop()`` / the group's shutdown)."""
        with self._lock:
            self._stopped = True
        self._cancel_event.set()

    @property
    def parked(self) -> bool:
        """True once ``retry_limit`` was exhausted — no further attempts
        until the workflow's subscriptions restart (Requirement 8.3)."""
        with self._lock:
            return self._parked

    @property
    def reconnecting(self) -> bool:
        with self._lock:
            return self._active

    # -- internals -------------------------------------------------------

    def _default_spawn(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(
            target=target,
            name=f"trigger-reconnect-{self.node_id}",
            daemon=True,
        )
        thread.start()

    def _restored_state_value(self) -> str:
        restored = self._restored_state
        if callable(restored):
            restored = restored()
        if restored in (HEALTH_SUBSCRIBED, HEALTH_POLLING):
            return restored
        # Derive from the health handle's mechanism: OPC UA nodes in poll
        # mode (explicit or auto-fallback) restore to ``polling``, all
        # others to ``subscribed`` (Requirement 8.2).
        if self._health.mechanism == MECHANISM_POLL:
            return HEALTH_POLLING
        return HEALTH_SUBSCRIBED

    def _reconnect_loop(self) -> None:
        started = self._clock()
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        try:
            while True:
                self._health.set_state(
                    HEALTH_RECONNECTING, error=str(self._last_error)
                )
                if self._waiter(delay):
                    # stop() cancelled the backoff wait.
                    return
                with self._lock:
                    if self._stopped:
                        return
                    reconnect = self._reconnect
                attempt = self._health.record_reconnect_attempt()
                try:
                    outcome = reconnect()
                    succeeded = outcome is not False
                except Exception as error:  # noqa: BLE001 - a failed
                    # attempt is data for the next one, never fatal here.
                    self._last_error = error
                    outcome = False
                    succeeded = False
                if outcome is RECONNECT_PARK:
                    # Permanent failure (e.g. an authorization denial,
                    # Requirement 8.7): the worker set its own ``failed``
                    # health with the actionable diagnostic; the engine
                    # parks without restoring or overwriting it, and the
                    # denial is not retried (it does not self-heal).
                    #
                    # Contract: the worker owns the FINAL health write on
                    # park — the engine never writes health after
                    # receiving RECONNECT_PARK. Because this loop's
                    # pre-attempt ``reconnecting`` write may have landed
                    # after the worker's ``failed`` record, workers MUST
                    # (re-)assert their permanent-failure health inside
                    # the attempt that returns RECONNECT_PARK
                    # (greengrass-denial-health-race).
                    with self._lock:
                        self._parked = True
                    logger.error(
                        "Trigger '%s' reconnect to %s parked: permanent "
                        "failure (no further attempts)",
                        self.node_id,
                        self.target,
                    )
                    return
                if succeeded:
                    self._health.reset_reconnect_attempts()
                    self._health.set_state(self._restored_state_value())
                    logger.info(
                        "Trigger '%s' reconnected to %s after %d attempt(s) "
                        "(%.1fs)",
                        self.node_id,
                        self.target,
                        attempt,
                        self._clock() - started,
                    )
                    return
                if self._retry_limit >= 1 and attempt >= self._retry_limit:
                    message = (
                        f"Trigger '{self.node_id}' failed after {attempt} "
                        f"reconnect attempts: {self.target} — "
                        f"{self._last_error}"
                    )
                    self._health.set_state(HEALTH_FAILED, error=message)
                    with self._lock:
                        self._parked = True
                    logger.error("%s", message)
                    return
                logger.warning(
                    "Trigger '%s' reconnect attempt %d to %s failed: %s",
                    self.node_id,
                    attempt,
                    self.target,
                    self._last_error,
                )
                delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
        finally:
            with self._lock:
                self._active = False


# ---------------------------------------------------------------------------
# WorkflowTriggerGroup (design C4: core + one worker per binding)
# ---------------------------------------------------------------------------


class WorkflowTriggerGroup:
    """Everything the manager runs for ONE trigger-driven registration:
    the :class:`WorkflowActivationCore` (queue + gates + dispatcher) plus
    one transport worker and one :class:`TriggerHealth` per
    Trigger_Binding. Stopping is clean and complete: workers stopped,
    gates closed, dispatcher joined (Requirement 6.1)."""

    def __init__(
        self,
        registration_id: str,
        fingerprint: str,
        core: WorkflowActivationCore,
    ) -> None:
        self.registration_id = registration_id
        self.fingerprint = fingerprint
        self.core = core
        #: (binding, worker-or-None, health) in binding order; ``None``
        #: workers are bindings whose transport factory was unavailable
        #: (their health record says so).
        self.members: List[Tuple[TriggerBinding, Any, TriggerHealth]] = []
        #: node_id → the node's :class:`ReconnectEngine`; stopped with the
        #: group so a parked backoff wait never delays shutdown.
        self._engines: Dict[str, ReconnectEngine] = {}

    def add_member(
        self,
        binding: TriggerBinding,
        worker: Any,
        health: TriggerHealth,
        engine: Optional[ReconnectEngine] = None,
    ) -> None:
        self.members.append((binding, worker, health))
        if engine is not None:
            self._engines[binding.node_id] = engine

    def engine(self, trigger_node_id: str) -> Optional[ReconnectEngine]:
        """The node's reconnect engine (test/introspection seam)."""
        return self._engines.get(trigger_node_id)

    def start(self) -> None:
        """Dispatcher first (so deliveries during worker startup are
        accepted), then every worker; a worker failing to start is
        contained on its own health record."""
        self.core.start()
        for binding, worker, health in self.members:
            if worker is None:
                continue
            try:
                worker.start()
            except Exception as error:  # noqa: BLE001 - containment (12.4)
                logger.exception(
                    "Trigger worker for node '%s' (workflow %s) failed to "
                    "start",
                    binding.node_id,
                    self.registration_id,
                )
                health.set_state(
                    HEALTH_FAILED,
                    error=(
                        f"Trigger '{binding.node_id}' worker failed to "
                        f"start: {error}"
                    ),
                )

    def stop(self, timeout: float = 5.0) -> None:
        # Engines first: cancel any in-progress backoff wait so a 60 s
        # delay never blocks shutdown, and refuse new reconnect loops.
        for engine in self._engines.values():
            engine.stop()
        for binding, worker, _health in self.members:
            if worker is None:
                continue
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - stop must always complete
                logger.exception(
                    "Trigger worker for node '%s' (workflow %s) failed to "
                    "stop cleanly",
                    binding.node_id,
                    self.registration_id,
                )
        # Gates closed (debounce timers cancelled) + dispatcher joined.
        self.core.stop(timeout=timeout)

    def health(self) -> List[Dict[str, Any]]:
        return [health.to_wire() for _b, _w, health in self.members]


# ---------------------------------------------------------------------------
# TriggerSubscriptionManager (Requirements 6.1, 6.2, 9.1, 12.4)
# ---------------------------------------------------------------------------


class TriggerSubscriptionManager:
    """The long-lived lifecycle layer owning all trigger subscriptions.

    ``on_registrations_changed`` (wired to the WorkflowWatcher's
    ``registrations_listeners`` hook) re-reads the registered artifact
    sets the watcher maintains — ``workflow_registrations`` rows with
    status ``registered`` plus each set's ``compiled_pipeline.json`` —
    and diffs them against the running groups: new trigger-driven
    registrations start a :class:`WorkflowTriggerGroup`, removed/invalid
    ones stop theirs, and a changed trigger surface (fingerprint)
    restarts the group. With zero trigger-driven workflows registered the
    manager starts no threads and opens no connections (Requirement 12.4).

    **Transport factory contract** (Requirement 6.2 — injectable with
    production defaults; tasks 7.x/8.x supply the production
    implementations, until then the defaults are ``None`` placeholders
    and affected triggers surface health ``failed``)::

        factory(binding_kind, parameters, on_delivery, on_connection_lost,
                health) -> worker

    - ``binding_kind``: ``"mqtt_subscribe"`` or ``"opcua_subscribe"``.
    - ``parameters``: the binding's effective parameters (catalog
      defaults applied by the compiler).
    - ``on_delivery(context: dict) -> bool``: the worker calls this with
      the built Trigger_Context on every message/data change; the manager
      routes it to :meth:`WorkflowActivationCore.fire` (the node's
      Concurrency_Policy applies). Returns True when the firing was
      accepted.
    - ``on_connection_lost(error: BaseException) -> None``: the worker
      calls this when its transport drops. The manager wires it to the
      node's :class:`ReconnectEngine` (the canonical reconnect/backoff
      path, Requirements 8.1–8.3): a worker exposing a callable
      ``reconnect`` attribute (``() -> bool``; False or a raise = failed
      attempt) is late-bound into its engine after construction — the
      engine then drives backoff, attempt counting against
      ``retry_limit``, and the health transitions. Workers may also
      expose ``restored_state`` (a state string or zero-arg callable) to
      control the post-reconnect health state; without it the engine
      derives ``polling``/``subscribed`` from the health mechanism. A
      worker without ``reconnect`` leaves the engine unbound, and
      connection loss only marks health ``reconnecting`` with the error.
    - ``health``: the node's :class:`TriggerHealth` handle — the worker
      reports ``connecting``/``subscribed``/``polling``/``failed``
      transitions and (OPC UA) the active mechanism through it.
    - The returned worker exposes ``start()`` and ``stop()``; ``start``
      must not block indefinitely, ``stop`` releases the transport.

    ``run_starter_factory(registration_id) -> run_starter`` builds the
    per-workflow dispatch seam (default: :func:`default_run_starter`,
    the ``api.trigger_workflow``-mirroring production path). ``clock``
    and ``timer_factory`` thread through to the policy gates so tests
    control time. All DAO imports are deferred: the module stays
    importable without ``COMPONENT_WORK_PATH``.
    """

    def __init__(
        self,
        session_factory: Optional[Callable] = None,
        mqtt_transport_factory: Optional[Callable] = None,
        opcua_transport_factory: Optional[Callable] = None,
        run_starter_factory: Optional[
            Callable[[str], Callable[[RunActivation], None]]
        ] = None,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable = threading.Timer,
    ) -> None:
        self._session_factory = session_factory
        self._mqtt_transport_factory = mqtt_transport_factory
        self._opcua_transport_factory = opcua_transport_factory
        if run_starter_factory is None:
            run_starter_factory = self._default_run_starter_factory
        self._run_starter_factory = run_starter_factory
        self._clock = clock
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._groups: Dict[str, WorkflowTriggerGroup] = {}
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_registrations_changed(self) -> None:
        """Diff the registered trigger-driven artifact sets against the
        running groups and start/stop accordingly (Requirement 6.1 — the
        watcher notification after every ``sync_once`` drives this, so a
        new registration's subscriptions start within one watch cycle)."""
        try:
            desired = self._load_trigger_bindings()
        except Exception:  # noqa: BLE001 - never take LocalServer down
            logger.exception(
                "Could not read workflow registrations for trigger "
                "subscription reconciliation; keeping current subscriptions"
            )
            return

        with self._lock:
            if self._stopped:
                return
            # Stop groups whose registration disappeared, turned invalid,
            # or changed its trigger surface.
            for registration_id in list(self._groups):
                bindings = desired.get(registration_id)
                if bindings is not None:
                    fingerprint = bindings_fingerprint(bindings)
                    if fingerprint == self._groups[registration_id].fingerprint:
                        continue
                self._stop_group_locked(registration_id)

            # Start groups for new (or restarted) trigger-driven
            # registrations.
            for registration_id, bindings in desired.items():
                if registration_id in self._groups:
                    continue
                try:
                    self._start_group_locked(registration_id, bindings)
                except Exception:  # noqa: BLE001 - containment (12.4)
                    logger.exception(
                        "Trigger group for registration %s failed to start",
                        registration_id,
                    )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop every group (workers stopped, gates closed, dispatchers
        joined) and refuse further reconciliation."""
        with self._lock:
            self._stopped = True
            for registration_id in list(self._groups):
                self._stop_group_locked(registration_id, timeout=timeout)

    # ------------------------------------------------------------------
    # Trigger_Health (Requirement 9.1)
    # ------------------------------------------------------------------

    def health(self, registration_id: str) -> List[Dict[str, Any]]:
        """The registration's Trigger_Health records in wire form —
        one per trigger node, in binding order; empty for unknown or
        trigger-less registrations (task 9.1's API source)."""
        with self._lock:
            group = self._groups.get(registration_id)
            if group is None:
                return []
            return group.health()

    def group(self, registration_id: str) -> Optional[WorkflowTriggerGroup]:
        """The running group for a registration (test/introspection seam)."""
        with self._lock:
            return self._groups.get(registration_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _default_run_starter_factory(
        self, registration_id: str
    ) -> Callable[[RunActivation], None]:
        return default_run_starter(
            registration_id, session_factory=self._session_factory
        )

    def _load_trigger_bindings(self) -> Dict[str, List[TriggerBinding]]:
        """The trigger-driven registrations the watcher currently
        maintains: ``registered`` rows whose compiled document carries at
        least one Trigger_Binding. DAO/model imports are deferred
        (COMPONENT_WORK_PATH discipline)."""
        from workflow_engine.models import WorkflowRegistration

        factory = self._session_factory
        if factory is None:
            from dao.sqlite_db.sqlite_db_operations import SessionLocal

            factory = SessionLocal

        desired: Dict[str, List[TriggerBinding]] = {}
        session = factory()
        try:
            rows = (
                session.query(WorkflowRegistration)
                .filter(WorkflowRegistration.status == "registered")
                .all()
            )
            registrations = [(row.id, row.artifact_path) for row in rows]
        finally:
            session.close()

        for registration_id, artifact_path in registrations:
            try:
                document = self._read_compiled_document(artifact_path)
            except Exception:  # noqa: BLE001 - one bad set never blocks the rest
                logger.exception(
                    "Could not read the compiled document for registration "
                    "%s; skipping its triggers this cycle",
                    registration_id,
                )
                continue
            bindings = parse_trigger_bindings(document)
            if bindings:
                desired[registration_id] = bindings
        return desired

    @staticmethod
    def _read_compiled_document(artifact_path: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(artifact_path or "", COMPILED_PIPELINE_FILE)
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        return document if isinstance(document, dict) else None

    def _start_group_locked(
        self, registration_id: str, bindings: List[TriggerBinding]
    ) -> None:
        core = WorkflowActivationCore(
            registration_id,
            policies=[
                TriggerPolicy.from_parameters(binding.node_id, binding.parameters)
                for binding in bindings
            ],
            run_starter=self._run_starter_factory(registration_id),
            clock=self._clock,
            timer_factory=self._timer_factory,
        )
        group = WorkflowTriggerGroup(
            registration_id, bindings_fingerprint(bindings), core
        )
        for binding in bindings:
            health = self._make_health(binding)
            worker, engine = self._make_worker(
                registration_id, core, binding, health
            )
            group.add_member(binding, worker, health, engine=engine)
        group.start()
        self._groups[registration_id] = group
        logger.info(
            "Trigger subscriptions started for registration %s (%d trigger "
            "node(s))",
            registration_id,
            len(bindings),
        )

    def _stop_group_locked(
        self, registration_id: str, timeout: float = 5.0
    ) -> None:
        group = self._groups.pop(registration_id, None)
        if group is None:
            return
        try:
            group.stop(timeout=timeout)
        except Exception:  # noqa: BLE001 - stop must always complete
            logger.exception(
                "Trigger group for registration %s failed to stop cleanly",
                registration_id,
            )
        logger.info(
            "Trigger subscriptions stopped for registration %s",
            registration_id,
        )

    @staticmethod
    def _make_health(binding: TriggerBinding) -> TriggerHealth:
        mechanism = None
        if binding.kind == BINDING_OPCUA_SUBSCRIBE:
            mode = str(binding.parameters.get("mode") or MECHANISM_SUBSCRIBE)
            mechanism = (
                MECHANISM_POLL if mode == MECHANISM_POLL else MECHANISM_SUBSCRIBE
            )
        return TriggerHealth(binding.node_id, binding.kind, mechanism=mechanism)

    def _make_worker(
        self,
        registration_id: str,
        core: WorkflowActivationCore,
        binding: TriggerBinding,
        health: TriggerHealth,
    ) -> Tuple[Any, Optional[ReconnectEngine]]:
        """One transport worker plus its :class:`ReconnectEngine` through
        the injected factory (contract in the class docstring); a missing
        or failing factory is contained on the node's health record, never
        on the group.

        The engine is the canonical ``on_connection_lost`` path: it is
        built before the factory runs (so its bound method IS the callback
        the worker receives) and late-bound to the worker's ``reconnect``
        callable afterwards. A worker without a callable ``reconnect``
        attribute leaves the engine unbound — connection loss then only
        marks health ``reconnecting`` with the error (the pre-engine
        minimal behavior)."""
        factory = (
            self._mqtt_transport_factory
            if binding.kind == BINDING_MQTT_SUBSCRIBE
            else self._opcua_transport_factory
        )
        if factory is None:
            health.set_state(
                HEALTH_FAILED,
                error=(
                    f"Trigger '{binding.node_id}': no {binding.kind} "
                    f"transport is available in this build"
                ),
            )
            logger.warning(
                "No %s transport factory is wired; trigger '%s' of "
                "registration %s cannot subscribe",
                binding.kind,
                binding.node_id,
                registration_id,
            )
            return None, None

        node_id = binding.node_id
        engine = ReconnectEngine(
            node_id=node_id,
            target=describe_trigger_target(binding.kind, binding.parameters),
            health=health,
            retry_limit=TriggerPolicy.from_parameters(
                node_id, binding.parameters
            ).retry_limit,
            clock=self._clock,
        )

        def on_delivery(context: Dict[str, Any]) -> bool:
            return core.fire(node_id, context)

        try:
            worker = factory(
                binding.kind,
                binding.parameters,
                on_delivery,
                engine.on_connection_lost,
                health,
            )
        except Exception as error:  # noqa: BLE001 - containment (12.4)
            logger.exception(
                "Transport factory for trigger '%s' (registration %s) failed",
                node_id,
                registration_id,
            )
            health.set_state(
                HEALTH_FAILED,
                error=(
                    f"Trigger '{node_id}' transport could not be created: "
                    f"{error}"
                ),
            )
            return None, None

        reconnect = getattr(worker, "reconnect", None)
        if callable(reconnect):
            engine.bind(
                reconnect,
                restored_state=getattr(worker, "restored_state", None),
            )
        return worker, engine


# ---------------------------------------------------------------------------
# MQTT transports (design C5) — Greengrass IPC SubscribeToIoTCore
# ---------------------------------------------------------------------------

#: Greengrass IPC SubscribeToIoTCore supports only QoS 0 and 1; a
#: configured qos is clamped to this maximum, exactly as the publish
#: path clamps (mirrors ``output_bindings.GREENGRASS_MAX_QOS``; kept as
#: a local constant so this module needs only the standard library at
#: import time).
GREENGRASS_MAX_QOS = 1

#: How long a SubscribeToIoTCore activation waits for the nucleus
#: response before failing the attempt (mirrors the publish path's
#: ``get_response().result(timeout=10.0)``).
GREENGRASS_SUBSCRIBE_TIMEOUT_SECONDS = 10.0


def greengrass_subscribe_denial_message(topic: str) -> str:
    """The actionable Trigger_Health error for a Greengrass nucleus
    subscribe denial (Requirement 8.7), mirroring
    ``output_bindings._default_greengrass_publisher``'s publish-denial
    text but for ``aws.greengrass#SubscribeToIoTCore``."""
    return (
        "Greengrass IPC denied SubscribeToIoTCore for topic "
        "'{0}': the LocalServer component's "
        "aws.greengrass.ipc.mqttproxy accessControl configuration "
        "does not authorize subscribing to this topic. Add (or fix) a "
        "policy entry authorizing 'aws.greengrass#SubscribeToIoTCore' "
        "on a resource covering the topic in the component recipe "
        "(recipe-arm64-jp6.yaml / recipe-arm64-jp5.yaml / "
        "recipe-arm64.yaml / recipe-amd64.yaml, "
        "ComponentConfiguration accessControl) — or redeploy the "
        "workflow through the portal so the deployment merges its "
        "subscribe policy — and redeploy.".format(topic)
    )


class GreengrassIpcSubscriber:
    """MQTT subscribe transport over the Greengrass IPC
    ``SubscribeToIoTCore`` operation (Requirements 6.3, 6.8, 8.7 —
    design C5, task 7.1).

    Fits the manager's transport-factory worker contract: ``start()`` /
    ``stop()`` plus a callable ``reconnect`` attribute the manager
    late-binds into the node's :class:`ReconnectEngine`. The
    ``awsiot.greengrasscoreipc`` client is imported lazily inside
    :meth:`_subscribe`, so this module stays importable without the SDK
    (matching ``output_bindings``' paho/opcua discipline).

    - ``start()``: health ``connecting`` → IPC connect →
      ``SubscribeToIoTCoreRequest`` (topic filter, qos clamped to
      :data:`GREENGRASS_MAX_QOS` exactly like the publish path) activated
      with a stream handler → health ``subscribed``.
    - ``on_stream_event`` → Trigger_Context ``{topic, payload, qos,
      timestamp}`` → ``on_delivery`` (Requirement 6.8).
    - ``on_stream_error`` / ``on_stream_closed`` → ``on_connection_lost``
      (the node's reconnect engine). Per the SDK's
      ``StreamResponseHandler`` contract, ``on_stream_error`` returns
      True so the stream closes and the reconnect path owns recovery.
    - ``UnauthorizedError`` at subscribe time (start or reconnect):
      health ``failed`` with :func:`greengrass_subscribe_denial_message`,
      the worker remembers the denial, and NO reconnect is attempted —
      inside a reconnect the worker returns :data:`RECONNECT_PARK` so
      the engine parks without counting the denial against
      ``retry_limit`` or overwriting the denial health (Requirement 8.7:
      authorization does not self-heal by retrying).

    ``ipc_connect`` is an injection seam (``() -> ipc client``) so tests
    substitute a stub without the SDK or a nucleus socket.
    """

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]],
        on_delivery: Callable[[Dict[str, Any]], Any],
        on_connection_lost: Callable[[BaseException], None],
        health: TriggerHealth,
        ipc_connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        parameters = parameters or {}
        self.topic = str(parameters.get("topic") or "")
        self.configured_qos = _coerce_int(parameters.get("qos"), 0)
        self._on_delivery = on_delivery
        self._on_connection_lost = on_connection_lost
        self._health = health
        self._ipc_connect = ipc_connect
        self._lock = threading.Lock()
        self._ipc_client: Any = None
        self._operation: Any = None
        #: Bumped on every (re)subscribe and teardown so a stale stream
        #: handler (from a torn-down operation) can never route a
        #: spurious loss signal into the reconnect engine.
        self._generation = 0
        self._denied = False
        self._stopping = False

    @property
    def effective_qos(self) -> int:
        """The qos actually subscribed: the configured value clamped to
        the Greengrass maximum of 1, mirroring the publish path's clamp
        (Requirement 6.3)."""
        return min(self.configured_qos, GREENGRASS_MAX_QOS)

    # -- worker contract --------------------------------------------------

    def start(self) -> None:
        """Establish the subscription. A denial parks the worker on its
        ``failed`` denial health without raising (Requirement 8.7: no
        reconnect, not counted against ``retry_limit``); any other
        failure propagates to the group's start containment."""
        self._health.set_state(HEALTH_CONNECTING)
        self._subscribe()

    def stop(self) -> None:
        """Close the operation and IPC client quietly."""
        with self._lock:
            self._stopping = True
        self._teardown_transport()

    def reconnect(self) -> Any:
        """One reconnect attempt (the engine's late-bound callable):
        tear the old operation/client down and re-run the subscribe
        path. True = restored; a raise = failed attempt (the engine
        retries with backoff); :data:`RECONNECT_PARK` = the nucleus
        denied the subscription — the engine parks and the denial
        health set here stands (Requirement 8.7)."""
        with self._lock:
            denied = self._denied
        if denied:
            # A reconnect loop that slipped in before the denial was
            # marked writes ``reconnecting`` pre-attempt, and the engine
            # parks on RECONNECT_PARK without touching health — so the
            # ``failed`` denial record must be re-established here to be
            # the settled state under every interleaving
            # (greengrass-denial-health-race). Health is written outside
            # the worker lock (matching _mark_denied's discipline —
            # TriggerHealth has its own lock).
            self._health.set_state(
                HEALTH_FAILED,
                error=greengrass_subscribe_denial_message(self.topic),
            )
            return RECONNECT_PARK
        self._teardown_transport()
        if not self._subscribe():
            # _subscribe returns False only on a denial (anything else
            # raises): health is already ``failed`` with the actionable
            # diagnostic; park the engine.
            return RECONNECT_PARK
        return True

    # -- internals ---------------------------------------------------------

    def _subscribe(self) -> bool:
        """Connect the IPC client and activate the subscription.

        True on success (health ``subscribed``); False on an
        ``UnauthorizedError`` denial (health ``failed`` with the
        accessControl diagnostic, worker marked denied); any other
        failure raises after quiet cleanup."""
        import awsiot.greengrasscoreipc
        import awsiot.greengrasscoreipc.client as ipc_client_module
        import awsiot.greengrasscoreipc.model as model

        with self._lock:
            self._generation += 1
            generation = self._generation

        # Clamp exactly as _default_greengrass_publisher does
        # (Requirement 6.3): >= 1 → AT_LEAST_ONCE, else AT_MOST_ONCE.
        qos_value = (
            model.QOS.AT_LEAST_ONCE
            if self.configured_qos >= GREENGRASS_MAX_QOS
            else model.QOS.AT_MOST_ONCE
        )
        request = model.SubscribeToIoTCoreRequest()
        request.topic_name = self.topic
        request.qos = qos_value

        connect = self._ipc_connect or awsiot.greengrasscoreipc.connect
        ipc_client = connect()
        operation = ipc_client.new_subscribe_to_iot_core(
            self._make_stream_handler(ipc_client_module, generation)
        )
        try:
            operation.activate(request)
            operation.get_response().result(
                timeout=GREENGRASS_SUBSCRIBE_TIMEOUT_SECONDS
            )
        except model.UnauthorizedError:
            # Mark the denial (and retire this subscribe generation)
            # BEFORE closing the denied operation: the close can fire
            # on_stream_closed/on_stream_error on the CURRENT-generation
            # handler, and _handle_stream_lost must already be armed to
            # suppress it — via ``_denied`` and the now-stale generation —
            # so a permanent authorization denial can never route a
            # stream-lost signal into the reconnect engine
            # (greengrass-denial-health-race).
            with self._lock:
                self._generation += 1
            self._mark_denied()
            self._close_quietly(operation, ipc_client)
            return False
        except Exception:
            self._close_quietly(operation, ipc_client)
            raise

        with self._lock:
            self._ipc_client = ipc_client
            self._operation = operation
        self._health.set_state(HEALTH_SUBSCRIBED)
        logger.info(
            "Trigger '%s' subscribed to IoT Core topic filter '%s' (qos %d)",
            self._health.node_id,
            self.topic,
            self.effective_qos,
        )
        return True

    def _make_stream_handler(
        self, ipc_client_module: Any, generation: int
    ) -> Any:
        """A ``SubscribeToIoTCoreStreamHandler`` bound to this worker and
        one subscribe generation (stale generations are ignored)."""
        worker = self

        class _StreamHandler(
            ipc_client_module.SubscribeToIoTCoreStreamHandler
        ):
            def on_stream_event(self, event: Any) -> None:
                worker._handle_stream_event(event)

            def on_stream_error(self, error: Exception) -> bool:
                worker._handle_stream_lost(error, generation)
                # SDK StreamResponseHandler contract: return True to
                # close the stream (False keeps it open). The reconnect
                # engine owns recovery, so the broken stream closes.
                return True

            def on_stream_closed(self) -> None:
                worker._handle_stream_lost(
                    RuntimeError(
                        "SubscribeToIoTCore stream closed for topic "
                        f"'{worker.topic}'"
                    ),
                    generation,
                )

        return _StreamHandler()

    def _handle_stream_event(self, event: Any) -> None:
        """Build the MQTT Trigger_Context ``{topic, payload, qos,
        timestamp}`` from one ``IoTCoreMessage`` and deliver it
        (Requirement 6.8); a bad event is logged, never raised into the
        SDK's event loop."""
        try:
            message = getattr(event, "message", None)
            topic = getattr(message, "topic_name", None) or self.topic
            raw = getattr(message, "payload", None)
            if raw is None:
                raw = b""
            if isinstance(raw, (bytes, bytearray)):
                payload = bytes(raw).decode("utf-8", errors="replace")
            else:
                payload = str(raw)
            self._on_delivery(
                {
                    "topic": str(topic),
                    "payload": payload,
                    "qos": self.effective_qos,
                    "timestamp": time.time(),
                }
            )
        except Exception:  # noqa: BLE001 - containment: a delivery
            # failure never propagates into the IPC event loop.
            logger.exception(
                "Trigger '%s' failed to deliver a message from topic "
                "filter '%s'",
                self._health.node_id,
                self.topic,
            )

    def _handle_stream_lost(
        self, error: BaseException, generation: int
    ) -> None:
        """Route a stream error/close into the reconnect path — unless
        the worker is stopping, parked on a denial, or the signal comes
        from a stale (torn-down) subscribe generation."""
        with self._lock:
            if (
                self._stopping
                or self._denied
                or generation != self._generation
            ):
                return
        self._on_connection_lost(error)

    def _mark_denied(self) -> None:
        with self._lock:
            self._denied = True
        message = greengrass_subscribe_denial_message(self.topic)
        self._health.set_state(HEALTH_FAILED, error=message)
        logger.error("%s", message)

    def _teardown_transport(self) -> None:
        """Release the current operation/IPC client quietly, invalidating
        their stream handlers first (generation bump) so the deliberate
        close never looks like a connection loss."""
        with self._lock:
            self._generation += 1
            operation = self._operation
            ipc_client = self._ipc_client
            self._operation = None
            self._ipc_client = None
        self._close_quietly(operation, ipc_client)

    @staticmethod
    def _close_quietly(*closables: Any) -> None:
        for closable in closables:
            close = getattr(closable, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:  # noqa: BLE001 - teardown must always finish
                logger.debug(
                    "Quiet close of %r failed", closable, exc_info=True
                )


# ---------------------------------------------------------------------------
# MQTT transports (design C5) — paho-mqtt AWS IoT mutual TLS / plain broker
# ---------------------------------------------------------------------------

#: Connection constants mirroring the publish path
#: (``output_bindings.DEFAULT_MQTT_PORT`` / ``AWS_IOT_TLS_PORT`` /
#: ``AWS_IOT_MAX_QOS`` / ``AWS_IOT_REQUIRED_PARAMETERS``); kept as local
#: constants so this module needs only the standard library at import
#: time.
DEFAULT_MQTT_PORT = 1883
#: When ``aws_iot`` is enabled and ``broker_port`` was left at the
#: plain-MQTT default, the standard mutual-TLS port is used instead —
#: exactly as ``_run_mqtt_publish`` switches for publishing.
AWS_IOT_TLS_PORT = 8883
#: AWS IoT Core does not support MQTT QoS 2; the configured qos is
#: clamped to this maximum on the aws_iot path, mirroring the publish
#: clamp.
AWS_IOT_MAX_QOS = 1
#: Parameters required for AWS IoT Core mutual TLS (same set the publish
#: path requires): the thing name (MQTT client id) and the device-local
#: certificate file paths.
AWS_IOT_REQUIRED_PARAMETERS = (
    "iot_thing_name",
    "iot_ca_cert_path",
    "iot_client_cert_path",
    "iot_private_key_path",
)


class _PahoMqttSubscriber:
    """Shared long-lived paho-mqtt subscribe worker (Requirements 6.4,
    6.5, 6.8 — design C5, task 7.2). Concrete transports are
    :class:`AwsIotTlsSubscriber` (mutual TLS to AWS IoT Core) and
    :class:`PlainBrokerSubscriber` (plain broker); they differ only in
    client configuration and connect target.

    Fits the manager's transport-factory worker contract: ``start()`` /
    ``stop()`` plus a callable ``reconnect`` attribute the manager
    late-binds into the node's :class:`ReconnectEngine`. ``paho.mqtt``
    is imported lazily inside :meth:`_connect`, so this module stays
    importable without paho installed (matching ``output_bindings``'
    discipline).

    Connection lifecycle:

    - ``start()``: health ``connecting`` → build one long-lived
      ``paho.mqtt.client.Client`` → ``connect(host, port)`` →
      ``loop_start()`` (paho's network thread).
    - ``on_connect`` issues ``subscribe(topic, qos)`` — running the
      subscribe inside ``on_connect`` means every (re)connection of the
      paho session re-subscribes. Health turns ``subscribed`` once the
      subscribe call is issued on a successful connect (pragmatic
      choice: paho accepted the SUBSCRIBE onto an established session;
      we do not wait for the SUBACK via ``on_subscribe`` — a broker
      that then drops the session routes through ``on_disconnect`` and
      the reconnect path anyway).
    - ``on_message`` → Trigger_Context ``{topic, payload, qos,
      timestamp}`` (topic from the message, payload UTF-8-decoded with
      replacement, qos = the effective subscribed qos) →
      ``on_delivery`` (Requirement 6.8).

    Reconnect ownership (no double-reconnect races): paho's
    ``loop_start`` thread would normally auto-reconnect after an
    unexpected disconnect, which would race the spec's
    ``retry_limit``/backoff semantics owned by the
    :class:`ReconnectEngine`. The ``on_disconnect`` handler therefore
    calls ``client.disconnect()`` on an unexpected loss — flipping paho
    into its deliberate-disconnect state so the network loop exits
    instead of retrying — and routes the loss to ``on_connection_lost``
    (the engine). The engine then drives backoff and calls
    :meth:`reconnect`, which tears the dead client down and rebuilds
    from scratch. A subscribe-generation guard (mirroring
    :class:`GreengrassIpcSubscriber`) makes callbacks from a torn-down
    client inert, so a stale loss signal can never double-trigger the
    engine.

    ``client_factory`` (``(client_id) -> client``) is an injection seam
    so tests substitute a stub client without paho installed.
    """

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]],
        on_delivery: Callable[[Dict[str, Any]], Any],
        on_connection_lost: Callable[[BaseException], None],
        health: TriggerHealth,
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.parameters = dict(parameters or {})
        self.topic = str(self.parameters.get("topic") or "")
        self.configured_qos = _coerce_int(self.parameters.get("qos"), 0)
        self._on_delivery = on_delivery
        self._on_connection_lost = on_connection_lost
        self._health = health
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._client: Any = None
        #: Bumped on every (re)connect and teardown so a callback from a
        #: torn-down client can never route a spurious loss signal into
        #: the reconnect engine (the Greengrass worker's guard pattern).
        self._generation = 0
        self._stopping = False

    # -- subclass surface --------------------------------------------------

    @property
    def effective_qos(self) -> int:
        """The qos actually subscribed (the aws_iot path clamps)."""
        return self.configured_qos

    def _client_id(self) -> str:
        """The MQTT client id ('' lets paho generate one)."""
        return ""

    def _configure_client(self, client: Any) -> None:
        """Per-transport client configuration (e.g. ``tls_set``)."""

    def _connect_target(self) -> Tuple[str, int]:
        """The (host, port) this transport connects to."""
        raise NotImplementedError

    # -- worker contract ---------------------------------------------------

    def start(self) -> None:
        """Establish the connection (subscription follows in
        ``on_connect``); a failure propagates to the group's start
        containment."""
        self._health.set_state(HEALTH_CONNECTING)
        self._connect()

    def stop(self) -> None:
        """Stop the network loop and disconnect quietly."""
        with self._lock:
            self._stopping = True
        self._teardown_transport()

    def reconnect(self) -> bool:
        """One reconnect attempt (the engine's late-bound callable):
        tear the dead client down and rebuild it — connect plus, via
        ``on_connect``, the re-subscribe. True = the session was
        re-established; a raise = failed attempt (the engine retries
        with backoff per ``retry_limit``). A connection the broker then
        refuses at the MQTT level surfaces through ``on_disconnect`` /
        the failed ``on_connect`` rc and re-enters the engine as a new
        loss."""
        self._teardown_transport()
        self._connect()
        return True

    # -- internals ---------------------------------------------------------

    def _connect(self) -> None:
        client = self._new_client()
        with self._lock:
            self._generation += 1
            generation = self._generation
        client.on_connect = self._make_on_connect(generation)
        client.on_message = self._make_on_message(generation)
        client.on_disconnect = self._make_on_disconnect(generation)
        host, port = self._connect_target()
        try:
            client.connect(host, int(port))
            client.loop_start()
        except Exception:
            self._close_client_quietly(client)
            raise
        with self._lock:
            self._client = client

    def _new_client(self) -> Any:
        """One configured paho client (or the injected stub)."""
        if self._client_factory is not None:
            client = self._client_factory(self._client_id())
        else:
            import paho.mqtt.client as mqtt

            try:
                client = mqtt.Client(client_id=self._client_id())
            except TypeError:
                # paho-mqtt >= 2.0 requires the callback API version as
                # the first argument; VERSION1 keeps the classic
                # callback signatures used below.
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1,
                    client_id=self._client_id(),
                )
        self._configure_client(client)
        return client

    def _make_on_connect(self, generation: int) -> Callable:
        def on_connect(
            client: Any, _userdata: Any = None, _flags: Any = None,
            rc: Any = 0, *_extra: Any,
        ) -> None:
            if self._is_stale(generation):
                return
            if _paho_rc_failed(rc):
                self._handle_lost(
                    RuntimeError(
                        f"MQTT broker refused the connection (rc={rc})"
                    ),
                    generation,
                )
                return
            try:
                # Subscribing inside on_connect re-subscribes on every
                # (re)connection of the paho session (Requirements 6.4,
                # 6.5).
                client.subscribe(self.topic, self.effective_qos)
            except Exception as error:  # noqa: BLE001 - a failed
                # subscribe is a connection-level loss for recovery.
                self._handle_lost(error, generation)
                return
            self._health.set_state(HEALTH_SUBSCRIBED)
            logger.info(
                "Trigger '%s' subscribed to MQTT topic filter '%s' "
                "(qos %d)",
                self._health.node_id,
                self.topic,
                self.effective_qos,
            )

        return on_connect

    def _make_on_message(self, generation: int) -> Callable:
        def on_message(_client: Any, _userdata: Any, message: Any) -> None:
            if self._is_stale(generation):
                return
            try:
                raw = getattr(message, "payload", None)
                if raw is None:
                    raw = b""
                if isinstance(raw, (bytes, bytearray)):
                    payload = bytes(raw).decode("utf-8", errors="replace")
                else:
                    payload = str(raw)
                topic = getattr(message, "topic", None) or self.topic
                self._on_delivery(
                    {
                        "topic": str(topic),
                        "payload": payload,
                        "qos": self.effective_qos,
                        "timestamp": time.time(),
                    }
                )
            except Exception:  # noqa: BLE001 - containment: a delivery
                # failure never propagates into paho's network thread.
                logger.exception(
                    "Trigger '%s' failed to deliver a message from topic "
                    "filter '%s'",
                    self._health.node_id,
                    self.topic,
                )

        return on_message

    def _make_on_disconnect(self, generation: int) -> Callable:
        def on_disconnect(
            client: Any, _userdata: Any = None, rc: Any = 0, *_extra: Any
        ) -> None:
            if not _paho_rc_failed(rc):
                # rc 0 = deliberate disconnect (stop()/teardown); the
                # reconnect engine must not engage.
                return
            # Unexpected loss: flip paho into its deliberate-disconnect
            # state so its network loop exits instead of auto-reconnecting
            # — the ReconnectEngine owns recovery (retry_limit/backoff
            # semantics, Requirement 8.1), so a paho-internal retry would
            # race it.
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort loop shutdown
                logger.debug(
                    "Quiet disconnect after connection loss failed",
                    exc_info=True,
                )
            self._handle_lost(
                RuntimeError(f"MQTT connection lost (rc={rc})"), generation
            )

        return on_disconnect

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return self._stopping or generation != self._generation

    def _handle_lost(self, error: BaseException, generation: int) -> None:
        """Route a connection loss into the reconnect path — unless the
        worker is stopping or the signal comes from a stale (torn-down)
        client generation."""
        if self._is_stale(generation):
            return
        self._on_connection_lost(error)

    def _teardown_transport(self) -> None:
        """Release the current client quietly, invalidating its callbacks
        first (generation bump) so the deliberate teardown never looks
        like a connection loss."""
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
        self._close_client_quietly(client)

    @staticmethod
    def _close_client_quietly(client: Any) -> None:
        """``loop_stop`` + ``disconnect``, each tolerated to fail."""
        if client is None:
            return
        for name in ("loop_stop", "disconnect"):
            method = getattr(client, name, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception:  # noqa: BLE001 - teardown must always finish
                logger.debug(
                    "Quiet %s of MQTT client failed", name, exc_info=True
                )


def _paho_rc_failed(rc: Any) -> bool:
    """Whether a paho result/reason code signals failure — an int rc
    (callback API VERSION1) is a failure when non-zero; a ReasonCode
    object answers through ``is_failure``."""
    is_failure = getattr(rc, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    try:
        return int(rc) != 0
    except (TypeError, ValueError):
        return bool(rc)


class AwsIotTlsSubscriber(_PahoMqttSubscriber):
    """MQTT subscribe transport to AWS IoT Core over mutual TLS
    (Requirement 6.4 — design C5, task 7.2), mirroring
    ``_run_mqtt_publish``'s aws_iot connection configuration exactly:
    ``broker_host`` is the AWS IoT endpoint, a ``broker_port`` left at
    the plain-MQTT default (1883) switches to the standard mutual-TLS
    port (8883), ``iot_thing_name`` becomes the MQTT client id, the
    device-local certificate paths go to ``tls_set(ca_certs, certfile,
    keyfile)``, and the qos is clamped to 1 (AWS IoT Core does not
    support QoS 2)."""

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]],
        on_delivery: Callable[[Dict[str, Any]], Any],
        on_connection_lost: Callable[[BaseException], None],
        health: TriggerHealth,
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        super().__init__(
            parameters, on_delivery, on_connection_lost, health,
            client_factory=client_factory,
        )
        missing = [
            name for name in AWS_IOT_REQUIRED_PARAMETERS
            if not self.parameters.get(name)
        ]
        if missing:
            # Mirrors the publish path's requirement check; contained by
            # the manager as a failed health record naming the gap.
            raise ValueError(
                "AWS IoT subscribing requires {0}".format(", ".join(missing))
            )
        if not str(self.parameters.get("broker_host") or "").strip():
            raise ValueError(
                "AWS IoT subscribing requires broker_host (the AWS IoT "
                "Core endpoint, e.g. xxxxxxxx-ats.iot.<region>."
                "amazonaws.com), exactly as mqtt_publish does"
            )

    @property
    def effective_qos(self) -> int:
        return min(self.configured_qos, AWS_IOT_MAX_QOS)

    def _client_id(self) -> str:
        return str(self.parameters["iot_thing_name"])

    def _configure_client(self, client: Any) -> None:
        client.tls_set(
            ca_certs=str(self.parameters["iot_ca_cert_path"]),
            certfile=str(self.parameters["iot_client_cert_path"]),
            keyfile=str(self.parameters["iot_private_key_path"]),
        )

    def _connect_target(self) -> Tuple[str, int]:
        host = str(self.parameters["broker_host"])
        port = _coerce_int(
            self.parameters.get("broker_port"), DEFAULT_MQTT_PORT
        )
        if port == DEFAULT_MQTT_PORT:
            port = AWS_IOT_TLS_PORT
        return host, port


class PlainBrokerSubscriber(_PahoMqttSubscriber):
    """MQTT subscribe transport to a plain broker (Requirement 6.5 —
    design C5, task 7.2), mirroring ``_run_mqtt_publish``'s plain-broker
    path: ``connect(broker_host, broker_port)`` with no TLS, no client
    id override, and the configured qos unclamped."""

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]],
        on_delivery: Callable[[Dict[str, Any]], Any],
        on_connection_lost: Callable[[BaseException], None],
        health: TriggerHealth,
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        super().__init__(
            parameters, on_delivery, on_connection_lost, health,
            client_factory=client_factory,
        )
        if not str(self.parameters.get("broker_host") or "").strip():
            raise ValueError(
                "Plain-broker subscribing requires broker_host"
            )

    def _connect_target(self) -> Tuple[str, int]:
        host = str(self.parameters["broker_host"])
        port = _coerce_int(
            self.parameters.get("broker_port"), DEFAULT_MQTT_PORT
        )
        return host, port


def default_mqtt_transport_factory(
    binding_kind: str,
    parameters: Optional[Dict[str, Any]],
    on_delivery: Callable[[Dict[str, Any]], Any],
    on_connection_lost: Callable[[BaseException], None],
    health: TriggerHealth,
) -> Any:
    """The production ``mqtt_transport_factory`` (the manager's factory
    contract). Target dispatch mirrors the publish path's precedence
    (Requirements 6.3, 6.4, 6.5): ``greengrass`` →
    :class:`GreengrassIpcSubscriber`; ``aws_iot`` →
    :class:`AwsIotTlsSubscriber` (mutual TLS); otherwise a set
    ``broker_host`` → :class:`PlainBrokerSubscriber`. A configuration
    with no target at all (validator V8 normally prevents it) raises
    with a clear message, which the manager contains as a ``failed``
    health record naming the gap (Requirement 12.4)."""
    parameters = parameters or {}
    if parameters.get("greengrass"):
        return GreengrassIpcSubscriber(
            parameters, on_delivery, on_connection_lost, health
        )
    if parameters.get("aws_iot"):
        return AwsIotTlsSubscriber(
            parameters, on_delivery, on_connection_lost, health
        )
    if str(parameters.get("broker_host") or "").strip():
        return PlainBrokerSubscriber(
            parameters, on_delivery, on_connection_lost, health
        )
    raise ValueError(
        f"mqtt_subscribe trigger '{health.node_id}' declares no connection "
        "target: enable greengrass, enable aws_iot, or set broker_host "
        "(validator check V8_MQTT_SUB_NO_TARGET normally prevents this "
        "configuration)"
    )


# ---------------------------------------------------------------------------
# OPC UA worker (design C6) — session + subscription setup (task 8.1)
# ---------------------------------------------------------------------------

#: The subscription's sampling/publishing interval catalog default (ms),
#: mirroring the ``opcua_subscribe`` descriptor's ``sampling_interval_ms``
#: default.
DEFAULT_OPCUA_SAMPLING_INTERVAL_MS = 100

#: Polling_Fallback / explicit ``mode=poll`` read interval catalog
#: default (ms), mirroring the ``opcua_subscribe`` descriptor's
#: ``poll_interval_ms`` default (Requirement 6.7).
DEFAULT_OPCUA_POLL_INTERVAL_MS = 500

#: Liveness_Watchdog keepalive interval (Requirement 8.4): while a
#: subscribe-mode session is active, the watchdog reads the server-status
#: node every 5 seconds — the Phase 0.2 spike's remedy for silent
#: subscription death (a dead server sends NO signal; loss surfaces only
#: on the next read).
OPCUA_WATCHDOG_INTERVAL_SECONDS = 5.0

#: The well-known OPC UA server-status node
#: (``Server_ServerStatus``, ns=0;i=2259) the Liveness_Watchdog reads as
#: its keepalive probe.
OPCUA_SERVER_STATUS_NODE = "i=2259"

#: How long a torn-down worker waits for its watchdog/poll thread to
#: exit; the threads wake immediately on the teardown event, so the
#: timeout is a safety bound, not an expected wait.
OPCUA_MONITOR_JOIN_TIMEOUT_SECONDS = 5.0


def _opcua_source_timestamp(data: Any) -> Optional[str]:
    """The server-supplied source timestamp of one data-change
    notification as an ISO-8601 string, or None when the server did not
    supply one (design Data Models: OPC UA Trigger_Context's
    ``source_timestamp``).

    python-opcua hands ``datachange_notification`` a ``DataChangeNotif``
    whose ``monitored_item.Value.SourceTimestamp`` is a ``datetime`` (or
    None); anything unexpected degrades to None rather than failing the
    delivery.
    """
    try:
        value = getattr(getattr(data, "monitored_item", None), "Value", None)
        timestamp = getattr(value, "SourceTimestamp", None)
        if timestamp is None:
            return None
        isoformat = getattr(timestamp, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
        return str(timestamp)
    except Exception:  # noqa: BLE001 - a malformed notification never
        # costs the firing; the timestamp is simply absent.
        return None


class _OpcuaDataChangeHandler:
    """The ``SubHandler``-shaped object ``create_subscription`` receives:
    python-opcua calls ``datachange_notification(node, val, data)`` on it
    from the subscription's delivery thread. Bound to one subscribe
    generation so a notification from a torn-down session is inert."""

    def __init__(self, worker: "OpcuaSubscribeWorker", generation: int) -> None:
        self._worker = worker
        self._generation = generation

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        self._worker._handle_datachange(val, data, self._generation)


class OpcuaSubscribeWorker:
    """OPC UA subscribe transport worker (Requirements 6.6, 6.7, 6.8,
    8.4, 8.5, 8.6 — design C6, tasks 8.1/8.2): one ``python-opcua``
    client session per trigger node with a true data-change subscription
    on ``node_id``, a Liveness_Watchdog, subscribe→poll auto-fallback,
    and an explicit poll mode.

    Fits the manager's transport-factory worker contract: ``start()`` /
    ``stop()`` plus a callable ``reconnect`` attribute the manager
    late-binds into the node's :class:`ReconnectEngine`. The ``opcua``
    client is imported lazily inside :meth:`_build_session`, so this
    module stays importable without the package (matching
    ``output_bindings``' discipline).

    - **Session build** mirrors ``output_bindings._default_opcua_writer``
      exactly (Requirement 6.6): ``opcua.Client(endpoint)`` →
      ``set_user``/``set_password``/``set_security_string`` from the SAME
      parameter mapping (the security dict is produced by the reused
      ``output_bindings._opcua_security_from_params`` helper; the
      client-configuration calls are transcribed faithfully from the
      writer, which applies them inline) → ``connect()``.
    - **subscribe mode**: ``client.create_subscription(
      sampling_interval_ms, handler)`` +
      ``subscription.subscribe_data_change(client.get_node(node_id))``;
      ``datachange_notification`` builds Trigger_Context
      ``{endpoint, node_id, value, source_timestamp}`` (value passed
      through as delivered — ``serialize_trigger_context`` degrades
      non-JSON-native types at persistence time; ``source_timestamp`` is
      ISO-8601 when the server supplies one, else None) →
      ``on_delivery`` (Requirement 6.8). Handler exceptions are contained
      — nothing ever raises into the opcua delivery thread.
    - **Liveness_Watchdog** (Requirement 8.4, the key spike finding —
      a dying server sends the subscribed client NO signal): while a
      subscribe-mode session is active, a daemon watchdog thread performs
      a keepalive read of the server-status node
      (:data:`OPCUA_SERVER_STATUS_NODE`) every
      :data:`OPCUA_WATCHDOG_INTERVAL_SECONDS` seconds. ANY keepalive
      exception (``TimeoutError`` / ``BrokenPipeError`` /
      ``ConnectionError`` / ``CancelledError`` per the spike, or anything
      else) tears the ENTIRE session down and routes to
      ``on_connection_lost`` — the reconnect engine then drives
      backoff/rebuild. Generation/stop guards keep a stopping worker's
      watchdog from ever signalling loss.
    - **Auto-fallback** (Requirement 8.5): when the session connect
      succeeded but ``create_subscription``/``subscribe_data_change``
      fails, the worker enters Polling_Fallback on that SAME session —
      a poll loop at ``poll_interval_ms``, health mechanism ``poll``
      (``autoFallback: true``) + state ``polling``, the transition
      logged naming the trigger node. A session-connect failure is a
      normal connection failure and raises as before.
    - **poll mode** (Requirements 6.7, 3.5): explicit ``mode=poll``
      skips subscription entirely — a poll loop reads ``node_id``
      (``get_value()``) every ``poll_interval_ms``; the first read
      primes and every read whose value differs from the previous read
      fires. The Trigger_Context is shape-identical to subscribe mode's;
      ``source_timestamp`` is always None under polling (the plain value
      read carries no server timestamp — documented shape consistency,
      Requirement 6.8). Health ``polling``, mechanism ``poll``
      (``autoFallback: false``). A poll-read exception tears the session
      down and routes to ``on_connection_lost``.
    - **reconnect** (Requirement 8.6): tears the ENTIRE session down and
      rebuilds it from scratch — per the spike the broken secure channel
      is unrecoverable. A ``mode=subscribe`` rebuild ALWAYS attempts a
      true subscription first (even after an auto-fallback), falling
      back again only if subscription setup fails again; explicit
      ``mode=poll`` rebuilds straight into polling. True = rebuilt;
      a raise = failed attempt (the engine retries with backoff per
      ``retry_limit``). :meth:`restored_state` (zero-arg, late-bound
      into the engine by the manager) answers ``polling`` or
      ``subscribed`` per the CURRENT mechanism so the engine restores
      the right health state.
    - ``stop()``: quiet teardown — per the Phase 0.2 spike, tearing down
      a DEAD session raises (``BrokenPipeError``/``CancelledError``/
      ``TimeoutError`` on channel operations), so subscription delete and
      client disconnect each tolerate failure. Watchdog/poll threads
      wake immediately (event-based waits, no bare sleeps) and are
      joined promptly.
    - A subscribe-generation guard (the MQTT workers' pattern) makes
      notifications, keepalive failures, and poll reads from a torn-down
      session inert, so a stale signal can never fire a spurious
      activation or a spurious loss.

    Injection seams (tests need neither the opcua package nor real
    time): ``client_factory`` (``(endpoint) -> client``) substitutes a
    stub client — it receives the same security-configuration and
    ``connect()`` calls the real one does; ``watchdog_interval``
    overrides the 5 s keepalive cadence; ``waiter(delay_seconds) ->
    bool`` replaces the event-based wait for BOTH the watchdog and poll
    loops (return True to cancel the loop, mirroring the
    :class:`ReconnectEngine` waiter contract).
    """

    def __init__(
        self,
        parameters: Optional[Dict[str, Any]],
        on_delivery: Callable[[Dict[str, Any]], Any],
        on_connection_lost: Callable[[BaseException], None],
        health: TriggerHealth,
        client_factory: Optional[Callable[[str], Any]] = None,
        watchdog_interval: float = OPCUA_WATCHDOG_INTERVAL_SECONDS,
        waiter: Optional[Callable[[float], bool]] = None,
    ) -> None:
        self.parameters = dict(parameters or {})
        self.endpoint = str(self.parameters.get("endpoint") or "")
        self.node_id = str(self.parameters.get("node_id") or "")
        self.sampling_interval_ms = _coerce_int(
            self.parameters.get("sampling_interval_ms"),
            DEFAULT_OPCUA_SAMPLING_INTERVAL_MS,
        )
        #: The configured detection mode: ``subscribe`` (the catalog
        #: default; anything unrecognized degrades to it) or ``poll``
        #: (Requirement 6.7).
        self.mode = (
            MECHANISM_POLL
            if str(self.parameters.get("mode") or "") == MECHANISM_POLL
            else MECHANISM_SUBSCRIBE
        )
        self.poll_interval_ms = _coerce_int(
            self.parameters.get("poll_interval_ms"),
            DEFAULT_OPCUA_POLL_INTERVAL_MS,
        )
        if not self.endpoint.strip():
            # Contained by the manager as a failed health record naming
            # the gap (the catalog requires endpoint; defensive here).
            raise ValueError("OPC UA subscribing requires endpoint")
        if not self.node_id.strip():
            raise ValueError("OPC UA subscribing requires node_id")
        self._on_delivery = on_delivery
        self._on_connection_lost = on_connection_lost
        self._health = health
        self._client_factory = client_factory
        self._watchdog_interval = watchdog_interval
        self._waiter = waiter
        self._lock = threading.Lock()
        self._client: Any = None
        self._subscription: Any = None
        #: Bumped on every (re)establish and teardown so a data-change
        #: notification, keepalive failure, or poll read from a
        #: torn-down session can never deliver a spurious firing or a
        #: spurious loss (the MQTT workers' generation-guard pattern).
        self._generation = 0
        self._stopping = False
        #: The current generation's wake event — set on teardown so the
        #: watchdog/poll thread's event-based wait cancels immediately
        #: (no bare sleeps); recreated per establish.
        self._wake: Optional[threading.Event] = None
        #: The current generation's monitor threads (watchdog OR poll —
        #: never both), joined on teardown.
        self._monitor_threads: List[threading.Thread] = []

    # -- worker contract ---------------------------------------------------

    def start(self) -> None:
        """Establish the session and detection mechanism per ``mode``
        (health ``connecting`` → ``subscribed`` or ``polling``); a
        session-connect failure propagates to the group's start
        containment, a subscription-setup failure auto-falls-back to
        polling (Requirement 8.5)."""
        self._health.set_state(HEALTH_CONNECTING)
        self._establish()

    def stop(self) -> None:
        """Tear the session down quietly (a dead session's teardown
        raises per the spike — tolerated); the watchdog/poll thread is
        cancelled and joined promptly."""
        with self._lock:
            self._stopping = True
        self._teardown_session()

    def reconnect(self) -> bool:
        """One reconnect attempt (the engine's late-bound callable): tear
        the ENTIRE session down and rebuild client, subscription, and
        monitored item from scratch — per the Phase 0.2 spike the broken
        channel is unrecoverable, so nothing of the old session is
        reused. A ``mode=subscribe`` rebuild ALWAYS attempts a true
        subscription first, even after an auto-fallback, falling back
        again only if subscription setup fails again (Requirement 8.6);
        explicit ``mode=poll`` rebuilds straight into polling. True =
        rebuilt; a raise = failed attempt (the engine retries with
        backoff per ``retry_limit``)."""
        self._teardown_session()
        self._establish()
        return True

    def restored_state(self) -> str:
        """The health state a successful reconnect restores (the
        engine's ``restored_state`` seam, Requirements 8.2, 8.6):
        ``polling`` or ``subscribed`` per the CURRENT mechanism — a
        rebuild that auto-fell-back restores ``polling``, one that
        re-established the true subscription restores ``subscribed``."""
        if self._health.mechanism == MECHANISM_POLL:
            return HEALTH_POLLING
        return HEALTH_SUBSCRIBED

    # -- internals ---------------------------------------------------------

    def _establish(self) -> None:
        """Establish per the configured ``mode``: ``poll`` skips
        subscription entirely (Requirement 6.7); ``subscribe`` attempts
        the true subscription (with auto-fallback, Requirement 8.5)."""
        if self.mode == MECHANISM_POLL:
            self._establish_poll()
        else:
            self._establish_subscribe()

    def _establish_subscribe(self) -> None:
        """Build the session and register the data-change subscription
        (Requirement 6.6), then start the Liveness_Watchdog
        (Requirement 8.4). A session-connect failure propagates (a
        normal connection failure); a subscription-setup failure on the
        connected session enters Polling_Fallback on that SAME session
        instead of raising (auto-fallback, Requirement 8.5)."""
        generation, wake = self._next_generation()
        client = self._build_session()
        try:
            handler = _OpcuaDataChangeHandler(self, generation)
            subscription = client.create_subscription(
                self.sampling_interval_ms, handler
            )
            subscription.subscribe_data_change(client.get_node(self.node_id))
        except (Exception, CancelledError) as error:  # noqa: BLE001 -
            # auto-fallback (Requirement 8.5): the session connect
            # succeeded — only the subscription setup failed — so this
            # session polls. (If the channel actually died mid-setup,
            # the first poll read fails and routes to the reconnect
            # path.)
            logger.warning(
                "Trigger '%s' could not establish a true OPC UA "
                "subscription on node '%s' at %s (%s); falling back to "
                "polling every %d ms",
                self._health.node_id,
                self.node_id,
                self.endpoint,
                error,
                self.poll_interval_ms,
            )
            self._enter_poll(client, generation, wake, auto_fallback=True)
            return
        with self._lock:
            self._client = client
            self._subscription = subscription
        self._health.set_mechanism(MECHANISM_SUBSCRIBE)
        self._health.set_state(HEALTH_SUBSCRIBED)
        logger.info(
            "Trigger '%s' subscribed to OPC UA node '%s' at %s "
            "(sampling interval %d ms)",
            self._health.node_id,
            self.node_id,
            self.endpoint,
            self.sampling_interval_ms,
        )
        self._start_monitor_thread(
            self._watchdog_loop, client, generation, wake, "watchdog"
        )

    def _establish_poll(self) -> None:
        """Explicit ``mode=poll`` (Requirement 6.7): connect the session
        (same security build) and poll — no subscription objects at all."""
        generation, wake = self._next_generation()
        client = self._build_session()
        self._enter_poll(client, generation, wake, auto_fallback=False)

    def _enter_poll(
        self,
        client: Any,
        generation: int,
        wake: threading.Event,
        auto_fallback: bool,
    ) -> None:
        """Adopt ``client`` as a polling session: health ``polling`` +
        mechanism ``poll`` (flagged when entered by auto-fallback,
        Requirement 8.5) and the poll loop thread started."""
        with self._lock:
            self._client = client
            self._subscription = None
        self._health.set_mechanism(MECHANISM_POLL, auto_fallback=auto_fallback)
        self._health.set_state(HEALTH_POLLING)
        if not auto_fallback:
            logger.info(
                "Trigger '%s' polling OPC UA node '%s' at %s every %d ms",
                self._health.node_id,
                self.node_id,
                self.endpoint,
                self.poll_interval_ms,
            )
        self._start_monitor_thread(
            self._poll_loop, client, generation, wake, "poll"
        )

    def _build_session(self) -> Any:
        """One connected, security-configured opcua client — the exact
        ``_default_opcua_writer`` session build (Requirement 6.6):
        ``Client(endpoint)``, then the security calls, then ``connect()``.
        The ``opcua`` import happens here (lazily) so the module imports
        without the package."""
        if self._client_factory is not None:
            client = self._client_factory(self.endpoint)
        else:
            try:
                from opcua import Client
            except ImportError as e:
                raise RuntimeError(
                    "The 'opcua' Python package is not available; it is "
                    "delivered as a Workflow_Component dependency"
                ) from e
            client = Client(self.endpoint)
        self._apply_security(client)
        client.connect()
        return client

    def _apply_security(self, client: Any) -> None:
        """Apply the ``opcua_write`` security mapping to the client
        (Requirement 6.6). The parameter→security-dict extraction REUSES
        ``output_bindings._opcua_security_from_params`` (imported lazily —
        ``output_bindings`` is stdlib-import-light); the
        ``set_user``/``set_password``/``set_security_string`` application
        below is transcribed faithfully from
        ``output_bindings._default_opcua_writer``, where it lives inline
        in the writer function rather than in an importable helper."""
        from workflow_engine.output_bindings import (
            _opcua_security_from_params,
        )

        security = _opcua_security_from_params(self.parameters)
        if not security:
            return
        username = security.get("username")
        if username:
            client.set_user(str(username))
            password = security.get("password")
            if password is not None:
                client.set_password(str(password))
        policy = security.get("security_policy")
        cert = security.get("client_cert_path")
        key = security.get("client_key_path")
        if policy and cert and key:
            # opcua set_security_string format:
            # "<Policy>,<Mode>,<client_cert>,<client_key>[,<server_cert>]"
            mode = security.get("security_mode") or "SignAndEncrypt"
            parts = [str(policy), str(mode), str(cert), str(key)]
            server_cert = security.get("server_cert_path")
            if server_cert:
                parts.append(str(server_cert))
            client.set_security_string(",".join(parts))

    def _handle_datachange(
        self, value: Any, data: Any, generation: int
    ) -> None:
        """Build the OPC UA Trigger_Context ``{endpoint, node_id, value,
        source_timestamp}`` from one data-change notification and deliver
        it (Requirement 6.8); contained — a delivery failure is logged,
        never raised into the opcua subscription thread."""
        if self._is_stale(generation):
            return
        try:
            self._on_delivery(
                {
                    "endpoint": self.endpoint,
                    "node_id": self.node_id,
                    # Passed through as delivered; non-JSON-native variant
                    # values degrade to strings at persistence time
                    # (serialize_trigger_context).
                    "value": value,
                    "source_timestamp": _opcua_source_timestamp(data),
                }
            )
        except Exception:  # noqa: BLE001 - containment: a delivery
            # failure never propagates into the opcua delivery thread.
            logger.exception(
                "Trigger '%s' failed to deliver a data change from OPC UA "
                "node '%s' at %s",
                self._health.node_id,
                self.node_id,
                self.endpoint,
            )

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return self._stopping or generation != self._generation

    # -- Liveness_Watchdog and poll loop (task 8.2) --------------------------

    def _next_generation(self) -> Tuple[int, threading.Event]:
        """Open a new establish generation: bump the counter and install
        a fresh wake event (one per generation, so a stale thread's
        pending wait can never swallow the new generation's cancel)."""
        with self._lock:
            self._generation += 1
            wake = threading.Event()
            self._wake = wake
            return self._generation, wake

    def _start_monitor_thread(
        self,
        target: Callable[[Any, int, threading.Event], None],
        client: Any,
        generation: int,
        wake: threading.Event,
        kind: str,
    ) -> None:
        """One daemon monitor thread (watchdog or poll) bound to one
        establish generation; tracked so teardown joins it."""
        thread = threading.Thread(
            target=target,
            args=(client, generation, wake),
            name=f"trigger-opcua-{kind}-{self._health.node_id}",
            daemon=True,
        )
        with self._lock:
            self._monitor_threads.append(thread)
        thread.start()

    def _wait(self, wake: threading.Event, delay_seconds: float) -> bool:
        """The watchdog/poll interval wait — True when cancelled.
        Event-based (teardown sets ``wake`` so a parked wait ends
        immediately — no bare sleeps); the injected ``waiter`` replaces
        it for tests (same contract as :class:`ReconnectEngine`)."""
        if self._waiter is not None:
            return bool(self._waiter(delay_seconds))
        return wake.wait(delay_seconds)

    def _watchdog_loop(
        self, client: Any, generation: int, wake: threading.Event
    ) -> None:
        """The Liveness_Watchdog (Requirement 8.4): while this
        generation's subscribe-mode session is active, read the
        server-status node every :attr:`_watchdog_interval` seconds.
        ANY keepalive exception (``TimeoutError`` / ``BrokenPipeError``
        / ``ConnectionError`` / ``CancelledError`` per the spike, or
        anything else) means the session is dead — tear the ENTIRE
        session down and route to ``on_connection_lost`` (the reconnect
        engine drives backoff/rebuild). A stopping/torn-down worker's
        watchdog exits without ever signalling loss."""
        while True:
            if self._wait(wake, self._watchdog_interval):
                return
            if self._is_stale(generation):
                return
            try:
                client.get_node(OPCUA_SERVER_STATUS_NODE).get_value()
            except (Exception, CancelledError) as error:  # noqa: BLE001 -
                # ANY keepalive failure means the session is dead (the
                # spike's silent subscription death surfaces exactly
                # here); CancelledError is a BaseException since
                # Python 3.8 and is one of the spike's four loss
                # signals, so it is named explicitly.
                if self._is_stale(generation):
                    return
                logger.warning(
                    "Trigger '%s' OPC UA keepalive to %s failed (%s: %s); "
                    "tearing the session down",
                    self._health.node_id,
                    self.endpoint,
                    type(error).__name__,
                    error,
                )
                self._lose_session(error, generation)
                return

    def _poll_loop(
        self, client: Any, generation: int, wake: threading.Event
    ) -> None:
        """Polling value-change detection (Requirements 6.7, 8.5): read
        ``node_id`` every ``poll_interval_ms``; the FIRST read primes
        (no firing) and each subsequent read fires exactly when its
        value differs from the previous read. ``source_timestamp`` is
        None under polling (documented in the class docstring). A read
        exception tears the session down and routes to
        ``on_connection_lost``."""
        primed = False
        last_value: Any = None
        while True:
            if self._is_stale(generation):
                return
            try:
                value = client.get_node(self.node_id).get_value()
            except (Exception, CancelledError) as error:  # noqa: BLE001 -
                # a failed poll read means the session is dead; the
                # reconnect engine owns recovery (CancelledError is a
                # BaseException since Python 3.8 — named explicitly).
                if self._is_stale(generation):
                    return
                logger.warning(
                    "Trigger '%s' OPC UA poll read of node '%s' at %s "
                    "failed (%s: %s); tearing the session down",
                    self._health.node_id,
                    self.node_id,
                    self.endpoint,
                    type(error).__name__,
                    error,
                )
                self._lose_session(error, generation)
                return
            if self._is_stale(generation):
                return
            if primed and value != last_value:
                self._deliver_polled_value(value)
            primed = True
            last_value = value
            if self._wait(wake, self.poll_interval_ms / 1000.0):
                return

    def _deliver_polled_value(self, value: Any) -> None:
        """One poll-mode firing — the same Trigger_Context shape as
        subscribe mode's (Requirement 6.8), ``source_timestamp`` None
        (a plain ``get_value()`` read carries no server timestamp);
        contained like :meth:`_handle_datachange`."""
        try:
            self._on_delivery(
                {
                    "endpoint": self.endpoint,
                    "node_id": self.node_id,
                    "value": value,
                    "source_timestamp": None,
                }
            )
        except Exception:  # noqa: BLE001 - containment: a delivery
            # failure never kills the poll loop.
            logger.exception(
                "Trigger '%s' failed to deliver a polled value change "
                "from OPC UA node '%s' at %s",
                self._health.node_id,
                self.node_id,
                self.endpoint,
            )

    def _lose_session(self, error: BaseException, generation: int) -> None:
        """Tear the ENTIRE session down and route the loss to
        ``on_connection_lost`` — unless the worker is stopping or the
        signal comes from a stale generation (a stopping worker's
        watchdog/poll thread never signals loss)."""
        with self._lock:
            if self._stopping or generation != self._generation:
                return
        self._teardown_session()
        self._on_connection_lost(error)

    def _teardown_session(self) -> None:
        """Release the current subscription and client quietly,
        invalidating the data-change handler and the watchdog/poll
        thread first (generation bump + wake), then joining the
        monitor threads (the calling thread excluded — the watchdog
        tears down from inside itself on a keepalive failure).
        Per the spike, teardown of a DEAD session raises
        (``BrokenPipeError`` etc. on channel operations) — every step
        tolerates failure so teardown always completes."""
        with self._lock:
            self._generation += 1
            subscription = self._subscription
            client = self._client
            self._subscription = None
            self._client = None
            wake = self._wake
            self._wake = None
            threads = list(self._monitor_threads)
            self._monitor_threads.clear()
        if wake is not None:
            wake.set()
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            thread.join(timeout=OPCUA_MONITOR_JOIN_TIMEOUT_SECONDS)
        self._close_session_quietly(subscription, client)

    @staticmethod
    def _close_session_quietly(subscription: Any, client: Any) -> None:
        """``subscription.delete()`` + ``client.disconnect()``, each
        tolerated to fail (a dead session's teardown raises per the
        spike)."""
        if subscription is not None:
            try:
                subscription.delete()
            except (Exception, CancelledError):  # noqa: BLE001 - teardown
                # must always finish (a dead session raises
                # CancelledError/BrokenPipeError on channel operations
                # per the spike).
                logger.debug(
                    "Quiet delete of OPC UA subscription failed",
                    exc_info=True,
                )
        if client is not None:
            try:
                client.disconnect()
            except (Exception, CancelledError):  # noqa: BLE001 - teardown
                # must always finish.
                logger.debug(
                    "Quiet disconnect of OPC UA client failed",
                    exc_info=True,
                )


def default_opcua_transport_factory(
    binding_kind: str,
    parameters: Optional[Dict[str, Any]],
    on_delivery: Callable[[Dict[str, Any]], Any],
    on_connection_lost: Callable[[BaseException], None],
    health: TriggerHealth,
) -> Any:
    """The production ``opcua_transport_factory`` (the manager's factory
    contract, Requirements 6.6, 6.7): one :class:`OpcuaSubscribeWorker`
    per ``opcua_subscribe`` binding — true subscription with the
    Liveness_Watchdog and subscribe→poll auto-fallback under
    ``mode=subscribe`` (the default), pure polling under
    ``mode=poll``."""
    return OpcuaSubscribeWorker(
        parameters, on_delivery, on_connection_lost, health
    )
