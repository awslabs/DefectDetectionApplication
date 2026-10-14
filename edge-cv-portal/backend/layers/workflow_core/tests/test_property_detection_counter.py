# Feature: rtsp-rtmp-stream-cameras, Property 24: Counter correctness
"""Property test P24 — the detection counter agrees with a reference oracle.

**Feature: rtsp-rtmp-stream-cameras, Property 24: Counter correctness**

*For any* Detection_List, parameters, and frame size, ``count_detections``
SHALL equal a reference oracle that:

1. Filters by confidence and by the zone rule.
2. Groups by Label_Key.
3. Zero-fills the listed classes.
4. Sums the total.

**Validates: Requirements 13.2, 13.3, 13.5**

``count_detections`` is the whole of a ``detection_counter`` node: what it
returns becomes ``counter.<nodeId>.counts.<Label_Key>``,
``counter.<nodeId>.total`` and ``counter.<nodeId>.labels`` in the run
metadata, which conditions, output templates and the event gate then read
(Requirements 13.2, 13.3). It also has to run identically on the device
and in the Portal's cloud test sandbox (Requirement 13.9), so both the
values *and* their order matter.

How each clause is checked:

1. *Filtering.* Two independent oracles, one per zone shape, so the
   geometry is never checked against a copy of itself:

   - **Rectangular zones** (:func:`_rectangle_passes`) decide membership
     by plain interval comparison — the box center inside the pixel-space
     rectangle for ``center``, overlapping intervals on both axes for
     ``overlap``. Nothing about the even-odd ray casting the module uses
     is reproduced.
   - **Convex zones** (:func:`_convex_passes`) use the two textbook
     alternatives to the module's algorithms: a half-plane sign test for
     ``center`` (instead of ray casting) and the separating axis theorem
     for ``overlap`` (instead of vertex-in-box / corner-in-polygon /
     edge-crossing). Both are exactly equivalent to the module's
     predicates for convex polygons, and share no code with them.

2. *Grouping* uses :func:`_oracle_label_key`, the glossary sentence
   re-derived by character classification (the same hand reading Property
   23 pins, deliberately repeated here so this file stands alone rather
   than importing another property test's helpers).

3. *Zero-filling and order.* The oracle builds ``counts`` as the
   configured classes first, then the newly seen labels in Detection_List
   order, and the properties compare the **ordered items**, not just the
   mappings, because parity between the device and the sandbox is a
   byte-level promise.

4. *The total* is asserted both against the oracle and as a standalone
   invariant (``total == sum(counts.values())``) over a wider corpus.

Beyond the oracle equality, the properties pin the relations a reference
oracle cannot express on its own: monotonicity in ``min_confidence``,
``center`` never being looser than ``overlap``, invariance under a
uniform rescaling of the frame (the Zone being normalized), the
documented boundary-inclusive geometry, the Requirement 13.5 warning for
a missing Detection_List and the Requirement 13.6 error for a Zone that
cannot be applied, totality, determinism and the merged metadata shape.

Scope notes:

- The generators produce the Zone and the ``classes`` list as structured
  values and hand ``count_detections`` the *parameter* spelling of them
  (a JSON string, an object list, a comma-separated string), while the
  oracle consumes the structured value. The parsing of malformed
  parameters is therefore not re-implemented here: ``parse_zone`` and
  ``parse_label_list`` have their own deterministic suite and are what
  validator rule V13 reports, and the two "unusable Zone" properties
  below cover the outcome a malformed value produces at run time.
- Box coordinates are drawn from a grid of odd multiples of ``1/160``
  and Zone coordinates from multiples of ``1/20``, both scaled by the
  frame size. No box edge and no box center can then coincide with a
  rectangular Zone edge, so the rectangular oracle never has to judge an
  exact-boundary case; separation is at least ``width/160`` pixels, far
  above the module's relative boundary tolerance. Convex zones have no
  such guarantee (their vertices come from an ellipse), so those
  examples assume a decision margin. Exact-boundary behaviour is pinned
  separately, and deliberately, by
  :func:`test_counter_zone_boundary_is_inclusive`.
- Confidences are generated as numbers or as absent; a confidence or a
  threshold written as text is read as its number, which
  :func:`test_counter_reads_numeric_text_like_numbers` pins instead of
  duplicating in the oracle.
"""

from __future__ import annotations

import json
import math
import string
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.analytics.scene import (
    COUNTER_METADATA_KEYS,
    DEFAULT_ZONE_RULE,
    MAX_ZONE_POINTS,
    MIN_ZONE_POINTS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_WARNING,
    ZONE_RULE_CENTER,
    ZONE_RULE_OVERLAP,
    ZONE_RULES,
    count_detections,
    run_metadata,
)

Point = Tuple[float, float]
Bounds = Tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# Oracle part 1: the Label_Key, re-derived from the glossary sentence
# ---------------------------------------------------------------------------

