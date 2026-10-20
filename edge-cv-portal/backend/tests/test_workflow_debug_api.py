"""``workflow_debug_api`` (workflow-debug-mode design §7.2, §13; tasks.md 3.6)
on the conftest moto stack plus the sessions table, with stubbed ``logs``,
``iot-data`` and ``s3`` clients.

The permission matrix per route; every 400/409 of the creation path with its
remedy and the all-or-nothing behaviour; the 201 shape and the shadow
document; DELETE; the log route's validation, filters, termination rule, 404,
400, 429 and the empty-stream answer; the image route's guard and presign; the
console link; no secrets in the captured handler logs.
"""
import base64
import json
import logging
import sys
import uuid
from io import BytesIO

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
PORTAL_ACCOUNT = "111111111111"
TABLE = "test-workflow-debug-sessions"
WF_VERSION = "3"


def client_error(code, operation, status=400, headers=None):
    response = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    if headers:
        response["ResponseMetadata"]["HTTPHeaders"] = headers
    return ClientError(response, operation)


class StubLogs:
    def __init__(self):
        self.calls = []
        self.get_response = {"events": [], "nextForwardToken": "f/0"}
        self.filter_response = {"events": []}
        self.raise_on_read = None
        self.raise_on_create_group = None

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    def create_log_group(self, **kwargs):
        self._record("create_log_group", kwargs)
        if self.raise_on_create_group:
            raise self.raise_on_create_group
        return {}

    def put_retention_policy(self, **kwargs):
        self._record("put_retention_policy", kwargs)
        return {}

    def tag_resource(self, **kwargs):
        self._record("tag_resource", kwargs)
        return {}

    def get_log_events(self, **kwargs):
        self._record("get_log_events", kwargs)
        if self.raise_on_read:
            raise self.raise_on_read
        return self.get_response

    def filter_log_events(self, **kwargs):
        self._record("filter_log_events", kwargs)
        if self.raise_on_read:
            raise self.raise_on_read
        return self.filter_response

    def names(self):
        return [n for n, _ in self.calls]

    def reads(self):
        return [(n, k) for n, k in self.calls if n in ("get_log_events", "filter_log_events")]


class StubIotData:
    def __init__(self):
        self.updates = []
        self.fail = {}
        self.reported = {}

    def update_thing_shadow(self, **kwargs):
        self.updates.append(kwargs)
        failure = self.fail.get(kwargs["thingName"])
        if failure:
            raise failure
        return {}

    def get_thing_shadow(self, **kwargs):
        document = self.reported.get(kwargs["thingName"])
        if document is None:
            raise client_error("ResourceNotFoundException", "GetThingShadow", 404)
        return {"payload": BytesIO(json.dumps(document).encode("utf-8"))}


class StubS3:
    def __init__(self):
        self.calls = []
        self.region_header = "eu-west-1"
        self.list_raises = None
        self.rules = None
        self.lifecycle_raises = None
        self.objects = set()

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
        return {"Rules": list(self.rules or [])}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if kwargs["Key"] not in self.objects:
            raise client_error("404", "HeadObject", 404)
        return {"ContentLength": 10}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.calls.append(("generate_presigned_url", {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}))
        return ("https://{0}.s3.amazonaws.com/{1}?X-Amz-Signature=deadbeef0123&X-Amz-Security-Token=fixture-token-"
                "{2}".format(Params["Bucket"], Params["Key"], "t" * 300))


def ensure_sessions_table():
    client = boto3.client("dynamodb", region_name=REGION)
    if TABLE in client.list_tables()["TableNames"]:
        return
    client.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi_usecase", "AttributeType": "S"}, {"AttributeName": "gsi_pair", "AttributeType": "S"},
            {"AttributeName": "gsi_sort", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {"IndexName": "by-usecase",
             "KeySchema": [{"AttributeName": "gsi_usecase", "KeyType": "HASH"},
                           {"AttributeName": "gsi_sort", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "by-pair",
             "KeySchema": [{"AttributeName": "gsi_pair", "KeyType": "HASH"},
                           {"AttributeName": "gsi_sort", "KeyType": "RANGE"}],
             "Projection": {"ProjectionType": "ALL"}},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class Api:
    """One use case with devices, a workflow and provisioned users; invokes
    the handler with synthetic API Gateway events."""

    def __init__(self, env, api, logs, iotdata, s3, purposes):
        self.env = env
        self.api = api
        self.logs = logs
        self.iotdata = iotdata
        self.s3 = s3
        self.purposes = purposes
        self.usecase_id = env.create_usecase()
        env.stack.tables.usecases.update_item(
            Key={"usecase_id": self.usecase_id},
            UpdateExpression="SET account_id = :a, #r = :r, cross_account_role_arn = :arn",
            ExpressionAttributeNames={"#r": "region"},
            ExpressionAttributeValues={":a": PORTAL_ACCOUNT, ":r": REGION,
                                       ":arn": "arn:aws:iam::{0}:role/DDAPortalAccessRole".format(PORTAL_ACCOUNT)})
        self.workflow_id = str(uuid.uuid4())
        env.stack.tables.workflows.put_item(Item={"workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
                                                  "created_at": 1, "name": "wf", "current_version": WF_VERSION})
        self.devices = ["thing-a-" + self.usecase_id[-6:], "thing-b-" + self.usecase_id[-6:]]
        for device in self.devices:
            env.stack.tables.devices.put_item(Item={"device_id": device, "usecase_id": self.usecase_id,
                                                    "target_architecture": "arm64_jp5"})
        self.users = {}
        for role in ("Viewer", "Operator", "DataScientist", "UseCaseAdmin", "PortalAdmin", "DataLabeler"):
            user = env.make_user(role)
            # Provisioned as the backfill writes them: an enabled global row
            # (required under enforcement) and the use-case row.
            for scope in ("global", self.usecase_id):
                env.stack.tables.user_roles.put_item(Item={
                    "user_id": user["user_id"], "usecase_id": scope, "role": role,
                    "username": user["username"], "email": user["email"], "status": "enabled"})
            self.users[role] = user
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

    def event(self, method, resource, user, path_params=None, body=None, query=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or {}, "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"], "cognito:username": user["username"],
                "custom:role": user["role"]}}},
        }

    def call(self, method, resource, user, path_params=None, body=None, query=None):
        response = self.api.handler(self.event(method, resource, user, path_params, body, query), None)
        return response["statusCode"], json.loads(response["body"]) if response["body"] else {}

    def create(self, user=None, **body):
        payload = {"workflow_id": self.workflow_id, "device_id": self.devices[0]}
        payload.update(body)
        return self.call("POST", "/debug/usecases/{usecase_id}/sessions", user or self.users["Operator"],
                         {"usecase_id": self.usecase_id}, payload)

    def audit_rows(self, action):
        return [row for row in self.env.stack.tables.audit_log.scan()["Items"] if row.get("action") == action]

    def meta(self, session_id):
        return self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "META"}).get("Item")

    def pair(self, session_id, device):
        return self.table.get_item(Key={"pk": "SESSION#" + session_id, "sk": "PAIR#" + device}).get("Item")

    def lock(self, device):
        from workflow_debug import sessions as s
        key = s.pairlock_key(self.usecase_id, device, self.workflow_id)
        return self.table.get_item(Key=key).get("Item")


