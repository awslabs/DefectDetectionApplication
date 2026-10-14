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
"""Exploration + preservation for the cold-model window on the DEPLOYED
WORKFLOW ENGINE path.

Bugfix: `.kiro/specs/cold-model-first-run-failure/`, tasks 1.2 and 1.4.

The cold-model tests assert the POST-FIX expectations and therefore MUST FAIL
on unfixed code, where the execution reaches `run_pipeline` and records the
generic `"Pipeline failed to change state to PLAYING, check logs above this."`
The warm tests are preservation and MUST PASS on unfixed code.

Measured origin (bugfix.md Defect 4): on jetson-thor1, 2026-09-22, of twelve
executions of one workflow the single failure was the FIRST after a backend
container restart. Triton load state lives only in the backend process, and
`emltriton`'s `Initialize()` enqueues the load then checks readiness on the
very next line, so the first run of any process races an in-flight load.

_Requirements: 1.13-1.18, 2.11, 2.13, Property 3, Property 4_
"""
import json
import time

import pytest

# First: sets COMPONENT_WORK_PATH, which dao.sqlite_db.sqlite_db_operations
# reads at module import time to build its SQLite URL.
from workflow_engine_test_utils import (  # noqa: I001
    DEVICE_ARCH,
    make_session_factory,
)

from readiness_fakes import (
    STATE_LOADING,
    STATE_READY,
    STATE_UNAVAILABLE,
    FakeTritonClient,
)
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)


WORKFLOW_ID = "wf-cold"
EXECUTION_ID = "exec-cold"
REGISTRATION_ID = "wf-cold:1"
WORKFLOW_MODEL = "blue-plate-rfdetr-small"


# ---------------------------------------------------------------- helpers

class FakePipelineManager:
    """Records every run_pipeline call, so "the gate ran before the
    pipeline" and "the pipeline was never started" are both assertable."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        self.calls.append(pipeline_str)
        return dict(self.tag_values)


def triton_document(model=WORKFLOW_MODEL):
    """A compiled document with one `emltriton` element — the shape whose
    model name `_resolve_model_names` rewrites and whose readiness the gate
    must check."""
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "1",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "videotestsrc",
                     "args": {"num-buffers": 1}},
                    {"nodeId": "model_1", "factory": "emltriton",
                     "args": {"model": model}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def tritonless_document():
    """No `emltriton` element: the gate must not consult Triton at all."""
    document = triton_document()
    document["segments"][0]["elements"] = [
        {"nodeId": "n1", "factory": "videotestsrc",
         "args": {"num-buffers": 1}},
        {"nodeId": None, "factory": "fakesink", "args": {}},
    ]
    return document


@pytest.fixture
def seeded(tmp_path):
    """A registration + pending execution pair with a compiled document on
    disk, ready for `execute()`."""
    artifact_path = tmp_path / "artifacts"
    artifact_path.mkdir()

    def seed(document):
        (artifact_path / "compiled_pipeline.json").write_text(
            json.dumps(document)
        )
        (artifact_path / "manifest.json").write_text(
            json.dumps({"workflowId": WORKFLOW_ID, "targetArch": DEVICE_ARCH})
        )
        session_factory = make_session_factory()
        session = session_factory()
        try:
            session.add(WorkflowRegistration(
                id=REGISTRATION_ID,
                workflow_id=WORKFLOW_ID,
                version="1",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status="registered",
                registered_at=int(time.time()),
            ))
            session.add(WorkflowExecution(
                id=EXECUTION_ID,
                registration_id=REGISTRATION_ID,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            ))
            session.commit()
        finally:
            session.close()
        return session_factory

    return seed


def run_execution(session_factory, manager, client, monkeypatch):
    """Execute with the readiness gate pointed at `client`."""
    import dda_triton.model_readiness as readiness

    monkeypatch.setattr(readiness, "_repo_has_models", lambda: True)
    monkeypatch.setattr(readiness, "_client", lambda: client)
    # No real waiting in tests.
    monkeypatch.setattr(readiness, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(readiness, "READY_TIMEOUT_S", 0.0)

    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
    ).execute(EXECUTION_ID)

    session = session_factory()
    try:
        return session.get(WorkflowExecution, EXECUTION_ID)
    finally:
        session.close()


# ------------------------------------------- cold model: MUST FAIL unfixed

def test_a_loading_model_never_starts_the_pipeline(seeded, monkeypatch):
    """The core fix: a not-yet-READY model fails the execution BEFORE the
    pipeline is started, so the operator is not told to check the pipeline.

    On unfixed code `run_pipeline` IS called and the recorded error is
    "Pipeline failed to change state to PLAYING, check logs above this."

    _Requirements: 2.11, Property 3_
    """
    session_factory = seeded(triton_document())
    manager = FakePipelineManager()
    client = FakeTritonClient([STATE_LOADING])

    row = run_execution(session_factory, manager, client, monkeypatch)

    assert manager.calls == [], (
        "the pipeline was started for a model that is not READY; the run "
        "then fails inside emltriton with a message naming neither the "
        "model nor its state"
    )
    assert row.status == EXECUTION_STATUS_FAILED
    assert "Pipeline failed to change state to PLAYING" not in (row.error or "")


def test_the_failure_names_the_model_and_its_state(seeded, monkeypatch):
    """Requirement 2.11(b) / defect 1.18: the error must identify the model
    and the state, which the generic GStreamer message did not."""
    session_factory = seeded(triton_document())
    client = FakeTritonClient([STATE_LOADING])

    row = run_execution(
        session_factory, FakePipelineManager(), client, monkeypatch
    )

    error = row.error or ""
    assert WORKFLOW_MODEL in error, error
    assert STATE_LOADING in error, error


def test_an_unavailable_model_fails_without_waiting(seeded, monkeypatch):
    """A terminal load failure is reported as such, with Triton's reason."""
    session_factory = seeded(triton_document())
    client = FakeTritonClient([STATE_UNAVAILABLE])
    client.list_triton_models = lambda: [
        {"model_component": "model-blue-plate-rfdetr-small-"
                            + DEVICE_ARCH.replace("_", "-"),
         "status": STATE_UNAVAILABLE, "reason": "opset unsupported"}
    ]
    manager = FakePipelineManager()

    row = run_execution(session_factory, manager, client, monkeypatch)

    assert manager.calls == []
    assert row.status == EXECUTION_STATUS_FAILED
    assert STATE_UNAVAILABLE in (row.error or "")


