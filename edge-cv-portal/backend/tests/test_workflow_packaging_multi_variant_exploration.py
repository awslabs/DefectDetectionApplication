# -*- coding: utf-8 -*-
"""Bug-condition exploration test (Task 11, case 8) for edge-deploy-reliability.

Property 11: Bug Condition — Packaged workflow components are deployable on
every targeted device (Defect F, `isBugCondition_F`).

**These tests assert the FIXED (post-F) packaging output, so they are
EXPECTED TO FAIL on the F-UNFIXED tree.** The failure is the counterexample
confirming the defect: the Defect C fix's `local_server_component_dependencies`
emits one HARD LocalServer entry per distinct variant of the selected
architectures into the single recipe-GLOBAL ComponentDependencies block.
Greengrass ComponentDependencies is not per-platform-manifest, so a workflow
packaged for architectures spanning distinct LocalServer variants produces a
component whose dependency closure cannot co-resolve on ANY single device.

Verified incident: `dda.workflow.f81a4c66-...` v1.0.0 was packaged for
`arm64_jp5` + `arm64_jp6`; its recipe carried HARD deps on BOTH
`aws.edgeml.dda.LocalServer.arm64JP5` and `...arm64JP6`; deployment 44f2c596
to the JP6 device ryan-orin-nano failed `FAILED_ROLLBACK_COMPLETE: Service
aws.edgeml.dda.LocalServer.arm64JP5 in broken state after deployment` —
Greengrass resolved the recipe's dependency closure and installed the JP5
variant on the JP6 device even though the deployment document listed only
arm64JP6 components. The upstream deployment-service arch gates never inspect
the recipe's dependency closure, so the Defect C design's documented
mitigation does not engage (1.16).

The SAME tests are re-run in task 13.2 against the fixed
`workflow_packaging.py`, where they must PASS: exactly one LocalServer entry
when the selected architectures collapse to one distinct variant (|V| = 1),
zero LocalServer entries when they span multiple variants (|V| > 1).

Defect F is a deterministic pure-function defect, so the tests call
`local_server_component_dependencies` directly (the
test_workflow_packaging_variant_min_version.py harness pattern: the module is
imported inside the moto mock only because its module-level boto3 clients
demand it).

Validates: Requirements 1.14, 1.15, 1.16
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

LOCAL_SERVER_PREFIX = "aws.edgeml.dda.LocalServer."

# The arch -> LocalServer variant partition from the design (Fix
# Implementation §7 / greengrass_publish.TARGET_TO_LOCAL_SERVER naming
# discipline): JP4/JP5/JP6 are distinct variants; both x86_64 flavors
# collapse to the single amd64 variant. Hardcoded as an independent oracle
# rather than read back from the module under test.
VARIANT_OF = {
    "arm64_jp4": "aws.edgeml.dda.LocalServer.arm64JP4",
    "arm64_jp5": "aws.edgeml.dda.LocalServer.arm64JP5",
    "arm64_jp6": "aws.edgeml.dda.LocalServer.arm64JP6",
    "x86_64": "aws.edgeml.dda.LocalServer.amd64",
    "x86_64_nvidia": "aws.edgeml.dda.LocalServer.amd64",
}
ARCHS = sorted(VARIANT_OF)


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


def local_server_entries(dependencies):
    """The LocalServer ComponentDependencies entries among the emitted ones."""
    return {name: entry for name, entry in dependencies.items()
            if name.startswith(LOCAL_SERVER_PREFIX)}


# --------------------------------------------------------------------------
# Hypothesis strategy: non-empty arch subsets of
# {arm64_jp4, arm64_jp5, arm64_jp6, x86_64, x86_64_nvidia} that map to MORE
# THAN ONE distinct LocalServer variant (isBugCondition_F). Any 2+ subset
# qualifies except {x86_64, x86_64_nvidia}, which collapses to amd64.
# --------------------------------------------------------------------------

multi_variant_arch_sets = st.sets(
    st.sampled_from(ARCHS), min_size=2, max_size=len(ARCHS),
).filter(lambda archs: len({VARIANT_OF[a] for a in archs}) > 1)


class TestMultiVariantLocalServerEmission:
    """Exploration case 8 — multi-variant emission (isBugCondition_F)."""

    def test_incident_arch_pair_emits_at_most_one_variant(self, packaging):
        """isBugCondition_F, the verified incident's recipe shape: the
        unfixed local_server_component_dependencies(['arm64_jp5',
        'arm64_jp6']) emits HARD entries for BOTH LocalServer.arm64JP5 and
        .arm64JP6 into the recipe-global dependency block — the exact shape
        that made dda.workflow.f81a4c66 v1.0.0 undeployable (deployment
        44f2c596 to ryan-orin-nano: FAILED_ROLLBACK_COMPLETE, the JP5
        variant broken on the JP6 device).

        Deployability requires at most one distinct LocalServer variant in
        the emitted entries.

        Validates: Requirements 1.14, 1.15 (expected behavior 2.16)
        """
        dependencies = packaging.local_server_component_dependencies(
            ["arm64_jp5", "arm64_jp6"])

        emitted = local_server_entries(dependencies)
        assert len(emitted) <= 1, (
            "COUNTEREXAMPLE (Defect F): local_server_component_dependencies("
            "['arm64_jp5', 'arm64_jp6']) emitted {} distinct LocalServer "
            "variants into the single recipe-global ComponentDependencies "
            "block: {} — the incident recipe shape (dda.workflow.f81a4c66 "
            "v1.0.0, deployment 44f2c596, FAILED_ROLLBACK_COMPLETE on "
            "ryan-orin-nano). No single device can co-resolve both variants."
            .format(len(emitted),
                    {name: entry for name, entry in sorted(emitted.items())}))

    # Example count comes from the conftest hypothesis profile: 25 for fast
    # local runs (portal-fast), 100 (the spec minimum) with
    # HYPOTHESIS_PROFILE=ci.
    @settings(deadline=None)
    @given(archs=multi_variant_arch_sets)
    def test_multi_variant_selection_emits_zero_local_server_entries(
            self, packaging, archs):
        """Property 11 (Bug Condition): for ANY non-empty arch subset of
        {arm64_jp4, arm64_jp5, arm64_jp6, x86_64, x86_64_nvidia} mapping to
        more than one distinct LocalServer variant, the fixed
        local_server_component_dependencies emits ZERO LocalServer entries —
        the recipe's dependency closure never carries a LocalServer variant
        that cannot co-resolve with the device's own variant.

        FAILS on F-unfixed code, which emits one HARD entry per distinct
        variant (isBugCondition_F).

        Validates: Requirements 1.14, 1.15, 1.16 (expected behavior 2.16)
        """
        expected_variants = {VARIANT_OF[a] for a in archs}
        assert len(expected_variants) > 1  # strategy invariant

        dependencies = packaging.local_server_component_dependencies(
            sorted(archs))

        emitted = local_server_entries(dependencies)
        assert emitted == {}, (
            "COUNTEREXAMPLE (Defect F): architecture selection {} spans {} "
            "distinct LocalServer variants, yet "
            "local_server_component_dependencies emitted {} into the "
            "recipe-global block — an undeployable component on every "
            "targeted device (Greengrass installs the full dependency "
            "closure regardless of the deployment document's component list)."
            .format(sorted(archs), len(expected_variants), sorted(emitted)))
