"""
Feature-floor coverage guard: rtsp-rtmp-stream-cameras Requirement 9.7
(design D14, design component 5 "Feature floor").

A workflow that uses a Stream_Camera_Source_Node or a Scene_Analytics_Node
needs a LocalServer build that understands those node types: an older
LocalServer reads a ``streamBinding`` point as a slot point with no slots
and then stalls on an unfed ``appsrc`` for 120 s. The packager therefore
resolves such a workflow's floor as the MAXIMUM of the per-architecture
floor (WORKFLOW_MIN_LOCAL_SERVER_VERSIONS) and the FEATURE floor
(WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS), and rejects any
architecture missing from the feature floor map with
``STREAM_CAMERAS_UNSUPPORTED_ARCH``.

This suite is the coverage/lockstep half of that mechanism, modelled on
test_workflow_min_localserver_floor_coverage.py (the jp7 floor guard):

1. the DEPLOYED feature-floor literal in
   ``edge-cv-portal/infrastructure/lib/compute-stack.ts`` covers exactly
   ``workflow_packaging.ARCH_TO_LOCAL_SERVER_COMPONENT`` once it carries any
   entry, with well-formed ``N.N.N`` values — so a future arch fan-out
   (e.g. JP8) cannot add an architecture that silently has no feature
   floor, and 26.3 cannot fill the map for only some architectures;
2. the packager's notion of "the new node types" (``STREAM_FEATURE_TYPE_
   IDS``) stays equal to the shared vocabulary (its own stream-protocol map
   plus workflow_core's ``SCENE_ANALYTICS_TYPES``), so a new member of
   either family cannot join the catalog without joining the floor;
3. the key set has TEETH: the gate accepts exactly the map's architectures
   and rejects every other one by name, the resolved floor is the maximum of
   the two floors (raised in BOTH manifest fields and in the LocalServer
   ComponentDependencies entry), and a stream-free workflow resolves exactly
   as before;
4. the gate is WIRED into the packaging handler: an uncovered architecture
   returns 409 ``STREAM_CAMERAS_UNSUPPORTED_ARCH`` and registers no
   component version.

Pre-enablement state: the literal is deliberately absent / empty until the
first supporting LocalServer builds are published (design component 5; spec
tasks 9.2 then 26.3). That is FAIL-CLOSED, not a gap — every architecture
is rejected — so the assertions below tolerate an absent or empty literal
and pin the covering contract the moment it carries an entry.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module import):
    python3 -m pytest tests/test_stream_camera_feature_floor_coverage.py \
        -q -p no:cacheprovider
"""
import io
import json
import re
import sys
import zipfile

import pytest

# Shared compute-stack.ts reader (the jp7 floor guard's extractor module):
# it raises AssertionError with a loud message when compute-stack.ts moved.
from test_jp7_localserver_floor_exploration import (
    COMPUTE_STACK_TS,
    read_compute_stack_source,
)
# The packaging harness that drives the real handler end to end (a validated
# workflow version, a Use_Case bucket, patched Use_Case-account clients).
from test_workflow_packaging_binding_points import (
    BindingPointsEnv,
    COMPONENTS_ROOT,
)

#: The deployed feature-floor env var (design component 5).
FEATURE_FLOOR_ENV = 'WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS'

#: Semantic version shape every floor value must carry (plain N.N.N).
_SEMVER = re.compile(r"\d+\.\d+\.\d+")

_LITERAL_ENTRY = re.compile(
    r"""(['"]?)([A-Za-z0-9_]+)\1\s*:\s*(['"])([^'"]*)\3""")


