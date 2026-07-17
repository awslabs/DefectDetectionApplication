"""Property test for Bedrock configuration resolution
(custom-node-code-assist, task 1.3).

**Feature: custom-node-code-assist, Property 10: Bedrock configuration
resolution**

_For any_ stored `bedrock_configuration` settings item - any subset of the
known keys, arbitrary extra keys, Decimal-typed numbers (how DynamoDB stores
every number), explicit nulls for the sampling parameters, junk
`timeout_seconds` values, nested (`{setting_key, value: {...}}`) or flat item
shape, or no stored item at all - `bedrock_common.get_bedrock_configuration()`
SHALL return the defaults overridden by the present non-null stored values,
with an explicitly-null `temperature`/`top_p` left unset, and a resolved
`timeout_seconds` that is an integer in [1, 60], equal to 60 whenever the
stored value is missing or uninterpretable.

**Validates: Requirements 4.1, 4.4, 4.6, 4.7**

Runs against the shared moto stack from conftest.py: the stored item goes
through a real (moto) DynamoDB put/get round trip, so numbers arrive as
Decimal exactly as in production.
"""
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

SETTINGS_TABLE_NAME = "test-settings-bedrock-config-resolution"

KNOWN_KEYS = ("model_id", "region", "max_tokens",
              "temperature", "top_p", "timeout_seconds")
SAMPLING_KEYS = ("temperature", "top_p")


@pytest.fixture(scope="module")
def config_env(aws_stack):
    """Settings table + freshly imported bedrock_common bound to it inside
    moto (conftest re-import pattern)."""
    import boto3

    os.environ["SETTINGS_TABLE"] = SETTINGS_TABLE_NAME
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # Re-import so the module binds SETTINGS_TABLE and a moto-intercepted
    # boto3 resource.
    sys.modules.pop("bedrock_common", None)
    import bedrock_common

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        bedrock_common=bedrock_common,
        settings_table=resource.Table(SETTINGS_TABLE_NAME),
    )


# ---------------------------------------------------------------------------
# DynamoDB round-trip helpers
# ---------------------------------------------------------------------------

def to_ddb(value):
    """Native Python -> DynamoDB-storable (floats become Decimal, exactly
    like boto3 requires for numbers)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    return value


def expected_native(value):
    """What a stored native value looks like after the DynamoDB Decimal
    round trip and the module's Decimal-to-native conversion: whole numbers
    come back as int, fractional ones as float, everything else unchanged."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        d = Decimal(str(value))
        return float(d) if d % 1 else int(d)
    if isinstance(value, list):
        return [expected_native(v) for v in value]
    return value


def apply_store_state(env, shape, stored):
    """Materialize a store state: no item, the production nested shape
    ({setting_key, value: {...}}), or the also-accepted flat shape."""
    env.settings_table.delete_item(
        Key={"setting_key": "bedrock_configuration"})
    if shape == "no_item":
        return
    if shape == "nested":
        env.settings_table.put_item(Item={
            "setting_key": "bedrock_configuration",
            "value": to_ddb(stored),
        })
    else:  # flat: attributes directly on the item
        item = {"setting_key": "bedrock_configuration"}
        item.update(to_ddb(stored))
        env.settings_table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Floats destined for DynamoDB are rounded: raw hypothesis floats include
# magnitudes below DynamoDB's representable range (< ~1e-130), which fail
# PutItem with a number underflow before the code under test runs.
storable_unit_floats = st.floats(
    min_value=0, max_value=1, allow_nan=False, allow_infinity=False,
).map(lambda x: round(x, 6))

# Non-empty printable-ASCII strings for the string-typed keys.
storable_strings = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1, max_size=40,
)

# Values per known key; None appears everywhere so both "explicit null
# sampling parameter" and "null non-sampling value keeps the default" are
# exercised.
KNOWN_KEY_VALUES = {
    "model_id": st.one_of(st.none(), storable_strings),
    "region": st.one_of(st.none(), storable_strings),
    "max_tokens": st.one_of(
        st.none(),
        st.integers(min_value=1, max_value=100000),
        st.floats(min_value=1, max_value=100000,
                  allow_nan=False, allow_infinity=False)
        .map(lambda x: round(x, 3)),
    ),
    "temperature": st.one_of(st.none(), st.integers(min_value=0, max_value=1),
                             storable_unit_floats),
    "top_p": st.one_of(st.none(), st.integers(min_value=0, max_value=1),
                       storable_unit_floats),
    # Interpretable (in- and out-of-range ints, Decimal floats, booleans)
    # and uninterpretable (null, letter strings, lists) timeout values.
    "timeout_seconds": st.one_of(
        st.none(),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(min_value=-100, max_value=200,
                  allow_nan=False, allow_infinity=False)
        .map(lambda x: round(x, 3)),
        st.booleans(),
        st.text(alphabet="abcdefghijxyz ", min_size=1, max_size=8),
        st.lists(st.integers(min_value=0, max_value=9),
                 min_size=0, max_size=3),
    ),
}

