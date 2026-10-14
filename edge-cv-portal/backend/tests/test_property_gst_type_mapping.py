
"""Property test for the GType -> parameter Type_Mapping (task 1.5).

**Feature: gst-parameter-prepopulation, Property 3: Type mapping is total and correctly typed over writable known GTypes**

For any generated GStreamer_Property whose GType is in the known mapping
set and which is writable, `map_property` produces a Parameter_Suggestion
(never a Skipped entry) whose `paramType` matches the GType class per the
mapping table (int/float/bool/string/enum), whose `constraints` carry the
property's min/max when the property is ranged and the enum nicks when it
is a GEnum, and whose `default`, when present, is the property's declared
default converted to the mapped paramType.

**Validates: Requirements 2.1, 2.2, 2.3**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real `map_property` directly.
"""

from __future__ import annotations

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
# Generators: writable properties over the mapped GType set
#
# Each strategy yields (prop, expected_param_type, expected_default) where
# expected_default is:
#   ('known', value)  - the generator constructed a definitely-usable (or
#                       definitely-absent, value=None) default, so the exact
#                       carried default is asserted (Requirement 2.3);
#   ('unknown',)      - the default was drawn arbitrarily (possibly
#                       unconvertible or out of range), so only the typed
#                       correctness of whatever default appears is asserted.
# ---------------------------------------------------------------------------

# All GTypes the scalar rows of the mapping table claim (Requirement 2.1);
# generated GEnum type names must avoid these so the enum row is exercised.
_SCALAR_GTYPES = frozenset(GTYPE_INT) | frozenset(GTYPE_FLOAT) | {GTYPE_BOOL, GTYPE_STRING}

_names = st.text(min_size=1, max_size=30)
_optional_blurbs = st.one_of(st.none(), st.text(max_size=60))

_finite_floats = st.floats(allow_nan=False, allow_infinity=False)

# Arbitrary JSON-scalar-or-null defaults (may be unusable for the paramType).
_arbitrary_defaults = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    _finite_floats,
    st.text(max_size=20),
)


@st.composite
def _ranged_numeric_cases(draw, gtypes, bound_values, usable_defaults):
    """Shared shape for the int and float rows: random one- or two-sided
    ranges (min <= max when both present) and none/usable/arbitrary
    defaults."""
    gtype = draw(gtypes)
    lo_full, hi_full = sorted((draw(bound_values), draw(bound_values)))
    lo = lo_full if draw(st.booleans()) else None
    hi = hi_full if draw(st.booleans()) else None

    mode = draw(st.sampled_from(('none', 'usable', 'arbitrary')))
    if mode == 'none':
        default, expected = None, ('known', None)
    elif mode == 'usable':
        # Drawn within [lo_full, hi_full], so it satisfies whichever bounds
        # survived above.
        default = draw(usable_defaults(lo_full, hi_full))
        expected = ('known', default)
    else:
        default, expected = draw(_arbitrary_defaults), ('unknown',)

    prop = GstProperty(name=draw(_names), gtype=gtype, owner=draw(_names),
                       writable=True, blurb=draw(_optional_blurbs),
                       default=default, min=lo, max=hi, enum_values=None)
    return prop, expected


_int_cases = _ranged_numeric_cases(
    gtypes=st.sampled_from(sorted(GTYPE_INT)),
    bound_values=st.integers(min_value=-10**12, max_value=10**12),
    usable_defaults=lambda lo, hi: st.integers(min_value=lo, max_value=hi),
)

_float_cases = _ranged_numeric_cases(
    gtypes=st.sampled_from(sorted(GTYPE_FLOAT)),
    bound_values=st.floats(min_value=-1e12, max_value=1e12,
                           allow_nan=False, allow_infinity=False),
    usable_defaults=lambda lo, hi: st.floats(min_value=lo, max_value=hi,
                                             allow_nan=False, allow_infinity=False),
)


@st.composite
def _bool_cases(draw):
    mode = draw(st.sampled_from(('none', 'usable', 'arbitrary')))
    if mode == 'none':
        default, expected = None, ('known', None)
    elif mode == 'usable':
        default = draw(st.booleans())
        expected = ('known', default)
    else:
        default, expected = draw(_arbitrary_defaults), ('unknown',)
    prop = GstProperty(name=draw(_names), gtype=GTYPE_BOOL, owner=draw(_names),
                       writable=True, blurb=draw(_optional_blurbs),
                       default=default, min=None, max=None, enum_values=None)
    return prop, expected


@st.composite
def _string_cases(draw):
    mode = draw(st.sampled_from(('none', 'usable', 'arbitrary')))
    if mode == 'none':
        default, expected = None, ('known', None)
    elif mode == 'usable':
        # Usable string default: non-NULL, non-empty after strip (3.1).
        default = draw(st.text(min_size=1, max_size=30).filter(lambda s: s.strip()))
        expected = ('known', default)
    else:
        default, expected = draw(_arbitrary_defaults), ('unknown',)
    prop = GstProperty(name=draw(_names), gtype=GTYPE_STRING, owner=draw(_names),
                       writable=True, blurb=draw(_optional_blurbs),
                       default=default, min=None, max=None, enum_values=None)
    return prop, expected


