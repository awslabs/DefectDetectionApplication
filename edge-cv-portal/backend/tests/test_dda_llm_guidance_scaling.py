"""
Example-based unit tests for the Sent -> Source coordinate scale-back
(layers/shared/python/dda_llm_guidance.py :: scale_detections), plus its
handoff to the pre-existing guidance_to_prelabel GuidanceError on the
sub-pixel-collapse edge.

Spec: llm-model-token-and-image-sizing, task 2.5.
Requirements: 7.3, 7.4, 7.5, 9.3, 9.6

Both functions under test are pure (no boto3, no Pillow, no I/O), so
these tests need no moto fixtures — conftest.py already places the
shared layer on sys.path. The generative sweep lives in the sibling
property test (test_property_coordinate_space.py); these are the named
examples the design calls out, with hand-computed expectations.

The mapping rule under test (Requirement 7.3, then 7.4):

    scaled = floor(v * source_extent / sent_extent + 0.5)   # round-half-up
    result = min(source_extent, max(0, scaled))             # clamp
"""
import copy
import json

import pytest

from dda_llm_guidance import (
    GuidanceError,
    guidance_to_prelabel,
    parse_guidance,
    scale_detections,
)


def _box(left, top, width, height, class_name='scratch'):
    """One validated-shape box detection (the internal Detection model)."""
    return {
        'class': class_name,
        'geometry': 'box',
        'box': {'left': float(left), 'top': float(top),
                'width': float(width), 'height': float(height)},
    }


def _polygon(vertices, class_name='scratch'):
    """One validated-shape polygon detection."""
    return {
        'class': class_name,
        'geometry': 'polygon',
        'vertices': [(float(x), float(y)) for x, y in vertices],
    }


# ---------------------------------------------------------------------------
# The genuine no-op (Requirement 7.5)
# ---------------------------------------------------------------------------

class TestIdentityNoOp:
    def test_equal_dimension_pairs_return_the_same_list_object(self):
        detections = [_box(10, 20, 30, 40)]
        before = copy.deepcopy(detections)

        result = scale_detections(detections, 1024, 682, 1024, 682)

        # The same list object — an early return, not a multiply by 1.0,
        # so the downstream Pre_Label is bit-for-bit the pre-feature one.
        assert result is detections
        assert detections == before

    def test_empty_list_identity(self):
        detections = []
        assert scale_detections(detections, 640, 480, 640, 480) is detections

    def test_non_positive_sent_extent_returns_the_same_list_object(self):
        # The other arm of the early return: a degenerate sent extent
        # never divides, never scales.
        detections = [_box(0, 0, 1, 1)]
        assert scale_detections(detections, 0, 480, 640, 480) is detections
        assert scale_detections(detections, 640, 0, 640, 480) is detections


# ---------------------------------------------------------------------------
# Round-half-up at exactly .5 (Requirement 7.3)
# ---------------------------------------------------------------------------

class TestRoundHalfUp:
    def test_half_rounds_up_where_bankers_rounding_would_go_down(self):
        # sent 2x2 -> source 5x5: vertex (1, 1) maps to 1 * 5 / 2 = 2.5
        # on both axes. Python's round() is banker's rounding and maps
        # 2.5 to the even neighbour 2; the contract floor(2.5 + 0.5) = 3.
        assert round(2.5) == 2  # the pitfall the formula avoids

        polygon = _polygon([(0, 0), (1, 1), (0, 1)])
        scaled = scale_detections([polygon], 2, 2, 5, 5)[0]

        assert scaled['vertices'] == [(0.0, 0.0), (3.0, 3.0), (0.0, 3.0)]

    def test_half_rounds_up_at_an_odd_result_too(self):
        # sent 2x2 -> source 3x3: 1 * 3 / 2 = 1.5 -> 2 (both roundings
        # agree here; pinned so the .5 boundary is covered on each side).
        polygon = _polygon([(0, 0), (1, 1), (0, 1)])
        scaled = scale_detections([polygon], 2, 2, 3, 3)[0]

        assert scaled['vertices'] == [(0.0, 0.0), (2.0, 2.0), (0.0, 2.0)]


# ---------------------------------------------------------------------------
# Clamping into [0, source_extent] (Requirement 7.4)
# ---------------------------------------------------------------------------

