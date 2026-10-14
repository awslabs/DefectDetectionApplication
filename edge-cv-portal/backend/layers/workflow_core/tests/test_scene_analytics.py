"""Unit tests for `workflow_core.analytics.scene` (Scene_Analytics_Nodes).

Feature: rtsp-rtmp-stream-cameras, task 1.4. Deterministic companions to
the hypothesis properties of tasks 1.5 to 1.8 (Properties 23 to 26): they
pin the Label_Key rules, the Zone parser's problem set, the two
boundary-inclusive geometry predicates, the counter's zero-filling and
outcomes, the association's one-to-one greedy matching, and the event
gate automaton under every `emit` mode.

Requirements: 13.2, 13.3, 13.5, 13.6, 13.7, 14.2, 14.3, 14.4, 15.2, 15.3,
15.4.
"""

import re

import pytest

from workflow_core.analytics.scene import (
    ASSOCIATION_METADATA_KEYS,
    COUNTER_METADATA_KEYS,
    DEFAULT_MIN_OVERLAP,
    EMIT_ON_ACTIVATE,
    EMIT_ON_CHANGE,
    EMIT_WHILE_ACTIVE,
    EMIT_MODES,
    EventGateState,
    GATE_STATE_ACTIVE,
    GATE_STATE_INACTIVE,
    LABEL_KEY_PATTERN,
    MAX_REQUIRED_CLASSES,
    MAX_ZONE_POINTS,
    MIN_ZONE_POINTS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_WARNING,
    TRANSITION_ACTIVATED,
    TRANSITION_CLEARED,
    TRANSITION_NONE,
    ZONE_RULE_CENTER,
    ZONE_RULE_OVERLAP,
    associate,
    box_intersects_polygon,
    count_detections,
    event_gate_metadata,
    label_key,
    parse_label_list,
    parse_zone,
    point_in_polygon,
    run_metadata,
    step_event_gate,
)

# The Condition_Language / output-template dotted-path segment shape
# (output_bindings._TOKEN and _DOTTED_PLACEHOLDER on the device).
CONDITION_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_]+$")

FULL_FRAME_ZONE = "[[0, 0], [1, 0], [1, 1], [0, 1]]"
LEFT_HALF_ZONE = "[[0, 0], [0.5, 0], [0.5, 1], [0, 1]]"
FRAME = (1000, 1000)


def detection(label, confidence=0.9, box=(10, 10, 20, 20), detection_id=None):
    """A Detection_List entry, in source-frame pixels."""
    return {
        "id": detection_id if detection_id is not None else "id-" + label,
        "label": label,
        "confidence": confidence,
        "x_min": float(box[0]),
        "y_min": float(box[1]),
        "x_max": float(box[2]),
        "y_max": float(box[3]),
    }


# ---------------------------------------------------------------------------
# label_key (Requirements 13.3, 13.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("person", "person"),
        ("Person", "person"),
        ("Hard Hat", "hard_hat"),
        ("  hard   hat  ", "hard_hat"),
        ("hard-hat", "hard_hat"),
        ("HARD//HAT", "hard_hat"),
        ("safety_vest", "safety_vest"),
        ("class 3", "class_3"),
        ("3M", "3m"),
        ("---", ""),
        ("", ""),
        ("ü", ""),
        ("Pérson", "p_rson"),
    ],
)
def test_label_key_normalizes_as_the_glossary_specifies(label, expected):
    assert label_key(label) == expected


@pytest.mark.parametrize(
    "label",
    ["person", "Hard Hat", "---", "", "3M", "a..b", "ü", "MiXeD_case-1"],
)
def test_label_key_is_idempotent_and_addressable(label):
    key = label_key(label)
    assert label_key(key) == key
    if key:
        assert re.match(LABEL_KEY_PATTERN, key)
        # Usable as a dotted-path segment: counter.<node>.counts.<key>.
        assert CONDITION_PATH_SEGMENT.match(key)


