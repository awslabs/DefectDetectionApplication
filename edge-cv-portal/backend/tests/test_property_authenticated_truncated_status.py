"""Property test for authenticated, truncated status reports
(station-quick-setup task 5.12).

**Feature: station-quick-setup, Property 16: Status reports are authenticated and applied with truncation**

*For any* Device_Registration, a status report changes the Setup_Status if and
only if its report secret hashes to the stored report-secret hash and the
current status is not ``completed``; an accepted ``completed`` report yields
status ``completed``, an accepted ``failed`` report yields status ``failed``
with the stored error summary equal to the first 1024 characters of the
reported summary; every rejected report leaves the registration unchanged.

**Validates: Requirements 6.1, 6.2, 6.7**

This drives the *real* ``quick_setup.report_status`` route end to end through
the module ``handler`` (``POST /quick-setup/status``) against a moto-backed
DynamoDB device-registrations table. Only registrations that have already
exchanged credentials carry a ``report_secret_hash`` and therefore sit in one
of ``in_progress`` / ``failed`` / ``completed`` -- exactly the states a real
report can target -- so the generator seeds from those states and presents
either the genuine report secret or a wrong one.

The property is an *iff*: the transition happens precisely when the presented
secret authenticates AND the current status is reportable (not ``completed``).
Every other combination -- wrong secret, unknown registration, or a
``completed`` target -- must return the single uniform error and leave the
stored item byte-for-byte unchanged (Req 6.7). Truncation is checked on the
``failed`` branch by generating error summaries that straddle the 1024-char
bound (Req 6.2).
"""
from __future__ import annotations

import hashlib
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

# The uniform rejection code every unauthenticated / unreportable report gets
# (Req 6.7): bad secret, unknown id, and completed-target are indistinguishable.
INVALID_TOKEN_ERROR_CODE = "invalid_token"

# Mirror of the route's stored-summary bound (Req 6.2).
ERROR_SUMMARY_MAX_CHARS = 1024

# The two states from which a report is accepted, and the terminal state that
# rejects every report.
REPORTABLE_FROM = ("in_progress", "failed")
NON_REPORTABLE_WITH_SECRET = ("completed",)

# Report secrets: non-empty printable text (the real secret is token_urlsafe,
# but the route only ever hashes the raw bytes, so any non-empty string
# exercises the same compare path).
report_secrets = st.text(min_size=1, max_size=64)

# Error summaries that straddle the 1024-char truncation bound: empty, short,
# exactly at the bound, and comfortably over it.
error_summaries = st.text(min_size=0, max_size=2100)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket, and the real quick_setup / token_service
    / rate_limiter / shared_utils modules imported inside the mock so their
    module-level boto3 resources are intercepted by moto.

    Bundle-artifact env vars are set before importing quick_setup because that
    module reads them at import time; they are unused by the status-report path
    but keep the module import self-consistent.
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
            "registrations": resource.Table(REGISTRATIONS_TABLE),
        }


