"""Model-reference validation in the test run's Validate step.

Pins the fix for the validation divergence where a test run passed (and
proceeded to execute) a workflow whose model references the Validate
button rejected: step_validate now loads the same Model_Registry
snapshot the Validate endpoint uses (model_registry_snapshot.py) and
runs the validator with it, so MODEL_REF_UNRESOLVED findings
short-circuit the run before the sandbox executes (12.12;
vllm-triton-inference 6.5, 6.12). Registry read errors fail closed.
"""

import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TEST_RUNS_TABLE_NAME = "test-steps-registry-test-runs"
TRAINING_JOBS_TABLE_NAME = "test-steps-registry-training-jobs"
MODELS_TABLE_NAME = "test-steps-registry-models"

REGISTERED_VISION_MODEL = "cookies-binary"
REGISTERED_VLLM_MODEL = "qwen-chat"

# The user-reported shape: folder_source -> model_inference -> capture.
def definition_with_model(model_name):
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "n1", "type": "folder_source",
             "position": {"x": 100, "y": 100},
             "parameters": {"location": "/aws_dda/images"}},
            {"id": "n2", "type": "model_inference",
             "position": {"x": 350, "y": 100},
             "parameters": {"modelName": model_name}},
            {"id": "n3", "type": "capture",
             "position": {"x": 600, "y": 100},
             "parameters": {"output_path": "/aws_dda/captures"}},
        ],
        "connections": [
            {"id": "c1",
             "from": {"node": "n1", "port": "out"},
             "to": {"node": "n2", "port": "in"}},
            {"id": "c2",
             "from": {"node": "n2", "port": "out"},
             "to": {"node": "n3", "port": "in"}},
        ],
    }


@pytest.fixture(scope="module")
def steps_env(aws_stack):
    """TestRuns + Model_Registry tables and a freshly imported
    workflow_test_steps bound to them inside moto."""
    import boto3

    os.environ["TEST_RUNS_TABLE"] = TEST_RUNS_TABLE_NAME
    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    os.environ["MODELS_TABLE"] = MODELS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TEST_RUNS_TABLE_NAME,
        KeySchema=[{"AttributeName": "test_run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "test_run_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-training-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-models-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds the table names above and
    # moto-intercepted clients (test_workflow_testing_errors pattern).
    for module_name in ("workflow_test_steps", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_test_steps

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        steps=workflow_test_steps,
        s3=boto3.client("s3", region_name=REGION),
        bucket=os.environ["PORTAL_ARTIFACTS_BUCKET"],
        runs_table=resource.Table(TEST_RUNS_TABLE_NAME),
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models_table=resource.Table(MODELS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    os.environ.pop("MODELS_TABLE", None)
    sys.modules.pop("workflow_test_steps", None)


@pytest.fixture
def usecase(steps_env):
    """A fresh Use_Case with one vision and one vLLM model registered."""
    usecase_id = f"uc-{uuid.uuid4()}"
    steps_env.training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": REGISTERED_VISION_MODEL,
        "model_type": "anomaly_detection",
        "created_at": 1,
    })
    steps_env.training_table.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": REGISTERED_VLLM_MODEL,
        "model_type": "vllm",
        "created_at": 1,
    })
    return usecase_id


def stage_run(steps_env, definition, usecase_id):
    """Stage a stored definition + TestRuns item; returns the step input."""
    test_run_id = f"run-{uuid.uuid4()}"
    definition_key = f"workflows/{usecase_id}/defs/{test_run_id}.json"
    results_key = f"workflows/{usecase_id}/test-runs/{test_run_id}/results.json"
    steps_env.s3.put_object(
        Bucket=steps_env.bucket, Key=definition_key,
        Body=json.dumps(definition).encode("utf-8"),
    )
    steps_env.runs_table.put_item(Item={
        "test_run_id": test_run_id,
        "workflow_id": "wf-1",
        "usecase_id": usecase_id,
        "status": "running",
        "started_at": 1,
        "results_s3_key": results_key,
        "failure": None,
    })
    return {
        "test_run_id": test_run_id,
        "workflow_id": "wf-1",
        "workflow_version": 1,
        "usecase_id": usecase_id,
        "definition_s3_key": definition_key,
        "results_s3_key": results_key,
        "artifacts_bucket": steps_env.bucket,
        "target_arch": "x86_64",
        "simulation": True,
        "custom_node_type_pins": {},
    }


def validate(steps_env, inp):
    return steps_env.steps.handler({"step": "validate", "input": inp}, None)


def get_run(steps_env, test_run_id):
    return steps_env.runs_table.get_item(
        Key={"test_run_id": test_run_id})["Item"]


class TestModelReferenceValidation:

    def test_unresolved_model_reference_fails_the_run(self, steps_env, usecase):
        """A modelName that resolves to no registered model fails
        step_validate with MODEL_REF_UNRESOLVED on the inference node —
        the divergence the Validate button already caught (6.5)."""
        inp = stage_run(steps_env,
                        definition_with_model("no-such-model"), usecase)
        outcome = validate(steps_env, inp)

        assert outcome["ok"] is False
        assert outcome["stage"] == "validate"
        assert any(e["code"] == "MODEL_REF_UNRESOLVED" and
                   e["nodeId"] == "n2" for e in outcome["errors"])
        assert get_run(steps_env, inp["test_run_id"])["status"] == "failed"

    def test_vllm_record_on_model_inference_fails_the_run(self, steps_env,
                                                          usecase):
        """model_inference referencing a vllm-typed record fails with the
        model-type/node-family rule (6.12)."""
        inp = stage_run(steps_env,
                        definition_with_model(REGISTERED_VLLM_MODEL), usecase)
        outcome = validate(steps_env, inp)

        assert outcome["ok"] is False
        assert any(e["code"] == "MODEL_REF_UNRESOLVED" for e in outcome["errors"])

    def test_registered_vision_model_passes(self, steps_env, usecase):
        """The same workflow with a registered vision model validates
        clean — parity with the Validate endpoint's accept path."""
        inp = stage_run(steps_env,
                        definition_with_model(REGISTERED_VISION_MODEL),
                        usecase)
        outcome = validate(steps_env, inp)

        assert outcome["ok"] is True, outcome

    def test_registry_read_error_fails_closed(self, steps_env, usecase,
                                              monkeypatch):
        """A registry read failure fails the run rather than recording a
        validation pass that skipped the resolution check."""
        monkeypatch.setattr(steps_env.steps, "TRAINING_JOBS_TABLE",
                            "no-such-table")
        inp = stage_run(steps_env,
                        definition_with_model(REGISTERED_VISION_MODEL),
                        usecase)
        outcome = validate(steps_env, inp)

        assert outcome["ok"] is False
        assert outcome["errors"][0]["code"] == "MODEL_REGISTRY_LOAD_FAILED"
        run = get_run(steps_env, inp["test_run_id"])
        assert run["status"] == "failed"
        assert "not" in run["failure"]["message"] and \
            "executed" in run["failure"]["message"]

    def test_unconfigured_registry_skips_resolution(self, steps_env, usecase,
                                                    monkeypatch):
        """Without TRAINING_JOBS_TABLE the resolution check is skipped
        (pre-feature degradation, matching the Validate endpoint)."""
        monkeypatch.setattr(steps_env.steps, "TRAINING_JOBS_TABLE", None)
        inp = stage_run(steps_env,
                        definition_with_model("no-such-model"), usecase)
        outcome = validate(steps_env, inp)

        assert outcome["ok"] is True, outcome
