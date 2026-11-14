"""The Bedrock_Scorer sends the node's ``additional_model_request_fields``
on inspection-format and legacy replays: the Portal half of the integration
of the two features (design section 25).

The real ``functions/workflow_tuning.py`` handler against moto, with the
module's dispatch seam driven inline and a Bedrock stub that records every
Converse request; samples are written to the Sample_Store the way the device
writes them and indexed through the session route.

- An inspection-format node with three reference files: after the latest
  version sets the fields, the replay of the same sample sends the same
  request plus ``additionalModelRequestFields`` (the replay takes the
  fields from the latest version, like the model and the region).
- A legacy anomaly-mode node sends them too.

The node's ``anomaly_mode`` is absent or on, so both nodes are tunable at
c299a6e1 and the tests reach their request assertions there: that replay
sends at most two images and no request fields.
"""
import hashlib
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
SAMPLES_PREFIX = "workflow-tuning/samples/"
ANOMALY = "/workflow-tuning/anomaly"
NODE_ID = "check"
THING = "dev-1"
FIELDS = "additional_model_request_fields"
TOP_K = '{"inferenceConfig": {"topK": 20}}'
TOP_K_OBJECT = {"inferenceConfig": {"topK": 20}}
REFERENCE_LIST = "\n".join(["file:///aws_dda/reference/legend.jpg",
                            "file:///aws_dda/reference/notes.txt",
                            "file:///aws_dda/reference/sheet.pdf"])
INSPECTION_NODE = {
    "model": "us.amazon.nova-pro-v1:0", "region": "us-west-2",
    "answer_format": "inspection", "box_scale": "pixels", "max_tokens": 600,
    "prompt": "Check the parts.", "reference_payload_path": REFERENCE_LIST,
    "allowed_uri_prefixes": "file:///aws_dda/reference/",
}
LEGACY_NODE = {
    "model": "us.amazon.nova-pro-v1:0", "region": "us-west-2",
    "anomaly_mode": True, "max_tokens": 300, "prompt": "Compare the parts.",
}
INSPECTION_PASS = json.dumps({
    "verdict": "pass", "confidence": 0.9, "reason": "checked",
    "objects": [{"name": "roof", "qc": "OK", "reason": "seen",
                 "bounding_box": {"x_min": 10, "y_min": 10, "x_max": 90,
                                  "y_max": 90}}]})
LEGACY_OK = '{"is_anomalous": false, "confidence": 0.9}'
LIST_BLOCKS = ["text", "text", "image", "text", "image", "text", "document",
               "text", "document"]


