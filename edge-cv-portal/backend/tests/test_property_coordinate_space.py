"""
Property-based test for the Sent -> Source coordinate space transform of
the `llm:` auto-label family (layers/shared/python/dda_llm_guidance.py):
`scale_detections` composed with the pre-existing `guidance_to_prelabel`.

Spec: llm-model-token-and-image-sizing, task 2.4. Property 7.

**Feature: llm-model-token-and-image-sizing, Property 7: Pre_Label geometry is expressed in the original image's coordinate space**
**Validates: Requirements 7.3, 7.4, 7.5, 7.8**

Both functions under test are pure (no boto3, no Pillow, no I/O), so this
test needs no moto fixtures and no AWS credentials — conftest.py already
places the shared layer on sys.path. The property runs at 100 examples
via its own `@settings`, which takes precedence over the profile default.

Generator notes:

- Dimension pairs are constrained to `sent <= source` on each axis (the
  Image_Downscaler never enlarges — Requirement 6.5), drawn from three
  families: the equal pair with high weight, so Requirement 7.5's
  genuine no-op path is exercised often; near-equal pairs of the
  1001/1000 shape, whose scale factor sits a hair above 1 and exposes
  the sub-pixel-collapse edge; and free pairs for general downscale
  ratios (including a single axis left equal, where the mapping reduces
  to pure round-half-up on that axis).
- Geometry is drawn on the exact quarter-pixel grid (i / 4.0) over the
  SENT dimensions, so every generated detection is valid in Sent space
  by construction — in bounds, positive extent, >= 3 vertices —
  including coordinates at 0 and at the extent, and sub-pixel extents
  (the collapse candidates). A serialize/parse round trip through the
  real Sent-space validator enforces that premise on every example
  (quarters are dyadic, so the JSON round trip is float-exact).
- The expected mapping is recomputed in the test from Requirement 7.3's
  rule — min(bound, max(0, floor(v * source / sent + 0.5))) — applied
  to box corners and polygon vertices, never read back from the module
  under test.
"""
import copy
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_llm_guidance import (
    GuidanceError,
    guidance_to_prelabel,
    parse_guidance,
    polygon_bounding_box,
    rasterize_to_rle,
    scale_detections,
    serialize_guidance,
)
from dda_manifest import rle_decode

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_modalities = st.sampled_from(
    ('Segmentation', 'ObjectDetection', 'Classification'))

# Label_Set from a fixed pool of clean names (every name equals its strip).
_LABEL_POOL = ('scratch', 'dent', 'crack', 'stain', 'chip', 'burr')
_label_sets = st.lists(
    st.sampled_from(_LABEL_POOL), min_size=1, max_size=4, unique=True)

# Source extents for the equal and free families. Kept modest because
# Segmentation rasterizes at the SOURCE dimensions, O(columns x edges).
_extents = st.integers(min_value=1, max_value=64)

# Source extents for the near-equal family — 1001/1000 is the design's
# named sub-pixel-collapse pair.
_near_equal_sources = st.sampled_from((1001, 501, 251, 101))


@st.composite
def _dimension_cases(draw):
    """(sent_w, sent_h, source_w, source_h) with sent <= source per axis.

    Three families: 'equal' (weighted high — the Requirement 7.5 no-op),
    'near_equal' (scale factor a hair above 1 on one or both axes, the
    sub-pixel-collapse edge), and 'free' (any downscale ratio).
    """
    family = draw(st.sampled_from(
        ('equal', 'equal', 'equal', 'near_equal', 'free', 'free')))
    if family == 'equal':
        width = draw(_extents)
        height = draw(_extents)
        return width, height, width, height
    if family == 'near_equal':
        source_width = draw(_near_equal_sources)
        source_height = draw(_near_equal_sources)
        sent_width = source_width - draw(st.integers(min_value=0, max_value=1))
        sent_height = source_height - draw(st.integers(min_value=0, max_value=1))
        return sent_width, sent_height, source_width, source_height
    source_width = draw(_extents)
    source_height = draw(_extents)
    sent_width = draw(st.integers(min_value=1, max_value=source_width))
    sent_height = draw(st.integers(min_value=1, max_value=source_height))
    return sent_width, sent_height, source_width, source_height