#: The characters a Label_Key may contain, classified after lowercasing.
_KEY_CHARACTERS = frozenset(string.ascii_lowercase + string.digits)


def _oracle_label_key(label: Any) -> str:
    """The Label_Key of ``label``, straight from the glossary sentence.

    Lowercase; replace each run of characters other than ASCII letters and
    digits by one underscore; strip leading and trailing underscores.
    Written with character classification and an explicit run collapse, so
    it shares no machinery with the module's regular expression.
    """
    if label is None:
        return ""
    text = label if isinstance(label, str) else str(label)
    pieces: List[str] = []
    in_run = False
    for character in text.lower():
        if character in _KEY_CHARACTERS:
            pieces.append(character)
            in_run = False
        else:
            if not in_run:
                pieces.append("_")
            in_run = True
    return "".join(pieces).strip("_")


# ---------------------------------------------------------------------------
# Oracle part 2: reading a Detection_List entry
# ---------------------------------------------------------------------------


def _oracle_confidence(entry: Dict[str, Any]) -> float:
    """An entry's confidence; 0 when it is missing or not a number.

    The module's documented reading: "an entry without a numeric
    confidence is read as confidence 0". ``bool`` is not a number and
    ``nan``/``inf`` are not usable.
    """
    value = entry.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return float(value)


def _oracle_box_bounds(entry: Dict[str, Any]) -> Optional[Bounds]:
    """An entry's box as ordered ``(x_min, y_min, x_max, y_max)``.

    ``None`` when any coordinate is missing or unusable, which is the
    module's "an entry without a usable box fails the Zone test".
    """
    values = [
        entry.get("x_min"),
        entry.get("y_min"),
        entry.get("x_max"),
        entry.get("y_max"),
    ]
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return None
    x_one, y_one, x_two, y_two = (float(value) for value in values)
    return (
        min(x_one, x_two),
        min(y_one, y_two),
        max(x_one, x_two),
        max(y_one, y_two),
    )


def _box_center(bounds: Bounds) -> Point:
    return ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)


# ---------------------------------------------------------------------------
# Oracle part 3a: rectangular zones, by interval comparison
# ---------------------------------------------------------------------------


