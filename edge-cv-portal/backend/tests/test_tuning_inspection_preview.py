"""The Candidate preview and its warnings follow the inspection contract
(AC-7 backend, FR-5.1).

For an inspection-format node (``answer_format`` exactly ``inspection``):

- the previewed user message is the prompt, a blank line and the inspection
  instruction, shown unrendered; ``templateRendered`` is ``not
  render_prompt``, because with ``render_prompt`` on the device renders the
  template per run;
- the preview lists the request's ``attachments`` — one ``{label, kind,
  format}`` per block, input image first — so an author sees the labels the
  prompt must use;
- the schema warning names ``verdict`` (``answer_schema_missing_verdict``)
  instead of ``is_anomalous``.

Legacy previews and warnings are unchanged.

At c299a6e1 ``templateRendered`` is ``True``, a ``{"verdict": …}`` prompt
draws ``answer_schema_missing_is_anomalous``, and the preview has no
``attachments`` key.
"""
import json
import os
import sys
import uuid

import boto3
import pytest
from dynamo_helpers import all_table_names

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"
ANOMALY = "/workflow-tuning/anomaly"
NODE_ID = "check"

#: Restated locally (never imported): the two instructions and their
#: separator.
INSPECTION_INSTRUCTION = (
    'Respond with exactly one JSON object and nothing else: {"verdict": '
    '"pass"|"fail", "confidence": 0..1, "reason": "<one sentence>", '
    '"objects": [{"name": "<part>", "qc": "OK"|"NOK", "reason": "<short>", '
    '"bounding_box": {"x_min": <int>, "y_min": <int>, "x_max": <int>, '
    '"y_max": <int>}}, ...]} with one entry in objects per expected part.')
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')
SEPARATOR = "\n\n"


def verdict_warning(field):
    return {
        "code": "answer_schema_missing_verdict", "field": field,
        "message": (f'The {field} specifies an answer format that does not '
                    f'include "verdict". An inspection-format node\'s parser '
                    f'requires exactly one JSON object with verdict, '
                    f'confidence, reason and objects.')}


INSPECTION_PARAMETERS = {
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "answer_format": "inspection",
    "render_prompt": True,
    "box_scale": "pixels",
    "max_tokens": 600,
    "prompt": "Check order {trigger.payload_json.order_id}.",
    "system_prompt": "",
    "reference_payload_path": ("file:///aws_dda/reference/legend.jpg\n"
                               "file:///aws_dda/reference/notes.txt"),
    "allowed_uri_prefixes": "file:///aws_dda/reference/",
}
LEGACY_PARAMETERS = {
    "model": "us.amazon.nova-lite-v1:0",
    "region": "us-west-2",
    "anomaly_mode": True,
    "prompt": "Compare the plate to the reference.",
    "max_tokens": 256,
}
INPUT = {"label": "Input image", "kind": "image", "format": "jpeg"}


@pytest.fixture(scope="module")
def tuning(aws_stack):
    """The real handler module against moto, with the tuning table and the
    Use_Case's Sample_Store bucket in place."""
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    if TUNING_TABLE_NAME not in all_table_names(dynamodb):
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
    os.environ["WORKFLOW_TUNING_FUNCTION_NAME"] = "test-workflow-tuning-fn"
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning

    return workflow_tuning


