"""Bug condition exploration test — workflow-deploy-subscribe-merge task 1.

Bugfix spec: .kiro/specs/workflow-deploy-subscribe-merge/

Reproduces C(X) at the ``create_workflow_deployment`` level: the portal's
workflow deployment path builds its own components map (the target's
existing component set merged with the workflow entry) and submits the
Greengrass deployment directly WITHOUT calling
``apply_subscribe_access_control`` — so portal workflow deployments of a
subscribing workflow ship no ``dda:workflow-subscribe:<workflowId>``
grant, and the on-device SubscribeToIoTCore call is denied.

Live incident (2026-08-05, ryan-orin-nano): workflow ``bedrock_test`` v5
(mqtt_subscribe on ``dda/bedrock-test-trigger``) portal-deployed as
deployment 2014a473 — the submitted LocalServer arm64JP6 entry carried no
configurationUpdate; device logs show "Greengrass IPC denied
SubscribeToIoTCore for topic 'dda/bedrock-test-trigger'".

These tests assert the FIXED behavior and are EXPECTED TO FAIL on
unfixed code — the failures prove the bug exists. After task 3.1 lands
the ``apply_subscribe_access_control(components_map)`` call (plus the
additive ``warnings`` response field) in ``create_workflow_deployment``,
this same file validates the fix (task 3.2).

Property 1: Bug Condition (Fix Check) — Subscribe policy rides every
workflow deployment with LocalServer in the merged set.
**Validates: Requirements 2.1, 2.3** (defect: 1.1)

Property 2: Bug Condition (Fix Check) — Warning surfaced when LocalServer
is absent from the merged set.
**Validates: Requirements 2.2** (defect: 1.2)

Harness: DeployEnv convention from test_subscribe_deployment_warning.py —
the stateful FakeGreengrass/FakeIot from
test_workflow_packaging_deployment_integration.py wired in as the
Use_Case-account clients via monkeypatched ``get_usecase_client``, with
real workflow metadata + version items seeded in the moto-backed
Workflows / WorkflowVersions tables from conftest.py.
"""
import json
import sys
import uuid

import pytest

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, FakeIot)

SUBSCRIBE_OPERATION = "aws.greengrass#SubscribeToIoTCore"
MQTTPROXY = "aws.greengrass.ipc.mqttproxy"
LOCAL_SERVER_ARM64JP6 = "aws.edgeml.dda.LocalServer.arm64JP6"

# The live incident's topic plus a wildcard filter, so the resources
# assertion checks exact recorded-filter fidelity, not a single string.
TOPICS = ["dda/bedrock-test-trigger", "factory/line1/+/state"]

THING = "ryan-orin-nano"


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments (and its workflow_guards binding) inside the
    moto mock so module-level boto3 clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


class WorkflowDeployEnv:
    """create_workflow_deployment harness: FakeGreengrass/FakeIot as the
    Use_Case-account clients, real workflow metadata + version items in
    the moto-backed tables, deploys through the handler with the portal's
    dispatch shape (component_type: workflow / workflow_id)."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Workflow Deploy Subscribe Merge Test",
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

    def seed_subscribing_workflow(self, version=1, topics=TOPICS):
        """A workflow whose deployed version records subscribed_topics and
        passes every pre-submit gate: workflows-table metadata item
        (get_workflow_metadata), version item with a passed validation run
        and a component_arn (deployment guard + packaged check). No
        camera/plugin/LLM attributes, so those gates no-op."""
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        self.env.stack.tables.workflows.put_item(Item={
            "workflow_id": workflow_id,
            "usecase_id": self.usecase_id,
            "name": "bedrock_test",
            "latest_version": version,
            "created_at": 1,
        })
        item = {
            "workflow_id": workflow_id,
            "version": version,
            "validation_status": {"status": "passed"},
            "component_arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                              f"components:dda.workflow.{workflow_id}"
                              f":versions:{version}.0.0"),
        }
        if topics is not None:
            item["subscribed_topics"] = list(topics)
        self.env.stack.tables.versions.put_item(Item=item)
        return workflow_id

    def register_device(self, thing_name=THING):
        """A core device passing the min-LocalServer compatibility gate."""
        self.gg.register_device(thing_name, local_server_version="99.0.0",
                                arch="arm64JP6")

    def thing_arn(self, thing_name=THING):
        return f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:thing/{thing_name}"

    def deploy(self, workflow_id, **body):
        """POST /deployments with the portal's workflow dispatch shape."""
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": workflow_id,
                "target_devices": [THING], **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def submitted_components(self):
        [call] = self.gg.create_deployment_calls
        return call["components"]


@pytest.fixture
def wf_env(env, deployments, monkeypatch):
    return WorkflowDeployEnv(env, deployments, monkeypatch)


# ==========================================================================
# Case A — revision, the live incident shape (Property 1)
# ==========================================================================

