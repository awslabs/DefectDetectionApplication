"""Inspection-format nodes are tunable: the rule, published (AC-3).

**Feature: inspection-format tuning — the Tunable_Node rule (FR-1).**

A ``bedrock_inference`` node whose ``answer_format`` is exactly
``inspection`` is a Tunable_Node whatever its ``anomaly_mode``: the
executor exports every answered inspection run. Every other node keeps the
anomaly-mode rule of Property 1.

This file is the Portal half. It publishes
``tests/fixtures/inspection_tuning_cases.json``, which the frontend half
(``eligibility.inspection.property.test.ts`` and
``utils/referenceFiles.mirror.test.ts``) asserts:

- the published eligibility case table
  (``anomaly_tuning_eligibility_cases.json``) crossed with ``answer_format``
  values, each with its expected tunability;
- the inspection instruction and its separator, which the frontend's draft
  projection must reproduce;
- ``referenceListCases``: for each ``reference_payload_path`` value and
  ``answer_format``, the kinds, labels and problems the shared
  reference-list functions give.

Regenerate it after an intentional change with::

    DDA_WRITE_INSPECTION_TUNING_FIXTURE=1 \\
      python -m pytest tests/test_property_tuning_inspection_format.py

The expectations are restated here, never imported from the module under
test: the anomaly-mode answer is the committed eligibility fixture's (its own
test derives it from an independent restatement), and the inspection clause
is restated below. Beyond the rule, a tuning session for an inspection-format
node with ``anomaly_mode: false`` is created, scored and applied through the
routes, and the three refusals name both settings.

At c299a6e1 the three-argument ``is_tunable_node`` raises ``TypeError``,
``node_is_tunable`` of an inspection node with ``anomaly_mode: false``
returns ``False``, and the session create answers ``400 NODE_NOT_TUNABLE``.
"""
import hashlib
import json
import os
import sys
import time
import uuid
from decimal import Decimal

import boto3
import pytest
from dynamo_helpers import all_table_names

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_CORE_LAYER = os.path.abspath(
    os.path.join(_HERE, "..", "layers", "workflow_core", "python"))
if _WORKFLOW_CORE_LAYER not in sys.path:
    sys.path.append(_WORKFLOW_CORE_LAYER)

from workflow_core import anomaly_invocation as ai  # noqa: E402

FIXTURE_PATH = os.path.join(_HERE, "fixtures", "inspection_tuning_cases.json")
ELIGIBILITY_FIXTURE_PATH = os.path.join(
    _HERE, "fixtures", "anomaly_tuning_eligibility_cases.json")
WRITE_ENV = "DDA_WRITE_INSPECTION_TUNING_FIXTURE"

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"
SAMPLES_PREFIX = "workflow-tuning/samples/"
ANOMALY = "/workflow-tuning/anomaly"
BEDROCK = "bedrock_inference"
LLM = "llm_inference"

#: The ``answer_format`` values crossed with the eligibility table:
#: ``(id, present, value)``. Only the exact string ``inspection`` selects
#: the inspection format; case, surrounding space and other values do not.
ANSWER_FORMATS = (
    ("absent", False, None),
    ("inspection", True, "inspection"),
    ("legacy", True, "legacy"),
    ("Inspection", True, "Inspection"),
    ("space-inspection", True, " inspection"),
    ("empty", True, ""),
    ("null", True, None),
)

#: The rule both halves implement, published with the fixture.
RULE = [
    "A bedrock_inference node whose answer_format is exactly the string "
    "'inspection' (no trimming, no case folding) is a Tunable_Node, "
    "whatever its anomaly_mode.",
    "Every other node, an llm_inference node with answer_format "
    "'inspection' included, is tunable exactly when the eligibility table's "
    "expectedTunable says so (Requirement 1.5, Property 1).",
]

UPLOAD_PNG = "upload://" + "a" * 64 + ".png"
UPLOAD_PDF = "upload://" + "b" * 64 + ".pdf"

