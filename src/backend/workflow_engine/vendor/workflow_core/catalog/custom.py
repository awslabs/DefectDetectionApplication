"""Custom node type declarations: conversion and catalog resolution.

Makes the Node_Type_Catalog composable (custom-node-designer design,
"Dynamic Node_Type_Catalog extension"): built-in descriptors stay the
static frozen ``NODE_CATALOG``; Custom_Node_Type declarations — stored
per Use_Case in the identical camelCase wire shape the node-catalog
endpoint already serves — are converted into frozen
``NodeTypeDescriptor`` instances by :func:`descriptor_from_declaration`
and merged into a per-request effective catalog by
:func:`resolve_catalog`.

Validation (Requirements 5.3, 8.2, 8.5):
  - port types against ``PORT_TYPES``,
  - categories against ``CATEGORIES``,
  - parameter descriptors against ``PARAMETER_TYPES`` (including
    constraint satisfiability, defaults and examples satisfying the
    parameter's own type and constraints, exactly like the built-in
    catalog well-formedness predicate),
  - mappings against ``ARCHITECTURES``,
  - DeepStream-flagged declarations restricted to the JetPack
    architectures (``arm64_jp4/jp5/jp6``) — a DeepStream-backed type is
    unavailable on architectures without a matching runtime (5.3).

Invalid declarations raise :class:`DeclarationError` identifying the
offending field (8.5).
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .models import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCHITECTURES,
    CATEGORIES,
    PARAM_TYPE_ENUM,
    PARAMETER_TYPES,
    PORT_TYPES,
    GstMapping,
    NodeTypeDescriptor,
    ParameterDescriptor,
    PortDescriptor,
)
from .nodes import NODE_CATALOG

#: Architectures with a DeepStream runtime (Requirement 5.1/5.3):
#: DeepStream targets Jetson, so DeepStream-flagged declarations may only
#: declare mappings for the JetPack builds.
DEEPSTREAM_ARCHITECTURES = (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
)

#: Wire constraint keys -> ParameterDescriptor.constraints keys.
#: Mirrors (inverts) ``_CONSTRAINT_KEY_MAP`` in workflow_validation.py;
#: min/max/regex/values pass through unchanged.
_WIRE_CONSTRAINT_KEY_MAP = {
    "minLength": "min_length",
    "maxLength": "max_length",
    "minExclusive": "min_exclusive",
}


class DeclarationError(ValueError):
    """A Custom_Node_Type declaration is invalid.

    ``field`` identifies the offending field using a JSON-path-like
    notation over the wire declaration (e.g. ``inputs[0].portType``,
    ``parameters[2].default``, ``mappings[1].arch``) so the caller can
    surface exactly the invalid declaration entry (Requirement 8.5).
    """

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__("{0}: {1}".format(field, message))


# --------------------------------------------------------------------------
# Small field validators
# --------------------------------------------------------------------------

def _require_dict(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise DeclarationError(field, "must be an object, got {0}".format(type(value).__name__))
    return value


def _require_list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise DeclarationError(field, "must be a list, got {0}".format(type(value).__name__))
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeclarationError(field, "must be a non-empty string, got {0!r}".format(value))
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DeclarationError(field, "must be a boolean, got {0!r}".format(value))
    return value


# --------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------

def _port_from_wire(port: Any, field: str) -> PortDescriptor:
    port = _require_dict(port, field)
    name = _require_str(port.get("name"), field + ".name")
    port_type = port.get("portType")
    if port_type not in PORT_TYPES:
        raise DeclarationError(
            field + ".portType",
            "unknown port type {0!r}; must be one of {1}".format(port_type, list(PORT_TYPES)),
        )
    return PortDescriptor(name=name, port_type=port_type)


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

def _constraints_from_wire(constraints: Any, field: str) -> dict:
    if constraints is None:
        return {}
    constraints = _require_dict(constraints, field)
    return {_WIRE_CONSTRAINT_KEY_MAP.get(k, k): v for k, v in constraints.items()}


def _check_constraints_satisfiable(param_type: str, constraints: dict, field: str) -> None:
    """Constraints must admit at least one value (mirrors the built-in
    catalog well-formedness predicate)."""
    if "min" in constraints and "max" in constraints:
        if constraints["min"] > constraints["max"]:
            raise DeclarationError(field, "empty numeric range: min > max")
    if "min_exclusive" in constraints and "max" in constraints:
        if constraints["min_exclusive"] >= constraints["max"]:
            raise DeclarationError(
                field, "empty numeric range: minExclusive >= max"
            )
    if "min_length" in constraints and constraints["min_length"] < 0:
        raise DeclarationError(field + ".minLength", "must be non-negative")
    if "min_length" in constraints and "max_length" in constraints:
        if constraints["min_length"] > constraints["max_length"]:
            raise DeclarationError(field, "empty length range: minLength > maxLength")
    if "values" in constraints:
        values = constraints["values"]
        if not isinstance(values, list) or not values:
            raise DeclarationError(field + ".values", "must be a non-empty list")
    if param_type == PARAM_TYPE_ENUM and not constraints.get("values"):
        raise DeclarationError(
            field + ".values",
            "enum parameters require a non-empty 'values' constraint",
        )
    if "regex" in constraints:
        pattern = constraints["regex"]
        if not isinstance(pattern, str):
            raise DeclarationError(field + ".regex", "must be a string pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DeclarationError(field + ".regex", "invalid pattern: {0}".format(exc))


def _parameter_from_wire(parameter: Any, field: str) -> ParameterDescriptor:
    # Imported at call time: the validator package imports back into
    # workflow_core.catalog, so a module-level import here would recurse
    # through a partially initialized catalog package.
    from ..validator.parameters import check_parameter_value

    parameter = _require_dict(parameter, field)
    name = _require_str(parameter.get("name"), field + ".name")

    param_type = parameter.get("paramType")
    if param_type not in PARAMETER_TYPES:
        raise DeclarationError(
            field + ".paramType",
            "unknown parameter type {0!r}; must be one of {1}".format(
                param_type, list(PARAMETER_TYPES)
            ),
        )

    required = _require_bool(parameter.get("required", False), field + ".required")

    constraints = _constraints_from_wire(parameter.get("constraints"), field + ".constraints")
    _check_constraints_satisfiable(param_type, constraints, field + ".constraints")

    # Field-level help is part of the registration declaration
    # (Requirement 8.1) and of the catalog well-formedness predicate the
    # built-in node types satisfy: a non-empty description and at least
    # one example value satisfying the parameter's own constraints.
    description = _require_str(parameter.get("description"), field + ".description")
    examples = _require_list(parameter.get("examples"), field + ".examples")
    if not examples:
        raise DeclarationError(field + ".examples", "must contain at least one example value")

    depends_on = parameter.get("dependsOn")
    if depends_on is not None:
        depends_on = _require_str(depends_on, field + ".dependsOn")

    descriptor = ParameterDescriptor(
        name=name,
        param_type=param_type,
        required=required,
        default=parameter.get("default"),
        constraints=constraints,
        depends_on=depends_on,
        description=description,
        examples=list(examples),
    )

    # A declared default must satisfy the parameter's own type and
    # constraints (None means "no default": required parameters the
    # user must set).
    default = descriptor.default
    if default is not None:
        violation = check_parameter_value(descriptor, default)
        if violation is not None:
            raise DeclarationError(
                field + ".default",
                "default {0!r} violates the parameter's own declaration: {1}".format(
                    default, violation.message
                ),
            )

    for index, example in enumerate(examples):
        example_field = "{0}.examples[{1}]".format(field, index)
        if example is None:
            raise DeclarationError(example_field, "example values must not be null")
        violation = check_parameter_value(descriptor, example)
        if violation is not None:
            raise DeclarationError(
                example_field,
                "example {0!r} violates the parameter's own declaration: {1}".format(
                    example, violation.message
                ),
            )

    return descriptor


def _check_depends_on(parameters: Sequence[ParameterDescriptor], field: str) -> None:
    """``dependsOn`` must name a bool parameter on the same node type."""
    bool_params = {p.name for p in parameters if p.param_type == "bool"}
    for index, parameter in enumerate(parameters):
        if parameter.depends_on is None:
            continue
        if parameter.depends_on not in bool_params or parameter.depends_on == parameter.name:
            raise DeclarationError(
                "{0}[{1}].dependsOn".format(field, index),
                "{0!r} must name a bool parameter declared on the same node type".format(
                    parameter.depends_on
                ),
            )


# --------------------------------------------------------------------------
# Mappings
# --------------------------------------------------------------------------

def _element_from_wire(element: Any, field: str) -> dict:
    element = _require_dict(element, field)
    factory = _require_str(element.get("factory"), field + ".factory")
    args_template = element.get("argsTemplate")
    if args_template is None:
        args_template = {}
    args_template = _require_dict(args_template, field + ".argsTemplate")
    return {"factory": factory, "args_template": dict(args_template)}


def _mapping_from_wire(mapping: Any, field: str, deepstream: bool) -> GstMapping:
    mapping = _require_dict(mapping, field)

    arch = mapping.get("arch")
    if arch not in ARCHITECTURES:
        raise DeclarationError(
            field + ".arch",
            "unknown architecture {0!r}; must be one of {1}".format(arch, list(ARCHITECTURES)),
        )
    if deepstream and arch not in DEEPSTREAM_ARCHITECTURES:
        raise DeclarationError(
            field + ".arch",
            "DeepStream-flagged declarations may only map the JetPack "
            "architectures {0}; got {1!r}".format(list(DEEPSTREAM_ARCHITECTURES), arch),
        )

    element_chain = _require_list(mapping.get("elementChain", []), field + ".elementChain")
    elements = [
        _element_from_wire(element, "{0}.elementChain[{1}]".format(field, index))
        for index, element in enumerate(element_chain)
    ]

    executor_binding = mapping.get("executorBinding")
    if executor_binding is not None:
        executor_binding = _require_str(executor_binding, field + ".executorBinding")

    plugin_dependencies = _require_list(
        mapping.get("pluginDependencies", []), field + ".pluginDependencies"
    )
    for index, dependency in enumerate(plugin_dependencies):
        _require_str(dependency, "{0}.pluginDependencies[{1}]".format(field, index))

    return GstMapping(
        arch=arch,
        element_chain=elements,
        executor_binding=executor_binding,
        plugin_dependencies=list(plugin_dependencies),
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def descriptor_from_declaration(decl: Any) -> NodeTypeDescriptor:
    """Convert a stored Custom_Node_Type declaration into a frozen descriptor.

    ``decl`` is the node-catalog wire shape (camelCase — identical to what
    ``descriptor_to_wire`` in workflow_validation.py serves): ``typeId``,
    ``displayName``, ``category``, ``inputs``/``outputs`` (``[{name,
    portType}]``), ``parameters`` (``[{name, paramType, required, default,
    constraints, dependsOn, description, examples}]``), ``mappings``
    (``[{arch, elementChain: [{factory, argsTemplate}], executorBinding,
    pluginDependencies}]``), and ``hardwareDependent``. An optional
    ``deepstream`` flag restricts the declarable mapping architectures to
    ``arm64_jp4/jp5/jp6`` (Requirement 5.3). Extra keys (``typeVersion``,
    ``lifecycleState``) are ignored.

    Raises :class:`DeclarationError` identifying the offending field for
    any invalid declaration (Requirement 8.5).
    """
    decl = _require_dict(decl, "declaration")

    type_id = _require_str(decl.get("typeId"), "typeId")
    display_name = _require_str(decl.get("displayName"), "displayName")

    category = decl.get("category")
    if category not in CATEGORIES:
        raise DeclarationError(
            "category",
            "unknown category {0!r}; must be one of {1}".format(category, list(CATEGORIES)),
        )

    deepstream = _require_bool(decl.get("deepstream", False), "deepstream")
    hardware_dependent = _require_bool(
        decl.get("hardwareDependent", False), "hardwareDependent"
    )

    inputs = [
        _port_from_wire(port, "inputs[{0}]".format(index))
        for index, port in enumerate(_require_list(decl.get("inputs", []), "inputs"))
    ]
    outputs = [
        _port_from_wire(port, "outputs[{0}]".format(index))
        for index, port in enumerate(_require_list(decl.get("outputs", []), "outputs"))
    ]

    parameters = [
        _parameter_from_wire(parameter, "parameters[{0}]".format(index))
        for index, parameter in enumerate(
            _require_list(decl.get("parameters", []), "parameters")
        )
    ]
    _check_parameter_names_unique(parameters)
    _check_depends_on(parameters, "parameters")

    mappings = []
    seen_archs = set()
    for index, mapping in enumerate(_require_list(decl.get("mappings", []), "mappings")):
        field = "mappings[{0}]".format(index)
        converted = _mapping_from_wire(mapping, field, deepstream)
        if converted.arch in seen_archs:
            raise DeclarationError(
                field + ".arch", "duplicate mapping for architecture {0!r}".format(converted.arch)
            )
        seen_archs.add(converted.arch)
        mappings.append(converted)

    return NodeTypeDescriptor(
        type_id=type_id,
        category=category,
        display_name=display_name,
        inputs=inputs,
        outputs=outputs,
        parameters=parameters,
        mappings=mappings,
        hardware_dependent=hardware_dependent,
    )


def _check_parameter_names_unique(parameters: Sequence[ParameterDescriptor]) -> None:
    seen = set()
    for index, parameter in enumerate(parameters):
        if parameter.name in seen:
            raise DeclarationError(
                "parameters[{0}].name".format(index),
                "duplicate parameter name {0!r}".format(parameter.name),
            )
        seen.add(parameter.name)


def resolve_catalog(custom_descriptors: Sequence[NodeTypeDescriptor]) -> tuple:
    """Merge custom descriptors into the built-in catalog (Requirement 8.2).

    Returns ``NODE_CATALOG + tuple(custom_descriptors)`` preserving order:
    built-in descriptors first (unchanged), then the custom descriptors in
    the given order. Duplicate ``type_id`` entries are rejected from the
    result — a custom descriptor colliding with a built-in type id never
    displaces the built-in (built-ins always win), and a custom descriptor
    colliding with an earlier custom descriptor is dropped (first wins).
    """
    resolved = list(NODE_CATALOG)
    seen = {descriptor.type_id for descriptor in NODE_CATALOG}
    for descriptor in custom_descriptors:
        if descriptor.type_id in seen:
            continue
        seen.add(descriptor.type_id)
        resolved.append(descriptor)
    return tuple(resolved)
