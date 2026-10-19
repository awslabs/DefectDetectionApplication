"""``telemetry.records`` (workflow-telemetry-dashboards design §4.4, §6.5,
§7.1, §7.2, §7.5; tasks.md 3.2): the ``eventMs`` rule by kind, the property
that every accepted Summary's receive-time key lies inside the roll-up's
query window for its ``eventMs`` minute, the key layouts, the item builders
and the lake record.
"""
import json
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry import attribution, fold, records

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
DEVICE = "mic-730-demo"
T0 = 1_792_000_000_000
TOL = records.SUMMARY_EVENT_TIME_TOLERANCE_MS


def record(kind="summary", fields=None, *, received_ms=T0 + 10_400, message_id="aaaabbbb-0000-4000-8000-00000000000a",
           seq=7, producer="engine", instance=INSTANCE):
    if fields is None:
        fields = {"schema": "dda.summary.v1", "intervalStart": T0, "intervalEnd": T0 + 10_000,
                  "emittedAt": T0 + 10_100, "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
                  "producer": {"name": producer, "version": "1.0.0", "instance": instance}, "seq": seq,
                  "intervalS": 10, "idle": False,
                  "runs": {"completed": 1, "failed": 0, "dropped": 0},
                  "detections": {"runsWithDetections": 1, "total": 2, "perClass": {"helmet": {"total": 2, "max": 2}},
                                 "truncated": False},
                  "model": {"state": "READY", "models": {"m": "READY"}, "waits": 0, "waitedMs": 0},
                  "ext": {"ppe": {"frames": 1}},
                  "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}}
    event_ms, ts_source = records.event_time(kind, fields, received_ms)
    schema = {"summary": "dda.summary.v1", "event": "dda.event.v1", "live": "dda.live.v1"}[kind]
    return attribution.AcceptedRecord(
        usecase_id=UC, device_id=DEVICE, workflow_id=WF, workflow_version=str(fields.get("workflowVersion", "")),
        kind=kind, schema=schema, producer=producer, instance=instance, seq=fields.get("seq", seq),
        event_ms=event_ms, received_ms=received_ms, ts_source=ts_source, message_id=message_id, fields=fields,
        dropped=[], blocks_dropped=[], raw=fields, raw_text=json.dumps(fields), usecase={"usecase_id": UC},
        account="222222222222")


# --------------------------------------------------------------------------- the eventMs rule
class TestEventTime:
    def test_the_shared_tolerance(self):
        assert records.SUMMARY_EVENT_TIME_TOLERANCE_MS == 300_000
        assert records.SUMMARY_EVENT_TIME_TOLERANCE_MS is fold.SUMMARY_EVENT_TIME_TOLERANCE_MS

    def test_summary_within_five_minutes_uses_interval_end(self):
        r = T0 + 10_400
        assert records.event_time("summary", {"intervalEnd": T0 + 10_000}, r) == (T0 + 10_000, "device")
        assert records.event_time("summary", {"intervalEnd": r - TOL}, r) == (r - TOL, "device")
        assert records.event_time("summary", {"intervalEnd": r + TOL}, r) == (r + TOL, "device")

    def test_summary_beyond_five_minutes_uses_receive_time(self):
        r = T0 + 10_400
        assert records.event_time("summary", {"intervalEnd": r - TOL - 1}, r) == (r, "received")
        assert records.event_time("summary", {"intervalEnd": r + TOL + 1}, r) == (r, "received")
        assert records.event_time("summary", {}, r) == (r, "received")

    def test_event_within_24h_before_and_5_min_after(self):
        r = T0
        assert records.event_time("event", {"ts": r - 86_400_000}, r) == (r - 86_400_000, "device")
        assert records.event_time("event", {"ts": r - 86_400_001}, r) == (r, "received")
        assert records.event_time("event", {"ts": r + 300_000}, r) == (r + 300_000, "device")
        assert records.event_time("event", {"ts": r + 300_001}, r) == (r, "received")

    def test_live_ts_is_informational(self):
        assert records.event_time("live", {"ts": 5}, T0) == (5, "device")
        assert records.event_time("live", {}, T0) == (T0, "received")


@settings(max_examples=200, deadline=None)
@given(received_ms=st.integers(min_value=1_600_000_000_000, max_value=1_900_000_000_000),
       offset=st.integers(min_value=-2 * 86_400_000, max_value=2 * 86_400_000))
def test_every_accepted_summary_lies_inside_the_rollup_query_window(received_ms, offset):
    """The property of §4.4: with ``eventMs`` from the rule, the receive-time
    key ``S#<receivedMs>`` is within ``[minute - 5 min, minute end + 5 min]``
    of the minute the Summary buckets into, so the roll-up's Query finds it."""
    event_ms, _source = records.event_time("summary", {"intervalEnd": received_ms + offset}, received_ms)
    minute = fold.bucket_minute("summary", event_ms)
    window_start = minute - fold.SUMMARY_EVENT_TIME_TOLERANCE_MS
    window_end = minute + fold.MINUTE_MS + fold.SUMMARY_EVENT_TIME_TOLERANCE_MS
    assert window_start <= received_ms <= window_end
    assert records.summary_sk(window_start, "0" * 8) <= records.summary_sk(received_ms, "f" * 8) <= records.summary_sk(window_end, "f" * 8)


# --------------------------------------------------------------------------- keys
class TestKeys:
    def test_summary_store_keys(self):
        assert records.pair_pk(UC, DEVICE, WF) == "P#{0}#{1}#{2}".format(UC, DEVICE, WF)
        assert records.pair_of_pk(records.pair_pk(UC, DEVICE, WF)) == (UC, DEVICE, WF)
        assert records.pair_of_pk("C#x#y") is None and records.pair_of_pk("P#a#b") is None
        assert records.summary_sk(T0 + 10_400, "aaaabbbb-0000-4000-8000-00000000000a") == "S#1792000010400#aaaabbbb"
        assert records.summary_sk(5, "ABCDEF0123") == "S#0000000000005#abcdef01"
        assert records.last_sk("engine") == "LAST#engine"
        assert records.lasthb_sk("ppe-clipper") == "LASTHB#ppe-clipper"
        assert records.LIVE_SK == "LIVE"
        # LAST* pointers share the prefix the now route queries (sk BEGINS_WITH 'LAST').
        assert records.last_sk("x").startswith("LAST") and records.lasthb_sk("x").startswith("LAST")
        assert records.frame_token(INSTANCE, 0) == INSTANCE + ":0"

    def test_event_store_keys(self):
        event_id = str(uuid.uuid4())
        assert records.event_sk(T0, event_id) == "E#{0:013d}#{1}".format(T0, event_id)
        assert records.by_workflow_pk(UC, WF) == "W#{0}#{1}".format(UC, WF)
        assert records.by_workflow_sk(T0, DEVICE, event_id) == "{0:013d}#{1}#{2}".format(T0, DEVICE, event_id)
        assert records.clip_index_pk(UC, WF) == "C#{0}#{1}".format(UC, WF)
        assert records.clip_index_sk(T0, DEVICE, "clip-1") == "{0:013d}#{1}#clip-1".format(T0, DEVICE)

    def test_thirteen_digit_timestamps_sort_lexically(self):
        assert records.summary_sk(999, "a") < records.summary_sk(1000, "a") < records.summary_sk(T0, "a")
        assert records.event_sk(T0, "a") < records.event_sk(T0 + 1, "a")

    def test_clip_retention_days(self, monkeypatch):
        monkeypatch.delenv("TELEMETRY_CLIP_RETENTION_DAYS", raising=False)
        assert records.clip_retention_days() == 90
        monkeypatch.setenv("TELEMETRY_CLIP_RETENTION_DAYS", "30")
        assert records.clip_retention_days() == 30
        monkeypatch.setenv("TELEMETRY_CLIP_RETENTION_DAYS", "junk")
        assert records.clip_retention_days() == 90


# --------------------------------------------------------------------------- items
class TestItems:
    def test_summary_item(self):
        item = records.summary_item(record(), gap=2)
        assert item["pk"] == records.pair_pk(UC, DEVICE, WF)
        assert item["sk"] == "S#1792000010400#aaaabbbb"
        assert item["summary"]["runs"]["completed"] == 1
        assert item["receivedMs"] == T0 + 10_400 and item["eventMs"] == T0 + 10_000 and item["tsSource"] == "device"
        assert item["producer"] == "engine" and item["instance"] == INSTANCE and item["seq"] == 7
        assert item["gap"] == 2 and item["idle"] is False and item["messageId"] == "aaaabbbb-0000-4000-8000-00000000000a"
        assert item["expires_at"] == (T0 + 10_400) // 1000 + 48 * 3600

    def test_event_item(self):
        event_id = str(uuid.uuid4())
        fields = {"schema": "dda.event.v1", "type": "violation_confirmed", "eventId": event_id, "ts": T0 + 10_000,
                  "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
                  "producer": {"name": "ppe-clipper", "version": "1", "instance": INSTANCE}, "severity": "warning",
                  "data": {"reasons": {"no-helmet": 1}}}
        item = records.event_item(record("event", fields, producer="ppe-clipper", seq=None))
        assert item["sk"] == records.event_sk(T0 + 10_000, event_id)
        assert item["gsi1pk"] == "W#{0}#{1}".format(UC, WF)
        assert item["gsi1sk"] == records.by_workflow_sk(T0 + 10_000, DEVICE, event_id)
        assert item["type"] == "violation_confirmed" and item["deviceId"] == DEVICE and item["eventId"] == event_id
        assert item["event"]["data"] == {"reasons": {"no-helmet": 1}}
        assert item["expires_at"] == (T0 + 10_400) // 1000 + 30 * 86400

    def test_clip_index_item(self, monkeypatch):
        monkeypatch.setenv("TELEMETRY_CLIP_RETENTION_DAYS", "90")
        fields = {"schema": "dda.event.v1", "type": "clip_ready", "eventId": str(uuid.uuid4()), "ts": T0,
                  "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
                  "producer": {"name": "ppe-clipper", "version": "1", "instance": INSTANCE}, "severity": "info",
                  "data": {"clipId": "clip-1", "revision": 2, "startTs": T0 - 20_000, "endTs": T0 - 8_000,
                           "durationS": 12.0, "reasons": {"no-helmet": {"frames": 3, "objects": 3}},
                           "totals": {"perClass": {"human": 4}}, "file": {"name": "c.mp4", "bytes": 10},
                           "s3": {"bucket": "b", "key": "k"}, "s3Error": None}}
        media = {"bucket": "b", "key": "k", "sidecarKey": None, "state": "bound", "reason": None}
        item = records.clip_index_item(record("event", fields, producer="ppe-clipper", seq=None), media)
        assert item["pk"] == "C#{0}#{1}".format(UC, WF)
        assert item["sk"] == records.clip_index_sk(T0 - 20_000, DEVICE, "clip-1")
        assert item["revision"] == 2 and item["media"] == media and item["startTs"] == T0 - 20_000
        assert item["expires_at"] == (T0 + 10_400) // 1000 + 90 * 86400

    def test_live_pointer_values(self):
        fields = {"schema": "dda.live.v1", "ts": T0, "instance": INSTANCE, "seq": 4000, "deviceId": DEVICE,
                  "workflowId": WF, "frame": {"bucket": "b", "key": "k/0.jpg", "width": 640, "height": 360, "bytes": 999},
                  "perClass": {"helmet": 1}, "people": 1}
        values = records.live_pointer_values(record("live", fields, seq=4000, producer="live"))
        assert values["frameToken"] == INSTANCE + ":4000" and values["seq"] == 4000 and values["instance"] == INSTANCE
        assert values["key"] == "k/0.jpg" and values["bytes"] == 999 and values["receivedMs"] == T0 + 10_400
        assert values["perClass"] == {"helmet": 1} and values["people"] == 1 and "violation" not in values
        assert values["expires_at"] == (T0 + 10_400) // 1000 + 24 * 3600


# --------------------------------------------------------------------------- the lake record
class TestLakeRecord:
    def test_summary_lake_record(self):
        rec = record()
        lake = records.lake_record(rec, rec.raw_text)
        assert lake["v"] == 1
        assert lake["message_id"] == "aaaabbbb-0000-4000-8000-00000000000a"
        assert lake["usecase_id"] == UC and lake["device_id"] == DEVICE and lake["workflow_id"] == WF
        assert lake["workflow_version"] == "3" and lake["kind"] == "summary" and lake["schema"] == "dda.summary.v1"
        assert lake["producer"] == "engine" and lake["instance"] == INSTANCE and lake["seq"] == 7
        assert lake["received_ms"] == T0 + 10_400 and lake["event_ms"] == T0 + 10_000 and lake["ts_source"] == "device"
        # Flattened one level.
        assert lake["runs_completed"] == 1 and lake["runs_failed"] == 0 and lake["runs_dropped"] == 0
        assert lake["per_class"] == {"helmet": {"total": 2, "max": 2}}
        assert lake["detections_total"] == 2 and lake["detections_runs_with_detections"] == 1
        assert lake["model_state"] == "READY" and lake["model_models"] == {"m": "READY"}
        assert lake["telemetry_queue_drops"] == 0
        assert lake["ext"] == {"ppe": {"frames": 1}}
        assert lake["interval_start"] == T0 and lake["interval_end"] == T0 + 10_000 and lake["interval_s"] == 10
        assert lake["idle"] is False
        assert lake["raw"] == rec.raw_text and json.loads(lake["raw"])["seq"] == 7
        # The identity fields are not duplicated under their camelCase names.
        for absent in ("deviceId", "workflowId", "workflowVersion", "device_id_", "producer_name"):
            assert absent not in lake

    def test_event_lake_record_keeps_data_as_a_map(self):
        event_id = str(uuid.uuid4())
        fields = {"schema": "dda.event.v1", "type": "run_error", "eventId": event_id, "ts": T0 + 10_000,
                  "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
                  "producer": {"name": "engine", "version": "1", "instance": INSTANCE}, "severity": "error",
                  "executionId": "exec-1", "data": {"error": "boom", "count": 1}}
        rec = record("event", fields, seq=None)
        lake = records.lake_record(rec, rec.raw_text)
        assert lake["kind"] == "event" and lake["type"] == "run_error" and lake["ts"] == T0 + 10_000
        assert lake["severity"] == "error" and lake["execution_id"] == "exec-1"
        assert lake["data"] == {"error": "boom", "count": 1} and lake["seq"] is None
        assert "eventId" not in lake and lake["event_ms"] == T0 + 10_000

    def test_snake_case(self):
        assert records.snake_case("runsWithDetections") == "runs_with_detections"
        assert records.snake_case("intervalS") == "interval_s"
        assert records.snake_case("already_snake") == "already_snake"
        assert records.snake_case("p50Ms") == "p50_ms"

    def test_the_record_is_json_serializable(self):
        rec = record()
        json.dumps(records.lake_record(rec, rec.raw_text))
