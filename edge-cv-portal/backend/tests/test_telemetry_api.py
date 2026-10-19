"""``telemetry_api`` (workflow-telemetry-dashboards design §8-§12, §18;
tasks.md 3.5) on the conftest moto stack plus the four telemetry tables with
stubbed ``iot-data``, ``s3``, ``greengrassv2`` and ``iot`` clients.

The permission matrix per route; every settings refusal and the 202 path with
41 devices; ``unrecorded_devices``; the dashboards CRUD rules; templates
availability; ``now`` from pointers; ``series`` validation and resolutions;
``events`` paging and refusals; clips listing states and the media route
(binding before any STS or S3 call, the guard, one audit per hour); the lease
route's refusals and the slot transaction; the live route across two agent
instances; no secrets in the captured logs; lowercase error codes.
"""
import base64
import json
import logging
import sys
import uuid

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import telemetry_tables
from telemetry import fold, records, retention, schemas

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
INSTANCE = "0f1e2d3c-4b5a-4968-8776-65544332211a"
OTHER_INSTANCE = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e"
T0 = 1_792_000_000_000
ROLES = ("Viewer", "Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin", "DataLabeler")


def client_error(code, operation="Op", status=400, headers=None):
    response = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    if headers:
        response["ResponseMetadata"]["HTTPHeaders"] = headers
    return ClientError(response, operation)


class StubIotData:
    def __init__(self):
        self.updates = []
        self.fail = {}

    def update_thing_shadow(self, **kwargs):
        self.updates.append(kwargs)
        failure = self.fail.get(kwargs["thingName"])
        if failure:
            raise failure
        return {}

    def payloads(self):
        return [json.loads(u["payload"]) for u in self.updates]


class StubS3:
    def __init__(self):
        self.calls = []
        self.region_header = REGION
        self.list_raises = None
        self.rules = retention.telemetry_rules(clip_days=90)
        self.versioning = None
        self.objects = {}            # key -> {"ContentLength", "ContentType", "Metadata"}
        self.head_raises = {}

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        if self.list_raises:
            raise self.list_raises
        headers = {"x-amz-bucket-region": self.region_header} if self.region_header else {}
        count = 1 if any(k.startswith(kwargs.get("Prefix", "")) for k in self.objects) else 0
        return {"KeyCount": count, "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": headers}}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.calls.append(("get_bucket_lifecycle_configuration", kwargs))
        return {"Rules": list(self.rules)}

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        return {"Status": self.versioning} if self.versioning else {}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if kwargs["Key"] in self.head_raises:
            raise self.head_raises[kwargs["Key"]]
        if kwargs["Key"] not in self.objects:
            raise client_error("404", "HeadObject", 404)
        return dict(self.objects[kwargs["Key"]])

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.calls.append(("generate_presigned_url", {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}))
        return "https://{0}.s3.amazonaws.com/{1}?X-Amz-Signature=deadbeef0123&X-Amz-Security-Token=fixture-token-{2}".format(
            Params["Bucket"], Params["Key"], "t" * 300)


class StubGreengrass:
    def __init__(self):
        self.devices = {}
        self.status = {}

    def list_core_devices(self, **kwargs):
        return {"coreDevices": [{"coreDeviceThingName": n} for n in sorted(self.devices)]}

    def list_tags_for_resource(self, **kwargs):
        name = kwargs["resourceArn"].rsplit(":", 1)[-1]
        return {"tags": {"dda-portal:managed": "true"} if self.devices.get(name) else {}}

    def get_core_device(self, **kwargs):
        name = kwargs["coreDeviceThingName"]
        if name not in self.status:
            raise client_error("ResourceNotFoundException", "GetCoreDevice", 404)
        return {"status": self.status[name], "lastStatusUpdateTimestamp": "2026-10-18", "coreVersion": "2.12.0", "platform": "linux"}


