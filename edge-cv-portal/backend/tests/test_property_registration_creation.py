"""Property test for complete registration creation (station-quick-setup 3.2).

**Feature: station-quick-setup, Property 1: Valid registrations are created completely**

For any device name and Device_Group matching ``[a-zA-Z0-9:_-]{1,128}``
(including group names absent from the use-case account) and any authorized
user, submitting a registration creates exactly one Device_Registration with
status ``pending`` whose recorded device name, Device_Group, Use_Case,
creating user, and creation time equal the submitted values and request
context.

**Validates: Requirements 1.1, 1.6, 1.8**

This exercises the real ``device_registrations.create_registration`` handler
end to end against a moto-backed AWS stack (DynamoDB registrations table + GSI,
the shared use-cases / user-roles / audit tables, and a moto IoT endpoint for
the cross-account uniqueness lookup), so the property pins the actual persisted
item and API response rather than a stubbed value. The use case is configured
single-account (root ARN) so ``assume_cross_account_role`` resolves to the
Lambda's own (moto) credentials and the ``iot.describe_thing`` uniqueness probe
runs against the empty moto IoT account -> every generated device name is free
(Req 1.8: a Device_Group absent from the account is accepted as-is).
"""
from __future__ import annotations

import json
import os
import sys
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

# Roles that hold Permission.MANAGE_DEVICES for a Use_Case (Req 1.1 authorized
# user). PortalAdmin is a global super-user; the others are Use_Case scoped.
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
        import token_service  # noqa: F401
        import device_registrations

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "dr": device_registrations,
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


def _event(user_id, role, body):
    """A minimal API Gateway proxy event with a Cognito authorizer claim and
    the request-context fields the Setup_Command builder reads."""
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


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    user_id=user_ids,
    role=st.sampled_from(AUTHORIZED_ROLES),
)
def test_valid_registrations_are_created_completely(
    reg_stack, device_name, device_group, user_id, role
):
    """**Feature: station-quick-setup, Property 1: Valid registrations are created completely**

    For any valid device name / Device_Group and any authorized user,
    submitting a registration creates exactly one ``pending``
    Device_Registration whose recorded fields equal the submitted values and
    request context.

    **Validates: Requirements 1.1, 1.6, 1.8**
    """
    dr = reg_stack["dr"]
    usecase_id = _create_usecase(reg_stack)

    body = {
        "device_name": device_name,
        "device_group": device_group,
        "usecase_id": usecase_id,
    }

    # Bracket the call to bound the recorded creation time (Req 1.6).
    import time as _time
    before = int(_time.time())
    response = dr.handler(_event(user_id, role, body), None)
    after = int(_time.time())

    # --- The request succeeds and returns the created registration (Req 1.1).
    assert response["statusCode"] == 201, response["body"]
    payload = json.loads(response["body"])
    returned = payload["registration"]

    # --- Exactly one Device_Registration exists for this Use_Case (Req 1.1).
    from boto3.dynamodb.conditions import Key
    items = reg_stack["registrations"].query(
        IndexName="usecase-device-index",
        KeyConditionExpression=Key("usecase_id").eq(usecase_id),
    )["Items"]
    assert len(items) == 1, (
        f"expected exactly one registration for {usecase_id}, got {len(items)}")
    item = items[0]

    # --- Status is pending (Req 1.1).
    assert item["status"] == "pending"
    assert returned["status"] == "pending"

    # --- Recorded fields equal the submitted values (Req 1.6, 1.8). The
    #     Device_Group is stored verbatim even though it does not exist as an
    #     IoT Thing Group in the account (Req 1.8).
    assert item["device_name"] == device_name
    assert item["device_group"] == device_group
    assert item["usecase_id"] == usecase_id
    assert returned["device_name"] == device_name
    assert returned["device_group"] == device_group
    assert returned["usecase_id"] == usecase_id

    # --- Creating user recorded (Req 1.6).
    assert item["created_by"] == user_id
    assert returned["created_by"] == user_id

    # --- Creation time recorded within the request window (Req 1.6).
    created_at = int(item["created_at"])
    assert before <= created_at <= after
    assert int(returned["created_at"]) == created_at

    # --- The single valid registration id is coherent across item / response.
    assert item["registration_id"] == returned["registration_id"]
