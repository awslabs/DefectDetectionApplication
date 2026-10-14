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
"""Property tests for ``StreamSession`` (rtsp-rtmp-stream-cameras task 16.6),
with a fake worker and an injected clock.

**Feature: rtsp-rtmp-stream-cameras, Property 18: Session backoff schedule**
**Feature: rtsp-rtmp-stream-cameras, Property 19: Latest-frame monotonicity and bounded buffering**
**Validates: Requirements 8.3, 8.5, 8.6**
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hypothesis import given, settings, strategies as st  # noqa: E402

from stream_fakes import FakeReader, make_session  # noqa: E402
from stream_ingest import health  # noqa: E402
from stream_ingest.session import (  # noqa: E402
    BACKOFF_RESET_AFTER_S,
    CONFIGURATION_RETRY_S,
    TRANSIENT_DELAYS_S,
    Backoff,
)

transient = st.sampled_from(health.TRANSIENT_CATEGORIES)
configuration = st.sampled_from(health.CONFIGURATION_CATEGORIES)
streamed = st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=59.9),
                     st.floats(min_value=60.0, max_value=600.0))

events = st.lists(st.one_of(
    st.tuples(st.just("transient"), transient, streamed),
    st.tuples(st.just("configuration"), configuration, streamed),
    st.tuples(st.just("change"), st.just(None), st.just(0.0)),
), min_size=1, max_size=25)


def model_schedule(sequence, session_history=False):
    """Property 18, written independently: ``(delay, configuration class)``
    after each event.

    With ``session_history`` (a StreamSession, which knows whether it has
    streamed under its current configuration), ``not_found`` after the
    session streamed takes the transient ladder (Requirement 8.6, owner
    decision 2026-09-29). The bare Backoff classifies by category only."""
    attempt = 0
    has_streamed = False
    schedule = []
    for kind, category, streamed_for in sequence:
        if kind == "change":
            attempt = 0
            has_streamed = False
            schedule.append((0.0, False))
            continue
        if streamed_for > 0:
            has_streamed = True
        if streamed_for >= 60.0:
            attempt = 0
        configuration_class = kind == "configuration" and not (
            session_history and has_streamed and category == health.NOT_FOUND)
        if configuration_class:
            schedule.append((300.0, True))
        else:
            schedule.append(((1.0, 2.0, 4.0, 8.0, 16.0, 30.0)[min(attempt, 5)], False))
            attempt += 1
    return schedule


def model_delays(sequence):
    return [delay for delay, _configuration in model_schedule(sequence)]


class TestProperty18BackoffSchedule:
    def test_the_constants_are_the_requirement(self):
        assert TRANSIENT_DELAYS_S == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
        assert CONFIGURATION_RETRY_S == 300.0
        assert BACKOFF_RESET_AFTER_S == 60.0

    @settings(max_examples=25, deadline=None)
    @given(sequence=events)
    def test_the_backoff_follows_the_schedule(self, sequence):
        backoff = Backoff()
        delays = []
        for kind, category, streamed_for in sequence:
            if kind == "change":
                backoff.reset()
                delays.append(0.0)
            else:
                delays.append(backoff.delay(category, streamed_for))
        assert delays == model_delays(sequence)

    @settings(max_examples=25, deadline=None)
    @given(sequence=events)
    def test_the_session_retries_exactly_on_schedule(self, sequence):
        """Through a StreamSession: each failure schedules the next worker
        start at the model delay, never earlier."""
        session, spawner, clock, _timer = make_session()
        session.start()
        expected = model_schedule(sequence, session_history=True)
        for (kind, category, streamed_for), (delay, configuration_class) in zip(sequence, expected):
            worker = spawner.current
            assert worker is not None and not worker.killed
            if streamed_for > 0:
                worker.stream()
                worker.produce()
                # Keep the worker alive (and its frames fresh) while it
                # streams, as a real one would.
                remaining = streamed_for
                while remaining > 0:
                    step = min(1.0, remaining)
                    clock.advance(step)
                    remaining -= step
                    worker.produce()
                    worker.health("streaming")
                    session.tick()
            workers_before = len(spawner.workers)
            if kind == "change":
                session.restart("configuration changed")
                assert len(spawner.workers) == workers_before + 1, "a change restarts at once"
                continue
            failed_at = clock.now
            if kind == "configuration" or category != health.WORKER_EXIT:
                worker.error(category, "boom")
            else:
                worker.exit(1)
            assert session.state == (health.FAILED if configuration_class else health.RECONNECTING)
            assert session.health()["lastError"]["category"] == category
            if delay > 0:
                clock.now = failed_at + delay - 0.01
                session.tick()
                assert len(spawner.workers) == workers_before, "no retry before the delay"
            # Set, not accumulated, so float rounding cannot land short of it.
            clock.now = failed_at + delay
            session.tick()
            assert len(spawner.workers) == workers_before + 1, "the retry starts at the delay"

    def test_configuration_failures_wait_until_the_configuration_changes(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        spawner.current.error(health.AUTHENTICATION_FAILED, "401 Unauthorized")
        for _ in range(9):
            clock.advance(30.0)
            session.tick()
        assert len(spawner.workers) == 1
        session.restart("credentials changed")
        assert len(spawner.workers) == 2
        assert session.state == health.CONNECTING


class TestNotFoundAfterStreaming:
    """Requirement 8.6's exception (owner decision 2026-09-29, found on
    hardware): once the path has streamed, a 404 is a publisher outage at a
    relay server or NVR, retried on the transient ladder."""

    @staticmethod
    def _streamed_session():
        session, spawner, clock, _timer = make_session()
        session.start()
        spawner.current.stream()
        spawner.current.produce()
        assert session.state == health.STREAMING
        return session, spawner, clock

    @staticmethod
    def _retry_delay(session, spawner, clock):
        workers, failed_at = len(spawner.workers), clock.now
        for step in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 300.0):
            clock.now = failed_at + step
            session.tick()
            if len(spawner.workers) > workers:
                return step
        return None

    def test_a_path_that_never_streamed_waits_the_configuration_retry(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        spawner.current.error(health.NOT_FOUND, "Not Found (404)")
        assert session.state == health.FAILED
        assert session.health()["nextAttemptInS"] == 300.0
        assert self._retry_delay(session, spawner, clock) == 300.0

    def test_a_path_that_streamed_is_retried_like_a_transient_failure(self):
        session, spawner, clock = self._streamed_session()
        spawner.current.error(health.NOT_FOUND, "Not Found (404)")
        assert session.state == health.RECONNECTING
        assert session.health()["lastError"]["category"] == health.NOT_FOUND
        assert self._retry_delay(session, spawner, clock) == 1.0
        # Still gone: the ladder continues, never the 5-minute wait.
        spawner.current.error(health.NOT_FOUND, "Not Found (404)")
        assert session.state == health.RECONNECTING
        assert self._retry_delay(session, spawner, clock) == 2.0

    def test_other_configuration_failures_keep_their_retry_after_streaming(self):
        session, spawner, clock = self._streamed_session()
        spawner.current.error(health.AUTHENTICATION_FAILED, "401 Unauthorized")
        assert session.state == health.FAILED
        assert self._retry_delay(session, spawner, clock) == 300.0

    def test_a_configuration_change_forgets_that_the_path_streamed(self):
        session, spawner, clock = self._streamed_session()
        session.restart("configuration changed")
        spawner.current.error(health.NOT_FOUND, "Not Found (404)")
        assert session.state == health.FAILED
        assert self._retry_delay(session, spawner, clock) == 300.0

    def test_a_connection_test_restart_remembers_that_the_path_streamed(self):
        session, spawner, clock = self._streamed_session()
        session.restart("connection test", configuration_changed=False)
        spawner.current.error(health.NOT_FOUND, "Not Found (404)")
        assert session.state == health.RECONNECTING
        assert self._retry_delay(session, spawner, clock) == 1.0


def _operations():
    return st.lists(st.one_of(
        st.tuples(st.just("produce"), st.integers(min_value=1, max_value=4)),
        st.tuples(st.just("consume"), st.integers(min_value=0, max_value=2)),
        st.tuples(st.just("reconnect"), st.just(0)),
    ), min_size=1, max_size=40)


class TestProperty19LatestFrame:
    @settings(max_examples=25, deadline=None)
    @given(operations=_operations())
    def test_consumers_see_strictly_newer_latest_frames_and_buffers_stay_bounded(self, operations):
        observed_buffers = []
        holder = {}

        def on_read(_header):
            # A copy is in flight: the cached frame plus this one at most.
            observed_buffers.append(holder["session"].buffers_held())

        session, spawner, clock, _timer = make_session(reader=FakeReader(on_read))
        holder["session"] = session
        session.start()
        spawner.current.stream()
        last_seen = {0: 0, 1: 0, 2: 0}
        unseen = {0: False, 1: False, 2: False}
        for kind, argument in operations:
            worker = spawner.current
            if kind == "produce":
                worker.produce(argument)
                worker.health("streaming")
                unseen = {consumer: True for consumer in unseen}
            elif kind == "consume":
                newest_worker_seq = worker.frames[-1][0] if worker.frames else 0
                frame = session.latest_frame(after_seq=last_seen[argument])
                if unseen[argument]:
                    assert frame is not None, "a frame produced since the last request is served"
                if frame is not None:
                    assert frame.seq > last_seen[argument], "sequence numbers increase strictly"
                    # The newest frame is served, never an older one.
                    if newest_worker_seq:
                        assert frame.seq >= session._seq_offset + newest_worker_seq
                    last_seen[argument] = frame.seq
                    unseen[argument] = False
            else:
                worker.exit(1)
                clock.advance(30.0)
                session.tick()
                spawner.current.stream()
                # A frame the old worker held and nobody fetched died with it.
                unseen = {consumer: False for consumer in unseen}
            assert session.buffers_held() <= 2
        assert all(count <= 2 for count in observed_buffers)

    def test_a_slow_consumer_gets_the_newest_frame_and_nothing_queues(self):
        session, spawner, _clock, _timer = make_session()
        session.start()
        worker = spawner.current
        worker.stream()
        worker.produce(50)
        frame = session.latest_frame()
        assert frame.seq == 50
        assert session.buffers_held() == 1
        assert len(worker.frames) == 1

    def test_sequence_numbers_keep_increasing_across_worker_restarts(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        spawner.current.stream()
        spawner.current.produce(7)
        assert session.latest_frame().seq == 7
        spawner.current.exit(1)
        clock.advance(1.0)
        session.tick()
        spawner.current.stream()
        spawner.current.produce(1)
        frame = session.latest_frame(after_seq=7)
        assert frame is not None and frame.seq == 8

    def test_a_stale_frame_is_not_served_when_a_fresh_one_is_required(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        spawner.current.stream()
        spawner.current.produce(1, acquired_at_ms=int(clock.wall() * 1000) - 5000)
        assert session.latest_frame(max_age_ms=2000) is None
        assert session.latest_frame().seq == 1
