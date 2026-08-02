"""Tests for quick-setup Target_Architecture recording on completion
(device-arch-compatibility tasks 2.3, 2.4).

These drive the *real* ``quick_setup.report_status`` route end to end through
the module ``handler`` (``POST /quick-setup/status``) against moto-backed
DynamoDB tables: the device-registrations table (the authenticated transition)
and the portal Devices table (``dda-portal-devices``) that receives the
recorded DDA Target_Architecture.

Coverage (design "Testing Strategy" A):
  (a) completed report with a valid arch -> Devices UpdateItem + audit event;
  (b) completed report with an invalid arch -> no write, still completed;
  (c) completed report with no arch -> no write, still completed;
  (d) Devices-table failure -> still 200;
  (e) unauthenticated / mismatched report -> no write.

Plus a hypothesis property (min 100 examples):
  **Feature: device-arch-compatibility, Property 7: Fixed-set write gate**
  the Devices-table write happens iff the reported value is in the fixed set,
  and the registration reaches ``completed`` regardless of the arch value's
  validity or presence.

**Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.6**
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
DEVICES_TABLE = "test-devices"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

INVALID_TOKEN_ERROR_CODE = "invalid_token"

# The fixed set the write gate accepts — identical to devices.py / quick_setup.py.
TARGET_ARCHITECTURES = ("x86_64", "x86_64_nvidia",
                        "arm64_jp4", "arm64_jp5", "arm64_jp6")

REPORTABLE_FROM = ("in_progress", "failed")


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, devices, use-cases, and
    audit tables plus the artifacts bucket, and the real quick_setup module
    imported inside the mock so its module-level boto3 resources (and the
    DEVICES_TABLE constant read at import time) are intercepted by moto.
    """
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ["DEVICES_TABLE"] = DEVICES_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["PORTAL_ARTIFACTS_BUCKET"] = ARTIFACTS_BUCKET
    os.environ.setdefault(
        "QUICK_SETUP_BUNDLE_KEY", "quick-setup/current/setup-bundle.tar.gz"
    )
    os.environ.setdefault("QUICK_SETUP_BUNDLE_SHA256", "0" * 64)
    os.environ.setdefault(
        "QUICK_SETUP_BOOTSTRAP_KEY", "quick-setup/current/bootstrap.sh"
    )

    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=REGISTRATIONS_TABLE,
            KeySchema=[{"AttributeName": "registration_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "registration_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
                {"AttributeName": "device_name", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "usecase-device-index",
                "KeySchema": [
                    {"AttributeName": "usecase_id", "KeyType": "HASH"},
                    {"AttributeName": "device_name", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=DEVICES_TABLE,
            KeySchema=[{"AttributeName": "device_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "device_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=AUDIT_LOG_TABLE,
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=ARTIFACTS_BUCKET)

        for module_name in ("shared_utils", "token_service", "rate_limiter",
                            "session_policy", "quick_setup"):
            sys.modules.pop(module_name, None)
        import shared_utils  # noqa: F401
        import token_service  # noqa: F401
        import rate_limiter  # noqa: F401
        import session_policy  # noqa: F401
        import quick_setup

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "qs": quick_setup,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "devices": resource.Table(DEVICES_TABLE),
            "audit": resource.Table(AUDIT_LOG_TABLE),
        }


def _status_event(body):
    """Minimal API Gateway proxy event for POST /quick-setup/status."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/status",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": "203.0.113.7"},
        },
    }


def _seed_registration(registrations, *, status="in_progress", secret="s3cr3t"):
    """Put a Device_Registration that has already exchanged credentials (so it
    carries a report_secret_hash) in the given ``status``; returns its stored
    item (including a unique device_name / usecase_id)."""
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"
    device_name = f"device-{uuid.uuid4().hex[:12]}"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    item = {
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": device_name,
        "device_group": "DDA_transition_EC2_Group",
        "status": status,
        "token_hash": hashlib.sha256(b"consumed-token").hexdigest(),
        "token_expires_at": 4_000_000_000,
        "consumed_at": 1_700_000_000,
        "report_secret_hash": secret_hash,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    registrations.put_item(Item=item)
    return item


def _device_item(devices, device_id):
    return devices.get_item(Key={"device_id": device_id}).get("Item")


def _report(qs, registration_id, secret, *, arch="__omit__", status="completed"):
    body = {
        "registration_id": registration_id,
        "report_secret": secret,
        "status": status,
    }
    if arch != "__omit__":
        body["target_architecture"] = arch
    return qs.handler(_status_event(body), None)


def _audit_actions(audit):
    """All audit-event action strings currently in the table."""
    return [it.get("action") for it in audit.scan().get("Items", [])]


# ---------------------------------------------------------------------------
# (a) completed + valid arch -> Devices UpdateItem + audit event
# ---------------------------------------------------------------------------

def test_completed_valid_arch_writes_device_and_audit(qs_stack):
    """**Validates: Requirements 2.2, 2.6**"""
    qs = qs_stack["qs"]
    seeded = _seed_registration(qs_stack["registrations"])
    device_name = seeded["device_name"]
    usecase_id = seeded["usecase_id"]

    resp = _report(qs, seeded["registration_id"], "s3cr3t", arch="arm64_jp6")
    assert resp["statusCode"] == 200, resp

    item = _device_item(qs_stack["devices"], device_name)
    assert item is not None
    assert item["target_architecture"] == "arm64_jp6"
    assert item["usecase_id"] == usecase_id
    assert item["updated_by"] == "quick-setup"
    assert "updated_at" in item

    # A record_target_architecture audit event was written for this device.
    events = [
        it for it in qs_stack["audit"].scan().get("Items", [])
        if it.get("action") == "record_target_architecture"
        and it.get("resource_id") == device_name
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev["resource_type"] == "device"
    assert ev["details"]["target_architecture"] == "arm64_jp6"
    assert ev["details"]["outcome"] == "success"


# ---------------------------------------------------------------------------
# (b) completed + invalid arch -> no write, still completed
# ---------------------------------------------------------------------------

def test_completed_invalid_arch_no_write_still_completed(qs_stack):
    """**Validates: Requirements 2.3**"""
    qs = qs_stack["qs"]
    seeded = _seed_registration(qs_stack["registrations"])
    device_name = seeded["device_name"]

    resp = _report(qs, seeded["registration_id"], "s3cr3t",
                   arch="arm64_jp7")  # not in the fixed set
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["status"] == "completed"

    # Registration still transitioned to completed.
    reg = qs_stack["registrations"].get_item(
        Key={"registration_id": seeded["registration_id"]})["Item"]
    assert reg["status"] == "completed"
    # No Devices-table item was created for an out-of-set value.
    assert _device_item(qs_stack["devices"], device_name) is None


# ---------------------------------------------------------------------------
# (c) completed + no arch -> no write, still completed
# ---------------------------------------------------------------------------

def test_completed_no_arch_no_write_still_completed(qs_stack):
    """**Validates: Requirements 2.4**"""
    qs = qs_stack["qs"]
    seeded = _seed_registration(qs_stack["registrations"])
    device_name = seeded["device_name"]

    resp = _report(qs, seeded["registration_id"], "s3cr3t")  # arch omitted
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["status"] == "completed"
    assert _device_item(qs_stack["devices"], device_name) is None


# ---------------------------------------------------------------------------
# (d) Devices-table failure -> still 200
# ---------------------------------------------------------------------------

def test_devices_table_failure_still_completes(qs_stack, monkeypatch):
    """**Validates: Requirements 2 (non-destructive best-effort write)**"""
    qs = qs_stack["qs"]
    seeded = _seed_registration(qs_stack["registrations"])

    # Point the recorder at a non-existent Devices table so update_item raises;
    # the completion must still return 200 (failure is logged and swallowed).
    monkeypatch.setattr(qs, "DEVICES_TABLE", "does-not-exist-table")

    resp = _report(qs, seeded["registration_id"], "s3cr3t", arch="x86_64")
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["status"] == "completed"

    reg = qs_stack["registrations"].get_item(
        Key={"registration_id": seeded["registration_id"]})["Item"]
    assert reg["status"] == "completed"


# ---------------------------------------------------------------------------
# (e) unauthenticated / mismatched report -> no write
# ---------------------------------------------------------------------------

def test_unauthenticated_report_no_write(qs_stack):
    """**Validates: Requirements 2.5**"""
    qs = qs_stack["qs"]
    seeded = _seed_registration(qs_stack["registrations"], secret="right")
    device_name = seeded["device_name"]

    resp = _report(qs, seeded["registration_id"], "wrong", arch="x86_64")
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == INVALID_TOKEN_ERROR_CODE

    # No transition, no arch write.
    reg = qs_stack["registrations"].get_item(
        Key={"registration_id": seeded["registration_id"]})["Item"]
    assert reg["status"] == "in_progress"
    assert _device_item(qs_stack["devices"], device_name) is None


# ---------------------------------------------------------------------------
# Property 7: Fixed-set write gate
# ---------------------------------------------------------------------------

# Arch values that straddle the fixed set: exact members, near-misses, empty,
# and arbitrary text.
arch_values = st.one_of(
    st.sampled_from(TARGET_ARCHITECTURES),
    st.sampled_from(["arm64_jp7", "x86", "X86_64", "arm64", "aarch64", ""]),
    st.text(max_size=24),
    st.none(),
)


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(arch=arch_values, current_status=st.sampled_from(REPORTABLE_FROM))
def test_fixed_set_write_gate(qs_stack, arch, current_status):
    """**Feature: device-arch-compatibility, Property 7: Fixed-set write gate**

    The Devices-table write happens iff the reported value is a member of the
    fixed set, and the registration reaches ``completed`` regardless of the
    arch value's validity or presence.

    **Validates: Requirements 2.2, 2.3, 2.4**
    """
    qs = qs_stack["qs"]
    devices = qs_stack["devices"]
    registrations = qs_stack["registrations"]

    seeded = _seed_registration(registrations, status=current_status)
    device_name = seeded["device_name"]

    # ``None`` models an omitted field; any other value is sent as-is.
    resp = _report(
        qs, seeded["registration_id"], "s3cr3t",
        arch="__omit__" if arch is None else arch,
    )

    # Completion is reached regardless of the arch value.
    assert resp["statusCode"] == 200, (arch, resp)
    assert json.loads(resp["body"])["status"] == "completed"
    reg = registrations.get_item(
        Key={"registration_id": seeded["registration_id"]})["Item"]
    assert reg["status"] == "completed"

    # The write happens iff the value is a fixed-set member.
    item = _device_item(devices, device_name)
    should_write = arch in TARGET_ARCHITECTURES
    if should_write:
        assert item is not None
        assert item["target_architecture"] == arch
        assert item["updated_by"] == "quick-setup"
    else:
        assert item is None
