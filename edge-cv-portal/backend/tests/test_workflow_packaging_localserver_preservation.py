"""Preservation property tests (Task 12) for edge-deploy-reliability.

**Feature: edge-deploy-reliability, Property 12: Preservation — Single-variant
packaging output unchanged (Defect F)**

**Validates: Requirements 3.14, 3.15, 3.16**

Observation-first (observed on the F-UNFIXED `workflow_packaging.py` and
recorded here as the golden contract; conftest configures no version-floor
env vars, so `min_local_server_version_for` resolves to the scalar default
"1.0.0" for every arch):

- single-arch outputs of ``local_server_component_dependencies``::

    arm64_jp4     -> {"aws.edgeml.dda.LocalServer.arm64JP4":
                      {"VersionRequirement": ">=1.0.0", "DependencyType": "HARD"}}
    arm64_jp5     -> {"aws.edgeml.dda.LocalServer.arm64JP5": {same shape}}
    arm64_jp6     -> {"aws.edgeml.dda.LocalServer.arm64JP6": {same shape}}
    x86_64        -> {"aws.edgeml.dda.LocalServer.amd64":    {same shape}}
    x86_64_nvidia -> {"aws.edgeml.dda.LocalServer.amd64":    {same shape}}

- ``["x86_64", "x86_64_nvidia"]`` collapses to EXACTLY the one amd64 entry
  above (same shape, either input order).

- ``model_component_dependencies`` emits one unpinned HARD entry
  (``{"VersionRequirement": ">=0.0.0", "DependencyType": "HARD"}``) per
  distinct ``published_component.component_name``; ``plugin_component_
  dependencies`` emits one pinned HARD entry (``">={v}.0.0 <{v+1}.0.0"``)
  per non-None record — and the packaging handler's merge
  ``{**plugin, **model, **local_server}`` preserves both byte-identical
  (the three namespaces are disjoint).

These tests are written BEFORE the Defect F fix (task 13.1: single-variant-
only LocalServer emission) and must PASS on the unfixed tree AND keep
passing after the fix: every assertion here is about arch sets that collapse
to ONE distinct LocalServer variant (NOT isBugCondition_F) or about the
model/plugin namespaces the fix must never touch. Nothing here constrains
the multi-variant LocalServer output (that is task 11 / Property 11).

``build_recipe``'s non-ComponentDependencies field contract is pinned
exhaustively by test_workflow_packaging_recipe_preservation.py (Property 7);
here the 3.16 slice asserted is that dependency CONTENT never leaks into
the other recipe fields and passes through byte-identical.

Harness: `aws_stack` from conftest so the freshly imported
`workflow_packaging` binds moto-intercepted module-level clients; every
function under test (`local_server_component_dependencies`,
`model_component_dependencies`, `plugin_component_dependencies`,
`build_recipe`) is a pure seam needing no seeded tables.
"""
import os
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# --------------------------------------------------------------------------
# Golden contract, recorded from the F-UNFIXED tree (NOT imported from the
# module under test)
# --------------------------------------------------------------------------

#: arch id -> LocalServer variant component name (the fail-closed
#: ARCH_TO_LOCAL_SERVER_COMPONENT discipline; bare '.arm64' never appears).
LOCAL_SERVER_VARIANTS = {
    "arm64_jp4": "aws.edgeml.dda.LocalServer.arm64JP4",
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "x86_64": "aws.edgeml.dda.LocalServer.amd64",
    "x86_64_nvidia": "aws.edgeml.dda.LocalServer.amd64",
}

ARCHS = sorted(LOCAL_SERVER_VARIANTS)
AMD64_FLAVORS = ("x86_64", "x86_64_nvidia")
ARM_ARCHS = tuple(a for a in ARCHS if a not in AMD64_FLAVORS)

#: Resolved floor when no WORKFLOW_MIN_LOCAL_SERVER_VERSION[S] /
#: DDA_LOCAL_SERVER_VERSION is configured (conftest sets none of them).
DEFAULT_FLOOR = "1.0.0"