def test_label_key_is_total_for_non_strings():
    assert label_key(None) == ""
    assert label_key(7) == "7"


# ---------------------------------------------------------------------------
# parse_label_list (Requirements 13.7, 14.1)
# ---------------------------------------------------------------------------


def test_parse_label_list_keys_in_order_without_problems():
    keys, problems = parse_label_list("Person, Hard Hat , safety vest", 32)
    assert keys == ["person", "hard_hat", "safety_vest"]
    assert problems == []


def test_parse_label_list_accepts_an_already_split_sequence():
    assert parse_label_list(["Person", "Hard Hat"], 10) == (
        ["person", "hard_hat"],
        [],
    )


def test_parse_label_list_treats_a_blank_value_as_no_labels():
    for value in (None, "", "   "):
        assert parse_label_list(value, 10) == ([], [])


def test_parse_label_list_reports_an_empty_entry():
    keys, problems = parse_label_list("person,,hardhat", 10)
    assert keys == ["person", "hardhat"]
    assert len(problems) == 1
    assert "entry 2 is empty" in problems[0]


def test_parse_label_list_reports_a_label_without_letters_or_digits():
    keys, problems = parse_label_list("person, ---", 10)
    assert keys == ["person"]
    assert len(problems) == 1
    assert "no letters or digits" in problems[0]


def test_parse_label_list_deduplicates_by_label_key_without_a_problem():
    keys, problems = parse_label_list("Person, person, PERSON", 10)
    assert keys == ["person"]
    assert problems == []


def test_parse_label_list_reports_more_than_max_items():
    text = ",".join("class{0}".format(index) for index in range(MAX_REQUIRED_CLASSES + 1))
    keys, problems = parse_label_list(text, MAX_REQUIRED_CLASSES)
    assert len(keys) == MAX_REQUIRED_CLASSES + 1
    assert len(problems) == 1
    assert "at most {0}".format(MAX_REQUIRED_CLASSES) in problems[0]


# ---------------------------------------------------------------------------
# parse_zone (Requirements 13.6, 13.7)
# ---------------------------------------------------------------------------


def test_parse_zone_canonical_form():
    points, problems = parse_zone("[[0, 0], [1, 0], [0.5, 1]]")
    assert points == [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)]
    assert problems == []


def test_parse_zone_accepts_point_objects_and_a_points_wrapper():
    expected = [(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)]
    points, problems = parse_zone('[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0.5, "y": 1}]')
    assert (points, problems) == (expected, [])
    points, problems = parse_zone('{"points": [[0, 0], [1, 0], [0.5, 1]]}')
    assert (points, problems) == (expected, [])


def test_parse_zone_accepts_an_already_parsed_polygon():
    points, problems = parse_zone([[0, 0], [1, 0], [0.5, 1]])
    assert (points, problems) == ([(0.0, 0.0), (1.0, 0.0), (0.5, 1.0)], [])


def test_parse_zone_treats_a_blank_value_as_no_zone():
    for value in (None, "", "   "):
        assert parse_zone(value) == (None, [])


def test_parse_zone_reports_invalid_json():
    points, problems = parse_zone("[[0, 0], [1, 0]")
    assert points is None
    assert problems == ["Zone is not valid JSON."]


@pytest.mark.parametrize("count", [0, 1, MIN_ZONE_POINTS - 1, MAX_ZONE_POINTS + 1])
def test_parse_zone_reports_too_few_or_too_many_points(count):
    polygon = [[index / (count + 1.0), 0.5] for index in range(count)]
    points, problems = parse_zone(polygon)
    assert points is None
    assert any("between 3 and 32 points" in problem for problem in problems)


@pytest.mark.parametrize(
    "polygon,axis",
    [
        ([[0, 0], [1.5, 0], [0.5, 1]], "x"),
        ([[0, 0], [1, 0], [0.5, -0.1]], "y"),
    ],
)
def test_parse_zone_reports_a_coordinate_outside_zero_to_one(polygon, axis):
    points, problems = parse_zone(polygon)
    assert points is None
    assert any(
        "{0} coordinate must be between 0 and 1".format(axis) in problem
        for problem in problems
    )


