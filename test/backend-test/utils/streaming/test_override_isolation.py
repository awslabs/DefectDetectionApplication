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
"""Property-based test for override-preview isolation.

Feature: concurrent-camera-stream-viewing, Property 20: Override preview isolation
— for any per-request configuration override previewed against a *running* session,
the session's applied control values (``session.applied_features``) and the shared
latest-frame slot delivered to other viewers are left exactly unchanged by the
override request.

Validates: Requirements 5.4.

How the property is exercised
-----------------------------
A real :class:`StreamBroadcaster` is wired with an injected ``backend_factory``
(building the hardware-free :class:`MockCameraBackend` registered against one shared
:class:`ClaimRegistry`) and an injected :class:`MockClock`, exactly like the other
broadcaster property tests. For each example we:

1. Subscribe a generated number of viewers to one camera. Start-on-first opens the
   single device claim; the rest reuse it. A test-only worker stub marks the session
   RUNNING and publishes one deterministic frame into the shared latest-frame slot
   (mirroring the real acquisition worker after its first published frame).
2. Optionally perform a prior successful ``apply_settings`` so the session has a
   non-empty ``applied_features`` (the in-effect control set) to protect.
3. Snapshot the protected state: a deep copy of ``session.applied_features``, the
   identity + seq + bytes of the current latest-frame slot, the backend's
   ``apply_features`` call count, and the frame every viewer currently reads via
   ``get_frame``.
4. Call ``preview_with_override(camera_id, override_config)`` with a generated
   override config (optionally several times).

We then assert the isolation post-condition (per task 8.3): after the call(s),

* ``session.applied_features`` is byte-for-byte unchanged (equal to the snapshot);
* the shared latest-frame slot is the *same object* with the same seq and bytes — so
  every other viewer's ``get_frame`` returns the identical, same-seq frame; and
* the backend saw no additional ``apply_features`` call from the preview (the override
  was never applied to the live claim) and the single claim is still held.

Note (per task 8.3): while a session is active the override is intentionally NOT
applied to the live claim — isolation is the guarantee. This test asserts isolation,
not that the override is reflected in the returned preview.

Test-only technique (no production code changed)
------------------------------------------------
Like ``test_inference_reuse.py`` / ``test_inference_failure.py``, the
``AcquisitionWorker`` symbol in the broadcaster module is patched with a no-op stub
for the lifetime of each example so no real daemon thread / wall-clock grab loop
runs. The stub marks the session RUNNING and publishes a single deterministic frame
on ``start()`` (mirroring the real worker after its first frame). The broadcaster's
subscribe / apply_settings / preview_with_override logic runs completely unchanged.
"""
from __future__ import annotations

import copy
import unittest.mock as mock
from typing import List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import utils.streaming.broadcaster as broadcaster_module
from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    ClaimRegistry,
    MockCameraBackend,
    MockClock,
    make_raw_frame,
)
from utils.streaming.broadcaster import StreamBroadcaster
from utils.streaming.models import FrameStatus, SessionState, StreamConfig

# Keep max_viewers comfortably above the generated viewer count so the active
# session (not the viewer-limit branch) is what we exercise.
_CONFIG = StreamConfig(max_viewers=8, stale_after_s=600.0)
_CAMERA = "Fake_override_iso_1"

# The deterministic frame the worker stub publishes into the shared slot. Its
# byte payload is unique so "same frame" can be asserted on identity AND content.
_SESSION_FRAME_SEQ = 7
_SESSION_FRAME = make_raw_frame(seq=_SESSION_FRAME_SEQ, width=8, height=8)


def _make_worker_stub(frame, clock):
    """Build a no-op ``AcquisitionWorker`` stub that publishes ``frame`` on start.

    The stub matches the broadcaster's construction/usage contract
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then ``start()`` /
    ``stop()`` / ``join()``) but runs no thread. ``start()`` marks the session RUNNING
    and publishes a single deterministic frame into the shared latest-frame slot,
    mirroring the real worker after its first published frame.
    """

    class _StubAcquisitionWorker:
        def __init__(self, session, *, stream_config=None, time_fn=None, **kwargs):
            self.session = session

        def start(self):
            self.session.state = SessionState.RUNNING
            self.session.publish(frame, now=clock.time())
            return None

        def stop(self):
            return None

        def join(self, timeout: Optional[float] = None):
            return None

    return _StubAcquisitionWorker


# --- generators -------------------------------------------------------------

# A single control value: ints / floats / short strings, the shapes the backend's
# apply_features accepts for gain / exposure / advanced controls.
_control_values = st.one_of(
    st.integers(min_value=-100, max_value=10_000),
    st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    st.text(min_size=0, max_size=8),
    st.booleans(),
)

# An image-source-style control dict (gain / exposure / brightness / contrast ...).
_control_dict = st.dictionaries(
    keys=st.sampled_from(["gain", "exposure", "brightness", "contrast", "gamma"]),
    values=_control_values,
    min_size=0,
    max_size=5,
)

# An advanced-settings device-feature list ([{"feature","value"}, ...]).
_advanced_list = st.lists(
    st.fixed_dictionaries(
        {
            "feature": st.sampled_from(
                ["Gain", "ExposureTime", "Gamma", "BlackLevel", "AcquisitionFrameRate"]
            ),
            "value": _control_values,
        }
    ),
    min_size=0,
    max_size=4,
)


