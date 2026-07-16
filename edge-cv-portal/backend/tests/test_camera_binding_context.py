"""
Deploy-time binding context endpoint and Camera_Binding delivery —
get_camera_binding_context / create_workflow_deployment
(functions/deployments.py, camera-registry-sync task 11.7).

Verifies:
- the binding context view returns, per Camera_Input_Node and target
  Edge_Device, the device's registered Camera_Sources with hint-matching
  pre-selection (Requirements 8.1, 8.5), and an empty matrix for versions
  without Camera_Input_Nodes (8.9);
- submission with camera_bindings writes desired.bindings["{wf}/{ver}"]
  into each target thing's dda-camera-bindings shadow, prunes keys for
  versions no longer deployed, leaves the Greengrass component set
  untouched, and stores the bindings on the workflow-deployment record
  (Requirements 8.2, 8.6, 12.3);
- a registry read failure rejects with REGISTRY_UNAVAILABLE instead of
  skipping validation; a mid-submission shadow write failure aborts
  deployment creation and best-effort prunes already-written targets.
"""
import io
import json
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import REGION, TEST_ENV

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-binding-context"
ACCOUNT_ID = "123456789012"
NOW_MS = 1_730_000_000_000


# --------------------------------------------------------------------------
# Module import with the camera registry table in place
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def binding_stack(aws_stack):
    import boto3
    import os

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=CAMERA_REGISTRY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME

    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "deployments": deployments,
        "registry": resource.Table(CAMERA_REGISTRY_TABLE_NAME),
    }


# --------------------------------------------------------------------------
# Fake Use_Case-account clients (greengrassv2 / iot-data)
# --------------------------------------------------------------------------

class FakeGreengrass:
    """Just enough greengrassv2 for the deployment flow: LocalServer
    compatibility listing, latest-deployment lookup, and creation."""

    def __init__(self):
        self.installed = {}
        self.create_deployment_calls = []

    def register_device(self, thing_name, local_server_version="99.0.0"):
        self.installed[thing_name] = [{
            "componentName": "aws.edgeml.dda.LocalServer.x86_64",
            "componentVersion": local_server_version,
        }]

    def get_paginator(self, operation):
        assert operation == "list_installed_components"
        installed = self.installed

        class _Paginator:
            def paginate(self, coreDeviceThingName=None, **_):
                return iter([{"installedComponents":
                              list(installed.get(coreDeviceThingName, []))}])
        return _Paginator()

    def list_deployments(self, **_):
        return {"deployments": []}

    def create_deployment(self, **params):
        self.create_deployment_calls.append(params)
        deployment_id = f"dep-{uuid.uuid4()}"
        return {"deploymentId": deployment_id,
                "iotJobId": f"job-{deployment_id}",
                "iotJobArn": f"arn:aws:iot:{REGION}:{ACCOUNT_ID}:job/x"}


class FakeIotData:
    """Stateful fake of the assumed-role iot-data client: named-shadow
    get/update with standard null-key pruning semantics."""

    def __init__(self):
        self.bindings = {}  # thing_name -> {key: bindings}
        self.fail_for = set()

    def get_thing_shadow(self, thingName, shadowName):
        assert shadowName == "dda-camera-bindings"
        if thingName not in self.bindings:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "no shadow"}}, "GetThingShadow")
        payload = json.dumps({"state": {"desired": {
            "bindings": self.bindings[thingName]}}})
        return {"payload": io.BytesIO(payload.encode())}

    def update_thing_shadow(self, thingName, shadowName, payload):
        assert shadowName == "dda-camera-bindings"
        if thingName in self.fail_for:
            raise ClientError(
                {"Error": {"Code": "InternalFailure", "Message": "boom"}},
                "UpdateThingShadow")
        update = json.loads(payload)["state"]["desired"]["bindings"]
        current = self.bindings.setdefault(thingName, {})
        for key, value in update.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

