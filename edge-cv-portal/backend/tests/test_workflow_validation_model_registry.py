"""
Model_Registry snapshot wiring in the validation endpoint
(vllm-triton-inference task 1.10).

POST /workflows/{id}/validate loads the Use_Case's model records
(training-jobs table + models table) into the ``model_registry``
snapshot mapping and passes it to validate(), so MODEL_REF_UNRESOLVED
findings (Requirement 6.12) are produced by the production endpoint:

1. An llm_inference modelName resolving to a registered vllm-typed
   record produces no resolution finding.
2. An unresolvable reference produces a MODEL_REF_UNRESOLVED finding
   identifying the node and the reference.
3. The model-type/node-family rule applies: model_inference referencing
   a vllm record (and llm_inference referencing a vision record) fails.
4. Published component base names in the models table resolve via the
   backing training-job record's model type.
5. Without TRAINING_JOBS_TABLE configured, resolution is skipped
   (pre-feature behavior — the moto conftest stack takes this path).

_Requirements: 6.12_
"""
import json
import os
import sys
import uuid

import boto3
import pytest

from conftest import REGION

TRAINING_JOBS_TABLE = "test-training-jobs"
MODELS_TABLE = "test-models"


def _create_registry_tables(dynamodb):
    """The training-jobs and models tables with their usecase GSIs
    (storage-stack.ts shapes)."""
    dynamodb.create_table(
        TableName=TRAINING_JOBS_TABLE,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=MODELS_TABLE,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-models-index",
            "KeySchema": [
                {"AttributeName": "usecase_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(scope="module")
def registry_stack(aws_stack):
    """workflow_validation imported with the registry tables configured.

    Creates the training-jobs/models tables inside the session moto
    mock, points the env vars at them, and re-imports the handler so
    its module-level names bind. Teardown restores the conftest
    environment (no registry tables) and evicts the module so later
    test modules re-import the unconfigured variant.
    """
    client = boto3.client("dynamodb", region_name=REGION)
    _create_registry_tables(client)

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE
    os.environ["MODELS_TABLE"] = MODELS_TABLE
    sys.modules.pop("workflow_validation", None)
    import workflow_validation

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "module": workflow_validation,
        "training_jobs": resource.Table(TRAINING_JOBS_TABLE),
        "models": resource.Table(MODELS_TABLE),
    }

    os.environ.pop("TRAINING_JOBS_TABLE", None)
    os.environ.pop("MODELS_TABLE", None)
    sys.modules.pop("workflow_validation", None)


# ---------------------------------------------------------------- helpers

def seed_training_record(stack, usecase_id, model_name, model_type,
                         created_at=1):
    training_id = str(uuid.uuid4())
    stack["training_jobs"].put_item(Item={
        "training_id": training_id,
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_version": "1.0.0",
        "model_type": model_type,
        "status": "Completed",
        "created_at": created_at,
    })
    return training_id


def seed_models_table_record(stack, usecase_id, name, training_job_id,
                             created_at=1):
    """A published-model registry item (component base name spelling)."""
    stack["models"].put_item(Item={
        "model_id": f"{training_job_id}-1.0.0",
        "usecase_id": usecase_id,
        "name": name,
        "version": "1.0.0",
        "stage": "candidate",
        "training_job_id": training_job_id,
        "created_at": created_at,
    })


def seed_stored_workflow(env, usecase_id, definition):
    workflow_id = f"wf-{uuid.uuid4()}"
    s3_key = (f"workflows/{usecase_id}/{workflow_id}/versions/1/"
              f"workflow.json")
    env.s3.put_object(Bucket=env.bucket, Key=s3_key,
                      Body=json.dumps(definition).encode("utf-8"))
    env.stack.tables.workflows.put_item(Item={
        "workflow_id": workflow_id,
        "usecase_id": usecase_id,
        "name": "model-registry-test",
        "latest_version": 1,
        "created_at": 1,
        "updated_at": 1,
    })
    env.stack.tables.versions.put_item(Item={
        "workflow_id": workflow_id,
        "version": 1,
        "s3_definition_key": s3_key,
        "validation_status": {"status": "none"},
        "custom_node_types": {},
    })
    return workflow_id


def inference_workflow_definition(node_type, model_name):
    """camera_source -> model_inference -> capture, with an llm_inference
    or model_inference node carrying the model reference under test."""
    inference_parameters = {"modelName": model_name}
    if node_type == "llm_inference":
        inference_parameters["prompt_template"] = "Describe: {label}"
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "n1", "type": "camera_source",
             "position": {"x": 100, "y": 100}, "parameters": {}},
            {"id": "inf1", "type": node_type,
             "position": {"x": 350, "y": 100},
             "parameters": inference_parameters},
            {"id": "n3", "type": "capture",
             "position": {"x": 600, "y": 100},
             "parameters": {"output_path": "/data/captures"}},
        ],
        "connections": [],
    }


