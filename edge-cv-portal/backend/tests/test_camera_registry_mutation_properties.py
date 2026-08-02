"""
Property-based test for the Camera_Registry API mutation routes
(camera-registry-sync task 6.3).

**Feature: camera-registry-sync, Property 9: Portal mutation produces pending state and matching desired document**

*For any* valid create, update, or delete of a Camera_Source through the
portal API, the registry entry transitions to `pending` with a fresh
`portal_change_id`, and the shadow desired document written for the
device contains a change entry whose operation and content round-trip
to the submitted mutation.

**Validates: Requirements 5.1**

Generators: all three operations, unicode/whitespace-heavy names,
arbitrary param dicts (string/int/bool values), existing entries across
mutable origins and sync statuses (including already-pending entries
with a prior portal_change_id, to check freshness), and explicit vs
generated camera source ids on create.

Runs against the moto-backed conftest stack with the real handler
module and a recording fake iot-data shadow client (the assumed-role
transport is the only faked piece).
"""
import itertools
import json
import os
import sys
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-mutation-props"
SETTINGS_TABLE_NAME = "test-settings-camera-mutation-props"

_device_counter = itertools.count()

# Every portal_change_id ever issued during this test run; Property 9's
# "fresh portal_change_id" means a mutation never reuses one.
_issued_change_ids = set()


# ---------------------------------------------------------------------------
# Environment (module-scoped so hypothesis examples share the stack)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def camera_env(aws_stack):
    """Camera registry + settings tables and a freshly bound handler module."""
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

    # Re-import so the module binds the table names above and
    # moto-intercepted boto3 clients (conftest pattern).
    sys.modules.pop("camera_registry", None)
    sys.modules.pop("camera_sync", None)
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
        registry=resource.Table(CAMERA_REGISTRY_TABLE_NAME),
        shadow_holder=holder,
    )
    camera_registry.iot_data_client = original


@pytest.fixture(scope="module")
def operator(aws_stack):
    """One Operator user and Use_Case shared by every example."""
    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Property 9 Use Case",
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
    response = camera_env.module.handler(
        make_event(method, device_id, user, sub_path, body), None)
    return response["statusCode"], json.loads(response["body"])


