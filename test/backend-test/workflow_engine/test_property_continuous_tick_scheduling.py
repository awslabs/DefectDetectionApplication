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
"""Property test for continuous tick scheduling.

**Feature: rtsp-rtmp-stream-cameras, Property 21: Continuous tick
scheduling**

*For any* sequence of tick times, run durations, frame arrivals, and
streaming states:

- at most one run is in progress at a time;
- each frame sequence number starts at most one run;
- every tick that elapses during a run is counted as skipped and never
  started later;
- no run starts while the session is not streaming;
- each outage records exactly one stream-unavailable event.

**Validates: Requirements 11.2, 11.3, 11.5**

The runner is driven by an injected clock: ``step()`` makes one decision
and returns the delay to the next, and the fake executor advances the
clock by the run's duration, so the simulation is exact. Sampling periods
are powers of two and run durations are quarter periods, so every tick
time is exact in binary floating point; a run may end exactly on a tick,
which then counts as elapsed during the run. Outages last longer than the
longest run plus a period, so the runner observes every one. Frames
arrive while streaming at a source rate of their own, with sequence
numbers that keep rising across outages (the session's are), and like a
real session the fake returns a fresh cached frame even during an outage.
"""
import itertools
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine.continuous_runner import IDLE_POLL_S, ContinuousRunner
from workflow_engine.stream_feed import FrameHandoff, StreamFeed

_FPS = st.sampled_from([0.5, 1.0, 2.0, 4.0, 8.0])
_SOURCE_FPS = st.sampled_from([2.0, 5.0, 10.0, 25.0])


class SimClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Frame:
    def __init__(self, seq, acquired_at_ms):
        self.seq = seq
        self.acquired_at_ms = acquired_at_ms


class SimStream:
    """Streaming segments over the clock, with frames at the source rate
    while streaming."""

    def __init__(self, clock, segments, source_fps):
        self.clock = clock
        self.segments = segments  # [(start, end, streaming)]
        self.frames = []
        seq = itertools.count(1)
        for start, end, streaming in segments:
            if streaming:
                t = start + 1.0 / source_fps
                while t < end:
                    self.frames.append((t, next(seq)))
                    t += 1.0 / source_fps
        self.health_checks = []

    def streaming_at(self, t):
        for start, end, streaming in self.segments:
            if start <= t < end:
                return streaming
        return self.segments[-1][2]

    def health(self, camera_key):
        state = "streaming" if self.streaming_at(self.clock.now) else "reconnecting"
        return {"state": state}

    def latest_frame(self, camera_key, after_seq=0, max_age_ms=None, wait_ms=0):
        # Like a real session, a fresh cached frame is returned even while
        # the camera is not streaming: only the runner's health check keeps
        # a run from starting on it.
        newest = None
        for arrived, seq in self.frames:
            if arrived > self.clock.now:
                break
            newest = (arrived, seq)
        if newest is None or newest[1] <= after_seq:
            return None
        return Frame(newest[1], int(newest[0] * 1000))


class SimExecutor:
    def __init__(self, clock, durations):
        self.clock = clock
        self.durations = itertools.cycle(durations)
        self.in_run = False
        self.runs = []  # (execution_id, start, end)

    def __call__(self, execution_id):
        assert not self.in_run, "two runs in progress at once"
        self.in_run = True
        start = self.clock.now
        self.clock.advance(next(self.durations))
        self.runs.append((execution_id, start, self.clock.now))
        self.in_run = False


class SimStore:
    def __init__(self):
        self.contexts = {}
        self.ids = itertools.count(1)

    def insert(self, registration_id, context):
        execution_id = "exec-{0}".format(next(self.ids))
        self.contexts[execution_id] = dict(context)
        return execution_id

    def status(self, execution_id):
        return "completed"


