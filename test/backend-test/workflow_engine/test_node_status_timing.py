#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Unit tests for NodeStatusCollector invocation durations and durationMs
serialization (node-execution-timing, task 1.2).

Covers ``record_invocation_duration`` containment and first-wins semantics
(R1.3, R1.4, R1.7), ``duration_ms_of`` precedence (R1.4), and the additive
``durationMs`` field in ``to_map()`` (R1.8, R2.1, R2.3, R2.4).
"""
import json

from workflow_engine.node_status import (
    NodeStatusCollector,
    STATUS_SUCCESS,
)


def _collector(node_ids=("n1", "n2")):
    return NodeStatusCollector(
        name_map={"elem_{0}".format(n): n for n in node_ids}
    )


class TestRecordInvocationDuration:
    def test_stores_rounded_int_for_tracked_node(self):
        collector = _collector()
        collector.record_invocation_duration("n1", 412.6)
        assert collector.duration_ms_of("n1") == 413

    def test_first_recorded_value_wins(self):
        collector = _collector()
        collector.record_invocation_duration("n1", 100)
        collector.record_invocation_duration("n1", 999)
        assert collector.duration_ms_of("n1") == 100

    def test_ignores_none_and_untracked_node_ids(self):
        collector = _collector()
        collector.record_invocation_duration(None, 100)
        collector.record_invocation_duration("ghost", 100)
        assert collector.duration_ms_of("ghost") is None
        assert "durationMs" not in collector.to_map()["n1"]

    def test_ignores_negative_and_non_numeric_values(self):
        collector = _collector()
        collector.record_invocation_duration("n1", -1)
        collector.record_invocation_duration("n1", -0.4)
        collector.record_invocation_duration("n1", "412")
        collector.record_invocation_duration("n1", None)
        collector.record_invocation_duration("n1", True)
        collector.record_invocation_duration("n1", float("nan"))
        collector.record_invocation_duration("n1", float("inf"))
        assert collector.duration_ms_of("n1") is None

    def test_zero_is_a_valid_duration(self):
        collector = _collector()
        collector.record_invocation_duration("n1", 0)
        assert collector.duration_ms_of("n1") == 0
        assert collector.to_map()["n1"]["durationMs"] == 0


class TestDurationMsOfPrecedence:
    def test_invocation_value_wins_over_lifecycle_value(self, monkeypatch):
        clock = iter([10.0, 12.5])
        monkeypatch.setattr(
            "workflow_engine.node_status.time.monotonic", lambda: next(clock)
        )
        collector = _collector(("n1",))
        collector.mark_running_all()      # lifecycle start at 10.0
        collector.mark_success_all()      # lifecycle duration = 2500 ms
        collector.record_invocation_duration("n1", 77)
        assert collector.duration_ms_of("n1") == 77
        assert collector.to_map()["n1"]["durationMs"] == 77

    def test_falls_back_to_lifecycle_value(self, monkeypatch):
        clock = iter([10.0, 10.412])
        monkeypatch.setattr(
            "workflow_engine.node_status.time.monotonic", lambda: next(clock)
        )
        collector = _collector(("n1",))
        collector.mark_running_all()
        collector.mark_success_all()
        assert collector.duration_ms_of("n1") == 412

    def test_none_when_nothing_recorded(self):
        collector = _collector(("n1",))
        assert collector.duration_ms_of("n1") is None


class TestToMapDurationMs:
    def test_duration_field_is_additive_and_conditional(self):
        collector = _collector(("n1", "n2"))
        collector.mark_running_all()
        collector.mark_failure("n2", "boom")
        collector.mark_success_all()
        collector.record_invocation_duration("n1", 1234)
        status_map = json.loads(collector.to_json())
        # n1: existing fields untouched, durationMs added (R2.1, R2.4).
        assert status_map["n1"]["status"] == STATUS_SUCCESS
        assert status_map["n1"]["durationMs"] == 1234
        assert isinstance(status_map["n1"]["durationMs"], int)
        # n2: status/detail preserved; lifecycle duration present since the
        # node ran to a terminal state (R1.8).
        assert status_map["n2"]["status"] == "failure"
        assert status_map["n2"]["detail"] == "boom"
        assert status_map["n2"]["durationMs"] >= 0

    def test_no_duration_field_without_a_recorded_duration(self):
        collector = _collector(("n1",))
        collector.mark_failure("n1", "never ran")  # terminal without running
        entry = collector.to_map()["n1"]
        assert entry == {"status": "failure", "detail": "never ran"}
