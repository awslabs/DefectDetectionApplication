"""
Camera_Registry stream camera flows with Portal-managed credentials
(rtsp-rtmp-stream-cameras task 8.6).

Route-level unit tests against the moto-backed conftest stack. Unlike the
older registry route tests, nothing on the credential path is faked:
moto's Secrets Manager, IAM, and IoT data plane sit behind the real
``camera_registry`` + ``stream_credentials`` modules, and the desired
change is read back from moto's named ``dda-camera-registry`` shadow, which
merges updates field by field like AWS IoT does. A shadow write is made to
fail the natural way, by addressing a thing that does not exist.

Covered:

- stream body validation: 400s naming the field, never echoing a value,
  and every other type's validation unchanged (Reqs 5.2, 18.3)
- create, update, clear, and delete with credentials: what the vault
  holds, what the shadow and registry carry, what the view returns
  (Reqs 5.3, 5.7, 5.8)
- the delivery-failure rollback for create, update, re-add after a clear,
  clear, and delete (Req 5.4)
- the 409 when the Use_Case_Account does not grant the Portal the
  credential capabilities, and credential-free stream cameras still
  accepted (Req 5.9)
- the existing authorization and audit behaviour (Req 5.10)
- non-stream flows untouched by the credential machinery (Req 18.3)

Requirements: 5.2, 5.3, 5.4, 5.8, 5.9, 5.10, 18.3
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-stream-credentials"
SETTINGS_TABLE_NAME = "test-settings-camera-stream-credentials"
DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"
SHADOW_NAME = "dda-camera-registry"
DELIVERY_FAILURE = {
    "error": "Failed to deliver the change to the device sync channel"}

# Distinctive credential values: a substring hit anywhere is a real leak.
USERNAME = "viewer-UNAME-7f3a"
PASSWORD = "hunter2-PWD-9c1e"
PASSWORD_2 = "rotated-PWD-44b0"
URL_SECRET = "streamkey-USEC-12de"
ALL_SECRETS = (USERNAME, PASSWORD, PASSWORD_2, URL_SECRET)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """Registry + settings tables, the device token-exchange role, and a
    freshly imported handler bound to moto's clients."""
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
        secrets=boto3.client("secretsmanager", region_name=REGION),
        iam=iam,
        iot=boto3.client("iot", region_name=REGION),
        iot_data=boto3.client("iot-data", region_name=REGION),
    )


@pytest.fixture
def device(camera_env, env):
    """A registered, synced device of a fresh Use_Case, with its IoT thing
    (so shadow writes succeed) and an Operator."""
    usecase_id = env.create_usecase()
    device_id = f"thing-sc-{uuid.uuid4().hex[:12]}"
    camera_env.iot.create_thing(thingName=device_id)
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })
    return SimpleNamespace(device_id=device_id, usecase_id=usecase_id,
                           operator=env.make_user(role="Operator"),
                           viewer=env.make_user(role="Viewer"))


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


def invoke(camera_env, method, device_id, user, sub_path="", body=None):
    """(status, parsed body, raw body string)."""
    response = camera_env.module.handler(
        make_event(method, device_id, user, sub_path, body), None)
    return (response["statusCode"], json.loads(response["body"]),
            response["body"])


def rtsp_body(csid=None, credentials=None, **params):
    body = {
        "name": "Dock 3 overview",
        "type": "RTSP",
        "params": {"url": "rtsp://10.0.4.21:554/Streaming/Channels/101",
                   "transport": "tcp", "latencyMs": 200, **params},
    }
    if csid is not None:
        body["camera_source_id"] = csid
    if credentials is not None:
        body["credentials"] = credentials
    return body


def create(camera_env, device, csid, credentials=None, **params):
    status, body, _ = invoke(camera_env, "POST", device.device_id,
                             device.operator,
                             body=rtsp_body(csid, credentials, **params))
    assert status == 201, body
    return body


def update(camera_env, device, csid, body, user=None):
    return invoke(camera_env, "PUT", device.device_id,
                  user or device.operator, sub_path=f"/{csid}", body=body)


def secret_name(camera_env, device, csid):
    return camera_env.credentials.secret_name(device.device_id, csid)


