"""Unit tests for device-registration creation failure paths
(station-quick-setup task 3.6).

Covers the non-property, failure-path acceptance criteria of
``device_registrations.create_registration``:

- Req 1.4: a caller without the manage-devices permission is rejected with an
  access-denied error and an audit event is recorded.
- Req 1.5: if that rejection audit event cannot be recorded, the whole
  registration operation fails with an error.
- Req 1.10: if device-name uniqueness cannot be verified (cross-account role
  assumption fails, or the IoT Thing lookup fails), the request is rejected
  with a verification-failed error and nothing is persisted.
- Req 2.7: if Setup_Token generation or storage fails, the operation fails
  with a "setup command could not be generated" error and no Device_
  Registration is persisted.

These run against the real ``device_registrations`` handler over a moto-backed
AWS stack (the registrations table + GSI, the shared use-cases / user-roles /
audit tables, and a moto IoT endpoint for the cross-account uniqueness lookup),
so each failure path exercises the actual persistence / RBAC / audit code
rather than a stub. The single-account Use_Case (root ARN) routes
``assume_cross_account_role`` to the Lambda's own (moto) credentials so the
``iot.describe_thing`` probe hits the empty moto IoT account -> the device name
is free on the happy path.

_Requirements: 1.4, 1.5, 1.10, 2.7_
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Table names, distinct from other self-contained test modules so this module's
# moto stack is independent.
REGISTRATIONS_TABLE = "test-dr-failure-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"

# A role that holds Permission.MANAGE_DEVICES (authorized to register devices).
AUTHORIZED_ROLE = "UseCaseAdmin"
# A role that does NOT hold Permission.MANAGE_DEVICES (Viewer is read-only).
UNAUTHORIZED_ROLE = "Viewer"


@pytest.fixture(scope="module")
def reg_stack():
    """moto-backed AWS with the device-registrations table + GSI and the real
    device_registrations / token_service / shared_utils modules imported inside
    the mock so their module-level boto3 resources are moto-intercepted."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["QUICK_SETUP_BOOTSTRAP_SHA256"] = "a" * 64

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
            TableName=USECASES_TABLE,
            KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "usecase_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=USER_ROLES_TABLE,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "usecase_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
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

        for module_name in ("shared_utils", "token_service", "device_registrations"):
            sys.modules.pop(module_name, None)
        import shared_utils  # noqa: F401
        import token_service  # noqa: F401
        import device_registrations

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "dr": device_registrations,
            "token_service": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
            "audit": resource.Table(AUDIT_LOG_TABLE),
        }


def _create_usecase(reg_stack):
    """A fresh single-account Use_Case (root ARN => Lambda-own moto creds for
    the IoT uniqueness probe)."""
    usecase_id = f"uc-{uuid.uuid4()}"
    reg_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Failure Path Test",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
        "external_id": "ext-id",
    })
    return usecase_id


def _event(user_id, role, body):
    """A minimal API Gateway proxy event with a Cognito authorizer claim."""
    return {
        "httpMethod": "POST",
        "path": "/device-registrations",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": {"claims": {
                "sub": user_id,
                "email": f"{user_id}@example.com",
                "cognito:username": user_id,
                "custom:role": role,
            }},
        },
    }


def _registrations_for(reg_stack, usecase_id):
    """All persisted registrations for a Use_Case (via the GSI)."""
    from boto3.dynamodb.conditions import Key
    return reg_stack["registrations"].query(
        IndexName="usecase-device-index",
        KeyConditionExpression=Key("usecase_id").eq(usecase_id),
    )["Items"]


def _audit_events(reg_stack, user_id, action=None):
    """All audit records for one acting user (each test uses a fresh uuid user
    id, isolating events without table truncation)."""
    items, kwargs = [], {}
    while True:
        response = reg_stack["audit"].scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    events = [i for i in items if i.get("user_id") == user_id]
    if action is not None:
        events = [e for e in events if e.get("action") == action]
    return events


# ---------------------------------------------------------------------------
# Req 1.4: RBAC denial -> access-denied + audit event
# ---------------------------------------------------------------------------