@pytest.fixture
def api(env, monkeypatch):
    monkeypatch.setenv("WORKFLOW_DEBUG_SESSIONS_TABLE", TABLE)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("PORTAL_REGISTRY_ENFORCED", raising=False)
    for name in ("DEBUG_LOG_RETENTION_DAYS", "DEBUG_MAX_SESSIONS_PER_USECASE", "DEBUG_MAX_DEVICES_PER_SESSION",
                 "DEBUG_MAX_MESSAGES_PER_SECOND", "DEBUG_MAX_BYTES_PER_SECOND",
                 "DEBUG_GET_LOG_EVENTS_PER_SECOND", "DEBUG_FILTER_LOG_EVENTS_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)
    ensure_sessions_table()
    for name in list(sys.modules):
        if name == "workflow_debug_api" or name.startswith("edge_telemetry_common") or name.startswith("workflow_debug"):
            sys.modules.pop(name, None)
    import workflow_debug_api as module
    from edge_telemetry_common import credentials
    from workflow_debug import log_reads
    logs, iotdata, s3 = StubLogs(), StubIotData(), StubS3()
    purposes = []

    def fake_client(service, usecase, purpose, region=None, **kwargs):
        purposes.append((service, purpose, kwargs.get("bucket")))
        return {"iot-data": iotdata, "s3": s3}[service]

    monkeypatch.setattr(credentials, "client", fake_client)
    monkeypatch.setattr(module, "_logs", lambda: logs)
    module.reset_caches()
    log_reads.reset()
    yield Api(env, module, logs, iotdata, s3, purposes)
    module.reset_caches()
    log_reads.reset()


# =========================================================================== permissions
class TestPermissionMatrix:
    def test_viewer_is_refused_on_post_with_an_audit_row(self, api):
        before = len(api.audit_rows("unauthorized_access"))
        status, body = api.create(user=api.users["Viewer"])
        assert status == 403
        assert body["error_code"] == "INSUFFICIENT_PERMISSIONS" and body["required_permission"] == "manage_devices"
        rows = api.audit_rows("unauthorized_access")
        assert len(rows) == before + 1
        assert rows[-1]["user_id"] == api.users["Viewer"]["user_id"]
        assert rows[-1]["details"]["usecase_id"] == api.usecase_id
        assert api.iotdata.updates == []

    def test_operator_and_usecase_admin_may_start(self, api):
        status, body = api.create(user=api.users["Operator"])
        assert status == 201, body
        status, body = api.create(user=api.users["UseCaseAdmin"], device_id=api.devices[1])
        assert status == 201, body

    def test_viewer_may_list_and_read_but_data_labeler_may_not(self, api):
        api.create()
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["Viewer"],
                                {"usecase_id": api.usecase_id})
        assert status == 200 and len(body["sessions"]) == 1
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["DataLabeler"],
                                {"usecase_id": api.usecase_id})
        assert status == 403 and body["error_code"] == "INSUFFICIENT_PERMISSIONS"
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/log", api.users["DataLabeler"],
                                {"usecase_id": api.usecase_id},
                                query={"device_id": api.devices[0], "workflow_id": api.workflow_id})
        assert status == 403
        session_id = api.create(device_id=api.devices[1])[1]["session_id"]
        assert api.call("GET", "/debug/sessions/{session_id}", api.users["DataLabeler"],
                        {"session_id": session_id})[0] == 403
        assert api.call("GET", "/debug/sessions/{session_id}", api.users["Viewer"],
                        {"session_id": session_id})[0] == 200
        assert api.call("DELETE", "/debug/sessions/{session_id}", api.users["Viewer"],
                        {"session_id": session_id})[0] == 403
        assert api.call("GET", "/debug/sessions/{session_id}/images/{execution_id}", api.users["DataLabeler"],
                        {"session_id": session_id, "execution_id": "e-1"})[0] == 403

    def test_console_link_is_portal_admin_only(self, api):
        session_id = api.create()[1]["session_id"]
        for role in ("Viewer", "Operator", "UseCaseAdmin"):
            status, body = api.call("GET", "/debug/sessions/{session_id}/console-link", api.users[role],
                                    {"session_id": session_id})
            assert status == 403 and body["error_code"] == "INSUFFICIENT_PERMISSIONS", role
        status, body = api.call("GET", "/debug/sessions/{session_id}/console-link", api.users["PortalAdmin"],
                                {"session_id": session_id})
        assert status == 200
        assert body["url"] == (
            "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/"
            "$252Fdda$252Fworkflow-debug$252F" + api.usecase_id + "/log-events/" + api.devices[0] + "$252F" + api.workflow_id)
        assert body["logStream"] == api.devices[0] + "/" + api.workflow_id

    def test_registry_unavailable_is_a_500_not_a_403(self, api, monkeypatch):
        from shared_utils import RegistryUnavailable, rbac_manager

        def boom(*args, **kwargs):
            raise RegistryUnavailable("registry down")

        monkeypatch.setattr(rbac_manager, "has_permission", boom)
        status, body = api.create()
        assert status == 500 and body["error_code"] == "registry_unavailable"
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["Viewer"],
                                {"usecase_id": api.usecase_id})
        assert status == 500 and body["error_code"] == "registry_unavailable"

    def test_unknown_route_and_options(self, api):
        status, body = api.call("GET", "/debug/nothing", api.users["Viewer"])
        assert status == 404 and body["error_code"] == "not_found"
        response = api.api.handler({"httpMethod": "OPTIONS", "resource": "/debug/usecases/{usecase_id}/sessions"}, None)
        assert response["statusCode"] == 200


