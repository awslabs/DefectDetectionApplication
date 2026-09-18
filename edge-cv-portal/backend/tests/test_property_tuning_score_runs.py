"""Property tests for Score_Run admission, the chunked Bedrock_Scorer and
the Device_Score_Job dispatcher (spec: .kiro/specs/quality-prompt-tuning,
task 6.4).

- **Feature: quality-prompt-tuning, Property 10: Score_Run admission,
  chunking, concurrency, cancellation and resume bounds hold** —
  **Validates: Requirements 6.8, 6.10, 6.11, 6.13, 10.4**
- **Feature: quality-prompt-tuning, Property 11: Device_Score_Jobs are
  delivered, executed, reported and bounded exactly** (Portal half) —
  **Validates: Requirements 6.9, 6.11, 6.12**

How these are driven
--------------------

Each example drives the REAL ``functions/workflow_tuning.py`` handler
against moto with three doubles on the module's own client seams — a
recording ``dispatch_action`` (so the self-invoked execution and poll steps
run inline, one at a time, exactly as Lambda would), a scripted Bedrock
``converse`` client that records every request and the peak number of
concurrent calls, and a fake ``iot-data`` client with real named-shadow
merge semantics — plus a controllable clock (``now_s``) so the 60-minute
resume budget and the 15-minute silence timer are reached deterministically
instead of by waiting.

Property 10 draws a **sequence of start/step/cancel/expire events** and
asserts the bounds after every one of them; Property 11 draws a **device
behaviour script** (outcome batches, re-written batches, unreadable
batches, ``reported`` progress, silence, failure, cancellation) and asserts
the Portal's ingestion and finalization after every poll step. Every
expectation is an independent restatement of the requirements transcribed
in this file — the admission table, the chunk/concurrency bounds, the
exactly-once ingestion and the finalize rules are computed here from the
drawn events, never imported from ``workflow_tuning``.

Two deliberate deviations, both recorded in the OUTCOME of task 6.4:

* Requirement 6.13's bound is **600** planned invocations and Requirement
  6.8's step is **100** invocations. Driving 600+ real invocations per
  example is not feasible, so Property 10 patches
  ``MAX_PLANNED_INVOCATIONS`` and ``CHUNK_INVOCATIONS`` to small drawn
  values and asserts the behaviour over them;
  :func:`test_documented_bounds` pins the module's real constants (600,
  100, 4 threads, 1..3 repeats, 3600 s, 900 s) literally, so changing a
  number fails too. The **concurrency** bound is asserted against the real
  ``SCORE_THREADS``: the drawn chunk goes up to 8 with a slow fake client,
  so a raised thread count shows up as a peak above 4.
* The device half of Property 11 (concurrency 1, batches of ≤ 20, a worker
  separate from the executor) is task 3.4's
  ``test/backend-test/workflow_engine/test_property_tuning_job_runner.py``;
  this file asserts the Portal half only, writing the batch objects in the
  device's documented shape.

The enumerated cases over the same space are task 6.2's
``test_tuning_score_runs.py``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from decimal import Decimal
from unittest import mock

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"

# --------------------------------------------------------------------------
# Independent restatement of the contract under test.
# --------------------------------------------------------------------------

#: Requirement 6.13: the invocation bound of one Score_Run.
REF_MAX_PLANNED_INVOCATIONS = 600
#: Requirement 6.8: invocations per execution step and concurrent
#: invocations.
REF_CHUNK_INVOCATIONS = 100
REF_SCORE_THREADS = 4
#: Requirement 6.7: repeats per sample.
REF_MIN_REPEATS, REF_MAX_REPEATS = 1, 3
#: Requirement 10.4: a run that cannot resume within 60 minutes.
REF_RUN_STALE_SECONDS = 3600
#: Requirement 6.12: a Device_Score_Job silent for 15 minutes.
REF_JOB_SILENCE_SECONDS = 900
#: Requirement 10.3: runs kept per Candidate.
REF_MAX_RUNS_PER_CANDIDATE = 20

#: The named shadow Device_Score_Jobs travel on and its job map key,
#: restated from the device runner.
REF_TUNING_SHADOW = "dda-workflow-tuning"
REF_JOBS_KEY = "jobs"

REF_SAMPLES_PREFIX = "workflow-tuning/samples/"
REF_JOBS_PREFIX = "workflow-tuning/jobs/"
REF_SESSIONS_PREFIX = "workflow-tuning/sessions/"

#: Requirement 6.5's categories.
REF_CATEGORIES = ("correct", "false_pass", "false_fail", "parse_failure",
                  "invocation_error")

ANOMALOUS_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'
NORMAL_ANSWER = '{"is_anomalous": false, "confidence": 0.8}'
GARBAGE_ANSWER = "I cannot tell from these pictures."

BEDROCK_NODE_ID = "bedrock_1"
LLM_NODE_ID = "llm_1"

BEDROCK_NODE = {
    "id": BEDROCK_NODE_ID,
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
    "id": LLM_NODE_ID,
    "type": "llm_inference",
    "position": {"x": 0, "y": 200},
    "parameters": {
        "modelName": "qwen2-vl",
        "prompt_template": "Inspect the plate.",
        "max_tokens": 512,
        "temperature": 0.2,
        "anomaly_mode": True,
    },
}


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


# ==========================================================================
# Doubles on the module's client seams
# ==========================================================================

class FakeBedrock:
    """A scripted Bedrock runtime client recording every request and the
    peak number of concurrent Converse calls."""

    def __init__(self, script=None, default=ANOMALOUS_ANSWER, delay=0.005):
        self.script = dict(script or {})
        self.default = default
        self.delay = delay
        self.calls = []
        self.regions = []
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def input_marker(kwargs):
        for block in kwargs["messages"][0]["content"]:
            if "image" in block:
                return block["image"]["source"]["bytes"].decode(
                    "utf-8", "replace")
        return ""

    def converse(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(self.delay)
            marker = self.input_marker(kwargs)
            answer = self.default
            for key, value in self.script.items():
                if key in marker:
                    answer = value
                    break
            if isinstance(answer, BaseException):
                raise answer
            return {"output": {"message": {"content": [{"text": answer}]}},
                    "usage": {"outputTokens": 42}}
        finally:
            with self._lock:
                self._in_flight -= 1


class FakeIotData:
    """A named-shadow store with the real service's merge semantics: a
    nested ``None`` deletes its key."""

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

    # -- helpers ---------------------------------------------------------
    def desired_jobs(self, thing):
        state = self.shadows.get((thing, REF_TUNING_SHADOW)) or {}
        return (state.get("desired") or {}).get(REF_JOBS_KEY) or {}

    def report(self, thing, job_id, entry):
        state = self.shadows.setdefault((thing, REF_TUNING_SHADOW),
                                       {"desired": {}, "reported": {}})
        self._merge(state, {"reported": {REF_JOBS_KEY: {job_id: entry}}})


# ==========================================================================
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
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


class World:
    """One Use_Case and its users; a fresh workflow, session and run per
    example, with the module's seams replaced by recording doubles."""

    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id, "name": "Tuning use case",
            "account_id": ACCOUNT_ID, "tuning_sample_export": True})
        self.editor = self._user("DataScientist")

    @staticmethod
    def _user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    # ------------------------------------------------------------- setup
    def fresh_workflow(self, nodes=None):
        workflow_id = str(uuid.uuid4())
        document = {"schemaVersion": "1.0",
                    "nodes": nodes or [BEDROCK_NODE, LLM_NODE],
                    "connections": []}
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/1/"
               f"workflow.json")
        self.s3.put_object(Bucket="test-portal-artifacts", Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id, "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID, "name": "Tuning workflow",
            "created_at": 1, "updated_at": 1, "latest_version": 1,
            "created_by": self.editor["user_id"]})
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": 1,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.editor["user_id"]})
        return workflow_id

    def deploy_to(self, workflow_id, devices):
        """The Portal's own record that the devices run the workflow —
        what makes a device eligible for a Device_Score_Job (Req 6.9)."""
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id, "created_at": 1,
            "deployment_status": "IN_PROGRESS", "component_type": "workflow",
            "workflow_id": workflow_id, "target_devices": list(devices)})

    # ---------------------------------------------------------- requests
    def call(self, method, resource, path_params=None, body=None, query=None,
             user=None):
        user = user or self.editor
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method, "resource": resource, "path": path,
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

    def open_session(self, workflow_id, node_id):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions",
            body={"workflow_id": workflow_id, "node_id": node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def refresh(self, session_id):
        return self.call("POST",
                         "/workflow-tuning/anomaly/sessions/{id}/refresh",
                         {"id": session_id})

    def set_labels(self, session_id, sample_ids, label):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            {"id": session_id},
            body={"sampleIds": list(sample_ids), "label": label})

    def create_candidate(self, session_id, name="Candidate A"):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            {"id": session_id},
            body={"name": name, "prompt": "Is the plate defective?",
                  "systemPrompt": "Answer as an inspector.",
                  "maxTokens": 256})
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def start_run(self, session_id, candidate_id, repeats=None, device=None):
        payload = {"candidateId": candidate_id}
        if repeats is not None:
            payload["repeats"] = repeats
        if device is not None:
            payload["deviceThingName"] = device
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            {"id": session_id}, body=payload)

    def cancel(self, run_id):
        return self.call(
            "POST", "/workflow-tuning/anomaly/score-runs/{rid}/cancel",
            {"rid": run_id})

    # ----------------------------------------------------- sample store
    def node_prefix(self, workflow_id, node_id):
        return f"{REF_SAMPLES_PREFIX}{workflow_id}/{node_id}/"

    def seed_sample(self, workflow_id, node_id, thing, execution, content,
                    exported_at=1000):
        base = f"{self.node_prefix(workflow_id, node_id)}{thing}/{execution}"
        data = image_bytes(content)
        reference = image_bytes(content + "-ref")
        document = {
            "schemaVersion": 1, "source": "live", "workflowId": workflow_id,
            "version": 1, "executionId": execution, "nodeId": node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data)},
            "reference": {"key": base + ".reference.jpg",
                          "sha256": hashlib.sha256(reference).hexdigest(),
                          "bytes": len(reference)},
            "recorded": {"isAnomalous": True, "confidence": 0.9,
                         "answer": ANOMALOUS_ANSWER, "parseError": None},
            "promptFingerprint": "sha256:baseline",
        }
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=data)
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".reference.jpg",
                           Body=reference)
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=json.dumps(document).encode("utf-8"))
        return {"sampleId": f"{thing}/{execution}", "marker": content}

    def put_object(self, key, body):
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key, Body=body)

    def objects(self, prefix):
        response = self.s3.list_objects_v2(Bucket=SAMPLE_BUCKET,
                                           Prefix=prefix)
        return {o["Key"] for o in response.get("Contents", []) or []}

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

    def run_item(self, session_id, run_id):
        item = self.table.get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": f"RUN#{run_id}"}).get("Item")
        return native(item) if item else None

    def runs(self, session_id):
        return [i for i in self.items(f"SESSION#{session_id}", "RUN#")
                if i.get("runId")]

    def outcomes(self, run_id):
        return self.items(f"RUN#{run_id}", "OUT#")

    def lock(self, session_id):
        item = self.table.get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": "RUNLOCK"}).get("Item")
        return native(item) if item else None


