"""
Node_Generator sanity tests (custom-node-designer, task 5.1).

Basic unit coverage of functions/node_generator.py: prompt/tool assembly
helpers, request validation, RBAC denial, and the asynchronous start/poll
generation flow - the start routes return 202 and dispatch a self-invoked
worker; GET /plugins/generate/{session} polls the turn state until it
settles (with invoke_generation stubbed and the async dispatch wired to
run the worker synchronously in-process - the full mocked-Converse
integration suite is task 5.2).

_Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

NODE_GEN_SESSIONS_TABLE_NAME = "test-node-gen-sessions"
SETTINGS_TABLE_NAME = "test-settings-node-generator"

TOOL_NAME = "create_plugin_scaffold"


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
        "architectures": ["x86_64", "arm64_jp5"],
    }
    declaration.update(overrides)
    return declaration


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

    # Re-import so the module binds the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    for module_name in ("node_generator", "workflow_generator",
                        "workflow_validation"):
        sys.modules.pop(module_name, None)
    import node_generator

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=node_generator,
        stack=aws_stack,
        sessions_table=resource.Table(NODE_GEN_SESSIONS_TABLE_NAME),
        s3=boto3.client("s3", region_name=REGION),
        bucket=TEST_ENV["PORTAL_ARTIFACTS_BUCKET"],
    )


def make_user():
    user_id = f"user-{uuid.uuid4()}"
    return {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "UseCaseAdmin"}


def make_usecase(gen_env):
    usecase_id = f"uc-{uuid.uuid4()}"
    gen_env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "UC", "account_id": "123456789012",
    })
    return usecase_id


def grant_admin(gen_env, user, usecase_id):
    gen_env.stack.tables.user_roles.put_item(Item={
        "user_id": user["user_id"], "usecase_id": usecase_id,
        "role": "UseCaseAdmin",
    })


def api_event(resource, user, body=None, session_id=None, method="POST"):
    return {
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


def invoke(gen_env, resource, user, body=None, session_id=None, method="POST"):
    response = gen_env.module.handler(
        api_event(resource, user, body, session_id, method), None)
    return response["statusCode"], json.loads(response["body"])


def poll(gen_env, user, session_id):
    """GET /plugins/generate/{session} - poll the current generation turn."""
    return invoke(gen_env, "/plugins/generate/{session}", user,
                  session_id=session_id, method="GET")


def wire_synchronous_worker(gen_env, monkeypatch):
    """Replace the async self-invocation with an in-process worker run, so
    the dispatched turn settles before the start route's 202 returns."""
    def dispatch(session_id, prompt, user):
        gen_env.module.handler({
            "node_gen_worker": True,
            "session_id": session_id,
            "prompt": prompt,
            "user": user,
        }, None)
    monkeypatch.setattr(gen_env.module, "dispatch_generation_worker", dispatch)


# ---------------------------------------------------------------------------
# Prompt and tool assembly (2.2, 2.4)
# ---------------------------------------------------------------------------