def describe(camera_env, name):
    try:
        return camera_env.secrets.describe_secret(SecretId=name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def current_version(description):
    """The version AWSCURRENT points at (works for a secret pending
    deletion, which GetSecretValue refuses)."""
    currents = [version for version, stages in
                (description.get("VersionIdsToStages") or {}).items()
                if "AWSCURRENT" in stages]
    assert len(currents) == 1, description.get("VersionIdsToStages")
    return currents[0]


def secret_value(camera_env, name, version_id):
    return json.loads(camera_env.secrets.get_secret_value(
        SecretId=name, VersionId=version_id)["SecretString"])


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


def consume_change(camera_env, device_id, csid):
    """What the Edge_Sync_Agent does once it applied a change: clear the
    desired entry with an explicit null."""
    camera_env.iot_data.update_thing_shadow(
        thingName=device_id, shadowName=SHADOW_NAME,
        payload=json.dumps({"state": {"desired": {"changes": {csid: None}}}}))


def camera_item(camera_env, device_id, csid):
    return camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"}).get("Item")


def device_items(camera_env, device_id):
    return camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id}).get("Items", [])


def audit_rows(env_stack, device_id):
    return [row for row in env_stack.tables.audit_log.scan().get("Items", [])
            if (row.get("details") or {}).get("device_id") == device_id]


def assert_no_secret_in(text, where):
    for value in ALL_SECRETS:
        assert value not in text, f"a credential value leaked into {where}"


def camera_view_of(camera_env, device, csid):
    status, body, raw = invoke(camera_env, "GET", device.device_id,
                               device.operator)
    assert status == 200, body
    assert_no_secret_in(raw, "GET /devices/{id}/cameras")
    return next(c for c in body["cameras"] if c["camera_source_id"] == csid)


class DeniedClient:
    """A Use_Case_Account client whose every call is AccessDenied, for the
    Req 5.9 paths."""

    def __init__(self, service):
        self.service = service

    def __getattr__(self, operation):
        def denied(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException",
                           "Message": "not authorized"}}, operation)
        return denied


@pytest.fixture
def deny(camera_env, monkeypatch):
    """Make the Use_Case_Account deny the Portal one service."""
    original = camera_env.credentials._client

    def install(service):
        def client(name, usecase, region=None, session_name=None):
            if name == service:
                return DeniedClient(name)
            return original(name, usecase, region=region,
                            session_name=session_name)
        monkeypatch.setattr(camera_env.credentials, "_client", client)
    return install


# ---------------------------------------------------------------------------
# Req 5.2 / 18.3: body validation
# ---------------------------------------------------------------------------

class TestStreamBodyValidation:

    @pytest.mark.parametrize("key", ["password", "username", "token",
                                     "urlSecret", "Secret"])
    def test_credential_like_param_is_rejected_by_field(
            self, camera_env, device, key):
        status, body, raw = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body=rtsp_body("cam-1", **{key: PASSWORD}))
        assert status == 400
        assert body["field"] == f"params.{key}"
        assert PASSWORD not in raw
        assert describe(camera_env, secret_name(camera_env, device,
                                                "cam-1")) is None
        assert shadow_changes(camera_env, device.device_id) == {}

    def test_server_managed_param_is_rejected(self, camera_env, device):
        status, body, _ = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body=rtsp_body("cam-1", credentialRef={
                "secretArn": "arn:aws:secretsmanager:x", "versionId": "v"}))
        assert status == 400
        assert body["field"] == "params.credentialRef"

    def test_user_information_in_the_url_is_rejected_without_echo(
            self, camera_env, device):
        body = rtsp_body("cam-1")
        body["params"]["url"] = f"rtsp://admin:{PASSWORD}@10.0.4.21/live"
        status, response, raw = invoke(camera_env, "POST", device.device_id,
                                       device.operator, body=body)
        assert status == 400
        assert response["field"] == "params.url"
        assert response["code"] == "user_info"
        assert PASSWORD not in raw

    def test_rtsp_only_setting_is_rejected_for_rtmp(self, camera_env, device):
        status, body, _ = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body={"name": "Ingest", "type": "RTMP",
                  "params": {"url": "rtmp://media.local/live",
                             "transport": "tcp"}})
        assert status == 400
        assert body["field"] == "params.transport"

    @pytest.mark.parametrize("key,value", [
        ("latencyMs", 5001), ("maxFrameDimension", 319),
        ("stallTimeoutS", 61), ("decoder", "gpu"), ("transport", "sctp")])
    def test_out_of_domain_setting_is_rejected(self, camera_env, device,
                                               key, value):
        status, body, _ = invoke(camera_env, "POST", device.device_id,
                                 device.operator,
                                 body=rtsp_body("cam-1", **{key: value}))
        assert status == 400
        assert body["field"] == f"params.{key}"

    def test_unknown_credential_field_is_rejected(self, camera_env, device):
        status, body, raw = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body=rtsp_body("cam-1", credentials={"token": PASSWORD}))
        assert status == 400
        assert body["field"] == "credentials.token"
        assert PASSWORD not in raw

    def test_credentials_and_clear_together_are_rejected(
            self, camera_env, device):
        body = rtsp_body("cam-1", credentials={"password": PASSWORD})
        body["clearCredentials"] = True
        status, response, raw = invoke(camera_env, "POST", device.device_id,
                                       device.operator, body=body)
        assert status == 400
        assert response["field"] == "clearCredentials"
        assert PASSWORD not in raw

    def test_other_types_keep_their_validation(self, camera_env, device):
        """A non-stream body is validated exactly as before this feature:
        stream rules (credential-like keys, URL rules) do not apply."""
        params = {"devicePath": "/dev/video0", "password": "legacy-value",
                  "url": "rtsp://admin:pw@legacy/"}
        status, body, _ = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body={"name": "USB", "type": "Camera", "params": params,
                  "camera_source_id": "usb-1"})
        assert status == 201, body
        change = shadow_changes(camera_env, device.device_id)["usb-1"]
        assert change["params"] == params


