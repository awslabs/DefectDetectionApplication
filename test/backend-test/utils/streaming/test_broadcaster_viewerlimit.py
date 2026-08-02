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
"""Stateful property-based test for viewer-limit enforcement.

Feature: concurrent-camera-stream-viewing, Property 6: Viewer-limit enforcement
— for any interleaving of subscribe / unsubscribe against a single camera with a
small ``max_viewers``, a subscribe issued while the camera is already at
``max_viewers`` active viewers is rejected with
``SubscribeResult(accepted=False, reason="viewer_limit")`` and does NOT change
the viewer set; the active viewer count never exceeds ``max_viewers``; and every
previously-accepted viewer remains registered and can still read frames
(``get_frame`` never errors and never reports ``DISCONNECTED`` for them)
(Req 1.3, 1.6).

Driven by a Hypothesis ``RuleBasedStateMachine`` (min 100 iterations) over the
two operations that move the viewer set:

* ``subscribe(camera)`` — attempt to add a viewer.
* ``unsubscribe(viewer)`` — remove a previously-accepted viewer.

After every step a set of invariants is checked: the broadcaster's reported
viewer count equals the model's set size, never exceeds ``max_viewers``, and all
modelled viewers read a frame successfully.

Worker threads are stubbed: a test-only :class:`_NoThreadStreamBroadcaster`
subclass replaces the real :class:`AcquisitionWorker` (which would spawn a
producer thread grabbing from the backend and, on the default ``None`` grab,
self-terminate the session into ``ERROR``) with an in-line
:class:`_FakeAcquisitionWorker` that synchronously publishes one frame and marks
the session ``RUNNING``. This keeps the test deterministic and focused on the
broadcaster's registry / viewer-limit logic. **No production code is modified** —
the subclass only swaps the worker class around the inherited ``_start_session``
call.
"""
import utils.streaming.broadcaster as broadcaster_module
from hypothesis import settings
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
from hypothesis import strategies as st

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    MockClock,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import FrameStatus, SessionState, StreamConfig

CAMERA_ID = "Fake_1"
# Freshness ceiling large enough that the single published frame never reads
# STALE regardless of the (unadvanced) virtual clock — keeps the "existing
# viewers can read frames" invariant about registration, not freshness.
_NEVER_STALE_S = 1e9


# --------------------------------------------------------------------------- #
# Test-only worker stub + broadcaster subclass (no production code changes)
# --------------------------------------------------------------------------- #
class _FakeAcquisitionWorker:
    """In-line stand-in for ``AcquisitionWorker`` that spawns no thread.

    Mimics just enough of the real worker for the broadcaster's start-on-first
    path: on :meth:`start` it synchronously publishes a single frame into the
    session's latest-frame slot and transitions the session to ``RUNNING`` (as
    the real worker does on its first frame), so existing viewers' ``get_frame``
    returns ``OK`` rather than ``NO_FRAME`` — without a real producer thread that
    would grab from the mock backend and disconnect on the default ``None`` grab.
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
    implementation, so all of the production registry / open / start_stream /
    rollback logic still runs unchanged — only the producer thread is stubbed.
    """

    def _start_session(self, camera_id, config):
        real_worker_cls = broadcaster_module.AcquisitionWorker
        broadcaster_module.AcquisitionWorker = _FakeAcquisitionWorker
        try:
            return super()._start_session(camera_id, config)
        finally:
            broadcaster_module.AcquisitionWorker = real_worker_cls


