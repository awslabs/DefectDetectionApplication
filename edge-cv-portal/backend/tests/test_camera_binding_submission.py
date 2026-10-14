"""
Deployment submission behavior — create_workflow_deployment /
record_workflow_deployment (functions/deployments.py, camera-registry-sync
task 11.10).

Fills the route-level gaps around task 11.7's examples
(test_camera_binding_context.py) and task 11.9's pure-function properties
(test_camera_binding_delivery_properties.py):

- the deploy_workflow audit event of a deployment created with
  Camera_Bindings carries the acting user, the deployment identifier, the
  target Edge_Devices, and the delivered camera_bindings, with a timestamp
  (Requirement 12.3); rejected submissions (REGISTRY_UNAVAILABLE, delivery
  failure) log no deploy_workflow event and leave no deployment record;
- a shadow write failing on the FIRST target aborts with nothing written
  and nothing to prune (Requirement 8.6);
- the confirmed-warnings path succeeds end to end with the warnings
  echoed (confirmed) in the 201 response, while unconfirmed warnings
  reject before any shadow write (Requirements 8.6, 9.3 route wiring);
- binding storage on the workflow-deployment record: numeric override
  values survive DynamoDB via _dynamo_safe (floats stored as Decimal),
  a route-level manual override round-trips through shadow and record,
  and a version without Camera_Input_Nodes stores no camera_bindings
  attribute and reports camera_bindings_delivered false (Requirement 12.3).
"""
import sys
import uuid
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from conftest import REGION, TEST_ENV
from test_camera_binding_context import BindingEnv, camera_node

SUBMISSION_REGISTRY_TABLE = "test-camera-registry-submission"


# --------------------------------------------------------------------------
# Module stack: own registry table name so this module can coexist with
# test_camera_binding_context.py in one moto session
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def binding_stack(aws_stack):
    import os

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=SUBMISSION_REGISTRY_TABLE,
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
    os.environ["CAMERA_REGISTRY_TABLE"] = SUBMISSION_REGISTRY_TABLE

    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield {
        "deployments": deployments,
        "registry": resource.Table(SUBMISSION_REGISTRY_TABLE),
    }


@pytest.fixture
def fleet(env, binding_stack, monkeypatch):
    return BindingEnv(env, binding_stack, monkeypatch)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def audit_events(user_id, action=None):
    """All audit records for one acting user (fresh uuid user per test)."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(
        TEST_ENV["AUDIT_LOG_TABLE"])
    items, kwargs = [], {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    events = [i for i in items if i.get("user_id") == user_id]
    if action is not None:
        events = [e for e in events if e.get("action") == action]
    return events


def deployment_records(fleet):
    """Workflow-deployment records of this test's fresh use case."""
    response = fleet.env.stack.tables.deployments.query(
        IndexName="usecase-deployments-index",
        KeyConditionExpression=Key("usecase_id").eq(fleet.usecase_id))
    return response.get("Items", [])


# --------------------------------------------------------------------------
# Audit event payload (Requirement 12.3)
# --------------------------------------------------------------------------

class TestDeploymentAuditEvent:
    def test_deploy_with_bindings_records_user_targets_and_bindings(
            self, fleet):
        """The deploy_workflow audit event carries the acting user, the
        deployment identifier, the target Edge_Devices, and the delivered
        camera_bindings, with a timestamp (12.3)."""
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.seed_registry("line-b", {"cfg-2": {}})
        fleet.gg.register_device("line-a")
        fleet.gg.register_device("line-b")
        bindings = {"line-a": {"n1": {"cameraSourceId": "cfg-1"}},
                    "line-b": {"n1": {"cameraSourceId": "cfg-2"}}}

        status, payload = fleet.deploy(["line-a", "line-b"],
                                       camera_bindings=bindings)

        assert status == 201, payload
        events = audit_events(fleet.user["user_id"], "deploy_workflow")
        assert len(events) == 1
        record = events[0]
        assert record["user_id"] == fleet.user["user_id"]
        assert record["resource_type"] == "workflow"
        assert record["resource_id"] == fleet.workflow_id
        assert record["result"] == "success"
        assert int(record["timestamp"]) > 0
        details = record["details"]
        assert details["deployment_id"] == payload["deployment_id"]
        assert details["usecase_id"] == fleet.usecase_id
        assert details["target_devices"] == ["line-a", "line-b"]
        assert details["camera_bindings"] == bindings

    def test_rejected_delivery_logs_no_deploy_event_and_no_record(
            self, fleet):
        """A submission aborted by a binding delivery failure logs no
        deploy_workflow audit event and stores no deployment record —
        only successful creations are audited (12.3)."""
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a")
        fleet.iot_data.fail_for.add("line-a")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 502
        assert payload["error"]["code"] == "BINDING_DELIVERY_FAILED"
        assert audit_events(fleet.user["user_id"], "deploy_workflow") == []
        assert deployment_records(fleet) == []


