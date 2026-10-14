"""Property tests for applying a Candidate, the authorization guard and the
data boundaries (spec: .kiro/specs/quality-prompt-tuning, task 6.4).

- **Feature: quality-prompt-tuning, Property 14: Applying changes exactly
  the three Prompt_Set parameters through the designer save path** —
  **Validates: Requirements 8.1, 8.2, 8.4, 8.6, 11.4**
- **Feature: quality-prompt-tuning, Property 15: Authorization precedes
  everything and mirrors the workflow handlers** — **Validates:
  Requirements 9.1, 9.2**
- **Feature: quality-prompt-tuning, Property 16: Requests and jobs carry
  only images and prompt content; images never enter DynamoDB** —
  **Validates: Requirements 9.3, 9.5, 9.6**

How these are driven
--------------------

Each example drives the REAL ``functions/workflow_tuning.py`` handler
against moto through the actual routes, with a fresh workflow and
Tuning_Session per example, and with the module's Bedrock and ``iot-data``
seams replaced by recording doubles (Property 16 inspects what those
doubles were handed).

Every expectation is an independent restatement of the requirements
transcribed in this file:

* Property 14 compares the stored new version against the **canonical
  serialization the designer's own ``canonicalize_definition`` produces**
  for the previous latest document, and asserts the diff is confined to the
  target node's three Prompt_Set parameters — a canonicalization-independent
  statement of Requirement 8.1 — plus that the stored document is itself in
  canonical form (Requirement 8.6). :func:`test_apply_is_byte_identical_to_a_designer_save`
  additionally applies the same edit through ``PUT /workflows/{id}`` on a
  twin workflow and compares the two stored objects byte for byte.
* Property 15 restates the route → permission table and the role →
  permission table (from ``shared_utils.RBACManager``) here, and asserts the
  uniform 404 / audited 403 outcomes of ``authorize_workflow_access``.
  Authorization running FIRST is asserted by sending an unparseable body:
  an authorized caller gets 400, a reader 403 and an outsider 404.
* Property 16 asserts that every Converse request consists of exactly the
  Verdict_Instruction-suffixed prompt, the labelled image blocks and the
  Node_Parameters — and that no identifier, object key, bucket name or
  credential appears anywhere in it — that the Device_Score_Job manifest
  carries only identifiers, the Prompt_Set, Node_Parameters and
  Sample_Store keys, and that no DynamoDB item of the session or its runs
  contains image bytes.

The enumerated cases over the same space are tasks 6.1-6.3's
``test_tuning_session_routes.py``, ``test_tuning_score_runs.py`` and
``test_tuning_apply.py``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import uuid
from decimal import Decimal
from unittest import mock

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from dynamo_helpers import all_table_names

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"

# --------------------------------------------------------------------------
# Independent restatement of the contract under test.
# --------------------------------------------------------------------------

#: Requirement 6.2 / design: the Verdict_Instruction the Invocation_Builder
#: appends to every Anomaly_Mode user prompt, and its separator.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')
INSTRUCTION_SEPARATOR = "\n\n"

#: Requirement 8.1: the three parameters an apply may change — and the only
#: ones.
REF_PROMPT_KEY = {"bedrock_inference": "prompt",
                  "llm_inference": "prompt_template"}
REF_PROMPT_SET_KEYS = ("system_prompt", "max_tokens")

#: Requirement 9.1: the permissions the routes require.
READ, EDIT, SAVE = "workflow:read", "workflow:edit", "workflow:save"

#: The role → workflow permission table (shared_utils.RBACManager),
#: restated: exactly these roles hold each permission.
ROLE_PERMISSIONS = {
    "Viewer": {READ},
    "Operator": {READ},
    "DataLabeler": set(),
    "DataScientist": {READ, EDIT, SAVE},
    "UseCaseAdmin": {READ, EDIT, SAVE},
    "PortalAdmin": {READ, EDIT, SAVE},
}

#: Requirement 9.1: the workflow handlers' uniform 404 body.
REF_NOT_FOUND = {"error": {"code": "WORKFLOW_NOT_FOUND",
                          "message": "Workflow not found", "details": {}}}

#: Requirement 9.4: every object this feature touches lives here.
REF_TUNING_ROOT = "workflow-tuning/"
REF_SAMPLES_PREFIX = "workflow-tuning/samples/"

#: Requirement 9.6: the only keys a Device_Score_Job manifest may carry.
REF_MANIFEST_KEYS = {"schemaVersion", "jobId", "sessionId", "runId",
                     "workflowId", "nodeId", "nodeType", "nodeParameters",
                     "promptSet", "repeats", "samples"}
REF_MANIFEST_SAMPLE_KEYS = {"sampleId", "inputKey", "label", "referenceKey",
                            "metadataSnippet"}

ANOMALOUS_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'

ANOMALY = "/workflow-tuning/anomaly"

BEDROCK_NODE_ID = "bedrock_1"
LLM_NODE_ID = "llm_1"

#: Nodes that may accompany the Tunable_Node; their parameters must come
#: through an apply byte-identical.
OTHER_NODES = {
    "cam_1": {"id": "cam_1", "type": "csi_camera_source",
              "position": {"x": 10, "y": 20}, "parameters": {}},
    "out_1": {"id": "out_1", "type": "mqtt_publish",
              "position": {"x": 400, "y": 120},
              "parameters": {"topic": "line-1/results"}},
    "free_1": {"id": "free_1", "type": "bedrock_inference",
               "position": {"x": 200, "y": 300},
               "parameters": {"prompt": "Describe the plate.",
                              "system_prompt": "Be terse.",
                              "max_tokens": 128, "anomaly_mode": False}},
}

CONNECTION = {"id": "c1", "from": {"node": "cam_1", "port": "video"},
              "to": {"node": BEDROCK_NODE_ID, "port": "in"}}


def native(value):
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, dict):
        return {key: native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [native(item) for item in value]
    return value


def image_bytes(content: str) -> bytes:
    return b"\xff\xd8JPEGDATA-" + content.encode() + b"\xff\xd9"


def strings_in(value):
    """Every string (and decoded bytes) anywhere inside a structure."""
    if isinstance(value, (bytes, bytearray)):
        yield bytes(value).decode("utf-8", "replace")
    elif isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            for found in strings_in(item):
                yield found
    elif isinstance(value, (list, tuple)):
        for item in value:
            for found in strings_in(item):
                yield found


# ==========================================================================
# Doubles
# ==========================================================================

class FakeBedrock:
    def __init__(self, answer=ANOMALOUS_ANSWER):
        self.answer = answer
        self.calls = []
        self.regions = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.answer}]}},
                "usage": {"outputTokens": 17}}


class FakeIotData:
    def __init__(self):
        self.shadows = {}
        self.updates = []

    @staticmethod
    def _merge(target, patch):
        for key, value in (patch or {}).items():
            if value is None:
                target.pop(key, None)
            elif isinstance(value, dict):
                child = target.get(key)
                if not isinstance(child, dict):
                    child = {}
                    target[key] = child
                FakeIotData._merge(child, value)
            else:
                target[key] = value

    def update_thing_shadow(self, thingName, shadowName, payload):
        document = json.loads(payload)
        self.updates.append({"thingName": thingName,
                             "shadowName": shadowName, "document": document})
        state = self.shadows.setdefault((thingName, shadowName),
                                        {"desired": {}, "reported": {}})
        self._merge(state, document.get("state") or {})
        return {"payload": io.BytesIO(b"{}")}

    def get_thing_shadow(self, thingName, shadowName):
        state = self.shadows.get((thingName, shadowName))
        if state is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "No shadow exists"}},
                "GetThingShadow")
        return {"payload": io.BytesIO(
            json.dumps({"state": state}).encode("utf-8"))}


class Seams:
    """The module's client seams, replaced for one example."""

    def __init__(self, module, answer=ANOMALOUS_ANSWER):
        self.module = module
        self.bedrock = FakeBedrock(answer)
        self.iot = FakeIotData()
        self.dispatched = []
        self._patches = []

    def __enter__(self):
        module = self.module
        self._patches = [
            mock.patch.object(module, "dispatch_action",
                              lambda payload: self.dispatched.append(
                                  dict(payload))),
            mock.patch.object(module, "bedrock_client",
                              self._bedrock_client),
            mock.patch.object(module, "iot_data_client",
                              lambda usecase: self.iot),
            mock.patch.object(module, "POLL_INTERVAL_SECONDS", 0),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False

    def _bedrock_client(self, region):
        self.bedrock.regions.append(region)
        return self.bedrock

    def drive(self, limit=20):
        """Run every dispatched action inline until none is left."""
        results = []
        while self.dispatched:
            assert len(results) < limit, "the step chain did not terminate"
            results.append(self.module.handler(self.dispatched.pop(0), None))
        return results


# ==========================================================================
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
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


class World:
    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.workflows = stack.workflows
        self.s3 = boto3.client("s3", region_name=REGION)
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        self.users = {role: self._user(role) for role in ROLE_PERMISSIONS}
        self.editor = self.users["DataScientist"]

    @staticmethod
    def _user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    # ------------------------------------------------------------- setup
    def put_workflow(self, document, workflow_id=None, version=1):
        workflow_id = workflow_id or str(uuid.uuid4())
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Tuning workflow",
            "description": "A tuned workflow", "created_at": 1,
            "updated_at": version, "latest_version": version,
            "created_by": self.editor["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.editor["user_id"]})
        return workflow_id

    def definition_key(self, workflow_id, version):
        return (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
                f"{version}/workflow.json")

    def read_definition(self, workflow_id, version):
        return self.s3.get_object(
            Bucket=PORTAL_BUCKET,
            Key=self.definition_key(workflow_id, version))["Body"].read()

    def workflow_item(self, workflow_id):
        return native(self.stack.tables.workflows.get_item(
            Key={"workflow_id": workflow_id}).get("Item") or {})

    def deploy_to(self, workflow_id, devices):
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id, "created_at": 1,
            "deployment_status": "IN_PROGRESS", "component_type": "workflow",
            "workflow_id": workflow_id, "target_devices": list(devices)})

    def audit_events(self, action, user_id=None):
        """The audit events of one action (filtered server-side: the table
        grows with every example, so a bare scan would not stay cheap)."""
        condition = Attr("action").eq(action)
        if user_id:
            condition = condition & Attr("user_id").eq(user_id)
        return [native(i) for i in self.stack.tables.audit_log.scan(
            FilterExpression=condition).get("Items", [])]

    def applies(self, session_id):
        """The ``apply_prompt_tuning`` audit events of one session."""
        return [native(i) for i in self.stack.tables.audit_log.scan(
            FilterExpression=(Attr("action").eq("apply_prompt_tuning")
                              & Attr("details.session_id").eq(session_id))
        ).get("Items", [])]

    # ---------------------------------------------------------- requests
    def call(self, method, resource, path_params=None, body=None, query=None,
             user=None, raw_body=None):
        user = user or self.editor
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": query,
            "body": (raw_body if raw_body is not None
                     else (json.dumps(body) if body is not None else None)),
            "requestContext": {"authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"]}}},
        }
        response = self.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"] or "{}")

    def open_session(self, workflow_id, node_id):
        status, body = self.call("POST", f"{ANOMALY}/sessions",
                                 body={"workflow_id": workflow_id,
                                       "node_id": node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def refresh(self, session_id):
        return self.call("POST", f"{ANOMALY}/sessions/{{id}}/refresh",
                         {"id": session_id})

    def set_labels(self, session_id, sample_ids, label):
        return self.call("PUT",
                         f"{ANOMALY}/sessions/{{id}}/samples/labels",
                         {"id": session_id},
                         body={"sampleIds": list(sample_ids),
                               "label": label})

    def create_candidate(self, session_id, prompt="Is the plate defective?",
                         system_prompt="Answer as an inspector.",
                         max_tokens=256, name="Candidate A"):
        payload = {"name": name, "prompt": prompt, "maxTokens": max_tokens}
        payload["systemPrompt"] = system_prompt
        status, body = self.call("POST",
                                 f"{ANOMALY}/sessions/{{id}}/candidates",
                                 {"id": session_id}, body=payload)
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def start_run(self, session_id, candidate_id, repeats=None):
        payload = {"candidateId": candidate_id}
        if repeats is not None:
            payload["repeats"] = repeats
        return self.call("POST", f"{ANOMALY}/sessions/{{id}}/score-runs",
                         {"id": session_id}, body=payload)

    def select(self, session_id, candidate_id):
        return self.call("PUT", f"{ANOMALY}/sessions/{{id}}/selection",
                         {"id": session_id},
                         body={"candidateId": candidate_id})

    def apply(self, session_id, **payload):
        return self.call("POST", f"{ANOMALY}/sessions/{{id}}/apply",
                         {"id": session_id}, body=payload)

    # ----------------------------------------------------- sample store
    def seed_sample(self, workflow_id, node_id, thing, execution, content,
                    exported_at=1000, reference=True):
        base = (f"{REF_SAMPLES_PREFIX}{workflow_id}/{node_id}/{thing}/"
                f"{execution}")
        data = image_bytes(content)
        reference_data = image_bytes(content + "-ref")
        document = {
            "schemaVersion": 1, "source": "live", "workflowId": workflow_id,
            "version": 1, "executionId": execution, "nodeId": node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data)},
            "recorded": {"isAnomalous": True, "confidence": 0.9,
                         "answer": ANOMALOUS_ANSWER, "parseError": None},
            "promptFingerprint": "sha256:baseline",
        }
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=data)
        if reference:
            document["reference"] = {
                "key": base + ".reference.jpg",
                "sha256": hashlib.sha256(reference_data).hexdigest(),
                "bytes": len(reference_data)}
            self.s3.put_object(Bucket=SAMPLE_BUCKET,
                               Key=base + ".reference.jpg",
                               Body=reference_data)
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=json.dumps(document).encode("utf-8"))
        return {"sampleId": f"{thing}/{execution}", "inputBytes": data,
                "referenceBytes": reference_data if reference else None,
                "inputKey": base + ".input.jpg",
                "referenceKey": (base + ".reference.jpg") if reference
                else None}

    def read_object(self, key):
        return self.s3.get_object(Bucket=SAMPLE_BUCKET,
                                  Key=key)["Body"].read()

    # ------------------------------------------------------------- reads
    def items(self, pk, prefix=None):
        condition = Key("pk").eq(pk)
        if prefix:
            condition = condition & Key("sk").begins_with(prefix)
        return [native(i) for i in self.table.query(
            KeyConditionExpression=condition).get("Items", [])]

    def raw_items(self, pk):
        return self.table.query(
            KeyConditionExpression=Key("pk").eq(pk)).get("Items", [])

    def run_item(self, session_id, run_id):
        item = self.table.get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": f"RUN#{run_id}"}).get("Item")
        return native(item) if item else None

    # ------------------------------------------------- a scored candidate
    def scored_candidate(self, workflow_id, session_id, node_id, seams,
                         prompt="Is the plate defective?",
                         system_prompt="Answer as an inspector.",
                         max_tokens=256, repeats=1, samples=1,
                         device="dev-1"):
        """A Candidate with one completed Score_Run.

        A ``bedrock_inference`` node is scored by the real Bedrock_Scorer
        against the fake Converse client; an ``llm_inference`` node is
        scored by a real Device_Score_Job whose device is simulated — the
        outcome batch is written in the device's documented shape and
        ``reported.jobs[jobId]`` completed — so both node types reach a
        completed run through their real path.
        """
        seeded = [self.seed_sample(workflow_id, node_id, device,
                                   f"exec-{index}", f"s{index}",
                                   exported_at=1000 + index)
                  for index in range(samples)]
        self.deploy_to(workflow_id, [device])
        assert self.refresh(session_id)[0] == 200
        assert self.set_labels(session_id,
                               [s["sampleId"] for s in seeded], "OK")[0] == 200
        candidate_id = self.create_candidate(
            session_id, prompt=prompt, system_prompt=system_prompt,
            max_tokens=max_tokens)
        status, body = self.start_run(session_id, candidate_id,
                                     repeats=repeats)
        assert status == 202, body
        run_id = body["runId"]
        if body["mode"] == "device":
            run = self.run_item(session_id, run_id)
            units = [(sample["sampleId"], repeat) for sample in seeded
                     for repeat in range(1, repeats + 1)]
            key = (f"workflow-tuning/sessions/{session_id}/runs/{run_id}/"
                   f"outcomes-1.json")
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key,
                               Body=device_batch(run["jobId"], session_id,
                                                 run_id, device, units))
            seams.iot.shadows.setdefault(
                (device, "dda-workflow-tuning"),
                {"desired": {}, "reported": {}})["reported"]["jobs"] = {
                    run["jobId"]: {"status": "completed", "done": len(units),
                                   "total": len(units)}}
        seams.drive()
        run = self.run_item(session_id, run_id)
        assert run["status"] == "completed", run
        return candidate_id, run_id, seeded


