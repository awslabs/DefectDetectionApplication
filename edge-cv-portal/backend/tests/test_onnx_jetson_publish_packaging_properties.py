# -*- coding: utf-8 -*-
"""Preservation property suite (task 2) for onnx-jetson-publish-packaging.

**Property 2: Preservation — non-bug inputs are behaviorally identical.**

Every property here was OBSERVED on the UNFIXED tree first and then encoded,
so the suite PASSES before the fix and must keep passing after it (task 3.7).
It pins the baselines design.md's Preservation Checking enumerates:

1. **Publish target-map baseline** (3.1, 3.2) — the exact
   `(LocalServer variant, platform)` pair every currently mapped compile
   target resolves to TODAY (including `jetson-xavier-jp7` → arm64JP7 /
   aarch64), the per-target component naming/recipe/write-back an
   end-to-end publish produces for those targets, and a genuinely unknown
   target failing closed through `resolve_target_platform` with
   `PublishError` and NO `create_component_version`. Deliberately NOT an
   exact-keys assertion: the fix ADDS the three `onnx-jetson-xavier-jp{N}`
   keys, so this pins that today's mapped targets keep resolving to exactly
   today's pairs — never that the maps stay frozen.
2. **Neo Phase 2 packaging** (3.4) — over generated Neo-only completed-job
   sets: one packaged entry per completed job (target id verbatim), the
   ZIP layout (`manifest.json` at the payload root, compiled files under
   the `<stage_type>/` dir), and the `create_dda_manifest` manifest
   byte-identical — `model_graph` + `compilable_models` (framework
   PYTORCH) + `dataset`, and NO top-level `runtime` key (the on-device DLR
   default is the CORRECT runtime for Neo artifacts).
3. **BYO explicit lists** (3.5) — over generated explicit target lists: the
   packaged entry list equals the request verbatim (order included), one
   shared component ZIP, and the layout unchanged (`manifest.json` at the
   root with `runtime: 'onnx'` + merged `dataset`, the artifact under
   `<stage_type>/`).
4. **Non-JP7 workflow resolution** (3.7, 3.8) — over generated published
   shapes and architecture selections EXCLUDING `arm64_jp7`: resolution
   results and the uncovered-architecture error message byte-identical to
   the baseline oracle recorded from the unfixed tree; and
   `ARCH_TO_LOCAL_SERVER_COMPONENT` / `ARCH_TO_GG_PLATFORM` byte-identical.
   Includes the deliberate pin that `onnx-jetson-xavier-jp5/-jp6` published
   entries NEVER satisfy `arm64_jp5`/`arm64_jp6` coverage — true today and
   REQUIRED to stay true after the fix (JP5/JP6 keep Neo-only vision
   semantics; only arm64_jp7 gains an extra accepted id).

The compile-targets guard (3.9) is NOT duplicated here — the gate is
re-running `test_onnx_compile_diagnostics_exploration.py` case 9 (no JP7 Neo
compile target, exactly seven targets). The frontend inference baseline
(3.13) lives in
`edge-cv-portal/frontend/src/pages/deployments/onnxComponentArch.property.test.ts`
(preservation describe block).

Harness: the moto-backed `aws_stack` conftest fixture plus this suite's OWN
training-jobs / models tables (production GSI shapes) and bucket, following
`test_onnx_jetson_publish_packaging_exploration.py` but with distinct table
names so the suites stay independent. greengrassv2 (which moto does not
implement) is a fake client. Hypothesis settings come from the
conftest-registered profiles (`portal-fast`/`ci`) — no hardcoded
`max_examples`.

Fix-checking properties (task 4, appended AFTER the fix landed) live at the
bottom of this file under the "Fix-checking properties" banner: Correctness
Properties 3 (target-map totality, task 4.1), 4 (fan-out/manifest/BYO
default, task 4.2), 5 (derived-name gates, task 4.3), 6 (arm64_jp7
resolution, task 4.4), and 1 (full pipeline, task 4.7). The six Property 2
preservation tests above them are UNCHANGED.

Run (needs the tests-directory conftest — no `--noconftest`), from
edge-cv-portal/backend/tests with the /home/ubuntu/.venvs/dda-portal-tests
venv:
    python3 -m pytest test_onnx_jetson_publish_packaging_properties.py \
      -q -p no:cacheprovider

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10,
3.1, 3.2, 3.4, 3.5, 3.7, 3.8**
"""
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-jetson-props"
MODELS_TABLE_NAME = "test-models-onnx-jetson-props"

ACCOUNT_ID = "123456789012"
BUCKET = "test-onnx-jetson-props-bucket"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.abspath(os.path.join(_HERE, "..", "functions"))
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")

# ---------------------------------------------------------------------------
# Baselines OBSERVED on the unfixed tree (independent oracles — hardcoded,
# never read back from the modules under test)
# ---------------------------------------------------------------------------

#: The `(LocalServer variant, manifest platform)` pair every currently
#: mapped compile target resolves to TODAY (greengrass_publish.py
#: TARGET_TO_LOCAL_SERVER / TARGET_TO_PLATFORM, observed unfixed). The fix
#: ADDS the three onnx-jetson-xavier-jp{N} keys; these pairs must never
#: change (3.1).
BASELINE_TARGET_RESOLUTION = {
    "jetson-xavier": ("aws.edgeml.dda.LocalServer.arm64JP4", "aarch64"),
    "jetson-xavier-jp5": ("aws.edgeml.dda.LocalServer.arm64JP5", "aarch64"),
    "jetson-xavier-jp6": ("aws.edgeml.dda.LocalServer.arm64JP6", "aarch64"),
    "jetson-xavier-jp7": ("aws.edgeml.dda.LocalServer.arm64JP7", "aarch64"),
    "arm64-cpu": ("aws.edgeml.dda.LocalServer.arm64JP4", "aarch64"),
    "x86_64-cpu": ("aws.edgeml.dda.LocalServer.amd64", "amd64"),
    "x86_64-cuda": ("aws.edgeml.dda.LocalServer.amd64", "amd64"),
}

#: workflow_packaging.py arch maps as they exist TODAY — asserted
#: byte-identical (dict equality) because the fix must not touch either
#: map (3.8).
BASELINE_ARCH_TO_LOCAL_SERVER_COMPONENT = {
    "arm64_jp4": "aws.edgeml.dda.LocalServer.arm64JP4",
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
    "x86_64": "aws.edgeml.dda.LocalServer.amd64",
    "x86_64_nvidia": "aws.edgeml.dda.LocalServer.amd64",
}
BASELINE_ARCH_TO_GG_PLATFORM = {
    "x86_64": "amd64",
    "x86_64_nvidia": "amd64",
    "arm64_jp4": "aarch64",
    "arm64_jp5": "aarch64",
    "arm64_jp6": "aarch64",
    "arm64_jp7": "aarch64",
}

