"""Inspection-format candidates are scored under the inspection contract
(AC-4, AC-5, the Portal halves of AC-6b and AC-6c, and AC-35).

The real ``functions/workflow_tuning.py`` handler against moto, with the
module's dispatch seam driven inline and a scripted Bedrock client that
records every Converse request. Samples are written to the Sample_Store the
way the device writes them and indexed through the session route.

What an inspection-format replay must do (design §10.1):

- parse the answer with ``parse_inspection_answer``, ``fail`` counting as
  anomalous, so the categories follow the Labels; a rejected answer is a
  ``parse_failure`` carrying the parser's reason;
- record ``verdict``, ``imageCount`` (and ``documentCount``), ``latencyMs``
  (also on a client error), ``rawAnswer`` and ``outputTokens``;
- render the Candidate's texts from the sample's ``metadataSnippetJson``,
  masked signed values included, and never render metadata the device left
  out of the snippet;
- send the sample's recorded references in recorded order, with their kinds
  and formats, and make no Bedrock call when the sample cannot reproduce the
  device's request: a missing reference, another reference count, malformed
  recorded references, an unresolved placeholder, an unresolvable expected
  count, an omitted-metadata conflict or an unreadable reference object;
- split a step whose samples' recorded bytes pass 128 MiB, with the same
  outcomes as an unsplit run.

The node's ``anomaly_mode`` is absent (the Bedrock default), so the node is
tunable at c299a6e1 too and every test reaches its scoring assertion there:
the legacy parser rejects inspection JSON (``parse_failure`` where
``correct`` is expected), the template is sent unrendered, and every guarded
sample still reaches Bedrock.
"""
import hashlib
import json
import os
import sys
import threading
import time
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
MIB = 1024 * 1024

#: Restated locally (never imported): the instruction an inspection-format
#: node appends instead of the Verdict_Instruction, and its separator.
INSPECTION_INSTRUCTION = (
    'Respond with exactly one JSON object and nothing else: {"verdict": '
    '"pass"|"fail", "confidence": 0..1, "reason": "<one sentence>", '
    '"objects": [{"name": "<part>", "qc": "OK"|"NOK", "reason": "<short>", '
    '"bounding_box": {"x_min": <int>, "y_min": <int>, "x_max": <int>, '
    '"y_max": <int>}}, ...]} with one entry in objects per expected part.')
SEPARATOR = "\n\n"

#: The design §10.1 texts of a replay that cannot be faithful.
MISSING_REFERENCE = (
    "the sample was recorded without a reference image, but this node sends "
    "one (reference_payload_path is set); it cannot be replayed faithfully")
REFERENCE_COUNT = (
    "the sample was recorded with 3 reference file(s), but this node now "
    "sends 2; it cannot be replayed faithfully")
MALFORMED_REFERENCES = "the sample's recorded references are malformed"
OMITTED_METADATA = (
    "the sample's recorded metadata leaves out "
    "trigger.payload_json.refs.0.image (inline reference image data), which "
    "this prompt renders; it cannot be replayed faithfully")

BASE_PARAMETERS = {
    "model": "us.amazon.nova-pro-v1:0",
    "region": "us-west-2",
    "answer_format": "inspection",
    "box_scale": "pixels",
    "max_tokens": 600,
    "prompt": "Check the parts.",
}
LEGEND = "file:///aws_dda/reference/legend.jpg"
NOTES = "file:///aws_dda/reference/notes.txt"
SHEET = "file:///aws_dda/reference/sheet.pdf"
ORDER_SNIPPET = {"trigger": {"topic": "plant/line1/evaluate",
                             "payload_json": {"order_id": "o-1"}, "qos": 1}}


def inspection_answer(verdict, names=("roof",), qc=None, confidence=0.9):
    """A schema-valid inspection answer."""
    return json.dumps({
        "verdict": verdict, "confidence": confidence, "reason": "checked",
        "objects": [{"name": name,
                     "qc": qc or ("OK" if verdict == "pass" else "NOK"),
                     "reason": "seen",
                     "bounding_box": {"x_min": 10, "y_min": 10, "x_max": 90,
                                      "y_max": 90}}
                    for name in names]})


PASS = inspection_answer("pass", confidence=0.93)
FAIL = inspection_answer("fail", confidence=0.81)
#: Schema-valid, but a pass may not carry a NOK object.
PASS_WITH_NOK = inspection_answer("pass", qc="NOK")


# ==========================================================================
# Harness
# ==========================================================================