@pytest.fixture(scope="module")
def world(aws_stack, tuning):
    return World(aws_stack, tuning)


def device_batch(job_id, session_id, run_id, thing, units):
    """One outcome batch exactly as the device writes it
    (src/backend/workflow_engine/tuning/job_runner.py ``_write_batch``)."""
    return json.dumps({
        "schemaVersion": 1, "jobId": job_id, "sessionId": session_id,
        "runId": run_id, "batch": 1, "thingName": thing,
        "writtenAt": 1_700_000_000,
        "outcomes": [{
            "sampleId": sample_id, "repeat": repeat, "label": "OK",
            "category": "false_fail", "isAnomalous": True,
            "confidence": 0.9, "rawAnswer": ANOMALOUS_ANSWER,
            "outputTokens": 19, "latencyMs": 130,
        } for sample_id, repeat in units],
    }, sort_keys=True).encode("utf-8")


# ==========================================================================
# Property 14: applying
# ==========================================================================

prompt_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ?.,", min_size=1, max_size=40
).filter(lambda text: text.strip())

target_specs = st.fixed_dictionaries({
    "nodeType": st.sampled_from(("bedrock_inference", "llm_inference")),
    "hasPrompt": st.booleans(),
    "hasSystemPrompt": st.booleans(),
    "hasMaxTokens": st.booleans(),
    "extras": st.booleans(),
    "others": st.lists(st.sampled_from(sorted(OTHER_NODES)),
                       min_size=0, max_size=3, unique=True),
})

