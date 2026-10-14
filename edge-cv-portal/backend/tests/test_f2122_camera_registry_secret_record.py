"""
The camera's own secret and its record, through the routes and the sync
reducer (rtsp-rtmp-stream-cameras tasks 29.4 and 29.5; finding 22, and
the route-involving cases of finding 21 (c)).

Per plan decision D3 this is tasks.md's ``test_camera_registry_secret_
record.py``. Route-level tests against the moto stack: the real
``camera_registry`` and ``stream_credentials`` modules over moto's Secrets
Manager, IAM and IoT data plane, and the real reducer
(``camera_registry.camera_sync._process_report``) for the device's
reports. Every Secrets Manager call the module under test makes is
recorded (operation and SecretId or Name), so each test can check that no
call leaves the device's prefix.

Covered (29.4):

- the finding 22 reproduction: the re-keyed camera's delete schedules
  ``.../portal-x``, and no ``.../cfg-y`` secret is created
- a credential update of ``cfg-y`` writes a new version of
  ``.../portal-x`` and re-tags it; a clear schedules it, a re-add restores
  it; with the recorded secret gone for good, an update creates
  ``.../cfg-y`` and records it; the record survives credential-free
  updates, clears and deletes
- an entry created before the record resolves its reported reference
- out-of-prefix values, each as a recorded, a pending and a reported
  value: ignored with a WARNING that holds no value, and no call outside
  the prefix
- the in-use skip, mirrors (linked or by shape) not counting as users, and
  no ``DeleteSecret`` call when every resolved secret is in use
- an update or delete of a stream mirror, and a re-applied ConflictEvent
  against one, refused with 409 ``CAMERA_SOURCE_ALIAS`` writing nothing; a
  mirror of another type deletes as before
- the carry-forward after an acknowledged clear, and while configured
- the create id check (Requirement 5.2)
- ids that cannot name a secret: the 400 before the grant, the 400 from
  the vault, and a clear that calls nothing
- a create with ``clearCredentials`` schedules nothing and resolves no
  account; account-resolution failures
- the fifth design review's N3: a stale recorded ARN falls through to the
  live secret under the derived name
- no response carries ``credential_secret_arn``, ``alias_of`` or the
  record's value, and no credential value reaches a response, an audit
  row or a log record

Covered (29.5, the cases that involve the routes): a late duplicate of
the alias event; an edited failed create; the Portal's own event reduced
before ``mark_pending``; the removal on any device build.

Requirements: 5.2, 5.8, 5.11, 5.12, 18.3
"""
import itertools
import json
import logging
import os
import re
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-f2122-record"
SETTINGS_TABLE_NAME = "test-settings-f2122-record"
DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"
SHADOW_NAME = "dda-camera-registry"
ACCOUNT = "123456789012"
PREFIX = "dda-portal/stream-camera-credentials"
URL = "rtsp://10.0.4.21:554/Streaming/Channels/101"
RTMP_URL = "rtmp://media.local/live/dock"

# Distinctive credential values: a substring hit anywhere is a real leak.
USERNAME = "f2122rec-UNAME-0b3e"
PASSWORD = "f2122rec-PWD-5d1c"
PASSWORD_2 = "f2122rec-PWD2-77aa"
ALL_SECRETS = (USERNAME, PASSWORD, PASSWORD_2)

ALIAS_ERROR_CODE = "CAMERA_SOURCE_ALIAS"
CANNOT_HOLD = {"error": "this camera id cannot hold Portal-managed "
                        "credentials", "field": "camera_source_id"}

#: A complete ARN of the use case's account and region.
_ARN = re.compile(
    r"arn:aws:secretsmanager:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":secret:(?P<name>[A-Za-z0-9/_+=.@-]+)-[A-Za-z0-9]{6}")

STREAM_DEFAULTS = {
    "RTSP": {"transport": "tcp", "latencyMs": 200, "decoder": "auto",
             "maxFrameDimension": 1920, "stallTimeoutS": 10,
             "credentialsConfigured": False},
    "RTMP": {"decoder": "auto", "maxFrameDimension": 1920,
             "stallTimeoutS": 10, "credentialsConfigured": False},
}

_clock = itertools.count(1_730_000_000_000, 1_000)


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
        secrets=boto3.client("secretsmanager", region_name=REGION),
        iot=boto3.client("iot", region_name=REGION),
        iot_data=boto3.client("iot-data", region_name=REGION),
        audit=aws_stack.tables.audit_log,
    )


class RecordingClient:
    """A Secrets Manager client that records every operation the module
    under test makes, with the SecretId (or the Name of a CreateSecret)."""

    def __init__(self, client, calls):
        self._client = client
        self._calls = calls

    def __getattr__(self, operation):
        method = getattr(self._client, operation)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            self._calls.append(
                (operation, kwargs.get("SecretId", kwargs.get("Name"))))
            return method(*args, **kwargs)
        return call


@pytest.fixture(autouse=True)
def vault_calls(camera_env, monkeypatch):
    """Every Secrets Manager call the routes make in this test."""
    calls = []
    original = camera_env.credentials._client

    def client(name, usecase, region=None, session_name=None):
        real = original(name, usecase, region=region,
                        session_name=session_name)
        return RecordingClient(real, calls) if name == "secretsmanager" \
            else real
    monkeypatch.setattr(camera_env.credentials, "_client", client)
    return calls


@pytest.fixture
def device(camera_env, env):
    """A registered device of a fresh Use_Case (account 123456789012), with
    its IoT thing so shadow writes succeed, and an Operator."""
    return make_device(camera_env, env)


def make_device(camera_env, env, device_id=None):
    usecase_id = env.create_usecase()
    device_id = device_id or f"thing-f2122r-{uuid.uuid4().hex[:12]}"
    camera_env.iot.create_thing(thingName=device_id)
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })
    return SimpleNamespace(device_id=device_id, usecase_id=usecase_id,
                           operator=env.make_user(role="Operator"))


# ---------------------------------------------------------------------------
# Route, shadow, vault and registry helpers
# ---------------------------------------------------------------------------

def make_event(method, device_id, user, sub_path="", body=None,
               path_parameters=None):
    if path_parameters is None:
        path_parameters = {"id": device_id}
        if sub_path and sub_path not in ("/refresh", "/conflicts"):
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


def invoke(camera_env, method, device, sub_path="", body=None,
           path_parameters=None):
    """(status, parsed body, raw body) of one call by the Operator."""
    response = camera_env.module.handler(
        make_event(method, device.device_id, device.operator, sub_path,
                   body, path_parameters), None)
    return (response["statusCode"], json.loads(response["body"]),
            response["body"])


