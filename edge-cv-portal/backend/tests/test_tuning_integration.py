"""
Anomaly_Tuning Portal integration: the end-to-end flow and the
Device_Score_Job dispatch matrix
(spec: .kiro/specs/quality-prompt-tuning, task 6.5,
design.md "Testing Strategy → Integration tests").

Two integration flows, both over the real ``functions/workflow_tuning.py``
handler against moto:

1. **Portal end-to-end (moto + stubbed Bedrock)** — exported samples for two
   devices and two definition versions are seeded exactly as the device
   writes them, then a Tuning_Session is created, refreshed, labelled, given
   Synthetic_Negatives, scored by the chunked Bedrock_Scorer driven inline,
   compared against the baseline's run, selected and applied; the new
   version's definition diff, the audit event and the session's
   Tuning_Result are asserted, and the session is finally deleted with its
   run objects while the exported samples survive.
2. **VLM dispatch (moto + stubbed iot-data)** — the manifest and
   ``desired.jobs`` document, ingestion exactly once, and all four
   finalization paths (completed, reported failure, 15-minute silence,
   cancellation).

Around them sit the unit-level checks task 6.5 names which only make sense
against a live store: the overview's per-node counts, the Baseline_Candidate
following a new definition version, the refresh summary, chunk stepping with
the cursor, resume without re-issuing a persisted unit, the 60-minute
finalize, and the prune to 20 runs per Candidate.

Doubles are installed only on the module's own client seams: a recording
``dispatch_action`` (so the self-invoked steps run inline, one at a time,
exactly as Lambda would), a scripted Converse client, and a fake ``iot-data``
implementing named-shadow merge semantics (a nested ``None`` deletes its
key). Everything else — DynamoDB, S3, RBAC, the audit log and the designer
save path — is real.

Expectations are restated here (the sidecar shape, the Verdict_Instruction,
the shadow document, the canonical definition serialization), never imported
from the handler, so a change cannot move both the code and its expectation
together.
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
PORTAL_BUCKET = "test-portal-artifacts"
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

ANOMALOUS = '{"is_anomalous": true, "confidence": 0.91}'
NORMAL = '{"is_anomalous": false, "confidence": 0.77}'

BASELINE_PROMPT = "Compare the plate to the reference."
BASELINE_SYSTEM = "You are a quality inspector."
BASELINE_MAX_TOKENS = 256


def prompt_fingerprint(prompt, system_prompt, max_tokens):
    """``sha256:<hex>`` over a Prompt_Set's canonical JSON.

    Restated here (never imported) exactly as the design defines it: the
    prompt text, the normalized system text (blank ⇒ ``None``) and the
    token budget, canonically serialized. A device that ran the deployed
    Prompt_Set records this value in its sidecar, so the Portal can tell a
    sample produced by the baseline from one produced by another prompt
    (Requirement 3.6).
    """
    payload = {
        "prompt": str(prompt or ""),
        "system_prompt": (str(system_prompt)
                          if system_prompt is not None
                          and str(system_prompt).strip() else None),
        "max_tokens": max_tokens,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The fingerprint of the deployed Prompt_Set of :data:`BEDROCK_NODE`.
BASELINE_FINGERPRINT = prompt_fingerprint(BASELINE_PROMPT, BASELINE_SYSTEM,
                                          BASELINE_MAX_TOKENS)
NEW_PROMPT = "Describe the reference, describe the input, then compare."
NEW_SYSTEM = "You are a meticulous inspector."
NEW_MAX_TOKENS = 384

BEDROCK_NODE = {
    "id": "bedrock_1",
    "type": "bedrock_inference",
    "position": {"x": 0, "y": 0},
    "parameters": {
        "model": "us.amazon.nova-lite-v1:0",
        "prompt": BASELINE_PROMPT,
        "system_prompt": BASELINE_SYSTEM,
        "max_tokens": BASELINE_MAX_TOKENS,
        "region": "us-west-2",
        "anomaly_mode": True,
        "crop_margin_percent": 5,
    },
}
LLM_NODE = {
    "id": "llm_1",
    "type": "llm_inference",
    "position": {"x": 10, "y": 200},
    "parameters": {
        "modelName": "qwen2-vl",
        "prompt_template": "Inspect the plate.",
        "system_prompt": "Answer strictly as JSON.",
        "max_tokens": 512,
        "temperature": 0.2,
        "anomaly_mode": True,
    },
}
CAMERA_NODE = {
    "id": "cam_1",
    "type": "camera_source",
    "position": {"x": 0, "y": 300},
    "parameters": {"camera_id": "cam-a"},
}
CONNECTION = {
    "id": "c1",
    "from": {"node": "cam_1", "port": "out"},
    "to": {"node": "bedrock_1", "port": "in"},
}


# ==========================================================================
# Doubles on the module's client seams
# ==========================================================================

class FakeBedrock:
    """A Converse client answering per sample marker.

    ``script`` maps a marker contained in the request's input image bytes to
    the answer text (or an exception to raise). Every request is recorded
    with the peak number of concurrent calls.
    """

    def __init__(self, script=None, default=ANOMALOUS, delay=0.0):
        self.script = dict(script or {})
        self.default = default
        self.delay = delay
        self.calls = []
        self.regions = []
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def input_bytes(kwargs):
        for block in kwargs["messages"][0]["content"]:
            if "image" in block:
                return block["image"]["source"]["bytes"]
        return b""

    def converse(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            marker = self.input_bytes(kwargs).decode("utf-8", "replace")
            answer = self.default
            for key, value in self.script.items():
                if key in marker:
                    answer = value
                    break
            if isinstance(answer, BaseException):
                raise answer
            return {"output": {"message": {"content": [{"text": answer}]}},
                    "usage": {"outputTokens": 33}}
        finally:
            with self._lock:
                self._in_flight -= 1


class FakeIotData:
    """A named-shadow store with the real service's merge semantics."""

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
    """One Use_Case, workflow and set of users, with the module's client
    seams replaced by recording doubles."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.monkeypatch = monkeypatch
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.put_usecase()
        self.saver = self._user("DataScientist")   # read + edit + save
        self.reader = self._user("Viewer")         # read only
        self.workflow_id = None
        self.node_id = None
        self.dispatched = []
        self.bedrock = FakeBedrock()
        self.iot = FakeIotData()

        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client", self._bedrock_client)
        monkeypatch.setattr(module, "iot_data_client",
                            lambda usecase: self.iot)
        monkeypatch.setattr(module, "POLL_INTERVAL_SECONDS", 0)

    # ------------------------------------------------------------- setup
    def _user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def _bedrock_client(self, region):
        self.bedrock.regions.append(region)
        return self.bedrock

    def script_bedrock(self, script=None, default=ANOMALOUS, delay=0.0):
        self.bedrock = FakeBedrock(script=script, default=default,
                                   delay=delay)
        return self.bedrock

    def put_usecase(self, export=True, retention_days=None):
        item = {"usecase_id": self.usecase_id, "name": "Tuning use case",
                "account_id": ACCOUNT_ID, "tuning_sample_export": export}
        if retention_days is not None:
            item["tuning_sample_retention_days"] = retention_days
        self.stack.tables.usecases.put_item(Item=item)

    def definition_key(self, version, workflow_id=None):
        return (f"workflows/{self.usecase_id}/"
                f"{workflow_id or self.workflow_id}/versions/{version}/"
                f"workflow.json")

    def canonical(self, document):
        """The canonical serialization the Workflow_Serializer produces,
        restated here (sorted keys, 2-space indent, ASCII)."""
        return json.dumps(document, sort_keys=True, indent=2,
                          ensure_ascii=True)

    def put_workflow(self, nodes, connections=None, version=1,
                     workflow_id=None):
        workflow_id = workflow_id or str(uuid.uuid4())
        document = {
            "schemaVersion": 1,
            "nodes": sorted((json.loads(json.dumps(n)) for n in nodes),
                            key=lambda n: n["id"]),
            "connections": sorted(
                (json.loads(json.dumps(c))
                 for c in (connections if connections is not None else [])),
                key=lambda c: c["id"]),
        }
        key = self.definition_key(version, workflow_id)
        self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                           Body=self.canonical(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID,
            "name": "Tuning workflow",
            "description": "A workflow with a tunable node",
            "created_at": 1, "updated_at": 1,
            "latest_version": version,
            "created_by": self.saver["user_id"],
        })
        self.stack.tables.versions.put_item(Item={
            "workflow_id": workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.saver["user_id"],
            "validation_status": {"status": "none"},
            "compiled_arch_keys": {}, "component_arn": None,
        })
        self.workflow_id = workflow_id
        return workflow_id

    def deploy_workflow_to(self, devices):
        """The Portal's own record that these devices run the workflow."""
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "deployment_status": "IN_PROGRESS",
            "component_type": "workflow",
            "workflow_id": self.workflow_id,
            "target_devices": list(devices),
        })

    def stored_definition(self, version):
        item = self.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id,
                 "version": version}).get("Item")
        assert item, f"no version item for v{version}"
        body = self.s3.get_object(Bucket=PORTAL_BUCKET,
                                  Key=item["s3_definition_key"])
        return body["Body"].read().decode("utf-8")

    # ----------------------------------------------------- sample store
    def sample_prefix(self, node_id=None):
        return (f"{SAMPLES_PREFIX}{self.workflow_id}/"
                f"{node_id or self.node_id}/")

    def export_sample(self, thing="dev-1", execution=None, node_id=None,
                      exported_at=1000, version=1, is_anomalous=True,
                      fingerprint=BASELINE_FINGERPRINT, reference=True,
                      write_input=True, write_sidecar=True):
        """Write one Tuning_Sample exactly as the device writes it:
        ``{prefix}{node}/{thing}/{exec}.{json,input.jpg,reference.jpg}``
        (src/backend/workflow_engine/tuning/sample_export.py)."""
        execution = execution or f"exec-{uuid.uuid4().hex[:8]}"
        base = f"{self.sample_prefix(node_id)}{thing}/{execution}"
        input_bytes = f"INPUT-{execution}".encode()
        reference_bytes = f"REFERENCE-{execution}".encode()
        document = {
            "schemaVersion": 1, "source": "live",
            "workflowId": self.workflow_id, "version": version,
            "executionId": execution, "nodeId": node_id or self.node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes)},
            "recorded": {"isAnomalous": is_anomalous, "confidence": 0.9,
                         "answer": ANOMALOUS if is_anomalous else NORMAL,
                         "parseError": None},
            "promptFingerprint": fingerprint,
        }
        if reference:
            document["reference"] = {
                "key": base + ".reference.jpg",
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                "bytes": len(reference_bytes)}
            self.s3.put_object(Bucket=SAMPLE_BUCKET,
                               Key=base + ".reference.jpg",
                               Body=reference_bytes)
        if write_input:
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                               Body=input_bytes)
        if write_sidecar:
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                               Body=json.dumps(document).encode("utf-8"))
        return {"sampleId": f"{thing}/{execution}", "executionId": execution,
                "thingName": thing, "base": base, "document": document,
                "inputBytes": input_bytes}

    def objects_under(self, prefix):
        response = self.s3.list_objects_v2(Bucket=SAMPLE_BUCKET,
                                           Prefix=prefix)
        return sorted(o["Key"] for o in response.get("Contents", []) or [])

    def table(self):
        return boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)

    def native(self, value):
        return json.loads(json.dumps(value, default=float))

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

    # convenience wrappers ------------------------------------------------
    def create_session(self, node_id=None):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions", self.saver,
            body={"workflow_id": self.workflow_id,
                  "node_id": node_id or self.node_id})
        assert status in (200, 201), body
        return body

    def session_id(self, node_id=None):
        return self.create_session(node_id)["session"]["sessionId"]

    def view_session(self, session_id, user=None):
        return self.call("GET", "/workflow-tuning/anomaly/sessions/{id}",
                         user or self.saver, {"id": session_id})

    def refresh(self, session_id):
        return self.call("POST",
                         "/workflow-tuning/anomaly/sessions/{id}/refresh",
                         self.saver, {"id": session_id})

    def samples(self, session_id, query=None):
        return self.call("GET",
                         "/workflow-tuning/anomaly/sessions/{id}/samples",
                         self.saver, {"id": session_id}, query=query)

    def set_labels(self, session_id, sample_ids, label):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            self.saver, {"id": session_id},
            body={"sampleIds": sample_ids, "label": label})

    def toggle_synthetic(self, session_id, enabled):
        return self.call(
            "PUT",
            "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
            self.saver, {"id": session_id}, body={"enabled": enabled})

    def candidate(self, session_id, name="Rewritten prompt",
                  prompt=NEW_PROMPT, system_prompt=NEW_SYSTEM,
                  max_tokens=NEW_MAX_TOKENS):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            self.saver, {"id": session_id},
            body={"name": name, "prompt": prompt,
                  "systemPrompt": system_prompt, "maxTokens": max_tokens})
        assert status == 201, body
        return body["candidate"]["candidateId"]

    def start_run(self, session_id, candidate_id, repeats=None, device=None):
        body = {"candidateId": candidate_id}
        if repeats is not None:
            body["repeats"] = repeats
        if device is not None:
            body["deviceThingName"] = device
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            self.saver, {"id": session_id}, body=body)

    def get_run(self, run_id):
        return self.call("GET", "/workflow-tuning/anomaly/score-runs/{rid}",
                         self.saver, {"rid": run_id})

    def outcomes(self, run_id, query=None):
        return self.call(
            "GET", "/workflow-tuning/anomaly/score-runs/{rid}/outcomes",
            self.saver, {"rid": run_id}, query=query)

    def cancel(self, run_id):
        return self.call(
            "POST", "/workflow-tuning/anomaly/score-runs/{rid}/cancel",
            self.saver, {"rid": run_id})

    def diff(self, run_id, other):
        return self.call(
            "GET",
            "/workflow-tuning/anomaly/score-runs/{rid}/diff/{other}",
            self.saver, {"rid": run_id, "other": other})

    def select(self, session_id, candidate_id):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            self.saver, {"id": session_id},
            body={"candidateId": candidate_id})

    def apply(self, session_id, body=None):
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            self.saver, {"id": session_id}, body=body or {})

    def delete_session(self, session_id):
        return self.call("DELETE",
                         "/workflow-tuning/anomaly/sessions/{id}",
                         self.saver, {"id": session_id})

    # ------------------------------------------------------------ steps
    def step(self, payload=None):
        """Run one dispatched action inline, as Lambda would."""
        if payload is None:
            assert self.dispatched, "no action was dispatched"
            payload = self.dispatched.pop(0)
        return self.module.handler(payload, None)

    def drive(self, limit=60):
        results = []
        while self.dispatched:
            assert len(results) < limit, "the step chain did not terminate"
            results.append(self.step())
        return results

    # ------------------------------------------------------------ store
    def run_item(self, session_id, run_id):
        return self.native(self.table().get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": f"RUN#{run_id}"}).get("Item"))

    def outcome_items(self, run_id):
        return self.native(self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(f"RUN#{run_id}")
                & boto3.dynamodb.conditions.Key("sk").begins_with("OUT#")),
        ).get("Items", []))

    def partition(self, pk):
        return self.native(self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(pk)),
        ).get("Items", []))

    def rewind_run(self, session_id, run_id, seconds):
        """Move a running run's clocks into the past, so the 60-minute
        resume budget and the 15-minute silence timer are reachable
        without waiting."""
        run = self.run_item(session_id, run_id)
        started = int(run["startedAt"]) - seconds
        self.table().update_item(
            Key={"pk": f"SESSION#{session_id}", "sk": f"RUN#{run_id}"},
            UpdateExpression="SET startedAt = :s, lastProgressAt = :s",
            ExpressionAttributeValues={":s": started})

    def audit_events(self, action=None):
        items = self.stack.tables.audit_log.scan().get("Items", [])
        events = [self.native(i) for i in items]
        events = [e for e in events
                  if (e.get("details") or {}).get("usecase_id")
                  == self.usecase_id]
        if action:
            events = [e for e in events if e.get("action") == action]
        return events


