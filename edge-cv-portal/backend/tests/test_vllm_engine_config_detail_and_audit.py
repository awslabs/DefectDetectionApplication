"""
Unit tests for vLLM engine-configuration detail exposure, non-vLLM
rejection, and the update audit event
(vllm-sizing-and-packaging-errors, task 2.7).

Covers:
- `models.py get_model` includes the stored Engine_Configuration on the
  detail response for vLLM records, and omits it for non-vLLM (vision)
  records (Requirement 1.2)
- `PUT /api/v1/models/vllm/{training_id}/engine-configuration` against a
  non-vLLM (vision) record is rejected with HTTP 400 identifying the
  record as non-vLLM, leaving the record untouched (Requirement 2.3)
- a successful update writes an audit event carrying the previous and the
  updated Engine_Configuration values (Requirement 2.6)

Runs against the moto-backed conftest stack with the real
functions/models.py and functions/model_import.py handlers. The
weight-estimation seam (`estimate_weights`, a module attribute of
model_import) is stubbed to None so no network access happens.

_Requirements: 1.2, 2.3, 2.6_
"""
import json
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-engine-detail-audit"

STORED_ENGINE_CONFIGURATION = {
    "dtype": "bfloat16",
    "gpu_memory_utilization": Decimal("0.3"),
    "max_model_len": 4096,
    "tensor_parallel_size": 1,
    "enforce_eager": True,
}


@pytest.fixture(scope="module")
def env(aws_stack):
    """Training-jobs table + freshly imported models / model_import bound
    to it inside moto, estimation seam stubbed (no network)."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    sys.modules.pop("model_import", None)
    import model_import
    sys.modules.pop("models", None)
    import models

    mp.setattr(model_import, "estimate_weights",
               lambda record, s3_head=None, hf_fetch=None: None)

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        models=models,
        stack=aws_stack,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        audit_log=aws_stack.tables.audit_log,
    )
    mp.undo()


@pytest.fixture
def seeded(env):
    """Fresh Use_Case + DataScientist user per test."""
    usecase_id = f"uc-{uuid.uuid4()}"
    env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Engine Detail/Audit UC",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "DataScientist"}
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user=user)


def seed_record(env, seeded, **overrides):
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "detail-audit-llm",
        "model_version": "1.0",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/detail-audit"},
        "engine_configuration": dict(STORED_ENGINE_CONFIGURATION),
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    item.update(overrides)
    env.training_jobs.put_item(Item=item)
    return item


def claims(user):
    return {
        "authorizer": {
            "claims": {
                "sub": user["user_id"],
                "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": user["role"],
            }
        }
    }


def get_model_event(training_id, user):
    return {
        "httpMethod": "GET",
        "path": f"/api/v1/models/{training_id}",
        "pathParameters": {"id": training_id},
        "queryStringParameters": None,
        "body": None,
        "requestContext": claims(user),
    }


def put_engine_config_event(training_id, user, supplied):
    return {
        "httpMethod": "PUT",
        "path": f"/api/v1/models/vllm/{training_id}/engine-configuration",
        "pathParameters": {"training_id": training_id},
        "queryStringParameters": None,
        "body": json.dumps({"engine_configuration": supplied}),
        "requestContext": claims(user),
    }


def assert_config_equals(expected, actual):
    """Numeric equality across the Decimal/JSON representations."""
    assert set(actual) == set(expected), (
        f"expected settings {sorted(expected)}, got {sorted(actual)}")
    for key, value in expected.items():
        if isinstance(value, bool):
            assert actual[key] is value, (
                f"{key}: got {actual[key]!r}, expected {value!r}")
        elif isinstance(value, (int, float, Decimal)):
            assert Decimal(str(actual[key])) == Decimal(str(value)), (
                f"{key}: got {actual[key]!r}, expected {value!r}")
        else:
            assert actual[key] == value


# ---------------------------------------------------------------------------
# get_model includes engine_configuration for vLLM records (1.2)
# ---------------------------------------------------------------------------

def test_get_model_includes_engine_configuration_for_vllm(env, seeded):
    record = seed_record(env, seeded)

    response = env.models.get_model(
        get_model_event(record["training_id"], seeded.user), None)

    assert response["statusCode"] == 200, response["body"]
    model = json.loads(response["body"])["model"]
    assert "engine_configuration" in model, (
        "vLLM model detail must include the stored engine configuration")
    assert_config_equals(STORED_ENGINE_CONFIGURATION,
                         model["engine_configuration"])


def test_get_model_omits_engine_configuration_for_vision(env, seeded):
    record = seed_record(
        env, seeded,
        model_type="classification", source="imported",
        model_source=None, engine_configuration=None)

    response = env.models.get_model(
        get_model_event(record["training_id"], seeded.user), None)

    assert response["statusCode"] == 200, response["body"]
    model = json.loads(response["body"])["model"]
    assert "engine_configuration" not in model, (
        "non-vLLM model detail must not carry an engine_configuration")


# ---------------------------------------------------------------------------
# PUT against a non-vLLM (vision) record -> 400 (2.3)
# ---------------------------------------------------------------------------

def test_update_rejects_non_vllm_record(env, seeded):
    record = seed_record(
        env, seeded,
        model_name="vision-model",
        model_type="classification", source="imported",
        model_source=None, engine_configuration=None)

    response = env.model_import.handler(
        put_engine_config_event(record["training_id"], seeded.user,
                                {"gpu_memory_utilization": 0.6}), None)

    assert response["statusCode"] == 400, response["body"]
    body = json.loads(response["body"])
    assert "not a vLLM model" in body["error"], (
        f"the 400 must identify the record as non-vLLM, got {body['error']!r}")

    # The vision record is untouched.
    stored = env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert stored.get("engine_configuration") is None
    assert stored["updated_at"] == record["updated_at"]


# ---------------------------------------------------------------------------
# Audit event carries previous and updated values (2.6)
# ---------------------------------------------------------------------------

def test_update_audit_event_carries_previous_and_updated_values(env, seeded):
    record = seed_record(env, seeded)
    supplied = {"gpu_memory_utilization": 0.85, "max_model_len": 8192}

    response = env.model_import.handler(
        put_engine_config_event(record["training_id"], seeded.user,
                                supplied), None)
    assert response["statusCode"] == 200, response["body"]

    events = env.audit_log.scan(
        FilterExpression=Attr("user_id").eq(seeded.user["user_id"])
    ).get("Items", [])
    update_events = [e for e in events
                     if e["action"] == "update_vllm_engine_configuration"]
    assert len(update_events) == 1, (
        f"expected exactly one update audit event, got {len(update_events)}")
    event = update_events[0]

    assert event["result"] == "success"
    assert event["resource_type"] == "training_job"
    assert event["resource_id"] == record["training_id"]

    details = event["details"]
    assert_config_equals(STORED_ENGINE_CONFIGURATION,
                         details["previous_engine_configuration"])
    expected_updated = dict(STORED_ENGINE_CONFIGURATION)
    expected_updated.update(supplied)
    assert_config_equals(expected_updated,
                         details["updated_engine_configuration"])
