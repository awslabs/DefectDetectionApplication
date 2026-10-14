"""
Node_Generator integration tests with the Bedrock Converse API mocked
(custom-node-designer, task 5.2).

POST /plugins/generate, GET /plugins/generate/{session}, and
POST /plugins/generate/{session}/message (functions/node_generator.py)
are exercised against the shared moto stack from conftest.py with
``get_bedrock_client`` replaced by a mocked Converse client, so
``invoke_generation`` itself runs. The asynchronous self-invocation of
the start/poll flow is wired to run the worker synchronously in-process,
and the ``generate``/``followup`` helpers below start a turn (202) and
poll it to a settled state, returning the effective outcome. Covered:

1. Prompt/template-convention assembly (2.2): the converse() call
   carries a system prompt embedding the Custom_Node_Type declaration,
   the rendered reference scaffold (the template conventions), and the
   Frame_Processing_Hook contract; a toolConfig forcing the
   create_plugin_scaffold tool whose inputSchema requires every scaffold
   file; and the configured model id / inference parameters / region.
2. Tool-use output handling: a valid create_plugin_scaffold tool input
   completes the turn with the file map in the poll response, persisting
   the chat session (DynamoDB) and the source snapshot (S3); a response
   without the tool call fails the turn with NO_SCAFFOLD_RETURNED (502)
   with no history or snapshot persisted.
3. Follow-up source inclusion (2.4): a follow-up prompt in the same
   session replays the history and embeds the current generated source
   with an instruction to modify rather than regenerate; the snapshot is
   updated to the modified file map.
4. Scaffold-validation rejection (2.6): tool output that does not form a
   buildable Plugin_Scaffold fails the turn with
   GENERATED_SCAFFOLD_INVALID (422) naming the defects, with the session
   history and snapshot untouched so the prompt is preserved for retry.
5. Timeout and invocation failure (2.7): a read timeout fails the turn
   with GENERATION_TIMEOUT (504) carrying the configured timeout
   (clamped to at most 240 seconds when building the client); ClientError
   fails with BEDROCK_INVOCATION_FAILED (502); an unreachable endpoint
   fails with BEDROCK_UNREACHABLE (502) - all without mutating the
   session history or snapshot, so the prompt is preserved for retry.

_Requirements: 2.2, 2.4, 2.6, 2.7_
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from conftest import REGION, TEST_ENV

NODE_GEN_SESSIONS_TABLE_NAME = "test-node-gen-sessions-integration"
SETTINGS_TABLE_NAME = "test-settings-node-gen-integration"

TOOL_NAME = "create_plugin_scaffold"

ARCHITECTURES = ["x86_64", "arm64_jp5"]


def make_declaration(**overrides):
    """A valid Custom_Node_Type declaration (wire shape + architectures)."""
    declaration = {
        "typeId": "custom_blur",
        "displayName": "Custom Blur",
        "description": "Blurs each frame.",
        "category": "preprocessing",
        "inputs": [{"name": "in", "portType": "VideoFrames"}],
        "outputs": [{"name": "out", "portType": "VideoFrames"}],
        "parameters": [
            {
                "name": "radius",
                "paramType": "int",
                "required": True,
                "default": 3,
                "constraints": {"min": 1, "max": 99},
                "description": "Blur radius in pixels.",
                "examples": [3],
            },
        ],
        "architectures": list(ARCHITECTURES),
    }
    declaration.update(overrides)
    return declaration


def converse_tool_response(tool_input, text=None):
    """A Converse API response whose assistant message calls the tool."""
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
    """NodeGenSessions + settings tables and a freshly imported
    node_generator module bound to them inside moto."""
    import boto3

    os.environ["NODE_GEN_SESSIONS_TABLE"] = NODE_GEN_SESSIONS_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    os.environ["PLUGIN_SOURCES_PREFIX"] = "plugin-sources"

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=NODE_GEN_SESSIONS_TABLE_NAME,
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

    # Re-import so the modules bind the table names above and
    # moto-intercepted boto3 clients (conftest pattern). workflow_generator
    # must be re-imported too: it reads SETTINGS_TABLE at import time and
    # node_generator takes get_bedrock_configuration from it.
    for module_name in ("node_generator", "workflow_generator",
                        "workflow_validation"):
        sys.modules.pop(module_name, None)
    import node_generator

    from workflow_core.scaffold import render_scaffold
    declaration = make_declaration()

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=node_generator,
        stack=aws_stack,
        sessions_table=resource.Table(NODE_GEN_SESSIONS_TABLE_NAME),
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
        s3=boto3.client("s3", region_name=REGION),
        bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
        declaration=declaration,
        reference_files=render_scaffold(declaration),
    )


@pytest.fixture(autouse=True)
def clean_bedrock_config(gen_env):
    """Each test starts from the default Bedrock_Configuration."""
    gen_env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})
    yield


@pytest.fixture(autouse=True)
def synchronous_worker(gen_env, monkeypatch):
    """Wire the asynchronous self-invocation to run the generation worker
    synchronously in-process, so a dispatched turn is settled by the time
    the start route's 202 returns and the first poll sees the outcome."""
    def dispatch(session_id, prompt, user):
        gen_env.module.handler({
            "node_gen_worker": True,
            "session_id": session_id,
            "prompt": prompt,
            "user": user,
        }, None)
    monkeypatch.setattr(gen_env.module, "dispatch_generation_worker",
                        dispatch)


