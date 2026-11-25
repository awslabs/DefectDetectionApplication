"""
RF-DETR-Seg feature floor: rfdetr-seg-support Requirement 14.8 (b), (c),
(e), design DD8 (spec task 14.4).

A workflow referencing a model whose Model_Registry record carries
``detection.detection_arch == 'rf_detr_seg'`` needs a LocalServer build that
carries the Seg device contract (the ``rf_detr_instance_segmentation`` stage
type, ``mask_rle`` in the Detection_List, the ``detections`` column and the
mask-forwarding marshal). The packager therefore resolves such a workflow's
floors as the MAXIMUM of the lineage floor, the stream floor and the Seg
FEATURE floor (``WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS``), rejects
any requested architecture missing from the Seg map with
``RFDETR_SEG_UNSUPPORTED_ARCH`` before any version item is written, and
records ``has_rfdetr_seg_model`` on the version item unconditionally; the
pre-submit deployment gate reads the same literal and the discriminator.

This suite is the teeth half of that mechanism (the lockstep / coverage half
is ``test_rfdetr_seg_feature_floor_coverage.py``), modelled on
``test_stream_camera_feature_floor_coverage.py`` sections 3–4 and on
``test_stream_binding_deployment.py``:

1. the pure helpers — ``rfdetr_seg_model_names`` (the discriminator),
   ``rfdetr_seg_arch_gate_findings`` (accepts exactly the map's archs,
   rejects every other one by name, empty without a Seg model), the three
   widened floor resolvers (``max(lineage, stream, seg)``, numeric ordering,
   ``PackagingError('rfdetr-seg-feature-floor/<arch>')`` for a missing
   entry), ``load_model_records`` / ``resolve_model_components(records=)``;
2. the manifest: ``build_manifest(..., rfdetr_seg=True)`` raises BOTH
   ``minLocalServerVersion`` and ``minLocalServerVersions`` (the mirror of
   ``test_stream_camera_feature_floor_coverage.py:462-489``), the map entry
   for the package's ``targetArch`` equals the scalar, a Seg map missing the
   package's arch raises;
3. every widened signature defaults ``rfdetr_seg`` to ``False`` (the
   ``test_stream_free_packaging_preservation.py:289-303`` pattern, tolerant
   of ``build_manifest``'s pre-existing keyword-only ``reference_files``) and
   ``deployments.check_local_server_compatibility`` does too;
4. the gate is WIRED into the packaging handler: a Seg-model workflow on an
   uncovered architecture returns 409 ``RFDETR_SEG_UNSUPPORTED_ARCH`` and
   registers nothing; on a covered one it packages with the raised floors
   in the manifest and the LocalServer ComponentDependencies entry and
   writes ``has_rfdetr_seg_model = True``; a workflow whose model is not a
   Seg model writes ``False`` and keeps its unraised floors;
5. the deployments matrix — function level (below the floor, at the floor,
   no entry, both features, the default) and route level (409 details
   ``rfdetr_seg_model`` / ``rfdetr_seg_min_local_server_versions`` only for
   a Seg workflow, the per-version override not stepping around the floor,
   a workflow without the discriminator unaffected).

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module imports):

    python3 -m pytest tests/test_rfdetr_seg_feature_floor.py \
        -q -p no:cacheprovider
"""
import io
import json
import os
import sys
import uuid
import zipfile
from inspect import Parameter, signature
from types import SimpleNamespace

import boto3
import pytest

from conftest import REGION
from test_camera_binding_context import BindingEnv, camera_node
# The packaging harness that drives the real handler end to end (a validated
# workflow version, a Use_Case bucket, patched Use_Case-account clients).
from test_workflow_packaging_binding_points import (
    BindingPointsEnv,
    COMPONENTS_ROOT,
)

#: The deployed Seg feature-floor env var (design DD8).
SEG_FLOOR_ENV = 'WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS'
#: The Model_Registry table this module binds workflow_packaging to (its own
#: name, so it can coexist with the other packaging suites in one moto
#: session).
TRAINING_JOBS_TABLE_NAME = 'test-rfdetr-seg-feature-floor-training-jobs'
#: Own camera-registry table for the deployments stack, likewise.
SEG_REGISTRY_TABLE = 'test-camera-registry-rfdetr-seg-floor'
#: The x86_64 LocalServer variant FakeGreengrass.register_device installs
#: (``local_server_component_arch`` resolves it); every route-level floor
#: case below is keyed on this arch.
FLEET_ARCH = 'x86_64'

