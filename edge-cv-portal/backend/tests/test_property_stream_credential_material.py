"""
Property-based test for stream Camera_Source credential handling in the
Camera_Registry API (rtsp-rtmp-stream-cameras task 8.4).

**Feature: rtsp-rtmp-stream-cameras, Property 11: The registry never stores or returns credential material**

*For any* stream create or update that carries credentials:

- No credential value SHALL appear in the stored registry item, the
  desired shadow payload, the audit details, or the response.
- Every response SHALL present any stored URL with its user information
  redacted.

**Validates: Requirements 5.3, 5.7, 6.1**

Generators: both credentialed mutations (create and update), both stream
types with every accepted scheme, Stream_URLs with optional port, path
and a non-secret query, each in-domain stream setting present or absent,
and every non-empty subset of the three credential fields
(``username``/``password``/``urlSecret``) with punctuation-heavy values.
Each example also seeds a *legacy* stream row whose stored ``params.url``
embeds ``user:password@`` — the shape `validate_stream_camera_body` now
rejects on the way in but that a row created before this feature can
still hold — so the redaction clause is exercised against real stored
data.

Soundness of the "no credential value appears" checks: every generated
credential value (and the legacy URL's user information) is prefixed with
a per-example sentinel, so a substring hit can only be a real leak, and
the alphabet excludes ``"`` and ``\\`` so that a leaked value cannot hide
behind JSON escaping. The Credential_Vault secret is read back and
asserted to *contain* every value, which keeps the negative assertions
from passing vacuously.

Runs against the moto-backed conftest stack (DynamoDB, Secrets Manager,
IAM) with the real `camera_registry` + `stream_credentials` modules and a
recording fake iot-data shadow client (the assumed-role shadow transport
is the only faked piece).
"""
import itertools
import json
import os
import string
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-p11-credentials"
SETTINGS_TABLE_NAME = "test-settings-camera-p11-credentials"

DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"

_device_counter = itertools.count()
_sentinel_counter = itertools.count()


