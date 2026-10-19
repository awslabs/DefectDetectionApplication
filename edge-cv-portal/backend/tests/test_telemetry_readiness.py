"""``telemetry.readiness`` (workflow-telemetry-dashboards design §10.1;
tasks.md 3.4): each item ready and not ready with its remedy; the probe
against stubbed responses with and without the ``x-amz-bucket-region``
header -> ``region_source`` ``header``/``usecase`` and the cross-region
warning; ``NoSuchBucket`` and ``AccessDenied``; the enforcement warning text;
the 60 s cache."""
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

import telemetry_tables

PORTAL_ACCOUNT = "111111111111"
CROSS_ACCOUNT = "222222222222"
REGION = "us-east-1"
T0 = 1_792_000_000_000


def client_error(code, operation="Op", headers=None, status=400):
    response = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    if headers:
        response["ResponseMetadata"]["HTTPHeaders"] = headers
    return ClientError(response, operation)


class StubS3:
    def __init__(self):
        self.calls = []
        self.region_header = REGION
        self.list_raises = None
        self.rules = []
        self.lifecycle_raises = None
        self.versioning = None

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        if self.list_raises:
            raise self.list_raises
        headers = {"x-amz-bucket-region": self.region_header} if self.region_header else {}
        return {"KeyCount": 0, "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": headers}}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.calls.append(("get_bucket_lifecycle_configuration", kwargs))
        if self.lifecycle_raises:
            raise self.lifecycle_raises
        return {"Rules": list(self.rules)}

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        return {"Status": self.versioning} if self.versioning else {}


class StubIot:
    def __init__(self, account=CROSS_ACCOUNT):
        self.account = account
        self.raises = None
        self.function_arn = "arn:aws:lambda:{0}:{1}:function:dda-portal-telemetry-ingest:acct-{2}".format(
            REGION, PORTAL_ACCOUNT, account)

    def get_topic_rule(self, **kwargs):
        if self.raises:
            raise self.raises
        return {"rule": {"ruleName": kwargs["ruleName"], "actions": [{"lambda": {"functionArn": self.function_arn}}]}}


class StubGreengrass:
    def __init__(self, devices):
        self.devices = devices      # name -> managed bool

    def list_core_devices(self, **kwargs):
        return {"coreDevices": [{"coreDeviceThingName": n} for n in sorted(self.devices)]}

    def list_tags_for_resource(self, **kwargs):
        name = kwargs["resourceArn"].rsplit(":", 1)[-1]
        return {"tags": {"dda-portal:managed": "true"} if self.devices.get(name) else {}}


