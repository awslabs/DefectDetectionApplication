"""
Example-based unit tests for the LLM guidance parser core
(layers/shared/python/dda_llm_guidance.py): extract_first_json,
parse_guidance, and serialize_guidance.

Spec: llm-auto-labeling, task 2.2.
Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures — conftest.py already places the shared layer on
sys.path.
"""
import json

import pytest

from dda_llm_guidance import (
    GuidanceError,
    MAX_DETECTIONS,
    extract_first_json,
    parse_guidance,
    serialize_guidance,
)

WIDTH = 100
HEIGHT = 80
LABEL_SET = ['scratch', 'dent']


def _doc(*detections):
    """Wire-format guidance JSON text for the given detection entries."""
    return json.dumps({'detections': list(detections)})


def _box(left=10, top=20, width=30, height=25, cls='scratch'):
    return {'class': cls,
            'box': {'left': left, 'top': top, 'width': width, 'height': height}}


def _polygon(vertices=((10, 10), (40, 10), (25, 40)), cls='dent'):
    return {'class': cls, 'polygon': [list(v) for v in vertices]}


# ---------------------------------------------------------------------------
# extract_first_json (Requirements 4.1, 4.2)
# ---------------------------------------------------------------------------

class TestExtractFirstJson:
    def test_bare_object(self):
        assert extract_first_json('{"detections": []}') == {'detections': []}

    def test_fenced_json_block(self):
        text = 'Here is the result:\n```json\n{"detections": []}\n```\n'
        assert extract_first_json(text) == {'detections': []}

    def test_prose_before_and_after(self):
        text = ('I looked carefully at the image. '
                '{"detections": [{"class": "scratch"}]} '
                'Let me know if you need anything else.')
        assert extract_first_json(text) == {
            'detections': [{'class': 'scratch'}]}

    def test_truncated_object_before_valid_one_is_skipped(self):
        # The leading '{' never closes, so it fails to decode; the next
        # candidate in reading order is the one returned.
        text = '{"broken": [1, 2 ... and then {"detections": []} follows'
        assert extract_first_json(text) == {'detections': []}

    def test_multiple_valid_objects_first_in_reading_order_wins(self):
        text = 'first {"a": 1} then {"b": 2}'
        assert extract_first_json(text) == {'a': 1}

    def test_no_object_at_all_raises(self):
        with pytest.raises(GuidanceError, match='no parseable JSON object'):
            extract_first_json('nothing to see here, just prose')

    def test_top_level_json_array_rejected(self):
        # An array document contains no JSON *object* to extract.
        with pytest.raises(GuidanceError, match='no parseable JSON object'):
            extract_first_json('[1, 2, 3]')


# ---------------------------------------------------------------------------
# parse_guidance rejections (Requirements 4.2-4.8)
# ---------------------------------------------------------------------------

class TestParseGuidanceRejections:
    def _reject(self, text, reason_substring):
        with pytest.raises(GuidanceError) as excinfo:
            parse_guidance(text, LABEL_SET, WIDTH, HEIGHT)
        assert reason_substring in str(excinfo.value)

    # -- class name (Requirement 4.4)

    def test_class_not_in_label_set(self):
        self._reject(_doc(_box(cls='crack')), 'not in the Label_Set')

    def test_class_differing_only_in_case(self):
        self._reject(_doc(_box(cls='Scratch')), 'not in the Label_Set')

    def test_class_with_surrounding_whitespace_accepted_and_trimmed(self):
        detections = parse_guidance(
            _doc(_box(cls='  scratch \n')), LABEL_SET, WIDTH, HEIGHT)
        assert len(detections) == 1
        assert detections[0]['class'] == 'scratch'

    # -- box coordinates (Requirement 4.5)

    def test_non_numeric_coordinate(self):
        self._reject(_doc(_box(left='12')), 'not a finite number')

    def test_nan_coordinate(self):
        # Python's json module parses NaN, so it must be caught by
        # validation, not by extraction.
        self._reject(
            _doc(_box()).replace('"left": 10', '"left": NaN'),
            'not a finite number')

    def test_infinity_coordinate(self):
        self._reject(
            _doc(_box()).replace('"top": 20', '"top": Infinity'),
            'not a finite number')

    def test_true_as_coordinate(self):
        # bool is an int subclass in Python; it must still be rejected.
        self._reject(_doc(_box(width=True)), 'not a finite number')

    def test_zero_extent(self):
        self._reject(_doc(_box(width=0)), 'must be positive')

    def test_negative_extent(self):
        self._reject(_doc(_box(height=-5)), 'must be positive')

    def test_box_overflows_left_bound(self):
        self._reject(_doc(_box(left=-1)), 'outside the image bounds')

    def test_box_overflows_top_bound(self):
        self._reject(_doc(_box(top=-0.5)), 'outside the image bounds')

    def test_box_overflows_right_bound(self):
        self._reject(_doc(_box(left=80, width=21)), 'outside the image bounds')

    def test_box_overflows_bottom_bound(self):
        self._reject(_doc(_box(top=60, height=21)), 'outside the image bounds')

    # -- polygon (Requirement 4.6)

    def test_polygon_with_two_vertices(self):
        self._reject(
            _doc(_polygon(vertices=((0, 0), (10, 10)))), 'at least 3')

    def test_polygon_vertex_outside_bounds(self):
        self._reject(
            _doc(_polygon(vertices=((0, 0), (WIDTH + 1, 0), (10, 10)))),
            'outside the image bounds')

    # -- cardinality (Requirement 4.7)

    def test_101_detections_rejected_with_cap_reason(self):
        detections = [_box() for _ in range(MAX_DETECTIONS + 1)]
        self._reject(_doc(*detections), f'at most {MAX_DETECTIONS}')

    # -- structure (Requirement 4.3)

    def test_both_box_and_polygon_present(self):
        entry = _box()
        entry['polygon'] = [[0, 0], [10, 0], [10, 10]]
        self._reject(_doc(entry), 'exactly one of')

    def test_neither_box_nor_polygon_present(self):
        self._reject(_doc({'class': 'scratch'}), 'exactly one of')

    def test_detections_missing(self):
        self._reject('{"results": []}', "'detections' must be a list")

    def test_detections_not_a_list(self):
        self._reject('{"detections": {"class": "scratch"}}',
                      "'detections' must be a list")


