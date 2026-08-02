"""Property test for the single-valid-token invariant (station-quick-setup 3.10).

**Feature: station-quick-setup, Property 5: At most one Setup_Token is valid per registration**

For any Device_Registration and any sequence of command regenerations, only
the most recently issued token validates successfully; every earlier token,
every consumed token, and every token of a deleted registration is rejected
with the invalid-token error.

**Validates: Requirements 2.5, 3.4**

This exercises the real ``device_registrations`` handler (create +
regenerate + delete) together with ``token_service.validate_token`` against a
moto-backed AWS stack, so the property pins the actual persisted token
material rather than a stubbed value. Each ``regenerate_command`` performs the
single atomic ``UpdateItem`` that replaces the stored token hash (Req 2.5);
``validate_token`` resolves the token through that same item, so a superseded,
consumed, or deleted token collapses to the shared ``INVALID`` result
(Req 3.4). The use case is single-account (root ARN) so the cross-account
uniqueness probe runs against the empty moto IoT account and every generated
device name is free.
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

# Roles that hold Permission.MANAGE_DEVICES for a Use_Case (authorized to
# create/regenerate/delete). PortalAdmin is a global super-user.
AUTHORIZED_ROLES = ["UseCaseAdmin", "Operator", "PortalAdmin"]

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
        # (PK usecase_id, SK device_name) used for the uniqueness check.
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
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


def _create_usecase(reg_stack):
    """A fresh single-account use case (root ARN => Lambda-own credentials for
    the moto IoT uniqueness probe). Unique per example so device names never
    collide across generated cases."""
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


def _claims(user_id, role):
    return {"claims": {
        "sub": user_id,
        "email": f"{user_id}@example.com",
        "cognito:username": user_id,
        "custom:role": role,
    }}


def _create_event(user_id, role, body):
    """POST /device-registrations event."""
    return {
        "httpMethod": "POST",
        "path": "/device-registrations",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": _claims(user_id, role),
        },
    }


def _regenerate_event(user_id, role, registration_id):
    """POST /device-registrations/{id}/command event."""
    return {
        "httpMethod": "POST",
        "path": f"/device-registrations/{registration_id}/command",
        "pathParameters": {"id": registration_id},
        "body": "{}",
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": _claims(user_id, role),
        },
    }


def _delete_event(user_id, role, registration_id):
    """DELETE /device-registrations/{id} event."""
    return {
        "httpMethod": "DELETE",
        "path": f"/device-registrations/{registration_id}",
        "pathParameters": {"id": registration_id},
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": _claims(user_id, role),
        },
    }


def _extract_token(setup_command):
    """Pull the Setup_Token out of the one-line Setup_Command. The token is the
    final ``--token <value>`` argument and contains no whitespace."""
    marker = "--token "
    idx = setup_command.rindex(marker)
    return setup_command[idx + len(marker):].strip()


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    user_id=user_ids,
    role=st.sampled_from(AUTHORIZED_ROLES),
    num_regenerations=st.integers(min_value=0, max_value=5),
    terminal=st.sampled_from(["none", "consume", "delete"]),
)
def test_at_most_one_token_valid_per_registration(
    reg_stack, device_name, device_group, user_id, role,
    num_regenerations, terminal,
):
    """**Feature: station-quick-setup, Property 5: At most one Setup_Token is valid per registration**

    For any registration and any sequence of regenerations, only the most
    recently issued token validates; every earlier token is INVALID, a
    consumed token is INVALID, and after deletion every token (including the
    latest) is INVALID.

    **Validates: Requirements 2.5, 3.4**
    """
    dr = reg_stack["dr"]
    ts = reg_stack["ts"]
    ValidationResult = ts.ValidationResult
    usecase_id = _create_usecase(reg_stack)

    # --- Create the registration; capture the first issued token.
    create_response = dr.handler(
        _create_event(user_id, role, {
            "device_name": device_name,
            "device_group": device_group,
            "usecase_id": usecase_id,
        }), None)
    assert create_response["statusCode"] == 201, create_response["body"]
    create_payload = json.loads(create_response["body"])
    registration_id = create_payload["registration"]["registration_id"]

    # Ordered history of every token ever issued for this registration.
    tokens = [_extract_token(create_payload["setup_command"])]

    # --- Regenerate the Setup_Command a generated number of times. Each
    #     regeneration is the single atomic UpdateItem that supersedes the
    #     prior token (Req 2.5).
    for _ in range(num_regenerations):
        regen_response = dr.handler(
            _regenerate_event(user_id, role, registration_id), None)
        assert regen_response["statusCode"] == 200, regen_response["body"]
        regen_payload = json.loads(regen_response["body"])
        tokens.append(_extract_token(regen_payload["setup_command"]))

    # Distinct tokens each round: regeneration mints fresh secret material.
    assert len(set(tokens)) == len(tokens)

    # Validate all tokens at a fixed present time, comfortably before any
    # expiry (fresh 90-minute tokens), so the only cause of INVALID is
    # supersession/consumption/deletion rather than expiry.
    now = int(time.time())

    # --- Core invariant: exactly one token validates, and it is the latest
    #     one (Req 2.5). Every earlier token is rejected as INVALID (Req 3.4).
    for older_token in tokens[:-1]:
        assert ts.validate_token(older_token, now).result == ValidationResult.INVALID

    latest_token = tokens[-1]
    latest_result = ts.validate_token(latest_token, now).result
    assert latest_result == ValidationResult.VALID

    # Count of tokens that validate across the whole history is exactly one.
    valid_count = sum(
        1 for tok in tokens
        if ts.validate_token(tok, now).result == ValidationResult.VALID)
    assert valid_count == 1

    # --- Terminal action: consuming or deleting must invalidate the last
    #     remaining valid token, so afterwards NO token validates.
    if terminal == "consume":
        # Simulate the credential exchange consuming the token (Req 3.4): the
        # atomic consume sets consumed_at; validation then collapses the
        # consumed token to INVALID.
        reg_stack["registrations"].update_item(
            Key={"registration_id": registration_id},
            UpdateExpression="SET consumed_at = :now",
            ExpressionAttributeValues={":now": now},
        )
        assert (ts.validate_token(latest_token, now).result
                == ValidationResult.INVALID)
        for tok in tokens:
            assert (ts.validate_token(tok, now).result
                    == ValidationResult.INVALID)

    elif terminal == "delete":
        # Deleting the registration invalidates its token (Req 3.4): the
        # embedded registration id no longer resolves, so every token in the
        # history is INVALID.
        delete_response = dr.handler(
            _delete_event(user_id, role, registration_id), None)
        assert delete_response["statusCode"] == 200, delete_response["body"]
        for tok in tokens:
            assert (ts.validate_token(tok, now).result
                    == ValidationResult.INVALID)
