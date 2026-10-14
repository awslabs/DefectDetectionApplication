"""Property test: every Parameter_Suggestion passes declaration validation (task 1.9).

**Feature: gst-parameter-prepopulation, Property 5: Every suggestion passes declaration validation**

For any generated GStreamer_Property that maps to a Parameter_Suggestion,
the suggestion satisfies the declaration validation rules: non-empty name,
non-empty description (blurb or synthesized fallback), at least one example
value valid for the paramType, non-empty `values` for enum, and min <= max
when both are present.

The cross-check is against `workflow_core.catalog.custom`'s REAL parameter
validation (not a re-implementation): each suggestion is embedded as the
sole parameter of a minimal Custom_Node_Type declaration and fed through
`descriptor_from_declaration`, which enforces exactly the rules above —
non-empty name/description, constraint satisfiability (min <= max,
non-empty enum `values`), at least one example, and defaults/examples
checked against the parameter's own type and constraints via
`check_parameter_value`. Acceptance (no `DeclarationError`) is the
property.

**Validates: Requirements 2.4, 2.6**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures; `workflow_core` is on sys.path via the layer path conftest adds.
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
from workflow_core.catalog.custom import DeclarationError, descriptor_from_declaration
from workflow_core.catalog.models import CATEGORIES

# ---------------------------------------------------------------------------
# Generators: writable properties over the mapped GType set.
#
# Names are realistic GStreamer property names (launch-safe identifiers,
# never blank) — GObject property names cannot be empty or whitespace, so
# generating blank names would leave the realistic input space. Defaults
# are boundary-skewed (absent, blank strings, wrong-typed, out-of-range)
# so both branches of Requirement 2.6 are exercised: carried defaults used
# as examples, and synthesized examples for required suggestions.
# ---------------------------------------------------------------------------

# GType names claimed by the scalar mapping rows; generated GEnum type names
# must avoid these so the enum row is the one exercised.
_SCALAR_GTYPES = frozenset(GTYPE_INT) | frozenset(GTYPE_FLOAT) | {GTYPE_BOOL, GTYPE_STRING}

# GObject-style property names: non-empty, letter first, dashes allowed.
_property_names = st.from_regex(r'[a-z][a-z0-9-]{0,24}', fullmatch=True)

# Blurbs include None, blank, and whitespace-only so the synthesized
# description fallback path (Requirement 2.4) is exercised.
_optional_blurbs = st.one_of(
    st.none(),
    st.just(''),
    st.text(alphabet=' \t\n', min_size=1, max_size=4),
    st.text(min_size=1, max_size=60),
)

# The full default palette: null, blank-ish strings, and every JSON scalar
# shape — deliberately including wrong-typed values for each paramType, so
# many generated properties come out required with synthesized examples.
_boundary_defaults = st.one_of(
    st.none(),
    st.just(''),
    st.text(alphabet=' \t\n', min_size=1, max_size=4),
    st.booleans(),
    st.integers(min_value=-10**13, max_value=10**13),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)


@st.composite
def _numeric_props(draw, gtypes, bound_values):
    """A writable int/float-row property with a random one-/two-sided range
    (min <= max when both survive) and a boundary-skewed default."""
    lo, hi = sorted((draw(bound_values), draw(bound_values)))
    return GstProperty(
        name=draw(_property_names),
        gtype=draw(gtypes),
        owner=draw(_property_names),
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
        name=draw(_property_names),
        gtype=gtype,
        owner=draw(_property_names),
        writable=True,
        blurb=draw(_optional_blurbs),
        default=draw(_boundary_defaults),
        min=None,
        max=None,
        enum_values=None,
    )


@st.composite
def _enum_props(draw):
    """A writable GEnum-shaped property with at least one enum value.
    Defaults mix matching nicks, matching values, and boundary misses."""
    gtype = draw(st.from_regex(r'Gst[A-Z][a-zA-Z]{0,20}', fullmatch=True)
                 .filter(lambda g: g not in _SCALAR_GTYPES))
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

    return GstProperty(name=draw(_property_names), gtype=gtype,
                       owner=draw(_property_names), writable=True,
                       blurb=draw(_optional_blurbs),
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
# The real validator: a suggestion is valid iff a minimal Custom_Node_Type
# declaration carrying it as its sole parameter converts without error.
# ---------------------------------------------------------------------------

def _declaration_wrapping(suggestion):
    """Embed one Parameter_Suggestion in a minimal, otherwise-valid
    Custom_Node_Type declaration in the node-catalog wire shape."""
    return {
        'typeId': 'custom.gst_suggestion_check',
        'displayName': 'GST suggestion check',
        'category': CATEGORIES[0],
        'inputs': [],
        'outputs': [],
        'parameters': [suggestion],
        'mappings': [],
    }


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(prop=_mappable_props)
def test_every_suggestion_passes_declaration_validation(prop):
    """**Feature: gst-parameter-prepopulation, Property 5: Every suggestion passes declaration validation**

    For any writable property with a mapped GType, the produced
    Parameter_Suggestion satisfies the declaration validation rules —
    non-empty name, non-empty description (2.4), at least one example
    valid for the paramType (2.6), non-empty `values` for enum, and
    min <= max when both present — as judged by
    `workflow_core.catalog.custom`'s real parameter validation
    (`descriptor_from_declaration`), not a re-implementation.

    **Validates: Requirements 2.4, 2.6**
    """
    result = map_property(prop)

    # Mappable, writable input: a suggestion is produced, never Skipped.
    assert not isinstance(result, Skipped)
    assert isinstance(result, dict)

    # The stated invariants, asserted directly for diagnosability.
    assert isinstance(result['name'], str) and result['name'].strip()
    assert isinstance(result['description'], str) and result['description'].strip()
    assert isinstance(result['examples'], list) and len(result['examples']) >= 1
    assert all(example is not None for example in result['examples'])
    constraints = result.get('constraints', {})
    if result['paramType'] == 'enum':
        assert constraints.get('values'), 'enum suggestions must carry non-empty values'
    if 'min' in constraints and 'max' in constraints:
        assert constraints['min'] <= constraints['max']

    # The cross-check: workflow_core.catalog.custom's real validator
    # accepts the suggestion, including check_parameter_value on the
    # default and every example.
    try:
        descriptor = descriptor_from_declaration(_declaration_wrapping(result))
    except DeclarationError as error:
        raise AssertionError(
            f'suggestion rejected by catalog.custom declaration validation: '
            f'{error} (suggestion={result!r}, property={prop!r})'
        ) from error

    # The descriptor round-trips the suggestion's identity fields.
    (parameter,) = descriptor.parameters
    assert parameter.name == result['name']
    assert parameter.param_type == result['paramType']
    assert parameter.required == result['required']