# ---------------------------------------------------------------------------
# Req 5.3 / 5.7: create with credentials
# ---------------------------------------------------------------------------

class TestCreateWithCredentials:

    def test_create_stores_the_credentials_and_delivers_only_a_reference(
            self, camera_env, device, env):
        create(camera_env, device, "cam-1",
               credentials={"username": USERNAME, "password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        description = describe(camera_env, name)
        assert description is not None and description.get(
            "DeletedDate") is None
        version = current_version(description)
        # The value carries the supplied fields only.
        assert secret_value(camera_env, name, version) == {
            "username": USERNAME, "password": PASSWORD}
        tags = {t["Key"]: t["Value"] for t in description["Tags"]}
        assert tags == {"dda-portal:usecase_id": device.usecase_id,
                        "dda-portal:device_id": device.device_id,
                        "dda-portal:camera_source_id": "cam-1"}

        # The desired change carries the reference and the flags.
        change = shadow_changes(camera_env, device.device_id)["cam-1"]
        params = change["params"]
        assert params["credentialRef"] == {
            "secretArn": description["ARN"], "versionId": version}
        assert params["credentialsConfigured"] is True
        assert isinstance(params["credentialsUpdatedAt"], int)
        assert params["url"] == "rtsp://10.0.4.21:554/Streaming/Channels/101"

        # No credential value anywhere the registry path writes.
        assert_no_secret_in(json.dumps(change), "the desired change")
        assert_no_secret_in(json.dumps(device_items(
            camera_env, device.device_id), default=str), "the registry")
        rows = audit_rows(env.stack, device.device_id)
        assert [row["action"] for row in rows] == ["create_camera_source"]
        assert rows[0]["details"]["credentials_configured"] is True
        assert "credentialRef" not in json.dumps(rows[0], default=str)
        assert_no_secret_in(json.dumps(rows, default=str), "the audit log")

    def test_create_grants_the_device_read_access_to_its_own_secrets(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        policy = camera_env.iam.get_role_policy(
            RoleName=DEVICE_ROLE_NAME,
            PolicyName="DDAStreamCameraCredentialRead")["PolicyDocument"]
        if isinstance(policy, str):
            policy = json.loads(policy)
        (statement,) = policy["Statement"]
        assert statement["Effect"] == "Allow"
        assert statement["Action"] == ["secretsmanager:GetSecretValue"]
        assert statement["Resource"].endswith(
            ":secret:dda-portal/stream-camera-credentials/"
            "${credentials-iot:ThingName}/*")

    def test_view_reports_credential_state_and_never_values(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"username": USERNAME, "password": PASSWORD})
        view = camera_view_of(camera_env, device, "cam-1")
        assert view["credentials"]["configured"] is True
        assert isinstance(view["credentials"]["updatedAt"], int)
        assert "credentialRef" not in view["params"]

    def test_legacy_url_with_user_information_is_redacted_in_the_view(
            self, camera_env, device):
        camera_env.registry.put_item(Item={
            "device_id": device.device_id, "sk": "CAMERA#legacy",
            "camera_source_id": "legacy", "usecase_id": device.usecase_id,
            "name": "Legacy RTSP", "type": "RTSP",
            "params": {"url": f"rtsp://admin:{PASSWORD}@10.0.9.9/live"},
            "capabilities": {}, "origin": "edge-configured", "version": 1,
            "sync_status": "synced", "last_reported_at": 1_700_000_000_000,
        })
        view = camera_view_of(camera_env, device, "legacy")
        assert "10.0.9.9/live" in view["params"]["url"]
        assert view["credentials"] == {"configured": False,
                                       "updatedAt": None}

    def test_credential_free_stream_camera_stores_nothing(
            self, camera_env, device):
        create(camera_env, device, "cam-1")
        assert describe(camera_env, secret_name(camera_env, device,
                                                "cam-1")) is None
        params = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]
        assert "credentialRef" not in params
        assert camera_view_of(camera_env, device, "cam-1")[
            "credentials"]["configured"] is False


# ---------------------------------------------------------------------------
# Req 5.8: update, clear, delete
# ---------------------------------------------------------------------------

class TestUpdateClearDelete:

    def test_update_with_credentials_writes_a_new_version_and_reference(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        first = current_version(describe(camera_env, name))
        consume_change(camera_env, device.device_id, "cam-1")

        status, body, raw = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD_2}))
        assert status == 200, body
        assert_no_secret_in(raw, "the update response")
        second = current_version(describe(camera_env, name))
        assert second != first
        assert secret_value(camera_env, name, second) == {
            "password": PASSWORD_2}
        params = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]
        assert params["credentialRef"]["versionId"] == second

    def test_update_without_credentials_carries_the_reference_forward(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        reference = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]["credentialRef"]
        consume_change(camera_env, device.device_id, "cam-1")

        status, body, _ = update(camera_env, device, "cam-1",
                                 rtsp_body(latencyMs=400))
        assert status == 200, body
        params = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]
        assert params["credentialRef"] == reference
        assert params["credentialsConfigured"] is True
        assert params["latencyMs"] == 400

    def test_clear_delivers_no_reference_and_schedules_the_deletion(
            self, camera_env, device, env):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        version = current_version(describe(camera_env, name))

        body = rtsp_body()
        body["clearCredentials"] = True
        status, response, _ = update(camera_env, device, "cam-1", body)
        assert status == 200, response
        params = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]
        assert params["credentialsConfigured"] is False
        # The earlier change was not consumed yet, so the shadow merged
        # both: the clear must still remove the old reference.
        assert "credentialRef" not in params
        description = describe(camera_env, name)
        assert description.get("DeletedDate") is not None
        assert current_version(description) == version  # versions untouched
        rows = audit_rows(env.stack, device.device_id)
        assert rows[-1]["action"] == "update_camera_source"
        assert rows[-1]["details"]["credentials_configured"] is False

    def test_update_after_an_unacknowledged_clear_keeps_the_clear(
            self, camera_env, device, env):
        """The device reported the reference, then the Portal cleared it;
        an unrelated edit before the device acknowledges the clear must not
        deliver the old reference again.

        Re-keyed by task 29 (third design review, finding 3): the device
        reports the camera a create made under ``cfg-<imageSourceId>``, so
        the acknowledged camera is a ``cfg-`` entry with origin
        ``edge-configured``. The create route refuses a ``cfg-`` id, so the
        entry is seeded as the sync reducer leaves it after the re-key: the
        create's params, its ``credential_secret_arn``, and the mirror's
        ``cam-1`` item retired."""
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        item = camera_item(camera_env, device.device_id, "cam-1")
        consume_change(camera_env, device.device_id, "cam-1")

        # The old fixture shape, a synced stream entry under a non-cfg- id,
        # is a create mirror now: the clear is refused and writes nothing.
        mirror = dict(item)
        mirror["params"] = dict(item["pending_content"]["params"])
        mirror["sync_status"] = "synced"
        camera_env.registry.put_item(Item=mirror)
        audit_before = len(audit_rows(env.stack, device.device_id))
        clear = rtsp_body()
        clear["clearCredentials"] = True
        status, body, _ = update(camera_env, device, "cam-1", clear)
        assert status == 409, body
        assert body["code"] == "CAMERA_SOURCE_ALIAS"
        assert camera_item(camera_env, device.device_id, "cam-1") == mirror
        assert shadow_changes(camera_env, device.device_id) == {}
        assert len(audit_rows(env.stack, device.device_id)) == audit_before
        assert describe(camera_env, secret_name(
            camera_env, device, "cam-1")).get("DeletedDate") is None

        # The acknowledged camera as the device reports it: cfg-1.
        camera_env.registry.delete_item(
            Key={"device_id": device.device_id, "sk": "CAMERA#cam-1"})
        camera_env.registry.put_item(Item={
            "device_id": device.device_id, "sk": "CAMERA#cfg-1",
            "camera_source_id": "cfg-1", "usecase_id": device.usecase_id,
            "name": item["name"], "type": "RTSP",
            "params": dict(item["pending_content"]["params"]),
            "capabilities": {}, "origin": "edge-configured", "version": 1,
            "absent": False, "sync_status": "synced",
            "last_reported_at": 1_700_000_000_000,
            "credential_secret_arn": item["credential_secret_arn"],
        })

        assert update(camera_env, device, "cfg-1", clear)[0] == 200
        status, body, _ = update(camera_env, device, "cfg-1",
                                 rtsp_body(latencyMs=300))
        assert status == 200, body
        params = shadow_changes(camera_env, device.device_id)["cfg-1"][
            "params"]
        assert "credentialRef" not in params
        pending = camera_item(camera_env, device.device_id, "cfg-1")[
            "pending_content"]["params"]
        assert "credentialRef" not in pending

    def test_readding_credentials_after_a_clear_restores_the_secret(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        clear = rtsp_body()
        clear["clearCredentials"] = True
        assert update(camera_env, device, "cam-1", clear)[0] == 200
        name = secret_name(camera_env, device, "cam-1")
        assert describe(camera_env, name).get("DeletedDate") is not None

        status, body, _ = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD_2}))
        assert status == 200, body
        description = describe(camera_env, name)
        assert description.get("DeletedDate") is None
        version = current_version(description)
        assert secret_value(camera_env, name, version) == {
            "password": PASSWORD_2}
        params = shadow_changes(camera_env, device.device_id)["cam-1"][
            "params"]
        assert params["credentialRef"]["versionId"] == version

    def test_delete_schedules_the_deletion_after_the_change_is_written(
            self, camera_env, device, env):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        consume_change(camera_env, device.device_id, "cam-1")
        status, body, _ = invoke(camera_env, "DELETE", device.device_id,
                                 device.operator, sub_path="/cam-1")
        assert status == 200, body
        assert shadow_changes(camera_env, device.device_id)["cam-1"][
            "op"] == "delete"
        name = secret_name(camera_env, device, "cam-1")
        assert describe(camera_env, name).get("DeletedDate") is not None
        assert audit_rows(env.stack, device.device_id)[-1][
            "action"] == "delete_camera_source"

    def test_deleting_a_credential_free_stream_camera_is_not_an_error(
            self, camera_env, device):
        create(camera_env, device, "cam-1")
        status, body, _ = invoke(camera_env, "DELETE", device.device_id,
                                 device.operator, sub_path="/cam-1")
        assert status == 200, body
        assert describe(camera_env, secret_name(camera_env, device,
                                                "cam-1")) is None