def stream_body(csid=None, credentials=None, source_type="RTSP", **params):
    body = {"name": f"camera {csid or 'new'}", "type": source_type,
            "params": ({"url": URL, "transport": "tcp", **params}
                       if source_type == "RTSP"
                       else {"url": RTMP_URL, **params})}
    if csid is not None:
        body["camera_source_id"] = csid
    if credentials is not None:
        body["credentials"] = credentials
    return body


def camera_body(csid=None):
    body = {"name": f"camera {csid or 'new'}", "type": "Camera",
            "params": {"devicePath": "/dev/video0"}}
    if csid is not None:
        body["camera_source_id"] = csid
    return body


def secret_name(device, csid):
    return f"{PREFIX}/{device.device_id}/{csid}"


def describe(camera_env, secret_id):
    try:
        return camera_env.secrets.describe_secret(SecretId=secret_id)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def current_version(description):
    currents = [version for version, stages in
                (description.get("VersionIdsToStages") or {}).items()
                if "AWSCURRENT" in stages]
    assert len(currents) == 1, description.get("VersionIdsToStages")
    return currents[0]


def secret_value(camera_env, secret_id, version_id):
    return json.loads(camera_env.secrets.get_secret_value(
        SecretId=secret_id, VersionId=version_id)["SecretString"])


def tags_of(description):
    return {tag["Key"]: tag["Value"] for tag in description.get("Tags") or []}


def make_secret(camera_env, name):
    """A secret the test creates itself (a decoy or a legacy secret)."""
    return camera_env.secrets.create_secret(
        Name=name, SecretString=json.dumps({"password": "decoy-value"}))["ARN"]


def vault_state(camera_env, secret_id):
    """What a describe shows of a secret: its versions and deletion."""
    description = describe(camera_env, secret_id)
    if description is None:
        return None
    return (sorted((description.get("VersionIdsToStages") or {}).items()),
            description.get("DeletedDate"))


def shadow_changes(camera_env, device_id):
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
    camera_env.iot_data.update_thing_shadow(
        thingName=device_id, shadowName=SHADOW_NAME,
        payload=json.dumps({"state": {"desired": {"changes": {csid: None}}}}))


def items_of(camera_env, device_id):
    return {item["sk"]: item for item in camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id}).get("Items", [])}


def camera_item(camera_env, device_id, csid):
    return items_of(camera_env, device_id).get(f"CAMERA#{csid}")


def conflicts(camera_env, device_id):
    return [item for sk, item in items_of(camera_env, device_id).items()
            if sk.startswith("CONFLICT#")]


def audit_rows(camera_env, device_id):
    return [row for row in camera_env.audit.scan().get("Items", [])
            if (row.get("details") or {}).get("device_id") == device_id]


def seed(camera_env, device, csid, source_type="RTSP", **attributes):
    """A registry camera item, stored as the routes or the reducer leave
    it (a synced cfg- stream camera by default)."""
    item = {
        "device_id": device.device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": device.usecase_id,
        "name": f"camera {csid}", "type": source_type,
        "params": ({"url": URL, "transport": "tcp"} if source_type == "RTSP"
                   else {"devicePath": "/dev/video0"}),
        "capabilities": {}, "origin": "edge-configured", "version": 1,
        "absent": False, "sync_status": "synced",
        "last_reported_at": 1_700_000_000_000,
    }
    item.update(attributes)
    camera_env.registry.put_item(Item=item)
    return item


def reduce(camera_env, device, cameras, failures=None):
    """Reduce one merged reported state with the real reducer, as the SQS
    ingest and the refresh route do."""
    reported = {"schemaVersion": 1, "reportedAt": next(_clock),
                "cameras": cameras}
    if failures is not None:
        reported["failures"] = failures
    camera_env.module.camera_sync._process_report(
        device.device_id, reported, usecase_id=device.usecase_id)


def reported_camera(change, version=1, ack=None):
    """The camera a device reports for a change it applied: the stream
    settings completed with the device defaults, the credential keys
    echoed (inventory.stream_params), origin edge-configured."""
    source_type = change["type"]
    params = dict(change.get("params") or {})
    if source_type in STREAM_DEFAULTS:
        params = {**STREAM_DEFAULTS[source_type], **params}
    camera = {"version": version, "name": change["name"],
              "type": source_type, "origin": "edge-configured",
              "params": params, "capabilities": {}}
    if ack is not None:
        camera["ack"] = ack
    return camera


def create(camera_env, device, csid, credentials=None, source_type="RTSP"):
    """A Portal create through the route; returns its desired change."""
    body = (stream_body(csid, credentials, source_type)
            if source_type in STREAM_DEFAULTS else camera_body(csid))
    status, response, _ = invoke(camera_env, "POST", device, body=body)
    assert status == 201, response
    change = shadow_changes(camera_env, device.device_id)[
        response["camera_source_id"]]
    return SimpleNamespace(csid=response["camera_source_id"], change=change,
                           change_id=response["portal_change_id"])


def rekey(camera_env, device, portal_csid="portal-x", cfg_csid="cfg-y",
          credentials=None, source_type="RTSP"):
    """A create, acknowledged the way the device does it: one report with
    the created camera under ``cfg_csid`` and its mirror under
    ``portal_csid``, then the report that retires the mirror."""
    if credentials is None and source_type in STREAM_DEFAULTS:
        credentials = {"username": USERNAME, "password": PASSWORD}
    created = create(camera_env, device, portal_csid,
                     credentials if source_type in STREAM_DEFAULTS else None,
                     source_type)
    camera = reported_camera(created.change, ack=created.change_id)
    alias_event = {cfg_csid: camera, portal_csid: dict(camera)}
    reduce(camera_env, device, alias_event)
    mirror = camera_item(camera_env, device.device_id, portal_csid)
    reduce(camera_env, device, {cfg_csid: camera})
    consume_change(camera_env, device.device_id, portal_csid)
    reference = (created.change.get("params") or {}).get("credentialRef")
    return SimpleNamespace(
        created=created, camera=camera, mirror=mirror,
        alias_event=alias_event, reference=reference,
        arn=(reference or {}).get("secretArn"),
        name=secret_name(device, portal_csid))


def in_prefix(device, secret_id):
    """Whether one SecretId names the device's prefix plus one segment:
    a complete ARN of the use case's account and region, or a name."""
    match = _ARN.fullmatch(secret_id) if isinstance(secret_id, str) else None
    if match:
        if (match["account"], match["region"]) != (ACCOUNT, REGION):
            return False
        name = match["name"]
    else:
        name = secret_id
    prefix = f"{PREFIX}/{device.device_id}/"
    rest = name[len(prefix):] if isinstance(name, str) \
        and name.startswith(prefix) else ""
    return bool(re.fullmatch(r"[A-Za-z0-9_+=.@-]+", rest))


