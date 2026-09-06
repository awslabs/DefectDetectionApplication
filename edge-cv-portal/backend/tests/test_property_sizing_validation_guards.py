"""
Property-based test for the sizing validation guards
(functions/dda_labeling.py :: _validate_preview_run_request /
start_preview_run, and functions/data_accounts.py ::
validate_model_token_limits / handle_model_token_limits).

Spec: llm-model-token-and-image-sizing, task 7.5.

**Feature: llm-model-token-and-image-sizing, Property 11: Request
validation rejects invalid sizing inputs and touches nothing**
**Validates: Requirements 3.3, 3.5, 4.2, 4.3, 5.5**

*For any* Preview_Run request whose Downscale_Setting is neither
Downscale_Off nor a Max_Image_Edge option, or whose
Token_Budget_Selection is present and is not an integer between 1 and
128000 inclusive, the Preview_API SHALL reject the request with an
error naming every violated rule, SHALL read no referenced object, and
SHALL invoke no model; and *for any* Model_Token_Limits change
containing an invalid key or value, an over-size mapping, a non-mapping
value, or submitted without authority, the Settings_API SHALL reject
the change and leave the persisted Model_Token_Limits and the
Bedrock_Configuration unchanged.

The property has two halves, one test each, both driving the real
handlers against the moto stack from conftest.py:

- **Preview half** — request bodies with a non-empty subset of sizing
  violations injected (`downscale_max_edge` from booleans, strings
  including '1024' and 'off', floats including 1024.0, and integers
  outside the option set; `token_budget` from booleans, strings, floats,
  0, negatives, 128001 and null), crossed with the predecessor's
  existing violation set (llm-autolabel-prompt-tuning's
  test_property_preview_api_guards, whose PreviewEnv facade,
  violation-injection machinery and zero-call S3/Bedrock spies are
  reused unmodified) so multi-violation enumeration is covered.
- **Settings half** — submitted token-limit mappings with injected
  invalid keys (empty string, 257 characters) and values (bool, float,
  string, 0, out-of-range integers, null, arrays, objects), 201-entry
  mappings, and non-mapping values, each submitted both with and
  without `BEDROCK_CONFIG_WRITE`; plus a fully valid mapping submitted
  without authority (Requirement 4.3 applies to *any* change). JSON
  object keys are strings by construction, so a non-string key cannot
  arrive through the real request path; the non-string-key rule is
  asserted directly against `validate_model_token_limits` (the exact
  function the handler calls) in the same test.

The frontend half of Requirement 3.3 (client-side rejection before any
request is issued) is asserted in PromptTuningPreview.property.test.tsx.

Each test runs at 100 examples via its own `@settings`, which takes
precedence over the profile default.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from conftest import REGION

# Predecessor machinery, reused unmodified (llm-autolabel-prompt-tuning):
# the PreviewEnv facade with its refuse-and-record S3/Bedrock spies and
# store baselines, the violation-set generator and request builder, and
# the module-scoped `dda` fixture (the real dda_labeling inside moto).
from test_property_llm_autolabel_preservation import _Patcher
from test_property_preview_api_guards import (  # noqa: F401 (dda fixture)
    PreviewEnv,
    _build_rejected_request,
    _CallSpy,
    _rejected_specs,
    dda,
)

# ---------------------------------------------------------------------------
# Sizing violation material (design, Property 11 generator, half one).
# Every entry is JSON-round-trip stable, so the value the generator
# injects is exactly the value the handler sees.
# ---------------------------------------------------------------------------

# Neither null (Downscale_Off) nor one of the six Max_Image_Edge options
# (Req 5.5): booleans (bool is an int subclass), strings including the
# digit string '1024' and 'off', floats including the whole-valued
# 1024.0 (== 1024 numerically), and integers outside the option set.
BAD_DOWNSCALE_SETTINGS = (
    True, False,
    "1024", "off", "512", "none",
    1024.0, 2048.0, 512.5,
    0, -512, 511, 513, 1023, 2047, 2049, 4096, 100000,
)

# Present and not a non-boolean integer in [1, 128000] (Req 3.5):
# booleans, strings including digit strings, floats including
# whole-valued floats, zero, negatives, above-ceiling integers, and
# null (an empty budget control omits the key entirely, so a present
# null is a violation, never Downscale_Off-style shorthand).
BAD_TOKEN_BUDGETS = (
    True, False,
    "10000", "1", "abc", "",
    10000.0, 128000.0, 0.5,
    0, -1, -128000, 128001, 500000,
    None,
)


@st.composite
def _sizing_rejection_specs(draw):
    """A non-empty subset of sizing violations, optionally crossed with
    one of the predecessor's violation specs so the all-rules-evaluated
    pass is asserted across both feature generations at once."""
    sizing_rules = draw(st.lists(st.sampled_from(("downscale", "budget")),
                                 unique=True, min_size=1, max_size=2))
    crossed = draw(st.booleans())
    return SimpleNamespace(
        sizing_rules=sizing_rules,
        bad_downscale=draw(st.sampled_from(BAD_DOWNSCALE_SETTINGS)),
        bad_budget=draw(st.sampled_from(BAD_TOKEN_BUDGETS)),
        predecessor=draw(_rejected_specs()) if crossed else None,
        sample_count=draw(st.integers(min_value=1, max_value=5)),
    )


# =========================================================================== #
# Half one: the Preview_API rejects invalid sizing inputs, names every
# violated rule, and touches nothing.
# =========================================================================== #

# Feature: llm-model-token-and-image-sizing, Property 11: Request
# validation rejects invalid sizing inputs and touches nothing
@settings(max_examples=100, deadline=None)
@given(spec=_sizing_rejection_specs())
def test_property_preview_sizing_rejection_enumerates_and_touches_nothing(
        aws_stack, dda, spec):
    """
    **Feature: llm-model-token-and-image-sizing, Property 11: Request
    validation rejects invalid sizing inputs and touches nothing**

    For any Preview_Run request whose Downscale_Setting is neither
    Downscale_Off nor a Max_Image_Edge option, or whose
    Token_Budget_Selection is present and is not an integer between 1
    and 128000 inclusive — alone or combined with any subset of the
    predecessor's violations — the Preview_API answers one 400 naming
    every violated rule (the downscale message listing the six
    permitted values, the budget message naming the accepted range),
    reads no referenced object, invokes no model, starts no run, and
    writes nothing.

    **Validates: Requirements 3.3, 3.5, 5.5**
    """
    patcher = _Patcher()
    try:
        env = PreviewEnv(aws_stack, dda, patcher)
        user = env.make_user("DataScientist")

        if spec.predecessor is not None:
            body, _, expected, _ = _build_rejected_request(
                env, spec.predecessor)
        else:
            # Everything else valid: the sizing rules are then provably
            # the sole cause of the rejection.
            body = env.valid_body(sample_count=spec.sample_count)
            expected = set()

        if "downscale" in spec.sizing_rules:
            body["downscale_max_edge"] = spec.bad_downscale
            expected.add("downscale_max_edge")
        if "budget" in spec.sizing_rules:
            body["token_budget"] = spec.bad_budget
            expected.add("token_budget")

        status, response, _ = env.start(body, user)

        # One 400 carrying every violation — the sizing rules are
        # evaluated in the same all-rules pass as every predecessor rule,
        # never short-circuited (Req 3.5, 5.5).
        assert status == 400, response
        assert response["error"] == env.module.PREVIEW_VALIDATION_FAILED_MESSAGE
        errors = response["validation_errors"]
        assert {error["parameter"] for error in errors} == expected, errors
        assert all(error["message"] for error in errors)

        if "downscale" in spec.sizing_rules:
            # Exactly one entry, identifying the Downscale_Setting and
            # listing every permitted value (Req 5.5).
            [downscale_error] = [error for error in errors
                                 if error["parameter"] == "downscale_max_edge"]
            for option in dda.module.MAX_IMAGE_EDGE_OPTIONS:
                assert str(option) in downscale_error["message"], (
                    downscale_error)
        if "budget" in spec.sizing_rules:
            # Exactly one entry, identifying the Token_Budget_Selection
            # and naming the accepted range (Req 3.5, 3.3).
            [budget_error] = [error for error in errors
                              if error["parameter"] == "token_budget"]
            assert str(dda.module.MODEL_TOKEN_LIMIT_CEILING) \
                in budget_error["message"], budget_error

        # No object read, no model invoked, no run item, no sample item,
        # no lock, no executor invoke — and no preview_run audit event,
        # so no Preview_Run was started in any observable sense.
        env.assert_nothing_written(user["user_id"])
        assert env.audit_events(user["user_id"], "preview_run") == []
    finally:
        patcher.undo()


# =========================================================================== #
# Half two: the Settings_API rejects invalid (or unauthorized)
# Model_Token_Limits changes and leaves both settings items untouched.
# =========================================================================== #

SETTINGS_TABLE_NAME = "test-settings-sizing-guards"
RESOURCE_ID = "bedrock-configuration"

TOKEN_LIMITS_KEY = "llm_model_token_limits"
BEDROCK_CONFIG_KEY = "bedrock_configuration"

# Every role that lacks BEDROCK_CONFIG_WRITE (PortalAdmin-only).
UNAUTHORIZED_SETTINGS_ROLES = ("Viewer", "Operator", "DataScientist",
                               "UseCaseAdmin", "DataLabeler")

# A stored global configuration in exactly the shape
# update_bedrock_configuration_setting persists, with pinned write
# metadata so byte-equality across the rejected requests is
# deterministic (test_property_model_token_limits_isolation.py
# conventions).
SEED_UPDATED_AT = 1700000000000
SEED_UPDATED_BY = "seed-admin"
SEEDED_BEDROCK_CONFIG_VALUE = {
    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "region": "us-west-2",
    "max_tokens": 128000,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 45,
}

# Invalid limit values (Req 4.2): booleans classified as non-integers,
# strings including digit strings, floats including whole-valued floats,
# out-of-range integers with 0 and 128001 at the boundaries, null,
# arrays and objects. All JSON-round-trip stable.
INVALID_LIMIT_VALUES = (
    True, False,
    "10000", "",
    10000.5, 128000.0,
    0, -1, 128001,
    None,
    [10000],
    {"nested": 10000},
)

# The JSON-expressible invalid keys: an empty string and a
# 257-character string. (A non-string key cannot exist in a JSON
# object; that rule is asserted directly below.)
EMPTY_KEY = ""
LONG_KEY = "k" * 257


def make_user(role):
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": role}


@pytest.fixture(scope="module")
def settings_env(aws_stack):
    """Settings table + freshly imported data_accounts inside moto, with
    the module's S3 client replaced by a refuse-and-record spy and the
    Bedrock model listing (the only Bedrock entry point the module owns)
    replaced by a tripwire — the token-limits routes must reach neither.
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

    s3_spy = _CallSpy("data_accounts s3")
    data_accounts.s3 = s3_spy

    def _no_model_options(*args, **kwargs):
        raise AssertionError(
            "the token-limits routes must not reach the Bedrock model "
            "listing")

    data_accounts.list_bedrock_model_options = _no_model_options

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        data_accounts=data_accounts,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
        audit_log=aws_stack.tables.audit_log,
        s3_spy=s3_spy,
    )


