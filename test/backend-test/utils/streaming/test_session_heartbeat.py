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
"""Property-based tests for viewer heartbeat update and the staleness predicate.

Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and
staleness predicate — for any viewer and any receipt time ``t``, processing a
heartbeat sets that viewer's ``last_active`` timestamp to ``t``; and for any
``last_active`` time and current time ``now``, the viewer is classified stale
exactly when ``now - last_active > stale_timeout_s``. The boundary
``now - last_active == stale_timeout_s`` is classified NOT stale.

Validates: Requirements 3.9, 8.3

Scope note (pending task 5.3)
-----------------------------
The broadcaster's heartbeat refresh and stale-viewer sweep are implemented in
task 5.3, which is not yet complete. At the level available today the contract
this test pins is:

* a heartbeat at time ``t`` means the viewer's ``last_active`` becomes ``t`` — we
  validate this directly against the :class:`Viewer` model (the single field the
  broadcaster mutates on heartbeat / frame GET, per Req 8.3); and
* the staleness classification is ``now - last_active > stale_timeout_s`` — we
  validate this against a small local predicate that mirrors the design
  (``StreamBroadcaster.sweep_stale_viewers`` / ``STALE_TIMEOUT_S``). The predicate
  is exercised against ``StreamConfig.stale_timeout_s`` so it pins the exact
  threshold the task-5.3 implementation must satisfy.

This module imports only ``Viewer`` and ``StreamConfig`` (both pure data, no
``gi`` / native dependency), so it is import-safe on hosts without the GenICam /
GStreamer stack.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import StreamConfig, Viewer

# StreamConfig's documented default stale window (Req 8.3 / design STALE_TIMEOUT_S).
DEFAULT_STALE_TIMEOUT_S = 30


def _is_stale(last_active: float, now: float, stale_timeout_s: float) -> bool:
    """Local mirror of the design's staleness predicate (pending task 5.3).

    A viewer is stale exactly when it has been inactive *longer than* the stale
    window: ``now - last_active > stale_timeout_s``. Equality (inactive for
    exactly the window) is NOT stale.
    """
    return (now - last_active) > stale_timeout_s


def _heartbeat(viewer: Viewer, t: float) -> Viewer:
    """Local mirror of the design's heartbeat refresh (pending task 5.3).

    Processing a heartbeat received at ``t`` sets the viewer's ``last_active`` to
    ``t`` (Req 8.3). Returns the same viewer for convenience.
    """
    viewer.last_active = t
    return viewer


# Wide-but-finite timestamps so the float subtraction ``now - last_active`` stays
# well-conditioned (no NaN/inf, no catastrophic-cancellation surprises) while
# still spanning viewers that are fresh, exactly at the window, and very stale.
_times = st.floats(min_value=-1.0e6, max_value=1.0e6, allow_nan=False, allow_infinity=False)

# Stale windows including 0 (any elapsed time makes a viewer stale) through long
# windows.
_timeouts = st.floats(min_value=0.0, max_value=1.0e4, allow_nan=False, allow_infinity=False)

# Integer-valued times for the exact-boundary test: ``int + int`` is representable
# exactly as a float in this range, so ``now - last_active == stale_timeout_s``
# holds without floating-point drift.
_int_times = st.integers(min_value=-100_000, max_value=100_000)
_int_timeouts = st.integers(min_value=0, max_value=10_000)


def _viewer(last_active: float = 0.0) -> Viewer:
    """Build a viewer with the given ``last_active`` timestamp."""
    return Viewer(
        viewer_id="viewer-hb",
        camera_id="cam-hb",
        subscribed_at=0.0,
        last_active=last_active,
    )


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(initial=_times, t=_times)
def test_property_9_heartbeat_sets_last_active(initial, t):
    """Processing a heartbeat at ``t`` sets the viewer's ``last_active`` to ``t``.

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    viewer = _viewer(last_active=initial)

    _heartbeat(viewer, t)

    assert viewer.last_active == t


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(initial=_times, first=_times, second=_times)
def test_property_9_heartbeat_overwrites_previous(initial, first, second):
    """Each heartbeat overwrites the prior ``last_active`` with the new receipt time.

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    viewer = _viewer(last_active=initial)

    _heartbeat(viewer, first)
    assert viewer.last_active == first

    _heartbeat(viewer, second)
    assert viewer.last_active == second


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(last_active=_times, now=_times, stale_timeout_s=_timeouts)
def test_property_9_staleness_predicate(last_active, now, stale_timeout_s):
    """A viewer is stale exactly when ``now - last_active > stale_timeout_s``.

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    stale = _is_stale(last_active, now, stale_timeout_s)

    inactive_for = now - last_active
    if inactive_for > stale_timeout_s:
        assert stale is True
    else:
        assert stale is False


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(last_active=_int_times, stale_timeout_s=_int_timeouts)
def test_property_9_exact_boundary_is_not_stale(last_active, stale_timeout_s):
    """Inactive for exactly ``stale_timeout_s`` is NOT stale (strict ``>``).

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    # now - last_active == stale_timeout_s exactly (integer-valued floats).
    now = float(last_active) + float(stale_timeout_s)

    assert _is_stale(float(last_active), now, float(stale_timeout_s)) is False


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(last_active=_int_times, stale_timeout_s=_int_timeouts)
def test_property_9_one_past_boundary_is_stale(last_active, stale_timeout_s):
    """Inactive for ``stale_timeout_s + 1`` is stale (just beyond the window).

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    now = float(last_active) + float(stale_timeout_s) + 1.0

    assert _is_stale(float(last_active), now, float(stale_timeout_s)) is True


# Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
# Validates: Requirements 3.9, 8.3
@settings(max_examples=200, deadline=None)
@given(subscribed_at=_int_times, t=_int_times)
def test_property_9_heartbeat_then_staleness_against_config(subscribed_at, t):
    """End-to-end: a heartbeat at ``t`` makes the viewer fresh until the configured window.

    Pins the contract task 5.3 must satisfy against ``StreamConfig.stale_timeout_s``:
    immediately after a heartbeat at ``t`` the viewer is fresh, remains fresh at
    exactly ``t + stale_timeout_s``, and is stale one tick later.

    Feature: concurrent-camera-stream-viewing, Property 9: Heartbeat update and staleness predicate
    Validates: Requirements 3.9, 8.3
    """
    config = StreamConfig()
    timeout = config.stale_timeout_s
    assert timeout == DEFAULT_STALE_TIMEOUT_S

    viewer = _viewer(last_active=float(subscribed_at))
    _heartbeat(viewer, float(t))

    # At receipt time: fresh.
    assert _is_stale(viewer.last_active, float(t), timeout) is False
    # At exactly the window boundary: still fresh (strict ``>``).
    assert _is_stale(viewer.last_active, float(t) + timeout, timeout) is False
    # One tick past the window: stale.
    assert _is_stale(viewer.last_active, float(t) + timeout + 1.0, timeout) is True
