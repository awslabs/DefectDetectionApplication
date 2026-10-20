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
"""Summary ``seq`` is numbered per workflow (workflow-telemetry-dashboards
design §4.1, §6.5, §7.3).

The Portal checks ``seq`` per Pair (use case, device, workflowId): the
ingest's ``gap``/``duplicate`` from the ``LAST#``/``LASTHB#`` pointers and the
roll-up's ``fold.dedupe``/``fold.count_gaps`` all work inside one Pair
partition. Each workflowId therefore has its own counter, starting at 1 per
agent instance, shared by its Summaries and Heartbeats and by all its
registrations and versions, and kept when an accumulator is dropped and
recreated (transient accumulators included). Consecutive messages of a Pair
differ by exactly 1, so the Portal's own ``count_gaps`` finds no gap.
"""
import importlib
import os
import sys

import pytest

from workflow_engine.telemetry import hooks
from workflow_engine.telemetry.registry_view import RegistrationInfo
from workflow_engine.telemetry.summarizer import IntervalSummarizer, SummarizerSettings

THING = "thing-1"
INSTANCE = "7c1e7d4e-3b2a-4c9d-8e1f-0a2b3c4d5e6f"
WF_A = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_B = "1b2c3d4e-5f6a-4b7c-9d0e-1f2a3b4c5d6e"
REG_A3 = "reg-a:3"
REG_A4 = "reg-a:4"
REG_B = "reg-b:1"
#: A multiple of 300 s: aligned to the 10 s, 60 s and 300 s grids.
BASE = 1_792_075_200.0
BASE_MS = int(BASE * 1000)
PORTAL_FUNCTIONS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "edge-cv-portal", "backend",
                                "functions")


class FakeClock:
    def __init__(self, now=BASE):
        self.now = float(now)

    def __call__(self):
        return self.now


class FakeView:
    def __init__(self, rows):
        self.rows = dict(rows)

    def snapshot(self):
        return dict(self.rows)


def run_record(registration_id, workflow_id, version, finished_at, execution_id="exec"):
    finished_ms = int(round(finished_at * 1000))
    return hooks.RunRecord(
        registration_id=registration_id, workflow_id=workflow_id, workflow_version=version,
        execution_id=execution_id, status="completed", error=None, failing_node_id=None, traceback=None,
        capture_id="c", output_dir=None, log_path=None, enqueued_at_s=finished_ms // 1000 - 1,
        accepted_ms=finished_ms - 300, run_started_ms=finished_ms - 250, finished_ms=finished_ms,
        queued_ms=0, setup_ms=50, readiness_wait_ms=0, trigger_context={}, node_status=None, tag_values=None,
        detections=None, detection_count=0, continuous=False,
    )


def make(clock, view):
    return IntervalSummarizer(
        SummarizerSettings(enabled=True, summary_interval_s=10, idle_heartbeat_s=60), clock, view,
        device_id=THING, instance=INSTANCE, producer_version="1.0.0",
    )


def scenario():
    """Ten minutes of two workflows on one instance with accumulator churn:

    - A (versions 3 then 4) runs every 5 s and Summarizes every 10 s; B is idle
      and Heartbeats every 60 s;
    - at 125 s A's version changes 3 -> 4 (REG_A3 leaves with its final
      window, REG_A4 appears);
    - at 245 s REG_A4 leaves and a late run for it arrives at 247 s (a
      transient accumulator that emits once); REG_A4 comes back at 305 s;
    - at 425 s B leaves (its final Heartbeat) and comes back at 485 s.

    Returns every emitted payload in order."""
    clock = FakeClock(BASE)
    view = FakeView({REG_A3: RegistrationInfo(WF_A, "3", (), False), REG_B: RegistrationInfo(WF_B, "1", (), False)})
    s = make(clock, view)
    out = []
    current_a = [REG_A3, "3"]
    for step in range(1, 1201):  # 0.5 s steps over 600 s
        now = BASE + step * 0.5
        clock.now = now
        offset = step * 0.5
        if offset == 125:
            del view.rows[REG_A3]
            view.rows[REG_A4] = RegistrationInfo(WF_A, "4", (), False)
            current_a[:] = [REG_A4, "4"]
        elif offset == 245:
            del view.rows[REG_A4]
        elif offset == 247:
            s.observe_run(run_record(REG_A4, WF_A, "4", now, "late"))
        elif offset == 305:
            view.rows[REG_A4] = RegistrationInfo(WF_A, "4", (), False)
        elif offset == 425:
            del view.rows[REG_B]
        elif offset == 485:
            view.rows[REG_B] = RegistrationInfo(WF_B, "1", (), False)
        if offset % 5 == 0 and current_a[0] in view.rows:
            s.observe_run(run_record(current_a[0], WF_A, current_a[1], now, "exec-%d" % step))
        out.extend(e.payload for e in s.tick())
    return s, out


def seqs_by_workflow(payloads):
    by_wf = {}
    for payload in payloads:
        by_wf.setdefault(payload["workflowId"], []).append(payload["seq"])
    return by_wf


