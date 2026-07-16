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
"""Stateful property-based test for stream-session lifecycle.

Feature: concurrent-camera-stream-viewing, Property 2: Session lifecycle (start
on first, stop and release on last) — the first viewer to subscribe to a camera
that has no session creates *exactly one* session (one backend ``open()`` /
device claim), and whenever a camera's active viewer count returns to 0 the
session is stopped and the backend ``close()`` (claim release) is invoked. A
brand-new subscribe after a teardown starts a *fresh* session/claim rather than
resurrecting the closed one (Req 3.1, 3.3, 3.7, 8.7).

The property is exercised as a Hypothesis ``RuleBasedStateMachine`` over a real
:class:`StreamBroadcaster` whose ``backend_factory`` builds the hardware-free
:class:`MockCameraBackend` and whose clock is a deterministic :class:`MockClock`.
The machine interleaves three kinds of rules:

* ``subscribe(camera)`` — register a new viewer (start-on-first-viewer),
* ``unsubscribe(viewer)`` — remove a known viewer (stop-on-last-viewer), and
* ``advance_and_sweep()`` — push the clock past the stale window and run
  ``sweep_stale_viewers`` so sessions can also be emptied (and torn down) via
  staleness, not only explicit unsubscribes.

After every rule the machine re-checks the full lifecycle invariant against a
shadow model of the expected viewers per camera (see :meth:`_check_invariant`).

Test-only worker stub
---------------------
In production :meth:`StreamBroadcaster._start_session` starts a real
``AcquisitionWorker`` on a daemon thread (the grab→publish producer). That
background thread is irrelevant to the *lifecycle* invariant (it never opens or
closes the device claim — only the broadcaster does) but it would introduce
non-determinism into the assertions and could transition sessions to ``ERROR``
concurrently. To keep the state machine deterministic we patch the
``AcquisitionWorker`` symbol *in the broadcaster module* with a no-op
:class:`_StubWorker` for the duration of each run. This is a test-only seam: no
production code is modified, and the broadcaster's own start/stop/close
orchestration (the code under test) runs exactly as in production.
"""
from __future__ import annotations

from unittest import mock

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
)

from mock_camera_backend import ClaimRegistry, MockClock, make_raw_frame
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import StreamConfig

# A small fixed pool of cameras so the machine repeatedly drives the same camera
# through full start→stop→restart cycles (rather than always touching a fresh id).
CAMERAS = ("Fake_1", "Fake_2", "Fake_3")

# Stale window used by the model; advancing the clock past it makes every
# currently-subscribed viewer eligible for the sweep. A large max_viewers keeps
# this test focused purely on lifecycle (viewer-limit rejection is Property 6,
# task 5.7), so every subscribe is accepted and drives real lifecycle.
STALE_TIMEOUT_S = 30
MAX_VIEWERS = 1000


def _build_mock_backend(camera_id, claims, clock):
    """Build a fresh mock backend wired to the shared claim registry and clock.

    Each new session gets a brand-new backend instance so the test can assert
    that every start-on-first-viewer opens a *fresh* claim (not a resurrected
    one) and that each torn-down session's backend had ``close()`` invoked.
    """
    from mock_camera_backend import MockAravisBackend

    # default_grab keeps the backend representative of a healthy camera; the
    # stub worker never calls grab(), so no frames are actually consumed.
    return MockAravisBackend(
        camera_id,
        claim_registry=claims,
        clock=clock,
        default_grab=make_raw_frame(seq=0),
    )