@st.composite
def _sent_space_boxes(draw, width, height):
    """A box valid in Sent space on the quarter-pixel grid: in bounds
    with positive extent. Corners at 0 and at the extent are reachable;
    sub-pixel extents (the collapse candidates) are deliberately
    allowed."""
    left_q = draw(st.integers(min_value=0, max_value=4 * width - 1))
    top_q = draw(st.integers(min_value=0, max_value=4 * height - 1))
    width_q = draw(st.integers(min_value=1, max_value=4 * width - left_q))
    height_q = draw(st.integers(min_value=1, max_value=4 * height - top_q))
    return {'left': left_q / 4.0, 'top': top_q / 4.0,
            'width': width_q / 4.0, 'height': height_q / 4.0}


@st.composite
def _sent_space_vertices(draw, width, height):
    """3-6 vertices valid in Sent space, coordinates at 0 and at the
    extent included; degenerate (collinear, tiny) polygons allowed."""
    count = draw(st.integers(min_value=3, max_value=6))
    return [
        (draw(st.integers(min_value=0, max_value=4 * width)) / 4.0,
         draw(st.integers(min_value=0, max_value=4 * height)) / 4.0)
        for _ in range(count)
    ]


@st.composite
def _sent_space_detections(draw, width, height, label_set):
    class_name = draw(st.sampled_from(label_set))
    if draw(st.booleans()):
        return {'class': class_name, 'geometry': 'box',
                'box': draw(_sent_space_boxes(width, height))}
    return {'class': class_name, 'geometry': 'polygon',
            'vertices': draw(_sent_space_vertices(width, height))}


@st.composite
def _transform_cases(draw):
    sent_width, sent_height, source_width, source_height = \
        draw(_dimension_cases())
    label_set = draw(_label_sets)
    detections = draw(st.lists(
        _sent_space_detections(sent_width, sent_height, label_set),
        min_size=0, max_size=5))
    modality = draw(_modalities)
    return (sent_width, sent_height, source_width, source_height,
            label_set, detections, modality)


# ---------------------------------------------------------------------------
# Reference computations (independent of the module under test)
# ---------------------------------------------------------------------------

def _requirement_7_3_coordinate(value, source_extent, sent_extent):
    """Requirement 7.3's mapping rule, recomputed in the test:
    min(bound, max(0, floor(v * source / sent + 0.5))) — round-half-up,
    then clamped into [0, source_extent]."""
    scaled = math.floor(value * source_extent / sent_extent + 0.5)
    return float(min(source_extent, max(0, scaled)))


def _reference_scaled(detections, sent_width, sent_height,
                      source_width, source_height):
    """The expected Sent -> Source mapping: boxes as their two corners
    (left, top) and (left + width, top + height) with the extents
    re-derived as differences; polygons per vertex."""
    expected = []
    for detection in detections:
        if detection['geometry'] == 'box':
            box = detection['box']
            left = _requirement_7_3_coordinate(
                box['left'], source_width, sent_width)
            top = _requirement_7_3_coordinate(
                box['top'], source_height, sent_height)
            right = _requirement_7_3_coordinate(
                box['left'] + box['width'], source_width, sent_width)
            bottom = _requirement_7_3_coordinate(
                box['top'] + box['height'], source_height, sent_height)
            expected.append({
                'class': detection['class'], 'geometry': 'box',
                'box': {'left': left, 'top': top,
                        'width': right - left, 'height': bottom - top},
            })
        else:
            expected.append({
                'class': detection['class'], 'geometry': 'polygon',
                'vertices': [
                    (_requirement_7_3_coordinate(x, source_width, sent_width),
                     _requirement_7_3_coordinate(y, source_height, sent_height))
                    for x, y in detection['vertices']
                ],
            })
    return expected


