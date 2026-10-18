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
"""The executor's telemetry hook points (workflow-telemetry-dashboards
design §5.2 "Hook points"; workflow-debug-mode tasks.md 1.6).

Built like ``test_cold_model_engine_exploration.py``: a registration and a
pending execution in an in-memory database, a fake pipeline manager, the
artifact root in ``tmp_path``. ``WorkflowExecutor.execute`` must hand one
``RunRecord`` to the run observers on every terminal path once the
registration is loaded: the timing stamps, the capped detections, the
traceback of a caught failure, the fresh-session read with its fallback, and
the ``CURRENT_EXECUTION`` context variable set during the run and reset after.

Validates: dashboards R2.2, R2.5, R5.2; debug R4.1, R13.1.
"""
import json
import time
from types import SimpleNamespace

import pytest

from workflow_engine_test_utils import DEVICE_ARCH, make_session_factory

from workflow_engine import pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.telemetry import hooks

WORKFLOW_ID = "wf-hooks"
REGISTRATION_ID = "wf-hooks:1"
EXECUTION_ID = "exec-hooks"
MODEL = "plates-detector"


@pytest.fixture(autouse=True)
def clean_registries():
    hooks._clear_observers()
    yield
    hooks._clear_observers()


class FakePipelineManager:
    def __init__(self, tag_values=None, raise_with=None, on_run=None):
        self.tag_values = tag_values or {}
        self.raise_with = raise_with
        self.on_run = on_run
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        if self.on_run is not None:
            self.on_run()
        if self.raise_with is not None:
            raise self.raise_with
        return dict(self.tag_values)


def plain_document():
    return {
        "schemaVersion": 1, "workflowId": WORKFLOW_ID, "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": [
            {"nodeId": "n1", "factory": "videotestsrc", "args": {"num-buffers": 1}},
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ]}],
        "executorBindings": [], "pluginDependencies": [],
    }


def triton_document():
    document = plain_document()
    document["segments"][0]["elements"].insert(
        1, {"nodeId": "model_1", "factory": "emltriton", "args": {"model": MODEL}})
    return document


@pytest.fixture
def world(tmp_path):
    """Seeds a registration + pending execution; returns a helper that runs
    the executor and the artifact root."""
    artifact_path = tmp_path / "artifacts"
    artifact_path.mkdir()
    capture_root = tmp_path / "captures"
    capture_root.mkdir()
    state = {}

    def seed(document, *, status="registered", enqueued_at=None, registration_id=REGISTRATION_ID,
             trigger_context=None):
        (artifact_path / "compiled_pipeline.json").write_text(json.dumps(document))
        (artifact_path / "manifest.json").write_text(json.dumps(
            {"workflowId": WORKFLOW_ID, "targetArch": DEVICE_ARCH}))
        session_factory = make_session_factory()
        session = session_factory()
        try:
            session.add(WorkflowRegistration(
                id=REGISTRATION_ID, workflow_id=WORKFLOW_ID, version="1", arch=DEVICE_ARCH,
                artifact_path=str(artifact_path), status=status, registered_at=int(time.time()),
            ))
            session.add(WorkflowExecution(
                id=EXECUTION_ID, registration_id=registration_id,
                started_at=enqueued_at if enqueued_at is not None else int(time.time()),
                status=EXECUTION_STATUS_PENDING,
                trigger_context_json=json.dumps(trigger_context) if trigger_context else None,
            ))
            session.commit()
        finally:
            session.close()
        state["session_factory"] = session_factory
        return session_factory

    def run(manager, session_factory=None):
        session_factory = session_factory or state["session_factory"]
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            capture_root_for=lambda registration, context: str(capture_root),
        ).execute(EXECUTION_ID)
        session = session_factory()
        try:
            return session.get(WorkflowExecution, EXECUTION_ID)
        finally:
            session.close()

    return SimpleNamespace(seed=seed, run=run, capture_root=capture_root,
                           artifact_path=artifact_path)


def _collect_records():
    records = []
    hooks.register_run_observer(records.append)
    return records


def _write_sidecar(capture_root, count):
    output_dir = capture_root / WORKFLOW_ID / EXECUTION_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_id = "{0}-{1}".format(WORKFLOW_ID, EXECUTION_ID)
    detections = {
        str(i): {"class_label": "person" if i % 2 else "vest", "confidence": 0.5 + (i % 50) / 100.0,
                 "bounding_box": [i, i, i + 10, i + 20]}
        for i in range(count)
    }
    (output_dir / (capture_id + ".detections.json")).write_text(json.dumps({"detections": detections}))
    return output_dir, capture_id


