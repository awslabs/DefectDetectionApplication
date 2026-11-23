"""
RF-DETR-Seg feature-floor coverage guard: rfdetr-seg-support Requirement
14.8 (a), (d), (e), design DD8 (spec task 14.4).

A workflow referencing an RF-DETR-Seg model needs a LocalServer build that
carries the Seg device contract; the packager resolves such a workflow's
floors as the MAXIMUM of the lineage, stream and Seg FEATURE floors
(``WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS``) and rejects any requested
architecture missing from the Seg map with ``RFDETR_SEG_UNSUPPORTED_ARCH``.

This suite is the coverage/lockstep half of that mechanism, modelled on
``test_stream_camera_feature_floor_coverage.py`` (the teeth half is
``test_rfdetr_seg_feature_floor.py``):

1. the DEPLOYED Seg feature-floor literal in
   ``edge-cv-portal/infrastructure/lib/compute-stack.ts`` covers exactly
   ``workflow_packaging.ARCH_TO_LOCAL_SERVER_COMPONENT`` minus the
   architectures listed in ``UNVERIFIED_RFDETR_SEG_ARCHES`` once it carries
   any entry, with well-formed ``N.N.N`` values — so a future arch fan-out
   cannot add an architecture that silently has no Seg floor, and an
   architecture is left out only by a recorded decision;
2. the packager parses exactly the shape the literal travels as, and the
   gate code is the spelled contract;
3. with the deployed literal the gate accepts every verified architecture
   and rejects exactly the unverified ones by name (once non-empty);
4. a workflow without a Seg model is never gated and packages with its
   floors unchanged whatever the literal holds (Requirement 17.1).

Pre-enablement state: the literal is deployed EMPTY (``JSON.stringify({})``)
until the six Seg-capable LocalServer builds of Requirement 14.5 exist; the
six entries are then entered in ONE change (Requirement 14.8 (d), spec group
20), an arch whose build or hardware verification is late being listed in
``UNVERIFIED_RFDETR_SEG_ARCHES`` with its reason instead. Empty is
FAIL-CLOSED, not a gap — every architecture is rejected — so the assertions
below tolerate an absent or empty literal and pin the covering contract the
moment it carries an entry.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module import):

    python3 -m pytest tests/test_rfdetr_seg_feature_floor_coverage.py \
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
# The depth-aware ``{env}: JSON.stringify({...})`` extractor of the stream
# floor guard: None for an absent literal, {} for the empty one.
from test_stream_camera_feature_floor_coverage import extract_env_json_map
# The packaging harness that drives the real handler end to end (a validated
# workflow version, a Use_Case bucket, patched Use_Case-account clients).
from test_workflow_packaging_binding_points import (
    BindingPointsEnv,
    COMPONENTS_ROOT,
)

#: The deployed Seg feature-floor env var (design DD8).
FEATURE_FLOOR_ENV = 'WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS'

#: Architectures with no Seg-capable LocalServer build, or none verified,
#: each with the reason it is left out of the floor map (Requirement
#: 14.8 (d) iii: such an architecture has no floor and fails closed with
#: RFDETR_SEG_UNSUPPORTED_ARCH, and the feature is not declared shipped for
#: it). Every other architecture in ARCH_TO_LOCAL_SERVER_COMPONENT must have
#: a floor once the literal carries any entry. Remove an entry here in the
#: same change that adds its floor to compute-stack.ts. Empty pre-enablement:
#: spec group 20 fills the literal and records the unverified set together.
UNVERIFIED_RFDETR_SEG_ARCHES = {}

#: Semantic version shape every floor value must carry (plain N.N.N).
_SEMVER = re.compile(r"\d+\.\d+\.\d+")

SEG_MODEL_NAME = 'coverage-seg'


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
    """The deployed Seg feature-floor literal: a dict ({} pre-enablement),
    or None if the env entry were ever removed from compute-stack.ts."""
    return extract_env_json_map(FEATURE_FLOOR_ENV, read_compute_stack_source())


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


# --------------------------------------------------------------------------
# 1. The deployed literal covers exactly the packager arch vocabulary
# --------------------------------------------------------------------------
class TestDeployedFeatureFloorCoverage:
    """The map's keys pinned to ARCH_TO_LOCAL_SERVER_COMPONENT."""

    def test_literal_is_present_in_compute_stack(self, deployed_feature_floor):
        # Validates: Requirements 14.8
        # Task 14.3 added the literal (empty); its removal would silently
        # leave both Lambdas without the env var — the same fail-closed
        # result, but no longer a deliberate, visible one.
        assert deployed_feature_floor is not None, (
            f"{FEATURE_FLOOR_ENV} is missing from {COMPUTE_STACK_TS}; the "
            "Seg feature floor literal must be present (empty "
            "pre-enablement) beside the stream feature floor")

    def test_deployed_literal_keys_equal_packaging_arch_vocabulary(
            self, deployed_feature_floor, packaging):
        # Validates: Requirements 14.8
        if not deployed_feature_floor:
            pytest.skip(
                f"{FEATURE_FLOOR_ENV} is deployed as the EMPTY map "
                "(pre-enablement state, spec group 20 fills it in one change "
                "once the six Seg-capable LocalServer builds exist). "
                "Fail-closed: every architecture is rejected, which "
                "TestFeatureFloorGateTeeth pins directly")
        literal_keys = set(deployed_feature_floor)
        packager_archs = set(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        expected = packager_archs - set(UNVERIFIED_RFDETR_SEG_ARCHES)
        missing = expected - literal_keys
        extra = literal_keys - expected
        assert literal_keys == expected, (
            f"{FEATURE_FLOOR_ENV} must cover EXACTLY the packager arch "
            "vocabulary (ARCH_TO_LOCAL_SERVER_COMPONENT) minus the "
            f"architectures listed as unverified ({sorted(UNVERIFIED_RFDETR_SEG_ARCHES)}). "
            f"Missing from the literal: {sorted(missing)} - a workflow "
            "referencing an RF-DETR-Seg model is REJECTED outright on each "
            "of those architectures (RFDETR_SEG_UNSUPPORTED_ARCH), so a "
            "partially filled map silently makes the feature unavailable "
            "there; list an architecture in UNVERIFIED_RFDETR_SEG_ARCHES "
            "only when that is the recorded decision (Requirement 14.8 d). "
            f"Unexpected literal keys: {sorted(extra)} (dead entries no arch "
            "resolves - likely a typo'd arch id - or an unverified "
            "architecture given a floor without removing it from "
            "UNVERIFIED_RFDETR_SEG_ARCHES). Fix compute-stack.ts and/or "
            "workflow_packaging.py so both vocabularies move together")

    def test_unverified_architectures_are_known_and_documented(self, packaging):
        # Validates: Requirements 14.8
        # An unverified entry must name a real architecture (a typo would
        # silently exempt nothing) and say why it is left out.
        unknown = set(UNVERIFIED_RFDETR_SEG_ARCHES) - set(
            packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        assert not unknown, (
            f"UNVERIFIED_RFDETR_SEG_ARCHES names unknown architectures "
            f"{sorted(unknown)}; they are not in ARCH_TO_LOCAL_SERVER_COMPONENT")
        assert all(str(reason).strip()
                   for reason in UNVERIFIED_RFDETR_SEG_ARCHES.values())

    def test_deployed_literal_rejects_exactly_the_unverified_architectures(
            self, deployed_feature_floor, packaging, monkeypatch):
        # Validates: Requirements 14.8
        # With the deployed literal, the gate accepts every verified
        # architecture and rejects each unverified one by name.
        if not deployed_feature_floor:
            pytest.skip(f"{FEATURE_FLOOR_ENV} carries no entries yet")
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            dict(deployed_feature_floor))
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        findings = packaging.rfdetr_seg_arch_gate_findings([SEG_MODEL_NAME], archs)
        assert sorted(f['arch'] for f in findings) == \
            sorted(UNVERIFIED_RFDETR_SEG_ARCHES)
        assert all(f['code'] == 'RFDETR_SEG_UNSUPPORTED_ARCH' for f in findings)

    def test_deployed_literal_values_are_wellformed_semver(
            self, deployed_feature_floor):
        # Validates: Requirements 14.8
        if not deployed_feature_floor:
            pytest.skip(f"{FEATURE_FLOOR_ENV} carries no entries yet")
        malformed = {
            arch: version for arch, version in deployed_feature_floor.items()
            if not _SEMVER.fullmatch(version)
        }
        assert not malformed, (
            f"{FEATURE_FLOOR_ENV} carries malformed floor value(s) "
            f"{malformed} - each must be a plain N.N.N version (they are "
            "max()'d against the architecture and stream floors and become "
            "'>=N.N.N' Greengrass VersionRequirements plus manifest.json floors)")

    def test_amd64_pair_takes_one_version(self, deployed_feature_floor):
        # Validates: Requirements 14.8
        # x86_64 and x86_64_nvidia both run aws.edgeml.dda.LocalServer.amd64
        # (ARCH_TO_LOCAL_SERVER_COMPONENT), so once both are filled they
        # must name the same .amd64 build.
        if not deployed_feature_floor:
            pytest.skip(f"{FEATURE_FLOOR_ENV} carries no entries yet")
        if {"x86_64", "x86_64_nvidia"} <= set(deployed_feature_floor):
            assert deployed_feature_floor["x86_64"] == \
                deployed_feature_floor["x86_64_nvidia"]

    def test_packager_parses_the_deployed_literal_shape(
            self, deployed_feature_floor, packaging, monkeypatch):
        # Validates: Requirements 14.8
        # The deployed literal travels as the JSON string JSON.stringify
        # produces; pin that the packager's parser reads exactly it (a
        # JSON-vs-shape mismatch would silently leave the map empty, i.e.
        # reject every architecture).
        literal = deployed_feature_floor or {"arm64_jp6": "1.0.0"}
        monkeypatch.setenv(FEATURE_FLOOR_ENV, json.dumps(literal))
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == literal
        # The empty literal parses to the empty (fail-closed) map, as do
        # malformed and absent configuration.
        monkeypatch.setenv(FEATURE_FLOOR_ENV, json.dumps({}))
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        monkeypatch.setenv(FEATURE_FLOOR_ENV, "not-json{")
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        monkeypatch.delenv(FEATURE_FLOOR_ENV, raising=False)
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        # The other two floor maps are read from their OWN env vars and are
        # unaffected by this one (three independent floors).
        assert packaging._parse_min_versions_map() == \
            packaging._parse_min_versions_map('WORKFLOW_MIN_LOCAL_SERVER_VERSIONS')

    def test_stream_floor_literal_is_not_raised_for_seg(
            self, deployed_feature_floor):
        # Validates: Requirements 14.8
        # 14.8 (f): the lineage floor and the stream literal are untouched by
        # the Seg feature — the Seg floor is a third map, not an edit of the
        # stream one.
        source = read_compute_stack_source()
        stream = extract_env_json_map(
            'WORKFLOW_STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS', source)
        assert stream == {
            "arm64_jp5": "1.0.51", "arm64_jp6": "1.0.74", "arm64_jp7": "1.0.52",
            "x86_64": "1.0.47", "x86_64_nvidia": "1.0.47"}
        lineage = extract_env_json_map('WORKFLOW_MIN_LOCAL_SERVER_VERSIONS', source)
        assert lineage == {
            "arm64_cpu": "1.1.0", "arm64_jp5": "1.0.0", "arm64_jp6": "1.0.0",
            "arm64_jp7": "1.0.0", "x86_64": "1.0.0", "x86_64_nvidia": "1.0.0"}


# --------------------------------------------------------------------------
# 2. The gate code and the discriminator are the spelled contract
# --------------------------------------------------------------------------
class TestGateVocabulary:
    def test_gate_code_is_the_spelled_contract(self, packaging):
        # Validates: Requirements 14.8
        assert packaging.GATE_RFDETR_SEG_ARCH_UNSUPPORTED == \
            'RFDETR_SEG_UNSUPPORTED_ARCH'

    def test_discriminator_is_the_seg_detection_arch(self, packaging):
        # Validates: Requirements 14.8
        # The record field the packager keys the floor on is the Seg arch of
        # the shared-layer vocabulary (detection_training.DETECTION_ARCHES).
        from detection_training import DETECTION_ARCHES
        assert packaging.RFDETR_SEG_DETECTION_ARCH == 'rf_detr_seg'
        assert packaging.RFDETR_SEG_DETECTION_ARCH in DETECTION_ARCHES


# --------------------------------------------------------------------------
# 3. The key set has teeth under the deployed (empty) literal
# --------------------------------------------------------------------------
class TestFeatureFloorGateTeeth:
    def test_deployed_literal_as_parsed_gates_as_configured(
            self, deployed_feature_floor, packaging, monkeypatch):
        # Validates: Requirements 14.8
        # The literal exactly as deployed: pre-enablement every arch is
        # rejected; once filled, exactly the unverified set is.
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS",
            dict(deployed_feature_floor or {}))
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        findings = packaging.rfdetr_seg_arch_gate_findings([SEG_MODEL_NAME], archs)
        if not deployed_feature_floor:
            assert sorted(f['arch'] for f in findings) == archs
        else:
            assert sorted(f['arch'] for f in findings) == \
                sorted(UNVERIFIED_RFDETR_SEG_ARCHES)
        # A workflow without a Seg model is never gated, whatever the map.
        assert packaging.rfdetr_seg_arch_gate_findings([], archs) == []