# ---------------------------------------------------------------------------
# parse_guidance acceptance (Requirements 4.5, 4.6, 4.9)
# ---------------------------------------------------------------------------

class TestParseGuidanceAcceptance:
    def test_empty_detections_is_valid_empty_result(self):
        assert parse_guidance(
            '{"detections": []}', LABEL_SET, WIDTH, HEIGHT) == []

    def test_box_touching_bounds_exactly(self):
        # left + width == WIDTH and top + height == HEIGHT are in bounds.
        detections = parse_guidance(
            _doc(_box(left=0, top=0, width=WIDTH, height=HEIGHT)),
            LABEL_SET, WIDTH, HEIGHT)
        assert detections == [{
            'class': 'scratch',
            'geometry': 'box',
            'box': {'left': 0.0, 'top': 0.0,
                    'width': float(WIDTH), 'height': float(HEIGHT)},
        }]

    def test_polygon_touching_bounds_exactly(self):
        # Vertices with x == WIDTH and y == HEIGHT are in bounds.
        detections = parse_guidance(
            _doc(_polygon(vertices=((0, 0), (WIDTH, 0), (WIDTH, HEIGHT)))),
            LABEL_SET, WIDTH, HEIGHT)
        assert detections == [{
            'class': 'dent',
            'geometry': 'polygon',
            'vertices': [(0.0, 0.0), (float(WIDTH), 0.0),
                         (float(WIDTH), float(HEIGHT))],
        }]

    def test_document_mixing_box_and_polygon_detections(self):
        detections = parse_guidance(
            _doc(_box(cls='scratch'), _polygon(cls='dent')),
            LABEL_SET, WIDTH, HEIGHT)
        assert [d['geometry'] for d in detections] == ['box', 'polygon']
        assert [d['class'] for d in detections] == ['scratch', 'dent']


# ---------------------------------------------------------------------------
# serialize_guidance (wire-format counterpart of parse_guidance)
# ---------------------------------------------------------------------------

class TestSerializeGuidance:
    def test_serialized_form_matches_wire_format(self):
        detections = parse_guidance(
            _doc(_box(), _polygon()), LABEL_SET, WIDTH, HEIGHT)
        document = json.loads(serialize_guidance(detections))
        assert document == {'detections': [
            {'class': 'scratch',
             'box': {'left': 10.0, 'top': 20.0,
                     'width': 30.0, 'height': 25.0}},
            {'class': 'dent',
             'polygon': [[10.0, 10.0], [40.0, 10.0], [25.0, 40.0]]},
        ]}

    def test_serialized_output_reparses_to_the_same_detections(self):
        original = parse_guidance(
            _doc(_box(), _polygon()), LABEL_SET, WIDTH, HEIGHT)
        reparsed = parse_guidance(
            serialize_guidance(original), LABEL_SET, WIDTH, HEIGHT)
        assert reparsed == original