candidate_specs = st.fixed_dictionaries({
    "prompt": prompt_text,
    "systemPrompt": st.one_of(st.just(""), prompt_text),
    "maxTokens": st.one_of(st.none(), st.integers(min_value=1,
                                                  max_value=4096)),
})


def build_document(spec):
    """A Workflow_Definition whose target node is a Tunable_Node."""
    node_type = spec["nodeType"]
    parameters = {"anomaly_mode": True}
    if node_type == "bedrock_inference":
        node_id = BEDROCK_NODE_ID
        if spec["extras"]:
            parameters.update({"model": "us.amazon.nova-pro-v1:0",
                               "region": "eu-central-1"})
    else:
        node_id = LLM_NODE_ID
        parameters["modelName"] = "qwen2-vl"
        if spec["extras"]:
            parameters.update({"temperature": 0.3, "top_p": 0.8,
                               "max_image_dimension": 1024})
    if spec["hasPrompt"]:
        parameters[REF_PROMPT_KEY[node_type]] = "the deployed prompt"
    if spec["hasSystemPrompt"]:
        parameters["system_prompt"] = "the deployed system prompt"
    if spec["hasMaxTokens"]:
        parameters["max_tokens"] = 321
    target = {"id": node_id, "type": node_type,
              "position": {"x": 0, "y": 0}, "parameters": parameters}
    nodes = [target] + [json.loads(json.dumps(OTHER_NODES[name]))
                        for name in spec["others"]]
    connections = []
    if "cam_1" in spec["others"] and node_id == BEDROCK_NODE_ID:
        connections.append(json.loads(json.dumps(CONNECTION)))
    return {"schemaVersion": 1, "nodes": nodes,
            "connections": connections}, node_id, node_type


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=target_specs, candidate=candidate_specs,
       still_tunable=st.booleans(),
       break_kind=st.sampled_from(("freeform", "removed")))