SEG_MODEL_NAME = 'parts-seg'
SEG_MODEL_COMPONENT = 'model-parts-seg'
DETECTION_MODEL_NAME = 'parts-det'
DETECTION_MODEL_COMPONENT = 'model-parts-det'


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """The training-jobs Model_Registry table (production GSI shape) plus a
    freshly imported workflow_packaging bound to it inside moto — the
    ``test_workflow_packaging_recipe_preservation.py`` setup, so the gate
    has real records to read."""
    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
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
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        packaging=workflow_packaging,
        training_table=resource.Table(TRAINING_JOBS_TABLE_NAME),
    )
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    sys.modules.pop("workflow_packaging", None)


@pytest.fixture(scope="module")
def packaging(packaging_env):
    return packaging_env.packaging


@pytest.fixture(scope="module")
def seg_stack(aws_stack):
    """The deployments module bound to its own camera-registry table (the
    ``test_stream_binding_deployment.py`` module-stack pattern)."""
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=SEG_REGISTRY_TABLE,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    os.environ["CAMERA_REGISTRY_TABLE"] = SEG_REGISTRY_TABLE
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "deployments": deployments,
        "registry": resource.Table(SEG_REGISTRY_TABLE),
    }


@pytest.fixture(scope="module")
def deployments(seg_stack):
    """The module under test, for the function-level cases."""
    return seg_stack["deployments"]


@pytest.fixture
def fleet(env, seg_stack, monkeypatch):
    return BindingEnv(env, seg_stack, monkeypatch)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def seg_record(name=SEG_MODEL_NAME, component=SEG_MODEL_COMPONENT,
               detection_arch='rf_detr_seg', model_type='instance_segmentation'):
    """A published Model_Registry record in the vision ``published_components``
    shape, with the ``detection`` block a Seg training / conversion / import
    record carries (design "Portal records")."""
    return {
        "model_name": name,
        "model_type": model_type,
        "detection": {"detection_arch": detection_arch, "output_mode": "instance"},
        "published_component": None,
        "published_components": [{
            "component_name": f"{component}-jetson-xavier-jp6",
            "component_version": "1.0.0",
            "target": "jetson-xavier-jp6",
            "status": "published",
        }],
    }


def detection_record(name=DETECTION_MODEL_NAME,
                     component=DETECTION_MODEL_COMPONENT):
    """An RF-DETR detection record: the DETR family's other arch, never a
    Seg record."""
    return seg_record(name, component, detection_arch='rf_detr',
                      model_type='object_detection')


def model_definition(model_name):
    """folder_source -> model_inference -> capture: one ``model_ref``
    (``model_inference.modelName``); compiles for arm64_jp6 with no plugin
    dependencies."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "inf", "type": "model_inference", "position": {"x": 200, "y": 0},
             "parameters": {"modelName": model_name}},
            {"id": "cap", "type": "capture", "position": {"x": 400, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "inf", "port": "in"}},
            {"id": "c2", "from": {"node": "inf", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def folder_definition():
    """A model-free workflow (the pre-feature shape): folder -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "src", "type": "folder_source", "position": {"x": 0, "y": 0},
             "parameters": {"location": "/data/images"}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "src", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def seed_record(packaging_env, usecase_id, record):
    item = {"training_id": f"tr-{uuid.uuid4()}", "usecase_id": usecase_id,
            "created_at": 1, **record}
    packaging_env.training_table.put_item(Item=item)


def set_seg_floor(packaging, monkeypatch, floor_map):
    monkeypatch.setattr(packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
                        dict(floor_map))


def packaged_manifest(harness, arch):
    key = (f"{COMPONENTS_ROOT}/{harness.workflow_id}/1/1.0.0/"
           f"{arch}/workflow-{arch}.zip")
    body = harness.env.s3.get_object(
        Bucket=harness.usecase_bucket, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return json.loads(zf.read("manifest.json").decode("utf-8"))


def registered_recipe(harness):
    raw = harness.greengrass.create_component_version.call_args.kwargs[
        "inlineRecipe"]
    return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)


