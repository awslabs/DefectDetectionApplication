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
"""The ``dda.event.v1`` helpers (workflow-telemetry-dashboards design §4.2,
§5.2 ``events.py``; tasks.md 1.2): coalescing per key and window, the
``count``, the rate limiter and its counter, the redaction of a planted URL
with user information and of a Credential_Store value, the envelope shape
and its size.

Validates: R3.1-R3.3, R19.4.
"""
import json
import uuid

import pytest

from workflow_engine.telemetry import events as events_module
from workflow_engine.telemetry.events import (
    MAX_ERROR_CHARS, MAX_EVENT_BYTES, EventRateLimiter, RunErrorCoalescer, build_event, encode_event,
    redact_error,
)

WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_OTHER = "1b2c3d4e-5f6a-4b7c-9d0e-1f2a3b4c5d6e"
INSTANCE = "7c1e7d4e-3b2a-4c9d-8e1f-0a2b3c4d5e6f"
NOW_MS = 1_792_075_062_000
PRODUCER = {"name": "engine", "version": "1.0.0", "instance": INSTANCE}


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = NOW_MS / 1000.0

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class Record:
    def __init__(self, error, node="node-1", execution_id="exec-1", workflow_id=WF, version="3", finished_ms=None):
        self.error = error
        self.failing_node_id = node
        self.execution_id = execution_id
        self.workflow_id = workflow_id
        self.workflow_version = version
        self.finished_ms = finished_ms if finished_ms is not None else NOW_MS
        self.status = "failed"


@pytest.fixture
def coalescer():
    clock = FakeClock()
    c = RunErrorCoalescer(clock=lambda: clock.mono, wall=lambda: clock.wall, secrets=lambda: ["s3cr3t-value"])
    c.test_clock = clock
    return c


class TestCoalescer:
    def test_first_failure_of_a_key_is_emitted_at_once_with_count_1(self, coalescer):
        data = coalescer.observe(Record("Triton returned 500"))
        assert data is not None
        assert data["type"] == "run_error" and data["workflowId"] == WF and data["workflowVersion"] == "3"
        assert data["executionId"] == "exec-1"
        assert data["data"] == {"error": "Triton returned 500", "failingNodeId": "node-1", "count": 1,
                                "firstTs": NOW_MS, "lastTs": NOW_MS}

    def test_failures_within_ten_seconds_are_coalesced_into_one_event_with_count(self, coalescer):
        clock = coalescer.test_clock
        assert coalescer.observe(Record("boom", execution_id="e1")) is not None
        for i in range(4):
            clock.advance(1.5)
            assert coalescer.observe(Record("boom", execution_id="e%d" % (i + 2),
                                            finished_ms=NOW_MS + int(clock.mono - 1000) * 1000)) is None
        assert coalescer.flush() == []  # the window is still open (6 s)
        clock.advance(4.5)  # 10.5 s since the window opened
        due = coalescer.flush()
        assert len(due) == 1
        assert due[0]["data"]["count"] == 4
        assert due[0]["data"]["firstTs"] == NOW_MS + 1_000 and due[0]["data"]["lastTs"] == NOW_MS + 6_000
        assert due[0]["executionId"] == "e5"
        assert coalescer.emitted == 2 and coalescer.coalesced == 4

    def test_at_most_one_event_per_key_per_window(self, coalescer):
        clock = coalescer.test_clock
        emitted = 0
        for _ in range(100):  # a failure every 300 ms for 30 s
            if coalescer.observe(Record("boom")) is not None:
                emitted += 1
            emitted += len(coalescer.flush())
            clock.advance(0.3)
        emitted += len(coalescer.flush())
        assert emitted == 3  # 0 s, 10 s, 20 s (the 30 s window is still open)
        assert coalescer.observed == 100

    def test_a_failure_after_an_unflushed_window_joins_its_event(self, coalescer):
        clock = coalescer.test_clock
        assert coalescer.observe(Record("boom")) is not None
        clock.advance(2)
        assert coalescer.observe(Record("boom")) is None
        clock.advance(12)  # the window elapsed but nobody flushed
        data = coalescer.observe(Record("boom"))
        assert data is not None and data["data"]["count"] == 2
        clock.advance(1)
        assert coalescer.observe(Record("boom")) is None  # the new window is open
        assert coalescer.flush() == []

    def test_an_idle_key_emits_at_once_again(self, coalescer):
        clock = coalescer.test_clock
        assert coalescer.observe(Record("boom")) is not None
        clock.advance(11)
        assert coalescer.flush() == []  # nothing coalesced: the key is forgotten
        assert coalescer.pending_keys() == 0
        assert coalescer.observe(Record("boom")) is not None

    def test_keys_are_workflow_node_and_the_first_64_characters(self, coalescer):
        base = "x" * 64
        assert coalescer.observe(Record(base + "-one")) is not None
        assert coalescer.observe(Record(base + "-two")) is None  # same first 64 characters
        assert coalescer.observe(Record(base + "-one", node="node-2")) is not None  # another node
        assert coalescer.observe(Record(base + "-one", workflow_id=WF_OTHER)) is not None  # another workflow
        assert coalescer.observe(Record("y" * 70)) is not None  # another error prefix
        assert coalescer.pending_keys() == 4

    def test_missing_error_text_and_node(self, coalescer):
        data = coalescer.observe(Record(None, node=None, execution_id=None))
        assert data["data"]["error"] == "run failed" and data["data"]["failingNodeId"] is None
        assert data["executionId"] is None

    def test_error_text_is_redacted_and_cut_to_1024_characters(self, coalescer):
        planted = ("connect to rtsp://admin:hunter2@camera.example.com/stream failed; token s3cr3t-value "
                   "rejected; password=abc123 ") + "z" * 2000
        data = coalescer.observe(Record(planted))
        error = data["data"]["error"]
        assert "hunter2" not in error and "rtsp://***@camera.example.com" in error
        assert "s3cr3t-value" not in error and "***" in error
        assert "abc123" not in error
        assert len(error) == MAX_ERROR_CHARS