def test_parse_zone_reports_a_malformed_point():
    points, problems = parse_zone('[[0, 0], "nope", [0.5, 1]]')
    assert points is None
    assert any("point 2 must be a pair of numbers" in problem for problem in problems)


def test_parse_zone_reports_a_non_polygon_value():
    points, problems = parse_zone("42")
    assert points is None
    assert problems and "list of [x, y] points" in problems[0]
    points, problems = parse_zone('{"polygon": []}')
    assert points is None
    assert problems and "'points' list" in problems[0]


def test_parse_zone_reports_an_integer_beyond_the_float_range():
    """json.loads parses a 400-digit integer exactly; math.isfinite of it
    raises OverflowError, which must be a problem, never an exception."""
    huge = "1" * 400
    points, problems = parse_zone("[[{0}, 0], [0, 0], [0, 1]]".format(huge))
    assert points is None
    assert problems == ["Zone point 1 must be a pair of numbers [x, y]."]


# ---------------------------------------------------------------------------
# Geometry (Requirement 13.2)
# ---------------------------------------------------------------------------

UNIT_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
CONCAVE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (5.0, 2.0), (0.0, 10.0)]


@pytest.mark.parametrize(
    "point,expected",
    [
        ((5.0, 5.0), True),
        ((0.0, 0.0), True),  # vertex: boundary inclusive
        ((0.0, 5.0), True),  # on an edge
        ((10.0, 10.0), True),
        ((-0.001, 5.0), False),
        ((10.001, 5.0), False),
        ((5.0, -1.0), False),
        ((15.0, 15.0), False),
    ],
)
def test_point_in_polygon_even_odd_boundary_inclusive(point, expected):
    assert point_in_polygon(point[0], point[1], UNIT_SQUARE) is expected


def test_point_in_polygon_handles_a_concave_polygon():
    # (5, 5) is in the notch cut out of the top edge, so it is outside.
    assert point_in_polygon(5.0, 5.0, CONCAVE) is False
    assert point_in_polygon(1.0, 5.0, CONCAVE) is True


def test_point_in_polygon_is_false_for_a_degenerate_polygon():
    assert point_in_polygon(0.0, 0.0, [(0.0, 0.0), (1.0, 1.0)]) is False
    assert point_in_polygon(0.0, 0.0, []) is False
    assert point_in_polygon(0.0, 0.0, None) is False


@pytest.mark.parametrize(
    "box,expected",
    [
        ((2.0, 2.0, 4.0, 4.0), True),  # inside
        ((-5.0, -5.0, 15.0, 15.0), True),  # contains the polygon
        ((-5.0, -5.0, 0.0, 0.0), True),  # touches a vertex
        ((-5.0, 4.0, 5.0, 6.0), True),  # crosses an edge
        ((10.0, 4.0, 20.0, 6.0), True),  # touches an edge
        ((10.001, 4.0, 20.0, 6.0), False),
        ((-20.0, -20.0, -10.0, -10.0), False),
    ],
)
def test_box_intersects_polygon(box, expected):
    assert box_intersects_polygon(box, UNIT_SQUARE) is expected


def test_box_intersects_polygon_accepts_a_detection_entry_and_inverted_boxes():
    entry = detection("person", box=(2, 2, 4, 4))
    assert box_intersects_polygon(entry, UNIT_SQUARE) is True
    assert box_intersects_polygon((4.0, 4.0, 2.0, 2.0), UNIT_SQUARE) is True
    assert box_intersects_polygon("nope", UNIT_SQUARE) is False


# ---------------------------------------------------------------------------
# count_detections (Requirements 13.2, 13.3, 13.5, 13.6)
# ---------------------------------------------------------------------------


