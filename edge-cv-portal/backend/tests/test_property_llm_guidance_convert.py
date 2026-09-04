"""
Property-based tests for the modality converter
(layers/shared/python/dda_llm_guidance.py): guidance_to_prelabel and
polygon_bounding_box.

Spec: llm-auto-labeling, task 4.3. Properties 9-12.

**Feature: llm-auto-labeling, Property 9: Detection preservation**
**Validates: Requirements 5.1, 5.3**
**Feature: llm-auto-labeling, Property 10: Polygon hull tightness**
**Validates: Requirements 5.3**
**Feature: llm-auto-labeling, Property 11: Empty is success, degenerate is failure**
**Validates: Requirements 5.5, 5.7**
**Feature: llm-auto-labeling, Property 12: Classification totality**
**Validates: Requirements 5.4**

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures and no AWS credentials — conftest.py already places
the shared layer on sys.path and registers the hypothesis profile these
tests run under.

Generator note: coordinates are drawn on a quarter-pixel grid (i / 4.0).
Dyadic quarters at these magnitudes are exact in IEEE doubles, so hull
identities like `left + width == max x` and bounds comparisons are
exact, with no float-rounding flakiness.

Preservation (Property 9) generates detections guaranteed to cover at
least one pixel center (boxes and rectangle polygons enclosing a whole
pixel), so Segmentation rasterization always succeeds and preservation
is never confounded by degenerate rejections. Same-class runs are
generated explicitly (consecutive detections sharing a class) to pin
"never merged across detections sharing a class".

The empty/degenerate property (Property 11) instead allows arbitrary
in-bounds geometry, including sub-pixel slivers and degenerate
polygons, and asserts the two outcomes — GuidanceError raised XOR a
successful Pre_Label in which every detection contributed an element —
are disjoint and exhaustive over "a detection produced nothing".
"""
from hypothesis import given
from hypothesis import strategies as st

