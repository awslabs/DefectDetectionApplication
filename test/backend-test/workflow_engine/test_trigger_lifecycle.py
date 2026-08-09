# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Example tests for trigger lifecycle and dispatch wiring (task 6.6).

Feature: trigger-activation-runtime — TriggerSubscriptionManager lifecycle
(groups start when a trigger-driven registration appears and stop on
removal/invalidation), the dispatcher's pending ``WorkflowExecution`` with
``trigger_context_json`` and at-most-one-in-flight-run guarantee, failing-run
containment, and the manager's zero-trigger no-op / contained reconcile
failure.

Requirements: 6.1, 6.2, 7.6, 7.7, 12.4
"""
import copy
import json
import threading
import time

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    VALID_COMPILED,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.trigger_runtime import (
    HEALTH_SUBSCRIBED,
    TriggerSubscriptionManager,
)

REGISTRATION_ID = "reg-trig-1"
TRIGGER_NODE_ID = "trig1"
ACTIVATED_INPUT_ID = "input1"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def trigger_compiled(node_id=TRIGGER_NODE_ID, parameters=None, activates=(ACTIVATED_INPUT_ID,)):
    """A compiled document whose executorBindings carry one mqtt_subscribe
    Trigger_Binding with an ``activates`` list (compiler task 3.1's shape)."""
    document = copy.deepcopy(VALID_COMPILED)
    binding_parameters = {
        "topic": "factory/line1/start",
        "qos": 1,
        "greengrass": True,
        "concurrency_policy": "queue",
        "queue_depth": 10,
        "retry_limit": 0,
        "priority": 100,
    }
    binding_parameters.update(parameters or {})
    document["executorBindings"] = [
        {
            "nodeId": node_id,
            "binding": "mqtt_subscribe",
            "parameters": binding_parameters,
            "upstreamNodeIds": [],
            "downstreamNodeIds": [],
            "activates": list(activates),
        }
    ]
    return document


def add_registration(
    session_factory,
    registration_id=REGISTRATION_ID,
    artifact_path="",
    status="registered",
):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=registration_id,
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=artifact_path,
                status=status,
                registered_at=int(time.time()),
            )
        )
        session.commit()
    finally:
        session.close()


def set_registration_status(session_factory, registration_id, status):
    session = session_factory()
    try:
        row = session.get(WorkflowRegistration, registration_id)
        row.status = status
        session.commit()
    finally:
        session.close()


def remove_registration(session_factory, registration_id):
    session = session_factory()
    try:
        row = session.get(WorkflowRegistration, registration_id)
        session.delete(row)
        session.commit()
    finally:
        session.close()


class StubTransportWorker:
    """Fits the manager's transport-factory worker contract: ``start()`` /
    ``stop()``, recording each call (Requirement 6.2 injection seam)."""

    def __init__(self, kind, parameters, on_delivery, on_connection_lost, health):
        self.kind = kind
        self.parameters = parameters
        self.on_delivery = on_delivery
        self.on_connection_lost = on_connection_lost
        self.health = health
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        self.health.set_state(HEALTH_SUBSCRIBED)

    def stop(self):
        self.stop_calls += 1


class StubTransportFactory:
    """``factory(kind, parameters, on_delivery, on_connection_lost, health)
    -> worker``, recording every construction."""

    def __init__(self):
        self.calls = []
        self.workers = []

    def __call__(self, kind, parameters, on_delivery, on_connection_lost, health):
        self.calls.append((kind, dict(parameters)))
        worker = StubTransportWorker(
            kind, parameters, on_delivery, on_connection_lost, health
        )
        self.workers.append(worker)
        return worker


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def manager_stopper():
    """Stop every manager built in a test so no dispatcher/worker thread
    outlives it."""
    managers = []
    yield managers.append
    for manager in managers:
        manager.stop()


@pytest.fixture
def no_registered_executor():
    """Ensure no workflow executor is registered (dispatch returns False →
    triggered runs stay pending, the manual-trigger behavior)."""
    previous = executor.get_executor()
    executor.set_executor(None)
    yield
    executor.set_executor(previous)


