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
"""Property test for execution identity without the new node types.

**Feature: rtsp-rtmp-stream-cameras, Property 28: Execution identity
without the new node types**

*For any* compiled document without stream binding points or analytics
bindings, including legacy documents without ``bindingPoints``, the
executor SHALL:

- plan zero stream feeds;
- call the pipeline manager exactly as before;
- produce run metadata with no ``frame``, ``stream``, ``counter``,
  ``association``, or ``event`` keys.

**Validates: Requirements 10.7, 18.1**

Mirrors ``test_property_python_source_free_identity.py``: the pre-feature
oracle is built over the unchanged code paths. A plain document takes the
exact ``run_pipeline(launch_string, latency_metrics=..., status_sink=...)``
call with no frame positional; an Aravis-fed document keeps its frame push
with bytes-per-pixel-inferred caps. The only Run_Metadata delta allowed is
the seeded ``trigger`` key, which predates this feature. The stream
service and the configured-camera lookup must never be touched.
"""
import itertools
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
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.stream_feed import document_has_stream_or_analytics, plan_stream_feeds

# --- generators --------------------------------------------------------------

_FACTORIES = st.sampled_from(["videotestsrc", "videoconvert", "videoscale", "queue", "fakesink"])

_ARGS = st.dictionaries(
    keys=st.sampled_from(["num-buffers", "silent", "qos"]),
    values=st.one_of(st.integers(min_value=0, max_value=30), st.booleans()),
    max_size=2,
)

#: Executor bindings of the pre-feature kinds: never an analytics binding.
_PRE_FEATURE_BINDINGS = ("digital_output", "mqtt_publish", "opcua_write", "modbus_write",
                         "llm_inference", "bedrock_inference", "webhook")


@st.composite
def _segments(draw):
    segments = []
    node_counter = itertools.count(1)
    for index in range(draw(st.integers(min_value=1, max_value=2))):
        elements = []
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            node_id = "n{0}".format(next(node_counter)) if draw(st.booleans()) else None
            elements.append({"nodeId": node_id, "factory": draw(_FACTORIES), "args": draw(_ARGS)})
        segments.append({"name": "s{0}".format(index), "elements": elements})
    return segments


@st.composite
def _stream_free_points(draw):
    """Binding points of the pre-feature families, and points whose
    ``streamBinding`` marker is present but not True."""
    points = []
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(["slots", "adapter", "csi", "stream-false", "python-false"]))
        point = {"nodeId": "cam-n{0}".format(index), "nodeType": "camera_source",
                 "parameters": {"device": "/dev/video{0}".format(index)}, "slots": []}
        if kind == "slots":
            point["slots"] = [{"param": "device", "segment": 0, "element": 0, "arg": "device"}]
        elif kind == "adapter":
            point["adapterBinding"] = True
        elif kind == "csi":
            point["csiSensorBinding"] = True
        elif kind == "stream-false":
            # The marker present but not True is not a stream point, even
            # on a stream node type.
            point["nodeType"] = draw(st.sampled_from(["rtsp_camera_source", "rtmp_stream_source"]))
            point["parameters"] = {"url": "rtsp://192.168.1.64/stream"}
            point["streamBinding"] = draw(st.sampled_from([False, None, 0, "true"]))
        else:
            point["pythonSourceBinding"] = draw(st.sampled_from([False, None, 0]))
        points.append(point)
    return points


@st.composite
def _stream_free_documents(draw):
    """(document, has_aravis): no stream binding point and no analytics
    binding — the legacy shape, an empty list, pre-feature points only, or
    exactly one Aravis point."""
    document = {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": draw(_segments()),
        "executorBindings": [],
        "pluginDependencies": [],
    }
    variant = draw(st.sampled_from(["legacy", "empty", "pre-feature", "aravis"]))
    if variant == "empty":
        document["bindingPoints"] = []
    elif variant == "pre-feature":
        document["bindingPoints"] = draw(_stream_free_points())
    elif variant == "aravis":
        document["segments"].append({"name": "s-aravis", "elements": [
            {"nodeId": "cam1", "factory": "appsrc", "args": {"name": "appsrc_cam1"}},
            {"nodeId": "cam1", "factory": "videoconvert", "args": {}},
            {"nodeId": None, "factory": "fakesink", "args": {}}]})
        document["bindingPoints"] = [{
            "nodeId": "cam1", "nodeType": "aravis_camera_source",
            "parameters": {"camera_id": "Aravis-Fake-GV01", "gain": 4, "exposure": 5000000},
            "slots": [], "aravisBinding": True}]
    return document, variant == "aravis"


# --- fakes -------------------------------------------------------------------


class FakePipelineManager:
    def __init__(self, tag_values):
        self.tag_values = tag_values
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return dict(self.tag_values)