def _status_event(body):
    """Minimal API Gateway proxy event for POST /quick-setup/status."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/status",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": "203.0.113.7"},
        },
    }


def _seed_registration(registrations, *, status, secret):
    """Put a Device_Registration that has already exchanged credentials (so it
    carries a report_secret_hash) in the given ``status``, returning the stored
    item."""
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    item = {
        "registration_id": registration_id,
        "usecase_id": usecase_id,
        "device_name": f"device-{uuid.uuid4().hex[:8]}",
        "device_group": "DDA_transition_EC2_Group",
        "status": status,
        "token_hash": hashlib.sha256(b"consumed-token").hexdigest(),
        "token_expires_at": 4_000_000_000,
        "consumed_at": 1_700_000_000,
        "report_secret_hash": secret_hash,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    # A failed registration may already carry a prior error summary.
    if status == "failed":
        item["error_summary"] = "prior failure summary"
    registrations.put_item(Item=item)
    return item


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    current_status=st.sampled_from(
        REPORTABLE_FROM + NON_REPORTABLE_WITH_SECRET
    ),
    report_status=st.sampled_from(("completed", "failed")),
    secret=report_secrets,
    use_correct_secret=st.booleans(),
    error_summary=error_summaries,
)
def test_status_reports_are_authenticated_and_truncated(
    qs_stack, current_status, report_status, secret,
    use_correct_secret, error_summary,
):
    """**Feature: station-quick-setup, Property 16: Status reports are authenticated and applied with truncation**

    **Validates: Requirements 6.1, 6.2, 6.7**
    """
    qs = qs_stack["qs"]
    registrations = qs_stack["registrations"]

    seeded = _seed_registration(
        registrations, status=current_status, secret=secret
    )
    registration_id = seeded["registration_id"]

    # Canonical stored form (DynamoDB normalizes numbers to Decimal), used as
    # the "unchanged" baseline for the rejection branch.
    baseline = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]

    # Present either the genuine secret or a guaranteed-different one.
    presented_secret = secret if use_correct_secret else (secret + "x")
    body = {
        "registration_id": registration_id,
        "report_secret": presented_secret,
        "status": report_status,
    }
    if report_status == "failed":
        body["error_summary"] = error_summary

    response = qs.handler(_status_event(body), None)
    code = response["statusCode"]
    parsed = json.loads(response["body"])

    stored = registrations.get_item(
        Key={"registration_id": registration_id}
    )["Item"]

    # The report is applied iff the secret authenticates AND the current status
    # is reportable (not completed) -- the property's iff.
    secret_authenticates = use_correct_secret
    should_apply = secret_authenticates and current_status in REPORTABLE_FROM

    if should_apply:
        # --- Accepted: 200 and the status transitions to the reported value
        #     (Req 6.1 completed / Req 6.2 failed).
        assert code == 200, (code, parsed)
        assert parsed["status"] == report_status
        assert stored["status"] == report_status

        if report_status == "failed":
            # Stored error summary is exactly the first 1024 chars of the
            # reported summary (Req 6.2), truncating iff it exceeded the bound.
            assert stored["error_summary"] == error_summary[:ERROR_SUMMARY_MAX_CHARS]
            assert len(stored["error_summary"]) <= ERROR_SUMMARY_MAX_CHARS
        else:
            # A completed report writes no new error summary; any prior summary
            # is left untouched.
            assert stored.get("error_summary") == baseline.get("error_summary")
    else:
        # --- Rejected: uniform error, registration byte-for-byte unchanged
        #     (Req 6.7). Covers wrong secret and completed-target alike.
        assert code == 403, (code, parsed)
        assert parsed["error"] == INVALID_TOKEN_ERROR_CODE
        assert stored == baseline


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    report_status=st.sampled_from(("completed", "failed")),
    report_secret=report_secrets,
    error_summary=error_summaries,
)
def test_unknown_registration_is_rejected_uniformly(
    qs_stack, report_status, report_secret, error_summary
):
    """A report against a registration id that does not exist is rejected with
    the same uniform error and persists nothing (Req 6.7).

    **Feature: station-quick-setup, Property 16: Status reports are authenticated and applied with truncation**

    **Validates: Requirements 6.1, 6.2, 6.7**
    """
    qs = qs_stack["qs"]
    registrations = qs_stack["registrations"]

    unknown_id = f"reg-{uuid.uuid4()}"
    body = {
        "registration_id": unknown_id,
        "report_secret": report_secret,
        "status": report_status,
    }
    if report_status == "failed":
        body["error_summary"] = error_summary

    response = qs.handler(_status_event(body), None)
    parsed = json.loads(response["body"])

    assert response["statusCode"] == 403, parsed
    assert parsed["error"] == INVALID_TOKEN_ERROR_CODE
    # No item was created for the unknown id.
    assert "Item" not in registrations.get_item(
        Key={"registration_id": unknown_id}
    )
