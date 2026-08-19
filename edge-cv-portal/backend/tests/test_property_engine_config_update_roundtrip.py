"""Property test for the vLLM engine-configuration update round trip
(vllm-sizing-and-packaging-errors, task 2.4).

**Feature: vllm-sizing-and-packaging-errors, Property 2: Engine-configuration
update round trip**

_For any_ existing vLLM_Model_Record and any valid partial update, the stored
Engine_Configuration afterward equals the previous configuration overlaid
with the supplied values, and the update response returns that same complete
configuration; a subsequent `GET /api/v1/models/{model_id}` reflects the same
configuration.

**Validates: Requirements 2.1, 2.4**

Runs against the moto-backed conftest stack with the real
functions/model_import.py PUT handler (through its router) and the real
functions/models.py get_model detail handler. The weight-estimation seam
(`estimate_weights`, imported as a module attribute of model_import) is
replaced with a stub returning None so no network access happens — the fit
check is irrelevant to this property and degrades to 'unverified'.
"""
import json
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-engine-update-roundtrip"

# The defined engine settings (must match model_import.ENGINE_DEFAULTS —
# asserted in the fixture so drift is caught).
# ``limit_mm_per_prompt`` was added by jp6-vllm-kv-cache-oom-regression task
# 3.1 (the multimodal limit becomes an authored, sized engine setting,
# design Decision 1). The drift guard itself is unchanged in strength: it
# still pins the key set exactly.
KNOWN_ENGINE_KEYS = ("dtype", "gpu_memory_utilization", "max_model_len",
                     "tensor_parallel_size", "enforce_eager",
                     "limit_mm_per_prompt")


@pytest.fixture(scope="module")
def env(aws_stack):
    """Training-jobs table + freshly imported model_import / models bound to
    it inside moto (conftest re-import pattern), with the estimation seam
    stubbed out (no network)."""
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

    assert set(model_import.ENGINE_DEFAULTS) == set(KNOWN_ENGINE_KEYS)

    # No network from the non-blocking fit check (Requirement 3.4 seam).
    mp.setattr(model_import, "estimate_weights",
               lambda record, s3_head=None, hf_fetch=None: None)

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        models=models,
        stack=aws_stack,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    mp.undo()


# ---------------------------------------------------------------------------
# Generators: complete stored configurations and valid partial updates
# ---------------------------------------------------------------------------

# Floats destined for DynamoDB are rounded so they survive the Decimal
# round trip exactly.
_gpu_memory_utilization = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)

ENGINE_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": _gpu_memory_utilization,
    "max_model_len": st.integers(min_value=1, max_value=131072),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8),
    "enforce_eager": st.booleans(),
    # An optional {"image": <int 1..8>} and an optional {"video": <int 0..8>},
    # at least one of them (task 3.1; the `video` key was added by the video
    # widening task — `video: 0` is the JP6-measured serving configuration).
    "limit_mm_per_prompt": st.one_of(
        st.integers(min_value=1, max_value=8).map(lambda n: {"image": n}),
        st.integers(min_value=0, max_value=8).map(lambda n: {"video": n}),
        st.tuples(st.integers(min_value=1, max_value=8),
                  st.integers(min_value=0, max_value=8)).map(
            lambda pair: {"image": pair[0], "video": pair[1]}),
    ),
}


