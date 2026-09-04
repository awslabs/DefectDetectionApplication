"""
Property-based tests for the LLM guidance parser core
(layers/shared/python/dda_llm_guidance.py): parse_guidance and
serialize_guidance.

Spec: llm-auto-labeling, task 2.3. Properties 1-5.

**Feature: llm-auto-labeling, Property 1: Guidance round trip**
**Validates: Requirements 4.10**
**Feature: llm-auto-labeling, Property 2: Validation is total and all-or-nothing**
**Validates: Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**
**Feature: llm-auto-labeling, Property 3: Class closure**
**Validates: Requirements 4.4**
**Feature: llm-auto-labeling, Property 4: Geometric containment of guidance**
**Validates: Requirements 4.5, 4.6**
**Feature: llm-auto-labeling, Property 5: Cardinality bound**
**Validates: Requirements 3.2, 4.7**

The module under test is pure (no boto3, no I/O), so these tests need no
moto fixtures and no AWS credentials — conftest.py already places the
shared layer on sys.path and registers the hypothesis profile these
tests run under.

Generator note: coordinates are drawn on a quarter-pixel grid (i / 4.0).
Dyadic quarters at these magnitudes are exact in IEEE doubles, so sums
like `left + width` and the JSON dump/load cycle are both exact — the
round-trip and bounds comparisons in the parser see precisely the
generated values, with no float-rounding flakiness.
"""
import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_llm_guidance import (
    MAX_DETECTIONS,
    GuidanceError,
    parse_guidance,
    serialize_guidance,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Image dimensions in pixels.
_dimensions = st.integers(min_value=4, max_value=1024)

# Class names: letters and digits only — no whitespace, so every name is
# equal to its .strip() and survives the parser's trimming untouched.
_class_names = st.text(
    alphabet=st.characters(
        whitelist_categories=('Lu', 'Ll', 'Nd'),
        whitelist_characters='_-',
        max_codepoint=0x24F,
    ),
    min_size=1,
    max_size=12,
)

# A Label_Set of 1-10 distinct names.
_label_sets = st.lists(_class_names, min_size=1, max_size=10, unique=True)


def _quarter(draw, low_i, high_i):
    """A float on the exact quarter-pixel grid: i / 4.0, i in [low_i, high_i]."""
    return draw(st.integers(min_value=low_i, max_value=high_i)) / 4.0


@st.composite
def _in_bounds_boxes(draw, width, height):
    """A validated-model in-bounds box: positive extent, left+width <= width."""
    li = draw(st.integers(min_value=0, max_value=4 * width - 1))
    ti = draw(st.integers(min_value=0, max_value=4 * height - 1))
    wi = draw(st.integers(min_value=1, max_value=4 * width - li))
    hi = draw(st.integers(min_value=1, max_value=4 * height - ti))
    return {'left': li / 4.0, 'top': ti / 4.0,
            'width': wi / 4.0, 'height': hi / 4.0}


@st.composite
def _in_bounds_vertices(draw, width, height):
    """3-8 polygon vertices, each with 0 <= x <= width and 0 <= y <= height."""
    count = draw(st.integers(min_value=3, max_value=8))
    return [
        (_quarter(draw, 0, 4 * width), _quarter(draw, 0, 4 * height))
        for _ in range(count)
    ]


@st.composite
def _valid_detections(draw, width, height, label_set):
    """One internal-model Detection that parse_guidance must accept."""
    class_name = draw(st.sampled_from(label_set))
    if draw(st.booleans()):
        return {'class': class_name, 'geometry': 'box',
                'box': draw(_in_bounds_boxes(width, height))}
    return {'class': class_name, 'geometry': 'polygon',
            'vertices': draw(_in_bounds_vertices(width, height))}


@st.composite
def _valid_cases(draw, min_detections=0, max_detections=8):
    """(width, height, label_set, detections) with every detection valid."""
    width = draw(_dimensions)
    height = draw(_dimensions)
    label_set = draw(_label_sets)
    detections = draw(st.lists(
        _valid_detections(width, height, label_set),
        min_size=min_detections, max_size=max_detections,
    ))
    return width, height, label_set, detections


def _wire_entry(detection):
    """The wire-format guidance entry for an internal-model Detection."""
    entry = {'class': detection['class']}
    if detection['geometry'] == 'box':
        entry['box'] = dict(detection['box'])
    else:
        entry['polygon'] = [[x, y] for x, y in detection['vertices']]
    return entry


def _absent_class(label_set):
    """A class name that cannot be a member: longer than every member."""
    return max(label_set, key=len) + 'x'


_INVALID_KINDS = (
    'unknown_class',
    'non_string_class',
    'box_and_polygon',
    'neither_geometry',
    'non_numeric_coordinate',
    'bool_coordinate',
    'nan_coordinate',
    'zero_extent',
    'negative_position',
    'overflowing_box',
    'two_vertex_polygon',
    'out_of_bounds_vertex',
)


@st.composite
def _invalid_entries(draw, width, height, label_set):
    """One wire entry that parse_guidance must reject, of a drawn kind."""
    kind = draw(st.sampled_from(_INVALID_KINDS))
    class_name = draw(st.sampled_from(label_set))
    box = draw(_in_bounds_boxes(width, height))
    vertices = draw(_in_bounds_vertices(width, height))

    if kind == 'unknown_class':
        return {'class': _absent_class(label_set), 'box': box}
    if kind == 'non_string_class':
        return {'class': 42, 'box': box}
    if kind == 'box_and_polygon':
        return {'class': class_name, 'box': box,
                'polygon': [[x, y] for x, y in vertices]}
    if kind == 'neither_geometry':
        return {'class': class_name}
    if kind == 'non_numeric_coordinate':
        return {'class': class_name, 'box': dict(box, left='10')}
    if kind == 'bool_coordinate':
        return {'class': class_name, 'box': dict(box, width=True)}
    if kind == 'nan_coordinate':
        return {'class': class_name, 'box': dict(box, top=float('nan'))}
    if kind == 'zero_extent':
        return {'class': class_name, 'box': dict(box, height=0)}
    if kind == 'negative_position':
        return {'class': class_name, 'box': dict(box, left=-0.25)}
    if kind == 'overflowing_box':
        return {'class': class_name,
                'box': {'left': width / 2.0, 'top': 0.0,
                        'width': float(width), 'height': float(height)}}
    if kind == 'two_vertex_polygon':
        return {'class': class_name, 'polygon': [[0, 0], [1, 1]]}
    # out_of_bounds_vertex
    return {'class': class_name,
            'polygon': [[0, 0], [width + 1, 0], [0, 1]]}


# Wire entries whose coordinates may or may not be in bounds — the input
# space for the conditional closure/containment properties. Coordinates
# range over [-width, 2*width] x [-height, 2*height].
@st.composite
def _unconstrained_entries(draw, width, height, label_set):
    class_name = draw(st.one_of(
        st.sampled_from(label_set),
        st.just(_absent_class(label_set)),
    ))
    if draw(st.booleans()):
        return {'class': class_name, 'box': {
            'left': _quarter(draw, -4 * width, 8 * width),
            'top': _quarter(draw, -4 * height, 8 * height),
            'width': _quarter(draw, 0, 4 * width),
            'height': _quarter(draw, 0, 4 * height),
        }}
    count = draw(st.integers(min_value=3, max_value=6))
    return {'class': class_name, 'polygon': [
        [_quarter(draw, -4 * width, 8 * width),
         _quarter(draw, -4 * height, 8 * height)]
        for _ in range(count)
    ]}


@st.composite
def _unconstrained_cases(draw):
    width = draw(_dimensions)
    height = draw(_dimensions)
    label_set = draw(_label_sets)
    entries = draw(st.lists(
        _unconstrained_entries(width, height, label_set), max_size=4))
    return width, height, label_set, entries


# ---------------------------------------------------------------------------
# Property 1: Guidance round trip
# ---------------------------------------------------------------------------

@given(case=_valid_cases())
def test_guidance_round_trip(case):
    """**Feature: llm-auto-labeling, Property 1: Guidance round trip**

    For every valid Coordinate_Guidance document, parsing its
    serialization yields detections with identical class names, geometry
    types, and coordinate values:
    `parse_guidance(serialize_guidance(d), L, w, h) == d`.

    **Validates: Requirements 4.10**
    """
    width, height, label_set, detections = case

    parsed = parse_guidance(
        serialize_guidance(detections), label_set, width, height)

    assert parsed == detections


# ---------------------------------------------------------------------------
# Property 2: Validation is total and all-or-nothing
# ---------------------------------------------------------------------------

@given(
    case=_valid_cases(max_detections=6),
    data=st.data(),
)
def test_one_invalid_detection_rejects_the_whole_document(case, data):
    """**Feature: llm-auto-labeling, Property 2: Validation is total and all-or-nothing**

    A document containing exactly one invalid detection among any number
    of valid ones always raises GuidanceError, regardless of where the
    invalid detection sits — never a partially validated list.

    **Validates: Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**
    """
    width, height, label_set, detections = case
    entries = [_wire_entry(d) for d in detections]

    invalid = data.draw(_invalid_entries(width, height, label_set),
                        label='invalid entry')
    position = data.draw(st.integers(min_value=0, max_value=len(entries)),
                         label='position')
    entries.insert(position, invalid)

    with pytest.raises(GuidanceError):
        parse_guidance(json.dumps({'detections': entries}),
                       label_set, width, height)


@given(
    text=st.text(max_size=300),
    label_set=_label_sets,
    width=_dimensions,
    height=_dimensions,
)
def test_validation_is_total_over_arbitrary_text(text, label_set, width, height):
    """**Feature: llm-auto-labeling, Property 2: Validation is total and all-or-nothing**

    For every input text, parse_guidance either returns a fully valid
    detection list or raises GuidanceError — no other exception type,
    and every returned detection is fully validated (class in the
    Label_Set, exactly one geometry).

    **Validates: Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**
    """
    try:
        result = parse_guidance(text, label_set, width, height)
    except GuidanceError:
        return

    assert isinstance(result, list)
    assert len(result) <= MAX_DETECTIONS
    for detection in result:
        assert detection['class'] in label_set
        assert detection['geometry'] in ('box', 'polygon')
        assert ('box' in detection) != ('vertices' in detection)


# ---------------------------------------------------------------------------
# Property 3: Class closure
# ---------------------------------------------------------------------------

@given(case=_unconstrained_cases())
def test_class_closure(case):
    """**Feature: llm-auto-labeling, Property 3: Class closure**

    Every class name in a returned detection list is an exact,
    case-sensitive member of the Label_Set — no parse result can name a
    class the job does not define, for any input document.

    **Validates: Requirements 4.4**
    """
    width, height, label_set, entries = case

    try:
        result = parse_guidance(json.dumps({'detections': entries}),
                                label_set, width, height)
    except GuidanceError:
        return

    for detection in result:
        assert detection['class'] in label_set


# ---------------------------------------------------------------------------
# Property 4: Geometric containment of guidance
# ---------------------------------------------------------------------------

@given(case=_unconstrained_cases())
def test_geometric_containment(case):
    """**Feature: llm-auto-labeling, Property 4: Geometric containment of guidance**

    Every box in a returned detection list satisfies left >= 0, top >= 0,
    left + width <= w, top + height <= h with width > 0 and height > 0;
    every polygon vertex satisfies 0 <= x <= w and 0 <= y <= h — for any
    input document the parser accepts.

    **Validates: Requirements 4.5, 4.6**
    """
    width, height, label_set, entries = case

    try:
        result = parse_guidance(json.dumps({'detections': entries}),
                                label_set, width, height)
    except GuidanceError:
        return

    for detection in result:
        if detection['geometry'] == 'box':
            box = detection['box']
            assert box['width'] > 0 and box['height'] > 0
            assert box['left'] >= 0 and box['top'] >= 0
            assert box['left'] + box['width'] <= width
            assert box['top'] + box['height'] <= height
            assert all(math.isfinite(v) for v in box.values())
        else:
            for x, y in detection['vertices']:
                assert math.isfinite(x) and math.isfinite(y)
                assert 0 <= x <= width
                assert 0 <= y <= height


# ---------------------------------------------------------------------------
# Property 5: Cardinality bound
# ---------------------------------------------------------------------------

@given(
    case=_valid_cases(min_detections=1, max_detections=1),
    count=st.integers(min_value=0, max_value=MAX_DETECTIONS),
)
def test_up_to_the_cap_is_accepted(case, count):
    """**Feature: llm-auto-labeling, Property 5: Cardinality bound**

    A document with 0-100 valid detections is accepted, and the returned
    list has exactly the document's entry count.

    **Validates: Requirements 3.2, 4.7**
    """
    width, height, label_set, detections = case
    entries = [_wire_entry(detections[0])] * count

    result = parse_guidance(json.dumps({'detections': entries}),
                            label_set, width, height)

    assert len(result) == count


@settings(deadline=None)
@given(
    case=_valid_cases(min_detections=1, max_detections=1),
    count=st.integers(min_value=MAX_DETECTIONS + 1, max_value=150),
    data=st.data(),
)
def test_over_the_cap_is_rejected_with_the_cap_reason(case, count, data):
    """**Feature: llm-auto-labeling, Property 5: Cardinality bound**

    A document with 101-150 detections is rejected, and — because the cap
    is checked before any per-detection validation — the reported reason
    is always the cap, even when the oversized document also contains an
    invalid detection.

    **Validates: Requirements 3.2, 4.7**
    """
    width, height, label_set, detections = case

    if data.draw(st.booleans(), label='include an invalid entry'):
        entry = data.draw(_invalid_entries(width, height, label_set),
                          label='invalid entry')
    else:
        entry = _wire_entry(detections[0])
    entries = [entry] * count

    with pytest.raises(GuidanceError) as excinfo:
        parse_guidance(json.dumps({'detections': entries}),
                       label_set, width, height)

    message = str(excinfo.value)
    assert f'at most {MAX_DETECTIONS}' in message
    assert str(count) in message
