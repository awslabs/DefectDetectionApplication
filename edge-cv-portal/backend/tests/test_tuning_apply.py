"""
Anomaly_Tuning apply: saving the selected Candidate as a new
Workflow_Definition version
(spec: .kiro/specs/quality-prompt-tuning, task 6.3).

Deterministic, enumerated cases over the real ``functions/workflow_tuning.py``
handler against moto: the portal tables and the portal artifacts bucket from
the shared ``aws_stack`` fixture, the tuning single table and the Use_Case's
Sample_Store created here.

What is asserted here (Requirements 8.1-8.6, 9.1, 9.2, 11.4): the new version
differs from the previous latest ONLY in the target node's ``prompt``
(``prompt_template`` for ``llm_inference``), ``system_prompt`` and
``max_tokens``; ``latest_version`` increases by exactly one; the stored
document is the canonical serialization the designer save path produces and
the version item has a designer save's shape (no validation, no compiled
architectures, no component, Custom_Node_Type pins carried); the selection
and its completed Score_Run are required; a target that is no longer a
Tunable_Node is refused with no new version and no success audit; the
``apply_prompt_tuning`` audit event and the session's Tuning_Result carry the
fields Requirement 8.3 names; and authorization (``workflow:save``) precedes
every other check.

The invariants over the same space are task 6.4's Property 14/15/16 tests and
task 6.5's wider integration matrix; this file states expectations literally —
the version item's shape and the canonical document are restated/recomputed
here rather than taken from the handler's own return value, so a change in the
save path cannot move both the code and its expectation together.
"""
import json
import os
import sys
import uuid
from decimal import Decimal

import boto3
import pytest

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TUNING_TABLE_NAME = "test-workflow-tuning"
SAMPLE_BUCKET = f"dda-inference-results-{ACCOUNT_ID}"
PORTAL_BUCKET = "test-portal-artifacts"

