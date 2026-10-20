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
"""The Interval summarizer (workflow-telemetry-dashboards design §4.1, §5.2
``summarizer.py``, §5.4; tasks.md 1.1) with a fake clock and a fake
``RegistrationView``.

Interval alignment; the sub-second first window (a start 400 ms before a
boundary gives a first Summary spanning 10.4 s declared ``intervalS: 10``);
the Heartbeat window; a registration added with no run Heartbeats at the
next ``idleHeartbeatS`` boundary; a superseded registration emits its final
window and nothing after; a mid-Interval version change yields two
Summaries with consecutive ``seq``; a late observation for a dropped id
emits once; the 64-label cap and ``__other__``; maxima; ``total`` from
``detection_count`` with ``truncated`` for a 1,200-detection run; Heartbeats
after 5 idle minutes; ``seq`` continuity per workflow across Summaries and Heartbeats;
the size cap; ``model.waits == 1`` for one continuous wait polled three
times; ``waitedMs`` summed from gate outcomes; no latency sample for
``run_started_ms is None``; a refused run counts in ``runs.failed``;
``NONE`` without Triton; the enable-time reset (three records while
disabled, enable, one record -> ``runs.completed == 1``); disable discards
the window and ``seq`` continues.

Validates: R2.1-R2.5, R2.7-R2.9, R5.7, R19.4.
"""
import json

import pytest

from workflow_engine.telemetry import hooks
from workflow_engine.telemetry import summarizer as summarizer_module
from workflow_engine.telemetry.registry_view import RegistrationInfo
from workflow_engine.telemetry.summarizer import (
    MODEL_STATES, IntervalSummarizer, PairAccumulator, SummarizerSettings, normalize_state, worst_state,
)

THING = "thing-1"
INSTANCE = "7c1e7d4e-3b2a-4c9d-8e1f-0a2b3c4d5e6f"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_B = "1b2c3d4e-5f6a-4b7c-9d0e-1f2a3b4c5d6e"
REG_A = "reg-a:3"
REG_A4 = "reg-a:4"
REG_B = "reg-b:1"
#: A multiple of 300 s: aligned to the 10 s, 60 s and 300 s grids.
BASE = 1_792_075_200.0
BASE_MS = int(BASE * 1000)


class FakeClock:
    def __init__(self, now=BASE):
        self.now = float(now)

    def __call__(self):
        return self.now

    def set(self, seconds):
        self.now = float(seconds)

    def advance(self, seconds):
        self.now += float(seconds)


class FakeView:
    def __init__(self, rows=None):
        self.rows = dict(rows) if rows is not None else {REG_A: RegistrationInfo(WF_A, "3", (), False)}

    def snapshot(self):
        return dict(self.rows)


def ms(seconds):
    return int(round(seconds * 1000))


def run_record(registration_id=REG_A, workflow_id=WF_A, version="3", execution_id="exec-1", status="completed",
               finished_at=None, latency_ms=250, detections=None, detection_count=None, tag_values=None,
               run_started=True, error=None, failing_node_id=None):
    finished_ms = ms(finished_at) if finished_at is not None else BASE_MS + 5_000
    run_started_ms = finished_ms - latency_ms if run_started else None
    if detections is not None and detection_count is None:
        detection_count = len(detections)
    return hooks.RunRecord(
        registration_id=registration_id, workflow_id=workflow_id, workflow_version=version,
        execution_id=execution_id, status=status, error=error, failing_node_id=failing_node_id, traceback=None,
        capture_id="c", output_dir=None, log_path=None, enqueued_at_s=finished_ms // 1000 - 1,
        accepted_ms=finished_ms - latency_ms - 50, run_started_ms=run_started_ms, finished_ms=finished_ms,
        queued_ms=0, setup_ms=50 if run_started else None, readiness_wait_ms=0, trigger_context={},
        node_status=None, tag_values=tag_values, detections=detections,
        detection_count=detection_count if detection_count is not None else 0, continuous=False,
    )