@st.composite
def _scenarios(draw):
    fps = draw(_FPS)
    period = 1.0 / fps
    # Run durations of (k + f) periods: between ticks (f in {1/4, 1/2,
    # 3/4}) or ending exactly on one (f = 0, k >= 1), where the tick at
    # the moment the run ends counts as elapsed during it.
    durations = []
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        fraction = draw(st.sampled_from([0.0, 0.25, 0.5, 0.75]))
        whole = draw(st.integers(min_value=1 if fraction == 0.0 else 0, max_value=3))
        durations.append((whole + fraction) * period)
    longest = max(durations)
    minimum_outage = longest + period + IDLE_POLL_S + 1.0
    segments = []
    t = 0.0
    streaming = draw(st.booleans())
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        if streaming:
            length = draw(st.sampled_from([2.0, 5.0, 10.0, 20.0]))
        else:
            length = minimum_outage + draw(st.sampled_from([0.0, 1.5, 4.0]))
        segments.append((t, t + length, streaming))
        t += length
        streaming = not streaming
    # End on a long streaming segment so every outage completes.
    segments.append((t, t + 30.0, True))
    return fps, durations, segments, draw(_SOURCE_FPS)


def _simulate(fps, durations, segments, source_fps):
    clock = SimClock()
    stream = SimStream(clock, segments, source_fps)
    executor = SimExecutor(clock, durations)
    store = SimStore()
    feed = StreamFeed(node_id="cam", protocol="rtsp", camera_key="cfg-7", url="rtsp://10.0.0.7/main",
                      processing_mode="continuous", frames_per_second=fps, max_frame_age_ms=60000,
                      keep_recent_runs=20, keep_notable_runs=200, camera_source_id="cfg-7")
    runner = ContinuousRunner("wf-1:3", feed, execute=executor, stream_manager=stream, store=store,
                              handoff=FrameHandoff(), clock=clock, wall=lambda: 1_790_000_000 + clock.now)
    horizon = segments[-1][1]
    steps = 0
    while clock.now < horizon:
        delay = runner.step()
        assert delay is not None and delay > 0
        clock.advance(delay)
        steps += 1
        assert steps < 200_000
    return runner, stream, executor, store


@settings(deadline=None)
@given(scenario=_scenarios())
def test_continuous_tick_scheduling(scenario):
    """**Feature: rtsp-rtmp-stream-cameras, Property 21: Continuous tick
    scheduling**

    **Validates: Requirements 11.2, 11.3, 11.5**
    """
    fps, durations, segments, source_fps = scenario
    period = 1.0 / fps
    runner, stream, executor, store = _simulate(fps, durations, segments, source_fps)
    counters = runner.counters()
    runs = executor.runs

    # At most one run at a time (asserted inside the executor), and they
    # never overlap.
    for (_, _, previous_end), (_, start, _) in zip(runs, runs[1:]):
        # Never started later: no run starts at the moment the previous one
        # ended; the next tick after the end comes first.
        assert start > previous_end

    # Each frame sequence number starts at most one run, in rising order,
    # and each run analyzes the newest frame at its tick.
    seqs = [store.contexts[execution_id]["frameSeq"] for execution_id, _, _ in runs]
    assert len(seqs) == len(set(seqs))
    assert seqs == sorted(seqs)
    for execution_id, start, _ in runs:
        newest = max(seq for arrived, seq in stream.frames if arrived <= start)
        assert store.contexts[execution_id]["frameSeq"] == newest

    # No run starts while the session is not streaming.
    for _, start, _ in runs:
        assert stream.streaming_at(start)

    # Every tick that elapsed during a run is counted as skipped busy.
    expected_busy = sum(math.floor((end - start) / period) for _, start, end in runs)
    assert counters["skippedBusy"] == expected_busy

    # One stream-unavailable event per outage (the first segment counts
    # when the camera starts out not streaming).
    outages = sum(1 for _start, _end, streaming in segments if not streaming)
    assert counters["streamUnavailable"] == outages

    # Counters agree with what happened.
    assert counters["started"] == len(runs) == counters["completed"]
    assert counters["failed"] == 0

    # Runs begin within 10 s of the session (re)reaching streaming
    # (Requirement 11.1).
    for index, (start, end, streaming) in enumerate(segments):
        restarted = index == 0 or not segments[index - 1][2]
        if streaming and restarted and end - start >= 10.0:
            assert any(start <= run_start <= start + 10.0 for _, run_start, _ in runs)
