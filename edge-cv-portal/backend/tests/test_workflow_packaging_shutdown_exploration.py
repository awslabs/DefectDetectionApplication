"""Bug-condition exploration test (Task 1, portal half) for
stale-workflow-registrations — REWORKED after an on-device counterexample.

Property 1: Bug Condition — Stale Versions Are Retired and Cleaned
(portal packaging half, Property 1(a)).

History of this property:

1. Original bug: ``build_recipe`` emitted only the one-shot ``Run`` copy
   lifecycle and no cleanup at all, so replaced versions' staged files
   accumulated forever (JP6 evidence: dirs 2/, 6/, 7/ on disk while only
   component v7.0.0 was deployed).
2. First fix (now REVERTED): a ``Shutdown: rm -rf {install_dir}`` step per
   manifest, on the design assumption that Greengrass runs Shutdown only on
   component replace/remove. **On-device counterexample (JP6 greengrass.log
   at 00:28:58): Greengrass transitions a FINISHED one-shot generic
   component RUNNING → STOPPING as soon as its Run script exits 0
   ("generic-service-stopping. Service finished running") and executes
   Shutdown ~10ms later — the rm -rf deleted the freshly staged artifacts
   on EVERY deploy. /aws_dda/workflows/e830f55d-5744-4edf-be43-1a33fbd4605d/
   was left empty and workflow modbus_test v1 never registered.**
3. Reworked fix (asserted here): NO Shutdown (or any other lifecycle step
   that fires on Run completion); instead the Run script itself performs
   the stale-version cleanup — best-effort ``rm -rf`` of the workflow's
   root (all previously staged versions) BEFORE the mandatory
   ``mkdir && cp`` staging chain. Cleanup thus happens exactly when a new
   version's Run executes (every component version change), and the
   re-copy-on-restart behavior is unchanged.

Harness: the pure-seam pattern from
test_workflow_packaging_recipe_preservation.py::TestBuildRecipePureContract
— workflow_packaging imported inside the moto mock (aws_stack) with a
dedicated training-jobs table, then ``build_recipe`` property-tested
directly across random workflow ids, versions, and arch subsets.

**Validates: Requirements 1.1** (expected behavior 2.1)
"""
import os
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = (
    "test-stale-workflow-registrations-exploration-training-jobs")

#: The device workflow staging root the recipe copies artifacts under —
#: recorded independently of the module under test.
DEVICE_WORKFLOWS_ROOT = "/aws_dda/workflows"

#: All device architectures build_recipe accepts (portal catalog).
ARCHS = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"]

#: Lifecycle steps Greengrass executes as part of the stop sequence of a
#: FINISHED one-shot generic component (Shutdown ~10ms after Run exits 0,
#: verified on the JP6 device) or as recovery hooks. ANY of these on a
#: one-shot dda.workflow.* component can fire right after a successful Run
#: and destroy the freshly staged artifacts — none may ever appear.
FORBIDDEN_LIFECYCLE_STEPS = ("Shutdown", "Startup", "Recover")


@pytest.fixture(scope="module")
def packaging_env(aws_stack):
    """workflow_packaging imported inside moto with its training-jobs
    table (the same setup as the recipe-preservation suite's pure-seam
    fixture, with a distinct table name so suites never collide)."""
    import boto3

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

    yield SimpleNamespace(packaging=workflow_packaging)
    os.environ.pop("TRAINING_JOBS_TABLE", None)
    sys.modules.pop("workflow_packaging", None)


_workflow_ids = st.uuids().map(lambda u: f"wf-{u}")
_versions = st.integers(min_value=1, max_value=50)
_arch_sets = st.sets(st.sampled_from(ARCHS), min_size=1)


def _build_recipe(packaging_env, workflow_id, workflow_version, archs):
    component_version = f"{workflow_version}.0.0"
    bucket = "usecase-bucket-shutdown-exploration"
    final_keys = {
        arch: ("workflows/components/{wf}/{wfv}/{cv}/{arch}/"
               "workflow-{arch}.zip".format(
                   wf=workflow_id, wfv=workflow_version,
                   cv=component_version, arch=arch))
        for arch in archs
    }
    return packaging_env.packaging.build_recipe(
        workflow_id, workflow_version, bucket, final_keys)