# ---------------------------------------------------------------------------
# Req 5.4: rollback when the shadow write fails
# ---------------------------------------------------------------------------

def break_shadow(camera_env, device):
    """Make every later shadow write fail: moto's iot-data refuses a
    thing that does not exist."""
    camera_env.iot.delete_thing(thingName=device.device_id)


class TestDeliveryFailureRollback:

    def test_failed_create_force_deletes_the_new_secret(
            self, camera_env, device, env):
        break_shadow(camera_env, device)
        status, body, raw = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body=rtsp_body("cam-1", credentials={"password": PASSWORD}))
        assert (status, body) == (502, DELIVERY_FAILURE)
        assert_no_secret_in(raw, "the 502")
        assert describe(camera_env, secret_name(camera_env, device,
                                                "cam-1")) is None
        assert camera_item(camera_env, device.device_id, "cam-1") is None
        assert audit_rows(env.stack, device.device_id) == []

    def test_failed_update_moves_awscurrent_back(self, camera_env, device,
                                                 env):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        before = current_version(describe(camera_env, name))
        item_before = camera_item(camera_env, device.device_id, "cam-1")
        audit_before = len(audit_rows(env.stack, device.device_id))

        break_shadow(camera_env, device)
        status, body, _ = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD_2}))
        assert (status, body) == (502, DELIVERY_FAILURE)
        description = describe(camera_env, name)
        assert current_version(description) == before
        assert secret_value(camera_env, name, before) == {
            "password": PASSWORD}
        assert description.get("DeletedDate") is None
        assert camera_item(camera_env, device.device_id,
                           "cam-1") == item_before
        assert len(audit_rows(env.stack, device.device_id)) == audit_before

    def test_failed_readd_after_a_clear_schedules_the_deletion_again(
            self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        clear = rtsp_body()
        clear["clearCredentials"] = True
        assert update(camera_env, device, "cam-1", clear)[0] == 200
        name = secret_name(camera_env, device, "cam-1")
        before = current_version(describe(camera_env, name))

        break_shadow(camera_env, device)
        status, body, _ = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD_2}))
        assert (status, body) == (502, DELIVERY_FAILURE)
        description = describe(camera_env, name)
        assert description.get("DeletedDate") is not None, \
            "the cleared credentials were restored for good"
        assert current_version(description) == before

    def test_failed_clear_leaves_the_secret_live(self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        break_shadow(camera_env, device)
        clear = rtsp_body()
        clear["clearCredentials"] = True
        assert update(camera_env, device, "cam-1", clear)[:2] == (
            502, DELIVERY_FAILURE)
        assert describe(camera_env, name).get("DeletedDate") is None

    def test_failed_delete_leaves_the_secret_live(self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        break_shadow(camera_env, device)
        status, body, _ = invoke(camera_env, "DELETE", device.device_id,
                                 device.operator, sub_path="/cam-1")
        assert (status, body) == (502, DELIVERY_FAILURE)
        assert describe(camera_env, name).get("DeletedDate") is None


# ---------------------------------------------------------------------------
# Req 5.9: credential storage the Use_Case_Account does not grant
# ---------------------------------------------------------------------------

class TestCredentialStorageUnavailable:

    @pytest.mark.parametrize("service,capability", [
        ("secretsmanager", "permission to store stream camera credentials"),
        ("iam", "permission to grant devices read access to stream camera "
                "credentials"),
    ])
    def test_credentialed_create_is_rejected_and_writes_nothing(
            self, camera_env, device, env, deny, service, capability):
        deny(service)
        status, body, raw = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body=rtsp_body("cam-1", credentials={"password": PASSWORD}))
        assert status == 409
        assert body["code"] == "STREAM_CREDENTIALS_UNAVAILABLE"
        assert body["capability"] == capability
        assert capability in body["error"]
        assert "update the use-case account stack" in body["error"]
        assert_no_secret_in(raw, "the 409")
        assert shadow_changes(camera_env, device.device_id) == {}
        assert camera_item(camera_env, device.device_id, "cam-1") is None
        assert audit_rows(env.stack, device.device_id) == []

    def test_credentialed_update_is_rejected_and_writes_nothing(
            self, camera_env, device, deny):
        create(camera_env, device, "cam-1")
        consume_change(camera_env, device.device_id, "cam-1")
        item_before = camera_item(camera_env, device.device_id, "cam-1")
        deny("secretsmanager")
        status, body, _ = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD}))
        assert status == 409
        assert body["code"] == "STREAM_CREDENTIALS_UNAVAILABLE"
        assert camera_item(camera_env, device.device_id,
                           "cam-1") == item_before
        assert shadow_changes(camera_env, device.device_id) == {}

    def test_credential_free_stream_camera_is_still_accepted(
            self, camera_env, device, deny):
        deny("secretsmanager")
        create(camera_env, device, "cam-1")
        assert "cam-1" in shadow_changes(camera_env, device.device_id)


