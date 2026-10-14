"""Property test for global Bedrock configuration preservation
(llm-model-token-and-image-sizing, task 5.3).

**Feature: llm-model-token-and-image-sizing, Property 3: Global Bedrock
configuration semantics are preserved for every other consumer**

_For any_ stored Bedrock_Configuration (fields present, absent, null, or
malformed) and _any_ submitted partial change, the resolved configuration
and the inference configuration built for a Bedrock_Consumer SHALL equal
the pre-feature results: `maxTokens` from the Global_Max_Tokens,
`temperature` when set, `topP` only when temperature is unset and top_p is
set, never both, omitted fields left at their current effective values,
per-field pre-feature defaults for absent or uncoercible fields, and
`timeout_seconds` coerced and clamped into 1 to 240 inclusive.

**Validates: Requirements 1.5, 4.5, 4.6, 10.2, 10.3, 10.5, 10.8, 10.9**

Differential test: every outcome is compared against a PINNED in-test
reimplementation of the pre-feature rules (resolution, inference-config
construction, and the settings PUT's merge-then-validate), written from
literals and never calling the code under test. A populated
`llm_model_token_limits` settings item sits in the same table for every
example — including an entry for the effective model identifier — and the
workflow-generation and node-designer consumers' captured Converse kwargs
and client construction args are asserted invariant to that item's content
(Requirements 1.5, 10.5).

The store states go through a real (moto) DynamoDB put/get round trip, so
numbers arrive as Decimal exactly as in production. The generators are the
union of the ones used by test_property_bedrock_config_resolution.py
(subsets of known keys, extra keys, Decimal numbers, explicit nulls, junk
timeout_seconds, nested and flat item shapes, no item at all) and
test_property_bedrock_sampling_exclusivity.py (set/unset/None crossed over
temperature and top_p), extended with `max_tokens` values above the
128000 Model_Token_Limit_Ceiling — which must never be clamped
(Requirement 4.5's "no upper bound", "ceiling applied to no field").

Pinning note on Requirement 10.3 and boolean timeouts: the pre-feature
code coerces `timeout_seconds` with `int()`, and Python booleans coerce
(`int(True) == 1`, `int(False) == 0` -> clamped to 1). The untouched
baseline test test_property_bedrock_config_resolution.py pins exactly that
behavior, so this preservation differential pins it identically rather
than inventing a boolean -> 240 substitution that the pre-feature code
never performed.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-bedrock-global-preservation"

BEDROCK_CONFIG_SETTING_KEY = "bedrock_configuration"
TOKEN_LIMITS_SETTING_KEY = "llm_model_token_limits"

KNOWN_KEYS = ("model_id", "region", "max_tokens",
              "temperature", "top_p", "timeout_seconds")
SAMPLING_KEYS = ("temperature", "top_p")

# Constant consumer inputs (the node designer's system prompt and tool
# config are plain pass-through arguments of its invoke_generation).
NODE_SYSTEM_PROMPT = "You are generating a plugin scaffold."
NODE_TOOL_CONFIG = {"tools": []}


# ---------------------------------------------------------------------------
# Pinned pre-feature rules (the differential baseline).
# Written from literals only — never from the modules under test.
# ---------------------------------------------------------------------------

# bedrock_common.DEFAULT_BEDROCK_CONFIG as it stood before this feature.
# The region default is os.environ['AWS_REGION'], which conftest pins to
# us-east-1 (= conftest.REGION) before anything imports.
PINNED_DEFAULTS = {
    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "region": REGION,
    "max_tokens": 4096,
    "temperature": None,
    "top_p": None,
    "timeout_seconds": 240,
}


def pinned_resolved_configuration(stored_native):
    """The pre-feature resolution rules (Requirements 10.3, 10.9).

    Defaults overridden by present non-null stored values; the sampling
    parameters honor an explicitly stored null (present key wins, even
    when null); extra stored keys never leak through; timeout_seconds
    coerced with int() (booleans coerce: True -> 1, False -> 0) and
    clamped into [1, 240], with 240 substituted when the stored value is
    absent, null, or cannot be coerced to an integer.
    """
    config = dict(PINNED_DEFAULTS)
    if stored_native is not None:
        for key in PINNED_DEFAULTS:
            if key in SAMPLING_KEYS:
                if key in stored_native:
                    config[key] = stored_native[key]
            elif stored_native.get(key) is not None:
                config[key] = stored_native[key]
    try:
        timeout = int(config["timeout_seconds"])
    except (TypeError, ValueError):
        timeout = 240
    config["timeout_seconds"] = max(1, min(timeout, 240))
    return config


def pinned_inference_config(resolved):
    """The pre-feature inference configuration (Requirements 10.2, 10.8).

    maxTokens from the Global_Max_Tokens; temperature when set; topP only
    when temperature is unset and top_p is set; never both; nothing else.
    """
    inference_config = {"maxTokens": int(resolved["max_tokens"])}
    if resolved.get("temperature") is not None:
        inference_config["temperature"] = float(resolved["temperature"])
    elif resolved.get("top_p") is not None:
        inference_config["topP"] = float(resolved["top_p"])
    return inference_config


def pinned_validation_errors(config):
    """The pre-feature Bedrock_Configuration validation (Requirement 4.5).

    model_id / region non-empty after trimming; max_tokens a non-boolean
    integer of at least 1 with NO upper bound; temperature / top_p unset
    or numbers in [0, 1]; timeout_seconds a non-boolean integer in
    [1, 240]. The 128000 Model_Token_Limit_Ceiling applies to no field.
    """
    errors = []
    model_id = config.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        errors.append("model_id must be a non-empty string")
    region = config.get("region")
    if not isinstance(region, str) or not region.strip():
        errors.append("region must be a non-empty string")
    max_tokens = config.get("max_tokens")
    if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
            or max_tokens < 1):
        errors.append("max_tokens must be a positive integer")
    for key in SAMPLING_KEYS:
        value = config.get(key)
        if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (0 <= value <= 1)):
            errors.append(f"{key} must be a number between 0 and 1")
    timeout = config.get("timeout_seconds")
    if (not isinstance(timeout, int) or isinstance(timeout, bool)
            or not (1 <= timeout <= 240)):
        errors.append("timeout_seconds must be an integer between 1 and 240")
    return errors


# ---------------------------------------------------------------------------
# Fixture: settings table + freshly imported consumer and settings modules
# bound to it inside moto, with recording Converse client factories.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def preservation_env(aws_stack):
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    # Table names the consumer modules read at import; the session stores
    # are never touched by the paths this test drives.
    os.environ["WORKFLOW_CHAT_SESSIONS_TABLE"] = (
        "test-chat-sessions-global-preservation")
    os.environ["NODE_GEN_SESSIONS_TABLE"] = (
        "test-node-gen-sessions-global-preservation")
    os.environ["PLUGIN_SOURCES_PREFIX"] = "plugin-sources"
    # The environment bootstrap must not shadow the persisted token-limits
    # item this test plants (its content is the variable under test).
    os.environ.pop("LLM_MODEL_TOKEN_LIMITS", None)

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the modules bind SETTINGS_TABLE above and
    # moto-intercepted boto3 clients (conftest pattern).
    # workflow_generator reloads bedrock_common itself; node_generator
    # takes get_bedrock_configuration / get_bedrock_client from
    # workflow_generator; data_accounts serves the settings API.
    for module_name in ("data_accounts", "node_generator",
                        "workflow_generator", "workflow_validation",
                        "code_assist", "bedrock_common"):
        sys.modules.pop(module_name, None)
    import workflow_generator
    import node_generator
    import data_accounts
    import bedrock_common

    # Recording Converse client factories: capture the client construction
    # args (region, timeout_seconds) each consumer uses, and hand back a
    # per-consumer mock client (Requirement 10.5's "client construction").
    wf_client = MagicMock(name="bedrock-runtime-workflow")
    node_client = MagicMock(name="bedrock-runtime-node")
    wf_client_args, node_client_args = [], []

    def wf_factory(region, timeout_seconds):
        wf_client_args.append((region, timeout_seconds))
        return wf_client

    def node_factory(region, timeout_seconds):
        node_client_args.append((region, timeout_seconds))
        return node_client

    workflow_generator.get_bedrock_client = wf_factory
    node_generator.get_bedrock_client = node_factory

    admin_id = f"user-{uuid.uuid4()}"
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        workflow_generator=workflow_generator,
        node_generator=node_generator,
        data_accounts=data_accounts,
        bedrock_common=bedrock_common,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
        wf_client=wf_client,
        node_client=node_client,
        wf_client_args=wf_client_args,
        node_client_args=node_client_args,
        wf_messages=workflow_generator.converse_messages(
            [], "Connect a camera to a capture node."),
        node_messages=node_generator.converse_messages(
            [], "Blur each frame."),
        admin={"user_id": admin_id, "email": f"{admin_id}@example.com",
               "username": admin_id, "role": "PortalAdmin"},
    )


# ---------------------------------------------------------------------------
# DynamoDB round-trip helpers (as in the untouched baseline test)
# ---------------------------------------------------------------------------

def to_ddb(value):
    """Native Python -> DynamoDB-storable (floats become Decimal)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def expected_native(value):
    """A stored native value after the DynamoDB Decimal round trip and the
    modules' Decimal-to-native conversion: whole numbers come back as int,
    fractional ones as float, everything else unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        d = Decimal(str(value))
        return float(d) if d % 1 else int(d)
    if isinstance(value, list):
        return [expected_native(v) for v in value]
    return value


def native_stored(stored):
    return ({key: expected_native(value) for key, value in stored.items()}
            if stored is not None else None)


def apply_store_state(env, shape, stored):
    """Materialize a Bedrock_Configuration store state: no item, the
    production nested shape ({setting_key, value: {...}}), or the
    also-accepted flat shape."""
    env.settings_table.delete_item(
        Key={"setting_key": BEDROCK_CONFIG_SETTING_KEY})
    if shape == "no_item":
        return
    if shape == "nested":
        env.settings_table.put_item(Item={
            "setting_key": BEDROCK_CONFIG_SETTING_KEY,
            "value": to_ddb(stored),
        })
    else:  # flat: attributes directly on the item
        item = {"setting_key": BEDROCK_CONFIG_SETTING_KEY}
        item.update(to_ddb(stored))
        env.settings_table.put_item(Item=item)


def set_token_limits_item(env, mapping):
    """Write (or remove, for None) the Model_Token_Limits settings item."""
    env.settings_table.delete_item(
        Key={"setting_key": TOKEN_LIMITS_SETTING_KEY})
    if mapping is not None:
        env.settings_table.put_item(Item={
            "setting_key": TOKEN_LIMITS_SETTING_KEY,
            "value": to_ddb(mapping),
        })


def raw_config_item(env):
    """The raw stored bedrock_configuration item, or None."""
    response = env.settings_table.get_item(
        Key={"setting_key": BEDROCK_CONFIG_SETTING_KEY})
    return response.get("Item")


# ---------------------------------------------------------------------------
# Consumer and settings invocation helpers
# ---------------------------------------------------------------------------

def _tool_response(tool_name):
    """A Converse response whose assistant message calls the tool, so each
    consumer's invoke_generation returns without an error branch."""
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tool-1", "name": tool_name,
                         "input": {"generated": True}}},
        ]}},
        "stopReason": "tool_use",
    }


