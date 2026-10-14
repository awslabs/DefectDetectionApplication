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
"""Camera-manager and enumeration wiring for the Static_Video_Camera.

Feature: static-camera-video-loop (Requirements 2.1–2.6, 3.7, 3.10, 4.1,
4.9, 6.2). Mock-based: the ``mock_gi`` conftest stub makes
``utils.camera_manager`` and ``aravis_functions`` importable, and a real
``StaticVideoStore`` over ``tmp_path`` is installed as their video store.

* The video grab is served before ``get_frame_lock``/``camera_objects`` and
  never connects anything; status/connect/features/disconnect behave like
  the image camera's short-circuits, pinned and unpinned.
* ``getCameras()`` lists the video camera exactly while a video is pinned,
  next to the image camera and physical cameras; a video-store failure
  never breaks the rest of the enumeration.
* ``getCamera()`` returns a handle whose vendor/model select the AWS-DDA
  packed-RGB default pipeline.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from exceptions.api.aravis_camera_exception import AravisCameraException
from exceptions.api.aravis_camera_not_found import AravisCameraNotFound
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID
from utils.static_video_camera import (
    STATIC_VIDEO_CAMERA_ID,
    STATIC_VIDEO_CAMERA_IDENTITY,
    StaticVideoStore,
)
from static_video_support import ManualClock, expected_frame
from video_manager_support import (
    FakeAravisBus,
    camera_fields,
    import_camera_manager,
)


class _RecordingDict(dict):
    """A camera_objects stand-in recording every read access."""

    touched = False

    def __contains__(self, key):
        self.touched = True
        return super().__contains__(key)

    def __getitem__(self, key):
        self.touched = True
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.touched = True
        return super().get(key, default)


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def video_store(tmp_path, clock):
    return StaticVideoStore(base_dir=str(tmp_path / "video"), clock=clock)


@pytest.fixture
def camera_manager(video_store):
    module = import_camera_manager()
    recording = _RecordingDict()
    lock = MagicMock()
    with patch.object(module, "get_static_video_store", lambda: video_store), \
            patch.object(module, "camera_objects", recording), \
            patch.object(module, "get_frame_lock", lock), \
            patch.object(module, "connect_camera", wraps=module.connect_camera) as connect:
        module._test_recording = recording
        module._test_lock = lock
        module._test_connect = connect
        yield module


def test_grab_serves_the_loop_frame_without_touching_physical_state(
        camera_manager, video_store, clock, clip_library):
    clip = clip_library.decodable()[0]
    epoch_ms = video_store.pin_bytes(clip.data, "scene.mp4")["pinnedAtEpochMs"]
    clock.now_s = epoch_ms / 1000.0 + 0.9
    frame = camera_manager.get_camera_frame(
        STATIC_VIDEO_CAMERA_ID, {"gain": 3, "exposure": 1000})
    assert frame["data"] == expected_frame(clip, epoch_ms, clock.now_s)
    assert frame["pixel_format"] == "RGB"
    assert camera_manager._test_recording.touched is False
    assert camera_manager._test_lock.acquire.call_count == 0
    assert camera_manager._test_lock.__enter__.call_count == 0
    camera_manager._test_connect.assert_not_called()


def test_unpinned_grab_names_the_camera(camera_manager):
    with pytest.raises(Exception) as exc_info:
        camera_manager.get_camera_frame(STATIC_VIDEO_CAMERA_ID)
    message = str(exc_info.value)
    assert STATIC_VIDEO_CAMERA_ID in message
    assert "no usable pinned video" in message
    assert camera_manager._test_recording.touched is False


def test_status_connect_features_disconnect(camera_manager, video_store, clip_library):
    from utils.common import CameraStatusEnum

    assert camera_manager.get_camera_status(STATIC_VIDEO_CAMERA_ID).status == \
        CameraStatusEnum.DISCONNECTED
    with pytest.raises(AravisCameraException) as exc_info:
        camera_manager.connect_camera(STATIC_VIDEO_CAMERA_ID)
    assert STATIC_VIDEO_CAMERA_ID in str(exc_info.value)

    video_store.pin_bytes(clip_library.decodable()[0].data, "scene.mp4")
    assert camera_manager.get_camera_status(STATIC_VIDEO_CAMERA_ID).status == \
        CameraStatusEnum.CONNECTED
    assert camera_manager.connect_camera(STATIC_VIDEO_CAMERA_ID) is True
    assert camera_manager.get_camera_feature_bounds(STATIC_VIDEO_CAMERA_ID) == {}
    assert camera_manager.apply_camera_features(
        STATIC_VIDEO_CAMERA_ID, [{"feature": "Gain", "type": "float", "value": 2}]) == {}
    assert camera_manager.disconnect_camera(STATIC_VIDEO_CAMERA_ID) is True
    assert camera_manager._test_recording.touched is False
    assert camera_manager._test_lock.__enter__.call_count == 0


# --- enumeration ---------------------------------------------------------------


@pytest.fixture
def aravis_functions():
    import edge_ml1_p_camera_management.aravis_functions as module
    return module


class _FakeImageStore:
    def __init__(self, pinned):
        self.pinned = pinned

    def is_pinned(self):
        return self.pinned


_PHYSICAL = [{
    "id": "Basler-40022199", "model": "a2A1920", "address": "10.0.0.7",
    "physical_id": "40022199", "protocol": "USB3Vision", "serial": "40022199",
    "vendor": "Basler",
}]


@pytest.mark.parametrize("image_pinned", [False, True])
def test_get_cameras_lists_the_video_camera_iff_pinned(
        aravis_functions, video_store, clip_library, image_pinned):
    bus = FakeAravisBus(_PHYSICAL)
    with patch.object(aravis_functions, "Aravis", bus), \
            patch.object(aravis_functions, "get_store", lambda: _FakeImageStore(image_pinned)), \
            patch.object(aravis_functions, "get_video_store", lambda: video_store):
        ids = [camera.id for camera in aravis_functions.getCameras()]
        assert STATIC_VIDEO_CAMERA_ID not in ids
        video_store.pin_bytes(clip_library.decodable()[0].data, "scene.mp4")
        cameras = aravis_functions.getCameras()
        rescanned = aravis_functions.rescan_cameras()
        for result in (cameras, rescanned):
            ids = [camera.id for camera in result]
            assert ids.count(STATIC_VIDEO_CAMERA_ID) == 1
            assert (STATIC_IMAGE_CAMERA_ID in ids) is image_pinned
            assert ids[0] == "Basler-40022199"
            video = next(c for c in result if c.id == STATIC_VIDEO_CAMERA_ID)
            assert camera_fields(video) == tuple(
                STATIC_VIDEO_CAMERA_IDENTITY[f] for f in
                ("id", "model", "address", "physical_id", "protocol", "serial", "vendor"))
            assert all(value for value in camera_fields(video))
        video_store.unpin()
        assert STATIC_VIDEO_CAMERA_ID not in [c.id for c in aravis_functions.getCameras()]


def test_video_store_failure_never_breaks_enumeration(aravis_functions):
    def _raising():
        raise RuntimeError("video store unavailable")

    with patch.object(aravis_functions, "Aravis", FakeAravisBus(_PHYSICAL)), \
            patch.object(aravis_functions, "get_store", lambda: _FakeImageStore(True)), \
            patch.object(aravis_functions, "get_video_store", _raising):
        ids = [camera.id for camera in aravis_functions.getCameras()]
    assert ids == ["Basler-40022199", STATIC_IMAGE_CAMERA_ID]


def test_get_camera_handle_selects_the_rgb_default_pipeline(
        aravis_functions, video_store, clip_library):
    with patch.object(aravis_functions, "get_video_store", lambda: video_store):
        with pytest.raises(AravisCameraNotFound) as exc_info:
            aravis_functions.getCamera(STATIC_VIDEO_CAMERA_ID)
        assert STATIC_VIDEO_CAMERA_ID in str(exc_info.value)
        video_store.pin_bytes(clip_library.decodable()[0].data, "scene.mp4")
        handle = aravis_functions.getCamera(STATIC_VIDEO_CAMERA_ID)
    assert handle.get_vendor_name() == "AWS-DDA"
    assert handle.get_model_name() == "Static Video Camera"

    # The Image_Source default lookup (vendor, then model, else the vendor's
    # "default") resolves this model to the AWS-DDA packed-RGB chain.
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src",
                               "backend", "utils", "config",
                               "default_camera_configurations.json")
    with open(config_path, "r", encoding="utf-8") as handle_file:
        configs = json.load(handle_file)
    vendor = configs["AWS-DDA"]
    model = "Static Video Camera" if "Static Video Camera" in vendor else "default"
    assert "format=RGB" in vendor[model]["processingPipeline"]
