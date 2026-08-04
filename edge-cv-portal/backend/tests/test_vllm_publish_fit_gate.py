"""
Unit tests for the vLLM publish-time Fit_Check gate in
functions/greengrass_publish.py (vllm-sizing-and-packaging-errors
task 3.2).

Covers publish_component's vLLM preflight gate:

- when the fit check fails for EVERY supported Target_Architecture and
  the request carries no override, the publish fails with HTTP 422,
  `fit_check.status == 'failed'` with per-architecture findings,
  `create_component_version` is never called, and the record stays in
  its pre-publish state (Requirement 3.6)
- with an explicit `skip_fit_check: true` override the publish
  proceeds, the response carries `fit_check.status == 'overridden'`,
  and the audit event details record `skip_fit_check: true`
  (Requirement 3.7)
- when the Weight_Estimate cannot be determined (estimate is None) the
  publish proceeds with `fit_check.status == 'unverified'`
  (Requirement 3.4)

Runs against the moto-backed conftest stack with the real
functions/greengrass_publish.py handler; greengrassv2 (which moto does
not implement) is a fake client recording created recipes. The weight
estimation seam (`estimate_weights`, imported as a module attribute)
is monkeypatched so no network or S3 access happens.

_Requirements: 3.6, 3.7, 3.4_
"""
import importlib.util
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Attr

from conftest import REGION
from vllm_fit_check import GIB, WeightEstimate

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-fit-gate"
MODELS_TABLE_NAME = "test-models-vllm-fit-gate"

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")


def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    (inside the moto mock, so its module-level boto3 resource and table
    names bind to the test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish_fit_gate", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish_fit_gate"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake Greengrass client (moto has no greengrassv2)
# ---------------------------------------------------------------------------

class FakeGreengrass:
    def __init__(self):
        self.created = []          # parsed recipes, in creation order
        self.deleted = []          # ARNs passed to delete_component

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        self.created.append(recipe)
        arn = (f"arn:aws:greengrass:{REGION}:123456789012:components:"
               f"{recipe['ComponentName']}:versions:"
               f"{recipe['ComponentVersion']}")
        return {"arn": arn}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables + real greengrass_publish module."""
    import boto3

    mp = pytest.MonkeyPatch()
    mp.setenv("TRAINING_JOBS_TABLE", TRAINING_JOBS_TABLE_NAME)
    mp.setenv("MODELS_TABLE", MODELS_TABLE_NAME)

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=MODELS_TABLE_NAME,
        KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "model_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    module = _load_publish_module()

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=module,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        models=resource.Table(MODELS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
        audit_log=aws_stack.tables.audit_log,
    )
    mp.undo()


@pytest.fixture
def seeded(pub_env, monkeypatch):
    """Fresh Use_Case + DataScientist; no 2s polling sleeps; fake GG."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    gg = FakeGreengrass()
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    usecase_id = f"uc-{uuid.uuid4()}"
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "vLLM Fit Gate Use Case",
        "account_id": "123456789012",
        "s3_bucket": "test-vllm-usecase-bucket",
    })
    user_id = f"user-{uuid.uuid4()}"
    pub_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id, gg=gg)


def seed_vllm_record(pub_env, seeded, **overrides):
    """A packaged vLLM_Model_Record as packaging.py leaves it."""
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "Fit Gate LLM",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "example/some-llm"},
        "engine_configuration": {
            "dtype": "auto",
            "gpu_memory_utilization": "0.3",
            "max_model_len": 4096,
            "tensor_parallel_size": 1,
            "enforce_eager": True,
        },
        "packaged_components": [{
            "target": "jetson-xavier-jp6",
            "status": "packaged",
            "component_package_s3": (
                "s3://test-vllm-usecase-bucket/model_artifacts/model-abc/"
                "abc_greengrass_model_component.zip"),
            "supported_architectures": ["arm64_jp6"],
        }],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    item.update(overrides)
    pub_env.training_jobs.put_item(Item=item)
    return item


def publish_event(training_id, user_id, extra_body=None):
    body = {
        # Required by the shared request shape; the vLLM branch derives
        # the actual convention name/version itself.
        "component_name": "model-caller-chosen",
        "component_version": "9.0.0",
    }
    body.update(extra_body or {})
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user_id,
                    "email": f"{user_id}@example.com",
                    "cognito:username": user_id,
                }
            }
        },
    }


def stored_record(pub_env, training_id):
    return pub_env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]