@pytest.fixture
def bedrock(gen_env, monkeypatch):
    """Mocked Converse API client injected through get_bedrock_client.

    ``client_requests`` records every (region, timeout_seconds) the
    handler asked a client for, so tests can assert the clamped timeout.
    The default response returns the reference scaffold for the default
    declaration (a valid, buildable file map).
    """
    client = MagicMock(name="bedrock-runtime")
    client.converse.return_value = converse_tool_response(
        {"files": dict(gen_env.reference_files)},
        text="Here is the scaffold.")
    client_requests = []

    def fake_get_bedrock_client(region, timeout_seconds):
        client_requests.append((region, timeout_seconds))
        return client

    monkeypatch.setattr(gen_env.module, "get_bedrock_client",
                        fake_get_bedrock_client)
    return SimpleNamespace(client=client, client_requests=client_requests)


@pytest.fixture
def ctx(gen_env):
    """A fresh Use_Case and a UseCaseAdmin authorized on it."""
    usecase_id = f"uc-{uuid.uuid4()}"
    gen_env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "UC", "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "UseCaseAdmin"}
    gen_env.stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id, "role": "UseCaseAdmin",
    })
    return SimpleNamespace(usecase_id=usecase_id, user=user)


def invoke(gen_env, resource, user, body=None, session_id=None,
           method="POST"):
    """Invoke the handler; returns (status_code, parsed_body)."""
    event = {
        "httpMethod": method,
        "resource": resource,
        "path": resource.replace("{session}", session_id or ""),
        "pathParameters": {"session": session_id} if session_id else None,
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
    response = gen_env.module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def settle(gen_env, ctx, status, body):
    """Resolve a started turn to its effective outcome via the poll route.

    A 202 start response is polled (the synchronous_worker fixture already
    settled the turn); a completed turn returns (200, turn payload) - the
    former synchronous success shape - and a failed turn returns
    (turn_error.http_status, {"error": turn_error}) - the former
    synchronous error shape. Non-202 start responses pass through.
    """
    if status != 202:
        return status, body
    poll_status, turn = invoke(
        gen_env, "/plugins/generate/{session}", ctx.user,
        session_id=body["session_id"], method="GET")
    assert poll_status == 200
    assert turn["turn_status"] in ("completed", "failed")
    if turn["turn_status"] == "completed":
        return 200, turn
    error = turn["turn_error"]
    return error.get("http_status", 502), {"error": error}


def generate(gen_env, ctx, prompt, declaration=None):
    """Start a generation turn (POST /plugins/generate) and poll it to
    its settled outcome."""
    status, body = invoke(gen_env, "/plugins/generate", ctx.user, {
        "usecase_id": ctx.usecase_id,
        "prompt": prompt,
        "declaration": declaration or gen_env.declaration,
    })
    return settle(gen_env, ctx, status, body)


def followup(gen_env, ctx, session_id, prompt):
    """Start a follow-up turn (POST .../message) and poll it to its
    settled outcome."""
    status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                          ctx.user, {"prompt": prompt},
                          session_id=session_id)
    return settle(gen_env, ctx, status, body)


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
    """All portal-S3 keys under the Use_Case's plugin-sources prefix."""
    response = gen_env.s3.list_objects_v2(
        Bucket=gen_env.bucket, Prefix=f"plugin-sources/{usecase_id}/")
    return [obj["Key"] for obj in response.get("Contents", [])]


def assert_failed_first_turn_untouched(gen_env, usecase_id):
    """A failed first turn leaves the session record with no message
    history and no source snapshot, so the prompt stays retryable (2.6,
    2.7). (The session record itself exists: it carries the turn state
    for the poll route in the async start/poll flow.)"""
    (session,) = sessions_for(gen_env, usecase_id)
    assert session["turn_status"] == "failed"
    assert session["messages"] == []
    assert session.get("current_source_key") is None
    assert s3_keys_for(gen_env, usecase_id) == []


