"""Preservation property tests (Task 2) for stale-workflow-registrations.

**Property 2: Preservation — Deployed Version and Recipe Contract Unchanged
(portal half)**

**Validates: Requirements 3.2**

Observation-first (observed on the UNFIXED workflow_packaging.py and
recorded here as the golden contract): for any (workflow_id,
workflow_version, arch subset, ComponentDependencies dict), ``build_recipe``
emits a recipe fully determined by its inputs — RecipeFormatVersion,
ComponentName/Version/Type, Publisher, ComponentConfiguration, per-arch
platform manifests (ordering, 'variant'/'runtime: nvidia' attributes, the
one-shot Run copy script with Timeout 300 and requiresPrivilege, artifact
URIs/Unarchive/Permission), the empty top-level Lifecycle, and a
byte-identical ComponentDependencies passthrough (the recently-added
model-* and per-arch LocalServer floor entries included, alongside the
pinned-HARD dda.plugin.* entries; the key is omitted entirely when no
dependencies are supplied).

The assertions use the modulo-comparison technique from
test_workflow_packaging_recipe_preservation.py, applied to the field this
fix changes. REWORKED after the on-device counterexample that reverted the
first fix (a ``Shutdown: rm -rf {install_dir}`` step — Greengrass runs
Shutdown ~10ms after a one-shot Run exits 0, deleting the freshly staged
artifacts on every deploy; JP6 greengrass.log 00:28:58, workflow
modbus_test v1 never registered). The reworked fix moves the stale-version
cleanup INTO the Run script as a best-effort ``rm -rf`` prefix, so the
allowed delta is now: the recipe with the cleanup prefix stripped from
each manifest's Run script must equal the independently-computed
pre-stale-registrations golden, and NO manifest may carry a Shutdown key
anywhere. Any other change to any field fails the property. The cleanup
prefix's own content is asserted by the task-1 exploration test, not
here.

NOT duplicated here: the packaged llm_inference ``modelName`` rewrite is a
different code path (compiled-document assembly, not build_recipe) and is
already locked by its own suite (vllm-model-name-mismatch tests).

Harness: the pure ``build_recipe`` seam, imported inside the moto-backed
``aws_stack`` from conftest so the module-level boto3 clients never reach
real AWS — the same pattern as
test_workflow_packaging_recipe_preservation.py::TestBuildRecipePureContract.
"""
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

PLUGIN_PREFIX = "dda.plugin."

#: Independently-recorded platform mapping (golden, not imported from the
#: module under test): workflow_core arch id -> Greengrass architecture.
ARCH_TO_GG_PLATFORM = {
    "x86_64": "amd64",
    "x86_64_nvidia": "amd64",
    "arm64_jp4": "aarch64",
    "arm64_jp5": "aarch64",
    "arm64_jp6": "aarch64",
}

#: LocalServer variant component names as recorded from the unfixed
#: handler's ComponentDependencies output (edge-deploy-reliability) —
#: build_recipe passes these through untouched.
LOCAL_SERVER_COMPONENTS = (
    "aws.edgeml.dda.LocalServer.arm64JP4",
    "aws.edgeml.dda.LocalServer.arm64JP5",
    "aws.edgeml.dda.LocalServer.arm64JP6",
    "aws.edgeml.dda.LocalServer.amd64",
)

#: Model component names shaped like greengrass_publish output.
MODEL_COMPONENTS = (
    "model-defect-model",
    "model-vllm-opt125m-smoke",
    "model-scratch-detector",
)


# --------------------------------------------------------------------------
# The golden recipe contract (recorded from UNFIXED build_recipe output)
# --------------------------------------------------------------------------

def expected_manifest_order(archs):
    """Golden ordering: sorted, except plain x86_64 listed directly after
    x86_64_nvidia (both map to amd64; the nvidia manifest must match
    first)."""
    ordered = sorted(archs)
    if "x86_64" in ordered and "x86_64_nvidia" in ordered:
        ordered.remove("x86_64")
        ordered.insert(ordered.index("x86_64_nvidia") + 1, "x86_64")
    return ordered


