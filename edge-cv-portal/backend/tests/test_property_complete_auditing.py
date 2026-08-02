"""Property test for complete quick-setup auditing (station-quick-setup
task 5.13).

**Feature: station-quick-setup, Property 18: Every quick-setup operation is audited completely**

For any token redemption at the Quick_Setup_Endpoint (for the Setup_Bundle or
Provisioning_Credentials) and any token validation failure -- whatever the
outcome -- exactly one audit event is recorded, and that event is *complete*:
it carries the action type, the acting principal, the requested resource, the
source IP of the redeeming request, the outcome, and a timestamp; a validated
redemption additionally carries the device name and Use_Case, and a validation
failure additionally carries the failure reason.

**Validates: Requirements 3.8, 8.1, 8.2**

This drives the *real* ``quick_setup`` request pipeline
(``_token_authenticated_request`` -> ``get_bundle_manifest`` /
``exchange_credentials``) end to end against a moto-backed AWS stack: the
device-registrations table (which also backs the ``RATELIMIT#`` counters read
by ``rate_limiter.check`` and holds the atomic consume of the credential
exchange), the shared audit-log table (written by the strict
audit-before-effect ``pending`` entry and its terminal finalize), the
use-cases table (resolved for the region, cross-account role, external id, and
account id), the portal artifacts S3 bucket (the bundle object the manifest
points at), and STS (moto issues the scoped session credentials). So the
property pins the *actually persisted* audit item rather than a stubbed value.

The two-phase audit-before-effect protocol writes a single audit item per
request: a ``pending`` entry recorded *before* any redemption effect, then the
same entry finalized to ``success``/``failure`` with the terminal details
merged in. The test therefore asserts that after each request there is exactly
one finalized audit item for the (unique-per-example) source IP, and that its
fields cover every element Property 18 requires for that outcome.

Scenario coverage (the ``scenario`` strategy) exercises every redemption
outcome the pipeline audits:

  * ``valid``        -- a real, unconsumed, unexpired token -> the redemption
                        succeeds; the audit outcome is ``success`` and the
                        record carries device name + Use_Case (Req 8.2).
  * ``expired``      -- a matching-secret token presented past its expiry ->
                        ``TokenService`` classifies it ``EXPIRED``; the audit
                        outcome is ``failure`` with reason ``token_expired``,
                        and (because the bound registration is known) device
                        name + Use_Case (Req 3.8 + 8.2).
  * ``unknown``      -- a token whose embedded registration id is absent.
  * ``wrong_secret`` -- a registration whose stored hash is a *different*
                        secret than the presented token.
  * ``consumed``     -- a matching-secret token whose registration is already
                        consumed.
                        The last three all collapse to the uniform
                        ``INVALID`` classification -> audit outcome ``failure``
                        with reason ``invalid_token`` (Req 3.8).

Both redeemable resources (``bundle`` and ``credentials``) are exercised via
the ``resource`` strategy so the recorded ``action`` /``resource`` fields are
pinned for each. Every registration id / use case id / source IP is unique per
example so the shared tables stay isolated and the invalid-token rate limiter
(a single request per example) never trips.
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
ACCOUNT_ID = "123456789012"

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

BUNDLE_KEY = "quick-setup/current/setup-bundle.tar.gz"
# The exact bundle bytes served and the deploy-time checksum over them; baked
# into the Lambda env before quick_setup is imported (mirrors Req 4.5). The
# credential path ignores these, but the bundle path needs a real object.
BUNDLE_CONTENT = b"fake-setup-bundle-tarball-bytes-for-property-18\x00\x01\x02"
BUNDLE_SHA256 = hashlib.sha256(BUNDLE_CONTENT).hexdigest()

# Serving deployment identity for the request context (the bundle manifest
# derives its Quick_Setup_Endpoint URL from these).
DOMAIN_NAME = "api.example.com"
STAGE = "v1"

# The two redeemable resources the pipeline audits (Req 8.2).
REDEEMABLE_RESOURCES = ("bundle", "credentials")

# The redemption/validation scenarios the pipeline audits, and the audit
# outcome + failure reason each must produce.
SCENARIOS = ("valid", "expired", "unknown", "wrong_secret", "consumed")
FAILURE_SCENARIOS = ("expired", "unknown", "wrong_secret", "consumed")
# Scenarios where the bound registration is known to the pipeline, so the
# audit record must carry device name + Use_Case (Req 8.2).
REGISTRATION_KNOWN_SCENARIOS = ("valid", "expired")

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128}.
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# A spread of plausible AWS regions so the audited Use_Case is not a fixed
# default.
aws_regions = st.sampled_from([
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1",
    "eu-central-1", "ap-southeast-1", "ap-northeast-1",
])

# Issuance clock times: realistic wall-clock epochs. The lower bound is kept
# well above the 900s STS floor from expiry so the valid-credential exchange
# never refuses on remaining lifetime.
issuance_times = st.integers(min_value=1_000_000_000, max_value=7_258_118_400)
# How far past expiry the expired-scenario token is presented (>= 1 second).
past_expiry_offsets = st.integers(min_value=1, max_value=10_000_000)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables, the artifacts bucket holding the bundle object, and the real
    quick_setup / token_service / rate_limiter / session_policy / shared_utils
    modules imported inside the mock so their module-level boto3 resources
    (DynamoDB, S3, STS) are intercepted by moto.

    Bundle-artifact env vars are set BEFORE importing quick_setup because that
    module reads them at import time.
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
        s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY, Body=BUNDLE_CONTENT)

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
            "audit": resource.Table(AUDIT_LOG_TABLE),
        }


def _redemption_event(resource, token, source_ip):
    """Minimal API Gateway proxy event for a POST /quick-setup/{resource}
    request carrying the Setup_Token in the body, the serving deployment
    identity, and the source IP the pipeline audits + keys the limiter on."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/quick-setup/{resource}",
        "body": json.dumps({"token": token}),
        "requestContext": {
            "domainName": DOMAIN_NAME,
            "stage": STAGE,
            "identity": {"sourceIp": source_ip},
        },
    }


