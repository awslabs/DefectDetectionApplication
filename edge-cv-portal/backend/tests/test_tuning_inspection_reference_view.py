"""The samples view shows a list-form sample's reference files (AC-36
backend, design §10.7).

A sample whose node sent a list of references records ``references``
instead of a ``reference`` block. Its view carries every usable entry in
recorded order — ``{index, label, kind, format, bytes, width, height}`` plus
a presigned ``url`` — with ``reference: None`` and ``singleImage: False``;
an entry that is not a usable reference is left out. Single-image and legacy
samples keep today's ``reference`` and ``singleImage`` and get no
``references`` key. With ``include_metadata`` an inspection sample's
``metadataSnippet`` is its decoded ``metadataSnippetJson``.

At c299a6e1 a list-form sample's view has no ``references`` key.
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
SNIPPET = {"trigger": {"topic": "plant/line1/evaluate",
                       "payload_json": {"order_id": "o-1",
                                        "body": {"scale": 2.0}},
                       "qos": 1}}


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
    """One Use_Case and workflow whose inspection node exported a
    list-form, a single-image and a legacy-shaped sample."""

    def __init__(self, stack, module):
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
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0},
                "parameters": {"model": "us.amazon.nova-pro-v1:0",
                               "answer_format": "inspection",
                               "prompt": "Check the parts."}}
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/1/"
               f"workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key, Body=json.dumps(
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
        self.bases = {}

    def put_sample(self, execution, exported_at, reference=None,
                   references=None, **extra):
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/{NODE_ID}/{THING}/"
                f"{execution}")
        self.bases[execution] = base
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        sidecar = {
            "schemaVersion": 1, "source": "live",
            "workflowId": self.workflow_id, "version": 1,
            "executionId": execution, "nodeId": NODE_ID,
            "nodeType": "bedrock_inference", "thingName": THING,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes), "width": 640,
                      "height": 480},
            "recorded": {"isAnomalous": False, "confidence": 0.9,
                         "verdict": "pass", "answer": "{}",
                         "parseError": None},
            "promptFingerprint": "sha256:recorded",
        }
        if reference is not None:
            sidecar["reference"] = dict(reference,
                                        key=base + ".reference.jpg")
        if references is not None:
            sidecar["references"] = [
                dict(entry, key=f"{base}.reference-{entry['index']}."
                     f"{'jpg' if entry['kind'] == 'image' else entry['format']}")
                if isinstance(entry, dict) and "index" in entry else entry
                for entry in references]
        sidecar.update(extra)
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=json.dumps(sidecar).encode("utf-8"))

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

    def views(self, query=None):
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": NODE_ID})
        assert status in (200, 201), body
        status, body = self.call(
            "GET", f"{ANOMALY}/sessions/{{id}}/samples",
            {"id": body["session"]["sessionId"]}, query=query)
        assert status == 200, body
        return {s["executionId"]: s for s in body["samples"]}


LIST_REFERENCES = [
    {"index": 1, "label": "Reference 1", "kind": "image", "format": "jpeg",
     "sha256": "1" * 64, "bytes": 48211, "width": 800, "height": 450},
    {"index": 2, "label": "Reference 2", "kind": "document",
     "format": "txt", "sha256": "2" * 64, "bytes": 12288},
    {"index": 3, "label": "Reference 3", "kind": "document",
     "format": "pdf", "sha256": "3" * 64, "bytes": 182004},
]


def test_a_list_form_sample_shows_every_reference_with_a_url(aws_stack,
                                                             tuning):
    env = Env(aws_stack, tuning)
    env.put_sample("list-form", 1000, references=LIST_REFERENCES)
    view = env.views()["list-form"]

    references = view["references"]
    assert [{key: entry.get(key) for key in (
        "index", "label", "kind", "format", "bytes", "width", "height")}
        for entry in references] == [
        {"index": 1, "label": "Reference 1", "kind": "image",
         "format": "jpeg", "bytes": 48211, "width": 800, "height": 450},
        {"index": 2, "label": "Reference 2", "kind": "document",
         "format": "txt", "bytes": 12288, "width": None, "height": None},
        {"index": 3, "label": "Reference 3", "kind": "document",
         "format": "pdf", "bytes": 182004, "width": None, "height": None},
    ]
    base = env.bases["list-form"]
    for entry, suffix in zip(references, (".reference-1.jpg",
                                          ".reference-2.txt",
                                          ".reference-3.pdf")):
        assert isinstance(entry["url"], str)
        assert (base + suffix).rsplit("/", 1)[-1] in entry["url"]
        assert "key" not in entry and "sha256" not in entry
    assert view["reference"] is None
    assert view["singleImage"] is False


def test_an_entry_that_is_not_a_usable_reference_is_left_out(aws_stack,
                                                            tuning):
    env = Env(aws_stack, tuning)
    env.put_sample("partly-malformed", 1000, references=[
        LIST_REFERENCES[0],
        {"index": 2, "label": "Reference 2", "kind": "document",
         "format": "exe", "bytes": 10},
        "not an entry",
        {"index": 4, "label": "Reference 4", "kind": "image",
         "format": "png", "bytes": 10},
    ])
    view = env.views()["partly-malformed"]
    assert [entry["index"] for entry in view["references"]] == [1]
    assert view["singleImage"] is False


def test_include_metadata_decodes_the_recorded_snippet_string(aws_stack,
                                                              tuning):
    """``metadataSnippet`` is ``json.loads(metadataSnippetJson)`` when that
    is an object — key order and the integral float ``2.0`` included."""
    env = Env(aws_stack, tuning)
    env.put_sample("with-snippet", 1000, metadataSnippetJson=json.dumps(
        SNIPPET, separators=(",", ":")))
    env.put_sample("snippet-not-an-object", 1001,
                   metadataSnippetJson="[1, 2]",
                   metadataSnippet={"trigger": {"kept": True}})
    views = env.views({"include_metadata": "true"})
    snippet = views["with-snippet"]["metadataSnippet"]
    assert snippet == SNIPPET
    assert list(snippet["trigger"]["payload_json"]) == ["order_id", "body"]
    assert isinstance(snippet["trigger"]["payload_json"]["body"]["scale"],
                      float)
    assert views["snippet-not-an-object"]["metadataSnippet"] == {
        "trigger": {"kept": True}}


def test_single_image_and_legacy_samples_are_unchanged(aws_stack, tuning):
    """Preservation: a single-image sample keeps its presigned ``reference``
    and ``singleImage: False``, a sample without a reference shows the
    single-image indication, and neither gets a ``references`` key. Passes
    at c299a6e1 too."""
    env = Env(aws_stack, tuning)
    env.put_sample("single", 1000, reference={
        "sha256": "4" * 64, "bytes": 3522, "width": 320, "height": 200})
    env.put_sample("no-reference", 1001)
    views = env.views()
    single = views["single"]
    assert "references" not in single
    assert single["singleImage"] is False
    assert single["reference"]["key"] == (
        env.bases["single"] + ".reference.jpg")
    assert "single.reference.jpg" in single["reference"]["url"]
    plain = views["no-reference"]
    assert "references" not in plain
    assert plain["reference"] is None and plain["singleImage"] is True
