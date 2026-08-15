# -*- coding: utf-8 -*-
"""Bug-condition exploration suite (task 1) for onnx-jetson-publish-packaging.

**Property 1: Bug Condition — a compiled ONNX artifact reaches per-JetPack
Jetson components.**

Every case here asserts the FIXED expected behavior, so on the UNFIXED tree
cases 1-4 are EXPECTED TO FAIL — each failure is the counterexample
confirming one of the four hypothesized defects. Case 5 encodes RETAINED
fail-closed behavior (Requirement 2.10): it PASSES on the unfixed tree and
MUST keep passing after the fix — it is never inverted.

- **Case 1 — compiled-ONNX packaging shape** (`isBugCondition_2`):
  `packaging.py::package_components` routes a completed
  `{target: 'onnx', export_format: 'onnx'}` compilation entry through the
  generic Neo Phase 2 loop, producing ONE `packaged_components` entry with
  `target: 'onnx'` whose `create_dda_manifest` manifest has NO top-level
  `runtime` key (the device would default to the DLR runner; JP7 ships no
  DLR at all). Fixed behavior: one entry per
  `onnx-jetson-xavier-jp5/-jp6/-jp7` target from ONE artifact, `manifest.json`
  at the ZIP root with `runtime: 'onnx'` + `runtime_artifact`, and the
  artifact at `<stage_type>/model.onnx` (1.4, 1.5 / 2.6, 2.7).
- **Case 2 — publish fails closed on 'onnx'** (`isBugCondition_1`): the
  packaged ONNX target is a key of neither `TARGET_TO_LOCAL_SERVER` nor
  `TARGET_TO_PLATFORM`, so `resolve_target_platform` raises
  `PublishError` "Unsupported compile target 'onnx'" and the target is
  recorded as failed with no component version created. Fixed behavior:
  three per-JetPack components `model-{safe}-onnx-jetson-xavier-jp{N}`,
  each recipe platform `aarch64` with a HARD dependency on
  `aws.edgeml.dda.LocalServer.arm64JP{N}`, and a per-target
  `published_components` entry with `status: 'published'` (1.1, 1.3 /
  2.1-2.5).
- **Case 3 — BYO default omits JP7** (`isBugCondition_2`, import arm): the
  `is_onnx_import` bypass defaults `onnx_targets` to
  `['jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu']`. Fixed
  behavior: the defaulted entry set is exactly
  `{jetson-xavier-jp5, jetson-xavier-jp6, jetson-xavier-jp7, x86_64-cpu}`
  (1.6 / 2.8).
- **Case 4 — arm64_jp7 workflow resolution** (`isBugCondition_3`): a record
  with a published `onnx-jetson-xavier-jp7` entry still fails
  `resolve_model_components(archs=['arm64_jp7'])` with the
  uncovered-architecture `PackagingError` naming the model and
  `arm64_jp7 (target jetson-xavier-jp7)`, because `ARCH_TO_PUBLISH_TARGET`
  accepts only the (unproducible) Neo id. Fixed behavior: the JP7 ONNX
  component name resolves with no error (1.7 / 2.9).
- **Case 5 — fail-closed retained without a JP7 component** (encodes 2.10):
  the same resolution against a record with only Neo JP5/JP6 published
  entries raises the uncovered-architecture error naming the model and the
  arch. PASSES on the unfixed tree and MUST keep passing after the fix —
  do NOT invert.

(The frontend arm, case 6 / `isBugCondition_4`, lives in
`edge-cv-portal/frontend/src/pages/deployments/onnxComponentArch.property.test.ts`
— example pinning cases that PASS on the unfixed tree, documenting that the
frontend fix is pinning, not code.)

Expected counterexamples on the unfixed tree:
- ONE packaged entry `{'target': 'onnx', ...}` (no per-JetPack fan-out).
- A component ZIP whose root `manifest.json` has NO `runtime` key.
- `PublishError: Unsupported compile target 'onnx': it has no platform and
  LocalServer mapping ...` recorded as a failed target with no create.
- The three-entry BYO default `['jetson-xavier-jp5', 'jetson-xavier-jp6',
  'x86_64-cpu']`.
- `PackagingError ... no published Greengrass component for the selected
  architecture(s) arm64_jp7 (target jetson-xavier-jp7)`.

Harness: the moto-backed `aws_stack` conftest fixture plus training-jobs
(with the production `usecase-training-index` GSI shape) and models tables,
following `test_vllm_multi_arch_publish_exploration.py`. greengrassv2 (which
moto does not implement) is a fake client honoring
`create_component_version` / `describe_component` / `get_paginator` /
`delete_component`. S3 is seeded with a REAL synthetic export `model.tar.gz`
(containing `model.onnx`) and a REAL trained artifact `model.tar.gz`
(containing `config.yaml` + `export_artifacts/manifest.json` + a `.pt` file)
so `create_dda_manifest` and the packagers run for real.

Run (needs the tests-directory conftest for the moto stack — no
`--noconftest`), from edge-cv-portal/backend/tests with the
/home/ubuntu/.venvs/dda-portal-tests venv:
    python3 -m pytest test_onnx_jetson_publish_packaging_exploration.py \
      -q -p no:cacheprovider

**Validates: Requirements 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8**
"""
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-jetson-publish"
MODELS_TABLE_NAME = "test-models-onnx-jetson-publish"

