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
"""Property test for rotation metadata.

**Feature: static-camera-video-loop, Property 9: Rotation honored**
*For any* clip with rotation ``r`` in {0, 90, 180, 270}, the served first
frame equals ffmpeg's autorotated decode, and the displayed dimensions are
reported.

**Validates: Requirements 3.6**

ffmpeg's command-line decode (autorotate is its default) is the independent
oracle for the display orientation. Its YUV->RGB conversion can differ from
OpenCV's by rounding, so the comparison allows a tiny mean difference and
checks that every other orientation is clearly worse.
"""
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.video_loop import VideoLoopPlayer, probe_video
from video_clip_library import HEIGHT, WIDTH


def _as_array(data, width, height):
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).astype(np.int16)


def _rotations(library):
    names = sorted(clip.name for clip in library.rotated())
    if not names:
        pytest.skip("this image's ffmpeg could not write rotation metadata")
    return names


@settings(deadline=None)
@given(data=st.data())
def test_rotation_metadata_is_honored(clip_library, data):
    name = data.draw(st.sampled_from(_rotations(clip_library)), label="clip")
    clip = clip_library.get(name)

    info = probe_video(clip.path)
    if clip.rotation in (90, 270):
        assert (info.width, info.height) == (HEIGHT, WIDTH)
    else:
        assert (info.width, info.height) == (WIDTH, HEIGHT)

    player = VideoLoopPlayer(clip.path, info.fps, info.frame_count,
                             width=info.width, height=info.height)
    try:
        index = data.draw(st.integers(min_value=0, max_value=info.frame_count - 1),
                          label="index")
        frame = player.frame(index)
        assert (frame["width"], frame["height"]) == (info.width, info.height)
        assert frame["data"] == clip.frames[index]

        first = player.frame(0)
        served = _as_array(first["data"], first["width"], first["height"])
        reference = _as_array(clip.ffmpeg_first_frame, info.width, info.height)
        error = float(np.mean(np.abs(served - reference)))
        assert error <= 2.0, (name, error)

        # The oracle discriminates: the other orientations of the same frame
        # (rotated back into this shape where possible) are clearly worse.
        others = [np.rot90(served, k) for k in (1, 2, 3)]
        for other in others:
            if other.shape == reference.shape:
                assert float(np.mean(np.abs(other - reference))) > error + 5.0
    finally:
        player.close()
