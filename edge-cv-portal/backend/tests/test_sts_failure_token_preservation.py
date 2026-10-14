"""Unit test for STS-failure token preservation (station-quick-setup task 5.10).

Requirement 5.7: IF Provisioning_Credentials cannot be issued after a
Setup_Token has been successfully validated, THEN the Quick_Setup_Endpoint
returns an issuance error and the Portal_Backend does NOT mark the Setup_Token
as consumed, so the Setup_Bundle can retry the credential exchange within the
remaining Setup_Token lifetime.

These tests drive the *real* ``quick_setup`` request pipeline
(``_token_authenticated_request`` -> ``exchange_credentials``) end to end
against a moto-backed AWS stack: the device-registrations table (which also
backs the ``RATELIMIT#`` counters read by ``rate_limiter.check`` and holds the
atomic conditional consume), the shared audit-log table (written by the strict
audit-before-effect entry), and the use-cases table (resolved for the
cross-account role, external id, account id, and region). ``sts.assume_role``
is replaced with a fake that fails, so the credential-issuance step raises
*after* the token has already validated -- exactly the Req 5.7 window.

The assertions confirm that after an STS failure:
    * the endpoint returns an issuance error (not a token error), and
    * the registration is untouched -- ``consumed_at`` is still ``0`` (the
      "never consumed" sentinel) and ``status`` is still ``pending`` -- so a
      subsequent exchange with a working STS succeeds and consumes the token
      exactly once (the retry path).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket, and the real quick_setup / token_service
    / rate_limiter / shared_utils modules imported inside the mock so their
    module-level boto3 resources are intercepted by moto.

    ``sts.assume_role`` is swapped per test by the caller; everything else runs
    against moto.
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
        import session_policy  # noqa: F401
        import quick_setup

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "qs": quick_setup,
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


class _FailingSTS:
    """Stub for ``quick_setup.sts`` whose ``assume_role`` always raises the
    given exception, simulating a credential-issuance failure that occurs
    *after* the Setup_Token has already validated (the Req 5.7 window)."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def assume_role(self, **kwargs):
        self.calls += 1
        raise self._exc


class _RecordingSTS:
    """Stub for ``quick_setup.sts`` that returns well-formed short-lived
    credentials, used to prove the exchange succeeds on retry."""

    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        duration = kwargs.get("DurationSeconds", 3600)
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
    unconsumed token issued at ``issued_at``. Returns
    ``(registration_id, token)``."""
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
    return registration_id, token


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


# An STS ClientError (the common failure mode) and a KeyError (malformed
# response missing ``Credentials``) are both caught by exchange_credentials.
_STS_CLIENT_ERROR = ClientError(
    {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
    "AssumeRole",
)


@pytest.mark.parametrize("sts_exc", [_STS_CLIENT_ERROR, KeyError("Credentials")])
def test_sts_failure_returns_issuance_error_and_leaves_token_unconsumed(
    qs_stack, sts_exc
):
    """After a validated token, an STS failure returns an issuance error and
    leaves the registration untouched (Req 5.7)."""
    qs = qs_stack["qs"]
    registrations = qs_stack["registrations"]

    issued_at = 1_730_000_000
    registration_id, token = _seed_valid_registration(qs_stack, issued_at)

    failing_sts = _FailingSTS(sts_exc)
    original_sts = qs.sts
    qs.sts = failing_sts

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: issued_at
    try:
        response = qs.handler(
            _credentials_event(token, f"ip-{uuid.uuid4()}"), None
        )
    finally:
        _time.time = _orig_time
        qs.sts = original_sts

    body = json.loads(response["body"])

    # The token validated, so STS was actually reached (the Req 5.7 window).
    assert failing_sts.calls == 1

    # The endpoint returns an issuance error -- NOT a token error: the token
    # is still good, only issuance failed.
    assert response["statusCode"] == 502, (response["statusCode"], body)
    assert body.get("error") == "credential_issuance_failed", body

    # The token is left UNCONSUMED so the exchange can be retried (Req 5.7):
    # consumed_at is still the "never consumed" sentinel and the status is
    # still pending (no transition to in_progress).
    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert int(stored["consumed_at"]) == 0, stored
    assert stored["status"] == "pending", stored
    assert not stored.get("report_secret_hash"), stored


def test_exchange_can_be_retried_after_sts_failure(qs_stack):
    """A failed issuance leaves the token usable: a retry with a working STS
    within the remaining lifetime succeeds and consumes the token exactly once
    (Req 5.7)."""
    qs = qs_stack["qs"]
    registrations = qs_stack["registrations"]

    issued_at = 1_730_000_000
    registration_id, token = _seed_valid_registration(qs_stack, issued_at)

    original_sts = qs.sts

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: issued_at
    try:
        # First attempt: STS fails -> issuance error, token preserved.
        qs.sts = _FailingSTS(_STS_CLIENT_ERROR)
        first = qs.handler(
            _credentials_event(token, f"ip-{uuid.uuid4()}"), None
        )
        assert first["statusCode"] == 502, first

        stored_after_failure = registrations.get_item(
            Key={"registration_id": registration_id}
        )["Item"]
        assert int(stored_after_failure["consumed_at"]) == 0
        assert stored_after_failure["status"] == "pending"

        # Retry: STS works -> the SAME token now yields credentials.
        recorder = _RecordingSTS()
        qs.sts = recorder
        retry = qs.handler(
            _credentials_event(token, f"ip-{uuid.uuid4()}"), None
        )
    finally:
        _time.time = _orig_time
        qs.sts = original_sts

    retry_body = json.loads(retry["body"])
    assert retry["statusCode"] == 200, (retry["statusCode"], retry_body)
    assert len(recorder.calls) == 1
    creds = retry_body["credentials"]
    assert creds["access_key_id"]
    assert creds["secret_access_key"]
    assert creds["session_token"]
    assert retry_body["report_secret"]

    # The retry consumed the token exactly once and transitioned the
    # registration to in_progress.
    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert stored["status"] == "in_progress", stored
    assert int(stored["consumed_at"]) > 0, stored
    assert stored.get("report_secret_hash"), stored
