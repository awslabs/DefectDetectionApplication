"""Synthetic negatives are refused for inspection-format nodes (AC-17
backend, design §10.5).

A Synthetic_Negative pairs one node's input image with a sibling node's
reference image of the same execution and labels it NOK. An
inspection-format node's reference image (for example a legend of part
shapes) is not a per-part golden sample, so such a pair is not a defect.
Enabling them on an inspection-format session answers ``400
SYNTHETIC_NEGATIVES_UNSUPPORTED`` and creates nothing; disabling, and
legacy sessions, are unchanged.

At c299a6e1 the enable answers 200.
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
THING = "dev-1"
UNSUPPORTED_MESSAGE = (
    "Synthetic negatives need a per-part golden reference image; an "
    "inspection-format node's reference image (for example a legend) is not "
    "one, so cross-paired images are not defects")

INSPECTION_NODE = {"id": "check", "type": "bedrock_inference",
                   "position": {"x": 0, "y": 0},
                   "parameters": {"model": "us.amazon.nova-pro-v1:0",
                                  "answer_format": "inspection",
                                  "prompt": "Check the parts."}}
LEGACY_NODE = {"id": "check", "type": "bedrock_inference",
               "position": {"x": 0, "y": 0},
               "parameters": {"model": "us.amazon.nova-lite-v1:0",
                              "anomaly_mode": True,
                              "prompt": "Compare the plate."}}
SIBLING_NODE = {"id": "sibling", "type": "bedrock_inference",
                "position": {"x": 0, "y": 100},
                "parameters": {"model": "us.amazon.nova-lite-v1:0",
                               "anomaly_mode": True,
                               "prompt": "Compare the lid."}}


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
    """A workflow whose ``check`` node has one OK-labelled sample, and
    whose sibling node exported a reference image for the same execution
    on the same device: exactly the pair a Synthetic_Negative would be."""

    def __init__(self, stack, module, check_node):
        self.module = module
        s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        user_id = f"user-{uuid.uuid4()}"
        self.user = {"user_id": user_id, "email": f"{user_id}@example.com",
                     "username": user_id, "role": "DataScientist"}
        self.workflow_id = str(uuid.uuid4())
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/1/"
               f"workflow.json")
        s3.put_object(Bucket=PORTAL_BUCKET, Key=key, Body=json.dumps(
            {"schemaVersion": "1.0", "nodes": [check_node, SIBLING_NODE],
             "connections": []}).encode("utf-8"))
        stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Two-node workflow",
            "created_at": 1, "updated_at": 1, "latest_version": 1,
            "created_by": user_id})
        stack.tables.versions.put_item(Item={
            "workflow_id": self.workflow_id, "version": 1,
            "s3_definition_key": key, "created_at": 1,
            "created_by": user_id})
        prefix = f"{SAMPLES_PREFIX}{self.workflow_id}/"
        base = f"{prefix}check/{THING}/exec-1"
        input_bytes = b"INPUT-exec-1"
        s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                      Body=input_bytes)
        s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                      Body=json.dumps({
                          "schemaVersion": 1, "source": "live",
                          "workflowId": self.workflow_id, "version": 1,
                          "executionId": "exec-1", "nodeId": "check",
                          "nodeType": "bedrock_inference",
                          "thingName": THING, "exportedAt": 1000,
                          "input": {"key": base + ".input.jpg",
                                    "sha256": hashlib.sha256(
                                        input_bytes).hexdigest(),
                                    "bytes": len(input_bytes)},
                          "recorded": {"isAnomalous": False,
                                       "confidence": 0.9, "answer": "{}",
                                       "parseError": None},
                          "promptFingerprint": "sha256:recorded",
                      }).encode("utf-8"))
        s3.put_object(Bucket=SAMPLE_BUCKET,
                      Key=f"{prefix}sibling/{THING}/exec-1.reference.jpg",
                      Body=b"SIBLING-REFERENCE")
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": "check"})
        assert status in (200, 201), body
        self.session_id = body["session"]["sessionId"]
        status, body = self.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
            {"id": self.session_id},
            body={"sampleIds": [f"{THING}/exec-1"], "label": "OK"})
        assert status == 200 and body["missing"] == [], body

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

    def toggle(self, enabled):
        return self.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/synthetic-negatives",
            {"id": self.session_id}, body={"enabled": enabled})

    def synthetic_flag(self):
        status, body = self.call("GET", f"{ANOMALY}/sessions/{{id}}",
                                 {"id": self.session_id})
        assert status == 200, body
        return (body["session"]["syntheticNegativesEnabled"],
                body["labelCounts"]["synthetic"])


def test_enabling_on_an_inspection_format_session_is_refused(aws_stack,
                                                             tuning):
    """400 ``SYNTHETIC_NEGATIVES_UNSUPPORTED`` with the reason; the flag
    stays off and no Synthetic_Negative exists."""
    env = Env(aws_stack, tuning, INSPECTION_NODE)
    status, body = env.toggle(True)
    assert status == 400, body
    assert body["error"]["code"] == "SYNTHETIC_NEGATIVES_UNSUPPORTED"
    assert body["error"]["message"] == UNSUPPORTED_MESSAGE
    assert env.synthetic_flag() == (False, 0)


def test_disabling_on_an_inspection_format_session_is_unchanged(aws_stack,
                                                                tuning):
    """Preservation: disabling answers 200 as today, whatever the node's
    answer format. Passes at c299a6e1 too."""
    env = Env(aws_stack, tuning, INSPECTION_NODE)
    status, body = env.toggle(False)
    assert status == 200, body
    assert (body["enabled"], body["created"], body["removed"]) == (
        False, 0, 0)


def test_a_legacy_session_still_creates_synthetic_negatives(aws_stack,
                                                            tuning):
    """Preservation: an anomaly-mode node's session pairs the OK sample
    with the sibling's reference, as today. Passes at c299a6e1 too."""
    env = Env(aws_stack, tuning, LEGACY_NODE)
    status, body = env.toggle(True)
    assert status == 200, body
    assert (body["enabled"], body["created"]) == (True, 1)
    assert env.synthetic_flag() == (True, 1)
