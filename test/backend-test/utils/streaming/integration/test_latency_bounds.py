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
"""First-frame availability and latest-frame read latency-bound integration tests.

Feature: concurrent-camera-stream-viewing, task 12.1.

These are **timing / integration** tests (not property tests). They wire the real
``StreamBroadcaster`` + ``StreamSession`` + ``AcquisitionWorker`` together against
the hardware-free mock ``CameraBackend`` and assert the wall-clock latency bounds
from the acceptance criteria:

* **Req 3.10 — first frame within 10 s.** A new ``StreamSession`` makes its first
  ``Latest_Frame`` available to viewers within 10 seconds of starting. We subscribe
  through the broadcaster (which opens the backend claim and starts a *real* worker
  thread) against a mock that delivers frames immediately, and assert a frame becomes
  readable well within the 10 s bound.

* **Req 2.3 — latest-frame read < 100 ms.** A viewer's request for the current
  preview frame returns the ``Latest_Frame`` within 100 ms. Because the read is a
  pure in-memory latest-frame-slot copy (no device grab), we measure the wall-clock
  time around ``broadcaster.get_frame`` after a frame has been published and assert it
  is comfortably under 100 ms.

* **Req 4.1 — available frame returned < 500 ms.** Same read path, asserted against
  the looser 500 ms bound for an available frame.

* **Req 4.2 — no-frame response < 500 ms.** When no ``Latest_Frame`` is available yet
  (session started, first frame not produced), a ``NO_FRAME`` response is returned
  within 500 ms. Measured around both ``broadcaster.get_frame`` (startup phase) and
  ``session.read_latest``.

* **Req 4.7 — stale response < 500 ms.** When the latest frame is older than the
  freshness ceiling, a ``STALE`` response is returned within 500 ms. Freshness is a
  pure function of the injected ``now`` vs the frame's ``acquired_at``, so we publish a
  frame and read it with an advanced ``now`` and measure the wall-clock time of the
  ``read_latest`` call.

All reads are pure in-memory operations, so the measured latencies are orders of
magnitude below the requirement bounds; the tests pin that this stays true. They are
fast and deterministic — the only real sleeps are tiny poll intervals while waiting
for the worker thread to publish the first frame.
"""
from __future__ import annotations

import time

import pytest

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import FrameStatus, SessionState, StreamConfig
from utils.streaming.session import StreamSession

CAMERA_ID = "Fake_1"

# Acceptance-criteria latency bounds (seconds).
FIRST_FRAME_BOUND_S = 10.0   # Req 3.10
READ_100MS_BOUND_S = 0.100   # Req 2.3
READ_500MS_BOUND_S = 0.500   # Req 4.1, 4.2, 4.7

# How many times to sample a read latency; we assert the worst-case sample.
_LATENCY_SAMPLES = 50


def _make_broadcaster(backend_kind, *, frames=None, default_grab=None,
                      stream_config=None, claim_registry=None):
    """Build a real ``StreamBroadcaster`` backed by a scripted mock backend.

    The injected ``backend_factory`` hands the broadcaster a mock backend of the
    requested family (``aravis`` / ``gstreamer``) so subscribe/start opens a real
    (in-memory) claim and the broadcaster spins up a *real* ``AcquisitionWorker``
    thread driving ``grab -> publish`` against the scripted frames.
    """
    backend_cls = MOCK_BACKEND_CLASSES[backend_kind]
    cfg = stream_config or StreamConfig()

    def factory(camera_id, config):
        return backend_cls(
            camera_id,
            frames=list(frames) if frames else None,
            default_grab=default_grab,
            claim_registry=claim_registry,
        )

    return StreamBroadcaster(stream_config=cfg, backend_factory=factory)


def _wait_for_status(broadcaster, camera_id, viewer_id, target_status, deadline_s):
    """Poll ``get_frame`` until it reports ``target_status`` or the deadline passes.

    Returns ``(elapsed_seconds, result)``. ``elapsed_seconds`` is the wall-clock time
    from the start of polling until the target status was first observed; if the
    deadline elapses first, ``result`` carries the last (non-matching) result so the
    caller can assert/diagnose.
    """
    start = time.perf_counter()
    last = None
    while True:
        last = broadcaster.get_frame(camera_id, viewer_id)
        if last.status == target_status:
            return time.perf_counter() - start, last
        if time.perf_counter() - start >= deadline_s:
            return time.perf_counter() - start, last
        time.sleep(0.005)


