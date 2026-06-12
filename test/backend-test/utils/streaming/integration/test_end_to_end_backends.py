#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""End-to-end fake-device tests for both backends (Task 12.5).

Feature: concurrent-camera-stream-viewing.

These exercise the **real** broadcast stream stack — :class:`StreamBroadcaster`
+ :class:`StreamSession` + :class:`AcquisitionWorker` — driving the **real**
``CameraBackend`` adapters end to end, with a fake/simulated device behind each
adapter so no physical camera is required (Req 2.8):

* **Aravis path** — a Fake Aravis camera. The Aravis "Fake" interface is enabled
  (``Aravis.enable_interface("Fake")``, the same call ``camera_manager.Camera``
  makes) and the real :class:`AravisBackend` opens / streams / grabs the fake
  ``Fake_1`` device through the existing ``camera_manager.Camera`` acquisition
  path.
* **GStreamer path** — a simulated source. The real :class:`GStreamerBackend`
  runs a ``videotestsrc``-based pipeline (supplied via its
  ``pipeline_description`` override), so frames are produced by GStreamer's own
  test source rather than hardware.

For each backend the full lifecycle is asserted (Req 2.8 backend parity):

    subscribe -> viewerId issued
    get_frame -> eventually OK with frame bytes
    viewer_count -> reflects the number of subscribers
    unsubscribe (all viewers) -> session released, claim dropped
    get_frame -> DISCONNECTED (no session)

These are integration/end-to-end tests (NOT property-based tests).

Environment note: the real adapters require the native ``gi`` / Aravis / Gst
stack, which is not importable in a bare checkout. The native-dependent setup is
guarded so the tests SKIP cleanly where the stack is absent and RUN in the
flask-app image / on a host that has ``gi`` + Aravis + Gst. Run, for example:

    PYTHONPATH=src/backend:test/backend-test/utils/streaming \\
        python -m pytest \\
        test/backend-test/utils/streaming/integration/test_end_to_end_backends.py \\
        --noconftest -v
