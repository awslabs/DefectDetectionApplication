"""
Anomaly_Tuning Score_Runs: admission, the chunked Bedrock_Scorer, the
Device_Score_Job dispatcher, the run/outcome/diff/cancel routes, selection
and the 20-runs-per-candidate prune
(spec: .kiro/specs/quality-prompt-tuning, task 6.2).

Deterministic, enumerated cases over the real ``functions/workflow_tuning.py``
handler against moto, with three doubles installed on the module's own client
seams: a recording ``dispatch_action`` (so the self-invoked execution and poll
steps are driven inline, one at a time, exactly as Lambda would), a scripted
Bedrock ``converse`` client that records every request and its concurrency, and
a fake ``iot-data`` client that implements named-shadow merge semantics
(including ``None`` deleting a key).

What is asserted here (Requirements 6.1, 6.3, 6.5-6.14, 7.1-7.5, 9.3, 9.6,
10.3, 10.4): the admission table (409 in-progress, 400 for repeats outside
1..3, 400 over the 600-invocation bound, 400 for a VLM run without an eligible
device), the planned units being exactly the OK/NOK samples × repeats, one
Converse request per unit built by the shared Invocation_Builder in the node's
region, the categorization of every outcome shape, ≤ 100 invocations per step
with ≤ 4 in flight, cursor stepping and resume without re-issuing a persisted
unit, cancellation keeping what was produced, the 60-minute finalize, the job
manifest and shadow document carrying only identifiers/prompts/keys, ingestion
exactly once, the completed/failed/silence/cancelled finalize paths, and that
no DynamoDB item ever carries image bytes.

The invariants over the same space are task 6.4's property tests (Properties
10, 11, 16); this file states expectations literally — the Verdict_Instruction
and the shadow document shape are restated here rather than imported, so a
change in the shared module or the device runner cannot move both the code and
its expectation together.
"""
import hashlib
import io
import json
import os
import sys
import threading
import time
import uuid
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
SAMPLES_PREFIX = "workflow-tuning/samples/"
JOBS_PREFIX = "workflow-tuning/jobs/"
SESSIONS_PREFIX = "workflow-tuning/sessions/"

#: Restated locally (never imported): the Verdict_Instruction the executor
#: appends to every Anomaly_Mode user prompt, and its separator.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')
INSTRUCTION_SEPARATOR = "\n\n"

#: The named shadow Device_Score_Jobs travel on, restated from the device
#: runner (src/backend/workflow_engine/tuning/job_runner.py).
TUNING_SHADOW = "dda-workflow-tuning"

ANOMALOUS_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'
NORMAL_ANSWER = '{"is_anomalous": false, "confidence": 0.8}'

BEDROCK_NODE = {
    "id": "bedrock_1",
    "type": "bedrock_inference",
    "position": {"x": 0, "y": 0},
    "parameters": {
        "model": "us.amazon.nova-lite-v1:0",
        "prompt": "Compare the plate to the reference.",
        "system_prompt": "You are a quality inspector.",
        "max_tokens": 256,
        "region": "us-west-2",
        "anomaly_mode": True,
    },
}
LLM_NODE = {
    "id": "llm_1",
    "type": "llm_inference",
    "position": {"x": 0, "y": 200},
    "parameters": {
        "modelName": "qwen2-vl",
        "prompt_template": "Inspect {trigger.payload_json.part}.",
        "max_tokens": 512,
        "temperature": 0.2,
        "anomaly_mode": True,
    },
}
PLAIN_NODE = {
    "id": "cam_1",
    "type": "camera_source",
    "position": {"x": 0, "y": 300},
    "parameters": {},
}


# ==========================================================================
# Doubles on the module's client seams
# ==========================================================================

