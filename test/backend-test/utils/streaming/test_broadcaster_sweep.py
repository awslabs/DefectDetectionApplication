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
"""Property-based test for the stale-viewer sweep.

Feature: concurrent-camera-stream-viewing, Property 10: Stale sweep deregisters,
heartbeats keep alive — for any set of viewers with arbitrary last-active
timestamps, a stale-sweep at time ``now`` removes exactly those viewers with
``now - last_active > stale_timeout_s`` (decrementing the active count and
treating them as unsubscribed), while any viewer that has sent a heartbeat
within ``stale_timeout_s`` of ``now`` is retained (Req 3.8, 8.2, 8.6).

Validates: Requirements 3.8, 8.2, 8.6

Design notes / test seams
-------------------------
* The :class:`StreamBroadcaster` is driven with an injected ``backend_factory``
  that builds the hardware-free :class:`MockCameraBackend` and an injected
  :class:`MockClock`, so subscribe/heartbeat/sweep timing is fully
  deterministic. All mocks share one :class:`ClaimRegistry` so the single-claim
  invariant can be checked across the whole run.

* A real subscribe would spawn an acquisition-worker daemon thread. To keep the
  property deterministic and thread-free we patch the ``AcquisitionWorker``
  symbol in the broadcaster module with a no-op :class:`_StubWorker` for the
  duration of each example (the worker-stub technique from
  ``test_broadcaster_lifecycle.py``). This is a TEST-ONLY seam — production code
  is not modified; the broadcaster's real subscribe/heartbeat/sweep/teardown
  orchestration (the code under test) runs exactly as in production.

* Driving the sweep through the *real* clock: viewers are subscribed at a base
  time; a generated subset then heartbeats after the clock is advanced. The
  sweep is invoked at the broadcaster's own clock value, and the expected
  outcome is computed from each viewer's effective ``last_active`` (subscribe
  time, or the later heartbeat time for the heartbeated subset).
"""
from __future__ import annotations

import unittest.mock as mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    MockClock,
    make_raw_frame,
)
from utils.streaming import broadcaster as broadcaster_module
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import StreamConfig

# Stale window the sweep compares against. A viewer is stale exactly when it has
# been inactive strictly longer than this many seconds (Req 8.6).
STALE_TIMEOUT_S = 30
# Large viewer cap so this test focuses purely on the sweep (no viewer-limit
# rejection, which is Property 6 / task 5.7) and every subscribe is accepted.
MAX_VIEWERS = 1000
# Single camera keeps the model small; the sweep logic is per-session and the
# broadcaster sweeps every registered session uniformly.
CAMERA_ID = "Fake_1"