# =========================================================================== creation
class TestCreation:
    def test_201_shape_and_the_shadow_document(self, api):
        status, body = api.create(duration_minutes=30, level="info")
        assert status == 201, body
        assert set(body) >= {"session_id", "state", "devices", "region_source", "expiresAt", "enabledAt", "logGroup"}
        assert body["state"] == "active"
        assert body["devices"] == [{"device_id": api.devices[0], "shadow": "ok"}]
        assert body["region_source"] == "header" and "warning" not in body
        assert body["expiresAt"] - body["enabledAt"] == 30 * 60 * 1000
        assert body["logGroup"] == "/dda/workflow-debug/" + api.usecase_id
        # The shadow document.
        assert len(api.iotdata.updates) == 1
        update = api.iotdata.updates[0]
        assert update["thingName"] == api.devices[0] and update["shadowName"] == "dda-workflow-debug"
        document = json.loads(update["payload"])
        entry = document["state"]["desired"]["sessions"][api.workflow_id]
        assert document["state"]["desired"]["v"] == 1
        assert entry == {
            "sessionId": body["session_id"], "usecaseId": api.usecase_id,
            "enabledAt": body["enabledAt"], "expiresAt": body["expiresAt"],
            "level": "info", "images": False, "imageMaxKb": 150,
            "bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "region": "eu-west-1",
            "prefix": "dda-telemetry/debug/{0}/{1}/{2}/{3}/".format(api.usecase_id, api.devices[0], api.workflow_id,
                                                                  body["session_id"]),
            "maxMessagesPerSecond": 10, "maxBytesPerSecond": 262144,
        }
        # The stored items.
        meta = api.meta(body["session_id"])
        assert meta["state"] == "active" and meta["workflow_version"] == WF_VERSION
        assert meta["media"] == {"bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "region": "eu-west-1",
                                 "region_source": "header"}
        assert meta["options"]["maxMessagesPerSecond"] == 10 and meta["options"]["durationMinutes"] == 30
        pair = api.pair(body["session_id"], api.devices[0])
        assert pair["state"] == "active" and pair["shadow"]["result"] == "ok"
        assert pair["logStream"] == api.devices[0] + "/" + api.workflow_id
        assert api.lock(api.devices[0])["session_id"] == body["session_id"]
        # The log group was prepared with the retention and tag.
        assert api.logs.names()[:3] == ["create_log_group", "put_retention_policy", "tag_resource"]
        assert api.logs.calls[1][1] == {"logGroupName": body["logGroup"], "retentionInDays": 14}
        assert api.logs.calls[2][1]["tags"] == {"dda-portal:usecase-id": api.usecase_id}
        # The audit row.
        rows = [r for r in api.audit_rows("debug_session_started") if r["resource_id"] == body["session_id"]]
        assert len(rows) == 1
        assert rows[0]["details"]["devices"] == [api.devices[0]]
        assert rows[0]["user_id"] == api.users["Operator"]["user_id"]
        # The region probe ran through image-read (decision P4), not media-retention-read.
        assert ("s3", "image-read", "dda-inference-results-" + PORTAL_ACCOUNT) in api.purposes
        assert all(p[1] != "media-retention-read" for p in api.purposes)
        assert api.s3.calls[0] == ("list_objects_v2", {"Bucket": "dda-inference-results-" + PORTAL_ACCOUNT,
                                                      "Prefix": "dda-telemetry/debug/" + api.usecase_id + "/",
                                                      "MaxKeys": 1})
        assert ("iot-data", "debug-control", None) in api.purposes

    def test_deployment_values_from_the_environment_reach_the_shadow(self, api, monkeypatch):
        monkeypatch.setenv("DEBUG_MAX_MESSAGES_PER_SECOND", "5")
        monkeypatch.setenv("DEBUG_MAX_BYTES_PER_SECOND", "65536")
        monkeypatch.setenv("DEBUG_LOG_RETENTION_DAYS", "30")
        status, body = api.create()
        assert status == 201
        entry = json.loads(api.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][api.workflow_id]
        assert entry["maxMessagesPerSecond"] == 5 and entry["maxBytesPerSecond"] == 65536
        assert api.logs.calls[1][1]["retentionInDays"] == 30

    def test_region_falls_back_to_the_use_case_with_a_warning(self, api):
        api.s3.region_header = None
        status, body = api.create()
        assert status == 201
        assert body["region_source"] == "usecase" and "warning" in body
        entry = json.loads(api.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][api.workflow_id]
        assert entry["region"] == REGION

    def test_region_from_a_redirect_errors_header(self, api):
        api.s3.list_raises = client_error("PermanentRedirect", "ListObjectsV2", 301,
                                          {"x-amz-bucket-region": "us-west-2"})
        status, body = api.create()
        assert status == 201 and body["region_source"] == "header"
        entry = json.loads(api.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][api.workflow_id]
        assert entry["region"] == "us-west-2"

    def test_override_bucket_is_used(self, api):
        api.env.stack.tables.usecases.update_item(Key={"usecase_id": api.usecase_id},
                                                  UpdateExpression="SET inference_uploader_s3_bucket = :b",
                                                  ExpressionAttributeValues={":b": "custom-results-bucket"})
        status, body = api.create()
        assert status == 201
        entry = json.loads(api.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][api.workflow_id]
        assert entry["bucket"] == "custom-results-bucket"

    def test_failed_shadow_write_leaves_the_session_pending(self, api):
        api.iotdata.fail[api.devices[0]] = client_error("ResourceNotFoundException", "UpdateThingShadow", 404)
        status, body = api.create()
        assert status == 201
        assert body["state"] == "pending"
        assert body["devices"] == [{"device_id": api.devices[0], "shadow": "failed", "error": "thing_not_found"}]
        assert api.meta(body["session_id"])["state"] == "pending"
        pair = api.pair(body["session_id"], api.devices[0])
        assert pair["state"] == "pending" and pair["shadow"] == {"result": "failed", "error": "thing_not_found",
                                                                 "writtenAt": pair["shadow"]["writtenAt"]}

    def test_denied_and_throttled_shadow_writes_are_classified(self, api):
        api.iotdata.fail[api.devices[0]] = client_error("AccessDeniedException", "UpdateThingShadow", 403)
        api.iotdata.fail[api.devices[1]] = client_error("ThrottlingException", "UpdateThingShadow", 429)
        api.env.put_deployment(api.usecase_id, status="ACTIVE", component_type="workflow",
                               workflow_id=api.workflow_id, target_devices=api.devices)
        status, body = api.create(device_id=None, all_devices=True)
        assert status == 201 and body["state"] == "pending"
        by_device = {row["device_id"]: row for row in body["devices"]}
        assert by_device[api.devices[0]]["error"] == "access_denied"
        assert by_device[api.devices[1]]["error"] == "throttled"
        # The throttled write was retried once.
        assert sum(1 for u in api.iotdata.updates if u["thingName"] == api.devices[1]) == 2


