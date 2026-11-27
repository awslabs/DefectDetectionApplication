# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Layout_Check feature floor: car-assembly-check task 12.1 (Requirement 9.3,
design §7, DD9 variant B).

A workflow with a ``layout_check`` node needs a LocalServer build whose
scene-analytics pass evaluates the node's executor binding. The packager
resolves such a workflow's floors as the MAXIMUM of the lineage, stream, Seg
and Layout_Check FEATURE floors (``WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_
VERSIONS``), rejects any requested architecture missing from that map with
``LAYOUT_CHECK_UNSUPPORTED_ARCH`` before anything is registered, and records
``has_layout_check`` on the version item unconditionally; the pre-submit
deployment gate reads the same map and the discriminator.

The teeth half of the mechanism (the shape half is
``test_layout_check_feature_floor_coverage.py``), modelled case by case on
``test_rfdetr_seg_feature_floor.py``:

1. the pure helpers: ``_has_node_type``, ``layout_check_arch_gate_findings``
   (exactly the map's archs accepted, the rest rejected by name with each
   mapped arch's floor listed), the widened floor resolvers
   (``max(lineage, stream, seg, layout_check)``, numeric order, the
   ``PackagingError('layout-check-feature-floor/<arch>')`` backstop), the
   LocalServer dependency requirement;
2. the manifest scalar AND map raised; Seg-only and stream-only manifests
   byte-identical whatever the Layout_Check floor holds;
3. every widened signature defaults ``layout_check`` to ``False``;
4. the handler: 409 with the complete findings and nothing registered on an
   uncovered arch; raised floors and ``has_layout_check = True`` on a covered
   one; ``False`` for a workflow without the node;
5. the deployments gate, function and route level: a device below the floor
   is refused with the installed and required versions named.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module imports):

    python3 -m pytest tests/test_layout_check_feature_floor.py \
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
from test_workflow_packaging_binding_points import (
    BindingPointsEnv,
    COMPONENTS_ROOT,
)

#: The per-deployment Layout_Check floor env var (design §7).
LAYOUT_FLOOR_ENV = 'WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS'
#: Own Model_Registry and camera-registry tables, so this module can share
#: one moto session with the other packaging and deployment suites.
TRAINING_JOBS_TABLE_NAME = 'test-layout-check-feature-floor-training-jobs'
LAYOUT_REGISTRY_TABLE = 'test-camera-registry-layout-check-floor'
#: The LocalServer variant FakeGreengrass.register_device installs.
FLEET_ARCH = 'x86_64'

DETECTOR_NAME = 'car-assembly-gray'
DETECTOR_COMPONENT = 'model-car-assembly-gray'
SEG_MODEL_NAME = 'parts-seg'
SEG_MODEL_COMPONENT = 'model-parts-seg'
JP6_VARIANT = 'aws.edgeml.dda.LocalServer.arm64JP6'

_REGIONS = json.dumps({
    "rear_fender": [[0.12, 0.44], [0.47, 0.37], [0.47, 0.86], [0.12, 0.80]],
    "roof": [[0.30, 0.36], [0.40, 0.16], [0.60, 0.16], [0.72, 0.36]],
})


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """A Model_Registry table plus a freshly imported workflow_packaging bound
    to it (the test_rfdetr_seg_feature_floor.py setup)."""
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
def layout_stack(aws_stack):
    """The deployments module bound to its own camera-registry table."""
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=LAYOUT_REGISTRY_TABLE,
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
    os.environ["CAMERA_REGISTRY_TABLE"] = LAYOUT_REGISTRY_TABLE
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "deployments": deployments,
        "registry": resource.Table(LAYOUT_REGISTRY_TABLE),
    }


@pytest.fixture(scope="module")
def deployments(layout_stack):
    return layout_stack["deployments"]


@pytest.fixture
def fleet(env, layout_stack, monkeypatch):
    return BindingEnv(env, layout_stack, monkeypatch)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def model_record(name, component, detection_arch, model_type):
    """A published Model_Registry record (vision ``published_components``)."""
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


def detector_record():
    return model_record(DETECTOR_NAME, DETECTOR_COMPONENT, 'rf_detr',
                        'object_detection')


def seg_record():
    return model_record(SEG_MODEL_NAME, SEG_MODEL_COMPONENT, 'rf_detr_seg',
                        'instance_segmentation')


