"""Unit tests for the shared parameter constraint predicate (task 3.1).

Covers the declared type check plus min/max, enum, length, and regex
constraints over ParameterDescriptor.

_Requirements: 1.8, 4.4_
"""

import pytest

from workflow_core.catalog import ParameterDescriptor
from workflow_core.validator import (
    VIOLATION_MAX,
    VIOLATION_MAX_LENGTH,
    VIOLATION_MIN,
    VIOLATION_MIN_EXCLUSIVE,
    VIOLATION_MIN_LENGTH,
    VIOLATION_REGEX,
    VIOLATION_REQUIRED,
    VIOLATION_TYPE,
    VIOLATION_UNKNOWN_TYPE,
    VIOLATION_VALUES,
    check_parameter_value,
    is_parameter_value_valid,
)


def _desc(name="p", param_type="string", required=True, default=None, constraints=None):
    return ParameterDescriptor(
        name=name,
        param_type=param_type,
        required=required,
        default=default,
        constraints=constraints or {},
    )


# --------------------------------------------------------------------------
# Required / missing values
# --------------------------------------------------------------------------

class TestRequired:
    def test_missing_value_on_required_parameter_is_violation(self):
        violation = check_parameter_value(_desc(required=True), None)
        assert violation is not None
        assert violation.code == VIOLATION_REQUIRED
        assert "p" in violation.message

    def test_missing_value_on_optional_parameter_is_valid(self):
        assert check_parameter_value(_desc(required=False), None) is None

    def test_missing_value_on_optional_parameter_skips_constraints(self):
        desc = _desc(required=False, constraints={"min_length": 5})
        assert is_parameter_value_valid(desc, None)


# --------------------------------------------------------------------------
# Type checks
# --------------------------------------------------------------------------

class TestTypeCheck:
    @pytest.mark.parametrize("param_type,good,bad", [
        ("string", "hello", 42),
        ("code", "print('x')", 3.14),
        ("model_ref", "widget-anomaly-v3", ["not", "a", "string"]),
        ("int", 7, "7"),
        ("float", 0.5, "0.5"),
        ("bool", True, "true"),
    ])
    def test_declared_type_enforced(self, param_type, good, bad):
        desc = _desc(param_type=param_type)
        assert check_parameter_value(desc, good) is None
        violation = check_parameter_value(desc, bad)
        assert violation is not None
        assert violation.code == VIOLATION_TYPE

    def test_bool_is_not_an_int(self):
        violation = check_parameter_value(_desc(param_type="int"), True)
        assert violation is not None
        assert violation.code == VIOLATION_TYPE

    def test_int_is_accepted_for_float(self):
        assert check_parameter_value(_desc(param_type="float"), 1) is None

    def test_float_is_rejected_for_int(self):
        violation = check_parameter_value(_desc(param_type="int"), 1.5)
        assert violation is not None
        assert violation.code == VIOLATION_TYPE

    def test_unknown_declared_type_is_violation(self):
        violation = check_parameter_value(_desc(param_type="mystery"), "x")
        assert violation is not None
        assert violation.code == VIOLATION_UNKNOWN_TYPE


# --------------------------------------------------------------------------
# Numeric min/max
# --------------------------------------------------------------------------

class TestNumericRange:
    def test_within_range_is_valid(self):
        desc = _desc(param_type="int", constraints={"min": 0, "max": 100})
        assert check_parameter_value(desc, 50) is None

    def test_boundaries_inclusive(self):
        desc = _desc(param_type="int", constraints={"min": 0, "max": 100})
        assert check_parameter_value(desc, 0) is None
        assert check_parameter_value(desc, 100) is None

    def test_below_min(self):
        desc = _desc(param_type="int", constraints={"min": 0, "max": 100})
        violation = check_parameter_value(desc, -1)
        assert violation is not None
        assert violation.code == VIOLATION_MIN

    def test_above_max(self):
        desc = _desc(param_type="int", constraints={"min": 0, "max": 100})
        violation = check_parameter_value(desc, 101)
        assert violation is not None
        assert violation.code == VIOLATION_MAX

    def test_min_only(self):
        desc = _desc(param_type="int", constraints={"min": 0})
        assert check_parameter_value(desc, 10 ** 12) is None
        assert check_parameter_value(desc, -1).code == VIOLATION_MIN

    def test_float_range(self):
        desc = _desc(param_type="float", constraints={"min": 0.0, "max": 1.0})
        assert check_parameter_value(desc, 0.5) is None
        assert check_parameter_value(desc, 1.5).code == VIOLATION_MAX

    def test_nan_fails_bounded_range(self):
        desc = _desc(param_type="float", constraints={"min": 0.0, "max": 1.0})
        violation = check_parameter_value(desc, float("nan"))
        assert violation is not None

    def test_min_exclusive_rejects_the_bound_itself(self):
        # Mirrors the llm_inference top_p parameter: top_p > 0.0, <= 1.0.
        desc = _desc(param_type="float",
                     constraints={"min_exclusive": 0.0, "max": 1.0})
        violation = check_parameter_value(desc, 0.0)
        assert violation is not None
        assert violation.code == VIOLATION_MIN_EXCLUSIVE

    def test_min_exclusive_rejects_values_below_the_bound(self):
        desc = _desc(param_type="float",
                     constraints={"min_exclusive": 0.0, "max": 1.0})
        assert check_parameter_value(desc, -0.5).code == VIOLATION_MIN_EXCLUSIVE

    def test_min_exclusive_accepts_values_above_the_bound(self):
        desc = _desc(param_type="float",
                     constraints={"min_exclusive": 0.0, "max": 1.0})
        assert check_parameter_value(desc, 0.001) is None
        assert check_parameter_value(desc, 1.0) is None

    def test_min_exclusive_nan_fails(self):
        desc = _desc(param_type="float", constraints={"min_exclusive": 0.0})
        assert check_parameter_value(desc, float("nan")) is not None