#: The arch → vision publish-target acceptance for the NON-JP7 architectures
#: as observed on the unfixed tree (workflow_packaging.ARCH_TO_PUBLISH_TARGET
#: minus arm64_jp7). The fix changes ONLY arm64_jp7 (it gains
#: 'onnx-jetson-xavier-jp7' as a second accepted id); every arch here keeps
#: exactly this singleton acceptance (3.7).
BASELINE_NON_JP7_ARCH_TO_PUBLISH_TARGET = {
    "arm64_jp4": "jetson-xavier",
    "arm64_jp5": "jetson-xavier-jp5",
    "arm64_jp6": "jetson-xavier-jp6",
    "x86_64": "x86_64-cpu",
    "x86_64_nvidia": "x86_64-cuda",
}

#: First model_graph stage type in the seeded manifests — the packagers nest
#: per-stage artifacts under this directory inside the component ZIP.
STAGE_TYPE = "yolo_object_detection"

#: The exact create_dda_manifest output for the seeded trained artifact,
#: observed on the unfixed tree — the runtime-LESS manifest the Neo Phase 2
#: loop writes as manifest.json (the DLR default is CORRECT for Neo
#: artifacts; 3.4).
_MODEL_GRAPH = {
    "stages": [
        {
            "type": STAGE_TYPE,
            "input_shape": [1, 3, 640, 640],
        }
    ]
}
EXPECTED_NEO_MANIFEST = {
    "model_graph": _MODEL_GRAPH,
    "compilable_models": [{
        "filename": "model.pt",
        "data_input_config": {"input": [1, 3, 640, 640]},
        "framework": "PYTORCH",
    }],
    "dataset": {"image_width": 640, "image_height": 640},
}

#: The exact BYO-import component manifest observed on the unfixed tree:
#: the imported Smart-Import manifest with the config.yaml dataset block
#: merged in (3.5).
EXPECTED_BYO_MANIFEST = {
    "runtime": "onnx",
    "runtime_artifact": "model.onnx",
    "model_graph": _MODEL_GRAPH,
    "input_shape": [1, 3, 640, 640],
    "dataset": {"image_width": 640, "image_height": 640},
}

#: Neo compile-target ids a trained vision model can hold completed
#: compilation jobs for (COMPILATION_TARGETS vocabulary minus 'onnx').
NEO_TARGETS = (
    "jetson-xavier",
    "jetson-xavier-jp5",
    "jetson-xavier-jp6",
    "arm64-cpu",
    "x86_64-cpu",
    "x86_64-cuda",
)

#: Published-target pool for the workflow-resolution property: today's Neo /
#: x86 vision ids plus the per-JetPack ONNX ids the fix introduces — seeding
#: ONNX entries NOW pins that they contribute nothing to non-JP7 coverage,
#: before and after the fix.
PUBLISHED_TARGET_POOL = (
    "jetson-xavier",
    "jetson-xavier-jp5",
    "jetson-xavier-jp6",
    "jetson-xavier-jp7",
    "x86_64-cpu",
    "x86_64-cuda",
    "onnx-jetson-xavier-jp5",
    "onnx-jetson-xavier-jp6",
    "onnx-jetson-xavier-jp7",
)


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


#: The files inside every synthetic Neo compiled `model.tar.gz` — the Phase 2
#: loop extracts them under `<stage_type>/` in the component payload.
NEO_COMPILED_FILES = ("compiled.so", "compiled.params")


def neo_compiled_tar():
    """A synthetic Neo compilation output archive."""
    return _tar_bytes({
        name: f"neo-artifact-{name}".encode("utf-8")
        for name in NEO_COMPILED_FILES
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


class FakeGreengrass:
    """Accepts every create and reports DEPLOYABLE immediately, so the only
    publish outcomes these properties observe come from the handler's own
    decisions (naming, resolution, fail-closed recording)."""

    def __init__(self):
        self.attempts = []       # every (name, version) create attempted
        self.created = []        # parsed recipes that were accepted
        self.deleted = []        # rollback attempts

    def create_component_version(self, inlineRecipe, tags=None):
        recipe = json.loads(inlineRecipe)
        name = recipe["ComponentName"]
        version = recipe["ComponentVersion"]
        self.attempts.append((name, version))
        self.created.append(recipe)
        return {"arn": component_arn(name, version)}

    def describe_component(self, arn):
        return {"status": {"componentState": "DEPLOYABLE", "message": ""}}

    def delete_component(self, arn):
        self.deleted.append(arn)

    def recipe_for(self, name):
        for recipe in self.created:
            if recipe["ComponentName"] == name:
                return recipe
        return None


# ---------------------------------------------------------------------------
# Environment (module-scoped: function-scoped fixtures are incompatible
# with Hypothesis, so every per-example seam is patched inside the tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def props_env(aws_stack):
    """Training-jobs (production usecase-training-index GSI shape) + models
    tables with this suite's OWN names, the bucket, the real packaging /
    greengrass_publish modules loaded inside the mock, workflow_packaging
    re-imported so it binds the same training-jobs table, and one shared
    Use_Case + DataScientist for the package/publish handlers (workflow
    resolution uses a fresh usecase id per example instead)."""
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
                             "portal_packaging_onnx_jetson_props")
    publish = _load_module(_PUBLISH_PATH,
                           "portal_gg_publish_onnx_jetson_props")

    # workflow_packaging must bind the training-jobs table above (the copy
    # conftest imported was bound before TRAINING_JOBS_TABLE existed).
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    resource = boto3.resource("dynamodb", region_name=REGION)

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Jetson Preservation Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })

    # Shared S3 artifacts (content is target-independent; each generated
    # example seeds its own training-jobs record pointing at them).
    trained_uri = _put_s3(s3, "props/trained/model.tar.gz",
                          trained_artifact_tar())
    neo_uri = _put_s3(s3, "props/neo/model.tar.gz", neo_compiled_tar())
    byo_uri = _put_s3(s3, "props/byo/model.tar.gz", byo_onnx_package_tar())

    yield SimpleNamespace(
        packaging=packaging,
        publish=publish,
        workflow=workflow_packaging,
        s3=s3,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        usecase_id=usecase_id,
        user_id=user_id,
        trained_uri=trained_uri,
        neo_uri=neo_uri,
        byo_uri=byo_uri,
    )
    mp.undo()
    sys.modules.pop("workflow_packaging", None)


def _put_s3(s3, key, data):
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return f"s3://{BUCKET}/{key}"


# ---------------------------------------------------------------------------
# Seeding / invocation helpers
# ---------------------------------------------------------------------------

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


def package(env, training_id, targets=None):
    body = {} if targets is None else {"targets": targets}
    response = env.packaging.package_components(
        _event("package", training_id, env.user_id, body), None)
    return response["statusCode"], json.loads(response["body"])


def publish(env, training_id, component_name, gg, mp):
    mp.setattr(env.publish.time, "sleep", lambda seconds: None)
    mp.setattr(env.publish, "get_usecase_client",
               lambda service, usecase, **kwargs: gg)
    response = env.publish.publish_component(
        _event("publish", training_id, env.user_id, {
            "component_name": component_name,
            "component_version": "1.0.0",
            "friendly_name": "Preserved Model",
        }), None)
    return response["statusCode"], json.loads(response["body"])


