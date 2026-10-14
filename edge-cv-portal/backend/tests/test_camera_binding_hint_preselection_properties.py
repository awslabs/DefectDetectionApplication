"""
Property-based test for deploy-time binding hint pre-selection —
get_camera_binding_context (functions/deployments.py).

**Feature: camera-registry-sync, Property 18: Binding hint pre-selection**

*For any* Camera_Input_Node hint and any device registry snapshot, the
proposed pre-selection equals the registry entry whose id matches the
hint when such an entry exists, and is empty otherwise.

**Validates: Requirements 8.5**

Task 11.8 (spec: camera-registry-sync). Exercised route-level through
the binding-context view against the moto-backed registry table; the
example-based unit tests over the same endpoint live in
test_camera_binding_context.py (task 11.7).

Every hypothesis example seeds its own workflow version and uniquely
named target devices into DynamoDB (the module-scoped Use_Case and user
are shared), so generated sizes are kept small: at most 3 nodes, 3
devices, and 4 cameras per device.
"""
import json
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION, WorkflowStoreEnv

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-hint-preselection"
ACCOUNT_ID = "123456789012"


# ---------------------------------------------------------------------------
# Module import with the camera registry table in place
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def harness(aws_stack):
    """Camera registry table plus deployments imported against it, and a
    module-scoped Use_Case with a deploy-capable user shared by every
    generated example. Examples isolate from one another through fresh
    uuid-based workflow and device ids, never table truncation."""
    import os

    import boto3

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

    env = WorkflowStoreEnv(aws_stack)
    resource = boto3.resource("dynamodb", region_name=REGION)
    return SimpleNamespace(
        deployments=deployments,
        env=env,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        user=env.make_user(role="UseCaseAdmin"),
        usecase_id=env.create_usecase(),
    )


# ---------------------------------------------------------------------------
# Per-example seeding and invocation
# ---------------------------------------------------------------------------

def _seed_workflow(harness, workflow_id, camera_nodes):
    harness.env.stack.tables.workflows.put_item(Item={
        "workflow_id": workflow_id,
        "usecase_id": harness.usecase_id,
        "name": "camera workflow",
        "latest_version": 1,
    })
    harness.env.stack.tables.versions.put_item(Item={
        "workflow_id": workflow_id,
        "version": 1,
        "validation_status": {"status": "passed", "validated_at": 1,
                              "findings": []},
        "component_arn": f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:"
                         f"components:wf:versions:1",
        "has_binding_points": True,
        "camera_input_nodes": camera_nodes,
    })


def _seed_registry(harness, thing_name, camera_source_ids):
    now_ms = int(time.time() * 1000)
    harness.registry.put_item(Item={
        "device_id": thing_name, "sk": "META",
        "usecase_id": harness.usecase_id,
        "never_synced": False, "last_report_at": now_ms,
    })
    for csid in camera_source_ids:
        harness.registry.put_item(Item={
            "device_id": thing_name, "sk": f"CAMERA#{csid}",
            "camera_source_id": csid, "usecase_id": harness.usecase_id,
            "name": f"cam {csid}", "type": "Camera",
            "params": {"devicePath": "/dev/video0"},
            "capabilities": {}, "origin": "edge-configured",
            "version": 1, "sync_status": "synced", "absent": False,
            "last_reported_at": now_ms,
        })


def _binding_context(harness, workflow_id, device_names):
    query = {"view": "binding-context", "usecase_id": harness.usecase_id,
             "workflow_id": workflow_id,
             "target_devices": ",".join(device_names)}
    event = harness.env.event("GET", "/deployments", harness.user,
                              query=query)
    response = harness.deployments.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_NODE_IDS = ["n1", "n2", "n3"]
_CSID_POOL = ["cfg-1", "cfg-2", "cfg-3", "cfg-4"]


@st.composite
def _hint_cases(draw):
    """A workflow version's Camera_Input_Nodes — each carrying a hint
    referencing a Camera_Source id, a degenerate hint without an id, or
    no hint at all — alongside per-device registry snapshots: devices
    registering arbitrary subsets of the id pool (possibly none of the
    hinted ids, possibly an empty registry) and devices never seen by
    the registry at all."""
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    nodes = []
    hinted = {}  # node_id -> hinted cameraSourceId (only id-carrying hints)
    for node_id in node_ids:
        node = {
            "node_id": node_id,
            "node_type": draw(st.sampled_from(
                ["camera_source", "acme.cam_input"])),
            "compiled_device_paths": {"x86_64": "/dev/video0"},
        }
        hint_kind = draw(st.sampled_from(["none", "id", "no-id"]))
        if hint_kind == "id":
            csid = draw(st.sampled_from(_CSID_POOL))
            node["binding_hint"] = {"cameraSourceId": csid,
                                    "cameraName": f"cam {csid}",
                                    "sourceDeviceId": "authoring-device"}
            hinted[node_id] = csid
        elif hint_kind == "no-id":
            # Advisory hint that names no Camera_Source id: never
            # pre-selects anything.
            node["binding_hint"] = {"cameraName": "advisory only"}
        nodes.append(node)

    # Per device: None means the device has never reported (no registry
    # rows at all); a list is the device's registered Camera_Source ids
    # (possibly empty).
    device_cameras = draw(st.lists(
        st.one_of(st.none(),
                  st.lists(st.sampled_from(_CSID_POOL),
                           unique=True, max_size=4)),
        min_size=1, max_size=3))
    return nodes, hinted, device_cameras


# ---------------------------------------------------------------------------
# Property 18
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_hint_cases())
def test_preselection_is_exactly_the_hint_matches(harness, case):
    """**Feature: camera-registry-sync, Property 18: Binding hint
    pre-selection**

    **Validates: Requirements 8.5**

    For every target Edge_Device, the binding context's proposed
    pre-selection maps a Camera_Input_Node to its hinted cameraSourceId
    exactly when that id is present in the device's Camera_Registry, and
    proposes nothing for that node otherwise — including devices that
    register other sources, devices with empty registries, never-synced
    devices, hintless nodes, and hints naming no Camera_Source id.
    """
    nodes, hinted, device_cameras = case

    workflow_id = f"wf-{uuid.uuid4()}"
    _seed_workflow(harness, workflow_id, nodes)
    stamp = uuid.uuid4().hex[:8]
    device_names = []
    for index, cameras in enumerate(device_cameras):
        thing_name = f"line-{stamp}-{index}"
        device_names.append(thing_name)
        if cameras is not None:
            _seed_registry(harness, thing_name, cameras)

    status, payload = _binding_context(harness, workflow_id, device_names)

    assert status == 200, payload
    assert set(payload["targets"]) == set(device_names)
    for thing_name, cameras in zip(device_names, device_cameras):
        target = payload["targets"][thing_name]
        registered = set(cameras or [])
        expected = {node_id: csid for node_id, csid in hinted.items()
                    if csid in registered}
        # Pre-selection is exactly the hint matches present in THIS
        # device's registry — empty when no hinted id is registered.
        assert target["preselected"] == expected
        # Coherence with 8.1: every proposed id is among the device's
        # selectable Camera_Source options.
        options = {c["camera_source_id"] for c in target["cameras"]}
        assert options == registered
        assert set(expected.values()) <= options