def assert_calls_in_prefix(vault_calls, device):
    for operation, secret_id in vault_calls:
        assert in_prefix(device, secret_id), \
            f"{operation} named {secret_id!r}, outside the device's prefix"


def deletes(vault_calls):
    return [secret_id for operation, secret_id in vault_calls
            if operation == "delete_secret"]


def warnings_of(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno >= logging.WARNING]


def assert_no_secret_in(text, where):
    for value in ALL_SECRETS:
        assert value not in text, f"a credential value leaked into {where}"


# ---------------------------------------------------------------------------
# Finding 22: the record follows the camera through the re-key
# ---------------------------------------------------------------------------

class TestFinding22:

    def test_deleting_the_rekeyed_camera_schedules_its_original_secret(
            self, camera_env, device, vault_calls):
        rekeyed = rekey(camera_env, device)
        # The link (29.5): the created entry holds the record, and the
        # mirror pointed at it until the device retired it.
        created = camera_item(camera_env, device.device_id, "cfg-y")
        assert created["credential_secret_arn"] == rekeyed.arn
        assert rekeyed.mirror["alias_of"] == "cfg-y"
        assert camera_item(camera_env, device.device_id, "portal-x") is None

        status, body, _ = invoke(camera_env, "DELETE", device,
                                 sub_path="/cfg-y")
        assert status == 200, body
        assert describe(camera_env, rekeyed.arn).get(
            "DeletedDate") is not None
        assert describe(camera_env, secret_name(device, "cfg-y")) is None
        assert deletes(vault_calls)[0] == rekeyed.arn
        assert_calls_in_prefix(vault_calls, device)

    def test_a_credential_update_writes_into_the_original_secret(
            self, camera_env, device, vault_calls):
        rekeyed = rekey(camera_env, device)
        first = current_version(describe(camera_env, rekeyed.arn))
        del vault_calls[:]

        status, body, raw = invoke(
            camera_env, "PUT", device, sub_path="/cfg-y",
            body=stream_body(credentials={"password": PASSWORD_2}))
        assert status == 200, body
        assert_no_secret_in(raw, "the update response")
        description = describe(camera_env, rekeyed.arn)
        second = current_version(description)
        assert second != first
        assert secret_value(camera_env, rekeyed.arn, second) == {
            "password": PASSWORD_2}
        assert tags_of(description)["dda-portal:camera_source_id"] == "cfg-y"
        reference = shadow_changes(camera_env, device.device_id)["cfg-y"][
            "params"]["credentialRef"]
        assert reference == {"secretArn": rekeyed.arn, "versionId": second}
        # It stopped at the first secret that exists: .../cfg-y is never
        # described, let alone created.
        assert describe(camera_env, secret_name(device, "cfg-y")) is None
        assert secret_name(device, "cfg-y") not in [
            secret_id for _, secret_id in vault_calls]
        assert camera_item(camera_env, device.device_id, "cfg-y")[
            "credential_secret_arn"] == rekeyed.arn
        assert_calls_in_prefix(vault_calls, device)

    def test_a_clear_schedules_the_secret_and_a_readd_restores_it(
            self, camera_env, device, vault_calls):
        rekeyed = rekey(camera_env, device)
        clear = stream_body()
        clear["clearCredentials"] = True
        status, body, _ = invoke(camera_env, "PUT", device,
                                 sub_path="/cfg-y", body=clear)
        assert status == 200, body
        assert describe(camera_env, rekeyed.arn).get(
            "DeletedDate") is not None
        entry = camera_item(camera_env, device.device_id, "cfg-y")
        assert entry["credential_secret_arn"] == rekeyed.arn

        status, body, _ = invoke(
            camera_env, "PUT", device, sub_path="/cfg-y",
            body=stream_body(credentials={"password": PASSWORD_2}))
        assert status == 200, body
        description = describe(camera_env, rekeyed.arn)
        assert description.get("DeletedDate") is None
        assert secret_value(camera_env, rekeyed.arn,
                            current_version(description)) == {
            "password": PASSWORD_2}
        assert describe(camera_env, secret_name(device, "cfg-y")) is None
        assert camera_item(camera_env, device.device_id, "cfg-y")[
            "credential_secret_arn"] == rekeyed.arn
        assert_calls_in_prefix(vault_calls, device)

    def test_with_the_recorded_secret_gone_an_update_creates_and_records(
            self, camera_env, device, vault_calls):
        rekeyed = rekey(camera_env, device)
        camera_env.secrets.delete_secret(SecretId=rekeyed.arn,
                                         ForceDeleteWithoutRecovery=True)

        status, body, _ = invoke(
            camera_env, "PUT", device, sub_path="/cfg-y",
            body=stream_body(credentials={"password": PASSWORD_2}))
        assert status == 200, body
        created = describe(camera_env, secret_name(device, "cfg-y"))
        assert created is not None
        assert tags_of(created)["dda-portal:camera_source_id"] == "cfg-y"
        assert camera_item(camera_env, device.device_id, "cfg-y")[
            "credential_secret_arn"] == created["ARN"]
        assert shadow_changes(camera_env, device.device_id)["cfg-y"][
            "params"]["credentialRef"]["secretArn"] == created["ARN"]
        assert_calls_in_prefix(vault_calls, device)

    def test_the_record_survives_updates_clears_and_deletes_without_credentials(
            self, camera_env, device):
        rekeyed = rekey(camera_env, device)

        def record():
            return camera_item(camera_env, device.device_id, "cfg-y").get(
                "credential_secret_arn")

        assert invoke(camera_env, "PUT", device, sub_path="/cfg-y",
                      body=stream_body(latencyMs=300))[0] == 200
        assert record() == rekeyed.arn
        clear = stream_body()
        clear["clearCredentials"] = True
        assert invoke(camera_env, "PUT", device, sub_path="/cfg-y",
                      body=clear)[0] == 200
        assert record() == rekeyed.arn
        # The device's reports keep it too.
        reduce(camera_env, device, {"cfg-y": reported_camera(
            {"type": "RTSP", "name": "camera new",
             "params": {"url": URL, "credentialsConfigured": False}},
            version=2)})
        assert record() == rekeyed.arn
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-y")[0] == 200
        assert record() == rekeyed.arn

    def test_an_entry_from_before_the_record_resolves_its_reported_reference(
            self, camera_env, device, vault_calls):
        legacy_arn = make_secret(camera_env, secret_name(device, "portal-old"))
        seed(camera_env, device, "cfg-l", params={
            "url": URL, "transport": "tcp",
            "credentialRef": {"secretArn": legacy_arn, "versionId": "v-old"},
            "credentialsConfigured": True,
            "credentialsUpdatedAt": 1_700_000_000_000})

        status, body, _ = invoke(
            camera_env, "PUT", device, sub_path="/cfg-l",
            body=stream_body(credentials={"password": PASSWORD}))
        assert status == 200, body
        description = describe(camera_env, legacy_arn)
        assert secret_value(camera_env, legacy_arn,
                            current_version(description)) == {
            "password": PASSWORD}
        assert tags_of(description)["dda-portal:camera_source_id"] == "cfg-l"
        assert describe(camera_env, secret_name(device, "cfg-l")) is None
        assert camera_item(camera_env, device.device_id, "cfg-l")[
            "credential_secret_arn"] == legacy_arn

        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-l")[0] == 200
        assert describe(camera_env, legacy_arn).get("DeletedDate") is not None
        assert_calls_in_prefix(vault_calls, device)

    def test_a_stale_recorded_arn_falls_through_to_the_live_derived_secret(
            self, camera_env, device, vault_calls):
        """Fifth design review, N3: duplicates are compared by SecretId,
        so a record whose ARN no longer exists does not hide the live
        secret of the same name."""
        name = secret_name(device, "cfg-n3")
        stale = make_secret(camera_env, name)
        camera_env.secrets.delete_secret(SecretId=stale,
                                         ForceDeleteWithoutRecovery=True)
        live = make_secret(camera_env, name)
        assert live != stale
        seed(camera_env, device, "cfg-n3", credential_secret_arn=stale)

        status, body, _ = invoke(
            camera_env, "PUT", device, sub_path="/cfg-n3",
            body=stream_body(credentials={"password": PASSWORD}))
        assert status == 200, body
        description = describe(camera_env, live)
        assert secret_value(camera_env, live,
                            current_version(description)) == {
            "password": PASSWORD}
        assert camera_item(camera_env, device.device_id, "cfg-n3")[
            "credential_secret_arn"] == live

        del vault_calls[:]
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-n3")[0] == 200
        assert describe(camera_env, live).get("DeletedDate") is not None
        assert live in deletes(vault_calls)
        assert_calls_in_prefix(vault_calls, device)