def run_consumers(env):
    """Drive both Bedrock_Consumers exactly as their handlers do — resolve
    the configuration, then invoke — and capture the Converse kwargs and
    the client construction args of each."""
    env.wf_client.converse.reset_mock(return_value=True, side_effect=True)
    env.wf_client.converse.return_value = _tool_response(
        env.workflow_generator.TOOL_NAME)
    env.node_client.converse.reset_mock(return_value=True, side_effect=True)
    env.node_client.converse.return_value = _tool_response(
        env.node_generator.TOOL_NAME)
    env.wf_client_args.clear()
    env.node_client_args.clear()

    # Workflow generation: config = get_bedrock_configuration();
    # invoke_generation(config, messages) — as in generate_workflow().
    wf_config = env.workflow_generator.get_bedrock_configuration()
    _, _, wf_err = env.workflow_generator.invoke_generation(
        wf_config, env.wf_messages)
    assert wf_err is None, f"workflow consumer invocation failed: {wf_err!r}"

    # Node designer: config = get_bedrock_configuration();
    # invoke_generation(config, system_prompt, messages, tool_config).
    node_config = env.node_generator.get_bedrock_configuration()
    _, _, node_err = env.node_generator.invoke_generation(
        node_config, NODE_SYSTEM_PROMPT, env.node_messages, NODE_TOOL_CONFIG)
    assert node_err is None, f"node consumer invocation failed: {node_err!r}"

    assert env.wf_client.converse.call_count == 1
    assert env.node_client.converse.call_count == 1
    return SimpleNamespace(
        wf_kwargs=env.wf_client.converse.call_args.kwargs,
        node_kwargs=env.node_client.converse.call_args.kwargs,
        wf_client_args=tuple(env.wf_client_args),
        node_client_args=tuple(env.node_client_args),
        wf_config=wf_config,
        node_config=node_config,
    )