def extract_env_json_map(env_name, source):
    """The ``{env_name}: JSON.stringify({...})`` object literal from
    compute-stack.ts as a ``{key: value}`` dict, or None when the env entry
    is not present at all (the documented pre-enablement state for the
    feature floor, spec task 9.2).

    Depth-aware brace scan and comment stripping, exactly like the jp7
    extractor, so a nested value or a ``//`` comment cannot truncate the
    parse. An empty literal (``JSON.stringify({})``) parses to ``{}`` —
    distinct from the absent literal's ``None``.
    """
    anchor = re.compile(
        r"%s\s*:\s*JSON\.stringify\s*\(\s*\{" % re.escape(env_name))
    match = anchor.search(source)
    if not match:
        return None
    depth, i = 1, match.end()
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    if depth:
        raise AssertionError(
            f"unbalanced braces in the {env_name} literal in "
            f"{COMPUTE_STACK_TS} - extractor cannot parse compute-stack.ts")
    body = re.sub(r"//[^\n]*", "", source[match.end():i - 1])
    return {key: value for _, key, _, value in _LITERAL_ENTRY.findall(body)}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployed_feature_floor():
    """The deployed feature-floor literal: a dict, or None while the env
    entry has not been added to compute-stack.ts yet (task 9.2)."""
    return extract_env_json_map(FEATURE_FLOOR_ENV, read_compute_stack_source())


def stream_definition(node_type="rtsp_camera_source"):
    """A minimal Stream_Camera_Source_Node workflow: source -> capture."""
    return {
        "schemaVersion": 1,
        "nodes": [
            {"id": "stream", "type": node_type, "position": {"x": 0, "y": 0},
             "parameters": {"url": "rtsp://10.0.0.5:554/s1"}},
            {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
             "parameters": {"output_path": "/out"}},
        ],
        "connections": [
            {"id": "c1", "from": {"node": "stream", "port": "out"},
             "to": {"node": "cap", "port": "in"}},
        ],
    }


def folder_definition():
    """A stream-free workflow (the pre-feature shape): folder -> capture."""
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


def analytics_definition():
    """A Scene_Analytics_Node workflow: the feature floor covers the
    analytics family too, not only the stream sources (design D14)."""
    definition = folder_definition()
    definition["nodes"].insert(1, {
        "id": "cnt", "type": "detection_counter",
        "position": {"x": 150, "y": 0}, "parameters": {}})
    return definition


def graph_of(packaging, definition):
    result = packaging.parse_definition(json.dumps(definition))
    assert result.ok, getattr(result, "error", None)
    return result.graph


# --------------------------------------------------------------------------
# 1. The deployed literal covers exactly the packager arch vocabulary
# --------------------------------------------------------------------------

