"""Property test for the parameter constraint predicate (task 3.3).

**Feature: workflow-manager, Property 9: Parameter constraint predicate correctness**

For all parameter descriptors in the catalog and all generated values (valid
and invalid against the descriptor's type and constraints), the shared
parameter-validation predicate accepts the value if and only if the value
satisfies the descriptor's declared type and constraints.

**Validates: Requirements 1.8**
"""

from __future__ import annotations

import re
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    NODE_CATALOG,
    PARAMETER_TYPES,
    ParameterDescriptor,
)
from workflow_core.validator import (
    check_parameter_value,
    is_parameter_value_valid,
)

# ---------------------------------------------------------------------------
# Independent oracle
#
# States the property's right-hand side ("the value satisfies the
# descriptor's declared type and constraints") directly from the descriptor
# semantics documented in workflow_core.catalog.models, written
# independently of the predicate implementation under test.
# ---------------------------------------------------------------------------

_STRING_LIKE = ("string", "code", "model_ref")


def _value_has_declared_type(param_type: str, value: Any) -> bool:
    if param_type in _STRING_LIKE:
        return isinstance(value, str)
    if param_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if param_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if param_type == "bool":
        return isinstance(value, bool)
    if param_type == "enum":
        # An enum's "type" is membership in its declared value set, which is
        # checked as the "values" constraint below.
        return True
    # Unknown declared type: nothing satisfies it.
    return False


def _is_member(value: Any, members: list) -> bool:
    """Membership that never conflates bools with numerically equal ints."""
    for member in members:
        if isinstance(value, bool) or isinstance(member, bool):
            if value is member:
                return True
        elif value == member:
            return True
    return False


def _value_satisfies_constraints(constraints: dict, value: Any) -> bool:
    if "values" in constraints and not _is_member(value, constraints["values"]):
        return False

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = constraints.get("min")
        if minimum is not None and not value >= minimum:  # NaN fails bounds
            return False
        maximum = constraints.get("max")
        if maximum is not None and not value <= maximum:
            return False

    if isinstance(value, str):
        min_length = constraints.get("min_length")
        if min_length is not None and len(value) < min_length:
            return False
        max_length = constraints.get("max_length")
        if max_length is not None and len(value) > max_length:
            return False
        pattern = constraints.get("regex")
        if pattern is not None and re.search(pattern, value) is None:
            return False

    return True


def _expected_valid(descriptor: ParameterDescriptor, value: Any) -> bool:
    """The property's specification of validity."""
    if value is None:
        return not descriptor.required
    return _value_has_declared_type(descriptor.param_type, value) and (
        _value_satisfies_constraints(descriptor.constraints or {}, value)
    )


# ---------------------------------------------------------------------------
# Descriptor generation: random ParameterDescriptors with satisfiable
# constraints, plus every descriptor actually declared in the catalog.
# ---------------------------------------------------------------------------

# Simple, valid regex constraints paired with hypothesis' from_regex for
# generating conforming strings (search semantics, like the predicate).
_REGEX_POOL = (
    r"^opc\.tcp://.+",
    r"^[A-Za-z][A-Za-z0-9_-]*$",
    r"^\d+$",
    r"^/[^\0]*$",
)

_ENUM_MEMBERS = st.one_of(
    st.text(max_size=10),
    st.integers(min_value=-100, max_value=100),
    st.booleans(),
)