@pytest.fixture
def env(aws_stack, tuning, monkeypatch):
    return Env(aws_stack, tuning, monkeypatch)


@pytest.fixture
def bedrock_env(env):
    """A workflow whose anomaly-mode bedrock node is the Tunable_Node, with
    the VLM node as its sibling Inspection_Node."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, CAMERA_NODE], [CONNECTION])
    env.node_id = BEDROCK_NODE["id"]
    return env


@pytest.fixture
def llm_env(env):
    """A session on the anomaly-mode VLM node (scored by a device)."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, CAMERA_NODE], [CONNECTION])
    env.node_id = LLM_NODE["id"]
    env.session = env.session_id(LLM_NODE["id"])
    env.candidate_id = env.candidate(env.session)
    return env


# ==========================================================================
# 1. Portal end-to-end (moto + stubbed Bedrock)
# ==========================================================================

class TestPortalEndToEnd:
    """Requirements 3.1, 5.1, 6.8, 8.1, 8.3, 10.5: seed → session → refresh
    → label → synthetic negatives → score → compare → select → apply."""

    def test_two_devices_and_two_versions_through_apply(self, bedrock_env,
                                                        monkeypatch):
        env = bedrock_env
        # -- exported samples: two devices, two definition versions ------
        # dev-1 ran version 1 (the deployed prompt); dev-2 ran version 2
        # with a different prompt, so its samples are flagged.
        a = env.export_sample(thing="dev-1", execution="exec-a",
                              exported_at=1000)
        b = env.export_sample(thing="dev-1", execution="exec-b",
                              exported_at=1100, is_anomalous=False)
        # The sibling Inspection_Node exported a Reference_Image for the
        # same execution on the same device -> exec-a gains a synthetic
        # negative when the toggle is enabled.
        env.export_sample(thing="dev-1", execution="exec-a",
                          node_id=LLM_NODE["id"], exported_at=1000)

        # -- the session indexes what exists at creation -----------------
        created = env.create_session()
        session_id = created["session"]["sessionId"]
        assert created["session"]["baselineFingerprint"] == \
            BASELINE_FINGERPRINT
        assert created["refresh"]["indexed"] == 2

        # -- more samples arrive, including two the index must skip ------
        c = env.export_sample(thing="dev-2", execution="exec-c",
                              exported_at=1200,
                              fingerprint="sha256:other-prompt")
        env.export_sample(thing="dev-2", execution="exec-d",
                          exported_at=1300, write_input=False)
        env.s3.put_object(Bucket=SAMPLE_BUCKET,
                          Key=f"{env.sample_prefix()}dev-2/exec-e.json",
                          Body=b"{not json")

        status, refreshed = env.refresh(session_id)
        assert status == 200, refreshed
        summary = refreshed["refresh"]
        assert summary["indexed"] == 1
        assert summary["discovered"] == 3
        assert summary["skipped"] == {"missing_input": 1, "unreadable": 1}
        assert summary["beyondBound"] == 0

        # -- the samples view: three samples, the v2 one flagged ---------
        status, listed = env.samples(session_id)
        assert status == 200, listed
        views = {s["sampleId"]: s for s in listed["samples"]}
        assert set(views) == {a["sampleId"], b["sampleId"], c["sampleId"]}
        assert views[c["sampleId"]]["differentPrompt"] is True
        assert views[a["sampleId"]]["differentPrompt"] is False
        assert views[a["sampleId"]]["thingName"] == "dev-1"
        assert views[c["sampleId"]]["thingName"] == "dev-2"
        # Images are served as time-limited presigned URLs, never as bytes.
        input_url = views[a["sampleId"]]["input"]["url"]
        assert input_url.startswith("https://")
        assert "Signature=" in input_url and "Expires=" in input_url
        assert views[a["sampleId"]]["reference"]["url"]
        assert views[a["sampleId"]]["singleImage"] is False

        # -- labels ------------------------------------------------------
        status, labelled = env.set_labels(session_id, [a["sampleId"]], "OK")
        assert status == 200, labelled
        assert env.set_labels(session_id, [b["sampleId"]], "NOK")[0] == 200
        assert env.set_labels(session_id, [c["sampleId"]],
                              "EXCLUDE")[0] == 200

        # -- synthetic negatives ----------------------------------------
        status, synthetic = env.toggle_synthetic(session_id, True)
        assert status == 200, synthetic
        assert synthetic["created"] == 1
        synthetic_id = f"{a['sampleId']}|syn|{LLM_NODE['id']}"
        status, listed = env.samples(session_id, {"synthetic": "true"})
        assert [s["sampleId"] for s in listed["samples"]] == [synthetic_id]
        assert listed["samples"][0]["label"] == "NOK"
        assert listed["samples"][0]["sourceSampleId"] == a["sampleId"]

        # -- the Candidate's run, driven inline in two chunks ------------
        candidate_id = env.candidate(session_id)
        monkeypatch.setattr(env.module, "CHUNK_INVOCATIONS", 2)
        bedrock = env.script_bedrock(script={
            "exec-a": NORMAL,          # OK, agrees      -> correct
            "exec-b": ANOMALOUS,       # NOK, agrees     -> correct
        }, default=NORMAL)             # synthetic NOK   -> false_pass
        status, started = env.start_run(session_id, candidate_id)
        assert status == 202, started
        run_id = started["runId"]
        assert started["plannedInvocations"] == 3
        assert started["samples"] == 3        # the EXCLUDE one is not scored
        assert started["mode"] == "bedrock"
        steps = env.drive()
        assert [s["status"] for s in steps] == ["running", "completed"]
        assert steps[0]["issued"] == 2 and steps[0]["cursor"] == 2
        assert len(bedrock.calls) == 3
        assert bedrock.regions == [BEDROCK_NODE["parameters"]["region"]] * 2

        # every request carried the Candidate's prompt with the
        # Verdict_Instruction appended, and the Candidate's system text
        for call in bedrock.calls:
            content = call["messages"][0]["content"]
            assert content[0]["text"] == (NEW_PROMPT + INSTRUCTION_SEPARATOR
                                          + VERDICT_INSTRUCTION)
            assert call["system"] == [{"text": NEW_SYSTEM}]
            assert call["inferenceConfig"]["maxTokens"] == NEW_MAX_TOKENS
            assert call["modelId"] == BEDROCK_NODE["parameters"]["model"]
            # the image labels the executor uses, in its order
            assert [b["text"] for b in content[1:] if "text" in b] == [
                "Input image:", "Reference image:"]

        status, run = env.get_run(run_id)
        assert status == 200, run
        assert run["run"]["status"] == "completed"
        assert run["run"]["summary"]["invocations"] == 3
        assert run["run"]["summary"]["correct"] == 2
        assert run["run"]["summary"]["falsePass"] == 1
        assert run["run"]["summary"]["accuracy"] == pytest.approx(2 / 3)
        candidate_summary = run["run"]["summary"]

        # -- the baseline's run, for comparison -------------------------
        baseline = env.script_bedrock(default=ANOMALOUS)
        status, started_baseline = env.start_run(session_id, "baseline")
        assert status == 202, started_baseline
        baseline_run_id = started_baseline["runId"]
        env.drive()
        assert len(baseline.calls) == 3
        for call in baseline.calls:
            content = call["messages"][0]["content"]
            assert content[0]["text"] == (BASELINE_PROMPT
                                          + INSTRUCTION_SEPARATOR
                                          + VERDICT_INSTRUCTION)
            assert call["system"] == [{"text": BASELINE_SYSTEM}]
            assert call["inferenceConfig"]["maxTokens"] == \
                BASELINE_MAX_TOKENS
        status, baseline_view = env.get_run(baseline_run_id)
        assert baseline_view["run"]["summary"]["falseFail"] == 1
        assert baseline_view["run"]["summary"]["correct"] == 2
        baseline_summary = baseline_view["run"]["summary"]

        # -- compare -----------------------------------------------------
        status, difference = env.diff(run_id, baseline_run_id)
        assert status == 200, difference
        differing = {d["sampleId"]: d for d in difference["differing"]}
        assert set(differing) == {a["sampleId"], synthetic_id}
        assert differing[a["sampleId"]]["a"]["categories"] == ["correct"]
        assert differing[a["sampleId"]]["b"]["categories"] == ["false_fail"]
        assert b["sampleId"] not in differing

        # the false passes are listable on their own (Requirement 7.4)
        status, false_passes = env.outcomes(run_id,
                                           {"category": "false_pass"})
        assert status == 200, false_passes
        assert [o["sampleId"] for o in false_passes["outcomes"]] == [
            synthetic_id]
        assert false_passes["outcomes"][0]["rawAnswer"] == NORMAL

        # -- select and apply -------------------------------------------
        assert env.select(session_id, candidate_id)[0] == 200
        before = json.loads(env.stored_definition(1))
        status, applied = env.apply(session_id)
        assert status == 200, applied
        assert applied["previousVersion"] == 1 and applied["newVersion"] == 2

        after_json = env.stored_definition(2)
        after = json.loads(after_json)
        # exactly the three Prompt_Set parameters of the target node moved
        before_nodes = {n["id"]: n for n in before["nodes"]}
        after_nodes = {n["id"]: n for n in after["nodes"]}
        assert set(before_nodes) == set(after_nodes)
        for node_id, node in after_nodes.items():
            if node_id == BEDROCK_NODE["id"]:
                continue
            assert node == before_nodes[node_id], node_id
        target_before = before_nodes[BEDROCK_NODE["id"]]["parameters"]
        target_after = after_nodes[BEDROCK_NODE["id"]]["parameters"]
        assert target_after["prompt"] == NEW_PROMPT
        assert target_after["system_prompt"] == NEW_SYSTEM
        assert target_after["max_tokens"] == NEW_MAX_TOKENS
        moved = {k for k in set(target_before) | set(target_after)
                 if target_before.get(k) != target_after.get(k)}
        assert moved == {"prompt", "system_prompt", "max_tokens"}
        assert after["connections"] == before["connections"]
        # stored canonically, as the designer save path stores it
        assert after_json == env.canonical(after)
        # nothing was validated, packaged or deployed
        version_item = env.stack.tables.versions.get_item(
            Key={"workflow_id": env.workflow_id, "version": 2})["Item"]
        assert env.native(version_item["validation_status"]) == {
            "status": "none"}
        assert env.native(version_item["compiled_arch_keys"]) == {}
        assert version_item["component_arn"] is None

        # -- the Tuning_Result and the audit event ----------------------
        result = applied["tuningResult"]
        assert result["candidateId"] == candidate_id
        assert result["scoreRunId"] == run_id
        assert result["newVersion"] == 2 and result["previousVersion"] == 1
        assert result["appliedBy"] == env.saver["user_id"]
        assert result["summary"]["correct"] == candidate_summary["correct"]
        assert result["baselineSummary"]["falseFail"] == \
            baseline_summary["falseFail"]
        events = env.audit_events("apply_prompt_tuning")
        assert len(events) == 1
        details = events[0]["details"]
        assert details["workflow_id"] == env.workflow_id
        assert details["version"] == 2
        assert details["previous_version"] == 1
        assert details["node_id"] == BEDROCK_NODE["id"]
        assert details["session_id"] == session_id
        assert details["candidate_id"] == candidate_id
        assert details["score_run_id"] == run_id
        assert events[0]["resource_id"] == env.workflow_id

        # -- the Baseline_Candidate follows the new latest version -------
        status, view = env.view_session(session_id)
        assert status == 200, view
        baseline_candidate = [c for c in view["candidates"]
                              if c["candidateId"] == "baseline"][0]
        assert baseline_candidate["prompt"] == NEW_PROMPT
        assert baseline_candidate["maxTokens"] == NEW_MAX_TOKENS
        assert view["session"]["latestTuningResult"]["newVersion"] == 2

        # -- the runs survive as history --------------------------------
        assert env.get_run(run_id)[0] == 200
        assert env.get_run(baseline_run_id)[0] == 200

    def test_session_delete_removes_run_objects_not_exported_samples(
            self, bedrock_env):
        """Requirement 10.5: deleting a session deletes what the session
        owns and nothing the devices exported."""
        env = bedrock_env
        sample = env.export_sample(thing="dev-1", execution="exec-a")
        session_id = env.session_id()
        assert env.set_labels(session_id, [sample["sampleId"]],
                              "OK")[0] == 200
        run_id = str(uuid.uuid4())
        # A Device_Score_Job's outcome batch, where the device appends it.
        batch_key = (f"{SESSIONS_PREFIX}{session_id}/runs/{run_id}/"
                     f"outcomes-1.json")
        env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=batch_key,
                          Body=json.dumps({"outcomes": []}).encode("utf-8"))
        exported = env.objects_under(env.sample_prefix())
        assert len(exported) == 3      # sidecar + input + reference

        status, deleted = env.delete_session(session_id)
        assert status == 200, deleted
        assert env.objects_under(f"{SESSIONS_PREFIX}{session_id}/") == []
        assert env.objects_under(env.sample_prefix()) == exported
        # the session's items are gone; a new session re-indexes the same
        # samples from the store
        assert env.partition(f"SESSION#{session_id}") == []
        fresh = env.create_session()
        assert fresh["session"]["sessionId"] != session_id
        assert fresh["refresh"]["indexed"] == 1


