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
"""The per-session batcher (workflow-debug-mode design §4.1, §5.1
``batcher.py``, §5.4; tasks.md 2.4) with a fake clock and a publisher stub.

100 KB packing and the 120 KB hard cap; drop-oldest; the §5.4 truncation
order and its markers on oversized fixtures; the byte bucket (a 100 KB batch
at 32768 B/s waits about 3 s and counts ``bytesDeferred``; at 262144 B/s the
sustained 10 s volume stays within the cap); ``rateDeferred``; the spool-full
back-off (intervals 1, 2, 4 s on three rejections, halving on success,
``spoolFull == 3``, no rejected batch re-sent).

Validates: debug R7.1, R7.2, R7.3, R12.3.
"""
import json

import pytest

from workflow_engine.telemetry.debug import batcher as batcher_module
from workflow_engine.telemetry.debug.batcher import (
    MESSAGE_HARD_BYTES,
    MESSAGE_SOFT_BYTES,
    Batcher,
    TokenBucket,
    serialize,
    truncate_record,
)

TOPIC = "dda/debug/uc/wf/record"
ENVELOPE = {"sessionId": "11111111-1111-4111-8111-111111111111",
            "instance": "22222222-2222-4222-8222-222222222222",
            "deviceId": "thing-1", "workflowId": "wf", "workflowVersion": "3"}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePublisher:
    def __init__(self):
        self.published = []
        self.failures_by_reason = {"denied": 0, "spool_full": 0, "other": 0}
        self.publish_failures = 0
        self.publish_successes = 0

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload, qos))

    def messages(self):
        return [json.loads(p[1]) for p in self.published]


def make(clock=None, publisher=None, **kwargs):
    clock = clock or FakeClock()
    publisher = publisher or FakePublisher()
    options = dict(topic=TOPIC, envelope=ENVELOPE, wall=lambda: clock.now + 1_000_000_000)
    options.update(kwargs)
    return Batcher(publisher=publisher, clock=clock, **options), clock, publisher


def record(size=100, index=0, **extra):
    base = {"type": "run", "ts": 1_792_000_000_000 + index, "executionId": "exec-%d" % index,
            "status": "completed", "error": None, "truncated": []}
    base.update(extra)
    padding = max(0, size - len(serialize(base)) - 12)
    base["pad"] = "x" * padding
    return base


class TestPacking:
    def test_envelope_shape_seq_and_qos_1(self):
        batcher, clock, publisher = make()
        batcher.enqueue(record(index=1))
        batcher.enqueue(record(index=2))
        assert batcher.drain() == 1
        topic, payload, qos = publisher.published[0]
        assert topic == TOPIC and qos == 1
        message = json.loads(payload)
        assert message["schema"] == "dda.debug.v1"
        assert message["seq"] == 1
        assert message["sessionId"] == ENVELOPE["sessionId"]
        assert message["instance"] == ENVELOPE["instance"]
        assert message["deviceId"] == "thing-1" and message["workflowId"] == "wf"
        assert message["workflowVersion"] == "3"
        assert message["sentAt"] == int((clock.now + 1_000_000_000) * 1000)
        assert [r["executionId"] for r in message["records"]] == ["exec-1", "exec-2"]
        assert batcher.counters["records"] == 2
        assert batcher.counters["bytes"] == len(payload)

    def test_messages_are_packed_up_to_100_kb(self):
        batcher, clock, publisher = make(max_messages_per_second=100, max_bytes_per_second=10_000_000)
        for i in range(30):
            batcher.enqueue(record(size=10 * 1024, index=i))
        batcher.drain()
        sizes = [len(p[1]) for p in publisher.published]
        assert all(size <= MESSAGE_SOFT_BYTES for size in sizes)
        assert len(publisher.published) == 4  # 9 + 9 + 9 + 3 records of ~10 KB
        counts = [len(m["records"]) for m in publisher.messages()]
        assert counts == [9, 9, 9, 3]
        assert [m["seq"] for m in publisher.messages()] == [1, 2, 3, 4]

    def test_at_most_200_records_per_message(self):
        batcher, clock, publisher = make(max_messages_per_second=100, max_bytes_per_second=10_000_000)
        for i in range(450):
            batcher.enqueue(record(size=50, index=i))
        batcher.drain()
        counts = [len(m["records"]) for m in publisher.messages()]
        assert counts == [200, 200, 50]

    def test_an_oversized_single_record_is_truncated_to_fit_and_the_hard_cap_holds(self):
        batcher, clock, publisher = make(max_bytes_per_second=10_000_000)
        logs = [{"ts": i, "level": "DEBUG", "logger": "l", "message": "m" * 400, "src": "tap"}
                for i in range(400)]
        batcher.enqueue(record(index=1, logs=logs))
        assert batcher.drain() == 1
        payload = publisher.published[0][1]
        assert len(payload) <= MESSAGE_SOFT_BYTES <= MESSAGE_HARD_BYTES
        message = json.loads(payload)
        sent = message["records"][0]
        assert len(sent["logs"]) < 400
        assert sent["truncated"][0]["field"] == "logs.tap"
        assert sent["truncated"][0]["originalBytes"] > 0
        assert batcher.counters["truncations"] == 1

    def test_a_record_that_does_not_fit_the_remaining_budget_opens_the_next_message(self):
        batcher, clock, publisher = make(max_messages_per_second=100, max_bytes_per_second=10_000_000)
        batcher.enqueue(record(size=60 * 1024, index=1))
        batcher.enqueue(record(size=60 * 1024, index=2))
        batcher.drain()
        assert [len(m["records"]) for m in publisher.messages()] == [1, 1]
        assert all(not m["records"][0]["truncated"] for m in publisher.messages())


