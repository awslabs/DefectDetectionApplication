"""Property test for token-to-registration binding and checksum
(station-quick-setup task 5.5).

**Feature: station-quick-setup, Property 10: A valid token binds to exactly its registration, with a correct checksum**

For any population of Device_Registrations and any valid token among them, the
bundle manifest returned for that token carries exactly that registration's
device name, Device_Group, registration id, the Use_Case's AWS region, and the
serving deployment's Quick_Setup_Endpoint URL, and its ``bundle_sha256`` equals
the SHA-256 of the exact bundle object served.

**Validates: Requirements 3.7, 4.1, 4.5**

This drives the *real* ``quick_setup`` request pipeline
(``_token_authenticated_request`` -> ``get_bundle_manifest``) end to end
against a moto-backed AWS stack: the device-registrations table (which also
backs the ``RATELIMIT#`` counters read by ``rate_limiter.check``), the shared
audit-log table (written by the strict audit-before-effect entry), the
use-cases table (resolved for the per-registration AWS region), and the portal
artifacts S3 bucket (which holds the exact bundle object the checksum is taken
over). So the property pins the actual HTTP response the station receives.

Each example seeds a *population* of distinct registrations (each with its own
use case, device name, Device_Group, region, and unconsumed/unexpired token),
then presents exactly one member's token. The manifest must bind to that member
and no other -- the crux of "a valid token binds to *exactly* its registration"
(Req 3.7). Every registration id / use case id / source IP is unique per
example so the shared tables stay isolated and the invalid-token rate limiter
never trips across examples.

The bundle object is a fixed blob uploaded once in the fixture, with
``QUICK_SETUP_BUNDLE_SHA256`` baked from its exact bytes (mirroring the
deploy-time packaging of Req 4.5); the test independently recomputes the
SHA-256 of the object *actually served from S3* and asserts the manifest's
``bundle_sha256`` matches it, so a drift between the served bytes and the
advertised checksum would fail the property.
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

BUNDLE_KEY = "quick-setup/current/setup-bundle.tar.gz"
# The exact bundle bytes served, and the deploy-time checksum computed over
# them (Req 4.5). Baked into the Lambda env before quick_setup is imported.
BUNDLE_CONTENT = b"fake-setup-bundle-tarball-bytes-for-property-10\x00\x01\x02"
BUNDLE_SHA256 = hashlib.sha256(BUNDLE_CONTENT).hexdigest()

# Serving deployment identity -> the Quick_Setup_Endpoint URL the manifest must
# carry (Req 4.1). Derived by quick_setup._quick_setup_url from these fields.
DOMAIN_NAME = "api.example.com"
STAGE = "v1"
EXPECTED_QS_URL = f"https://{DOMAIN_NAME}/{STAGE}/quick-setup"

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128}.
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# A spread of plausible AWS regions so the manifest must reflect the specific
# Use_Case's region rather than a fixed default.
aws_regions = st.sampled_from([
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1",
    "eu-central-1", "ap-southeast-1", "ap-northeast-1",
])

# One member of the seeded population.
registration_specs = st.builds(
    lambda device_name, device_group, region: {
        "device_name": device_name,
        "device_group": device_group,
        "region": region,
    },
    device_name=iot_names,
    device_group=iot_names,
    region=aws_regions,
)

# Issuance clock times, from the epoch to well beyond the year 2200.
issuance_times = st.integers(min_value=0, max_value=7_258_118_400)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, and audit
    tables plus the artifacts bucket holding the fixed bundle object, and the
    real quick_setup / token_service / rate_limiter / shared_utils modules
    imported inside the mock so their module-level boto3 resources are
    intercepted by moto.

    The bundle-artifact env vars are set BEFORE importing quick_setup because
    that module reads them at import time; QUICK_SETUP_BUNDLE_SHA256 is baked
    from the exact bytes uploaded to S3 (Req 4.5).
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
        # The single, exact bundle object the checksum is taken over (Req 4.5).
        s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY, Body=BUNDLE_CONTENT)

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


def _bundle_event(token, source_ip):
    """Minimal API Gateway proxy event for POST /quick-setup/bundle carrying
    the Setup_Token in the body, the serving deployment identity (so the
    manifest's Quick_Setup_Endpoint URL is derived), and the source IP the
    pipeline keys on."""
    return {
        "httpMethod": "POST",
        "path": "/v1/quick-setup/bundle",
        "body": json.dumps({"token": token}),
        "requestContext": {
            "domainName": DOMAIN_NAME,
            "stage": STAGE,
            "identity": {"sourceIp": source_ip},
        },
    }


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    specs=st.lists(registration_specs, min_size=1, max_size=5),
    target_selector=st.integers(min_value=0, max_value=10_000),
    issued_at=issuance_times,
)
def test_valid_token_binds_to_exactly_its_registration_with_correct_checksum(
    qs_stack, specs, target_selector, issued_at
):
    """**Feature: station-quick-setup, Property 10: A valid token binds to exactly its registration, with a correct checksum**

    **Validates: Requirements 3.7, 4.1, 4.5**
    """
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]
    registrations = qs_stack["registrations"]
    usecases = qs_stack["usecases"]
    s3 = qs_stack["s3"]

    # Seed a population of distinct registrations, each with its own use case
    # (carrying its own AWS region) and a real, unconsumed, unexpired token.
    seeded = []
    for spec in specs:
        registration_id = f"reg-{uuid.uuid4()}"
        usecase_id = f"uc-{uuid.uuid4()}"

        usecases.put_item(Item={
            "usecase_id": usecase_id,
            "name": "Test Use Case",
            "account_id": "123456789012",
            "region": spec["region"],
        })

        token, token_hash, expires_at = ts.generate_token(
            registration_id, now=issued_at
        )
        registrations.put_item(Item={
            "registration_id": registration_id,
            "usecase_id": usecase_id,
            "device_name": spec["device_name"],
            "device_group": spec["device_group"],
            "status": "pending",
            "token_hash": token_hash,
            "token_expires_at": expires_at,
            "consumed_at": 0,
            "created_at": issued_at,
            "updated_at": issued_at,
        })
        seeded.append({
            "registration_id": registration_id,
            "usecase_id": usecase_id,
            "device_name": spec["device_name"],
            "device_group": spec["device_group"],
            "region": spec["region"],
            "token": token,
        })

    # Pick exactly one member of the population and present its valid token
    # (presented at issuance time, well within the 90-minute lifetime).
    target = seeded[target_selector % len(seeded)]
    source_ip = f"ip-{uuid.uuid4()}"

    import time as _time
    _orig_time = _time.time
    _time.time = lambda: issued_at
    try:
        response = qs.handler(_bundle_event(target["token"], source_ip), None)
    finally:
        _time.time = _orig_time

    # --- A valid token yields the manifest (Req 4.1).
    assert response["statusCode"] == 200, response
    body = json.loads(response["body"])

    # --- The manifest binds to EXACTLY the presented registration (Req 3.7):
    #     every per-registration parameter matches the target and no other.
    params = body["parameters"]
    assert params["registration_id"] == target["registration_id"]
    assert params["device_name"] == target["device_name"]
    assert params["device_group"] == target["device_group"]
    assert params["aws_region"] == target["region"]
    # --- The serving deployment's Quick_Setup_Endpoint URL (Req 4.1).
    assert params["quick_setup_url"] == EXPECTED_QS_URL

    # --- bundle_sha256 equals the SHA-256 of the EXACT object served (Req 4.5).
    served_bytes = s3.get_object(
        Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY
    )["Body"].read()
    assert body["bundle_sha256"] == hashlib.sha256(served_bytes).hexdigest()
    assert body["bundle_sha256"] == BUNDLE_SHA256

    # --- A presigned download URL for the bundle is provided over HTTPS.
    assert body["bundle_url"].startswith("https://")

    # --- The persisted registration is unchanged: the bundle route validates
    #     but never consumes the token (download is retryable, Req 4.1).
    stored = registrations.get_item(
        Key={"registration_id": target["registration_id"]}
    )["Item"]
    assert int(stored.get("consumed_at", 0)) == 0
    assert stored["status"] == "pending"