def read_snapshot(gen_env, usecase_id, session_id):
    obj = gen_env.s3.get_object(
        Bucket=gen_env.bucket,
        Key=(f"plugin-sources/{usecase_id}/gen-sessions/{session_id}/"
             "current_source.json"),
    )
    return json.loads(obj["Body"].read().decode("utf-8"))


# ===========================================================================
# 1. Prompt and template-convention assembly (Requirement 2.2)
# ===========================================================================

class TestPromptAndConventionAssembly:

    def test_system_prompt_embeds_conventions_declaration_and_hook_contract(
            self, gen_env, bedrock, ctx):
        """The converse() call carries the declaration, the rendered
        reference scaffold (the Plugin_Scaffold template conventions),
        and the Frame_Processing_Hook contract in the system prompt, and
        the bare prompt as the user turn (Requirement 2.2)."""
        from workflow_core.scaffold import HOOK_FILE

        prompt = "Blur each incoming frame"
        status, _ = generate(gen_env, ctx, prompt)
        assert status == 200

        assert bedrock.client.converse.call_count == 1
        kwargs = bedrock.client.converse.call_args.kwargs
        system_text = kwargs["system"][0]["text"]
        # Frame_Processing_Hook contract
        assert "process_frame(frame, params)" in system_text
        assert HOOK_FILE in system_text
        # Declaration and template conventions (the reference scaffold)
        assert json.dumps(gen_env.declaration, sort_keys=True) in system_text
        assert json.dumps(gen_env.reference_files,
                          sort_keys=True) in system_text

        messages = kwargs["messages"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][0]["text"] == prompt

    def test_tool_config_forces_scaffold_tool_requiring_every_file(
            self, gen_env, bedrock, ctx):
        """The toolConfig forces create_plugin_scaffold and its
        inputSchema requires every file of the reference scaffold, so the
        model must return a complete buildable file map (2.2)."""
        from workflow_core.scaffold import (
            HOOK_FILE, README_FILE, build_config_path, c_source_path,
        )

        status, _ = generate(gen_env, ctx, "Blur frames")
        assert status == 200

        tool_config = bedrock.client.converse.call_args.kwargs["toolConfig"]
        assert tool_config["toolChoice"] == {"tool": {"name": TOOL_NAME}}

        (tool,) = tool_config["tools"]
        assert tool["toolSpec"]["name"] == TOOL_NAME
        schema = tool["toolSpec"]["inputSchema"]["json"]
        assert "files" in schema["required"]
        expected = {HOOK_FILE, README_FILE,
                    c_source_path(gen_env.declaration)}
        expected.update(build_config_path(arch) for arch in ARCHITECTURES)
        files_schema = schema["properties"]["files"]
        assert set(files_schema["required"]) == expected
        assert set(files_schema["properties"]) == expected

    def test_configured_model_and_inference_parameters_are_used(
            self, gen_env, bedrock, ctx):
        """The Bedrock_Configuration from the settings table drives the
        model id, inference parameters, region, and timeout; temperature
        wins over top_p when both are configured (2.2, 2.7)."""
        put_bedrock_config(gen_env, model_id="amazon.nova-pro-v1:0",
                           region="eu-west-1", max_tokens=1024,
                           temperature=0.7, top_p=0.5, timeout_seconds=30)

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        assert payload["model_id"] == "amazon.nova-pro-v1:0"

        kwargs = bedrock.client.converse.call_args.kwargs
        assert kwargs["modelId"] == "amazon.nova-pro-v1:0"
        assert kwargs["inferenceConfig"] == {"maxTokens": 1024,
                                             "temperature": 0.7}
        assert "topP" not in kwargs["inferenceConfig"]
        assert bedrock.client_requests[-1] == ("eu-west-1", 30)


# ===========================================================================
# 2. Tool-use output handling
# ===========================================================================