class TestCaseARevisionCarriesSubscribePolicy:
    """**Validates: Requirements 2.1, 2.3** (defect 1.1).

    The target already has an effective deployment carrying LocalServer
    arm64JP6 (exactly the merged-set shape of deployment 2014a473). The
    fixed path must attach the dda:workflow-subscribe:<workflowId> policy
    to the SUBMITTED LocalServer entry — resolved from the authoritative
    workflow version being deployed — with no warnings in the response."""

    def test_submitted_localserver_entry_carries_subscribe_policy(
            self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow()
        wf_env.register_device()
        wf_env.gg.seed_deployment(wf_env.thing_arn(), {
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
        })

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True

        components = wf_env.submitted_components()
        # Revision semantics preserved: LocalServer carried over, workflow
        # entry placed at the authoritative version (major -> version item).
        assert LOCAL_SERVER_ARM64JP6 in components
        assert components[f"dda.workflow.{workflow_id}"][
            "componentVersion"] == "1.0.0"

        local_server = components[LOCAL_SERVER_ARM64JP6]
        # BUG (1.1): on unfixed code the submitted LocalServer entry has NO
        # configurationUpdate at all — the on-device subscribe is denied.
        assert "configurationUpdate" in local_server, (
            "submitted LocalServer entry carries no configurationUpdate: "
            "the dda:workflow-subscribe grant was never merged "
            f"(live incident shape); entry={local_server!r}")
        merge_doc = json.loads(local_server["configurationUpdate"]["merge"])
        policy = merge_doc["accessControl"][MQTTPROXY][
            f"dda:workflow-subscribe:{workflow_id}"]
        assert policy["operations"] == [SUBSCRIBE_OPERATION]
        assert policy["resources"] == TOPICS

        # Merge attached cleanly -> no warnings field (additive pattern).
        assert "warnings" not in payload


# ==========================================================================
# Case B — fresh deployment, no LocalServer in the merged set (Property 2)
# ==========================================================================

class TestCaseBFreshDeploymentSurfacesWarning:
    """**Validates: Requirements 2.2** (defect 1.2).

    No existing deployment for the target: the merged map is just the
    workflow entry, with no LocalServer to attach the grant to. The fixed
    path must surface the actionable warning(s) additively in the 201
    response."""

    def test_201_response_carries_actionable_warnings(self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow()
        wf_env.register_device()
        # No seed_deployment: fresh target, merged map = workflow entry only.

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is False

        # The merged set really has no LocalServer (warning is legitimate).
        components = wf_env.submitted_components()
        assert not any(name.startswith("aws.edgeml.dda.LocalServer")
                       for name in components)
        # The merge had nowhere to attach: the components map is untouched.
        assert components == {
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"}}

        # BUG (1.2): on unfixed code the 201 body has no `warnings` key —
        # the submission is silent about the guaranteed on-device denial.
        assert "warnings" in payload, (
            "201 response has no `warnings` field: the subscribe "
            "authorization had nowhere to attach and the caller was never "
            f"told; body keys={sorted(payload)}")
        [warning] = payload["warnings"]
        assert workflow_id in warning
        for topic in TOPICS:
            assert topic in warning
        assert "LocalServer" in warning
        assert SUBSCRIBE_OPERATION in warning
        assert "denied" in warning


# ==========================================================================
# Case C — existing configurationUpdate.merge is preserved (Property 1)
# ==========================================================================

class TestCaseCExistingMergePreservedAndPolicyUpserted:
    """**Validates: Requirements 2.1** (defect 1.1, existing-merge edge).

    The seeded LocalServer entry already carries a configurationUpdate.merge
    document (the live incident's manual-recovery grant plus a store-style
    config key). The fixed path must upsert the workflow's policy key while
    preserving every pre-existing merge key."""

    PREEXISTING_MERGE = {
        "accessControl": {
            MQTTPROXY: {
                "manual:recovery-grant": {
                    "policyDescription": "hand-added incident recovery",
                    "operations": [SUBSCRIBE_OPERATION],
                    "resources": ["dda/manual-recovery"],
                },
            },
        },
        "store": {"path": "/data/store", "retentionDays": 7},
    }

    def test_preexisting_merge_keys_survive_and_policy_is_upserted(
            self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow()
        wf_env.register_device()
        wf_env.gg.seed_deployment(wf_env.thing_arn(), {
            LOCAL_SERVER_ARM64JP6: {
                "componentVersion": "1.0.51",
                "configurationUpdate": {
                    "merge": json.dumps(self.PREEXISTING_MERGE)},
            },
        })

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        components = wf_env.submitted_components()
        local_server = components[LOCAL_SERVER_ARM64JP6]
        merge_doc = json.loads(local_server["configurationUpdate"]["merge"])

        # Every pre-existing merge key survives (non-destructive upsert).
        assert merge_doc["store"] == self.PREEXISTING_MERGE["store"]
        assert merge_doc["accessControl"][MQTTPROXY][
            "manual:recovery-grant"] == self.PREEXISTING_MERGE[
                "accessControl"][MQTTPROXY]["manual:recovery-grant"]

        # BUG (1.1): on unfixed code the carried-over merge document is
        # submitted verbatim — the workflow's policy key is never upserted.
        policies = merge_doc["accessControl"][MQTTPROXY]
        assert f"dda:workflow-subscribe:{workflow_id}" in policies, (
            "workflow subscribe policy was not upserted into the existing "
            f"merge document; policy keys={sorted(policies)}")
        policy = policies[f"dda:workflow-subscribe:{workflow_id}"]
        assert policy["operations"] == [SUBSCRIBE_OPERATION]
        assert policy["resources"] == TOPICS
        assert "warnings" not in payload