def read_component_zip(env, component_package_s3):
    """Download a packaged component ZIP from moto S3 and return
    (namelist, manifest_dict_or_None)."""
    assert component_package_s3.startswith("s3://"), component_package_s3
    bucket, key = component_package_s3[len("s3://"):].split("/", 1)
    with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
        env.s3.download_file(bucket, key, handle.name)
        with zipfile.ZipFile(handle.name) as zipf:
            names = zipf.namelist()
            manifest = None
            if "manifest.json" in names:
                manifest = json.loads(zipf.read("manifest.json"))
            return names, manifest


def seed_packaged_record(env, targets):
    """A vision record already packaged for `targets` — the publish input."""
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": "Preserved Model",
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
            for target in targets
        ],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    return training_id


def seed_neo_record(env, neo_targets):
    """A trained vision record with completed Neo compilation jobs."""
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": "Preserved Model",
        "model_type": "object_detection",
        "status": "Completed",
        "artifact_s3": env.trained_uri,
        "compilation_jobs": [
            {
                "target": target,
                "status": "Completed",
                "compiled_model_s3": env.neo_uri,
            }
            for target in neo_targets
        ],
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    return training_id


def seed_byo_record(env):
    """An imported BYO ONNX record (is_onnx_import)."""
    training_id = str(uuid.uuid4())
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": "byo-onnx",
        "model_type": "object_detection",
        "source": "imported",
        "status": "Completed",
        "metadata": {"framework": "ONNX", "model_file": "model.onnx"},
        "artifact_s3": env.byo_uri,
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    })
    return training_id


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


def seed_registry_record(env, usecase_id, model_name, entries):
    env.training_jobs.put_item(Item={
        "training_id": f"tr-{uuid.uuid4()}",
        "usecase_id": usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "created_at": 1,
        "published_components": entries,
    })


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"

_slugs = st.text(alphabet=_ALNUM + "-", min_size=1, max_size=16).filter(
    lambda s: not s.startswith("-") and not s.endswith("-") and "--" not in s)

component_base_names = _slugs.map(lambda s: f"model-{s}")

mapped_target_lists = st.lists(
    st.sampled_from(sorted(BASELINE_TARGET_RESOLUTION)),
    min_size=1, max_size=3, unique=True)

# Genuinely unknown packaging targets (3.2): the prefix guarantees they can
# never collide with a mapped target — today's seven ids OR the three
# onnx-jetson-xavier-jp{N} ids the fix adds — so this stays "genuinely
# unknown" on both trees.
unknown_targets = _slugs.map(lambda s: f"unknown-target-{s}")

neo_target_lists = st.lists(st.sampled_from(NEO_TARGETS),
                            min_size=1, max_size=3, unique=True)

byo_explicit_lists = st.lists(
    st.sampled_from(sorted(BASELINE_TARGET_RESOLUTION)),
    min_size=1, max_size=4, unique=True)

published_target_sets = st.lists(st.sampled_from(PUBLISHED_TARGET_POOL),
                                 min_size=1, max_size=4, unique=True)

non_jp7_arch_selections = st.lists(
    st.sampled_from(sorted(BASELINE_NON_JP7_ARCH_TO_PUBLISH_TARGET)),
    min_size=1, max_size=3, unique=True)


# ===========================================================================
# Baseline 1 — publish target-map resolution and unknown-target fail-closed
# ===========================================================================

@settings(deadline=None)
@given(component_name=component_base_names,
       targets=mapped_target_lists,
       unknown_target=unknown_targets)
def test_preservation_publish_target_map_baseline_and_unknown_fail_closed(
        props_env, component_name, targets, unknown_target):
    """**Property 2 — publish target-map baseline (3.1, 3.2).**

    OBSERVED on the unfixed tree and encoded: every currently mapped compile
    target resolves to exactly today's `(LocalServer variant, platform)`
    pair — through the module maps, through `resolve_target_platform` /
    `resolve_local_server_component`, and through a real end-to-end
    `publish_component` (per-target `f"{base}-{suffix}"` naming, the
    baseline platform stamped on the recipe manifest, a HARD dependency on
    the baseline LocalServer variant, and a per-target
    `published_components` entry with `status: 'published'`).

    A genuinely unknown target (`unknown-target-*` can never be mapped,
    before OR after the fix — the fix only adds the three
    `onnx-jetson-xavier-jp{N}` keys) raises `PublishError` from
    `resolve_target_platform`, is recorded as a failed target, and NO
    `create_component_version` happens for it. Deliberately NOT asserted:
    that the maps hold exactly today's keys — the fix ADDS keys, and this
    baseline must survive that.

    # Validates: Requirements 3.1, 3.2
    """
    module = props_env.publish

    # The map/resolver baseline for every currently mapped target.
    for target, (local_server, platform) in BASELINE_TARGET_RESOLUTION.items():
        assert module.TARGET_TO_LOCAL_SERVER[target] == local_server, (
            f"{target} must keep mapping to {local_server}, got "
            f"{module.TARGET_TO_LOCAL_SERVER.get(target)}")
        assert module.TARGET_TO_PLATFORM[target] == platform, (
            f"{target} must keep mapping to platform {platform}, got "
            f"{module.TARGET_TO_PLATFORM.get(target)}")
        assert module.resolve_target_platform(target) == platform
        assert module.resolve_local_server_component(
            target, platform) == local_server

    # A genuinely unknown target fails closed in resolve_target_platform.
    with pytest.raises(module.PublishError) as excinfo:
        module.resolve_target_platform(unknown_target)
    assert "Unsupported compile target" in str(excinfo.value)

    # End-to-end publish: mapped targets resolve exactly as today; the
    # unknown target is recorded failed with no create.
    all_targets = list(targets) + [unknown_target]
    training_id = seed_packaged_record(props_env, all_targets)
    gg = FakeGreengrass()
    with pytest.MonkeyPatch.context() as mp:
        status, body = publish(props_env, training_id, component_name, gg, mp)

    # A failing target does NOT fail a vision publish (no atomicity gate,
    # no rollback).
    assert status == 200, body
    assert gg.deleted == [], (
        f"vision publish must never roll back created versions: {gg.deleted}")
    entries = {entry["target"]: entry
               for entry in body["published_components"]}

    for target in targets:
        local_server, platform = BASELINE_TARGET_RESOLUTION[target]
        expected_name = f"{component_name}-{target.replace('_', '-')}"
        entry = entries[target]
        assert entry["component_name"] == expected_name
        assert entry["status"] == "published"
        assert entry["platform"] == platform
        assert (expected_name, "1.0.0") in gg.attempts
        recipe = gg.recipe_for(expected_name)
        assert recipe is not None
        assert recipe["Manifests"][0]["Platform"]["architecture"] == platform
        assert local_server in recipe["ComponentDependencies"], (
            f"{expected_name} must depend on {local_server}, got "
            f"{sorted(recipe['ComponentDependencies'])}")
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"

    failed = entries[unknown_target]
    failed_name = f"{component_name}-{unknown_target.replace('_', '-')}"
    assert failed["status"] == "failed"
    assert "Unsupported compile target" in failed["error"], (
        f"the unknown target must fail through resolve_target_platform: "
        f"{failed['error']!r}")
    assert failed_name not in {name for name, _ in gg.attempts}, (
        f"an unmapped target must never reach create_component_version: "
        f"{gg.attempts}")


