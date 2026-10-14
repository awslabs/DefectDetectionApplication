"""
Example-based unit tests for the modality converter
(layers/shared/python/dda_llm_guidance.py): guidance_to_prelabel and
polygon_bounding_box.

Spec: llm-auto-labeling, task 4.2.
Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.7

Semantics under test: Segmentation emits one RLE region per detection
in guidance order (never merged across a shared class) with
image_width/image_height matching the source dimensions; ObjectDetection
keeps validated box coordinates verbatim and collapses polygons to their
axis-aligned hull, rejecting boxes whose int()-truncated extent is zero;
Classification derives 'anomaly' from one-or-more detections and
'normal' from zero. Empty guidance is a success for all three
modalities; degenerate geometry (zero rasterized spans, sub-pixel box
extent) raises GuidanceError naming the offending detection.

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures — conftest.py already places the shared layer on
sys.path.
"""
import pytest

import dda_manifest

from dda_llm_guidance import (
    GuidanceError,
    guidance_to_prelabel,
    polygon_bounding_box,
    rasterize_to_rle,
)

LABEL_SET = ['scratch', 'dent']
BINARY_LABEL_SET = ['normal', 'anomaly']


def _box_detection(left, top, width, height, cls='scratch'):
    return {'class': cls, 'geometry': 'box',
            'box': {'left': float(left), 'top': float(top),
                    'width': float(width), 'height': float(height)}}


def _polygon_detection(vertices, cls='dent'):
    return {'class': cls, 'geometry': 'polygon',
            'vertices': [tuple(v) for v in vertices]}


# ---------------------------------------------------------------------------
# Segmentation (Requirements 5.1, 5.2)
# ---------------------------------------------------------------------------

class TestSegmentationConversion:
    def test_two_same_class_detections_stay_two_regions(self):
        # Two disjoint boxes sharing a class are never merged into one
        # region (Requirement 5.1).
        detections = [
            _box_detection(0, 0, 2, 2, cls='scratch'),
            _box_detection(4, 4, 2, 2, cls='scratch'),
        ]
        prelabel = guidance_to_prelabel(
            detections, 'Segmentation', LABEL_SET, 8, 8)
        assert prelabel['modality'] == 'Segmentation'
        assert len(prelabel['regions']) == 2
        assert [r['class'] for r in prelabel['regions']] == \
            ['scratch', 'scratch']
        # The two regions carry distinct masks — proof they were not merged.
        assert prelabel['regions'][0]['rle'] != prelabel['regions'][1]['rle']

    def test_region_order_matches_guidance_order(self):
        detections = [
            _box_detection(0, 0, 1, 1, cls='dent'),
            _polygon_detection([(2, 2), (6, 2), (2, 6)], cls='scratch'),
            _box_detection(6, 6, 2, 2, cls='dent'),
        ]
        prelabel = guidance_to_prelabel(
            detections, 'Segmentation', LABEL_SET, 8, 8)
        assert [r['class'] for r in prelabel['regions']] == \
            ['dent', 'scratch', 'dent']
        # Each region's RLE is exactly the rasterization of the detection
        # at the same guidance position.
        for region, detection in zip(prelabel['regions'], detections):
            assert region['rle'] == rasterize_to_rle(detection, 8, 8)

    def test_image_dimensions_match_source(self):
        prelabel = guidance_to_prelabel(
            [_box_detection(1, 1, 3, 2)], 'Segmentation', LABEL_SET, 7, 5)
        assert prelabel['image_width'] == 7
        assert prelabel['image_height'] == 5

    def test_regions_decode_via_manifest_rle(self):
        # The emitted RLE is in the existing Pre_Label format:
        # dda_manifest.rle_decode accepts it (Requirement 5.2).
        prelabel = guidance_to_prelabel(
            [_box_detection(1, 1, 2, 2)], 'Segmentation', LABEL_SET, 4, 4)
        mask = dda_manifest.rle_decode(prelabel['regions'][0]['rle'], 4, 4)
        assert sum(mask) == 4  # the 2x2 box

    def test_empty_guidance_is_success_with_empty_regions(self):
        # Requirement 5.5: zero detections emit an empty regions list.
        prelabel = guidance_to_prelabel(
            [], 'Segmentation', LABEL_SET, 10, 10)
        assert prelabel == {
            'modality': 'Segmentation',
            'regions': [],
            'image_width': 10,
            'image_height': 10,
        }

    def test_zero_span_detection_rejected_naming_detection(self):
        # Requirement 5.7: a sub-pixel box covers no pixel center and
        # rasterizes to zero spans — the whole conversion fails with a
        # reason naming the offending detection.
        detections = [
            _box_detection(0, 0, 2, 2, cls='scratch'),
            _box_detection(0.6, 0.6, 0.3, 0.3, cls='dent'),
        ]
        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(detections, 'Segmentation', LABEL_SET, 4, 4)
        assert 'detection 1' in str(excinfo.value)
        assert "'dent'" in str(excinfo.value)
        assert 'covers no pixel' in str(excinfo.value)

    def test_zero_span_polygon_rejected(self):
        # A tiny triangle missing every pixel center is also degenerate.
        detections = [
            _polygon_detection([(0.1, 0.1), (0.4, 0.1), (0.25, 0.4)]),
        ]
        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(detections, 'Segmentation', LABEL_SET, 4, 4)
        assert 'detection 0' in str(excinfo.value)


# ---------------------------------------------------------------------------
# ObjectDetection (Requirements 5.3, 5.7)
# ---------------------------------------------------------------------------

