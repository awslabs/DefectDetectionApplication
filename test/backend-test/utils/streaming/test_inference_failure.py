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
"""Property-based test for inference-failure session integrity.

Feature: concurrent-camera-stream-viewing, Property 17: Inference failure leaves
the session intact — for any active session, if an inference frame request fails
or the camera becomes unavailable for that request, an error / no-frame indication
is returned to the caller while the session remains RUNNING with its claim
retained.

Validates: Requirements 6.6.

How the property is exercised
-----------------------------
A real :class:`StreamBroadcaster` is wired with an injected ``backend_factory``
(building the shared :class:`MockCameraBackend` registered against one shared
:class:`ClaimRegistry`) and an injected :class:`MockClock`, exactly like the
single-claim / disconnection-cascade property tests. For each example we:

1. Subscribe a generated number of viewers to a single camera (start-on-first
   opens the one device claim; the rest reuse it). The session is now ACTIVE
   (RUNNING) holding exactly one claim.
2. Make the inference frame *read* fail in one of two ways the active-session
   path can encounter (per task 7.1 the active path is a pure latest-slot read
   that opens no second claim and never stops the session):

   * ``no_frame`` — leave the latest-frame slot empty (no publish). The session
     read returns NO_FRAME, so ``get_inference_frame`` returns ``None`` (the
     no-frame / error indication).
   * ``read_error`` — script the session's slot read to raise (modelling a frame
     read that errors / the camera becoming unavailable for that request). The
     broadcaster's defensive branch swallows it and returns ``None`` while
     leaving the live session untouched.

3. Call ``get_inference_frame`` (optionally several times).

We then assert, for every variation and viewer count, that the inference request
returned ``None`` (the error / no-frame indication) WITHOUT disturbing the live
session (Req 6.6):

* the session still exists and its state is still ``RUNNING``;
* the viewer count is unchanged;
* the single device claim is still held (``registry.open_count == 1``) — no second
  claim was opened (``backend.open_count == 1``) and the claim was **not** released
  (``backend.close_count == 0``); and
* the single-claim invariant held throughout (no overlap, no release without a
  matching open).

Test-only technique (no production code changed)
------------------------------------------------
Like ``test_broadcaster_claim.py`` / ``test_broadcaster_disconnect.py``, this
patches the ``AcquisitionWorker`` symbol in the broadcaster module with a no-op
stub for the duration of each example so no real daemon thread / wall-clock grab
loop runs. The stub marks the session RUNNING on ``start()`` (mirroring the real
worker after its first published frame), which is exactly the live state the
property quantifies over. The ``read_error`` variation patches the *session
instance's* ``read_latest`` for the single inference call to raise — a pure test
seam that drives the broadcaster's existing defensive path; production code is
untouched.
"""
from __future__ import annotations

import unittest.mock as mock
from typing import Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    MockClock,
)
from utils.streaming import broadcaster as broadcaster_module
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import SessionState, StreamConfig