# ===========================================================================
# Baseline 2 — Neo Phase 2 packaging shape and runtime-LESS manifest
# ===========================================================================

@settings(deadline=None)
@given(neo_targets=neo_target_lists)
def test_preservation_neo_phase2_packaging_shape_and_runtimeless_manifest(
        props_env, neo_targets):
    """**Property 2 — Neo Phase 2 packaging baseline (3.4).**

    OBSERVED on the unfixed tree and encoded: for any set of completed Neo
    compilation jobs, the generic Phase 2 loop emits exactly one packaged
    entry per job (target id verbatim, in job order, each with its own
    component ZIP), the ZIP holds `manifest.json` at the payload root and
    the extracted compiled files under the `<stage_type>/` dir, and the
    manifest is the `create_dda_manifest` output byte-identical —
    `model_graph` + `compilable_models` (framework PYTORCH) + `dataset`,
    with NO top-level `runtime` key. The DLR default the device derives
    from that absent key is the CORRECT runtime for Neo/DLR artifacts, so
    the fix must leave this loop alone.

    # Validates: Requirements 3.4
    """
    training_id = seed_neo_record(props_env, neo_targets)

    status, body = package(props_env, training_id)

    assert status == 200, f"Neo packaging failed: {body}"
    entries = body["packaged_components"]
    assert [e["target"] for e in entries] == list(neo_targets), (
        f"Neo Phase 2 must emit one entry per completed job, target ids "
        f"verbatim in job order; got {[e['target'] for e in entries]}")
    assert all(e["status"] == "packaged" for e in entries), entries

    expected_members = {"manifest.json"} | {
        f"{STAGE_TYPE}/{name}" for name in NEO_COMPILED_FILES}
    for entry in entries:
        names, manifest = read_component_zip(
            props_env, entry["component_package_s3"])
        assert set(names) == expected_members, (
            f"the {entry['target']} Neo component ZIP layout changed: "
            f"{sorted(names)} != {sorted(expected_members)}")
        assert manifest == EXPECTED_NEO_MANIFEST, (
            f"the {entry['target']} Neo manifest changed: {manifest}")
        assert "runtime" not in manifest, (
            f"the Neo manifest must stay runtime-LESS (the DLR default is "
            f"correct for Neo artifacts); got runtime="
            f"{manifest.get('runtime')!r}")


# ===========================================================================
# Baseline 3 — BYO explicit target lists verbatim and ZIP layout
# ===========================================================================

@settings(deadline=None)
@given(explicit_targets=byo_explicit_lists)
def test_preservation_byo_explicit_target_lists_verbatim_and_layout(
        props_env, explicit_targets):
    """**Property 2 — BYO-import explicit-list baseline (3.5).**

    OBSERVED on the unfixed tree and encoded: for any EXPLICIT caller-
    requested target list, the BYO ONNX import path packages ONE artifact
    and emits the packaged entry list exactly equal to the request —
    verbatim, order preserved, one shared `component_package_s3` — and the
    component ZIP layout is unchanged: `manifest.json` at the root with
    `runtime: 'onnx'`, `runtime_artifact`, and the merged `dataset` block,
    the artifact under `<stage_type>/`. (Only the DEFAULTED list changes
    with the fix — that is exploration case 3, not this baseline.)

    # Validates: Requirements 3.5
    """
    training_id = seed_byo_record(props_env)

    status, body = package(props_env, training_id, targets=explicit_targets)

    assert status == 200, f"BYO ONNX packaging failed: {body}"
    entries = body["packaged_components"]
    assert [e["target"] for e in entries] == list(explicit_targets), (
        f"explicit BYO target lists must be honored verbatim; requested "
        f"{explicit_targets}, packaged {[e['target'] for e in entries]}")
    assert all(e["status"] == "packaged" for e in entries), entries
    uris = {e["component_package_s3"] for e in entries}
    assert len(uris) == 1, (
        f"BYO packaging builds ONE architecture-agnostic artifact shared "
        f"by every requested target; got {sorted(uris)}")

    names, manifest = read_component_zip(props_env, next(iter(uris)))
    assert set(names) == {"manifest.json", f"{STAGE_TYPE}/model.onnx"}, (
        f"the BYO component ZIP layout changed: {sorted(names)}")
    assert manifest == EXPECTED_BYO_MANIFEST, (
        f"the BYO component manifest changed: {manifest}")


# ===========================================================================
# Baseline 4 — non-JP7 workflow resolution and the untouched arch maps
# ===========================================================================

def test_preservation_workflow_arch_maps_byte_identical(props_env):
    """**Property 2 — workflow arch maps baseline (3.8).**

    `ARCH_TO_LOCAL_SERVER_COMPONENT` and `ARCH_TO_GG_PLATFORM` are asserted
    byte-identical (full dict equality) to the values observed on the
    unfixed tree: the fix touches NEITHER map (only the publish-target
    acceptance for arm64_jp7 changes, in a different structure).

    # Validates: Requirements 3.8
    """
    assert props_env.workflow.ARCH_TO_LOCAL_SERVER_COMPONENT == \
        BASELINE_ARCH_TO_LOCAL_SERVER_COMPONENT, (
            "ARCH_TO_LOCAL_SERVER_COMPONENT changed; the fix must not "
            "touch it")
    assert props_env.workflow.ARCH_TO_GG_PLATFORM == \
        BASELINE_ARCH_TO_GG_PLATFORM, (
            "ARCH_TO_GG_PLATFORM changed; the fix must not touch it")


@settings(deadline=None)
@given(published_targets=published_target_sets,
       archs=non_jp7_arch_selections)