def _rectangle_of(polygon: Sequence[Point]) -> Bounds:
    """The axis-aligned rectangle spanned by ``polygon``'s points."""
    xs = [x for x, _y in polygon]
    ys = [y for _x, y in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _rectangle_passes(bounds: Optional[Bounds], polygon, rule: str) -> bool:
    """Whether a box passes a *rectangular* Zone, by interval comparison.

    No ray casting, no edge crossing: ``center`` is "the center's x lies
    between the rectangle's x bounds, and likewise for y", and ``overlap``
    is "the x intervals meet and the y intervals meet".
    """
    if bounds is None:
        return False
    left, bottom, right, top = _rectangle_of(polygon)
    if rule == ZONE_RULE_OVERLAP:
        return (
            bounds[0] <= right
            and bounds[2] >= left
            and bounds[1] <= top
            and bounds[3] >= bottom
        )
    center_x, center_y = _box_center(bounds)
    return left <= center_x <= right and bottom <= center_y <= top


# ---------------------------------------------------------------------------
# Oracle part 3b: convex zones, by half-planes and the separating axis
# ---------------------------------------------------------------------------


def _edge_normals(polygon: Sequence[Point]) -> List[Point]:
    """The unit outward normals of ``polygon``'s edges."""
    axes: List[Point] = []
    count = len(polygon)
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        edge_x, edge_y = bx - ax, by - ay
        length = math.hypot(edge_x, edge_y)
        if length == 0.0:
            continue
        axes.append((-edge_y / length, edge_x / length))
    return axes


def _projection(axis: Point, points: Sequence[Point]) -> Tuple[float, float]:
    values = [axis[0] * x + axis[1] * y for x, y in points]
    return min(values), max(values)


def _convex_contains(point: Point, polygon: Sequence[Point]) -> Tuple[bool, float]:
    """Half-plane point-in-convex-polygon test, with its decision margin.

    A point is inside a convex polygon exactly when it lies on the same
    side of every edge. Returns ``(inside, margin)``, the margin being the
    smallest distance from the point to any edge *line* — the amount by
    which the signs could be perturbed.
    """
    count = len(polygon)
    positive = False
    negative = False
    margin = float("inf")
    for index in range(count):
        ax, ay = polygon[index]
        bx, by = polygon[(index + 1) % count]
        cross = (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax)
        length = math.hypot(bx - ax, by - ay)
        if length == 0.0:
            continue
        margin = min(margin, abs(cross) / length)
        if cross > 0.0:
            positive = True
        elif cross < 0.0:
            negative = True
    return (not (positive and negative)), margin


def _convex_intersects(bounds: Bounds, polygon: Sequence[Point]) -> Tuple[bool, float]:
    """Separating axis test for a box against a convex polygon.

    Two convex shapes are disjoint exactly when some axis separates their
    projections; the candidate axes are the edge normals of both shapes.
    Returns ``(intersects, margin)``, the margin being the smallest
    absolute projection gap over all axes.
    """
    left, bottom, right, top = bounds
    corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    axes: List[Point] = [(1.0, 0.0), (0.0, 1.0)]
    axes.extend(_edge_normals(polygon))

    intersects = True
    margin = float("inf")
    for axis in axes:
        low_box, high_box = _projection(axis, corners)
        low_zone, high_zone = _projection(axis, polygon)
        gap = max(low_box, low_zone) - min(high_box, high_zone)
        margin = min(margin, abs(gap))
        if gap > 0.0:
            intersects = False
    return intersects, margin


def _convex_passes(
    bounds: Optional[Bounds], polygon: Sequence[Point], rule: str
) -> Tuple[bool, float]:
    """Whether a box passes a *convex* Zone, and the decision margin."""
    if bounds is None:
        return False, float("inf")
    if rule == ZONE_RULE_OVERLAP:
        return _convex_intersects(bounds, polygon)
    return _convex_contains(_box_center(bounds), polygon)


# ---------------------------------------------------------------------------
# Oracle part 4: the counter itself
# ---------------------------------------------------------------------------


def _scale(polygon: Sequence[Point], frame_size: Tuple[float, float]) -> List[Point]:
    """A normalized Zone in pixel space."""
    width, height = frame_size
    return [(x * width, y * height) for x, y in polygon]


def _oracle_counter(
    detections: Any,
    *,
    class_labels: Sequence[Any],
    min_confidence: float,
    zone_points: Optional[Sequence[Point]],
    zone_rule: Any,
    frame_size: Optional[Tuple[float, float]],
    passes,
) -> Dict[str, Any]:
    """The reference counter of Property 24.

    ``passes(bounds, scaled_polygon, rule) -> bool`` is the independent
    geometry oracle for the Zone shape under test. Returns the counter's
    run metadata plus the ``outcome`` the requirements prescribe.
    """
    counts: Dict[str, int] = {}
    labels: Dict[str, str] = {}
    for label in class_labels:
        # An entry of a comma-separated list is the text between the
        # commas, trimmed: the separator's whitespace is not part of the
        # label, whichever spelling the parameter arrived in.
        written = (label if isinstance(label, str) else str(label)).strip()
        key = _oracle_label_key(written)
        if key == "" or key in counts:
            continue
        counts[key] = 0
        labels[key] = written

    outcome = OUTCOME_OK
    entries: List[Dict[str, Any]]
    if isinstance(detections, list):
        entries = [entry for entry in detections if isinstance(entry, dict)]
    else:
        # Requirement 13.5: no Detection_List is a warning, not a failure.
        outcome = OUTCOME_WARNING
        entries = []

    scaled: Optional[List[Point]] = None
    if zone_points is not None:
        if frame_size is None:
            # Requirement 13.6: a Zone that cannot be applied is an error.
            outcome = OUTCOME_ERROR
            entries = []
        else:
            scaled = _scale(zone_points, frame_size)

    rule = zone_rule if zone_rule in ZONE_RULES else DEFAULT_ZONE_RULE

    total = 0
    spelled_by_detection: set = set()
    for entry in entries:
        if _oracle_confidence(entry) < min_confidence:
            continue
        if scaled is not None and not passes(_oracle_box_bounds(entry), scaled, rule):
            continue
        key = _oracle_label_key(entry.get("label"))
        counts[key] = counts.get(key, 0) + 1
        if key not in spelled_by_detection:
            label = entry.get("label")
            labels[key] = (
                "" if label is None else label if isinstance(label, str) else str(label)
            )
            spelled_by_detection.add(key)
        total += 1

    return {
        "counts": counts,
        "total": total,
        "labels": labels,
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Frame sizes a detector reports, as ``(width, height)`` pixels.
_FRAME_SIZES = (
    (320, 240),
    (640, 480),
    (1280, 720),
    (1920, 1080),
    (256, 256),
    (1000, 600),
)

#: Detector labels. Deliberately mixed case, spaced, punctuated,
#: non-ASCII and unaddressable ("-", ""), because the grouping is by
#: Label_Key and collisions between spellings are the interesting case.
#: No label contains a comma, so a ``classes`` list can be joined into the
#: parameter spelling.
_LABELS = (
    "person",
    "Person",
    "PERSON",
    "hard hat",
    "hardhat",
    "Hard-Hat",
    "hard_hat",
    "safety vest",
    "forklift",
    "3M",
    "widget_7",
    "P\u00e9rson",
    "\u65e5\u672c",
    "-",
    "",
)

#: Box coordinates are odd multiples of 1/160 of a frame dimension;
#: Zone coordinates are multiples of 1/20. An odd multiple of 1/160 can
#: never equal 8k/160, and neither can the mean of two of them
#: (odd + odd = 2 mod 4, so the mean is again odd/160), so no box edge and
#: no box center ever lands on a rectangular Zone edge.
_ZONE_GRID = st.integers(min_value=1, max_value=19).map(lambda k: k / 20.0)


@st.composite
def _box_extent(draw: Any) -> Tuple[float, float]:
    """One axis of a box, as two normalized grid coordinates.

    Half the boxes span the frame freely and half are small (within four
    grid steps), because a small box is what tells the ``overlap`` rule
    apart from a rule that accepts everything. Adding a multiple of
    ``4/160`` keeps both ends, and hence the center, off the Zone grid.
    """
    low = draw(st.integers(min_value=0, max_value=40))
    if draw(st.booleans()):
        high = draw(st.integers(min_value=0, max_value=40))
    else:
        high = low + draw(st.integers(min_value=0, max_value=3))
    return (4 * low + 1) / 160.0, (4 * high + 1) / 160.0


def _confidences() -> st.SearchStrategy[Any]:
    """Confidences as a detector reports them, or absent."""
    return st.one_of(
        st.sampled_from([0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]),
        st.floats(min_value=0.0, max_value=1.0, width=32),
        st.none(),
    )


def _thresholds() -> st.SearchStrategy[float]:
    return st.one_of(
        st.sampled_from([0.0, 0.25, 0.5, 0.75, 1.0]),
        st.floats(min_value=0.0, max_value=1.0, width=32),
    )


@st.composite
def _detection_lists(draw: Any, frame_size: Tuple[int, int]) -> List[Dict[str, Any]]:
    """A Detection_List in the pixel space of ``frame_size``.

    Some entries carry no box at all (a detector that reported only a
    label), and boxes may be written with their coordinates inverted.
    """
    width, height = frame_size
    size = draw(st.integers(min_value=0, max_value=6))
    entries: List[Dict[str, Any]] = []
    for index in range(size):
        entry: Dict[str, Any] = {
            "id": "d{0}".format(index),
            "label": draw(st.sampled_from(_LABELS)),
            "confidence": draw(_confidences()),
        }
        if draw(st.integers(min_value=0, max_value=9)) > 0:
            x_low, x_high = draw(_box_extent())
            y_low, y_high = draw(_box_extent())
            entry["x_min"] = x_low * width
            entry["x_max"] = x_high * width
            entry["y_min"] = y_low * height
            entry["y_max"] = y_high * height
        entries.append(entry)
    return entries


@st.composite
def _rectangular_zones(draw: Any) -> List[Point]:
    """A rectangular Zone, as four normalized points."""
    x_one = draw(_ZONE_GRID)
    x_two = draw(_ZONE_GRID)
    y_one = draw(_ZONE_GRID)
    y_two = draw(_ZONE_GRID)
    assume(x_one != x_two and y_one != y_two)
    left, right = min(x_one, x_two), max(x_one, x_two)
    bottom, top = min(y_one, y_two), max(y_one, y_two)
    return [(left, bottom), (right, bottom), (right, top), (left, top)]


@st.composite
def _convex_zones(draw: Any) -> List[Point]:
    """A strictly convex Zone: points of an ellipse, sorted by angle.

    Distinct angles on an ellipse are never collinear, so the polygon is
    convex and the half-plane and separating-axis oracles apply. The
    radii and center keep every coordinate inside 0 to 1, so the Zone is
    well formed.
    """
    count = draw(st.integers(min_value=MIN_ZONE_POINTS, max_value=8))
    angles = draw(
        st.lists(
            st.integers(min_value=0, max_value=359),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    angles.sort()
    radius_x = draw(st.sampled_from([0.1, 0.2, 0.3, 0.4]))
    radius_y = draw(st.sampled_from([0.1, 0.2, 0.3, 0.4]))
    center_x = 0.5 + draw(st.sampled_from([-0.05, 0.0, 0.05]))
    center_y = 0.5 + draw(st.sampled_from([-0.05, 0.0, 0.05]))
    return [
        (
            center_x + radius_x * math.cos(math.radians(angle)),
            center_y + radius_y * math.sin(math.radians(angle)),
        )
        for angle in angles
    ]


def _class_lists() -> st.SearchStrategy[List[str]]:
    """A ``classes`` parameter as a list of labels, commas excluded."""
    return st.lists(
        st.one_of(
            st.sampled_from(_LABELS),
            st.text(
                alphabet=string.ascii_letters + string.digits + " -_",
                max_size=6,
            ),
        ),
        max_size=5,
    )


def _zone_parameter(points: Sequence[Point], form: str) -> Any:
    """A Zone in one of the spellings a caller may hand the counter."""
    if form == "json":
        return json.dumps([[x, y] for x, y in points])
    if form == "objects":
        return json.dumps([{"x": x, "y": y} for x, y in points])
    if form == "wrapped":
        return json.dumps({"points": [[x, y] for x, y in points]})
    return [(x, y) for x, y in points]


def _classes_parameter(labels: Sequence[str], form: str) -> Any:
    """A ``classes`` list as written, or already split."""
    if form == "text":
        return ",".join(labels)
    return list(labels)


@st.composite
def _counter_cases(draw: Any, zones: st.SearchStrategy[List[Point]]) -> Dict[str, Any]:
    """One counter invocation: detections, parameters and frame size."""
    frame_size = draw(st.sampled_from(_FRAME_SIZES))
    return {
        "frame_size": frame_size,
        "detections": draw(_detection_lists(frame_size)),
        "class_labels": draw(_class_lists()),
        "min_confidence": draw(_thresholds()),
        "zone_points": draw(st.one_of(st.none(), zones)),
        "zone_rule": draw(st.sampled_from(ZONE_RULES)),
        "zone_form": draw(st.sampled_from(["json", "objects", "wrapped", "list"])),
        "classes_form": draw(st.sampled_from(["text", "list"])),
    }


def _invoke(case: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """Call ``count_detections`` with a case's parameter spellings."""
    zone_points = overrides.get("zone_points", case["zone_points"])
    zone = (
        None
        if zone_points is None
        else _zone_parameter(zone_points, case["zone_form"])
    )
    return count_detections(
        overrides.get("detections", case["detections"]),
        classes=_classes_parameter(case["class_labels"], case["classes_form"]),
        min_confidence=overrides.get("min_confidence", case["min_confidence"]),
        zone=zone,
        zone_rule=overrides.get("zone_rule", case["zone_rule"]),
        frame_size=overrides.get("frame_size", case["frame_size"]),
    )


def _expect(case: Dict[str, Any], passes, **overrides: Any) -> Dict[str, Any]:
    """The oracle's answer for a case."""
    return _oracle_counter(
        overrides.get("detections", case["detections"]),
        class_labels=case["class_labels"],
        min_confidence=overrides.get("min_confidence", case["min_confidence"]),
        zone_points=overrides.get("zone_points", case["zone_points"]),
        zone_rule=overrides.get("zone_rule", case["zone_rule"]),
        frame_size=overrides.get("frame_size", case["frame_size"]),
        passes=passes,
    )


def _assert_matches_oracle(result: Dict[str, Any], expected: Dict[str, Any]) -> None:
    """The result equals the oracle, ordering included."""
    assert result["counts"] == expected["counts"], (result, expected)
    assert list(result["counts"].items()) == list(expected["counts"].items()), (
        result["counts"],
        expected["counts"],
    )
    assert result["total"] == expected["total"], (result, expected)
    assert result["labels"] == expected["labels"], (result, expected)
    assert result["outcome"] == expected["outcome"], (result, expected)


# ---------------------------------------------------------------------------
# Clauses 1 to 4: equality with the reference oracle
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()))
def test_counter_equals_the_reference_oracle_with_a_rectangular_zone(
    case: Dict[str, Any],
) -> None:
    """Rectangular Zones, judged by interval comparison.

    The coordinate grids make every comparison unambiguous by at least
    ``width / 160`` pixels, so no example needs to be discarded and the
    oracle never has to reproduce the module's boundary tolerance.
    """
    _assert_matches_oracle(
        _invoke(case), _expect(case, _rectangle_passes)
    )


@settings(max_examples=100)
@given(_counter_cases(_convex_zones()))
def test_counter_equals_the_reference_oracle_with_a_convex_zone(
    case: Dict[str, Any],
) -> None:
    """Convex Zones, judged by half-planes and the separating axis.

    An ellipse's vertices are not on the box grid, so an example whose
    decision sits within a hair of the boundary is discarded: the module
    counts a touching box as inside (boundary inclusive, within a relative
    tolerance) while the oracle compares strictly, and
    :func:`test_counter_zone_boundary_is_inclusive` is what pins that
    behaviour instead.
    """
    zone_points = case["zone_points"]

    def passes(bounds: Optional[Bounds], polygon: Sequence[Point], rule: str) -> bool:
        return _convex_passes(bounds, polygon, rule)[0]

    if zone_points is not None:
        width, height = case["frame_size"]
        margin = max(width, height) * 1e-6
        scaled = _scale(zone_points, (width, height))
        for entry in case["detections"]:
            bounds = _oracle_box_bounds(entry)
            if bounds is None:
                continue
            _decision, slack = _convex_passes(bounds, scaled, case["zone_rule"])
            assume(slack >= margin)

    _assert_matches_oracle(_invoke(case), _expect(case, passes))


# ---------------------------------------------------------------------------
# Clause 3: the configured classes are always present, zero when unseen
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()))
def test_counter_zero_fills_every_configured_class(case: Dict[str, Any]) -> None:
    """Requirement 13.3: every label in ``classes`` is present, zero when
    unseen — whatever the filtering did, including when there is no
    Detection_List at all."""
    configured = []
    for label in case["class_labels"]:
        key = _oracle_label_key(label)
        if key and key not in configured:
            configured.append(key)

    for detections in (case["detections"], None, []):
        counts = _invoke(case, detections=detections)["counts"]
        assert list(counts)[: len(configured)] == configured, (configured, counts)
        for key in configured:
            assert isinstance(counts[key], int) and counts[key] >= 0, (key, counts)
    # A class no detection carries is present and zero.
    unseen = "z{0}".format("q" * 3)
    assume(all(_oracle_label_key(label) != unseen for label in case["class_labels"]))
    result = count_detections(
        case["detections"],
        classes=[unseen] + list(case["class_labels"]),
        min_confidence=case["min_confidence"],
        zone=None,
        frame_size=case["frame_size"],
    )
    assert result["counts"][unseen] == 0, result


# ---------------------------------------------------------------------------
# Clause 4, and the shape of the merged metadata
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_counter_cases(_convex_zones()))
def test_counter_total_equals_the_sum_of_the_counts(case: Dict[str, Any]) -> None:
    """``total`` is the sum of ``counts``, always — including the empty
    Label_Key of a label with nothing addressable in it, which is why it
    is kept as a key at all."""
    result = _invoke(case)
    assert result["total"] == sum(result["counts"].values()), result
    assert result["total"] >= 0
    assert all(isinstance(value, int) for value in result["counts"].values()), result


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()))
def test_counter_labels_map_every_counted_key_to_an_original_label(
    case: Dict[str, Any],
) -> None:
    """``labels`` covers exactly the keys of ``counts``, each mapping back
    to a spelling whose Label_Key is that key (Requirement 13.3), and a
    detector's spelling wins over the one written in ``classes``."""
    result = _invoke(case)
    assert set(result["labels"]) == set(result["counts"]), result
    for key, original in result["labels"].items():
        assert isinstance(original, str), (key, original)
        assert _oracle_label_key(original) == key, (key, original)

    # The detector's spelling is the label's origin.
    detected = count_detections(
        [
            {
                "id": "d0",
                "label": "Hard-Hat",
                "confidence": 1.0,
                "x_min": 0.0,
                "y_min": 0.0,
                "x_max": 4.0,
                "y_max": 4.0,
            }
        ],
        classes="hard hat",
    )
    assert detected["labels"]["hard_hat"] == "Hard-Hat", detected