class FakeBedrock:
    """A scripted Bedrock runtime client.

    ``script`` maps a marker contained in the request's INPUT image bytes to
    the answer text to return (or an exception instance to raise, or a
    ``(text, tokens)`` pair). Anything unscripted answers
    :data:`ANOMALOUS_ANSWER`. Every request is recorded, together with the
    peak number of concurrent calls.
    """

    def __init__(self, region, script=None, default=ANOMALOUS_ANSWER,
                 delay=0.01):
        self.region = region
        self.script = dict(script or {})
        self.default = default
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def input_bytes(kwargs):
        content = kwargs["messages"][0]["content"]
        for block in content:
            if "image" in block:
                return block["image"]["source"]["bytes"]
        return b""

    def converse(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(self.delay)
            marker = self.input_bytes(kwargs).decode("utf-8", "replace")
            answer = self.default
            for key, value in self.script.items():
                if key in marker:
                    answer = value
                    break
            if isinstance(answer, BaseException):
                raise answer
            tokens = 42
            if isinstance(answer, tuple):
                answer, tokens = answer
            response = {"output": {"message": {"content": [{"text": answer}]}}}
            if tokens is not None:
                response["usage"] = {"outputTokens": tokens}
            return response
        finally:
            with self._lock:
                self._in_flight -= 1


class FakeIotData:
    """A named-shadow store with the merge semantics the real service has:
    a nested ``None`` deletes its key."""

    def __init__(self):
        self.shadows = {}
        self.updates = []
        self.fail_update = False

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
        if self.fail_update:
            raise ClientError(
                {"Error": {"Code": "InternalFailure",
                           "Message": "shadow unavailable"}},
                "UpdateThingShadow")
        document = json.loads(payload)
        self.updates.append({"thingName": thingName,
                             "shadowName": shadowName,
                             "document": document})
        state = self.shadows.setdefault((thingName, shadowName),
                                        {"desired": {}, "reported": {}})
        self._merge(state, (document.get("state") or {}))
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

    # -- test helpers ----------------------------------------------------
    def desired_jobs(self, thing):
        state = self.shadows.get((thing, TUNING_SHADOW)) or {}
        return (state.get("desired") or {}).get("jobs") or {}

    def report(self, thing, job_id, entry):
        state = self.shadows.setdefault((thing, TUNING_SHADOW),
                                        {"desired": {}, "reported": {}})
        self._merge(state, {"reported": {"jobs": {job_id: entry}}})


# ==========================================================================
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
    """The real handler module against moto, with the tuning table and the
    Use_Case's Sample_Store bucket in place."""
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
    os.environ["WORKFLOW_TUNING_FUNCTION_NAME"] = "test-workflow-tuning-fn"
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning

    return workflow_tuning


class Env:
    """One Use_Case + workflow + session, with the module's client seams
    replaced by recording doubles."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.monkeypatch = monkeypatch
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning use case",
            "account_id": ACCOUNT_ID,
            "tuning_sample_export": True,
        })
        self.editor = self._user("DataScientist")
        self.reader = self._user("Viewer")
        self.outsider = self._user("DataLabeler")
        self.workflow_id = None
        self.node_id = None
        self.dispatched = []
        self.bedrock = None
        self.bedrock_regions = []
        self.iot = FakeIotData()

        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client", self._bedrock_client)
        monkeypatch.setattr(module, "iot_data_client",
                            lambda usecase: self.iot)
        # Poll steps never wait in tests.
        monkeypatch.setattr(module, "POLL_INTERVAL_SECONDS", 0)

    # ------------------------------------------------------------- setup
    def _user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def _bedrock_client(self, region):
        self.bedrock_regions.append(region)
        if self.bedrock is None:
            self.bedrock = FakeBedrock(region)
        self.bedrock.region = region
        return self.bedrock

    def script_bedrock(self, script=None, default=ANOMALOUS_ANSWER,
                       delay=0.01):
        self.bedrock = FakeBedrock(None, script=script, default=default,
                                   delay=delay)
        return self.bedrock

    def put_workflow(self, nodes, name="Tuning workflow", version=1,
                     workflow_id=None):
        workflow_id = workflow_id or str(uuid.uuid4())
        document = {"schemaVersion": "1.0", "nodes": nodes,
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket="test-portal-artifacts", Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID,
            "name": name,
            "created_at": 1,
            "updated_at": version,
            "latest_version": version,
            "created_by": self.editor["user_id"],
        })
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.editor["user_id"],
        })
        self.workflow_id = workflow_id
        return workflow_id

    def deploy_workflow_to(self, devices, status="IN_PROGRESS",
                           workflow_id=None):
        """Record an active deployment of the workflow to devices — the
        Portal's own record of what a device runs."""
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "deployment_status": status,
            "component_type": "workflow",
            "workflow_id": workflow_id or self.workflow_id,
            "target_devices": list(devices),
        })

    # ----------------------------------------------------- sample store
    def sample_prefix(self, node_id=None, workflow_id=None):
        return (f"{SAMPLES_PREFIX}{workflow_id or self.workflow_id}/"
                f"{node_id or self.node_id}/")

    def index_sample(self, session_id, label="OK", thing="dev-1",
                     execution=None, exported_at=1000, reference=True,
                     write_objects=True, metadata_snippet=None,
                     is_anomalous=True):
        """Write one indexed Tuning_Sample (and its image objects) directly.

        The index path itself is task 6.1's; a Score_Run only reads the
        indexed items and the image objects, so seeding them directly keeps
        these cases fast and precise.
        """
        execution = execution or f"exec-{uuid.uuid4()}"
        base = f"{self.sample_prefix()}{thing}/{execution}"
        sample_id = f"{thing}/{execution}"
        input_bytes = f"INPUT-BYTES-{execution}".encode()
        reference_bytes = f"REF-BYTES-{execution}".encode()
        if write_objects:
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                               Body=input_bytes)
            if reference:
                self.s3.put_object(Bucket=SAMPLE_BUCKET,
                                   Key=base + ".reference.jpg",
                                   Body=reference_bytes)
        sidecar = {
            "schemaVersion": 1, "source": "live",
            "workflowId": self.workflow_id, "version": 1,
            "executionId": execution, "nodeId": self.node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes)},
            "recorded": {"isAnomalous": is_anomalous, "confidence": 0.9,
                         "answer": ANOMALOUS_ANSWER, "parseError": None},
            "promptFingerprint": "sha256:baseline",
        }
        if reference:
            sidecar["reference"] = {
                "key": base + ".reference.jpg",
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                "bytes": len(reference_bytes)}
        if metadata_snippet is not None:
            sidecar["metadataSnippet"] = metadata_snippet
        item = {
            "pk": f"SESSION#{session_id}", "sk": f"SAMPLE#{sample_id}",
            "sampleId": sample_id, "sidecar": sidecar,
            "sidecarKey": base + ".json",
            "workflowId": self.workflow_id, "nodeId": self.node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "executionId": execution, "version": 1,
            "exportedAt": exported_at, "source": "live",
            "inputKey": base + ".input.jpg",
            "inputSha256": hashlib.sha256(input_bytes).hexdigest(),
            "referenceKey": (base + ".reference.jpg") if reference else None,
            "recordedIsAnomalous": is_anomalous,
            "promptFingerprint": "sha256:baseline",
            "duplicateOf": None, "differentPrompt": False,
            "synthetic": False, "indexedAt": exported_at,
        }
        if label is not None:
            item["label"] = label
        self.table().put_item(
            Item=json.loads(json.dumps(item), parse_float=Decimal))
        return {"sampleId": sample_id, "executionId": execution,
                "inputBytes": input_bytes, "base": base}

    def table(self):
        return boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)

    # ---------------------------------------------------------- invoke
    def call(self, method, resource, user, path_params=None, body=None,
             query=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": path,
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

    def session_id(self, node_id=None):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions", self.editor,
            body={"workflow_id": self.workflow_id,
                  "node_id": node_id or self.node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def candidate(self, session_id, name="Candidate A",
                  prompt="Is this plate defective?",
                  system_prompt="Answer as an inspector.", max_tokens=256):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            self.editor, {"id": session_id},
            body={"name": name, "prompt": prompt,
                  "systemPrompt": system_prompt, "maxTokens": max_tokens})
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def start_run(self, session_id, candidate_id, repeats=None, device=None,
                  user=None):
        body = {"candidateId": candidate_id}
        if repeats is not None:
            body["repeats"] = repeats
        if device is not None:
            body["deviceThingName"] = device
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            user or self.editor, {"id": session_id}, body=body)

    def get_run(self, run_id, user=None):
        return self.call("GET", "/workflow-tuning/anomaly/score-runs/{rid}",
                         user or self.editor, {"rid": run_id})

    def outcomes(self, run_id, query=None, user=None):
        return self.call(
            "GET", "/workflow-tuning/anomaly/score-runs/{rid}/outcomes",
            user or self.editor, {"rid": run_id}, query=query)

    def cancel(self, run_id, user=None):
        return self.call(
            "POST", "/workflow-tuning/anomaly/score-runs/{rid}/cancel",
            user or self.editor, {"rid": run_id})

    def diff(self, run_id, other, user=None):
        return self.call(
            "GET",
            "/workflow-tuning/anomaly/score-runs/{rid}/diff/{other}",
            user or self.editor, {"rid": run_id, "other": other})

    def select(self, session_id, candidate_id, user=None):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            user or self.editor, {"id": session_id},
            body={"candidateId": candidate_id})

    # ------------------------------------------------------------ steps
    def step(self):
        """Run the next dispatched action inline (as Lambda would)."""
        assert self.dispatched, "no action was dispatched"
        payload = self.dispatched.pop(0)
        return self.module.handler(payload, None)

    def drive(self, limit=40):
        """Run every dispatched action until none is left."""
        results = []
        while self.dispatched:
            assert len(results) < limit, "the step chain did not terminate"
            results.append(self.step())
        return results

    def run_item(self, session_id, run_id):
        return self.table().get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": f"RUN#{run_id}"}).get("Item")

    def outcome_items(self, run_id):
        return self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(f"RUN#{run_id}")
                & boto3.dynamodb.conditions.Key("sk").begins_with("OUT#")),
        ).get("Items", [])

    def partition(self, pk):
        return self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(pk)),
        ).get("Items", [])


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


@pytest.fixture
def bedrock_env(env):
    """A session on the bedrock Tunable_Node with a Candidate."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, PLAIN_NODE])
    env.node_id = BEDROCK_NODE["id"]
    env.session = env.session_id()
    env.candidate_id = env.candidate(env.session)
    return env


@pytest.fixture
def llm_env(env):
    """A session on the llm Tunable_Node with a Candidate."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, PLAIN_NODE])
    env.node_id = LLM_NODE["id"]
    env.session = env.session_id(LLM_NODE["id"])
    env.candidate_id = env.candidate(env.session)
    return env


# ==========================================================================
# 1. Admission (Requirements 6.6, 6.7, 6.10, 6.13, 4.3)
# ==========================================================================