class FakeCameraManager:
    def __init__(self):
        self.frame = {"data": b"\x00" * 8, "width": 4, "height": 2}
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return self.frame


class UntouchableStreamManager:
    """Records any use; a stream-free run must never use the service."""

    def __init__(self):
        self.used = []

    def __getattr__(self, name):
        self.used.append(name)
        raise AssertionError("the stream service was used: {0}".format(name))


# --- shared per-module state ---------------------------------------------------

_SESSION_FACTORY = None
_IDS = itertools.count(1)
_TAG_VALUES = {"is_anomalous": False}
_NEW_KEYS = ("frame", "stream", "counter", "association", "event")


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
            id=registration_id, workflow_id="wf-1", version="3", arch=DEVICE_ARCH,
            artifact_path=str(artifact_path), status="registered", registered_at=int(time.time())))
        session.add(WorkflowExecution(
            id=execution_id, registration_id=registration_id, started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING))
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


# --- properties ------------------------------------------------------------------


@st.composite
def _bindings(draw):
    return [{"nodeId": "o{0}".format(index), "binding": draw(st.sampled_from(_PRE_FEATURE_BINDINGS)),
             "parameters": {}} for index in range(draw(st.integers(min_value=0, max_value=4)))]


#: ``streamBinding`` values that are not ``True``: truthy ones included,
#: since the packager's marker is the boolean itself.
_NOT_TRUE = st.sampled_from([False, None, 0, 1, "true", "yes", [True], {"on": True}])


@given(document_and_variant=_stream_free_documents(), bindings=_bindings(), marker=_NOT_TRUE,
       node_type=st.sampled_from(["rtsp_camera_source", "rtmp_stream_source"]))
def test_stream_free_documents_plan_nothing(document_and_variant, bindings, marker, node_type):
    """Zero stream feeds, and no ``frame`` seeding, for every stream-free
    document with any pre-feature executor bindings, even beside a stream
    node type whose marker is not ``True``."""
    document, _has_aravis = document_and_variant
    points = list(document.get("bindingPoints") or [])
    points.append({"nodeId": "not-fed", "nodeType": node_type,
                   "parameters": {"url": "rtsp://192.168.1.64/stream"}, "slots": [],
                   "streamBinding": marker})
    document = dict(document, executorBindings=bindings, bindingPoints=points)

    assert plan_stream_feeds(document, None) == []
    assert document_has_stream_or_analytics(document) is False


@settings(deadline=None)
@given(document_and_variant=_stream_free_documents())
def test_execution_identity_without_the_new_node_types(document_and_variant):
    """**Feature: rtsp-rtmp-stream-cameras, Property 28: Execution identity
    without the new node types**

    **Validates: Requirements 10.7, 18.1**
    """
    document, has_aravis = document_and_variant
    assert plan_stream_feeds(document, None) == []

    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="stream-free-identity-")
    capture_root = tempfile.mkdtemp(prefix="stream-free-captures-")
    try:
        artifact_path = write_artifact_set(root, compiled=document)
        execution_id = _seed_run(session_factory, artifact_path, sequence)

        grabber = FakeCameraManager()
        manager = FakePipelineManager(_TAG_VALUES)
        stream_manager = UntouchableStreamManager()
        created = []
        lookups = []
        observed = []
        # The production default: no injected manager, so the executor
        # would create the process-wide one on first use.
        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            frame_grabber=grabber,
            stream_camera_resolver=lambda session: lookups.append(session) or {},
            post_run_handler=lambda registration, doc, tags: observed.append(tags),
        )
        with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root), \
                patch.object(gst_plugins, "_scan_registry", return_value=True), \
                patch("stream_ingest.manager.get_stream_ingest_manager",
                      lambda: created.append(1) or stream_manager):
            executor.execute(execution_id)

        # The stream service is never created or used, and the camera
        # lookup never runs.
        assert created == []
        assert stream_manager.used == []
        assert lookups == []

        # The same pipeline invocation as before the feature.
        assert len(manager.calls) == 1
        launch, args, kwargs = manager.calls[0]
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        if has_aravis:
            assert args == (grabber.frame,)
            assert "appsrc name=appsrc caps=video/x-raw,format=GRAY8" in launch
        else:
            assert grabber.calls == []
            assert launch == rendering.render_launch_string(document)
            assert args == ()

        row = _get_execution(session_factory, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
        assert row.failing_node_id is None

        # The Run_Metadata gains none of the new keys: its only delta from
        # the pipeline's tag values is the pre-feature ``trigger`` seed.
        assert observed == [dict(_TAG_VALUES, trigger={})]
        assert not set(observed[0]) & set(_NEW_KEYS)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(capture_root, ignore_errors=True)
