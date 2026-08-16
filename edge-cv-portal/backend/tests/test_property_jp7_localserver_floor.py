"""Preservation property tests (Task 2) for jp7-workflow-min-localserver-floor.

**Feature: jp7-workflow-min-localserver-floor, Property 2: Preservation —
Everything Outside The Missing-Key Resolutions Is Unchanged**

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Observation-first: every behavior below was OBSERVED on the UNFIXED tree
(before the CDK floor-map completion and the fallback hardening of design.md)
and is recorded here as the golden contract. These tests PASS on the unfixed
tree and MUST keep passing after the fix — none of them exercises the bug
condition (a KNOWN arch missing from a CONFIGURED, non-empty floor map):

- **jp4/jp5/jp6 identity (3.1)**: with the REAL ``compute-stack.ts`` literal
  loaded, the packager resolves ``'1.0.0'`` from each JetPack arch's own map
  entry — recipe entry ``'>=1.0.0'`` HARD, manifest
  ``minLocalServerVersion: '1.0.0'`` — and a PBT pins that ANY map containing
  the arch resolves exactly its entry (map-entry resolution is untouched by
  the fix by construction).
- **Scalar-chain identity (3.6)**: an EMPTY floor map resolves the scalar for
  every arch (known, unknown, ``None``) — the contract the env-clearing
  preservation suites (e.g. test_workflow_packaging_localserver_
  preservation.py) rely on. The hardening only applies to NON-empty maps.
- **Multi-variant omission, Defect F (3.2)**: >1 distinct LocalServer variant
  → ``{}`` (dependency omitted entirely); the x86_64 + x86_64_nvidia pair
  still collapses to ONE ``.amd64`` entry carrying the max of the two floors.
- **Override uniformity (3.5)**: ``check_local_server_compatibility`` called
  with ``by_arch={}`` (the per-version-override calling convention) gates
  EVERY device against the override, regardless of variant.
- **Legacy recognition (3.6)**: ``local_server_component_arch`` pinned over
  the full component-name vocabulary (JP-tagged, legacy bare arm64/aarch64,
  amd64/x86, junk).
- **Manifest schema (3.4)**: ``build_manifest`` key set and value types
  pinned; only the VALUES for previously-missing archs may change after the
  fix (so no value for jp7/x86 floors is asserted here).
- **Model/plugin dependency identity (3.3)**: covered by the existing-suite
  baselines recorded in tasks.md task 2 (test_workflow_packaging_localserver_
  preservation.py already pins model_component_dependencies /
  plugin_component_dependencies and the merge composition) — no new deep pin.

FIX-CHECK SECTION (Task 4.2 — **Property 3: Fix Checking — A Configured Map
Never Silently Falls Back To The Scalar For A Known Arch**, Requirements
2.3, 3.6): the classes after the "Property 3 fix-check" banner below exercise
the HARDENED behavior on the fixed tree — the packaging resolution partition
(mapped → entry; missing KNOWN arch → '1.0.0' + loud warning, never the
scalar; None/unknown → scalar chain; empty map → scalar for everything), the
deployments-side ``_fill_missing_arch_floors`` completion contract, and the
end-to-end recipe/manifest shape (no cross-lineage scalar can reach a
VersionRequirement or minLocalServerVersion for a known arch unless the map
explicitly carries it).

Harness: ``aws_stack`` from conftest (fresh ``workflow_packaging`` /
``deployments`` imports bind moto-intercepted clients); Hypothesis profiles
are conftest-registered (portal-fast/ci) — no hardcoded max_examples here.
Module floor state is swapped per-example with a try/finally context manager
(not the function-scoped monkeypatch fixture) so PBTs stay Hypothesis-clean.
The fix-check PBTs assert the hardening warnings via ``caplog`` — the only
function-scoped fixture in their signatures — with the matching Hypothesis
health check suppressed and ``caplog.clear()`` at each example start.
"""
import logging
import os
import re
import sys
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from test_workflow_packaging_deployment_integration import FakeGreengrass

# --------------------------------------------------------------------------
# Golden contract, recorded from the UNFIXED tree (NOT imported from the
# modules under test)
# --------------------------------------------------------------------------

#: arch id -> LocalServer variant component name (the fail-closed
#: ARCH_TO_LOCAL_SERVER_COMPONENT discipline; bare '.arm64' never emitted).
LOCAL_SERVER_VARIANTS = {
    "arm64_jp4": "aws.edgeml.dda.LocalServer.arm64JP4",
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "arm64_jp7": "aws.edgeml.dda.LocalServer.arm64JP7",
    "x86_64": "aws.edgeml.dda.LocalServer.amd64",
    "x86_64_nvidia": "aws.edgeml.dda.LocalServer.amd64",
}

