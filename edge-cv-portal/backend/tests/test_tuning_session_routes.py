"""
Anomaly_Tuning session routes: overview, session lifecycle, sample index,
Labels, Synthetic_Negatives, Candidates and the Candidate preview
(spec: .kiro/specs/quality-prompt-tuning, task 6.1).

Deterministic, enumerated cases over the real ``functions/workflow_tuning.py``
handler against moto: the portal tables and the portal artifacts bucket from
the shared ``aws_stack`` fixture, the tuning single table and the Use_Case's
inference results bucket (the Sample_Store) created here, and Tuning_Samples
seeded as the device writes them
(``workflow-tuning/samples/{wf}/{node}/{thing}/{exec}.{json,input.jpg,
reference.jpg}`` — see src/backend/workflow_engine/tuning/sample_export.py).

What is asserted here (Requirements 1.2, 1.6, 3.1-3.7, 4.1-4.8, 5.1-5.5,
5.7, 9.1, 9.2, 9.4, 9.5, 10.2, 10.5): the route table task 6.1 lands, the
error table's statuses, authorization running before any other validation,
faithful/additive/bounded/label-preserving indexing, the filters and paging,
the Label and Synthetic_Negative toggles, Candidate CRUD with the read-only
baseline, and the preview's exact request text and warnings.

The invariants over the same space are tasks 6.4's property tests
(Properties 5, 12, 13, 15, 16, 18); this file states expectations
literally, restating the sidecar shape and the Verdict_Instruction locally
rather than importing them, so a change in the shared module cannot move
both the code and its expectation together.
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
SAMPLES_PREFIX = "workflow-tuning/samples/"

#: Restated locally (never imported): the Verdict_Instruction the executor
#: appends to every Anomaly_Mode user prompt, and its separator.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')
INSTRUCTION_SEPARATOR = "\n\n"

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
FREEFORM_NODE = {
    "id": "bedrock_free",
    "type": "bedrock_inference",
    "position": {"x": 0, "y": 100},
    "parameters": {"prompt": "Describe the plate.", "anomaly_mode": False},
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
# Harness
# ==========================================================================

@pytest.fixture(scope="module")
def tuning(aws_stack):
    """The real handler module against moto, with the tuning table and the
    Use_Case's Sample_Store bucket in place."""
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    existing = all_table_names(dynamodb)
    if TUNING_TABLE_NAME not in existing:
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
    sys.modules.pop("workflow_tuning", None)
    import workflow_tuning

    return workflow_tuning


class Env:
    """One Use_Case + workflow + users, and the Sample_Store seeding."""

    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning use case",
            "account_id": ACCOUNT_ID,
        })
        self.editor = self._user("DataScientist")
        self.reader = self._user("Viewer")
        self.outsider = self._user("DataLabeler")
        self.workflow_id = None
        self.node_id = None

    # ------------------------------------------------------------- setup
    def _user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def enable_export(self, enabled=True, retention_days=None):
        item = {"usecase_id": self.usecase_id, "name": "Tuning use case",
                "account_id": ACCOUNT_ID, "tuning_sample_export": enabled}
        if retention_days is not None:
            item["tuning_sample_retention_days"] = retention_days
        self.stack.tables.usecases.put_item(Item=item)

    def put_workflow(self, nodes, name="Tuning workflow", version=1,
                     workflow_id=None):
        workflow_id = workflow_id or str(uuid.uuid4())
        document = {"schemaVersion": "1.0", "nodes": nodes,
                    "connections": []}
        key = f"workflows/{self.usecase_id}/{workflow_id}/versions/{version}/workflow.json"
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

    # ----------------------------------------------------- sample store
    def sample_prefix(self, node_id=None, workflow_id=None):
        return (f"{SAMPLES_PREFIX}{workflow_id or self.workflow_id}/"
                f"{node_id or self.node_id}/")

    def seed_sample(self, thing="dev-1", execution=None, node_id=None,
                    workflow_id=None, exported_at=1000, source="live",
                    is_anomalous=True, confidence=0.9,
                    answer='{"is_anomalous": true, "confidence": 0.9}',
                    parse_error=None, fingerprint="sha256:baseline",
                    reference=True, input_bytes=None, version=1,
                    detection_id=None, detection_slot=None,
                    metadata_snippet=None, write_input=True,
                    sidecar_body=None):
        """Write one Tuning_Sample exactly as the device writes it."""
        execution = execution or f"exec-{uuid.uuid4()}"
        prefix = self.sample_prefix(node_id, workflow_id)
        base = f"{prefix}{thing}/{execution}"
        input_bytes = input_bytes or f"INPUT-BYTES-{execution}".encode()
        reference_bytes = f"REF-BYTES-{execution}".encode()
        document = {
            "schemaVersion": 1,
            "source": source,
            "workflowId": workflow_id or self.workflow_id,
            "version": version,
            "executionId": execution,
            "nodeId": node_id or self.node_id,
            "nodeType": "bedrock_inference",
            "thingName": thing,
            "exportedAt": exported_at,
            "input": {"key": base + ".input.jpg",
                      "sha256": hashlib.sha256(input_bytes).hexdigest(),
                      "bytes": len(input_bytes)},
            "recorded": {"isAnomalous": is_anomalous,
                         "confidence": confidence,
                         "answer": answer, "parseError": parse_error},
            "promptFingerprint": fingerprint,
            "detectionId": detection_id,
            "detectionSlot": detection_slot,
        }
        if reference:
            document["reference"] = {
                "key": base + ".reference.jpg",
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                "bytes": len(reference_bytes)}
        if metadata_snippet is not None:
            document["metadataSnippet"] = metadata_snippet
        if write_input:
            self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                               Body=input_bytes)
        if reference:
            self.s3.put_object(Bucket=SAMPLE_BUCKET,
                               Key=base + ".reference.jpg",
                               Body=reference_bytes)
        body = (sidecar_body if sidecar_body is not None
                else json.dumps(document).encode("utf-8"))
        self.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                           Body=body)
        return {"sampleId": f"{thing}/{execution}", "executionId": execution,
                "thingName": thing, "base": base, "document": document,
                "inputBytes": input_bytes}

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
    def create_session(self, user=None, node_id=None, workflow_id=None):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions", user or self.editor,
            body={"workflow_id": workflow_id or self.workflow_id,
                  "node_id": node_id or self.node_id})
        return status, body

    def session_id(self, node_id=None):
        status, body = self.create_session(node_id=node_id)
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def refresh(self, session_id, user=None):
        return self.call("POST",
                         "/workflow-tuning/anomaly/sessions/{id}/refresh",
                         user or self.editor, {"id": session_id})

    def samples(self, session_id, query=None, user=None):
        return self.call("GET",
                         "/workflow-tuning/anomaly/sessions/{id}/samples",
                         user or self.editor, {"id": session_id}, query=query)

    def set_labels(self, session_id, sample_ids, label, user=None):
        return self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            user or self.editor, {"id": session_id},
            body={"sampleIds": sample_ids, "label": label})

    def toggle_synthetic(self, session_id, enabled, user=None):
        return self.call(
            "PUT",
            "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
            user or self.editor, {"id": session_id},
            body={"enabled": enabled})

    def create_candidate(self, session_id, user=None, **fields):
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            user or self.editor, {"id": session_id}, body=fields)

    def preview(self, candidate_id, session_id=None, user=None, query=None):
        params = dict(query or {})
        if session_id:
            params["session_id"] = session_id
        return self.call("GET",
                         "/workflow-tuning/anomaly/candidates/{cid}/preview",
                         user or self.editor, {"cid": candidate_id},
                         query=params or None)


