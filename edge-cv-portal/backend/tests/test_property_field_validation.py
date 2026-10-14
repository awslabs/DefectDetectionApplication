"""Property test for device-registration field validation (station-quick-setup
task 3.3).

**Feature: station-quick-setup, Property 2: Invalid or missing fields are
rejected, identified per field, with no persistence**

For any registration request in which any non-empty subset of
{device name, Device_Group, Use_Case} is missing/empty, or the device name or
a new Device_Group name violates ``[a-zA-Z0-9:_-]{1,128}``, the request is
rejected with an error identifying exactly the offending fields and no
Device_Registration item is written.

**Validates: Requirements 1.2, 1.9**

`create_registration` performs validation before any RBAC check, cross-account
lookup, token generation, or persistence: it first collects every
missing/empty required field (device_name, device_group, usecase_id) and
rejects with those, then collects every pattern-invalid IoT-name field
(device_name, device_group) and rejects with those. This test drives the real
handler across arbitrary combinations of valid / empty / pattern-invalid field
values and asserts the response names exactly the offending fields and that the
registrations table stays empty.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
REGISTRATIONS_TABLE = "test-device-registrations"

# The valid IoT Thing / Thing Group alphabet (Req 1.2). Strings drawn from it
# contain no whitespace, so they survive .strip() unchanged and match the
# pattern.
IOT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-"

# A reference copy of the Requirement 1.2 pattern, stated independently of the
# handler's own constant so the test pins the contract rather than echoing it.
REF_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]{1,128}$")

# Non-whitespace characters outside the IoT alphabet. Embedding one guarantees
# a value that is non-empty after .strip() yet fails the pattern.
INVALID_CHARS = "!@#$%^&*()+=/\\.,;?<>|~ "  # trailing space intentional (middle-only)

_REQUIRED_FIELDS = ("device_name", "device_group", "usecase_id")
_PATTERN_FIELDS = ("device_name", "device_group")


@pytest.fixture(scope="module")
def registrations():
    """A moto-backed registrations table plus the real device_registrations
    handler imported under the mock, so module-level boto3 resources bind to
    moto."""
    from moto import mock_aws

    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("QUICK_SETUP_BOOTSTRAP_SHA256", "0" * 64)

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

        # Import the handler fresh under the active mock so its module-level
        # dynamodb resource is intercepted by moto.
        sys.modules.pop("device_registrations", None)
        import device_registrations  # noqa: E402

        table = boto3.resource("dynamodb", region_name=REGION).Table(
            REGISTRATIONS_TABLE)
        yield device_registrations, table

        sys.modules.pop("device_registrations", None)


# --------------------------------------------------------------------------- #
# Strategies                                                                   #
# --------------------------------------------------------------------------- #

# Valid IoT names / non-empty use-case ids: survive .strip() and match pattern.
valid_names = st.text(alphabet=IOT_ALPHABET, min_size=1, max_size=128)

# Values that reduce to empty after the handler's `str(v or '').strip()`.
# `None` models a wholly omitted field.
empty_values = st.sampled_from(["", "   ", "\t", "  \n ", None])


@st.composite
def invalid_pattern_values(draw):
    """A value that is non-empty after .strip() but violates the IoT pattern:
    either a valid core with an embedded invalid character, or an over-length
    (>128) run of otherwise-valid characters."""
    kind = draw(st.sampled_from(["bad_char", "too_long"]))
    if kind == "too_long":
        return draw(st.text(alphabet=IOT_ALPHABET, min_size=129, max_size=160))
    prefix = draw(st.text(alphabet=IOT_ALPHABET, max_size=10))
    suffix = draw(st.text(alphabet=IOT_ALPHABET, max_size=10))
    # A non-whitespace invalid char guarantees a non-empty, non-matching value
    # regardless of prefix/suffix (which never contain whitespace).
    bad = draw(st.sampled_from([c for c in INVALID_CHARS if not c.isspace()]))
    return prefix + bad + suffix


def _value_for(kind, is_pattern_field, draw):
    if kind == "valid":
        return draw(valid_names)
    if kind == "empty":
        return draw(empty_values)
    # "invalid" only occurs for pattern fields.
    return draw(invalid_pattern_values())


@st.composite
def offending_bodies(draw):
    """Build a registration body with at least one offending field.

    device_name / device_group are each valid, empty, or pattern-invalid;
    usecase_id (no pattern requirement) is valid or empty. If nothing offends,
    force device_name into an offending kind so the body always satisfies the
    property's precondition."""
    dn_kind = draw(st.sampled_from(["valid", "empty", "invalid"]))
    dg_kind = draw(st.sampled_from(["valid", "empty", "invalid"]))
    uc_kind = draw(st.sampled_from(["valid", "empty"]))

    offends = (dn_kind != "valid" or dg_kind != "valid" or uc_kind != "valid")
    if not offends:
        dn_kind = draw(st.sampled_from(["empty", "invalid"]))

    kinds = {"device_name": dn_kind, "device_group": dg_kind, "usecase_id": uc_kind}
    body = {}
    for field in _REQUIRED_FIELDS:
        value = _value_for(kinds[field], field in _PATTERN_FIELDS, draw)
        if value is None:
            # `None` models an omitted key entirely.
            continue
        body[field] = value
    return body


# --------------------------------------------------------------------------- #
# Reference oracle (independent of the handler's internals)                    #
# --------------------------------------------------------------------------- #

def _is_empty(value):
    return not str(value if value is not None else "").strip()


def _expected_rejection(body):
    """Return ('missing', fields) or ('invalid', fields) per Req 1.9 then 1.2
    precedence."""
    missing = [f for f in _REQUIRED_FIELDS if _is_empty(body.get(f))]
    if missing:
        return "missing", set(missing)
    invalid = [
        f for f in _PATTERN_FIELDS
        if not REF_PATTERN.match(str(body[f]).strip())
    ]
    return "invalid", set(invalid)


# --------------------------------------------------------------------------- #
# Property                                                                     #
# --------------------------------------------------------------------------- #

@settings(max_examples=100, deadline=None)
@given(body=offending_bodies())
def test_invalid_or_missing_fields_rejected_per_field_no_persistence(
        registrations, body):
    """**Feature: station-quick-setup, Property 2: Invalid or missing fields
    are rejected, identified per field, with no persistence**

    **Validates: Requirements 1.2, 1.9**
    """
    device_registrations, table = registrations

    kind, expected_fields = _expected_rejection(body)
    # The precondition of the property: at least one field must offend.
    assume(bool(expected_fields))

    user = {"user_id": "user-under-test"}
    event = {"requestContext": {"domainName": "example.com", "stage": "v1"}}

    response = device_registrations.create_registration(user, dict(body), event)

    # Rejected with a client (validation) error.
    assert response["statusCode"] == 400
    payload = json.loads(response["body"])

    if kind == "missing":
        # Req 1.9: identifies exactly the missing/empty fields.
        assert payload["error"] == "Missing required fields"
        assert set(payload["missing_fields"]) == expected_fields
    else:
        # Req 1.2: identifies exactly the pattern-invalid fields.
        assert payload["error"] == "Invalid field values"
        assert set(payload["invalid_fields"]) == expected_fields

    # No Device_Registration item was written (holds for both rejection paths).
    assert table.scan(Select="COUNT")["Count"] == 0