#: Restated locally (never imported): the Verdict_Instruction the executor
#: appends to every Anomaly_Mode user prompt.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.')

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
        "crop_margin_percent": 5,
    },
}
LLM_NODE = {
    "id": "llm_1",
    "type": "llm_inference",
    "position": {"x": 10, "y": 200},
    "parameters": {
        "modelName": "qwen2-vl",
        "prompt_template": "Inspect {trigger.payload_json.part}.",
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

NEW_PROMPT = "Describe the reference, describe the input, then compare."
NEW_SYSTEM = "You are a meticulous inspector."
NEW_MAX_TOKENS = 384


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
    """One Use_Case + workflow + Tuning_Session, with the Candidates, the
    Score_Runs and the selection seeded through the real routes where they
    exist and directly where a route would only slow the case down."""

    def __init__(self, stack, module):
        self.stack = stack
        self.module = module
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Tuning use case",
            "account_id": ACCOUNT_ID,
            "tuning_sample_export": True,
        })
        self.saver = self._user("DataScientist")     # read + edit + save
        self.reader = self._user("Viewer")           # read only
        self.operator = self._user("Operator")       # read, no save
        self.outsider = self._user("DataLabeler")    # no read at all
        self.workflow_id = None
        self.node_id = None

    # ------------------------------------------------------------- setup
    def _user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def definition_key(self, version, workflow_id=None):
        return (f"workflows/{self.usecase_id}/"
                f"{workflow_id or self.workflow_id}/versions/{version}/"
                f"workflow.json")

    def put_workflow(self, nodes, connections=None, version=1,
                     workflow_id=None, custom_node_types=None,
                     document=None):
        """A stored workflow at ``version``.

        The document is written in the canonical form the Workflow_Serializer
        produces (schemaVersion 1, nodes and connections ordered by id,
        sorted keys, 2-space indent), restated here rather than imported, so
        the applied version can be compared with it directly.
        """
        workflow_id = workflow_id or str(uuid.uuid4())
        document = document if document is not None else {
            "schemaVersion": 1,
            "nodes": sorted((json.loads(json.dumps(n)) for n in nodes),
                            key=lambda n: n["id"]),
            "connections": sorted(
                (json.loads(json.dumps(c))
                 for c in (connections if connections is not None else [])),
                key=lambda c: c["id"]),
        }
        key = self.definition_key(version, workflow_id)
        self.s3.put_object(
            Bucket=PORTAL_BUCKET, Key=key,
            Body=json.dumps(document, sort_keys=True, indent=2,
                            ensure_ascii=True).encode("utf-8"))
        self.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "account_id": ACCOUNT_ID,
            "name": "Tuning workflow",
            "description": "A workflow with a tunable node",
            "created_at": 1,
            "updated_at": 1,
            "latest_version": version,
            "created_by": self.saver["user_id"],
        })
        version_item = {
            "workflow_id": workflow_id, "version": version,
            "s3_definition_key": key, "created_at": 1,
            "created_by": self.saver["user_id"],
            "validation_status": {"status": "none"},
            "compiled_arch_keys": {}, "component_arn": None,
        }
        if custom_node_types is not None:
            version_item["custom_node_types"] = custom_node_types
        self.stack.tables.versions.put_item(Item=version_item)
        self.workflow_id = workflow_id
        return workflow_id

    def workflow_item(self):
        return self.stack.tables.workflows.get_item(
            Key={"workflow_id": self.workflow_id})["Item"]

    def version_item(self, version):
        return self.stack.tables.versions.get_item(
            Key={"workflow_id": self.workflow_id,
                 "version": version}).get("Item")

    def stored_definition(self, version):
        item = self.version_item(version)
        assert item, f"no version item for v{version}"
        body = self.s3.get_object(Bucket=PORTAL_BUCKET,
                                  Key=item["s3_definition_key"])
        return json.loads(body["Body"].read().decode("utf-8"))

    def stored_json(self, version):
        item = self.version_item(version)
        body = self.s3.get_object(Bucket=PORTAL_BUCKET,
                                  Key=item["s3_definition_key"])
        return body["Body"].read().decode("utf-8")

    def table(self):
        return boto3.resource("dynamodb", region_name=REGION).Table(
            TUNING_TABLE_NAME)

    # ---------------------------------------------------------- invoke
    def call(self, method, resource, user, path_params=None, body=None,
             query=None, raw_body=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", str(value))
        event = {
            "httpMethod": method,
            "resource": resource,
            "path": path,
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

    def session_id(self, node_id=None):
        status, body = self.call(
            "POST", "/workflow-tuning/anomaly/sessions", self.saver,
            body={"workflow_id": self.workflow_id,
                  "node_id": node_id or self.node_id})
        assert status in (200, 201), body
        return body["session"]["sessionId"]

    def candidate(self, session_id, name="Rewritten prompt",
                  prompt=NEW_PROMPT, system_prompt=NEW_SYSTEM,
                  max_tokens=NEW_MAX_TOKENS):
        body = {"name": name, "prompt": prompt, "systemPrompt": system_prompt}
        if max_tokens is not None:
            body["maxTokens"] = max_tokens
        status, response = self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/candidates",
            self.saver, {"id": session_id}, body=body)
        assert status == 201, response
        return response["candidate"]["candidateId"]

    def seed_run(self, session_id, candidate_id, status="completed",
                 summary=None, started_at=1000, run_id=None):
        """One Score_Run item (plus its by-id pointer) in a terminal state.

        Driving the real scorer is one end-to-end case below; every other
        case only needs a run in a given state.
        """
        run_id = run_id or str(uuid.uuid4())
        summary = summary if summary is not None else {
            "samples": 10, "invocations": 10, "correct": 9, "falsePass": 1,
            "falseFail": 0, "parseFailure": 0, "invocationError": 0,
            "accuracy": 0.9, "unstable": 0, "meanOutputTokens": 120,
            "maxOutputTokens": 190, "meanLatencyMs": 2500,
        }
        item = {
            "pk": f"SESSION#{session_id}", "sk": f"RUN#{run_id}",
            "runId": run_id, "sessionId": session_id,
            "usecaseId": self.usecase_id, "workflowId": self.workflow_id,
            "nodeId": self.node_id, "candidateId": candidate_id,
            "status": status, "mode": "bedrock", "repeats": 1,
            "plannedInvocations": 10, "done": 10,
            "startedAt": started_at, "finishedAt": started_at + 10,
            "summary": summary,
        }
        self.table().put_item(
            Item=json.loads(json.dumps(item), parse_float=Decimal))
        self.table().put_item(Item={"pk": f"RUN#{run_id}", "sk": "META",
                                    "runId": run_id,
                                    "sessionId": session_id})
        return run_id

    def select(self, session_id, candidate_id, user=None):
        status, body = self.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/selection",
            user or self.saver, {"id": session_id},
            body={"candidateId": candidate_id})
        assert status == 200, body
        return body

    def apply(self, session_id, user=None, body=None, raw_body=None):
        return self.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/apply",
            user or self.saver, {"id": session_id},
            body=body if body is not None else {}, raw_body=raw_body)

    def session_item(self, session_id):
        return json.loads(json.dumps(
            self.table().get_item(
                Key={"pk": f"SESSION#{session_id}", "sk": "META"})["Item"],
            default=float))

    def audit_events(self, action=None):
        """This Use_Case's audit events (the table is shared by the whole
        module, so filter to this Env's own workflow)."""
        items = self.stack.tables.audit_log.scan().get("Items", [])
        events = [json.loads(json.dumps(i, default=float)) for i in items]
        events = [e for e in events
                  if (e.get("details") or {}).get("usecase_id")
                  == self.usecase_id]
        if action:
            events = [e for e in events if e.get("action") == action]
        return events


@pytest.fixture
def env(aws_stack, tuning):
    return Env(aws_stack, tuning)


@pytest.fixture
def bedrock_env(env):
    """A workflow whose ``bedrock_inference`` node is the Tunable_Node, with
    a session, a Candidate, a completed Score_Run and the selection made."""
    env.put_workflow([BEDROCK_NODE, LLM_NODE, CAMERA_NODE], [CONNECTION])
    env.node_id = BEDROCK_NODE["id"]
    env.session = env.session_id()
    env.candidate_id = env.candidate(env.session)
    env.run_id = env.seed_run(env.session, env.candidate_id)
    env.select(env.session, env.candidate_id)
    return env


def node_of(document, node_id):
    for node in document.get("nodes") or []:
        if node.get("id") == node_id:
            return node
    raise AssertionError(f"node {node_id} not in document")


# ==========================================================================
# 1. The applied version (Requirements 8.1, 8.6, 11.4)
# ==========================================================================

