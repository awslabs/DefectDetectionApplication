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
"""Executor feed-planning and failure-path wiring tests (task 7.1).

Feature: static-image-camera-source (Requirements 4.2, 4.5, 5.6).

The static camera id must flow through the UNCHANGED workflow plumbing:

* ``camera_binding.resolve_bindings`` fills ``aravis_assignments`` for a
  ``cameraSourceId`` resolving to ``static-image-camera`` exactly as for
  a physical id — no special-casing, no document modification (Req 4.5);
* ``plan_aravis_feeds`` yields ``AravisFeed(camera_id="static-image-camera")``
  with no node catalog addition (Req 4.2, 4.5);
* a ``WorkflowExecutor`` run resolved to the static camera grabs the real
  Pinned_Image through the real ``camera_manager.get_camera_frame``
  short-circuit and pushes it into the compiled pipeline through the
  existing Frame_Feed (``run_pipeline(launch_string, frame_data)``) with
  RGB caps, proceeding to completion (Req 4.2);
* a run whose static grab raises (no pinned image) fails with
  ``failing_node_id`` set on the Aravis node and the error naming the
  Static_Image_Camera, leaving other runs unaffected (Req 5.6).

Document shapes and the executor harness mirror
``test/backend-test/workflow_engine/test_workflow_aravis_feed.py`` /
``test_workflow_aravis_executor.py``.
"""
import copy
import io
import os
import sys
import time
from unittest.mock import patch

import pytest
from PIL import Image

# The workflow engine test helpers live in the sibling directory; pytest
# only puts a test file's own directory on sys.path (same shim as
# test/backend-test/output_bindings_fixes/conftest.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_TESTS = os.path.join(os.path.dirname(_HERE), "workflow_engine")
if _ENGINE_TESTS not in sys.path:
    sys.path.insert(0, _ENGINE_TESTS)

