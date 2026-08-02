"""Bug 1 preservation property tests (workflow-designer-bugfixes, task 2).

**Feature: workflow-designer-bugfixes, Property 2: Preservation - Explicit
temperature behavior unchanged**

_For any_ input where the bug condition does NOT hold - a temperature
explicitly stored as a number in [0, 1], or a per-request override supplied -
the pipeline SHALL produce exactly the same result as the original: the
applicable temperature sent as `inferenceConfig.temperature` with `topP`
suppressed; invalid overrides (out of range, non-numeric, boolean) rejected
with 400 `INVALID_TEMPERATURE` before any Bedrock call; `temperature` and
`topP` never sent together; a request without a temperature key (what a blank
GenerateChatPanel field produces) using the configured value; and all
non-temperature settings validation rules unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

PRESERVATION TESTS (observation-first): these encode the observed behavior of
the UNFIXED code for non-buggy inputs and MUST PASS both before and after the
Bug 1 fix - they pin down the baseline the fix must not disturb. Assertions
deliberately avoid anything that differs between the unfixed and fixed code
(e.g. the effective temperature when nothing was stored).

Runs against the shared moto stack from conftest.py with the Bedrock Converse
API mocked exactly as in test_property_bedrock_sampling_unset.py /
test_workflow_generation.py.
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

SETTINGS_TABLE_NAME = "test-settings-sampling-preservation"
CHAT_SESSIONS_TABLE_NAME = "test-workflow-chat-sessions-sampling-preservation"
NODE_GEN_SESSIONS_TABLE_NAME = "test-node-gen-sessions-sampling-preservation"

BEDROCK_CONFIG_RESOURCE_ID = "bedrock-configuration"

# Minimal catalog-conformant Workflow_Definition (as in
# test_workflow_generation.py) so the generated definition validates cleanly.
VALID_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "csi_camera_source",
         "position": {"x": 100, "y": 100}, "parameters": {}},
        {"id": "n2", "type": "capture",
         "position": {"x": 350, "y": 100},
         "parameters": {"output_path": "/data/captures"}},
    ],
    "connections": [
        {"id": "c1",
         "from": {"node": "n1", "port": "out"},
         "to": {"node": "n2", "port": "in"}},
    ],
}

NODE_DECLARATION = {
    "typeId": "custom_blur",
    "displayName": "Custom Blur",
    "description": "Blurs each frame.",
    "category": "preprocessing",
    "inputs": [{"name": "in", "portType": "VideoFrames"}],
    "outputs": [{"name": "out", "portType": "VideoFrames"}],
    "parameters": [],
    "architectures": ["x86_64"],
}


def tool_response(tool_name, tool_input):
    """A Converse API response whose assistant message calls the tool."""
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tool-1", "name": tool_name,
                         "input": tool_input}},
        ]}},
        "stopReason": "tool_use",
    }


@pytest.fixture(scope="module")
def sampling_env(aws_stack):
    """Settings + chat-session tables and freshly imported
    workflow_generator / node_generator / data_accounts modules bound to
    them inside moto, with the Converse clients mocked."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    os.environ["WORKFLOW_CHAT_SESSIONS_TABLE"] = CHAT_SESSIONS_TABLE_NAME
    os.environ["NODE_GEN_SESSIONS_TABLE"] = NODE_GEN_SESSIONS_TABLE_NAME
    os.environ["PLUGIN_SOURCES_PREFIX"] = "plugin-sources"

    client = boto3.client("dynamodb", region_name=REGION)
    for table_name in (SETTINGS_TABLE_NAME, CHAT_SESSIONS_TABLE_NAME,
                       NODE_GEN_SESSIONS_TABLE_NAME):
        key = ("setting_key" if table_name == SETTINGS_TABLE_NAME
               else "session_id")
        client.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key,
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    # Re-import so the modules bind the table names above and
    # moto-intercepted boto3 clients (conftest pattern). node_generator
    # takes get_bedrock_configuration from workflow_generator, and
    # data_accounts serves the settings API.
    for module_name in ("data_accounts", "node_generator",
                        "workflow_generator", "workflow_validation"):
        sys.modules.pop(module_name, None)
    import workflow_generator
    import node_generator
    import data_accounts

    # Mocked Converse clients, one per pipeline, injected permanently
    # through get_bedrock_client (reset per example in the tests).
    wf_client = MagicMock(name="bedrock-runtime-workflow")
    node_client = MagicMock(name="bedrock-runtime-node")
    workflow_generator.get_bedrock_client = lambda region, timeout: wf_client
    node_generator.get_bedrock_client = lambda region, timeout: node_client

    # Run the node-generation worker synchronously in-process so a
    # dispatched turn is settled by the time the start route returns.
    def dispatch(session_id, prompt, user):
        node_generator.handler({
            "node_gen_worker": True,
            "session_id": session_id,
            "prompt": prompt,
            "user": user,
        }, None)
    node_generator.dispatch_generation_worker = dispatch

    from workflow_core.scaffold import render_scaffold
    reference_files = render_scaffold(NODE_DECLARATION)

    # One Use_Case with a DataScientist (workflow generation) and a
    # UseCaseAdmin (node generation), reused across examples.
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "UC", "account_id": "123456789012",
    })

    def make_user(role, usecase_role=None):
        user_id = f"user-{uuid.uuid4()}"
        user = {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}
        if usecase_role:
            aws_stack.tables.user_roles.put_item(Item={
                "user_id": user_id, "usecase_id": usecase_id,
                "role": usecase_role,
            })
        return user

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        workflow_generator=workflow_generator,
        node_generator=node_generator,
        data_accounts=data_accounts,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
        wf_client=wf_client,
        node_client=node_client,
        reference_files=reference_files,
        usecase_id=usecase_id,
        wf_user=make_user("DataScientist", "DataScientist"),
        node_user=make_user("UseCaseAdmin", "UseCaseAdmin"),
        admin=make_user("PortalAdmin"),
    )


