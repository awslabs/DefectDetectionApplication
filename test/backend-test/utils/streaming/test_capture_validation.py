# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Property-based tests for out-of-range per-capture config rejection (Task 7.6).

Feature: concurrent-camera-stream-viewing, Property 21: Out-of-range capture
configuration is rejected — for any per-capture image-source configuration
containing at least one parameter value outside its accepted range, the request
is rejected with an error identifying an offending parameter and the previously
active image-source configuration is preserved unchanged.

These target the pure validator added in task 7.3,
:func:`utils.streaming.backends.validate_config_against_bounds`, which raises
:class:`~utils.streaming.backends.CaptureConfigValidationError` (carrying the
offending ``parameter``) when a supplied value falls outside its bounds. The
validator is pure logic with no ``gi`` / hardware dependency, so this module is
import-safe on a bare checkout.

Validates: Requirements 6.5
"""
import copy

from hypothesis import assume, given, settings
from hypothesis import strategies as st

# ``backends`` is import-safe without the native stack: its gi imports are lazy
# and ``validate_config_against_bounds`` is pure validation logic.
from utils.streaming.backends import (
    CaptureConfigValidationError,
    validate_config_against_bounds,
)

# Advanced (non-top-level) control keys exercised by the bounds/config maps: one
# enumeration control and one boolean control.
ENUM_KEY = "balanceWhiteAuto"
BOOL_KEY = "reverseX"

# Top-level numeric controls range-checked by the validator.
NUMERIC_KEYS = ("gain", "exposure")

# Short lowercase identifiers used for enumeration option strings.
_safe_text = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)

# Values that are NOT valid booleans (reject for a boolean control): excludes
# bool, 0/1, and the "true"/"false"/"0"/"1" strings the validator accepts.
_non_boolean_values = st.sampled_from(["maybe", "yes", "nope", "on", "off", 2, 3, 7, 3.5])


@st.composite
def _capture_case(draw):
    """Build a (bounds, bad_config, offending_param, good_config) example.

    ``bounds`` carries gain/exposure numeric ranges, an enumeration control, and
    a boolean control. ``bad_config`` forces exactly one parameter
    (``offending_param``) out of range while every other value stays in range, so
    the validator deterministically rejects naming that parameter. ``good_config``
    is an all-in-range counterpart used to assert the accepting path.
    """
    # Numeric bounds for gain / exposure (min < max).
    def numeric_bounds():
        lo = draw(st.integers(min_value=-1000, max_value=1000))
        hi = draw(st.integers(min_value=lo + 1, max_value=lo + 2000))
        return {"type": "float", "min": lo, "max": hi}

    gain_b = numeric_bounds()
    exp_b = numeric_bounds()
    numeric_bounds_map = {"gain": gain_b, "exposure": exp_b}

    options = draw(st.lists(_safe_text, min_size=1, max_size=5, unique=True))

    bounds = {
        "gain": gain_b,
        "exposure": exp_b,
        ENUM_KEY: {"type": "enumeration", "options": options},
        BOOL_KEY: {"type": "boolean"},
    }

    offending = draw(st.sampled_from(["gain", "exposure", ENUM_KEY, BOOL_KEY]))

    def in_range_numeric(b):
        return draw(st.integers(min_value=b["min"], max_value=b["max"]))

    def out_of_range_numeric(b):
        return draw(
            st.one_of(
                st.integers(min_value=b["min"] - 1000, max_value=b["min"] - 1),
                st.integers(min_value=b["max"] + 1, max_value=b["max"] + 1000),
            )
        )

    def in_range_enum():
        return draw(st.sampled_from(options))

    def out_of_range_enum():
        val = draw(_safe_text)
        # Must not match any accepted option (validator compares str(value)).
        assume(val not in options)
        return val

    def in_range_bool():
        return draw(st.sampled_from([True, False, 0, 1, "true", "false", "0", "1"]))

    def out_of_range_bool():
        return draw(_non_boolean_values)

    # bad_config: exactly the offending parameter is out of range.
    bad_config = {
        "gain": out_of_range_numeric(gain_b) if offending == "gain" else in_range_numeric(gain_b),
        "exposure": out_of_range_numeric(exp_b)
        if offending == "exposure"
        else in_range_numeric(exp_b),
        "advancedSettings": {
            ENUM_KEY: out_of_range_enum() if offending == ENUM_KEY else in_range_enum(),
            BOOL_KEY: out_of_range_bool() if offending == BOOL_KEY else in_range_bool(),
        },
    }

    # good_config: every value is freshly drawn in range.
    good_config = {
        "gain": in_range_numeric(gain_b),
        "exposure": in_range_numeric(exp_b),
        "advancedSettings": {
            ENUM_KEY: in_range_enum(),
            BOOL_KEY: in_range_bool(),
        },
    }

    return bounds, bad_config, offending, good_config


# Feature: concurrent-camera-stream-viewing, Property 21: Out-of-range capture configuration is rejected
# Validates: Requirements 6.5
@settings(max_examples=200)
@given(_capture_case())
def test_property_21_out_of_range_capture_configuration_is_rejected(case):
    """Out-of-range capture config is rejected naming an offending parameter;
    the in-range config is accepted and validation never mutates the config.

    Feature: concurrent-camera-stream-viewing, Property 21: Out-of-range capture configuration is rejected
    Validates: Requirements 6.5
    """
    bounds, bad_config, offending, good_config = case

    # The previously active configuration must be preserved unchanged across a
    # rejected validation, so snapshot it for an equality check afterwards.
    bad_snapshot = copy.deepcopy(bad_config)

    # Rejection: the out-of-range config raises, naming an offending parameter.
    try:
        validate_config_against_bounds(bad_config, bounds)
        raised = None
    except CaptureConfigValidationError as err:
        raised = err

    assert raised is not None, (
        f"expected CaptureConfigValidationError for out-of-range {offending}, got none"
    )
    # The error identifies an offending parameter, and (since exactly one value
    # was forced out of range) it is precisely that parameter.
    assert raised.parameter == offending
    assert offending in ("gain", "exposure", ENUM_KEY, BOOL_KEY)

    # Preservation: validation must not mutate the supplied config (the active
    # image-source configuration is left unchanged on rejection).
    assert bad_config == bad_snapshot

    # Acceptance: an all-in-range config validates and returns None.
    good_snapshot = copy.deepcopy(good_config)
    assert validate_config_against_bounds(good_config, bounds) is None
    # The accepting path likewise leaves the config untouched.
    assert good_config == good_snapshot
