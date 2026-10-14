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
"""Property-based test for inference claim reuse with dedicated-claim fallback.

Feature: concurrent-camera-stream-viewing, Property 16: Inference reuses the
session claim, falls back when none — for any camera, a frame request from the
inference/capture path returns a frame WITHOUT opening a second claim when a
session is active (the open-claim count stays exactly 1), and when no session
exists it opens exactly one dedicated claim, returns a frame, and releases it
(the open-claim count returns to 0).

Validates: Requirements 6.1, 6.3, 6.4.

How the property is exercised
-----------------------------
:meth:`StreamBroadcaster.get_inference_frame` is driven against an injected
``backend_factory`` that builds the hardware-free :class:`MockCameraBackend`
registered against one shared :class:`ClaimRegistry` (so the test can read the
true per-camera open-claim count) plus an injected :class:`MockClock` (so frame
freshness is deterministic). Every backend the factory builds is captured so the
test can assert exactly how many backends were created and how many ``open()`` /
``close()`` calls each saw — that is how "no second claim was opened" is proven.

Hypothesis generates two independent scenarios per example (camera id + frame
geometry):

* **Active session** — subscribe first (the single claim is opened), publish a
  frame into the live session, then call ``get_inference_frame``. The call must
  return a ``{"data", "height", "width"}`` dict, the open-claim count must stay
  exactly 1, and **no** second backend / second ``open()`` may occur (the running
  claim is reused).
* **No session** — call ``get_inference_frame`` on a camera with no session. The
  call must open exactly one dedicated claim, return a frame dict, and release the
  claim so the registry's open count returns to 0 (with the peak concurrency for
  that camera being exactly 1).

Worker-stub technique (test-only; mirrors ``test_broadcaster_claim.py``)
------------------------------------------------------------------------
In production a subscribe starts a real :class:`AcquisitionWorker` on a daemon
thread (a ``grab -> publish`` loop). That thread is irrelevant to the claim-reuse
property and would make the active-session frame non-deterministic. For the
lifetime of each example the ``AcquisitionWorker`` symbol in the broadcaster
module is patched with a no-op stub that simply marks the session RUNNING on
``start()`` (mirroring the real worker after its first published frame). The
active-session frame is then made deterministically available by publishing it
through the session directly before reading. The broadcaster's
subscribe / get_inference_frame logic — and therefore every ``open()`` /
``close()`` that moves the device claim — runs completely unchanged. No
production code is modified.
"""
from __future__ import annotations

