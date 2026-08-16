"""
Bug condition exploration suite: jp7-workflow-min-localserver-floor.

Property 1: Bug Condition - Known Archs Resolve Per-Lineage Floors From The
Deployed Map (spec .kiro/specs/jp7-workflow-min-localserver-floor).

These tests assert the EXPECTED (fixed) behavior against the REAL deployed
configuration: the ``WORKFLOW_MIN_LOCAL_SERVER_VERSIONS`` literal and the
``DDA_LOCAL_SERVER_VERSION`` scalar parsed out of
``edge-cv-portal/infrastructure/lib/compute-stack.ts`` and loaded into the
real backend modules (monkeypatched module attributes, the established test
style of test_workflow_packaging_variant_min_version.py).

On the UNFIXED tree cases 1-6 FAIL, reproducing the incident numbers of
deployment ``cb139a40`` (workflow ``dda.workflow.421f8233`` v5.0.0 ->
jetson-thor1, JP7): the floor map has no ``arm64_jp7`` / ``x86_64`` /
``x86_64_nvidia`` keys, so both backend consumers silently substitute the
cross-lineage scalar ``1.0.63`` - baking the unsatisfiable
``aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.63`` recipe constraint (JP7
lineage latest = 1.0.5) and pre-submit-rejecting deployable devices.

FAILURE HERE IS THE BUG-CONDITION PROOF, NOT A BROKEN TEST. Do not weaken
these assertions; the suite must pass unmodified once the fix lands
(tasks 3.1-3.3, verified in task 3.4).

The literal/scalar extractor below is the one the permanent coverage test
(test_workflow_min_localserver_floor_coverage.py, task 4.1 / design
Decision 3) reuses.
"""
import os
import re
import sys

import pytest
from hypothesis import example, given, strategies as st

from test_workflow_packaging_deployment_integration import FakeGreengrass

_HERE = os.path.dirname(os.path.abspath(__file__))

#: The deployed Lambda environment source of truth (design: the CDK env map
#: in compute-stack.ts; compute-stack.js is a gitignored build artifact).
COMPUTE_STACK_TS = os.path.abspath(os.path.join(
    _HERE, "..", "..", "infrastructure", "lib", "compute-stack.ts"))

_FLOOR_MAP_ANCHOR = re.compile(
    r"WORKFLOW_MIN_LOCAL_SERVER_VERSIONS\s*:\s*JSON\.stringify\s*\(\s*\{")
_SCALAR_ANCHOR = re.compile(
    r"DDA_LOCAL_SERVER_VERSION\s*:\s*(['\"])([^'\"]+)\1")
_LITERAL_ENTRY = re.compile(
    r"""(['"]?)([A-Za-z0-9_]+)\1\s*:\s*(['"])([^'"]*)\3""")


def read_compute_stack_source(path=COMPUTE_STACK_TS):
    """The compute-stack.ts source; fails loudly when the file moved."""
    if not os.path.isfile(path):
        raise AssertionError(
            f"compute-stack.ts not found at {path} - the deployed-environment "
            "source of truth moved; update the extractor (and the coverage "
            "test that reuses it)")
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_floor_map_literal(source=None):
    """The WORKFLOW_MIN_LOCAL_SERVER_VERSIONS object literal parsed into a
    ``{arch_id: version}`` dict.

    Tolerant of TS quoting styles, trailing commas, and // comments inside
    the literal. Fails LOUDLY when the ``JSON.stringify({`` anchor cannot be
    found: the anchor disappearing is itself a coverage-relevant change
    someone must look at (design Decision 3).
    """
    if source is None:
        source = read_compute_stack_source()
    match = _FLOOR_MAP_ANCHOR.search(source)
    if not match:
        raise AssertionError(
            "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS: JSON.stringify({...}) anchor "
            f"not found in {COMPUTE_STACK_TS} - the env literal was renamed, "
            "moved, or restructured; this is a coverage-relevant change")
    # Scan to the literal's matching close brace (depth-aware, so a future
    # nested value cannot silently truncate the parse).
    depth, i = 1, match.end()
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    if depth:
        raise AssertionError(
            "unbalanced braces in the WORKFLOW_MIN_LOCAL_SERVER_VERSIONS "
            "literal - extractor cannot parse compute-stack.ts")
    body = re.sub(r"//[^\n]*", "", source[match.end():i - 1])
    return {key: value for _, key, _, value in _LITERAL_ENTRY.findall(body)}


def extract_scalar_default(source=None):
    """The DDA_LOCAL_SERVER_VERSION scalar (the cross-lineage legacy-lineage
    number, '1.0.63' in prod). Fails loudly when the anchor is missing."""
    if source is None:
        source = read_compute_stack_source()
    match = _SCALAR_ANCHOR.search(source)
    if not match:
        raise AssertionError(
            f"DDA_LOCAL_SERVER_VERSION scalar not found in {COMPUTE_STACK_TS}"
            " - the env entry was renamed or removed (requirement 3.6 keeps "
            "it as the last-resort default; investigate)")
    return match.group(2)


