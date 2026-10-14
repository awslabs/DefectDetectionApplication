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
"""Tests for per-execution run-log capture (Requirements 2.1-2.6, 8.5).

Exercises ``RunLogCapture`` in isolation (size bound, no-touch on existing
logging, contained handler errors) and end to end through the
WorkflowExecutor (the run's launch string, a resolution message, and the
terminal outcome land in the captured file for both success and failure),
following the mocked-manager / temp-dir fixture style of
``test_workflow_pipeline_executor.py``.
"""
import logging
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.run_log import RunLogCapture

# A document whose terminal is a plain fakesink (no artifact routing), with
# an emltriton node so a failure maps to node "n2".
COMPILED_DOC = {
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
                 "args": {"model": "widget-anomaly-v3"}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}


class FakePipelineManager:
    """Mocked GstPipelineManager: returns tags or raises a mapped error."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


def seed_run(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id="wf-1:3",
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status="registered",
                registered_at=int(time.time()),
            )
        )
        session.add(
            WorkflowExecution(
                id="exec-1",
                registration_id="wf-1:3",
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            )
        )
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


def make_executor(session_factory, manager):
    return WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
    )


def folder_source_doc(folder):
    """A capture-free document whose filesrc points at a directory, so the
    executor logs a frame-source resolution message during the run."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "filesrc",
                     "args": {"location": str(folder)}},
                    {"nodeId": "n2", "factory": "jpegdec", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def read_log(log_root):
    path = os.path.join(str(log_root), "wf-1", "exec-1", "run.log")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestExecutorRunLogCapture:
    def test_success_log_has_launch_resolution_and_tags(
        self, tmp_path, session_factory
    ):
        # A folder source so a resolution message is logged; a jpeg inside
        # so the source resolves cleanly.
        folder = tmp_path / "frames"
        folder.mkdir()
        (folder / "frame.jpg").write_bytes(b"not-a-real-jpeg")
        artifact_path = write_artifact_set(
            tmp_path, compiled=folder_source_doc(folder)
        )
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})
        log_root = tmp_path / "captures"

        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(log_root)
        ):
            make_executor(session_factory, manager).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.log_path == os.path.join(
            str(log_root), "wf-1", "exec-1", "run.log"
        )
        contents = read_log(log_root)
        # Launch string (R2.2)
        assert "starting pipeline:" in contents
        assert "jpegdec ! fakesink" in contents
        # A resolution message (R2.2)
        assert "Resolved workflow frame source" in contents
        # Terminal outcome with tag values (R2.2)
        assert "completed" in contents
        assert "is_anomalous" in contents

    def test_failure_log_has_failing_node_and_error(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        error = RuntimeError(
            "Pipeline failed with: Could not reach Triton. "
            "gst_emltriton_start (): "
            "/GstPipeline:pipeline0/GstEmltriton:emltriton0:"
        )
        manager = FakePipelineManager(error=error)
        log_root = tmp_path / "captures"

        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(log_root)
        ):
            make_executor(session_factory, manager).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        contents = read_log(log_root)
        # Failing node + underlying error (R2.2, R2.3)
        assert "node n2" in contents
        assert "Could not reach Triton" in contents

    def test_existing_logging_is_untouched_by_a_run(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": False})
        log_root = tmp_path / "captures"

        root = logging.getLogger()
        we = logging.getLogger("workflow_engine")
        gst = logging.getLogger("gstreamer")
        before_root_handlers = list(root.handlers)
        before_root_level = root.level
        before_we_level = we.level
        before_gst_level = gst.level
        before_we_handlers = list(we.handlers)
        before_gst_handlers = list(gst.handlers)

        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(log_root)
        ):
            make_executor(session_factory, manager).execute(execution_id)

        # Root untouched: same handlers, same level (R2.5).
        assert list(root.handlers) == before_root_handlers
        assert root.level == before_root_level
        # Named loggers: levels restored and no leftover capture handler.
        assert we.level == before_we_level
        assert gst.level == before_gst_level
        assert list(we.handlers) == before_we_handlers
        assert list(gst.handlers) == before_gst_handlers

    def test_forced_handler_error_does_not_fail_the_run(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=COMPILED_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": True})
        log_root = tmp_path / "captures"

        def boom(*args, **kwargs):
            raise OSError("cannot open log file")

        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(log_root)
        ), patch("workflow_engine.run_log.RotatingFileHandler", boom):
            make_executor(session_factory, manager).execute(execution_id)

        # The run still reaches a terminal status despite capture failing
        # (R2.6, R8.5), and no capture handler is left attached.
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED
        assert not any(
            isinstance(h, logging.FileHandler)
            for h in logging.getLogger("workflow_engine").handlers
        )


class TestRunLogCaptureUnit:
    def test_file_is_size_bounded(self, tmp_path):
        log_path = str(tmp_path / "run.log")
        capture = RunLogCapture(
            "exec-x", log_path, max_bytes=2048, backup_count=1
        )
        lg = logging.getLogger("workflow_engine")
        with capture:
            for i in range(2000):
                lg.info("log line %d with some padding to take up bytes", i)

        # Live file is bounded by max_bytes (RotatingFileHandler rolls
        # before a record would exceed it); the rolled backup is bounded
        # too, so total on-disk is bounded regardless of volume (R2.4).
        assert os.path.getsize(log_path) <= 2048 + 512
        backup = log_path + ".1"
        total = os.path.getsize(log_path)
        if os.path.exists(backup):
            total += os.path.getsize(backup)
        assert total <= 2 * 2048 + 512
        # It actually captured (and rotated) rather than dropping everything.
        assert os.path.getsize(log_path) > 0

    def test_start_stop_attaches_then_detaches_without_touching_root(
        self, tmp_path
    ):
        log_path = str(tmp_path / "run.log")
        root = logging.getLogger()
        we = logging.getLogger("workflow_engine")
        before_root_handlers = list(root.handlers)
        before_we_handlers = list(we.handlers)
        before_we_level = we.level

        capture = RunLogCapture("exec-x", log_path)
        capture.start()
        # A capture handler is attached only to the named logger, never root.
        assert len(we.handlers) == len(before_we_handlers) + 1
        assert list(root.handlers) == before_root_handlers

        logging.getLogger("workflow_engine.child").info("captured line")
        capture.stop()

        # Detached and level restored; root never touched (R2.5).
        assert list(we.handlers) == before_we_handlers
        assert we.level == before_we_level
        assert list(root.handlers) == before_root_handlers
        with open(log_path, "r", encoding="utf-8") as f:
            assert "captured line" in f.read()

    def test_stop_is_idempotent_and_safe_without_start(self, tmp_path):
        capture = RunLogCapture("exec-x", str(tmp_path / "run.log"))
        # Never started: stop is a no-op, raises nothing.
        capture.stop()
        capture.stop()