# ---------------------------------------------------------------------------
# Relations a reference oracle cannot state on its own
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()), _thresholds())
def test_counter_counts_are_monotone_in_min_confidence(
    case: Dict[str, Any], other: float
) -> None:
    """Raising ``min_confidence`` can only ever remove detections."""
    low, high = sorted([case["min_confidence"], other])
    counts_low = _invoke(case, min_confidence=low)["counts"]
    counts_high = _invoke(case, min_confidence=high)["counts"]
    for key in set(counts_low) | set(counts_high):
        assert counts_high.get(key, 0) <= counts_low.get(key, 0), (key, low, high)
    assert (
        _invoke(case, min_confidence=high)["total"]
        <= _invoke(case, min_confidence=low)["total"]
    )


@settings(max_examples=100)
@given(_counter_cases(_convex_zones()))
def test_counter_center_rule_is_never_looser_than_the_overlap_rule(
    case: Dict[str, Any],
) -> None:
    """A box whose center is in the Zone necessarily intersects it, so
    ``center`` selects a subset of what ``overlap`` selects."""
    assume(case["zone_points"] is not None)
    center = _invoke(case, zone_rule=ZONE_RULE_CENTER)["counts"]
    overlap = _invoke(case, zone_rule=ZONE_RULE_OVERLAP)["counts"]
    for key in set(center) | set(overlap):
        assert center.get(key, 0) <= overlap.get(key, 0), (key, center, overlap)


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()), st.sampled_from([0.5, 2.0, 4.0]))
def test_counter_is_invariant_under_a_uniform_rescaling(
    case: Dict[str, Any], factor: float
) -> None:
    """A Zone is normalized, so scaling the frame and every box by the
    same power of two changes nothing. Powers of two keep the scaling
    exact, so this isolates the scaling rule rather than float rounding."""
    width, height = case["frame_size"]
    scaled_detections = []
    for entry in case["detections"]:
        scaled = dict(entry)
        for key in ("x_min", "x_max", "y_min", "y_max"):
            if key in scaled:
                scaled[key] = scaled[key] * factor
        scaled_detections.append(scaled)

    original = _invoke(case)
    rescaled = _invoke(
        case,
        detections=scaled_detections,
        frame_size=(width * factor, height * factor),
    )
    assert rescaled["counts"] == original["counts"], (original, rescaled)
    assert rescaled["total"] == original["total"]