def test_preservation_non_jp7_workflow_resolution_identical(
        props_env, published_targets, archs):
    """**Property 2 — non-JP7 workflow vision resolution baseline (3.7).**

    OBSERVED on the unfixed tree and encoded: for any published shape
    (including per-JetPack ONNX entries) and any architecture selection
    EXCLUDING `arm64_jp7`, `resolve_model_components` behaves exactly per
    the baseline oracle recorded from today's `ARCH_TO_PUBLISH_TARGET`:

    - coverage: an arch is covered iff its ONE baseline target id is among
      the record's published targets — `onnx-jetson-xavier-jp5/-jp6/-jp7`
      entries NEVER cover a non-JP7 arch (JP5/JP6 keep Neo-only vision
      semantics after the fix);
    - resolved names: exactly the entries whose target is among the
      selected archs' baseline targets;
    - the uncovered-architecture `PackagingError` message byte-identical.

    # Validates: Requirements 3.7
    """
    model_name = "preserved-vision-model"
    usecase_id = f"uc-{uuid.uuid4()}"  # fresh per example: GSI isolation
    name_of_target = {t: f"model-preserved-{t}" for t in published_targets}
    seed_registry_record(
        props_env, usecase_id, model_name,
        [published_entry(name_of_target[t], t) for t in published_targets])

    # The baseline oracle, computed independently of the module under test.
    target_of_arch = {a: BASELINE_NON_JP7_ARCH_TO_PUBLISH_TARGET[a]
                      for a in archs}
    published_set = set(published_targets)
    uncovered = sorted(a for a, t in target_of_arch.items()
                       if t not in published_set)

    if uncovered:
        expected_message = (
            f"Model '{model_name}' has no published Greengrass component "
            f"for the selected architecture(s) "
            f"{', '.join(f'{a} (target {target_of_arch[a]})' for a in uncovered)}; "
            f"it is published for targets "
            f"[{', '.join(sorted(published_set))}]. "
            f"Publish the model for every selected architecture "
            f"before packaging workflows that use it")
        with pytest.raises(props_env.workflow.PackagingError) as excinfo:
            props_env.workflow.resolve_model_components(
                [model_name], usecase_id, list(archs))
        assert excinfo.value.artifact == f"models/{model_name}"
        assert str(excinfo.value) == expected_message, (
            f"the uncovered-architecture error text changed:\n"
            f"  got:      {str(excinfo.value)!r}\n"
            f"  expected: {expected_message!r}")
    else:
        resolved = props_env.workflow.resolve_model_components(
            [model_name], usecase_id, list(archs))
        accepted = set(target_of_arch.values())
        expected_names = {name_of_target[t] for t in published_targets
                          if t in accepted}
        assert resolved == {model_name: expected_names}, (
            f"non-JP7 resolution changed: {resolved!r} != "
            f"{{{model_name!r}: {expected_names!r}}}")


def test_preservation_onnx_jp5_jp6_entries_never_cover_arm64_jp5_jp6(
        props_env):
    """**Property 2 — ONNX JP5/JP6 entries stay outside JP5/JP6 coverage
    (3.7).**

    A record published ONLY as `onnx-jetson-xavier-jp5` and
    `onnx-jetson-xavier-jp6` does NOT cover `arm64_jp5` / `arm64_jp6`:
    resolution fails closed naming both archs and their (Neo) baseline
    targets. True on the unfixed tree and REQUIRED to stay true after the
    fix — only `arm64_jp7` gains an ONNX accepted id; JP5/JP6 keep their
    Neo-only vision route.

    # Validates: Requirements 3.7
    """
    model_name = "onnx-only"
    usecase_id = f"uc-{uuid.uuid4()}"
    seed_registry_record(
        props_env, usecase_id, model_name,
        [published_entry("model-onnx-only-onnx-jetson-xavier-jp5",
                         "onnx-jetson-xavier-jp5"),
         published_entry("model-onnx-only-onnx-jetson-xavier-jp6",
                         "onnx-jetson-xavier-jp6")])

    with pytest.raises(props_env.workflow.PackagingError) as excinfo:
        props_env.workflow.resolve_model_components(
            [model_name], usecase_id, ["arm64_jp5", "arm64_jp6"])

    message = str(excinfo.value)
    assert "arm64_jp5 (target jetson-xavier-jp5)" in message, message
    assert "arm64_jp6 (target jetson-xavier-jp6)" in message, message


# ===========================================================================
# ===========================================================================
# Fix-checking properties (task 4) — Correctness Properties 1, 3, 4, 5, 6
# ===========================================================================
# ===========================================================================
# Everything below was added AFTER the fix landed (tasks 3.1-3.7 verified).
# Oracles are hardcoded independently of the modules under test.

#: The per-JetPack compiled-ONNX target vocabulary the fix introduces
#: (design step 1) — the independent oracle for packaging.ONNX_ARCH_TO_TARGET
#: values / ONNX_COMPILED_TARGETS.
EXPECTED_ONNX_TARGETS = [
    "onnx-jetson-xavier-jp5",
    "onnx-jetson-xavier-jp6",
    "onnx-jetson-xavier-jp7",
]

#: LocalServer variant each per-JetPack ONNX target MUST resolve to (2.2).
EXPECTED_ONNX_LOCAL_SERVER = {
    "onnx-jetson-xavier-jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "onnx-jetson-xavier-jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "onnx-jetson-xavier-jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}

#: The fixed BYO-import DEFAULT target list (2.8) — exact, order included.
EXPECTED_BYO_DEFAULT = [
    "jetson-xavier-jp5",
    "jetson-xavier-jp6",
    "jetson-xavier-jp7",
    "x86_64-cpu",
]

#: arm64_jp7's accepted publish-target ids after the fix — primary first,
#: then the compiled-ONNX extra (workflow_packaging.publish_targets_for_arch
#: order; the multi-id error phrase renders them in this order).
EXPECTED_JP7_ACCEPTED = ("jetson-xavier-jp7", "onnx-jetson-xavier-jp7")

#: The frontend JetPack-token inference regex (archCompatibility.ts) — the
#: oracle for Property 5's "carries a JetPack token whose major equals the
#: target's" clause.
FRONTEND_JP_TOKEN_RE = re.compile(r"(?:jp|jetpack)(4|5|6|7)(?![0-9])")

#: The Greengrass component-name charset gate — an independent copy of the
#: production regex (Property 5).
GG_NAME_ORACLE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
GG_NAME_MAX_ORACLE = 128


def jp_major_of(text):
    """The first JetPack-token major in a name (frontend inference oracle),
    or None."""
    match = FRONTEND_JP_TOKEN_RE.search(text.lower())
    return match.group(1) if match else None


# --- Fix-checking generators ----------------------------------------------

#: Safe vision model-name slugs for derived-name properties: no embedded
#: jp/jetpack-digit token (so the ONLY JetPack token in the derived name is
#: the target suffix's — the input space the frontend singleton inference is
#: specified over) and no 'vllm-' prefix (vision model-{safe} bases; the
#: model-vllm- namespace belongs to the vLLM publish path).
onnx_safe_slugs = _slugs.filter(
    lambda s: not FRONTEND_JP_TOKEN_RE.search(s)
    and not s.startswith("vllm-"))

#: Safe slugs long enough that model-{slug}-{onnx target} ALWAYS exceeds the
#: 128-character Greengrass limit: len('model-') + 110 + 1 + 22 = 139 > 128
#: for the shortest ONNX target suffix.
overlong_safe_slugs = st.text(alphabet=_ALNUM, min_size=110,
                              max_size=130).filter(
    lambda s: not FRONTEND_JP_TOKEN_RE.search(s)
    and not s.startswith("vllm-"))

#: model_graph stage types (the per-stage artifact dir inside the ZIP).
stage_types = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_",
                      min_size=3, max_size=24).filter(
    lambda s: not s.startswith("_") and not s.endswith("_"))

#: .onnx artifact filenames inside the export archive.
onnx_filenames = _slugs.map(lambda s: f"{s}.onnx")

#: Dataset image dimensions (config.yaml dataset block).
image_dims = st.integers(min_value=32, max_value=2048)

#: Absent (None → the fixed default) or explicit BYO target lists.
maybe_byo_lists = st.none() | byo_explicit_lists