def normalize(value):
    """Numeric-type-insensitive comparison shape (DynamoDB Decimals vs
    JSON ints round-tripped through the shadow payload)."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return value


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Unicode/whitespace-heavy names (any non-empty name is a valid mutation).
_names = st.text(
    alphabet=st.characters(
        codec="utf-8", categories=("L", "N", "P", "Zs", "S")
    ),
    min_size=1,
    max_size=24,
)

_types = st.sampled_from(["Camera", "Folder", "ICam", "NvidiaCSI", "RTSP"])

_param_keys = st.one_of(
    st.sampled_from(["devicePath", "cameraId", "url", "gain", "exposure"]),
    st.text(min_size=1, max_size=12),
)
_param_values = st.one_of(
    st.booleans(),
    st.integers(min_value=-10_000_000, max_value=10_000_000),
    st.text(max_size=20),
)
_params = st.dictionaries(_param_keys, _param_values, max_size=5)

_csids = st.tuples(
    st.sampled_from(["cfg", "portal"]),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=10),
).map(lambda t: f"{t[0]}-{t[1]}")

# Origins a portal mutation may legally target (edge-discovered is the
# immutability property, task 6.4).
_mutable_origins = st.sampled_from(["edge-configured", "portal-created"])


@st.composite
def _existing_entries(draw):
    """A pre-existing registry camera entry an update/delete targets."""
    sync_status = draw(st.sampled_from(["synced", "pending", "failed"]))
    entry = {
        "csid": draw(_csids),
        "name": draw(_names),
        "type": draw(_types),
        "params": draw(_params),
        "origin": draw(_mutable_origins),
        "version": draw(st.integers(min_value=0, max_value=50)),
        "sync_status": sync_status,
    }
    if sync_status == "pending":
        entry["portal_change_id"] = f"pc-prior-{draw(st.integers(0, 999))}"
    if sync_status == "failed":
        entry["failure_reason"] = "prior failure"
    return entry


@st.composite
def _mutation_cases(draw):
    op = draw(st.sampled_from(["create", "update", "delete"]))
    case = {"op": op}
    if op == "create":
        case["body"] = {
            "name": draw(_names),
            "type": draw(_types),
            "params": draw(_params),
        }
        explicit = draw(st.booleans())
        if explicit:
            case["body"]["camera_source_id"] = draw(_csids)
    else:
        case["existing"] = draw(_existing_entries())
        if op == "update":
            case["body"] = {
                "name": draw(_names),
                "type": draw(_types),
                "params": draw(_params),
            }
    return case


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_mutation_cases())
def test_portal_mutation_pending_state_and_matching_desired_document(
        camera_env, operator, case):
    """Every valid portal mutation marks the entry pending with a fresh
    portal_change_id, and the shadow desired change round-trips the
    submitted operation and content (Requirement 5.1)."""
    device_id = f"thing-prop9-{next(_device_counter)}"
    usecase_id = operator.usecase_id
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })

    op = case["op"]
    existing = case.get("existing")
    if existing is not None:
        item = {
            "device_id": device_id, "sk": f"CAMERA#{existing['csid']}",
            "camera_source_id": existing["csid"], "usecase_id": usecase_id,
            "name": existing["name"], "type": existing["type"],
            "params": existing["params"], "capabilities": {},
            "origin": existing["origin"], "version": existing["version"],
            "sync_status": existing["sync_status"],
            "last_reported_at": 1_700_000_000_000,
        }
        if "portal_change_id" in existing:
            item["portal_change_id"] = existing["portal_change_id"]
        if "failure_reason" in existing:
            item["failure_reason"] = existing["failure_reason"]
        camera_env.registry.put_item(Item=item)

    fake = FakeIotDataClient()
    camera_env.shadow_holder["client"] = fake

    if op == "create":
        status, body = invoke(camera_env, "POST", device_id, operator.user,
                              body=case["body"])
        assert status == 201
        csid = body["camera_source_id"]
        assert body["origin"] == "portal-created"
    elif op == "update":
        csid = existing["csid"]
        status, body = invoke(camera_env, "PUT", device_id, operator.user,
                              sub_path=f"/{csid}", body=case["body"])
        assert status == 200
    else:
        csid = existing["csid"]
        status, body = invoke(camera_env, "DELETE", device_id, operator.user,
                              sub_path=f"/{csid}")
        assert status == 200

    # --- pending state with a fresh portal_change_id ---
    assert body["sync_status"] == "pending"
    change_id = body["portal_change_id"]
    assert change_id.startswith("pc-")
    assert change_id not in _issued_change_ids, \
        "portal_change_id was reused across mutations"
    _issued_change_ids.add(change_id)
    if existing is not None and "portal_change_id" in existing:
        assert change_id != existing["portal_change_id"]

    registry_item = camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"}).get("Item")
    assert registry_item is not None
    assert registry_item["sync_status"] == "pending"
    assert registry_item["portal_change_id"] == change_id
    # A fresh change supersedes any earlier failure.
    assert "failure_reason" not in registry_item

    # --- exactly one desired document written for the device ---
    assert len(fake.updates) == 1
    update = fake.updates[0]
    assert update["thing_name"] == device_id
    assert update["shadow_name"] == "dda-camera-registry"
    changes = update["payload"]["state"]["desired"]["changes"]
    assert list(changes.keys()) == [csid]
    change = changes[csid]

    # --- operation and content round-trip the submitted mutation ---
    assert change["op"] == op
    assert change["portalChangeId"] == change_id
    pending_content = registry_item["pending_content"]
    assert pending_content["op"] == op

    if op == "delete":
        assert normalize(dict(pending_content)) == {"op": "delete"}
        assert change["baseVersion"] == existing["version"]
    else:
        submitted = {
            "name": case["body"]["name"],
            "type": case["body"]["type"],
            "params": case["body"]["params"],
        }
        shadow_content = {k: change[k] for k in ("name", "type", "params")}
        recorded_content = {k: pending_content[k]
                            for k in ("name", "type", "params")}
        assert normalize(shadow_content) == normalize(submitted)
        assert normalize(recorded_content) == normalize(submitted)
        if op == "update":
            assert change["baseVersion"] == existing["version"]
            # Edge-reported content stays effective until the ack; the
            # portal version travels only in pending_content.
            assert registry_item["name"] == existing["name"]
