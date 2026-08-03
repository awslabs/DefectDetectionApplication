"""Bug 1 bug condition exploration property test (workflow-designer-bugfixes,
task 1).

**Feature: workflow-designer-bugfixes, Property 1: Bug Condition - Temperature
omitted when unset**

_For any_ Bedrock configuration store state in which no temperature value was
explicitly stored (nothing stored at all, a stored item without the
`temperature` key, or a stored explicit null) and any generation request
without a temperature override, the `get_bedrock_configuration()` +
`invoke_generation()` pipeline (workflow and node generators alike) SHALL
produce an `inferenceConfig` containing no `temperature` key - and no `topP`
key unless a top_p was explicitly stored; and _for any_ settings save with a
blank/null temperature (or top_p), validation SHALL accept the save and store
the parameter as unset, round-tripping as unset through GET
(`read_stored_bedrock_configuration`).

**Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3**

EXPLORATION TEST: this encodes the EXPECTED (fixed) behavior and is EXPECTED
TO FAIL on the unfixed code - the failure (with its counterexamples) confirms
the bug exists (isBugCondition1 in design.md). After the Bug 1 fix it must
pass unchanged.

Runs against the shared moto stack from conftest.py with the Bedrock Converse
API mocked exactly as in test_workflow_generation.py /
test_node_generator_integration.py.
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

from conftest import REGION, TEST_ENV

SETTINGS_TABLE_NAME = "test-settings-sampling-unset"
CHAT_SESSIONS_TABLE_NAME = "test-workflow-chat-sessions-sampling-unset"
NODE_GEN_SESSIONS_TABLE_NAME = "test-node-gen-sessions-sampling-unset"

BEDROCK_CONFIG_RESOURCE_ID = "bedrock-configuration"

# Minimal catalog-conformant Workflow_Definition (as in
# test_workflow_generation.py) so the generated definition validates cleanly
# and passes the Generation_Gate (csi_camera_source is a real input-category
# catalog type; a nonexistent type would leave the graph with no input node
# and be rejected by the gate).
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


def apply_store_state(env, state, top_p):
    """Materialize an unset-temperature Bedrock_Configuration store state.

    state:
      'no_item'            - nothing stored at all
      'no_temperature_key' - stored item whose value has no temperature key
      'explicit_null'      - stored item with temperature explicitly null
    top_p: an explicitly stored top_p value, or None for not stored
    (necessarily None when state == 'no_item').
    """
    env.settings_table.delete_item(Key={"setting_key": "bedrock_configuration"})
    if state == "no_item":
        return
    value = {}
    if state == "explicit_null":
        value["temperature"] = None
    if top_p is not None:
        value["top_p"] = _to_ddb(top_p)
    env.settings_table.put_item(Item={
        "setting_key": "bedrock_configuration",
        "value": value,
    })


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


def generate_workflow(env):
    """POST /workflows/generate without a temperature override."""
    env.wf_client.converse.reset_mock(return_value=True, side_effect=True)
    env.wf_client.converse.return_value = tool_response(
        "create_workflow", VALID_DEFINITION)
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/generate",
        "path": "/workflows/generate",
        "body": json.dumps({"usecase_id": env.usecase_id,
                            "prompt": "Camera to capture"}),
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


def assert_no_temperature(inference_config, top_p):
    """Property 1 core assertion over a captured inferenceConfig."""
    assert "temperature" not in inference_config, (
        f"temperature must be omitted when unset, got "
        f"{inference_config!r}")
    if top_p is None:
        assert "topP" not in inference_config, (
            f"topP must be omitted unless a top_p was explicitly stored, "
            f"got {inference_config!r}")
    else:
        assert inference_config.get("topP") == pytest.approx(top_p)


# ---------------------------------------------------------------------------
# Generators: unset-temperature store states from isBugCondition1
# ---------------------------------------------------------------------------

@st.composite
def unset_temperature_stores(draw):
    """(state, top_p): a store state where no temperature was explicitly
    stored, optionally with an explicitly stored top_p (only possible when
    an item exists at all)."""
    state = draw(st.sampled_from(
        ["no_item", "no_temperature_key", "explicit_null"]))
    top_p = None
    if state != "no_item":
        # Rounded to 6 decimal places: DynamoDB numbers only support
        # exponents down to ~1e-130, so unconstrained floats (e.g.
        # 6.5e-177) are unrealizable store states - boto3's Decimal
        # serializer raises Underflow before the property is evaluated.
        top_p = draw(st.one_of(
            st.none(),
            st.floats(min_value=0, max_value=1,
                      allow_nan=False, allow_infinity=False)
            .map(lambda x: round(x, 6)),
        ))
    return state, top_p


# ---------------------------------------------------------------------------
# Property 1a: workflow generation omits temperature when unset
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(store=unset_temperature_stores())
def test_workflow_generation_omits_temperature_when_unset(sampling_env, store):
    """For any unset-temperature store state and a generation request with
    no temperature override, the workflow-generation inferenceConfig
    contains no temperature key, and no topP key unless a top_p was
    explicitly stored (Requirements 1.1, 2.1)."""
    state, top_p = store
    apply_store_state(sampling_env, state, top_p)

    status, _ = generate_workflow(sampling_env)
    assert status == 200

    assert sampling_env.wf_client.converse.call_count == 1
    inference_config = sampling_env.wf_client.converse.call_args.kwargs[
        "inferenceConfig"]
    assert_no_temperature(inference_config, top_p)


# ---------------------------------------------------------------------------
# Property 1b: node scaffold generation omits temperature when unset
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(store=unset_temperature_stores())
def test_node_generation_omits_temperature_when_unset(sampling_env, store):
    """For any unset-temperature store state and a node scaffold generation
    (node_generator reuses get_bedrock_configuration()), the inferenceConfig
    contains no temperature key, and no topP key unless a top_p was
    explicitly stored (Requirements 1.2, 2.2)."""
    state, top_p = store
    apply_store_state(sampling_env, state, top_p)

    status, _ = generate_node(sampling_env)
    assert status == 200

    assert sampling_env.node_client.converse.call_count == 1
    inference_config = sampling_env.node_client.converse.call_args.kwargs[
        "inferenceConfig"]
    assert_no_temperature(inference_config, top_p)


# ---------------------------------------------------------------------------
# Property 1c: settings save accepts null sampling parameters and
#              round-trips them as unset
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(
    nulled=st.lists(st.sampled_from(["temperature", "top_p"]),
                    min_size=1, max_size=2, unique=True),
    prior_numeric_config=st.booleans(),
)
def test_settings_save_accepts_null_and_round_trips_unset(
        sampling_env, nulled, prior_numeric_config):
    """PUT bedrock-configuration with temperature (and/or top_p) explicitly
    null is accepted with 200 and the value round-trips as unset through GET
    / read_stored_bedrock_configuration (Requirements 1.3, 2.3)."""
    sampling_env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})

    if prior_numeric_config:
        # An admin previously configured numeric sampling values and now
        # clears the field(s).
        status, _ = invoke_settings(sampling_env, "PUT", {
            "temperature": 0.5, "top_p": 0.8,
        })
        assert status == 200

    body = {key: None for key in nulled}
    status, payload = invoke_settings(sampling_env, "PUT", body)
    assert status == 200, (
        f"save with {body!r} must be accepted, got {status}: {payload!r}")

    for key in nulled:
        assert payload["bedrock_configuration"][key] is None

    # Round-trip: GET reports the parameter as unset, not the default.
    status, payload = invoke_settings(sampling_env, "GET")
    assert status == 200
    for key in nulled:
        assert payload["bedrock_configuration"][key] is None, (
            f"{key} must round-trip as unset, got "
            f"{payload['bedrock_configuration'][key]!r}")

    config = sampling_env.data_accounts.read_stored_bedrock_configuration()
    for key in nulled:
        assert config[key] is None
