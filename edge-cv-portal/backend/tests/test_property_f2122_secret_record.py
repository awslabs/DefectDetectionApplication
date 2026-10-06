"""
Property-based tests for the camera's own secret in the Camera_Registry
(rtsp-rtmp-stream-cameras task 29.4).

**Feature: rtsp-rtmp-stream-cameras, Property 31: The Portal acts on the camera's own secret, and never on one still in use**

*For any* registry entries of a device, sequence of reports (including the
create re-key, and duplicated or replayed documents events), recorded,
pending and reported values of any shape, and credential create, update,
clear or delete of one camera:

- Every secret the Camera_Registry describes, writes, restores or
  schedules for deletion SHALL be named
  ``dda-portal/stream-camera-credentials/{device}/`` plus one segment, in
  the use case's account and region. Every recorded, pending or reported
  value it used SHALL be a complete ARN.
- An update SHALL write a new version of the first resolved secret that
  exists. It SHALL create a secret only when none exists, and then only
  ``secret_name(device, id)``, and only when ``device_secret_id`` accepts
  that name. Otherwise it SHALL return 400 with ``field:
  camera_source_id`` and write nothing to the vault, the shadow, the
  registry or the audit log.
- A clearing update or a delete SHALL schedule exactly the camera's
  resolved secrets that no other stream entry of the device resolves,
  ignoring entries pending a delete and create mirrors, whether linked by
  ``alias_of`` or recognized by shape. A create SHALL schedule nothing. An
  update or delete of a stream mirror SHALL write nothing.
- An update that mentions neither credentials nor a clear SHALL deliver a
  reported Credential_Reference only while the entry reports
  ``credentialsConfigured: true``, and SHALL deliver no reference while
  the entry's pending change is a clear.
- ``credential_secret_arn`` SHALL survive every report and the re-key, and
  every later update, clear or delete that stores no credentials SHALL
  keep it. No API response SHALL carry the key ``credential_secret_arn``
  or ``alias_of``, or the value of ``credential_secret_arn``.

**Validates: Requirements 5.2, 5.8**

Two properties, both against the real routes and the real reducer over the
moto stack, with a recording Secrets Manager client and a recording fake
iot-data client:

- ``test_operations_act_on_the_cameras_own_secret`` generates the stored
  state directly: secrets that exist, are pending deletion or are gone;
  a target entry and other stream entries of any id kind (``cfg-``,
  ``portal-``, ``cam-1``, ids that cannot name a secret), status, mirror
  link, and recorded, pending and reported values of any shape (valid
  ARNs, stale ARNs, bare names, partial ARNs, other devices, accounts and
  regions, a trailing newline, an extra segment, non-strings); and one
  operation, including creates with body ids that fail the 5.2 check. It
  checks each clause against a reference model of the resolution, with
  the exact Secrets Manager call sequence.
- ``test_the_record_survives_reports_and_the_rekey`` creates a camera with
  credentials, then interleaves the device's reports (the re-key with its
  mirror, replays and late duplicates, later reports, acks) with route
  operations (credential-free updates, clears, re-adds, updates and
  deletes of the mirror), and checks that the created entry keeps the
  record and that every operation acts on that secret.
"""
import itertools
import json
import logging
import os
import re
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import event, given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-f2122-p31"
SETTINGS_TABLE_NAME = "test-settings-f2122-p31"
DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"
ACCOUNT = "123456789012"
PREFIX = "dda-portal/stream-camera-credentials"
URLS = {"RTSP": "rtsp://10.0.4.21:554/live", "RTMP": "rtmp://media.local/live"}
PASSWORD = "P31-PWD-c4e1"
CANNOT_HOLD = {"error": "this camera id cannot hold Portal-managed "
                        "credentials", "field": "camera_source_id"}

_device_counter = itertools.count()
_clock = itertools.count(1_730_000_000_000, 1_000)


# ---------------------------------------------------------------------------
# Environment (module-scoped so hypothesis examples share the stack)
# ---------------------------------------------------------------------------