# ---------------------------------------------------------------------------
# Req 5.10: the existing authorization and audit events
# ---------------------------------------------------------------------------

class TestAuthorization:

    def test_viewer_cannot_create_a_stream_camera_or_store_credentials(
            self, camera_env, device):
        status, _, raw = invoke(
            camera_env, "POST", device.device_id, device.viewer,
            body=rtsp_body("cam-1", credentials={"password": PASSWORD}))
        assert status == 403
        assert_no_secret_in(raw, "the 403")
        assert describe(camera_env, secret_name(camera_env, device,
                                                "cam-1")) is None
        assert shadow_changes(camera_env, device.device_id) == {}

    def test_viewer_cannot_change_or_clear_credentials(self, camera_env,
                                                       device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        name = secret_name(camera_env, device, "cam-1")
        before = current_version(describe(camera_env, name))
        status, _, _ = update(
            camera_env, device, "cam-1",
            rtsp_body(credentials={"password": PASSWORD_2}),
            user=device.viewer)
        assert status == 403
        clear = rtsp_body()
        clear["clearCredentials"] = True
        assert update(camera_env, device, "cam-1", clear,
                      user=device.viewer)[0] == 403
        description = describe(camera_env, name)
        assert current_version(description) == before
        assert description.get("DeletedDate") is None

    def test_viewer_sees_the_credential_state(self, camera_env, device):
        create(camera_env, device, "cam-1",
               credentials={"password": PASSWORD})
        status, body, raw = invoke(camera_env, "GET", device.device_id,
                                   device.viewer)
        assert status == 200
        assert_no_secret_in(raw, "the Viewer's camera list")
        (view,) = [c for c in body["cameras"]
                   if c["camera_source_id"] == "cam-1"]
        assert view["credentials"]["configured"] is True


# ---------------------------------------------------------------------------
# Req 18.3: other types never touch the credential machinery
# ---------------------------------------------------------------------------

class TestOtherTypesUnchanged:

    @pytest.fixture
    def vault_forbidden(self, camera_env, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("a non-stream flow touched the vault")
        # Task 29: a non-stream flow never resolves the use case's account
        # either (Req 18.3).
        for name in ("ensure_device_read_grant", "store_stream_credentials",
                     "withdraw_stream_credentials",
                     "schedule_secret_deletion", "_usecase_account_id",
                     "secret_scope"):
            monkeypatch.setattr(camera_env.credentials, name, forbidden)

    @pytest.fixture
    def payloads(self, camera_env, monkeypatch):
        """The raw desired-change payloads, recorded on their way to the
        real (moto) shadow. A merged shadow drops nulls, so only the
        payload shows whether a write carried any."""
        recorded = []
        original = camera_env.module.iot_data_client

        class Recording:
            def __init__(self, client):
                self.client = client

            def update_thing_shadow(self, **kwargs):
                recorded.append(json.loads(kwargs["payload"]))
                return self.client.update_thing_shadow(**kwargs)

        monkeypatch.setattr(camera_env.module, "iot_data_client",
                            lambda usecase_id: Recording(
                                original(usecase_id)))
        return recorded

    def test_create_update_delete_of_another_type(self, camera_env, device,
                                                  env, vault_forbidden,
                                                  payloads):
        params = {"devicePath": "/dev/video2"}
        status, body, _ = invoke(
            camera_env, "POST", device.device_id, device.operator,
            body={"name": "USB", "type": "Camera", "params": params,
                  "camera_source_id": "usb-1",
                  "credentials": {"password": PASSWORD}})
        assert status == 201, body
        # Delivered exactly as sent: no flags, no reference, no nulls.
        assert payloads[-1]["state"]["desired"]["changes"]["usb-1"][
            "params"] == params
        assert shadow_changes(camera_env, device.device_id)["usb-1"][
            "params"] == params
        consume_change(camera_env, device.device_id, "usb-1")

        status, body, _ = update(camera_env, device, "usb-1", {
            "name": "USB", "type": "Camera",
            "params": {"devicePath": "/dev/video3"}})
        assert status == 200, body
        assert payloads[-1]["state"]["desired"]["changes"]["usb-1"][
            "params"] == {"devicePath": "/dev/video3"}
        consume_change(camera_env, device.device_id, "usb-1")

        status, body, _ = invoke(camera_env, "DELETE", device.device_id,
                                 device.operator, sub_path="/usb-1")
        assert status == 200, body

        for row in audit_rows(env.stack, device.device_id):
            assert "credentials_configured" not in row["details"]
        view = camera_view_of(camera_env, device, "usb-1")
        assert "credentials" not in view


# ---------------------------------------------------------------------------
# Reqs 5.7, 6.1: no read returns a legacy row's credential material
# ---------------------------------------------------------------------------

LEGACY_PASSWORD = "LEGACY-PWD-51c0"
LEGACY_TOKEN = "LEGACY-TOKEN-8d2e"
LEGACY_USER = "LEGACY-USER-a7b3"
REFERENCE_SENTINEL = "REF-SENTINEL-6b90"
LEGACY_SECRETS = (LEGACY_PASSWORD, LEGACY_TOKEN, LEGACY_USER,
                  REFERENCE_SENTINEL)


class TestLegacyCredentialMaterialInViews:
    """A stream row written before this feature, when the Cameras tab took
    any JSON, can hold a credentialed URL and credential-like ``params``
    keys. The body validation now rejects both on the way in; every read
    masks what is already stored. Other types are served as before
    (Req 18.3)."""

    @staticmethod
    def legacy_item(device, csid, source_type, params):
        return {
            "device_id": device.device_id, "sk": f"CAMERA#{csid}",
            "camera_source_id": csid, "usecase_id": device.usecase_id,
            "name": f"Legacy {csid}", "type": source_type, "params": params,
            "capabilities": {}, "origin": "portal-created", "version": 1,
            "sync_status": "failed", "last_reported_at": 1_700_000_000_000,
        }

    def test_camera_read_masks_the_credential_keys_of_stream_rows(
            self, camera_env, device):
        camera_env.registry.put_item(Item=self.legacy_item(
            device, "legacy-rtsp", "RTSP", {
                "url": (f"rtsp://{LEGACY_USER}:{LEGACY_PASSWORD}@10.0.9.9"
                        f"/live?token={LEGACY_TOKEN}"),
                "username": LEGACY_USER, "password": LEGACY_PASSWORD,
                "token": LEGACY_TOKEN, "transport": "tcp"}))
        camera_env.registry.put_item(Item=self.legacy_item(
            device, "legacy-rtmp", "RTMP", {
                "url": "rtmp://media.local/live",
                "urlSecret": LEGACY_TOKEN, "secret": LEGACY_PASSWORD,
                "user": LEGACY_USER}))

        status, body, raw = invoke(camera_env, "GET", device.device_id,
                                   device.viewer)

        assert status == 200, body
        for value in LEGACY_SECRETS:
            assert value not in raw
        views = {c["camera_source_id"]: c for c in body["cameras"]}
        rtsp = views["legacy-rtsp"]["params"]
        assert (rtsp["username"], rtsp["password"], rtsp["token"]) == (
            "***", "***", "***")
        assert rtsp["url"].startswith("rtsp://***@10.0.9.9/live")
        assert "token=***" in rtsp["url"]
        # A non-secret setting is served as stored.
        assert rtsp["transport"] == "tcp"
        assert views["legacy-rtmp"]["params"] == {
            "url": "rtmp://media.local/live", "urlSecret": "***",
            "secret": "***", "user": "***"}
        # The credential state still reports only what the Portal wrote.
        assert views["legacy-rtsp"]["credentials"] == {
            "configured": False, "updatedAt": None}

    def test_other_types_keep_their_params(self, camera_env, device):
        camera_env.registry.put_item(Item=self.legacy_item(
            device, "usb-legacy", "Camera", {
                "devicePath": "/dev/video0", "password": "camera-param"}))
        view = camera_view_of(camera_env, device, "usb-legacy")
        assert view["params"] == {"devicePath": "/dev/video0",
                                  "password": "camera-param"}
        assert "credentials" not in view

    def test_conflict_read_redacts_both_recorded_versions(
            self, camera_env, device):
        stream_cid, camera_cid = uuid.uuid4().hex, uuid.uuid4().hex
        camera_env.registry.put_item(Item={
            "device_id": device.device_id,
            "sk": f"CONFLICT#100#{stream_cid}",
            "camera_source_id": "legacy-rtsp",
            "edge_version": {"name": "Legacy", "type": "RTSP", "params": {
                "url": f"rtsp://{LEGACY_USER}:{LEGACY_PASSWORD}@10.0.9.9/live",
                "credentialRef": {
                    "secretArn": ("arn:aws:secretsmanager:us-east-1:"
                                  f"111122223333:secret:{REFERENCE_SENTINEL}"),
                    "versionId": "v1"},
                "credentialsConfigured": True}},
            "portal_version": {"op": "update", "name": "Legacy",
                               "type": "RTSP", "params": {
                                   "url": "rtsp://10.0.9.9/live",
                                   "password": LEGACY_PASSWORD}},
            "resolution": "edge-retained", "created_at": 100,
        })
        camera_versions = {
            "edge_version": {"name": "USB", "type": "Camera",
                             "params": {"devicePath": "/dev/video0"}},
            "portal_version": {"op": "update", "name": "USB",
                               "type": "Camera", "params": {
                                   "devicePath": "/dev/video2",
                                   "password": "camera-param"}},
        }
        camera_env.registry.put_item(Item={
            "device_id": device.device_id,
            "sk": f"CONFLICT#200#{camera_cid}",
            "camera_source_id": "usb-legacy", **camera_versions,
            "resolution": "edge-retained", "created_at": 200,
        })

        status, body, raw = invoke(camera_env, "GET", device.device_id,
                                   device.viewer, sub_path="/conflicts")

        assert status == 200, body
        for value in LEGACY_SECRETS:
            assert value not in raw
        by_id = {c["conflict_id"]: c for c in body["conflicts"]}
        assert by_id[stream_cid]["edge_version"]["params"] == {
            "url": "rtsp://***@10.0.9.9/live", "credentialsConfigured": True}
        assert by_id[stream_cid]["portal_version"] == {
            "op": "update", "name": "Legacy", "type": "RTSP",
            "params": {"url": "rtsp://10.0.9.9/live", "password": "***"}}
        # A secret-free version of another type is returned unchanged.
        for key, version in camera_versions.items():
            assert by_id[camera_cid][key] == version