def definition(model_name, with_layout=True):
    """folder -> model_inference -> capture, plus a layout_check node on the
    detector's output when ``with_layout``."""
    nodes = [
        ("src", "folder_source", {"location": "/aws_dda/batch-input/"}),
        ("inf", "model_inference", {"modelName": model_name}),
        ("cap", "capture", {"output_path": "/aws_dda/captures"}),
    ]
    connections = [("src", "inf"), ("inf", "cap")]
    if with_layout:
        nodes.append(("layout", "layout_check", {"expected_regions": _REGIONS}))
        connections.append(("inf", "layout"))
    return {
        "schemaVersion": 1,
        "nodes": [{"id": node_id, "type": node_type,
                   "position": {"x": index * 100, "y": 0},
                   "parameters": parameters}
                  for index, (node_id, node_type, parameters) in enumerate(nodes)],
        "connections": [{"id": f"c{index}",
                         "from": {"node": source, "port": "out"},
                         "to": {"node": target, "port": "in"}}
                        for index, (source, target) in enumerate(connections)],
    }


def seed_record(packaging_env, usecase_id, record):
    item = {"training_id": f"tr-{uuid.uuid4()}", "usecase_id": usecase_id,
            "created_at": 1, **record}
    packaging_env.training_table.put_item(Item=item)