def invoke_settings_put(env, change):
    """PUT /data-accounts/bedrock-configuration through the real handler
    (real route, RBAC gate, merge and validation) as PortalAdmin."""
    event = {
        "httpMethod": "PUT",
        "resource": "/data-accounts/{id}",
        "path": "/data-accounts/bedrock-configuration",
        "pathParameters": {"id": "bedrock-configuration"},
        "body": json.dumps(change),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": env.admin["user_id"],
                    "email": env.admin["email"],
                    "cognito:username": env.admin["username"],
                    "custom:role": env.admin["role"],
                }
            }
        },
    }
    response = env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Generators — the union of the baseline tests' generators, extended with
# above-ceiling max_tokens values.
# ---------------------------------------------------------------------------

# Floats destined for DynamoDB are rounded: raw hypothesis floats include
# magnitudes below DynamoDB's representable range, which fail PutItem
# before the code under test runs.
storable_unit_floats = st.floats(
    min_value=0, max_value=1, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6))

storable_strings = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1, max_size=40,
)

# A set sampling parameter (test_property_bedrock_sampling_exclusivity's
# value pool): ints 0/1 and unit floats.
set_sampling_values = st.one_of(
    st.integers(min_value=0, max_value=1),
    storable_unit_floats,
)

# max_tokens: the baseline pool extended with integers above the 128000
# Model_Token_Limit_Ceiling, which resolution must return unclamped
# (Requirements 4.5, 10.9's per-field defaults for None).
stored_max_tokens = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=100000),
    st.integers(min_value=128001, max_value=500000),
    st.floats(min_value=1, max_value=100000,
              allow_nan=False, allow_infinity=False)
    .map(lambda x: round(x, 3)),
)

