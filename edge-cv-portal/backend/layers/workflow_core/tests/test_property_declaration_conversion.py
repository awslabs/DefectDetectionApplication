"""Property test for Custom_Node_Type declaration conversion (task 1.3).

**Feature: custom-node-designer, Property 1: Declaration conversion accepts exactly the valid declarations**

For all Custom_Node_Type declarations (valid ones, and ones corrupted with
a random known defect — port type outside PORT_TYPES, category outside
CATEGORIES, parameter type outside PARAMETER_TYPES, architecture outside
ARCHITECTURES, default violating its own constraints, DeepStream mapping
outside arm64_jp4/jp5/jp6, duplicate parameter names or mapping
architectures, ...), ``descriptor_from_declaration`` succeeds if and only
if the declaration is valid; on success the resulting descriptor
faithfully reflects the declaration and satisfies the same catalog
well-formedness predicate as built-in node types, and DeepStream-flagged
declarations yield mappings only for arm64_jp4/jp5/jp6; on failure the
error identifies the offending field.

**Validates: Requirements 1.7, 5.3, 8.4, 8.5**
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import (
    ARCHITECTURES,
    CATEGORIES,
    DEEPSTREAM_ARCHITECTURES,
    PARAMETER_TYPES,
    PORT_TYPES,
    DeclarationError,
    descriptor_from_declaration,
)
from workflow_core.validator import check_parameter_value

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

# Identifier-ish names. Lowercase a-j only, so sentinel values used by the
# corruption strategies (uppercase / punctuation) can never collide with a
# generated name.
_NAME = st.text(alphabet="abcdefghij", min_size=1, max_size=8)

_DESCRIPTION = st.text(
    alphabet="abcdefghij ", min_size=1, max_size=30
).filter(lambda s: s.strip())

#: Wire constraint keys -> ParameterDescriptor.constraints keys (mirrors
#: the module under test's _WIRE_CONSTRAINT_KEY_MAP).
_WIRE_CONSTRAINT_KEY_MAP = {"minLength": "min_length", "maxLength": "max_length"}

_NON_JETSON_ARCHITECTURES = tuple(
    arch for arch in ARCHITECTURES if arch not in DEEPSTREAM_ARCHITECTURES
)


def _invalid_member(known):
    """A value outside the ``known`` constant tuple (or not a string at all)."""
    return st.one_of(
        st.none(),
        st.text(alphabet="XYZ0123456789", max_size=8).filter(lambda s: s not in known),
    )


# ---------------------------------------------------------------------------
# Valid parameter declarations (wire shape), one strategy per parameter type
# ---------------------------------------------------------------------------

@st.composite
def _int_param(draw, name):
    lo = draw(st.integers(-1000, 1000))
    hi = lo + draw(st.integers(0, 1000))
    value = st.integers(lo, hi)
    return {
        "name": name,
        "paramType": "int",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "constraints": {"min": lo, "max": hi},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=3)),
    }


@st.composite
def _float_param(draw, name):
    lo = draw(st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False))
    hi = lo + draw(st.floats(0, 1e6, allow_nan=False, allow_infinity=False))
    value = st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False)
    return {
        "name": name,
        "paramType": "float",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "constraints": {"min": lo, "max": hi},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=3)),
    }


@st.composite
def _string_like_param(draw, name, param_type):
    min_len = draw(st.integers(0, 3))
    max_len = min_len + draw(st.integers(0, 5))
    value = st.text(alphabet="abcdefghij", min_size=min_len, max_size=max_len)
    return {
        "name": name,
        "paramType": param_type,
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "constraints": {"minLength": min_len, "maxLength": max_len},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=3)),
    }


@st.composite
def _bool_param(draw, name):
    return {
        "name": name,
        "paramType": "bool",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), st.booleans())),
        "constraints": {},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(st.booleans(), min_size=1, max_size=2)),
    }


@st.composite
def _enum_param(draw, name):
    values = draw(st.lists(_NAME, min_size=1, max_size=4, unique=True))
    member = st.sampled_from(values)
    return {
        "name": name,
        "paramType": "enum",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), member)),
        "constraints": {"values": values},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(member, min_size=1, max_size=3)),
    }


def _param_of_type(param_type, name):
    if param_type == "int":
        return _int_param(name)
    if param_type == "float":
        return _float_param(name)
    if param_type == "bool":
        return _bool_param(name)
    if param_type == "enum":
        return _enum_param(name)
    return _string_like_param(name, param_type)  # string / code / model_ref


@st.composite
def _parameter_lists(draw):
    """1..4 uniquely named parameters. Element 0 is always an int with a
    min/max range so the default/example corruption strategies always have
    a bounded parameter to violate; validly, sometimes a parameter
    dependsOn a bool parameter of the same declaration."""
    names = draw(st.lists(_NAME, min_size=1, max_size=4, unique=True))
    params = [draw(_int_param(names[0]))]
    for name in names[1:]:
        param_type = draw(st.sampled_from(PARAMETER_TYPES))
        params.append(draw(_param_of_type(param_type, name)))

    bool_names = [p["name"] for p in params if p["paramType"] == "bool"]
    if bool_names and draw(st.booleans()):
        toggle = draw(st.sampled_from(bool_names))
        candidates = [p for p in params if p["name"] != toggle]
        if candidates:
            draw(st.sampled_from(candidates))["dependsOn"] = toggle
    return params


# ---------------------------------------------------------------------------
# Valid ports and mappings (wire shape)
# ---------------------------------------------------------------------------

_PORTS = st.lists(
    st.fixed_dictionaries({"name": _NAME, "portType": st.sampled_from(PORT_TYPES)}),
    min_size=1,
    max_size=2,
)

_ELEMENT_ARGS = st.dictionaries(_NAME, _NAME, max_size=2)


@st.composite
def _elements(draw):
    element = {"factory": draw(_NAME)}
    args = draw(st.one_of(st.none(), _ELEMENT_ARGS))
    if args is not None:
        element["argsTemplate"] = args
    return element


@st.composite
def _mapping_for(draw, arch):
    mapping = {
        "arch": arch,
        "elementChain": draw(st.lists(_elements(), max_size=2)),
        "pluginDependencies": draw(st.lists(_NAME, max_size=2)),
    }
    executor_binding = draw(st.one_of(st.none(), _NAME))
    if executor_binding is not None:
        mapping["executorBinding"] = executor_binding
    return mapping


@st.composite
def _mapping_lists(draw, deepstream):
    pool = DEEPSTREAM_ARCHITECTURES if deepstream else ARCHITECTURES
    archs = draw(
        st.lists(st.sampled_from(pool), min_size=1, max_size=len(pool), unique=True)
    )
    return [draw(_mapping_for(arch)) for arch in archs]


# ---------------------------------------------------------------------------
# Valid declarations
# ---------------------------------------------------------------------------

@st.composite
def valid_declarations(draw):
    deepstream = draw(st.booleans())
    return {
        "typeId": "custom." + draw(_NAME),
        "displayName": draw(_DESCRIPTION),
        "category": draw(st.sampled_from(CATEGORIES)),
        "deepstream": deepstream,
        "hardwareDependent": draw(st.booleans()),
        "inputs": draw(_PORTS),
        "outputs": draw(_PORTS),
        "parameters": draw(_parameter_lists()),
        "mappings": draw(_mapping_lists(deepstream)),
    }


# ---------------------------------------------------------------------------
# Corruptions: each takes (draw, valid declaration), plants exactly one
# known defect in place, and returns the field the error must identify.
# ---------------------------------------------------------------------------

def _corrupt_category(draw, decl):
    decl["category"] = draw(_invalid_member(CATEGORIES))
    return "category"


def _corrupt_port_type(draw, decl):
    side = draw(st.sampled_from(["inputs", "outputs"]))
    index = draw(st.integers(0, len(decl[side]) - 1))
    decl[side][index]["portType"] = draw(_invalid_member(PORT_TYPES))
    return "{0}[{1}].portType".format(side, index)


def _corrupt_param_type(draw, decl):
    index = draw(st.integers(0, len(decl["parameters"]) - 1))
    decl["parameters"][index]["paramType"] = draw(_invalid_member(PARAMETER_TYPES))
    return "parameters[{0}].paramType".format(index)


def _corrupt_mapping_arch(draw, decl):
    index = draw(st.integers(0, len(decl["mappings"]) - 1))
    decl["mappings"][index]["arch"] = draw(_invalid_member(ARCHITECTURES))
    return "mappings[{0}].arch".format(index)


def _corrupt_deepstream_non_jetson(draw, decl):
    # A DeepStream-flagged declaration mapping a non-Jetson architecture
    # (Requirement 5.3).
    decl["deepstream"] = True
    decl["mappings"] = [decl["mappings"][0]]
    decl["mappings"][0]["arch"] = draw(st.sampled_from(_NON_JETSON_ARCHITECTURES))
    return "mappings[0].arch"


def _corrupt_duplicate_mapping_arch(draw, decl):
    decl["mappings"].append(copy.deepcopy(decl["mappings"][0]))
    return "mappings[{0}].arch".format(len(decl["mappings"]) - 1)


def _corrupt_duplicate_parameter_name(draw, decl):
    decl["parameters"].append(copy.deepcopy(decl["parameters"][0]))
    return "parameters[{0}].name".format(len(decl["parameters"]) - 1)


def _corrupt_default(draw, decl):
    # parameters[0] is always the bounded int parameter.
    param = decl["parameters"][0]
    param["default"] = param["constraints"]["max"] + 1 + draw(st.integers(0, 100))
    return "parameters[0].default"


def _corrupt_example(draw, decl):
    param = decl["parameters"][0]
    param["examples"] = list(param["examples"]) + [param["constraints"]["max"] + 1]
    return "parameters[0].examples[{0}]".format(len(param["examples"]) - 1)


def _corrupt_empty_examples(draw, decl):
    decl["parameters"][0]["examples"] = []
    return "parameters[0].examples"


def _corrupt_depends_on(draw, decl):
    # Uppercase sentinel: generated parameter names are lowercase a-j only.
    decl["parameters"][0]["dependsOn"] = "NO_SUCH_TOGGLE"
    return "parameters[0].dependsOn"


def _corrupt_enum_without_values(draw, decl):
    decl["parameters"][0] = {
        "name": decl["parameters"][0]["name"],
        "paramType": "enum",
        "required": False,
        "default": None,
        "constraints": {},
        "description": "an enum with no declared values",
        "examples": ["a"],
    }
    return "parameters[0].constraints.values"


def _corrupt_type_id(draw, decl):
    decl["typeId"] = draw(st.sampled_from([None, "", "   "]))
    return "typeId"


def _corrupt_display_name(draw, decl):
    decl["displayName"] = draw(st.sampled_from([None, "", "   "]))
    return "displayName"


_CORRUPTIONS = [
    _corrupt_category,
    _corrupt_port_type,
    _corrupt_param_type,
    _corrupt_mapping_arch,
    _corrupt_deepstream_non_jetson,
    _corrupt_duplicate_mapping_arch,
    _corrupt_duplicate_parameter_name,
    _corrupt_default,
    _corrupt_example,
    _corrupt_empty_examples,
    _corrupt_depends_on,
    _corrupt_enum_without_values,
    _corrupt_type_id,
    _corrupt_display_name,
]


@st.composite
def declaration_cases(draw):
    """(declaration, expected_error_field). expected_error_field is None
    for valid declarations."""
    decl = draw(valid_declarations())
    if draw(st.booleans()):
        return decl, None
    corruption = draw(st.sampled_from(_CORRUPTIONS))
    return decl, corruption(draw, decl)


# ---------------------------------------------------------------------------
# Success-side assertions
# ---------------------------------------------------------------------------

def _expected_constraints(wire_constraints):
    return {
        _WIRE_CONSTRAINT_KEY_MAP.get(key, key): value
        for key, value in (wire_constraints or {}).items()
    }


def _assert_faithful(decl, descriptor):
    """The converted descriptor reflects the declaration exactly."""
    assert descriptor.type_id == decl["typeId"]
    assert descriptor.display_name == decl["displayName"]
    assert descriptor.category == decl["category"]
    assert descriptor.hardware_dependent == decl["hardwareDependent"]

    for wire_ports, ports in ((decl["inputs"], descriptor.inputs),
                              (decl["outputs"], descriptor.outputs)):
        assert [(p.name, p.port_type) for p in ports] == [
            (w["name"], w["portType"]) for w in wire_ports
        ]

    assert len(descriptor.parameters) == len(decl["parameters"])
    for wire, param in zip(decl["parameters"], descriptor.parameters):
        assert param.name == wire["name"]
        assert param.param_type == wire["paramType"]
        assert param.required == wire["required"]
        assert param.default == wire["default"]
        assert param.constraints == _expected_constraints(wire["constraints"])
        assert param.depends_on == wire.get("dependsOn")
        assert param.description == wire["description"]
        assert param.examples == wire["examples"]

    assert len(descriptor.mappings) == len(decl["mappings"])
    for wire, mapping in zip(decl["mappings"], descriptor.mappings):
        assert mapping.arch == wire["arch"]
        assert mapping.element_chain == [
            {"factory": e["factory"], "args_template": e.get("argsTemplate") or {}}
            for e in wire["elementChain"]
        ]
        assert mapping.executor_binding == wire.get("executorBinding")
        assert mapping.plugin_dependencies == wire["pluginDependencies"]


def _assert_well_formed(descriptor):
    """The descriptor satisfies the same catalog well-formedness predicate
    as built-in node types (Requirement 8.4)."""
    assert descriptor.type_id and isinstance(descriptor.type_id, str)
    assert descriptor.category in CATEGORIES

    for port in list(descriptor.inputs) + list(descriptor.outputs):
        assert isinstance(port.name, str) and port.name
        assert port.port_type in PORT_TYPES

    for param in descriptor.parameters:
        assert isinstance(param.name, str) and param.name
        assert param.param_type in PARAMETER_TYPES

        constraints = param.constraints
        if "min" in constraints and "max" in constraints:
            assert constraints["min"] <= constraints["max"]
        if "min_length" in constraints:
            assert constraints["min_length"] >= 0
        if "min_length" in constraints and "max_length" in constraints:
            assert constraints["min_length"] <= constraints["max_length"]
        if param.param_type == "enum":
            assert constraints.get("values")

        # Field-level help, defaults and examples all satisfy the
        # parameter's own type and constraints.
        assert isinstance(param.description, str) and param.description.strip()
        assert isinstance(param.examples, list) and param.examples
        if param.default is not None:
            assert check_parameter_value(param, param.default) is None
        for example in param.examples:
            assert example is not None
            assert check_parameter_value(param, example) is None

    seen_archs = set()
    for mapping in descriptor.mappings:
        assert mapping.arch in ARCHITECTURES
        assert mapping.arch not in seen_archs
        seen_archs.add(mapping.arch)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=25)
@given(case=declaration_cases())
def test_declaration_conversion_accepts_exactly_the_valid_declarations(case):
    """**Feature: custom-node-designer, Property 1: Declaration conversion accepts exactly the valid declarations**

    **Validates: Requirements 1.7, 5.3, 8.4, 8.5**
    """
    decl, expected_field = case

    if expected_field is None:
        descriptor = descriptor_from_declaration(copy.deepcopy(decl))
        _assert_faithful(decl, descriptor)
        _assert_well_formed(descriptor)
        # DeepStream-flagged declarations yield mappings only for the
        # JetPack architectures (Requirement 5.3).
        if decl["deepstream"]:
            assert all(
                mapping.arch in DEEPSTREAM_ARCHITECTURES
                for mapping in descriptor.mappings
            )
    else:
        with pytest.raises(DeclarationError) as excinfo:
            descriptor_from_declaration(decl)
        # The error identifies the offending field (Requirements 1.7, 8.5).
        assert excinfo.value.field == expected_field
        assert expected_field in str(excinfo.value)