class TestDeployedFeatureFloorCoverage:
    """The map's keys pinned to ARCH_TO_LOCAL_SERVER_COMPONENT."""

    def test_deployed_literal_keys_equal_packaging_arch_vocabulary(
            self, deployed_feature_floor, packaging):
        # Validates: Requirements 9.7
        if deployed_feature_floor is None:
            pytest.skip(
                f"{FEATURE_FLOOR_ENV} is not in {COMPUTE_STACK_TS} yet "
                "(pre-enablement state, spec task 9.2). The packager's "
                "unconfigured map FAILS CLOSED: every architecture is "
                "rejected with STREAM_CAMERAS_UNSUPPORTED_ARCH, which "
                "TestFeatureFloorGateTeeth pins directly")
        if not deployed_feature_floor:
            pytest.skip(
                f"{FEATURE_FLOOR_ENV} is deployed as the EMPTY map "
                "(pre-enablement state, spec task 26.3 fills it once the "
                "supporting LocalServer builds are published). Fail-closed: "
                "every architecture is rejected")
        literal_keys = set(deployed_feature_floor)
        packager_archs = set(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        missing = packager_archs - literal_keys
        extra = literal_keys - packager_archs
        assert literal_keys == packager_archs, (
            f"{FEATURE_FLOOR_ENV} must cover EXACTLY the packager arch "
            "vocabulary (ARCH_TO_LOCAL_SERVER_COMPONENT). Missing from the "
            f"literal: {sorted(missing)} - a workflow using stream camera or "
            "scene analytics nodes is REJECTED outright on each of those "
            "architectures (STREAM_CAMERAS_UNSUPPORTED_ARCH), so a partially "
            "filled map silently makes the feature unavailable there; "
            f"unknown extra literal keys: {sorted(extra)} (dead entries no "
            "arch resolves - likely a typo'd arch id). Fix compute-stack.ts "
            "and/or workflow_packaging.py so both vocabularies move together")

    def test_deployed_literal_values_are_wellformed_semver(
            self, deployed_feature_floor):
        # Validates: Requirements 9.7
        if not deployed_feature_floor:
            pytest.skip(f"{FEATURE_FLOOR_ENV} carries no entries yet")
        malformed = {
            arch: version for arch, version in deployed_feature_floor.items()
            if not _SEMVER.fullmatch(version)
        }
        assert not malformed, (
            f"{FEATURE_FLOOR_ENV} carries malformed floor value(s) "
            f"{malformed} - each must be a plain N.N.N version (they are "
            "max()'d against the architecture floor and become '>=N.N.N' "
            "Greengrass VersionRequirements plus manifest.json floors)")

    def test_packager_parses_the_deployed_literal_shape(
            self, deployed_feature_floor, packaging, monkeypatch):
        # Validates: Requirements 9.7
        # The deployed literal travels as the JSON string JSON.stringify
        # produces; pin that the packager's parser reads exactly it (a
        # JSON-vs-shape mismatch would silently leave the map empty, i.e.
        # reject every architecture).
        literal = deployed_feature_floor or {"arm64_jp6": "1.0.0"}
        monkeypatch.setenv(FEATURE_FLOOR_ENV, json.dumps(literal))
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == literal
        # Malformed / absent configuration degrades to the empty map, which
        # is fail-closed here (reject) rather than fail-open.
        monkeypatch.setenv(FEATURE_FLOOR_ENV, "not-json{")
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        monkeypatch.delenv(FEATURE_FLOOR_ENV, raising=False)
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        # The architecture floor map is read from its OWN env var and is
        # unaffected by the feature floor's (they are independent floors).
        assert packaging._parse_min_versions_map() == \
            packaging._parse_min_versions_map(
                'WORKFLOW_MIN_LOCAL_SERVER_VERSIONS')


# --------------------------------------------------------------------------
# 2. The feature's node-type vocabulary stays in lockstep with workflow_core
# --------------------------------------------------------------------------

class TestFeatureNodeTypeVocabulary:
    def test_feature_types_are_stream_plus_scene_analytics(self, packaging):
        # Validates: Requirements 9.7
        from workflow_core.validator import SCENE_ANALYTICS_TYPES

        assert packaging.STREAM_FEATURE_TYPE_IDS == (
            set(packaging.STREAM_SOURCE_PROTOCOLS) | set(SCENE_ANALYTICS_TYPES)), (
            "the packager's feature-floor node-type family drifted from the "
            "shared vocabulary: it must be exactly the Stream_Camera_Source_"
            "Node types (STREAM_SOURCE_PROTOCOLS) plus workflow_core's "
            "SCENE_ANALYTICS_TYPES. A family member outside it would package "
            "without the feature floor and stall on an older LocalServer")
        # The concrete members, spelled out so a silent catalog rename is
        # visible here (Requirements 2.1, 13.1, 14.1, 15.1).
        assert packaging.STREAM_FEATURE_TYPE_IDS == {
            "rtsp_camera_source", "rtmp_stream_source",
            "detection_counter", "object_association", "event_gate"}

    def test_gate_code_is_the_spelled_contract(self, packaging):
        # Validates: Requirements 9.7
        assert packaging.GATE_STREAM_ARCH_UNSUPPORTED == \
            'STREAM_CAMERAS_UNSUPPORTED_ARCH'

    def test_stream_source_spelled_as_unified_input_is_covered(self, packaging):
        # Validates: Requirements 9.7
        # The compiler's expand_unified_inputs rewrites a unified node into
        # its source type, so the DEVICE runs a stream source either way;
        # the floor follows the validator's effective-type rule so the
        # unified spelling cannot bypass it.
        unified = {
            "schemaVersion": 1,
            "nodes": [
                {"id": "u", "type": "unified_input", "position": {"x": 0, "y": 0},
                 "parameters": {"source_kind": "rtsp_camera",
                                "url": "rtsp://10.0.0.5:554/s1"}},
                {"id": "cap", "type": "capture", "position": {"x": 200, "y": 0},
                 "parameters": {"output_path": "/out"}},
            ],
            "connections": [
                {"id": "c1", "from": {"node": "u", "port": "out"},
                 "to": {"node": "cap", "port": "in"}},
            ],
        }
        assert packaging.gather_stream_feature_node_ids(
            graph_of(packaging, unified)) == ["u"]
        # A unified node on a pre-feature source kind is NOT covered.
        unified["nodes"][0]["parameters"] = {
            "source_kind": "folder", "folder_path": "/in"}
        assert packaging.gather_stream_feature_node_ids(
            graph_of(packaging, unified)) == []


# --------------------------------------------------------------------------
# 3. The key set has teeth: accepted archs, rejections, and the maximum
# --------------------------------------------------------------------------

class TestFeatureFloorGateTeeth:
    def test_gate_accepts_exactly_the_mapped_architectures(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {arch: "1.2.0" for arch in archs})
        graph = graph_of(packaging, stream_definition())
        assert packaging.stream_feature_arch_gate_findings(graph, archs) == []

    @pytest.mark.parametrize("node_type", ["rtsp_camera_source",
                                           "rtmp_stream_source"])
    def test_unmapped_architecture_is_rejected_by_name(
            self, packaging, monkeypatch, node_type):
        # Validates: Requirements 9.7
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        dropped = "arm64_jp5"
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {arch: "1.2.0" for arch in archs if arch != dropped})
        graph = graph_of(packaging, stream_definition(node_type))
        findings = packaging.stream_feature_arch_gate_findings(graph, archs)
        assert [f['arch'] for f in findings] == [dropped]
        finding = findings[0]
        assert finding['code'] == 'STREAM_CAMERAS_UNSUPPORTED_ARCH'
        assert finding['nodeIds'] == ["stream"]
        assert dropped in finding['message'] and "stream" in finding['message']

    def test_empty_map_rejects_every_architecture(self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        # The pre-enablement state: no LocalServer build supports the node
        # types, so nothing may be packaged with them (fail closed).
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS", {})
        graph = graph_of(packaging, stream_definition())
        findings = packaging.stream_feature_arch_gate_findings(graph, archs)
        assert sorted(f['arch'] for f in findings) == archs
        assert all(f['code'] == 'STREAM_CAMERAS_UNSUPPORTED_ARCH'
                   for f in findings)

    def test_stream_free_workflow_is_never_gated(self, packaging, monkeypatch):
        # Validates: Requirements 9.7, 18.1
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS", {})
        graph = graph_of(packaging, folder_definition())
        assert packaging.gather_stream_feature_node_ids(graph) == []
        assert packaging.stream_feature_arch_gate_findings(
            graph, sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)) == []


