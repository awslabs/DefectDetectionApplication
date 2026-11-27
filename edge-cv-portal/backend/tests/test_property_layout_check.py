# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Property and example tests for the shared Layout Check rules
(car-assembly-check task 5.3; design §1, DD3, DD5, DD23).

``workflow_core/analytics/layout.py`` decides the Layout Check verdict on
the device and in the cloud test sandbox, so its geometry is pinned
against oracles that share no code with it:

- the column sweep ``_region_intervals`` equals the pixel-centre rule of
  DD3, counted by brute force per pixel on frames up to 64 x 64, as
  interval lists: for random star-shaped polygons, for rings snapped to
  the half-pixel grid (vertices exactly on column centres) and for the
  worked cases of design §1;
- ``interval_iou``, ``interval_union``, ``interval_intersection_length``
  and ``outline_overlap`` equal set computations, and the outline is the
  pixel-centre union of the regions;
- ``rle_intervals`` decodes every vector of
  ``test/fixtures/canonical_rle_vectors.json`` and refuses the malformed
  forms;
- ``check_layout`` follows the status table of design §1 and the outline
  rule of DD23, and ``attach_detection_ids`` recovers the ids the device's
  ``detections.build_detection_list`` draws (DD5).

Requirements: 5.9, 12.2
"""
import ast
import copy
import json
import math
import os
import random
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.analytics import layout

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_SRC_BACKEND = os.path.join(_REPO, "src", "backend")
if _SRC_BACKEND not in sys.path:
    # Appended so the device tree can never shadow a portal layer module.
    sys.path.append(_SRC_BACKEND)

from workflow_engine import detections  # noqa: E402  (the device normalizer, DD5)

_RLE_VECTORS = os.path.join(_REPO, "test", "fixtures", "canonical_rle_vectors.json")


# ---------------------------------------------------------------------------
# Oracles (no code shared with the module under test)
# ---------------------------------------------------------------------------

def _oracle_pixels(polygon, width, height):
    """DD3's rule evaluated per pixel centre: a pixel is inside iff an odd
    number of edges with ``min_x <= cx < max_x`` cross ``X = cx`` at or
    above its centre. Each edge runs from its endpoint of smaller x."""
    scaled = [(x * width, y * height) for x, y in polygon]
    edges = []
    for index, (xa, ya) in enumerate(scaled):
        xb, yb = scaled[(index + 1) % len(scaled)]
        if xa == xb:
            continue
        if xa > xb:
            xa, ya, xb, yb = xb, yb, xa, ya
        edges.append((xa, ya, xb, yb))
    pixels = set()
    for x in range(width):
        cx = x + 0.5
        crossings = [y0 + (cx - x0) * (y1 - y0) / (x1 - x0)
                     for x0, y0, x1, y1 in edges if x0 <= cx < x1]
        for y in range(height):
            if sum(1 for crossing in crossings if crossing <= y + 0.5) % 2 == 1:
                pixels.add(x * height + y)
    return pixels


def _column_runs(pixels, width, height):
    """A pixel set as maximal runs per column (the sweep's list form)."""
    intervals = []
    for x in range(width):
        y = 0
        while y < height:
            if x * height + y not in pixels:
                y += 1
                continue
            start = y
            while y < height and x * height + y in pixels:
                y += 1
            intervals.append((x * height + start, x * height + y))
    return intervals


def _runs(pixels):
    """A pixel set as maximal runs of the flat column-major index."""
    intervals = []
    for index in sorted(pixels):
        if intervals and intervals[-1][1] == index:
            intervals[-1] = (intervals[-1][0], index + 1)
        else:
            intervals.append((index, index + 1))
    return intervals


def _pixels_of(intervals):
    return {index for start, end in intervals for index in range(start, end)}


def _rle(pixels, width, height):
    """The Canonical_RLE of a pixel set: column-major run lengths,
    background first."""
    counts = []
    value = 0
    run = 0
    for index in range(width * height):
        bit = 1 if index in pixels else 0
        if bit == value:
            run += 1
        else:
            counts.append(run)
            value = bit
            run = 1
    counts.append(run)
    return " ".join(str(count) for count in counts)


def _rect(x0, x1, y0, y1, height):
    """Pixels of columns ``x0 .. x1 - 1`` and rows ``y0 .. y1 - 1``."""
    return {x * height + y for x in range(x0, x1) for y in range(y0, y1)}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

@st.composite
def _star_polygons(draw, max_vertices=12):
    """A simple polygon: vertices at sorted, distinct angles around a
    centre (star-shaped), inside the unit square."""
    count = draw(st.integers(min_value=3, max_value=max_vertices))
    angles = sorted(draw(st.lists(
        st.floats(min_value=0.0, max_value=2 * math.pi, exclude_max=True),
        min_size=count, max_size=count, unique=True)))
    radii = draw(st.lists(st.floats(min_value=0.05, max_value=0.3),
                          min_size=count, max_size=count))
    centre_x = draw(st.floats(min_value=0.3, max_value=0.7))
    centre_y = draw(st.floats(min_value=0.3, max_value=0.7))
    return [(centre_x + radius * math.cos(angle), centre_y + radius * math.sin(angle))
            for angle, radius in zip(angles, radii)]


_frame_sides = st.integers(min_value=1, max_value=64)


@st.composite
def _snapped_rings(draw):
    """Any closed ring (simple or not) with vertices on the half-pixel grid
    of a power-of-two frame, so vertices fall exactly on column centres and
    pixel boundaries."""
    width = draw(st.sampled_from([4, 8, 16, 32, 64]))
    height = draw(st.sampled_from([4, 8, 16, 32, 64]))
    count = draw(st.integers(min_value=3, max_value=8))
    ring = [(draw(st.integers(0, 2 * width)) / (2 * width),
             draw(st.integers(0, 2 * height)) / (2 * height))
            for _ in range(count)]
    return ring, width, height


_interval_lists = st.lists(
    st.tuples(st.integers(min_value=0, max_value=60),
              st.integers(min_value=0, max_value=12)).map(
        lambda pair: (pair[0], pair[0] + pair[1])),
    max_size=8)


# ---------------------------------------------------------------------------
# _region_intervals: the column sweep against the pixel-centre oracle
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(polygon=_star_polygons(), width=_frame_sides, height=_frame_sides)
def test_sweep_equals_the_pixel_centre_oracle_on_star_polygons(polygon, width, height):
    expected = _column_runs(_oracle_pixels(polygon, width, height), width, height)
    assert layout._region_intervals(polygon, width, height) == expected


@settings(max_examples=100, deadline=None)
@given(case=_snapped_rings())
def test_sweep_equals_the_pixel_centre_oracle_on_grid_snapped_rings(case):
    ring, width, height = case
    expected = _column_runs(_oracle_pixels(ring, width, height), width, height)
    assert layout._region_intervals(ring, width, height) == expected


@settings(max_examples=100, deadline=None)
@given(polygon=_star_polygons(), width=_frame_sides, height=_frame_sides)
def test_sweep_does_not_depend_on_the_ring_direction(polygon, width, height):
    assert layout._region_intervals(list(reversed(polygon)), width, height) == \
        layout._region_intervals(polygon, width, height)


def test_worked_rectangle_on_8_by_8():
    rectangle = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    assert layout._region_intervals(rectangle, 8, 8) == [
        (2 * 8 + 2, 2 * 8 + 6), (3 * 8 + 2, 3 * 8 + 6),
        (4 * 8 + 2, 4 * 8 + 6), (5 * 8 + 2, 5 * 8 + 6),
    ]


def test_triangle_with_a_horizontal_base_on_10_by_10():
    # The horizontal base is kept as a crossing at y = 0 for every column
    # it spans, so every column pairs evenly: 0+1+2+3+4+4+3+2+1+0 pixels.
    triangle = [[0, 0], [1, 0], [0.5, 0.5]]
    intervals = layout._region_intervals(triangle, 10, 10)
    assert sum(end - start for start, end in intervals) == 20
    assert intervals == _column_runs(_oracle_pixels(triangle, 10, 10), 10, 10)
    assert intervals[0] == (1 * 10, 1 * 10 + 1)


def test_vertex_exactly_on_a_column_centre():
    # A notch whose tip lies at x = 2.5 / 8, the centre of column 2: both
    # notch edges start there, so column 2 sees the crossings 0.8, 4, 4,
    # 7.2. The two pairs touch at row 4 and form one run, rows 1 to 6.
    notch = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.45], [2.5 / 8, 0.5],
             [0.9, 0.55], [0.9, 0.9], [0.1, 0.9]]
    intervals = layout._region_intervals(notch, 8, 8)
    assert intervals == _column_runs(_oracle_pixels(notch, 8, 8), 8, 8)
    assert [interval for interval in intervals if 16 <= interval[0] < 24] == [(17, 23)]


