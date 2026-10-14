"""
Camera_Registry Staleness_Threshold settings API tests
(camera-registry-sync Requirement 4.3).

Task 6.5 (spec: camera-registry-sync).

The Staleness_Threshold rides the existing PortalAdmin-only
/data-accounts/{id} settings routes with the reserved id
'camera-registry-configuration' (the same carrier as
'bedrock-configuration'; no new API Gateway routes), handled by
data_accounts.py. These tests cover:

1. Authorization: only PortalAdmin may read or write; every other role
   gets 403 and nothing is persisted.
2. Reads return the effective value (default 24 before anything is
   stored, the stored value afterwards).
3. Writes validate a positive number of hours and persist the exact item
   shape read by camera_registry.staleness_threshold_hours():
       {setting_key: 'camera_registry.staleness_threshold_hours',
        value: <hours>}
   so the cameras route picks the value up.
4. Successful updates write an audit record.

Runs against the shared moto stack from conftest.py.

_Requirements: 4.3_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-camera-registry-settings-api"
RESOURCE_ID = "camera-registry-configuration"
SETTING_KEY = "camera_registry.staleness_threshold_hours"


@pytest.fixture(scope="module")
def settings_env(aws_stack):
    """Settings table + freshly imported data_accounts and camera_registry
    modules inside moto (conftest re-import pattern)."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so both modules bind SETTINGS_TABLE_NAME and
    # moto-intercepted boto3 resources.
    for module_name in ("data_accounts", "camera_registry"):
        sys.modules.pop(module_name, None)
    import data_accounts
    import camera_registry

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        data_accounts=data_accounts,
        camera_registry=camera_registry,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_setting(settings_env):
    """Each test starts with no stored staleness threshold item."""
    settings_env.settings_table.delete_item(Key={"setting_key": SETTING_KEY})
    yield


def make_user(role):
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": role}


def invoke(settings_env, method, user, body=None):
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}",
        "path": f"/data-accounts/{RESOURCE_ID}",
        "pathParameters": {"id": RESOURCE_ID},
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
    response = settings_env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def stored_item(settings_env):
    return settings_env.settings_table.get_item(
        Key={"setting_key": SETTING_KEY}).get("Item")


# ===========================================================================
# 1. Authorization (Requirement 4.3: PortalAdmin only)
# ===========================================================================

class TestCameraRegistrySettingAuthz:

    @pytest.mark.parametrize("role", ["Viewer", "Operator", "DataScientist",
                                      "UseCaseAdmin"])
    @pytest.mark.parametrize("method", ["GET", "PUT"])
    def test_non_portal_admin_is_denied(self, settings_env, role, method):
        """Every non-PortalAdmin role gets 403 on read and write and no
        setting is persisted (Requirement 4.3)."""
        user = make_user(role)
        status, payload = invoke(settings_env, method, user,
                                 body={"staleness_threshold_hours": 12})
        assert status == 403
        assert "PortalAdmin" in payload["error"]
        assert stored_item(settings_env) is None

    def test_portal_admin_is_allowed(self, settings_env):
        """PortalAdmin can read and write (Requirement 4.3)."""
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "GET", admin)
        assert status == 200
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"staleness_threshold_hours": 12})
        assert status == 200


# ===========================================================================
# 2. Reads (default and stored values)
# ===========================================================================

class TestCameraRegistrySettingRead:

    def test_read_returns_default_24_when_nothing_stored(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload["staleness_threshold_hours"] == 24
        assert payload["default_staleness_threshold_hours"] == 24

    def test_read_returns_stored_value(self, settings_env):
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"staleness_threshold_hours": 48})
        assert status == 200
        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload["staleness_threshold_hours"] == 48
        assert payload["default_staleness_threshold_hours"] == 24


# ===========================================================================
# 3. Writes: persisted shape (readable by the cameras route) and validation
# ===========================================================================

class TestCameraRegistrySettingWrite:

    def test_write_persists_the_shape_the_cameras_route_reads(self, settings_env):
        """The stored item is {setting_key, value} - exactly what
        camera_registry.staleness_threshold_hours() expects - and the
        cameras-route reader returns the updated value (Requirement 4.3)."""
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"staleness_threshold_hours": 6})
        assert status == 200

        item = stored_item(settings_env)
        assert item["setting_key"] == SETTING_KEY
        assert float(item["value"]) == 6
        assert item["updated_by"] == admin["user_id"]

        # Readable by the cameras route (Requirement 4.3).
        assert settings_env.camera_registry.staleness_threshold_hours() == 6

    def test_fractional_hours_are_accepted(self, settings_env):
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"staleness_threshold_hours": 0.5})
        assert status == 200
        assert settings_env.camera_registry.staleness_threshold_hours() == 0.5

    @pytest.mark.parametrize("hours", [0, -1, -0.5, "many", None, True, [24]])
    def test_invalid_values_are_rejected(self, settings_env, hours):
        """The threshold must be a positive number of hours; anything else
        is rejected with 400 and nothing is persisted."""
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"staleness_threshold_hours": hours})
        assert status == 400
        assert any("staleness_threshold_hours" in e
                   for e in payload["validation_errors"])
        assert stored_item(settings_env) is None
        # The readers keep serving the default.
        assert settings_env.camera_registry.staleness_threshold_hours() == 24

    def test_missing_key_is_rejected(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin, body={})
        assert status == 400
        assert stored_item(settings_env) is None


# ===========================================================================
# 4. Audit records
# ===========================================================================

@pytest.fixture
def audit_table(aws_stack):
    """The moto-backed audit log table shared_utils.log_audit_event writes to."""
    import boto3
    from conftest import TEST_ENV

    return boto3.resource("dynamodb", region_name=REGION).Table(
        TEST_ENV["AUDIT_LOG_TABLE"])


class TestCameraRegistrySettingAudit:

    def test_update_writes_audit_record(self, settings_env, audit_table):
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"staleness_threshold_hours": 36})
        assert status == 200

        records = [i for i in audit_table.scan()["Items"]
                   if i["user_id"] == admin["user_id"]
                   and i["action"] == "update_camera_registry_configuration"]
        assert len(records) == 1
        assert records[0]["resource_id"] == SETTING_KEY
        assert records[0]["result"] == "success"
        assert float(records[0]["details"]["staleness_threshold_hours"]) == 36