# --------------------------------------------------------------------------- #
# The stateful machine
# --------------------------------------------------------------------------- #
class ViewerLimitMachine(RuleBasedStateMachine):
    """Subscribe/unsubscribe a single camera and assert the viewer-limit contract.

    Feature: concurrent-camera-stream-viewing, Property 6: Viewer-limit enforcement
    Validates: Requirements 1.3, 1.6
    """

    viewers = Bundle("viewers")

    def __init__(self):
        super().__init__()
        self.broadcaster = None
        self.max_viewers = None
        # Model of currently-registered viewer ids (mirrors the broadcaster's set).
        self.model_viewers: set[str] = set()

    @initialize(
        n=st.integers(min_value=1, max_value=4),
        backend_cls=st.sampled_from(list(MOCK_BACKEND_CLASSES.values())),
    )
    def setup(self, n, backend_cls):
        """Build a broadcaster with a small ``max_viewers`` to hit the boundary fast."""
        self.max_viewers = n
        clock = MockClock(start=1000.0)
        registry = ClaimRegistry()

        def backend_factory(camera_id, config, _cls=backend_cls, _clock=clock, _reg=registry):
            return _cls(camera_id, clock=_clock, claim_registry=_reg)

        self.broadcaster = _NoThreadStreamBroadcaster(
            stream_config=StreamConfig(max_viewers=n, stale_after_s=_NEVER_STALE_S),
            backend_factory=backend_factory,
            time_fn=clock,
        )

    # --- rules ------------------------------------------------------------

    @rule(target=viewers)
    def subscribe(self):
        """Attempt a subscribe; assert accept-below-limit vs reject-at-limit."""
        at_limit = len(self.model_viewers) >= self.max_viewers
        result = self.broadcaster.subscribe(CAMERA_ID)

        if at_limit:
            # Property 6: past max_viewers, reject with reason "viewer_limit" and
            # leave the viewer set untouched (Req 1.3, 1.6).
            assert result.accepted is False
            assert result.reason == "viewer_limit"
            assert result.viewer_id is None
            assert result.viewer_count == self.max_viewers
            assert self.broadcaster.viewer_count(CAMERA_ID) == len(self.model_viewers)
            return multiple()  # nothing added to the bundle

        # Below the limit: accept, register, and report the new count.
        assert result.accepted is True, f"unexpected rejection: {result!r}"
        assert result.reason is None
        assert result.viewer_id is not None
        self.model_viewers.add(result.viewer_id)
        assert result.viewer_count == len(self.model_viewers)
        return result.viewer_id

    @precondition(lambda self: len(self.model_viewers) > 0)
    @rule(viewer=consumes(viewers))
    def unsubscribe(self, viewer):
        """Remove a previously-accepted viewer; the set must shrink by one."""
        if viewer not in self.model_viewers:
            # The bundle may retain ids already removed by a prior step; skip them.
            return
        self.broadcaster.unsubscribe(CAMERA_ID, viewer)
        self.model_viewers.discard(viewer)
        assert self.broadcaster.viewer_count(CAMERA_ID) == len(self.model_viewers)

    # --- invariants -------------------------------------------------------

    @invariant()
    def count_matches_model_and_within_limit(self):
        """Reported count equals the model and never exceeds max_viewers."""
        if self.broadcaster is None:
            return
        count = self.broadcaster.viewer_count(CAMERA_ID)
        assert count == len(self.model_viewers)
        assert count <= self.max_viewers

    @invariant()
    def existing_viewers_can_read(self):
        """Every registered viewer still reads a frame (never errors/DISCONNECTED)."""
        if self.broadcaster is None:
            return
        for viewer_id in self.model_viewers:
            result = self.broadcaster.get_frame(CAMERA_ID, viewer_id)
            assert result.status != FrameStatus.DISCONNECTED, (
                f"existing viewer {viewer_id} unexpectedly DISCONNECTED"
            )
            # The stubbed worker published a fresh frame on session start, so a
            # registered viewer reads OK (proves it remains a live viewer).
            assert result.status == FrameStatus.OK, (
                f"existing viewer {viewer_id} read {result.status} (expected OK)"
            )
            assert result.frame is not None


# Min 100 iterations (examples), with enough steps per example to fill past the
# small max_viewers boundary and exercise re-subscribe after unsubscribe.
ViewerLimitMachine.TestCase.settings = settings(
    max_examples=25,
    stateful_step_count=24,
    deadline=None,
)

# pytest collects this TestCase subclass and runs the stateful machine.
TestViewerLimitEnforcement = ViewerLimitMachine.TestCase
