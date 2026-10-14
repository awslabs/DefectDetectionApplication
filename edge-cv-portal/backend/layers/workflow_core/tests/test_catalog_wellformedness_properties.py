"""Property test for catalog well-formedness (task 1.3).

**Feature: workflow-manager, Property 13: Catalog well-formedness**

For all node type descriptors in the catalog, the descriptor declares its
input ports, output ports, and parameters completely: every port has a type
from the known port-type set, every parameter has a valid type and
satisfiable constraints, every declared default value satisfies its own
parameter's constraints, and the category is one of the five palette
sections. Every parameter additionally carries field-level help: a
non-empty description and at least one example value satisfying the
parameter's own type and constraints.

**Validates: Requirements 2.8**
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    CATEGORIES,
    NODE_CATALOG,
    PARAMETER_TYPES,
    PORT_TYPES,
    ParameterDescriptor,
    PortDescriptor,
)
from workflow_core.validator import check_parameter_value

# The catalog is a fixed, finite data set; the property quantifies over
# every descriptor in it, so the generator samples descriptors directly
# from the catalog itself.
node_type_descriptors = st.sampled_from(NODE_CATALOG)


# ---------------------------------------------------------------------------
# Constraint satisfiability and default-value satisfaction checks
# ---------------------------------------------------------------------------

def _assert_constraints_satisfiable(param: ParameterDescriptor, context: str):
    """Constraints must admit at least one value (Property 13)."""
    constraints = param.constraints

    assert isinstance(constraints, dict), (
        "%s: constraints must be a dict, got %r" % (context, type(constraints))
    )

    # Numeric ranges must be non-empty.
    if "min" in constraints and "max" in constraints:
        assert constraints["min"] <= constraints["max"], (
            "%s: empty numeric range min=%r > max=%r"
            % (context, constraints["min"], constraints["max"])
        )
    if "min_exclusive" in constraints and "max" in constraints:
        assert constraints["min_exclusive"] < constraints["max"], (
            "%s: empty numeric range min_exclusive=%r >= max=%r"
            % (context, constraints["min_exclusive"], constraints["max"])
        )

    # Length ranges must be non-empty and non-negative.
    if "min_length" in constraints:
        assert constraints["min_length"] >= 0, (
            "%s: negative min_length %r" % (context, constraints["min_length"])
        )
    if "min_length" in constraints and "max_length" in constraints:
        assert constraints["min_length"] <= constraints["max_length"], (
            "%s: empty length range" % context
        )

    # Enumerated value sets must be non-empty.
    if "values" in constraints:
        assert len(constraints["values"]) > 0, (
            "%s: empty allowed-values list" % context
        )

    # Enum parameters are only satisfiable with a declared value set.
    if param.param_type == "enum":
        assert constraints.get("values"), (
            "%s: enum parameter without a non-empty 'values' constraint" % context
        )

    # Regex constraints must at least be valid patterns.
    if "regex" in constraints:
        try:
            re.compile(constraints["regex"])
        except re.error as exc:
            raise AssertionError(
                "%s: invalid regex constraint %r (%s)"
                % (context, constraints["regex"], exc)
            )


_STRING_LIKE_TYPES = ("string", "code", "model_ref")


def _assert_default_satisfies_constraints(param: ParameterDescriptor, context: str):
    """A declared default must satisfy its own parameter's constraints."""
    default = param.default
    if default is None:
        # No declared default (e.g. required parameters the user must set).
        return

    constraints = param.constraints

    # Type agreement between default and declared parameter type.
    if param.param_type == "int":
        assert isinstance(default, int) and not isinstance(default, bool), (
            "%s: int default %r is not an int" % (context, default)
        )
    elif param.param_type == "float":
        assert isinstance(default, (int, float)) and not isinstance(default, bool), (
            "%s: float default %r is not numeric" % (context, default)
        )
    elif param.param_type == "bool":
        assert isinstance(default, bool), (
            "%s: bool default %r is not a bool" % (context, default)
        )
    elif param.param_type in _STRING_LIKE_TYPES:
        assert isinstance(default, str), (
            "%s: %s default %r is not a string" % (context, param.param_type, default)
        )
    # enum defaults are checked against the values list below.

    # Range constraints (min/max inclusive, min_exclusive strict).
    if "min" in constraints:
        assert default >= constraints["min"], (
            "%s: default %r below min %r" % (context, default, constraints["min"])
        )
    if "min_exclusive" in constraints:
        assert default > constraints["min_exclusive"], (
            "%s: default %r not above min_exclusive %r"
            % (context, default, constraints["min_exclusive"])
        )
    if "max" in constraints:
        assert default <= constraints["max"], (
            "%s: default %r above max %r" % (context, default, constraints["max"])
        )

    # Length constraints (string-like values).
    if "min_length" in constraints:
        assert len(default) >= constraints["min_length"], (
            "%s: default %r shorter than min_length %r"
            % (context, default, constraints["min_length"])
        )
    if "max_length" in constraints:
        assert len(default) <= constraints["max_length"], (
            "%s: default %r longer than max_length %r"
            % (context, default, constraints["max_length"])
        )

    # Enumerated value sets.
    if "values" in constraints:
        assert default in constraints["values"], (
            "%s: default %r not in allowed values %r"
            % (context, default, constraints["values"])
        )

    # Regex constraints.
    if "regex" in constraints:
        assert re.search(constraints["regex"], default) is not None, (
            "%s: default %r does not match regex %r"
            % (context, default, constraints["regex"])
        )


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------

