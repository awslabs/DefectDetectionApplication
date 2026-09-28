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
"""Property test for enumeration and inventory independence.

**Feature: static-camera-video-loop, Property 10: Enumeration and inventory iff pinned, independent of the image camera**
*For any* interleaving of image and video pin/unpin operations and any
physical camera list:

- enumeration contains the video entry exactly while a video is pinned and
  the image entry exactly while an image is pinned;
- physical entries are unchanged and in order;
- ``build_inventory`` emits one ``StaticVideo`` entry while pinned, an
  absent entry after a reported unpin, and never the Aravis-enumerated
  duplicate;
- each camera's status and frames depend only on its own operations.

**Validates: Requirements 2.1, 2.2, 2.4, 2.5, 4.6, 4.7, 6.1, 6.2**

Real stores over temporary directories drive the real ``getCameras()`` /
``rescan_cameras()`` (the Aravis bus replaced by a fake physical list), the
real ``enumerate_aravis`` mapping, and ``build_inventory``. A small tracker
plays Camera_Discovery: a camera that leaves the bus stays tracked as an
absent leftover, which is exactly the state in which a duplicate would
reappear. The absence arguments mirror the agent: a camera is reported
after every operation, so an unpin of a pinned camera starts its absence
episode at that instant.
"""
import io
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from camera_discovery import (
    InventorySnapshot,
    TrackedCamera,
    aravis_stable_id,
    enumerate_aravis,
)
from camera_sync import (
    ORIGIN_EDGE_DISCOVERED,
    TYPE_ARAVIS_DISCOVERED,
    TYPE_STATIC_IMAGE,
    TYPE_STATIC_VIDEO,
    build_inventory,
)
from utils.static_image_camera import (
    STATIC_IMAGE_CAMERA_ID,
    STATIC_IMAGE_CAMERA_IDENTITY,
    StaticImagePinError,
    StaticImageStore,
    StaticImageUnavailableError,
)
from utils.static_video_camera import (
    STATIC_VIDEO_CAMERA_ID,
    STATIC_VIDEO_CAMERA_IDENTITY,
    StaticVideoPinError,
    StaticVideoStore,
    StaticVideoUnavailableError,
)
from static_video_support import ManualClock, expected_frame, store_dir
from video_manager_support import (
    FakeAravisBus,
    camera_fields,
    physical_camera_lists,
    physical_fields,
)

#: Tiny lossless images for the image camera, (width, height, rgb).
_IMAGES = ((4, 3, (10, 20, 30)), (8, 6, (200, 100, 50)), (5, 7, (0, 255, 0)))


def _png(width, height, color):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _derived_id(identity):
    return aravis_stable_id(identity["vendor"], identity["model"],
                            identity["serial"], identity["physical_id"])


_IMAGE_ARAVIS_ID = _derived_id(STATIC_IMAGE_CAMERA_IDENTITY)
_VIDEO_ARAVIS_ID = _derived_id(STATIC_VIDEO_CAMERA_IDENTITY)

_OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(("image", "video")),
        st.sampled_from(("pin", "pin", "unpin")),
        st.integers(min_value=0, max_value=1000),
        st.floats(min_value=0.0, max_value=120.0, allow_nan=False),
    ),
    min_size=1,
    max_size=6,
)


@pytest.fixture(scope="module")
def aravis_functions():
    import edge_ml1_p_camera_management.aravis_functions as module
    return module


class _Tracker:
    """Camera_Discovery's tracked inventory: present while on the bus, an
    absent leftover (with the time it left) afterwards."""

    def __init__(self):
        self.tracked = {}

    def observe(self, cameras, now_ms):
        current = {camera.stable_id: camera for camera in cameras}
        for stable_id, entry in list(self.tracked.items()):
            if stable_id not in current and not entry.absent:
                self.tracked[stable_id] = TrackedCamera(
                    camera=entry.camera, absent=True, absent_since=now_ms)
        for stable_id, camera in current.items():
            self.tracked[stable_id] = TrackedCamera(
                camera=camera, absent=False, absent_since=None)
        return InventorySnapshot(cameras=dict(self.tracked))


class _Model:
    """What each camera's own operations say its state is."""

    def __init__(self):
        self.image = None           # (file name, expected RGB frame)
        self.image_absent_since = None
        self.image_reported = False
        self.video = None           # (clip, epoch ms, metadata)
        self.video_absent_since = None
        self.video_reported = False


def _registrations(entries, camera_id):
    """Entries registering one virtual camera: its dedicated entry, or any
    entry whose parameters name it (the Aravis-enumerated duplicate)."""
    return [entry for entry in entries
            if entry.camera_source_id == camera_id
            or entry.params.get("cameraId") == camera_id]


def _apply(operation, model, image_store, video_store, clips, now_ms):
    kind, action, index, _dt = operation
    if kind == "image" and action == "pin":
        width, height, color = _IMAGES[index % len(_IMAGES)]
        name = "image{}.png".format(index % len(_IMAGES))
        image_store.pin_bytes(_png(width, height, color), name)
        want = Image.new("RGB", (width, height), color=color).tobytes()
        model.image = (name, want)
        model.image_absent_since = None
        model.image_reported = True
    elif kind == "image":
        if model.image is None:
            with pytest.raises(StaticImagePinError):
                image_store.unpin()
        else:
            image_store.unpin()
            model.image = None
            if model.image_reported and model.image_absent_since is None:
                model.image_absent_since = now_ms
    elif action == "pin":
        clip = clips[index % len(clips)]
        metadata = video_store.pin_bytes(clip.data, clip.name)
        model.video = (clip, metadata["pinnedAtEpochMs"], metadata)
        model.video_absent_since = None
        model.video_reported = True
    else:
        if model.video is None:
            with pytest.raises(StaticVideoPinError) as exc_info:
                video_store.unpin()
            assert "no video is pinned" in str(exc_info.value)
        else:
            video_store.unpin()
            model.video = None
            if model.video_reported and model.video_absent_since is None:
                model.video_absent_since = now_ms


