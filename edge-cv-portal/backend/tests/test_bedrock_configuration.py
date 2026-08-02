"""
Bedrock_Configuration settings API tests (workflow-manager Requirement 10.6).

Task 10.2 (spec: workflow-manager).

The Bedrock_Configuration read/write API rides the existing
PortalAdmin-only /data-accounts/{id} routes with the reserved id
'bedrock-configuration' (no new API Gateway routes may be added), handled
by data_accounts.py. These tests cover:

1. Authorization: only PortalAdmin (Permission.BEDROCK_CONFIG_WRITE) may
   read or write; every other role gets 403 and a denied audit record.
2. Reads return the effective configuration (defaults before anything is
   stored, stored values afterwards).
3. Writes validate inputs (timeout <= 60, temperature/top_p in [0, 1],
   non-empty model id, positive max_tokens) and persist the exact item
   shape read by workflow_generator.get_bedrock_configuration():
       {setting_key: 'bedrock_configuration',
        value: {model_id, region, max_tokens, temperature, top_p,
                timeout_seconds}}
4. Successful updates write an audit record.

Runs against the shared moto stack from conftest.py.

_Requirements: 10.6_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-bedrock"
RESOURCE_ID = "bedrock-configuration"

VALID_CONFIG = {
    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "region": "us-west-2",
    "max_tokens": 2048,
    "temperature": 0.5,
    "top_p": 0.8,
    "timeout_seconds": 45,
}


@pytest.fixture(scope="module")
def bedrock_env(aws_stack):
    """Settings table + freshly imported data_accounts module inside moto."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds SETTINGS_TABLE and a moto-intercepted
    # boto3 resource (conftest pattern).
    sys.modules.pop("data_accounts", None)
    import data_accounts

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        data_accounts=data_accounts,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_setting(bedrock_env):
    """Each test starts with no stored bedrock_configuration item."""
    bedrock_env.settings_table.delete_item(Key={"setting_key": "bedrock_configuration"})
    yield


def make_user(role):
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": role}


def invoke(bedrock_env, method, user, body=None):
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
    response = bedrock_env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def stored_item(bedrock_env):
    return bedrock_env.settings_table.get_item(
        Key={"setting_key": "bedrock_configuration"}).get("Item")


@pytest.fixture
def audit_table(aws_stack):
    """The moto-backed audit log table shared_utils.log_audit_event writes to."""
    import boto3
    from conftest import TEST_ENV

    return boto3.resource("dynamodb", region_name=REGION).Table(
        TEST_ENV["AUDIT_LOG_TABLE"])


# ===========================================================================
# 1. Authorization (Requirement 10.6: PortalAdmin only)
# ===========================================================================

class TestBedrockConfigurationAuthz:

    @pytest.mark.parametrize("role", ["Viewer", "Operator", "DataScientist",
                                      "UseCaseAdmin"])
    @pytest.mark.parametrize("method", ["GET", "PUT"])
    def test_non_portal_admin_is_denied(self, bedrock_env, role, method):
        """Every non-PortalAdmin role gets 403 on read and write and no
        configuration is persisted (Requirement 10.6)."""
        user = make_user(role)
        status, payload = invoke(bedrock_env, method, user, body=VALID_CONFIG)
        assert status == 403
        assert "bedrock-config:write" in payload["required_permissions"]
        assert stored_item(bedrock_env) is None

    def test_portal_admin_is_allowed(self, bedrock_env):
        """PortalAdmin holds bedrock-config:write and can read and write
        (Requirement 10.6)."""
        admin = make_user("PortalAdmin")
        status, _ = invoke(bedrock_env, "GET", admin)
        assert status == 200
        status, _ = invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)
        assert status == 200


# ===========================================================================
# 2. Reads (defaults and stored values)
# ===========================================================================

class TestBedrockConfigurationRead:

    def test_read_returns_defaults_when_nothing_stored(self, bedrock_env):
        admin = make_user("PortalAdmin")
        status, payload = invoke(bedrock_env, "GET", admin)
        assert status == 200
        config = payload["bedrock_configuration"]
        assert config["model_id"]
        assert config["timeout_seconds"] == 60
        assert set(config) == {"model_id", "region", "max_tokens",
                               "temperature", "top_p", "timeout_seconds"}

    def test_read_returns_stored_values(self, bedrock_env):
        admin = make_user("PortalAdmin")
        status, _ = invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)
        assert status == 200
        status, payload = invoke(bedrock_env, "GET", admin)
        assert status == 200
        assert payload["bedrock_configuration"] == VALID_CONFIG


# ===========================================================================
# 3. Writes: persisted shape and input validation
# ===========================================================================