class FakeBedrock:
    """A scripted Bedrock runtime client: the answer is chosen by the
    execution id carried in the request's input image bytes. Every request
    is recorded."""

    def __init__(self):
        self.script = {}
        self.default = PASS
        self.calls = []
        self._lock = threading.Lock()

    def converse(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        content = kwargs["messages"][0]["content"]
        marker = next((block["image"]["source"]["bytes"] for block in content
                       if "image" in block), b"").decode("utf-8", "replace")
        answer = self.script.get(marker[len("INPUT-"):], self.default)
        if isinstance(answer, BaseException):
            raise answer
        return {"output": {"message": {"content": [{"text": answer}]}},
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
        self.bedrock = FakeBedrock()
        self.regions = []
        self.exported_at = 1000
        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client", self._client)

    def _client(self, region):
        self.regions.append(region)
        return self.bedrock

    # ------------------------------------------------------------- setup
    def put_workflow(self, parameters, version=1):
        node = {"id": NODE_ID, "type": "bedrock_inference",
                "position": {"x": 0, "y": 0},
                "parameters": dict(BASE_PARAMETERS, **parameters)}
        document = {"schemaVersion": "1.0", "nodes": [node],
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Inspection workflow",
            "created_at": 1, "updated_at": version,
            "latest_version": version, "created_by": self.user["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": self.workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.user["user_id"]})

    def put_sample(self, execution, snippet=ORDER_SNIPPET, omitted=None,
                   reference=False, references=(), missing=(),
                   input_recorded_bytes=None):
        """One inspection Tuning_Sample exactly as the device writes it.

        ``reference`` writes the single-image form; ``references`` is the
        list form as ``(kind, format)`` pairs, and ``missing`` the 1-based
        positions whose objects are not written."""
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/{NODE_ID}/{THING}/"
                f"{execution}")
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        self.exported_at += 1
        sidecar = {
            "schemaVersion": 1, "source": "live",
            "answerFormat": "inspection", "workflowId": self.workflow_id,
            "version": 1, "executionId": execution, "nodeId": NODE_ID,
            "nodeType": "bedrock_inference", "thingName": THING,
            "exportedAt": self.exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": input_recorded_bytes or len(input_bytes),
                      "width": 640, "height": 480},
            "recorded": {"isAnomalous": False, "confidence": 0.9,
                         "verdict": "pass", "answer": PASS,
                         "parseError": None},
            "promptFingerprint": "sha256:recorded",
            "detectionId": None, "detectionSlot": None,
        }
        if isinstance(snippet, str):
            sidecar["metadataSnippetJson"] = snippet
        elif snippet is not None:
            sidecar["metadataSnippetJson"] = json.dumps(
                snippet, separators=(",", ":"))
        if omitted is not None:
            sidecar["metadataSnippetOmitted"] = list(omitted)
        objects = {}
        if reference:
            data = f"REF-{execution}".encode()
            key = base + ".reference.jpg"
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key, Body=data)
            objects[key] = data
            sidecar["reference"] = {
                "key": key, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "width": 320, "height": 200}
        if references:
            entries = []
            for index, (kind, fmt) in enumerate(references, start=1):
                data = f"REF{index}-{execution}".encode()
                extension = "jpg" if kind == "image" else fmt
                key = f"{base}.reference-{index}.{extension}"
                if index not in missing:
                    self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key,
                                       Body=data)
                objects[key] = data
                entry = {"index": index, "label": f"Reference {index}",
                         "kind": kind, "format": fmt, "key": key,
                         "sha256": hashlib.sha256(data).hexdigest(),
                         "bytes": len(data)}
                if kind == "image":
                    entry.update(width=320, height=200)
                entries.append(entry)
            sidecar["references"] = entries
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=json.dumps(sidecar).encode("utf-8"))
        return {"sampleId": f"{THING}/{execution}", "objects": objects,
                "base": base}

    # ---------------------------------------------------------- invoke
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
        self.session_id = body["session"]["sessionId"]
        return self.session_id

    def label(self, sample_ids, label):
        status, body = self.call(
            "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels",
            {"id": self.session_id},
            body={"sampleIds": list(sample_ids), "label": label})
        assert status == 200, body
        assert body["missing"] == [], body

    def candidate(self, prompt, system_prompt="", max_tokens=600):
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/candidates",
            {"id": self.session_id},
            body={"name": f"Candidate {uuid.uuid4().hex[:6]}",
                  "prompt": prompt, "systemPrompt": system_prompt,
                  "maxTokens": max_tokens})
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def score(self, candidate_id="baseline"):
        """Run one Score_Run to its end; returns ``(outcomes by sample id,
        the step results)``."""
        status, body = self.call(
            "POST", f"{ANOMALY}/sessions/{{id}}/score-runs",
            {"id": self.session_id},
            body={"candidateId": candidate_id, "repeats": 1})
        assert status == 202, body
        run_id = body["runId"]
        steps = []
        while self.dispatched:
            assert len(steps) < 20, "the step chain did not terminate"
            steps.append(self.module.handler(self.dispatched.pop(0), None))
        status, view = self.call(
            "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes", {"rid": run_id})
        assert status == 200, view
        assert view["run"]["status"] == "completed", view["run"]
        return {o["sampleId"]: o for o in view["outcomes"]}, steps


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


