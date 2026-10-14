"""Property test for Plugin_Scaffold validation (task 1.8).

**Feature: custom-node-designer, Property 3: Scaffold validation rejects non-buildable source**

For all scaffold file maps produced by corrupting a valid scaffold with a
random defect (removing the Frame_Processing_Hook file, removing all build
configurations, emptying required files), scaffold validation rejects the
source with a description of the failure, and accepts every uncorrupted
scaffold.

**Validates: Requirements 2.6**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import CATEGORIES, PORT_TYPES
from workflow_core.catalog.models import DEVICE_ARCHITECTURES
from workflow_core.scaffold import (
    HOOK_FILE,
    ScaffoldError,
    build_config_path,
    c_source_path,
    render_scaffold,
    scaffold_defects,
    validate_scaffold,
)

# ---------------------------------------------------------------------------
# Valid declaration generation (wire shape + selected architectures)
# ---------------------------------------------------------------------------

# Identifier-ish names: lowercase a-j so every typeId yields a usable
# GStreamer element name and parameter names are valid throughout.
_NAME = st.text(alphabet="abcdefghij", min_size=1, max_size=8)

_DESCRIPTION = st.text(
    alphabet="abcdefghij ", min_size=1, max_size=30
).filter(lambda s: s.strip())


@st.composite
def _int_param(draw, name):
    lo = draw(st.integers(-100, 100))
    hi = lo + draw(st.integers(0, 100))
    value = st.integers(lo, hi)
    return {
        "name": name,
        "paramType": "int",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "constraints": {"min": lo, "max": hi},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=2)),
    }


@st.composite
def _float_param(draw, name):
    lo = draw(st.floats(-1e3, 1e3, allow_nan=False, allow_infinity=False))
    hi = lo + draw(st.floats(0, 1e3, allow_nan=False, allow_infinity=False))
    value = st.floats(min_value=lo, max_value=hi,
                      allow_nan=False, allow_infinity=False)
    return {
        "name": name,
        "paramType": "float",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "constraints": {"min": lo, "max": hi},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=2)),
    }


@st.composite
def _bool_param(draw, name):
    return {
        "name": name,
        "paramType": "bool",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), st.booleans())),
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(st.booleans(), min_size=1, max_size=2)),
    }


@st.composite
def _string_param(draw, name):
    value = st.text(alphabet="abcdefghij", min_size=0, max_size=8)
    return {
        "name": name,
        "paramType": "string",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), value)),
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(value, min_size=1, max_size=2)),
    }


@st.composite
def _enum_param(draw, name):
    values = draw(st.lists(_NAME, min_size=1, max_size=3, unique=True))
    member = st.sampled_from(values)
    return {
        "name": name,
        "paramType": "enum",
        "required": draw(st.booleans()),
        "default": draw(st.one_of(st.none(), member)),
        "constraints": {"values": values},
        "description": draw(_DESCRIPTION),
        "examples": draw(st.lists(member, min_size=1, max_size=2)),
    }


def _param_for(draw, name):
    kind = draw(st.sampled_from(["int", "float", "bool", "string", "enum"]))
    strategy = {
        "int": _int_param,
        "float": _float_param,
        "bool": _bool_param,
        "string": _string_param,
        "enum": _enum_param,
    }[kind]
    return draw(strategy(name))


_PORTS = st.lists(
    st.fixed_dictionaries(
        {"name": _NAME, "portType": st.sampled_from(PORT_TYPES)}),
    min_size=1,
    max_size=2,
)


@st.composite
def valid_declarations(draw):
    """A valid Custom_Node_Type declaration with selected architectures,
    accepted by render_scaffold."""
    names = draw(st.lists(_NAME, min_size=0, max_size=3, unique=True))
    architectures = draw(st.lists(
        st.sampled_from(DEVICE_ARCHITECTURES),
        min_size=1, max_size=len(DEVICE_ARCHITECTURES), unique=True))
    return {
        "typeId": "custom." + draw(_NAME),
        "displayName": draw(_DESCRIPTION),
        "description": draw(st.one_of(st.none(), _DESCRIPTION)),
        "category": draw(st.sampled_from(CATEGORIES)),
        "inputs": draw(_PORTS),
        "outputs": draw(_PORTS),
        "parameters": [_param_for(draw, name) for name in names],
        "architectures": architectures,
    }


# ---------------------------------------------------------------------------
# Corruptions: each plants one named defect (Property 3) into a rendered
# scaffold file map in place, and returns the fragments the failure
# description must contain.
# ---------------------------------------------------------------------------

def _remove_hook_file(draw, files, declaration):
    """Removing the Frame_Processing_Hook file."""
    del files[HOOK_FILE]
    return [HOOK_FILE]


def _remove_all_build_configurations(draw, files, declaration):
    """Removing all build configurations."""
    for arch in declaration["architectures"]:
        del files[build_config_path(arch)]
    # every missing architecture must be described
    return [build_config_path(arch) for arch in declaration["architectures"]]


def _empty_required_file(draw, files, declaration):
    """Emptying a required file (hook, C skeleton, or a build config)."""
    required = [HOOK_FILE, c_source_path(declaration)] + [
        build_config_path(arch) for arch in declaration["architectures"]]
    path = draw(st.sampled_from(required))
    files[path] = draw(st.sampled_from(["", "   ", "\n\t \n"]))
    return [path, "empty"]


_CORRUPTIONS = [
    _remove_hook_file,
    _remove_all_build_configurations,
    _empty_required_file,
]


@st.composite
def scaffold_cases(draw):
    """(declaration, files, expected_fragments). expected_fragments is
    None for an uncorrupted scaffold, otherwise the substrings the
    rejection description must contain."""
    declaration = draw(valid_declarations())
    files = render_scaffold(declaration)
    if draw(st.booleans()):
        return declaration, files, None
    corruption = draw(st.sampled_from(_CORRUPTIONS))
    return declaration, files, corruption(draw, files, declaration)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@settings(max_examples=25)
@given(case=scaffold_cases())
def test_scaffold_validation_rejects_non_buildable_source(case):
    """**Feature: custom-node-designer, Property 3: Scaffold validation rejects non-buildable source**

    **Validates: Requirements 2.6**
    """
    declaration, files, expected_fragments = case

    if expected_fragments is None:
        # Every uncorrupted scaffold is accepted.
        assert scaffold_defects(files, declaration) == []
        assert validate_scaffold(files, declaration) is None
    else:
        # Every corrupted scaffold is rejected with a description of the
        # failure (Requirement 2.6).
        defects = scaffold_defects(files, declaration)
        assert defects, "corrupted scaffold reported no defects"
        assert all(isinstance(defect, str) and defect.strip()
                   for defect in defects)

        with pytest.raises(ScaffoldError) as excinfo:
            validate_scaffold(files, declaration)
        assert excinfo.value.defects == defects

        message = str(excinfo.value)
        for fragment in expected_fragments:
            assert fragment in message