# ---------------------------------------------------------------------------
# The documented boundary-inclusive geometry
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.sampled_from([0.25, 0.5]),
    st.sampled_from([0.25, 0.5]),
    st.sampled_from([256, 512, 1024]),
    st.sampled_from([256, 512, 1024]),
    st.integers(min_value=0, max_value=7),
    st.sampled_from([2.0, 8.0, 32.0]),
)
def test_counter_zone_boundary_is_inclusive(
    left: float,
    bottom: float,
    width: int,
    height: int,
    which: int,
    half: float,
) -> None:
    """A box centered exactly on a Zone corner or edge is counted.

    The oracle properties steer clear of exact-boundary cases, so this
    pins the module's documented rule directly. Dyadic Zone coordinates
    and power-of-two frame sizes make every pixel coordinate exact, so
    "exactly on the boundary" really is exact.
    """
    right, top = left + 0.25, bottom + 0.25
    zone = [(left, bottom), (right, bottom), (right, top), (left, top)]
    pixel_left, pixel_right = left * width, right * width
    pixel_bottom, pixel_top = bottom * height, top * height
    middle_x = (pixel_left + pixel_right) / 2.0
    middle_y = (pixel_bottom + pixel_top) / 2.0
    boundary_points = [
        (pixel_left, pixel_bottom),
        (pixel_right, pixel_bottom),
        (pixel_right, pixel_top),
        (pixel_left, pixel_top),
        (middle_x, pixel_bottom),
        (pixel_right, middle_y),
        (middle_x, pixel_top),
        (pixel_left, middle_y),
    ]
    point_x, point_y = boundary_points[which]

    detections = [
        {
            "id": "d0",
            "label": "person",
            "confidence": 1.0,
            "x_min": point_x - half,
            "x_max": point_x + half,
            "y_min": point_y - half,
            "y_max": point_y + half,
        }
    ]
    for rule in ZONE_RULES:
        result = count_detections(
            detections,
            zone=json.dumps([[x, y] for x, y in zone]),
            zone_rule=rule,
            frame_size=(width, height),
        )
        assert result["counts"] == {"person": 1}, (rule, which, result)
        assert result["outcome"] == OUTCOME_OK, result