"""
from __future__ import annotations

import time

import pytest

# The whole module is meaningless without the native ``gi`` bindings; skip the
# entire file cleanly on a bare checkout. ``exc_type=ImportError`` also skips when
# ``gi`` is present as a stub but its native ``_gi`` extension fails to import
# (the bare-checkout case), instead of erroring.
pytest.importorskip(
    "gi",
    reason="native gi bindings not available in this environment",
    exc_type=ImportError,
)

# ``broadcaster`` / ``backends`` are import-safe even without the native stack
# (their gi imports are lazy), so these top-level imports never trip the skip.
from utils.streaming.backends import AravisBackend, GStreamerBackend  # noqa: E402
from utils.streaming.broadcaster import StreamBroadcaster  # noqa: E402
from utils.streaming.models import FrameStatus, StreamConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# Native-stack availability guards (skip cleanly when a backend can't run here)
# --------------------------------------------------------------------------- #
def _require_aravis():
    """Skip unless the Aravis bindings are importable, returning the module.

    Mirrors ``camera_manager`` / ``backends._load_aravis_runtime``:
    ``gi.require_version('Aravis', '0.8')`` then import ``Aravis``. Any failure
    (missing typelib, version mismatch) results in a clean skip rather than an
    error, so this runs only where the GenICam stack is present.
    """
    try:
        import gi

        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis

        return Aravis
    except Exception as exc:  # ValueError / ImportError / etc.
        pytest.skip(f"Aravis stack not available: {exc}")


def _require_gst():
    """Skip unless the GStreamer bindings are importable, returning the module.

    Mirrors ``backends._load_gstreamer_runtime``: ``gi.require_version('Gst',
    '1.0')`` / ``GstApp`` then import. Any failure results in a clean skip.
    """
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst

        return Gst
    except Exception as exc:
        pytest.skip(f"GStreamer stack not available: {exc}")


# --------------------------------------------------------------------------- #
# Real-backend factories behind a fake/simulated device
# --------------------------------------------------------------------------- #
# A short freshness ceiling is fine: the poll loop reads frames promptly so they
# never age past it, but the lifecycle (start/stop/claim release) is what matters.
_CONFIG = StreamConfig(max_viewers=8, first_frame_timeout_s=10)

# The Aravis "Fake" interface exposes a single fake device addressed as "Fake_1"
# (the id used throughout the streaming test-suite and by camera_manager).
_FAKE_ARAVIS_CAMERA = "Fake_1"

# A self-contained, hardware-free GStreamer source. ``GStreamerBackend`` appends
# ``! videoconvert ! video/x-raw,format=RGB ! appsink ...`` to this description.
_GST_PIPELINE_DESCRIPTION = "videotestsrc is-live=true"


def _aravis_backend_factory(camera_id, config):
    """Build a real :class:`AravisBackend` bound to the Fake Aravis device."""
    return AravisBackend(camera_id, image_source_config=None, stream_config=_CONFIG)


def _gstreamer_backend_factory(camera_id, config):
    """Build a real :class:`GStreamerBackend` driven by a simulated videotestsrc."""
    return GStreamerBackend(
        camera_id,
        stream_config=_CONFIG,
        pipeline_description=_GST_PIPELINE_DESCRIPTION,
    )


# Each entry: (label, camera_id, broadcaster-factory, native-stack guard).
_BACKENDS = [
    pytest.param(
        "aravis", _FAKE_ARAVIS_CAMERA, _aravis_backend_factory, _require_aravis,
        id="aravis-fake-device",
    ),
    pytest.param(
        "gstreamer", "GstSim_1", _gstreamer_backend_factory, _require_gst,
        id="gstreamer-videotestsrc",
    ),
]


def _poll_until_ok(broadcaster, camera_id, viewer_id, timeout_s=12.0, interval_s=0.1):
    """Poll ``get_frame`` until it returns OK (or a non-recoverable status).

    The first frame may take a moment to flow through open -> start_stream ->
    worker grab -> publish, so NO_FRAME is expected transiently. Returns the
    final :class:`FrameResult` once OK is observed or the deadline passes.
    """
    deadline = time.monotonic() + timeout_s
    result = broadcaster.get_frame(camera_id, viewer_id)
    while time.monotonic() < deadline:
        if result.status == FrameStatus.OK:
            return result
        if result.status == FrameStatus.DISCONNECTED:
            # A real disconnect would never recover; surface it for the assertion.
            return result
        time.sleep(interval_s)
        result = broadcaster.get_frame(camera_id, viewer_id)
    return result


# --------------------------------------------------------------------------- #
# End-to-end lifecycle, parametrized across both backends (Req 2.8 parity) and a
# couple of representative subscriber counts (1-3 examples each).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("num_viewers", [1, 2])
@pytest.mark.parametrize("label,camera_id,backend_factory,guard", _BACKENDS)
def test_end_to_end_subscribe_frames_unsubscribe(
    label, camera_id, backend_factory, guard, num_viewers
):
    """subscribe -> frames -> viewer_count -> unsubscribe releases the claim.

    Runs the real broadcaster/session/worker against the real backend adapter
    with a fake (Aravis) / simulated (GStreamer) device, asserting the same
    lifecycle for both backends (Req 2.8).

    Validates: Requirements 2.8
    """
    # Enable the Aravis Fake interface up front (idempotent; camera_manager.Camera
    # also does this on construction). For GStreamer this is a no-op guard.
    native = guard()
    if label == "aravis":
        native.enable_interface("Fake")

    broadcaster = StreamBroadcaster(
        stream_config=_CONFIG, backend_factory=backend_factory
    )
    viewer_ids = []
    try:
        # --- subscribe: each subscribe issues a distinct viewer id (Req 8.1) ---
        for _ in range(num_viewers):
            result = broadcaster.subscribe(camera_id)
            assert result.accepted is True, (
                f"[{label}] subscribe was rejected: reason={result.reason}"
            )
            assert result.viewer_id is not None
            viewer_ids.append(result.viewer_id)

        assert len(set(viewer_ids)) == num_viewers, "viewer ids must be distinct"

        # --- viewer_count reflects the number of subscribers (Req 8.4) ---
        assert broadcaster.viewer_count(camera_id) == num_viewers

        # --- frames: get_frame eventually returns OK with frame bytes (Req 2.3) ---
        first_viewer = viewer_ids[0]
        frame_result = _poll_until_ok(broadcaster, camera_id, first_viewer)
        assert frame_result.status == FrameStatus.OK, (
            f"[{label}] expected OK frame, got {frame_result.status} "
            f"(error={frame_result.error})"
        )
        assert frame_result.frame is not None
        assert isinstance(frame_result.frame.data, (bytes, bytearray))
        assert len(frame_result.frame.data) > 0, "frame payload must be non-empty"

        # Every subscriber reads the identical latest frame within the interval
        # (single shared acquisition fanned out, Req 2.5).
        if num_viewers > 1:
            seqs = {
                broadcaster.get_frame(camera_id, vid).frame.seq
                for vid in viewer_ids
            }
            assert len(seqs) == 1, "all viewers must observe the same latest frame seq"

        # --- unsubscribe all but the last: session stays up (Req 3.7 / 8.7) ---
        for vid in viewer_ids[:-1]:
            broadcaster.unsubscribe(camera_id, vid)
        assert broadcaster.viewer_count(camera_id) == 1
        # Still streaming for the remaining viewer.
        assert broadcaster.get_frame(camera_id, viewer_ids[-1]).status in (
            FrameStatus.OK,
            FrameStatus.NO_FRAME,
            FrameStatus.STALE,
        )

        # --- unsubscribe the last viewer: stop-on-last releases the claim ---
        broadcaster.unsubscribe(camera_id, viewer_ids[-1])
        viewer_ids.clear()
        assert broadcaster.viewer_count(camera_id) == 0

        # --- a subsequent get_frame returns DISCONNECTED (no session, Req 7.5) ---
        post = broadcaster.get_frame(camera_id, "nonexistent-viewer")
        assert post.status == FrameStatus.DISCONNECTED
        assert post.frame is None
    finally:
        # Best-effort teardown so a failed assertion never leaks an open claim.
        for vid in viewer_ids:
            try:
                broadcaster.unsubscribe(camera_id, vid)
            except Exception:
                pass