# --------------------------------------------------------------------------
# 1. Pure helpers
# --------------------------------------------------------------------------
class TestSegModelDiscriminator:
    def test_gate_code_is_the_spelled_contract(self, packaging):
        # Validates: Requirements 14.8
        assert packaging.GATE_RFDETR_SEG_ARCH_UNSUPPORTED == \
            'RFDETR_SEG_UNSUPPORTED_ARCH'
        assert packaging.RFDETR_SEG_DETECTION_ARCH == 'rf_detr_seg'

    def test_only_records_with_the_seg_detection_arch_count(self, packaging):
        # Validates: Requirements 14.8
        records = {
            "seg-a": seg_record("seg-a"),
            "det": detection_record(),
            "yolo": {"model_name": "yolo", "model_type": "object_detection",
                     "detection": {"detection_arch": "yolo"}},
            "lfv": {"model_name": "lfv", "model_type": "segmentation"},
            "vllm": {"model_name": "vllm", "model_type": "vllm",
                     "published_component": {"component_name": "x"}},
            "odd": {"model_name": "odd", "detection": "not-a-dict"},
            "seg-b": seg_record("seg-b"),
        }
        assert packaging.rfdetr_seg_model_names(records) == ["seg-a", "seg-b"]

    def test_no_records_means_no_seg_model(self, packaging):
        # Validates: Requirements 14.8, 17.1
        assert packaging.rfdetr_seg_model_names({}) == []
        assert packaging.rfdetr_seg_model_names(None) == []
        assert packaging.rfdetr_seg_model_names(
            {"det": detection_record()}) == []