#: ``reference_payload_path`` values of ``referenceListCases``: ``(id,
#: value)``. JSON values only, and only whitespace that Python's
#: ``str.strip`` and JavaScript's ``String.prototype.trim`` treat alike.
REFERENCE_LIST_VALUES = (
    ("null", None),
    ("number", 7),
    ("empty", ""),
    ("blank-lines", "  \n \n"),
    ("payload-path", "refs.0.image"),
    ("payload-path-named-like-a-pdf", "refs.0.pdf"),
    ("file-jpg", "file:///aws_dda/images/source.jpg"),
    ("s3-png", "s3://reference-bucket/legend.png"),
    ("https-pdf-query-fragment",
     "https://example.com/docs/sheet.PDF?X-Amz-Signature=abc#page=2"),
    ("file-docx-upper-case", "file:///aws_dda/reference/Assembly.DOCX"),
    ("file-markdown", "file:///aws_dda/reference/notes.markdown"),
    ("file-htm", "file:///aws_dda/reference/page.htm"),
    ("file-xls", "file:///aws_dda/reference/parts.xls"),
    ("leading-dot-name", "file:///aws_dda/reference/.pdf"),
    ("uri-without-path", "https://example.com"),
    ("upper-case-scheme", "FILE:///aws_dda/reference/sheet.pdf"),
    ("upload-png", UPLOAD_PNG),
    ("upload-pdf", UPLOAD_PDF),
    ("upload-upper-case-hex", "upload://" + "A" * 64 + ".png"),
    ("upload-63-hex", "upload://" + "a" * 63 + ".png"),
    ("upload-gif", "upload://" + "a" * 64 + ".gif"),
    ("upload-slash", "upload://" + "a" * 32 + "/" + "a" * 31 + ".png"),
    ("legend-and-pdf",
     "file:///aws_dda/reference/legend.jpg\n"
     "file:///aws_dda/reference/assembly.pdf"),
    ("png-txt-pdf-mixed-newlines",
     "file:///aws_dda/reference/legend.png\r\n"
     "file:///aws_dda/reference/notes.txt\r"
     "file:///aws_dda/reference/sheet.pdf"),
    ("spaces-and-blank-lines",
     "  refs.0.image  \n\n\t file:///aws_dda/reference/notes.txt \n"),
    ("upload-and-uri", UPLOAD_PNG + "\nfile:///aws_dda/reference/sheet.pdf"),
    ("six-documents", "\n".join(
        "file:///aws_dda/reference/doc{0}.pdf".format(n) for n in range(6))),
    ("twenty-images", "\n".join(
        "refs.{0}.image".format(n) for n in range(20))),
)

#: The ``answer_format`` values each reference-list value is crossed with.
REFERENCE_LIST_ANSWER_FORMATS = (
    ("inspection", "inspection"), ("legacy", "legacy"), ("null", None))


# ---------------------------------------------------------------------------
# The case table and the fixture
# ---------------------------------------------------------------------------

def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def ref_is_tunable(case, answer_format_present, answer_format):
    """The rule, restated: the inspection clause first, then the
    anomaly-mode answer the eligibility table publishes."""
    if (case["nodeType"] == BEDROCK and answer_format_present
            and answer_format == "inspection"):
        return True
    return case["expectedTunable"]


def _derive_cases():
    eligibility = _load(ELIGIBILITY_FIXTURE_PATH)
    return [{
        "id": case["id"],
        "nodeType": case["nodeType"],
        "anomalyModeId": case["anomalyModeId"],
        "anomalyModePresent": case["anomalyModePresent"],
        "anomalyMode": case["anomalyMode"],
        "expectedTunable": {
            format_id: ref_is_tunable(case, present, value)
            for format_id, present, value in ANSWER_FORMATS},
    } for case in eligibility["cases"]]


def _derive_reference_list_cases():
    """The shared functions' answers for every value and answer_format.
    These names are new with this change, so they are imported here."""
    from workflow_core.anomaly_invocation import (
        parse_reference_list, reference_labels, reference_list_problems)
    cases = []
    for value_id, value in REFERENCE_LIST_VALUES:
        specs = parse_reference_list(value)
        kinds = [spec.kind for spec in specs]
        labels = [spec.label for spec in specs]
        assert labels == list(reference_labels(kinds))
        for format_id, answer_format in REFERENCE_LIST_ANSWER_FORMATS:
            cases.append({
                "id": "{0}#{1}".format(value_id, format_id),
                "value": value,
                "answerFormat": answer_format,
                "kinds": kinds,
                "labels": labels,
                "problems": list(reference_list_problems(specs,
                                                         answer_format)),
            })
    return cases