LOCAL_SERVER_PREFIX = "aws.edgeml.dda.LocalServer."
PLUGIN_PREFIX = "dda.plugin."

#: The env vars min_local_server_version_for reads at module import time —
#: cleared before the fixture import so the recorded goldens apply.
_FLOOR_ENV_VARS = ("WORKFLOW_MIN_LOCAL_SERVER_VERSION",
                   "WORKFLOW_MIN_LOCAL_SERVER_VERSIONS",
                   "DDA_LOCAL_SERVER_VERSION")


def golden_local_server_entry(arch):
    """The exact single-variant entry observed on the unfixed tree."""
    return {
        LOCAL_SERVER_VARIANTS[arch]: {
            "VersionRequirement": ">=" + DEFAULT_FLOOR,
            "DependencyType": "HARD",
        },
    }


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients are intercepted, with the version-floor env vars cleared
    so the module binds the recorded default floor."""
    saved = {name: os.environ.pop(name, None) for name in _FLOOR_ENV_VARS}
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    yield workflow_packaging
    sys.modules.pop("workflow_packaging", None)
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


# --------------------------------------------------------------------------
# Hypothesis strategies
# --------------------------------------------------------------------------

#: Arch sets that collapse to EXACTLY ONE distinct LocalServer variant —
#: the preservation domain (never isBugCondition_F): any single arch, or
#: any non-empty subset of the two amd64 flavors.
single_variant_arch_sets = st.one_of(
    st.sampled_from(ARCHS).map(lambda a: [a]),
    st.sets(st.sampled_from(AMD64_FLAVORS), min_size=1).map(sorted),
)

#: Any arch set at all (the 3.15 domain: model/plugin entries are untouched
#: for ANY architecture selection, single- or multi-variant).
any_arch_sets = st.sets(st.sampled_from(ARCHS), min_size=1).map(sorted)

_slugs = st.uuids().map(str)

#: resolve_model_components output shape: model name -> published_component
#: map carrying component_name (model-* namespace, disjoint from the plugin
#: and LocalServer namespaces by construction).
resolved_model_sets = st.dictionaries(
    keys=_slugs.map(lambda s: f"model-name-{s}"),
    values=_slugs.map(lambda s: {"component_name": f"model-{s}",
                                 "component_version": "1.0.0"}),
    max_size=3,
)

#: load_custom_plugin_records output shape: dependency key -> Plugin_Record
#: (or None for an unresolvable dependency, which the function skips).
plugin_dep_records = st.dictionaries(
    keys=_slugs.map(lambda s: f"custom:uc/{s}"),
    values=st.one_of(
        st.none(),
        st.builds(lambda s, v: {"plugin_id": f"plg-{s}", "version": v},
                  _slugs, st.integers(min_value=1, max_value=9)),
    ),
    max_size=3,
)


def expected_model_entries(resolved):
    """Golden model_component_dependencies contract (recorded, unpinned)."""
    names = sorted({p["component_name"] for p in resolved.values()})
    return {name: {"VersionRequirement": ">=0.0.0", "DependencyType": "HARD"}
            for name in names}


def expected_plugin_entries(dep_records):
    """Golden plugin_component_dependencies contract (recorded, pinned)."""
    entries = {}
    for record in dep_records.values():
        if not record:
            continue
        version = int(record["version"])
        entries[PLUGIN_PREFIX + record["plugin_id"]] = {
            "VersionRequirement": f">={version}.0.0 <{version + 1}.0.0",
            "DependencyType": "HARD",
        }
    return entries


def merged_dependencies(packaging, dep_records, resolved, archs):
    """The packaging handler's merge composition, exactly as written at the
    merge site (workflow_packaging.handler)."""
    return {
        **packaging.plugin_component_dependencies(dep_records),
        **packaging.model_component_dependencies(resolved),
        **packaging.local_server_component_dependencies(archs),
    }


# --------------------------------------------------------------------------
# (a) Single-arch LocalServer entries are byte-identical to today's
# (Requirement 3.14)
# --------------------------------------------------------------------------

class TestSingleArchLocalServerEntryPreserved:

    @pytest.mark.parametrize("arch", ARCHS)
    def test_single_arch_emits_todays_exact_entry(self, packaging, arch):
        """**Feature: edge-deploy-reliability, Property 12: Preservation —
        Single-variant packaging output unchanged**

        For every single arch, the emitted LocalServer entry equals the
        recorded unfixed output exactly: the ARCH_TO_LOCAL_SERVER_COMPONENT
        name, VersionRequirement '>=' + min_local_server_version_for(arch),
        DependencyType HARD — and nothing else.

        Validates: Requirements 3.14
        """
        out = packaging.local_server_component_dependencies([arch])
        assert out == golden_local_server_entry(arch), (
            "PRESERVATION REGRESSION (Property 12/3.14): single-arch {} "
            "LocalServer output changed: {!r}".format(arch, out))
        # The floor is the module's own per-arch minimum (scalar default
        # here): the '>=' + min_local_server_version_for(arch) contract.
        floor = packaging.min_local_server_version_for(arch)
        assert out[LOCAL_SERVER_VARIANTS[arch]]["VersionRequirement"] == \
            ">=" + floor

    @settings(max_examples=25, deadline=None)
    @given(archs=single_variant_arch_sets)
    def test_any_single_variant_arch_set_emits_todays_exact_entry(
            self, packaging, archs):
        """**Feature: edge-deploy-reliability, Property 12: Preservation —
        Single-variant packaging output unchanged**

        For ANY arch set collapsing to one distinct LocalServer variant
        (NOT isBugCondition_F), the output is exactly today's single entry
        for that variant.

        Validates: Requirements 3.14
        """
        out = packaging.local_server_component_dependencies(archs)
        assert out == golden_local_server_entry(archs[0]), (
            "PRESERVATION REGRESSION (Property 12/3.14): single-variant "
            "arch set {} no longer emits today's exact entry: {!r}"
            .format(archs, out))


# --------------------------------------------------------------------------
# (b) x86_64 + x86_64_nvidia collapse to exactly one amd64 entry
# (Requirement 3.14)
# --------------------------------------------------------------------------

class TestAmd64PairCollapsePreserved:

    @pytest.mark.parametrize("archs", [
        ["x86_64", "x86_64_nvidia"],
        ["x86_64_nvidia", "x86_64"],
    ])
    def test_both_amd64_flavors_collapse_to_one_entry(self, packaging, archs):
        """**Feature: edge-deploy-reliability, Property 12: Preservation —
        Single-variant packaging output unchanged**

        The x86_64 + x86_64_nvidia pair (either order) collapses to EXACTLY
        one aws.edgeml.dda.LocalServer.amd64 entry, equal to today's.

        Validates: Requirements 3.14
        """
        out = packaging.local_server_component_dependencies(archs)
        assert out == golden_local_server_entry("x86_64"), (
            "PRESERVATION REGRESSION (Property 12/3.14): the amd64-flavor "
            "collapse changed for {}: {!r}".format(archs, out))
        assert len(out) == 1


# --------------------------------------------------------------------------
# (c) Model and plugin entries are untouched by the merge composition
# (Requirement 3.15)
# --------------------------------------------------------------------------

class TestMergePreservesModelAndPluginEntries:

    @settings(max_examples=25, deadline=None)
    @given(dep_records=plugin_dep_records, resolved=resolved_model_sets,
           archs=any_arch_sets)
    def test_merge_composition_preserves_model_and_plugin_entries(
            self, packaging, dep_records, resolved, archs):
        """**Feature: edge-deploy-reliability, Property 12: Preservation —
        Single-variant packaging output unchanged**

        For ANY architecture selection and any model/plugin inputs, the
        handler's merge composition preserves the model_component_
        dependencies and plugin_component_dependencies outputs
        byte-identical, and every other merged key is a LocalServer entry
        — so the Defect F fix (which touches only the LocalServer function)
        can never disturb them.

        Validates: Requirements 3.15
        """
        plugin_out = packaging.plugin_component_dependencies(dep_records)
        model_out = packaging.model_component_dependencies(resolved)

        # The separate functions' own outputs match the recorded goldens.
        assert plugin_out == expected_plugin_entries(dep_records), (
            "PRESERVATION REGRESSION (Property 12/3.15): "
            "plugin_component_dependencies output changed")
        assert model_out == expected_model_entries(resolved), (
            "PRESERVATION REGRESSION (Property 12/3.15): "
            "model_component_dependencies output changed")

        merged = merged_dependencies(packaging, dep_records, resolved, archs)

        assert {k: v for k, v in merged.items()
                if k.startswith(PLUGIN_PREFIX)} == plugin_out, (
            "PRESERVATION REGRESSION (Property 12/3.15): dda.plugin.* "
            "entries did not survive the merge byte-identical for archs {}"
            .format(archs))
        assert {k: v for k, v in merged.items()
                if k in model_out} == model_out, (
            "PRESERVATION REGRESSION (Property 12/3.15): model component "
            "entries did not survive the merge byte-identical for archs {}"
            .format(archs))
        leftover = {k for k in merged
                    if not k.startswith(PLUGIN_PREFIX) and k not in model_out}
        assert all(k.startswith(LOCAL_SERVER_PREFIX) for k in leftover), (
            "merge produced entries outside the three disjoint namespaces: "
            "{!r}".format(sorted(leftover)))


# --------------------------------------------------------------------------
# (3.16 slice) The merged dependencies pass through build_recipe
# byte-identical without leaking into any other field
# --------------------------------------------------------------------------

class TestMergedDependenciesPassThroughBuildRecipe:

    @settings(max_examples=25, deadline=None)
    @given(dep_records=plugin_dep_records, resolved=resolved_model_sets,
           archs=single_variant_arch_sets, workflow_version=st.integers(
               min_value=1, max_value=50))
    def test_build_recipe_fields_independent_of_dependency_content(
            self, packaging, dep_records, resolved, archs, workflow_version):
        """**Feature: edge-deploy-reliability, Property 12: Preservation —
        Single-variant packaging output unchanged**

        build_recipe attaches the merged ComponentDependencies
        byte-identical and every other recipe field is unaffected by the
        dependency content (compared against the same call with plugin-only
        dependencies — the pre-Defect-C shape). The full non-dependency
        field contract is pinned by test_workflow_packaging_recipe_
        preservation.py (Property 7).

        Validates: Requirements 3.16
        """
        workflow_id = "wf-defect-f-preservation"
        component_version = f"{workflow_version}.0.0"
        bucket = "usecase-bucket-defect-f"
        final_keys = {
            arch: ("workflows/components/{wf}/{wfv}/{cv}/{arch}/"
                   "workflow-{arch}.zip".format(
                       wf=workflow_id, wfv=workflow_version,
                       cv=component_version, arch=arch))
            for arch in archs
        }

        merged = merged_dependencies(packaging, dep_records, resolved, archs)
        recipe = packaging.build_recipe(
            workflow_id, workflow_version, bucket, final_keys,
            component_dependencies=merged)

        assert recipe["ComponentDependencies"] == merged, (
            "PRESERVATION REGRESSION (Property 12/3.16): the merged "
            "ComponentDependencies did not pass through build_recipe "
            "byte-identical")

        plugin_only = packaging.plugin_component_dependencies(dep_records)
        baseline = packaging.build_recipe(
            workflow_id, workflow_version, bucket, final_keys,
            component_dependencies=plugin_only or None)

        strip = lambda r: {k: v for k, v in r.items()
                           if k != "ComponentDependencies"}
        assert strip(recipe) == strip(baseline), (
            "PRESERVATION REGRESSION (Property 12/3.16): dependency "
            "content leaked into a non-ComponentDependencies recipe field")
