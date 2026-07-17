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
"""Property test for Aravis-free execution identity.

**Feature: aravis-camera-input, Property 15: Aravis-free execution identity**

*For any* compiled document containing no Aravis binding point — including
every legacy document without a ``bindingPoints`` section — zero Aravis
feeds SHALL be planned and the executor SHALL invoke the pipeline manager
through the exact pre-feature call path:
``run_pipeline(launch_string, latency_metrics=...)`` with no ``frame_data``
argument, no frame grab, and the launch string rendered from the on-disk
document.

**Validates: Requirements 6.6**

Generators mirror the real Aravis-free input space: legacy documents
(``bindingPoints`` absent entirely), documents with an empty
``bindingPoints`` list, and documents whose binding points belong to
other camera families (slot-substituted ``camera_source`` points,
``adapterBinding: true`` points, ``csiSensorBinding: true`` points, and
points carrying ``aravisBinding: false``) — never ``aravisBinding: true``.
The binding-resolution provider is absent, returns ``None``, or raises
(the provider-fallback path); each variant must leave the pre-feature
call shape untouched.

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
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

from workflow_engine import gst_plugins, rendering
from workflow_engine.aravis_feed import plan_aravis_feeds
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

# --- generators --------------------------------------------------------------

#: Launch-safe factories the pre-feature executor runs unmodified (no
#: emlpython bridge rewriting, no {work_dir} resolution, no appsrc).
_FACTORIES = st.sampled_from(
    ["videotestsrc", "videoconvert", "videoscale", "queue", "fakesink"]
)

#: Launch-safe argument values (identifier-shaped, no quoting needed).
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
def _non_aravis_binding_points(draw):
    """Binding points of the other camera families — never
    ``aravisBinding: true``."""
    points = []
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(
            ["slots", "adapter", "csi", "aravis-false"]))
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
            # The marker present but not True is not an Aravis point.
            point["aravisBinding"] = draw(st.sampled_from([False, None, 0]))
        points.append(point)
    return points


@st.composite
def _aravis_free_documents(draw):
    """A compiled_pipeline.json with no Aravis binding point: the legacy
    shape (no ``bindingPoints`` at all), an empty list, or non-Aravis
    points only."""
    document = {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": draw(_segments()),
        "executorBindings": [],
        "pluginDependencies": [],
    }
    variant = draw(st.sampled_from(["legacy", "empty", "non-aravis"]))
    if variant == "empty":
        document["bindingPoints"] = []
    elif variant == "non-aravis":
        document["bindingPoints"] = draw(_non_aravis_binding_points())
    return document


#: Binding-resolution provider variants: absent (pre-feature wiring),
#: returning None (no resolution cached), raising (provider isolation).
_PROVIDER_VARIANTS = st.sampled_from(["absent", "none", "raises"])


# --- fakes -------------------------------------------------------------------


class FakePipelineManager:
    """Records every run_pipeline call exactly as made, so the
    pre-feature call shape (no frame_data argument at all) is
    distinguishable from an explicit frame push."""

    def __init__(self):
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return {}


class FakeCameraManager:
    """Callable frame grabber recording calls — must never be called."""

    def __init__(self):
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return {"data": b"\x00" * 8, "width": 4, "height": 2}


def _make_provider(variant, calls):
    if variant == "absent":
        return None
    if variant == "none":
        def provider(registration_id):
            calls.append(registration_id)
            return None
        return provider

    def raising_provider(registration_id):
        calls.append(registration_id)
        raise RuntimeError("watcher is gone")
    return raising_provider


# --- shared per-module state (one sqlite database, unique rows per example) ---

_SESSION_FACTORY = None
_IDS = itertools.count(1)


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
    return registration_id, execution_id


def _get_execution(session_factory, execution_id):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


# --- property ----------------------------------------------------------------


@given(document=_aravis_free_documents(), provider_variant=_PROVIDER_VARIANTS)
@settings(deadline=None)
def test_aravis_free_execution_identity(document, provider_variant):
    """**Feature: aravis-camera-input, Property 15: Aravis-free
    execution identity**

    **Validates: Requirements 6.6**
    """
    # Zero feeds planned, resolution or not (6.6).
    assert plan_aravis_feeds(document, None) == []

    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="aravis-free-identity-")
    try:
        artifact_path = write_artifact_set(root, compiled=document)
        registration_id, execution_id = _seed_run(
            session_factory, artifact_path, sequence
        )

        provider_calls = []
        provider = _make_provider(provider_variant, provider_calls)
        grabber = FakeCameraManager()
        manager = FakePipelineManager()
        executor = WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
            binding_resolution_provider=provider,
            frame_grabber=grabber,
        )

        with patch.object(gst_plugins, "_scan_registry", return_value=True):
            executor.execute(execution_id)

        # The camera manager is never touched (no Aravis feed exists).
        assert grabber.calls == []

        # The exact pre-feature call path: one run_pipeline call, the
        # on-disk document's rendered launch string, NO frame_data
        # positional at all, latency_metrics the only keyword.
        assert len(manager.calls) == 1
        launch, args, kwargs = manager.calls[0]
        assert launch == rendering.render_launch_string(document)
        assert args == ()
        assert set(kwargs) == {"latency_metrics"}

        # A consulted provider is called with the registration id; its
        # absence, None, or failure never alters the run's outcome.
        if provider is not None:
            assert provider_calls == [registration_id]
        row = _get_execution(session_factory, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED
    finally:
        shutil.rmtree(root, ignore_errors=True)
