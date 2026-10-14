"""DELETE /devices/{id}: removing a device from DDA (devices.delete_device).

Harness: the established devices.py pattern (test_model_status_devices_read)
- the module is imported inside the moto-backed ``aws_stack`` session
fixture. IoT, the IoT data plane and DynamoDB are moto; moto does not
implement greengrassv2, so the core devices live in ``FakeGreengrass``.
The use case is single-account (root ARN), so every client the handler
builds resolves to moto through ``create_boto3_client``'s default
credentials; the factory is patched only to hand out the fake Greengrass
and to wrap IoT clients for fault injection.

Registry enforcement is ON (``PORTAL_REGISTRY_ENFORCED=true``, as deployed):
every acting user holds a real Portal_Identity row.
"""
import json
import sys
import uuid

import boto3
import pytest
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
THING_POLICY = "GreengrassV2IoTThingPolicy"

CAMERA_REGISTRY_TABLE = "test-device-delete-camera-registry"
REGISTRATIONS_TABLE = "test-device-delete-registrations"
ACCOUNT_SYNC_TABLE = "test-device-delete-account-sync"

TAG_MANAGED = {"dda-portal:managed": "true"}


def _client_error(code, message, operation):
    return ClientError({"Error": {"Code": code, "Message": message}},
                       operation)


def _create_table_once(client, **kwargs):
    try:
        client.create_table(BillingMode="PAY_PER_REQUEST", **kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise


@pytest.fixture(scope="module")
def devices(aws_stack):
    """devices.py imported inside the moto mock with the removal tables
    configured (module-level env reads)."""
    import os

    ddb = boto3.client("dynamodb", region_name=REGION)
    _create_table_once(
        ddb, TableName=CAMERA_REGISTRY_TABLE,
        KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "device_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}])
    _create_table_once(
        ddb, TableName=REGISTRATIONS_TABLE,
        KeySchema=[{"AttributeName": "registration_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "registration_id", "AttributeType": "S"},
            {"AttributeName": "usecase_id", "AttributeType": "S"},
            {"AttributeName": "device_name", "AttributeType": "S"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "usecase-device-index",
            "KeySchema": [{"AttributeName": "usecase_id", "KeyType": "HASH"},
                          {"AttributeName": "device_name", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}}])
    _create_table_once(
        ddb, TableName=ACCOUNT_SYNC_TABLE,
        KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "device_id", "AttributeType": "S"}])

    iot = boto3.client("iot", region_name=REGION)
    try:
        iot.create_policy(policyName=THING_POLICY, policyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "iot:Connect",
                           "Resource": "*"}]}))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    env = {"CAMERA_REGISTRY_TABLE": CAMERA_REGISTRY_TABLE,
           "REGISTRATIONS_TABLE": REGISTRATIONS_TABLE,
           "ACCOUNT_SYNC_TABLE": ACCOUNT_SYNC_TABLE}
    previous = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        for module_name in ("devices", "deployments", "workflow_guards"):
            sys.modules.pop(module_name, None)
        import devices
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return devices


class FakeGreengrass:
    """greengrassv2 stand-in: core devices keyed by thing name, with tags."""

    def __init__(self):
        self.core_devices = {}
        self.tag_lookups = []
        self.delete_error = None
        # What the live service answers for a missing coreDevices ARN
        # (not the documented ResourceNotFoundException).
        self.tags_not_found_code = "NotFoundException"

    def list_tags_for_resource(self, resourceArn):
        self.tag_lookups.append(resourceArn)
        name = resourceArn.rsplit(":coreDevices:", 1)[-1]
        if name not in self.core_devices:
            raise _client_error(self.tags_not_found_code,
                                "Resource was not found", "ListTagsForResource")
        return {"tags": dict(self.core_devices[name])}

    def delete_core_device(self, coreDeviceThingName):
        if self.delete_error is not None:
            raise self.delete_error
        if coreDeviceThingName not in self.core_devices:
            raise _client_error("ResourceNotFoundException",
                                "core device not found", "DeleteCoreDevice")
        del self.core_devices[coreDeviceThingName]
        return {}


class ClientProxy:
    """A client with some methods replaced. Each override is called as
    override(wrapped_client, **kwargs)."""

    def __init__(self, client, overrides):
        self._client = client
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            override = self._overrides[name]
            return lambda **kwargs: override(self._client, **kwargs)
        return getattr(self._client, name)