# Interpretable (in- and out-of-range ints, Decimal floats, booleans) and
# uninterpretable (null, letter strings, lists) timeout values (Req 10.3).
stored_timeouts = st.one_of(
    st.none(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(min_value=-100, max_value=200,
              allow_nan=False, allow_infinity=False)
    .map(lambda x: round(x, 3)),
    st.booleans(),
    st.text(alphabet="abcdefghijxyz ", min_size=1, max_size=8),
    st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=3),
)

NON_SAMPLING_KEY_VALUES = {
    "model_id": st.one_of(st.none(), storable_strings),
    "region": st.one_of(st.none(), storable_strings),
    "max_tokens": stored_max_tokens,
    "timeout_seconds": stored_timeouts,
}

extra_keys = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12,
).filter(lambda k: k not in KNOWN_KEYS and k not in ("value", "setting_key"))

extra_values = st.one_of(
    st.none(),
    st.integers(min_value=-100, max_value=100),
    storable_strings,
)


@st.composite
def stored_bedrock_items(draw):
    """(shape, stored): nothing stored, or a nested/flat item over any
    subset of the non-sampling keys, with each sampling parameter
    independently absent / explicitly null / set (the exclusivity test's
    tri-state), plus arbitrary extra keys."""
    shape = draw(st.sampled_from(["no_item", "nested", "flat"]))
    if shape == "no_item":
        return shape, None
    stored = {}
    for key in ("model_id", "region", "max_tokens", "timeout_seconds"):
        if draw(st.booleans()):
            stored[key] = draw(NON_SAMPLING_KEY_VALUES[key])
    for key in SAMPLING_KEYS:
        state = draw(st.sampled_from(["absent", "null", "set"]))
        if state == "null":
            stored[key] = None
        elif state == "set":
            stored[key] = draw(set_sampling_values)
    stored.update(draw(st.dictionaries(extra_keys, extra_values, max_size=3)))
    return shape, stored