class TestRedactError:
    def test_planted_url_user_information_and_store_value_are_masked(self):
        text = "GET https://user:pa55@host.example.com/x?signature=abc with key verysecretvalue"
        out = redact_error(text, ["verysecretvalue"])
        assert "pa55" not in out and "verysecretvalue" not in out and "abc" not in out
        assert "https://***@host.example.com/x" in out

    def test_non_strings_and_none(self):
        assert redact_error(None) == ""
        assert redact_error(ValueError("bad thing"), []) == "bad thing"
        assert redact_error("", []) == ""

    def test_default_secrets_come_from_the_credential_store(self, monkeypatch):
        monkeypatch.setattr(events_module, "_default_secrets", lambda: ["from-the-store"])
        assert "from-the-store" not in redact_error("value from-the-store leaked")


class TestRateLimiter:
    def test_sixty_per_minute_then_dropped_and_counted(self):
        clock = FakeClock()
        limiter = EventRateLimiter(clock=lambda: clock.mono)
        results = [limiter.allow() for _ in range(70)]
        assert results.count(True) == 60 and results[60:] == [False] * 10
        assert limiter.dropped == 10 and limiter.allowed == 60
        clock.advance(59)
        assert limiter.allow() is False and limiter.dropped == 11
        clock.advance(1.5)  # the first stamps leave the sliding window
        assert limiter.allow() is True

    def test_the_window_slides(self):
        clock = FakeClock()
        limiter = EventRateLimiter(limit=3, window_s=10.0, clock=lambda: clock.mono)
        assert [limiter.allow() for _ in range(3)] == [True, True, True]
        clock.advance(5)
        assert limiter.allow() is False
        clock.advance(5)
        assert limiter.allow() is True  # the three stamps are now 10 s old
        assert limiter.dropped == 1


class TestBuildEvent:
    def test_run_error_envelope_shape_and_defaults(self):
        data = {"error": "boom", "failingNodeId": "node-1", "count": 2, "firstTs": NOW_MS, "lastTs": NOW_MS + 5}
        event = build_event("run_error", device_id="thing-1", workflow_id=WF, workflow_version=3, producer=PRODUCER,
                            data=data, ts=NOW_MS + 10, execution_id="exec-1")
        assert event["schema"] == "dda.event.v1" and event["type"] == "run_error"
        uuid.UUID(event["eventId"])
        assert event["ts"] == NOW_MS + 10 and event["deviceId"] == "thing-1"
        assert event["workflowId"] == WF and event["workflowVersion"] == "3"
        assert event["producer"] == PRODUCER and event["executionId"] == "exec-1"
        assert event["severity"] == "error" and event["data"] is data
        assert len(encode_event(event)) <= MAX_EVENT_BYTES
        json.loads(encode_event(event).decode("utf-8"))

    def test_default_severity_by_type_and_explicit_severity(self):
        assert build_event("clip_ready", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                           data={}, ts=1)["severity"] == "info"
        assert build_event("violation_confirmed", device_id="d", workflow_id=WF, workflow_version="1",
                           producer=PRODUCER, data={}, ts=1)["severity"] == "warning"
        assert build_event("run_error", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                           data={}, ts=1, severity="warning")["severity"] == "warning"
        with pytest.raises(ValueError):
            build_event("run_error", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                        data={}, ts=1, severity="fatal")

    def test_unknown_type_and_non_object_data_are_refused(self):
        with pytest.raises(ValueError):
            build_event("something", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                        data={}, ts=1)
        with pytest.raises(ValueError):
            build_event("run_error", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                        data=[], ts=1)

    def test_ts_defaults_to_the_wall_clock_and_execution_id_is_optional(self):
        event = build_event("run_error", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                            data={}, wall=lambda: NOW_MS / 1000.0)
        assert event["ts"] == NOW_MS and "executionId" not in event

    def test_an_oversized_run_error_is_shortened_to_fit_16_kb(self):
        data = {"error": "e" * 20000, "failingNodeId": None, "count": 1, "firstTs": 1, "lastTs": 1}
        event = build_event("run_error", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                            data=data, ts=1)
        assert len(encode_event(event)) <= MAX_EVENT_BYTES and len(event["data"]["error"]) < 20000

    def test_an_oversized_event_of_another_type_is_refused(self):
        with pytest.raises(ValueError):
            build_event("clip_ready", device_id="d", workflow_id=WF, workflow_version="1", producer=PRODUCER,
                        data={"blob": "b" * 20000}, ts=1)