def _delete_certificate_like_iot(client, **kwargs):
    """moto fidelity shim. AWS IoT's forceDelete drops the certificate's
    policy attachments with it; moto 5.x deletes the certificate but keeps
    its principal_policies entries, and every later delete_certificate in
    the session then fails with ResourceNotFoundException while it scans
    them. Detach the policies first, which is what IoT does."""
    if kwargs.get("forceDelete"):
        arn = client.describe_certificate(
            certificateId=kwargs["certificateId"]
        )["certificateDescription"]["certificateArn"]
        for policy in client.list_attached_policies(target=arn)["policies"]:
            client.detach_policy(policyName=policy["policyName"], target=arn)
    return client.delete_certificate(**kwargs)


class Env:
    def __init__(self, devices, aws_stack):
        self.devices = devices
        self.greengrass = FakeGreengrass()
        self.iot_overrides = {}
        self.iot = boto3.client("iot", region_name=REGION)
        self.iot_data = boto3.client("iot-data", region_name=REGION)
        resource = boto3.resource("dynamodb", region_name=REGION)
        self.tables = aws_stack.tables
        self.camera_registry = resource.Table(CAMERA_REGISTRY_TABLE)
        self.registrations = resource.Table(REGISTRATIONS_TABLE)
        self.account_sync = resource.Table(ACCOUNT_SYNC_TABLE)
        self.usecase_id = f"uc-{uuid.uuid4().hex[:8]}"
        self.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Device delete test",
            "account_id": ACCOUNT_ID,
            "region": REGION,
            "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
            "external_id": "ext-id",
        })

    def client_factory(self, service, credentials=None, region=None):
        if service == "greengrassv2":
            return self.greengrass
        client = boto3.client(service, region_name=region or REGION)
        if service != "iot":
            return client
        # Test overrides wrap the shimmed client, so an override that
        # delegates still gets IoT's forceDelete behavior.
        client = ClientProxy(
            client, {"delete_certificate": _delete_certificate_like_iot})
        if self.iot_overrides:
            return ClientProxy(client, self.iot_overrides)
        return client

    def user(self, role):
        user_id = f"user-{uuid.uuid4().hex[:8]}"
        self.tables.user_roles.put_item(Item={
            "user_id": user_id, "usecase_id": "global", "role": role,
            "status": "enabled", "username": user_id,
            "email": f"{user_id}@example.com", "assigned_at": 0,
            "assigned_by": "test_device_delete"})
        return {"user_id": user_id, "email": f"{user_id}@example.com",
                "username": user_id, "role": role}

    def seed_device(self, tags=TAG_MANAGED, name=None):
        """A provisioned DDA station: thing + certificate + policy,
        classic and named shadows, a core device, and every portal row."""
        name = name or f"station-{uuid.uuid4().hex[:8]}"
        self.iot.create_thing(thingName=name)
        cert = self.iot.create_keys_and_certificate(setAsActive=True)
        self.iot.attach_policy(policyName=THING_POLICY,
                               target=cert["certificateArn"])
        self.iot.attach_thing_principal(thingName=name,
                                        principal=cert["certificateArn"])
        state = json.dumps({"state": {"reported": {"ok": True}}}).encode()
        self.iot_data.update_thing_shadow(thingName=name, payload=state)
        for shadow in ("dda-camera-registry", "dda-model-status"):
            self.iot_data.update_thing_shadow(
                thingName=name, shadowName=shadow, payload=state)
        self.greengrass.core_devices[name] = dict(tags)

        self.tables.devices.put_item(Item={
            "device_id": name, "usecase_id": self.usecase_id,
            "target_architecture": "arm64_jp6"})
        for sk in ("META", "CAMERA#cam-1", "PIN_REQUEST#00000000000001#abcd"):
            self.camera_registry.put_item(Item={
                "device_id": name, "sk": sk, "usecase_id": self.usecase_id})
        self.account_sync.put_item(Item={
            "device_id": name, "pendingChanges": True, "status": "failed"})
        self.registrations.put_item(Item={
            "registration_id": f"reg-{uuid.uuid4().hex[:8]}",
            "usecase_id": self.usecase_id, "device_name": name,
            "device_group": "Line3", "status": "completed"})
        return name, cert

    def remove(self, name, user, delete_thing=None, event=None):
        query = {"usecase_id": self.usecase_id}
        if delete_thing is not None:
            query["delete_thing"] = delete_thing
        response = self.devices.delete_device(name, user, query, event)
        return response["statusCode"], json.loads(response["body"])

    def audit_entries(self, name):
        items = self.tables.audit_log.scan(
            FilterExpression=Attr("resource_id").eq(name)
            & Attr("action").eq("delete_device"))["Items"]
        return sorted(items, key=lambda item: item["timestamp"])

    def portal_rows(self, name):
        registrations = self.registrations.scan(
            FilterExpression=Attr("device_name").eq(name))["Items"]
        return {
            "device": self.tables.devices.get_item(
                Key={"device_id": name}).get("Item"),
            "camera_registry": self.camera_registry.query(
                KeyConditionExpression="device_id = :d",
                ExpressionAttributeValues={":d": name})["Items"],
            "account_sync": self.account_sync.get_item(
                Key={"device_id": name}).get("Item"),
            "registrations": registrations,
        }

    def thing_exists(self, name):
        try:
            self.iot.describe_thing(thingName=name)
            return True
        except ClientError as e:
            assert e.response["Error"]["Code"] == "ResourceNotFoundException"
            return False

    def certificate_status(self, certificate_id):
        try:
            return self.iot.describe_certificate(
                certificateId=certificate_id)["certificateDescription"]["status"]
        except ClientError as e:
            assert e.response["Error"]["Code"] == "ResourceNotFoundException"
            return None