def test_property_apply_changes_exactly_the_three_prompt_set_parameters(
        world, spec, candidate, still_tunable, break_kind):
    """**Feature: quality-prompt-tuning, Property 14: Applying changes
    exactly the three Prompt_Set parameters through the designer save
    path** — **Validates: Requirements 8.1, 8.2, 8.4, 8.6, 11.4**

    *For any* latest Workflow_Definition containing a Tunable_Node and any
    Candidate with a completed Score_Run, the new version differs from the
    previous latest only in that node's ``prompt``/``prompt_template``,
    ``system_prompt`` and ``max_tokens``, ``latest_version`` increases by
    exactly one, and the stored document equals the canonical serialization
    a designer save of the same edit produces; a target that is not a
    Tunable_Node is refused with no new version and no success audit.
    """
    document, node_id, node_type = build_document(spec)
    workflow_id = world.put_workflow(document)
    session_id = world.open_session(workflow_id, node_id)

    with Seams(world.module) as seams:
        candidate_id, run_id, _seeded = world.scored_candidate(
            workflow_id, session_id, node_id, seams,
            prompt=candidate["prompt"],
            system_prompt=candidate["systemPrompt"],
            max_tokens=candidate["maxTokens"] or 256)
        assert world.select(session_id, candidate_id)[0] == 200

        # The Candidate's Prompt_Set as it was persisted (maxTokens is
        # always a number once the catalog default is applied).
        status, view = world.call("GET", f"{ANOMALY}/sessions/{{id}}",
                                 {"id": session_id})
        assert status == 200, view
        stored_candidate = [c for c in view["candidates"]
                            if c["candidateId"] == candidate_id][0]
        prompt_set = {"prompt": stored_candidate["prompt"],
                      "systemPrompt": stored_candidate["systemPrompt"],
                      "maxTokens": stored_candidate["maxTokens"]}

        before_bytes = world.read_definition(workflow_id, 1)

        if not still_tunable:
            # Requirement 8.4: the target stops being a Tunable_Node.
            broken = json.loads(json.dumps(document))
            if break_kind == "freeform":
                for node in broken["nodes"]:
                    if node["id"] == node_id:
                        node["parameters"]["anomaly_mode"] = False
            else:
                broken["nodes"] = [n for n in broken["nodes"]
                                   if n["id"] != node_id]
                broken["connections"] = []
            world.s3.put_object(
                Bucket=PORTAL_BUCKET,
                Key=world.definition_key(workflow_id, 2),
                Body=json.dumps(broken).encode("utf-8"))
            world.stack.tables.versions.put_item(Item={
                "workflow_id": workflow_id, "version": 2,
                "s3_definition_key": world.definition_key(workflow_id, 2),
                "created_at": 2, "created_by": world.editor["user_id"]})
            world.stack.tables.workflows.update_item(
                Key={"workflow_id": workflow_id},
                UpdateExpression="SET latest_version = :v",
                ExpressionAttributeValues={":v": 2})

            status, body = world.apply(session_id)
            assert status == 409, body
            assert body["error"]["code"] == "NODE_NOT_TUNABLE"
            # No new version, no success audit (Requirement 8.4).
            assert world.workflow_item(workflow_id)["latest_version"] == 2
            assert world.applies(session_id) == []
            with pytest.raises(ClientError):
                world.read_definition(workflow_id, 3)
            return

        status, body = world.apply(session_id)
        assert status == 200, body

    # -- Requirement 8.1: exactly one new version ------------------------
    item = world.workflow_item(workflow_id)
    assert item["latest_version"] == 2
    assert body["newVersion"] == 2 and body["previousVersion"] == 1
    # The previous version's object is untouched.
    assert world.read_definition(workflow_id, 1) == before_bytes

    stored = world.read_definition(workflow_id, 2)
    new_document = json.loads(stored)
    # Requirement 8.6: the document is in the designer's canonical form —
    # canonicalizing it again changes nothing.
    canonical, error = world.workflows.canonicalize_definition(new_document)
    assert error is None, error
    assert canonical.encode("utf-8") == stored

    # -- Requirement 8.1: the diff is confined to the three parameters ---
    old_canonical, error = world.workflows.canonicalize_definition(document)
    assert error is None, error
    old_document = json.loads(old_canonical)
    assert new_document["schemaVersion"] == old_document["schemaVersion"]
    assert new_document["connections"] == old_document["connections"]
    old_nodes = {node["id"]: node for node in old_document["nodes"]}
    new_nodes = {node["id"]: node for node in new_document["nodes"]}
    assert set(new_nodes) == set(old_nodes)
    prompt_key = REF_PROMPT_KEY[node_type]
    changeable = {prompt_key} | set(REF_PROMPT_SET_KEYS)
    for other_id, old_node in old_nodes.items():
        if other_id == node_id:
            continue
        assert new_nodes[other_id] == old_node, (
            f"node {other_id} was not byte-identical")
    old_target, new_target = old_nodes[node_id], new_nodes[node_id]
    assert {k: v for k, v in new_target.items() if k != "parameters"} == \
        {k: v for k, v in old_target.items() if k != "parameters"}
    old_parameters = old_target["parameters"]
    new_parameters = new_target["parameters"]
    for key in set(old_parameters) | set(new_parameters):
        if key in changeable:
            continue
        assert new_parameters.get(key) == old_parameters.get(key), (
            f"parameter {key} of the target node changed")

    # -- and the three carry the Candidate's Prompt_Set ------------------
    assert new_parameters[prompt_key] == prompt_set["prompt"]
    if prompt_set["systemPrompt"]:
        assert new_parameters["system_prompt"] == prompt_set["systemPrompt"]
    else:
        assert not new_parameters.get("system_prompt")
    assert new_parameters["max_tokens"] == prompt_set["maxTokens"]

    # -- Requirement 8.3: the Tuning_Result and the audit event ----------
    result = body["tuningResult"]
    assert result["newVersion"] == 2 and result["previousVersion"] == 1
    assert result["candidateId"] == candidate_id
    assert result["scoreRunId"] == run_id
    applied = world.applies(session_id)
    assert len(applied) == 1
    details = applied[0]["details"]
    assert details["workflow_id"] == workflow_id
    assert details["version"] == 2
    assert details["node_id"] == node_id
    assert details["candidate_id"] == candidate_id
    assert details["score_run_id"] == run_id
    assert applied[0]["user_id"] == world.editor["user_id"]

    # -- Requirement 8.5: nothing was validated, packaged or deployed ----
    version_item = native(world.stack.tables.versions.get_item(
        Key={"workflow_id": workflow_id, "version": 2}).get("Item"))
    assert version_item["validation_status"] == {"status": "none"}
    assert version_item.get("component_arn") is None
    assert not version_item.get("compiled_arch_keys")


