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
"""Device Image_Source ROI applied to a DEPLOYED workflow's launch string.

The operator's Region_Of_Interest (``imageCrop`` on the Image_Source)
reached only the classic capture path, where ``GstPipelineBuilder``
appends ``videocrop``. A deployed (portal-compiled) workflow runs from the
compiled document, which has no such step, so every deployed run inferred
on the FULL sensor frame however the ROI was configured — observed on a
DLAP-701 as 4608x3288 bedrock inputs against a configured 2560x1376
region, which starved the detector of resolution and produced zero
detections.

These tests drive the real ``WorkflowExecutor`` with fake camera and
pipeline managers and assert on the launch string it hands to
``run_pipeline``, so they pin the actual GStreamer chain rather than an
intermediate. Every "not applied" case must still COMPLETE the run: a
workflow that ran before this feature must keep running.
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
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

REGISTRATION_ID = "wf-1:3"

#: The ROI actually persisted on the DLAP-701 for the Basler image source.
DEVICE_ROI = {"top": 1145, "bottom": 767, "left": 1369, "right": 679}

#: The Basler's full frame, which that ROI crops to 2560x1376.
FRAME_WIDTH = 4608
FRAME_HEIGHT = 3288


def make_aravis_document(node_id="n1", graph_crop=None):
    """A compiled document with one Aravis camera source.

    ``graph_crop`` adds a videocrop element of its own, standing in for a
    Crop node the graph author placed — the case the device ROI must
    defer to instead of cropping twice.
    """
    elements = [
        {"nodeId": node_id, "factory": "appsrc",
         "args": {"name": "appsrc_{0}".format(node_id)}},
        {"nodeId": node_id, "factory": "videoconvert", "args": {}},
    ]
    if graph_crop is not None:
        elements.append(
            {"nodeId": "crop_1", "factory": "videocrop", "args": dict(graph_crop)}
        )
    elements.append({"nodeId": None, "factory": "fakesink", "args": {}})
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": elements}],
        "bindingPoints": [
            {
                "nodeId": node_id,
                "nodeType": "aravis_camera_source",
                "parameters": {"camera_id": "Basler-267601652282-23405186",
                               "gain": 1, "exposure": 500},
                "slots": [],
                "aravisBinding": True,
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def make_frame(width=FRAME_WIDTH, height=FRAME_HEIGHT, pixel_format=None):
    """A grabbed frame. ``pixel_format='bayer:bggr'`` reproduces the real
    Basler grab, which makes the executor inject bayer2rgb."""
    frame = {
        "data": b"\x00" * (width * height),
        "width": width,
        "height": height,
    }
    if pixel_format is not None:
        frame["pixel_format"] = pixel_format
    return frame


class FakePipelineManager:
    def __init__(self):
        self.calls = []

    def run_pipeline(self, pipeline_str, *args, **kwargs):
        self.calls.append((pipeline_str, args, kwargs))
        return {}


class FakeCameraManager:
    def __init__(self, frame=None):
        self.frame = frame if frame is not None else make_frame()
        self.calls = []

    def __call__(self, camera_id, config):
        self.calls.append((camera_id, dict(config)))
        return self.frame


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True):
        yield


def seed_run(session_factory, artifact_path):
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
    return "exec-1"


def get_execution(session_factory, execution_id="exec-1"):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def run_with_roi(
    tmp_path, session_factory, image_crop, frame=None, graph_crop=None,
    resolver_returns_config=True,
):
    """Execute one deployed run whose device Image_Source configuration
    carries ``image_crop``, and return the launch string."""
    artifact_path = write_artifact_set(
        tmp_path, compiled=make_aravis_document(graph_crop=graph_crop)
    )
    execution_id = seed_run(session_factory, artifact_path)

    manager = FakePipelineManager()
    grabber = FakeCameraManager(frame=frame)

    def camera_config_resolver(session, camera_id):
        if not resolver_returns_config:
            return None
        config = {"gain": 1, "exposure": 500}
        if image_crop is not _NO_CROP:
            config["imageCrop"] = image_crop
        return config

    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        frame_grabber=grabber,
        camera_config_resolver=camera_config_resolver,
    ).execute(execution_id)

    assert get_execution(session_factory).status == EXECUTION_STATUS_COMPLETED
    assert len(manager.calls) == 1
    return manager.calls[0][0]


#: Sentinel meaning "the config has no imageCrop key at all".
_NO_CROP = object()


class TestDeviceRoiApplied:
    def test_configured_roi_becomes_a_videocrop_after_the_source_chain(
        self, tmp_path, session_factory
    ):
        """The whole point: a deployed run crops to the operator's ROI."""
        launch = run_with_roi(tmp_path, session_factory, DEVICE_ROI)

        assert launch == (
            "appsrc name=appsrc caps=video/x-raw,format=GRAY8 "
            "! videoconvert "
            "! videocrop top=1145 bottom=767 left=1369 right=679 "
            "! fakesink"
        )

    def test_bayer_frame_crops_after_the_demosaic(
        self, tmp_path, session_factory
    ):
        """videocrop cannot consume video/x-bayer, and cropping a mosaic
        at an odd offset would swap the colour filter order — so the crop
        must sit after the injected bayer2rgb, not before it."""
        launch = run_with_roi(
            tmp_path,
            session_factory,
            DEVICE_ROI,
            frame=make_frame(pixel_format="bayer:bggr"),
        )

        assert launch == (
            "appsrc name=appsrc caps=video/x-bayer,format=bggr "
            "! bayer2rgb "
            "! videoconvert "
            "! videocrop top=1145 bottom=767 left=1369 right=679 "
            "! fakesink"
        )

    def test_a_single_edge_roi_is_applied(self, tmp_path, session_factory):
        launch = run_with_roi(
            tmp_path, session_factory, {"left": 100}
        )
        assert "videocrop top=0 bottom=0 left=100 right=0" in launch


class TestDeviceRoiNotApplied:
    """Every skip path leaves the launch string exactly as it was before
    this feature, and none of them fails the run."""

    def test_a_graph_that_already_crops_wins(self, tmp_path, session_factory):
        """A Crop node in the graph means the author decided; applying the
        device ROI on top would double-crop."""
        launch = run_with_roi(
            tmp_path,
            session_factory,
            DEVICE_ROI,
            graph_crop={"top": 10, "bottom": 10, "left": 10, "right": 10},
        )

        assert launch.count("videocrop") == 1
        assert "videocrop top=10 bottom=10 left=10 right=10" in launch
        assert "1145" not in launch

    def test_all_zero_roi_leaves_the_chain_untouched(
        self, tmp_path, session_factory
    ):
        launch = run_with_roi(
            tmp_path,
            session_factory,
            {"top": 0, "bottom": 0, "left": 0, "right": 0},
        )
        assert "videocrop" not in launch

    @pytest.mark.parametrize("bad", [
        {"top": -5, "bottom": 0, "left": 0, "right": 0},
        {"top": 10.5},
        {"top": "wide"},
        "not a mapping",
        [],
    ])
    def test_a_malformed_roi_is_ignored_not_fatal(
        self, tmp_path, session_factory, bad
    ):
        launch = run_with_roi(tmp_path, session_factory, bad)
        assert "videocrop" not in launch

    def test_an_roi_larger_than_the_frame_is_skipped(
        self, tmp_path, session_factory
    ):
        """videocrop would fail to negotiate and take the run down; the
        operator gets a warning and an uncropped run instead."""
        launch = run_with_roi(
            tmp_path,
            session_factory,
            {"top": 0, "bottom": 0, "left": FRAME_WIDTH, "right": 10},
        )
        assert "videocrop" not in launch

    def test_no_image_crop_key_is_the_pre_feature_path(
        self, tmp_path, session_factory
    ):
        """A camera whose Image_Source has no ROI configured."""
        launch = run_with_roi(tmp_path, session_factory, _NO_CROP)
        assert launch == (
            "appsrc name=appsrc caps=video/x-raw,format=GRAY8 "
            "! videoconvert ! fakesink"
        )

    def test_no_local_image_source_is_the_pre_feature_path(
        self, tmp_path, session_factory
    ):
        """A camera with no locally configured Image_Source at all: the
        feed's planned parameters drive the grab and nothing crops."""
        launch = run_with_roi(
            tmp_path, session_factory, DEVICE_ROI,
            resolver_returns_config=False,
        )
        assert "videocrop" not in launch