ARCHS = sorted(LOCAL_SERVER_VARIANTS)
JP456_ARCHS = ("arm64_jp4", "arm64_jp5", "arm64_jp6")
AMD64_FLAVORS = ("x86_64", "x86_64_nvidia")

LOCAL_SERVER_PREFIX = "aws.edgeml.dda.LocalServer."

#: Installed-component name suffixes the gate can encounter on real devices
#: (write side emits the JP-tagged/amd64 names; legacy bare names survive on
#: already-provisioned JP4 devices; x86_64 is an accepted amd64 alias).
VARIANT_SUFFIXES = ("arm64JP4", "arm64JP5", "arm64JP6", "arm64JP7",
                    "arm64", "aarch64", "amd64", "x86_64")

# --------------------------------------------------------------------------
# The REAL deployed environment: parse the WORKFLOW_MIN_LOCAL_SERVER_VERSIONS
# literal and the DDA_LOCAL_SERVER_VERSION scalar out of compute-stack.ts
# (the actually-deployed configuration, not a synthetic map).
# --------------------------------------------------------------------------

_COMPUTE_STACK_TS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "infrastructure", "lib", "compute-stack.ts"))


def load_prod_floor_config():
    """(floor_map, scalar) from the compute-stack.ts source. Fails loudly
    when either anchor is missing — the anchor disappearing is itself a
    coverage-relevant change someone must look at."""
    with open(_COMPUTE_STACK_TS, encoding="utf-8") as f:
        text = f.read()
    literal = re.search(
        r"WORKFLOW_MIN_LOCAL_SERVER_VERSIONS:\s*JSON\.stringify\(\{(.*?)\}\s*\)",
        text, re.S)
    assert literal, (
        "compute-stack.ts WORKFLOW_MIN_LOCAL_SERVER_VERSIONS JSON.stringify "
        "anchor not found — the deployed floor-map literal moved or was "
        "removed")
    body = re.sub(r"//[^\n]*", "", literal.group(1))  # strip // comments
    floor_map = {key: value for key, value in
                 re.findall(r"([A-Za-z0-9_]+)\s*:\s*'([^']+)'", body)}
    assert floor_map, "floor-map literal parsed empty"
    scalar = re.search(r"DDA_LOCAL_SERVER_VERSION:\s*'([^']+)'", text)
    assert scalar, "compute-stack.ts DDA_LOCAL_SERVER_VERSION anchor not found"
    return floor_map, scalar.group(1)


PROD_FLOOR_MAP, PROD_SCALAR = load_prod_floor_config()


# --------------------------------------------------------------------------
# Fixtures (the established import-inside-moto style)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    yield workflow_packaging
    sys.modules.pop("workflow_packaging", None)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@contextmanager
def floors(packaging, floor_map, scalar):
    """Swap the module-level floor state (map + scalar) and restore it —
    per-example safe inside Hypothesis bodies, unlike the function-scoped
    monkeypatch fixture."""
    saved_map = packaging.MIN_LOCAL_SERVER_VERSIONS
    saved_scalar = packaging.MIN_LOCAL_SERVER_VERSION
    packaging.MIN_LOCAL_SERVER_VERSIONS = floor_map
    packaging.MIN_LOCAL_SERVER_VERSION = scalar
    try:
        yield
    finally:
        packaging.MIN_LOCAL_SERVER_VERSIONS = saved_map
        packaging.MIN_LOCAL_SERVER_VERSION = saved_scalar


def _manifest(packaging, arch, **kwargs):
    return packaging.build_manifest(
        "wf-preserve", 1, arch,
        gst_plugins=[], python_packages=[],
        custom_python_nodes=[], user={"user_id": "user-1"}, **kwargs)


# --------------------------------------------------------------------------
# Hypothesis strategies
# --------------------------------------------------------------------------

#: Purely numeric 3-part versions, so tuple order == the modules' version
#: key order (deployments._version_key / packaging floor_key) and the
#: expected outcomes are computable without importing either function.
version_tuples = st.tuples(st.integers(0, 20), st.integers(0, 20),
                           st.integers(0, 99))


def vstr(t):
    return ".".join(str(part) for part in t)


versions = version_tuples.map(vstr)

#: Arch ids never in ARCH_TO_LOCAL_SERVER_COMPONENT (unknown-arch domain).
unknown_archs = st.sampled_from(
    ["arm64_jp9", "riscv64", "windows_x86", "junk-arch", ""])

