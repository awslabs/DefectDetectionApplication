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
"""Tests for the executor's per-run capture-output routing (Task 2).

deployed-workflow-run-observability Requirement 1: ``_route_capture_outputs``
populates a terminal ``emlcapture`` element's ``meta`` with the
``triton_inference_output_*`` routing string for the outputs the deployed
model declares, targeting the per-run ``{output_dir}/{capture_id}`` location;
non-capture documents are left untouched; ``execute`` records
``has_image_results``/``output_dir``/``capture_id`` when a capture document
runs, and the additive tag values are unchanged (Requirements 1.1-1.6, 8.5).

Follows the ``test_workflow_pipeline_executor.py`` fixture style: a mocked
pipeline manager and a temporary Triton model repo (config.pbtxt only), so no
GStreamer, no embedded Triton, and no real ``/aws_dda`` tree are needed.
"""
import copy
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine import pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

# --- documents ---------------------------------------------------------------

#: A File_Output_Node document: the terminal element of its only segment is
#: ``emlcapture`` (what the portal capture output node compiles to), preceded
#: by a model-inference element whose config.pbtxt declares the capture
#: outputs.
CAPTURE_DOC = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                {"nodeId": "n2", "factory": "emltriton",
                 "args": {"model": "model-widget"}},
                {"nodeId": "n3", "factory": "jpegenc", "args": {}},
                {"nodeId": "n4", "factory": "emlcapture", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}