class TestQueue:
    def test_drop_oldest_is_counted(self):
        batcher, clock, publisher = make(queue_maxlen=3)
        for i in range(5):
            batcher.enqueue(record(index=i))
        assert batcher.pending() == 3
        assert batcher.counters["queueDrops"] == 2
        batcher.drain()
        assert [r["executionId"] for r in publisher.messages()[0]["records"]] == ["exec-2", "exec-3", "exec-4"]

    def test_drain_respects_the_base_interval_unless_forced(self):
        batcher, clock, publisher = make()
        batcher.enqueue(record(index=1))
        assert batcher.drain() == 1
        batcher.enqueue(record(index=2))
        assert batcher.drain() == 0  # 0.5 s have not passed
        clock.advance(0.5)
        assert batcher.drain() == 1
        batcher.enqueue(record(index=3))
        assert batcher.drain(force=True) == 1


class TestBuckets:
    def test_token_bucket_refills_continuously_with_a_one_second_burst(self):
        clock = FakeClock()
        bucket = TokenBucket(10, clock)
        assert bucket.level() == 10
        assert bucket.take(10) and not bucket.take(1)
        clock.advance(0.25)
        assert bucket.level() == pytest.approx(2.5)
        clock.advance(5)
        assert bucket.level() == 10  # capped at one second's worth
        assert TokenBucket(10, clock, minimum_capacity=100).capacity == 100
        assert bucket.seconds_until(15) == pytest.approx(0.5)

    def test_100kb_batch_at_32768_bytes_per_second_waits_about_3_s(self):
        batcher, clock, publisher = make(max_bytes_per_second=32768)
        for i in range(10):
            batcher.enqueue(record(size=10 * 1024, index=i))  # about 100 KB
        assert batcher.drain() == 0
        assert batcher.counters["bytesDeferred"] == 1
        assert publisher.published == []
        sent_at = None
        for _ in range(80):  # 50 ms steps
            clock.advance(0.05)
            if batcher.drain(force=True):
                sent_at = clock.now - 1000.0
                break
        assert sent_at is not None
        # The bucket starts with one second's worth (32 KB) and refills at
        # 32 KB/s, so a ~100 KB message passes after about 2-3 s.
        assert 1.8 <= sent_at <= 3.2, sent_at
        assert len(publisher.published) == 1
        assert batcher.counters["bytesDeferred"] >= 1

    def test_sustained_volume_at_the_default_cap_stays_within_it(self):
        batcher, clock, publisher = make(max_bytes_per_second=262144, max_messages_per_second=20)
        total = 0
        for second in range(10):
            for i in range(12):  # about 1.1 MB offered per second, one record per message
                batcher.enqueue(record(size=90 * 1024, index=second * 100 + i))
            for _ in range(10):
                clock.advance(0.1)
                batcher.drain(force=True)
        total = sum(len(p[1]) for p in publisher.published)
        # 10 s at 256 KB/s plus the one-second burst the bucket started with.
        assert total <= 262144 * 11
        assert total >= 262144 * 8
        assert batcher.counters["bytesDeferred"] > 0

    def test_rate_deferred_at_the_message_cap(self):
        batcher, clock, publisher = make(max_messages_per_second=2, max_bytes_per_second=10_000_000)
        for i in range(5):
            batcher.enqueue(record(size=60 * 1024, index=i))  # one record per message
        assert batcher.drain() == 2
        assert batcher.counters["rateDeferred"] == 1
        assert batcher.pending() == 3
        clock.advance(1.0)
        assert batcher.drain() == 2
        clock.advance(1.0)
        assert batcher.drain() == 1
        assert [m["seq"] for m in publisher.messages()] == [1, 2, 3, 4, 5]


