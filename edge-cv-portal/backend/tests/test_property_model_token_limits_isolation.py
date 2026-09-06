"""
Property-based test for Model_Token_Limits write isolation
(functions/data_accounts.py :: handle_model_token_limits,
update_model_token_limits_setting, update_bedrock_configuration_setting).

Spec: llm-model-token-and-image-sizing, task 5.2.

**Feature: llm-model-token-and-image-sizing, Property 14: Token limit
writes fully replace and stay isolated from the global configuration**
**Validates: Requirements 1.1, 4.1, 4.4, 4.7, 4.8**

For any persisted Model_Token_Limits mapping and any valid submitted
mapping (including the empty mapping), a Model_Token_Limits write SHALL
leave the persisted mapping equal to the submitted mapping
entry-for-entry with no omitted entry retained, SHALL leave every
Bedrock_Configuration field unchanged, and SHALL produce the same
persisted state on repeated submission of the same mapping; for any
valid Bedrock_Configuration change, the persisted Model_Token_Limits
SHALL be unchanged; and after an empty mapping is persisted, the
Token_Budget_Resolver SHALL return 10000 for every model identifier
with no Token_Budget_Selection.

Runs against the shared moto stack from conftest.py with the REAL
handlers — every write goes through data_accounts.handler with a
PortalAdmin event, exactly as production traffic does. Per the design's
test strategy:

- Persisted and submitted mappings are drawn from
  `st.dictionaries(st.text(min_size=1, max_size=32),
  st.integers(1, 128000), max_size=12)`, with the empty mapping given
  explicit weight and with the two key sets built from one shared pool
  so overlapping and disjoint sets — and therefore omission — are
  exercised routinely.
- Isolation is asserted on the WHOLE stored item (write metadata
  included): a token-limits write must leave the `bedrock_configuration`
  item byte-equal to its pre-write state (or absent when it was
  absent), and a Bedrock_Configuration write must leave the
  `llm_model_token_limits` item byte-equal.
- Idempotence compares everything the handler persists except
  `updated_at`, which is wall-clock write metadata (two puts of the
  same mapping may land on different milliseconds), never mapping
  state.
- Exact-string key matching (Requirement 1.1) is asserted by
  persisting keys that differ only in case and in surrounding
  whitespace and checking each resolves independently through the real
  round trip (PUT -> stored item -> GET -> resolve_token_budget).

Each property runs at 100 examples via its own `@settings`, which takes
precedence over the profile default.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from conftest import REGION

# Shared-layer resolver (conftest puts layers/shared/python on sys.path):
# the empty-mapping clause of the property is asserted straight through it.
from dda_llm_request import resolve_token_budget

SETTINGS_TABLE_NAME = "test-settings-token-limits-isolation"
RESOURCE_ID = "bedrock-configuration"

TOKEN_LIMITS_KEY = "llm_model_token_limits"
BEDROCK_CONFIG_KEY = "bedrock_configuration"

# A stored global configuration in exactly the shape
# update_bedrock_configuration_setting persists, with pinned write
# metadata so byte-equality across a token-limits write is
# deterministic. max_tokens carries the deployed 128000 — the value the
# feature exists to stop disturbing (Requirement 4.4).
SEEDED_BEDROCK_CONFIG_ITEM = {
    "setting_key": BEDROCK_CONFIG_KEY,
    "value": {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "region": "us-west-2",
        "max_tokens": 128000,
        "temperature": Decimal("0.5"),
        "top_p": None,
        "timeout_seconds": 45,
    },
    "updated_at": 1700000000000,
    "updated_by": "seed-admin",
}

# Pinned write metadata for seeded token-limits items (test 2 asserts the
# whole item, timestamps included, survives a configuration write).
SEED_UPDATED_AT = 1700000000000
SEED_UPDATED_BY = "seed-admin"


# ---------------------------------------------------------------------------
# moto environment (conftest pattern, as in test_bedrock_configuration.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def token_env(aws_stack):
    """Settings table + freshly imported data_accounts module inside moto.

    LLM_MODEL_TOKEN_LIMITS is removed from the environment so the
    bootstrap mapping is empty and the persisted item is the one source
    of truth under test.
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


def make_admin():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "PortalAdmin"}


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


def invoke_token_limits(env, method, user, mapping=None):
    """GET / PUT /data-accounts/bedrock-configuration/token-limits."""
    body = {"model_token_limits": mapping} if mapping is not None else None
    return _invoke(env, method, user, path_suffix="/token-limits", body=body)


