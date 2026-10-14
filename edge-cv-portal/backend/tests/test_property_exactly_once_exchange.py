"""Property test for exactly-once credential exchange (station-quick-setup
task 5.9).

**Feature: station-quick-setup, Property 15: Credential exchange is exactly-once and transitions to in_progress**

For any Device_Registration and any number of sequential or interleaved
credential-exchange attempts presenting the *same* Setup_Token, exactly one
attempt returns credentials -- leaving the registration ``in_progress`` with
the token consumed -- and every other attempt receives the uniform
invalid-token error.

**Validates: Requirements 5.4, 5.5**

This drives the *real* ``quick_setup`` credential-exchange path
(``_token_authenticated_request`` -> ``exchange_credentials``) end to end
against a moto-backed AWS stack: the device-registrations table (which also
backs the ``RATELIMIT#`` counters read by ``rate_limiter.check`` and holds the
atomic conditional consume that is the linearization point of the exchange),
the shared audit-log table (written by the strict audit-before-effect entry),
the use-cases table (resolved for the cross-account role, external id, account
id, and region), and STS (moto issues the short-lived scoped session
credentials from ``sts.assume_role``).

Concurrency modelling: DynamoDB's conditional ``UpdateItem`` -- the sole
mutation that consumes the token -- linearizes all writers, so any interleaving
of N concurrent exchange attempts is observationally equivalent to *some*
sequential order of those same attempts. The property therefore presents the
same token in a sequence of N attempts and asserts the exactly-once outcome;
that covers the concurrent case because the conditional consume admits exactly
one winner regardless of arrival order.

Each attempt is presented from a *distinct* source IP so the invalid-token
rate limiter (>10 invalid tokens in 5 minutes from one IP -> block) never trips
across the post-consume rejections and confounds the exactly-once assertion --
this test isolates the token-consume semantics, not the rate limiter (Property
11 covers that). Every registration id / use case id is unique per example so
the shared tables stay isolated across examples.
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

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

# Uniform invalid-token error body (Req 3.5 / 5.4): every losing attempt sees
# exactly this.
INVALID_TOKEN_ERROR_CODE = "invalid_token"

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128}.
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# A spread of plausible AWS regions so the exchange must reflect the specific
# Use_Case's region.
aws_regions = st.sampled_from([
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1",
    "eu-central-1", "ap-southeast-1", "ap-northeast-1",
])

# Issuance clock times: realistic wall-clock epochs (year 2001 to well beyond
# 2200). All attempts are presented at issuance time, so the remaining token
# lifetime is the full 90-minute TTL -- comfortably above the 900s STS floor.
# The lower bound is kept strictly positive on purpose: the exchange stamps
# ``consumed_at`` with the current epoch, and ``0`` is the "never consumed"
# sentinel, so only a degenerate exchange at the 1970 epoch would make the
# stamp collide with the sentinel (the exactly-once guarantee still holds there
# via the ``status = pending`` consume condition, but the stamp is then an
# unusable observable). No real deployment issues tokens in 1970.
issuance_times = st.integers(min_value=1_000_000_000, max_value=7_258_118_400)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket, and the real quick_setup / token_service
    / rate_limiter / shared_utils modules imported inside the mock so their
    module-level boto3 resources (DynamoDB, S3, STS) are intercepted by moto.

    Bundle-artifact env vars are set before importing quick_setup because that
    module reads them at import time; they are unused by the credential-
    exchange path but keep the module import self-consistent.
    """
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
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
            TableName=USECASES_TABLE,
            KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
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
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


def _credentials_event(token, source_ip):
    """Minimal API Gateway proxy event for POST /quick-setup/credentials
    carrying the Setup_Token in the body and the source IP the pipeline keys
    the rate limiter on."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/credentials",
        "body": json.dumps({"token": token}),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": source_ip},
        },
    }


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    region=aws_regions,
    attempts=st.integers(min_value=1, max_value=8),
    issued_at=issuance_times,
)
def test_credential_exchange_is_exactly_once(
    qs_stack, device_name, device_group, region, attempts, issued_at
):
    """**Feature: station-quick-setup, Property 15: Credential exchange is exactly-once and transitions to in_progress**

    **Validates: Requirements 5.4, 5.5**
    """
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]
    registrations = qs_stack["registrations"]
    usecases = qs_stack["usecases"]

    # Seed one Device_Registration with its own use case (cross-account role,
    # external id, account id, and region) and a real, unconsumed, unexpired
    # token.
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"

    usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": "123456789012",
        "cross_account_role_arn":
            "arn:aws:iam::123456789012:role/DDAPortalAccessRole",
        "external_id": f"ext-{uuid.uuid4()}",
        "region": region,
    })

    token, token_hash, expires_at = ts.generate_token(
        registration_id, now=issued_at
    )
    registrations.put_item(Item={
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": device_name,
        "device_group": device_group,
        "status": "pending",
        "token_hash": token_hash,
        "token_expires_at": expires_at,
        "consumed_at": 0,
        "created_at": issued_at,
        "updated_at": issued_at,
    })

    # Present the SAME token across `attempts` sequential requests, each from a
    # distinct source IP so the invalid-token rate limiter never trips on the
    # post-consume rejections. All attempts are presented at issuance time, so
    # the token is comfortably within its lifetime for every attempt.
    import time as _time
    _orig_time = _time.time
    _time.time = lambda: issued_at
    try:
        results = []
        for _ in range(attempts):
            source_ip = f"ip-{uuid.uuid4()}"
            response = qs.handler(
                _credentials_event(token, source_ip), None
            )
            results.append(
                (response["statusCode"], json.loads(response["body"]))
            )
    finally:
        _time.time = _orig_time

    # --- Exactly one attempt returns credentials (Req 5.4 exactly-once).
    successes = [(code, body) for code, body in results if code == 200]
    assert len(successes) == 1, results

    # --- The winning attempt returns usable Provisioning_Credentials plus the
    #     report secret (Req 5.1 material, exchanged exactly once).
    _, success_body = successes[0]
    creds = success_body["credentials"]
    assert creds["access_key_id"]
    assert creds["secret_access_key"]
    assert creds["session_token"]
    assert success_body["report_secret"]
    assert success_body["aws_region"] == region

    # --- Every other attempt is rejected with the uniform invalid-token error
    #     (Req 5.4): a concurrent/subsequent request for a consumed token loses
    #     the race and is indistinguishable from any other invalid token.
    losers = [(code, body) for code, body in results if code != 200]
    assert len(losers) == attempts - 1
    for code, body in losers:
        assert code == 403, (code, body)
        assert body["error"] == INVALID_TOKEN_ERROR_CODE, body

    # --- The registration transitioned to in_progress exactly once with the
    #     token consumed (Req 5.5): the atomic conditional consume is the
    #     linearization point, so the final state is deterministic regardless
    #     of how many attempts were made.
    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert stored["status"] == "in_progress"
    assert int(stored["consumed_at"]) > 0
    # A report-secret hash was stored atomically with the consume for later
    # status reporting; the raw secret is never persisted.
    assert stored.get("report_secret_hash")