# Populated Model_Token_Limits mappings (valid entries, as the Settings_API
# would persist them); an entry for the effective model identifier is
# planted separately in each test.
model_identifier_keys = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1, max_size=32,
)

token_limit_mappings = st.dictionaries(
    model_identifier_keys, st.integers(min_value=1, max_value=128000),
    min_size=1, max_size=8,
)


@st.composite
def valid_partial_changes(draw):
    """A partial Bedrock_Configuration change whose submitted fields are
    each valid under the pre-feature rules — including max_tokens above
    the 128000 ceiling, whitespace-padded strings (trimmed on write), and
    explicit nulls that unset a sampling parameter."""
    change = {}
    for key in draw(st.lists(st.sampled_from(KNOWN_KEYS), unique=True)):
        if key in ("model_id", "region"):
            padding = st.sampled_from(["", " ", "  "])
            change[key] = (draw(padding) + draw(storable_strings)
                           + draw(padding))
        elif key == "max_tokens":
            change[key] = draw(st.one_of(
                st.integers(min_value=1, max_value=128000),
                st.integers(min_value=128001, max_value=1_000_000),
            ))
        elif key in SAMPLING_KEYS:
            change[key] = draw(st.one_of(st.none(), set_sampling_values))
        else:  # timeout_seconds
            change[key] = draw(st.integers(min_value=1, max_value=240))
    return change


# ---------------------------------------------------------------------------
# Property 3, part 1: resolution and inference-config construction equal
# the pinned pre-feature rules with a populated token-limits item present
# (Requirements 10.2, 10.3, 10.8, 10.9, 1.5's maxTokens source).
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(case=stored_bedrock_items(),
       limits=token_limit_mappings,
       planted_budget=st.integers(min_value=1, max_value=128000))