# --------------------------------------------------------------------------
# REGISTRY_UNAVAILABLE rejection side effects (Requirement 8.6 gate)
# --------------------------------------------------------------------------

class TestRegistryUnavailable:
    def test_rejection_leaves_no_side_effects(self, fleet, monkeypatch):
        """REGISTRY_UNAVAILABLE rejects before any shadow write, stores
        no deployment record, and logs no deploy_workflow audit event."""
        fleet.seed_workflow([camera_node()])
        fleet.gg.register_device("line-a")
        monkeypatch.setattr(fleet.deployments, "CAMERA_REGISTRY_TABLE",
                            "missing-table-name")

        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})

        assert status == 503
        assert payload["error"]["code"] == "REGISTRY_UNAVAILABLE"
        assert fleet.iot_data.bindings == {}
        assert deployment_records(fleet) == []
        assert audit_events(fleet.user["user_id"], "deploy_workflow") == []


# --------------------------------------------------------------------------
# Partial shadow-write failure on the FIRST target (Requirement 8.6)
# --------------------------------------------------------------------------

class TestFirstTargetFailure:
    def test_first_target_failure_writes_nothing_and_prunes_nothing(
            self, fleet):
        """A shadow write failing on the first target aborts with nothing
        written and nothing to prune: every target's shadow (including
        pre-existing keys on the failing device) is untouched and no
        Greengrass deployment is created (8.6)."""
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.seed_registry("line-b", {"cfg-2": {}})
        fleet.gg.register_device("line-a")
        fleet.gg.register_device("line-b")
        pre_existing = {"other-wf/2": {"n1": {"cameraSourceId": "old"}}}
        fleet.iot_data.bindings["line-a"] = dict(pre_existing)
        fleet.iot_data.fail_for.add("line-a")

        status, payload = fleet.deploy(
            ["line-a", "line-b"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}},
                             "line-b": {"n1": {"cameraSourceId": "cfg-2"}}})

        assert status == 502
        assert payload["error"]["code"] == "BINDING_DELIVERY_FAILED"
        assert payload["error"]["details"]["failed_device"] == "line-a"
        assert payload["error"]["details"]["rolled_back_devices"] == []
        # Nothing was written anywhere: line-a keeps only its pre-existing
        # keys, line-b's shadow was never created.
        assert fleet.iot_data.bindings["line-a"] == pre_existing
        assert "line-b" not in fleet.iot_data.bindings
        assert fleet.gg.create_deployment_calls == []
        assert deployment_records(fleet) == []


# --------------------------------------------------------------------------
# Confirmed-warnings path end to end (route wiring of 8.8/9.3 into 8.6)
# --------------------------------------------------------------------------

class TestWarningsConfirmedPath:
    WARNING_ID = "camera-degraded:line-a:n1:cfg-1:absent"

    def seed_degraded(self, fleet):
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {"absent": True,
                                                 "absent_since": 1}})
        fleet.gg.register_device("line-a")

    def test_unconfirmed_warning_rejects_before_any_shadow_write(
            self, fleet):
        self.seed_degraded(fleet)
        status, payload = fleet.deploy(
            ["line-a"],
            camera_bindings={"line-a": {"n1": {"cameraSourceId": "cfg-1"}}})
        assert status == 409
        assert payload["error"]["code"] == "CAMERA_WARNINGS_UNCONFIRMED"
        [warning] = payload["error"]["details"]["warnings"]
        assert warning["id"] == self.WARNING_ID
        assert warning["confirmed"] is False
        assert fleet.iot_data.bindings == {}
        assert fleet.gg.create_deployment_calls == []

    def test_confirmed_warning_succeeds_with_warnings_in_response(
            self, fleet):
        """With every warning id confirmed the deployment is created,
        bindings are delivered, and the 201 response echoes the warnings
        as confirmed."""
        self.seed_degraded(fleet)
        bindings = {"line-a": {"n1": {"cameraSourceId": "cfg-1"}}}

        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings,
                                       confirmed_warnings=[self.WARNING_ID])

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is True
        [warning] = payload["camera_warnings"]
        assert warning["id"] == self.WARNING_ID
        assert warning["confirmed"] is True
        assert fleet.iot_data.bindings["line-a"] == {
            f"{fleet.workflow_id}/1": bindings["line-a"]}
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["camera_bindings"] == bindings


# --------------------------------------------------------------------------
# Binding storage on the deployment record (Requirement 12.3)
# --------------------------------------------------------------------------

