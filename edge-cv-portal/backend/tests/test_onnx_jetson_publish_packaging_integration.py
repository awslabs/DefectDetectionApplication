# -*- coding: utf-8 -*-
"""Backend integration tests (task 5.2) for onnx-jetson-publish-packaging.

Design "Testing Strategy > Integration Tests", backend arms — full
package → publish → workflow-resolution pipelines over moto AWS plus the
fake Greengrass client (moto implements no greengrassv2):

- **Trained compiled-ONNX end-to-end**: a trained-model record with a
  completed `{target: 'onnx', export_format: 'onnx'}` compilation entry
  and REAL seeded S3 artifacts → `package_components` fans out the three
  per-JetPack entries from one upload → `publish_component` creates three
  DEPLOYABLE components `model-{safe}-onnx-jetson-xavier-jp{N}` with
  aarch64 recipes carrying the matching `arm64JP{N}` HARD LocalServer
  dependency, and writes the per-target `published_components` entries
  back to the training-jobs record → `resolve_model_components(...,
  archs=['arm64_jp7'])` resolves the JP7 ONNX component and
  `model_component_dependencies` emits its HARD workflow dependency.
- **BYO ONNX import end-to-end**: an import-shaped record → the defaulted
  packaging list now covers `jetson-xavier-jp7` → publish creates the JP7
  component → arm64_jp7 workflow resolution succeeds through the EXISTING
  `ARCH_TO_PUBLISH_TARGET['arm64_jp7'] = 'jetson-xavier-jp7'` entry — it
  still resolves with `ARCH_TO_EXTRA_PUBLISH_TARGETS` emptied, proving no
  extras involvement.

Harness: the moto-backed conftest `aws_stack` fixture plus this module's
OWN training-jobs / models tables and bucket (isolated from the sibling
suites), following test_onnx_jetson_publish_packaging_exploration.py.

Run (needs the tests-directory conftest — no `--noconftest`), from
edge-cv-portal/backend/tests with the /home/ubuntu/.venvs/dda-portal-tests
venv:
    python3 -m pytest test_onnx_jetson_publish_packaging_integration.py \
      -q -p no:cacheprovider

**Validates: Requirements 2.5, 2.6, 2.8, 2.9**
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-jetson-integration"
MODELS_TABLE_NAME = "test-models-onnx-jetson-integration"

ACCOUNT_ID = "123456789012"
BUCKET = "test-onnx-jetson-integration-bucket"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.abspath(os.path.join(_HERE, "..", "functions"))
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")

#: Independent oracles, restated (not read back from the modules under
#: test).
ONNX_TARGETS = [
    "onnx-jetson-xavier-jp5",
    "onnx-jetson-xavier-jp6",
    "onnx-jetson-xavier-jp7",
]
LOCAL_SERVER_FOR_TARGET = {
    "onnx-jetson-xavier-jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "onnx-jetson-xavier-jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "onnx-jetson-xavier-jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
    "jetson-xavier-jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "jetson-xavier-jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "jetson-xavier-jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}
BYO_EXPECTED_DEFAULT = {
    "jetson-xavier-jp5",
    "jetson-xavier-jp6",
    "jetson-xavier-jp7",
    "x86_64-cpu",
}
MODEL_DEPENDENCY_ENTRY = {"VersionRequirement": ">=0.0.0",
                          "DependencyType": "HARD"}

STAGE_TYPE = "yolo_object_detection"


def _load_module(path, name):
    """Load a functions/*.py module under a distinct module name (inside
    the moto mock, so module-level boto3 clients bind to the fake AWS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic S3 artifacts — real archives so the packagers run for real
# ---------------------------------------------------------------------------

def _tar_bytes(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relpath, data in members.items():
            info = tarfile.TarInfo(name=relpath)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


_CONFIG_YAML = (
    "dataset:\n"
    "  image_width: 640\n"
    "  image_height: 640\n"
).encode("utf-8")

_MODEL_GRAPH = {
    "stages": [
        {
            "type": STAGE_TYPE,
            "input_shape": [1, 3, 640, 640],
        }
    ]
}


def trained_artifact_tar():
    manifest = {
        "model_graph": _MODEL_GRAPH,
        "input_shape": [1, 3, 640, 640],
    }
    return _tar_bytes({
        "config.yaml": _CONFIG_YAML,
        "export_artifacts/manifest.json": json.dumps(manifest).encode("utf-8"),
        "export_artifacts/model.pt": b"pt-weights-placeholder",
    })


def onnx_export_tar():
    return _tar_bytes({"model.onnx": b"onnx-protobuf-placeholder"})


def byo_onnx_package_tar():
    manifest = {
        "runtime": "onnx",
        "runtime_artifact": "model.onnx",
        "model_graph": _MODEL_GRAPH,
        "input_shape": [1, 3, 640, 640],
    }
    return _tar_bytes({
        "config.yaml": _CONFIG_YAML,
        "export_artifacts/manifest.json": json.dumps(manifest).encode("utf-8"),
        "export_artifacts/model.onnx": b"onnx-protobuf-placeholder",
    })


# ---------------------------------------------------------------------------
# Fake Greengrass client (moto has no greengrassv2)
# ---------------------------------------------------------------------------

def component_arn(name, version=None):
    arn = f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:{name}"
    return f"{arn}:versions:{version}" if version else arn


class _FakePaginator:
    def __init__(self, fake, operation):
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs):
        if self.operation == "list_components":
            yield {"components": [
                {"componentName": name, "arn": component_arn(name)}
                for name in sorted(self.fake.state)
            ]}
        elif self.operation == "list_component_versions":
            name = str(kwargs["arn"]).split(":components:")[1].split(":")[0]
            yield {"componentVersions": [
                {"componentVersion": version,
                 "arn": component_arn(name, version)}
                for version in sorted(self.fake.state.get(name, ()))
            ]}
        else:  # pragma: no cover - unexpected paginator in the publish path
            raise AssertionError(f"unexpected paginator: {self.operation}")


class FakeGreengrass:
    """Behaves like the greengrassv2 service for the vision publish path:
    every accepted create becomes DEPLOYABLE immediately."""

    def __init__(self):
        self.state = {}          # component name -> set of version strings
        self.attempts = []       # every (name, version) create attempted
        self.created = []        # parsed recipes that were accepted
        self.created_arns = []
        self.delete_attempts = []

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        name = recipe["ComponentName"]
        version = recipe["ComponentVersion"]
        self.attempts.append((name, version))
        self.state.setdefault(name, set()).add(version)
        self.created.append(recipe)
        arn = component_arn(name, version)
        self.created_arns.append(arn)
        return {"arn": arn}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.delete_attempts.append(arn)

    def get_paginator(self, operation):
        return _FakePaginator(self, operation)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def integ_env(aws_stack):
    """This module's own training-jobs / models tables and bucket, the real
    packaging / greengrass_publish modules loaded inside the mock, and
    workflow_packaging re-imported so it binds the same tables."""
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

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)

    packaging = _load_module(_PACKAGING_PATH,
                             "portal_packaging_onnx_jetson_integration")
    publish = _load_module(_PUBLISH_PATH,
                           "portal_gg_publish_onnx_jetson_integration")

    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=packaging,
        publish=publish,
        workflow=workflow_packaging,
        s3=s3,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
    )
    mp.undo()
    sys.modules.pop("workflow_packaging", None)


@pytest.fixture
def seeded(integ_env, monkeypatch):
    """Fresh Use_Case (single-account, so get_usecase_client returns the
    moto-bound default clients) + DataScientist, and no polling sleeps."""
    monkeypatch.setattr(integ_env.publish.time, "sleep", lambda s: None)
    usecase_id = f"uc-{uuid.uuid4()}"
    integ_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Jetson Integration Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    integ_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Seeding / invocation helpers
# ---------------------------------------------------------------------------

def _put_s3(integ_env, key, data):
    integ_env.s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return f"s3://{BUCKET}/{key}"


def seed_trained_onnx_record(integ_env, seeded, model_name):
    """A trained-model record with a completed torch.onnx.export
    compilation entry, plus its REAL S3 artifacts."""
    training_id = str(uuid.uuid4())
    trained_uri = _put_s3(integ_env, f"training/{training_id}/model.tar.gz",
                          trained_artifact_tar())
    export_uri = _put_s3(integ_env, f"export/{training_id}/model.tar.gz",
                         onnx_export_tar())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "status": "Completed",
        "artifact_s3": trained_uri,
        "compilation_jobs": [{
            "target": "onnx",
            "export_format": "onnx",
            "status": "Completed",
            "compiled_model_s3": export_uri,
        }],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    integ_env.training_jobs.put_item(Item=item)
    return item


def seed_byo_onnx_import_record(integ_env, seeded, model_name):
    """An imported BYO ONNX record (is_onnx_import) with its REAL package."""
    training_id = str(uuid.uuid4())
    package_uri = _put_s3(integ_env, f"imports/{training_id}/model.tar.gz",
                          byo_onnx_package_tar())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "source": "imported",
        "status": "Completed",
        "metadata": {"framework": "ONNX", "model_file": "model.onnx"},
        "artifact_s3": package_uri,
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    integ_env.training_jobs.put_item(Item=item)
    return item


def _event(path_suffix, training_id, user_id, body):
    return {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/{path_suffix}",
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


def package(integ_env, seeded, record, targets=None):
    body = {} if targets is None else {"targets": targets}
    response = integ_env.packaging.package_components(
        _event("package", record["training_id"], seeded.user_id, body), None)
    return response["statusCode"], json.loads(response["body"])


def publish(integ_env, seeded, record, monkeypatch, gg, component_name):
    monkeypatch.setattr(integ_env.publish, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    response = integ_env.publish.publish_component(
        _event("publish", record["training_id"], seeded.user_id, {
            "component_name": component_name,
            "component_version": "1.0.0",
            "friendly_name": record["model_name"],
        }), None)
    return response["statusCode"], json.loads(response["body"])


def recipe_for(gg, name):
    for recipe in gg.created:
        if recipe["ComponentName"] == name:
            return recipe
    return None


def assert_recipe_stamped(gg, name, target):
    """One accepted recipe: platform aarch64 + HARD dep on the matching
    JetPack LocalServer variant."""
    recipe = recipe_for(gg, name)
    assert recipe is not None, (
        f"no recipe created for {name}; created="
        f"{[r['ComponentName'] for r in gg.created]}")
    assert recipe["Manifests"][0]["Platform"]["architecture"] == "aarch64", (
        f"{name} recipe mis-stamped: {recipe['Manifests'][0]['Platform']}")
    local_server = LOCAL_SERVER_FOR_TARGET[target]
    assert local_server in recipe["ComponentDependencies"], (
        f"{name} must depend on {local_server}, got "
        f"{sorted(recipe['ComponentDependencies'])}")
    assert recipe["ComponentDependencies"][local_server][
        "DependencyType"] == "HARD"


# ===========================================================================
# End-to-end 1: trained compiled-ONNX — package → publish → resolve
# ===========================================================================

def test_trained_onnx_end_to_end_package_publish_resolve(
        integ_env, seeded, monkeypatch):
    """Trained record with a completed onnx export entry + seeded S3 →
    package (per-JetPack fan-out from one upload) → publish (three
    DEPLOYABLE components with correct recipes and per-target write-back)
    → resolve_model_components(['arm64_jp7']) resolves the JP7 ONNX
    component into workflow dependencies.

    **Validates: Requirements 2.5, 2.6, 2.9**
    """
    model_name = "yolo-e2e"
    base = f"model-{model_name}"
    record = seed_trained_onnx_record(integ_env, seeded, model_name)

    # --- package: three per-JetPack entries from ONE upload (2.6) ---------
    status, body = package(integ_env, seeded, record)
    assert status == 200, f"packaging failed: {body}"
    packaged = [e for e in body["packaged_components"]
                if e.get("status") == "packaged"]
    assert sorted(e["target"] for e in packaged) == ONNX_TARGETS
    assert len({e["component_package_s3"] for e in packaged}) == 1

    # --- publish: three DEPLOYABLE per-JetPack components (2.1-2.5) -------
    gg = FakeGreengrass()
    status, body = publish(integ_env, seeded, record, monkeypatch, gg, base)
    assert status == 200, f"publish failed: {body}"
    published = body["published_components"]
    failed = [c for c in published if c.get("status") != "published"]
    assert failed == [], (
        f"failed targets: {[(c.get('target'), c.get('error')) for c in failed]}")

    expected_names = {f"{base}-{t}" for t in ONNX_TARGETS}
    assert {name for name, _ in gg.attempts} == expected_names
    for target in ONNX_TARGETS:
        assert_recipe_stamped(gg, f"{base}-{target}", target)

    # --- write-back: per-target published_components on the record (2.5) --
    item = integ_env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    stored = item.get("published_components") or []
    stored_published = [e for e in stored if e.get("status") == "published"]
    assert {(e["target"], e["component_name"]) for e in stored_published} \
        == {(t, f"{base}-{t}") for t in ONNX_TARGETS}, (
            f"write-back mismatch: {stored}")

    # --- workflow resolution for arm64_jp7 (2.9) ---------------------------
    jp7_component = f"{base}-onnx-jetson-xavier-jp7"
    resolved = integ_env.workflow.resolve_model_components(
        [model_name], seeded.usecase_id, ["arm64_jp7"])
    assert set(resolved[model_name]) == {jp7_component}

    # ... and into the workflow's ComponentDependencies.
    deps = integ_env.workflow.model_component_dependencies(resolved)
    assert deps == {jp7_component: dict(MODEL_DEPENDENCY_ENTRY)}


# ===========================================================================
# End-to-end 2: BYO ONNX import — default covers JP7, resolution through
# the EXISTING primary map entry (no extras involvement)
# ===========================================================================

def test_byo_onnx_import_end_to_end_jp7_via_primary_map_entry(
        integ_env, seeded, monkeypatch):
    """BYO import record → default packaging covers jetson-xavier-jp7 →
    publish creates the JP7 component → arm64_jp7 resolution succeeds
    through the EXISTING ARCH_TO_PUBLISH_TARGET entry — it still resolves
    with ARCH_TO_EXTRA_PUBLISH_TARGETS emptied, proving no extras
    involvement.

    **Validates: Requirements 2.5, 2.8, 2.9**
    """
    model_name = "byo-e2e"
    base = f"model-{model_name}"
    record = seed_byo_onnx_import_record(integ_env, seeded, model_name)

    # --- package with NO explicit targets: the default covers JP7 (2.8) ---
    status, body = package(integ_env, seeded, record)
    assert status == 200, f"BYO packaging failed: {body}"
    packaged = [e for e in body["packaged_components"]
                if e.get("status") == "packaged"]
    assert {e["target"] for e in packaged} == BYO_EXPECTED_DEFAULT
    assert len({e["component_package_s3"] for e in packaged}) == 1, (
        "the BYO arch-agnostic artifact is uploaded once")

    # --- publish: one component per defaulted target, JP7 included --------
    gg = FakeGreengrass()
    status, body = publish(integ_env, seeded, record, monkeypatch, gg, base)
    assert status == 200, f"publish failed: {body}"
    published = body["published_components"]
    failed = [c for c in published if c.get("status") != "published"]
    assert failed == [], (
        f"failed targets: {[(c.get('target'), c.get('error')) for c in failed]}")

    jp7_component = f"{base}-jetson-xavier-jp7"
    created_names = {name for name, _ in gg.attempts}
    assert created_names == {
        f"{base}-{t.replace('_', '-')}" for t in BYO_EXPECTED_DEFAULT}
    assert_recipe_stamped(gg, jp7_component, "jetson-xavier-jp7")

    # --- write-back carries the JP7 entry (2.5) ----------------------------
    item = integ_env.training_jobs.get_item(
        Key={"training_id": record["training_id"]})["Item"]
    stored = item.get("published_components") or []
    jp7_entries = [e for e in stored
                   if e.get("target") == "jetson-xavier-jp7"
                   and e.get("status") == "published"]
    assert [e["component_name"] for e in jp7_entries] == [jp7_component], (
        f"write-back mismatch: {stored}")

    # --- arm64_jp7 resolves through the EXISTING primary map entry (2.9) --
    resolved = integ_env.workflow.resolve_model_components(
        [model_name], seeded.usecase_id, ["arm64_jp7"])
    assert set(resolved[model_name]) == {jp7_component}

    # No extras involvement: with ARCH_TO_EXTRA_PUBLISH_TARGETS emptied,
    # the resolution still succeeds via ARCH_TO_PUBLISH_TARGET alone.
    monkeypatch.setattr(integ_env.workflow,
                        "ARCH_TO_EXTRA_PUBLISH_TARGETS", {})
    resolved_no_extras = integ_env.workflow.resolve_model_components(
        [model_name], seeded.usecase_id, ["arm64_jp7"])
    assert set(resolved_no_extras[model_name]) == {jp7_component}
