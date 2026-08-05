"""Deployment-time subscribe accessControl warning — example tests.

Feature: trigger-activation-runtime, task 11.4.

Example anchors around ``deployments.apply_subscribe_access_control``
(the Property 16 property test in test_property_subscribed_topics.py
covers the generated space):

- A component set carrying a subscribing workflow (version item with
  recorded ``subscribed_topics``) but NO LocalServer component yields an
  actionable warning naming the workflow, every topic, the LocalServer
  component, and the subscribe authorization / on-device denial
  consequence, with the component set untouched (Requirement 10.3).
- With LocalServer present, no warnings and the
  ``dda:workflow-subscribe:<workflowId>`` policy is attached to the
  LocalServer entry's configurationUpdate merge (Requirement 10.2
  example-level anchor).
- At the ``create_deployment`` endpoint level (harness mirroring
  test_deployment_shadow_manager.py), the 201 response carries the
  additive ``warnings`` field exactly when the merge produced warnings,
  and omits it byte-identically otherwise (Requirements 10.3, 10.4).

The recorded topics resolve through ``workflow_guards.get_version_item``:
the function-level tests swap the module attribute for a fake (the
convention from test_property_subscribed_topics.py); the endpoint-level
tests seed real version items in the moto-backed WorkflowVersions table.

_Requirements: 10.3, 10.4_
"""
import copy
import json
import sys
import uuid

import pytest

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, FakeGreengrass, FakeIot)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so its module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"
MQTTPROXY = "aws.greengrass.ipc.mqttproxy"

WORKFLOW_ID = "wf-a1b2c3"
TOPICS = ["dda/trigger/start", "factory/line1/+/state"]


def subscribing_version_item(workflow_id=WORKFLOW_ID, version=2,
                             topics=TOPICS):
    return {"workflow_id": workflow_id, "version": version,
            "validation_status": {"status": "passed"},
            "subscribed_topics": list(topics)}


def call_with_version_items(deployments, components_map, version_items):
    """apply_subscribe_access_control with workflow_guards.get_version_item
    swapped for a fake resolving from the given items (deployments binds
    the module, not the function, so the attribute swap intercepts)."""
    def fake_get_version_item(workflow_id, version):
        return version_items.get((workflow_id, version))

    original = deployments.workflow_guards.get_version_item
    deployments.workflow_guards.get_version_item = fake_get_version_item
    try:
        return deployments.apply_subscribe_access_control(components_map)
    finally:
        deployments.workflow_guards.get_version_item = original


# ==========================================================================
# (a) Subscribing workflow, no LocalServer -> actionable warning (10.3)
# ==========================================================================

