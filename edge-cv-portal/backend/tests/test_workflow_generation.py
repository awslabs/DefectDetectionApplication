"""
Workflow generation integration tests (workflow-manager Requirements
10.2, 10.5, 10.7).

Task 10.4 (spec: workflow-manager).

POST /workflows/generate (functions/workflow_generator.py) is exercised
against the shared moto stack from conftest.py with the Bedrock Converse
API mocked (design.md "Integration tests": Bedrock invocation with mocked
Converse API).

Generation is now asynchronous (workflow-manager-gaps Requirement 1):
POST returns 202 {job_id, session_id} and the generation itself runs in
a worker self-invocation. The `generate` helper drives that transport
end to end - submit, in-process worker execution, terminal
Generation_Job outcome - so every semantic assertion below exercises
the exact production code path while keeping the old (status, payload)
call shape. TestGenerationWorkerLifecycle covers the worker's job state
transitions (task 6.2). Covered:

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
   timeout is clamped to at most 240 seconds when building the client;
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

# Minimal catalog-conformant Workflow_Definition: csi_camera_source -> capture,
# every required parameter set, acyclic, all nodes reachable - so the
# Workflow_Validator reports zero errors.
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


def generation_event(ctx, prompt, session_id=None, current_definition=None,
                     temperature=None):
    """An API Gateway POST /workflows/generate event."""
    body = {"usecase_id": ctx.usecase_id, "prompt": prompt}
    if session_id is not None:
        body["session_id"] = session_id
    if current_definition is not None:
        body["current_definition"] = current_definition
    if temperature is not None:
        body["temperature"] = temperature
    return {
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


def submit(gen_env, ctx, prompt, session_id=None, current_definition=None,
           temperature=None):
    """POST /workflows/generate (asynchronous submit) with the worker
    dispatch captured instead of Lambda-invoked; returns
    (status_code, parsed_body, worker_events)."""
    event = generation_event(ctx, prompt, session_id=session_id,
                             current_definition=current_definition,
                             temperature=temperature)
    dispatched = []

    def capture_dispatch(job_id, user):
        dispatched.append({"workflow_gen_worker": True, "job_id": job_id,
                           "user": user})

    original = gen_env.module.dispatch_generation_worker
    gen_env.module.dispatch_generation_worker = capture_dispatch
    try:
        response = gen_env.module.handler(event, None)
    finally:
        gen_env.module.dispatch_generation_worker = original
    return response["statusCode"], json.loads(response["body"]), dispatched


def job_record(gen_env, job_id):
    """The stored Generation_Job item (Decimal-free), or None."""
    item = gen_env.sessions_table.get_item(
        Key={"session_id": f"genjob#{job_id}"}).get("Item")
    return gen_env.module.decimal_to_native(item) if item else None


def job_outcome(gen_env, job_id):
    """A terminal Generation_Job's outcome as (status_code, payload):
    succeeded -> (200, stored result.json), failed -> the stored
    http_status and error envelope."""
    item = job_record(gen_env, job_id)
    assert item is not None, f"job {job_id} record missing"
    if item["status"] == "succeeded":
        obj = gen_env.s3.get_object(Bucket=gen_env.bucket,
                                    Key=item["result_s3_key"])
        return 200, json.loads(obj["Body"].read().decode("utf-8"))
    assert item["status"] == "failed", \
        f"job {job_id} is not terminal: {item['status']}"
    failure = item["failure"]
    return int(failure["http_status"]), {"error": failure["error"]}


def generate(gen_env, ctx, prompt, session_id=None, current_definition=None,
             temperature=None):
    """One full generation through the asynchronous submit/worker
    transport; returns (status_code, parsed_body) with the same semantics
    the old synchronous endpoint had.

    Synchronous submit rejections return directly. On 202 the captured
    worker payload is executed in-process (the async self-invocation runs
    this same handler) and the terminal Generation_Job outcome is read
    back from the job record / stored result.
    """
    status, payload, dispatched = submit(
        gen_env, ctx, prompt, session_id=session_id,
        current_definition=current_definition, temperature=temperature)
    if status != 202:
        return status, payload
    (worker_event,) = dispatched
    gen_env.module.handler(worker_event, None)
    return job_outcome(gen_env, payload["job_id"])


def put_bedrock_config(gen_env, **values):
    """Store a Bedrock_Configuration item the way the settings API does."""
    def to_ddb(value):
        return Decimal(str(value)) if isinstance(value, float) else value
    gen_env.settings_table.put_item(Item={
        "setting_key": "bedrock_configuration",
        "value": {k: to_ddb(v) for k, v in values.items()},
    })


def sessions_for(gen_env, usecase_id):
    """Chat session items belonging to a Use_Case (fresh per test).

    Generation_Job records (record_type=generation_job, key-prefixed
    'genjob#') live in the same table and are excluded: these helpers
    assert Chat_Session persistence semantics only.
    """
    items = gen_env.sessions_table.scan()["Items"]
    return [i for i in items
            if i.get("usecase_id") == usecase_id
            and i.get("record_type") != "generation_job"]


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

    def test_configured_timeout_is_clamped_to_240_seconds(self, gen_env,
                                                          bedrock, ctx):
        """A stored timeout above the 240-second maximum is clamped before
        the client is built (Requirement 10.7: at most 240 seconds)."""
        put_bedrock_config(gen_env, timeout_seconds=300)

        status, _ = generate(gen_env, ctx, "Camera to capture")
        assert status == 200
        assert bedrock.client_requests[-1][1] == 240

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


# ===========================================================================
# 5. Generation worker job lifecycle (workflow-manager-gaps task 6.2;
#    Requirements 1.2, 2.6, 2.7, 2.9, 3.5)
# ===========================================================================

# VALID_DEFINITION plus one unconnected capture node: exactly one
# repairable Structural_Error (V5 unreachable node), so the gate decides
# repair and a second Converse invocation (the Repair_Pass) runs.
REPAIRABLE_DEFINITION = {
    "schemaVersion": 1,
    "nodes": VALID_DEFINITION["nodes"] + [
        {"id": "n3", "type": "capture",
         "position": {"x": 600, "y": 100},
         "parameters": {"output_path": "/data/other"}},
    ],
    "connections": VALID_DEFINITION["connections"],
}


class TestGenerationWorkerLifecycle:
    """run_generation_worker: pending -> running -> terminal transitions,
    first-terminal-write-wins immutability, combined rejection+timeout
    failure recording, and the internal-error backstop."""

    def test_success_records_succeeded_job_with_stored_result(self, gen_env,
                                                              bedrock, ctx):
        """A 200 generation outcome stores the full sync-endpoint payload
        in S3 under jobs/{job_id}/result.json and conditionally
        transitions the job to succeeded with the result reference,
        terminal_at, and a refreshed TTL (Req 2.6, 2.7)."""
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "pending"

        (worker_event,) = dispatched
        result = gen_env.module.handler(worker_event, None)
        assert result == {"ok": True}

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "succeeded"
        assert job["result_s3_key"] == (
            f"workflows/{ctx.usecase_id}/chat-sessions/"
            f"{body['session_id']}/jobs/{body['job_id']}/result.json")
        assert job["terminal_at"] >= job["dispatched_at"]
        assert job["ttl"] == int(job["terminal_at"] / 1000) + \
            gen_env.module.SESSION_TTL_SECONDS

        # The stored result IS the sync endpoint's payload (Req 2.2).
        obj = gen_env.s3.get_object(Bucket=gen_env.bucket,
                                    Key=job["result_s3_key"])
        payload = json.loads(obj["Body"].read().decode("utf-8"))
        assert payload["session_id"] == body["session_id"]
        assert payload["usecase_id"] == ctx.usecase_id
        assert set(payload) >= {"definition", "findings", "error_count",
                                "warning_count", "validation_passed",
                                "assistant_text", "model_id", "gate"}

    def test_terminal_job_is_not_regenerated_and_stays_immutable(
            self, gen_env, bedrock, ctx):
        """A worker arriving after a terminal write (here: a
        dispatch-failure 502) loses the conditional pending -> running
        transition, runs no generation, and leaves the stored terminal
        state untouched (first terminal write wins, Req 2.6)."""
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        stored = {"code": "GENERATION_NOT_STARTED",
                  "message": "Workflow generation could not be started.",
                  "details": {"job_id": body["job_id"]}}
        assert gen_env.module.record_job_failure(body["job_id"], 502, stored)

        (worker_event,) = dispatched
        result = gen_env.module.handler(worker_event, None)
        assert result == {"ok": False}

        assert bedrock.client.converse.call_count == 0
        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "failed"
        assert job["failure"]["http_status"] == 502
        assert job["failure"]["error"] == stored
        assert "result_s3_key" not in job

    def test_repair_timeout_after_rejection_records_combined_failure(
            self, gen_env, bedrock, ctx):
        """A Repair_Pass GENERATION_TIMEOUT after a first-pass gate
        rejection records exactly one terminal failed state whose
        GENERATION_REJECTED envelope details carry both the structural
        errors and the timeout indication (Req 2.9, 3.5)."""
        put_bedrock_config(gen_env, timeout_seconds=45)
        bedrock.client.converse.side_effect = [
            converse_tool_response(REPAIRABLE_DEFINITION),
            ReadTimeoutError(
                endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
        ]

        status, payload = generate(gen_env, ctx, "Camera to capture")
        assert bedrock.client.converse.call_count == 2  # first pass + repair

        assert status == 422
        error = payload["error"]
        assert error["code"] == "GENERATION_REJECTED"
        assert error["details"]["repair_attempted"] is True
        codes = {e["code"] for e in error["details"]["structural_errors"]}
        assert codes == {"V5_UNREACHABLE_NODE"}
        assert error["details"]["timeout"]["timeout_seconds"] == 45

        # One terminal failed state; session and snapshot untouched.
        job_id = job_record_id_for(gen_env, ctx.usecase_id)
        job = job_record(gen_env, job_id)
        assert job["status"] == "failed"
        assert job["failure"]["http_status"] == 422
        assert sessions_for(gen_env, ctx.usecase_id) == []
        assert s3_keys_for(gen_env, ctx.usecase_id) == []

    def test_worker_exception_records_500_internal_error(self, gen_env,
                                                         bedrock, ctx,
                                                         monkeypatch):
        """An unexpected exception inside the worker still ends the job
        terminal: failed with a 500 INTERNAL_ERROR envelope, so no
        exception path leaves the job pending/running."""
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(gen_env.module, "run_generation_core", explode)

        (worker_event,) = dispatched
        result = gen_env.module.handler(worker_event, None)
        assert result == {"ok": False}

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "failed"
        assert job["failure"]["http_status"] == 500
        assert job["failure"]["error"]["code"] == "INTERNAL_ERROR"
        assert job["terminal_at"] >= job["dispatched_at"]


def job_record_id_for(gen_env, usecase_id):
    """The single Generation_Job id recorded for a Use_Case."""
    items = gen_env.sessions_table.scan()["Items"]
    jobs = [i for i in items
            if i.get("usecase_id") == usecase_id
            and i.get("record_type") == "generation_job"]
    (job,) = jobs
    return job["job_id"]


# ===========================================================================
# 6. Generation job status endpoint (workflow-manager-gaps task 7.1;
#    Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.8, 2.10, 3.6)
# ===========================================================================

def status_event(user, job_id):
    """An API Gateway GET /workflows/generate/{job_id} event."""
    return {
        "httpMethod": "GET",
        "resource": "/workflows/generate/{job_id}",
        "path": f"/workflows/generate/{job_id}",
        "pathParameters": {"job_id": job_id},
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


def poll(gen_env, user, job_id):
    """GET /workflows/generate/{job_id}; returns (status_code, body)."""
    response = gen_env.module.handler(status_event(user, job_id), None)
    return response["statusCode"], json.loads(response["body"])


class TestGenerationJobStatus:
    """GET /workflows/generate/{job_id}: state-appropriate payloads,
    uniform 404, RBAC 403, and lazy poll-time reaping."""

    def test_unknown_job_id_returns_fixed_404(self, gen_env, ctx):
        """A Job_ID that never existed (or was TTL-removed) returns the
        fixed 404 JOB_NOT_FOUND envelope (Req 2.4, 2.10)."""
        status, payload = poll(gen_env, ctx.user, "no-such-job")
        assert status == 404
        assert payload["error"]["code"] == "JOB_NOT_FOUND"

    def test_pending_job_returns_status_only(self, gen_env, bedrock, ctx):
        """A pending job polls 200 {job_id, status} with neither a result
        nor a failure envelope (Req 2.1, 2.8), and an immediate poll after
        the 202 never 404s (Req 1.8)."""
        status, body, _ = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert status == 200
        assert payload == {"job_id": body["job_id"], "status": "pending"}

    def test_succeeded_job_embeds_result_field_for_field(self, gen_env,
                                                         bedrock, ctx):
        """A succeeded job returns 200 embedding the stored result.json
        field-for-field - the sync endpoint's exact payload (Req 2.2) -
        and repeated polls are identical (Req 2.6)."""
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202
        (worker_event,) = dispatched
        gen_env.module.handler(worker_event, None)

        job = job_record(gen_env, body["job_id"])
        obj = gen_env.s3.get_object(Bucket=gen_env.bucket,
                                    Key=job["result_s3_key"])
        stored_result = json.loads(obj["Body"].read().decode("utf-8"))

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert status == 200
        assert payload["job_id"] == body["job_id"]
        assert payload["status"] == "succeeded"
        for field in ("session_id", "usecase_id", "definition", "findings",
                      "error_count", "warning_count", "validation_passed",
                      "assistant_text", "model_id", "gate"):
            assert payload[field] == stored_result[field]

        # Reads never mutate terminal jobs: a repeated poll is identical.
        assert poll(gen_env, ctx.user, body["job_id"]) == (status, payload)
        assert job_record(gen_env, body["job_id"]) == job

    def test_failed_job_replays_stored_envelope_verbatim(self, gen_env,
                                                         bedrock, ctx):
        """A failed job returns the stored http_status and the stored
        envelope verbatim (Req 2.3)."""
        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202
        (worker_event,) = dispatched
        gen_env.module.handler(worker_event, None)

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "failed"

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert status == job["failure"]["http_status"] == 504
        assert payload["error"] == job["failure"]["error"]
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"

    def test_no_usecase_access_returns_the_same_404(self, gen_env, bedrock,
                                                    ctx, env):
        """A user with no access to the job's Use_Case gets a 404
        byte-identical to the unknown-job 404 - no cross-tenant existence
        leak (Req 2.4)."""
        status, body, _ = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        # DataLabeler has no workflow permissions at all (no Use_Case
        # access through the WORKFLOW_READ gate).
        outsider = env.make_user(role="DataLabeler")
        unknown = gen_env.module.handler(
            status_event(outsider, "no-such-job"), None)
        cross = gen_env.module.handler(
            status_event(outsider, body["job_id"]), None)
        assert cross["statusCode"] == unknown["statusCode"] == 404
        assert cross["body"] == unknown["body"]

    def test_access_without_generation_permission_returns_403(self, gen_env,
                                                              bedrock, ctx,
                                                              env):
        """Use_Case access (WORKFLOW_READ) without WORKFLOW_CREATE or
        WORKFLOW_EDIT gets the existing 403 RBAC envelope (Req 2.5)."""
        status, body, _ = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        viewer = env.make_user(role="Viewer")
        env.assign_role(viewer, ctx.usecase_id, "Viewer")
        status, payload = poll(gen_env, viewer, body["job_id"])
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"

    def test_overdue_nonterminal_job_is_reaped_to_504(self, gen_env, bedrock,
                                                      ctx):
        """A non-terminal job past its deadline_at is lazily transitioned
        to failed with 504 GENERATION_ABNORMAL_TERMINATION on poll
        (Req 3.6), and repeated polls serve the same terminal state."""
        status, body, _ = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        gen_env.sessions_table.update_item(
            Key={"session_id": f"genjob#{body['job_id']}"},
            UpdateExpression="SET deadline_at = :past",
            ExpressionAttributeValues={":past": 1},
        )

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_ABNORMAL_TERMINATION"

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "failed"
        assert job["failure"]["http_status"] == 504
        assert job["terminal_at"] is not None
        assert job["ttl"] == int(job["terminal_at"] / 1000) + \
            gen_env.module.SESSION_TTL_SECONDS

        assert poll(gen_env, ctx.user, body["job_id"]) == (status, payload)

    def test_in_deadline_job_is_not_reaped(self, gen_env, bedrock, ctx):
        """A non-terminal job still inside its deadline polls its stored
        state unchanged - the reaper fires only past deadline_at."""
        status, body, _ = submit(gen_env, ctx, "Camera to capture")
        assert status == 202

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert (status, payload["status"]) == (200, "pending")
        assert job_record(gen_env, body["job_id"])["status"] == "pending"

    def test_s3_read_failure_returns_500_leaving_record_untouched(
            self, gen_env, bedrock, ctx):
        """An unreadable stored result for a succeeded job returns 500
        INTERNAL_ERROR and leaves the job record untouched so a later
        poll can succeed (design: error handling table)."""
        status, body, dispatched = submit(gen_env, ctx, "Camera to capture")
        assert status == 202
        (worker_event,) = dispatched
        gen_env.module.handler(worker_event, None)

        job = job_record(gen_env, body["job_id"])
        assert job["status"] == "succeeded"
        gen_env.s3.delete_object(Bucket=gen_env.bucket,
                                 Key=job["result_s3_key"])

        status, payload = poll(gen_env, ctx.user, body["job_id"])
        assert status == 500
        assert payload["error"]["code"] == "INTERNAL_ERROR"
        assert job_record(gen_env, body["job_id"]) == job
