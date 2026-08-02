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
"""Stateful property test for viewer-count correctness and duplicate subscriptions.

Feature: concurrent-camera-stream-viewing, Property 8: Viewer-count correctness and
duplicate subscriptions — the reported active viewer count for a camera is always a
non-negative integer equal to the number of distinct registered viewers; subscribing
K times to the same camera yields K distinct viewer ids that each count toward the
total (a duplicate subscription is a *new* viewer, never a merge).

Validates: Requirements 8.1, 8.4, 8.5, 8.8

Approach
--------
A Hypothesis :class:`RuleBasedStateMachine` drives a real
:class:`~utils.streaming.broadcaster.StreamBroadcaster` (constructed with an injected
mock ``backend_factory`` and an injected :class:`MockClock`) through arbitrary
interleavings of two rules:

* ``subscribe(camera)`` — registers a new viewer for a camera and records the returned
  ``viewer_id`` in a reference model (a ``{camera_id -> set[viewer_id]}`` map). Every
  accepted subscription must return a globally-distinct ``viewer_id`` and bump the
  camera's count by exactly one; a subscription rejected at the per-camera cap must be
  a ``viewer_limit`` rejection that leaves the count untouched.
* ``unsubscribe(viewer)`` — removes a previously-issued viewer and must drop that
  camera's count by exactly one.

After every step an invariant asserts, for every camera, that
``broadcaster.viewer_count(camera)`` is a non-negative ``int`` equal to the number of
distinct viewer ids the reference model expects for that camera.

Worker stubbing (no production change)
--------------------------------------
``StreamBroadcaster._start_session`` constructs and ``start()``s an
:class:`~utils.streaming.worker.AcquisitionWorker` on a real daemon thread. That thread
is irrelevant to viewer-count bookkeeping but would spin a real grab/sleep loop per
first-subscribed camera across hundreds of stateful steps. We therefore monkeypatch the
``AcquisitionWorker`` symbol *in the broadcaster module* with a no-op
:class:`_StubWorker` for the duration of this test module (autouse fixture below). This
is a test-only seam: production code is untouched, and the broadcaster's real
subscribe / unsubscribe / session start+stop / claim logic (including ``backend.open``
/ ``start_stream`` / ``close``) still runs exactly as shipped — only the background
acquisition thread is replaced by a no-op.

This module is import-safe on hosts without the ``gi`` / GenICam / GStreamer stack:
``broadcaster.py`` keeps its device imports lazy and the backend here is the in-memory
mock double.
"""
from unittest.mock import patch

import pytest
from hypothesis import settings
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    multiple,
    rule,
)
from hypothesis import strategies as st

from mock_camera_backend import ClaimRegistry, MockClock, MockCameraBackend
from utils.streaming.models import StreamConfig

import utils.streaming.broadcaster as broadcaster_module
from utils.streaming.broadcaster import StreamBroadcaster

# A small fixed set of cameras the machine quantifies over. Several cameras let the
# machine interleave subscriptions across independent sessions while keeping the search
# space tractable.
CAMERAS = ["Fake_1", "Fake_2", "Fake_3"]
_cameras = st.sampled_from(CAMERAS)

# Per-camera viewer ceiling (StreamConfig default). Reaching it exercises the
# viewer_limit rejection branch without merging duplicate subscriptions.
MAX_VIEWERS = StreamConfig().max_viewers


class _StubWorker:
    """No-op stand-in for :class:`AcquisitionWorker` (no real acquisition thread).

    Matches the constructor signature the broadcaster calls
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)``) and the
    ``start`` / ``stop`` / ``join`` lifecycle the broadcaster drives, but does nothing —
    so subscribe/unsubscribe bookkeeping is exercised without spinning a background
    grab/sleep loop per session. Production worker code is unchanged; only the symbol
    the broadcaster resolves is swapped for this double in tests.
    """

    def __init__(self, session, *args, **kwargs):
        self.session = session

    def start(self):
        return None

    def stop(self):
        return None

    def join(self, timeout=None):
        return None


@pytest.fixture(autouse=True)
def _stub_acquisition_worker():
    """Swap the broadcaster's ``AcquisitionWorker`` for the no-op stub during this module."""
    with patch.object(broadcaster_module, "AcquisitionWorker", _StubWorker):
        yield