def _first_degenerate_message(detections, modality, width, height):
    """The pre-existing GuidanceError message guidance_to_prelabel
    raises at the first degenerate detection, recomputed independently;
    None when no detection is degenerate (conversion must succeed)."""
    for index, detection in enumerate(detections):
        if modality == 'ObjectDetection':
            if detection['geometry'] == 'box':
                box = detection['box']
            else:
                box = polygon_bounding_box(detection['vertices'])
            if int(box['width']) < 1 or int(box['height']) < 1:
                return (f'detection {index} ({detection["class"]!r}) converts '
                        f'to a bounding box with zero width or height')
        else:
            rle = rasterize_to_rle(detection, width, height)
            if len(rle.split()) == 1:
                return (f'detection {index} ({detection["class"]!r}) covers '
                        f'no pixel and cannot become a mask region')
    return None


def _outcome(detections, modality, label_set, width, height):
    """('ok', Pre_Label) or ('error', message). guidance_to_prelabel is
    pre-existing and unchanged, so a direct call on unscaled detections
    at the image's own dimensions IS the pinned pre-feature call path."""
    try:
        return 'ok', guidance_to_prelabel(
            detections, modality, label_set, width, height)
    except GuidanceError as error:
        return 'error', str(error)


# ---------------------------------------------------------------------------
# Property 7: Pre_Label geometry is expressed in the original image's
# coordinate space
# ---------------------------------------------------------------------------

@given(case=_transform_cases())
@settings(max_examples=100, deadline=None)
def test_prelabel_geometry_is_expressed_in_source_space(case):
    """**Feature: llm-model-token-and-image-sizing, Property 7: Pre_Label geometry is expressed in the original image's coordinate space**

    For any validated Coordinate_Guidance over the Sent_Dimensions, any
    modality and any Label_Set: the Pre_Label geometry lies within the
    Source_Dimensions bounds and equals the geometry scaled by
    Requirement 7.3's round-half-up-and-clamp rule recomputed here (7.3,
    7.4); whenever the Sent_Dimensions equal the Source_Dimensions the
    whole Pre_Label equals the pinned pre-feature call path exactly and
    `scale_detections` returns its input list unchanged (7.5); the
    Classification Pre_Label is unscaled — equal with and without the
    transform (7.8). The only admissible conversion failure is the
    sub-pixel-collapse edge, raising the pre-existing GuidanceError with
    its pre-existing message.

    **Validates: Requirements 7.3, 7.4, 7.5, 7.8**
    """
    (sent_width, sent_height, source_width, source_height,
     label_set, detections, modality) = case

    # Premise: the generated guidance is valid in Sent space under the
    # real validator (the round trip is float-exact on the quarter grid).
    assert parse_guidance(serialize_guidance(detections), label_set,
                          sent_width, sent_height) == detections

    snapshot = copy.deepcopy(detections)
    scaled = scale_detections(detections, sent_width, sent_height,
                              source_width, source_height)
    assert detections == snapshot, 'scale_detections mutated its input'

    dimensions_equal = \
        (sent_width, sent_height) == (source_width, source_height)
    if dimensions_equal:
        # Requirement 7.5: a genuine no-op — the same list object, no
        # scaling, no rounding, no clamping, no float round trip.
        assert scaled is detections
    else:
        # Requirements 7.3 and 7.4: every coordinate equals the mapping
        # rule recomputed in this test, clamped into the source bounds;
        # boxes mapped as two corners, polygons per vertex.
        assert scaled == _reference_scaled(
            detections, sent_width, sent_height, source_width, source_height)

    if modality == 'Classification':
        # Requirement 7.8: no coordinate scaling — the result is equal
        # with and without the transform, and is exactly the pre-feature
        # Classification Pre_Label.
        unscaled_outcome = _outcome(detections, 'Classification', label_set,
                                    source_width, source_height)
        assert _outcome(scaled, 'Classification', label_set,
                        source_width, source_height) == unscaled_outcome
        assert unscaled_outcome == ('ok', {
            'modality': 'Classification',
            'label': 'anomaly' if detections else 'normal',
        })
        return

    status, result = _outcome(scaled, modality, label_set,
                               source_width, source_height)

    if dimensions_equal:
        # Requirement 7.5: the whole Pre_Label — or the pre-existing
        # failure, for degenerate sub-pixel input — equals the pinned
        # pre-feature call path exactly.
        assert (status, result) == _outcome(
            detections, modality, label_set, source_width, source_height)

    if status == 'error':
        # The sub-pixel-collapse edge (design, scale_detections notes):
        # only a detection whose scaled geometry is degenerate may fail,
        # and only with the pre-existing GuidanceError message.
        expected_message = _first_degenerate_message(
            scaled, modality, source_width, source_height)
        assert expected_message is not None, (
            f'GuidanceError raised though no scaled detection is '
            f'degenerate: {result}'
        )
        assert result == expected_message
        return

    # Success: Requirement 7.4 — every emitted coordinate lies within
    # the Source_Dimensions bounds and the Pre_Label is expressed at the
    # Source_Dimensions, one element per detection in guidance order.
    if modality == 'ObjectDetection':
        assert result['image_width'] == source_width
        assert result['image_height'] == source_height
        assert len(result['boxes']) == len(scaled)
        for emitted, detection in zip(result['boxes'], scaled):
            assert emitted['class'] == detection['class']
            if detection['geometry'] == 'box':
                expected_box = detection['box']
            else:
                expected_box = polygon_bounding_box(detection['vertices'])
            assert (emitted['left'], emitted['top'],
                    emitted['width'], emitted['height']) == (
                expected_box['left'], expected_box['top'],
                expected_box['width'], expected_box['height'])
            assert emitted['left'] >= 0
            assert emitted['top'] >= 0
            assert emitted['left'] + emitted['width'] <= source_width
            assert emitted['top'] + emitted['height'] <= source_height
    else:
        assert result['image_width'] == source_width
        assert result['image_height'] == source_height
        assert len(result['regions']) == len(scaled)
        for emitted, detection in zip(result['regions'], scaled):
            assert emitted['class'] == detection['class']
            # The mask is rasterized from the SCALED geometry directly
            # at the Source_Dimensions — never resampled ...
            assert emitted['rle'] == rasterize_to_rle(
                detection, source_width, source_height)
            # ... and decodes within source_width x source_height:
            # non-negative counts summing to exactly the source pixel
            # count (the invariant dda_manifest.rle_decode enforces).
            counts = [int(token) for token in emitted['rle'].split()]
            assert all(count >= 0 for count in counts)
            assert sum(counts) == source_width * source_height
            if source_width * source_height <= 4096:
                # Affordable dense decode through the real manifest
                # decoder, grounding "decodes" in the actual consumer.
                mask = rle_decode(emitted['rle'], source_width, source_height)
                assert len(mask) == source_width * source_height


