"""Property test for status-gated deletion (station-quick-setup 3.11).

**Feature: station-quick-setup, Property 17: Deletion is gated on status and invalidates the token**

For any Device_Registration, deletion succeeds if and only if its Setup_Status
is not ``completed``; after a successful deletion the registration no longer
exists and its token is rejected, and after a rejected deletion (status
``completed``) the registration is unchanged.

**Validates: Requirements 6.6, 6.9**

This exercises the real ``device_registrations.delete_registration`` handler
end to end against a moto-backed AWS stack (the DynamoDB registrations table +
GSI and the shared use-cases / user-roles / audit tables) together with the
real ``token_service.validate_token`` so the property pins the actual persisted
state and the actual token-validation outcome rather than a stubbed value.

Each example plants a registration with a freshly minted Setup_Token in a
generated Setup_Status, invokes the delete handler as an authorized user, and
asserts the gate:

* status ``completed``  -> 409 rejection, item byte-for-byte unchanged, and the
  Setup_Token still validates (Req 6.9);
* any other status      -> 200 deletion, the item is gone, and the Setup_Token
  now validates as INVALID because validation resolves through the item that no
  longer exists (Req 6.6).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128} (Req 1.2).
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# User ids are opaque principal identifiers (Cognito ``sub`` claim).
user_ids = st.from_regex(r"[a-zA-Z0-9_-]{1,40}", fullmatch=True)

# Roles that hold Permission.MANAGE_DEVICES for a Use_Case (authorized deleter).
AUTHORIZED_ROLES = ["UseCaseAdmin", "Operator", "PortalAdmin"]

# Every Setup_Status a registration can hold. Only ``completed`` blocks deletion
# (Req 6.9); the other four must all delete successfully (Req 6.6).
ALL_STATUSES = ["pending", "in_progress", "expired", "failed", "completed"]
DELETABLE_STATUSES = ["pending", "in_progress", "expired", "failed"]

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Shared table names match conftest's TEST_ENV so the real shared_utils module
# (which reads these at import time) resolves to the moto-backed tables.
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
REGISTRATIONS_TABLE = "test-device-registrations"


@pytest.fixture(scope="module")
def reg_stack():
    """moto-backed AWS with the device-registrations table + GSI and the real
    device_registrations / token_service / shared_utils modules imported inside
    the mock so their module-level boto3 resources are intercepted by moto."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["QUICK_SETUP_BOOTSTRAP_SHA256"] = "a" * 64

    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name=REGION)
        # Registrations table (PK registration_id) + usecase-device-index GSI
        # (PK usecase_id, SK device_name).
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

        # Re-import the real modules inside the active mock so their
        # module-level boto3 resources/clients are moto-backed and consistent
        # with each other (shared Permission enum / rbac_manager).
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
    """A fresh single-account use case, unique per example."""
    usecase_id = f"uc-{uuid.uuid4()}"
    reg_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
        "external_id": "ext-id",
    })
    return usecase_id


def _plant_registration(reg_stack, usecase_id, device_name, device_group,
                        user_id, status, now):
    """Persist a Device_Registration in ``status`` holding a freshly minted
    Setup_Token. Returns ``(registration_id, token, item)``."""
    registration_id = str(uuid.uuid4())
    token, token_hash, token_expires_at = reg_stack["token_service"].generate_token(
        registration_id, now=now)
    item = {
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": device_name,
        "device_group": device_group,
        "status": status,
        "created_by": user_id,
        "created_at": now,
        "updated_at": now,
        "token_hash": token_hash,
        "token_expires_at": token_expires_at,
        "token_generation": 1,
        "consumed_at": 0,
    }
    reg_stack["registrations"].put_item(Item=item)
    return registration_id, token, item


def _delete_event(user_id, role, registration_id):
    """A minimal API Gateway proxy DELETE event with a Cognito authorizer."""
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


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    user_id=user_ids,
    role=st.sampled_from(AUTHORIZED_ROLES),
    status=st.sampled_from(ALL_STATUSES),
)
def test_deletion_is_gated_on_status_and_invalidates_the_token(
    reg_stack, device_name, device_group, user_id, role, status
):
    """**Feature: station-quick-setup, Property 17: Deletion is gated on status and invalidates the token**

    Deletion succeeds iff the Setup_Status is not ``completed``; a successful
    deletion removes the registration and its token stops validating, while a
    rejected deletion leaves a ``completed`` registration unchanged with its
    token still resolving.

    **Validates: Requirements 6.6, 6.9**
    """
    dr = reg_stack["dr"]
    token_service = reg_stack["token_service"]
    ValidationResult = token_service.ValidationResult

    now = int(time.time())
    usecase_id = _create_usecase(reg_stack)
    registration_id, token, planted = _plant_registration(
        reg_stack, usecase_id, device_name, device_group, user_id, status, now)

    # Precondition: while the item exists and the token is unexpired/unconsumed,
    # the Setup_Token validates (VALID), so a post-deletion INVALID is a genuine
    # consequence of removing the item (Req 6.6).
    pre = token_service.validate_token(token, now)
    assert pre.result == ValidationResult.VALID

    response = dr.handler(_delete_event(user_id, role, registration_id), None)
    status_code = response["statusCode"]

    stored = reg_stack["registrations"].get_item(
        Key={"registration_id": registration_id}).get("Item")
    post = token_service.validate_token(token, now)

    if status == "completed":
        # --- Rejected: completed registrations cannot be deleted (Req 6.9).
        assert status_code == 409, response["body"]
        # --- The registration is unchanged, byte for byte.
        assert stored == planted
        # --- Its token still resolves (nothing was invalidated).
        assert post.result == ValidationResult.VALID
    else:
        # --- Deletion succeeds for every non-completed status (Req 6.6).
        assert status_code == 200, response["body"]
        assert json.loads(response["body"])["deleted"] is True
        # --- The registration no longer exists.
        assert stored is None
        # --- Its Setup_Token is now rejected (resolves to INVALID through the
        #     missing item), i.e. the token was invalidated by deletion (Req 6.6).
        assert post.result == ValidationResult.INVALID
