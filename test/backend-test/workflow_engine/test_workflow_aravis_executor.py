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
"""WorkflowExecutor Aravis frame feed wiring tests.

Feature: aravis-camera-input (Requirements 6.4, 6.5, 6.6).

A fake camera manager and a fake pipeline manager exercise the executor's
binding-resolution provider, feed planning, frame grab, and Frame_Feed
push without GStreamer or the gi/Aravis runtime:

- a resolved Aravis assignment grabs from the resolved camera and pushes
  the frame through ``run_pipeline(launch_string, frame_data)``, running
  the resolution's substituted document;
- with no provider (or an unbound resolution) the binding point's
  rendered default parameters drive the grab;
- a raising camera manager fails the execution with ``failing_node_id``
  set to the Aravis node and the camera error;
- a raising provider falls back to the on-disk document;
- Aravis-free documents take the exact pre-feature call shape (no
  frame_data argument at all).
"""
import time
from unittest.mock import patch

import pytest

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins
from workflow_engine.camera_binding import STATUS_RESOLVED, ResolutionResult
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

REGISTRATION_ID = "wf-1:3"


def make_aravis_document(node_id="n1", parameters=None, extra_points=()):
    """A compiled_pipeline.json with one Aravis camera source: the
    compiled appsrc chain plus the packager's aravisBinding point."""
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
        ] + list(extra_points),
        "executorBindings": [],
        "pluginDependencies": [],
    }


PLAIN_DOC = {
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
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}


def make_frame(width=4, height=2, bytes_per_pixel=1):
    return {
        "data": b"\x00" * (width * height * bytes_per_pixel),
        "width": width,
        "height": height,
    }


class FakePipelineManager:
    """Records every run_pipeline call exactly as it was made, so the
    pre-feature call shape (no frame_data argument at all) is
    distinguishable from an explicit frame push."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


class FakeCameraManager:
    """Callable frame grabber recording (camera_id, config) calls."""

    def __init__(self, frame=None, error=None):
        self.frame = frame if frame is not None else make_frame()
        self.error = error
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        if self.error is not None:
            raise self.error
        return self.frame


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_run(session_factory, artifact_path, status="registered"):
    session = session_factory()
    try:
        session.add(
            WorkflowRegistration(
                id=REGISTRATION_ID,
                workflow_id="wf-1",
                version="3",
                arch=DEVICE_ARCH,
                artifact_path=str(artifact_path),
                status=status,
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
    return "exec-1"


def get_execution(session_factory, execution_id="exec-1"):
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


class TestResolvedAssignmentFeed:
    def test_resolved_assignment_drives_grab_and_frame_push(
        self, tmp_path, session_factory
    ):
        """Requirement 6.4: the provider's resolution supplies both the
        substituted document and the Aravis assignment; the grab uses the
        resolved camera id/config and the frame goes through
        run_pipeline(launch_string, frame_data)."""
        disk_doc = make_aravis_document()
        artifact_path = write_artifact_set(tmp_path, compiled=disk_doc)
        execution_id = seed_run(session_factory, artifact_path)

        # The substituted document differs from disk so the launch string
        # proves which document ran.
        substituted = make_aravis_document()
        substituted["segments"][0]["elements"][2]["args"] = {"silent": True}
        resolution = ResolutionResult(
            document=substituted,
            status=STATUS_RESOLVED,
            aravis_assignments={
                "n1": {
                    "cameraSourceId": "cfg-is-1",
                    "params": {"camera_id": "Basler-12345678",
                               "gain": 20, "exposure": 8000000},
                }
            },
        )
        provider_calls = []

        def provider(registration_id):
            provider_calls.append(registration_id)
            return resolution

        frame = make_frame(width=8, height=4)
        grabber = FakeCameraManager(frame=frame)
        manager = FakePipelineManager()

        make_executor(
            session_factory, manager, grabber=grabber, provider=provider
        ).execute(execution_id)

        assert provider_calls == [REGISTRATION_ID]
        assert grabber.calls == [
            ("Basler-12345678", {"gain": 20, "exposure": 8000000})
        ]
        assert len(manager.calls) == 1
        launch, args, kwargs = manager.calls[0]
        # The Frame_Feed appsrc: named for run_pipeline's lookup, base
        # caps derived from the frame (width/height are appended from the
        # frame inside run_pipeline, the classic Camera-type model).
        assert launch == (
            "appsrc name=appsrc caps=video/x-raw,format=GRAY8 "
            "! videoconvert ! fakesink silent=true"
        )
        assert args == (frame,)
        # The executor threads the per-node status collector's sink through
        # the Aravis Frame_Feed run alongside the latency metrics
        # (deployed-workflow-run-observability R3).
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED

    def test_watcher_cached_resolution_document_is_not_mutated(
        self, tmp_path, session_factory
    ):
        """The executor's appsrc rewrite happens on a private copy —
        the provider's cached document keeps its compiled shape."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        resolution = ResolutionResult(
            document=make_aravis_document(), status=STATUS_RESOLVED
        )

        make_executor(
            session_factory,
            FakePipelineManager(),
            grabber=FakeCameraManager(),
            provider=lambda registration_id: resolution,
        ).execute(execution_id)

        appsrc_args = resolution.document["segments"][0]["elements"][0]["args"]
        assert appsrc_args == {"name": "appsrc_n1"}