def _audit_events_for(audit_table, source_ip):
    """All audit items recorded against the given source IP (the audit
    ``user_id`` for quick-setup redemptions). Unique source IP per example
    keeps this a clean read of just this request's audit trail."""
    items = audit_table.scan().get("Items", [])
    return [item for item in items if item.get("user_id") == source_ip]


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    resource=st.sampled_from(REDEEMABLE_RESOURCES),
    scenario=st.sampled_from(SCENARIOS),
    device_name=iot_names,
    device_group=iot_names,
    region=aws_regions,
    issued_at=issuance_times,
    past_offset=past_expiry_offsets,
)
def test_every_quick_setup_operation_is_audited_completely(
    qs_stack, resource, scenario, device_name, device_group, region,
    issued_at, past_offset,
):
    """**Feature: station-quick-setup, Property 18: Every quick-setup operation is audited completely**

    **Validates: Requirements 3.8, 8.1, 8.2**
    """
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]
    registrations = qs_stack["registrations"]
    usecases = qs_stack["usecases"]
    audit = qs_stack["audit"]

    # Unique keys per example -> isolation in the shared tables and no
    # invalid-token rate-limit accumulation across examples.
    registration_id = f"reg-{uuid.uuid4()}"
    usecase_id = f"uc-{uuid.uuid4()}"
    source_ip = f"ip-{uuid.uuid4()}"

    # A fully-configured Use_Case so BOTH the bundle path (needs region) and
    # the credential path (needs region + cross-account role + external id +
    # account id) can complete for the `valid` scenario.
    usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": ACCOUNT_ID,
        "cross_account_role_arn":
            f"arn:aws:iam::{ACCOUNT_ID}:role/DDAPortalAccessRole",
        "external_id": f"ext-{uuid.uuid4()}",
        "region": region,
    })

    # Mint a real token for this registration; store only the secret hash.
    token, token_hash, expires_at = ts.generate_token(
        registration_id, now=issued_at
    )

    # Shape the stored registration + the presented token + the presentation
    # time per scenario, then compute the expected audit outcome.
    presented_token = token
    presented_at = issued_at  # within the token lifetime by default

    if scenario == "unknown":
        # No registration item at all: the embedded id resolves to nothing.
        pass
    else:
        stored_hash = token_hash
        consumed_at = 0
        if scenario == "wrong_secret":
            # Store a DIFFERENT secret's hash so the presented token's hash
            # cannot match (superseded/wrong-secret case -> INVALID).
            other_token, other_hash, _ = ts.generate_token(
                registration_id, now=issued_at
            )
            stored_hash = other_hash
        elif scenario == "consumed":
            # Already-consumed token -> INVALID (takes priority over expiry).
            consumed_at = issued_at
        registrations.put_item(Item={
            "registration_id": registration_id,
            "usecase_id": usecase_id,
            "device_name": device_name,
            "device_group": device_group,
            "status": "pending",
            "token_hash": stored_hash,
            "token_expires_at": expires_at,
            "consumed_at": consumed_at,
            "created_at": issued_at,
            "updated_at": issued_at,
        })
        if scenario == "expired":
            # Present the (matching-secret, unconsumed) token past expiry.
            presented_at = expires_at + past_offset

    # Invoke the real pipeline at the chosen presentation time.
    import time as _time
    _orig_time = _time.time
    _time.time = lambda: presented_at
    try:
        response = qs.handler(
            _redemption_event(resource, presented_token, source_ip), None
        )
    finally:
        _time.time = _orig_time

    status_code = response["statusCode"]
    body = json.loads(response["body"])

    # --- Exactly one audit event was recorded for this request (Req 8.1/8.2:
    #     an audit event is recorded for the redemption regardless of outcome;
    #     the two-phase protocol collapses to one finalized item).
    events = _audit_events_for(audit, source_ip)
    assert len(events) == 1, (scenario, resource, events)
    event = events[0]
    details = event.get("details") or {}

    # --- The record is COMPLETE for every outcome (Req 8.1/8.2/3.8):
    #       * action type identifying the redeemed resource,
    #       * acting principal (the source IP is the recorded user_id),
    #       * the requested resource,
    #       * the source IP of the redeeming request,
    #       * the outcome, and
    #       * a timestamp.
    assert event["action"] == f"redeem_{resource}"
    assert event["resource_type"] == "quick_setup"
    assert event["user_id"] == source_ip
    assert details.get("resource") == resource
    assert details.get("source_ip") == source_ip
    assert int(event["timestamp"]) > 0
    # The entry was finalized past its 'pending' phase to a terminal outcome.
    assert event["result"] in ("success", "failure")
    assert "completed_at" in event
    assert details.get("outcome") in ("success", "failure")
    assert event["result"] == details.get("outcome")

    if scenario == "valid":
        # A validated redemption succeeds and the audit outcome is success
        # (Req 8.2). The record carries device name + Use_Case + the bound
        # registration id (the acting principal for a station-reported action).
        assert status_code == 200, (resource, body)
        assert event["result"] == "success"
        assert details.get("device_name") == device_name
        assert details.get("usecase_id") == usecase_id
        assert details.get("registration_id") == registration_id
    else:
        # A validation failure is audited with the failure reason and the
        # source IP (Req 3.8), and the outcome is failure.
        assert scenario in FAILURE_SCENARIOS
        assert status_code == 403, (resource, scenario, body)
        assert event["result"] == "failure"
        reason = details.get("reason")
        assert reason, (scenario, details)
        if scenario == "expired":
            # Distinct expiration reason, and the bound registration is known
            # so device name + Use_Case are still audited (Req 8.2 + 3.8).
            assert reason == "token_expired"
            assert body["error"] == "token_expired"
            assert details.get("device_name") == device_name
            assert details.get("usecase_id") == usecase_id
        else:
            # unknown / wrong_secret / consumed collapse to the uniform
            # invalid-token failure.
            assert reason == "invalid_token"
            assert body["error"] == "invalid_token"

    # Secret material is never written to the audit record (Req 8.3 cross-check).
    serialized = json.dumps(details, default=str)
    assert presented_token not in serialized
    assert "report_secret" not in details