class TestCompletedRun:
    def test_a_run_with_1200_detections_produces_the_expected_record(self, world):
        world.seed(plain_document())
        output_dir, capture_id = _write_sidecar(world.capture_root, 1200)
        records = _collect_records()
        before_ms = int(time.time() * 1000)

        row = world.run(FakePipelineManager(tag_values={"is_anomalous": False}))

        assert row.status == EXECUTION_STATUS_COMPLETED
        assert len(records) == 1
        record = records[0]
        assert record.registration_id == REGISTRATION_ID
        assert record.workflow_id == WORKFLOW_ID
        assert record.workflow_version == "1"
        assert record.execution_id == EXECUTION_ID
        assert record.status == "completed"
        assert record.error is None and record.failing_node_id is None
        assert record.traceback is None
        assert record.capture_id == capture_id
        assert record.output_dir == str(output_dir)
        assert record.log_path == row.log_path and record.log_path
        assert record.enqueued_at_s == row.started_at or record.enqueued_at_s <= row.started_at
        assert before_ms <= record.accepted_ms <= record.run_started_ms <= record.finished_ms
        assert record.queued_ms < 2000
        assert record.setup_ms == record.run_started_ms - record.accepted_ms
        assert record.readiness_wait_ms == 0
        assert record.trigger_context == {}
        assert record.continuous is False
        assert isinstance(record.node_status, dict) and "n1" in record.node_status
        assert record.tag_values["is_anomalous"] is False
        assert record.tag_values["trigger"] == {}
        assert len(record.detections) == 1000
        assert record.detection_count == 1200
        assert {"label", "confidence", "x_min", "y_min", "x_max", "y_max", "id"} <= set(
            record.detections[0])

    def test_queued_ms_reflects_the_enqueue_time(self, world):
        world.seed(plain_document(), enqueued_at=int(time.time()) - 3)
        records = _collect_records()
        world.run(FakePipelineManager())
        assert 2000 <= records[0].queued_ms <= 4500  # one-second enqueue resolution

    def test_trigger_context_and_continuous_flag_travel(self, world):
        context = {"source": "continuous", "frameSeq": 7, "frameAcquiredAtMs": 1, "tickAtMs": 2}
        world.seed(plain_document(), trigger_context=context)
        records = _collect_records()
        world.run(FakePipelineManager())
        assert records[0].trigger_context == context
        assert records[0].continuous is True


class TestReadinessGate:
    def test_readiness_wait_is_reported_and_excluded_from_the_latency(self, world, monkeypatch):
        import dda_triton.model_readiness as readiness

        world.seed(triton_document())
        records = _collect_records()
        model_events = []
        hooks.register_model_observer(lambda *a: model_events.append(a))

        def holding_gate(model_name, resolved_name=None, **kwargs):
            time.sleep(0.25)  # the gate holds the run
            return readiness.ReadinessOutcome(ready=True, state="READY", waited_seconds=2.0)

        monkeypatch.setattr(readiness, "ensure_model_ready", holding_gate)
        manager = FakePipelineManager()
        row = world.run(manager)

        assert row.status == EXECUTION_STATUS_COMPLETED
        assert len(manager.calls) == 1
        record = records[0]
        assert record.readiness_wait_ms == 2000
        assert record.setup_ms >= 250  # includes the gate's hold
        assert record.finished_ms - record.run_started_ms < 250  # the latency excludes it
        readiness_events = [e for e in model_events if e[0] == "readiness"]
        assert len(readiness_events) == 1
        _, registration_id, execution_id, model, state, waited_ms, ready = readiness_events[0]
        assert (registration_id, execution_id, state, waited_ms, ready) == (
            REGISTRATION_ID, EXECUTION_ID, "READY", 2000, True)
        assert MODEL in model

    def test_a_refused_model_reports_not_ready_and_no_run_start(self, world, monkeypatch):
        import dda_triton.model_readiness as readiness

        world.seed(triton_document())
        records = _collect_records()
        model_events = []
        hooks.register_model_observer(lambda *a: model_events.append(a))
        monkeypatch.setattr(readiness, "ensure_model_ready", lambda *a, **k: readiness.ReadinessOutcome(
            ready=False, state="LOADING", message="model is LOADING", waited_seconds=1.5))
        manager = FakePipelineManager()
        row = world.run(manager)

        assert manager.calls == []
        assert row.status == EXECUTION_STATUS_FAILED
        record = records[0]
        assert record.status == "failed"
        assert record.run_started_ms is None and record.setup_ms is None
        assert record.readiness_wait_ms == 1500
        assert record.traceback is None
        assert model_events[0][0] == "readiness"
        assert model_events[0][4:] == ("LOADING", 1500, False)

    def test_a_raising_outcome_hook_never_changes_the_gate_decision(self, monkeypatch):
        import dda_triton.model_readiness as readiness

        monkeypatch.setattr(readiness, "ensure_model_ready", lambda *a, **k: readiness.ReadinessOutcome(
            ready=False, state="LOADING", message="still loading"))

        def boom(model, outcome):
            raise RuntimeError("hook boom")

        assert WorkflowExecutor._await_model_readiness(triton_document(), on_outcome=boom) == "still loading"
        assert WorkflowExecutor._await_model_readiness(plain_document(), on_outcome=boom) is None


