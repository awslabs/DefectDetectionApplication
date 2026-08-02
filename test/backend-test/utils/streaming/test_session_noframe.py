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
"""Property-based tests for ``StreamSession`` no-frame-yet handling.

Feature: concurrent-camera-stream-viewing, Property 12: No-frame-yet handling —
for any session that has started but published no frame, ``read_latest`` returns
status ``NO_FRAME`` and the session remains active (not stopped).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import FrameStatus, SessionState, StreamConfig
from utils.streaming.session import StreamSession

# The non-terminal / "started" lifecycle states a session can occupy while it has
# come up but not yet published its first frame. STOPPED is the terminal state and
# is deliberately excluded: Property 12 asserts a started session is *not* moved to
# STOPPED merely by a no-frame read.
_STARTED_STATES = [
    SessionState.STARTING,
    SessionState.RUNNING,
    SessionState.STOPPING,
    SessionState.ERROR,
]

# Arbitrary injected "now" clock values spanning negative, zero, and large epochs.
_now_values = st.floats(
    allow_nan=False,
    allow_infinity=False,
    min_value=-1_000_000.0,
    max_value=1_000_000_000.0,
)


# Feature: concurrent-camera-stream-viewing, Property 12: No-frame-yet handling
# Validates: Requirements 2.6, 4.2
@settings(max_examples=25)
@given(
    state=st.sampled_from(_STARTED_STATES),
    stale_after_s=st.floats(
        allow_nan=False, allow_infinity=False, min_value=0.0, max_value=120.0
    ),
    camera_id=st.text(min_size=1, max_size=32),
    nows=st.lists(_now_values, min_size=1, max_size=5),
)
def test_property_12_no_frame_yet_handling(state, stale_after_s, camera_id, nows):
    """A started session with no published frame reads NO_FRAME and stays active.

    For any started (non-terminal) session state and any injected ``now``, a
    session that has published no frame yet returns ``FrameStatus.NO_FRAME`` with
    no frame payload, and the read never advances the session to ``STOPPED`` nor
    otherwise mutates its lifecycle state.

    Feature: concurrent-camera-stream-viewing, Property 12: No-frame-yet handling
    Validates: Requirements 2.6, 4.2
    """
    config = StreamConfig(stale_after_s=stale_after_s)
    session = StreamSession(
        camera_id=camera_id,
        backend=None,
        stream_config=config,
        state=state,
    )

    # Precondition: the session has started but published no frame.
    assert session.latest is None
    assert session.state != SessionState.STOPPED

    # Reading repeatedly with arbitrary clocks must keep yielding NO_FRAME and must
    # never move the session out of its started state into STOPPED.
    for now in nows:
        result = session.read_latest(now)

        # No frame is available, so the status is NO_FRAME with no payload.
        assert result.status == FrameStatus.NO_FRAME
        assert result.frame is None

        # The read is non-destructive: the slot stays empty and the session stays
        # in its original active state (in particular, it is never stopped).
        assert session.latest is None
        assert session.state == state
        assert session.state != SessionState.STOPPED