class RecordingBedrock:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.answer}]}},
                "usage": {"outputTokens": 42}}


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
    """One Use_Case, workflow and Tuning_Session over moto, with the
    module's dispatch and Bedrock seams replaced by recording doubles."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        user_id = f"user-{uuid.uuid4()}"
        self.user = {"user_id": user_id, "email": f"{user_id}@example.com",
                     "username": user_id, "role": "DataScientist"}
        self.workflow_id = str(uuid.uuid4())
        self.session_id = None
        self.dispatched = []
        self.bedrock = RecordingBedrock(INSPECTION_PASS)
        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client",
                            lambda region: self.bedrock)

    def put_workflow(self, parameters, version=1):
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0}, "parameters": dict(parameters)}
        document = {"schemaVersion": "1.0", "nodes": [node],
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Request fields workflow",
            "created_at": 1, "updated_at": version,
            "latest_version": version, "created_by": self.user["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": self.workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.user["user_id"]})

    def put_sample(self, execution, references=(), inspection=True):
        """One Tuning_Sample as the device writes it: ``references`` is the
        list form as ``(kind, format)`` pairs."""
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/{NODE_ID}/{THING}/"
                f"{execution}")
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        sidecar = {
            "schemaVersion": 1, "source": "live",
            "workflowId": self.workflow_id, "version": 1,
            "executionId": execution, "nodeId": NODE_ID,
            "nodeType": "bedrock_inference", "thingName": THING,
            "exportedAt": 1001,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes)},
            "recorded": {"isAnomalous": False, "confidence": 0.9,
                         "answer": INSPECTION_PASS if inspection
                         else LEGACY_OK, "parseError": None},
            "promptFingerprint": "sha256:recorded",
            "detectionId": None, "detectionSlot": None,
        }
        if inspection:
            sidecar["answerFormat"] = "inspection"
            sidecar["recorded"]["verdict"] = "pass"
            sidecar["input"].update(width=640, height=480)
            sidecar["metadataSnippetJson"] = json.dumps(
                {"trigger": {"topic": "plant/line1/evaluate",
                             "payload_json": {"order_id": "o-1"}}},
                separators=(",", ":"))
        entries = []
        for index, (kind, fmt) in enumerate(references, start=1):
            data = f"REF{index}-{execution}".encode()
            extension = "jpg" if kind == "image" else fmt
            key = f"{base}.reference-{index}.{extension}"
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key, Body=data)
            entry = {"index": index, "label": f"Reference {index}",
                     "kind": kind, "format": fmt, "key": key,
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "bytes": len(data)}
            if kind == "image":
                entry.update(width=320, height=200)
            entries.append(entry)
        if entries:
            sidecar["references"] = entries
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=json.dumps(sidecar).encode("utf-8"))
        return f"{THING}/{execution}"

    def call(self, method, resource, path_params=None, body=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {
                "sub": self.user["user_id"], "email": self.user["email"],
                "cognito:username": self.user["username"],
                "custom:role": self.user["role"]}}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"] or "{}")

    def open_session(self, sample_id):
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": NODE_ID})
        assert status in (200, 201), body
        self.session_id = body["session"]["sessionId"]
        status, body = self.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
            {"id": self.session_id},
            body={"sampleIds": [sample_id], "label": "OK"})
        assert status == 200 and body["missing"] == [], body

    def score_baseline(self):
        """One Baseline_Candidate Score_Run to its end: ``(the Converse
        kwargs it sent, its outcomes)``."""
        self.bedrock.calls.clear()
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/score-runs",
            {"id": self.session_id},
            body={"candidateId": "baseline", "repeats": 1})
        assert status == 202, body
        while self.dispatched:
            self.module.handler(self.dispatched.pop(0), None)
        status, view = self.call(
            "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes",
            {"rid": body["runId"]})
        assert status == 200, view
        assert view["run"]["status"] == "completed", view["run"]
        (kwargs,) = self.bedrock.calls
        return kwargs, view["outcomes"]


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


def block_kinds(kwargs):
    return [next(iter(block)) for block in kwargs["messages"][0]["content"]]


def test_an_inspection_list_replay_adds_the_latest_versions_request_fields(env):
    """The same three-reference sample, scored under a version without and
    then with the fields: the second request is the first plus
    additionalModelRequestFields, documents included."""
    env.put_workflow(INSPECTION_NODE, version=1)
    sample_id = env.put_sample("s-1", references=(
        ("image", "jpeg"), ("document", "txt"), ("document", "pdf")))
    env.open_session(sample_id)
    without, _outcomes = env.score_baseline()

    env.put_workflow(dict(INSPECTION_NODE, **{FIELDS: TOP_K}), version=2)
    status, body = env.call("POST", f"{ANOMALY}/sessions/{{id}}/refresh",
                            {"id": env.session_id})
    assert status == 200, body
    with_fields, outcomes = env.score_baseline()

    assert block_kinds(with_fields) == LIST_BLOCKS
    assert with_fields["additionalModelRequestFields"] == TOP_K_OBJECT
    assert "additionalModelRequestFields" not in without
    assert {key: value for key, value in with_fields.items()
            if key != "additionalModelRequestFields"} == without
    (outcome,) = outcomes
    assert outcome["category"] == "correct"
    assert (outcome["imageCount"], outcome["documentCount"]) == (2, 2)


def test_a_legacy_replay_sends_the_request_fields_too(env):
    """An anomaly-mode node's replay carries the fields beside the two
    legacy content blocks; the verdict is read by the legacy parser."""
    env.bedrock.answer = LEGACY_OK
    env.put_workflow(dict(LEGACY_NODE, **{FIELDS: TOP_K}))
    sample_id = env.put_sample("s-legacy", inspection=False)
    env.open_session(sample_id)
    kwargs, outcomes = env.score_baseline()

    assert kwargs["additionalModelRequestFields"] == TOP_K_OBJECT
    assert block_kinds(kwargs) == ["text", "text", "image"]
    (outcome,) = outcomes
    assert outcome["category"] == "correct"