#: Extra floor-map entries: other known archs plus junk keys.
extra_map_entries = st.dictionaries(
    st.sampled_from(ARCHS + ["arm64_jp9", "riscv64", "not_an_arch"]),
    versions, max_size=4)

#: Arch subsets resolving to MORE THAN ONE distinct LocalServer variant —
#: the Defect F omission domain (requirement 3.2).
multi_variant_arch_sets = st.sets(
    st.sampled_from(ARCHS), min_size=2,
).filter(
    lambda s: len({LOCAL_SERVER_VARIANTS[a] for a in s}) > 1,
).map(sorted)

#: Device fleets for the gate: name -> None (no LocalServer installed) or
#: (installed variant suffix, installed version tuple).
device_fleets = st.dictionaries(
    st.integers(0, 5).map(lambda i: f"dev-{i}"),
    st.one_of(st.none(),
              st.tuples(st.sampled_from(VARIANT_SUFFIXES), version_tuples)),
    min_size=1, max_size=4)


# ==========================================================================
# (1) jp4/jp5/jp6 identity with the REAL deployed literal (Requirement 3.1)
# ==========================================================================

class TestJp456IdentityWithProdLiteral:

    def test_prod_literal_carries_jp456_entries_at_1_0_0(self):
        """**Property 2: Preservation** — the deployed literal's jp4/jp5/jp6
        entries are exactly '1.0.0' (observed unfixed; the fix only ADDS
        keys, it must never change these).

        # Validates: Requirements 3.1
        """
        for arch in JP456_ARCHS:
            assert PROD_FLOOR_MAP.get(arch) == "1.0.0", (
                "PRESERVATION REGRESSION (3.1): compute-stack.ts floor "
                "entry for {} changed: {!r}".format(
                    arch, PROD_FLOOR_MAP.get(arch)))

    @pytest.mark.parametrize("arch", JP456_ARCHS)
    def test_jp_arch_resolution_recipe_and_manifest_byte_identical(
            self, packaging, arch):
        """**Property 2: Preservation** — with the prod literal + scalar
        loaded, each JetPack arch resolves '1.0.0' from its own entry; the
        recipe ComponentDependencies entry is '>=1.0.0' HARD on the exact
        variant name, and the manifest's minLocalServerVersion is '1.0.0'
        (observed unfixed values, byte-identical).

        # Validates: Requirements 3.1
        """
        with floors(packaging, dict(PROD_FLOOR_MAP), PROD_SCALAR):
            assert packaging.min_local_server_version_for(arch) == "1.0.0"

            out = packaging.local_server_component_dependencies([arch])
            assert out == {LOCAL_SERVER_VARIANTS[arch]: {
                "VersionRequirement": ">=1.0.0",
                "DependencyType": "HARD",
            }}, ("PRESERVATION REGRESSION (3.1): single-arch {} recipe "
                 "entry changed: {!r}".format(arch, out))

            manifest = _manifest(packaging, arch)
            assert manifest["minLocalServerVersion"] == "1.0.0"
            # The embedded map's jp4/jp5/jp6 entries stay byte-identical
            # (the fix may only ADD jp7/x86 keys additively, 3.4).
            for jp_arch in JP456_ARCHS:
                assert manifest["minLocalServerVersions"][jp_arch] == "1.0.0"

    @given(arch=st.sampled_from(ARCHS), entry=versions,
           extra=extra_map_entries, scalar=versions)
    def test_mapped_arch_resolves_exactly_its_map_entry(
            self, packaging, arch, entry, extra, scalar):
        """**Property 2: Preservation (PBT)** — _for any_ floor map
        CONTAINING the arch (any known arch, any entry value, any other
        entries, any scalar), resolution returns exactly the map entry.
        Map-entry resolution is outside the bug condition and must be
        untouched by the fix.

        # Validates: Requirements 3.1
        """
        floor_map = dict(extra)
        floor_map[arch] = entry
        with floors(packaging, floor_map, vstr(scalar)):
            resolved = packaging.min_local_server_version_for(arch)
        assert resolved == entry, (
            "PRESERVATION REGRESSION (3.1): mapped arch {} no longer "
            "resolves its own map entry: {!r} != {!r}".format(
                arch, resolved, entry))


# ==========================================================================
# (2) Scalar-chain identity on an EMPTY map (Requirement 3.6)
# ==========================================================================