class TestAdmission:

    def test_202_with_planned_invocations_and_dispatch(self, bedrock_env):
        for _ in range(3):
            bedrock_env.index_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=2)
        assert status == 202, body
        assert body["plannedInvocations"] == 6
        assert body["samples"] == 3 and body["repeats"] == 2
        assert body["mode"] == "bedrock"
        run_id = body["runId"]
        item = bedrock_env.run_item(bedrock_env.session, run_id)
        assert item["status"] == "running"
        assert int(item["plannedInvocations"]) == 6
        assert len(item["plannedSamples"]) == 3
        # The by-id pointer the .../score-runs/{rid} routes resolve through.
        pointer = bedrock_env.table().get_item(
            Key={"pk": f"RUN#{run_id}", "sk": "META"}).get("Item")
        assert pointer["sessionId"] == bedrock_env.session
        assert bedrock_env.dispatched == [{
            "action": "execute_score_run", "run_id": run_id,
            "session_id": bedrock_env.session, "cursor": 0}]

    def test_plan_is_exactly_the_ok_and_nok_samples(self, bedrock_env):
        ok = bedrock_env.index_sample(bedrock_env.session, label="OK")
        nok = bedrock_env.index_sample(bedrock_env.session, label="NOK")
        bedrock_env.index_sample(bedrock_env.session, label="EXCLUDE")
        bedrock_env.index_sample(bedrock_env.session, label=None)
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 202, body
        item = bedrock_env.run_item(bedrock_env.session, body["runId"])
        planned = {e["sampleId"] for e in item["plannedSamples"]}
        assert planned == {ok["sampleId"], nok["sampleId"]}
        assert int(item["plannedInvocations"]) == 2

    @pytest.mark.parametrize("repeats", [0, 4, 7, -1, "two", 1.5, True])
    def test_repeats_outside_1_to_3_is_400(self, bedrock_env, repeats):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=repeats)
        assert status == 400, body
        assert body["error"]["code"] == "INVALID_REPEATS"
        assert not bedrock_env.dispatched

    @pytest.mark.parametrize("repeats", [1, 2, 3])
    def test_repeats_1_to_3_are_accepted(self, bedrock_env, repeats):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=repeats)
        assert status == 202, body
        assert body["plannedInvocations"] == repeats

    def test_repeats_default_to_one(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert body["repeats"] == 1 and body["plannedInvocations"] == 1

    def test_over_600_planned_invocations_is_400(self, bedrock_env):
        # 201 samples x 3 repeats = 603 > 600. No image objects are written:
        # the bound is enforced before any invocation is issued.
        for index in range(201):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"e{index:04d}",
                                     write_objects=False)
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=3)
        assert status == 400, body
        assert body["error"]["code"] == "RUN_TOO_LARGE"
        assert body["error"]["details"]["plannedInvocations"] == 603
        assert body["error"]["details"]["bound"] == 600
        assert not bedrock_env.dispatched
        # ... and the same samples at repeats = 2 (402) are admitted.
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=2)
        assert status == 202, body
        assert body["plannedInvocations"] == 402

    def test_no_labelled_samples_is_400(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label=None)
        bedrock_env.index_sample(bedrock_env.session, label="EXCLUDE")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_LABELLED_SAMPLES"

    def test_second_run_is_409_naming_the_first(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        _status, first = bedrock_env.start_run(bedrock_env.session,
                                              bedrock_env.candidate_id)
        bedrock_env.dispatched.clear()
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 409, body
        assert body["error"]["code"] == "RUN_IN_PROGRESS"
        assert body["error"]["details"]["runId"] == first["runId"]
        assert first["runId"] in body["error"]["message"]
        assert not bedrock_env.dispatched

    def test_a_finished_run_frees_the_slot(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        _status, first = bedrock_env.start_run(bedrock_env.session,
                                              bedrock_env.candidate_id)
        bedrock_env.script_bedrock()
        bedrock_env.drive()
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 202, body
        assert body["runId"] != first["runId"]

    def test_a_stale_run_is_taken_over_and_finalized_failed(self,
                                                            bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        _status, first = bedrock_env.start_run(bedrock_env.session,
                                              bedrock_env.candidate_id)
        bedrock_env.dispatched.clear()
        # Age the run past the 60-minute budget (Requirement 10.4).
        bedrock_env.table().update_item(
            Key={"pk": f"SESSION#{bedrock_env.session}",
                 "sk": f"RUN#{first['runId']}"},
            UpdateExpression="SET startedAt = :t",
            ExpressionAttributeValues={":t": int(time.time()) - 4000})
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 202, body
        stale = bedrock_env.run_item(bedrock_env.session, first["runId"])
        assert stale["status"] == "failed"
        assert "60 minutes" in str(stale["error"])
        assert stale["summary"]["invocations"] == 0

    def test_unknown_candidate_is_404(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            "does-not-exist")
        assert status == 404
        assert body["error"]["code"] == "CANDIDATE_NOT_FOUND"

    def test_missing_candidate_id_is_400(self, bedrock_env):
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            bedrock_env.editor, {"id": bedrock_env.session}, body={})
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_node_no_longer_tunable_is_400(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        # A new version in which the node left Anomaly_Mode.
        freeform = json.loads(json.dumps(BEDROCK_NODE))
        freeform["parameters"]["anomaly_mode"] = False
        bedrock_env.put_workflow([freeform, PLAIN_NODE], version=2,
                                 workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"

    def test_reader_gets_403_and_outsider_the_uniform_404(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        bedrock_env.stack.tables.user_roles.put_item(Item={
            "user_id": bedrock_env.reader["user_id"],
            "usecase_id": bedrock_env.usecase_id, "role": "Viewer"})
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id,
            user=bedrock_env.reader)
        assert status == 403, body
        assert body["error"]["code"] == "FORBIDDEN"
        status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id,
            user=bedrock_env.outsider)
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_authorization_precedes_validation(self, bedrock_env):
        """A reader's invalid request is a 403, not a 400."""
        bedrock_env.stack.tables.user_roles.put_item(Item={
            "user_id": bedrock_env.reader["user_id"],
            "usecase_id": bedrock_env.usecase_id, "role": "Viewer"})
        status, body = bedrock_env.start_run(
            bedrock_env.session, "nope", repeats=99,
            user=bedrock_env.reader)
        assert status == 403, body

    def test_authorization_precedes_even_body_parsing(self, bedrock_env):
        """Requirement 9.2: authorization is evaluated before ANY other
        validation — an unparseable body from a reader is still a 403."""
        bedrock_env.stack.tables.user_roles.put_item(Item={
            "user_id": bedrock_env.reader["user_id"],
            "usecase_id": bedrock_env.usecase_id, "role": "Viewer"})
        event = {
            "httpMethod": "POST",
            "resource": "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            "path": f"/workflow-tuning/anomaly/sessions/"
                    f"{bedrock_env.session}/score-runs",
            "pathParameters": {"id": bedrock_env.session},
            "body": "{not json",
            "requestContext": {"authorizer": {"claims": {
                "sub": bedrock_env.reader["user_id"],
                "email": bedrock_env.reader["email"],
                "cognito:username": bedrock_env.reader["username"],
                "custom:role": bedrock_env.reader["role"]}}},
        }
        response = bedrock_env.module.handler(event, None)
        assert response["statusCode"] == 403
        assert json.loads(response["body"])["error"]["code"] == "FORBIDDEN"


# ==========================================================================
# 2. VLM admission: device eligibility (Requirement 6.9)
# ==========================================================================

class TestDeviceEligibility:

    def test_without_an_eligible_device_the_run_is_refused(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.index_sample(llm_env.session, label="NOK", thing="dev-2")
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_ELIGIBLE_DEVICE"
        details = body["error"]["details"]
        assert details["exported"] == ["dev-1", "dev-2"]
        assert details["eligible"] == []
        # The devices that exported samples but do not report the workflow.
        assert details["ineligible"] == ["dev-1", "dev-2"]
        assert not llm_env.dispatched

    def test_single_eligible_device_is_used(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.index_sample(llm_env.session, label="NOK", thing="dev-2")
        llm_env.deploy_workflow_to(["dev-2"])
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 202, body
        assert body["deviceThingName"] == "dev-2"
        assert body["mode"] == "device"

    def test_more_than_one_eligible_device_requires_a_choice(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.index_sample(llm_env.session, label="NOK", thing="dev-2")
        llm_env.deploy_workflow_to(["dev-1", "dev-2"])
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "DEVICE_REQUIRED"
        assert body["error"]["details"]["eligible"] == ["dev-1", "dev-2"]
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id,
                                        device="dev-1")
        assert status == 202, body
        assert body["deviceThingName"] == "dev-1"

    def test_a_device_that_never_exported_is_not_eligible(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.deploy_workflow_to(["dev-1", "dev-9"])
        status, body = llm_env.start_run(llm_env.session,
                                         llm_env.candidate_id,
                                         device="dev-9")
        assert status == 400, body
        assert body["error"]["code"] == "DEVICE_NOT_ELIGIBLE"
        assert body["error"]["details"]["eligible"] == ["dev-1"]

    def test_an_inactive_deployment_does_not_make_a_device_eligible(
            self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.deploy_workflow_to(["dev-1"], status="FAILED")
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_ELIGIBLE_DEVICE"

    def test_a_deployment_of_another_workflow_does_not_count(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.deploy_workflow_to(["dev-1"], workflow_id="another-workflow")
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_ELIGIBLE_DEVICE"

    def test_the_packaged_component_name_also_registers_a_device(
            self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": llm_env.usecase_id, "created_at": 1,
            "deployment_status": "COMPLETED",
            "components": [{"component_name":
                            f"dda.workflow.{llm_env.workflow_id}"}],
            "target_devices": ["dev-1"]})
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 202, body
        assert body["deviceThingName"] == "dev-1"


# ==========================================================================
# 3. The Bedrock_Scorer (Requirements 6.1, 6.3, 6.5, 6.8, 6.13)
# ==========================================================================

class TestBedrockScorer:

    def test_one_request_per_sample_per_repeat_built_by_the_builder(
            self, bedrock_env):
        sample = bedrock_env.index_sample(bedrock_env.session, label="NOK")
        bedrock_env.script_bedrock()
        _status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=2)
        bedrock_env.drive()
        calls = bedrock_env.bedrock.calls
        assert len(calls) == 2
        request = calls[0]
        assert request["modelId"] == "us.amazon.nova-lite-v1:0"
        assert request["inferenceConfig"] == {"maxTokens": 256}
        assert request["system"] == [{"text": "Answer as an inspector."}]
        content = request["messages"][0]["content"]
        # prompt + blank line + the Verdict_Instruction, then the labelled
        # input image and the labelled reference image, in that order.
        assert content[0] == {"text": "Is this plate defective?"
                                      + INSTRUCTION_SEPARATOR
                                      + VERDICT_INSTRUCTION}
        assert content[1] == {"text": "Input image:"}
        assert content[2]["image"]["format"] == "jpeg"
        assert content[2]["image"]["source"]["bytes"] == sample["inputBytes"]
        assert content[3] == {"text": "Reference image:"}
        assert content[4]["image"]["source"]["bytes"].startswith(b"REF-BYTES")
        # The node's configured region, not the Lambda's.
        assert bedrock_env.bedrock_regions == ["us-west-2"]
        assert body["plannedInvocations"] == 2

    def test_single_image_sample_sends_only_the_input(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK",
                                 reference=False)
        bedrock_env.script_bedrock()
        bedrock_env.start_run(bedrock_env.session, bedrock_env.candidate_id)
        bedrock_env.drive()
        content = bedrock_env.bedrock.calls[0]["messages"][0]["content"]
        assert [block for block in content if "image" in block].__len__() == 1
        assert not [b for b in content if b.get("text") == "Reference image:"]

    def test_every_outcome_category(self, bedrock_env):
        ok_correct = bedrock_env.index_sample(bedrock_env.session,
                                             label="OK", execution="cat1")
        nok_correct = bedrock_env.index_sample(bedrock_env.session,
                                              label="NOK", execution="cat2")
        false_pass = bedrock_env.index_sample(bedrock_env.session,
                                              label="NOK", execution="cat3")
        false_fail = bedrock_env.index_sample(bedrock_env.session,
                                              label="OK", execution="cat4")
        unparseable = bedrock_env.index_sample(bedrock_env.session,
                                               label="OK", execution="cat5")
        raising = bedrock_env.index_sample(bedrock_env.session,
                                           label="OK", execution="cat6")
        bedrock_env.script_bedrock(script={
            "cat1": NORMAL_ANSWER,
            "cat2": ANOMALOUS_ANSWER,
            "cat3": NORMAL_ANSWER,
            "cat4": ANOMALOUS_ANSWER,
            "cat5": "I am not sure, sorry.",
            "cat6": RuntimeError("ThrottlingException: slow down"),
        })
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        status, view = bedrock_env.outcomes(body["runId"])
        assert status == 200, view
        by_sample = {o["sampleId"]: o for o in view["outcomes"]}
        assert by_sample[ok_correct["sampleId"]]["category"] == "correct"
        assert by_sample[nok_correct["sampleId"]]["category"] == "correct"
        assert by_sample[false_pass["sampleId"]]["category"] == "false_pass"
        assert by_sample[false_fail["sampleId"]]["category"] == "false_fail"
        parse = by_sample[unparseable["sampleId"]]
        assert parse["category"] == "parse_failure"
        # The raw answer character-for-character and the parser's reason.
        assert parse["rawAnswer"] == "I am not sure, sorry."
        assert "is_anomalous" in parse["parseError"]
        error = by_sample[raising["sampleId"]]
        assert error["category"] == "invocation_error"
        assert "ThrottlingException" in error["error"]
        # ... and the run continued past the failing invocation.
        assert view["run"]["status"] == "completed"
        assert view["summary"]["invocations"] == 6
        assert view["summary"]["correct"] == 2
        assert view["summary"]["falsePass"] == 1
        assert view["summary"]["falseFail"] == 1
        assert view["summary"]["parseFailure"] == 1
        assert view["summary"]["invocationError"] == 1

    def test_outcome_records_tokens_confidence_and_latency(self,
                                                           bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="NOK")
        bedrock_env.script_bedrock(default=(ANOMALOUS_ANSWER, 137))
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        _status, view = bedrock_env.outcomes(body["runId"])
        outcome = view["outcomes"][0]
        assert outcome["isAnomalous"] is True
        assert outcome["confidence"] == 0.9
        assert outcome["outputTokens"] == 137
        assert outcome["latencyMs"] >= 0
        assert outcome["rawAnswer"] == ANOMALOUS_ANSWER
        assert view["summary"]["maxOutputTokens"] == 137

    def test_a_step_issues_at_most_100_invocations_with_4_in_flight(
            self, bedrock_env):
        for index in range(120):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"s{index:04d}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER, delay=0.002)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        first = bedrock_env.step()
        assert first["issued"] == 100
        assert len(bedrock_env.bedrock.calls) == 100
        assert bedrock_env.bedrock.max_in_flight <= 4
        assert bedrock_env.bedrock.max_in_flight > 1
        item = bedrock_env.run_item(bedrock_env.session, run_id)
        assert int(item["cursor"]) == 100
        assert int(item["done"]) == 100
        assert item["status"] == "running"
        # The step re-invoked itself with the next cursor.
        assert bedrock_env.dispatched == [{
            "action": "execute_score_run", "run_id": run_id,
            "session_id": bedrock_env.session, "cursor": 100}]
        second = bedrock_env.step()
        assert second["issued"] == 20
        assert len(bedrock_env.bedrock.calls) == 120
        assert not bedrock_env.dispatched
        item = bedrock_env.run_item(bedrock_env.session, run_id)
        assert item["status"] == "completed"
        assert int(item["done"]) == 120
        assert item["summary"]["invocations"] == 120

    def test_resume_never_reissues_a_persisted_unit(self, bedrock_env):
        for index in range(3):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"r{index}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=2)
        run_id = body["runId"]
        bedrock_env.step()
        assert len(bedrock_env.bedrock.calls) == 6
        # An interrupted step is retried from cursor 0: every unit is
        # already persisted, so nothing is re-issued.
        bedrock_env.dispatched.clear()
        result = bedrock_env.module.handler(
            {"action": "execute_score_run", "run_id": run_id,
             "session_id": bedrock_env.session, "cursor": 0}, None)
        assert len(bedrock_env.bedrock.calls) == 6
        assert result["status"] == "completed"
        assert len(bedrock_env.outcome_items(run_id)) == 6

    def test_a_step_resumes_from_the_last_persisted_outcome(self,
                                                            bedrock_env):
        for index in range(4):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"p{index}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        # Simulate a step that persisted two units and then died before it
        # could advance the cursor or re-invoke.
        plan = bedrock_env.run_item(bedrock_env.session, run_id)[
            "plannedSamples"]
        for entry in plan[:2]:
            bedrock_env.table().put_item(Item={
                "pk": f"RUN#{run_id}", "sk": f"OUT#{entry['sampleId']}#1",
                "runId": run_id, "sessionId": bedrock_env.session,
                "sampleId": entry["sampleId"], "repeat": 1,
                "label": "OK", "category": "correct", "isAnomalous": False})
        bedrock_env.dispatched.clear()
        bedrock_env.module.handler(
            {"action": "execute_score_run", "run_id": run_id,
             "session_id": bedrock_env.session, "cursor": 0}, None)
        assert len(bedrock_env.bedrock.calls) == 2
        sent = {FakeBedrock.input_bytes(c).decode() for c
                in bedrock_env.bedrock.calls}
        assert not any(entry["sampleId"].split("/")[1] in marker
                       for entry in plan[:2] for marker in sent)
        assert len(bedrock_env.outcome_items(run_id)) == 4

    def test_cancellation_stops_the_run_and_keeps_its_outcomes(
            self, bedrock_env):
        for index in range(150):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"c{index:04d}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER, delay=0.001)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        bedrock_env.step()
        assert len(bedrock_env.bedrock.calls) == 100
        status, cancelled = bedrock_env.cancel(run_id)
        assert status == 200, cancelled
        assert cancelled["run"]["status"] == "cancelled"
        assert cancelled["run"]["summary"]["invocations"] == 100
        # The queued next step issues nothing.
        bedrock_env.step()
        assert len(bedrock_env.bedrock.calls) == 100
        assert len(bedrock_env.outcome_items(run_id)) == 100
        assert not bedrock_env.dispatched

    def test_the_60_minute_budget_finalizes_a_resumed_run_as_failed(
            self, bedrock_env):
        for index in range(3):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"t{index}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        bedrock_env.table().update_item(
            Key={"pk": f"SESSION#{bedrock_env.session}",
                 "sk": f"RUN#{run_id}"},
            UpdateExpression="SET startedAt = :t",
            ExpressionAttributeValues={":t": int(time.time()) - 3601})
        result = bedrock_env.step()
        assert result["status"] == "failed"
        assert not bedrock_env.bedrock.calls
        item = bedrock_env.run_item(bedrock_env.session, run_id)
        assert "60 minutes" in item["error"]
        assert item["summary"]["invocations"] == 0

    def test_an_unreadable_sample_object_is_one_invocation_error(
            self, bedrock_env):
        good = bedrock_env.index_sample(bedrock_env.session, label="OK",
                                        execution="good")
        missing = bedrock_env.index_sample(bedrock_env.session, label="OK",
                                           execution="gone",
                                           write_objects=False)
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        _status, view = bedrock_env.outcomes(body["runId"])
        by_sample = {o["sampleId"]: o for o in view["outcomes"]}
        assert by_sample[good["sampleId"]]["category"] == "correct"
        broken = by_sample[missing["sampleId"]]
        assert broken["category"] == "invocation_error"
        assert missing["base"] + ".input.jpg" in broken["error"]
        # Only the readable sample reached Bedrock.
        assert len(bedrock_env.bedrock.calls) == 1

    def test_no_dynamodb_item_carries_image_bytes(self, bedrock_env):
        sample = bedrock_env.index_sample(bedrock_env.session, label="OK")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        marker = sample["inputBytes"].decode()
        items = (bedrock_env.partition(f"SESSION#{bedrock_env.session}")
                 + bedrock_env.partition(f"RUN#{body['runId']}"))
        for item in items:
            serialized = json.dumps(item, default=str)
            assert marker not in serialized
            assert "REF-BYTES" not in serialized

    def test_the_request_carries_only_prompt_text_and_image_bytes(
            self, bedrock_env):
        """Requirement 9.3: no credentials, object keys or identifiers."""
        sample = bedrock_env.index_sample(bedrock_env.session, label="OK")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        request = bedrock_env.bedrock.calls[0]
        serialized = json.dumps(request, default=lambda o: "<bytes>")
        for forbidden in (sample["sampleId"], body["runId"],
                          bedrock_env.session, bedrock_env.workflow_id,
                          SAMPLE_BUCKET, "workflow-tuning/"):
            assert forbidden not in serialized
        assert set(request) == {"modelId", "messages", "inferenceConfig",
                                "system"}

    def test_the_real_client_has_the_executors_timeout_and_no_retries(
            self, tuning):
        """Requirement 6.3: the node's region and the executor's transport
        settings. The scorer's client seam itself, not the double the other
        cases install.

        ``retries={'max_attempts': 1}`` is the value the executor's
        ``_default_bedrock_invoker`` passes, which botocore normalizes to
        ``{'total_max_attempts': 2, 'mode': 'legacy'}``; asserting the
        normalized form pins that the replay's transport is configured
        exactly like the deployed node's.
        """
        client = tuning.bedrock_client("us-west-2")
        assert client.meta.region_name == "us-west-2"
        assert client.meta.config.read_timeout == 30
        assert client.meta.config.retries == {"total_max_attempts": 2,
                                              "mode": "legacy"}
        assert client.meta.service_model.service_name == "bedrock-runtime"


# ==========================================================================
# 4. Device_Score_Job dispatch, ingestion and finalize
#    (Requirements 6.9, 6.11, 6.12, 9.6)
# ==========================================================================

def outcome_batch(run_id, session_id, job_id, index, outcomes,
                  thing="dev-1"):
    """One outcome batch object exactly as the device writes it."""
    return {
        "schemaVersion": 1, "jobId": job_id, "sessionId": session_id,
        "runId": run_id, "batch": index, "thingName": thing,
        "writtenAt": 1, "outcomes": outcomes,
    }


def _device_job_manifest():
    """The device runner's real ``JobManifest``, or None when the device
    tree is not importable in this environment."""
    here = os.path.dirname(os.path.abspath(__file__))
    device_backend = os.path.abspath(
        os.path.join(here, "..", "..", "..", "src", "backend"))
    if device_backend not in sys.path:
        sys.path.append(device_backend)
    try:
        from workflow_engine.tuning.job_runner import JobManifest
    except Exception:                                   # pragma: no cover
        return None
    return JobManifest


DEVICE_JOB_MANIFEST = _device_job_manifest()


class TestDeviceScoreJob:

    @staticmethod
    def dispatch(llm_env, samples=2, repeats=1, labels=("OK", "NOK")):
        seeded = []
        for index in range(samples):
            seeded.append(llm_env.index_sample(
                llm_env.session, label=labels[index % len(labels)],
                thing="dev-1", execution=f"j{index}",
                metadata_snippet={"trigger": {"payload_json": {
                    "part": f"P-{index}"}}}))
        llm_env.deploy_workflow_to(["dev-1"])
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id,
                                        repeats=repeats)
        assert status == 202, body
        return body, seeded

    def test_manifest_and_shadow_document(self, llm_env):
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        item = llm_env.run_item(llm_env.session, run_id)
        job_id = item["jobId"]
        assert item["mode"] == "device"
        assert item["deviceThingName"] == "dev-1"
        key = f"{JOBS_PREFIX}{job_id}/manifest.json"
        assert item["manifestKey"] == key
        manifest = json.loads(llm_env.s3.get_object(
            Bucket=SAMPLE_BUCKET, Key=key)["Body"].read())
        assert manifest["jobId"] == job_id
        assert manifest["sessionId"] == llm_env.session
        assert manifest["runId"] == run_id
        assert manifest["workflowId"] == llm_env.workflow_id
        assert manifest["nodeId"] == "llm_1"
        assert manifest["repeats"] == 1
        assert manifest["promptSet"] == {
            "prompt": "Is this plate defective?",
            "systemPrompt": "Answer as an inspector.", "maxTokens": 256}
        # Node_Parameters with the Candidate's Prompt_Set laid over them.
        assert manifest["nodeParameters"]["modelName"] == "qwen2-vl"
        assert manifest["nodeParameters"]["temperature"] == 0.2
        assert manifest["nodeParameters"]["prompt_template"] == \
            "Is this plate defective?"
        assert manifest["nodeParameters"]["max_tokens"] == 256
        entry = [s for s in manifest["samples"]
                 if s["sampleId"] == seeded[0]["sampleId"]][0]
        assert entry["inputKey"] == seeded[0]["base"] + ".input.jpg"
        assert entry["referenceKey"] == seeded[0]["base"] + ".reference.jpg"
        assert entry["label"] == "OK"
        assert entry["metadataSnippet"] == {
            "trigger": {"payload_json": {"part": "P-0"}}}
        # Identifiers, the Prompt_Set, Node_Parameters and keys only: no
        # image bytes, no presigned URL, no credential (Requirement 9.6).
        serialized = json.dumps(manifest)
        assert "INPUT-BYTES" not in serialized
        assert "X-Amz-Signature" not in serialized
        assert "sha256" not in serialized

        # desired.jobs[jobId] = {manifestKey, cancel} on the named shadow.
        assert llm_env.iot.updates[0]["thingName"] == "dev-1"
        assert llm_env.iot.updates[0]["shadowName"] == TUNING_SHADOW
        assert llm_env.iot.desired_jobs("dev-1") == {
            job_id: {"manifestKey": key, "cancel": False}}
        # ... and the poll step was dispatched.
        assert llm_env.dispatched[0]["action"] == "poll_score_job"

    def test_the_manifest_parses_on_the_device(self, llm_env):
        """Cross-surface: the device's own manifest parser accepts it."""
        if DEVICE_JOB_MANIFEST is None:
            pytest.skip("the device backend is not importable here")
        body, _seeded = self.dispatch(llm_env, samples=3, repeats=2)
        item = llm_env.run_item(llm_env.session, body["runId"])
        manifest = json.loads(llm_env.s3.get_object(
            Bucket=SAMPLE_BUCKET, Key=item["manifestKey"])["Body"].read())
        parsed = DEVICE_JOB_MANIFEST.from_document(manifest, item["jobId"])
        assert parsed is not None
        assert parsed.session_id == llm_env.session
        assert parsed.run_id == body["runId"]
        assert parsed.repeats == 2
        assert parsed.unusable == 0
        assert parsed.total == 6
        assert parsed.node_parameters["modelName"] == "qwen2-vl"

    def test_ingests_batches_exactly_once_and_finalizes_completed(
            self, llm_env):
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        item = llm_env.run_item(llm_env.session, run_id)
        job_id = item["jobId"]
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-1.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "correct", "isAnomalous": False,
                 "confidence": 0.7, "rawAnswer": NORMAL_ANSWER,
                 "outputTokens": 21, "latencyMs": 900},
            ])).encode())
        llm_env.iot.report("dev-1", job_id,
                           {"status": "running", "done": 1, "total": 2,
                            "updatedAt": int(time.time())})
        result = llm_env.step()
        assert result["status"] == "running"
        assert result["ingested"]["batches"] == 1
        assert result["ingested"]["outcomes"] == 1
        assert len(llm_env.outcome_items(run_id)) == 1

        # A poll that sees no new batch ingests nothing again.
        result = llm_env.step()
        assert result["ingested"]["batches"] == 0
        assert len(llm_env.outcome_items(run_id)) == 1

        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-2.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 2, [
                {"sampleId": seeded[1]["sampleId"], "repeat": 1,
                 "label": "NOK", "category": "correct", "isAnomalous": True,
                 "confidence": 0.95, "rawAnswer": ANOMALOUS_ANSWER,
                 "outputTokens": 25, "latencyMs": 1100},
            ])).encode())
        llm_env.iot.report("dev-1", job_id,
                           {"status": "completed", "done": 2, "total": 2,
                            "updatedAt": int(time.time())})
        result = llm_env.step()
        assert result["status"] == "completed"
        item = llm_env.run_item(llm_env.session, run_id)
        assert item["status"] == "completed"
        assert int(item["done"]) == 2
        assert item["summary"]["invocations"] == 2
        assert item["summary"]["correct"] == 2
        assert item["summary"]["accuracy"] == 1
        # The job is removed from desired on finalize, and polling stops.
        assert llm_env.iot.desired_jobs("dev-1") == {}
        assert not llm_env.dispatched

    def test_a_reingested_batch_never_duplicates_or_overwrites_an_outcome(
            self, llm_env):
        """Requirement 6.12 / Property 11: every batch is ingested exactly
        once — a batch object read a second time (a poll step that died
        after writing its outcomes but before recording the key, or a
        device that rewrote the object) adds nothing and changes nothing."""
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        job_id = llm_env.run_item(llm_env.session, run_id)["jobId"]
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        key = prefix + "outcomes-1.json"
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=key,
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "correct", "isAnomalous": False,
                 "rawAnswer": NORMAL_ANSWER},
            ])).encode())
        result = llm_env.step()
        assert result["ingested"]["outcomes"] == 1
        assert len(llm_env.outcome_items(run_id)) == 1

        # The same batch object, rewritten with a different verdict for the
        # same (sample, repeat), and the ingestion pointer forgotten.
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=key,
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "false_fail", "isAnomalous": True,
                 "rawAnswer": ANOMALOUS_ANSWER},
            ])).encode())
        llm_env.table().update_item(
            Key={"pk": f"SESSION#{llm_env.session}", "sk": f"RUN#{run_id}"},
            UpdateExpression="SET ingestedBatches = :b",
            ExpressionAttributeValues={":b": []})
        result = llm_env.step()
        assert result["ingested"]["batches"] == 1
        assert result["ingested"]["outcomes"] == 0
        items = llm_env.outcome_items(run_id)
        assert len(items) == 1
        assert items[0]["category"] == "correct"
        assert items[0]["rawAnswer"] == NORMAL_ANSWER

    def test_a_reported_failure_finalizes_the_run_failed(self, llm_env):
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        job_id = llm_env.run_item(llm_env.session, run_id)["jobId"]
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-1.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "correct", "isAnomalous": False},
            ])).encode())
        llm_env.iot.report("dev-1", job_id,
                           {"status": "failed", "done": 1, "total": 2,
                            "error": "could not persist outcome batch 2",
                            "updatedAt": int(time.time())})
        result = llm_env.step()
        assert result["status"] == "failed"
        item = llm_env.run_item(llm_env.session, run_id)
        assert "could not persist outcome batch 2" in item["error"]
        # The partial Score_Summary is kept.
        assert item["summary"]["invocations"] == 1
        assert llm_env.iot.desired_jobs("dev-1") == {}

    def test_completed_with_fewer_outcomes_than_total_is_failed(self,
                                                                llm_env):
        body, _seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        job_id = llm_env.run_item(llm_env.session, run_id)["jobId"]
        llm_env.iot.report("dev-1", job_id,
                           {"status": "completed", "done": 1, "total": 2,
                            "updatedAt": int(time.time())})
        result = llm_env.step()
        assert result["status"] == "failed"
        item = llm_env.run_item(llm_env.session, run_id)
        assert "1 of 2" in item["error"]

    def test_fifteen_minutes_without_progress_is_failed(self, llm_env):
        body, _seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        llm_env.table().update_item(
            Key={"pk": f"SESSION#{llm_env.session}", "sk": f"RUN#{run_id}"},
            UpdateExpression="SET lastProgressAt = :t",
            ExpressionAttributeValues={":t": int(time.time()) - 901})
        result = llm_env.step()
        assert result["status"] == "failed"
        item = llm_env.run_item(llm_env.session, run_id)
        assert "no progress for 15 minutes" in item["error"]
        assert item["summary"]["invocations"] == 0
        assert llm_env.iot.desired_jobs("dev-1") == {}

    def test_progress_resets_the_silence_timer(self, llm_env):
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        job_id = llm_env.run_item(llm_env.session, run_id)["jobId"]
        llm_env.table().update_item(
            Key={"pk": f"SESSION#{llm_env.session}", "sk": f"RUN#{run_id}"},
            UpdateExpression="SET lastProgressAt = :t",
            ExpressionAttributeValues={":t": int(time.time()) - 901})
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-1.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "correct", "isAnomalous": False},
            ])).encode())
        result = llm_env.step()
        assert result["status"] == "running"
        assert result["ingested"]["outcomes"] == 1

    def test_cancellation_requests_it_on_the_shadow_and_keeps_outcomes(
            self, llm_env):
        body, seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        item = llm_env.run_item(llm_env.session, run_id)
        job_id = item["jobId"]
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-1.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 1, [
                {"sampleId": seeded[0]["sampleId"], "repeat": 1,
                 "label": "OK", "category": "correct", "isAnomalous": False},
            ])).encode())
        llm_env.step()
        status, cancelled = llm_env.cancel(run_id)
        assert status == 200, cancelled
        assert cancelled["run"]["status"] == "cancelled"
        assert cancelled["run"]["summary"]["invocations"] == 1
        # cancel: true reached the device, then the entry was removed when
        # the run was finalized.
        requests = [u["document"]["state"]["desired"]["jobs"][job_id]
                    for u in llm_env.iot.updates
                    if job_id in (u["document"].get("state", {})
                                  .get("desired", {}).get("jobs", {}))]
        assert {"manifestKey": item["manifestKey"], "cancel": True} in requests
        assert requests[-1] is None
        assert llm_env.iot.desired_jobs("dev-1") == {}
        # The trailing batch the device wrote after the cancellation is
        # still ingested by the final poll, and the summary refreshed.
        llm_env.s3.put_object(
            Bucket=SAMPLE_BUCKET, Key=prefix + "outcomes-2.json",
            Body=json.dumps(outcome_batch(run_id, llm_env.session, job_id, 2, [
                {"sampleId": seeded[1]["sampleId"], "repeat": 1,
                 "label": "NOK", "category": "false_pass",
                 "isAnomalous": False},
            ])).encode())
        result = llm_env.step()
        assert result["status"] == "cancelled"
        item = llm_env.run_item(llm_env.session, run_id)
        assert item["summary"]["invocations"] == 2
        assert item["summary"]["falsePass"] == 1
        assert not llm_env.dispatched

    def test_a_dispatch_failure_marks_the_run_failed(self, llm_env):
        llm_env.index_sample(llm_env.session, label="OK", thing="dev-1")
        llm_env.deploy_workflow_to(["dev-1"])
        llm_env.iot.fail_update = True
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 200, body
        assert body["dispatchFailed"] is True
        assert body["run"]["status"] == "failed"
        assert "could not be started" in body["run"]["error"]
        # The session's run slot was freed, so another run can be started.
        llm_env.iot.fail_update = False
        status, second = llm_env.start_run(llm_env.session,
                                          llm_env.candidate_id)
        assert status == 202, second

    def test_an_unreadable_batch_does_not_stop_the_poll(self, llm_env):
        body, _seeded = self.dispatch(llm_env)
        run_id = body["runId"]
        prefix = f"{SESSIONS_PREFIX}{llm_env.session}/runs/{run_id}/"
        llm_env.s3.put_object(Bucket=SAMPLE_BUCKET,
                              Key=prefix + "outcomes-1.json",
                              Body=b"{ this is not json")
        result = llm_env.step()
        assert result["status"] == "running"
        assert result["ingested"]["errors"] == 1
        assert len(llm_env.outcome_items(run_id)) == 0


