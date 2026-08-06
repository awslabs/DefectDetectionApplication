"""Bug condition exploration test — workflow-deploy-component-version task 1.

Bugfix spec: .kiro/specs/workflow-deploy-component-version/

Reproduces C(X) in the exact incident shape: a workflow version whose
REGISTERED Greengrass component version's major exceeds the workflow
version (re-packaged; ``next_component_version`` bumps the major because
Greengrass component versions are immutable). The portal deploy path
pins the entry at ``{workflow_version}.0.0`` — the OLD version — so
Greengrass delivers nothing and reports COMPLETED, and the three
major-parse consumers (subscribe grant merge, camera-binding prune keys,
vLLM gate) misresolve bumped-major entries.

Live incident (2026-08-06, ryan-orin-nano): workflow ``modbus_test``
(e830f55d-…) v1 re-packaged to component ``2.0.0`` after ``1.0.0``
shipped a broken recipe; portal deploy 72c2f784 (revision 14) pinned
``dda.workflow.e830f55d…`` at ``1.0.0``; the device reported COMPLETED,
artifacts still missing, workflow unregistered.

These tests assert the FIXED behavior and are EXPECTED TO FAIL on
unfixed code — the failures prove the bug exists. After task 3 lands
the three-leg fix (forward resolution on the deploy path, scan-first
reverse resolution in the consumers, discrete ``component_version``
field at packaging time), this same file validates the fix (task 3.4).

Property 1: Bug Condition (Fix Check) — Deploy path pins and reports the
registered component version.
**Validates: Requirements 2.1, 2.2** (defects: 1.1, 1.2)

Property 2: Bug Condition (Fix Check) — Subscribe-topic resolution
survives bumped majors.
**Validates: Requirements 2.3** (defect: 1.3)

Property 3: Bug Condition (Fix Check) — Binding keys and the vLLM gate
resolve the true workflow version.
**Validates: Requirements 2.4** (defect: 1.4)

Property 4: Bug Condition (Fix Check) — Packaging records the component
version discretely.
**Validates: Requirements 2.5** (defect: 1.5)

Harnesses: WorkflowDeployEnv (endpoint-level create_workflow_deployment;
FakeGreengrass/FakeIot as the Use_Case-account clients, real workflow
metadata + version items in the moto-backed tables) from
test_workflow_deploy_subscribe_merge_exploration.py, and FleetEnv (real
packaging pipeline against moto S3 + a mocked component registry) from
test_workflow_packaging_deployment_integration.py.
"""
import json
import sys
import uuid

import pytest

from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, MQTTPROXY, SUBSCRIBE_OPERATION,
    WorkflowDeployEnv)
from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FleetEnv)

# The live incident's trigger topic plus a wildcard filter, so the
# resources assertion checks recorded-filter fidelity.
TOPICS = ["dda/modbus-test-trigger", "factory/line1/+/state"]

# The incident shape: workflow v1 re-packaged to component 2.0.0.
WORKFLOW_VERSION = 1
REGISTERED_COMPONENT_VERSION = "2.0.0"
STALE_PIN = "1.0.0"  # what unfixed code derives: {workflow_version}.0.0


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


class RepackagedWorkflowEnv(WorkflowDeployEnv):
    """WorkflowDeployEnv extended with the re-packaged seeding shape:
    the version item's component_arn ends ``:versions:2.0.0`` while the
    workflow version (and latest_version) stays 1 — exactly what
    workflow_packaging leaves behind after a re-package of v1."""

    def seed_repackaged_workflow(
            self, workflow_version=WORKFLOW_VERSION,
            component_version=REGISTERED_COMPONENT_VERSION,
            topics=None, has_llm_inference=False,
            packaged_architectures=None, name="modbus_test"):
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        self.env.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "name": name,
            "latest_version": workflow_version,
            "created_at": 1,
        })
        item = {
            "workflow_id": workflow_id,
            "version": workflow_version,
            "validation_status": {"status": "passed"},
            # The authoritative record: packaging's success bookkeeping
            # wrote the REGISTERED component version into the arn suffix.
            "component_arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                              f"components:dda.workflow.{workflow_id}"
                              f":versions:{component_version}"),
        }
        if topics is not None:
            item["subscribed_topics"] = list(topics)
        if has_llm_inference:
            item["has_llm_inference"] = True
            item["packaged_architectures"] = list(
                packaged_architectures or [])
        self.env.stack.tables.versions.put_item(Item=item)
        return workflow_id

    def association_record(self, deployment_id):
        return self.env.stack.tables.deployments.get_item(
            Key={"deployment_id": deployment_id}).get("Item")

    def audit_entry(self, workflow_id, action="deploy_workflow"):
        entries = [item
                   for item in self.env.stack.tables.audit_log.scan()
                   .get("Items", [])
                   if item.get("action") == action
                   and item.get("resource_id") == workflow_id]
        assert entries, f"no '{action}' audit entry for {workflow_id}"
        return entries[-1]


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return RepackagedWorkflowEnv(env, deployments, monkeypatch)