def audit_events(pub_env, user_id):
    """All audit events logged for the given (per-test unique) user."""
    return pub_env.audit_log.scan(
        FilterExpression=Attr("user_id").eq(user_id)).get("Items", [])


# 100 GiB of weights: fails every profile entry regardless of the
# configured gpu_memory_utilization fraction (profiles are 30 GiB).
HUGE_ESTIMATE = WeightEstimate(
    total_bytes=100 * GIB,
    method="safetensors_files",
    detail="synthetic 100 GiB estimate (test)",
)


def patch_estimate(pub_env, monkeypatch, estimate):
    """Replace the estimation seam so no network/S3 access happens."""
    calls = []

    def fake_estimate_weights(record, s3_head=None, hf_fetch=None):
        calls.append(record)
        return estimate

    monkeypatch.setattr(pub_env.module, "estimate_weights",
                        fake_estimate_weights)
    return calls


# ---------------------------------------------------------------------------
# All-architecture failure -> 422, no registration, record unchanged (3.6)
# ---------------------------------------------------------------------------

def test_all_arch_failure_blocks_publish_with_422(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_estimate(pub_env, monkeypatch, HUGE_ESTIMATE)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 422, response["body"]
    body = json.loads(response["body"])

    # fit_check.status == 'failed' with per-architecture failing findings.
    assert body["fit_check"]["status"] == "failed"
    findings = body["fit_check"]["findings"]
    assert findings, "expected at least one per-architecture finding"
    assert all(finding["fits"] is False for finding in findings)
    assert body["fit_check"]["estimate"]["total_bytes"] == 100 * GIB

    # The error message carries the sizing statement and the correct
    # remediation direction (raise, never lower).
    assert "raise gpu_memory_utilization" in body["error"]
    assert "lower" not in body["error"].lower()

    # No component registration was attempted.
    assert seeded.gg.created == []

    # Record untouched: no publish state, updated_at unchanged.
    stored = stored_record(pub_env, record["training_id"])
    assert "published_component" not in stored
    assert "component_name" not in stored
    assert "published" not in stored
    assert "published_components" not in stored
    assert stored["updated_at"] == record["updated_at"]

    # Nothing was made available for deployments.
    listing = pub_env.models.scan().get("Items", [])
    assert all(item.get("training_job_id") != record["training_id"]
               for item in listing)


# ---------------------------------------------------------------------------
# skip_fit_check override -> publish proceeds and audits the override (3.7)
# ---------------------------------------------------------------------------

def test_skip_fit_check_override_proceeds_and_audits(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_estimate(pub_env, monkeypatch, HUGE_ESTIMATE)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id,
                      extra_body={"skip_fit_check": True}), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # Response annotates the override with the failing findings retained.
    assert body["fit_check"]["status"] == "overridden"
    assert body["fit_check"]["findings"]
    assert all(finding["fits"] is False
               for finding in body["fit_check"]["findings"])

    # The publish actually proceeded through component registration.
    assert len(seeded.gg.created) == 1
    assert seeded.gg.created[0]["ComponentName"] == "model-vllm-fit-gate-llm"

    # Record marked published.
    stored = stored_record(pub_env, record["training_id"])
    assert stored["published"] is True
    assert stored["published_component"]["component_name"] == \
        "model-vllm-fit-gate-llm"

    # The audit event details carry the skip_fit_check override.
    events = audit_events(pub_env, seeded.user_id)
    publish_events = [e for e in events
                      if e["action"] == "publish_greengrass_component"]
    assert len(publish_events) == 1
    event = publish_events[0]
    assert event["result"] == "success"
    assert event["details"]["skip_fit_check"] is True


# ---------------------------------------------------------------------------
# Unverifiable estimate -> publish proceeds as 'unverified' (3.4)
# ---------------------------------------------------------------------------

def test_unverified_estimate_proceeds(pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    patch_estimate(pub_env, monkeypatch, None)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)

    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # Annotated as unverified: no estimate, no findings, not blocked.
    assert body["fit_check"]["status"] == "unverified"
    assert body["fit_check"]["estimate"] is None
    assert body["fit_check"]["findings"] == []

    # Publish went through normally.
    assert len(seeded.gg.created) == 1
    stored = stored_record(pub_env, record["training_id"])
    assert stored["published"] is True

    # No override was recorded on the audit event.
    events = audit_events(pub_env, seeded.user_id)
    publish_events = [e for e in events
                      if e["action"] == "publish_greengrass_component"]
    assert len(publish_events) == 1
    assert "skip_fit_check" not in publish_events[0]["details"]