# ---------------------------------------------------------------------------
# Store-state setup and invocation helpers
# ---------------------------------------------------------------------------

def _to_ddb(value):
    return Decimal(str(value)) if isinstance(value, float) else value


def store_sampling_config(env, temperature, top_p=None):
    """Store a Bedrock_Configuration item with an explicitly configured
    temperature (a number - the non-bug-condition store states) and an
    optionally stored top_p."""
    env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})
    value = {"temperature": _to_ddb(temperature)}
    if top_p is not None:
        value["top_p"] = _to_ddb(top_p)
    env.settings_table.put_item(Item={
        "setting_key": "bedrock_configuration",
        "value": value,
    })


def clear_stored_config(env):
    env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})


def auth_context(user):
    return {
        "authorizer": {
            "claims": {
                "sub": user["user_id"],
                "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"],
            }
        }
    }


_NO_OVERRIDE = object()


def generate_workflow(env, temperature_override=_NO_OVERRIDE):
    """POST /workflows/generate, optionally carrying a temperature override.

    When no override is given the request body has no temperature key at
    all - exactly the payload a blank GenerateChatPanel temperature field
    produces (Requirement 3.5).
    """
    env.wf_client.converse.reset_mock(return_value=True, side_effect=True)
    env.wf_client.converse.return_value = tool_response(
        "create_workflow", VALID_DEFINITION)
    body = {"usecase_id": env.usecase_id, "prompt": "Camera to capture"}
    if temperature_override is not _NO_OVERRIDE:
        body["temperature"] = temperature_override
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/generate",
        "path": "/workflows/generate",
        "body": json.dumps(body),
        "requestContext": auth_context(env.wf_user),
    }
    response = env.workflow_generator.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def generate_node(env):
    """POST /plugins/generate (start) polled to its settled outcome."""
    env.node_client.converse.reset_mock(return_value=True, side_effect=True)
    env.node_client.converse.return_value = tool_response(
        "create_plugin_scaffold", {"files": dict(env.reference_files)})

    def invoke(resource, method, body=None, session_id=None):
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{session}", session_id or ""),
            "pathParameters": ({"session": session_id} if session_id
                               else None),
            "body": json.dumps(body) if body is not None else None,
            "requestContext": auth_context(env.node_user),
        }
        response = env.node_generator.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    status, body = invoke("/plugins/generate", "POST", {
        "usecase_id": env.usecase_id,
        "prompt": "Blur frames",
        "declaration": NODE_DECLARATION,
    })
    if status != 202:
        return status, body
    poll_status, turn = invoke("/plugins/generate/{session}", "GET",
                               session_id=body["session_id"])
    assert poll_status == 200
    if turn["turn_status"] == "completed":
        return 200, turn
    error = turn["turn_error"]
    return error.get("http_status", 502), {"error": error}


