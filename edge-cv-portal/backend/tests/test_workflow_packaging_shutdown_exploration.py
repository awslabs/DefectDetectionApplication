"""Bug-condition exploration test (Task 1, portal half) for
stale-workflow-registrations.

Property 1: Bug Condition — Stale Versions Are Retired and Cleaned
(portal packaging half, Property 1(a)).

**These tests assert the FIXED (post-fix) build_recipe behavior, so they
are EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the
counterexample confirming the bug: ``workflow_packaging.py::build_recipe``
emits only a one-shot ``Run`` lifecycle per platform manifest (``mkdir -p``
+ ``cp -r`` into ``/aws_dda/workflows/{id}/{version}``) and NO ``Shutdown``
cleanup, so when Greengrass replaces component version N with N+1 (or
removes the component) version N's staged files remain on device disk
forever — the root cause of the stale directories (2/, 6/, 7/) verified
live on the JP6 device.

Expected counterexample on the UNFIXED tree:
    build_recipe(...)['Manifests'][i]['Lifecycle'] == {'Run': ...}
    (no 'Shutdown' key on any manifest, for every arch subset)

The SAME test is re-run in task 5.1 against the fixed build_recipe
(each manifest's Lifecycle gains a Shutdown removing the install dir),
where it must PASS.

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


class TestRecipeShutdownCleanup:
    """Property 1(a): every generated platform manifest carries a Shutdown
    lifecycle step that removes the workflow version's install directory."""

    @settings(max_examples=25, deadline=None)
    @given(workflow_id=_workflow_ids, workflow_version=_versions,
           archs=_arch_sets)
    def test_every_manifest_carries_install_dir_removing_shutdown(
            self, packaging_env, workflow_id, workflow_version, archs):
        """For ANY workflow id, workflow version, and arch subset, every
        manifest of the generated ``dda.workflow.{id}`` recipe must carry
        a ``Shutdown`` lifecycle step whose script removes
        ``/aws_dda/workflows/{id}/{workflow_version}`` — so Greengrass
        cleans the outgoing version's staged files on replace/remove.

        EXPECTED FAILURE on the unfixed tree: the Lifecycle carries only
        the one-shot ``Run`` copy script — no Shutdown key at all — so
        replaced versions' staged directories persist forever (the JP6
        device evidence: dirs 2/, 6/, 7/ all on disk while only component
        v7.0.0 is deployed).

        Validates: Requirements 1.1 (expected behavior 2.1)
        """
        component_version = f"{workflow_version}.0.0"
        bucket = "usecase-bucket-shutdown-exploration"
        final_keys = {
            arch: ("workflows/components/{wf}/{wfv}/{cv}/{arch}/"
                   "workflow-{arch}.zip".format(
                       wf=workflow_id, wfv=workflow_version,
                       cv=component_version, arch=arch))
            for arch in archs
        }

        recipe = packaging_env.packaging.build_recipe(
            workflow_id, workflow_version, bucket, final_keys)

        install_dir = "{root}/{wf}/{wfv}".format(
            root=DEVICE_WORKFLOWS_ROOT, wf=workflow_id, wfv=workflow_version)

        assert recipe["Manifests"], "build_recipe emitted no manifests"
        for manifest in recipe["Manifests"]:
            lifecycle = manifest["Lifecycle"]
            assert "Shutdown" in lifecycle, (
                "COUNTEREXAMPLE (Req 1.1): manifest for platform {platform!r} "
                "has Lifecycle == {lifecycle!r} — only the one-shot Run copy "
                "script, no Shutdown cleanup — so when Greengrass replaces or "
                "removes this component version, its staged files under "
                "{install!r} remain on device disk forever".format(
                    platform=manifest.get("Platform"),
                    lifecycle=lifecycle,
                    install=install_dir))

            script = str(lifecycle["Shutdown"].get("Script", ""))
            assert "rm" in script and install_dir in script, (
                "COUNTEREXAMPLE (Req 1.1): manifest for platform {platform!r} "
                "carries a Shutdown step but its script {script!r} does not "
                "remove the install directory {install!r}".format(
                    platform=manifest.get("Platform"),
                    script=script,
                    install=install_dir))