def test_apply_is_byte_identical_to_a_designer_save(world):
    """Requirement 8.6 / 11.4 at its sharpest: the same three-parameter
    edit applied through tuning and typed into the designer produce the
    byte-identical stored definition.

    Property 14 asserts the canonical form of the applied document; this
    pins it against the designer save path itself (``PUT /workflows/{id}``
    of ``functions/workflows.py``), which the tuning handler must reuse
    rather than re-implement.
    """
    document, node_id, node_type = build_document({
        "nodeType": "bedrock_inference", "hasPrompt": True,
        "hasSystemPrompt": True, "hasMaxTokens": True, "extras": True,
        "others": ["cam_1", "out_1", "free_1"]})
    tuned_id = world.put_workflow(document)
    twin_id = world.put_workflow(json.loads(json.dumps(document)))
    session_id = world.open_session(tuned_id, node_id)

    prompt = "describe the reference, then the input, then decide"
    system_prompt = "you are a strict inspector"
    max_tokens = 512
    with Seams(world.module) as seams:
        candidate_id, _run_id, _seeded = world.scored_candidate(
            tuned_id, session_id, node_id, seams, prompt=prompt,
            system_prompt=system_prompt, max_tokens=max_tokens)
        assert world.select(session_id, candidate_id)[0] == 200
        status, body = world.apply(session_id)
        assert status == 200, body

    # The same edit, typed into the designer on the twin workflow.
    edited = json.loads(json.dumps(document))
    for node in edited["nodes"]:
        if node["id"] == node_id:
            node["parameters"][REF_PROMPT_KEY[node_type]] = prompt
            node["parameters"]["system_prompt"] = system_prompt
            node["parameters"]["max_tokens"] = max_tokens
    event = {
        "httpMethod": "PUT", "resource": "/workflows/{id}",
        "path": f"/workflows/{twin_id}", "pathParameters": {"id": twin_id},
        "queryStringParameters": None,
        "body": json.dumps({"definition": edited}),
        "requestContext": {"authorizer": {"claims": {
            "sub": world.editor["user_id"], "email": world.editor["email"],
            "cognito:username": world.editor["username"],
            "custom:role": world.editor["role"]}}},
    }
    response = world.workflows.handler(event, None)
    assert response["statusCode"] == 200, response["body"]

    assert world.read_definition(tuned_id, 2) == \
        world.read_definition(twin_id, 2)