def test_count_detections_groups_by_label_key_and_zero_fills_classes():
    detections = [
        detection("Person"),
        detection("person"),
        detection("Hard Hat"),
        detection("forklift"),
    ]
    result = count_detections(
        detections, classes="Person, Hard Hat, Safety Vest", frame_size=FRAME
    )
    assert result["counts"] == {
        "person": 2,
        "hard_hat": 1,
        "safety_vest": 0,
        "forklift": 1,
    }
    assert result["total"] == 4
    assert result["labels"] == {
        "person": "Person",
        "hard_hat": "Hard Hat",
        "safety_vest": "Safety Vest",
        "forklift": "forklift",
    }
    assert result["outcome"] == OUTCOME_OK
    assert result["problems"] == []


def test_count_detections_labels_prefer_the_detectors_spelling():
    result = count_detections(
        [detection("Person"), detection("PERSON")], classes="person, forklift"
    )
    assert result["labels"] == {"person": "Person", "forklift": "forklift"}
    assert result["counts"] == {"person": 2, "forklift": 0}


def test_count_detections_total_equals_the_sum_of_counts():
    detections = [detection("person"), detection("---"), detection("box")]
    result = count_detections(detections)
    assert result["total"] == sum(result["counts"].values()) == 3
    # A label with no letters or digits keeps the empty key, so the total
    # and the counts cannot disagree.
    assert result["counts"][""] == 1


def test_count_detections_filters_by_confidence():
    detections = [
        detection("person", confidence=0.9),
        detection("person", confidence=0.4),
    ]
    result = count_detections(detections, min_confidence=0.5)
    assert result["counts"] == {"person": 1}
    assert result["total"] == 1


def test_count_detections_center_zone_rule():
    inside = detection("person", box=(400, 400, 440, 440))  # center 420,420
    outside = detection("person", box=(480, 400, 600, 440))  # center 540,420
    result = count_detections(
        [inside, outside],
        zone=LEFT_HALF_ZONE,
        zone_rule=ZONE_RULE_CENTER,
        frame_size=FRAME,
    )
    assert result["counts"] == {"person": 1}
    assert result["outcome"] == OUTCOME_OK


def test_count_detections_overlap_zone_rule_counts_a_straddling_box():
    straddling = detection("person", box=(480, 400, 600, 440))  # center outside
    result = count_detections(
        [straddling],
        zone=LEFT_HALF_ZONE,
        zone_rule=ZONE_RULE_OVERLAP,
        frame_size=FRAME,
    )
    assert result["counts"] == {"person": 1}
    center_result = count_detections(
        [straddling],
        zone=LEFT_HALF_ZONE,
        zone_rule=ZONE_RULE_CENTER,
        frame_size=FRAME,
    )
    assert center_result["total"] == 0


def test_count_detections_scales_the_zone_by_the_frame_size():
    entry = detection("person", box=(1200, 500, 1240, 540))
    wide = count_detections(
        [entry], zone=FULL_FRAME_ZONE, frame_size=(1920, 1080)
    )
    narrow = count_detections(
        [entry], zone=FULL_FRAME_ZONE, frame_size=(640, 480)
    )
    assert wide["total"] == 1
    assert narrow["total"] == 0


def test_count_detections_without_a_detection_list_warns_and_zero_fills():
    result = count_detections(None, classes="person", frame_size=FRAME)
    assert result["counts"] == {"person": 0}
    assert result["total"] == 0
    assert result["outcome"] == OUTCOME_WARNING
    assert result["problems"] and "no detection list" in result["problems"][0]


def test_count_detections_with_an_empty_detection_list_is_ok():
    result = count_detections([], classes="person")
    assert result["counts"] == {"person": 0}
    assert result["outcome"] == OUTCOME_OK
    assert result["problems"] == []