def _max_read_latency(read_callable, samples=_LATENCY_SAMPLES):
    """Return the worst-case wall-clock duration (seconds) of ``read_callable``."""
    worst = 0.0
    for _ in range(samples):
        start = time.perf_counter()
        read_callable()
        worst = max(worst, time.perf_counter() - start)
    return worst


# --------------------------------------------------------------------------- #
# Req 3.10 — first frame available within 10 s of session start
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend_kind", list(MOCK_BACKEND_CLASSES))
def test_first_frame_available_within_10s(backend_kind):
    """A new session makes the first frame available well within the 10 s bound.

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 3.10
    """
    registry = ClaimRegistry()
    # default_grab is a real frame so the worker keeps publishing fresh frames and
    # the stream never disconnects for the duration of the test.
    broadcaster = _make_broadcaster(
        backend_kind,
        frames=[make_raw_frame(seq=i) for i in range(3)],
        default_grab=make_raw_frame(seq=99),
        claim_registry=registry,
    )

    result = broadcaster.subscribe(CAMERA_ID, config={"type": "Camera"})
    assert result.accepted, f"subscribe rejected: {result.reason}"
    viewer_id = result.viewer_id
    try:
        elapsed, frame_result = _wait_for_status(
            broadcaster, CAMERA_ID, viewer_id,
            FrameStatus.OK, deadline_s=FIRST_FRAME_BOUND_S + 2.0,
        )
        assert frame_result.status == FrameStatus.OK, (
            f"first frame never became available (last status={frame_result.status})"
        )
        assert frame_result.frame is not None
        # The hard acceptance bound (Req 3.10); in practice this is a few ms.
        assert elapsed < FIRST_FRAME_BOUND_S, (
            f"first frame took {elapsed:.3f}s, exceeding the {FIRST_FRAME_BOUND_S}s bound"
        )
    finally:
        broadcaster.unsubscribe(CAMERA_ID, viewer_id)

    # The single device claim was released on stop-on-last (sanity on teardown).
    registry.assert_single_claim()
    assert registry.total_open() == 0


# --------------------------------------------------------------------------- #
# Req 2.3 / 4.1 — latest-frame read latency for an available frame
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend_kind", list(MOCK_BACKEND_CLASSES))
def test_available_frame_read_under_100ms(backend_kind):
    """Reading an available latest frame returns within 100 ms (and 500 ms).

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 2.3, 4.1
    """
    broadcaster = _make_broadcaster(
        backend_kind,
        frames=[make_raw_frame(seq=i) for i in range(3)],
        default_grab=make_raw_frame(seq=99),
    )
    result = broadcaster.subscribe(CAMERA_ID, config={"type": "Camera"})
    assert result.accepted
    viewer_id = result.viewer_id
    try:
        # Ensure a frame is actually available before timing the read path.
        _, ok = _wait_for_status(
            broadcaster, CAMERA_ID, viewer_id, FrameStatus.OK, deadline_s=FIRST_FRAME_BOUND_S
        )
        assert ok.status == FrameStatus.OK

        def read_ok():
            res = broadcaster.get_frame(CAMERA_ID, viewer_id)
            # Stays OK because the worker keeps republishing fresh frames.
            assert res.status == FrameStatus.OK
            return res

        worst = _max_read_latency(read_ok)
        assert worst < READ_100MS_BOUND_S, (
            f"worst available-frame read was {worst * 1000:.3f}ms, "
            f"exceeding the 100ms bound (Req 2.3)"
        )
        # The looser Req 4.1 bound is satisfied a fortiori.
        assert worst < READ_500MS_BOUND_S
    finally:
        broadcaster.unsubscribe(CAMERA_ID, viewer_id)