def seed_compiled_onnx_record(env, stage_type, onnx_filename, width, height):
    """A trained vision record with a completed torch.onnx.export
    compilation entry whose S3 artifacts carry the GENERATED shape: the
    trained model.tar.gz (config.yaml dims + export_artifacts manifest with
    the generated stage type) and the export model.tar.gz (the generated
    .onnx filename)."""
    training_id = str(uuid.uuid4())
    model_graph = {
        "stages": [
            {"type": stage_type, "input_shape": [1, 3, height, width]}
        ]
    }
    config_yaml = (
        f"dataset:\n"
        f"  image_width: {width}\n"
        f"  image_height: {height}\n"
    ).encode("utf-8")
    trained_manifest = {
        "model_graph": model_graph,
        "input_shape": [1, 3, height, width],
    }
    trained_uri = _put_s3(
        env.s3, f"props/onnx/{training_id}/trained/model.tar.gz",
        _tar_bytes({
            "config.yaml": config_yaml,
            "export_artifacts/manifest.json":
                json.dumps(trained_manifest).encode("utf-8"),
            "export_artifacts/model.pt": b"pt-weights-placeholder",
        }))
    export_uri = _put_s3(
        env.s3, f"props/onnx/{training_id}/export/model.tar.gz",
        _tar_bytes({onnx_filename: b"onnx-protobuf-placeholder"}))
    env.training_jobs.put_item(Item={
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": "compiled-onnx-model",
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
    })
    return training_id, model_graph


# ===========================================================================
# Property 3 — ONNX target maps are total and compose fail-closed (task 4.1)
# ===========================================================================

@settings(deadline=None)
@given(component_name=component_base_names,
       onnx_target=st.sampled_from(EXPECTED_ONNX_TARGETS),
       unknown_target=unknown_targets)
def test_property_3_onnx_target_maps_total_and_fail_closed(
        props_env, component_name, onnx_target, unknown_target):
    """**Property 3: Fix Checking — ONNX target maps are total and compose
    fail-closed.**

    For every value of `packaging.ONNX_ARCH_TO_TARGET`: the id is a key of
    BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`,
    `resolve_target_platform` returns `aarch64` without raising, and
    `resolve_local_server_component` returns the `arm64JP{N}` variant whose
    JetPack major equals the id's JetPack token. For any generated unmapped
    target: `resolve_target_platform` raises `PublishError`, and an
    end-to-end publish records it failed with NO `create_component_version`
    call (while a mapped ONNX target in the same publish succeeds). The
    vllm-multi-arch-publish-conflict map-totality discipline (every value
    of `packaging.VLLM_ARCH_TO_TARGET` in both maps) holds with the ONNX
    entries present.

    # Validates: Requirements 2.2, 2.3, 3.2
    """
    packaging = props_env.packaging
    publish_module = props_env.publish

    # The fixed vocabulary is exactly the designed one (independent oracle).
    assert sorted(packaging.ONNX_ARCH_TO_TARGET.values()) == \
        EXPECTED_ONNX_TARGETS
    assert list(packaging.ONNX_COMPILED_TARGETS) == \
        list(packaging.ONNX_ARCH_TO_TARGET.values())

    # Totality + resolution for every ONNX id.
    for target in packaging.ONNX_ARCH_TO_TARGET.values():
        assert target in publish_module.TARGET_TO_LOCAL_SERVER, (
            f"{target} missing from TARGET_TO_LOCAL_SERVER")
        assert target in publish_module.TARGET_TO_PLATFORM, (
            f"{target} missing from TARGET_TO_PLATFORM")
        platform = publish_module.resolve_target_platform(target)
        assert platform == "aarch64", (
            f"{target} must resolve to platform aarch64, got {platform!r}")
        local_server = publish_module.resolve_local_server_component(
            target, platform)
        assert local_server == EXPECTED_ONNX_LOCAL_SERVER[target]
        # The variant's JetPack major equals the target id's JetPack token.
        assert jp_major_of(local_server) == jp_major_of(target), (
            f"{target} (jp{jp_major_of(target)}) resolved to LocalServer "
            f"{local_server} (jp{jp_major_of(local_server)})")

    # vLLM map-totality discipline holds with the ONNX entries present.
    for target in packaging.VLLM_ARCH_TO_TARGET.values():
        assert target in publish_module.TARGET_TO_LOCAL_SERVER, (
            f"vLLM target {target} missing from TARGET_TO_LOCAL_SERVER")
        assert target in publish_module.TARGET_TO_PLATFORM, (
            f"vLLM target {target} missing from TARGET_TO_PLATFORM")

    # An unmapped target still fails closed in resolve_target_platform.
    with pytest.raises(publish_module.PublishError) as excinfo:
        publish_module.resolve_target_platform(unknown_target)
    assert "Unsupported compile target" in str(excinfo.value)

    # End-to-end composition: one mapped ONNX target publishes; the
    # unmapped target is recorded failed with NO create for it.
    training_id = seed_packaged_record(
        props_env, [onnx_target, unknown_target])
    gg = FakeGreengrass()
    with pytest.MonkeyPatch.context() as mp:
        status, body = publish(props_env, training_id, component_name, gg, mp)

    assert status == 200, body
    entries = {entry["target"]: entry
               for entry in body["published_components"]}

    published = entries[onnx_target]
    assert published["status"] == "published"
    assert published["platform"] == "aarch64"
    expected_name = f"{component_name}-{onnx_target}"
    assert published["component_name"] == expected_name
    assert (expected_name, "1.0.0") in gg.attempts

    failed = entries[unknown_target]
    assert failed["status"] == "failed"
    assert "Unsupported compile target" in failed["error"]
    failed_name = f"{component_name}-{unknown_target.replace('_', '-')}"
    assert failed_name not in {name for name, _ in gg.attempts}, (
        f"an unmapped target must never reach create_component_version: "
        f"{gg.attempts}")


# ===========================================================================
# Property 4 — compiled-ONNX fan-out/manifest; BYO default gains JP7
# (task 4.2)
# ===========================================================================

@settings(deadline=None)
@given(stage_type=stage_types,
       onnx_filename=onnx_filenames,
       width=image_dims,
       height=image_dims,
       byo_targets=maybe_byo_lists)