class _StubWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Implements just the surface the broadcaster touches — construction plus
    ``start`` / ``stop`` / ``join`` — without spawning any thread or touching the
    backend, so the broadcaster's subscribe/sweep/teardown orchestration runs
    deterministically and the sweep assertions are race-free.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self):
        return None

    def stop(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        return None


# A small population of viewers. Each is described by the offset (in seconds)
# applied to the clock *before* it subscribes, so viewers end up with a spread of
# subscribe times relative to one another.
_subscribe_offsets = st.lists(
    st.integers(min_value=0, max_value=120),
    min_size=1,
    max_size=12,
)


# Feature: concurrent-camera-stream-viewing, Property 10: Stale sweep deregisters, heartbeats keep alive
# Validates: Requirements 3.8, 8.2, 8.6
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=150, deadline=None)
@given(
    subscribe_offsets=_subscribe_offsets,
    data=st.data(),
)
def test_property_10_stale_sweep_deregisters_heartbeats_keep_alive(
    backend_cls, subscribe_offsets, data
):
    """Sweep removes exactly viewers stale past the window; heartbeated ones stay.

    Subscribe a generated set of viewers at a spread of times, heartbeat a
    generated subset at later (generated) times, then sweep at the broadcaster's
    current clock value and assert exactly the viewers whose effective
    ``last_active`` is older than ``stale_timeout_s`` were removed.

    Feature: concurrent-camera-stream-viewing, Property 10: Stale sweep deregisters, heartbeats keep alive
    Validates: Requirements 3.8, 8.2, 8.6
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1000.0)
    config = StreamConfig(max_viewers=MAX_VIEWERS, stale_timeout_s=STALE_TIMEOUT_S)

    def backend_factory(camera_id, cfg):
        return backend_cls(
            camera_id,
            frames=[make_raw_frame(seq=0)],
            claim_registry=registry,
            clock=clock,
            default_grab=make_raw_frame(seq=0),
        )

    broadcaster = StreamBroadcaster(
        stream_config=config,
        backend_factory=backend_factory,
        time_fn=clock.time,
    )

    # Shadow model: viewer_id -> effective last_active time the test expects.
    expected_last_active: dict[str, float] = {}

    with mock.patch.object(broadcaster_module, "AcquisitionWorker", _StubWorker):
        # --- Phase 1: subscribe every viewer at a spread of (monotonic) times.
        viewer_ids: list[str] = []
        prev_offset = 0
        for offset in subscribe_offsets:
            # MockClock cannot move backwards; advance by the non-negative delta
            # so subscribe times are monotonic but still spread out.
            delta = max(0, offset - prev_offset)
            clock.advance(delta)
            prev_offset = offset

            result = broadcaster.subscribe(CAMERA_ID)
            assert result.accepted, f"unexpected rejection: {result.reason}"
            assert result.viewer_id is not None
            viewer_ids.append(result.viewer_id)
            expected_last_active[result.viewer_id] = clock.time()

        assert broadcaster.viewer_count(CAMERA_ID) == len(viewer_ids)

        # --- Phase 2: heartbeat a generated subset at later, generated times.
        heartbeat_subset = data.draw(
            st.lists(
                st.sampled_from(viewer_ids),
                unique=True,
                max_size=len(viewer_ids),
            ),
            label="heartbeat_subset",
        )
        for viewer_id in heartbeat_subset:
            # Advance the clock by a generated non-negative amount before each
            # heartbeat so heartbeated viewers get a fresher last_active.
            bump = data.draw(st.integers(min_value=0, max_value=100), label="hb_bump")
            clock.advance(bump)
            refreshed = broadcaster.heartbeat(CAMERA_ID, viewer_id)
            assert refreshed is True
            expected_last_active[viewer_id] = clock.time()

        # --- Phase 3: advance to an arbitrary sweep time and sweep.
        sweep_bump = data.draw(st.integers(min_value=0, max_value=200), label="sweep_bump")
        clock.advance(sweep_bump)
        now = clock.time()

        # Predict the exact outcome from the model: a viewer is removed iff it has
        # been inactive strictly longer than the stale window (Req 8.6).
        expected_removed = {
            vid
            for vid, last_active in expected_last_active.items()
            if (now - last_active) > STALE_TIMEOUT_S
        }
        expected_retained = set(expected_last_active) - expected_removed

        broadcaster.sweep_stale_viewers(now)

        # --- Assertions: exactly the stale viewers were removed; the rest stay.
        session = broadcaster._sessions.get(CAMERA_ID)
        if expected_retained:
            assert session is not None, "session torn down while viewers remain"
            surviving = set(session.viewers.keys())
            assert surviving == expected_retained, (
                f"survivors {surviving} != expected retained {expected_retained}"
            )
            # No retained viewer is stale; no removed viewer survived.
            for vid in surviving:
                assert (now - expected_last_active[vid]) <= STALE_TIMEOUT_S
            assert surviving.isdisjoint(expected_removed)

            # Active count tracks the retained set exactly (Req 8.2).
            assert broadcaster.viewer_count(CAMERA_ID) == len(expected_retained)
            # Session still holds its single device claim.
            assert registry.open_count(CAMERA_ID) == 1
        else:
            # Every viewer was stale: the session is emptied and torn down, which
            # releases the single device claim (stop-on-last, Req 8.7).
            assert session is None, "session should be torn down when fully swept"
            assert broadcaster.viewer_count(CAMERA_ID) == 0
            assert registry.open_count(CAMERA_ID) == 0

    # Single-claim invariant never overlapped throughout the run.
    registry.assert_single_claim()
