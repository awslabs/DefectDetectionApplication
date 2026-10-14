"""Property test for invalid vLLM engine-configuration updates
(vllm-sizing-and-packaging-errors, task 2.5).

**Feature: vllm-sizing-and-packaging-errors, Property 3: Invalid updates
change nothing**

_For any_ engine-configuration update containing at least one unknown key or
out-of-range value, the response is HTTP 400 with a finding naming every
offending field (with its value and reason), and the stored
Engine_Configuration is byte-identical to its pre-request value.

**Validates: Requirements 2.2**

Runs against the moto-backed conftest stack with the real
functions/model_import.py PUT handler (through its router). The
weight-estimation seam is stubbed out so no network access happens (a
rejected update never reaches the fit check anyway, but the stub keeps the
suite hermetic).
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

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-engine-update-invalid"

# ``limit_mm_per_prompt`` was added by jp6-vllm-kv-cache-oom-regression task
# 3.1 (design Decision 1). The drift guard below keeps its exact-key-set
# strength.
KNOWN_ENGINE_KEYS = ("dtype", "gpu_memory_utilization", "max_model_len",
                     "tensor_parallel_size", "enforce_eager",
                     "limit_mm_per_prompt")

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@pytest.fixture(scope="module")
def env(aws_stack):
    """Training-jobs table + freshly imported model_import bound to it
    inside moto, with the estimation seam stubbed out (no network)."""
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

    assert set(model_import.ENGINE_DEFAULTS) == set(KNOWN_ENGINE_KEYS)
    mp.setattr(model_import, "estimate_weights",
               lambda record, s3_head=None, hf_fetch=None: None)

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        stack=aws_stack,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    mp.undo()


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_gpu_memory_utilization = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)

VALID_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": _gpu_memory_utilization,
    "max_model_len": st.integers(min_value=1, max_value=131072),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8),
    "enforce_eager": st.booleans(),
    # An optional "image" (1..8) and an optional "video" (0..8); at least
    # one of them. `video: 0` is the JP6-measured configuration (see
    # model_import's LIMIT_MM_RANGES comment).
    "limit_mm_per_prompt": st.one_of(
        st.integers(min_value=1, max_value=8).map(lambda n: {"image": n}),
        st.integers(min_value=0, max_value=8).map(lambda n: {"video": n}),
        st.tuples(st.integers(min_value=1, max_value=8),
                  st.integers(min_value=0, max_value=8)).map(
            lambda pair: {"image": pair[0], "video": pair[1]}),
    ),
}

# Out-of-range / wrong-type values per known setting (all JSON-serializable
# so the API Gateway body round trip preserves them).
INVALID_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(
        ("float64", "int8", "fp16", "AUTO", "", "bf16", 3, 1.5, True, None)),
    "gpu_memory_utilization": st.sampled_from(
        (0, 0.0, -0.25, 1.000001, 2, 100, True, False, "0.5", None)),
    "max_model_len": st.sampled_from(
        (0, -1, -100, 3.5, True, False, "2048", None)),
    "tensor_parallel_size": st.sampled_from(
        (0, -2, 1.5, True, False, "1", None)),
    "enforce_eager": st.sampled_from(
        (0, 1, 2.5, "true", "false", "True", None)),
    # non-dict, no key at all, unknown sub-key (still fail-closed), non-int
    # and out-of-range counts on BOTH accepted sub-keys.
    # NOTE (video widening): `{"video": 1}` and `{"image": 1, "video": 1}`
    # moved from this list to VALID_VALUE_STRATEGIES — bounding video is now
    # authorable. `{"image": 2, "audio": 1}` stays here: an unknown sub-key
    # is still rejected fail-closed.
    "limit_mm_per_prompt": st.sampled_from(
        (2, "2", 1.0, True, False, None, [2], [],
         {}, {"image": 0}, {"image": 9}, {"image": -1}, {"image": True},
         {"image": False}, {"image": 1.5}, {"image": "2"}, {"image": None},
         {"video": -1}, {"video": 9}, {"video": True}, {"video": 1.5},
         {"video": "0"}, {"video": None}, {"image": 1, "video": 9},
         {"image": 0, "video": 0}, {"image": 2, "audio": 1},
         {"audio": 1}, {"image": 1, "video": 0, "audio": 1})),
}

# Unknown setting keys — never a defined key, and never the literal
# "engine_configuration" (a top-level key of that name would be read as the
# wrapped settings object by the handler's body-shape detection).
unknown_keys = st.text(alphabet=_ALNUM + "_", min_size=1, max_size=15).filter(
    lambda k: k not in KNOWN_ENGINE_KEYS and k != "engine_configuration")

unknown_key_values = st.sampled_from((1, 0.5, "x", True, None))


@st.composite
def invalid_update_cases(draw):
    """(update mapping, set of offending keys, wrapped-body flag) with at
    least one offending entry (unknown key or out-of-range value), possibly
    mixed with valid entries."""
    invalid_known = draw(st.lists(st.sampled_from(KNOWN_ENGINE_KEYS),
                                  unique=True))
    n_unknown = draw(st.integers(min_value=0, max_value=2))
    unknowns = draw(st.lists(unknown_keys, unique=True,
                             min_size=n_unknown, max_size=n_unknown))
    if not invalid_known and not unknowns:
        invalid_known = [draw(st.sampled_from(KNOWN_ENGINE_KEYS))]

    valid_pool = [k for k in KNOWN_ENGINE_KEYS if k not in invalid_known]
    valid_keys = draw(st.lists(st.sampled_from(valid_pool), unique=True)) \
        if valid_pool else []

    update = {}
    for key in valid_keys:
        update[key] = draw(VALID_VALUE_STRATEGIES[key])
    for key in invalid_known:
        update[key] = draw(INVALID_VALUE_STRATEGIES[key])
    for key in unknowns:
        update[key] = draw(unknown_key_values)

    offending = set(invalid_known) | set(unknowns)
    wrapped = draw(st.booleans())
    return update, offending, wrapped


full_stored_configurations = st.fixed_dictionaries(
    {key: VALID_VALUE_STRATEGIES[key] for key in KNOWN_ENGINE_KEYS})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_ddb(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def seed_record(env, stored_configuration):
    usecase_id = f"uc-{uuid.uuid4()}"
    env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Invalid Update UC",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "DataScientist"}
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": usecase_id,
        "model_name": "invalid-update-llm",
        "model_version": "1.0",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/invalid-update"},
        "engine_configuration": to_ddb(stored_configuration),
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    return training_id, user


def put_engine_config_event(training_id, user, update, wrapped):
    body = {"engine_configuration": dict(update)} if wrapped else dict(update)
    return {
        "httpMethod": "PUT",
        "path": f"/api/v1/models/vllm/{training_id}/engine-configuration",
        "pathParameters": {"training_id": training_id},
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
# Property 3: Invalid updates change nothing
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(stored=full_stored_configurations, case=invalid_update_cases())
def test_invalid_updates_change_nothing(env, stored, case):
    """**Feature: vllm-sizing-and-packaging-errors, Property 3: Invalid
    updates change nothing**

    For any update containing an unknown key or an out-of-range value, the
    response is HTTP 400 with a per-field finding naming every offending
    field (field, value, reason), and the stored Engine_Configuration is
    byte-identical afterwards (Requirement 2.2)."""
    update, offending, wrapped = case
    training_id, user = seed_record(env, stored)

    before = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]

    response = env.model_import.handler(
        put_engine_config_event(training_id, user, update, wrapped), None)

    # --- HTTP 400 with per-field findings
    assert response["statusCode"] == 400, (
        f"invalid update must be rejected with 400, got "
        f"{response['statusCode']}: {response['body']}")
    body = json.loads(response["body"])
    findings = body.get("findings")
    assert isinstance(findings, list) and findings, (
        f"400 response must carry a findings list, got {body!r}")

    # Every offending field is named — and only offending fields (valid
    # entries mixed into the same request produce no finding).
    finding_fields = {finding["field"] for finding in findings}
    expected_fields = {f"engine_configuration.{key}" for key in offending}
    assert finding_fields == expected_fields, (
        f"findings must name exactly the offending fields "
        f"{sorted(expected_fields)}, got {sorted(finding_fields)}")

    # Each finding carries the offending value and a non-empty reason.
    for finding in findings:
        key = finding["field"].split(".", 1)[1]
        assert "value" in finding
        assert finding["value"] == update[key]
        assert finding.get("reason"), (
            f"finding for {finding['field']} must carry a reason")

    # --- Store byte-identical: configuration and updated_at unchanged
    after = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    assert after["engine_configuration"] == before["engine_configuration"], (
        "stored Engine_Configuration must be unchanged after a rejected "
        "update")
    assert after["updated_at"] == before["updated_at"], (
        "a rejected update must not touch the record")
    assert after == before