def _derive_fixture():
    return {
        "feature": "inspection-format tuning",
        "rule": RULE,
        "generatedBy": ("edge-cv-portal/backend/tests/"
                        "test_property_tuning_inspection_format.py"),
        "regenerateWith": (
            "DDA_WRITE_INSPECTION_TUNING_FIXTURE=1 python -m pytest "
            "tests/test_property_tuning_inspection_format.py"),
        "consumedBy": [
            "edge-cv-portal/backend/tests/"
            "test_property_tuning_inspection_format.py",
            "edge-cv-portal/frontend/src/pages/workflow-tuning/"
            "eligibility.inspection.property.test.ts",
            "edge-cv-portal/frontend/src/utils/referenceFiles.mirror.test.ts",
        ],
        "inspectionJsonInstruction": ai.INSPECTION_JSON_INSTRUCTION,
        "anomalyInstructionSeparator": ai.ANOMALY_INSTRUCTION_SEPARATOR,
        "answerFormats": [{"id": format_id, "present": present,
                           "value": value}
                          for format_id, present, value in ANSWER_FORMATS],
        "cases": _derive_cases(),
        "referenceListCases": _derive_reference_list_cases(),
    }


def _serialize(document):
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _parameters(case, format_entry):
    parameters = {}
    if case["anomalyModePresent"]:
        parameters["anomaly_mode"] = case["anomalyMode"]
    if format_entry["present"]:
        parameters["answer_format"] = format_entry["value"]
    return parameters


