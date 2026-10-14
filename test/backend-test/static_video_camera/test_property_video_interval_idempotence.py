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
"""Property test for interval idempotence and acquisition-config invariance.

**Feature: static-camera-video-loop, Property 4: Interval idempotence and acquisition-config invariance**
*For any* clip, two times in the same frame interval, and any acquisition
config, grabs through ``camera_manager.get_camera_frame("static-video-camera",
config)`` return byte-identical frames.

**Validates: Requirements 3.3, 3.7**

Goes through the wired camera_manager short-circuit (the ``mock_gi``
conftest stub makes the module importable), with a real store over a
temporary directory installed as the module's video store.
"""
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_video_camera import STATIC_VIDEO_CAMERA_ID, StaticVideoStore
from static_video_support import ManualClock, expected_frame, store_dir
from video_manager_support import import_camera_manager

_configs = st.one_of(
    st.none(),
    st.fixed_dictionaries({}, optional={
        "gain": st.floats(min_value=0, max_value=48, allow_nan=False),
        "exposure": st.integers(min_value=1, max_value=1_000_000),
        "advancedSettings": st.dictionaries(
            st.sampled_from(["BalanceRatio", "Gamma", "PixelFormat", "Width"]),
            st.one_of(st.integers(-10, 10), st.text(max_size=6)),
            max_size=3,
        ),
    }),
)


@settings(deadline=None)
@given(data=st.data(), first_config=_configs, second_config=_configs)
def test_same_interval_same_bytes_regardless_of_config(clip_library, data,
                                                       first_config, second_config):
    camera_manager = import_camera_manager()
    names = sorted(clip.name for clip in clip_library.all_playable())
    clip = clip_library.get(data.draw(st.sampled_from(names), label="clip"))
    clock = ManualClock()
    with store_dir() as base_dir:
        store = StaticVideoStore(base_dir=base_dir, clock=clock)
        epoch_ms = store.pin_bytes(clip.data, "scene.mp4")["pinnedAtEpochMs"]
        period_s = 1.0 / clip.fps
        loop_index = data.draw(st.integers(min_value=0, max_value=50), label="k")
        frame_index = data.draw(
            st.integers(min_value=0, max_value=clip.frame_count - 1), label="i")
        start = epoch_ms / 1000.0 + (loop_index * clip.frame_count + frame_index) * period_s
        first_at = start + period_s * data.draw(st.floats(0.05, 0.45), label="f1")
        second_at = start + period_s * data.draw(st.floats(0.55, 0.95), label="f2")

        with patch.object(camera_manager, "get_static_video_store", lambda: store):
            clock.now_s = first_at
            first = camera_manager.get_camera_frame(STATIC_VIDEO_CAMERA_ID, first_config)
            clock.now_s = second_at
            second = camera_manager.get_camera_frame(STATIC_VIDEO_CAMERA_ID, second_config)

        assert first == second
        assert first["data"] == clip.frames[frame_index]
        assert first["data"] == expected_frame(clip, epoch_ms, first_at)