class TestBedrockConfigurationWrite:

    def test_write_persists_the_shape_workflow_generator_reads(self, bedrock_env):
        """The stored item is {setting_key, value: {...}} with all six keys
        - exactly what workflow_generator.get_bedrock_configuration()
        expects (Requirement 10.6)."""
        admin = make_user("PortalAdmin")
        status, _ = invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)
        assert status == 200

        item = stored_item(bedrock_env)
        assert item["setting_key"] == "bedrock_configuration"
        value = item["value"]
        assert set(value) == {"model_id", "region", "max_tokens",
                              "temperature", "top_p", "timeout_seconds"}
        assert value["model_id"] == VALID_CONFIG["model_id"]
        assert value["region"] == VALID_CONFIG["region"]
        assert int(value["max_tokens"]) == 2048
        assert float(value["temperature"]) == 0.5
        assert float(value["top_p"]) == 0.8
        assert int(value["timeout_seconds"]) == 45

    def test_partial_update_merges_over_current_values(self, bedrock_env):
        admin = make_user("PortalAdmin")
        invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)
        status, payload = invoke(bedrock_env, "PUT", admin,
                                 body={"temperature": 0.9})
        assert status == 200
        config = payload["bedrock_configuration"]
        assert config["temperature"] == 0.9
        assert config["model_id"] == VALID_CONFIG["model_id"]
        assert config["timeout_seconds"] == VALID_CONFIG["timeout_seconds"]

    @pytest.mark.parametrize("overrides,expected_error", [
        ({"timeout_seconds": 61}, "timeout_seconds"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"temperature": 1.5}, "temperature"),
        ({"temperature": -0.1}, "temperature"),
        ({"top_p": 2}, "top_p"),
        ({"model_id": "   "}, "model_id"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": "many"}, "max_tokens"),
    ])
    def test_invalid_values_are_rejected(self, bedrock_env, overrides,
                                         expected_error):
        """Timeout <= 60, temperature/top_p in [0, 1], non-empty model id,
        positive integer max_tokens (task 10.2 validation rules)."""
        admin = make_user("PortalAdmin")
        body = dict(VALID_CONFIG, **overrides)
        status, payload = invoke(bedrock_env, "PUT", admin, body=body)
        assert status == 400
        assert any(expected_error in e for e in payload["validation_errors"])
        assert stored_item(bedrock_env) is None


# ===========================================================================
# 4. Audit records
# ===========================================================================

class TestBedrockConfigurationAudit:

    def test_update_writes_audit_record(self, bedrock_env, audit_table):
        admin = make_user("PortalAdmin")
        status, _ = invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)
        assert status == 200

        records = [i for i in audit_table.scan()["Items"]
                   if i["user_id"] == admin["user_id"]
                   and i["action"] == "update_bedrock_configuration"]
        assert len(records) == 1
        assert records[0]["resource_id"] == "bedrock_configuration"
        assert records[0]["result"] == "success"
        assert int(records[0]["timestamp"]) > 0

    def test_denied_attempt_writes_unauthorized_access_record(
            self, bedrock_env, audit_table):
        viewer = make_user("Viewer")
        status, _ = invoke(bedrock_env, "PUT", viewer, body=VALID_CONFIG)
        assert status == 403

        records = [i for i in audit_table.scan()["Items"]
                   if i["user_id"] == viewer["user_id"]
                   and i["action"] == "unauthorized_access"]
        assert len(records) == 1
        assert records[0]["result"] == "denied"
        assert "bedrock-config:write" in records[0]["details"]["required_permissions"]


# ===========================================================================
# 5. Model options for the settings dropdown
#    GET /data-accounts/bedrock-configuration/models
# ===========================================================================

def invoke_models(bedrock_env, user, query=None):
    """GET /data-accounts/bedrock-configuration/models; (status, body)."""
    event = {
        "httpMethod": "GET",
        "resource": "/data-accounts/{id}/models",
        "path": f"/data-accounts/{RESOURCE_ID}/models",
        "pathParameters": {"id": RESOURCE_ID},
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
    response = bedrock_env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


class FakeBedrockControlClient:
    """Stand-in for the bedrock control-plane client (list APIs)."""

    def __init__(self, profiles=None, models=None,
                 profiles_error=None, models_error=None):
        self.profiles = profiles or []
        self.models = models or []
        self.profiles_error = profiles_error
        self.models_error = models_error

    def list_inference_profiles(self, **kwargs):
        if self.profiles_error:
            raise self.profiles_error
        return {"inferenceProfileSummaries": self.profiles}

    def list_foundation_models(self, **kwargs):
        if self.models_error:
            raise self.models_error
        return {"modelSummaries": self.models}


def access_denied_error(operation):
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        operation,
    )


@pytest.fixture
def fake_bedrock(bedrock_env, monkeypatch):
    """Injects a FakeBedrockControlClient; records the requested region."""
    state = SimpleNamespace(client=FakeBedrockControlClient(), regions=[])

    def fake_get_client(region):
        state.regions.append(region)
        return state.client

    monkeypatch.setattr(bedrock_env.data_accounts,
                        "_get_bedrock_control_client", fake_get_client)
    return state