# Keep max_viewers comfortably above the generated viewer count so the active
# session — not the viewer-limit branch (Property 6) — is what we exercise.
_CONFIG = StreamConfig(max_viewers=8, stale_after_s=2.0)
_CAMERA = "Fake_inference_fail_1"


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the broadcaster's construction/usage contract
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so the session state is deterministic. ``start()`` marks the session
    RUNNING to mirror the real worker after its first published frame.
    """

    def __init__(self, session, *, stream_config=None, time_fn=None, **kwargs) -> None:
        self.session = session

    def start(self):
        self.session.state = SessionState.RUNNING
        return None

    def stop(self) -> None:
        return None

    def join(self, timeout: Optional[float] = None) -> None:
        return None


# Feature: concurrent-camera-stream-viewing, Property 17: Inference failure leaves the session intact
# Validates: Requirements 6.6
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=150, deadline=None)
@given(
    num_viewers=st.integers(min_value=1, max_value=_CONFIG.max_viewers),
    failure_mode=st.sampled_from(["no_frame", "read_error"]),
    num_requests=st.integers(min_value=1, max_value=4),
    clock_advance=st.floats(
        min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_17_inference_failure_leaves_session_intact(
    backend_cls, num_viewers, failure_mode, num_requests, clock_advance
):
    """A failed/unavailable inference request returns None while the active
    session stays RUNNING with its single claim retained and viewer set unchanged.

    Feature: concurrent-camera-stream-viewing, Property 17: Inference failure leaves the session intact
    Validates: Requirements 6.6
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1000.0)
    created: dict[str, object] = {}

    def backend_factory(camera_id, config):
        backend = backend_cls(
            camera_id,
            claim_registry=registry,
            clock=clock,
        )
        created[camera_id] = backend
        return backend

    broadcaster = StreamBroadcaster(
        stream_config=_CONFIG,
        backend_factory=backend_factory,
        time_fn=clock.time,
    )

    with mock.patch.object(broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker):
        # 1. Subscribe the generated number of viewers: start-on-first opens the
        #    one claim, the rest reuse it. The session is now ACTIVE (RUNNING).
        viewer_ids = []
        for _ in range(num_viewers):
            result = broadcaster.subscribe(_CAMERA)
            assert result.accepted is True
            viewer_ids.append(result.viewer_id)

        # Sanity: a single claim is open while the session has viewers, and the
        # session is live. No frame has been published yet (slot empty).
        assert broadcaster.viewer_count(_CAMERA) == num_viewers
        assert registry.open_count(_CAMERA) == 1

        session = broadcaster._sessions[_CAMERA]
        backend = created[_CAMERA]
        assert session.state == SessionState.RUNNING
        assert session.latest is None  # nothing published => active read fails (NO_FRAME)

        # Record the device-call counts so we can prove the inference read reused
        # the running claim: no new open(), no close() (the claim is retained).
        opens_before = backend.open_count
        closes_before = backend.close_count
        grabs_before = backend.grab_count
        assert opens_before == 1
        assert closes_before == 0

        # Advancing the clock varies the freshness context of the (empty) slot;
        # the inference read must behave the same — None, session intact.
        clock.advance(clock_advance)

        # 2 + 3. Drive the failed inference request(s). In ``read_error`` mode the
        #        session's slot read is scripted to raise for the duration of the
        #        inference calls (models a frame read that errors / camera
        #        unavailable for that request); the broadcaster's defensive branch
        #        must swallow it and return None without disturbing the session.
        for _ in range(num_requests):
            if failure_mode == "read_error":
                with mock.patch.object(
                    session, "read_latest", side_effect=RuntimeError("inference read failed")
                ):
                    frame = broadcaster.get_inference_frame(_CAMERA)
            else:  # "no_frame": empty slot => NO_FRAME => None
                frame = broadcaster.get_inference_frame(_CAMERA)

            # The caller gets the error / no-frame indication: None (Req 6.6).
            assert frame is None

    # --- assertions: the session is fully intact (Req 6.6) ---------------

    # The session still exists and is still RUNNING (never torn down / errored).
    assert _CAMERA in broadcaster._sessions
    assert broadcaster._sessions[_CAMERA] is session
    assert session.state == SessionState.RUNNING

    # The viewer set is unchanged.
    assert broadcaster.viewer_count(_CAMERA) == num_viewers
    assert set(session.viewers.keys()) == set(viewer_ids)

    # The single device claim is still held: no second claim opened, and the
    # claim was NOT released by the failed inference request.
    assert registry.open_count(_CAMERA) == 1
    assert registry.total_open() == 1
    assert backend.open_count == opens_before      # no new claim opened
    assert backend.close_count == closes_before    # claim retained (not released)

    # The active-session inference path reuses the running claim via a pure slot
    # read: it performs no device grab of its own.
    assert backend.grab_count == grabs_before

    # The single-claim invariant held throughout (no overlap, no release without
    # a matching open).
    registry.assert_single_claim()

    # Cleanup: drain viewers so the broadcaster stops the session and releases the
    # claim through its normal stop-on-last path, returning the claim count to 0.
    with mock.patch.object(broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker):
        for viewer_id in list(session.viewers.keys()):
            broadcaster.unsubscribe(_CAMERA, viewer_id)
    assert registry.open_count(_CAMERA) == 0