class BindingEnv:
    def __init__(self, env, binding_stack, monkeypatch):
        self.env = env
        self.deployments = binding_stack["deployments"]
        self.registry = binding_stack["registry"]
        self.user = env.make_user(role="UseCaseAdmin")
        self.usecase_id = env.create_usecase()
        self.workflow_id = f"wf-{uuid.uuid4()}"

        self.gg = FakeGreengrass()
        self.iot_data = FakeIotData()

        def fake_client(service_name, usecase, session_name=None, region=None):
            assert usecase["usecase_id"] == self.usecase_id
            if service_name == "greengrassv2":
                return self.gg
            if service_name == "iot-data":
                return self.iot_data
            if service_name == "iot":
                # Built unconditionally by the create flow; only used to
                # resolve thing-group targets, which these tests avoid.
                return object()
            raise AssertionError(f"unexpected client: {service_name}")

        monkeypatch.setattr(self.deployments, "get_usecase_client", fake_client)

    # ------------------------------------------------------------- setup
    def seed_workflow(self, camera_nodes, has_binding_points=True):
        self.env.stack.tables.workflows.put_item(Item={
            "workflow_id": self.workflow_id,
            "usecase_id": self.usecase_id,
            "name": "camera workflow",
            "latest_version": 1,
        })
        item = {
            "workflow_id": self.workflow_id,
            "version": 1,
            "validation_status": {"status": "passed", "validated_at": 1,
                                  "findings": []},
            "component_arn": f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                             f"components:wf:versions:1",
        }
        if camera_nodes is not None:
            item["has_binding_points"] = has_binding_points
            item["camera_input_nodes"] = camera_nodes
        self.env.stack.tables.versions.put_item(Item=item)

    def seed_registry(self, thing_name, cameras, never_synced=False):
        self.registry.put_item(Item={
            "device_id": thing_name, "sk": "META",
            "usecase_id": self.usecase_id,
            "never_synced": never_synced, "last_report_at": NOW_MS,
        })
        for csid, entry in cameras.items():
            item = {
                "device_id": thing_name, "sk": f"CAMERA#{csid}",
                "camera_source_id": csid, "usecase_id": self.usecase_id,
                "name": f"cam {csid}", "type": "Camera",
                "params": {"devicePath": "/dev/video0"},
                "capabilities": {}, "origin": "edge-configured",
                "version": 1, "sync_status": "synced", "absent": False,
                # Fresh (non-stale) unless the entry overrides it.
                "last_reported_at": int(
                    self.deployments.datetime.utcnow().timestamp() * 1000),
            }
            item.update(entry)
            self.registry.put_item(Item={k: v for k, v in item.items()
                                         if v is not None})

    # ------------------------------------------------------------ invoke
    def binding_context(self, targets, **query):
        query = {"view": "binding-context", "usecase_id": self.usecase_id,
                 "workflow_id": self.workflow_id,
                 "target_devices": ",".join(targets), **query}
        event = self.env.event("GET", "/deployments", self.user, query=query)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def deploy(self, targets, **body):
        body = {"component_type": "workflow", "usecase_id": self.usecase_id,
                "workflow_id": self.workflow_id, "target_devices": targets,
                **body}
        event = self.env.event("POST", "/deployments", self.user, body=body)
        response = self.deployments.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


def camera_node(node_id="n1", hint_csid=None):
    node = {"node_id": node_id, "node_type": "camera_source",
            "compiled_device_paths": {"x86_64": "/dev/video0"}}
    if hint_csid:
        node["binding_hint"] = {"cameraSourceId": hint_csid}
    return node


@pytest.fixture
def fleet(env, binding_stack, monkeypatch):
    return BindingEnv(env, binding_stack, monkeypatch)


# --------------------------------------------------------------------------
# Binding context endpoint (Requirements 8.1, 8.5, 8.9)
# --------------------------------------------------------------------------

class TestBindingContext:
    def test_per_target_cameras_with_hint_preselection(self, fleet):
        """The hint pre-selects only on devices whose registry contains
        the hinted source (8.5); every device's Camera_Sources are the
        selectable options (8.1)."""
        fleet.seed_workflow([camera_node(hint_csid="cfg-1")])
        fleet.seed_registry("line-a", {"cfg-1": {}, "cfg-2": {}})
        fleet.seed_registry("line-b", {"cfg-9": {}})

        status, payload = fleet.binding_context(["line-a", "line-b"])

        assert status == 200, payload
        assert payload["binding_required"] is True
        assert payload["camera_input_nodes"] == [{
            "node_id": "n1", "node_type": "camera_source",
            "binding_hint": {"cameraSourceId": "cfg-1"},
        }]
        line_a = payload["targets"]["line-a"]
        assert line_a["state"] == "synced"
        assert [c["camera_source_id"] for c in line_a["cameras"]] == \
            ["cfg-1", "cfg-2"]
        assert line_a["preselected"] == {"n1": "cfg-1"}
        # Fields the picker displays (7.4-shaped view)
        option = line_a["cameras"][0]
        for field in ("name", "type", "params", "sync_status", "stale",
                      "absent", "origin"):
            assert field in option
        # line-b does not register the hinted source: no pre-selection
        assert payload["targets"]["line-b"]["preselected"] == {}

    def test_never_synced_target_state(self, fleet):
        fleet.seed_workflow([camera_node()])
        status, payload = fleet.binding_context(["line-x"])
        assert status == 200, payload
        assert payload["targets"]["line-x"]["state"] == "never-synced"
        assert payload["targets"]["line-x"]["cameras"] == []

    def test_version_without_camera_nodes_returns_empty_matrix(self, fleet):
        """No Camera_Input_Nodes: the frontend skips the step (8.9); no
        targets are even required."""
        fleet.seed_workflow(None)
        status, payload = fleet.binding_context([])
        assert status == 200, payload
        assert payload["binding_required"] is False
        assert payload["camera_input_nodes"] == []
        assert payload["targets"] == {}