@pytest.fixture
def env(aws_stack, tuning):
    return Env(aws_stack, tuning)


@pytest.fixture
def bedrock_env(env):
    """A Use_Case with export enabled and a workflow whose bedrock node is
    the Tunable_Node under test."""
    env.enable_export(True)
    env.put_workflow([BEDROCK_NODE, FREEFORM_NODE, LLM_NODE, PLAIN_NODE])
    env.node_id = BEDROCK_NODE["id"]
    return env


# ==========================================================================
# 1. Overview (Requirements 1.2, 1.6)
# ==========================================================================

class TestOverview:

    def test_lists_only_workflows_with_tunable_nodes(self, bedrock_env):
        other = bedrock_env.put_workflow([PLAIN_NODE], name="No tunables")
        bedrock_env.put_workflow([BEDROCK_NODE, FREEFORM_NODE, LLM_NODE,
                                  PLAIN_NODE])
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.editor,
            query={"usecase_id": bedrock_env.usecase_id})
        assert status == 200, body
        listed = {w["workflowId"] for w in body["workflows"]}
        assert other not in listed
        entry = [w for w in body["workflows"]
                 if w["workflowId"] == bedrock_env.workflow_id][0]
        # The freeform bedrock node and the camera node are not tunable.
        assert [n["nodeId"] for n in entry["nodes"]] == ["bedrock_1", "llm_1"]
        assert entry["latestVersion"] == 1

    def test_node_type_model_and_sample_counts(self, bedrock_env):
        bedrock_env.seed_sample(execution="e1")
        bedrock_env.seed_sample(execution="e2")
        bedrock_env.seed_sample(execution="e3", node_id="llm_1")
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.editor,
            query={"usecase_id": bedrock_env.usecase_id,
                   "workflow_id": bedrock_env.workflow_id})
        assert status == 200, body
        nodes = {n["nodeId"]: n for n in body["workflows"][0]["nodes"]}
        assert nodes["bedrock_1"]["nodeType"] == "bedrock_inference"
        assert nodes["bedrock_1"]["model"] == "us.amazon.nova-lite-v1:0"
        assert nodes["bedrock_1"]["sampleCount"] == 2
        assert nodes["llm_1"]["model"] == "qwen2-vl"
        assert nodes["llm_1"]["sampleCount"] == 1

    def test_reports_export_disabled(self, env):
        env.enable_export(False)
        env.put_workflow([BEDROCK_NODE])
        status, body = env.call(
            "GET", "/workflow-tuning/anomaly/workflows", env.editor,
            query={"usecase_id": env.usecase_id})
        assert status == 200
        assert body["sampleExportEnabled"] is False
        assert body["sampleRetentionDays"] == 30

    def test_reports_export_enabled_with_retention(self, env):
        env.enable_export(True, retention_days=90)
        env.put_workflow([BEDROCK_NODE])
        status, body = env.call(
            "GET", "/workflow-tuning/anomaly/workflows", env.editor,
            query={"usecase_id": env.usecase_id})
        assert body["sampleExportEnabled"] is True
        assert body["sampleRetentionDays"] == 90

    def test_links_existing_sessions(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.editor,
            query={"usecase_id": bedrock_env.usecase_id,
                   "workflow_id": bedrock_env.workflow_id})
        nodes = {n["nodeId"]: n for n in body["workflows"][0]["nodes"]}
        assert nodes["bedrock_1"]["sessionId"] == session_id
        assert nodes["llm_1"]["sessionId"] is None

    def test_missing_usecase_id_is_400(self, bedrock_env):
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.editor)
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_non_reader_gets_the_uniform_404(self, bedrock_env):
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.outsider,
            query={"usecase_id": bedrock_env.usecase_id})
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_reader_may_view(self, bedrock_env):
        status, _body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/workflows", bedrock_env.reader,
            query={"usecase_id": bedrock_env.usecase_id})
        assert status == 200


# ==========================================================================
# 2. Session create-or-get (Requirements 3.1, 5.1, 10.2)
# ==========================================================================