ACCOUNT_ID = "123456789012"
BUCKET = "test-onnx-jetson-usecase-bucket"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.abspath(os.path.join(_HERE, "..", "functions"))
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")

#: The per-JetPack compiled-ONNX packaging-target vocabulary the design
#: fixes (design step 1, `packaging.ONNX_ARCH_TO_TARGET` values) — hardcoded
#: here as an independent oracle, not read back from the module under test.
ONNX_TARGETS = [
    "onnx-jetson-xavier-jp5",
    "onnx-jetson-xavier-jp6",
    "onnx-jetson-xavier-jp7",
]

#: LocalServer variant each per-JetPack ONNX component MUST depend on (2.2).
LOCAL_SERVER_FOR_TARGET = {
    "onnx-jetson-xavier-jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "onnx-jetson-xavier-jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "onnx-jetson-xavier-jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}

#: The fixed BYO-import default target list (2.8): JP7 joins JP5/JP6/x86.
BYO_EXPECTED_DEFAULT = {
    "jetson-xavier-jp5",
    "jetson-xavier-jp6",
    "jetson-xavier-jp7",
    "x86_64-cpu",
}

#: First model_graph stage type in the seeded manifests — the on-device
#: OnnxRunner resolves the artifact at <version_dir>/<stage_type>/<file>.
STAGE_TYPE = "yolo_object_detection"

MODEL_NAME = "yolo-test"
SAFE_MODEL_NAME = "yolo-test"
BASE_COMPONENT_NAME = f"model-{SAFE_MODEL_NAME}"


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
    """A gzipped tar archive from {relative_path: bytes}."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relpath, data in members.items():
            info = tarfile.TarInfo(name=relpath)
            info.size = len(data)
            # A sane mtime: extracted files keep it, and the Phase 2
            # zipfile rejects pre-1980 timestamps.
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
    """The trained-model `model.tar.gz` create_dda_manifest expects:
    config.yaml + export_artifacts/manifest.json + a .pt file."""
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
    """The `torch.onnx.export` job's `model.tar.gz`: the .onnx artifact."""
    return _tar_bytes({
        "model.onnx": b"onnx-protobuf-placeholder",
    })


def byo_onnx_package_tar():
    """An imported BYO Smart-Import ONNX package: device manifest with
    runtime='onnx' at export_artifacts/manifest.json plus model.onnx."""
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
def onnx_env(aws_stack):
    """Training-jobs (production usecase-training-index GSI shape) + models
    tables, the use-case bucket, the real packaging / greengrass_publish
    modules loaded inside the mock, and workflow_packaging re-imported so
    it binds the same training-jobs table."""
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
                             "portal_packaging_onnx_jetson_exploration")
    publish = _load_module(_PUBLISH_PATH,
                           "portal_gg_publish_onnx_jetson_exploration")

    # workflow_packaging must bind the training-jobs table above (the copy
    # conftest imported was bound before TRAINING_JOBS_TABLE existed).
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
def seeded(onnx_env, monkeypatch):
    """Fresh Use_Case (single-account, so get_usecase_client returns the
    moto-bound default clients) + DataScientist, and no polling sleeps."""
    monkeypatch.setattr(onnx_env.publish.time, "sleep", lambda s: None)
    usecase_id = f"uc-{uuid.uuid4()}"
    onnx_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Jetson Publish Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    onnx_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Seeding / invocation helpers