def test_published_fixture_matches_the_case_table():
    """The fixture on disk is exactly the derived document, so the frontend
    half asserts the cases this half derives."""
    serialized = _serialize(_derive_fixture())
    if os.environ.get(WRITE_ENV) == "1":
        with open(FIXTURE_PATH, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    assert os.path.exists(FIXTURE_PATH), (
        "missing shared fixture {0}; regenerate with {1}=1".format(
            FIXTURE_PATH, WRITE_ENV))
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        on_disk = handle.read()
    assert on_disk == serialized, (
        "the published inspection tuning fixture differs from the derived "
        "cases; regenerate it with {0}=1 and review the diff with the "
        "frontend half".format(WRITE_ENV))


def test_three_argument_rule_agrees_with_every_published_case():
    """``is_tunable_node(type, anomaly_mode, answer_format)`` returns the
    published expectation for every case, and an inspection-format Bedrock
    node is tunable for every ``anomaly_mode`` value of the table."""
    document = _load(FIXTURE_PATH)
    disagreements = []
    for case in document["cases"]:
        mode = case["anomalyMode"] if case["anomalyModePresent"] else None
        for format_entry in document["answerFormats"]:
            value = format_entry["value"] if format_entry["present"] else None
            actual = ai.is_tunable_node(case["nodeType"], mode, value)
            expected = case["expectedTunable"][format_entry["id"]]
            if actual is not expected:
                disagreements.append(
                    (case["id"], format_entry["id"], expected, actual))
    assert not disagreements, disagreements
    bedrock = [case for case in document["cases"]
               if case["nodeType"] == BEDROCK]
    assert len(bedrock) == len({case["anomalyModeId"] for case in bedrock})
    assert all(case["expectedTunable"]["inspection"] for case in bedrock)
    assert any(not case["expectedTunable"]["absent"] for case in bedrock)


def test_node_is_tunable_agrees_with_every_published_case(tuning):
    """The Portal's ``node_is_tunable`` reads ``answer_format`` off the
    definition node and agrees with every published case."""
    document = _load(FIXTURE_PATH)
    disagreements = []
    for case in document["cases"]:
        for format_entry in document["answerFormats"]:
            node = {"id": "n", "type": case["nodeType"],
                    "parameters": _parameters(case, format_entry)}
            actual = tuning.node_is_tunable(node)
            expected = case["expectedTunable"][format_entry["id"]]
            if actual is not expected:
                disagreements.append(
                    (case["id"], format_entry["id"], expected, actual))
    assert not disagreements, disagreements


def test_the_published_instruction_strings_are_the_shared_modules():
    """Preservation: the inspection instruction and its separator that the
    fixture publishes for the frontend's draft projection are the strings
    the shared module already appends (unchanged by this fix)."""
    document = _load(FIXTURE_PATH)
    assert document["inspectionJsonInstruction"] == \
        ai.INSPECTION_JSON_INSTRUCTION
    assert document["anomalyInstructionSeparator"] == "\n\n" == \
        ai.ANOMALY_INSTRUCTION_SEPARATOR
    assert document["inspectionJsonInstruction"].startswith(
        "Respond with exactly one JSON object and nothing else: ")


def test_the_published_reference_list_cases_are_the_shared_rules():
    """Every published reference-list case is what the shared functions
    give, and the table covers each rule the editor and the validator
    report."""
    from workflow_core.anomaly_invocation import (
        parse_reference_list, reference_list_problems)
    document = _load(FIXTURE_PATH)
    by_id = {case["id"]: case for case in document["referenceListCases"]}
    assert len(by_id) == len(REFERENCE_LIST_VALUES) * len(
        REFERENCE_LIST_ANSWER_FORMATS)
    for case in document["referenceListCases"]:
        specs = parse_reference_list(case["value"])
        assert [spec.kind for spec in specs] == case["kinds"], case["id"]
        assert [spec.label for spec in specs] == case["labels"], case["id"]
        assert list(reference_list_problems(
            specs, case["answerFormat"])) == case["problems"], case["id"]

    mixed = by_id["png-txt-pdf-mixed-newlines#inspection"]
    assert mixed["kinds"] == ["image", "document", "document"]
    assert mixed["labels"] == ["Reference 1", "Reference 2", "Reference 3"]
    assert mixed["problems"] == []
    assert by_id["file-jpg#legacy"]["labels"] == ["Reference image"]
    assert by_id["payload-path-named-like-a-pdf#inspection"]["kinds"] == [
        "image"]
    assert by_id["https-pdf-query-fragment#inspection"]["kinds"] == [
        "document"]
    assert by_id["upper-case-scheme#inspection"]["kinds"] == ["image"]
    assert by_id["null#inspection"]["kinds"] == []
    legacy_pair = by_id["legend-and-pdf#legacy"]["problems"]
    assert len(legacy_pair) == 1
    assert "only answer_format inspection" in legacy_pair[0]
    assert by_id["legend-and-pdf#inspection"]["problems"] == []
    for malformed in ("upload-upper-case-hex", "upload-63-hex", "upload-gif",
                      "upload-slash"):
        problems = by_id[malformed + "#inspection"]["problems"]
        assert problems and problems[0].startswith(
            "line 1 of reference_payload_path is not a valid uploaded-file "
            "reference"), malformed
    assert by_id["six-documents#inspection"]["problems"] == [
        "reference_payload_path lists 6 documents; Bedrock Converse accepts "
        "at most 5 per request"]
    assert by_id["twenty-images#inspection"]["problems"] == [
        "reference_payload_path lists 20 images; with the input image that "
        "is more than the 20 images Bedrock Converse accepts per request"]


# ---------------------------------------------------------------------------
# AC-3 through the routes: create, score and apply
# ---------------------------------------------------------------------------

INSPECTION_NODE = {
    "id": "check",
    "type": BEDROCK,
    "position": {"x": 0, "y": 0},
    "parameters": {
        "model": "us.amazon.nova-pro-v1:0",
        "region": "us-west-2",
        "anomaly_mode": False,
        "answer_format": "inspection",
        "box_scale": "pixels",
        "prompt": "Check that every ordered part is present.",
        "max_tokens": 600,
    },
}
LEGACY_OFF_NODE = {
    "id": "check",
    "type": BEDROCK,
    "position": {"x": 0, "y": 0},
    "parameters": {"model": "us.amazon.nova-pro-v1:0",
                   "prompt": "Describe the part.", "anomaly_mode": False},
}
CAMERA_NODE = {"id": "cam", "type": "camera_source",
               "position": {"x": 0, "y": 200},
               "parameters": {"camera_id": "cam-a"}}
CONNECTION = {"id": "c1", "from": {"node": "cam", "port": "out"},
              "to": {"node": "check", "port": "in"}}
NOT_TUNABLE_MESSAGE = (
    "Node 'check' is not tunable in version {version}: tuning needs a "
    "Bedrock node in anomaly mode or inspection format, or a VLM node in "
    "anomaly mode")
PASS_ANSWER = json.dumps({
    "verdict": "pass", "confidence": 0.93, "reason": "all parts present",
    "objects": [{"name": "roof", "qc": "OK", "reason": "present",
                 "bounding_box": {"x_min": 1, "y_min": 1, "x_max": 9,
                                  "y_max": 9}}]})


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


class ScriptedBedrock:
    """Answers every Converse request with one text and records it."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.answer}]}},
                "usage": {"outputTokens": 40}}


class Env:
    """One Use_Case and workflow, with the module's dispatch and Bedrock
    seams replaced by recording doubles."""

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
        self.dispatched = []
        self.bedrock = ScriptedBedrock(PASS_ANSWER)
        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client",
                            lambda region: self.bedrock)

    def put_version(self, nodes, version):
        document = {
            "schemaVersion": 1,
            "nodes": sorted((json.loads(json.dumps(n)) for n in nodes),
                            key=lambda n: n["id"]),
            "connections": [CONNECTION],
        }
        key = (f"workflows/{self.usecase_id}/{self.workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key, Body=json.dumps(
            document, sort_keys=True, indent=2).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Inspection workflow",
            "created_at": 1, "updated_at": version,
            "latest_version": version, "created_by": self.user["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": self.workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.user["user_id"],
            "validation_status": {"status": "none"},
            "compiled_arch_keys": {}, "component_arn": None})

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

    def create_session(self):
        return self.call("POST", f"{ANOMALY}/sessions",
                         body={"workflow_id": self.workflow_id,
                               "node_id": "check"})

    def seed_sample(self, session_id, label="OK"):
        """One labelled inspection sample: its objects in the Sample_Store
        and its index item built by the module's own builder."""
        execution = f"exec-{uuid.uuid4().hex[:12]}"
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/check/dev-1/"
                f"{execution}")
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        sidecar = {
            "schemaVersion": 1, "source": "live",
            "answerFormat": "inspection", "workflowId": self.workflow_id,
            "version": 1, "executionId": execution, "nodeId": "check",
            "nodeType": BEDROCK, "thingName": "dev-1",
            "exportedAt": int(time.time()),
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes), "width": 640,
                      "height": 480},
            "recorded": {"isAnomalous": False, "confidence": 0.9,
                         "verdict": "pass", "answer": PASS_ANSWER,
                         "parseError": None},
            "promptFingerprint": "sha256:baseline",
            "metadataSnippetJson": json.dumps(
                {"trigger": {"topic": "plant/line1/evaluate"}},
                separators=(",", ":")),
        }
        item = self.module.build_sample_item(
            session_id, f"dev-1/{execution}", base + ".json", sidecar, None,
            None)
        item["label"] = label
        boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME).put_item(
                Item=json.loads(json.dumps(item), parse_float=Decimal))

    def drive(self):
        steps = []
        while self.dispatched:
            assert len(steps) < 20, "the step chain did not terminate"
            steps.append(self.module.handler(self.dispatched.pop(0), None))
        return steps


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


