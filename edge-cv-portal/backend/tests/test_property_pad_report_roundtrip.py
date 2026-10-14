"""Property test for the extended (pads-bearing) report round-trip (task 1.2).

**Feature: port-guidance-and-pad-prepopulation, Property 1: Extended report round-trip**

For any valid Introspection_Report — with pad data (including truncated
caps and per-element pad read failures), without pad data, or mixed per
element — `parse_report(serialize_report(report))` equals the original
report field-for-field, and the serialized document survives a real
`json.dumps`/`json.loads` cycle unchanged.

**Validates: Requirements 4.1, 4.3**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and
parse/serialize functions directly. The generators extend the report
generators of `test_property_gst_report_roundtrip.py` with pad
strategies covering every legal element pad state.
"""

from __future__ import annotations

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

# Caps together with a matching capsTruncated marker (Requirement 3.4 shape):
#   - caps shorter than the bound, not truncated (the common case);
#   - caps of exactly MAX_CAPS_LEN marked truncated (capture cut it there);
#   - caps of exactly MAX_CAPS_LEN not truncated (a caps string that is
#     naturally at the boundary) — the largest caps the parser accepts.
_caps_with_truncation = st.one_of(
    st.tuples(st.text(max_size=80), st.just(False)),
    st.tuples(st.text(min_size=MAX_CAPS_LEN, max_size=MAX_CAPS_LEN), st.just(True)),
    st.tuples(st.text(min_size=MAX_CAPS_LEN, max_size=MAX_CAPS_LEN), st.just(False)),
)

_pads = _caps_with_truncation.flatmap(
    lambda caps_pair: st.builds(
        PadTemplate,
        name=st.text(max_size=40),
        direction=st.sampled_from(VALID_PAD_DIRECTIONS),
        presence=st.sampled_from(VALID_PAD_PRESENCES),
        caps=st.just(caps_pair[0]),
        caps_truncated=st.just(caps_pair[1]),
    )
)

# Every legal element pad state (domain invariant: pads_error is non-None
# only when pads == []):
#   - pads=None, pads_error=None            (legacy element, 4.2)
#   - pads=[],   pads_error=None            (no static pad templates, 3.5)
#   - pads=[],   pads_error=<diagnostic>    (per-element read failure, 3.2)
#   - pads=[...non-empty...], pads_error=None
_pad_states = st.one_of(
    st.tuples(st.none(), st.none()),
    st.tuples(st.just([]), st.none()),
    st.tuples(st.just([]), st.text(min_size=1, max_size=60)),
    st.tuples(st.lists(_pads, min_size=1, max_size=6), st.none()),
)

_elements = _pad_states.flatmap(
    lambda pad_state: st.builds(
        ReportElement,
        factory=st.text(max_size=40),
        element_gtype=st.text(max_size=40),
        instantiation_error=_optional_texts,
        properties=st.lists(_properties, max_size=6),
        pads=st.just(pad_state[0]),
        pads_error=st.just(pad_state[1]),
    )
)

# Elements are drawn independently, so a single report freely mixes
# pads-bearing, pad-free, empty-pad-list, and read-failure elements.
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
def test_extended_report_round_trip_through_json(report):
    """**Feature: port-guidance-and-pad-prepopulation, Property 1: Extended report round-trip**

    For any valid report — pads-bearing, legacy, or mixed per element —
    serialization followed by a real `json.dumps`/`json.loads` cycle and
    re-parsing reproduces an equal Report, and the JSON cycle leaves the
    serialized document unchanged (Requirements 4.1, 4.3).

    **Validates: Requirements 4.1, 4.3**
    """
    document = serialize_report(report)

    # The serialized form survives a real JSON dump/load cycle unchanged:
    # what the build uploads is byte-for-byte re-interpretable.
    recovered_document = json.loads(json.dumps(document))
    assert recovered_document == document

    # parse_report is the inverse of serialize_report through that cycle
    # (4.1, 4.3): every report-level, element-level, pad-level, and
    # property field of the re-parsed report equals the original.
    assert parse_report(recovered_document) == report