class Api:
    def __init__(self, env, module, stubs):
        self.env = env
        self.module = module
        self.iotdata, self.s3, self.gg, self.purposes = stubs
        self.usecase_id = str(uuid.uuid4())
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "telemetry", "account_id": PORTAL_ACCOUNT, "region": REGION,
            "cross_account_role_arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(PORTAL_ACCOUNT)})
        self.workflow_id = str(uuid.uuid4())
        env.stack.tables.workflows.put_item(Item={"workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
                                                  "created_at": 1, "name": "wf", "current_version": "3"})
        self.devices = ["thing-a-" + self.usecase_id[-6:], "thing-b-" + self.usecase_id[-6:]]
        for device in self.devices:
            self.record(device)
            self.gg.devices[device] = True
            self.gg.status[device] = "HEALTHY"
        self.users = {}
        for role in ROLES:
            user = env.make_user(role)
            for scope in ("global", self.usecase_id):
                env.stack.tables.user_roles.put_item(Item={
                    "user_id": user["user_id"], "usecase_id": scope, "role": role,
                    "username": user["username"], "email": user["email"], "status": "enabled"})
            self.users[role] = user
        self.tables = telemetry_tables.tables()

    def record(self, device, usecase_id=None, arch="arm64_jp5"):
        self.env.stack.tables.devices.put_item(Item={"device_id": device, "usecase_id": usecase_id or self.usecase_id,
                                                     "target_architecture": arch})

    def event(self, method, resource, user, path_params=None, body=None, query=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {"httpMethod": method, "resource": resource, "path": path, "pathParameters": path_params or {},
                "queryStringParameters": query, "body": json.dumps(body) if body is not None else None,
                "requestContext": {"authorizer": {"claims": {"sub": user["user_id"], "email": user["email"],
                                                              "cognito:username": user["username"], "custom:role": user["role"]}}}}

    def call(self, method, resource, user, path_params=None, body=None, query=None):
        response = self.module.handler(self.event(method, resource, user, path_params, body, query), None)
        return response["statusCode"], json.loads(response["body"]) if response["body"] else {}

    def settings(self, user=None, **body):
        payload = {"enabled": True, "summaryIntervalS": 10, "idleHeartbeatS": 60}
        payload.update(body)
        return self.call("PUT", "/telemetry/usecases/{usecase_id}/settings", user or self.users["UseCaseAdmin"],
                         {"usecase_id": self.usecase_id}, payload)

    def create(self, user=None, **body):
        payload = {"usecase_id": self.usecase_id, "name": "Line A", "workflow_id": self.workflow_id,
                   "device_ids": list(self.devices), "template_id": "ppe-compliance"}
        payload.update(body)
        return self.call("POST", "/telemetry/dashboards", user or self.users["Operator"], body=payload)

    def dashboard_route(self, method, tail, dashboard_id, user=None, body=None, query=None, extra=None):
        params = {"dashboard_id": dashboard_id}
        params.update(extra or {})
        return self.call(method, "/telemetry/dashboards/{dashboard_id}" + tail, user or self.users["Viewer"], params, body, query)

    def audit_rows(self, action):
        """This use case's audit rows (the table is shared by the whole session)."""
        mine = {u["user_id"] for u in self.users.values()}
        rows = []
        for row in self.env.stack.tables.audit_log.scan()["Items"]:
            if row.get("action") != action:
                continue
            details = row.get("details") or {}
            if details.get("usecase_id") == self.usecase_id or (row.get("user_id") in mine and "usecase_id" not in details):
                rows.append(row)
        return rows

    def pk(self, device=None):
        return records.pair_pk(self.usecase_id, device or self.devices[0], self.workflow_id)

    def put_pointer(self, sk, device=None, **values):
        item = {"pk": self.pk(device), "sk": sk, "expires_at": T0 // 1000 + 10 ** 6}
        item.update(values)
        self.tables["summaries"].put_item(Item=item)

    def put_event(self, event_type, event_ms, device=None, data=None):
        event_id = str(uuid.uuid4())
        device = device or self.devices[0]
        self.tables["events"].put_item(Item={
            "pk": self.pk(device), "sk": records.event_sk(event_ms, event_id), "type": event_type, "eventId": event_id,
            "deviceId": device, "receivedMs": event_ms + 100, "eventMs": event_ms, "tsSource": "device",
            "gsi1pk": records.by_workflow_pk(self.usecase_id, self.workflow_id),
            "gsi1sk": records.by_workflow_sk(event_ms, device, event_id),
            "event": {"type": event_type, "ts": event_ms, "severity": "info", "data": data or {"error": "boom"}},
            "expires_at": T0 // 1000 + 10 ** 6})
        return event_id

    def put_clip(self, clip_id, start_ts, device=None, state="bound", s3_error=None, reasons=None):
        device = device or self.devices[0]
        key = "dda-telemetry/clips/{0}/{1}/{2}/2026-10-18/{3}.mp4".format(self.usecase_id, device, self.workflow_id, clip_id)
        media = {"bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "key": key, "sidecarKey": key[:-4] + ".json",
                 "state": state, "reason": "bucket_not_allowed" if state == "not_allowed" else None}
        if state == "upload_failed":
            media = {"bucket": None, "key": None, "sidecarKey": None, "state": "upload_failed", "reason": None}
        sk = records.clip_index_sk(start_ts, device, clip_id)
        self.tables["events"].put_item(Item={
            "pk": records.clip_index_pk(self.usecase_id, self.workflow_id), "sk": sk, "clipId": clip_id, "deviceId": device,
            "workflowId": self.workflow_id, "startTs": start_ts, "endTs": start_ts + 12_000, "durationS": 12,
            "reasons": reasons or {"no-helmet": {"frames": 3, "objects": 3}}, "totals": {}, "file": {"name": clip_id + ".mp4"},
            "media": media, "revision": 1, "s3Error": s3_error, "receivedMs": start_ts + 20_000,
            "expires_at": T0 // 1000 + 10 ** 6})
        return sk, key

    def put_live(self, instance, seq, received_ms, device=None):
        device = device or self.devices[0]
        key = schemas.live_key_for(self.usecase_id, device, self.workflow_id, seq)
        self.put_pointer("LIVE", device=device, instance=instance, seq=seq, frameToken=records.frame_token(instance, seq),
                         ts=received_ms - 500, receivedMs=received_ms, bucket="dda-inference-results-" + PORTAL_ACCOUNT, key=key,
                         width=1280, height=720, bytes=200000, perClass={"helmet": 2}, people=2)
        self.s3.objects[key] = {"ContentLength": 200000, "ContentType": "image/jpeg", "Metadata": {"seq": str(seq)}}
        return key


@pytest.fixture
def api(env, monkeypatch):
    telemetry_tables.set_env(monkeypatch)
    telemetry_tables.ensure_telemetry_tables()
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    for name in ("TELEMETRY_NOW_POLL_SECONDS", "TELEMETRY_LIVE_MAX_LEASES", "TELEMETRY_LIVE_MAX_VIEW_MINUTES",
                 "TELEMETRY_CLIP_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    for name in list(sys.modules):
        if name in ("telemetry_api", "telemetry") or name.startswith("telemetry.") or name.startswith("edge_telemetry_common"):
            sys.modules.pop(name, None)
    import telemetry_api as module
    from edge_telemetry_common import credentials
    iotdata, s3, gg = StubIotData(), StubS3(), StubGreengrass()
    purposes = []

    def fake_client(service, usecase, purpose, region=None, **kwargs):
        purposes.append((service, purpose, kwargs.get("bucket")))
        return {"iot-data": iotdata, "s3": s3, "greengrassv2": gg, "iot": None}[service]

    monkeypatch.setattr(credentials, "client", fake_client)
    module.reset_caches()
    return Api(env, module, (iotdata, s3, gg, purposes))


def lowercase_codes(status, body):
    if status == 403:
        assert body["error_code"] == "INSUFFICIENT_PERMISSIONS"
    elif status >= 400:
        assert body["error_code"] == body["error_code"].lower() and " " not in body["error_code"], body


# --------------------------------------------------------------------------- permissions
class TestPermissions:
    def test_schemas_for_any_authenticated_user(self, api):
        for role in ROLES:
            status, body = api.call("GET", "/telemetry/schemas", api.users[role])
            assert status == 200, role
        assert body["telemetryNowPollSeconds"] == 5
        assert body["cadence"]["series"] == {"minute": 60, "hour": 300, "day": 900}
        assert body["cadence"]["now"] == 5 and body["cadence"]["events"] == 15 and body["cadence"]["liveFrame"] == 2
        assert set(body["templates"]) == {"generic-detector", "anomaly", "ppe-compliance"}
        assert body["catalog"]["version"] == 1 and len(body["catalog"]["measures"]) == 35
        assert body["caps"]["devicesPerDashboard"] == 20 and body["caps"]["liveMaxLeases"] == 5
        assert body["registry"]["modelStates"] and body["personClasses"] == ["person", "human"]

    def test_the_matrix(self, api):
        status, created = api.create(api.users["Operator"])
        assert status == 201
        dashboard_id = created["dashboard"]["dashboard_id"]
        reads = [("GET", "/telemetry/dashboards", None, None, {"usecase_id": api.usecase_id}),
                 ("GET", "/telemetry/dashboards/{dashboard_id}", {"dashboard_id": dashboard_id}, None, None),
                 ("GET", "/telemetry/dashboards/{dashboard_id}/now", {"dashboard_id": dashboard_id}, None, None),
                 ("GET", "/telemetry/dashboards/{dashboard_id}/series", {"dashboard_id": dashboard_id}, None,
                  {"from": str(T0), "to": str(T0 + 60_000)}),
                 ("GET", "/telemetry/dashboards/{dashboard_id}/events", {"dashboard_id": dashboard_id}, None, None),
                 ("GET", "/telemetry/dashboards/{dashboard_id}/clips", {"dashboard_id": dashboard_id}, None, None)]
        def manages(role):
            return [("GET", "/telemetry/usecases/{usecase_id}/templates", {"usecase_id": api.usecase_id}, None, None),
                    ("POST", "/telemetry/dashboards", None, {"usecase_id": api.usecase_id, "name": "x-" + role, "workflow_id": api.workflow_id,
                                                             "device_ids": api.devices, "template_id": "anomaly"}, None),
                    ("PATCH", "/telemetry/dashboards/{dashboard_id}", {"dashboard_id": dashboard_id}, {"name": "renamed-" + role}, None)]
        telemetry = [("GET", "/telemetry/usecases/{usecase_id}/settings", {"usecase_id": api.usecase_id}, None, None),
                     ("PUT", "/telemetry/usecases/{usecase_id}/settings", {"usecase_id": api.usecase_id},
                      {"enabled": False}, None),
                     ("GET", "/telemetry/usecases/{usecase_id}/rejections", {"usecase_id": api.usecase_id}, None, None)]
        for role in ROLES:
            user = api.users[role]
            for method, resource, params, body, query in reads:
                status, answer = api.call(method, resource, user, params, body, query)
                expected = role != "DataLabeler"
                assert (status < 400) is expected, (role, resource, status, answer)
                lowercase_codes(status, answer)
            for method, resource, params, body, query in manages(role):
                status, answer = api.call(method, resource, user, params, body, query)
                expected = role in ("Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin")
                assert (status < 400) is expected, (role, method, resource, status, answer)
                lowercase_codes(status, answer)
            for method, resource, params, body, query in telemetry:
                status, answer = api.call(method, resource, user, params, body, query)
                expected = role in ("UseCaseAdmin", "PortalAdmin")
                assert (status < 400) is expected, (role, method, resource, status, answer)
                lowercase_codes(status, answer)
            status, answer = api.call("GET", "/telemetry/rejections", user, query={"account_id": PORTAL_ACCOUNT})
            assert (status == 200) is (role == "PortalAdmin"), (role, status, answer)
            if role != "PortalAdmin":
                assert answer["error_code"] == "INSUFFICIENT_PERMISSIONS" and answer["required_role"] == "PortalAdmin"
        denied = api.audit_rows("unauthorized_access")
        assert any(row["user_id"] == api.users["DataLabeler"]["user_id"] for row in denied)
        assert any(row["details"].get("required_permission") == "dashboards:read" for row in denied)
        assert any(row["details"].get("required_permission") == "telemetry:manage" for row in denied)
        viewer_denied = [r for r in denied if r["user_id"] == api.users["Viewer"]["user_id"]]
        assert viewer_denied and all(r["details"].get("required_permission") in ("dashboards:manage", "telemetry:manage")
                                     or r["details"].get("required_role") == "PortalAdmin" for r in viewer_denied)

    def test_registry_unavailable_is_500(self, api, monkeypatch):
        from shared_utils import RegistryUnavailable

        def boom(*args, **kwargs):
            raise RegistryUnavailable("down")

        monkeypatch.setattr(api.module.rbac_manager, "has_permission", boom)
        status, body = api.call("GET", "/telemetry/dashboards", api.users["Viewer"], query={"usecase_id": api.usecase_id})
        assert status == 500 and body["error_code"] == "registry_unavailable"

    def test_options_and_not_found(self, api):
        response = api.module.handler({"httpMethod": "OPTIONS", "resource": "/telemetry/schemas"}, None)
        assert response["statusCode"] == 200
        status, body = api.call("GET", "/telemetry/nothing", api.users["Viewer"])
        assert status == 404 and body["error_code"] == "not_found"


# --------------------------------------------------------------------------- settings
class TestSettings:
    def test_get_returns_defaults_and_the_checklist(self, api):
        status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/settings", api.users["UseCaseAdmin"],
                                {"usecase_id": api.usecase_id})
        assert status == 200
        assert body["settings"]["enabled"] is False and body["settings"]["summaryIntervalS"] == 10
        assert body["settings"]["media_location"]["bucket"] == "dda-inference-results-" + PORTAL_ACCOUNT
        ids = [item["id"] for item in body["checklist"]]
        assert ids == ["ingestion_resources", "region", "device_records", "telemetry_source", "media_location",
                       "media_retention", "registry_enforcement", "device_shadows"]
        assert not [i for i in body["checklist"] if i["id"] == "telemetry_source"][0]["ready"]

    def test_put_enables_writes_the_shadows_and_audits(self, api):
        status, body = api.settings()
        assert status == 200, body
        assert body["settings"]["enabled"] is True and body["settings"]["media_location"]["region"] == REGION
        assert body["settings"]["media_location"]["region_source"] == "header"
        assert sorted(r["device_id"] for r in body["devices"]) == sorted(api.devices)
        assert all(r["shadow"] == "ok" for r in body["devices"]) and body["unrecorded_devices"] == []
        payloads = api.iotdata.payloads()
        assert len(payloads) == 2 and all(p["state"]["desired"]["personClasses"] == ["person", "human"] for p in payloads)
        assert all(p["state"]["desired"]["usecaseId"] == api.usecase_id and p["state"]["desired"]["enabled"] for p in payloads)
        assert ("iot-data", "api-control", None) in api.purposes
        assert len(api.audit_rows("telemetry_enabled")) == 1
        item = api.tables["config"].get_item(Key={"pk": "UC#" + api.usecase_id, "sk": "SETTINGS"})["Item"]
        assert item["enabled"] is True and item["gsi2pk"] == "USECASE" and item["account_id"] == PORTAL_ACCOUNT
        shadows = [i for i in api.tables["config"].query(KeyConditionExpression=Key("pk").eq("UC#" + api.usecase_id))["Items"]
                   if i["sk"].startswith("DEVSHADOW#")]
        assert len(shadows) == 2 and all(s["result"] == "ok" for s in shadows)
        status, body = api.settings(summaryIntervalS=20)
        assert status == 200 and len(api.audit_rows("telemetry_settings_changed")) == 1
        status, body = api.settings(enabled=False)
        assert status == 200 and len(api.audit_rows("telemetry_disabled")) == 1
        assert [i for i in body["checklist"] if i["id"] == "device_shadows"][0]["ready"] is True

    def test_refusals(self, api):
        cases = [
            ({"personClasses": ["person"]}, "invalid_settings"),
            ({"enabled": "yes"}, "invalid_settings"),
            ({"summaryIntervalS": 4}, "invalid_settings"),
            ({"summaryIntervalS": 61}, "invalid_settings"),
            ({"idleHeartbeatS": 29}, "invalid_settings"),
            ({"idleHeartbeatS": "60"}, "invalid_settings"),
            ({"unknown": 1}, "invalid_settings"),
            ({"media_location": {"bucket": "other"}}, "bucket_not_allowed"),
            ({"bucket": "other"}, "bucket_not_allowed"),
        ]
        for over, code in cases:
            status, body = api.settings(**over)
            assert status == 400 and body["error_code"] == code, (over, body)
            lowercase_codes(status, body)
        bad_json = api.event("PUT", "/telemetry/usecases/{usecase_id}/settings", api.users["UseCaseAdmin"], {"usecase_id": api.usecase_id})
        bad_json["body"] = "{not json"
        response = api.module.handler(bad_json, None)
        assert response["statusCode"] == 400 and json.loads(response["body"])["error_code"] == "invalid_settings"

    def test_region_and_role_refusals(self, api):
        api.env.stack.tables.usecases.update_item(Key={"usecase_id": api.usecase_id}, UpdateExpression="SET #r = :r",
                                                  ExpressionAttributeNames={"#r": "region"}, ExpressionAttributeValues={":r": "eu-west-1"})
        status, body = api.settings()
        assert status == 400 and body["error_code"] == "region_not_supported" and "remedy" in body
        assert api.settings(enabled=False)[0] == 200, "disabling needs no region"
        api.env.stack.tables.usecases.update_item(
            Key={"usecase_id": api.usecase_id}, UpdateExpression="SET #r = :r, account_id = :a, cross_account_role_arn = :arn",
            ExpressionAttributeNames={"#r": "region"},
            ExpressionAttributeValues={":r": REGION, ":a": "222222222222", ":arn": "arn:aws:iam::222222222222:root"})
        status, body = api.settings()
        assert status == 400 and body["error_code"] == "usecase_role_invalid" and "DDAPortalAccessRole" in body["remedy"]

    def test_media_location_not_writable(self, api):
        api.s3.list_raises = client_error("NoSuchBucket", "ListObjectsV2", 404)
        status, body = api.settings()
        assert status == 400 and body["error_code"] == "media_location_not_writable" and body["reason"] == "NoSuchBucket"
        assert body["remedy"].startswith("Create the use case's inference-results bucket")
        api.s3.list_raises = client_error("AccessDenied", "ListObjectsV2", 403)
        status, body = api.settings()
        assert status == 400 and body["reason"] == "AccessDenied" and "dda-portal:managed=true" in body["remedy"]

    def test_the_202_path_with_41_devices_and_unrecorded_devices(self, api):
        extra = ["thing-{0:02d}-{1}".format(i, api.usecase_id[-4:]) for i in range(39)]
        for device in extra:
            api.record(device)
        api.gg.devices["unrecorded-" + api.usecase_id[-4:]] = True
        api.module.reset_caches()
        status, body = api.settings()
        assert status == 202, body
        assert body["pending_devices"] == 41 and body["devices"] == [] and body["settings"]["shadowSyncPending"] is True
        assert body["unrecorded_devices"] == ["unrecorded-" + api.usecase_id[-4:]]
        assert api.iotdata.updates == [], "no synchronous write above the limit"
        item = api.tables["config"].get_item(Key={"pk": "UC#" + api.usecase_id, "sk": "SETTINGS"})["Item"]
        assert item["shadowSyncPending"] is True
        assert len(api.audit_rows("telemetry_enabled")) == 1

    def test_a_failed_shadow_write_is_reported_per_device(self, api):
        api.iotdata.fail[api.devices[1]] = client_error("ResourceNotFoundException", "UpdateThingShadow", 404)
        status, body = api.settings()
        assert status == 200
        rows = {r["device_id"]: r for r in body["devices"]}
        assert rows[api.devices[0]]["shadow"] == "ok" and rows[api.devices[1]] == {"device_id": api.devices[1], "shadow": "failed",
                                                                                   "error": "thing_not_found"}
        shadows = [i for i in body["checklist"] if i["id"] == "device_shadows"][0]
        assert shadows["ready"] is False and shadows["failed"] == {api.devices[1]: "thing_not_found"}


# --------------------------------------------------------------------------- dashboards
class TestDashboards:
    def test_create_get_list_patch_delete(self, api):
        api.settings()
        api.iotdata.updates.clear()
        status, body = api.create()
        assert status == 201, body
        dashboard = body["dashboard"]
        assert dashboard["name"] == "Line A" and dashboard["template_id"] == "ppe-compliance" and dashboard["template_version"] == 1
        assert sorted(dashboard["device_ids"]) == sorted(api.devices) and dashboard["usecase_id"] == api.usecase_id
        assert sorted(r["device_id"] for r in body["devices"]) == sorted(api.devices)
        assert len(api.iotdata.updates) == 2, "the devices' shadows are written at once"
        assert len(api.audit_rows("dashboard_created")) == 1
        dashboard_id = dashboard["dashboard_id"]
        status, body = api.dashboard_route("GET", "", dashboard_id)
        assert status == 200 and body["dashboard"]["dashboard_id"] == dashboard_id
        status, body = api.call("GET", "/telemetry/dashboards", api.users["Viewer"], query={"usecase_id": api.usecase_id})
        assert status == 200 and [d["dashboard_id"] for d in body["dashboards"]] == [dashboard_id]
        status, body = api.dashboard_route("PATCH", "", dashboard_id, api.users["Operator"], {"name": "Line B"})
        assert status == 200 and body["dashboard"]["name"] == "Line B" and body["devices"] == []
        assert len(api.audit_rows("dashboard_updated")) == 1
        markers = [i["sk"] for i in api.tables["config"].query(KeyConditionExpression=Key("pk").eq("UC#" + api.usecase_id))["Items"]
                   if i["sk"].startswith("DASHNAME#")]
        assert markers == ["DASHNAME#line b"]
        status, body = api.dashboard_route("DELETE", "", dashboard_id, api.users["Operator"])
        assert status == 200 and body["deleted"] is True and len(api.audit_rows("dashboard_deleted")) == 1
        assert api.dashboard_route("GET", "", dashboard_id)[0] == 404
        count = api.tables["config"].get_item(Key={"pk": "UC#" + api.usecase_id, "sk": "DASHCOUNT"})["Item"]
        assert int(count["count"]) == 0
        assert api.call("GET", "/telemetry/dashboards", api.users["Viewer"], query={"usecase_id": api.usecase_id})[1]["dashboards"] == []

    def test_name_uniqueness_after_nfkc_case_folding(self, api):
        assert api.create(name="Line A")[0] == 201
        status, body = api.create(name="  line a ")
        assert status == 409 and body["error_code"] == "dashboard_name_taken"
        status, body = api.create(name="Ｌine A")
        assert status == 409, "full-width letters fold to the same name"
        assert api.create(name="Line B")[0] == 201
        status, created = api.create(name="Line C")
        status, body = api.dashboard_route("PATCH", "", created["dashboard"]["dashboard_id"], api.users["Operator"], {"name": "LINE B"})
        assert status == 409 and body["error_code"] == "dashboard_name_taken"
        status, body = api.dashboard_route("PATCH", "", created["dashboard"]["dashboard_id"], api.users["Operator"], {"name": "LINE C"})
        assert status == 200 and body["dashboard"]["name"] == "LINE C", "a case change of its own name is allowed"

    def test_device_not_recorded_with_the_remedy(self, api):
        status, body = api.create(device_ids=[api.devices[0], "stranger"])
        assert status == 400 and body["error_code"] == "device_not_recorded" and body["device_ids"] == ["stranger"]
        assert "Target architecture" in body["remedy"] and "node_designer:manage" in body["remedy"]
        other = str(uuid.uuid4())
        api.record("elsewhere", usecase_id=other)
        api.module.reset_caches()
        status, body = api.create(device_ids=["elsewhere"])
        assert status == 400 and body["error_code"] == "device_not_recorded"

    def test_validation_and_caps(self, api):
        cases = [
            ({"name": ""}, "invalid_dashboard"), ({"name": "x" * 101}, "invalid_dashboard"), ({"name": 7}, "invalid_dashboard"),
            ({"workflow_id": "not-a-uuid"}, "invalid_dashboard"), ({"workflow_id": str(uuid.uuid4())}, "invalid_dashboard"),
            ({"device_ids": []}, "invalid_dashboard"), ({"device_ids": "thing"}, "invalid_dashboard"),
            ({"device_ids": [api.devices[0], api.devices[0]]}, "invalid_dashboard"),
            ({"device_ids": ["bad name"]}, "invalid_dashboard"), ({"template_id": "nope"}, "invalid_dashboard"),
        ]
        for over, code in cases:
            status, body = api.create(**over)
            assert status == 400 and body["error_code"] == code, (over, body)
        twenty_one = ["thing-{0:02d}-{1}".format(i, api.usecase_id[-4:]) for i in range(21)]
        for device in twenty_one:
            api.record(device)
        api.module.reset_caches()
        status, body = api.create(device_ids=twenty_one)
        assert status == 400 and body["error_code"] == "invalid_dashboard" and body["max"] == 20
        assert api.create(device_ids=twenty_one[:20])[0] == 201
        api.tables["config"].put_item(Item={"pk": "UC#" + api.usecase_id, "sk": "DASHCOUNT", "count": 100})
        status, body = api.create(name="one too many")
        assert status == 409 and body["error_code"] == "dashboard_limit_reached" and body["max"] == 100
        status, body = api.call("POST", "/telemetry/dashboards", api.users["Operator"], body={"name": "x"})
        assert status == 400 and body["error_code"] == "invalid_dashboard"
        status, body = api.call("POST", "/telemetry/dashboards", api.users["PortalAdmin"],
                                body={"usecase_id": str(uuid.uuid4()), "name": "x", "workflow_id": api.workflow_id,
                                      "device_ids": api.devices, "template_id": "anomaly"})
        assert status == 404 and body["error_code"] == "usecase_not_found"

    def test_patch_writes_the_shadows_of_added_devices_only(self, api):
        api.settings()
        status, created = api.create(device_ids=[api.devices[0]])
        api.iotdata.updates.clear()
        dashboard_id = created["dashboard"]["dashboard_id"]
        status, body = api.dashboard_route("PATCH", "", dashboard_id, api.users["Operator"], {"device_ids": list(api.devices)})
        assert status == 200 and [r["device_id"] for r in body["devices"]] == [api.devices[1]]
        assert [u["thingName"] for u in api.iotdata.updates] == [api.devices[1]]
        status, body = api.dashboard_route("PATCH", "", dashboard_id, api.users["Operator"], {"device_ids": ["stranger"]})
        assert status == 400 and body["error_code"] == "device_not_recorded"
        status, body = api.dashboard_route("PATCH", "", dashboard_id, api.users["Operator"], {"template_id": "anomaly"})
        assert status == 400 and body["error_code"] == "invalid_dashboard"
        status, body = api.dashboard_route("PATCH", "", dashboard_id, api.users["Operator"], {})
        assert status == 400


# --------------------------------------------------------------------------- templates
class TestTemplates:
    def test_availability_from_the_pointers(self, api):
        query = {"workflow_id": api.workflow_id, "device_ids": ",".join(api.devices)}
        status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/templates", api.users["Operator"],
                                {"usecase_id": api.usecase_id}, query=query)
        assert status == 200
        assert {t["id"]: t["availability"] for t in body["templates"]} == {"generic-detector": "unavailable", "anomaly": "unavailable",
                                                                           "ppe-compliance": "unavailable"}
        engine = {"runs": {"completed": 3, "failed": 0, "dropped": 0}, "latency": {"p50Ms": 1, "p95Ms": 2, "maxMs": 3, "n": 3},
                  "detections": {"runsWithDetections": 1, "total": 2, "perClass": {"helmet": {"total": 2, "max": 2}}},
                  "people": {"seen": 1}, "model": {"state": "READY", "models": {}, "waits": 0, "waitedMs": 0}}
        api.put_pointer("LAST#engine", receivedMs=T0, eventMs=T0, seq=1, instance=INSTANCE, summary=engine, lastRunMs=T0)
        api.module.time.time = lambda: T0 / 1000.0 + 60
        try:
            status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/templates", api.users["Operator"],
                                    {"usecase_id": api.usecase_id}, query=query)
            states = {t["id"]: t for t in body["templates"]}
            assert states["generic-detector"]["availability"] == "available"
            assert states["anomaly"]["availability"] == "partial" and "anomalous-share" in states["anomaly"]["unreported_panels"]
            assert states["ppe-compliance"]["availability"] == "partial"
            assert "violations-by-reason" in states["ppe-compliance"]["unreported_panels"]
            ppe = {"ext": {"ppe": {"people": {"seen": 2, "compliant": 1, "violating": 1},
                                   "violations": {"frames": 1, "byReason": {"no-helmet": {"frames": 1, "objects": 1}}},
                                   "clipsOpened": 1, "frames": 10}}}
            api.put_pointer("LAST#ppe-clipper", device=api.devices[1], receivedMs=T0, eventMs=T0, seq=1, instance=OTHER_INSTANCE, summary=ppe)
            status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/templates", api.users["Operator"],
                                    {"usecase_id": api.usecase_id}, query=query)
            states = {t["id"]: t for t in body["templates"]}
            assert states["ppe-compliance"]["availability"] == "available" and states["ppe-compliance"]["producers"] == ["engine", "ppe-clipper"]
            # A stale pointer (older than 24 h) counts as unavailable.
            api.module.time.time = lambda: T0 / 1000.0 + 25 * 3600
            status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/templates", api.users["Operator"],
                                    {"usecase_id": api.usecase_id}, query=query)
            assert all(t["availability"] == "unavailable" for t in body["templates"])
        finally:
            import time as real_time
            api.module.time.time = real_time.time


# --------------------------------------------------------------------------- now
class TestNow:
    def test_values_from_pointers_flow_and_hint(self, api):
        api.record(api.devices[0], arch="x86_64_nvidia")
        api.settings()
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        summary = {"runs": {"completed": 3, "failed": 1, "dropped": 0}, "model": {"state": "READY", "models": {}, "waits": 0, "waitedMs": 0},
                   "telemetry": {"queueDrops": 0, "publishFailures": 0, "eventsDropped": 0}}
        api.put_pointer("LAST#engine", receivedMs=T0, eventMs=T0, seq=8, instance=INSTANCE, summary=summary, lastRunMs=T0,
                        workflowVersion="3")
        for seq in (9, 10, 11):
            api.put_pointer("LASTHB#engine", receivedMs=T0 + 60_000 * (seq - 8), eventMs=T0 + 60_000 * (seq - 8), seq=seq, instance=INSTANCE)
        api.put_event("run_error", T0 + 170_000)
        api.put_event("clip_ready", T0 - 3600_000, data={"clipId": "old"})      # outside the 15-minute window
        minute = fold.bucket_minute("event", T0 + 120_000)
        api.tables["aggregates"].put_item(Item={"pk": api.pk(), "sk": "M#" + fold.minute_key(minute),
                                                "measures": {"telemetry_publish_failures": 4, "runs_failed": 1, "gaps": 2, "summaries": 6,
                                                             "model_state_worst": "LOADING"},
                                                "events": {}, "summariesFolded": 6, "gaps": 2, "weights": {}, "expires_at": T0 // 1000 + 10 ** 6})
        api.module.time.time = lambda: (T0 + 180_000) / 1000.0
        try:
            status, body = api.dashboard_route("GET", "/now", dashboard_id)
        finally:
            import time as real_time
            api.module.time.time = real_time.time
        assert status == 200, body
        device = body["devices"][api.devices[0]]
        assert device["flow"] == "idle"
        assert device["last"]["LAST#engine"]["summary"]["runs"]["completed"] == 3, "Heartbeats keep the values"
        assert device["last"]["LAST#engine"]["lastRunMs"] == T0 and device["ages"]["lastRunAgeS"] == 180
        assert device["ages"]["lastSummaryAgeS"] == 180 and device["ages"]["heartbeatAgeS"] == 0
        assert [e["type"] for e in device["events"]] == ["run_error"]
        assert device["lastHour"]["measures"]["telemetry_publish_failures"] == 4 and device["lastHour"]["gaps"] == 2
        assert device["lastHour"]["measures"]["model_state_worst"] == "LOADING"
        assert device["coreDevice"]["status"] == "HEALTHY" and device["targetArchitecture"] == "x86_64_nvidia"
        assert device["variantHint"] is None, "idle, not silent"
        other = body["devices"][api.devices[1]]
        assert other["flow"] == "silent" and other["last"] == {} and other["ages"]["noRunIn48h"] is True
        assert "rejections" not in body, "a Viewer sees no rejection counts"
        assert body["enabled"] is True and body["pollSeconds"] == 5
        api.module.time.time = lambda: (T0 + 3600_000) / 1000.0
        try:
            status, body = api.dashboard_route("GET", "/now", dashboard_id, api.users["UseCaseAdmin"])
        finally:
            api.module.time.time = real_time.time
        device = body["devices"][api.devices[0]]
        assert device["flow"] == "silent" and device["variantHint"] and "amd64Nvidia" in device["variantHint"]
        assert "rejections" in body and body["rejections"]["counts"] == {}
        assert ("greengrassv2", "api-control", None) in api.purposes


# --------------------------------------------------------------------------- series
class TestSeries:
    def test_validation_and_the_three_resolutions(self, api, monkeypatch):
        status, created = api.create(template_id="generic-detector")
        dashboard_id = created["dashboard"]["dashboard_id"]
        minute = fold.bucket_minute("event", T0)
        for device in api.devices:
            pk = records.pair_pk(api.usecase_id, device, api.workflow_id)
            api.tables["aggregates"].put_item(Item={"pk": pk, "sk": "M#" + fold.minute_key(minute), "measures": {"runs_completed": 3, "summaries": 6, "gaps": 0},
                                                    "events": {}, "summariesFolded": 6, "gaps": 0, "expires_at": T0 // 1000 + 10 ** 6})
            api.tables["aggregates"].put_item(Item={"pk": pk, "sk": "H#" + fold.hour_key(minute), "measures": {"runs_completed": 30},
                                                    "events": {"clip_ready": 2}, "summariesFolded": 60, "gaps": 1, "expires_at": T0 // 1000 + 10 ** 6})
            api.tables["aggregates"].put_item(Item={"pk": pk, "sk": "D#" + fold.day_key(minute), "measures": {"runs_completed": 300},
                                                    "events": {}, "summariesFolded": 600, "gaps": 1, "expires_at": T0 // 1000 + 10 ** 6})
        queries = []
        original = api.module.series.query_device

        def counting(*args, **kwargs):
            queries.append(args[2])
            return original(*args, **kwargs)

        monkeypatch.setattr(api.module.series, "query_device", counting)
        status, body = api.dashboard_route("GET", "/series", dashboard_id, query={"from": str(minute), "to": str(minute + 15 * 60_000)})
        assert status == 200 and body["resolution"] == "minute" and body["refreshSeconds"] == 60
        assert set(body["devices"]) == set(api.devices) and queries == ["minute", "minute"], "one Query per Device"
        points = body["devices"][api.devices[0]]["points"]
        assert points == [{"t": minute, "measures": {"runs_completed": 3, "summaries": 6, "gaps": 0}, "events": {}, "gaps": 0, "summaries": 6}]
        status, body = api.dashboard_route("GET", "/series", dashboard_id, query={"from": str(minute), "to": str(minute + 2 * fold.DAY_MS)})
        assert body["resolution"] == "hour" and body["devices"][api.devices[0]]["points"][0]["measures"] == {"runs_completed": 30}
        assert body["devices"][api.devices[0]]["points"][0]["events"] == {"clip_ready": 2}
        status, body = api.dashboard_route("GET", "/series", dashboard_id, query={"from": str(minute), "to": str(minute + 100 * fold.DAY_MS)})
        assert body["resolution"] == "day" and body["refreshSeconds"] == 900 and body["devices"][api.devices[0]]["points"][0]["measures"] == {"runs_completed": 300}
        status, body = api.dashboard_route("GET", "/series", dashboard_id, query={"from": str(minute), "to": str(minute + 60_000), "device_id": api.devices[1],
                                                                                   "measures": "runs_completed"})
        assert status == 200 and list(body["devices"]) == [api.devices[1]]
        for query, code in [({"to": str(minute)}, "invalid_range"), ({"from": str(minute), "to": str(minute)}, "invalid_range"),
                            ({"from": str(minute), "to": str(minute + 401 * fold.DAY_MS)}, "range_too_long"),
                            ({"from": str(minute), "to": str(minute + 60_000), "device_id": "stranger"}, "invalid_device"),
                            ({"from": str(minute), "to": str(minute + 60_000), "measures": "ppe_frames"}, "invalid_measure")]:
            status, body = api.dashboard_route("GET", "/series", dashboard_id, query=query)
            assert status == 400 and body["error_code"] == code, (query, body)
            lowercase_codes(status, body)


# --------------------------------------------------------------------------- events
class TestEvents:
    def test_paging_and_refusals(self, api):
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        now_ms = int(api.module.time.time() * 1000)
        for index in range(60):
            api.put_event("run_error" if index % 2 else "clip_ready", now_ms - 1000 * (index + 1),
                          device=api.devices[index % 2])
        api.put_event("run_error", now_ms - 500, device="stranger")
        status, body = api.dashboard_route("GET", "/events", dashboard_id)
        assert status == 200 and len(body["events"]) == 50 and body["cursor"]
        assert body["events"][0]["eventMs"] == now_ms - 1000 and body["events"][0]["type"] == "clip_ready"
        assert all(e["deviceId"] in api.devices for e in body["events"])
        status, page2 = api.dashboard_route("GET", "/events", dashboard_id, query={"cursor": body["cursor"]})
        assert status == 200 and len(page2["events"]) == 10 and page2["cursor"] is None
        assert {e["eventId"] for e in page2["events"]}.isdisjoint({e["eventId"] for e in body["events"]})
        status, filtered = api.dashboard_route("GET", "/events", dashboard_id, query={"type": "clip_ready", "device_id": api.devices[0]})
        assert status == 200 and len(filtered["events"]) == 30 and all(e["type"] == "clip_ready" for e in filtered["events"])
        status, windowed = api.dashboard_route("GET", "/events", dashboard_id, query={"from": str(now_ms - 10_500), "to": str(now_ms)})
        assert len(windowed["events"]) == 10
        for query, code in [({"from": str(now_ms), "to": str(now_ms - 1)}, "invalid_range"), ({"from": "x"}, "invalid_range"),
                            ({"from": str(now_ms - 31 * fold.DAY_MS), "to": str(now_ms)}, "invalid_range"),
                            ({"cursor": "@@@"}, "invalid_cursor"), ({"device_id": "stranger"}, "invalid_device"),
                            ({"type": "explosion"}, "invalid_type")]:
            status, answer = api.dashboard_route("GET", "/events", dashboard_id, query=query)
            assert status == 400 and answer["error_code"] == code, (query, answer)
        foreign = base64.urlsafe_b64encode(json.dumps({"gsi1pk": "W#other#wf", "gsi1sk": "x", "pk": "p", "sk": "s"}).encode()).decode().rstrip("=")
        status, answer = api.dashboard_route("GET", "/events", dashboard_id, query={"cursor": foreign})
        assert status == 400 and answer["error_code"] == "invalid_cursor"


# --------------------------------------------------------------------------- clips
class TestClips:
    def test_listing_states(self, api):
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        now_ms = int(api.module.time.time() * 1000)
        _, key_ok = api.put_clip("clip-ok", now_ms - 60_000)
        api.s3.objects[key_ok] = {"ContentLength": 10, "ContentType": "video/mp4", "Metadata": {}}
        api.put_clip("clip-missing", now_ms - 50_000)
        _, key_forbidden = api.put_clip("clip-forbidden", now_ms - 40_000)
        api.s3.head_raises[key_forbidden] = client_error("403", "HeadObject", 403)
        api.s3.objects[key_forbidden + ".present"] = {}
        api.put_clip("clip-failed", now_ms - 30_000, state="upload_failed", s3_error="upload failed: AccessDenied")
        api.put_clip("clip-foreign", now_ms - 20_000, state="not_allowed")
        api.put_clip("clip-stranger", now_ms - 10_000, device="stranger")
        status, body = api.dashboard_route("GET", "/clips", dashboard_id)
        assert status == 200
        states = {c["clipId"]: c for c in body["clips"]}
        assert set(states) == {"clip-ok", "clip-missing", "clip-forbidden", "clip-failed", "clip-foreign"}
        assert states["clip-ok"]["state"] == "playable" and states["clip-ok"]["hasSidecar"] is True
        assert states["clip-missing"]["state"] == "expired_or_missing"
        assert states["clip-forbidden"]["state"] == "unavailable" and "dda-portal:managed=true" in states["clip-forbidden"]["note"]
        assert states["clip-failed"]["state"] == "upload_failed" and states["clip-failed"]["s3Error"] == "upload failed: AccessDenied"
        assert states["clip-foreign"]["state"] == "not_allowed" and states["clip-foreign"]["reason"] == "bucket_not_allowed"
        assert [c["clipId"] for c in body["clips"]][0] == "clip-foreign", "newest first"
        heads = [c for c in api.s3.calls if c[0] == "head_object"]
        assert len(heads) == 3, "only bound entries are checked"
        status, body = api.dashboard_route("GET", "/clips", dashboard_id, query={"device_id": api.devices[1]})
        assert status == 200 and body["clips"] == []
        status, body = api.dashboard_route("GET", "/clips", dashboard_id, query={"reason": "no-vest"})
        assert body["clips"] == []
        status, body = api.dashboard_route("GET", "/clips", dashboard_id, query={"reason": "no-helmet"})
        assert len(body["clips"]) == 5
        status, body = api.dashboard_route("GET", "/clips", dashboard_id, query={"device_id": "stranger"})
        assert status == 400 and body["error_code"] == "invalid_device"
        status, body = api.dashboard_route("GET", "/clips", dashboard_id, query={"cursor": "!!"})
        assert status == 400 and body["error_code"] == "invalid_cursor"

    def test_media_route_binds_first_then_guards_then_presigns_with_one_audit_per_hour(self, api, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        now_ms = int(api.module.time.time() * 1000)
        sk, key = api.put_clip("clip-1", now_ms - 60_000)
        api.s3.objects[key] = {"ContentLength": 10, "ContentType": "video/mp4", "Metadata": {}}
        clip_ref = base64.urlsafe_b64encode(sk.encode()).decode().rstrip("=")
        order = []
        original_bind = api.module.media.bind
        monkeypatch.setattr(api.module.media, "bind", lambda *a, **k: order.append("bind") or original_bind(*a, **k))
        from edge_telemetry_common import credentials
        original_client = credentials.client
        monkeypatch.setattr(credentials, "client", lambda *a, **k: order.append("credentials") or original_client(*a, **k))
        status, body = api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, extra={"clip_ref": clip_ref})
        assert status == 200, body
        assert order == ["bind", "credentials"], "binding before any credential"
        assert body["video_url"].startswith("https://") and body["download_url"] and body["sidecar_url"] and body["expires_in"] == 900
        presigns = [c for c in api.s3.calls if c[0] == "generate_presigned_url"]
        assert len(presigns) == 3 and presigns[0][1]["ExpiresIn"] == 900
        assert presigns[0][1]["Params"]["ResponseContentType"] == "video/mp4"
        assert presigns[1][1]["Params"]["ResponseContentDisposition"] == 'attachment; filename="clip-1.mp4"'
        assert ("s3", "media-read", "dda-inference-results-" + PORTAL_ACCOUNT) in api.purposes
        assert len(api.audit_rows("clip_media_minted")) == 1
        api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, extra={"clip_ref": clip_ref})
        assert len(api.audit_rows("clip_media_minted")) == 1, "once per (user, clip) per hour"
        api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, api.users["Operator"], extra={"clip_ref": clip_ref})
        assert len(api.audit_rows("clip_media_minted")) == 2
        text = caplog.text
        assert "X-Amz-Signature" not in text and "X-Amz-Security-Token" not in text
        for row in api.env.stack.tables.audit_log.scan()["Items"]:
            assert "X-Amz-Signature" not in json.dumps(row, default=str)
        # The guard refuses playback while enforcement is off.
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "false")
        status, body = api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, extra={"clip_ref": clip_ref})
        assert status == 409 and body["error_code"] == "registry_enforcement_required" and "PORTAL_REGISTRY_ENFORCED" in body["remedy"]
        status, body = api.dashboard_route("GET", "/clips", dashboard_id)
        assert status == 200 and len(body["clips"]) == 1, "listing stays available"
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
        status, body = api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, extra={"clip_ref": "not-a-ref!!"})
        assert status == 404 and body["error_code"] == "clip_not_found"
        failed_sk, _ = api.put_clip("clip-2", now_ms - 50_000, state="upload_failed")
        ref2 = base64.urlsafe_b64encode(failed_sk.encode()).decode().rstrip("=")
        status, body = api.dashboard_route("GET", "/clips/{clip_ref}/media", dashboard_id, extra={"clip_ref": ref2})
        assert status == 409 and body["error_code"] == "clip_media_unavailable"


# --------------------------------------------------------------------------- live lease and frames
class TestLive:
    def lease(self, api, dashboard_id, user=None, device=None):
        return api.dashboard_route("POST", "/live-lease", dashboard_id, user, {"device_id": device or api.devices[0]})

    def test_lease_refusals_and_the_slot_transaction(self, api, monkeypatch):
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        status, body = self.lease(api, dashboard_id)
        assert status == 409 and body["error_code"] == "telemetry_disabled"
        api.settings()
        api.s3.versioning = "Enabled"
        api.module.reset_caches()
        status, body = self.lease(api, dashboard_id)
        assert status == 409 and body["error_code"] == "live_unavailable" and body["reason"] == "media_versioned"
        api.s3.versioning = None
        api.s3.rules = []
        api.module.reset_caches()
        status, body = self.lease(api, dashboard_id)
        assert status == 409 and body["error_code"] == "live_unavailable" and body["reason"] == "media_retention_unset"
        api.s3.rules = retention.telemetry_rules()
        api.module.reset_caches()
        status, body = self.lease(api, dashboard_id, device="stranger")
        assert status == 400 and body["error_code"] == "invalid_device"
        api.iotdata.updates.clear()
        status, body = self.lease(api, dashboard_id)
        assert status == 200, body
        assert body["slot"] == 0 and body["renewSeconds"] == 20 and body["region"] == REGION
        assert body["leaseExpiresAt"] - body["startedAt"] == 60_000 and body["expiresAt"] == body["leaseExpiresAt"]
        payload = api.iotdata.payloads()[-1]
        entry = payload["state"]["desired"]["live"][api.workflow_id]
        assert entry["bucket"] == "dda-inference-results-" + PORTAL_ACCOUNT and entry["region"] == REGION
        assert entry["prefix"] == "dda-telemetry/live/{0}/{1}/{2}/".format(api.usecase_id, api.devices[0], api.workflow_id)
        assert entry["maxFps"] == 1 and entry["maxBytes"] == 307200 and entry["expiresAt"] == body["expiresAt"]
        assert len(api.audit_rows("live_lease_granted")) == 1
        status, renewed = self.lease(api, dashboard_id)
        assert status == 200 and renewed["slot"] == 0 and renewed["startedAt"] == body["startedAt"]
        assert len(api.audit_rows("live_lease_granted")) == 1, "renewals are not audited"
        for role in ("Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin"):
            status, answer = self.lease(api, dashboard_id, api.users[role])
            assert status == 200, (role, answer)
        status, body = self.lease(api, dashboard_id, api.users["DataLabeler"])
        assert status == 403
        extra_user = api.env.make_user("Viewer")
        api.env.stack.tables.user_roles.put_item(Item={"user_id": extra_user["user_id"], "usecase_id": "global", "role": "Viewer", "status": "enabled"})
        status, body = self.lease(api, dashboard_id, extra_user)
        assert status == 429 and body["error_code"] == "live_capacity_reached" and body["reason"] == "usecase_slots" and body["max"] == 5
        slots = [i for i in api.tables["config"].query(KeyConditionExpression=Key("pk").eq("LEASE#" + api.usecase_id))["Items"]
                 if i["sk"].startswith("SLOT#")]
        assert len(slots) == 5 and all(s["device_id"] == api.devices[0] for s in slots)
        # A failed shadow write releases the slot.
        api.iotdata.fail[api.devices[1]] = client_error("ResourceNotFoundException", "UpdateThingShadow", 404)
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "6")
        status, body = self.lease(api, dashboard_id, extra_user, device=api.devices[1])
        assert status == 502 and body["error_code"] == "live_lease_failed" and body["remedy"] == api.module.LEASE_FAILED_REMEDY
        holders = [i for i in api.tables["config"].query(KeyConditionExpression=Key("pk").eq("LEASE#" + api.usecase_id))["Items"]
                   if i["sk"].startswith("HOLDER#") and extra_user["user_id"] in i["sk"]]
        assert holders == []
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "false")
        status, body = self.lease(api, dashboard_id)
        assert status == 409 and body["error_code"] == "registry_enforcement_required"

    def test_device_live_limit(self, api, monkeypatch):
        monkeypatch.setenv("TELEMETRY_LIVE_MAX_LEASES", "20")
        api.settings()
        for index in range(8):
            workflow_id = str(uuid.uuid4())
            api.env.stack.tables.workflows.put_item(Item={"workflow_id": workflow_id, "usecase_id": api.usecase_id, "created_at": 1})
            status, created = api.create(name="dash-{0}".format(index), workflow_id=workflow_id, device_ids=[api.devices[0]])
            assert self.lease(api, created["dashboard"]["dashboard_id"])[0] == 200
        status, created = api.create(name="ninth", device_ids=[api.devices[0]])
        status, body = self.lease(api, created["dashboard"]["dashboard_id"])
        assert status == 429 and body["reason"] == "device_live_limit" and body["max"] == 8

    def test_live_frames_across_two_agent_instances(self, api, caplog):
        caplog.set_level(logging.INFO)
        api.settings()
        status, created = api.create()
        dashboard_id = created["dashboard"]["dashboard_id"]
        now_ms = int(api.module.time.time() * 1000)
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0]})
        assert status == 200 and body == {"frame": None, "frame_token": None, "state": "no_live_source"}
        api.put_live(INSTANCE, 4000, now_ms - 2000)
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0]})
        assert status == 200 and body["frame_token"] == INSTANCE + ":4000"
        frame = body["frame"]
        assert frame["url"].startswith("https://") and frame["width"] == 1280 and frame["perClass"] == {"helmet": 2} and frame["expires_in"] == 120
        presign = [c for c in api.s3.calls if c[0] == "generate_presigned_url"][-1][1]
        assert presign["ExpiresIn"] == 120 and presign["Params"]["ResponseContentType"] == "image/jpeg"
        assert presign["Params"]["ResponseCacheControl"] == "no-store"
        token = body["frame_token"]
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0], "have": token})
        assert body == {"frame": None, "frame_token": token, "age_s": body["age_s"]}, "the same frame: null with the token"
        # The agent restarts: a new instance, seq 0, a later arrival.
        api.put_live(OTHER_INSTANCE, 0, now_ms - 1000)
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0], "have": token})
        assert body["frame"] is not None and body["frame_token"] == OTHER_INSTANCE + ":0"
        assert body["frame"]["url"].endswith("0.jpg?X-Amz-Signature=deadbeef0123&X-Amz-Security-Token=fixture-token-" + "t" * 300)
        new_token = body["frame_token"]
        # The HEAD seq mismatch (the slot was rewritten): frame null with the previous token echoed.
        key = schemas.live_key_for(api.usecase_id, api.devices[0], api.workflow_id, 0)
        api.s3.objects[key]["Metadata"] = {"seq": "16"}
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0], "have": token})
        assert body == {"frame": None, "frame_token": token, "age_s": body["age_s"]}
        api.s3.objects[key]["Metadata"] = {"seq": "0"}
        api.s3.objects[key]["ContentLength"] = 400_000
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0]})
        assert body["frame"] is None and body["unavailable"] == "frame_too_large" and body["frame_token"] == new_token
        api.s3.objects[key]["ContentLength"] = 1000
        api.s3.objects[key]["ContentType"] = "text/plain"
        assert api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0]})[1]["unavailable"] == "frame_too_large"
        del api.s3.objects[key]
        status, body = api.dashboard_route("GET", "/live", dashboard_id, query={"device_id": api.devices[0], "have": token})
        assert body["frame"] is None and body["frame_token"] == token, "a missing object echoes the previous token"
        for query, code in [({"device_id": "stranger"}, "invalid_device"), ({"device_id": api.devices[0], "have": "junk"}, "invalid_token"),
                            ({"device_id": api.devices[0], "have": INSTANCE + ":" + "9" * 50}, "invalid_token")]:
            status, body = api.dashboard_route("GET", "/live", dashboard_id, query=query)
            assert status == 400 and body["error_code"] == code, (query, body)
        text = caplog.text
        assert "X-Amz-Signature" not in text and "X-Amz-Security-Token" not in text and "fixture-token" not in text
        for line in text.splitlines():
            for token_ in line.split():
                assert len(token_) <= 256, token_[:80]
        live_rows = api.tables["summaries"].query(KeyConditionExpression=Key("pk").eq(api.pk()))["Items"]
        assert all("url" not in json.dumps(r, default=str).lower() or "X-Amz" not in json.dumps(r, default=str) for r in live_rows), "no URL stored"


