"""
Workflow generation integration tests (workflow-manager Requirements
10.2, 10.5, 10.7).

Task 10.4 (spec: workflow-manager).

POST /workflows/generate (functions/workflow_generator.py) is exercised
against the shared moto stack from conftest.py with the Bedrock Converse
API mocked (design.md "Integration tests": Bedrock invocation with mocked
Converse API). Covered:

1. Prompt/catalog assembly (10.2): the Converse call carries a system
   prompt embedding the serialized node catalog, a toolConfig whose
   create_workflow inputSchema IS the Workflow_Definition JSON Schema with
   toolChoice forced, and the configured model id / inference parameters.
2. Tool-use output handling: a valid create_workflow tool input returns
   200 with the canonical definition plus the complete findings list, and
   the chat session (DynamoDB) and canvas snapshot (S3) are persisted;
   unparseable tool output returns 422 with no session mutation; a
   response without the tool call returns 502 NO_WORKFLOW_RETURNED.
3. Follow-up modification flow (10.5): a second prompt in the same
   session replays the history and sends a user turn embedding the
   current canvas definition with an instruction to modify rather than
   regenerate; a client-provided current_definition is authoritative.
4. Timeout and invocation failure (10.7): a read timeout returns 504
   GENERATION_TIMEOUT carrying the configured timeout; the configured
   timeout is clamped to at most 60 seconds when building the client;
   ClientError returns 502 - all without touching the session.

_Requirements: 10.2, 10.5, 10.7_
"""
import json
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from conftest import REGION, TEST_ENV

CHAT_SESSIONS_TABLE_NAME = "test-workflow-chat-sessions"
SETTINGS_TABLE_NAME = "test-settings-generation"

TOOL_NAME = "create_workflow"

# Minimal catalog-conformant Workflow_Definition: camera_source -> capture,
# every required parameter set, acyclic, all nodes reachable - so the
# Workflow_Validator reports zero errors.
VALID_DEFINITION = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "camera_source",
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


def converse_tool_response(tool_input, text=None):
    """A Converse API response whose assistant message calls create_workflow."""
    content = []
    if text is not None:
        content.append({"text": text})
    content.append({"toolUse": {"toolUseId": "tool-1", "name": TOOL_NAME,
                                "input": tool_input}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
    }


@pytest.fixture(scope="module")
def gen_env(aws_stack):
    """Chat-sessions + settings tables and a freshly imported
    workflow_generator module bound to them inside moto."""
    import boto3

    os.environ["WORKFLOW_CHAT_SESSIONS_TABLE"] = CHAT_SESSIONS_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CHAT_SESSIONS_TABLE_NAME,
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    for module_name in ("workflow_generator", "workflow_validation"):
        sys.modules.pop(module_name, None)
    import workflow_generator

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=workflow_generator,
        sessions_table=resource.Table(CHAT_SESSIONS_TABLE_NAME),
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
        s3=boto3.client("s3", region_name=REGION),
        bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
    )


@pytest.fixture(autouse=True)
def clean_bedrock_config(gen_env):
    """Each test starts from the default Bedrock_Configuration."""
    gen_env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})
    yield


@pytest.fixture
def bedrock(gen_env, monkeypatch):
    """Mocked Converse API client injected through get_bedrock_client.

    ``client_requests`` records every (region, timeout_seconds) the
    handler asked a client for, so tests can assert the clamped timeout.
    """
    client = MagicMock(name="bedrock-runtime")
    client.converse.return_value = converse_tool_response(
        VALID_DEFINITION, text="Here is the workflow.")
    client_requests = []

    def fake_get_bedrock_client(region, timeout_seconds):
        client_requests.append((region, timeout_seconds))
        return client

    monkeypatch.setattr(gen_env.module, "get_bedrock_client",
                        fake_get_bedrock_client)
    return SimpleNamespace(client=client, client_requests=client_requests)


@pytest.fixture
def ctx(env):
    """A fresh Use_Case and a DataScientist authorized on it."""
    usecase_id = env.create_usecase()
    user = env.make_user()
    env.assign_role(user, usecase_id, "DataScientist")
    return SimpleNamespace(usecase_id=usecase_id, user=user)