def invoke_settings(env, method, body=None):
    """GET/PUT /data-accounts/bedrock-configuration as PortalAdmin."""
    event = {
        "httpMethod": method,
        "resource": "/data-accounts/{id}",
        "path": f"/data-accounts/{BEDROCK_CONFIG_RESOURCE_ID}",
        "pathParameters": {"id": BEDROCK_CONFIG_RESOURCE_ID},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": auth_context(env.admin),
    }
    response = env.data_accounts.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def captured_inference_config(client):
    assert client.converse.call_count == 1
    return client.converse.call_args.kwargs["inferenceConfig"]


def assert_temperature_sent(inference_config, expected):
    """The applicable temperature is sent, topP suppressed, and the
    never-both invariant holds (Requirements 3.1, 3.2, 3.4)."""
    assert inference_config.get("temperature") == pytest.approx(
        float(expected)), (
        f"expected temperature {expected!r} in {inference_config!r}")
    assert "topP" not in inference_config, (
        f"topP must be suppressed when a temperature applies, got "
        f"{inference_config!r}")
    assert_never_both(inference_config)


def assert_never_both(inference_config):
    """Invariant across all generated cases: temperature and topP are
    never both present (Requirement 3.4)."""
    assert not ("temperature" in inference_config
                and "topP" in inference_config), (
        f"temperature and topP must never be sent together, got "
        f"{inference_config!r}")


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Floats destined for DynamoDB storage are rounded to 6 decimal places:
# raw hypothesis floats include subnormals (e.g. 2.2e-311) below DynamoDB's
# representable number range (magnitude >= 1e-130), which fail PutItem with
# a number underflow before the code under test even runs. Six decimals
# comfortably covers the realistic temperature/top_p input space.
storable_floats = st.floats(
    min_value=0, max_value=1, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6))

# Explicitly stored temperatures: numeric ints (0, 1) and floats in [0, 1]
# (floats are Decimal-encoded on storage, as DynamoDB requires).
stored_temperatures = st.one_of(
    st.integers(min_value=0, max_value=1),
    storable_floats,
)

optional_stored_top_ps = st.one_of(st.none(), storable_floats)

# Valid per-request overrides: numbers in [0, 1] (booleans are NOT valid).
valid_overrides = st.one_of(
    st.integers(min_value=0, max_value=1),
    st.floats(min_value=0, max_value=1,
              allow_nan=False, allow_infinity=False),
)

# Invalid per-request overrides: out-of-range numbers, booleans,
# non-numeric JSON values. None is excluded - it means "no override".
invalid_overrides = st.one_of(
    st.floats(min_value=1, max_value=1e6, exclude_min=True,
              allow_nan=False, allow_infinity=False),
    st.floats(min_value=-1e6, max_value=0, exclude_max=True,
              allow_nan=False, allow_infinity=False),
    st.integers(min_value=2, max_value=1000),
    st.integers(min_value=-1000, max_value=-1),
    st.booleans(),
    st.text(max_size=10),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
)

# Invalid stored sampling values for the settings PUT: out-of-range numbers,
# booleans, non-numbers. Explicit null is excluded - its acceptance is the
# Bug 1 fix itself (exploration test), not preserved behavior.
invalid_sampling_values = st.one_of(
    st.floats(min_value=1, max_value=100, exclude_min=True,
              allow_nan=False, allow_infinity=False),
    st.floats(min_value=-100, max_value=0, exclude_max=True,
              allow_nan=False, allow_infinity=False),
    st.integers(min_value=2, max_value=100),
    st.integers(min_value=-100, max_value=-1),
    st.booleans(),
    st.text(max_size=10),
    st.lists(st.integers(), max_size=2),
)