class TestBindingRecordStorage:
    def test_route_level_override_round_trips_shadow_and_record(self, fleet):
        """A manual override with numeric values passes constraint
        validation, is delivered to the shadow, and is stored on the
        deployment record (numbers come back as DynamoDB Decimals equal
        to the submitted values)."""
        fleet.seed_workflow([camera_node()])
        fleet.seed_registry("line-a", {"cfg-1": {}})
        fleet.gg.register_device("line-a")
        # icam_source declares only the device path; the override passes
        # constraint validation and round-trips through shadow and record.
        override = {"device": "/dev/video9"}
        bindings = {"line-a": {"n1": {"override": override}}}

        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings)

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is True
        assert fleet.iot_data.bindings["line-a"] == {
            f"{fleet.workflow_id}/1": bindings["line-a"]}
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        stored = record["camera_bindings"]["line-a"]["n1"]["override"]
        assert stored == override
        assert stored["device"] == "/dev/video9"

    def test_record_storage_is_decimal_safe_for_float_override_values(
            self, fleet):
        """record_workflow_deployment stores float override values (e.g.
        a camera-backed Custom_Node_Type's float parameter) through
        _dynamo_safe: put_item accepts them and they come back as
        Decimals — DynamoDB rejects raw Python floats."""
        deployment_id = f"dep-{uuid.uuid4()}"
        bindings = {"line-a": {"n1": {"override": {"device": "/dev/video1",
                                                   "gain": 2.5}}}}

        fleet.deployments.record_workflow_deployment(
            deployment_id, fleet.usecase_id, fleet.workflow_id, 1,
            "arn:aws:iot:us-east-1:123456789012:thing/line-a",
            ["line-a"], None, False, None, fleet.user,
            camera_bindings=bindings)

        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": deployment_id})["Item"]
        stored = record["camera_bindings"]["line-a"]["n1"]["override"]
        assert stored["gain"] == Decimal("2.5")
        assert isinstance(stored["gain"], Decimal)

    def test_version_without_camera_nodes_stores_no_bindings_attribute(
            self, fleet):
        """A version with no Camera_Input_Nodes deploys without bindings:
        camera_bindings_delivered is false, no shadow is touched, and the
        deployment record carries no camera_bindings attribute."""
        fleet.seed_workflow(None)
        fleet.gg.register_device("line-a")

        status, payload = fleet.deploy(["line-a"])

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is False
        assert fleet.iot_data.bindings == {}
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert "camera_bindings" not in record


# --------------------------------------------------------------------------
# Aravis binding delivery (aravis-camera-input task 8.4 — Requirement 5.5)
# --------------------------------------------------------------------------

class TestAravisBindingDelivery:
    def test_aravis_binding_writes_shadow_and_leaves_artifact_untouched(
            self, fleet):
        """A submission binding an aravis_camera_source node to a
        registered AravisDiscovered Camera_Source writes the expected
        desired document into the target's dda-camera-bindings shadow —
        keyed {workflowId}/{version}, carrying the node's binding — and
        leaves the packaged artifact untouched: the Greengrass deployment
        references only the component, and the staged artifact bytes are
        byte-identical after submission (5.5)."""
        aravis_node = {"node_id": "arv1", "node_type": "aravis_camera_source"}
        fleet.seed_workflow([aravis_node])
        fleet.seed_registry("line-a", {"arv-1": {
            "type": "AravisDiscovered",
            "origin": "edge-discovered",
            "params": {"cameraId": "Aravis-Fake-GV01", "serial": "GV01",
                       "protocol": "Fake", "address": "0.0.0.0"},
        }})
        fleet.gg.register_device("line-a")

        # The packaged Workflow_Component artifact as staged in portal S3
        # by the Component_Packager — bindings ride the shadow, never the
        # artifact.
        artifact_key = f"workflows/{fleet.workflow_id}/1/arm64_jp5/component.zip"
        artifact_bytes = b"compiled-workflow-artifact-bytes"
        fleet.env.s3.put_object(Bucket=fleet.env.bucket, Key=artifact_key,
                                Body=artifact_bytes)

        bindings = {"line-a": {"arv1": {"cameraSourceId": "arv-1"}}}
        status, payload = fleet.deploy(["line-a"], camera_bindings=bindings)

        assert status == 201, payload
        assert payload["camera_bindings_delivered"] is True
        # The expected desired document (unchanged shadow mechanism):
        # desired.bindings["{workflowId}/{version}"] = node bindings.
        assert fleet.iot_data.bindings["line-a"] == {
            f"{fleet.workflow_id}/1": {"arv1": {"cameraSourceId": "arv-1"}}}
        # The Greengrass deployment carries only the component map — no
        # binding data rides the component set.
        [call] = fleet.gg.create_deployment_calls
        assert call["components"] == {
            f"dda.workflow.{fleet.workflow_id}": {"componentVersion": "1.0.0"}}
        # The staged artifact bytes are untouched.
        stored = fleet.env.s3.get_object(
            Bucket=fleet.env.bucket, Key=artifact_key)["Body"].read()
        assert stored == artifact_bytes
        # The delivered bindings are recorded on the deployment record.
        record = fleet.env.stack.tables.deployments.get_item(
            Key={"deployment_id": payload["deployment_id"]})["Item"]
        assert record["camera_bindings"] == bindings