# ---------------------------------------------------------------------------

def _put_s3(onnx_env, key, data):
    onnx_env.s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return f"s3://{BUCKET}/{key}"


def seed_trained_onnx_record(onnx_env, seeded, model_name=MODEL_NAME):
    """A trained-model training-jobs record with a completed
    `torch.onnx.export` compilation entry, plus its REAL S3 artifacts."""
    training_id = str(uuid.uuid4())
    trained_uri = _put_s3(onnx_env, f"training/{training_id}/model.tar.gz",
                          trained_artifact_tar())
    export_uri = _put_s3(onnx_env, f"export/{training_id}/model.tar.gz",
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
    onnx_env.training_jobs.put_item(Item=item)
    return item


def seed_byo_onnx_import_record(onnx_env, seeded, model_name="byo-onnx"):
    """An imported BYO ONNX record (is_onnx_import) with its REAL package."""
    training_id = str(uuid.uuid4())
    package_uri = _put_s3(onnx_env, f"imports/{training_id}/model.tar.gz",
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
    onnx_env.training_jobs.put_item(Item=item)
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


def package(onnx_env, seeded, record, targets=None):
    body = {} if targets is None else {"targets": targets}
    response = onnx_env.packaging.package_components(
        _event("package", record["training_id"], seeded.user_id, body), None)
    return response["statusCode"], json.loads(response["body"])


def publish(onnx_env, seeded, record, monkeypatch, gg):
    monkeypatch.setattr(onnx_env.publish, "get_usecase_client",
                        lambda service, usecase, **kw: gg)
    response = onnx_env.publish.publish_component(
        _event("publish", record["training_id"], seeded.user_id, {
            "component_name": BASE_COMPONENT_NAME,
            "component_version": "1.0.0",
            "friendly_name": record["model_name"],
        }), None)
    return response["statusCode"], json.loads(response["body"])


def read_component_zip(onnx_env, component_package_s3):
    """Download a packaged component ZIP from moto S3 and return
    (namelist, manifest_dict_or_None)."""
    assert component_package_s3.startswith("s3://"), component_package_s3
    bucket, key = component_package_s3[len("s3://"):].split("/", 1)
    with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
        onnx_env.s3.download_file(bucket, key, handle.name)
        with zipfile.ZipFile(handle.name) as zipf:
            names = zipf.namelist()
            manifest = None
            if "manifest.json" in names:
                manifest = json.loads(zipf.read("manifest.json"))
            return names, manifest


def recipe_for(gg, component_name):
    for recipe in gg.created:
        if recipe["ComponentName"] == component_name:
            return recipe
    return None


# ---------------------------------------------------------------------------
# Case 1 — compiled-ONNX packaging shape (isBugCondition_2)
# ---------------------------------------------------------------------------

def test_case_1_compiled_onnx_packaging_fans_out_per_jetpack(
        onnx_env, seeded):
    """isBugCondition_2 holds (a completed torch.onnx.export compilation
    entry), so packaging must fan the ONE exported artifact out into one
    `packaged_components` entry per per-JetPack ONNX target — and the
    arch-less `target: 'onnx'` id must be gone.

    Counterexample on the unfixed tree: the generic Neo Phase 2 loop emits
    exactly ONE entry with `target: 'onnx'`.

    **Validates: Requirements 1.4 (expected behavior 2.6)**
    """
    record = seed_trained_onnx_record(onnx_env, seeded)

    status, body = package(onnx_env, seeded, record)

    assert status == 200, f"packaging failed: {body}"
    entries = body["packaged_components"]
    packaged = [e for e in entries if e.get("status") == "packaged"]
    targets = sorted(e["target"] for e in packaged)

    assert "onnx" not in targets, (
        f"COUNTEREXAMPLE (isBugCondition_2, 1.4): package_components routed "
        f"the completed torch.onnx.export entry through the generic Neo "
        f"Phase 2 loop and emitted the arch-less `target: 'onnx'` entry "
        f"instead of per-JetPack entries; packaged targets={targets}")
    assert targets == sorted(ONNX_TARGETS), (
        f"COUNTEREXAMPLE (isBugCondition_2, 1.4): expected exactly one "
        f"packaged entry per per-JetPack ONNX target {sorted(ONNX_TARGETS)}, "
        f"got {targets} (full entries: {entries})")
    for entry in packaged:
        assert entry.get("component_package_s3"), (
            f"packaged entry for {entry['target']} carries no "
            f"component_package_s3: {entry}")


def test_case_1_manifest_carries_runtime_onnx_and_stage_layout(
        onnx_env, seeded):
    """The compiled-ONNX component ZIP must hold `manifest.json` at the
    payload root with top-level `runtime: 'onnx'` and a `runtime_artifact`,
    and the .onnx artifact at `<stage_type>/model.onnx` (the on-device
    OnnxRunner path). Without `runtime` the device defaults to the DLR
    runner — and JP7 ships no DLR at all.

    Counterexample on the unfixed tree: the Phase 2 loop writes the
    `create_dda_manifest` output — a manifest with `model_graph`,
    `compilable_models`, `dataset` and NO `runtime` key.

    **Validates: Requirements 1.5 (expected behavior 2.7)**
    """
    record = seed_trained_onnx_record(onnx_env, seeded)

    status, body = package(onnx_env, seeded, record)

    assert status == 200, f"packaging failed: {body}"
    packaged = [e for e in body["packaged_components"]
                if e.get("status") == "packaged"
                and e.get("component_package_s3")]
    assert packaged, f"nothing packaged: {body['packaged_components']}"

    for entry in packaged:
        names, manifest = read_component_zip(
            onnx_env, entry["component_package_s3"])
        assert manifest is not None, (
            f"no root manifest.json in the {entry['target']} component ZIP "
            f"(members: {names})")
        assert manifest.get("runtime") == "onnx", (
            f"COUNTEREXAMPLE (isBugCondition_2, 1.5): the {entry['target']} "
            f"component manifest has runtime={manifest.get('runtime')!r} "
            f"(manifest keys: {sorted(manifest)}) — the on-device "
            f"__load_runtime_config defaults an absent runtime to 'dlr', so "
            f"the device would instantiate the DLR runner against an ONNX "
            f"artifact (JP7 has no DLR at all)")
        runtime_artifact = manifest.get("runtime_artifact")
        assert runtime_artifact, (
            f"the {entry['target']} manifest carries no runtime_artifact "
            f"naming the .onnx file (manifest keys: {sorted(manifest)})")
        expected_member = f"{STAGE_TYPE}/{runtime_artifact}"
        assert expected_member in names, (
            f"COUNTEREXAMPLE (isBugCondition_2, 1.5): the ONNX artifact is "
            f"not at <stage_type>/{runtime_artifact} "
            f"(expected {expected_member!r}; ZIP members: {names}) — the "
            f"OnnxRunner resolves <version_dir>/<stage_type>/<artifact>")


# ---------------------------------------------------------------------------
# Case 2 — publish fails closed on 'onnx' (isBugCondition_1)
# ---------------------------------------------------------------------------

def test_case_2_onnx_targets_are_mapped_in_both_publish_maps(onnx_env):
    """isBugCondition_1 must NOT hold for any per-JetPack ONNX target:
    every id must be a key of BOTH TARGET_TO_LOCAL_SERVER and
    TARGET_TO_PLATFORM, mapped to its own JetPack's LocalServer variant and
    to aarch64. The arch-less 'onnx' id stays unmapped (fail-closed
    preserved — the fix never reintroduces a default).

    Counterexample on the unfixed tree: isBugCondition_1 holds — 'onnx' AND
    each 'onnx-jetson-xavier-jp{N}' id are keys of NEITHER map, so any
    packaged ONNX entry fails resolve_target_platform.

    **Validates: Requirements 1.1 (expected behavior 2.2, 2.3)**
    """
    module = onnx_env.publish

    def is_bug_condition_1(target):
        return (target not in module.TARGET_TO_LOCAL_SERVER
                or target not in module.TARGET_TO_PLATFORM)

    unmapped = sorted(t for t in ONNX_TARGETS if is_bug_condition_1(t))
    assert unmapped == [], (
        f"COUNTEREXAMPLE (isBugCondition_1, 1.1): per-JetPack ONNX "
        f"target(s) {unmapped} absent from TARGET_TO_LOCAL_SERVER "
        f"({sorted(module.TARGET_TO_LOCAL_SERVER)}) or TARGET_TO_PLATFORM "
        f"({sorted(module.TARGET_TO_PLATFORM)}); the arch-less 'onnx' id is "
        f"also unmapped ('onnx' in TARGET_TO_LOCAL_SERVER: "
        f"{'onnx' in module.TARGET_TO_LOCAL_SERVER}, in TARGET_TO_PLATFORM: "
        f"{'onnx' in module.TARGET_TO_PLATFORM}), so "
        f"resolve_target_platform raises PublishError for every packaged "
        f"ONNX entry and no component version is ever created")

    for target in ONNX_TARGETS:
        assert module.TARGET_TO_LOCAL_SERVER[target] == \
            LOCAL_SERVER_FOR_TARGET[target], (
                f"{target} must map to {LOCAL_SERVER_FOR_TARGET[target]}, "
                f"got {module.TARGET_TO_LOCAL_SERVER[target]}")
        assert module.TARGET_TO_PLATFORM[target] == "aarch64", (
            f"{target} must map to platform aarch64, got "
            f"{module.TARGET_TO_PLATFORM[target]}")

    # Retained fail-closed discipline: the arch-less compile-target id is
    # never a publish target (holds on the unfixed AND the fixed tree).
    assert "onnx" not in module.TARGET_TO_LOCAL_SERVER
    assert "onnx" not in module.TARGET_TO_PLATFORM


def test_case_2_publish_creates_three_per_jetpack_onnx_components(
        onnx_env, seeded, monkeypatch):
    """Publishing a record packaged from a compiled ONNX artifact must
    create three per-JetPack components model-{safe}-onnx-jetson-xavier-jp{N},
    each recipe manifest platform aarch64 with a HARD ComponentDependencies
    entry on aws.edgeml.dda.LocalServer.arm64JP{N}, and one per-target
    published_components entry with status 'published'.

    The record is seeded with the packaged_components shape packaging.py
    leaves: on the fixed tree the per-JetPack fan-out entry set
    (packaging.ONNX_COMPILED_TARGETS); on the unfixed tree the single
    `target: 'onnx'` entry — exactly isBugCondition_1's input.

    Counterexample on the unfixed tree: resolve_target_platform raises
    PublishError "Unsupported compile target 'onnx'" and the target is
    recorded as failed with NO component version created.

    **Validates: Requirements 1.1, 1.3 (expected behavior 2.1, 2.2, 2.4, 2.5)**
    """
    packaged_targets = list(
        getattr(onnx_env.packaging, "ONNX_COMPILED_TARGETS", None)
        or ["onnx"])
    training_id = str(uuid.uuid4())
    onnx_env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": MODEL_NAME,
        "model_type": "object_detection",
        "status": "Completed",
        "packaged_components": [
            {
                "target": target,
                "status": "packaged",
                "component_package_s3": (
                    f"s3://{BUCKET}/model_artifacts/model-abc/"
                    f"abc_greengrass_model_component.zip"),
            }
            for target in packaged_targets
        ],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    record = {"training_id": training_id, "model_name": MODEL_NAME}
    gg = FakeGreengrass()

    status, body = publish(onnx_env, seeded, record, monkeypatch, gg)

    assert status == 200, f"publish failed outright: {body}"
    published = body["published_components"]
    failed = [c for c in published if c.get("status") != "published"]
    assert failed == [], (
        f"COUNTEREXAMPLE (isBugCondition_1, 1.1/1.3): publish recorded "
        f"failed target(s) with no component version created: "
        f"{[(c.get('target'), c.get('error')) for c in failed]}; create "
        f"attempts={gg.attempts} — the packaged ONNX target is a key of "
        f"neither TARGET_TO_LOCAL_SERVER nor TARGET_TO_PLATFORM, so "
        f"resolve_target_platform fails closed and the ONNX model cannot "
        f"be published for any Jetson device")

    expected_names = {f"{BASE_COMPONENT_NAME}-{t}" for t in ONNX_TARGETS}
    created_names = {name for name, _ in gg.attempts}
    assert created_names == expected_names, (
        f"COUNTEREXAMPLE (1.3): expected one create per per-JetPack ONNX "
        f"component {sorted(expected_names)}, got {sorted(created_names)}")

    for target in ONNX_TARGETS:
        name = f"{BASE_COMPONENT_NAME}-{target}"
        recipe = recipe_for(gg, name)
        assert recipe is not None, f"no recipe created for {name}"
        assert recipe["Manifests"][0]["Platform"]["architecture"] == \
            "aarch64", (
                f"{name} recipe mis-stamped: "
                f"{recipe['Manifests'][0]['Platform']}")
        local_server = LOCAL_SERVER_FOR_TARGET[target]
        assert local_server in recipe["ComponentDependencies"], (
            f"{name} must depend on {local_server}, got "
            f"{sorted(recipe['ComponentDependencies'])}")
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"

        entry = next((c for c in published if c.get("target") == target),
                     None)
        assert entry is not None, (
            f"no per-target published_components entry for {target}: "
            f"{published}")
        assert entry["component_name"] == name
        assert entry["status"] == "published"


# ---------------------------------------------------------------------------
# Case 3 — BYO default omits JP7 (isBugCondition_2, import arm)
# ---------------------------------------------------------------------------

def test_case_3_byo_onnx_import_default_targets_include_jp7(
        onnx_env, seeded):
    """isBugCondition_2's import arm holds (is_onnx_import(record) AND
    requested_targets = NULL): the defaulted packaged entry set must be
    exactly {jetson-xavier-jp5, jetson-xavier-jp6, jetson-xavier-jp7,
    x86_64-cpu}.

    Counterexample on the unfixed tree: the three-entry default
    ['jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu'] — even the
    working BYO import path never packages for JP7.

    **Validates: Requirements 1.6 (expected behavior 2.8)**
    """
    record = seed_byo_onnx_import_record(onnx_env, seeded)

    status, body = package(onnx_env, seeded, record)  # no explicit targets

    assert status == 200, f"BYO ONNX packaging failed: {body}"
    entries = body["packaged_components"]
    targets = {e["target"] for e in entries if e.get("status") == "packaged"}
    assert targets == BYO_EXPECTED_DEFAULT, (
        f"COUNTEREXAMPLE (isBugCondition_2 import arm, 1.6): the BYO ONNX "
        f"defaulted target list is {sorted(targets)} — expected exactly "
        f"{sorted(BYO_EXPECTED_DEFAULT)}; 'jetson-xavier-jp7' is "
        f"{'present' if 'jetson-xavier-jp7' in targets else 'MISSING'}")


# ---------------------------------------------------------------------------
# Cases 4 and 5 — arm64_jp7 workflow resolution (isBugCondition_3)
# ---------------------------------------------------------------------------

def published_entry(component_name, target, version="1.0.0"):
    """One per-target published_components entry, the vision registry shape
    greengrass_publish.py writes."""
    return {
        "component_name": component_name,
        "target": target,
        "component_version": version,
        "status": "published",
        "platform": "aarch64",
        "component_arn": component_arn(component_name, version),
    }


def seed_registry_record(onnx_env, usecase_id, model_name, entries):
    onnx_env.training_jobs.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "created_at": 1,
        "published_components": entries,
    })


