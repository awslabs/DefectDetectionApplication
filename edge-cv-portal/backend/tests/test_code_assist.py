"""
Code assist endpoint unit tests (custom-node-code-assist, task 2.7).

POST /code-assist (functions/code_assist.py, dispatched from
workflow_generator.handler) is exercised against the shared moto stack
from conftest.py with the Bedrock Converse API mocked. Covered:

1. Handler route dispatch: POST /code-assist reaches
   code_assist.handle_code_assist; unknown resources return 404.
2. Request-validation 400 matrix: MISSING_FIELDS, INVALID_SURFACE,
   INVALID_CONTRACT, INVALID_PROMPT (whitespace-only / over 4,000 chars),
   INVALID_JSON - all settled without any Bedrock client construction.
3. RBAC matrix per surface (6.1, 6.2): workflow create/edit permission for
   workflow-builder (DataScientist / UseCaseAdmin / PortalAdmin allowed;
   Viewer / Operator denied); UseCaseAdmin-in-Use_Case or PortalAdmin for
   node-designer (DataScientist also denied). Denial returns the uniform
   403 FORBIDDEN envelope, writes the unauthorized_access audit entry
   carrying the acting user, surface, Use_Case, and timestamp (6.3), and
   constructs no Bedrock client.
4. Mocked Converse failure paths (5.1-5.3): a read timeout returns 504
   GENERATION_TIMEOUT with details.timeout_seconds echoing the clamped
   configured value; a response without the provide_code tool call (or
   with empty code) returns 422 NO_CODE_RETURNED.
5. Happy path: a valid provide_code tool response returns 200 with
   {code, notes, model_id, contract}.

_Requirements: 5.1, 5.2, 5.3, 6.1, 6.2, 6.3_
"""
import json
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ReadTimeoutError

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-code-assist"

TOOL_NAME = "provide_code"

# Valid for every contract exercised here: process_frame_or_handle
# (exactly one of process_frame/handle), process_frame, and frame_hook
# all accept a lone top-level process_frame definition.
VALID_CODE = "def process_frame(frame, metadata):\n    return None\n"


def tool_response(code, notes="One short paragraph."):
    """A Converse API response whose assistant message calls provide_code."""
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tool-1", "name": TOOL_NAME,
                         "input": {"code": code, "notes": notes}}},
        ]}},
        "stopReason": "tool_use",
    }


TEXT_ONLY_RESPONSE = {
    "output": {"message": {"role": "assistant",
                           "content": [{"text": "I cannot help."}]}},
    "stopReason": "end_turn",
}


