"""
Shared fixtures and fakes for the Portal_Pin_API route tests
(cloud-static-camera-provisioning task 2).

Provides the module-scoped ``pin_env`` fixture (a fresh camera-registry
table + component bucket + re-imported camera_registry handler with the
iot-data and S3 seams swapped for holder-injected fakes — the
test_camera_registry_mutation_properties.py pattern), synthetic API
Gateway event builders, recording fake clients, Pillow-generated image
bytes (the test_synthetic_data_unit.py convention), and audit-log
lookup helpers.

Import the fixture into a test module with::

    from pin_route_helpers import pin_env  # noqa: F401
"""
import io
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from conftest import REGION, TEST_ENV

# Staging prefix mirrored from camera_registry (kept literal so a
# regression in the module constant fails a test rather than silently
# following it).
STAGING_PREFIX = "static-image-pins/staging/"


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pin_env(aws_stack):
    """Camera-registry table + component bucket + rebound handler module.

    Unique table/bucket names per test module (module-scoped fixture),
    with the iot_data_client and pin_s3_client seams replaced by holders
    each example fills with a fresh recording fake.
    """
    import boto3

    suffix = uuid.uuid4().hex[:10]
    table_name = f"test-camera-registry-pin-{suffix}"
    bucket_name = f"test-dda-component-pin-{suffix}"

    os.environ["CAMERA_REGISTRY_TABLE"] = table_name
    os.environ["COMPONENT_BUCKET"] = bucket_name

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "device_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "device_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=bucket_name)

    # Re-import so the module binds the names above and moto-intercepted
    # boto3 clients (conftest pattern).
    for module_name in ("camera_registry", "camera_sync", "pin_requests"):
        sys.modules.pop(module_name, None)
    import camera_registry

    shadow_holder = {"client": None}
    s3_holder = {"client": None}
    original_iot = camera_registry.iot_data_client
    original_s3 = camera_registry.pin_s3_client
    camera_registry.iot_data_client = lambda usecase_id: shadow_holder["client"]
    camera_registry.pin_s3_client = lambda: s3_holder["client"]

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        module=camera_registry,
        registry=resource.Table(table_name),
        bucket=bucket_name,
        s3=s3,
        shadow_holder=shadow_holder,
        s3_holder=s3_holder,
        devices=aws_stack.tables.devices,
        usecases=aws_stack.tables.usecases,
        user_roles=aws_stack.tables.user_roles,
        audit_log=aws_stack.tables.audit_log,
    )
    camera_registry.iot_data_client = original_iot
    camera_registry.pin_s3_client = original_s3


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeIotDataClient:
    """Records update_thing_shadow writes; optionally fails them."""

    def __init__(self, log=None, fail=False):
        self.updates = []
        self.log = log
        self.fail = fail

    def update_thing_shadow(self, thingName, shadowName, payload):
        if self.fail:
            raise RuntimeError("injected shadow failure")
        self.updates.append({
            "thing_name": thingName,
            "shadow_name": shadowName,
            "payload": json.loads(payload),
        })
        if self.log is not None:
            self.log.append(("shadow_write", thingName))
        return {}


class RecordingS3:
    """Wraps the real (moto) S3 client, recording mutating Image_Transport
    calls in an ordered log; optionally fails copy_object (the canonical
    store step)."""

    def __init__(self, inner, log=None, fail_copy=False):
        self.inner = inner
        self.log = log if log is not None else []
        self.fail_copy = fail_copy
        self.copies = []
        self.deletes = []

    def head_object(self, **kwargs):
        return self.inner.head_object(**kwargs)

    def get_object(self, **kwargs):
        return self.inner.get_object(**kwargs)

    def generate_presigned_url(self, *args, **kwargs):
        return self.inner.generate_presigned_url(*args, **kwargs)

    def copy_object(self, **kwargs):
        if self.fail_copy:
            raise RuntimeError("injected store failure")
        self.copies.append(kwargs)
        self.log.append(("canonical_copy", kwargs.get("Key")))
        return self.inner.copy_object(**kwargs)

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)
        return self.inner.delete_object(**kwargs)


# ---------------------------------------------------------------------------
# Users, devices, events
# ---------------------------------------------------------------------------

def make_user(role="Operator"):
    """A claims-backed user (fresh id, JWT custom:role = role)."""
    user_id = f"user-{uuid.uuid4()}"
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": role,
    }


def create_usecase(env, name="Pin Test Use Case"):
    usecase_id = f"uc-{uuid.uuid4()}"
    env.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": name,
        "account_id": "123456789012",
    })
    return usecase_id


def register_device(env, usecase_id, prefix="thing-pin"):
    """A fresh device with a portal devices-table record."""
    device_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    env.devices.put_item(Item={
        "device_id": device_id,
        "usecase_id": usecase_id,
    })
    return device_id


def make_event(method, device_id, user, sub_path="", body=None, query=None,
               csid=None, raw_body=None):
    path_parameters = {"id": device_id}
    if csid is not None:
        path_parameters["csid"] = csid
    if raw_body is not None:
        serialized = raw_body
    else:
        serialized = json.dumps(body) if body is not None else None
    return {
        "httpMethod": method,
        "path": f"/devices/{device_id}/cameras{sub_path}",
        "pathParameters": path_parameters,
        "queryStringParameters": query,
        "body": serialized,
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


def invoke(env, method, device_id, user, sub_path="", body=None, query=None,
           csid=None, raw_body=None):
    response = env.module.handler(
        make_event(method, device_id, user, sub_path, body, query,
                   csid, raw_body), None)
    return response["statusCode"], json.loads(response["body"])


def stage_object(env, payload):
    """PUT a payload to a fresh staging key (the presigned-PUT stand-in)."""
    staging_key = f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    env.s3.put_object(Bucket=env.bucket, Key=staging_key, Body=payload)
    return staging_key


def submit_pin(env, device_id, user, payload, file_name="sample.png",
               query=None):
    """Stage a payload and submit it through the pin route."""
    staging_key = stage_object(env, payload)
    return invoke(env, "POST", device_id, user,
                  sub_path="/static-image/pin",
                  body={"stagingKey": staging_key, "fileName": file_name},
                  query=query)


# ---------------------------------------------------------------------------
# Image bytes (Pillow-generated, the _tiny_png_bytes convention)
# ---------------------------------------------------------------------------

def image_bytes(image_format="PNG", size=(4, 4), color=(255, 0, 0)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=image_format)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Registry / audit lookups
# ---------------------------------------------------------------------------

def pin_request_items(env, device_id):
    """The device's PIN_REQUEST# items (any status), via the module's own
    query helper (newest first)."""
    import pin_requests

    return pin_requests.query_pin_request_items(env.registry, device_id)


def audit_events(env, device_id, action=None):
    """Audit-log entries for one device (fresh devices per example keep
    this isolated), oldest first."""
    from boto3.dynamodb.conditions import Attr

    condition = Attr("resource_id").eq(device_id)
    if action is not None:
        condition = condition & Attr("action").eq(action)
    items = []
    kwargs = {"FilterExpression": condition}
    while True:
        response = env.audit_log.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return sorted(items, key=lambda item: int(item["timestamp"]))