class TestAppliedVersion:

    def test_apply_saves_a_new_version_with_the_prompt_set(self,
                                                           bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 200, body
        assert body["version"] == 2
        assert body["newVersion"] == 2
        assert body["previousVersion"] == 1
        assert body["workflowId"] == bedrock_env.workflow_id
        assert body["nodeId"] == BEDROCK_NODE["id"]

        node = node_of(bedrock_env.stored_definition(2), BEDROCK_NODE["id"])
        assert node["parameters"]["prompt"] == NEW_PROMPT
        assert node["parameters"]["system_prompt"] == NEW_SYSTEM
        assert node["parameters"]["max_tokens"] == NEW_MAX_TOKENS

    def test_only_the_three_prompt_set_parameters_change(self, bedrock_env):
        before = bedrock_env.stored_definition(1)
        status, _body = bedrock_env.apply(bedrock_env.session)
        assert status == 200
        after = bedrock_env.stored_definition(2)

        # Reverting exactly the three parameters must restore the previous
        # document (canonically compared), i.e. nothing else moved.
        reverted = json.loads(json.dumps(after))
        node = node_of(reverted, BEDROCK_NODE["id"])
        for key in ("prompt", "system_prompt", "max_tokens"):
            node["parameters"][key] = BEDROCK_NODE["parameters"][key]
        assert json.dumps(reverted, sort_keys=True) == json.dumps(
            before, sort_keys=True)

        # And every other node/connection is byte-identical.
        for node_id in (LLM_NODE["id"], CAMERA_NODE["id"]):
            assert node_of(after, node_id) == node_of(before, node_id)
        assert after["connections"] == before["connections"]

    def test_the_previous_version_is_untouched(self, bedrock_env):
        before_json = bedrock_env.stored_json(1)
        before_item = bedrock_env.version_item(1)
        bedrock_env.apply(bedrock_env.session)
        assert bedrock_env.stored_json(1) == before_json
        assert bedrock_env.version_item(1) == before_item

    def test_latest_version_increases_by_exactly_one(self, bedrock_env):
        before = bedrock_env.workflow_item()
        bedrock_env.apply(bedrock_env.session)
        after = bedrock_env.workflow_item()
        assert int(after["latest_version"]) == int(
            before["latest_version"]) + 1
        assert int(after["updated_at"]) > int(before["updated_at"])
        # Metadata a designer save of the same edit would not touch.
        assert after["name"] == before["name"]
        assert after["description"] == before["description"]
        assert after["created_by"] == before["created_by"]

    def test_the_document_is_the_canonical_serialization(self, bedrock_env):
        bedrock_env.apply(bedrock_env.session)
        stored = bedrock_env.stored_json(2)
        document = json.loads(stored)
        # Canonical form: sorted keys, 2-space indent, ASCII, nodes ordered
        # by id, and schemaVersion 1 (workflow_core.serializer.serialize).
        assert stored == json.dumps(document, sort_keys=True, indent=2,
                                    ensure_ascii=True)
        assert document["schemaVersion"] == 1
        assert [n["id"] for n in document["nodes"]] == sorted(
            n["id"] for n in document["nodes"])

    def test_the_definition_key_follows_the_designer_layout(self,
                                                            bedrock_env):
        bedrock_env.apply(bedrock_env.session)
        assert bedrock_env.version_item(2)["s3_definition_key"] == \
            bedrock_env.definition_key(2)

    def test_the_version_item_has_a_designer_saves_shape(self, bedrock_env):
        bedrock_env.apply(bedrock_env.session)
        item = json.loads(json.dumps(bedrock_env.version_item(2),
                                     default=float))
        assert item["workflow_id"] == bedrock_env.workflow_id
        assert int(item["version"]) == 2
        assert item["created_by"] == bedrock_env.saver["user_id"]
        assert item["created_at"] > 0
        # Requirement 8.5: applying validates, packages and deploys nothing.
        assert item["validation_status"] == {"status": "none"}
        assert item["compiled_arch_keys"] == {}
        assert item["component_arn"] is None
        assert item["custom_node_types"] == {}

    def test_custom_node_type_pins_are_carried_forward(self, env):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE], [CONNECTION],
                         custom_node_types={"my.type": 3})
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session)
        env.seed_run(session, candidate_id)
        env.select(session, candidate_id)
        status, _body = env.apply(session)
        assert status == 200
        item = json.loads(json.dumps(env.version_item(2), default=float))
        assert item["custom_node_types"] == {"my.type": 3}

    def test_applying_twice_allocates_successive_versions(self,
                                                          bedrock_env):
        assert bedrock_env.apply(bedrock_env.session)[1]["version"] == 2
        # The selection and its completed run still stand, so a second
        # apply saves another version off the new latest.
        assert bedrock_env.apply(bedrock_env.session)[1]["version"] == 3
        assert int(bedrock_env.workflow_item()["latest_version"]) == 3
        assert bedrock_env.stored_definition(3) == \
            bedrock_env.stored_definition(2)

    def test_llm_nodes_receive_the_prompt_template(self, env):
        env.put_workflow([LLM_NODE, CAMERA_NODE])
        env.node_id = LLM_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session, prompt="Inspect {trigger.x}.")
        env.seed_run(session, candidate_id)
        env.select(session, candidate_id)
        status, body = env.apply(session)
        assert status == 200, body
        parameters = node_of(env.stored_definition(2),
                             LLM_NODE["id"])["parameters"]
        assert parameters["prompt_template"] == "Inspect {trigger.x}."
        assert parameters["system_prompt"] == NEW_SYSTEM
        assert parameters["max_tokens"] == NEW_MAX_TOKENS
        # The non-tunable Node_Parameters are untouched.
        assert parameters["modelName"] == "qwen2-vl"
        assert parameters["temperature"] == 0.2
        assert "prompt" not in parameters

    def test_an_empty_system_prompt_clears_an_existing_one(self, bedrock_env):
        candidate_id = bedrock_env.candidate(
            bedrock_env.session, name="No system prompt", system_prompt="")
        bedrock_env.seed_run(bedrock_env.session, candidate_id)
        bedrock_env.select(bedrock_env.session, candidate_id)
        status, _body = bedrock_env.apply(bedrock_env.session)
        assert status == 200
        parameters = node_of(bedrock_env.stored_definition(2),
                             BEDROCK_NODE["id"])["parameters"]
        assert parameters["system_prompt"] == ""

    def test_an_empty_system_prompt_is_not_introduced(self, env):
        """A node without a system prompt and a Candidate without one keeps
        the parameter absent: nothing beyond the Prompt_Set is written."""
        node = json.loads(json.dumps(BEDROCK_NODE))
        node["parameters"].pop("system_prompt")
        env.put_workflow([node, CAMERA_NODE])
        env.node_id = node["id"]
        session = env.session_id()
        candidate_id = env.candidate(session, system_prompt="")
        env.seed_run(session, candidate_id)
        env.select(session, candidate_id)
        assert env.apply(session)[0] == 200
        parameters = node_of(env.stored_definition(2),
                             node["id"])["parameters"]
        assert "system_prompt" not in parameters
        assert parameters["prompt"] == NEW_PROMPT

    def test_applying_the_baseline_reproduces_the_deployed_prompt_set(self,
                                                                      env):
        """The Baseline_Candidate is the deployed Prompt_Set, so applying it
        stores a document equal to the previous latest one."""
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        env.seed_run(session, "baseline")
        env.select(session, "baseline")
        status, body = env.apply(session)
        assert status == 200, body
        assert json.dumps(env.stored_definition(2), sort_keys=True) == \
            json.dumps(env.stored_definition(1), sort_keys=True)

    def test_a_baseline_without_max_tokens_leaves_the_parameter_absent(self,
                                                                       env):
        node = json.loads(json.dumps(BEDROCK_NODE))
        node["parameters"].pop("max_tokens")
        env.put_workflow([node, CAMERA_NODE])
        env.node_id = node["id"]
        session = env.session_id()
        env.seed_run(session, "baseline")
        env.select(session, "baseline")
        assert env.apply(session)[0] == 200
        parameters = node_of(env.stored_definition(2),
                             node["id"])["parameters"]
        assert "max_tokens" not in parameters

    def test_the_baseline_candidate_follows_the_new_version(self,
                                                            bedrock_env):
        """Requirement 5.1: the workflow gained a new latest version, so the
        session's Baseline_Candidate becomes the applied Prompt_Set."""
        bedrock_env.apply(bedrock_env.session)
        status, body = bedrock_env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}",
            bedrock_env.saver, {"id": bedrock_env.session})
        assert status == 200, body
        assert body["session"]["baselineVersion"] == 2
        baseline = [c for c in body["candidates"] if c["isBaseline"]][0]
        assert baseline["prompt"] == NEW_PROMPT
        assert baseline["systemPrompt"] == NEW_SYSTEM
        assert baseline["maxTokens"] == NEW_MAX_TOKENS
        # The scored Candidate and its run survive as history.
        scored = [c for c in body["candidates"]
                  if c["candidateId"] == bedrock_env.candidate_id][0]
        assert scored["latestRun"]["runId"] == bedrock_env.run_id


