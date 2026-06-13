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
"""Property-based test for the disconnection cascade.

Feature: concurrent-camera-stream-viewing, Property 14: Disconnection cascade —
for any session with any set of subscribed viewers, when a grab times out / fails
such that the camera is marked disconnected, the session is stopped with the
backend ``close()`` (claim release) invoked **before** stop completion is
reported, and every subscribed viewer's subsequent ``get_frame`` returns
``DISCONNECTED`` with no frame payload (never a previously cached frame).

Validates: Requirements 3.5, 3.11, 7.1, 7.2, 7.3, 7.4, 7.5.

How the cascade is exercised
----------------------------
A real :class:`StreamBroadcaster` is wired with an injected ``backend_factory``
(building the shared :class:`MockCameraBackend` registered against one shared
:class:`ClaimRegistry`) and an injected :class:`MockClock`, exactly like the
single-claim stateful test. For each example we:

1. Subscribe a generated number of viewers to a single camera (start-on-first
   opens the one device claim; the rest reuse it).
2. Optionally publish a frame into the session's latest-frame slot first, so
   there *is* a cached frame the cascade must refuse to serve.
3. Simulate the disconnect by setting ``session.state = SessionState.ERROR`` —
   exactly the transition the real acquisition worker performs on a failed /
   timed-out grab (``WorkerStopReason.DISCONNECTED`` / ``FIRST_FRAME_TIMEOUT``).
4. Call ``get_frame`` for one viewer to trigger the broadcaster's disconnection
   cascade.

We then assert:

* the cascade stopped the session and the backend ``close()`` (claim release)
  was invoked, releasing the single claim (``registry.open_count == 0``), and
  that the ``close()`` happened **before** the ``DISCONNECTED`` result was
  returned (the call log records ``close`` during the ``get_frame`` call);
* every subscribed viewer's subsequent ``get_frame`` returns
  ``FrameStatus.DISCONNECTED`` with ``frame is None`` — never the cached frame;
* the session was removed from the registry, so a later ``get_frame`` still
  returns ``DISCONNECTED``.

Test-only technique (no production code changed)
------------------------------------------------
Like ``test_broadcaster_claim.py``, this patches the ``AcquisitionWorker`` symbol
in the broadcaster module with a no-op stub for the duration of each example so
no real daemon thread / wall-clock grab loop runs. The stub marks the session
RUNNING on ``start()`` (mirroring the real worker after its first published
frame). The disconnect itself is modelled by setting ``SessionState.ERROR``
directly, which is the precise state the real worker leaves behind before the
broadcaster cascade tears the session down. Production code is untouched.
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
    make_raw_frame,
)
from utils.streaming import broadcaster as broadcaster_module
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import FrameStatus, SessionState, StreamConfig

# Keep max_viewers comfortably above the generated viewer count so the cascade —
# not the viewer-limit branch (Property 6) — is what we exercise.
_CONFIG = StreamConfig(max_viewers=8, stale_after_s=2.0)
_CAMERA = "Fake_disconnect_1"


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the broadcaster's construction/usage contract
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so the cascade runs deterministically. ``start()`` marks the session
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


# Feature: concurrent-camera-stream-viewing, Property 14: Disconnection cascade
# Validates: Requirements 3.5, 3.11, 7.1, 7.2, 7.3, 7.4, 7.5
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=150, deadline=None)
@given(
    num_viewers=st.integers(min_value=1, max_value=_CONFIG.max_viewers),
    publish_cached_frame=st.booleans(),
    cached_seq=st.integers(min_value=1, max_value=10_000),
    clock_advance=st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False),
    probe_offset=st.integers(min_value=0, max_value=_CONFIG.max_viewers - 1),
)
def test_property_14_disconnection_cascade(
    backend_cls, num_viewers, publish_cached_frame, cached_seq, clock_advance, probe_offset
):
    """A disconnect stops the session, releases the claim before reporting
    completion, and makes every viewer's get_frame return DISCONNECTED with no
    cached payload; the session is removed so later reads stay DISCONNECTED.

    Feature: concurrent-camera-stream-viewing, Property 14: Disconnection cascade
    Validates: Requirements 3.5, 3.11, 7.1, 7.2, 7.3, 7.4, 7.5
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
        # 1. Subscribe the generated number of viewers to the one camera.
        viewer_ids = []
        for _ in range(num_viewers):
            result = broadcaster.subscribe(_CAMERA)
            assert result.accepted is True
            viewer_ids.append(result.viewer_id)

        # Sanity: the single claim is open while the session has viewers.
        assert broadcaster.viewer_count(_CAMERA) == num_viewers
        assert registry.open_count(_CAMERA) == 1

        session = broadcaster._sessions[_CAMERA]
        backend = created[_CAMERA]

        # 2. Optionally publish a frame so a cached frame exists to prove it is
        #    NOT served once the camera is disconnected. A generated seq makes the
        #    cached frame byte-distinguishable across examples.
        cached_frame = None
        if publish_cached_frame:
            cached_frame = session.publish(make_raw_frame(seq=cached_seq), now=clock.time())
            # The slot is genuinely populated and would read OK pre-disconnect.
            assert session.latest is cached_frame

        # Advancing the clock varies how "old" the cached frame is (fresh vs
        # stale) at disconnect time; the cascade must report DISCONNECTED either
        # way, never OK/STALE with the cached payload.
        clock.advance(clock_advance)

        # 3. Simulate the disconnect: ERROR is exactly what the worker leaves on a
        #    failed/timed-out grab (Req 3.11, 7.2). Record the close-call count so
        #    we can prove close() happens DURING the cascade get_frame below.
        session.state = SessionState.ERROR
        closes_before = backend.close_count
        assert closes_before == 0  # claim still held; nothing released yet

        # 4. Trigger the cascade via a viewer's get_frame (any viewer works).
        probe_viewer = viewer_ids[probe_offset % num_viewers]
        result = broadcaster.get_frame(_CAMERA, probe_viewer)

    # --- assertions on the cascade ---------------------------------------

    # The triggering get_frame reports DISCONNECTED with no payload (Req 7.5).
    assert result.status == FrameStatus.DISCONNECTED
    assert result.frame is None

    # close() (claim release) was invoked as part of the cascade, BEFORE the
    # DISCONNECTED result was returned. Because get_frame is synchronous, the
    # close recorded during the call necessarily completed before the return
    # value we are now holding (Req 7.3, 7.4). The shared registry confirms the
    # single device claim was actually released.
    assert backend.close_count == closes_before + 1
    assert "close" in backend.call_log
    assert registry.open_count(_CAMERA) == 0
    assert registry.total_open() == 0

    # The session was stopped and removed from the registry (Req 3.5, 3.7).
    assert _CAMERA not in broadcaster._sessions
    assert broadcaster.viewer_count(_CAMERA) == 0
    assert session.state == SessionState.STOPPED

    # Every subscribed viewer's subsequent get_frame returns DISCONNECTED with no
    # frame — never the previously cached frame (Req 7.5, 3.11).
    for viewer_id in viewer_ids:
        res = broadcaster.get_frame(_CAMERA, viewer_id)
        assert res.status == FrameStatus.DISCONNECTED
        assert res.frame is None
        if cached_frame is not None:
            assert res.frame is not cached_frame

    # The single-claim invariant held throughout (no overlap, no release without
    # a matching open).
    registry.assert_single_claim()