class TestScalarChainIdentityOnEmptyMap:

    @given(arch=st.one_of(st.none(), st.sampled_from(ARCHS), unknown_archs),
           scalar=versions)
    def test_empty_map_resolves_scalar_for_any_arch(
            self, packaging, arch, scalar):
        """**Property 2: Preservation (PBT)** — _for any_ scalar and any
        arch (known, unknown, None), an EMPTY floor map resolves the
        scalar. This is the contract the env-clearing preservation suites
        rely on; the hardening applies only to NON-empty maps and must
        leave this chain byte-identical.

        # Validates: Requirements 3.6
        """
        with floors(packaging, {}, scalar):
            resolved = packaging.min_local_server_version_for(arch)
        assert resolved == scalar, (
            "PRESERVATION REGRESSION (3.6): empty-map scalar chain broke "
            "for arch {!r}: {!r} != {!r}".format(arch, resolved, scalar))


# ==========================================================================
# (3) Multi-variant omission — Defect F (Requirement 3.2)
# ==========================================================================

class TestMultiVariantOmissionPreserved:

    @given(archs=multi_variant_arch_sets)
    def test_multi_variant_selection_omits_localserver_dependency(
            self, packaging, archs):
        """**Property 2: Preservation (PBT)** — _for any_ arch subset
        resolving to MORE THAN ONE distinct LocalServer variant,
        local_server_component_dependencies returns {} (the Defect F
        omission), with the prod literal + scalar loaded. Observed unfixed;
        the multi-variant branch is untouched by the fix.

        # Validates: Requirements 3.2
        """
        with floors(packaging, dict(PROD_FLOOR_MAP), PROD_SCALAR):
            out = packaging.local_server_component_dependencies(archs)
        assert out == {}, (
            "PRESERVATION REGRESSION (3.2/Defect F): multi-variant arch "
            "set {} no longer omits the LocalServer dependency: {!r}"
            .format(archs, out))

    @given(x86_floor=version_tuples, nvidia_floor=version_tuples,
           nvidia_first=st.booleans())
    def test_x86_pair_collapses_to_one_amd64_entry_with_max_floor(
            self, packaging, x86_floor, nvidia_floor, nvidia_first):
        """**Property 2: Preservation (PBT)** — the x86_64 + x86_64_nvidia
        pair (either order) still collapses to EXACTLY ONE .amd64 entry
        carrying the MAX of the two mapped floors (both archs in the map —
        never the bug condition).

        # Validates: Requirements 3.2
        """
        floor_map = {"x86_64": vstr(x86_floor),
                     "x86_64_nvidia": vstr(nvidia_floor)}
        archs = (["x86_64_nvidia", "x86_64"] if nvidia_first
                 else ["x86_64", "x86_64_nvidia"])
        with floors(packaging, floor_map, PROD_SCALAR):
            out = packaging.local_server_component_dependencies(archs)
        expected_floor = vstr(max(x86_floor, nvidia_floor))
        assert out == {"aws.edgeml.dda.LocalServer.amd64": {
            "VersionRequirement": ">=" + expected_floor,
            "DependencyType": "HARD",
        }}, ("PRESERVATION REGRESSION (3.2): x86 pair collapse changed for "
             "floors {}: {!r}".format(floor_map, out))
        assert len(out) == 1


# ==========================================================================
# (4) Override uniformity — by_arch={} bypasses the map (Requirement 3.5)
# ==========================================================================