class FakeIotDataClient:
    """Records the desired-change payloads the routes write."""

    def __init__(self):
        self.updates = []

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.updates.append(json.loads(payload))
        return {}


class RecordingClient:
    """A Secrets Manager client recording (operation, SecretId or Name)."""

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


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture(scope="module")
def camera_env(aws_stack):
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

    credentials = camera_registry.stream_credentials
    shadow = {"client": None}
    calls = {"list": []}
    original_iot = camera_registry.iot_data_client
    original_client = credentials._client
    camera_registry.iot_data_client = lambda usecase_id: shadow["client"]

    def recording_client(name, usecase, region=None, session_name=None):
        real = original_client(name, usecase, region=region,
                               session_name=session_name)
        return RecordingClient(real, calls["list"]) \
            if name == "secretsmanager" else real
    credentials._client = recording_client

    handler = ListHandler()
    logging.getLogger().addHandler(handler)

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Property 31 Use Case",
        "account_id": ACCOUNT})
    user_id = f"user-{uuid.uuid4()}"
    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry, credentials=credentials,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        audit=aws_stack.tables.audit_log, shadow=shadow, calls=calls,
        logs=handler, usecase_id=usecase_id,
        user={"user_id": user_id, "email": f"{user_id}@example.com",
              "username": user_id, "role": "Operator"},
    )
    logging.getLogger().removeHandler(handler)
    credentials._client = original_client
    camera_registry.iot_data_client = original_iot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def invoke(camera_env, method, device_id, csid=None, body=None,
           create=False):
    path_parameters = {"id": device_id}
    if csid is not None:
        path_parameters["csid"] = csid
    event_ = {
        "httpMethod": method,
        "path": (f"/devices/{device_id}/cameras" if create or csid is None
                 else f"/devices/{device_id}/cameras/{csid}"),
        "pathParameters": path_parameters,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {
            "sub": camera_env.user["user_id"],
            "email": camera_env.user["email"],
            "cognito:username": camera_env.user["username"],
            "custom:role": camera_env.user["role"],
        }}},
    }
    response = camera_env.module.handler(event_, None)
    return (response["statusCode"], json.loads(response["body"]),
            response["body"])


def items_of(camera_env, device_id):
    return {item["sk"]: item for item in camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id}).get("Items", [])}


def entry_of(camera_env, device_id, csid):
    return items_of(camera_env, device_id).get(f"CAMERA#{csid}")


def audit_count(camera_env, device_id):
    return len([row for row in camera_env.audit.scan().get("Items", [])
                if (row.get("details") or {}).get("device_id") == device_id])


def describe(camera_env, secret_id):
    try:
        return camera_env.secrets.describe_secret(SecretId=secret_id)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def reduce(camera_env, device_id, cameras, failures=None):
    reported = {"schemaVersion": 1, "reportedAt": next(_clock),
                "cameras": cameras}
    if failures is not None:
        reported["failures"] = failures
    camera_env.module.camera_sync._process_report(
        device_id, reported, usecase_id=camera_env.usecase_id)


def stream_params(source_type, **extra):
    params = {"url": URLS[source_type]}
    params.update(extra)
    return params


def assert_response_hides_the_record(raw, record):
    assert "credential_secret_arn" not in raw
    assert "alias_of" not in raw
    if isinstance(record, str) and len(record) > 8:
        assert record not in raw
    assert PASSWORD not in raw


# ---------------------------------------------------------------------------
# The reference model of design component 7
# ---------------------------------------------------------------------------

_SEGMENT = re.compile(r"[A-Za-z0-9_+=.@-]+")
_COMPLETE_ARN = re.compile(
    r"arn:aws:secretsmanager:([a-z0-9-]+):(\d{12}):secret:"
    r"([A-Za-z0-9/_+=.@-]+)-[A-Za-z0-9]{6}")


def model_name_ok(name, device_id):
    prefix = f"{PREFIX}/{device_id}/"
    return (isinstance(name, str) and name.startswith(prefix)
            and _SEGMENT.fullmatch(name[len(prefix):]) is not None)


