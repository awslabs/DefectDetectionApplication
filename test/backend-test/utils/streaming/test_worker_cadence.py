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
"""Property-based test for producer-cadence independence.

Feature: concurrent-camera-stream-viewing, Property 5: Producer cadence is
independent of viewer count — for any fixed sequence of frames produced by the
backend, the sequence of frames published into the latest-frame slot is
identical whether 1 or ``max_viewers`` viewers are reading arbitrarily, and no
viewer read prevents or delays a publish (drop-to-latest, no backpressure)
(Req 4.5).

The acquisition worker is the single producer driving one ``StreamSession``. We
script the mock ``CameraBackend`` with a fixed, byte-distinguishable frame
sequence and drive ``AcquisitionWorker.run()`` deterministically with an
injected ``MockClock`` (used as both the time source and the sleep function, so
no real threads or wall-clock time are involved). The loop terminates on its
own once the scripted queue is exhausted (the trailing ``None`` grab is treated
as a disconnect), so exactly ``len(frames)`` publishes occur.

Arbitrary, interleaved viewer reads are modelled deterministically by hooking
the session's ``publish`` and issuing a generated number of ``read_latest``
calls (per viewer) in each inter-publish interval. The test then asserts that
the captured sequence of published ``(seq, data, width, height)`` tuples is:

* identical for 1 viewer vs ``max_viewers`` viewers, and
* identical to the scripted input sequence (so reads neither drop, reorder, nor
  delay a publish).
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
from utils.streaming.models import SessionState, StreamConfig, Viewer
from utils.streaming.session import StreamSession
from utils.streaming.worker import AcquisitionWorker

# Per-camera viewer ceiling the property quantifies over (StreamConfig default).
MAX_VIEWERS = StreamConfig().max_viewers
CAMERA_ID = "Fake_1"


def _make_viewer(camera_id: str, now: float) -> Viewer:
    """Build a Viewer with a unique id for registration in a session."""
    return Viewer(
        viewer_id=str(uuid.uuid4()),
        camera_id=camera_id,
        subscribed_at=now,
        last_active=now,
    )


def _run_scenario(backend_cls, payloads, width, height, num_viewers, reads_per_interval):
    """Drive the worker over a fixed scripted sequence with interleaved reads.

    Builds a session served by a freshly-scripted mock backend, registers
    ``num_viewers`` viewers, hooks ``publish`` to (a) record every frame pushed
    into the latest-frame slot and (b) issue ``reads_per_interval[i]`` reads per
    viewer in interval ``i``, then runs the worker loop to natural completion.

    Returns the list of captured ``(seq, data, width, height)`` tuples in the
    exact order they were published.
    """
    # Deterministic virtual clock used as BOTH the worker's time source and its
    # sleep function, so the rate-ceiling sleep advances virtual time instead of
    # blocking and no real threads/time are involved.
    clock = MockClock(start=1000.0)

    scripted = [make_raw_frame(seq=i, width=width, height=height, data=p)
                for i, p in enumerate(payloads)]
    # default_grab defaults to None: once the scripted queue drains, the worker
    # sees a failed grab on an established stream and stops (DISCONNECTED), so
    # exactly len(scripted) publishes occur.
    backend = backend_cls(CAMERA_ID, frames=list(scripted), clock=clock)
    # The broadcaster owns open/start_stream in production; the worker only
    # grab->publishes. Acquire the single claim and start acquisition before
    # driving the loop so grab() serves the scripted frames.
    backend.open()
    backend.start_stream()

    session = StreamSession(
        CAMERA_ID,
        backend=backend,
        # Generous freshness window so interleaved reads stay OK; irrelevant to
        # what gets published, but keeps reads representative of live viewers.
        stream_config=StreamConfig(stale_after_s=10_000.0),
    )

    viewers = [_make_viewer(CAMERA_ID, clock.time()) for _ in range(num_viewers)]
    for viewer in viewers:
        session.add_viewer(viewer)
    assert session.viewer_count() == num_viewers

    published = []
    interval = {"i": 0}
    real_publish = session.publish
    total = len(scripted)

    worker = AcquisitionWorker(
        session,
        time_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    def recording_publish(frame, width=None, height=None, now=0.0):
        result = real_publish(frame, width=width, height=height, now=now)
        published.append((result.seq, bytes(result.data), result.width, result.height))
        # Interleave arbitrary viewer reads AFTER this publish and before the
        # next one. Reads are pure slot reads and must not affect what the
        # producer publishes next (no backpressure).
        idx = interval["i"]
        reads = reads_per_interval[idx] if idx < len(reads_per_interval) else 0
        for _ in range(reads):
            for viewer in viewers:
                session.read_latest(now)
        interval["i"] += 1
        # Stop the loop cleanly once the fixed scripted sequence is exhausted, so
        # the run is deterministic and bounded (no real threads / wall clock).
        if len(published) >= total:
            worker.stop()
        return result

    # Hook the single producer's publish path to capture + interleave reads.
    session.publish = recording_publish

    worker.run()

    return published


# Fixed scripted frame sequence: 1..10 byte-distinguishable frames.
_payloads = st.lists(st.binary(min_size=0, max_size=24), min_size=1, max_size=10)
# Arbitrary per-interval read counts (per viewer) modelling interleaved reads.
_reads = st.lists(st.integers(min_value=0, max_value=4), min_size=0, max_size=10)


# Feature: concurrent-camera-stream-viewing, Property 5: Producer cadence is independent of viewer count
# Validates: Requirements 4.5
@pytest.mark.parametrize("backend_cls", MOCK_BACKEND_CLASSES.values(), ids=list(MOCK_BACKEND_CLASSES))
@settings(max_examples=25)
@given(
    payloads=_payloads,
    width=st.integers(min_value=1, max_value=64),
    height=st.integers(min_value=1, max_value=64),
    reads_single=_reads,
    reads_multi=_reads,
)
def test_property_5_producer_cadence_independent_of_viewers(
    backend_cls, payloads, width, height, reads_single, reads_multi
):
    """Published frame sequence is identical for 1 vs max_viewers readers.

    Feature: concurrent-camera-stream-viewing, Property 5: Producer cadence is independent of viewer count
    Validates: Requirements 4.5
    """
    # The fixed input the producer should publish verbatim, regardless of how
    # many viewers read or how often they read (seq is the monotonic 1-based
    # publish index).
    expected = [(i + 1, bytes(p), width, height) for i, p in enumerate(payloads)]

    # One viewer reading arbitrarily.
    published_single = _run_scenario(
        backend_cls, payloads, width, height,
        num_viewers=1, reads_per_interval=reads_single,
    )

    # max_viewers all reading arbitrarily (more, differently-scheduled reads).
    published_multi = _run_scenario(
        backend_cls, payloads, width, height,
        num_viewers=MAX_VIEWERS, reads_per_interval=reads_multi,
    )

    # No read prevented or delayed a publish: every scripted frame was published
    # exactly once, in order, with identical bytes — for either viewer count.
    assert published_single == expected
    assert published_multi == expected

    # The producer cadence is therefore independent of viewer count and reads.
    assert published_single == published_multi
    assert len(published_single) == len(payloads)
    assert len(published_multi) == len(payloads)