def test_count_detections_with_a_zone_and_unknown_frame_size_errors():
    detections = [detection("person", box=(400, 400, 440, 440))]
    for frame_size in (None, (0, 1080), {"width": 1920}, "big"):
        result = count_detections(
            detections, classes="person", zone=LEFT_HALF_ZONE, frame_size=frame_size
        )
        assert result["outcome"] == OUTCOME_ERROR
        assert result["counts"] == {"person": 0}
        assert result["total"] == 0
        assert any("frame dimensions are unknown" in p for p in result["problems"])


def test_count_detections_without_a_zone_ignores_an_unknown_frame_size():
    result = count_detections([detection("person")], frame_size=None)
    assert result["outcome"] == OUTCOME_OK
    assert result["counts"] == {"person": 1}


def test_count_detections_with_a_malformed_zone_errors():
    result = count_detections(
        [detection("person")], zone="[[0,0],[1,0]]", frame_size=FRAME
    )
    assert result["outcome"] == OUTCOME_ERROR
    assert result["total"] == 0
    assert result["problems"]


def test_count_detections_is_total_for_malformed_entries():
    detections = [
        detection("person"),
        {"label": "person"},  # no box, no confidence
        {"label": "person", "confidence": float("nan"), "x_min": 0},
        "not a detection",
        {"label": "box", "confidence": 0.9, "x_min": float("inf"),
         "y_min": 0, "x_max": 1, "y_max": 1},
    ]
    # Without a zone, an entry needs no box; a missing or non-finite
    # confidence reads as 0 and so passes a zero threshold.
    plain = count_detections(detections)
    assert plain["counts"] == {"person": 3, "box": 1}
    assert plain["total"] == 4
    assert plain["outcome"] == OUTCOME_OK
    # With a zone, an entry without a usable box cannot pass it.
    zoned = count_detections(
        detections, zone=FULL_FRAME_ZONE, zone_rule=ZONE_RULE_CENTER, frame_size=FRAME
    )
    assert zoned["counts"] == {"person": 1}
    assert zoned["outcome"] == OUTCOME_OK


def test_count_detections_run_metadata_projection():
    result = count_detections([detection("person")])
    assert tuple(run_metadata(result)) == COUNTER_METADATA_KEYS
    assert "outcome" not in run_metadata(result)
    assert "problems" not in run_metadata(result)


def test_count_detections_is_deterministic():
    detections = [detection("b"), detection("a"), detection("b")]
    first = count_detections(detections, classes="c, b", frame_size=FRAME)
    second = count_detections(detections, classes="c, b", frame_size=FRAME)
    assert first == second
    assert list(first["counts"]) == ["c", "b", "a"]


# ---------------------------------------------------------------------------
# associate (Requirements 14.2, 14.3, 14.4)
# ---------------------------------------------------------------------------


def _person(box, detection_id, confidence=0.9):
    return detection("person", confidence=confidence, box=box, detection_id=detection_id)


def test_associate_compliant_and_violating_subjects():
    detections = [
        _person((0, 0, 100, 200), "p1"),
        _person((300, 0, 400, 200), "p2"),
        # A hard hat fully inside p1, and one inside p2. The detector's
        # spelling differs from the parameter's; both key to hard_hat.
        detection("Hard Hat", box=(20, 0, 60, 30), detection_id="h1"),
        detection("Hard Hat", box=(320, 0, 360, 30), detection_id="h2"),
        # Only p1 has a vest.
        detection("vest", box=(20, 60, 80, 140), detection_id="v1"),
    ]
    result = associate(
        detections,
        subject_class="Person",
        required_classes="Hard Hat, vest",
        min_overlap=0.5,
    )
    assert result["subjects"] == 2
    assert result["compliant"] == 1
    assert result["violations"] == 1
    assert result["missing"] == {"hard_hat": 0, "vest": 1}
    assert result["violating_ids"] == ["p2"]
    assert result["outcome"] == OUTCOME_OK
    assert result["compliant"] + result["violations"] == result["subjects"]