@pytest.fixture
def env(devices, aws_stack, monkeypatch):
    monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    environment = Env(devices, aws_stack)
    monkeypatch.setattr(devices, "create_boto3_client",
                        environment.client_factory)
    monkeypatch.setattr(devices, "DETACH_PROPAGATION_DELAYS", (0, 0, 0, 0))
    return environment


# ---------------------------------------------------------------------------
# Remove from DDA (default): core device + portal rows; IoT identity kept
# ---------------------------------------------------------------------------

def test_remove_deletes_core_device_and_portal_rows_but_keeps_iot_thing(env):
    name, cert = env.seed_device()
    other, _ = env.seed_device()
    # Same device name registered under another use case (same account):
    # not this use case's registration, so it stays.
    env.registrations.put_item(Item={
        "registration_id": f"reg-{uuid.uuid4().hex[:8]}",
        "usecase_id": "uc-elsewhere", "device_name": name,
        "device_group": "Line9", "status": "completed"})

    status, body = env.remove(name, env.user("Operator"))

    assert status == 200, body
    assert body["deleted"] is True
    assert body["delete_thing"] is False
    assert body["thing_deleted"] is False
    assert body["certificates_deleted"] == []
    assert body["warnings"] == []
    assert body["portal_records_deleted"] == {
        "device_record": 1, "camera_registry": 3, "account_sync": 1,
        "registrations": 1}
    assert env.greengrass.tag_lookups == [
        f"arn:aws:greengrass:{REGION}:{ACCOUNT_ID}:coreDevices:{name}"]

    # The core device is gone, and with it the dda-portal:managed tag.
    assert name not in env.greengrass.core_devices
    rows = env.portal_rows(name)
    assert rows["device"] is None
    assert rows["camera_registry"] == []
    assert rows["account_sync"] is None
    assert [r["usecase_id"] for r in rows["registrations"]] == ["uc-elsewhere"]

    # The AWS IoT identity is untouched: the device keeps its connection.
    assert env.thing_exists(name)
    assert env.iot.list_thing_principals(thingName=name)["principals"] == [
        cert["certificateArn"]]
    assert env.certificate_status(cert["certificateId"]) == "ACTIVE"
    env.iot_data.get_thing_shadow(thingName=name, shadowName="dda-model-status")

    # Another device is not affected.
    assert other in env.greengrass.core_devices
    other_rows = env.portal_rows(other)
    assert other_rows["device"] is not None
    assert len(other_rows["camera_registry"]) == 3
    assert other_rows["account_sync"] is not None
    assert len(other_rows["registrations"]) == 1


def test_delete_thing_removes_certificate_shadows_and_thing(env):
    name, cert = env.seed_device()

    status, body = env.remove(name, env.user("UseCaseAdmin"),
                              delete_thing="true")

    assert status == 200, body
    assert body["deleted"] is True
    assert body["delete_thing"] is True
    assert body["thing_deleted"] is True
    assert body["certificates_deactivated"] == [cert["certificateId"]]
    assert body["certificates_deleted"] == [cert["certificateId"]]
    assert sorted(body["shadows_deleted"]) == sorted(
        ["(classic)", "dda-camera-registry", "dda-model-status"])
    assert body["warnings"] == []

    assert not env.thing_exists(name)
    assert env.certificate_status(cert["certificateId"]) is None
    assert name not in env.greengrass.core_devices
    assert env.portal_rows(name)["device"] is None
    # The thing policy is shared by every device and stays.
    env.iot.get_policy(policyName=THING_POLICY)