class TestRunScriptStaleVersionCleanup:
    """Property 1(a), reworked: cleanup lives in the Run script (before
    staging), and no lifecycle step that fires on Run completion exists."""

    @settings(max_examples=25, deadline=None)
    @given(workflow_id=_workflow_ids, workflow_version=_versions,
           archs=_arch_sets)
    def test_run_script_cleans_stale_versions_before_staging(
            self, packaging_env, workflow_id, workflow_version, archs):
        """For ANY workflow id, workflow version, and arch subset, every
        manifest of the generated ``dda.workflow.{id}`` recipe must carry
        ONLY a one-shot ``Run`` lifecycle whose script (1) best-effort
        removes the workflow's staging root ``/aws_dda/workflows/{id}``
        (all previously staged versions) BEFORE staging, scoped to exactly
        this workflow's directory — never the shared workflows root and
        never another workflow, and (2) then mandatorily stages the
        incoming version into ``/aws_dda/workflows/{id}/{version}`` via
        ``mkdir -p && cp -r``, with the cleanup joined by ``;`` so a
        cleanup failure can never block staging.

        The cleanup deliberately removes the incoming version's own dir
        too (it is re-created and re-copied immediately after), making the
        script idempotent across component restarts and same-version
        re-packages.

        Validates: Requirements 1.1 (expected behavior 2.1)
        """
        recipe = _build_recipe(
            packaging_env, workflow_id, workflow_version, archs)

        workflow_root = "{root}/{wf}".format(
            root=DEVICE_WORKFLOWS_ROOT, wf=workflow_id)
        install_dir = "{root}/{wfv}".format(
            root=workflow_root, wfv=workflow_version)

        assert recipe["Manifests"], "build_recipe emitted no manifests"
        for manifest in recipe["Manifests"]:
            lifecycle = manifest["Lifecycle"]
            platform = manifest.get("Platform")

            assert set(lifecycle) == {"Run"}, (
                "COUNTEREXAMPLE (Req 1.1): manifest for platform "
                "{platform!r} has Lifecycle keys {keys!r} — these one-shot "
                "components must carry ONLY the Run step; any other step "
                "can fire on Run completion (on-device evidence: Shutdown "
                "ran ~10ms after Run exited 0 and rm -rf'd the freshly "
                "staged artifacts)".format(
                    platform=platform, keys=sorted(lifecycle)))

            script = str(lifecycle["Run"].get("Script", ""))

            # (1) Cleanup prefix: best-effort removal of THIS workflow's
            # staging root, terminated by ';' so failure never blocks the
            # staging chain.
            cleanup, sep, staging = script.partition("; ")
            assert sep, (
                "COUNTEREXAMPLE (Req 1.1): Run script for platform "
                "{platform!r} has no best-effort cleanup prefix "
                "(no ';' separator): {script!r} — stale sibling versions "
                "would accumulate on device disk forever".format(
                    platform=platform, script=script))
            assert cleanup.startswith("rm -rf "), (
                "COUNTEREXAMPLE (Req 1.1): Run script cleanup prefix "
                "{cleanup!r} does not remove stale versions".format(
                    cleanup=cleanup))
            # Scoped to exactly this workflow's root: removes stale sibling
            # versions, never the shared workflows root / other workflows.
            assert f"rm -rf {workflow_root} " in cleanup + " ", (
                "COUNTEREXAMPLE (Req 1.1): cleanup {cleanup!r} does not "
                "target this workflow's staging root {root!r}".format(
                    cleanup=cleanup, root=workflow_root))
            assert f"rm -rf {DEVICE_WORKFLOWS_ROOT} " not in cleanup + " ", (
                "UNSAFE: cleanup {cleanup!r} removes the SHARED workflows "
                "root {root!r} (would destroy other workflows)".format(
                    cleanup=cleanup, root=DEVICE_WORKFLOWS_ROOT))

            # (2) Mandatory staging chain after the cleanup: mkdir && cp
            # into the incoming version's install dir.
            assert staging.startswith(f"mkdir -p {install_dir} && "), (
                "COUNTEREXAMPLE (Req 1.1): staging {staging!r} does not "
                "re-create the install dir {install!r} after cleanup".format(
                    staging=staging, install=install_dir))
            assert f" {install_dir}/" in staging and "cp -r " in staging, (
                "COUNTEREXAMPLE (Req 1.1): staging {staging!r} does not "
                "copy the artifacts into {install!r}".format(
                    staging=staging, install=install_dir))

    @settings(max_examples=25, deadline=None)
    @given(workflow_id=_workflow_ids, workflow_version=_versions,
           archs=_arch_sets)
    def test_no_lifecycle_step_that_fires_on_run_completion(
            self, packaging_env, workflow_id, workflow_version, archs):
        """REGRESSION GUARD encoding the discovered on-device bug: no
        manifest Lifecycle may contain a Shutdown, Startup, or Recover
        step for these one-shot components.

        Why: Greengrass transitions a FINISHED generic component
        RUNNING → STOPPING as soon as its one-shot Run script exits 0 and
        runs Shutdown ~10ms later (JP6 greengrass.log 00:28:58: Run exit 0
        → "generic-service-stopping. Service finished running" →
        "Shutdown initiated" → rm -rf). Any such step can therefore
        execute right after every successful deploy and destroy the
        freshly staged artifacts — the first fix's
        ``Shutdown: rm -rf {install_dir}`` left
        /aws_dda/workflows/e830f55d-.../ empty and the workflow
        unregistered. Cleanup must live inside the Run script instead.

        Validates: Requirements 1.1 (expected behavior 2.1)
        """
        recipe = _build_recipe(
            packaging_env, workflow_id, workflow_version, archs)

        for manifest in recipe["Manifests"]:
            lifecycle = manifest["Lifecycle"]
            forbidden = [step for step in FORBIDDEN_LIFECYCLE_STEPS
                         if step in lifecycle]
            assert not forbidden, (
                "REGRESSION (Req 1.1): manifest for platform {platform!r} "
                "carries lifecycle step(s) {steps!r} — on a one-shot "
                "generic component these fire on Run completion and can "
                "delete the freshly staged artifacts (verified on-device: "
                "the reverted Shutdown fix destaged every deploy)".format(
                    platform=manifest.get("Platform"), steps=forbidden))