def test_resolution_and_inference_config_match_pre_feature_rules(
        preservation_env, case, limits, planted_budget):
    """For any stored Bedrock_Configuration — fields present, absent,
    null, or malformed — get_bedrock_configuration() and
    build_inference_config() equal the pinned pre-feature reimplementation
    even with a populated Model_Token_Limits item (including an entry for
    the effective model identifier) in the same table, resolution reports
    no error and leaves the stored item unchanged, and the inference
    configuration carries maxTokens from the Global_Max_Tokens with at
    most one sampling parameter (Requirements 1.5, 10.2, 10.3, 10.8,
    10.9)."""
    env = preservation_env
    shape, stored = case
    apply_store_state(env, shape, stored)

    expected = pinned_resolved_configuration(native_stored(stored))

    # The populated token-limits item, with an entry for the effective
    # model identifier — the sharpest value a leak would pick up.
    mapping = dict(limits)
    mapping[expected["model_id"]] = planted_budget
    set_token_limits_item(env, mapping)

    raw_before = raw_config_item(env)

    resolved = env.bedrock_common.get_bedrock_configuration()

    # Differential: the pre-feature resolution, field for field. Covers
    # per-field defaults for absent/null values (Req 10.9), explicit-null
    # sampling parameters, unclamped max_tokens above 128000, and the
    # timeout coercion and clamping rules (Req 10.3).
    assert resolved == expected, (
        f"stored {stored!r} ({shape}) must resolve to the pre-feature "
        f"configuration {expected!r}, got {resolved!r}")
    assert set(resolved) == set(KNOWN_KEYS), (
        f"extra stored keys must never leak through, got {set(resolved)!r}")
    timeout = resolved["timeout_seconds"]
    assert isinstance(timeout, int) and not isinstance(timeout, bool)
    assert 1 <= timeout <= 240

    inference_config = env.bedrock_common.build_inference_config(resolved)
    assert inference_config == pinned_inference_config(expected), (
        f"inference config for {resolved!r} must equal the pre-feature "
        f"result, got {inference_config!r}")

    # maxTokens comes from the Global_Max_Tokens, not from the planted
    # token-limits entry for this very model id (Requirement 1.5).
    assert inference_config["maxTokens"] == int(expected["max_tokens"])
    # Never both sampling parameters; nothing beyond the known keys
    # (Requirements 10.2, 10.8).
    assert not {"temperature", "topP"} <= set(inference_config)
    assert set(inference_config) <= {"maxTokens", "temperature", "topP"}

    # Resolution reported no error (it returned) and left the stored item
    # unchanged (Requirement 10.9).
    assert raw_config_item(env) == raw_before


# ---------------------------------------------------------------------------
# Property 3, part 2: the workflow-generation and node-designer consumers'
# captured Converse kwargs and client construction args are invariant to
# the Model_Token_Limits item's content (Requirements 1.5, 10.5).
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(case=stored_bedrock_items(),
       limits_a=st.one_of(st.none(), token_limit_mappings),
       limits_b=token_limit_mappings,
       budget_a=st.integers(min_value=1, max_value=60000),
       budget_b=st.integers(min_value=60001, max_value=128000))
def test_consumer_requests_invariant_to_token_limits_item(
        preservation_env, case, limits_a, limits_b, budget_a, budget_b):
    """For any stored Bedrock_Configuration, the Converse kwargs and the
    client construction args of the workflow-generation and node-designer
    consumers are identical whether the Model_Token_Limits item is absent
    or populated — including an entry for the very model id in use — and
    equal the pinned pre-feature request: maxTokens from the
    Global_Max_Tokens, criterion-2 sampling rules, client built from the
    resolved region and clamped timeout (Requirements 1.5, 10.2, 10.3,
    10.5, 10.8)."""
    env = preservation_env
    shape, stored = case
    apply_store_state(env, shape, stored)

    expected = pinned_resolved_configuration(native_stored(stored))

    # Two token-limits store states whose content differs at the entry for
    # the effective model identifier (budget_a != budget_b by range).
    state_a = (None if limits_a is None
               else {**limits_a, expected["model_id"]: budget_a})
    state_b = {**limits_b, expected["model_id"]: budget_b}

    set_token_limits_item(env, state_a)
    capture_a = run_consumers(env)
    set_token_limits_item(env, state_b)
    capture_b = run_consumers(env)

    # Invariance: no field of either consumer's request or client
    # construction moves with the token-limits content (Req 1.5, 10.5).
    assert capture_a.wf_kwargs == capture_b.wf_kwargs, (
        "the workflow-generation Converse request must be invariant to "
        "the Model_Token_Limits item")
    assert capture_a.node_kwargs == capture_b.node_kwargs, (
        "the node-designer Converse request must be invariant to the "
        "Model_Token_Limits item")
    assert capture_a.wf_client_args == capture_b.wf_client_args
    assert capture_a.node_client_args == capture_b.node_client_args

    # Each consumer resolved the pre-feature configuration (Req 10.5's
    # criterion-3 resolution) ...
    assert capture_a.wf_config == expected
    assert capture_a.node_config == expected

    # ... built the pre-feature inference configuration from the
    # Global_Max_Tokens (Req 1.5, 10.2, 10.8) ...
    expected_inference = pinned_inference_config(expected)
    for kwargs in (capture_a.wf_kwargs, capture_a.node_kwargs):
        assert kwargs["inferenceConfig"] == expected_inference
        assert kwargs["modelId"] == expected["model_id"]

    # ... and constructed its client from the resolved region and the
    # coerced, clamped timeout, exactly once (Req 10.3, 10.5).
    expected_client_args = ((expected["region"],
                             expected["timeout_seconds"]),)
    assert capture_a.wf_client_args == expected_client_args
    assert capture_a.node_client_args == expected_client_args