@pytest.fixture
def registry_usecase_id():
    """A fresh usecase per test so GSI queries never cross-contaminate."""
    return f"uc-{uuid.uuid4()}"


def test_case_4_arm64_jp7_resolves_published_jp7_onnx_component(
        onnx_env, registry_usecase_id):
    """isBugCondition_3 holds (arm64_jp7 selected, the referenced model has
    a published JP7 ONNX component): resolve_model_components must resolve
    the JP7 ONNX component name with no error.

    Counterexample on the unfixed tree: ARCH_TO_PUBLISH_TARGET accepts only
    'jetson-xavier-jp7' (which nothing can publish), so the resolution
    raises the uncovered-architecture PackagingError naming the model and
    'arm64_jp7 (target jetson-xavier-jp7)'.

    **Validates: Requirements 1.7 (expected behavior 2.9)**
    """
    model_name = MODEL_NAME
    jp7_component = f"{BASE_COMPONENT_NAME}-onnx-jetson-xavier-jp7"
    seed_registry_record(
        onnx_env, registry_usecase_id, model_name,
        [published_entry(jp7_component, "onnx-jetson-xavier-jp7")])

    try:
        resolved = onnx_env.workflow.resolve_model_components(
            [model_name], registry_usecase_id, ["arm64_jp7"])
    except onnx_env.workflow.PackagingError as error:
        pytest.fail(
            f"COUNTEREXAMPLE (isBugCondition_3, 1.7): "
            f"resolve_model_components(['{model_name}'], usecase, "
            f"archs=['arm64_jp7']) raised PackagingError "
            f"(artifact={error.artifact!r}): {error} — for a record with a "
            f"published '{jp7_component}' entry (target "
            f"'onnx-jetson-xavier-jp7', status 'published'); no vision "
            f"workflow can ever be packaged for a JP7 device")

    assert model_name in resolved, (
        f"resolution succeeded but returned no entry for '{model_name}': "
        f"{resolved!r}")
    names = resolved[model_name]
    names = {names["component_name"]} if isinstance(names, dict) else \
        set(names)
    assert jp7_component in names, (
        f"the JP7 ONNX component name did not resolve: {names!r}")