def test_associate_matches_one_to_one_within_a_class():
    # Two people overlapping the same single hard hat: only one can use it.
    detections = [
        _person((0, 0, 100, 200), "p1"),
        _person((10, 0, 110, 200), "p2"),
        detection("hardhat", box=(20, 0, 60, 30), detection_id="h1"),
    ]
    result = associate(
        detections, subject_class="person", required_classes="hardhat", min_overlap=0.5
    )
    assert result["subjects"] == 2
    assert result["compliant"] == 1
    assert result["missing"] == {"hardhat": 1}
    assert result["violating_ids"] == ["p2"]


def test_associate_prefers_the_larger_overlap():
    # One hat, two subjects: it lies fully inside p_full and only 75%
    # inside p_partial, which comes first in the Detection_List. Overlap
    # therefore beats subject order, and p_partial is the violation.
    detections = [
        _person((70, 0, 170, 100), "p_partial"),
        _person((0, 0, 100, 100), "p_full"),
        detection("hardhat", box=(60, 10, 100, 30), detection_id="h1"),
    ]
    result = associate(
        detections, subject_class="person", required_classes="hardhat", min_overlap=0.5
    )
    assert result["subjects"] == 2
    assert result["compliant"] == 1
    assert result["violating_ids"] == ["p_partial"]


def test_associate_breaks_overlap_ties_by_subject_order():
    # The hat lies fully inside both subjects, so the first one wins.
    detections = [
        _person((0, 0, 100, 100), "p1"),
        _person((50, 0, 150, 100), "p2"),
        detection("hardhat", box=(60, 10, 80, 30), detection_id="h1"),
    ]
    result = associate(
        detections, subject_class="person", required_classes="hardhat", min_overlap=0.5
    )
    assert result["violating_ids"] == ["p2"]


def test_associate_honours_min_overlap_on_the_detections_own_area():
    # The hat's box is half inside the person's box.
    detections = [
        _person((0, 0, 100, 100), "p1"),
        detection("hardhat", box=(90, 10, 110, 30), detection_id="h1"),
    ]
    lenient = associate(
        detections, subject_class="person", required_classes="hardhat", min_overlap=0.5
    )
    strict = associate(
        detections, subject_class="person", required_classes="hardhat", min_overlap=0.75
    )
    assert lenient["compliant"] == 1
    assert strict["compliant"] == 0
    assert strict["violating_ids"] == ["p1"]


def test_associate_filters_subjects_by_confidence_and_zone_only():
    detections = [
        _person((400, 400, 440, 480), "p_low", confidence=0.2),
        _person((400, 500, 440, 580), "p_in"),
        _person((900, 500, 940, 580), "p_out"),
        # A low-confidence hat inside p_in still makes it compliant:
        # min_confidence gates subjects, the overlap rule gates the rest.
        detection("hardhat", box=(410, 500, 430, 520), confidence=0.01),
    ]
    result = associate(
        detections,
        subject_class="person",
        required_classes="hardhat",
        min_confidence=0.5,
        zone=LEFT_HALF_ZONE,
        frame_size=FRAME,
    )
    assert result["subjects"] == 1
    assert result["compliant"] == 1
    assert result["violating_ids"] == []


def test_associate_without_a_detection_list_warns():
    result = associate(None, subject_class="person", required_classes="hardhat")
    assert result["subjects"] == 0
    assert result["compliant"] == 0
    assert result["violations"] == 0
    assert result["missing"] == {"hardhat": 0}
    assert result["outcome"] == OUTCOME_WARNING


def test_associate_with_a_zone_and_unknown_frame_size_errors():
    result = associate(
        [_person((0, 0, 100, 100), "p1")],
        subject_class="person",
        required_classes="hardhat",
        zone=LEFT_HALF_ZONE,
        frame_size=None,
    )
    assert result["outcome"] == OUTCOME_ERROR
    assert result["subjects"] == 0
    assert result["violating_ids"] == []


