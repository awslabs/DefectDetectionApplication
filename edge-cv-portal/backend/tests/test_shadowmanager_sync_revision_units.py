"""Fix-checking unit tests — shadowmanager-sync-config-on-revision
task 4.1 (design Testing Strategy, Unit Tests).

Bugfix spec: .kiro/specs/shadowmanager-sync-config-on-revision/

Property 3: Fix Checking — Union-Not-Replace Merge Semantics.
**Validates: Requirements 2.1, 2.2, 3.2, 3.3, 3.5**

Example-style companions to the Property 3 PBT in
test_shadowmanager_sync_revision_properties.py:

- helper level (``ensure_shadow_manager_sync`` directly, stub resolver):
  bare entry → the full portal merge injected; corrupt (non-JSON) merge →
  replaced with the full portal config + a warning logged; non-list
  ``namedShadows`` → replaced with the portal names (+ warning), siblings
  kept;
- endpoint level ('added'/'merged' reporting, design Decision 4):
  a fresh deploy's ``auto_included`` ShadowManager entry keeps the exact
  pre-fix shape (component_name / component_version / reason, verbatim);
  a 'merged' completion adds NO ``auto_included`` entry;
- workflow endpoint (presence gate, 3.6 scope guard): a FRESH workflow
  deployment (no previous revision) submits NO ShadowManager entry — the
  ensure step must not start auto-including it on that path.

Honesty guard (design Decision 7): the endpoint assertions are about the
SUBMITTED deployment document (the FakeGreengrass
``create_deployment_calls`` capture) — nothing here touches a real device
or account.
"""
import json
import logging
import sys

import pytest

from test_deployment_shadow_manager import (
    LOCAL_SERVER_COMPONENT, ShadowManagerEnv)
from test_workflow_deploy_subscribe_merge_exploration import (
    WorkflowDeployEnv)

SHADOW_MANAGER = "aws.greengrass.ShadowManager"

# The portal shadow names (contract-pinned, like the sibling spec suites).
PORTAL_SHADOW_NAMES = ["dda-camera-registry", "dda-camera-bindings",
                       "dda-model-status"]

# The full portal synchronize document (same key order as
# portal_shadow_sync_config(), so json.dumps reproduces the injected merge
# string byte-for-byte).
PORTAL_SYNC_CONFIG = {
    "synchronize": {
        "direction": "betweenDeviceAndCloud",
        "coreThing": {
            "classic": True,
            "namedShadows": PORTAL_SHADOW_NAMES,
        },
    }
}

# The fresh-add auto_included reason string, verbatim (the task-2
# preservation pin — the 'added' path must keep reproducing it).
FRESH_ADD_REASON = (
    "Syncs the dda-camera-registry, dda-camera-bindings and "
    "dda-model-status named shadows with IoT Core for camera registry "
    "synchronization and model GPU-fallback status visibility")


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


def run_helper(deployments, entry):
    """ensure_shadow_manager_sync on a single-entry components map with a
    stub resolver. Returns (result, submitted_entry)."""
    components_map = {SHADOW_MANAGER: entry}
    result = deployments.ensure_shadow_manager_sync(
        components_map, lambda: "9.9.9-stub")
    return result, components_map[SHADOW_MANAGER]


# ==========================================================================
# Helper level — merge injection and corrupt-input handling
# ==========================================================================

