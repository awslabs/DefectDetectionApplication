"""Property test for bounded credential lifetime (station-quick-setup task 5.8).

**Feature: station-quick-setup, Property 14: Credential validity never exceeds remaining token lifetime**

For any Setup_Token and any exchange time within its lifetime, the requested
credential duration is at most the token's remaining lifetime at issuance, and
the exchange is refused outright when the remainder is below the STS minimum
session duration (900s).

**Validates: Requirements 5.3**

This drives the *real* ``quick_setup`` request pipeline
(``_token_authenticated_request`` -> ``exchange_credentials``) end to end
against a moto-backed AWS stack: the device-registrations table (which also
backs the ``RATELIMIT#`` counters read by ``rate_limiter.check``), the shared
audit-log table (written by the strict audit-before-effect entry), and the
use-cases table (resolved for the cross-account role / region / account id).
Only ``sts.assume_role`` is stubbed -- with a recording fake -- so the test can
observe the exact ``DurationSeconds`` the handler requests while still exercising
the true remaining-lifetime arithmetic, the 900s-floor refusal, and the atomic
token-consume that follows a successful mint.

Each example presents a token at an exchange time strictly inside its 90-minute
lifetime (so ``TokenService.validate_token`` classifies it ``VALID`` and the
pipeline reaches ``exchange_credentials``). The remaining lifetime is generated
directly and the exchange time derived from it, giving uniform coverage of both
the below-floor (refuse) and above-floor (mint) branches; explicit boundary
examples pin the 900s floor and the 3600s role-chaining ceiling. Every
registration id / use case id / source IP is unique per example so the shared
tables stay isolated and the invalid-token rate limiter never trips across
examples.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, example, given, settings
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

# The 90-minute token lifetime and the STS session-duration bounds mirrored
# from the module under test (kept as literals here so the test pins the
# contract independently of the implementation constants).
TOKEN_TTL_SECONDS = 90 * 60          # 5400
STS_MIN_DURATION_SECONDS = 900       # AssumeRole floor -> refuse below this
STS_MAX_DURATION_SECONDS = 3600      # role-chaining ceiling


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables, and the real quick_setup / token_service / rate_limiter /
    shared_utils modules imported inside the mock so their module-level boto3
    resources are intercepted by moto.

    ``sts.assume_role`` is replaced per example by the test with a recording
    fake; everything else runs against moto.
    """
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["PORTAL_ARTIFACTS_BUCKET"] = ARTIFACTS_BUCKET
    os.environ.setdefault(
        "QUICK_SETUP_BUNDLE_KEY", "quick-setup/current/setup-bundle.tar.gz"
    )
    os.environ.setdefault("QUICK_SETUP_BUNDLE_SHA256", "a" * 64)
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
        import quick_setup

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "qs": quick_setup,
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


class _RecordingSTS:
    """Stub for ``quick_setup.sts`` that records every ``assume_role`` call and
    returns well-formed short-lived credentials.

    The ``Expiration`` returned is ``now + DurationSeconds`` (a real datetime,
    since ``exchange_credentials`` serializes it via ``.isoformat()``); the
    property under test only cares about the *requested* ``DurationSeconds``,
    which is captured in :attr:`calls`.
    """

    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        duration = kwargs.get("DurationSeconds", 0)
        expiration = datetime.now(timezone.utc) + timedelta(seconds=duration)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAFAKEACCESSKEYID",
                "SecretAccessKey": "fake-secret-access-key",
                "SessionToken": "fake-session-token",
                "Expiration": expiration,
            }
        }


def _seed_valid_registration(qs_stack, issued_at):
    """Store a use case and a fresh ``pending`` registration with a real,
    unconsumed token issued at ``issued_at`` (expiring at
    ``issued_at + TOKEN_TTL_SECONDS``). Returns ``(token, expires_at)``."""
    ts = qs_stack["ts"]
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"

    qs_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": "123456789012",
        "region": REGION,
        "cross_account_role_arn": (
            "arn:aws:iam::123456789012:role/DDAPortalAccessRole"
        ),
        "external_id": "ext-id-123",
    })

    token, token_hash, expires_at = ts.generate_token(
        registration_id, now=issued_at
    )
    qs_stack["registrations"].put_item(Item={
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": "station-42",
        "device_group": "Line3_Group",
        "status": "pending",
        "token_hash": token_hash,
        "token_expires_at": expires_at,
        "consumed_at": 0,
        "created_at": issued_at,
        "updated_at": issued_at,
    })
    return token, expires_at