class TestOverrideUniformity:

    @given(devices=device_fleets, override=version_tuples)
    def test_override_gates_every_device_uniformly(
            self, deployments, devices, override):
        """**Property 2: Preservation (PBT)** — _for any_ fleet of devices
        (any installed variant, any installed version, or no LocalServer at
        all) and any override value, check_local_server_compatibility
        called with by_arch={} (the per-version-override calling
        convention) gates EVERY device against the override, regardless of
        variant: exactly the devices below the override (or with no /
        unreadable LocalServer) are reported, each carrying the override as
        its required minimum.

        # Validates: Requirements 3.5
        """
        gg = FakeGreengrass()
        for name, spec in devices.items():
            if spec is None:
                gg.register_device(name)  # no LocalServer installed
            else:
                suffix, installed = spec
                gg.register_device(name, local_server_version=vstr(installed),
                                   arch=suffix)
        override_str = vstr(override)
        incompatible = deployments.check_local_server_compatibility(
            gg, sorted(devices), override_str, {})

        expected_blocked = {name for name, spec in devices.items()
                            if spec is None or spec[1] < override}
        assert {d["device"] for d in incompatible} == expected_blocked, (
            "PRESERVATION REGRESSION (3.5): override uniformity broke — "
            "override {} devices {!r}".format(override_str, devices))
        for entry in incompatible:
            assert entry["min_local_server_version"] == override_str, (
                "PRESERVATION REGRESSION (3.5): a device was gated against "
                "{!r} instead of the uniform override {!r}".format(
                    entry["min_local_server_version"], override_str))

    def test_jp5_jp6_devices_pass_gate_with_prod_map_as_today(
            self, deployments):
        """**Property 2: Preservation** — JP5/JP6 portal deploys keep
        passing the pre-submit gate exactly as today: with the prod map
        and scalar, current-lineage JP5 (1.0.39) and JP6 (1.0.59) devices
        are compatible.

        # Validates: Requirements 3.1
        """
        gg = FakeGreengrass()
        gg.register_device("jp5-dev", local_server_version="1.0.39",
                           arch="arm64JP5")
        gg.register_device("jp6-dev", local_server_version="1.0.59",
                           arch="arm64JP6")
        incompatible = deployments.check_local_server_compatibility(
            gg, ["jp5-dev", "jp6-dev"], PROD_SCALAR, dict(PROD_FLOOR_MAP))
        assert incompatible == [], (
            "PRESERVATION REGRESSION (3.1): JP5/JP6 devices no longer pass "
            "the gate with the prod map: {!r}".format(incompatible))


# ==========================================================================
# (5) Legacy component-name recognition (Requirement 3.6)
# ==========================================================================

class TestLegacyNameRecognitionPinned:

    #: The full observed vocabulary of local_server_component_arch (read
    #: side), recorded from the unfixed tree.
    OBSERVED_MAPPING = {
        LOCAL_SERVER_PREFIX + "arm64JP4": "arm64_jp4",
        LOCAL_SERVER_PREFIX + "arm64JP5": "arm64_jp5",
        LOCAL_SERVER_PREFIX + "arm64JP6": "arm64_jp6",
        LOCAL_SERVER_PREFIX + "arm64JP7": "arm64_jp7",
        # Legacy bare JetPack 4 names: retired on the write side, still
        # recognized on read for already-provisioned JP4 devices.
        LOCAL_SERVER_PREFIX + "arm64": "arm64_jp4",
        LOCAL_SERVER_PREFIX + "aarch64": "arm64_jp4",
        LOCAL_SERVER_PREFIX + "amd64": "x86_64",
        LOCAL_SERVER_PREFIX + "x86_64": "x86_64",
    }

    def test_full_name_vocabulary_maps_as_observed(self, deployments):
        """**Property 2: Preservation** — the read-side legacy recognition
        is unchanged over the full name vocabulary: JP-tagged names map to
        their arch ids, legacy bare arm64/aarch64 map to arm64_jp4 (whose
        floor entry exists), amd64/x86 map to x86_64.

        # Validates: Requirements 3.6
        """
        for name, expected in self.OBSERVED_MAPPING.items():
            resolved = deployments.local_server_component_arch(name)
            assert resolved == expected, (
                "PRESERVATION REGRESSION (3.6): {} now maps to {!r} "
                "instead of {!r}".format(name, resolved, expected))

    def test_undeterminable_names_stay_none(self, deployments):
        """**Property 2: Preservation** — junk / non-LocalServer names stay
        unrecognized (None), so arch-undetermined devices keep falling to
        the scalar fallback and are reported/blocked exactly as today.

        # Validates: Requirements 3.6
        """
        f = deployments.local_server_component_arch
        assert f(None) is None
        assert f("") is None
        assert f("aws.greengrass.Nucleus") is None
        assert f("dda.workflow.421f8233") is None
        # Prefix present but no recognizable variant token.
        assert f(LOCAL_SERVER_PREFIX + "mystery") is None

    @given(name=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        max_size=40))
    def test_names_without_the_localserver_prefix_are_never_recognized(
            self, deployments, name):
        """**Property 2: Preservation (PBT)** — _for any_ component name not
        starting with the LocalServer prefix, recognition returns None
        (never a spurious arch id).

        # Validates: Requirements 3.6
        """
        if name.startswith(LOCAL_SERVER_PREFIX):
            return  # outside this property's domain
        assert deployments.local_server_component_arch(name) is None


# ==========================================================================
# (6) Manifest schema pin (Requirement 3.4)
# ==========================================================================