def invoke_bedrock_config_put(env, user, change):
    """PUT /data-accounts/bedrock-configuration (partial change body)."""
    return _invoke(env, "PUT", user, body=change)


def read_item(env, setting_key):
    """The whole stored settings item, or None when absent."""
    return env.settings_table.get_item(
        Key={"setting_key": setting_key}).get("Item")


def reset_settings(env, *, persisted_limits=None, seed_config=False):
    """Per-example state reset: both items dropped, then seeded.

    `persisted_limits` (a mapping) seeds the token-limits item with
    pinned write metadata; `seed_config` seeds the pinned global
    configuration item.
    """
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
        env.settings_table.put_item(Item=SEEDED_BEDROCK_CONFIG_ITEM)


def without_write_timestamp(item):
    """The persisted state minus `updated_at` — wall-clock write metadata
    that two puts of the same mapping may legitimately differ on by a
    millisecond. Everything else (setting_key, value, updated_by) must be
    identical for the idempotence clause."""
    return {k: v for k, v in item.items() if k != "updated_at"}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Model identifier keys and limit values per the design's generator:
# st.dictionaries(st.text(min_size=1, max_size=32), st.integers(1, 128000)).
_model_keys = st.text(min_size=1, max_size=32)
_limit_values = st.integers(min_value=1, max_value=128000)

# A single mapping, with the empty mapping given explicit weight.
_weighted_mappings = st.one_of(
    st.just({}),
    st.dictionaries(_model_keys, _limit_values, max_size=12),
)


@st.composite
def _mapping_pairs(draw):
    """(persisted, submitted) built from one shared key pool.

    Each pool key lands in the persisted mapping, the submitted mapping,
    both (usually with different values, exercising replacement), or
    neither — so overlapping and disjoint key sets, and in particular
    omission (a persisted key absent from the submission), occur
    routinely. The empty submitted mapping is given explicit extra
    weight (Requirement 4.8).
    """
    pool = draw(st.lists(_model_keys, max_size=12, unique=True))
    persisted, submitted = {}, {}
    for key in pool:
        membership = draw(st.sampled_from(
            ("persisted", "submitted", "both", "neither")))
        if membership in ("persisted", "both"):
            persisted[key] = draw(_limit_values)
        if membership in ("submitted", "both"):
            submitted[key] = draw(_limit_values)
    if draw(st.integers(min_value=0, max_value=4)) == 0:
        submitted = {}
    return persisted, submitted


# Valid partial Bedrock_Configuration changes: any subset of the six
# fields, each value valid under validate_bedrock_configuration, so the
# merged result is always accepted and the write always happens.
# (Sampling values use clean decimal representations — their numeric
# variety belongs to Property 3; what matters here is which fields the
# change carries.)
_sampling_values = st.one_of(
    st.none(), st.sampled_from((0, 1, 0.05, 0.5, 0.95)))
_bedrock_partial_changes = st.fixed_dictionaries({}, optional={
    "model_id": st.sampled_from((
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "us.amazon.nova-pro-v1:0",
        "m1",
    )),
    "region": st.sampled_from(("us-east-1", "us-west-2", "eu-central-1")),
    "max_tokens": st.integers(min_value=1, max_value=1_000_000),
    "temperature": _sampling_values,
    "top_p": _sampling_values,
    "timeout_seconds": st.integers(min_value=1, max_value=240),
})

# Lowercase alphabetic bases so the upper-case variant always differs.
_variant_bases = st.text(alphabet="abcdefghijklmnopqrstuvwxyz",
                         min_size=3, max_size=8)


@st.composite
def _variant_families(draw):
    """(variants, mapping): keys differing only in case or in surrounding
    whitespace, a non-empty subset of them persisted with pairwise
    distinct values (Requirement 1.1: exact string comparison, no
    trimming, no case folding)."""
    base = draw(_variant_bases)
    variants = [base, base.upper(), " " + base, base + " ", " " + base + " "]
    included = sorted(draw(st.sets(
        st.integers(min_value=0, max_value=len(variants) - 1), min_size=1)))
    start = draw(st.integers(min_value=1,
                             max_value=128000 - len(variants)))
    mapping = {variants[i]: start + i for i in included}
    return variants, mapping