# ==========================================================================
# 2. Preconditions (Requirements 8.2, 8.4)
# ==========================================================================

class TestPreconditions:

    def test_apply_without_a_selection_is_409(self, env):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session)
        env.seed_run(session, candidate_id)
        status, body = env.apply(session)
        assert status == 409
        assert body["error"]["code"] == "NO_SELECTION"
        assert env.version_item(2) is None

    def test_a_candidate_id_that_is_not_the_selection_is_409(self,
                                                             bedrock_env):
        other = bedrock_env.candidate(bedrock_env.session, name="Other")
        status, body = bedrock_env.apply(bedrock_env.session,
                                         body={"candidateId": other})
        assert status == 409
        assert body["error"]["code"] == "CANDIDATE_NOT_SELECTED"
        assert body["error"]["details"]["selectedCandidateId"] == \
            bedrock_env.candidate_id
        assert bedrock_env.version_item(2) is None

    def test_the_selected_candidate_id_may_be_confirmed_in_the_body(self,
                                                                     bedrock_env):
        status, _body = bedrock_env.apply(
            bedrock_env.session, body={"candidateId": bedrock_env.candidate_id})
        assert status == 200

    def test_a_selection_pointing_at_a_deleted_candidate_is_404(self,
                                                                bedrock_env):
        bedrock_env.table().delete_item(
            Key={"pk": f"SESSION#{bedrock_env.session}",
                 "sk": f"CAND#{bedrock_env.candidate_id}"})
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 404
        assert body["error"]["code"] == "CANDIDATE_NOT_FOUND"

    @pytest.mark.parametrize("status_value", ["running", "cancelled",
                                             "failed"])
    def test_a_candidate_without_a_completed_run_is_409(self, env,
                                                        status_value):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session)
        env.seed_run(session, candidate_id, status=status_value)
        env.select(session, candidate_id)
        status, body = env.apply(session)
        assert status == 409
        assert body["error"]["code"] == "NO_COMPLETED_RUN"
        assert body["error"]["details"]["candidateId"] == candidate_id
        assert env.version_item(2) is None

    def test_a_candidate_with_no_run_at_all_is_409(self, env):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session)
        env.select(session, candidate_id)
        status, body = env.apply(session)
        assert status == 409
        assert body["error"]["code"] == "NO_COMPLETED_RUN"

    def test_another_candidates_completed_run_does_not_qualify(self, env):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        scored = env.candidate(session, name="Scored")
        unscored = env.candidate(session, name="Unscored")
        env.seed_run(session, scored)
        env.select(session, unscored)
        status, body = env.apply(session)
        assert status == 409
        assert body["error"]["code"] == "NO_COMPLETED_RUN"

    def test_the_most_recent_completed_run_is_recorded(self, bedrock_env):
        newer = bedrock_env.seed_run(bedrock_env.session,
                                     bedrock_env.candidate_id,
                                     started_at=5000)
        bedrock_env.seed_run(bedrock_env.session, bedrock_env.candidate_id,
                             status="failed", started_at=9000)
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 200
        assert body["tuningResult"]["scoreRunId"] == newer

    def test_a_named_run_is_used_when_given(self, bedrock_env):
        bedrock_env.seed_run(bedrock_env.session, bedrock_env.candidate_id,
                             started_at=5000)
        status, body = bedrock_env.apply(bedrock_env.session,
                                         body={"runId": bedrock_env.run_id})
        assert status == 200
        assert body["tuningResult"]["scoreRunId"] == bedrock_env.run_id

    def test_a_named_run_of_another_candidate_is_404(self, bedrock_env):
        other = bedrock_env.candidate(bedrock_env.session, name="Other")
        other_run = bedrock_env.seed_run(bedrock_env.session, other)
        status, body = bedrock_env.apply(bedrock_env.session,
                                         body={"runId": other_run})
        assert status == 404
        assert body["error"]["code"] == "RUN_NOT_FOUND"

    def test_a_named_run_that_is_not_completed_is_409(self, bedrock_env):
        cancelled = bedrock_env.seed_run(bedrock_env.session,
                                         bedrock_env.candidate_id,
                                         status="cancelled")
        status, body = bedrock_env.apply(bedrock_env.session,
                                         body={"runId": cancelled})
        assert status == 409
        assert body["error"]["code"] == "RUN_NOT_COMPLETED"
        assert body["error"]["details"]["status"] == "cancelled"

    def test_a_named_run_that_does_not_exist_is_404(self, bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session,
                                         body={"runId": "no-such-run"})
        assert status == 404
        assert body["error"]["code"] == "RUN_NOT_FOUND"

    def test_a_node_no_longer_in_anomaly_mode_is_refused(self, bedrock_env):
        """Requirement 8.4: no new version, no success audit."""
        node = json.loads(json.dumps(BEDROCK_NODE))
        node["parameters"]["anomaly_mode"] = False
        bedrock_env.put_workflow([node, CAMERA_NODE], version=2,
                                 workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 409
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        details = body["error"]["details"]
        assert details["nodeId"] == BEDROCK_NODE["id"]
        assert details["nodeType"] == "bedrock_inference"
        assert details["anomaly_mode"] is False
        assert details["version"] == 2
        assert bedrock_env.version_item(3) is None
        assert int(bedrock_env.workflow_item()["latest_version"]) == 2
        assert bedrock_env.audit_events("apply_prompt_tuning") == []

    def test_a_node_that_no_longer_exists_is_refused(self, bedrock_env):
        bedrock_env.put_workflow([CAMERA_NODE], version=2,
                                 workflow_id=bedrock_env.workflow_id)
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 409
        assert body["error"]["code"] == "NODE_NOT_TUNABLE"
        assert body["error"]["details"]["nodeType"] is None
        assert bedrock_env.version_item(3) is None

    def test_a_concurrent_save_is_refused_without_consuming_a_version(
            self, bedrock_env, monkeypatch):
        """Apply is a read-modify-write of a document the caller never
        sent, so a version saved in between must not be discarded."""
        module = bedrock_env.module
        original = module.load_latest_definition

        def stale(workflow_item):
            version, document = original(workflow_item)
            # Someone else saves v2 while this apply holds v1.
            bedrock_env.stack.tables.workflows.update_item(
                Key={"workflow_id": bedrock_env.workflow_id},
                UpdateExpression="SET latest_version = :v",
                ExpressionAttributeValues={":v": version + 1})
            return version, document

        monkeypatch.setattr(module, "load_latest_definition", stale)
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 409
        assert body["error"]["code"] == "STALE_DEFINITION"
        assert int(bedrock_env.workflow_item()["latest_version"]) == 2
        assert bedrock_env.version_item(3) is None

    def test_a_definition_the_designer_would_reject_is_400(self, env):
        """The stored document is patched and re-canonicalized, so a
        document the Workflow_Serializer rejects fails the apply with the
        designer save path's own error — and saves nothing."""
        env.put_workflow(
            None, document={"schemaVersion": 1,
                            "nodes": [BEDROCK_NODE, CAMERA_NODE],
                            "connections": [],
                            "unexpected": "field"})
        env.node_id = BEDROCK_NODE["id"]
        session = env.session_id()
        candidate_id = env.candidate(session)
        env.seed_run(session, candidate_id)
        env.select(session, candidate_id)
        status, body = env.apply(session)
        assert status == 400
        assert body["error"]["code"] in ("SCHEMA_VIOLATION",
                                        "INVALID_DEFINITION")
        assert env.version_item(2) is None
        assert int(env.workflow_item()["latest_version"]) == 1


# ==========================================================================
# 3. Tuning_Result and audit (Requirement 8.3)
# ==========================================================================

class TestTuningResultAndAudit:

    def test_the_session_records_the_tuning_result(self, bedrock_env):
        baseline_run = bedrock_env.seed_run(
            bedrock_env.session, "baseline",
            summary={"samples": 10, "invocations": 10, "correct": 2,
                     "falsePass": 4, "falseFail": 4, "parseFailure": 0,
                     "invocationError": 0, "accuracy": 0.2, "unstable": 0,
                     "meanOutputTokens": 23, "maxOutputTokens": 23,
                     "meanLatencyMs": 1500})
        assert baseline_run
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 200

        result = bedrock_env.session_item(
            bedrock_env.session)["latestTuningResult"]
        assert result["newVersion"] == 2
        assert result["previousVersion"] == 1
        assert result["candidateId"] == bedrock_env.candidate_id
        assert result["candidateName"] == "Rewritten prompt"
        assert result["scoreRunId"] == bedrock_env.run_id
        assert result["appliedBy"] == bedrock_env.saver["user_id"]
        assert result["appliedAt"] > 0
        assert result["summary"]["accuracy"] == 0.9
        assert result["summary"]["falsePass"] == 1
        # Both summaries travel with the result so the node panel can show
        # the improvement (Requirement 1.4).
        assert result["baselineSummary"]["accuracy"] == 0.2
        assert body["tuningResult"] == result
        assert body["session"]["latestTuningResult"] == result

    def test_the_baseline_summary_is_absent_without_a_baseline_run(self,
                                                                   bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session)
        assert status == 200
        assert body["tuningResult"]["baselineSummary"] is None

    def test_the_audit_event_carries_the_required_fields(self, bedrock_env):
        bedrock_env.apply(bedrock_env.session)
        events = bedrock_env.audit_events("apply_prompt_tuning")
        assert len(events) == 1
        event = events[0]
        assert event["user_id"] == bedrock_env.saver["user_id"]
        assert event["resource_type"] == "workflow"
        assert event["resource_id"] == bedrock_env.workflow_id
        assert event["result"] == "success"
        details = event["details"]
        assert details["workflow_id"] == bedrock_env.workflow_id
        assert int(details["version"]) == 2
        assert details["node_id"] == BEDROCK_NODE["id"]
        assert details["session_id"] == bedrock_env.session
        assert details["candidate_id"] == bedrock_env.candidate_id
        assert details["score_run_id"] == bedrock_env.run_id
        assert details["usecase_id"] == bedrock_env.usecase_id

    def test_the_response_carries_the_applied_prompt_set(self, bedrock_env):
        _status, body = bedrock_env.apply(bedrock_env.session)
        assert body["promptSet"] == {"prompt": NEW_PROMPT,
                                     "systemPrompt": NEW_SYSTEM,
                                     "maxTokens": NEW_MAX_TOKENS}
        assert body["candidate"]["candidateId"] == bedrock_env.candidate_id
        assert body["run"]["runId"] == bedrock_env.run_id


# ==========================================================================
# 4. Authorization (Requirements 9.1, 9.2)
# ==========================================================================

class TestAuthorization:

    def test_a_reader_cannot_apply(self, bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session,
                                         user=bedrock_env.reader)
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert body["error"]["details"]["required_permissions"] == [
            "workflow:save"]
        assert bedrock_env.version_item(2) is None
        denials = [e for e in bedrock_env.audit_events("unauthorized_access")
                   if e["details"].get("required_permissions") ==
                   ["workflow:save"]]
        assert denials, "the denial must be audited"

    def test_an_operator_without_save_cannot_apply(self, bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session,
                                         user=bedrock_env.operator)
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_a_non_reader_gets_the_uniform_404(self, bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session,
                                         user=bedrock_env.outsider)
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
        assert bedrock_env.version_item(2) is None

    def test_an_unknown_session_is_the_uniform_404(self, bedrock_env):
        status, body = bedrock_env.apply("no-such-session")
        assert status == 404
        assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_authorization_precedes_every_other_check(self, bedrock_env):
        """A reader whose request is also malformed and whose session has no
        completed run still sees the 403 (Requirement 9.2)."""
        bedrock_env.table().update_item(
            Key={"pk": f"SESSION#{bedrock_env.session}", "sk": "META"},
            UpdateExpression="SET selectedCandidateId = :n",
            ExpressionAttributeValues={":n": None})
        status, body = bedrock_env.apply(bedrock_env.session,
                                         user=bedrock_env.reader,
                                         raw_body="{not json")
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_a_malformed_body_is_400_for_an_authorized_caller(self,
                                                              bedrock_env):
        status, body = bedrock_env.apply(bedrock_env.session,
                                         raw_body="{not json")
        assert status == 400
        assert body["error"]["code"] == "INVALID_JSON"


# ==========================================================================
# 5. Requirement 8.5 and 11.4: nothing else happens
# ==========================================================================

class TestNoSideEffects:

    def test_apply_neither_validates_packages_nor_deploys(self, bedrock_env):
        deployments_before = bedrock_env.stack.tables.deployments.scan().get(
            "Count", 0)
        bedrock_env.apply(bedrock_env.session)
        item = bedrock_env.version_item(2)
        assert item.get("validation_status") == {"status": "none"}
        assert item.get("compiled_arch_keys") == {}
        assert item.get("component_arn") is None
        assert bedrock_env.stack.tables.deployments.scan().get(
            "Count", 0) == deployments_before
        # No packaging artifact was written beside the definition.
        keys = [o["Key"] for o in bedrock_env.s3.list_objects_v2(
            Bucket=PORTAL_BUCKET,
            Prefix=(f"workflows/{bedrock_env.usecase_id}/"
                    f"{bedrock_env.workflow_id}/")).get("Contents", [])]
        assert sorted(keys) == [bedrock_env.definition_key(1),
                                bedrock_env.definition_key(2)]

    def test_the_samples_labels_and_runs_survive_an_apply(self, bedrock_env):
        table = bedrock_env.table()
        before = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={
                ":pk": f"SESSION#{bedrock_env.session}"}).get("Items", [])
        bedrock_env.apply(bedrock_env.session)
        after = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={
                ":pk": f"SESSION#{bedrock_env.session}"}).get("Items", [])
        assert {i["sk"] for i in before} <= {i["sk"] for i in after}
        runs_before = [i for i in before if str(i["sk"]).startswith("RUN#")]
        runs_after = [i for i in after if str(i["sk"]).startswith("RUN#")]
        assert runs_before == runs_after

    def test_another_workflow_is_untouched(self, bedrock_env):
        other_id = bedrock_env.put_workflow([BEDROCK_NODE, CAMERA_NODE])
        # put_workflow re-points env.workflow_id; restore the session's one.
        bedrock_env.workflow_id = json.loads(json.dumps(
            bedrock_env.session_item(bedrock_env.session)))["workflowId"]
        assert other_id != bedrock_env.workflow_id
        bedrock_env.apply(bedrock_env.session)
        other = bedrock_env.stack.tables.workflows.get_item(
            Key={"workflow_id": other_id})["Item"]
        assert int(other["latest_version"]) == 1