class TestRenderedDefaultFeed:
    def test_no_provider_grabs_with_rendered_default_parameters(
        self, tmp_path, session_factory
    ):
        """Requirement 6.4: without a provider the binding point's
        rendered camera_id/gain/exposure drive the grab."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager()
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        assert grabber.calls == [
            ("Aravis-Fake-GV01", {"gain": 4, "exposure": 5000000})
        ]
        launch, args, kwargs = manager.calls[0]
        assert args == (grabber.frame,)
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED

    def test_rgb_frame_derives_rgb_caps(self, tmp_path, session_factory):
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager(frame=make_frame(bytes_per_pixel=3))
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        launch = manager.calls[0][0]
        assert "caps=video/x-raw,format=RGB " in launch

    def test_bayer_tagged_frame_gets_bayer_caps_and_demosaic(
        self, tmp_path, session_factory
    ):
        """A camera-tagged Bayer frame (camera_manager pixel_format) must
        NOT be mislabeled GRAY8 (it is 1 byte/pixel, indistinguishable by
        size): the appsrc gets video/x-bayer caps and a bayer2rgb demosaic
        is inserted before the compiled videoconvert, mirroring the classic
        Image_Source conversion chain."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        frame = make_frame(bytes_per_pixel=1)
        frame["pixel_format"] = "bayer:bggr"
        grabber = FakeCameraManager(frame=frame)
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        launch = manager.calls[0][0]
        assert (
            "appsrc name=appsrc caps=video/x-bayer,format=bggr "
            "! bayer2rgb ! videoconvert" in launch
        )

    def test_local_image_source_configuration_drives_the_grab(
        self, tmp_path, session_factory
    ):
        """A camera with a locally configured Image_Source grabs with THAT
        configuration (gain/exposure/advanced GenICam settings) — the
        workflow always respects the device's own camera settings; the
        planned binding/rendered parameters only apply to cameras without
        a local Image_Source."""
        from dao.sqlite_db.models import (
            ImageSource as ImageSourceRow,
            ImageSourceConfiguration as ImageSourceConfigurationRow,
        )

        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)

        session = session_factory()
        try:
            session.add(ImageSourceConfigurationRow(
                imageSourceConfigId="isc-1", gain=17, exposure=250000,
                advancedSettings={"reverseX": True},
            ))
            session.add(ImageSourceRow(
                imageSourceId="is-1", name="Basler bench",
                cameraId="Aravis-Fake-GV01", imageSourceConfigId="isc-1",
            ))
            session.commit()
        finally:
            session.close()

        grabber = FakeCameraManager()
        manager = FakePipelineManager()
        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        assert len(grabber.calls) == 1
        camera_id, config = grabber.calls[0]
        assert camera_id == "Aravis-Fake-GV01"
        # The local Image_Source configuration, not the rendered
        # {gain: 4, exposure: 5000000} defaults.
        assert config["gain"] == 17
        assert config["exposure"] == 250000
        assert config["advancedSettings"] == {"reverseX": True}
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED

    def test_raw_tagged_frame_uses_the_tagged_format(
        self, tmp_path, session_factory
    ):
        """A camera-tagged raw frame names its actual format even when the
        bytes-per-pixel guess would agree (Mono8) or disagree (packed)."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        frame = make_frame(bytes_per_pixel=4)
        frame["pixel_format"] = "BGRA"
        grabber = FakeCameraManager(frame=frame)
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        launch = manager.calls[0][0]
        assert "caps=video/x-raw,format=BGRA " in launch
        assert "bayer2rgb" not in launch


class TestGrabFailure:
    def test_raising_camera_manager_fails_with_the_aravis_node(
        self, tmp_path, session_factory
    ):
        """Requirement 6.5: a grab failure fails the execution with
        failing_node_id set to the Aravis node and the camera error;
        the pipeline never starts."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager(
            error=Exception(
                "Unable to get camera frame for camera id: Aravis-Fake-GV01"
            )
        )
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert row.failing_node_id == "n1"
        assert "Unable to get camera frame" in row.error
        assert row.finished_at is not None

    def test_planning_error_fails_before_the_pipeline_starts(
        self, tmp_path, session_factory
    ):
        """Two Aravis points violate the single Frame_Feed contract:
        the run fails with the planner's reason, no grab, no pipeline."""
        document = make_aravis_document(extra_points=[{
            "nodeId": "n7",
            "nodeType": "aravis_camera_source",
            "parameters": {"camera_id": "cam-b"},
            "slots": [],
            "aravisBinding": True,
        }])
        artifact_path = write_artifact_set(tmp_path, compiled=document)
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager()
        manager = FakePipelineManager()

        make_executor(session_factory, manager, grabber=grabber).execute(
            execution_id
        )

        assert grabber.calls == []
        assert manager.calls == []
        row = get_execution(session_factory)
        assert row.status == EXECUTION_STATUS_FAILED
        assert "n1" in row.error and "n7" in row.error