class ViewerCountMachine(RuleBasedStateMachine):
    """Drives subscribe/unsubscribe and checks viewer-count == distinct viewers.

    Feature: concurrent-camera-stream-viewing, Property 8: Viewer-count correctness and
    duplicate subscriptions
    Validates: Requirements 8.1, 8.4, 8.5, 8.8
    """

    # Each entry is a ``(camera_id, viewer_id)`` tuple for an active viewer; rules add
    # to this on subscribe and consume from it on unsubscribe.
    viewers = Bundle("viewers")

    def __init__(self):
        super().__init__()
        self.clock = MockClock(start=1000.0)
        self.registry = ClaimRegistry()
        self.config = StreamConfig()
        self.broadcaster = StreamBroadcaster(
            stream_config=self.config,
            backend_factory=self._make_backend,
            time_fn=self.clock,
        )
        # Reference model: camera_id -> set of currently-registered viewer ids.
        self.expected: dict[str, set] = {camera: set() for camera in CAMERAS}
        # Every viewer id ever issued, to assert global distinctness (duplicate
        # subscriptions to the same camera must mint a fresh id, never reuse one).
        self.issued_ids: set = set()

    def _make_backend(self, camera_id, config):
        """Build an always-openable mock backend that shares the claim registry."""
        return MockCameraBackend(
            camera_id,
            claim_registry=self.registry,
            clock=self.clock,
        )

    # --- rules -----------------------------------------------------------

    @rule(target=viewers, camera=_cameras)
    def subscribe(self, camera):
        """Subscribe a viewer; record its distinct id and assert the count bump."""
        before = self.broadcaster.viewer_count(camera)
        assert before == len(self.expected[camera])

        result = self.broadcaster.subscribe(camera)

        if result.accepted:
            # A duplicate subscription to the same camera is a brand-new viewer: the
            # id must be non-null and never seen before (Req 8.5, 8.8).
            assert result.viewer_id is not None
            assert result.viewer_id not in self.issued_ids, (
                f"subscribe reused viewer id {result.viewer_id!r}"
            )
            self.issued_ids.add(result.viewer_id)
            self.expected[camera].add(result.viewer_id)

            # Each subscription adds exactly one to the active count (Req 8.1, 8.4).
            after = self.broadcaster.viewer_count(camera)
            assert after == before + 1
            assert result.viewer_count == after
            return (camera, result.viewer_id)

        # The only rejection reachable here is hitting the per-camera cap; it must not
        # disturb the count (existing viewers untouched).
        assert result.reason == "viewer_limit"
        assert result.viewer_id is None
        assert before == MAX_VIEWERS
        assert self.broadcaster.viewer_count(camera) == before
        assert result.viewer_count == before
        return multiple()

    @rule(entry=consumes(viewers))
    def unsubscribe(self, entry):
        """Unsubscribe a known viewer; assert the count drops by exactly one."""
        camera, viewer_id = entry
        before = self.broadcaster.viewer_count(camera)
        assert viewer_id in self.expected[camera]

        self.broadcaster.unsubscribe(camera, viewer_id)
        self.expected[camera].discard(viewer_id)

        after = self.broadcaster.viewer_count(camera)
        assert after == before - 1
        assert after == len(self.expected[camera])

    # --- invariant -------------------------------------------------------

    @invariant()
    def viewer_count_matches_distinct_viewers(self):
        """Count is a non-negative int equal to the distinct registered viewers."""
        for camera in CAMERAS:
            count = self.broadcaster.viewer_count(camera)
            # A genuine non-negative integer (reject bools, which are int subclasses).
            assert isinstance(count, int) and not isinstance(count, bool)
            assert count >= 0
            assert count == len(self.expected[camera])


# Feature: concurrent-camera-stream-viewing, Property 8: Viewer-count correctness and duplicate subscriptions
# Validates: Requirements 8.1, 8.4, 8.5, 8.8
TestViewerCount = ViewerCountMachine.TestCase
TestViewerCount.settings = settings(max_examples=25, stateful_step_count=50, deadline=None)
