"""
Property-based test for manifest indistinguishability (llm-auto-labeling,
task 16.2).

**Feature: llm-auto-labeling, Property 14: Manifest indistinguishability**
**Validates: Requirements 8.2, 8.3, 8.4**

For each modality, an annotation is generated and serialized twice with
`dda_manifest.serialize_manifest`:

- once as the LLM-origin record — `dda_llm_guidance.guidance_to_prelabel`
  output (the exact shape the auto-label worker writes to the artifacts
  bucket) normalized through the worker's real `_canonical_annotation`
  bridge (image_width/image_height -> image_size, task 8), and
- once as an equivalent record of another origin — the same annotation
  written directly in the canonical modality-tagged model, the shape a
  labeler submission persists.

The emitted JSON Lines entries must be byte-identical, have identical
attribute names and structure at every nesting level, carry no
LLM-specific key anywhere, and pass the worker's existing validation gate
(`_validate_manifest_lines`) without transformation. For Segmentation,
`render_mask_png` must produce byte-identical PNG output given identical
RLE regions and the same job-wide color map.

The worker module is imported inside the moto mock (module fixture, same
convention as test_dda_labeling_worker_canonical_annotation.py) only for
its pure helpers `_canonical_annotation` and `_validate_manifest_lines`;
no AWS call is made.

Generator note: coordinates are drawn on the exact quarter-pixel grid
(i / 4.0) and every detection is guaranteed to cover at least one whole
pixel, so conversion never fails on degenerate geometry and the property
is exercised on successfully converted Pre_Labels only.
"""
import json
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dda_llm_guidance import guidance_to_prelabel
from dda_manifest import build_color_map, render_mask_png, serialize_manifest


@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_labeling_worker imported inside the moto mock."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling_worker

    return dda_labeling_worker


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Image dimensions in pixels. Kept small: every Segmentation example
# rasterizes, RLE-decodes, and PNG-renders O(width * height) pixels.
_dimensions = st.integers(min_value=4, max_value=32)

# Class names: letters/digits only, so every name equals its .strip()
# and build_color_map's distinct/non-empty constraints hold.
_class_names = st.text(
    alphabet=st.characters(
        whitelist_categories=('Lu', 'Ll', 'Nd'),
        whitelist_characters='_-',
        max_codepoint=0x24F,
    ),
    min_size=1,
    max_size=12,
)

# 1-4 distinct classes (build_color_map supports up to 10).
_label_sets = st.lists(_class_names, min_size=1, max_size=4, unique=True)


@st.composite
def _covering_boxes(draw, width, height):
    """An in-bounds quarter-grid box containing the whole pixel
    [px, px+1) x [py, py+1), so it always rasterizes to foreground and
    its int()-truncated extent is >= 1 in both axes."""
    px = draw(st.integers(min_value=0, max_value=width - 1))
    py = draw(st.integers(min_value=0, max_value=height - 1))
    li = draw(st.integers(min_value=0, max_value=4 * px))
    ti = draw(st.integers(min_value=0, max_value=4 * py))
    ri = draw(st.integers(min_value=4 * (px + 1), max_value=4 * width))
    bi = draw(st.integers(min_value=4 * (py + 1), max_value=4 * height))
    return {'left': li / 4.0, 'top': ti / 4.0,
            'width': (ri - li) / 4.0, 'height': (bi - ti) / 4.0}


@st.composite
def _covering_detections(draw, width, height, label_set):
    """One convertible Detection: a covering box, or the same rectangle
    as a 4-vertex polygon (either winding order)."""
    class_name = draw(st.sampled_from(label_set))
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
def _cases(draw):
    """(width, height, label_set, detections, human_annotated): a
    convertible detection list (possibly empty — an empty Pre_Label is
    a success and must serialize indistinguishably too) plus the
    record-level origin flag, held identical across both records."""
    width = draw(_dimensions)
    height = draw(_dimensions)
    label_set = draw(_label_sets)
    detections = draw(st.lists(
        _covering_detections(width, height, label_set), max_size=4))
    human_annotated = draw(st.booleans())
    return width, height, label_set, detections, human_annotated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOURCE_REF = 's3://dataset-bucket/images/img-0001.png'
_MASK_URI = 's3://output-bucket/labeled/job-p14/masks/img-0001.png'
_CREATION_DATE = '2026-01-01T00:00:00Z'

# No manifest key may hint at the annotation's origin (Requirement 8.4).
_LLM_KEY_MARKERS = ('llm', 'prompt', 'guidance', 'model_id',
                    'detection_prompt', 'auto_label')


def _record(annotation, human_annotated, mask_uri=None):
    """An AnnotationRecord whose metadata is identical across origins —
    only the annotation construction path differs."""
    record = {
        'source_ref': _SOURCE_REF,
        'annotation': annotation,
        'human_annotated': human_annotated,
        'creation_date': _CREATION_DATE,
    }
    if mask_uri is not None:
        record['mask_s3_uri'] = mask_uri
    return record


def _structure(value):
    """The entry's shape: attribute names at every nesting level with
    leaf value types, ignoring leaf values."""
    if isinstance(value, dict):
        return {key: _structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_structure(item) for item in value]
    return type(value).__name__


def _all_keys(value):
    """Every dict key at every nesting level."""
    keys = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys.update(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_keys(item))
    return keys


