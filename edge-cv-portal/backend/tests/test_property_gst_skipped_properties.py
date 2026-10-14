"""Property test for skipped properties in the Type_Mapping (task 1.6).

**Feature: gst-parameter-prepopulation, Property 4: Unknown or non-writable properties are always skipped with a reason**

For any generated GStreamer_Property that has an unmapped GType or is not
writable, `map_property` yields a skipped entry carrying the property name
and a non-empty reason, and yields no Parameter_Suggestion.

Three skip causes per `map_property` (Requirement 2.5):
  - the property is not writable (regardless of GType);
  - the GType has no mapping row: not an int/float GType, not gboolean or
    gchararray, and no enum values are declared;
  - a GEnum-shaped property whose enum value list is empty (nothing to
    offer as allowed values).

**Validates: Requirements 2.5**

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
# Generators
# ---------------------------------------------------------------------------

# All GTypes the mapping table can convert via its scalar rows; a writable
# property with a non-empty enum value list also maps (to enum) regardless
# of its GType name, so "unmapped" additionally requires no enum values.
_SCALAR_GTYPES = frozenset(GTYPE_INT) | frozenset(GTYPE_FLOAT) | {GTYPE_BOOL, GTYPE_STRING}

_names = st.text(min_size=1, max_size=30)
_optional_blurbs = st.one_of(st.none(), st.text(max_size=60))

# Arbitrary JSON-scalar-or-null defaults; skipping must not depend on them.
_arbitrary_defaults = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)

# Realistic unmapped GType names from Requirement 2.5's examples, mixed with
# arbitrary generated type names that avoid the scalar mapping rows.
_KNOWN_UNMAPPED = ('GstCaps', 'GstStructure', 'GstFraction', 'GstBuffer',
                   'GObject', 'GstObject', 'gpointer', 'GValueArray')
_unmapped_gtypes = st.one_of(
    st.sampled_from(_KNOWN_UNMAPPED),
    _names.filter(lambda g: g not in _SCALAR_GTYPES),
)

# Any GType at all: mapped scalar rows, known-unmapped, or arbitrary.
_any_gtypes = st.one_of(
    st.sampled_from(sorted(_SCALAR_GTYPES)),
    st.sampled_from(_KNOWN_UNMAPPED),
    _names,
)

_optional_enum_values = st.one_of(
    st.none(),
    st.lists(
        st.builds(EnumValue,
                  value=st.integers(),
                  nick=st.text(min_size=1, max_size=20)),
        max_size=4,
    ),
)


@st.composite
def _optional_range(draw):
    """A random one-/two-sided numeric range (or none at all)."""
    lo, hi = sorted((draw(st.integers(min_value=-10**9, max_value=10**9)),
                     draw(st.integers(min_value=-10**9, max_value=10**9))))
    return (lo if draw(st.booleans()) else None,
            hi if draw(st.booleans()) else None)


@st.composite
def _non_writable_props(draw):
    """Not writable: skipped regardless of GType, enum values, or default."""
    lo, hi = draw(_optional_range())
    return GstProperty(
        name=draw(_names),
        gtype=draw(_any_gtypes),
        owner=draw(_names),
        writable=False,
        blurb=draw(_optional_blurbs),
        default=draw(_arbitrary_defaults),
        min=lo,
        max=hi,
        enum_values=draw(_optional_enum_values),
    )


@st.composite
def _unmapped_writable_props(draw):
    """Writable, but the GType has no mapping row and no enum values."""
    lo, hi = draw(_optional_range())
    return GstProperty(
        name=draw(_names),
        gtype=draw(_unmapped_gtypes),
        owner=draw(_names),
        writable=True,
        blurb=draw(_optional_blurbs),
        default=draw(_arbitrary_defaults),
        min=lo,
        max=hi,
        enum_values=None,
    )


@st.composite
def _empty_enum_writable_props(draw):
    """Writable GEnum-shaped property declaring an empty enum value list."""
    return GstProperty(
        name=draw(_names),
        gtype=draw(_unmapped_gtypes),
        owner=draw(_names),
        writable=True,
        blurb=draw(_optional_blurbs),
        default=draw(_arbitrary_defaults),
        min=None,
        max=None,
        enum_values=[],
    )


_skippable_props = st.one_of(
    _non_writable_props(),
    _unmapped_writable_props(),
    _empty_enum_writable_props(),
)


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(prop=_skippable_props)
def test_unmapped_or_non_writable_properties_are_skipped_with_a_reason(prop):
    """**Feature: gst-parameter-prepopulation, Property 4: Unknown or non-writable properties are always skipped with a reason**

    For any property with an unmapped GType or `writable == False`,
    `map_property` returns a Skipped entry (never a Parameter_Suggestion)
    carrying the property's name and a non-empty reason (Requirement 2.5).

    **Validates: Requirements 2.5**
    """
    result = map_property(prop)

    # No Parameter_Suggestion is ever produced for a skippable property.
    assert isinstance(result, Skipped)
    assert not isinstance(result, dict)

    # The skipped entry carries the property name and a non-empty reason.
    assert result.name == prop.name
    assert isinstance(result.reason, str)
    assert result.reason.strip()