def _check_enumeration(result, physical, model):
    ids = [camera.id for camera in result]
    expected = [camera["id"] for camera in physical]
    if model.image is not None:
        expected.append(STATIC_IMAGE_CAMERA_ID)
    if model.video is not None:
        expected.append(STATIC_VIDEO_CAMERA_ID)
    assert ids == expected
    assert [camera_fields(c) for c in result[:len(physical)]] == [
        physical_fields(camera) for camera in physical]


def _check_virtual_entry(entries, camera_id, type_, family, pinned,
                         absent_since, metadata):
    registrations = _registrations(entries, camera_id)
    expected = 1 if pinned or absent_since is not None else 0
    assert len(registrations) == expected, [
        (e.camera_source_id, e.type, e.absent) for e in registrations]
    if not expected:
        return
    (entry,) = registrations
    assert entry.camera_source_id == camera_id
    assert entry.type == type_
    assert entry.origin == ORIGIN_EDGE_DISCOVERED
    assert entry.params == {}
    block = entry.capabilities[family]
    assert block["id"] == camera_id
    if pinned:
        assert entry.absent is False
        for key, value in metadata.items():
            assert block[key] == value
    else:
        assert entry.absent is True
        assert entry.absent_since == absent_since
        assert "fileName" not in block


@settings(deadline=None)
@given(operations=_OPERATIONS, physical=physical_camera_lists)
def test_each_virtual_camera_follows_only_its_own_operations(
        aravis_functions, clip_library, operations, physical):
    clips = clip_library.decodable()
    clock = ManualClock()
    with store_dir() as image_dir, store_dir() as video_dir:
        image_store = StaticImageStore(base_dir=image_dir)
        video_store = StaticVideoStore(base_dir=video_dir, clock=clock)
        model = _Model()
        tracker = _Tracker()
        for operation in operations:
            clock.now_s += operation[3]
            now_ms = int(clock.now_s * 1000)
            _apply(operation, model, image_store, video_store, clips, now_ms)

            # Enumeration: standard and forced rescan alike.
            with patch.object(aravis_functions, "Aravis", FakeAravisBus(physical)), \
                    patch.object(aravis_functions, "get_store", lambda: image_store), \
                    patch.object(aravis_functions, "get_video_store", lambda: video_store):
                cameras = aravis_functions.getCameras()
                rescanned = aravis_functions.rescan_cameras()
            _check_enumeration(cameras, physical, model)
            _check_enumeration(rescanned, physical, model)

            # Inventory over what Camera_Discovery would track.
            discovered = enumerate_aravis(enumerator=lambda: list(cameras))
            assert discovered.failures == []
            snapshot = tracker.observe(discovered.cameras, now_ms)
            image_status = image_store.status()
            video_status = video_store.status()
            entries = build_inventory(
                [],
                snapshot,
                static_image_pinned=image_status["pinned"],
                static_image_metadata=image_status["metadata"],
                static_image_absent_since=(
                    None if model.image is not None else model.image_absent_since),
                static_video_pinned=video_status["pinned"],
                static_video_metadata=video_status["metadata"],
                static_video_absent_since=(
                    None if model.video is not None else model.video_absent_since),
            )
            _check_virtual_entry(
                entries, STATIC_VIDEO_CAMERA_ID, TYPE_STATIC_VIDEO, "staticVideo",
                model.video is not None, model.video_absent_since,
                model.video[2] if model.video else None)
            _check_virtual_entry(
                entries, STATIC_IMAGE_CAMERA_ID, TYPE_STATIC_IMAGE, "staticImage",
                model.image is not None, model.image_absent_since,
                {"fileName": model.image[0]} if model.image else None)
            ids = [entry.camera_source_id for entry in entries]
            assert _VIDEO_ARAVIS_ID not in ids
            assert _IMAGE_ARAVIS_ID not in ids
            for camera in physical:
                (entry,) = [e for e in entries
                            if e.params.get("cameraId") == camera["id"]]
                assert entry.type == TYPE_ARAVIS_DISCOVERED
                assert entry.absent is False

            # Status and frames: each store reflects only its own camera.
            assert image_status["pinned"] is (model.image is not None)
            if model.image is not None:
                assert image_status["metadata"]["fileName"] == model.image[0]
                assert image_store.get_frame()["data"] == model.image[1]
            else:
                with pytest.raises(StaticImageUnavailableError):
                    image_store.get_frame()
            assert video_status["pinned"] is (model.video is not None)
            if model.video is not None:
                clip, epoch_ms, metadata = model.video
                assert video_status["metadata"] == metadata
                assert video_store.get_frame()["data"] == expected_frame(
                    clip, epoch_ms, clock.now_s)
            else:
                with pytest.raises(StaticVideoUnavailableError):
                    video_store.get_frame()
