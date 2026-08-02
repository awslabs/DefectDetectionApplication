"""
Camera_Registry API mutation, conflict-reapply, and refresh routes
(camera-registry-sync task 6.2).

Minimal route-level verification against the moto-backed conftest stack
with a fake iot-data shadow client:
  - POST/PUT/DELETE write the shadow desired.changes entry FIRST and mark
    the registry entry pending with a fresh portal_change_id (Req 5.1)
  - origin edge-discovered mutations rejected with DISCOVERY_MANAGED (5.6)
  - shadow client failure -> 502 with registry state untouched
  - Viewer denied on mutation routes (Reqs 5.7, 12.2)
  - POST .../conflicts/{cid}/reapply re-issues the portal version (6.4)
  - POST .../cameras/refresh pulls the shadow and runs the ingest reducer

The full RBAC matrix, audit payload assertions, and boundary sweep are
task 6.6.

Requirements: 5.1, 5.6, 5.7, 6.4, 12.2, 12.3
"""
import io
import json
import os
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-mutation-routes"
SETTINGS_TABLE_NAME = "test-settings-camera-mutation"


@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """Camera registry + settings tables and a freshly bound handler module."""
    import boto3

    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    sys.modules.pop("camera_registry", None)
    sys.modules.pop("camera_sync", None)
    import camera_registry

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
    )


class FakeIotDataClient:
    """Records update_thing_shadow writes; serves get_thing_shadow pulls."""

    def __init__(self, shadow_document=None, fail_update=False):
        self.updates = []
        self.shadow_document = shadow_document
        self.fail_update = fail_update

    def update_thing_shadow(self, thingName, shadowName, payload):
        if self.fail_update:
            raise ClientError(
                {"Error": {"Code": "UnauthorizedException",
                           "Message": "assumed role failure"}},
                "UpdateThingShadow")
        self.updates.append({
            "thing_name": thingName,
            "shadow_name": shadowName,
            "payload": json.loads(payload),
        })
        return {}

    def get_thing_shadow(self, thingName, shadowName):
        if self.shadow_document is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "no shadow"}},
                "GetThingShadow")
        return {"payload": io.BytesIO(
            json.dumps(self.shadow_document).encode())}


@pytest.fixture
def fake_shadow(camera_env, monkeypatch):
    """Replace the assumed-role iot-data client with a recording fake."""
    client = FakeIotDataClient()
    monkeypatch.setattr(camera_env.module, "iot_data_client",
                        lambda usecase_id: client)
    return client


def make_event(method, device_id, user, sub_path="", query=None, body=None):
    path = f"/devices/{device_id}/cameras{sub_path}"
    path_parameters = {"id": device_id}
    if sub_path.startswith("/conflicts/") and sub_path.endswith("/reapply"):
        path_parameters["cid"] = sub_path.split("/")[2]
    elif sub_path and sub_path not in ("/refresh",):
        path_parameters["csid"] = sub_path.lstrip("/")
    return {
        "httpMethod": method,
        "path": path,
        "pathParameters": path_parameters,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def invoke(camera_env, method, device_id, user, sub_path="", query=None,
           body=None):
    response = camera_env.module.handler(
        make_event(method, device_id, user, sub_path, query, body), None)
    return response["statusCode"], json.loads(response["body"])


def now_ms():
    return int(time.time() * 1000)


def put_meta(camera_env, device_id, usecase_id, last_report_at=None):
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": last_report_at or now_ms(), "never_synced": False,
    })


def put_camera(camera_env, device_id, usecase_id, csid, **attrs):
    item = {
        "device_id": device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": usecase_id,
        "name": attrs.pop("name", csid), "type": attrs.pop("type", "Camera"),
        "params": attrs.pop("params", {"devicePath": "/dev/video0"}),
        "capabilities": attrs.pop("capabilities", {}),
        "origin": attrs.pop("origin", "edge-configured"),
        "version": attrs.pop("version", 3),
        "sync_status": attrs.pop("sync_status", "synced"),
        "last_reported_at": attrs.pop("last_reported_at", now_ms()),
    }
    item.update(attrs)
    camera_env.registry.put_item(Item=item)


def get_camera_item(camera_env, device_id, csid):
    response = camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"})
    return response.get("Item")