def model_secret_id(candidate, device_id, derived=False):
    """(secret_id, name) or None, by the rules of Requirement 5.8."""
    if derived:
        return (candidate, candidate) if model_name_ok(candidate, device_id) \
            else None
    if not isinstance(candidate, str):
        return None
    match = _COMPLETE_ARN.fullmatch(candidate)
    if not match or (match.group(1), match.group(2)) != (REGION, ACCOUNT):
        return None
    return (candidate, match.group(3)) \
        if model_name_ok(match.group(3), device_id) else None


def model_reference_arn(params):
    """(present, value): the credentialRef.secretArn a params dict holds;
    a non-object credentialRef is present and never valid."""
    if not isinstance(params, dict):
        return False, None
    reference = params.get("credentialRef")
    if reference is None:
        return False, None
    if not isinstance(reference, dict):
        return True, object()
    value = reference.get("secretArn")
    return value is not None, value


def model_resolve(entry, device_id, csid):
    """The camera's secrets in order, deduplicated by SecretId."""
    entry = entry or {}
    pending = entry.get("pending_content")
    pending_params = pending.get("params") if isinstance(pending, dict) \
        else None
    candidates = []
    if entry.get("credential_secret_arn") is not None:
        candidates.append((entry["credential_secret_arn"], False))
    for params in (pending_params, entry.get("params")):
        present, value = model_reference_arn(params)
        if present:
            candidates.append((value, False))
    candidates.append((f"{PREFIX}/{device_id}/{csid}", True))
    resolved, seen = [], set()
    for candidate, derived in candidates:
        pair = model_secret_id(candidate, device_id, derived)
        if pair is not None and pair[0] not in seen:
            seen.add(pair[0])
            resolved.append(pair)
    return resolved


def model_is_stream(entry):
    pending = entry.get("pending_content")
    return entry.get("type") in URLS or (
        isinstance(pending, dict) and pending.get("type") in URLS)


def model_is_mirror(entry, csid):
    return model_is_stream(entry) and bool(
        entry.get("alias_of")
        or (entry.get("sync_status") == "synced"
            and not csid.startswith("cfg-")))


def model_referenced_names(others, device_id):
    names = set()
    for csid, entry in others.items():
        pending = entry.get("pending_content")
        if not model_is_stream(entry) or model_is_mirror(entry, csid):
            continue
        if (entry.get("sync_status") == "pending"
                and isinstance(pending, dict)
                and pending.get("op") == "delete"):
            continue
        names.update(name for _, name in model_resolve(entry, device_id,
                                                       csid))
    return names


def model_carried(entry):
    keys = ("credentialRef", "credentialsConfigured", "credentialsUpdatedAt")
    pending = entry.get("pending_content")
    pending_params = pending.get("params") if isinstance(pending, dict) \
        else None
    if isinstance(pending_params, dict):
        if pending_params.get("credentialRef"):
            return {k: pending_params[k] for k in keys if k in pending_params}
        if pending_params.get("credentialsConfigured") is False:
            return {}
    reported = entry.get("params")
    if (isinstance(reported, dict) and reported.get("credentialRef")
            and reported.get("credentialsConfigured") is True):
        return {k: reported[k] for k in keys if k in reported}
    return {}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

POOL = ("portal-a", "portal-b", "cfg-1", "cfg-2", "cam-1")
TARGET_IDS = ("cfg-1", "cfg-2", "portal-a", "cam-1", "a/b", "cam 1")
OTHER_IDS = ("cfg-7", "cfg-8", "portal-m")
# Complete ARNs of the device's prefix (live or stale) are weighted up,
# so that most examples resolve at least one recorded, pending or reported
# secret next to the values the routes must ignore.
VALUE_KINDS = ("arn_of",) * 6 + ("stale",) * 2 + (
    "bare_name", "partial", "other_device", "sibling_device",
    "other_account", "other_region", "newline", "nested", "empty",
    "number", "boolean", "mapping")