# --------------------------------------------------------------------------
# Fixtures: real modules with the REAL deployed configuration loaded
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stack_env():
    """(floor_map, scalar) parsed from the actual compute-stack.ts."""
    source = read_compute_stack_source()
    return extract_floor_map_literal(source), extract_scalar_default(source)


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


@pytest.fixture
def prod_packaging(packaging, stack_env, monkeypatch):
    """workflow_packaging resolving floors from the parsed prod literal and
    scalar (the established monkeypatch style)."""
    floor_map, scalar = stack_env
    monkeypatch.setattr(packaging, "MIN_LOCAL_SERVER_VERSIONS", dict(floor_map))
    monkeypatch.setattr(packaging, "MIN_LOCAL_SERVER_VERSION", scalar)
    return packaging


def _gate(deployments, stack_env, greengrass, thing_names):
    """check_local_server_compatibility exactly as the real pre-submit call
    site invokes it (deployments.py ~line 3414, no per-version override):
    the module map/scalar - monkeypatched to the parsed prod values - feed
    the arguments."""
    floor_map, scalar = stack_env
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(deployments, "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS",
                   dict(floor_map))
        mp.setattr(deployments, "WORKFLOW_MIN_LOCAL_SERVER_VERSION", scalar)
        return deployments.check_local_server_compatibility(
            greengrass, thing_names,
            deployments.WORKFLOW_MIN_LOCAL_SERVER_VERSION,
            deployments.WORKFLOW_MIN_LOCAL_SERVER_VERSIONS)


# --------------------------------------------------------------------------
# Extractor sanity (PASSES on the unfixed tree): proves the six failures
# below are genuine bug conditions, not parse failures.
# --------------------------------------------------------------------------

class TestExtractorSanity:
    def test_literal_parses_with_jp456_floors_and_wellformed_scalar(
            self, stack_env):
        # Validates: Requirements 1.1 (extraction preamble - the real
        # deployed configuration, not a synthetic map)
        floor_map, scalar = stack_env
        assert floor_map.get("arm64_jp4") == "1.0.0"
        assert floor_map.get("arm64_jp5") == "1.0.0"
        assert floor_map.get("arm64_jp6") == "1.0.0"
        assert re.fullmatch(r"\d+\.\d+\.\d+", scalar), scalar
        for arch, version in floor_map.items():
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), (arch, version)


# --------------------------------------------------------------------------
# Cases 1-6: MUST FAIL on the unfixed tree (bug-condition proof); MUST PASS
# unmodified after the fix (task 3.4).
# --------------------------------------------------------------------------