class TestHelperMergeInjection:
    """**Validates: Requirements 2.1, 2.2, 3.2** (helper level)."""

    def test_bare_entry_gets_full_portal_merge(self, deployments):
        """A bare entry (the exact thor1 revision shape — componentVersion
        only, no configurationUpdate) is completed with the full portal
        synchronize merge; the explicit version survives.
        # Validates: Requirements 2.1"""
        result, entry = run_helper(
            deployments, {"componentVersion": "2.3.15"})

        assert result == "merged"
        assert entry["componentVersion"] == "2.3.15"
        assert (entry["configurationUpdate"]["merge"]
                == json.dumps(PORTAL_SYNC_CONFIG))

    def test_corrupt_merge_replaced_with_portal_config_and_warning_logged(
            self, deployments, caplog):
        """An unparseable merge string is replaced with the full portal
        config — nothing recoverable exists to preserve — and the
        corruption is logged as a warning naming the component.
        # Validates: Requirements 2.1"""
        with caplog.at_level(logging.WARNING):
            result, entry = run_helper(deployments, {
                "componentVersion": "2.3.15",
                "configurationUpdate": {"merge": "not-json{"},
            })

        assert result == "merged"
        assert (entry["configurationUpdate"]["merge"]
                == json.dumps(PORTAL_SYNC_CONFIG))
        [record] = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert SHADOW_MANAGER in record.getMessage()
        assert "not-json{" in record.getMessage()

    def test_non_list_named_shadows_replaced_siblings_kept(
            self, deployments, caplog):
        """A parseable document whose namedShadows is not a list has JUST
        that node replaced with the portal names (+ a warning); the
        caller's direction/classic values and unknown keys survive.
        # Validates: Requirements 2.2, 3.2, 3.3"""
        with caplog.at_level(logging.WARNING):
            result, entry = run_helper(deployments, {
                "componentVersion": "2.3.15",
                "configurationUpdate": {"merge": json.dumps({
                    "synchronize": {
                        "direction": "deviceToCloud",
                        "coreThing": {
                            "classic": False,
                            "namedShadows": "dda-camera-registry",
                            "maxOutboundSyncUpdatesPerSecond": 60,
                        },
                    },
                    "strategy": {"type": "realTime"},
                })},
            })

        assert result == "merged"
        doc = json.loads(entry["configurationUpdate"]["merge"])
        core = doc["synchronize"]["coreThing"]
        # The corrupt node is replaced with exactly the portal names...
        assert core["namedShadows"] == PORTAL_SHADOW_NAMES
        # ...while every sibling caller value survives byte-for-byte.
        assert doc["synchronize"]["direction"] == "deviceToCloud"
        assert core["classic"] is False
        assert core["maxOutboundSyncUpdatesPerSecond"] == 60
        assert doc["strategy"] == {"type": "realTime"}
        assert any("namedShadows" in r.getMessage() for r in caplog.records
                   if r.levelno == logging.WARNING)


# ==========================================================================
# Endpoint level — 'added' vs 'merged' reporting (design Decision 4)
# ==========================================================================

class TestEndpointReporting:
    """**Validates: Requirements 2.1, 3.5** (create_deployment endpoint)."""

    def test_added_keeps_exact_auto_included_entry_shape(self, sm_env):
        """A fresh deployment (no caller ShadowManager) reports the
        auto_included entry in its exact pre-fix shape — component_name,
        component_version, reason string verbatim (the task-2 pin).
        # Validates: Requirements 2.1 ('added' reporting preserved)"""
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT], target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [reported] = [e for e in payload["auto_included"]
                      if e["component_name"] == SHADOW_MANAGER]
        assert reported == {
            "component_name": SHADOW_MANAGER,
            "component_version": sm_env.deployments.SHADOW_MANAGER_VERSION,
            "reason": FRESH_ADD_REASON,
        }

    def test_merged_adds_no_auto_included_entry(self, sm_env):
        """A caller-supplied (bare) ShadowManager entry is COMPLETED, not
        added: the submitted entry carries the full portal merge but the
        auto_included report stays silent about it (merge-into-existing is
        logged, not reported — design Decision 4, the Nucleus precedent).
        # Validates: Requirements 2.1, 3.5"""
        status, payload = sm_env.deploy_components(
            [LOCAL_SERVER_COMPONENT,
             {"component_name": SHADOW_MANAGER,
              "component_version": "2.3.15"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = sm_env.gg.create_deployment_calls
        entry = call["components"][SHADOW_MANAGER]
        # The merge really happened (this run was 'merged', not a skip)...
        merged = json.loads(entry["configurationUpdate"]["merge"])
        assert (merged["synchronize"]["coreThing"]["namedShadows"]
                == PORTAL_SHADOW_NAMES)
        assert entry["componentVersion"] == "2.3.15"
        # ...and no auto_included ShadowManager entry was reported.
        assert [e for e in payload["auto_included"]
                if e["component_name"] == SHADOW_MANAGER] == []


# ==========================================================================
# Workflow endpoint — presence gate (3.6 scope guard)
# ==========================================================================

class TestWorkflowPresenceGate:
    """**Validates: Requirements 3.5, 3.6** (create_workflow_deployment)."""

    def test_fresh_workflow_deployment_adds_no_shadow_manager(self, wf_env):
        """A FRESH workflow deployment (no previous latest-for-target
        revision) submits NO ShadowManager entry: the ensure step is
        presence-gated and must not start auto-including it on the
        workflow path (out of the requirements' scope).
        # Validates: Requirements 3.6"""
        # topics=None: no subscribe merge, so the submitted map isolates
        # the workflow entry.
        workflow_id = wf_env.seed_subscribing_workflow(topics=None)
        wf_env.register_device()
        # No seed_deployment: fresh target, merged map = workflow entry only.

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is False
        components = wf_env.submitted_components()
        assert SHADOW_MANAGER not in components
        assert components == {
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"}}