def expected_unfixed_recipe(workflow_id, workflow_version, component_version,
                            bucket, archs, dependencies):
    """The COMPLETE registered recipe as observed on the unfixed tree —
    including the verbatim ComponentDependencies passthrough (key absent
    when no dependencies are supplied) — computed independently of
    workflow_packaging.py."""
    install_dir = "/aws_dda/workflows/{}/{}".format(
        workflow_id, workflow_version)
    arm_archs = [a for a in archs if ARCH_TO_GG_PLATFORM[a] == "aarch64"]
    disambiguate_arm = len(arm_archs) > 1

    manifests = []
    for arch in expected_manifest_order(archs):
        platform = {"os": "linux", "architecture": ARCH_TO_GG_PLATFORM[arch]}
        if disambiguate_arm and ARCH_TO_GG_PLATFORM[arch] == "aarch64":
            platform["variant"] = arch
        elif arch == "x86_64_nvidia":
            platform["runtime"] = "nvidia"
        manifests.append({
            "Platform": platform,
            "Lifecycle": {
                "Run": {
                    "Script": (
                        "mkdir -p {install} && cp -r "
                        "{{artifacts:decompressedPath}}/workflow-{arch}/. "
                        "{install}/".format(install=install_dir, arch=arch)),
                    "Timeout": 300,
                    "requiresPrivilege": True,
                },
            },
            "Artifacts": [{
                "Uri": ("s3://{bucket}/workflows/components/{wf}/{wfv}/{cv}/"
                        "{arch}/workflow-{arch}.zip".format(
                            bucket=bucket, wf=workflow_id,
                            wfv=workflow_version, cv=component_version,
                            arch=arch)),
                "Unarchive": "ZIP",
                "Permission": {"Read": "ALL"},
            }],
        })

    recipe = {
        "RecipeFormatVersion": "2020-01-25",
        "ComponentName": "dda.workflow.{}".format(workflow_id),
        "ComponentVersion": component_version,
        "ComponentType": "aws.greengrass.generic",
        "ComponentPublisher": "DDA Portal Workflow Manager",
        "ComponentConfiguration": {
            "DefaultConfiguration": {
                "WorkflowId": workflow_id,
                "WorkflowVersion": str(workflow_version),
            },
        },
        "Manifests": manifests,
        "Lifecycle": {},
    }
    if dependencies:
        recipe["ComponentDependencies"] = dependencies
    return recipe


def _strip_run_cleanup_prefix(script):
    """The Run script with its best-effort stale-version cleanup prefix
    (everything up to and including the first ``'; '``) removed — the ONLY
    delta the reworked fix is allowed to introduce to the Run script. On
    the pre-stale-registrations tree no prefix existed, so the golden
    carries the bare staging chain."""
    _cleanup, sep, staging = script.partition("; ")
    return staging if sep else script


def recipe_without_run_cleanup_prefix(recipe):
    """The recipe with the cleanup prefix stripped from each manifest's
    Run script — must equal the pre-stale-registrations golden in every
    field. Note this does NOT strip a Shutdown key: no manifest may carry
    one (asserted separately), since Greengrass fires Shutdown on one-shot
    Run completion and would destroy the staged artifacts."""
    stripped = dict(recipe)
    stripped["Manifests"] = [
        {**manifest,
         "Lifecycle": {
             k: ({**v, "Script": _strip_run_cleanup_prefix(v["Script"])}
                 if k == "Run" else v)
             for k, v in manifest["Lifecycle"].items()}}
        for manifest in recipe["Manifests"]
    ]
    return stripped


# --------------------------------------------------------------------------
# Fixtures / harness
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """workflow_packaging freshly imported inside the moto mock, so its
    module-level boto3 clients are intercepted (pure-seam pattern from
    test_workflow_packaging_recipe_preservation.py)."""
    for module_name in ("workflow_packaging", "node_catalog_resolution",
                        "model_registry_snapshot"):
        sys.modules.pop(module_name, None)
    import workflow_packaging

    yield SimpleNamespace(packaging=workflow_packaging)
    sys.modules.pop("workflow_packaging", None)


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

_ARCHS = sorted(ARCH_TO_GG_PLATFORM)

_workflow_ids = st.uuids().map(lambda u: f"wf-{u}")
_versions = st.integers(min_value=1, max_value=50)
_arch_sets = st.sets(st.sampled_from(_ARCHS), min_size=1)