def _assert_indistinguishable(llm_line, other_line, modality, worker):
    """The shared Property 14 assertions over one entry pair."""
    llm_entry = json.loads(llm_line)
    other_entry = json.loads(other_line)

    # Identical attribute names and structure (Requirement 8.4) — and,
    # stronger, byte-identical emission: serialize_manifest has no
    # notion of Pre_Label origin.
    assert _structure(llm_entry) == _structure(other_entry)
    assert llm_line == other_line

    # No LLM-specific key at any nesting level (Requirement 8.4).
    for key in _all_keys(llm_entry):
        lowered = key.lower()
        assert not any(marker in lowered for marker in _LLM_KEY_MARKERS), \
            f'LLM-specific key {key!r} leaked into the manifest entry'

    # The existing validation gate accepts the entry untransformed
    # (Requirement 8.2).
    assert worker._validate_manifest_lines([llm_line], modality) == []


# ---------------------------------------------------------------------------
# Property 14: Manifest indistinguishability
# ---------------------------------------------------------------------------

@given(case=_cases())
def test_classification_entries_indistinguishable(case, worker):
    """**Feature: llm-auto-labeling, Property 14: Manifest indistinguishability**

    A Classification manifest entry serialized from an LLM-origin
    Pre_Label is byte-identical to one serialized from an equivalent
    annotation of another origin, carries no LLM-specific attribute,
    and passes the existing validation gate untransformed.

    **Validates: Requirements 8.2, 8.4**
    """
    width, height, label_set, detections, human_annotated = case
    job = {'job_name': 'job-p14', 'modality': 'Classification',
           'label_set': ['normal', 'anomaly']}

    llm_annotation = worker._canonical_annotation(
        guidance_to_prelabel(
            detections, 'Classification', label_set, width, height),
        'Classification')
    other_annotation = {'modality': 'Classification',
                        'label': llm_annotation['label']}

    llm_line, = serialize_manifest(
        [_record(llm_annotation, human_annotated)], job)
    other_line, = serialize_manifest(
        [_record(other_annotation, human_annotated)], job)

    _assert_indistinguishable(llm_line, other_line, 'Classification', worker)


@given(case=_cases())
def test_object_detection_entries_indistinguishable(case, worker):
    """**Feature: llm-auto-labeling, Property 14: Manifest indistinguishability**

    An ObjectDetection manifest entry serialized from an LLM-origin
    Pre_Label (through the worker's real image_size normalization) is
    byte-identical to one serialized from the equivalent canonical
    annotation of another origin, carries no LLM-specific attribute,
    and passes the existing validation gate untransformed.

    **Validates: Requirements 8.2, 8.4**
    """
    width, height, label_set, detections, human_annotated = case
    job = {'job_name': 'job-p14', 'modality': 'ObjectDetection',
           'label_set': label_set}

    llm_annotation = worker._canonical_annotation(
        guidance_to_prelabel(
            detections, 'ObjectDetection', label_set, width, height),
        'ObjectDetection')
    other_annotation = {
        'modality': 'ObjectDetection',
        'image_size': {'width': width, 'height': height},
        'boxes': [dict(box) for box in llm_annotation['boxes']],
    }

    llm_line, = serialize_manifest(
        [_record(llm_annotation, human_annotated)], job)
    other_line, = serialize_manifest(
        [_record(other_annotation, human_annotated)], job)

    _assert_indistinguishable(llm_line, other_line, 'ObjectDetection', worker)


@given(case=_cases())
def test_segmentation_entries_and_masks_indistinguishable(case, worker):
    """**Feature: llm-auto-labeling, Property 14: Manifest indistinguishability**

    A Segmentation manifest entry serialized from an LLM-origin
    Pre_Label is byte-identical to one serialized from the equivalent
    canonical annotation of another origin, carries no LLM-specific
    attribute, and passes the existing validation gate untransformed —
    and `render_mask_png` emits byte-identical PNG output given
    identical RLE regions and the same job-wide color map.

    **Validates: Requirements 8.2, 8.3, 8.4**
    """
    width, height, label_set, detections, human_annotated = case
    color_map = build_color_map(label_set)
    job = {'job_name': 'job-p14', 'modality': 'Segmentation',
           'label_set': label_set, 'color_map': color_map}

    llm_annotation = worker._canonical_annotation(
        guidance_to_prelabel(
            detections, 'Segmentation', label_set, width, height),
        'Segmentation')
    equivalent_regions = [dict(region)
                          for region in llm_annotation['regions']]
    other_annotation = {
        'modality': 'Segmentation',
        'image_size': {'width': width, 'height': height},
        'regions': equivalent_regions,
    }

    llm_line, = serialize_manifest(
        [_record(llm_annotation, human_annotated, _MASK_URI)], job)
    other_line, = serialize_manifest(
        [_record(other_annotation, human_annotated, _MASK_URI)], job)

    _assert_indistinguishable(llm_line, other_line, 'Segmentation', worker)

    # Requirement 8.3: identical RLE + identical color map -> pixel- and
    # byte-identical rendered mask, regardless of the regions' origin.
    llm_png = render_mask_png(
        llm_annotation['regions'], width, height, color_map)
    other_png = render_mask_png(
        equivalent_regions, width, height, color_map)
    assert llm_png == other_png