class TestSpoolFullBackOff:
    def test_three_rejections_double_the_interval_and_success_halves_it(self):
        batcher, clock, publisher = make()
        assert batcher.drain_interval_s == 0.5
        batcher.enqueue(record(index=1))
        batcher.drain()
        assert len(publisher.published) == 1
        intervals = []
        for n in range(1, 4):
            publisher.failures_by_reason["spool_full"] = n  # the publisher thread reported one more
            publisher.publish_failures = n
            clock.advance(batcher.drain_interval_s)
            batcher.enqueue(record(index=10 + n))
            batcher.drain()
            intervals.append(batcher.drain_interval_s)
        assert intervals == [1.0, 2.0, 4.0]
        assert batcher.counters["spoolFull"] == 3
        assert batcher.counters["publishFailures"] == 3
        # Each batch was handed over exactly once: nothing rejected is re-sent.
        assert len(publisher.published) == 4
        assert [m["records"][0]["executionId"] for m in publisher.messages()] == [
            "exec-1", "exec-11", "exec-12", "exec-13"]
        publisher.publish_successes = 1
        clock.advance(4.0)
        batcher.drain()
        assert batcher.drain_interval_s == 2.0
        publisher.publish_successes = 3
        clock.advance(2.0)
        batcher.drain()
        assert batcher.drain_interval_s == 0.5
        publisher.publish_successes = 4
        batcher.drain(force=True)
        assert batcher.drain_interval_s == 0.5

    def test_interval_is_capped_at_10_s(self):
        batcher, clock, publisher = make()
        publisher.failures_by_reason["spool_full"] = 8
        batcher.drain(force=True)
        assert batcher.drain_interval_s == 10.0
        assert batcher.counters["spoolFull"] == 8

    def test_while_backed_off_records_still_queue_with_drop_oldest(self):
        batcher, clock, publisher = make(queue_maxlen=2)
        publisher.failures_by_reason["spool_full"] = 1
        batcher.drain(force=True)
        for i in range(4):
            batcher.enqueue(record(index=i))
        assert batcher.pending() == 2
        assert batcher.counters["queueDrops"] == 2
        assert batcher.drain() == 0  # within the 1 s back-off
        clock.advance(1.0)
        assert batcher.drain() == 1

    def test_status_reports_the_state(self):
        batcher, clock, publisher = make()
        status = batcher.status()
        assert status["queued"] == 0 and status["seq"] == 0
        assert status["drainIntervalS"] == 0.5
        assert set(status["counters"]) >= {"records", "bytes", "queueDrops", "truncations",
                                           "rateDeferred", "bytesDeferred", "spoolFull",
                                           "publishFailures"}


def _big_record():
    return {
        "type": "run", "ts": 1, "executionId": "exec-big", "status": "failed",
        "error": "e" * 3000,
        "traceback": "t" * 6000,
        "trigger": {"source": "mqtt", "type": "mqtt", "topic": "t", "payload": "p" * 3000,
                    "timestamp": 1, "receivedMs": 1},
        "nodes": [{"nodeId": "n%d" % i, "status": "success", "durationMs": 1, "detail": "d" * 500}
                  for i in range(5)],
        "bindings": [{"nodeId": "b", "kind": "mqtt_publish", "result": "success", "detail": "b" * 500}],
        "outputs": {"detections": [{"label": "p", "confidence": 0.5, "box": [1, 2, 3, 4]}
                                   for _ in range(300)],
                    "llm": {"l1": {"text": "L" * 3000}},
                    "bedrock": {"b1": {"text": "B" * 3000}},
                    "isAnomalous": False, "confidence": 0.5},
        "logs": ([{"ts": i, "level": "DEBUG", "logger": "x", "message": "tap-%d-" % i + "m" * 100,
                   "src": "tap"} for i in range(50)]
                 + [{"ts": 100 + i, "level": "INFO", "logger": "y", "message": "run-%d-" % i + "m" * 100,
                     "src": "runlog"} for i in range(50)]),
        "truncated": [{"field": "logs.runlog", "originalBytes": 999}],
    }