def _invoke(env, method, user, path_suffix="", body=None):
    """Drive the real Lambda handler with an API Gateway event."""
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}",
        "path": f"/data-accounts/{RESOURCE_ID}{path_suffix}",
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
    response = env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def invoke_token_limits_put(env, user, submitted):
    """PUT /data-accounts/bedrock-configuration/token-limits with any
    submitted value (mapping or not) in the wrapped request shape."""
    return _invoke(env, "PUT", user, path_suffix="/token-limits",
                   body={"model_token_limits": submitted})


def read_item(env, setting_key):
    """The whole stored settings item, or None when absent."""
    return env.settings_table.get_item(
        Key={"setting_key": setting_key}).get("Item")


def reset_settings(env, *, persisted_limits=None, seed_config=False):
    """Per-example state reset: both items dropped, then seeded with
    pinned write metadata."""
    env.settings_table.delete_item(Key={"setting_key": TOKEN_LIMITS_KEY})
    env.settings_table.delete_item(Key={"setting_key": BEDROCK_CONFIG_KEY})
    if persisted_limits is not None:
        env.settings_table.put_item(Item={
            "setting_key": TOKEN_LIMITS_KEY,
            "value": persisted_limits,
            "updated_at": SEED_UPDATED_AT,
            "updated_by": SEED_UPDATED_BY,
        })
    if seed_config:
        env.settings_table.put_item(Item={
            "setting_key": BEDROCK_CONFIG_KEY,
            "value": SEEDED_BEDROCK_CONFIG_VALUE,
            "updated_at": SEED_UPDATED_AT,
            "updated_by": SEED_UPDATED_BY,
        })


