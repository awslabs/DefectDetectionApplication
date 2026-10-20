"""``telemetry.shadow`` (workflow-telemetry-dashboards design §5.4, §9.3,
§10.2; tasks.md 3.4): the ``iot-data`` payload with ``PERSON_CLASSES``; a
lease write with the region and a ``null`` removal; ``DEVSHADOW#``
recording; the credential purpose; error classification."""
import json
import sys
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

import telemetry_tables

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "7a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
T0 = 1_792_000_000_000


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "UpdateThingShadow")


class StubIotData:
    def __init__(self):
        self.updates = []
        self.fail = {}
        self.reported = {}

    def update_thing_shadow(self, **kwargs):
        self.updates.append(kwargs)
        failure = self.fail.get(kwargs["thingName"])
        if failure:
            if isinstance(failure, list):
                error = failure.pop(0)
                if error is None:
                    return {}
                raise error
            raise failure
        return {}

    def get_thing_shadow(self, **kwargs):
        document = self.reported.get(kwargs["thingName"])
        if document is None:
            raise client_error("ResourceNotFoundException")
        return {"payload": BytesIO(json.dumps(document).encode("utf-8"))}

    def payloads(self):
        return [json.loads(u["payload"]) for u in self.updates]


@pytest.fixture
def shadow(aws_stack, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", "111111111111")
    for name in list(sys.modules):
        if name == "telemetry" or name.startswith("telemetry.") or name.startswith("edge_telemetry_common"):
            sys.modules.pop(name, None)
    from telemetry import shadow as module
    from edge_telemetry_common import credentials
    stub = StubIotData()
    purposes = []

    def fake_client(service, usecase, purpose, region=None, **kwargs):
        purposes.append((service, purpose))
        return stub

    monkeypatch.setattr(credentials, "client", fake_client)
    module.purposes = purposes
    module.stub = stub
    return module


def usecase():
    return {"usecase_id": UC, "account_id": "111111111111", "region": "us-east-1"}


SETTINGS = {"enabled": True, "summaryIntervalS": 15, "idleHeartbeatS": 120}


class TestDesired:
    def test_the_document_carries_person_classes_and_no_live(self, shadow):
        desired = shadow.desired_document(UC, SETTINGS, T0)
        assert desired == {"v": 1, "usecaseId": UC, "enabled": True, "summaryIntervalS": 15, "idleHeartbeatS": 120,
                           "personClasses": ["person", "human"], "updatedAt": T0}
        assert "live" not in desired and shadow.PERSON_CLASSES == ["person", "human"]
        defaults = shadow.desired_document(UC, {}, T0)
        assert defaults["enabled"] is False and defaults["summaryIntervalS"] == 10 and defaults["idleHeartbeatS"] == 60

    def test_the_hash_ignores_updated_at(self, shadow):
        a = shadow.desired_hash(shadow.desired_document(UC, SETTINGS, T0))
        b = shadow.desired_hash(shadow.desired_document(UC, SETTINGS, T0 + 1))
        c = shadow.desired_hash(shadow.desired_document(UC, dict(SETTINGS, enabled=False), T0))
        assert a == b and a != c and len(a) == 64

    def test_write_desired_calls_iot_data_through_api_control_and_records_devshadow(self, shadow):
        table = telemetry_tables.tables()["config"]
        result = shadow.write_desired(usecase(), "thing-a", SETTINGS, table=table, now_ms=T0)
        assert result["result"] == "ok" and result["desiredHash"]
        assert shadow.purposes == [("iot-data", "api-control")]
        update = shadow.stub.updates[0]
        assert update["thingName"] == "thing-a" and update["shadowName"] == "dda-workflow-telemetry"
        payload = json.loads(update["payload"])
        assert payload["state"]["desired"]["personClasses"] == ["person", "human"]
        assert payload["state"]["desired"]["usecaseId"] == UC and payload["state"]["desired"]["enabled"] is True
        assert "live" not in payload["state"]["desired"]
        row = table.get_item(Key={"pk": "UC#" + UC, "sk": "DEVSHADOW#thing-a"})["Item"]
        assert row["result"] == "ok" and row["desiredHash"] == result["desiredHash"] and row["device_id"] == "thing-a"
        assert "error" not in row

    def test_a_failed_write_is_recorded(self, shadow):
        table = telemetry_tables.tables()["config"]
        shadow.stub.fail["thing-b"] = client_error("ResourceNotFoundException")
        result = shadow.write_desired(usecase(), "thing-b", SETTINGS, table=table, now_ms=T0)
        assert result["result"] == "failed" and result["error"] == "thing_not_found"
        row = table.get_item(Key={"pk": "UC#" + UC, "sk": "DEVSHADOW#thing-b"})["Item"]
        assert row["result"] == "failed" and row["error"] == "thing_not_found"

    def test_a_throttled_write_is_retried_once(self, shadow):
        shadow.stub.fail["thing-c"] = [client_error("ThrottlingException"), None]
        result = shadow.update_shadow(shadow.stub, "thing-c", {"state": {}}, sleep=lambda s: None)
        assert result["result"] == "ok" and len(shadow.stub.updates) == 2
        shadow.stub.fail["thing-d"] = [client_error("ThrottlingException"), client_error("ThrottlingException")]
        result = shadow.update_shadow(shadow.stub, "thing-d", {"state": {}}, sleep=lambda s: None)
        assert result["result"] == "failed" and result["error"] == "throttled"

    def test_write_all_desired(self, shadow):
        table = telemetry_tables.tables()["config"]
        shadow.stub.fail["thing-2"] = client_error("AccessDeniedException")
        results = shadow.write_all_desired(usecase(), ["thing-1", "thing-2", "thing-3"], SETTINGS, table=table, now_ms=T0)
        assert {d: r["result"] for d, r in results.items()} == {"thing-1": "ok", "thing-2": "failed", "thing-3": "ok"}
        assert results["thing-2"]["error"] == "access_denied"
        assert shadow.purposes == [("iot-data", "api-control")], "one client for the batch"
        assert shadow.write_all_desired(usecase(), [], SETTINGS) == {}

    def test_classify_error(self, shadow):
        assert shadow.classify_error(client_error("ResourceNotFoundException")) == "thing_not_found"
        assert shadow.classify_error(client_error("AccessDeniedException")) == "access_denied"
        assert shadow.classify_error(client_error("ThrottlingException")) == "throttled"
        assert shadow.classify_error(client_error("InvalidRequestException")) == "InvalidRequestException"
        assert shadow.classify_error(RuntimeError("x" * 300)) == "x" * 200


class TestLease:
    def test_lease_entry_and_write(self, shadow):
        entry = shadow.lease_entry(UC, "thing-a", WF, T0 + 60_000, "dda-inference-results-111111111111", "eu-west-1")
        assert entry == {"expiresAt": T0 + 60_000, "maxFps": 1, "maxWidth": 1280, "maxHeight": 720, "maxBytes": 307200,
                         "bucket": "dda-inference-results-111111111111", "region": "eu-west-1",
                         "prefix": "dda-telemetry/live/{0}/thing-a/{1}/".format(UC, WF)}
        result = shadow.write_lease(usecase(), "thing-a", WF, entry)
        assert result["result"] == "ok" and shadow.purposes == [("iot-data", "api-control")]
        payload = shadow.stub.payloads()[0]
        assert payload == {"state": {"desired": {"live": {WF: entry}}}}
        assert payload["state"]["desired"]["live"][WF]["region"] == "eu-west-1"
        assert "personClasses" not in payload["state"]["desired"], "a lease write merges only its entry"

    def test_a_null_removes_the_entry(self, shadow):
        shadow.write_lease(usecase(), "thing-a", WF, None)
        assert shadow.stub.payloads()[0] == {"state": {"desired": {"live": {WF: None}}}}

    def test_write_all_leases(self, shadow):
        shadow.stub.fail["thing-b"] = client_error("ResourceNotFoundException")
        results = shadow.write_all_leases(usecase(), [("thing-a", WF, None), ("thing-b", WF, None)])
        assert results[("thing-a", WF)]["result"] == "ok" and results[("thing-b", WF)]["error"] == "thing_not_found"
        assert shadow.write_all_leases(usecase(), []) == {}


class TestReported:
    def test_read_reported(self, shadow):
        shadow.stub.reported["thing-a"] = {"state": {"reported": {"v": 1, "enabled": True, "live": {}}}}
        assert shadow.read_reported(usecase(), "thing-a") == {"v": 1, "enabled": True, "live": {}}
        assert shadow.read_reported(usecase(), "thing-missing") is None
        shadow.stub.reported["thing-b"] = {"state": {}}
        assert shadow.read_reported(usecase(), "thing-b") is None