def test_convex_tip_on_a_column_centre_adds_no_pixel():
    tip = [[2.5 / 8, 0.5], [0.9, 0.1], [0.9, 0.9]]
    intervals = layout._region_intervals(tip, 8, 8)
    assert intervals == _column_runs(_oracle_pixels(tip, 8, 8), 8, 8)
    assert not [interval for interval in intervals if 16 <= interval[0] < 24]


def test_sweep_of_an_unusable_polygon_or_frame_is_empty():
    square = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    assert layout._region_intervals([[0, 0], [1, 1]], 8, 8) == []
    assert layout._region_intervals([[0, 0], [1, "a"], [1, 1]], 8, 8) == []
    assert layout._region_intervals(square, 0, 8) == []
    assert layout._region_intervals(square, 8, True) == []
    assert layout._region_intervals(None, 8, 8) == []


# ---------------------------------------------------------------------------
# Interval operations against set oracles
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(a=_interval_lists, b=_interval_lists)
def test_interval_iou_equals_the_set_oracle(a, b):
    first, second = _pixels_of(a), _pixels_of(b)
    union = first | second
    expected = len(first & second) / len(union) if union else 0.0
    assert layout.interval_iou(a, b) == expected


@settings(max_examples=100, deadline=None)
@given(lists=st.lists(_interval_lists, max_size=4))
def test_interval_union_equals_the_set_oracle(lists):
    pixels = set()
    for intervals in lists:
        pixels |= _pixels_of(intervals)
    assert layout.interval_union(lists) == _runs(pixels)


@settings(max_examples=100, deadline=None)
@given(a=_interval_lists, b=_interval_lists)
def test_interval_intersection_length_equals_the_set_oracle(a, b):
    assert layout.interval_intersection_length(a, b) == \
        len(_pixels_of(a) & _pixels_of(b))


def test_interval_union_merges_touching_and_overlapping_inputs():
    assert layout.interval_union([[(0, 3)], [(3, 5)]]) == [(0, 5)]
    assert layout.interval_union([[(4, 9), (0, 2)], [(1, 5)]]) == [(0, 9)]
    assert layout.interval_union([[(0, 0), (5, 4), ("a", 9), (True, 3)], None]) == []
    assert layout.interval_union("not a list") == []


