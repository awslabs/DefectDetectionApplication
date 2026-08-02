"""Property test for required/optional classification in the Type_Mapping (task 1.7).

**Feature: gst-parameter-prepopulation, Property 6: Required classification follows the usable-default rule**

For any generated GStreamer_Property that maps to a Parameter_Suggestion,
the suggestion is required exactly when the property lacks a usable
default for its mapped paramType (Requirement 3.1):

  - int:    default is an int (bools rejected) or an integral float,
            within the property's declared min/max;
  - float:  default is an int or float (bools rejected), within min/max;
  - bool:   default is a bool;
  - string: default is a non-NULL string, non-empty after strip
            (null / empty / whitespace-only defaults are unusable);
  - enum:   default is a string matching one of the enum's nicks, or an
            int matching one of the enum's values.

When optional, the suggestion carries the property default (converted to
the mapped paramType) as the declaration default; when required, it
carries no default at all (Requirement 3.2).

**Validates: Requirements 3.1, 3.2**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real `map_property` directly.
"""

from __future__ import annotations

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    GTYPE_BOOL,
    GTYPE_FLOAT,
    GTYPE_INT,
    GTYPE_STRING,
    EnumValue,
    GstProperty,
    Skipped,
    map_property,
)

# ---------------------------------------------------------------------------
# Generators: writable properties over the mapped GType set, with defaults
# skewed toward the classification boundaries — absent defaults, null/empty/
# whitespace-only string defaults, wrong-typed defaults, and out-of-range
# numeric defaults (Requirement 3.1's "no usable default" cases).
# ---------------------------------------------------------------------------

# GType names claimed by the scalar mapping rows; generated GEnum type names
# must avoid these so the enum row is the one exercised.
_SCALAR_GTYPES = frozenset(GTYPE_INT) | frozenset(GTYPE_FLOAT) | {GTYPE_BOOL, GTYPE_STRING}

_names = st.text(min_size=1, max_size=30)
_optional_blurbs = st.one_of(st.none(), st.text(max_size=60))

# Strings that are unusable as a string/enum default: empty or whitespace-only.
_blankish_strings = st.one_of(
    st.just(''),
    st.text(alphabet=' \t\n\r', min_size=1, max_size=6),
)

# The full default palette: null, blank-ish strings, and every JSON scalar
# shape — deliberately including wrong-typed values for each paramType.
_boundary_defaults = st.one_of(
    st.none(),
    _blankish_strings,
    st.booleans(),
    st.integers(min_value=-10**13, max_value=10**13),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)


@st.composite
def _numeric_props(draw, gtypes, bound_values):
    """A writable int/float-row property with a random one-/two-sided range
    and a boundary-skewed default (possibly out of range or wrong-typed)."""
    lo, hi = sorted((draw(bound_values), draw(bound_values)))
    return GstProperty(
        name=draw(_names),
        gtype=draw(gtypes),
        owner=draw(_names),
        writable=True,
        blurb=draw(_optional_blurbs),
        default=draw(_boundary_defaults),
        min=lo if draw(st.booleans()) else None,
        max=hi if draw(st.booleans()) else None,
        enum_values=None,
    )


_int_props = _numeric_props(
    gtypes=st.sampled_from(sorted(GTYPE_INT)),
    bound_values=st.integers(min_value=-10**12, max_value=10**12),
)

_float_props = _numeric_props(
    gtypes=st.sampled_from(sorted(GTYPE_FLOAT)),
    bound_values=st.floats(min_value=-1e12, max_value=1e12,
                           allow_nan=False, allow_infinity=False),
)


@st.composite
def _scalar_props(draw, gtype):
    """A writable bool/string-row property with a boundary-skewed default."""
    return GstProperty(
        name=draw(_names),
        gtype=gtype,
        owner=draw(_names),
        writable=True,
        blurb=draw(_optional_blurbs),
        default=draw(_boundary_defaults),
        min=None,
        max=None,
        enum_values=None,
    )