# ==========================================================================
# 2. Overview, baseline refresh, refresh summary
# ==========================================================================

class TestOverviewAndBaseline:

    def test_overview_counts_samples_per_tunable_node(self, bedrock_env):
        """Requirement 1.2: per-node sample counts over the Sample_Store."""
        env = bedrock_env
        env.export_sample(thing="dev-1", execution="exec-a")
        env.export_sample(thing="dev-2", execution="exec-b")
        env.export_sample(thing="dev-1", execution="exec-c",
                          node_id=LLM_NODE["id"])
        status, body = env.call("GET", "/workflow-tuning/anomaly/workflows",
                                env.saver,
                                query={"usecase_id": env.usecase_id,
                                       "workflow_id": env.workflow_id})
        assert status == 200, body
        entry = body["workflows"][0]
        assert entry["latestVersion"] == 1
        nodes = {n["nodeId"]: n for n in entry["nodes"]}
        # the camera node is not tunable
        assert set(nodes) == {BEDROCK_NODE["id"], LLM_NODE["id"]}
        assert nodes[BEDROCK_NODE["id"]]["sampleCount"] == 2
        assert nodes[BEDROCK_NODE["id"]]["model"] == \
            BEDROCK_NODE["parameters"]["model"]
        assert nodes[LLM_NODE["id"]]["sampleCount"] == 1
        assert nodes[LLM_NODE["id"]]["model"] == \
            LLM_NODE["parameters"]["modelName"]
        assert body["sampleExportEnabled"] is True

    def test_the_baseline_candidate_follows_a_new_definition_version(
            self, bedrock_env):
        """Requirement 5.1: the Baseline_Candidate is the deployed
        Prompt_Set of the latest version, refreshed when it changes."""
        env = bedrock_env
        session_id = env.session_id()
        status, view = env.view_session(session_id)
        baseline = [c for c in view["candidates"]
                    if c["candidateId"] == "baseline"][0]
        assert baseline["prompt"] == BASELINE_PROMPT
        assert baseline["systemPrompt"] == BASELINE_SYSTEM
        assert baseline["maxTokens"] == BASELINE_MAX_TOKENS
        assert baseline["isBaseline"] is True
        first_fingerprint = view["session"]["baselineFingerprint"]
        assert first_fingerprint == BASELINE_FINGERPRINT

        env.put_workflow(
            [{**BEDROCK_NODE,
              "parameters": {**BEDROCK_NODE["parameters"],
                             "prompt": "A completely different prompt.",
                             "max_tokens": 128}},
             LLM_NODE, CAMERA_NODE], [CONNECTION],
            version=2, workflow_id=env.workflow_id)

        # The Baseline_Candidate is re-snapshotted when the session is
        # refreshed (and on a create-or-get of the same session), never
        # silently on a read.
        status, refreshed = env.refresh(session_id)
        assert status == 200, refreshed
        status, view = env.view_session(session_id)
        assert status == 200, view
        baseline = [c for c in view["candidates"]
                    if c["candidateId"] == "baseline"][0]
        assert baseline["prompt"] == "A completely different prompt."
        assert baseline["maxTokens"] == 128
        assert baseline["systemPrompt"] == BASELINE_SYSTEM
        assert view["session"]["baselineVersion"] == 2
        assert view["session"]["baselineFingerprint"] != first_fingerprint
        assert view["session"]["baselineFingerprint"] == prompt_fingerprint(
            "A completely different prompt.", BASELINE_SYSTEM, 128)

    def test_a_refresh_is_additive_and_preserves_labels(self, bedrock_env):
        """Requirement 3.4: a later refresh adds new samples and never
        rewrites an indexed one — its Label survives."""
        env = bedrock_env
        first = env.export_sample(thing="dev-1", execution="exec-a")
        session_id = env.session_id()
        assert env.set_labels(session_id, [first["sampleId"]],
                              "NOK")[0] == 200
        second = env.export_sample(thing="dev-1", execution="exec-b")

        status, refreshed = env.refresh(session_id)
        assert status == 200, refreshed
        assert refreshed["refresh"]["indexed"] == 1
        assert refreshed["refresh"]["discovered"] == 1
        # the already-indexed sample was not even re-read
        assert refreshed["refresh"]["skipped"] == {}
        status, listed = env.samples(session_id)
        labels = {s["sampleId"]: s["label"] for s in listed["samples"]}
        assert labels == {first["sampleId"]: "NOK",
                          second["sampleId"]: None}
        # a second refresh with nothing new is a no-op
        status, again = env.refresh(session_id)
        assert again["refresh"]["indexed"] == 0
        assert again["refresh"]["discovered"] == 0
        assert again["refresh"]["skipped"] == {}
        status, listed = env.samples(session_id)
        assert {s["sampleId"]: s["label"] for s in listed["samples"]} == labels

    def test_a_duplicate_input_is_marked_against_the_earliest_sample(
            self, bedrock_env):
        """Requirement 3.3: same input hash -> marked as a duplicate of the
        earliest sample carrying it."""
        env = bedrock_env
        original = env.export_sample(thing="dev-1", execution="exec-a",
                                     exported_at=1000)
        # A second sample whose input bytes are byte-identical.
        clone = env.export_sample(thing="dev-2", execution="exec-a",
                                  exported_at=2000)
        session_id = env.session_id()
        status, listed = env.samples(session_id)
        assert status == 200, listed
        views = {s["sampleId"]: s for s in listed["samples"]}
        assert views[original["sampleId"]]["duplicateOf"] is None
        assert views[clone["sampleId"]]["duplicateOf"] == \
            original["sampleId"]