# ---------------------------------------------------------------------------
# Property 2a: an explicitly stored temperature keeps being sent
#              (workflow generation), topP suppressed (Req 3.1, 3.4, 3.5)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(temperature=stored_temperatures, top_p=optional_stored_top_ps)
def test_workflow_generation_sends_stored_temperature(
        sampling_env, temperature, top_p):
    """For any explicitly stored temperature in [0, 1] (with or without a
    stored top_p) and a generation request without a temperature key (the
    blank-GenerateChatPanel payload), the stored temperature is sent as
    inferenceConfig.temperature and topP is suppressed
    (Requirements 3.1, 3.4, 3.5)."""
    store_sampling_config(sampling_env, temperature, top_p)

    status, _ = generate_workflow(sampling_env)
    assert status == 200

    inference_config = captured_inference_config(sampling_env.wf_client)
    assert_temperature_sent(inference_config, temperature)


# ---------------------------------------------------------------------------
# Property 2b: node scaffold generation keeps sending the stored
#              temperature (Req 3.1, 3.4)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(temperature=stored_temperatures, top_p=optional_stored_top_ps)
def test_node_generation_sends_stored_temperature(
        sampling_env, temperature, top_p):
    """For any explicitly stored temperature in [0, 1], node scaffold
    generation (node_generator reuses get_bedrock_configuration()) keeps
    sending it as inferenceConfig.temperature with topP suppressed
    (Requirements 3.1, 3.4)."""
    store_sampling_config(sampling_env, temperature, top_p)

    status, _ = generate_node(sampling_env)
    assert status == 200

    inference_config = captured_inference_config(sampling_env.node_client)
    assert_temperature_sent(inference_config, temperature)


# ---------------------------------------------------------------------------
# Property 2c: a valid override replaces the configured temperature for
#              that invocation only (Req 3.2, 3.4)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(stored=stored_temperatures, top_p=optional_stored_top_ps,
       override=valid_overrides)
def test_valid_override_replaces_stored_temperature_for_one_invocation(
        sampling_env, stored, top_p, override):
    """For any explicitly stored temperature and any valid override
    (a number in [0, 1]), the override is sent for that invocation
    (topP suppressed), and a subsequent invocation without an override
    reverts to the stored temperature (Requirements 3.2, 3.4)."""
    store_sampling_config(sampling_env, stored, top_p)

    # Overridden invocation: the override wins.
    status, _ = generate_workflow(sampling_env,
                                  temperature_override=override)
    assert status == 200
    inference_config = captured_inference_config(sampling_env.wf_client)
    assert_temperature_sent(inference_config, override)

    # Next invocation without an override: back to the stored value -
    # the override applied to that invocation only.
    status, _ = generate_workflow(sampling_env)
    assert status == 200
    inference_config = captured_inference_config(sampling_env.wf_client)
    assert_temperature_sent(inference_config, stored)


# ---------------------------------------------------------------------------
# Property 2d: invalid overrides keep rejecting with 400 INVALID_TEMPERATURE
#              before any Bedrock call (Req 3.3)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(stored=stored_temperatures, override=invalid_overrides)
def test_invalid_override_rejected_before_bedrock_call(
        sampling_env, stored, override):
    """For any invalid per-request temperature (out of range, non-numeric,
    or boolean), the request is rejected with 400 INVALID_TEMPERATURE and
    Bedrock is never invoked (Requirement 3.3)."""
    store_sampling_config(sampling_env, stored)

    status, body = generate_workflow(sampling_env,
                                     temperature_override=override)
    assert status == 400, (
        f"override {override!r} must be rejected, got {status}: {body!r}")
    assert body["error"]["code"] == "INVALID_TEMPERATURE"
    assert sampling_env.wf_client.converse.call_count == 0, (
        "Bedrock must not be invoked when the override is invalid")