def test_property_4_onnx_fanout_manifest_and_byo_default(
        props_env, stage_type, onnx_filename, width, height, byo_targets):
    """**Property 4: Fix Checking — compiled-ONNX fan-out and manifest; BYO
    default gains JP7.**

    For any generated artifact/manifest shape (stage type, .onnx filename,
    dataset dims): `package_components` fans the ONE uploaded artifact out
    into exactly the ONNX_COMPILED_TARGETS entry set (no `target: 'onnx'`
    entry, one shared component ZIP); `manifest.json` sits at the ZIP root
    with `runtime: 'onnx'`, `runtime_artifact` naming the .onnx file, the
    Phase 1 `model_graph` and `dataset` blocks, and NO `compilable_models`;
    the .onnx artifact sits at `<stage_type>/<filename>`. For any generated
    absent/explicit BYO target list: the DEFAULT is exactly
    ['jetson-xavier-jp5', 'jetson-xavier-jp6', 'jetson-xavier-jp7',
    'x86_64-cpu']; an explicit list is honored verbatim.

    # Validates: Requirements 2.6, 2.7, 2.8, 3.5
    """
    # --- compiled-ONNX fan-out over the generated shape -------------------
    training_id, model_graph = seed_compiled_onnx_record(
        props_env, stage_type, onnx_filename, width, height)

    status, body = package(props_env, training_id)

    assert status == 200, f"compiled-ONNX packaging failed: {body}"
    entries = body["packaged_components"]
    targets = sorted(e["target"] for e in entries)
    assert targets == EXPECTED_ONNX_TARGETS, (
        f"fan-out must produce exactly the per-JetPack ONNX entry set "
        f"{EXPECTED_ONNX_TARGETS} (no 'onnx' entry); got {targets}")
    assert all(e["status"] == "packaged" for e in entries), entries

    uris = {e["component_package_s3"] for e in entries}
    assert len(uris) == 1, (
        f"the fan-out shares ONE uploaded artifact; got {sorted(uris)}")

    names, manifest = read_component_zip(props_env, next(iter(uris)))
    assert set(names) == {"manifest.json", f"{stage_type}/{onnx_filename}"}, (
        f"ZIP must hold manifest.json at the root and the .onnx at "
        f"<stage_type>/<filename>; got {sorted(names)}")
    assert manifest == {
        "model_graph": model_graph,
        "dataset": {"image_width": width, "image_height": height},
        "runtime": "onnx",
        "runtime_artifact": onnx_filename,
    }, f"Compiled_ONNX_Manifest changed: {manifest}"
    assert "compilable_models" not in manifest

    # --- BYO default / explicit lists --------------------------------------
    byo_training_id = seed_byo_record(props_env)
    status, body = package(props_env, byo_training_id, targets=byo_targets)

    assert status == 200, f"BYO packaging failed: {body}"
    byo_packaged = [e["target"] for e in body["packaged_components"]]
    if byo_targets is None:
        assert byo_packaged == EXPECTED_BYO_DEFAULT, (
            f"the defaulted BYO list must be exactly {EXPECTED_BYO_DEFAULT}; "
            f"got {byo_packaged}")
    else:
        assert byo_packaged == list(byo_targets), (
            f"an explicit BYO list must be honored verbatim; requested "
            f"{byo_targets}, packaged {byo_packaged}")


# ===========================================================================
# Property 5 — derived ONNX component names satisfy every gate (task 4.3)
# ===========================================================================

@settings(deadline=None)
@given(safe_slug=onnx_safe_slugs,
       overlong_slug=overlong_safe_slugs)
def test_property_5_derived_onnx_names_satisfy_every_gate(
        props_env, safe_slug, overlong_slug):
    """**Property 5: Fix Checking — derived ONNX component names satisfy
    every gate.**

    For any safe vision model name and any ONNX target, the derived
    per-target name equals `{base}-onnx-jetson-xavier-jp{N}` (the suffix
    transform is the identity — no underscores in the ONNX ids), starts
    with `model-`, matches the Greengrass charset `^[a-zA-Z0-9._-]+$`,
    passes `validate_greengrass_component_name` without raising, carries a
    JetPack token (frontend regex `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/`)
    whose major equals the target's, and does NOT start with `model-vllm-`
    (generated bases are vision `model-{safe}` names: no `vllm-` prefix and
    no embedded JetPack token — the constrained input space the design
    names). A base long enough that every derived name exceeds 128
    characters fails closed PER TARGET: each entry is recorded failed with
    the `PublishError` length message and NO `create_component_version`
    call happens.

    # Validates: Requirements 2.1, 2.4
    """
    module = props_env.publish
    base = f"model-{safe_slug}"

    for target in EXPECTED_ONNX_TARGETS:
        derived = f"{base}-{target.replace('_', '-')}"
        # Suffix transform is the identity for the ONNX ids.
        assert derived == f"{base}-{target}"
        jp_major = target[-1]
        assert derived == f"{base}-onnx-jetson-xavier-jp{jp_major}"
        assert derived.startswith("model-")
        assert not derived.startswith("model-vllm-")
        assert GG_NAME_ORACLE_RE.match(derived), (
            f"derived name outside the Greengrass charset: {derived!r}")
        assert len(derived) <= GG_NAME_MAX_ORACLE
        # The production gate accepts it.
        module.validate_greengrass_component_name(derived)
        # The frontend JetPack-token inference sees exactly the target's
        # major.
        assert jp_major_of(derived) == jp_major, (
            f"{derived} carries JetPack token "
            f"jp{jp_major_of(derived)}, target is jp{jp_major}")

    # Over-long names fail closed per target with PublishError and no
    # create.
    long_base = f"model-{overlong_slug}"
    for target in EXPECTED_ONNX_TARGETS:
        assert len(f"{long_base}-{target}") > GG_NAME_MAX_ORACLE
    training_id = seed_packaged_record(
        props_env, list(EXPECTED_ONNX_TARGETS))
    gg = FakeGreengrass()
    with pytest.MonkeyPatch.context() as mp:
        status, body = publish(props_env, training_id, long_base, gg, mp)

    assert status == 200, body
    entries = {entry["target"]: entry
               for entry in body["published_components"]}
    for target in EXPECTED_ONNX_TARGETS:
        entry = entries[target]
        assert entry["status"] == "failed", (
            f"an over-long derived name must fail closed for {target}: "
            f"{entry}")
        assert "exceeds the Greengrass limit" in entry["error"], (
            f"expected the PublishError length message: {entry['error']!r}")
    assert gg.attempts == [], (
        f"no create_component_version may happen for over-long names: "
        f"{gg.attempts}")


# ===========================================================================
# Property 6 — arm64_jp7 workflow resolution is exact and stays fail-closed
# (task 4.4)
# ===========================================================================

@settings(deadline=None)
@given(published_targets=published_target_sets,
       extra_archs=non_jp7_arch_selections)
