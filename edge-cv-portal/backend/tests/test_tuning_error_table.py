"""
Anomaly_Tuning Portal API error table: every row, exactly as the design
states it (spec: .kiro/specs/quality-prompt-tuning, task 6.5,
design.md "Error Handling → Portal API").

Deterministic, enumerated cases over the real ``functions/workflow_tuning.py``
handler against moto, with doubles only on the module's own client seams (a
recording ``dispatch_action`` so a Bedrock step can be driven inline, a
scripted Converse client and a fake ``iot-data`` with named-shadow merge
semantics).

The design's table is transcribed into :data:`ERROR_TABLE` below and each row
is covered by at least one case marked with :func:`covers`;
``test_every_row_of_the_error_table_is_covered`` fails if a row loses its
case (or if a case names a row the table does not have), so the table and the
tests cannot drift apart silently.

Task 6.1-6.3's files assert these statuses inside their own subject areas;
this file asserts the table AS a table — one place where the whole contract
is visible — and adds the parts a per-area file has no reason to check: that
the uniform 404 is byte-identical for a missing and for an unreadable
workflow across every shape of route, that every 403 is audited with the
permission it required, and that a dispatch failure (shadow or S3) leaves
nothing behind but a failed run.

Expectations are stated literally here (codes, messages, bounds and the
Verdict_Instruction are restated, never imported), so a change in the handler
cannot move both the code and its expectation together.
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

#: The named shadow Device_Score_Jobs travel on, restated from the device
#: runner (src/backend/workflow_engine/tuning/job_runner.py).
TUNING_SHADOW = "dda-workflow-tuning"

ANOMALOUS_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'

#: The bounds the table's rows state, restated from the design (Requirements
#: 6.13, 6.10).
MAX_PLANNED_INVOCATIONS = 600
MIN_REPEATS, MAX_REPEATS = 1, 3


# --------------------------------------------------------------------------
# The design's Portal API error table, transcribed
# --------------------------------------------------------------------------

ERROR_TABLE = {
    "not_readable_or_missing": (
        "Not readable / workflow missing", "404",
        "the uniform not-found response"),
    "readable_lacking_edit_or_save": (
        "Readable, lacking edit/save", "403",
        "the forbidden response, audited"),
    "node_not_tunable": (
        "Node not tunable in the latest version", "400 (create) / 409 (apply)",
        "names node type and anomaly_mode"),
    "export_disabled": (
        "Export disabled for the Use_Case", "200",
        "sampleExportEnabled: false so the UI explains"),
    "baseline_read_only": (
        "Baseline edit/delete", "409",
        "'the baseline candidate is read-only'"),
    "second_run_in_a_session": (
        "Second run in a session", "409",
        "names the in-progress runId"),
    "run_bounds": (
        "Planned invocations > 600 / repeats outside 1..3", "400",
        "states the bound"),
    "no_eligible_device": (
        "VLM run without an eligible device", "400",
        "lists devices that exported samples but do not report the "
        "registration"),
    "apply_without_completed_run": (
        "Apply without a completed run on the selection", "409",
        "Requirement 8.2"),
    "dispatch_failure": (
        "Shadow update / S3 failure during dispatch", "200",
        "run failed with the botocore reason; nothing else changes"),
    "bedrock_error_per_sample": (
        "Bedrock throttling/errors per sample", "n/a",
        "invocation_error outcome with the error class; the run continues"),
}

#: Rows a test in this file claims to cover (filled by :func:`covers` at
#: collection time).
_COVERED = set()


def covers(row):
    """Mark a test as covering one row of :data:`ERROR_TABLE`."""
    assert row in ERROR_TABLE, f"'{row}' is not a row of the error table"

    def decorate(func):
        _COVERED.add(row)
        func.error_table_row = row
        return func

    return decorate


# --------------------------------------------------------------------------
# Workflow fixtures
# --------------------------------------------------------------------------

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
        "prompt_template": "Inspect the plate.",
        "max_tokens": 512,
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
    """A Converse client that answers (or raises) per request."""

    def __init__(self, answers=None, default=ANOMALOUS_ANSWER):
        self.answers = list(answers or [])
        self.default = default
        self.calls = []
        self._lock = threading.Lock()

    def converse(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, BaseException):
            raise answer
        return {"output": {"message": {"content": [{"text": answer}]}},
                "usage": {"outputTokens": 12}}


class FakeIotData:
    """A named-shadow store with the real service's merge semantics (a
    nested ``None`` deletes its key), optionally failing writes."""

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

    def desired_jobs(self, thing):
        state = self.shadows.get((thing, TUNING_SHADOW)) or {}
        return (state.get("desired") or {}).get("jobs") or {}


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
    """One Use_Case + workflow + users, with the module's client seams
    replaced by recording doubles."""

    def __init__(self, stack, module, monkeypatch):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.put_usecase()
        # DataScientist: workflow read + edit + save. Viewer: read only.
        # Operator: read, no edit/save. DataLabeler: no workflow:read.
        self.saver = self._user("DataScientist")
        self.reader = self._user("Viewer")
        self.operator = self._user("Operator")
        self.outsider = self._user("DataLabeler")
        self.workflow_id = None
        self.node_id = None
        self.dispatched = []
        self.bedrock = FakeBedrock()
        self.iot = FakeIotData()

        monkeypatch.setattr(module, "dispatch_action",
                            lambda payload: self.dispatched.append(
                                dict(payload)))
        monkeypatch.setattr(module, "bedrock_client",
                            lambda region: self.bedrock)
        monkeypatch.setattr(module, "iot_data_client",
                            lambda usecase: self.iot)
        monkeypatch.setattr(module, "POLL_INTERVAL_SECONDS", 0)

    # ------------------------------------------------------------- setup
    def _user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def put_usecase(self, export=True, bucket=None):
        item = {"usecase_id": self.usecase_id, "name": "Tuning use case",
                "account_id": ACCOUNT_ID, "tuning_sample_export": export}
        if bucket is not None:
            item["inference_uploader_s3_bucket"] = bucket
        self.stack.tables.usecases.put_item(Item=item)

    def put_workflow(self, nodes, version=1, workflow_id=None):
        workflow_id = workflow_id or str(uuid.uuid4())
        document = {"schemaVersion": 1, "nodes": nodes, "connections": []}
        key = (f"workflows/{self.usecase_id}/{workflow_id}/versions/"
               f"{version}/workflow.json")
        self.s3.put_object(Bucket="test-portal-artifacts", Key=key,
                           Body=json.dumps(document).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID,
            "name": "Tuning workflow",
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
        self.stack.tables.deployments.put_item(Item={
            "deployment_id": f"dep-{uuid.uuid4()}",
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "deployment_status": "IN_PROGRESS",
            "component_type": "workflow",
            "workflow_id": self.workflow_id,
            "target_devices": list(devices),
        })

    # ----------------------------------------------------- sample store
    def table(self):
        return boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)

    def seed_sample(self, session_id, label="OK", thing="dev-1",
                    execution=None, exported_at=1000):
        """One indexed Tuning_Sample with its image objects."""
        execution = execution or f"exec-{uuid.uuid4()}"
        base = (f"{SAMPLES_PREFIX}{self.workflow_id}/{self.node_id}/"
                f"{thing}/{execution}")
        sample_id = f"{thing}/{execution}"
        input_bytes = f"INPUT-{execution}".encode()
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                           Body=input_bytes)
        item = {
            "pk": f"SESSION#{session_id}", "sk": f"SAMPLE#{sample_id}",
            "sampleId": sample_id,
            "sidecar": {"schemaVersion": 1, "executionId": execution,
                        "thingName": thing, "exportedAt": exported_at},
            "sidecarKey": base + ".json",
            "workflowId": self.workflow_id, "nodeId": self.node_id,
            "nodeType": "bedrock_inference", "thingName": thing,
            "executionId": execution, "version": 1,
            "exportedAt": exported_at, "source": "live",
            "inputKey": base + ".input.jpg",
            "inputSha256": hashlib.sha256(input_bytes).hexdigest(),
            "referenceKey": None,
            "recordedIsAnomalous": True,
            "promptFingerprint": "sha256:baseline",
            "duplicateOf": None, "differentPrompt": False,
            "synthetic": False, "indexedAt": exported_at,
        }
        if label is not None:
            item["label"] = label
        self.table().put_item(
            Item=json.loads(json.dumps(item), parse_float=Decimal))
        return sample_id

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
            "POST", "/workflow-tuning/anomaly/sessions", self.saver,
            body={"workflow_id": self.workflow_id,
                  "node_id": node_id or self.node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def candidate(self, session_id, name="Candidate A"):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            self.saver, {"id": session_id},
            body={"name": name, "prompt": "Is this plate defective?",
                  "systemPrompt": "Answer as an inspector.",
                  "maxTokens": 256})
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
            user or self.saver, {"id": session_id}, body=body)

    def step(self):
        assert self.dispatched, "no action was dispatched"
        return self.module.handler(self.dispatched.pop(0), None)

    def drive(self, limit=40):
        results = []
        while self.dispatched:
            assert len(results) < limit, "the step chain did not terminate"
            results.append(self.step())
        return results

    def run_item(self, session_id, run_id):
        return json.loads(json.dumps(self.table().get_item(
            Key={"pk": f"SESSION#{session_id}",
                 "sk": f"RUN#{run_id}"}).get("Item"), default=float))

    def outcome_items(self, run_id):
        items = self.table().query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("pk").eq(f"RUN#{run_id}")
                & boto3.dynamodb.conditions.Key("sk").begins_with("OUT#")),
        ).get("Items", [])
        return json.loads(json.dumps(items, default=float))

    def seed_completed_run(self, session_id, candidate_id,
                           status="completed"):
        """A Score_Run item (plus its by-id pointer) in a terminal state."""
        run_id = str(uuid.uuid4())
        item = {
            "pk": f"SESSION#{session_id}", "sk": f"RUN#{run_id}",
            "runId": run_id, "sessionId": session_id,
            "usecaseId": self.usecase_id, "workflowId": self.workflow_id,
            "nodeId": self.node_id, "candidateId": candidate_id,
            "status": status, "mode": "bedrock", "repeats": 1,
            "plannedInvocations": 2, "done": 2,
            "startedAt": 1000, "finishedAt": 1010,
            "summary": {"samples": 2, "invocations": 2, "correct": 2,
                        "falsePass": 0, "falseFail": 0, "parseFailure": 0,
                        "invocationError": 0, "accuracy": 1.0,
                        "unstable": 0},
        }
        self.table().put_item(
            Item=json.loads(json.dumps(item), parse_float=Decimal))
        self.table().put_item(Item={"pk": f"RUN#{run_id}", "sk": "META",
                                    "runId": run_id,
                                    "sessionId": session_id})
        return run_id

    def audit_events(self, action=None):
        items = self.stack.tables.audit_log.scan().get("Items", [])
        events = [json.loads(json.dumps(i, default=float)) for i in items]
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
    """A session on the anomaly-mode bedrock node with one Candidate."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, PLAIN_NODE])
    env.node_id = BEDROCK_NODE["id"]
    env.session = env.session_id()
    env.candidate_id = env.candidate(env.session)
    return env