def audit_denials(env, user_sub):
    """Every unauthorized_access audit entry recorded for one user."""
    items = env.audit_log.scan().get("Items", [])
    return [item for item in items
            if item.get("user_id") == user_sub
            and item.get("action") == "unauthorized_access"]


def _expected_violations(value):
    """(violation count, keys whose values are invalid) for one
    submitted value, mirroring Requirement 4.2's rules directly — not
    the implementation: a non-mapping is one whole-change violation;
    otherwise one violation for exceeding 200 entries, one per key that
    is not a non-empty string of at most 256 characters, and one per
    value that is not a non-boolean integer in [1, 128000], counted
    independently so an entry can violate both rules."""
    if not isinstance(value, dict):
        return 1, set()
    count = 1 if len(value) > 200 else 0
    bad_value_keys = set()
    for key, limit in value.items():
        if not isinstance(key, str) or not key or len(key) > 256:
            count += 1
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= 128000):
            count += 1
            bad_value_keys.add(key)
    return count, bad_value_keys


# ------------------------------------------------------------- generators

_model_keys = st.text(min_size=1, max_size=32)
_valid_limits = st.integers(min_value=1, max_value=128000)
_valid_mappings = st.dictionaries(_model_keys, _valid_limits, max_size=6)


@st.composite
def _invalid_submissions(draw):
    """One invalid Model_Token_Limits submission: a non-mapping value,
    a 201+-entry mapping, or a mapping carrying at least one invalid
    key and/or value alongside any number of valid entries."""
    kind = draw(st.sampled_from(("non_mapping", "entries", "oversize")))
    if kind == "non_mapping":
        return draw(st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(max_size=12),
            st.lists(_valid_limits, max_size=3),
        ))
    if kind == "oversize":
        entry_count = draw(st.integers(min_value=201, max_value=203))
        return {f"model-{index:03d}": 10000 for index in range(entry_count)}
    entries = dict(draw(st.dictionaries(_model_keys, _valid_limits,
                                        max_size=4)))
    injections = draw(st.lists(
        st.sampled_from(("bad_value", "empty_key", "long_key")),
        unique=True, min_size=1, max_size=3))
    if "bad_value" in injections:
        key = draw(_model_keys.filter(lambda k: k not in entries))
        entries[key] = draw(st.sampled_from(INVALID_LIMIT_VALUES))
    if "empty_key" in injections:
        entries[EMPTY_KEY] = draw(st.one_of(
            _valid_limits, st.sampled_from(INVALID_LIMIT_VALUES)))
    if "long_key" in injections:
        entries[LONG_KEY] = draw(st.one_of(
            _valid_limits, st.sampled_from(INVALID_LIMIT_VALUES)))
    return entries