def make_manager(session_factory, mqtt_factory=None, run_starter_factory=None):
    return TriggerSubscriptionManager(
        session_factory=session_factory,
        mqtt_transport_factory=mqtt_factory,
        run_starter_factory=run_starter_factory,
    )


# ---------------------------------------------------------------------------
# (a) Lifecycle: groups start on registration, stop on removal/invalidation
#     (Requirements 6.1, 6.2)
# ---------------------------------------------------------------------------


def test_group_starts_when_trigger_registration_appears(tmp_path, manager_stopper):
    """A registered trigger-driven artifact set gets one group with one
    started stub worker per Trigger_Binding (Requirements 6.1, 6.2)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: lambda activation: None,
    )
    manager_stopper(manager)

    manager.on_registrations_changed()

    group = manager.group(REGISTRATION_ID)
    assert group is not None
    # The injected factory received the binding's kind and parameters
    # (Requirement 6.2), and the worker was started (Requirement 6.1).
    assert factory.calls == [
        (
            "mqtt_subscribe",
            trigger_compiled()["executorBindings"][0]["parameters"],
        )
    ]
    assert len(factory.workers) == 1
    assert factory.workers[0].start_calls == 1
    assert factory.workers[0].stop_calls == 0
    # Trigger_Health surfaces the subscribed worker.
    records = manager.health(REGISTRATION_ID)
    assert [r["nodeId"] for r in records] == [TRIGGER_NODE_ID]
    assert records[0]["state"] == HEALTH_SUBSCRIBED


def test_group_stops_when_registration_is_removed(tmp_path, manager_stopper):
    """Removing the registration row stops and releases the workflow's
    subscriptions (Requirement 6.1)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: lambda activation: None,
    )
    manager_stopper(manager)

    manager.on_registrations_changed()
    assert manager.group(REGISTRATION_ID) is not None

    remove_registration(session_factory, REGISTRATION_ID)
    manager.on_registrations_changed()

    assert manager.group(REGISTRATION_ID) is None
    assert factory.workers[0].stop_calls == 1
    assert manager.health(REGISTRATION_ID) == []


def test_group_stops_when_registration_becomes_invalid(tmp_path, manager_stopper):
    """A registration leaving ``registered`` status (invalidation) stops its
    group exactly like removal (Requirement 6.1)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: lambda activation: None,
    )
    manager_stopper(manager)

    manager.on_registrations_changed()
    assert manager.group(REGISTRATION_ID) is not None

    set_registration_status(session_factory, REGISTRATION_ID, "invalid")
    manager.on_registrations_changed()

    assert manager.group(REGISTRATION_ID) is None
    assert factory.workers[0].stop_calls == 1


# ---------------------------------------------------------------------------
# (b) Dispatch: pending WorkflowExecution with trigger_context_json, at most
#     one in-flight run (Requirement 7.6)
# ---------------------------------------------------------------------------


def test_dispatch_persists_pending_execution_with_trigger_context(
    tmp_path, manager_stopper, no_registered_executor
):
    """A delivery through the stub transport reaches default_run_starter,
    which inserts a pending WorkflowExecution with the Trigger_Context
    persisted as trigger_context_json — with no executor registered the row
    stays pending, exactly the manual-trigger behavior (Requirement 7.6)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    factory = StubTransportFactory()
    # No run_starter_factory: the manager builds the production
    # default_run_starter over the injected sqlite session factory.
    manager = make_manager(session_factory, mqtt_factory=factory)
    manager_stopper(manager)
    manager.on_registrations_changed()

    context = {
        "topic": "factory/line1/start",
        "payload": '{"go": true}',
        "qos": 1,
        "timestamp": 1723456789.5,
    }
    assert factory.workers[0].on_delivery(context) is True

    def executions():
        session = session_factory()
        try:
            return session.query(WorkflowExecution).all()
        finally:
            session.close()

    assert wait_for(lambda: len(executions()) == 1)
    row = executions()[0]
    assert row.registration_id == REGISTRATION_ID
    assert row.status == "pending"
    assert json.loads(row.trigger_context_json) == context