@given(descriptor=node_type_descriptors)
def test_catalog_well_formedness(descriptor):
    """**Feature: workflow-manager, Property 13: Catalog well-formedness**

    **Validates: Requirements 2.8**
    """
    ctx = "node type %r" % descriptor.type_id

    # The category is one of the five palette sections.
    assert descriptor.category in CATEGORIES, (
        "%s: unknown category %r" % (ctx, descriptor.category)
    )

    # Every port has a type from the known port-type set.
    for direction, ports in (("input", descriptor.inputs),
                             ("output", descriptor.outputs)):
        for port in ports:
            port_ctx = "%s %s port %r" % (ctx, direction, getattr(port, "name", port))
            assert isinstance(port, PortDescriptor), (
                "%s: not a PortDescriptor" % port_ctx
            )
            assert isinstance(port.name, str) and port.name, (
                "%s: missing port name" % port_ctx
            )
            assert port.port_type in PORT_TYPES, (
                "%s: unknown port type %r" % (port_ctx, port.port_type)
            )

    # Every parameter has a valid type, satisfiable constraints, and a
    # declared default that satisfies its own constraints.
    for param in descriptor.parameters:
        param_ctx = "%s parameter %r" % (ctx, getattr(param, "name", param))
        assert isinstance(param, ParameterDescriptor), (
            "%s: not a ParameterDescriptor" % param_ctx
        )
        assert isinstance(param.name, str) and param.name, (
            "%s: missing parameter name" % param_ctx
        )
        assert param.param_type in PARAMETER_TYPES, (
            "%s: unknown parameter type %r" % (param_ctx, param.param_type)
        )
        _assert_constraints_satisfiable(param, param_ctx)
        _assert_default_satisfies_constraints(param, param_ctx)

        # Field-level help: a non-empty description and at least one
        # example value satisfying the parameter's own constraints.
        assert isinstance(param.description, str) and param.description.strip(), (
            "%s: missing description" % param_ctx
        )
        assert isinstance(param.examples, list) and param.examples, (
            "%s: missing examples" % param_ctx
        )
        for example in param.examples:
            violation = check_parameter_value(param, example)
            assert violation is None, (
                "%s: example %r is not a valid value: %s"
                % (param_ctx, example, violation)
            )
