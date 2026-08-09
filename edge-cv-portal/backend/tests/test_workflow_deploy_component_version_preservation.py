"""Preservation tests — workflow-deploy-component-version task 2.

Bugfix spec: .kiro/specs/workflow-deploy-component-version/

Property 5: Preservation — First-package deploys are byte-identical.
**Validates: Requirements 3.1, 3.3**

Property 6: Preservation — Generic path and consumer fallback unchanged.
**Validates: Requirements 3.2, 3.5, 3.6**

Observation-first: these assertions encode the observed UNFIXED behavior
of ``create_workflow_deployment`` and the three major-parse consumers for
the NON-bug population — workflow versions whose registered component
version equals ``{workflow_version}.0.0`` (first package, never
re-packaged), and component entries matching NO recorded component
version (unrecorded/faked version items, the population
``test_property_subscribed_topics.py`` rides). This is the baseline the
fix must preserve:

- First-package fresh deploy (version item arn ``:versions:1.0.0``,
  ``workflow_version=1``): the submitted components map is exactly
  ``{dda.workflow.<id>: {componentVersion: "1.0.0"}}``; the association
  record, the audit entry, and the 201 body all say
  ``component_version == "1.0.0"`` (3.1). The subscribing-workflow
  ``warnings`` field of the working-tree subscribe-merge fix is part of
  the captured baseline (3.8 via 3.5/3.6 shapes).
- First-package revision deploy (workflow v2, arn ``:versions:2.0.0``):
  the submitted map deep-equals the carried-over components (LocalServer
  and others, any pre-existing configurationUpdate verbatim) plus the
  workflow entry at ``2.0.0``, deployment name reused, record/audit
  shapes unchanged (3.1, 3.3).
- Consumer fallback fidelity: for workflow entries whose componentVersion
  matches NO recorded component version (version items without
  ``component_arn``/``component_version``, or no version item at all),
  ``collect_workflow_subscribed_topics``,
  ``_deployed_workflow_binding_keys``, and
  ``collect_vllm_component_manifests`` produce exactly today's
  major-parse results (3.2, 3.5, 3.6).

These tests are EXPECTED TO PASS on unfixed code (they capture the
baseline) and MUST STILL PASS after the fix lands: for first packages the
``component_arn`` suffix equals ``{workflow_version}.0.0``, so forward
resolution returns the identical string, and for unrecorded items the
scan finds nothing so the consumers ride the major-parse fallback.

Harness: ``WorkflowDeployEnv`` imported from
test_workflow_deploy_subscribe_merge_exploration.py (FakeGreengrass /
FakeIot wired in via monkeypatched ``get_usecase_client``, real metadata
+ version items in the moto-backed tables).
"""
import copy
import json
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Attr

from test_workflow_deploy_subscribe_merge_exploration import (
    LOCAL_SERVER_ARM64JP6, THING, TOPICS, WorkflowDeployEnv)


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


# Observed 201 body key set on unfixed code. `warnings` is additive and
# appears only when the subscribe merge produced warnings (working-tree
# subscribe-merge fix — part of the captured baseline, Requirement 3.8).
EXPECTED_BODY_KEYS = {
    "deployment_id", "iot_job_id", "iot_job_arn", "workflow_id",
    "workflow_version", "component_name", "component_version", "target_arn",
    "target_devices", "target_thing_group", "is_revision",
    "superseded_deployment_id", "camera_bindings_delivered",
    "camera_warnings", "message",
}


def deploy_audit_entries(wf_env, workflow_id):
    """The deploy_workflow audit entries recorded for one workflow."""
    response = wf_env.env.stack.tables.audit_log.scan(
        FilterExpression=(Attr("resource_id").eq(workflow_id)
                          & Attr("action").eq("deploy_workflow")))
    return response["Items"]


# ==========================================================================
# Property 5 — first-package FRESH deploy is byte-identical (3.1)
# ==========================================================================