@pytest.fixture(scope="module")
def world(aws_stack, tuning):
    return World(aws_stack, tuning)


class Seams:
    """The module's client seams, replaced for one example."""

    def __init__(self, module, bedrock=None, iot=None, clock=None):
        self.module = module
        self.bedrock = bedrock or FakeBedrock()
        self.iot = iot or FakeIotData()
        self.dispatched = []
        self.clock = [int(clock or 1_700_000_000)]
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
            mock.patch.object(module, "now_s", lambda: self.clock[0]),
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

    # -- driving ---------------------------------------------------------
    def advance(self, seconds):
        self.clock[0] += int(seconds)

    def step(self):
        """Run the next dispatched action inline, as Lambda would."""
        payload = self.dispatched.pop(0)
        return self.module.handler(payload, None)


# ==========================================================================
# Property 10: admission, chunking, concurrency, cancellation, resume
# ==========================================================================

ANSWER_KINDS = ("anomalous", "normal", "garbage", "error")

EVENTS = ("step", "step", "step", "replay", "cancel", "start_second",
          "expire")


def expected_category(label, kind):
    """Requirement 6.5, restated: the category of one replay."""
    if kind == "error":
        return "invocation_error"
    if kind == "garbage":
        return "parse_failure"
    anomalous = kind == "anomalous"
    if label == "NOK":
        return "correct" if anomalous else "false_pass"
    return "false_fail" if anomalous else "correct"


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(samples=st.lists(st.tuples(st.sampled_from(("OK", "NOK")),
                                  st.sampled_from(ANSWER_KINDS)),
                        min_size=1, max_size=5),
       repeats=st.integers(min_value=REF_MIN_REPEATS,
                           max_value=REF_MAX_REPEATS),
       bound=st.integers(min_value=1, max_value=20),
       chunk=st.integers(min_value=1, max_value=8),
       events=st.lists(st.sampled_from(EVENTS), min_size=0, max_size=6))