class Env:
    """One Use_Case and workflow with a session on its node."""

    def __init__(self, stack, module, parameters):
        self.module = module
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        user_id = f"user-{uuid.uuid4()}"
        self.user = {"user_id": user_id, "email": f"{user_id}@example.com",
                     "username": user_id, "role": "DataScientist"}
        self.workflow_id = str(uuid.uuid4())
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0}, "parameters": parameters}
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/1/"
               f"workflow.json")
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=PORTAL_BUCKET, Key=key, Body=json.dumps(
                {"schemaVersion": "1.0", "nodes": [node],
                 "connections": []}).encode("utf-8"))
        stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Inspection workflow",
            "created_at": 1, "updated_at": 1, "latest_version": 1,
            "created_by": user_id})
        stack.tables.versions.put_item(Item={
            "workflow_id": self.workflow_id, "version": 1,
            "s3_definition_key": key, "created_at": 1,
            "created_by": user_id})
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": NODE_ID})
        assert status in (200, 201), body
        self.session_id = body["session"]["sessionId"]

    def call(self, method, resource, path_params=None, body=None,
             query=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": self.user["user_id"], "email": self.user["email"],
                "cognito:username": self.user["username"],
                "custom:role": self.user["role"]}}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"] or "{}")

    def preview(self, prompt, system_prompt="", max_tokens=600):
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/candidates",
            {"id": self.session_id},
            body={"name": f"Candidate {uuid.uuid4().hex[:6]}",
                  "prompt": prompt, "systemPrompt": system_prompt,
                  "maxTokens": max_tokens})
        assert status == 201, body
        status, preview = self.call(
            "GET", f"{ANOMALY}/candidates/{{cid}}/preview",
            {"cid": body["candidate"]["candidateId"]},
            query={"session_id": self.session_id})
        assert status == 200, preview
        return preview


def node(parameters):
    return {"id": NODE_ID, "type": "bedrock_inference",
            "parameters": dict(parameters)}


def prompt_set(prompt="Check the parts.", system_prompt="", max_tokens=600):
    return {"prompt": prompt, "systemPrompt": system_prompt,
            "maxTokens": max_tokens}


def test_the_preview_is_the_unrendered_prompt_and_lists_the_attachments(
        aws_stack, tuning):
    """Through the route: the prompt and the inspection instruction,
    unrendered, ``templateRendered`` false with ``render_prompt`` on, the
    attachments in request order, and no warning for a prompt that demands
    the verdict JSON."""
    env = Env(aws_stack, tuning, INSPECTION_PARAMETERS)
    prompt = ('Check order {trigger.payload_json.order_id}; answer with '
              'the {"verdict": ...} JSON.')
    preview = env.preview(prompt)

    assert preview["userMessage"] == (
        prompt + SEPARATOR + INSPECTION_INSTRUCTION)
    assert preview["templateRendered"] is False
    assert preview["attachments"] == [
        INPUT,
        {"label": "Reference 1", "kind": "image", "format": "jpeg"},
        {"label": "Reference 2", "kind": "document", "format": "txt"},
    ]
    assert preview["warnings"] == []
    assert preview["systemText"] is None
    assert (preview["model"], preview["region"], preview["maxTokens"]) == (
        "us.amazon.nova-pro-v1:0", "us-west-2", 600)


def test_a_json_answer_format_without_verdict_draws_the_verdict_warning(
        aws_stack, tuning):
    """A prompt or system prompt that demands JSON without naming
    ``verdict`` draws ``answer_schema_missing_verdict`` for its field, and
    never the ``is_anomalous`` warning."""
    env = Env(aws_stack, tuning, INSPECTION_PARAMETERS)
    assert env.preview("Answer in JSON.")["warnings"] == [
        verdict_warning("prompt")]
    assert env.preview("Check the parts.",
                       system_prompt='Reply {"ok": true}')["warnings"] == [
        verdict_warning("systemPrompt")]
    codes = [w["code"] for w in env.preview("Answer in JSON.",
                                            max_tokens=10)["warnings"]]
    assert codes == ["max_tokens_truncation", "answer_schema_missing_verdict"]


