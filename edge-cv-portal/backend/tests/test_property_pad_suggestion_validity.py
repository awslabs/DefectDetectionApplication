"""Property test for suggestion validity and determinism (task 1.10).

**Feature: port-guidance-and-pad-prepopulation, Property 8: Derived suggestions are valid and derivation is deterministic**

For any report element, every derived Port_Suggestion satisfies the existing
Ports_Step validation rules (non-empty trimmed name, portType in the
Node_Type_Catalog), and calling `ports_for_element` twice on the same element
yields deeply equal results.

**Validates: Requirements 5.5, 5.7**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and derivation
functions directly.
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    MAX_CAPS_LEN,
    VALID_PAD_DIRECTIONS,
    VALID_PAD_PRESENCES,
    PadTemplate,
    ReportElement,
    ports_for_element,
)

# The Node_Type_Catalog: the declared Port_Types the Ports_Step accepts
# (requirements glossary — VideoFrames, InferenceMeta, EventSignal).
NODE_TYPE_CATALOG = ('VideoFrames', 'InferenceMeta', 'EventSignal')

# ---------------------------------------------------------------------------
# Generators: valid ReportElement instances across every pad-data state
# ---------------------------------------------------------------------------

_optional_texts = st.one_of(st.none(), st.text(max_size=60))

# Pad names deliberately include empty and whitespace-only templates (which
# must never surface as Port_Suggestions) alongside ordinary names.
_pad_names = st.one_of(
    st.just(''),
    st.text(alphabet=' \t\n', max_size=4),
    st.text(max_size=40),
)

# Caps with and without the confident `video/x-raw` prefix, including the
# MAX_CAPS_LEN boundary, so both confident and unconfirmed suggestions occur.
_caps = st.one_of(
    st.text(max_size=MAX_CAPS_LEN),
    st.text(max_size=MAX_CAPS_LEN - 20).map(lambda s: 'video/x-raw' + s),
)

_pads = st.builds(
    PadTemplate,
    name=_pad_names,
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.sampled_from(VALID_PAD_PRESENCES),
    caps=_caps,
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
    st.tuples(st.lists(_pads, min_size=1, max_size=8), st.none()),
)


@st.composite
def _elements(draw):
    pads, pads_error = draw(_pad_states)
    return ReportElement(
        factory=draw(st.text(max_size=40)),
        element_gtype=draw(st.text(max_size=40)),
        instantiation_error=draw(_optional_texts),
        properties=[],
        pads=pads,
        pads_error=pads_error,
    )


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(element=_elements())
def test_suggestions_valid_and_derivation_deterministic(element):
    """**Feature: port-guidance-and-pad-prepopulation, Property 8: Derived suggestions are valid and derivation is deterministic**

    For any report element, every derived Port_Suggestion satisfies the
    existing Ports_Step validation rules — non-empty trimmed name and a
    portType from the Node_Type_Catalog (Requirement 5.5) — and calling
    `ports_for_element` twice on the same element yields deeply equal
    results (Requirement 5.7).

    **Validates: Requirements 5.5, 5.7**
    """
    first = ports_for_element(element)
    # Deep-copy the first result before the second call so any (forbidden)
    # shared mutable state between calls would surface in the comparison.
    first_snapshot = copy.deepcopy(first)
    second = ports_for_element(element)

    # Requirement 5.5: every derived Port_Suggestion passes the Ports_Step
    # validation rules.
    for suggestion in first['portSuggestions']:
        assert isinstance(suggestion['name'], str)
        assert suggestion['name'].strip() != ''
        assert suggestion['portType'] in NODE_TYPE_CATALOG

    # Requirement 5.7: derivation is deterministic — two calls on the same
    # element yield deeply equal results.
    assert first_snapshot == second
    assert first == second