@pytest.fixture
def llm_env(env):
    """A session on the anomaly-mode VLM node with one Candidate."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, PLAIN_NODE])
    env.node_id = LLM_NODE["id"]
    env.session = env.session_id(LLM_NODE["id"])
    env.candidate_id = env.candidate(env.session)
    return env


# ==========================================================================
# Row: Not readable / workflow missing -> 404
# ==========================================================================

class TestNotReadableOrMissing:
    """The uniform 404 that never confirms whether a workflow, session,
    candidate or run exists (Requirement 9.1)."""

    #: Every workflow-scoped route shape, as (method, resource, params-key).
    ROUTES = [
        ("GET", "/workflow-tuning/anomaly/sessions/{id}", "id"),
        ("DELETE", "/workflow-tuning/anomaly/sessions/{id}", "id"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/refresh", "id"),
        ("GET", "/workflow-tuning/anomaly/sessions/{id}/samples", "id"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
         "id"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
         "id"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/candidates", "id"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs", "id"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/selection", "id"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/apply", "id"),
    ]

    @covers("not_readable_or_missing")
    @pytest.mark.parametrize("method,resource,key", ROUTES)
    def test_unreadable_and_missing_answer_the_same_404(self, bedrock_env,
                                                        method, resource,
                                                        key):
        """A caller without ``workflow:read`` and a session that does not
        exist must be indistinguishable, byte for byte."""
        unknown = str(uuid.uuid4())
        missing = bedrock_env.call(method, resource, bedrock_env.saver,
                                  {key: unknown}, body={})
        unreadable = bedrock_env.call(method, resource, bedrock_env.outsider,
                                      {key: bedrock_env.session}, body={})
        assert missing[0] == 404, missing
        assert missing == unreadable
        assert missing[1] == {"error": {"code": "WORKFLOW_NOT_FOUND",
                                        "message": "Workflow not found",
                                        "details": {}}}

    @covers("not_readable_or_missing")
    def test_the_missing_workflow_on_create_is_the_same_404(self, env):
        env.put_workflow([BEDROCK_NODE])
        env.node_id = BEDROCK_NODE["id"]
        missing = env.call("POST", "/workflow-tuning/anomaly/sessions",
                           env.saver,
                           body={"workflow_id": str(uuid.uuid4()),
                                 "node_id": "bedrock_1"})
        unreadable = env.call("POST", "/workflow-tuning/anomaly/sessions",
                              env.outsider,
                              body={"workflow_id": env.workflow_id,
                                    "node_id": "bedrock_1"})
        assert missing[0] == 404 and missing == unreadable

    @covers("not_readable_or_missing")
    def test_an_unknown_run_and_candidate_answer_the_uniform_404(
            self, bedrock_env):
        for method, resource, params in [
            ("GET", "/workflow-tuning/anomaly/score-runs/{rid}",
             {"rid": str(uuid.uuid4())}),
            ("GET", "/workflow-tuning/anomaly/score-runs/{rid}/outcomes",
             {"rid": str(uuid.uuid4())}),
            ("POST", "/workflow-tuning/anomaly/score-runs/{rid}/cancel",
             {"rid": str(uuid.uuid4())}),
            ("GET", "/workflow-tuning/anomaly/candidates/{cid}/preview",
             {"cid": str(uuid.uuid4())}),
        ]:
            status, body = bedrock_env.call(method, resource,
                                           bedrock_env.saver, params,
                                           body={},
                                           query={"session_id":
                                                  bedrock_env.session})
            assert status == 404, (resource, body)
            assert body["error"]["code"] in ("WORKFLOW_NOT_FOUND",
                                            "CANDIDATE_NOT_FOUND")

    @covers("not_readable_or_missing")
    def test_the_overview_answers_the_uniform_404_to_a_non_reader(self, env):
        env.put_workflow([BEDROCK_NODE])
        status, body = env.call("GET", "/workflow-tuning/anomaly/workflows",
                                env.outsider,
                                query={"usecase_id": env.usecase_id})
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ==========================================================================
# Row: Readable, lacking edit/save -> 403 (audited)
# ==========================================================================

class TestForbidden:

    #: (method, resource, required permission) for every mutating route.
    MUTATIONS = [
        ("DELETE", "/workflow-tuning/anomaly/sessions/{id}", "workflow:edit"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/refresh",
         "workflow:edit"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
         "workflow:edit"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
         "workflow:edit"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
         "workflow:edit"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
         "workflow:edit"),
        ("PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
         "workflow:edit"),
        ("POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
         "workflow:save"),
    ]

    @covers("readable_lacking_edit_or_save")
    @pytest.mark.parametrize("method,resource,permission", MUTATIONS)
    def test_a_reader_is_forbidden_and_audited(self, bedrock_env, method,
                                              resource, permission):
        status, body = bedrock_env.call(method, resource, bedrock_env.reader,
                                       {"id": bedrock_env.session}, body={})
        assert status == 403, body
        assert body["error"]["code"] == "FORBIDDEN"
        assert body["error"]["details"]["required_permissions"] == [permission]
        denials = [e for e in bedrock_env.audit_events("unauthorized_access")
                   if e.get("resource_id") == resource
                   and (e.get("details") or {}).get("required_permissions")
                   == [permission]]
        assert denials, f"the denial of {resource} must be audited"
        assert denials[0]["result"] == "denied"

    @covers("readable_lacking_edit_or_save")
    def test_an_operator_may_read_but_not_save(self, bedrock_env):
        """An Operator holds ``workflow:read`` but neither edit nor save."""
        status, _body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.operator, {"id": bedrock_env.session})
        assert status == 200
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            bedrock_env.operator, {"id": bedrock_env.session}, body={})
        assert status == 403
        assert body["error"]["details"]["required_permissions"] == [
            "workflow:save"]

    @covers("readable_lacking_edit_or_save")
    def test_the_403_precedes_every_other_validation(self, bedrock_env):
        """A reader's mutation is refused before the body is even parsed —
        so the 403 cannot be turned into a 400 by sending nonsense."""
        status, body = bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            bedrock_env.reader, {"id": bedrock_env.session},
            body={"sampleIds": ["nope"], "label": "NOT_A_LABEL"})
        assert status == 403, body
        assert body["error"]["details"]["required_permissions"] == [
            "workflow:edit"]


# ==========================================================================
# Row: Node not tunable -> 400 (create) / 409 (apply)
# ==========================================================================

class TestNodeNotTunable:

    @covers("node_not_tunable")
    @pytest.mark.parametrize("node,expected_type,expected_mode", [
        ({**BEDROCK_NODE, "parameters": {**BEDROCK_NODE["parameters"],
                                         "anomaly_mode": False}},
         "bedrock_inference", False),
        (PLAIN_NODE, "camera_source", None),
    ])
    def test_create_is_400_naming_the_type_and_anomaly_mode(
            self, env, node, expected_type, expected_mode):
        env.put_workflow([node])
        status, body = env.call(
            "POST", "/workflow-tuning/anomaly/sessions", env.saver,
            body={"workflow_id": env.workflow_id, "node_id": node["id"]})
        assert status == 400, body
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        details = body["error"]["details"]
        assert details["nodeType"] == expected_type
        assert details["anomaly_mode"] == expected_mode
        assert details["version"] == 1

    @covers("node_not_tunable")
    def test_create_on_a_node_that_does_not_exist_is_400(self, env):
        env.put_workflow([BEDROCK_NODE])
        status, body = env.call(
            "POST", "/workflow-tuning/anomaly/sessions", env.saver,
            body={"workflow_id": env.workflow_id, "node_id": "ghost"})
        assert status == 400, body
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        assert body["error"]["details"]["nodeType"] is None

    @covers("node_not_tunable")
    def test_apply_is_409_naming_the_type_and_anomaly_mode(self,
                                                           bedrock_env):
        run_id = bedrock_env.seed_completed_run(bedrock_env.session,
                                                bedrock_env.candidate_id)
        assert run_id
        status, _body = bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            bedrock_env.saver, {"id": bedrock_env.session},
            body={"candidateId": bedrock_env.candidate_id})
        assert status == 200
        # The node leaves anomaly mode in a newer version.
        bedrock_env.put_workflow(
            [{**BEDROCK_NODE,
              "parameters": {**BEDROCK_NODE["parameters"],
                             "anomaly_mode": False}}],
            version=2, workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            bedrock_env.saver, {"id": bedrock_env.session}, body={})
        assert status == 409, body
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        details = body["error"]["details"]
        assert details["nodeType"] == "bedrock_inference"
        assert details["anomaly_mode"] is False
        assert details["version"] == 2
        # No new version was allocated.
        assert bedrock_env.stack.tables.workflows.get_item(
            Key={"workflow_id": bedrock_env.workflow_id}
        )["Item"]["latest_version"] == 2

    @covers("node_not_tunable")
    def test_starting_a_run_on_an_untunable_node_is_400(self, bedrock_env):
        bedrock_env.seed_sample(bedrock_env.session, label="OK")
        bedrock_env.put_workflow(
            [{**BEDROCK_NODE,
              "parameters": {**BEDROCK_NODE["parameters"],
                             "anomaly_mode": False}}],
            version=2, workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        assert not bedrock_env.dispatched


# ==========================================================================
# Row: Export disabled for the Use_Case -> 200 with sampleExportEnabled false
# ==========================================================================

class TestExportDisabled:

    @covers("export_disabled")
    def test_the_overview_still_answers_200_and_says_export_is_off(self,
                                                                   env):
        env.put_usecase(export=False)
        env.put_workflow([BEDROCK_NODE, LLM_NODE, PLAIN_NODE])
        status, body = env.call("GET", "/workflow-tuning/anomaly/workflows",
                                env.saver,
                                query={"usecase_id": env.usecase_id})
        assert status == 200, body
        assert body["sampleExportEnabled"] is False
        # The workflows are still listed, so the UI can explain why there
        # are no samples rather than hiding the feature.
        assert [w["workflowId"] for w in body["workflows"]] == [
            env.workflow_id]

    @covers("export_disabled")
    def test_a_session_can_still_be_opened_with_export_off(self, env):
        env.put_usecase(export=False)
        env.put_workflow([BEDROCK_NODE])
        env.node_id = BEDROCK_NODE["id"]
        status, body = env.call(
            "POST", "/workflow-tuning/anomaly/sessions", env.saver,
            body={"workflow_id": env.workflow_id, "node_id": "bedrock_1"})
        assert status in (200, 201), body
        assert body["session"]["sessionId"]


# ==========================================================================
# Row: Baseline edit/delete -> 409
# ==========================================================================

class TestBaselineReadOnly:

    @covers("baseline_read_only")
    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    def test_editing_or_deleting_the_baseline_is_409(self, bedrock_env,
                                                     method):
        status, body = bedrock_env.call(
            method,
            "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.saver,
            {"id": bedrock_env.session, "cid": "baseline"},
            body={"name": "Renamed", "prompt": "Anything"})
        assert status == 409, body
        assert body["error"]["code"] == "BASELINE_READ_ONLY"
        assert "read-only" in body["error"]["message"].lower()
        # The baseline survived untouched.
        status, session = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.saver, {"id": bedrock_env.session})
        baseline = [c for c in session["candidates"]
                    if c["candidateId"] == "baseline"]
        assert baseline and baseline[0]["prompt"] == \
            BEDROCK_NODE["parameters"]["prompt"]


# ==========================================================================
# Row: Second run in a session -> 409 naming the in-progress run
# ==========================================================================

class TestSecondRun:

    @covers("second_run_in_a_session")
    def test_a_second_run_is_409_naming_the_in_progress_run(self,
                                                            bedrock_env):
        bedrock_env.seed_sample(bedrock_env.session, label="OK")
        status, first = bedrock_env.start_run(bedrock_env.session,
                                             bedrock_env.candidate_id)
        assert status == 202, first
        second_candidate = bedrock_env.candidate(bedrock_env.session,
                                                 name="Candidate B")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            second_candidate)
        assert status == 409, body
        assert body["error"]["code"] == "RUN_IN_PROGRESS"
        assert first["runId"] in body["error"]["message"]
        assert body["error"]["details"]["runId"] == first["runId"]
        # Once the first run finishes the slot is free again.
        bedrock_env.drive()
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            second_candidate)
        assert status == 202, body


# ==========================================================================
# Row: Planned invocations > 600 / repeats outside 1..3 -> 400
# ==========================================================================

class TestRunBounds:

    @covers("run_bounds")
    @pytest.mark.parametrize("repeats", [0, 4, 600, -1, "two", 2.5])
    def test_repeats_outside_1_to_3_is_400_stating_the_bound(self,
                                                             bedrock_env,
                                                             repeats):
        bedrock_env.seed_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id,
                                            repeats=repeats)
        assert status == 400, body
        assert body["error"]["code"] == "INVALID_REPEATS"
        assert f"{MIN_REPEATS}" in body["error"]["message"]
        assert f"{MAX_REPEATS}" in body["error"]["message"]
        assert not bedrock_env.dispatched

    @covers("run_bounds")
    def test_more_than_600_planned_invocations_is_400_stating_the_bound(
            self, bedrock_env, monkeypatch):
        """The bound is checked before any invocation is issued. The real
        601-sample case is the property test's; here the constant is
        lowered and pinned separately below."""
        monkeypatch.setattr(bedrock_env.module, "MAX_PLANNED_INVOCATIONS", 5)
        for _ in range(3):
            bedrock_env.seed_sample(bedrock_env.session, label="OK")
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id,
                                            repeats=2)
        assert status == 400, body
        assert body["error"]["code"] == "RUN_TOO_LARGE"
        assert body["error"]["details"] == {"plannedInvocations": 6,
                                            "samples": 3, "repeats": 2,
                                            "bound": 5}
        assert "6" in body["error"]["message"]
        assert not bedrock_env.dispatched
        assert not bedrock_env.bedrock.calls

    @covers("run_bounds")
    def test_the_real_bounds_are_the_documented_ones(self, bedrock_env):
        module = bedrock_env.module
        assert module.MAX_PLANNED_INVOCATIONS == MAX_PLANNED_INVOCATIONS
        assert (module.MIN_REPEATS, module.MAX_REPEATS) == (MIN_REPEATS,
                                                            MAX_REPEATS)


# ==========================================================================
# Row: VLM run without an eligible device -> 400
# ==========================================================================

class TestNoEligibleDevice:

    @covers("no_eligible_device")
    def test_400_lists_the_devices_that_exported_but_are_not_registered(
            self, llm_env):
        llm_env.seed_sample(llm_env.session, label="OK", thing="dev-a")
        llm_env.seed_sample(llm_env.session, label="NOK", thing="dev-b")
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_ELIGIBLE_DEVICE"
        details = body["error"]["details"]
        assert details["exported"] == ["dev-a", "dev-b"]
        assert details["registered"] == []
        assert details["eligible"] == []
        assert details["ineligible"] == ["dev-a", "dev-b"]
        assert not llm_env.iot.updates

    @covers("no_eligible_device")
    def test_a_registered_device_that_never_exported_is_not_eligible(
            self, llm_env):
        llm_env.seed_sample(llm_env.session, label="OK", thing="dev-a")
        llm_env.deploy_workflow_to(["dev-z"])
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 400, body
        assert body["error"]["code"] == "NO_ELIGIBLE_DEVICE"
        assert body["error"]["details"]["registered"] == ["dev-z"]
        assert body["error"]["details"]["ineligible"] == ["dev-a"]

    @covers("no_eligible_device")
    def test_a_device_that_both_exported_and_reports_the_workflow_runs(
            self, llm_env):
        llm_env.seed_sample(llm_env.session, label="OK", thing="dev-a")
        llm_env.deploy_workflow_to(["dev-a"])
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 202, body
        assert body["deviceThingName"] == "dev-a"
        assert llm_env.iot.desired_jobs("dev-a")


# ==========================================================================
# Row: Apply without a completed run on the selection -> 409
# ==========================================================================

class TestApplyWithoutCompletedRun:

    @covers("apply_without_completed_run")
    def test_no_selection_is_409(self, bedrock_env):
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            bedrock_env.saver, {"id": bedrock_env.session}, body={})
        assert status == 409, body
        assert body["error"]["code"] == "NO_SELECTION"

    @covers("apply_without_completed_run")
    @pytest.mark.parametrize("status_value", ["running", "cancelled",
                                              "failed"])
    def test_a_selection_without_a_completed_run_is_409(self, bedrock_env,
                                                        status_value):
        bedrock_env.seed_completed_run(bedrock_env.session,
                                       bedrock_env.candidate_id,
                                       status=status_value)
        bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            bedrock_env.saver, {"id": bedrock_env.session},
            body={"candidateId": bedrock_env.candidate_id})
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            bedrock_env.saver, {"id": bedrock_env.session}, body={})
        assert status == 409, body
        assert body["error"]["code"] == "NO_COMPLETED_RUN"
        assert body["error"]["details"]["candidateId"] == \
            bedrock_env.candidate_id
        assert bedrock_env.stack.tables.workflows.get_item(
            Key={"workflow_id": bedrock_env.workflow_id}
        )["Item"]["latest_version"] == 1


# ==========================================================================
# Row: Shadow update / S3 failure during dispatch
# ==========================================================================

class TestDispatchFailure:

    def _prepare(self, llm_env):
        llm_env.seed_sample(llm_env.session, label="OK", thing="dev-a")
        llm_env.seed_sample(llm_env.session, label="NOK", thing="dev-a")
        llm_env.deploy_workflow_to(["dev-a"])

    @covers("dispatch_failure")
    def test_a_shadow_failure_fails_the_run_with_the_botocore_reason(
            self, llm_env):
        self._prepare(llm_env)
        llm_env.iot.fail_update = True
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 200, body
        assert body["dispatchFailed"] is True
        assert body["run"]["status"] == "failed"
        assert "InternalFailure" in body["run"]["error"]
        # Nothing else changed: no outcomes, no poll step, and the run slot
        # is free again.
        assert llm_env.outcome_items(body["run"]["runId"]) == []
        assert not llm_env.dispatched
        status, retry = llm_env.start_run(llm_env.session,
                                         llm_env.candidate_id)
        assert status in (200, 202), retry

    @covers("dispatch_failure")
    def test_an_s3_failure_fails_the_run_with_the_botocore_reason(
            self, llm_env):
        self._prepare(llm_env)
        # The manifest cannot be written: the Use_Case's Sample_Store
        # bucket does not exist.
        llm_env.put_usecase(bucket="dda-inference-results-nowhere")
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 200, body
        assert body["dispatchFailed"] is True
        assert body["run"]["status"] == "failed"
        assert "NoSuchBucket" in body["run"]["error"] \
            or "does not exist" in body["run"]["error"]
        assert not llm_env.iot.updates
        assert llm_env.outcome_items(body["run"]["runId"]) == []

    @covers("dispatch_failure")
    def test_the_failed_run_keeps_the_samples_and_their_labels(self,
                                                               llm_env):
        self._prepare(llm_env)
        llm_env.iot.fail_update = True
        status, body = llm_env.start_run(llm_env.session,
                                        llm_env.candidate_id)
        assert status == 200, body
        status, samples = llm_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}/samples",
            llm_env.saver, {"id": llm_env.session})
        assert status == 200
        assert sorted(s["label"] for s in samples["samples"]) == ["NOK", "OK"]


# ==========================================================================
# Row: Bedrock throttling/errors per sample
# ==========================================================================

class TestBedrockErrorPerSample:

    @covers("bedrock_error_per_sample")
    def test_a_throttled_sample_is_one_invocation_error_and_the_run_goes_on(
            self, bedrock_env):
        first = bedrock_env.seed_sample(bedrock_env.session, label="OK",
                                        exported_at=1000)
        second = bedrock_env.seed_sample(bedrock_env.session, label="NOK",
                                         exported_at=2000)
        assert first != second
        throttling = ClientError(
            {"Error": {"Code": "ThrottlingException",
                       "Message": "Too many requests"}}, "Converse")
        # The first unit is throttled, the second answers normally. The
        # scorer issues units in plan order with 4 threads, so pin the
        # answer per request instead of relying on ordering.
        answers = {}

        def converse(**kwargs):
            content = kwargs["messages"][0]["content"]
            marker = b""
            for block in content:
                if "image" in block:
                    marker = block["image"]["source"]["bytes"]
                    break
            answers[marker] = answers.get(marker, 0) + 1
            if marker.decode("utf-8", "replace").endswith(
                    first.split("/")[-1]):
                raise throttling
            return {"output": {"message": {"content": [
                {"text": ANOMALOUS_ANSWER}]}},
                "usage": {"outputTokens": 7}}

        bedrock_env.bedrock.converse = converse
        status, body = bedrock_env.start_run(bedrock_env.session,
                                            bedrock_env.candidate_id)
        assert status == 202, body
        run_id = body["runId"]
        bedrock_env.drive()

        run = bedrock_env.run_item(bedrock_env.session, run_id)
        assert run["status"] == "completed"
        outcomes = {o["sampleId"]: o
                    for o in bedrock_env.outcome_items(run_id)}
        assert set(outcomes) == {first, second}
        failed = outcomes[first]
        assert failed["category"] == "invocation_error"
        # The error class is named, and no verdict was invented.
        assert "ClientError" in failed["error"]
        assert "ThrottlingException" in failed["error"]
        assert failed["isAnomalous"] is None
        assert outcomes[second]["category"] == "correct"
        # The Score_Summary counts the error without losing the run.
        assert run["summary"]["invocations"] == 2
        assert run["summary"]["invocationError"] == 1
        assert run["summary"]["correct"] == 1


# ==========================================================================
# The table itself
# ==========================================================================

def test_every_row_of_the_error_table_is_covered():
    """The design's Portal API error table and this file's cases agree."""
    assert _COVERED == set(ERROR_TABLE), {
        "uncovered": sorted(set(ERROR_TABLE) - _COVERED),
        "unknown": sorted(_COVERED - set(ERROR_TABLE)),
    }


def test_the_error_envelope_is_the_workflow_handlers_one(bedrock_env):
    """Every error body is ``{error: {code, message, details}}`` — the
    envelope workflows.py uses, so the frontend reads one shape."""
    responses = [
        bedrock_env.call("GET", "/workflow-tuning/anomaly/sessions/{id}",
                         bedrock_env.saver, {"id": str(uuid.uuid4())}),
        bedrock_env.call("POST", "/workflow-tuning/anomaly/sessions",
                         bedrock_env.saver, body={}),
        bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            bedrock_env.reader, {"id": bedrock_env.session}, body={}),
    ]
    for status, body in responses:
        assert status >= 400
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details"}
        assert isinstance(body["error"]["code"], str)
        assert isinstance(body["error"]["message"], str)
        assert isinstance(body["error"]["details"], dict)


def test_an_unserved_path_under_the_section_is_501(bedrock_env):
    """The handler answers a path it does not serve without pretending it
    is a client error."""
    status, body = bedrock_env.call(
        "GET", "/workflow-tuning/anomaly/not-a-route", bedrock_env.saver)
    assert status == 501
    assert body["error"]["code"] == "NOT_IMPLEMENTED"


def test_time_is_not_faked_away(bedrock_env):
    """A guard on this file's own harness: the poll interval is patched to
    zero so tests never sleep, but nothing else about time is."""
    assert bedrock_env.module.POLL_INTERVAL_SECONDS == 0
    assert bedrock_env.module.RUN_STALE_SECONDS == 3600
    assert bedrock_env.module.JOB_SILENCE_SECONDS == 900
    assert abs(bedrock_env.module.now_s() - int(time.time())) <= 2
