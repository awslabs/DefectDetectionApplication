"""
Images per auto-label worker run: Settings API tests
(Settings -> Auto-Labeling).

The setting rides the existing PortalAdmin-only /data-accounts/{id}
settings routes with the reserved id 'autolabel-configuration' (the same
carrier as 'bedrock-configuration' and 'camera-registry-configuration';
no new API Gateway route), handled by data_accounts.py. These tests
cover:

1. Authorization: only PortalAdmin may read or write; every other role
   gets 403, nothing is stored and the denied attempt is audit-logged.
2. Reads: null with nothing stored, the stored value after a save, with
   the default 5 and the bounds 1 to 5. A stored value the worker would
   ignore reads back as null.
3. Writes: 1 to 5 are stored as {setting_key: 'autolabel.images_per_run',
   value, updated_at, updated_by}; null deletes the item; anything else,
   a missing key and a malformed body get 400 and change nothing.
4. Audit: a successful save writes one update_autolabel_configuration
   record.
5. Round trip: a value saved through the API is the value
   dda_autolabel_worker reads at the start of a run.

Runs against the shared moto stack from conftest.py. The module fixture
sets os.environ['SETTINGS_TABLE'] for the re-imports and puts the
previous value back at teardown, and every test deletes the item before
and after it runs, so no other module's worker tests see a saved value.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

SETTINGS_TABLE_NAME = "test-settings-autolabel-settings-api"
RESOURCE_ID = "autolabel-configuration"
SETTING_KEY = "autolabel.images_per_run"
EXPECTED_ERROR = ("images_per_run must be a whole number from 1 to 5, "
                  "or null to use the default (5)")
INVALID_ERROR_BODY = {
    "error": "Invalid auto-labeling configuration",
    "validation_errors": [EXPECTED_ERROR],
}
NON_ADMIN_ROLES = ["Viewer", "Operator", "DataScientist", "UseCaseAdmin"]


@pytest.fixture(scope="module")
def settings_env(aws_stack):
    """Settings table + freshly imported data_accounts and
    dda_autolabel_worker modules inside moto (conftest re-import
    pattern), both bound to SETTINGS_TABLE_NAME."""
    import boto3

    previous = os.environ.get("SETTINGS_TABLE")
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
    for module_name in ("data_accounts", "dda_autolabel_worker"):
        sys.modules.pop(module_name, None)
    import data_accounts
    import dda_autolabel_worker

    resource = boto3.resource("dynamodb", region_name=REGION)
    try:
        yield SimpleNamespace(
            data_accounts=data_accounts,
            worker=dda_autolabel_worker,
            settings_table=resource.Table(SETTINGS_TABLE_NAME),
        )
    finally:
        if previous is None:
            os.environ.pop("SETTINGS_TABLE", None)
        else:
            os.environ["SETTINGS_TABLE"] = previous


@pytest.fixture(autouse=True)
def clean_setting(settings_env):
    """Each test starts and ends with no stored images per run item."""
    settings_env.settings_table.delete_item(Key={"setting_key": SETTING_KEY})
    yield
    settings_env.settings_table.delete_item(Key={"setting_key": SETTING_KEY})


@pytest.fixture
def audit_table(aws_stack):
    """The moto-backed audit log table shared_utils.log_audit_event writes to."""
    import boto3

    return boto3.resource("dynamodb", region_name=REGION).Table(
        TEST_ENV["AUDIT_LOG_TABLE"])


def make_user(role):
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": role}


def invoke(settings_env, method, user, body=None, raw_body=None):
    """Call the handler; `raw_body` is sent as is (for malformed JSON)."""
    if raw_body is None:
        raw_body = json.dumps(body) if body is not None else None
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}",
        "path": f"/data-accounts/{RESOURCE_ID}",
        "pathParameters": {"id": RESOURCE_ID},
        "body": raw_body,
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


def audit_records(audit_table, user, action):
    return [item for item in audit_table.scan()["Items"]
            if item["user_id"] == user["user_id"]
            and item["action"] == action]


# ===========================================================================
# 1. Authorization: PortalAdmin only
# ===========================================================================

class TestAutolabelSettingAuthz:

    @pytest.mark.parametrize("role", NON_ADMIN_ROLES)
    @pytest.mark.parametrize("method", ["GET", "PUT"])
    def test_non_portal_admin_is_denied(self, settings_env, audit_table,
                                        role, method):
        """Every non-PortalAdmin role gets 403 on read and write, nothing
        is stored, and the attempt is audit-logged as denied."""
        user = make_user(role)
        status, payload = invoke(settings_env, method, user,
                                 body={"images_per_run": 2})
        assert status == 403
        assert "PortalAdmin" in payload["error"]
        assert stored_item(settings_env) is None

        denied = audit_records(audit_table, user, "unauthorized_access")
        assert len(denied) == 1
        assert denied[0]["resource_id"] == SETTING_KEY
        assert denied[0]["resource_type"] == "setting"
        assert denied[0]["result"] == "denied"
        assert denied[0]["details"]["method"] == method

    def test_other_methods_are_not_found(self, settings_env):
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "DELETE", admin)
        assert status == 404
        assert stored_item(settings_env) is None


# ===========================================================================
# 2. Reads
# ===========================================================================

class TestAutolabelSettingRead:

    def test_nothing_stored_reads_null_with_default_and_bounds(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload == {
            "images_per_run": None,
            "default_images_per_run": 5,
            "min_images_per_run": 1,
            "max_images_per_run": 5,
        }

    def test_value_the_worker_would_ignore_reads_null(self, settings_env):
        """A value outside 1 to 5 stored directly reads back as null, the
        same rule as the worker."""
        settings_env.settings_table.put_item(Item={
            "setting_key": SETTING_KEY, "value": 9})
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload["images_per_run"] is None
        assert settings_env.data_accounts.read_stored_images_per_run() is None


# ===========================================================================
# 3. Writes
# ===========================================================================

class TestAutolabelSettingWrite:

    @pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
    def test_save_then_reload(self, settings_env, value):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"images_per_run": value})
        assert status == 200
        assert payload == {
            "message": "Auto-labeling configuration updated successfully",
            "images_per_run": value,
        }

        item = stored_item(settings_env)
        assert item["setting_key"] == SETTING_KEY
        assert item["value"] == value
        assert item["updated_by"] == admin["user_id"]
        assert isinstance(item["updated_at"], Decimal)

        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload["images_per_run"] == value

    def test_null_after_a_save_deletes_the_item(self, settings_env):
        admin = make_user("PortalAdmin")
        invoke(settings_env, "PUT", admin, body={"images_per_run": 3})
        assert stored_item(settings_env) is not None

        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"images_per_run": None})
        assert status == 200
        assert payload["images_per_run"] is None
        assert stored_item(settings_env) is None

        status, payload = invoke(settings_env, "GET", admin)
        assert status == 200
        assert payload["images_per_run"] is None

    def test_null_with_nothing_stored_stores_nothing(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"images_per_run": None})
        assert status == 200
        assert payload["images_per_run"] is None
        assert stored_item(settings_env) is None

    @pytest.mark.parametrize("value", [0, 6, -1, 2.5, "3", True, [3], {"n": 3}])
    def test_invalid_values_are_rejected(self, settings_env, value):
        """400 with the exact error; nothing is stored, and an earlier
        saved value stays."""
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"images_per_run": value})
        assert status == 400
        assert payload == INVALID_ERROR_BODY
        assert stored_item(settings_env) is None

        invoke(settings_env, "PUT", admin, body={"images_per_run": 4})
        status, payload = invoke(settings_env, "PUT", admin,
                                 body={"images_per_run": value})
        assert status == 400
        assert payload == INVALID_ERROR_BODY
        assert stored_item(settings_env)["value"] == 4

    def test_missing_key_is_rejected(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin, body={})
        assert status == 400
        assert payload == {
            "error": "Invalid auto-labeling configuration",
            "validation_errors": ["images_per_run is required: " + EXPECTED_ERROR],
        }
        assert stored_item(settings_env) is None

    def test_non_object_body_is_rejected(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin, body=[3])
        assert status == 400
        assert payload == {"error": "Request body must be a JSON object"}
        assert stored_item(settings_env) is None

    def test_invalid_json_is_rejected(self, settings_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(settings_env, "PUT", admin,
                                 raw_body="{not json")
        assert status == 400
        assert payload == {"error": "Request body is not valid JSON"}
        assert stored_item(settings_env) is None


# ===========================================================================
# 4. Audit records
# ===========================================================================

class TestAutolabelSettingAudit:

    def test_save_writes_one_audit_record(self, settings_env, audit_table):
        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"images_per_run": 2})
        assert status == 200

        records = audit_records(audit_table, admin,
                                "update_autolabel_configuration")
        assert len(records) == 1
        assert records[0]["resource_type"] == "setting"
        assert records[0]["resource_id"] == SETTING_KEY
        assert records[0]["result"] == "success"
        assert records[0]["details"]["images_per_run"] == 2


# ===========================================================================
# 5. Round trip to the worker
# ===========================================================================

class TestAutolabelSettingRoundTrip:

    def test_saved_value_is_what_the_worker_reads(self, settings_env):
        assert settings_env.worker.SETTINGS_TABLE == SETTINGS_TABLE_NAME
        assert settings_env.worker._images_per_run() == (None, "default")

        admin = make_user("PortalAdmin")
        status, _ = invoke(settings_env, "PUT", admin,
                           body={"images_per_run": 2})
        assert status == 200
        assert settings_env.worker._images_per_run() == (2, "settings")

        status, _ = invoke(settings_env, "PUT", admin,
                           body={"images_per_run": None})
        assert status == 200
        assert settings_env.worker._images_per_run() == (None, "default")