def test_certificate_shared_with_another_thing_is_detached_but_kept(env):
    name, cert = env.seed_device()
    neighbour = f"neighbour-{uuid.uuid4().hex[:8]}"
    env.iot.create_thing(thingName=neighbour)
    env.iot.attach_thing_principal(thingName=neighbour,
                                   principal=cert["certificateArn"])

    status, body = env.remove(name, env.user("Operator"), delete_thing="true")

    assert status == 200, body
    assert body["thing_deleted"] is True
    assert body["certificates_deactivated"] == []
    assert body["certificates_deleted"] == []
    assert any(neighbour in warning for warning in body["warnings"]), body
    assert not env.thing_exists(name)
    assert env.certificate_status(cert["certificateId"]) == "ACTIVE"
    assert env.iot.list_thing_principals(thingName=neighbour)["principals"] == [
        cert["certificateArn"]]


def test_missing_iot_thing_still_removes_the_core_device(env):
    name, cert = env.seed_device()
    env.iot.detach_thing_principal(thingName=name,
                                   principal=cert["certificateArn"])
    env.iot.delete_thing(thingName=name)

    status, body = env.remove(name, env.user("Operator"), delete_thing="true")

    assert status == 200, body
    assert body["thing_deleted"] is False
    assert any("was not found" in warning for warning in body["warnings"])
    assert name not in env.greengrass.core_devices


def test_detach_propagation_race_is_retried(env):
    name, cert = env.seed_device()
    calls = {"delete_certificate": 0, "delete_thing": 0}

    def racing(operation, code, message):
        def override(client, **kwargs):
            calls[operation] += 1
            if calls[operation] == 1:
                raise _client_error(code, message, operation)
            return getattr(client, operation)(**kwargs)
        return override

    env.iot_overrides.update(
        delete_certificate=racing(
            "delete_certificate", "DeleteConflictException",
            "Things must be detached before deletion"),
        delete_thing=racing(
            "delete_thing", "InvalidRequestException",
            f"Cannot delete. Thing {name} is still attached to one or more "
            f"principals"))

    status, body = env.remove(name, env.user("Operator"), delete_thing="true")

    assert status == 200, body
    assert calls == {"delete_certificate": 2, "delete_thing": 2}
    assert body["certificates_deleted"] == [cert["certificateId"]]
    assert not env.thing_exists(name)


def test_thing_delete_failure_keeps_the_device_listed(env):
    """A failed thing delete must leave the core device (and the portal
    rows) in place, so the device stays on the Devices page and the
    removal can be retried."""
    name, cert = env.seed_device()

    def denied(client, **kwargs):
        raise _client_error("AccessDeniedException", "not authorized",
                            "DeleteThing")

    env.iot_overrides["delete_thing"] = denied

    status, body = env.remove(name, env.user("Operator"), delete_thing="true")

    assert status == 502, body
    assert body["failed_step"] == "delete_thing"
    assert body["deleted"] is False
    # Partial progress is reported: the certificate was already revoked.
    assert body["certificates_deleted"] == [cert["certificateId"]]
    assert name in env.greengrass.core_devices
    assert env.portal_rows(name)["device"] is not None
    assert sorted(e["result"] for e in env.audit_entries(name)) == [
        "failure", "pending"]

    # Retrying once the cause is fixed completes the removal.
    env.iot_overrides.clear()
    status, body = env.remove(name, env.user("Operator"), delete_thing="true")
    assert status == 200, body
    assert body["thing_deleted"] is True
    assert name not in env.greengrass.core_devices


def test_core_device_delete_failure_returns_502_and_keeps_portal_rows(env):
    name, _ = env.seed_device()
    env.greengrass.delete_error = _client_error(
        "ConflictException", "a deployment is in progress", "DeleteCoreDevice")

    status, body = env.remove(name, env.user("Operator"))

    assert status == 502, body
    assert body["failed_step"] == "delete_core_device"
    assert name in env.greengrass.core_devices
    assert env.portal_rows(name)["device"] is not None


# ---------------------------------------------------------------------------
# Refusals: only DDA-managed core devices, only manage_devices holders
# ---------------------------------------------------------------------------

