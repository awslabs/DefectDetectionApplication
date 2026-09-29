"""
Property-based test for the Requirement 5.4 rollback of a stream
Camera_Source credential write whose delivery then fails
(rtsp-rtmp-stream-cameras task 8.5).

**Feature: rtsp-rtmp-stream-cameras, Property 12: A credential delivery failure leaves no referenced version**

*For any* sequence of credential writes in which the shadow write fails:

- If the request updated an existing secret, the Credential_Vault secret's
  ``AWSCURRENT`` version after the failed request SHALL equal its version
  before the request.
- If the request created the secret, the secret SHALL no longer exist.

**Validates: Requirement 5.4**

Generators: a *sequence* of 1-4 credential writes against one
Camera_Source, each one delivered through a shadow client that either
records the write or fails it, with at least one failing step per example.
A step either carries credentials (a fresh non-empty subset of
``username``/``password``/``urlSecret``) or clears them
(``clearCredentials: true``, which schedules the secret's deletion once
delivered, Req 5.8). Both stream types with every accepted scheme; a fresh
Stream_URL and in-domain setting set per step; and a per-step choice of
shadow failure (a throttling ``ClientError``, a missing-shadow
``ClientError``, or a plain ``RuntimeError``), since
``write_desired_change`` turns any shadow-path exception into the same
502.

The sequence is what makes the property meaningful: the route's rollback
branch depends on what the Credential_Vault already held for the camera
(nothing, a live secret, or a secret pending deletion after a clear), so
the test tracks the vault and registry across the steps and checks the
matching clause after every step:

- a failed create leaves no secret;
- a failed update leaves ``AWSCURRENT`` where the last successful write
  left it, and a secret that was pending deletion is pending deletion
  again (the request restored it before writing its version);
- a failed clear changes nothing;
- a delivered write advances the vault, and a delivered clear schedules
  the secret's deletion without touching its versions.

Beyond the two clauses, each failing step also asserts the rest of
Requirement 5.4: the existing delivery-failure response (502, the same
body as every other shadow failure), the device's registry partition
byte-identical to before the step, no audit event, and the "nothing
references it" part: the withdrawn version is not ``AWSCURRENT``, and the
Credential_Reference the registry still carries (if any) resolves to the
value of the last successful write.

Runs against the moto-backed conftest stack (DynamoDB, Secrets Manager,
IAM) with the real ``camera_registry`` + ``stream_credentials`` modules.
The only faked piece is the assumed-role iot-data shadow transport; the
vault writes, the rollback, and the registry are real.

Each failing step records which clause it exercised as a Hypothesis
``event``, so ``--hypothesis-show-statistics`` shows that every rollback
branch really occurs.

One emulator limitation shapes the sequences: after an explicit
``UpdateSecretVersionStage`` (the rollback of an *existing* secret), moto
does not demote the restored version on the next ``PutSecretValue``, so it
reports two ``AWSCURRENT`` versions and resolves ``AWSCURRENT`` to the
stale one, where real Secrets Manager keeps exactly one. An example
therefore ends at its first rolled-back update instead of loosening the
assertions to fit the emulator; every other shape (repeated failed
creates, delivered writes and clears before a failed one, a failed create
followed by a delivered one) runs to the end of the sequence.
"""
import copy
import itertools
import json
import os
import string
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import event, given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-p12-rollback"
SETTINGS_TABLE_NAME = "test-settings-camera-p12-rollback"

DEVICE_ROLE_NAME = "GreengrassV2TokenExchangeRole"

#: The existing delivery-failure response every shadow failure returns
#: (`write_desired_change`), which Requirement 5.4 keeps unchanged.
DELIVERY_FAILURE_ERROR = (
    "Failed to deliver the change to the device sync channel")

_device_counter = itertools.count()
_sentinel_counter = itertools.count()


