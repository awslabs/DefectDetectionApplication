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
"""
Property test for build infrastructure configuration defaults and
validation in ``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 9.2, 9.5**

The expected behavior is restated here independently of the
implementation:

- *For any* partial stored configuration object (random subset of fields
  present, possibly stored as None), the effective configuration read
  contains every documented parameter, equal to the stored value when
  present and to the documented default otherwise: ARM64 instance type
  m6g.4xlarge, x86_64 instance type m6i.4xlarge, volume size 100 GB,
  region us-east-1, maximum runtime 4 hours (Req 9.2).

- *For any* configuration update, ``validate_build_config`` accepts if
  and only if each supplied instance type's family architecture matches
  the slot it is configured for (per the instance-family -> architecture
  lookup), the volume size is a positive number, and the maximum runtime
  is a positive duration (Req 9.5). Rejections identify the invalid
  parameter.

- A rejected update leaves the stored configuration unchanged (atomic
  reject): no field of a rejected update is applied, even individually
  valid ones (Req 9.5).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure domain module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402

# ---------------------------------------------------------------------------
# Independent restatement of the documented defaults (Req 9.2, design §7)
# ---------------------------------------------------------------------------

_DOCUMENTED_DEFAULTS = {
    "arm64_instance_type": "m6g.4xlarge",
    "x86_64_instance_type": "m6i.4xlarge",
    "volume_size_gb": 100,
    "region": "us-east-1",
    "max_runtime_hours": 4,
}

# Independent (partial) instance-family -> architecture table, per the
# design ("m6g/c7g/r6g/... -> arm64, m6i/m5/c6i/... -> x86_64"). The
# generators below only draw families from these sets or families that are
# deliberately unknown, so this restatement decides every generated case.
_ARM64_FAMILIES = frozenset({"m6g", "m7g", "c6g", "c7g", "r6g", "r7g", "t4g"})
_X86_64_FAMILIES = frozenset({"m5", "m6i", "m7i", "c5", "c6i", "r5", "r6i", "t3"})
_UNKNOWN_FAMILIES = frozenset({"zz9", "foo", "quantum1"})

_SIZES = ("large", "xlarge", "2xlarge", "4xlarge", "16xlarge")


def _well_formed_types(families):
    return st.builds(
        lambda fam, size: "{0}.{1}".format(fam, size),
        st.sampled_from(sorted(families)),
        st.sampled_from(_SIZES),
    )


# Instance-type candidate values for either slot: valid arm64 types, valid
# x86_64 types, unknown families, and malformed values.
_INSTANCE_TYPE_VALUES = st.one_of(
    _well_formed_types(_ARM64_FAMILIES),
    _well_formed_types(_X86_64_FAMILIES),
    _well_formed_types(_UNKNOWN_FAMILIES),
    st.sampled_from(
        ["m6g", "4xlarge", "", "m6g.", ".xlarge", "M6G.4xlarge", 42, 1.5, True]
    ),
    st.none(),
)

# Candidate values for the positive-number parameters: positive numbers,
# non-positive numbers, and non-numbers.
_NUMBER_VALUES = st.one_of(
    st.integers(min_value=1, max_value=10000),
    st.floats(min_value=0.001, max_value=10000, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-10000, max_value=0),
    st.floats(min_value=-10000, max_value=0, allow_nan=False, allow_infinity=False),
    st.sampled_from(["100", "abc", "", True, False]),
    st.none(),
)

_UPDATE_FIELD_STRATEGIES = {
    "arm64_instance_type": _INSTANCE_TYPE_VALUES,
    "x86_64_instance_type": _INSTANCE_TYPE_VALUES,
    "volume_size_gb": _NUMBER_VALUES,
    "max_runtime_hours": _NUMBER_VALUES,
    "region": st.sampled_from(["us-east-1", "eu-west-1", "us-west-2"]),
}


@st.composite
def _updates(draw):
    """A partial configuration update: a random subset of fields."""
    fields = draw(
        st.lists(
            st.sampled_from(sorted(_UPDATE_FIELD_STRATEGIES)),
            unique=True,
            max_size=len(_UPDATE_FIELD_STRATEGIES),
        )
    )
    return {field: draw(_UPDATE_FIELD_STRATEGIES[field]) for field in fields}


# Stored partial configurations for the defaults check: a random subset of
# documented fields, holding an arbitrary stored value or None (None counts
# as absent on read).
_STORED_VALUES = st.one_of(
    st.none(),
    st.integers(min_value=1, max_value=999),
    st.sampled_from(["m6g.4xlarge", "c6i.2xlarge", "eu-central-1", "custom-value"]),
)


@st.composite
def _stored_configs(draw):
    if draw(st.booleans()):
        fields = draw(
            st.lists(
                st.sampled_from(sorted(_DOCUMENTED_DEFAULTS)),
                unique=True,
                max_size=len(_DOCUMENTED_DEFAULTS),
            )
        )
        return {field: draw(_STORED_VALUES) for field in fields}
    return None  # configuration never written


# ---------------------------------------------------------------------------
# Independent acceptance predicate (Req 9.5)
# ---------------------------------------------------------------------------

def _expected_instance_type_ok(value, required_arch):
    """True iff the value is a well-formed instance type whose family
    architecture (per the independent table) matches the slot."""
    if not isinstance(value, str):
        return False
    family, sep, size = value.partition(".")
    if not sep or not family or not size:
        return False
    family = family.lower()
    if required_arch == build_domain.ARCH_ARM64:
        return family in _ARM64_FAMILIES
    return family in _X86_64_FAMILIES


def _expected_positive_number(value):
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value > 0


def _expected_invalid_parameters(update):
    """The set of supplied parameters that must be rejected (Req 9.5).
    A field supplied as None reverts to its default and is not validated."""
    invalid = set()
    for parameter, required_arch in (
        ("arm64_instance_type", build_domain.ARCH_ARM64),
        ("x86_64_instance_type", build_domain.ARCH_X86_64),
    ):
        if parameter in update and update[parameter] is not None:
            if not _expected_instance_type_ok(update[parameter], required_arch):
                invalid.add(parameter)
    for parameter in ("volume_size_gb", "max_runtime_hours"):
        if parameter in update and update[parameter] is not None:
            if not _expected_positive_number(update[parameter]):
                invalid.add(parameter)
    return invalid


# Feature: portal-build-fleet-and-workflow-gates, Property 14: Configuration defaults and validation
# Validates: Requirements 9.2, 9.5
@settings(max_examples=200)
@given(stored=_stored_configs(), update=_updates())
def test_configuration_defaults_and_validation(stored, update):
    """For any partial stored configuration, the effective read contains
    every documented parameter (stored value when present, documented
    default otherwise, Req 9.2); for any update, validate_build_config
    accepts iff every supplied instance type matches its architecture
    slot, the volume size is a positive number, and the max runtime is a
    positive duration, and a rejected update leaves the stored
    configuration unchanged (atomic reject, Req 9.5)."""
    # --- Part 1: effective-config read applies documented defaults (9.2) ---
    effective = build_domain.effective_build_config(stored)
    for field, default in _DOCUMENTED_DEFAULTS.items():
        assert field in effective, (
            "effective configuration is missing parameter %r" % field
        )
        stored_value = (stored or {}).get(field)
        expected = stored_value if stored_value is not None else default
        assert effective[field] == expected, (
            "effective[%r] = %r, expected %r (stored=%r)"
            % (field, effective[field], expected, stored)
        )

    # --- Part 2: validation accepts exactly the valid updates (9.5) ---
    invalid_parameters = _expected_invalid_parameters(update)
    result = build_domain.validate_build_config(update)
    assert result.valid == (not invalid_parameters), (
        "validate_build_config(%r) returned valid=%r, expected %r "
        "(invalid parameters: %r)"
        % (update, result.valid, not invalid_parameters, invalid_parameters)
    )
    if invalid_parameters:
        # Every rejection identifies the invalid parameter (Req 9.5).
        reported = {error["parameter"] for error in result.errors}
        assert reported == invalid_parameters, (
            "rejection reported parameters %r, expected %r: %r"
            % (reported, invalid_parameters, result.errors)
        )
    else:
        assert result.errors == ()

    # --- Part 3: rejected updates leave stored config unchanged (9.5) ---
    if invalid_parameters:
        before = dict(stored) if stored else {}
        new_config, apply_result = build_domain.apply_config_update(stored, update)
        assert not apply_result.valid
        assert new_config == before, (
            "rejected update mutated the stored configuration: %r -> %r "
            "(update=%r)" % (before, new_config, update)
        )
        # The original input object is also untouched.
        assert (dict(stored) if stored else {}) == before