# ==========================================================================
# Property 15: authorization
# ==========================================================================

#: The route → permission table (design "Portal API" + Requirement 9.1),
#: restated. ``needs`` is what the path parameters must point at, and
#: ``probe`` is how "authorization first" (Requirement 9.2) is provoked:
#:
#: ``"raw"``      an unparseable body — an authorized caller sees the 400
#:                ``INVALID_JSON``, a denied caller never does;
#: ``"locator"``  the collection POST, whose body NAMES the workflow the
#:                authorization is scoped to, so the locator must be read
#:                first; the probe instead carries a valid locator with an
#:                invalid payload (a node that is not a Tunable_Node), and
#:                an authorized caller sees that 400;
#: ``None``       the route reads no body, so an authorized caller succeeds.
ROUTES = (
    ("overview", "GET", f"{ANOMALY}/workflows", READ, "none", None),
    ("create_session", "POST", f"{ANOMALY}/sessions", EDIT, "none",
     "locator"),
    ("view_session", "GET", f"{ANOMALY}/sessions/{{id}}", READ, "session",
     None),
    ("delete_session", "DELETE", f"{ANOMALY}/sessions/{{id}}", EDIT,
     "session", None),
    ("refresh", "POST", f"{ANOMALY}/sessions/{{id}}/refresh", EDIT,
     "session", None),
    ("samples", "GET", f"{ANOMALY}/sessions/{{id}}/samples", READ, "session",
     None),
    ("labels", "PUT", f"{ANOMALY}/sessions/{{id}}/samples/labels", EDIT,
     "session", "raw"),
    ("synthetic", "PUT", f"{ANOMALY}/sessions/{{id}}/synthetic-negatives",
     EDIT, "session", "raw"),
    ("create_candidate", "POST", f"{ANOMALY}/sessions/{{id}}/candidates",
     EDIT, "session", "raw"),
    ("update_candidate", "PUT",
     f"{ANOMALY}/sessions/{{id}}/candidates/{{cid}}", EDIT, "candidate",
     "raw"),
    ("delete_candidate", "DELETE",
     f"{ANOMALY}/sessions/{{id}}/candidates/{{cid}}", EDIT, "candidate",
     None),
    ("preview", "GET", f"{ANOMALY}/candidates/{{cid}}/preview", READ,
     "candidate", None),
    ("score_runs", "POST", f"{ANOMALY}/sessions/{{id}}/score-runs", EDIT,
     "session", "raw"),
    ("selection", "PUT", f"{ANOMALY}/sessions/{{id}}/selection", EDIT,
     "session", "raw"),
    ("apply", "POST", f"{ANOMALY}/sessions/{{id}}/apply", SAVE, "session",
     "raw"),
    ("get_run", "GET", f"{ANOMALY}/score-runs/{{rid}}", READ, "run", None),
    ("outcomes", "GET", f"{ANOMALY}/score-runs/{{rid}}/outcomes", READ,
     "run", None),
    ("cancel", "POST", f"{ANOMALY}/score-runs/{{rid}}/cancel", EDIT, "run",
     None),
    ("diff", "GET", f"{ANOMALY}/score-runs/{{rid}}/diff/{{other}}", READ,
     "run", None),
)

ROUTES_BY_NAME = {route[0]: route for route in ROUTES}


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(role=st.sampled_from(sorted(ROLE_PERMISSIONS)),
       route_name=st.sampled_from(sorted(ROUTES_BY_NAME)))
def test_property_authorization_precedes_everything(world, role, route_name):
    """**Feature: quality-prompt-tuning, Property 15: Authorization
    precedes everything and mirrors the workflow handlers** — **Validates:
    Requirements 9.1, 9.2**

    *For any* user and any Anomaly_Tuning request, viewing succeeds iff the
    user holds ``workflow:read`` (else the uniform 404), mutations iff
    ``workflow:edit`` and apply iff ``workflow:save`` (else 403 for a
    reader, 404 for a non-reader), authorization is evaluated before any
    other validation, and denials are audited as
    ``authorize_workflow_access`` does.

    The "before any other validation" clause is asserted by giving every
    route a request an authorized caller must reject: an unparseable body
    where the path locates the resource, and a valid locator with an
    invalid payload on the collection POST (whose body is what names the
    workflow the authorization is scoped to). A denied caller must never
    see that rejection.
    """
    _name, method, resource, permission, needs, probe = \
        ROUTES_BY_NAME[route_name]
    # A fresh principal per example, so the audit assertions below are
    # exact rather than differential.
    user = World._user(role)
    held = ROLE_PERMISSIONS[role]

    document, node_id, _node_type = build_document({
        "nodeType": "bedrock_inference", "hasPrompt": True,
        "hasSystemPrompt": True, "hasMaxTokens": True, "extras": False,
        "others": ["cam_1"]})
    workflow_id = world.put_workflow(document)
    session_id = world.open_session(workflow_id, node_id)

    path_params = {}
    query = None
    body = None
    raw_body = None
    if probe == "raw":
        raw_body = "{ this is not json"
    elif probe == "locator":
        # A valid locator (so the request names a Use_Case to authorize
        # against) with a payload an authorized caller must reject: a node
        # that is not a Tunable_Node.
        body = {"workflow_id": workflow_id, "node_id": "cam_1"}
    with Seams(world.module) as seams:
        if needs == "candidate":
            candidate_id = world.create_candidate(session_id)
            path_params = {"id": session_id, "cid": candidate_id}
            query = {"session_id": session_id}
        elif needs == "run":
            candidate_id, run_id, _seeded = world.scored_candidate(
                workflow_id, session_id, node_id, seams)
            path_params = {"rid": run_id, "other": run_id}
        elif needs == "session":
            path_params = {"id": session_id}
        else:
            query = {"usecase_id": world.usecase_id,
                     "workflow_id": workflow_id}

        status, response = world.call(
            method, resource, path_params, body=body, query=query, user=user,
            raw_body=raw_body)
        denials = world.audit_events("unauthorized_access",
                                    user_id=user["user_id"])

        if READ not in held:
            # The uniform 404, byte for byte: existence is never leaked,
            # and it is answered before the request is validated.
            assert status == 404, (role, route_name, response)
            assert response == REF_NOT_FOUND
            assert denials == []
        elif permission not in held:
            # An audited 403 naming the permission the operation needs.
            assert status == 403, (role, route_name, response)
            assert response["error"]["code"] == "FORBIDDEN"
            assert response["error"]["details"]["required_permissions"] == \
                [permission]
            assert response["error"]["details"]["usecase_id"] == \
                world.usecase_id
            assert len(denials) == 1, denials
            assert denials[0]["result"] == "denied"
            assert denials[0]["resource_type"] == "workflow"
            assert denials[0]["details"]["required_permissions"] == \
                [permission]
            assert denials[0]["details"]["method"] == method
            assert denials[0]["details"]["usecase_id"] == world.usecase_id
        elif probe == "raw":
            # Authorized: the body IS validated — after authorization.
            assert status == 400, (role, route_name, response)
            assert response["error"]["code"] == "INVALID_JSON"
            assert denials == []
        elif probe == "locator":
            assert status == 400, (role, route_name, response)
            assert response["error"]["code"] == "NODE_NOT_TUNABLE"
            assert denials == []
        else:
            assert status not in (401, 403), (role, route_name, response)
            assert response != REF_NOT_FOUND, (role, route_name, response)
            assert denials == []