class TestToolUseOutputHandling:

    def test_valid_tool_output_returns_files_and_persists_session_snapshot(
            self, gen_env, bedrock, ctx):
        """A valid create_plugin_scaffold tool input yields 200 with the
        generated file map, a persisted chat session, and the source
        snapshot in S3 (Requirements 2.2, 2.3)."""
        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        assert payload["files"] == gen_env.reference_files
        assert payload["assistant_text"] == "Here is the scaffold."

        (session,) = sessions_for(gen_env, ctx.usecase_id)
        assert session["session_id"] == payload["session_id"]
        assert session["user_id"] == ctx.user["user_id"]
        assert [m["role"] for m in session["messages"]] == ["user",
                                                            "assistant"]
        assert session["messages"][0]["text"] == "Blur frames"

        snapshot = read_snapshot(gen_env, ctx.usecase_id,
                                 payload["session_id"])
        assert snapshot == gen_env.reference_files

    def test_response_without_tool_call_returns_502(self, gen_env, bedrock,
                                                    ctx):
        """A model response with no create_plugin_scaffold tool call
        yields 502 NO_SCAFFOLD_RETURNED and persists nothing, preserving
        the prompt for retry."""
        bedrock.client.converse.return_value = {
            "output": {"message": {"role": "assistant",
                                   "content": [{"text": "I cannot help."}]}},
            "stopReason": "end_turn",
        }

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 502
        assert payload["error"]["code"] == "NO_SCAFFOLD_RETURNED"
        assert payload["error"]["details"]["stop_reason"] == "end_turn"

        assert_failed_first_turn_untouched(gen_env, ctx.usecase_id)


# ===========================================================================
# 3. Follow-up source inclusion (Requirement 2.4)
# ===========================================================================

class TestFollowUpSourceInclusion:

    def test_followup_embeds_current_source_and_replays_history(
            self, gen_env, bedrock, ctx):
        """A follow-up prompt replays the session history and sends a
        user turn embedding the current generated source with an
        instruction to modify rather than regenerate (Requirement 2.4)."""
        status, first = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        session_id = first["session_id"]
        stored_snapshot = read_snapshot(gen_env, ctx.usecase_id, session_id)

        status, second = followup(gen_env, ctx, session_id,
                                  "Make the blur stronger")
        assert status == 200
        assert second["session_id"] == session_id

        assert bedrock.client.converse.call_count == 2
        messages = bedrock.client.converse.call_args.kwargs["messages"]

        # History replayed: first user turn, assistant turn, new user turn.
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[0]["content"][0]["text"] == "Blur frames"

        followup_text = messages[2]["content"][0]["text"]
        assert followup_text.startswith("Make the blur stronger")
        assert "CURRENT PLUGIN SCAFFOLD SOURCE (JSON file map):" \
            in followup_text
        # The session's stored source snapshot is embedded verbatim.
        assert json.dumps(stored_snapshot, sort_keys=True) in followup_text
        assert ("rather than generating new source from scratch"
                in followup_text)

        # Session history now holds both exchanges.
        (session,) = sessions_for(gen_env, ctx.usecase_id)
        assert [m["role"] for m in session["messages"]] == \
            ["user", "assistant", "user", "assistant"]

    def test_followup_updates_snapshot_to_modified_source(self, gen_env,
                                                          bedrock, ctx):
        """The modified file map returned for a follow-up becomes the
        session's new source snapshot (2.4)."""
        from workflow_core.scaffold import HOOK_FILE

        status, first = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        session_id = first["session_id"]

        modified = dict(gen_env.reference_files)
        modified[HOOK_FILE] = modified[HOOK_FILE] + "\n# stronger blur\n"
        bedrock.client.converse.return_value = converse_tool_response(
            {"files": modified}, text="Strengthened the blur.")

        status, payload = followup(gen_env, ctx, session_id,
                                   "Make the blur stronger")
        assert status == 200
        assert payload["files"] == modified
        assert read_snapshot(gen_env, ctx.usecase_id, session_id) == modified


# ===========================================================================
# 4. Scaffold-validation rejection with prompt preservation (Requirement 2.6)
# ===========================================================================