@pytest.mark.parametrize(
    "subject_class,required_classes",
    [("", "hardhat"), (None, "hardhat"), ("person", ""), ("person", "---")],
)
def test_associate_without_configuration_errors(subject_class, required_classes):
    result = associate(
        [_person((0, 0, 100, 100), "p1")],
        subject_class=subject_class,
        required_classes=required_classes,
    )
    assert result["outcome"] == OUTCOME_ERROR
    assert result["compliant"] == 0


def test_associate_run_metadata_projection_and_determinism():
    detections = [
        _person((0, 0, 100, 200), "p1"),
        detection("hardhat", box=(20, 0, 60, 30), detection_id="h1"),
    ]
    first = associate(
        detections, subject_class="person", required_classes="hardhat, vest"
    )
    second = associate(
        detections, subject_class="person", required_classes="hardhat, vest"
    )
    assert first == second
    assert tuple(run_metadata(first)) == ASSOCIATION_METADATA_KEYS


def test_associate_default_min_overlap_is_the_catalog_default():
    detections = [
        _person((0, 0, 100, 100), "p1"),
        detection("hardhat", box=(60, 10, 140, 30), detection_id="h1"),
    ]
    # 50% of the hat's area is inside the person, exactly the default.
    assert DEFAULT_MIN_OVERLAP == 0.5
    result = associate(detections, subject_class="person", required_classes="hardhat")
    assert result["compliant"] == 1


# ---------------------------------------------------------------------------
# step_event_gate (Requirements 15.2, 15.3, 15.4)
# ---------------------------------------------------------------------------


def _run(outcomes, **kwargs):
    """Step a fresh gate through ``outcomes``; returns the per-run records.

    Run *n* carries ``now_ms = n``, so a timestamp identifies its run.
    """
    state = EventGateState()
    records = []
    for index, outcome in enumerate(outcomes):
        state, passed, transition = step_event_gate(
            state, outcome, now_ms=index, **kwargs
        )
        records.append((state, passed, transition))
    return records


def test_event_gate_activates_after_exactly_activate_after_trues():
    records = _run([True, True, True, True], activate_after=3, clear_after=3)
    transitions = [record[2] for record in records]
    assert transitions == [
        TRANSITION_NONE,
        TRANSITION_NONE,
        TRANSITION_ACTIVATED,
        TRANSITION_NONE,
    ]
    assert [record[0].active for record in records] == [False, False, True, True]
    assert [record[1] for record in records] == [False, False, True, False]
    assert records[2][0].active_since_ms == 2
    assert records[3][0].consecutive_true == 4


def test_event_gate_clears_after_exactly_clear_after_falses():
    records = _run(
        [True, True, False, False, False], activate_after=2, clear_after=3
    )
    transitions = [record[2] for record in records]
    assert transitions == [
        TRANSITION_NONE,
        TRANSITION_ACTIVATED,
        TRANSITION_NONE,
        TRANSITION_NONE,
        TRANSITION_CLEARED,
    ]
    assert records[-1][0].active is False
    assert records[-1][0].active_since_ms is None
    assert records[-1][0].consecutive_false == 3


def test_event_gate_counts_an_unevaluable_condition_as_false():
    records = _run([True, True, None, None], activate_after=2, clear_after=2)
    assert records[1][2] == TRANSITION_ACTIVATED
    assert records[3][2] == TRANSITION_CLEARED
    assert records[2][0].consecutive_false == 1


def test_event_gate_true_run_resets_the_false_counter():
    records = _run([False, False, True, False], activate_after=5, clear_after=5)
    assert records[1][0].consecutive_false == 2
    assert records[2][0].consecutive_false == 0
    assert records[2][0].consecutive_true == 1
    assert records[3][0].consecutive_true == 0


def test_event_gate_emit_on_activate_passes_only_the_activating_run():
    records = _run(
        [True, True, True, False, False, True, True],
        activate_after=2,
        clear_after=2,
        emit=EMIT_ON_ACTIVATE,
    )
    assert [record[1] for record in records] == [
        False,
        True,
        False,
        False,
        False,
        False,
        True,
    ]