CREATE_IDS = ("<missing>", None, "cam-new", "portal-new", "a/b", "cfg-new",
              "disc-new", "static-video-camera", "", "cam-1\n", "..", 0,
              "z" * 129)


@st.composite
def values(draw):
    return (draw(st.sampled_from(VALUE_KINDS)), draw(st.sampled_from(POOL)))


@st.composite
def references(draw):
    """A credentialRef shape: absent, an object with a value, an object
    without secretArn, or a value that is not an object."""
    shape = draw(st.sampled_from(("absent", "object", "object", "no_arn",
                                  "not_object")))
    return shape, draw(values())


@st.composite
def entries(draw, ids):
    csid = draw(st.sampled_from(ids))
    # A synced stream entry under a non-cfg- id is a create mirror by its
    # shape, so mirrors are drawn on purpose, linked or not, rather than
    # by accident of the status.
    shape = draw(st.sampled_from(("synced", "synced", "pending", "failed",
                                  "mirror")))
    alias_of = None
    if shape == "mirror":
        status = "synced"
        if csid.startswith("cfg-") or draw(st.booleans()):
            alias_of = "cfg-9"
    elif shape == "synced" and not csid.startswith("cfg-"):
        status = draw(st.sampled_from(("pending", "failed")))
    else:
        status = shape
    pending = None
    if status != "synced" or (alias_of is None and draw(st.booleans())):
        pending = draw(st.sampled_from(("update", "clear", "create",
                                        "delete")))
        if status == "synced":
            status = "pending"
    return {
        "csid": csid,
        "type": draw(st.sampled_from(("RTSP", "RTMP"))),
        "status": status,
        "pending": pending,
        "pending_reference": draw(references()),
        "record": draw(st.one_of(st.none(), values())),
        "reported_reference": draw(references()),
        "configured": draw(st.sampled_from((True, False, None))),
        "alias_of": alias_of,
    }


@st.composite
def scenarios(draw):
    target = draw(entries(TARGET_IDS))
    others = draw(st.lists(entries(OTHER_IDS), max_size=2,
                           unique_by=lambda e: e["csid"]))
    operation = draw(st.sampled_from(("update", "update", "clear", "delete",
                                      "plain", "create")))
    create = {"id": draw(st.sampled_from(CREATE_IDS)),
              "kind": draw(st.sampled_from(("credentials", "clear",
                                            "plain")))}
    return {
        "secrets": {name: draw(st.sampled_from(("absent", "live",
                                                "pending_deletion")))
                    for name in POOL},
        "target": target, "others": others, "operation": operation,
        "create": create,
    }


# ---------------------------------------------------------------------------
# Building the stored state
# ---------------------------------------------------------------------------

def build_value(kind_and_name, device_id, arns, decoys):
    kind, name = kind_and_name
    base = f"{PREFIX}/{device_id}/{name}"
    fabricated = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{base}"
    if kind == "arn_of":
        return arns.get(name) or f"{fabricated}-QwErTy"
    if kind == "stale":
        return f"{fabricated}-StAlEx"
    if kind == "bare_name":
        return base
    if kind == "partial":
        return fabricated
    if kind == "other_device":
        return decoys["other"]
    if kind == "sibling_device":
        return decoys["sibling"]
    if kind == "other_account":
        return (f"arn:aws:secretsmanager:{REGION}:111122223333:secret:"
                f"{base}-AbCdEf")
    if kind == "other_region":
        return (f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:"
                f"{base}-AbCdEf")
    if kind == "newline":
        return (arns.get(name) or f"{fabricated}-QwErTy") + "\n"
    if kind == "nested":
        return decoys["nested"]
    if kind == "empty":
        return ""
    if kind == "number":
        return 7
    if kind == "boolean":
        return True
    return {"secretArn": arns.get(name) or base}


def build_reference(shape_and_value, device_id, arns, decoys):
    shape, value = shape_and_value
    if shape == "absent":
        return None
    built = build_value(value, device_id, arns, decoys)
    if shape == "object":
        return {"secretArn": built, "versionId": "v-generated"}
    if shape == "no_arn":
        return {"versionId": "v-generated"}
    if isinstance(built, (dict, list)) or built is None:
        return "not-an-object"
    return built