def test_property_score_run_admission_chunking_cancellation_and_resume(
        world, samples, repeats, bound, chunk, events):
    """**Feature: quality-prompt-tuning, Property 10: Score_Run admission,
    chunking, concurrency, cancellation and resume bounds hold** —
    **Validates: Requirements 6.8, 6.10, 6.11, 6.13, 10.4**

    *For any* sequence of start/cancel/step events against a session with N
    labelled samples and R repeats, at most one run is in progress (a second
    start rejected naming the first), a start with N×R over the bound is
    rejected before any invocation, each Bedrock execution step issues at
    most one chunk of invocations with at most 4 in flight, after
    cancellation no further invocations are issued and every produced
    outcome remains, a resumed run never re-issues an already-persisted
    ``(sample, repeat)``, and a run older than 60 minutes is finalized as
    failed on resume.
    """
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id, BEDROCK_NODE_ID)
    seeded = []
    script = {}
    for index, (label, kind) in enumerate(samples):
        marker = f"s{index}"
        written = world.seed_sample(workflow_id, BEDROCK_NODE_ID, "dev-1",
                                    f"exec-{index}", marker,
                                    exported_at=1000 + index)
        written["label"] = label
        written["kind"] = kind
        seeded.append(written)
        script[marker] = {
            "anomalous": ANOMALOUS_ANSWER, "normal": NORMAL_ANSWER,
            "garbage": GARBAGE_ANSWER,
            "error": RuntimeError("bedrock is unavailable"),
        }[kind]
    assert world.refresh(session_id)[0] == 200
    for sample in seeded:
        assert world.set_labels(session_id, [sample["sampleId"]],
                                sample["label"])[0] == 200
    candidate_id = world.create_candidate(session_id)

    label_of = {s["sampleId"]: s["label"] for s in seeded}
    kind_of = {s["sampleId"]: s["kind"] for s in seeded}
    units = {(s["sampleId"], repeat)
             for s in seeded for repeat in range(1, repeats + 1)}
    planned = len(seeded) * repeats

    bedrock = FakeBedrock(script=script)
    with Seams(world.module, bedrock=bedrock) as seams, \
            mock.patch.object(world.module, "MAX_PLANNED_INVOCATIONS",
                              bound), \
            mock.patch.object(world.module, "CHUNK_INVOCATIONS", chunk):
        status, body = world.start_run(session_id, candidate_id,
                                       repeats=repeats)

        # -- Requirement 6.13: rejected BEFORE any invocation -------------
        if planned > bound:
            assert status == 400, body
            assert body["error"]["code"] == "RUN_TOO_LARGE"
            assert body["error"]["details"]["plannedInvocations"] == planned
            assert body["error"]["details"]["bound"] == bound
            assert bedrock.calls == []
            assert world.runs(session_id) == []
            assert world.lock(session_id) is None
            assert seams.dispatched == []
            return

        assert status == 202, body
        run_id = body["runId"]
        assert body["plannedInvocations"] == planned
        assert len(seams.dispatched) == 1
        assert seams.dispatched[0]["action"] == "execute_score_run"
        # Requirement 6.10: the session's single slot is held by this run.
        assert (world.lock(session_id) or {}).get("runId") == run_id

        def state():
            return world.run_item(session_id, run_id)

        def check_invariants(calls_before, outcomes_before):
            """The bounds that hold after every event."""
            run = state()
            outcomes = world.outcomes(run_id)
            keys = [(o["sampleId"], int(o["repeat"])) for o in outcomes]
            # Exactly once, and only planned units.
            assert len(keys) == len(set(keys))
            assert set(keys) <= units
            # Requirement 10.4: a persisted unit is never re-issued, so
            # every Converse call produced exactly one new outcome.
            assert len(bedrock.calls) == len(outcomes)
            assert len(bedrock.calls) <= planned
            # Requirement 6.8: the step's chunk and concurrency bounds.
            assert len(bedrock.calls) - calls_before <= chunk
            assert bedrock.max_in_flight <= REF_SCORE_THREADS
            # Requirement 6.11: nothing produced is ever lost.
            assert len(outcomes) >= outcomes_before
            # At most one run in progress in the session.
            running = [r for r in world.runs(session_id)
                       if r.get("status") == "running"]
            assert len(running) <= 1
            lock = world.lock(session_id)
            if run.get("status") == "running":
                assert (lock or {}).get("runId") == run_id
            else:
                assert lock is None or lock.get("runId") != run_id
            return run, outcomes

        run, outcomes = check_invariants(0, 0)
        cancelled_at = None
        last_payload = None
        replays = 0

        for event in events:
            calls_before = len(bedrock.calls)
            outcomes_before = len(world.outcomes(run_id))
            status_before = state().get("status")

            if event == "step":
                if not seams.dispatched:
                    # A terminal run dispatches no further step.
                    assert status_before != "running"
                    continue
                last_payload = dict(seams.dispatched[0])
                seams.step()
                run, outcomes = check_invariants(calls_before,
                                                 outcomes_before)
                if status_before != "running":
                    # Requirement 6.11: no further invocation after the
                    # run stopped.
                    assert len(bedrock.calls) == calls_before
                    assert len(outcomes) == outcomes_before
                elif run.get("status") == "running":
                    # A running run always has a pending step (one per
                    # step, plus one per replayed delivery).
                    assert 1 <= len(seams.dispatched) <= 1 + replays

            elif event == "replay":
                # Requirement 10.4: an execution step delivered twice (a
                # Lambda retry) resumes from the persisted outcomes — it
                # may continue the run's remaining work, but it never
                # re-issues a unit that is already persisted, which
                # ``check_invariants`` catches as a Converse call that
                # produced no new outcome.
                if last_payload is None:
                    continue
                seams.dispatched.insert(0, dict(last_payload))
                seams.step()
                if state().get("status") == "running":
                    replays += 1
                run, outcomes = check_invariants(calls_before,
                                                 outcomes_before)

            elif event == "cancel":
                status, body = world.cancel(run_id)
                assert status == 200, body
                run, outcomes = check_invariants(calls_before,
                                                 outcomes_before)
                assert run.get("status") in ("cancelled", "completed",
                                             "failed")
                if status_before == "running":
                    assert run["status"] == "cancelled"
                    assert bool(run.get("cancelRequested")) is True
                # Every outcome produced so far remains.
                assert len(outcomes) == outcomes_before
                if cancelled_at is None:
                    cancelled_at = outcomes_before

            elif event == "start_second":
                status, body = world.start_run(session_id, candidate_id,
                                               repeats=repeats)
                if status_before == "running":
                    # Requirement 6.10: rejected, naming the first run.
                    assert status == 409, body
                    assert body["error"]["code"] == "RUN_IN_PROGRESS"
                    assert body["error"]["details"]["runId"] == run_id
                    assert run_id in body["error"]["message"]
                    assert len(world.runs(session_id)) == 1
                    check_invariants(calls_before, outcomes_before)
                else:
                    # The slot is free once the run is terminal; a new run
                    # owns the session from here, so this example's
                    # invariants for the first run are established.
                    assert status == 202, body
                    assert body["runId"] != run_id
                    break

            elif event == "expire":
                if status_before != "running":
                    continue
                # Requirement 10.4: the run outlives its 60-minute budget.
                seams.advance(REF_RUN_STALE_SECONDS + 60)
                if not seams.dispatched:
                    seams.dispatched.append({
                        "action": "execute_score_run", "run_id": run_id,
                        "session_id": session_id,
                        "cursor": int(state().get("cursor") or 0)})
                seams.step()
                run, outcomes = check_invariants(calls_before,
                                                 outcomes_before)
                assert run["status"] == "failed"
                assert "60 minutes" in str(run.get("error"))
                assert len(outcomes) == outcomes_before

        # -- a step delivered twice (Requirement 10.4) -------------------
        # A retried execution step — the harshest one, carrying the run's
        # FIRST cursor — resumes from the persisted outcomes: it may
        # continue the remaining work, but every Converse call it issues
        # must still produce a new outcome, so no already-persisted
        # ``(sample, repeat)`` is replayed.
        run = state()
        if run.get("status") == "running" and world.outcomes(run_id):
            calls_before = len(bedrock.calls)
            outcomes_before = len(world.outcomes(run_id))
            seams.dispatched.insert(0, {"action": "execute_score_run",
                                        "run_id": run_id,
                                        "session_id": session_id,
                                        "cursor": 0})
            seams.step()
            check_invariants(calls_before, outcomes_before)

        # -- the final state -------------------------------------------
        run = state()
        outcomes = world.outcomes(run_id)
        summary = run.get("summary") or {}
        counts = Counter(o.get("category") for o in outcomes)
        assert summary["invocations"] == len(outcomes)
        assert summary["samples"] == len({o["sampleId"] for o in outcomes})
        for category in REF_CATEGORIES:
            key = {"correct": "correct", "false_pass": "falsePass",
                   "false_fail": "falseFail",
                   "parse_failure": "parseFailure",
                   "invocation_error": "invocationError"}[category]
            assert summary[key] == counts[category]
        # Requirement 6.5: each outcome's category is the one its Label and
        # the model's answer imply.
        for outcome in outcomes:
            assert outcome["category"] == expected_category(
                label_of[outcome["sampleId"]], kind_of[outcome["sampleId"]])
        if run.get("status") == "completed":
            assert {(o["sampleId"], int(o["repeat"]))
                    for o in outcomes} == units
            assert run.get("done") == planned
        if cancelled_at is not None:
            assert len(outcomes) >= cancelled_at