def test_event_gate_emit_on_change_passes_activations_and_clears():
    records = _run(
        [True, True, True, False, False, False],
        activate_after=2,
        clear_after=2,
        emit=EMIT_ON_CHANGE,
    )
    assert [record[1] for record in records] == [
        False,
        True,
        False,
        False,
        True,
        False,
    ]


def test_event_gate_emit_while_active_passes_every_active_run():
    records = _run(
        [True, True, True, False, False],
        activate_after=2,
        clear_after=2,
        emit=EMIT_WHILE_ACTIVE,
    )
    # Run 4 is the first false: the gate is still active, so it passes;
    # run 5 clears it, so it does not.
    assert [record[1] for record in records] == [False, True, True, True, False]


def test_event_gate_while_active_repeat_interval_rate_limits():
    state = EventGateState()
    passes = []
    # Activate on the first run, then step every 100 ms with a 250 ms window.
    for index in range(6):
        state, passed, _transition = step_event_gate(
            state,
            True,
            activate_after=1,
            clear_after=1,
            emit=EMIT_WHILE_ACTIVE,
            repeat_interval_ms=250,
            now_ms=index * 100,
        )
        passes.append(passed)
    # Emits at 0, then again at 300 (the first run at least 250 ms later),
    # then not again until 550, which this sequence never reaches.
    assert passes == [True, False, False, True, False, False]


def test_event_gate_reactivation_emits_again_under_a_repeat_interval():
    state = EventGateState()
    state, first_passed, _ = step_event_gate(
        state,
        True,
        activate_after=1,
        clear_after=1,
        emit=EMIT_WHILE_ACTIVE,
        repeat_interval_ms=10000,
        now_ms=0,
    )
    state, cleared_passed, transition = step_event_gate(
        state,
        False,
        activate_after=1,
        clear_after=1,
        emit=EMIT_WHILE_ACTIVE,
        repeat_interval_ms=10000,
        now_ms=100,
    )
    state, reactivated_passed, _ = step_event_gate(
        state,
        True,
        activate_after=1,
        clear_after=1,
        emit=EMIT_WHILE_ACTIVE,
        repeat_interval_ms=10000,
        now_ms=200,
    )
    assert first_passed is True
    assert (cleared_passed, transition) == (False, TRANSITION_CLEARED)
    assert reactivated_passed is True


def test_event_gate_state_is_immutable_and_returns_a_new_value():
    state = EventGateState()
    next_state, _passed, _transition = step_event_gate(state, True, activate_after=1)
    assert state == EventGateState()
    assert next_state is not state
    assert next_state.active is True
    with pytest.raises(Exception):
        next_state.active = False


@pytest.mark.parametrize("emit", list(EMIT_MODES) + ["nonsense", None])
def test_event_gate_is_total_for_malformed_parameters(emit):
    state, passed, transition = step_event_gate(
        None,
        True,
        activate_after=0,
        clear_after="x",
        emit=emit,
        repeat_interval_ms=-5,
        now_ms=7,
    )
    # activate_after below 1 is read as 1, so one true activates.
    assert state.active is True
    assert transition == TRANSITION_ACTIVATED
    assert isinstance(passed, bool)


def test_event_gate_metadata_shape():
    state, _passed, transition = step_event_gate(
        EventGateState(), True, activate_after=1, now_ms=1234
    )
    assert event_gate_metadata(state, transition) == {
        "state": GATE_STATE_ACTIVE,
        "transition": TRANSITION_ACTIVATED,
        "active_since": 1234,
        "consecutive_true": 1,
        "consecutive_false": 0,
    }
    assert event_gate_metadata(EventGateState(), TRANSITION_NONE) == {
        "state": GATE_STATE_INACTIVE,
        "transition": TRANSITION_NONE,
        "active_since": None,
        "consecutive_true": 0,
        "consecutive_false": 0,
    }
