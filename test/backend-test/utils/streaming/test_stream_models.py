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
"""Property-based tests for ``StreamConfig`` timeout clamping.

Feature: concurrent-camera-stream-viewing, Property 15: Timeout/configuration
clamping — for any configured frame-timeout or open-timeout value, the effective
timeout used equals that value clamped to the range [500, 30000] ms.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import StreamConfig

# The inclusive clamp bounds enforced by StreamConfig (Req 7.1).
MIN_TIMEOUT_MS = 500
MAX_TIMEOUT_MS = 30000

# Generate arbitrary integers that straddle the clamp window: a generous span
# below the lower bound, across the in-range band, and well above the upper
# bound, so each Hypothesis run exercises below-500, in-range, and above-30000.
_timeout_ints = st.integers(min_value=-100_000, max_value=1_000_000)


def _expected_clamp(value: int) -> int:
    """Reference clamp into the inclusive [500, 30000] ms range."""
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, value))


# Feature: concurrent-camera-stream-viewing, Property 15: Timeout/configuration clamping
# Validates: Requirements 7.1
@settings(max_examples=200)
@given(frame_timeout_ms=_timeout_ints, open_timeout_ms=_timeout_ints)
def test_property_15_timeout_configuration_clamping(frame_timeout_ms, open_timeout_ms):
    """StreamConfig clamps both timeouts to [500, 30000]; in-range values unchanged.

    Feature: concurrent-camera-stream-viewing, Property 15: Timeout/configuration clamping
    Validates: Requirements 7.1
    """
    config = StreamConfig(
        frame_timeout_ms=frame_timeout_ms,
        open_timeout_ms=open_timeout_ms,
    )

    # The effective timeouts equal the configured values clamped to [500, 30000].
    assert config.frame_timeout_ms == _expected_clamp(frame_timeout_ms)
    assert config.open_timeout_ms == _expected_clamp(open_timeout_ms)

    # The clamped results always lie within the accepted inclusive range.
    assert MIN_TIMEOUT_MS <= config.frame_timeout_ms <= MAX_TIMEOUT_MS
    assert MIN_TIMEOUT_MS <= config.open_timeout_ms <= MAX_TIMEOUT_MS

    # Values below the floor clamp up to the floor; above the ceiling clamp down
    # to the ceiling; in-range values are left unchanged.
    if frame_timeout_ms < MIN_TIMEOUT_MS:
        assert config.frame_timeout_ms == MIN_TIMEOUT_MS
    elif frame_timeout_ms > MAX_TIMEOUT_MS:
        assert config.frame_timeout_ms == MAX_TIMEOUT_MS
    else:
        assert config.frame_timeout_ms == frame_timeout_ms

    if open_timeout_ms < MIN_TIMEOUT_MS:
        assert config.open_timeout_ms == MIN_TIMEOUT_MS
    elif open_timeout_ms > MAX_TIMEOUT_MS:
        assert config.open_timeout_ms == MAX_TIMEOUT_MS
    else:
        assert config.open_timeout_ms == open_timeout_ms
