"""``telemetry.schemas`` and the committed contracts (workflow-telemetry-dashboards
design §4, §8.1, §8.2; tasks.md 3.1).

Every case of the plan: a 3 s first interval accepted; a 12 s interval
declared as 10 rejected; a 60 s Heartbeat declaring 60 accepted; a Heartbeat
declaring 10 over 60 s rejected; a Summary declaring ``intervalS: 120``
rejected; a numeric string coerced; an out-of-set model state coerced and
counted; a live key whose slot differs rejected; each Event type's ``data``;
the adapters equal the registry; every measure an adapter emits is in the
catalog; every template measure exists in the catalog and a template naming
an unknown measure fails; the catalog content hash changes only with a
version bump.
"""
import base64
import copy
import json
import os
import uuid

import pytest

from telemetry import schemas

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
DEVICE = "mic-730-demo"
T0 = 1_792_000_000_000  # an aligned epoch ms (a multiple of 10 s)

#: The catalog content hash per version. A content change must bump the
#: version and add the new hash here (design §8.1).
CATALOG_HASHES = {
    1: "cfffe8733bf582d64d32b1e887603c07666dcec267881fe69012ee364252fb45",
}


def summary(**over):
    message = {
        "schema": "dda.summary.v1",
        "intervalStart": T0, "intervalEnd": T0 + 10_000, "emittedAt": T0 + 10_200,
        "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
        "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
        "seq": 7, "intervalS": 10, "idle": False,
        "runs": {"completed": 12, "failed": 1, "dropped": 2},
        "latency": {"p50Ms": 120, "p95Ms": 300, "maxMs": 410, "n": 13},
        "detections": {"runsWithDetections": 10, "total": 44,
                       "perClass": {"helmet": {"total": 30, "max": 4}, "human": {"total": 14, "max": 3}},
                       "truncated": False},
        "outcomes": {"anomalous": 2, "normal": 10},
        "people": {"seen": 3},
        "model": {"state": "READY", "models": {"ppe-yolo": "READY"}, "waits": 0, "waitedMs": 0},
        "continuous": {"skippedNoNewFrame": 4, "paused": False, "effectiveFps": 4.5},
        "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0},
    }
    message.update(over)
    return message


def heartbeat(**over):
    message = {
        "schema": "dda.summary.v1",
        "intervalStart": T0, "intervalEnd": T0 + 60_000, "emittedAt": T0 + 60_100,
        "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
        "producer": {"name": "engine", "version": "1.0.0", "instance": INSTANCE},
        "seq": 8, "intervalS": 60, "idle": True,
        "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0},
    }
    message.update(over)
    return message


def ppe_ext(**over):
    ext = {"people": {"seen": 3, "compliant": 2, "violating": 1},
           "violations": {"frames": 2, "byReason": {"no-helmet": {"frames": 2, "objects": 3}}},
           "clipsOpened": 1, "frames": 50}
    ext.update(over)
    return ext


def event(event_type="run_error", **over):
    data = {
        "run_error": {"error": "node failed", "failingNodeId": "detector-1", "count": 1,
                      "firstTs": T0 + 1, "lastTs": T0 + 1},
        "clip_ready": {"clipId": "clip-2026-10-18T10.00.00Z", "revision": 1, "startTs": T0, "endTs": T0 + 12_000,
                       "durationS": 12.0, "reasons": {"no-helmet": {"frames": 10, "objects": 12}},
                       "totals": {"perClass": {"human": 20}, "violationFrames": 10, "violationObjects": 12},
                       "file": {"name": "clip.mp4", "bytes": 1024, "sha256": "ab" * 32, "width": 1280,
                                "height": 720, "codec": "h264"},
                       "s3": {"bucket": "dda-inference-results-222222222222",
                              "key": "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip.mp4".format(UC, DEVICE, WF),
                              "sidecarKey": "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/clip.json".format(UC, DEVICE, WF)},
                       "s3Error": None},
        "violation_confirmed": {"reasons": {"no-helmet": 2}, "people": 3, "violatingPeople": 2, "clipId": "clip-1"},
    }[event_type]
    message = {
        "schema": "dda.event.v1", "type": event_type, "eventId": str(uuid.uuid4()), "ts": T0 + 500,
        "deviceId": DEVICE, "workflowId": WF, "workflowVersion": "3",
        "producer": {"name": "engine" if event_type == "run_error" else "ppe-clipper", "version": "1.0.0",
                     "instance": INSTANCE},
        "executionId": "exec-1", "data": data,
    }
    message.update(over)
    return message