class _StubWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Implements just the surface the broadcaster touches — construction plus
    ``start`` / ``stop`` / ``join`` — without spawning any thread or touching the
    backend, so the broadcaster's session start/stop/close orchestration runs
    deterministically and the lifecycle assertions are race-free.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self):
        return None

    def stop(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        return None


class SessionLifecycleMachine(RuleBasedStateMachine):
    """Drives subscribe / unsubscribe / sweep against a real broadcaster.

    Maintains a shadow model:

    * ``model[camera]`` — set of viewer ids the *test* believes are active, with
      each viewer's ``last_active`` time (for predicting the stale sweep), and
    * ``backends[camera]`` — every mock backend the factory has built for that
      camera, in creation order (so we can assert each torn-down session's
      backend had ``close()`` invoked and each new session uses a fresh backend),
    * ``sessions_created[camera]`` — how many sessions the test expects to have
      been started for the camera (incremented on every 0→≥1 transition).
    """

    viewers = Bundle("viewers")

    def __init__(self) -> None:
        super().__init__()
        self.clock = MockClock(start=1000.0)
        self.claims = ClaimRegistry()
        self.config = StreamConfig(
            max_viewers=MAX_VIEWERS,
            stale_timeout_s=STALE_TIMEOUT_S,
        )
        # Every backend ever built, per camera, in creation order.
        self.backends: dict[str, list] = {c: [] for c in CAMERAS}
        # Expected number of sessions started per camera (0→≥1 transitions).
        self.sessions_created: dict[str, int] = {c: 0 for c in CAMERAS}
        # Shadow of active viewers: camera -> {viewer_id: last_active}.
        self.model: dict[str, dict] = {c: {} for c in CAMERAS}

        def backend_factory(camera_id, config):
            backend = _build_mock_backend(camera_id, self.claims, self.clock)
            self.backends[camera_id].append(backend)
            return backend

        # Patch the worker symbol the broadcaster constructs (test-only seam).
        self._worker_patcher = mock.patch(
            "utils.streaming.broadcaster.AcquisitionWorker", _StubWorker
        )
        self._worker_patcher.start()

        self.broadcaster = StreamBroadcaster(
            stream_config=self.config,
            backend_factory=backend_factory,
            time_fn=self.clock,
        )

    def teardown(self) -> None:
        """Stop the worker patch at the end of each generated run."""
        self._worker_patcher.stop()

    # --- helpers ----------------------------------------------------------

    def _camera_active(self, camera: str) -> bool:
        return len(self.model[camera]) > 0

    # --- rules ------------------------------------------------------------

    @rule(target=viewers, camera=st.sampled_from(CAMERAS))
    def subscribe(self, camera):
        """Register a new viewer; the first one for a camera starts a session."""
        was_active = self._camera_active(camera)
        now = self.clock.time()
        result = self.broadcaster.subscribe(camera)

        # max_viewers is huge, the mock open never fails: subscribe is accepted.
        assert result.accepted, f"unexpected rejection: {result.reason}"
        assert result.viewer_id is not None

        if not was_active:
            # 0→≥1 transition: exactly one new session/claim was started.
            self.sessions_created[camera] += 1

        self.model[camera][result.viewer_id] = now
        return (camera, result.viewer_id)

    @rule(viewer=consumes(viewers))
    def unsubscribe(self, viewer):
        """Remove a known viewer; the last one tears the session down."""
        camera, viewer_id = viewer
        # The viewer may already have been swept away by advance_and_sweep; in
        # that case it is gone from the model and unsubscribe is a no-op.
        existed = viewer_id in self.model[camera]
        self.broadcaster.unsubscribe(camera, viewer_id)
        self.model[camera].pop(viewer_id, None)
        # Nothing else to assert here; _check_invariant covers the outcome.
        _ = existed

    @rule(seconds=st.integers(min_value=STALE_TIMEOUT_S + 1, max_value=STALE_TIMEOUT_S + 600))
    def advance_and_sweep(self, seconds):
        """Advance the clock past the stale window and sweep stale viewers.

        Every viewer subscribed before this advance becomes stale
        (``now - last_active > stale_timeout_s``) and is removed; any camera that
        empties as a result is torn down (claim released), exactly like the
        last-viewer unsubscribe path.
        """
        now = self.clock.advance(seconds)
        self.broadcaster.sweep_stale_viewers(now)
        # Mirror the sweep in the model: drop every viewer past the stale window.
        for camera in CAMERAS:
            stale = [
                vid
                for vid, last_active in self.model[camera].items()
                if (now - last_active) > STALE_TIMEOUT_S
            ]
            for vid in stale:
                self.model[camera].pop(vid, None)

    # --- invariant --------------------------------------------------------

    @invariant()
    def _check_invariant(self):
        """Assert the start-on-first / stop-and-release-on-last lifecycle."""
        for camera in CAMERAS:
            expected_count = len(self.model[camera])
            built = self.backends[camera]

            # Reported viewer count tracks the model exactly.
            assert self.broadcaster.viewer_count(camera) == expected_count, (
                f"{camera}: broadcaster count "
                f"{self.broadcaster.viewer_count(camera)} != model {expected_count}"
            )

            # The number of sessions ever started equals the number of backends
            # the factory built — each 0→≥1 transition creates exactly one
            # session, hence exactly one backend (Req 3.1).
            assert len(built) == self.sessions_created[camera], (
                f"{camera}: built {len(built)} backends but expected "
                f"{self.sessions_created[camera]} sessions"
            )

            if expected_count >= 1:
                # Active camera: exactly one open session/claim — the latest
                # backend is open with a single acquired claim, not yet closed.
                assert camera in self.broadcaster._sessions, (
                    f"{camera}: active ({expected_count} viewers) but no session"
                )
                current = built[-1]
                assert current.is_open, f"{camera}: current backend not open"
                assert current.open_count == 1, (
                    f"{camera}: current backend open_count={current.open_count}"
                )
                assert current.close_count == 0, (
                    f"{camera}: active session's backend was closed"
                )
                assert self.claims.open_count(camera) == 1, (
                    f"{camera}: expected exactly 1 open claim, got "
                    f"{self.claims.open_count(camera)}"
                )
            else:
                # Empty camera: no session, claim released.
                assert camera not in self.broadcaster._sessions, (
                    f"{camera}: empty but session still registered"
                )
                assert self.claims.open_count(camera) == 0, (
                    f"{camera}: empty but {self.claims.open_count(camera)} open claims"
                )
                if built:
                    # The most recent session was stopped: close() was invoked
                    # exactly once (claim released, Req 3.3, 3.7, 8.7).
                    last = built[-1]
                    assert last.close_count == 1, (
                        f"{camera}: torn-down backend close_count={last.close_count}"
                    )
                    assert not last.is_open

            # Every non-current backend belongs to a finished session and must
            # have been closed exactly once (no leaked claim across restarts),
            # and a restart always used a brand-new backend instance.
            for old in built[:-1]:
                assert old.close_count == 1, (
                    f"{camera}: a previous session backend was not closed once "
                    f"(close_count={old.close_count})"
                )
                assert not old.is_open

        # Global single-claim invariant: never overlapping, never released twice.
        self.claims.assert_single_claim()


# Feature: concurrent-camera-stream-viewing, Property 2: Session lifecycle (start on first, stop and release on last)
# Validates: Requirements 3.1, 3.3, 3.7, 8.7
TestSessionLifecycle = SessionLifecycleMachine.TestCase
TestSessionLifecycle.settings = settings(max_examples=25, stateful_step_count=40, deadline=None)