def test_an_inspection_node_with_anomaly_mode_false_is_tuned_end_to_end(env):
    """AC-3: a session is created, scored under the inspection contract and
    applied for an inspection-format node whose ``anomaly_mode`` is false."""
    env.put_version([INSPECTION_NODE, CAMERA_NODE], 1)
    status, body = env.create_session()
    assert status == 201, body
    session_id = body["session"]["sessionId"]
    env.seed_sample(session_id)

    status, body = env.call(
        "POST", f"{ANOMALY}/sessions/{{id}}/candidates", {"id": session_id},
        body={"name": "Stricter", "maxTokens": 600,
              "prompt": "Check every ordered part; name each one."})
    assert status == 201, body
    candidate_id = body["candidate"]["candidateId"]
    status, body = env.call(
        "POST", f"{ANOMALY}/sessions/{{id}}/score-runs", {"id": session_id},
        body={"candidateId": candidate_id})
    assert status == 202, body
    run_id = body["runId"]
    env.drive()
    status, view = env.call(
        "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes", {"rid": run_id})
    assert status == 200, view
    assert view["run"]["status"] == "completed"
    (outcome,) = view["outcomes"]
    assert outcome["category"] == "correct"
    assert outcome["verdict"] == "pass"
    (kwargs,) = env.bedrock.calls
    assert kwargs["messages"][0]["content"][0]["text"].endswith(
        ai.ANOMALY_INSTRUCTION_SEPARATOR + ai.INSPECTION_JSON_INSTRUCTION)

    status, body = env.call(
        "PUT", f"{ANOMALY}/sessions/{{id}}/selection", {"id": session_id},
        body={"candidateId": candidate_id})
    assert status == 200, body
    status, body = env.call("POST", f"{ANOMALY}/sessions/{{id}}/apply",
                            {"id": session_id}, body={})
    assert status == 200, body
    assert body["newVersion"] == 2
    assert body["promptSet"]["prompt"] == (
        "Check every ordered part; name each one.")