def set_layout_floor(packaging, monkeypatch, floor_map):
    monkeypatch.setattr(packaging, "LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS",
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
class TestVocabulary:
    def test_gate_code_and_node_type_are_the_spelled_contract(self, packaging):
        # Validates: Requirements 9.3
        assert packaging.GATE_LAYOUT_CHECK_ARCH_UNSUPPORTED == \
            'LAYOUT_CHECK_UNSUPPORTED_ARCH'
        assert packaging.LAYOUT_CHECK_TYPE_ID == 'layout_check'

    def test_layout_check_is_not_a_stream_feature_type(self, packaging):
        # Validates: Requirements 9.3
        # It has its own floor; joining STREAM_FEATURE_TYPE_IDS would put
        # every Layout_Check workflow under the stream floor as well.
        assert 'layout_check' not in packaging.STREAM_FEATURE_TYPE_IDS

    def test_has_node_type_reads_the_definition_nodes(self, packaging):
        # Validates: Requirements 9.3
        assert packaging._has_node_type(definition(DETECTOR_NAME), 'layout_check')
        assert not packaging._has_node_type(
            definition(DETECTOR_NAME, with_layout=False), 'layout_check')
        assert not packaging._has_node_type({}, 'layout_check')
        assert not packaging._has_node_type(None, 'layout_check')
        assert not packaging._has_node_type(
            {"nodes": ["layout_check", {"type": "layout_check"}]}, 'layout_check')


class TestLayoutArchGateFindings:
    def test_gate_accepts_exactly_the_mapped_architectures(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        set_layout_floor(packaging, monkeypatch, {arch: "1.2.0" for arch in archs})
        assert packaging.layout_check_arch_gate_findings(
            definition(DETECTOR_NAME), archs) == []

    def test_unmapped_architecture_is_rejected_naming_arch_node_and_floors(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        set_layout_floor(packaging, monkeypatch,
                         {"arm64_jp5": "1.0.4", "x86_64": "1.0.60"})
        findings = packaging.layout_check_arch_gate_findings(
            definition(DETECTOR_NAME), ["arm64_jp5", "arm64_jp6"])
        assert [f['arch'] for f in findings] == ["arm64_jp6"]
        [finding] = findings
        assert finding['code'] == 'LAYOUT_CHECK_UNSUPPORTED_ARCH'
        assert finding['nodeIds'] == ["layout"]
        message = finding['message']
        assert "'arm64_jp6'" in message
        assert "layout_check" in message
        assert "layout" in message
        # Each mapped arch is listed with its floor version.
        assert "arm64_jp5 >= 1.0.4" in message
        assert "x86_64 >= 1.0.60" in message

    def test_empty_map_rejects_every_architecture(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        # No floor configured for this deployment: fail closed everywhere.
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        set_layout_floor(packaging, monkeypatch, {})
        findings = packaging.layout_check_arch_gate_findings(
            definition(DETECTOR_NAME), archs)
        assert sorted(f['arch'] for f in findings) == archs
        assert all(f['code'] == 'LAYOUT_CHECK_UNSUPPORTED_ARCH' for f in findings)
        assert all(f['message'].endswith("version: none") for f in findings)

    def test_workflow_without_the_node_is_never_gated(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        set_layout_floor(packaging, monkeypatch, {})
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        assert packaging.layout_check_arch_gate_findings(
            definition(DETECTOR_NAME, with_layout=False), archs) == []
        stream_only = {"nodes": [{"id": "cam", "type": "rtsp_camera_source"}]}
        assert packaging.layout_check_arch_gate_findings(stream_only, archs) == []


class TestFloorIsTheMaximumOfFour:
    def test_layout_floor_raises_the_architecture_floor(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp5": "1.0.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        assert packaging.min_local_server_version_for("arm64_jp5") == "1.0.0"
        assert packaging.min_local_server_version_for(
            "arm64_jp5", layout_check=True) == "1.0.4"

    def test_higher_other_floor_wins(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp5": "1.1.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        assert packaging.min_local_server_version_for(
            "arm64_jp5", layout_check=True) == "1.1.0"

    def test_comparison_is_numeric_not_lexicographic(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp5": "1.0.9"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.10"})
        assert packaging.min_local_server_version_for(
            "arm64_jp5", layout_check=True) == "1.0.10"

    def test_four_floors_combine_as_their_maximum(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.4.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True, rfdetr_seg=True,
            layout_check=True) == "1.4.0"
        set_layout_floor(packaging, monkeypatch, {"arm64_jp6": "1.5.0"})
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True, rfdetr_seg=True,
            layout_check=True) == "1.5.0"
        # Each other flag alone does not pull the Layout_Check floor in.
        assert packaging.min_local_server_version_for(
            "arm64_jp6", rfdetr_seg=True) == "1.4.0"
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True) == "1.3.0"

    def test_unmapped_architecture_fails_closed(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.min_local_server_version_for("arm64_jp6", layout_check=True)
        assert excinfo.value.artifact == "layout-check-feature-floor/arm64_jp6"
        assert "arm64_jp6" in excinfo.value.message
        assert "LAYOUT_CHECK_UNSUPPORTED_ARCH" in excinfo.value.message
        assert "layout_check" in excinfo.value.message

    def test_map_is_raised_only_for_the_layout_floor_archs(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.0", "x86_64": "1.0.0"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0", "x86_64": "1.0.56"})
        set_layout_floor(packaging, monkeypatch,
                         {"arm64_jp5": "1.0.4", "x86_64": "1.0.60"})
        assert packaging.min_local_server_versions_map() == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.0", "x86_64": "1.0.0"}
        assert packaging.min_local_server_versions_map(layout_check=True) == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.4", "x86_64": "1.0.60"}
        assert packaging.min_local_server_versions_map(
            rfdetr_seg=True, layout_check=True) == {
            "arm64_jp6": "1.3.0", "arm64_jp5": "1.0.4", "x86_64": "1.0.60"}
        # A Layout_Check-floor arch absent from the lineage map joins raised.
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        assert packaging.min_local_server_versions_map(layout_check=True) == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.4", "x86_64": "1.0.60"}

    def test_local_server_dependency_requirement_is_raised(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp6": "1.2.0"})
        plain = packaging.local_server_component_dependencies(["arm64_jp6"])
        raised = packaging.local_server_component_dependencies(
            ["arm64_jp6"], layout_check=True)
        assert plain[JP6_VARIANT]["VersionRequirement"] == ">=1.0.0"
        assert raised[JP6_VARIANT]["VersionRequirement"] == ">=1.2.0"
        assert raised[JP6_VARIANT]["DependencyType"] == "HARD"
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.local_server_component_dependencies(
                ["arm64_jp5"], layout_check=True)
        assert excinfo.value.artifact == "layout-check-feature-floor/arm64_jp5"


# --------------------------------------------------------------------------
# 2. The manifest
# --------------------------------------------------------------------------
class TestManifestFloors:
    KWARGS = dict(gst_plugins=[], python_packages=[], custom_python_nodes=[],
                  user={"user_id": "u1"})

    def test_manifest_scalar_and_map_are_both_raised(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp5": "1.0.0", "arm64_jp6": "1.0.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        plain = packaging.build_manifest("wf-1", 1, "arm64_jp5", **self.KWARGS)
        assert plain["minLocalServerVersion"] == "1.0.0"
        assert plain["minLocalServerVersions"] == {
            "arm64_jp5": "1.0.0", "arm64_jp6": "1.0.0"}
        raised = packaging.build_manifest(
            "wf-1", 1, "arm64_jp5", layout_check=True, **self.KWARGS)
        assert raised["minLocalServerVersion"] == "1.0.4"
        assert raised["minLocalServerVersions"] == {
            "arm64_jp5": "1.0.4", "arm64_jp6": "1.0.0"}
        assert raised["minLocalServerVersions"]["arm64_jp5"] == \
            raised["minLocalServerVersion"]
        volatile = {"packagedAt", "minLocalServerVersion", "minLocalServerVersions"}
        assert {k: v for k, v in plain.items() if k not in volatile} == \
            {k: v for k, v in raised.items() if k not in volatile}

    def test_layout_map_missing_the_target_arch_raises(self, packaging, monkeypatch):
        # Validates: Requirements 9.3
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.build_manifest(
                "wf-1", 1, "arm64_jp6", layout_check=True, **self.KWARGS)
        assert excinfo.value.artifact == "layout-check-feature-floor/arm64_jp6"

    @pytest.mark.parametrize("flags", [
        {"rfdetr_seg": True},
        {"stream_features": True},
    ], ids=["seg-only", "stream-only"])
    def test_seg_only_and_stream_only_manifests_are_byte_identical(
            self, packaging, monkeypatch, flags):
        # Validates: Requirements 9.3
        # A workflow without a layout_check node stamps the same bytes
        # whatever the Layout_Check floor holds.
        monkeypatch.setattr(packaging, "now_ms", lambda: 1_700_000_000_000)
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.74"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.85"})
        texts = []
        for layout_floor in ({}, {"arm64_jp6": "9.9.9"}):
            set_layout_floor(packaging, monkeypatch, layout_floor)
            texts.append(json.dumps(packaging.build_manifest(
                "wf-1", 1, "arm64_jp6", **flags, **self.KWARGS), sort_keys=True))
            texts.append(json.dumps(packaging.build_manifest(
                "wf-1", 1, "arm64_jp6", layout_check=False, **flags,
                **self.KWARGS), sort_keys=True))
        assert len(set(texts)) == 1


# --------------------------------------------------------------------------
# 3. Defaults
# --------------------------------------------------------------------------
class TestWidenedSignaturesDefaultOff:
    def test_layout_check_defaults_to_false_on_every_widened_signature(
            self, packaging):
        # Validates: Requirements 9.3
        for name in ("min_local_server_version_for",
                     "min_local_server_versions_map",
                     "local_server_component_dependencies",
                     "build_manifest"):
            parameters = signature(getattr(packaging, name)).parameters
            parameter = parameters["layout_check"]
            assert parameter.default is False, name
            assert parameter.kind is Parameter.POSITIONAL_OR_KEYWORD, name
            names = list(parameters)
            assert names.index("layout_check") == names.index("rfdetr_seg") + 1
        build_manifest = signature(packaging.build_manifest).parameters
        assert build_manifest["reference_files"].kind is Parameter.KEYWORD_ONLY

    def test_deployments_gate_defaults_layout_check_to_false(self, deployments):
        # Validates: Requirements 9.3
        parameters = signature(
            deployments.check_local_server_compatibility).parameters
        assert parameters["layout_check"].default is False
        names = list(parameters)
        assert names.index("layout_check") == names.index("rfdetr_seg") + 1


# --------------------------------------------------------------------------
# 4. The gate is wired into the packaging handler
# --------------------------------------------------------------------------
class TestHandlerAppliesTheLayoutFloor:
    def harness(self, env, packaging_env, monkeypatch, workflow, *records):
        harness = BindingPointsEnv(env, packaging_env.packaging, monkeypatch,
                                   workflow)
        for record in records:
            seed_record(packaging_env, harness.usecase_id, record)
        return harness

    def test_uncovered_architecture_is_rejected_and_registers_nothing(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 9.3
        packaging = packaging_env.packaging
        set_layout_floor(packaging, monkeypatch, {"arm64_jp5": "1.0.4"})
        harness = self.harness(env, packaging_env, monkeypatch,
                               definition(DETECTOR_NAME), detector_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 409, payload
        error = payload["error"]
        assert error["code"] == 'LAYOUT_CHECK_UNSUPPORTED_ARCH'
        assert "arm64_jp6" in error["message"]
        assert "arm64_jp5 >= 1.0.4" in error["message"]
        assert error["details"]["version"] == 1
        assert error["details"]["architectures"] == ["arm64_jp6"]
        assert error["details"]["findings"] == [{
            "code": 'LAYOUT_CHECK_UNSUPPORTED_ARCH', "arch": "arm64_jp6",
            "nodeIds": ["layout"], "message": error["message"]}]
        harness.greengrass.create_component_version.assert_not_called()
        item = harness.version_item()
        assert "has_layout_check" not in item
        assert "packaged_at" not in item

    def test_covered_architecture_packages_with_the_raised_floor(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 9.3
        packaging = packaging_env.packaging
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp6": "1.3.0"})
        harness = self.harness(env, packaging_env, monkeypatch,
                               definition(DETECTOR_NAME), detector_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = packaged_manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.3.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.3.0"}
        dependencies = registered_recipe(harness)["ComponentDependencies"]
        assert dependencies[JP6_VARIANT]["VersionRequirement"] == ">=1.3.0"
        item = harness.version_item()
        assert item["has_layout_check"] is True
        assert item["has_rfdetr_seg_model"] is False
        assert item["has_stream_features"] is False

    def test_seg_and_layout_take_the_higher_floor(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 9.3
        packaging = packaging_env.packaging
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.85"})
        set_layout_floor(packaging, monkeypatch, {"arm64_jp6": "1.0.90"})
        harness = self.harness(env, packaging_env, monkeypatch,
                               definition(SEG_MODEL_NAME), seg_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = packaged_manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.0.90"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.0.90"}
        item = harness.version_item()
        assert item["has_layout_check"] is True
        assert item["has_rfdetr_seg_model"] is True

    def test_seg_only_workflow_is_not_gated_and_writes_false(
            self, env, packaging_env, monkeypatch):
        # Validates: Requirements 9.3
        packaging = packaging_env.packaging
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.85"})
        set_layout_floor(packaging, monkeypatch, {})  # fail-closed for layout only
        harness = self.harness(env, packaging_env, monkeypatch,
                               definition(SEG_MODEL_NAME, with_layout=False),
                               seg_record())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = packaged_manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.0.85"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.0.85"}
        item = harness.version_item()
        assert item["has_layout_check"] is False
        assert item["has_rfdetr_seg_model"] is True


# --------------------------------------------------------------------------
# 5. The deployments gate
# --------------------------------------------------------------------------
class _Greengrass:
    def __init__(self, installed, component=JP6_VARIANT):
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


def set_deployed_layout_floor(deployments, monkeypatch, floor_map):
    monkeypatch.setattr(deployments,
                        "WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS",
                        dict(floor_map))


class TestDeploymentGateFunctionLevel:
    def test_floor_helper_mirrors_the_map(self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        set_deployed_layout_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        assert deployments.layout_check_feature_floor_for("arm64_jp6") == "1.2.3"
        assert deployments.layout_check_feature_floor_for("arm64_jp5") is None
        assert deployments.layout_check_feature_floor_for(None) is None

    def test_below_floor_device_is_rejected_with_both_versions_named(
            self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        set_deployed_layout_floor(deployments, monkeypatch, {"arm64_jp6": "1.2.3"})
        [device] = deployments.check_local_server_compatibility(
            _Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0", {},
            layout_check=True)
        assert device["local_server_version"] == "1.0.40"
        assert device["min_local_server_version"] == "1.2.3"
        assert "1.2.3" in device["reason"] and "1.0.40" in device["reason"]

    def test_at_floor_accepted_and_numeric_order(self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        set_deployed_layout_floor(deployments, monkeypatch, {"arm64_jp6": "1.0.9"})
        assert deployments.check_local_server_compatibility(
            _Greengrass({"a": "1.0.9", "b": "1.0.10"}), ["a", "b"], "1.0.0", {},
            layout_check=True) == []

    def test_max_with_the_seg_floor(self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(
            deployments, "WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.85"})
        set_deployed_layout_floor(deployments, monkeypatch, {"arm64_jp6": "1.0.90"})
        [device] = deployments.check_local_server_compatibility(
            _Greengrass({"jp6-a": "1.0.86"}), ["jp6-a"], "1.0.0", {},
            rfdetr_seg=True, layout_check=True)
        assert device["min_local_server_version"] == "1.0.90"

    def test_no_entry_and_empty_map_reject_naming_the_architecture(
            self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        for floor_map in ({"arm64_jp5": "1.0.4"}, {}):
            set_deployed_layout_floor(deployments, monkeypatch, floor_map)
            [device] = deployments.check_local_server_compatibility(
                _Greengrass({"jp6-a": "99.0.0"}), ["jp6-a"], "1.0.0", {},
                layout_check=True)
            assert device["min_local_server_version"] is None
            assert "arm64_jp6" in device["reason"]
            assert "layout_check" in device["reason"]

    def test_default_off_ignores_a_configured_layout_floor(
            self, deployments, monkeypatch):
        # Validates: Requirements 9.3
        set_deployed_layout_floor(deployments, monkeypatch, {"arm64_jp6": "9.9.9"})
        assert deployments.check_local_server_compatibility(
            _Greengrass({"jp6-a": "1.0.40"}), ["jp6-a"], "1.0.0",
            {"arm64_jp6": "1.0.0"}) == []


def mark_layout_check(fleet):
    fleet.env.stack.tables.versions.update_item(
        Key={"workflow_id": fleet.workflow_id, "version": 1},
        UpdateExpression="SET has_layout_check = :v",
        ExpressionAttributeValues={":v": True})


class TestDeploymentRouteAppliesTheLayoutFloor:
    def deploy(self, fleet, monkeypatch, floor_map, installed, layout=True,
               override=None):
        fleet.seed_workflow([camera_node("n1")])
        if layout:
            mark_layout_check(fleet)
        if override:
            fleet.env.stack.tables.versions.update_item(
                Key={"workflow_id": fleet.workflow_id, "version": 1},
                UpdateExpression="SET min_local_server_version = :v",
                ExpressionAttributeValues={":v": override})
        set_deployed_layout_floor(fleet.deployments, monkeypatch, floor_map)
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a", local_server_version=installed)
        return fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

    def test_below_floor_device_gets_409_naming_installed_and_required(
            self, fleet, monkeypatch):
        # Validates: Requirements 9.3
        status, payload = self.deploy(
            fleet, monkeypatch, {FLEET_ARCH: "1.0.60"}, installed="1.0.5")
        assert status == 409, payload
        error = payload["error"]
        assert error["code"] == "INCOMPATIBLE_LOCAL_SERVER"
        [device] = error["details"]["incompatible_devices"]
        assert device["local_server_version"] == "1.0.5"
        assert device["min_local_server_version"] == "1.0.60"
        assert "1.0.5" in device["reason"] and "1.0.60" in device["reason"]
        assert error["details"]["layout_check"] is True
        assert error["details"]["layout_check_min_local_server_versions"] == \
            {FLEET_ARCH: "1.0.60"}
        assert "rfdetr_seg_model" not in error["details"]
        assert fleet.gg.create_deployment_calls == []

    def test_device_at_the_floor_is_accepted(self, fleet, monkeypatch):
        # Validates: Requirements 9.3
        status, payload = self.deploy(
            fleet, monkeypatch, {FLEET_ARCH: "1.0.60"}, installed="1.0.60")
        assert status == 201, payload
        assert len(fleet.gg.create_deployment_calls) == 1

    def test_per_version_pin_does_not_lower_the_floor(self, fleet, monkeypatch):
        # Validates: Requirements 9.3
        status, payload = self.deploy(
            fleet, monkeypatch, {FLEET_ARCH: "1.0.60"}, installed="1.0.5",
            override="1.0.0")
        assert status == 409, payload
        [device] = payload["error"]["details"]["incompatible_devices"]
        assert device["min_local_server_version"] == "1.0.60"

    def test_workflow_without_the_discriminator_is_unaffected(
            self, fleet, monkeypatch):
        # Validates: Requirements 9.3
        status, payload = self.deploy(
            fleet, monkeypatch, {FLEET_ARCH: "9.9.9"}, installed="1.0.5",
            layout=False)
        assert status == 201, payload