# ---------------------------------------------------------------------------
# Property 2e: settings validation keeps rejecting invalid sampling values
#              and accepting numeric ones (Req 3.1)
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(key=st.sampled_from(["temperature", "top_p"]),
       value=invalid_sampling_values)
def test_settings_rejects_invalid_sampling_values(sampling_env, key, value):
    """PUT bedrock-configuration with an out-of-range, boolean, or
    non-numeric temperature/top_p keeps rejecting with 400 and the
    established validation message (unchanged validation rules)."""
    clear_stored_config(sampling_env)

    status, body = invoke_settings(sampling_env, "PUT", {key: value})
    assert status == 400, (
        f"{key}={value!r} must be rejected, got {status}: {body!r}")
    assert f"{key} must be a number between 0 and 1" \
        in body["validation_errors"]


@settings(deadline=None)
@given(temperature=stored_temperatures, top_p=storable_floats)
def test_settings_accepts_numeric_sampling_values_and_round_trips(
        sampling_env, temperature, top_p):
    """PUT bedrock-configuration with numeric temperature/top_p in [0, 1]
    keeps being accepted with 200 and round-trips through GET
    (Requirement 3.1)."""
    clear_stored_config(sampling_env)

    status, body = invoke_settings(sampling_env, "PUT", {
        "temperature": temperature, "top_p": top_p,
    })
    assert status == 200, f"got {status}: {body!r}"
    assert body["bedrock_configuration"]["temperature"] == pytest.approx(
        temperature)
    assert body["bedrock_configuration"]["top_p"] == pytest.approx(top_p)

    status, body = invoke_settings(sampling_env, "GET")
    assert status == 200
    assert body["bedrock_configuration"]["temperature"] == pytest.approx(
        temperature)
    assert body["bedrock_configuration"]["top_p"] == pytest.approx(top_p)


# ---------------------------------------------------------------------------
# Property 2f: non-temperature settings validation rules unchanged
# ---------------------------------------------------------------------------

@st.composite
def non_sampling_field_cases(draw):
    """(field, value, valid, error_snippet) over the non-sampling
    Bedrock_Configuration fields' validation rules as they stand today."""
    field = draw(st.sampled_from(
        ["model_id", "region", "max_tokens", "timeout_seconds"]))
    valid = draw(st.booleans())

    if field in ("model_id", "region"):
        if valid:
            value = draw(st.text(min_size=1, max_size=40).filter(
                lambda s: s.strip()))
        else:
            value = draw(st.one_of(
                st.just(""), st.just("   "),
                st.integers(), st.booleans(), st.none(),
                st.lists(st.text(max_size=3), max_size=2),
            ))
        snippet = f"{field} must be a non-empty string"
    elif field == "max_tokens":
        if valid:
            value = draw(st.integers(min_value=1, max_value=100000))
        else:
            value = draw(st.one_of(
                st.integers(min_value=-1000, max_value=0),
                st.booleans(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=10), st.none(),
            ))
        snippet = "max_tokens must be a positive integer"
    else:  # timeout_seconds: integer in [1, 60]
        if valid:
            value = draw(st.integers(min_value=1, max_value=60))
        else:
            value = draw(st.one_of(
                st.integers(min_value=-1000, max_value=0),
                st.integers(min_value=61, max_value=1000),
                st.booleans(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=10), st.none(),
            ))
        snippet = "timeout_seconds must be an integer between 1 and 60"
    return field, value, valid, snippet


@settings(deadline=None)
@given(case=non_sampling_field_cases())
def test_settings_non_sampling_rules_unchanged(sampling_env, case):
    """model_id / region / max_tokens / timeout_seconds keep accepting and
    rejecting exactly as today (unchanged validation rules, timeout clamp
    domain 1..60)."""
    field, value, valid, snippet = case
    clear_stored_config(sampling_env)

    status, body = invoke_settings(sampling_env, "PUT", {field: value})
    if valid:
        assert status == 200, (
            f"valid {field}={value!r} must be accepted, got {status}: "
            f"{body!r}")
    else:
        assert status == 400, (
            f"invalid {field}={value!r} must be rejected, got {status}: "
            f"{body!r}")
        assert snippet in body["validation_errors"]