@st.composite
def _override_configs(draw):
    """Generate a varied per-request override config (dict / list / advanced / None)."""
    kind = draw(st.sampled_from(["dict", "dict_with_advanced", "feature_list", "none", "empty"]))
    if kind == "none":
        return None
    if kind == "empty":
        return {}
    if kind == "feature_list":
        return draw(_advanced_list)
    config = draw(_control_dict)
    if kind == "dict_with_advanced":
        config = dict(config)
        config["advancedSettings"] = draw(_advanced_list)
    return config


# Feature: concurrent-camera-stream-viewing, Property 20: Override preview isolation
# Validates: Requirements 5.4
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=150, deadline=None)
@given(
    num_viewers=st.integers(min_value=1, max_value=_CONFIG.max_viewers),
    prior_features=st.one_of(st.none(), _control_dict),
    override_config=_override_configs(),
    num_previews=st.integers(min_value=1, max_value=4),
    clock_advance=st.floats(
        min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_20_override_preview_isolation(
    backend_cls, num_viewers, prior_features, override_config, num_previews, clock_advance
):
    """An override preview against a running session leaves the session's applied
    control values and the shared latest-frame slot exactly unchanged for every viewer.

    Feature: concurrent-camera-stream-viewing, Property 20: Override preview isolation
    Validates: Requirements 5.4
    """
    registry = ClaimRegistry()
    clock = MockClock(start=1000.0)
    created: dict[str, MockCameraBackend] = {}

    def backend_factory(camera_id, config):
        backend = backend_cls(camera_id, claim_registry=registry, clock=clock)
        created[camera_id] = backend
        return backend

    broadcaster = StreamBroadcaster(
        stream_config=_CONFIG,
        backend_factory=backend_factory,
        time_fn=clock.time,
    )

    worker_stub = _make_worker_stub(_SESSION_FRAME, clock)

    with mock.patch.object(broadcaster_module, "AcquisitionWorker", worker_stub):
        # 1. Subscribe the generated number of viewers; start-on-first opens the one
        #    claim and the worker stub publishes the deterministic frame + marks RUNNING.
        viewer_ids: List[str] = []
        for _ in range(num_viewers):
            result = broadcaster.subscribe(_CAMERA)
            assert result.accepted is True
            viewer_ids.append(result.viewer_id)

        session = broadcaster._sessions[_CAMERA]
        backend = created[_CAMERA]
        assert session.state == SessionState.RUNNING
        assert broadcaster.viewer_count(_CAMERA) == num_viewers
        assert registry.open_count(_CAMERA) == 1
        assert session.latest is not None  # the worker stub published a frame

        # 2. Optionally establish a non-empty applied_features via a prior successful
        #    apply_settings (the in-effect control set the override must not disturb).
        if prior_features:
            broadcaster.apply_settings(_CAMERA, prior_features)

        # 3. Snapshot the protected state BEFORE the override preview.
        applied_before = copy.deepcopy(session.applied_features)
        latest_obj_before = session.latest
        seq_before = session.latest.seq
        data_before = session.latest.data
        apply_count_before = backend.apply_features_count
        opens_before = backend.open_count
        closes_before = backend.close_count

        # The frame every viewer currently reads (get_frame refreshes heartbeat but
        # must not alter the slot). Captured before the override for comparison.
        frames_before = {}
        for viewer_id in viewer_ids:
            res = broadcaster.get_frame(_CAMERA, viewer_id)
            assert res.status == FrameStatus.OK
            frames_before[viewer_id] = (res.frame.seq, res.frame.data)

        # Vary the freshness context; isolation must hold regardless of clock.
        clock.advance(clock_advance)

        # 4. Drive the override preview request(s) against the running session.
        for _ in range(num_previews):
            preview = broadcaster.preview_with_override(_CAMERA, override_config)
            # Active-session path returns a pure read of the current latest frame;
            # since a frame was published it returns that frame's payload (the
            # override is intentionally NOT reflected — isolation is the guarantee).
            assert preview is not None
            assert preview["data"] == data_before

        # --- assertions: the session is fully isolated from the override (Req 5.4) ---

        # (a) applied_features is byte-for-byte unchanged.
        assert session.applied_features == applied_before

        # (b) the shared latest-frame slot is the SAME object with the same seq/bytes,
        #     so the override never touched the frame other viewers read.
        assert session.latest is latest_obj_before
        assert session.latest.seq == seq_before
        assert session.latest.data == data_before

        # (c) every viewer still reads the identical, same-seq frame after the override.
        for viewer_id in viewer_ids:
            res = broadcaster.get_frame(_CAMERA, viewer_id)
            assert res.status == FrameStatus.OK
            assert (res.frame.seq, res.frame.data) == frames_before[viewer_id]
            assert res.frame.seq == seq_before
            assert res.frame.data == data_before

        # (d) the override was never applied to the live claim: no extra apply_features,
        #     and no second claim opened / claim released by the preview.
        assert backend.apply_features_count == apply_count_before
        assert backend.open_count == opens_before
        assert backend.close_count == closes_before
        assert registry.open_count(_CAMERA) == 1
        assert registry.total_open() == 1

        # The session is left intact (still RUNNING, viewer set unchanged).
        assert session.state == SessionState.RUNNING
        assert broadcaster.viewer_count(_CAMERA) == num_viewers
        assert set(session.viewers.keys()) == set(viewer_ids)

        # The single-claim invariant held throughout.
        registry.assert_single_claim()

        # Cleanup: drain viewers so the session stops and the claim is released.
        for viewer_id in list(session.viewers.keys()):
            broadcaster.unsubscribe(_CAMERA, viewer_id)

    assert registry.open_count(_CAMERA) == 0
