"""Property test for the Introspection_Report round-trip (task 1.2).

**Feature: gst-parameter-prepopulation, Property 1: Introspection report round-trip**

For any valid Introspection_Report structure,
`parse_report(serialize_report(report))` produces an equivalent report,
and the `serialize_report` output survives a real `json.dumps` /
`json.loads` cycle unchanged.

**Validates: Requirements 8.1, 8.2**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and
parse/serialize functions directly.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    STATUS_CAPTURED,
    STATUS_FAILED,
    EnumValue,
    GstProperty,
    Report,
    ReportElement,
    parse_report,
    serialize_report,
)

# ---------------------------------------------------------------------------
# Generators: valid Report dataclass instances
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
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(report=_reports)
def test_report_round_trip_through_json(report):
    """**Feature: gst-parameter-prepopulation, Property 1: Introspection report round-trip**

    For any valid report, serialization followed by a real
    `json.dumps`/`json.loads` cycle and re-parsing reproduces an equal
    Report, and the JSON cycle leaves the serialized document unchanged
    (Requirements 8.1, 8.2).

    **Validates: Requirements 8.1, 8.2**
    """
    document = serialize_report(report)

    # The serialized form survives a real JSON dump/load cycle unchanged
    # (8.2): what the build uploads is byte-for-byte re-interpretable.
    recovered_document = json.loads(json.dumps(document))
    assert recovered_document == document

    # parse_report is the inverse of serialize_report through that cycle
    # (8.1): the served report equals the captured one.
    assert parse_report(recovered_document) == report
