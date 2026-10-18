"""``telemetry_ingest`` (workflow-telemetry-dashboards design §6; tasks.md 3.3)
on moto tables with the GSIs and stubbed identity and Firehose clients.

The accept path writes the item, both pointers and the lake record; a Lambda
retry of the same ``r``/``m`` overwrites itself; a retransmission (same
``instance``/``seq``, new ``m``) is stored, counted ``duplicate`` and the
pointer unchanged; ``lastRunMs`` moves only for a Summary with runs and a
Heartbeat never blanks ``LAST#``; the ``LIVE`` pointer across two agent
instances; Clip_Index revisions; every rejection reason counted with a sample
and no payload in the logs; a Firehose failure after two retries does not
fail the message; ``rate_exceeded``; ``block_not_owned``; captured logs carry
no payload string over 256 characters.
"""
import base64
import json
import logging
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import telemetry_tables

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
OTHER_INSTANCE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e"
PR = "ab" * 32
T0 = 1_792_000_000_000
NOW = T0 + 10_400


class StubFirehose:
    def __init__(self):
        self.calls = []
        self.fail_times = 0
        self.raise_error = None

    def put_record_batch(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error:
            raise self.raise_error
        if self.fail_times > 0:
            self.fail_times -= 1
            return {"FailedPutCount": len(kwargs["Records"]),
                    "RequestResponses": [{"ErrorCode": "ServiceUnavailableException"} for _ in kwargs["Records"]]}
        return {"FailedPutCount": 0, "RequestResponses": [{"RecordId": "r"} for _ in kwargs["Records"]]}

    def records(self):
        out = []
        for call in self.calls:
            for record in call["Records"]:
                out.append((call["DeliveryStreamName"], json.loads(record["Data"].decode("utf-8"))))
        return out


class Ingest:
    def __init__(self, env, module, firehose):
        self.env = env
        self.module = module
        self.firehose = firehose
        self.usecase_id = str(uuid.uuid4())
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "telemetry", "account_id": PORTAL_ACCOUNT, "region": REGION,
            "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(PORTAL_ACCOUNT)})
        self.workflow_id = str(uuid.uuid4())
        env.stack.tables.workflows.put_item(Item={"workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
                                                  "created_at": 1, "name": "wf", "current_version": "3"})
        self.device = "thing-" + self.usecase_id[-8:]
        env.stack.tables.devices.put_item(Item={"device_id": self.device, "usecase_id": self.usecase_id,
                                                "target_architecture": "arm64_jp5"})
        self.tables = telemetry_tables.tables()
        self.enable()
        self.things = {PR: self.device}

    def enable(self, enabled=True):
        self.tables["config"].put_item(Item={"pk": "UC#" + self.usecase_id, "sk": "SETTINGS", "enabled": enabled,
                                             "gsi2pk": "USECASE", "gsi2sk": "UC#" + self.usecase_id})
        self.module.reset_caches()

    def pk(self):
        return "P#{0}#{1}#{2}".format(self.usecase_id, self.device, self.workflow_id)

    def summary(self, **over):
        message = {
            "schema": "dda.summary.v1", "intervalStart": T0, "intervalEnd": T0 + 10_000, "emittedAt": T0 + 10_100,
            "deviceId": self.device, "workflowId": self.workflow_id, "workflowVersion": "3",
            "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
            "seq": 7, "intervalS": 10, "idle": False,
            "runs": {"completed": 12, "failed": 1, "dropped": 2},
            "latency": {"p50Ms": 120, "p95Ms": 300, "maxMs": 410, "n": 13},
            "detections": {"runsWithDetections": 10, "total": 44, "perClass": {"helmet": {"total": 30, "max": 4}},
                           "truncated": False},
            "model": {"state": "READY", "models": {}, "waits": 0, "waitedMs": 0},
            "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0},
        }
        message.update(over)
        return message

    def heartbeat(self, **over):
        message = {
            "schema": "dda.summary.v1", "intervalStart": T0, "intervalEnd": T0 + 60_000, "emittedAt": T0 + 60_100,
            "deviceId": self.device, "workflowId": self.workflow_id, "workflowVersion": "3",
            "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
            "seq": 8, "intervalS": 60, "idle": True,
            "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0},
        }
        message.update(over)
        return message

    def event(self, event_type="run_error", **over):
        data = {
            "run_error": {"error": "boom at rtsp://user:hunter2@cam.example.com/live", "failingNodeId": "n1",
                          "count": 1, "firstTs": NOW - 100, "lastTs": NOW - 100},
            "clip_ready": {"clipId": "clip-1", "revision": 1, "startTs": NOW - 20_000, "endTs": NOW - 8_000,
                           "durationS": 12.0, "reasons": {"no-helmet": {"frames": 10, "objects": 12}},
                           "totals": {"perClass": {"human": 20}, "violationFrames": 10, "violationObjects": 12},
                           "file": {"name": "clip.mp4", "bytes": 1024, "sha256": "ab" * 32, "width": 1280,
                                    "height": 720, "codec": "h264"},
                           "s3": {"bucket": "dda-inference-results-" + PORTAL_ACCOUNT,
                                  "key": "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip-1.mp4".format(
                                      self.usecase_id, self.device, self.workflow_id),
                                  "sidecarKey": "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip-1.json".format(
                                      self.usecase_id, self.device, self.workflow_id)},
                           "s3Error": None},
            "violation_confirmed": {"reasons": {"no-helmet": 2}, "people": 3, "violatingPeople": 2, "clipId": "clip-1"},
        }[event_type]
        message = {
            "schema": "dda.event.v1", "type": event_type, "eventId": str(uuid.uuid4()), "ts": NOW - 100,
            "deviceId": self.device, "workflowId": self.workflow_id, "workflowVersion": "3",
            "producer": {"name": "engine" if event_type == "run_error" else "ppe-clipper", "version": "1.0.0",
                         "instance": INSTANCE},
            "executionId": "exec-1", "data": data,
        }
        message.update(over)
        return message

    def live(self, seq=17, instance=INSTANCE, **over):
        from telemetry import schemas
        message = {
            "schema": "dda.live.v1", "ts": NOW, "instance": instance, "seq": seq, "deviceId": self.device,
            "workflowId": self.workflow_id,
            "frame": {"bucket": "dda-inference-results-" + PORTAL_ACCOUNT,
                      "key": schemas.live_key_for(self.usecase_id, self.device, self.workflow_id, seq),
                      "width": 1280, "height": 720, "bytes": 204800},
            "perClass": {"helmet": 2}, "people": 2,
        }
        message.update(over)
        return message

    def rule_event(self, payload, *, kind=None, r=NOW, m=None, pr=PR, c=None, topic=None):
        kind = kind or {"dda.summary.v1": "summary", "dda.event.v1": "event", "dda.live.v1": "live"}[payload["schema"]]
        return {"p": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"),
                "t": topic or "dda/telemetry/{0}/{1}/{2}".format(self.usecase_id, self.workflow_id, kind),
                "pr": pr, "c": c if c is not None else self.device, "r": r, "m": m or str(uuid.uuid4())}

    def send(self, payload, **kwargs):
        clock_ms = kwargs.pop("now_ms", NOW)
        account = kwargs.pop("account", PORTAL_ACCOUNT)
        context = SimpleNamespace(invoked_function_arn="arn:aws:lambda:{0}:{1}:function:dda-portal-telemetry-ingest:acct-{2}".format(
            REGION, PORTAL_ACCOUNT, account))
        return self.module.process(self.rule_event(payload, **kwargs), context, clock=lambda: clock_ms / 1000.0,
                                   sleep=lambda s: None)

    def items(self):
        return self.tables["summaries"].query(KeyConditionExpression=Key("pk").eq(self.pk()))["Items"]

    def item(self, sk, table="summaries"):
        return self.tables[table].get_item(Key={"pk": self.pk(), "sk": sk}).get("Item")

    def config_items(self, pk=None):
        return self.tables["config"].query(KeyConditionExpression=Key("pk").eq(pk or "UC#" + self.usecase_id))["Items"]

    def clip_entries(self):
        return self.tables["events"].query(KeyConditionExpression=Key("pk").eq(
            "C#{0}#{1}".format(self.usecase_id, self.workflow_id)))["Items"]


@pytest.fixture
def ingest(env, monkeypatch):
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TELEMETRY_FIREHOSE_SUMMARIES", "test-fh-summaries")
    monkeypatch.setenv("TELEMETRY_FIREHOSE_EVENTS", "test-fh-events")
    monkeypatch.delenv("TELEMETRY_CLIP_RETENTION_DAYS", raising=False)
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    # The package object must go too: `from telemetry import attribution` would
    # otherwise reuse its stale submodule attribute and bind the previous
    # test's class objects.
    for name in list(sys.modules):
        if name in ("telemetry_ingest", "telemetry") or name.startswith("edge_telemetry_common") or name.startswith("telemetry."):
            sys.modules.pop(name, None)
    import telemetry_ingest as module
    firehose = StubFirehose()
    monkeypatch.setattr(module, "_firehose", lambda: firehose)
    harness = Ingest(env, module, firehose)

    def resolve(pr, usecase, **kwargs):
        return harness.things.get(pr)

    monkeypatch.setattr(module.identity, "resolve_thing", resolve)
    module.reset_caches()
    return harness


# --------------------------------------------------------------------------- the accept path
class TestSummaries:
    def test_accept_path_writes_the_item_both_pointers_the_marker_and_the_lake_record(self, ingest):
        outcome = ingest.send(ingest.summary(), m="aaaabbbb-1111-4222-8333-44445555666a", r=NOW)
        assert outcome["result"] == "accepted" and outcome["kind"] == "summary"
        assert outcome["gap"] == 0 and outcome["duplicate"] is False and outcome["pointer_moved"] is True
        item = ingest.item("S#{0:013d}#aaaabbbb".format(NOW))
        assert item["summary"]["runs"]["completed"] == 12 and item["seq"] == 7 and item["eventMs"] == T0 + 10_000
        last = ingest.item("LAST#engine")
        assert last["receivedMs"] == NOW and last["seq"] == 7 and last["instance"] == INSTANCE
        assert last["summary"]["runs"]["completed"] == 12 and last["lastRunMs"] == NOW
        assert ingest.item("LASTHB#engine") is None
        seen = [i for i in ingest.config_items() if i["sk"] == "SEEN"]
        assert len(seen) == 1 and seen[0]["receivedMs"] == NOW
        lake = ingest.firehose.records()
        assert len(lake) == 1 and lake[0][0] == "test-fh-summaries"
        record = lake[0][1]
        assert record["v"] == 1 and record["runs_completed"] == 12 and record["per_class"] == {"helmet": {"total": 30, "max": 4}}
        assert record["message_id"] == "aaaabbbb-1111-4222-8333-44445555666a" and json.loads(record["raw"])["seq"] == 7

    def test_a_lambda_retry_overwrites_itself(self, ingest):
        ingest.send(ingest.summary(), m="aaaabbbb-1111-4222-8333-44445555666a", r=NOW)
        ingest.send(ingest.summary(), m="aaaabbbb-1111-4222-8333-44445555666a", r=NOW)
        assert len([i for i in ingest.items() if i["sk"].startswith("S#")]) == 1

    def test_a_retransmission_is_stored_counted_duplicate_and_leaves_the_pointer(self, ingest):
        ingest.send(ingest.summary(seq=7), r=NOW)
        ingest.send(ingest.summary(seq=8, runs={"completed": 1, "failed": 0, "dropped": 0}), r=NOW + 10_000)
        outcome = ingest.send(ingest.summary(seq=7), r=NOW + 10_500)
        assert outcome["result"] == "accepted" and outcome["duplicate"] is True and outcome["gap"] == 0
        assert len([i for i in ingest.items() if i["sk"].startswith("S#")]) == 3
        last = ingest.item("LAST#engine")
        assert last["seq"] == 8 and last["receivedMs"] == NOW + 10_000 and last["summary"]["runs"]["completed"] == 1
        rej = [i for i in ingest.config_items() if i["sk"].startswith("REJ#")]
        assert len(rej) == 1 and rej[0]["duplicate"] == 1
        assert not any(i["sk"].startswith("SAMPLE#") for i in ingest.config_items()), "a diagnostic has no sample"

    def test_gap_estimate(self, ingest):
        ingest.send(ingest.summary(seq=7), r=NOW)
        outcome = ingest.send(ingest.summary(seq=10), r=NOW + 30_000)
        assert outcome["gap"] == 2
        assert ingest.item("S#{0:013d}#{1}".format(NOW + 30_000, ingest.items()[-1]["sk"].split("#")[2]))["gap"] == 2 or \
            any(i.get("gap") == 2 for i in ingest.items())
        restarted = ingest.send(ingest.summary(seq=1, producer={"name": "engine", "version": "1.0.0", "instance": OTHER_INSTANCE}),
                                r=NOW + 40_000)
        assert restarted["gap"] == 0 and restarted["duplicate"] is False, "a new instance is neither"

    def test_last_run_ms_moves_only_for_a_summary_with_runs_and_a_heartbeat_never_blanks_last(self, ingest):
        ingest.send(ingest.summary(seq=7), r=NOW)
        assert ingest.item("LAST#engine")["lastRunMs"] == NOW
        ingest.send(ingest.summary(seq=8, runs={"completed": 0, "failed": 0, "dropped": 3}), r=NOW + 10_000)
        last = ingest.item("LAST#engine")
        assert last["receivedMs"] == NOW + 10_000 and last["lastRunMs"] == NOW, "zero runs leave lastRunMs"
        assert last["summary"]["runs"]["dropped"] == 3
        for seq in (9, 10, 11):
            ingest.send(ingest.heartbeat(seq=seq, intervalEnd=T0 + 60_000 * (seq - 8), intervalStart=T0 + 60_000 * (seq - 9),
                                         emittedAt=T0 + 60_000 * (seq - 8) + 100), r=NOW + 60_000 * (seq - 8))
        last = ingest.item("LAST#engine")
        assert last["seq"] == 8 and last["lastRunMs"] == NOW and last["summary"]["runs"]["dropped"] == 3
        lasthb = ingest.item("LASTHB#engine")
        assert lasthb["seq"] == 11 and lasthb["receivedMs"] == NOW + 180_000 and "summary" not in lasthb
        assert [r[0] for r in ingest.firehose.records()] == ["test-fh-summaries"] * 2, "Heartbeats are not in the lake"
        assert len([i for i in ingest.config_items() if i["sk"] == "SEEN"]) == 1

    def test_an_older_receive_time_processed_later_does_not_move_the_pointer(self, ingest):
        ingest.send(ingest.summary(seq=8), r=NOW + 10_000)
        outcome = ingest.send(ingest.summary(seq=7), r=NOW)
        assert outcome["pointer_moved"] is False and outcome["duplicate"] is True
        assert ingest.item("LAST#engine")["seq"] == 8

    def test_block_not_owned(self, ingest):
        ppe = {"ppe": {"people": {"seen": 3, "compliant": 2, "violating": 1},
                       "violations": {"frames": 2, "byReason": {"no-helmet": {"frames": 2, "objects": 3}}},
                       "clipsOpened": 1, "frames": 50}}
        from_engine = ingest.send(ingest.summary(ext=ppe), r=NOW)
        assert from_engine["diagnostics"] == {} and ingest.item("LAST#engine")["summary"]["ext"]["ppe"]["frames"] == 50
        helper = ingest.summary(seq=1, producer={"name": "ppe-clipper", "version": "1", "instance": OTHER_INSTANCE}, ext=ppe)
        outcome = ingest.send(helper, r=NOW + 1000)
        assert outcome["result"] == "accepted" and outcome["diagnostics"] == {"block_not_owned": 4}
        stored = [i for i in ingest.items() if i["sk"].startswith("S#") and i["producer"] == "ppe-clipper"][0]
        assert "runs" not in stored["summary"] and "latency" not in stored["summary"] and stored["summary"]["ext"]["ppe"]["frames"] == 50
        assert ingest.item("LAST#ppe-clipper")["seq"] == 1
        rej = [i for i in ingest.config_items() if i["sk"].startswith("REJ#")]
        assert rej and rej[0]["block_not_owned"] == 4
        lake = [r for s, r in ingest.firehose.records() if r["producer"] == "ppe-clipper"][0]
        assert "runs_completed" not in lake and json.loads(lake["raw"])["runs"]["completed"] == 12, "the lake keeps the raw payload"

    def test_field_dropped_is_counted_and_the_summary_stands(self, ingest, capsys):
        outcome = ingest.send(ingest.summary(model={"state": "WARMING", "models": {}, "waits": 0, "waitedMs": 0}), r=NOW)
        assert outcome["result"] == "accepted"
        assert ingest.item("LAST#engine")["summary"]["model"]["state"] == "UNKNOWN"
        lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
        assert any("FieldDropped" in l and l["Schema"] == "dda.summary.v1" for l in lines)


# --------------------------------------------------------------------------- Events
class TestEvents:
    def test_run_error_is_stored_redacted_and_written_to_the_lake(self, ingest):
        payload = ingest.event("run_error")
        outcome = ingest.send(payload, r=NOW)
        assert outcome["result"] == "accepted" and outcome["kind"] == "event" and outcome["type"] == "run_error"
        item = ingest.item("E#{0:013d}#{1}".format(NOW - 100, payload["eventId"]), table="events")
        assert item["event"]["data"]["error"] == "boom at rtsp://***@cam.example.com/live"
        assert item["gsi1pk"] == "W#{0}#{1}".format(ingest.usecase_id, ingest.workflow_id)
        assert item["type"] == "run_error" and item["deviceId"] == ingest.device
        lake = ingest.firehose.records()
        assert lake[0][0] == "test-fh-events" and lake[0][1]["kind"] == "event"
        assert "hunter2" not in lake[0][1]["raw"] and lake[0][1]["data"]["error"].startswith("boom at rtsp://***@")

    def test_a_qos1_redelivery_overwrites_itself(self, ingest):
        payload = ingest.event("run_error")
        ingest.send(payload, r=NOW)
        ingest.send(payload, r=NOW + 5000)
        items = ingest.tables["events"].query(KeyConditionExpression=Key("pk").eq(ingest.pk()))["Items"]
        assert len(items) == 1

    def test_clip_index_revisions(self, ingest):
        first = ingest.event("clip_ready")
        outcome = ingest.send(first, r=NOW)
        assert outcome["clip_index_updated"] is True
        entries = ingest.clip_entries()
        assert len(entries) == 1 and entries[0]["revision"] == 1 and entries[0]["media"]["state"] == "bound"
        assert entries[0]["media"]["key"].endswith("/clip-1.mp4") and entries[0]["media"]["sidecarKey"].endswith("/clip-1.json")
        second = ingest.event("clip_ready")
        second["data"]["revision"] = 2
        second["data"]["startTs"] = first["data"]["startTs"]
        second["data"]["file"]["bytes"] = 2048
        assert ingest.send(second, r=NOW + 1000)["clip_index_updated"] is True
        entries = ingest.clip_entries()
        assert len(entries) == 1 and entries[0]["revision"] == 2 and entries[0]["file"]["bytes"] == 2048
        stale = ingest.event("clip_ready")
        stale["data"]["revision"] = 1
        stale["data"]["file"]["bytes"] = 1
        assert ingest.send(stale, r=NOW + 2000)["clip_index_updated"] is False
        assert ingest.clip_entries()[0]["file"]["bytes"] == 2048
        shifted = ingest.event("clip_ready")
        shifted["data"]["revision"] = 2
        shifted["data"]["startTs"] = first["data"]["startTs"] + 5000
        ingest.send(shifted, r=NOW + 3000)
        assert len(ingest.clip_entries()) == 2, "a shifted startTs is a new clip"

    def test_clip_media_states(self, ingest):
        failed = ingest.event("clip_ready")
        failed["data"]["s3"] = None
        failed["data"]["s3Error"] = "upload failed: token=abc12345 denied"
        ingest.send(failed, r=NOW)
        entry = ingest.clip_entries()[0]
        assert entry["media"]["state"] == "upload_failed" and entry["s3Error"] == "upload failed: token=*** denied"
        elsewhere = ingest.event("clip_ready")
        elsewhere["data"]["clipId"] = "clip-2"
        elsewhere["data"]["s3"]["bucket"] = "someone-elses-bucket"
        ingest.send(elsewhere, r=NOW + 1)
        entry = [e for e in ingest.clip_entries() if e["clipId"] == "clip-2"][0]
        assert entry["media"]["state"] == "not_allowed" and entry["media"]["reason"] == "bucket_not_allowed"
        outside = ingest.event("clip_ready")
        outside["data"]["clipId"] = "clip-3"
        outside["data"]["s3"]["key"] = "dda-telemetry/clips/{0}/other-device/{1}/x.mp4".format(ingest.usecase_id, ingest.workflow_id)
        ingest.send(outside, r=NOW + 2)
        entry = [e for e in ingest.clip_entries() if e["clipId"] == "clip-3"][0]
        assert entry["media"]["state"] == "not_allowed" and entry["media"]["reason"] == "key_outside_prefix"

    def test_rate_exceeded(self, ingest):
        for index in range(120):
            assert ingest.send(ingest.event("run_error"), r=NOW + index, now_ms=NOW + index)["result"] == "accepted"
        outcome = ingest.send(ingest.event("run_error"), r=NOW + 120, now_ms=NOW + 120)
        assert outcome == {"result": "rejected", "reason": "rate_exceeded", "usecase_id": ingest.usecase_id}
        rej = [i for i in ingest.config_items() if i["sk"].startswith("REJ#")]
        assert sum(int(i.get("rate_exceeded", 0)) for i in rej) == 1
        assert ingest.send(ingest.summary(), r=NOW + 121, now_ms=NOW + 121)["result"] == "accepted"


# --------------------------------------------------------------------------- live frames
class TestLive:
    def test_the_live_pointer_follows_arrival_across_two_instances(self, ingest):
        assert ingest.send(ingest.live(seq=4000), r=NOW)["pointer_moved"] is True
        live = ingest.item("LIVE")
        assert live["seq"] == 4000 and live["frameToken"] == INSTANCE + ":4000" and live["key"].endswith("/0.jpg")
        restarted = ingest.send(ingest.live(seq=0, instance=OTHER_INSTANCE), r=NOW + 2000)
        assert restarted["pointer_moved"] is True
        live = ingest.item("LIVE")
        assert live["seq"] == 0 and live["instance"] == OTHER_INSTANCE and live["frameToken"] == OTHER_INSTANCE + ":0"
        assert live["receivedMs"] == NOW + 2000 and live["perClass"] == {"helmet": 2}
        older = ingest.send(ingest.live(seq=4001), r=NOW + 1000)
        assert older["pointer_moved"] is False
        assert ingest.item("LIVE")["instance"] == OTHER_INSTANCE
        assert ingest.firehose.records() == [], "live frames are not in the lake"
        assert not any(i["sk"] == "SEEN" for i in ingest.config_items())

    def test_a_wrong_slot_is_schema_invalid(self, ingest):
        payload = ingest.live(seq=17)
        payload["frame"]["key"] = payload["frame"]["key"].replace("/1.jpg", "/3.jpg")
        assert ingest.send(payload, r=NOW)["reason"] == "schema_invalid"


# --------------------------------------------------------------------------- rejections
class TestRejections:
    def test_every_reason_is_counted_with_a_sample_and_no_payload_in_the_logs(self, ingest, caplog, capsys):
        caplog.set_level(logging.INFO)
        secret = "payload-secret-" + "x" * 300
        cases = [
            ("invalid_json", dict(payload=None, raw_p="!!notbase64!!")),
            ("too_large", dict(payload=ingest.summary(vendor=secret * 60))),
            ("schema_invalid", dict(payload=ingest.summary(intervalS=120, intervalEnd=T0 + 120_000, emittedAt=T0 + 120_000, note=secret))),
            ("schema_unknown", dict(payload=ingest.summary(), topic="dda/telemetry/{0}/{1}/frames".format(ingest.usecase_id, ingest.workflow_id))),
            ("unattributed", dict(payload=ingest.summary(), pr="cd" * 32)),
            ("attribution_mismatch", dict(payload=ingest.summary(deviceId=secret), c="someone-else")),
            ("workflow_not_in_usecase", dict(payload=ingest.summary(), topic="dda/telemetry/{0}/{1}/summary".format(ingest.usecase_id, uuid.uuid4()))),
        ]
        for reason, options in cases:
            payload = options.pop("payload")
            raw_p = options.pop("raw_p", None)
            event = ingest.rule_event(payload or ingest.summary(), **options)
            if raw_p is not None:
                event["p"] = raw_p
            context = SimpleNamespace(invoked_function_arn="arn:aws:lambda:{0}:{1}:function:f:acct-{1}".format(REGION, PORTAL_ACCOUNT))
            outcome = ingest.module.process(event, context, clock=lambda: NOW / 1000.0)
            assert outcome["result"] == "rejected" and outcome["reason"] == reason, reason
        ingest.enable(False)
        assert ingest.send(ingest.summary(), r=NOW)["reason"] == "usecase_disabled"
        ingest.enable(True)
        rej = [i for i in ingest.config_items() if i["sk"].startswith("REJ#")]
        counted = {k: int(v) for item in rej for k, v in item.items() if k not in ("pk", "sk", "expires_at", "minute")}
        for reason, _ in cases:
            assert counted.get(reason) == 1, reason
        assert counted["usecase_disabled"] == 1
        samples = [i for i in ingest.config_items() if i["sk"].startswith("SAMPLE#")]
        assert {s["reason"] for s in samples} >= {r for r, _ in cases} | {"usecase_disabled"}
        for sample in samples:
            assert len(json.dumps(sample["sample"], default=str)) <= 1024
            assert secret not in json.dumps(sample["sample"], default=str)
        text = caplog.text + capsys.readouterr().out
        assert secret not in text and "hunter2" not in text
        for line in text.splitlines():
            for token in line.split():
                assert len(token) <= 256 or token.startswith("{"), token[:80]

    def test_identity_unavailable_is_counted_per_account_and_alarmed(self, ingest, capsys, monkeypatch):
        from edge_telemetry_common.identity import LookupDenied

        def denied(pr, usecase, **kwargs):
            raise LookupDenied("identity lookup denied for account " + PORTAL_ACCOUNT)

        monkeypatch.setattr(ingest.module.identity, "resolve_thing", denied)
        outcome = ingest.send(ingest.summary(), r=NOW)
        assert outcome["reason"] == "identity_unavailable"
        lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
        assert any("IdentityUnavailable" in l for l in lines)
        assert any(l.get("Reason") == "identity_unavailable" and l.get("Rejected") == 1 for l in lines)

    def test_a_retryable_identity_failure_raises(self, ingest, monkeypatch):
        from edge_telemetry_common.identity import LookupRetryable

        def throttled(pr, usecase, **kwargs):
            raise LookupRetryable("ThrottlingException")

        monkeypatch.setattr(ingest.module.identity, "resolve_thing", throttled)
        with pytest.raises(LookupRetryable):
            ingest.send(ingest.summary(), r=NOW)
        assert ingest.items() == []

    def test_rejections_before_a_use_case_is_known_count_per_account(self, ingest):
        outcome = ingest.send(ingest.summary(), topic="dda/telemetry/{0}/{1}/summary".format(uuid.uuid4(), ingest.workflow_id))
        assert outcome["reason"] == "attribution_mismatch" and outcome["usecase_id"] is not None
        bad_topic = ingest.send(ingest.summary(), topic="dda/results")
        assert bad_topic["reason"] == "attribution_mismatch" and bad_topic["usecase_id"] is None
        account_items = ingest.config_items("X#" + PORTAL_ACCOUNT)
        assert any(i["sk"].startswith("REJ#") and int(i.get("attribution_mismatch", 0)) >= 1 for i in account_items)

    def test_an_invocation_outside_an_account_alias_is_refused(self, ingest):
        context = SimpleNamespace(invoked_function_arn="arn:aws:lambda:{0}:{1}:function:f".format(REGION, PORTAL_ACCOUNT))
        outcome = ingest.module.process(ingest.rule_event(ingest.summary()), context, clock=lambda: NOW / 1000.0)
        assert outcome["reason"] == "attribution_mismatch"
        assert ingest.module.handler("not an object", context)["reason"] == "invalid_json"

    def test_a_wrong_account_alias_is_a_mismatch(self, ingest):
        assert ingest.send(ingest.summary(), account="333333333333")["reason"] == "attribution_mismatch"


# --------------------------------------------------------------------------- the lake
class TestLake:
    def test_a_firehose_failure_after_two_retries_does_not_fail_the_message(self, ingest):
        ingest.firehose.fail_times = 5
        outcome = ingest.send(ingest.summary(), r=NOW)
        assert outcome["result"] == "accepted" and outcome["lake_failed"] == 1
        assert len(ingest.firehose.calls) == 3, "one call plus two retries"
        assert ingest.item("LAST#engine") is not None

    def test_a_firehose_exception_is_swallowed(self, ingest):
        ingest.firehose.raise_error = ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "x"}},
                                                  "PutRecordBatch")
        outcome = ingest.send(ingest.summary(), r=NOW)
        assert outcome["result"] == "accepted" and outcome["lake_failed"] == 1

    def test_a_transient_failure_is_retried_and_succeeds(self, ingest):
        ingest.firehose.fail_times = 1
        outcome = ingest.send(ingest.summary(), r=NOW)
        assert outcome["lake_failed"] == 0 and len(ingest.firehose.calls) == 2

    def test_batching_limits(self, ingest):
        firehose = StubFirehose()
        lines = ["x" * 1000 + "\n"] * 1201
        failed = ingest.module.put_lake_records("s", lines, client=firehose, sleep=lambda s: None)
        assert failed == 0
        assert [len(c["Records"]) for c in firehose.calls] == [500, 500, 201]
        firehose = StubFirehose()
        big = ["y" * (3 * 1024 * 1024) + "\n"] * 3
        ingest.module.put_lake_records("s", big, client=firehose, sleep=lambda s: None)
        assert [len(c["Records"]) for c in firehose.calls] == [1, 1, 1]