class TestCreateOrGetSession:

    def test_creates_session_with_baseline_from_latest_version(self,
                                                              bedrock_env):
        status, body = bedrock_env.create_session()
        assert status == 201, body
        session = body["session"]
        assert session["workflowId"] == bedrock_env.workflow_id
        assert session["nodeId"] == "bedrock_1"
        assert session["nodeType"] == "bedrock_inference"
        assert session["baselineVersion"] == 1
        assert session["baselineCandidateId"] == "baseline"
        assert session["syntheticNegativesEnabled"] is False
        assert body["created"] is True
        assert body["node"]["model"] == "us.amazon.nova-lite-v1:0"
        assert body["node"]["maxTokensBounds"] == {"min": 1, "max": 4096}

        status, view = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session["sessionId"]})
        assert status == 200
        baseline = [c for c in view["candidates"] if c["isBaseline"]][0]
        assert baseline["candidateId"] == "baseline"
        assert baseline["prompt"] == BEDROCK_NODE["parameters"]["prompt"]
        assert baseline["systemPrompt"] == \
            BEDROCK_NODE["parameters"]["system_prompt"]
        assert baseline["maxTokens"] == 256
        assert baseline["latestRun"] is None

    def test_second_create_returns_the_same_session(self, bedrock_env):
        first = bedrock_env.session_id()
        status, body = bedrock_env.create_session()
        assert status == 200
        assert body["created"] is False
        assert body["session"]["sessionId"] == first

    def test_one_session_per_workflow_and_node(self, bedrock_env):
        bedrock_env.node_id = "bedrock_1"
        first = bedrock_env.session_id()
        bedrock_env.node_id = "llm_1"
        second = bedrock_env.session_id()
        assert first != second

    def test_baseline_refreshes_on_a_new_version(self, bedrock_env):
        session_id = bedrock_env.session_id()
        changed = json.loads(json.dumps(BEDROCK_NODE))
        changed["parameters"]["prompt"] = "A rewritten prompt."
        changed["parameters"]["max_tokens"] = 512
        bedrock_env.put_workflow([changed, LLM_NODE], version=2,
                                 workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.create_session()
        assert status == 200
        assert body["session"]["baselineVersion"] == 2
        _status, view = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session_id})
        baseline = [c for c in view["candidates"] if c["isBaseline"]][0]
        assert baseline["prompt"] == "A rewritten prompt."
        assert baseline["maxTokens"] == 512
        assert baseline["baselineVersion"] == 2

    def test_indexes_samples_on_create(self, bedrock_env):
        bedrock_env.seed_sample(execution="e1")
        bedrock_env.seed_sample(execution="e2")
        status, body = bedrock_env.create_session()
        assert status == 201
        assert body["refresh"]["indexed"] == 2
        assert body["session"]["lastRefresh"]["indexed"] == 2

    def test_non_tunable_node_is_400(self, bedrock_env):
        status, body = bedrock_env.create_session(node_id="bedrock_free")
        assert status == 400
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        assert body["error"]["details"]["nodeType"] == "bedrock_inference"
        assert body["error"]["details"]["anomaly_mode"] is False

    def test_unknown_node_is_400(self, bedrock_env):
        status, body = bedrock_env.create_session(node_id="nope")
        assert status == 400
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"

    def test_plain_node_is_400(self, bedrock_env):
        status, body = bedrock_env.create_session(node_id="cam_1")
        assert status == 400
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"

    def test_missing_fields_is_400(self, bedrock_env):
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions", bedrock_env.editor,
            body={"workflow_id": bedrock_env.workflow_id})
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_unknown_workflow_is_404(self, bedrock_env):
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions", bedrock_env.editor,
            body={"workflow_id": str(uuid.uuid4()), "node_id": "bedrock_1"})
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_reader_cannot_create(self, bedrock_env):
        status, body = bedrock_env.create_session(user=bedrock_env.reader)
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert body["error"]["details"]["required_permissions"] == \
            ["workflow:edit"]

    def test_non_reader_gets_404(self, bedrock_env):
        status, body = bedrock_env.create_session(user=bedrock_env.outsider)
        assert status == 404

    def test_authorization_precedes_validation(self, bedrock_env):
        """Requirement 9.2: a caller without read access is refused before
        the body is even looked at."""
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions",
            bedrock_env.outsider, body={"node_id": "bedrock_1",
                                        "workflow_id":
                                            bedrock_env.workflow_id})
        assert status == 404
        # A reader with a non-tunable node: the 403 comes before the 400.
        status, body = bedrock_env.create_session(user=bedrock_env.reader,
                                                 node_id="cam_1")
        assert status == 403


# ==========================================================================
# 3. Indexing (Requirements 3.1-3.6, 9.5)
# ==========================================================================