from dda_llm_guidance import (
    GuidanceError,
    guidance_to_prelabel,
    polygon_bounding_box,
    rasterize_to_rle,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Image dimensions in pixels. Kept modest: Segmentation conversion
# rasterizes every detection at O(columns x edges).
_dimensions = st.integers(min_value=4, max_value=64)

# Class names: letters/digits only, so every name equals its .strip().
_class_names = st.text(
    alphabet=st.characters(
        whitelist_categories=('Lu', 'Ll', 'Nd'),
        whitelist_characters='_-',
        max_codepoint=0x24F,
    ),
    min_size=1,
    max_size=12,
)

# Small Label_Sets (1-4 distinct names) so same-class detections are
# frequent even before the explicit run expansion below.
_label_sets = st.lists(_class_names, min_size=1, max_size=4, unique=True)


def _quarter(draw, low_i, high_i):
    """A float on the exact quarter-pixel grid: i / 4.0, i in [low_i, high_i]."""
    return draw(st.integers(min_value=low_i, max_value=high_i)) / 4.0


# --- Coverage-guaranteed geometry (Property 9) ----------------------------

@st.composite
def _covering_boxes(draw, width, height):
    """An in-bounds box guaranteed to contain the whole pixel
    [px, px+1) x [py, py+1) — so its center (px+0.5, py+0.5) is covered,
    rasterization yields foreground, and the extent is >= 1 in both
    axes (int()-truncation never reaches zero)."""
    px = draw(st.integers(min_value=0, max_value=width - 1))
    py = draw(st.integers(min_value=0, max_value=height - 1))
    li = draw(st.integers(min_value=0, max_value=4 * px))
    ti = draw(st.integers(min_value=0, max_value=4 * py))
    ri = draw(st.integers(min_value=4 * (px + 1), max_value=4 * width))
    bi = draw(st.integers(min_value=4 * (py + 1), max_value=4 * height))
    return {'left': li / 4.0, 'top': ti / 4.0,
            'width': (ri - li) / 4.0, 'height': (bi - ti) / 4.0}


@st.composite
def _covering_detections(draw, width, height, class_name):
    """One Detection with the given class, guaranteed to cover at least
    one pixel center: a covering box, or that same rectangle expressed
    as a 4-vertex polygon (either winding order)."""
    box = draw(_covering_boxes(width, height))
    if draw(st.booleans()):
        return {'class': class_name, 'geometry': 'box', 'box': box}
    left, top = box['left'], box['top']
    right, bottom = left + box['width'], top + box['height']
    vertices = [(left, top), (right, top), (right, bottom), (left, bottom)]
    if draw(st.booleans()):
        vertices.reverse()
    return {'class': class_name, 'geometry': 'polygon', 'vertices': vertices}


@st.composite
def _preservation_cases(draw):
    """(width, height, label_set, detections) where every detection is
    guaranteed to convert in every modality. Detections are built from
    explicit (class, run_length) runs so consecutive same-class
    detections are generated deliberately."""
    width = draw(_dimensions)
    height = draw(_dimensions)
    label_set = draw(_label_sets)
    runs = draw(st.lists(
        st.tuples(st.sampled_from(label_set),
                  st.integers(min_value=1, max_value=3)),
        min_size=0, max_size=4,
    ))
    detections = []
    for class_name, length in runs:
        for _ in range(length):
            detections.append(
                draw(_covering_detections(width, height, class_name)))
    return width, height, label_set, detections


# --- Unconstrained in-bounds geometry (Properties 11, 12) ------------------

@st.composite
def _in_bounds_boxes(draw, width, height):
    """A validated-model in-bounds box with positive (possibly
    sub-pixel) extent — degenerate slivers are deliberately allowed."""
    li = draw(st.integers(min_value=0, max_value=4 * width - 1))
    ti = draw(st.integers(min_value=0, max_value=4 * height - 1))
    wi = draw(st.integers(min_value=1, max_value=4 * width - li))
    hi = draw(st.integers(min_value=1, max_value=4 * height - ti))
    return {'left': li / 4.0, 'top': ti / 4.0,
            'width': wi / 4.0, 'height': hi / 4.0}


@st.composite
def _in_bounds_vertices(draw, width, height):
    """3-6 in-bounds vertices; collinear, tiny, and self-intersecting
    polygons are deliberately allowed."""
    count = draw(st.integers(min_value=3, max_value=6))
    return [
        (_quarter(draw, 0, 4 * width), _quarter(draw, 0, 4 * height))
        for _ in range(count)
    ]


@st.composite
def _unconstrained_detections(draw, width, height, label_set):
    class_name = draw(st.sampled_from(label_set))
    if draw(st.booleans()):
        return {'class': class_name, 'geometry': 'box',
                'box': draw(_in_bounds_boxes(width, height))}
    return {'class': class_name, 'geometry': 'polygon',
            'vertices': draw(_in_bounds_vertices(width, height))}


@st.composite
def _unconstrained_cases(draw):
    width = draw(_dimensions)
    height = draw(_dimensions)
    label_set = draw(_label_sets)
    detections = draw(st.lists(
        _unconstrained_detections(width, height, label_set), max_size=6))
    return width, height, label_set, detections


@st.composite
def _polygon_cases(draw):
    """(width, height, vertices) with in-bounds validated vertices."""
    width = draw(_dimensions)
    height = draw(_dimensions)
    return width, height, draw(_in_bounds_vertices(width, height))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hull_box(detection):
    """The box a detection converts to in ObjectDetection: its own box,
    or the polygon's axis-aligned hull."""
    if detection['geometry'] == 'box':
        return detection['box']
    return polygon_bounding_box(detection['vertices'])


def _has_foreground(rle):
    """True when the RLE carries at least one foreground pixel — a lone
    count is the all-background emission for zero spans."""
    return len(rle.split()) > 1


# ---------------------------------------------------------------------------
# Property 9: Detection preservation
# ---------------------------------------------------------------------------

@given(case=_preservation_cases())
def test_detection_preservation(case):
    """**Feature: llm-auto-labeling, Property 9: Detection preservation**

    For every convertible detection list, Segmentation emits exactly one
    region per detection and ObjectDetection exactly one box per
    detection — `len(regions) == len(detections)` and
    `len(boxes) == len(detections)` — with class names and guidance
    order preserved, and detections sharing a class (generated as
    explicit same-class runs) never merged.

    **Validates: Requirements 5.1, 5.3**
    """
    width, height, label_set, detections = case
    expected_classes = [d['class'] for d in detections]

    segmentation = guidance_to_prelabel(
        detections, 'Segmentation', label_set, width, height)
    assert len(segmentation['regions']) == len(detections)
    assert [r['class'] for r in segmentation['regions']] == expected_classes
    assert segmentation['image_width'] == width
    assert segmentation['image_height'] == height

    detection_prelabel = guidance_to_prelabel(
        detections, 'ObjectDetection', label_set, width, height)
    assert len(detection_prelabel['boxes']) == len(detections)
    assert [b['class'] for b in detection_prelabel['boxes']] == expected_classes
    assert detection_prelabel['image_width'] == width
    assert detection_prelabel['image_height'] == height


# ---------------------------------------------------------------------------
# Property 10: Polygon hull tightness
# ---------------------------------------------------------------------------

@given(case=_polygon_cases())
def test_polygon_hull_tightness(case):
    """**Feature: llm-auto-labeling, Property 10: Polygon hull tightness**

    The axis-aligned hull of a polygon satisfies the exact min/max
    identity — `left == min x`, `top == min y`,
    `left + width == max x`, `top + height == max y` (exact on the
    quarter-pixel grid) — and the hull of in-bounds vertices is itself
    in bounds.

    **Validates: Requirements 5.3**
    """
    width, height, vertices = case

    hull = polygon_bounding_box(vertices)

    xs = [x for x, _ in vertices]
    ys = [y for _, y in vertices]
    assert hull['left'] == min(xs)
    assert hull['top'] == min(ys)
    assert hull['left'] + hull['width'] == max(xs)
    assert hull['top'] + hull['height'] == max(ys)

    assert hull['left'] >= 0
    assert hull['top'] >= 0
    assert hull['left'] + hull['width'] <= width
    assert hull['top'] + hull['height'] <= height


# ---------------------------------------------------------------------------
# Property 11: Empty is success, degenerate is failure
# ---------------------------------------------------------------------------

@given(width=_dimensions, height=_dimensions, label_set=_label_sets)
def test_empty_guidance_is_a_success(width, height, label_set):
    """**Feature: llm-auto-labeling, Property 11: Empty is success, degenerate is failure**

    Zero detections always convert successfully in every geometry
    modality: Segmentation emits an empty `regions` list and
    ObjectDetection an empty `boxes` list — never an error.

    **Validates: Requirements 5.5, 5.7**
    """
    segmentation = guidance_to_prelabel(
        [], 'Segmentation', label_set, width, height)
    assert segmentation['regions'] == []
    assert segmentation['image_width'] == width
    assert segmentation['image_height'] == height

    detection_prelabel = guidance_to_prelabel(
        [], 'ObjectDetection', label_set, width, height)
    assert detection_prelabel['boxes'] == []
    assert detection_prelabel['image_width'] == width
    assert detection_prelabel['image_height'] == height


@given(case=_unconstrained_cases())
def test_degenerate_is_failure_and_success_is_exhaustive(case):
    """**Feature: llm-auto-labeling, Property 11: Empty is success, degenerate is failure**

    Over "a detection produced nothing", the two outcomes are disjoint
    and exhaustive: conversion raises GuidanceError exactly when some
    detection produces nothing (rasterizes to zero foreground for
    Segmentation; int()-truncates to a zero-extent box for
    ObjectDetection), XOR it succeeds with every detection having
    contributed one element — never a silently dropped detection, never
    an empty region.

    **Validates: Requirements 5.5, 5.7**
    """
    width, height, label_set, detections = case

    # Segmentation: degenerate iff some detection covers no pixel center.
    seg_degenerate = any(
        not _has_foreground(rasterize_to_rle(d, width, height))
        for d in detections
    )
    try:
        segmentation = guidance_to_prelabel(
            detections, 'Segmentation', label_set, width, height)
    except GuidanceError:
        assert seg_degenerate, \
            'GuidanceError raised though every detection covers a pixel'
    else:
        assert not seg_degenerate, \
            'conversion succeeded though a detection covers no pixel'
        assert len(segmentation['regions']) == len(detections)
        for region in segmentation['regions']:
            assert _has_foreground(region['rle'])

    # ObjectDetection: degenerate iff some box (or polygon hull)
    # int()-truncates to zero width or height.
    od_degenerate = any(
        int(_hull_box(d)['width']) < 1 or int(_hull_box(d)['height']) < 1
        for d in detections
    )
    try:
        detection_prelabel = guidance_to_prelabel(
            detections, 'ObjectDetection', label_set, width, height)
    except GuidanceError:
        assert od_degenerate, \
            'GuidanceError raised though every box has whole-pixel extent'
    else:
        assert not od_degenerate, \
            'conversion succeeded though a box truncates to zero extent'
        assert len(detection_prelabel['boxes']) == len(detections)


# ---------------------------------------------------------------------------
# Property 12: Classification totality
# ---------------------------------------------------------------------------

@given(case=_unconstrained_cases())
def test_classification_totality(case):
    """**Feature: llm-auto-labeling, Property 12: Classification totality**

    Classification conversion is total over validated guidance —
    degenerate geometry never fails it — and the label satisfies the
    biconditional: `anomaly` iff the detection list is non-empty,
    `normal` iff it is empty.

    **Validates: Requirements 5.4**
    """
    width, height, label_set, detections = case

    result = guidance_to_prelabel(
        detections, 'Classification', label_set, width, height)

    assert result == {
        'modality': 'Classification',
        'label': 'anomaly' if detections else 'normal',
    }
    assert (result['label'] == 'anomaly') == bool(detections)