# ==========================================================================
# 6. End to end: score a Candidate for real, then apply it
# ==========================================================================

class TestEndToEnd:
    """Requirement 8.1/8.2 through the real Score_Run path: index → label →
    score → select → apply, with the Bedrock transport and the self-invoked
    execution step driven inline."""

    def test_score_then_apply(self, env, monkeypatch):
        env.put_workflow([BEDROCK_NODE, CAMERA_NODE], [CONNECTION])
        env.node_id = BEDROCK_NODE["id"]

        # Two exported Tuning_Samples, as the device writes them.
        prefix = (f"workflow-tuning/samples/{env.workflow_id}/"
                  f"{env.node_id}/dev-1/")
        sample_ids = []
        for index, verdict in enumerate((True, False)):
            execution = f"exec-{index}"
            base = f"{prefix}{execution}"
            payload = f"INPUT-{index}".encode()
            env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".input.jpg",
                              Body=payload)
            env.s3.put_object(Bucket=SAMPLE_BUCKET, Key=base + ".json",
                              Body=json.dumps({
                                  "schemaVersion": 1, "source": "live",
                                  "workflowId": env.workflow_id, "version": 1,
                                  "executionId": execution,
                                  "nodeId": env.node_id,
                                  "nodeType": "bedrock_inference",
                                  "thingName": "dev-1", "exportedAt": 1000 + index,
                                  "input": {"key": base + ".input.jpg",
                                            "sha256": f"hash-{index}",
                                            "bytes": len(payload)},
                                  "recorded": {"isAnomalous": verdict,
                                               "confidence": 0.9,
                                               "answer": "{}",
                                               "parseError": None},
                                  "promptFingerprint": "sha256:old",
                              }).encode("utf-8"))
            sample_ids.append(f"dev-1/{execution}")

        session = env.session_id()
        status, body = env.call(
            "PUT", "/workflow-tuning/anomaly/sessions/{id}/samples/labels",
            env.saver, {"id": session},
            body={"sampleIds": sample_ids, "label": "NOK"})
        assert status == 200, body

        candidate_id = env.candidate(session)

        # Drive the scorer: a recording dispatcher plus a scripted Converse.
        dispatched = []
        monkeypatch.setattr(env.module, "dispatch_action",
                            lambda payload: dispatched.append(dict(payload)))

        class Bedrock:
            def __init__(self):
                self.calls = []

            def converse(self, **kwargs):
                self.calls.append(kwargs)
                return {"output": {"message": {"content": [
                    {"text": '{"is_anomalous": true, "confidence": 0.95}'}]}},
                    "usage": {"outputTokens": 120}}

        bedrock = Bedrock()
        monkeypatch.setattr(env.module, "bedrock_client",
                            lambda region: bedrock)

        status, body = env.call(
            "POST", "/workflow-tuning/anomaly/sessions/{id}/score-runs",
            env.saver, {"id": session}, body={"candidateId": candidate_id})
        assert status == 202, body
        run_id = body["runId"]
        while dispatched:
            env.module.handler(dispatched.pop(0), None)

        status, body = env.call("GET",
                                "/workflow-tuning/anomaly/score-runs/{rid}",
                                env.saver, {"rid": run_id})
        assert status == 200, body
        assert body["run"]["status"] == "completed"
        assert body["run"]["summary"]["correct"] == 2

        # The replay used the Candidate's prompt plus the Verdict_Instruction.
        assert len(bedrock.calls) == 2
        sent = bedrock.calls[0]["messages"][0]["content"][0]["text"]
        assert sent.startswith(NEW_PROMPT)
        assert sent.endswith(VERDICT_INSTRUCTION)

        env.select(session, candidate_id)
        status, body = env.apply(session)
        assert status == 200, body
        assert body["version"] == 2
        assert body["tuningResult"]["scoreRunId"] == run_id
        parameters = node_of(env.stored_definition(2),
                             BEDROCK_NODE["id"])["parameters"]
        assert parameters["prompt"] == NEW_PROMPT
        assert parameters["system_prompt"] == NEW_SYSTEM
        assert parameters["max_tokens"] == NEW_MAX_TOKENS
        # Exported samples and their Labels are untouched by an apply.
        status, body = env.call(
            "GET", "/workflow-tuning/anomaly/sessions/{id}/samples",
            env.saver, {"id": session})
        assert status == 200
        assert {s["sampleId"] for s in body["samples"]} == set(sample_ids)
        assert {s["label"] for s in body["samples"]} == {"NOK"}


