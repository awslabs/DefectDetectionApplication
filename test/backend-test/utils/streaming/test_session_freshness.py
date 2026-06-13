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
"""Property-based tests for ``StreamSession`` frame freshness classification.

Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
— for any latest frame published at ``acquired_at`` and any request time ``now``,
``StreamSession.read_latest(now)`` returns status ``OK`` (with the frame) when
``now - acquired_at <= stale_after_s`` and status ``STALE`` (still carrying the
last frame) otherwise. The boundary ``now - acquired_at == stale_after_s`` is
classified ``OK``.

Validates: Requirements 4.6, 4.7
"""
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import FrameStatus, StreamConfig
from utils.streaming.session import StreamSession

# StreamConfig's documented default freshness ceiling (Req 4.6).
DEFAULT_STALE_AFTER_S = 2.0

# Acquired-at / now request times. Bounded to a wide but finite span so the
# float subtraction ``now - acquired_at`` stays well-conditioned (no NaN/inf and
# no catastrophic-cancellation surprises), while still spanning frames that are
# fresh, exactly at the ceiling, and arbitrarily stale.
_times = st.floats(min_value=-1.0e6, max_value=1.0e6, allow_nan=False, allow_infinity=False)

# Arbitrary freshness ceilings, including 0.0 (everything but a same-instant read
# is stale) up through long windows.
_stale_after = st.floats(min_value=0.0, max_value=1.0e4, allow_nan=False, allow_infinity=False)

# Integer-valued times for exact-boundary tests: ``int + int`` is representable
# exactly as a float in this range, so ``now - acquired_at == stale_after_s``
# holds without floating-point drift.
_int_times = st.integers(min_value=-100_000, max_value=100_000)
_int_stale = st.integers(min_value=0, max_value=10_000)


def _session(stale_after_s: float) -> StreamSession:
    """Build a session whose freshness ceiling is ``stale_after_s``."""
    return StreamSession(
        camera_id="cam-freshness",
        stream_config=StreamConfig(stale_after_s=stale_after_s),
    )


# Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
# Validates: Requirements 4.6, 4.7
@settings(max_examples=200, deadline=None)
@given(acquired_at=_times, now=_times, stale_after_s=_stale_after)
def test_property_11_frame_freshness_predicate(acquired_at, now, stale_after_s):
    """read_latest is OK when age <= stale_after_s and STALE when age exceeds it.

    Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
    Validates: Requirements 4.6, 4.7
    """
    session = _session(stale_after_s)
    published = session.publish(b"frame-payload", width=4, height=2, now=acquired_at)

    result = session.read_latest(now)

    age = now - acquired_at
    if age <= stale_after_s:
        # Fresh (or exactly at the ceiling): OK and carries the published frame.
        assert result.status == FrameStatus.OK
        assert result.frame is published
    else:
        # Older than the ceiling: STALE, still surfacing the last good frame.
        assert result.status == FrameStatus.STALE
        assert result.frame is published


# Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
# Validates: Requirements 4.6, 4.7
@settings(max_examples=200, deadline=None)
@given(acquired_at=_int_times, stale_after_s=_int_stale)
def test_property_11_exact_boundary_is_ok(acquired_at, stale_after_s):
    """A frame whose age equals ``stale_after_s`` exactly classifies as OK.

    Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
    Validates: Requirements 4.6, 4.7
    """
    session = _session(float(stale_after_s))
    session.publish(b"frame-payload", width=4, height=2, now=float(acquired_at))

    # now - acquired_at == stale_after_s exactly (integer-valued floats).
    now = float(acquired_at) + float(stale_after_s)
    result = session.read_latest(now)

    assert result.status == FrameStatus.OK


# Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
# Validates: Requirements 4.6, 4.7
@settings(max_examples=200, deadline=None)
@given(acquired_at=_times, stale_after_s=_stale_after)
def test_property_11_just_past_boundary_is_stale(acquired_at, stale_after_s):
    """A frame even marginally older than ``stale_after_s`` classifies as STALE.

    Feature: concurrent-camera-stream-viewing, Property 11: Frame freshness predicate
    Validates: Requirements 4.6, 4.7
    """
    session = _session(stale_after_s)
    session.publish(b"frame-payload", width=4, height=2, now=acquired_at)

    # The smallest representable step beyond the ceiling: age > stale_after_s.
    boundary = acquired_at + stale_after_s
    now = math.nextafter(boundary, math.inf)
    # Guard against the rare case where nextafter does not increase the age past
    # the ceiling at this magnitude; only assert when the age is strictly greater.
    if now - acquired_at > stale_after_s:
        result = session.read_latest(now)
        assert result.status == FrameStatus.STALE
