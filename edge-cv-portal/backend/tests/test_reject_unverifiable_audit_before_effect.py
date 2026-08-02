"""Unit tests for reject-on-unverifiable and audit-before-effect
(station-quick-setup task 5.15).

Two security invariants of the Quick_Setup_Endpoint request pipeline
(``quick_setup._token_authenticated_request``) are pinned here:

* **Reject-on-unverifiable (Req 3.10)** — IF any Setup_Token security check
  cannot be *evaluated*, THEN the endpoint rejects the request rather than
  proceeding with an unverified token. Two unevaluable-check windows exist in
  the pipeline and both must reject (and serve nothing):
    - the invalid-token rate-limit admission check cannot read its state, and
    - ``TokenService.validate_token`` returns ``CHECK_FAILED`` (a storage
      error while resolving the registration behind the token).

* **Audit-before-effect (Req 8.4)** — IF the strict "pending" audit entry that
  precedes any redemption effect cannot be recorded, THEN the endpoint rejects
  the request and serves neither the Setup_Bundle nor Provisioning_Credentials.

These are example-based unit tests (not property tests). They drive the *real*
``quick_setup`` pipeline against a moto-backed AWS stack (the
device-registrations table — which also backs the ``RATELIMIT#`` counters —,
the shared audit-log table, the use-cases table, and the artifacts bucket),
inducing each failure through the real collaborators:

    * rate-limit-unavailable  -> ``rate_limiter.load_state`` raises, so the
      real ``rate_limiter.check`` raises and the pipeline's step-1 guard fires;
    * token CHECK_FAILED      -> ``token_service._load_registration`` raises,
      so the real ``validate_token`` genuinely returns ``CHECK_FAILED``;
    * audit-write failure     -> ``record_audit_event_strict`` raises, exactly
      as it would when the audit table is unwritable.

Each seeded token is otherwise real, unconsumed, and unexpired, so any
*successful* pipeline run would reach the bundle/credentials action and serve
content — isolating the behavior under test to the injected failure. Every
test also asserts the registration is left untouched (status still ``pending``,
``consumed_at`` still ``0``, no ``report_secret_hash``): nothing was served and
no effect leaked.

**Validates: Requirements 3.10, 8.4**
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REGION = "us-east-1"

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

BUNDLE_KEY = "quick-setup/current/setup-bundle.tar.gz"
BUNDLE_SHA256 = "a" * 64


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket, and the real quick_setup / token_service
    / rate_limiter / session_policy / shared_utils modules imported inside the
    mock so their module-level boto3 resources are intercepted by moto."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["PORTAL_ARTIFACTS_BUCKET"] = ARTIFACTS_BUCKET
    os.environ["QUICK_SETUP_BUNDLE_KEY"] = BUNDLE_KEY
    os.environ["QUICK_SETUP_BUNDLE_SHA256"] = BUNDLE_SHA256
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
        # Place the bundle object so the missing-artifact 503 path can never be
        # what a *successful* run would hit — a clean run would serve content.
        s3.put_object(
            Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY, Body=b"fake-bundle-bytes"
        )

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
            "rl": rate_limiter,
            "s3": s3,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


class _RecordingSTS:
    """STS stub returning well-formed short-lived credentials, so a *clean*
    credentials run would succeed — proving a rejection is caused only by the
    injected failure, not by an unrelated STS problem."""

    def assume_role(self, **kwargs):
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


def _seed_valid_registration(qs_stack, now):
    """Store a use case and a fresh ``pending`` registration with a real,
    unconsumed, unexpired token. Returns ``(token, registration_id)``."""
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

    token, token_hash, expires_at = ts.generate_token(registration_id, now=now)
    qs_stack["registrations"].put_item(Item={
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": "station-42",
        "device_group": "Line3_Group",
        "status": "pending",
        "token_hash": token_hash,
        "token_expires_at": expires_at,
        "consumed_at": 0,
        "created_at": now,
        "updated_at": now,
    })
    return token, registration_id


def _event(resource, token, source_ip):
    """Minimal API Gateway proxy event for a token-authenticated route."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/quick-setup/{resource}",
        "body": json.dumps({"token": token}),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": source_ip},
        },
    }


def _invoke_at(qs_stack, resource, token, now, source_ip):
    """Invoke the pipeline for ``resource`` at a pinned clock, returning
    ``(status_code, body_dict)``. Uses a working STS so a clean credentials
    run would otherwise succeed."""
    qs = qs_stack["qs"]
    original_sts = qs.sts
    qs.sts = _RecordingSTS()

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: now
    try:
        response = qs.handler(_event(resource, token, source_ip), None)
    finally:
        _time.time = _orig_time
        qs.sts = original_sts
    return response["statusCode"], json.loads(response["body"])


def _assert_serves_nothing(body):
    """No bundle manifest and no credential material leaked in the response."""
    assert "bundle_url" not in body
    assert "bundle_sha256" not in body
    assert "parameters" not in body
    assert "credentials" not in body
    assert "report_secret" not in body


def _assert_registration_untouched(registrations, registration_id):
    """The registration is exactly as seeded: no state effect leaked."""
    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert stored["status"] == "pending", stored
    assert int(stored["consumed_at"]) == 0, stored
    assert not stored.get("report_secret_hash"), stored