def test_property_6_arm64_jp7_resolution_exact_and_fail_closed(
        props_env, published_targets, extra_archs):
    """**Property 6: Fix Checking — arm64_jp7 workflow resolution is exact
    and stays fail-closed.**

    For any generated published shape and architecture selection including
    `arm64_jp7`: a published `onnx-jetson-xavier-jp7` OR `jetson-xavier-jp7`
    entry covers `arm64_jp7` and its component name resolves (names are the
    entries whose target is in the union of the selected archs' accepted
    ids); with NO JP7-accepted published entry, resolution raises
    `PackagingError` naming the model and
    `arm64_jp7 (targets jetson-xavier-jp7 or onnx-jetson-xavier-jp7)`. For
    every non-JP7 arch the accepted set is exactly today's singleton —
    `onnx-jetson-xavier-jp5/-jp6` entries never satisfy
    `arm64_jp5`/`arm64_jp6` coverage (their uncovered message keeps the
    singleton `(target …)` phrase).

    # Validates: Requirements 2.9, 2.10, 3.7
    """
    workflow = props_env.workflow

    # The accepted-target structure itself: singletons for non-JP7 archs,
    # the exact ordered pair for arm64_jp7, () for an unknown arch.
    for arch, target in BASELINE_NON_JP7_ARCH_TO_PUBLISH_TARGET.items():
        assert workflow.publish_targets_for_arch(arch) == (target,), (
            f"{arch} must keep exactly today's singleton acceptance")
    assert workflow.publish_targets_for_arch("arm64_jp7") == \
        EXPECTED_JP7_ACCEPTED
    assert workflow.publish_targets_for_arch("no-such-arch") == ()

    # End-to-end resolution with arm64_jp7 always selected.
    archs = ["arm64_jp7"] + list(extra_archs)
    model_name = "jp7-vision-model"
    usecase_id = f"uc-{uuid.uuid4()}"  # fresh per example: GSI isolation
    name_of_target = {t: f"model-jp7-{t}" for t in published_targets}
    seed_registry_record(
        props_env, usecase_id, model_name,
        [published_entry(name_of_target[t], t) for t in published_targets])

    # Independent oracle for the accepted sets.
    accepted_of_arch = {"arm64_jp7": EXPECTED_JP7_ACCEPTED}
    for arch in extra_archs:
        accepted_of_arch[arch] = (
            BASELINE_NON_JP7_ARCH_TO_PUBLISH_TARGET[arch],)
    published_set = set(published_targets)
    uncovered = sorted(
        arch for arch, accepted in accepted_of_arch.items()
        if not any(t in published_set for t in accepted))

    if uncovered:
        def _phrase(accepted):
            if len(accepted) == 1:
                return f"target {accepted[0]}"
            return f"targets {' or '.join(accepted)}"
        expected_message = (
            f"Model '{model_name}' has no published Greengrass component "
            f"for the selected architecture(s) "
            f"{', '.join(f'{a} ({_phrase(accepted_of_arch[a])})' for a in uncovered)}; "
            f"it is published for targets "
            f"[{', '.join(sorted(published_set))}]. "
            f"Publish the model for every selected architecture "
            f"before packaging workflows that use it")
        with pytest.raises(workflow.PackagingError) as excinfo:
            workflow.resolve_model_components(
                [model_name], usecase_id, archs)
        assert excinfo.value.artifact == f"models/{model_name}"
        assert str(excinfo.value) == expected_message, (
            f"uncovered-architecture error text mismatch:\n"
            f"  got:      {str(excinfo.value)!r}\n"
            f"  expected: {expected_message!r}")
        if "arm64_jp7" in uncovered:
            assert ("arm64_jp7 (targets jetson-xavier-jp7 or "
                    "onnx-jetson-xavier-jp7)") in str(excinfo.value)
    else:
        resolved = workflow.resolve_model_components(
            [model_name], usecase_id, archs)
        accepted_union = {t for accepted in accepted_of_arch.values()
                          for t in accepted}
        expected_names = {name_of_target[t] for t in published_targets
                          if t in accepted_union}
        assert resolved == {model_name: expected_names}, (
            f"resolution mismatch: {resolved!r} != "
            f"{{{model_name!r}: {expected_names!r}}}")
        # The JP7 coverage came from a JP7-accepted entry, and its name is
        # among the resolved names.
        jp7_hits = published_set & set(EXPECTED_JP7_ACCEPTED)
        assert jp7_hits, "covered case must hold a JP7-accepted entry"
        for target in jp7_hits:
            assert name_of_target[target] in resolved[model_name]


# ===========================================================================
# Property 1 — full pipeline: package' → publish' (task 4.7)
# ===========================================================================

@settings(deadline=None)
@given(safe_slug=onnx_safe_slugs,
       stage_type=stage_types,
       onnx_filename=onnx_filenames)
def test_property_1_full_pipeline_package_then_publish(
        props_env, safe_slug, stage_type, onnx_filename):
    """**Property 1: Fix Checking — a compiled ONNX artifact reaches
    per-JetPack Jetson components, end to end.**

    For any generated model name and stage type: package' emits one
    packaged entry per per-JetPack ONNX target (no `target: 'onnx'` entry)
    with a `runtime: 'onnx'` manifest at the ZIP root, and publish' then
    creates THREE distinct components `{base}-onnx-jetson-xavier-jp{N}`,
    each recipe manifest platform `aarch64` with a HARD
    `ComponentDependencies` entry on exactly that JetPack's
    `aws.edgeml.dda.LocalServer.arm64JP{N}` variant, and writes one
    per-target `published_components` entry with `status: 'published'`.

    # Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.6, 2.7
    """
    training_id, _model_graph = seed_compiled_onnx_record(
        props_env, stage_type, onnx_filename, 640, 640)

    # package': per-JetPack fan-out with runtime: onnx manifests.
    status, body = package(props_env, training_id)
    assert status == 200, f"packaging failed: {body}"
    entries = body["packaged_components"]
    assert sorted(e["target"] for e in entries) == EXPECTED_ONNX_TARGETS, (
        f"expected exactly the per-JetPack packaged entry set; got "
        f"{[e['target'] for e in entries]}")
    for entry in entries:
        assert entry["status"] == "packaged", entry
        _names, manifest = read_component_zip(
            props_env, entry["component_package_s3"])
        assert manifest is not None
        assert manifest.get("runtime") == "onnx", (
            f"{entry['target']} manifest runtime="
            f"{manifest.get('runtime')!r}")
        assert manifest.get("runtime_artifact") == onnx_filename

    # publish': three distinct per-JetPack components with the matching
    # HARD LocalServer dependency and per-target write-back.
    base = f"model-{safe_slug}"
    gg = FakeGreengrass()
    with pytest.MonkeyPatch.context() as mp:
        status, body = publish(props_env, training_id, base, gg, mp)

    assert status == 200, f"publish failed: {body}"
    published = {entry["target"]: entry
                 for entry in body["published_components"]}
    assert sorted(published) == EXPECTED_ONNX_TARGETS

    expected_names = {f"{base}-{t}" for t in EXPECTED_ONNX_TARGETS}
    assert len(expected_names) == 3
    created_names = {name for name, _ in gg.attempts}
    assert created_names == expected_names, (
        f"expected one create per per-JetPack component "
        f"{sorted(expected_names)}; got {sorted(created_names)}")

    for target in EXPECTED_ONNX_TARGETS:
        entry = published[target]
        assert entry["status"] == "published", entry
        assert entry["platform"] == "aarch64"
        name = f"{base}-{target}"
        assert entry["component_name"] == name
        recipe = gg.recipe_for(name)
        assert recipe is not None, f"no recipe created for {name}"
        assert recipe["Manifests"][0]["Platform"]["architecture"] == \
            "aarch64"
        local_server = EXPECTED_ONNX_LOCAL_SERVER[target]
        assert local_server in recipe["ComponentDependencies"], (
            f"{name} must depend on {local_server}; got "
            f"{sorted(recipe['ComponentDependencies'])}")
        assert recipe["ComponentDependencies"][local_server][
            "DependencyType"] == "HARD"