def content_of(kwargs):
    return kwargs["messages"][0]["content"]


def block_kinds(kwargs):
    return [next(iter(block)) for block in content_of(kwargs)]


# ==========================================================================
# AC-4: categories, verdict and the recorded outcome fields
# ==========================================================================

def test_inspection_answers_are_categorized_by_the_inspection_parser(env):
    """pass on OK and fail on NOK are correct, pass on NOK a false pass,
    fail on OK a false fail, and an answer the inspection parser rejects a
    parse failure carrying the parser's reason."""
    env.put_workflow({})
    samples = {
        "ok-pass": ("OK", PASS), "nok-fail": ("NOK", FAIL),
        "nok-pass": ("NOK", PASS), "ok-fail": ("OK", FAIL),
        "ok-malformed": ("OK", PASS_WITH_NOK),
    }
    ids = {name: env.put_sample(name)["sampleId"] for name in samples}
    env.open_session()
    for name, (label, answer) in samples.items():
        env.label([ids[name]], label)
        env.bedrock.script[name] = answer
    outcomes, _steps = env.score()

    assert {name: outcomes[ids[name]]["category"] for name in samples} == {
        "ok-pass": "correct", "nok-fail": "correct",
        "nok-pass": "false_pass", "ok-fail": "false_fail",
        "ok-malformed": "parse_failure"}
    malformed = outcomes[ids["ok-malformed"]]
    assert malformed["parseError"] == "verdict pass with NOK object(s): roof"
    assert malformed.get("verdict") is None
    passed = outcomes[ids["ok-pass"]]
    assert (passed["verdict"], passed["isAnomalous"], passed["confidence"]) \
        == ("pass", False, 0.93)
    failed = outcomes[ids["nok-fail"]]
    assert (failed["verdict"], failed["isAnomalous"], failed["confidence"]) \
        == ("fail", True, 0.81)
    for kwargs in env.bedrock.calls:
        assert content_of(kwargs)[0]["text"] == (
            "Check the parts." + SEPARATOR + INSPECTION_INSTRUCTION)
    assert set(env.regions) == {"us-west-2"}


def test_an_outcome_records_verdict_image_count_latency_answer_and_tokens(
        env):
    """The single-image form sends the recorded reference as the second
    image, and the outcome carries ``verdict``, ``imageCount: 2``,
    ``latencyMs``, ``rawAnswer`` and ``outputTokens``."""
    env.put_workflow({"reference_payload_path": LEGEND})
    sample = env.put_sample("two-images", reference=True)
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    outcomes, _steps = env.score()

    outcome = outcomes[sample["sampleId"]]
    assert outcome.get("verdict") == "pass"
    assert outcome["imageCount"] == 2
    assert "documentCount" not in outcome
    assert outcome["category"] == "correct"
    assert isinstance(outcome["latencyMs"], int) and outcome["latencyMs"] >= 0
    assert outcome["rawAnswer"] == PASS
    assert outcome["outputTokens"] == 42
    (kwargs,) = env.bedrock.calls
    content = content_of(kwargs)
    assert block_kinds(kwargs) == ["text", "text", "image", "text", "image"]
    assert [content[1]["text"], content[3]["text"]] == [
        "Input image:", "Reference image:"]
    (reference_bytes,) = sample["objects"].values()
    assert content[4]["image"]["source"]["bytes"] == reference_bytes


def test_a_bedrock_client_error_is_an_invocation_error_with_latency(env):
    """A client error is ``invocation_error`` with the error text, and the
    outcome still records ``latencyMs`` and ``imageCount``."""
    env.put_workflow({})
    sample = env.put_sample("throttled")
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    env.bedrock.script["throttled"] = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Converse")
    outcomes, _steps = env.score()

    outcome = outcomes[sample["sampleId"]]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"].startswith("ClientError: ")
    assert "ThrottlingException" in outcome["error"]
    assert outcome["latencyMs"] is not None
    assert outcome.get("imageCount") == 1


