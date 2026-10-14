"""
Camera_Registry API read routes (camera-registry-sync task 6.1).

Minimal route-level verification against the moto-backed conftest stack:
GET /devices/{id}/cameras (never-synced state, per-entry staleness against
the Staleness_Threshold, absent passthrough) and
GET /devices/{id}/cameras/conflicts (newest first). The full RBAC matrix,
audit assertions, and boundary sweep are task 6.6.

Requirements: 1.3, 1.5, 1.6, 4.1, 4.2, 4.4, 6.3, 12.1
"""
import json
import os
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-read-routes"
SETTINGS_TABLE_NAME = "test-settings-camera-registry"

HOUR_MS = 3600 * 1000


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
    import camera_registry

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        settings=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_staleness_setting(camera_env):
    """Each test starts from the default Staleness_Threshold."""
    camera_env.settings.delete_item(
        Key={"setting_key": "camera_registry.staleness_threshold_hours"})
    yield


def make_event(method, device_id, user, sub_path="", query=None):
    path = f"/devices/{device_id}/cameras{sub_path}"
    return {
        "httpMethod": method,
        "resource": f"/devices/{{id}}/cameras{sub_path}",
        "path": path,
        "pathParameters": {"id": device_id},
        "queryStringParameters": query,
        "body": None,
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


def invoke(camera_env, method, device_id, user, sub_path="", query=None):
    response = camera_env.module.handler(
        make_event(method, device_id, user, sub_path, query), None)
    return response["statusCode"], json.loads(response["body"])


def now_ms():
    return int(time.time() * 1000)


def put_meta(camera_env, device_id, usecase_id, last_report_at,
             never_synced=False):
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": last_report_at, "never_synced": never_synced,
    })


def put_camera(camera_env, device_id, usecase_id, csid, **attrs):
    item = {
        "device_id": device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": usecase_id,
        "name": attrs.pop("name", csid), "type": attrs.pop("type", "Camera"),
        "params": attrs.pop("params", {"devicePath": "/dev/video0"}),
        "capabilities": attrs.pop("capabilities", {}),
        "origin": attrs.pop("origin", "edge-configured"),
        "version": attrs.pop("version", 1),
        "sync_status": attrs.pop("sync_status", "synced"),
    }
    item.update(attrs)
    camera_env.registry.put_item(Item=item)


class TestGetCameras:
    def test_never_synced_device_returns_explicit_state(self, camera_env, env):
        """A device with no completed synchronization reports
        state=never-synced, not a bare empty list (Requirement 1.6)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"

        status, body = invoke(camera_env, "GET", device_id, user,
                              query={"usecase_id": usecase_id})

        assert status == 200
        assert body["state"] == "never-synced"
        assert body["cameras"] == []
        assert body["device_status"] == "UNKNOWN"

    def test_unresolvable_usecase_is_rejected(self, camera_env, env):
        """A device the registry has never seen needs the usecase_id
        parameter for the authorization check."""
        user = env.make_user(role="Viewer")
        device_id = f"thing-{uuid.uuid4()}"

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 400
        assert "usecase_id" in body["error"]

    def test_synced_device_lists_entries_with_staleness_and_absence(
            self, camera_env, env):
        """Registry entries carry computed stale (Req 4.1), absent with
        absent_since (Req 4.4), META freshness, and device status (4.2)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        now = now_ms()

        put_meta(camera_env, device_id, usecase_id, last_report_at=now)
        put_camera(camera_env, device_id, usecase_id, "cfg-fresh",
                   name="a-fresh", last_reported_at=now)
        put_camera(camera_env, device_id, usecase_id, "cfg-old",
                   name="b-old", last_reported_at=now - 25 * HOUR_MS)
        put_camera(camera_env, device_id, usecase_id, "disc-gone",
                   name="c-gone", origin="edge-discovered",
                   last_reported_at=now, absent=True,
                   absent_since=now - HOUR_MS)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["state"] == "synced"
        assert body["usecase_id"] == usecase_id
        assert body["last_report_at"] == now
        assert body["staleness_threshold_hours"] == 24
        assert body["count"] == 3

        by_id = {c["camera_source_id"]: c for c in body["cameras"]}
        assert by_id["cfg-fresh"]["stale"] is False
        assert by_id["cfg-fresh"]["absent"] is False
        # 25 h old against the default 24 h Staleness_Threshold (Req 4.1)
        assert by_id["cfg-old"]["stale"] is True
        assert by_id["cfg-old"]["last_reported_at"] == now - 25 * HOUR_MS
        assert by_id["disc-gone"]["absent"] is True
        assert by_id["disc-gone"]["absent_since"] == now - HOUR_MS
        assert by_id["disc-gone"]["origin"] == "edge-discovered"

    def test_staleness_threshold_setting_is_honored(self, camera_env, env):
        """The settings entry overrides the default 24 h threshold."""
        camera_env.settings.put_item(Item={
            "setting_key": "camera_registry.staleness_threshold_hours",
            "value": 1,
        })
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        now = now_ms()

        put_meta(camera_env, device_id, usecase_id, last_report_at=now)
        put_camera(camera_env, device_id, usecase_id, "cfg-2h",
                   last_reported_at=now - 2 * HOUR_MS)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["staleness_threshold_hours"] == 1
        assert body["cameras"][0]["stale"] is True

    def test_device_usecase_wins_over_query_parameter(self, camera_env, env):
        """Authorization scopes to the device's own usecase_id from the
        registry, not a caller-supplied parameter (Reqs 1.4, 1.5)."""
        user = env.make_user(role="Viewer")
        device_usecase = env.create_usecase()
        other_usecase = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, device_usecase,
                 last_report_at=now_ms())

        status, body = invoke(camera_env, "GET", device_id, user,
                              query={"usecase_id": other_usecase})

        assert status == 200
        assert body["usecase_id"] == device_usecase


class TestGetConflicts:
    def test_conflicts_newest_first(self, camera_env, env):
        """Conflict events are returned newest first (Requirement 6.3)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())

        older_id, newer_id = str(uuid.uuid4()), str(uuid.uuid4())
        camera_env.registry.put_item(Item={
            "device_id": device_id, "sk": f"CONFLICT#100#{older_id}",
            "usecase_id": usecase_id, "camera_source_id": "cfg-a",
            "edge_version": {"name": "edge"}, "portal_version": {"name": "portal"},
            "resolution": "edge-retained", "created_at": 100,
        })
        camera_env.registry.put_item(Item={
            "device_id": device_id, "sk": f"CONFLICT#200#{newer_id}",
            "usecase_id": usecase_id, "camera_source_id": "cfg-b",
            "edge_version": None, "portal_version": {"name": "portal"},
            "resolution": "deletion-retained", "created_at": 200,
        })

        status, body = invoke(camera_env, "GET", device_id, user,
                              sub_path="/conflicts")

        assert status == 200
        assert body["count"] == 2
        assert [c["created_at"] for c in body["conflicts"]] == [200, 100]
        assert body["conflicts"][0]["conflict_id"] == newer_id
        assert body["conflicts"][0]["resolution"] == "deletion-retained"
        assert body["conflicts"][1]["camera_source_id"] == "cfg-a"
        assert body["conflicts"][1]["edge_version"] == {"name": "edge"}