def live(seq=17, **over):
    message = {
        "schema": "dda.live.v1", "ts": T0, "instance": INSTANCE, "seq": seq, "deviceId": DEVICE, "workflowId": WF,
        "frame": {"bucket": "dda-inference-results-222222222222",
                  "key": "dda-telemetry/live/{0}/{1}/{2}/{3}.jpg".format(UC, DEVICE, WF, seq % 16),
                  "width": 1280, "height": 720, "bytes": 204800},
        "perClass": {"helmet": 2}, "people": 2, "violation": False, "reasons": {},
    }
    message.update(over)
    return message


def validate(message, **kwargs):
    text = json.dumps(message)
    return schemas.validate_message(message, text, kind=schemas.KIND_BY_SCHEMA[message["schema"]], **kwargs)


def encode(message):
    return base64.b64encode(json.dumps(message).encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------- the registry and adapters
class TestRegistry:
    def test_adapters_equal_the_registry(self):
        path = os.path.join(os.path.dirname(schemas.__file__), "registry.json")
        with open(path, encoding="utf-8") as fh:
            registry = json.load(fh)
        assert schemas.REGISTRY == registry
        assert set(schemas.ADAPTERS) == set(registry["schemas"]) == set(registry["kinds"].values())
        for schema, declaration in registry["schemas"].items():
            adapter = schemas.ADAPTERS[schema]
            assert adapter.fields == declaration["fields"]
            assert adapter.kind == declaration["kind"]
            assert adapter.max_bytes == declaration["maxBytes"]
        assert schemas.SCHEMA_BY_KIND == {"summary": "dda.summary.v1", "event": "dda.event.v1", "live": "dda.live.v1"}
        assert schemas.MESSAGE_MAX_BYTES == 16384 and schemas.PAYLOAD_MAX_BYTES == 131072
        assert schemas.ADAPTERS["dda.summary.v1"].max_bytes == 16384
        assert schemas.ADAPTERS["dda.event.v1"].max_bytes == 16384
        assert schemas.ADAPTERS["dda.live.v1"].max_bytes == 131072

    def test_the_eight_model_states_and_the_ordering(self):
        assert set(schemas.MODEL_STATES) == {"NONE", "READY", "LOADING", "UNLOADING", "UNAVAILABLE", "UNKNOWN",
                                             "NOT_DEPLOYED", "INCOMPLETE"}
        assert schemas.MODEL_STATE_ORDER == ("NONE", "READY", "UNLOADING", "LOADING", "INCOMPLETE", "NOT_DEPLOYED",
                                             "UNKNOWN", "UNAVAILABLE")
        assert schemas.worst_state(["READY", "LOADING", "READY"]) == "LOADING"
        assert schemas.worst_state(["NONE"]) == "NONE"
        assert schemas.worst_state(["READY", "weird"]) == "UNKNOWN"
        assert schemas.worst_state(["UNKNOWN", "UNAVAILABLE"]) == "UNAVAILABLE"
        assert schemas.worst_state(["INCOMPLETE", "NOT_DEPLOYED"]) == "NOT_DEPLOYED"
        assert schemas.worst_state([]) is None
        assert schemas.worst_state([None]) is None

    def test_block_owners(self):
        for block in ("runs", "latency", "detections", "outcomes", "people", "model", "continuous"):
            assert schemas.block_owner(block) == ("engine",)
        assert schemas.block_owner("ext.ppe") == ("ppe-clipper", "engine")
        assert schemas.block_owner("ext.other") is None
        assert schemas.block_owner("telemetry") is None

    def test_drop_unowned_blocks(self):
        fields = validate(summary(ext={"ppe": ppe_ext(frames=100)})).fields
        kept, dropped = schemas.drop_unowned_blocks(fields, "engine")
        assert dropped == [] and "runs" in kept and kept["ext"]["ppe"]["frames"] == 100
        kept, dropped = schemas.drop_unowned_blocks(fields, "ppe-clipper")
        assert sorted(dropped) == ["continuous", "detections", "latency", "model", "outcomes", "people", "runs"]
        assert "runs" not in kept and kept["ext"]["ppe"]["frames"] == 100 and kept["telemetry"]
        kept, dropped = schemas.drop_unowned_blocks(fields, "someone-else")
        assert "ext.ppe" in dropped and kept["ext"] == {}
        # The input is not mutated.
        assert "runs" in fields and "ppe" in fields["ext"]


# --------------------------------------------------------------------------- the Summary rules
class TestSummary:
    def test_a_well_formed_summary_is_typed(self):
        result = validate(summary())
        assert result.kind == "summary" and result.schema == "dda.summary.v1" and result.dropped == []
        assert result.fields["runs"] == {"completed": 12, "failed": 1, "dropped": 2}
        assert result.fields["detections"]["perClass"]["helmet"] == {"total": 30, "max": 4}
        assert result.fields["continuous"]["effectiveFps"] == 4.5
        assert result.fields["producer"]["instance"] == INSTANCE
        assert result.raw is not None and result.raw["seq"] == 7

    def test_a_three_second_first_interval_is_accepted(self):
        result = validate(summary(intervalStart=T0 + 7_000, intervalEnd=T0 + 10_000, emittedAt=T0 + 10_000))
        assert result.fields["intervalEnd"] - result.fields["intervalStart"] == 3_000

    def test_a_twelve_second_interval_declared_as_ten_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(summary(intervalStart=T0, intervalEnd=T0 + 12_000, emittedAt=T0 + 12_000))
        assert caught.value.reason == "schema_invalid" and "length rule" in caught.value.detail

    def test_an_eleven_second_window_is_within_the_tolerance(self):
        validate(summary(intervalStart=T0, intervalEnd=T0 + 11_000, emittedAt=T0 + 11_000))

    def test_a_sub_second_window_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid):
            validate(summary(intervalStart=T0 + 9_500, intervalEnd=T0 + 10_000, emittedAt=T0 + 10_000))

    def test_a_sixty_second_heartbeat_declaring_sixty_is_accepted(self):
        result = validate(heartbeat())
        assert result.fields["idle"] is True and result.fields["intervalS"] == 60
        assert "runs" not in result.fields and result.dropped == []

    def test_a_heartbeat_declaring_ten_over_sixty_seconds_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(heartbeat(intervalS=10))
        assert "intervalS" in caught.value.detail

    def test_a_summary_declaring_one_hundred_twenty_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(summary(intervalS=120, intervalEnd=T0 + 120_000, emittedAt=T0 + 120_000))
        assert "intervalS" in caught.value.detail
        with pytest.raises(schemas.SchemaInvalid):
            validate(summary(intervalS=4, intervalEnd=T0 + 4_000, emittedAt=T0 + 4_000))

    def test_a_heartbeat_outside_thirty_to_three_hundred_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid):
            validate(heartbeat(intervalS=301, intervalEnd=T0 + 301_000, emittedAt=T0 + 301_000))
        validate(heartbeat(intervalS=300, intervalEnd=T0 + 300_000, emittedAt=T0 + 300_000))
        validate(heartbeat(intervalS=30, intervalEnd=T0 + 30_000, emittedAt=T0 + 30_000))

    def test_an_off_grid_interval_end_is_accepted(self):
        """A removed registration's final window ends at the removal time (advisory alignment)."""
        validate(summary(intervalStart=T0 + 3_000, intervalEnd=T0 + 8_765, emittedAt=T0 + 8_900))

    def test_emitted_at_before_interval_end_is_rejected(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(summary(emittedAt=T0 + 9_999))
        assert "emittedAt" in caught.value.detail

    def test_a_numeric_string_is_coerced(self):
        result = validate(summary(seq="7", runs={"completed": "12", "failed": "1", "dropped": "2"},
                                  continuous={"skippedNoNewFrame": 4, "paused": "false", "effectiveFps": "4.5"}))
        assert result.fields["seq"] == 7 and result.fields["runs"]["completed"] == 12
        assert result.fields["continuous"] == {"skippedNoNewFrame": 4, "paused": False, "effectiveFps": 4.5}
        assert result.dropped == []

    def test_an_integer_workflow_version_is_normalized_to_a_string(self):
        assert validate(summary(workflowVersion=3)).fields["workflowVersion"] == "3"
        assert validate(summary(workflowVersion="")).fields["workflowVersion"] == ""
        with pytest.raises(schemas.SchemaInvalid):
            validate(summary(workflowVersion="v" * 65))

    def test_an_out_of_set_model_state_is_coerced_to_unknown_and_counted(self):
        result = validate(summary(model={"state": "WARMING", "models": {"ppe-yolo": "nope"}, "waits": 1, "waitedMs": 400}))
        assert result.fields["model"]["state"] == "UNKNOWN"
        assert result.fields["model"]["models"] == {"ppe-yolo": "UNKNOWN"}
        assert result.fields["model"]["waits"] == 1
        assert result.dropped == ["model.state", "model.models.ppe-yolo"]

    def test_every_declared_model_state_is_kept(self):
        for state in schemas.MODEL_STATES:
            result = validate(summary(model={"state": state, "models": {}, "waits": 0, "waitedMs": 0}))
            assert result.fields["model"]["state"] == state and result.dropped == []

    def test_an_optional_field_that_fails_is_dropped_and_counted(self):
        result = validate(summary(latency={"p50Ms": "fast", "p95Ms": 1, "maxMs": 1, "n": 1},
                                  people={"seen": -1}, outcomes="many"))
        assert "latency" not in result.fields and "people" not in result.fields and "outcomes" not in result.fields
        assert sorted(result.dropped) == ["latency", "outcomes", "people"]
        assert result.fields["runs"]["completed"] == 12

    def test_a_required_field_that_fails_is_schema_invalid(self):
        for over in ({"seq": "seven"}, {"seq": 0}, {"deviceId": "bad name"}, {"workflowId": "not-a-uuid"},
                     {"producer": {"name": "Engine", "version": "1", "instance": INSTANCE}},
                     {"telemetry": {"queueDrops": 0}}, {"idle": "maybe"}, {"intervalStart": -1}):
            with pytest.raises(schemas.SchemaInvalid) as caught:
                validate(summary(**over))
            assert caught.value.reason == "schema_invalid", over
        incomplete = summary()
        del incomplete["intervalEnd"]
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(incomplete)
        assert caught.value.detail == "intervalEnd: missing"

    def test_the_engine_needs_runs_and_model_when_not_idle(self):
        message = summary()
        del message["runs"]
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(message)
        assert "runs" in caught.value.detail
        message = summary()
        del message["model"]
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        # A helper producer carries neither.
        helper = summary(producer={"name": "ppe-clipper", "version": "2", "instance": INSTANCE})
        for block in ("runs", "latency", "detections", "outcomes", "people", "model", "continuous"):
            del helper[block]
        helper["ext"] = {"ppe": ppe_ext()}
        result = validate(helper)
        assert result.fields["ext"]["ppe"]["violations"]["byReason"]["no-helmet"] == {"frames": 2, "objects": 3}
        assert result.dropped == []

    def test_summary_blocks_on_a_heartbeat_are_dropped(self):
        result = validate(heartbeat(runs={"completed": 1, "failed": 0, "dropped": 0}, ext={"ppe": {}}))
        assert "runs" not in result.fields and "ext" not in result.fields
        assert sorted(result.dropped) == ["ext", "runs"]

    def test_per_class_limits(self):
        many = {"c{0}".format(i): {"total": 1, "max": 1} for i in range(65)}
        result = validate(summary(detections={"runsWithDetections": 1, "total": 65, "perClass": many}))
        assert "detections" not in result.fields and result.dropped == ["detections"]
        sixty_four = {"c{0}".format(i): {"total": 1, "max": 1} for i in range(64)}
        typed = validate(summary(detections={"runsWithDetections": 1, "total": 64, "perClass": sixty_four}))
        assert len(typed.fields["detections"]["perClass"]) == 64
        long_label = {"x" * 65: {"total": 1, "max": 1}}
        assert "detections" not in validate(summary(detections={"runsWithDetections": 1, "total": 1,
                                                                "perClass": long_label})).fields
        other = {"__other__": {"total": 9, "max": 9}}
        assert validate(summary(detections={"runsWithDetections": 1, "total": 9, "perClass": other,
                                            "truncated": True})).fields["detections"]["truncated"] is True

    def test_model_map_limits(self):
        many = {"m{0}".format(i): "READY" for i in range(17)}
        result = validate(summary(model={"state": "READY", "models": many, "waits": 0, "waitedMs": 0}))
        assert "models" not in result.fields["model"] and result.dropped == ["model.models"]

    def test_extension_limits(self):
        unknown = validate(summary(ext={"other": {"anything": [1, 2, 3]}}))
        assert unknown.fields["ext"] == {"other": {"anything": [1, 2, 3]}} and unknown.dropped == []
        too_many = validate(summary(ext={"e{0}".format(i): {} for i in range(9)}))
        assert "ext" not in too_many.fields and too_many.dropped == ["ext"]
        bad_name = validate(summary(ext={"PPE": {}}))
        assert "ext" not in bad_name.fields
        big = validate(summary(ext={"blob": {"x": "y" * 9000}}))
        assert "ext" not in big.fields
        bad_ppe = validate(summary(ext={"ppe": {"people": {"seen": 1}}}))
        assert bad_ppe.fields["ext"] == {} and bad_ppe.dropped == ["ext.ppe"]
        inconsistent = validate(summary(ext={"ppe": ppe_ext(people={"seen": 1, "compliant": 1, "violating": 1})}))
        assert inconsistent.fields["ext"] == {} and inconsistent.dropped == ["ext.ppe"]
        reasons = {"r{0}".format(i): {"frames": 1, "objects": 1} for i in range(17)}
        too_many_reasons = validate(summary(ext={"ppe": ppe_ext(violations={"frames": 17, "byReason": reasons})}))
        assert too_many_reasons.dropped == ["ext.ppe"]

    def test_undeclared_fields_are_kept_in_raw_only(self):
        result = validate(summary(vendor={"x": 1}))
        assert "vendor" not in result.fields and result.raw["vendor"] == {"x": 1}

    def test_nesting_depth_is_bounded(self):
        deep = {"x": 1}
        for _ in range(12):
            deep = {"deeper": deep}
        result = validate(summary(ext={"other": deep}))
        # An unknown extension is opaque (kept for the lake); a declared field is checked.
        assert result.fields["ext"]["other"] == deep

    def test_wrong_schema_for_the_topic_kind(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.validate_message(summary(), json.dumps(summary()), kind="event")
        assert caught.value.reason == "schema_invalid"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.validate_message({"schema": "dda.other.v9"}, "{}", kind="summary")
        assert caught.value.reason == "schema_unknown"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.validate_message({"foo": 1}, "{}", kind="summary")
        assert caught.value.reason == "schema_unknown"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.adapter_for("dda.summary.v1").validate([1, 2])
        assert caught.value.reason == "invalid_json"

    def test_too_large(self):
        big = summary(ext={"blob": {"x": "y" * 7000}}, vendor="z" * 10000)
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(big)
        assert caught.value.reason == "too_large"
        # A live frame has the payload limit only.
        validate(live(vendor="z" * 20000), usecase_id=UC)


# --------------------------------------------------------------------------- Events
class TestEvent:
    def test_run_error(self):
        result = validate(event("run_error"))
        assert result.kind == "event" and result.fields["type"] == "run_error"
        assert result.fields["severity"] == "error"
        assert result.fields["data"]["error"] == "node failed" and result.fields["data"]["count"] == 1
        assert result.fields["executionId"] == "exec-1"

    def test_clip_ready(self):
        result = validate(event("clip_ready"))
        assert result.fields["severity"] == "info"
        data = result.fields["data"]
        assert data["clipId"] == "clip-2026-10-18T10.00.00Z" and data["revision"] == 1
        assert data["s3"]["sidecarKey"].endswith("clip.json") and data["s3Error"] is None
        assert data["file"]["sha256"] == "ab" * 32
        assert data["reasons"] == {"no-helmet": {"frames": 10, "objects": 12}}
        assert data["totals"]["perClass"] == {"human": 20}

    def test_clip_ready_without_s3(self):
        message = event("clip_ready")
        message["data"]["s3"] = None
        message["data"]["s3Error"] = "upload failed: AccessDenied"
        data = validate(message).fields["data"]
        assert data["s3"] is None and data["s3Error"] == "upload failed: AccessDenied"

    def test_violation_confirmed(self):
        result = validate(event("violation_confirmed"))
        assert result.fields["severity"] == "warning"
        assert result.fields["data"] == {"reasons": {"no-helmet": 2}, "people": 3, "violatingPeople": 2, "clipId": "clip-1"}
        message = event("violation_confirmed")
        message["data"]["clipId"] = None
        assert validate(message).fields["data"]["clipId"] is None

    def test_explicit_severity_is_kept(self):
        assert validate(event("run_error", severity="warning")).fields["severity"] == "warning"
        result = validate(event("run_error", severity="loud"))
        assert result.fields["severity"] == "error" and result.dropped == ["severity"]

    def test_data_refusals(self):
        message = event("clip_ready")
        message["data"]["clipId"] = "bad clip id"
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        message = event("clip_ready")
        message["data"]["endTs"] = message["data"]["startTs"] - 1
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(message)
        assert "endTs" in caught.value.detail
        message = event("clip_ready")
        message["data"]["endTs"] = message["data"]["startTs"] + 3_600_001
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        message = event("clip_ready")
        message["data"]["endTs"] = message["data"]["startTs"] + 3_600_000
        validate(message)
        message = event("clip_ready")
        del message["data"]["revision"]
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        message = event("run_error")
        message["data"]["error"] = "e" * 1025
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        message = event("run_error")
        del message["data"]
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)
        with pytest.raises(schemas.SchemaInvalid):
            validate(event("run_error", data="broken"))
        with pytest.raises(schemas.SchemaInvalid):
            validate(event("run_error", type="explosion"))
        with pytest.raises(schemas.SchemaInvalid):
            validate(event("run_error", eventId="not-a-uuid"))
        message = event("violation_confirmed")
        del message["data"]["reasons"]
        with pytest.raises(schemas.SchemaInvalid):
            validate(message)

    def test_optional_data_fields_are_dropped_not_refused(self):
        message = event("clip_ready")
        message["data"]["file"]["width"] = 9999
        result = validate(message)
        assert "width" not in result.fields["data"]["file"] and result.dropped == ["data.file.width"]
        message = event("run_error")
        message["data"]["count"] = 0
        result = validate(message)
        assert "count" not in result.fields["data"] and result.dropped == ["data.count"]
        message = event("clip_ready")
        message["data"]["s3"] = {"bucket": "b"}
        result = validate(message)
        assert "s3" not in result.fields["data"] and result.dropped == ["data.s3"]

    def test_an_event_may_be_minimal(self):
        message = event("run_error")
        del message["executionId"]
        message["data"] = {"error": ""}
        result = validate(message)
        assert result.fields["data"] == {"error": ""} and "executionId" not in result.fields


# --------------------------------------------------------------------------- live frames
class TestLive:
    def test_a_frame_with_the_right_slot(self):
        result = validate(live(seq=17), usecase_id=UC)
        assert result.kind == "live" and result.fields["seq"] == 17
        assert result.fields["frame"]["key"].endswith("/1.jpg")
        assert result.fields["perClass"] == {"helmet": 2}
        assert result.fields["instance"] == INSTANCE

    def test_a_key_whose_slot_differs_is_rejected(self):
        message = live(seq=17)
        message["frame"]["key"] = message["frame"]["key"].replace("/1.jpg", "/2.jpg")
        with pytest.raises(schemas.SchemaInvalid) as caught:
            validate(message, usecase_id=UC)
        assert "frame.key" in caught.value.detail
        other = live(seq=17)
        other["frame"]["key"] = "dda-telemetry/live/{0}/{1}/{2}/1.jpg".format(str(uuid.uuid4()), DEVICE, WF)
        with pytest.raises(schemas.SchemaInvalid):
            validate(other, usecase_id=UC)
        padded = live(seq=17)
        padded["frame"]["key"] = padded["frame"]["key"].replace("/1.jpg", "/01.jpg")
        with pytest.raises(schemas.SchemaInvalid):
            validate(padded, usecase_id=UC)
        # The slot is written without leading zeros, and seq 0 uses slot 0.
        assert schemas.live_key_for(UC, DEVICE, WF, 0).endswith("/0.jpg")
        assert schemas.live_key_for(UC, DEVICE, WF, 4000).endswith("/0.jpg")
        assert schemas.live_key_for(UC, DEVICE, WF, 4001).endswith("/1.jpg")
        assert schemas.live_key_for(UC, DEVICE, WF, 15).endswith("/15.jpg")
        validate(live(seq=0), usecase_id=UC)
        validate(live(seq=4000), usecase_id=UC)

    def test_without_a_use_case_the_slot_is_not_checked(self):
        message = live(seq=17)
        message["frame"]["key"] = "elsewhere.jpg"
        validate(message)

    def test_optional_fields_may_be_absent(self):
        message = live()
        for name in ("perClass", "people", "violation", "reasons"):
            del message[name]
        result = validate(message, usecase_id=UC)
        assert result.dropped == [] and "violation" not in result.fields

    def test_frame_limits(self):
        message = live()
        message["frame"]["bytes"] = 307_201
        with pytest.raises(schemas.SchemaInvalid):
            validate(message, usecase_id=UC)
        message = live()
        message["frame"]["width"] = 1281
        with pytest.raises(schemas.SchemaInvalid):
            validate(message, usecase_id=UC)
        message = live()
        message["frame"]["height"] = 721
        with pytest.raises(schemas.SchemaInvalid):
            validate(message, usecase_id=UC)
        with pytest.raises(schemas.SchemaInvalid):
            validate(live(instance="agent-1"), usecase_id=UC)
        with pytest.raises(schemas.SchemaInvalid):
            validate(live(seq=-1), usecase_id=UC)
        message = live()
        del message["frame"]
        with pytest.raises(schemas.SchemaInvalid):
            validate(message, usecase_id=UC)


# --------------------------------------------------------------------------- decode
class TestDecode:
    def test_decode_payload(self):
        document, text = schemas.decode_payload(encode(summary()))
        assert document["schema"] == "dda.summary.v1" and json.loads(text) == document

    def test_decode_refusals(self):
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.decode_payload("not base64!!")
        assert caught.value.reason == "invalid_json"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.decode_payload(base64.b64encode(b"[1, 2]").decode())
        assert caught.value.reason == "invalid_json"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.decode_payload(base64.b64encode(b"\xff\xfe").decode())
        assert caught.value.reason == "invalid_json"
        with pytest.raises(schemas.SchemaInvalid) as caught:
            schemas.decode_payload(base64.b64encode(b"x" * 131073).decode())
        assert caught.value.reason == "too_large"
        with pytest.raises(schemas.SchemaInvalid):
            schemas.decode_payload(None)

    def test_coerce_rule(self):
        assert schemas.coerce("true") is True and schemas.coerce(" False ") is False
        assert schemas.coerce("12") == 12 and schemas.coerce("1.5") == 1.5
        assert schemas.coerce("abc") == "abc" and schemas.coerce(3) == 3
        assert schemas.coerce(None) is None


# --------------------------------------------------------------------------- catalog and templates
class TestCatalogAndTemplates:
    def test_every_catalog_entry_is_well_formed(self):
        folds = set(schemas.CATALOG["folds"])
        assert folds == {"sum", "max", "mean", "p50mean", "p95max", "worst", "last", "count"}
        ids = [m["id"] for m in schemas.catalog_measures()]
        assert len(ids) == len(set(ids))
        for measure in schemas.catalog_measures():
            assert measure["fold"] in folds, measure["id"]
            assert measure["source"] in ("summary", "event"), measure["id"]
            assert isinstance(measure["perKey"], bool)
            assert measure["unit"] and measure["label"]
            assert measure["perKey"] == ("*" in measure["path"]), measure["id"]

    def test_every_summary_measure_path_is_a_declared_field(self):
        """Every measure an adapter emits is in the catalog, and every catalog
        path points at a field the Summary adapter declares."""
        fields = schemas.ADAPTERS["dda.summary.v1"].fields
        for measure in schemas.catalog_measures():
            if measure["source"] != "summary" or measure.get("derived"):
                continue
            spec = {"type": "object", "fields": fields}
            for part in measure["path"].split("."):
                if part == "*":
                    assert spec["type"] in ("objectmap", "countmap", "enummap"), measure["id"]
                    spec = {"type": "object", "fields": spec.get("fields", {})} if spec["type"] == "objectmap" else {"type": "leaf"}
                    continue
                if spec["type"] == "extensions":
                    spec = spec["known"][part]
                    continue
                assert spec["type"] == "object", (measure["id"], part)
                assert part in spec["fields"], (measure["id"], part)
                spec = spec["fields"][part]
            assert spec["type"] in ("int", "number", "bool", "modelState", "leaf"), measure["id"]

    def test_the_declared_fields_the_fold_reads_are_all_in_the_catalog(self):
        """The §4.1 'Measures emitted' list, by id."""
        expected = {
            "runs_completed", "runs_failed", "runs_dropped", "latency_p50_ms", "latency_p95_ms", "latency_max_ms",
            "detections_total", "runs_with_detections", "detections_class_total", "detections_class_max",
            "anomalous", "normal", "people_seen_max", "model_state_worst", "model_waits", "model_wait_ms",
            "continuous_skipped_no_new_frame", "continuous_paused", "continuous_fps", "ppe_people_seen_max",
            "ppe_people_violating_max", "ppe_people_compliant_max", "ppe_violation_frames", "ppe_reason_frames",
            "ppe_reason_objects", "ppe_clips_opened", "ppe_frames", "telemetry_queue_drops",
            "telemetry_publish_failures", "telemetry_events_dropped", "summaries", "gaps",
            "events_violation_confirmed", "events_clip_ready", "events_run_error",
        }
        assert {m["id"] for m in schemas.catalog_measures()} == expected
        assert schemas.measure_by_id("runs_completed")["fold"] == "sum"
        assert schemas.measure_by_id("latency_p50_ms")["fold"] == "p50mean"
        assert schemas.measure_by_id("latency_p95_ms")["fold"] == "p95max"
        assert schemas.measure_by_id("model_state_worst")["fold"] == "worst"
        assert schemas.measure_by_id("continuous_paused")["fold"] == "last"
        assert schemas.measure_by_id("continuous_fps")["fold"] == "mean"
        assert schemas.measure_by_id("summaries")["fold"] == "count"
        assert schemas.measure_by_id("nope") is None
        event_types = set(schemas.ADAPTERS["dda.event.v1"].fields["type"]["values"])
        assert {m["eventType"] for m in schemas.catalog_measures() if m["source"] == "event"} == event_types

    def test_every_template_measure_exists_in_the_catalog(self):
        assert set(schemas.TEMPLATES) == {"generic-detector", "anomaly", "ppe-compliance"}
        catalog_ids = {m["id"] for m in schemas.catalog_measures()}
        for template in schemas.TEMPLATES.values():
            assert template["version"] == 1
            assert set(template["requires"]) == {"blocks", "extensions"}
            for block in template["requires"]["blocks"]:
                assert block in schemas.REGISTRY["blocks"]
            for extension in template["requires"]["extensions"]:
                assert extension in schemas.REGISTRY["extensions"]
            assert template["panels"]
            for panel in template["panels"]:
                assert panel["id"] and panel["title"] and panel["chart"] and panel["measures"]
            missing = [m for m in schemas.template_measures(template) if m not in catalog_ids]
            assert missing == [], template["id"]
            assert set(template["tiles"]) <= {"now", "health", "clips", "live"}
        assert schemas.TEMPLATES["ppe-compliance"]["tiles"] == ["now", "health", "clips", "live"]
        assert "clips" not in schemas.TEMPLATES["generic-detector"]["tiles"]
        assert schemas.TEMPLATES["ppe-compliance"]["requires"]["extensions"] == ["ppe"]
        assert schemas.TEMPLATES["anomaly"]["requires"]["blocks"] == ["runs", "outcomes"]
        assert "events_clip_ready" in schemas.template_measures(schemas.TEMPLATES["ppe-compliance"])

    def test_the_panels_of_section_eight_two(self):
        generic = [p["id"] for p in schemas.TEMPLATES["generic-detector"]["panels"]]
        assert generic == ["runs", "latency", "detections", "people", "model-state", "model-waits"]
        anomaly = [p["id"] for p in schemas.TEMPLATES["anomaly"]["panels"]]
        assert anomaly == ["runs", "anomalous-share", "latency", "model-state"]
        ppe = [p["id"] for p in schemas.TEMPLATES["ppe-compliance"]["panels"]]
        assert ppe[:6] == generic
        assert ppe[6:] == ["violations-by-reason", "people-compliance", "clips", "violation-events"]
        model_state = next(p for p in schemas.TEMPLATES["generic-detector"]["panels"] if p["id"] == "model-state")
        assert model_state["measures"] == ["model_state_worst"] and model_state["chart"] == "stacked-bar-states"

    def test_a_template_naming_an_unknown_measure_fails(self):
        template = copy.deepcopy(schemas.TEMPLATES["anomaly"])
        template["panels"][0]["measures"].append("runs_imagined")
        catalog_ids = {m["id"] for m in schemas.catalog_measures()}
        missing = [m for m in schemas.template_measures(template) if m not in catalog_ids]
        assert missing == ["runs_imagined"]

    def test_the_catalog_content_hash_changes_only_with_a_version_bump(self):
        version = schemas.CATALOG["version"]
        assert version in CATALOG_HASHES, "a new catalog version needs its hash recorded here"
        assert schemas.catalog_content_hash() == CATALOG_HASHES[version], (
            "the catalog content changed without a version bump (design §8.1)")

    def test_resolve_path(self):
        fields = validate(summary(ext={"ppe": ppe_ext()})).fields
        assert schemas.resolve_path(fields, "runs.completed") == 12
        assert schemas.resolve_path(fields, "detections.perClass.*.total") == {"helmet": 30, "human": 14}
        assert schemas.resolve_path(fields, "ext.ppe.violations.byReason.*.frames") == {"no-helmet": 2}
        assert schemas.resolve_path(fields, "model.state") == "READY"
        assert schemas.resolve_path(fields, "latency.nope") is None
        assert schemas.resolve_path(fields, "outcomes.anomalous.deeper") is None
        assert schemas.resolve_path(fields, "") is None
        assert schemas.resolve_path(validate(heartbeat()).fields, "runs.completed") is None