# ==========================================================================
# AC-5, AC-35: samples that cannot reproduce the device's request
# ==========================================================================

GUARD_CASES = {
    # case: (node parameters, sample keyword arguments, expected error)
    "missing_reference": (
        {"reference_payload_path": LEGEND}, {}, MISSING_REFERENCE),
    "unresolved_placeholder": (
        {"render_prompt": True,
         "prompt": "Order {trigger.payload_json.missing}."}, {},
        "unresolved prompt placeholder trigger.payload_json.missing"),
    "no_snippet": (
        {"render_prompt": True,
         "prompt": "Order {trigger.payload_json.order_id}."},
        {"snippet": None},
        "unresolved prompt placeholder trigger.payload_json.order_id"),
    "undecodable_snippet": (
        {"render_prompt": True,
         "prompt": "Order {trigger.payload_json.order_id}."},
        {"snippet": "not json"},
        "unresolved prompt placeholder trigger.payload_json.order_id"),
    "unresolvable_count": (
        {"expected_count_path": "trigger.payload_json.body.items"}, {},
        "expected count path trigger.payload_json.body.items did not "
        "resolve to a positive count"),
    "reference_count": (
        {"reference_payload_path": LEGEND + "\n" + NOTES},
        {"references": (("image", "jpeg"), ("document", "txt"),
                        ("document", "pdf"))},
        REFERENCE_COUNT),
    "malformed_references": (
        {"reference_payload_path": LEGEND + "\n" + NOTES},
        {"references": (("image", "png"), ("document", "txt"))},
        MALFORMED_REFERENCES),
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
def test_a_sample_that_cannot_be_replayed_faithfully_makes_no_call(env,
                                                                   case):
    """No Bedrock call is made, and the outcome is ``invocation_error``
    with the design's text, for every sample whose recorded data cannot
    reproduce the device's request."""
    parameters, sample_kwargs, expected = GUARD_CASES[case]
    env.put_workflow(parameters)
    sample = env.put_sample(f"guard-{case}", **sample_kwargs)
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    outcomes, _steps = env.score()

    assert env.bedrock.calls == []
    outcome = outcomes[sample["sampleId"]]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"] == expected
    assert outcome["latencyMs"] is None


def test_an_unreadable_reference_object_is_the_per_sample_read_error(env):
    """A recorded reference object that cannot be read is the existing
    per-sample read error naming that key, with no Bedrock call."""
    env.put_workflow({"reference_payload_path": "\n".join(
        (LEGEND, NOTES, SHEET))})
    sample = env.put_sample(
        "gone-notes", missing=(2,),
        references=(("image", "jpeg"), ("document", "txt"),
                    ("document", "pdf")))
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    outcomes, _steps = env.score()

    assert env.bedrock.calls == []
    outcome = outcomes[sample["sampleId"]]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"].startswith(
        "could not read the sample image " + sample["base"]
        + ".reference-2.txt: ")


def test_a_sample_with_a_reference_is_replayed_single_image_for_a_node_without_one(env):  # noqa: E501
    """A node that now configures no reference replays the sample with its
    input image only; that is not an error (FR-4.3a)."""
    env.put_workflow({})
    sample = env.put_sample("dropped-reference", reference=True)
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    outcomes, _steps = env.score()

    (kwargs,) = env.bedrock.calls
    assert block_kinds(kwargs) == ["text", "text", "image"]
    outcome = outcomes[sample["sampleId"]]
    assert outcome.get("imageCount") == 1
    assert outcome["category"] == "correct"


# ==========================================================================
# AC-6b, AC-6c: rendering from the recorded snippet
# ==========================================================================

def test_a_masked_signed_url_is_replayed_masked(env):
    """The prompt is rendered from the recorded snippet, so a presigned URL
    the device stored masked is replayed with the masked value."""
    masked = ("https://images.example.com/part.jpg?X-Amz-Algorithm="
              "AWS4-HMAC-SHA256&X-Amz-Credential=***&X-Amz-Signature=***")
    env.put_workflow({"render_prompt": True,
                      "prompt": "Inspect {trigger.payload_json.image_url}."})
    sample = env.put_sample("masked", snippet={"trigger": {
        "topic": "plant/line1/evaluate",
        "payload_json": {"image_url": masked}, "qos": 1}})
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    env.score()

    (kwargs,) = env.bedrock.calls
    assert content_of(kwargs)[0]["text"] == (
        "Inspect " + masked + "." + SEPARATOR + INSPECTION_INSTRUCTION)