def test_core_device_without_the_dda_tag_is_not_removed(env):
    name, cert = env.seed_device(tags={"owner": "another-team"})

    status, body = env.remove(name, env.user("PortalAdmin"),
                              delete_thing="true")

    assert status == 404, body
    assert name in env.greengrass.core_devices
    assert env.thing_exists(name)
    assert env.certificate_status(cert["certificateId"]) == "ACTIVE"
    assert env.portal_rows(name)["device"] is not None
    assert env.audit_entries(name) == []


@pytest.mark.parametrize("not_found_code", [
    "NotFoundException",          # what the live service returns
    "ResourceNotFoundException",  # what the API reference documents
])
def test_unknown_core_device_is_404(env, not_found_code):
    env.greengrass.tags_not_found_code = not_found_code
    status, body = env.remove("no-such-station", env.user("PortalAdmin"))
    assert status == 404, body
    assert body["error"] == "Device not found"
    assert env.audit_entries("no-such-station") == []


@pytest.mark.parametrize("role", ["Viewer", "DataScientist", "DataLabeler"])
def test_role_without_manage_devices_is_denied_and_audited(env, role):
    name, cert = env.seed_device()

    status, body = env.remove(name, env.user(role), delete_thing="true")

    assert status == 403, body
    assert name in env.greengrass.core_devices
    assert env.thing_exists(name)
    assert env.certificate_status(cert["certificateId"]) == "ACTIVE"
    assert env.portal_rows(name)["device"] is not None
    assert [e["result"] for e in env.audit_entries(name)] == ["rejected"]


def test_unprovisioned_caller_is_denied(env):
    """A token whose custom:role claims PortalAdmin but that has no
    Portal_Identity row grants nothing (registry enforcement)."""
    name, _ = env.seed_device()
    claimed_admin = {"user_id": f"user-{uuid.uuid4().hex[:8]}",
                     "email": "x@example.com", "username": "x",
                     "role": "PortalAdmin"}

    status, body = env.remove(name, claimed_admin)

    assert status == 403, body
    assert name in env.greengrass.core_devices


def test_audit_write_failure_blocks_the_removal(env, monkeypatch):
    name, _ = env.seed_device()
    user = env.user("Operator")

    def failing_audit(*args, **kwargs):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(env.devices, "record_audit_event_strict",
                        failing_audit)

    status, body = env.remove(name, user)

    assert status == 500, body
    assert name in env.greengrass.core_devices
    assert env.portal_rows(name)["device"] is not None


@pytest.mark.parametrize("query, error", [
    ({}, "usecase_id parameter required"),
    ({"usecase_id": "uc-x"}, "Invalid device id"),
])
def test_bad_input_is_400(env, query, error):
    device_id = "bad/../id" if query else "station-1"
    response = env.devices.delete_device(device_id, env.user("PortalAdmin"),
                                         query)
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == error


def test_unknown_use_case_is_404(env):
    response = env.devices.delete_device(
        "station-1", env.user("PortalAdmin"), {"usecase_id": "uc-missing"})
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error"] == "Use case not found"


# ---------------------------------------------------------------------------
# Routing and the audit trail through the Lambda entry point
# ---------------------------------------------------------------------------

def test_handler_routes_delete_and_records_an_attributed_audit_entry(env):
    name, _ = env.seed_device()
    user = env.user("Operator")
    event = {
        "httpMethod": "DELETE",
        "path": f"/devices/{name}",
        "pathParameters": {"id": name},
        "queryStringParameters": {"usecase_id": env.usecase_id,
                                  "delete_thing": "true"},
        "body": None,
        "requestContext": {
            "authorizer": {"claims": {
                "sub": user["user_id"], "email": user["email"],
                "cognito:username": user["username"],
                "custom:role": "Operator"}},
            "identity": {"sourceIp": "203.0.113.7", "userAgent": "pytest"},
        },
    }

    response = env.devices.handler(event, None)

    assert response["statusCode"] == 200, response
    body = json.loads(response["body"])
    assert body["deleted"] is True and body["thing_deleted"] is True

    # Audit before effect: a pending entry, then the outcome, which points
    # back at it. Both are attributed to the caller.
    entries = {e["result"]: e for e in env.audit_entries(name)}
    assert sorted(entries) == ["pending", "success"]
    pending, outcome = entries["pending"], entries["success"]
    assert pending["details"] == {"usecase_id": env.usecase_id,
                                  "delete_thing": True}
    assert outcome["details"]["pending_event_id"] == pending["event_id"]
    assert outcome["details"]["thing_deleted"] is True
    for entry in (pending, outcome):
        assert entry["user_id"] == user["user_id"]
        assert entry["source_ip"] == "203.0.113.7"