@st.composite
def roundtrip_cases(draw):
    """(stored complete configuration, non-empty valid partial update,
    wrapped-body flag)."""
    stored = {key: draw(ENGINE_VALUE_STRATEGIES[key])
              for key in KNOWN_ENGINE_KEYS}
    update_keys = draw(st.lists(st.sampled_from(KNOWN_ENGINE_KEYS),
                                unique=True, min_size=1))
    supplied = {key: draw(ENGINE_VALUE_STRATEGIES[key])
                for key in update_keys}
    wrapped = draw(st.booleans())
    return stored, supplied, wrapped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_ddb(value):
    """Native -> DynamoDB-storable (floats as Decimal), like the handler."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def assert_value_equals(key, expected, actual):
    """Numeric equality across the int/float/Decimal representations the
    DynamoDB and JSON round trips produce."""
    if isinstance(expected, dict):
        # Nested settings (limit_mm_per_prompt) round trip as maps; compare
        # key-wise so the numeric representations are checked, not just ==.
        assert isinstance(actual, dict), (
            f"{key}: {actual!r} is not an object")
        assert set(actual) == set(expected), (
            f"{key}: got keys {sorted(actual)}, expected {sorted(expected)}")
        for sub_key, sub_expected in expected.items():
            assert_value_equals(f"{key}.{sub_key}", sub_expected,
                                actual[sub_key])
        return
    if isinstance(expected, bool):
        assert actual is expected, (
            f"{key}: got {actual!r}, expected {expected!r}")
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float, Decimal)), (
            f"{key}: {actual!r} is not a number")
        assert Decimal(str(actual)) == Decimal(str(expected)), (
            f"{key}: got {actual!r}, expected {expected!r}")
    else:
        assert actual == expected, (
            f"{key}: got {actual!r}, expected {expected!r}")


def seed_user_and_usecase(env):
    usecase_id = f"uc-{uuid.uuid4()}"
    env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Engine Update Roundtrip UC",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "DataScientist"}
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return usecase_id, user


def seed_vllm_record(env, usecase_id, stored_configuration):
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": usecase_id,
        "model_name": "roundtrip-llm",
        "model_version": "1.0",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/roundtrip-llm"},
        "engine_configuration": to_ddb(stored_configuration),
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    return training_id


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


def put_engine_config_event(training_id, user, supplied, wrapped):
    body = {"engine_configuration": dict(supplied)} if wrapped \
        else dict(supplied)
    return {
        "httpMethod": "PUT",
        "path": f"/api/v1/models/vllm/{training_id}/engine-configuration",
        "pathParameters": {"training_id": training_id},
        "queryStringParameters": None,
        "body": json.dumps(body),
        "requestContext": claims(user),
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


# ---------------------------------------------------------------------------
# Property 2: Engine-configuration update round trip
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(case=roundtrip_cases())
def test_update_round_trip(env, case):
    """**Feature: vllm-sizing-and-packaging-errors, Property 2:
    Engine-configuration update round trip**

    For any existing vLLM_Model_Record and any valid partial update, PUT
    returns the complete updated configuration (supplied settings take the
    new values, unsupplied settings keep their stored values), the record
    stores that same configuration, and a subsequent GET reflects it
    (Requirements 2.1, 2.4)."""
    stored, supplied, wrapped = case
    usecase_id, user = seed_user_and_usecase(env)
    training_id = seed_vllm_record(env, usecase_id, stored)

    expected = dict(stored)
    expected.update(supplied)

    # --- PUT: the update response carries the complete updated config (2.4)
    response = env.model_import.handler(
        put_engine_config_event(training_id, user, supplied, wrapped), None)
    assert response["statusCode"] == 200, (
        f"valid update must succeed, got {response['statusCode']}: "
        f"{response['body']}")
    body = json.loads(response["body"])

    returned = body["engine_configuration"]
    assert set(returned) == set(KNOWN_ENGINE_KEYS), (
        f"response must carry the complete configuration, got "
        f"{sorted(returned)}")
    for key in KNOWN_ENGINE_KEYS:
        assert_value_equals(f"response.{key}", expected[key], returned[key])

    # ...and the re-package/publish notice (2.4).
    assert body.get("notice"), "update response must carry a notice"
    assert "packaged" in body["notice"] and "published" in body["notice"]

    # --- Store: the record holds previous overlaid with supplied (2.1)
    item = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    stored_after = item["engine_configuration"]
    assert set(stored_after) == set(KNOWN_ENGINE_KEYS)
    for key in KNOWN_ENGINE_KEYS:
        assert_value_equals(f"stored.{key}", expected[key], stored_after[key])

    # --- GET: the model detail reflects the same configuration (2.1, 1.2)
    get_response = env.models.get_model(get_model_event(training_id, user),
                                        None)
    assert get_response["statusCode"] == 200, get_response["body"]
    model = json.loads(get_response["body"])["model"]
    detail = model.get("engine_configuration")
    assert isinstance(detail, dict), (
        "GET must include the stored engine configuration for vLLM records")
    assert set(detail) == set(KNOWN_ENGINE_KEYS)
    for key in KNOWN_ENGINE_KEYS:
        assert_value_equals(f"detail.{key}", expected[key], detail[key])