from workflow_engine_test_utils import (  # noqa: E402
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from camera_sync import CameraSourceState  # noqa: E402
from workflow_engine import gst_plugins  # noqa: E402
from workflow_engine.aravis_feed import AravisFeed, plan_aravis_feeds  # noqa: E402
from workflow_engine.camera_binding import (  # noqa: E402
    STATUS_RESOLVED,
    resolve_bindings,
)
from workflow_engine.models import WorkflowExecution, WorkflowRegistration  # noqa: E402
from workflow_engine.pipeline_executor import (  # noqa: E402
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)
from workflow_engine.vendor.workflow_core.catalog.nodes import NODE_CATALOG  # noqa: E402

from utils.static_image_camera import (  # noqa: E402
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)

from camera_manager_support import import_camera_manager  # noqa: E402
from static_image_strategies import expected_frame  # noqa: E402

REGISTRATION_ID = "wf-1:3"


def make_image_bytes(img_format="PNG", width=6, height=4, color=(12, 200, 99)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=img_format)
    return buffer.getvalue()


def make_aravis_document(node_id="n1", parameters=None):
    """A compiled_pipeline.json with one Aravis camera source: the compiled
    appsrc chain plus the packager's aravisBinding point (the exact shape
    from test_workflow_aravis_executor.py)."""
    if parameters is None:
        parameters = {"camera_id": "Aravis-Fake-GV01",
                      "gain": 4, "exposure": 5000000}
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
                    {"nodeId": node_id, "factory": "videoconvert", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "bindingPoints": [
            {
                "nodeId": node_id,
                "nodeType": "aravis_camera_source",
                "parameters": dict(parameters),
                "slots": [],
                "aravisBinding": True,
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def make_inventory_entry(camera_source_id, camera_id):
    """A configured Camera-type entry from ``build_inventory``, params
    carrying the Aravis ``cameraId`` (the inventory spelling)."""
    return CameraSourceState(
        camera_source_id=camera_source_id,
        name="bench cam",
        type="Camera",
        origin="edge-configured",
        params={"cameraId": camera_id},
    )


def resolve_static_binding(document, node_id="n1"):
    """resolve_bindings with a cameraSourceId whose inventory entry is the
    Static_Image_Camera — the exact path a Portal Camera_Binding takes."""
    inventory = {"cfg-static": make_inventory_entry(
        "cfg-static", STATIC_IMAGE_CAMERA_ID)}
    return resolve_bindings(
        document, {node_id: {"cameraSourceId": "cfg-static"}}, inventory)


class FakePipelineManager:
    """Records every run_pipeline call exactly as it was made."""

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


@pytest.fixture
def pinned_store(tmp_path):
    store = StaticImageStore(base_dir=str(tmp_path / "store"))
    store.image_bytes_for_test = make_image_bytes()
    store.pin_bytes(store.image_bytes_for_test, "workflow-input.png")
    return store


@pytest.fixture
def empty_store(tmp_path):
    return StaticImageStore(base_dir=str(tmp_path / "store"))


def real_grabber(store):
    """A frame grabber delegating to the REAL camera_manager short-circuit
    (the executor's production path) over the given store."""
    cm = import_camera_manager()

    def grab(camera_id, config):
        with patch.object(cm, "get_static_image_store", lambda: store):
            return cm.get_camera_frame(camera_id, config)

    return grab


def seed_run(session_factory, artifact_path, execution_id="exec-1"):
    session = session_factory()
    try:
        if session.get(WorkflowRegistration, REGISTRATION_ID) is None:
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
                id=execution_id,
                registration_id=REGISTRATION_ID,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            )
        )
        session.commit()
    finally:
        session.close()
    return execution_id


def get_execution(session_factory, execution_id):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def make_executor(session_factory, manager, grabber=None, provider=None):
    return WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        binding_resolution_provider=provider,
        frame_grabber=grabber,
    )


# ---------------------------------------------------------------------------
# Req 4.2 / 4.5 — binding resolution and feed planning, no special-casing
# ---------------------------------------------------------------------------


class TestBindingResolutionAndFeedPlanning:
    def test_resolve_bindings_fills_aravis_assignment_for_static_id(self):
        """A cameraSourceId resolving to the static camera fills
        aravis_assignments through the standard path (Req 4.5)."""
        document = make_aravis_document()
        snapshot = copy.deepcopy(document)

        result = resolve_static_binding(document)

        assert result.status == STATUS_RESOLVED
        assert result.missing == ()
        assignment = result.aravis_assignments["n1"]
        assert assignment["cameraSourceId"] == "cfg-static"
        assert assignment["params"]["cameraId"] == STATIC_IMAGE_CAMERA_ID
        assert assignment["params"]["camera_id"] == STATIC_IMAGE_CAMERA_ID
        # No slot substitution, no document modification (Req 4.5).
        assert result.document["segments"] == document["segments"]
        assert document == snapshot

    def test_static_resolution_is_structurally_identical_to_physical(self):
        """No special-casing: the resolution for the static id equals the
        resolution for a physical id modulo the camera id string."""
        physical_id = "Basler-12345678"

        def resolve_with(camera_id):
            inventory = {"cfg-1": make_inventory_entry("cfg-1", camera_id)}
            return resolve_bindings(
                make_aravis_document(),
                {"n1": {"cameraSourceId": "cfg-1"}},
                inventory,
            )

        static_result = resolve_with(STATIC_IMAGE_CAMERA_ID)
        physical_result = resolve_with(physical_id)

        def normalized(result, camera_id):
            assignment = copy.deepcopy(result.aravis_assignments)
            for entry in assignment.values():
                for key, value in list(entry["params"].items()):
                    if value == camera_id:
                        entry["params"][key] = "<CAMERA_ID>"
            return (result.status, result.missing, result.errors,
                    result.adapter_assignments, assignment)

        assert normalized(static_result, STATIC_IMAGE_CAMERA_ID) == \
            normalized(physical_result, physical_id)

    def test_plan_aravis_feeds_yields_static_feed(self):
        """Req 4.2: the planner produces the expected AravisFeed for the
        static camera with no document modification."""
        document = make_aravis_document()
        snapshot = copy.deepcopy(document)
        resolution = resolve_static_binding(document)

        feeds = plan_aravis_feeds(document, resolution)

        assert feeds == [AravisFeed(
            node_id="n1", camera_id=STATIC_IMAGE_CAMERA_ID, config={})]
        assert document == snapshot

    def test_no_node_catalog_addition(self):
        """Req 4.5: the static camera rides the existing
        aravis_camera_source node type — the catalog carries no
        static-image node."""
        type_ids = {descriptor.type_id for descriptor in NODE_CATALOG}
        assert "aravis_camera_source" in type_ids
        assert not any("static" in type_id for type_id in type_ids)


# ---------------------------------------------------------------------------
# Req 4.2 — executor run feeds the Pinned_Image through the Frame_Feed
# ---------------------------------------------------------------------------


class TestExecutorStaticFrameFeed:
    def test_run_feeds_pinned_image_and_completes(
        self, tmp_path, session_factory, pinned_store
    ):
        """A run resolved to the static camera grabs the real Pinned_Image
        through the real camera_manager short-circuit and pushes it into
        the compiled pipeline with RGB caps; the run completes."""
        document = make_aravis_document()
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        resolution = resolve_static_binding(document)
        manager = FakePipelineManager()

        make_executor(
            session_factory,
            manager,
            grabber=real_grabber(pinned_store),
            provider=lambda registration_id: resolution,
        ).execute(execution_id)

        row = get_execution(session_factory, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED, row.error
        assert len(manager.calls) == 1
        launch, args, _ = manager.calls[0]
        # The Frame_Feed appsrc carries truthful RGB caps derived from the
        # frame's pixel_format tag.
        assert "appsrc name=appsrc caps=video/x-raw,format=RGB " in launch
        # The pushed frame IS the decoded Pinned_Image, byte for byte.
        assert args == (expected_frame(pinned_store.image_bytes_for_test),)

    def test_grab_config_is_accepted_and_frame_unchanged(
        self, tmp_path, session_factory, pinned_store
    ):
        """Rendered gain/exposure parameters flow into the grab config and
        are accepted (and ignored) by the static short-circuit (Req 3.4
        via the executor path)."""
        document = make_aravis_document(parameters={
            "camera_id": STATIC_IMAGE_CAMERA_ID,
            "gain": 7, "exposure": 900000,
        })
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        # No provider: the rendered parameters drive the grab.
        make_executor(
            session_factory, manager, grabber=real_grabber(pinned_store)
        ).execute(execution_id)

        row = get_execution(session_factory, execution_id)
        assert row.status == EXECUTION_STATUS_COMPLETED, row.error
        _, args, _ = manager.calls[0]
        assert args == (expected_frame(pinned_store.image_bytes_for_test),)


# ---------------------------------------------------------------------------
# Req 5.6 — grab failure fails only that run, naming the static camera
# ---------------------------------------------------------------------------


class TestExecutorStaticGrabFailure:
    def test_unpinned_grab_fails_run_with_node_and_camera_name(
        self, tmp_path, session_factory, empty_store
    ):
        """A run whose static grab raises (no Pinned_Image) fails with
        failing_node_id on the Aravis node and the error naming the
        Static_Image_Camera; the pipeline never starts and other runs are
        unaffected."""
        document = make_aravis_document()
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        failing_id = seed_run(session_factory, artifact_path, "exec-1")
        bystander_id = seed_run(session_factory, artifact_path, "exec-2")
        resolution = resolve_static_binding(document)
        manager = FakePipelineManager()

        make_executor(
            session_factory,
            manager,
            grabber=real_grabber(empty_store),
            provider=lambda registration_id: resolution,
        ).execute(failing_id)

        assert manager.calls == []
        row = get_execution(session_factory, failing_id)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "n1"
        assert STATIC_IMAGE_CAMERA_ID in row.error
        assert "no usable pinned image" in row.error
        assert row.finished_at is not None
        # Other runs' execution and status are unchanged (Req 5.6).
        bystander = get_execution(session_factory, bystander_id)
        assert bystander.status == EXECUTION_STATUS_PENDING
        assert bystander.error is None

    def test_pin_after_failure_lets_the_next_run_complete(
        self, tmp_path, session_factory, empty_store
    ):
        """The failure is contained to the run: pinning an image lets a
        subsequent run over the same registration complete normally."""
        document = make_aravis_document()
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        first = seed_run(session_factory, artifact_path, "exec-1")
        second = seed_run(session_factory, artifact_path, "exec-2")
        resolution = resolve_static_binding(document)
        manager = FakePipelineManager()
        executor = make_executor(
            session_factory,
            manager,
            grabber=real_grabber(empty_store),
            provider=lambda registration_id: resolution,
        )

        executor.execute(first)
        assert get_execution(session_factory, first).status == \
            EXECUTION_STATUS_FAILED

        data = make_image_bytes("JPEG", width=5, height=3)
        empty_store.pin_bytes(data, "recovered.jpg")
        executor.execute(second)

        row = get_execution(session_factory, second)
        assert row.status == EXECUTION_STATUS_COMPLETED, row.error
        _, args, _ = manager.calls[0]
        assert args == (expected_frame(data),)