class Harness:
    def __init__(self, module, env, monkeypatch):
        self.module = module
        self.env = env
        self.s3 = StubS3()
        self.iot = StubIot()
        self.gg = StubGreengrass({})
        self.purposes = []
        from edge_telemetry_common import credentials

        def fake_client(service, usecase, purpose, region=None, **kwargs):
            self.purposes.append((service, purpose, kwargs.get("bucket")))
            return {"s3": self.s3, "iot": self.iot, "greengrassv2": self.gg}[service]

        monkeypatch.setattr(credentials, "client", fake_client)
        self.tables = telemetry_tables.tables()

    def usecase(self, cross=False, **over):
        account = CROSS_ACCOUNT if cross else PORTAL_ACCOUNT
        base = {"usecase_id": str(uuid.uuid4()), "account_id": account, "region": REGION,
                "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(account)}
        base.update(over)
        self.env.stack.tables.usecases.put_item(Item=base)
        return base

    def record(self, usecase, device, other_usecase=None):
        self.env.stack.tables.devices.put_item(Item={"device_id": device, "usecase_id": other_usecase or usecase["usecase_id"],
                                                     "target_architecture": "arm64_jp5"})

    def checklist(self, usecase, settings=None, now=T0 / 1000.0, clip_days=None):
        from edge_telemetry_common import devices
        devices.clear_cache()
        self.module.clear_cache()
        return {item["id"]: item for item in self.module.checklist(
            usecase, settings or {"enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60},
            config_table=self.tables["config"], devices_table=self.env.stack.tables.devices,
            portal_region=REGION, clip_days=clip_days, now=now)}


@pytest.fixture
def ready(env, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("PORTAL_REGISTRY_ENFORCED", raising=False)
    for name in list(sys.modules):
        if name == "telemetry" or name.startswith("telemetry.") or name.startswith("edge_telemetry_common"):
            sys.modules.pop(name, None)
    from telemetry import readiness as module
    return Harness(module, env, monkeypatch)


class TestItems:
    def test_portal_topology_ingestion_region_and_no_role_form(self, ready):
        items = ready.checklist(ready.usecase())
        assert items["ingestion_resources"]["ready"] is True and items["ingestion_resources"]["topology"] == "portal"
        assert items["region"]["ready"] is True
        assert "role_form" not in items
        assert list(items) == ["ingestion_resources", "region", "device_records", "telemetry_source", "media_location",
                               "media_retention", "registry_enforcement", "device_shadows"]
        assert ("iot", "api-control", None) not in ready.purposes

    def test_cross_topology_ingestion_resources(self, ready):
        usecase = ready.usecase(cross=True)
        items = ready.checklist(usecase)
        assert items["ingestion_resources"]["ready"] is True and items["ingestion_resources"]["topology"] == "cross"
        assert ("iot", "api-control", None) in ready.purposes
        assert items["role_form"]["ready"] is True
        ready.iot.function_arn = ready.iot.function_arn.replace("acct-" + CROSS_ACCOUNT, "acct-333333333333")
        items = ready.checklist(usecase)
        assert items["ingestion_resources"]["ready"] is False and items["ingestion_resources"]["error"] == "rule_action_mismatch"
        assert items["ingestion_resources"]["remedy"].startswith("Update the use-case account template")
        ready.iot.raises = client_error("ResourceNotFoundException", "GetTopicRule")
        items = ready.checklist(usecase)
        assert items["ingestion_resources"]["ready"] is False and items["ingestion_resources"]["error"] == "ResourceNotFoundException"

    def test_region_and_role_form(self, ready):
        items = ready.checklist(ready.usecase(region="eu-west-1"))
        assert items["region"]["ready"] is False and "outside the Portal's region" in items["region"]["remedy"]
        bad_role = ready.usecase(cross=True, cross_account_role_arn="arn:aws:iam::{0}:root".format(CROSS_ACCOUNT))
        items = ready.checklist(bad_role)
        assert items["role_form"]["ready"] is False and "DDAPortalAccessRole" in items["role_form"]["remedy"]
        wrong_account = ready.usecase(cross=True, cross_account_role_arn="arn:aws:iam::333333333333:role/DDAPortalAccessRole")
        assert ready.checklist(wrong_account)["role_form"]["ready"] is False
        assert ready.module.role_arn_valid({"account_id": CROSS_ACCOUNT, "cross_account_role_arn": "arn:aws:iam::{0}:role/X".format(CROSS_ACCOUNT)})

    def test_device_records(self, ready):
        usecase = ready.usecase()
        other = ready.usecase()
        ready.gg.devices = {"thing-a": True, "thing-b": True, "thing-c": True, "unmanaged": False}
        ready.record(usecase, "thing-a")
        ready.record(usecase, "thing-c", other_usecase=other["usecase_id"])
        items = ready.checklist(usecase)
        item = items["device_records"]
        assert item["ready"] is False
        assert item["unrecorded"] == ["thing-b", "thing-c"]
        assert item["other_usecase"] == {"thing-c": other["usecase_id"]}
        assert item["recorded"] == ["thing-a"] and item["managed"] == ["thing-a", "thing-b", "thing-c"]
        assert "Target architecture" in item["remedy"] and "node_designer:manage" in item["remedy"]
        assert ("greengrassv2", "api-control", None) in ready.purposes
        ready.record(usecase, "thing-b")
        ready.record(usecase, "thing-c")
        assert ready.checklist(usecase)["device_records"]["ready"] is True

    def test_telemetry_source(self, ready):
        usecase = ready.usecase()
        items = ready.checklist(usecase)
        assert items["telemetry_source"]["ready"] is False and "Telemetry_Agent" in items["telemetry_source"]["remedy"]
        ready.tables["config"].put_item(Item={"pk": "UC#" + usecase["usecase_id"], "sk": "SEEN", "receivedMs": T0 - 1000,
                                              "expires_at": T0 // 1000 + 3600})
        items = ready.checklist(usecase)
        assert items["telemetry_source"]["ready"] is True and items["telemetry_source"]["lastSeenMs"] == T0 - 1000
        ready.tables["config"].put_item(Item={"pk": "UC#" + usecase["usecase_id"], "sk": "SEEN", "receivedMs": T0 - 1000,
                                              "expires_at": T0 // 1000 - 1})
        assert ready.checklist(usecase)["telemetry_source"]["ready"] is False, "an expired marker does not count"

    def test_media_location_probe(self, ready):
        usecase = ready.usecase()
        items = ready.checklist(usecase)
        item = items["media_location"]
        assert item["ready"] is True and item["media_location"]["region"] == REGION
        assert item["media_location"]["region_source"] == "header" and "warning" not in item
        assert item["media_location"]["bucket"] == "dda-inference-results-" + PORTAL_ACCOUNT
        call = ready.s3.calls[0]
        assert call[0] == "list_objects_v2" and call[1]["MaxKeys"] == 1
        assert call[1]["Prefix"] == "dda-telemetry/live/{0}/".format(usecase["usecase_id"])
        assert ("s3", "media-read", "dda-inference-results-" + PORTAL_ACCOUNT) in ready.purposes
        # Cross-region warning.
        ready.s3.region_header = "eu-west-1"
        item = ready.checklist(usecase)["media_location"]
        assert item["ready"] is True and item["media_location"]["region"] == "eu-west-1"
        assert "eu-west-1" in item["warning"] and REGION in item["warning"] and "across regions" in item["warning"]
        # Header absent: the use case's region with a warning.
        ready.s3.region_header = None
        item = ready.checklist(usecase)["media_location"]
        assert item["media_location"]["region"] == REGION and item["media_location"]["region_source"] == "usecase"
        assert "header absent" in item["warning"]
        # A redirect carrying the header still answers the region.
        ready.s3.list_raises = client_error("PermanentRedirect", "ListObjectsV2", {"x-amz-bucket-region": "us-west-2"}, 301)
        item = ready.checklist(usecase)["media_location"]
        assert item["ready"] is True and item["media_location"]["region"] == "us-west-2"
        # NoSuchBucket and AccessDenied.
        ready.s3.list_raises = client_error("NoSuchBucket", "ListObjectsV2", status=404)
        item = ready.checklist(usecase)["media_location"]
        assert item["ready"] is False and item["error"] == "NoSuchBucket"
        assert item["remedy"] == "Create the use case's inference-results bucket, or set its override."
        ready.s3.list_raises = client_error("AccessDenied", "ListObjectsV2", status=403)
        item = ready.checklist(usecase)["media_location"]
        assert item["ready"] is False and item["error"] == "AccessDenied" and "dda-portal:managed=true" in item["remedy"]

    def test_probe_function_directly(self, ready):
        probe = ready.module.probe_media_location({"usecase_id": "u", "region": REGION}, "b", client=ready.s3)
        assert probe == (True, REGION, "header", None, None)
        ready.s3.region_header = None
        probe = ready.module.probe_media_location({"usecase_id": "u", "region": REGION}, "b", client=ready.s3)
        assert probe.region_source == "usecase" and probe.warning == ready.module.HEADER_ABSENT_WARNING

    def test_media_retention(self, ready):
        from telemetry import retention
        usecase = ready.usecase()
        item = ready.checklist(usecase)["media_retention"]
        assert item["ready"] is False and item["reason"] == "media_retention_unset"
        assert "telemetry_lifecycle.py" in item["remedy"]
        assert ("s3", "media-retention-read", "dda-inference-results-" + PORTAL_ACCOUNT) in ready.purposes
        ready.s3.rules = retention.telemetry_rules(clip_days=90)
        item = ready.checklist(usecase, clip_days=90)["media_retention"]
        assert item["ready"] is True and item["versioning"] == "never" and item["clipRuleDays"] == 90 and "warning" not in item
        ready.s3.versioning = "Enabled"
        item = ready.checklist(usecase)["media_retention"]
        assert item["ready"] is False and item["reason"] == "media_versioned"
        ready.s3.versioning = "Suspended"
        item = ready.checklist(usecase, clip_days=30)["media_retention"]
        assert item["ready"] is True and "90 days" in item["warning"] and "30" in item["warning"]
        ready.s3.lifecycle_raises = client_error("NoSuchLifecycleConfiguration", "GetBucketLifecycleConfiguration", status=404)
        item = ready.checklist(usecase)["media_retention"]
        assert item["ready"] is False and item["reason"] == "media_retention_unset"
        ready.s3.lifecycle_raises = client_error("AccessDenied", "GetBucketLifecycleConfiguration", status=403)
        item = ready.checklist(usecase)["media_retention"]
        assert item["ready"] is False and item["reason"] == "lifecycle_unreadable" and item["error"] == "AccessDenied"

    def test_registry_enforcement_warning(self, ready, monkeypatch):
        usecase = ready.usecase()
        item = ready.checklist(usecase)["registry_enforcement"]
        assert item["ready"] is True and item["enforced"] is False
        assert item["warning"] == ready.module.ENFORCEMENT_WARNING
        assert "dashboards:read" in item["warning"] and "clip playback and live frames are refused" in item["warning"]
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
        item = ready.checklist(usecase)["registry_enforcement"]
        assert item["enforced"] is True and "warning" not in item

    def test_device_shadows(self, ready):
        from telemetry import shadow
        usecase = ready.usecase()
        settings = {"enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60}
        ready.record(usecase, "thing-a")
        ready.record(usecase, "thing-b")
        item = ready.checklist(usecase, settings)["device_shadows"]
        assert item["ready"] is False and item["pending"] == ["thing-a", "thing-b"] and item["failed"] == {}
        assert "online" in item["remedy"]
        desired = shadow.desired_document(usecase["usecase_id"], settings, 0)
        shadow.record_devshadow(ready.tables["config"], usecase["usecase_id"], "thing-a", desired, {"result": "ok", "writtenAt": T0})
        shadow.record_devshadow(ready.tables["config"], usecase["usecase_id"], "thing-b", desired,
                                {"result": "failed", "error": "thing_not_found", "writtenAt": T0})
        item = ready.checklist(usecase, settings)["device_shadows"]
        assert item["ready"] is False and item["pending"] == [] and item["failed"] == {"thing-b": "thing_not_found"}
        shadow.record_devshadow(ready.tables["config"], usecase["usecase_id"], "thing-b", desired, {"result": "ok", "writtenAt": T0})
        assert ready.checklist(usecase, settings)["device_shadows"]["ready"] is True
        # A settings change makes the recorded hash stale.
        changed = dict(settings, summaryIntervalS=20)
        item = ready.checklist(usecase, changed)["device_shadows"]
        assert item["pending"] == ["thing-a", "thing-b"]


class TestCache:
    def test_items_are_cached_sixty_seconds(self, ready):
        usecase = ready.usecase()
        ready.gg.devices = {"thing-a": True}
        first = ready.module.checklist(usecase, {}, config_table=ready.tables["config"], devices_table=ready.env.stack.tables.devices,
                                       portal_region=REGION, now=T0 / 1000.0)
        calls = len(ready.s3.calls)
        ready.s3.region_header = "eu-west-1"
        second = ready.module.checklist(usecase, {}, config_table=ready.tables["config"], devices_table=ready.env.stack.tables.devices,
                                        portal_region=REGION, now=T0 / 1000.0 + 59)
        assert len(ready.s3.calls) == calls, "no new probe within 60 s"
        assert [i for i in second if i["id"] == "media_location"][0]["media_location"]["region"] == REGION
        third = ready.module.checklist(usecase, {}, config_table=ready.tables["config"], devices_table=ready.env.stack.tables.devices,
                                       portal_region=REGION, now=T0 / 1000.0 + 61)
        assert len(ready.s3.calls) > calls
        assert [i for i in third if i["id"] == "media_location"][0]["media_location"]["region"] == "eu-west-1"
        assert first is not third
