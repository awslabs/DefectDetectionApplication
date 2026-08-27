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
"""Tests for the Custom_Python_Node bridge (Requirement 9.8).

The subprocess tests run real handler subprocesses through the framed
stdin/stdout protocol; the rewrite tests are pure-function tests over the
compiled document; the executor tests inject a fake bridged runner so no
GStreamer is required.
"""
import os
import time

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import python_bridge, rendering
from workflow_engine.python_bridge import (
    BridgeSpec,
    CustomPythonBridge,
    CustomPythonNodeError,
    bridge_specs,
    build_bridges,
    rewrite_document,
)

NODE_ID = "pynode"

ECHO_HANDLER = """\
def handle(frame, metadata):
    out = dict(metadata)
    out["frames"] = out.get("frames", 0) + 1
    out["seen"] = [metadata.get("width"), len(frame)]
    return frame[::-1], out
"""


def write_handler(tmp_path, code, name="handler.py"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        f.write(code)
    return path


def make_bridge(handler_path, **kwargs):
    kwargs.setdefault("wall_clock_limit_sec", 5.0)
    return CustomPythonBridge(NODE_ID, handler_path, **kwargs)


# ---------------------------------------------------------------------------
# Protocol round trip with a real subprocess
# ---------------------------------------------------------------------------


class TestProtocolRoundTrip:
    def test_frames_and_metadata_round_trip(self, tmp_path):
        bridge = make_bridge(write_handler(tmp_path, ECHO_HANDLER))
        try:
            out_frame, out_meta = bridge.process_frame(
                b"abcdef", metadata={"width": 4}, width=4, height=2,
                frame_format="RGB",
            )
            assert out_frame == b"fedcba"
            assert out_meta["frames"] == 1
            assert out_meta["seen"] == [4, 6]

            # Same subprocess serves subsequent frames.
            out_frame, out_meta = bridge.process_frame(b"xy", metadata={})
            assert out_frame == b"yx"
            assert out_meta["frames"] == 1
        finally:
            bridge.stop()

    def test_none_frame_result_passes_input_through(self, tmp_path):
        handler = "def handle(frame, metadata):\n    return None, {'ok': 1}\n"
        bridge = make_bridge(write_handler(tmp_path, handler))
        try:
            out_frame, out_meta = bridge.process_frame(b"data")
            assert out_frame == b"data"
            assert out_meta == {"ok": 1}
        finally:
            bridge.stop()

    def test_stop_is_idempotent_and_ends_the_subprocess(self, tmp_path):
        bridge = make_bridge(write_handler(tmp_path, ECHO_HANDLER))
        bridge.start()
        process = bridge._process
        bridge.stop()
        bridge.stop()
        assert process.poll() is not None


# ---------------------------------------------------------------------------
# Failure modes: every one identifies the node (Requirement 9.8)
# ---------------------------------------------------------------------------


class TestWallClockLimit:
    def test_timeout_kills_the_subprocess_and_names_the_node(self, tmp_path):
        handler = (
            "import time\n"
            "def handle(frame, metadata):\n"
            "    time.sleep(30)\n"
        )
        bridge = make_bridge(
            write_handler(tmp_path, handler), wall_clock_limit_sec=0.5
        )
        bridge.start()
        process = bridge._process
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(b"frame")
        assert excinfo.value.node_id == NODE_ID
        assert "wall-clock" in str(excinfo.value)
        assert NODE_ID in str(excinfo.value)
        # Deadline-based, not sleep-until-done: the handler slept 30s.
        deadline = time.time() + 5
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert process.poll() is not None


class TestMemoryLimit:
    def test_exceeding_the_memory_limit_names_the_node(self, tmp_path):
        pytest.importorskip("resource")
        handler = (
            "def handle(frame, metadata):\n"
            "    block = bytearray(1 << 32)\n"  # 4 GiB, over the limit
            "    return frame, {'len': len(block)}\n"
        )
        bridge = make_bridge(
            write_handler(tmp_path, handler),
            memory_limit_bytes=512 * 1024 * 1024,
        )
        try:
            with pytest.raises(CustomPythonNodeError) as excinfo:
                bridge.process_frame(b"frame")
        finally:
            bridge.stop()
        assert excinfo.value.node_id == NODE_ID
        message = str(excinfo.value)
        assert "MemoryError" in message or "memory" in message.lower()


class TestHandlerFailures:
    def test_handler_exception_names_the_node(self, tmp_path):
        handler = (
            "def handle(frame, metadata):\n"
            "    raise ValueError('boom from user code')\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(b"frame")
        assert excinfo.value.node_id == NODE_ID
        assert "boom from user code" in str(excinfo.value)

    def test_handler_without_handle_function_names_the_node(self, tmp_path):
        bridge = make_bridge(write_handler(tmp_path, "x = 1\n"))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(b"frame")
        assert excinfo.value.node_id == NODE_ID
        assert "handle" in str(excinfo.value)

    def test_missing_handler_file_names_the_node(self, tmp_path):
        bridge = make_bridge(os.path.join(str(tmp_path), "absent.py"))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.start()
        assert excinfo.value.node_id == NODE_ID
        assert "handler not found" in str(excinfo.value)

    def test_subprocess_crash_names_the_node(self, tmp_path):
        handler = (
            "import os\n"
            "def handle(frame, metadata):\n"
            "    os._exit(3)\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(b"frame")
        assert excinfo.value.node_id == NODE_ID
        assert "exit code 3" in str(excinfo.value)


class TestProtocolViolations:
    def test_garbage_on_stdout_names_the_node(self, tmp_path):
        # stdout belongs to the protocol; raw writes to it corrupt the
        # framing and must fail the node, not hang or poison the run.
        handler = (
            "import sys\n"
            "def handle(frame, metadata):\n"
            "    sys.stdout.buffer.write(b'garbage')\n"
            "    sys.stdout.buffer.flush()\n"
            "    return frame, {}\n"
        )
        bridge = make_bridge(write_handler(tmp_path, handler))
        with pytest.raises(CustomPythonNodeError) as excinfo:
            bridge.process_frame(b"frame")
        assert excinfo.value.node_id == NODE_ID
        assert "protocol violation" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Compiled-document rewrite (pure functions)
# ---------------------------------------------------------------------------


def make_document(segments):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": segments,
        "executorBindings": [],
        "pluginDependencies": ["dda-emlpython"],
    }


EMLPYTHON_ELEMENT = {
    "nodeId": NODE_ID,
    "factory": "emlpython",
    "args": {"handler-path": "python/pynode/handler.py"},
}

BRIDGED_DOC = make_document(
    [
        {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}},
                EMLPYTHON_ELEMENT,
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ]
)


class TestBridgeSpecs:
    def test_specs_extracted_in_render_order(self):
        specs = bridge_specs(BRIDGED_DOC)
        assert specs == [
            BridgeSpec(
                node_id=NODE_ID,
                handler_path="python/pynode/handler.py",
                sink_name="py_in_pynode",
                src_name="py_out_pynode",
            )
        ]

    def test_document_without_bridges_yields_no_specs(self):
        document = make_document(
            [{"name": "s0",
              "elements": [{"nodeId": "n1", "factory": "videotestsrc",
                            "args": {}}]}]
        )
        assert bridge_specs(document) == []


class TestRewriteDocument:
    def test_bridge_element_becomes_appsink_appsrc_pair(self):
        rewritten = rewrite_document(BRIDGED_DOC)

        launch = rendering.render_launch_string(rewritten)
        # ``caps=video/x-raw,format=RGB`` on the appsink joined with the
        # pipeline-stall fix (test_python_bridge_pipeline_stall.py):
        # unconstrained bridge appsinks negotiated formats the handler
        # runtime rejects (RGBx from Bayer sources, RGBA64_LE observed
        # on jetson-thor1). The pin was regenerated per the launch-string
        # maintenance path; every other element is byte-identical.
        assert launch == (
            "videotestsrc num-buffers=1 ! "
            "appsink name=py_in_pynode emit-signals=true sync=false "
            "max-buffers=1 caps=video/x-raw,format=RGB "
            "appsrc name=py_out_pynode is-live=true format=time block=true "
            "! fakesink"
        )
        assert "emlpython" not in launch

    def test_pair_keeps_the_custom_node_id_for_failure_mapping(self):
        rewritten = rewrite_document(BRIDGED_DOC)
        name_map = rendering.element_name_map(rewritten)
        assert name_map["py_in_pynode"] == NODE_ID
        assert name_map["py_out_pynode"] == NODE_ID

    def test_from_stays_upstream_and_link_to_moves_downstream(self):
        document = make_document(
            [
                {
                    "name": "s1",
                    "from": "t0",
                    "linkTo": "f0",
                    "elements": [
                        {"nodeId": "q", "factory": "queue", "args": {}},
                        EMLPYTHON_ELEMENT,
                        {"nodeId": "c", "factory": "videoconvert",
                         "args": {}},
                    ],
                }
            ]
        )
        rewritten = rewrite_document(document)
        first, second = rewritten["segments"]
        assert first.get("from") == "t0"
        assert "linkTo" not in first
        assert second.get("linkTo") == "f0"
        assert "from" not in second
        launch = rendering.render_launch_string(rewritten)
        assert launch.startswith("t0. ! queue ! appsink")
        assert launch.endswith("! videoconvert ! f0.")

    def test_two_bridges_in_one_segment_split_into_three_parts(self):
        second_element = {
            "nodeId": "py2",
            "factory": "emlpython",
            "args": {"handler-path": "python/py2/handler.py"},
        }
        document = make_document(
            [
                {
                    "name": "s0",
                    "elements": [
                        {"nodeId": "n1", "factory": "videotestsrc",
                         "args": {}},
                        EMLPYTHON_ELEMENT,
                        second_element,
                        {"nodeId": None, "factory": "fakesink", "args": {}},
                    ],
                }
            ]
        )
        rewritten = rewrite_document(document)
        assert len(rewritten["segments"]) == 3
        launch = rendering.render_launch_string(rewritten)
        assert "py_in_pynode" in launch and "py_out_pynode" in launch
        assert "py_in_py2" in launch and "py_out_py2" in launch

    def test_document_without_bridges_is_unchanged(self):
        document = make_document(
            [{"name": "s0",
              "elements": [{"nodeId": "n1", "factory": "videotestsrc",
                            "args": {}}]}]
        )
        assert rewrite_document(document) == document


class TestBuildBridges:
    def test_handler_paths_resolve_inside_the_artifacts(self, tmp_path):
        bridges = build_bridges(bridge_specs(BRIDGED_DOC), str(tmp_path))
        assert len(bridges) == 1
        assert bridges[0].node_id == NODE_ID
        assert bridges[0]._handler_path == os.path.join(
            str(tmp_path), "python/pynode/handler.py"
        )

    def test_missing_handler_path_arg_names_the_node(self, tmp_path):
        spec = BridgeSpec(
            node_id=NODE_ID, handler_path=None,
            sink_name="py_in_pynode", src_name="py_out_pynode",
        )
        with pytest.raises(CustomPythonNodeError) as excinfo:
            build_bridges([spec], str(tmp_path))
        assert excinfo.value.node_id == NODE_ID


# ---------------------------------------------------------------------------
# Executor integration: rewrite + bridged runner + failure containment
# ---------------------------------------------------------------------------

from unittest.mock import patch

from workflow_engine import gst_plugins
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


class ExplodingPipelineManager:
    """The plain manager must never run for bridged documents."""

    def run_pipeline(self, *args, **kwargs):
        raise AssertionError(
            "GstPipelineManager.run_pipeline must not be used for "
            "documents with Custom_Python_Nodes"
        )


class FakeBridgedRunner:
    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []

    def __call__(self, launch_string, bridges, latency_metrics=None):
        self.calls.append((launch_string, list(bridges)))
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


def seed_bridged_run(session_factory, tmp_path, compiled):
    artifact_path = write_artifact_set(tmp_path, compiled=compiled)
    handler_dir = os.path.join(artifact_path, "python", NODE_ID)
    os.makedirs(handler_dir, exist_ok=True)
    with open(os.path.join(handler_dir, "handler.py"), "w") as f:
        f.write(ECHO_HANDLER)
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
    return artifact_path


def get_execution(session_factory):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, "exec-1")
    finally:
        session.close()


class TestExecutorIntegration:
    def test_bridged_document_runs_through_the_bridged_runner(self, tmp_path):
        session_factory = make_session_factory()
        artifact_path = seed_bridged_run(session_factory, tmp_path, BRIDGED_DOC)
        runner = FakeBridgedRunner(tag_values={"is_anomalous": False})

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        assert len(runner.calls) == 1
        launch_string, bridges = runner.calls[0]
        assert "emlpython" not in launch_string
        assert "appsink name=py_in_pynode" in launch_string
        assert "appsrc name=py_out_pynode" in launch_string
        assert [bridge.node_id for bridge in bridges] == [NODE_ID]
        assert bridges[0]._handler_path == os.path.join(
            str(artifact_path), "python", NODE_ID, "handler.py"
        )
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )

    def test_bridge_failure_fails_only_that_run_with_the_node(self, tmp_path):
        session_factory = make_session_factory()
        seed_bridged_run(session_factory, tmp_path, BRIDGED_DOC)
        runner = FakeBridgedRunner(
            error=CustomPythonNodeError(NODE_ID, "handler failed: boom")
        )

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == NODE_ID
        assert "handler failed: boom" in row.error

    def test_missing_handler_path_arg_fails_with_the_node(self, tmp_path):
        broken_element = {
            "nodeId": NODE_ID,
            "factory": "emlpython",
            "args": {},  # no handler-path
        }
        document = make_document(
            [
                {
                    "name": "s0",
                    "elements": [
                        {"nodeId": "n1", "factory": "videotestsrc",
                         "args": {}},
                        broken_element,
                        {"nodeId": None, "factory": "fakesink", "args": {}},
                    ],
                }
            ]
        )
        session_factory = make_session_factory()
        seed_bridged_run(session_factory, tmp_path, document)
        runner = FakeBridgedRunner()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        assert runner.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == NODE_ID
        assert "handler-path" in row.error

    def test_document_without_bridges_uses_the_plain_manager(self, tmp_path):
        class RecordingManager:
            def __init__(self):
                self.calls = []

            def run_pipeline(self, pipeline_str, frame_data=None,
                             latency_metrics=None, status_sink=None):
                self.calls.append(pipeline_str)
                return {}

        plain_doc = make_document(
            [{"name": "s0",
              "elements": [{"nodeId": "n1", "factory": "videotestsrc",
                            "args": {}}]}]
        )
        session_factory = make_session_factory()
        seed_bridged_run(session_factory, tmp_path, plain_doc)
        manager = RecordingManager()
        runner = FakeBridgedRunner()

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        assert runner.calls == []
        assert manager.calls == ["videotestsrc"]
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )
