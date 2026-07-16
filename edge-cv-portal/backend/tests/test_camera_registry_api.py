"""
Camera_Registry API unit tests (camera-registry-sync task 6.6).

Fills the gaps the route-level suites (test_camera_registry_read_routes,
test_camera_registry_mutation_routes, test_camera_registry_settings)
leave open:

1. RBAC matrix per route: Viewer allowed on the read routes (GET cameras,
   GET conflicts, POST refresh), denied on every mutating route
   (POST/PUT/DELETE, conflict re-apply) with an `unauthorized_access`
   audit event; Operator allowed everywhere (Reqs 5.7, 12.1, 12.2).
2. Cross-use-case scoping: an elevated role granted through the
   user-roles table for a DIFFERENT Use_Case does not carry over — the
   mutating routes return 403 scoped to the device's own usecase_id and
   log `unauthorized_access` (Req 1.5). Note the portal's role model
   resolves the JWT custom:role claim as a GLOBAL baseline (rbac_manager
   falls back to it for any Use_Case), so per-use-case scoping is
   exercised through user-roles table assignments, and every valid
   baseline role includes view_devices — cross-use-case read denial does
   not exist in this RBAC model.
3. Staleness boundaries: an entry exactly AT the threshold is NOT stale
   (the check is strictly greater); one millisecond past it is (Req 4.1),
   for the default and a configured threshold (Req 4.3).
4. Disconnected-status pass-through from the underlying Greengrass
   core-device lookup (Req 4.2).
5. Settings default: unreadable/invalid stored threshold values fall
   back to the default 24 (Req 4.3).
6. Never-synced response shape with pending portal entries (Req 1.6).
7. Absent display fields at boundary detail (Req 4.4).
8. Conflict re-apply edge cases: deletion-retained re-create, an
   already-effective portal deletion, an event without a portal version
   (Req 6.4).
9. Audit event payload per mutating route — update/delete/reapply
   (create is covered by the mutation-routes suite): acting user,
   device, camera source, timestamp (Reqs 12.2, 12.3).

Runs against the moto-backed conftest stack with the sibling suites'
fixture pattern (own registry/settings tables, module re-import,
FakeIotDataClient for the sync channel).

_Requirements: 1.5, 1.6, 4.1, 4.2, 4.3, 4.4, 5.7, 6.4, 12.1, 12.2, 12.3_
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

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-api"
SETTINGS_TABLE_NAME = "test-settings-camera-registry-api"

HOUR_MS = 3600 * 1000
STALENESS_SETTING_KEY = "camera_registry.staleness_threshold_hours"


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
        settings=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_staleness_setting(camera_env):
    """Each test starts from the default Staleness_Threshold."""
    camera_env.settings.delete_item(
        Key={"setting_key": STALENESS_SETTING_KEY})
    yield


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
    """Recording fake for the sync channel; serves an empty valid report
    so the refresh route succeeds for authorized callers."""
    client = FakeIotDataClient(shadow_document={
        "state": {"reported": {"schemaVersion": 1,
                               "reportedAt": now_ms(),
                               "cameras": {}}},
    })
    monkeypatch.setattr(camera_env.module, "iot_data_client",
                        lambda usecase_id: client)
    return client


def make_event(method, device_id, user, sub_path="", query=None, body=None):
    path = f"/devices/{device_id}/cameras{sub_path}"
    path_parameters = {"id": device_id}
    if sub_path.startswith("/conflicts/") and sub_path.endswith("/reapply"):
        path_parameters["cid"] = sub_path.split("/")[2]
    elif sub_path and sub_path not in ("/refresh", "/conflicts"):
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


def put_meta(camera_env, device_id, usecase_id, last_report_at=None,
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
        "version": attrs.pop("version", 3),
        "sync_status": attrs.pop("sync_status", "synced"),
    }
    item.update(attrs)
    camera_env.registry.put_item(
        Item={k: v for k, v in item.items() if v is not None})


def put_conflict(camera_env, device_id, usecase_id, csid,
                 portal_version, created_at=100):
    cid = str(uuid.uuid4())
    camera_env.registry.put_item(
        Item={k: v for k, v in {
            "device_id": device_id, "sk": f"CONFLICT#{created_at}#{cid}",
            "usecase_id": usecase_id, "camera_source_id": csid,
            "edge_version": {"name": "edge"},
            "portal_version": portal_version,
            "resolution": "edge-retained", "created_at": created_at,
        }.items() if v is not None})
    return cid


def audit_events(aws_stack, user, action):
    return [e for e in aws_stack.tables.audit_log.scan()["Items"]
            if e["user_id"] == user["user_id"] and e["action"] == action]


def get_camera_item(camera_env, device_id, csid):
    return camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"}).get("Item")


# ---------------------------------------------------------------------------
# Per-route preparation for the RBAC matrix. Each entry seeds the device
# state a route needs and returns the invoke arguments.
# ---------------------------------------------------------------------------

def _route_get_cameras(camera_env, device_id, usecase_id):
    return dict(method="GET", sub_path="")


def _route_get_conflicts(camera_env, device_id, usecase_id):
    return dict(method="GET", sub_path="/conflicts")


def _route_refresh(camera_env, device_id, usecase_id):
    return dict(method="POST", sub_path="/refresh")


def _route_create(camera_env, device_id, usecase_id):
    return dict(method="POST", sub_path="",
                body={"name": "matrix cam", "type": "Camera",
                      "params": {"devicePath": "/dev/video7"}})


def _route_update(camera_env, device_id, usecase_id):
    put_camera(camera_env, device_id, usecase_id, "cfg-m",
               last_reported_at=now_ms())
    return dict(method="PUT", sub_path="/cfg-m",
                body={"name": "renamed", "type": "Camera",
                      "params": {"devicePath": "/dev/video0"}})


def _route_delete(camera_env, device_id, usecase_id):
    put_camera(camera_env, device_id, usecase_id, "cfg-m",
               last_reported_at=now_ms())
    return dict(method="DELETE", sub_path="/cfg-m")


def _route_reapply(camera_env, device_id, usecase_id):
    put_camera(camera_env, device_id, usecase_id, "cfg-m",
               last_reported_at=now_ms())
    cid = put_conflict(camera_env, device_id, usecase_id, "cfg-m",
                       {"op": "update", "name": "portal name",
                        "type": "Camera",
                        "params": {"devicePath": "/dev/video1"}})
    return dict(method="POST", sub_path=f"/conflicts/{cid}/reapply")


READ_ROUTES = {
    "get_cameras": (_route_get_cameras, 200),
    "get_conflicts": (_route_get_conflicts, 200),
    "refresh": (_route_refresh, 200),
}

MUTATING_ROUTES = {
    "create": (_route_create, 201),
    "update": (_route_update, 200),
    "delete": (_route_delete, 200),
    "reapply": (_route_reapply, 200),
}

ALL_ROUTES = {**READ_ROUTES, **MUTATING_ROUTES}


def prepare_and_invoke(camera_env, env, route_name, user):
    """Seed a fresh device for `route_name` and invoke it as `user`."""
    prepare, expected = ALL_ROUTES[route_name]
    usecase_id = env.create_usecase()
    device_id = f"thing-{uuid.uuid4()}"
    put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
    kwargs = prepare(camera_env, device_id, usecase_id)
    status, body = invoke(camera_env, device_id=device_id, user=user,
                          **kwargs)
    return status, body, expected, device_id, usecase_id


# ===========================================================================
# 1. RBAC matrix per route (Reqs 5.7, 12.1, 12.2)
# ===========================================================================

class TestRbacMatrix:

    @pytest.mark.parametrize("route_name", sorted(READ_ROUTES))
    def test_viewer_allowed_on_read_routes(self, camera_env, env,
                                           fake_shadow, route_name):
        """Viewer-held view_devices grants every read route (Req 12.1)."""
        user = env.make_user(role="Viewer")
        status, _, expected, _, _ = prepare_and_invoke(
            camera_env, env, route_name, user)
        assert status == expected

    @pytest.mark.parametrize("route_name", sorted(MUTATING_ROUTES))
    @pytest.mark.parametrize("role", ["Viewer", "DataScientist"])
    def test_non_operator_denied_on_mutating_routes(
            self, aws_stack, camera_env, env, fake_shadow, route_name, role):
        """Roles without manage_devices get 403 on every mutating route,
        nothing reaches the sync channel, and the denial is audited as
        unauthorized_access (Reqs 5.7, 12.2)."""
        user = env.make_user(role=role)
        status, body, _, device_id, usecase_id = prepare_and_invoke(
            camera_env, env, route_name, user)

        assert status == 403
        assert body["required_permission"] == "manage_devices"
        assert fake_shadow.updates == []

        denials = audit_events(aws_stack, user, "unauthorized_access")
        assert len(denials) == 1
        denial = denials[0]
        assert denial["result"] == "denied"
        assert denial["resource_type"] == "camera_registry"
        assert denial["resource_id"] == device_id
        assert denial["details"]["required_permission"] == "manage_devices"
        assert denial["details"]["usecase_id"] == usecase_id
        assert denial["timestamp"] > 0

    @pytest.mark.parametrize("route_name", sorted(ALL_ROUTES))
    def test_operator_allowed_on_every_route(self, camera_env, env,
                                             fake_shadow, route_name):
        """Operator-held permissions grant reads and mutations alike
        (Reqs 12.1, 12.2)."""
        user = env.make_user(role="Operator")
        status, _, expected, _, _ = prepare_and_invoke(
            camera_env, env, route_name, user)
        assert status == expected


# ===========================================================================
# 2. Cross-use-case scoping (Req 1.5)
# ===========================================================================

class TestCrossUseCaseScoping:

    @pytest.mark.parametrize("route_name", sorted(MUTATING_ROUTES))
    def test_operator_of_another_usecase_is_denied(
            self, aws_stack, camera_env, env, fake_shadow, route_name):
        """An Operator assignment in a DIFFERENT Use_Case does not reach
        the device: 403 scoped to the device's own usecase_id with an
        unauthorized_access audit event (Req 1.5)."""
        user = env.make_user(role="Viewer")
        other_usecase = env.create_usecase()
        env.assign_role(user, other_usecase, "Operator")

        status, _, _, device_id, device_usecase = prepare_and_invoke(
            camera_env, env, route_name, user)

        assert status == 403
        assert fake_shadow.updates == []
        denials = audit_events(aws_stack, user, "unauthorized_access")
        assert len(denials) == 1
        # The denial is scoped to the device's own Use_Case, not the one
        # the caller holds Operator in.
        assert denials[0]["details"]["usecase_id"] == device_usecase
        assert denials[0]["resource_id"] == device_id

    def test_operator_assignment_in_device_usecase_grants(
            self, camera_env, env, fake_shadow):
        """The same user shape succeeds once the user-roles table grants
        Operator for the device's own Use_Case — scoping is per-use-case
        (Reqs 1.5, 12.2)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        env.assign_role(user, usecase_id, "Operator")
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-s",
                   last_reported_at=now_ms())

        status, body = invoke(
            camera_env, "PUT", device_id, user, sub_path="/cfg-s",
            body={"name": "granted", "type": "Camera"})

        assert status == 200
        assert body["sync_status"] == "pending"
        assert len(fake_shadow.updates) == 1


# ===========================================================================
# 3. Staleness boundaries (Reqs 4.1, 4.3)
# ===========================================================================

class TestStalenessBoundaries:

    @pytest.fixture
    def frozen_now(self, camera_env, monkeypatch):
        """Pin the route's clock so boundary math is exact."""
        fixed = now_ms()
        monkeypatch.setattr(camera_env.module, "now_ms", lambda: fixed)
        return fixed

    def test_exactly_at_default_threshold_is_not_stale(
            self, camera_env, env, frozen_now):
        """An entry whose age equals the threshold exactly is NOT stale —
        the comparison is strictly greater (Req 4.1)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=frozen_now)
        put_camera(camera_env, device_id, usecase_id, "cfg-b",
                   last_reported_at=frozen_now - 24 * HOUR_MS)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["cameras"][0]["stale"] is False

    def test_one_ms_past_default_threshold_is_stale(
            self, camera_env, env, frozen_now):
        """One millisecond past the threshold flips to stale (Req 4.1)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=frozen_now)
        put_camera(camera_env, device_id, usecase_id, "cfg-b",
                   last_reported_at=frozen_now - 24 * HOUR_MS - 1)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["cameras"][0]["stale"] is True

    def test_boundary_follows_the_configured_threshold(
            self, camera_env, env, frozen_now):
        """The same strict boundary applies to a PortalAdmin-configured
        threshold (Reqs 4.1, 4.3)."""
        camera_env.settings.put_item(Item={
            "setting_key": STALENESS_SETTING_KEY, "value": 2})
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=frozen_now)
        put_camera(camera_env, device_id, usecase_id, "cfg-at",
                   name="a-at", last_reported_at=frozen_now - 2 * HOUR_MS)
        put_camera(camera_env, device_id, usecase_id, "cfg-past",
                   name="b-past",
                   last_reported_at=frozen_now - 2 * HOUR_MS - 1)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["staleness_threshold_hours"] == 2
        by_id = {c["camera_source_id"]: c for c in body["cameras"]}
        assert by_id["cfg-at"]["stale"] is False
        assert by_id["cfg-past"]["stale"] is True

    def test_never_reported_entry_is_not_stale(self, camera_env, env,
                                               frozen_now):
        """Portal-created pending entries carry no last-reported timestamp;
        staleness does not apply to them (Req 4.1)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=frozen_now)
        put_camera(camera_env, device_id, usecase_id, "portal-p",
                   origin="portal-created", sync_status="pending")

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["cameras"][0]["last_reported_at"] is None
        assert body["cameras"][0]["stale"] is False


# ===========================================================================
# 4. Disconnected-status pass-through (Req 4.2)
# ===========================================================================

class FakeGreengrassClient:
    def __init__(self, status=None, error=None):
        self.status = status
        self.error = error
        self.calls = []

    def get_core_device(self, coreDeviceThingName):
        self.calls.append(coreDeviceThingName)
        if self.error:
            raise self.error
        return {"status": self.status}


class TestDeviceStatusPassThrough:

    @pytest.fixture
    def greengrass(self, camera_env, monkeypatch):
        """Route the existing device-status lookup at a fake Greengrass
        client (assumed-role chain stubbed at the module seams)."""
        client = FakeGreengrassClient(status="DISCONNECTED")
        monkeypatch.setattr(camera_env.module, "get_usecase", lambda uid: {
            "usecase_id": uid,
            "cross_account_role_arn":
                "arn:aws:iam::123456789012:role/test-usecase-role",
            "external_id": "test-external-id",
            "region": REGION,
        })
        monkeypatch.setattr(camera_env.module, "assume_cross_account_role",
                            lambda arn, ext: {"AccessKeyId": "AK",
                                              "SecretAccessKey": "SK",
                                              "SessionToken": "ST"})
        monkeypatch.setattr(camera_env.module, "create_boto3_client",
                            lambda service, creds, region: client)
        return client

    def test_disconnected_status_surfaces_in_the_response(
            self, camera_env, env, greengrass):
        """The Greengrass core-device status passes through to the
        cameras response unchanged (Req 4.2)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-1",
                   last_reported_at=now_ms())

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["device_status"] == "DISCONNECTED"
        # The lookup targeted this device's thing name.
        assert greengrass.calls == [device_id]
        # The inventory itself is unaffected by connectivity (Req 4.2:
        # status is indicated ALONGSIDE the inventory).
        assert body["count"] == 1

    def test_lookup_failure_degrades_to_unknown(self, camera_env, env,
                                                greengrass):
        """A failing status lookup never fails the request — the response
        carries UNKNOWN and the inventory stays readable (Req 4.2)."""
        greengrass.error = RuntimeError("core device lookup failed")
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-1",
                   last_reported_at=now_ms())

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["device_status"] == "UNKNOWN"
        assert body["count"] == 1


# ===========================================================================
# 5. Settings default fallback (Req 4.3)
# ===========================================================================

class TestStalenessSettingFallback:

    @pytest.mark.parametrize("stored", [0, -3, "garbage"])
    def test_invalid_stored_values_fall_back_to_default(
            self, camera_env, env, stored):
        """Non-positive or unparseable stored thresholds keep the route on
        the default 24 hours (Req 4.3)."""
        camera_env.settings.put_item(Item={
            "setting_key": STALENESS_SETTING_KEY, "value": stored})
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["staleness_threshold_hours"] == 24


# ===========================================================================
# 6. Never-synced response shape (Req 1.6)
# ===========================================================================

class TestNeverSyncedShape:

    def test_never_synced_meta_with_pending_portal_entries(
            self, camera_env, env):
        """A device whose META still says never_synced reports the explicit
        never-synced state while listing queued portal-created entries so
        operators see what they staged (Req 1.6)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=None,
                 never_synced=True)
        put_camera(camera_env, device_id, usecase_id, "portal-q",
                   origin="portal-created", sync_status="pending")

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        assert body["state"] == "never-synced"
        assert body["last_report_at"] is None
        assert body["count"] == 1
        assert body["cameras"][0]["camera_source_id"] == "portal-q"
        assert body["cameras"][0]["sync_status"] == "pending"


# ===========================================================================
# 7. Absent display fields at boundary detail (Req 4.4)
# ===========================================================================

class TestAbsentDisplayFields:

    def test_present_entry_never_carries_absent_since(self, camera_env, env):
        """absent_since is a display field of ABSENT entries only; a
        present entry omits it even when the attribute lingers (Req 4.4)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        now = now_ms()
        put_meta(camera_env, device_id, usecase_id, last_report_at=now)
        put_camera(camera_env, device_id, usecase_id, "disc-back",
                   origin="edge-discovered", last_reported_at=now,
                   absent=False, absent_since=now - HOUR_MS)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        camera = body["cameras"][0]
        assert camera["absent"] is False
        assert "absent_since" not in camera

    def test_absent_entry_without_timestamp_omits_absent_since(
            self, camera_env, env):
        """An absent entry with no recorded absence timestamp shows
        absent=true without a fabricated absent_since (Req 4.4)."""
        user = env.make_user(role="Viewer")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        now = now_ms()
        put_meta(camera_env, device_id, usecase_id, last_report_at=now)
        put_camera(camera_env, device_id, usecase_id, "disc-gone",
                   origin="edge-discovered", last_reported_at=now,
                   absent=True)

        status, body = invoke(camera_env, "GET", device_id, user)

        assert status == 200
        camera = body["cameras"][0]
        assert camera["absent"] is True
        assert "absent_since" not in camera


# ===========================================================================
# 8. Conflict re-apply edge cases (Req 6.4)
# ===========================================================================

class TestConflictReapplyEdgeCases:

    def test_deletion_retained_conflict_reapplies_as_create(
            self, camera_env, env, fake_shadow):
        """Re-applying the portal version of a deletion-retained conflict
        (entry gone) re-creates the source as a new pending change
        (Reqs 6.4, 6.5 aftermath)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        cid = put_conflict(camera_env, device_id, usecase_id, "cfg-gone",
                           {"op": "update", "name": "portal name",
                            "type": "Camera",
                            "params": {"devicePath": "/dev/video3"}})

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path=f"/conflicts/{cid}/reapply")

        assert status == 200
        change = (fake_shadow.updates[0]["payload"]["state"]["desired"]
                  ["changes"]["cfg-gone"])
        # The deleted entry cannot be updated: the portal version is
        # re-issued as a create with no baseVersion.
        assert change["op"] == "create"
        assert "baseVersion" not in change
        assert change["name"] == "portal name"

        item = get_camera_item(camera_env, device_id, "cfg-gone")
        assert item["sync_status"] == "pending"
        assert item["origin"] == "portal-created"
        assert item["pending_content"]["op"] == "create"

    def test_reapply_of_effective_deletion_is_conflict_409(
            self, camera_env, env, fake_shadow):
        """A portal DELETE that already became effective (entry gone)
        cannot be re-applied — 409 with no shadow write (Req 6.4)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        cid = put_conflict(camera_env, device_id, usecase_id, "cfg-del",
                           {"op": "delete"})

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path=f"/conflicts/{cid}/reapply")

        assert status == 409
        assert "already effective" in body["error"]
        assert fake_shadow.updates == []

    def test_conflict_without_portal_version_is_400(
            self, camera_env, env, fake_shadow):
        """A conflict event carrying no portal version has nothing to
        re-apply (Req 6.4)."""
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        cid = put_conflict(camera_env, device_id, usecase_id, "cfg-x",
                           portal_version=None)

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path=f"/conflicts/{cid}/reapply")

        assert status == 400
        assert fake_shadow.updates == []


# ===========================================================================
# 9. Audit event payload per mutating route (Reqs 12.2, 12.3)
#    (create_camera_source is asserted by the mutation-routes suite)
# ===========================================================================

class TestMutatingAuditPayloads:

    def _assert_payload(self, events, device_id, csid, usecase_id,
                        portal_change_id):
        assert len(events) == 1
        event = events[0]
        assert event["result"] == "success"
        assert event["resource_type"] == "camera_registry"
        assert event["resource_id"] == device_id
        assert event["timestamp"] > 0
        details = event["details"]
        assert details["device_id"] == device_id
        assert details["camera_source_id"] == csid
        assert details["usecase_id"] == usecase_id
        assert details["portal_change_id"] == portal_change_id

    def test_update_audit_payload(self, aws_stack, camera_env, env,
                                  fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-u",
                   last_reported_at=now_ms())

        status, body = invoke(
            camera_env, "PUT", device_id, user, sub_path="/cfg-u",
            body={"name": "renamed", "type": "Camera"})

        assert status == 200
        self._assert_payload(
            audit_events(aws_stack, user, "update_camera_source"),
            device_id, "cfg-u", usecase_id, body["portal_change_id"])

    def test_delete_audit_payload(self, aws_stack, camera_env, env,
                                  fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-d",
                   last_reported_at=now_ms())

        status, body = invoke(camera_env, "DELETE", device_id, user,
                              sub_path="/cfg-d")

        assert status == 200
        self._assert_payload(
            audit_events(aws_stack, user, "delete_camera_source"),
            device_id, "cfg-d", usecase_id, body["portal_change_id"])

    def test_reapply_audit_payload_carries_conflict_id(
            self, aws_stack, camera_env, env, fake_shadow):
        user = env.make_user(role="Operator")
        usecase_id = env.create_usecase()
        device_id = f"thing-{uuid.uuid4()}"
        put_meta(camera_env, device_id, usecase_id, last_report_at=now_ms())
        put_camera(camera_env, device_id, usecase_id, "cfg-r",
                   last_reported_at=now_ms())
        cid = put_conflict(camera_env, device_id, usecase_id, "cfg-r",
                           {"op": "update", "name": "portal name",
                            "type": "Camera", "params": {}})

        status, body = invoke(camera_env, "POST", device_id, user,
                              sub_path=f"/conflicts/{cid}/reapply")

        assert status == 200
        events = audit_events(aws_stack, user, "reapply_camera_conflict")
        self._assert_payload(events, device_id, "cfg-r", usecase_id,
                             body["portal_change_id"])
        assert events[0]["details"]["conflict_id"] == cid