# --------------------------------------------------------------------------
# Submission: delivery, pruning, record storage (8.2, 8.6, 12.3)
# --------------------------------------------------------------------------

class TestSubmission:
    def test_bindings_delivered_pruned_and_recorded(self, fleet):
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.seed_registry("line-b", {"cfg-2": {}})
        fleet.gg.register_device("line-a")
        fleet.gg.register_device("line-b")
        # Pre-existing shadow keys on line-a: an older version of this
        # workflow and a workflow absent from the deployment's component
        # set — both no longer deployed, both pruned.
        key = f"{fleet.workflow_id}/1"
        fleet.iot_data.bindings["line-a"] = {
            f"{fleet.workflow_id}/0": {"n1": {"cameraSourceId": "old"}},
            "gone-wf/3": {"n1": {"cameraSourceId": "old"}},
        }
        bindings = {"line-a": {"n1": {"cameraSourceId": "cfg-1"}},
                    "line-b": {"n1": {"cameraSourceId": "cfg-2"}}}

        status, payload = fleet.deploy(["line-a", "line-b"],
                                       camera_bindings=bindings)

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is True
        # Distinct bindings per device, keyed {workflowId}/{version} (8.2)
        assert fleet.iot_data.bindings["line-a"] == {
            key: {"n1": {"cameraSourceId": "cfg-1"}}}
        assert fleet.iot_data.bindings["line-b"] == {
            key: {"n1": {"cameraSourceId": "cfg-2"}}}
        # Greengrass deployment carries only the component map — the
        # artifact and component set are untouched by bindings (8.6)
        [call] = fleet.gg.create_deployment_calls
        assert call["components"] == {
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "1.0.0"}}
        # Bindings stored on the workflow-deployment record (12.3)
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["camera_bindings"] == bindings

    def test_registry_read_failure_rejects_with_registry_unavailable(
            self, fleet, monkeypatch):
        fleet.seed_workflow([camera_node()])
        fleet.gg.register_device("line-a")
        monkeypatch.setattr(fleet.deployments, "CAMERA_REGISTRY_TABLE",
                            "missing-table-name")
        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})
        assert status == 503
        assert payload["error"]["code"] == "REGISTRY_UNAVAILABLE"
        assert fleet.gg.create_deployment_calls == []

    def test_mid_submission_failure_aborts_and_prunes_written_targets(
            self, fleet):
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.seed_registry("line-b", {"cfg-2": {}})
        fleet.gg.register_device("line-a")
        fleet.gg.register_device("line-b")
        fleet.iot_data.fail_for.add("line-b")

        status, payload = fleet.deploy(
            ["line-a", "line-b"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}},
                             "line-b": {"n1": {"cameraSourceId": "cfg-2"}}})

        assert status == 502
        assert payload["error"]["code"] == "BINDING_DELIVERY_FAILED"
        assert payload["error"]["details"]["failed_device"] == "line-b"
        # No Greengrass deployment was created
        assert fleet.gg.create_deployment_calls == []
        # line-a's already-written key was best-effort pruned
        assert f"{fleet.workflow_id}/1" not in \
            fleet.iot_data.bindings.get("line-a", {})

    def test_unbound_node_rejected_before_any_shadow_write(self, fleet):
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a")
        status, payload = fleet.deploy(["line-a"])
        assert status == 409
        assert payload["error"]["code"] == "CAMERA_BINDINGS_INVALID"
        assert fleet.iot_data.bindings == {}
        assert fleet.gg.create_deployment_calls == []
