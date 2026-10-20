# Copyright 2026 Amazon Web Services, Inc.
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
"""Engine executions and the self-heal (triton-model-cleanup-wedge task
4.3, design D7, Property 15): every execution registers at its start, is
never held, is refused only after the commit, and a triggered or manual
execution that the restart cuts off is marked failed with the self-heal
error.

The executor is the real ``WorkflowExecutor`` on a temporary sqlite
database; the pipeline manager and the Triton client are fakes, and the
run registry is a private ``RunActivity`` instance on a fake clock."""
import inspect
import json
import os
import tempfile
import threading
import time

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dao.sqlite_db.sqlite_db_operations import Base
import workflow_engine.models  # noqa: F401 - registers the tables on Base
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    EXECUTION_STATUS_RUNNING,
    WorkflowExecutor,
)
from workflow_engine.stream_feed import CONTINUOUS_SOURCE
from utils import run_activity as ra
from triton_cleanup_wedge.fakes import FakeTritonClient

ARCH = "x86_64"
WORKFLOW_ID = "wf-1"
REGISTRATION_ID = "wf-1:2"
MODEL = "model-a"
ERROR = ("Interrupted: self-heal attempt 1 restarted the LocalServer backend to clear a wedged "
         "Triton model load; run the workflow again.")
REFUSAL = ("The LocalServer backend is restarting to clear a wedged Triton model load (self-heal "
           "attempt 1); run the workflow again in a few minutes.")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def activity():
    clock = Clock()
    instance = ra.RunActivity(clock=clock)
    instance.clock = clock
    previous = ra.install(instance)
    yield instance
    ra.install(previous)


def document(with_model=True):
    elements = [{"nodeId": "n1", "factory": "videotestsrc", "args": {"num-buffers": 1}}]
    if with_model:
        elements.append({"nodeId": "model_1", "factory": "emltriton", "args": {"model": MODEL}})
    elements.append({"nodeId": None, "factory": "fakesink", "args": {}})
    return {"schemaVersion": 1, "workflowId": WORKFLOW_ID, "workflowVersion": "2", "targetArch": ARCH,
            "segments": [{"name": "s0", "elements": elements}], "executorBindings": [],
            "pluginDependencies": []}


