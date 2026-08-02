"""Unit test for missing bundle artifacts (station-quick-setup task 5.6).

A valid Setup_Token whose bundle object is absent from the artifacts bucket
must be rejected by ``get_bundle_manifest`` with a 503 and no partial content
(no presigned URL, no per-registration parameters) — the endpoint never serves
artifacts from an incomplete/missing deployment.

**Validates: Requirements 4.10**

These are example-based unit tests (not property tests). They drive the *real*
``quick_setup`` request pipeline (``_token_authenticated_request`` →
``get_bundle_manifest``) against a moto-backed AWS stack: the
device-registrations table (which also backs the ``RATELIMIT#`` counters read
by ``rate_limiter.check``), the shared audit-log table (written by the strict
audit-before-effect entry), the use-cases table, and the portal artifacts S3
bucket. The token is a real, unconsumed, unexpired token so
``TokenService.validate_token`` classifies it ``VALID`` and the pipeline reaches
the bundle action — isolating the behavior under test to "bundle object
missing".

A positive-contrast test (bundle object present) confirms the 503 is caused
specifically by the missing artifact and not by unrelated misconfiguration.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

REGION = "us-east-1"

# Table / bucket names; kept aligned with the other quick-setup tests so the
# real shared_utils module (which reads table names at import time) resolves to
# the same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

BUNDLE_KEY = "quick-setup/current/setup-bundle.tar.gz"
BUNDLE_SHA256 = "a" * 64  # deploy-time checksum baked into the Lambda env


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket, and the real quick_setup /
    token_service / rate_limiter / shared_utils modules imported inside the
    mock so their module-level boto3 resources are intercepted by moto.

    The bundle-artifact env vars are set BEFORE importing quick_setup because
    that module reads them at import time; setting them ensures the missing
    object is rejected on the 503 (bundle_unavailable) path rather than the
    500 (bundle_not_configured) path.
    """
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
            "s3": s3,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
        }


def _seed_valid_registration(qs_stack, now):
    """Store a use case and a fresh registration with a real, unconsumed,
    unexpired token; return (token, registration_id)."""
    ts = qs_stack["ts"]
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"

    qs_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": "123456789012",
        "region": REGION,
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


def _bundle_event(token, source_ip):
    """Minimal API Gateway proxy event for POST /quick-setup/bundle."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/bundle",
        "body": json.dumps({"token": token}),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": source_ip},
        },
    }


def test_valid_token_with_missing_bundle_returns_503_and_no_partial_content(qs_stack):
    """**Validates: Requirements 4.10**

    A valid token whose bundle object is absent from S3 is rejected with a 503,
    and the response carries no partial bundle content (no presigned URL, no
    per-registration parameters, no checksum).
    """
    qs = qs_stack["qs"]
    now = 1_730_000_000

    # Ensure the bundle object is NOT present in the artifacts bucket.
    try:
        qs_stack["s3"].delete_object(Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY)
    except Exception:
        pass

    token, _ = _seed_valid_registration(qs_stack, now)

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: now
    try:
        response = qs.handler(_bundle_event(token, f"ip-{uuid.uuid4()}"), None)
    finally:
        _time.time = _orig_time

    # Rejected with a 503 (service unavailable) — the bundle artifact is missing.
    assert response["statusCode"] == 503, response
    body = json.loads(response["body"])
    assert body.get("error") == "bundle_unavailable", body

    # No partial content is served: none of the manifest fields leak.
    assert "bundle_url" not in body
    assert "bundle_sha256" not in body
    assert "parameters" not in body


def test_bundle_present_does_not_return_503(qs_stack):
    """Positive contrast: with the bundle object present, the same valid token
    is NOT rejected with the missing-artifact 503 and a complete manifest is
    served — proving the 503 above is specific to the absent artifact.
    """
    qs = qs_stack["qs"]
    now = 1_730_000_500

    # Place the bundle object so head_object succeeds.
    qs_stack["s3"].put_object(
        Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY, Body=b"fake-bundle-bytes"
    )
    token, registration_id = _seed_valid_registration(qs_stack, now)

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: now
    try:
        response = qs.handler(_bundle_event(token, f"ip-{uuid.uuid4()}"), None)
    finally:
        _time.time = _orig_time

    assert response["statusCode"] == 200, response
    body = json.loads(response["body"])
    assert body["bundle_sha256"] == BUNDLE_SHA256
    assert "bundle_url" in body
    assert body["parameters"]["registration_id"] == registration_id
