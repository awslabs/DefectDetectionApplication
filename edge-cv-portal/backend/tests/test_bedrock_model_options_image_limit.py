"""
Model_Image_Limit on the Bedrock model options listing
(llm-autolabel-prompt-tuning Requirements 7.1, 7.5).

Task 6.5 (spec: llm-autolabel-prompt-tuning).

GET /data-accounts/bedrock-configuration/models (data_accounts.
list_bedrock_model_options) carries an additive per-option `image_limit`
resolved through the shared-layer `resolve_model_image_limit` against the
`LLM_MODEL_IMAGE_LIMITS` environment configuration, so the labeling-job
wizard's few-shot attach/omit hint reads the same source the Preview_API and
the Auto_Labeler resolve. These tests cover:

1. A configured integer limit of at least 1 is returned verbatim.
2. Unlisted models, non-integer entries (strings, floats, bools, null) and
   values below 1 all resolve to the shared default of 20.
3. An absent, blank or malformed LLM_MODEL_IMAGE_LIMITS resolves every model
   to the default instead of erroring.
4. The change is strictly additive: every pre-existing field of every option,
   the option ordering, and the rest of the payload are unchanged.

Runs against the shared moto stack from conftest.py with a stubbed Bedrock
control-plane client (the same FakeBedrockControlClient shape used by
test_bedrock_configuration.py).

_Requirements: 7.1, 7.5_

Extended by task 5.4 (spec: llm-model-token-and-image-sizing) with section
3: every option also carries an additive `token_limit` beside
`image_limit`, resolved through the shared resolve_token_budget against
the persisted llm_model_token_limits settings item (default 10000), with
the rest of the payload unchanged.

_Requirements: 1.6, 3.1_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-model-options"
RESOURCE_ID = "bedrock-configuration"

# The pre-feature option payload: exactly {id, label} per option.
PRE_FEATURE_FIELDS = {"id", "label"}

PROFILES = [
    {"inferenceProfileId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "inferenceProfileName": "US Anthropic Claude Sonnet 4.5"},
    {"inferenceProfileId": "us.amazon.nova-pro-v1:0",
     "inferenceProfileName": "US Amazon Nova Pro"},
]

FOUNDATION_MODELS = [
    {"modelId": "amazon.titan-text-express-v1",
     "modelName": "Titan Text Express",
     "modelLifecycle": {"status": "ACTIVE"},
     "inferenceTypesSupported": ["ON_DEMAND"]},
]


@pytest.fixture(scope="module")
def options_env(aws_stack):
    """Settings table + freshly imported data_accounts module inside moto."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds SETTINGS_TABLE and a moto-intercepted
    # boto3 resource (conftest pattern).
    sys.modules.pop("data_accounts", None)
    import data_accounts

    yield SimpleNamespace(data_accounts=data_accounts)


class FakeBedrockControlClient:
    """Stand-in for the bedrock control-plane client (list APIs)."""

    def __init__(self, profiles=None, models=None):
        self.profiles = profiles or []
        self.models = models or []

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": self.profiles}

    def list_foundation_models(self, **kwargs):
        return {"modelSummaries": self.models}


@pytest.fixture
def fake_bedrock(options_env, monkeypatch):
    """Injects a FakeBedrockControlClient carrying the fixed listings."""
    state = SimpleNamespace(client=FakeBedrockControlClient(
        profiles=PROFILES, models=FOUNDATION_MODELS))

    monkeypatch.setattr(options_env.data_accounts,
                        "_get_bedrock_control_client",
                        lambda region: state.client)
    return state


def make_admin():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "PortalAdmin"}


def invoke_models(options_env, user=None):
    """GET /data-accounts/bedrock-configuration/models; (status, body)."""
    user = user or make_admin()
    event = {
        "httpMethod": "GET",
        "resource": "/data-accounts/{id}/models",
        "path": f"/data-accounts/{RESOURCE_ID}/models",
        "pathParameters": {"id": RESOURCE_ID},
        "queryStringParameters": None,
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
    response = options_env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def limits_of(payload):
    return {option["id"]: option["image_limit"] for option in payload["models"]}


# ===========================================================================
# 1. Configured values (Requirement 7.1)
# ===========================================================================

def test_configured_limit_is_returned_per_option(options_env, fake_bedrock,
                                                 monkeypatch):
    """A configured integer of at least 1 is reported verbatim for that model;
    models absent from the configuration keep the default of 20."""
    monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS", json.dumps({
        "us.amazon.nova-pro-v1:0": 4,
        "amazon.titan-text-express-v1": 1,
    }))

    status, payload = invoke_models(options_env)
    assert status == 200
    assert limits_of(payload) == {
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 20,  # unlisted
        "amazon.titan-text-express-v1": 1,
        "us.amazon.nova-pro-v1:0": 4,
    }


