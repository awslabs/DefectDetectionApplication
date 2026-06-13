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
"""Property-based test for live settings apply preserving the session.

Feature: concurrent-camera-stream-viewing, Property 18: Live settings apply
preserves the session — for any set of valid camera control values applied to a
running session, the backend reflects the requested values, the session remains
RUNNING, the subscribed viewer set is unchanged, and the latest frame remains
readable throughout.

Validates: Requirements 5.1, 5.2, 5.3.

How the property is exercised
-----------------------------
:meth:`StreamBroadcaster.apply_settings` is driven against an injected
``backend_factory`` that builds the hardware-free :class:`MockCameraBackend`
registered against one shared :class:`ClaimRegistry` (so the test can read the
true per-camera open-claim count) plus an injected :class:`MockClock` (so frame
freshness is deterministic). The mock's ``apply_features`` records each request
and returns the device-accepted values (the normalized requested values), which
is exactly what the broadcaster hands back to its caller.

Per example Hypothesis generates a camera id, a viewer count ``N`` in
``[1, max_viewers]``, a frame geometry, and a *valid* feature dict drawn from
gain / exposure / advancedSettings. The test:

1. Subscribes ``N`` viewers (the first opens the single device claim) and records
   the exact set of issued viewer ids.
2. Publishes one fresh frame into the live session so there is a readable latest
   frame before the apply.
3. Calls ``apply_settings`` with the generated valid feature dict.
4. Asserts the post-conditions of Property 18:
   * the return value equals the device-accepted values (the mock reflects the
     requested values), and the backend recorded the apply (Req 5.1, 5.2),
   * ``session.state`` is still ``RUNNING`` (Req 5.3),
   * the subscribed viewer-id set is byte-for-byte unchanged before/after
     (Req 5.3),
   * ``get_frame`` still returns ``OK`` with a readable frame (the same published
     payload) after the apply — the latest frame stayed readable throughout
     (Req 5.2, 5.3),
   * the single device claim was neither re-opened nor released (count stays 1).

Worker-stub technique (test-only; mirrors ``test_inference_reuse.py``)
----------------------------------------------------------------------
In production a subscribe starts a real :class:`AcquisitionWorker` on a daemon
thread (a ``grab -> publish`` loop). That thread is irrelevant to the
settings-apply property and would make the live frame non-deterministic. For the
lifetime of each example the ``AcquisitionWorker`` symbol in the broadcaster
module is patched with a no-op stub that simply marks the session RUNNING on
``start()`` (mirroring the real worker after its first published frame). The
live frame is then made deterministically available by publishing it through the
session directly before the apply. The broadcaster's subscribe / apply_settings /
get_frame logic — and therefore every ``open()`` / ``close()`` / ``apply_features``
that touches the device — runs completely unchanged. No production code is
modified.
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
from utils.streaming.models import FrameStatus, SessionState, StreamConfig

# A small fixed set of physical cameras the property quantifies over.
CAMERAS = ("cam_a", "cam_b", "cam_c")

# Keep max_viewers small so subscribing the full range stays fast while still
# exercising varied viewer counts (the property must hold for any N in range).
_MAX_VIEWERS = 4
_STREAM_CONFIG = StreamConfig(max_viewers=_MAX_VIEWERS, stale_timeout_s=30)

# Hypothesis runs >= 100 iterations; the work per example is tiny and synchronous.
_PROP_SETTINGS = settings(max_examples=120, deadline=None)


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the construction/usage contract the broadcaster relies on
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so the session frame and the open-claim count are deterministic.
    ``start()`` marks the session RUNNING to mirror the real worker after its
    first published frame.
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


# --------------------------------------------------------------------------- #
# Strategy: a *valid* camera-control feature dict (gain / exposure / advanced).
# --------------------------------------------------------------------------- #
# Valid gain / exposure scalar values. Kept to finite, in-range numbers so the
# device (mock) accepts them verbatim — Property 18 is about valid values being
# reflected (out-of-range rejection is Property 19, out of scope here).
_gain_values = st.floats(min_value=0.0, max_value=48.0, allow_nan=False, allow_infinity=False)
_exposure_values = st.floats(
    min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False
)

# An advancedSettings entry is a {"feature","value"} device-feature mapping.
_advanced_feature_names = st.sampled_from(
    ["Gamma", "BlackLevel", "Sharpness", "Saturation", "Brightness"]
)
_advanced_entry = st.fixed_dictionaries(
    {
        "feature": _advanced_feature_names,
        "value": st.one_of(
            st.floats(min_value=0.0, max_value=4.0, allow_nan=False, allow_infinity=False),
            st.integers(min_value=0, max_value=255),
        ),
    }
)


@st.composite
def valid_feature_dicts(draw):
    """Generate a non-empty image-source-style feature dict of valid controls.

    Produces some combination of ``gain`` / ``exposure`` / ``advancedSettings``
    (at least one present) so every generated request carries at least one
    control to apply. ``advancedSettings`` is a list of distinct-named feature
    entries.
    """
    include_gain = draw(st.booleans())
    include_exposure = draw(st.booleans())
    include_advanced = draw(st.booleans())
    # Ensure at least one control is present.
    if not (include_gain or include_exposure or include_advanced):
        include_gain = True

    features: dict = {}
    if include_gain:
        features["gain"] = draw(_gain_values)
    if include_exposure:
        features["exposure"] = draw(_exposure_values)
    if include_advanced:
        entries = draw(
            st.lists(_advanced_entry, min_size=1, max_size=5, unique_by=lambda e: e["feature"])
        )
        features["advancedSettings"] = entries
    return features


def _make_broadcaster(backend_kind, registry, clock, created):
    """Build a broadcaster whose factory records every backend it creates.

    Each backend shares the one ``registry`` + ``clock`` so the test reads the
    true per-camera open-claim count; the ``created`` list captures every backend
    instance so the test can assert no second claim was opened during the apply.
    """

    def backend_factory(camera_id, config) -> MockCameraBackend:
        backend = make_backend(
            backend_kind,
            camera_id,
            claim_registry=registry,
            clock=clock,
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
    viewer_count=st.integers(min_value=1, max_value=_MAX_VIEWERS),
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
    seq=st.integers(min_value=1, max_value=1000),
    features=valid_feature_dicts(),
)
@_PROP_SETTINGS
def test_live_settings_apply_preserves_session(
    backend_kind, camera, viewer_count, width, height, seq, features
):
    """Applying valid settings to a running session preserves it (Req 5.1, 5.2, 5.3).

    Feature: concurrent-camera-stream-viewing, Property 18: Live settings apply
    preserves the session.
    Validates: Requirements 5.1, 5.2, 5.3.
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1_000.0)
    created: List[MockCameraBackend] = []

    with mock.patch.object(
        broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker
    ):
        broadcaster = _make_broadcaster(backend_kind, registry, clock, created)

        # Subscribe N viewers (the first opens the single device claim). Record the
        # exact set of issued viewer ids so we can prove it is unchanged later.
        viewer_ids = set()
        for _ in range(viewer_count):
            result = broadcaster.subscribe(camera)
            assert result.accepted, "subscribe within max_viewers must be accepted"
            viewer_ids.add(result.viewer_id)

        assert len(viewer_ids) == viewer_count
        assert registry.open_count(camera) == 1, "exactly one claim for the active camera"
        assert len(created) == 1, "subscribe should build exactly one backend"
        backend = created[0]

        session = broadcaster._sessions[camera]
        assert session.state == SessionState.RUNNING

        # Publish a fresh frame into the live session so there is a readable latest
        # frame before the apply (no real worker thread runs).
        published = session.publish(
            make_raw_frame(seq=seq, width=width, height=height), now=clock.time()
        )

        # Confirm the frame is readable BEFORE the apply.
        viewer_for_read = next(iter(viewer_ids))
        before = broadcaster.get_frame(camera, viewer_for_read)
        assert before.status == FrameStatus.OK
        assert before.frame is not None

        # Snapshot the viewer set and claim accounting immediately before applying.
        viewers_before = set(session.viewers.keys())
        assert viewers_before == viewer_ids
        opens_before = backend.open_count
        closes_before = backend.close_count
        applies_before = backend.apply_features_count

        # --- the operation under test: apply valid live settings ---
        accepted = broadcaster.apply_settings(camera, features)

        # 1) The backend reflects the requested values: the broadcaster returns
        #    exactly the device-accepted set, and the backend recorded the apply
        #    (Req 5.1, 5.2). The mock echoes the normalized requested values.
        assert isinstance(accepted, dict)
        assert backend.apply_features_count == applies_before + 1
        assert backend.applied_features[-1] == features
        assert accepted == backend.last_applied_features
        expected_accepted = MockCameraBackend._normalize_features(features)
        assert accepted == expected_accepted

        # 2) The session remains RUNNING (Req 5.3).
        assert broadcaster._sessions.get(camera) is session
        assert session.state == SessionState.RUNNING

        # 3) The subscribed viewer set is unchanged before/after (Req 5.3).
        viewers_after = set(session.viewers.keys())
        assert viewers_after == viewers_before == viewer_ids
        assert broadcaster.viewer_count(camera) == viewer_count

        # 4) The latest frame is still readable after the apply, returning the same
        #    published payload/seq (the slot was never cleared) (Req 5.2, 5.3).
        after = broadcaster.get_frame(camera, viewer_for_read)
        assert after.status == FrameStatus.OK
        assert after.frame is not None
        assert after.frame.seq == published.seq
        assert after.frame.data == published.data
        assert after.frame.width == width
        assert after.frame.height == height

        # 5) The single device claim was neither re-opened nor released by the apply
        #    (the live claim is reused; no overlap ever observed).
        assert backend.open_count == opens_before, "apply must not re-open the claim"
        assert backend.close_count == closes_before, "apply must not release the claim"
        assert registry.open_count(camera) == 1
        assert registry.max_concurrent.get(camera) == 1
        assert not registry.overlap_detected
        assert len(created) == 1, "no second backend may be built by apply"

        # Cleanup: drop all viewers so the claim is released through the normal path.
        for viewer_id in viewer_ids:
            broadcaster.unsubscribe(camera, viewer_id)
        assert registry.open_count(camera) == 0
