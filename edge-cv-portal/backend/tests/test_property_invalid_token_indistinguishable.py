"""Property test for indistinguishable invalid-token responses (station-quick-setup task 5.3).

**Feature: station-quick-setup, Property 8: Invalid-token responses are indistinguishable**

For any pair of invalid presentations drawn from {token of a nonexistent
registration, consumed token, token superseded by regeneration, token of a
deleted registration, well-formed token with a wrong secret}, the HTTP status
and response body returned by the Quick_Setup_Endpoint are byte-identical.

**Validates: Requirements 3.5**

This exercises the real ``quick_setup`` request pipeline
(``_token_authenticated_request``) end to end against a moto-backed AWS stack
(the device-registrations table used by ``token_service``/``rate_limiter`` and
the shared audit-log table), so the property pins the actual HTTP response the
endpoint returns rather than a stubbed value. Both token-authenticated routes
(``/quick-setup/bundle`` and ``/quick-setup/credentials``) are exercised, so
the response is shown indistinguishable not only across the five invalid
categories but also across the requested resource.

Each presentation uses a distinct source IP so the per-IP invalid-token rate
limiter (Req 3.9) never trips within an example and never perturbs the
response body being compared.
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

# Table names match conftest's TEST_ENV so the real shared_utils module (which
# reads these at import time) resolves to the same moto-backed tables.
REGISTRATIONS_TABLE = "test-device-registrations"
AUDIT_LOG_TABLE = "test-audit-log"

# The five invalid categories named by Property 8. All must collapse to one
# byte-identical response (Req 3.5 / 3.4).
INVALID_CATEGORIES = [
    "nonexistent",   # token of a registration that never existed
    "deleted",       # token of a registration that was deleted
    "consumed",      # token already consumed by a completed exchange
    "superseded",    # token superseded by Setup_Command regeneration
    "wrong_secret",  # well-formed token for a real registration, wrong secret
]

# The two token-authenticated routes that flow through the shared pipeline.
TOKEN_ROUTES = ["/quick-setup/bundle", "/quick-setup/credentials"]

# A fixed evaluation clock; token expirations are set well beyond it so the
# invalid categories are reached on their own merits (never via expiry).
NOW = 1_700_000_000
FAR_FUTURE = NOW + 3600


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations + audit tables and the
    real quick_setup / token_service / rate_limiter / shared_utils modules
    imported inside the mock so their module-level boto3 resources are
    intercepted by moto."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    # Referenced only by routes past token validation, which invalid tokens
    # never reach; set so module import is fully configured.
    os.environ.setdefault("PORTAL_ARTIFACTS_BUCKET", "test-portal-artifacts")
    os.environ.setdefault("QUICK_SETUP_BOOTSTRAP_KEY", "quick-setup/current/bootstrap.sh")

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


def _put_registration(table, registration_id, token_hash, *, consumed=False):
    """Persist a Device_Registration item with the given token hash."""
    table.put_item(Item={
        "registration_id": registration_id,
        "usecase_id": f"uc-{uuid.uuid4()}",
        "device_name": f"dev-{uuid.uuid4().hex[:12]}",
        "device_group": "grp",
        "status": "in_progress" if consumed else "pending",
        "token_hash": token_hash,
        "token_expires_at": FAR_FUTURE,
        "consumed_at": NOW if consumed else 0,
    })


def _make_presentation(stack, category, secret_seed):
    """Set up table state for an invalid ``category`` and return the token to
    present. ``secret_seed`` varies the generated secrets across examples."""
    ts = stack["ts"]
    table = stack["registrations"]
    registration_id = f"reg-{uuid.uuid4()}"

    if category == "nonexistent":
        # A well-formed token whose registration id was never persisted.
        token, _hash, _exp = ts.generate_token(registration_id, now=NOW)
        return token

    if category == "deleted":
        # Persist then delete: the primary-key read resolves to nothing.
        token, token_hash, _exp = ts.generate_token(registration_id, now=NOW)
        _put_registration(table, registration_id, token_hash)
        table.delete_item(Key={"registration_id": registration_id})
        return token

    if category == "consumed":
        # The correct token, but the registration is marked consumed.
        token, token_hash, _exp = ts.generate_token(registration_id, now=NOW)
        _put_registration(table, registration_id, token_hash, consumed=True)
        return token

    if category == "superseded":
        # Persist a registration whose stored hash belongs to a *newer* token
        # (regeneration), then present the older token -> hash mismatch.
        old_token, _old_hash, _exp = ts.generate_token(registration_id, now=NOW)
        _new_token, new_hash, _exp2 = ts.generate_token(registration_id, now=NOW)
        _put_registration(table, registration_id, new_hash)
        return old_token

    if category == "wrong_secret":
        # A real registration, but the presented secret does not match.
        _token, token_hash, _exp = ts.generate_token(registration_id, now=NOW)
        _put_registration(table, registration_id, token_hash)
        wrong_secret = f"wrong-secret-{secret_seed}-{uuid.uuid4().hex}"
        return f"{ts.TOKEN_PREFIX}.{registration_id}.{wrong_secret}"

    raise AssertionError(f"unknown category {category}")


def _event(route, token, source_ip):
    """A minimal API Gateway proxy event for a token-authenticated POST."""
    return {
        "httpMethod": "POST",
        "path": route,
        "body": json.dumps({"token": token}),
        "requestContext": {"identity": {"sourceIp": source_ip}},
    }


def _normalized_response(response):
    """The comparable, byte-level view of a response: status code, body bytes,
    and the token-relevant error headers. A distinguishable response would
    differ in one of these."""
    return (
        response["statusCode"],
        response["body"],
        response["headers"].get("Content-Type"),
    )


@settings(max_examples=120, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(secret_seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_invalid_token_responses_are_indistinguishable(qs_stack, secret_seed):
    """**Feature: station-quick-setup, Property 8: Invalid-token responses are indistinguishable**

    Every invalid presentation, across all five categories and both
    token-authenticated routes, yields a byte-identical HTTP status and
    response body.

    **Validates: Requirements 3.5**
    """
    qs = qs_stack["qs"]

    responses = []
    ip_counter = 0
    for category in INVALID_CATEGORIES:
        for route in TOKEN_ROUTES:
            token = _make_presentation(qs_stack, category, secret_seed)
            # Distinct source IP per presentation so the invalid-token rate
            # limiter (Req 3.9) never trips and never alters the response.
            source_ip = f"ip-{ip_counter}-{uuid.uuid4()}"
            ip_counter += 1

            response = qs.handler(_event(route, token, source_ip), None)
            responses.append((category, route, _normalized_response(response)))

    # Every presentation must be rejected with the uniform invalid-token error.
    reference_category, reference_route, reference = responses[0]
    status, body, _ct = reference
    assert status == 403, f"expected 403 invalid-token, got {status}: {body}"
    assert json.loads(body)["error"] == "invalid_token"

    # Core property: all responses are byte-identical to the reference (Req 3.5).
    for category, route, normalized in responses:
        assert normalized == reference, (
            "invalid-token response distinguishable: "
            f"{category} via {route} -> {normalized} != "
            f"{reference_category} via {reference_route} -> {reference}"
        )