@pytest.mark.parametrize("configured", [
    "20",       # JSON string
    20.0,       # float
    True,       # bool (an int subclass, but not a configured limit)
    None,       # explicit null
    0,          # below 1
    -5,         # below 1
    [20],       # wrong type entirely
])
def test_invalid_entries_resolve_to_the_default(options_env, fake_bedrock,
                                               monkeypatch, configured):
    """Non-integer entries and values below 1 can neither widen the bound nor
    drive it to zero: each falls back to the shared default of 20
    (Requirement 7.1)."""
    monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS",
                       json.dumps({"us.amazon.nova-pro-v1:0": configured}))

    status, payload = invoke_models(options_env)
    assert status == 200
    assert limits_of(payload)["us.amazon.nova-pro-v1:0"] == 20


@pytest.mark.parametrize("raw", [
    None,               # variable not set at all
    "",                 # blank
    "   ",              # whitespace only
    "{not json",        # malformed
    "[1, 2, 3]",        # valid JSON, wrong shape
    "null",
])
def test_absent_or_malformed_configuration_defaults_every_model(
        options_env, fake_bedrock, monkeypatch, raw):
    """An absent, blank or malformed LLM_MODEL_IMAGE_LIMITS resolves every
    model to the default of 20 rather than failing the listing
    (Requirement 7.1)."""
    if raw is None:
        monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    else:
        monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS", raw)

    status, payload = invoke_models(options_env)
    assert status == 200
    assert set(limits_of(payload).values()) == {20}


# ===========================================================================
# 2. The field is strictly additive (Requirement 7.5)
# ===========================================================================

def test_rest_of_the_option_payload_is_unchanged(options_env, fake_bedrock,
                                                 monkeypatch):
    """Only the additive per-model limits are added (`image_limit`, and
    `token_limit` since llm-model-token-and-image-sizing task 5.1): each
    option keeps its exact id and label, the ordering is unchanged, and the
    payload's other keys are untouched, so consumers that ignore the fields
    are unaffected."""
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    status, before = invoke_models(options_env)
    assert status == 200

    monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS",
                       json.dumps({"us.amazon.nova-pro-v1:0": 3}))
    status, after = invoke_models(options_env)
    assert status == 200

    # Pre-feature shape: nothing but id, label and the additive fields.
    for option in after["models"]:
        assert set(option) == PRE_FEATURE_FIELDS | {"image_limit",
                                                    "token_limit"}

    def without_limit(payload):
        return [{k: v for k, v in option.items()
                 if k not in ("image_limit", "token_limit")}
                for option in payload["models"]]

    # id/label pairs and their order survive the configuration change.
    assert without_limit(after) == without_limit(before)
    assert without_limit(after) == [
        {"id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
         "label": "US Anthropic Claude Sonnet 4.5"},
        {"id": "amazon.titan-text-express-v1",
         "label": "Titan Text Express"},
        {"id": "us.amazon.nova-pro-v1:0",
         "label": "US Amazon Nova Pro"},
    ]

    # The rest of the response body is unchanged.
    assert set(after) == set(before) == {"models", "region"}
    assert after["region"] == before["region"]


# ===========================================================================
# 3. token_limit beside image_limit
#    (llm-model-token-and-image-sizing task 5.4; Requirements 1.6, 3.1)
# ===========================================================================

TOKEN_LIMITS_SETTING_KEY = "llm_model_token_limits"


@pytest.fixture
def settings_table(options_env):
    """The moto-backed settings table this module's data_accounts binds."""
    import boto3

    return boto3.resource("dynamodb",
                          region_name=REGION).Table(SETTINGS_TABLE_NAME)


@pytest.fixture(autouse=True)
def clean_token_limits_item(settings_table):
    """No stored llm_model_token_limits item around any test in this
    module, so the pre-existing image-limit cases keep the exact
    environment they were written against."""
    settings_table.delete_item(Key={"setting_key": TOKEN_LIMITS_SETTING_KEY})
    yield
    settings_table.delete_item(Key={"setting_key": TOKEN_LIMITS_SETTING_KEY})


def token_limits_of(payload):
    return {option["id"]: option["token_limit"]
            for option in payload["models"]}


def test_token_limit_is_carried_beside_image_limit(options_env, fake_bedrock,
                                                   settings_table,
                                                   monkeypatch):
    """Each option carries `token_limit` resolved from the persisted
    Model_Token_Limits through the shared resolve_token_budget - configured
    models verbatim, unlisted models the default of 10000 - beside an
    `image_limit` resolved from its own configuration exactly as before, so
    the budget the wizard pre-fills reads the same source the request paths
    resolve (Requirements 1.6, 3.1)."""
    monkeypatch.setenv("LLM_MODEL_IMAGE_LIMITS",
                       json.dumps({"us.amazon.nova-pro-v1:0": 4}))
    settings_table.put_item(Item={
        "setting_key": TOKEN_LIMITS_SETTING_KEY,
        "value": {"us.amazon.nova-pro-v1:0": 20000,
                  "amazon.titan-text-express-v1": 1},
    })

    status, payload = invoke_models(options_env)
    assert status == 200
    assert token_limits_of(payload) == {
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 10000,  # unlisted
        "amazon.titan-text-express-v1": 1,
        "us.amazon.nova-pro-v1:0": 20000,
    }
    # image_limit rides beside it, resolved from its own source unchanged.
    assert limits_of(payload) == {
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 20,
        "amazon.titan-text-express-v1": 20,
        "us.amazon.nova-pro-v1:0": 4,
    }