# ---------------------------------------------------------------------------
# Out-of-prefix values (Requirement 5.8 bullet 3)
# ---------------------------------------------------------------------------

def _bad_values(camera_env, env, device):
    """Each kind of value the routes must ignore, built so that a wrong
    acceptance would reach a real secret: the decoys exist."""
    other = make_device(camera_env, env)
    sibling = f"{device.device_id}-b"
    decoys = {
        "other": make_secret(camera_env, f"{PREFIX}/{other.device_id}/decoy"),
        "partial": make_secret(camera_env,
                               f"{PREFIX}/{device.device_id}/decoy-partial"),
        "nested": make_secret(camera_env,
                              f"{PREFIX}/{device.device_id}/nested/decoy"),
        "newline": make_secret(camera_env,
                               f"{PREFIX}/{device.device_id}/decoy-nl"),
        "sibling": make_secret(camera_env, f"{PREFIX}/{sibling}/decoy"),
    }
    values = {
        "other_device_name": f"{PREFIX}/{other.device_id}/decoy",
        "other_device_arn": decoys["other"],
        "partial_arn": (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:"
                        f"{PREFIX}/{device.device_id}/decoy-partial"),
        "extra_segment": decoys["nested"],
        "trailing_newline": decoys["newline"] + "\n",
        "other_account": ("arn:aws:secretsmanager:us-east-1:111122223333:"
                          f"secret:{PREFIX}/{device.device_id}/decoy-AbCdEf"),
        "other_region": (f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:"
                         f"secret:{PREFIX}/{device.device_id}/decoy-AbCdEf"),
        "prefix_sibling": decoys["sibling"],
    }
    return values, decoys


BAD_KINDS = ("other_device_name", "other_device_arn", "partial_arn",
             "extra_segment", "trailing_newline", "other_account",
             "other_region", "prefix_sibling")


def _seed_with(camera_env, device, csid, position, value):
    reference = {"secretArn": value, "versionId": "v-bad"}
    if position == "record":
        return seed(camera_env, device, csid, credential_secret_arn=value)
    if position == "reported":
        return seed(camera_env, device, csid, params={
            "url": URL, "transport": "tcp", "credentialRef": reference,
            "credentialsConfigured": True})
    return seed(camera_env, device, csid, sync_status="pending",
                portal_change_id="pc-pending", pending_content={
                    "op": "update", "name": "pending", "type": "RTSP",
                    "params": {"url": URL, "credentialRef": reference,
                               "credentialsConfigured": True}})


@pytest.mark.parametrize("position", ["record", "pending", "reported"])
@pytest.mark.parametrize("kind", BAD_KINDS)
def test_out_of_prefix_values_are_ignored_with_a_warning_without_the_value(
        camera_env, env, device, vault_calls, caplog, kind, position):
    values, decoys = _bad_values(camera_env, env, device)
    value = values[kind]
    before = {key: vault_state(camera_env, arn) for key, arn in decoys.items()}
    _seed_with(camera_env, device, "cfg-u", position, value)
    _seed_with(camera_env, device, "cfg-d", position, value)
    caplog.clear()
    del vault_calls[:]

    # An update with credentials ignores the value and creates the secret
    # named after its own id.
    status, body, _ = invoke(
        camera_env, "PUT", device, sub_path="/cfg-u",
        body=stream_body(credentials={"password": PASSWORD}))
    assert status == 200, body
    created = describe(camera_env, secret_name(device, "cfg-u"))
    assert created is not None
    assert shadow_changes(camera_env, device.device_id)["cfg-u"]["params"][
        "credentialRef"]["secretArn"] == created["ARN"]

    # A delete ignores it too: only the derived name is scheduled.
    status, body, _ = invoke(camera_env, "DELETE", device, sub_path="/cfg-d")
    assert status == 200, body
    assert deletes(vault_calls) == [secret_name(device, "cfg-d")]

    assert_calls_in_prefix(vault_calls, device)
    assert value not in [secret_id for _, secret_id in vault_calls]
    for key, arn in decoys.items():
        assert vault_state(camera_env, arn) == before[key], \
            f"the {key} decoy secret was touched"
    messages = warnings_of(caplog)
    for csid in ("cfg-u", "cfg-d"):
        assert any(f"{position} secret candidate" in message
                   and repr(csid) in message for message in messages), \
            messages
    for record in caplog.records:
        text = record.getMessage()
        assert value not in text and value.strip() not in text, \
            "a rejected candidate's value reached a log record"


# ---------------------------------------------------------------------------
# The in-use skip and create mirrors
# ---------------------------------------------------------------------------

