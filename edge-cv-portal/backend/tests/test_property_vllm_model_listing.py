"""
Property-based test for model listing discrimination
(vllm-triton-inference task 2.5).

**Feature: vllm-triton-inference, Property 3: Model listing discrimination**

*For any* mixed set of vision and vLLM model records in a Use_Case, the
model listing includes every vLLM record, and a record carries the
`vllm` model type indicator if and only if it is a vLLM_Model_Record.

**Validates: Requirements 1.8**

Generators: mixed sequences of vision records (trained and imported
BYOM shapes, varied or absent model_type attributes, never `vllm`) and
vLLM_Model_Records exactly as `register_vllm_model` writes them
(model_type `vllm`, source `vllm`, XOR model_source, complete resolved
engine_configuration with Decimal numerics, publish_eligible).

Runs against the moto-backed conftest stack with the real
functions/models.py handler; records are seeded straight into the
training-jobs table (created here with the production
`usecase-training-index` GSI shape) and listed through `list_models`.
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

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-listing"


# ---------------------------------------------------------------------------
# Environment (module-scoped so hypothesis examples share the stack)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def models_env(aws_stack):
    """Training-jobs table (production GSI shape) + real models module."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
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

    # Re-import so the module binds the table name above and
    # moto-intercepted boto3 clients (conftest pattern).
    sys.modules.pop("models", None)
    import models

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=models,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        user_roles=aws_stack.tables.user_roles,
        usecases=aws_stack.tables.usecases,
    )


def make_list_event(usecase_id, user):
    return {
        "httpMethod": "GET",
        "path": "/api/v1/models",
        "resource": "/api/v1/models",
        "pathParameters": None,
        "queryStringParameters": {"usecase_id": usecase_id},
        "body": None,
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
# Generators
# ---------------------------------------------------------------------------

_model_names = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "Zs")),
    min_size=1, max_size=20,
)

# Vision model_type values seen on trained / imported records; absent
# means list_models applies its 'classification' default. Never 'vllm'.
_vision_model_types = st.one_of(
    st.none(),
    st.sampled_from(
        ["classification", "detection", "segmentation", "pytorch", "onnx"]),
)

_vision_sources = st.sampled_from(["trained", "imported"])

_hf_model_ids = st.tuples(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=1, max_size=12),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-.",
            min_size=1, max_size=16),
).map(lambda t: f"{t[0]}/{t[1]}")

_s3_artifacts = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=3, max_size=20,
).map(lambda k: f"s3://test-bucket/models/{k}.tar.gz")

# Valid partial engine configurations, resolved onto the documented
# defaults exactly the way register_vllm_model stores them (Decimals
# for DynamoDB numerics).
_ENGINE_DEFAULTS_DDB = {
    "dtype": "auto",
    "gpu_memory_utilization": Decimal("0.5"),
    "max_model_len": 2048,
    "tensor_parallel_size": 1,
    "enforce_eager": True,
}

_partial_engine_configs = st.fixed_dictionaries(
    {},
    optional={
        "dtype": st.sampled_from(["auto", "float16", "bfloat16", "float32"]),
        "gpu_memory_utilization": st.decimals(
            min_value=Decimal("0.1"), max_value=Decimal("1.0"), places=2),
        "max_model_len": st.integers(min_value=1, max_value=32768),
        "tensor_parallel_size": st.integers(min_value=1, max_value=8),
        "enforce_eager": st.booleans(),
    },
)


@st.composite
def _vision_records(draw):
    record = {
        "kind": "vision",
        "model_name": draw(_model_names),
        "source": draw(_vision_sources),
        "status": "Completed",
    }
    model_type = draw(_vision_model_types)
    if model_type is not None:
        record["model_type"] = model_type
    return record


@st.composite
def _vllm_records(draw):
    """A vLLM_Model_Record exactly as register_vllm_model writes it."""
    engine_configuration = dict(_ENGINE_DEFAULTS_DDB)
    engine_configuration.update(draw(_partial_engine_configs))
    if draw(st.booleans()):
        model_source = {"huggingface_model_id": draw(_hf_model_ids)}
    else:
        model_source = {"s3_model_artifact": draw(_s3_artifacts)}
    return {
        "kind": "vllm",
        "model_name": draw(_model_names),
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": model_source,
        "engine_configuration": engine_configuration,
    }


_record_sets = st.lists(
    st.one_of(_vision_records(), _vllm_records()),
    min_size=0, max_size=6,
)


# ---------------------------------------------------------------------------
# Property 3: Model listing discrimination
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_record_sets)
def test_listing_includes_every_vllm_record_with_exact_discrimination(
        models_env, records):
    """**Feature: vllm-triton-inference, Property 3: Model listing
    discrimination**

    The listing includes every vLLM record, and a listed record carries
    the `vllm` model type indicator iff it is a vLLM_Model_Record
    (Requirement 1.8).
    """
    # Fresh Use_Case and user per example: the listing queries the
    # usecase-training-index GSI, so examples are isolated without
    # table truncation.
    usecase_id = f"uc-{uuid.uuid4()}"
    models_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Property 3 Use Case",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": "DataScientist",
    }
    models_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })

    # Seed the mixed record set into the training-jobs table.
    seeded = {}  # training_id -> generated record
    for index, record in enumerate(records):
        training_id = str(uuid.uuid4())
        item = {
            "training_id": training_id,
            "usecase_id": usecase_id,
            "model_name": record["model_name"],
            "model_version": "1.0.0",
            "status": record["status"],
            "source": record["source"],
            "created_by": user["email"],
            "created_at": 1_700_000_000_000 + index,
            "updated_at": 1_700_000_000_000 + index,
        }
        if "model_type" in record:
            item["model_type"] = record["model_type"]
        if record["kind"] == "vllm":
            item["publish_eligible"] = record["publish_eligible"]
            item["model_source"] = record["model_source"]
            item["engine_configuration"] = record["engine_configuration"]
        models_env.training_jobs.put_item(Item=item)
        seeded[training_id] = record

    response = models_env.module.list_models(
        make_list_event(usecase_id, user), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    listed = {model["model_id"]: model for model in body["models"]}

    # Every listed model corresponds to a seeded record of this
    # Use_Case (no phantom entries).
    assert set(listed).issubset(set(seeded))

    # The listing includes every vLLM record.
    vllm_ids = {tid for tid, rec in seeded.items() if rec["kind"] == "vllm"}
    missing = vllm_ids - set(listed)
    assert not missing, f"vLLM records missing from the listing: {missing}"

    # A record carries the `vllm` model type indicator iff it is a
    # vLLM_Model_Record.
    for training_id, model in listed.items():
        record = seeded[training_id]
        if record["kind"] == "vllm":
            assert model["model_type"] == "vllm", (
                f"vLLM record {training_id} listed without the vllm "
                f"model type indicator: {model['model_type']!r}")
            assert model["source"] == "vllm"
        else:
            assert model["model_type"] != "vllm", (
                f"vision record {training_id} listed with the vllm "
                f"model type indicator")
            assert model["source"] != "vllm"
