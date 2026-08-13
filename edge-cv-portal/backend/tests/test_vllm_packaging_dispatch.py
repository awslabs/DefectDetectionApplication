"""
Unit tests for the vLLM packaging bypass in functions/packaging.py
(vllm-triton-inference task 3.2).

Covers `package_vllm_component` and the `package_components` dispatch:

- a vLLM_Model_Record dispatches to the vLLM packager: the generated
  Triton_vLLM_Repository is zipped, uploaded to
  `model_artifacts/model-{uuid}/…zip` in the Use_Case bucket, and one
  `packaged_components` entry per supported target is recorded, each
  carrying `supported_architectures` (2.4, 2.5)
- the JP5 flag adds the `jetson-xavier-jp5` target (2.5)
- strict ordering: a generation failure uploads nothing and leaves the
  record untouched (2.8); an upload failure records nothing (2.6) —
  both report the failing step, keeping publish retryable
- `auto_triggered` chains into `_trigger_component_creation`
- vision records never enter the vLLM branch (8.2)

Runs against the moto-backed conftest stack with the real
functions/packaging.py handler; records are seeded straight into a
training-jobs table created with the production key shape.
"""
import importlib.util
import io
import json
import os
import sys
import uuid
import zipfile
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-vllm-packaging"
USECASE_BUCKET = "test-vllm-usecase-bucket"

_PACKAGING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "functions", "packaging.py")