extra_keys = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12,
).filter(lambda k: k not in KNOWN_KEYS and k not in ("value", "setting_key"))

extra_values = st.one_of(
    st.none(),
    st.integers(min_value=-100, max_value=100),
    storable_strings,
)


@st.composite
def stored_bedrock_items(draw):
    """(shape, stored): a store state - nothing stored, or a nested/flat
    item over any subset of known keys plus arbitrary extra keys."""
    shape = draw(st.sampled_from(["no_item", "nested", "flat"]))
    if shape == "no_item":
        return shape, None
    stored = {}
    for key in draw(st.lists(st.sampled_from(KNOWN_KEYS), unique=True)):
        stored[key] = draw(KNOWN_KEY_VALUES[key])
    stored.update(draw(st.dictionaries(extra_keys, extra_values, max_size=3)))
    return shape, stored


# ---------------------------------------------------------------------------
# Property 10: Bedrock configuration resolution
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(case=stored_bedrock_items())
def test_bedrock_configuration_resolution(config_env, case):
    """For any stored configuration item, the resolved configuration equals
    the defaults overridden by the present non-null stored values, with
    explicit-null sampling parameters unset, and a timeout that is an
    integer in [1, 60], equal to 60 whenever the stored value is missing or
    uninterpretable (Requirements 4.1, 4.4, 4.6, 4.7)."""
    shape, stored = case
    apply_store_state(config_env, shape, stored)

    resolved = config_env.bedrock_common.get_bedrock_configuration()
    defaults = dict(config_env.bedrock_common.DEFAULT_BEDROCK_CONFIG)

    # Exactly the known keys - extra stored keys never leak through.
    assert set(resolved) == set(defaults) == set(KNOWN_KEYS)

    native = ({key: expected_native(value)
               for key, value in stored.items()} if stored else {})

    # Non-sampling keys: a present non-null stored value overrides the
    # default; a missing key or explicit null keeps the default (Req 4.1).
    for key in ("model_id", "region", "max_tokens"):
        if native.get(key) is not None:
            assert resolved[key] == native[key], (
                f"{key}: stored {native[key]!r} must override the default, "
                f"got {resolved[key]!r}")
        else:
            assert resolved[key] == defaults[key], (
                f"{key}: missing/null stored value must resolve to the "
                f"default {defaults[key]!r}, got {resolved[key]!r}")

    # Sampling parameters: a present key overrides - including an explicit
    # null, which leaves the parameter unset; a missing key resolves to the
    # (unset) default (Requirements 4.6, 4.7).
    for key in SAMPLING_KEYS:
        if stored is not None and key in stored:
            assert resolved[key] == native[key] and (
                (resolved[key] is None) == (native[key] is None)), (
                f"{key}: explicitly stored {native[key]!r} (null = unset) "
                f"must be honored, got {resolved[key]!r}")
        else:
            assert resolved[key] is None, (
                f"{key}: unstored sampling parameter must stay unset, "
                f"got {resolved[key]!r}")

    # Timeout: always an integer in [1, 60]; 60 whenever the stored value
    # is missing or uninterpretable; otherwise the interpreted integer
    # clamped to [1, 60] (Requirement 4.4).
    timeout = resolved["timeout_seconds"]
    assert isinstance(timeout, int) and not isinstance(timeout, bool), (
        f"timeout must resolve to an int, got {timeout!r}")
    assert 1 <= timeout <= 60, f"timeout must be in [1, 60], got {timeout!r}"

    raw = native.get("timeout_seconds")
    try:
        interpreted = int(raw)
    except (TypeError, ValueError):
        interpreted = None
    if raw is None or interpreted is None:
        assert timeout == 60, (
            f"missing/uninterpretable stored timeout {raw!r} must resolve "
            f"to 60, got {timeout}")
    else:
        assert timeout == max(1, min(interpreted, 60)), (
            f"stored timeout {raw!r} must resolve to its clamped integer "
            f"value, got {timeout}")