class TestCreationRefusals:
    def test_invalid_session_names_the_field(self, api):
        cases = [
            ({"workflow_id": None}, "workflow_id"),
            ({"workflow_id": str(uuid.uuid4())}, "workflow_id"),
            ({"device_id": None}, "device_id"),
            ({"device_id": api.devices[0], "all_devices": True}, "device_id"),
            ({"all_devices": "yes", "device_id": None}, "all_devices"),
            ({"device_id": "bad thing name"}, "device_id"),
            ({"duration_minutes": 4}, "duration_minutes"),
            ({"duration_minutes": 1441}, "duration_minutes"),
            ({"duration_minutes": "60"}, "duration_minutes"),
            ({"level": "trace"}, "level"),
            ({"images": "no"}, "images"),
            ({"image_max_kb": 49}, "image_max_kb"),
            ({"image_max_kb": 301}, "image_max_kb"),
        ]
        for body, field in cases:
            status, answer = api.create(**body)
            assert status == 400, (body, answer)
            assert answer["error_code"] == "invalid_session" and answer["field"] == field, (body, answer)
        assert api.iotdata.updates == [] and api.meta("x") is None

    def test_malformed_body(self, api):
        event = api.event("POST", "/debug/usecases/{usecase_id}/sessions", api.users["Operator"],
                          {"usecase_id": api.usecase_id})
        event["body"] = "{not json"
        response = api.api.handler(event, None)
        assert response["statusCode"] == 400 and json.loads(response["body"])["error_code"] == "invalid_session"

    def test_unknown_usecase_is_404(self, api):
        user = api.users["PortalAdmin"]
        status, body = api.call("POST", "/debug/usecases/{usecase_id}/sessions", user,
                                {"usecase_id": "uc-" + str(uuid.uuid4())},
                                {"workflow_id": api.workflow_id, "device_id": api.devices[0]})
        assert status == 404 and body["error_code"] == "usecase_not_found"

    def test_device_not_recorded_with_the_remedy(self, api):
        status, body = api.create(device_id="unrecorded-thing")
        assert status == 400 and body["error_code"] == "device_not_recorded"
        assert "UseCaseAdmin or PortalAdmin" in body["remedy"] and "node_designer:manage" in body["remedy"]
        assert "Operators do not hold" in body["remedy"]
        # A device recorded under another use case counts as unrecorded here.
        api.env.stack.tables.devices.put_item(Item={"device_id": "elsewhere-thing", "usecase_id": "uc-other"})
        api.api.reset_caches()
        assert api.create(device_id="elsewhere-thing")[1]["error_code"] == "device_not_recorded"

    def test_no_target_devices_names_both_reasons(self, api):
        api.env.put_deployment(api.usecase_id, status="ACTIVE", component_type="workflow",
                               workflow_id=api.workflow_id, target_devices=["thing-without-record"])
        status, body = api.create(device_id=None, all_devices=True)
        assert status == 400 and body["error_code"] == "no_target_devices"
        assert body["no_device_record"] == ["thing-without-record"]
        assert body["no_deployment"] == sorted(api.devices)
        assert "node_designer:manage" in body["remedy"]

    def test_all_devices_resolves_the_intersection(self, api):
        api.env.put_deployment(api.usecase_id, status="ACTIVE", component_type="workflow",
                               workflow_id=api.workflow_id, target_devices=[api.devices[1], "thing-without-record"])
        status, body = api.create(device_id=None, all_devices=True)
        assert status == 201
        assert [row["device_id"] for row in body["devices"]] == [api.devices[1]]
        assert api.meta(body["session_id"])["all_devices"] is True

    def test_devices_per_session_cap(self, api, monkeypatch):
        monkeypatch.setenv("DEBUG_MAX_DEVICES_PER_SESSION", "1")
        api.env.put_deployment(api.usecase_id, status="ACTIVE", component_type="workflow",
                               workflow_id=api.workflow_id, target_devices=api.devices)
        status, body = api.create(device_id=None, all_devices=True)
        assert status == 409 and body["error_code"] == "debug_session_limit"
        assert body["limit"] == "devices_per_session" and body["active"] == 2 and body["max"] == 1
        assert api.iotdata.updates == []

    def test_images_need_registry_enforcement(self, api):
        status, body = api.create(images=True)
        assert status == 409 and body["error_code"] == "registry_enforcement_required"
        assert "PORTAL_REGISTRY_ENFORCED" in body["remedy"]
        assert api.iotdata.updates == [] and api.s3.calls == []

    def test_images_need_the_lifecycle_rule(self, api, monkeypatch):
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
        api.s3.lifecycle_raises = client_error("NoSuchLifecycleConfiguration", "GetBucketLifecycleConfiguration", 404)
        status, body = api.create(images=True)
        assert status == 409 and body["error_code"] == "debug_images_unavailable"
        assert "telemetry_lifecycle.py" in body["remedy"] and body["reason"] == "rule_absent"
        assert ("s3", "media-retention-read", "dda-inference-results-" + PORTAL_ACCOUNT) in api.purposes
        from telemetry import retention
        api.s3.lifecycle_raises = None
        api.s3.rules = retention.telemetry_rules()[:2]
        assert api.create(images=True)[1]["error_code"] == "debug_images_unavailable"
        api.s3.rules = retention.telemetry_rules()
        status, body = api.create(images=True, image_max_kb=200)
        assert status == 201
        entry = json.loads(api.iotdata.updates[0]["payload"])["state"]["desired"]["sessions"][api.workflow_id]
        assert entry["images"] is True and entry["imageMaxKb"] == 200

    def test_sink_unavailable(self, api):
        api.logs.raise_on_create_group = client_error("AccessDeniedException", "CreateLogGroup", 403)
        status, body = api.create()
        assert status == 502 and body["error_code"] == "debug_sink_unavailable"
        assert api.iotdata.updates == []

    def test_usecase_cap_creates_nothing(self, api, monkeypatch):
        monkeypatch.setenv("DEBUG_MAX_SESSIONS_PER_USECASE", "1")
        assert api.create()[0] == 201
        status, body = api.create(device_id=api.devices[1])
        assert status == 409 and body["error_code"] == "debug_session_limit"
        assert body["limit"] == "usecase" and body["active"] == 1 and body["max"] == 1
        assert api.lock(api.devices[1]) is None
        assert len(api.iotdata.updates) == 1

    def test_device_cap_of_two_sessions(self, api):
        other_wf = str(uuid.uuid4())
        third_wf = str(uuid.uuid4())
        for wf in (other_wf, third_wf):
            api.env.stack.tables.workflows.put_item(Item={"workflow_id": wf, "usecase_id": api.usecase_id, "created_at": 1})
        assert api.create()[0] == 201
        assert api.create(workflow_id=other_wf)[0] == 201
        status, body = api.create(workflow_id=third_wf)
        assert status == 409 and body["error_code"] == "debug_session_limit"
        assert body["limit"] == "device" and body["device"] == api.devices[0] and body["active"] == 2

    def test_second_creation_for_one_pair_is_refused(self, api):
        status, first = api.create()
        assert status == 201
        status, body = api.create()
        assert status == 409 and body["error_code"] == "debug_session_exists"
        assert body["session_id"] == first["session_id"] and body["device"] == api.devices[0]
        assert len(api.iotdata.updates) == 1
        metas = [i for i in api.table.query(IndexName="by-usecase",
                                            KeyConditionExpression=boto3.dynamodb.conditions.Key("gsi_usecase").eq(
                                                "UC#" + api.usecase_id))["Items"]]
        assert len(metas) == 1

    def test_two_concurrent_creations_commit_exactly_one(self, api):
        """Driven through the PAIRLOCK condition: the two transactions are
        built first (both see a free Pair), then committed one after the
        other; the second cannot commit."""
        from workflow_debug import sessions as s
        client = boto3.client("dynamodb", region_name=REGION)
        built = []
        for _ in range(2):
            built.append(s.creation_actions(
                TABLE, session_id=str(uuid.uuid4()), usecase_id=api.usecase_id, workflow_id=api.workflow_id,
                workflow_version="1", devices=[api.devices[1]], all_devices=False, enabled_at=1, expires_at=2,
                actor="u", options={}, log_group="/g", max_sessions_per_usecase=4))
        client.transact_write_items(TransactItems=s.serialize_actions(built[0]))
        with pytest.raises(ClientError) as excinfo:
            client.transact_write_items(TransactItems=s.serialize_actions(built[1]))
        assert excinfo.value.response["Error"]["Code"] == "TransactionCanceledException"
        assert api.lock(api.devices[1])["session_id"] == built[0][0]["Put"]["Item"]["session_id"]
        assert api.meta(built[1][0]["Put"]["Item"]["session_id"]) is None


