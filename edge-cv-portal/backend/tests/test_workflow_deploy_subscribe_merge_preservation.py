"""Preservation tests — workflow-deploy-subscribe-merge task 2.

Bugfix spec: .kiro/specs/workflow-deploy-subscribe-merge/

Property 3: Preservation — Non-subscribing workflow deployments are
byte-identical.
**Validates: Requirements 3.1, 3.3**

Observation-first: these assertions encode the observed UNFIXED behavior
of ``create_workflow_deployment`` for workflow versions whose item records
NO ``subscribed_topics`` — the baseline the fix must preserve:

- Fresh deploy (no existing deployment): the submitted components map is
  exactly ``{dda.workflow.<id>: {componentVersion: "{v}.0.0"}}`` and the
  201 body has no ``warnings`` key (3.1).
- Revision: the submitted map deep-equals the carried-over components
  (LocalServer and others, any pre-existing configurationUpdate carried
  verbatim) plus the workflow entry at the new version — no
  configurationUpdate added anywhere — with the existing deployment name
  reused and the association record written with the usual shape (3.1, 3.3).

These tests are EXPECTED TO PASS on unfixed code (they capture the
baseline) and MUST STILL PASS after task 3.1 lands the
``apply_subscribe_access_control`` call: a non-subscribing version yields
no merge and no warnings, so the submission stays byte-identical.

Harness: same DeployEnv convention as task 1 — ``WorkflowDeployEnv`` is
imported from test_workflow_deploy_subscribe_merge_exploration.py
(FakeGreengrass/FakeIot wired in via monkeypatched ``get_usecase_client``,
real metadata + version items in the moto-backed tables).
"""
import copy
import json
import sys

import pytest

from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, THING, WorkflowDeployEnv)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return WorkflowDeployEnv(env, deployments, monkeypatch)


# Observed 201 body key set on unfixed code (no `warnings` key). The fix
# only ever ADDS `warnings` when the merge produces some; a non-subscribing
# deployment must keep exactly this shape.
EXPECTED_BODY_KEYS = {
    "deployment_id", "iot_job_id", "iot_job_arn", "workflow_id",
    "workflow_version", "component_name", "component_version", "target_arn",
    "target_devices", "target_thing_group", "is_revision",
    "superseded_deployment_id", "camera_bindings_delivered",
    "camera_warnings", "message",
}


# ==========================================================================
# Non-subscribing fresh deploy — byte-identical map, no warnings (3.1)
# ==========================================================================

class TestNonSubscribingFreshDeployIsByteIdentical:
    """**Validates: Requirements 3.1**

    Version item WITHOUT subscribed_topics, no existing deployment: the
    submitted components map is exactly the workflow entry — no
    configurationUpdate, no extra keys — and the 201 body has no
    `warnings` key."""

    def test_submitted_map_is_exactly_the_workflow_entry(self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow(topics=None)
        wf_env.register_device()

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is False

        components = wf_env.submitted_components()
        assert components == {
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"}}

        assert "warnings" not in payload
        assert set(payload) == EXPECTED_BODY_KEYS


# ==========================================================================
# Non-subscribing revision — carried-over components untouched (3.1, 3.3)
# ==========================================================================

class TestNonSubscribingRevisionIsByteIdentical:
    """**Validates: Requirements 3.1, 3.3**

    The target's existing deployment carries LocalServer plus extra
    components (one with its own configurationUpdate). Revising with a
    non-subscribing workflow submits the carried-over components verbatim
    plus the workflow entry at the new version, reuses the deployment
    name, and writes the association record with the usual shape."""

    EXISTING_NAME = "fleet-alpha"
    TELEMETRY_MERGE = {"logging": {"level": "DEBUG"}}

    def seeded_components(self, workflow_id):
        return {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
            "com.example.telemetry": {
                "componentVersion": "3.1.4",
                "configurationUpdate": {
                    "merge": json.dumps(self.TELEMETRY_MERGE)},
            },
            # Older workflow component version, replaced by the revision.
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"},
        }

    def test_submitted_map_deep_equals_carried_components_plus_entry(
            self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
        wf_env.register_device()
        seeded = self.seeded_components(workflow_id)
        superseded_id = wf_env.gg.seed_deployment(
            wf_env.thing_arn(), seeded, name=self.EXISTING_NAME)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True
        assert payload["superseded_deployment_id"] == superseded_id
        assert "warnings" not in payload
        assert set(payload) == EXPECTED_BODY_KEYS

        # Byte-identity (3.1): carried-over components verbatim — the
        # pre-existing configurationUpdate untouched, no configurationUpdate
        # added anywhere — plus the workflow entry replaced at the new
        # version (3.3).
        expected = copy.deepcopy(seeded)
        expected[f"dda.workflow.{workflow_id}"] = {
            "componentVersion": "2.0.0"}
        [call] = wf_env.gg.create_deployment_calls
        assert call["components"] == expected

        # Deployment name reused (3.3).
        assert call["deploymentName"] == self.EXISTING_NAME
        assert call["targetArn"] == wf_env.thing_arn()

    def test_deployment_record_written_with_usual_shape(self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
        wf_env.register_device()
        superseded_id = wf_env.gg.seed_deployment(
            wf_env.thing_arn(), self.seeded_components(workflow_id),
            name=self.EXISTING_NAME)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        record = wf_env.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]

        assert record["usecase_id"] == wf_env.usecase_id
        assert record["component_type"] == "workflow"
        assert record["workflow_id"] == workflow_id
        assert record["workflow_version"] == 2
        assert record["component_name"] == f"dda.workflow.{workflow_id}"
        assert record["component_version"] == "2.0.0"
        assert record["target_arn"] == wf_env.thing_arn()
        assert record["target_devices"] == [THING]
        assert record["target_thing_group"] is None
        assert record["status"] == "IN_PROGRESS"
        assert record["deployment_status"] == "IN_PROGRESS"
        assert record["is_revision"] is True
        assert record["superseded_deployment_id"] == superseded_id
        assert record["created_by"] == wf_env.user["user_id"]
        assert "created_at" in record and "updated_at" in record
        # No camera bindings were delivered: the attribute is absent.
        assert "camera_bindings" not in record