# ==========================================================================
# 7. The save path is the designer's (Requirements 8.6, 11.4)
# ==========================================================================

class TestSavePathReuse:

    def test_apply_goes_through_the_workflows_module(self, tuning):
        """Canonicalization, the stored document, the version item and the
        Custom_Node_Type pins are the workflow handler's own functions — not
        a second copy in this module."""
        import inspect

        source = inspect.getsource(tuning.apply_candidate)
        for call in ("workflows.canonicalize_definition(",
                     "workflows.put_definition(",
                     "workflows.put_version_item(",
                     "workflows.custom_node_type_references("):
            assert call in source, call

    def test_the_version_allocation_matches_the_designer_saves(self, tuning):
        """The allocation is the same atomic increment ``PUT /workflows/{id}``
        performs (plus apply's stale-version guard)."""
        import ast
        import inspect
        import workflows

        def literals(function):
            """Every string literal of a function, implicit concatenation
            already joined by the parser."""
            tree = ast.parse(inspect.getsource(function).lstrip())
            return {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)}

        allocation = ('SET latest_version = latest_version + :one, '
                      'updated_at = :updated')
        assert allocation in literals(tuning.allocate_next_version)
        assert allocation in literals(workflows.update_workflow)
        assert any("attribute_exists(workflow_id)" in text
                   for text in literals(tuning.allocate_next_version))

    def test_apply_never_validates_packages_or_deploys(self, tuning):
        """Requirement 8.5 at the source level: the apply path calls no
        validation, packaging, compilation or deployment operation."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(tuning.apply_candidate).lstrip())
        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            while isinstance(target, ast.Attribute):
                called.add(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                called.add(target.id)
        for forbidden in ("validate", "package", "deploy", "greengrass",
                          "compil"):
            offenders = [name for name in called
                         if forbidden in name.lower()]
            assert not offenders, (forbidden, offenders)

    def test_only_the_three_parameters_are_written(self, tuning):
        """``patch_prompt_set`` is pure and writes nothing else."""
        document = {"schemaVersion": 1, "nodes": [
            json.loads(json.dumps(BEDROCK_NODE)),
            json.loads(json.dumps(CAMERA_NODE))], "connections": []}
        original = json.loads(json.dumps(document))
        patched = tuning.patch_prompt_set(
            document, BEDROCK_NODE["id"],
            {"prompt": "P", "systemPrompt": "S", "maxTokens": 99})
        assert document == original, "the caller's document is not mutated"
        parameters = node_of(patched, BEDROCK_NODE["id"])["parameters"]
        changed = {k for k, v in parameters.items()
                   if BEDROCK_NODE["parameters"].get(k) != v}
        assert changed == {"prompt", "system_prompt", "max_tokens"}
        assert node_of(patched, CAMERA_NODE["id"]) == node_of(
            original, CAMERA_NODE["id"])

    def test_patching_an_absent_node_changes_nothing(self, tuning):
        document = {"schemaVersion": 1,
                    "nodes": [json.loads(json.dumps(CAMERA_NODE))],
                    "connections": []}
        patched = tuning.patch_prompt_set(
            document, "nope", {"prompt": "P", "systemPrompt": "S",
                               "maxTokens": 1})
        assert patched == document