PROFILES = [
    {"inferenceProfileId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "inferenceProfileName": "US Anthropic Claude Sonnet 4.5"},
    {"inferenceProfileId": "us.amazon.nova-pro-v1:0",
     "inferenceProfileName": "US Amazon Nova Pro"},
]

FOUNDATION_MODELS = [
    # Fronted by a listed inference profile: must be deduplicated away.
    {"modelId": "anthropic.claude-sonnet-4-5-20250929-v1:0",
     "modelName": "Claude Sonnet 4.5",
     "modelLifecycle": {"status": "ACTIVE"},
     "inferenceTypesSupported": ["INFERENCE_PROFILE"]},
    # Plain ON_DEMAND foundation model: included.
    {"modelId": "amazon.titan-text-express-v1",
     "modelName": "Titan Text Express",
     "modelLifecycle": {"status": "ACTIVE"},
     "inferenceTypesSupported": ["ON_DEMAND"]},
    # Legacy/EOL model: excluded.
    {"modelId": "anthropic.claude-v2",
     "modelName": "Claude 2",
     "modelLifecycle": {"status": "LEGACY"},
     "inferenceTypesSupported": ["ON_DEMAND"]},
    # Not invokable on demand (profile-only, no profile listed): excluded.
    {"modelId": "meta.llama4-maverick-17b-instruct-v1:0",
     "modelName": "Llama 4 Maverick",
     "modelLifecycle": {"status": "ACTIVE"},
     "inferenceTypesSupported": ["INFERENCE_PROFILE"]},
]


class TestBedrockModelOptions:

    @pytest.mark.parametrize("role", ["Viewer", "Operator", "DataScientist",
                                      "UseCaseAdmin"])
    def test_non_portal_admin_is_denied(self, bedrock_env, fake_bedrock, role):
        """The models listing is gated exactly like the configuration GET:
        PortalAdmin only (Requirement 10.6)."""
        status, payload = invoke_models(bedrock_env, make_user(role))
        assert status == 403
        assert "bedrock-config:write" in payload["required_permissions"]
        assert fake_bedrock.regions == []

    def test_returns_deduplicated_invokable_options(self, bedrock_env,
                                                    fake_bedrock):
        """Inference profiles and ACTIVE ON_DEMAND foundation models are
        returned as {id, label}; profiles win over the foundation models
        they front; non-ON_DEMAND and non-ACTIVE models are excluded;
        anthropic sorts first."""
        fake_bedrock.client = FakeBedrockControlClient(
            profiles=PROFILES, models=FOUNDATION_MODELS)

        status, payload = invoke_models(bedrock_env, make_user("PortalAdmin"))
        assert status == 200
        assert "permissions" not in payload

        ids = [m["id"] for m in payload["models"]]
        assert ids == [
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",  # anthropic first
            "amazon.titan-text-express-v1",
            "us.amazon.nova-pro-v1:0",
        ]
        assert payload["models"][0]["label"] == "US Anthropic Claude Sonnet 4.5"
        # Deduplicated: the profile-fronted foundation model id is absent.
        assert "anthropic.claude-sonnet-4-5-20250929-v1:0" not in ids
        # Excluded: LEGACY lifecycle and profile-only inference types.
        assert "anthropic.claude-v2" not in ids
        assert "meta.llama4-maverick-17b-instruct-v1:0" not in ids

    def test_uses_configured_region_with_query_override(self, bedrock_env,
                                                        fake_bedrock):
        """The listing targets the stored Bedrock_Configuration region;
        ?region=... overrides it."""
        admin = make_user("PortalAdmin")
        invoke(bedrock_env, "PUT", admin, body=VALID_CONFIG)  # us-west-2

        status, payload = invoke_models(bedrock_env, admin)
        assert status == 200
        assert payload["region"] == "us-west-2"
        assert fake_bedrock.regions[-1] == "us-west-2"

        status, payload = invoke_models(bedrock_env, admin,
                                        query={"region": "eu-central-1"})
        assert status == 200
        assert payload["region"] == "eu-central-1"
        assert fake_bedrock.regions[-1] == "eu-central-1"

    def test_access_denied_returns_empty_list_with_permissions_hint(
            self, bedrock_env, fake_bedrock):
        """When the Lambda lacks the bedrock list permissions, the endpoint
        degrades gracefully: 200 with an empty list and a 'permissions'
        hint so the UI falls back to free-text entry."""
        fake_bedrock.client = FakeBedrockControlClient(
            profiles_error=access_denied_error("ListInferenceProfiles"),
            models_error=access_denied_error("ListFoundationModels"),
        )

        status, payload = invoke_models(bedrock_env, make_user("PortalAdmin"))
        assert status == 200
        assert payload["models"] == []
        assert "permissions" in payload

    def test_partial_access_denied_still_returns_available_options(
            self, bedrock_env, fake_bedrock):
        """If only one list call is denied the other's results are still
        returned, together with the permissions hint."""
        fake_bedrock.client = FakeBedrockControlClient(
            profiles=PROFILES,
            models_error=access_denied_error("ListFoundationModels"),
        )

        status, payload = invoke_models(bedrock_env, make_user("PortalAdmin"))
        assert status == 200
        assert [m["id"] for m in payload["models"]] == [
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.amazon.nova-pro-v1:0",
        ]
        assert "permissions" in payload