# =========================================================================== listing, detail, stop
class TestSessionsReadAndStop:
    def test_list_and_detail(self, api):
        api.iotdata.reported[api.devices[0]] = {"state": {"reported": {
            "v": 1, "instance": "inst-1", "agentVersion": "1.0.0",
            "sessions": {api.workflow_id: {"sessionId": "x", "state": "active", "counters": {"records": 5,
                                                                                           "publishFailures": 0}}}}}}
        session_id = api.create()[1]["session_id"]
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["Viewer"],
                                {"usecase_id": api.usecase_id})
        assert status == 200 and body["next"] is None
        assert [s["session_id"] for s in body["sessions"]] == [session_id]
        listed = body["sessions"][0]
        assert listed["state"] == "active" and listed["devices"][0]["device_id"] == api.devices[0]
        assert listed["devices"][0]["logStream"] == api.devices[0] + "/" + api.workflow_id
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["Viewer"],
                                {"usecase_id": api.usecase_id}, query={"state": "stopped"})
        assert status == 200 and body["sessions"] == []
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/sessions", api.users["Viewer"],
                                {"usecase_id": api.usecase_id}, query={"state": "nope"})
        assert status == 400 and body["error_code"] == "invalid_query"
        status, body = api.call("GET", "/debug/sessions/{session_id}", api.users["Viewer"], {"session_id": session_id})
        assert status == 200
        assert body["session_id"] == session_id and body["workflow_id"] == api.workflow_id
        assert body["devices"][0]["reported"]["state"] == "active"
        assert body["devices"][0]["reported"]["counters"]["records"] == 5
        assert body["devices"][0]["reported"]["instance"] == "inst-1"
        status, body = api.call("GET", "/debug/sessions/{session_id}", api.users["Viewer"],
                                {"session_id": str(uuid.uuid4())})
        assert status == 404 and body["error_code"] == "no_debug_session"

    def test_delete_writes_null_and_stops(self, api):
        session_id = api.create()[1]["session_id"]
        api.iotdata.updates.clear()
        status, body = api.call("DELETE", "/debug/sessions/{session_id}", api.users["Operator"], {"session_id": session_id})
        assert status == 200
        assert body["state"] == "stopped" and body["devices"] == [{"device_id": api.devices[0], "shadow": "ok"}]
        assert len(api.iotdata.updates) == 1
        document = json.loads(api.iotdata.updates[0]["payload"])
        assert document == {"state": {"desired": {"v": 1, "sessions": {api.workflow_id: None}}}}
        assert api.meta(session_id)["state"] == "stopped"
        assert api.pair(session_id, api.devices[0])["state"] == "stopped"
        assert api.lock(api.devices[0]) is None
        assert [r for r in api.audit_rows("debug_session_stopped") if r["resource_id"] == session_id]
        # A second stop is refused; the Pair is free for a new session.
        status, body = api.call("DELETE", "/debug/sessions/{session_id}", api.users["Operator"], {"session_id": session_id})
        assert status == 409 and body["error_code"] == "debug_session_not_live"
        assert api.create()[0] == 201
        status, body = api.call("DELETE", "/debug/sessions/{session_id}", api.users["Operator"],
                                {"session_id": str(uuid.uuid4())})
        assert status == 404 and body["error_code"] == "no_debug_session"