def detections_of(*labels):
    return [{"id": "d%d" % i, "label": label, "confidence": 0.9, "x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1}
            for i, label in enumerate(labels)]


def make(clock, view=None, enabled=True, interval=10, heartbeat=60, **kwargs):
    return IntervalSummarizer(
        SummarizerSettings(enabled=enabled, summary_interval_s=interval, idle_heartbeat_s=heartbeat),
        clock, view if view is not None else FakeView(), device_id=THING, instance=INSTANCE,
        producer_version="1.0.0", **kwargs,
    )


def tick_at(summarizer, clock, seconds):
    clock.set(seconds)
    return summarizer.tick()


def payloads(emissions):
    return [e.payload for e in emissions]


# ---------------------------------------------------------------------------
# Alignment and windows
# ---------------------------------------------------------------------------


class TestAlignmentAndWindows:
    def test_first_summary_spans_from_start_to_the_next_wall_clock_boundary(self):
        clock = FakeClock(BASE + 3)
        s = make(clock)
        clock.set(BASE + 4)
        s.observe_run(run_record(finished_at=BASE + 4))
        assert tick_at(s, clock, BASE + 9.9) == []  # before the boundary: nothing
        out = payloads(tick_at(s, clock, BASE + 10.2))
        assert len(out) == 1
        summary = out[0]
        assert summary["schema"] == "dda.summary.v1"
        assert summary["intervalStart"] == BASE_MS + 3_000 and summary["intervalEnd"] == BASE_MS + 10_000
        assert summary["intervalEnd"] % 10_000 == 0
        assert summary["emittedAt"] == BASE_MS + 10_200 and summary["emittedAt"] >= summary["intervalEnd"]
        assert summary["intervalS"] == 10 and summary["idle"] is False
        assert summary["deviceId"] == THING and summary["workflowId"] == WF_A and summary["workflowVersion"] == "3"
        assert summary["producer"] == {"name": "engine", "version": "1.0.0", "instance": INSTANCE}
        assert summary["seq"] == 1
        assert summary["runs"] == {"completed": 1, "failed": 0, "dropped": 0}
        assert summary["latency"] == {"p50Ms": 250, "p95Ms": 250, "maxMs": 250, "n": 1}
        assert summary["model"] == {"state": "NONE", "models": {}, "waits": 0, "waitedMs": 0}
        assert summary["telemetry"] == {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}
        assert "detections" not in summary and "outcomes" not in summary and "continuous" not in summary
        json.dumps(summary)  # serializable
        # the next window is exactly one interval
        clock.set(BASE + 15)
        s.observe_run(run_record(execution_id="exec-2", finished_at=BASE + 15))
        out = payloads(tick_at(s, clock, BASE + 20.1))
        assert [(p["intervalStart"], p["intervalEnd"], p["seq"]) for p in out] == [
            (BASE_MS + 10_000, BASE_MS + 20_000, 2)]

    def test_a_start_400_ms_before_a_boundary_folds_into_the_next_one(self):
        clock = FakeClock(BASE + 9.6)
        s = make(clock)
        clock.set(BASE + 9.8)
        s.observe_run(run_record(finished_at=BASE + 9.8))
        assert tick_at(s, clock, BASE + 10.5) == []  # the 400 ms window is not emitted
        out = payloads(tick_at(s, clock, BASE + 20.3))
        assert len(out) == 1
        summary = out[0]
        assert summary["intervalStart"] == BASE_MS + 9_600 and summary["intervalEnd"] == BASE_MS + 20_000
        assert summary["intervalEnd"] - summary["intervalStart"] == 10_400
        assert summary["intervalS"] == 10
        assert summary["runs"]["completed"] == 1  # the run of the folded window is inside

    def test_heartbeat_window_is_on_the_idle_grid(self):
        clock = FakeClock(BASE + 5)
        s = make(clock)  # a registration with no run
        for boundary in (10, 20, 30, 40, 50):
            assert tick_at(s, clock, BASE + boundary + 0.1) == []
        out = payloads(tick_at(s, clock, BASE + 60.4))
        assert len(out) == 1
        heartbeat = out[0]
        assert heartbeat["idle"] is True and heartbeat["intervalS"] == 60
        assert heartbeat["intervalEnd"] == BASE_MS + 60_000 and heartbeat["intervalEnd"] % 60_000 == 0
        assert heartbeat["intervalStart"] == BASE_MS + 5_000  # the first one after start is shorter
        assert heartbeat["emittedAt"] == BASE_MS + 60_400
        for key in ("runs", "latency", "detections", "outcomes", "people", "continuous", "ext", "model"):
            assert key not in heartbeat
        assert heartbeat["telemetry"] == {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}
        assert heartbeat["seq"] == 1
        # the next Heartbeat spans the full idleHeartbeatS
        for boundary in (70, 80, 90, 100, 110):
            assert tick_at(s, clock, BASE + boundary + 0.1) == []
        out = payloads(tick_at(s, clock, BASE + 120.2))
        assert [(p["intervalStart"], p["intervalEnd"], p["seq"]) for p in out] == [
            (BASE_MS + 60_000, BASE_MS + 120_000, 2)]

    def test_a_registration_added_without_a_run_heartbeats_at_the_next_idle_boundary(self):
        clock = FakeClock(BASE)
        view = FakeView(rows={})
        s = make(clock, view)
        assert tick_at(s, clock, BASE + 60.1) == []  # nothing registered: nothing emitted
        view.rows[REG_B] = RegistrationInfo(WF_B, "1", ("plates",), True)
        tick_at(s, clock, BASE + 75)  # the snapshot change is adopted
        assert s.tracked_ids() == [REG_B]
        assert tick_at(s, clock, BASE + 80.1) == []
        out = payloads(tick_at(s, clock, BASE + 120.1))
        assert len(out) == 1 and out[0]["idle"] is True and out[0]["workflowId"] == WF_B
        assert out[0]["intervalStart"] == BASE_MS + 75_000 and out[0]["intervalEnd"] == BASE_MS + 120_000
        assert out[0]["workflowVersion"] == "1"

    def test_a_superseded_registration_emits_its_final_window_and_nothing_after(self):
        clock = FakeClock(BASE)
        view = FakeView()
        s = make(clock, view)
        clock.set(BASE + 2)
        s.observe_run(run_record(finished_at=BASE + 2))
        assert len(tick_at(s, clock, BASE + 10.1)) == 1
        clock.set(BASE + 12)
        s.observe_run(run_record(execution_id="exec-2", finished_at=BASE + 12))
        del view.rows[REG_A]  # superseded
        out = payloads(tick_at(s, clock, BASE + 13))
        assert len(out) == 1
        final = out[0]
        assert final["idle"] is False and final["runs"]["completed"] == 1
        assert final["intervalStart"] == BASE_MS + 10_000 and final["intervalEnd"] == BASE_MS + 13_000
        assert final["seq"] == 2
        assert s.tracked_ids() == []
        for boundary in (20, 30, 60, 120, 300, 360):
            assert tick_at(s, clock, BASE + boundary + 0.1) == []

    def test_a_superseded_registration_without_recent_runs_emits_a_final_heartbeat(self):
        clock = FakeClock(BASE)
        view = FakeView()
        s = make(clock, view)
        assert len(tick_at(s, clock, BASE + 60.1)) == 1  # the regular Heartbeat
        del view.rows[REG_A]
        out = payloads(tick_at(s, clock, BASE + 75))
        assert len(out) == 1 and out[0]["idle"] is True
        assert out[0]["intervalStart"] == BASE_MS + 60_000 and out[0]["intervalEnd"] == BASE_MS + 75_000
        assert tick_at(s, clock, BASE + 120.1) == []

    def test_a_final_window_shorter_than_one_second_is_not_emitted(self):
        clock = FakeClock(BASE)
        view = FakeView()
        s = make(clock, view)
        clock.set(BASE + 2)
        s.observe_run(run_record(finished_at=BASE + 2))
        assert len(tick_at(s, clock, BASE + 10.1)) == 1
        del view.rows[REG_A]
        assert tick_at(s, clock, BASE + 10.5) == []  # 500 ms: the schema would refuse it
        assert s.tracked_ids() == []

    def test_a_mid_interval_version_change_yields_two_summaries_with_consecutive_seq(self):
        clock = FakeClock(BASE)
        view = FakeView()
        s = make(clock, view)
        clock.set(BASE + 2)
        s.observe_run(run_record(finished_at=BASE + 2))
        assert [p["seq"] for p in payloads(tick_at(s, clock, BASE + 10.1))] == [1]
        clock.set(BASE + 12)
        s.observe_run(run_record(execution_id="exec-2", finished_at=BASE + 12))
        # version 3 -> 4 at BASE + 15
        del view.rows[REG_A]
        view.rows[REG_A4] = RegistrationInfo(WF_A, "4", (), False)
        first = payloads(tick_at(s, clock, BASE + 15))
        clock.set(BASE + 17)
        s.observe_run(run_record(registration_id=REG_A4, version="4", execution_id="exec-3",
                                 finished_at=BASE + 17))
        second = payloads(tick_at(s, clock, BASE + 20.1))
        both = first + second
        assert [(p["workflowId"], p["workflowVersion"], p["seq"], p["runs"]["completed"]) for p in both] == [
            (WF_A, "3", 2, 1), (WF_A, "4", 3, 1)]
        assert first[0]["intervalStart"] == BASE_MS + 10_000 and first[0]["intervalEnd"] == BASE_MS + 15_000
        assert second[0]["intervalStart"] == BASE_MS + 15_000 and second[0]["intervalEnd"] == BASE_MS + 20_000
        assert s.tracked_ids() == [REG_A4]

    def test_a_late_observation_for_a_dropped_id_emits_once(self):
        clock = FakeClock(BASE)
        view = FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", ("plates",), True)})
        s = make(clock, view)
        clock.set(BASE + 2)
        s.observe_model("readiness", REG_A, "exec-0", "plates", "READY", 0, True)
        s.observe_run(run_record(finished_at=BASE + 2))
        assert len(tick_at(s, clock, BASE + 10.1)) == 1
        del view.rows[REG_A]
        tick_at(s, clock, BASE + 11)  # final window < 1 s: nothing; the id is gone
        assert s.tracked_ids() == []
        clock.set(BASE + 17)
        s.observe_run(run_record(execution_id="late", finished_at=BASE + 17))  # finished after the drop
        out = payloads(tick_at(s, clock, BASE + 20.1))
        assert len(out) == 1
        late = out[0]
        assert late["workflowId"] == WF_A and late["workflowVersion"] == "3" and late["runs"]["completed"] == 1
        assert late["intervalStart"] == BASE_MS + 17_000 and late["intervalEnd"] == BASE_MS + 20_000
        assert late["model"]["models"] == {"plates": "READY"}  # the remembered states
        assert s.tracked_ids() == []
        for boundary in (30, 60, 120):
            assert tick_at(s, clock, BASE + boundary + 0.1) == []

    def test_a_late_drop_without_a_run_emits_nothing(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={}))
        s.observe_drop("reg-unknown:1", "trigger", "queue_full")
        assert tick_at(s, clock, BASE + 10.1) == []
        assert tick_at(s, clock, BASE + 60.1) == []

    def test_heartbeats_resume_after_five_idle_minutes(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 5)
        s.observe_run(run_record(finished_at=BASE + 5))
        first = payloads(tick_at(s, clock, BASE + 10.1))
        assert first[0]["runs"]["completed"] == 1
        # one tick covering 20 .. 300: a Summary per boundary (the run is within 5 minutes), no Heartbeat
        out = payloads(tick_at(s, clock, BASE + 300.4))
        assert len(out) == 29 and all(p["idle"] is False for p in out)
        assert [p["runs"]["completed"] for p in out] == [0] * 29
        assert out[-1]["intervalEnd"] == BASE_MS + 300_000
        # 310 .. 360: idle; the Heartbeat at the 60 s grid spans the last Summary's end to the boundary
        out = payloads(tick_at(s, clock, BASE + 360.2))
        assert len(out) == 1 and out[0]["idle"] is True
        assert out[0]["intervalStart"] == BASE_MS + 300_000 and out[0]["intervalEnd"] == BASE_MS + 360_000
        assert out[0]["intervalS"] == 60
        # a new run makes the Pair active again at the next summary boundary
        clock.set(BASE + 365)
        s.observe_run(run_record(execution_id="again", finished_at=BASE + 365))
        out = payloads(tick_at(s, clock, BASE + 370.1))
        assert len(out) == 1 and out[0]["idle"] is False
        assert out[0]["intervalStart"] == BASE_MS + 360_000 and out[0]["intervalEnd"] == BASE_MS + 370_000

    def test_a_stale_window_start_is_clipped_to_the_declared_interval(self):
        clock = FakeClock(BASE + 2)
        s = make(clock)  # window starts at BASE + 2, no emission until a run (Heartbeat at 60)
        clock.set(BASE + 44)
        s.observe_run(run_record(finished_at=BASE + 44))
        out = payloads(tick_at(s, clock, BASE + 50.1))
        assert len(out) == 1
        # 48 s have passed since the window opened; the Summary declares 10 s and may not exceed 11 s
        assert out[0]["intervalStart"] == BASE_MS + 40_000 and out[0]["intervalEnd"] == BASE_MS + 50_000
        assert out[0]["runs"]["completed"] == 1

    def test_seq_is_one_counter_per_workflow_across_summaries_and_heartbeats(self):
        clock = FakeClock(BASE)
        view = FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", (), False),
                              REG_B: RegistrationInfo(WF_B, "1", (), False)})
        s = make(clock, view)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        seqs = []
        for boundary in (10, 20, 30, 40, 50, 60):
            seqs.extend((p["workflowId"], p["idle"], p["seq"])
                        for p in payloads(tick_at(s, clock, BASE + boundary + 0.2)))
        # six Summaries for A (active) and one Heartbeat for B at 60, each workflow counting from 1
        assert sorted(seqs) == sorted([(WF_A, False, n) for n in range(1, 7)] + [(WF_B, True, 1)])
        # the instance's emission count
        assert s.seq == 7
        clock.set(BASE + 61)
        s.observe_run(run_record(registration_id=REG_B, workflow_id=WF_B, version="1", finished_at=BASE + 61))
        out = payloads(tick_at(s, clock, BASE + 70.2))
        assert sorted((p["workflowId"], p["idle"], p["seq"]) for p in out) == sorted(
            [(WF_A, False, 7), (WF_B, False, 2)])

    def test_boundaries_in_one_tick_are_processed_in_order_with_observations_split_by_time(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 3)
        s.observe_run(run_record(execution_id="e1", finished_at=BASE + 3))
        clock.set(BASE + 13)
        s.observe_run(run_record(execution_id="e2", finished_at=BASE + 13))
        clock.set(BASE + 14)
        s.observe_run(run_record(execution_id="e3", finished_at=BASE + 14))
        out = payloads(tick_at(s, clock, BASE + 20.5))
        assert [(p["intervalEnd"], p["runs"]["completed"]) for p in out] == [
            (BASE_MS + 10_000, 1), (BASE_MS + 20_000, 2)]