# ---------------------------------------------------------------------------
# Property 14, clause 1-3: a token-limits write replaces the mapping in
# its entirety, leaves the global configuration item untouched, and is
# idempotent.
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 14: Token limit
# writes fully replace and stay isolated from the global configuration
@settings(max_examples=100, deadline=None)
# Total omission: every persisted entry dropped by the empty mapping:
@example(pair=({"us.amazon.nova-pro-v1:0": 10000}, {}), config_seeded=True)
# First write into an empty store:
@example(pair=({}, {"us.amazon.nova-pro-v1:0": 128000}), config_seeded=True)
# Overlap with a changed value plus an omitted and an added key:
@example(pair=({"kept": 1, "omitted": 2}, {"kept": 3, "added": 4}),
         config_seeded=True)
# No global configuration item stored: the write must not conjure one:
@example(pair=({"m": 1}, {}), config_seeded=False)
@given(pair=_mapping_pairs(), config_seeded=st.booleans())
def test_token_limit_write_fully_replaces_and_stays_isolated(
        token_env, pair, config_seeded):
    """
    **Feature: llm-model-token-and-image-sizing, Property 14: Token
    limit writes fully replace and stay isolated from the global
    configuration**

    For any persisted mapping and any valid submitted mapping, a PUT of
    the token-limits item persists exactly the submitted mapping
    (entry-for-entry, no omitted entry retained), returns the persisted
    mapping in the response, leaves the stored `bedrock_configuration`
    item byte-equal to its pre-write state (absent stays absent), and a
    repeated submission of the same mapping yields identical persisted
    state.

    **Validates: Requirements 1.1, 4.1, 4.4, 4.7, 4.8**
    """
    persisted, submitted = pair
    admin = make_admin()
    reset_settings(token_env, persisted_limits=persisted,
                   seed_config=config_seeded)
    config_before = read_item(token_env, BEDROCK_CONFIG_KEY)

    # -- the write --------------------------------------------------------
    status, payload = invoke_token_limits(token_env, "PUT", admin,
                                          mapping=submitted)
    assert status == 200
    # Requirement 4.1: the response carries the persisted mapping.
    assert payload["model_token_limits"] == submitted

    item_first = read_item(token_env, TOKEN_LIMITS_KEY)
    assert item_first is not None
    assert item_first["setting_key"] == TOKEN_LIMITS_KEY
    # Entry-for-entry replacement (Decimal == int holds elementwise), and
    # no omitted entry retained: the key sets are identical.
    assert item_first["value"] == submitted
    assert set(item_first["value"].keys()) == set(submitted.keys())

    # Requirement 4.4: the global configuration item — every field,
    # write metadata included — is exactly its pre-write state, and an
    # absent item stays absent.
    assert read_item(token_env, BEDROCK_CONFIG_KEY) == config_before

    # -- idempotence ------------------------------------------------------
    status_again, payload_again = invoke_token_limits(
        token_env, "PUT", admin, mapping=submitted)
    assert status_again == 200
    assert payload_again["model_token_limits"] == submitted
    item_second = read_item(token_env, TOKEN_LIMITS_KEY)
    assert (without_write_timestamp(item_second)
            == without_write_timestamp(item_first))
    assert read_item(token_env, BEDROCK_CONFIG_KEY) == config_before

    # -- the effective read agrees with the store --------------------------
    status_get, got = invoke_token_limits(token_env, "GET", admin)
    assert status_get == 200
    assert got["model_token_limits"] == submitted
    assert got["source"] == "settings"


# ---------------------------------------------------------------------------
# Property 14, clause 4: a valid Bedrock_Configuration change leaves the
# persisted Model_Token_Limits byte-equal.
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 14: Token limit
# writes fully replace and stay isolated from the global configuration
@settings(max_examples=100, deadline=None)
# The motivating scenario: the global budget is retuned for one model
# while another model's configured limit must not move:
@example(mapping={"us.amazon.nova-pro-v1:0": 10000},
         change={"max_tokens": 999999}, limits_seeded=True)
# The empty persisted mapping survives a configuration write as empty:
@example(mapping={}, change={"max_tokens": 1}, limits_seeded=True)
# An absent token-limits item stays absent — the write conjures nothing:
@example(mapping={}, change={"timeout_seconds": 240}, limits_seeded=False)
# An empty change body is a valid change (merges nothing, rewrites the
# effective configuration) and still touches no token-limits entry:
@example(mapping={"m": 128000}, change={}, limits_seeded=True)
@given(mapping=_weighted_mappings, change=_bedrock_partial_changes,
       limits_seeded=st.booleans())