# =========================================================================== the log route
def cw_event(record, timestamp=5000):
    return {"timestamp": timestamp, "message": json.dumps(record), "ingestionTime": timestamp}


RUN = {"type": "run", "ts": 1000, "id": "s:i:1:0", "writeTs": 5000, "receivedMs": 4000, "executionId": "e-1",
       "status": "failed", "nodes": [{"nodeId": "detector"}, {"nodeId": "publish"}]}
LOG_INFO = {"type": "log", "ts": 1500, "id": "s:i:1:1", "writeTs": 5000, "receivedMs": 4000, "level": "INFO",
            "message": "between"}
LOG_ERROR = {"type": "log", "ts": 2000, "id": "s:i:1:2", "writeTs": 5000, "receivedMs": 4000, "level": "ERROR",
             "message": "oops"}
DROP = {"type": "drop", "ts": 2500, "id": "s:i:2:0", "writeTs": 5001, "receivedMs": 4001, "nodeId": "publish",
        "reason": "queue_full", "count": 3}


class TestLogRoute:
    def read(self, api, user=None, reset_bucket=True, **query):
        if reset_bucket:
            # Each read starts with a full bucket; the pacing itself is
            # tested by test_429_from_the_bucket_without_a_cloudwatch_call.
            from workflow_debug import log_reads
            log_reads.reset()
        params = {"device_id": api.devices[0], "workflow_id": api.workflow_id}
        params.update({k: v for k, v in query.items() if v is not None})
        return api.call("GET", "/debug/usecases/{usecase_id}/log", user or api.users["Viewer"],
                        {"usecase_id": api.usecase_id}, query=params)

    def test_404_without_any_session_for_the_pair(self, api):
        status, body = self.read(api)
        assert status == 404 and body["error_code"] == "no_debug_session"
        assert api.logs.reads() == []

    def test_window_and_limit_validation(self, api):
        api.create()
        for query, field in [({"from": "x"}, "from"), ({"from": 2000, "to": 1000}, "from"),
                             ({"from": 0, "to": 24 * 3600 * 1000 + 1}, "to"), ({"limit": 0}, "limit"),
                             ({"limit": 10001}, "limit"), ({"execution_id": "bad id!"}, "execution_id"),
                             ({"node_id": "n" * 129}, "node_id"), ({"level": "TRACE"}, "level"),
                             ({"type": "other"}, "type"), ({"ts_from": "abc"}, "ts_from")]:
            status, body = self.read(api, **query)
            assert status == 400 and body["error_code"] == "invalid_query" and body["field"] == field, query
        status, body = api.call("GET", "/debug/usecases/{usecase_id}/log", api.users["Viewer"],
                                {"usecase_id": api.usecase_id}, query={"workflow_id": api.workflow_id})
        assert status == 400 and body["field"] == "device_id"
        assert api.logs.reads() == []

    def test_default_window_and_page_size(self, api):
        api.create()
        status, body = self.read(api)
        assert status == 200 and body == {"events": [], "next": None}
        name, kwargs = api.logs.reads()[0]
        assert name == "get_log_events"
        assert kwargs["logGroupName"] == "/dda/workflow-debug/" + api.usecase_id
        assert kwargs["logStreamName"] == api.devices[0] + "/" + api.workflow_id
        assert kwargs["startFromHead"] is True and kwargs["limit"] == 2000 and "nextToken" not in kwargs
        assert kwargs["endTime"] - kwargs["startTime"] == 15 * 60 * 1000
        rows = [r for r in api.audit_rows("debug_log_viewed") if r["details"].get("device_id") == api.devices[0]
                and r["user_id"] == api.users["Viewer"]["user_id"]]
        assert rows

    def test_events_filters_and_the_same_token_end(self, api):
        api.create()
        api.logs.get_response = {"events": [cw_event(RUN), cw_event(LOG_INFO), cw_event(LOG_ERROR), cw_event(DROP, 5001)],
                                 "nextForwardToken": "f/1"}
        status, body = self.read(api, limit=500, **{"from": 1, "to": 9000})
        assert status == 200
        assert [e["id"] for e in body["events"]] == ["s:i:1:0", "s:i:1:1", "s:i:1:2", "s:i:2:0"]
        assert body["events"][0] == {"id": "s:i:1:0", "writeTs": 5000, "receivedMs": 4000, "ts": 1000, "record": RUN}
        assert body["next"] == "f/1"
        kwargs = api.logs.reads()[-1][1]
        assert kwargs["limit"] == 500 and kwargs["startTime"] == 1 and kwargs["endTime"] == 9000
        # Server-side filters (each a distinct query, so the 2 s cache does not answer).
        assert [e["id"] for e in self.read(api, type="log", **{"from": 1, "to": 9000})[1]["events"]] == ["s:i:1:1", "s:i:1:2"]
        assert [e["id"] for e in self.read(api, level="error", **{"from": 1, "to": 9000})[1]["events"]] == ["s:i:1:2"]
        assert [e["id"] for e in self.read(api, node_id="publish", **{"from": 1, "to": 9000})[1]["events"]] == ["s:i:1:0", "s:i:2:0"]
        assert [e["id"] for e in self.read(api, ts_from=1500, ts_to=2000, **{"from": 1, "to": 9000})[1]["events"]] == ["s:i:1:1", "s:i:1:2"]
        # The same-token page is the end of the window.
        status, body = self.read(api, next="f/1", **{"from": 1, "to": 9000})
        assert status == 200 and body["next"] is None
        assert api.logs.reads()[-1][1]["nextToken"] == "f/1"
        # An empty page is the end too.
        api.logs.get_response = {"events": [], "nextForwardToken": "f/2"}
        assert self.read(api, next="f/9", **{"from": 1, "to": 9000})[1]["next"] is None

    def test_execution_id_uses_filter_log_events_with_the_stream_list(self, api):
        api.create()
        api.logs.filter_response = {"events": [cw_event(RUN)], "nextToken": "n/1"}
        status, body = self.read(api, execution_id="e-1", **{"from": 1, "to": 9000})
        assert status == 200 and body["next"] == "n/1" and [e["id"] for e in body["events"]] == ["s:i:1:0"]
        name, kwargs = api.logs.reads()[-1]
        assert name == "filter_log_events"
        assert kwargs["logStreamNames"] == [api.devices[0] + "/" + api.workflow_id]
        assert kwargs["filterPattern"] == '{ $.executionId = "e-1" }'
        assert "logStreamNamePrefix" not in kwargs
        api.logs.filter_response = {"events": [cw_event(RUN)]}
        status, body = self.read(api, execution_id="e-1", next="n/1", **{"from": 1, "to": 9000})
        assert body["next"] is None and api.logs.reads()[-1][1]["nextToken"] == "n/1"

    def test_identical_queries_are_answered_from_the_two_second_cache(self, api):
        api.create()
        self.read(api, **{"from": 1, "to": 9000})
        self.read(api, **{"from": 1, "to": 9000})
        assert len(api.logs.reads()) == 1

    def test_empty_stream_rejected_token_and_throttling(self, api):
        api.create()
        api.logs.raise_on_read = client_error("ResourceNotFoundException", "GetLogEvents", 400)
        status, body = self.read(api, **{"from": 1, "to": 9000})
        assert status == 200 and body == {"events": [], "next": None}
        api.logs.raise_on_read = client_error("InvalidParameterException", "GetLogEvents", 400)
        status, body = self.read(api, next="f/bad", **{"from": 1, "to": 9001})
        assert status == 400 and body["error_code"] == "invalid_token"
        api.logs.raise_on_read = client_error("ThrottlingException", "GetLogEvents", 400)
        status, body = self.read(api, **{"from": 1, "to": 9002})
        assert status == 429 and body["error_code"] == "log_read_throttled" and body["retry_after_ms"] == 3000

    def test_429_from_the_bucket_without_a_cloudwatch_call(self, api):
        api.create()
        from workflow_debug import log_reads
        pacer = log_reads.reset()
        for _ in range(4):
            pacer.take("get")
        status, body = self.read(api, reset_bucket=False, **{"from": 1, "to": 9000})
        assert status == 429 and body["error_code"] == "log_read_throttled"
        assert body["retry_after_ms"] == 250
        assert api.logs.reads() == []
        pacer.take("filter"), pacer.take("filter")
        status, body = self.read(api, reset_bucket=False, execution_id="e-1", **{"from": 1, "to": 9000})
        assert status == 429 and body["retry_after_ms"] == 500 and api.logs.reads() == []

    def test_log_viewed_is_audited_once_per_user_and_session_per_hour(self, api):
        api.create()
        before = len(api.audit_rows("debug_log_viewed"))
        self.read(api, **{"from": 1, "to": 9000})
        self.read(api, **{"from": 1, "to": 9001})
        assert len(api.audit_rows("debug_log_viewed")) == before + 1
        self.read(api, user=api.users["Operator"], **{"from": 1, "to": 9002})
        assert len(api.audit_rows("debug_log_viewed")) == before + 2

    def test_stopped_sessions_still_serve_their_stream(self, api):
        session_id = api.create()[1]["session_id"]
        api.call("DELETE", "/debug/sessions/{session_id}", api.users["Operator"], {"session_id": session_id})
        assert self.read(api)[0] == 200


