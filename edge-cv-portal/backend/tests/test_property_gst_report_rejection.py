"""Property test for malformed Introspection_Report rejection (task 1.3).

**Feature: gst-parameter-prepopulation, Property 2: Malformed report documents are rejected, not crashed on**

For any JSON-decodable document that is not a valid version-1
Introspection_Report — whether arbitrary JSON or a targeted mutation of a
valid report — `parse_report` raises the typed `ReportError` and nothing
else, so callers can map it to the "introspection_failed" unavailability
reason instead of surfacing an internal error.

**Validates: Requirements 8.3, 1.6**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real parse function directly.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from gst_properties import (
    REPORT_VERSION,
    STATUS_CAPTURED,
    STATUS_FAILED,
    VALID_STATUSES,
    EnumValue,
    GstProperty,
    Report,
    ReportElement,
    ReportError,
    parse_report,
    serialize_report,
)

# ---------------------------------------------------------------------------
# Generators: valid Report dataclass instances (mirrors the round-trip test)
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

_elements = st.builds(
    ReportElement,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=_optional_texts,
    properties=st.lists(_properties, max_size=6),
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
# Generators: arbitrary JSON values
# ---------------------------------------------------------------------------

_json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        _finite_floats,
        st.text(max_size=20),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=12), children, max_size=4),
    ),
    max_leaves=15,
)

# Bias some documents toward "almost a report" shapes so the structural
# checks below the top level get exercised too, not just the top-level
# type/version gate.
_near_report_documents = st.fixed_dictionaries(
    {},
    optional={
        'reportVersion': _json_values,
        'status': _json_values,
        'message': _json_values,
        'gstVersion': _json_values,
        'capturedAt': _json_values,
        'elements': _json_values,
    },
)

_arbitrary_documents = st.one_of(_json_values, _near_report_documents)


def _is_valid_report(document) -> bool:
    """True when `parse_report` accepts the document.

    Any non-ReportError exception propagates: that in itself is a
    violation of Property 2 and fails the test.
    """
    try:
        parse_report(document)
    except ReportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Generators: targeted mutations of valid serialized reports
# ---------------------------------------------------------------------------

# A JSON object is not accepted for any field of the version-1 report
# schema (every field is a string / bool / number / scalar / array), so
# clobbering any present value with an object guarantees a ReportError.
_CLOBBER = {'not': 'a valid field value'}

# Keys whose absence makes the enclosing structure invalid.
_REQUIRED_TOP_KEYS = ('reportVersion', 'status')
_REQUIRED_ELEMENT_KEYS = ('factory', 'elementGType')
_REQUIRED_PROPERTY_KEYS = ('name', 'gtype', 'owner', 'writable')

_bad_versions = st.one_of(
    st.sampled_from([0, 2, -1, None, True, '1', str(REPORT_VERSION)]),
    st.integers().filter(lambda v: v != REPORT_VERSION),
)

_bad_statuses = st.one_of(
    st.text(max_size=20).filter(lambda s: s not in VALID_STATUSES),
    st.sampled_from([None, 3, True, [], {}]),
)


@st.composite
def _mutated_documents(draw):
    """A valid serialized report broken by exactly one targeted mutation."""
    document = serialize_report(draw(_reports))

    kinds = [
        'bad_version',
        'bad_status',
        'drop_top_key',
        'clobber_top_value',
    ]
    element_indices = list(range(len(document['elements'])))
    property_locations = [
        (i, j)
        for i, element in enumerate(document['elements'])
        for j in range(len(element['properties']))
    ]
    if element_indices:
        kinds += ['drop_element_key', 'clobber_element_value']
    if property_locations:
        kinds += ['drop_property_key', 'clobber_property_value']

    kind = draw(st.sampled_from(kinds))

    if kind == 'bad_version':
        document['reportVersion'] = draw(_bad_versions)
    elif kind == 'bad_status':
        document['status'] = draw(_bad_statuses)
    elif kind == 'drop_top_key':
        del document[draw(st.sampled_from(_REQUIRED_TOP_KEYS))]
    elif kind == 'clobber_top_value':
        document[draw(st.sampled_from(sorted(document)))] = _CLOBBER
    elif kind == 'drop_element_key':
        element = document['elements'][draw(st.sampled_from(element_indices))]
        del element[draw(st.sampled_from(_REQUIRED_ELEMENT_KEYS))]
    elif kind == 'clobber_element_value':
        element = document['elements'][draw(st.sampled_from(element_indices))]
        element[draw(st.sampled_from(sorted(element)))] = _CLOBBER
    elif kind == 'drop_property_key':
        i, j = draw(st.sampled_from(property_locations))
        prop = document['elements'][i]['properties'][j]
        del prop[draw(st.sampled_from(_REQUIRED_PROPERTY_KEYS))]
    else:  # clobber_property_value
        i, j = draw(st.sampled_from(property_locations))
        prop = document['elements'][i]['properties'][j]
        prop[draw(st.sampled_from(sorted(prop)))] = _CLOBBER

    return document


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(document=_arbitrary_documents)
def test_arbitrary_json_is_rejected_with_report_error(document):
    """**Feature: gst-parameter-prepopulation, Property 2: Malformed report documents are rejected, not crashed on**

    For arbitrary JSON-decodable input that is not a valid report,
    `parse_report` raises `ReportError` — never a TypeError, KeyError,
    or any other crash (Requirements 8.3, 1.6).

    **Validates: Requirements 8.3, 1.6**
    """
    # Discard the (vanishingly rare) accidental valid report. Any
    # non-ReportError exception raised here already fails the test.
    assume(not _is_valid_report(document))

    with pytest.raises(ReportError):
        parse_report(document)


@settings(max_examples=100, deadline=None)
@given(document=_mutated_documents())
def test_mutated_valid_reports_are_rejected_with_report_error(document):
    """**Feature: gst-parameter-prepopulation, Property 2: Malformed report documents are rejected, not crashed on**

    Any valid report broken by a single targeted mutation — a dropped
    required key, a wrong-typed field value at any depth, or an invalid
    reportVersion/status — is rejected with `ReportError` and nothing
    else (Requirements 8.3, 1.6).

    **Validates: Requirements 8.3, 1.6**
    """
    with pytest.raises(ReportError):
        parse_report(document)