def test_the_execution_row_is_never_left_stuck(seeded, monkeypatch):
    """Requirement 2.13: whatever the gate decides, the row reaches a
    terminal state — never left `running` or `pending`."""
    session_factory = seeded(triton_document())
    client = FakeTritonClient([STATE_LOADING])

    row = run_execution(
        session_factory, FakePipelineManager(), client, monkeypatch
    )

    assert row.status == EXECUTION_STATUS_FAILED
    assert row.finished_at is not None


# ----------------------------------------- warm model: MUST PASS unfixed

def test_a_ready_model_runs_the_pipeline_exactly_as_before(seeded,
                                                          monkeypatch):
    """Preservation: the warm path is byte-identical — same single
    `run_pipeline` call, same terminal status, no added latency.

    _Requirements: Property 4_
    """
    session_factory = seeded(triton_document())
    manager = FakePipelineManager(tag_values={"is_anomalous": False})
    client = FakeTritonClient([STATE_READY])

    row = run_execution(session_factory, manager, client, monkeypatch)

    assert len(manager.calls) == 1, (
        f"expected exactly one pipeline run, got {len(manager.calls)}"
    )
    assert "emltriton" in manager.calls[0]
    assert row.status != EXECUTION_STATUS_FAILED, row.error


def test_a_document_without_a_triton_element_never_consults_triton(
    seeded, monkeypatch
):
    """Non-Triton workflows pay nothing and must not be able to fail on a
    Triton state read. _Requirements: Property 4_"""
    session_factory = seeded(tritonless_document())
    manager = FakePipelineManager()
    client = FakeTritonClient([STATE_LOADING])

    row = run_execution(session_factory, manager, client, monkeypatch)

    assert client.poll_count == 0, (
        "a workflow with no emltriton element consulted Triton state"
    )
    assert len(manager.calls) == 1
    assert row.status != EXECUTION_STATUS_FAILED, row.error