def test_sub_pixel_collapse_raises_the_pre_existing_guidance_error():
    """**Feature: llm-model-token-and-image-sizing, Property 7: Pre_Label geometry is expressed in the original image's coordinate space**

    The design's named edge, pinned deterministically: at the 1001/1000
    near-equal pair, a sub-pixel box that passed validation in Sent
    space collapses to zero extent in Source space, and conversion
    raises the pre-existing GuidanceError with its pre-existing message
    — no new category, no new reason.

    **Validates: Requirements 7.3, 7.4, 7.5, 7.8**
    """
    detections = [{
        'class': 'scratch', 'geometry': 'box',
        'box': {'left': 0.0, 'top': 0.0, 'width': 0.25, 'height': 0.25},
    }]
    # Valid in Sent space (1000 x 1000) under the real validator.
    assert parse_guidance(serialize_guidance(detections), ['scratch'],
                          1000, 1000) == detections

    scaled = scale_detections(detections, 1000, 1000, 1001, 1001)
    # floor(0.25 * 1001 / 1000 + 0.5) == 0: both corners land on 0, the
    # box collapses to zero extent.
    assert scaled == [{
        'class': 'scratch', 'geometry': 'box',
        'box': {'left': 0.0, 'top': 0.0, 'width': 0.0, 'height': 0.0},
    }]

    with pytest.raises(GuidanceError) as excinfo:
        guidance_to_prelabel(scaled, 'ObjectDetection', ['scratch'],
                             1001, 1001)
    assert str(excinfo.value) == (
        "detection 0 ('scratch') converts to a bounding box with zero "
        "width or height"
    )