def test_interval_iou_of_two_empty_lists_is_zero():
    assert layout.interval_iou([], []) == 0.0


@settings(max_examples=100, deadline=None)
@given(polygons=st.lists(_star_polygons(max_vertices=8), min_size=1, max_size=3),
       width=st.integers(min_value=1, max_value=32),
       height=st.integers(min_value=1, max_value=32),
       mask=_interval_lists)
def test_outline_overlap_equals_the_pixel_centre_oracle(polygons, width, height, mask):
    regions = {"class{0}".format(index): polygon for index, polygon in enumerate(polygons)}
    outline = layout.outline_intervals(regions, width, height)
    union = set()
    for polygon in polygons:
        union |= _oracle_pixels(polygon, width, height)
    assert _pixels_of(outline) == union
    assert outline == _runs(union)
    mask_pixels = _pixels_of(mask)
    expected = len(mask_pixels & union) / len(mask_pixels) if mask_pixels else None
    assert layout.outline_overlap(mask, outline) == expected


def test_outline_counts_touching_and_overlapping_regions_once():
    left = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
    right = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]
    middle = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]
    outline = layout.outline_intervals({"l": left, "r": right, "m": middle}, 8, 8)
    assert outline == [(0, 64)]
    assert layout.outline_overlap([(0, 64)], outline) == 1.0
    assert layout.outline_overlap([], outline) is None


# ---------------------------------------------------------------------------
# rle_intervals: the canonical vectors and the malformed forms
# ---------------------------------------------------------------------------

with open(_RLE_VECTORS, encoding="utf-8") as _fh:
    _VECTORS = json.load(_fh)["vectors"]


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_rle_intervals_decodes_every_canonical_vector(vector):
    rows = vector["mask_rows"]
    width, height = len(rows[0]), len(rows)
    assert (vector["width"], vector["height"]) == (width, height)
    pixels = {x * height + y for y, row in enumerate(rows)
              for x, bit in enumerate(row) if bit == "1"}
    intervals, problem = layout.rle_intervals(vector["rle"], width, height)
    assert problem is None
    assert intervals == _runs(pixels)
    assert _rle(pixels, width, height) == vector["rle"]


@pytest.mark.parametrize("rle, width, height, fragment", [
    ("1 2", 2, 2, "covers 3 pixels, not the 4 of the 2 x 2 frame"),
    ("", 2, 2, "covers 0 pixels"),
    ("2 3", 2, 2, "covers more than the 4 pixels"),
    ("1 a 2", 2, 2, "not a run length: 'a'"),
    ("1 -3", 2, 2, "not a run length: '-3'"),
    ("+4", 2, 2, "not a run length"),
    ("1.5 2.5", 2, 2, "not a run length"),
    ("\u00b2 2", 2, 2, "not a run length"),
    ("9999999999999 1", 2, 2, "run longer than the frame"),
    (None, 2, 2, "not a string"),
    ([0, 4], 2, 2, "not a string"),
    ("4", 0, 4, "frame size"),
    ("4", 2.5, 2, "frame size"),
])
def test_rle_intervals_refuses_malformed_masks(rle, width, height, fragment):
    intervals, problem = layout.rle_intervals(rle, width, height)
    assert intervals is None
    assert fragment in problem


def test_rle_intervals_accepts_leading_zeros_and_zero_runs():
    assert layout.rle_intervals("0000000000000000004", 2, 2) == ([], None)
    # A zero-length background run between two foreground runs: one run.
    assert layout.rle_intervals("0 2 0 2", 2, 2) == ([(0, 4)], None)
    assert layout.rle_intervals("1 2 1", 2.0, 2) == ([(1, 3)], None)


# ---------------------------------------------------------------------------
# position_score
# ---------------------------------------------------------------------------

_SQUARE = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]


def test_position_score_of_the_exact_rectangle_is_one():
    mask = _rle(_rect(2, 6, 2, 6, 8), 8, 8)
    assert layout.position_score(mask, 8, 8, _SQUARE) == (1.0, None)


def test_position_score_of_a_shifted_copy_is_the_analytic_iou():
    # Shifted one column: 12 shared pixels of 16 + 16 - 12 = 20.
    mask = _rle(_rect(3, 7, 2, 6, 8), 8, 8)
    assert layout.position_score(mask, 8, 8, _SQUARE) == (12 / 20, None)


def test_position_score_of_a_disjoint_mask_is_zero():
    mask = _rle(_rect(6, 8, 6, 8, 8), 8, 8)
    assert layout.position_score(mask, 8, 8, _SQUARE) == (0.0, None)


def test_position_score_reports_unusable_inputs():
    assert layout.position_score("64", 0, 8, _SQUARE) == (None, "frame size unknown")
    score, problem = layout.position_score("64", 8, 8, [[0, 0], [1, 1]])
    assert score is None and "polygon" in problem
    score, problem = layout.position_score("1 2", 8, 8, _SQUARE)
    assert score is None and problem.startswith("mask_rle covers 3 pixels")


# ---------------------------------------------------------------------------
# parse_expected_regions: the cyclic duplicate rule and normalization
# ---------------------------------------------------------------------------

def test_a_ring_repeating_its_first_vertex_last_is_read_as_closed():
    ring = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]]
    regions, problems = layout.parse_expected_regions(json.dumps({"roof": ring}))
    assert problems == []
    assert regions == {"roof": [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]}


