"""Property test for pads-reason classification (task 1.7).

**Feature: port-guidance-and-pad-prepopulation, Property 5: Pads-reason classification is total and exclusive**

For any report element, `ports_for_element` returns
`padsReason == 'pads_not_captured'` iff `pads is None`,
`'pads_read_failed'` (with the diagnostic as `padsMessage`) iff
`pads == []` with a `pads_error`, `'no_pad_templates'` iff `pads == []`
without one, and `None` iff `pads` is non-empty — and whenever a reason
is set, both `portSuggestions` and `unmappedPads` are empty.

**Validates: Requirements 4.7, 4.8**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and derivation
function directly.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    MAX_CAPS_LEN,
    PADS_REASON_NO_TEMPLATES,
    PADS_REASON_NOT_CAPTURED,
    PADS_REASON_READ_FAILED,
    VALID_PAD_DIRECTIONS,
    VALID_PAD_PRESENCES,
    EnumValue,
    GstProperty,
    PadTemplate,
    ReportElement,
    ports_for_element,
)

# ---------------------------------------------------------------------------
# Generators: valid ReportElement instances across every pad-data state
# (mirroring the element generators of test_property_pad_suggestions_unchanged)
# ---------------------------------------------------------------------------

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

_gtypes = st.one_of(
    st.sampled_from((
        'gint', 'guint', 'gint64', 'guint64', 'glong', 'gulong', 'guchar',
        'gfloat', 'gdouble', 'gboolean', 'gchararray',
        'GstCaps', 'gpointer', 'GstMyEnum',
    )),
    st.text(max_size=40),
)

_properties = st.builds(
    GstProperty,
    name=st.text(max_size=40),
    gtype=_gtypes,
    owner=st.text(max_size=40),
    writable=st.booleans(),
    blurb=_optional_texts,
    default=_optional_scalars,
    min=_optional_numbers,
    max=_optional_numbers,
    enum_values=st.one_of(st.none(), st.lists(_enum_values, max_size=6)),
)

# Valid pad templates, including whitespace-only names, every presence, and
# caps up to the MAX_CAPS_LEN boundary — so the non-empty state exercises
# suggestions and unmapped pads alike.
_pads = st.builds(
    PadTemplate,
    name=st.one_of(
        st.text(max_size=40),
        st.text(alphabet=' \t\n', max_size=4),  # empty/whitespace-only names
    ),
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.sampled_from(VALID_PAD_PRESENCES),
    caps=st.one_of(
        st.text(max_size=MAX_CAPS_LEN),
        st.text(max_size=200).map(lambda s: 'video/x-raw' + s),
    ),
    caps_truncated=st.booleans(),
)

# Every valid pad-data state, respecting the domain invariant that
# pads_error is non-None only when pads == []:
#   - pads=None, pads_error=None      (legacy element, pads not captured)
#   - pads=[],   pads_error=None      (element declares no templates)
#   - pads=[],   pads_error=<message> (per-element pad read failure)
#   - pads=[...non-empty...], pads_error=None
_pad_states = st.one_of(
    st.tuples(st.none(), st.none()),
    st.tuples(st.just([]), st.none()),
    st.tuples(st.just([]), st.text(min_size=1, max_size=60)),
    st.tuples(st.lists(_pads, min_size=1, max_size=6), st.none()),
)


@st.composite
def _elements(draw):
    pads, pads_error = draw(_pad_states)
    return ReportElement(
        factory=draw(st.text(max_size=40)),
        element_gtype=draw(st.text(max_size=40)),
        instantiation_error=draw(_optional_texts),
        properties=draw(st.lists(_properties, max_size=4)),
        pads=pads,
        pads_error=pads_error,
    )


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(element=_elements())
def test_pads_reason_classification_is_total_and_exclusive(element):
    """**Feature: port-guidance-and-pad-prepopulation, Property 5: Pads-reason classification is total and exclusive**

    The four element states map iff-style onto `padsReason`/`padsMessage`
    (Requirements 4.7, 4.8), and whenever a reason is set both
    `portSuggestions` and `unmappedPads` are empty.

    **Validates: Requirements 4.7, 4.8**
    """
    result = ports_for_element(element)
    reason = result['padsReason']
    message = result['padsMessage']

    # Iff mapping of the four mutually exclusive element states (4.7, 4.8).
    if element.pads is None:
        assert reason == PADS_REASON_NOT_CAPTURED
        assert message is None
    elif element.pads == [] and element.pads_error is not None:
        assert reason == PADS_REASON_READ_FAILED
        assert message == element.pads_error
    elif element.pads == []:
        assert reason == PADS_REASON_NO_TEMPLATES
        assert message is None
    else:
        assert reason is None
        assert message is None

    # The reverse direction of each iff: a given reason implies its state.
    if reason == PADS_REASON_NOT_CAPTURED:
        assert element.pads is None
    elif reason == PADS_REASON_READ_FAILED:
        assert element.pads == [] and element.pads_error is not None
    elif reason == PADS_REASON_NO_TEMPLATES:
        assert element.pads == [] and element.pads_error is None
    else:
        assert reason is None
        assert element.pads  # non-empty list

    # Whenever a reason is set, no derivation output leaks through.
    if reason is not None:
        assert result['portSuggestions'] == []
        assert result['unmappedPads'] == []