class TestCreateCamera:
    def test_create_writes_shadow_first_and_marks_pending(
            self, camera_env, env, fake_shadow):
        """POST creates origin portal-created, pending, with the desired
        change delivered to the sync channel (Requirement 5.1)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, body = invoke(
            camera_env, "POST", device_id, user,
            body={"name": "Portal cam", "type": "Camera",
                  "params": {"devicePath": "/dev/video5"}})

        assert status == 201
        assert body["origin"] == "portal-created"
        assert body["sync_status"] == "pending"
        csid = body["camera_source_id"]
        change_id = body["portal_change_id"]
        assert change_id.startswith("pc-")

        # Shadow desired.changes entry (written first).
        assert len(fake_shadow.updates) == 1
        update = fake_shadow.updates[0]
        assert update["thing_name"] == device_id
        assert update["shadow_name"] == "dda-camera-registry"
        change = update["payload"]["state"]["desired"]["changes"][csid]
        assert change["op"] == "create"
        assert change["portalChangeId"] == change_id
        assert change["name"] == "Portal cam"
        assert change["params"] == {"devicePath": "/dev/video5"}

        # Registry entry marked pending with the same portal_change_id.
        item = get_camera_item(camera_env, device_id, csid)
        assert item["sync_status"] == "pending"
        assert item["portal_change_id"] == change_id
        assert item["origin"] == "portal-created"
        assert item["pending_content"]["op"] == "create"

    def test_shadow_failure_returns_502_and_leaves_registry_untouched(
            self, camera_env, env, monkeypatch):
        """An assumed-role shadow client failure returns 502 without
        creating any registry state (task 6.2)."""
        client = FakeIotDataClient(fail_update=True)
        monkeypatch.setattr(camera_env.module, "iot_data_client",
                            lambda usecase_id: client)
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, body = invoke(
            camera_env, "POST", device_id, user,
            body={"name": "cam", "type": "Camera",
                  "camera_source_id": "portal-x"})

        assert status == 502
        assert get_camera_item(camera_env, device_id, "portal-x") is None

    def test_viewer_cannot_mutate(self, camera_env, env, fake_shadow):
        """Mutation routes require the Operator permission (Reqs 5.7, 12.2)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, body = invoke(camera_env, "POST", device_id, user,
                              body={"name": "cam", "type": "Camera"})

        assert status == 403
        assert fake_shadow.updates == []