import unittest.mock as mock
from typing import List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import utils.streaming.broadcaster as broadcaster_module
from mock_camera_backend import (
    BACKEND_KINDS,
    ClaimRegistry,
    MockCameraBackend,
    MockClock,
    make_backend,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import SessionState, StreamConfig

# A small fixed set of physical cameras the property quantifies over.
CAMERAS = ("cam_a", "cam_b", "cam_c")

_STREAM_CONFIG = StreamConfig(max_viewers=4, stale_timeout_s=30)

# Hypothesis runs >= 100 iterations; the work per example is tiny and synchronous.
_PROP_SETTINGS = settings(max_examples=25, deadline=None)


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the construction/usage contract the broadcaster relies on
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so the open-claim count is deterministic. ``start()`` marks the session
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


def _make_broadcaster(backend_kind, registry, clock, created):
    """Build a broadcaster whose factory records every backend it creates.

    Each backend shares the one ``registry`` + ``clock`` so the test reads the true
    per-camera open-claim count, and is pre-loaded with a single scripted frame so
    the no-session dedicated-claim path has a frame to grab. The ``created`` list
    captures every backend instance so the test can assert exactly how many
    backends / ``open()`` calls occurred.
    """

    def backend_factory(camera_id, config) -> MockCameraBackend:
        backend = make_backend(
            backend_kind,
            camera_id,
            claim_registry=registry,
            clock=clock,
            # A scripted frame so the dedicated-claim fallback's single grab
            # returns a frame (the active-session path publishes its own frame and
            # never grabs from this queue).
            frames=[make_raw_frame(seq=1, width=8, height=8)],
        )
        created.append(backend)
        return backend

    return StreamBroadcaster(
        stream_config=_STREAM_CONFIG,
        backend_factory=backend_factory,
        time_fn=clock.time,
    )


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
@given(
    camera=st.sampled_from(CAMERAS),
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
    seq=st.integers(min_value=1, max_value=1000),
)
@_PROP_SETTINGS
def test_inference_reuses_active_session_claim(backend_kind, camera, width, height, seq):
    """Active session: inference reuses the claim, opening no second claim (Req 6.1, 6.4).

    Feature: concurrent-camera-stream-viewing, Property 16: Inference reuses the
    session claim, falls back when none.
    Validates: Requirements 6.1, 6.4.
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1_000.0)
    created: List[MockCameraBackend] = []

    with mock.patch.object(
        broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker
    ):
        broadcaster = _make_broadcaster(backend_kind, registry, clock, created)

        # First viewer opens the single device claim (start-on-first-viewer).
        result = broadcaster.subscribe(camera)
        assert result.accepted
        assert registry.open_count(camera) == 1
        assert len(created) == 1, "subscribe should build exactly one backend"
        session_backend = created[0]
        assert session_backend.open_count == 1

        # Publish a frame into the live session so the reused-claim read has a
        # deterministically-available, fresh frame (no real worker thread runs).
        session = broadcaster._sessions[camera]
        session.publish(
            make_raw_frame(seq=seq, width=width, height=height), now=clock.time()
        )

        # Snapshot interaction counts immediately before the inference read.
        opens_before = session_backend.open_count
        backends_before = len(created)

        frame = broadcaster.get_inference_frame(camera)

        # A frame dict in the legacy shape is returned from the reused claim.
        assert isinstance(frame, dict)
        assert set(frame.keys()) == {"data", "height", "width"}
        assert frame["width"] == width
        assert frame["height"] == height

        # The running claim was reused: no second backend, no second open(), and
        # the open-claim count stayed exactly 1 (never overlapped).
        assert len(created) == backends_before, "no second backend may be built"
        assert session_backend.open_count == opens_before, "no second open() may occur"
        assert registry.open_count(camera) == 1
        assert registry.max_concurrent.get(camera) == 1
        assert not registry.overlap_detected

        # The session is left intact (still RUNNING, claim retained, viewer kept).
        assert broadcaster._sessions.get(camera) is session
        assert session.state == SessionState.RUNNING
        assert broadcaster.viewer_count(camera) == 1

        # Cleanup: drop the viewer so the claim is released through the normal path.
        broadcaster.unsubscribe(camera, result.viewer_id)
        assert registry.open_count(camera) == 0


@pytest.mark.parametrize("backend_kind", BACKEND_KINDS)
@given(
    camera=st.sampled_from(CAMERAS),
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
)
@_PROP_SETTINGS
def test_inference_falls_back_to_dedicated_claim_when_no_session(
    backend_kind, camera, width, height
):
    """No session: inference opens exactly one dedicated claim and releases it (Req 6.3).

    Feature: concurrent-camera-stream-viewing, Property 16: Inference reuses the
    session claim, falls back when none.
    Validates: Requirements 6.3.
    """
    registry = ClaimRegistry()
    clock = MockClock(start=2_000.0)
    created: List[MockCameraBackend] = []

    with mock.patch.object(
        broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker
    ):
        broadcaster = _make_broadcaster(backend_kind, registry, clock, created)

        # Precondition: no session and no open claim for this camera.
        assert broadcaster._sessions.get(camera) is None
        assert registry.open_count(camera) == 0

        frame = broadcaster.get_inference_frame(camera, config={"width": width})

        # A frame dict in the legacy shape is returned from the dedicated grab.
        assert isinstance(frame, dict)
        assert set(frame.keys()) == {"data", "height", "width"}

        # Exactly one dedicated backend was built, opened once, grabbed, and closed.
        assert len(created) == 1, "exactly one dedicated backend should be built"
        dedicated = created[0]
        assert dedicated.open_count == 1, "exactly one dedicated claim opened"
        assert dedicated.close_count == 1, "the dedicated claim must be released"
        assert dedicated.grab_count == 1

        # The claim was released: open count returns to 0, peak concurrency was 1,
        # and no overlapping claim was ever observed.
        assert registry.open_count(camera) == 0
        assert registry.max_concurrent.get(camera) == 1
        assert not registry.overlap_detected
        assert not registry.released_without_open

        # No lingering session was created by the fallback.
        assert broadcaster._sessions.get(camera) is None
        assert broadcaster.viewer_count(camera) == 0