#: A non-capture document: terminal element is a real sink, so it is not a
#: File_Output_Node run (R1.5).
NON_CAPTURE_DOC = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                {"nodeId": "n2", "factory": "emltriton",
                 "args": {"model": "model-widget"}},
                {"nodeId": "n3", "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}

ALL_OUTPUTS = (
    "output_overlay",
    "output_mask",
    "output_capture",
    "output_anomalous",
    "output_confidence",
)


# --- fixtures / helpers ------------------------------------------------------


class FakePipelineManager:
    """Mocked GstPipelineManager: records the launch string, returns tags."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


def write_model_repo(root, model_name="model-widget", outputs=ALL_OUTPUTS):
    """A minimal Triton model repo declaring ``outputs`` for ``model_name``.

    Mirrors the ``input {}`` message form the existing metadata reader
    assumes; only the ``output {}`` blocks the routing enumerates matter."""
    model_dir = os.path.join(str(root), model_name)
    os.makedirs(model_dir, exist_ok=True)
    lines = [
        'name: "{0}"'.format(model_name),
        'platform: "ensemble"',
        'max_batch_size: 0',
        'input {',
        '  name: "INPUT"',
        '  data_type: TYPE_UINT8',
        '  dims: [ -1 ]',
        '}',
    ]
    for name in outputs:
        lines += [
            'output {',
            '  name: "{0}"'.format(name),
            '  data_type: TYPE_UINT8',
            '  dims: [ -1 ]',
            '}',
        ]
    with open(os.path.join(model_dir, "config.pbtxt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return str(root)


def seed_run(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id="wf-1:3",
            workflow_id="wf-1",
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(artifact_path),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id="exec-1",
            registration_id="wf-1:3",
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return "exec-1"


def get_execution(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def terminal_emlcapture(document):
    return document["segments"][0]["elements"][-1]


# --- _route_capture_outputs (direct) -----------------------------------------


class TestRouteCaptureOutputs:
    def test_populates_meta_for_capture_terminal_with_all_outputs(self, tmp_path):
        document = copy.deepcopy(CAPTURE_DOC)
        repo = write_model_repo(tmp_path)
        # The broker appends {capture_id}.{ext}, so the routed prefix is the
        # per-run output_dir folder itself (NOT output_dir/capture_id).
        prefix = "/aws_dda/captures/wf-1/exec-1"

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            routed = WorkflowExecutor._route_capture_outputs(
                document,
                "/aws_dda/captures/wf-1/exec-1",
                "wf-1-exec-1",
            )

        assert routed is True
        args = terminal_emlcapture(document)["args"]
        # Base captured frame is aimed at the per-run dir via buffer-message-id.
        assert args["buffer-message-id"] == "file-target_{0}-jpg".format(prefix)
        meta = args["meta"]
        assert meta == (
            "triton_inference_output_overlay:"
            "file-target_{p}-overlay.jpg,"
            "triton_inference_output_mask:file-target_{p}-mask.png,"
            "triton_inference_output_capture:file-target_{p}-jsonl,"
            "triton_inference_output_anomalous:{p}_is-anomalous,"
            "triton_inference_output_confidence:{p}_confidence"
        ).format(p=prefix)

    def test_only_declared_outputs_are_targeted(self, tmp_path):
        # The model declares only overlay + capture; the other three targets
        # must be absent (R1.3, R1.5).
        document = copy.deepcopy(CAPTURE_DOC)
        repo = write_model_repo(
            tmp_path, outputs=("output_overlay", "output_capture")
        )

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        meta = terminal_emlcapture(document)["args"]["meta"]
        assert "triton_inference_output_overlay:" in meta
        assert "triton_inference_output_capture:" in meta
        assert "triton_inference_output_mask" not in meta
        assert "triton_inference_output_anomalous" not in meta
        assert "triton_inference_output_confidence" not in meta

    def test_no_declared_outputs_leaves_meta_unset_but_is_capture(self, tmp_path):
        # A model whose config declares no capture outputs (or the fixture
        # repos in tests: no config at all) gets no meta, but the document is
        # still a File_Output_Node run.
        document = copy.deepcopy(CAPTURE_DOC)
        repo = write_model_repo(tmp_path, outputs=())

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            routed = WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        assert routed is True
        assert "meta" not in terminal_emlcapture(document)["args"]

    def test_missing_model_repo_leaves_meta_unset(self, tmp_path):
        document = copy.deepcopy(CAPTURE_DOC)
        missing = os.path.join(str(tmp_path), "does-not-exist")

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", missing):
            routed = WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        assert routed is True
        assert "meta" not in terminal_emlcapture(document)["args"]

    def test_non_capture_document_returns_false_and_is_untouched(self, tmp_path):
        document = copy.deepcopy(NON_CAPTURE_DOC)
        before = copy.deepcopy(document)
        repo = write_model_repo(tmp_path)

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            routed = WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        assert routed is False
        assert document == before

    def test_existing_meta_is_not_overwritten(self, tmp_path):
        document = copy.deepcopy(CAPTURE_DOC)
        terminal_emlcapture(document)["args"]["meta"] = "author-supplied"
        repo = write_model_repo(tmp_path)

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        assert terminal_emlcapture(document)["args"]["meta"] == "author-supplied"

    def test_capture_meta_placeholder_is_replaced(self, tmp_path):
        document = copy.deepcopy(CAPTURE_DOC)
        terminal_emlcapture(document)["args"]["meta"] = "{capture_meta}"
        repo = write_model_repo(tmp_path)

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            WorkflowExecutor._route_capture_outputs(
                document, "/out/dir", "cap-1"
            )

        meta = terminal_emlcapture(document)["args"]["meta"]
        assert meta != "{capture_meta}"
        assert "triton_inference_output_overlay:" in meta


# --- execute() integration ---------------------------------------------------


class TestExecuteRecordsArtifactLocation:
    def test_capture_run_sets_has_image_results_and_location(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=CAPTURE_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})

        capture_root = os.path.join(str(tmp_path), "captures")
        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: manager,
            ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert bool(row.has_image_results) is True
        assert row.output_dir == os.path.join(capture_root, "wf-1", execution_id)
        assert row.capture_id == "wf-1-{0}".format(execution_id)
        # The per-run directory is created best-effort before the run.
        assert os.path.isdir(row.output_dir)

    def test_non_capture_run_leaves_has_image_results_false(
        self, tmp_path, session_factory
    ):
        # vlm-parity-run-results Requirement 2.3: output_dir/capture_id
        # are now ALWAYS recorded (so metadata JSON and inference-node
        # frames have a destination even without a File_Output
        # terminal); has_image_results stays false for a non-capture
        # run without persisted node frames.
        artifact_path = write_artifact_set(tmp_path, compiled=NON_CAPTURE_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        capture_root = os.path.join(str(tmp_path), "captures")
        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: manager,
            ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert bool(row.has_image_results) is False
        assert row.output_dir == os.path.join(
            capture_root, "wf-1", execution_id)
        assert row.capture_id == "wf-1-{0}".format(execution_id)

    def test_tag_values_unchanged_by_routing(self, tmp_path, session_factory):
        # R1.6: routing is additive — the tag values the run produces are
        # untouched by capture-output routing.
        artifact_path = write_artifact_set(tmp_path, compiled=CAPTURE_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(
            tag_values={"is_anomalous": False, "confidence": 0.42}
        )
        received = []

        capture_root = os.path.join(str(tmp_path), "captures")
        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: manager,
                post_run_handler=lambda reg, doc, tags: received.append(tags),
            ).execute(execution_id)

        # `trigger: {}` is the seeded trigger-less-run delta
        # (custom-python-source Requirements 2.5, 11.1).
        assert received == [
            {"is_anomalous": False, "confidence": 0.42, "trigger": {}}
        ]

    def test_routing_failure_is_contained(self, tmp_path, session_factory):
        # R8.5: a failure in capture routing never fails the run.
        artifact_path = write_artifact_set(tmp_path, compiled=CAPTURE_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})

        capture_root = os.path.join(str(tmp_path), "captures")
        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ), patch.object(
            WorkflowExecutor,
            "_route_capture_outputs",
            side_effect=RuntimeError("routing exploded"),
        ):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: manager,
            ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert bool(row.has_image_results) is False