# ==========================================================================
# Case 1 — Re-packaged pin, the incident shape (Property 1)
# ==========================================================================

class TestCase1RepackagedPinIncidentShape:
    """**Validates: Requirements 2.1, 2.2** (defects 1.1, 1.2).

    Deploy workflow v1 whose version item records component 2.0.0 (the
    modbus_test incident shape). The fixed path must submit the entry at
    the REGISTERED version and report it consistently in the association
    record, the audit entry, and the 201 response."""

    def test_submitted_entry_record_audit_and_response_carry_registered_version(
            self, wf_env):
        workflow_id = wf_env.seed_repackaged_workflow()
        wf_env.register_device()

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload

        components = wf_env.submitted_components()
        entry = components[f"dda.workflow.{workflow_id}"]
        record = wf_env.association_record(payload["deployment_id"])
        audit = wf_env.audit_entry(workflow_id)

        observed = {
            "submitted_entry": entry.get("componentVersion"),
            "association_record": record.get("component_version"),
            "audit_details": (audit.get("details") or {}).get(
                "component_version"),
            "response_body": payload.get("component_version"),
        }
        # BUG (1.1/1.2): on unfixed code every one of these says 1.0.0 —
        # the stale {workflow_version}.0.0 pin — while the version item's
        # arn says 2.0.0. Greengrass sees no component change, delivers
        # nothing, and reports COMPLETED (live deploy 72c2f784).
        expected = {key: REGISTERED_COMPONENT_VERSION for key in observed}
        assert observed == expected, (
            "workflow deploy path did not follow the REGISTERED component "
            f"version {REGISTERED_COMPONENT_VERSION} recorded on the "
            f"version item's component_arn; observed={observed!r}")


# ==========================================================================
# Case 2 — Subscribe resolution on a bumped major (Property 2)
# ==========================================================================

class TestCase2SubscribeResolutionOnBumpedMajor:
    """**Validates: Requirements 2.3** (defect 1.3).

    The bedrock_test shape: a components map carries a CARRIED-OVER
    workflow entry at 2.0.0 (registered by re-packaging v1) plus a
    LocalServer entry. The fixed ``apply_subscribe_access_control`` must
    resolve the entry to the v1 version item and merge its recorded
    subscribed_topics into the LocalServer grant."""

    def test_localserver_merge_carries_v1_topics_for_bumped_major_entry(
            self, wf_env):
        workflow_id = wf_env.seed_repackaged_workflow(topics=TOPICS)
        components_map = {
            f"dda.workflow.{workflow_id}": {
                "componentVersion": REGISTERED_COMPONENT_VERSION},
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
        }

        warnings = wf_env.deployments.apply_subscribe_access_control(
            components_map)

        local_server = components_map[LOCAL_SERVER_ARM64JP6]
        # BUG (1.3): on unfixed code get_version_item(workflow_id, 2)
        # resolves nothing (latest_version is 1), no topics are collected,
        # and the components map is left untouched — the on-device
        # SubscribeToIoTCore call is denied.
        assert "configurationUpdate" in local_server, (
            "subscribe grant was not merged for the bumped-major workflow "
            "entry: the consumer parsed major 2 and resolved no version "
            f"item; entry={local_server!r}, warnings={warnings!r}")
        merge_doc = json.loads(local_server["configurationUpdate"]["merge"])
        policy = merge_doc["accessControl"][MQTTPROXY][
            f"dda:workflow-subscribe:{workflow_id}"]
        assert policy["operations"] == [SUBSCRIBE_OPERATION]
        assert policy["resources"] == TOPICS
        assert warnings == []


# ==========================================================================
# Case 3 — Binding-key survival (Property 3)
# ==========================================================================

