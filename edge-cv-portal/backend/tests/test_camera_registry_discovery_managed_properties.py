"""
Property-based test for the Camera_Registry API discovery-managed
immutability (camera-registry-sync task 6.4).

**Feature: camera-registry-sync, Property 10: Discovery-managed sources are immutable from the Portal**

*For any* Camera_Source entry and any mutation operation (update,
delete), the portal API rejects the mutation identifying the source as
discovery-managed exactly when the entry's origin is `edge-discovered`,
and accepts it (subject to other validation) for every other origin.
For the rejected case: the response is 409 with code
`DISCOVERY_MANAGED`, zero shadow writes occur, and the stored registry
item is byte-identical afterward.

**Validates: Requirements 5.6**

Generators: PUT (arbitrary valid bodies) and DELETE mutations against
entries across all origins (edge-discovered, edge-configured,
portal-created), arbitrary sync statuses (synced/pending/failed,
including pending with a prior portal_change_id and failed with a
reason), unicode/whitespace-heavy names, arbitrary param and
capability dicts, versions, and absent/absent_since markers.

Runs against the moto-backed conftest stack with the real handler
module and a recording fake iot-data shadow client (the assumed-role
transport is the only faked piece).
"""
import itertools
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

CAMERA_REGISTRY_TABLE_NAME = "test-camera-registry-discovery-managed-props"
SETTINGS_TABLE_NAME = "test-settings-camera-discovery-managed-props"

_device_counter = itertools.count()


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
        "name": "Property 10 Use Case",
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

# Discovered capability metadata (arbitrary content per Property 10).
_capabilities = st.dictionaries(
    st.sampled_from(["formats", "driver", "busInfo", "cardName"]),
    st.one_of(st.text(max_size=20),
              st.lists(st.text(max_size=10), max_size=3)),
    max_size=4,
)

_csids = st.tuples(
    st.sampled_from(["disc", "cfg", "portal"]),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=12),
).map(lambda t: f"{t[0]}-{t[1]}")

_origins = st.sampled_from(
    ["edge-discovered", "edge-configured", "portal-created"])


@st.composite
def _existing_entries(draw):
    """A pre-existing registry camera entry (arbitrary content/status)."""
    sync_status = draw(st.sampled_from(["synced", "pending", "failed"]))
    entry = {
        "csid": draw(_csids),
        "name": draw(_names),
        "type": draw(_types),
        "params": draw(_params),
        "capabilities": draw(_capabilities),
        "origin": draw(_origins),
        "version": draw(st.integers(min_value=0, max_value=50)),
        "sync_status": sync_status,
        "absent": draw(st.booleans()),
    }
    if entry["absent"]:
        entry["absent_since"] = draw(
            st.integers(min_value=1_600_000_000_000,
                        max_value=1_800_000_000_000))
    if sync_status == "pending":
        entry["portal_change_id"] = f"pc-prior-{draw(st.integers(0, 999))}"
    if sync_status == "failed":
        entry["failure_reason"] = "prior failure"
    return entry


@st.composite
def _mutation_cases(draw):
    case = {
        "op": draw(st.sampled_from(["update", "delete"])),
        "existing": draw(_existing_entries()),
    }
    if case["op"] == "update":
        case["body"] = {
            "name": draw(_names),
            "type": draw(_types),
            "params": draw(_params),
        }
    return case


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_mutation_cases())
def test_discovery_managed_sources_are_immutable_from_the_portal(
        camera_env, operator, case):
    """A PUT or DELETE is rejected 409 DISCOVERY_MANAGED with zero shadow
    writes and a byte-identical stored item exactly when the entry's
    origin is edge-discovered; every other origin is accepted
    (Requirement 5.6)."""
    device_id = f"thing-prop10-{next(_device_counter)}"
    usecase_id = operator.usecase_id
    camera_env.registry.put_item(Item={
        "device_id": device_id, "sk": "META", "usecase_id": usecase_id,
        "last_report_at": 1_700_000_000_000, "never_synced": False,
    })

    existing = case["existing"]
    csid = existing["csid"]
    item = {
        "device_id": device_id, "sk": f"CAMERA#{csid}",
        "camera_source_id": csid, "usecase_id": usecase_id,
        "name": existing["name"], "type": existing["type"],
        "params": existing["params"],
        "capabilities": existing["capabilities"],
        "origin": existing["origin"], "version": existing["version"],
        "sync_status": existing["sync_status"],
        "absent": existing["absent"],
        "last_reported_at": 1_700_000_000_000,
    }
    for optional in ("absent_since", "portal_change_id", "failure_reason"):
        if optional in existing:
            item[optional] = existing[optional]
    camera_env.registry.put_item(Item=item)

    stored_before = camera_env.registry.get_item(
        Key={"device_id": device_id, "sk": f"CAMERA#{csid}"})["Item"]

    fake = FakeIotDataClient()
    camera_env.shadow_holder["client"] = fake

    if case["op"] == "update":
        status, body = invoke(camera_env, "PUT", device_id, operator.user,
                              sub_path=f"/{csid}", body=case["body"])
    else:
        status, body = invoke(camera_env, "DELETE", device_id, operator.user,
                              sub_path=f"/{csid}")

    if existing["origin"] == "edge-discovered":
        # --- rejected, identified as discovery-managed ---
        assert status == 409
        assert body["code"] == "DISCOVERY_MANAGED"
        assert body["camera_source_id"] == csid

        # --- zero shadow writes ---
        assert fake.updates == []

        # --- stored registry item byte-identical ---
        stored_after = camera_env.registry.get_item(
            Key={"device_id": device_id, "sk": f"CAMERA#{csid}"})["Item"]
        assert stored_after == stored_before
    else:
        # --- every other origin is accepted (Property 10 biconditional) ---
        assert status == 200
        assert body.get("code") != "DISCOVERY_MANAGED"
        assert body["sync_status"] == "pending"
