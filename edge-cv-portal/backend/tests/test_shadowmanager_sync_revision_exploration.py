"""Bug condition exploration test — shadowmanager-sync-config-on-revision
task 1 (backend leg, cases 1-3).

Bugfix spec: .kiro/specs/shadowmanager-sync-config-on-revision/

Reproduces C(X): a portal deployment submission whose component set already
contains ``aws.greengrass.ShadowManager`` — bare, or with a stale
``synchronize`` merge missing portal shadow names. The auto-include gate in
``create_deployment`` (``if needs_nucleus and 'aws.greengrass.ShadowManager'
not in components_map:``) skips entirely, and ``create_workflow_deployment``
copies the previous revision's components verbatim with no ShadowManager
logic — so the bare/stale entry ships, Greengrass preserves the device's
last-applied config, and shadow names added to the portal auto-include list
after the device's last CONFIGURED revision (``dda-model-status``) never
sync to IoT Core.

Live incident (jetson-thor1): revision 2 (Aug 14) was the last revision with
a ShadowManager ``configurationUpdate`` (the OLD two-shadow list); revisions
3-10 all bare (``{"componentVersion": "2.3.15"}``); the device writes the
``dda-model-status`` shadow locally but cloud ``get-thing-shadow`` returns
``ResourceNotFoundException`` — the model-gpu-fallback-visibility portal
panel is broken fleet-wide for every revised device.

These tests assert the FIXED behavior and are EXPECTED TO FAIL on unfixed
code — the failures prove the bug exists. After task 3.1 lands
``ensure_shadow_manager_sync`` at both call sites, this same file validates
the fix (task 3.3).

Honesty guard (design Decision 7): every assertion here is about the
SUBMITTED deployment document — the FakeGreengrass ``create_deployment_calls``
capture — never about a real device re-syncing shadows to IoT Core (that is
the task-9 USER ACTION on thor1).

Property 1: Bug Condition (Fix Check) — Revised deployments carry the full
portal shadow sync.
**Validates: Requirements 2.1, 2.2, 2.3, 2.5** (defects: 1.1, 1.2, 1.3, 1.4)

Harnesses: ShadowManagerEnv (endpoint-level create_deployment) from
test_deployment_shadow_manager.py for cases 1-2, and WorkflowDeployEnv
(endpoint-level create_workflow_deployment with a seeded previous
latest-for-target deployment) from
test_workflow_deploy_subscribe_merge_exploration.py for case 3.
"""
import json
import sys

import pytest

from test_deployment_shadow_manager import (
    LOCAL_SERVER_COMPONENT, ShadowManagerEnv)
from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, WorkflowDeployEnv)

# The portal shadow names (deployments.py module constants; hardcoded here
# on purpose so the test pins the CONTRACT, not whatever the constants say).
PORTAL_SHADOW_NAMES = ["dda-camera-registry", "dda-camera-bindings",
                       "dda-model-status"]

# The EXACT thor1 revision shape (bugfix.md Incident Record): what the UI
# revise flow resubmits for revisions 3-10 — name + version only, the
# configurationUpdate structurally dropped by the prefill API.
THOR1_BARE_SHADOW_MANAGER = {
    "component_name": "aws.greengrass.ShadowManager",
    "component_version": "2.3.15",
}

# The OLD rev-2 merge (defect 1.4's origin): the two-shadow list that
# predates model-gpu-fallback-visibility, with direction/classic set —
# exactly the last CONFIGURED revision's synchronize document on thor1.
STALE_REV2_MERGE = {
    "synchronize": {
        "direction": "betweenDeviceAndCloud",
        "coreThing": {
            "classic": True,
            "namedShadows": ["dda-camera-registry", "dda-camera-bindings"],
        },
    }
}


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture
def sm_env(env, deployments, monkeypatch):
    return ShadowManagerEnv(env, deployments, monkeypatch)


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return WorkflowDeployEnv(env, deployments, monkeypatch)


def submitted_named_shadows(entry):
    """synchronize.coreThing.namedShadows of a submitted components-map
    entry's parsed configurationUpdate.merge; [] when the entry is bare."""
    merge = (entry.get("configurationUpdate") or {}).get("merge")
    if not merge:
        return []
    doc = json.loads(merge)
    return (doc.get("synchronize", {})
               .get("coreThing", {})
               .get("namedShadows", []))


# ==========================================================================
# Case 1 — Revise-shape bare entry through create_deployment (defects
# 1.1/1.2, Requirements 2.1, 2.5)
# ==========================================================================

