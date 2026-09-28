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
"""Property test for the Loop_Position arithmetic.

**Feature: static-camera-video-loop, Property 1: Loop index arithmetic**
*For any* ``fps`` in (0, 240], ``frameCount >= 1``, epoch and time:
``loop_frame_index`` lies in ``[0, frameCount)``, is periodic with period
``frameCount * 1000 / fps`` ms, is non-decreasing within one period, and
equals the closed form ``floor(((t - epoch) mod loop) / period)``.

**Validates: Requirements 3.1, 3.2**

Times are generated as "frame i of loop k, at fraction f of its display
interval", so the expected index is exact and float rounding at interval
boundaries cannot make the oracle ambiguous.
"""
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.video_loop import MAX_VIDEO_FPS, loop_frame_index

_fps = st.one_of(
    st.floats(min_value=0.5, max_value=MAX_VIDEO_FPS, allow_nan=False,
              allow_infinity=False),
    st.sampled_from([24000 / 1001, 30000 / 1001, 60000 / 1001, 25.0, 30.0,
                     60.0, 120.0, 240.0, 1.0, 7.5]),
)


@settings(deadline=None)
@given(
    fps=_fps,
    frame_count=st.integers(min_value=1, max_value=20_000),
    epoch_ms=st.integers(min_value=0, max_value=2 ** 42),
    loop_k=st.integers(min_value=-3, max_value=50),
    data=st.data(),
)
def test_loop_index_arithmetic(fps, frame_count, epoch_ms, loop_k, data):
    index = data.draw(st.integers(min_value=0, max_value=frame_count - 1))
    fraction = data.draw(st.floats(min_value=0.1, max_value=0.9))
    period_ms = 1000.0 / fps
    now_ms = epoch_ms + (loop_k * frame_count + index + fraction) * period_ms

    served = loop_frame_index(now_ms, epoch_ms, fps, frame_count)

    # Range, closed form, and periodicity (any whole number of loops, before
    # or after the epoch, lands on the same frame).
    assert 0 <= served < frame_count
    assert served == index

    # Non-decreasing within one period: a later time inside the same loop
    # never maps to an earlier frame.
    if index + 1 < frame_count:
        later = loop_frame_index(now_ms + period_ms, epoch_ms, fps, frame_count)
        assert later == index + 1
    else:
        # The display interval after the last frame is frame 0 of the next
        # loop (Requirement 3.2: return to the first frame immediately).
        wrapped = loop_frame_index(now_ms + period_ms, epoch_ms, fps, frame_count)
        assert wrapped == 0


@settings(deadline=None)
@given(
    fps=_fps,
    epoch_ms=st.integers(min_value=0, max_value=2 ** 42),
    now_ms=st.floats(min_value=-1e12, max_value=1e13, allow_nan=False),
)
def test_single_frame_video_always_serves_frame_zero(fps, epoch_ms, now_ms):
    assert loop_frame_index(now_ms, epoch_ms, fps, 1) == 0


@settings(deadline=None)
@given(
    fps=_fps,
    frame_count=st.integers(min_value=2, max_value=5_000),
    epoch_ms=st.integers(min_value=0, max_value=2 ** 42),
    now_ms=st.floats(min_value=0, max_value=2 ** 43, allow_nan=False),
)
def test_arbitrary_times_stay_in_range(fps, frame_count, epoch_ms, now_ms):
    served = loop_frame_index(now_ms, epoch_ms, fps, frame_count)
    assert 0 <= served < frame_count
    period_ms = 1000.0 / fps
    elapsed = (now_ms - epoch_ms) % (frame_count * period_ms)
    # Closed form, allowing a one-frame tie only where the float division
    # lands within a rounding error of an interval boundary.
    closed = min(int(math.floor(elapsed / period_ms)), frame_count - 1)
    assert served == closed or abs(elapsed / period_ms - round(elapsed / period_ms)) < 1e-6


@pytest.mark.parametrize("bad_fps", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_fps_rejected(bad_fps):
    with pytest.raises(ValueError):
        loop_frame_index(1000.0, 0, bad_fps, 10)


def test_invalid_frame_count_rejected():
    with pytest.raises(ValueError):
        loop_frame_index(1000.0, 0, 30.0, 0)