# ==========================================================================
# Property 11 (Portal half): Device_Score_Job delivery, ingestion and
# finalization
# ==========================================================================

REPORT_KINDS = (None, None, None, "running", "completed", "completed_short",
                "failed", "cancelled")

device_rounds = st.lists(
    st.fixed_dictionaries({
        "batch": st.integers(min_value=0, max_value=3),
        "rewrite": st.booleans(),
        "dupe": st.booleans(),
        "corrupt": st.booleans(),
        "report": st.sampled_from(REPORT_KINDS),
        "advance": st.sampled_from((0, 60, REF_JOB_SILENCE_SECONDS + 60)),
        # A cancellation ends the run, so it stays rare enough for the
        # longer device behaviours to be reached.
        "cancel": st.integers(min_value=0, max_value=5).map(lambda n: n == 0),
    }), min_size=1, max_size=5)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=3),
       repeats=st.integers(min_value=REF_MIN_REPEATS,
                           max_value=REF_MAX_REPEATS),
       rounds=device_rounds)
def test_property_device_score_jobs_are_delivered_ingested_and_finalized(
        world, count, repeats, rounds):
    """**Feature: quality-prompt-tuning, Property 11: Device_Score_Jobs are
    delivered, executed, reported and bounded exactly** (Portal half) —
    **Validates: Requirements 6.9, 6.11, 6.12**

    *For any* job manifest and any device behaviour (progress, silence,
    failure, cancellation), the Portal delivers the job through the
    device's named shadow, ingests every outcome batch exactly once,
    finalizes ``completed`` only when the device reports ``done == total``,
    finalizes ``failed`` after 15 minutes without progress or on a reported
    failure, and asks the device to stop on cancellation — keeping every
    outcome already produced.

    The device half (concurrency 1, batches of at most 20, a worker
    separate from the executor) is task 3.4's device property test; here
    the batches are written in the device's documented shape.
    """
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id, LLM_NODE_ID)
    device = "dev-1"
    world.deploy_to(workflow_id, [device])
    seeded = [world.seed_sample(workflow_id, LLM_NODE_ID, device,
                               f"exec-{index}", f"s{index}",
                               exported_at=1000 + index)
              for index in range(count)]
    assert world.refresh(session_id)[0] == 200
    for sample in seeded:
        assert world.set_labels(session_id, [sample["sampleId"]], "OK")[0] \
            == 200
    candidate_id = world.create_candidate(session_id)

    units = [(s["sampleId"], repeat) for s in seeded
             for repeat in range(1, repeats + 1)]
    total = len(units)

    with Seams(world.module) as seams:
        status, body = world.start_run(session_id, candidate_id,
                                       repeats=repeats)
        assert status == 202, body
        assert body["mode"] == "device"
        # Requirement 6.9: the device the Portal records as running the
        # workflow, and only such a device, may execute the job.
        assert body["deviceThingName"] == device
        assert body["deviceEligibility"]["eligible"] == [device]
        run_id = body["runId"]
        run = world.run_item(session_id, run_id)
        job_id = run["jobId"]
        manifest_key = run["manifestKey"]
        assert manifest_key == f"{REF_JOBS_PREFIX}{job_id}/manifest.json"
        manifest = json.loads(world.read_object(manifest_key))
        assert manifest["runId"] == run_id
        assert manifest["repeats"] == repeats
        assert [s["sampleId"] for s in manifest["samples"]] == [
            s["sampleId"] for s in seeded]
        # The job is delivered through the device's named shadow, once.
        assert seams.iot.desired_jobs(device) == {
            job_id: {"manifestKey": manifest_key, "cancel": False}}
        # The first poll step is queued.
        assert [p["action"] for p in seams.dispatched] == ["poll_score_job"]

        outcomes_prefix = (f"{REF_SESSIONS_PREFIX}{session_id}/runs/"
                           f"{run_id}/")
        # The Portal's own model of the device's progress.
        replayed = []           # units the device has answered
        expected_answer = {}    # unit -> the FIRST answer written for it
        batch_index = 0
        ingested_keys = []      # readable batch keys the Portal has read
        batch_units = {}        # key -> the units it carries
        last_progress = seams.clock[0]
        expected_status = "running"

        for round_spec in rounds:
            if expected_status != "running":
                break

            # -- the user cancels (Requirement 6.11) ---------------------
            if round_spec["cancel"]:
                before = len(world.outcomes(run_id))
                status, body = world.cancel(run_id)
                assert status == 200, body
                expected_status = "cancelled"
                # The device is asked to stop after the batch in flight.
                entry = seams.iot.desired_jobs(device).get(job_id)
                assert entry is None or entry.get("cancel") is True
                assert len(world.outcomes(run_id)) == before

            # -- the device writes outcome batches ----------------------
            pending = [unit for unit in units if unit not in replayed]
            batch = pending[:round_spec["batch"]]
            fresh_keys = []
            if batch:
                batch_index += 1
                key = outcomes_prefix + f"outcomes-{batch_index}.json"
                world.put_object(key, batch_document(
                    job_id, session_id, run_id, device, batch_index, batch,
                    ANOMALOUS_ANSWER))
                batch_units[key] = list(batch)
                for unit in batch:
                    expected_answer.setdefault(unit, ANOMALOUS_ANSWER)
                replayed.extend(batch)
                fresh_keys.append(key)
            if round_spec["rewrite"] and ingested_keys:
                # An ALREADY-INGESTED batch object re-written with
                # different answers: it is never ingested again, so the
                # persisted outcomes do not move (Requirement 6.12).
                stale_key = ingested_keys[-1]
                world.put_object(stale_key, batch_document(
                    job_id, session_id, run_id, device, batch_index,
                    batch_units[stale_key], NORMAL_ANSWER))
            if round_spec["dupe"] and ingested_keys:
                # The same outcomes re-sent under a NEW batch name (a
                # device that restarted its job and re-numbered its
                # batches): the batch is read, but each outcome is
                # persisted exactly once, so the first answer stands.
                batch_index += 1
                key = outcomes_prefix + f"outcomes-{batch_index}.json"
                world.put_object(key, batch_document(
                    job_id, session_id, run_id, device, batch_index,
                    batch_units[ingested_keys[0]], NORMAL_ANSWER))
                batch_units[key] = list(batch_units[ingested_keys[0]])
                fresh_keys.append(key)
            if round_spec["corrupt"]:
                batch_index += 1
                world.put_object(
                    outcomes_prefix + f"outcomes-{batch_index}.json",
                    b"{ this is not json")

            # -- the device reports through the shadow ------------------
            report = round_spec["report"]
            if report is not None:
                entry = {"status": {"completed_short": "completed"}.get(
                    report, report), "done": len(replayed), "total": total}
                if report == "completed_short":
                    entry["done"] = max(0, len(replayed) - 1)
                if report == "failed":
                    entry["error"] = "the model could not be loaded"
                seams.iot.report(device, job_id, entry)

            seams.advance(round_spec["advance"])

            # -- the Portal polls ---------------------------------------
            assert seams.dispatched, "a running job keeps polling"
            seams.step()
            ingested_keys.extend(fresh_keys)

            persisted = world.outcomes(run_id)
            keys = [(o["sampleId"], int(o["repeat"])) for o in persisted]
            # Requirement 6.12: every batch ingested exactly once — one
            # item per unit, carrying the FIRST answer written for it.
            assert len(keys) == len(set(keys))
            assert set(keys) == set(expected_answer)
            for outcome in persisted:
                unit = (outcome["sampleId"], int(outcome["repeat"]))
                assert outcome["rawAnswer"] == expected_answer[unit]
                assert outcome["thingName"] == device

            # The Portal's restated finalize rule. Reading ANY new batch
            # object is progress, even one that adds no outcome.
            if fresh_keys:
                last_progress = seams.clock[0]
            if expected_status == "running":
                if report == "failed":
                    expected_status = "failed"
                elif report == "cancelled":
                    expected_status = "cancelled"
                elif report == "completed":
                    expected_status = ("completed"
                                       if len(replayed) == total
                                       else "failed")
                elif report == "completed_short":
                    expected_status = "failed"
                elif (seams.clock[0] - last_progress) \
                        > REF_JOB_SILENCE_SECONDS:
                    expected_status = "failed"

            run = world.run_item(session_id, run_id)
            assert run["status"] == expected_status, (
                f"round {round_spec} expected {expected_status}, got "
                f"{run['status']} (error={run.get('error')})")
            if expected_status == "running":
                assert [p["action"] for p in seams.dispatched] == [
                    "poll_score_job"]
            else:
                # Requirement 6.9/6.11: the job is withdrawn from the
                # device and the run stops being polled.
                assert job_id not in seams.iot.desired_jobs(device)
                assert seams.dispatched == []
                assert run.get("finishedAt")
                assert (run.get("summary") or {})["invocations"] == len(
                    persisted)
                if expected_status == "failed" and report != "failed":
                    assert run.get("error")

        # Every outcome the device produced survived the run's fate.
        persisted = world.outcomes(run_id)
        assert {(o["sampleId"], int(o["repeat"]))
                for o in persisted} == set(expected_answer)
        # And no outcome the device never reported was invented.
        assert set(expected_answer) <= set(units)