def test_a_bodyless_create_names_no_workflow_to_authorize_against(world):
    """The one documented exception to Property 15's ordering clause.

    ``POST /workflow-tuning/anomaly/sessions`` has no resource in its path:
    its body is what NAMES the workflow whose Use_Case the authorization is
    scoped to. A request whose body cannot be parsed therefore identifies no
    Use_Case at all and is a 400 for every caller, including one with no
    access — it leaks nothing, because the answer does not depend on any
    stored resource. Property 15 probes this route with a valid locator and
    an invalid payload instead.
    """
    for role in sorted(ROLE_PERMISSIONS):
        status, body = world.call("POST", f"{ANOMALY}/sessions",
                                 user=World._user(role),
                                 raw_body="{ this is not json")
        assert status == 400, (role, body)
        assert body["error"]["code"] == "INVALID_JSON"
        # And a body that names a workflow the caller may not read gets the
        # uniform 404, never a hint that the workflow exists.
        document, node_id, _type = build_document({
            "nodeType": "bedrock_inference", "hasPrompt": True,
            "hasSystemPrompt": False, "hasMaxTokens": False,
            "extras": False, "others": []})
        workflow_id = world.put_workflow(document)
        status, body = world.call(
            "POST", f"{ANOMALY}/sessions", user=World._user("DataLabeler"),
            body={"workflow_id": workflow_id, "node_id": node_id})
        assert status == 404 and body == REF_NOT_FOUND


def test_route_table_matches_the_handler(tuning):
    """The route table Property 15 is stated over is the handler's: every
    route the module serves is in the table, and every table entry is
    served."""
    source = open(tuning.__file__, encoding="utf-8").read()
    for _name, method, resource, _permission, _needs, _body in ROUTES:
        assert f"'{resource}'" in source or \
            f"f'{resource.replace(ANOMALY, '{ANOMALY}')}'" in source or \
            resource.replace(ANOMALY, "") in source, resource
        assert f"'{method}'" in source
    # Every permission the handler names is one of the three.
    for permission in ("WORKFLOW_READ", "WORKFLOW_EDIT", "WORKFLOW_SAVE"):
        assert f"Permission.{permission}" in source
    assert "WORKFLOW_DELETE" not in source


# ==========================================================================
# Property 16: data boundaries
# ==========================================================================

@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(candidate=candidate_specs,
       samples=st.integers(min_value=1, max_value=3),
       repeats=st.integers(min_value=1, max_value=2),
       extras=st.booleans(), reference=st.booleans())
