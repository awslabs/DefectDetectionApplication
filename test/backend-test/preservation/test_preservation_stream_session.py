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
"""Preservation baseline: concurrent-camera-stream session logic (Req 3.5).

Spec: python-3-11-security-upgrade — Property 2: Preservation — No functional
regression for non-3.9 artifacts.

The concurrent-camera-stream-viewing feature is **pure orchestration logic** with
no ``gi`` / device / interpreter-version dependency (``StreamSession`` /
``StreamBroadcaster`` use only ``threading`` / ``dataclasses`` / ``enum``). Moving
the DDA interpreter from 3.9 to 3.11 must not change any of its behavior. This
module captures that behavior as a baseline on the UNFIXED (3.9) tree and is
re-run unchanged after the fix (task 11) to confirm no regression.

It generates interleavings of subscription / heartbeat / clock-advance / stale-
sweep / unsubscribe events against the real :class:`StreamBroadcaster` (with a
mock backend and an injected clock) and asserts the feature's documented session
invariants hold throughout:

* the active viewer count never exceeds ``max_viewers`` and always equals the
  model's set size (Req 1.3, 1.6);
* a heartbeat at time ``t`` sets the viewer's ``last_active`` to ``t``, and the
  stale-sweep removes a viewer exactly when ``now - last_active >
  stale_timeout_s`` (strict ``>``; the exact boundary is NOT stale) (Req 3.9, 8.3);
* the single-device-claim invariant holds — at most one open claim per camera,
  never overlapping — and a session exists iff it has at least one viewer, so the
  last unsubscribe / sweep releases the claim (Req 1.2, 2.1, 3.3, 3.4, 8.7).

**Validates: Requirements 3.5**

Runs in a bare checkout (no native deps):

    PYTHONPATH=src/backend:test/backend-test/utils/streaming \\
        python3 -m pytest test/backend-test/preservation/test_preservation_stream_session.py \\
        --noconftest -v

No production code is modified — a test-only broadcaster subclass swaps the real
acquisition worker (which would spawn a producer thread) for an in-line stub, so
all of the production registry / claim / lifecycle logic still runs unchanged.
"""
import utils.streaming.broadcaster as broadcaster_module
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
)

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    MockClock,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import SessionState, StreamConfig

# Freshness ceiling large enough that the single published frame never reads
# STALE; this baseline is about the viewer/session registry, not frame freshness.
_NEVER_STALE_S = 1e9


# --------------------------------------------------------------------------- #
# Test-only worker stub + broadcaster subclass (no production code changes)
# --------------------------------------------------------------------------- #
class _FakeAcquisitionWorker:
    """In-line stand-in for ``AcquisitionWorker`` that spawns no thread.

    On :meth:`start` it synchronously publishes one frame and marks the session
    ``RUNNING`` (as the real worker does on its first frame), so the session is
    live without a real producer thread that would grab from the mock backend and
    self-terminate into ``ERROR`` on the default ``None`` grab.
    """

    def __init__(self, session, *, stream_config=None, time_fn=lambda: 0.0,
                 sleep_fn=None, stop_event=None):
        self._session = session
        self._time = time_fn

    def start(self):
        self._session.publish(make_raw_frame(seq=1), now=self._time())
        self._session.state = SessionState.RUNNING
        return None

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


class _NoThreadStreamBroadcaster(StreamBroadcaster):
    """``StreamBroadcaster`` whose sessions use the no-thread worker stub.

    Overrides only :meth:`_start_session` to swap the module-level
    ``AcquisitionWorker`` for :class:`_FakeAcquisitionWorker` around the inherited
    implementation, leaving all production registry / open / start_stream /
    rollback logic intact.
    """

    def _start_session(self, camera_id, config):
        real_worker_cls = broadcaster_module.AcquisitionWorker
        broadcaster_module.AcquisitionWorker = _FakeAcquisitionWorker
        try:
            return super()._start_session(camera_id, config)
        finally:
            broadcaster_module.AcquisitionWorker = real_worker_cls


# --------------------------------------------------------------------------- #
# Stateful machine: subscribe / heartbeat / advance / sweep / unsubscribe
# --------------------------------------------------------------------------- #
# A small, fixed set of cameras so multi-viewer + multi-camera interleavings are
# exercised while keeping the boundary (max_viewers) reachable quickly.
_CAMERAS = ("Cam_A", "Cam_B", "Cam_C")
_MAX_VIEWERS = 3
_STALE_TIMEOUT_S = 30  # StreamConfig default; integer so boundaries stay exact.