class TestProviderFallback:
    def test_raising_provider_falls_back_to_the_disk_document(
        self, tmp_path, session_factory
    ):
        """A provider failure never takes the run down: the on-disk
        document runs on its rendered defaults."""
        artifact_path = write_artifact_set(
            tmp_path, compiled=make_aravis_document()
        )
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager()
        manager = FakePipelineManager()

        def broken_provider(registration_id):
            raise RuntimeError("watcher is gone")

        make_executor(
            session_factory, manager, grabber=grabber, provider=broken_provider
        ).execute(execution_id)

        assert grabber.calls == [
            ("Aravis-Fake-GV01", {"gain": 4, "exposure": 5000000})
        ]
        launch = manager.calls[0][0]
        assert launch == (
            "appsrc name=appsrc caps=video/x-raw,format=GRAY8 "
            "! videoconvert ! fakesink"
        )
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED

    def test_raising_provider_never_fails_an_aravis_free_run(
        self, tmp_path, session_factory
    ):
        artifact_path = write_artifact_set(tmp_path, compiled=PLAIN_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        manager = FakePipelineManager()

        def broken_provider(registration_id):
            raise RuntimeError("watcher is gone")

        make_executor(
            session_factory, manager, provider=broken_provider
        ).execute(execution_id)

        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED


class TestAravisFreePath:
    def test_aravis_free_document_takes_the_pre_feature_call_shape(
        self, tmp_path, session_factory
    ):
        """Requirement 6.6: no Aravis binding points — no grab, and
        run_pipeline is invoked without any frame_data argument."""
        artifact_path = write_artifact_set(tmp_path, compiled=PLAIN_DOC)
        execution_id = seed_run(session_factory, artifact_path)
        grabber = FakeCameraManager()
        manager = FakePipelineManager(tag_values={"is_anomalous": False})

        make_executor(
            session_factory, manager, grabber=grabber,
            provider=lambda registration_id: None,
        ).execute(execution_id)

        assert grabber.calls == []
        launch, args, kwargs = manager.calls[0]
        assert launch == "videotestsrc num-buffers=1 ! fakesink"
        assert args == ()
        # No frame_data is passed (the pre-feature Frame_Feed shape); the
        # only added keyword is the per-node status collector's sink
        # (deployed-workflow-run-observability R3), which is inert in
        # run_pipeline when the pipeline emits no bus signals.
        assert set(kwargs) == {"latency_metrics", "status_sink"}
        assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED
