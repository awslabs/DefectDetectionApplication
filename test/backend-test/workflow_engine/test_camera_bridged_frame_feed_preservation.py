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
"""Preservation property tests: bridged pipeline camera frame feed stall
(bridged-pipeline-camera-frame-feed-stall, Property 2: Preservation).

Property 2: Preservation — Non-camera bridged and non-bridged call
shapes.

The camera-frame-feed fix changes exactly one keyword argument in the
bridged branch of ``WorkflowExecutor.execute``
(``workflow_engine/pipeline_executor.py``, ``frame_data=python_frame_data``
→ ``frame_data=frame_data``). These tests pin the runner/manager
invocations of every family where the bug condition does NOT hold, so
the fix cannot change them:

- **Python-source bridged run** (Requirement 3.1): the bridged runner
  keeps receiving the produced frame as ``frame_data``. After the merge
  at pipeline_executor.py lines 1344–1345, ``frame_data`` and
  ``python_frame_data`` are the same object for this family, so the fix
  is behavior-identical by construction. Cross-checked against
  ``test_workflow_python_source_executor.py`` (TestBridgedCoexistence).
- **Feed-free bridged run** (Requirement 3.2): the bridged runner is
  invoked with NO ``frame_data`` keyword at all — ``_run_bridged`` only
  adds the kwarg when the value is not None, and both variables are
  ``None`` for this family. Cross-checked against
  ``test_workflow_python_bridge.py`` (TestExecutorIntegration).
- **Non-bridged Aravis run** (Requirement 3.3): the plain manager keeps
  running ``run_pipeline(launch_string, frame_data)`` with the grabbed
  frame — that branch (line 1608) already used the merged variable and
  is untouched. Also pinned in depth by
  ``test_workflow_aravis_executor.py``; the thin test here keeps this
  spec's suite self-contained.
- **Feed-free, bridge-free run** (Requirement 3.4): the plain
  ``run_pipeline(launch_string)`` path, no frame_data argument at all.
  Also pinned by ``test_workflow_aravis_executor.py``
  (TestAravisFreePath); thin self-contained pin here.

Observation-first methodology: each assertion encodes the invocation
observed on the UNFIXED tree (spec/jetpack7-support HEAD a2ad086, buggy
line still present), so this suite PASSES before the fix (baseline) and
must STILL pass after it (task 3.3).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**
"""
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
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

REGISTRATION_ID = "wf-1:3"
SOURCE_NODE_ID = "src1"
PYTHON_NODE_ID = "pynode"
CAMERA_NODE_ID = "n1"

#: 2x2 RGB Produced_Frame — the Custom Python SOURCE node's output
#: (same shape as test_workflow_python_source_executor.py).
PRODUCER_HANDLER = """\
def produce_frame(context):
    return {
        "data": b"\\x01" * 12,
        "width": 2,
        "height": 2,
        "format": "RGB",
    }
"""

#: The produced frame the handler above emits, for the equality pin.
PRODUCED_FRAME = {
    "data": b"\x01" * 12, "width": 2, "height": 2, "format": "RGB",
}

#: Pass-through Custom_Python_Node handler — the bridged runner double
#: never runs it; it only needs to exist so build_bridges resolves the
#: handler path.
ECHO_HANDLER = """\
def handle(frame, metadata):
    return frame, dict(metadata)
"""

EMLPYTHON_ELEMENT = {
    "nodeId": PYTHON_NODE_ID,
    "factory": "emlpython",
    "args": {"handler-path": "python/{0}/handler.py".format(PYTHON_NODE_ID)},
}


def make_document(segments, binding_points=(), plugin_dependencies=()):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": segments,
        "bindingPoints": list(binding_points),
        "executorBindings": [],
        "pluginDependencies": list(plugin_dependencies),
    }


