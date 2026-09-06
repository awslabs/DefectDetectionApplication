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
"""Unit tests for the camera-manager static-id short-circuit wiring
(task 5.3).

Feature: static-image-camera-source (Requirements 3.6, 5.7).

Mock-based (the ``mock_gi`` conftest stub makes ``utils.camera_manager``
importable without the Aravis/GLib stack; imports happen inside test
bodies so the conftest import mocker is active, matching
``utils/test_camera_manager.py``):

* a static grab succeeds without touching ``camera_objects`` or calling
  ``connect_camera`` / the per-request acquisition cycle (Req 3.6);
* pin / replace / unpin operations perform zero camera-manager
  interactions and disturb no mocked physical camera object (Req 5.7);
* ``connect_camera`` / ``get_camera_status`` /
  ``get_camera_feature_bounds`` / ``apply_camera_features`` /
  ``disconnect_camera`` static-id behaviors, pinned and unpinned,
  including ``AravisCameraException`` naming the camera when unpinned.
"""
import io

import pytest
from unittest.mock import Mock, patch
from PIL import Image

from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    StaticImageStore,
)

from camera_manager_support import import_camera_manager


def make_image_bytes(img_format="PNG", width=4, height=4, color=(9, 90, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=img_format)
    return buffer.getvalue()


class RecordingDict(dict):
    """A camera_objects stand-in recording every read access."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.touched = False

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
def pinned_store(tmp_path):
    store = StaticImageStore(base_dir=str(tmp_path / "store"))
    store.pin_bytes(make_image_bytes(), "pinned.png")
    return store


@pytest.fixture
def empty_store(tmp_path):
    return StaticImageStore(base_dir=str(tmp_path / "store"))


# ---------------------------------------------------------------------------
# Req 3.6 — static grab bypasses the acquisition machinery entirely
# ---------------------------------------------------------------------------


def test_static_grab_touches_no_camera_machinery(pinned_store):
    """A static grab succeeds without touching camera_objects, without
    connect_camera, and without the per-request acquisition cycle."""
    cm = import_camera_manager()

    objects = RecordingDict()
    with patch.object(cm, "get_static_image_store", lambda: pinned_store), \
            patch.object(cm, "camera_objects", objects), \
            patch.object(cm, "connect_camera") as mock_connect, \
            patch.object(cm, "_get_camera_frame") as mock_cycle:
        frame = cm.get_camera_frame(STATIC_IMAGE_CAMERA_ID, {"gain": 3})

    assert frame["pixel_format"] == "RGB"
    assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
    mock_connect.assert_not_called()
    mock_cycle.assert_not_called()
    assert objects.touched is False
    assert not cm.get_frame_lock.locked()


def test_static_grab_without_pin_raises_naming_camera(empty_store):
    """No pin → the existing Exception contract, naming the camera."""
    cm = import_camera_manager()

    with patch.object(cm, "get_static_image_store", lambda: empty_store), \
            patch.object(cm, "connect_camera") as mock_connect:
        with pytest.raises(Exception) as excinfo:
            cm.get_camera_frame(STATIC_IMAGE_CAMERA_ID)

    message = str(excinfo.value)
    assert STATIC_IMAGE_CAMERA_ID in message
    assert "no usable pinned image" in message
    mock_connect.assert_not_called()


# ---------------------------------------------------------------------------
# Req 5.7 — pin lifecycle performs zero camera-manager interactions
# ---------------------------------------------------------------------------


def test_pin_replace_unpin_disturb_no_physical_camera(tmp_path):
    """Pin, replace, and unpin never touch the camera manager or any
    mocked physical camera object held in camera_objects."""
    cm = import_camera_manager()

    store = StaticImageStore(base_dir=str(tmp_path / "store"))
    physical_camera = Mock(name="PhysicalCamera")
    objects = {"Basler-40021234": physical_camera}

    with patch.object(cm, "camera_objects", objects), \
            patch.object(cm, "connect_camera") as mock_connect, \
            patch.object(cm, "disconnect_camera") as mock_disconnect:
        store.pin_bytes(make_image_bytes("PNG"), "first.png")           # pin
        store.pin_bytes(make_image_bytes("JPEG", 6, 2), "second.jpg")   # replace
        store.unpin()                                                   # unpin

    mock_connect.assert_not_called()
    mock_disconnect.assert_not_called()
    assert physical_camera.method_calls == []
    assert objects == {"Basler-40021234": physical_camera}


# ---------------------------------------------------------------------------
# connect / disconnect / status / feature entry points, pinned and unpinned
# ---------------------------------------------------------------------------


def test_connect_camera_pinned_true_without_camera_construction(pinned_store):
    cm = import_camera_manager()

    objects = RecordingDict()
    with patch.object(cm, "get_static_image_store", lambda: pinned_store), \
            patch.object(cm, "camera_objects", objects), \
            patch.object(cm, "manager_base") as mock_manager_base:
        assert cm.connect_camera(STATIC_IMAGE_CAMERA_ID) is True

    mock_manager_base.Camera.assert_not_called()
    assert objects == {}


def test_connect_camera_unpinned_raises_naming_camera(empty_store):
    cm = import_camera_manager()
    from exceptions.api.aravis_camera_exception import AravisCameraException

    with patch.object(cm, "get_static_image_store", lambda: empty_store), \
            patch.object(cm, "manager_base") as mock_manager_base:
        with pytest.raises(AravisCameraException) as excinfo:
            cm.connect_camera(STATIC_IMAGE_CAMERA_ID)

    assert STATIC_IMAGE_CAMERA_ID in str(excinfo.value)
    mock_manager_base.Camera.assert_not_called()


def test_disconnect_camera_is_noop_true(pinned_store):
    cm = import_camera_manager()

    objects = RecordingDict()
    with patch.object(cm, "get_static_image_store", lambda: pinned_store), \
            patch.object(cm, "camera_objects", objects):
        assert cm.disconnect_camera(STATIC_IMAGE_CAMERA_ID) is True
    assert objects.touched is False


def test_get_camera_status_pinned_connected(pinned_store):
    cm = import_camera_manager()
    from utils.common import CameraStatusEnum

    with patch.object(cm, "get_static_image_store", lambda: pinned_store):
        status = cm.get_camera_status(STATIC_IMAGE_CAMERA_ID)
    assert status.status == CameraStatusEnum.CONNECTED


def test_get_camera_status_unpinned_disconnected(empty_store):
    cm = import_camera_manager()
    from utils.common import CameraStatusEnum

    with patch.object(cm, "get_static_image_store", lambda: empty_store):
        status = cm.get_camera_status(STATIC_IMAGE_CAMERA_ID)
    assert status.status == CameraStatusEnum.DISCONNECTED


@pytest.mark.parametrize("store_fixture", ["pinned_store", "empty_store"])
def test_get_camera_feature_bounds_is_empty_never_connects(
    store_fixture, request
):
    """Explicit {} whether pinned or not — never connect on demand."""
    cm = import_camera_manager()

    store = request.getfixturevalue(store_fixture)
    with patch.object(cm, "get_static_image_store", lambda: store), \
            patch.object(cm, "connect_camera") as mock_connect:
        assert cm.get_camera_feature_bounds(STATIC_IMAGE_CAMERA_ID) == {}
    mock_connect.assert_not_called()


@pytest.mark.parametrize("store_fixture", ["pinned_store", "empty_store"])
def test_apply_camera_features_is_empty_never_connects(
    store_fixture, request
):
    """Explicit {} for a non-empty feature batch — never connect."""
    cm = import_camera_manager()

    store = request.getfixturevalue(store_fixture)
    features = [{"feature": "ReverseX", "type": "boolean", "value": True}]
    with patch.object(cm, "get_static_image_store", lambda: store), \
            patch.object(cm, "connect_camera") as mock_connect:
        assert cm.apply_camera_features(
            STATIC_IMAGE_CAMERA_ID, features
        ) == {}
    mock_connect.assert_not_called()
