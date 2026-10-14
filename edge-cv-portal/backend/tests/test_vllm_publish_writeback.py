"""
Unit tests for the vLLM publish metadata write-back and state
transitions in functions/greengrass_publish.py (vllm-triton-inference
task 4.2).

Covers publish_component's vLLM branch:

- on success `supported_architectures` (mirroring
  vllm_supported_architectures()) and `runtime: 'vllm'` are written
  onto the record's `published_component` map and into the recipe's
  ComponentConfiguration.DefaultConfiguration; a top-level
  `component_name` attribute is materialized for the (task 5.2)
  component_name-index GSI; the record is marked published with the
  component name/version and a models-table item makes the component
  version available for deployments (2.4, 2.9)
- on any Greengrass failure no partial state is written: the record
  stays pre-publish (retryable), no models-table item is created, and
  component versions created during the attempt are rolled back (2.6)

Runs against the moto-backed conftest stack with the real
functions/greengrass_publish.py handler; greengrassv2 (which moto does
not implement) is a fake client recording recipes and deletions.
"""
import importlib.util
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-publish"
MODELS_TABLE_NAME = "test-models-vllm-publish"

_PUBLISH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "greengrass_publish.py")


def _load_publish_module():
    """Load functions/greengrass_publish.py under a distinct module name
    (inside the moto mock, so its module-level boto3 resource and table
    names bind to the test stack)."""
    spec = importlib.util.spec_from_file_location(
        "portal_greengrass_publish", _PUBLISH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_greengrass_publish"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake Greengrass client (moto has no greengrassv2)
# ---------------------------------------------------------------------------

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
    def __init__(self, create_error=None, final_state="DEPLOYABLE"):
        self.created = []          # parsed recipes, in creation order
        self.deleted = []          # ARNs passed to delete_component
        self.create_error = create_error
        self.final_state = final_state
        # component name -> registered version strings, so the cloud-side
        # version derivation observes what this fake has accepted.
        self.registered = {}

    def create_component_version(self, inlineRecipe, tags=None):
        if self.create_error is not None:
            raise self.create_error
        recipe = json.loads(inlineRecipe)
        self.created.append(recipe)
        self.registered.setdefault(recipe["ComponentName"], set()).add(
            recipe["ComponentVersion"])
        arn = (f"arn:aws:greengrass:{REGION}:123456789012:components:"
               f"{recipe['ComponentName']}:versions:"
               f"{recipe['ComponentVersion']}")
        return {"arn": arn}

    def describe_component(self, arn):
        return {"status": {"componentState": self.final_state,
                           "message": "simulated registration failure"}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pub_env(aws_stack):
    """Training-jobs + models tables + real greengrass_publish module."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    os.environ["MODELS_TABLE"] = MODELS_TABLE_NAME

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
    )


@pytest.fixture
def seeded(pub_env, monkeypatch):
    """Fresh Use_Case + DataScientist; no 2s polling sleeps."""
    monkeypatch.setattr(pub_env.module.time, "sleep", lambda s: None)
    usecase_id = f"uc-{uuid.uuid4()}"
    pub_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "vLLM Publish Use Case",
        "account_id": "123456789012",
        "s3_bucket": "test-vllm-usecase-bucket",
    })
    user_id = f"user-{uuid.uuid4()}"
    pub_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


def seed_vllm_record(pub_env, seeded, **overrides):
    """A packaged vLLM_Model_Record as packaging.py leaves it."""
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "My LLM",
        "model_type": "vllm",
        "source": "vllm",
        "status": "Completed",
        "publish_eligible": True,
        "model_source": {"huggingface_model_id": "facebook/opt-125m"},
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


def publish_event(training_id, user_id):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/publish",
        "pathParameters": {"id": training_id},
        "body": json.dumps({
            # Required by the shared request shape; the vLLM branch
            # derives the actual convention name/version itself.
            "component_name": "model-caller-chosen",
            "component_version": "9.0.0",
        }),
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


# ---------------------------------------------------------------------------
# Success: metadata write-back and published state (2.4, 2.9)
# ---------------------------------------------------------------------------

def test_publish_success_writes_metadata_and_marks_published(
        pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    gg = FakeGreengrass()
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])
    assert body["component_name"] == "model-vllm-my-llm"
    assert body["component_version"] == "1.0.0"

    # Recipe DefaultConfiguration carries the informational metadata. Each
    # Per_JetPack_Component advertises exactly the ONE architecture its
    # target serves, not the record-wide union (vllm-multi-arch-publish-
    # conflict Requirement 2.3).
    assert len(gg.created) == 1
    default_config = gg.created[0]["ComponentConfiguration"][
        "DefaultConfiguration"]
    assert default_config["runtime"] == "vllm"
    assert default_config["supported_architectures"] == ["arm64_jp6"]

    stored = stored_record(pub_env, record["training_id"])

    # published_component map: the record-level keys stay the unsuffixed
    # base name / shared version / record-wide arch union for legacy
    # readers (Requirement 2.5).
    published = stored["published_component"]
    assert published["component_name"] == "model-vllm-my-llm"
    assert published["component_version"] == "1.0.0"
    assert published["supported_architectures"] == ["arm64_jp6", "arm64_jp7"]
    assert published["runtime"] == "vllm"
    assert published["component_arns"]["jetson-xavier-jp6"].endswith(
        "components:model-vllm-my-llm-jetson-xavier-jp6:versions:1.0.0")

    # ...and the new components list carries one entry per
    # Per_JetPack_Component, each with its own suffixed name and its own
    # single architecture (Requirement 2.5).
    assert published["components"] == [{
        "component_name": "model-vllm-my-llm-jetson-xavier-jp6",
        "component_version": "1.0.0",
        "target": "jetson-xavier-jp6",
        "architecture": "arm64_jp6",
        "supported_architectures": ["arm64_jp6"],
        "component_arn": published["component_arns"]["jetson-xavier-jp6"],
    }]

    # Top-level component_name stays the unsuffixed base name so the
    # component_name-index GSI keeps resolving ONE record (task 5.2).
    assert stored["component_name"] == "model-vllm-my-llm"

    # Record marked published; per-target entries retained, each carrying
    # its per-target name and its own architecture.
    assert stored["published"] is True
    assert stored["published_components"][0]["status"] == "published"
    assert stored["published_components"][0]["component_name"] == \
        "model-vllm-my-llm-jetson-xavier-jp6"
    assert stored["published_components"][0]["supported_architectures"] == \
        ["arm64_jp6"]

    # Component version made available for deployments (models table).
    model_item = pub_env.models.get_item(
        Key={"model_id": f"{record['training_id']}-1.0.0"}).get("Item")
    assert model_item is not None
    assert model_item["name"] == "model-vllm-my-llm"
    assert model_item["component_arns"]["jetson-xavier-jp6"] == \
        published["component_arns"]["jetson-xavier-jp6"]


# ---------------------------------------------------------------------------
# Failure: no partial state, record stays pre-publish (2.6)
# ---------------------------------------------------------------------------

def assert_pre_publish(pub_env, record):
    stored = stored_record(pub_env, record["training_id"])
    assert "published_component" not in stored
    assert "component_name" not in stored
    assert "published" not in stored
    assert "published_components" not in stored
    assert stored["updated_at"] == record["updated_at"]
    # No models-table item — nothing made available for deployments.
    listing = pub_env.models.scan().get("Items", [])
    assert all(item["training_job_id"] != record["training_id"]
               for item in listing if "training_job_id" in item)


def test_registration_failure_writes_no_state(pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    gg = FakeGreengrass(create_error=ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "CreateComponentVersion"))
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["failed_step"] == "greengrass_registration"
    assert body["retryable"] is True
    assert "denied" in body["error"]

    assert_pre_publish(pub_env, record)
    assert gg.deleted == []  # nothing was created, nothing to roll back


def test_non_deployable_component_is_rolled_back(pub_env, seeded, monkeypatch):
    record = seed_vllm_record(pub_env, seeded)
    gg = FakeGreengrass(final_state="FAILED")
    monkeypatch.setattr(pub_env.module, "get_usecase_client",
                        lambda service, usecase, **kw: gg)

    response = pub_env.module.publish_component(
        publish_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["failed_step"] == "greengrass_registration"

    assert_pre_publish(pub_env, record)
    # The version created during this attempt was rolled back so a
    # retry can re-register the same derived 1.0.0.
    assert len(gg.deleted) == 1
    assert gg.deleted[0].endswith(
        "components:model-vllm-my-llm-jetson-xavier-jp6:versions:1.0.0")
