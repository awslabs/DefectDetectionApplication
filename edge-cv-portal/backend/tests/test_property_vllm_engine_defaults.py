"""Property test for vLLM engine configuration defaults overlay
(vllm-triton-inference, task 2.4).

**Feature: vllm-triton-inference, Property 2: Engine configuration defaults
overlay**

_For any_ valid partial vLLM_Engine_Configuration, the resolved configuration
SHALL contain every defined engine setting, with each supplied setting keeping
its supplied value and each omitted setting equal to its documented default;
and the vLLM_Model_Record built from a valid registration request SHALL store
model type `vllm`, the given source reference, and the complete resolved
engine configuration.

**Validates: Requirements 1.2, 1.3**

The record half runs against the shared moto stack from conftest.py: the
registration handler performs a real (moto) DynamoDB put and, for S3-sourced
registrations, a real (moto) S3 head_object readability probe.
"""
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-engine-defaults"
ARTIFACTS_BUCKET = "test-vllm-engine-defaults-artifacts"


@pytest.fixture(scope="module")
def vllm_env(aws_stack):
    """Training-jobs table + artifact bucket + freshly imported model_import
    bound to them inside moto (conftest re-import pattern)."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=ARTIFACTS_BUCKET)

    # Re-import so the module binds TRAINING_JOBS_TABLE and moto-intercepted
    # boto3 clients.
    sys.modules.pop("model_import", None)
    import model_import

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        stack=aws_stack,
        s3=s3,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )


# ---------------------------------------------------------------------------
# Generators: valid partial engine configurations and valid sources
# ---------------------------------------------------------------------------

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_HF_TAIL = _ALNUM + "._-"

# Floats destined for DynamoDB are rounded so they stay inside DynamoDB's
# representable number range (raw hypothesis floats go below ~1e-130).
_gpu_memory_utilization = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)

# One in-range strategy per defined engine setting.
ENGINE_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": _gpu_memory_utilization,
    "max_model_len": st.integers(min_value=1, max_value=131072),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8),
    "enforce_eager": st.booleans(),
}


@st.composite
def partial_engine_configurations(draw):
    """Any subset (including empty and complete) of the defined engine
    settings, each with an in-range value."""
    keys = draw(st.lists(st.sampled_from(sorted(ENGINE_VALUE_STRATEGIES)),
                         unique=True))
    return {key: draw(ENGINE_VALUE_STRATEGIES[key]) for key in keys}


huggingface_model_ids = st.builds(
    lambda head, tail, name: f"{head}{tail}/{name}",
    st.text(alphabet=_ALNUM, min_size=1, max_size=1),
    st.text(alphabet=_HF_TAIL, min_size=0, max_size=20),
    st.text(alphabet=_HF_TAIL, min_size=1, max_size=24),
)

s3_artifact_keys = st.text(
    alphabet=_ALNUM + "-_/", min_size=1, max_size=30,
).filter(lambda k: not k.startswith("/") and "//" not in k)


@st.composite
def valid_registration_cases(draw):
    """(source_kind, source_ref, partial engine configuration) for a valid
    registration request — exactly one source, in-range engine values."""
    engine_configuration = draw(partial_engine_configurations())
    if draw(st.booleans()):
        return "huggingface_model_id", draw(huggingface_model_ids), \
            engine_configuration
    key = f"{draw(s3_artifact_keys)}.tar.gz"
    return "s3_model_artifact", f"s3://{ARTIFACTS_BUCKET}/{key}", \
        engine_configuration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_stored_value_equals(key, expected, actual):
    """A DynamoDB round trip returns numbers as Decimal; booleans and
    strings come back unchanged."""
    if isinstance(expected, bool):
        assert actual is expected, (
            f"{key}: stored {actual!r} must equal supplied/default "
            f"{expected!r}")
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float, Decimal)), (
            f"{key}: stored {actual!r} is not a number")
        assert Decimal(str(actual)) == Decimal(str(expected)), (
            f"{key}: stored {actual!r} must equal supplied/default "
            f"{expected!r}")
    else:
        assert actual == expected, (
            f"{key}: stored {actual!r} must equal supplied/default "
            f"{expected!r}")


def registration_event(user, body):
    return {
        "httpMethod": "POST",
        "resource": "/api/v1/models/vllm",
        "path": "/api/v1/models/vllm",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
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


# ---------------------------------------------------------------------------
# Property 2: Engine configuration defaults overlay
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(supplied=partial_engine_configurations())
def test_resolved_configuration_overlays_defaults(vllm_env, supplied):
    """**Feature: vllm-triton-inference, Property 2: Engine configuration
    defaults overlay**

    For any valid partial engine configuration, the resolved configuration
    contains every defined setting, supplied settings keep their values, and
    omitted settings equal their documented defaults (Requirement 1.2)."""
    model_import = vllm_env.model_import
    supplied_snapshot = dict(supplied)

    resolved = model_import.resolve_engine_configuration(supplied)

    defaults = model_import.ENGINE_DEFAULTS
    assert set(resolved) == set(defaults), (
        f"resolved configuration must contain exactly the defined settings "
        f"{sorted(defaults)}, got {sorted(resolved)}")
    for key in defaults:
        if key in supplied_snapshot:
            assert resolved[key] == supplied_snapshot[key], (
                f"{key}: supplied value {supplied_snapshot[key]!r} must be "
                f"kept, got {resolved[key]!r}")
        else:
            assert resolved[key] == defaults[key], (
                f"{key}: omitted setting must equal its documented default "
                f"{defaults[key]!r}, got {resolved[key]!r}")
    # The overlay is pure: the supplied mapping is not mutated.
    assert supplied == supplied_snapshot


@settings(deadline=None)
@given(case=valid_registration_cases())
def test_record_stores_type_source_and_complete_configuration(vllm_env, case):
    """**Feature: vllm-triton-inference, Property 2: Engine configuration
    defaults overlay**

    The record built from a valid registration request stores model type
    `vllm`, the given source reference, and the complete engine configuration
    — supplied values kept, omitted settings at their documented defaults
    (Requirements 1.2, 1.3)."""
    source_field, source_ref, supplied = case
    model_import = vllm_env.model_import
    stack = vllm_env.stack

    # Fresh use case + DataScientist user per example (single-account use
    # case, so the S3 readability probe uses the moto-intercepted client).
    usecase_id = f"uc-{uuid.uuid4()}"
    stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "vLLM Engine Defaults UC",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "DataScientist"}
    stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "DataScientist",
    })

    # S3-sourced registrations must reference a readable artifact.
    if source_field == "s3_model_artifact":
        key = source_ref[len(f"s3://{ARTIFACTS_BUCKET}/"):]
        vllm_env.s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=key,
                               Body=b"weights")

    body = {
        "usecase_id": usecase_id,
        "model_name": "engine-defaults-model",
        "model_version": "1.0",
        source_field: source_ref,
    }
    if supplied:
        body["engine_configuration"] = dict(supplied)

    response = model_import.handler(registration_event(user, body), None)
    assert response["statusCode"] == 201, (
        f"valid registration must succeed, got {response['statusCode']}: "
        f"{response['body']}")
    training_id = json.loads(response["body"])["training_id"]

    item = vllm_env.training_jobs.get_item(
        Key={"training_id": training_id}).get("Item")
    assert item is not None, "registration must write the vLLM_Model_Record"

    # Model type `vllm`, scoped to the owning Use_Case (Req 1.3).
    assert item["model_type"] == "vllm"
    assert item["usecase_id"] == usecase_id

    # The given source reference — and only it (Req 1.3).
    assert item["model_source"] == {source_field: source_ref}, (
        f"record must store the given source reference, got "
        f"{item['model_source']!r}")

    # The complete engine configuration: every defined setting present,
    # supplied values kept, omitted settings at their defaults (1.2, 1.3).
    stored = item["engine_configuration"]
    defaults = model_import.ENGINE_DEFAULTS
    assert set(stored) == set(defaults), (
        f"stored configuration must contain exactly the defined settings "
        f"{sorted(defaults)}, got {sorted(stored)}")
    for key in defaults:
        expected = supplied[key] if key in supplied else defaults[key]
        assert_stored_value_equals(key, expected, stored[key])