class TestFloorIsTheMaximumOfBoth:
    def test_feature_floor_raises_the_architecture_floor(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.3"})
        assert packaging.min_local_server_version_for("arm64_jp6") == "1.0.0"
        assert packaging.min_local_server_version_for(
            "arm64_jp6", stream_features=True) == "1.2.3"

    def test_higher_architecture_floor_wins(self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_cpu": "1.1.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_cpu": "1.0.5"})
        assert packaging.min_local_server_version_for(
            "arm64_cpu", stream_features=True) == "1.1.0"

    def test_comparison_is_numeric_not_lexicographic(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp7": "1.0.9"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp7": "1.0.10"})
        # A string compare would pick '1.0.9'.
        assert packaging.min_local_server_version_for(
            "arm64_jp7", stream_features=True) == "1.0.10"

    def test_unmapped_architecture_fails_closed(self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        # The handler's gate rejects first; this is the backstop for any
        # caller that skipped it - never a silently un-raised floor.
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.0"})
        with pytest.raises(packaging.PackagingError) as excinfo:
            packaging.min_local_server_version_for(
                "arm64_jp5", stream_features=True)
        assert "arm64_jp5" in str(excinfo.value)
        assert "STREAM_CAMERAS_UNSUPPORTED_ARCH" in str(excinfo.value)

    def test_manifest_scalar_and_map_are_both_raised_consistently(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        # The device (workflow_engine.discovery.validate_artifact) reads the
        # per-arch MAP in preference to the scalar, so leaving the map
        # unraised would re-open the bypass the feature floor closes.
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"})
        kwargs = dict(gst_plugins=[], python_packages=[],
                      custom_python_nodes=[], user={"user_id": "u1"})

        plain = packaging.build_manifest("wf-1", 1, "arm64_jp6", **kwargs)
        assert plain["minLocalServerVersion"] == "1.0.0"
        assert plain["minLocalServerVersions"] == {
            "arm64_jp6": "1.0.0", "arm64_jp5": "1.0.5"}

        streamed = packaging.build_manifest(
            "wf-1", 1, "arm64_jp6", stream_features=True, **kwargs)
        assert streamed["minLocalServerVersion"] == "1.2.0"
        assert streamed["minLocalServerVersions"] == {
            "arm64_jp6": "1.2.0", "arm64_jp5": "1.2.0"}
        # The map entry for the package's own targetArch equals the scalar.
        assert streamed["minLocalServerVersions"]["arm64_jp6"] == \
            streamed["minLocalServerVersion"]

    def test_local_server_dependency_requirement_is_raised(
            self, packaging, monkeypatch):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.2.0"})
        plain = packaging.local_server_component_dependencies(["arm64_jp6"])
        streamed = packaging.local_server_component_dependencies(
            ["arm64_jp6"], stream_features=True)
        variant = "aws.edgeml.dda.LocalServer.arm64JP6"
        assert plain[variant]["VersionRequirement"] == ">=1.0.0"
        assert streamed[variant]["VersionRequirement"] == ">=1.2.0"
        assert streamed[variant]["DependencyType"] == "HARD"


# --------------------------------------------------------------------------
# 4. The gate is WIRED into the handler (the rejection is observable)
# --------------------------------------------------------------------------

class TestHandlerAppliesTheFeatureFloor:
    """End-to-end through ``workflow_packaging.handler``, reusing the
    binding-point suite's packaging harness (validated workflow version,
    Use_Case bucket, patched Use_Case-account clients)."""

    @staticmethod
    def _manifest(harness, arch):
        key = (f"{COMPONENTS_ROOT}/{harness.workflow_id}/1/1.0.0/"
               f"{arch}/workflow-{arch}.zip")
        body = harness.env.s3.get_object(
            Bucket=harness.usecase_bucket, Key=key)["Body"].read()
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return json.loads(zf.read("manifest.json").decode("utf-8"))

    @pytest.mark.parametrize("definition_factory", [
        stream_definition,
        lambda: analytics_definition(),
    ])
    def test_uncovered_architecture_is_rejected_and_registers_nothing(
            self, env, packaging, monkeypatch, definition_factory):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS", {})
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   definition_factory())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 409, payload
        assert payload["error"]["code"] == 'STREAM_CAMERAS_UNSUPPORTED_ARCH'
        assert "arm64_jp6" in payload["error"]["message"]
        # Rejected BEFORE any component version exists (7.5 discipline).
        harness.greengrass.create_component_version.assert_not_called()

    def test_covered_architecture_packages_with_the_raised_floor(
            self, env, packaging, monkeypatch):
        # Validates: Requirements 9.7
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0"})
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   stream_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = self._manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.3.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.3.0"}
        raw = harness.greengrass.create_component_version.call_args.kwargs[
            "inlineRecipe"]
        recipe = json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        assert recipe["ComponentDependencies"][
            "aws.edgeml.dda.LocalServer.arm64JP6"][
                "VersionRequirement"] == ">=1.3.0"

    def test_stream_free_workflow_keeps_its_unraised_floors(
            self, env, packaging, monkeypatch):
        # Validates: Requirements 9.7, 18.1
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS",
            {"arm64_jp6": "1.3.0"})
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   folder_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = self._manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.0.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.0.0"}
