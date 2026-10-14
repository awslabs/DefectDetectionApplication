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
"""Property-based tests for independent, no-re-grab latest-frame reads.

Feature: concurrent-camera-stream-viewing, Property 4: Reads are independent of
other viewers and do not re-grab — for any viewer and any number of other
viewers in any read state, a viewer's ``read_latest`` returns the current latest
frame as a pure function of the slot, independent of the presence / count /
pending reads of other viewers; repeated reads with no intervening publish
return the same frame without invoking a device grab.

Validates: Requirements 1.5, 2.4
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import FrameStatus, StreamConfig, Viewer
from utils.streaming.session import StreamSession

from mock_camera_backend import (
    BACKEND_KINDS,
    MockClock,
    make_backend,
    make_raw_frame,
)


def _build_published_session(backend_kind, clock):
    """Create a started session backed by a mock backend with one published frame.

    Mirrors what the acquisition worker does for a single interval: open the
    claim, start the stream, perform exactly one device ``grab``, and publish the
    resulting frame into the latest-frame slot. Returns the session, the backend,
    the published frame, and the backend ``grab_count`` observed immediately after
    the publish so reads can be asserted to never increase it.
    """
    backend = make_backend(backend_kind, camera_id="Fake_1", clock=clock)
    backend.queue_frame(make_raw_frame(seq=7, width=8, height=8))
    backend.open()
    backend.start_stream()

    raw = backend.grab(timeout_ms=backend.last_grab_timeout_ms or 5000)
    grabs_after_publish = backend.grab_count  # exactly one device grab so far

    session = StreamSession("Fake_1", backend=backend, stream_config=StreamConfig())
    published = session.publish(raw, now=clock.time())

    return session, backend, published, grabs_after_publish


def _set_viewer_count(session, count, clock):
    """Add/remove viewers so the registry holds exactly ``count`` viewers.

    Models "any number of other viewers in any read state" — the presence and
    count of viewers must not influence what ``read_latest`` returns.
    """
    current = session.viewer_count()
    while session.viewer_count() < count:
        idx = session.viewer_count()
        session.add_viewer(
            Viewer(
                viewer_id=f"viewer-{idx}",
                camera_id=session.camera_id,
                subscribed_at=clock.time(),
                last_active=clock.time(),
            )
        )
    # Remove from the end if we overshot a previous, larger count.
    while session.viewer_count() > count:
        last_id = f"viewer-{session.viewer_count() - 1}"
        session.remove_viewer(last_id)
    return current


# Feature: concurrent-camera-stream-viewing, Property 4: Reads are independent of other viewers and do not re-grab
# Validates: Requirements 1.5, 2.4
@settings(max_examples=25, deadline=None)
@given(
    backend_kind=st.sampled_from(BACKEND_KINDS),
    # Each entry is (viewer_count_before_read, now_at_read). The viewer count
    # varies the presence/count of other viewers; now varies the read timing.
    reads=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=8),
            st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=2,
        max_size=25,
    ),
)
def test_property_4_reads_independent_and_no_regrab(backend_kind, reads):
    """read_latest is a pure function of the slot; reads never re-grab.

    Feature: concurrent-camera-stream-viewing, Property 4: Reads are independent
    of other viewers and do not re-grab
    Validates: Requirements 1.5, 2.4
    """
    clock = MockClock(start=0.0)
    session, backend, published, grabs_after_publish = _build_published_session(
        backend_kind, clock
    )

    # Baseline read with zero other viewers present. Every later read — under any
    # viewer count and any read order — must return this identical frame.
    baseline = session.read_latest(now=clock.time())
    assert baseline.frame is not None
    assert baseline.frame.seq == published.seq
    assert baseline.frame.data == published.data

    for viewer_count, now in reads:
        # Vary the presence/count of "other viewers" before this read. No publish
        # occurs anywhere in this loop, so the latest-frame slot is unchanged.
        _set_viewer_count(session, viewer_count, clock)

        result = session.read_latest(now=now)

        # Independent of viewer presence/count: the same latest frame is returned,
        # byte-identical and with the same monotonic seq, as a pure function of
        # the slot (Req 1.5, 2.4).
        assert result.frame is not None
        assert result.frame.seq == published.seq
        assert result.frame.data == published.data
        assert result.frame.width == published.width
        assert result.frame.height == published.height
        # Status is never NO_FRAME (a frame has been published) and never
        # DISCONNECTED (read_latest only classifies freshness).
        assert result.status in (FrameStatus.OK, FrameStatus.STALE)

        # Reads NEVER trigger a device grab: the backend grab count is unchanged
        # from the single acquisition that produced the published frame.
        assert backend.grab_count == grabs_after_publish

    # After all reads, no extra device grabs were issued by reading (Req 2.4),
    # and the backend never performed more than the one acquisition grab.
    assert backend.grab_count == grabs_after_publish == 1


# Feature: concurrent-camera-stream-viewing, Property 4: Reads are independent of other viewers and do not re-grab
# Validates: Requirements 1.5, 2.4
@settings(max_examples=25, deadline=None)
@given(
    backend_kind=st.sampled_from(BACKEND_KINDS),
    repeat=st.integers(min_value=1, max_value=50),
    now=st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False),
)
def test_property_4_repeated_reads_return_same_frame_without_grab(backend_kind, repeat, now):
    """Repeated reads with no intervening publish return the same frame, no grab.

    Feature: concurrent-camera-stream-viewing, Property 4: Reads are independent
    of other viewers and do not re-grab
    Validates: Requirements 1.5, 2.4
    """
    clock = MockClock(start=0.0)
    session, backend, published, grabs_after_publish = _build_published_session(
        backend_kind, clock
    )

    seen = [session.read_latest(now=now) for _ in range(repeat)]

    # Every repeated read returns the byte-identical, same-seq frame because no
    # publish intervened (pure function of the slot, Req 2.4).
    for result in seen:
        assert result.frame is not None
        assert result.frame.seq == published.seq
        assert result.frame.data == published.data

    # None of the repeated reads invoked a device grab.
    assert backend.grab_count == grabs_after_publish == 1
