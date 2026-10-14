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
"""Property test for source-free execution identity (Task 10.4).

**Feature: custom-python-source, Property 14: Source-free execution
identity**

*For any* compiled document declaring no Custom_Python_Source_Node —
including pre-feature documents with no ``bindingPoints`` section and
documents with Aravis or camera points only — ``plan_python_sources``
returns ``[]`` and the executor produces the same pipeline invocation,
execution row, node status, and persisted Run_Metadata as the
pre-feature executor, apart from the seeded ``trigger`` key.

Mirrors ``test_property_aravis_free_execution_identity.py``: the
pre-feature oracle is implemented by construction over the unchanged
code paths — the plain document takes the exact
``run_pipeline(launch_string, latency_metrics=..., status_sink=...)``
call shape with no frame_data positional, and the Aravis-fed document
keeps the frame push with bytes-per-pixel-INFERRED caps (an Aravis grab
never sets ``format``, so the explicit-caps preference never engages).
The only allowed Run_Metadata delta is the seeded ``trigger`` key.

**Validates: Requirements 7.3, 11.1, 11.5**
"""
import itertools
import json
import shutil
import tempfile
import time
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor, rendering
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.node_status import STATUS_SUCCESS
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.python_source import plan_python_sources

# --- generators --------------------------------------------------------------

#: Launch-safe factories the pre-feature executor runs unmodified.
_FACTORIES = st.sampled_from(
    ["videotestsrc", "videoconvert", "videoscale", "queue", "fakesink"]
)

_ARGS = st.dictionaries(
    keys=st.sampled_from(["num-buffers", "silent", "qos"]),
    values=st.one_of(st.integers(min_value=0, max_value=30), st.booleans()),
    max_size=2,
)


@st.composite
def _segments(draw):
    """1..2 segments of 1..3 elements each — always a non-empty render."""
    segments = []
    node_counter = itertools.count(1)
    for index in range(draw(st.integers(min_value=1, max_value=2))):
        elements = []
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            node_id = (
                "n{0}".format(next(node_counter))
                if draw(st.booleans()) else None
            )
            elements.append({
                "nodeId": node_id,
                "factory": draw(_FACTORIES),
                "args": draw(_ARGS),
            })
        segments.append({"name": "s{0}".format(index), "elements": elements})
    return segments


@st.composite
def _non_source_binding_points(draw):
    """Binding points that plan zero Python sources: camera-family
    points and points whose ``pythonSourceBinding`` marker is present
    but not True."""
    points = []
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(
            ["slots", "adapter", "csi", "python-false"]))
        point = {
            "nodeId": "cam-n{0}".format(index),
            "nodeType": "camera_source",
            "parameters": {"device": "/dev/video{0}".format(index)},
            "slots": [],
        }
        if kind == "slots":
            point["slots"] = [{"param": "device", "segment": 0,
                               "element": 0, "arg": "device"}]
        elif kind == "adapter":
            point["adapterBinding"] = True
        elif kind == "csi":
            point["csiSensorBinding"] = True
        else:
            # The marker present but not True is not a Python source.
            point["pythonSourceBinding"] = draw(
                st.sampled_from([False, None, 0]))
        points.append(point)
    return points