def build_item(spec, device_id, usecase_id, arns, decoys):
    source_type = spec["type"]
    params = stream_params(source_type)
    reference = build_reference(spec["reported_reference"], device_id, arns,
                                decoys)
    if reference is not None:
        params["credentialRef"] = reference
    if spec["configured"] is not None:
        params["credentialsConfigured"] = spec["configured"]
    item = {
        "device_id": device_id, "sk": f"CAMERA#{spec['csid']}",
        "camera_source_id": spec["csid"], "usecase_id": usecase_id,
        "name": f"camera {spec['csid']}", "type": source_type,
        "params": params, "capabilities": {},
        "origin": ("edge-configured" if spec["csid"].startswith("cfg-")
                   else "portal-created"),
        "version": 2, "absent": False, "sync_status": spec["status"],
        "last_reported_at": 1_700_000_000_000,
    }
    if spec["pending"] is not None:
        item["portal_change_id"] = f"pc-{spec['csid']}"
        if spec["pending"] == "delete":
            item["pending_content"] = {"op": "delete"}
        else:
            pending_params = stream_params(source_type)
            if spec["pending"] == "clear":
                pending_params["credentialsConfigured"] = False
            else:
                pending_reference = build_reference(
                    spec["pending_reference"], device_id, arns, decoys)
                if pending_reference is not None:
                    pending_params["credentialRef"] = pending_reference
                    pending_params["credentialsConfigured"] = True
            item["pending_content"] = {
                "op": "create" if spec["pending"] == "create" else "update",
                "name": item["name"], "type": source_type,
                "params": pending_params}
    if spec["record"] is not None:
        item["credential_secret_arn"] = build_value(
            spec["record"], device_id, arns, decoys)
    if spec["alias_of"] is not None:
        item["alias_of"] = spec["alias_of"]
    return item


def make_world(camera_env, case):
    device_id = f"thing-p31-{next(_device_counter)}"
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META",
        "usecase_id": camera_env.usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False})
    arns, exists, pending_deletion = {}, set(), set()
    for name, state in case["secrets"].items():
        if state == "absent":
            continue
        arn = camera_env.secrets.create_secret(
            Name=f"{PREFIX}/{device_id}/{name}",
            SecretString=json.dumps({"password": "seeded"}))["ARN"]
        arns[name] = arn
        exists.update({arn, f"{PREFIX}/{device_id}/{name}"})
        if state == "pending_deletion":
            camera_env.secrets.delete_secret(SecretId=arn,
                                             RecoveryWindowInDays=7)
            pending_deletion.update({arn, f"{PREFIX}/{device_id}/{name}"})
    decoys = {
        "other": camera_env.secrets.create_secret(
            Name=f"{PREFIX}/other-{device_id}/decoy",
            SecretString="{}")["ARN"],
        "sibling": camera_env.secrets.create_secret(
            Name=f"{PREFIX}/{device_id}-b/decoy", SecretString="{}")["ARN"],
        "nested": camera_env.secrets.create_secret(
            Name=f"{PREFIX}/{device_id}/nested/decoy",
            SecretString="{}")["ARN"],
    }
    target = build_item(case["target"], device_id, camera_env.usecase_id,
                        arns, decoys)
    others = {}
    for spec in case["others"]:
        if spec["csid"] == case["target"]["csid"]:
            continue
        others[spec["csid"]] = build_item(spec, device_id,
                                          camera_env.usecase_id, arns, decoys)
    for item in [target, *others.values()]:
        camera_env.registry.put_item(Item=item)
    return SimpleNamespace(device_id=device_id, arns=arns, exists=exists,
                           pending_deletion=pending_deletion, decoys=decoys,
                           target=target, others=others,
                           csid=case["target"]["csid"])