@pytest.fixture(scope="module")
def portal_fold():
    """The Portal's own ``telemetry.fold`` (pure; ``telemetry.schemas`` is
    stdlib only), imported from the repository with the Portal functions
    directory on ``sys.path`` and the module table restored afterwards."""
    saved_path = list(sys.path)
    saved = {name: module for name, module in sys.modules.items()
             if name == "telemetry" or name.startswith("telemetry.")}
    for name in saved:
        sys.modules.pop(name)
    sys.path.insert(0, os.path.abspath(PORTAL_FUNCTIONS))
    try:
        fold = importlib.import_module("telemetry.fold")
    finally:
        sys.path[:] = saved_path
        for name in [n for n in sys.modules if n == "telemetry" or n.startswith("telemetry.")]:
            sys.modules.pop(name)
        sys.modules.update(saved)
    return fold


def store_items(payloads):
    """Summary_Store items as the ingest writes them (``records.summary_item``:
    ``producer`` is the name, ``eventMs`` the ``intervalEnd``), per Pair."""
    by_pair = {}
    for index, payload in enumerate(payloads):
        by_pair.setdefault(payload["workflowId"], []).append({
            "producer": payload["producer"]["name"], "instance": payload["producer"]["instance"],
            "seq": payload["seq"], "eventMs": payload["intervalEnd"], "receivedMs": payload["emittedAt"] + index,
            "idle": payload["idle"], "summary": payload,
        })
    return by_pair


def minute_gaps(fold, items):
    minutes = sorted({fold.bucket_minute("summary", item["eventMs"]) for item in items})
    return {minute: fold.count_gaps(items, minute) for minute in minutes}


class TestSeqPerWorkflow:
    def test_each_workflow_counts_from_one_without_holes(self):
        s, out = scenario()
        by_wf = seqs_by_workflow(out)
        assert set(by_wf) == {WF_A, WF_B}
        for workflow_id, seqs in by_wf.items():
            assert seqs == list(range(1, len(seqs) + 1)), (workflow_id, seqs)
        # both versions and the transient accumulator of A share A's counter
        versions = [(p["workflowVersion"], p["seq"]) for p in out if p["workflowId"] == WF_A]
        assert {"3", "4"} == {v for v, _ in versions}
        late = [p for p in out if p["workflowId"] == WF_A and p["intervalStart"] == BASE_MS + 247_000]
        assert len(late) == 1, "the late run emitted once from a transient accumulator"
        # Summaries and Heartbeats of B share B's counter, across B leaving and coming back
        kinds_b = [p["idle"] for p in out if p["workflowId"] == WF_B]
        assert all(kinds_b) and len(kinds_b) >= 9
        # the agent status keeps a monotonic count of every emission (the sum of the per-workflow counters)
        assert s.seq == len(out) == sum(len(seqs) for seqs in by_wf.values())
        assert s.status()["seq"] == len(out)

    def test_consecutive_messages_of_a_pair_differ_by_exactly_one(self):
        _, out = scenario()
        for workflow_id, seqs in seqs_by_workflow(out).items():
            assert all(later - earlier == 1 for earlier, later in zip(seqs, seqs[1:])), (workflow_id, seqs)

    def test_a_counter_survives_disable_and_enable(self):
        clock = FakeClock(BASE)
        view = FakeView({REG_A3: RegistrationInfo(WF_A, "3", (), False), REG_B: RegistrationInfo(WF_B, "1", (), False)})
        s = make(clock, view)
        out = []
        for step in range(1, 241):
            clock.now = BASE + step * 0.5
            if step == 140:
                s.configure(SummarizerSettings(enabled=False))
            elif step == 160:
                s.configure(SummarizerSettings(enabled=True, summary_interval_s=10, idle_heartbeat_s=60))
            if step % 10 == 0:
                s.observe_run(run_record(REG_A3, WF_A, "3", clock.now, "exec-%d" % step))
            out.extend(e.payload for e in s.tick())
        for workflow_id, seqs in seqs_by_workflow(out).items():
            assert seqs == list(range(1, len(seqs) + 1)), (workflow_id, seqs)

    def test_the_portal_fold_finds_no_gap_and_dedupes_per_pair(self, portal_fold):
        _, out = scenario()
        for workflow_id, items in store_items(out).items():
            gaps = minute_gaps(portal_fold, items)
            assert sum(gaps.values()) == 0, (workflow_id, gaps)
            # a retransmission is folded once and still yields no gap
            doubled = items + [dict(items[3], receivedMs=items[3]["receivedMs"] + 5)]
            assert len(portal_fold.dedupe(doubled)) == len(items)
            assert sum(minute_gaps(portal_fold, doubled).values()) == 0

    def test_the_portal_fold_counted_false_gaps_for_one_counter_per_instance(self, portal_fold):
        """The 1.0.57 numbering (one counter for every workflow of the
        instance), renumbered from the same emissions: the Portal's per-Pair
        ``count_gaps`` sees the other workflow's numbers as gaps."""
        _, out = scenario()
        old = [dict(payload, seq=index + 1) for index, payload in enumerate(out)]
        total = 0
        for items in store_items(old).values():
            total += sum(minute_gaps(portal_fold, items).values())
        assert total > 0
        # every emission of the other workflow in between is one false gap
        by_wf = seqs_by_workflow(old)
        expected = sum(later - earlier - 1 for seqs in by_wf.values() for earlier, later in zip(seqs, seqs[1:]))
        assert total == expected