class TestUpdateAndDeleteCamera:
    def test_update_marks_pending_and_delivers_change(
            self, camera_env, env, fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)
        put_camera(camera_env, device_id, usecase_id, "cfg-1",
                   name="old name", version=7)

        status, body = invoke(
            camera_env, "PUT", device_id, user, sub_path="/cfg-1",
            body={"name": "new name", "type": "Camera",
                  "params": {"devicePath": "/dev/video1"}})

        assert status == 200
        change_id = body["portal_change_id"]
        change = (fake_shadow.updates[0]["payload"]["state"]["desired"]
                  ["changes"]["cfg-1"])
        assert change["op"] == "update"
        assert change["baseVersion"] == 7
        assert change["name"] == "new name"

        item = get_camera_item(camera_env, device_id, "cfg-1")
        assert item["sync_status"] == "pending"
        assert item["portal_change_id"] == change_id
        # Edge-reported content stays effective until the ack; the portal
        # version travels in pending_content.
        assert item["name"] == "old name"
        assert item["pending_content"]["name"] == "new name"

    def test_discovery_managed_sources_are_rejected(
            self, camera_env, env, fake_shadow):
        """Mutations of origin edge-discovered are rejected with
        DISCOVERY_MANAGED and no shadow write (Requirement 5.6)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)
        put_camera(camera_env, device_id, usecase_id, "disc-abc",
                   origin="edge-discovered")

        for method, body in (("PUT", {"name": "x", "type": "Camera"}),
                             ("DELETE", None)):
            status, response = invoke(camera_env, method, device_id, user,
                                      sub_path="/disc-abc", body=body)
            assert status == 409
            assert response["code"] == "DISCOVERY_MANAGED"

        assert fake_shadow.updates == []
        item = get_camera_item(camera_env, device_id, "disc-abc")
        assert item["sync_status"] == "synced"

    def test_delete_issues_pending_delete(self, camera_env, env, fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)
        put_camera(camera_env, device_id, usecase_id, "cfg-del", version=4)

        status, body = invoke(camera_env, "DELETE", device_id, user,
                              sub_path="/cfg-del")

        assert status == 200
        change = (fake_shadow.updates[0]["payload"]["state"]["desired"]
                  ["changes"]["cfg-del"])
        assert change["op"] == "delete"
        assert change["baseVersion"] == 4

        item = get_camera_item(camera_env, device_id, "cfg-del")
        assert item["sync_status"] == "pending"
        assert item["pending_content"] == {"op": "delete"}

    def test_unknown_camera_is_404(self, camera_env, env, fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, _ = invoke(camera_env, "PUT", device_id, user,
                           sub_path="/missing",
                           body={"name": "x", "type": "Camera"})
        assert status == 404
        assert fake_shadow.updates == []


class TestReapplyConflict:
    def test_reapply_reissues_portal_version_as_new_pending_change(
            self, camera_env, env, fake_shadow):
        """Re-apply issues the overridden portal version with a fresh
        portal_change_id and stamps the conflict item (Requirement 6.4)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)
        put_camera(camera_env, device_id, usecase_id, "cfg-c",
                   name="edge name", version=9)

        cid = str(uuid.uuid4())
        conflict_sk = f"CONFLICT#100#{cid}"
        camera_env.registry.put_item(Item={
            "device_id": device_id, "sk": conflict_sk,
            "usecase_id": usecase_id, "camera_source_id": "cfg-c",
            "edge_version": {"name": "edge name"},
            "portal_version": {"op": "update", "name": "portal name",
                               "type": "Camera",
                               "params": {"devicePath": "/dev/video2"}},
            "resolution": "edge-retained", "created_at": 100,
        })

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path=f"/conflicts/{cid}/reapply")

        assert status == 200
        change_id = body["portal_change_id"]
        change = (fake_shadow.updates[0]["payload"]["state"]["desired"]
                  ["changes"]["cfg-c"])
        assert change["op"] == "update"
        assert change["portalChangeId"] == change_id
        assert change["name"] == "portal name"
        assert change["baseVersion"] == 9

        item = get_camera_item(camera_env, device_id, "cfg-c")
        assert item["sync_status"] == "pending"
        assert item["portal_change_id"] == change_id

        conflict = camera_env.registry.get_item(
            Key={"device_id": device_id, "sk": conflict_sk})["Item"]
        assert conflict["reapplied_as"] == change_id

    def test_unknown_conflict_is_404(self, camera_env, env, fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, _ = invoke(camera_env, "POST", device_id, user,
                           sub_path=f"/conflicts/{uuid.uuid4()}/reapply")
        assert status == 404
        assert fake_shadow.updates == []


class TestRefresh:
    def test_refresh_pulls_shadow_and_runs_reducer(
            self, camera_env, env, monkeypatch):
        """POST .../cameras/refresh reduces the pulled reported state into
        the registry exactly like the ingest path (task 6.2)."""
        reported = {
            "schemaVersion": 1,
            "reportedAt": now_ms(),
            "cameras": {
                "cfg-r1": {"version": 2, "name": "Refreshed cam",
                           "type": "Camera", "origin": "edge-configured",
                           "params": {"devicePath": "/dev/video0"},
                           "capabilities": {}, "absent": False},
            },
        }
        client = FakeIotDataClient(shadow_document={
            "state": {"reported": reported}})
        monkeypatch.setattr(camera_env.module, "iot_data_client",
                            lambda usecase_id: client)

        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path="/refresh",
                              query={"usecase_id": usecase_id})

        assert status == 200
        assert body["state"] == "synced"
        assert body["count"] == 1
        assert body["cameras"][0]["camera_source_id"] == "cfg-r1"
        assert body["cameras"][0]["name"] == "Refreshed cam"
        assert body["cameras"][0]["sync_status"] == "synced"

    def test_refresh_without_shadow_is_404(self, camera_env, env,
                                           monkeypatch):
        client = FakeIotDataClient(shadow_document=None)
        monkeypatch.setattr(camera_env.module, "iot_data_client",
                            lambda usecase_id: client)
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"

        status, _ = invoke(camera_env, "POST", device_id, user,
                           sub_path="/refresh",
                           query={"usecase_id": usecase_id})
        assert status == 404


class TestAudit:
    def test_mutations_log_audit_events(self, camera_env, env, aws_stack,
                                        fake_shadow):
        """Mutating routes record audit events carrying the acting user,
        device, and camera source (Requirements 12.2, 12.3)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id)

        status, body = invoke(
            camera_env, "POST", device_id, user,
            body={"name": "cam", "type": "Camera"})
        assert status == 201

        events = aws_stack.tables.audit_log.scan()["Items"]
        matching = [e for e in events
                    if e["user_id"] == user["user_id"]
                    and e["action"] == "create_camera_source"]
        assert len(matching) == 1
        event = matching[0]
        assert event["resource_id"] == device_id
        assert event["details"]["camera_source_id"] == body["camera_source_id"]
        assert event["details"]["device_id"] == device_id
        assert event["timestamp"] > 0