def decoy_states(camera_env, decoys):
    states = {}
    for key, arn in decoys.items():
        description = describe(camera_env, arn)
        states[key] = (sorted((description.get("VersionIdsToStages")
                               or {}).items()),
                       description.get("DeletedDate"))
    return states


def check_calls_in_prefix(calls, device_id, derived_names):
    for operation, secret_id in calls:
        if secret_id in derived_names:
            assert model_name_ok(secret_id, device_id), (operation,
                                                         secret_id)
            continue
        # Any other id is a recorded, pending or reported value: a
        # complete ARN of the use case's account and region, under the
        # device's prefix plus one segment.
        assert model_secret_id(secret_id, device_id) is not None, \
            f"{operation} named {secret_id!r}"


# ---------------------------------------------------------------------------
# Property 31, part 1: one operation on any stored state
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(scenarios())
def test_operations_act_on_the_cameras_own_secret(camera_env, case):
    world = make_world(camera_env, case)
    device_id, csid, target = world.device_id, world.csid, world.target
    fake = FakeIotDataClient()
    camera_env.shadow["client"] = fake
    calls = camera_env.calls["list"]
    del calls[:]
    del camera_env.logs.records[:]
    before = items_of(camera_env, device_id)
    audit_before = audit_count(camera_env, device_id)
    decoys_before = decoy_states(camera_env, world.decoys)
    operation = case["operation"]
    source_type = target["type"]
    derived = f"{PREFIX}/{device_id}/{csid}"
    resolved = model_resolve(target, device_id, csid)
    mirror = model_is_mirror(target, csid)
    record_before = target.get("credential_secret_arn")
    event(f"operation: {operation}")

    body = {"name": "edited", "type": source_type,
            "params": stream_params(source_type)}
    if operation == "create":
        create = case["create"]
        body = {"name": "new", "type": source_type,
                "params": stream_params(source_type)}
        if create["id"] != "<missing>":
            body["camera_source_id"] = create["id"]
        if create["kind"] == "credentials":
            body["credentials"] = {"password": PASSWORD}
        elif create["kind"] == "clear":
            body["clearCredentials"] = True
        status, response, raw = invoke(camera_env, "POST", device_id,
                                       body=body, create=True)
        new_id = create["id"]
        valid = (new_id in ("<missing>", None)
                 or (isinstance(new_id, str)
                     and re.fullmatch(r"[A-Za-z0-9_.@+=-]{1,128}", new_id)
                     and set(new_id) != {"."}
                     and not re.match(r"(cfg-|disc-|arv-)", new_id)
                     and new_id not in ("static-image-camera",
                                        "static-video-camera")))
        if not valid:
            event("create: invalid body id")
            assert status == 400, response
            assert response["field"] == "camera_source_id"
            assert calls == [] and fake.updates == []
            assert items_of(camera_env, device_id) == before
            assert audit_count(camera_env, device_id) == audit_before
        else:
            assert status == 201, response
            created_id = response["camera_source_id"]
            if new_id in ("<missing>", None):
                assert re.fullmatch(r"portal-[0-9a-f]{12}", created_id)
            name = f"{PREFIX}/{device_id}/{created_id}"
            if create["kind"] == "credentials":
                assert calls == [("describe_secret", name),
                                 ("create_secret", name)], calls
                record = entry_of(camera_env, device_id, created_id)[
                    "credential_secret_arn"]
                assert record == describe(camera_env, name)["ARN"]
            else:
                # A create that clears or omits credentials touches nothing.
                assert calls == []
        assert_response_hides_the_record(raw, None)
        return

    if operation == "delete":
        status, response, raw = invoke(camera_env, "DELETE", device_id, csid)
    else:
        if operation == "update":
            body["credentials"] = {"password": PASSWORD}
        elif operation == "clear":
            body["clearCredentials"] = True
        status, response, raw = invoke(camera_env, "PUT", device_id, csid,
                                       body=body)
    assert_response_hides_the_record(raw, record_before)
    for record in camera_env.logs.records:
        assert PASSWORD not in record.getMessage()

    if mirror:
        event("a stream mirror: refused")
        assert status == 409, response
        assert response["code"] == "CAMERA_SOURCE_ALIAS"
        assert calls == [] and fake.updates == []
        assert items_of(camera_env, device_id) == before
        assert audit_count(camera_env, device_id) == audit_before
        return

    check_calls_in_prefix(calls, device_id, {derived})
    assert decoy_states(camera_env, world.decoys) == decoys_before

    if operation == "update":
        expected_calls, written = [], None
        for secret_id, _ in resolved:
            expected_calls.append(("describe_secret", secret_id))
            if secret_id in world.exists:
                written = secret_id
                break
        if written is not None:
            if written in world.pending_deletion:
                expected_calls += [("restore_secret", written),
                                   ("describe_secret", written)]
            expected_calls += [("put_secret_value", written),
                               ("tag_resource", written)]
        elif resolved and model_name_ok(derived, device_id):
            expected_calls.append(("create_secret", derived))
        if not resolved or (written is None
                            and not model_name_ok(derived, device_id)):
            event("update: the id cannot hold credentials")
            assert (status, response) == (400, CANNOT_HOLD)
            assert calls == expected_calls
            assert fake.updates == []
            assert items_of(camera_env, device_id) == before
            assert audit_count(camera_env, device_id) == audit_before
            return
        assert status == 200, response
        assert calls == expected_calls, (calls, expected_calls)
        written_arn = describe(camera_env, written or derived)["ARN"]
        change = fake.updates[-1]["state"]["desired"]["changes"][csid]
        assert change["params"]["credentialRef"]["secretArn"] == written_arn
        assert entry_of(camera_env, device_id, csid)[
            "credential_secret_arn"] == written_arn
        event("update: wrote an existing secret" if written
              else "update: created the derived name")
        return

    # A clear, a delete and a plain edit store no credentials: the record
    # stays as it was.
    assert status == 200, response
    after = entry_of(camera_env, device_id, csid)
    assert after.get("credential_secret_arn") == record_before
    if operation == "plain":
        assert calls == []
        params = fake.updates[-1]["state"]["desired"]["changes"][csid][
            "params"]
        expected = model_carried(target)
        assert params.get("credentialRef") == expected.get("credentialRef")
        assert params.get("credentialsConfigured") == expected.get(
            "credentialsConfigured")
        event("plain edit: carried a reference" if expected
              else "plain edit: carried nothing")
        return
    in_use = model_referenced_names(world.others, device_id)
    expected = [secret_id for secret_id, name in resolved
                if name not in in_use]
    assert calls == [("delete_secret", secret_id) for secret_id in expected]
    event(f"{operation}: scheduled {len(expected)} of {len(resolved)}")