class TestSegArchGateFindings:
    def test_gate_accepts_exactly_the_mapped_architectures(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        set_seg_floor(packaging, monkeypatch, {arch: "1.2.0" for arch in archs})
        assert packaging.rfdetr_seg_arch_gate_findings(
            [SEG_MODEL_NAME], archs) == []

    def test_unmapped_architecture_is_rejected_by_name(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        dropped = "arm64_jp7"
        set_seg_floor(packaging, monkeypatch,
                      {arch: "1.2.0" for arch in archs if arch != dropped})
        findings = packaging.rfdetr_seg_arch_gate_findings(
            [SEG_MODEL_NAME, "seg-b"], archs)
        assert [f['arch'] for f in findings] == [dropped]
        finding = findings[0]
        assert finding['code'] == 'RFDETR_SEG_UNSUPPORTED_ARCH'
        assert finding['models'] == [SEG_MODEL_NAME, "seg-b"]
        assert dropped in finding['message']
        assert SEG_MODEL_NAME in finding['message']
        assert "arm64_jp6" in finding['message']  # a supported arch is named

    def test_empty_map_rejects_every_architecture(self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        # The pre-enablement state: no LocalServer build carries the Seg
        # contract, so nothing referencing a Seg model may be packaged.
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        set_seg_floor(packaging, monkeypatch, {})
        findings = packaging.rfdetr_seg_arch_gate_findings([SEG_MODEL_NAME], archs)
        assert sorted(f['arch'] for f in findings) == archs
        assert all(f['code'] == 'RFDETR_SEG_UNSUPPORTED_ARCH' for f in findings)
        assert all("none" in f['message'] for f in findings)

    def test_workflow_without_a_seg_model_is_never_gated(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        set_seg_floor(packaging, monkeypatch, {})
        assert packaging.rfdetr_seg_arch_gate_findings(
            [], sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)) == []


class TestFloorIsTheMaximumOfThree:
    def test_seg_floor_raises_the_architecture_floor(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.3"})
        assert packaging.min_local_server_version_for("arm64_jp6") == "1.0.0"
        assert packaging.min_local_server_version_for(
            "arm64_jp6", rfdetr_seg=True) == "1.2.3"

    def test_higher_architecture_floor_wins(self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_cpu": "1.1.0"})
        set_seg_floor(packaging, monkeypatch, {"arm64_cpu": "1.0.5"})
        assert packaging.min_local_server_version_for(
            "arm64_cpu", rfdetr_seg=True) == "1.1.0"

    def test_comparison_is_numeric_not_lexicographic(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp7": "1.0.9"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp7": "1.0.10"})
        # A string compare would pick '1.0.9'.
        assert packaging.min_local_server_version_for(
            "arm64_jp7", rfdetr_seg=True) == "1.0.10"

    def test_three_floors_combine_as_their_maximum(self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True, rfdetr_seg=True) == "1.3.0"
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.4.0"})
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True, rfdetr_seg=True) == "1.4.0"
        # Each flag alone still resolves its own maximum.
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True) == "1.3.0"
        assert packaging.min_local_server_version_for(
            "arm64_jp6", rfdetr_seg=True) == "1.4.0"

    def test_unmapped_architecture_fails_closed(self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        # The handler's gate rejects first; this is the backstop for any
        # caller that skipped it - never a silently un-raised floor.
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.min_local_server_version_for("arm64_jp5", rfdetr_seg=True)
        assert excinfo.value.artifact == "rfdetr-seg-feature-floor/arm64_jp5"
        assert "arm64_jp5" in str(excinfo.value)
        assert "RFDETR_SEG_UNSUPPORTED_ARCH" in str(excinfo.value)
        assert "RF-DETR-Seg" in excinfo.value.message

    def test_map_is_raised_only_for_the_seg_floor_archs(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5", "x86_64": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0", "x86_64": "1.1.0"})
        set_seg_floor(packaging, monkeypatch,
                      {"arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"})
        assert packaging.min_local_server_versions_map() == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5", "x86_64": "1.0.0"}
        assert packaging.min_local_server_versions_map(rfdetr_seg=True) == {
            "arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0", "x86_64": "1.0.0"}
        # Both features: every arch takes max(lineage, stream, seg); an arch
        # in only one of the two feature maps is raised by that one alone.
        assert packaging.min_local_server_versions_map(
            stream_features=True, rfdetr_seg=True) == {
            "arm64_jp6": "1.3.0", "arm64_jp5": "1.2.0", "x86_64": "1.1.0"}
        # A Seg-floor arch absent from the lineage map joins the map raised.
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        assert packaging.min_local_server_versions_map(rfdetr_seg=True) == {
            "arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"}

    def test_local_server_dependency_requirement_is_raised(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        plain = packaging.local_server_component_dependencies(["arm64_jp6"])
        raised = packaging.local_server_component_dependencies(
            ["arm64_jp6"], rfdetr_seg=True)
        variant = "aws.edgeml.dda.LocalServer.arm64JP6"
        assert plain[variant]["VersionRequirement"] == ">=1.0.0"
        assert raised[variant]["VersionRequirement"] == ">=1.2.0"
        assert raised[variant]["DependencyType"] == "HARD"
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.local_server_component_dependencies(
                ["arm64_jp5"], rfdetr_seg=True)
        assert excinfo.value.artifact == "rfdetr-seg-feature-floor/arm64_jp5"


class TestManifestFloors:
    """The mirror of test_stream_camera_feature_floor_coverage.py:462-489
    (tasks review pass 7, C23): ``build_manifest`` is the only caller of
    ``min_local_server_versions_map``, so the Seg floor reaches the device
    only through it."""

    KWARGS = dict(gst_plugins=[], python_packages=[], custom_python_nodes=[],
                  user={"user_id": "u1"})

    def test_manifest_scalar_and_map_are_both_raised_consistently(
            self, packaging, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"})
        set_seg_floor(packaging, monkeypatch,
                      {"arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"})
        plain = packaging.build_manifest("wf-1", 1, "arm64_jp6", **self.KWARGS)
        assert plain["minLocalServerVersion"] == "1.0.0"
        assert plain["minLocalServerVersions"] == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"}
        seg = packaging.build_manifest(
            "wf-1", 1, "arm64_jp6", rfdetr_seg=True, **self.KWARGS)
        assert seg["minLocalServerVersion"] == "1.2.0"
        assert seg["minLocalServerVersions"] == {
            "arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"}
        # The map entry for the package's own targetArch equals the scalar.
        assert seg["minLocalServerVersions"]["arm64_jp6"] == \
            seg["minLocalServerVersion"]
        # Nothing else in the manifest moves with the flag.
        volatile = {"packagedAt", "minLocalServerVersion", "minLocalServerVersions"}
        assert {k: v for k, v in plain.items() if k not in volatile} == \
            {k: v for k, v in seg.items() if k not in volatile}

    def test_seg_map_missing_the_target_arch_raises(self, packaging, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.build_manifest(
                "wf-1", 1, "arm64_jp5", rfdetr_seg=True, **self.KWARGS)
        assert excinfo.value.artifact == "rfdetr-seg-feature-floor/arm64_jp5"
        # Without the flag the same package stamps as before.
        plain = packaging.build_manifest("wf-1", 1, "arm64_jp5", **self.KWARGS)
        assert plain["minLocalServerVersion"] == "1.0.5"


class TestWidenedSignaturesDefaultOff:
    def test_rfdetr_seg_defaults_to_false_on_every_widened_signature(
            self, packaging):
        # Validates: Requirements 14.8, 17.1
        # Every existing call site calls these without the new keyword, so
        # the default IS the backward-compatibility contract.
        for name in ("min_local_server_version_for",
                     "min_local_server_versions_map",
                     "local_server_component_dependencies",
                     "build_manifest"):
            parameters = signature(getattr(packaging, name)).parameters
            parameter = parameters["rfdetr_seg"]
            assert parameter.default is False, (
                f"{name}() must default rfdetr_seg to False so a workflow "
                "without a Seg model resolves its floors exactly as before "
                "(Requirement 17.1)")
            assert parameter.kind is Parameter.POSITIONAL_OR_KEYWORD
            # The stream keyword keeps its default beside the new one.
            assert parameters["stream_features"].default is False
        # build_manifest's pre-existing keyword-only reference_files stays
        # keyword-only, after rfdetr_seg (base drift BD1).
        build_manifest = signature(packaging.build_manifest).parameters
        assert build_manifest["reference_files"].kind is Parameter.KEYWORD_ONLY
        assert build_manifest["reference_files"].default is None
        names = list(build_manifest)
        assert names.index("rfdetr_seg") == names.index("stream_features") + 1
        assert names.index("reference_files") > names.index("rfdetr_seg")

    def test_resolve_model_components_records_defaults_to_none(self, packaging):
        # Validates: Requirements 14.8, 17.1
        parameter = signature(
            packaging.resolve_model_components).parameters["records"]
        assert parameter.default is None

    def test_deployments_gate_defaults_rfdetr_seg_to_false(self, deployments):
        # Validates: Requirements 14.8, 17.1
        parameters = signature(
            deployments.check_local_server_compatibility).parameters
        assert parameters["rfdetr_seg"].default is False
        assert parameters["stream_features"].default is False
        names = list(parameters)
        assert names.index("rfdetr_seg") == names.index("stream_features") + 1


class TestLoadModelRecords:
    """``load_model_records`` is the first step of ``resolve_model_components``
    split out; both read the same snapshot and fail closed the same way."""

    def test_records_are_keyed_by_name_in_reference_order(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8
        packaging = packaging_env.packaging
        usecase_id = env.create_usecase()
        seed_record(packaging_env, usecase_id, seg_record())
        seed_record(packaging_env, usecase_id, detection_record())
        records = packaging.load_model_records(
            [DETECTION_MODEL_NAME, SEG_MODEL_NAME], usecase_id)
        assert list(records) == [DETECTION_MODEL_NAME, SEG_MODEL_NAME]
        assert records[SEG_MODEL_NAME]["detection"]["detection_arch"] == \
            "rf_detr_seg"
        assert packaging.rfdetr_seg_model_names(records) == [SEG_MODEL_NAME]
        # Resolution from the pre-loaded records equals resolution from a
        # fresh read (every existing caller's path).
        archs = ["arm64_jp6"]
        assert packaging.resolve_model_components(
            [DETECTION_MODEL_NAME, SEG_MODEL_NAME], usecase_id, archs,
            records=records) == packaging.resolve_model_components(
            [DETECTION_MODEL_NAME, SEG_MODEL_NAME], usecase_id, archs) == {
            DETECTION_MODEL_NAME: {f"{DETECTION_MODEL_COMPONENT}-jetson-xavier-jp6"},
            SEG_MODEL_NAME: {f"{SEG_MODEL_COMPONENT}-jetson-xavier-jp6"},
        }

    def test_missing_record_fails_closed_naming_the_model(
            self, env, packaging_env):
        # Validates: Requirements 14.8
        packaging = packaging_env.packaging
        usecase_id = env.create_usecase()
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.load_model_records(["ghost"], usecase_id)
        assert excinfo.value.artifact == "models/ghost"
        assert "no record" in excinfo.value.message
        # The same error from resolve_model_components' own read.
        with pytest.raises(packaging.PackagingError) as excinfo2:
            packaging.resolve_model_components(["ghost"], usecase_id, ["arm64_jp6"])
        assert excinfo2.value.artifact == "models/ghost"
        assert excinfo2.value.message == excinfo.value.message

    def test_no_references_and_no_registry_load_nothing(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        packaging = packaging_env.packaging
        assert packaging.load_model_records([], env.create_usecase()) == {}
        monkeypatch.setattr(packaging, "TRAINING_JOBS_TABLE", "")
        assert packaging.load_model_records([SEG_MODEL_NAME], "uc-x") == {}
        assert packaging.resolve_model_components(
            [SEG_MODEL_NAME], "uc-x", ["arm64_jp6"]) == {}
        assert packaging.resolve_model_components(
            [SEG_MODEL_NAME], "uc-x", ["arm64_jp6"], records={}) == {}


# --------------------------------------------------------------------------
# 2. The gate is WIRED into the packaging handler
# --------------------------------------------------------------------------
class TestHandlerAppliesTheSegFloor:
    """End-to-end through ``workflow_packaging.handler`` with a seeded
    Model_Registry record, reusing the binding-point suite's packaging
    harness (validated workflow version, Use_Case bucket, patched
    Use_Case-account clients)."""

    def harness(self, env, packaging_env, monkeypatch, definition, *records):
        harness = BindingPointsEnv(env, packaging_env.packaging, monkeypatch,
                                   definition)
        for record in records:
            seed_record(packaging_env, harness.usecase_id, record)
        return harness

    def test_uncovered_architecture_is_rejected_and_registers_nothing(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8
        packaging = packaging_env.packaging
        set_seg_floor(packaging, monkeypatch, {})
        harness = self.harness(env, packaging_env, monkeypatch,
                               model_definition(SEG_MODEL_NAME), seg_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 409, payload
        error = payload["error"]
        assert error["code"] == 'RFDETR_SEG_UNSUPPORTED_ARCH'
        assert "arm64_jp6" in error["message"]
        assert SEG_MODEL_NAME in error["message"]
        assert error["details"]["version"] == 1
        assert error["details"]["architectures"] == ["arm64_jp6"]
        [finding] = error["details"]["findings"]
        assert finding == {
            "code": 'RFDETR_SEG_UNSUPPORTED_ARCH', "arch": "arm64_jp6",
            "models": [SEG_MODEL_NAME], "message": error["message"]}
        # Rejected BEFORE any component version exists (7.5 discipline) and
        # before the version item is written: no discriminator, no arn.
        harness.greengrass.create_component_version.assert_not_called()
        item = harness.version_item()
        assert "has_rfdetr_seg_model" not in item
        assert "packaged_at" not in item

    def test_covered_architecture_packages_with_the_raised_floor(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8
        packaging = packaging_env.packaging
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_seg_floor(packaging, monkeypatch, {"arm64_jp6": "1.3.0"})
        harness = self.harness(env, packaging_env, monkeypatch,
                               model_definition(SEG_MODEL_NAME), seg_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = packaged_manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.3.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.3.0"}
        recipe = registered_recipe(harness)
        dependencies = recipe["ComponentDependencies"]
        assert dependencies["aws.edgeml.dda.LocalServer.arm64JP6"][
            "VersionRequirement"] == ">=1.3.0"
        # The Seg model's own component is still a dependency (resolution
        # from the pre-loaded records).
        assert f"{SEG_MODEL_COMPONENT}-jetson-xavier-jp6" in dependencies
        # The discriminator is written on the version item (14.8 b).
        assert harness.version_item()["has_rfdetr_seg_model"] is True

    def test_workflow_without_a_seg_model_keeps_its_unraised_floors(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        packaging = packaging_env.packaging
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_seg_floor(packaging, monkeypatch, {})  # fail-closed for Seg only
        harness = self.harness(env, packaging_env, monkeypatch,
                               model_definition(DETECTION_MODEL_NAME),
                               detection_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = packaged_manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.0.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.0.0"}
        assert registered_recipe(harness)["ComponentDependencies"][
            "aws.edgeml.dda.LocalServer.arm64JP6"]["VersionRequirement"] == ">=1.0.0"
        # Written unconditionally, False for a workflow without a Seg model.
        assert harness.version_item()["has_rfdetr_seg_model"] is False

    def test_model_free_workflow_writes_false_and_is_never_gated(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        packaging = packaging_env.packaging
        set_seg_floor(packaging, monkeypatch, {})
        harness = self.harness(env, packaging_env, monkeypatch,
                               folder_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        item = harness.version_item()
        assert item["has_rfdetr_seg_model"] is False
        assert item["has_stream_features"] is False


# --------------------------------------------------------------------------
# 3. The deployments gate — function level
# --------------------------------------------------------------------------
class TestDeploymentGateFunctionLevel:
    """``check_local_server_compatibility`` directly, for the wording and
    the defaults the route depends on (the
    ``test_stream_binding_deployment.py`` TestFloorGateFunctionLevel
    pattern)."""

    class _Greengrass:
        def __init__(self, installed, component="aws.edgeml.dda.LocalServer.arm64JP6"):
            self.installed = installed
            self.component = component

        def get_paginator(self, operation):
            assert operation == "list_installed_components"
            installed, component = self.installed, self.component

            class _Paginator:
                def paginate(self, coreDeviceThingName=None, **_):
                    version = installed.get(coreDeviceThingName)
                    components = ([] if version is None else [{
                        "componentName": component,
                        "componentVersion": version}])
                    return iter([{"installedComponents": components}])
            return _Paginator()

    def set_floor(self, deployments, monkeypatch, floor_map):
        monkeypatch.setattr(
            deployments, "WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            dict(floor_map))

    def test_floor_helper_mirrors_the_map(self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        assert deployments.rfdetr_seg_feature_floor_for("arm64_jp6") == "1.2.3"
        assert deployments.rfdetr_seg_feature_floor_for("arm64_jp5") is None
        assert deployments.rfdetr_seg_feature_floor_for(None) is None

    def test_below_floor_device_is_rejected_with_both_versions_named(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0", {},
            rfdetr_seg=True)
        assert device["device"] == "jp6-a"
        assert device["local_server_version"] == "1.0.40"
        assert device["min_local_server_version"] == "1.2.3"
        assert "1.2.3" in device["reason"] and "1.0.40" in device["reason"]

    def test_device_at_or_above_the_floor_is_accepted(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        gg = self._Greengrass({"jp6-a": "1.2.3", "jp6-b": "1.10.0"})
        assert deployments.check_local_server_compatibility(
            gg, ["jp6-a", "jp6-b"], "1.0.0", {}, rfdetr_seg=True) == []

    def test_comparison_is_numeric_not_lexicographic(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.0.9"})
        assert deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.10"}), ["jp6-a"], "1.0.0", {},
            rfdetr_seg=True) == []

    def test_seg_floor_raises_but_never_lowers_the_resolved_floor(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.1.0"})
        gg = self._Greengrass({"jp6-a": "1.0.40"})
        [raised] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.0.1"}, rfdetr_seg=True)
        assert raised["min_local_server_version"] == "1.1.0"
        [kept] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.3.0"}, rfdetr_seg=True)
        assert kept["min_local_server_version"] == "1.3.0"

    def test_both_features_take_the_maximum_of_the_three_floors(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0"})
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.0"})
        gg = self._Greengrass({"jp6-a": "1.0.40"})
        [device] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.0.1"},
            stream_features=True, rfdetr_seg=True)
        assert device["min_local_server_version"] == "1.3.0"
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.4.0"})
        [device] = deployments.check_local_server_compatibility(
            gg, ["jp6-a"], "1.0.0", {"arm64_jp6": "1.0.1"},
            stream_features=True, rfdetr_seg=True)
        assert device["min_local_server_version"] == "1.4.0"

    def test_no_entry_rejects_naming_the_architecture(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        # No build of this lineage carries the Seg contract: there is no
        # version to name, so min_local_server_version is None and the
        # reason names the architecture and the feature.
        self.set_floor(deployments, monkeypatch, {"arm64_jp7": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "99.0.0"}), ["jp6-a"], "1.0.0", {},
            rfdetr_seg=True)
        assert device["local_server_version"] == "99.0.0"
        assert device["min_local_server_version"] is None
        assert "arm64_jp6" in device["reason"]
        assert "RF-DETR-Seg" in device["reason"]

    def test_empty_map_rejects_every_device(self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {})
        incompatible = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "99.0.0", "jp6-b": "1.0.0"}),
            ["jp6-a", "jp6-b"], "1.0.0", {}, rfdetr_seg=True)
        assert [d["device"] for d in incompatible] == ["jp6-a", "jp6-b"]
        assert all(d["min_local_server_version"] is None for d in incompatible)

    def test_unknown_variant_is_rejected_with_the_undetermined_wording(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            self._Greengrass({"odd": "1.5.0"},
                             component="aws.edgeml.dda.LocalServer.mystery"),
            ["odd"], "1.0.0", {}, rfdetr_seg=True)
        assert device["min_local_server_version"] is None
        assert "could not be determined" in device["reason"]

    def test_default_off_ignores_a_configured_seg_floor(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "9.9.9"})
        assert deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0",
            {"arm64_jp6": "1.0.0"}) == []
        # ... and the stream flag alone does not pull the Seg floor in.
        monkeypatch.setattr(
            deployments, "WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0"})
        assert deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0",
            {"arm64_jp6": "1.0.0"}, stream_features=True) == []

    def test_no_local_server_installed_keeps_its_pre_feature_reason(
            self, deployments, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        self.set_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            self._Greengrass({"jp6-a": None}), ["jp6-a"], "1.0.0", {},
            rfdetr_seg=True)
        assert device["local_server_version"] is None
        assert device["reason"] == \
            'No LocalServer component is installed on this device'


# --------------------------------------------------------------------------
# 4. The deployments gate — route level (POST /deployments)
# --------------------------------------------------------------------------
def mark_seg_model(fleet, enabled=True):
    """Set the packager-recorded ``has_rfdetr_seg_model`` discriminator on
    the seeded version item — what activates the Seg floor gate in the
    deployment handler."""
    fleet.env.stack.tables.versions.update_item(
        Key={"workflow_id": fleet.workflow_id, "version": 1},
        UpdateExpression="SET has_rfdetr_seg_model = :v",
        ExpressionAttributeValues={":v": enabled})


def set_deployed_seg_floor(fleet, monkeypatch, floor_map):
    monkeypatch.setattr(fleet.deployments,
                        "WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
                        dict(floor_map))


class TestDeploymentRouteAppliesTheSegFloor:
    def deploy_seg_workflow(self, fleet, monkeypatch, floor_map,
                            installed="1.0.5", seg=True, override=None):
        fleet.seed_workflow([camera_node("n1")])
        if seg:
            mark_seg_model(fleet)
        if override:
            fleet.env.stack.tables.versions.update_item(
                Key={"workflow_id": fleet.workflow_id, "version": 1},
                UpdateExpression="SET min_local_server_version = :v",
                ExpressionAttributeValues={":v": override})
        set_deployed_seg_floor(fleet, monkeypatch, floor_map)
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a", local_server_version=installed)
        return fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

    def test_below_floor_device_is_rejected_with_the_seg_details(
            self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.4.0"}, installed="1.0.5")
        assert status == 409, payload
        error = payload["error"]
        assert error["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        [device] = error["details"]["incompatible_devices"]
        assert device["device"] == "line-a"
        assert device["local_server_version"] == "1.0.5"
        assert device["min_local_server_version"] == "1.4.0"
        assert "1.4.0" in device["reason"]
        # The configured Seg floor that produced the per-device minimums is
        # echoed; the stream keys are not (no stream features here).
        assert error["details"]["rfdetr_seg_model"] is True
        assert error["details"]["rfdetr_seg_min_local_server_versions"] == \
            {FLEET_ARCH: "1.4.0"}
        assert "stream_features" not in error["details"]
        assert fleet.gg.create_deployment_calls == []

    def test_device_at_the_floor_is_accepted(self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.4.0"}, installed="1.4.0")
        assert status == 201, payload
        assert len(fleet.gg.create_deployment_calls) == 1

    def test_no_entry_rejects_naming_the_architecture(self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {"arm64_jp7": "1.4.0"}, installed="99.0.0")
        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] is None
        assert FLEET_ARCH in device["reason"]
        assert "RF-DETR-Seg" in device["reason"]
        assert payload["error"]["details"]["rfdetr_seg_model"] is True

    def test_deployed_empty_map_rejects_every_device(self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        # The shipped pre-enablement state (empty until spec group 20 fills
        # it) rejects every device rather than resolving a floor every
        # device satisfies: fail closed.
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {}, installed="99.0.0")
        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] is None
        assert payload["error"]["details"][
            "rfdetr_seg_min_local_server_versions"] == {}
        assert fleet.gg.create_deployment_calls == []

    def test_per_version_pin_does_not_license_an_unsupported_device(
            self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        # A per-version min_local_server_version override is a floor pin,
        # not a licence: it cannot lower a Seg workflow below the Seg floor
        # (deployments.py check_local_server_compatibility, the stream rule).
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "1.4.0"}, installed="1.0.5",
            override="1.0.0")
        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] == "1.4.0"
        assert "1.4.0" in device["reason"]

    def test_per_version_pin_does_not_step_around_a_missing_entry(
            self, fleet, monkeypatch):
        # Validates: Requirements 14.8
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {}, installed="99.0.0", override="1.0.0")
        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] is None
        assert FLEET_ARCH in device["reason"]

    def test_workflow_without_the_discriminator_is_unaffected(
            self, fleet, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        # Identity: with a 9.9.9 Seg floor configured, a workflow whose
        # version item carries no has_rfdetr_seg_model deploys exactly as
        # before — the gate is activated by the discriminator only.
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "9.9.9"}, installed="1.0.5",
            seg=False)
        assert status == 201, payload

    def test_non_seg_rejection_payload_gains_no_seg_keys(
            self, fleet, monkeypatch):
        # Validates: Requirements 14.8, 17.1
        # Identity: a workflow without a Seg model rejected by the ordinary
        # architecture floor gets the pre-feature payload — no
        # rfdetr_seg_model and no Seg floor map.
        monkeypatch.setattr(fleet.deployments,
                            "WORKFLOW_MIN_LOCAL_SERVER_VERSION", "2.0.0")
        status, payload = self.deploy_seg_workflow(
            fleet, monkeypatch, {FLEET_ARCH: "9.9.9"}, installed="1.0.5",
            seg=False)
        assert status == 409, payload
        details = payload["error"]["details"]
        assert "rfdetr_seg_model" not in details
        assert "rfdetr_seg_min_local_server_versions" not in details
        [device] = details["incompatible_devices"]
        assert device["min_local_server_version"] == "2.0.0"