def test_three_distinct_vertices_plus_the_repeated_first_are_accepted_as_three():
    ring = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9], [0.1, 0.1]]
    regions, problems = layout.parse_expected_regions({"roof": ring})
    assert problems == []
    assert regions == {"roof": [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]}


def test_the_vertex_count_is_taken_after_the_cyclic_drop():
    two = [[0.1, 0.1], [0.9, 0.9], [0.1, 0.1]]
    assert layout.parse_expected_regions({"roof": two}) == (None, [
        "Region 'roof': the polygon must have between 3 and 64 distinct points; "
        "this one has 2.",
    ])
    # 65 points whose last repeats the first: 64 remain, accepted.
    circle = [[0.5 + 0.4 * math.cos(2 * math.pi * i / 64),
               0.5 + 0.4 * math.sin(2 * math.pi * i / 64)] for i in range(64)]
    regions, problems = layout.parse_expected_regions({"roof": circle + [circle[0]]})
    assert problems == [] and len(regions["roof"]) == 64
    # Consecutive duplicates anywhere in the ring are dropped as well.
    stutter = [[0.1, 0.1], [0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.9, 0.9], [0.1, 0.9]]
    regions, problems = layout.parse_expected_regions({"roof": stutter})
    assert problems == [] and len(regions["roof"]) == 4


def test_labels_are_stripped_and_vertices_are_floats():
    regions, problems = layout.parse_expected_regions(
        '{" roof ": [[0, 0], [1, 0], [1, 1]], "rear_fender": [[0, 0], [1, 1], [0, 1]]}')
    assert problems == []
    assert list(regions) == ["roof", "rear_fender"]
    assert regions["roof"] == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert all(isinstance(value, float) for point in regions["roof"] for value in point)


@pytest.mark.parametrize("value", [None, "", "   \n"])
def test_a_blank_value_is_unusable_with_one_problem(value):
    assert layout.parse_expected_regions(value) == (None, ["No expected regions are set."])


# ---------------------------------------------------------------------------
# check_layout: the status table (design §1) on a 16 x 16 frame
# ---------------------------------------------------------------------------

_W = _H = 16
_ROOF = [[0.25, 0.0625], [0.75, 0.0625], [0.75, 0.375], [0.25, 0.375]]
_REAR = [[0.0625, 0.5], [0.4375, 0.5], [0.4375, 0.875], [0.0625, 0.875]]
_FRONT = [[0.5625, 0.5], [0.9375, 0.5], [0.9375, 0.875], [0.5625, 0.875]]
_REGIONS = {"roof": _ROOF, "front_fender": _FRONT, "rear_fender": _REAR}
_ROOF_PIXELS = _rect(4, 12, 1, 6, _H)
_REAR_PIXELS = _rect(1, 7, 8, 14, _H)
_FRONT_PIXELS = _rect(9, 15, 8, 14, _H)
#: Bottom-left corner: outside every region.
_CORNER_PIXELS = _rect(0, 2, 14, 16, _H)


def _regions(*labels):
    parsed, problems = layout.parse_expected_regions(
        {label: _REGIONS[label] for label in labels})
    assert problems == []
    return parsed


def _entry(entry_id, label, pixels=None, confidence=0.9, **extra):
    entry = {"id": entry_id, "class_label": label, "confidence": confidence,
             "bounding_box": [1.0, 2.0, 3.0, 4.0]}
    if pixels is not None:
        entry["mask_rle"] = _rle(pixels, _W, _H)
    entry.update(extra)
    return entry


def _correct():
    return [_entry("r1", "roof", _ROOF_PIXELS),
            _entry("f1", "front_fender", _FRONT_PIXELS),
            _entry("b1", "rear_fender", _REAR_PIXELS)]


def _check(entries, regions=None, frame=(_W, _H), **thresholds):
    if regions is None:
        regions = _regions("roof", "front_fender", "rear_fender")
    return layout.check_layout(entries, frame, regions, **thresholds)


def _statuses(result):
    return {label: piece["status"] for label, piece in result["pieces"].items()}


def test_worked_regions_rasterize_to_the_expected_pixels():
    assert _pixels_of(layout._region_intervals(_ROOF, _W, _H)) == _ROOF_PIXELS
    assert _pixels_of(layout._region_intervals(_REAR, _W, _H)) == _REAR_PIXELS
    assert _pixels_of(layout._region_intervals(_FRONT, _W, _H)) == _FRONT_PIXELS


def test_a_correct_assembly_passes():
    result = _check(_correct())
    assert result["pass"] is True
    assert result["outcome"] == "ok" and result["problems"] == []
    assert _statuses(result) == {"roof": "ok", "front_fender": "ok", "rear_fender": "ok"}
    assert result["flagged"] == [] and result["ignored"] == []
    assert result["pieces"]["roof"] == {"status": "ok", "iou": 1.0, "detections": [{
        "id": "r1", "class_label": "roof", "confidence": 0.9,
        "bounding_box": [1.0, 2.0, 3.0, 4.0], "iou": 1.0, "outline_overlap": 1.0}]}
    assert result["frame"] == {"width": 16, "height": 16}
    assert result["expected_regions"]["roof"] == _ROOF
    assert (result["min_iou"], result["min_confidence"], result["min_outline_overlap"]) == \
        (0.7, 0.5, 0.1)