class TestScaffoldValidationRejection:

    def test_unbuildable_first_output_fails_turn_with_422_and_no_history(
            self, gen_env, bedrock, ctx):
        """Tool output that does not form a buildable Plugin_Scaffold
        (missing hook file) fails the turn with GENERATED_SCAFFOLD_INVALID
        (422) naming the defect and persists no history or snapshot, so
        the prompt is preserved for retry (Requirement 2.6)."""
        from workflow_core.scaffold import HOOK_FILE

        files = dict(gen_env.reference_files)
        files.pop(HOOK_FILE)
        bedrock.client.converse.return_value = converse_tool_response(
            {"files": files})

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 422
        assert payload["error"]["code"] == "GENERATED_SCAFFOLD_INVALID"
        assert any(HOOK_FILE in d
                   for d in payload["error"]["details"]["defects"])

        assert_failed_first_turn_untouched(gen_env, ctx.usecase_id)

    def test_unbuildable_followup_output_preserves_session_and_snapshot(
            self, gen_env, bedrock, ctx):
        """An unbuildable follow-up output rejects with 422 while the
        session history and the current source snapshot stay exactly as
        they were, preserving the prompt for retry (2.6)."""
        from workflow_core.scaffold import HOOK_FILE

        status, first = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        session_id = first["session_id"]
        (session_before,) = sessions_for(gen_env, ctx.usecase_id)

        # An emptied hook file is not a buildable Plugin_Scaffold.
        broken = dict(gen_env.reference_files)
        broken[HOOK_FILE] = ""
        bedrock.client.converse.return_value = converse_tool_response(
            {"files": broken})

        status, payload = followup(gen_env, ctx, session_id,
                                   "Make the blur stronger")
        assert status == 422
        assert payload["error"]["code"] == "GENERATED_SCAFFOLD_INVALID"
        assert payload["error"]["details"]["defects"]

        # Session untouched: same two messages, same snapshot content.
        (session_after,) = sessions_for(gen_env, ctx.usecase_id)
        assert session_after["messages"] == session_before["messages"]
        assert read_snapshot(gen_env, ctx.usecase_id, session_id) == \
            gen_env.reference_files


# ===========================================================================
# 5. Timeout and invocation failure behavior (Requirement 2.7)
# ===========================================================================

class TestTimeoutAndFailure:

    def test_read_timeout_returns_504_with_configured_timeout(self, gen_env,
                                                              bedrock, ctx):
        """A Bedrock read timeout yields 504 GENERATION_TIMEOUT carrying
        the configured timeout, the client was built with that timeout,
        and no session is persisted so the prompt can be retried (2.7)."""
        put_bedrock_config(gen_env, timeout_seconds=45)
        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"
        assert payload["error"]["details"]["timeout_seconds"] == 45
        assert bedrock.client_requests[-1][1] == 45

        assert_failed_first_turn_untouched(gen_env, ctx.usecase_id)

    def test_configured_timeout_is_clamped_to_240_seconds(self, gen_env,
                                                          bedrock, ctx):
        """A stored timeout above the 240-second maximum is clamped before
        the client is built (2.7: at most 240 seconds)."""
        put_bedrock_config(gen_env, timeout_seconds=300)

        status, _ = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        assert bedrock.client_requests[-1][1] == 240

    def test_invocation_client_error_returns_502(self, gen_env, bedrock,
                                                 ctx):
        """A Bedrock invocation failure yields 502 with the Bedrock error
        code identified and persists no session (2.7)."""
        bedrock.client.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException",
                       "Message": "Rate exceeded"}},
            "Converse",
        )

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 502
        assert payload["error"]["code"] == "BEDROCK_INVOCATION_FAILED"
        assert payload["error"]["details"]["bedrock_error_code"] == \
            "ThrottlingException"

        assert_failed_first_turn_untouched(gen_env, ctx.usecase_id)

    def test_unreachable_endpoint_returns_502(self, gen_env, bedrock, ctx):
        """An unreachable Bedrock endpoint yields 502 BEDROCK_UNREACHABLE
        identifying the region, with nothing persisted (2.7)."""
        put_bedrock_config(gen_env, region="eu-central-1")
        bedrock.client.converse.side_effect = EndpointConnectionError(
            endpoint_url="https://bedrock-runtime.eu-central-1.amazonaws.com")

        status, payload = generate(gen_env, ctx, "Blur frames")
        assert status == 502
        assert payload["error"]["code"] == "BEDROCK_UNREACHABLE"
        assert payload["error"]["details"]["region"] == "eu-central-1"

        assert_failed_first_turn_untouched(gen_env, ctx.usecase_id)

    def test_followup_timeout_preserves_session_and_snapshot(self, gen_env,
                                                             bedrock, ctx):
        """A timeout on a follow-up turn leaves the session history and
        the current source snapshot untouched, so the prompt is preserved
        for retry (2.7)."""
        status, first = generate(gen_env, ctx, "Blur frames")
        assert status == 200
        session_id = first["session_id"]
        (session_before,) = sessions_for(gen_env, ctx.usecase_id)

        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
        status, payload = followup(gen_env, ctx, session_id,
                                   "Make the blur stronger")
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"

        (session_after,) = sessions_for(gen_env, ctx.usecase_id)
        assert session_after["messages"] == session_before["messages"]
        assert read_snapshot(gen_env, ctx.usecase_id, session_id) == \
            gen_env.reference_files
