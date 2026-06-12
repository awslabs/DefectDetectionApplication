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
"""Wall-clock timing/integration tests for the inference/capture path (task 12.4).

Feature: concurrent-camera-stream-viewing.

These are **integration / timing** tests (NOT Hypothesis property tests): they
wire together the *real* :class:`StreamBroadcaster`, the *real*
:class:`AcquisitionWorker` (running on its own daemon thread against the real
wall clock), and a hardware-free :class:`MockCameraBackend`, then measure actual
elapsed wall-clock behaviour to pin the timing acceptance criteria that the
property tests deliberately do not cover.

Covered acceptance criteria
----------------------------
* **Req 6.2** — *While the inference pipeline is acquiring a frame from a camera
  that has an active stream session, the broadcaster keeps delivering preview
  frames to each subscribed viewer at a rate no lower than 90% of the session's
  configured frame rate, with no single inter-frame gap exceeding 500 ms.*

  We run a live session whose worker publishes frames at the configured refresh
  ceiling (``min_refresh_fps``), then — concurrently, from another thread — hammer
  :meth:`StreamBroadcaster.get_inference_frame` (which, with an active session, is
  a pure read of the shared latest-frame slot that reuses the running claim and
  never grabs the device). Over a short measurement window we sample the frames a
  viewer observes and assert that the producer's cadence is essentially
  undisturbed: every inter-frame gap stays at or below 500 ms and the delivered
  rate stays at or above 90% of the configured rate.

* **Req 6.3 (timing)** — *When the inference pipeline requests a frame from a
  camera that has no active stream session, the camera manager opens a dedicated
  claim, acquires the frame, and returns it within 2000 ms.*

  With no session we time a single :meth:`StreamBroadcaster.get_inference_frame`
  call (the dedicated-claim fallback: open -> start_stream -> grab -> stop_stream
  -> close) and assert it returns a frame within the 2000 ms bound.

Why this is not flaky
---------------------
The mock backend's ``grab`` returns instantly, so the worker's cadence is
governed purely by its refresh-ceiling sleep (``1 / min_refresh_fps``), i.e. the
device is "fast" and the broadcaster caps at the configured rate. The wall-clock
windows are kept small (~1 s) and the assertions carry generous tolerance
(the 500 ms gap bound is 2.5x the 200 ms target interval; the rate floor is
checked with headroom) so ordinary scheduler jitter cannot trip them.

Running (bypasses the backend-wide conftest, mirroring the sibling streaming
tests' import approach)::

    PYTHONPATH=src/backend:test/backend-test/utils/streaming \
        python3 -m pytest \
        test/backend-test/utils/streaming/integration/test_inference_timing.py \
        --noconftest -q
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

import pytest

from mock_camera_backend import (
    BACKEND_KINDS,
    ClaimRegistry,
    MockCameraBackend,
    make_backend,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import FrameStatus, StreamConfig

# A single physical camera id used across the timing scenarios.
CAMERA = "Fake_timing_1"

# Configured refresh ceiling for the live session. The mock device is "fast"
# (instant grabs), so the worker publishes at exactly this rate -> a 200 ms
# target inter-frame interval at 5 fps.
CONFIGURED_FPS = 5.0
TARGET_INTERVAL_S = 1.0 / CONFIGURED_FPS  # 0.2 s

# Req 6.2 hard bound: no single inter-frame gap may exceed 500 ms.
MAX_GAP_S = 0.5
# Req 6.2 rate floor: delivered rate must stay >= 90% of the configured rate.
MIN_RATE_FRACTION = 0.9
# Req 6.3 timing bound for the dedicated-claim grab.
DEDICATED_DEADLINE_S = 2.0

# Wall-clock measurement window for the cadence sample (~1 s, kept small).
MEASURE_WINDOW_S = 1.2
# Upper bound on how long we wait for the worker's first published frame.
FIRST_FRAME_DEADLINE_S = 3.0


def _make_session_broadcaster(
    backend_kind: str, registry: ClaimRegistry
) -> Tuple[StreamBroadcaster, List[MockCameraBackend]]:
    """Build a broadcaster + factory for the live-session timing scenario.

    The factory builds a mock backend whose ``grab`` always returns a frame
    (``default_grab`` is a real :class:`RawFrame`), so the worker never sees a
    timeout/disconnect and keeps publishing at the configured refresh ceiling for
    the whole window. The real wall clock (``time.time``) and the real
    :class:`AcquisitionWorker` are used so the measured cadence is genuine.
    """
    created: List[MockCameraBackend] = []

    def backend_factory(camera_id, config) -> MockCameraBackend:
        backend = make_backend(
            backend_kind,
            camera_id,
            claim_registry=registry,
            # Every grab returns a frame so the worker publishes continuously.
            default_grab=make_raw_frame(seq=0, width=8, height=8),
        )
        created.append(backend)
        return backend

    broadcaster = StreamBroadcaster(
        stream_config=StreamConfig(
            min_refresh_fps=CONFIGURED_FPS,
            # Generous freshness ceiling so polled frames never read STALE during
            # the window; not the subject of this test.
            stale_after_s=5.0,
        ),
        backend_factory=backend_factory,
        # time_fn left at the default (time.time) -> real wall clock.
    )
    return broadcaster, created


def _wait_for_first_frame(broadcaster: StreamBroadcaster, viewer_id: str) -> None:
    """Block until the worker has published its first frame (or fail on timeout)."""
    deadline = time.monotonic() + FIRST_FRAME_DEADLINE_S
    while time.monotonic() < deadline:
        result = broadcaster.get_frame(CAMERA, viewer_id)
        if result.status == FrameStatus.OK and result.frame is not None:
            return
        time.sleep(0.005)
    pytest.fail(
        f"worker did not publish a first frame within {FIRST_FRAME_DEADLINE_S:g}s"
    )


def _sample_viewer_cadence(
    broadcaster: StreamBroadcaster, viewer_id: str, window_s: float
) -> List[float]:
    """Poll the viewer's frame for ``window_s`` and return per-frame acquired-at stamps.

    Returns the ``acquired_at`` timestamp of each *distinct* frame (by ``seq``)
    the viewer observed, in order. Inter-frame gaps are the consecutive
    differences of this list. Polling is tight (5 ms) so we never miss a published
    frame at the 200 ms cadence.
    """
    seen_seqs: set = set()
    stamps: List[float] = []
    end = time.monotonic() + window_s
    while time.monotonic() < end:
        result = broadcaster.get_frame(CAMERA, viewer_id)
        frame = result.frame
        if (
            result.status == FrameStatus.OK
            and frame is not None
            and frame.seq not in seen_seqs
        ):
            seen_seqs.add(frame.seq)
            stamps.append(frame.acquired_at)
        time.sleep(0.005)
    return stamps


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
def test_inference_during_session_preserves_viewer_cadence(backend_kind):
    """Req 6.2: concurrent inference grabs do not disturb the viewer frame cadence.

    A live session publishes at the configured 5 fps refresh ceiling. While a
    background thread continuously calls ``get_inference_frame`` (reusing the
    running claim via a pure slot read), a subscribed viewer's observed frames must
    keep flowing with no inter-frame gap exceeding 500 ms and at >= 90% of the
    configured rate.
    """
    registry = ClaimRegistry()
    broadcaster, created = _make_session_broadcaster(backend_kind, registry)

    sub = broadcaster.subscribe(CAMERA)
    assert sub.accepted and sub.viewer_id is not None
    viewer_id = sub.viewer_id

    inference_calls = 0
    inference_frames = 0
    stop_inference = threading.Event()

    def hammer_inference() -> None:
        nonlocal inference_calls, inference_frames
        while not stop_inference.is_set():
            frame = broadcaster.get_inference_frame(CAMERA)
            inference_calls += 1
            if frame is not None:
                inference_frames += 1
            # Tight but not a busy-spin; many calls land inside the window.
            time.sleep(0.002)

    inference_thread = threading.Thread(target=hammer_inference, daemon=True)
    try:
        # Single claim is open for the live session.
        assert registry.open_count(CAMERA) == 1

        _wait_for_first_frame(broadcaster, viewer_id)

        inference_thread.start()
        stamps = _sample_viewer_cadence(broadcaster, viewer_id, MEASURE_WINDOW_S)
    finally:
        stop_inference.set()
        inference_thread.join(timeout=2.0)
        broadcaster.unsubscribe(CAMERA, viewer_id)

    # The inference path actually ran concurrently and reused the claim (it
    # returned frames from the shared slot) without ever opening a second claim.
    assert inference_calls > 0, "inference thread did not run during the window"
    assert inference_frames > 0, "inference reads returned no frame from the slot"
    assert len(created) == 1, "inference must reuse the session claim, not build a backend"
    assert registry.max_concurrent.get(CAMERA) == 1
    assert not registry.overlap_detected

    # Enough distinct frames to measure a meaningful cadence over ~1.2 s @ 5 fps.
    assert len(stamps) >= 3, (
        f"too few frames observed to assess cadence: {len(stamps)}"
    )

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]

    # Req 6.2: no single inter-frame gap exceeds 500 ms.
    worst_gap = max(gaps)
    assert worst_gap <= MAX_GAP_S, (
        f"inter-frame gap {worst_gap*1000:.0f} ms exceeded the 500 ms bound "
        f"(gaps_ms={[round(g*1000) for g in gaps]})"
    )

    # Req 6.2: delivered rate stays >= 90% of the configured rate. Measured from
    # the producer's own acquired-at stamps across the observed frames.
    span = stamps[-1] - stamps[0]
    assert span > 0.0
    delivered_fps = (len(stamps) - 1) / span
    assert delivered_fps >= MIN_RATE_FRACTION * CONFIGURED_FPS, (
        f"delivered {delivered_fps:.2f} fps < 90% of configured {CONFIGURED_FPS} fps "
        f"(gaps_ms={[round(g*1000) for g in gaps]})"
    )

    # Session cleanly torn down; claim released.
    assert registry.open_count(CAMERA) == 0


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
def test_dedicated_claim_inference_returns_within_2s(backend_kind):
    """Req 6.3 (timing): a no-session inference grab returns a frame within 2000 ms.

    With no active session, ``get_inference_frame`` takes the dedicated-claim
    fallback (open -> start_stream -> grab -> stop_stream -> close). We time the
    call end-to-end and assert it returns a frame within the 2 s bound and leaves
    no lingering claim.
    """
    registry = ClaimRegistry()
    created: List[MockCameraBackend] = []

    def backend_factory(camera_id, config) -> MockCameraBackend:
        backend = make_backend(
            backend_kind,
            camera_id,
            claim_registry=registry,
            # One scripted frame for the single dedicated grab to return.
            frames=[make_raw_frame(seq=1, width=8, height=8)],
        )
        created.append(backend)
        return backend

    broadcaster = StreamBroadcaster(
        stream_config=StreamConfig(min_refresh_fps=CONFIGURED_FPS),
        backend_factory=backend_factory,
    )

    # Precondition: no session, no open claim.
    assert broadcaster.viewer_count(CAMERA) == 0
    assert registry.open_count(CAMERA) == 0

    start = time.monotonic()
    frame = broadcaster.get_inference_frame(CAMERA, config={"width": 8})
    elapsed = time.monotonic() - start

    # Returned an actual frame in the legacy dict shape, within the 2 s bound.
    assert isinstance(frame, dict)
    assert set(frame.keys()) == {"data", "height", "width"}
    assert elapsed <= DEDICATED_DEADLINE_S, (
        f"dedicated-claim inference took {elapsed*1000:.0f} ms (> 2000 ms bound)"
    )

    # Exactly one dedicated claim opened and released; nothing lingers.
    assert len(created) == 1
    dedicated = created[0]
    assert dedicated.open_count == 1
    assert dedicated.close_count == 1
    assert dedicated.grab_count == 1
    assert registry.open_count(CAMERA) == 0
    assert registry.max_concurrent.get(CAMERA) == 1
    assert not registry.overlap_detected
    assert broadcaster.viewer_count(CAMERA) == 0