# ==========================================================================
# 5. Run views: progress, outcomes, diff (Requirements 6.8, 7.1-7.4)
# ==========================================================================

class TestRunViews:

    def complete_run(self, env, script=None, repeats=1, labels=("OK", "NOK"),
                     executions=("v0", "v1")):
        seeded = []
        for index, execution in enumerate(executions):
            seeded.append(env.index_sample(
                env.session, label=labels[index % len(labels)],
                execution=execution))
        env.script_bedrock(script=script, default=NORMAL_ANSWER)
        _status, body = env.start_run(env.session, env.candidate_id,
                                      repeats=repeats)
        env.drive()
        return body["runId"], seeded

    def test_progress_and_summary_of_a_running_run(self, bedrock_env):
        for index in range(120):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"g{index:04d}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER, delay=0.001)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        bedrock_env.step()
        status, view = bedrock_env.get_run(run_id)
        assert status == 200, view
        assert view["run"]["status"] == "running"
        assert view["run"]["done"] == 100
        assert view["run"]["plannedInvocations"] == 120
        assert view["run"]["summary"]["invocations"] == 100
        assert view["candidate"]["candidateId"] == bedrock_env.candidate_id
        assert view["session"]["sessionId"] == bedrock_env.session

    def test_outcomes_filtered_by_category_and_paged(self, bedrock_env):
        run_id, seeded = self.complete_run(
            bedrock_env,
            script={"v0": ANOMALOUS_ANSWER, "v1": NORMAL_ANSWER},
            executions=("v0", "v1", "v2", "v3"),
            labels=("OK", "NOK"))
        status, view = bedrock_env.outcomes(run_id)
        assert status == 200, view
        assert view["matched"] == 4
        status, view = bedrock_env.outcomes(run_id,
                                           query={"category": "false_fail"})
        assert [o["sampleId"] for o in view["outcomes"]] == [
            seeded[0]["sampleId"]]
        status, view = bedrock_env.outcomes(run_id,
                                           query={"category": "false_pass"})
        # v1 answered "normal" from the script, v2/v3 from the default: the
        # two NOK samples that were answered "normal" are the false passes.
        assert [o["sampleId"] for o in view["outcomes"]] == [
            seeded[1]["sampleId"], seeded[3]["sampleId"]]
        status, view = bedrock_env.outcomes(run_id,
                                           query={"category": "correct"})
        assert [o["sampleId"] for o in view["outcomes"]] == [
            seeded[2]["sampleId"]]
        status, view = bedrock_env.outcomes(run_id, query={"limit": "2"})
        assert view["count"] == 2 and view["nextCursor"]
        status, page2 = bedrock_env.outcomes(
            run_id, query={"limit": "2", "cursor": view["nextCursor"]})
        assert page2["count"] == 2 and page2["nextCursor"] is None
        first = {o["sampleId"] for o in view["outcomes"]}
        second = {o["sampleId"] for o in page2["outcomes"]}
        assert not (first & second)

    def test_outcomes_carry_the_sample_with_presigned_images(self,
                                                              bedrock_env):
        run_id, seeded = self.complete_run(bedrock_env)
        _status, view = bedrock_env.outcomes(run_id)
        sample = view["samples"][seeded[0]["sampleId"]]
        assert sample["label"] == "OK"
        assert "Signature=" in sample["input"]["url"]
        assert "Expires=" in sample["input"]["url"]
        assert sample["reference"]["url"]
        assert sample["recorded"]["answer"] == ANOMALOUS_ANSWER

    def test_outcomes_can_be_sorted_by_confidence(self, bedrock_env):
        run_id, _seeded = self.complete_run(
            bedrock_env,
            script={"v0": '{"is_anomalous": false, "confidence": 0.1}',
                    "v1": '{"is_anomalous": true, "confidence": 0.99}'})
        _status, view = bedrock_env.outcomes(run_id,
                                            query={"sort": "confidence"})
        assert [o["confidence"] for o in view["outcomes"]] == [0.99, 0.1]
        _status, view = bedrock_env.outcomes(
            run_id, query={"sort": "confidence", "order": "asc"})
        assert [o["confidence"] for o in view["outcomes"]] == [0.1, 0.99]

    def test_unstable_repeats_are_reported(self, bedrock_env):
        """Requirement 6.7: instability is the samples whose repeats
        disagree."""
        sample = bedrock_env.index_sample(bedrock_env.session, label="NOK")
        bedrock = bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        answers = [ANOMALOUS_ANSWER, NORMAL_ANSWER]
        counter = {"n": 0}
        lock = threading.Lock()

        def alternating(**kwargs):
            with lock:
                index = counter["n"]
                counter["n"] += 1
            bedrock.calls.append(kwargs)
            return {"output": {"message": {"content": [
                {"text": answers[index % 2]}]}},
                "usage": {"outputTokens": 10 + index}}

        bedrock.converse = alternating
        _status, body = bedrock_env.start_run(
            bedrock_env.session, bedrock_env.candidate_id, repeats=2)
        bedrock_env.drive()
        _status, view = bedrock_env.get_run(body["runId"])
        assert view["run"]["summary"]["samples"] == 1
        assert view["run"]["summary"]["invocations"] == 2
        assert view["run"]["summary"]["unstable"] == 1
        assert sample["sampleId"]

    def test_diff_lists_only_the_samples_whose_categories_differ(
            self, bedrock_env):
        first_run, seeded = self.complete_run(
            bedrock_env, script={"v0": NORMAL_ANSWER, "v1": ANOMALOUS_ANSWER})
        second_candidate = bedrock_env.candidate(
            bedrock_env.session, name="Candidate B",
            prompt="Look for chips and cracks.")
        bedrock_env.script_bedrock(script={"v0": NORMAL_ANSWER,
                                          "v1": NORMAL_ANSWER})
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             second_candidate)
        bedrock_env.drive()
        second_run = body["runId"]
        status, view = bedrock_env.diff(first_run, second_run)
        assert status == 200, view
        assert view["count"] == 1
        entry = view["differing"][0]
        assert entry["sampleId"] == seeded[1]["sampleId"]
        assert entry["label"] == "NOK"
        assert entry["a"]["categories"] == ["correct"]
        assert entry["b"]["categories"] == ["false_pass"]
        assert entry["a"]["outcomes"][0]["rawAnswer"] == ANOMALOUS_ANSWER

    def test_diff_against_a_run_of_another_session_is_404(self, bedrock_env):
        run_id, _seeded = self.complete_run(bedrock_env)
        status, body = bedrock_env.diff(run_id, str(uuid.uuid4()))
        assert status == 404
        assert body["error"]["code"] == "RUN_NOT_FOUND"

    def test_an_unknown_run_is_the_uniform_404(self, bedrock_env):
        status, body = bedrock_env.get_run(str(uuid.uuid4()))
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_a_non_reader_never_learns_a_run_exists(self, bedrock_env):
        run_id, _seeded = self.complete_run(bedrock_env)
        status, body = bedrock_env.get_run(run_id, user=bedrock_env.outsider)
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
        status, body = bedrock_env.outcomes(run_id,
                                           user=bedrock_env.outsider)
        assert status == 404

    def test_a_reader_may_view_but_not_cancel(self, bedrock_env):
        bedrock_env.stack.tables.user_roles.put_item(Item={
            "user_id": bedrock_env.reader["user_id"],
            "usecase_id": bedrock_env.usecase_id, "role": "Viewer"})
        for index in range(120):
            bedrock_env.index_sample(bedrock_env.session, label="OK",
                                     execution=f"w{index:04d}")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER, delay=0.001)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        run_id = body["runId"]
        status, _view = bedrock_env.get_run(run_id, user=bedrock_env.reader)
        assert status == 200
        status, denied = bedrock_env.cancel(run_id, user=bedrock_env.reader)
        assert status == 403
        assert denied["error"]["code"] == "FORBIDDEN"
        assert bedrock_env.run_item(bedrock_env.session,
                                    run_id)["status"] == "running"

    def test_cancelling_a_finished_run_is_a_no_op(self, bedrock_env):
        run_id, _seeded = self.complete_run(bedrock_env)
        status, body = bedrock_env.cancel(run_id)
        assert status == 200
        assert body["alreadyFinished"] is True
        assert body["run"]["status"] == "completed"

    def test_the_session_view_reports_the_latest_run_per_candidate(
            self, bedrock_env):
        run_id, _seeded = self.complete_run(bedrock_env)
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": bedrock_env.session})
        assert status == 200, body
        entries = {c["candidateId"]: c for c in body["candidates"]}
        assert entries[bedrock_env.candidate_id]["latestRun"]["runId"] == \
            run_id
        assert entries[bedrock_env.candidate_id]["latestRun"]["status"] == \
            "completed"
        assert entries["baseline"]["latestRun"] is None
        assert body["runCount"] == 1


