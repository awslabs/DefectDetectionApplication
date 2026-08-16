"""Property test for fail-open on unverifiable weight estimates
(vllm-sizing-and-packaging-errors, task 2.6).

**Feature: vllm-sizing-and-packaging-errors, Property 5: Unverifiable
estimates never block**

_For any_ vLLM_Model_Record whose weight estimation returns no estimate
(`estimate_weights` yields None — fetch failures etc.), registration,
engine-configuration update, and publish all proceed (no fit-related
rejection), and the response marks the fit check as 'unverified'.

**Validates: Requirements 3.4**

Runs against the moto-backed conftest stack with the real
functions/model_import.py handlers and the real
functions/greengrass_publish.py publish handler (loaded under a distinct
module name, fit-gate test pattern). The estimation seam
(`estimate_weights`, a module attribute of each consumer) is replaced with
a stub returning None, so no network or S3 access happens; greengrassv2
(which moto does not implement) is a fake client recording created recipes.
"""
import importlib.util
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

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-fit-unverified"
MODELS_TABLE_NAME = "test-models-fit-unverified"

KNOWN_ENGINE_KEYS = ("dtype", "gpu_memory_utilization", "max_model_len",
                     "tensor_parallel_size", "enforce_eager")

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")


def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    (inside the moto mock, so its module-level boto3 resource and table
    names bind to the test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_unverified", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_unverified"] = module
    spec.loader.exec_module(module)
    return module


class _FakePaginator:
    """Serves list_components / list_component_versions from the fake's own
    registered state — the surface the vLLM version derivation now uses
    (existing_component_versions)."""

    def __init__(self, fake, operation):
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs):
        if self.operation == "list_components":
            yield {"components": [
                {"componentName": name,
                 "arn": (f"arn:aws:greengrass:{REGION}:123456789012:"
                         f"components:{name}")}
                for name in sorted(self.fake.registered)
            ]}
        elif self.operation == "list_component_versions":
            name = str(kwargs["arn"]).split(":components:")[1].split(":")[0]
            yield {"componentVersions": [
                {"componentVersion": version}
                for version in sorted(self.fake.registered.get(name, ()))
            ]}
        else:  # pragma: no cover - unexpected paginator in the publish path
            raise AssertionError(f"unexpected paginator: {self.operation}")


class FakeGreengrass:
    """Fake greengrassv2 client (moto has no greengrassv2)."""

    def __init__(self):
        self.created = []
        self.deleted = []
        # component name -> registered version strings, so the cloud-side
        # version derivation observes what this fake has accepted.
        self.registered = {}

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        self.created.append(recipe)
        self.registered.setdefault(recipe["ComponentName"], set()).add(
            recipe["ComponentVersion"])
        arn = (f"arn:aws:greengrass:{REGION}:123456789012:components:"
               f"{recipe['ComponentName']}:versions:"
               f"{recipe['ComponentVersion']}")
        return {"arn": arn}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)


