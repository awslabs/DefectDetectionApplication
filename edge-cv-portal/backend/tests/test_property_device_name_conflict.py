"""Property test for device-name conflict rejection (station-quick-setup task 3.4).

**Feature: station-quick-setup, Property 3: Conflicting device names are rejected**

For any set of existing device names (as IoT Things in the use-case account or
as existing Device_Registrations in the same Use_Case), submitting a
registration with any name from that set is rejected with a conflict error
identifying that name, and no Device_Registration is created.

**Validates: Requirements 1.3**

The property is exercised against the real ``device_registrations.create_registration``
handler with the moto-backed AWS stack from ``conftest`` (real DynamoDB tables
and a real cross-account IoT lookup through moto). The Use_Case is configured
as a single-account setup (root ``cross_account_role_arn``) so
``create_boto3_client`` resolves to a moto-intercepted IoT client — the same
code path the handler runs in production, with no AWS mocking of the handler
itself.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION, TEST_ENV

REGISTRATIONS_TABLE_NAME = "test-device-registrations"
USECASE_DEVICE_INDEX = "usecase-device-index"
ACCOUNT_ID = "123456789012"


@pytest.fixture(scope="module")
def reg_env(aws_stack):
    """Device-registrations table + a freshly bound handler module.

    Depends on ``aws_stack`` so the moto mock (and the real ``shared_utils``
    layer) is active before ``device_registrations`` binds its module-level
    boto3 resource and reads ``REGISTRATIONS_TABLE``.
    """
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE_NAME
    # Bootstrap checksum is embedded in the Setup_Command; only needed on the
    # success path, but set so command building never fails on the happy path.
    os.environ.setdefault("QUICK_SETUP_BOOTSTRAP_SHA256", "0" * 64)

    client = boto3.client("dynamodb", region_name=REGION)
    existing = client.list_tables().get("TableNames", [])
    if REGISTRATIONS_TABLE_NAME not in existing:
        client.create_table(
            TableName=REGISTRATIONS_TABLE_NAME,
            KeySchema=[{"AttributeName": "registration_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "registration_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
                {"AttributeName": "device_name", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": USECASE_DEVICE_INDEX,
                "KeySchema": [
                    {"AttributeName": "usecase_id", "KeyType": "HASH"},
                    {"AttributeName": "device_name", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )

    # Re-import so the module binds the table name above and the
    # moto-intercepted boto3 resource (conftest re-import pattern).
    sys.modules.pop("token_service", None)
    sys.modules.pop("device_registrations", None)
    import device_registrations  # noqa: E402

    resource = boto3.resource("dynamodb", region_name=REGION)

    # A single-account Use_Case: the root ARN routes the cross-account helper to
    # the Lambda's own (moto) credentials, so the IoT uniqueness lookup hits
    # moto's IoT service in-region.
    usecase_id = f"uc-{uuid.uuid4()}"
    resource.Table(TEST_ENV["USECASES_TABLE"]).put_item(Item={
        "usecase_id": usecase_id,
        "name": "Quick Setup Test",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
        "external_id": "",
    })

    yield SimpleNamespace(
        module=device_registrations,
        table=resource.Table(REGISTRATIONS_TABLE_NAME),
        iot=boto3.client("iot", region_name=REGION),
        usecase_id=usecase_id,
    )


def _make_user(role="Operator"):
    """An authorized (manage-devices) portal user, resolved from JWT claims."""
    user_id = f"user-{uuid.uuid4()}"
    return {
        "sub": user_id,
        "email": f"{user_id}@example.com",
        "cognito:username": user_id,
        "custom:role": role,
    }


def _event(claims, body):
    return {
        "httpMethod": "POST",
        "path": "/device-registrations",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "authorizer": {"claims": claims},
        },
    }


# Valid IoT Thing / Thing Group names per IOT_NAME_PATTERN = [a-zA-Z0-9:_-]{1,128}.
# Kept short so appended per-example uniqueness suffixes stay within 128 chars.
_iot_name_chars = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-"
)
valid_iot_names = st.text(alphabet=_iot_name_chars, min_size=1, max_size=60)

# Which channel the name already exists on (Req 1.3 lists both).
conflict_kinds = st.sampled_from(["iot_thing", "existing_registration"])


@settings(max_examples=150, deadline=None)
@given(base_name=valid_iot_names, group=valid_iot_names, kind=conflict_kinds)
def test_conflicting_device_names_are_rejected(reg_env, base_name, group, kind):
    """**Feature: station-quick-setup, Property 3: Conflicting device names are rejected**

    A registration whose device name already exists (as an IoT Thing in the
    Use_Case account, or as a Device_Registration in the same Use_Case) is
    rejected with a 409 conflict identifying that exact name, and creates no
    new Device_Registration.

    **Validates: Requirements 1.3**
    """
    module = reg_env.module
    usecase_id = reg_env.usecase_id

    # Per-example unique name so examples cannot contaminate one another; still
    # a valid IoT name (suffix drawn from the allowed alphabet, total <= 128).
    device_name = f"{base_name}-{uuid.uuid4().hex}"[:128]

    pre_registration_id = None
    try:
        if kind == "iot_thing":
            # Name already registered as an IoT Thing in the account (Req 1.3).
            reg_env.iot.create_thing(thingName=device_name)
            expected_existing = 0
        else:
            # Name already used by an existing Device_Registration in the same
            # Use_Case (Req 1.3). It must be discoverable via the GSI, so the
            # IoT lookup must first report the name free — no thing is created.
            pre_registration_id = str(uuid.uuid4())
            reg_env.table.put_item(Item={
                "registration_id": pre_registration_id,
                "usecase_id": usecase_id,
                "device_name": device_name,
                "device_group": "PreexistingGroup",
                "status": "pending",
                "created_by": "someone-else",
                "created_at": 1,
                "updated_at": 1,
                "token_hash": "x" * 64,
                "token_expires_at": 10_000,
                "token_generation": 1,
                "consumed_at": 0,
            })
            expected_existing = 1

        response = module.handler(
            _event(_make_user(), {
                "device_name": device_name,
                "device_group": group,
                "usecase_id": usecase_id,
            }),
            None,
        )
        body = json.loads(response["body"])

        # Rejected with a conflict error (Req 1.3).
        assert response["statusCode"] == 409, body
        # The error identifies the conflicting device name (Req 1.3).
        assert body.get("device_name") == device_name

        # No Device_Registration was created beyond any pre-existing one: the
        # GSI holds exactly the registrations that existed before the call.
        items = reg_env.table.query(
            IndexName=USECASE_DEVICE_INDEX,
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("usecase_id").eq(usecase_id)
                & boto3.dynamodb.conditions.Key("device_name").eq(device_name)
            ),
        ).get("Items", [])
        assert len(items) == expected_existing
        if kind == "existing_registration":
            # The one item present is the pre-existing registration, untouched.
            assert items[0]["registration_id"] == pre_registration_id
    finally:
        # Isolate examples: drop anything this example created.
        if kind == "iot_thing":
            try:
                reg_env.iot.delete_thing(thingName=device_name)
            except Exception:
                pass
        if pre_registration_id is not None:
            reg_env.table.delete_item(Key={"registration_id": pre_registration_id})