# ==========================================================================
# 6. Selection (Requirements 7.5, 7.6)
# ==========================================================================

class TestSelection:

    def test_selection_is_persisted_and_replaceable(self, bedrock_env):
        second = bedrock_env.candidate(bedrock_env.session, name="B",
                                       prompt="Another prompt.")
        status, body = bedrock_env.select(bedrock_env.session,
                                         bedrock_env.candidate_id)
        assert status == 200, body
        assert body["selectedCandidateId"] == bedrock_env.candidate_id
        _status, view = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": bedrock_env.session})
        assert view["session"]["selectedCandidateId"] == \
            bedrock_env.candidate_id
        status, body = bedrock_env.select(bedrock_env.session, second)
        assert body["selectedCandidateId"] == second

    def test_selection_reports_the_latest_runs_false_passes(self,
                                                            bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="NOK",
                                 execution="fp-1")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        status, selection = bedrock_env.select(bedrock_env.session,
                                              bedrock_env.candidate_id)
        assert status == 200, selection
        assert selection["falsePasses"] == 1
        assert selection["latestRun"]["runId"] == body["runId"]

    def test_selecting_an_unknown_candidate_is_404(self, bedrock_env):
        status, body = bedrock_env.select(bedrock_env.session, "nope")
        assert status == 404
        assert body["error"]["code"] == "CANDIDATE_NOT_FOUND"

    def test_selection_can_be_cleared(self, bedrock_env):
        bedrock_env.select(bedrock_env.session, bedrock_env.candidate_id)
        status, body = bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            bedrock_env.editor, {"id": bedrock_env.session},
            body={"candidateId": None})
        assert status == 200, body
        assert body["selectedCandidateId"] is None

    def test_a_reader_may_not_select(self, bedrock_env):
        bedrock_env.stack.tables.user_roles.put_item(Item={
            "user_id": bedrock_env.reader["user_id"],
            "usecase_id": bedrock_env.usecase_id, "role": "Viewer"})
        status, body = bedrock_env.select(bedrock_env.session,
                                         bedrock_env.candidate_id,
                                         user=bedrock_env.reader)
        assert status == 403, body


