"""Verdicts of already-backfilled inspection samples are recovered on read
(AC-9 backend, design §10.6).

An older LocalServer's backfill recorded every inspection-format sample with
``recorded.isAnomalous: null``, no parse error and the device's accepted
answer. The Portal recovers the verdict from that answer when it reads the
sample, under the inspection rule, without rewriting anything:

- ``fail`` is always recovered: under ``error_verdict: fail`` a rejected
  answer was published as ``fail`` anyway;
- ``pass`` is recovered only when the session's ``errorVerdict`` (the
  node's ``error_verdict``, ``error`` by default) is known and is not
  ``fail``, because under ``fail`` the old backfill also exported answers
  the device had rejected;
- a sidecar with a parse error, or whose answer is not a schema-valid
  inspection answer, keeps its null verdict;
- the samples view adds ``recorded.verdict`` and ``recorded.recovered``
  only when a verdict exists, and the ``verdict`` and ``disagree`` filters
  use the effective values.

At c299a6e1 the samples view returns ``recorded.isAnomalous: None`` for
these samples and the anomalous-verdict filter returns nothing.
"""
import hashlib
import json
import logging
import os
import sys
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
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


def inspection_answer(verdict, confidence):
    return json.dumps({
        "verdict": verdict, "confidence": confidence, "reason": "checked",
        "objects": [{"name": "roof",
                     "qc": "OK" if verdict == "pass" else "NOK",
                     "reason": "seen",
                     "bounding_box": {"x_min": 1, "y_min": 1, "x_max": 9,
                                      "y_max": 9}}]})


#: The backfilled samples: (execution, answer, parse error).
PASS_ANSWER = inspection_answer("pass", 0.93)
FAIL_ANSWER = inspection_answer("fail", 0.88)
LEGACY_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'
BACKFILLED = {
    "bf-pass": (PASS_ANSWER, None),
    "bf-fail": (FAIL_ANSWER, None),
    "bf-parse-error": (FAIL_ANSWER, "schema: answer.objects is missing"),
    "bf-legacy-answer": (LEGACY_ANSWER, None),
}


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
    """One Use_Case and workflow whose inspection node already has
    backfilled samples in the Sample_Store."""

    def __init__(self, stack, module, error_verdict=None):
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
        parameters = {"model": "us.amazon.nova-pro-v1:0",
                      "answer_format": "inspection",
                      "prompt": "Check the parts."}
        if error_verdict is not None:
            parameters["error_verdict"] = error_verdict
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0}, "parameters": parameters}
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
        for offset, (execution, (answer, parse_error)) in enumerate(
                sorted(BACKFILLED.items())):
            self.put_backfilled(execution, answer, parse_error,
                                1000 + offset)
        self.session_id = self.open_session()

    def put_backfilled(self, execution, answer, parse_error, exported_at):
        """A sidecar with the shape of the owner's backfilled samples."""
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/{NODE_ID}/{THING}/"
                f"{execution}")
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        sidecar = {
            "schemaVersion": 1, "source": "backfill",
            "workflowId": self.workflow_id, "version": 1,
            "executionId": execution, "nodeId": NODE_ID,
            "nodeType": "bedrock_inference", "thingName": THING,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes)},
            "recorded": {"isAnomalous": None, "confidence": None,
                         "answer": answer, "parseError": parse_error},
            "promptFingerprint": "sha256:recorded",
            "detectionId": None, "detectionSlot": None,
        }
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

    def open_session(self):
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": self.workflow_id,
                                       "node_id": NODE_ID})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def recorded(self, query=None):
        """``{execution: recorded}`` of the samples view."""
        status, body = self.call(
            "GET", f"{ANOMALY}/sessions/{{id}}/samples",
            {"id": self.session_id}, query=query)
        assert status == 200, body
        return {s["executionId"]: s["recorded"] for s in body["samples"]}

    def label(self, execution, label):
        status, body = self.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
            {"id": self.session_id},
            body={"sampleIds": [f"{THING}/{execution}"], "label": label})
        assert status == 200 and body["missing"] == [], body

    def table(self):
        return boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)

    def items(self):
        return self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(
                    f"SESSION#{self.session_id}"))).get("Items", [])

    def samples_items(self):
        return sorted((i for i in self.items()
                       if str(i["sk"]).startswith("SAMPLE#")),
                      key=lambda i: i["sk"])


