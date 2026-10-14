"""Unit tests: regenerate and delete reject a completed registration
(station-quick-setup task 3.12).

A Device_Registration whose Setup_Status is ``completed`` is terminal: the
portal must neither issue a new Setup_Command for it (Req 2.8) nor delete it
(Req 6.9). Both handlers must reject the request and leave the stored item
byte-for-byte unchanged.

These run against the real ``device_registrations`` handler over a moto-backed
AWS stack (the registrations table + GSI plus the shared use-cases /
user-roles / audit tables) so the rejection path exercises the actual
persistence / RBAC code rather than a stub. Each test plants a ``completed``
registration holding a minted Setup_Token, invokes the handler as an
authorized user, and asserts a 409 rejection with the item unchanged.

_Requirements: 2.8, 6.9_
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import uuid

import pytest

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Table names distinct from the other self-contained test modules so this
# module's moto stack is independent.
REGISTRATIONS_TABLE = "test-completed-immutable-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"

# A role that holds Permission.MANAGE_DEVICES (authorized to regenerate/delete).
AUTHORIZED_ROLE = "UseCaseAdmin"


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
        import token_service
        import device_registrations

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "dr": device_registrations,
            "token_service": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


def _create_usecase(reg_stack):
    """A fresh single-account Use_Case, unique per test."""
    usecase_id = f"uc-{uuid.uuid4()}"
    reg_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Completed Immutable Test",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
        "external_id": "ext-id",
    })
    return usecase_id


def _plant_completed_registration(reg_stack, usecase_id, user_id):
    """Persist a ``completed`` Device_Registration holding a minted
    Setup_Token. Returns ``(registration_id, planted_item)``."""
    now = int(time.time())
    registration_id = str(uuid.uuid4())
    device_name = f"station-{uuid.uuid4().hex}"[:128]
    _, token_hash, token_expires_at = reg_stack["token_service"].generate_token(
        registration_id, now=now)
    item = {
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": device_name,
        "device_group": "Line3_Group",
        "status": "completed",
        "created_by": user_id,
        "created_at": now,
        "updated_at": now,
        "token_hash": token_hash,
        "token_expires_at": token_expires_at,
        "token_generation": 1,
        "consumed_at": now,
        "report_secret_hash": "b" * 64,
    }
    reg_stack["registrations"].put_item(Item=item)
    return registration_id, item


def _regenerate_event(user_id, role, registration_id):
    """A minimal API Gateway proxy POST /command event."""
    return {
        "httpMethod": "POST",
        "path": f"/device-registrations/{registration_id}/command",
        "pathParameters": {"id": registration_id},
        "body": json.dumps({}),
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


def _delete_event(user_id, role, registration_id):
    """A minimal API Gateway proxy DELETE event."""
    return {
        "httpMethod": "DELETE",
        "path": f"/device-registrations/{registration_id}",
        "pathParameters": {"id": registration_id},
        "body": None,
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


def _stored(reg_stack, registration_id):
    """The persisted registration item, or ``None`` if absent."""
    return reg_stack["registrations"].get_item(
        Key={"registration_id": registration_id}).get("Item")


# ---------------------------------------------------------------------------
# Req 2.8: regenerate rejects a completed registration, leaving it unchanged
# ---------------------------------------------------------------------------

def test_regenerate_rejects_completed_registration_unchanged(reg_stack):
    """Requesting a new Setup_Command for a ``completed`` registration is
    rejected and the stored item is left byte-for-byte unchanged (Req 2.8)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"
    registration_id, planted = _plant_completed_registration(
        reg_stack, usecase_id, user_id)
    before = copy.deepcopy(planted)

    response = dr.handler(
        _regenerate_event(user_id, AUTHORIZED_ROLE, registration_id), None)

    # --- Rejected: already completed (Req 2.8).
    assert response["statusCode"] == 409, response["body"]
    body = json.loads(response["body"])
    assert "completed" in body["error"].lower()
    # No new token material is leaked on the rejection path.
    assert "setup_command" not in body

    # --- The registration is unchanged, byte for byte: no new token issued,
    #     no status change, no generation bump (Req 2.8).
    assert _stored(reg_stack, registration_id) == before


# ---------------------------------------------------------------------------
# Req 6.9: delete rejects a completed registration, leaving it unchanged
# ---------------------------------------------------------------------------

def test_delete_rejects_completed_registration_unchanged(reg_stack):
    """Deleting a ``completed`` registration is rejected and the stored item is
    left byte-for-byte unchanged (Req 6.9)."""
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)
    user_id = f"user-{uuid.uuid4()}"
    registration_id, planted = _plant_completed_registration(
        reg_stack, usecase_id, user_id)
    before = copy.deepcopy(planted)

    response = dr.handler(
        _delete_event(user_id, AUTHORIZED_ROLE, registration_id), None)

    # --- Rejected: completed registrations cannot be deleted (Req 6.9).
    assert response["statusCode"] == 409, response["body"]
    body = json.loads(response["body"])
    assert "completed" in body["error"].lower()

    # --- The registration still exists, unchanged byte for byte (Req 6.9).
    assert _stored(reg_stack, registration_id) == before
