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
"""Property-based test for settings-apply failure retention.

Feature: concurrent-camera-stream-viewing, Property 19: Settings-apply failure
retains prior values — for any apply request in which at least one control fails,
the operation returns an error identifying the failed control, the control values
in effect before the request are retained unchanged, and the session remains
active.

Validates: Requirements 5.5.

How the property is exercised
-----------------------------
A real :class:`StreamBroadcaster` is wired with an injected ``backend_factory``
(building the shared :class:`MockCameraBackend` registered against one shared
:class:`ClaimRegistry`) and an injected :class:`MockClock`, exactly like the
single-claim / inference-failure property tests. For each example we:

1. Subscribe a generated number of viewers to a single camera (start-on-first
   opens the one device claim; the rest reuse it). The session is now ACTIVE
   (RUNNING) holding exactly one claim.
2. *Optionally* perform one successful ``apply_settings`` first (mock
   ``fail_apply`` off) to establish the prior in-effect control values
   (``session.applied_features``).
3. Flip the mock backend to ``fail_apply=True`` and issue a subsequent
   ``apply_settings`` with a generated, non-empty feature set, which the backend
   rejects (models "at least one control fails").

We then assert, for every variation, prior/failed feature set and viewer count,
that the failed request (Req 5.5, Property 19):

* raised :class:`SettingsApplyError`;
* the error names the failed control(s) (``err.control`` matches the controls in
  the failed request);
* the prior in-effect values are retained unchanged — ``err.retained`` and the
  session's ``applied_features`` both still equal the pre-request snapshot; and
* the session remained active (still present, still ``RUNNING``) with its viewer
  set unchanged and its single device claim still held.

Test-only technique (no production code changed)
------------------------------------------------
Like ``test_broadcaster_claim.py`` / ``test_inference_failure.py``, this patches
the ``AcquisitionWorker`` symbol in the broadcaster module with a no-op stub for
the duration of each example so no real daemon thread / wall-clock grab loop runs.
The stub marks the session RUNNING on ``start()`` (mirroring the real worker after
its first published frame), which is exactly the live state the property
quantifies over. Failure is induced purely through the mock backend's existing
``fail_apply`` knob; production code is untouched.
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
)
from utils.streaming import broadcaster as broadcaster_module
from utils.streaming.broadcaster import (
    SettingsApplyError,
    StreamBroadcaster,
    _describe_failed_controls,
)
from utils.streaming.models import SessionState, StreamConfig

# Keep max_viewers comfortably above the generated viewer count so the active
# session — not the viewer-limit branch — is what we exercise.
_CONFIG = StreamConfig(max_viewers=8, stale_after_s=2.0)
_CAMERA = "Fake_settings_fail_1"


class _StubAcquisitionWorker:
    """No-op stand-in for ``AcquisitionWorker`` (test-only; see module docstring).

    Matches the broadcaster's construction/usage contract
    (``AcquisitionWorker(session, stream_config=..., time_fn=...)`` then
    ``start()`` / ``stop()`` / ``join()``) but runs no thread and performs no
    grabs, so the session state is deterministic. ``start()`` marks the session
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


@st.composite
def _feature_dict(draw, *, require_nonempty: bool = False):
    """Generate a varied image-source-style camera-control feature dict.

    Produces an arbitrary subset of ``gain`` / ``exposure`` plus an optional
    ``advancedSettings`` list of named GenICam features — the shapes
    ``apply_features`` / ``_describe_failed_controls`` accept. When
    ``require_nonempty`` is set the result is guaranteed to carry at least one
    named control (used for the failing request so a control name is always
    derivable).
    """
    features: dict = {}
    if draw(st.booleans()):
        features["gain"] = draw(st.integers(min_value=0, max_value=100))
    if draw(st.booleans()):
        features["exposure"] = draw(st.integers(min_value=1, max_value=100000))
    advanced = draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "feature": st.sampled_from(
                        ["Gamma", "BlackLevel", "Sharpness", "Saturation"]
                    ),
                    "value": st.integers(min_value=0, max_value=255),
                }
            ),
            max_size=3,
        )
    )
    if advanced:
        features["advancedSettings"] = advanced
    if require_nonempty and not features:
        features["gain"] = draw(st.integers(min_value=0, max_value=100))
    return features


