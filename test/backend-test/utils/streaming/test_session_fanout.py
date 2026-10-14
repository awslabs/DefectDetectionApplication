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
"""Property-based test for stream fan-out.

Feature: concurrent-camera-stream-viewing, Property 3: Fan-out delivers the
identical latest frame to every viewer — for any set of 1..max_viewers viewers
and any sequence of published frames, every viewer reading after a publish (and
before the next publish) receives a byte-identical frame carrying the same seq
as every other viewer reading in that interval, and that frame is the most
recently published one (Req 1.1, 1.4, 2.2, 2.5).
"""
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mock_camera_backend import (
    MOCK_BACKEND_CLASSES,
    MockClock,
    make_raw_frame,
)
from utils.streaming.models import FrameStatus, StreamConfig, Viewer
from utils.streaming.session import StreamSession

# Per-camera viewer ceiling the property quantifies over (StreamConfig default).
MAX_VIEWERS = StreamConfig().max_viewers


def _make_viewer(camera_id: str, now: float) -> Viewer:
    """Build a Viewer with a unique id for registration in a session."""
    return Viewer(
        viewer_id=str(uuid.uuid4()),
        camera_id=camera_id,
        subscribed_at=now,
        last_active=now,
    )


# A sequence of frames: vary count (1..12) and the raw bytes of each frame so the
# fan-out assertion is exercised over many distinct, byte-distinguishable frames.
_frame_payloads = st.lists(
    st.binary(min_size=0, max_size=32),
    min_size=1,
    max_size=12,
)


# Feature: concurrent-camera-stream-viewing, Property 3: Fan-out delivers the identical latest frame to every viewer
# Validates: Requirements 1.1, 1.4, 2.2, 2.5
@pytest.mark.parametrize("backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES))
@settings(max_examples=25)
@given(
    payloads=_frame_payloads,
    num_viewers=st.integers(min_value=1, max_value=MAX_VIEWERS),
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
)
def test_property_3_fanout_identical_latest_frame(
    backend_cls, payloads, num_viewers, width, height
):
    """Every viewer reading in an interval gets the same-seq, byte-identical, most-recent frame.

    Feature: concurrent-camera-stream-viewing, Property 3: Fan-out delivers the identical latest frame to every viewer
    Validates: Requirements 1.1, 1.4, 2.2, 2.5
    """
    camera_id = "Fake_1"
    clock = MockClock(start=1000.0)
    backend = backend_cls(camera_id, clock=clock)
    # Generous freshness window so the injected clock keeps every read OK.
    session = StreamSession(
        camera_id,
        backend=backend,
        stream_config=StreamConfig(stale_after_s=10_000.0),
    )

    # Register 1..max_viewers distinct viewers; all share the one latest-frame slot.
    for _ in range(num_viewers):
        session.add_viewer(_make_viewer(camera_id, clock.time()))
    assert session.viewer_count() == num_viewers

    for payload in payloads:
        # The producer publishes the next frame at the current clock time so the
        # frame is fresh (now == acquired_at) when read in this interval.
        now = clock.time()
        frame = make_raw_frame(width=width, height=height, data=payload)
        published = session.publish(frame, now=now)

        # Every viewer reads after the publish and before the next one. Collect
        # each viewer's result; they must agree with each other and the publish.
        results = [session.read_latest(now) for _ in range(num_viewers)]

        for result in results:
            # The frame is fresh, so the read classifies OK (Req 1.1, 1.4).
            assert result.status == FrameStatus.OK
            assert result.frame is not None
            # Same seq as the most recent publish (Req 2.5).
            assert result.frame.seq == published.seq
            # Byte-identical data equal to the most recent publish (Req 2.2).
            assert result.frame.data == payload
            assert result.frame.width == width
            assert result.frame.height == height
            # The served frame is the most recently published one.
            assert result.frame is published

        # All viewers observed exactly one identical seq in this interval.
        seqs = {r.frame.seq for r in results}
        datas = {bytes(r.frame.data) for r in results}
        assert len(seqs) == 1, "viewers disagreed on the latest seq"
        assert len(datas) == 1, "viewers received differing frame bytes"

        clock.advance(0.001)
