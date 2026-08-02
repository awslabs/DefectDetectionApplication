"""Property test for the derivation partition (task 1.8).

**Feature: port-guidance-and-pad-prepopulation, Property 6: Derivation partitions the pads**

For any element with a non-empty pad list, every Pad_Template appears in
exactly one of `portSuggestions` or `unmappedPads`: a pad with presence
`always` and a non-whitespace name template becomes a Port_Suggestion whose
direction is `input` for `sink` and `output` for `src` and whose name is the
name template verbatim; a pad with presence `sometimes` or `request` becomes
an Unmapped_Pad carrying its name, direction, presence, and the runtime-pads
caveat; a pad whose name template is empty or whitespace-only becomes an
Unmapped_Pad with the invalid-name caveat; and both output lists preserve
the pads' report order.

**Validates: Requirements 5.1, 5.4, 5.6**

Pure-module test: `gst_properties` imports no boto3 and needs no AWS
fixtures, so this test runs against the real dataclasses and derivation
function directly.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from gst_properties import (
    MAX_CAPS_LEN,
    PAD_DIRECTION_SINK,
    PAD_PRESENCE_ALWAYS,
    VALID_PAD_DIRECTIONS,
    VALID_PAD_PRESENCES,
    PadTemplate,
    ReportElement,
    ports_for_element,
)
from gst_properties import _CAVEAT_INVALID_NAME, _CAVEAT_RUNTIME_PADS

# ---------------------------------------------------------------------------
# Generators: non-empty pad lists spanning every partition branch
# ---------------------------------------------------------------------------

# Name templates deliberately include the invalid-name cases (empty and
# whitespace-only strings) alongside ordinary and whitespace-padded names.
_whitespace_only_names = st.text(alphabet=' \t\n\r', max_size=6)
_names = st.one_of(
    _whitespace_only_names,                 # includes the empty string
    st.text(max_size=40),                   # arbitrary names
    st.text(min_size=1, max_size=20).map(lambda s: f'{s}_%u'),
)

_pads = st.builds(
    PadTemplate,
    name=_names,
    direction=st.sampled_from(VALID_PAD_DIRECTIONS),
    presence=st.sampled_from(VALID_PAD_PRESENCES),   # all three presences
    caps=st.text(max_size=MAX_CAPS_LEN),
    caps_truncated=st.booleans(),
)

_elements = st.builds(
    ReportElement,
    factory=st.text(max_size=40),
    element_gtype=st.text(max_size=40),
    instantiation_error=st.one_of(st.none(), st.text(max_size=60)),
    properties=st.just([]),
    pads=st.lists(_pads, min_size=1, max_size=10),
    pads_error=st.none(),
)


def _expected_partition(pads):
    """Classify each pad exactly as the design's derivation table specifies."""
    suggestions = []
    unmapped = []
    for pad in pads:
        if pad.presence != PAD_PRESENCE_ALWAYS:
            unmapped.append({
                'name': pad.name,
                'direction': pad.direction,
                'presence': pad.presence,
                'caveat': _CAVEAT_RUNTIME_PADS.format(presence=pad.presence),
            })
        elif not pad.name.strip():
            unmapped.append({
                'name': pad.name,
                'direction': pad.direction,
                'presence': pad.presence,
                'caveat': _CAVEAT_INVALID_NAME,
            })
        else:
            suggestions.append({
                'name': pad.name,
                'direction': 'input' if pad.direction == PAD_DIRECTION_SINK else 'output',
            })
    return suggestions, unmapped


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(element=_elements)
def test_derivation_partitions_the_pads(element):
    """**Feature: port-guidance-and-pad-prepopulation, Property 6: Derivation partitions the pads**

    Every pad lands in exactly one output list with the correct direction
    mapping (sink -> input, src -> output), the name template verbatim, the
    correct caveat per unmapped case, and report order preserved in both
    lists — Requirements 5.1, 5.4, 5.6.

    **Validates: Requirements 5.1, 5.4, 5.6**
    """
    result = ports_for_element(element)
    suggestions = result['portSuggestions']
    unmapped = result['unmappedPads']

    # Partition: every pad appears in exactly one output list.
    assert len(suggestions) + len(unmapped) == len(element.pads)

    expected_suggestions, expected_unmapped = _expected_partition(element.pads)

    # Unmapped_Pads: verbatim name, direction, presence, the correct caveat
    # per case, in report order.
    assert unmapped == expected_unmapped

    # Port_Suggestions: verbatim name and sink->input / src->output mapping,
    # in report order (portType/confidence/reason are Properties 7 and 8).
    assert [{'name': s['name'], 'direction': s['direction']} for s in suggestions] \
        == expected_suggestions
