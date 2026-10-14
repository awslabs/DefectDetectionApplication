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
"""WorkflowExecutor Custom Python source feed wiring tests (Task 10.5).

Feature: custom-python-source (Requirements 5.2, 5.4, 7.4, 7.5, 7.6, 7.7).

Real producer handler subprocesses run through ``build_producer_bridge``
exactly as in production; the pipeline manager (and, for the coexistence
test, the bridged runner) are the fake harness pieces the aravis executor
tests established, so no GStreamer is required:

- a produced frame runs through ``run_pipeline(launch_string, frame_data)``
  — the single-frame push + EOS Frame_Feed model (Requirement 7.5) — with
  the appsrc renamed and given the frame's explicit caps;
- a planning failure (two fed sources) marks the run failed with every
  offending node named and never starts the pipeline (Requirement 7.6);
- a source node and a bridged Custom Python node feed and pump in ONE run
  through ``run_bridged_pipeline(..., frame_data=...)`` (Requirement 7.4);
- the producer's fetched-sources list lands in the run log
  (Requirement 5.4);
- the source node appears in the per-node run status, with failure
  attribution through ``failing_node_id`` (Requirement 7.7);
- a fetch outside the node's allowed prefixes fails the run with the
  node identified (Requirement 5.2).
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import STATUS_SUCCESS
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

NODE_ID = "src1"

#: 2x2 RGB Produced_Frame with producer metadata (Requirement 6.7).
BASIC_HANDLER = """\
def produce_frame(context):
    return {
        "data": b"\\x01" * 12,
        "width": 2,
        "height": 2,
        "format": "RGB",
        "metadata": {"model": "demo"},
    }
"""

#: Fetches a payload beside the handler through dda_frames, so the
#: executor's fetched-sources run-log line names a concrete path.
FETCHING_HANDLER = """\
import os

import dda_frames


def produce_frame(context):
    source = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "payload.bin"
    )
    dda_frames.load_bytes(source)
    return {"data": b"\\x00" * 4, "width": 2, "height": 2,
            "format": "GRAY8"}
"""

#: Fetches a source outside the node's declared prefixes (Requirement 5.2).
DENIED_HANDLER = """\
import dda_frames


def produce_frame(context):
    dda_frames.load_bytes("/etc/hostname")
    return {"data": b"\\x00", "width": 1, "height": 1, "format": "GRAY8"}
"""

#: Returning None must fail the run (Requirement 3.8).
NONE_HANDLER = """\
def produce_frame(context):
    return None
"""

#: Pass-through per-frame handler for the bridged Custom Python node.
ECHO_HANDLER = """\
def handle(frame, metadata):
    return frame, metadata
"""


def make_source_document(node_id=NODE_ID, prefixes="", extra_points=(),
                         extra_elements=()):
    """A compiled_pipeline.json with one Custom Python source: the
    compiled appsrc chain plus the packager's pythonSourceBinding point."""
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": node_id, "factory": "appsrc",
                     "args": {"name": "appsrc_{0}".format(node_id)}},
                    {"nodeId": node_id, "factory": "videoconvert",
                     "args": {}},
                ] + list(extra_elements) + [
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "bindingPoints": [
            {
                "nodeId": node_id,
                "nodeType": "custom_python_source",
                "pythonSourceBinding": True,
                "parameters": {"allowed_uri_prefixes": prefixes},
                "slots": [],
            }
        ] + list(extra_points),
        "executorBindings": [],
        "pluginDependencies": [],
    }