# ==========================================================================
# 7. Retention and lifecycle (Requirements 10.2, 10.3, 10.5)
# ==========================================================================

class TestRetention:

    def test_only_the_20_most_recent_runs_of_a_candidate_are_kept(
            self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK",
                                 execution="prune-1")
        run_ids = []
        for index in range(22):
            bedrock_env.script_bedrock(default=NORMAL_ANSWER)
            _status, body = bedrock_env.start_run(bedrock_env.session,
                                                  bedrock_env.candidate_id)
            run_ids.append(body["runId"])
            bedrock_env.drive()
            # Distinct start times so "most recent" is well defined.
            bedrock_env.table().update_item(
                Key={"pk": f"SESSION#{bedrock_env.session}",
                     "sk": f"RUN#{body['runId']}"},
                UpdateExpression="SET startedAt = :t",
                ExpressionAttributeValues={":t": 1000 + index})
        surviving = [i["sk"][len("RUN#"):] for i
                     in bedrock_env.partition(
                         f"SESSION#{bedrock_env.session}")
                     if i["sk"].startswith("RUN#")]
        assert len(surviving) == 20
        assert run_ids[-1] in surviving
        # The pruned runs' outcomes and pointer items are gone with them.
        for pruned in run_ids[:2]:
            assert pruned not in surviving
            assert bedrock_env.partition(f"RUN#{pruned}") == []

    def test_deleting_the_session_removes_runs_and_run_objects(
            self, bedrock_env):
        sample = bedrock_env.index_sample(bedrock_env.session, label="OK")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        run_id = body["runId"]
        prefix = f"{SESSIONS_PREFIX}{bedrock_env.session}/runs/{run_id}/"
        bedrock_env.s3.put_object(Bucket=SAMPLE_BUCKET,
                                  Key=prefix + "outcomes-1.json",
                                  Body=b"{}")
        status, deleted = bedrock_env.call(
            "DELETE", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": bedrock_env.session})
        assert status == 200, deleted
        assert deleted["deleted"]["objects"] == 1
        assert bedrock_env.partition(f"RUN#{run_id}") == []
        assert bedrock_env.partition(f"SESSION#{bedrock_env.session}") == []
        # The exported sample objects survive for other and future sessions.
        assert bedrock_env.s3.get_object(
            Bucket=SAMPLE_BUCKET,
            Key=sample["base"] + ".input.jpg")["Body"].read()

    def test_deleting_a_candidate_deletes_its_runs_only(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        other = bedrock_env.candidate(bedrock_env.session, name="Other",
                                      prompt="Other prompt.")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, mine = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, theirs = bedrock_env.start_run(bedrock_env.session, other)
        bedrock_env.drive()
        status, body = bedrock_env.call(
            "DELETE",
            "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor, {"id": bedrock_env.session, "cid": other})
        assert status == 200, body
        assert body["runsDeleted"] == 1
        assert bedrock_env.partition(f"RUN#{theirs['runId']}") == []
        assert bedrock_env.outcome_items(mine["runId"])

    def test_outcomes_carry_a_ttl(self, bedrock_env):
        bedrock_env.index_sample(bedrock_env.session, label="OK")
        bedrock_env.script_bedrock(default=NORMAL_ANSWER)
        _status, body = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        bedrock_env.drive()
        item = bedrock_env.outcome_items(body["runId"])[0]
        assert int(item["ttl"]) > int(time.time()) + 80 * 24 * 3600


# ==========================================================================
# 8. The end-to-end Bedrock path over the real index
# ==========================================================================

class TestEndToEnd:

    def test_index_label_score_compare_select(self, env):
        """The real index path, then a full Bedrock run and a selection."""
        env.put_workflow([BEDROCK_NODE, PLAIN_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        # Two devices' samples, exported as the device writes them.
        seeded = []
        for index, thing in enumerate(("dev-a", "dev-b")):
            execution = f"e2e-{index}"
            base = f"{env.sample_prefix()}{thing}/{execution}"
            input_bytes = f"INPUT-BYTES-{execution}".encode()
            env.s3.put_object(Bucket=SAMPLE_BUCKET,
                              Key=base + ".input.jpg", Body=input_bytes)
            env.s3.put_object(Bucket=SAMPLE_BUCKET,
                              Key=base + ".reference.jpg", Body=b"REF-BYTES")
            env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                              Body=json.dumps({
                                  "schemaVersion": 1, "source": "live",
                                  "workflowId": env.workflow_id,
                                  "version": 1, "executionId": execution,
                                  "nodeId": env.node_id,
                                  "nodeType": "bedrock_inference",
                                  "thingName": thing, "exportedAt": 100 + index,
                                  "input": {"key": base + ".input.jpg",
                                            "sha256": hashlib.sha256(
                                                input_bytes).hexdigest(),
                                            "bytes": len(input_bytes)},
                                  "reference": {"key": base + ".reference.jpg",
                                                "sha256": "x", "bytes": 9},
                                  "recorded": {"isAnomalous": True,
                                               "confidence": 0.99,
                                               "answer": ANOMALOUS_ANSWER,
                                               "parseError": None},
                                  "promptFingerprint": "sha256:baseline",
                              }).encode())
            seeded.append(f"{thing}/{execution}")
        status, refreshed = env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/refresh",
            env.editor, {"id": session})
        assert status == 200, refreshed
        assert refreshed["refresh"]["indexed"] == 2
        status, _labelled = env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            env.editor, {"id": session},
            body={"sampleIds": [seeded[0]], "label": "OK"})
        assert status == 200
        env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            env.editor, {"id": session},
            body={"sampleIds": [seeded[1]], "label": "NOK"})
        candidate = env.candidate(session, name="E2E",
                                  prompt="Inspect the plate.")
        env.script_bedrock(script={"e2e-0": NORMAL_ANSWER,
                                   "e2e-1": ANOMALOUS_ANSWER})
        status, started = env.start_run(session, candidate, repeats=2)
        assert status == 202, started
        assert started["plannedInvocations"] == 4
        env.drive()
        status, view = env.get_run(started["runId"])
        assert view["run"]["status"] == "completed"
        assert view["run"]["summary"] == {
            "samples": 2, "invocations": 4, "correct": 4, "falsePass": 0,
            "falseFail": 0, "parseFailure": 0, "invocationError": 0,
            "accuracy": 1, "unstable": 0, "meanOutputTokens": 42,
            "maxOutputTokens": 42,
            "meanLatencyMs": view["run"]["summary"]["meanLatencyMs"],
        }
        status, selection = env.select(session, candidate)
        assert selection["falsePasses"] == 0
        # The baseline can be scored too, and both runs compare.
        env.script_bedrock(default=ANOMALOUS_ANSWER)
        status, baseline_run = env.start_run(session, "baseline")
        assert status == 202, baseline_run
        env.drive()
        status, diff = env.diff(started["runId"], baseline_run["runId"])
        assert status == 200, diff
        # The baseline calls the deployed prompt: dev-a (OK) now fails.
        assert [e["sampleId"] for e in diff["differing"]] == [seeded[0]]