class TestManifestSchemaPinned:

    #: build_manifest key -> value type, observed on the unfixed tree
    #: (workflowName is None in this shape; subscribed_topics only appears
    #: when non-empty). Only VALUES for previously-missing archs may change
    #: after the fix — the key set and types may not.
    MANIFEST_TYPES = {
        "componentName": str,
        "componentVersion": str,
        "workflowId": str,
        "workflowVersion": int,
        "targetArch": str,
        "minLocalServerVersion": str,
        "minLocalServerVersions": dict,
        "pluginDependencies": list,
        "pythonDependencies": list,
        "pluginChecksums": dict,
        "pluginComponents": dict,
        "customPythonNodeIds": list,
        "packagedAt": int,
        "packagedBy": str,
    }

    @pytest.mark.parametrize("arch", ARCHS)
    def test_manifest_key_set_and_types_unchanged(self, packaging, arch):
        """**Property 2: Preservation** — for EVERY known arch (jp7/x86
        included), the manifest schema is unchanged: same key set, same
        value types. No floor VALUE for jp7/x86 is asserted here (those are
        the corrected values, requirement 2.1/2.2 — exploration suite's
        job); the schema itself must not move.

        # Validates: Requirements 3.4
        """
        with floors(packaging, dict(PROD_FLOOR_MAP), PROD_SCALAR):
            manifest = _manifest(packaging, arch)
        expected_keys = set(self.MANIFEST_TYPES) | {"workflowName"}
        assert set(manifest) == expected_keys, (
            "PRESERVATION REGRESSION (3.4): manifest key set changed for "
            "{}: {!r}".format(arch, sorted(set(manifest) ^ expected_keys)))
        for key, expected_type in self.MANIFEST_TYPES.items():
            assert isinstance(manifest[key], expected_type), (
                "PRESERVATION REGRESSION (3.4): manifest[{!r}] is {} "
                "(expected {})".format(
                    key, type(manifest[key]).__name__,
                    expected_type.__name__))
        assert manifest["workflowName"] is None  # str-or-None field, None here

    def test_subscribed_topics_key_only_when_non_empty(self, packaging):
        """**Property 2: Preservation** — the subscribed_topics key still
        appears ONLY when a non-empty list is passed (trigger-less
        manifests stay byte-identical to pre-feature output).

        # Validates: Requirements 3.4
        """
        with floors(packaging, dict(PROD_FLOOR_MAP), PROD_SCALAR):
            without = _manifest(packaging, "arm64_jp6")
            with_topics = _manifest(packaging, "arm64_jp6",
                                    subscribed_topics=["dda/trigger/a"])
        assert "subscribed_topics" not in without
        assert with_topics["subscribed_topics"] == ["dda/trigger/a"]
        assert set(with_topics) - set(without) == {"subscribed_topics"}


# ==========================================================================
# ==========================================================================
# Property 3 fix-check section (Task 4.2)
#
# **Feature: jp7-workflow-min-localserver-floor, Property 3: Fix Checking —
# A Configured Map Never Silently Falls Back To The Scalar For A Known Arch**
#
# # Validates: Requirements 2.3, 3.6
#
# These tests exercise the HARDENED behavior (design Decision 2 packaging
# guard + Decision 2 deployments-side map completion) and PASS on the fixed
# tree: for any configured (non-empty) floor map, a KNOWN arch missing from
# it resolves the safe per-lineage floor '1.0.0' with a loud warning — never
# the cross-lineage scalar — while mapped archs, None/unknown archs, and the
# empty-map scalar chain stay on their documented branches.
# ==========================================================================
# ==========================================================================

#: The substring unique to the two hardening warnings (packaging's
#: missing-known-arch substitution and deployments' map completion both
#: point at the coverage test / compute-stack.ts).
HARDENING_WARNING_MARKER = "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS is configured"

#: Configured (NON-empty) floor maps over arbitrary subsets of known archs
#: plus junk keys — the Property 3 map domain.
configured_floor_maps = st.tuples(
    st.dictionaries(st.sampled_from(ARCHS), versions, max_size=6),
    st.dictionaries(st.sampled_from(["arm64_jp9", "riscv64", "not_an_arch"]),
                    versions, max_size=2),
).map(lambda pair: {**pair[0], **pair[1]}).filter(bool)


def _hardening_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and HARDENING_WARNING_MARKER in r.getMessage()]