def test_a_shifted_piece_is_misplaced_and_flagged():
    entries = _correct()
    # Three of its six columns stay in its pocket: IoU 18 / 54.
    entries[2] = _entry("b1", "rear_fender", _rect(4, 10, 8, 14, _H))
    result = _check(entries)
    assert result["pass"] is False and result["outcome"] == "ok"
    assert result["pieces"]["rear_fender"]["status"] == "misplaced"
    assert result["pieces"]["rear_fender"]["iou"] == round(18 / 54, 4)
    assert result["flagged"] == ["b1"]


def test_an_absent_class_is_missing():
    result = _check(_correct()[1:])
    assert result["pieces"]["roof"] == {"status": "missing", "iou": None, "detections": []}
    assert result["pass"] is False and result["outcome"] == "ok"
    assert result["flagged"] == []


def test_two_counted_instances_are_a_duplicate_and_both_flagged():
    entries = _correct() + [_entry("r2", "roof", _rect(4, 8, 1, 6, _H))]
    result = _check(entries)
    piece = result["pieces"]["roof"]
    assert piece["status"] == "duplicate"
    assert piece["iou"] == 1.0
    assert [detection["id"] for detection in piece["detections"]] == ["r1", "r2"]
    assert result["flagged"] == ["r1", "r2"]
    assert result["pass"] is False


@pytest.mark.parametrize("extra, reason", [
    ({}, "it carries no mask_rle"),
    ({"mask_truncated": True}, "its mask was truncated"),
    ({"mask_rle": "1 2"}, "mask_rle covers 3 pixels, not the 256 of the 16 x 16 frame"),
])
def test_an_entry_without_a_usable_mask_is_misplaced_with_a_warning(extra, reason):
    entries = _correct()
    entries[0] = _entry("r1", "roof", None, **extra)
    result = _check(entries)
    assert result["pieces"]["roof"]["status"] == "misplaced"
    assert result["pieces"]["roof"]["iou"] is None
    assert result["pieces"]["roof"]["detections"][0]["outline_overlap"] is None
    assert result["outcome"] == "warning"
    assert result["problems"] == [
        "detection r1 (roof) has no usable mask: {0}".format(reason)]
    assert result["flagged"] == ["r1"]
    assert result["pass"] is False


def test_a_zero_area_mask_is_counted_with_iou_zero():
    entries = _correct()
    entries[0] = _entry("r1", "roof", set())
    result = _check(entries)
    assert result["pieces"]["roof"]["status"] == "misplaced"
    assert result["pieces"]["roof"]["iou"] == 0.0
    assert result["pieces"]["roof"]["detections"][0]["outline_overlap"] is None
    assert result["ignored"] == []


@pytest.mark.parametrize("entries", [None, "detections", {"0": {}}, 3])
def test_entries_that_are_not_a_list_leave_every_piece_missing(entries):
    result = _check(entries)
    assert result["outcome"] == "warning"
    assert result["problems"] == ["no detections to check"]
    assert _statuses(result) == {"roof": "missing", "front_fender": "missing",
                                 "rear_fender": "missing"}
    assert all(piece["detections"] == [] for piece in result["pieces"].values())
    assert result["pass"] is False


@pytest.mark.parametrize("frame", [None, (0, 16), (16, -1), {"width": "a", "height": 16},
                                   (16.5, 16), (1 << 17, 16), (16, 16, 3)])
def test_an_unknown_frame_leaves_every_piece_missing(frame):
    result = _check(_correct(), frame=frame)
    assert result["outcome"] == "warning"
    assert result["problems"] == ["frame size unknown"]
    assert set(_statuses(result).values()) == {"missing"}
    assert result["frame"] is None
    assert result["pass"] is False


def test_a_frame_mapping_with_float_sides_is_accepted():
    result = _check(_correct(), frame={"width": 16.0, "height": 16.0})
    assert result["pass"] is True
    assert result["frame"] == {"width": 16, "height": 16}


@pytest.mark.parametrize("regions", [None, {}, "{}", {"roof": [[0, 0], [1, 1]]},
                                     {"roof": "x"}, {1: _ROOF}])
def test_unusable_regions_are_an_error(regions):
    result = layout.check_layout(_correct(), (_W, _H), regions)
    assert result["outcome"] == "error"
    assert result["problems"] == ["expected_regions is not usable"]
    assert result["pieces"] == {} and result["expected_regions"] is None
    assert result["pass"] is False


def test_an_unexpected_class_is_reported_with_a_warning():
    entries = _correct() + [_entry("w1", "wheel", _ROOF_PIXELS),
                            _entry("w2", "wheel", _ROOF_PIXELS),
                            _entry("x1", "axle", _ROOF_PIXELS, confidence=0.2)]
    result = _check(entries)
    assert result["unexpected_classes"] == ["wheel"]
    assert result["problems"] == ["unexpected classes: wheel"]
    assert result["outcome"] == "warning"
    assert set(_statuses(result).values()) == {"ok"}
    assert result["pass"] is False


def test_pass_needs_every_configured_class_whatever_the_map_size():
    one = _regions("roof")
    assert _check([_entry("r1", "roof", _ROOF_PIXELS)], regions=one)["pass"] is True
    assert _check([], regions=one)["pass"] is False

    four = dict(_regions("roof", "front_fender", "rear_fender"))
    four["wheel"] = [(0.8125, 0.0625), (0.9375, 0.0625), (0.9375, 0.375), (0.8125, 0.375)]
    wheel = _entry("w1", "wheel", _rect(13, 15, 1, 6, _H))
    assert _check(_correct() + [wheel], regions=four)["pass"] is True
    result = _check(_correct(), regions=four)
    assert result["pieces"]["wheel"]["status"] == "missing"
    assert result["pass"] is False