def make_python_source_bridged_document():
    """Custom Python SOURCE node (produced frame) plus an emlpython
    bridge in one document — the Requirement 3.1 family. Mirrors
    test_workflow_python_source_executor.py TestBridgedCoexistence."""
    return make_document(
        segments=[
            {
                "name": "s0",
                "elements": [
                    {"nodeId": SOURCE_NODE_ID, "factory": "appsrc",
                     "args": {"name": "appsrc_{0}".format(SOURCE_NODE_ID)}},
                    {"nodeId": SOURCE_NODE_ID, "factory": "videoconvert",
                     "args": {}},
                    EMLPYTHON_ELEMENT,
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        binding_points=[
            {
                "nodeId": SOURCE_NODE_ID,
                "nodeType": "custom_python_source",
                "pythonSourceBinding": True,
                "parameters": {"allowed_uri_prefixes": ""},
                "slots": [],
            }
        ],
        plugin_dependencies=["dda-emlpython"],
    )


def make_feed_free_bridged_document():
    """emlpython bridge, no Aravis binding point, no Custom Python
    source — the Requirement 3.2 family (the pre-existing bridged call
    shape). Mirrors test_workflow_python_bridge.py BRIDGED_DOC."""
    return make_document(
        segments=[
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "videotestsrc",
                     "args": {"num-buffers": 1}},
                    EMLPYTHON_ELEMENT,
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        plugin_dependencies=["dda-emlpython"],
    )


def make_aravis_non_bridged_document():
    """Aravis binding point, no bridges — the Requirement 3.3 family.
    Mirrors test_workflow_aravis_executor.py make_aravis_document."""
    return make_document(
        segments=[
            {
                "name": "s0",
                "elements": [
                    {"nodeId": CAMERA_NODE_ID, "factory": "appsrc",
                     "args": {"name": "appsrc_{0}".format(CAMERA_NODE_ID)}},
                    {"nodeId": CAMERA_NODE_ID, "factory": "videoconvert",
                     "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        binding_points=[
            {
                "nodeId": CAMERA_NODE_ID,
                "nodeType": "aravis_camera_source",
                "parameters": {"camera_id": "Aravis-Fake-GV01",
                               "gain": 4, "exposure": 5000000},
                "slots": [],
                "aravisBinding": True,
            }
        ],
    )


def make_plain_document():
    """No feed, no bridges — the Requirement 3.4 family."""
    return make_document(
        segments=[
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "videotestsrc",
                     "args": {"num-buffers": 1}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
    )


def make_frame(width=4, height=2, bytes_per_pixel=1):
    return {
        "data": b"\x00" * (width * height * bytes_per_pixel),
        "width": width,
        "height": height,
    }


class FakeCameraManager:
    """Callable frame grabber recording (camera_id, config) calls."""

    def __init__(self, frame=None):
        self.frame = frame if frame is not None else make_frame()
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return self.frame


class RecordingBridgedRunner:
    """Records every bridged-runner invocation exactly as it was made —
    ``(launch_string, bridges, kwargs)`` — so the presence/absence of
    the ``frame_data`` keyword is directly observable (``_run_bridged``
    omits the kwarg entirely when the value is ``None``)."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def __call__(self, launch_string, bridges, **kwargs):
        self.calls.append((launch_string, list(bridges), dict(kwargs)))
        return dict(self.tag_values)


class RecordingPipelineManager:
    """Records every run_pipeline call exactly as it was made, so the
    positional frame_data push (or its absence) is observable."""

    def __init__(self, tag_values=None):
        self.tag_values = tag_values or {}
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return dict(self.tag_values)


class ExplodingPipelineManager:
    """The plain manager must never run for bridged documents — any
    fallthrough fails loudly instead of silently taking the wrong
    path."""

    def run_pipeline(self, *args, **kwargs):
        raise AssertionError(
            "GstPipelineManager.run_pipeline must not be used for "
            "documents with Custom_Python_Nodes"
        )


class ExplodingBridgedRunner:
    """The bridged runner must never run for bridge-free documents."""

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "run_bridged_pipeline must not be used for documents "
            "without Custom_Python_Nodes"
        )


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


def seed_run(session_factory, tmp_path, compiled, handlers=()):
    """Artifact set with the compiled document plus per-node handler
    files at python/<node>/handler.py; one pending execution row."""
    artifact_path = write_artifact_set(tmp_path, compiled=compiled)
    for node_id, code in handlers:
        write_handler(artifact_path, node_id, code)
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=REGISTRATION_ID,
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
                registration_id=REGISTRATION_ID,
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


class TestPythonSourceBridgedPreservation:
    def test_bridged_runner_receives_the_produced_frame(self, tmp_path):
        """Requirement 3.1: a document with a Custom Python source node
        AND emlpython bridges keeps invoking the bridged runner with the
        produced frame as ``frame_data``. Observed on the unfixed tree:
        one runner call, ``frame_data`` equal to the producer's frame
        dict (it crosses the producer-subprocess boundary, so equality —
        not identity — is the preserved contract). After the line
        1344–1345 merge, ``frame_data is python_frame_data`` for this
        family, so the fix cannot change this call."""
        session_factory = make_session_factory()
        seed_run(
            session_factory, tmp_path,
            compiled=make_python_source_bridged_document(),
            handlers=[
                (SOURCE_NODE_ID, PRODUCER_HANDLER),
                (PYTHON_NODE_ID, ECHO_HANDLER),
            ],
        )
        runner = RecordingBridgedRunner(tag_values={"is_anomalous": False})

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        assert len(runner.calls) == 1
        launch_string, bridges, kwargs = runner.calls[0]
        # The fed source: renamed appsrc with the frame's explicit caps,
        # and the emlpython element rewritten to its appsink/appsrc pair.
        assert launch_string.startswith(
            "appsrc name=appsrc caps=video/x-raw,format=RGB "
        )
        assert "appsink name=py_in_{0}".format(PYTHON_NODE_ID) in launch_string
        assert [bridge.node_id for bridge in bridges] == [PYTHON_NODE_ID]
        # The produced frame is forwarded to the runner.
        assert kwargs["frame_data"] == PRODUCED_FRAME
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )


class TestFeedFreeBridgedPreservation:
    def test_bridged_runner_invoked_without_frame_data_keyword(
        self, tmp_path
    ):
        """Requirement 3.2: a document with emlpython bridges but no
        frame feed of either kind (no Aravis point, no Custom Python
        source) keeps the pre-existing bridged call shape bit-identical:
        the runner is invoked with NO ``frame_data`` keyword at all
        (``_run_bridged`` only adds the kwarg when the value is not
        None; both feed variables are None for this family)."""
        session_factory = make_session_factory()
        seed_run(
            session_factory, tmp_path,
            compiled=make_feed_free_bridged_document(),
            handlers=[(PYTHON_NODE_ID, ECHO_HANDLER)],
        )
        runner = RecordingBridgedRunner(tag_values={"is_anomalous": False})

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=ExplodingPipelineManager,
            bridged_pipeline_runner=runner,
        ).execute("exec-1")

        assert len(runner.calls) == 1
        launch_string, bridges, kwargs = runner.calls[0]
        assert "emlpython" not in launch_string
        assert "appsink name=py_in_{0}".format(PYTHON_NODE_ID) in launch_string
        assert [bridge.node_id for bridge in bridges] == [PYTHON_NODE_ID]
        # The pre-existing bridged call shape: no frame_data keyword at
        # all — the exact kwarg set observed on the unfixed tree.
        assert "frame_data" not in kwargs
        assert set(kwargs) == {"latency_metrics"}
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )


class TestNonBridgedAravisPreservation:
    def test_grabbed_frame_goes_through_run_pipeline_positionally(
        self, tmp_path
    ):
        """Requirement 3.3: a document with an Aravis frame feed but no
        bridges keeps running through
        ``run_pipeline(launch_string, frame_data)`` with the grabbed
        frame as the positional argument — the non-bridged branch (line
        1608) already used the merged variable and is untouched by the
        fix. Pinned in depth by ``test_workflow_aravis_executor.py``;
        this thin pin keeps the spec's suite self-contained."""
        session_factory = make_session_factory()
        seed_run(
            session_factory, tmp_path,
            compiled=make_aravis_non_bridged_document(),
        )
        frame = make_frame(width=8, height=4)
        grabber = FakeCameraManager(frame=frame)
        manager = RecordingPipelineManager(
            tag_values={"is_anomalous": False}
        )

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            bridged_pipeline_runner=ExplodingBridgedRunner(),
            frame_grabber=grabber,
        ).execute("exec-1")

        assert grabber.calls == [
            ("Aravis-Fake-GV01", {"gain": 4, "exposure": 5000000})
        ]
        assert len(manager.calls) == 1
        launch_string, args, kwargs = manager.calls[0]
        assert launch_string.startswith("appsrc name=appsrc ")
        # The grabbed frame, positionally — the classic Camera-type
        # Frame_Feed shape observed on the unfixed tree.
        assert args == (frame,)
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )


class TestFeedFreeBridgeFreePreservation:
    def test_plain_document_takes_the_plain_run_pipeline_path(
        self, tmp_path
    ):
        """Requirement 3.4: a document with neither a frame feed nor
        bridges keeps taking the plain ``run_pipeline(launch_string)``
        path — no frame_data argument at all. Also pinned by
        ``test_workflow_aravis_executor.py`` (TestAravisFreePath); thin
        self-contained pin here."""
        session_factory = make_session_factory()
        seed_run(
            session_factory, tmp_path, compiled=make_plain_document()
        )
        grabber = FakeCameraManager()
        manager = RecordingPipelineManager(
            tag_values={"is_anomalous": False}
        )

        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            bridged_pipeline_runner=ExplodingBridgedRunner(),
            frame_grabber=grabber,
        ).execute("exec-1")

        assert grabber.calls == []
        assert len(manager.calls) == 1
        launch_string, args, kwargs = manager.calls[0]
        assert launch_string == "videotestsrc num-buffers=1 ! fakesink"
        assert args == ()
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        assert get_execution(session_factory).status == (
            EXECUTION_STATUS_COMPLETED
        )
