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
"""Stateful property-based test for the single device-claim invariant.

Feature: concurrent-camera-stream-viewing, Property 1: Single device-claim
invariant — for any interleaving of subscribe / unsubscribe / stale-sweep /
disconnect operations across one or more cameras, the number of open
``Device_Claim``s for a camera is:

* exactly **1** while that camera has at least one active viewer (a running
  session),
* **0** when it has none,
* **never greater than 1**, and
* never *overlapping* — a new claim is never opened for a camera before the
  prior claim for that same camera has been released.

Validates: Requirements 1.2, 2.1, 3.4, 7.7.

How the machine exercises the broadcaster
-----------------------------------------
A :class:`hypothesis.stateful.RuleBasedStateMachine` drives a real
:class:`StreamBroadcaster` over a randomized sequence of rules:

* ``subscribe(camera)`` — register a viewer (start-on-first-viewer opens the
  single claim).
* ``unsubscribe(viewer)`` — drop a previously-issued viewer (stop-on-last-viewer
  releases the claim).
* ``advance_clock`` — move the injected clock forward so stale-sweep windows can
  elapse deterministically.
* ``sweep_stale_viewers`` — run the broadcaster's stale-viewer sweep at the
  current clock time (emptied sessions stop and release their claim).
* ``disconnect(camera)`` — simulate a device drop by marking the session ERROR
  (exactly what the acquisition worker does on a failed/timed-out grab) and then
  calling ``get_frame`` to trigger the disconnection cascade, which stops the
  session and releases the claim before reporting DISCONNECTED.

The broadcaster is wired with an injected ``backend_factory`` that builds the
shared :class:`MockCameraBackend` registered against one shared
:class:`ClaimRegistry`, so the machine can read the true open-claim count per
camera, and an injected :class:`MockClock` so the stale-sweep is deterministic.

Test-only technique (no production code changed)
------------------------------------------------
In production the broadcaster starts a real :class:`AcquisitionWorker` on a
daemon thread when a session starts (``grab -> publish`` loop). That thread is
irrelevant to the claim invariant and would make per-rule claim counting racy
(and would burn real wall-clock time on its rate-ceiling sleeps). For
determinism this test patches the ``AcquisitionWorker`` symbol *in the
broadcaster module* with a no-op stub for the duration of each machine run (see
:class:`_StubAcquisitionWorker`). This is a pure test seam: the broadcaster's
subscribe / unsubscribe / sweep / disconnect-cascade logic — and therefore every
``open()`` / ``close()`` that moves the device claim — runs completely unchanged.
The stub marks the session RUNNING on start (mirroring the worker after its
first frame) so claim/viewer assertions reflect a live session. Disconnects are
modelled by setting ``SessionState.ERROR`` directly, which is exactly the state
transition the real worker performs on a disconnect before the broadcaster
cascade tears the session down.
"""
from __future__ import annotations

import unittest.mock as mock
from typing import Optional

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    multiple,
    rule,
)

import utils.streaming.broadcaster as broadcaster_module
from mock_camera_backend import ClaimRegistry, MockClock, make_backend
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import SessionState, StreamConfig

# The small fixed set of physical cameras the machine quantifies over. Using
# more than one camera exercises that the single-claim invariant holds
# *per camera* and that operations on one camera never disturb another.
CAMERAS = ("cam_a", "cam_b", "cam_c")