class TestInUseAndMirrors:

    def test_a_secret_another_live_entry_uses_is_kept_until_the_last_goes(
            self, camera_env, device, vault_calls, caplog):
        rekeyed = rekey(camera_env, device, "portal-s", "cfg-s1")
        # Another camera of the device reports the same reference.
        seed(camera_env, device, "cfg-s2", params={
            "url": URL, "transport": "tcp",
            "credentialRef": rekeyed.reference,
            "credentialsConfigured": True})
        del vault_calls[:]
        caplog.set_level(logging.INFO)

        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-s1")[0] == 200
        assert describe(camera_env, rekeyed.arn).get("DeletedDate") is None
        assert rekeyed.arn not in deletes(vault_calls)
        assert any("still uses it" in record.getMessage()
                   and record.levelno == logging.INFO
                   for record in caplog.records)

        # cfg-s1 is pending its delete now, so it no longer counts.
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-s2")[0] == 200
        assert describe(camera_env, rekeyed.arn).get(
            "DeletedDate") is not None
        assert_calls_in_prefix(vault_calls, device)

    def test_no_delete_secret_call_when_every_resolved_secret_is_in_use(
            self, camera_env, device, vault_calls):
        shared = make_secret(camera_env, secret_name(device, "cfg-a"))
        seed(camera_env, device, "cfg-a", credential_secret_arn=shared)
        # cfg-b resolves the same secret, which is also cfg-a's derived
        # name: both of cfg-a's ids are in use, by name included.
        seed(camera_env, device, "cfg-b", params={
            "url": URL, "transport": "tcp",
            "credentialRef": {"secretArn": shared, "versionId": "v1"},
            "credentialsConfigured": True})
        del vault_calls[:]

        clear = stream_body()
        clear["clearCredentials"] = True
        assert invoke(camera_env, "PUT", device, sub_path="/cfg-a",
                      body=clear)[0] == 200
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-a")[0] == 200
        assert deletes(vault_calls) == []
        assert describe(camera_env, shared).get("DeletedDate") is None

    def test_a_linked_mirror_does_not_count_as_a_user(
            self, camera_env, device, vault_calls):
        created = create(camera_env, device, "portal-m",
                         {"password": PASSWORD})
        camera = reported_camera(created.change, ack=created.change_id)
        reduce(camera_env, device, {"cfg-m": camera,
                                    "portal-m": dict(camera)})
        mirror = camera_item(camera_env, device.device_id, "portal-m")
        assert mirror["alias_of"] == "cfg-m"
        arn = created.change["params"]["credentialRef"]["secretArn"]
        assert mirror["credential_secret_arn"] == arn

        # The mirror row is still shown, and resolves the secret, but owns
        # none: deleting the created camera schedules it.
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-m")[0] == 200
        assert describe(camera_env, arn).get("DeletedDate") is not None

    def test_a_shape_mirror_does_not_count_as_a_user(
            self, camera_env, device, vault_calls):
        rekeyed = rekey(camera_env, device, "portal-z", "cfg-z")
        # A late duplicate of the alias event brings the mirror back
        # without alias_of (29.5).
        reduce(camera_env, device, rekeyed.alias_event)
        mirror = camera_item(camera_env, device.device_id, "portal-z")
        assert mirror["sync_status"] == "synced"
        assert "alias_of" not in mirror

        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-z")[0] == 200
        assert describe(camera_env, rekeyed.arn).get(
            "DeletedDate") is not None

    @pytest.mark.parametrize("linked", [True, False])
    def test_an_update_or_delete_of_a_stream_mirror_is_refused(
            self, camera_env, device, vault_calls, linked):
        created = create(camera_env, device, "portal-w",
                         {"password": PASSWORD})
        camera = reported_camera(created.change, ack=created.change_id)
        reduce(camera_env, device, {"cfg-w": camera,
                                    "portal-w": dict(camera)})
        if not linked:
            mirror = camera_item(camera_env, device.device_id, "portal-w")
            mirror.pop("alias_of")
            camera_env.registry.put_item(Item=mirror)
        consume_change(camera_env, device.device_id, "portal-w")
        items_before = items_of(camera_env, device.device_id)
        audit_before = len(audit_rows(camera_env, device.device_id))
        del vault_calls[:]

        responses = [
            invoke(camera_env, "PUT", device, sub_path="/portal-w",
                   body=stream_body(credentials={"password": PASSWORD_2})),
            invoke(camera_env, "PUT", device, sub_path="/portal-w",
                   body=stream_body(latencyMs=300)),
            invoke(camera_env, "DELETE", device, sub_path="/portal-w"),
        ]
        for status, body, raw in responses:
            assert status == 409, body
            assert body["code"] == ALIAS_ERROR_CODE
            assert body["camera_source_id"] == "portal-w"
            if linked:
                assert body["created_camera_source_id"] == "cfg-w"
            else:
                assert "created_camera_source_id" not in body
            assert "alias_of" not in raw
            assert_no_secret_in(raw, "the 409")
        assert items_of(camera_env, device.device_id) == items_before
        assert shadow_changes(camera_env, device.device_id) == {}
        assert len(audit_rows(camera_env, device.device_id)) == audit_before
        assert vault_calls == []

    def test_a_mirror_of_another_type_deletes_as_before(
            self, camera_env, device):
        created = create(camera_env, device, "portal-c",
                         source_type="Camera")
        camera = reported_camera(created.change, ack=created.change_id)
        reduce(camera_env, device, {"cfg-c": camera,
                                    "portal-c": dict(camera)})
        mirror = camera_item(camera_env, device.device_id, "portal-c")
        assert mirror["sync_status"] == "synced"
        assert "alias_of" not in mirror
        assert "credential_secret_arn" not in mirror
        consume_change(camera_env, device.device_id, "portal-c")

        status, body, _ = invoke(camera_env, "DELETE", device,
                                 sub_path="/portal-c")
        assert status == 200, body
        assert shadow_changes(camera_env, device.device_id)["portal-c"][
            "op"] == "delete"

    def test_reapplying_a_conflict_against_a_stream_mirror_is_refused(
            self, camera_env, device, vault_calls):
        created = create(camera_env, device, "portal-r",
                         {"password": PASSWORD})
        camera = reported_camera(created.change, ack=created.change_id)
        reduce(camera_env, device, {"cfg-r": camera,
                                    "portal-r": dict(camera)})
        consume_change(camera_env, device.device_id, "portal-r")
        cid = uuid.uuid4().hex
        camera_env.registry.put_item(Item={
            "device_id": device.device_id, "sk": f"CONFLICT#100#{cid}",
            "usecase_id": device.usecase_id, "camera_source_id": "portal-r",
            "edge_version": {"name": "edge", "type": "RTSP",
                             "params": {"url": URL}},
            "portal_version": {"op": "update", "name": "portal",
                               "type": "RTSP",
                               "params": {"url": URL, "latencyMs": 300}},
            "resolution": "edge-retained", "created_at": 100,
        })
        items_before = items_of(camera_env, device.device_id)
        audit_before = len(audit_rows(camera_env, device.device_id))
        del vault_calls[:]

        status, body, _ = invoke(
            camera_env, "POST", device,
            sub_path=f"/conflicts/{cid}/reapply",
            path_parameters={"id": device.device_id, "cid": cid})
        assert status == 409, body
        assert body["code"] == ALIAS_ERROR_CODE
        assert body["created_camera_source_id"] == "cfg-r"
        assert items_of(camera_env, device.device_id) == items_before
        assert shadow_changes(camera_env, device.device_id) == {}
        assert len(audit_rows(camera_env, device.device_id)) == audit_before
        assert vault_calls == []


