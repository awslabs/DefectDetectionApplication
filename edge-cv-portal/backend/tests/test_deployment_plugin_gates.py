"""
Plugin lifecycle and architecture deployment gates
(functions/deployments.py + functions/devices.py).

Task 10.5 (spec: custom-node-designer). The pre-submit check that already
compares LocalServer versions per device gains two gates over the
deployment's Plugin_Component dependency closure:

- Lifecycle gate (Requirements 9.7, 9.8, 9.11, 16.3): dev-state
  components are rejected for any target; test-state components are
  permitted only to devices flagged ``test_device`` (a Devices-table
  attribute a UseCaseAdmin sets); prod deploys anywhere in the Use_Case.
  Rejections carry PLUGIN_LIFECYCLE_VIOLATION identifying the
  Plugin_Component and its Lifecycle_State.
- Architecture gate (Requirement 16.6): each target device's recorded
  Target_Architecture is checked against the platform manifests of every
  depended-on Plugin_Component version — x86_64 and x86_64_nvidia
  matched distinctly, no fallback — rejecting with
  PLUGIN_ARCH_UNSUPPORTED listing each offending
  {pluginComponent, version, device, deviceArch}.

Standalone Plugin_Component deployments are recorded in the Deployments
table with component_type: 'plugin'; workflow deployments rely on
Greengrass dependency resolution to deliver the depended-on
Plugin_Component versions (Requirement 16.5), so the deployment's
component set carries only the Workflow_Component.

The property tests for the gate decision logic are tasks 10.6/10.7;
these are the example-based unit and integration tests.
"""
import json
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Key

from test_workflow_packaging_deployment_integration import (
    ACCOUNT_ID, REGION, FakeGreengrass, FakeIot, make_dewarp_definition)


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


@pytest.fixture(scope="module")
def devices_module(aws_stack):
    sys.modules.pop("devices", None)
    import devices

    return devices


# ==========================================================================
# Pure gate decision logic (evaluate_plugin_lifecycle_gate /
# evaluate_plugin_arch_gate)
# ==========================================================================

class TestLifecycleGatePure:
    def test_prod_deploys_anywhere(self, deployments):
        violations = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.p1": "prod"},
            {"line-a": False, "bench-1": True})
        assert violations == []

    def test_dev_rejected_for_any_target(self, deployments):
        """dev-state components are rejected even when every target is a
        Test_Device (9.7 fail-closed / 16.3)."""
        violations = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.p1": "dev"},
            {"bench-1": True, "bench-2": True})
        [violation] = violations
        assert violation["pluginComponent"] == "dda.plugin.p1"
        assert violation["lifecycleState"] == "dev"
        assert violation["devices"] == ["bench-1", "bench-2"]

    def test_unknown_state_fails_closed(self, deployments):
        [violation] = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.p1": None}, {"bench-1": True})
        assert violation["lifecycleState"] is None

    def test_test_state_only_to_test_devices(self, deployments):
        """test-state components reject exactly the unflagged targets,
        identifying the component and its state (9.7, 9.8)."""
        violations = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.p1": "test"},
            {"line-a": False, "bench-1": True, "line-b": False})
        [violation] = violations
        assert violation["pluginComponent"] == "dda.plugin.p1"
        assert violation["lifecycleState"] == "test"
        assert violation["devices"] == ["line-a", "line-b"]

    def test_test_state_allowed_when_all_targets_flagged(self, deployments):
        violations = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.p1": "test"},
            {"bench-1": True, "bench-2": True})
        assert violations == []

    def test_mixed_closure_reports_every_violation(self, deployments):
        violations = deployments.evaluate_plugin_lifecycle_gate(
            {"dda.plugin.dev1": "dev", "dda.plugin.ok": "prod",
             "dda.plugin.t1": "test"},
            {"line-a": False})
        assert [v["pluginComponent"] for v in violations] == \
            ["dda.plugin.dev1", "dda.plugin.t1"]