class TestBugConditionExploration:
    def test_case1_literal_covers_jp7_and_x86_archs(self, stack_env):
        """Case 1 - literal coverage (the CDK-fix pin): the deployed floor
        map must carry explicit per-lineage keys for every arch the backend
        knows. The runtime hardening alone cannot make this pass - it pins
        the compute-stack.ts edit specifically (design File 1)."""
        # Validates: Requirements 1.1, 1.5
        floor_map, _ = stack_env
        missing = {"arm64_jp7", "x86_64", "x86_64_nvidia"} - set(floor_map)
        assert not missing, (
            f"WORKFLOW_MIN_LOCAL_SERVER_VERSIONS literal is missing {sorted(missing)}; "
            f"parsed literal = {floor_map} - every arch absent from the map "
            "silently inherits the cross-lineage scalar (the cb139a40 root "
            "cause)")

    def test_case2_packager_jp7_floor_and_recipe_dependency(
            self, prod_packaging):
        """Case 2 - the incident (defect 1.1): the packager must resolve the
        JP7 per-lineage floor and emit a satisfiable recipe constraint. On
        the unfixed tree this resolves '1.0.63' / '>=1.0.63' - the exact
        cb139a40 constraint, unsatisfiable against jetson-thor1's =1.0.5
        pin (JP7 lineage latest = 1.0.5)."""
        # Validates: Requirements 1.1
        floor = prod_packaging.min_local_server_version_for("arm64_jp7")
        assert floor == "1.0.0", (
            f"min_local_server_version_for('arm64_jp7') resolved {floor!r} - "
            "the cross-lineage scalar, not the JP7 per-lineage floor '1.0.0'")

        deps = prod_packaging.local_server_component_dependencies(
            ["arm64_jp7"])
        assert deps == {
            "aws.edgeml.dda.LocalServer.arm64JP7": {
                "VersionRequirement": ">=1.0.0",
                "DependencyType": "HARD",
            }
        }, (
            f"recipe ComponentDependencies = {deps!r} - the unfixed tree "
            "bakes the HARD '>=1.0.63' cross-lineage constraint into the "
            "immutable recipe (deployment cb139a40, FAILED_NO_STATE_CHANGE)")

    def test_case3_manifest_jp7_floor(self, prod_packaging):
        """Case 3 - manifest floor (defect 1.2): the artifact manifest.json
        must record the JP7 per-lineage floor, not the scalar."""
        # Validates: Requirements 1.2
        manifest = prod_packaging.build_manifest(
            "wf-421f8233", 5, "arm64_jp7",
            gst_plugins=[], python_packages=[],
            custom_python_nodes=[], user={"user_id": "user-1"})
        assert manifest["minLocalServerVersion"] == "1.0.0", (
            "manifest.json minLocalServerVersion = "
            f"{manifest['minLocalServerVersion']!r} - the cross-lineage "
            "scalar reached the on-device compatibility surface")

    # Case 4 - pre-submit gate (defect 1.4), scoped PBT: for any installed
    # version in the REAL JP7 lineage range (1.0.0-1.0.5), a device reporting
    # aws.edgeml.dda.LocalServer.arm64JP7 at that version must pass the gate
    # under the prod map. On the unfixed tree every lineage version is
    # rejected with the exact observed reason ("Installed LocalServer version
    # 1.0.5 is older than the required minimum 1.0.63" for the pinned
    # incident example).
    # Validates: Requirements 1.4
    @example(patch=5)  # jetson-thor1: installed arm64JP7 1.0.5 (incident)
    @given(patch=st.integers(min_value=0, max_value=5))
    def test_case4_gate_passes_jp7_lineage_versions(
            self, deployments, stack_env, patch):
        installed = f"1.0.{patch}"
        gg = FakeGreengrass()
        gg.register_device("jetson-thor1", local_server_version=installed,
                           arch="arm64JP7")
        incompatible = _gate(deployments, stack_env, gg, ["jetson-thor1"])
        assert incompatible == [], (
            f"pre-submit gate rejected a JP7 device at {installed}: "
            f"{incompatible[0]['reason']!r} (the second bite of the "
            "missing-key fallback, defect 1.4)")

    def test_case5_latent_x86_floors_collapse_and_gate(
            self, prod_packaging, deployments, stack_env):
        """Case 5 - latent x86 (defect 1.5): both x86 flavors must resolve
        their own '1.0.0' floors, collapse to ONE .amd64 recipe entry
        satisfiable by the amd64 lineage (latest 1.0.37), and the gate must
        pass an amd64 device at 1.0.37. Unfixed: '>=1.0.63' baked, device
        blocked - undetected only because no x86 workflow deploy has been
        attempted."""
        # Validates: Requirements 1.5
        for arch in ("x86_64", "x86_64_nvidia"):
            floor = prod_packaging.min_local_server_version_for(arch)
            assert floor == "1.0.0", (
                f"min_local_server_version_for({arch!r}) resolved {floor!r} "
                "- the cross-lineage scalar (amd64 lineage latest = 1.0.37)")

        deps = prod_packaging.local_server_component_dependencies(
            ["x86_64", "x86_64_nvidia"])
        assert deps == {
            "aws.edgeml.dda.LocalServer.amd64": {
                "VersionRequirement": ">=1.0.0",
                "DependencyType": "HARD",
            }
        }, (
            f"amd64 collapse emitted {deps!r} - expected ONE .amd64 entry "
            "at the per-lineage '>=1.0.0' floor")

        gg = FakeGreengrass()
        gg.register_device("amd64-dev", local_server_version="1.0.37",
                           arch="amd64")
        incompatible = _gate(deployments, stack_env, gg, ["amd64-dev"])
        assert incompatible == [], (
            "pre-submit gate rejected an amd64 device at the lineage's "
            f"latest 1.0.37: {incompatible[0]['reason']!r}")

    def test_case6_future_arch_never_inherits_cross_lineage_scalar(
            self, prod_packaging, stack_env, monkeypatch):
        """Case 6 - recurrence shape (defect 1.6): a future arch (JP8) added
        to ARCH_TO_LOCAL_SERVER_COMPONENT without a floor-map key must NOT
        silently inherit the cross-lineage scalar. Unfixed: silent 1.0.63 -
        the exact shape that will bite the JP8 fan-out."""
        # Validates: Requirements 1.6
        _, scalar = stack_env
        extended = dict(prod_packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        extended["arm64_jp8"] = "aws.edgeml.dda.LocalServer.arm64JP8"
        monkeypatch.setattr(
            prod_packaging, "ARCH_TO_LOCAL_SERVER_COMPONENT", extended)

        floor = prod_packaging.min_local_server_version_for("arm64_jp8")
        assert floor != scalar, (
            f"a hypothetical arm64_jp8 silently resolved the cross-lineage "
            f"scalar {scalar!r} under the prod map - the recurrence class is "
            "open (nothing fails loudly at packaging time)")
        assert floor == "1.0.0", (
            f"arm64_jp8 resolved {floor!r} - expected the safe per-lineage "
            "floor '1.0.0' (design Decision 1)")