class TestHardenedResolutionPartition:
    """Packaging-side Property 3: the min_local_server_version_for
    resolution partition under a configured map."""

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(floor_map=configured_floor_maps, scalar=versions)
    def test_configured_map_partition_mapped_missing_known_and_scalar_chain(
            self, packaging, caplog, floor_map, scalar):
        """**Property 3: Fix Checking (PBT)** — _for any_ configured
        (non-empty) floor map over arbitrary known-arch subsets plus junk
        keys and any scalar: mapped archs resolve exactly their map entry
        (no hardening warning); missing KNOWN archs resolve '1.0.0' — never
        the scalar — with a loud warning naming the arch; None and unknown
        archs stay on the scalar chain (unless the map literally carries
        the unknown key, in which case the first branch returns its entry).

        # Validates: Requirements 2.3, 3.6
        """
        with floors(packaging, dict(floor_map), scalar):
            # -- known archs: mapped -> entry, missing -> '1.0.0' + warning
            for arch in ARCHS:
                caplog.clear()
                with caplog.at_level(logging.WARNING):
                    resolved = packaging.min_local_server_version_for(arch)
                if arch in floor_map:
                    assert resolved == floor_map[arch], (
                        "FIX-CHECK FAILURE (2.3): mapped known arch {} did "
                        "not resolve its own entry: {!r} != {!r}".format(
                            arch, resolved, floor_map[arch]))
                    assert not _hardening_warnings(caplog), (
                        "FIX-CHECK FAILURE (2.3): spurious hardening "
                        "warning for MAPPED arch {}".format(arch))
                else:
                    assert resolved == packaging.SAFE_LINEAGE_FLOOR == "1.0.0", (
                        "FIX-CHECK FAILURE (2.3): missing KNOWN arch {} "
                        "resolved {!r} instead of the safe per-lineage "
                        "floor '1.0.0'".format(arch, resolved))
                    if scalar != "1.0.0":
                        assert resolved != scalar, (
                            "FIX-CHECK FAILURE (2.3): missing KNOWN arch "
                            "{} silently fell back to the cross-lineage "
                            "scalar {!r}".format(arch, scalar))
                    named = [r for r in _hardening_warnings(caplog)
                             if repr(arch) in r.getMessage()
                             and "1.0.0" in r.getMessage()]
                    assert named, (
                        "FIX-CHECK FAILURE (2.3): no hardening warning "
                        "naming missing KNOWN arch {!r} and the "
                        "substituted floor".format(arch))
            # -- None and unknown archs: scalar chain, no hardening warning
            for arch in (None, "", "arm64_jp9", "riscv64", "windows_x86"):
                caplog.clear()
                with caplog.at_level(logging.WARNING):
                    resolved = packaging.min_local_server_version_for(arch)
                if arch and arch in floor_map:  # junk key literally mapped
                    assert resolved == floor_map[arch]
                else:
                    assert resolved == scalar, (
                        "FIX-CHECK FAILURE (3.6): None/unknown arch {!r} "
                        "left the scalar chain: {!r} != {!r}".format(
                            arch, resolved, scalar))
                assert not _hardening_warnings(caplog), (
                    "FIX-CHECK FAILURE (3.6): hardening warning fired for "
                    "None/unknown arch {!r}".format(arch))

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(arch=st.one_of(st.none(), st.sampled_from(ARCHS), unknown_archs),
           scalar=versions)
    def test_empty_map_resolves_scalar_for_everything_without_warning(
            self, packaging, caplog, arch, scalar):
        """**Property 3: Fix Checking (PBT)** — with an EMPTY map, the
        scalar for everything (known, unknown, None) and NO hardening
        warning: the hardening applies only to configured maps, so the
        env-clearing test-environment contract is untouched.

        # Validates: Requirements 3.6
        """
        caplog.clear()
        with floors(packaging, {}, scalar):
            with caplog.at_level(logging.WARNING):
                resolved = packaging.min_local_server_version_for(arch)
        assert resolved == scalar, (
            "FIX-CHECK FAILURE (3.6): empty map did not resolve the scalar "
            "for arch {!r}: {!r} != {!r}".format(arch, resolved, scalar))
        assert not _hardening_warnings(caplog), (
            "FIX-CHECK FAILURE (3.6): hardening warning fired on an EMPTY "
            "map for arch {!r}".format(arch))


