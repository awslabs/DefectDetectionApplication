"""
Preservation tests for task 29's Portal changes (rtsp-rtmp-stream-cameras
task 29, plan item 10).

Behavior that tasks 29.4 and 29.5 must keep exactly as it is at
``2ae4645``. Written before the fix, these tests PASS on the base and
must still pass on the fixed code:

- A ``cfg-`` camera, RTSP and ``Camera``, with a stale update failure: the
  failure keeps the entry ``failed``, still pins it at the Portal's own
  documents event after a Portal delete (pending, no ConflictEvent), and
  the device's 404 for the delete marks it ``failed`` (Requirements 5.12,
  18.3).
- ``disc-`` and ``arv-`` entries pending a Portal delete, RTSP and
  ``Camera``: an earlier change's failure pins them, and the device's
  ``discovery-managed`` refusal of the delete marks them ``failed``.
- Entries that are not pending a delete (pending an update, pending a
  create, synced) are not deleted by a report that omits them while it
  holds a failure from another change; a failure for the current change
  marks its entry failed; a failure without a ``portalChangeId`` keeps a
  pending delete of ``portal-q``.
- Non-stream create, update and delete never touch the Credential_Vault
  and never resolve the use case's account (Requirement 18.3).
- A plain edit of a ``cfg-`` stream camera that reports a
  ``credentialRef`` with ``credentialsConfigured: true`` delivers that
  reference again (Requirement 5.8).

Self-contained (plan decision D4): it imports nothing but conftest, so
``bin/run_on_base.sh`` runs it unchanged against the base worktree. Its
own tables, the real routes over the moto stack, and the real reducer
(``camera_registry.camera_sync._process_report``).

Requirements: 5.8, 5.12, 18.3
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-f2122-pres"
SETTINGS_TABLE_NAME = "test-settings-f2122-pres"
DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"
SHADOW_NAME = "dda-camera-registry"
URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
REASON_404 = "Image source not found"
REASON_DISCOVERY_MANAGED = "discovery-managed"


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
        credentials=camera_registry.stream_credentials,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        iot=boto3.client("iot", region_name=REGION),
        iot_data=boto3.client("iot-data", region_name=REGION),
    )


@pytest.fixture
def device(camera_env, env):
    """A registered device of a fresh Use_Case, with its IoT thing so
    shadow writes succeed, and an Operator."""
    usecase_id = env.create_usecase()
    device_id = f"thing-f2122p-{uuid.uuid4().hex[:12]}"
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


def items_of(camera_env, device_id):
    return {item["sk"]: item for item in camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id}).get("Items", [])}


def camera_item(camera_env, device_id, csid):
    return items_of(camera_env, device_id).get(f"CAMERA#{csid}")


def conflicts(camera_env, device_id):
    return [item for sk, item in items_of(camera_env, device_id).items()
            if sk.startswith("CONFLICT#")]


def params_for(source_type):
    return ({"url": URL, "transport": "tcp"} if source_type == "RTSP"
            else {"devicePath": "/dev/video0"})


def seed(camera_env, device, csid, source_type, **attributes):
    """A registry camera item, stored as the routes or the reducer leave
    it."""
    item = {
        "device_id": device.device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": device.usecase_id,
        "name": f"camera {csid}", "type": source_type,
        "params": params_for(source_type), "capabilities": {},
        "origin": "portal-created", "version": 0, "absent": False,
        "sync_status": "synced",
    }
    item.update(attributes)
    camera_env.registry.put_item(Item=item)
    return item


def reduce(camera_env, device, cameras, failures=None,
           reported_at=1_730_000_000_000):
    """Reduce one merged reported state with the real reducer."""
    reported = {"schemaVersion": 1, "reportedAt": reported_at,
                "cameras": cameras}
    if failures is not None:
        reported["failures"] = failures
    camera_env.module.camera_sync._process_report(
        device.device_id, reported, usecase_id=device.usecase_id)


# ---------------------------------------------------------------------------
# cfg- and discovery-managed entries keep today's rule (5.12, 18.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
def test_cfg_camera_with_a_stale_update_failure_keeps_todays_rule(
        camera_env, device, source_type):
    seed(camera_env, device, "cfg-z", source_type,
         origin="edge-configured", version=3,
         last_reported_at=1_700_000_000_000)
    edited = dict(params_for(source_type))
    edited.update({"latencyMs": 400} if source_type == "RTSP"
                  else {"gain": 8})
    status, body = invoke(camera_env, "PUT", device, sub_path="/cfg-z",
                          body={"name": "edited", "type": source_type,
                                "params": edited})
    assert status == 200, body
    update_id = body["portal_change_id"]

    # The update failed with a 404 (the camera was deleted at the station),
    # and the device's report retired the camera key; the failure stays in
    # the merged shadow, so it is in every later documents event.
    stale = {"cfg-z": {"reason": REASON_404, "portalChangeId": update_id}}
    reduce(camera_env, device, {}, stale, reported_at=1_730_000_001_000)
    reduce(camera_env, device, {}, stale, reported_at=1_730_000_002_000)
    entry = camera_item(camera_env, device.device_id, "cfg-z")
    assert entry["sync_status"] == "failed"
    assert entry["failure_reason"] == REASON_404

    # A Portal delete: at the Portal's own documents event the stale
    # failure still pins the entry.
    status, body = invoke(camera_env, "DELETE", device, sub_path="/cfg-z")
    assert status == 200, body
    delete_id = body["portal_change_id"]
    reduce(camera_env, device, {}, stale, reported_at=1_730_000_003_000)
    entry = camera_item(camera_env, device.device_id, "cfg-z")
    assert entry["sync_status"] == "pending"
    assert entry["pending_content"] == {"op": "delete"}
    assert conflicts(camera_env, device.device_id) == []

    # The device has no such Image_Source: its 404 for the delete marks the
    # entry failed, exactly as before task 29.
    reduce(camera_env, device, {},
           {"cfg-z": {"reason": REASON_404, "portalChangeId": delete_id}},
           reported_at=1_730_000_004_000)
    entry = camera_item(camera_env, device.device_id, "cfg-z")
    assert entry["sync_status"] == "failed"
    assert entry["failure_reason"] == REASON_404
    assert entry["portal_change_id"] == delete_id


@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
@pytest.mark.parametrize("csid", ["disc-x", "arv-x"])
def test_discovery_managed_entry_pending_a_delete_stays_pinned(
        camera_env, device, csid, source_type):
    # As mark_pending leaves an entry a Portal delete marked pending.
    seed(camera_env, device, csid, source_type, sync_status="pending",
         portal_change_id="pc-delete", pending_content={"op": "delete"})
    earlier = {csid: {"reason": REASON_DISCOVERY_MANAGED,
                      "portalChangeId": "pc-earlier"}}

    reduce(camera_env, device, {}, earlier, reported_at=1_730_000_001_000)
    entry = camera_item(camera_env, device.device_id, csid)
    assert entry is not None, "an earlier change's failure no longer pins it"
    assert entry["sync_status"] == "pending"
    assert conflicts(camera_env, device.device_id) == []

    reduce(camera_env, device, {},
           {csid: {"reason": REASON_DISCOVERY_MANAGED,
                   "portalChangeId": "pc-delete"}},
           reported_at=1_730_000_002_000)
    entry = camera_item(camera_env, device.device_id, csid)
    assert entry["sync_status"] == "failed"
    assert entry["failure_reason"] == REASON_DISCOVERY_MANAGED


# ---------------------------------------------------------------------------
# Every entry that is not pending a delete keeps today's rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
def test_failures_from_other_changes_keep_entries_not_pending_a_delete(
        camera_env, device, source_type):
    seed(camera_env, device, "portal-u", source_type, sync_status="pending",
         portal_change_id="pc-u2", version=2,
         pending_content={"op": "update", "name": "edited",
                          "type": source_type,
                          "params": params_for(source_type)})
    seed(camera_env, device, "portal-c", source_type, sync_status="pending",
         portal_change_id="pc-c2",
         pending_content={"op": "create", "name": "camera portal-c",
                          "type": source_type,
                          "params": params_for(source_type)})
    seed(camera_env, device, "portal-s", source_type, version=4,
         origin="edge-configured", last_reported_at=1_700_000_000_000)
    failures = {csid: {"reason": "an earlier change failed",
                       "portalChangeId": f"pc-{csid}-earlier"}
                for csid in ("portal-u", "portal-c", "portal-s")}

    before = {csid: camera_item(camera_env, device.device_id, csid)
              for csid in failures}
    reduce(camera_env, device, {}, failures)

    for csid, item in before.items():
        assert camera_item(camera_env, device.device_id, csid) == item, csid
    assert conflicts(camera_env, device.device_id) == []


@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
def test_a_failure_for_the_current_change_marks_the_entry_failed(
        camera_env, device, source_type):
    seed(camera_env, device, "portal-f", source_type, sync_status="pending",
         portal_change_id="pc-f", version=2,
         pending_content={"op": "update", "name": "edited",
                          "type": source_type,
                          "params": params_for(source_type)})
    reduce(camera_env, device, {},
           {"portal-f": {"reason": "location is required",
                         "portalChangeId": "pc-f"}})
    entry = camera_item(camera_env, device.device_id, "portal-f")
    assert entry["sync_status"] == "failed"
    assert entry["failure_reason"] == "location is required"


@pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
def test_a_failure_without_a_change_id_keeps_a_pending_delete(
        camera_env, device, source_type):
    seed(camera_env, device, "portal-q", source_type, sync_status="pending",
         portal_change_id="pc-q", pending_content={"op": "delete"})
    before = camera_item(camera_env, device.device_id, "portal-q")
    reduce(camera_env, device, {},
           {"portal-q": {"reason": "applied by hand at the station"}})
    assert camera_item(camera_env, device.device_id, "portal-q") == before


# ---------------------------------------------------------------------------
# Non-stream requests (18.3) and the carry-forward (5.8)
# ---------------------------------------------------------------------------

def test_non_stream_requests_never_touch_the_vault_or_resolve_the_account(
        camera_env, device, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a non-stream request reached the vault")

    for name in ("ensure_device_read_grant", "store_stream_credentials",
                 "schedule_secret_deletion", "_usecase_account_id"):
        monkeypatch.setattr(camera_env.credentials, name, forbidden)

    status, body = invoke(camera_env, "POST", device, body={
        "name": "USB", "type": "Camera", "camera_source_id": "usb-1",
        "params": {"devicePath": "/dev/video2"},
        "credentials": {"password": "not-a-stream-credential"}})
    assert status == 201, body
    status, body = invoke(camera_env, "PUT", device, sub_path="/usb-1",
                          body={"name": "USB", "type": "Camera",
                                "params": {"devicePath": "/dev/video3"}})
    assert status == 200, body
    status, body = invoke(camera_env, "DELETE", device, sub_path="/usb-1")
    assert status == 200, body
    assert shadow_changes(camera_env, device.device_id)["usb-1"][
        "op"] == "delete"


def test_plain_edit_carries_a_reported_reference_while_configured(
        camera_env, device):
    reference = {
        "secretArn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:"
                      f"dda-portal/stream-camera-credentials/"
                      f"{device.device_id}/portal-c0ffee-AbCdEf"),
        "versionId": "reported-version-1",
    }
    seed(camera_env, device, "cfg-c", "RTSP", origin="edge-configured",
         version=2, last_reported_at=1_700_000_000_000,
         params={"url": URL, "transport": "tcp", "credentialRef": reference,
                 "credentialsConfigured": True,
                 "credentialsUpdatedAt": 1_700_000_000_000})

    status, body = invoke(camera_env, "PUT", device, sub_path="/cfg-c",
                          body={"name": "renamed", "type": "RTSP",
                                "params": {"url": URL, "latencyMs": 300}})
    assert status == 200, body
    params = shadow_changes(camera_env, device.device_id)["cfg-c"]["params"]
    assert params["credentialRef"] == reference
    assert params["credentialsConfigured"] is True
    assert params["latencyMs"] == 300