# ---------------------------------------------------------------------------
# Property 31, part 2: the record through reports and the re-key
# ---------------------------------------------------------------------------

STEPS = ("replay", "device_report", "plain", "clear", "readd",
         "mirror_update", "mirror_delete")


@st.composite
def histories(draw):
    return {
        "type": draw(st.sampled_from(("RTSP", "RTMP"))),
        "steps": draw(st.lists(st.tuples(st.sampled_from(STEPS),
                                         st.booleans()),
                               min_size=1, max_size=8)),
    }


def camera_from_params(source_type, name, params, version, ack=None):
    defaults = ({"transport": "tcp", "latencyMs": 200}
                if source_type == "RTSP" else {})
    defaults.update({"decoder": "auto", "maxFrameDimension": 1920,
                     "stallTimeoutS": 10, "credentialsConfigured": False})
    camera = {"version": version, "name": name, "type": source_type,
              "origin": "edge-configured",
              "params": {**defaults, **params}, "capabilities": {}}
    if ack is not None:
        camera["ack"] = ack
    # As the ingest parses a documents event: numbers as Decimal.
    return json.loads(json.dumps(camera), parse_float=Decimal)


@settings(deadline=None)
@given(histories())
def test_the_record_survives_reports_and_the_rekey(camera_env, case):
    device_id = f"thing-p31h-{next(_device_counter)}"
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META",
        "usecase_id": camera_env.usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False})
    fake = FakeIotDataClient()
    camera_env.shadow["client"] = fake
    calls = camera_env.calls["list"]
    source_type = case["type"]
    raws = []

    status, response, raw = invoke(camera_env, "POST", device_id, body={
        "name": "dock", "type": source_type, "camera_source_id": "portal-k",
        "params": stream_params(source_type),
        "credentials": {"password": PASSWORD}}, create=True)
    assert status == 201, response
    raws.append(raw)
    created = fake.updates[-1]["state"]["desired"]["changes"]["portal-k"]
    record = created["params"]["credentialRef"]["secretArn"]

    # The re-key: the created camera and, for one report, its mirror.
    version = 1
    merged = {key: value for key, value in created["params"].items()
              if value is not None}
    camera = camera_from_params(source_type, "dock", merged, version,
                                ack=response["portal_change_id"])
    alias_event = {"cfg-k": camera, "portal-k": dict(camera)}
    reduce(camera_env, device_id, alias_event)
    assert entry_of(camera_env, device_id, "cfg-k")[
        "credential_secret_arn"] == record
    assert entry_of(camera_env, device_id, "portal-k")["alias_of"] == "cfg-k"

    def report(ack=None):
        nonlocal version
        version += 1
        reduce(camera_env, device_id, {"cfg-k": camera_from_params(
            source_type, "dock", merged, version, ack=ack)})

    for step, ack in case["steps"]:
        event(f"step: {step}")
        if step == "replay":
            reduce(camera_env, device_id, alias_event)
        elif step == "device_report":
            report()
        elif step in ("plain", "clear", "readd"):
            body = {"name": "dock", "type": source_type,
                    "params": stream_params(source_type)}
            if step == "clear":
                body["clearCredentials"] = True
            elif step == "readd":
                body["credentials"] = {"password": PASSWORD}
            del calls[:]
            status, response, raw = invoke(camera_env, "PUT", device_id,
                                           "cfg-k", body=body)
            raws.append(raw)
            assert status == 200, response
            delivered = fake.updates[-1]["state"]["desired"]["changes"][
                "cfg-k"]["params"]
            if step == "readd":
                # The credentials go into the camera's own secret.
                assert delivered["credentialRef"]["secretArn"] == record
                assert describe(camera_env, record).get("DeletedDate") is None
            if step == "clear":
                assert ("delete_secret", record) in calls
                assert describe(camera_env, record).get(
                    "DeletedDate") is not None
            check_calls_in_prefix(calls, device_id,
                                  {f"{PREFIX}/{device_id}/cfg-k"})
            if ack:
                # The merged shadow keeps every key the device stops
                # reporting; a clear's report drops only the flag's value.
                merged.update({key: value for key, value in delivered.items()
                               if value is not None})
                report(ack=response["portal_change_id"])
        else:
            mirror = entry_of(camera_env, device_id, "portal-k")
            status, response, raw = invoke(
                camera_env, "PUT" if step == "mirror_update" else "DELETE",
                device_id, "portal-k",
                body={"name": "x", "type": source_type,
                      "params": stream_params(source_type)}
                if step == "mirror_update" else None)
            raws.append(raw)
            if mirror is None:
                assert status == 404, response
            else:
                assert status == 409, response
                assert response["code"] == "CAMERA_SOURCE_ALIAS"
        entry = entry_of(camera_env, device_id, "cfg-k")
        assert entry is not None
        assert entry["credential_secret_arn"] == record, step

    del calls[:]
    status, response, raw = invoke(camera_env, "DELETE", device_id, "cfg-k")
    raws.append(raw)
    assert status == 200, response
    assert ("delete_secret", record) in calls
    assert describe(camera_env, record).get("DeletedDate") is not None
    assert entry_of(camera_env, device_id, "cfg-k")[
        "credential_secret_arn"] == record
    for raw in raws:
        assert_response_hides_the_record(raw, record)
