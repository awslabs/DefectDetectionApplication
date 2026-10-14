"""
Bug-condition exploration tests for findings 21 (c) and 22, Portal side
(rtsp-rtmp-stream-cameras task 29, plan item 9).

Written before the fix, to FAIL on the unfixed code at ``2ae4645`` on
their assertions, and to pass once tasks 29.4 and 29.5 are in:

- ``test_f22_deleting_a_rekeyed_camera_schedules_its_original_secret``
  (finding 22): a camera created with credentials as ``portal-x`` gets its
  secret ``.../portal-x``. The device creates it as ``cfg-y`` and mirrors
  it under ``portal-x`` for one report, which the next report retires.
  Deleting ``cfg-y`` used to schedule the deletion of ``.../cfg-y``, which
  does not exist, and left ``.../portal-x`` behind.
- ``test_f21c_deleting_a_failed_create_removes_it`` (finding 21 (c)), for a
  credential-free RTSP camera and a ``Camera``: a create of ``portal-y``
  failed on the device, and the failure stays in the merged shadow. After
  a Portal delete, the Portal's own documents event used to keep the entry
  pending forever, because the create's failure pinned it.

Self-contained (plan decision D4): it imports nothing but conftest, so
``bin/run_on_base.sh`` can run it unchanged against the base worktree. It
creates its own tables, drives the real ``camera_registry`` routes over
the moto stack (DynamoDB, Secrets Manager, IAM, IoT data), and reduces
reports with the real reducer, ``camera_registry.camera_sync
._process_report``, as the refresh route does.

Requirements: 5.8, 5.12
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-f2122-bugs"
SETTINGS_TABLE_NAME = "test-settings-f2122-bugs"
DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"
SHADOW_NAME = "dda-camera-registry"
SECRET_PREFIX = "dda-portal/stream-camera-credentials"
URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
PASSWORD = "f2122-bugs-PWD-3e7a"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """This module's own registry and settings tables, the device
    token-exchange role, and a freshly imported camera_registry."""
    import boto3

    os.environ["CAMERA_REGISTRY_TABLE"] = CAMERA_REGISTRY_TABLE_NAME
    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME

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
            {"AttributeName": "usecase_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "setting_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    iam = boto3.client("iam", region_name=REGION)
    try:
        iam.create_role(
            RoleName=DEVICE_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "credentials.iot.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }],
            }),
        )
    except iam.exceptions.EntityAlreadyExistsException:
        pass

    sys.modules.pop("camera_registry", None)
    sys.modules.pop("stream_credentials", None)
    import camera_registry

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        iot=boto3.client("iot", region_name=REGION),
        iot_data=boto3.client("iot-data", region_name=REGION),
    )


@pytest.fixture
def device(camera_env, env):
    """A registered device of a fresh Use_Case (account 123456789012), with
    its IoT thing so shadow writes succeed, and an Operator."""
    usecase_id = env.create_usecase()
    device_id = f"thing-f2122b-{uuid.uuid4().hex[:12]}"
    camera_env.iot.create_thing(thingName=device_id)
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })
    return SimpleNamespace(device_id=device_id, usecase_id=usecase_id,
                           operator=env.make_user(role="Operator"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(method, device_id, user, sub_path="", body=None):
    path_parameters = {"id": device_id}
    if sub_path:
        path_parameters["csid"] = sub_path.lstrip("/")
    return {
        "httpMethod": method,
        "path": f"/devices/{device_id}/cameras{sub_path}",
        "pathParameters": path_parameters,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {
            "sub": user["user_id"],
            "email": user["email"],
            "cognito:username": user["username"],
            "custom:role": user["role"],
        }}},
    }


def invoke(camera_env, method, device, sub_path="", body=None):
    """(status, parsed body) of one route call by the device's Operator."""
    response = camera_env.module.handler(
        make_event(method, device.device_id, device.operator, sub_path,
                   body), None)
    return response["statusCode"], json.loads(response["body"])


def shadow_changes(camera_env, device_id):
    """desired.changes of the device's registry shadow ({} if none)."""
    try:
        response = camera_env.iot_data.get_thing_shadow(
            thingName=device_id, shadowName=SHADOW_NAME)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return {}
        raise
    document = json.loads(response["payload"].read())
    return (document.get("state") or {}).get("desired", {}).get(
        "changes") or {}


def camera_item(camera_env, device_id, csid):
    return camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"}).get("Item")


