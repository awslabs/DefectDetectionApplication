"""
Packaging backward-compatibility unit tests for rtsp-rtmp-stream-cameras
(spec task 6.4): a workflow that contains NONE of the new node types must
package exactly as it did before this feature.

**Feature: rtsp-rtmp-stream-cameras**

**Validates: Requirement 18.1**

Task 6.1 widened `gather_camera_input_nodes` / `build_binding_points` and
task 6.2 threaded a `stream_features` flag through the whole floor chain
(`min_local_server_version_for`, `min_local_server_versions_map`,
`local_server_component_dependencies`, `build_manifest`) plus a
pre-compile architecture gate. Every one of those seams sits on the path
that EXISTING workflows travel, so this suite pins the unchanged half:

1. **Floors resolve exactly as before.** The pre-feature resolution rule is
   re-spelled here as a local ORACLE (not imported from the module under
   test) and compared against `min_local_server_version_for(arch)` — the
   default, `stream_features=False` call every existing call site makes —
   over the representative floor-map configurations (unconfigured, fully
   configured, partially configured, single-entry) x every architecture x
   three feature-floor configurations, including one whose feature floor
   ('9.9.9') is far ABOVE every architecture floor. A stream-free workflow
   must not see it. `min_local_server_versions_map()` likewise returns the
   architecture map untouched, and every extended signature still defaults
   `stream_features` to False so an un-updated caller keeps pre-feature
   behaviour.
2. **Dependency entries are unchanged.** Single-variant architecture sets
   emit the pre-feature `>=<floor>` HARD entry, and a multi-variant set
   still emits nothing (edge-deploy-reliability Defect F) — with the
   feature floor configured high throughout.
3. **manifest.json is unchanged.** The key set carries no new key, both
   floor fields equal the oracle, and the manifest is BYTE-identical
   whether or not the feature floor is configured (`now_ms` frozen).
4. **Stream-free graphs never enter the new paths.** The whole pre-feature
   source vocabulary (folder, icam, CSI, aravis, the three together, and
   `unified_input` on pre-feature source kinds) yields no feature nodes and
   no gate findings on any architecture, and each existing camera type's
   binding point keeps its own marker with no `streamBinding` /
   `streamProtocol` key anywhere in the document.
5. **The handler agrees end to end.** With the feature floor map EMPTY (the
   currently deployed pre-enablement state, which rejects every stream
   workflow) a camera workflow still packages for all six architectures
   with its pre-feature floors, registers its component version, and its
   artifacts carry no stream key; configuring the feature floor high
   changes nothing about it.

The complementary halves live elsewhere and are not duplicated here:
`test_stream_camera_feature_floor_coverage.py` (task 6.2) pins the RAISED
floor and the rejection, and `test_property_stream_binding_points.py`
(task 6.3, Property 9) pins stream binding points and the compiled-document
byte identity of stream-free packaging over generated corpora.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module import; every pure seam below makes no AWS call):
    python3 -m pytest tests/test_stream_free_packaging_preservation.py \
        -q -p no:cacheprovider
"""
import io
import json
import sys
import zipfile
from inspect import signature

import pytest

from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.catalog.custom import resolve_catalog
from workflow_core.compiler import CompileContext, compile as compile_workflow
from workflow_core.serializer import parse

# The packaging harness that drives the real handler end to end (a validated
# workflow version, a Use_Case bucket, patched Use_Case-account clients) and
# the pre-feature definitions the binding-point suite already curates.
from test_workflow_packaging_binding_points import (
    BindingPointsEnv,
    COMPONENTS_ROOT,
    aravis_definition,
    cameraless_definition,
    csi_definition,
    icam_definition,
    mixed_camera_definition,
)

#: The feature-floor module constant (task 6.2). Monkeypatched rather than
#: set through the environment because it is parsed once at import.
FEATURE_FLOOR_ATTR = 'STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS'

#: The architecture-floor module constant (jp7-workflow-min-localserver-floor).
ARCH_FLOOR_ATTR = 'MIN_LOCAL_SERVER_VERSIONS'


# --------------------------------------------------------------------------
# Golden pre-feature contract, recorded from HEAD and re-spelled here (NOT
# imported from the module under test, so a change to either side shows up)
# --------------------------------------------------------------------------

#: arch id -> LocalServer variant component name.
GOLDEN_LOCAL_SERVER_VARIANTS = {
    "arm64_cpu": "aws.edgeml.dda.LocalServer.arm64",
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
    "x86_64": "aws.edgeml.dda.LocalServer.amd64",
    "x86_64_nvidia": "aws.edgeml.dda.LocalServer.amd64",
}