def _credentials_event(token, source_ip):
    """Minimal API Gateway proxy event for POST /quick-setup/credentials."""
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


# Issuance clock times, from the epoch to well beyond the year 2200, so the
# arithmetic must hold regardless of absolute time.
issuance_times = st.integers(min_value=0, max_value=7_258_118_400)

# The remaining token lifetime at the moment of exchange, in seconds. Kept in
# [1, TOKEN_TTL_SECONDS] so the exchange time is strictly inside the lifetime
# (the token validates as VALID and the pipeline reaches exchange_credentials).
remaining_lifetimes = st.integers(min_value=1, max_value=TOKEN_TTL_SECONDS)


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(issued_at=issuance_times, remaining=remaining_lifetimes)
# Boundary examples around the 900s floor and the 3600s ceiling.
@example(issued_at=1_730_000_000, remaining=1)                        # far below floor
@example(issued_at=1_730_000_000, remaining=STS_MIN_DURATION_SECONDS - 1)  # just below
@example(issued_at=1_730_000_000, remaining=STS_MIN_DURATION_SECONDS)      # exactly floor
@example(issued_at=1_730_000_000, remaining=STS_MIN_DURATION_SECONDS + 1)  # just above
@example(issued_at=1_730_000_000, remaining=STS_MAX_DURATION_SECONDS - 1)  # just below ceiling
@example(issued_at=1_730_000_000, remaining=STS_MAX_DURATION_SECONDS)      # exactly ceiling
@example(issued_at=1_730_000_000, remaining=STS_MAX_DURATION_SECONDS + 1)  # just above ceiling
@example(issued_at=1_730_000_000, remaining=TOKEN_TTL_SECONDS)             # full lifetime
def test_credential_duration_never_exceeds_remaining_token_lifetime(
    qs_stack, issued_at, remaining
):
    """**Feature: station-quick-setup, Property 14: Credential validity never exceeds remaining token lifetime**

    **Validates: Requirements 5.3**
    """
    qs = qs_stack["qs"]

    token, expires_at = _seed_valid_registration(qs_stack, issued_at)

    # Exchange time is derived from the generated remaining lifetime: with
    # remaining >= 1, now < expires_at, so the token is still valid.
    now = expires_at - remaining
    assert now < expires_at  # invariant: exchange happens within the lifetime

    recorder = _RecordingSTS()
    original_sts = qs.sts
    qs.sts = recorder

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: now
    try:
        response = qs.handler(
            _credentials_event(token, f"ip-{uuid.uuid4()}"), None
        )
    finally:
        _time.time = _orig_time
        qs.sts = original_sts

    body = json.loads(response["body"])

    if remaining < STS_MIN_DURATION_SECONDS:
        # Below the STS floor: the exchange is refused outright with the
        # distinct token_expired error, and NO credentials are minted.
        assert response["statusCode"] == 403, response
        assert body.get("error") == "token_expired", body
        assert recorder.calls == [], (
            "assume_role must not be called when the remaining lifetime is "
            "below the STS minimum"
        )
    else:
        # At or above the floor: credentials are issued, and the requested
        # DurationSeconds is min(ceiling, remaining) -- in particular NEVER
        # more than the remaining token lifetime (the core property).
        assert response["statusCode"] == 200, response
        assert len(recorder.calls) == 1, recorder.calls
        requested_duration = recorder.calls[0]["DurationSeconds"]

        # Core property: credential validity never exceeds remaining lifetime.
        assert requested_duration <= remaining, (
            f"requested {requested_duration}s exceeds remaining {remaining}s"
        )
        # And it is exactly the min of the role-chaining ceiling and remaining.
        assert requested_duration == min(STS_MAX_DURATION_SECONDS, remaining)
        # It also respects the STS floor and ceiling.
        assert STS_MIN_DURATION_SECONDS <= requested_duration <= STS_MAX_DURATION_SECONDS