# =========================================================================== images and the console link
class TestImages:
    def route(self, api, session_id, execution_id="exec-1", user=None, device=None):
        return api.call("GET", "/debug/sessions/{session_id}/images/{execution_id}", user or api.users["Viewer"],
                        {"session_id": session_id, "execution_id": execution_id},
                        query={"device_id": device} if device else None)

    def test_guard_while_enforcement_is_off(self, api):
        session_id = api.create()[1]["session_id"]
        status, body = self.route(api, session_id)
        assert status == 409 and body["error_code"] == "registry_enforcement_required"
        assert "PORTAL_REGISTRY_ENFORCED" in body["remedy"]
        assert not any(n == "head_object" for n, _ in api.s3.calls)

    def test_head_and_presign_through_image_read(self, api, monkeypatch):
        session_id = api.create()[1]["session_id"]
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
        key = "dda-telemetry/debug/{0}/{1}/{2}/{3}/exec-1.jpg".format(api.usecase_id, api.devices[0], api.workflow_id, session_id)
        status, body = self.route(api, session_id)
        assert status == 404 and body["error_code"] == "image_not_found"
        api.s3.objects.add(key)
        status, body = self.route(api, session_id)
        assert status == 200
        assert body["expires_in"] == 120 and body["key"] == key and body["url"].startswith("https://")
        presign = [k for n, k in api.s3.calls if n == "generate_presigned_url"][-1]
        assert presign["ExpiresIn"] == 120 and presign["Params"] == {"Bucket": "dda-inference-results-" + PORTAL_ACCOUNT, "Key": key}
        assert ("s3", "image-read", "dda-inference-results-" + PORTAL_ACCOUNT) in api.purposes
        status, body = self.route(api, session_id, execution_id="bad id!")
        assert status == 400 and body["error_code"] == "invalid_query"
        status, body = self.route(api, session_id, device="not-in-session")
        assert status == 400 and body["field"] == "device_id"
        status, body = self.route(api, str(uuid.uuid4()))
        assert status == 404 and body["error_code"] == "no_debug_session"