# ---------------------------------------------------------------------------
# Updates that do not mention credentials (Requirement 5.8 bullet 5)
# ---------------------------------------------------------------------------

class TestCarryForward:

    def test_a_plain_edit_after_an_acknowledged_clear_delivers_no_reference(
            self, camera_env, device):
        """Second design review, finding 2: the merged report still holds
        the old credentialRef, with credentialsConfigured false."""
        rekeyed = rekey(camera_env, device)
        seed(camera_env, device, "cfg-y",
             credential_secret_arn=rekeyed.arn, version=2,
             params={**rekeyed.camera["params"],
                     "credentialsConfigured": False})

        status, body, _ = invoke(camera_env, "PUT", device,
                                 sub_path="/cfg-y",
                                 body=stream_body(latencyMs=300))
        assert status == 200, body
        params = shadow_changes(camera_env, device.device_id)["cfg-y"][
            "params"]
        # A device applying it fetches nothing: it carries no reference.
        assert "credentialRef" not in params
        assert "credentialsConfigured" not in params
        pending = camera_item(camera_env, device.device_id, "cfg-y")[
            "pending_content"]["params"]
        assert "credentialRef" not in pending

    def test_while_configured_the_reported_reference_is_carried(
            self, camera_env, device):
        rekeyed = rekey(camera_env, device)
        status, body, _ = invoke(camera_env, "PUT", device,
                                 sub_path="/cfg-y",
                                 body=stream_body(latencyMs=300))
        assert status == 200, body
        params = shadow_changes(camera_env, device.device_id)["cfg-y"][
            "params"]
        assert params["credentialRef"] == rekeyed.reference
        assert params["credentialsConfigured"] is True


# ---------------------------------------------------------------------------
# The create id check (Requirement 5.2)
# ---------------------------------------------------------------------------

INVALID_CREATE_IDS = ["a/b", "cfg-x", "disc-x", "arv-x",
                      "static-image-camera", "static-video-camera", "",
                      "cam-1\n", ".", "..", 0, "x" * 129, "cam 1", True,
                      ["cam-1"]]


class TestCreateIdCheck:

    @pytest.mark.parametrize("source_type", ["RTSP", "RTMP"])
    @pytest.mark.parametrize("csid", INVALID_CREATE_IDS,
                             ids=lambda value: repr(value)[:24])
    def test_an_invalid_body_id_is_refused_writing_nothing(
            self, camera_env, device, vault_calls, source_type, csid):
        body = stream_body(None, {"password": PASSWORD}, source_type)
        body["camera_source_id"] = csid
        status, response, raw = invoke(camera_env, "POST", device, body=body)
        assert status == 400, response
        assert response["field"] == "camera_source_id"
        if isinstance(csid, str) and len(csid) > 2 and \
                csid not in ("static-image-camera", "static-video-camera"):
            assert json.dumps(csid)[1:-1] not in raw
        assert_no_secret_in(raw, "the 400")
        assert shadow_changes(camera_env, device.device_id) == {}
        assert [sk for sk in items_of(camera_env, device.device_id)
                if sk != "META"] == []
        assert audit_rows(camera_env, device.device_id) == []
        assert vault_calls == []

    @pytest.mark.parametrize("csid", ["cam-1", "portal-x", "y" * 128,
                                      "a_b.c@d+e=f-g"])
    def test_a_valid_body_id_is_accepted(self, camera_env, device, csid):
        status, response, _ = invoke(camera_env, "POST", device,
                                     body=stream_body(csid))
        assert status == 201, response
        assert response["camera_source_id"] == csid

    @pytest.mark.parametrize("present", [False, True])
    def test_a_missing_or_null_id_gets_a_generated_portal_id(
            self, camera_env, device, present):
        body = stream_body()
        if present:
            body["camera_source_id"] = None
        status, response, _ = invoke(camera_env, "POST", device, body=body)
        assert status == 201, response
        assert re.fullmatch(r"portal-[0-9a-f]{12}",
                            response["camera_source_id"])

    @pytest.mark.parametrize("csid,expected", [
        ("cfg-x", "cfg-x"), ("a/b", "a/b"), ("", None)])
    def test_a_non_stream_create_keeps_its_id_handling(
            self, camera_env, device, csid, expected):
        status, response, _ = invoke(camera_env, "POST", device,
                                     body=camera_body(csid))
        assert status == 201, response
        if expected is None:
            assert response["camera_source_id"].startswith("portal-")
        else:
            assert response["camera_source_id"] == expected


# ---------------------------------------------------------------------------
# Ids that cannot name a secret, creates that clear, account resolution
# ---------------------------------------------------------------------------