# Feature: concurrent-camera-stream-viewing, Property 19: Settings-apply failure retains prior values
# Validates: Requirements 5.5
@pytest.mark.parametrize(
    "backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES)
)
@settings(max_examples=25, deadline=None)
@given(
    num_viewers=st.integers(min_value=1, max_value=_CONFIG.max_viewers),
    establish_prior=st.booleans(),
    prior_features=_feature_dict(),
    failed_features=_feature_dict(require_nonempty=True),
    clock_advance=st.floats(
        min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_19_settings_apply_failure_retains_prior_values(
    backend_cls,
    num_viewers,
    establish_prior,
    prior_features,
    failed_features,
    clock_advance,
):
    """A failing apply raises SettingsApplyError naming the failed control,
    retains the prior in-effect values unchanged, and leaves the session active
    (RUNNING) with its viewer set and single claim intact.

    Feature: concurrent-camera-stream-viewing, Property 19: Settings-apply failure retains prior values
    Validates: Requirements 5.5
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
        # 1. Subscribe the generated number of viewers: start-on-first opens the
        #    one claim, the rest reuse it. The session is now ACTIVE (RUNNING).
        viewer_ids = []
        for _ in range(num_viewers):
            result = broadcaster.subscribe(_CAMERA)
            assert result.accepted is True
            viewer_ids.append(result.viewer_id)

        assert broadcaster.viewer_count(_CAMERA) == num_viewers
        assert registry.open_count(_CAMERA) == 1

        session = broadcaster._sessions[_CAMERA]
        backend = created[_CAMERA]
        assert session.state == SessionState.RUNNING

        # 2. Optionally establish the prior in-effect control values via one
        #    successful apply (fail_apply is off by default on the mock).
        if establish_prior:
            accepted = broadcaster.apply_settings(_CAMERA, prior_features)
            # The session now records the device-accepted prior values.
            assert session.applied_features == accepted

        # Snapshot the values in effect BEFORE the failing request — these are
        # exactly what must be retained unchanged afterwards.
        prior_snapshot = dict(session.applied_features)

        opens_before = backend.open_count
        closes_before = backend.close_count

        # Varying the clock changes the freshness context; the failed apply must
        # behave identically (error + retention + active session).
        clock.advance(clock_advance)

        # 3. Flip the backend to reject the next apply, then issue the failing
        #    apply request (models "at least one control fails").
        backend.fail_apply = True

        with pytest.raises(SettingsApplyError) as exc_info:
            broadcaster.apply_settings(_CAMERA, failed_features)

    err = exc_info.value

    # --- assertions: the failure is descriptive and retains prior state ---

    # The error identifies the camera and names the failed control(s) (Req 5.5).
    assert err.camera_id == _CAMERA
    expected_control = _describe_failed_controls(failed_features)
    assert err.control == expected_control
    assert expected_control != "<unknown control>"  # a named control was derived

    # The prior in-effect values are retained unchanged: both the error's
    # ``retained`` payload and the session's recorded applied_features still equal
    # the pre-request snapshot (the failed request did not mutate them).
    assert err.retained == prior_snapshot
    assert session.applied_features == prior_snapshot

    # The session remains active: still present, still RUNNING, viewer set
    # unchanged, and the single device claim still held (not opened/closed by the
    # failed apply).
    assert _CAMERA in broadcaster._sessions
    assert broadcaster._sessions[_CAMERA] is session
    assert session.state == SessionState.RUNNING
    assert broadcaster.viewer_count(_CAMERA) == num_viewers
    assert set(session.viewers.keys()) == set(viewer_ids)
    assert registry.open_count(_CAMERA) == 1
    assert registry.total_open() == 1
    assert backend.open_count == opens_before    # no new claim opened
    assert backend.close_count == closes_before  # claim retained (not released)
    registry.assert_single_claim()

    # Cleanup: drain viewers so the broadcaster stops the session and releases the
    # claim through its normal stop-on-last path, returning the claim count to 0.
    with mock.patch.object(broadcaster_module, "AcquisitionWorker", _StubAcquisitionWorker):
        for viewer_id in list(session.viewers.keys()):
            broadcaster.unsubscribe(_CAMERA, viewer_id)
    assert registry.open_count(_CAMERA) == 0