class TestTruncationOrder:
    def test_order_tap_then_runlog_then_detections_then_texts_payload_traceback_details_error(self):
        full = _big_record()
        size = len(serialize(full))
        # Budget just below the record: only tap lines go.
        cut, markers = truncate_record(full, size - 100)
        assert [m["field"] for m in markers] == ["logs.tap"]
        assert all(e["src"] != "tap" or True for e in cut["logs"])
        assert len([e for e in cut["logs"] if e["src"] == "tap"]) < 50
        assert len([e for e in cut["logs"] if e["src"] == "runlog"]) == 50
        assert cut["truncated"][0] == {"field": "logs.runlog", "originalBytes": 999}  # seed kept
        assert full["logs"] and len(full["logs"]) == 100  # the input is untouched

        # A budget equal to the fully cut size forces every step (the error
        # cut is the one that makes it fit); the markers follow the §5.4 order.
        fully_cut = json.loads(serialize(full).decode("utf-8"))
        scratch = []
        fully_cut["logs"] = []
        batcher_module._cut_detections(fully_cut, scratch)
        batcher_module._cut_texts(fully_cut, scratch)
        batcher_module._cut_payload(fully_cut, scratch)
        batcher_module._cut_traceback(fully_cut, scratch)
        batcher_module._cut_details(fully_cut, "nodes", scratch)
        batcher_module._cut_details(fully_cut, "bindings", scratch)
        batcher_module._cut_error(fully_cut, scratch)
        budget = len(serialize(fully_cut)) + 2048  # room for the ten markers
        cut, markers = truncate_record(full, budget)
        fields = [m["field"] for m in markers]
        expected_prefix = ["logs.tap", "logs.runlog", "outputs.detections", "outputs.llm.l1.text",
                           "outputs.bedrock.b1.text", "trigger.payload", "traceback", "nodes[].detail",
                           "bindings[].detail", "error"]
        assert fields == expected_prefix
        assert len(serialize(cut)) <= budget
        assert len(cut["outputs"]["detections"]) == 100
        assert len(cut["outputs"]["llm"]["l1"]["text"]) == 1024
        assert len(cut["outputs"]["bedrock"]["b1"]["text"]) == 1024
        assert len(cut["trigger"]["payload"]) == 1024
        assert len(cut["traceback"]) == 2048 and cut["traceback"] == "t" * 2048
        assert all(len(n["detail"]) == 200 for n in cut["nodes"])
        assert len(cut["bindings"][0]["detail"]) == 200
        assert len(cut["error"]) == 1024
        assert all(m["originalBytes"] > 0 for m in markers)

    def test_markers_land_in_the_record_truncated_list(self):
        cut, markers = truncate_record(_big_record(), 20_000)
        assert cut["truncated"][0] == {"field": "logs.runlog", "originalBytes": 999}
        assert cut["truncated"][1:] == markers
        assert len(serialize(cut)) <= 20_000

    def test_record_still_too_large_is_replaced(self):
        record = _big_record()
        record["blob"] = "z" * (MESSAGE_HARD_BYTES + 10_000)  # not a cuttable field
        cut, markers = truncate_record(record, MESSAGE_HARD_BYTES)
        assert cut["error"] == "[record too large]"
        assert cut["executionId"] == "exec-big" and cut["status"] == "failed" and cut["type"] == "run"
        assert cut["truncated"][-1]["field"] == "record"
        assert markers[-1]["field"] == "record"
        assert len(serialize(cut)) < 4096

    def test_a_record_within_budget_is_returned_unchanged(self):
        record = _big_record()
        cut, markers = truncate_record(record, len(serialize(record)))
        assert markers == []
        assert cut == record

    def test_budget_is_capped_at_the_hard_cap(self):
        record = {"type": "run", "executionId": "e", "status": "completed",
                  "logs": [{"ts": i, "level": "DEBUG", "logger": "l", "message": "m" * 1000, "src": "tap"}
                           for i in range(200)], "truncated": []}
        cut, markers = truncate_record(record, 10 * MESSAGE_HARD_BYTES)
        assert len(serialize(cut)) <= MESSAGE_HARD_BYTES
        assert markers[0]["field"] == "logs.tap"


def test_module_constants():
    assert batcher_module.MESSAGE_SOFT_BYTES == 100 * 1024
    assert batcher_module.MESSAGE_HARD_BYTES == 120 * 1024
    assert batcher_module.MAX_RECORDS_PER_MESSAGE == 200
    assert batcher_module.BASE_DRAIN_INTERVAL_S == 0.5
    assert batcher_module.MAX_DRAIN_INTERVAL_S == 10.0