@st.composite
def _generated_descriptors(draw) -> ParameterDescriptor:
    param_type = draw(st.sampled_from(PARAMETER_TYPES))
    required = draw(st.booleans())
    constraints: dict = {}

    if param_type == "enum":
        constraints["values"] = draw(
            st.lists(_ENUM_MEMBERS, min_size=1, max_size=6)
        )
    elif param_type == "int":
        kind = draw(st.sampled_from(["none", "range", "values"]))
        if kind == "range":
            low = draw(st.integers(min_value=-1000, max_value=1000))
            high = draw(st.integers(min_value=low, max_value=low + 2000))
            if draw(st.booleans()):
                constraints["min"] = low
            if draw(st.booleans()):
                constraints["max"] = high
        elif kind == "values":
            constraints["values"] = draw(
                st.lists(st.integers(min_value=-100, max_value=100),
                         min_size=1, max_size=6)
            )
    elif param_type == "float":
        if draw(st.booleans()):
            low = draw(st.floats(min_value=-1e6, max_value=1e6,
                                 allow_nan=False, allow_infinity=False))
            high = draw(st.floats(min_value=low, max_value=1e6 + 1,
                                  allow_nan=False, allow_infinity=False))
            if draw(st.booleans()):
                constraints["min"] = low
            if draw(st.booleans()):
                constraints["max"] = high
    elif param_type in _STRING_LIKE:
        kind = draw(st.sampled_from(["none", "length", "regex"]))
        if kind == "length":
            min_length = draw(st.integers(min_value=0, max_value=20))
            max_length = draw(st.integers(min_value=min_length,
                                          max_value=min_length + 30))
            if draw(st.booleans()):
                constraints["min_length"] = min_length
            if draw(st.booleans()):
                constraints["max_length"] = max_length
        elif kind == "regex":
            constraints["regex"] = draw(st.sampled_from(_REGEX_POOL))
    # bool: no constraints.

    return ParameterDescriptor(
        name=draw(st.sampled_from(["p", "gain", "endpoint", "名前"])),
        param_type=param_type,
        required=required,
        default=None,
        constraints=constraints,
    )


_CATALOG_DESCRIPTORS = tuple(
    parameter
    for node_type in NODE_CATALOG
    for parameter in node_type.parameters
)

_descriptors = st.one_of(
    _generated_descriptors(),
    st.sampled_from(_CATALOG_DESCRIPTORS),
)


# ---------------------------------------------------------------------------
# Value generation: a conforming generator targeted at the descriptor's
# constraints plus an arbitrary pool covering wrong types, out-of-range
# numbers, NaN/inf, empty/whitespace/unicode strings, and None.
# ---------------------------------------------------------------------------

_ARBITRARY_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
    st.sampled_from(["", " ", "\t\n", "日本語テスト", "opc.tcp://plc:4840"]),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)


def _conforming_values(descriptor: ParameterDescriptor) -> st.SearchStrategy:
    constraints = descriptor.constraints or {}
    param_type = descriptor.param_type

    if "values" in constraints:
        return st.sampled_from(constraints["values"])
    if param_type == "int":
        return st.integers(
            min_value=constraints.get("min", -(10 ** 6)),
            max_value=constraints.get("max", 10 ** 6),
        )
    if param_type == "float":
        return st.floats(
            min_value=constraints.get("min", -1e6),
            max_value=constraints.get("max", 1e6),
            allow_nan=False,
            allow_infinity=False,
        )
    if param_type == "bool":
        return st.booleans()
    if param_type in _STRING_LIKE:
        if "regex" in constraints:
            return st.from_regex(constraints["regex"])
        return st.text(
            min_size=constraints.get("min_length", 0),
            max_size=constraints.get("max_length", 40),
        )
    return _ARBITRARY_VALUES


@st.composite
def _descriptor_value_pairs(draw):
    descriptor = draw(_descriptors)
    value = draw(st.one_of(_conforming_values(descriptor), _ARBITRARY_VALUES))
    return descriptor, value


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

@given(case=_descriptor_value_pairs())
def test_parameter_constraint_predicate_correctness(case):
    """**Feature: workflow-manager, Property 9: Parameter constraint predicate correctness**

    **Validates: Requirements 1.8**
    """
    descriptor, value = case
    expected = _expected_valid(descriptor, value)

    violation = check_parameter_value(descriptor, value)

    # The predicate accepts the value iff it satisfies the declared type
    # and constraints.
    assert (violation is None) == expected, (
        "check_parameter_value(%r, %r) returned %r but the value %s the "
        "descriptor's declared type and constraints"
        % (descriptor, value, violation,
           "satisfies" if expected else "violates")
    )

    # The boolean form agrees with the violation form.
    assert is_parameter_value_valid(descriptor, value) == expected

    # Rejections are descriptive (non-empty code and message) so the
    # configuration panel can display a validation error (Requirement 1.8).
    if violation is not None:
        assert violation.code
        assert violation.message