def test_the_three_refusals_name_both_settings(env):
    """Create (400), score (400) and apply (409) refuse a node that is not
    tunable with one message naming both ways a node becomes tunable, and
    keep the ``anomaly_mode`` detail beside the new ``answer_format``."""
    env.put_version([LEGACY_OFF_NODE, CAMERA_NODE], 1)
    status, body = env.create_session()
    assert status == 400, body
    error = body["error"]
    assert error["code"] == "NODE_NOT_TUNABLE"
    assert error["message"] == NOT_TUNABLE_MESSAGE.format(version=1)
    assert error["details"]["anomaly_mode"] is False
    assert error["details"]["answer_format"] is None

    # A session that exists for the inspection node, whose latest version
    # then turned the node into a legacy node with anomaly_mode off.
    env.put_version([INSPECTION_NODE, CAMERA_NODE], 2)
    status, body = env.create_session()
    assert status == 201, body
    session_id = body["session"]["sessionId"]
    env.seed_sample(session_id)
    env.put_version([LEGACY_OFF_NODE, CAMERA_NODE], 3)

    status, body = env.call(
        "POST", f"{ANOMALY}/sessions/{{id}}/score-runs", {"id": session_id},
        body={"candidateId": "baseline"})
    assert status == 400, body
    assert body["error"]["code"] == "NODE_NOT_TUNABLE"
    assert body["error"]["message"] == NOT_TUNABLE_MESSAGE.format(version=3)
    assert body["error"]["details"]["answer_format"] is None

    run_id = str(uuid.uuid4())
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        TUNING_TABLE_NAME)
    table.put_item(Item={
        "pk": f"SESSION#{session_id}", "sk": f"RUN#{run_id}",
        "runId": run_id, "sessionId": session_id,
        "candidateId": "baseline", "status": "completed",
        "startedAt": 1, "finishedAt": 2})
    table.put_item(Item={"pk": f"RUN#{run_id}", "sk": "META",
                         "runId": run_id, "sessionId": session_id})
    status, body = env.call(
        "PUT", f"{ANOMALY}/sessions/{{id}}/selection", {"id": session_id},
        body={"candidateId": "baseline"})
    assert status == 200, body
    status, body = env.call("POST", f"{ANOMALY}/sessions/{{id}}/apply",
                            {"id": session_id}, body={})
    assert status == 409, body
    assert body["error"]["code"] == "NODE_NOT_TUNABLE"
    assert body["error"]["message"] == NOT_TUNABLE_MESSAGE.format(version=3)
    assert body["error"]["details"]["anomaly_mode"] is False
    assert body["error"]["details"]["answer_format"] is None