def generate(gen_env, ctx, prompt, session_id=None, current_definition=None,
             temperature=None):
    """POST /workflows/generate; returns (status_code, parsed_body)."""
    body = {"usecase_id": ctx.usecase_id, "prompt": prompt}
    if session_id is not None:
        body["session_id"] = session_id
    if current_definition is not None:
        body["current_definition"] = current_definition
    if temperature is not None:
        body["temperature"] = temperature
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/generate",
        "path": "/workflows/generate",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": ctx.user["user_id"],
                    "email": ctx.user["email"],
                    "cognito:username": ctx.user["username"],
                    "custom:role": ctx.user["role"],
                }
            }
        },
    }
    response = gen_env.module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def put_bedrock_config(gen_env, **values):
    """Store a Bedrock_Configuration item the way the settings API does."""
    def to_ddb(value):
        return Decimal(str(value)) if isinstance(value, float) else value
    gen_env.settings_table.put_item(Item={
        "setting_key": "bedrock_configuration",
        "value": {k: to_ddb(v) for k, v in values.items()},
    })


def sessions_for(gen_env, usecase_id):
    """Chat session items belonging to a Use_Case (fresh per test)."""
    items = gen_env.sessions_table.scan()["Items"]
    return [i for i in items if i.get("usecase_id") == usecase_id]


def s3_keys_for(gen_env, usecase_id):
    """All portal-S3 keys under the Use_Case's workflows prefix."""
    response = gen_env.s3.list_objects_v2(
        Bucket=gen_env.bucket, Prefix=f"workflows/{usecase_id}/")
    return [obj["Key"] for obj in response.get("Contents", [])]


def read_snapshot(gen_env, usecase_id, session_id):
    obj = gen_env.s3.get_object(
        Bucket=gen_env.bucket,
        Key=f"workflows/{usecase_id}/chat-sessions/{session_id}/current_definition.json",
    )
    return json.loads(obj["Body"].read().decode("utf-8"))


# ===========================================================================
# 1. Prompt and catalog assembly (Requirement 10.2)
# ===========================================================================