class TestPromptAndToolAssembly:
    def test_tool_config_forces_create_plugin_scaffold(self, gen_env):
        from workflow_core.scaffold import render_scaffold
        declaration = make_declaration()
        reference = render_scaffold(declaration)
        config = gen_env.module.build_tool_config(declaration, reference)

        assert config["toolChoice"] == {"tool": {"name": TOOL_NAME}}
        assert config["tools"][0]["toolSpec"]["name"] == TOOL_NAME

    def test_tool_schema_requires_every_scaffold_file(self, gen_env):
        from workflow_core.scaffold import (
            HOOK_FILE, README_FILE, build_config_path, c_source_path,
            render_scaffold,
        )
        declaration = make_declaration()
        reference = render_scaffold(declaration)
        schema = gen_env.module.build_tool_config(
            declaration, reference)["tools"][0]["toolSpec"]["inputSchema"]["json"]

        files_schema = schema["properties"]["files"]
        assert "files" in schema["required"]
        expected = {HOOK_FILE, README_FILE, c_source_path(declaration),
                    build_config_path("x86_64"), build_config_path("arm64_jp5")}
        assert set(files_schema["required"]) == expected
        assert set(files_schema["properties"]) == expected

    def test_system_prompt_embeds_conventions_and_hook_contract(self, gen_env):
        from workflow_core.scaffold import HOOK_FILE, render_scaffold
        declaration = make_declaration()
        reference = render_scaffold(declaration)
        prompt = gen_env.module.build_system_prompt(declaration, reference)

        # Frame_Processing_Hook contract (2.2)
        assert "process_frame(frame, params)" in prompt
        assert HOOK_FILE in prompt
        # Declaration and the rendered template conventions (2.2)
        assert json.dumps(declaration, sort_keys=True) in prompt
        assert json.dumps(reference, sort_keys=True) in prompt

    def test_first_user_message_is_the_bare_prompt(self, gen_env):
        assert gen_env.module.build_user_message("blur frames", None) == "blur frames"

    def test_followup_message_embeds_source_and_instructs_modification(self, gen_env):
        source_json = json.dumps({"plugin/frame_processing_hook.py": "code"})
        text = gen_env.module.build_user_message("make it stronger", source_json)
        assert "make it stronger" in text
        assert source_json in text
        assert "rather" in text and "scratch" in text  # modify, not regenerate (2.4)


# ---------------------------------------------------------------------------
# Request validation and RBAC
# ---------------------------------------------------------------------------

class TestRequestValidation:
    def test_missing_fields_rejected(self, gen_env):
        user = make_user()
        status, body = invoke(gen_env, "/plugins/generate", user,
                              {"usecase_id": "uc-x"})
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_invalid_declaration_identifies_field(self, gen_env):
        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": make_declaration(typeId="---"),
        })
        assert status == 400
        assert body["error"]["code"] == "INVALID_DECLARATION"
        assert body["error"]["details"]["field"] == "typeId"

    def test_generate_denied_without_node_designer_generate(self, gen_env):
        user = make_user()
        user["role"] = "Viewer"
        usecase_id = make_usecase(gen_env)
        gen_env.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": usecase_id,
            "role": "Viewer",
        })
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": make_declaration(),
        })
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_unknown_route_is_404(self, gen_env):
        user = make_user()
        response = gen_env.module.handler({
            "httpMethod": "GET",
            "resource": "/plugins/generate",
            "requestContext": api_event("/x", user)["requestContext"],
        }, None)
        assert response["statusCode"] == 404

    def test_unknown_session_is_404(self, gen_env):
        user = make_user()
        status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                              user, {"prompt": "again"}, session_id="nope")
        assert status == 404
        assert body["error"]["code"] == "SESSION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Async start/poll flow: turn persistence, scaffold-validation rejection,
# and error surfacing through the poll route (2.3, 2.6, 2.7)
# (invoke_generation stubbed; the mocked-Converse suite is task 5.2)
# ---------------------------------------------------------------------------

def stub_generation(gen_env, monkeypatch, tool_input):
    monkeypatch.setattr(
        gen_env.module, "invoke_generation",
        lambda config, system_prompt, messages, tool_config:
            (tool_input, "done", None))