class TestSampleIndex:

    def test_records_sidecar_fields_and_no_image_bytes(self, bedrock_env):
        seeded = bedrock_env.seed_sample(
            execution="e1", exported_at=1234, source="backfill",
            is_anomalous=False, confidence=0.25, answer="{...}",
            detection_id="a3f50a41", detection_slot=1, version=38,
            metadata_snippet={"trigger": {"payload_json": {"part": "A"}}})
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.samples(session_id,
                                           {"include_metadata": "true"})
        assert status == 200, body
        sample = body["samples"][0]
        assert sample["sampleId"] == seeded["sampleId"]
        assert sample["thingName"] == "dev-1"
        assert sample["executionId"] == "e1"
        assert sample["version"] == 38
        assert sample["exportedAt"] == 1234
        assert sample["source"] == "backfill"
        assert sample["detectionId"] == "a3f50a41"
        assert sample["detectionSlot"] == 1
        assert sample["recorded"] == {"isAnomalous": False,
                                      "confidence": 0.25,
                                      "answer": "{...}", "parseError": None}
        assert sample["metadataSnippet"] == {
            "trigger": {"payload_json": {"part": "A"}}}
        assert sample["input"]["key"] == seeded["base"] + ".input.jpg"
        assert sample["input"]["sha256"] == \
            seeded["document"]["input"]["sha256"]
        assert sample["reference"]["key"] == seeded["base"] + ".reference.jpg"
        assert sample["singleImage"] is False

        # Requirement 9.5: no image bytes anywhere in the item.
        raw = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME).get_item(
                Key={"pk": f"SESSION#{session_id}",
                     "sk": f"SAMPLE#{seeded['sampleId']}"})["Item"]
        serialized = json.dumps(raw, default=str)
        assert seeded["inputBytes"].decode() not in serialized
        assert "INPUT-BYTES" not in serialized

    def test_single_image_sample(self, bedrock_env):
        bedrock_env.seed_sample(execution="e1", reference=False)
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.samples(session_id)
        assert body["samples"][0]["reference"] is None
        assert body["samples"][0]["singleImage"] is True

    def test_unreadable_and_missing_input_are_skipped_by_reason(self,
                                                               bedrock_env):
        bedrock_env.seed_sample(execution="ok1")
        bedrock_env.seed_sample(execution="bad", sidecar_body=b"{not json")
        bedrock_env.seed_sample(execution="gone", write_input=False)
        bedrock_env.seed_sample(execution="empty",
                                sidecar_body=json.dumps({"a": 1}).encode())
        status, body = bedrock_env.create_session()
        assert status == 201
        summary = body["refresh"]
        assert summary["indexed"] == 1
        assert summary["skipped"] == {"unreadable": 1, "missing_input": 1,
                                      "malformed": 1}
        assert summary["discovered"] == 4

    def test_duplicate_marked_against_the_earliest(self, bedrock_env):
        shared = b"IDENTICAL-INPUT"
        bedrock_env.seed_sample(execution="early", exported_at=100,
                                input_bytes=shared)
        bedrock_env.seed_sample(execution="late", exported_at=200,
                                input_bytes=shared)
        bedrock_env.seed_sample(execution="other", exported_at=300)
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.samples(session_id)
        by_id = {s["executionId"]: s for s in body["samples"]}
        assert by_id["early"]["duplicateOf"] is None
        assert by_id["late"]["duplicateOf"] == "dev-1/early"
        assert by_id["other"]["duplicateOf"] is None
        assert body["labelCounts"]["duplicates"] == 1

    def test_different_prompt_flag(self, bedrock_env):
        bedrock_env.seed_sample(execution="same")
        bedrock_env.seed_sample(execution="other",
                                fingerprint="sha256:something-else")
        session_id = bedrock_env.session_id()
        # The baseline fingerprint is derived from the deployed Prompt_Set,
        # so BOTH seeded fingerprints differ from it here; pin the rule by
        # setting one sample's fingerprint to the session's baseline.
        session = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME).get_item(
                Key={"pk": f"SESSION#{session_id}", "sk": "META"})["Item"]
        baseline = session["baselineFingerprint"]
        table = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)
        table.update_item(
            Key={"pk": f"SESSION#{session_id}", "sk": "SAMPLE#dev-1/same"},
            UpdateExpression="SET promptFingerprint = :f",
            ExpressionAttributeValues={":f": baseline})
        _status, body = bedrock_env.samples(session_id)
        by_id = {s["executionId"]: s for s in body["samples"]}
        assert by_id["same"]["differentPrompt"] is False
        assert by_id["other"]["differentPrompt"] is True

    def test_refresh_is_additive_and_label_preserving(self, bedrock_env):
        bedrock_env.seed_sample(execution="first", exported_at=100)
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, ["dev-1/first"], "OK")
        bedrock_env.seed_sample(execution="second", exported_at=200)
        status, body = bedrock_env.refresh(session_id)
        assert status == 200, body
        assert body["refresh"]["indexed"] == 1
        assert body["labelCounts"]["total"] == 2
        _status, samples = bedrock_env.samples(session_id)
        by_id = {s["executionId"]: s for s in samples["samples"]}
        assert by_id["first"]["label"] == "OK"
        assert by_id["second"]["label"] is None
        # A second refresh with nothing new indexes nothing.
        _status, again = bedrock_env.refresh(session_id)
        assert again["refresh"]["indexed"] == 0
        assert again["refresh"]["discovered"] == 0

    def test_newest_samples_win_the_bound(self, bedrock_env, monkeypatch):
        monkeypatch.setattr(bedrock_env.module, "SAMPLE_INDEX_BOUND", 2)
        for index, exported_at in enumerate((100, 200, 300, 400)):
            bedrock_env.seed_sample(execution=f"e{index}",
                                    exported_at=exported_at)
        status, body = bedrock_env.create_session()
        assert status == 201
        assert body["refresh"]["indexed"] == 2
        assert body["refresh"]["beyondBound"] == 2
        _status, samples = bedrock_env.samples(body["session"]["sessionId"])
        assert [s["exportedAt"] for s in samples["samples"]] == [400, 300]

    def test_refresh_requires_edit(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.refresh(session_id,
                                            user=bedrock_env.reader)
        assert status == 403
        status, _body = bedrock_env.refresh(session_id,
                                            user=bedrock_env.outsider)
        assert status == 404

    def test_unknown_session_is_the_uniform_404(self, bedrock_env):
        status, body = bedrock_env.samples(str(uuid.uuid4()))
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"


# ==========================================================================
# 4. Samples: presigned URLs, filters, paging (Requirements 3.7, 4.4, 4.8)
# ==========================================================================

class TestSampleListing:

    def test_presigned_urls_valid_for_thirty_minutes(self, bedrock_env):
        import re
        import time as _time

        bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.samples(session_id)
        sample = body["samples"][0]
        assert body["expiresInSeconds"] == 1800
        for block in (sample["input"], sample["reference"]):
            url = block["url"]
            assert SAMPLE_BUCKET in url
            assert block["key"] in url
            # SigV4 states the lifetime, SigV2 the absolute expiry; either
            # way it must be the 30-minute bound (Requirement 4.8).
            if "X-Amz-Expires=" in url:
                assert "X-Amz-Expires=1800" in url
            else:
                expires = int(re.search(r"[?&]Expires=(\d+)", url).group(1))
                assert 1500 <= expires - int(_time.time()) <= 1800

    def test_unavailable_when_the_image_expired(self, bedrock_env):
        seeded = bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [seeded["sampleId"]], "OK")
        bedrock_env.s3.delete_object(Bucket=SAMPLE_BUCKET,
                                     Key=seeded["base"] + ".input.jpg")
        _status, body = bedrock_env.samples(session_id)
        sample = body["samples"][0]
        assert sample["unavailable"] is True
        assert sample["label"] == "OK"

    def test_filters(self, bedrock_env):
        a = bedrock_env.seed_sample(execution="a", thing="dev-1",
                                    exported_at=100, is_anomalous=True,
                                    source="live", version=1)
        b = bedrock_env.seed_sample(execution="b", thing="dev-2",
                                    exported_at=200, is_anomalous=False,
                                    source="backfill", version=2)
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [a["sampleId"]], "OK")
        bedrock_env.set_labels(session_id, [b["sampleId"]], "NOK")

        def ids(query):
            _status, body = bedrock_env.samples(session_id, query)
            return {s["executionId"] for s in body["samples"]}

        assert ids({"label": "OK"}) == {"a"}
        assert ids({"label": "NOK"}) == {"b"}
        assert ids({"label": "unlabelled"}) == set()
        assert ids({"device": "dev-2"}) == {"b"}
        assert ids({"version": "1"}) == {"a"}
        assert ids({"source": "backfill"}) == {"b"}
        assert ids({"verdict": "anomalous"}) == {"a"}
        assert ids({"verdict": "normal"}) == {"b"}
        # 'a' is labelled OK but recorded anomalous -> disagreement;
        # 'b' is labelled NOK but recorded normal -> disagreement too.
        assert ids({"disagree": "true"}) == {"a", "b"}
        bedrock_env.set_labels(session_id, [a["sampleId"]], "NOK")
        assert ids({"disagree": "true"}) == {"b"}
        assert ids({"duplicates": "false"}) == {"a", "b"}
        assert ids({"synthetic": "true"}) == set()

    def test_paging_with_cursor(self, bedrock_env):
        for index in range(5):
            bedrock_env.seed_sample(execution=f"e{index}",
                                    exported_at=100 + index)
        session_id = bedrock_env.session_id()
        _status, first = bedrock_env.samples(session_id, {"limit": "2"})
        assert first["count"] == 2
        assert first["matched"] == 5
        assert [s["executionId"] for s in first["samples"]] == ["e4", "e3"]
        _status, second = bedrock_env.samples(
            session_id, {"limit": "2", "cursor": first["nextCursor"]})
        assert [s["executionId"] for s in second["samples"]] == ["e2", "e1"]
        _status, third = bedrock_env.samples(
            session_id, {"limit": "2", "cursor": second["nextCursor"]})
        assert [s["executionId"] for s in third["samples"]] == ["e0"]
        assert third["nextCursor"] is None

    def test_invalid_cursor_is_400(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.samples(session_id, {"cursor": "!!!"})
        assert status == 400
        assert body["error"]["code"] == "INVALID_CURSOR"

    def test_reader_may_list(self, bedrock_env):
        bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.samples(session_id,
                                            user=bedrock_env.reader)
        assert status == 200


# ==========================================================================
# 5. Labels (Requirements 4.2, 4.3, 4.7)
# ==========================================================================

class TestLabels:

    def test_multi_set_and_counts(self, bedrock_env):
        seeded = [bedrock_env.seed_sample(execution=f"e{i}") for i in range(3)]
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.set_labels(
            session_id, [s["sampleId"] for s in seeded[:2]], "OK")
        assert status == 200, body
        assert len(body["updated"]) == 2
        assert body["labelCounts"]["OK"] == 2
        assert body["labelCounts"]["unlabelled"] == 1
        _status, body = bedrock_env.set_labels(
            session_id, [seeded[2]["sampleId"]], "EXCLUDE")
        assert body["labelCounts"] == {"OK": 2, "NOK": 0, "EXCLUDE": 1,
                                       "unlabelled": 0, "synthetic": 0,
                                       "duplicates": 0, "total": 3}

    def test_last_operation_wins_and_null_clears(self, bedrock_env):
        seeded = bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        for label in ("OK", "NOK", "EXCLUDE", "OK"):
            _status, body = bedrock_env.set_labels(
                session_id, [seeded["sampleId"]], label)
        assert body["labelCounts"]["OK"] == 1
        _status, body = bedrock_env.set_labels(
            session_id, [seeded["sampleId"]], None)
        assert body["labelCounts"]["unlabelled"] == 1
        assert body["labelCounts"]["OK"] == 0

    def test_lowercase_label_accepted(self, bedrock_env):
        seeded = bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.set_labels(
            session_id, [seeded["sampleId"]], "ok")
        assert body["label"] == "OK"

    def test_invalid_label_is_400(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.set_labels(session_id, ["dev-1/x"], "MAYBE")
        assert status == 400
        assert body["error"]["code"] == "INVALID_LABEL"

    def test_missing_sample_ids_is_400(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.set_labels(session_id, [], "OK")
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_unknown_sample_is_reported_not_created(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.set_labels(session_id, ["dev-9/nope"],
                                              "OK")
        assert body["updated"] == []
        assert body["missing"] == ["dev-9/nope"]
        assert body["labelCounts"]["total"] == 0

    def test_labelling_requires_edit(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.set_labels(session_id, ["dev-1/x"], "OK",
                                               user=bedrock_env.reader)
        assert status == 403


# ==========================================================================
# 6. Synthetic negatives (Requirements 4.5, 4.6)
# ==========================================================================

class TestSyntheticNegatives:

    def _seed_pair(self, env, execution, thing="dev-1", siblings=("llm_1",)):
        source = env.seed_sample(execution=execution, thing=thing)
        references = {}
        for sibling in siblings:
            seeded = env.seed_sample(execution=execution, thing=thing,
                                     node_id=sibling)
            references[sibling] = seeded["base"] + ".reference.jpg"
        return source, references

    def test_creates_one_per_ok_sample_and_sibling_reference(self,
                                                            bedrock_env):
        source, references = self._seed_pair(
            bedrock_env, "e1", siblings=("llm_1", "bedrock_free"))
        # A NOK-labelled and an unlabelled sample, both with a sibling
        # reference available: only OK samples produce Synthetic_Negatives.
        nok, _nok_refs = self._seed_pair(bedrock_env, "e2")
        unlabelled, _unlabelled_refs = self._seed_pair(bedrock_env, "e3")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [source["sampleId"]], "OK")
        bedrock_env.set_labels(session_id, [nok["sampleId"]], "NOK")

        status, body = bedrock_env.toggle_synthetic(session_id, True)
        assert status == 200, body
        assert body["created"] == 2
        assert body["enabled"] is True
        _status, samples = bedrock_env.samples(session_id,
                                               {"synthetic": "true"})
        assert len(samples["samples"]) == 2
        sources = {s["sourceSampleId"] for s in samples["samples"]}
        assert sources == {source["sampleId"]}
        assert nok["sampleId"] not in sources
        assert unlabelled["sampleId"] not in sources
        created = {s["siblingNodeId"]: s for s in samples["samples"]}
        assert set(created) == {"llm_1", "bedrock_free"}
        for sibling, sample in created.items():
            assert sample["label"] == "NOK"
            assert sample["synthetic"] is True
            assert sample["sourceSampleId"] == source["sampleId"]
            assert sample["input"]["key"] == source["base"] + ".input.jpg"
            assert sample["reference"]["key"] == references[sibling]
            assert sample["reference"]["url"]

    def test_only_same_execution_and_device(self, bedrock_env):
        source = bedrock_env.seed_sample(execution="e1", thing="dev-1")
        # A sibling reference from another execution and another device.
        bedrock_env.seed_sample(execution="other", thing="dev-1",
                                node_id="llm_1")
        bedrock_env.seed_sample(execution="e1", thing="dev-2",
                                node_id="llm_1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [source["sampleId"]], "OK")
        _status, body = bedrock_env.toggle_synthetic(session_id, True)
        assert body["created"] == 0

    def test_single_image_sibling_creates_nothing(self, bedrock_env):
        source = bedrock_env.seed_sample(execution="e1")
        bedrock_env.seed_sample(execution="e1", node_id="llm_1",
                                reference=False)
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [source["sampleId"]], "OK")
        _status, body = bedrock_env.toggle_synthetic(session_id, True)
        assert body["created"] == 0

    def test_disable_removes_them_and_keeps_labels(self, bedrock_env):
        source, _references = self._seed_pair(bedrock_env, "e1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [source["sampleId"]], "OK")
        bedrock_env.toggle_synthetic(session_id, True)
        _status, body = bedrock_env.toggle_synthetic(session_id, False)
        assert body["enabled"] is False
        assert body["removed"] == 1
        assert body["labelCounts"]["synthetic"] == 0
        assert body["labelCounts"]["total"] == 1
        _status, samples = bedrock_env.samples(session_id)
        assert samples["samples"][0]["label"] == "OK"

    def test_enabling_twice_is_idempotent(self, bedrock_env):
        source, _references = self._seed_pair(bedrock_env, "e1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [source["sampleId"]], "OK")
        _status, first = bedrock_env.toggle_synthetic(session_id, True)
        _status, second = bedrock_env.toggle_synthetic(session_id, True)
        assert first["created"] == 1
        assert second["created"] == 0
        assert second["labelCounts"]["synthetic"] == 1

    def test_missing_enabled_is_400(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.call(
            "PUT",
            "/workflow-tuning/anomaly/sessions/{id}/synthetic-negatives",
            bedrock_env.editor, {"id": session_id}, body={"enabled": "yes"})
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"

    def test_toggle_requires_edit(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.toggle_synthetic(session_id, True,
                                                     user=bedrock_env.reader)
        assert status == 403


# ==========================================================================
# 7. Candidates (Requirements 5.1, 5.2, 5.7)
# ==========================================================================

class TestCandidates:

    def test_create_edit_delete(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.create_candidate(
            session_id, name="Rewrite", prompt="Describe then compare.",
            systemPrompt="Be terse.", maxTokens=400)
        assert status == 201, body
        candidate = body["candidate"]
        assert candidate["isBaseline"] is False
        assert candidate["maxTokens"] == 400
        assert candidate["fingerprint"].startswith("sha256:")

        status, body = bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor,
            {"id": session_id, "cid": candidate["candidateId"]},
            body={"name": "Rewrite v2", "prompt": "Compare carefully.",
                  "maxTokens": 320})
        assert status == 200, body
        assert body["candidate"]["name"] == "Rewrite v2"
        assert body["candidate"]["prompt"] == "Compare carefully."
        assert body["candidate"]["maxTokens"] == 320
        assert body["candidate"]["systemPrompt"] == "Be terse."
        assert body["candidate"]["fingerprint"] != candidate["fingerprint"]

        status, body = bedrock_env.call(
            "DELETE",
            "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor,
            {"id": session_id, "cid": candidate["candidateId"]})
        assert status == 200, body
        _status, view = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session_id})
        assert [c["candidateId"] for c in view["candidates"]] == ["baseline"]

    def test_baseline_is_read_only(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor, {"id": session_id, "cid": "baseline"},
            body={"name": "hijack", "prompt": "x"})
        assert status == 409
        assert body["error"]["code"] == "BASELINE_READ_ONLY"
        status, body = bedrock_env.call(
            "DELETE",
            "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor, {"id": session_id, "cid": "baseline"})
        assert status == 409
        assert body["error"]["code"] == "BASELINE_READ_ONLY"

    def test_unknown_candidate_is_404(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.call(
            "DELETE",
            "/workflow-tuning/anomaly/sessions/{id}/candidates/{cid}",
            bedrock_env.editor, {"id": session_id, "cid": "nope"})
        assert status == 404
        assert body["error"]["code"] == "CANDIDATE_NOT_FOUND"

    @pytest.mark.parametrize("fields,code", [
        ({"prompt": "x"}, "INVALID_NAME"),
        ({"name": "  ", "prompt": "x"}, "INVALID_NAME"),
        ({"name": "n" * 129, "prompt": "x"}, "INVALID_NAME"),
        ({"name": "n"}, "INVALID_PROMPT"),
        ({"name": "n", "prompt": "   "}, "INVALID_PROMPT"),
        ({"name": "n", "prompt": "x", "systemPrompt": 5},
         "INVALID_SYSTEM_PROMPT"),
        ({"name": "n", "prompt": "x", "maxTokens": 0}, "INVALID_MAX_TOKENS"),
        ({"name": "n", "prompt": "x", "maxTokens": 5000},
         "INVALID_MAX_TOKENS"),
        ({"name": "n", "prompt": "x", "maxTokens": "many"},
         "INVALID_MAX_TOKENS"),
        ({"name": "n", "prompt": "x", "maxTokens": 12.5},
         "INVALID_MAX_TOKENS"),
    ])
    def test_validation(self, bedrock_env, fields, code):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.create_candidate(session_id, **fields)
        assert status == 400
        assert body["error"]["code"] == code

    def test_max_tokens_defaults_to_the_catalog_default(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, body = bedrock_env.create_candidate(
            session_id, name="No budget", prompt="Compare.")
        assert body["candidate"]["maxTokens"] == 256

    def test_llm_max_tokens_is_unbounded_above(self, bedrock_env):
        bedrock_env.node_id = "llm_1"
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.create_candidate(
            session_id, name="Large", prompt="Inspect.", maxTokens=9000)
        assert status == 201, body
        assert body["candidate"]["maxTokens"] == 9000

    def test_candidate_crud_requires_edit(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.create_candidate(
            session_id, user=bedrock_env.reader, name="n", prompt="p")
        assert status == 403


# ==========================================================================
# 8. Candidate preview (Requirements 5.3, 5.4, 5.5)
# ==========================================================================

class TestPreview:

    def test_shows_the_exact_request_text(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, created = bedrock_env.create_candidate(
            session_id, name="Rewrite", prompt="Compare the plates.",
            systemPrompt="  Be terse.  ", maxTokens=400)
        status, body = bedrock_env.preview(created["candidate"]["candidateId"],
                                          session_id)
        assert status == 200, body
        assert body["userMessage"] == ("Compare the plates."
                                       + INSTRUCTION_SEPARATOR
                                       + VERDICT_INSTRUCTION)
        assert body["systemText"] == "  Be terse.  "
        assert body["maxTokens"] == 400
        assert body["model"] == "us.amazon.nova-lite-v1:0"
        assert body["region"] == "us-west-2"
        assert body["warnings"] == []

    def test_baseline_preview(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.preview("baseline", session_id)
        assert status == 200
        assert body["userMessage"] == (
            BEDROCK_NODE["parameters"]["prompt"] + INSTRUCTION_SEPARATOR
            + VERDICT_INSTRUCTION)
        assert body["systemText"] == \
            BEDROCK_NODE["parameters"]["system_prompt"]

    def test_llm_template_is_shown_unrendered(self, bedrock_env):
        bedrock_env.node_id = "llm_1"
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.preview("baseline", session_id)
        assert status == 200, body
        assert body["userMessage"] == (
            "Inspect {trigger.payload_json.part}." + INSTRUCTION_SEPARATOR
            + VERDICT_INSTRUCTION)
        assert body["templateRendered"] is False
        assert body["model"] == "qwen2-vl"
        assert body["maxTokens"] == 512
        assert body["systemText"] is None

    def test_truncation_warning(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, created = bedrock_env.create_candidate(
            session_id, name="Tiny", prompt="Compare.", maxTokens=32)
        _status, body = bedrock_env.preview(
            created["candidate"]["candidateId"], session_id)
        codes = [w["code"] for w in body["warnings"]]
        assert codes == ["max_tokens_truncation"]
        assert "32" in body["warnings"][0]["message"]

    def test_schema_warning_for_prompt_and_system_prompt(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, created = bedrock_env.create_candidate(
            session_id, name="Own schema",
            prompt='Answer with JSON {"text": "...", "objects": []}.',
            systemPrompt='Always reply as JSON with keys text and objects.')
        _status, body = bedrock_env.preview(
            created["candidate"]["candidateId"], session_id)
        warnings = {(w["code"], w.get("field")) for w in body["warnings"]}
        assert warnings == {
            ("answer_schema_missing_is_anomalous", "prompt"),
            ("answer_schema_missing_is_anomalous", "systemPrompt")}

    def test_no_schema_warning_when_is_anomalous_named(self, bedrock_env):
        session_id = bedrock_env.session_id()
        _status, created = bedrock_env.create_candidate(
            session_id, name="Compatible",
            prompt='Answer with JSON {"is_anomalous": bool}.')
        _status, body = bedrock_env.preview(
            created["candidate"]["candidateId"], session_id)
        assert body["warnings"] == []

    def test_warnings_never_block(self, bedrock_env):
        """Requirements 5.4, 5.5: a warned Candidate is still stored and
        previewable."""
        session_id = bedrock_env.session_id()
        status, created = bedrock_env.create_candidate(
            session_id, name="Warned", prompt="Reply as JSON.", maxTokens=16)
        assert status == 201
        status, body = bedrock_env.preview(
            created["candidate"]["candidateId"], session_id)
        assert status == 200
        assert len(body["warnings"]) == 2

    def test_session_id_is_required(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.preview("baseline", None)
        assert status == 400
        assert body["error"]["code"] == "MISSING_FIELDS"
        # ... or the workflow/node pair of an existing session.
        status, body = bedrock_env.preview(
            "baseline", None,
            query={"workflow_id": bedrock_env.workflow_id,
                   "node_id": "bedrock_1"})
        assert status == 200, body
        assert body["sessionId"] == session_id

    def test_unknown_candidate_is_404(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, body = bedrock_env.preview("nope", session_id)
        assert status == 404
        assert body["error"]["code"] == "CANDIDATE_NOT_FOUND"

    def test_preview_requires_read(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.preview("baseline", session_id,
                                            user=bedrock_env.reader)
        assert status == 200
        status, _body = bedrock_env.preview("baseline", session_id,
                                            user=bedrock_env.outsider)
        assert status == 404


# ==========================================================================
# 9. Session view and delete (Requirements 10.2, 10.5)
# ==========================================================================

class TestSessionViewAndDelete:

    def test_view_reports_counts_candidates_and_node(self, bedrock_env):
        seeded = bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [seeded["sampleId"]], "OK")
        bedrock_env.create_candidate(session_id, name="c1", prompt="p")
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session_id})
        assert status == 200
        assert body["labelCounts"]["OK"] == 1
        assert len(body["candidates"]) == 2
        assert body["candidates"][0]["isBaseline"] is True
        assert body["node"]["nodeId"] == "bedrock_1"
        assert body["nodeStillTunable"] is True
        assert body["latestVersion"] == 1
        assert body["sampleExportEnabled"] is True
        assert body["runCount"] == 0

    def test_view_reports_a_node_that_stopped_being_tunable(self,
                                                           bedrock_env):
        session_id = bedrock_env.session_id()
        bedrock_env.put_workflow([LLM_NODE], version=2,
                                 workflow_id=bedrock_env.workflow_id)
        _status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session_id})
        assert body["nodeStillTunable"] is False
        assert body["node"] is None

    def test_delete_removes_state_and_keeps_exported_samples(self,
                                                             bedrock_env):
        seeded = bedrock_env.seed_sample(execution="e1")
        session_id = bedrock_env.session_id()
        bedrock_env.set_labels(session_id, [seeded["sampleId"]], "OK")
        bedrock_env.create_candidate(session_id, name="c1", prompt="p")
        # A Score_Run outcome object of this session in the Sample_Store.
        run_key = (f"workflow-tuning/sessions/{session_id}/runs/r1/"
                   f"outcomes-1.json")
        bedrock_env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=run_key,
                                  Body=b"[]")

        status, body = bedrock_env.call(
            "DELETE", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.editor, {"id": session_id})
        assert status == 200, body
        assert body["deleted"]["objects"] == 1
        assert body["deleted"]["items"] >= 3  # META + candidate + sample

        table = boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)
        remaining = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"SESSION#{session_id}"})
        assert remaining["Items"] == []
        # The uniqueness item is gone, so a new session can be created.
        status, again = bedrock_env.create_session()
        assert status == 201
        assert again["session"]["sessionId"] != session_id
        # The exported sample objects are untouched (Requirement 10.5).
        for suffix in (".json", ".input.jpg", ".reference.jpg"):
            bedrock_env.s3.head_object(Bucket=SAMPLE_BUCKET,
                                       Key=seeded["base"] + suffix)
        assert bedrock_env.s3.list_objects_v2(
            Bucket=SAMPLE_BUCKET,
            Prefix=f"workflow-tuning/sessions/{session_id}/"
        ).get("KeyCount") == 0

    def test_delete_requires_edit(self, bedrock_env):
        session_id = bedrock_env.session_id()
        status, _body = bedrock_env.call(
            "DELETE", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.reader, {"id": session_id})
        assert status == 403


# ==========================================================================
# 10. Routing edges
#
# Task 6.2 landed the Score_Run and selection routes and task 6.3 apply, so
# every designed route is served (their cases live in
# tests/test_tuning_score_runs.py and tests/test_tuning_apply.py).
# ==========================================================================

class TestNotImplementedRoutes:

    def test_an_unserved_path_under_the_section_is_501(self, bedrock_env):
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/no-such-thing",
            bedrock_env.editor)
        assert status == 501
        assert body["error"]["code"] == "NOT_IMPLEMENTED"

    def test_apply_is_routed(self, bedrock_env):
        """Apply is served (task 6.3): an unknown session is the uniform 404,
        not a 501."""
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            bedrock_env.editor, {"id": "no-such-session"}, body={})
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_unknown_route_is_404(self, bedrock_env):
        status, body = bedrock_env.call("GET", "/something-else",
                                        bedrock_env.editor)
        assert status == 404
        assert body["error"]["code"] == "NOT_FOUND"

    def test_options_preflight(self, bedrock_env):
        response = bedrock_env.module.handler({"httpMethod": "OPTIONS"}, None)
        assert response["statusCode"] == 200
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_an_unknown_action_is_reported_not_run(self, bedrock_env):
        result = bedrock_env.module.handler(
            {"action": "no_such_action", "run_id": "r"}, None)
        assert result["status"] == "unknown_action"
        assert result["action"] == "no_such_action"

    def test_a_score_run_action_for_a_vanished_run_is_a_no_op(self,
                                                             bedrock_env):
        result = bedrock_env.module.handler(
            {"action": "execute_score_run", "run_id": "does-not-exist"}, None)
        assert result["status"] == "gone"


# ==========================================================================
# 11. Malformed input
# ==========================================================================

class TestMalformedInput:

    def test_invalid_json_body(self, bedrock_env):
        event = {
            "httpMethod": "POST",
            "resource": "/workflow-tuning/anomaly/sessions",
            "path": "/workflow-tuning/anomaly/sessions",
            "body": "{not json",
            "requestContext": {"authorizer": {"claims": {
                "sub": bedrock_env.editor["user_id"],
                "email": bedrock_env.editor["email"],
                "cognito:username": bedrock_env.editor["username"],
                "custom:role": bedrock_env.editor["role"]}}},
        }
        response = bedrock_env.module.handler(event, None)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"]["code"] == "INVALID_JSON"

    def test_non_object_body(self, bedrock_env):
        status, body = bedrock_env.call(
            "POST", "/workflow-tuning/anomaly/sessions", bedrock_env.editor,
            body=[1, 2, 3])
        assert status == 400
        assert body["error"]["code"] == "INVALID_JSON"
