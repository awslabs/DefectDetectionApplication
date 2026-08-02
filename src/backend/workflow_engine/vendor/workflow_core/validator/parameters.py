"""Shared parameter constraint predicate (Requirements 1.8, 4.4).

Validates a single parameter value against its ``ParameterDescriptor``:
declared type check plus the declared constraints (min/max inclusive and
min_exclusive for numeric types, min_length/max_length/regex for
string-like types, ``values`` membership for enums and discrete value
sets).

This predicate is the single source of truth for parameter validation.
It is used by:
  - the Workflow_Validator check V4 (required parameters satisfy their
    constraints, Requirement 4.4), and
  - the frontend configuration panel, which mirrors this logic in
    TypeScript for inline validation errors (Requirement 1.8).

Keep the semantics here in sync with the TypeScript mirror
(``arePortsCompatible``'s sibling in the frontend validator package).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..catalog.models import (
    PARAM_TYPE_BOOL,
    PARAM_TYPE_CODE,
    PARAM_TYPE_ENUM,
    PARAM_TYPE_FLOAT,
    PARAM_TYPE_INT,
    PARAM_TYPE_MODEL_REF,
    PARAM_TYPE_STRING,
    ParameterDescriptor,
)

# --------------------------------------------------------------------------
# Violation codes (stable identifiers shared with the frontend mirror)
# --------------------------------------------------------------------------

VIOLATION_REQUIRED = "PARAM_REQUIRED"
VIOLATION_TYPE = "PARAM_TYPE"
VIOLATION_MIN = "PARAM_MIN"
VIOLATION_MIN_EXCLUSIVE = "PARAM_MIN_EXCLUSIVE"
VIOLATION_MAX = "PARAM_MAX"
VIOLATION_MIN_LENGTH = "PARAM_MIN_LENGTH"
VIOLATION_MAX_LENGTH = "PARAM_MAX_LENGTH"
VIOLATION_REGEX = "PARAM_REGEX"
VIOLATION_VALUES = "PARAM_VALUES"
VIOLATION_UNKNOWN_TYPE = "PARAM_UNKNOWN_TYPE"

#: Parameter types whose values are strings.
_STRING_LIKE_TYPES = (PARAM_TYPE_STRING, PARAM_TYPE_CODE, PARAM_TYPE_MODEL_REF)


@dataclass(frozen=True)
class ParameterViolation:
    """Why a parameter value fails validation.

    ``code`` is a stable machine-readable identifier; ``message`` is the
    human-readable reason displayed by the configuration panel
    (Requirement 1.8) and embedded in ValidationFinding records
    (Requirement 4.4).
    """

    code: str
    message: str


def check_parameter_value(descriptor: ParameterDescriptor, value: Any) -> ParameterViolation | None:
    """Validate ``value`` against ``descriptor``.

    Returns None when the value is valid, otherwise a ParameterViolation
    describing the first constraint violated. A missing value (None) is a
    violation only for required parameters.
    """
    name = descriptor.name

    if value is None:
        if descriptor.required:
            return ParameterViolation(
                VIOLATION_REQUIRED,
                "Required parameter '{0}' has no value".format(name),
            )
        return None

    type_violation = _check_type(descriptor, value)
    if type_violation is not None:
        return type_violation

    return _check_constraints(descriptor, value)


def is_parameter_value_valid(descriptor: ParameterDescriptor, value: Any) -> bool:
    """Boolean form of :func:`check_parameter_value`."""
    return check_parameter_value(descriptor, value) is None


# --------------------------------------------------------------------------
# Type check
# --------------------------------------------------------------------------

def _check_type(descriptor: ParameterDescriptor, value: Any) -> ParameterViolation | None:
    param_type = descriptor.param_type
    name = descriptor.name

    if param_type in _STRING_LIKE_TYPES:
        if not isinstance(value, str):
            return _type_violation(name, param_type, value)
        return None

    if param_type == PARAM_TYPE_INT:
        # bool is a subclass of int in Python; reject it explicitly.
        if not isinstance(value, int) or isinstance(value, bool):
            return _type_violation(name, param_type, value)
        return None

    if param_type == PARAM_TYPE_FLOAT:
        # Accept ints for float parameters (JSON number semantics).
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _type_violation(name, param_type, value)
        return None

    if param_type == PARAM_TYPE_BOOL:
        if not isinstance(value, bool):
            return _type_violation(name, param_type, value)
        return None

    if param_type == PARAM_TYPE_ENUM:
        # An enum value's "type" is membership in the declared value set;
        # the membership itself is checked with the constraints below.
        return None

    return ParameterViolation(
        VIOLATION_UNKNOWN_TYPE,
        "Parameter '{0}' has unknown declared type '{1}'".format(name, param_type),
    )


def _type_violation(name: str, param_type: str, value: Any) -> ParameterViolation:
    return ParameterViolation(
        VIOLATION_TYPE,
        "Parameter '{0}' expects type '{1}' but got {2}".format(
            name, param_type, type(value).__name__
        ),
    )


# --------------------------------------------------------------------------
# Constraint checks
# --------------------------------------------------------------------------

def _check_constraints(descriptor: ParameterDescriptor, value: Any) -> ParameterViolation | None:
    constraints = descriptor.constraints or {}
    name = descriptor.name

    # ``values`` membership: enum parameters and discrete value sets on
    # other types (e.g. an int parameter restricted to specific values).
    if "values" in constraints:
        allowed = constraints["values"]
        if not any(_matches_member(value, member) for member in allowed):
            return ParameterViolation(
                VIOLATION_VALUES,
                "Parameter '{0}' value {1!r} is not one of {2!r}".format(name, value, allowed),
            )

    # Numeric range. Negated comparisons so NaN fails bounded ranges.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = constraints.get("min")
        if minimum is not None and not value >= minimum:
            return ParameterViolation(
                VIOLATION_MIN,
                "Parameter '{0}' value {1!r} is below the minimum {2!r}".format(
                    name, value, minimum
                ),
            )
        minimum_exclusive = constraints.get("min_exclusive")
        if minimum_exclusive is not None and not value > minimum_exclusive:
            return ParameterViolation(
                VIOLATION_MIN_EXCLUSIVE,
                "Parameter '{0}' value {1!r} must be greater than {2!r}".format(
                    name, value, minimum_exclusive
                ),
            )
        maximum = constraints.get("max")
        if maximum is not None and not value <= maximum:
            return ParameterViolation(
                VIOLATION_MAX,
                "Parameter '{0}' value {1!r} is above the maximum {2!r}".format(
                    name, value, maximum
                ),
            )

    # String length and pattern.
    if isinstance(value, str):
        min_length = constraints.get("min_length")
        if min_length is not None and len(value) < min_length:
            return ParameterViolation(
                VIOLATION_MIN_LENGTH,
                "Parameter '{0}' must be at least {1} character(s) long".format(
                    name, min_length
                ),
            )
        max_length = constraints.get("max_length")
        if max_length is not None and len(value) > max_length:
            return ParameterViolation(
                VIOLATION_MAX_LENGTH,
                "Parameter '{0}' must be at most {1} character(s) long".format(
                    name, max_length
                ),
            )
        pattern = constraints.get("regex")
        if pattern is not None and re.search(pattern, value) is None:
            return ParameterViolation(
                VIOLATION_REGEX,
                "Parameter '{0}' value does not match the required pattern {1!r}".format(
                    name, pattern
                ),
            )

    return None


def _matches_member(value: Any, member: Any) -> bool:
    """Equality for ``values`` membership that never conflates bools with
    the numerically equal ints (True == 1, False == 0 in Python)."""
    if isinstance(value, bool) or isinstance(member, bool):
        return value is member
    return value == member
