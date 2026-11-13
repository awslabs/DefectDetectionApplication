"""Portal half of the inspection replay parity golden (AC-6, AC-6a, AC-35).

The device half (``test/backend-test/workflow_engine/
test_inspection_replay_parity_device.py``) runs the real executor with its
default invoker and a real sample exporter, and commits what the device sent
to Bedrock and what it uploaded as ``tests/fixtures/
inspection_replay_parity.json``. This half puts the golden's exact sidecar
bodies and the committed input, legend, notes and sheet files into moto S3
at the golden's keys (checking each file's digest against the golden),
indexes them through the real session route, labels the sample OK, scores
the Baseline_Candidate with one repeat against a recording Bedrock stub, and
compares the region and the Converse keyword arguments with the device's,
under the golden's substitution of every image and document block's bytes by
``{"sha256", "length"}``.

``single_legend`` is A's single legend. ``mixed_references`` is a list of one
image and two documents ("Reference 1" to "Reference 3"). The trigger payload
has order items whose keys are not sorted and the integral float ``2.0``,
which the prompt renders: after the sorted-keys upload and the DynamoDB round
trip, the Portal must still render ``[{"shape":…,"color":…}]`` and ``2.0``.

Each lane fails when its own side drifts: this one when the replay's request
changes. At c299a6e1 the replay sends the unrendered template and at most
two images, so ``assert kwargs == case["converseKwargs"]`` fails.
"""
import hashlib
import json
import os
import sys
import uuid

import boto3
import pytest
from dynamo_helpers import all_table_names

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(_HERE, "fixtures")
STEM = "inspection_replay_parity"
GOLDEN_PATH = os.path.join(FIXTURES, STEM + ".json")
INPUT_FILES = (STEM + ".input.jpg", STEM + ".legend.jpg",
               STEM + ".notes.txt", STEM + ".sheet.pdf")

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"
ANOMALY = "/workflow-tuning/anomaly"
WORKFLOW_ID = "wf-parity"
NODE_ID = "check"
SUBSTITUTED_ROOT = "file:///fixture/"

#: The device half's node, restated (never imported across lanes). Its two
#: reference parameters name the substituted sources; the scorer reads only
#: how many references the list holds.
NODE_PARAMETERS = {
    "answer_format": "inspection",
    "render_prompt": True,
    "box_scale": "pixels",
    "max_image_dimension": 1280,
    "max_tokens": 600,
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "prompt": ("Order {trigger.payload_json.order_id}: the parts "
               "{json:trigger.payload_json.body.order.items} at scale "
               "{trigger.payload_json.body.scale}, on an image "
               "{image.width} px wide."),
    "system_prompt": "You inspect stage {trigger.payload_json.body.qc_stage}.",
    "expected_count_path": "trigger.payload_json.body.order.items",
    "allowed_uri_prefixes": SUBSTITUTED_ROOT,
}
#: The reference files each case's node lists, in order.
CASE_REFERENCES = {
    "single_legend": (STEM + ".legend.jpg",),
    "mixed_references": (STEM + ".legend.jpg", STEM + ".notes.txt",
                         STEM + ".sheet.pdf"),
}
#: The device half's stubbed answer: a pass naming both ordered parts.
ANSWER = json.dumps({
    "verdict": "pass", "confidence": 0.91, "reason": "both parts match",
    "objects": [
        {"name": "roof", "qc": "OK", "reason": "red, left",
         "bounding_box": {"x_min": 60, "y_min": 80, "x_max": 260,
                          "y_max": 220}},
        {"name": "panel", "qc": "OK", "reason": "blue, right",
         "bounding_box": {"x_min": 340, "y_min": 200, "x_max": 580,
                          "y_max": 400}},
    ],
})


def digest(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "length": len(data)}