ARCHS = sorted(GOLDEN_LOCAL_SERVER_VARIANTS)

#: Resolved scalar floor when no WORKFLOW_MIN_LOCAL_SERVER_VERSION /
#: DDA_LOCAL_SERVER_VERSION is configured (conftest sets none of them), and
#: the safe per-lineage floor substituted for a known arch missing from a
#: configured map. Both are '1.0.0' today; pinned by
#: test_recorded_pre_feature_constants_still_hold so the oracle below cannot
#: silently diverge from the module.
GOLDEN_SCALAR_FLOOR = "1.0.0"
GOLDEN_SAFE_LINEAGE_FLOOR = "1.0.0"

#: A feature floor far above every architecture floor used below: if any
#: default (stream_features=False) resolution leaked the feature floor, it
#: would be unmistakable rather than coincidentally equal.
HIGH_FEATURE_FLOOR = "9.9.9"

#: manifest.json's key set before this feature (trigger-activation-runtime
#: added the conditional 'subscribed_topics'; everything else is always
#: present). rtsp-rtmp-stream-cameras adds NO manifest key — it only raises
#: the two floor VALUES for a stream/analytics workflow.
GOLDEN_MANIFEST_KEYS = {
    "componentName", "componentVersion", "workflowId", "workflowName",
    "workflowVersion", "targetArch", "minLocalServerVersion",
    "minLocalServerVersions", "pluginDependencies", "pythonDependencies",
    "pluginChecksums", "pluginComponents", "customPythonNodeIds",
    "packagedAt", "packagedBy",
}

#: Binding-point keys the packager could produce before this feature
#: (camera-registry-sync, csi-icam-input-nodes, aravis-camera-input,
#: custom-python-source): never streamBinding / streamProtocol.
PRE_FEATURE_POINT_KEYS = {
    "nodeId", "nodeType", "parameters", "slots", "bindingHint",
    "adapterBinding", "csiSensorBinding", "aravisBinding",
    "pythonSourceBinding",
}

#: The keys this feature introduces. None of them may appear anywhere in a
#: stream-free workflow's packaged output.
STREAM_POINT_KEYS = ("streamBinding", "streamProtocol")

#: Representative architecture-floor map configurations, each exercised
#: against every feature-floor configuration below.
FLOOR_MAP_CASES = {
    # The deployed-nothing case: every arch resolves to the scalar.
    "unconfigured": {},
    # Fully configured with distinct values per lineage.
    "complete": {"arm64_cpu": "1.0.3", "arm64_jp5": "1.0.7",
                 "arm64_jp6": "1.1.0", "arm64_jp7": "1.2.0",
                 "x86_64": "1.0.9", "x86_64_nvidia": "1.0.10"},
    # Configured but missing two KNOWN archs -> the safe per-lineage floor.
    "partial": {"arm64_jp6": "1.1.0", "x86_64": "1.0.9",
                "x86_64_nvidia": "1.0.9", "arm64_cpu": "1.0.3"},
    # Degenerate single entry: one arch mapped, the rest fall through.
    "single_entry": {"arm64_jp6": "1.4.2"},
}

#: Feature-floor map configurations. The stream-free resolution must be
#: identical under all of them, including the deployed pre-enablement empty
#: map and a map whose values dwarf every architecture floor.
FEATURE_FLOOR_CASES = {
    "empty_deployed_state": {},
    "all_archs_high": {arch: HIGH_FEATURE_FLOOR for arch in ARCHS},
    "some_archs_high": {"arm64_jp6": HIGH_FEATURE_FLOOR,
                        "x86_64": HIGH_FEATURE_FLOOR},
}


def pre_feature_floor(arch, arch_floor_map):
    """HEAD's ``min_local_server_version_for``, re-spelled as an oracle.

    The pre-feature body was exactly: the per-arch entry when mapped; else
    the safe per-lineage floor when the map is configured and the arch is a
    KNOWN arch; else the scalar default. The feature floor plays no part.
    """
    if arch and arch in arch_floor_map:
        return arch_floor_map[arch]
    if arch_floor_map and arch in GOLDEN_LOCAL_SERVER_VARIANTS:
        return GOLDEN_SAFE_LINEAGE_FLOOR
    return GOLDEN_SCALAR_FLOOR


def _version_key(version):
    return tuple(int(token) if token.isdigit() else 0
                 for token in str(version).split('.'))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (portal DynamoDB / S3) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture
def floors(packaging, monkeypatch):
    """Configure both floor maps as module constants (they are parsed from
    the environment once at import)."""
    def configure(arch_floor_map, feature_floor_map):
        monkeypatch.setattr(packaging, ARCH_FLOOR_ATTR, dict(arch_floor_map))
        monkeypatch.setattr(packaging, FEATURE_FLOOR_ATTR,
                            dict(feature_floor_map))
    return configure


def graph_of(definition):
    result = parse(json.dumps(definition))
    assert result.ok, getattr(result, "error", None)
    return result.graph


def unified_definition(parameters):
    """A ``unified_input`` source on a PRE-FEATURE source kind -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "u", "type": "unified_input", "position": {"x": 0, "y": 0},
             "parameters": dict(parameters)},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "u", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


#: The pre-feature source vocabulary a production workflow can be built
#: from. None of these may be seen as a stream/analytics workflow.
STREAM_FREE_DEFINITIONS = {
    "folder": cameraless_definition,
    "icam": icam_definition,
    "csi": csi_definition,
    "aravis": aravis_definition,
    "mixed_cameras": mixed_camera_definition,
    "unified_folder": lambda: unified_definition(
        {"source_kind": "folder", "folder_path": "/in"}),
    "unified_aravis": lambda: unified_definition(
        {"source_kind": "aravis_camera", "camera_id": "Aravis-Fake-GV01"}),
}


def assert_no_stream_key(value, where):
    """No streamBinding / streamProtocol key anywhere in the tree."""
    if isinstance(value, dict):
        for key in STREAM_POINT_KEYS:
            assert key not in value, (
                f"{where}: a stream-free workflow's packaged output carries "
                f"the {key!r} key introduced by rtsp-rtmp-stream-cameras "
                f"(Requirement 18.1 forbids any change to its output)")
        for child in value.values():
            assert_no_stream_key(child, where)
    elif isinstance(value, list):
        for child in value:
            assert_no_stream_key(child, where)


# --------------------------------------------------------------------------
# 1. Floors resolve exactly as before
# --------------------------------------------------------------------------

class TestRecordedPreFeatureContract:
    def test_recorded_pre_feature_constants_still_hold(self, packaging):
        # Validates: Requirement 18.1
        # The oracle above spells these literally; if the module's values
        # move, the oracle (not the module) is what must be re-derived.
        assert packaging.MIN_LOCAL_SERVER_VERSION == GOLDEN_SCALAR_FLOOR
        assert packaging.SAFE_LINEAGE_FLOOR == GOLDEN_SAFE_LINEAGE_FLOOR
        assert packaging.ARCH_TO_LOCAL_SERVER_COMPONENT == \
            GOLDEN_LOCAL_SERVER_VARIANTS
        assert set(DEVICE_ARCHITECTURES) == set(ARCHS)

    def test_stream_features_defaults_to_false_on_every_widened_signature(
            self, packaging):
        # Validates: Requirement 18.1
        # Every existing call site calls these without the new keyword, so
        # the default IS the backward-compatibility contract.
        for name in ("min_local_server_version_for",
                     "min_local_server_versions_map",
                     "local_server_component_dependencies",
                     "build_manifest"):
            parameter = signature(
                getattr(packaging, name)).parameters["stream_features"]
            assert parameter.default is False, (
                f"{name}() must default stream_features to False so a "
                "workflow without the new node types resolves its floors "
                "exactly as before (Requirement 18.1)")


class TestDefaultFloorResolutionIsPreFeature:
    @pytest.mark.parametrize("arch_case", sorted(FLOOR_MAP_CASES))
    @pytest.mark.parametrize("feature_case", sorted(FEATURE_FLOOR_CASES))
    def test_every_architecture_resolves_to_the_pre_feature_oracle(
            self, packaging, floors, arch_case, feature_case):
        # Validates: Requirement 18.1
        arch_floor_map = FLOOR_MAP_CASES[arch_case]
        floors(arch_floor_map, FEATURE_FLOOR_CASES[feature_case])
        for arch in ARCHS:
            expected = pre_feature_floor(arch, arch_floor_map)
            assert packaging.min_local_server_version_for(arch) == expected, (
                f"arch floor map {arch_case!r} + feature floor "
                f"{feature_case!r}: {arch} resolved "
                f"{packaging.min_local_server_version_for(arch)!r} instead of "
                f"the pre-feature {expected!r} — a workflow without the new "
                "node types must resolve its floor unchanged (18.1)")

    @pytest.mark.parametrize("arch_case", sorted(FLOOR_MAP_CASES))
    def test_unknown_and_missing_architectures_still_fall_back_to_the_scalar(
            self, packaging, floors, arch_case):
        # Validates: Requirement 18.1
        # An arch outside ARCH_TO_LOCAL_SERVER_COMPONENT (and a None arch)
        # took the scalar before the feature and still does — notably it
        # does NOT raise, which is what the stream path does for an
        # unmapped arch.
        arch_floor_map = FLOOR_MAP_CASES[arch_case]
        floors(arch_floor_map, {arch: HIGH_FEATURE_FLOOR
                                for arch in ARCHS + ["riscv64"]})
        for arch in ("riscv64", "", None):
            assert packaging.min_local_server_version_for(arch) == \
                pre_feature_floor(arch, arch_floor_map) == GOLDEN_SCALAR_FLOOR

    @pytest.mark.parametrize("feature_case", sorted(FEATURE_FLOOR_CASES))
    def test_versions_map_default_returns_the_architecture_map_untouched(
            self, packaging, floors, feature_case):
        # Validates: Requirement 18.1
        # manifest.json's per-arch map is the field a variant-aware device
        # reads in preference to the scalar, so an accidentally raised map
        # would change an existing workflow's deployability.
        arch_floor_map = FLOOR_MAP_CASES["complete"]
        floors(arch_floor_map, FEATURE_FLOOR_CASES[feature_case])
        resolved = packaging.min_local_server_versions_map()
        assert resolved == arch_floor_map
        # A fresh copy, exactly like the pre-feature dict(MIN_LOCAL_SERVER_
        # VERSIONS): mutating the result cannot corrupt the module constant.
        resolved["arm64_jp6"] = "0.0.1"
        assert packaging.MIN_LOCAL_SERVER_VERSIONS == arch_floor_map


# --------------------------------------------------------------------------
# 2. LocalServer ComponentDependencies entries are unchanged
# --------------------------------------------------------------------------

class TestStreamFreeDependencyEntriesUnchanged:
    ARCH_SETS = [[arch] for arch in ARCHS] + [["x86_64", "x86_64_nvidia"]]

    @pytest.mark.parametrize("archs", ARCH_SETS,
                             ids=lambda archs: "+".join(archs))
    @pytest.mark.parametrize("arch_case", sorted(FLOOR_MAP_CASES))
    def test_single_variant_arch_sets_emit_the_pre_feature_entry(
            self, packaging, floors, archs, arch_case):
        # Validates: Requirement 18.1
        arch_floor_map = FLOOR_MAP_CASES[arch_case]
        floors(arch_floor_map, {arch: HIGH_FEATURE_FLOOR for arch in ARCHS})
        expected_floor = max(
            (pre_feature_floor(arch, arch_floor_map) for arch in archs),
            key=_version_key)
        assert packaging.local_server_component_dependencies(archs) == {
            GOLDEN_LOCAL_SERVER_VARIANTS[archs[0]]: {
                "VersionRequirement": ">=" + expected_floor,
                "DependencyType": "HARD",
            }}

    def test_multi_variant_arch_set_still_emits_nothing(
            self, packaging, floors):
        # Validates: Requirement 18.1
        # edge-deploy-reliability Defect F: a recipe-global dependency
        # closure spanning variants is undeployable, so the entry is
        # omitted. The feature floor must not resurrect it.
        floors(FLOOR_MAP_CASES["complete"],
               {arch: HIGH_FEATURE_FLOOR for arch in ARCHS})
        assert packaging.local_server_component_dependencies(ARCHS) == {}


# --------------------------------------------------------------------------
# 3. manifest.json is unchanged
# --------------------------------------------------------------------------

def _manifest_kwargs():
    return dict(gst_plugins=[], python_packages=[], custom_python_nodes=[],
                user={"user_id": "u1"}, workflow_name="stream free")


class TestStreamFreeManifestUnchanged:
    def test_manifest_key_set_carries_no_new_key(self, packaging, floors):
        # Validates: Requirement 18.1
        floors(FLOOR_MAP_CASES["complete"],
               {arch: HIGH_FEATURE_FLOOR for arch in ARCHS})
        manifest = packaging.build_manifest(
            "wf-1", 1, "arm64_jp6", **_manifest_kwargs())
        assert set(manifest) == GOLDEN_MANIFEST_KEYS
        # The one conditional pre-feature key still behaves as before.
        with_topics = packaging.build_manifest(
            "wf-1", 1, "arm64_jp6", subscribed_topics=["dda/t"],
            **_manifest_kwargs())
        assert set(with_topics) == GOLDEN_MANIFEST_KEYS | {"subscribed_topics"}
        assert_no_stream_key(manifest, "build_manifest")

    @pytest.mark.parametrize("arch_case", sorted(FLOOR_MAP_CASES))
    @pytest.mark.parametrize("feature_case", sorted(FEATURE_FLOOR_CASES))
    def test_both_floor_fields_equal_the_pre_feature_oracle(
            self, packaging, floors, arch_case, feature_case):
        # Validates: Requirement 18.1
        arch_floor_map = FLOOR_MAP_CASES[arch_case]
        floors(arch_floor_map, FEATURE_FLOOR_CASES[feature_case])
        for arch in ARCHS:
            manifest = packaging.build_manifest(
                "wf-1", 1, arch, **_manifest_kwargs())
            assert manifest["minLocalServerVersion"] == \
                pre_feature_floor(arch, arch_floor_map)
            assert manifest["minLocalServerVersions"] == arch_floor_map

    @pytest.mark.parametrize("arch_case", sorted(FLOOR_MAP_CASES))
    def test_manifest_is_byte_identical_across_feature_floor_configurations(
            self, packaging, floors, monkeypatch, arch_case):
        # Validates: Requirement 18.1
        # The strongest statement of 18.1 at this seam: configuring (or not
        # configuring) the feature floor cannot change one byte of a
        # stream-free workflow's manifest. packagedAt is frozen so the only
        # remaining variable is the floor configuration.
        monkeypatch.setattr(packaging, "now_ms", lambda: 1_700_000_000_000)
        arch_floor_map = FLOOR_MAP_CASES[arch_case]
        rendered = set()
        for feature_floor_map in FEATURE_FLOOR_CASES.values():
            floors(arch_floor_map, feature_floor_map)
            rendered.add(json.dumps(
                [packaging.build_manifest("wf-1", 1, arch,
                                          **_manifest_kwargs())
                 for arch in ARCHS], sort_keys=True))
        assert len(rendered) == 1, (
            "a stream-free workflow's manifest.json differs between feature-"
            "floor configurations; Requirement 18.1 requires byte-identical "
            "pre-feature output")


# --------------------------------------------------------------------------
# 4. Stream-free graphs never enter the new code paths
# --------------------------------------------------------------------------

class TestStreamFreeGraphsAreNeverFeatureGraphs:
    @pytest.mark.parametrize("definition_name", sorted(STREAM_FREE_DEFINITIONS))
    @pytest.mark.parametrize("feature_case", sorted(FEATURE_FLOOR_CASES))
    def test_no_feature_nodes_and_no_gate_findings_on_any_architecture(
            self, packaging, floors, definition_name, feature_case):
        # Validates: Requirement 18.1
        floors(FLOOR_MAP_CASES["complete"], FEATURE_FLOOR_CASES[feature_case])
        graph = graph_of(STREAM_FREE_DEFINITIONS[definition_name]())
        assert packaging.gather_stream_feature_node_ids(graph) == []
        assert packaging.stream_feature_arch_gate_findings(graph, ARCHS) == []
        # And the node types themselves are outside the feature family.
        for node in graph.nodes:
            assert packaging.effective_node_type(node) not in \
                packaging.STREAM_FEATURE_TYPE_IDS

    def test_existing_camera_types_keep_their_binding_point_shape(
            self, packaging):
        # Validates: Requirement 18.1
        # The three pre-feature Camera_Input_Node types in one definition:
        # each keeps its own marker (icam a device slot, CSI
        # csiSensorBinding, aravis aravisBinding) and none acquires a
        # stream key on any architecture.
        definition = mixed_camera_definition()
        graph = graph_of(definition)
        camera_nodes = packaging.gather_camera_input_nodes(graph, set())
        assert [node.id for node in camera_nodes] == ["cam", "csi", "arv"]
        hints = packaging.binding_hints_from_definition(definition)
        catalog = resolve_catalog([])
        descriptors_by_id = {d.type_id: d for d in catalog}
        context = CompileContext(workflow_id="wf-18-1", workflow_version="1")

        for arch in ARCHS:
            compiled = compile_workflow(graph, arch, context,
                                        simulation=False, catalog=catalog)
            assert not isinstance(compiled, list), (arch, compiled)
            compiled_dict = compiled.to_dict()
            points = packaging.build_binding_points(
                camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
            by_id = {point["nodeId"]: point for point in points}
            assert set(by_id) == {"cam", "csi", "arv"}
            for point in points:
                assert set(point) <= PRE_FEATURE_POINT_KEYS, (
                    f"{arch}: binding point for {point['nodeId']} gained "
                    f"key(s) {sorted(set(point) - PRE_FEATURE_POINT_KEYS)}")
            assert by_id["csi"].get("csiSensorBinding") is True
            assert by_id["arv"].get("aravisBinding") is True
            assert by_id["cam"]["slots"], (
                f"{arch}: the icam device slot binding disappeared")
            assert_no_stream_key(
                json.loads(packaging.compiled_document_json(compiled, points)),
                f"compiled document ({arch})")


# --------------------------------------------------------------------------
# 5. The handler agrees end to end
# --------------------------------------------------------------------------

class TestHandlerPackagesStreamFreeWorkflowsUnchanged:
    """End-to-end through ``workflow_packaging.handler``, reusing the
    binding-point suite's packaging harness."""

    @staticmethod
    def _manifest(harness, arch):
        key = (f"{COMPONENTS_ROOT}/{harness.workflow_id}/1/1.0.0/"
               f"{arch}/workflow-{arch}.zip")
        body = harness.env.s3.get_object(
            Bucket=harness.usecase_bucket, Key=key)["Body"].read()
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return zf.read("manifest.json").decode("utf-8")

    @pytest.mark.parametrize("feature_case", sorted(FEATURE_FLOOR_CASES))
    def test_camera_workflow_packages_with_its_pre_feature_floors(
            self, env, packaging, monkeypatch, floors, feature_case):
        # Validates: Requirement 18.1
        # 'empty_deployed_state' is the CURRENTLY DEPLOYED feature floor:
        # every stream workflow is rejected on every arch, and an existing
        # camera workflow must be entirely unaffected by that.
        arch_floor_map = FLOOR_MAP_CASES["complete"]
        floors(arch_floor_map, FEATURE_FLOOR_CASES[feature_case])
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   icam_definition())
        status, payload = harness.package(ARCHS)
        assert status == 201, payload
        harness.greengrass.create_component_version.assert_called_once()
        for arch in ARCHS:
            manifest = json.loads(self._manifest(harness, arch))
            assert manifest["minLocalServerVersion"] == \
                pre_feature_floor(arch, arch_floor_map), arch
            assert manifest["minLocalServerVersions"] == arch_floor_map, arch
            assert set(manifest) == GOLDEN_MANIFEST_KEYS, arch
            assert_no_stream_key(manifest, f"manifest.json ({arch})")
            document = harness.compiled_pipeline(arch)
            assert_no_stream_key(document, f"compiled_pipeline.json ({arch})")
            assert [point["nodeId"] for point
                    in document["bindingPoints"]] == ["cam"]

    def test_cameraless_workflow_is_unaffected_by_the_feature_floor(
            self, env, packaging, monkeypatch, floors):
        # Validates: Requirement 18.1
        # No Camera_Input_Node at all: no bindingPoints section, no gate,
        # pre-feature floors, and the version item's discriminator false.
        floors({}, {arch: HIGH_FEATURE_FLOOR for arch in ARCHS})
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   cameraless_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = json.loads(self._manifest(harness, "arm64_jp6"))
        assert manifest["minLocalServerVersion"] == GOLDEN_SCALAR_FLOOR
        assert manifest["minLocalServerVersions"] == {}
        document = harness.compiled_pipeline("arm64_jp6")
        assert "bindingPoints" not in document
        assert_no_stream_key(document, "compiled_pipeline.json (cameraless)")
        item = harness.version_item()
        assert item.get("has_binding_points") in (False, None)

    def test_single_arch_recipe_dependency_floor_is_unraised(
            self, env, packaging, monkeypatch, floors):
        # Validates: Requirement 18.1
        # The Greengrass-side floor a device actually resolves against.
        arch_floor_map = {"arm64_jp6": "1.1.0"}
        floors(arch_floor_map, {arch: HIGH_FEATURE_FLOOR for arch in ARCHS})
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   icam_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        raw = harness.greengrass.create_component_version.call_args.kwargs[
            "inlineRecipe"]
        recipe = json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        assert recipe["ComponentDependencies"][
            GOLDEN_LOCAL_SERVER_VARIANTS["arm64_jp6"]][
                "VersionRequirement"] == ">=1.1.0"