class TestArchGatePure:
    MANIFEST = {"dda.plugin.p1": {"version": "2.0.0",
                                  "architectures": ["x86_64", "arm64_jp5"]}}

    def test_covered_architectures_pass(self, deployments):
        offending = deployments.evaluate_plugin_arch_gate(
            self.MANIFEST, {"d1": "x86_64", "d2": "arm64_jp5"})
        assert offending == []

    def test_x86_64_device_does_not_match_nvidia_only_manifest(self, deployments):
        """x86_64 and x86_64_nvidia are matched distinctly — no fallback
        in either direction (16.6)."""
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.p1": {"version": "1.0.0",
                               "architectures": ["x86_64_nvidia"]}},
            {"d1": "x86_64"})
        assert offending == [{"pluginComponent": "dda.plugin.p1",
                              "version": "1.0.0", "device": "d1",
                              "deviceArch": "x86_64"}]

    def test_nvidia_device_does_not_match_plain_x86_64_manifest(self, deployments):
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.p1": {"version": "1.0.0",
                               "architectures": ["x86_64"]}},
            {"d1": "x86_64_nvidia"})
        assert offending == [{"pluginComponent": "dda.plugin.p1",
                              "version": "1.0.0", "device": "d1",
                              "deviceArch": "x86_64_nvidia"}]

    def test_unrecorded_device_architecture_fails_closed(self, deployments):
        offending = deployments.evaluate_plugin_arch_gate(
            self.MANIFEST, {"d1": None})
        assert offending == [{"pluginComponent": "dda.plugin.p1",
                              "version": "2.0.0", "device": "d1",
                              "deviceArch": None}]

    def test_every_component_device_miss_is_listed(self, deployments):
        offending = deployments.evaluate_plugin_arch_gate(
            {"dda.plugin.a": {"version": "1.0.0", "architectures": ["x86_64"]},
             "dda.plugin.b": {"version": "3.0.0", "architectures": ["arm64_jp5"]}},
            {"d1": "x86_64", "d2": "arm64_jp5"})
        assert offending == [
            {"pluginComponent": "dda.plugin.a", "version": "1.0.0",
             "device": "d2", "deviceArch": "arm64_jp5"},
            {"pluginComponent": "dda.plugin.b", "version": "3.0.0",
             "device": "d1", "deviceArch": "x86_64"},
        ]


# ==========================================================================
# Integration harness
# ==========================================================================

