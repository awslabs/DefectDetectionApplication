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
"""Property test for explicit-caps frame feeding (Task 10.3).

**Feature: custom-python-source, Property 13: The Produced_Frame is fed
with explicit caps before the pipeline starts**

*For any* Produced_Frame across all supported Pixel_Formats and dims —
including frames whose byte length would make the pre-feature
bytes-per-pixel inference name a DIFFERENT format — executing a document
with one Custom_Python_Source_Node points the node's compiled ``appsrc``
at the Frame_Feed before the pipeline runs, hands the pipeline manager
frame data equal to the Produced_Frame, and sets caps naming exactly the
frame's declared Pixel_Format.

The producer bridge is the fake-harness seam here (a stub returning the
generated frame tuple), because the divergent-inference frames — byte
lengths inconsistent with the declared format — can never leave the real
runner's ``_resolve_produced_frame`` (it enforces the length invariant);
the property is about the EXECUTOR preferring the explicit declared
format over inference regardless of the payload size. The pipeline
manager and session are the fake harness the aravis-free identity tests
established.

**Validates: Requirements 7.1, 7.2**
"""
import copy
import tempfile
import time
import uuid
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor, python_bridge
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

NODE_ID = "src1"

SOURCE_DOCUMENT = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": NODE_ID, "factory": "appsrc",
                 "args": {"name": "appsrc_{0}".format(NODE_ID)}},
                {"nodeId": NODE_ID, "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "bindingPoints": [
        {
            "nodeId": NODE_ID,
            "nodeType": "custom_python_source",
            "pythonSourceBinding": True,
            "parameters": {"allowed_uri_prefixes": ""},
            "slots": [],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}

#: Channel counts per supported Pixel_Format, and the pre-feature
#: bytes-per-pixel inference table it could disagree with.
_CHANNELS = {"GRAY8": 1, "RGB": 3, "RGBA": 4}
_INFERRED = {1: "GRAY8", 3: "RGB", 4: "RGBA"}


class _FakeProducerBridge:
    """Stub producer bridge returning the generated Produced_Frame."""

    def __init__(self, node_id, frame_tuple):
        self.node_id = node_id
        self._frame_tuple = frame_tuple
        self.fetched_sources = []
        self.produce_calls = []

    def produce_frame(self, context, allowed_uri_prefixes=()):
        self.produce_calls.append(
            (copy.deepcopy(context), tuple(allowed_uri_prefixes))
        )
        return self._frame_tuple

    def stop(self):
        pass


class _FakePipelineManager:
    """Records every run_pipeline call exactly as it was made."""

    def __init__(self):
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return {}


# --- shared per-module state (one sqlite database and artifact set) ----------

_session_factory = None
_artifact_path = None


def _shared_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    return _session_factory


def _shared_artifact_path():
    global _artifact_path
    if _artifact_path is None:
        root = tempfile.mkdtemp(prefix="pysource_caps_artifacts_")
        _artifact_path = write_artifact_set(root, compiled=SOURCE_DOCUMENT)
    return _artifact_path


def _seed_run(factory):
    registration_id = "reg-{0}".format(uuid.uuid4().hex)
    execution_id = "exec-{0}".format(uuid.uuid4().hex)
    session = factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id,
            workflow_id="wf-1",
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(_shared_artifact_path()),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id=execution_id,
            registration_id=registration_id,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return execution_id


# --- strategies ---------------------------------------------------------------


@st.composite
def _produced_frames(draw):
    """(data, width, height, format): all supported formats and dims,
    with the payload's actual channel count drawn INDEPENDENTLY of the
    declared format — so bytes-per-pixel inference frequently names a
    different format than the declared one."""
    declared_format = draw(st.sampled_from(sorted(_CHANNELS)))
    width = draw(st.integers(min_value=1, max_value=8))
    height = draw(st.integers(min_value=1, max_value=8))
    actual_channels = draw(st.sampled_from([1, 3, 4]))
    data = bytes(
        draw(st.binary(
            min_size=width * height * actual_channels,
            max_size=width * height * actual_channels,
        ))
    )
    return data, width, height, declared_format, actual_channels


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 13: The Produced_Frame is fed
# with explicit caps before the pipeline starts
#
# For any Produced_Frame (across all supported Pixel_Formats and dims —
# including dims where bytes-per-pixel inference would name a different
# format), executing a document with one Custom_Python_Source_Node points
# the node's compiled appsrc at the Frame_Feed before the pipeline runs,
# hands the pipeline manager frame data equal to the Produced_Frame, and
# sets caps naming exactly the frame's declared Pixel_Format.
#
# **Validates: Requirements 7.1, 7.2**
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(frame=_produced_frames())
def test_property_13_produced_frame_feeds_with_explicit_caps(frame):
    """**Feature: custom-python-source, Property 13: The Produced_Frame
    is fed with explicit caps before the pipeline starts**

    **Validates: Requirements 7.1, 7.2**
    """
    data, width, height, declared_format, actual_channels = frame
    factory = _shared_session_factory()
    execution_id = _seed_run(factory)

    bridge = _FakeProducerBridge(
        NODE_ID, (data, width, height, declared_format, {})
    )
    build_calls = []

    def fake_build_producer_bridge(feed, artifact_path):
        build_calls.append((feed, artifact_path))
        return bridge

    manager = _FakePipelineManager()
    capture_root = tempfile.mkdtemp(prefix="pysource_caps_captures_")
    with patch.object(
        python_bridge, "build_producer_bridge", fake_build_producer_bridge
    ), patch.object(
        pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
    ), patch.object(gst_plugins, "_scan_registry", return_value=True):
        WorkflowExecutor(
            session_factory=factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)

    # One producer invocation before the one pipeline run.
    assert len(build_calls) == 1
    assert build_calls[0][0].node_id == NODE_ID
    assert bridge.produce_calls == [({}, ())]
    assert len(manager.calls) == 1
    launch, args, kwargs = manager.calls[0]

    # The node's appsrc was pointed at the Frame_Feed before the
    # pipeline ran: renamed for run_pipeline's lookup, caps naming
    # EXACTLY the declared Pixel_Format (never the inferred one).
    assert launch == (
        "appsrc name=appsrc caps=video/x-raw,format={0} "
        "! videoconvert ! fakesink".format(declared_format)
    )
    inferred = _INFERRED[actual_channels]
    if inferred != declared_format:
        # Delimited (trailing space): "format=RGB " must not match
        # inside "format=RGBA ".
        assert "format={0} ".format(inferred) not in launch

    # The manager is handed frame data equal to the Produced_Frame.
    assert args == ({
        "data": data, "width": width, "height": height,
        "format": declared_format,
    },)
    assert set(kwargs) == {"latency_metrics", "status_sink"}

    session = factory()
    try:
        row = session.get(WorkflowExecution, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
    finally:
        session.close()
