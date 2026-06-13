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
"""Wall-clock refresh-rate and viewer-independence performance tests (task 12.2).

These are *integration / performance* tests, not property tests: they drive the
**real** :class:`AcquisitionWorker` on its own thread against a real
:class:`StreamSession` and a mock :class:`CameraBackend` whose ``grab()`` cadence
we control, then measure the actual publish cadence with the real
``time.monotonic`` wall clock over a small bounded window (~1-2 s). They cover the
wall-clock side of the design's refresh-rate acceptance criteria:

* **Req 4.3** — a stream refreshes at >= 5 fps when the source can deliver >= 5 fps.
  The worker caps the producer at ``StreamConfig.min_refresh_fps`` (5 fps by
  default), so a source that can grab arbitrarily fast is published at ~5 fps.
* **Req 4.4** — when the device is slower than the 5 fps ceiling, the refresh rate
  tracks the device's own cadence (no artificial delay is added below the ceiling).
  We model a slow source with a ``grab()`` that sleeps, and assert the measured
  cadence matches the device rate rather than being pinned to 5 fps or stalling.
* **Req 4.5 (wall-clock side)** — the producer cadence is independent of the number
  of viewers. We run the identical fast source with 1 reader vs ``max_viewers`` (8)
  readers polling the latest-frame slot concurrently and assert the publish cadence
  is essentially unchanged (drop-to-latest: reads never slow or block the producer).

Timing tests are inherently sensitive to scheduler jitter, so each assertion uses a
generous tolerance band around the expected cadence rather than an exact equality,
and the cadence is measured between the first and last publish (independent of where
the measurement window happens to start/stop) to keep the estimate robust.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import pytest

# --- import bootstrap -------------------------------------------------------
# This test lives one directory below the other streaming tests, so make the
# sibling ``mock_camera_backend`` module and the ``src/backend`` source root
# importable with the same bare import style the sibling tests use, regardless of
# pytest's rootdir / invocation directory.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STREAMING_DIR = os.path.dirname(_THIS_DIR)
_BACKEND_SRC = os.path.abspath(
    os.path.join(_THIS_DIR, "..", "..", "..", "..", "..", "src", "backend")
)
for _p in (_STREAMING_DIR, _BACKEND_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mock_camera_backend import make_backend, make_raw_frame  # noqa: E402
from utils.streaming.models import StreamConfig, Viewer  # noqa: E402
from utils.streaming.session import StreamSession  # noqa: E402
from utils.streaming.worker import AcquisitionWorker  # noqa: E402

CAMERA_ID = "Fake_1"

# Default refresh ceiling the worker enforces for a fast-enough source (Req 4.3).
CEILING_FPS = StreamConfig().min_refresh_fps           # 5.0
CEILING_INTERVAL_S = 1.0 / CEILING_FPS                 # 0.2 s
MAX_VIEWERS = StreamConfig().max_viewers               # 8

# A deliberately slow device cadence, comfortably below the 5 fps ceiling, used to
# exercise the "track the device, don't add delay" path (Req 4.4).
SLOW_GRAB_S = 0.4                                       # -> ~2.5 fps device cadence
SLOW_DEVICE_FPS = 1.0 / SLOW_GRAB_S                     # 2.5

# Measurement windows (wall-clock seconds). Kept small but long enough to capture
# several publishes so the cadence estimate is meaningful.
FAST_WINDOW_S = 1.5
SLOW_WINDOW_S = 2.0


def _make_viewer(now: float) -> Viewer:
    """Build a Viewer with a unique id for registration in a session."""
    return Viewer(
        viewer_id=str(uuid.uuid4()),
        camera_id=CAMERA_ID,
        subscribed_at=now,
        last_active=now,
    )


def _fast_grab():
    """A source that can deliver frames arbitrarily fast (near-instant grab)."""
    return make_raw_frame(seq=0, width=8, height=8)


def _slow_grab():
    """A source slower than the refresh ceiling: each grab takes ``SLOW_GRAB_S``."""
    time.sleep(SLOW_GRAB_S)
    return make_raw_frame(seq=0, width=8, height=8)


def _run_worker_window(grab_fn, *, window_s, num_readers=0, stream_config=None):
    """Run the real worker thread for ``window_s`` and record publish timestamps.

    Drives the real :class:`AcquisitionWorker` (real ``time.monotonic`` clock and
    ``time.sleep``) against a mock backend whose ``grab()`` is ``grab_fn`` (reused
    indefinitely via ``default_grab``). ``num_readers`` viewer threads poll
    ``read_latest`` concurrently to model fan-out reads. The worker is signalled to
    stop after ``window_s`` and joined.

    Returns ``(publish_monotonic_times, elapsed_s)`` where ``publish_monotonic_times``
    is the wall-clock (monotonic) instant of every publish in order.
    """
    config = stream_config or StreamConfig()

    # default_grab is reused once the (empty) scripted queue drains, so the backend
    # serves frames forever at whatever cadence grab_fn imposes; the worker therefore
    # runs until we stop it rather than hitting a disconnect on an exhausted queue.
    backend = make_backend("aravis", camera_id=CAMERA_ID, default_grab=grab_fn)
    # The broadcaster owns open/start_stream in production; the worker only
    # grab->publishes. Acquire the claim and start acquisition before running.
    backend.open()
    backend.start_stream()

    session = StreamSession(CAMERA_ID, backend=backend, stream_config=config)

    for _ in range(num_readers):
        session.add_viewer(_make_viewer(time.time()))
    assert session.viewer_count() == num_readers

    publish_times: list[float] = []
    record_lock = threading.Lock()
    real_publish = session.publish

    def recording_publish(frame, width=None, height=None, now=0.0):
        result = real_publish(frame, width=width, height=height, now=now)
        with record_lock:
            publish_times.append(time.monotonic())
        return result

    session.publish = recording_publish

    # Concurrent fan-out readers: poll the latest-frame slot in a tight-but-not-spin
    # loop, mirroring real viewers. Pure slot reads must never slow the producer.
    stop_readers = threading.Event()

    def reader_loop():
        while not stop_readers.is_set():
            session.read_latest(time.time())
            time.sleep(0.005)

    reader_threads = [
        threading.Thread(target=reader_loop, name=f"reader-{i}", daemon=True)
        for i in range(num_readers)
    ]
    for t in reader_threads:
        t.start()

    worker = AcquisitionWorker(session, stream_config=config)

    start = time.monotonic()
    worker.start()
    time.sleep(window_s)
    worker.stop()
    worker.join(timeout=SLOW_GRAB_S + 2.0)
    elapsed = time.monotonic() - start

    stop_readers.set()
    for t in reader_threads:
        t.join(timeout=1.0)

    return publish_times, elapsed


def _cadence_fps(publish_times):
    """Estimate the publish cadence (fps) from recorded publish timestamps.

    Measured between the first and last publish so the estimate reflects the actual
    inter-publish interval and is independent of exactly where the measurement window
    opened or closed. Requires at least two publishes.
    """
    assert len(publish_times) >= 2, (
        f"need >= 2 publishes to estimate cadence, got {len(publish_times)}"
    )
    span = publish_times[-1] - publish_times[0]
    assert span > 0.0, "publish timestamps did not advance"
    return (len(publish_times) - 1) / span


# --------------------------------------------------------------------------- #
# Req 4.3: a >= 5 fps source refreshes at >= 5 fps
# --------------------------------------------------------------------------- #
def test_refresh_meets_5fps_for_fast_source():
    """A source that can grab >= 5 fps is published at ~>= 5 fps (Req 4.3).

    The worker caps the producer at the 5 fps refresh ceiling, so a near-instant
    grab source is published at approximately 5 fps. We allow a tolerance below 5
    fps for scheduler jitter (the ``time.sleep`` that paces the ceiling may overrun
    slightly), and a modest tolerance above to confirm the ceiling is roughly
    respected rather than free-running far faster.
    """
    publish_times, elapsed = _run_worker_window(_fast_grab, window_s=FAST_WINDOW_S)

    fps = _cadence_fps(publish_times)
    # Lower bound: ~5 fps with a 10% jitter allowance (Req 4.3, "at least 5 fps").
    assert fps >= CEILING_FPS * 0.9, (
        f"refresh {fps:.2f} fps below the >= 5 fps requirement for a fast source "
        f"({len(publish_times)} publishes in {elapsed:.2f}s)"
    )
    # Upper sanity bound: the producer is paced by the ceiling, not free-running.
    assert fps <= CEILING_FPS * 1.5, (
        f"refresh {fps:.2f} fps far exceeds the ~5 fps ceiling; pacing may be broken"
    )


# --------------------------------------------------------------------------- #
# Req 4.4: a source slower than the ceiling tracks the device cadence
# --------------------------------------------------------------------------- #
def test_refresh_tracks_device_rate_below_ceiling():
    """A sub-5-fps source refreshes at the device cadence, not the ceiling (Req 4.4).

    With a ``grab()`` that takes 0.4 s (~2.5 fps), the per-iteration grab already
    overruns the 0.2 s ceiling interval, so the worker adds no extra delay and the
    publish cadence tracks the device. We assert the measured cadence is close to the
    device's ~2.5 fps (within a tolerance band) and clearly below the 5 fps ceiling —
    i.e. the worker neither pins it to 5 fps nor stalls it well under the device rate.
    """
    publish_times, elapsed = _run_worker_window(_slow_grab, window_s=SLOW_WINDOW_S)

    fps = _cadence_fps(publish_times)
    # Tracks the device: comfortably below the 5 fps ceiling.
    assert fps < CEILING_FPS * 0.9, (
        f"slow-source refresh {fps:.2f} fps is not below the 5 fps ceiling; the worker "
        f"may be adding/forcing cadence ({len(publish_times)} publishes in {elapsed:.2f}s)"
    )
    # Tracks the device: no artificial delay below the device's own ~2.5 fps cadence.
    # Generous band to absorb grab + scheduling overhead on either side.
    assert SLOW_DEVICE_FPS * 0.7 <= fps <= SLOW_DEVICE_FPS * 1.25, (
        f"slow-source refresh {fps:.2f} fps does not track the ~{SLOW_DEVICE_FPS:.1f} fps "
        f"device cadence ({len(publish_times)} publishes in {elapsed:.2f}s)"
    )


# --------------------------------------------------------------------------- #
# Req 4.5 (wall-clock side): refresh rate is independent of viewer count
# --------------------------------------------------------------------------- #
def test_refresh_rate_independent_of_viewer_count():
    """Publish cadence is essentially unchanged from 1 to 8 concurrent viewers (Req 4.5).

    Drop-to-latest means viewer reads are pure, non-blocking slot copies that never
    apply backpressure to the producer. We run the identical fast source twice — once
    with a single reader, once with ``max_viewers`` (8) readers all polling
    concurrently — and assert the measured publish cadence is essentially the same and
    still meets the ~5 fps ceiling in both cases.
    """
    single_times, single_elapsed = _run_worker_window(
        _fast_grab, window_s=FAST_WINDOW_S, num_readers=1
    )
    multi_times, multi_elapsed = _run_worker_window(
        _fast_grab, window_s=FAST_WINDOW_S, num_readers=MAX_VIEWERS
    )

    fps_single = _cadence_fps(single_times)
    fps_multi = _cadence_fps(multi_times)

    # Both viewer counts still hit the ~5 fps ceiling (10% jitter allowance).
    assert fps_single >= CEILING_FPS * 0.9, (
        f"1-viewer refresh {fps_single:.2f} fps below the 5 fps ceiling"
    )
    assert fps_multi >= CEILING_FPS * 0.9, (
        f"{MAX_VIEWERS}-viewer refresh {fps_multi:.2f} fps below the 5 fps ceiling; "
        f"concurrent reads appear to be slowing the producer"
    )

    # Viewer-independence: 8 concurrent readers must not measurably slow the producer
    # relative to a single reader (allow a 20% band for wall-clock jitter).
    assert fps_multi >= fps_single * 0.8, (
        f"refresh dropped from {fps_single:.2f} fps (1 viewer) to {fps_multi:.2f} fps "
        f"({MAX_VIEWERS} viewers); reads are applying backpressure to the producer"
    )


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    sys.exit(pytest.main([__file__, "-v"]))