# ---------------------------------------------------------------------------
# Requirement 13.5: no Detection_List. Requirement 13.6: unusable Zone.
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    _counter_cases(_rectangular_zones()),
    st.one_of(
        st.none(),
        st.text(max_size=4),
        st.integers(),
        st.booleans(),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    ),
)
def test_counter_without_a_detection_list_warns_and_reports_zero_counts(
    case: Dict[str, Any], detections: Any
) -> None:
    """Requirement 13.5: a run with no Detection_List reports zero counts
    and a warning — never an error, and never a raised exception. An
    *empty* list is a Detection_List with no entries, so it stays ``ok``."""
    result = _invoke(case, detections=detections)
    assert result["total"] == 0, result
    assert set(result["counts"].values()) <= {0}, result
    assert result["outcome"] == OUTCOME_WARNING, result
    assert result["problems"], result

    empty = _invoke(case, detections=[])
    assert empty["total"] == 0 and empty["outcome"] == OUTCOME_OK, empty
    assert empty["problems"] == [], empty


@settings(max_examples=100)
@given(
    _counter_cases(_rectangular_zones()),
    st.one_of(
        st.none(),
        st.just((0, 0)),
        st.just((-640, 480)),
        st.just(("640", "480")),
        st.text(max_size=4),
        st.just({"width": 640}),
        st.integers(),
    ),
)
def test_counter_with_an_unusable_zone_errors_and_reports_zero_counts(
    case: Dict[str, Any], frame_size: Any
) -> None:
    """Requirement 13.6: a Zone that is configured but cannot be applied
    — here because the frame dimensions are unknown — is an error with an
    empty result, which gates the node's downstream nodes without failing
    the run. Without a Zone the same unknown frame size is harmless."""
    assume(case["zone_points"] is not None)
    result = _invoke(case, frame_size=frame_size)
    assert result["outcome"] == OUTCOME_ERROR, (frame_size, result)
    assert result["total"] == 0, result
    assert set(result["counts"].values()) <= {0}, result
    assert result["problems"], result

    without_zone = _invoke(case, zone_points=None, frame_size=frame_size)
    assert without_zone["outcome"] in (OUTCOME_OK, OUTCOME_WARNING), without_zone
    assert without_zone["total"] == _invoke(case, zone_points=None)["total"]