# ComponentDependencies passthrough universe: the pinned-HARD dda.plugin.*
# entries, the recently-added unpinned model component entries, and the
# per-arch LocalServer floor entries — merged into one dict exactly as the
# packaging handler hands them to build_recipe.
_plugin_dep_entries = st.dictionaries(
    keys=st.uuids().map(lambda u: f"{PLUGIN_PREFIX}plg-{u}"),
    values=st.integers(min_value=1, max_value=9).map(
        lambda major: {
            "VersionRequirement": f">={major}.0.0 <{major + 1}.0.0",
            "DependencyType": "HARD",
        }),
    max_size=3,
)
_model_dep_entries = st.dictionaries(
    keys=st.sampled_from(MODEL_COMPONENTS),
    values=st.just({"VersionRequirement": ">=0.0.0",
                    "DependencyType": "HARD"}),
    max_size=2,
)
_local_server_dep_entries = st.dictionaries(
    keys=st.sampled_from(LOCAL_SERVER_COMPONENTS),
    values=st.integers(min_value=0, max_value=200).map(
        lambda patch: {"VersionRequirement": f">=1.0.{patch}",
                       "DependencyType": "HARD"}),
    max_size=2,
)
_dependency_dicts = st.tuples(
    _plugin_dep_entries, _model_dep_entries, _local_server_dep_entries,
).map(lambda parts: {**parts[0], **parts[1], **parts[2]})


# --------------------------------------------------------------------------
# Property
# --------------------------------------------------------------------------

class TestRecipePreservationModuloRunCleanup:

    @settings(max_examples=25, deadline=None)
    @given(workflow_id=_workflow_ids, workflow_version=_versions,
           archs=_arch_sets, dependencies=_dependency_dicts)
    def test_recipe_equals_unfixed_golden_modulo_run_cleanup_prefix(
            self, packaging_env, workflow_id, workflow_version, archs,
            dependencies):
        """**Property 2: Preservation — Deployed Version and Recipe
        Contract Unchanged (portal half)**

        For ANY workflow id, version, arch subset, and ComponentDependencies
        dict (dda.plugin.* pinned HARD entries, model component entries,
        per-arch LocalServer floors), the recipe with the stale-version
        cleanup prefix stripped from each manifest's Run script equals the
        independently-computed pre-stale-registrations golden in EVERY
        field: staging chain, Run Timeout/requiresPrivilege, artifact URIs,
        manifest ordering/platform attributes, configuration, empty
        top-level Lifecycle, and the byte-identical ComponentDependencies
        passthrough. Additionally, NO manifest carries a Shutdown key —
        Greengrass fires Shutdown ~10ms after a one-shot Run exits 0
        (verified on device), so a Shutdown step would destroy the freshly
        staged artifacts on every deploy.

        **Validates: Requirements 3.2**
        """
        component_version = f"{workflow_version}.0.0"
        bucket = "usecase-bucket-shutdown-preservation"
        final_keys = {
            arch: ("workflows/components/{wf}/{wfv}/{cv}/{arch}/"
                   "workflow-{arch}.zip".format(
                       wf=workflow_id, wfv=workflow_version,
                       cv=component_version, arch=arch))
            for arch in archs
        }

        recipe = packaging_env.packaging.build_recipe(
            workflow_id, workflow_version, bucket, final_keys,
            component_dependencies=dependencies or None)

        golden = expected_unfixed_recipe(
            workflow_id, workflow_version, component_version, bucket,
            archs, dependencies)

        assert recipe_without_run_cleanup_prefix(recipe) == golden, (
            "PRESERVATION REGRESSION (Property 2 / Requirement 3.2): the "
            "recipe differs from the unfixed golden in a field other than "
            "the Run script's stale-version cleanup prefix (archs={})".format(
                sorted(archs)))

        # No Shutdown key anywhere: the reverted first fix's Shutdown fired
        # on one-shot Run completion and deleted the staged artifacts.
        for manifest in recipe["Manifests"]:
            assert "Shutdown" not in manifest["Lifecycle"], (
                "REGRESSION (Requirement 3.2): manifest for platform "
                "{platform!r} carries a Shutdown step, which Greengrass "
                "runs right after the one-shot Run exits 0".format(
                    platform=manifest.get("Platform")))

        # The passthrough restriction, stated explicitly: supplied
        # dependencies appear byte-identical; none supplied -> key absent.
        if dependencies:
            assert recipe["ComponentDependencies"] == dependencies
        else:
            assert "ComponentDependencies" not in recipe