def recovered(verdict, confidence):
    answer = PASS_ANSWER if verdict == "pass" else FAIL_ANSWER
    return {"isAnomalous": verdict == "fail", "confidence": confidence,
            "answer": answer, "parseError": None, "verdict": verdict,
            "recovered": True}


NOT_RECOVERED_PASS = {"isAnomalous": None, "confidence": None,
                      "answer": PASS_ANSWER, "parseError": None}


def test_with_error_verdict_error_pass_and_fail_are_recovered(aws_stack,
                                                              tuning):
    """``errorVerdict`` is ``error`` (the default): both verdicts are
    recovered, with the answer's confidence and the recovered mark; a
    parse error or a non-inspection answer stays null; no item is
    rewritten."""
    env = Env(aws_stack, tuning)
    before = env.samples_items()
    views = env.recorded()

    assert views["bf-pass"] == recovered("pass", 0.93)
    assert views["bf-fail"] == recovered("fail", 0.88)
    assert views["bf-parse-error"] == {
        "isAnomalous": None, "confidence": None, "answer": FAIL_ANSWER,
        "parseError": "schema: answer.objects is missing"}
    assert views["bf-legacy-answer"] == {
        "isAnomalous": None, "confidence": None, "answer": LEGACY_ANSWER,
        "parseError": None}
    assert env.samples_items() == before
    for item in before:
        assert item["recordedIsAnomalous"] is None


def test_the_verdict_and_disagree_filters_use_the_recovered_values(
        aws_stack, tuning):
    """The anomalous filter finds the recovered ``fail``, the normal filter
    the recovered ``pass`` and the samples that stay null, and the
    disagreement filter compares the Labels with the recovered values."""
    env = Env(aws_stack, tuning)
    assert set(env.recorded({"verdict": "anomalous"})) == {"bf-fail"}
    assert set(env.recorded({"verdict": "normal"})) == {
        "bf-pass", "bf-parse-error", "bf-legacy-answer"}

    env.label("bf-pass", "NOK")
    env.label("bf-fail", "OK")
    env.label("bf-legacy-answer", "OK")
    assert set(env.recorded({"disagree": "true"})) == {"bf-pass", "bf-fail"}


def test_with_error_verdict_fail_only_fail_is_recovered(aws_stack, tuning):
    """Under ``error_verdict: fail`` a recorded ``pass`` may be an answer
    the device rejected (for example one listing fewer parts than the
    order), so it stays null; ``fail`` is recovered."""
    env = Env(aws_stack, tuning, error_verdict="fail")
    views = env.recorded()
    assert views["bf-fail"] == recovered("fail", 0.88)
    assert views["bf-pass"] == NOT_RECOVERED_PASS


def test_a_session_without_error_verdict_recovers_only_fail_until_opened(
        aws_stack, tuning):
    """A session created before ``errorVerdict`` was recorded recovers only
    ``fail``; opening it again records the node's ``errorVerdict`` with its
    own update, after which ``pass`` is recovered too."""
    env = Env(aws_stack, tuning)
    env.table().update_item(
        Key={"pk": f"SESSION#{env.session_id}", "sk": "META"},
        UpdateExpression="REMOVE errorVerdict")
    views = env.recorded()
    assert views["bf-fail"] == recovered("fail", 0.88)
    assert views["bf-pass"] == NOT_RECOVERED_PASS

    assert env.open_session() == env.session_id
    meta = env.table().get_item(
        Key={"pk": f"SESSION#{env.session_id}", "sk": "META"})["Item"]
    assert meta["errorVerdict"] == "error"
    assert env.recorded()["bf-pass"] == recovered("pass", 0.93)