# ---------------------------------------------------------------------------
# Property 3, part 3: a partial PUT merges over the current effective
# values, validates with the pre-feature rules, and applies no ceiling to
# max_tokens (Requirements 4.5, 4.6).
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(case=stored_bedrock_items(),
       change=valid_partial_changes(),
       limits=token_limit_mappings)
def test_partial_put_merges_over_effective_values_with_no_ceiling(
        preservation_env, case, change, limits):
    """For any pre-existing store state and any submitted partial change
    with field-wise valid values, the settings PUT merges the submitted
    fields over the current effective configuration, validates the merged
    whole with the pre-feature rules, leaves every omitted field at its
    current effective value, applies no ceiling to max_tokens (values
    above 128000 persist and resolve unclamped), and persists nothing when
    the merged result is invalid (Requirements 4.5, 4.6)."""
    env = preservation_env
    shape, stored = case
    apply_store_state(env, shape, stored)
    set_token_limits_item(env, limits)  # populated item in the same table

    effective_before = pinned_resolved_configuration(native_stored(stored))
    merged = dict(effective_before)
    merged.update(change)
    expected_errors = pinned_validation_errors(merged)
    raw_before = raw_config_item(env)

    status, body = invoke_settings_put(env, change)

    if expected_errors:
        # Pre-feature rejection: merged-whole validation failed (e.g. a
        # malformed stored max_tokens left effective by an omitting
        # change); nothing is persisted.
        assert status == 400, (
            f"change {change!r} over effective {effective_before!r} must "
            f"be rejected as pre-feature, got {status}: {body!r}")
        assert set(body["validation_errors"]) == set(expected_errors)
        assert raw_config_item(env) == raw_before, (
            "a rejected change must leave the stored item unchanged")
        return

    assert status == 200, (
        f"valid change {change!r} over effective {effective_before!r} "
        f"must be accepted, got {status}: {body!r}")

    expected_persisted = dict(merged)
    expected_persisted["model_id"] = expected_persisted["model_id"].strip()
    expected_persisted["region"] = expected_persisted["region"].strip()

    # The response is the merged configuration: submitted fields applied,
    # every omitted field at its current effective value (Req 4.6).
    assert body["bedrock_configuration"] == expected_persisted
    for key in KNOWN_KEYS:
        if key not in change:
            assert body["bedrock_configuration"][key] == \
                effective_before[key], (
                f"omitted field {key} must keep its current effective "
                f"value {effective_before[key]!r}")

    # No ceiling applied to any field: the persisted value resolves back
    # exactly — max_tokens above 128000 included (Req 4.5).
    expected_resolved_after = {
        key: expected_native(value)
        for key, value in expected_persisted.items()
    }
    resolved_after = env.bedrock_common.get_bedrock_configuration()
    assert resolved_after == expected_resolved_after, (
        f"persisted {expected_persisted!r} must resolve back unclamped, "
        f"got {resolved_after!r}")
    if "max_tokens" in change and change["max_tokens"] > 128000:
        assert resolved_after["max_tokens"] == change["max_tokens"], (
            "the Model_Token_Limit_Ceiling must not be applied to "
            "max_tokens")