@settings(max_examples=100)
@given(
    _counter_cases(_rectangular_zones()),
    st.one_of(
        st.sampled_from(
            [
                "not json at all",
                "[]",
                "[[0.1, 0.2]]",
                "[[0, 0], [1, 0], [1, 1], [2, 0.5]]",
                "[[0, 0], [1, 0], [1, 1], [0.5, -0.1]]",
                '{"nope": 1}',
                '[[0, 0], [1, 0], ["a", 1]]',
                "42",
            ]
        ),
        st.lists(st.just([0.5, 0.5]), min_size=MAX_ZONE_POINTS + 1, max_size=40).map(
            json.dumps
        ),
    ),
)
def test_counter_with_a_malformed_zone_errors_and_reports_zero_counts(
    case: Dict[str, Any], zone: Any
) -> None:
    """A malformed Zone is the same class of failure as an unusable one:
    an error outcome with an empty result, never an exception and never a
    silently unfiltered count."""
    result = count_detections(
        case["detections"],
        classes=_classes_parameter(case["class_labels"], case["classes_form"]),
        min_confidence=case["min_confidence"],
        zone=zone,
        zone_rule=case["zone_rule"],
        frame_size=case["frame_size"],
    )
    assert result["outcome"] == OUTCOME_ERROR, (zone, result)
    assert result["total"] == 0, result
    assert set(result["counts"].values()) <= {0}, result
    assert result["problems"], result


