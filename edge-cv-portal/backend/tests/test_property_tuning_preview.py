"""Property test for the Candidate preview (spec:
.kiro/specs/quality-prompt-tuning, task 6.4).

**Feature: quality-prompt-tuning, Property 18: Candidate preview shows the
exact request text and warns on parser-hostile settings** — **Validates:
Requirements 5.3, 5.4, 5.5**

*For any* Candidate Prompt_Set, the previewed user message equals the
``prompt`` of ``build_bedrock_invocation`` (or the rendered
``build_llm_invocation`` prompt) for that Prompt_Set, the previewed system
text equals its ``system_prompt``, ``max_tokens`` below 64 produces the
truncation warning, and a prompt or system prompt specifying a JSON answer
without the key ``is_anomalous`` produces the schema warning, never blocking
the edit.

How this is driven
------------------

Each example asserts the same Prompt_Set three ways:

1. against an **independent restatement** transcribed in this file — the
   Verdict_Instruction and its separator, the system-text rule and both
   warning rules, none of them imported from ``workflow_tuning`` — so a
   mutation of the module fails here;
2. against the shared Invocation_Builder itself
   (``workflow_core.anomaly_invocation.build_bedrock_invocation`` /
   ``build_llm_invocation``), which is the literal wording of the property
   and of Requirement 5.3 ("the exact user-message text the
   Invocation_Builder will send");
3. through the REAL ``GET .../candidates/{cid}/preview`` route against moto,
   so what the editor is shown — and not only what a helper returns — is
   what is asserted.

"Never blocking" is asserted in both directions: the route answers 200 with
the warnings, and the Candidate that produced them is created and editable.

The enumerated cases over the same space are task 6.1's
``test_tuning_session_routes.py``.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"
ANOMALY = "/workflow-tuning/anomaly"

# --------------------------------------------------------------------------
# Independent restatement of the contract under test.
# --------------------------------------------------------------------------

#: Requirement 5.3 / design: the Verdict_Instruction the Invocation_Builder
#: appends to every Anomaly_Mode user prompt, and its separator.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')
INSTRUCTION_SEPARATOR = "\n\n"

#: Requirement 5.5: below this budget a truncated answer fails the parser.
REF_MIN_SAFE_MAX_TOKENS = 64

#: The warning codes (design "Frontend"/Requirements 5.4, 5.5).
REF_TRUNCATION_CODE = "max_tokens_truncation"
REF_SCHEMA_CODE = "answer_schema_missing_is_anomalous"

#: Requirement 5.4's "demands an answer format": the documented heuristic —
#: the text mentions JSON or shows a JSON object literal — restated here so
#: the interpretation is testable rather than implied.
def ref_demands_json(text: str) -> bool:
    lowered = text.lower()
    return "json" in lowered or ("{" in text and "}" in text)


def ref_warnings(prompt, system_prompt, max_tokens):
    """Requirements 5.4 and 5.5, restated: the warnings a Prompt_Set earns
    (order: the token budget first, then prompt, then system prompt)."""
    warnings = []
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) \
            and max_tokens < REF_MIN_SAFE_MAX_TOKENS:
        warnings.append((REF_TRUNCATION_CODE, None))
    for field, text in (("prompt", prompt), ("systemPrompt", system_prompt)):
        if not text:
            continue
        if ref_demands_json(text) and "is_anomalous" not in text.lower():
            warnings.append((REF_SCHEMA_CODE, field))
    return warnings


def ref_user_message(prompt):
    """Requirement 5.3: the Candidate's prompt followed by the
    Verdict_Instruction, separated by a blank line."""
    return str(prompt or "") + INSTRUCTION_SEPARATOR + VERDICT_INSTRUCTION


def ref_system_text(system_prompt):
    """The system text an Anomaly_Mode invocation carries: the operator's
    text VERBATIM, or none at all when it is absent or blank."""
    if system_prompt is None:
        return None
    text = str(system_prompt)
    return text if text.strip() else None


BEDROCK_NODE_ID = "bedrock_1"
LLM_NODE_ID = "llm_1"

BEDROCK_NODE = {
    "id": BEDROCK_NODE_ID, "type": "bedrock_inference",
    "position": {"x": 0, "y": 0},
    "parameters": {"model": "us.amazon.nova-lite-v1:0",
                   "prompt": "the deployed prompt",
                   "system_prompt": "the deployed system prompt",
                   "max_tokens": 256, "region": "us-west-2",
                   "anomaly_mode": True}}
LLM_NODE = {
    "id": LLM_NODE_ID, "type": "llm_inference",
    "position": {"x": 300, "y": 0},
    "parameters": {"modelName": "qwen2-vl",
                   "prompt_template": "the deployed template",
                   "system_prompt": "the deployed system prompt",
                   "max_tokens": 512, "temperature": 0.2,
                   "anomaly_mode": True}}


# ==========================================================================
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    if TUNING_TABLE_NAME not in dynamodb.list_tables().get("TableNames", []):
        dynamodb.create_table(
            TableName=TUNING_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=SAMPLE_BUCKET)
    except Exception:
        pass
    os.environ["WORKFLOW_TUNING_TABLE"] = TUNING_TABLE_NAME
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning

    return workflow_tuning


class World:
    """One Use_Case, one workflow with both Tunable_Node types, and one
    Tuning_Session per node — built once, since a preview mutates nothing."""

    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        self.editor = self._user("DataScientist")
        self.workflow_id = self._put_workflow()
        self.sessions = {
            BEDROCK_NODE_ID: self.open_session(BEDROCK_NODE_ID),
            LLM_NODE_ID: self.open_session(LLM_NODE_ID),
        }

    @staticmethod
    def _user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def _put_workflow(self):
        workflow_id = str(uuid.uuid4())
        document = {"schemaVersion": 1,
                    "nodes": [json.loads(json.dumps(BEDROCK_NODE)),
                              json.loads(json.dumps(LLM_NODE))],
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/1/"
               f"workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Tuning workflow",
            "created_at": 1, "updated_at": 1, "latest_version": 1,
            "created_by": self.editor["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": 1,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.editor["user_id"]})
        return workflow_id

    def call(self, method, resource, path_params=None, body=None, query=None,
             user=None):
        user = user or self.editor
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"] or "{}")

    def open_session(self, node_id):
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def create_candidate(self, session_id, prompt, system_prompt, max_tokens,
                         name="Candidate"):
        payload = {"name": name, "prompt": prompt,
                   "systemPrompt": system_prompt}
        if max_tokens is not None:
            payload["maxTokens"] = max_tokens
        status, body = self.call("POST",
                                 f"{ANOMALY}/sessions/{{id}}/candidates",
                                 {"id": session_id}, body=payload)
        return status, body

    def preview(self, session_id, candidate_id):
        return self.call("GET", f"{ANOMALY}/candidates/{{cid}}/preview",
                         {"cid": candidate_id},
                         query={"session_id": session_id})


@pytest.fixture(scope="module")
def world(aws_stack, tuning):
    return World(aws_stack, tuning)


# ==========================================================================
# Property 18
# ==========================================================================

#: Prompt fragments that cover the answer-format heuristic's whole space:
#: plain text, "json" in words, a JSON object literal, and the key the
#: Verdict_Parser needs.
FRAGMENTS = (
    "Compare the input image to the reference image.",
    "Answer in JSON.",
    'Return {"verdict": "bad"}',
    "Include is_anomalous and confidence.",
    "respond with json only",
    "{}",
    "List the differences that are defects.",
    'Answer {"is_anomalous": true}',
)

prompt_texts = st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=3) \
    .map(lambda parts: " ".join(parts).strip()) \
    .filter(lambda text: bool(text.strip()))

system_texts = st.one_of(
    st.just(""), st.just("   "),
    st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=2)
    .map(lambda parts: " ".join(parts)))


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(node_id=st.sampled_from((BEDROCK_NODE_ID, LLM_NODE_ID)),
       prompt=prompt_texts, system_prompt=system_texts,
       max_tokens=st.one_of(st.none(),
                            st.integers(min_value=1, max_value=4096)))
def test_property_preview_shows_the_exact_request_text_and_warns(
        world, node_id, prompt, system_prompt, max_tokens):
    """**Feature: quality-prompt-tuning, Property 18: Candidate preview
    shows the exact request text and warns on parser-hostile settings** —
    **Validates: Requirements 5.3, 5.4, 5.5**

    *For any* Candidate Prompt_Set, the previewed user message equals the
    ``prompt`` of ``build_bedrock_invocation`` (or the rendered
    ``build_llm_invocation`` prompt) for that Prompt_Set, the previewed
    system text equals its ``system_prompt``, ``max_tokens`` below 64
    produces the truncation warning, and a prompt or system prompt
    specifying a JSON answer without the key ``is_anomalous`` produces the
    schema warning, never blocking the edit.
    """
    module = world.module
    node = json.loads(json.dumps(
        BEDROCK_NODE if node_id == BEDROCK_NODE_ID else LLM_NODE))
    prompt_set = {"prompt": prompt, "systemPrompt": system_prompt,
                  "maxTokens": max_tokens}

    # -- 1. the module's own preview, against the restatement ------------
    preview = module.build_preview(node, prompt_set)
    warnings = module.preview_warnings(prompt, system_prompt, max_tokens)

    assert preview["userMessage"] == ref_user_message(prompt)
    assert preview["systemText"] == ref_system_text(system_prompt)
    # The token budget shown is the Prompt_Set's; a Prompt_Set that carries
    # none keeps the node's own Node_Parameter (the catalog default of 256
    # when the node has none either).
    assert preview["maxTokens"] == (
        max_tokens if max_tokens is not None
        else (node["parameters"].get("max_tokens") or 256))
    if node_id == LLM_NODE_ID:
        # A preview has no Run_Metadata, so the template is shown as it is.
        assert preview["templateRendered"] is False
        assert preview["model"] == node["parameters"]["modelName"]
    else:
        assert preview["templateRendered"] is True
        assert preview["model"] == node["parameters"]["model"]
        assert preview["region"] == node["parameters"]["region"]

    assert [(w["code"], w.get("field")) for w in warnings] == \
        ref_warnings(prompt, system_prompt, max_tokens)
    for warning in warnings:
        assert warning["message"]
        if warning["code"] == REF_TRUNCATION_CODE:
            assert str(REF_MIN_SAFE_MAX_TOKENS) in warning["message"]
        else:
            assert "is_anomalous" in warning["message"]

    # -- 2. against the shared Invocation_Builder itself -----------------
    parameters = dict(node["parameters"])
    parameters["prompt_template" if node_id == LLM_NODE_ID else "prompt"] = \
        prompt
    parameters["system_prompt"] = system_prompt
    if max_tokens is not None:
        parameters["max_tokens"] = max_tokens
    if node_id == LLM_NODE_ID:
        invocation = module.build_llm_invocation(
            parameters, parameters["prompt_template"], b"\xff\xd8", b"\xff\xd8")
        assert preview["userMessage"] == invocation.prompt
        assert preview["systemText"] == invocation.system_prompt
        assert preview["maxTokens"] == invocation.generation["max_tokens"]
    else:
        invocation = module.build_bedrock_invocation(
            parameters, b"\xff\xd8", b"\xff\xd8")
        assert preview["userMessage"] == invocation.prompt
        assert preview["systemText"] == invocation.system_prompt
        assert preview["maxTokens"] == invocation.max_tokens

    # -- 3. through the route, which never blocks the edit ---------------
    session_id = world.sessions[node_id]
    status, body = world.create_candidate(session_id, prompt, system_prompt,
                                         max_tokens)
    assert status == 201, body
    candidate_id = body["candidate"]["candidateId"]
    status, shown = world.preview(session_id, candidate_id)
    assert status == 200, shown
    assert shown["userMessage"] == ref_user_message(prompt)
    assert shown["systemText"] == ref_system_text(system_prompt)
    assert [(w["code"], w.get("field")) for w in shown["warnings"]] == \
        ref_warnings(prompt, system_prompt, max_tokens)
    assert shown["candidateId"] == candidate_id
    assert shown["sessionId"] == session_id
    assert shown["nodeType"] == node["type"]
    # Editing the Candidate to the same parser-hostile Prompt_Set is not
    # blocked either (Requirements 5.4, 5.5: warn, never block).
    status, edited = world.call(
        "PUT", f"{ANOMALY}/sessions/{{id}}/candidates/{{cid}}",
        {"id": session_id, "cid": candidate_id},
        body={"name": "Edited", "prompt": prompt,
              "systemPrompt": system_prompt,
              **({"maxTokens": max_tokens} if max_tokens is not None
                 else {})})
    assert status == 200, edited


# ==========================================================================
# Supporting checks
# ==========================================================================

def test_documented_vocabulary(tuning):
    """The Verdict_Instruction, its separator and the safe-budget bound
    Property 18 is stated over are the shared module's, literally."""
    assert tuning.MIN_SAFE_MAX_TOKENS == REF_MIN_SAFE_MAX_TOKENS == 64
    from workflow_core.anomaly_invocation import (
        ANOMALY_INSTRUCTION_SEPARATOR, BEDROCK_JSON_INSTRUCTION,
        DEFAULT_MAX_TOKENS)
    assert BEDROCK_JSON_INSTRUCTION == VERDICT_INSTRUCTION
    assert ANOMALY_INSTRUCTION_SEPARATOR == INSTRUCTION_SEPARATOR
    assert DEFAULT_MAX_TOKENS == 256


def test_the_two_warnings_at_their_edges(world):
    """The bounds of Requirements 5.4 and 5.5 at the values the property's
    drawn data straddles."""
    module = world.module
    # 63 warns, 64 does not.
    assert [w["code"] for w in module.preview_warnings("plain", "", 63)] == \
        ["max_tokens_truncation"]
    assert module.preview_warnings("plain", "", 64) == []
    # A JSON answer format without the key warns, with it does not.
    assert [w["code"] for w in
            module.preview_warnings("Answer in JSON.", "", 256)] == \
        ["answer_schema_missing_is_anomalous"]
    assert module.preview_warnings(
        'Answer in JSON with is_anomalous.', "", 256) == []
    # The system prompt is checked too, and named.
    warnings = module.preview_warnings("plain", 'Reply {"ok": true}', 256)
    assert [(w["code"], w["field"]) for w in warnings] == [
        ("answer_schema_missing_is_anomalous", "systemPrompt")]
    # Both warnings can be earned at once.
    assert [w["code"] for w in
            module.preview_warnings("Answer in JSON.", "", 10)] == [
        "max_tokens_truncation", "answer_schema_missing_is_anomalous"]