@pytest.fixture
def grant_calls(camera_env, monkeypatch):
    calls = []
    original = camera_env.credentials.ensure_device_read_grant

    def recording(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(camera_env.credentials, "ensure_device_read_grant",
                        recording)
    return calls


def _legacy_slash_entry(camera_env, device, **attributes):
    """A stream entry an API client created under 'a/b' before the 5.2
    check, never reported (so not a mirror)."""
    return seed(camera_env, device, "a/b", origin="portal-created",
                version=0, sync_status="pending", portal_change_id="pc-old",
                pending_content={"op": "create", "name": "legacy",
                                 "type": "RTSP", "params": {"url": URL}},
                **attributes)


class TestIdsThatCannotNameASecret:

    def test_an_update_with_credentials_is_refused_before_the_grant(
            self, camera_env, device, vault_calls, grant_calls):
        before = _legacy_slash_entry(camera_env, device)
        status, body, raw = invoke(
            camera_env, "PUT", device, sub_path="/a/b",
            body=stream_body(credentials={"password": PASSWORD}))
        assert (status, body) == (400, CANNOT_HOLD)
        assert_no_secret_in(raw, "the 400")
        assert grant_calls == []
        assert vault_calls == []
        assert camera_item(camera_env, device.device_id, "a/b") == before
        assert shadow_changes(camera_env, device.device_id) == {}
        assert audit_rows(camera_env, device.device_id) == []

    def test_with_a_stale_recorded_arn_the_vault_refuses_after_the_grant(
            self, camera_env, device, vault_calls, grant_calls):
        stale = (f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:"
                 f"{secret_name(device, 'gone')}-AbCdEf")
        before = _legacy_slash_entry(camera_env, device,
                                     credential_secret_arn=stale)
        status, body, _ = invoke(
            camera_env, "PUT", device, sub_path="/a/b",
            body=stream_body(credentials={"password": PASSWORD}))
        assert (status, body) == (400, CANNOT_HOLD)
        # The idempotent device read grant is the only write.
        assert len(grant_calls) == 1
        assert vault_calls == [("describe_secret", stale)]
        assert camera_item(camera_env, device.device_id, "a/b") == before
        assert shadow_changes(camera_env, device.device_id) == {}
        assert audit_rows(camera_env, device.device_id) == []

    def test_a_clear_makes_no_delete_secret_call(
            self, camera_env, device, vault_calls):
        _legacy_slash_entry(camera_env, device)
        clear = stream_body()
        clear["clearCredentials"] = True
        status, body, _ = invoke(camera_env, "PUT", device, sub_path="/a/b",
                                 body=clear)
        assert status == 200, body
        assert vault_calls == []


@pytest.fixture
def account_lookups(camera_env, monkeypatch):
    """Record every account resolution, then let it run."""
    calls = []
    original = camera_env.credentials._usecase_account_id

    def recording(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(camera_env.credentials, "_usecase_account_id",
                        recording)
    return calls


def test_a_create_that_clears_credentials_schedules_nothing_or_resolves_no_account(
        camera_env, device, vault_calls, account_lookups):
    body = stream_body("cam-c")
    body["clearCredentials"] = True
    status, response, _ = invoke(camera_env, "POST", device, body=body)
    assert status == 201, response
    assert vault_calls == []
    assert account_lookups == []


class TestAccountResolution:

    @pytest.fixture
    def unresolvable(self, camera_env, monkeypatch):
        def fail(code):
            def lookup(*args, **kwargs):
                raise ClientError({"Error": {"Code": code,
                                             "Message": "no account"}},
                                  "GetCallerIdentity")
            monkeypatch.setattr(camera_env.credentials,
                                "_usecase_account_id", lookup)
        return fail

    def test_a_credentialed_create_still_succeeds(
            self, camera_env, device, unresolvable):
        unresolvable("ExpiredTokenException")
        created = create(camera_env, device, "cam-a", {"password": PASSWORD})
        assert describe(camera_env, secret_name(device, "cam-a")) is not None
        assert camera_item(camera_env, device.device_id, "cam-a")[
            "credential_secret_arn"] == created.change["params"][
            "credentialRef"]["secretArn"]

    @pytest.mark.parametrize("code,expected", [
        ("ExpiredTokenException", 500), ("AccessDeniedException", 409)])
    def test_an_update_with_credentials_fails_writing_nothing(
            self, camera_env, device, vault_calls, unresolvable, code,
            expected):
        create(camera_env, device, "cam-a", {"password": PASSWORD})
        consume_change(camera_env, device.device_id, "cam-a")
        before = items_of(camera_env, device.device_id)
        audit_before = len(audit_rows(camera_env, device.device_id))
        del vault_calls[:]
        unresolvable(code)

        status, body, raw = invoke(
            camera_env, "PUT", device, sub_path="/cam-a",
            body=stream_body(credentials={"password": PASSWORD_2}))
        assert status == expected, body
        if expected == 409:
            assert body["code"] == "STREAM_CREDENTIALS_UNAVAILABLE"
        assert_no_secret_in(raw, "the error response")
        assert vault_calls == []
        assert items_of(camera_env, device.device_id) == before
        assert shadow_changes(camera_env, device.device_id) == {}
        assert len(audit_rows(camera_env, device.device_id)) == audit_before

    def test_a_clear_and_a_delete_schedule_nothing_and_warn(
            self, camera_env, device, vault_calls, unresolvable, caplog):
        create(camera_env, device, "cam-a", {"password": PASSWORD})
        create(camera_env, device, "cam-b", {"password": PASSWORD})
        del vault_calls[:]
        unresolvable("ExpiredTokenException")
        caplog.clear()

        clear = stream_body()
        clear["clearCredentials"] = True
        assert invoke(camera_env, "PUT", device, sub_path="/cam-a",
                      body=clear)[0] == 200
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cam-b")[0] == 200
        assert deletes(vault_calls) == []
        for csid in ("cam-a", "cam-b"):
            assert describe(camera_env, secret_name(device, csid)).get(
                "DeletedDate") is None
        assert sum("Could not resolve the use case's account" in message
                   for message in warnings_of(caplog)) == 2

    def test_a_non_stream_request_never_resolves_the_account(
            self, camera_env, device, account_lookups):
        assert invoke(camera_env, "POST", device,
                      body=camera_body("usb-1"))[0] == 201
        assert invoke(camera_env, "PUT", device, sub_path="/usb-1",
                      body=camera_body())[0] == 200
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/usb-1")[0] == 200
        assert account_lookups == []


# ---------------------------------------------------------------------------
# Nothing leaks into a response, an audit row or a log record
# ---------------------------------------------------------------------------

def test_no_response_carries_the_record_or_the_link_and_nothing_leaks(
        camera_env, device, caplog):
    # INFO is the level the Lambda runs at (camera_registry sets it on the
    # root logger); botocore's own DEBUG request logging is not enabled.
    caplog.set_level(logging.INFO)
    raws = []

    def call(method, sub_path="", body=None, path_parameters=None):
        status, response, raw = invoke(camera_env, method, device, sub_path,
                                       body, path_parameters)
        raws.append(raw)
        return status, response

    status, body = call("POST", body=stream_body(
        "portal-v", {"username": USERNAME, "password": PASSWORD}))
    assert status == 201, body
    change = shadow_changes(camera_env, device.device_id)["portal-v"]
    record = change["params"]["credentialRef"]["secretArn"]
    camera = reported_camera(change, ack=body["portal_change_id"])
    reduce(camera_env, device, {"cfg-v": camera, "portal-v": dict(camera)})
    # The mirror row, linked, is listed.
    assert call("GET")[0] == 200
    assert call("PUT", "/portal-v", stream_body(latencyMs=300))[0] == 409
    reduce(camera_env, device, {"cfg-v": camera})
    # A Portal update of cfg-v, overridden by the device's old content,
    # leaves a ConflictEvent whose portal version holds the reference.
    assert call("PUT", "/cfg-v", stream_body(
        credentials={"password": PASSWORD_2}))[0] == 200
    reduce(camera_env, device, {"cfg-v": {**camera, "version": 2,
                                          "name": "edited at the station"}})
    assert conflicts(camera_env, device.device_id)
    assert call("GET", "/conflicts")[0] == 200
    camera_env.iot_data.update_thing_shadow(
        thingName=device.device_id, shadowName=SHADOW_NAME,
        payload=json.dumps({"state": {"reported": {
            "schemaVersion": 1, "reportedAt": next(_clock),
            "cameras": {"cfg-v": {**camera, "version": 3}}}}}))
    assert call("POST", "/refresh")[0] == 200
    clear = stream_body()
    clear["clearCredentials"] = True
    assert call("PUT", "/cfg-v", clear)[0] == 200
    assert call("GET")[0] == 200
    assert call("DELETE", "/cfg-v")[0] == 200
    assert call("GET")[0] == 200

    assert camera_item(camera_env, device.device_id, "cfg-v")[
        "credential_secret_arn"] == record
    for raw in raws:
        assert "credential_secret_arn" not in raw
        assert "alias_of" not in raw
        assert record not in raw
        assert_no_secret_in(raw, "a response")
    assert_no_secret_in(json.dumps(audit_rows(camera_env, device.device_id),
                                   default=str), "the audit log")
    assert_no_secret_in(json.dumps(items_of(camera_env, device.device_id),
                                   default=str), "the registry")
    for log_record in caplog.records:
        assert_no_secret_in(log_record.getMessage(), "a log record")


# ---------------------------------------------------------------------------
# 29.5: the reducer and the routes together
# ---------------------------------------------------------------------------

class TestReducerWithTheRoutes:

    def test_a_late_duplicate_brings_the_mirror_back_without_alias_of(
            self, camera_env, device):
        rekeyed = rekey(camera_env, device)
        reduce(camera_env, device, rekeyed.alias_event)
        mirror = camera_item(camera_env, device.device_id, "portal-x")
        assert mirror["sync_status"] == "synced"
        assert "alias_of" not in mirror

        for method, body in (("PUT", stream_body(latencyMs=300)),
                             ("DELETE", None)):
            status, response, _ = invoke(camera_env, method, device,
                                         sub_path="/portal-x", body=body)
            assert status == 409, response
            assert response["code"] == ALIAS_ERROR_CODE
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/cfg-y")[0] == 200
        assert describe(camera_env, rekeyed.arn).get(
            "DeletedDate") is not None

    @pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
    def test_an_edited_failed_create_stays_pending_then_its_delete_removes_it(
            self, camera_env, device, source_type):
        """Second design review, finding 1."""
        stream = source_type == "RTSP"
        created = create(camera_env, device, "portal-e",
                         {"password": PASSWORD} if stream else None,
                         source_type)
        create_failure = {"portal-e": {"reason": "credential retrieval "
                                                 "failed: AccessDenied",
                                       "portalChangeId": created.change_id}}
        reduce(camera_env, device, {}, create_failure)
        assert camera_item(camera_env, device.device_id, "portal-e")[
            "sync_status"] == "failed"

        edit = stream_body(latencyMs=300) if stream else camera_body()
        status, body, _ = invoke(camera_env, "PUT", device,
                                 sub_path="/portal-e", body=edit)
        assert status == 200, body
        update_id = body["portal_change_id"]
        # The Portal's own documents event: the create's failure still
        # pins the entry, which is not pending a delete.
        reduce(camera_env, device, {}, create_failure)
        entry = camera_item(camera_env, device.device_id, "portal-e")
        assert entry["sync_status"] == "pending"
        assert conflicts(camera_env, device.device_id) == []
        # The device refuses the update of a camera it never created.
        refusal = {"portal-e": {"reason": "discovery-managed",
                                "portalChangeId": update_id}}
        reduce(camera_env, device, {}, refusal)
        entry = camera_item(camera_env, device.device_id, "portal-e")
        assert entry["sync_status"] == "failed"
        assert entry["failure_reason"] == "discovery-managed"

        assert invoke(camera_env, "DELETE", device,
                      sub_path="/portal-e")[0] == 200
        reduce(camera_env, device, {}, refusal)
        assert camera_item(camera_env, device.device_id, "portal-e") is None
        if stream:
            arn = created.change["params"]["credentialRef"]["secretArn"]
            assert describe(camera_env, arn).get("DeletedDate") is not None

    @pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
    def test_the_portal_event_reduced_before_mark_pending(
            self, camera_env, device, monkeypatch, source_type):
        created = create(camera_env, device, "portal-o",
                         source_type=source_type)
        create_failure = {"portal-o": {"reason": "the create failed",
                                       "portalChangeId": created.change_id}}
        reduce(camera_env, device, {}, create_failure)

        # The Portal's own documents event for the delete is reduced
        # before mark_pending is stored.
        original = camera_env.module.mark_pending

        def event_first(*args, **kwargs):
            reduce(camera_env, device, {}, create_failure)
            return original(*args, **kwargs)
        monkeypatch.setattr(camera_env.module, "mark_pending", event_first)
        status, body, _ = invoke(camera_env, "DELETE", device,
                                 sub_path="/portal-o")
        assert status == 200, body
        monkeypatch.setattr(camera_env.module, "mark_pending", original)
        first_delete = body["portal_change_id"]
        assert camera_item(camera_env, device.device_id, "portal-o")[
            "sync_status"] == "pending"

        # An older device refuses the delete: that failure is this
        # delete's, so it pins the entry, which turns failed.
        refusal = {"portal-o": {"reason": "discovery-managed",
                                "portalChangeId": first_delete}}
        reduce(camera_env, device, {}, refusal)
        entry = camera_item(camera_env, device.device_id, "portal-o")
        assert entry["sync_status"] == "failed"
        assert entry["failure_reason"] == "discovery-managed"

        # A second delete removes it at the Portal's own event.
        assert invoke(camera_env, "DELETE", device,
                      sub_path="/portal-o")[0] == 200
        reduce(camera_env, device, {}, refusal)
        assert camera_item(camera_env, device.device_id, "portal-o") is None

    @pytest.mark.parametrize("source_type", ["RTSP", "Camera"])
    def test_removal_on_any_build_discards_an_older_devices_refusal(
            self, camera_env, device, source_type):
        created = create(camera_env, device, "portal-g",
                         source_type=source_type)
        create_failure = {"portal-g": {"reason": "the create failed",
                                       "portalChangeId": created.change_id}}
        reduce(camera_env, device, {}, create_failure)
        status, body, _ = invoke(camera_env, "DELETE", device,
                                 sub_path="/portal-g")
        assert status == 200, body

        reduce(camera_env, device, {}, create_failure)
        assert camera_item(camera_env, device.device_id, "portal-g") is None
        # An older device then refuses the delete; the entry is gone, so
        # the failure is discarded.
        reduce(camera_env, device, {}, {"portal-g": {
            "reason": "discovery-managed",
            "portalChangeId": body["portal_change_id"]}})
        assert camera_item(camera_env, device.device_id, "portal-g") is None
