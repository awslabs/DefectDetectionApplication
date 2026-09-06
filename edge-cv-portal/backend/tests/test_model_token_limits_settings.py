"""
Model_Token_Limits settings surface unit tests
(functions/data_accounts.py :: validate_model_token_limits,
handle_model_token_limits, _llm_model_token_limits).

Task 5.4 (spec: llm-model-token-and-image-sizing).

These tests cover:

1. validate_model_token_limits at every boundary: 200 vs 201 entries,
   256 vs 257-character keys, the empty key, a True value, and the
   value range edges 1 / 128000 / 0 / 128001, with every invalid entry
   identified in a rejected change (Requirement 4.2).
2. GET /data-accounts/bedrock-configuration/token-limits reporting
   `source: "environment"` before the settings item exists and
   `"settings"` after a PUT lands, with whole-mapping precedence over
   the LLM_MODEL_TOKEN_LIMITS bootstrap (Requirements 4.1, 1.6).
3. PUT of {} persisting the empty mapping - a real empty item, never
   item deletion or bootstrap fall-through (Requirement 4.8).
4. The Decimal seam: a settings item written through moto (so numbers
   come back from DynamoDB as Decimal) reads back through
   _llm_model_token_limits() as native int, and a deliberately
   un-converted Decimal falls through resolve_token_budget to the
   default of 10000 - which is why the loader must convert before the
   resolver sees any value (Requirements 1.6, 3.1).

Runs against the shared moto stack from conftest.py with the real
handler (every route request goes through data_accounts.handler).

_Requirements: 3.1, 4.1, 4.2, 4.8, 1.6_
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION

# Shared-layer resolver (conftest puts layers/shared/python on sys.path):
# the Decimal fall-through is asserted straight against it.
from dda_llm_request import MODEL_TOKEN_LIMIT_DEFAULT, resolve_token_budget

SETTINGS_TABLE_NAME = "test-settings-token-limits-unit"
RESOURCE_ID = "bedrock-configuration"
TOKEN_LIMITS_KEY = "llm_model_token_limits"

NOVA = "us.amazon.nova-pro-v1:0"


@pytest.fixture(scope="module")
def settings_env(aws_stack):
    """Settings table + freshly imported data_accounts module inside moto.

    LLM_MODEL_TOKEN_LIMITS is removed so the environment bootstrap is
    empty unless a test pins it with monkeypatch.
    """
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    os.environ.pop("LLM_MODEL_TOKEN_LIMITS", None)
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

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        data_accounts=data_accounts,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_settings(settings_env):
    """Each test starts with no stored settings items."""
    for setting_key in (TOKEN_LIMITS_KEY, "bedrock_configuration"):
        settings_env.settings_table.delete_item(
            Key={"setting_key": setting_key})
    yield


def make_admin():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "PortalAdmin"}


def invoke_token_limits(settings_env, method, user, mapping=None, body=None):
    """GET/PUT /data-accounts/bedrock-configuration/token-limits.

    `mapping` is wrapped as {"model_token_limits": mapping}; `body` sends
    a raw body instead (for malformed-body cases).
    """
    if body is None and mapping is not None:
        body = {"model_token_limits": mapping}
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}/token-limits",
        "path": f"/data-accounts/{RESOURCE_ID}/token-limits",
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


def stored_token_limits(settings_env):
    return settings_env.settings_table.get_item(
        Key={"setting_key": TOKEN_LIMITS_KEY}).get("Item")


# ===========================================================================
# 1. validate_model_token_limits boundaries (Requirement 4.2)
# ===========================================================================

class TestValidateModelTokenLimits:

    def validate(self, settings_env, value):
        return settings_env.data_accounts.validate_model_token_limits(value)

    def test_accepts_exactly_200_entries(self, settings_env):
        mapping = {f"model-{i:03d}": 1 for i in range(200)}
        assert self.validate(settings_env, mapping) == []

    def test_rejects_201_entries(self, settings_env):
        mapping = {f"model-{i:03d}": 1 for i in range(201)}
        errors = self.validate(settings_env, mapping)
        assert len(errors) == 1
        assert "200" in errors[0]

    def test_accepts_a_256_character_key(self, settings_env):
        assert self.validate(settings_env, {"m" * 256: 1}) == []

    def test_rejects_a_257_character_key(self, settings_env):
        errors = self.validate(settings_env, {"m" * 257: 1})
        assert len(errors) == 1
        assert "256" in errors[0]

    def test_rejects_the_empty_key(self, settings_env):
        errors = self.validate(settings_env, {"": 1})
        assert len(errors) == 1
        assert "non-empty" in errors[0]

    def test_rejects_a_boolean_value(self, settings_env):
        """True is an int subclass but is classified as a non-integer
        (Requirement 4.2), consistently with resolve_token_budget."""
        errors = self.validate(settings_env, {NOVA: True})
        assert len(errors) == 1
        assert f"limit for '{NOVA}'" in errors[0]

    @pytest.mark.parametrize("value,valid", [
        (1, True),          # lower bound
        (128000, True),     # Model_Token_Limit_Ceiling
        (0, False),         # one below the lower bound
        (128001, False),    # one above the ceiling
    ])
    def test_value_range_boundaries(self, settings_env, value, valid):
        errors = self.validate(settings_env, {NOVA: value})
        if valid:
            assert errors == []
        else:
            assert len(errors) == 1
            assert "between 1 and 128000" in errors[0]

    @pytest.mark.parametrize("value", [None, [], "limits", 42, True])
    def test_rejects_non_mappings(self, settings_env, value):
        errors = self.validate(settings_env, value)
        assert len(errors) == 1
        assert "must be an object" in errors[0]


# ===========================================================================
# 2. GET: source reporting (Requirements 4.1, 1.6)
# ===========================================================================

class TestTokenLimitsSourceReporting:

    def test_get_reports_environment_before_the_item_exists(
            self, settings_env, monkeypatch):
        """With no persisted item and no bootstrap, the effective mapping
        is empty, sourced from the environment, and the response carries
        the default and the ceiling the wizard displays."""
        monkeypatch.delenv("LLM_MODEL_TOKEN_LIMITS", raising=False)

        status, payload = invoke_token_limits(settings_env, "GET",
                                              make_admin())
        assert status == 200
        assert payload["source"] == "environment"
        assert payload["model_token_limits"] == {}
        assert payload["default"] == 10000
        assert payload["ceiling"] == 128000

    def test_source_flips_to_settings_once_a_put_lands(
            self, settings_env, monkeypatch):
        """The LLM_MODEL_TOKEN_LIMITS bootstrap is reported (source
        'environment') only until the settings item exists; the persisted
        mapping then wins whole - no per-key merge with the bootstrap."""
        monkeypatch.setenv("LLM_MODEL_TOKEN_LIMITS",
                           json.dumps({NOVA: 20000, "bootstrap-only": 500}))
        admin = make_admin()

        status, payload = invoke_token_limits(settings_env, "GET", admin)
        assert status == 200
        assert payload["source"] == "environment"
        assert payload["model_token_limits"] == {NOVA: 20000,
                                                 "bootstrap-only": 500}

        status, payload = invoke_token_limits(settings_env, "PUT", admin,
                                              mapping={NOVA: 30000})
        assert status == 200
        # Requirement 4.1: the response carries the persisted mapping.
        assert payload["model_token_limits"] == {NOVA: 30000}

        status, payload = invoke_token_limits(settings_env, "GET", admin)
        assert status == 200
        assert payload["source"] == "settings"
        # Whole-mapping precedence: the bootstrap entry does not merge in.
        assert payload["model_token_limits"] == {NOVA: 30000}


# ===========================================================================
# 3. PUT {} persists empty (Requirement 4.8)
# ===========================================================================

class TestEmptyMappingPersistence:

    def test_put_of_empty_mapping_persists_empty(self, settings_env,
                                                 monkeypatch):
        """{} is a real persisted value: the item exists with an empty
        mapping, and neither the prior mapping nor the environment
        bootstrap resurfaces."""
        monkeypatch.setenv("LLM_MODEL_TOKEN_LIMITS",
                           json.dumps({NOVA: 20000}))
        admin = make_admin()
        status, _ = invoke_token_limits(settings_env, "PUT", admin,
                                        mapping={NOVA: 30000})
        assert status == 200

        status, payload = invoke_token_limits(settings_env, "PUT", admin,
                                              mapping={})
        assert status == 200
        assert payload["model_token_limits"] == {}

        item = stored_token_limits(settings_env)
        assert item is not None
        assert item["value"] == {}

        status, payload = invoke_token_limits(settings_env, "GET", admin)
        assert status == 200
        assert payload["model_token_limits"] == {}
        assert payload["source"] == "settings"


# ===========================================================================
# 4. PUT: rejected changes identify each invalid entry and persist
#    nothing (Requirement 4.2)
# ===========================================================================

class TestTokenLimitsPutRejection:

    def test_rejection_identifies_each_invalid_entry_and_writes_nothing(
            self, settings_env):
        admin = make_admin()
        status, _ = invoke_token_limits(settings_env, "PUT", admin,
                                        mapping={NOVA: 20000})
        assert status == 200
        item_before = stored_token_limits(settings_env)

        long_key = "x" * 257
        status, payload = invoke_token_limits(
            settings_env, "PUT", admin,
            mapping={NOVA: 0, "": 5, long_key: True})
        assert status == 400
        errors = payload["validation_errors"]
        # Every invalid entry is identified in the one response.
        assert any(f"limit for '{NOVA}'" in e for e in errors)
        assert any("non-empty" in e for e in errors)
        assert any(f"limit for '{long_key}'" in e for e in errors)

        # The entire change is rejected: the persisted item is untouched.
        assert stored_token_limits(settings_env) == item_before

    @pytest.mark.parametrize("body", [
        {},                                     # wrapper key missing
        {"model_token_limits": ["m", 1]},       # not a mapping
        {"model_token_limits": "limits"},       # not a mapping
    ])
    def test_non_mapping_submissions_are_rejected(self, settings_env, body):
        status, payload = invoke_token_limits(settings_env, "PUT",
                                              make_admin(), body=body)
        assert status == 400
        assert any("must be an object" in e
                   for e in payload["validation_errors"])
        assert stored_token_limits(settings_env) is None


# ===========================================================================
# 5. The Decimal seam (Requirements 1.6, 3.1)
# ===========================================================================

class TestDecimalConversionSeam:

    def test_settings_item_written_through_moto_reads_back_as_native_int(
            self, settings_env):
        """DynamoDB returns every number as Decimal; the loader converts
        through _decimal_to_native so resolve_token_budget - which rejects
        non-int types by design - sees native ints and returns the
        configured value."""
        settings_env.settings_table.put_item(Item={
            "setting_key": TOKEN_LIMITS_KEY,
            "value": {NOVA: 20000},
        })
        # Stored as Decimal on the wire (the raw read proves the seam is
        # real, not a moto artifact).
        raw = stored_token_limits(settings_env)["value"][NOVA]
        assert isinstance(raw, Decimal)

        settings_env.data_accounts._reset_model_token_limits_cache()
        mapping = settings_env.data_accounts._llm_model_token_limits()
        value = mapping[NOVA]
        assert type(value) is int
        assert not isinstance(value, Decimal)
        assert resolve_token_budget(NOVA, None, mapping) == 20000

    def test_unconverted_decimal_falls_through_to_the_default(self):
        """A Decimal that skipped the loader's conversion is a non-int to
        the resolver and falls through to the default of 10000 - which is
        why _llm_model_token_limits must convert before resolution."""
        limits = {NOVA: Decimal("20000")}
        assert resolve_token_budget(NOVA, None, limits) \
            == MODEL_TOKEN_LIMIT_DEFAULT == 10000
