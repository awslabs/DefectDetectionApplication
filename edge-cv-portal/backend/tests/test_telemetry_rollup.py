"""``telemetry_rollup`` (workflow-telemetry-dashboards design §7.3; tasks.md
3.3) on moto tables: one record of each kind fed and only ``S#``/``E#``
produce work; a minute recomputed from a window with a duplicate and an
out-of-order ``seq`` folds once and reports the authoritative ``gaps``; the
hour and day items are recomputed and idempotent across a retried batch; a
boundary Summary lands in the preceding minute; expired items are skipped.
"""
import sys
import uuid
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer

import telemetry_tables
from telemetry import fold, records

INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
OTHER_INSTANCE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
T0 = 1_792_000_000_000
MINUTE = fold.bucket_minute("event", T0)          # the minute containing T0
NOW_S = (MINUTE + 3 * 60_000) / 1000.0            # three minutes later
_serializer = TypeSerializer()


class Rollup:
    def __init__(self, module):
        self.module = module
        self.device = "dev-" + str(uuid.uuid4())[-8:]
        self.pk = records.pair_pk(UC, self.device, WF)
        self.tables = telemetry_tables.tables()

    def put_summary(self, seq, *, event_ms, received_ms=None, runs=(1, 0, 0), instance=INSTANCE, idle=False,
                    expires_at=None, message_id=None, latency=None, per_class=None):
        received_ms = received_ms if received_ms is not None else event_ms + 400
        fields = {"schema": "dda.summary.v1", "intervalStart": event_ms - 10_000, "intervalEnd": event_ms,
                  "emittedAt": event_ms + 50, "deviceId": self.device, "workflowId": WF, "workflowVersion": "1",
                  "producer": {"name": "engine", "version": "1", "instance": instance}, "seq": seq, "intervalS": 10,
                  "idle": idle, "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}}
        if not idle:
            fields["runs"] = {"completed": runs[0], "failed": runs[1], "dropped": runs[2]}
            fields["model"] = {"state": "READY", "models": {}, "waits": 0, "waitedMs": 0}
            fields["detections"] = {"runsWithDetections": 1, "total": seq,
                                    "perClass": per_class if per_class is not None else {"k": {"total": seq, "max": seq}},
                                    "truncated": False}
            if latency is not None:
                fields["latency"] = latency
        item = {"pk": self.pk, "sk": records.summary_sk(received_ms, message_id or str(uuid.uuid4())), "summary": fields,
                "receivedMs": received_ms, "eventMs": event_ms, "tsSource": "device", "producer": "engine",
                "instance": instance, "seq": seq, "gap": 0, "idle": idle,
                "expires_at": expires_at if expires_at is not None else int(NOW_S) + 48 * 3600}
        self.tables["summaries"].put_item(Item=item)
        return item

    def put_event(self, event_type, event_ms, expires_at=None):
        event_id = str(uuid.uuid4())
        item = {"pk": self.pk, "sk": records.event_sk(event_ms, event_id), "type": event_type, "eventId": event_id,
                "deviceId": self.device, "receivedMs": event_ms + 100, "eventMs": event_ms, "tsSource": "device",
                "gsi1pk": records.by_workflow_pk(UC, WF), "gsi1sk": records.by_workflow_sk(event_ms, self.device, event_id),
                "event": {"type": event_type}, "expires_at": expires_at if expires_at is not None else int(NOW_S) + 30 * 86400}
        self.tables["events"].put_item(Item=item)
        return item

    @staticmethod
    def stream_record(item, event_name="INSERT", sequence=None, keys_only=False):
        body = {"Keys": {"pk": _serializer.serialize(item["pk"]), "sk": _serializer.serialize(item["sk"])},
                "SequenceNumber": sequence or str(uuid.uuid4().int)[:20]}
        if not keys_only and event_name != "REMOVE":
            body["NewImage"] = {k: _serializer.serialize(v) for k, v in item.items() if k in ("pk", "sk", "eventMs", "seq")}
        return {"eventName": event_name, "eventID": str(uuid.uuid4()), "dynamodb": body}

    def run(self, items, clock=lambda: NOW_S):
        return self.module.process({"Records": items}, clock=clock)

    def aggregate(self, sk):
        return self.tables["aggregates"].get_item(Key={"pk": self.pk, "sk": sk}).get("Item")

    def aggregates(self):
        return self.tables["aggregates"].query(KeyConditionExpression=Key("pk").eq(self.pk))["Items"]


@pytest.fixture
def rollup(aws_stack, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    for name in list(sys.modules):
        if name in ("telemetry_rollup", "telemetry") or name.startswith("telemetry."):
            sys.modules.pop(name, None)
    import telemetry_rollup as module
    module.reset_clients()
    return Rollup(module)


def measures(item):
    return {k: (int(v) if isinstance(v, Decimal) and v % 1 == 0 else v) for k, v in item["measures"].items()}


# --------------------------------------------------------------------------- the skip rule
class TestSkipRule:
    def test_only_summary_and_event_records_produce_work(self, rollup):
        summary = rollup.put_summary(1, event_ms=MINUTE + 10_000)
        event = rollup.put_event("clip_ready", MINUTE + 20_000)
        pointer = {"pk": rollup.pk, "sk": "LAST#engine", "eventMs": MINUTE + 10_000, "seq": 1}
        heartbeat_pointer = {"pk": rollup.pk, "sk": "LASTHB#engine", "eventMs": MINUTE + 10_000, "seq": 1}
        live = {"pk": rollup.pk, "sk": "LIVE", "eventMs": MINUTE + 10_000, "seq": 1}
        clip = {"pk": "C#{0}#{1}".format(UC, WF), "sk": "{0:013d}#dev#clip".format(MINUTE), "eventMs": MINUTE}
        removed = rollup.stream_record(summary, event_name="REMOVE")
        result = rollup.run([rollup.stream_record(summary), rollup.stream_record(event, event_name="MODIFY"),
                             rollup.stream_record(pointer), rollup.stream_record(heartbeat_pointer),
                             rollup.stream_record(live), rollup.stream_record(clip), removed,
                             {"eventName": "INSERT", "dynamodb": {}}])
        assert result["batchItemFailures"] == []
        assert result["skipped"] == 6
        assert len(result["recomputed"]) == 1 and result["recomputed"][0]["minute"] == "M#" + fold.minute_key(MINUTE)
        assert len(rollup.aggregates()) == 3

    def test_work_item(self, rollup):
        wi = rollup.module.work_item
        item = {"pk": rollup.pk, "sk": records.summary_sk(MINUTE + 60_400, "abcdef01"), "eventMs": MINUTE + 60_000}
        assert wi(rollup.stream_record(item)) == (rollup.pk, MINUTE), "a boundary Summary buckets into the preceding minute"
        event = {"pk": rollup.pk, "sk": records.event_sk(MINUTE + 60_000, "e"), "eventMs": MINUTE + 60_000}
        assert wi(rollup.stream_record(event)) == (rollup.pk, MINUTE + 60_000)
        assert wi(rollup.stream_record(event, keys_only=True)) == (rollup.pk, MINUTE + 60_000), "an Event key carries eventMs"
        assert wi(rollup.stream_record(event, event_name="REMOVE")) is None
        assert wi({"eventName": "INSERT", "dynamodb": {"Keys": {"pk": {"S": "C#x"}, "sk": {"S": "0#d#c"}}}}) is None
        assert wi({"eventName": "INSERT", "dynamodb": {"Keys": {"pk": {"S": rollup.pk}, "sk": {"S": "LIVE"}}}}) is None
        assert wi({}) is None


# --------------------------------------------------------------------------- the fold
class TestMinute:
    def test_a_window_with_a_duplicate_and_an_out_of_order_seq_folds_once_with_authoritative_gaps(self, rollup):
        a = rollup.put_summary(5, event_ms=MINUTE + 10_000, runs=(5, 0, 0))
        rollup.put_summary(7, event_ms=MINUTE + 30_000, runs=(7, 0, 0), received_ms=MINUTE + 30_400)
        rollup.put_summary(6, event_ms=MINUTE + 20_000, runs=(6, 0, 0), received_ms=MINUTE + 31_000)   # arrived late
        rollup.put_summary(6, event_ms=MINUTE + 20_000, runs=(6, 0, 0), received_ms=MINUTE + 32_000)   # retransmitted
        rollup.put_summary(9, event_ms=MINUTE + 50_000, runs=(9, 0, 0))                                 # seq 8 lost
        rollup.put_event("clip_ready", MINUTE + 15_000)
        rollup.put_event("clip_ready", MINUTE + 59_999)
        rollup.put_event("run_error", MINUTE + 60_000)      # the next minute
        result = rollup.run([rollup.stream_record(a)])
        assert result["batchItemFailures"] == []
        minute = rollup.aggregate("M#" + fold.minute_key(MINUTE))
        m = measures(minute)
        assert m["runs_completed"] == 27 and m["summaries"] == 4 and minute["summariesFolded"] == 4
        assert m["gaps"] == 1 and minute["gaps"] == 1
        assert minute["events"] == {"clip_ready": 2} and m["events_clip_ready"] == 2
        assert m["detections_class_total"] == {"k": 27} and m["detections_class_max"] == {"k": 9}
        assert minute["expires_at"] == int(NOW_S) + 7 * 86400

    def test_a_boundary_summary_lands_in_the_preceding_minute(self, rollup):
        boundary = rollup.put_summary(3, event_ms=MINUTE + 60_000, runs=(3, 0, 0), received_ms=MINUTE + 60_300)
        rollup.put_summary(4, event_ms=MINUTE + 70_000, runs=(4, 0, 0))
        result = rollup.run([rollup.stream_record(boundary)])
        assert result["recomputed"][0]["minute"] == "M#" + fold.minute_key(MINUTE)
        assert measures(rollup.aggregate("M#" + fold.minute_key(MINUTE)))["runs_completed"] == 3
        assert rollup.aggregate("M#" + fold.minute_key(MINUTE + 60_000)) is None

    def test_a_summary_received_late_is_found_through_the_window(self, rollup):
        late = rollup.put_summary(2, event_ms=MINUTE + 10_000, runs=(2, 0, 0), received_ms=MINUTE + 10_000 + 290_000)
        rollup.run([rollup.stream_record(late)])
        assert measures(rollup.aggregate("M#" + fold.minute_key(MINUTE)))["runs_completed"] == 2

    def test_expired_items_are_skipped(self, rollup):
        live = rollup.put_summary(1, event_ms=MINUTE + 10_000, runs=(1, 0, 0))
        rollup.put_summary(2, event_ms=MINUTE + 20_000, runs=(50, 0, 0), expires_at=int(NOW_S) - 1)
        rollup.put_event("clip_ready", MINUTE + 15_000, expires_at=int(NOW_S) - 1)
        rollup.run([rollup.stream_record(live)])
        minute = rollup.aggregate("M#" + fold.minute_key(MINUTE))
        assert measures(minute)["runs_completed"] == 1 and minute["events"] == {}
        # An expired minute item is left out of the hour.
        rollup.tables["aggregates"].put_item(Item={"pk": rollup.pk, "sk": "M#" + fold.minute_key(MINUTE + 60_000),
                                                   "measures": {"runs_completed": 500, "summaries": 1, "gaps": 0},
                                                   "events": {}, "summariesFolded": 1, "gaps": 0, "weights": {},
                                                   "expires_at": int(NOW_S) - 1})
        rollup.run([rollup.stream_record(live)])
        assert measures(rollup.aggregate("H#" + fold.hour_key(MINUTE)))["runs_completed"] == 1

    def test_heartbeats_fold_their_counters_only(self, rollup):
        live = rollup.put_summary(1, event_ms=MINUTE + 10_000, runs=(1, 0, 0))
        hb = rollup.put_summary(2, event_ms=MINUTE + 20_000, idle=True)
        hb["summary"]["telemetry"]["publishFailures"] = 3
        rollup.tables["summaries"].put_item(Item=hb)
        rollup.run([rollup.stream_record(live)])
        m = measures(rollup.aggregate("M#" + fold.minute_key(MINUTE)))
        assert m["summaries"] == 1 and m["telemetry_publish_failures"] == 3 and m["runs_completed"] == 1

    def test_a_minute_with_a_non_integral_mean_is_stored_at_all_three_levels(self, rollup):
        # n-weighted p50 mean (100 * 1 + 150 * 2) / 3 = 133.333...: the fold yields a float, which the
        # DynamoDB resource refuses unless the roll-up converts it; a per-key max of 2.5 covers the nested maps.
        first = rollup.put_summary(1, event_ms=MINUTE + 10_000, runs=(1, 0, 0),
                                   latency={"p50Ms": 100, "p95Ms": 180, "maxMs": 200, "n": 1},
                                   per_class={"k": {"total": 1, "max": Decimal("2.5")}})
        rollup.put_summary(2, event_ms=MINUTE + 20_000, runs=(2, 0, 0),
                           latency={"p50Ms": 150, "p95Ms": 190, "maxMs": 210, "n": 2})
        result = rollup.run([rollup.stream_record(first)])
        assert result["batchItemFailures"] == []
        expected = (100 * 1 + 150 * 2) / 3
        for sk in ("M#" + fold.minute_key(MINUTE), "H#" + fold.hour_key(MINUTE), "D#" + fold.day_key(MINUTE)):
            item = rollup.aggregate(sk)
            assert item is not None, sk
            stored = item["measures"]["latency_p50_ms"]
            assert isinstance(stored, Decimal), sk
            assert abs(float(stored) - expected) < 1e-6, sk
            assert item["measures"]["detections_class_max"] == {"k": Decimal("2.5")}, sk
            assert item["measures"]["latency_p95_ms"] == 190 and item["measures"]["latency_max_ms"] == 210, sk
            assert item["weights"]["latency_p50_ms"] == 3, sk
            assert measures(item)["runs_completed"] == 3 and item["summariesFolded"] == 2, sk


class TestHourAndDay:
    def test_hour_and_day_items_are_recomputed_and_idempotent_across_a_retried_batch(self, rollup):
        first = rollup.put_summary(1, event_ms=MINUTE + 10_000, runs=(2, 1, 0))
        second = rollup.put_summary(2, event_ms=MINUTE + 60_000 + 10_000, runs=(3, 0, 1))
        rollup.put_event("violation_confirmed", MINUTE + 5_000)
        batch = [rollup.stream_record(first, sequence="1"), rollup.stream_record(second, sequence="2")]
        result = rollup.run(batch)
        assert result["batchItemFailures"] == []
        hour = rollup.aggregate("H#" + fold.hour_key(MINUTE))
        day = rollup.aggregate("D#" + fold.day_key(MINUTE))
        assert measures(hour)["runs_completed"] == 5 and measures(hour)["runs_failed"] == 1 and measures(hour)["runs_dropped"] == 1
        assert measures(hour)["summaries"] == 2 and hour["events"] == {"violation_confirmed": 1}
        assert measures(day) == measures(hour)
        assert hour["expires_at"] == int(NOW_S) + 400 * 86400 and day["expires_at"] == int(NOW_S) + 400 * 86400
        before = rollup.aggregates()
        again = rollup.run(batch)
        assert again["batchItemFailures"] == []
        after = rollup.aggregates()
        assert len(after) == len(before) == 4   # two minutes, one hour, one day
        for item in after:
            item.pop("computedAt")
        for item in before:
            item.pop("computedAt")
        assert sorted(after, key=lambda i: i["sk"]) == sorted(before, key=lambda i: i["sk"])

    def test_a_day_crossing(self, rollup):
        day_start = fold.day_of_key(fold.day_key(MINUTE)) + fold.DAY_MS
        before = rollup.put_summary(1, event_ms=day_start - 10_000, runs=(1, 0, 0), received_ms=day_start - 9_600)
        after = rollup.put_summary(2, event_ms=day_start + 10_000, runs=(2, 0, 0), received_ms=day_start + 10_400)
        rollup.run([rollup.stream_record(before), rollup.stream_record(after)], clock=lambda: day_start / 1000.0 + 600)
        assert measures(rollup.aggregate("D#" + fold.day_key(day_start - 1)))["runs_completed"] == 1
        assert measures(rollup.aggregate("D#" + fold.day_key(day_start)))["runs_completed"] == 2

    def test_a_failed_pair_reports_its_records_and_the_others_proceed(self, rollup, monkeypatch):
        good = rollup.put_summary(1, event_ms=MINUTE + 10_000)
        other = Rollup(rollup.module)
        bad = other.put_summary(1, event_ms=MINUTE + 10_000)
        original = rollup.module.recompute_minute

        def failing(pk, minute, now_s):
            if pk == other.pk:
                raise RuntimeError("boom")
            return original(pk, minute, now_s)

        monkeypatch.setattr(rollup.module, "recompute_minute", failing)
        result = rollup.run([rollup.stream_record(good, sequence="11"), rollup.stream_record(bad, sequence="22"),
                             rollup.stream_record(bad, sequence="23")])
        assert result["batchItemFailures"] == [{"itemIdentifier": "22"}, {"itemIdentifier": "23"}]
        assert rollup.aggregate("M#" + fold.minute_key(MINUTE)) is not None
        assert rollup.module.handler({"Records": [rollup.stream_record(good)]}, None) == {"batchItemFailures": []}

    def test_a_write_that_raises_reports_the_pair_and_leaves_no_partial_levels(self, rollup, monkeypatch):
        good = rollup.put_summary(1, event_ms=MINUTE + 10_000)
        other = Rollup(rollup.module)
        bad = other.put_summary(1, event_ms=MINUTE + 10_000)
        original = rollup.module._put

        def failing_put(pk, sk, folded, ttl_seconds, now_s, computed_ms):
            if pk == other.pk:
                raise TypeError("Float types are not supported. Use Decimal types instead.")
            return original(pk, sk, folded, ttl_seconds, now_s, computed_ms)

        monkeypatch.setattr(rollup.module, "_put", failing_put)
        result = rollup.run([rollup.stream_record(good, sequence="31"), rollup.stream_record(bad, sequence="32")])
        assert result["batchItemFailures"] == [{"itemIdentifier": "32"}]
        assert len(result["recomputed"]) == 1 and result["recomputed"][0]["summariesFolded"] == 1
        assert len(rollup.aggregates()) == 3 and other.aggregates() == []