# Keep max_viewers small so the viewer-limit branch is reached within a run
# (the limit path must also preserve the single-claim invariant). stale window
# kept modest so advance_clock can cross it.
_STREAM_CONFIG = StreamConfig(max_viewers=4, stale_timeout_s=30)


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the construction/usage contract the broadcaster relies on
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so claim counting is deterministic. ``start()`` marks the session
    RUNNING to mirror the real worker after its first published frame.
    """

    def __init__(self, session, *, stream_config=None, time_fn=None, **kwargs) -> None:
        self.session = session

    def start(self):
        # Mirror the real worker transitioning the session to RUNNING once the
        # first frame is published; no device interaction occurs in the stub.
        self.session.state = SessionState.RUNNING
        return None

    def stop(self) -> None:
        return None

    def join(self, timeout: Optional[float] = None) -> None:
        return None


class SingleClaimMachine(RuleBasedStateMachine):
    """Drives a real broadcaster and asserts the single-claim invariant.

    Concrete per-backend subclasses set :attr:`backend_kind` so the property is
    checked for both the mock Aravis and mock GStreamer backends (Req 2.8 parity).
    """

    #: Overridden by the concrete subclasses below ("aravis" | "gstreamer").
    backend_kind = "aravis"

    # Bundle of issued viewers: each entry is a (camera_id, viewer_id) pair that
    # the broadcaster accepted. Entries may become stale (their session was
    # swept or disconnected); unsubscribing a stale viewer is a harmless no-op.
    viewers = Bundle("viewers")

    def __init__(self) -> None:
        super().__init__()
        # Patch the worker symbol the broadcaster module resolves at session
        # start, for the lifetime of this machine run only.
        self._worker_patch = mock.patch.object(
            broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker
        )
        self._worker_patch.start()

        self.clock = MockClock(start=1_000.0)
        self.registry = ClaimRegistry()

        def backend_factory(camera_id, config):
            # Every backend shares the one registry + clock so the machine can
            # read the true per-camera open-claim count and stale-sweep is
            # deterministic. Backends open successfully (open-failure handling is
            # Property 7, out of scope here).
            return make_backend(
                self.backend_kind,
                camera_id,
                claim_registry=self.registry,
                clock=self.clock,
            )

        self.broadcaster = StreamBroadcaster(
            stream_config=_STREAM_CONFIG,
            backend_factory=backend_factory,
            time_fn=self.clock.time,
        )

    # --- rules ------------------------------------------------------------

    @rule(target=viewers, camera=st.sampled_from(CAMERAS))
    def subscribe(self, camera):
        """Subscribe a viewer; record accepted viewers, ignore limit rejections."""
        result = self.broadcaster.subscribe(camera)
        if result.accepted:
            return (camera, result.viewer_id)
        # Rejected (viewer_limit at the cap): nothing enters the bundle. The
        # rejection must not have created/opened a claim — checked by @invariant.
        return multiple()

    @rule(entry=consumes(viewers))
    def unsubscribe(self, entry):
        """Unsubscribe a previously-issued viewer (stop-on-last releases claim)."""
        camera, viewer_id = entry
        self.broadcaster.unsubscribe(camera, viewer_id)

    @rule(seconds=st.floats(min_value=0.0, max_value=60.0, allow_nan=False, allow_infinity=False))
    def advance_clock(self, seconds):
        """Advance the injected clock so stale windows can elapse deterministically."""
        self.clock.advance(seconds)

    @rule()
    def sweep_stale_viewers(self):
        """Sweep stale viewers at the current clock time; emptied sessions stop."""
        self.broadcaster.sweep_stale_viewers(self.clock.time())

    @rule(camera=st.sampled_from(CAMERAS))
    def disconnect(self, camera):
        """Simulate a device disconnect and trigger the broadcaster cascade.

        Mark the (running) session ERROR — exactly the transition the real
        acquisition worker performs on a failed/timed-out grab — then call
        ``get_frame`` so the broadcaster cascade stops the session and releases
        the single claim before returning DISCONNECTED.
        """
        session = self.broadcaster._sessions.get(camera)
        if session is None:
            return
        session.state = SessionState.ERROR
        # Any viewer id works: the ERROR/DISCONNECTED branch is evaluated before
        # the per-viewer heartbeat lookup.
        self.broadcaster.get_frame(camera, "disconnect-probe")

    # --- invariant --------------------------------------------------------

    @invariant()
    def single_claim_invariant(self):
        """After every rule: per-camera claim is 0/1, ==1 iff a viewer is active,
        never > 1, and no overlapping claims were ever observed.
        """
        for camera in CAMERAS:
            claim_count = self.registry.open_count(camera)
            viewer_count = self.broadcaster.viewer_count(camera)

            # Never more than one open claim per camera.
            assert claim_count <= 1, (
                f"camera {camera}: {claim_count} concurrent Device_Claims (> 1)"
            )
            assert claim_count in (0, 1), (
                f"camera {camera}: unexpected claim count {claim_count}"
            )

            # Exactly one claim iff the camera currently has >= 1 active viewer
            # (i.e. a live session); zero claims when it has none.
            if viewer_count >= 1:
                assert claim_count == 1, (
                    f"camera {camera}: {viewer_count} viewer(s) but {claim_count} claim(s) "
                    f"(expected exactly 1 open claim for an active camera)"
                )
            else:
                assert claim_count == 0, (
                    f"camera {camera}: no viewers but {claim_count} claim(s) "
                    f"(expected the claim to be released)"
                )

        # Global, history-aware checks: a second claim was never acquired while
        # one was still open (no overlap), and no claim was released without a
        # matching open. This is the "no overlapping claims" guarantee (Req 7.7).
        assert not self.registry.overlap_detected, (
            f"overlapping Device_Claim observed at some point: {self.registry.max_concurrent}"
        )
        assert not self.registry.released_without_open, (
            "a claim was released without a matching open"
        )
        assert self.registry.single_claim_ok
        assert all(
            peak <= 1 for peak in self.registry.max_concurrent.values()
        ), f"a camera reached > 1 concurrent claims at some point: {self.registry.max_concurrent}"

    def teardown(self) -> None:
        """Release any remaining sessions and remove the worker patch."""
        try:
            for camera in CAMERAS:
                session = self.broadcaster._sessions.get(camera)
                if session is not None:
                    # Drain viewers so the broadcaster stops the session and
                    # releases the claim through its normal stop-on-last path.
                    for viewer_id in list(session.viewers.keys()):
                        self.broadcaster.unsubscribe(camera, viewer_id)
        finally:
            self._worker_patch.stop()


class SingleClaimMachineAravis(SingleClaimMachine):
    """Single-claim invariant over the mock Aravis backend (Req 2.8 parity)."""

    backend_kind = "aravis"


class SingleClaimMachineGStreamer(SingleClaimMachine):
    """Single-claim invariant over the mock GStreamer backend (Req 2.8 parity)."""

    backend_kind = "gstreamer"


# Feature: concurrent-camera-stream-viewing, Property 1: Single device-claim invariant
# Validates: Requirements 1.2, 2.1, 3.4, 7.7
_MACHINE_SETTINGS = settings(max_examples=25, stateful_step_count=40, deadline=None)

TestSingleClaimInvariantAravis = SingleClaimMachineAravis.TestCase
TestSingleClaimInvariantAravis.settings = _MACHINE_SETTINGS

TestSingleClaimInvariantGStreamer = SingleClaimMachineGStreamer.TestCase
TestSingleClaimInvariantGStreamer.settings = _MACHINE_SETTINGS