def _load_packaging_module():
    """Load functions/packaging.py under a distinct module name.

    The handler module shares its file name with the PyPI `packaging`
    distribution pytest/setuptools depend on, so it must not be
    installed into sys.modules as `packaging`.
    """
    spec = importlib.util.spec_from_file_location(
        "portal_packaging", _PACKAGING_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_packaging"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pkg_env(aws_stack):
    """Training-jobs table + Use_Case bucket + real packaging module."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    aws_stack.s3.create_bucket(Bucket=USECASE_BUCKET)

    # Load inside the mock so the module binds the table name above and
    # moto-intercepted boto3 clients (conftest pattern).
    packaging = _load_packaging_module()

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=packaging,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        s3=aws_stack.s3,
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
    )


@pytest.fixture
def seeded(pkg_env):
    """Fresh Use_Case (single-account, owning the bucket) + DataScientist."""
    usecase_id = f"uc-{uuid.uuid4()}"
    pkg_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "vLLM Packaging Use Case",
        "account_id": "123456789012",
        "s3_bucket": USECASE_BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    pkg_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


def seed_vllm_record(pkg_env, seeded, **overrides):
    """A vLLM_Model_Record exactly as register_vllm_model writes it."""
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
        "engine_configuration": {
            "dtype": "auto",
            "gpu_memory_utilization": Decimal("0.5"),
            "max_model_len": 2048,
            "tensor_parallel_size": 1,
            "enforce_eager": True,
        },
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    item.update(overrides)
    pkg_env.training_jobs.put_item(Item=item)
    return item


def package_event(training_id, user_id, body=None):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/package",
        "pathParameters": {"id": training_id},
        "body": json.dumps(body or {}),
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


def bucket_keys(pkg_env):
    listing = pkg_env.s3.list_objects_v2(Bucket=USECASE_BUCKET)
    return [obj["Key"] for obj in listing.get("Contents", [])]


# ---------------------------------------------------------------------------
# Dispatch and success path (2.4, 2.5)
# ---------------------------------------------------------------------------

def test_vllm_dispatch_packages_uploads_and_records(pkg_env, seeded):
    record = seed_vllm_record(pkg_env, seeded)
    before_keys = set(bucket_keys(pkg_env))

    response = pkg_env.module.package_components(
        package_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    # One entry per supported target — jp6 and jp7 with the flag off —
    # each carrying supported_architectures (2.5).
    assert pkg_env.module.JP5_VLLM_ENABLED is False
    assert [c["target"] for c in body["packaged_components"]] == \
        ["jetson-xavier-jp6", "jetson-xavier-jp7"]
    for packaged in body["packaged_components"]:
        assert packaged["status"] == "packaged"
        assert packaged["supported_architectures"] == \
            ["arm64_jp6", "arm64_jp7"]
    entry = body["packaged_components"][0]

    # Uploaded to the Use_Case bucket under the model_artifacts scheme.
    new_keys = set(bucket_keys(pkg_env)) - before_keys
    assert len(new_keys) == 1
    s3_key = new_keys.pop()
    assert s3_key.startswith("model_artifacts/model-")
    assert s3_key.endswith("_greengrass_model_component.zip")
    assert entry["component_package_s3"] == f"s3://{USECASE_BUCKET}/{s3_key}"

    # The ZIP is exactly the generated Triton_vLLM_Repository.
    payload = pkg_env.s3.get_object(
        Bucket=USECASE_BUCKET, Key=s3_key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zipf:
        names = sorted(zipf.namelist())
        assert names == ["my-llm/1/model.json", "my-llm/config.pbtxt"]
        model_json = json.loads(zipf.read("my-llm/1/model.json"))
        assert model_json["model"] == "facebook/opt-125m"
        assert model_json["max_model_len"] == 2048
        assert 'backend: "vllm"' in zipf.read(
            "my-llm/config.pbtxt").decode()

    # packaged_components persisted on the record.
    stored = pkg_env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert stored["packaged_components"][0]["target"] == "jetson-xavier-jp6"
    assert stored["packaged_components"][1]["target"] == "jetson-xavier-jp7"
    for stored_entry in stored["packaged_components"]:
        assert stored_entry["supported_architectures"] == \
            ["arm64_jp6", "arm64_jp7"]


def test_jp5_flag_adds_jp5_target(pkg_env, seeded, monkeypatch):
    monkeypatch.setattr(pkg_env.module, "JP5_VLLM_ENABLED", True)
    record = seed_vllm_record(pkg_env, seeded)

    response = pkg_env.module.package_components(
        package_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    assert [c["target"] for c in body["packaged_components"]] == \
        ["jetson-xavier-jp6", "jetson-xavier-jp7", "jetson-xavier-jp5"]
    for entry in body["packaged_components"]:
        assert entry["supported_architectures"] == \
            ["arm64_jp6", "arm64_jp7", "arm64_jp5"]
        # One artifact serves both targets (same key scheme as ONNX).
        assert entry["component_package_s3"] == \
            body["packaged_components"][0]["component_package_s3"]


def test_auto_triggered_chains_component_creation(pkg_env, seeded, monkeypatch):
    record = seed_vllm_record(pkg_env, seeded)
    calls = []
    monkeypatch.setattr(
        pkg_env.module, "_trigger_component_creation",
        lambda training_id, training_job: calls.append(training_id))

    response = pkg_env.module.package_components(
        package_event(record["training_id"], seeded.user_id,
                      body={"auto_triggered": True}), None)
    assert response["statusCode"] == 200, response["body"]
    assert json.loads(response["body"])["component_creation_triggered"] is True
    assert calls == [record["training_id"]]

    # Without the flag, packaging does not chain (matches the other paths).
    calls.clear()
    record2 = seed_vllm_record(pkg_env, seeded)
    response = pkg_env.module.package_components(
        package_event(record2["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 200
    assert calls == []


# ---------------------------------------------------------------------------
# Strict ordering / failure atomicity (2.6, 2.8)
# ---------------------------------------------------------------------------

def test_generation_failure_uploads_nothing_and_leaves_record_unchanged(
        pkg_env, seeded):
    # No engine_configuration -> generate_vllm_repository raises before
    # any artifact is assembled (2.8).
    record = seed_vllm_record(pkg_env, seeded)
    pkg_env.training_jobs.update_item(
        Key={"training_id": record["training_id"]},
        UpdateExpression="REMOVE engine_configuration")
    before_keys = set(bucket_keys(pkg_env))

    response = pkg_env.module.package_components(
        package_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["failed_step"] == "repository_generation"
    assert "engine_configuration" in body["error"]

    assert set(bucket_keys(pkg_env)) == before_keys
    stored = pkg_env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert "packaged_components" not in stored
    assert stored["updated_at"] == record["updated_at"]


def test_upload_failure_records_nothing(pkg_env, seeded, monkeypatch):
    record = seed_vllm_record(pkg_env, seeded)

    class FailingS3:
        def upload_file(self, *args, **kwargs):
            raise RuntimeError("simulated S3 upload outage")

    monkeypatch.setattr(
        pkg_env.module, "get_usecase_client",
        lambda service, usecase, **kw: FailingS3())

    response = pkg_env.module.package_components(
        package_event(record["training_id"], seeded.user_id), None)
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["failed_step"] == "artifact_upload"
    assert "simulated S3 upload outage" in body["error"]

    stored = pkg_env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    assert "packaged_components" not in stored
    assert stored["updated_at"] == record["updated_at"]


# ---------------------------------------------------------------------------
# Vision records untouched (8.2)
# ---------------------------------------------------------------------------

def test_vision_record_never_enters_vllm_branch(pkg_env, seeded):
    # A trained vision record without compilation jobs still gets the
    # pre-existing packaging response, not a vLLM packaging attempt.
    training_id = str(uuid.uuid4())
    pkg_env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": "Vision Model",
        "model_type": "classification",
        "source": "trained",
        "status": "Completed",
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    before_keys = set(bucket_keys(pkg_env))

    response = pkg_env.module.package_components(
        package_event(training_id, seeded.user_id), None)
    assert response["statusCode"] == 400
    assert "No compilation jobs found" in json.loads(response["body"])["error"]
    assert set(bucket_keys(pkg_env)) == before_keys
