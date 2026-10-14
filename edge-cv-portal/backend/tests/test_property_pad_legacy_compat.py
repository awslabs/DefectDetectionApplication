"""Property test for legacy report compatibility (task 1.3).

**Feature: port-guidance-and-pad-prepopulation, Property 2: Legacy reports parse compatibly**

For any valid Introspection_Report, serializing it and then deleting the
`pads` and `padsError` keys from every element (producing exactly the
pre-feature version-1 document shape) parses successfully to a report in
which every element has `pads=None` and `pads_error=None` while every
other report-level, element-level, and property field equals the
original.

**Validates: Requirements 4.2**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and
parse/serialize functions directly.
"""

from __future__ import annotations

import dataclasses
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    MAX_CAPS_LEN,
    STATUS_CAPTURED,
    STATUS_FAILED,
    VALID_PAD_DIRECTIONS,
    VALID_PAD_PRESENCES,
    EnumValue,
    GstProperty,
    PadTemplate,
    Report,
    ReportElement,
    parse_report,
    serialize_report,
)

# ---------------------------------------------------------------------------
# Generators: valid Report dataclass instances, extended with pad data
# (mirroring test_property_gst_report_roundtrip.py)
# ---------------------------------------------------------------------------

# Finite floats only: NaN never compares equal to itself and infinities are
# not interchange-safe JSON, so neither can appear in a stored report.
_finite_floats = st.floats(allow_nan=False, allow_infinity=False)

# JSON scalar | None for property defaults.
_optional_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    _finite_floats,
    st.text(max_size=40),
)

# int | float | None for ranged numeric min/max.
_optional_numbers = st.one_of(st.none(), st.integers(), _finite_floats)

_optional_texts = st.one_of(st.none(), st.text(max_size=60))

_enum_values = st.builds(
    EnumValue,
    value=st.integers(),
    nick=st.text(max_size=30),
)

_properties = st.builds(
    GstProperty,
    name=st.text(max_size=40),
    gtype=st.text(max_size=40),
    owner=st.text(max_size=40),
    writable=st.booleans(),
    blurb=_optional_texts,
    default=_optional_scalars,
    min=_optional_numbers,
    max=_optional_numbers,
    enum_values=st.one_of(st.none(), st.lists(_enum_values, max_size=6)),
)

# Valid pad templates: directions/presences from the valid vocabularies,
# caps at most MAX_CAPS_LEN characters (including the exact boundary).
_pad_templates = st.builds(
    PadTemplate,
    name=st.text(max_size=40),
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.sampled_from(VALID_PAD_PRESENCES),
    caps=st.one_of(
        st.text(max_size=80),
        # Exercise the boundary: caps of exactly MAX_CAPS_LEN characters.
        st.just('x' * MAX_CAPS_LEN),
    ),
    caps_truncated=st.booleans(),
)

# Every valid (pads, pads_error) element state, respecting the domain
# invariant that pads_error is non-None only when pads == []:
#   - pads=None (legacy element, never captured)
#   - pads=[] without an error (element declares no templates)
#   - pads=[] with a diagnostic (per-element read failure)
#   - a populated pad list (error always None)
_pad_states = st.one_of(
    st.tuples(st.none(), st.none()),
    st.tuples(st.just(()), st.none()),
    st.tuples(st.just(()), st.text(min_size=1, max_size=60)),
    st.tuples(st.lists(_pad_templates, min_size=1, max_size=6), st.none()),
)


def _build_element(factory, element_gtype, instantiation_error, properties,
                   pad_state):
    pads, pads_error = pad_state
    return ReportElement(
        factory=factory,
        element_gtype=element_gtype,
        instantiation_error=instantiation_error,
        properties=properties,
        pads=None if pads is None else list(pads),
        pads_error=pads_error,
    )


_elements = st.builds(
    _build_element,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=_optional_texts,
    properties=st.lists(_properties, max_size=6),
    pad_state=_pad_states,
)

_reports = st.builds(
    Report,
    status=st.sampled_from((STATUS_CAPTURED, STATUS_FAILED)),
    message=_optional_texts,
    gst_version=_optional_texts,
    captured_at=_optional_texts,
    elements=st.lists(_elements, max_size=5),
)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(report=_reports)
def test_legacy_reports_parse_compatibly(report):
    """**Feature: port-guidance-and-pad-prepopulation, Property 2: Legacy reports parse compatibly**

    Serializing any valid report and deleting the `pads`/`padsError` keys
    from every element yields exactly the pre-feature version-1 document
    shape; parsing it succeeds, every element comes back with `pads=None`
    and `pads_error=None`, and every other report-level, element-level,
    and property field equals the original (Requirement 4.2).

    **Validates: Requirements 4.2**
    """
    document = serialize_report(report)

    # Produce exactly the pre-feature stored document shape: no element
    # carries a `pads` or `padsError` key. Run it through a real JSON
    # cycle, as a stored legacy report would be.
    for element_doc in document['elements']:
        element_doc.pop('pads', None)
        element_doc.pop('padsError', None)
    legacy_document = json.loads(json.dumps(document))

    parsed = parse_report(legacy_document)

    # Every element reports pads as never captured (legacy defaults).
    for element in parsed.elements:
        assert element.pads is None
        assert element.pads_error is None

    # All other report-level, element-level, and property fields are
    # field-for-field identical to the original report: the parse result
    # equals the original with only the pad fields reset.
    expected = dataclasses.replace(report, elements=[
        dataclasses.replace(element, pads=None, pads_error=None)
        for element in report.elements
    ])
    assert parsed == expected