def validate_request(module, env, user, workflow_id):
    event = {
        "httpMethod": "POST",
        "resource": "/workflows/{id}/validate",
        "path": f"/workflows/{workflow_id}/validate",
        "pathParameters": {"id": workflow_id},
        "body": json.dumps({}),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }
    response = module.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def resolution_findings(body):
    return [f for f in body["findings"]
            if f["code"] == "MODEL_REF_UNRESOLVED"]


@pytest.fixture
def scenario(registry_stack, env):
    usecase_id = env.create_usecase("Model Registry Use Case")
    user = env.make_user()
    env.assign_role(user, usecase_id, "DataScientist")

    def run(node_type, model_name):
        workflow_id = seed_stored_workflow(
            env, usecase_id,
            inference_workflow_definition(node_type, model_name))
        return validate_request(registry_stack["module"], env, user,
                                workflow_id)

    return {"usecase_id": usecase_id, "user": user, "run": run,
            "stack": registry_stack, "env": env}


# ------------------------------------------------------------------ tests

class TestModelReferenceResolutionEndpoint:

    def test_llm_inference_resolves_registered_vllm_record(self, scenario):
        seed_training_record(scenario["stack"], scenario["usecase_id"],
                             "opt-125m", "vllm")
        status, body = scenario["run"]("llm_inference", "opt-125m")
        assert status == 200, body
        assert resolution_findings(body) == []

    def test_unresolvable_reference_produces_finding(self, scenario):
        """Requirement 6.12: the endpoint itself reports unresolvable
        llm_inference model references (node and reference named)."""
        status, body = scenario["run"]("llm_inference", "ghost-model")
        assert status == 200, body
        findings = resolution_findings(body)
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert findings[0]["nodeId"] == "inf1"
        assert "ghost-model" in findings[0]["message"]
        assert body["passed"] is False

    def test_model_type_node_family_rule_applies(self, scenario):
        seed_training_record(scenario["stack"], scenario["usecase_id"],
                             "opt-125m", "vllm")
        seed_training_record(scenario["stack"], scenario["usecase_id"],
                             "widget-anomaly", "classification")

        # model_inference must not accept a vllm record ...
        status, body = scenario["run"]("model_inference", "opt-125m")
        assert status == 200, body
        assert len(resolution_findings(body)) == 1

        # ... and llm_inference must not accept a vision record.
        status, body = scenario["run"]("llm_inference", "widget-anomaly")
        assert status == 200, body
        assert len(resolution_findings(body)) == 1

        # The right pairings both resolve.
        status, body = scenario["run"]("model_inference", "widget-anomaly")
        assert status == 200, body
        assert resolution_findings(body) == []

    def test_component_base_name_spelling_resolves(self, scenario):
        """Published models registered in the models table under their
        component base name resolve with the backing record's type."""
        training_id = seed_training_record(
            scenario["stack"], scenario["usecase_id"],
            "yolo_test", "object_detection")
        seed_models_table_record(scenario["stack"], scenario["usecase_id"],
                                 "model-yolo-test", training_id)

        status, body = scenario["run"]("model_inference", "model-yolo-test")
        assert status == 200, body
        assert resolution_findings(body) == []

    def test_records_of_other_usecases_do_not_resolve(self, scenario):
        other_usecase = scenario["env"].create_usecase("Other Use Case")
        seed_training_record(scenario["stack"], other_usecase,
                             "opt-125m", "vllm")
        status, body = scenario["run"]("llm_inference", "opt-125m")
        assert status == 200, body
        assert len(resolution_findings(body)) == 1


class TestSnapshotLoader:
    """Pure loader behavior against the moto tables."""

    def test_unconfigured_table_skips_resolution(self, registry_stack,
                                                 monkeypatch):
        module = registry_stack["module"]
        monkeypatch.setattr(module, "TRAINING_JOBS_TABLE", None)
        assert module.load_model_registry_snapshot("uc-any") is None

    def test_newest_record_wins_per_name(self, registry_stack, env):
        usecase_id = env.create_usecase("Snapshot Use Case")
        seed_training_record(registry_stack, usecase_id, "dup-model",
                             "classification", created_at=1)
        seed_training_record(registry_stack, usecase_id, "dup-model",
                             "vllm", created_at=2)

        snapshot = registry_stack["module"].load_model_registry_snapshot(
            usecase_id)
        assert snapshot["dup-model"]["model_type"] == "vllm"

    def test_training_record_wins_name_collisions_with_models_table(
            self, registry_stack, env):
        usecase_id = env.create_usecase("Collision Use Case")
        training_id = seed_training_record(
            registry_stack, usecase_id, "model-direct", "vllm")
        seed_models_table_record(registry_stack, usecase_id,
                                 "model-direct", "some-other-training-id")

        snapshot = registry_stack["module"].load_model_registry_snapshot(
            usecase_id)
        assert snapshot["model-direct"]["training_id"] == training_id
        assert snapshot["model-direct"]["model_type"] == "vllm"