# ==========================================================================
# 3. Chunk stepping, cursor, resume, the 60-minute finalize and the prune
# ==========================================================================

class TestScoreRunSteps:

    def _labelled_session(self, env, count):
        session_id = env.session_id()
        ids = []
        for index in range(count):
            sample = env.export_sample(thing="dev-1",
                                       execution=f"exec-{index}",
                                       exported_at=1000 + index)
            ids.append(sample["sampleId"])
        env.refresh(session_id)
        assert env.set_labels(session_id, ids, "NOK")[0] == 200
        return session_id, ids

    def test_the_scorer_steps_in_chunks_and_carries_the_cursor(
            self, bedrock_env, monkeypatch):
        """Requirement 6.8: at most one chunk per step, the next step
        resuming at the cursor, with at most four invocations in flight."""
        env = bedrock_env
        session_id, ids = self._labelled_session(env, 5)
        candidate_id = env.candidate(session_id)
        monkeypatch.setattr(env.module, "CHUNK_INVOCATIONS", 2)
        bedrock = env.script_bedrock(default=ANOMALOUS, delay=0.01)
        status, started = env.start_run(session_id, candidate_id)
        assert status == 202, started
        run_id = started["runId"]
        assert started["plannedInvocations"] == 5

        cursors = []
        issued = []
        while env.dispatched:
            payload = env.dispatched[0]
            cursors.append(payload["cursor"])
            result = env.step()
            issued.append(result.get("issued"))
        assert cursors == [0, 2, 4]
        assert issued == [2, 2, 1]
        assert len(bedrock.calls) == 5
        # The real concurrency bound, not a patched one.
        assert env.module.SCORE_THREADS == 4
        assert bedrock.max_in_flight <= 4
        run = env.run_item(session_id, run_id)
        assert run["status"] == "completed"
        assert run["done"] == 5
        assert len(env.outcome_items(run_id)) == 5
        assert {o["repeat"] for o in env.outcome_items(run_id)} == {1}
        assert env.module.CHUNK_INVOCATIONS == 2   # patched for this case

    def test_the_documented_chunk_and_thread_bounds(self, bedrock_env):
        """The constants the chunking case lowers, pinned literally."""
        module = bedrock_env.module
        assert module.CHUNK_INVOCATIONS == 100
        assert module.SCORE_THREADS == 4
        assert module.RUN_STALE_SECONDS == 3600
        assert module.MAX_RUNS_PER_CANDIDATE == 20

    def test_a_replayed_step_never_re_issues_a_persisted_unit(
            self, bedrock_env, monkeypatch):
        """Requirement 10.4: a resumed (or retried) step re-issues nothing
        that was already persisted."""
        env = bedrock_env
        session_id, _ids = self._labelled_session(env, 4)
        candidate_id = env.candidate(session_id)
        monkeypatch.setattr(env.module, "CHUNK_INVOCATIONS", 2)
        bedrock = env.script_bedrock(default=ANOMALOUS)
        status, started = env.start_run(session_id, candidate_id)
        assert status == 202, started
        run_id = started["runId"]

        first = env.dispatched[0]
        env.step()                       # 2 units persisted
        assert len(env.outcome_items(run_id)) == 2
        calls_after_first = len(bedrock.calls)

        # Lambda retried the very same step event (cursor 0).
        env.step(dict(first))
        outcomes = env.outcome_items(run_id)
        assert len(outcomes) == 4        # the remaining two, not repeats
        assert len(bedrock.calls) == calls_after_first + 2
        # every Converse call produced exactly one persisted outcome
        assert len(bedrock.calls) == len(outcomes)
        keys = {(o["sampleId"], o["repeat"]) for o in outcomes}
        assert len(keys) == 4
        env.drive()
        assert env.run_item(session_id, run_id)["status"] == "completed"
        assert len(env.outcome_items(run_id)) == 4

    def test_a_run_that_cannot_finish_in_60_minutes_finalizes_as_failed(
            self, bedrock_env, monkeypatch):
        """Requirement 10.4: the 60-minute budget finalizes the run with
        its partial Score_Summary and frees the session's run slot."""
        env = bedrock_env
        session_id, _ids = self._labelled_session(env, 4)
        candidate_id = env.candidate(session_id)
        monkeypatch.setattr(env.module, "CHUNK_INVOCATIONS", 2)
        env.script_bedrock(default=ANOMALOUS)
        status, started = env.start_run(session_id, candidate_id)
        assert status == 202, started
        run_id = started["runId"]
        env.step()                        # 2 of 4 units persisted
        assert len(env.outcome_items(run_id)) == 2

        env.rewind_run(session_id, run_id, 3601)
        env.step()                        # the next step gives up

        run = env.run_item(session_id, run_id)
        assert run["status"] == "failed"
        assert "60 minutes" in run["error"]
        # the partial summary is a function of what was produced
        assert run["summary"]["invocations"] == 2
        assert run["done"] == 2
        assert len(env.outcome_items(run_id)) == 2
        # the slot is free: another run may start
        status, again = env.start_run(session_id, candidate_id)
        assert status == 202, again

    def test_only_the_20_most_recent_runs_of_a_candidate_are_kept(
            self, bedrock_env):
        """Requirement 10.3: the prune runs on finalize and takes the
        pruned runs' outcomes with them."""
        env = bedrock_env
        session_id, _ids = self._labelled_session(env, 1)
        candidate_id = env.candidate(session_id)
        # 20 older terminal runs of the same Candidate, each with one
        # outcome, seeded directly (driving 20 runs would only be slower).
        older = []
        for index in range(20):
            run_id = str(uuid.uuid4())
            older.append(run_id)
            env.table().put_item(Item=json.loads(json.dumps({
                "pk": f"SESSION#{session_id}", "sk": f"RUN#{run_id}",
                "runId": run_id, "sessionId": session_id,
                "usecaseId": env.usecase_id, "workflowId": env.workflow_id,
                "nodeId": env.node_id, "candidateId": candidate_id,
                "status": "completed", "mode": "bedrock", "repeats": 1,
                "plannedInvocations": 1, "done": 1,
                "startedAt": 100 + index, "finishedAt": 200 + index,
                "summary": {"invocations": 1, "correct": 1},
            }), parse_float=Decimal))
            env.table().put_item(Item={"pk": f"RUN#{run_id}", "sk": "META",
                                       "runId": run_id,
                                       "sessionId": session_id})
            env.table().put_item(Item={"pk": f"RUN#{run_id}",
                                       "sk": "OUT#s#1",
                                       "runId": run_id,
                                       "sampleId": "dev-1/exec-0",
                                       "repeat": 1, "category": "correct"})

        env.script_bedrock(default=ANOMALOUS)
        status, started = env.start_run(session_id, candidate_id)
        assert status == 202, started
        env.drive()

        runs = [r for r in env.partition(f"SESSION#{session_id}")
                if str(r["sk"]).startswith("RUN#")]
        assert len(runs) == 20
        kept = {r["runId"] for r in runs}
        assert started["runId"] in kept
        # the oldest run was pruned with its outcomes and its pointer
        pruned = older[0]
        assert pruned not in kept
        assert env.partition(f"RUN#{pruned}") == []
        assert env.get_run(pruned)[0] == 404
        # the newest of the older runs survived
        assert older[-1] in kept