def test_metadata_left_out_of_the_snippet_is_never_rendered(env):
    """A sample whose trigger carried its reference as inline data: a
    Candidate that renders the omitted path is ``invocation_error`` naming
    it, with no Bedrock call; one that renders only the order id is
    replayed with both images."""
    env.put_workflow({"render_prompt": True,
                      "reference_payload_path": "refs.0.image",
                      "prompt": "Order {trigger.payload_json.order_id}."})
    sample = env.put_sample(
        "inline-reference", reference=True,
        snippet={"trigger": {"topic": "plant/line1/evaluate",
                             "payload_json": {"refs": [{}],
                                              "order_id": "o-1"},
                             "qos": 1}},
        omitted=("trigger.payload", "trigger.payload_json.refs.0.image"))
    env.open_session()
    env.label([sample["sampleId"]], "OK")

    renders_refs = env.candidate("Refs {json:trigger.payload_json.refs}.")
    outcomes, _steps = env.score(renders_refs)
    assert env.bedrock.calls == []
    outcome = outcomes[sample["sampleId"]]
    assert outcome["category"] == "invocation_error"
    assert outcome["error"] == OMITTED_METADATA

    renders_order = env.candidate("Order {trigger.payload_json.order_id}.")
    outcomes, _steps = env.score(renders_order)
    (kwargs,) = env.bedrock.calls
    assert content_of(kwargs)[0]["text"] == (
        "Order o-1." + SEPARATOR + INSPECTION_INSTRUCTION)
    assert block_kinds(kwargs) == ["text", "text", "image", "text", "image"]
    assert outcomes[sample["sampleId"]]["category"] == "correct"


# ==========================================================================
# AC-35: the list form
# ==========================================================================

def test_a_list_form_sample_is_replayed_with_its_documents_in_order(env):
    """The recorded references are sent in recorded order with their kinds
    and formats, and the outcome counts two images and two documents."""
    env.put_workflow({"reference_payload_path": "\n".join(
        (LEGEND, NOTES, SHEET))})
    sample = env.put_sample(
        "mixed", references=(("image", "jpeg"), ("document", "txt"),
                             ("document", "pdf")))
    env.open_session()
    env.label([sample["sampleId"]], "OK")
    outcomes, _steps = env.score()

    outcome = outcomes[sample["sampleId"]]
    assert (outcome.get("imageCount"), outcome.get("documentCount")) == (2, 2)
    (kwargs,) = env.bedrock.calls
    content = content_of(kwargs)
    assert block_kinds(kwargs) == ["text", "text", "image", "text", "image",
                                   "text", "document", "text", "document"]
    assert [content[i]["text"] for i in (1, 3, 5, 7)] == [
        "Input image:", "Reference 1:", "Reference 2:", "Reference 3:"]
    by_key = sample["objects"]
    assert content[4]["image"]["source"]["bytes"] == by_key[
        sample["base"] + ".reference-1.jpg"]
    assert content[6]["document"] == {
        "format": "txt", "name": "Reference 2",
        "source": {"bytes": by_key[sample["base"] + ".reference-2.txt"]}}
    assert content[8]["document"] == {
        "format": "pdf", "name": "Reference 3",
        "source": {"bytes": by_key[sample["base"] + ".reference-3.pdf"]}}


def test_a_step_holding_more_than_128_mib_of_samples_is_split(env,
                                                              monkeypatch):
    """Two samples recording 70 MiB each are replayed in two steps, and the
    outcomes equal those of an unsplit run of the same Candidate."""
    env.put_workflow({})
    first = env.put_sample("large-1", input_recorded_bytes=70 * MIB)
    second = env.put_sample("large-2", input_recorded_bytes=70 * MIB)
    env.open_session()
    env.label([first["sampleId"]], "OK")
    env.label([second["sampleId"]], "NOK")
    env.bedrock.script.update({"large-1": PASS, "large-2": FAIL})

    split, steps = env.score()
    assert len(steps) == 2
    assert [step.get("issued") for step in steps] == [1, 1]

    monkeypatch.setattr(env.module, "CHUNK_SAMPLE_BYTES", 2 ** 40)
    unsplit, steps = env.score()
    assert len(steps) == 1
    assert {sid: o["category"] for sid, o in split.items()} == \
        {sid: o["category"] for sid, o in unsplit.items()} == {
            first["sampleId"]: "correct", second["sampleId"]: "correct"}
