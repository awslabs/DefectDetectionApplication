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
"""Integration / timing tests: new-subscriber latency and applied-setting visibility.

Feature: concurrent-camera-stream-viewing (task 12.3). These are wall-clock
**integration / timing** tests, *not* Hypothesis property tests, so they do not
update PBT status. They drive the real :class:`StreamBroadcaster` +
:class:`AcquisitionWorker` (a live producer thread) against the hardware-free
:class:`MockCameraBackend`, exercising the genuine subscribe / get_frame /
apply_settings code paths end-to-end.

Requirements under test
-----------------------
* **Req 1.4 — a new subscriber starts receiving frames quickly with no disruption
  to existing viewers.** With a session already running and existing viewers
  reading frames, a freshly-subscribed viewer must obtain an ``OK`` frame well
  within 2000 ms of subscribing, while the existing viewers keep receiving fresh
  frames (their sequence numbers keep advancing and they never observe
  ``DISCONNECTED``) and the single device claim is reused (never re-opened).

* **Req 5.2 — an applied setting becomes visible within 5 frames.** After
  ``apply_settings`` is applied to a running session, a published frame that
  reflects the applied control values must appear within 5 subsequent published
  frames.

Modelling "visible within N frames" with the mock
--------------------------------------------------
The real device has no notion of "the frame carries the current settings", so the
mock is scripted to *tag* every frame it produces with the controls currently in
effect. The acquisition worker is the single caller of ``grab()``; each frame the
mock returns from ``grab()`` encodes ``backend.last_applied_features`` — exactly the
device-accepted control set recorded by ``apply_features`` — into its ``data``
payload (``seq=<n>|settings=<tag>``). Because ``StreamBroadcaster.apply_settings``
calls ``backend.apply_features`` synchronously, every frame grabbed *after* the apply
carries the new tag.

A "published frame" is one acquisition interval, identified by the session's
monotonic ``seq``. Modelling "the applied setting is visible in frame K" therefore
means: a viewer's ``get_frame`` returns a frame whose tag reflects the applied
values. The test records the latest ``seq`` visible at the moment ``apply_settings``
returns (the baseline), then counts how many *new* distinct published frames
(``seq`` greater than the baseline) appear before one reflects the applied tag, and
asserts that count is ``<= 5`` (Req 5.2). The worker's refresh ceiling is slowed for
these tests (``min_refresh_fps`` small) so the poll loop reliably observes every
new ``seq`` rather than skipping past them.

No production code is modified. Only the public test double's existing
``default_grab`` hook (a per-grab callable, already supported) is used to script the
tagged frames; the broadcaster / worker / session run completely unchanged.

Run note
--------
This file imports only the import-safe streaming stack (no ``gi`` / device stack),
so it is intended to be run bypassing the backend-wide ``conftest.py``::

    PYTHONPATH=src/backend python3.9 -m pytest \
        test/backend-test/utils/streaming/integration/test_subscriber_and_settings_timing.py \
        --noconftest -p no:cacheprovider
"""
from __future__ import annotations

import itertools
import os
import sys
import time
from typing import Callable, List, Optional, Set

import pytest

# The shared test double (``mock_camera_backend``) lives in the parent
# ``streaming`` directory. Sibling tests in that directory import it directly
# because pytest puts the test's own directory on ``sys.path``; this file lives in
# the ``integration`` subdirectory, so we add the parent directory explicitly to
# replicate the sibling import (``from mock_camera_backend import ...``).
_STREAMING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _STREAMING_DIR not in sys.path:
    sys.path.insert(0, _STREAMING_DIR)

