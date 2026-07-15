"""Property test for Base_Class_Property filtering (task 1.10).

**Feature: gst-parameter-prepopulation, Property 7: Base-class filtering keeps exactly the element's own properties**

For any generated element with a mix of properties whose `owner` equals or
differs from the element's GType, `suggestions_for_element` includes every
writable mappable property owned by the element's own GType (including
names that shadow base-class names) and none owned by a different GType.

Base-class properties are excluded entirely: they appear neither in the
suggestions nor in the skipped list (Requirement 4.1). A property whose
owner IS the element's own GType is kept even when a base class declares a
property of the same name — ownership, not name, decides (Requirement 4.2).

**Validates: Requirements 4.1, 4.2**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real `suggestions_for_element`
directly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    GTYPE_BOOL,
    GTYPE_FLOAT,
    GTYPE_INT,
    GTYPE_STRING,
    EnumValue,
    GstProperty,
    ReportElement,
    suggestions_for_element,
)

# ---------------------------------------------------------------------------
# Generators: elements mixing own-owner and base-owner properties, with
# shadowed names (the same property name declared by both the element's own
# GType and a base GType) so Requirement 4.2's ownership-decides rule is
# exercised, plus non-writable and unmapped-GType properties so the kept
# own properties split across suggestions and skipped.
# ---------------------------------------------------------------------------

# GType names claimed by the scalar mapping rows; generated GEnum/unmapped
# type names must avoid these so the intended mapping row is exercised.
_SCALAR_GTYPES = frozenset(GTYPE_INT) | frozenset(GTYPE_FLOAT) | {GTYPE_BOOL, GTYPE_STRING}

# GObject-style class/property names.
_gtype_names = st.from_regex(r'Gst[A-Z][a-zA-Z]{0,20}', fullmatch=True)
_property_names = st.from_regex(r'[a-z][a-z0-9-]{0,24}', fullmatch=True)

_optional_blurbs = st.one_of(st.none(), st.text(max_size=60))

# Defaults do not affect filtering; a small boundary-skewed palette keeps
# both required and optional suggestions in play.
_defaults = st.one_of(
    st.none(),
    st.just(''),
    st.booleans(),
    st.integers(min_value=-10**9, max_value=10**9),
    st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)


@st.composite
def _gst_props(draw, owner: str) -> GstProperty:
    """One property owned by `owner`, drawn across every mapping outcome:
    the five mapped rows, an unmapped GType, an empty GEnum, and both
    writable states."""
    kind = draw(st.sampled_from(
        ['int', 'float', 'bool', 'string', 'enum', 'empty-enum', 'unmapped']))
    writable = draw(st.booleans())
    name = draw(_property_names)
    blurb = draw(_optional_blurbs)
    default = draw(_defaults)

    if kind == 'int':
        lo, hi = sorted((draw(st.integers(-10**9, 10**9)),
                         draw(st.integers(-10**9, 10**9))))
        return GstProperty(name=name, gtype=draw(st.sampled_from(sorted(GTYPE_INT))),
                           owner=owner, writable=writable, blurb=blurb, default=default,
                           min=lo if draw(st.booleans()) else None,
                           max=hi if draw(st.booleans()) else None)
    if kind == 'float':
        lo, hi = sorted((draw(st.floats(-1e9, 1e9, allow_nan=False)),
                         draw(st.floats(-1e9, 1e9, allow_nan=False))))
        return GstProperty(name=name, gtype=draw(st.sampled_from(sorted(GTYPE_FLOAT))),
                           owner=owner, writable=writable, blurb=blurb, default=default,
                           min=lo if draw(st.booleans()) else None,
                           max=hi if draw(st.booleans()) else None)
    if kind == 'bool':
        return GstProperty(name=name, gtype=GTYPE_BOOL, owner=owner,
                           writable=writable, blurb=blurb, default=default)
    if kind == 'string':
        return GstProperty(name=name, gtype=GTYPE_STRING, owner=owner,
                           writable=writable, blurb=blurb, default=default)
    if kind == 'enum':
        pairs = draw(st.lists(
            st.tuples(st.integers(), st.text(min_size=1, max_size=20)),
            min_size=1, max_size=4,
            unique_by=(lambda p: p[0], lambda p: p[1]),
        ))
        return GstProperty(name=name,
                           gtype=draw(_gtype_names.filter(lambda g: g not in _SCALAR_GTYPES)),
                           owner=owner, writable=writable, blurb=blurb, default=default,
                           enum_values=[EnumValue(value=v, nick=n) for v, n in pairs])
    if kind == 'empty-enum':
        return GstProperty(name=name,
                           gtype=draw(_gtype_names.filter(lambda g: g not in _SCALAR_GTYPES)),
                           owner=owner, writable=writable, blurb=blurb, default=default,
                           enum_values=[])
    # unmapped: a GType outside the mapping table, no enum values.
    return GstProperty(name=name,
                       gtype=draw(_gtype_names.filter(lambda g: g not in _SCALAR_GTYPES)),
                       owner=owner, writable=writable, blurb=blurb, default=default)


@st.composite
def _elements(draw) -> ReportElement:
    """An element with own-owner properties, base-owner properties, and
    shadowed names in both directions: base copies of own names (the
    subclass overrides a base property — the own pspec must be kept) and
    the plain base properties a real element always carries."""
    element_gtype = draw(_gtype_names)
    base_gtypes = draw(st.lists(
        _gtype_names.filter(lambda g: g != element_gtype),
        min_size=1, max_size=3, unique=True))
    base_owner = st.sampled_from(base_gtypes)

    own_props = draw(st.lists(_gst_props(owner=element_gtype), max_size=6))
    base_props = draw(st.lists(_gst_props(owner=draw(base_owner)), max_size=6))

    # Shadowed names: re-declare a subset of own property names on a base
    # GType (and vice versa), so name collisions across owners exist.
    shadows: List[GstProperty] = [
        replace(prop, owner=draw(base_owner))
        for prop in own_props if draw(st.booleans())
    ]
    shadows += [
        replace(prop, owner=element_gtype)
        for prop in base_props if draw(st.booleans())
    ]

    properties = draw(st.permutations(own_props + base_props + shadows))
    return ReportElement(
        factory=draw(_property_names),
        element_gtype=element_gtype,
        instantiation_error=None,
        properties=list(properties),
    )


# ---------------------------------------------------------------------------
# Oracle: mappability restated independently of map_property — a property
# yields a suggestion iff it is writable and its GType is in the mapping
# table (int/float/bool/string rows) or it is a GEnum with at least one
# enum value; every other own property lands in skipped.
# ---------------------------------------------------------------------------

def _is_mappable(prop: GstProperty) -> bool:
    if not prop.writable:
        return False
    if prop.gtype in GTYPE_INT or prop.gtype in GTYPE_FLOAT:
        return True
    if prop.gtype in (GTYPE_BOOL, GTYPE_STRING):
        return True
    return bool(prop.enum_values)


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(element=_elements())
def test_base_class_filtering_keeps_exactly_the_elements_own_properties(element):
    """**Feature: gst-parameter-prepopulation, Property 7: Base-class filtering keeps exactly the element's own properties**

    The derived suggestions include every writable mappable property whose
    owner is the element's own GType — including names shadowing base-class
    names (4.2) — and no property owned by a different GType appears in
    either the suggestions or the skipped list (4.1). Own-property order is
    preserved, and unmappable/non-writable own properties land in skipped.

    **Validates: Requirements 4.1, 4.2**
    """
    result = suggestions_for_element(element)

    own_props = [prop for prop in element.properties
                 if prop.owner == element.element_gtype]

    # Every writable mappable own property yields a suggestion, in order —
    # this includes own properties whose names shadow base-class names (4.2).
    expected_suggestions = [prop.name for prop in own_props if _is_mappable(prop)]
    assert [s['name'] for s in result['suggestions']] == expected_suggestions

    # Every remaining own property (non-writable or unmapped GType) lands
    # in skipped with its name, in order.
    expected_skipped = [prop.name for prop in own_props if not _is_mappable(prop)]
    assert [s['name'] for s in result['skipped']] == expected_skipped

    # Nothing owned by a different GType appears anywhere in the result:
    # the output is exactly one entry per own property (4.1) — base-class
    # properties are excluded entirely, not skipped-with-reason.
    assert len(result['suggestions']) + len(result['skipped']) == len(own_props)
