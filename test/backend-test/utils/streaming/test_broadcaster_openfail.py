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
"""Property-based test for open-failure handling.

Feature: concurrent-camera-stream-viewing, Property 7: Open-failure handling —
for any camera whose backend ``open()`` fails, a subscribe makes at most
``max_open_attempts`` (3) attempts and is then rejected with reason
``"camera_unavailable"``, leaving no session and zero open device claims for that
camera, while subscriptions to any other healthy camera still succeed
(Req 1.7, 3.2, 7.6).

Design notes / test seams
-------------------------
* The :class:`StreamBroadcaster` is driven with an injected ``backend_factory``
  that returns a *failing* mock backend (``fail_open=True``, so every ``open()``
  raises) for a designated set of "bad" cameras and a *healthy* mock for every
  other camera. All mocks share a single :class:`ClaimRegistry`, so the test can
  assert the zero-claims invariant for the bad camera and the single open claim
  for healthy cameras across the whole interleaving.

* ``max_open_attempts`` semantics: in production the *backend adapter* retries
  ``open()`` up to ``max_open_attempts`` internally (the real Aravis/GStreamer
  adapters do ``<= 3``); the broadcaster calls ``backend.open()`` once and
  surfaces the resulting failure as a ``camera_unavailable`` rejection. We
  therefore assert the broadcaster-level rejection + zero-claims invariant, and
  additionally assert the mock's recorded open-failure count never exceeds
  ``max_open_attempts`` (it is exactly 1 per subscribe at the broadcaster seam).

* Worker threads: a subscribe that *succeeds* would normally spawn a real
  acquisition-worker daemon thread. To keep the property deterministic and
  thread-free we replace the ``AcquisitionWorker`` symbol in the broadcaster
  module with a tiny test-only no-op subclass (``_NoopWorker``) for the duration
  of each example. This is a TEST-ONLY stub installed via ``mock.patch`` on the
  broadcaster module's name binding — **production code is not modified**; the
  broadcaster still constructs and tracks a "worker" and drives the real
  open/start_stream claim lifecycle, only the thread spin-up is stubbed out.
"""
from __future__ import annotations

import unittest.mock as mock

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
from utils.streaming.models import StreamConfig
from utils.streaming.worker import AcquisitionWorker

MAX_OPEN_ATTEMPTS = StreamConfig().max_open_attempts  # 3
_CONFIG = StreamConfig()


class _NoopWorker(AcquisitionWorker):
    """Test-only :class:`AcquisitionWorker` that never spawns a real thread.

    The broadcaster starts one acquisition worker per healthy session. For this
    property we only care about the open/claim lifecycle, not frame production,
    so ``start``/``stop``/``join`` are no-ops and no background thread (or wall
    clock) is involved. Subclassing keeps the constructor contract identical, so
    the broadcaster wires it exactly as it would the real worker.
    """

    def start(self):  # noqa: D401 - simple stub
        """Do not spawn a thread; record that a (stub) worker was started."""
        self._thread = None
        return None

    def stop(self) -> None:
        """No-op stop for the stub worker."""
        self.stop_event.set()

    def join(self, timeout=None) -> None:
        """No-op join for the stub worker."""
        return None


# Distinct, non-overlapping camera-id pools for healthy vs. bad cameras.
_healthy_ids = st.lists(
    st.from_regex(r"Healthy_[0-9]{1,3}", fullmatch=True),
    min_size=1,
    max_size=4,
    unique=True,
)
_bad_ids = st.lists(
    st.from_regex(r"Bad_[0-9]{1,3}", fullmatch=True),
    min_size=1,
    max_size=4,
    unique=True,
)


# Feature: concurrent-camera-stream-viewing, Property 7: Open-failure handling
# Validates: Requirements 1.7, 3.2, 7.6
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=150, deadline=None)
@given(
    healthy_ids=_healthy_ids,
    bad_ids=_bad_ids,
    seed=st.randoms(use_true_random=False),
)
def test_property_7_open_failure_handling(backend_cls, healthy_ids, bad_ids, seed):
    """Bad-camera subscribes reject with ``camera_unavailable`` and zero claims;
    healthy cameras still subscribe and hold exactly one claim.

    Feature: concurrent-camera-stream-viewing, Property 7: Open-failure handling
    Validates: Requirements 1.7, 3.2, 7.6
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1000.0)
    created: dict[str, object] = {}

    def backend_factory(camera_id, config):
        """Return a failing mock for bad cameras, a healthy mock otherwise.

        All backends share the single ``registry`` so the open-claim count per
        camera can be asserted across the whole interleaving.
        """
        if camera_id in bad_set:
            backend = backend_cls(
                camera_id,
                fail_open=True,           # every open() raises -> camera_unavailable
                claim_registry=registry,
                clock=clock,
            )
        else:
            backend = backend_cls(
                camera_id,
                frames=[make_raw_frame(seq=0)],
                claim_registry=registry,
                clock=clock,
            )
        created[camera_id] = backend
        return backend

    bad_set = set(bad_ids)
    # Ensure the two pools are disjoint (regex prefixes already guarantee this,
    # but be explicit so a future generator tweak cannot silently overlap them).
    assert bad_set.isdisjoint(set(healthy_ids))

    broadcaster = StreamBroadcaster(
        stream_config=_CONFIG,
        backend_factory=backend_factory,
        time_fn=clock.time,
    )

    # Interleave the healthy and bad subscribes in an arbitrary order so the
    # property holds regardless of sequencing.
    order = [("healthy", cid) for cid in healthy_ids] + [("bad", cid) for cid in bad_ids]
    seed.shuffle(order)

    subscribed_healthy: set[str] = set()

    # Stub worker thread creation for the duration of the scenario (test-only).
    with mock.patch.object(broadcaster_module, "AcquisitionWorker", _NoopWorker):
        for kind, camera_id in order:
            result = broadcaster.subscribe(camera_id)

            if kind == "bad":
                # Rejected as camera_unavailable, viewer_count 0, no viewer id.
                assert result.accepted is False
                assert result.reason == "camera_unavailable"
                assert result.viewer_count == 0
                assert result.viewer_id is None

                # No session left behind for the bad camera.
                assert camera_id not in broadcaster._sessions
                assert broadcaster.viewer_count(camera_id) == 0

                # Zero OPEN claims for the bad camera (open failed -> never acquired).
                assert registry.open_count(camera_id) == 0

                # At most max_open_attempts open attempts were made (Req 7.6). At the
                # broadcaster seam this is exactly 1 failed open per subscribe.
                assert created[camera_id].open_failure_count <= MAX_OPEN_ATTEMPTS
            else:
                # Healthy subscribe still succeeds even alongside failing cameras.
                assert result.accepted is True
                assert result.reason is None
                assert result.viewer_id is not None
                assert result.viewer_count >= 1

                # Exactly one open device claim is held for a healthy camera.
                assert registry.open_count(camera_id) == 1
                assert broadcaster.viewer_count(camera_id) == result.viewer_count
                subscribed_healthy.add(camera_id)

    # Final invariants over the whole run:
    # - Every healthy camera holds exactly one claim; no bad camera holds any.
    for camera_id in subscribed_healthy:
        assert registry.open_count(camera_id) == 1
    for camera_id in bad_set:
        assert registry.open_count(camera_id) == 0
        assert camera_id not in broadcaster._sessions

    # - The single-claim invariant never overlapped for any camera.
    registry.assert_single_claim()
    # - Total open claims equals the number of distinct healthy cameras started.
    assert registry.total_open() == len(subscribed_healthy)