# Feature: llm-model-token-and-image-sizing, Property 11: Request
# validation rejects invalid sizing inputs and touches nothing
@settings(max_examples=100, deadline=None)
# The explicitly named boundary: a 201-entry mapping of valid entries.
@example(submitted={f"model-{index:03d}": 10000 for index in range(201)},
         persisted={"kept-entry": 20000}, seed_config=True,
         role="DataScientist",
         valid_mapping={"us.amazon.nova-pro-v1:0": 10000})
# A non-mapping submission against an absent store.
@example(submitted=["not", "a", "mapping"], persisted=None,
         seed_config=False, role="Viewer", valid_mapping={})
# An empty key beside a boolean value: two violations, one entry each.
@example(submitted={"": 10000, "ok": True}, persisted={}, seed_config=True,
         role="DataLabeler", valid_mapping={"m": 1})
@given(submitted=_invalid_submissions(),
       persisted=st.one_of(st.none(), _valid_mappings),
       seed_config=st.booleans(),
       role=st.sampled_from(UNAUTHORIZED_SETTINGS_ROLES),
       valid_mapping=_valid_mappings)
def test_property_token_limits_change_rejected_and_touches_nothing(
        settings_env, submitted, persisted, seed_config, role,
        valid_mapping):
    """
    **Feature: llm-model-token-and-image-sizing, Property 11: Request
    validation rejects invalid sizing inputs and touches nothing**

    For any invalid Model_Token_Limits submission (non-mapping,
    over-size mapping, invalid key, invalid value), a PUT with
    BEDROCK_CONFIG_WRITE answers one 400 with exactly one validation
    error per violation, each invalid value named by its entry's key;
    the same submission without BEDROCK_CONFIG_WRITE — and a fully
    valid submission without it — answers the fixed 403 with no
    validation detail and records exactly one unauthorized_access audit
    entry with result='denied'. In every case both settings items (the
    Model_Token_Limits and the Bedrock_Configuration) are byte-equal to
    their pre-request state, and no S3 or Bedrock entry point is
    touched.

    **Validates: Requirements 4.2, 4.3**
    """
    reset_settings(settings_env, persisted_limits=persisted,
                   seed_config=seed_config)
    limits_before = read_item(settings_env, TOKEN_LIMITS_KEY)
    config_before = read_item(settings_env, BEDROCK_CONFIG_KEY)

    expected_count, bad_value_keys = _expected_violations(submitted)

    # -- with BEDROCK_CONFIG_WRITE: one 400 naming every violation -------
    admin = make_user("PortalAdmin")
    status, body = invoke_token_limits_put(settings_env, admin, submitted)
    assert status == 400, body
    assert body["error"] == "Invalid model token limits"
    errors = body["validation_errors"]
    # One error entry per violation — the whole mapping is validated,
    # nothing short-circuits past the first invalid element (Req 4.2).
    assert len(errors) == expected_count, (errors, submitted)
    assert all(isinstance(error, str) and error for error in errors)
    # Each invalid value is identified by its entry's key.
    for key in bad_value_keys:
        assert any(f"'{key}'" in error for error in errors), (key, errors)
    if isinstance(submitted, dict) and len(submitted) > 200:
        assert any("200" in error for error in errors), errors

    # The rejected change persisted nothing: both items byte-equal
    # (write metadata included), and an absent item stays absent.
    assert read_item(settings_env, TOKEN_LIMITS_KEY) == limits_before
    assert read_item(settings_env, BEDROCK_CONFIG_KEY) == config_before

    # -- the same submission without BEDROCK_CONFIG_WRITE: 403 -----------
    outsider = make_user(role)
    status, body = invoke_token_limits_put(settings_env, outsider, submitted)
    assert status == 403, body
    assert body == {
        "error": "PortalAdmin access required",
        "required_permissions": [
            settings_env.data_accounts.Permission
            .BEDROCK_CONFIG_WRITE.value],
    }
    denials = audit_denials(settings_env, outsider["user_id"])
    assert len(denials) == 1, denials
    assert denials[0]["result"] == "denied"
    assert read_item(settings_env, TOKEN_LIMITS_KEY) == limits_before
    assert read_item(settings_env, BEDROCK_CONFIG_KEY) == config_before

    # -- a fully VALID mapping without authority: rejected identically ---
    # (Requirement 4.3 covers any change submitted without authority,
    # valid or not, and the 403 must disclose no validation verdict.)
    second_outsider = make_user(role)
    status, body = invoke_token_limits_put(settings_env, second_outsider,
                                           valid_mapping)
    assert status == 403, body
    assert "validation_errors" not in body
    denials = audit_denials(settings_env, second_outsider["user_id"])
    assert len(denials) == 1, denials
    assert denials[0]["result"] == "denied"
    assert read_item(settings_env, TOKEN_LIMITS_KEY) == limits_before
    assert read_item(settings_env, BEDROCK_CONFIG_KEY) == config_before

    # No S3 call and (structurally) no Bedrock entry point on any path.
    assert settings_env.s3_spy.calls == []

    # A non-string key cannot ride a JSON request body (json.loads only
    # produces string keys), so Requirement 4.2's non-string-key clause
    # is asserted against the exact function the handler calls.
    non_string_key_errors = settings_env.data_accounts \
        .validate_model_token_limits({12345: 10000})
    assert len(non_string_key_errors) == 1, non_string_key_errors