@pytest.fixture(scope="module")
def ca(aws_stack):
    """Settings table and freshly imported workflow_generator/code_assist
    modules bound to it inside moto (test_workflow_generation pattern;
    workflow_generator reloads bedrock_common and code_assist on import,
    so both rebind SETTINGS_TABLE and moto-intercepted boto3 clients)."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    for module_name in ("workflow_generator", "workflow_validation",
                        "code_assist", "bedrock_common"):
        sys.modules.pop(module_name, None)
    import workflow_generator
    import code_assist

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        generator=workflow_generator,
        code_assist=code_assist,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


@pytest.fixture(autouse=True)
def clean_bedrock_config(ca):
    """Each test starts from the default Bedrock_Configuration."""
    ca.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})
    yield


@pytest.fixture
def bedrock(ca, monkeypatch):
    """Mocked Converse API client injected through code_assist's
    get_bedrock_client binding. ``client_requests`` records every
    (region, timeout_seconds) pair so tests can assert the clamp."""
    client = MagicMock(name="bedrock-runtime")
    client.converse.return_value = tool_response(VALID_CODE)
    client_requests = []

    def fake_get_bedrock_client(region, timeout_seconds):
        client_requests.append((region, timeout_seconds))
        return client

    monkeypatch.setattr(ca.code_assist, "get_bedrock_client",
                        fake_get_bedrock_client)
    return SimpleNamespace(client=client, client_requests=client_requests)


@pytest.fixture
def forbid_bedrock(ca, monkeypatch):
    """Loudly fail if any Bedrock client is constructed: a tripped guard
    surfaces as the handler's 500 INTERNAL_ERROR instead of the expected
    400/403 (Requirement 6.3: denial settles before any Bedrock client)."""
    def explode(*args, **kwargs):
        raise AssertionError("Bedrock client must not be constructed")
    monkeypatch.setattr(ca.code_assist, "get_bedrock_client", explode)


@pytest.fixture
def ctx(env):
    """A fresh Use_Case and a DataScientist authorized on it (holds the
    workflow create/edit permissions for the workflow-builder surface)."""
    usecase_id = env.create_usecase()
    user = env.make_user()
    env.assign_role(user, usecase_id, "DataScientist")
    return SimpleNamespace(usecase_id=usecase_id, user=user, env=env)


def request_body(usecase_id, surface="workflow-builder",
                 contract="process_frame_or_handle",
                 prompt="Add a Gaussian blur filter", **extra):
    body = {"usecase_id": usecase_id, "surface": surface,
            "contract": contract, "prompt": prompt}
    body.update(extra)
    return body


def post_code_assist(ca, user, body, resource="/code-assist",
                     method="POST"):
    """Invoke workflow_generator.handler with a POST /code-assist event
    (exercising the route dispatch on every call); returns
    (status_code, parsed_body)."""
    event = {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "body": body if isinstance(body, str) or body is None
        else json.dumps(body),
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
    response = ca.generator.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def put_bedrock_config(ca, **values):
    """Store a Bedrock_Configuration item the way the settings API does."""
    def to_ddb(value):
        return Decimal(str(value)) if isinstance(value, float) else value
    ca.settings_table.put_item(Item={
        "setting_key": "bedrock_configuration",
        "value": {k: to_ddb(v) for k, v in values.items()},
    })


def denial_audits(aws_stack, user):
    """unauthorized_access audit entries for one acting user (fresh uuid
    user per test, so no cross-test bleed)."""
    return [item for item in aws_stack.tables.audit_log.scan()["Items"]
            if item["user_id"] == user["user_id"]
            and item["action"] == "unauthorized_access"]


# ===========================================================================
# 1. Handler route dispatch
# ===========================================================================

class TestHandlerDispatch:

    def test_post_code_assist_dispatches_to_handle_code_assist(
            self, ca, ctx, monkeypatch):
        """The /code-assist resource branch delegates POSTs to
        code_assist.handle_code_assist with the event and resolved user."""
        sentinel = {"statusCode": 299, "body": json.dumps({"ok": True})}
        seen = {}

        def fake_handle(event, user):
            seen["resource"] = event["resource"]
            seen["user_id"] = user["user_id"]
            return sentinel

        monkeypatch.setattr(ca.generator.code_assist, "handle_code_assist",
                            fake_handle)
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id))
        assert status == 299
        assert payload == {"ok": True}
        assert seen == {"resource": "/code-assist",
                        "user_id": ctx.user["user_id"]}

    def test_unknown_resource_returns_404(self, ca, ctx):
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id),
            resource="/no-such-route")
        assert status == 404
        assert payload["error"]["code"] == "NOT_FOUND"


# ===========================================================================
# 2. Request-validation 400 matrix (no Bedrock traffic)
# ===========================================================================

class TestRequestValidation:

    @pytest.mark.parametrize("missing_field",
                             ["usecase_id", "surface", "contract", "prompt"])
    def test_missing_required_field_returns_400(self, ca, ctx,
                                                forbid_bedrock,
                                                missing_field):
        body = request_body(ctx.usecase_id)
        del body[missing_field]

        status, payload = post_code_assist(ca, ctx.user, body)
        assert status == 400
        assert payload["error"]["code"] == "MISSING_FIELDS"
        assert missing_field in payload["error"]["message"]

    def test_invalid_surface_returns_400(self, ca, ctx, forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, surface="sidebar"))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_SURFACE"

    def test_invalid_contract_returns_400(self, ca, ctx, forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user,
            request_body(ctx.usecase_id, contract="run_forever"))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_CONTRACT"

    def test_whitespace_only_prompt_returns_400(self, ca, ctx,
                                                forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, prompt="  \n\t "))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_PROMPT"

    def test_prompt_over_4000_chars_returns_400(self, ca, ctx,
                                                forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, prompt="x" * 4001))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_PROMPT"
        assert payload["error"]["details"]["max_length"] == 4000

    def test_non_string_current_code_returns_400(self, ca, ctx,
                                                 forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user,
            request_body(ctx.usecase_id, current_code=["not", "a", "str"]))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_JSON"

    def test_non_object_context_returns_400(self, ca, ctx, forbid_bedrock):
        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id, context="params"))
        assert status == 400
        assert payload["error"]["code"] == "INVALID_JSON"

    def test_malformed_json_body_returns_400(self, ca, ctx, forbid_bedrock):
        status, payload = post_code_assist(ca, ctx.user, "{not json")
        assert status == 400
        assert payload["error"]["code"] == "INVALID_JSON"


# ===========================================================================
# 3. RBAC matrix per surface (Requirements 6.1, 6.2, 6.3)
# ===========================================================================

# Surface -> contract used on that surface.
SURFACE_CONTRACTS = {"workflow-builder": "process_frame_or_handle",
                     "node-designer": "frame_hook"}

PERMITTED = [
    ("workflow-builder", "DataScientist"),   # workflow:create / workflow:edit
    ("workflow-builder", "UseCaseAdmin"),
    ("workflow-builder", "PortalAdmin"),
    ("node-designer", "UseCaseAdmin"),       # UseCaseAdmin in the Use_Case
    ("node-designer", "PortalAdmin"),
]

DENIED = [
    ("workflow-builder", "Viewer"),
    ("workflow-builder", "Operator"),
    ("node-designer", "Viewer"),
    ("node-designer", "Operator"),
    ("node-designer", "DataScientist"),      # workflow perms don't carry over
]


def make_actor(env, usecase_id, role):
    """A user holding `role`: PortalAdmin globally from the JWT claim,
    every other role assigned on the Use_Case (matching Team Management)."""
    user = env.make_user(role=role)
    if role != "PortalAdmin":
        env.assign_role(user, usecase_id, role)
    return user


class TestRBACMatrix:

    @pytest.mark.parametrize("surface,role", PERMITTED)
    def test_permitted_role_reaches_generation(self, ca, env, aws_stack,
                                               bedrock, surface, role):
        """Roles holding the surface's permitting permission get 200
        (Requirements 6.1, 6.2)."""
        usecase_id = env.create_usecase()
        user = make_actor(env, usecase_id, role)

        status, payload = post_code_assist(
            ca, user, request_body(usecase_id, surface=surface,
                                   contract=SURFACE_CONTRACTS[surface]))
        assert status == 200, payload
        assert payload["code"] == VALID_CODE
        assert denial_audits(aws_stack, user) == []

    @pytest.mark.parametrize("surface,role", DENIED)
    def test_denied_role_gets_403_with_audit_and_no_bedrock(
            self, ca, env, aws_stack, forbid_bedrock, surface, role):
        """Roles without the permitting permission get the uniform 403
        FORBIDDEN envelope, an unauthorized_access audit entry carrying
        the acting user, surface, Use_Case, and timestamp, and no Bedrock
        client is ever constructed (Requirement 6.3)."""
        usecase_id = env.create_usecase()
        user = make_actor(env, usecase_id, role)

        status, payload = post_code_assist(
            ca, user, request_body(usecase_id, surface=surface,
                                   contract=SURFACE_CONTRACTS[surface]))
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"
        assert payload["error"]["details"]["surface"] == surface
        assert payload["error"]["details"]["usecase_id"] == usecase_id

        (entry,) = denial_audits(aws_stack, user)
        assert entry["user_id"] == user["user_id"]
        assert entry["result"] == "denied"
        assert entry["resource_type"] == "code_assist"
        assert entry["details"]["surface"] == surface
        assert entry["details"]["usecase_id"] == usecase_id
        assert entry["timestamp"] > 0

    def test_usecase_admin_of_another_usecase_is_denied(self, ca, env,
                                                        aws_stack,
                                                        forbid_bedrock):
        """UseCaseAdmin authorizes node-designer use only within its own
        Use_Case (Requirement 6.2)."""
        home_usecase = env.create_usecase()
        target_usecase = env.create_usecase()
        user = env.make_user(role="Viewer")
        env.assign_role(user, home_usecase, "UseCaseAdmin")

        status, payload = post_code_assist(
            ca, user, request_body(target_usecase, surface="node-designer",
                                   contract="frame_hook"))
        assert status == 403
        assert payload["error"]["code"] == "FORBIDDEN"
        (entry,) = denial_audits(aws_stack, user)
        assert entry["details"]["usecase_id"] == target_usecase


# ===========================================================================
# 4. Mocked Converse failure paths (Requirements 5.1, 5.2, 5.3)
# ===========================================================================

class TestBedrockFailures:

    def test_read_timeout_returns_504_with_configured_timeout(self, ca,
                                                              bedrock, ctx):
        """A Bedrock read timeout yields 504 GENERATION_TIMEOUT with
        details.timeout_seconds equal to the configured value, and the
        client was built with that timeout (Requirement 5.2)."""
        put_bedrock_config(ca, timeout_seconds=45)
        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id))
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"
        assert payload["error"]["details"]["timeout_seconds"] == 45
        assert "45" in payload["error"]["message"]
        assert bedrock.client_requests[-1][1] == 45

    def test_read_timeout_echoes_clamped_timeout(self, ca, bedrock, ctx):
        """A stored timeout above the 240-second maximum is clamped before
        the client is built, and the 504 echoes the clamped value
        (Requirement 5.2: the applied timeout, at most 240 seconds)."""
        put_bedrock_config(ca, timeout_seconds=300)
        bedrock.client.converse.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id))
        assert status == 504
        assert payload["error"]["code"] == "GENERATION_TIMEOUT"
        assert payload["error"]["details"]["timeout_seconds"] == 240
        assert bedrock.client_requests[-1][1] == 240

    def test_response_without_tool_call_returns_422_no_code(self, ca,
                                                            bedrock, ctx):
        """A model response with no provide_code tool call yields 422
        NO_CODE_RETURNED (Requirement 5.3)."""
        bedrock.client.converse.return_value = TEXT_ONLY_RESPONSE

        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id))
        assert status == 422
        assert payload["error"]["code"] == "NO_CODE_RETURNED"
        assert payload["error"]["details"]["stop_reason"] == "end_turn"

    def test_empty_tool_code_returns_422_no_code(self, ca, bedrock, ctx):
        """A provide_code call whose code is empty/whitespace yields 422
        NO_CODE_RETURNED (Requirement 5.3: empty output)."""
        bedrock.client.converse.return_value = tool_response("   \n")

        status, payload = post_code_assist(
            ca, ctx.user, request_body(ctx.usecase_id))
        assert status == 422
        assert payload["error"]["code"] == "NO_CODE_RETURNED"


# ===========================================================================
# 5. Happy path
# ===========================================================================

class TestHappyPath:

    def test_returns_code_notes_model_id_and_contract(self, ca, bedrock,
                                                      ctx):
        """A valid provide_code tool response returns 200 with exactly
        {code, notes, model_id, contract}: the generated module, the
        model's notes, the configured model id, and the request's
        contract echoed back."""
        put_bedrock_config(ca, model_id="amazon.nova-pro-v1:0")
        bedrock.client.converse.return_value = tool_response(
            VALID_CODE, notes="Blurs each frame with a 5x5 kernel.")

        status, payload = post_code_assist(
            ca, ctx.user,
            request_body(ctx.usecase_id, contract="process_frame_or_handle"))
        assert status == 200
        assert payload == {
            "code": VALID_CODE,
            "notes": "Blurs each frame with a 5x5 kernel.",
            "model_id": "amazon.nova-pro-v1:0",
            "contract": "process_frame_or_handle",
        }
        assert bedrock.client.converse.call_count == 1