class TestPromptAndCatalogAssembly:

    def test_system_prompt_embeds_serialized_node_catalog(self, gen_env,
                                                          bedrock, ctx):
        """The Converse call carries the prompt and the full serialized
        node type catalog in the system prompt (Requirement 10.2)."""
        prompt = "Capture frames from the camera to disk"
        status, _ = generate(gen_env, ctx, prompt)
        assert status == 200

        assert bedrock.client.converse.call_count == 1
        kwargs = bedrock.client.converse.call_args.kwargs
        system_text = kwargs["system"][0]["text"]
        assert gen_env.module.serialized_catalog_json() in system_text

        messages = kwargs["messages"]
        assert messages[-1]["role"] == "user"
        assert prompt in messages[-1]["content"][0]["text"]

    def test_tool_config_is_definition_schema_with_forced_choice(self, gen_env,
                                                                 bedrock, ctx):
        """The create_workflow tool inputSchema IS the Workflow_Definition
        JSON Schema and toolChoice forces the tool (Requirement 10.2)."""
        from workflow_core.serializer import WORKFLOW_DEFINITION_SCHEMA

        status, _ = generate(gen_env, ctx, "Any pipeline")
        assert status == 200

        tool_config = bedrock.client.converse.call_args.kwargs["toolConfig"]
        assert tool_config["toolChoice"] == {"tool": {"name": TOOL_NAME}}

        (tool,) = tool_config["tools"]
        assert tool["toolSpec"]["name"] == TOOL_NAME
        sent_schema = tool["toolSpec"]["inputSchema"]["json"]
        expected = {k: v for k, v in WORKFLOW_DEFINITION_SCHEMA.items()
                    if k not in ("$schema", "$id")}
        assert sent_schema == expected

    def test_configured_model_and_inference_parameters_are_used(self, gen_env,
                                                                bedrock, ctx):
        """The Bedrock_Configuration from the settings table drives the
        model id, inference parameters, region, and timeout (10.2, 10.7).
        When both sampling parameters are configured, only temperature is
        sent - recent Anthropic models reject temperature + top_p
        together."""
        put_bedrock_config(gen_env, model_id="amazon.nova-pro-v1:0",
                           region="eu-west-1", max_tokens=1024,
                           temperature=0.7, top_p=0.5, timeout_seconds=30)

        status, payload = generate(gen_env, ctx, "Any pipeline")
        assert status == 200
        assert payload["model_id"] == "amazon.nova-pro-v1:0"

        kwargs = bedrock.client.converse.call_args.kwargs
        assert kwargs["modelId"] == "amazon.nova-pro-v1:0"
        assert kwargs["inferenceConfig"] == {"maxTokens": 1024,
                                             "temperature": 0.7}
        assert "topP" not in kwargs["inferenceConfig"]
        assert bedrock.client_requests[-1] == ("eu-west-1", 30)

    def test_default_configuration_sends_no_sampling_parameters(
            self, gen_env, bedrock, ctx):
        """With nothing stored, the default configuration is unset for
        both sampling parameters, so the invocation carries neither
        temperature nor topP - they are sent only when explicitly
        configured or overridden."""
        status, _ = generate(gen_env, ctx, "Any pipeline")
        assert status == 200

        inference_config = bedrock.client.converse.call_args.kwargs[
            "inferenceConfig"]
        assert "temperature" not in inference_config
        assert "topP" not in inference_config

    def test_top_p_is_sent_when_temperature_is_explicitly_null(
            self, gen_env, bedrock, ctx):
        """A stored configuration with temperature explicitly null sends
        topP (and no temperature) so top_p-only sampling is possible."""
        put_bedrock_config(gen_env, temperature=None, top_p=0.5)

        status, _ = generate(gen_env, ctx, "Any pipeline")
        assert status == 200

        inference_config = bedrock.client.converse.call_args.kwargs[
            "inferenceConfig"]
        assert inference_config["topP"] == 0.5
        assert "temperature" not in inference_config


# ===========================================================================
# 1b. Per-request temperature override
# ===========================================================================

class TestTemperatureOverride:
    """POST /workflows/generate accepts an optional `temperature` (0..1)
    that overrides the configured value for that invocation only and, per
    the existing rule, suppresses top_p."""

    def test_body_temperature_overrides_configured_value(self, gen_env,
                                                         bedrock, ctx):
        """A body temperature replaces the settings-table temperature for
        this invocation; top_p stays suppressed."""
        put_bedrock_config(gen_env, temperature=0.7, top_p=0.5)

        status, _ = generate(gen_env, ctx, "Any pipeline", temperature=0.05)
        assert status == 200

        inference_config = bedrock.client.converse.call_args.kwargs[
            "inferenceConfig"]
        assert inference_config["temperature"] == 0.05
        assert "topP" not in inference_config

    def test_body_temperature_overrides_null_configured_temperature(
            self, gen_env, bedrock, ctx):
        """Even a configuration with temperature explicitly null (top_p
        sampling) is overridden: the request temperature is sent and
        top_p is suppressed."""
        put_bedrock_config(gen_env, temperature=None, top_p=0.5)

        status, _ = generate(gen_env, ctx, "Any pipeline", temperature=0.0)
        assert status == 200

        inference_config = bedrock.client.converse.call_args.kwargs[
            "inferenceConfig"]
        assert inference_config["temperature"] == 0.0
        assert "topP" not in inference_config

    def test_boundary_values_zero_and_one_are_accepted(self, gen_env,
                                                       bedrock, ctx):
        for value in (0, 1, 0.5):
            status, _ = generate(gen_env, ctx, "Any pipeline",
                                 temperature=value)
            assert status == 200
            assert bedrock.client.converse.call_args.kwargs[
                "inferenceConfig"]["temperature"] == float(value)

    @pytest.mark.parametrize("value", [-0.1, 1.5, 2, "0.5", True, [0.2]])
    def test_invalid_temperature_is_rejected_with_400(self, gen_env, bedrock,
                                                      ctx, value):
        """Values outside [0, 1] or non-numbers reject with 400
        INVALID_TEMPERATURE before any Bedrock invocation."""
        status, payload = generate(gen_env, ctx, "Any pipeline",
                                   temperature=value)
        assert status == 400
        assert payload["error"]["code"] == "INVALID_TEMPERATURE"
        assert bedrock.client.converse.call_count == 0

    def test_absent_temperature_uses_configured_value(self, gen_env, bedrock,
                                                      ctx):
        """Without a body temperature the configured value is used
        unchanged (the override is per-invocation only)."""
        put_bedrock_config(gen_env, temperature=0.7, top_p=0.5)

        status, _ = generate(gen_env, ctx, "Any pipeline")
        assert status == 200
        assert bedrock.client.converse.call_args.kwargs[
            "inferenceConfig"]["temperature"] == 0.7