@pytest.mark.parametrize("references, expected", [
    ("", [INPUT]),
    ("file:///aws_dda/reference/legend.jpg",
     [INPUT, {"label": "Reference image", "kind": "image",
              "format": "jpeg"}]),
    ("refs.0.image",
     [INPUT, {"label": "Reference image", "kind": "image",
              "format": "jpeg"}]),
    ("file:///aws_dda/reference/sheet.pdf",
     [INPUT, {"label": "Reference 1", "kind": "document", "format": "pdf"}]),
    ("upload://" + "c" * 64 + ".png\nfile:///aws_dda/reference/a.xlsx\n"
     "refs.0.image",
     [INPUT, {"label": "Reference 1", "kind": "image", "format": "jpeg"},
      {"label": "Reference 2", "kind": "document", "format": "xlsx"},
      {"label": "Reference 3", "kind": "image", "format": "jpeg"}]),
])
def test_attachments_follow_the_request_order(tuning, references, expected):
    """One entry per block, input image first, labelled as the request
    labels it: a single image is "Reference image", any other list
    "Reference 1" onwards."""
    parameters = dict(INSPECTION_PARAMETERS, reference_payload_path=references)
    preview = tuning.build_preview(node(parameters), prompt_set())
    assert preview["attachments"] == expected
    assert preview["userMessage"] == (
        "Check the parts." + SEPARATOR + INSPECTION_INSTRUCTION)


@pytest.mark.parametrize("render_prompt, rendered", [
    (True, False), ("true", False), (1, False),
    (False, True), ("false", True), ("", True), (None, True)])
def test_template_rendered_is_not_render_prompt(tuning, render_prompt,
                                                rendered):
    """With ``render_prompt`` on, the device renders the template per run,
    so the preview reports it unrendered; off, the template is what is
    sent. An absent ``render_prompt`` is off."""
    parameters = dict(INSPECTION_PARAMETERS)
    if render_prompt is None:
        parameters.pop("render_prompt")
    else:
        parameters["render_prompt"] = render_prompt
    preview = tuning.build_preview(node(parameters), prompt_set())
    assert preview["templateRendered"] is rendered
    assert preview["attachments"][0] == INPUT


def test_preview_warnings_take_the_answer_format(tuning):
    """``preview_warnings(prompt, system_prompt, max_tokens,
    answer_format)``: only the exact string ``inspection`` selects the
    inspection wording."""
    assert tuning.preview_warnings("Answer in JSON.", "", 256,
                                   "inspection") == [
        verdict_warning("prompt")]
    assert tuning.preview_warnings(
        'Answer {"verdict": "pass"} in JSON.', "", 256, "inspection") == []
    for answer_format in (None, "legacy", "Inspection", " inspection"):
        assert [w["code"] for w in tuning.preview_warnings(
            "Answer in JSON.", "", 256, answer_format)] == [
            "answer_schema_missing_is_anomalous"]


def test_legacy_previews_and_warnings_are_unchanged(aws_stack, tuning):
    """Preservation: a legacy anomaly-mode node's preview keeps today's
    keys and values, with the Verdict_Instruction and no attachments, and
    the three-argument warnings are today's. Passes at c299a6e1 too."""
    for answer_format in (None, "legacy", "Inspection"):
        parameters = dict(LEGACY_PARAMETERS)
        if answer_format is not None:
            parameters["answer_format"] = answer_format
        preview = tuning.build_preview(node(parameters),
                                       prompt_set("Compare them."))
        assert preview == {
            "userMessage": "Compare them." + SEPARATOR + VERDICT_INSTRUCTION,
            "systemText": None, "maxTokens": 600,
            "model": "us.amazon.nova-lite-v1:0", "region": "us-west-2",
            "templateRendered": True}
    assert [w["code"] for w in tuning.preview_warnings(
        "Answer in JSON.", "", 256)] == ["answer_schema_missing_is_anomalous"]
    assert tuning.preview_warnings("Answer in JSON with is_anomalous.", "",
                                   256) == []

    env = Env(aws_stack, tuning, LEGACY_PARAMETERS)
    preview = env.preview("Answer in JSON.")
    assert "attachments" not in preview
    assert preview["templateRendered"] is True
    assert [w["code"] for w in preview["warnings"]] == [
        "answer_schema_missing_is_anomalous"]