# ---------------------------------------------------------------------------
# Detections, outcomes, people
# ---------------------------------------------------------------------------


class TestDetections:
    def test_sixty_four_label_cap_folds_into_other(self):
        clock = FakeClock(BASE)
        s = make(clock)
        labels = ["label-%02d" % i for i in range(70)]
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1, detections=detections_of(*labels)))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        per_class = summary["detections"]["perClass"]
        assert len(per_class) == 64 and "__other__" in per_class
        assert set(per_class) - {"__other__"} == set(labels[:63])
        assert per_class["__other__"] == {"total": 7, "max": 7}
        assert summary["detections"]["truncated"] is True
        assert summary["detections"]["total"] == 70 and summary["detections"]["runsWithDetections"] == 1
        assert sum(v["total"] for v in per_class.values()) == summary["detections"]["total"]

    def test_labels_longer_than_64_characters_are_cut(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1, detections=detections_of("x" * 100)))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert list(summary["detections"]["perClass"]) == ["x" * 64]

    def test_totals_maxima_people_and_runs_with_detections(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(execution_id="e1", finished_at=BASE + 1,
                                 detections=detections_of("person", "person", "person", "hat")))
        s.observe_run(run_record(execution_id="e2", finished_at=BASE + 2,
                                 detections=detections_of("person", "person", "person", "person", "person")))
        s.observe_run(run_record(execution_id="e3", finished_at=BASE + 3, detections=[]))
        s.observe_run(run_record(execution_id="e4", finished_at=BASE + 4))  # no detections list at all
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        detections = summary["detections"]
        assert detections["perClass"] == {"person": {"total": 8, "max": 5}, "hat": {"total": 1, "max": 1}}
        assert detections["total"] == 9 and detections["runsWithDetections"] == 2
        assert detections["truncated"] is False
        assert summary["people"] == {"seen": 5}
        assert summary["runs"]["completed"] == 4

    def test_person_classes_come_from_the_settings(self):
        clock = FakeClock(BASE)
        s = make(clock)
        s.configure(SummarizerSettings(enabled=True, person_classes=("worker",)))
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1, detections=detections_of("worker", "worker", "person")))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["people"] == {"seen": 2}

    def test_total_comes_from_detection_count_with_truncated_for_a_1200_detection_run(self):
        clock = FakeClock(BASE)
        s = make(clock)
        capped = detections_of(*(["person"] * 1000))  # the RunRecord carries at most 1,000 items
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1, detections=capped, detection_count=1200))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["detections"]["total"] == 1200
        assert summary["detections"]["perClass"] == {"person": {"total": 1000, "max": 1000}}
        assert summary["detections"]["truncated"] is True
        assert summary["people"] == {"seen": 1000}

    def test_outcomes_when_is_anomalous_is_present(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(execution_id="e1", finished_at=BASE + 1, tag_values={"is_anomalous": True}))
        s.observe_run(run_record(execution_id="e2", finished_at=BASE + 2, tag_values={"is_anomalous": False}))
        s.observe_run(run_record(execution_id="e3", finished_at=BASE + 3, tag_values={"is_anomalous": "true"}))
        s.observe_run(run_record(execution_id="e4", finished_at=BASE + 4, tag_values={"confidence": 0.5}))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["outcomes"] == {"anomalous": 2, "normal": 1}


