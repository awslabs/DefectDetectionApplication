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
"""Property test for video pin round trip and loop fidelity.

**Feature: static-camera-video-loop, Property 2: Pin round trip and loop fidelity**
*For any* library clip, prior state, and grab time, pinning succeeds and
the metadata reports the clip's container, codec, displayed size, fps,
frame count, duration, file name, and size; a grab at ``t`` returns exactly
the reference RGB frame at ``loop_frame_index(t)``, tagged ``RGB``, with
``len(data) == 3 * w * h``.

**Validates: Requirements 1.1, 1.2, 1.8, 3.1, 3.5**
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_video_camera import STATIC_VIDEO_CAMERA_ID, StaticVideoStore
from static_video_support import (
    BASE_TIME_S,
    ManualClock,
    expected_frame,
    expected_metadata,
    file_names,
    grab_offsets,
    store_dir,
)


def _names(library):
    return sorted(clip.name for clip in library.all_playable())


@settings(deadline=None)
@given(data=st.data(), file_name=file_names,
       pin_offset=st.floats(min_value=0.0, max_value=1e6, allow_nan=False))
def test_pin_round_trip_and_loop_fidelity(clip_library, data, file_name, pin_offset):
    names = _names(clip_library)
    clip = clip_library.get(data.draw(st.sampled_from(names), label="clip"))
    prior = data.draw(st.none() | st.sampled_from(names), label="prior")
    clock = ManualClock(BASE_TIME_S + pin_offset)
    with store_dir() as base_dir:
        store = StaticVideoStore(base_dir=base_dir, clock=clock)
        if prior is not None:
            store.pin_bytes(clip_library.get(prior).data, "prior.mp4")
            clock.now_s += 1.0

        epoch_ms = int(clock.now_s * 1000)
        metadata = store.pin_bytes(clip.data, file_name)
        assert metadata == expected_metadata(clip, file_name, epoch_ms)

        status = store.status()
        assert status == {"pinned": True, "cameraId": STATIC_VIDEO_CAMERA_ID,
                          "metadata": metadata}

        for offset in data.draw(st.lists(grab_offsets, min_size=1, max_size=6),
                                label="offsets"):
            clock.now_s = epoch_ms / 1000.0 + offset
            frame = store.get_frame()
            assert frame["data"] == expected_frame(clip, epoch_ms, clock.now_s)
            assert (frame["width"], frame["height"]) == (clip.width, clip.height)
            assert frame["pixel_format"] == "RGB"
            assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