# ---------------------------------------------------------------------------
# Environment (module-scoped so hypothesis examples share the stack)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """Camera registry + settings tables, the device token-exchange role,
    a freshly bound handler module, a swappable shadow client, and a
    recorder around the real `store_stream_credentials` (so the test can
    name the version the failed request wrote)."""
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

    # Swap the assumed-role iot-data client for a per-step fake via a
    # mutable holder (module-scoped fixture; each step installs its own).
    holder = {"client": None}
    original_iot = camera_registry.iot_data_client
    camera_registry.iot_data_client = lambda usecase_id: holder["client"]

    # Observe (do not replace) the real vault write, so an assertion can
    # name the version id the request stored and whether it created the
    # secret. The route calls this through the module attribute.
    stream_credentials = camera_registry.stream_credentials
    original_store = stream_credentials.store_stream_credentials
    writes = []

    def recording_store(*args, **kwargs):
        stored = original_store(*args, **kwargs)
        writes.append(stored)
        return stored

    stream_credentials.store_stream_credentials = recording_store

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        credentials=stream_credentials,
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        secrets=boto3.client("secretsmanager", region_name=REGION),
        shadow_holder=holder,
        writes=writes,
    )
    stream_credentials.store_stream_credentials = original_store
    camera_registry.iot_data_client = original_iot


@pytest.fixture(scope="module")
def operator(aws_stack):
    """One Operator user and Use_Case shared by every example."""
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Property 12 Use Case",
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


class RecordingIotDataClient:
    """A shadow client that accepts the desired write."""

    def __init__(self):
        self.updates = []

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.updates.append({
            "thing_name": thingName,
            "shadow_name": shadowName,
            "payload": json.loads(payload),
        })
        return {}


class FailingIotDataClient:
    """A shadow client whose desired write fails, the step-5 failure of
    design component 7. `write_desired_change` catches any exception and
    returns the same 502, so the kind is generated rather than fixed."""

    def __init__(self, mode):
        self.mode = mode
        self.attempts = 0

    def update_thing_shadow(self, thingName, shadowName, payload):
        self.attempts += 1
        if self.mode == "throttled":
            raise ClientError(
                {"Error": {"Code": "ThrottlingException",
                           "Message": "Rate exceeded"}},
                "UpdateThingShadow")
        if self.mode == "no_such_shadow":
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException",
                           "Message": "No shadow exists with name"}},
                "UpdateThingShadow")
        raise RuntimeError("endpoint unreachable")


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


def device_items(camera_env, device_id):
    return camera_env.registry.query(
        KeyConditionExpression="device_id = :d",
        ExpressionAttributeValues={":d": device_id},
    ).get("Items", [])


def camera_entry(camera_env, device_id, csid):
    return next((item for item in device_items(camera_env, device_id)
                 if item.get("sk") == f"CAMERA#{csid}"), None)


def describe(camera_env, name):
    """The secret's description, or None when it does not exist."""
    try:
        return camera_env.secrets.describe_secret(SecretId=name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def stages_of(description, version_id):
    """The staging labels a version carries, from a DescribeSecret."""
    return list((description.get("VersionIdsToStages") or {}).get(
        version_id) or [])


def vault_state(camera_env, name):
    """What the Credential_Vault holds for the camera.

    ``AWSCURRENT`` is read from ``DescribeSecret``'s stage map, which also
    works for a secret pending deletion (``GetSecretValue`` refuses those).
    The value is then read by version id: moto's ``GetSecretValue`` without
    one returns the *newest* version rather than the one ``AWSCURRENT``
    points at, which would hide exactly the rollback this property asserts.
    """
    description = describe(camera_env, name)
    if description is None:
        return SimpleNamespace(exists=False, pending=False, version=None,
                               value=None, description=None)
    currents = [version for version, stages in
                (description.get("VersionIdsToStages") or {}).items()
                if "AWSCURRENT" in (stages or [])]
    # Real Secrets Manager keeps exactly one; more means the sequence ran
    # into the emulator limitation the module docstring describes.
    assert len(currents) <= 1, f"{name}: several AWSCURRENT versions"
    version = currents[0] if currents else None
    pending = description.get("DeletedDate") is not None
    value = None
    if version is not None and not pending:
        value = camera_env.secrets.get_secret_value(
            SecretId=name, VersionId=version)["SecretString"]
    return SimpleNamespace(exists=True, pending=pending, version=version,
                           value=value, description=description)


def audit_count(audit, device_id):
    return len([
        row for row in audit.scan().get("Items", [])
        if (row.get("details") or {}).get("device_id") == device_id])


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Credential bodies: punctuation-heavy ASCII without '"' or '\\', which
# JSON would escape and so hide from a substring search.
_SECRET_ALPHABET = (string.ascii_letters + string.digits
                    + "-_.:+=!@#%^&*()[]{}<>,;?'|/ ")
_secret_bodies = st.text(alphabet=_SECRET_ALPHABET, min_size=1, max_size=24)

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
FAILURE_MODES = ("throttled", "no_such_shadow", "unreachable")
STEP_KINDS = ("credentials", "clear")


def build_url(scheme, host, port, path, query):
    authority = host if port is None else f"{host}:{port}"
    url = f"{scheme}://{authority}"
    if path:
        url += path
    if query:
        url += f"?{query}"
    return url


@st.composite
def _step(draw, source_type, kind):
    """One credential write to the camera, and how its delivery goes."""
    params = {
        "url": build_url(
            draw(st.sampled_from(_SCHEMES[source_type])),
            draw(_hosts), draw(_ports), draw(_paths), draw(_queries)),
    }
    if source_type == "RTSP":
        # transport and latencyMs are RTSP-only settings.
        if draw(st.booleans()):
            params["transport"] = draw(st.sampled_from(["tcp", "udp", "auto"]))
        if draw(st.booleans()):
            params["latencyMs"] = draw(st.integers(min_value=0,
                                                   max_value=5000))
    if draw(st.booleans()):
        params["decoder"] = draw(
            st.sampled_from(["auto", "hardware", "software"]))
    if draw(st.booleans()):
        params["maxFrameDimension"] = draw(
            st.integers(min_value=320, max_value=4096))
    if draw(st.booleans()):
        params["stallTimeoutS"] = draw(st.integers(min_value=2, max_value=60))

    credential_bodies = {}
    if kind == "credentials":
        fields = draw(st.lists(st.sampled_from(CREDENTIAL_FIELDS),
                               min_size=1, max_size=3, unique=True))
        credential_bodies = {field: draw(_secret_bodies)
                             for field in sorted(fields)}
    return {
        "kind": kind,
        "name": draw(_names),
        "params": params,
        "credential_bodies": credential_bodies,
        "failure_mode": draw(st.sampled_from(FAILURE_MODES)),
    }


@st.composite
def _sequences(draw):
    """A sequence of credential writes with at least one failing
    delivery — the antecedent of Property 12.

    The shape is drawn explicitly so that every rollback branch is
    exercised often:

    - ``create_fails``: the first write fails, so the request created the
      secret.
    - ``delivered_then_fails``: a delivered write before a failing one, so
      the request updated an existing secret.
    - ``cleared_then_fails``: a delivered write, a delivered clear (which
      schedules the secret's deletion), then a failing write, so the
      request restored a secret pending deletion before updating it.
    - ``clear_fails``: a delivered write, then a clear whose delivery
      fails, which must leave the live secret exactly as it was.
    - ``mixed`` leaves every kind and flag to Hypothesis (with one index
      forced to fail) so the named shapes do not narrow the search.
    """
    source_type = draw(st.sampled_from(["RTSP", "RTMP"]))
    shape = draw(st.sampled_from(["create_fails", "delivered_then_fails",
                                  "cleared_then_fails", "clear_fails",
                                  "mixed"]))
    if shape == "create_fails":
        count = draw(st.integers(min_value=1, max_value=4))
        kinds = ["credentials"] + [draw(st.sampled_from(STEP_KINDS))
                                   for _ in range(count - 1)]
        fails = [True] + [draw(st.booleans()) for _ in range(count - 1)]
    elif shape == "delivered_then_fails":
        count = draw(st.integers(min_value=2, max_value=4))
        kinds = (["credentials"]
                 + [draw(st.sampled_from(STEP_KINDS))
                    for _ in range(count - 2)]
                 + ["credentials"])
        fails = ([False] + [draw(st.booleans()) for _ in range(count - 2)]
                 + [True])
    elif shape == "cleared_then_fails":
        kinds = ["credentials", "clear", "credentials"]
        fails = [False, False, True]
    elif shape == "clear_fails":
        kinds = ["credentials", "clear"]
        fails = [False, True]
    else:
        count = draw(st.integers(min_value=1, max_value=4))
        kinds = [draw(st.sampled_from(STEP_KINDS)) for _ in range(count)]
        fails = [draw(st.booleans()) for _ in range(count)]
        # At least one step must fail delivery, or the property says
        # nothing about the example.
        fails[draw(st.integers(min_value=0, max_value=count - 1))] = True
    steps = []
    for kind, fail in zip(kinds, fails):
        step = draw(_step(source_type, kind))
        step["delivery_fails"] = fail
        steps.append(step)
    return {"type": source_type, "shape": shape, "steps": steps}


def sentinel(body, tag):
    """A per-example-unique credential value, so a substring hit in any
    surface can only be a real leak."""
    return f"P12{tag}{next(_sentinel_counter)}Z{body}"


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_sequences())
def test_delivery_failure_leaves_no_referenced_credential_version(
        camera_env, operator, audit, case):
    """Across a sequence of credential writes and clears, every step whose
    shadow write fails leaves the Credential_Vault as the last successful
    step left it — a created secret gone, an updated secret's AWSCURRENT
    back on its previous version, a restored secret pending deletion
    again — with the existing 502, the registry unchanged, and nothing
    referencing the withdrawn version (Req 5.4)."""
    module = camera_env.module
    device_id = f"thing-p12-{next(_device_counter)}"
    usecase_id = operator.usecase_id
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })
    csid = f"portal-p12-{next(_device_counter)}"
    name = camera_env.credentials.secret_name(device_id, csid)

    # Model state carried across the sequence.
    entry_exists = False          # does the registry hold the camera?
    delivered_reference = None    # the reference the registry last delivered
    delivered_value = None        # the secret value that reference names

    for index, step in enumerate(case["steps"]):
        clear = step["kind"] == "clear"
        where = (f"step {index} ({step['kind']}, "
                 f"{'fails' if step['delivery_fails'] else 'ok'})")
        credentials = {field: sentinel(body, field[:3])
                       for field, body in step["credential_bodies"].items()}
        secret_values = list(credentials.values())
        body = {
            "name": step["name"],
            "type": case["type"],
            "params": dict(step["params"]),
        }
        if clear:
            body["clearCredentials"] = True
        else:
            body["credentials"] = credentials

        # --- the vault and registry state before this step ---------------
        before = vault_state(camera_env, name)
        before_items = copy.deepcopy(device_items(camera_env, device_id))
        before_audit = audit_count(audit, device_id)
        writes_before = len(camera_env.writes)

        if step["delivery_fails"]:
            camera_env.shadow_holder["client"] = FailingIotDataClient(
                step["failure_mode"])
        else:
            camera_env.shadow_holder["client"] = RecordingIotDataClient()

        if entry_exists:
            status, response, raw = invoke(
                camera_env, "PUT", device_id, operator.user,
                sub_path=f"/{csid}", body=body)
        else:
            body["camera_source_id"] = csid
            status, response, raw = invoke(
                camera_env, "POST", device_id, operator.user, body=body)

        stored = None
        if clear:
            # A clear stores nothing: the secret is only scheduled for
            # deletion, and only once the change is delivered.
            assert len(camera_env.writes) == writes_before, \
                f"{where}: a clear wrote to the vault"
        else:
            # The credentials always reach the vault before the delivery
            # attempt (Req 5.3), so the rollback below is never vacuous.
            assert len(camera_env.writes) == writes_before + 1, \
                f"{where}: the vault write did not run"
            stored = camera_env.writes[-1]
            assert stored["created"] is not before.exists, where
            assert stored["restoredFromDeletion"] is before.pending, where

        if not step["delivery_fails"]:
            assert status == (200 if entry_exists else 201), response
            entry_exists = True
            after = vault_state(camera_env, name)
            params = camera_entry(camera_env, device_id, csid)[
                "pending_content"]["params"]
            if clear:
                # A delivered clear schedules the deletion (Req 5.8) and
                # leaves the versions alone; the change carries no
                # reference.
                assert params.get("credentialsConfigured") is False, where
                assert "credentialRef" not in params, where
                assert after.exists is before.exists, where
                assert after.pending is before.exists, \
                    f"{where}: the cleared secret is not pending deletion"
                assert after.version == before.version, where
                delivered_reference, delivered_value = None, None
                continue
            # A delivered write advances the vault and the registry: the
            # new version becomes AWSCURRENT of a live secret and the
            # registry carries its reference.
            assert after.exists and not after.pending, where
            assert after.version == stored["versionId"], where
            assert json.loads(after.value) == credentials, where
            delivered_reference = params["credentialRef"]
            assert delivered_reference["versionId"] == stored["versionId"], \
                where
            delivered_value = after.value
            continue

        # --- a failed delivery: the existing 502, no credential echoed ---
        assert status == 502, f"{where}: {response}"
        assert response == {"error": DELIVERY_FAILURE_ERROR}, where
        for value in secret_values:
            assert value not in raw, \
                f"{where}: a credential value was echoed in the 502"

        after = vault_state(camera_env, name)
        if clear:
            event("failed delivery of a clear")
            # Nothing was stored and nothing is scheduled before delivery.
            assert (after.exists, after.pending, after.version) == \
                (before.exists, before.pending, before.version), \
                f"{where}: a failed clear changed the vault"
        elif not before.exists:
            # "If the request created the secret, the secret SHALL no
            # longer exist."
            event("failed delivery of a created secret")
            assert not after.exists, \
                (f"{where}: the secret created by the failed request still "
                 "exists")
        else:
            # "If the request updated an existing secret, the secret's
            # AWSCURRENT version after the failed request SHALL equal its
            # version before the request."
            event("failed delivery of a new version of a secret pending "
                  "deletion" if before.pending else
                  "failed delivery of a new version of an existing secret")
            assert after.exists, \
                f"{where}: the failed update destroyed an existing secret"
            assert after.version == before.version, \
                f"{where}: AWSCURRENT moved off the pre-request version"
            assert after.value == before.value, \
                f"{where}: the secret's current value changed"
            # A secret the request restored from a clear's scheduled
            # deletion is pending deletion again; a live one stays live.
            assert after.pending is before.pending, \
                (f"{where}: the failed update changed whether the secret "
                 "is scheduled for deletion")
            # Nothing references the withdrawn version.
            assert "AWSCURRENT" not in stages_of(
                after.description, stored["versionId"]), \
                f"{where}: the withdrawn version is still AWSCURRENT"
            if delivered_reference is None:
                # The last delivered step cleared the credentials: the
                # registry must still reference nothing.
                params = camera_entry(camera_env, device_id, csid)[
                    "pending_content"]["params"]
                assert "credentialRef" not in params, where
            else:
                assert delivered_reference["versionId"] == before.version, \
                    where
                pinned = camera_env.secrets.get_secret_value(
                    SecretId=delivered_reference["secretArn"],
                    VersionId=delivered_reference["versionId"])
                assert pinned["SecretString"] == delivered_value, \
                    f"{where}: the referenced version no longer resolves"
                for value in secret_values:
                    assert value not in pinned["SecretString"], \
                        (f"{where}: the withdrawn credentials became the "
                         "referenced value")

        # --- the rest of Req 5.4: the registry is unchanged --------------
        after_items = device_items(camera_env, device_id)
        assert after_items == before_items, \
            f"{where}: the failed request changed the registry"
        assert audit_count(audit, device_id) == before_audit, \
            f"{where}: the failed request wrote an audit event"
        # A failed create leaves no entry to update, so the next step of
        # the sequence is another create.
        assert entry_exists == (
            any(item.get("sk") == f"CAMERA#{csid}" for item in after_items))

        if stored is not None and not stored["created"]:
            # The sequence ends here, because the *emulator* can no longer
            # represent the state this property talks about: after an
            # explicit `UpdateSecretVersionStage`, moto does not demote the
            # restored version on the next `PutSecretValue`, so it reports
            # two AWSCURRENT versions (real Secrets Manager keeps exactly
            # one). Asserting past that point would be asserting moto's
            # bookkeeping, so a rolled-back update is the last step of an
            # example rather than something the assertions are loosened
            # for.
            event("sequence ended at a rolled-back update (moto stage "
                  "bookkeeping)")
            break

    # The sequence had at least one failing delivery, and the module under
    # test is the real one (guards against a fixture that silently stops
    # exercising the route).
    assert any(step["delivery_fails"] for step in case["steps"])
    assert module.stream_credentials is camera_env.credentials