@st.composite
def _enum_cases(draw):
    # A GEnum type name never collides with the scalar mapping rows.
    gtype = draw(_names.filter(lambda g: g not in _SCALAR_GTYPES))
    pairs = draw(st.lists(
        st.tuples(st.integers(), st.text(min_size=1, max_size=20)),
        min_size=1, max_size=6,
        unique_by=(lambda p: p[0], lambda p: p[1]),
    ))
    enum_values = [EnumValue(value=v, nick=n) for v, n in pairs]

    mode = draw(st.sampled_from(('none', 'usable_nick', 'usable_value', 'arbitrary')))
    if mode == 'none':
        default, expected = None, ('known', None)
    elif mode == 'usable_nick':
        entry = draw(st.sampled_from(enum_values))
        default, expected = entry.nick, ('known', entry.nick)
    elif mode == 'usable_value':
        entry = draw(st.sampled_from(enum_values))
        default, expected = entry.value, ('known', entry.nick)
    else:
        default, expected = draw(_arbitrary_defaults), ('unknown',)

    prop = GstProperty(name=draw(_names), gtype=gtype, owner=draw(_names),
                       writable=True, blurb=draw(_optional_blurbs),
                       default=default, min=None, max=None,
                       enum_values=enum_values)
    return prop, expected


_mapped_cases = st.one_of(
    st.tuples(_int_cases, st.just('int')),
    st.tuples(_float_cases, st.just('float')),
    st.tuples(_bool_cases(), st.just('bool')),
    st.tuples(_string_cases(), st.just('string')),
    st.tuples(_enum_cases(), st.just('enum')),
)


def _assert_default_typed_correctly(suggestion, prop, param_type):
    """Whatever default the suggestion carries must be the property's
    declared default converted to the mapped paramType (Requirement 2.3)."""
    default = suggestion['default']
    if param_type == 'int':
        assert isinstance(default, int) and not isinstance(default, bool)
        assert default == prop.default  # numeric equality (e.g. 5 == 5.0)
        if prop.min is not None:
            assert default >= prop.min
        if prop.max is not None:
            assert default <= prop.max
    elif param_type == 'float':
        assert isinstance(default, float)
        assert default == prop.default
        if prop.min is not None:
            assert default >= prop.min
        if prop.max is not None:
            assert default <= prop.max
    elif param_type == 'bool':
        assert isinstance(default, bool)
        assert default is prop.default
    elif param_type == 'string':
        assert isinstance(default, str) and default.strip()
        assert default == prop.default
    else:  # enum: the carried default is a nick resolved from the declared one
        nicks = [ev.nick for ev in prop.enum_values]
        assert default in nicks
        assert any(ev.nick == default and prop.default in (ev.nick, ev.value)
                   for ev in prop.enum_values)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(case=_mapped_cases)
def test_mapping_is_total_and_correctly_typed_over_writable_known_gtypes(case):
    """**Feature: gst-parameter-prepopulation, Property 3: Type mapping is total and correctly typed over writable known GTypes**

    For any writable property with a mapped GType, `map_property` yields a
    Parameter_Suggestion with the paramType of the mapping table row (2.1),
    constraints carrying the declared min/max range or the enum nicks
    (2.2), and a default that, when present, is the declared default
    converted to the mapped paramType (2.3).

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    (prop, expected_default), expected_type = case

    result = map_property(prop)

    # Total over writable known GTypes: always a suggestion, never Skipped.
    assert not isinstance(result, Skipped)
    assert isinstance(result, dict)
    assert result['name'] == prop.name

    # 2.1: paramType per the mapping table row.
    assert result['paramType'] == expected_type

    # 2.2: constraints carry the declared range for ranged numerics and the
    # nick list for GEnums; the other rows carry no constraints.
    if expected_type in ('int', 'float'):
        expected_constraints = {}
        if prop.min is not None:
            expected_constraints['min'] = prop.min
        if prop.max is not None:
            expected_constraints['max'] = prop.max
        if expected_constraints:
            assert result['constraints'] == expected_constraints
        else:
            assert 'constraints' not in result
    elif expected_type == 'enum':
        assert result['constraints'] == {'values': [ev.nick for ev in prop.enum_values]}
    else:
        assert 'constraints' not in result

    # 2.3: default conversion. A definitely-usable generated default is
    # carried (converted for enum value defaults); an absent default stays
    # absent; any carried default is typed for the mapped paramType and is
    # used as the example value.
    if expected_default[0] == 'known':
        if expected_default[1] is None:
            assert 'default' not in result
        else:
            assert result['default'] == expected_default[1]
    if 'default' in result:
        _assert_default_typed_correctly(result, prop, expected_type)
        assert result['examples'] == [result['default']]
    assert result['required'] == ('default' not in result)