class TestGenerationTurn:
    def _generate(self, gen_env, monkeypatch, declaration=None):
        from workflow_core.scaffold import render_scaffold
        declaration = declaration or make_declaration()
        files = render_scaffold(declaration)
        stub_generation(gen_env, monkeypatch, {"files": files})
        wire_synchronous_worker(gen_env, monkeypatch)

        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": declaration,
        })
        return user, usecase_id, files, status, body

    def test_start_returns_202_and_poll_returns_files(self, gen_env, monkeypatch):
        user, usecase_id, files, status, body = self._generate(gen_env, monkeypatch)
        # The start route answers 202 immediately (well under the API
        # Gateway 29 s cap); the result arrives through the poll route.
        assert status == 202
        assert body["turn_status"] == "pending"
        assert body["usecase_id"] == usecase_id

        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "completed"
        assert turn["files"] == files
        assert turn["assistant_text"] == "done"
        assert turn["usecase_id"] == usecase_id

        session = gen_env.sessions_table.get_item(
            Key={"session_id": body["session_id"]}).get("Item")
        assert session is not None
        assert session["user_id"] == user["user_id"]
        assert session["turn_status"] == "completed"
        assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
        assert session["messages"][0]["text"] == "blur frames"

        snapshot = gen_env.s3.get_object(
            Bucket=gen_env.bucket, Key=session["current_source_key"])
        assert json.loads(snapshot["Body"].read()) == files

    def test_start_dispatches_worker_and_stays_pending_until_it_runs(
            self, gen_env, monkeypatch):
        from workflow_core.scaffold import render_scaffold
        declaration = make_declaration()
        dispatched = {}
        monkeypatch.setattr(
            gen_env.module, "dispatch_generation_worker",
            lambda session_id, prompt, user: dispatched.update(
                {"session_id": session_id, "prompt": prompt, "user": user}))

        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": declaration,
        })
        assert status == 202
        assert dispatched["session_id"] == body["session_id"]
        assert dispatched["prompt"] == "blur frames"

        # The turn stays pending until the (not yet run) worker settles it.
        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "pending"
        assert "files" not in turn

        # A second prompt cannot race the in-flight turn.
        status, conflict = invoke(
            gen_env, "/plugins/generate/{session}/message", user,
            {"prompt": "again"}, session_id=body["session_id"])
        assert status == 409
        assert conflict["error"]["code"] == "GENERATION_IN_PROGRESS"

        # Running the dispatched worker settles the turn.
        stub_generation(gen_env, monkeypatch, {"files": render_scaffold(declaration)})
        gen_env.module.handler({
            "node_gen_worker": True,
            "session_id": dispatched["session_id"],
            "prompt": dispatched["prompt"],
            "user": dispatched["user"],
        }, None)
        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "completed"

    def test_invalid_output_fails_turn_with_defects_and_untouched_history(
            self, gen_env, monkeypatch):
        # Missing hook file -> not a buildable Plugin_Scaffold (2.6)
        declaration = make_declaration()
        from workflow_core.scaffold import HOOK_FILE, render_scaffold
        files = dict(render_scaffold(declaration))
        files.pop(HOOK_FILE)
        stub_generation(gen_env, monkeypatch, {"files": files})
        wire_synchronous_worker(gen_env, monkeypatch)

        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": declaration,
        })
        assert status == 202

        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "failed"
        error = turn["turn_error"]
        assert error["code"] == "GENERATED_SCAFFOLD_INVALID"
        assert error["http_status"] == 422
        assert any(HOOK_FILE in d for d in error["details"]["defects"])

        # History/snapshot untouched: the prompt is preserved client-side
        # for retry (2.6).
        session = gen_env.sessions_table.get_item(
            Key={"session_id": body["session_id"]}).get("Item")
        assert session["messages"] == []
        assert session.get("current_source_key") is None

    def test_bedrock_failure_surfaces_through_poll(self, gen_env, monkeypatch):
        declaration = make_declaration()
        monkeypatch.setattr(
            gen_env.module, "invoke_generation",
            lambda config, system_prompt, messages, tool_config:
                (None, None, gen_env.module.error_response(
                    504, "GENERATION_TIMEOUT",
                    "Scaffold generation timed out after 60 seconds.")))
        wire_synchronous_worker(gen_env, monkeypatch)

        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": declaration,
        })
        assert status == 202

        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "failed"
        assert turn["turn_error"]["code"] == "GENERATION_TIMEOUT"
        assert turn["turn_error"]["http_status"] == 504

    def test_worker_exception_marks_turn_failed(self, gen_env, monkeypatch):
        declaration = make_declaration()

        def boom(config, system_prompt, messages, tool_config):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(gen_env.module, "invoke_generation", boom)
        wire_synchronous_worker(gen_env, monkeypatch)

        user = make_user()
        usecase_id = make_usecase(gen_env)
        grant_admin(gen_env, user, usecase_id)
        status, body = invoke(gen_env, "/plugins/generate", user, {
            "usecase_id": usecase_id,
            "prompt": "blur frames",
            "declaration": declaration,
        })
        assert status == 202

        status, turn = poll(gen_env, user, body["session_id"])
        assert status == 200
        assert turn["turn_status"] == "failed"
        assert turn["turn_error"]["code"] == "INTERNAL_ERROR"

    def test_followup_embeds_current_source(self, gen_env, monkeypatch):
        user, usecase_id, files, status, body = self._generate(gen_env, monkeypatch)
        assert status == 202
        session_id = body["session_id"]

        captured = {}

        def capture(config, system_prompt, messages, tool_config):
            captured["messages"] = messages
            return {"files": files}, "done", None

        monkeypatch.setattr(gen_env.module, "invoke_generation", capture)
        status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                              user, {"prompt": "stronger blur"},
                              session_id=session_id)
        assert status == 202
        assert body["turn_status"] == "pending"

        status, turn = poll(gen_env, user, session_id)
        assert status == 200
        assert turn["turn_status"] == "completed"

        user_turn = captured["messages"][-1]["content"][0]["text"]
        assert "stronger blur" in user_turn
        assert json.dumps(files, sort_keys=True) in user_turn  # 2.4

    def test_failed_followup_keeps_prior_source_and_history(self, gen_env, monkeypatch):
        user, usecase_id, files, status, body = self._generate(gen_env, monkeypatch)
        assert status == 202
        session_id = body["session_id"]
        status, turn = poll(gen_env, user, session_id)
        assert turn["turn_status"] == "completed"

        # Follow-up whose generation fails validation (empty file map).
        stub_generation(gen_env, monkeypatch, {"files": {}})
        status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                              user, {"prompt": "stronger blur"},
                              session_id=session_id)
        assert status == 202
        status, turn = poll(gen_env, user, session_id)
        assert turn["turn_status"] == "failed"
        assert turn["turn_error"]["code"] == "GENERATED_SCAFFOLD_INVALID"

        # The prior successful turn's history and snapshot survive, so a
        # retried follow-up still modifies the current source (2.6, 2.7).
        session = gen_env.sessions_table.get_item(
            Key={"session_id": session_id}).get("Item")
        assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
        snapshot = gen_env.s3.get_object(
            Bucket=gen_env.bucket, Key=session["current_source_key"])
        assert json.loads(snapshot["Body"].read()) == files

        stub_generation(gen_env, monkeypatch, {"files": files})
        status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                              user, {"prompt": "stronger blur"},
                              session_id=session_id)
        assert status == 202
        status, turn = poll(gen_env, user, session_id)
        assert turn["turn_status"] == "completed"
        assert turn["files"] == files

    def test_poll_unknown_session_is_404(self, gen_env):
        user = make_user()
        status, body = poll(gen_env, user, "nope")
        assert status == 404
        assert body["error"]["code"] == "SESSION_NOT_FOUND"

    def test_accept_creates_generated_plugin_record_with_prompt_provenance(
            self, gen_env, monkeypatch):
        user, usecase_id, files, status, body = self._generate(gen_env, monkeypatch)
        assert status == 202
        session_id = body["session_id"]
        status, turn = poll(gen_env, user, session_id)
        assert turn["turn_status"] == "completed"

        status, body = invoke(gen_env, "/plugins/generate/{session}/message",
                              user, {"accept": True}, session_id=session_id)
        assert status == 201
        plugin = body["plugin"]
        # Standard build/simulate/lifecycle entry point (2.5)
        assert plugin["kind"] == "generated"
        assert plugin["lifecycle_state"] == "dev"
        assert plugin["review"]["decision"] == "pending"
        assert plugin["provenance"]["prompt"] == "blur frames"
        assert plugin["provenance"]["prompts"] == ["blur frames"]
        assert plugin["provenance"]["generatedBy"] == user["user_id"]

        # Source written under the standard plugin-sources layout
        prefix = plugin["source_s3_prefix"]
        listing = gen_env.s3.list_objects_v2(
            Bucket=gen_env.bucket, Prefix=prefix)
        stored = {obj["Key"][len(prefix):] for obj in listing["Contents"]}
        assert stored == set(files)

        # And the Plugin_Record is readable through the records API
        record = gen_env.stack.tables.plugin_records.get_item(
            Key={"plugin_id": plugin["plugin_id"], "version": 1}).get("Item")
        assert record is not None
        assert record["usecase_id"] == usecase_id