# --------------------------------------------------------------------------
# Reject-on-unverifiable (Req 3.10): rate-limit state cannot be read.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("resource", ["bundle", "credentials"])
def test_rate_limit_state_unavailable_rejects_and_serves_nothing(qs_stack, resource):
    """**Validates: Requirements 3.10**

    When the invalid-token rate-limit admission check cannot read its state,
    the pipeline rejects (503) rather than proceeding with an unverified token,
    and serves neither the bundle nor credentials.
    """
    qs = qs_stack["qs"]
    rl = qs_stack["rl"]
    registrations = qs_stack["registrations"]
    now = 1_730_000_000

    token, registration_id = _seed_valid_registration(qs_stack, now)

    # Make the limiter's state load fail so the real rate_limiter.check raises,
    # exactly as it would if the counters table read failed.
    original_load_state = rl.load_state

    def _boom(_source_ip):
        raise RuntimeError("rate-limit state store is unavailable")

    rl.load_state = _boom
    try:
        status_code, body = _invoke_at(
            qs_stack, resource, token, now, f"ip-{uuid.uuid4()}"
        )
    finally:
        rl.load_state = original_load_state

    assert status_code == 503, (status_code, body)
    assert body.get("error") == "rate_limit_unavailable", body
    _assert_serves_nothing(body)
    _assert_registration_untouched(registrations, registration_id)


# --------------------------------------------------------------------------
# Reject-on-unverifiable (Req 3.10): token security check cannot be evaluated.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("resource", ["bundle", "credentials"])
def test_token_check_failed_rejects_and_serves_nothing(qs_stack, resource):
    """**Validates: Requirements 3.10**

    When ``TokenService.validate_token`` cannot evaluate the token (a storage
    error resolving the registration -> ``CHECK_FAILED``), the pipeline rejects
    (503 ``token_check_unavailable``) rather than proceeding with an unverified
    token, and serves neither the bundle nor credentials.
    """
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]
    registrations = qs_stack["registrations"]
    now = 1_730_000_100

    token, registration_id = _seed_valid_registration(qs_stack, now)

    # Make the registration loader raise so the real validate_token genuinely
    # returns CHECK_FAILED (the rate-limit admission check still succeeds: it
    # reads a non-existent RATELIMIT# item and allows the request).
    original_loader = ts._load_registration

    def _boom(_registration_id):
        raise RuntimeError("registration store is unavailable")

    ts._load_registration = _boom
    try:
        status_code, body = _invoke_at(
            qs_stack, resource, token, now, f"ip-{uuid.uuid4()}"
        )
    finally:
        ts._load_registration = original_loader

    assert status_code == 503, (status_code, body)
    assert body.get("error") == "token_check_unavailable", body
    _assert_serves_nothing(body)
    _assert_registration_untouched(registrations, registration_id)


# --------------------------------------------------------------------------
# Audit-before-effect (Req 8.4): the strict pending audit entry fails.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("resource", ["bundle", "credentials"])
def test_audit_write_failure_rejects_and_serves_nothing(qs_stack, resource):
    """**Validates: Requirements 8.4**

    When the strict "pending" audit entry that precedes any redemption effect
    cannot be recorded, the pipeline rejects (503 ``audit_unavailable``) and
    serves neither the bundle nor credentials, leaving the registration
    untouched.
    """
    qs = qs_stack["qs"]
    registrations = qs_stack["registrations"]
    now = 1_730_000_200

    token, registration_id = _seed_valid_registration(qs_stack, now)

    # record_audit_event_strict is imported into the quick_setup namespace;
    # make it raise exactly as it would when the audit table is unwritable.
    original_strict = qs.record_audit_event_strict

    def _boom(**_kwargs):
        raise RuntimeError("audit log is unwritable")

    qs.record_audit_event_strict = _boom
    try:
        status_code, body = _invoke_at(
            qs_stack, resource, token, now, f"ip-{uuid.uuid4()}"
        )
    finally:
        qs.record_audit_event_strict = original_strict

    assert status_code == 503, (status_code, body)
    assert body.get("error") == "audit_unavailable", body
    _assert_serves_nothing(body)
    _assert_registration_untouched(registrations, registration_id)


# --------------------------------------------------------------------------
# Positive contrast: with every collaborator healthy, the same seeded token
# DOES serve content — proving the rejections above are caused specifically by
# the injected failures and not by unrelated misconfiguration.
# --------------------------------------------------------------------------
def test_healthy_pipeline_serves_bundle(qs_stack):
    """A clean run of the bundle route serves a complete manifest, confirming
    the 503s above are specific to the induced unverifiable/audit failures."""
    registrations = qs_stack["registrations"]
    now = 1_730_000_300

    token, registration_id = _seed_valid_registration(qs_stack, now)
    status_code, body = _invoke_at(
        qs_stack, "bundle", token, now, f"ip-{uuid.uuid4()}"
    )

    assert status_code == 200, (status_code, body)
    assert body["bundle_sha256"] == BUNDLE_SHA256
    assert "bundle_url" in body
    assert body["parameters"]["registration_id"] == registration_id


def test_healthy_pipeline_serves_credentials(qs_stack):
    """A clean run of the credentials route issues credentials and consumes the
    token, confirming the 503s above are specific to the induced failures."""
    registrations = qs_stack["registrations"]
    now = 1_730_000_400

    token, registration_id = _seed_valid_registration(qs_stack, now)
    status_code, body = _invoke_at(
        qs_stack, "credentials", token, now, f"ip-{uuid.uuid4()}"
    )

    assert status_code == 200, (status_code, body)
    assert body["credentials"]["access_key_id"]
    assert body["report_secret"]

    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert stored["status"] == "in_progress", stored
    assert int(stored["consumed_at"]) > 0, stored
