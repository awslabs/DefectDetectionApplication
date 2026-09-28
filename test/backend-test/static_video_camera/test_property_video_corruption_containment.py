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
"""Property test for video corruption containment.

**Feature: static-camera-video-loop, Property 8: Corruption containment**
*For any* corruption mode (media deleted, truncated to its header, or
overwritten; sidecar missing or not JSON), a fresh store logs the cause
category, reports not pinned, fails grabs naming the video camera, leaves
the rest of the enumeration unchanged, and accepts a restoring pin.

**Validates: Requirements 3.10, 7.3**

The log assertion attaches a handler to the module logger (function-scoped
fixtures such as ``caplog`` do not mix with ``@given``).
"""
import logging
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import utils.static_video_camera as svc
from utils.static_video_camera import StaticVideoStore, StaticVideoUnavailableError
from static_video_support import ManualClock, expected_frame, store_dir

_EXPECTED_CATEGORY = {
    "video_missing": "missing data",
    "sidecar_missing": "missing data",
    "truncated_to_header": "undecodable data",
    "overwritten_same_size": "undecodable data",
    "sidecar_not_json": "undecodable data",
    "sidecar_missing_field": "undecodable data",
}

_physical = st.lists(
    st.sampled_from(["Aravis-Fake-GV01", "Basler-40022199", "static-image-camera"]),
    min_size=0, max_size=3, unique=True,
)


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _corrupt(mode, base_dir, junk):
    video = os.path.join(base_dir, "pinned_video")
    sidecar = os.path.join(base_dir, "pinned_video.json")
    if mode == "video_missing":
        os.remove(video)
    elif mode == "sidecar_missing":
        os.remove(sidecar)
    elif mode == "truncated_to_header":
        with open(video, "rb") as handle:
            head = handle.read(64)
        with open(video, "wb") as handle:
            handle.write(head)
    elif mode == "overwritten_same_size":
        size = os.path.getsize(video)
        with open(video, "wb") as handle:
            handle.write((junk * (size // len(junk) + 1))[:size])
    elif mode == "sidecar_not_json":
        with open(sidecar, "w", encoding="utf-8") as handle:
            handle.write("{not json")
    else:  # sidecar_missing_field
        with open(sidecar, "w", encoding="utf-8") as handle:
            handle.write('{"fileName": "x.mp4", "frameCount": 3}')


@settings(deadline=None)
@given(data=st.data(), mode=st.sampled_from(sorted(_EXPECTED_CATEGORY)),
       physical=_physical, junk=st.binary(min_size=1, max_size=32))
def test_corruption_is_contained(clip_library, data, mode, physical, junk):
    names = sorted(clip.name for clip in clip_library.decodable())
    clip = clip_library.get(data.draw(st.sampled_from(names), label="clip"))
    recovery = clip_library.get(data.draw(st.sampled_from(names), label="recovery"))
    handler = _RecordingHandler()
    module_logger = logging.getLogger(svc.__name__)
    original_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.ERROR)
    clock = ManualClock()
    try:
        with store_dir() as base_dir:
            StaticVideoStore(base_dir=base_dir, clock=clock).pin_bytes(clip.data, "a.mp4")
            _corrupt(mode, base_dir, junk)

            store = StaticVideoStore(base_dir=base_dir, clock=clock)
            status = store.status()
            assert status["pinned"] is False
            assert status["metadata"] is None
            messages = [record.getMessage() for record in handler.records]
            assert any(_EXPECTED_CATEGORY[mode] in message for message in messages), messages
            assert any(svc.STATIC_VIDEO_CAMERA_ID in message for message in messages)

            enumeration = list(physical)
            if store.is_pinned():
                enumeration.append(svc.STATIC_VIDEO_CAMERA_ID)
            assert enumeration == list(physical)

            with pytest.raises(StaticVideoUnavailableError) as exc_info:
                store.get_frame()
            assert svc.STATIC_VIDEO_CAMERA_ID in str(exc_info.value)
            assert "no usable pinned video" in str(exc_info.value)

            clock.now_s += 5.0
            metadata = store.pin_bytes(recovery.data, "b.mp4")
            assert store.status()["pinned"] is True
            clock.now_s += 1.5
            assert store.get_frame()["data"] == expected_frame(
                recovery, metadata["pinnedAtEpochMs"], clock.now_s)
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(original_level)