def substitute_bytes(value):
    """``value`` with every ``source.bytes`` replaced by its digest, the
    golden's substitution."""
    if isinstance(value, dict):
        return {key: (digest(item) if key == "bytes"
                      and isinstance(item, (bytes, bytearray))
                      else substitute_bytes(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_bytes(item) for item in value]
    return value


class RecordingBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"role": "assistant",
                                       "content": [{"text": ANSWER}]}}}


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
    """The golden's workflow in one Use_Case, with the module's dispatch
    and Bedrock seams replaced by recording doubles."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Parity use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        user_id = f"user-{uuid.uuid4()}"
        self.user = {"user_id": user_id, "email": f"{user_id}@example.com",
                     "username": user_id, "role": "DataScientist"}
        self.dispatched = []
        self.bedrock = RecordingBedrock()
        self.regions = []
        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client", self._client)

    def _client(self, region):
        self.regions.append(region)
        return self.bedrock

    def put_version(self, references, version):
        parameters = dict(NODE_PARAMETERS)
        parameters["reference_payload_path"] = "\n".join(
            SUBSTITUTED_ROOT + name for name in references)
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0}, "parameters": parameters}
        document = {"schemaVersion": "1.0", "nodes": [node],
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{WORKFLOW_ID}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": WORKFLOW_ID, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Parity workflow",
            "created_at": 1, "updated_at": version,
            "latest_version": version, "created_by": self.user["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": WORKFLOW_ID, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.user["user_id"]})

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

    def score_baseline(self, session_id):
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/score-runs",
            {"id": session_id}, body={"candidateId": "baseline",
                                      "repeats": 1})
        assert status == 202, body
        while self.dispatched:
            self.module.handler(self.dispatched.pop(0), None)
        status, view = self.call(
            "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes",
            {"rid": body["runId"]})
        assert status == 200, view
        return view


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


def load_golden():
    with open(GOLDEN_PATH, "r", encoding="utf-8") as handle:
        golden = json.load(handle)
    files = {}
    for name in INPUT_FILES:
        with open(os.path.join(FIXTURES, name), "rb") as handle:
            data = handle.read()
        files[hashlib.sha256(data).hexdigest()] = data
    return golden, files


def put_case_objects(env, case, files):
    """The case's uploaded objects (each from the committed file whose
    digest the golden records) and its sidecar body; returns the sample
    id."""
    sidecar_key = None
    for key, expected in case["objects"].items():
        data = files.get(expected["sha256"])
        assert data is not None, (
            "no committed input file has the digest the golden records for "
            "{0}".format(key))
        assert digest(data) == expected
        env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key, Body=data)
        if key.endswith(".input.jpg"):
            sidecar_key = key[:-len(".input.jpg")] + ".json"
    assert sidecar_key is not None
    env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=sidecar_key,
                      Body=case["sidecarBody"].encode("utf-8"))
    sidecar = json.loads(case["sidecarBody"])
    return "{0}/{1}".format(sidecar["thingName"], sidecar["executionId"])


def test_the_portal_replay_sends_the_devices_request(env):
    golden, files = load_golden()
    cases = {case["name"]: case for case in golden["cases"]}
    assert sorted(cases) == sorted(CASE_REFERENCES)

    # single_legend: the session's first index finds the sample.
    case = cases["single_legend"]
    env.put_version(CASE_REFERENCES["single_legend"], version=1)
    single_id = put_case_objects(env, case, files)
    status, body = env.call("POST", f"{ANOMALY}/sessions",
                            body={"workflow_id": WORKFLOW_ID,
                                  "node_id": NODE_ID})
    assert status in (200, 201), body
    session_id = body["session"]["sessionId"]
    status, body = env.call(
        "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
        {"id": session_id}, body={"sampleIds": [single_id], "label": "OK"})
    assert status == 200 and body["missing"] == [], body
    view = env.score_baseline(session_id)

    (kwargs,) = env.bedrock.calls
    assert env.regions[-1] == case["region"]
    assert substitute_bytes(kwargs) == case["converseKwargs"]
    (outcome,) = view["outcomes"]
    assert outcome["category"] == "correct"
    assert outcome["imageCount"] == 2

    # mixed_references: the node now lists three references; the refresh
    # indexes the second sample.
    case = cases["mixed_references"]
    env.put_version(CASE_REFERENCES["mixed_references"], version=2)
    mixed_id = put_case_objects(env, case, files)
    status, body = env.call("POST", f"{ANOMALY}/sessions/{{id}}/refresh",
                            {"id": session_id})
    assert status == 200 and body["refresh"]["indexed"] == 1, body
    for sample_id, label in ((mixed_id, "OK"), (single_id, "EXCLUDE")):
        status, body = env.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
            {"id": session_id}, body={"sampleIds": [sample_id],
                                      "label": label})
        assert status == 200 and body["missing"] == [], body
    env.bedrock.calls.clear()
    view = env.score_baseline(session_id)

    (kwargs,) = env.bedrock.calls
    assert env.regions[-1] == case["region"]
    assert substitute_bytes(kwargs) == case["converseKwargs"]
    (outcome,) = view["outcomes"]
    assert outcome["category"] == "correct"
    assert (outcome["imageCount"], outcome["documentCount"]) == (2, 2)