def test_non_numeric_confidence_counts_as_zero():
    entries = _correct()
    entries[0] = _entry("r1", "roof", _ROOF_PIXELS, confidence="high")
    assert _check(entries)["pieces"]["roof"]["status"] == "missing"
    assert _check(entries, min_confidence=0.0)["pieces"]["roof"]["status"] == "ok"
    low = _correct()
    low[0] = _entry("r1", "roof", _ROOF_PIXELS, confidence=0.49)
    assert _check(low)["pieces"]["roof"]["status"] == "missing"


def test_a_counted_entry_without_an_id_adds_a_warning():
    entries = _correct()
    entries[0] = _entry(None, "roof", _ROOF_PIXELS)
    result = _check(entries)
    assert result["pieces"]["roof"]["status"] == "ok"
    assert result["problems"] == ["a roof detection has no detection id"]
    assert result["outcome"] == "warning"
    assert result["pass"] is False


# ---------------------------------------------------------------------------
# check_layout: the outline rule (owner decision O19, design DD23)
# ---------------------------------------------------------------------------

def test_an_extra_instance_wholly_outside_the_outline_is_ignored():
    entries = _correct() + [_entry("s1", "rear_fender", _CORNER_PIXELS, confidence=0.81)]
    result = _check(entries)
    assert result["pass"] is True and result["outcome"] == "ok"
    assert result["problems"] == []
    assert result["pieces"]["rear_fender"]["status"] == "ok"
    assert result["flagged"] == []
    assert result["ignored"] == [{"id": "s1", "class_label": "rear_fender",
                                  "confidence": 0.81, "bounding_box": [1.0, 2.0, 3.0, 4.0],
                                  "outline_overlap": 0.0}]


def test_a_piece_set_down_outside_the_outline_leaves_its_class_missing():
    entries = _correct()
    entries[0] = _entry("r1", "roof", _CORNER_PIXELS)
    result = _check(entries)
    assert result["pieces"]["roof"]["status"] == "missing"
    assert [ignored["id"] for ignored in result["ignored"]] == ["r1"]
    assert result["flagged"] == []
    assert result["pass"] is False


def test_the_threshold_boundary_counts_equal_and_ignores_a_pixel_below():
    outside = _rect(15, 16, 0, 16, _H) | _rect(0, 1, 0, 2, _H)  # 18 pixels
    at_threshold = outside | {4 * _H + 1, 4 * _H + 2}           # 2 of 20 inside
    below = (outside | {4 * _H + 1}) | {1 * _H + 0}              # 1 of 20 inside
    assert len(at_threshold) == len(below) == 20

    counted = _check(_correct()[1:] + [_entry("r9", "roof", at_threshold)])
    roof = counted["pieces"]["roof"]
    assert roof["status"] == "misplaced"
    assert roof["detections"][0]["outline_overlap"] == 0.1
    assert counted["ignored"] == []

    ignored = _check(_correct()[1:] + [_entry("r9", "roof", below)])
    assert ignored["pieces"]["roof"]["status"] == "missing"
    assert [entry["outline_overlap"] for entry in ignored["ignored"]] == [0.05]


def test_min_outline_overlap_zero_ignores_nothing():
    entries = _correct() + [_entry("s1", "roof", _CORNER_PIXELS)]
    result = _check(entries, min_outline_overlap=0)
    assert result["ignored"] == []
    assert result["pieces"]["roof"]["status"] == "duplicate"
    assert result["flagged"] == ["r1", "s1"]
    assert result["min_outline_overlap"] == 0.0


def test_an_instance_without_a_mask_is_never_ignored():
    entries = _correct() + [_entry("s1", "roof", None, bounding_box=[0.0, 14.0, 2.0, 16.0])]
    result = _check(entries)
    assert result["ignored"] == []
    assert result["pieces"]["roof"]["status"] == "duplicate"
    assert result["outcome"] == "warning"


def test_an_unexpected_class_outside_the_outline_is_still_reported():
    entries = _correct() + [_entry("w1", "wheel", _CORNER_PIXELS)]
    result = _check(entries)
    assert result["unexpected_classes"] == ["wheel"]
    assert result["ignored"] == []
    assert result["pass"] is False


def test_swapped_fenders_inside_the_outline_are_both_misplaced():
    entries = [_entry("r1", "roof", _ROOF_PIXELS),
               _entry("f1", "front_fender", _REAR_PIXELS),
               _entry("b1", "rear_fender", _FRONT_PIXELS)]
    result = _check(entries)
    assert _statuses(result) == {"roof": "ok", "front_fender": "misplaced",
                                 "rear_fender": "misplaced"}
    assert result["flagged"] == ["f1", "b1"]
    assert result["ignored"] == []


@pytest.mark.parametrize("value", [0.7, -0.1, 0.5000001, "abc", None, True, float("nan")])
def test_an_out_of_range_min_outline_overlap_falls_back_to_the_default(value):
    result = _check(_correct() + [_entry("s1", "roof", _CORNER_PIXELS)],
                    min_outline_overlap=value)
    assert result["min_outline_overlap"] == 0.1
    assert result["outcome"] == "warning"
    assert result["problems"] == [
        "min_outline_overlap {0!r} is not a number between 0 and 0.5; the default "
        "0.1 is used".format(value)]
    assert [ignored["id"] for ignored in result["ignored"]] == ["s1"]
    assert result["pass"] is False


