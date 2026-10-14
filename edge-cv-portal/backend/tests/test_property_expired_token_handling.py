"""Property test for expired-token handling (station-quick-setup task 5.2).

**Feature: station-quick-setup, Property 7: Expired tokens are rejected and expire pending registrations**

For any Device_Registration and any presentation time at or after the token's
expiration, the Quick_Setup_Endpoint rejects the request with the expiration
error, and the registration's Setup_Status transitions to ``expired`` if and
only if it was ``pending``.

**Validates: Requirements 3.3**

This drives the *real* ``quick_setup`` request pipeline
(``_token_authenticated_request``) end to end against a moto-backed AWS stack:
the device-registrations table (which also backs the ``RATELIMIT#`` counters
read by ``rate_limiter.check``) and the shared audit-log table (written by the
strict audit-before-effect entry). So the property pins the actual HTTP
response and the actually-persisted Setup_Status transition rather than a
stubbed value.

The token is stored *unconsumed* (``consumed_at == 0``) and presented at or
after ``token_expires_at`` so ``TokenService.validate_token`` classifies it as
``EXPIRED`` — the precondition of Req 3.3. The stored status is varied across
the whole lifecycle set so the test exercises both directions of the
"transitions to ``expired`` if and only if it was ``pending``" biconditional:
a ``pending`` registration must become ``expired`` while every other status is
left unchanged. A unique registration id and a unique source IP per example
keep examples isolated in the shared tables (and keep the invalid-token rate
limiter from ever tripping across examples).
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Table names match conftest's TEST_ENV so the real shared_utils module (which
# reads these at import time) resolves to the same moto-backed tables.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"

# The full Setup_Status lifecycle set (design "State machine").
ALL_STATUSES = ["pending", "in_progress", "completed", "expired", "failed"]

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128}.
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# Issuance clock times, from the epoch to well beyond the year 2200.
issuance_times = st.integers(min_value=0, max_value=7_258_118_400)
# How far past expiration the token is presented (>= 0 == at or after expiry).
past_expiry_offsets = st.integers(min_value=0, max_value=10_000_000)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations + audit tables and the
    real quick_setup / token_service / rate_limiter / shared_utils modules
    imported inside the mock so their module-level boto3 resources are
    intercepted by moto."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    # get_bootstrap() reads these; not exercised by the expired-token path, but
    # set so the module imports cleanly and configuration is realistic.
    os.environ.setdefault("PORTAL_ARTIFACTS_BUCKET", "test-portal-artifacts")
    os.environ.setdefault("QUICK_SETUP_BOOTSTRAP_KEY", "quick-setup/current/bootstrap.sh")

    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name=REGION)
        # Registrations table (PK registration_id); the usecase-device-index
        # GSI is declared for parity with production though the expired path
        # does not query it.
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
        # module-level boto3 resources/clients are moto-backed and consistent.
        for module_name in ("shared_utils", "token_service", "rate_limiter",
                            "quick_setup"):
            sys.modules.pop(module_name, None)
        import shared_utils  # noqa: F401
        import token_service  # noqa: F401
        import rate_limiter  # noqa: F401
        import quick_setup

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "qs": quick_setup,
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
        }


def _bundle_event(token, source_ip):
    """A minimal API Gateway proxy event for POST /quick-setup/bundle carrying
    the Setup_Token in the body and the source IP the pipeline keys on."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/bundle",
        "body": json.dumps({"token": token}),
        "requestContext": {"identity": {"sourceIp": source_ip}},
    }


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    initial_status=st.sampled_from(ALL_STATUSES),
    issued_at=issuance_times,
    past_offset=past_expiry_offsets,
)
def test_expired_tokens_are_rejected_and_expire_pending_registrations(
    qs_stack, device_name, device_group, initial_status, issued_at, past_offset
):
    """**Feature: station-quick-setup, Property 7: Expired tokens are rejected and expire pending registrations**

    **Validates: Requirements 3.3**
    """
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]
    registrations = qs_stack["registrations"]

    # Unique keys per example -> isolation in the shared tables and no
    # invalid-token rate-limit accumulation across examples.
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"
    source_ip = f"ip-{uuid.uuid4()}"

    # Mint a real token for this registration and store only the secret hash
    # (Req 3.6), unconsumed, expiring at issued_at + 90 min.
    token, token_hash, expires_at = ts.generate_token(registration_id, now=issued_at)
    registrations.put_item(Item={
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": device_name,
        "device_group": device_group,
        "status": initial_status,
        "token_hash": token_hash,
        "token_expires_at": expires_at,
        "consumed_at": 0,
        "created_at": issued_at,
        "updated_at": issued_at,
    })

    # Present the token at or after its expiration (Req 3.3 precondition).
    presented_at = expires_at + past_offset
    import time as _time
    _orig_time = _time.time
    _time.time = lambda: presented_at
    try:
        response = qs.handler(_bundle_event(token, source_ip), None)
    finally:
        _time.time = _orig_time

    # --- The request is rejected with the DISTINCT expiration error (Req 3.3),
    #     not the uniform invalid-token error.
    assert response["statusCode"] == 403, response
    body = json.loads(response["body"])
    assert body["error"] == "token_expired", body
    assert body["error"] != "invalid_token"
    assert body == qs.TOKEN_EXPIRED_ERROR

    # --- Setup_Status transitions to `expired` if and only if it was `pending`.
    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    if initial_status == "pending":
        assert stored["status"] == "expired", (
            f"pending registration must transition to expired, got "
            f"{stored['status']!r}")
    else:
        assert stored["status"] == initial_status, (
            f"non-pending status {initial_status!r} must be left unchanged, "
            f"got {stored['status']!r}")