class TestCase3BindingKeySurvival:
    """**Validates: Requirements 2.4** (defect 1.4).

    A components map with a 2.0.0 entry for workflow v1: the survive-set
    ``_deployed_workflow_binding_keys`` derives must contain the live key
    ``{workflowId}/1`` — otherwise ``deliver_camera_bindings`` prunes the
    valid binding from the device shadow."""

    def test_binding_keys_derive_the_true_workflow_version(self, wf_env):
        workflow_id = wf_env.seed_repackaged_workflow()
        components_map = {
            f"dda.workflow.{workflow_id}": {
                "componentVersion": REGISTERED_COMPONENT_VERSION},
        }

        keys = wf_env.deployments._deployed_workflow_binding_keys(
            components_map)

        # BUG (1.4): on unfixed code the major parse yields
        # {workflowId}/2 — a version that does not exist — so the live
        # key {workflowId}/1 is absent from the survive-set and gets
        # pruned from the dda-camera-bindings shadow.
        assert keys == {f"{workflow_id}/{WORKFLOW_VERSION}"}, (
            "binding survive-set misderived from the bumped-major entry: "
            f"expected {{{workflow_id}/{WORKFLOW_VERSION}}}, got {keys!r}")


# ==========================================================================
# Case 4 — vLLM gate on a bumped major (Property 3)
# ==========================================================================

class TestCase4VllmGateOnBumpedMajor:
    """**Validates: Requirements 2.4** (defect 1.4).

    The generic path's ``collect_vllm_component_manifests`` must resolve
    a 2.0.0 workflow entry to the v1 version item (which records
    ``has_llm_inference`` + ``packaged_architectures``) so LLM-bearing
    workflows keep activating the architecture gate."""

    ARCHS = ["arm64_jp5", "arm64_jp6"]

    def test_manifest_produced_for_llm_bearing_bumped_major_entry(
            self, wf_env):
        workflow_id = wf_env.seed_repackaged_workflow(
            has_llm_inference=True, packaged_architectures=self.ARCHS)
        component_name = f"dda.workflow.{workflow_id}"

        manifests = wf_env.deployments.collect_vllm_component_manifests(
            {component_name: REGISTERED_COMPONENT_VERSION})

        # BUG (1.4): on unfixed code get_version_item(workflow_id, 2)
        # resolves nothing, so no manifest is produced and the LLM-bearing
        # workflow sails past the architecture gate onto e.g. a jp4 device.
        assert component_name in manifests, (
            "vLLM gate manifest missing for the bumped-major LLM-bearing "
            f"workflow entry — the gate is skipped; manifests={manifests!r}")
        assert manifests[component_name]["architectures"] == self.ARCHS


# ==========================================================================
# Case 5 — Packaging records component_version discretely (Property 4)
# ==========================================================================

class TestCase5PackagingRecordsComponentVersion:
    """**Validates: Requirements 2.5** (defect 1.5).

    Package through the real pipeline (FleetEnv): the version item's
    success bookkeeping must record ``component_version`` as a discrete
    field agreeing with the response and the ``component_arn`` suffix."""

    def test_version_item_records_discrete_component_version(
            self, env, packaging, deployments, monkeypatch):
        fleet = FleetEnv(env, packaging, deployments, monkeypatch)

        # The stock registry fake mints arns with a uuid ":versions:"
        # suffix; real Greengrass arns end ":versions:{version}". Return
        # real-shaped arns so the arn-suffix agreement assertion is
        # meaningful (this is a harness fidelity tweak, not a fix).
        def _create_real_shaped(**kwargs):
            recipe = json.loads(kwargs["inlineRecipe"])
            return {"arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                            f"components:{recipe['ComponentName']}"
                            f":versions:{recipe['ComponentVersion']}")}

        fleet.registry.create_component_version.side_effect = \
            _create_real_shaped
        fleet.seed_plugins(["x86_64"])

        status, payload = fleet.package(["x86_64"])

        assert status == 201, payload
        assert payload["component_version"] == "1.0.0"

        item = fleet.env.stack.tables.versions.get_item(
            Key={"workflow_id": fleet.workflow_id, "version": 1})["Item"]
        # BUG (1.5): on unfixed code the success bookkeeping records only
        # component_arn — no discrete component_version field — so no
        # consumer has a direct componentVersion -> workflow_version map.
        assert "component_version" in item, (
            "version item records no discrete component_version field "
            f"after packaging; recorded attributes={sorted(item)}")
        assert str(item["component_version"]) == payload["component_version"]
        # The two records agree: the arn suffix equals the field.
        assert item["component_arn"].rsplit(":versions:", 1)[-1] == \
            str(item["component_version"])