# ===========================================================================
# 2. Tool-use output handling
# ===========================================================================

class TestToolUseOutputHandling:

    def test_valid_tool_output_returns_definition_findings_and_session(
            self, gen_env, bedrock, ctx):
        """A valid create_workflow tool input yields 200 with the canonical
        definition plus complete findings, a persisted chat session, and
        the canvas snapshot in S3 (Requirements 10.2, 10.5)."""
        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert status == 200

        definition = payload["definition"]
        assert definition["schemaVersion"] == 1
        assert {n["id"] for n in definition["nodes"]} == {"n1", "n2"}
        assert [c["id"] for c in definition["connections"]] == ["c1"]

        assert isinstance(payload["findings"], list)
        assert payload["error_count"] == 0
        assert payload["validation_passed"] is True
        assert payload["assistant_text"] == "Here is the workflow."

        # Session persisted with both turns and the snapshot key.
        (session,) = sessions_for(gen_env, ctx.usecase_id)
        assert session["session_id"] == payload["session_id"]
        assert session["user_id"] == ctx.user["user_id"]
        assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
        assert session["current_definition_key"].endswith(
            f"chat-sessions/{payload['session_id']}/current_definition.json")

        # Snapshot in S3 equals the returned canonical definition.
        snapshot = read_snapshot(gen_env, ctx.usecase_id, payload["session_id"])
        assert snapshot == definition

    def test_unparseable_tool_output_returns_422_without_session_mutation(
            self, gen_env, bedrock, ctx):
        """Tool output that is not a valid Workflow_Definition yields 422
        and persists nothing, leaving the canvas untouched."""
        bedrock.client.converse.return_value = converse_tool_response(
            {"schemaVersion": 1, "nodes": "not-a-list"})

        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert status == 422
        assert payload["error"]["code"] == "GENERATED_DEFINITION_INVALID"
        assert payload["error"]["details"]["path"]

        assert sessions_for(gen_env, ctx.usecase_id) == []
        assert s3_keys_for(gen_env, ctx.usecase_id) == []

    def test_response_without_tool_call_returns_502(self, gen_env, bedrock,
                                                    ctx):
        """A model response with no create_workflow tool call yields 502
        NO_WORKFLOW_RETURNED and persists nothing."""
        bedrock.client.converse.return_value = {
            "output": {"message": {"role": "assistant",
                                   "content": [{"text": "I cannot help."}]}},
            "stopReason": "end_turn",
        }

        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert status == 502
        assert payload["error"]["code"] == "NO_WORKFLOW_RETURNED"
        assert payload["error"]["details"]["stop_reason"] == "end_turn"

        assert sessions_for(gen_env, ctx.usecase_id) == []
        assert s3_keys_for(gen_env, ctx.usecase_id) == []


# ===========================================================================
# 3. Follow-up modification flow (Requirement 10.5)
# ===========================================================================