class TestWarningWithoutLocalServer:
    def test_warning_names_workflow_topics_localserver_and_denial(
            self, deployments):
        """The warning names the workflow id, every recorded topic, the
        LocalServer component, and the subscribe authorization / denial
        consequence (Requirement 10.3)."""
        components_map = {
            f"dda.workflow.{WORKFLOW_ID}": {"componentVersion": "2.0.0"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
        }
        version_items = {(WORKFLOW_ID, 2): subscribing_version_item()}

        warnings = call_with_version_items(
            deployments, components_map, version_items)

        [warning] = warnings
        assert WORKFLOW_ID in warning
        for topic in TOPICS:
            assert topic in warning
        assert "LocalServer" in warning
        # The authorization that could not attach, and the consequence.
        assert SUBSCRIBE_OPERATION in warning
        assert "denied" in warning

    def test_component_set_is_untouched(self, deployments):
        """With nowhere to attach, the merge mutates nothing
        (Requirement 10.3: warning only, no partial merge)."""
        components_map = {
            f"dda.workflow.{WORKFLOW_ID}": {"componentVersion": "2.0.0"},
        }
        before = copy.deepcopy(components_map)
        version_items = {(WORKFLOW_ID, 2): subscribing_version_item()}

        warnings = call_with_version_items(
            deployments, components_map, version_items)

        assert len(warnings) == 1
        assert components_map == before


# ==========================================================================
# (b) LocalServer present -> no warnings, policy attached (10.2 anchor)
# ==========================================================================

class TestPolicyAttachedWithLocalServer:
    def test_no_warnings_and_policy_lands_on_localserver_entry(
            self, deployments):
        """Example-level anchor of the merge: the workflow-unique
        SubscribeToIoTCore policy lands in the LocalServer entry's
        configurationUpdate.merge and no warning is produced."""
        local_server_name = "aws.edgeml.dda.LocalServer.arm64JP6"
        components_map = {
            f"dda.workflow.{WORKFLOW_ID}": {"componentVersion": "2.0.0"},
            local_server_name: {"componentVersion": "1.0.99"},
        }
        version_items = {(WORKFLOW_ID, 2): subscribing_version_item()}

        warnings = call_with_version_items(
            deployments, components_map, version_items)

        assert warnings == []
        merge_doc = json.loads(
            components_map[local_server_name]["configurationUpdate"]["merge"])
        policy = merge_doc["accessControl"][MQTTPROXY][
            f"dda:workflow-subscribe:{WORKFLOW_ID}"]
        assert policy["operations"] == [SUBSCRIBE_OPERATION]
        assert policy["resources"] == TOPICS


# ==========================================================================
# (c) create_deployment endpoint: additive `warnings` field (10.3, 10.4)
# ==========================================================================

class DeployEnv:
    """Minimal create_deployment harness (test_deployment_shadow_manager.py
    pattern): a Use_Case with the stateful Greengrass/IoT fakes wired in as
    the Use_Case-account clients, and real version items seeded in the
    moto-backed WorkflowVersions table so workflow_guards.get_version_item
    resolves them for real."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Subscribe Warning Test",
            "account_id": ACCOUNT_ID,
        })

        self.gg = FakeGreengrass()
        self.iot = FakeIot()

        def deployment_client(service_name, usecase, session_name=None,
                              region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot":
                return self.iot
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(deployments, "get_usecase_client",
                            deployment_client)

    def seed_version_item(self, workflow_id, version=1, topics=None):
        item = {"workflow_id": workflow_id, "version": version,
                "validation_status": {"status": "passed"}}
        if topics is not None:
            item["subscribed_topics"] = list(topics)
        self.env.stack.tables.versions.put_item(Item=item)

    def deploy_components(self, components, **body):
        body = {"usecase_id": self.usecase_id, "components": components,
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def deploy_env(env, deployments, monkeypatch):
    return DeployEnv(env, deployments, monkeypatch)


class TestCreateDeploymentWarningsField:
    def test_subscribing_workflow_without_localserver_carries_warnings(
            self, deploy_env):
        """A 201 deployment of a subscribing workflow without LocalServer
        carries the additive `warnings` field with the actionable warning
        (Requirement 10.3)."""
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        deploy_env.seed_version_item(workflow_id, version=1, topics=TOPICS)

        status, payload = deploy_env.deploy_components(
            [{"component_name": f"dda.workflow.{workflow_id}",
              "component_version": "1.0.0"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [warning] = payload["warnings"]
        assert workflow_id in warning
        for topic in TOPICS:
            assert topic in warning
        assert "LocalServer" in warning
        assert SUBSCRIBE_OPERATION in warning

        # The submitted component set carries no accessControl merge.
        [call] = deploy_env.gg.create_deployment_calls
        assert call["components"][f"dda.workflow.{workflow_id}"] == {
            "componentVersion": "1.0.0"}

    def test_non_subscribing_deployment_has_no_warnings_field(
            self, deploy_env):
        """A workflow with no recorded subscribed_topics produces a 201
        response without the `warnings` key at all — the field is additive
        and pre-feature responses stay byte-identical (Requirement 10.4)."""
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        deploy_env.seed_version_item(workflow_id, version=1, topics=None)

        status, payload = deploy_env.deploy_components(
            [{"component_name": f"dda.workflow.{workflow_id}",
              "component_version": "1.0.0"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        assert "warnings" not in payload

    def test_subscribing_workflow_with_localserver_merges_without_warnings(
            self, deploy_env):
        """With LocalServer in the set, the 201 response has no `warnings`
        key and the submitted LocalServer entry carries the merged
        SubscribeToIoTCore policy (Requirement 10.2 endpoint anchor)."""
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        deploy_env.seed_version_item(workflow_id, version=1, topics=TOPICS)

        status, payload = deploy_env.deploy_components(
            [{"component_name": f"dda.workflow.{workflow_id}",
              "component_version": "1.0.0"},
             {"component_name": "aws.edgeml.dda.LocalServer.x86_64",
              "component_version": "1.2.0"}],
            target_devices=["line-a-camera-01"])

        assert status == 201, payload
        assert "warnings" not in payload

        [call] = deploy_env.gg.create_deployment_calls
        local_server = call["components"]["aws.edgeml.dda.LocalServer.x86_64"]
        merge_doc = json.loads(local_server["configurationUpdate"]["merge"])
        policy = merge_doc["accessControl"][MQTTPROXY][
            f"dda:workflow-subscribe:{workflow_id}"]
        assert policy["operations"] == [SUBSCRIBE_OPERATION]
        assert policy["resources"] == TOPICS