class TestObjectDetectionConversion:
    def test_box_coordinates_unchanged(self):
        # Requirement 5.3: box detections keep their validated
        # coordinates verbatim, including fractional values.
        prelabel = guidance_to_prelabel(
            [_box_detection(1.25, 2.5, 3.75, 4.5, cls='scratch')],
            'ObjectDetection', LABEL_SET, 10, 10)
        assert prelabel['modality'] == 'ObjectDetection'
        assert prelabel['boxes'] == [{
            'class': 'scratch',
            'left': 1.25,
            'top': 2.5,
            'width': 3.75,
            'height': 4.5,
        }]
        assert prelabel['image_width'] == 10
        assert prelabel['image_height'] == 10

    def test_polygon_collapses_to_expected_hull(self):
        # Triangle (2,1), (7,3), (4,6): hull is left=2, top=1,
        # width=7-2=5, height=6-1=5.
        prelabel = guidance_to_prelabel(
            [_polygon_detection([(2, 1), (7, 3), (4, 6)], cls='dent')],
            'ObjectDetection', LABEL_SET, 10, 10)
        assert prelabel['boxes'] == [{
            'class': 'dent',
            'left': 2.0,
            'top': 1.0,
            'width': 5.0,
            'height': 5.0,
        }]

    def test_box_order_and_classes_match_guidance_order(self):
        detections = [
            _box_detection(0, 0, 1, 1, cls='dent'),
            _polygon_detection([(2, 2), (6, 2), (2, 6)], cls='scratch'),
            _box_detection(6, 6, 2, 2, cls='dent'),
        ]
        prelabel = guidance_to_prelabel(
            detections, 'ObjectDetection', LABEL_SET, 8, 8)
        assert [b['class'] for b in prelabel['boxes']] == \
            ['dent', 'scratch', 'dent']

    def test_sub_pixel_wide_box_rejected_as_zero_extent(self):
        # Requirement 5.7: a 0.4px-wide box passes parse validation
        # (positive extent) but int(0.4) == 0, so the serialized manifest
        # box would have zero width — conversion must reject it.
        detections = [_box_detection(1, 1, 0.4, 2, cls='scratch')]
        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(
                detections, 'ObjectDetection', LABEL_SET, 10, 10)
        assert 'detection 0' in str(excinfo.value)
        assert "'scratch'" in str(excinfo.value)
        assert 'zero width or height' in str(excinfo.value)

    def test_sub_pixel_tall_box_rejected_as_zero_extent(self):
        detections = [_box_detection(1, 1, 2, 0.9, cls='dent')]
        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(
                detections, 'ObjectDetection', LABEL_SET, 10, 10)
        assert 'detection 0' in str(excinfo.value)
        assert 'zero width or height' in str(excinfo.value)

    def test_degenerate_polygon_hull_rejected(self):
        # A polygon whose hull is thinner than one pixel converts to a
        # zero-extent box and is rejected with the same reason.
        detections = [
            _polygon_detection([(1, 1), (1.4, 1), (1.2, 5)], cls='dent'),
        ]
        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(
                detections, 'ObjectDetection', LABEL_SET, 10, 10)
        assert 'detection 0' in str(excinfo.value)
        assert 'zero width or height' in str(excinfo.value)

    def test_empty_guidance_is_success_with_empty_boxes(self):
        # Requirement 5.5: zero detections emit an empty boxes list.
        prelabel = guidance_to_prelabel(
            [], 'ObjectDetection', LABEL_SET, 10, 10)
        assert prelabel == {
            'modality': 'ObjectDetection',
            'boxes': [],
            'image_width': 10,
            'image_height': 10,
        }


# ---------------------------------------------------------------------------
# Classification (Requirements 5.4, 5.5)
# ---------------------------------------------------------------------------

class TestClassificationConversion:
    def test_zero_detections_is_normal(self):
        prelabel = guidance_to_prelabel(
            [], 'Classification', BINARY_LABEL_SET, 10, 10)
        assert prelabel == {'modality': 'Classification', 'label': 'normal'}

    def test_one_detection_is_anomaly(self):
        prelabel = guidance_to_prelabel(
            [_box_detection(1, 1, 2, 2, cls='anomaly')],
            'Classification', BINARY_LABEL_SET, 10, 10)
        assert prelabel == {'modality': 'Classification', 'label': 'anomaly'}

    def test_many_detections_is_anomaly(self):
        detections = [
            _box_detection(0, 0, 1, 1, cls='anomaly'),
            _polygon_detection([(2, 2), (6, 2), (2, 6)], cls='anomaly'),
            _box_detection(6, 6, 2, 2, cls='anomaly'),
        ]
        prelabel = guidance_to_prelabel(
            detections, 'Classification', BINARY_LABEL_SET, 8, 8)
        assert prelabel == {'modality': 'Classification', 'label': 'anomaly'}


# ---------------------------------------------------------------------------
# polygon_bounding_box (Requirement 5.3)
# ---------------------------------------------------------------------------

class TestPolygonBoundingBox:
    def test_triangle_hull(self):
        assert polygon_bounding_box([(2, 1), (7, 3), (4, 6)]) == {
            'left': 2, 'top': 1, 'width': 5, 'height': 5}

    def test_hull_of_axis_aligned_rectangle_is_itself(self):
        assert polygon_bounding_box([(1, 2), (5, 2), (5, 4), (1, 4)]) == {
            'left': 1, 'top': 2, 'width': 4, 'height': 2}

    def test_fractional_vertices(self):
        assert polygon_bounding_box(
            [(0.5, 0.25), (3.5, 1.0), (2.0, 4.75)]) == {
            'left': 0.5, 'top': 0.25, 'width': 3.0, 'height': 4.5}