@pytest.mark.parametrize("value", [0, 0.5, "0.25"])
def test_min_outline_overlap_accepts_its_range(value):
    result = _check(_correct(), min_outline_overlap=value)
    assert result["outcome"] == "ok"
    assert result["min_outline_overlap"] == float(value)


@st.composite
def _scenes(draw):
    entries = []
    for index in range(draw(st.integers(min_value=0, max_value=7))):
        label = draw(st.sampled_from(["roof", "front_fender", "rear_fender", "wheel"]))
        confidence = draw(st.one_of(st.floats(min_value=0.0, max_value=1.0),
                                    st.just("n/a")))
        entry = {"id": "d{0}".format(index) if draw(st.integers(0, 4)) else None,
                 "class_label": label, "confidence": confidence,
                 "bounding_box": [0.0, 0.0, 1.0, 1.0]}
        kind = draw(st.sampled_from(["mask", "mask", "mask", "none", "bad", "empty"]))
        if kind == "mask":
            x0 = draw(st.integers(0, _W - 1))
            x1 = draw(st.integers(x0 + 1, _W))
            y0 = draw(st.integers(0, _H - 1))
            y1 = draw(st.integers(y0 + 1, _H))
            entry["mask_rle"] = _rle(_rect(x0, x1, y0, y1, _H), _W, _H)
        elif kind == "bad":
            entry["mask_rle"] = "1 2 3"
        elif kind == "empty":
            entry["mask_rle"] = str(_W * _H)
        entries.append(entry)
    thresholds = {
        "min_iou": draw(st.floats(min_value=0.01, max_value=1.0)),
        "min_confidence": draw(st.floats(min_value=0.0, max_value=1.0)),
        "min_outline_overlap": draw(st.floats(min_value=0.0, max_value=0.5)),
    }
    return entries, thresholds


def _confidence(entry):
    value = entry["confidence"]
    return value if isinstance(value, float) else 0.0


@settings(max_examples=100, deadline=None)
@given(scene=_scenes())
def test_check_layout_invariants(scene):
    entries, thresholds = scene
    result = _check(copy.deepcopy(entries), **thresholds)
    threshold = thresholds["min_outline_overlap"]
    outline = _ROOF_PIXELS | _FRONT_PIXELS | _REAR_PIXELS
    qualifying = [entry for entry in entries
                  if entry["class_label"] in _REGIONS
                  and _confidence(entry) >= thresholds["min_confidence"]]

    # Every qualifying instance is either counted or ignored, never both.
    counted = [detection for piece in result["pieces"].values()
               for detection in piece["detections"]]
    assert len(counted) + len(result["ignored"]) == len(qualifying)

    # Every ok class has exactly one counted instance with a measured
    # overlap at or above the threshold and an iou at or above min_iou.
    for piece in result["pieces"].values():
        if piece["status"] == "ok":
            assert len(piece["detections"]) == 1
            detection = piece["detections"][0]
            assert detection["outline_overlap"] is not None
            assert detection["outline_overlap"] >= threshold
            assert detection["iou"] >= thresholds["min_iou"]

    # Every ignored instance has a measured overlap below the threshold,
    # the one an independent set computation gives.
    by_id = {entry["id"]: entry for entry in entries if entry["id"] is not None}
    for ignored in result["ignored"]:
        assert ignored["outline_overlap"] < threshold
        if ignored["id"] is not None:
            intervals, _ = layout.rle_intervals(by_id[ignored["id"]]["mask_rle"], _W, _H)
            mask = _pixels_of(intervals)
            assert ignored["outline_overlap"] == round(len(mask & outline) / len(mask), 4)
            assert ignored["id"] not in result["flagged"]

    # flagged is a subset of the qualifying ids.
    assert set(result["flagged"]) <= {entry["id"] for entry in qualifying}
    assert result["pass"] == (result["outcome"] == "ok" and all(
        piece["status"] == "ok" for piece in result["pieces"].values()))

    section = layout.run_metadata(result)
    assert tuple(section) == layout.LAYOUT_METADATA_KEYS
    assert "mask_rle" not in json.dumps(section)


# ---------------------------------------------------------------------------
# attach_detection_ids against the device normalizer (DD5)
# ---------------------------------------------------------------------------

def _raw_record():
    return [
        {"class_index": "0", "class_label": "roof", "confidence": 0.91,
         "bounding_box": [40.0, 10.0, 90.0, 30.0], "mask_rle": "256"},
        {"class_index": "1", "class_label": "front_fender", "confidence": 0.8,
         "bounding_box": [120.5, 60, 170, 95]},
        # An identical twin: each of the two takes its own id.
        {"class_index": "1", "class_label": "front_fender", "confidence": 0.8,
         "bounding_box": [120.5, 60, 170, 95]},
        {"class_index": "2", "class_label": "rear_fender", "confidence": 1,
         "bounding_box": [5, 60, 50, 95]},
        # No class_label: the normalizer reads it as "", the join as "None".
        {"class_index": "3", "confidence": 0.7, "bounding_box": [1, 1, 2, 2]},
    ]