class TestFirstPackageFreshDeployByteIdentity:
    """**Validates: Requirements 3.1**

    Version item with ``component_arn`` ending ``:versions:1.0.0`` for
    ``workflow_version=1`` — the registered component version EQUALS
    ``{workflow_version}.0.0``, so this is NOT the bug condition. The
    submitted entry, association record, audit entry, and 201 body must
    keep exactly the observed unfixed shapes."""

    def test_submitted_map_record_audit_and_body_all_say_1_0_0(
            self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow()  # v1, arn :versions:1.0.0
        wf_env.register_device()

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload

        # Submitted components map: exactly the workflow entry at 1.0.0.
        components = wf_env.submitted_components()
        assert components == {
            f"dda.workflow.{workflow_id}": {"componentVersion": "1.0.0"}}

        # 201 body — observed unfixed shape. The seeded version records
        # subscribed_topics and the fresh merged set has no LocalServer,
        # so the working-tree subscribe-merge baseline adds `warnings`.
        assert set(payload) == EXPECTED_BODY_KEYS | {"warnings"}
        assert payload["workflow_id"] == workflow_id
        assert payload["workflow_version"] == 1
        assert payload["component_name"] == f"dda.workflow.{workflow_id}"
        assert payload["component_version"] == "1.0.0"
        assert payload["target_arn"] == wf_env.thing_arn()
        assert payload["target_devices"] == [THING]
        assert payload["target_thing_group"] is None
        assert payload["is_revision"] is False
        assert payload["superseded_deployment_id"] is None
        assert payload["camera_bindings_delivered"] is False
        assert payload["camera_warnings"] == []
        assert payload["message"] == (
            "Workflow deployment created successfully")
        [warning] = payload["warnings"]
        assert workflow_id in warning
        for topic in TOPICS:
            assert topic in warning

        # Association record — observed unfixed shape.
        record = wf_env.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["usecase_id"] == wf_env.usecase_id
        assert record["component_type"] == "workflow"
        assert record["workflow_id"] == workflow_id
        assert record["workflow_version"] == 1
        assert record["component_name"] == f"dda.workflow.{workflow_id}"
        assert record["component_version"] == "1.0.0"
        assert record["target_arn"] == wf_env.thing_arn()
        assert record["target_devices"] == [THING]
        assert record["target_thing_group"] is None
        assert record["status"] == "IN_PROGRESS"
        assert record["deployment_status"] == "IN_PROGRESS"
        assert record["is_revision"] is False
        assert record["superseded_deployment_id"] is None
        assert record["created_by"] == wf_env.user["user_id"]
        assert "created_at" in record and "updated_at" in record
        assert "camera_bindings" not in record

        # Audit entry — observed unfixed shape.
        [audit] = deploy_audit_entries(wf_env, workflow_id)
        assert audit["user_id"] == wf_env.user["user_id"]
        assert audit["resource_type"] == "workflow"
        assert audit["result"] == "success"
        details = audit["details"]
        assert details["usecase_id"] == wf_env.usecase_id
        assert details["workflow_version"] == 1
        assert details["deployment_id"] == payload["deployment_id"]
        assert details["component_name"] == f"dda.workflow.{workflow_id}"
        assert details["component_version"] == "1.0.0"
        assert details["target_arn"] == wf_env.thing_arn()
        assert details["target_devices"] == [THING]
        assert details["target_thing_group"] is None
        assert details["is_revision"] is False
        assert details["superseded_deployment_id"] is None


# ==========================================================================
# Property 5 — first-package REVISION deploy is byte-identical (3.1, 3.3)
# ==========================================================================

class TestFirstPackageRevisionDeployByteIdentity:
    """**Validates: Requirements 3.1, 3.3**

    Workflow v2 whose item's arn ends ``:versions:2.0.0`` (first package
    of v2 — NOT the bug condition). The target's existing deployment
    carries LocalServer plus extra components (one with its own
    configurationUpdate) and an older workflow entry. The revision must
    keep today's semantics exactly: carried-over components verbatim,
    workflow entry replaced at ``2.0.0``, deployment name reused,
    record/audit shapes unchanged."""

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
        # Non-subscribing v2 so byte-identity is exact (no subscribe
        # merge document lands on the carried LocalServer entry).
        workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
        wf_env.register_device()
        seeded = self.seeded_components(workflow_id)
        superseded_id = wf_env.gg.seed_deployment(
            wf_env.thing_arn(), seeded, name=self.EXISTING_NAME)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload
        assert payload["is_revision"] is True
        assert payload["superseded_deployment_id"] == superseded_id
        assert payload["component_version"] == "2.0.0"
        assert "warnings" not in payload
        assert set(payload) == EXPECTED_BODY_KEYS

        # Byte-identity (3.1): carried-over components verbatim — the
        # pre-existing configurationUpdate untouched — plus the workflow
        # entry replaced at 2.0.0 (3.3).
        expected = copy.deepcopy(seeded)
        expected[f"dda.workflow.{workflow_id}"] = {
            "componentVersion": "2.0.0"}
        [call] = wf_env.gg.create_deployment_calls
        assert call["components"] == expected

        # Deployment name reused, same target (3.3).
        assert call["deploymentName"] == self.EXISTING_NAME
        assert call["targetArn"] == wf_env.thing_arn()

    def test_record_and_audit_written_with_usual_shape(self, wf_env):
        workflow_id = wf_env.seed_subscribing_workflow(version=2, topics=None)
        wf_env.register_device()
        superseded_id = wf_env.gg.seed_deployment(
            wf_env.thing_arn(), self.seeded_components(workflow_id),
            name=self.EXISTING_NAME)

        status, payload = wf_env.deploy(workflow_id)

        assert status == 201, payload

        # Association record — observed unfixed shape (3.3).
        record = wf_env.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["component_type"] == "workflow"
        assert record["workflow_id"] == workflow_id
        assert record["workflow_version"] == 2
        assert record["component_name"] == f"dda.workflow.{workflow_id}"
        assert record["component_version"] == "2.0.0"
        assert record["target_arn"] == wf_env.thing_arn()
        assert record["target_devices"] == [THING]
        assert record["is_revision"] is True
        assert record["superseded_deployment_id"] == superseded_id
        assert record["status"] == "IN_PROGRESS"
        assert record["deployment_status"] == "IN_PROGRESS"
        assert "camera_bindings" not in record

        # Audit entry — observed unfixed shape.
        [audit] = deploy_audit_entries(wf_env, workflow_id)
        assert audit["result"] == "success"
        details = audit["details"]
        assert details["workflow_version"] == 2
        assert details["deployment_id"] == payload["deployment_id"]
        assert details["component_name"] == f"dda.workflow.{workflow_id}"
        assert details["component_version"] == "2.0.0"
        assert details["is_revision"] is True
        assert details["superseded_deployment_id"] == superseded_id


# ==========================================================================
# Property 6 — consumer fallback fidelity for unrecorded entries
# ==========================================================================

class TestConsumerFallbackFidelity:
    """**Validates: Requirements 3.2, 3.5, 3.6**

    Component sets whose workflow entries match NO recorded component
    version: one version item seeded WITHOUT ``component_arn`` /
    ``component_version`` (the faked-item population
    test_property_subscribed_topics.py rides), and one workflow with no
    version item at all. The three consumers must produce exactly
    today's major-parse results — the fallback the fix must preserve."""

    ARCHITECTURES = ["arm64JP6", "arm64JP5"]
    SUB_TOPICS = ["dda/a-topic", "plant/+/state"]

    @pytest.fixture
    def unrecorded(self, env):
        """(wf_a, wf_b): wf_a has a v3 item with NO packaging fields but
        subscribed_topics + LLM attributes; wf_b has no version item."""
        wf_a = f"wf-{uuid.uuid4().hex[:12]}"
        wf_b = f"wf-{uuid.uuid4().hex[:12]}"
        env.stack.tables.versions.put_item(Item={
            "workflow_id": wf_a,
            "version": 3,
            "subscribed_topics": list(self.SUB_TOPICS),
            "has_llm_inference": True,
            "packaged_architectures": list(self.ARCHITECTURES),
            # Deliberately NO component_arn / component_version: this
            # entry matches no recorded component version, so the fixed
            # code's scan finds nothing and must ride the major parse.
        })
        return wf_a, wf_b

    def components_map(self, wf_a, wf_b):
        return {
            f"dda.workflow.{wf_a}": {"componentVersion": "3.0.0"},
            f"dda.workflow.{wf_b}": {"componentVersion": "7.0.0"},
            LOCAL_SERVER_ARM64JP6: {"componentVersion": "1.0.51"},
            "aws.greengrass.Nucleus": {"componentVersion": "2.12.0"},
        }

    def test_subscribed_topics_match_major_parse(
            self, deployments, unrecorded):
        wf_a, wf_b = unrecorded
        topics = deployments.collect_workflow_subscribed_topics(
            self.components_map(wf_a, wf_b))
        # Today's behavior: entry 3.0.0 -> get_version_item(wf_a, 3) ->
        # the seeded item -> its topics; entry 7.0.0 -> no item -> nothing.
        assert topics == {wf_a: self.SUB_TOPICS}

    def test_binding_keys_match_major_parse(self, deployments, unrecorded):
        wf_a, wf_b = unrecorded
        keys = deployments._deployed_workflow_binding_keys(
            self.components_map(wf_a, wf_b))
        # Today's behavior: pure major parse, table-independent —
        # {workflowId}/{major} for every workflow entry.
        assert keys == {f"{wf_a}/3", f"{wf_b}/7"}

    def test_vllm_manifests_match_major_parse(self, deployments, unrecorded):
        wf_a, wf_b = unrecorded
        # collect_vllm_component_manifests takes {name: version string}.
        manifests = deployments.collect_vllm_component_manifests({
            f"dda.workflow.{wf_a}": "3.0.0",
            f"dda.workflow.{wf_b}": "7.0.0",
            LOCAL_SERVER_ARM64JP6: "1.0.51",
            "aws.greengrass.Nucleus": "2.12.0",
        })
        # Today's behavior: entry 3.0.0 -> item v3 -> has_llm_inference ->
        # manifest with its packaged_architectures; entry 7.0.0 -> no
        # item -> contributes nothing; non-workflow entries ignored.
        assert manifests == {
            f"dda.workflow.{wf_a}": {
                "version": "3.0.0",
                "architectures": self.ARCHITECTURES,
            },
        }