def test_bedrock_configuration_write_leaves_token_limits_unchanged(
        token_env, mapping, change, limits_seeded):
    """
    **Feature: llm-model-token-and-image-sizing, Property 14: Token
    limit writes fully replace and stay isolated from the global
    configuration**

    For any persisted Model_Token_Limits mapping (or none at all) and
    any valid partial Bedrock_Configuration change, the configuration
    PUT succeeds and persists, and the stored `llm_model_token_limits`
    item — every field, write metadata included — is byte-equal to its
    pre-write state.

    **Validates: Requirements 4.7, 1.1**
    """
    admin = make_admin()
    reset_settings(token_env,
                   persisted_limits=mapping if limits_seeded else None)
    limits_before = read_item(token_env, TOKEN_LIMITS_KEY)

    status, _ = invoke_bedrock_config_put(token_env, admin, change)
    assert status == 200
    # The configuration write really happened (the isolation claim is
    # not vacuous): the item now exists.
    assert read_item(token_env, BEDROCK_CONFIG_KEY) is not None

    # Requirement 4.7: the token-limits item is exactly its pre-write
    # state — byte-equal when present, still absent when absent.
    assert read_item(token_env, TOKEN_LIMITS_KEY) == limits_before


# ---------------------------------------------------------------------------
# Property 14, clause 5: after {} is persisted, resolution returns 10000
# for every model identifier with no Token_Budget_Selection.
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 14: Token limit
# writes fully replace and stay isolated from the global configuration
@settings(max_examples=100, deadline=None)
@example(persisted={"us.amazon.nova-pro-v1:0": 20000},
         identifiers=["us.amazon.nova-pro-v1:0"])
@given(persisted=_weighted_mappings,
       identifiers=st.lists(_model_keys, min_size=1, max_size=8))
def test_empty_mapping_resolves_the_default_for_every_identifier(
        token_env, persisted, identifiers):
    """
    **Feature: llm-model-token-and-image-sizing, Property 14: Token
    limit writes fully replace and stay isolated from the global
    configuration**

    After the empty mapping is persisted over any prior mapping, the
    stored value is the empty mapping ({} persists as empty, never as
    item deletion or bootstrap fall-through), the effective mapping the
    settings API reports is empty with source 'settings', and the
    Token_Budget_Resolver returns 10000 for every model identifier —
    including every identifier the prior mapping configured.

    **Validates: Requirements 4.8, 4.1**
    """
    admin = make_admin()
    reset_settings(token_env, persisted_limits=persisted)

    status, _ = invoke_token_limits(token_env, "PUT", admin, mapping={})
    assert status == 200

    item = read_item(token_env, TOKEN_LIMITS_KEY)
    assert item is not None
    assert item["value"] == {}

    status_get, got = invoke_token_limits(token_env, "GET", admin)
    assert status_get == 200
    assert got["model_token_limits"] == {}
    assert got["source"] == "settings"

    effective = got["model_token_limits"]
    for identifier in list(identifiers) + list(persisted.keys()):
        assert resolve_token_budget(identifier, None, effective) == 10000


# ---------------------------------------------------------------------------
# Requirement 1.1 through the persisted round trip: exact-string key
# matching — keys differing only in case or surrounding whitespace are
# independent entries.
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 14: Token limit
# writes fully replace and stay isolated from the global configuration
@settings(max_examples=100, deadline=None)
@example(family=(["nova", "NOVA", " nova", "nova ", " nova "],
                 {"nova": 100, "NOVA": 200, " nova": 300}))
@given(family=_variant_families())
def test_case_and_whitespace_variant_keys_resolve_independently(
        token_env, family):
    """
    **Feature: llm-model-token-and-image-sizing, Property 14: Token
    limit writes fully replace and stay isolated from the global
    configuration**

    A persisted mapping whose keys differ only in case or in
    surrounding whitespace resolves each key to exactly its own value —
    matched by exact string comparison with no trimming and no case
    folding — and every variant the mapping omits resolves the default,
    all through the real PUT -> store -> GET round trip.

    **Validates: Requirements 1.1, 4.1**
    """
    variants, mapping = family
    admin = make_admin()
    reset_settings(token_env)

    status, _ = invoke_token_limits(token_env, "PUT", admin, mapping=mapping)
    assert status == 200

    status_get, got = invoke_token_limits(token_env, "GET", admin)
    assert status_get == 200
    effective = got["model_token_limits"]
    assert effective == mapping

    for variant in variants:
        if variant in mapping:
            # Its own value — never a trimmed or case-folded neighbor's.
            assert (resolve_token_budget(variant, None, effective)
                    == mapping[variant])
        else:
            # Absent variants fall to the default even though a
            # near-identical key is configured.
            assert resolve_token_budget(variant, None, effective) == 10000