def test_attach_detection_ids_recovers_the_ids_the_device_draws():
    raw = _raw_record()
    built = detections.build_detection_list(raw, "right_to_left", rng=random.Random(11))
    assert [entry["label"] for entry in built] != [entry.get("class_label", "") for entry in raw]
    joined = layout.attach_detection_ids(raw, built)

    assert len(joined) == len(raw)
    ids = [entry["id"] for entry in joined]
    assert None not in ids[:4] and ids[4] is None
    assert len(set(ids[:4])) == 4
    by_id = {entry["id"]: entry for entry in built}
    for raw_entry, joined_entry in zip(raw[:4], joined[:4]):
        match = by_id[joined_entry["id"]]
        assert match["label"] == raw_entry["class_label"]
        assert match["confidence"] == float(raw_entry["confidence"])
        assert (match["x_min"], match["y_min"], match["x_max"], match["y_max"]) == \
            tuple(float(value) for value in raw_entry["bounding_box"])
    # Copies: the raw entries are untouched and keep their masks.
    assert "id" not in raw[0] and joined[0]["mask_rle"] == "256"
    # The label-less entry stays unjoined although the built list has it.
    assert "" in [entry["label"] for entry in built]


def test_a_mutated_box_stays_unjoined():
    raw = _raw_record()
    built = detections.build_detection_list(raw, "left_to_right", rng=random.Random(5))
    raw[0] = dict(raw[0], bounding_box=[40.0, 10.0, 90.0, 30.5])
    joined = layout.attach_detection_ids(raw, built)
    assert joined[0]["id"] is None
    assert all(entry["id"] is not None for entry in joined[1:4])


def test_attach_detection_ids_without_built_entries():
    raw = _raw_record()
    assert [entry["id"] for entry in layout.attach_detection_ids(raw, None)] == [None] * 5
    assert layout.attach_detection_ids(None, []) == []
    assert layout.attach_detection_ids([raw[0], "x", None], []) == [dict(raw[0], id=None)]


def test_a_label_less_entry_is_neither_flagged_nor_counted():
    raw = [_entry(None, "roof", _ROOF_PIXELS, bounding_box=[64.0, 16.0, 192.0, 96.0]),
           _entry(None, "front_fender", _FRONT_PIXELS, bounding_box=[1.0, 1.0, 2.0, 2.0]),
           _entry(None, "rear_fender", _REAR_PIXELS, bounding_box=[3.0, 3.0, 4.0, 4.0]),
           _entry(None, "roof", _CORNER_PIXELS, bounding_box=[5.0, 5.0, 6.0, 6.0])]
    for entry in raw:
        del entry["id"]
    del raw[3]["class_label"]
    built = detections.build_detection_list(raw, "left_to_right", rng=random.Random(3))
    joined = layout.attach_detection_ids(raw, built)
    assert joined[3]["id"] is None
    result = _check(joined)
    assert result["pass"] is True and result["outcome"] == "ok"
    assert result["flagged"] == [] and result["ignored"] == []
    assert result["unexpected_classes"] == []
    assert sorted(result["flagged"] + [d["id"] for p in result["pieces"].values()
                                       for d in p["detections"]]) == \
        sorted(entry["id"] for entry in joined[:3])


# ---------------------------------------------------------------------------
# run_metadata and the module contract
# ---------------------------------------------------------------------------

def test_run_metadata_emits_exactly_the_layout_keys_and_no_mask():
    result = _check(_correct())
    section = layout.run_metadata(result)
    assert tuple(section) == layout.LAYOUT_METADATA_KEYS
    assert "outcome" not in section and "problems" not in section
    assert "mask_rle" not in json.dumps(section)
    assert layout.run_metadata(None) == {}
    assert layout.run_metadata({"pass": True, "outcome": "ok", "mask_rle": "1"}) == \
        {"pass": True}


def test_layout_imports_only_the_standard_library_and_scene():
    with open(layout.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    absolute = set()
    relative = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append((node.level, node.module, [alias.name for alias in node.names]))
            else:
                absolute.add(node.module.split(".")[0])
    assert absolute <= {"json", "math", "bisect", "typing"}
    assert relative == [(1, None, ["scene"])]


def test_the_public_surface_is_the_interfaces_block():
    assert layout.__all__ == [
        "STATUS_OK", "STATUS_MISPLACED", "STATUS_MISSING", "STATUS_DUPLICATE",
        "DEFAULT_MIN_IOU", "DEFAULT_MIN_CONFIDENCE", "DEFAULT_MIN_OUTLINE_OVERLAP",
        "MAX_MIN_OUTLINE_OVERLAP", "MIN_VERTICES", "MAX_VERTICES", "MAX_REGIONS",
        "MIN_REGION_AREA", "LAYOUT_METADATA_KEYS", "parse_expected_regions",
        "rle_intervals", "interval_iou", "interval_union",
        "interval_intersection_length", "outline_intervals", "outline_overlap",
        "position_score", "attach_detection_ids", "check_layout", "run_metadata",
    ]
    assert (layout.STATUS_OK, layout.STATUS_MISPLACED, layout.STATUS_MISSING,
            layout.STATUS_DUPLICATE) == ("ok", "misplaced", "missing", "duplicate")
    assert (layout.DEFAULT_MIN_IOU, layout.DEFAULT_MIN_CONFIDENCE) == (0.7, 0.5)
    assert (layout.DEFAULT_MIN_OUTLINE_OVERLAP, layout.MAX_MIN_OUTLINE_OVERLAP) == (0.1, 0.5)
    assert (layout.MIN_VERTICES, layout.MAX_VERTICES, layout.MAX_REGIONS,
            layout.MIN_REGION_AREA) == (3, 64, 10, 0.005)