def test_case_5_fail_closed_retained_without_a_jp7_component(
        onnx_env, registry_usecase_id):
    """Encodes Requirement 2.10 — DO NOT INVERT. A record with only Neo
    JP5/JP6 published entries selected for arm64_jp7 must raise the
    uncovered-architecture PackagingError naming the model and the arch.

    PASSES on the unfixed tree and MUST keep passing after the fix: the
    fix makes arm64_jp7 resolvable only when a publishable JP7 entry
    exists — it never weakens the fail-closed coverage gate.

    **Validates: Requirement 1.7 edge (expected behavior 2.10)**
    """
    model_name = "neo-only"
    seed_registry_record(
        onnx_env, registry_usecase_id, model_name,
        [published_entry("model-neo-only-jetson-xavier-jp5",
                         "jetson-xavier-jp5"),
         published_entry("model-neo-only-jetson-xavier-jp6",
                         "jetson-xavier-jp6")])

    with pytest.raises(onnx_env.workflow.PackagingError) as excinfo:
        onnx_env.workflow.resolve_model_components(
            [model_name], registry_usecase_id, ["arm64_jp7"])

    message = str(excinfo.value)
    assert model_name in message, (
        f"the fail-closed error must name the model; got {message!r}")
    assert "arm64_jp7" in message, (
        f"the fail-closed error must name the uncovered architecture "
        f"'arm64_jp7'; got {message!r}")
