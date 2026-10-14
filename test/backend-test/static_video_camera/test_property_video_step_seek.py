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
"""Property test for step/seek equivalence of the loop player.

**Feature: static-camera-video-loop, Property 5: Step/seek equivalence**
*For any* clip and sequence of requested indices (steps inside and beyond
the window, backward jumps, wraps, repeats), every
``VideoLoopPlayer.frame(i)`` equals reference frame ``i``.

**Validates: Requirements 3.1, 3.3, 3.8**

The reference is one sequential decode per clip; the player reaches the
same frames by a mix of ``grab()`` stepping and ``CAP_PROP_POS_FRAMES``
seeks. The step window is drawn too, so both paths are exercised on every
clip regardless of its frame rate.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.video_loop import VideoLoopPlayer


def _clip_names(library):
    return sorted(clip.name for clip in library.decodable())


def _index_sequences(frame_count):
    last = frame_count - 1
    single = st.integers(min_value=0, max_value=last)
    patterns = st.sampled_from([
        [0, 1, 2, 3],                   # consecutive steps
        [0, last, 0],                   # wrap and back
        [last, last, 0, 0],             # repeats around the wrap
        [min(5, last), 1, min(9, last)],  # backward then forward
        list(range(0, frame_count, 3)),  # strided forward steps
        list(range(last, -1, -4)),       # strided backward jumps
    ])
    return st.one_of(st.lists(single, min_size=1, max_size=25), patterns)


@settings(deadline=None)
@given(data=st.data())
def test_step_and_seek_return_the_reference_frames(clip_library, data):
    name = data.draw(st.sampled_from(_clip_names(clip_library)), label="clip")
    clip = clip_library.get(name)
    indices = data.draw(_index_sequences(clip.frame_count), label="indices")
    step_window = data.draw(
        st.one_of(st.none(), st.integers(min_value=1, max_value=max(1, 2 * int(clip.fps)))),
        label="step_window",
    )
    player = VideoLoopPlayer(clip.path, clip.fps, clip.frame_count,
                             width=clip.width, height=clip.height,
                             step_window=step_window)
    try:
        previous = None
        for index in indices:
            frame = player.frame(index)
            assert frame["data"] == clip.frames[index], (name, index, indices)
            assert frame["width"] == clip.width
            assert frame["height"] == clip.height
            assert frame["pixel_format"] == "RGB"
            assert len(frame["data"]) == 3 * frame["width"] * frame["height"]
            if previous is not None and previous[0] == index:
                # Same frame requested again: the cached dict itself.
                assert frame is previous[1]
            previous = (index, frame)
    finally:
        player.close()


def test_player_opens_lazily_and_closes(clip_library):
    """Nothing decodes until a frame is requested (Requirement 3.9), and a
    closed player reopens on demand."""
    clip = clip_library.decodable()[0]
    player = VideoLoopPlayer(clip.path, clip.fps, clip.frame_count)
    assert player._capture is None
    player.frame(0)
    assert player._capture is not None
    player.close()
    assert player._capture is None
    assert player.frame(clip.frame_count - 1)["data"] == clip.frames[-1]
    player.close()