def test_rbac_denial_returns_access_denied_and_records_audit(reg_stack):
    """A user without manage-devices is rejected with 403 access-denied, an
    audit event is recorded, and no Device_Registration is created (Req 1.4)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"
    device_name = f"denied-{uuid.uuid4().hex}"[:128]

    response = dr.handler(_event(user_id, UNAUTHORIZED_ROLE, {
        "device_name": device_name,
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    assert response["statusCode"] == 403, response["body"]
    assert json.loads(response["body"])["error"] == "Access denied"

    # An audit event was recorded for the denial (Req 1.4).
    events = _audit_events(reg_stack, user_id, "create_device_registration")
    assert len(events) == 1, "expected exactly one denial audit event"
    record = events[0]
    assert record["result"] == "rejected"
    assert record["resource_id"] == device_name
    assert record["details"]["reason"] == "access_denied"
    assert record["details"]["usecase_id"] == usecase_id

    # Nothing persisted.
    assert _registrations_for(reg_stack, usecase_id) == []


# ---------------------------------------------------------------------------
# Req 1.5: audit-write failure aborts the whole operation
# ---------------------------------------------------------------------------

def test_audit_write_failure_aborts_operation(reg_stack, monkeypatch):
    """If the denial audit event cannot be recorded, the whole registration
    operation fails with a 500 error and nothing is persisted (Req 1.5)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"

    def _boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalError", "Message": "audit down"}},
            "PutItem")

    monkeypatch.setattr(dr, "record_audit_event_strict", _boom)

    response = dr.handler(_event(user_id, UNAUTHORIZED_ROLE, {
        "device_name": f"audit-fail-{uuid.uuid4().hex}"[:128],
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    # Fail the whole operation rather than silently denying (Req 1.5).
    assert response["statusCode"] == 500, response["body"]
    assert "audit" in json.loads(response["body"])["error"].lower()

    # Nothing persisted.
    assert _registrations_for(reg_stack, usecase_id) == []


# ---------------------------------------------------------------------------
# Req 1.10: uniqueness unverifiable -> verification-failed, nothing persisted
# ---------------------------------------------------------------------------

def test_uniqueness_unverifiable_role_assumption_failure(reg_stack, monkeypatch):
    """If the cross-account role cannot be assumed, the request is rejected
    with a verification-failed error and nothing is persisted (Req 1.10)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"

    def _cannot_assume(*args, **kwargs):
        raise Exception("cross-account role assumption failed")

    monkeypatch.setattr(dr, "assume_cross_account_role", _cannot_assume)

    response = dr.handler(_event(user_id, AUTHORIZED_ROLE, {
        "device_name": f"verify-fail-{uuid.uuid4().hex}"[:128],
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 502, body
    assert "uniqueness" in body["error"].lower()

    # Nothing persisted (Req 1.10).
    assert _registrations_for(reg_stack, usecase_id) == []


def test_uniqueness_unverifiable_iot_lookup_failure(reg_stack, monkeypatch):
    """If the IoT Thing lookup fails with a non-not-found error, the request is
    rejected with a verification-failed error and nothing is persisted
    (Req 1.10)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"

    class _FailingIot:
        def describe_thing(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException",
                           "Message": "rate exceeded"}},
                "DescribeThing")

    # Role assumption succeeds; the IoT client itself fails the lookup.
    monkeypatch.setattr(dr, "assume_cross_account_role",
                        lambda *a, **k: {"AccessKeyId": "x"})
    monkeypatch.setattr(dr, "create_boto3_client",
                        lambda *a, **k: _FailingIot())

    response = dr.handler(_event(user_id, AUTHORIZED_ROLE, {
        "device_name": f"iot-fail-{uuid.uuid4().hex}"[:128],
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 502, body
    assert "uniqueness" in body["error"].lower()

    # Nothing persisted (Req 1.10).
    assert _registrations_for(reg_stack, usecase_id) == []


# ---------------------------------------------------------------------------
# Req 2.7: token generation / storage failure -> no registration persisted
# ---------------------------------------------------------------------------

def test_token_generation_failure_persists_no_registration(reg_stack, monkeypatch):
    """If Setup_Token generation fails, the operation fails with a
    'setup command could not be generated' error and nothing is persisted
    (Req 2.7)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"

    def _boom(*args, **kwargs):
        raise RuntimeError("CSPRNG unavailable")

    monkeypatch.setattr(dr.token_service, "generate_token", _boom)

    response = dr.handler(_event(user_id, AUTHORIZED_ROLE, {
        "device_name": f"token-fail-{uuid.uuid4().hex}"[:128],
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 500, body
    assert "setup command could not be generated" in body["error"].lower()

    # No token-less registration persisted (Req 2.7).
    assert _registrations_for(reg_stack, usecase_id) == []


def test_token_storage_failure_persists_no_registration(reg_stack, monkeypatch):
    """If persisting the registration (with its token) fails, the operation
    fails and no Device_Registration is persisted (Req 2.7)."""
    dr = reg_stack["dr"]
    real_table = reg_stack["registrations"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"

    class _FailingPutTable:
        """Delegates the uniqueness query to the real table but fails the
        conditional put so no item is ever written."""

        def query(self, **kwargs):
            return real_table.query(**kwargs)

        def put_item(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "storage down"}},
                "PutItem")

    monkeypatch.setattr(dr, "_registrations_table", lambda: _FailingPutTable())

    response = dr.handler(_event(user_id, AUTHORIZED_ROLE, {
        "device_name": f"store-fail-{uuid.uuid4().hex}"[:128],
        "device_group": "Line3_Group",
        "usecase_id": usecase_id,
    }), None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 500, body
    assert "setup command could not be generated" in body["error"].lower()

    # Nothing persisted (Req 2.7) — verified against the real table.
    assert _registrations_for(reg_stack, usecase_id) == []
