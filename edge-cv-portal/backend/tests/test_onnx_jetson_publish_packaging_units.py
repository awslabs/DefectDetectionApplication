# -*- coding: utf-8 -*-
"""Unit tests (task 5.1) for onnx-jetson-publish-packaging.

Design "Testing Strategy > Unit Tests" coverage, example-based (the
Hypothesis property suites live in
test_onnx_jetson_publish_packaging_properties.py — do not duplicate them):

- `ONNX_ARCH_TO_TARGET` / `ONNX_COMPILED_TARGETS` vocabulary: closed set,
  the ids carry the `onnx` token (so derived component names carry
  `-onnx-`) and a JetPack token matching their arch, and the publish-side
  Target_Suffix transform (`target.replace('_', '-')`) is the identity for
  every id.
- Publish map matrix extension (the spirit of
  test_greengrass_publish_localserver.py): the three ONNX ids are keys of
  BOTH TARGET_TO_LOCAL_SERVER and TARGET_TO_PLATFORM, resolve to the
  JP5/JP6/JP7 LocalServer variants, and resolve platform `aarch64`.
- `package_compiled_onnx_component`: manifest fields (`runtime: 'onnx'`,
  `runtime_artifact`, `model_graph`, `dataset`, NO `compilable_models`),
  ZIP layout (`manifest.json` at the root, artifact at
  `<stage_type>/<file>`), recursive-scan tolerance for a nested `.onnx`,
  and error propagation (missing `.onnx`, failed download).
- Fan-out through `package_components`: exactly the three-entry
  `ONNX_COMPILED_TARGETS` set from ONE upload; per-target `failed` entries
  on packaging failure; mixed Neo+ONNX completed jobs leave the Neo entry
  (and its runtime-LESS Neo ZIP layout) unchanged alongside the fan-out;
  `requested_targets=['onnx']` (the compile-target id the UI knows)
  expands into the per-JetPack set.
- `publish_targets_for_arch`: singleton tuples for every non-JP7 arch, the
  `(jetson-xavier-jp7, onnx-jetson-xavier-jp7)` pair for arm64_jp7, and
  `()` for an unknown arch.
- `resolve_model_components`: arm64_jp7 coverage via EITHER accepted JP7
  id; resolved names drawn from the union of accepted ids; the singleton
  uncovered-architecture error text byte-identical to today's, and the JP7
  multi-id text rendering `(targets jetson-xavier-jp7 or
  onnx-jetson-xavier-jp7)`.

Harness: the moto-backed conftest `aws_stack` fixture plus this module's
OWN training-jobs / models tables and bucket (isolated from the sibling
suites), following test_onnx_jetson_publish_packaging_exploration.py.

Run (needs the tests-directory conftest — no `--noconftest`), from
edge-cv-portal/backend/tests with the /home/ubuntu/.venvs/dda-portal-tests
venv:
    python3 -m pytest test_onnx_jetson_publish_packaging_units.py \
      -q -p no:cacheprovider

**Validates: Requirements 2.1, 2.2, 2.6, 2.7, 2.9, 3.1**
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

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-jetson-units"
MODELS_TABLE_NAME = "test-models-onnx-jetson-units"

ACCOUNT_ID = "123456789012"
BUCKET = "test-onnx-jetson-units-bucket"

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.abspath(os.path.join(_HERE, "..", "functions"))
_PACKAGING_PATH = os.path.join(_FUNCTIONS, "packaging.py")
_PUBLISH_PATH = os.path.join(_FUNCTIONS, "greengrass_publish.py")

#: Independent oracles — restated, not read back from the modules under
#: test, so the assertions cannot silently drift with a behavior change.
EXPECTED_ONNX_ARCH_TO_TARGET = {
    "arm64_jp5": "onnx-jetson-xavier-jp5",
    "arm64_jp6": "onnx-jetson-xavier-jp6",
    "arm64_jp7": "onnx-jetson-xavier-jp7",
}
ONNX_TARGETS = sorted(EXPECTED_ONNX_ARCH_TO_TARGET.values())

LOCAL_SERVER_FOR_TARGET = {
    "onnx-jetson-xavier-jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "onnx-jetson-xavier-jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "onnx-jetson-xavier-jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
}

#: Today's singleton publish-target map (workflow_packaging
#: ARCH_TO_PUBLISH_TARGET), restated.
EXPECTED_PRIMARY_TARGET = {
    "arm64_jp4": "jetson-xavier",
    "arm64_jp5": "jetson-xavier-jp5",
    "arm64_jp6": "jetson-xavier-jp6",
    "arm64_jp7": "jetson-xavier-jp7",
    "x86_64": "x86_64-cpu",
    "x86_64_nvidia": "x86_64-cuda",
}

#: The exact uncovered-architecture message resolve_model_components
#: renders (observed on the fixed tree; singleton arch rendering is
#: byte-identical to the pre-fix tree).
UNCOVERED_MESSAGE = (
    "Model '{name}' has no published Greengrass component for the selected "
    "architecture(s) {rendered}; it is published for targets "
    "[{published}]. Publish the model for every selected architecture "
    "before packaging workflows that use it")

STAGE_TYPE = "yolo_object_detection"
MODEL_NAME = "yolo-test"


def _load_module(path, name):
    """Load a functions/*.py module under a distinct module name (inside
    the moto mock, so module-level boto3 clients bind to the fake AWS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic artifacts (real archives so the packagers run for real)
# ---------------------------------------------------------------------------

def _tar_bytes(members):
    """A gzipped tar archive from {relative_path: bytes}."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relpath, data in members.items():
            info = tarfile.TarInfo(name=relpath)
            info.size = len(data)
            # Post-1980 mtime: extracted files keep it and the Phase 2
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
    """The trained-model `model.tar.gz` create_dda_manifest expects."""
    manifest = {
        "model_graph": _MODEL_GRAPH,
        "input_shape": [1, 3, 640, 640],
    }
    return _tar_bytes({
        "config.yaml": _CONFIG_YAML,
        "export_artifacts/manifest.json": json.dumps(manifest).encode("utf-8"),
        "export_artifacts/model.pt": b"pt-weights-placeholder",
    })


def onnx_export_tar(members=None):
    """The torch.onnx.export job's model.tar.gz (default: model.onnx at
    the archive root, the shape the export script writes)."""
    return _tar_bytes(members if members is not None
                      else {"model.onnx": b"onnx-protobuf-placeholder"})


def neo_compiled_tar():
    """A Neo compilation output archive (opaque compiled artifacts)."""
    return _tar_bytes({
        "compiled.params": b"neo-params-placeholder",
        "compiled.so": b"neo-lib-placeholder",
    })


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def units_env(aws_stack):
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
                             "portal_packaging_onnx_jetson_units")
    publish = _load_module(_PUBLISH_PATH,
                           "portal_gg_publish_onnx_jetson_units")

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
def seeded(units_env):
    """Fresh Use_Case (single-account, so get_usecase_client returns the
    moto-bound default clients) + DataScientist."""
    usecase_id = f"uc-{uuid.uuid4()}"
    units_env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Jetson Units Use Case",
        "account_id": ACCOUNT_ID,
        "s3_bucket": BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    units_env.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })
    return SimpleNamespace(usecase_id=usecase_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Seeding / invocation helpers
# ---------------------------------------------------------------------------

def _put_s3(units_env, key, data):
    units_env.s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    return f"s3://{BUCKET}/{key}"


def seed_record(units_env, seeded, compilation_jobs, model_name=MODEL_NAME):
    """A trained-model training-jobs record with the given compilation
    entries and a REAL trained artifact in S3."""
    training_id = str(uuid.uuid4())
    trained_uri = _put_s3(units_env, f"training/{training_id}/model.tar.gz",
                          trained_artifact_tar())
    item = {
        "training_id": training_id,
        "usecase_id": seeded.usecase_id,
        "model_name": model_name,
        "model_type": "object_detection",
        "status": "Completed",
        "artifact_s3": trained_uri,
        "compilation_jobs": compilation_jobs,
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    units_env.training_jobs.put_item(Item=item)
    return item


def onnx_job(units_env, training_id_hint, tar_data=None):
    """One completed torch.onnx.export compilation entry + its S3 tar."""
    export_uri = _put_s3(
        units_env, f"export/{training_id_hint}/model.tar.gz",
        onnx_export_tar() if tar_data is None else tar_data)
    return {
        "target": "onnx",
        "export_format": "onnx",
        "status": "Completed",
        "compiled_model_s3": export_uri,
    }


def neo_job(units_env, training_id_hint, target="jetson-xavier-jp5"):
    """One completed Neo compilation entry + its S3 tar."""
    neo_uri = _put_s3(
        units_env, f"neo/{training_id_hint}/{target}/model.tar.gz",
        neo_compiled_tar())
    return {
        "target": target,
        "status": "Completed",
        "compiled_model_s3": neo_uri,
    }


def package(units_env, seeded, record, targets=None):
    body = {} if targets is None else {"targets": targets}
    event = {
        "httpMethod": "POST",
        "path": f"/api/v1/training/{record['training_id']}/package",
        "pathParameters": {"id": record["training_id"]},
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": seeded.user_id,
                    "email": f"{seeded.user_id}@example.com",
                    "cognito:username": seeded.user_id,
                }
            }
        },
    }
    response = units_env.packaging.package_components(event, None)
    return response["statusCode"], json.loads(response["body"])


def read_component_zip(units_env, component_package_s3):
    """Download a packaged component ZIP from moto S3 and return
    (namelist, manifest_dict_or_None)."""
    assert component_package_s3.startswith("s3://"), component_package_s3
    bucket, key = component_package_s3[len("s3://"):].split("/", 1)
    with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
        units_env.s3.download_file(bucket, key, handle.name)
        with zipfile.ZipFile(handle.name) as zipf:
            names = zipf.namelist()
            manifest = None
            if "manifest.json" in names:
                manifest = json.loads(zipf.read("manifest.json"))
            return names, manifest


#: The Phase 1 create_dda_manifest-shaped input for direct
#: package_compiled_onnx_component calls — carries compilable_models so the
#: DROP is observable.
def dda_manifest_sample():
    return {
        "model_graph": json.loads(json.dumps(_MODEL_GRAPH)),
        "dataset": {"image_width": 640, "image_height": 640},
        "compilable_models": [
            {"framework": "PYTORCH", "artifact": "model.pt"}],
    }


# ===========================================================================
# 1. ONNX_ARCH_TO_TARGET / ONNX_COMPILED_TARGETS vocabulary
# ===========================================================================

class TestOnnxVocabulary:
    def test_closed_vocabulary(self, units_env):
        """The per-JetPack compiled-ONNX vocabulary is exactly the three
        arm64_jp5/jp6/jp7 → onnx-jetson-xavier-jp{N} pairs, and
        ONNX_COMPILED_TARGETS is exactly its value list.

        **Validates: Requirements 2.6**
        """
        packaging = units_env.packaging
        assert packaging.ONNX_ARCH_TO_TARGET == EXPECTED_ONNX_ARCH_TO_TARGET
        assert sorted(packaging.ONNX_COMPILED_TARGETS) == ONNX_TARGETS
        assert len(packaging.ONNX_COMPILED_TARGETS) == 3

    def test_ids_carry_onnx_and_matching_jetpack_tokens(self, units_env):
        """Every id starts with the 'onnx-' token (so the derived component
        name `{base}-{target}` carries '-onnx-') and ends with the JetPack
        token of its own arch key.

        **Validates: Requirements 2.1**
        """
        for arch, target in units_env.packaging.ONNX_ARCH_TO_TARGET.items():
            assert target.startswith("onnx-"), target
            derived = f"model-x-{target}"
            assert "-onnx-" in derived, derived
            jp_token = arch.split("_")[-1]          # jp5 / jp6 / jp7
            assert target.endswith(f"-{jp_token}"), (arch, target)

    def test_target_suffix_transform_is_identity(self, units_env):
        """The publish-side Target_Suffix transform
        (`target.replace('_', '-')`) is the identity for every ONNX id (no
        underscores), so the published name is exactly
        `{base}-{target}`.

        **Validates: Requirements 2.1**
        """
        for target in units_env.packaging.ONNX_COMPILED_TARGETS:
            assert "_" not in target, target
            assert target.replace("_", "-") == target


# ===========================================================================
# 2. Publish map matrix extension (test_greengrass_publish_localserver.py
#    spirit)
# ===========================================================================

class TestPublishMapMatrix:
    def test_onnx_ids_present_in_both_maps(self, units_env):
        """Each ONNX id is a key of BOTH module-level maps (the both-maps-
        or-fail-closed discipline).

        **Validates: Requirements 2.2, 3.1**
        """
        pub = units_env.publish
        for target in ONNX_TARGETS:
            assert target in pub.TARGET_TO_LOCAL_SERVER, target
            assert target in pub.TARGET_TO_PLATFORM, target

    def test_onnx_ids_resolve_local_server_matrix(self, units_env):
        """The three ONNX ids resolve to the JP5/JP6/JP7 LocalServer
        variants (extends the resolver matrix of
        test_greengrass_publish_localserver.py).

        **Validates: Requirements 2.2**
        """
        pub = units_env.publish
        for target, expected in LOCAL_SERVER_FOR_TARGET.items():
            assert pub.resolve_local_server_component(
                target, pub.TARGET_TO_PLATFORM[target]) == expected

    def test_onnx_ids_resolve_platform_aarch64(self, units_env):
        """resolve_target_platform returns aarch64 for each ONNX id without
        raising.

        **Validates: Requirements 2.2**
        """
        pub = units_env.publish
        for target in ONNX_TARGETS:
            assert pub.resolve_target_platform(target) == "aarch64"

    def test_archless_onnx_id_still_fails_closed(self, units_env):
        """The arch-less 'onnx' compile-target id remains unmapped: it is a
        key of neither map and resolve_target_platform raises.

        **Validates: Requirements 3.1**
        """
        pub = units_env.publish
        assert "onnx" not in pub.TARGET_TO_LOCAL_SERVER
        assert "onnx" not in pub.TARGET_TO_PLATFORM
        with pytest.raises(pub.PublishError):
            pub.resolve_target_platform("onnx")


# ===========================================================================
# 3. package_compiled_onnx_component
# ===========================================================================

class TestPackageCompiledOnnxComponent:
    def _call(self, units_env, export_uri, manifest=None):
        return units_env.packaging.package_compiled_onnx_component(
            export_uri, manifest or dda_manifest_sample(),
            units_env.s3, {"s3_bucket": BUCKET})

    def test_manifest_fields_and_zip_layout(self, units_env):
        """The uploaded ZIP holds manifest.json at the root carrying exactly
        {model_graph, dataset, runtime: 'onnx', runtime_artifact} — with
        compilable_models DROPPED — and the artifact at
        <stage_type>/<onnx filename>.

        **Validates: Requirements 2.7**
        """
        export_uri = _put_s3(units_env, "unit/pkg-basic/model.tar.gz",
                             onnx_export_tar())

        s3_uri = self._call(units_env, export_uri)

        names, manifest = read_component_zip(units_env, s3_uri)
        assert manifest == {
            "model_graph": _MODEL_GRAPH,
            "dataset": {"image_width": 640, "image_height": 640},
            "runtime": "onnx",
            "runtime_artifact": "model.onnx",
        }
        assert "compilable_models" not in manifest
        assert sorted(names) == ["manifest.json",
                                 f"{STAGE_TYPE}/model.onnx"]

    def test_recursive_scan_finds_nested_onnx(self, units_env):
        """A .onnx nested in a subdirectory of the export archive is still
        found (recursive-scan tolerance) and lands at
        <stage_type>/<its basename>.

        **Validates: Requirements 2.7**
        """
        export_uri = _put_s3(
            units_env, "unit/pkg-nested/model.tar.gz",
            onnx_export_tar({
                "info.txt": b"not a model",
                "nested/deeper/custom_name.onnx": b"onnx-bytes",
            }))

        s3_uri = self._call(units_env, export_uri)

        names, manifest = read_component_zip(units_env, s3_uri)
        assert manifest["runtime"] == "onnx"
        assert manifest["runtime_artifact"] == "custom_name.onnx"
        assert f"{STAGE_TYPE}/custom_name.onnx" in names

    def test_archive_without_onnx_raises(self, units_env):
        """No .onnx anywhere in the archive → the packager raises (error
        propagation; the caller records per-target failed entries).

        **Validates: Requirements 2.7**
        """
        export_uri = _put_s3(
            units_env, "unit/pkg-no-onnx/model.tar.gz",
            onnx_export_tar({"model.pt": b"wrong artifact"}))

        with pytest.raises(FileNotFoundError, match=r"No \.onnx model file"):
            self._call(units_env, export_uri)

    def test_download_failure_propagates(self, units_env):
        """A missing S3 object propagates the client error (no swallowed
        failures).

        **Validates: Requirements 2.7**
        """
        with pytest.raises(Exception):
            self._call(units_env,
                       f"s3://{BUCKET}/unit/does-not-exist/model.tar.gz")


# ===========================================================================
# 4. Fan-out through package_components
# ===========================================================================

class TestCompiledOnnxFanOut:
    def test_three_entry_set_from_one_upload(self, units_env, seeded):
        """One completed onnx export entry fans out into exactly the three
        per-JetPack packaged entries, all pointing at the SAME uploaded
        component package (one upload).

        **Validates: Requirements 2.6**
        """
        record = seed_record(units_env, seeded,
                             [onnx_job(units_env, "fanout-basic")])

        status, body = package(units_env, seeded, record)

        assert status == 200, body
        entries = body["packaged_components"]
        packaged = [e for e in entries if e.get("status") == "packaged"]
        assert sorted(e["target"] for e in packaged) == ONNX_TARGETS
        uris = {e["component_package_s3"] for e in packaged}
        assert len(uris) == 1, (
            f"expected ONE upload shared by all three entries, got {uris}")

    def test_packaging_failure_records_per_target_failed_entries(
            self, units_env, seeded):
        """A compiled-ONNX packaging failure (no .onnx in the export
        archive) records one failed entry per per-JetPack target, mirroring
        the Neo failure shape.

        **Validates: Requirements 2.6**
        """
        bad_tar = onnx_export_tar({"model.pt": b"wrong artifact"})
        record = seed_record(
            units_env, seeded,
            [onnx_job(units_env, "fanout-failure", tar_data=bad_tar)])

        status, body = package(units_env, seeded, record)

        assert status == 200, body
        entries = body["packaged_components"]
        failed = [e for e in entries if e.get("status") == "failed"]
        assert sorted(e["target"] for e in failed) == ONNX_TARGETS
        for entry in failed:
            assert entry.get("error"), entry
            assert "component_package_s3" not in entry

    def test_mixed_neo_and_onnx_jobs_leave_neo_entry_unchanged(
            self, units_env, seeded):
        """Mixed completed jobs (Neo jetson-xavier-jp5 + onnx export): the
        Neo entry is packaged through the unchanged Phase 2 loop (its own
        target id, its own ZIP with the runtime-LESS create_dda_manifest
        manifest and the Neo layout) alongside the three-entry ONNX
        fan-out.

        **Validates: Requirements 2.6**
        """
        record = seed_record(units_env, seeded, [
            neo_job(units_env, "mixed", target="jetson-xavier-jp5"),
            onnx_job(units_env, "mixed"),
        ])

        status, body = package(units_env, seeded, record)

        assert status == 200, body
        packaged = [e for e in body["packaged_components"]
                    if e.get("status") == "packaged"]
        targets = sorted(e["target"] for e in packaged)
        assert targets == sorted(["jetson-xavier-jp5"] + ONNX_TARGETS)

        neo_entry = next(e for e in packaged
                         if e["target"] == "jetson-xavier-jp5")
        names, manifest = read_component_zip(
            units_env, neo_entry["component_package_s3"])
        assert manifest is not None
        assert "runtime" not in manifest, (
            f"the Neo manifest must stay runtime-less (the DLR default is "
            f"correct for Neo artifacts); keys={sorted(manifest)}")
        assert "model_graph" in manifest
        assert f"{STAGE_TYPE}/compiled.so" in names
        assert f"{STAGE_TYPE}/compiled.params" in names

        onnx_entries = [e for e in packaged
                        if e["target"] in ONNX_TARGETS]
        assert len(onnx_entries) == 3
        for entry in onnx_entries:
            _names, onnx_manifest = read_component_zip(
                units_env, entry["component_package_s3"])
            assert onnx_manifest.get("runtime") == "onnx"

    def test_requested_targets_onnx_expands_to_per_jetpack_set(
            self, units_env, seeded):
        """requested_targets=['onnx'] (the compile-target id the UI knows)
        keeps filtering on the export entry and the fan-out expands it into
        exactly the per-JetPack set — the Neo job is filtered out.

        **Validates: Requirements 2.6**
        """
        record = seed_record(units_env, seeded, [
            neo_job(units_env, "filter", target="jetson-xavier-jp5"),
            onnx_job(units_env, "filter"),
        ])

        status, body = package(units_env, seeded, record, targets=["onnx"])

        assert status == 200, body
        packaged = [e for e in body["packaged_components"]
                    if e.get("status") == "packaged"]
        assert sorted(e["target"] for e in packaged) == ONNX_TARGETS


# ===========================================================================
# 5. publish_targets_for_arch
# ===========================================================================

class TestPublishTargetsForArch:
    def test_non_jp7_archs_are_singletons(self, units_env):
        """Every non-JP7 arch keeps its exact singleton accepted-target
        tuple (Neo stays the JP5/JP6 vision route).

        **Validates: Requirements 2.9, 3.1**
        """
        workflow = units_env.workflow
        for arch, primary in EXPECTED_PRIMARY_TARGET.items():
            if arch == "arm64_jp7":
                continue
            assert workflow.publish_targets_for_arch(arch) == (primary,), arch

    def test_arm64_jp7_is_the_jp7_pair(self, units_env):
        """arm64_jp7 accepts exactly (jetson-xavier-jp7,
        onnx-jetson-xavier-jp7) — primary first, extra second.

        **Validates: Requirements 2.9**
        """
        assert units_env.workflow.publish_targets_for_arch("arm64_jp7") == (
            "jetson-xavier-jp7", "onnx-jetson-xavier-jp7")

    def test_unknown_arch_returns_empty_tuple(self, units_env):
        """An unknown arch returns () so the caller fails closed with
        today's no-known-target message.

        **Validates: Requirements 2.9, 3.1**
        """
        workflow = units_env.workflow
        assert workflow.publish_targets_for_arch("riscv64") == ()
        assert workflow.publish_targets_for_arch(None) == ()
        assert workflow.publish_targets_for_arch("") == ()


# ===========================================================================
# 6. resolve_model_components
# ===========================================================================

def _published_entry(component_name, target, version="1.0.0"):
    return {
        "component_name": component_name,
        "target": target,
        "component_version": version,
        "status": "published",
        "platform": "aarch64",
        "component_arn": (
            f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
            f"{component_name}:versions:{version}"),
    }


def _seed_registry_record(units_env, usecase_id, model_name, entries):
    units_env.training_jobs.put_item(Item={
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


class TestResolveModelComponents:
    def test_jp7_coverage_via_the_onnx_id(self, units_env,
                                          registry_usecase_id):
        """A published onnx-jetson-xavier-jp7 entry alone covers arm64_jp7
        and its name resolves.

        **Validates: Requirements 2.9**
        """
        name = "cover-onnx"
        component = "model-cover-onnx-onnx-jetson-xavier-jp7"
        _seed_registry_record(
            units_env, registry_usecase_id, name,
            [_published_entry(component, "onnx-jetson-xavier-jp7")])

        resolved = units_env.workflow.resolve_model_components(
            [name], registry_usecase_id, ["arm64_jp7"])

        assert set(resolved[name]) == {component}

    def test_jp7_coverage_via_the_neo_style_primary_id(self, units_env,
                                                       registry_usecase_id):
        """A published jetson-xavier-jp7 entry alone (the BYO ONNX import
        route) also covers arm64_jp7.

        **Validates: Requirements 2.9**
        """
        name = "cover-primary"
        component = "model-cover-primary-jetson-xavier-jp7"
        _seed_registry_record(
            units_env, registry_usecase_id, name,
            [_published_entry(component, "jetson-xavier-jp7")])

        resolved = units_env.workflow.resolve_model_components(
            [name], registry_usecase_id, ["arm64_jp7"])

        assert set(resolved[name]) == {component}

    def test_jp7_names_resolve_from_the_union_of_accepted_ids(
            self, units_env, registry_usecase_id):
        """With BOTH accepted JP7 entries published, the resolved names are
        drawn from the union of accepted ids (both names).

        **Validates: Requirements 2.9**
        """
        name = "cover-both"
        byo = "model-cover-both-jetson-xavier-jp7"
        onnx = "model-cover-both-onnx-jetson-xavier-jp7"
        _seed_registry_record(
            units_env, registry_usecase_id, name,
            [_published_entry(byo, "jetson-xavier-jp7"),
             _published_entry(onnx, "onnx-jetson-xavier-jp7"),
             _published_entry("model-cover-both-jetson-xavier-jp5",
                              "jetson-xavier-jp5")])

        resolved = units_env.workflow.resolve_model_components(
            [name], registry_usecase_id, ["arm64_jp7"])

        assert set(resolved[name]) == {byo, onnx}, (
            "names must come from the union of accepted JP7 ids only "
            "(the JP5 entry must not leak in)")

    def test_singleton_arch_error_text_byte_identical(self, units_env,
                                                      registry_usecase_id):
        """The uncovered-architecture message for a singleton arch renders
        byte-identical to today's `(target X)` form.

        **Validates: Requirements 3.1**
        """
        name = "neo-jp5-only"
        _seed_registry_record(
            units_env, registry_usecase_id, name,
            [_published_entry("model-neo-jp5-only-jetson-xavier-jp5",
                              "jetson-xavier-jp5")])

        with pytest.raises(units_env.workflow.PackagingError) as excinfo:
            units_env.workflow.resolve_model_components(
                [name], registry_usecase_id, ["arm64_jp6"])

        assert excinfo.value.message == UNCOVERED_MESSAGE.format(
            name=name,
            rendered="arm64_jp6 (target jetson-xavier-jp6)",
            published="jetson-xavier-jp5")
        assert excinfo.value.artifact == f"models/{name}"

    def test_jp7_multi_id_error_text(self, units_env, registry_usecase_id):
        """The uncovered-architecture message for arm64_jp7 renders the
        multi-id `(targets X or Y)` form naming both accepted ids.

        **Validates: Requirements 2.9, 3.1**
        """
        name = "neo-jp5-jp6"
        _seed_registry_record(
            units_env, registry_usecase_id, name,
            [_published_entry("model-neo-jp5-jp6-jetson-xavier-jp5",
                              "jetson-xavier-jp5"),
             _published_entry("model-neo-jp5-jp6-jetson-xavier-jp6",
                              "jetson-xavier-jp6")])

        with pytest.raises(units_env.workflow.PackagingError) as excinfo:
            units_env.workflow.resolve_model_components(
                [name], registry_usecase_id, ["arm64_jp7"])

        assert excinfo.value.message == UNCOVERED_MESSAGE.format(
            name=name,
            rendered=("arm64_jp7 (targets jetson-xavier-jp7 or "
                      "onnx-jetson-xavier-jp7)"),
            published="jetson-xavier-jp5, jetson-xavier-jp6")