# --------------------------------------------------------------------------
# Enum / discrete value sets
# --------------------------------------------------------------------------

class TestEnumValues:
    def test_member_is_valid(self):
        desc = _desc(param_type="enum",
                     constraints={"values": ["rising", "falling", "both"]})
        assert check_parameter_value(desc, "falling") is None

    def test_non_member_is_violation(self):
        desc = _desc(param_type="enum",
                     constraints={"values": ["rising", "falling", "both"]})
        violation = check_parameter_value(desc, "sideways")
        assert violation is not None
        assert violation.code == VIOLATION_VALUES

    def test_int_valued_enum(self):
        # Mirrors the mqtt_publish qos parameter in the catalog.
        desc = _desc(param_type="enum", constraints={"values": [0, 1, 2]})
        assert check_parameter_value(desc, 1) is None
        assert check_parameter_value(desc, 3).code == VIOLATION_VALUES

    def test_bool_does_not_match_int_member(self):
        desc = _desc(param_type="enum", constraints={"values": [0, 1, 2]})
        violation = check_parameter_value(desc, True)
        assert violation is not None
        assert violation.code == VIOLATION_VALUES

    def test_values_constraint_on_int_type(self):
        desc = _desc(param_type="int", constraints={"values": [10, 20, 30]})
        assert check_parameter_value(desc, 20) is None
        assert check_parameter_value(desc, 25).code == VIOLATION_VALUES


# --------------------------------------------------------------------------
# String length and regex
# --------------------------------------------------------------------------

class TestStringConstraints:
    def test_min_length(self):
        desc = _desc(constraints={"min_length": 1})
        assert check_parameter_value(desc, "x") is None
        assert check_parameter_value(desc, "").code == VIOLATION_MIN_LENGTH

    def test_max_length(self):
        desc = _desc(constraints={"max_length": 3})
        assert check_parameter_value(desc, "abc") is None
        assert check_parameter_value(desc, "abcd").code == VIOLATION_MAX_LENGTH

    def test_regex_match(self):
        # Mirrors the opcua_write endpoint parameter in the catalog.
        desc = _desc(constraints={"min_length": 1, "regex": r"^opc\.tcp://.+"})
        assert check_parameter_value(desc, "opc.tcp://plc-01:4840") is None
        violation = check_parameter_value(desc, "http://plc-01:4840")
        assert violation is not None
        assert violation.code == VIOLATION_REGEX

    def test_length_applies_to_code_and_model_ref(self):
        for param_type in ("code", "model_ref"):
            desc = _desc(param_type=param_type, constraints={"min_length": 1})
            assert check_parameter_value(desc, "").code == VIOLATION_MIN_LENGTH
            assert check_parameter_value(desc, "value") is None

    def test_unicode_length(self):
        desc = _desc(constraints={"min_length": 2, "max_length": 4})
        assert check_parameter_value(desc, "日本語") is None
        assert check_parameter_value(desc, "日").code == VIOLATION_MIN_LENGTH

    def test_empty_constraints_accept_any_string(self):
        assert check_parameter_value(_desc(constraints={}), "") is None


# --------------------------------------------------------------------------
# Catalog cross-check: every catalog default satisfies its own descriptor
# --------------------------------------------------------------------------

class TestCatalogDefaults:
    def test_all_catalog_defaults_are_valid(self):
        from workflow_core.catalog import NODE_CATALOG

        for node_type in NODE_CATALOG:
            for parameter in node_type.parameters:
                if parameter.default is None:
                    continue
                violation = check_parameter_value(parameter, parameter.default)
                assert violation is None, (
                    "{0}.{1} default {2!r} violates its own constraints: {3}".format(
                        node_type.type_id, parameter.name,
                        parameter.default, violation,
                    )
                )