@pytest.fixture(scope="module")
def env(aws_stack):
    """Training-jobs + models tables, fresh model_import and
    greengrass_publish modules, estimation seam stubbed to None."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "training_id",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "model_id",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    sys.modules.pop("model_import", None)
    import model_import
    publish = _load_publish_module()

    def no_estimate(record, s3_head=None, hf_fetch=None):
        return None

    # The estimation seam yields None everywhere: fetch failures etc. (3.4).
    mp.setattr(model_import, "estimate_weights", no_estimate)
    mp.setattr(publish, "estimate_weights", no_estimate)

    # No 2s polling sleeps; fake Greengrass for the publish path.
    gg = FakeGreengrass()
    mp.setattr(publish.time, "sleep", lambda s: None)
    mp.setattr(publish, "get_usecase_client",
               lambda service, usecase, **kw: gg)

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        model_import=model_import,
        publish=publish,
        gg=gg,
        stack=aws_stack,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    mp.undo()


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_HF_TAIL = _ALNUM + "._-"

_gpu_memory_utilization = st.floats(
    min_value=1e-6, max_value=1.0, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6)).filter(lambda x: 0.0 < x <= 1.0)

ENGINE_VALUE_STRATEGIES = {
    "dtype": st.sampled_from(("auto", "float16", "bfloat16", "float32")),
    "gpu_memory_utilization": _gpu_memory_utilization,
    "max_model_len": st.integers(min_value=1, max_value=131072),
    "tensor_parallel_size": st.integers(min_value=1, max_value=8),
    "enforce_eager": st.booleans(),
}


@st.composite
def partial_engine_configurations(draw):
    keys = draw(st.lists(st.sampled_from(KNOWN_ENGINE_KEYS), unique=True))
    return {key: draw(ENGINE_VALUE_STRATEGIES[key]) for key in keys}


@st.composite
def nonempty_partial_engine_configurations(draw):
    keys = draw(st.lists(st.sampled_from(KNOWN_ENGINE_KEYS), unique=True,
                         min_size=1))
    return {key: draw(ENGINE_VALUE_STRATEGIES[key]) for key in keys}


full_engine_configurations = st.fixed_dictionaries(
    {key: ENGINE_VALUE_STRATEGIES[key] for key in KNOWN_ENGINE_KEYS})

huggingface_model_ids = st.builds(
    lambda head, tail, name: f"{head}{tail}/{name}",
    st.text(alphabet=_ALNUM, min_size=1, max_size=1),
    st.text(alphabet=_HF_TAIL, min_size=0, max_size=20),
    st.text(alphabet=_HF_TAIL, min_size=1, max_size=24),
)


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


def seed_user_and_usecase(env):
    usecase_id = f"uc-{uuid.uuid4()}"
    env.stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Fit Unverified UC",
        "account_id": "123456789012",
        "s3_bucket": "test-fit-unverified-usecase-bucket",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {"user_id": user_id, "email": f"{user_id}@example.com",
            "username": user_id, "role": "DataScientist"}
    env.stack.tables.user_roles.put_item(Item={
        "user_id": user_id, "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return usecase_id, user


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


def assert_unverified(fit_check):
    """The fit check reports 'unverified' with no estimate and no findings."""
    assert fit_check["status"] == "unverified", (
        f"fit check must be 'unverified' when no estimate is available, "
        f"got {fit_check!r}")
    assert fit_check["estimate"] is None
    assert fit_check["findings"] == []


# ---------------------------------------------------------------------------
# Property 5, registration path
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(hf_model_id=huggingface_model_ids,
       supplied=partial_engine_configurations())
def test_registration_unverified_never_blocks(env, hf_model_id, supplied):
    """**Feature: vllm-sizing-and-packaging-errors, Property 5: Unverifiable
    estimates never block**

    For any valid registration whose weight estimation yields None, the
    registration succeeds (201) and its response carries a fit check marked
    'unverified' (Requirement 3.4)."""
    usecase_id, user = seed_user_and_usecase(env)

    body = {
        "usecase_id": usecase_id,
        "model_name": "unverified-reg-llm",
        "model_version": "1.0",
        "huggingface_model_id": hf_model_id,
    }
    if supplied:
        body["engine_configuration"] = dict(supplied)

    response = env.model_import.handler({
        "httpMethod": "POST",
        "resource": "/api/v1/models/vllm",
        "path": "/api/v1/models/vllm",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
        "requestContext": claims(user),
    }, None)

    assert response["statusCode"] == 201, (
        f"registration must never be blocked by an unverifiable fit check, "
        f"got {response['statusCode']}: {response['body']}")
    payload = json.loads(response["body"])
    assert payload["publish_eligible"] is True
    assert_unverified(payload["fit_check"])


# ---------------------------------------------------------------------------
# Property 5, update path
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(stored=full_engine_configurations,
       supplied=nonempty_partial_engine_configurations())
def test_update_unverified_never_blocks(env, stored, supplied):
    """**Feature: vllm-sizing-and-packaging-errors, Property 5: Unverifiable
    estimates never block**

    For any valid engine-configuration update whose weight estimation
    yields None, the update succeeds (200), the configuration is stored,
    and the response carries a fit check marked 'unverified'
    (Requirement 3.4)."""
    usecase_id, user = seed_user_and_usecase(env)
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": usecase_id,
        "model_name": "unverified-update-llm",
        "model_version": "1.0",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/unverified"},
        "engine_configuration": to_ddb(stored),
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })

    response = env.model_import.handler({
        "httpMethod": "PUT",
        "path": f"/api/v1/models/vllm/{training_id}/engine-configuration",
        "pathParameters": {"training_id": training_id},
        "queryStringParameters": None,
        "body": json.dumps({"engine_configuration": dict(supplied)}),
        "requestContext": claims(user),
    }, None)

    assert response["statusCode"] == 200, (
        f"an update must never be blocked by an unverifiable fit check, "
        f"got {response['statusCode']}: {response['body']}")
    payload = json.loads(response["body"])
    assert_unverified(payload["fit_check"])

    # The update itself took effect (it was not silently dropped).
    expected = dict(stored)
    expected.update(supplied)
    returned = payload["engine_configuration"]
    for key in KNOWN_ENGINE_KEYS:
        if isinstance(expected[key], bool):
            assert returned[key] is expected[key]
        elif isinstance(expected[key], (int, float)):
            assert Decimal(str(returned[key])) == Decimal(str(expected[key]))
        else:
            assert returned[key] == expected[key]


# ---------------------------------------------------------------------------
# Property 5, publish path
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(stored=full_engine_configurations)
def test_publish_unverified_never_blocks(env, stored):
    """**Feature: vllm-sizing-and-packaging-errors, Property 5: Unverifiable
    estimates never block**

    For any packaged vLLM_Model_Record whose weight estimation yields None,
    the publish proceeds through component registration (200) and the
    response carries a fit check marked 'unverified' (Requirement 3.4)."""
    usecase_id, user = seed_user_and_usecase(env)
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": usecase_id,
        "model_name": "Unverified Publish LLM",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/unverified"},
        "engine_configuration": to_ddb(stored),
        "packaged_components": [{
            "target": "jetson-xavier-jp6",
            "status": "packaged",
            "component_package_s3": (
                "s3://test-fit-unverified-usecase-bucket/model_artifacts/"
                "model-abc/abc_greengrass_model_component.zip"),
            "supported_architectures": ["arm64_jp6"],
        }],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })

    created_before = len(env.gg.created)
    response = env.publish.publish_component({
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps({
            "component_name": "model-caller-chosen",
            "component_version": "9.0.0",
        }),
        "requestContext": claims(user),
    }, None)

    assert response["statusCode"] == 200, (
        f"a publish must never be blocked by an unverifiable fit check, "
        f"got {response['statusCode']}: {response['body']}")
    payload = json.loads(response["body"])
    assert_unverified(payload["fit_check"])

    # Component registration actually happened — the operation proceeded.
    assert len(env.gg.created) == created_before + 1
    stored_after = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    assert stored_after["published"] is True