# ---------------------------------------------------------------------------
# Totality, determinism, the merged shape, and numeric text
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.one_of(
        st.none(),
        st.lists(
            st.one_of(
                st.none(),
                st.text(max_size=3),
                st.integers(),
                st.dictionaries(
                    st.sampled_from(
                        [
                            "id",
                            "label",
                            "confidence",
                            "x_min",
                            "y_min",
                            "x_max",
                            "y_max",
                            "other",
                        ]
                    ),
                    st.one_of(
                        st.none(),
                        st.booleans(),
                        st.text(max_size=4),
                        st.integers(min_value=-9, max_value=9),
                        st.floats(allow_nan=True, allow_infinity=True, width=16),
                        st.lists(st.integers(min_value=0, max_value=3), max_size=2),
                    ),
                    max_size=8,
                ),
            ),
            max_size=5,
        ),
        st.text(max_size=5),
        st.integers(),
    ),
    st.one_of(st.none(), st.text(max_size=6), st.integers(), st.lists(st.text(max_size=3), max_size=3)),
    st.one_of(st.none(), st.text(max_size=4), st.floats(allow_nan=True, allow_infinity=True, width=16)),
    st.one_of(st.none(), st.text(max_size=6), st.lists(st.text(max_size=3), max_size=4)),
    st.one_of(st.none(), st.text(max_size=8), st.sampled_from(list(ZONE_RULES))),
    st.one_of(st.none(), st.text(max_size=4), st.tuples(st.integers(), st.integers())),
)
def test_counter_is_total_deterministic_and_merges_a_fixed_shape(
    detections: Any,
    classes: Any,
    min_confidence: Any,
    zone: Any,
    zone_rule: Any,
    frame_size: Any,
) -> None:
    """Nothing raises, the same input gives the same answer, and only the
    three keys of Requirement 13.3 are merged into the run metadata.

    Totality matters because these run inside a workflow run that must
    not fail on a configuration mistake, and the fixed merged shape is
    what keeps the device and the sandbox byte-identical.
    """
    result = count_detections(
        detections,
        classes=classes,
        min_confidence=min_confidence,
        zone=zone,
        zone_rule=zone_rule,
        frame_size=frame_size,
    )
    again = count_detections(
        detections,
        classes=classes,
        min_confidence=min_confidence,
        zone=zone,
        zone_rule=zone_rule,
        frame_size=frame_size,
    )
    assert result == again
    assert list(result["counts"].items()) == list(again["counts"].items())

    assert set(result) == set(COUNTER_METADATA_KEYS) | {"outcome", "problems"}
    assert tuple(run_metadata(result)) == COUNTER_METADATA_KEYS, result
    assert result["outcome"] in (OUTCOME_OK, OUTCOME_WARNING, OUTCOME_ERROR)
    assert isinstance(result["problems"], list)
    assert result["total"] == sum(result["counts"].values())
    assert all(isinstance(key, str) for key in result["counts"])
    assert all(isinstance(value, int) for value in result["counts"].values())
    assert set(result["labels"]) == set(result["counts"])


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()))
def test_counter_reads_numeric_text_like_numbers(case: Dict[str, Any]) -> None:
    """A confidence or a threshold that arrived as text is read as its
    number, so a detector that serializes floats as strings counts the
    same as one that does not."""
    as_text = []
    for entry in case["detections"]:
        copied = dict(entry)
        if isinstance(copied.get("confidence"), float):
            copied["confidence"] = repr(copied["confidence"])
        as_text.append(copied)

    numeric = _invoke(case)
    textual = _invoke(
        case, detections=as_text, min_confidence=" {0} ".format(case["min_confidence"])
    )
    assert textual["counts"] == numeric["counts"], (numeric, textual)
    assert textual["total"] == numeric["total"]


@settings(max_examples=100)
@given(_counter_cases(_rectangular_zones()), st.text(max_size=8))
def test_counter_falls_back_to_the_default_zone_rule(
    case: Dict[str, Any], rule: str
) -> None:
    """An unknown ``zone_rule`` behaves as the catalog default rather than
    dropping the Zone or raising."""
    assume(rule not in ZONE_RULES)
    assert _invoke(case, zone_rule=rule) == _invoke(
        case, zone_rule=DEFAULT_ZONE_RULE
    )