# --------------------------------------------------------------------------
# 4. Non-Seg workflows are unchanged whatever the literal holds
# --------------------------------------------------------------------------
class TestNonSegWorkflowsUnchanged:
    """End-to-end through ``workflow_packaging.handler``: a workflow without
    a Seg model keeps its floors and gains only the ``False``
    discriminator (Requirement 17.1)."""

    @staticmethod
    def _manifest(harness, arch):
        key = (f"{COMPONENTS_ROOT}/{harness.workflow_id}/1/1.0.0/"
               f"{arch}/workflow-{arch}.zip")
        body = harness.env.s3.get_object(
            Bucket=harness.usecase_bucket, Key=key)["Body"].read()
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return json.loads(zf.read("manifest.json").decode("utf-8"))

    @pytest.mark.parametrize("seg_floor", [
        {}, {"arm64_jp6": "9.9.9"},
    ])
    def test_model_free_workflow_keeps_its_unraised_floors(
            self, env, packaging, monkeypatch, seg_floor):
        # Validates: Requirements 14.8, 17.1
        monkeypatch.setattr(
            packaging, "MIN_LOCAL_SERVER_VERSIONS", {"arm64_jp6": "1.0.0"})
        monkeypatch.setattr(
            packaging, "RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS", dict(seg_floor))
        harness = BindingPointsEnv(env, packaging, monkeypatch,
                                   folder_definition())
        status, payload = harness.package(["arm64_jp6"])
        assert status == 201, payload
        manifest = self._manifest(harness, "arm64_jp6")
        assert manifest["minLocalServerVersion"] == "1.0.0"
        assert manifest["minLocalServerVersions"] == {"arm64_jp6": "1.0.0"}
        raw = harness.greengrass.create_component_version.call_args.kwargs[
            "inlineRecipe"]
        recipe = json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        assert recipe["ComponentDependencies"][
            "aws.edgeml.dda.LocalServer.arm64JP6"][
                "VersionRequirement"] == ">=1.0.0"
        item = harness.version_item()
        assert item["has_rfdetr_seg_model"] is False
        assert item["has_stream_features"] is False