class FakePipelineManager:
    """Records every run_pipeline call exactly as it was made, so the
    single-frame push (frame_data positional) is observable."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def write_handler(artifact_path, node_id, code):
    handler_dir = os.path.join(str(artifact_path), "python", node_id)
    os.makedirs(handler_dir, exist_ok=True)
    with open(os.path.join(handler_dir, "handler.py"), "w") as f:
        f.write(code)
    return handler_dir


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


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, "exec-1")
    finally:
        session.close()


def seed_source_run(session_factory, tmp_path, compiled=None,
                    handler=BASIC_HANDLER):
    artifact_path = write_artifact_set(
        tmp_path, compiled=compiled or make_source_document()
    )
    write_handler(artifact_path, NODE_ID, handler)
    execution_id = seed_run(session_factory, artifact_path)
    return artifact_path, execution_id


class TestProducedFrameFeed:
    def test_produced_frame_takes_the_single_push_and_eos_run_path(
        self, tmp_path, session_factory
    ):
        """Requirements 7.1, 7.5: the Produced_Frame is handed to
        ``run_pipeline(launch_string, frame_data)`` — the Frame_Feed
        model that pushes exactly one wrapped buffer into the renamed
        appsrc and sends EOS — with caps naming the frame's explicit
        format; the producer's metadata merges under the node's key
        (Requirement 6.7)."""
        _, execution_id = seed_source_run(session_factory, tmp_path)
        manager = FakePipelineManager(tag_values={"is_anomalous": False})
        observed = []

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            post_run_handler=(
                lambda registration, document, tags: observed.append(tags)
            ),
        ).execute(execution_id)

        assert len(manager.calls) == 1
        launch, args, kwargs = manager.calls[0]
        # The compiled appsrc is renamed for run_pipeline's Frame_Feed
        # lookup and carries the frame's explicit caps (Requirement 7.2).
        assert launch == (
            "appsrc name=appsrc caps=video/x-raw,format=RGB "
            "! videoconvert ! fakesink"
        )
        # One frame_data positional — the single-frame push + EOS model.
        assert args == ({
            "data": b"\x01" * 12, "width": 2, "height": 2, "format": "RGB",
        },)
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )
        # Producer metadata merged under python_source.<nodeId>, before
        # the seeded trigger key.
        assert observed == [{
            "is_anomalous": False,
            "python_source": {NODE_ID: {"model": "demo"}},
            "trigger": {},
        }]

    def test_source_node_reaches_success_in_the_per_node_status(
        self, tmp_path, session_factory
    ):
        """Requirement 7.7: the node's appsrc element keeps its nodeId,
        so the NodeStatusCollector covers the source node with no new
        code — terminal success on the clean path."""
        _, execution_id = seed_source_run(session_factory, tmp_path)

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=FakePipelineManager,
        ).execute(execution_id)

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_COMPLETED
        status = json.loads(row.node_status_json)
        assert status[NODE_ID]["status"] == STATUS_SUCCESS


class TestPrePipelineFailures:
    def test_planning_failure_names_every_offender_and_never_starts(
        self, tmp_path, session_factory
    ):
        """Requirements 7.6, 8.5: a document carrying two fed frame
        sources fails the run with every offending node named before the
        pipeline (and any producer subprocess) ever starts."""
        document = make_source_document(extra_points=[{
            "nodeId": "src2",
            "nodeType": "custom_python_source",
            "pythonSourceBinding": True,
            "parameters": {},
            "slots": [],
        }])
        _, execution_id = seed_source_run(
            session_factory, tmp_path, compiled=document
        )
        manager = FakePipelineManager()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert "src1" in row.error and "src2" in row.error
        assert row.finished_at is not None

    def test_production_failure_is_attributed_to_the_node(
        self, tmp_path, session_factory
    ):
        """Requirements 3.8, 7.6: a producer that returns None fails the
        run with failing_node_id set to the source node, and the
        pipeline never starts."""
        _, execution_id = seed_source_run(
            session_factory, tmp_path, handler=NONE_HANDLER
        )
        manager = FakePipelineManager()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == NODE_ID
        assert "must produce a frame" in row.error

    def test_denied_prefix_fetch_fails_the_run_with_the_node(
        self, tmp_path, session_factory
    ):
        """Requirement 5.2: a fetch outside the node's allowed URI
        prefixes fails the run with the node identified and an error
        naming the source and the restriction."""
        document = make_source_document(prefixes="s3://allowed/\n")
        _, execution_id = seed_source_run(
            session_factory, tmp_path, compiled=document,
            handler=DENIED_HANDLER,
        )
        manager = FakePipelineManager()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == NODE_ID
        assert "/etc/hostname" in row.error
        assert "outside the node's allowed URI prefixes" in row.error


class TestBridgedCoexistence:
    def test_source_and_bridged_node_feed_and_pump_in_one_run(
        self, tmp_path, session_factory
    ):
        """Requirement 7.4: a source node plus a bridged Custom Python
        node run through ``run_bridged_pipeline(..., frame_data=...)``
        — the produced frame feeds the renamed appsrc while the
        emlpython bridge pumps, in the same pipeline."""
        document = make_source_document(extra_elements=[{
            "nodeId": "pynode",
            "factory": "emlpython",
            "args": {"handler-path": "python/pynode/handler.py"},
        }])
        artifact_path, execution_id = seed_source_run(
            session_factory, tmp_path, compiled=document
        )
        write_handler(artifact_path, "pynode", ECHO_HANDLER)
        runner_calls = []

        def runner(launch_string, bridges, latency_metrics=None,
                   frame_data=None):
            runner_calls.append((launch_string, list(bridges), frame_data))
            return {"is_anomalous": False}

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=FakePipelineManager,
            bridged_pipeline_runner=runner,
        ).execute(execution_id)

        assert len(runner_calls) == 1
        launch, bridges, frame_data = runner_calls[0]
        assert "emlpython" not in launch
        assert "appsink name=py_in_pynode" in launch
        assert "appsrc name=py_out_pynode" in launch
        # The fed source: renamed, explicit caps, produced frame.
        assert launch.startswith(
            "appsrc name=appsrc caps=video/x-raw,format=RGB "
        )
        assert [bridge.node_id for bridge in bridges] == ["pynode"]
        assert frame_data == {
            "data": b"\x01" * 12, "width": 2, "height": 2, "format": "RGB",
        }
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )


class TestFetchedSourcesRunLog:
    def test_fetched_sources_appear_in_the_run_log(
        self, tmp_path, session_factory
    ):
        """Requirement 5.4: every source the producer fetched through
        dda_frames is written to the run log captured for the run."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_source_document()
        )
        handler_dir = write_handler(artifact_path, NODE_ID, FETCHING_HANDLER)
        payload_path = os.path.join(handler_dir, "payload.bin")
        with open(payload_path, "wb") as f:
            f.write(b"payload-bytes")
        execution_id = seed_run(session_factory, artifact_path)
        capture_root = os.path.join(str(tmp_path), "captures")

        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=FakePipelineManager,
            ).execute(execution_id)

        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )
        log_path = os.path.join(capture_root, "wf-1", "exec-1", "run.log")
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
        assert payload_path in log_text