class StreamSessionPreservationMachine(RuleBasedStateMachine):
    """Drive the broadcaster and assert the concurrent-stream session invariants.

    Spec: python-3-11-security-upgrade — Property 2: Preservation.
    Validates: Requirements 3.5
    """

    viewers = Bundle("viewers")

    def __init__(self):
        super().__init__()
        self.clock = MockClock(start=1000.0)
        self.registry = ClaimRegistry()
        self.config = StreamConfig(
            max_viewers=_MAX_VIEWERS,
            stale_timeout_s=_STALE_TIMEOUT_S,
            stale_after_s=_NEVER_STALE_S,
        )

        backend_classes = list(MOCK_BACKEND_CLASSES.values())

        def backend_factory(camera_id, config, _classes=backend_classes,
                            _clock=self.clock, _reg=self.registry):
            # Deterministically pick a backend family from the camera id so both
            # the Aravis and GStreamer adapters are exercised across cameras.
            cls = _classes[hash(camera_id) % len(_classes)]
            return cls(camera_id, clock=_clock, claim_registry=_reg)

        self.broadcaster = _NoThreadStreamBroadcaster(
            stream_config=self.config,
            backend_factory=backend_factory,
            time_fn=self.clock,
        )
        # Model: viewer_id -> {"camera": camera_id, "last_active": float}.
        self.model: dict[str, dict] = {}

    # --- helpers ----------------------------------------------------------

    def _count(self, camera_id: str) -> int:
        return sum(1 for v in self.model.values() if v["camera"] == camera_id)

    # --- rules ------------------------------------------------------------

    @rule(target=viewers, camera=st.sampled_from(_CAMERAS))
    def subscribe(self, camera):
        """Subscribe a viewer; accept below the cap, reject at the cap (Req 1.6)."""
        at_limit = self._count(camera) >= _MAX_VIEWERS
        result = self.broadcaster.subscribe(camera)

        if at_limit:
            assert result.accepted is False
            assert result.reason == "viewer_limit"
            assert result.viewer_id is None
            assert result.viewer_count == _MAX_VIEWERS
            return multiple()

        assert result.accepted is True, f"unexpected rejection: {result!r}"
        assert result.viewer_id is not None
        # On subscribe the broadcaster stamps last_active = now (current clock).
        self.model[result.viewer_id] = {
            "camera": camera,
            "last_active": self.clock.time(),
        }
        assert result.viewer_count == self._count(camera)
        return result.viewer_id

    @precondition(lambda self: len(self.model) > 0)
    @rule(viewer=consumes(viewers))
    def unsubscribe(self, viewer):
        """Remove a viewer; the per-camera count shrinks by one (Req 8.7)."""
        info = self.model.pop(viewer, None)
        if info is None:
            return
        self.broadcaster.unsubscribe(info["camera"], viewer)
        assert self.broadcaster.viewer_count(info["camera"]) == self._count(info["camera"])

    @precondition(lambda self: len(self.model) > 0)
    @rule(viewer=viewers)
    def heartbeat(self, viewer):
        """A heartbeat sets the viewer's last_active to now (Req 3.9, 8.3)."""
        info = self.model.get(viewer)
        if info is None:
            return
        ok = self.broadcaster.heartbeat(info["camera"], viewer)
        assert ok is True, "heartbeat for a live viewer must succeed"
        info["last_active"] = self.clock.time()

    @rule(seconds=st.integers(min_value=0, max_value=45))
    def advance_clock(self, seconds):
        """Advance the injected clock (no behavioral change, just time passing)."""
        self.clock.advance(float(seconds))

    @rule()
    def sweep(self):
        """Sweep stale viewers; exactly the inactive-beyond-window ones go (Req 8.6)."""
        now = self.clock.time()
        expected_removed = {
            vid
            for vid, info in self.model.items()
            if (now - info["last_active"]) > _STALE_TIMEOUT_S
        }

        self.broadcaster.sweep_stale_viewers(now)

        for vid in expected_removed:
            self.model.pop(vid, None)

        # Survivors (exactly at the boundary or fresher) are retained.
        for camera in _CAMERAS:
            assert self.broadcaster.viewer_count(camera) == self._count(camera)

    # --- invariants -------------------------------------------------------

    @invariant()
    def count_matches_and_within_limit(self):
        """Reported count == model and never exceeds max_viewers (Req 1.6)."""
        for camera in _CAMERAS:
            count = self.broadcaster.viewer_count(camera)
            assert count == self._count(camera)
            assert count <= _MAX_VIEWERS

    @invariant()
    def single_claim_invariant(self):
        """At most one open device claim per camera, never overlapping (Req 2.1)."""
        self.registry.assert_single_claim()

    @invariant()
    def session_exists_iff_has_viewers(self):
        """A session/claim exists exactly while a camera has >= 1 viewer (Req 3.4)."""
        for camera in _CAMERAS:
            has_viewers = self._count(camera) > 0
            claim_open = self.registry.open_count(camera) > 0
            assert claim_open == has_viewers, (
                f"{camera}: claim_open={claim_open} but has_viewers={has_viewers}"
            )


StreamSessionPreservationMachine.TestCase.settings = settings(
    max_examples=25,
    stateful_step_count=30,
    deadline=None,
)

# pytest collects this TestCase subclass and runs the stateful machine.
TestStreamSessionPreservation = StreamSessionPreservationMachine.TestCase