def batch_document(job_id, session_id, run_id, thing, index, units, answer):
    """One outcome batch exactly as the device writes it
    (src/backend/workflow_engine/tuning/job_runner.py ``_write_batch``)."""
    return json.dumps({
        "schemaVersion": 1,
        "jobId": job_id,
        "sessionId": session_id,
        "runId": run_id,
        "batch": int(index),
        "thingName": thing,
        "writtenAt": 1_700_000_000,
        "outcomes": [{
            "sampleId": sample_id,
            "repeat": repeat,
            "label": "OK",
            "category": "false_fail" if answer == ANOMALOUS_ANSWER
            else "correct",
            "isAnomalous": answer == ANOMALOUS_ANSWER,
            "confidence": 0.9,
            "rawAnswer": answer,
            "outputTokens": 21,
            "latencyMs": 120,
        } for sample_id, repeat in units],
    }, sort_keys=True).encode("utf-8")


# ==========================================================================
# Supporting checks: the bounds the properties are stated over
# ==========================================================================

def test_documented_bounds(tuning):
    """The numbers Properties 10 and 11 bound against are the design's,
    literally.

    Property 10 patches the invocation bound and the chunk size so an
    example stays small; these pin the real constants, so changing one
    fails here.
    """
    assert tuning.MAX_PLANNED_INVOCATIONS == REF_MAX_PLANNED_INVOCATIONS \
        == 600
    assert tuning.CHUNK_INVOCATIONS == REF_CHUNK_INVOCATIONS == 100
    assert tuning.SCORE_THREADS == REF_SCORE_THREADS == 4
    assert tuning.MIN_REPEATS == REF_MIN_REPEATS == 1
    assert tuning.MAX_REPEATS == REF_MAX_REPEATS == 3
    assert tuning.RUN_STALE_SECONDS == REF_RUN_STALE_SECONDS == 3600
    assert tuning.JOB_SILENCE_SECONDS == REF_JOB_SILENCE_SECONDS == 900
    assert tuning.MAX_RUNS_PER_CANDIDATE == REF_MAX_RUNS_PER_CANDIDATE == 20
    assert tuning.TUNING_SHADOW_NAME == REF_TUNING_SHADOW
    assert tuning.SHADOW_JOBS_KEY == REF_JOBS_KEY
    assert tuning.tuning_settings.JOB_STORE_PREFIX == REF_JOBS_PREFIX
    assert tuning.tuning_settings.SESSION_STORE_PREFIX == REF_SESSIONS_PREFIX


def test_repeats_outside_the_bounds_are_rejected(world):
    """Requirement 6.7's admission rule at its edges, which the property's
    drawn repeats stay inside."""
    workflow_id = world.fresh_workflow()
    session_id = world.open_session(workflow_id, BEDROCK_NODE_ID)
    sample = world.seed_sample(workflow_id, BEDROCK_NODE_ID, "dev-1",
                               "exec-0", "s0")
    assert world.refresh(session_id)[0] == 200
    assert world.set_labels(session_id, [sample["sampleId"]], "OK")[0] == 200
    candidate_id = world.create_candidate(session_id)
    with Seams(world.module):
        for repeats in (0, 4, -1, "two", 1.5, True):
            status, body = world.start_run(session_id, candidate_id,
                                           repeats=repeats)
            assert status == 400, (repeats, body)
            assert body["error"]["code"] == "INVALID_REPEATS"