class TestConsoleLink:
    def test_device_selection(self, api):
        api.env.put_deployment(api.usecase_id, status="ACTIVE", component_type="workflow",
                               workflow_id=api.workflow_id, target_devices=api.devices)
        session_id = api.create(device_id=None, all_devices=True)[1]["session_id"]
        status, body = api.call("GET", "/debug/sessions/{session_id}/console-link", api.users["PortalAdmin"],
                                {"session_id": session_id})
        assert status == 400 and body["field"] == "device_id"
        status, body = api.call("GET", "/debug/sessions/{session_id}/console-link", api.users["PortalAdmin"],
                                {"session_id": session_id}, query={"device_id": api.devices[1]})
        assert status == 200 and body["url"].endswith("/log-events/" + api.devices[1] + "$252F" + api.workflow_id)
        status, body = api.call("GET", "/debug/sessions/{session_id}/console-link", api.users["PortalAdmin"],
                                {"session_id": str(uuid.uuid4())})
        assert status == 404


# =========================================================================== no secrets in logs
class TestNoSecretsInLogs:
    def test_handler_logs_carry_no_signature_token_or_long_payload_string(self, api, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)
        session_id = api.create(user=api.users["Viewer"])[1].get("session_id")  # a refusal
        session_id = api.create()[1]["session_id"]
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
        key = "dda-telemetry/debug/{0}/{1}/{2}/{3}/exec-1.jpg".format(api.usecase_id, api.devices[0], api.workflow_id, session_id)
        api.s3.objects.add(key)
        api.call("GET", "/debug/sessions/{session_id}/images/{execution_id}", api.users["Viewer"],
                 {"session_id": session_id, "execution_id": "exec-1"})
        api.logs.get_response = {"events": [cw_event(dict(RUN, trigger={"payload": "p" * 400}))], "nextForwardToken": "f/1"}
        api.call("GET", "/debug/usecases/{usecase_id}/log", api.users["Viewer"], {"usecase_id": api.usecase_id},
                 query={"device_id": api.devices[0], "workflow_id": api.workflow_id, "from": "1", "to": "9000"})
        api.logs.raise_on_read = client_error("ThrottlingException", "GetLogEvents", 400)
        api.call("GET", "/debug/usecases/{usecase_id}/log", api.users["Viewer"], {"usecase_id": api.usecase_id},
                 query={"device_id": api.devices[0], "workflow_id": api.workflow_id, "from": "1", "to": "9001"})
        api.call("DELETE", "/debug/sessions/{session_id}", api.users["Operator"], {"session_id": session_id})
        ours = [r for r in caplog.records if r.name.startswith("workflow_debug")]
        text = "\n".join(r.getMessage() for r in ours)
        assert "X-Amz-Signature" not in text and "X-Amz-Security-Token" not in text
        assert "p" * 257 not in text
        for record in ours:
            assert len(record.getMessage()) < 512