class PluginGateEnv:
    """A validated + packaged workflow version whose recorded
    Plugin_Component closure and target devices are seeded directly, with
    the stateful Greengrass/IoT fakes on the deployment side."""

    def __init__(self, env, deployments, monkeypatch):
        self.env = env
        self.deployments = deployments

        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = f"uc-{uuid.uuid4()}"
        env.stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Plugin Gate Test",
            "account_id": ACCOUNT_ID,
        })

        status, payload = env.invoke("POST", "/workflows", self.user, body={
            "usecase_id": self.usecase_id,
            "name": "gated workflow",
            "definition": make_dewarp_definition(),
        })
        assert status == 201, payload
        self.workflow_id = payload["workflow"]["workflow_id"]

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

    # ------------------------------------------------------------- setup
    def mark_packaged(self, plugin_components, version=1):
        """Validated + packaged version item with the recorded
        Plugin_Component dependency closure (what workflow_packaging.py
        persists)."""
        self.env.stack.tables.versions.update_item(
            Key={"workflow_id": self.workflow_id, "version": version},
            UpdateExpression=("SET validation_status = :v, "
                              "component_arn = :arn, plugin_components = :pc"),
            ExpressionAttributeValues={
                ":v": {"status": "passed", "validated_at": 1,
                       "findings_key": "findings/none.json"},
                ":arn": (f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:components:"
                         f"dda.workflow.{self.workflow_id}:versions:1.0.0"),
                ":pc": dict(plugin_components),
            },
        )

    def seed_plugin_record(self, plugin_id, version, lifecycle_state, archs):
        """A backing Plugin_Record with a registered Plugin_Component whose
        platform manifests cover exactly ``archs``."""
        self.env.stack.tables.plugin_records.put_item(Item={
            "plugin_id": plugin_id,
            "version": version,
            "usecase_id": self.usecase_id,
            "created_at": 1,
            "name": plugin_id,
            "lifecycle_state": lifecycle_state,
            "artifacts": {arch: {"buildStatus": "succeeded",
                                 "s3Key": f"plugins/{plugin_id}/{arch}.so",
                                 "checksum": "c" * 8, "signature": "s" * 8}
                          for arch in archs},
            "component": {"name": f"dda.plugin.{plugin_id}",
                          "version": f"{version}.0.0",
                          "arn": "arn:test",
                          "architectures": list(archs),
                          "status": "registered",
                          "packagedAt": 1,
                          "failure": None},
        })

    def put_device_record(self, thing_name, test_device=False, arch=None):
        item = {"device_id": thing_name, "usecase_id": self.usecase_id,
                "test_device": test_device}
        if arch is not None:
            item["target_architecture"] = arch
        self.env.stack.tables.devices.put_item(Item=item)

    def register_device(self, thing_name, local_server_version="1.2.0"):
        self.gg.register_device(thing_name,
                                local_server_version=local_server_version)

    # ------------------------------------------------------------ invoke
    def deploy_workflow(self, **body):
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": self.workflow_id, **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deploy_components(self, components, **body):
        body = {"usecase_id": self.usecase_id, "components": components,
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def association_record(self, deployment_id):
        return self.env.stack.tables.deployments.get_item(
            Key={"deployment_id": deployment_id}).get("Item")


@pytest.fixture
def gate_env(env, deployments, monkeypatch):
    return PluginGateEnv(env, deployments, monkeypatch)


# ==========================================================================
# Workflow deployments: lifecycle gate over the dependency closure
# (Requirements 9.7, 9.8, 9.11, 16.3, 16.5)
# ==========================================================================

class TestWorkflowLifecycleGate:
    def test_test_state_plugin_rejected_for_non_test_device(self, gate_env):
        """A workflow depending on a test-state plugin may not be deployed
        to a device not flagged test_device; the rejection identifies the
        Plugin_Component and its Lifecycle_State (9.8)."""
        gate_env.seed_plugin_record("edgefilter", 2, "test", ["x86_64"])
        gate_env.mark_packaged({"dda.plugin.edgefilter": "2.0.0"})
        gate_env.register_device("line-a-camera-01")
        gate_env.put_device_record("line-a-camera-01", test_device=False,
                                   arch="x86_64")

        status, payload = gate_env.deploy_workflow(
            target_devices=["line-a-camera-01"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_LIFECYCLE_VIOLATION"
        [violation] = payload["error"]["details"]["violations"]
        assert violation["pluginComponent"] == "dda.plugin.edgefilter"
        assert violation["lifecycleState"] == "test"
        assert violation["version"] == "2.0.0"
        assert violation["devices"] == ["line-a-camera-01"]

        # Rejected pre-submit: nothing reached Greengrass, no association.
        assert gate_env.gg.create_deployment_calls == []

    def test_test_state_plugin_deploys_to_flagged_test_device(self, gate_env):
        """Test_Devices accept test-state plugins (9.7); the deployment's
        component set carries only the Workflow_Component — Greengrass
        dependency resolution delivers the Plugin_Component (16.5)."""
        gate_env.seed_plugin_record("edgefilter", 2, "test", ["x86_64"])
        gate_env.mark_packaged({"dda.plugin.edgefilter": "2.0.0"})
        gate_env.register_device("bench-01")
        gate_env.put_device_record("bench-01", test_device=True,
                                   arch="x86_64")

        status, payload = gate_env.deploy_workflow(target_devices=["bench-01"])

        assert status == 201, payload
        [call] = gate_env.gg.create_deployment_calls
        assert set(call["components"]) == \
            {f"dda.workflow.{gate_env.workflow_id}"}

    def test_dev_state_plugin_rejected_even_for_test_device(self, gate_env):
        """dev-state components are rejected for any target (16.3 /
        Requirement 9 dev gates)."""
        gate_env.seed_plugin_record("edgefilter", 1, "dev", ["x86_64"])
        gate_env.mark_packaged({"dda.plugin.edgefilter": "1.0.0"})
        gate_env.register_device("bench-01")
        gate_env.put_device_record("bench-01", test_device=True,
                                   arch="x86_64")

        status, payload = gate_env.deploy_workflow(target_devices=["bench-01"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_LIFECYCLE_VIOLATION"
        [violation] = payload["error"]["details"]["violations"]
        assert violation["lifecycleState"] == "dev"
        assert gate_env.gg.create_deployment_calls == []

    def test_prod_state_plugin_deploys_anywhere(self, gate_env):
        """prod-state plugins deploy to any device in the Use_Case (9.11)."""
        gate_env.seed_plugin_record("edgefilter", 3, "prod", ["x86_64"])
        gate_env.mark_packaged({"dda.plugin.edgefilter": "3.0.0"})
        gate_env.register_device("line-a-camera-01")
        gate_env.put_device_record("line-a-camera-01", test_device=False,
                                   arch="x86_64")

        status, payload = gate_env.deploy_workflow(
            target_devices=["line-a-camera-01"])

        assert status == 201, payload

    def test_workflow_without_custom_plugins_is_unaffected(self, gate_env):
        gate_env.mark_packaged({})
        gate_env.register_device("line-a-camera-01")

        status, payload = gate_env.deploy_workflow(
            target_devices=["line-a-camera-01"])

        assert status == 201, payload


# ==========================================================================
# Workflow deployments: architecture gate (Requirement 16.6)
# ==========================================================================

class TestWorkflowArchitectureGate:
    def test_device_arch_missing_from_manifests_rejects(self, gate_env):
        """A device whose recorded Target_Architecture has no published
        Plugin_Artifact in a depended-on Plugin_Component version rejects
        the submission listing {pluginComponent, version, device,
        deviceArch} — x86_64 does not fall back to x86_64_nvidia (16.6)."""
        gate_env.seed_plugin_record("gpufilter", 1, "prod", ["x86_64_nvidia"])
        gate_env.mark_packaged({"dda.plugin.gpufilter": "1.0.0"})
        gate_env.register_device("line-a-camera-01")
        gate_env.put_device_record("line-a-camera-01", test_device=False,
                                   arch="x86_64")

        status, payload = gate_env.deploy_workflow(
            target_devices=["line-a-camera-01"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_ARCH_UNSUPPORTED"
        assert payload["error"]["details"]["unsupported"] == [{
            "pluginComponent": "dda.plugin.gpufilter",
            "version": "1.0.0",
            "device": "line-a-camera-01",
            "deviceArch": "x86_64",
        }]
        assert gate_env.gg.create_deployment_calls == []

    def test_matching_nvidia_arch_deploys(self, gate_env):
        gate_env.seed_plugin_record("gpufilter", 1, "prod", ["x86_64_nvidia"])
        gate_env.mark_packaged({"dda.plugin.gpufilter": "1.0.0"})
        gate_env.register_device("gpu-station-01")
        gate_env.put_device_record("gpu-station-01", test_device=False,
                                   arch="x86_64_nvidia")

        status, payload = gate_env.deploy_workflow(
            target_devices=["gpu-station-01"])

        assert status == 201, payload


# ==========================================================================
# Standalone Plugin_Component deployments (Requirement 16.3 + the
# component_type: 'plugin' Deployments-table record)
# ==========================================================================

class TestStandalonePluginDeployment:
    COMPONENT = [{"component_name": "dda.plugin.edgefilter",
                  "component_version": "2.0.0"}]

    def test_standalone_deploy_records_plugin_association(self, gate_env):
        gate_env.seed_plugin_record("edgefilter", 2, "prod", ["x86_64"])
        gate_env.put_device_record("line-a-camera-01", test_device=False,
                                   arch="x86_64")

        status, payload = gate_env.deploy_components(
            self.COMPONENT, target_devices=["line-a-camera-01"])

        assert status == 201, payload
        [call] = gate_env.gg.create_deployment_calls
        assert call["components"] == {
            "dda.plugin.edgefilter": {"componentVersion": "2.0.0"}}

        record = gate_env.association_record(payload["deployment_id"])
        assert record is not None
        assert record["component_type"] == "plugin"
        assert record["component_name"] == "dda.plugin.edgefilter"
        assert record["component_version"] == "2.0.0"
        assert record["plugin_components"] == \
            {"dda.plugin.edgefilter": "2.0.0"}
        assert record["target_devices"] == ["line-a-camera-01"]

    def test_standalone_test_state_restricted_to_test_devices(self, gate_env):
        """Standalone Plugin_Component deployments are subject to the same
        lifecycle gate: test state only to Test_Devices (16.3)."""
        gate_env.seed_plugin_record("edgefilter", 2, "test", ["x86_64"])
        gate_env.put_device_record("line-a-camera-01", test_device=False,
                                   arch="x86_64")

        status, payload = gate_env.deploy_components(
            self.COMPONENT, target_devices=["line-a-camera-01"])

        assert status == 409
        assert payload["error"]["code"] == "PLUGIN_LIFECYCLE_VIOLATION"
        assert gate_env.gg.create_deployment_calls == []
        # Nothing was recorded for the rejected submission.
        response = gate_env.env.stack.tables.deployments.query(
            IndexName="usecase-deployments-index",
            KeyConditionExpression=Key("usecase_id").eq(gate_env.usecase_id))
        assert response.get("Items", []) == []

    def test_standalone_test_state_deploys_to_test_device(self, gate_env):
        gate_env.seed_plugin_record("edgefilter", 2, "test", ["x86_64"])
        gate_env.put_device_record("bench-01", test_device=True,
                                   arch="x86_64")

        status, payload = gate_env.deploy_components(
            self.COMPONENT, target_devices=["bench-01"])

        assert status == 201, payload
        record = gate_env.association_record(payload["deployment_id"])
        assert record["component_type"] == "plugin"


# ==========================================================================
# Devices table test_device flag (set by a UseCaseAdmin)
# ==========================================================================

class TestDeviceFlagEndpoint:
    def _put(self, env, devices_module, user, usecase_id, device_id, body):
        event = env.event("PUT", "/devices/{id}", user,
                          workflow_id=device_id,
                          body={"usecase_id": usecase_id, **body})
        response = devices_module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def test_usecase_admin_sets_test_device_flag(self, env, devices_module):
        usecase_id = env.create_usecase()
        admin = env.make_user(role="UseCaseAdmin")

        status, payload = self._put(
            env, devices_module, admin, usecase_id, "bench-01",
            {"test_device": True, "target_architecture": "x86_64"})

        assert status == 200, payload
        assert payload["test_device"] is True
        assert payload["target_architecture"] == "x86_64"

        item = env.stack.tables.devices.get_item(
            Key={"device_id": "bench-01"}).get("Item")
        assert item["test_device"] is True
        assert item["target_architecture"] == "x86_64"
        assert item["usecase_id"] == usecase_id

    def test_flag_can_be_cleared(self, env, devices_module):
        usecase_id = env.create_usecase()
        admin = env.make_user(role="UseCaseAdmin")
        self._put(env, devices_module, admin, usecase_id, "bench-02",
                  {"test_device": True})

        status, payload = self._put(
            env, devices_module, admin, usecase_id, "bench-02",
            {"test_device": False})

        assert status == 200, payload
        assert payload["test_device"] is False

    @pytest.mark.parametrize("role", ["Operator", "DataScientist", "Viewer"])
    def test_non_admin_roles_are_denied(self, env, devices_module, role):
        usecase_id = env.create_usecase()
        user = env.make_user(role=role)

        status, payload = self._put(
            env, devices_module, user, usecase_id, "bench-03",
            {"test_device": True})

        assert status == 403, payload
        assert env.stack.tables.devices.get_item(
            Key={"device_id": "bench-03"}).get("Item") is None

    def test_invalid_target_architecture_rejected(self, env, devices_module):
        usecase_id = env.create_usecase()
        admin = env.make_user(role="UseCaseAdmin")

        status, payload = self._put(
            env, devices_module, admin, usecase_id, "bench-04",
            {"target_architecture": "risc-v"})

        assert status == 400, payload