# --------------------------------------------------------------------------- rejections
class TestRejections:
    def test_usecase_and_account_rejections(self, api):
        from telemetry import configdb
        now_ms = int(api.module.time.time() * 1000)
        api.tables["config"].update_item(**configdb.rejection_update("UC#" + api.usecase_id, now_ms - 60_000, {"schema_invalid": 2}))
        api.tables["config"].put_item(Item=configdb.sample_item("UC#" + api.usecase_id, now_ms - 60_000, "schema_invalid",
                                                                {"reason": "schema_invalid", "detail": "intervalS"}))
        api.tables["config"].update_item(**configdb.rejection_update("UC#" + api.usecase_id, now_ms - 2 * 3600_000, {"schema_invalid": 9}))
        api.tables["config"].update_item(**configdb.rejection_update("X#" + PORTAL_ACCOUNT, now_ms - 30_000, {"unattributed": 1}))
        status, body = api.call("GET", "/telemetry/usecases/{usecase_id}/rejections", api.users["UseCaseAdmin"], {"usecase_id": api.usecase_id})
        assert status == 200 and body["counts"] == {"schema_invalid": 2} and body["scope"] == "usecase"
        assert body["samples"][0]["reason"] == "schema_invalid" and body["samples"][0]["sample"]["detail"] == "intervalS"
        assert "per ingest container" in body["note"]
        status, body = api.call("GET", "/telemetry/rejections", api.users["PortalAdmin"], query={"account_id": PORTAL_ACCOUNT})
        assert status == 200 and body["counts"] == {"unattributed": 1} and body["scope"] == "account"
        status, body = api.call("GET", "/telemetry/rejections", api.users["PortalAdmin"], query={"account_id": "nope"})
        assert status == 400 and body["error_code"] == "invalid_query"
