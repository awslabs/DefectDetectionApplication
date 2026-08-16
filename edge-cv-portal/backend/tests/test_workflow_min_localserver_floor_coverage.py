"""
Permanent coverage/lockstep guard: jp7-workflow-min-localserver-floor.

Property 4: Fix Checking - The Coverage Test Pins All Three Vocabularies To
The Deployed Literal (spec .kiro/specs/jp7-workflow-min-localserver-floor,
design File 5 / Decision 3; requirement 2.4's build-time pin).

The cb139a40 incident class is "a known arch missing from the deployed
WORKFLOW_MIN_LOCAL_SERVER_VERSIONS floor map silently inherits the
cross-lineage scalar". This suite closes the recurrence class at test time
by holding three vocabularies in lockstep with the REAL deployed literal
parsed out of ``edge-cv-portal/infrastructure/lib/compute-stack.ts``:

1. the literal's key set == ``workflow_packaging.ARCH_TO_LOCAL_SERVER_COMPONENT``'s
   key set (the packager arch vocabulary),
2. ``deployments.LOCAL_SERVER_ARCH_IDS`` ⊆ the literal's keys (the gate-side
   completion vocabulary - a strict subset is expected: both x86 flavors
   collapse to the single ``x86_64`` gate arch),
3. ``LOCAL_SERVER_ARCH_IDS`` == ``local_server_component_arch``'s observable
   codomain (driven with every real component name plus the legacy bare
   ``.arm64``/``.aarch64`` JP4 names).

A future fan-out (e.g. JP8) that adds an arch to any one vocabulary without
the others - the exact omission that produced cb139a40 - fails here, before
anything is deployed. The literal extractor is shared with the exploration
suite (task 1); its anchor disappearing raises LOUDLY, because the anchor
vanishing is itself a coverage-relevant change someone must look at.

Runs from edge-cv-portal/backend WITH conftest (the moto ``aws_stack``
fixture backs the module imports):
    python3 -m pytest tests/test_workflow_min_localserver_floor_coverage.py \
        -q -p no:cacheprovider
"""
import re
import sys

import pytest

# Shared literal extractor (design Decision 3: single extractor, reused).
# It raises AssertionError with a loud message when compute-stack.ts moved
# or the JSON.stringify({...}) anchor was renamed/restructured.
from test_jp7_localserver_floor_exploration import (
    COMPUTE_STACK_TS,
    extract_floor_map_literal,
    read_compute_stack_source,
)

#: Semantic version shape every floor value must carry (plain N.N.N - the
#: Greengrass VersionRequirement floors are exact three-part versions).
_SEMVER = re.compile(r"\d+\.\d+\.\d+")

#: Legacy bare JetPack 4 component names: retired on the write side but
#: still recognized on read for already-provisioned JP4 devices - they are
#: part of local_server_component_arch's observable input vocabulary.
_LEGACY_JP4_COMPONENT_NAMES = (
    "aws.edgeml.dda.LocalServer.arm64",
    "aws.edgeml.dda.LocalServer.aarch64",
)


@pytest.fixture(scope="module")
def floor_map():
    """The deployed WORKFLOW_MIN_LOCAL_SERVER_VERSIONS literal, parsed from
    the actual compute-stack.ts (fails loudly when the anchor is missing)."""
    return extract_floor_map_literal(read_compute_stack_source())


@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