@st.composite
def _source_free_documents(draw):
    """(document, has_aravis): a compiled_pipeline.json with no Custom
    Python source binding point — the legacy shape (no ``bindingPoints``
    at all), an empty list, non-source points only, or exactly one
    Aravis point (the fed pre-feature camera family)."""
    document = {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": draw(_segments()),
        "executorBindings": [],
        "pluginDependencies": [],
    }
    variant = draw(st.sampled_from(
        ["legacy", "empty", "non-source", "aravis"]))
    if variant == "empty":
        document["bindingPoints"] = []
    elif variant == "non-source":
        document["bindingPoints"] = draw(_non_source_binding_points())
    elif variant == "aravis":
        document["segments"].append({
            "name": "s-aravis",
            "elements": [
                {"nodeId": "cam1", "factory": "appsrc",
                 "args": {"name": "appsrc_cam1"}},
                {"nodeId": "cam1", "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        })
        document["bindingPoints"] = [{
            "nodeId": "cam1",
            "nodeType": "aravis_camera_source",
            "parameters": {"camera_id": "Aravis-Fake-GV01",
                           "gain": 4, "exposure": 5000000},
            "slots": [],
            "aravisBinding": True,
        }]
    return document, variant == "aravis"


# --- fakes -------------------------------------------------------------------


class FakePipelineManager:
    """Records every run_pipeline call exactly as made, so the
    pre-feature call shape (no frame_data positional at all) is
    distinguishable from an explicit frame push."""

    def __init__(self, tag_values):
        self.tag_values = tag_values
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return dict(self.tag_values)


class FakeCameraManager:
    """Callable frame grabber. A 1-byte-per-pixel frame keeps the Aravis
    caps on the bytes-per-pixel-inference path (GRAY8) — the pre-feature
    oracle for Requirement 11.1."""

    def __init__(self):
        self.frame = {"data": b"\x00" * 8, "width": 4, "height": 2}
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return self.frame


# --- shared per-module state -------------------------------------------------

_SESSION_FACTORY = None
_IDS = itertools.count(1)

#: Fixed pipeline-produced tag values; the Run_Metadata identity check
#: is over the handed dict, whose only allowed delta is ``trigger``.
_TAG_VALUES = {"is_anomalous": False}


def _session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = make_session_factory()
    return _SESSION_FACTORY


def _seed_run(session_factory, artifact_path, sequence):
    registration_id = "wf-1:3:{0}".format(sequence)
    execution_id = "exec-{0}".format(sequence)
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id,
            workflow_id="wf-1",
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(artifact_path),
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


def _get_execution(session_factory, execution_id):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 14: Source-free execution identity
#
# For any compiled document declaring no Custom_Python_Source_Node
# (including pre-feature documents with no bindingPoints section and
# documents with Aravis or camera points only), plan_python_sources
# returns [] and the executor produces the same pipeline invocation,
# execution row, node status, and persisted Run_Metadata as the
# pre-feature executor, apart from the seeded trigger key.
#
# **Validates: Requirements 7.3, 11.1, 11.5**
# ---------------------------------------------------------------------------


@settings(deadline=None)
@given(document_and_variant=_source_free_documents())
def test_property_14_source_free_execution_identity(document_and_variant):
    """**Feature: custom-python-source, Property 14: Source-free
    execution identity**

    **Validates: Requirements 7.3, 11.1, 11.5**
    """
    document, has_aravis = document_and_variant

    # Zero Python sources planned for every source-free shape (7.3).
    assert plan_python_sources(document) == []

    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="pysource-free-identity-")
    try:
        artifact_path = write_artifact_set(root, compiled=document)
        execution_id = _seed_run(session_factory, artifact_path, sequence)

        grabber = FakeCameraManager()
        manager = FakePipelineManager(_TAG_VALUES)
        observed = []
        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            frame_grabber=grabber,
            post_run_handler=(
                lambda registration, doc, tags: observed.append(tags)
            ),
        )

        capture_root = tempfile.mkdtemp(prefix="pysource-free-captures-")
        with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
        ), patch.object(gst_plugins, "_scan_registry", return_value=True):
            executor.execute(execution_id)

        # The same pipeline invocation as the pre-feature executor.
        assert len(manager.calls) == 1
        launch, args, kwargs = manager.calls[0]
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        if has_aravis:
            # The Aravis Frame_Feed path is bit-identical: the grabbed
            # frame is pushed and its caps come from bytes-per-pixel
            # inference (an Aravis grab never sets 'format' — 11.1).
            assert grabber.calls == [
                ("Aravis-Fake-GV01", {"gain": 4, "exposure": 5000000})
            ]
            assert args == (grabber.frame,)
            assert "appsrc name=appsrc caps=video/x-raw,format=GRAY8" \
                in launch
        else:
            # The exact pre-feature call path: the on-disk document's
            # rendered launch string, NO frame_data positional at all.
            assert grabber.calls == []
            assert launch == rendering.render_launch_string(document)
            assert args == ()

        # The execution row reaches the same terminal state.
        row = _get_execution(session_factory, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.failing_node_id is None

        # The node status covers exactly the document's nodes, all
        # success — as the pre-feature executor persists it.
        expected_nodes = {
            element["nodeId"]
            for segment in document["segments"]
            for element in segment["elements"]
            if element["nodeId"] is not None
        }
        status = json.loads(row.node_status_json)
        assert set(status) == expected_nodes
        assert all(
            entry["status"] == STATUS_SUCCESS for entry in status.values()
        )

        # The Run_Metadata handed to the post-run pipeline (and
        # persisted by _persist_run_metadata) differs from the
        # pipeline's own tag values ONLY by the seeded trigger key.
        assert observed == [dict(_TAG_VALUES, trigger={})]
    finally:
        shutil.rmtree(root, ignore_errors=True)
