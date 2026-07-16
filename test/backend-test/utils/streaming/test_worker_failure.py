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
"""Property-based tests for acquisition-failure last-good-frame retention.

Feature: concurrent-camera-stream-viewing, Property 13: Acquisition failure
preserves the last good frame — for any published latest frame followed by a
failed grab, the latest-frame slot still equals the last successfully published
frame (the failed grab does not overwrite it) and an acquisition-failure
indication is produced (the session transitions to ERROR and the worker stops
with reason DISCONNECTED).

Validates: Requirements 2.7
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.streaming.models import SessionState, StreamConfig
from utils.streaming.session import StreamSession
from utils.streaming.worker import AcquisitionWorker, WorkerStopReason

from mock_camera_backend import (
    BACKEND_KINDS,
    MockClock,
    make_backend,
    make_raw_frame,
)


def _build_running_session(backend_kind, good_frames, clock):
    """Create a session + worker scripted with ``good_frames`` then a failed grab.

    Opens the device claim and starts the stream (the worker drives only
    ``grab`` / ``publish``, never ``open``/``start_stream``), queues the supplied
    good frames, and leaves the backend's exhausted-queue default as ``None`` so
    the grab immediately after the last good frame is a failed grab (timeout /
    acquisition failure) on an already-established stream.

    Returns the session, backend, worker, and the list of queued good frames.
    """
    backend = make_backend(backend_kind, camera_id="Fake_1", clock=clock)
    # The worker reads grab() directly; the claim must be open so grab() does not
    # short-circuit to None. Establish the stream first.
    backend.open()
    backend.start_stream()
    backend.queue_frames(good_frames)
    # default_grab is None -> the grab after the queue drains is a failed grab.

    session = StreamSession("Fake_1", backend=backend, stream_config=StreamConfig())
    worker = AcquisitionWorker(
        session,
        time_fn=clock,
        sleep_fn=clock.sleep,
    )
    return session, backend, worker, good_frames


# Feature: concurrent-camera-stream-viewing, Property 13: Acquisition failure preserves the last good frame
# Validates: Requirements 2.7
@settings(max_examples=25, deadline=None)
@given(
    backend_kind=st.sampled_from(BACKEND_KINDS),
    # Arbitrary good-frame payloads (>= 1 establishes the stream and a last-good
    # latest-frame). Distinct, byte-distinguishable payloads so "not overwritten"
    # is verifiable down to the exact bytes of the last good frame.
    payloads=st.lists(
        st.binary(min_size=1, max_size=32),
        min_size=1,
        max_size=10,
    ),
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
)
def test_property_13_failed_grab_preserves_last_good_frame(backend_kind, payloads, width, height):
    """A failed grab keeps the last good frame and yields a DISCONNECTED stop.

    Feature: concurrent-camera-stream-viewing, Property 13: Acquisition failure
    preserves the last good frame
    Validates: Requirements 2.7
    """
    clock = MockClock(start=0.0)
    good_frames = [
        make_raw_frame(seq=i, width=width, height=height, data=payload)
        for i, payload in enumerate(payloads)
    ]
    session, backend, worker, good_frames = _build_running_session(
        backend_kind, good_frames, clock
    )

    last_good = good_frames[-1]

    # Drive the grab -> publish loop. It publishes every good frame, then hits the
    # failed grab (None) on the established stream and returns.
    reason = worker.run()

    # --- acquisition-failure indication is produced (Req 2.7) ----------------
    assert reason == WorkerStopReason.DISCONNECTED
    assert worker.stop_reason == WorkerStopReason.DISCONNECTED
    assert session.state == SessionState.ERROR
    assert worker.error is not None and "disconnect" in worker.error.lower()

    # The backend was grabbed exactly once per good frame plus the one failed grab.
    assert backend.grab_count == len(good_frames) + 1

    # --- the failed grab did NOT overwrite the latest-frame slot (Req 2.7) ----
    assert session.latest is not None
    # seq is monotonic and assigned only on publish: equals the count of good
    # frames, proving the failed grab did not publish a new frame.
    assert session.latest.seq == len(good_frames)
    # The slot still holds the last successfully published frame, byte-identical.
    assert session.latest.data == last_good.data
    assert session.latest.width == last_good.width
    assert session.latest.height == last_good.height

    # A subsequent read returns that same retained last-good frame (it was not
    # cleared or overwritten by the failure).
    assert session.read_latest(now=clock.time()).frame is not None
    assert session.read_latest(now=clock.time()).frame.seq == len(good_frames)
    assert session.read_latest(now=clock.time()).frame.data == last_good.data


# Feature: concurrent-camera-stream-viewing, Property 13: Acquisition failure preserves the last good frame
# Validates: Requirements 2.7
def test_property_13_single_good_frame_then_failure_example():
    """Concrete example: one good frame, then a failed grab retains it.

    Feature: concurrent-camera-stream-viewing, Property 13: Acquisition failure
    preserves the last good frame
    Validates: Requirements 2.7
    """
    clock = MockClock(start=0.0)
    good = make_raw_frame(seq=0, width=8, height=8, data=b"good-frame")
    session, backend, worker, _ = _build_running_session("aravis", [good], clock)

    reason = worker.run()

    assert reason == WorkerStopReason.DISCONNECTED
    assert session.state == SessionState.ERROR
    # One good publish (seq 1) retained after the failed grab; not overwritten.
    assert session.latest is not None
    assert session.latest.seq == 1
    assert session.latest.data == b"good-frame"
    assert backend.grab_count == 2  # one good grab + one failed grab