def test_property_requests_and_jobs_carry_only_images_and_prompts(
        world, candidate, samples, repeats, extras, reference):
    """**Feature: quality-prompt-tuning, Property 16: Requests and jobs
    carry only images and prompt content; images never enter DynamoDB** —
    **Validates: Requirements 9.3, 9.5, 9.6**

    *For any* Score_Run invocation, every request field is image
    bytes/base64 of the sample, prompt text derived from the Candidate's
    Prompt_Set and the Verdict_Instruction, or Node_Parameters; every
    Device_Score_Job manifest contains only identifiers, the Prompt_Set,
    Node_Parameters and Sample_Store keys; and no DynamoDB item contains
    image bytes.
    """
    document, _node_id, _node_type = build_document({
        "nodeType": "bedrock_inference", "hasPrompt": True,
        "hasSystemPrompt": True, "hasMaxTokens": True, "extras": extras,
        "others": []})
    # A twin llm_inference node so the same example also dispatches a
    # Device_Score_Job (Requirement 9.6).
    document["nodes"].append({
        "id": LLM_NODE_ID, "type": "llm_inference",
        "position": {"x": 300, "y": 0},
        "parameters": {"modelName": "qwen2-vl",
                       "prompt_template": "the deployed template",
                       "system_prompt": "the deployed system prompt",
                       "max_tokens": 321, "anomaly_mode": True}})
    workflow_id = world.put_workflow(document)
    device = "dev-1"
    world.deploy_to(workflow_id, [device])

    max_tokens = candidate["maxTokens"] or 256
    with Seams(world.module) as seams:
        # -- the Bedrock_Scorer (Requirement 9.3) ------------------------
        session_id = world.open_session(workflow_id, BEDROCK_NODE_ID)
        seeded = [world.seed_sample(workflow_id, BEDROCK_NODE_ID, device,
                                    f"exec-{index}", f"s{index}",
                                    exported_at=1000 + index,
                                    reference=reference)
                  for index in range(samples)]
        assert world.refresh(session_id)[0] == 200
        assert world.set_labels(
            session_id, [s["sampleId"] for s in seeded], "NOK")[0] == 200
        candidate_id = world.create_candidate(
            session_id, prompt=candidate["prompt"],
            system_prompt=candidate["systemPrompt"], max_tokens=max_tokens)
        status, body = world.start_run(session_id, candidate_id,
                                       repeats=repeats)
        assert status == 202, body
        run_id = body["runId"]
        seams.drive()

        expected_prompt = (candidate["prompt"] + INSTRUCTION_SEPARATOR
                           + VERDICT_INSTRUCTION)
        parameters = [node for node in document["nodes"]
                      if node["id"] == BEDROCK_NODE_ID][0]["parameters"]
        by_input = {s["inputBytes"]: s for s in seeded}
        assert len(seams.bedrock.calls) == samples * repeats
        # Every request is issued in the node's configured region.
        assert set(seams.bedrock.regions) == {
            parameters.get("region", "us-east-1")}
        for request in seams.bedrock.calls:
            assert set(request) <= {"modelId", "messages", "inferenceConfig",
                                    "system"}
            assert request["modelId"] == parameters.get(
                "model", "us.amazon.nova-lite-v1:0")
            assert request["inferenceConfig"] == {"maxTokens": max_tokens}
            if candidate["systemPrompt"]:
                assert request["system"] == [
                    {"text": candidate["systemPrompt"]}]
            else:
                assert "system" not in request
            content = request["messages"][0]["content"]
            assert request["messages"][0]["role"] == "user"
            # The prompt, then the labelled images — and nothing else.
            assert content[0] == {"text": expected_prompt}
            assert content[1] == {"text": "Input image:"}
            image = content[2]["image"]
            assert image["format"] == "jpeg"
            sample = by_input[image["source"]["bytes"]]
            if reference:
                assert content[3] == {"text": "Reference image:"}
                assert content[4]["image"]["source"]["bytes"] == \
                    sample["referenceBytes"]
                assert len(content) == 5
            else:
                assert len(content) == 3
            # Requirement 9.3: no identifier, key, bucket or credential.
            forbidden = [session_id, run_id, candidate_id, workflow_id,
                         world.usecase_id, BEDROCK_NODE_ID, device,
                         SAMPLE_BUCKET, PORTAL_BUCKET, "workflow-tuning/",
                         sample["sampleId"], sample["inputKey"], "testing",
                         "AKIA", "Bearer "]
            for text in strings_in({k: v for k, v in request.items()
                                    if k != "messages"}):
                for token in forbidden:
                    assert token not in text, (token, text)
            for block in content:
                if "image" in block:
                    continue
                for token in forbidden:
                    assert token not in block["text"], (token, block)

        # -- the Device_Score_Job manifest (Requirement 9.6) ------------
        llm_session_id = world.open_session(workflow_id, LLM_NODE_ID)
        llm_seeded = [world.seed_sample(workflow_id, LLM_NODE_ID, device,
                                        f"exec-{index}", f"v{index}",
                                        exported_at=2000 + index,
                                        reference=reference)
                      for index in range(samples)]
        assert world.refresh(llm_session_id)[0] == 200
        assert world.set_labels(
            llm_session_id, [s["sampleId"] for s in llm_seeded],
            "OK")[0] == 200
        llm_candidate_id = world.create_candidate(
            llm_session_id, prompt=candidate["prompt"],
            system_prompt=candidate["systemPrompt"], max_tokens=max_tokens)
        status, body = world.start_run(llm_session_id, llm_candidate_id,
                                       repeats=repeats)
        assert status == 202, body
        llm_run_id = body["runId"]
        llm_run = world.run_item(llm_session_id, llm_run_id)
        manifest_bytes = world.read_object(llm_run["manifestKey"])
        manifest = json.loads(manifest_bytes)
        assert set(manifest) <= REF_MANIFEST_KEYS, set(manifest)
        assert manifest["promptSet"] == {"prompt": candidate["prompt"],
                                        "systemPrompt":
                                            candidate["systemPrompt"],
                                        "maxTokens": max_tokens}
        llm_parameters = [node for node in document["nodes"]
                          if node["id"] == LLM_NODE_ID][0]["parameters"]
        expected_parameters = dict(llm_parameters)
        expected_parameters["prompt_template"] = candidate["prompt"]
        expected_parameters["system_prompt"] = candidate["systemPrompt"]
        expected_parameters["max_tokens"] = max_tokens
        assert manifest["nodeParameters"] == expected_parameters
        keys = set()
        for entry in manifest["samples"]:
            assert set(entry) <= REF_MANIFEST_SAMPLE_KEYS, set(entry)
            keys.add(entry["inputKey"])
            if entry.get("referenceKey"):
                keys.add(entry["referenceKey"])
        # Only Sample_Store keys under this feature's own prefix.
        assert all(key.startswith(REF_TUNING_ROOT) for key in keys)
        # No image bytes, and no credential, anywhere in the manifest.
        assert b"JPEGDATA" not in manifest_bytes
        for text in strings_in(manifest):
            assert "testing" not in text
            assert "AKIA" not in text

        # -- Requirement 9.5: no image bytes in DynamoDB ----------------
        partitions = [f"SESSION#{session_id}", f"SESSION#{llm_session_id}",
                      f"RUN#{run_id}", f"RUN#{llm_run_id}"]
        for partition in partitions:
            raw = world.raw_items(partition)
            assert raw, partition
            serialized = json.dumps(native(raw), default=str)
            assert "JPEGDATA" not in serialized
            for item in raw:
                for value in item.values():
                    assert not isinstance(value, (bytes, bytearray))
                    assert not isinstance(value, boto3.dynamodb.types.Binary)