class TestClamping:
    def test_coordinates_clamped_at_both_ends(self):
        # scale_detections does not validate its input, so raw
        # out-of-range coordinates exercise the clamp directly — the
        # safety net behind the validated path.
        # x = -3: floor(-3 * 2 + 0.5) = floor(-5.5) = -6 -> clamped to 0
        # y = 12: floor(12 * 2 + 0.5) = 24            -> clamped to 20
        polygon = _polygon([(-3.0, 12.0), (5.0, 5.0), (0.0, 0.0)])
        scaled = scale_detections([polygon], 10, 10, 20, 20)[0]

        assert scaled['vertices'] == [(0.0, 20.0), (10.0, 10.0), (0.0, 0.0)]


# ---------------------------------------------------------------------------
# Boxes mapped as two corners (Requirements 7.3, 7.4)
# ---------------------------------------------------------------------------

class TestBoxCornerMapping:
    def test_box_right_edge_lands_exactly_on_the_source_width(self):
        # The design's worked pair: sent 1024x682 -> source 3000x2000.
        # A box whose right/bottom edges lie on the sent extents maps by
        # its two corners — (left, top) and (left + width, top + height)
        # — with the extents re-derived as differences:
        #   left   512 -> floor(1500.0 + 0.5) = 1500
        #   right 1024 -> floor(3000.0 + 0.5) = 3000 (clamped at 3000)
        #   top    341 -> floor(1000.0 + 0.5) = 1000
        #   bottom 682 -> floor(2000.0 + 0.5) = 2000 (clamped at 2000)
        detection = _box(512, 341, 512, 341)
        scaled = scale_detections([detection], 1024, 682, 3000, 2000)[0]

        assert scaled['box'] == {'left': 1500.0, 'top': 1000.0,
                                 'width': 1500.0, 'height': 1000.0}
        # The right edge sits exactly on the source width: in bounds,
        # not beyond it, so the pre-existing converter accepts the box.
        assert scaled['box']['left'] + scaled['box']['width'] == 3000.0

        prelabel = guidance_to_prelabel([scaled], 'ObjectDetection',
                                        ['scratch'], 3000, 2000)
        assert prelabel['boxes'] == [{'class': 'scratch',
                                      'left': 1500.0, 'top': 1000.0,
                                      'width': 1500.0, 'height': 1000.0}]


# ---------------------------------------------------------------------------
# Polygon vertices at the origin and at the extent (Requirements 7.3, 7.4)
# ---------------------------------------------------------------------------

class TestPolygonCornerMapping:
    def test_vertices_at_origin_and_extent_map_to_the_source_corners(self):
        polygon = _polygon([(0, 0), (1024, 682), (0, 682)])
        scaled = scale_detections([polygon], 1024, 682, 3000, 2000)[0]

        assert scaled['vertices'] == [(0.0, 0.0), (3000.0, 2000.0),
                                      (0.0, 2000.0)]


# ---------------------------------------------------------------------------
# The sub-pixel collapse (Requirements 9.3, 9.6)
# ---------------------------------------------------------------------------

class TestSubPixelCollapse:
    def test_collapse_raises_the_pre_existing_guidance_error(self):
        # The design's named near-equal pair: sent 1000x1000 against
        # source 1001x1001, a scale factor a hair above 1. A
        # 0.2-pixel-wide box IS valid in Sent space — the premise is
        # enforced by running the real Sent-space validator — but both
        # of its x corners round to 11 in Source space:
        #   left  10.6 -> floor(10.6106 + 0.5) = 11
        #   right 10.8 -> floor(10.8108 + 0.5) = 11    => width 0
        raw = json.dumps({'detections': [
            {'class': 'scratch',
             'box': {'left': 10.6, 'top': 100, 'width': 0.2, 'height': 50}},
        ]})
        detections = parse_guidance(raw, ['scratch'], 1000, 1000)

        scaled = scale_detections(detections, 1000, 1000, 1001, 1001)

        assert scaled is not detections  # the mapping genuinely ran
        assert scaled[0]['box']['width'] == 0.0

        with pytest.raises(GuidanceError) as excinfo:
            guidance_to_prelabel(scaled, 'ObjectDetection', ['scratch'],
                                 1001, 1001)

        # The pre-existing message, character for character: no new
        # failure category and no new reason string for this edge.
        assert str(excinfo.value) == (
            "detection 0 ('scratch') converts to a bounding box with "
            "zero width or height")
