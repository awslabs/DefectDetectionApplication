"""Property test for malformed Pad_Template rejection (task 1.4).

**Feature: port-guidance-and-pad-prepopulation, Property 3: Malformed pad data is rejected, not crashed on**

For any valid pad-bearing Introspection_Report document broken by a single
targeted pad mutation — a `pads` value that is not a list, a pad entry that
is not an object, a dropped or mistyped pad field, a `direction` outside
{sink, src}, a `presence` outside {always, sometimes, request}, or a `caps`
string longer than 4096 characters — `parse_report` raises the typed
`ReportError` and nothing else, so the route maps it to the existing
"introspection_failed" unavailability reason instead of an internal error.

**Validates: Requirements 4.4**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real parse function directly.
Mirrors the targeted-mutation approach of
`test_property_gst_report_rejection.py`.
"""

from __future__ import annotations

import pytest
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
    ReportError,
    parse_report,
    serialize_report,
)

# ---------------------------------------------------------------------------
# Generators: valid pad-bearing Report dataclass instances
# ---------------------------------------------------------------------------

# Finite floats only: NaN/inf are not interchange-safe JSON and cannot
# appear in a stored report.
_finite_floats = st.floats(allow_nan=False, allow_infinity=False)

_optional_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    _finite_floats,
    st.text(max_size=40),
)

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

_pad_templates = st.builds(
    PadTemplate,
    name=st.text(max_size=40),
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.sampled_from(VALID_PAD_PRESENCES),
    caps=st.text(max_size=80),
    caps_truncated=st.booleans(),
)

# Pad states respecting the domain invariant: pads_error is non-None only
# when pads == [] (per-element read failure).
_pad_states = st.one_of(
    st.just((None, None)),                                  # legacy: not captured
    st.tuples(st.just([]),                                  # empty list ± error
              st.one_of(st.none(), st.text(min_size=1, max_size=60))),
    st.tuples(st.lists(_pad_templates, min_size=1, max_size=4),
              st.none()),                                   # populated
)


def _element(factory, element_gtype, instantiation_error, properties, pad_state):
    pads, pads_error = pad_state
    return ReportElement(
        factory=factory,
        element_gtype=element_gtype,
        instantiation_error=instantiation_error,
        properties=properties,
        pads=pads,
        pads_error=pads_error,
    )


_elements = st.builds(
    _element,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=_optional_texts,
    properties=st.lists(_properties, max_size=4),
    pad_state=_pad_states,
)

# An element guaranteed to carry a non-empty pad list, so every mutation
# kind below always has a target.
_pad_bearing_elements = st.builds(
    _element,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=_optional_texts,
    properties=st.lists(_properties, max_size=4),
    pad_state=st.tuples(st.lists(_pad_templates, min_size=1, max_size=4),
                        st.none()),
)


# ---------------------------------------------------------------------------
# Generators: targeted pad mutations of valid serialized reports
# ---------------------------------------------------------------------------

# A JSON object is not accepted for any pad field (name str, direction str,
# presence str, caps str, capsTruncated bool), so clobbering any field with
# an object guarantees a mistype.
_CLOBBER = {'not': 'a valid pad field value'}

_PAD_FIELD_KEYS = ('name', 'direction', 'presence', 'caps', 'capsTruncated')

# `pads` values that are not a list.
_nonlist_pads = st.sampled_from([
    None, True, 0, 1.5, 'sink', {'name': 'sink'},
])

# Pad entries that are not an object.
_nonobject_entries = st.sampled_from([
    None, True, 7, 2.5, 'sink', ['sink'],
])

_bad_directions = st.one_of(
    st.text(max_size=20).filter(lambda s: s not in VALID_PAD_DIRECTIONS),
    st.sampled_from(['SINK', 'SRC', 'Sink', 'source', 'input', 'output']),
)

_bad_presences = st.one_of(
    st.text(max_size=20).filter(lambda s: s not in VALID_PAD_PRESENCES),
    st.sampled_from(['ALWAYS', 'Always', 'never', 'on-request', 'dynamic']),
)

# Caps strings strictly longer than MAX_CAPS_LEN characters.
_overlong_caps = st.text(min_size=1, max_size=50).map(
    lambda suffix: 'x' * MAX_CAPS_LEN + suffix
)


@st.composite
def _mutated_pad_documents(draw):
    """A valid pad-bearing serialized report broken by exactly one
    targeted pad mutation."""
    elements = draw(st.lists(_elements, max_size=3))
    target = draw(_pad_bearing_elements)
    insert_at = draw(st.integers(min_value=0, max_value=len(elements)))
    elements = elements[:insert_at] + [target] + elements[insert_at:]

    report = Report(
        status=draw(st.sampled_from((STATUS_CAPTURED, STATUS_FAILED))),
        message=draw(_optional_texts),
        gst_version=draw(_optional_texts),
        captured_at=draw(_optional_texts),
        elements=elements,
    )
    document = serialize_report(report)

    element = document['elements'][insert_at]
    pad_indices = list(range(len(element['pads'])))

    kind = draw(st.sampled_from([
        'nonlist_pads',
        'nonobject_entry',
        'drop_pad_field',
        'mistype_pad_field',
        'bad_direction',
        'bad_presence',
        'overlong_caps',
    ]))

    if kind == 'nonlist_pads':
        element['pads'] = draw(_nonlist_pads)
    elif kind == 'nonobject_entry':
        element['pads'][draw(st.sampled_from(pad_indices))] = draw(_nonobject_entries)
    elif kind == 'drop_pad_field':
        pad = element['pads'][draw(st.sampled_from(pad_indices))]
        del pad[draw(st.sampled_from(_PAD_FIELD_KEYS))]
    elif kind == 'mistype_pad_field':
        pad = element['pads'][draw(st.sampled_from(pad_indices))]
        pad[draw(st.sampled_from(_PAD_FIELD_KEYS))] = _CLOBBER
    elif kind == 'bad_direction':
        pad = element['pads'][draw(st.sampled_from(pad_indices))]
        pad['direction'] = draw(_bad_directions)
    elif kind == 'bad_presence':
        pad = element['pads'][draw(st.sampled_from(pad_indices))]
        pad['presence'] = draw(_bad_presences)
    else:  # overlong_caps
        pad = element['pads'][draw(st.sampled_from(pad_indices))]
        pad['caps'] = draw(_overlong_caps)

    return document


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(document=_mutated_pad_documents())
def test_malformed_pad_data_is_rejected_with_report_error(document):
    """**Feature: port-guidance-and-pad-prepopulation, Property 3: Malformed pad data is rejected, not crashed on**

    Any valid pad-bearing report broken by a single targeted pad
    mutation — a non-list `pads`, a non-object pad entry, a dropped or
    mistyped pad field, a `direction` outside {sink, src}, a `presence`
    outside {always, sometimes, request}, or a `caps` string longer than
    4096 characters — is rejected with `ReportError` and nothing else
    (Requirement 4.4).

    **Validates: Requirements 4.4**
    """
    with pytest.raises(ReportError):
        parse_report(document)