class TestDeploymentsMapCompletion:
    """Deployments-side Property 3: _fill_missing_arch_floors completes a
    configured map without ever overwriting present entries."""

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(floor_map=configured_floor_maps)
    def test_fill_missing_arch_floors_completion_contract(
            self, deployments, caplog, floor_map):
        """**Property 3: Fix Checking (PBT)** — _for any_ configured
        (non-empty) map: the output contains every LOCAL_SERVER_ARCH_IDS
        key; present entries (known or junk) are never overwritten; fills
        are exactly '1.0.0'; no other keys appear; the input is not
        mutated; and ONE loud warning names every filled arch (no warning
        when nothing is missing).

        # Validates: Requirements 2.3, 3.6
        """
        snapshot = dict(floor_map)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            out = deployments._fill_missing_arch_floors(floor_map)
        assert floor_map == snapshot, (
            "FIX-CHECK FAILURE (2.3): _fill_missing_arch_floors mutated "
            "its input")
        missing = [a for a in deployments.LOCAL_SERVER_ARCH_IDS
                   if a not in snapshot]
        for arch in deployments.LOCAL_SERVER_ARCH_IDS:
            assert arch in out, (
                "FIX-CHECK FAILURE (2.3): completed map is missing known "
                "arch {!r}".format(arch))
        for key, value in snapshot.items():
            assert out[key] == value, (
                "FIX-CHECK FAILURE (2.3): present entry {!r} was "
                "overwritten: {!r} != {!r}".format(key, out[key], value))
        for arch in missing:
            assert out[arch] == deployments.SAFE_LINEAGE_FLOOR == "1.0.0", (
                "FIX-CHECK FAILURE (2.3): filled arch {!r} carries {!r} "
                "instead of '1.0.0'".format(arch, out[arch]))
        expected_keys = set(snapshot) | (
            set(deployments.LOCAL_SERVER_ARCH_IDS) if missing else set())
        assert set(out) == expected_keys, (
            "FIX-CHECK FAILURE (2.3): completed map grew unexpected keys: "
            "{!r}".format(sorted(set(out) - expected_keys)))
        fill_warnings = _hardening_warnings(caplog)
        if missing:
            naming_all = [r for r in fill_warnings
                          if all(a in r.getMessage() for a in missing)]
            assert len(naming_all) == 1, (
                "FIX-CHECK FAILURE (2.3): expected exactly ONE completion "
                "warning naming all filled archs {!r}; got {} matching "
                "records".format(missing, len(naming_all)))
        else:
            assert not fill_warnings, (
                "FIX-CHECK FAILURE (2.3): completion warning fired for an "
                "already-complete map")

    def test_empty_map_returned_as_is_without_warning(
            self, deployments, caplog):
        """**Property 3: Fix Checking** — an EMPTY map is returned as-is
        (the very object, preserving the scalar-fallback chain when no map
        is configured) and no warning fires.

        # Validates: Requirements 3.6
        """
        empty = {}
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            out = deployments._fill_missing_arch_floors(empty)
        assert out is empty, (
            "FIX-CHECK FAILURE (3.6): empty map was not returned as-is")
        assert not _hardening_warnings(caplog)


class TestEndToEndShapeNeverCarriesScalar:
    """End-to-end Property 3 shape: recipe VersionRequirement and manifest
    minLocalServerVersion under a configured map."""

    @given(arch=st.sampled_from(ARCHS), floor_map=configured_floor_maps,
           scalar=versions)
    def test_recipe_and_manifest_floor_is_map_entry_or_safe_floor(
            self, packaging, arch, floor_map, scalar):
        """**Property 3: Fix Checking (PBT)** — _for any_ single known arch
        under any configured (non-empty) map and any scalar,
        local_server_component_dependencies([arch]) emits
        '>=<map entry or 1.0.0>' on the exact variant name and
        build_manifest records the same floor — so neither the recipe
        constraint nor the manifest can carry the cross-lineage scalar
        unless the map explicitly maps the arch to that value.

        # Validates: Requirements 2.3, 3.6
        """
        expected = floor_map.get(arch, "1.0.0")
        with floors(packaging, dict(floor_map), scalar):
            deps = packaging.local_server_component_dependencies([arch])
            manifest = _manifest(packaging, arch)
        assert deps == {LOCAL_SERVER_VARIANTS[arch]: {
            "VersionRequirement": ">=" + expected,
            "DependencyType": "HARD",
        }}, ("FIX-CHECK FAILURE (2.3): recipe entry for {} under map {!r} "
             "is {!r} (expected floor {!r})".format(
                 arch, floor_map, deps, expected))
        assert manifest["minLocalServerVersion"] == expected, (
            "FIX-CHECK FAILURE (2.3): manifest floor for {} is {!r} "
            "(expected {!r})".format(
                arch, manifest["minLocalServerVersion"], expected))
        if scalar != expected:
            assert deps[LOCAL_SERVER_VARIANTS[arch]][
                "VersionRequirement"] != ">=" + scalar, (
                "FIX-CHECK FAILURE (2.3): recipe constraint for {} "
                "silently carries the cross-lineage scalar {!r}".format(
                    arch, scalar))