# ==========================================================================
# 4. VLM dispatch (moto + stubbed iot-data)
# ==========================================================================

class TestVlmDispatch:
    """Requirements 6.9, 6.11, 6.12: the manifest and shadow document, the
    exactly-once ingestion and every finalization path."""

    def _prepare(self, llm_env, count=2, device="dev-a"):
        env = llm_env
        ids = []
        for index in range(count):
            sample = env.export_sample(thing=device,
                                       execution=f"exec-{index}",
                                       exported_at=1000 + index,
                                       node_id=LLM_NODE["id"])
            ids.append(sample["sampleId"])
        env.refresh(env.session)
        assert env.set_labels(env.session, ids[:1], "OK")[0] == 200
        if len(ids) > 1:
            assert env.set_labels(env.session, ids[1:], "NOK")[0] == 200
        env.deploy_workflow_to([device])
        return ids

    def _start(self, llm_env, repeats=None, device="dev-a"):
        status, started = llm_env.start_run(llm_env.session,
                                           llm_env.candidate_id,
                                           repeats=repeats, device=device)
        assert status == 202, started
        run = llm_env.run_item(llm_env.session, started["runId"])
        return started, run

    def _batch(self, env, session_id, run_id, name, outcomes,
               thing="dev-a"):
        key = f"{SESSIONS_PREFIX}{session_id}/runs/{run_id}/{name}"
        env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=key,
                          Body=json.dumps({"schemaVersion": 1,
                                           "thingName": thing,
                                           "outcomes": outcomes}
                                          ).encode("utf-8"))
        return key

    def test_the_manifest_and_the_shadow_document(self, llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env, repeats=2)
        job_id = run["jobId"]

        # the desired state: exactly this job, with its manifest key
        assert env.iot.updates[0]["thingName"] == "dev-a"
        assert env.iot.updates[0]["shadowName"] == TUNING_SHADOW
        assert env.iot.updates[0]["document"] == {"state": {"desired": {
            "jobs": {job_id: {"manifestKey": f"{JOBS_PREFIX}{job_id}/"
                                             f"manifest.json",
                              "cancel": False}}}}}

        body = env.s3.get_object(Bucket=SAMPLE_BUCKET,
                                 Key=run["manifestKey"])["Body"].read()
        manifest = json.loads(body.decode("utf-8"))
        assert manifest["jobId"] == job_id
        assert manifest["runId"] == started["runId"]
        assert manifest["sessionId"] == env.session
        assert manifest["nodeId"] == LLM_NODE["id"]
        assert manifest["nodeType"] == "llm_inference"
        assert manifest["repeats"] == 2
        assert manifest["promptSet"] == {"prompt": NEW_PROMPT,
                                         "systemPrompt": NEW_SYSTEM,
                                         "maxTokens": NEW_MAX_TOKENS}
        assert {s["sampleId"] for s in manifest["samples"]} == set(ids)
        for entry in manifest["samples"]:
            assert entry["inputKey"].endswith(".input.jpg")
            assert entry["referenceKey"].endswith(".reference.jpg")
            assert entry["label"] in ("OK", "NOK")
            # no bytes, no hashes, no presigned URLs (Requirement 9.6)
            assert set(entry) <= {"sampleId", "inputKey", "referenceKey",
                                  "label", "metadataSnippet"}
        serialized = json.dumps(manifest)
        assert "X-Amz-Signature" not in serialized
        assert "sha256" not in serialized
        # a poll step was queued, not a Bedrock step
        assert env.dispatched == [{"action": "poll_score_job",
                                  "run_id": started["runId"],
                                  "session_id": env.session,
                                  "immediate": True}]

    def test_outcome_batches_are_ingested_exactly_once(self, llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        first = [{"sampleId": ids[0], "repeat": 1, "label": "OK",
                  "category": "correct", "isAnomalous": False,
                  "confidence": 0.8, "rawAnswer": NORMAL,
                  "outputTokens": 20, "latencyMs": 900}]
        key = self._batch(env, env.session, run_id, "outcomes-1.json", first)
        env.iot.report("dev-a", run["jobId"], {"status": "running",
                                              "done": 1, "total": 2})
        env.step()                          # the immediate poll
        assert len(env.outcome_items(run_id)) == 1

        # the same batch object, re-read: nothing changes
        env.step()
        assert len(env.outcome_items(run_id)) == 1
        assert env.run_item(env.session, run_id)["ingestedBatches"] == [key]

        # the same (sample, repeat) re-sent under a NEW batch name with a
        # DIFFERENT verdict: the first outcome stands, it is not overwritten
        contradiction = [dict(first[0], category="false_fail",
                              isAnomalous=True, rawAnswer=ANOMALOUS,
                              confidence=0.99)]
        self._batch(env, env.session, run_id, "outcomes-1-again.json",
                    contradiction)
        env.step()
        outcomes = env.outcome_items(run_id)
        assert len(outcomes) == 1
        stored = outcomes[0]
        assert stored["rawAnswer"] == NORMAL
        assert stored["category"] == "correct"
        assert stored["isAnomalous"] is False
        assert stored["thingName"] == "dev-a"
        assert "ttl" in stored
        # the run's own summary agrees: one invocation, not two
        status, view = env.get_run(run_id)
        assert view["run"]["summary"]["invocations"] == 1
        assert view["run"]["summary"]["correct"] == 1
        env.dispatched.clear()

    def test_the_completed_path(self, llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        self._batch(env, env.session, run_id, "outcomes-1.json", [
            {"sampleId": ids[0], "repeat": 1, "label": "OK",
             "category": "correct", "isAnomalous": False},
            {"sampleId": ids[1], "repeat": 1, "label": "NOK",
             "category": "false_pass", "isAnomalous": False}])
        env.iot.report("dev-a", run["jobId"], {"status": "completed",
                                              "done": 2, "total": 2})
        result = env.step()
        assert result["status"] == "completed"
        stored = env.run_item(env.session, run_id)
        assert stored["status"] == "completed"
        assert stored["done"] == 2
        assert stored["summary"]["correct"] == 1
        assert stored["summary"]["falsePass"] == 1
        # the job is removed from desired, and polling stopped
        assert env.iot.desired_jobs("dev-a") == {}
        assert not env.dispatched

    def test_a_reported_failure_fails_the_run_with_the_reason(self,
                                                              llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        self._batch(env, env.session, run_id, "outcomes-1.json", [
            {"sampleId": ids[0], "repeat": 1, "label": "OK",
             "category": "correct", "isAnomalous": False}])
        env.iot.report("dev-a", run["jobId"],
                       {"status": "failed", "done": 1, "total": 2,
                        "error": "the manifest could not be read"})
        result = env.step()
        assert result["status"] == "failed"
        stored = env.run_item(env.session, run_id)
        assert stored["status"] == "failed"
        assert "the manifest could not be read" in stored["error"]
        # what the device did produce is kept and summarized
        assert stored["summary"]["invocations"] == 1
        assert env.iot.desired_jobs("dev-a") == {}
        assert not env.dispatched

    def test_a_short_completion_fails_the_run_naming_the_counts(self,
                                                                llm_env):
        env = llm_env
        self._prepare(env, count=2)
        started, run = self._start(env)
        env.iot.report("dev-a", run["jobId"], {"status": "completed",
                                              "done": 1, "total": 2})
        result = env.step()
        assert result["status"] == "failed"
        stored = env.run_item(env.session, started["runId"])
        assert "1 of 2" in stored["error"]

    def test_fifteen_minutes_of_silence_fails_the_run(self, llm_env):
        env = llm_env
        self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        # one poll with no report at all: the run keeps waiting
        result = env.step()
        assert result["status"] == "running"
        assert env.dispatched, "polling must continue while the job lives"

        env.rewind_run(env.session, run_id, 901)
        result = env.step()
        assert result["status"] == "failed"
        stored = env.run_item(env.session, run_id)
        assert stored["status"] == "failed"
        assert "15 minutes" in stored["error"]
        assert env.iot.desired_jobs("dev-a") == {}
        assert not env.dispatched
        assert env.module.JOB_SILENCE_SECONDS == 900

    def test_the_cancelled_path_keeps_what_the_device_produced(self,
                                                               llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        self._batch(env, env.session, run_id, "outcomes-1.json", [
            {"sampleId": ids[0], "repeat": 1, "label": "OK",
             "category": "correct", "isAnomalous": False}])
        env.step()                          # ingest the first batch
        assert len(env.outcome_items(run_id)) == 1

        status, cancelled = env.cancel(run_id)
        assert status == 200, cancelled
        assert cancelled["run"]["status"] == "cancelled"
        # the device was asked to stop through its desired state
        asked = [u for u in env.iot.updates
                 if (u["document"]["state"]["desired"]["jobs"]
                     .get(run["jobId"]) or {}).get("cancel") is True]
        assert asked, "the cancellation must reach the shadow"

        # the trailing batch the device had already written is still
        # ingested, and the summary refreshed, before polling stops
        self._batch(env, env.session, run_id, "outcomes-2.json", [
            {"sampleId": ids[1], "repeat": 1, "label": "NOK",
             "category": "correct", "isAnomalous": True}])
        env.drive()
        stored = env.run_item(env.session, run_id)
        assert stored["status"] == "cancelled"
        assert len(env.outcome_items(run_id)) == 2
        assert stored["summary"]["invocations"] == 2
        assert env.iot.desired_jobs("dev-a") == {}
        # the slot is free again
        status, again = env.start_run(env.session, env.candidate_id,
                                      device="dev-a")
        assert status == 202, again

    def test_an_unreadable_batch_does_not_stop_the_poll(self, llm_env):
        env = llm_env
        ids = self._prepare(env, count=2)
        started, run = self._start(env)
        run_id = started["runId"]
        env.s3.put_object(
            Bucket=SAMPLE_BUCKET,
            Key=f"{SESSIONS_PREFIX}{env.session}/runs/{run_id}/"
                f"outcomes-1.json",
            Body=b"{half written")
        self._batch(env, env.session, run_id, "outcomes-2.json", [
            {"sampleId": ids[0], "repeat": 1, "label": "OK",
             "category": "correct", "isAnomalous": False}])
        result = env.step()
        assert result["status"] == "running"
        assert result["ingested"]["errors"] == 1
        assert result["ingested"]["outcomes"] == 1
        assert len(env.outcome_items(run_id)) == 1
        assert env.dispatched
        env.dispatched.clear()