@pytest.fixture
def db(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    path = str(tmp_path / "engine.db")
    engine = create_engine("sqlite:///" + path, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def seed(doc=None, executions=(("exec-1", None),)):
        (artifacts / "compiled_pipeline.json").write_text(json.dumps(doc or document()))
        (artifacts / "manifest.json").write_text(json.dumps({"workflowId": WORKFLOW_ID, "targetArch": ARCH}))
        session = factory()
        try:
            if session.get(WorkflowRegistration, REGISTRATION_ID) is None:
                session.add(WorkflowRegistration(id=REGISTRATION_ID, workflow_id=WORKFLOW_ID, version="2",
                                                 arch=ARCH, artifact_path=str(artifacts),
                                                 status="registered", registered_at=int(time.time())))
            for execution_id, context in executions:
                session.add(WorkflowExecution(
                    id=execution_id, registration_id=REGISTRATION_ID, started_at=int(time.time()),
                    status=EXECUTION_STATUS_PENDING,
                    trigger_context_json=json.dumps(context) if context else None))
            session.commit()
        finally:
            session.close()
        return factory

    seed.factory = factory
    yield seed
    engine.dispose()


def row(factory, execution_id):
    session = factory()
    try:
        execution = session.get(WorkflowExecution, execution_id)
        return execution.status, execution.error
    finally:
        session.close()


class BlockingManager:
    """``run_pipeline`` waits until released (a run on READY models, or one
    whose ``emltriton`` waits on a stuck load)."""

    def __init__(self, raise_on_release=True):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.raise_on_release = raise_on_release

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None, status_sink=None):
        self.calls.append(pipeline_str)
        self.entered.set()
        self.release.wait(10)
        if self.raise_on_release:
            raise RuntimeError("process exited")
        return {}


@pytest.fixture
def gate(monkeypatch):
    from dda_triton import model_readiness

    client = FakeTritonClient(statuses=["READY"])
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: True)
    monkeypatch.setattr(model_readiness, "_client", lambda: client)
    monkeypatch.setattr(model_readiness, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(model_readiness, "_stall_for", lambda target: None)
    return client


def executor(factory, manager=None):
    return WorkflowExecutor(session_factory=factory, pipeline_manager_factory=lambda: manager or BlockingManager())


class PerThreadClient(FakeTritonClient):
    """The Triton state each executor thread sees, by thread name."""

    def __init__(self):
        super().__init__(statuses=["READY"])
        self.by_thread = {}

    def get_model_status(self, model_id):
        return self.by_thread.get(threading.current_thread().name, "READY")


def start(target, *args, name=None):
    thread = threading.Thread(target=target, args=args, daemon=True, name=name)
    thread.start()
    return thread


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_blocked_triggered_and_manual_executions_are_marked_failed(db, activity, gate, monkeypatch):
    from dda_triton import model_readiness

    client = PerThreadClient()
    client.by_thread["exec-gate"] = "LOADING"  # waits in its readiness gate
    monkeypatch.setattr(model_readiness, "_client", lambda: client)
    factory = db(executions=(("exec-gate", None), ("exec-pipe", {"source": "digital-input"})))
    t1 = start(executor(factory).execute, "exec-gate", name="exec-gate")
    assert wait_for(lambda: any(t.in_gate for t in activity.snapshot().tickets))
    # exec-pipe: past its gate, its pipeline waits on the stuck load.
    manager = BlockingManager()
    t2 = start(executor(factory, manager).execute, "exec-pipe", name="exec-pipe")
    assert manager.entered.wait(10)
    assert row(factory, "exec-gate")[0] == EXECUTION_STATUS_PENDING
    assert row(factory, "exec-pipe")[0] == EXECUTION_STATUS_RUNNING
    activity.clock.now += 901  # outside its gate for 900 s with a stalled model
    stalled = (MODEL,)
    assert activity.begin_quiesce()
    assert activity.commit_restart(lambda info: ra.is_blocked(info, stalled), REFUSAL) == "committed"
    report = activity.interrupt_all(ERROR, budget_s=5)
    assert sorted(report.marked) == ["exec-gate", "exec-pipe"] and report.skipped == 0
    assert row(factory, "exec-gate") == (EXECUTION_STATUS_FAILED, ERROR)
    assert row(factory, "exec-pipe") == (EXECUTION_STATUS_FAILED, ERROR)
    client.by_thread["exec-gate"] = "UNAVAILABLE"
    manager.release.set()
    t1.join(10)
    t2.join(10)
    assert activity.snapshot().tickets == (), "every ticket is finished"


def test_a_continuous_execution_registers_no_callback(db, activity, gate):
    factory = db(doc=document(with_model=False),
                 executions=(("exec-c", {"source": CONTINUOUS_SOURCE, "frameSeq": 1}),))
    manager = BlockingManager()
    thread = start(executor(factory, manager).execute, "exec-c")
    assert manager.entered.wait(10)
    infos = activity.snapshot().tickets
    assert len(infos) == 1 and not infos[0].has_callback and infos[0].continuous
    assert ra.running_continuous(activity.snapshot()) == ["wf-1 v2"]
    report = activity.interrupt_all(ERROR, budget_s=5)
    assert report.marked == [] and report.failed == []
    assert row(factory, "exec-c")[0] == EXECUTION_STATUS_RUNNING, "left for the startup reconciliation"
    manager.release.set()
    thread.join(10)


def test_a_completed_row_is_not_changed(db, activity, gate):
    factory = db(doc=document(with_model=False))
    manager = BlockingManager(raise_on_release=False)
    manager.release.set()
    wfe = executor(factory, manager)
    wfe.execute("exec-1")
    assert row(factory, "exec-1")[0] == EXECUTION_STATUS_COMPLETED
    ticket = activity.start_run("execution", "exec-1", on_interrupt=lambda e: wfe._mark_failed_best_effort("exec-1", e),
                                bind=False)
    report = activity.interrupt_all(ERROR, budget_s=5)
    assert report.marked == ["exec-1"]
    assert row(factory, "exec-1") == (EXECUTION_STATUS_COMPLETED, None)
    activity.finish_run(ticket)


def test_an_execution_preparing_its_feed_defers_the_restart(db, activity, gate):
    factory = db(doc=document(with_model=False))
    manager = BlockingManager(raise_on_release=False)
    manager.release.set()
    wfe = executor(factory, manager)
    entered, release = threading.Event(), threading.Event()
    original = wfe._capture_root

    def preparing(registration, trigger_context):
        entered.set()
        release.wait(10)
        return original(registration, trigger_context)

    wfe._capture_root = preparing
    thread = start(wfe.execute, "exec-1")
    assert entered.wait(10)
    infos = activity.snapshot().tickets
    assert len(infos) == 1 and infos[0].workflow == "wf-1 v2" and not infos[0].in_gate
    assert not ra.is_blocked(infos[0], (MODEL,)), "a preparing run is not blocked by the wedge"
    assert activity.begin_quiesce()
    assert activity.commit_restart(lambda info: ra.is_blocked(info, (MODEL,)), REFUSAL) == "runs"
    activity.end_quiesce()
    release.set()
    thread.join(10)


def test_an_execution_during_a_quiesce_is_never_held(db, activity, gate):
    factory = db(doc=document(with_model=False))
    manager = BlockingManager(raise_on_release=False)
    manager.release.set()
    wfe = executor(factory, manager)
    reached = {}
    original = wfe._capture_root

    def feed(registration, trigger_context):
        reached["at"] = time.time()
        reached["counted"] = len(activity.snapshot().tickets)
        return original(registration, trigger_context)

    wfe._capture_root = feed
    assert activity.begin_quiesce()
    started = time.time()
    wfe.execute("exec-1")
    assert reached["at"] - started < 1.0, "it reached its frame feed without waiting"
    assert reached["counted"] == 1, "and it counted as in flight"
    assert row(factory, "exec-1")[0] == EXECUTION_STATUS_COMPLETED
    activity.end_quiesce()


def test_an_execution_after_the_commit_is_refused_before_any_step(db, activity, gate, caplog):
    import logging

    caplog.set_level(logging.INFO)
    factory = db()
    wfe = executor(factory)
    steps = []
    wfe._capture_root = lambda *a: steps.append("capture_root")
    wfe._preflight = lambda *a: steps.append("preflight")
    assert activity.begin_quiesce()
    assert activity.commit_restart(lambda info: True, REFUSAL) == "committed"
    wfe.execute("exec-1")
    assert steps == []
    assert row(factory, "exec-1") == (EXECUTION_STATUS_FAILED, REFUSAL)
    assert "Refusing workflow run exec-1: " + REFUSAL in caplog.text
    assert activity.snapshot().tickets == ()


def test_an_execution_whose_gate_returns_after_the_commit_fails_at_after_gate(db, activity, gate):
    factory = db()
    manager = BlockingManager()
    wfe = executor(factory, manager)
    original = wfe._await_model_readiness
    statuses = []

    def gate_then_commit(document, **kwargs):
        # ``on_outcome`` is the telemetry hooks' optional per-model observer
        # (workflow-telemetry-dashboards design §5.2); forwarded unchanged.
        result = original(document, **kwargs)
        assert activity.begin_quiesce()
        assert activity.commit_restart(lambda info: True, REFUSAL) == "committed"
        statuses.append(row(factory, "exec-1")[0])
        return result

    wfe._await_model_readiness = gate_then_commit
    wfe.execute("exec-1")
    assert row(factory, "exec-1") == (EXECUTION_STATUS_FAILED, REFUSAL)
    assert statuses == [EXECUTION_STATUS_PENDING], "the row never became running"
    assert manager.calls == [], "the pipeline never started"


def test_await_model_readiness_keeps_its_signature_and_behavior(gate):
    # ``document`` plus the telemetry hooks' optional ``on_outcome`` observer
    # (default None; workflow-telemetry-dashboards design §5.2). The
    # one-argument calls below are the gate's behaviour without it.
    assert list(inspect.signature(WorkflowExecutor._await_model_readiness).parameters) == [
        "document", "on_outcome"]
    assert inspect.signature(WorkflowExecutor._await_model_readiness).parameters["on_outcome"].default is None
    assert WorkflowExecutor._await_model_readiness(document(with_model=False)) is None
    assert WorkflowExecutor._await_model_readiness(document()) is None
    gate.push_statuses(["UNAVAILABLE"])
    message = WorkflowExecutor._await_model_readiness(document())
    assert "Model 'model-a'" in message and "UNAVAILABLE" in message
    assert WorkflowExecutor._document_models(document()) == [MODEL]