# ---------------------------------------------------------------------------
# Latency, failures, drops, continuous
# ---------------------------------------------------------------------------


class TestRunsLatencyAndCounters:
    def test_latency_percentiles_over_the_sample(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        for i, latency in enumerate([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]):
            s.observe_run(run_record(execution_id="e%d" % i, finished_at=BASE + 1, latency_ms=latency))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["latency"] == {"p50Ms": 500, "p95Ms": 1000, "maxMs": 1000, "n": 10}

    def test_latency_sample_is_bounded_to_2000_values(self):
        acc = PairAccumulator(REG_A, WF_A, "3", False, BASE_MS)
        for i in range(2500):
            acc.observe_run(run_record(execution_id="e%d" % i, latency_ms=i + 1), BASE_MS + i, ("person",))
        blocks = acc.summary_blocks(None)
        assert blocks["latency"]["n"] == 2000 and blocks["latency"]["maxMs"] == 2500
        assert blocks["runs"]["completed"] == 2500

    def test_no_latency_sample_for_a_run_refused_before_running_and_it_counts_as_failed(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(status="failed", run_started=False, error="preflight refused",
                                 finished_at=BASE + 1))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["runs"] == {"completed": 0, "failed": 1, "dropped": 0}
        assert "latency" not in summary

    def test_failed_runs_with_a_sample_and_mixed_statuses(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(execution_id="ok", finished_at=BASE + 1, latency_ms=100))
        s.observe_run(run_record(execution_id="bad", status="failed", error="boom", finished_at=BASE + 2,
                                 latency_ms=300))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["runs"] == {"completed": 1, "failed": 1, "dropped": 0}
        assert summary["latency"]["n"] == 2 and summary["latency"]["maxMs"] == 300

    def test_dropped_is_gate_discards_plus_skipped_busy(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        s.observe_drop(REG_A, "trigger-1", "queue_full")
        s.observe_drop(REG_A, "trigger-1", "drop_policy")
        s.observe_continuous(REG_A, "skippedBusy", 3)
        s.observe_continuous(REG_A, "outputsSent", 5)  # ignored
        s.observe_drop(None, "trigger-1", "queue_full")  # no registration: ignored
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["runs"]["dropped"] == 5

    def test_continuous_block_from_ticks_and_the_status_provider(self):
        clock = FakeClock(BASE)
        statuses = {REG_A: {"paused": True, "effectiveFps": 2.5}}
        s = make(clock, status_provider=lambda rid: statuses.get(rid))
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        s.observe_continuous(REG_A, "skippedNoNewFrame", 4)
        s.observe_continuous(REG_A, "skippedNoNewFrame", 2)
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["continuous"] == {"skippedNoNewFrame": 6, "paused": True, "effectiveFps": 2.5}

    def test_no_continuous_block_without_a_runner_or_ticks(self):
        clock = FakeClock(BASE)
        s = make(clock, status_provider=lambda rid: None)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        assert "continuous" not in payloads(tick_at(s, clock, BASE + 10.1))[0]

    def test_telemetry_counters_are_deltas_since_the_previous_emission(self):
        clock = FakeClock(BASE)
        counters = {"queueDrops": 3, "publishFailures": 1, "eventsDropped": 0}
        s = make(clock, counters_provider=lambda: dict(counters))
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        first = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert first["telemetry"] == {"queueDrops": 3, "publishFailures": 1, "eventsDropped": 0}
        counters.update({"queueDrops": 5, "eventsDropped": 2})
        second = payloads(tick_at(s, clock, BASE + 20.1))[0]
        assert second["telemetry"] == {"queueDrops": 2, "publishFailures": 0, "eventsDropped": 2}
        third = payloads(tick_at(s, clock, BASE + 30.1))[0]
        assert third["telemetry"] == {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}


# ---------------------------------------------------------------------------
# The model block
# ---------------------------------------------------------------------------


class TestModelBlock:
    def test_none_without_triton(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", (), False)}))
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["model"] == {"state": "NONE", "models": {}, "waits": 0, "waitedMs": 0}

    def test_waited_ms_summed_from_gate_outcomes_and_waits_counted(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", ("plates", "faces"), True)}))
        clock.set(BASE + 1)
        s.observe_model("readiness", REG_A, "e1", "plates", "LOADING", 1200, False)
        s.observe_model("readiness", REG_A, "e1", "faces", "READY", 800, True)
        s.observe_model("readiness", REG_A, "e2", "plates", "READY", 0, True)
        s.observe_model("readiness", REG_A, "e2", "faces", "READY", 0, True)
        s.observe_run(run_record(execution_id="e1", finished_at=BASE + 1))
        s.observe_run(run_record(execution_id="e2", finished_at=BASE + 2))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["model"] == {"state": "LOADING", "models": {"plates": "READY", "faces": "READY"},
                                    "waits": 2, "waitedMs": 2000}

    def test_one_continuous_wait_polled_three_times_counts_once(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", ("plates",), True)}))
        clock.set(BASE + 1)
        wait = {"model": "plates", "tritonModel": "plates_v1", "state": "LOADING", "reason": None,
                "sinceMs": BASE_MS, "stalled": False}
        for _ in range(3):  # three MODEL_POLL_S polls of the same wait
            s.observe_model("wait", REG_A, dict(wait))
            s.observe_continuous(REG_A, "modelUnavailable", 1)  # the runner's own counter: ignored
        s.observe_model("wait", REG_A, None)  # the wait ends
        s.observe_run(run_record(finished_at=BASE + 5))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["model"]["waits"] == 1 and summary["model"]["waitedMs"] == 0
        assert summary["model"]["models"] == {"plates": "LOADING"}  # None clears nothing
        assert summary["model"]["state"] == "LOADING"
        assert summary["runs"]["dropped"] == 0
        # a second wait later counts again
        clock.set(BASE + 12)
        s.observe_model("wait", REG_A, dict(wait))
        s.observe_model("wait", REG_A, dict(wait))
        s.observe_run(run_record(execution_id="e2", finished_at=BASE + 13))
        summary = payloads(tick_at(s, clock, BASE + 20.1))[0]
        assert summary["model"]["waits"] == 1

    def test_last_known_states_repeat_without_an_observation_and_waits_reset(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", ("plates",), True)}))
        clock.set(BASE + 1)
        s.observe_model("readiness", REG_A, "e1", "plates", "LOADING", 3000, False)
        s.observe_run(run_record(finished_at=BASE + 1))
        first = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert first["model"] == {"state": "LOADING", "models": {"plates": "LOADING"}, "waits": 1,
                                  "waitedMs": 3000}
        second = payloads(tick_at(s, clock, BASE + 20.1))[0]  # still active, no model observation
        assert second["model"] == {"state": "LOADING", "models": {"plates": "LOADING"}, "waits": 0,
                                   "waitedMs": 0}

    def test_triton_pair_without_any_observation_reports_unknown(self):
        acc = PairAccumulator(REG_A, WF_A, "3", True, BASE_MS)
        assert acc.model_block() == {"state": "UNKNOWN", "models": {}, "waits": 0, "waitedMs": 0}

    def test_worst_state_ordering_and_unknown_strings(self):
        assert list(MODEL_STATES) == ["NONE", "READY", "UNLOADING", "LOADING", "INCOMPLETE", "NOT_DEPLOYED",
                                      "UNKNOWN", "UNAVAILABLE"]
        assert worst_state(["READY", "LOADING", "UNLOADING"]) == "LOADING"
        assert worst_state(["READY", "UNAVAILABLE", "UNKNOWN"]) == "UNAVAILABLE"
        assert worst_state(["ready", "not_deployed"]) == "NOT_DEPLOYED"
        assert worst_state([]) == "NONE"
        assert normalize_state("something-else") == "UNKNOWN"
        assert normalize_state(None) == "UNKNOWN"
        assert normalize_state(" ready ") == "READY"
        acc = PairAccumulator(REG_A, WF_A, "3", True, BASE_MS)
        acc.observe_readiness("plates", "WEIRD", 0, True)
        acc.observe_readiness("faces", "INCOMPLETE", 0, True)
        block = acc.model_block()
        assert block["models"] == {"plates": "UNKNOWN", "faces": "INCOMPLETE"} and block["state"] == "UNKNOWN"

    def test_models_map_is_capped_at_16_names_of_64_characters(self):
        acc = PairAccumulator(REG_A, WF_A, "3", True, BASE_MS)
        for i in range(20):
            acc.observe_readiness("m%02d-%s" % (i, "x" * 100), "READY", 0, True)
        block = acc.model_block()
        assert len(block["models"]) == 16 and all(len(name) == 64 for name in block["models"])


# ---------------------------------------------------------------------------
# Size cap, enable and disable
# ---------------------------------------------------------------------------


class TestCapAndSwitching:
    def test_size_cap_folds_per_class_then_model_names(self):
        clock = FakeClock(BASE)
        s = make(clock, FakeView(rows={REG_A: RegistrationInfo(WF_A, "3", ("plates",), True)}),
                 size_cap_bytes=700)
        clock.set(BASE + 1)
        for i in range(10):
            s.observe_model("readiness", REG_A, "e1", "model-%02d" % i, "READY", 0, True)
        s.observe_run(run_record(finished_at=BASE + 1, detections=detections_of(*["label-%02d" % i
                                                                                   for i in range(40)])))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert len(json.dumps(summary, separators=(",", ":")).encode("utf-8")) <= 700
        assert summary["detections"]["perClass"] == {"__other__": {"total": 40, "max": 1}}
        assert summary["detections"]["truncated"] is True and summary["detections"]["total"] == 40
        assert summary["model"]["models"] == {} and summary["model"]["state"] == "READY"
        assert s.oversized == 1

    def test_a_summary_within_the_cap_is_untouched(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1, detections=detections_of("person")))
        summary = payloads(tick_at(s, clock, BASE + 10.1))[0]
        assert summary["detections"]["perClass"] == {"person": {"total": 1, "max": 1}}
        assert s.oversized == 0
        assert len(json.dumps(summary).encode("utf-8")) <= summarizer_module.MAX_SUMMARY_BYTES

    def test_enable_resets_the_accumulators_and_starts_the_interval_at_the_enable_time(self):
        clock = FakeClock(BASE)
        s = make(clock, enabled=False)
        for i in range(3):
            clock.advance(1)
            s.observe_run(run_record(execution_id="disabled-%d" % i, finished_at=clock.now))
        assert tick_at(s, clock, BASE + 10.1) == [] and s.discarded_observations == 3
        clock.set(BASE + 13.5)
        s.configure(SummarizerSettings(enabled=True))
        clock.set(BASE + 15)
        s.observe_run(run_record(execution_id="enabled", finished_at=BASE + 15))
        out = payloads(tick_at(s, clock, BASE + 20.1))
        assert len(out) == 1
        assert out[0]["runs"]["completed"] == 1
        assert out[0]["intervalStart"] == BASE_MS + 13_500 and out[0]["intervalEnd"] == BASE_MS + 20_000
        assert out[0]["seq"] == 1

    def test_disable_discards_the_window_and_seq_continues(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        assert [p["seq"] for p in payloads(tick_at(s, clock, BASE + 10.1))] == [1]
        clock.set(BASE + 12)
        s.observe_run(run_record(execution_id="lost", finished_at=BASE + 12))
        s.configure(SummarizerSettings(enabled=False))
        assert tick_at(s, clock, BASE + 20.1) == []  # no partial Summary
        assert tick_at(s, clock, BASE + 60.1) == []  # no Heartbeat either
        assert s.tracked_ids() == []
        clock.set(BASE + 62)
        s.configure(SummarizerSettings(enabled=True))
        clock.set(BASE + 63)
        s.observe_run(run_record(execution_id="again", finished_at=BASE + 63))
        out = payloads(tick_at(s, clock, BASE + 70.1))
        assert [(p["seq"], p["runs"]["completed"]) for p in out] == [(2, 1)]
        assert out[0]["intervalStart"] == BASE_MS + 62_000

    def test_an_interval_change_rearms_the_ticker(self):
        clock = FakeClock(BASE)
        s = make(clock)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        s.configure(SummarizerSettings(enabled=True, summary_interval_s=5, idle_heartbeat_s=30))
        out = payloads(tick_at(s, clock, BASE + 5.1))
        assert len(out) == 1 and out[0]["intervalS"] == 5
        assert out[0]["intervalStart"] == BASE_MS and out[0]["intervalEnd"] == BASE_MS + 5_000

    def test_configure_rejects_a_non_settings_value(self):
        s = make(FakeClock(BASE))
        with pytest.raises(TypeError):
            s.configure({"enabled": True})


# ---------------------------------------------------------------------------
# Sink, containment, diagnostics
# ---------------------------------------------------------------------------


class TestSinkAndStatus:
    def test_the_sink_receives_every_emission_outside_the_lock_and_may_raise(self):
        clock = FakeClock(BASE)
        seen = []

        def sink(emission):
            seen.append(emission)
            raise RuntimeError("sink boom")

        s = make(clock, sink=sink)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        out = tick_at(s, clock, BASE + 10.1)
        assert len(out) == 1 and seen == out
        assert out[0].registration_id == REG_A and out[0].workflow_id == WF_A

    def test_garbage_observations_never_raise(self):
        clock = FakeClock(BASE)
        s = make(clock)
        s.observe_run("not a record")
        s.observe_run(None)
        s.observe_model("readiness", REG_A)  # wrong arity: ignored
        s.observe_model("wait", REG_A, "not a dict")
        s.observe_continuous(REG_A, "skippedBusy", "many")
        s.observe_drop(REG_A, None, None)
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        out = payloads(tick_at(s, clock, BASE + 10.1))
        assert len(out) == 1 and out[0]["runs"]["completed"] == 1

    def test_status_and_tracked_ids(self):
        clock = FakeClock(BASE)
        s = make(clock)
        status = s.status()
        assert status["enabled"] is True and status["trackedRegistrations"] == [REG_A]
        assert status["seq"] == 0 and status["summaryIntervalS"] == 10 and status["idleHeartbeatS"] == 60
        assert status["personClasses"] == ["person", "human"]
        assert status["threadAlive"] is False
        clock.set(BASE + 1)
        s.observe_run(run_record(finished_at=BASE + 1))
        tick_at(s, clock, BASE + 10.1)
        status = s.status()
        assert status["seq"] == 1 and status["emittedSummaries"] == 1 and status["lastEmissionMs"] == BASE_MS + 10_000

    def test_the_real_thread_ticks_and_stops(self):
        import time

        s = make(FakeClock(BASE), poll_s=0.02)
        s.start()
        try:
            assert s._thread.name == "telemetry-summarizer" and s._thread.daemon
            time.sleep(0.2)
            assert s.status()["threadAlive"] is True
        finally:
            s.stop()
            s._thread.join(2.0)
        assert not s._thread.is_alive()