def describe(camera_env, name):
    """The secret's description, or None when it does not exist."""
    try:
        return camera_env.secrets.describe_secret(SecretId=name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def reduce(camera_env, device, cameras, failures=None,
           reported_at=1_730_000_000_000):
    """Reduce one merged reported state with the real reducer, as the SQS
    ingest and the refresh route do."""
    reported = {"schemaVersion": 1, "reportedAt": reported_at,
                "cameras": cameras}
    if failures is not None:
        reported["failures"] = failures
    camera_env.module.camera_sync._process_report(
        device.device_id, reported, usecase_id=device.usecase_id)


# ---------------------------------------------------------------------------
# Finding 22: the re-keyed camera's secret
# ---------------------------------------------------------------------------

def test_f22_deleting_a_rekeyed_camera_schedules_its_original_secret(
        camera_env, device):
    status, body = invoke(camera_env, "POST", device, body={
        "name": "Dock 3 overview", "type": "RTSP",
        "camera_source_id": "portal-x",
        "params": {"url": URL, "transport": "tcp"},
        "credentials": {"username": "viewer", "password": PASSWORD},
    })
    assert status == 201, body
    change = shadow_changes(camera_env, device.device_id)["portal-x"]
    reference = change["params"]["credentialRef"]
    change_id = change["portalChangeId"]
    original = f"{SECRET_PREFIX}/{device.device_id}/portal-x"
    assert describe(camera_env, original) is not None

    # The device applied the create under its own id, cfg-y, and mirrors it
    # under portal-x for one report, ack included; its params echo the
    # Credential_Reference (inventory.stream_params).
    created = {
        "version": 1, "name": "Dock 3 overview", "type": "RTSP",
        "origin": "edge-configured",
        "params": {"url": URL, "transport": "tcp", "latencyMs": 200,
                   "decoder": "auto", "maxFrameDimension": 1920,
                   "stallTimeoutS": 10, "credentialRef": reference,
                   "credentialsConfigured": True,
                   "credentialsUpdatedAt": change["params"][
                       "credentialsUpdatedAt"]},
        "capabilities": {}, "ack": change_id,
    }
    reduce(camera_env, device, {"cfg-y": created, "portal-x": dict(created)},
           reported_at=1_730_000_001_000)
    # The next report retires the mirror (the device nulls the key, so the
    # merged state holds cfg-y only).
    reduce(camera_env, device, {"cfg-y": created},
           reported_at=1_730_000_002_000)
    assert camera_item(camera_env, device.device_id, "portal-x") is None
    assert camera_item(camera_env, device.device_id, "cfg-y")[
        "sync_status"] == "synced"

    status, body = invoke(camera_env, "DELETE", device, sub_path="/cfg-y")
    assert status == 200, body

    description = describe(camera_env, original)
    assert description is not None
    assert description.get("DeletedDate") is not None, (
        "deleting the re-keyed cfg-y left its original secret "
        ".../portal-x without a deletion date")
    assert describe(
        camera_env, f"{SECRET_PREFIX}/{device.device_id}/cfg-y") is None


# ---------------------------------------------------------------------------
# Finding 21 (c): the failed create the device never created
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
def test_f21c_deleting_a_failed_create_removes_it(camera_env, device,
                                                  source_type):
    params = ({"url": URL} if source_type == "RTSP"
              else {"devicePath": "/dev/video0"})
    status, body = invoke(camera_env, "POST", device, body={
        "name": "Failed create", "type": source_type,
        "camera_source_id": "portal-y", "params": params,
    })
    assert status == 201, body
    create_change_id = body["portal_change_id"]

    # The device failed the create; the failure stays in the merged shadow.
    failures = {"portal-y": {"reason": "the create failed on the device",
                             "portalChangeId": create_change_id}}
    reduce(camera_env, device, {}, failures, reported_at=1_730_000_001_000)
    assert camera_item(camera_env, device.device_id, "portal-y")[
        "sync_status"] == "failed"

    status, body = invoke(camera_env, "DELETE", device, sub_path="/portal-y")
    assert status == 200, body
    pending = camera_item(camera_env, device.device_id, "portal-y")
    assert pending["sync_status"] == "pending"
    assert pending["pending_content"] == {"op": "delete"}

    # The Portal's own documents event: the same merged state, which still
    # holds the create's failure and no camera key for portal-y.
    reduce(camera_env, device, {}, failures, reported_at=1_730_000_002_000)
    leftover = camera_item(camera_env, device.device_id, "portal-y")
    assert leftover is None, (
        "the create's failure kept the deleted entry: "
        f"sync_status={leftover.get('sync_status')!r}")