class TestFollowUpModification:

    def test_followup_embeds_current_definition_and_replays_history(
            self, gen_env, bedrock, ctx):
        """A follow-up prompt in the same session replays the history and
        sends the current canvas definition with a modification
        instruction, not a from-scratch request (Requirement 10.5)."""
        status, first = generate(gen_env, ctx, "Camera to capture")
        assert status == 200
        session_id = first["session_id"]
        stored_snapshot = read_snapshot(gen_env, ctx.usecase_id, session_id)

        status, second = generate(gen_env, ctx, "Add a rotate step",
                                  session_id=session_id)
        assert status == 200
        assert second["session_id"] == session_id

        assert bedrock.client.converse.call_count == 2
        messages = bedrock.client.converse.call_args.kwargs["messages"]

        # History replayed: first user turn, assistant turn, new user turn.
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert "Camera to capture" in messages[0]["content"][0]["text"]

        followup_text = messages[2]["content"][0]["text"]
        assert followup_text.startswith("Add a rotate step")
        assert "CURRENT CANVAS WORKFLOW DEFINITION (JSON):" in followup_text
        # The session's stored canvas snapshot is embedded verbatim.
        assert json.dumps(stored_snapshot, sort_keys=True) in followup_text
        assert ("Apply the requested change to this current definition "
                "rather than generating a new workflow from scratch"
                ) in followup_text

        # Session history now holds both exchanges.
        (session,) = sessions_for(gen_env, ctx.usecase_id)
        assert [m["role"] for m in session["messages"]] == \
            ["user", "assistant", "user", "assistant"]

    def test_client_provided_current_definition_is_authoritative(
            self, gen_env, bedrock, ctx):
        """A current_definition sent by the client (the live canvas) is
        embedded in the user turn in canonical form (Requirement 10.5)."""
        from workflow_core.serializer import parse, serialize

        status, _ = generate(gen_env, ctx, "Rename the capture path",
                             current_definition=VALID_DEFINITION)
        assert status == 200

        expected_canonical = serialize(
            parse(json.dumps(VALID_DEFINITION)).graph)
        user_text = bedrock.client.converse.call_args.kwargs[
            "messages"][-1]["content"][0]["text"]
        assert "CURRENT CANVAS WORKFLOW DEFINITION (JSON):" in user_text
        assert expected_canonical in user_text


# ===========================================================================
# 4. Timeout and invocation failure behavior (Requirement 10.7)
# ===========================================================================

class TestTimeoutAndFailure:

    def test_read_timeout_returns_504_with_configured_timeout(self, gen_env,
                                                              bedrock, ctx):
        """A Bedrock read timeout yields 504 GENERATION_TIMEOUT carrying
        the configured timeout, the client was built with that timeout,
        and no session is persisted so the prompt can be retried
        (Requirement 10.7)."""
        put_bedrock_config(gen_env, timeout_seconds=45)
        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"
        assert payload["error"]["details"]["timeout_seconds"] == 45
        assert bedrock.client_requests[-1][1] == 45

        assert sessions_for(gen_env, ctx.usecase_id) == []
        assert s3_keys_for(gen_env, ctx.usecase_id) == []

    def test_configured_timeout_is_clamped_to_60_seconds(self, gen_env,
                                                         bedrock, ctx):
        """A stored timeout above the 60-second maximum is clamped before
        the client is built (Requirement 10.7: at most 60 seconds)."""
        put_bedrock_config(gen_env, timeout_seconds=300)

        status, _ = generate(gen_env, ctx, "Camera to capture")
        assert status == 200
        assert bedrock.client_requests[-1][1] == 60

    def test_invocation_client_error_returns_502(self, gen_env, bedrock, ctx):
        """A Bedrock invocation failure yields 502 with the Bedrock error
        code and persists no session (Requirement 10.7)."""
        bedrock.client.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException",
                       "Message": "Rate exceeded"}},
            "Converse",
        )

        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert status == 502
        assert payload["error"]["code"] == "BEDROCK_INVOCATION_FAILED"
        assert payload["error"]["details"]["bedrock_error_code"] == \
            "ThrottlingException"

        assert sessions_for(gen_env, ctx.usecase_id) == []
        assert s3_keys_for(gen_env, ctx.usecase_id) == []