from mock_camera_backend import (  # noqa: E402  (import after sys.path shim)
    BACKEND_KINDS,
    ClaimRegistry,
    MockCameraBackend,
    make_backend,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster  # noqa: E402
from utils.streaming.models import FrameStatus, SessionState, StreamConfig  # noqa: E402

# A single physical camera is enough for these timing tests.
CAMERA = "Fake_timing_1"

# Generous max_viewers so we can add a new subscriber on top of existing ones.
# A small, positive refresh ceiling deliberately *slows* the producer so the
# polling loops below reliably observe every published frame (every seq) without
# skipping — important for the "within 5 frames" count to be meaningful. At 20 fps
# the cadence is ~50 ms; five frames take ~250 ms, far inside the test budget.
_REFRESH_FPS = 20.0
_STREAM_CONFIG = StreamConfig(
    max_viewers=8,
    min_refresh_fps=_REFRESH_FPS,
    # Keep frames classified OK between polls: the freshness ceiling is far larger
    # than the inter-frame cadence so a just-published frame never reads STALE.
    stale_after_s=5.0,
    first_frame_timeout_s=10,
)

# The new-subscriber acceptance bound (Req 1.4). The assertion uses the full bound;
# in practice the latency is sub-cadence (tens of ms) because the session already
# has a published latest frame the moment the new viewer subscribes.
_NEW_SUBSCRIBER_BUDGET_S = 2.0

# Upper bound on published frames for the applied setting to become visible (Req 5.2).
_MAX_FRAMES_TO_VISIBLE = 5


def _settings_tag(applied: Optional[dict]) -> str:
    """Render a device-accepted control set into a stable, comparable tag string.

    ``None`` / empty (no apply yet) renders as ``"none"``; otherwise the controls
    are rendered in sorted-key order so the tag is deterministic regardless of dict
    ordering.
    """
    if not applied:
        return "none"
    return ",".join(f"{key}={applied[key]}" for key in sorted(applied))


def _tagging_grab_factory(backend: MockCameraBackend) -> Callable[[], object]:
    """Return a zero-arg ``grab`` producer that tags each frame with live settings.

    The acquisition worker is the single caller of ``grab()``. Each produced
    :class:`RawFrame` carries a monotonic local counter and the controls currently
    in effect on the backend (``last_applied_features``, set by ``apply_features``)
    encoded into its ``data`` as ``seq=<n>|settings=<tag>``. This is how we model
    "the published frame reflects the applied setting" with a hardware-free double.
    """
    counter = itertools.count(1)

    def produce() -> object:
        n = next(counter)
        tag = _settings_tag(backend.last_applied_features)
        data = f"seq={n}|settings={tag}".encode("utf-8")
        return make_raw_frame(seq=n, width=8, height=8, data=data)

    return produce


def _make_broadcaster(
    backend_kind: str, registry: ClaimRegistry, created: List[MockCameraBackend]
) -> StreamBroadcaster:
    """Build a broadcaster whose factory yields tagging mock backends.

    Every backend shares one :class:`ClaimRegistry` (so the test reads the true
    per-camera open-claim count) and records itself into ``created`` (so the test
    can assert no second claim/backend was built). ``time_fn`` is the real wall
    clock so freshness and the measured latencies use real time — these are
    wall-clock timing tests.
    """

    def backend_factory(camera_id: str, config: Optional[dict]) -> MockCameraBackend:
        backend = make_backend(backend_kind, camera_id, claim_registry=registry)
        # Script an unbounded supply of settings-tagged frames for the worker loop.
        backend.default_grab = _tagging_grab_factory(backend)
        created.append(backend)
        return backend

    return StreamBroadcaster(
        stream_config=_STREAM_CONFIG,
        backend_factory=backend_factory,
        time_fn=time.time,
    )


def _wait_for_ok_frame(
    broadcaster: StreamBroadcaster,
    camera: str,
    viewer_id: str,
    *,
    timeout_s: float,
    poll_s: float = 0.005,
):
    """Poll ``get_frame`` until it returns an ``OK`` frame or the timeout elapses.

    Returns ``(frame, elapsed_seconds)`` on success, or ``(None, elapsed_seconds)``
    if no OK frame arrived within ``timeout_s``.
    """
    start = time.time()
    deadline = start + timeout_s
    while time.time() < deadline:
        result = broadcaster.get_frame(camera, viewer_id)
        if result.status == FrameStatus.OK and result.frame is not None:
            return result.frame, time.time() - start
        time.sleep(poll_s)
    return None, time.time() - start


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
def test_new_subscriber_starts_receiving_without_disrupting_existing_viewers(backend_kind):
    """A new subscriber gets frames < 2000 ms with no disruption to existing viewers.

    Feature: concurrent-camera-stream-viewing (task 12.3).
    Validates: Requirement 1.4.
    """
    registry = ClaimRegistry()
    created: List[MockCameraBackend] = []
    broadcaster = _make_broadcaster(backend_kind, registry, created)

    existing_viewers: List[str] = []
    new_viewer: Optional[str] = None
    try:
        # --- a running session with two existing viewers already reading frames ---
        for _ in range(2):
            result = broadcaster.subscribe(CAMERA, {"type": "Camera"})
            assert result.accepted, "subscribe within max_viewers must be accepted"
            existing_viewers.append(result.viewer_id)

        assert registry.open_count(CAMERA) == 1, "exactly one device claim for the camera"
        assert len(created) == 1, "first subscribe builds exactly one backend"

        # Wait until the existing viewers are actually receiving frames (the worker
        # has published its first frame and the session is RUNNING).
        first_frame, _ = _wait_for_ok_frame(
            broadcaster, CAMERA, existing_viewers[0], timeout_s=_STREAM_CONFIG.first_frame_timeout_s
        )
        assert first_frame is not None, "existing viewers should be receiving frames before we add one"
        assert broadcaster._sessions[CAMERA].state == SessionState.RUNNING

        # Snapshot what an existing viewer currently sees so we can prove its stream
        # keeps advancing (no disruption) across the new subscription.
        before = broadcaster.get_frame(CAMERA, existing_viewers[0])
        assert before.status == FrameStatus.OK and before.frame is not None
        existing_seq_before = before.frame.seq

        opens_before = created[0].open_count

        # --- the operation under test: subscribe a brand-new viewer ---
        t0 = time.time()
        new_result = broadcaster.subscribe(CAMERA, {"type": "Camera"})
        assert new_result.accepted, "the new viewer must be accepted onto the running session"
        new_viewer = new_result.viewer_id
        assert new_viewer not in existing_viewers
        # The new subscriber must start receiving frames well within 2000 ms.
        new_frame, latency_s = _wait_for_ok_frame(
            broadcaster, CAMERA, new_viewer, timeout_s=_NEW_SUBSCRIBER_BUDGET_S
        )
        total_since_subscribe = time.time() - t0
        assert new_frame is not None, (
            f"new subscriber did not receive a frame within {_NEW_SUBSCRIBER_BUDGET_S * 1000:.0f} ms"
        )
        assert total_since_subscribe < _NEW_SUBSCRIBER_BUDGET_S, (
            f"new subscriber latency {total_since_subscribe * 1000:.1f} ms exceeded the "
            f"{_NEW_SUBSCRIBER_BUDGET_S * 1000:.0f} ms bound (Req 1.4)"
        )

        # --- no disruption to existing viewers ---
        # The new subscription reused the single claim (no second open / backend).
        assert registry.open_count(CAMERA) == 1, "the single claim must be reused, not duplicated"
        assert not registry.overlap_detected
        assert len(created) == 1, "no second backend may be built for the new subscriber"
        assert created[0].open_count == opens_before, "claim must not be re-opened for a new viewer"

        # The existing viewer keeps getting fresh frames: its sequence advances and it
        # never observes DISCONNECTED while the new viewer is being added.
        after, _ = _wait_for_ok_frame(
            broadcaster, CAMERA, existing_viewers[0], timeout_s=_NEW_SUBSCRIBER_BUDGET_S
        )
        assert after is not None, "existing viewer must keep receiving frames (no disruption)"
        assert after.seq >= existing_seq_before, "existing viewer's stream must keep advancing"

        # Sample the existing viewer a few times: it must remain OK throughout.
        for _ in range(5):
            sample = broadcaster.get_frame(CAMERA, existing_viewers[1])
            assert sample.status in (FrameStatus.OK, FrameStatus.NO_FRAME), (
                f"existing viewer disrupted: status={sample.status}"
            )
            assert sample.status != FrameStatus.DISCONNECTED
            time.sleep(0.01)

        assert broadcaster.viewer_count(CAMERA) == 3
    finally:
        # Tear down all viewers so the worker stops and the claim is released.
        for viewer_id in existing_viewers:
            broadcaster.unsubscribe(CAMERA, viewer_id)
        if new_viewer is not None:
            broadcaster.unsubscribe(CAMERA, new_viewer)
        assert registry.open_count(CAMERA) == 0, "claim must be released once all viewers leave"


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
def test_applied_setting_becomes_visible_within_five_frames(backend_kind):
    """An applied setting is reflected in a published frame within 5 frames.

    Feature: concurrent-camera-stream-viewing (task 12.3).
    Validates: Requirement 5.2.

    See the module docstring for how "visible within N frames" is modelled with the
    settings-tagging mock: each grabbed frame encodes the controls in effect, so a
    frame reflecting the applied values must appear within 5 published frames (seqs)
    after ``apply_settings`` returns.
    """
    registry = ClaimRegistry()
    created: List[MockCameraBackend] = []
    broadcaster = _make_broadcaster(backend_kind, registry, created)

    viewer_id = None
    try:
        result = broadcaster.subscribe(CAMERA, {"type": "Camera"})
        assert result.accepted
        viewer_id = result.viewer_id

        # Ensure the session is running and producing (settings-untagged) frames.
        first_frame, _ = _wait_for_ok_frame(
            broadcaster, CAMERA, viewer_id, timeout_s=_STREAM_CONFIG.first_frame_timeout_s
        )
        assert first_frame is not None
        # Before any apply, frames carry the "none" tag.
        assert "settings=none" in first_frame.data.decode("utf-8")

        # The valid control values to apply live. A distinctive gain value keeps the
        # resulting tag unambiguous in the frame payload.
        features = {"gain": 37.0, "exposure": 1250.0}
        expected_tag = _settings_tag(MockCameraBackend._normalize_features(features))

        # --- the operation under test: apply settings to the live session ---
        accepted = broadcaster.apply_settings(CAMERA, features)
        assert accepted == MockCameraBackend._normalize_features(features)

        # Baseline: the latest published seq visible at the moment the apply returned.
        baseline = broadcaster.get_frame(CAMERA, viewer_id)
        assert baseline.status == FrameStatus.OK and baseline.frame is not None
        baseline_seq = baseline.frame.seq

        # Poll faster than the producer cadence so every new published frame (seq) is
        # observed. Count distinct *new* seqs until one reflects the applied tag.
        new_seqs: Set[int] = set()
        visible_seq: Optional[int] = None
        deadline = time.time() + 2.0  # ample: 5 frames at ~50 ms cadence is ~250 ms
        while time.time() < deadline:
            res = broadcaster.get_frame(CAMERA, viewer_id)
            if res.status == FrameStatus.OK and res.frame is not None:
                frame = res.frame
                if frame.seq > baseline_seq:
                    new_seqs.add(frame.seq)
                    if expected_tag in frame.data.decode("utf-8"):
                        visible_seq = frame.seq
                        break
            time.sleep(0.005)

        assert visible_seq is not None, (
            f"applied setting tag {expected_tag!r} never became visible within the budget"
        )

        # Number of published frames from baseline until the setting was visible: the
        # count of new distinct seqs up to and including the one that reflected it.
        frames_until_visible = len({s for s in new_seqs if s <= visible_seq})
        assert frames_until_visible <= _MAX_FRAMES_TO_VISIBLE, (
            f"applied setting took {frames_until_visible} published frames to become "
            f"visible; Req 5.2 allows at most {_MAX_FRAMES_TO_VISIBLE}"
        )

        # The session stayed intact throughout the live apply (Req 5.2 context).
        assert broadcaster._sessions[CAMERA].state == SessionState.RUNNING
        assert registry.open_count(CAMERA) == 1
    finally:
        if viewer_id is not None:
            broadcaster.unsubscribe(CAMERA, viewer_id)
        assert registry.open_count(CAMERA) == 0