# --------------------------------------------------------------------------- #
# Req 4.2 — no-frame response within 500 ms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backend_kind", list(MOCK_BACKEND_CLASSES))
def test_no_frame_response_under_500ms(backend_kind):
    """A session with no frame yet returns NO_FRAME within 500 ms.

    The mock backend yields only grab timeouts (``None``), so the worker stays in its
    start-up phase tolerating failed grabs (no frame is published) and the session
    remains active — exactly the "started, no Latest_Frame available" condition.

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 4.2
    """
    # default_grab is None -> the worker never publishes a frame during the brief
    # window we measure (well before the 10 s first-frame timeout).
    broadcaster = _make_broadcaster(backend_kind, frames=None, default_grab=None)
    result = broadcaster.subscribe(CAMERA_ID, config={"type": "Camera"})
    assert result.accepted
    viewer_id = result.viewer_id
    try:
        def read_no_frame():
            res = broadcaster.get_frame(CAMERA_ID, viewer_id)
            assert res.status == FrameStatus.NO_FRAME, (
                f"expected NO_FRAME during start-up, got {res.status}"
            )
            assert res.frame is None
            return res

        worst = _max_read_latency(read_no_frame)
        assert worst < READ_500MS_BOUND_S, (
            f"worst no-frame response was {worst * 1000:.3f}ms, exceeding the 500ms bound"
        )
    finally:
        broadcaster.unsubscribe(CAMERA_ID, viewer_id)


def test_no_frame_session_read_under_500ms():
    """``StreamSession.read_latest`` returns NO_FRAME within 500 ms before any publish.

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 4.2
    """
    session = StreamSession(
        CAMERA_ID, backend=None, stream_config=StreamConfig(), state=SessionState.STARTING
    )

    def read():
        res = session.read_latest(now=time.time())
        assert res.status == FrameStatus.NO_FRAME
        return res

    worst = _max_read_latency(read)
    assert worst < READ_500MS_BOUND_S, (
        f"worst no-frame read was {worst * 1000:.3f}ms, exceeding the 500ms bound"
    )


# --------------------------------------------------------------------------- #
# Req 4.7 — stale response within 500 ms
# --------------------------------------------------------------------------- #
def test_stale_response_under_500ms():
    """A frame older than the freshness ceiling reads STALE within 500 ms.

    Freshness is a pure function of ``now - acquired_at`` vs ``stale_after_s``, so we
    publish a frame at ``t0`` and read it with an advanced ``now`` (past the ceiling).
    The wall-clock duration of the ``read_latest`` call itself is what Req 4.7 bounds.

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 4.7
    """
    cfg = StreamConfig(stale_after_s=2.0)
    session = StreamSession(
        CAMERA_ID, backend=None, stream_config=cfg, state=SessionState.RUNNING
    )
    # Publish a frame stamped at t0; later reads use an injected now well past the
    # 2 s freshness ceiling so the slot classifies STALE.
    session.publish(make_raw_frame(seq=1), now=1000.0)
    stale_now = 1000.0 + cfg.stale_after_s + 5.0

    def read_stale():
        res = session.read_latest(now=stale_now)
        assert res.status == FrameStatus.STALE, f"expected STALE, got {res.status}"
        # The stale frame is still returned (last-good payload), but flagged stale.
        assert res.frame is not None
        return res

    worst = _max_read_latency(read_stale)
    assert worst < READ_500MS_BOUND_S, (
        f"worst stale response was {worst * 1000:.3f}ms, exceeding the 500ms bound"
    )


def test_ok_session_read_under_100ms():
    """A fresh frame reads OK within 100 ms via ``StreamSession.read_latest``.

    Feature: concurrent-camera-stream-viewing, task 12.1
    Validates: Requirements 2.3
    """
    cfg = StreamConfig(stale_after_s=2.0)
    session = StreamSession(
        CAMERA_ID, backend=None, stream_config=cfg, state=SessionState.RUNNING
    )
    session.publish(make_raw_frame(seq=1), now=1000.0)

    def read_ok():
        res = session.read_latest(now=1000.0 + 0.1)  # within the freshness ceiling
        assert res.status == FrameStatus.OK
        return res

    worst = _max_read_latency(read_ok)
    assert worst < READ_100MS_BOUND_S, (
        f"worst OK read was {worst * 1000:.3f}ms, exceeding the 100ms bound"
    )