def test_a_failed_error_verdict_update_still_opens_the_session(
        aws_stack, tuning, monkeypatch, caplog):
    """When the separate ``errorVerdict`` update fails, the session opens
    without it (only ``fail`` is recovered) and a WARNING is logged."""
    env = Env(aws_stack, tuning)
    env.table().update_item(
        Key={"pk": f"SESSION#{env.session_id}", "sk": "META"},
        UpdateExpression="REMOVE errorVerdict")

    class RefusingErrorVerdict:
        def __init__(self, table):
            self._table = table

        def __getattr__(self, name):
            return getattr(self._table, name)

        def update_item(self, **kwargs):
            if str(kwargs.get("UpdateExpression", "")).startswith(
                    "SET errorVerdict"):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceeded"
                                       "Exception", "Message": "busy"}},
                    "UpdateItem")
            return self._table.update_item(**kwargs)

    real_table = tuning.table
    monkeypatch.setattr(tuning, "table",
                        lambda: RefusingErrorVerdict(real_table()))
    with caplog.at_level(logging.WARNING):
        assert env.open_session() == env.session_id
    assert any("errorVerdict" in record.getMessage()
               and record.levelno == logging.WARNING
               for record in caplog.records)
    monkeypatch.setattr(tuning, "table", real_table)
    views = env.recorded()
    assert views["bf-pass"] == NOT_RECOVERED_PASS
    assert views["bf-fail"] == recovered("fail", 0.88)


def test_effective_recorded_keeps_every_other_sidecar_as_recorded(tuning):
    """The rule on its own: only a ``bedrock_inference`` sidecar with a
    null verdict, no parse error and a schema-valid inspection answer is
    recovered; a live inspection sample keeps its recorded verdict."""
    effective_recorded = tuning.effective_recorded
    backfilled = {"nodeType": "bedrock_inference",
                  "recorded": {"isAnomalous": None, "confidence": None,
                               "answer": FAIL_ANSWER, "parseError": None}}
    assert effective_recorded(backfilled, None) == {
        "isAnomalous": True, "confidence": 0.88, "verdict": "fail",
        "recovered": True}
    assert effective_recorded(dict(backfilled, nodeType="llm_inference"),
                              "error")["recovered"] is False
    live = {"nodeType": "bedrock_inference",
            "recorded": {"isAnomalous": False, "confidence": 0.7,
                         "verdict": "pass", "answer": PASS_ANSWER,
                         "parseError": None}}
    assert effective_recorded(live, "fail") == {
        "isAnomalous": False, "confidence": 0.7, "verdict": "pass",
        "recovered": False}
    assert effective_recorded({}, "error") == {
        "isAnomalous": None, "confidence": None, "verdict": None,
        "recovered": False}
    assert effective_recorded(None, "error")["verdict"] is None


def test_legacy_samples_are_unchanged(aws_stack, tuning):
    """Preservation: a legacy anomaly-mode sample's recorded view keeps
    exactly today's four keys and values. Passes at c299a6e1 too."""
    item = {"sampleId": "dev-1/legacy", "sidecar": {
        "nodeType": "bedrock_inference",
        "recorded": {"isAnomalous": True, "confidence": 0.9,
                     "answer": LEGACY_ANSWER, "parseError": None}}}
    view = tuning.sample_view(item, None)
    assert view["recorded"] == {"isAnomalous": True, "confidence": 0.9,
                                "answer": LEGACY_ANSWER, "parseError": None}
    unparsed = {"sampleId": "dev-1/unparsed", "sidecar": {
        "nodeType": "bedrock_inference",
        "recorded": {"isAnomalous": None, "confidence": None,
                     "answer": "the plate looks fine",
                     "parseError": "no JSON object found"}}}
    assert tuning.sample_view(unparsed, None)["recorded"] == {
        "isAnomalous": None, "confidence": None,
        "answer": "the plate looks fine",
        "parseError": "no JSON object found"}
