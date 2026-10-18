"""``workflow_debug.envelope`` (workflow-debug-mode design §4.1, §4.3): the
required fields and types, ``instance`` a UUID, 1-200 records each with a
``type`` and an integer ``ts``, unknown record types passed through, and the
reasons ``invalid_json``, ``too_large``, ``schema_invalid``.
"""
import base64
import copy
import json

import pytest

from workflow_debug import envelope as env

SESSION = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
INSTANCE = "0b1c2d3e-4f5a-4b6c-8d9e-0f1a2b3c4d5e"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


def valid():
    return {
        "schema": "dda.debug.v1", "sessionId": SESSION, "instance": INSTANCE,
        "deviceId": "core-thing", "workflowId": WF, "workflowVersion": "1",
        "seq": 17, "sentAt": 1792075062610,
        "records": [
            {"type": "run", "ts": 1792075062600, "executionId": "e-1", "status": "completed"},
            {"type": "log", "ts": 1792075062601, "level": "INFO", "logger": "x", "message": "m",
             "attribution": "between_runs"},
            {"type": "drop", "ts": 1792075062602, "nodeId": "n1", "reason": "queue_full", "count": 2},
            {"type": "session", "ts": 1792075062603, "state": "active", "reason": "heartbeat"},
        ],
    }


def encode(document):
    return base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")


class TestDecodePayload:
    def test_round_trip(self):
        assert env.decode_payload(encode(valid())) == valid()

    @pytest.mark.parametrize("payload", [None, "", 7, "not base64!!", base64.b64encode(b"\xff\xfe").decode()])
    def test_invalid_json(self, payload):
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.decode_payload(payload)
        assert excinfo.value.reason == "invalid_json"

    def test_a_json_array_is_invalid_json(self):
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.decode_payload(base64.b64encode(b"[1, 2]").decode())
        assert excinfo.value.reason == "invalid_json"

    def test_too_large_above_128_kb(self):
        big = valid()
        big["records"][0]["padding"] = "x" * (128 * 1024)
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.decode_payload(encode(big))
        assert excinfo.value.reason == "too_large"

    def test_exactly_128_kb_is_accepted(self):
        document = valid()
        raw = json.dumps(document).encode("utf-8")
        document["records"][0]["padding"] = "x" * (env.MAX_MESSAGE_BYTES - len(raw) - len('"padding":"",') - 1)
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        assert len(raw) <= env.MAX_MESSAGE_BYTES
        env.decode_payload(base64.b64encode(raw).decode())

    def test_no_payload_value_in_the_error_text(self):
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.decode_payload(base64.b64encode(b"{secret-looking}").decode())
        assert "secret" not in str(excinfo.value)


class TestValidateEnvelope:
    def test_valid_envelope_is_returned(self):
        document = valid()
        assert env.validate_envelope(document) is document

    def test_integer_workflow_version_is_normalized_to_a_string(self):
        document = valid()
        document["workflowVersion"] = 3
        assert env.validate_envelope(document)["workflowVersion"] == "3"

    @pytest.mark.parametrize("field", ["schema", "sessionId", "instance", "deviceId", "workflowId",
                                       "workflowVersion", "seq", "sentAt", "records"])
    def test_each_required_field(self, field):
        document = valid()
        del document[field]
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.validate_envelope(document)
        assert excinfo.value.reason == "schema_invalid"
        assert excinfo.value.detail == field

    @pytest.mark.parametrize("field,value", [
        ("schema", "dda.debug.v2"), ("sessionId", "not-a-uuid"), ("instance", "0b1c2d3e"),
        ("instance", 7), ("deviceId", ""), ("deviceId", "thing with spaces"), ("workflowId", "a/b"),
        ("workflowVersion", ""), ("workflowVersion", True), ("seq", 0), ("seq", "17"), ("seq", 2.0),
        ("seq", True), ("sentAt", -1), ("sentAt", "now"), ("records", []), ("records", {}),
        ("records", [{"type": "run", "ts": 1}] * 201),
    ])
    def test_each_type_and_range(self, field, value):
        document = valid()
        document[field] = value
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.validate_envelope(document)
        assert excinfo.value.reason == "schema_invalid"
        assert excinfo.value.detail.startswith(field)

    def test_two_hundred_records_are_accepted(self):
        document = valid()
        document["records"] = [{"type": "log", "ts": i} for i in range(200)]
        env.validate_envelope(document)

    def test_thing_names_may_carry_colons(self):
        document = valid()
        document["deviceId"] = "site:line-3:cam_01"
        env.validate_envelope(document)

    @pytest.mark.parametrize("record,detail", [
        ("not a dict", "records[0]"),
        ({"ts": 1}, "records[0].type"),
        ({"type": "", "ts": 1}, "records[0].type"),
        ({"type": 7, "ts": 1}, "records[0].type"),
        ({"type": "run"}, "records[0].ts"),
        ({"type": "run", "ts": "1"}, "records[0].ts"),
        ({"type": "run", "ts": 1.5}, "records[0].ts"),
        ({"type": "run", "ts": True}, "records[0].ts"),
    ])
    def test_each_record_needs_a_type_and_an_integer_ts(self, record, detail):
        document = valid()
        document["records"] = [record]
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.validate_envelope(document)
        assert excinfo.value.detail == detail

    def test_the_failing_record_index_is_named(self):
        document = valid()
        document["records"][2] = {"type": "drop"}
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.validate_envelope(document)
        assert excinfo.value.detail == "records[2].ts"

    def test_unknown_record_types_pass_through(self):
        document = valid()
        document["records"].append({"type": "future_kind", "ts": 1792075062604, "payload": {"a": 1}})
        validated = env.validate_envelope(copy.deepcopy(document))
        assert validated["records"][-1] == document["records"][-1]
        assert env.record_types(validated["records"]) == {"run": 1, "log": 1, "drop": 1, "session": 1, "future_kind": 1}

    def test_non_dict_envelope(self):
        with pytest.raises(env.EnvelopeError) as excinfo:
            env.validate_envelope([valid()])
        assert excinfo.value.detail == "envelope"

    def test_constants(self):
        assert env.SCHEMA == "dda.debug.v1"
        assert env.MAX_MESSAGE_BYTES == 131072
        assert env.MAX_RECORDS == 200
        assert env.KNOWN_RECORD_TYPES == ("run", "log", "drop", "session")