@st.composite
def _enum_props(draw):
    """A writable GEnum-shaped property. Defaults mix matching nicks,
    matching values, and boundary-skewed misses (null, blank strings,
    non-matching strings/ints, bools, floats)."""
    gtype = draw(_names.filter(lambda g: g not in _SCALAR_GTYPES))
    pairs = draw(st.lists(
        st.tuples(st.integers(), st.text(min_size=1, max_size=20)),
        min_size=1, max_size=6,
        unique_by=(lambda p: p[0], lambda p: p[1]),
    ))
    enum_values = [EnumValue(value=v, nick=n) for v, n in pairs]

    default = draw(st.one_of(
        _boundary_defaults,
        st.sampled_from([ev.nick for ev in enum_values]),
        st.sampled_from([ev.value for ev in enum_values]),
    ))

    return GstProperty(name=draw(_names), gtype=gtype, owner=draw(_names),
                       writable=True, blurb=draw(_optional_blurbs),
                       default=default, min=None, max=None,
                       enum_values=enum_values)


_mappable_props = st.one_of(
    _int_props,
    _float_props,
    _scalar_props(GTYPE_BOOL),
    _scalar_props(GTYPE_STRING),
    _enum_props(),
)


# ---------------------------------------------------------------------------
# Oracle: the usable-default rule of Requirement 3.1, restated independently
# of the implementation. Returns the converted declaration default, or None
# when the property has no usable default for its mapped paramType.
# ---------------------------------------------------------------------------

def _expected_param_type(prop: GstProperty) -> str:
    if prop.gtype in GTYPE_INT:
        return 'int'
    if prop.gtype in GTYPE_FLOAT:
        return 'float'
    if prop.gtype == GTYPE_BOOL:
        return 'bool'
    if prop.gtype == GTYPE_STRING:
        return 'string'
    return 'enum'


def _in_range(value, prop: GstProperty) -> bool:
    if prop.min is not None and value < prop.min:
        return False
    if prop.max is not None and value > prop.max:
        return False
    return True


def _usable_default(prop: GstProperty, param_type: str):
    default = prop.default
    if default is None:
        return None
    if param_type == 'int':
        # An int, or an integral float; bools are not ints; must respect
        # the property's own declared range.
        if isinstance(default, bool):
            return None
        if isinstance(default, int):
            return default if _in_range(default, prop) else None
        if isinstance(default, float) and default.is_integer():
            converted = int(default)
            return converted if _in_range(converted, prop) else None
        return None
    if param_type == 'float':
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            return None
        converted = float(default)
        return converted if _in_range(converted, prop) else None
    if param_type == 'bool':
        return default if isinstance(default, bool) else None
    if param_type == 'string':
        # NULL, empty, or whitespace-only string defaults are unusable.
        if isinstance(default, str) and default.strip():
            return default
        return None
    # enum: a string default must match a nick; an integer default is
    # resolved to the nick of the matching enum value.
    if isinstance(default, str):
        for entry in prop.enum_values:
            if entry.nick == default:
                return entry.nick
        return None
    if isinstance(default, int) and not isinstance(default, bool):
        for entry in prop.enum_values:
            if entry.value == default:
                return entry.nick
    return None


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(prop=_mappable_props)
def test_required_classification_follows_the_usable_default_rule(prop):
    """**Feature: gst-parameter-prepopulation, Property 6: Required classification follows the usable-default rule**

    For any writable property with a mapped GType, the produced
    Parameter_Suggestion is required exactly when the property lacks a
    usable default for its mapped paramType (3.1); when optional, it
    carries the property default, converted to the paramType, as the
    declaration default (3.2); when required, it carries no default.

    **Validates: Requirements 3.1, 3.2**
    """
    result = map_property(prop)

    # Mappable input: a suggestion is always produced.
    assert not isinstance(result, Skipped)
    assert isinstance(result, dict)

    param_type = _expected_param_type(prop)
    assert result['paramType'] == param_type

    expected_default = _usable_default(prop, param_type)

    if expected_default is None:
        # 3.1: no usable default => required, and no declaration default.
        assert result['required'] is True
        assert 'default' not in result
    else:
        # 3.2: usable default => optional, carrying the converted default.
        assert result['required'] is False
        assert 'default' in result
        assert result['default'] == expected_default
        # Converted to the mapped paramType, not merely equal in value.
        assert type(result['default']) is type(expected_default)