class TestCase1ReviseShapeBareEntry:
    """**Validates: Requirements 2.1, 2.5** (defects 1.1, 1.2).

    Deploy LocalServer + the EXACT thor1 bare caller-supplied ShadowManager
    entry. The fixed path must inject the full portal synchronize merge into
    the submitted entry while keeping the caller's explicit 2.3.15 pin."""

    def test_submitted_bare_entry_carries_full_portal_merge(self, sm_env):
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT, dict(THOR1_BARE_SHADOW_MANAGER)],
            target_devices=["jetson-thor1"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        entry = call["components"]["aws.greengrass.ShadowManager"]

        # The caller's explicit pin survives (3.5) — true before AND after.
        assert entry.get("componentVersion") == "2.3.15"

        # No auto_included ShadowManager entry either way: on unfixed code
        # the gate skipped; on fixed code a merge-into-existing is logged,
        # not reported (design Decision 4).
        assert [e for e in payload["auto_included"]
                if e["component_name"] == "aws.greengrass.ShadowManager"] == []

        # BUG (1.1/1.2): on unfixed code the gate skips entirely and the
        # submitted entry is byte-equal to the caller's bare entry —
        # {"componentVersion": "2.3.15"}, no configurationUpdate — so
        # Greengrass preserves the device's stale rev-2 shadow config.
        shadows = submitted_named_shadows(entry)
        assert set(PORTAL_SHADOW_NAMES) <= set(shadows), (
            "submitted ShadowManager entry does not carry the full portal "
            f"synchronize merge; expected namedShadows ⊇ "
            f"{PORTAL_SHADOW_NAMES}, submitted entry={entry!r}")


# ==========================================================================
# Case 2 — Stale two-shadow merge through create_deployment (defect 1.4's
# origin, Requirements 2.1, 2.2, 2.5)
# ==========================================================================

class TestCase2StaleTwoShadowMerge:
    """**Validates: Requirements 2.1, 2.2, 2.5** (defect 1.4's origin).

    The caller entry carries the OLD rev-2 merge (dda-camera-registry +
    dda-camera-bindings only, direction/classic set). The fixed path must
    union dda-model-status into the submitted merge while the two existing
    names and the direction/classic values survive.

    Counterexample note (observed on unfixed code): create_deployment's
    body parsing keeps only component_name + component_version — the
    caller's configurationUpdate is structurally dropped (the endpoint twin
    of the get_target_deployment prefill strip, defect 1.2) — and the
    presence gate then skips the auto-include, so the SUBMITTED entry is
    bare: neither the caller's two-shadow merge nor the portal merge ships.
    """

    def test_model_status_unioned_and_rev2_names_survive(self, sm_env):
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT,
             {"component_name": "aws.greengrass.ShadowManager",
              "component_version": "2.3.15",
              "configurationUpdate": {
                  "merge": json.dumps(STALE_REV2_MERGE)}}],
            target_devices=["jetson-thor1"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        entry = call["components"]["aws.greengrass.ShadowManager"]
        assert entry.get("componentVersion") == "2.3.15"

        # BUG (1.4 origin): on unfixed code the submitted entry carries no
        # synchronize merge at all (see the class docstring) — and a stale
        # merge, wherever it enters a components_map, ships unchanged with
        # two names. Fixed contract: dda-model-status is UNIONED in...
        shadows = submitted_named_shadows(entry)
        assert "dda-model-status" in shadows, (
            "dda-model-status was not unioned into the submitted "
            f"ShadowManager merge; submitted entry={entry!r}")
        # ...AND the two rev-2 names survive (union — never replace).
        assert "dda-camera-registry" in shadows
        assert "dda-camera-bindings" in shadows

        # The rev-2 field values survive/hold (3.3 — for this shape they
        # equal the portal defaults, so this passes on the fixed tree
        # whether the merge was unioned or freshly injected).
        merged = json.loads(entry["configurationUpdate"]["merge"])
        assert merged["synchronize"]["direction"] == "betweenDeviceAndCloud"
        assert merged["synchronize"]["coreThing"]["classic"] is True


# ==========================================================================
# Case 3 — Workflow revision copies the bare entry forward (defect 1.3,
# Requirements 2.3, 2.5)
# ==========================================================================

class TestCase3WorkflowRevisionCopiesBareEntryForward:
    """**Validates: Requirements 2.3, 2.5** (defect 1.3).

    The target's previous latest-for-target deployment carries LocalServer +
    bare ShadowManager (the thor1 shape). A workflow deployment revision
    copies that components map forward; the fixed path must apply the
    ensure/merge step to the copied entry — full three-shadow merge, the
    copied 2.3.15 componentVersion kept."""

    def test_copied_bare_entry_gets_full_merge_and_version_survives(
            self, wf_env):
        # No subscribed topics: the subscribe accessControl merge stays
        # quiet so the submitted document isolates the ShadowManager copy.
        workflow_id = wf_env.seed_subscribing_workflow(topics=None)
        wf_env.register_device()
        wf_env.gg.seed_deployment(wf_env.thing_arn(), {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            "aws.greengrass.ShadowManager": {"componentVersion": "2.3.15"},
        })

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True

        components = wf_env.submitted_components()
        # Carry-over semantics intact: LocalServer copied, workflow entry
        # placed (3.6 — true before AND after the fix).
        assert LOCAL_SERVER_ARM64JP6 in components
        assert f"dda.workflow.{workflow_id}" in components

        entry = components["aws.greengrass.ShadowManager"]
        # The copied explicit version survives (3.5).
        assert entry.get("componentVersion") == "2.3.15"

        # BUG (1.3): on unfixed code components_map is copied verbatim —
        # create_workflow_deployment has no ShadowManager logic at all —
        # so the submitted entry is the bare {"componentVersion": "2.3.15"}
        # forever, revision after revision.
        shadows = submitted_named_shadows(entry)
        assert set(PORTAL_SHADOW_NAMES) <= set(shadows), (
            "workflow revision copied the bare ShadowManager entry forward "
            f"without the portal synchronize merge; expected namedShadows ⊇ "
            f"{PORTAL_SHADOW_NAMES}, submitted entry={entry!r}")