def test_dispatch_runs_at_most_one_activation_at_a_time(tmp_path, manager_stopper):
    """The dispatcher is sequential: while a run is in flight, a second
    accepted firing waits in the queue and only dispatches after the first
    run completes (Requirement 7.6)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    release = threading.Event()
    lock = threading.Lock()
    state = {"started": [], "concurrent": 0, "max_concurrent": 0}

    def blocking_run_starter(activation):
        with lock:
            state["concurrent"] += 1
            state["max_concurrent"] = max(
                state["max_concurrent"], state["concurrent"]
            )
            state["started"].append(activation)
        release.wait(timeout=5)
        with lock:
            state["concurrent"] -= 1

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: blocking_run_starter,
    )
    manager_stopper(manager)
    manager.on_registrations_changed()

    worker = factory.workers[0]
    assert worker.on_delivery({"topic": "t", "payload": "1", "qos": 0, "timestamp": 1.0})
    assert worker.on_delivery({"topic": "t", "payload": "2", "qos": 0, "timestamp": 2.0})

    # The first activation is in flight; the second must NOT start.
    assert wait_for(lambda: len(state["started"]) == 1)
    time.sleep(0.4)  # give a broken concurrent dispatcher time to misbehave
    assert len(state["started"]) == 1

    release.set()
    assert wait_for(lambda: len(state["started"]) == 2)
    assert state["max_concurrent"] == 1
    payloads = [activation.context["payload"] for activation in state["started"]]
    assert payloads == ["1", "2"]  # FIFO at equal priority


# ---------------------------------------------------------------------------
# (c) Failing run containment (Requirement 7.7)
# ---------------------------------------------------------------------------


def test_failing_run_does_not_stop_subsequent_dispatches(tmp_path, manager_stopper):
    """A run_starter that raises is contained on that activation alone; the
    dispatcher continues with the next one (Requirement 7.7)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path, compiled=trigger_compiled())
    add_registration(session_factory, artifact_path=version_dir)

    attempted = []

    def failing_then_fine(activation):
        attempted.append(activation.context["payload"])
        if len(attempted) == 1:
            raise RuntimeError("pipeline exploded")

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: failing_then_fine,
    )
    manager_stopper(manager)
    manager.on_registrations_changed()

    worker = factory.workers[0]
    assert worker.on_delivery({"topic": "t", "payload": "boom", "qos": 0, "timestamp": 1.0})
    assert wait_for(lambda: attempted == ["boom"])

    # The failed run never blocks the next activation.
    assert worker.on_delivery({"topic": "t", "payload": "ok", "qos": 0, "timestamp": 2.0})
    assert wait_for(lambda: attempted == ["boom", "ok"])
    # The group is still healthy and dispatching.
    assert manager.group(REGISTRATION_ID) is not None


# ---------------------------------------------------------------------------
# (d) Zero-trigger no-op and contained reconcile failure (Requirement 12.4)
# ---------------------------------------------------------------------------


def test_manager_is_a_noop_with_zero_trigger_driven_workflows(
    tmp_path, manager_stopper
):
    """A registered zero-trigger workflow starts no group and no worker
    (Requirement 12.4)."""
    session_factory = make_session_factory()
    version_dir = write_artifact_set(tmp_path)  # VALID_COMPILED: no bindings
    add_registration(session_factory, artifact_path=version_dir)

    factory = StubTransportFactory()
    manager = make_manager(
        session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: lambda activation: None,
    )
    manager_stopper(manager)

    manager.on_registrations_changed()

    assert manager.group(REGISTRATION_ID) is None
    assert factory.calls == []
    assert factory.workers == []
    assert manager.health(REGISTRATION_ID) == []


def test_reconcile_failure_is_contained(manager_stopper):
    """A broken session factory makes on_registrations_changed log and
    return — it never raises, and the runtime continues (Requirement 12.4)."""

    def broken_session_factory():
        raise RuntimeError("database is gone")

    factory = StubTransportFactory()
    manager = make_manager(
        broken_session_factory,
        mqtt_factory=factory,
        run_starter_factory=lambda registration_id: lambda activation: None,
    )
    manager_stopper(manager)

    # Must not raise (containment) and must not start anything.
    manager.on_registrations_changed()
    assert factory.calls == []
    assert manager.health(REGISTRATION_ID) == []