class TestFloorMapCoverageLockstep:
    """Property 4 - the three-vocabulary lockstep pin."""

    def test_literal_parses_loudly_and_is_nonempty(self):
        # Validates: Requirements 2.4
        # extract_floor_map_literal raises AssertionError (LOUD, with the
        # file path and the anchor name) when the JSON.stringify({...})
        # anchor is missing - the anchor disappearing is itself a
        # coverage-relevant change. An empty parse would let every other
        # assertion fail confusingly, so pin non-emptiness here.
        literal = extract_floor_map_literal(read_compute_stack_source())
        assert literal, (
            f"WORKFLOW_MIN_LOCAL_SERVER_VERSIONS literal in "
            f"{COMPUTE_STACK_TS} parsed EMPTY - the deployed floor map has "
            "no per-arch entries; every known arch would take the hardened "
            "fallback path instead of an explicit per-lineage floor")

    def test_literal_keys_equal_packaging_arch_vocabulary(
            self, floor_map, packaging):
        # Validates: Requirements 2.4
        literal_keys = set(floor_map)
        packager_archs = set(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        missing = packager_archs - literal_keys
        extra = literal_keys - packager_archs
        assert literal_keys == packager_archs, (
            "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS must cover EXACTLY the "
            "packager arch vocabulary (ARCH_TO_LOCAL_SERVER_COMPONENT). "
            f"Missing from the literal: {sorted(missing)} (each would take "
            "the '1.0.0'+warning fallback instead of an explicit "
            "per-lineage floor - the cb139a40 omission shape); unknown "
            f"extra literal keys: {sorted(extra)} (dead entries no arch "
            "resolves - likely a typo'd arch id). Fix compute-stack.ts "
            "and/or workflow_packaging.py so both vocabularies move "
            "together (design Decision 3/4)")

    def test_deployments_arch_ids_subset_of_literal(
            self, floor_map, deployments):
        # Validates: Requirements 2.4
        gate_archs = set(deployments.LOCAL_SERVER_ARCH_IDS)
        missing = gate_archs - set(floor_map)
        assert not missing, (
            f"deployments.LOCAL_SERVER_ARCH_IDS entries {sorted(missing)} "
            "are absent from the deployed WORKFLOW_MIN_LOCAL_SERVER_VERSIONS "
            "literal - the gate-side map completion would fill them with "
            "the safe floor at every cold start (loud warning each time) "
            "instead of the CDK carrying an explicit per-lineage entry. "
            "Add the key(s) to compute-stack.ts")

    def test_arch_ids_match_component_arch_codomain(
            self, packaging, deployments):
        # Validates: Requirements 2.4
        # Drive the REAL classifier with every component name the packager
        # can emit, plus the legacy bare JP4 names it must keep recognizing
        # on read. The set of arch ids it can produce (its observable
        # codomain) must equal LOCAL_SERVER_ARCH_IDS - the constant
        # deployments.py uses to complete the floor map (it cannot import
        # workflow_packaging; this test IS the documented lockstep pin).
        component_names = (
            tuple(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT.values())
            + _LEGACY_JP4_COMPONENT_NAMES)
        codomain = {}
        for name in component_names:
            arch = deployments.local_server_component_arch(name)
            assert arch is not None, (
                f"local_server_component_arch({name!r}) returned None for a "
                "name the packager emits (or a legacy name it must keep "
                "recognizing) - a device running this component would be "
                "arch-undetermined and gated against the cross-lineage "
                "scalar")
            codomain[name] = arch

        assert set(codomain.values()) == set(
            deployments.LOCAL_SERVER_ARCH_IDS), (
            "local_server_component_arch's observable codomain "
            f"{sorted(set(codomain.values()))} != "
            f"deployments.LOCAL_SERVER_ARCH_IDS "
            f"{sorted(deployments.LOCAL_SERVER_ARCH_IDS)} (per-name: "
            f"{codomain}). The map-completion constant and the classifier "
            "drifted apart - update LOCAL_SERVER_ARCH_IDS and/or the "
            "classifier so the gate completes exactly the archs devices "
            "can report (design File 3)")

    def test_every_literal_value_is_wellformed_semver(self, floor_map):
        # Validates: Requirements 2.4
        malformed = {
            arch: version for arch, version in floor_map.items()
            if not _SEMVER.fullmatch(version)
        }
        assert not malformed, (
            f"WORKFLOW_MIN_LOCAL_SERVER_VERSIONS carries malformed floor "
            f"value(s) {malformed} - each must be a plain N.N.N version "
            "(they become '>=N.N.N' Greengrass VersionRequirements and "
            "packaging.version comparisons)")