# ---------------------------------------------------------------------------
# Environment (module-scoped so hypothesis examples share the stack)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """Camera registry + settings tables, the device token-exchange role,
    and a freshly bound handler module."""
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

    # The role ensure_device_read_grant writes its inline policy to, so
    # the real grant path runs instead of degrading to "failed".
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

    # Re-import so the modules bind the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    sys.modules.pop("camera_registry", None)
    sys.modules.pop("stream_credentials", None)
    import camera_registry

    # Swap the assumed-role iot-data client for a per-example fake via a
    # mutable holder (module-scoped fixture; each example installs a
    # fresh recording client into the holder).
    holder = {"client": None}
    original = camera_registry.iot_data_client
    camera_registry.iot_data_client = lambda usecase_id: holder["client"]

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        credentials=camera_registry.stream_credentials,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        shadow_holder=holder,
    )
    camera_registry.iot_data_client = original


@pytest.fixture(scope="module")
def operator(aws_stack):
    """One Operator user and Use_Case shared by every example."""
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Property 11 Use Case",
        "account_id": "123456789012",
    })
    user_id = f"user-{uuid.uuid4()}"
    user = {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": "Operator",
    }
    return SimpleNamespace(user=user, usecase_id=usecase_id)


@pytest.fixture(scope="module")
def audit(aws_stack):
    return aws_stack.tables.audit_log


class FakeIotDataClient:
    """Records update_thing_shadow writes."""

    def __init__(self):
        self.updates = []

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.updates.append({
            "thing_name": thingName,
            "shadow_name": shadowName,
            "payload": json.loads(payload),
        })
        return {}


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
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def invoke(camera_env, method, device_id, user, sub_path="", body=None):
    """Invoke the handler; returns (status, parsed body, raw body string)."""
    response = camera_env.module.handler(
        make_event(method, device_id, user, sub_path, body), None)
    raw = response["body"]
    return response["statusCode"], json.loads(raw), raw


def dumps(value):
    """Search text for a surface: unescaped unicode, so an ASCII
    credential appears verbatim if it is present at all."""
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Credential/user-info bodies: punctuation-heavy ASCII without '"' or
# '\\', which JSON would escape and so hide from a substring search (see
# the module docstring).
_SECRET_ALPHABET = (string.ascii_letters + string.digits
                    + "-_.:+=!@#%^&*()[]{}<>,;?'|/ ")
_secret_bodies = st.text(alphabet=_SECRET_ALPHABET, min_size=1, max_size=32)

# The user information of a *stored* URL: the characters RFC 3986 allows
# in a userinfo component, minus ':' (which separates the user from the
# password here). '@', '/', '?', '#' and whitespace cannot appear
# unencoded in a real authority, and a URL carrying them is not a URL
# whose user information a reader could identify at all, so they are out
# of this generator rather than out of the property.
_USERINFO_ALPHABET = string.ascii_letters + string.digits + "-._~%!$&'()*+,;="
_userinfo_bodies = st.text(alphabet=_USERINFO_ALPHABET,
                           min_size=1, max_size=24)

_names = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "P", "Zs")),
    min_size=1,
    max_size=24,
)

_hosts = st.from_regex(r"[a-z0-9][a-z0-9.\-]{0,20}", fullmatch=True)
_ports = st.one_of(st.none(), st.integers(min_value=1, max_value=65535))
_paths = st.one_of(st.none(),
                   st.from_regex(r"/[A-Za-z0-9/_.\-]{0,24}", fullmatch=True))
# Non-secret query parameters only: a Secret_Query_Parameter is rejected
# by check_stream_url on the way in (its own property is Property 2).
_queries = st.one_of(st.none(), st.sampled_from(
    ["channel=1", "profile=main", "subtype=0", "stream=0", "trackID=1"]))

_SCHEMES = {"RTSP": ("rtsp", "rtsps"), "RTMP": ("rtmp", "rtmps")}
CREDENTIAL_FIELDS = ("username", "password", "urlSecret")


def build_url(scheme, host, port, path, query, user_info=None):
    authority = host if port is None else f"{host}:{port}"
    if user_info:
        authority = f"{user_info}@{authority}"
    url = f"{scheme}://{authority}"
    if path:
        url += path
    if query:
        url += f"?{query}"
    return url


@st.composite
def _url_parts(draw, source_type):
    return {
        "scheme": draw(st.sampled_from(_SCHEMES[source_type])),
        "host": draw(_hosts),
        "port": draw(_ports),
        "path": draw(_paths),
        "query": draw(_queries),
    }


@st.composite
def _cases(draw):
    op = draw(st.sampled_from(["create", "update"]))
    source_type = draw(st.sampled_from(["RTSP", "RTMP"]))
    parts = draw(_url_parts(source_type))

    settings_params = {}
    if source_type == "RTSP":
        # transport and latencyMs are RTSP-only settings.
        if draw(st.booleans()):
            settings_params["transport"] = draw(
                st.sampled_from(["tcp", "udp", "auto"]))
        if draw(st.booleans()):
            settings_params["latencyMs"] = draw(
                st.integers(min_value=0, max_value=5000))
    if draw(st.booleans()):
        settings_params["decoder"] = draw(
            st.sampled_from(["auto", "hardware", "software"]))
    if draw(st.booleans()):
        settings_params["maxFrameDimension"] = draw(
            st.integers(min_value=320, max_value=4096))
    if draw(st.booleans()):
        settings_params["stallTimeoutS"] = draw(
            st.integers(min_value=2, max_value=60))

    fields = draw(st.lists(st.sampled_from(CREDENTIAL_FIELDS),
                           min_size=1, max_size=3, unique=True))
    credential_bodies = {field: draw(_secret_bodies)
                         for field in sorted(fields)}

    # The legacy row whose stored URL embeds user information.
    legacy = draw(_url_parts(draw(st.sampled_from(["RTSP", "RTMP"]))))
    legacy["type"] = next(
        t for t, schemes in _SCHEMES.items() if legacy["scheme"] in schemes)
    legacy["user_body"] = draw(_userinfo_bodies)
    legacy["password_body"] = draw(_userinfo_bodies)

    return {
        "op": op,
        "type": source_type,
        "url_parts": parts,
        "settings": settings_params,
        "credential_bodies": credential_bodies,
        "name": draw(_names),
        "legacy": legacy,
    }


def sentinel(body, tag):
    """A per-example-unique secret value, so a substring hit in any
    surface can only be a real leak."""
    return f"P11{tag}{next(_sentinel_counter)}Z{body}"


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_cases())
def test_registry_never_stores_or_returns_credential_material(
        camera_env, operator, audit, case):
    """A credentialed stream create or update keeps every credential value
    out of the registry item, the desired shadow payload, the audit
    details, and the response, and every response presents a stored URL
    with its user information redacted (Reqs 5.3, 5.7, 6.1)."""
    module = camera_env.module
    device_id = f"thing-p11-{next(_device_counter)}"
    usecase_id = operator.usecase_id
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })

    # --- the request under test -------------------------------------------
    credentials = {field: sentinel(body, field[:3])
                   for field, body in case["credential_bodies"].items()}
    secret_values = list(credentials.values())
    url = build_url(**case["url_parts"])
    body = {
        "name": case["name"],
        "type": case["type"],
        "params": {"url": url, **case["settings"]},
        "credentials": credentials,
    }

    # The update case acts on an acknowledged camera, which the device
    # reports under cfg-<imageSourceId> with origin edge-configured; a
    # synced stream entry under any other id is a create mirror, which the
    # routes refuse (task 29, third design review finding 3). The create
    # case keeps a portal- id, which the create id check accepts.
    case_number = next(_device_counter)
    csid = (f"cfg-p11-{case_number}" if case["op"] == "update"
            else f"portal-p11-{case_number}")
    prior_ref = {
        "secretArn": (f"arn:aws:secretsmanager:{REGION}:123456789012:secret:"
                      f"dda-portal/stream-camera-credentials/{device_id}/"
                      f"{csid}-aBcDeF"),
        "versionId": "prior-version-id",
    }
    if case["op"] == "update":
        # An existing stream entry, already carrying a Credential_Reference
        # from an earlier credentialed write.
        camera_env.registry.put_item(Item={
            "device_id": device_id, "sk": f"CAMERA#{csid}",
            "camera_source_id": csid, "usecase_id": usecase_id,
            "name": "existing", "type": case["type"],
            "params": {"url": url, "credentialRef": prior_ref,
                       "credentialsConfigured": True,
                       "credentialsUpdatedAt": 1_700_000_000_000},
            "capabilities": {}, "origin": "edge-configured", "version": 3,
            "sync_status": "synced",
            "last_reported_at": 1_700_000_000_000,
        })
        # The old fixture shape, a synced portal-created stream entry, is a
        # create mirror: its credentialed update is refused with 409
        # CAMERA_SOURCE_ALIAS and writes nothing.
        old_csid = f"portal-p11-old-{case_number}"
        old_item = {
            "device_id": device_id, "sk": f"CAMERA#{old_csid}",
            "camera_source_id": old_csid, "usecase_id": usecase_id,
            "name": "existing", "type": case["type"],
            "params": {"url": url, "credentialRef": prior_ref,
                       "credentialsConfigured": True,
                       "credentialsUpdatedAt": 1_700_000_000_000},
            "capabilities": {}, "origin": "portal-created", "version": 3,
            "sync_status": "synced",
            "last_reported_at": 1_700_000_000_000,
        }
        camera_env.registry.put_item(Item=old_item)
        refused = FakeIotDataClient()
        camera_env.shadow_holder["client"] = refused
        status, response, raw_response = invoke(
            camera_env, "PUT", device_id, operator.user,
            sub_path=f"/{old_csid}", body=body)
        assert status == 409, response
        assert response["code"] == "CAMERA_SOURCE_ALIAS"
        assert refused.updates == []
        stored_old = camera_env.registry.get_item(Key={
            "device_id": device_id, "sk": f"CAMERA#{old_csid}"})["Item"]
        assert stored_old == old_item
        for value in secret_values:
            assert value not in raw_response
        try:
            camera_env.secrets.describe_secret(
                SecretId=camera_env.credentials.secret_name(
                    device_id, old_csid))
        except camera_env.secrets.exceptions.ResourceNotFoundException:
            pass
        else:
            raise AssertionError("the refused update created a secret")

    # --- a legacy stored row whose URL embeds user information ------------
    legacy = case["legacy"]
    legacy_user = sentinel(legacy["user_body"], "usr")
    legacy_password = sentinel(legacy["password_body"], "pwd")
    legacy_url = build_url(
        legacy["scheme"], legacy["host"], legacy["port"], legacy["path"],
        legacy["query"], user_info=f"{legacy_user}:{legacy_password}")
    legacy_csid = f"cfg-legacy-{next(_device_counter)}"
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": f"CAMERA#{legacy_csid}",
        "camera_source_id": legacy_csid, "usecase_id": usecase_id,
        "name": "legacy", "type": legacy["type"],
        "params": {"url": legacy_url},
        "capabilities": {}, "origin": "edge-configured", "version": 1,
        "sync_status": "synced", "last_reported_at": 1_700_000_000_000,
    })

    fake = FakeIotDataClient()
    camera_env.shadow_holder["client"] = fake

    if case["op"] == "create":
        body["camera_source_id"] = csid
        status, response, raw_response = invoke(
            camera_env, "POST", device_id, operator.user, body=body)
        assert status == 201, response
    else:
        status, response, raw_response = invoke(
            camera_env, "PUT", device_id, operator.user,
            sub_path=f"/{csid}", body=body)
        assert status == 200, response
    # A create shows the submitted content while pending; an update keeps
    # the last-reported edge content effective until the device
    # acknowledges, and the seeded entry carries the same URL. Either way
    # the effective stored URL is the generated (secret-free) one.
    effective_url = url

    # --- the credentials really did reach the Credential_Vault -----------
    # (without this, every negative assertion below could pass vacuously)
    name = camera_env.credentials.secret_name(device_id, csid)
    stored_value = camera_env.secrets.get_secret_value(
        SecretId=name, VersionStage="AWSCURRENT")["SecretString"]
    stored_material = json.loads(stored_value)
    assert stored_material == credentials
    for value in secret_values:
        assert value in stored_value

    # --- surface 1: the stored registry items ----------------------------
    items = camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id},
    ).get("Items", [])
    registry_text = dumps(items)
    for value in secret_values:
        assert value not in registry_text, \
            "a credential value reached the registry item"
    assert "clearCredentials" not in registry_text

    entry = next(item for item in items
                 if item.get("sk") == f"CAMERA#{csid}")
    pending = entry["pending_content"]
    reference = pending["params"]["credentialRef"]
    # Only the reference travels, and only its two fields (Req 5.3).
    assert set(reference.keys()) == {"secretArn", "versionId"}
    assert reference["secretArn"].startswith(
        "arn:aws:secretsmanager:") and name in reference["secretArn"]
    assert reference != prior_ref
    assert pending["params"]["credentialsConfigured"] is True

    # --- surface 2: the desired shadow payload ---------------------------
    assert len(fake.updates) == 1
    shadow_text = dumps(fake.updates[0]["payload"])
    for value in secret_values:
        assert value not in shadow_text, \
            "a credential value reached the desired shadow document"
    change = fake.updates[0]["payload"]["state"]["desired"]["changes"][csid]
    assert change["params"]["credentialRef"] == reference
    assert "credentials" not in change
    assert "clearCredentials" not in change

    # --- surface 3: the audit details ------------------------------------
    audit_rows = audit.scan().get("Items", [])
    audit_text = dumps(audit_rows)
    for value in secret_values:
        assert value not in audit_text, \
            "a credential value reached an audit event"
    mine = [row for row in audit_rows
            if (row.get("details") or {}).get("device_id") == device_id]
    assert mine, "the mutation wrote no audit event"
    for row in mine:
        details = row["details"]
        assert details.get("credentials_configured") is True
        assert "credentials" not in details
        assert "credentialRef" not in details

    # --- surface 4: the mutation response --------------------------------
    for value in secret_values:
        assert value not in raw_response, \
            "a credential value was echoed in the response"

    # --- the read view ---------------------------------------------------
    status, view, raw_view = invoke(
        camera_env, "GET", device_id, operator.user)
    assert status == 200, view
    for value in secret_values + [legacy_user, legacy_password]:
        assert value not in raw_view, \
            "a credential value was returned by the cameras view"
    # The Credential_Reference is an internal pointer (Req 5.7).
    assert "credentialRef" not in raw_view
    assert prior_ref["secretArn"] not in raw_view
    assert reference["secretArn"] not in raw_view

    views = {camera["camera_source_id"]: camera for camera in view["cameras"]}

    # The legacy row's user information is masked, and the rest of its URL
    # is preserved byte for byte.
    expected_legacy = build_url(
        legacy["scheme"], legacy["host"], legacy["port"], legacy["path"],
        legacy["query"], user_info="***")
    assert views[legacy_csid]["params"]["url"] == expected_legacy
    assert views[legacy_csid]["credentials"] == {
        "configured": False, "updatedAt": None}

    # The mutated entry: secret-free URL returned unchanged, credential
    # state reported without any material.
    mutated = views[csid]
    assert mutated["params"]["url"] == effective_url
    assert mutated["credentials"]["configured"] is True
    assert mutated["credentials"]["updatedAt"] is not None
    assert "credentials" not in mutated["params"]
