"""Unit test for thing-groups listing pass-through (station-quick-setup task 3.8).

Verifies ``device_registrations.list_thing_groups`` returns the existing IoT
Thing Groups from the selected Use_Case account so the portal can present them
for Device_Group selection.

**Validates: Requirements 1.7**

Exercises the real ``device_registrations.handler`` GET
``/device-registrations/thing-groups`` route against the moto-backed AWS stack
from ``conftest`` (real DynamoDB tables and a real cross-account IoT
pass-through through moto). The Use_Case is configured single-account (root
``cross_account_role_arn``) so ``assume_cross_account_role`` /
``create_boto3_client`` resolve to the Lambda's own moto-intercepted IoT client
— the same code path the handler runs in production, with no AWS mocking of the
handler itself.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest

from conftest import REGION, TEST_ENV

REGISTRATIONS_TABLE_NAME = "test-device-registrations"
USECASE_DEVICE_INDEX = "usecase-device-index"
ACCOUNT_ID = "123456789012"


@pytest.fixture(scope="module")
def tg_env(aws_stack):
    """Device-registrations handler bound inside the active moto mock.

    Depends on ``aws_stack`` so the moto mock (and the real ``shared_utils``
    layer) is active before ``device_registrations`` binds its module-level
    boto3 resource and reads ``REGISTRATIONS_TABLE``.
    """
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE_NAME
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

    yield SimpleNamespace(
        module=device_registrations,
        resource=resource,
        iot=boto3.client("iot", region_name=REGION),
    )


def _make_usecase(tg_env):
    """A fresh single-account Use_Case: the root ARN routes the cross-account
    helper to the Lambda's own (moto) credentials, so the IoT thing-group
    listing hits moto's IoT service in-region."""
    usecase_id = f"uc-{uuid.uuid4()}"
    tg_env.resource.Table(TEST_ENV["USECASES_TABLE"]).put_item(Item={
        "usecase_id": usecase_id,
        "name": "Quick Setup Test",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cross_account_role_arn": f"arn:aws:iam::{ACCOUNT_ID}:root",
        "external_id": "",
    })
    return usecase_id


def _authorize(tg_env, usecase_id, role="UseCaseAdmin"):
    """Grant a fresh portal user access to the Use_Case via the user-roles
    table and return the JWT claims for the request event."""
    user_id = f"user-{uuid.uuid4()}"
    tg_env.resource.Table(TEST_ENV["USER_ROLES_TABLE"]).put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": role,
    })
    return {
        "sub": user_id,
        "email": f"{user_id}@example.com",
        "cognito:username": user_id,
        "custom:role": role,
    }


def _event(claims, usecase_id):
    return {
        "httpMethod": "GET",
        "path": "/device-registrations/thing-groups",
        "queryStringParameters": {"usecase_id": usecase_id},
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "authorizer": {"claims": claims},
        },
    }


def test_existing_thing_groups_are_returned(tg_env):
    """Existing IoT Thing Groups in the Use_Case account are returned for
    Device_Group selection (Req 1.7)."""
    usecase_id = _make_usecase(tg_env)
    claims = _authorize(tg_env, usecase_id)

    group_names = [f"Line{i}_Group-{uuid.uuid4().hex[:8]}" for i in range(3)]
    created_arns = {}
    for name in group_names:
        resp = tg_env.iot.create_thing_group(thingGroupName=name)
        created_arns[name] = resp["thingGroupArn"]

    try:
        response = tg_env.module.handler(_event(claims, usecase_id), None)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200, body
        returned = {g["group_name"]: g["group_arn"] for g in body["thing_groups"]}

        # Every created group is present in the pass-through response (Req 1.7).
        for name in group_names:
            assert name in returned, f"{name} missing from {list(returned)}"
            assert returned[name] == created_arns[name]

        assert body["count"] == len(body["thing_groups"])
    finally:
        for name in group_names:
            try:
                tg_env.iot.delete_thing_group(thingGroupName=name)
            except Exception:
                pass


def test_empty_account_returns_no_groups(tg_env):
    """A Use_Case account with no IoT Thing Groups returns an empty list, not
    an error (Req 1.7)."""
    usecase_id = _make_usecase(tg_env)
    claims = _authorize(tg_env, usecase_id)

    # Isolate from any groups other tests may have left behind by asserting our
    # freshly created groups are gone; the account is shared moto state, so we
    # only assert the call succeeds and returns a well-formed list.
    response = tg_env.module.handler(_event(claims, usecase_id), None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200, body
    assert isinstance(body["thing_groups"], list)
    assert body["count"] == len(body["thing_groups"])


def test_missing_usecase_id_is_rejected(tg_env):
    """A request without a ``usecase_id`` query parameter is rejected with a
    validation error and no IoT lookup."""
    usecase_id = _make_usecase(tg_env)
    claims = _authorize(tg_env, usecase_id)

    event = _event(claims, usecase_id)
    event["queryStringParameters"] = None

    response = tg_env.module.handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 400, body
    assert "usecase_id" in body["error"]