class TestFailurePaths:
    def test_preflight_refusal_reports_failed_without_start_or_traceback(self, world):
        world.seed(plain_document(), status="invalid")
        records = _collect_records()
        row = world.run(FakePipelineManager())
        assert row.status == EXECUTION_STATUS_FAILED
        record = records[0]
        assert record.status == "failed"
        assert record.run_started_ms is None
        assert record.setup_ms is None
        assert record.traceback is None
        assert record.error and "cannot be run" in record.error
        assert record.accepted_ms is not None
        assert record.capture_id is None and record.log_path is None

    def test_pipeline_exception_carries_the_traceback(self, world):
        world.seed(plain_document())
        records = _collect_records()
        row = world.run(FakePipelineManager(raise_with=RuntimeError("pipeline boom")))
        assert row.status == EXECUTION_STATUS_FAILED
        record = records[0]
        assert record.status == "failed"
        assert record.error == "pipeline boom"
        assert record.traceback is not None
        assert "Traceback (most recent call last)" in record.traceback
        assert "pipeline boom" in record.traceback
        assert len(record.traceback) <= 8192
        assert record.run_started_ms is not None

    def test_registration_missing_means_no_record(self, world):
        world.seed(plain_document(), registration_id="gone:9")
        records = _collect_records()
        row = world.run(FakePipelineManager())
        assert row.status == EXECUTION_STATUS_FAILED
        assert records == []

    def test_non_pending_execution_means_no_record(self, world):
        session_factory = world.seed(plain_document())
        session = session_factory()
        try:
            session.get(WorkflowExecution, EXECUTION_ID).status = EXECUTION_STATUS_COMPLETED
            session.commit()
        finally:
            session.close()
        records = _collect_records()
        world.run(FakePipelineManager())
        assert records == []

    def test_a_raising_observer_never_fails_the_run(self, world):
        world.seed(plain_document())

        def boom(record):
            raise RuntimeError("observer boom")

        hooks.register_run_observer(boom)
        row = world.run(FakePipelineManager())
        assert row.status == EXECUTION_STATUS_COMPLETED


class TestFreshSession:
    def test_terminal_row_is_read_through_a_fresh_session(self, world):
        session_factory = world.seed(plain_document())
        calls = []

        def counting_factory():
            calls.append(1)
            return session_factory()

        records = _collect_records()
        world.run(FakePipelineManager(), session_factory=counting_factory)
        assert len(calls) >= 2  # the run's own session plus the fresh terminal read
        assert records[0].status == "completed"

    def test_fresh_read_falls_back_to_the_in_memory_row(self, world):
        session_factory = world.seed(plain_document())
        calls = []

        def failing_second_factory():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("database locked")
            return session_factory()

        records = _collect_records()
        row = world.run(FakePipelineManager(tag_values={"k": "v"}), session_factory=failing_second_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert len(records) == 1
        assert records[0].status == "completed"
        assert records[0].execution_id == EXECUTION_ID
        assert records[0].tag_values["k"] == "v"


class TestContextVar:
    def test_current_execution_is_set_during_the_run_and_reset_after(self, world):
        world.seed(plain_document())
        seen = []
        manager = FakePipelineManager(on_run=lambda: seen.append(hooks.CURRENT_EXECUTION.get()))
        assert hooks.CURRENT_EXECUTION.get() is None
        world.run(manager)
        assert seen == [(REGISTRATION_ID, WORKFLOW_ID, EXECUTION_ID)]
        assert hooks.CURRENT_EXECUTION.get() is None

    def test_current_execution_is_reset_after_a_failure(self, world):
        world.seed(plain_document())
        world.run(FakePipelineManager(raise_with=RuntimeError("boom")))
        assert hooks.CURRENT_EXECUTION.get() is None

    def test_registration_identity_is_a_frozen_snapshot(self):
        identity = pipeline_executor._RegistrationIdentity("r", "w", "3")
        with pytest.raises(Exception):
            identity.id = "x"  # type: ignore[misc]
