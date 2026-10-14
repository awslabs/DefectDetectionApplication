"""Property test for caps confidence (task 1.9).

**Feature: port-guidance-and-pad-prepopulation, Property 7: Caps prefix decides confidence**

For any derived Port_Suggestion, `portType` is `VideoFrames`, and
`confident` is true iff the pad's caps string begins with the exact
case-sensitive characters `video/x-raw` (truncated caps included); every
non-confident suggestion carries the pad's caps string and a reason
stating that InferenceMeta and EventSignal are DDA semantic concepts the
caps cannot express.

**Validates: Requirements 5.2, 5.3**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and derivation
functions directly.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    CONFIDENT_CAPS_PREFIX,
    MAX_CAPS_LEN,
    PAD_PRESENCE_ALWAYS,
    PORT_TYPE_VIDEO_FRAMES,
    VALID_PAD_DIRECTIONS,
    PadTemplate,
    ReportElement,
    ports_for_element,
)

# ---------------------------------------------------------------------------
# Generators: caps strings biased around the video/x-raw prefix boundary
# ---------------------------------------------------------------------------

_MAX_SUFFIX = MAX_CAPS_LEN - len(CONFIDENT_CAPS_PREFIX)


@st.composite
def _case_variant_prefix(draw):
    """The prefix with each character's case independently flipped.

    Only the all-unflipped draw reproduces the exact case-sensitive
    prefix; every other variant must be classified non-confident.
    """
    flips = draw(st.lists(st.booleans(),
                          min_size=len(CONFIDENT_CAPS_PREFIX),
                          max_size=len(CONFIDENT_CAPS_PREFIX)))
    return ''.join(
        ch.swapcase() if flip else ch
        for ch, flip in zip(CONFIDENT_CAPS_PREFIX, flips)
    )


_suffixes = st.text(max_size=min(_MAX_SUFFIX, 60))

# Caps strategies, all bounded by MAX_CAPS_LEN so pads stay in the valid
# domain (`parse_report` rejects longer caps):
#   - exact prefix + arbitrary suffix         -> always confident
#   - case-variant prefix + arbitrary suffix  -> confident only when the
#     variant happens to be the exact prefix
#   - truncated (strict) prefixes of the prefix, e.g. 'video/x-ra' -> never
#     confident (shorter than the prefix itself)
#   - arbitrary text, including the empty string
_caps = st.one_of(
    st.tuples(st.just(CONFIDENT_CAPS_PREFIX), _suffixes).map(''.join),
    st.tuples(_case_variant_prefix(), _suffixes).map(''.join),
    st.integers(min_value=0, max_value=len(CONFIDENT_CAPS_PREFIX) - 1).map(
        lambda n: CONFIDENT_CAPS_PREFIX[:n]),
    st.text(max_size=80),
)

# Pads that always derive to a Port_Suggestion: presence 'always' and a
# non-whitespace name template (the two Unmapped_Pad routes are excluded so
# every generated pad exercises the confidence classification). Truncated
# variants are covered by caps_truncated spanning both values.
_suggestion_pads = st.builds(
    PadTemplate,
    name=st.text(min_size=1, max_size=40).filter(lambda s: s.strip()),
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.just(PAD_PRESENCE_ALWAYS),
    caps=_caps,
    caps_truncated=st.booleans(),
)

_elements = st.builds(
    ReportElement,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=st.one_of(st.none(), st.text(max_size=60)),
    properties=st.just([]),
    pads=st.lists(_suggestion_pads, min_size=1, max_size=8),
    pads_error=st.none(),
)


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(element=_elements)
def test_caps_prefix_decides_confidence(element):
    """**Feature: port-guidance-and-pad-prepopulation, Property 7: Caps prefix decides confidence**

    For any derived Port_Suggestion: `portType` is always `VideoFrames`;
    `confident` is true iff the pad's caps begin with the exact
    case-sensitive `video/x-raw` prefix (truncated caps included); and
    every non-confident suggestion carries the pad's caps string plus the
    reason naming InferenceMeta and EventSignal as DDA semantic concepts
    the caps cannot express — Requirements 5.2, 5.3.

    **Validates: Requirements 5.2, 5.3**
    """
    result = ports_for_element(element)
    suggestions = result['portSuggestions']

    # Every generated pad is presence 'always' with a valid name, so each
    # derives to exactly one Port_Suggestion in report order.
    assert len(suggestions) == len(element.pads)
    assert result['unmappedPads'] == []

    for pad, suggestion in zip(element.pads, suggestions):
        # 5.5 backdrop: the only caps-derivable Port_Type.
        assert suggestion['portType'] == PORT_TYPE_VIDEO_FRAMES

        # 5.2: confident iff the exact case-sensitive prefix, regardless
        # of capture-time truncation.
        expected_confident = pad.caps.startswith(CONFIDENT_CAPS_PREFIX)
        assert suggestion['confident'] is expected_confident

        # The caps string and truncation flag are carried through verbatim.
        assert suggestion['caps'] == pad.caps
        assert suggestion['capsTruncated'] is pad.caps_truncated

        if not expected_confident:
            # 5.3: the unconfirmed reason names the DDA semantic concepts
            # GStreamer caps cannot express.
            reason = suggestion['reason']
            assert 'InferenceMeta' in reason
            assert 'EventSignal' in reason
            assert 'DDA semantic concepts' in reason
        else:
            # Confident suggestions state the caps prefix instead.
            assert CONFIDENT_CAPS_PREFIX in suggestion['reason']
