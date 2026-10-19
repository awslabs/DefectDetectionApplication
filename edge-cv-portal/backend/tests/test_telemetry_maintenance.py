"""``telemetry_maintenance`` (workflow-telemetry-dashboards design §2.2,
§9.3, §10.3, §12.4; tasks.md 3.6): a run 4 minutes after the last re-sync
does nothing and one 16 minutes after re-syncs the stale device; lapsed
leases get ``null`` writes; the deletion cascade; the hourly sweep deletes
only objects older than 23 h and only under the use case's prefix; the 202
completion."""
import json
import sys
import uuid
from datetime import datetime, timezone

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import telemetry_tables
from telemetry import configdb, leases, shadow

PORTAL_ACCOUNT = "111111111111"
REGION = "us-east-1"
T0 = 1_792_000_000_000


class StubIotData:
    def __init__(self):
        self.updates = []
        self.fail = {}

    def update_thing_shadow(self, **kwargs):
        self.updates.append(kwargs)
        if kwargs["thingName"] in self.fail:
            raise self.fail[kwargs["thingName"]]
        return {}

    def payloads(self):
        return [(u["thingName"], json.loads(u["payload"])) for u in self.updates]


class StubS3:
    def __init__(self):
        self.objects = {}        # key -> LastModified datetime
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        prefix = kwargs.get("Prefix", "")
        contents = [{"Key": k, "LastModified": v} for k, v in sorted(self.objects.items()) if k.startswith(prefix)]
        return {"Contents": contents, "IsTruncated": False, "KeyCount": len(contents)}

    def delete_objects(self, **kwargs):
        self.calls.append(("delete_objects", kwargs))
        for obj in kwargs["Delete"]["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {"Deleted": kwargs["Delete"]["Objects"]}


class Harness:
    def __init__(self, env, module, iotdata, s3, purposes):
        self.env = env
        self.module = module
        self.iotdata = iotdata
        self.s3 = s3
        self.purposes = purposes
        self.tables = telemetry_tables.tables()

    def usecase(self, enabled=True, devices=("thing-a", "thing-b"), exists=True, pending=False):
        usecase_id = str(uuid.uuid4())
        record = {"usecase_id": usecase_id, "account_id": PORTAL_ACCOUNT, "region": REGION,
                  "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(PORTAL_ACCOUNT)}
        if exists:
            self.env.stack.tables.usecases.put_item(Item=record)
        names = ["{0}-{1}".format(d, usecase_id[-6:]) for d in devices]
        for device in names:
            self.env.stack.tables.devices.put_item(Item={"device_id": device, "usecase_id": usecase_id})
        settings = {"pk": "UC#" + usecase_id, "sk": "SETTINGS", "usecase_id": usecase_id, "enabled": enabled,
                    "summaryIntervalS": 10, "idleHeartbeatS": 60, "account_id": PORTAL_ACCOUNT, "region": REGION,
                    "gsi2pk": "USECASE", "gsi2sk": "UC#" + usecase_id, "shadowSyncPending": pending,
                    "media_location": {"bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "region": REGION}}
        self.tables["config"].put_item(Item=settings)
        self.module.reset_clients()
        return record, names, settings

    def devshadow(self, usecase_id, device, settings, result="ok", stale=False):
        desired = shadow.desired_document(usecase_id, dict(settings, summaryIntervalS=99) if stale else settings, 0)
        shadow.record_devshadow(self.tables["config"], usecase_id, device, desired, {"result": result, "writtenAt": T0})

    def marker(self, pk, sk, at):
        self.tables["config"].put_item(Item={"pk": pk, "sk": sk, "at": at})

    def partition(self, pk):
        return self.tables["config"].query(KeyConditionExpression=Key("pk").eq(pk))["Items"]

    def run(self, now_ms):
        return self.module.run(now_ms)


@pytest.fixture
def maint(env, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    for name in list(sys.modules):
        if name in ("telemetry_maintenance", "telemetry") or name.startswith("telemetry.") or name.startswith("edge_telemetry_common"):
            sys.modules.pop(name, None)
    import telemetry_maintenance as module
    from edge_telemetry_common import credentials
    iotdata, s3 = StubIotData(), StubS3()
    purposes = []

    def fake_client(service, usecase, purpose, region=None, **kwargs):
        purposes.append((service, purpose, kwargs.get("bucket")))
        return {"iot-data": iotdata, "s3": s3}[service]

    monkeypatch.setattr(credentials, "client", fake_client)
    # The other tests' use cases share the table; the sweep marker is fresh for this process.
    tables = telemetry_tables.tables()
    tables["config"].put_item(Item={"pk": "MAINT", "sk": "SWEEP", "at": T0})
    return Harness(env, module, iotdata, s3, purposes)


def own(summary, usecase_id):
    return {k: v for k, v in summary.items()}


class TestResync:
    def test_four_minutes_after_the_last_resync_nothing_happens_sixteen_minutes_after_the_stale_device_is_rewritten(self, maint):
        record, devices, settings = maint.usecase()
        usecase_id = record["usecase_id"]
        maint.devshadow(usecase_id, devices[0], settings)
        maint.devshadow(usecase_id, devices[1], settings, stale=True)
        maint.marker("UC#" + usecase_id, "RESYNC", T0)
        summary = maint.run(T0 + 4 * 60_000)
        assert not [r for r in summary["resynced"] if r["usecase_id"] == usecase_id]
        assert maint.iotdata.updates == []
        summary = maint.run(T0 + 16 * 60_000)
        resynced = [r for r in summary["resynced"] if r["usecase_id"] == usecase_id]
        assert resynced == [{"usecase_id": usecase_id, "devices": 2, "rewritten": [devices[1]], "ok": 1}]
        assert [name for name, _ in maint.iotdata.payloads()] == [devices[1]]
        payload = maint.iotdata.payloads()[0][1]["state"]["desired"]
        assert payload["summaryIntervalS"] == 10 and payload["personClasses"] == ["person", "human"] and payload["enabled"] is True
        marker = maint.tables["config"].get_item(Key={"pk": "UC#" + usecase_id, "sk": "RESYNC"})["Item"]
        assert int(marker["at"]) == T0 + 16 * 60_000 and int(marker["rewritten"]) == 1
        rows = {r["device_id"]: r for r in maint.partition("UC#" + usecase_id) if r["sk"].startswith("DEVSHADOW#")}
        assert rows[devices[1]]["result"] == "ok" and rows[devices[1]]["desiredHash"] == rows[devices[0]]["desiredHash"]
        assert ("iot-data", "api-control", None) in maint.purposes

    def test_missing_and_failed_records_are_rewritten_and_a_missing_marker_counts_as_old(self, maint):
        record, devices, settings = maint.usecase()
        usecase_id = record["usecase_id"]
        maint.devshadow(usecase_id, devices[0], settings, result="failed")
        summary = maint.run(T0)
        resynced = [r for r in summary["resynced"] if r["usecase_id"] == usecase_id][0]
        assert sorted(resynced["rewritten"]) == sorted(devices) and resynced["ok"] == 2

    def test_a_disabled_use_case_is_not_resynced(self, maint):
        record, devices, settings = maint.usecase(enabled=False)
        summary = maint.run(T0)
        assert not [r for r in summary["resynced"] if r["usecase_id"] == record["usecase_id"]]
        assert maint.iotdata.updates == []

    def test_the_202_completion(self, maint):
        record, devices, settings = maint.usecase(pending=True)
        usecase_id = record["usecase_id"]
        maint.marker("UC#" + usecase_id, "RESYNC", T0)
        summary = maint.run(T0 + 60_000)
        resynced = [r for r in summary["resynced"] if r["usecase_id"] == usecase_id][0]
        assert sorted(resynced["rewritten"]) == sorted(devices), "a pending sync runs regardless of the marker age"
        item = maint.tables["config"].get_item(Key={"pk": "UC#" + usecase_id, "sk": "SETTINGS"})["Item"]
        assert item["shadowSyncPending"] is False
        maint.iotdata.updates.clear()
        summary = maint.run(T0 + 120_000)
        assert not [r for r in summary["resynced"] if r["usecase_id"] == usecase_id]


class TestLeases:
    def test_lapsed_leases_get_null_writes_and_their_slots_go(self, maint):
        record, devices, settings = maint.usecase()
        usecase_id = record["usecase_id"]
        wf = str(uuid.uuid4())
        table = maint.tables["config"]
        leases.acquire(table, usecase_id, "dash", "sub-a", devices[0], wf, T0)
        leases.acquire(table, usecase_id, "dash", "sub-b", devices[0], wf, T0 + 5_000)
        leases.acquire(table, usecase_id, "dash", "sub-c", devices[1], wf, T0 + 50_000)
        maint.marker("UC#" + usecase_id, "RESYNC", T0 + 70_000)
        summary = maint.run(T0 + 70_000)
        expired = summary["expired"][usecase_id]
        assert expired["tidied"] == {"{0}/{1}".format(devices[0], wf): "ok"}
        assert expired["expired_holders"] == 2
        nulls = [(name, p) for name, p in maint.iotdata.payloads() if "live" in p["state"]["desired"]]
        assert nulls == [(devices[0], {"state": {"desired": {"live": {wf: None}}}})]
        remaining = leases.slots_of(leases.read_partition(table, usecase_id))
        assert set(remaining) == {2}, "dev-2's live slot stays"
        holders = [i for i in leases.read_partition(table, usecase_id) if i["sk"].startswith("HOLDER#")]
        assert len(holders) == 1 and holders[0]["sk"].endswith("#sub-c")
        maint.iotdata.updates.clear()
        summary = maint.run(T0 + 80_000)
        assert usecase_id not in summary["expired"], "nothing lapsed since"

    def test_a_failed_null_write_still_frees_the_slots(self, maint):
        record, devices, settings = maint.usecase()
        usecase_id = record["usecase_id"]
        wf = str(uuid.uuid4())
        table = maint.tables["config"]
        leases.acquire(table, usecase_id, "dash", "sub-a", devices[0], wf, T0)
        maint.iotdata.fail[devices[0]] = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "UpdateThingShadow")
        maint.marker("UC#" + usecase_id, "RESYNC", T0 + 70_000)
        summary = maint.run(T0 + 70_000)
        assert summary["expired"][usecase_id]["tidied"] == {"{0}/{1}".format(devices[0], wf): "failed"}
        assert leases.slots_of(leases.read_partition(table, usecase_id)) == {}


class TestCascade:
    def test_a_deleted_use_case_is_cleaned_up(self, maint):
        record, devices, settings = maint.usecase(exists=False)
        usecase_id = record["usecase_id"]
        wf = str(uuid.uuid4())
        table = maint.tables["config"]
        for device in devices:
            maint.devshadow(usecase_id, device, settings)
        leases.acquire(table, usecase_id, "dash", "sub-a", devices[0], wf, T0)
        dashboard_id = str(uuid.uuid4())
        table.put_item(Item={"pk": "DASH#" + dashboard_id, "sk": "META", "dashboard_id": dashboard_id, "usecase_id": usecase_id,
                             "name": "Line A", "gsi1pk": "UC#" + usecase_id, "gsi1sk": configdb.by_usecase_sk(T0, dashboard_id)})
        table.put_item(Item={"pk": "UC#" + usecase_id, "sk": "DASHNAME#line a", "dashboard_id": dashboard_id})
        table.put_item(Item={"pk": "UC#" + usecase_id, "sk": "DASHCOUNT", "count": 1})
        table.put_item(Item={"pk": "UC#" + usecase_id, "sk": "RESYNC", "at": T0})
        table.put_item(Item={"pk": "UC#" + usecase_id, "sk": "SEEN", "receivedMs": T0, "expires_at": T0 // 1000 + 100})
        summary = maint.run(T0 + 60_000)
        cascaded = [c for c in summary["cascaded"] if c["usecase_id"] == usecase_id][0]
        assert cascaded["switched_off"] == 2 and cascaded["dashboards"] == 1
        off = [(name, p["state"]["desired"]["enabled"]) for name, p in maint.iotdata.payloads()]
        assert sorted(off) == sorted((d, False) for d in devices)
        assert maint.partition("UC#" + usecase_id) == []
        assert maint.partition("LEASE#" + usecase_id) == []
        assert maint.partition("DASH#" + dashboard_id) == []
        assert not [r for r in summary["resynced"] if r["usecase_id"] == usecase_id]
        assert maint.run(T0 + 120_000)["cascaded"] == [] or usecase_id not in [c["usecase_id"] for c in maint.run(T0 + 120_000)["cascaded"]]

    def test_a_role_that_is_gone_does_not_stop_the_cascade(self, maint, monkeypatch):
        record, devices, settings = maint.usecase(exists=False)
        usecase_id = record["usecase_id"]
        for device in devices:
            maint.devshadow(usecase_id, device, settings)
        from edge_telemetry_common import credentials

        def denied(*args, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "AssumeRole")

        monkeypatch.setattr(credentials, "client", denied)
        summary = maint.run(T0)
        cascaded = [c for c in summary["cascaded"] if c["usecase_id"] == usecase_id][0]
        assert cascaded["switched_off"] == 0 and maint.partition("UC#" + usecase_id) == []


class TestSweep:
    def test_the_hourly_sweep_deletes_only_old_frames_under_the_use_cases_prefix(self, maint):
        record, devices, settings = maint.usecase()
        usecase_id = record["usecase_id"]
        other = str(uuid.uuid4())
        now_s = (T0 + 61 * 60_000) / 1000.0
        old = datetime.fromtimestamp(now_s - 24 * 3600, tz=timezone.utc)
        recent = datetime.fromtimestamp(now_s - 22 * 3600, tz=timezone.utc)
        prefix = "dda-telemetry/live/{0}/".format(usecase_id)
        maint.s3.objects = {
            prefix + "{0}/wf/0.jpg".format(devices[0]): old,
            prefix + "{0}/wf/1.jpg".format(devices[0]): recent,
            "dda-telemetry/live/{0}/dev/wf/0.jpg".format(other): old,
            "dda-telemetry/clips/{0}/dev/wf/2026/c.mp4".format(usecase_id): old,
        }
        maint.marker("UC#" + usecase_id, "RESYNC", T0 + 61 * 60_000)
        summary = maint.run(T0 + 61 * 60_000)
        swept = [s for s in summary["swept"] if s["usecase_id"] == usecase_id]
        assert swept == [{"usecase_id": usecase_id, "listed": 2, "deleted": 1}]
        assert set(maint.s3.objects) == {prefix + "{0}/wf/1.jpg".format(devices[0]),
                                         "dda-telemetry/live/{0}/dev/wf/0.jpg".format(other),
                                         "dda-telemetry/clips/{0}/dev/wf/2026/c.mp4".format(usecase_id)}
        listing = [c for c in maint.s3.calls if c[0] == "list_objects_v2"]
        assert all(c[1]["Prefix"] == prefix for c in listing if usecase_id in c[1]["Prefix"])
        assert ("s3", "live-expiry", "dda-inference-results-" + PORTAL_ACCOUNT) in maint.purposes
        assert summary.get("sweep_marker_written") is True
        marker = maint.tables["config"].get_item(Key={"pk": "MAINT", "sk": "SWEEP"})["Item"]
        assert int(marker["at"]) == T0 + 61 * 60_000
        # Within the hour nothing is swept again.
        maint.s3.calls.clear()
        summary = maint.run(T0 + 90 * 60_000)
        assert summary["swept"] == [] and "sweep_marker_written" not in summary
        assert not [c for c in maint.s3.calls if c[0] == "list_objects_v2"]

    def test_a_sweep_failure_is_counted_and_the_others_proceed(self, maint, monkeypatch):
        record, devices, settings = maint.usecase()
        original = maint.module.sweep_live_frames

        def failing(usecase, now_s):
            if usecase["usecase_id"] == record["usecase_id"]:
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "ListObjectsV2")
            return original(usecase, now_s)

        monkeypatch.setattr(maint.module, "sweep_live_frames", failing)
        maint.marker("UC#" + record["usecase_id"], "RESYNC", T0 + 61 * 60_000)
        summary = maint.run(T0 + 61 * 60_000)
        assert summary["errors"] >= 1 and summary.get("sweep_marker_written") is True
        assert maint.module.handler({}, None)["usecases"] >= 1
