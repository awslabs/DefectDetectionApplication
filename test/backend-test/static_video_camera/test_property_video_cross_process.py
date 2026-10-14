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
"""Property test for cross-process consistency.

**Feature: static-camera-video-loop, Property 3: Cross-process consistency**
*For any* clip and time, two store instances over one directory (modeling
two LocalServer processes) and a fresh instance created after them
(modeling a restart) return byte-identical frames.

**Validates: Requirements 3.4, 7.1, 7.2**

The instances share nothing but the directory and the wall clock, exactly
like the API process, the digital-input process, and a restarted
LocalServer.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_video_camera import StaticVideoStore
from static_video_support import (
    ManualClock,
    expected_frame,
    grab_offsets,
    store_dir,
)


@settings(deadline=None)
@given(data=st.data())
def test_processes_and_restarts_agree_on_the_frame(clip_library, data):
    names = sorted(clip.name for clip in clip_library.all_playable())
    clip = clip_library.get(data.draw(st.sampled_from(names), label="clip"))
    clock = ManualClock()
    with store_dir() as base_dir:
        api_process = StaticVideoStore(base_dir=base_dir, clock=clock)
        other_process = StaticVideoStore(base_dir=base_dir, clock=clock)
        metadata = api_process.pin_bytes(clip.data, "scene.mp4")
        epoch_ms = metadata["pinnedAtEpochMs"]

        offsets = data.draw(st.lists(grab_offsets, min_size=1, max_size=5),
                            label="offsets")
        before_restart = []
        for offset in offsets:
            clock.now_s = epoch_ms / 1000.0 + offset
            first = api_process.get_frame()
            second = other_process.get_frame()
            assert first == second
            assert first["data"] == expected_frame(clip, epoch_ms, clock.now_s)
            before_restart.append(first)

        # Restart: a brand-new instance restores the pin and its epoch and
        # resumes at the wall-clock Loop_Position (Requirements 7.1, 7.2).
        restarted = StaticVideoStore(base_dir=base_dir, clock=clock)
        assert restarted.status()["metadata"] == metadata
        for offset, frame in zip(offsets, before_restart):
            clock.now_s = epoch_ms / 1000.0 + offset
            assert restarted.get_frame() == frame
