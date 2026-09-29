# Feature: rtsp-rtmp-stream-cameras, Property 25: Association correctness
"""Property test P25 — object association agrees with a reference oracle.

**Feature: rtsp-rtmp-stream-cameras, Property 25: Association correctness**

*For any* Detection_List and parameters, ``associate`` SHALL:

- Use each required detection at most once per class.
- Satisfy only pairs that meet ``min_overlap``.
- Report ``compliant + violations = subjects``.
- Report ``missing[c]`` equal to the number of subjects with no class-c
  match.
- List exactly the ids of the non-compliant subjects.
- Be deterministic for identical input.

**Validates: Requirements 14.2, 14.3, 14.4**

``associate`` is the whole of an ``object_association`` node: what it
returns becomes ``association.<nodeId>.subjects``, ``.compliant``,
``.violations``, ``.missing.<Label_Key>`` and ``.violating_ids`` in the run
metadata, which conditions, output templates and the event gate then read
(Requirement 14.4). It also has to run identically on the device and in
the Portal's cloud test sandbox (Requirement 14.6), so both the values
*and* their order matter — every oracle comparison below therefore
compares the **ordered** items of ``missing`` and the **ordered**
``violating_ids``, not just their contents.

How each clause is checked:

1. *The matching* is judged by :func:`_oracle_greedy_match`, an
   independent formulation of the design's rule ("sort a class's pairs by
   overlap descending, then subject order, then detection order, and
   assign greedily"): it repeatedly takes the **best still-available
   pair** (iterated arg-max with the same tie-break) instead of sorting
   once and walking the list. The two are exactly equivalent, and share
   no code.
2. *The overlap rule* is re-derived from the requirement sentence
   ("at least ``min_overlap`` of its *own* box area lies inside the
   subject's box") by clipping the candidate box to the subject box and
   dividing areas. The arithmetic is deliberately performed in the same
   order as the module's, so a pair sitting exactly on the threshold
   (common here — the generated boxes live on a grid, so overlaps are
   exact fractions such as ``0.5``) is decided identically by both and
   the inclusive ``>=`` of Requirement 14.3 is genuinely exercised
   rather than dodged.
3. *Subject selection* (Requirement 14.2: subject Label_Key, confidence
   at least ``min_confidence``, box center inside the Zone when one is
   set) uses two independent zone oracles, so the geometry is never
   checked against a copy of itself: a plain interval comparison for
   rectangular zones and a half-plane sign test for convex ones, neither
   of which reproduces the module's even-odd ray casting.
4. *Label grouping* uses :func:`_oracle_label_key`, the glossary sentence
   re-derived by character classification (the same hand reading
   Property 23 pins, repeated here so this file stands alone rather than
   importing another property test's helpers).

Beyond the oracle equality, the properties pin what a reference oracle
cannot state on its own: the one-to-one bounds on a class's matching, the
"one hard hat cannot make two people compliant" scarcity case, the
larger-overlap preference and its tie-break, monotonicity in
``min_overlap`` (raising the threshold can only remove matches — the
greedy walk over the surviving pairs is unchanged), invariance under a
power-of-two rescaling of the frame, the Requirement 14.6 outcomes (no
Detection_List, an unusable Zone, a missing required parameter),
totality, determinism and the merged metadata shape.

Scope notes, all of them deliberate:

- ``min_overlap`` is generated inside the catalog's declared range
  (0.05 to 1). At ``0`` the inclusive comparison would pair every
  candidate with every subject, including disjoint boxes; that is the
  module's documented reading of ``>=`` but not a configuration the
  descriptor allows, so it is out of this corpus.
- Confidences are generated as numbers or as absent. A confidence, a
  threshold or an overlap written as text is read as its number, which
  :func:`test_association_reads_numeric_text_like_numbers` pins instead
  of duplicating in the oracle.
- ``subject_class`` may also appear in ``required_classes`` (the label
  pools overlap on ``person``). A subject then matches *itself* with
  overlap 1.0 — the literal consequence of the design's rule, reproduced
  by the oracle, and worth keeping in the corpus because it is a
  configuration an operator can actually type.
- The Zone and the ``required_classes`` list are generated as structured
  values and handed to ``associate`` in every parameter spelling (JSON
  points, ``{"x","y"}`` objects, a ``points`` wrapper, a pre-parsed list;
  comma-joined text or an already-split list), while the oracle consumes
  the structure. Parameter *parsing* is therefore not re-implemented
  here: ``parse_zone`` and ``parse_label_list`` have their own
  deterministic suite and are what validator rule V13 reports, and the
  outcome a malformed value produces at run time is pinned by
  :func:`test_association_reports_the_outcome_of_an_unusable_zone`.
- Box coordinates are multiples of a frame dimension over 16, so a box
  center is a multiple of a dimension over 32. Rectangular zones are
  judged by an interval comparison that is boundary-inclusive exactly as
  the module is, so an exact-boundary center needs no special handling;
  convex zones come from an ellipse, so those examples assume a decision
  margin, as the counter's property test does.
"""

from __future__ import annotations

import json
import math
import string
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.analytics.scene import (
    ASSOCIATION_METADATA_KEYS,
    DEFAULT_MIN_OVERLAP,
    MIN_ZONE_POINTS,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_WARNING,
    associate,
    run_metadata,
)

Point = Tuple[float, float]
Bounds = Tuple[float, float, float, float]
CellBox = Tuple[int, int, int, int]


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


def _oracle_required_keys(labels: Sequence[Any]) -> List[str]:
    """The Label_Keys of a ``required_classes`` list, de-duplicated.

    An entry of a comma-separated list is the text between the commas,
    trimmed, whichever spelling the parameter arrived in; an entry with
    nothing addressable in it has no key and is dropped.
    """
    keys: List[str] = []
    for label in labels:
        written = (label if isinstance(label, str) else str(label)).strip()
        key = _oracle_label_key(written)
        if key and key not in keys:
            keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Oracle part 2: reading a Detection_List entry
# ---------------------------------------------------------------------------


def _oracle_confidence(entry: Dict[str, Any]) -> float:
    """An entry's confidence; 0 when it is missing or not a number."""
    value = entry.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return float(value)


def _oracle_box_bounds(entry: Any) -> Optional[Bounds]:
    """An entry's box as ordered ``(x_min, y_min, x_max, y_max)``.

    ``None`` when any coordinate is missing or unusable: such a detection
    can neither pass a Zone nor take part in a pair.
    """
    if not isinstance(entry, dict):
        return None
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
# Oracle part 3: the overlap rule of Requirement 14.3
# ---------------------------------------------------------------------------


def _oracle_overlap(inner: Optional[Bounds], outer: Optional[Bounds]) -> float:
    """The fraction of ``inner``'s area that lies inside ``outer``.

    Requirement 14.3, read literally: clip the candidate box to the
    subject box and divide the clipped area by the candidate's own area.
    A candidate with no area lies nowhere, so it contributes 0.
    """
    if inner is None or outer is None:
        return 0.0
    inner_width = inner[2] - inner[0]
    inner_height = inner[3] - inner[1]
    if inner_width * inner_height <= 0.0:
        return 0.0
    left = max(inner[0], outer[0])
    right = min(inner[2], outer[2])
    bottom = max(inner[1], outer[1])
    top = min(inner[3], outer[3])
    if right - left <= 0.0 or top - bottom <= 0.0:
        return 0.0
    return ((right - left) * (top - bottom)) / (inner_width * inner_height)


def _oracle_greedy_match(
    subject_bounds: Sequence[Optional[Bounds]],
    candidate_bounds: Sequence[Optional[Bounds]],
    threshold: float,
) -> Set[int]:
    """The subjects matched for one required class.

    The design's rule, formulated as an iterated arg-max instead of a
    single sorted pass: while some still-available pair meets the
    threshold, take the best one — largest overlap, then the earliest
    subject, then the earliest detection — and retire both of its ends.
    A subject without a usable box takes part in no pair.
    """
    available_subjects = set(
        index
        for index, bounds in enumerate(subject_bounds)
        if bounds is not None
    )
    available_candidates = set(range(len(candidate_bounds)))
    matched: Set[int] = set()

    while available_subjects and available_candidates:
        best: Optional[Tuple[float, int, int]] = None
        for subject_index in sorted(available_subjects):
            for candidate_index in sorted(available_candidates):
                overlap = _oracle_overlap(
                    candidate_bounds[candidate_index],
                    subject_bounds[subject_index],
                )
                if overlap < threshold:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, subject_index, candidate_index)
        if best is None:
            break
        _overlap, subject_index, candidate_index = best
        matched.add(subject_index)
        available_subjects.discard(subject_index)
        available_candidates.discard(candidate_index)
    return matched


# ---------------------------------------------------------------------------
# Oracle part 4a: rectangular zones, by interval comparison
# ---------------------------------------------------------------------------


def _rectangle_of(polygon: Sequence[Point]) -> Bounds:
    """The axis-aligned rectangle spanned by ``polygon``'s points."""
    xs = [x for x, _y in polygon]
    ys = [y for _x, y in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _rectangle_contains(point: Point, polygon: Sequence[Point]) -> bool:
    """Point in a *rectangular* Zone, by plain interval comparison.

    No ray casting: "the x lies between the rectangle's x bounds, and
    likewise for y". Boundary inclusive, exactly as the module documents.
    """
    left, bottom, right, top = _rectangle_of(polygon)
    return left <= point[0] <= right and bottom <= point[1] <= top


# ---------------------------------------------------------------------------
# Oracle part 4b: convex zones, by half-planes
# ---------------------------------------------------------------------------


def _convex_contains(point: Point, polygon: Sequence[Point]) -> Tuple[bool, float]:
    """Half-plane point-in-convex-polygon test, with its decision margin.

    A point is inside a convex polygon exactly when it lies on the same
    side of every edge (a point *on* an edge line is inside, which is the
    module's boundary-inclusive reading). Returns ``(inside, margin)``,
    the margin being the smallest distance from the point to any edge
    line — the amount by which the signs could be perturbed.
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


def _convex_contains_only(point: Point, polygon: Sequence[Point]) -> bool:
    return _convex_contains(point, polygon)[0]


# ---------------------------------------------------------------------------
# Oracle part 5: association itself
# ---------------------------------------------------------------------------

_OUTCOME_SEVERITY = {OUTCOME_OK: 0, OUTCOME_WARNING: 1, OUTCOME_ERROR: 2}


def _worse(current: str, candidate: str) -> str:
    if _OUTCOME_SEVERITY[candidate] > _OUTCOME_SEVERITY[current]:
        return candidate
    return current


def _scale(polygon: Sequence[Point], frame_size: Tuple[float, float]) -> List[Point]:
    """A normalized Zone in pixel space."""
    width, height = frame_size
    return [(x * width, y * height) for x, y in polygon]


def _oracle_entries(detections: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """The usable entries, and whether the Detection_List is missing."""
    if not isinstance(detections, (list, tuple)):
        return [], True
    return [entry for entry in detections if isinstance(entry, dict)], False


def _oracle_subjects(
    entries: Sequence[Dict[str, Any]],
    *,
    subject_key: str,
    min_confidence: float,
    scaled_zone: Optional[Sequence[Point]],
    contains,
) -> List[Dict[str, Any]]:
    """Requirement 14.2: the subjects of a run.

    The detections whose Label_Key is the subject's, whose confidence
    reaches ``min_confidence``, and whose box center lies inside the Zone
    when one is configured.
    """
    subjects: List[Dict[str, Any]] = []
    for entry in entries:
        if _oracle_label_key(entry.get("label")) != subject_key:
            continue
        if _oracle_confidence(entry) < min_confidence:
            continue
        bounds = _oracle_box_bounds(entry)
        if scaled_zone is not None:
            if bounds is None:
                continue
            if not contains(_box_center(bounds), scaled_zone):
                continue
        subjects.append(entry)
    return subjects


def _oracle_associate(
    detections: Any,
    *,
    subject_class: Any,
    required_labels: Sequence[Any],
    min_overlap: float,
    min_confidence: float,
    zone_points: Optional[Sequence[Point]],
    frame_size: Optional[Tuple[float, float]],
    contains,
) -> Dict[str, Any]:
    """The reference association of Property 25.

    ``contains(point, scaled_polygon) -> bool`` is the independent zone
    oracle for the Zone shape under test. Returns the association's run
    metadata plus the ``outcome`` the requirements prescribe.
    """
    subject_key = _oracle_label_key(subject_class)
    required_keys = _oracle_required_keys(required_labels)
    missing = {key: 0 for key in required_keys}

    outcome = OUTCOME_OK
    if not subject_key or not required_keys:
        # A missing required parameter is an error, not a vacuous
        # "everything is compliant".
        outcome = _worse(outcome, OUTCOME_ERROR)

    entries, no_list = _oracle_entries(detections)
    if no_list:
        # Requirement 13.5 through 14.6: no Detection_List is a warning.
        outcome = _worse(outcome, OUTCOME_WARNING)

    scaled_zone: Optional[List[Point]] = None
    if zone_points is not None:
        if frame_size is None:
            # Requirement 13.6 through 14.6: a Zone that cannot be
            # applied is an error.
            outcome = _worse(outcome, OUTCOME_ERROR)
        else:
            scaled_zone = _scale(zone_points, frame_size)

    if outcome == OUTCOME_ERROR:
        return {
            "subjects": 0,
            "compliant": 0,
            "violations": 0,
            "missing": missing,
            "violating_ids": [],
            "outcome": outcome,
        }

    subjects = _oracle_subjects(
        entries,
        subject_key=subject_key,
        min_confidence=min_confidence,
        scaled_zone=scaled_zone,
        contains=contains,
    )
    subject_bounds = [_oracle_box_bounds(subject) for subject in subjects]

    matched_keys: List[Set[str]] = [set() for _subject in subjects]
    for key in required_keys:
        candidates = [
            entry
            for entry in entries
            if _oracle_label_key(entry.get("label")) == key
        ]
        matched = _oracle_greedy_match(
            subject_bounds,
            [_oracle_box_bounds(candidate) for candidate in candidates],
            min_overlap,
        )
        for subject_index in matched:
            matched_keys[subject_index].add(key)
        missing[key] = len(subjects) - len(matched)

    compliant = 0
    violating_ids: List[str] = []
    for subject_index, subject in enumerate(subjects):
        if all(key in matched_keys[subject_index] for key in required_keys):
            compliant += 1
            continue
        detection_id = subject.get("id")
        if detection_id is not None:
            violating_ids.append(
                detection_id
                if isinstance(detection_id, str)
                else str(detection_id)
            )

    return {
        "subjects": len(subjects),
        "compliant": compliant,
        "violations": len(subjects) - compliant,
        "missing": missing,
        "violating_ids": violating_ids,
        "outcome": outcome,
    }


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Frame sizes a detector reports, as ``(width, height)`` pixels. Every
#: dimension is divisible by 16, so a box coordinate on the cell grid is
#: an exact float and a rescaling by two is exact too.
_FRAME_SIZES = (
    (320, 240),
    (640, 480),
    (1280, 720),
    (1920, 1088),
    (256, 256),
)

#: Boxes live on a grid of ``1/16`` of each frame dimension, so overlap
#: fractions are exact small rationals (``1/2``, ``1/4``, ``3/4``, ...)
#: and land on the generated ``min_overlap`` thresholds often.
_GRID = 16

#: Subject spellings. Mixed case and punctuation, because subjects are
#: selected by Label_Key, not by the label as written.
_SUBJECT_LABELS = ("person", "Person", "PERSON", "worker")

#: Required-class spellings. ``person`` is deliberately among them: a
#: configuration where the subject class is also a required class is one
#: an operator can type, and it makes every subject match itself.
_REQUIRED_LABELS = (
    "hardhat",
    "Hard-Hat",
    "hard hat",
    "safety vest",
    "vest",
    "gloves",
    "person",
)

#: Labels that are neither the subject nor a required class, including two
#: with no addressable Label_Key at all.
_OTHER_LABELS = ("forklift", "pallet", "3M", "-", "")

#: How a generated detection's box relates to a scene anchor. Repetition
#: weights the interesting kinds: a partially shifted box is what makes
#: the overlap threshold bite.
_ENTRY_KINDS = (
    "anchor",
    "inside",
    "shifted",
    "shifted",
    "free",
    "degenerate",
    "no_box",
)

#: How a detection's ``id`` is spelled. ``none`` is a detection the
#: run metadata cannot name, which contributes no ``violating_ids`` entry.
_ID_KINDS = ("text", "text", "text", "number", "none")


def _confidences() -> st.SearchStrategy[Any]:
    """Confidences as a detector reports them, or absent.

    Low values are weighted up, so that a required-class detection whose
    confidence sits *below* ``min_confidence`` — the case that tells the
    subject-only gating of Requirement 14.2 apart from gating everything —
    turns up often.
    """
    return st.one_of(
        st.sampled_from([0.0, 0.0, 0.1, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]),
        st.floats(min_value=0.0, max_value=1.0, width=32),
        st.none(),
    )


def _thresholds() -> st.SearchStrategy[float]:
    """``min_confidence``, within its declared 0 to 1 range."""
    return st.one_of(
        st.sampled_from([0.0, 0.25, 0.5, 0.75, 1.0]),
        st.floats(min_value=0.0, max_value=1.0, width=32),
    )


def _overlaps() -> st.SearchStrategy[float]:
    """``min_overlap``, within its declared 0.05 to 1 range."""
    return st.one_of(
        st.sampled_from([0.05, 0.25, 1.0 / 3.0, 0.5, 0.75, 1.0]),
        st.floats(min_value=0.05, max_value=1.0),
    )


@st.composite
def _cell_boxes(draw: Any, *, min_span: int, max_span: int) -> CellBox:
    """A box on the cell grid, as ``(x0, y0, x1, y1)`` cells.

    ``min_span=0`` allows a degenerate box, which has no area and can
    therefore never meet an overlap threshold.
    """
    span_x = draw(st.integers(min_value=min_span, max_value=max_span))
    span_y = draw(st.integers(min_value=min_span, max_value=max_span))
    x0 = draw(st.integers(min_value=0, max_value=_GRID - span_x))
    y0 = draw(st.integers(min_value=0, max_value=_GRID - span_y))
    return (x0, y0, x0 + span_x, y0 + span_y)


@st.composite
def _derived_cell_boxes(draw: Any, anchor: CellBox, *, shifted: bool) -> CellBox:
    """A sub-box of ``anchor``, optionally shifted partly out of it.

    A shift of ``k`` cells out of a span of ``w`` leaves ``(w - |k|) / w``
    of the box inside the anchor, so the generated overlap fractions cover
    0 to 1 in exact steps — including the values ``min_overlap`` is
    generated from, which is what exercises the inclusive comparison of
    Requirement 14.3.
    """
    ax0, ay0, ax1, ay1 = anchor
    span_x = draw(st.integers(min_value=1, max_value=ax1 - ax0))
    span_y = draw(st.integers(min_value=1, max_value=ay1 - ay0))
    x0 = draw(st.integers(min_value=ax0, max_value=ax1 - span_x))
    y0 = draw(st.integers(min_value=ay0, max_value=ay1 - span_y))
    if shifted:
        x0 += draw(st.integers(min_value=-span_x, max_value=span_x))
        y0 += draw(st.integers(min_value=-span_y, max_value=span_y))
    return (x0, y0, x0 + span_x, y0 + span_y)


def _to_pixels(cell_box: CellBox, frame_size: Tuple[int, int]) -> Bounds:
    """A cell box in the pixel space of ``frame_size``."""
    width, height = frame_size
    x0, y0, x1, y1 = cell_box
    return (
        x0 * width / _GRID,
        y0 * height / _GRID,
        x1 * width / _GRID,
        y1 * height / _GRID,
    )


@st.composite
def _detection_lists(
    draw: Any, frame_size: Tuple[int, int], labels: Sequence[str]
) -> List[Dict[str, Any]]:
    """A Detection_List in the pixel space of ``frame_size``.

    Boxes cluster around one or two scene anchors, so subjects and
    candidate items genuinely overlap; some entries carry no box at all (a
    detector that reported only a label), some are degenerate, and some
    are written with their coordinates inverted.
    """
    anchors = draw(
        st.lists(
            _cell_boxes(min_span=4, max_span=12), min_size=1, max_size=2
        )
    )
    count = draw(st.integers(min_value=0, max_value=7))
    entries: List[Dict[str, Any]] = []
    for index in range(count):
        entry: Dict[str, Any] = {"label": draw(st.sampled_from(list(labels)))}

        id_kind = draw(st.sampled_from(_ID_KINDS))
        if id_kind == "text":
            entry["id"] = "d{0}".format(index)
        elif id_kind == "number":
            entry["id"] = index
        else:
            entry["id"] = None

        entry["confidence"] = draw(_confidences())

        anchor = draw(st.sampled_from(anchors))
        kind = draw(st.sampled_from(_ENTRY_KINDS))
        cell: Optional[CellBox]
        if kind == "anchor":
            cell = anchor
        elif kind == "inside":
            cell = draw(_derived_cell_boxes(anchor, shifted=False))
        elif kind == "shifted":
            cell = draw(_derived_cell_boxes(anchor, shifted=True))
        elif kind == "free":
            cell = draw(_cell_boxes(min_span=1, max_span=6))
        elif kind == "degenerate":
            cell = draw(_cell_boxes(min_span=0, max_span=4))
        else:
            cell = None

        if cell is not None:
            x0, y0, x1, y1 = _to_pixels(cell, frame_size)
            if draw(st.booleans()):
                entry.update(
                    {"x_min": x1, "y_min": y1, "x_max": x0, "y_max": y0}
                )
            else:
                entry.update(
                    {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}
                )
        entries.append(entry)
    return entries


#: Zone coordinates for a rectangular Zone, on a coarse grid.
_ZONE_GRID = st.integers(min_value=1, max_value=19).map(lambda k: k / 20.0)


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
    convex and the half-plane oracle applies. The radii and center keep
    every coordinate inside 0 to 1, so the Zone is well formed.
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


def _zone_parameter(points: Sequence[Point], form: str) -> Any:
    """A Zone in one of the spellings a caller may hand ``associate``."""
    if form == "json":
        return json.dumps([[x, y] for x, y in points])
    if form == "objects":
        return json.dumps([{"x": x, "y": y} for x, y in points])
    if form == "wrapped":
        return json.dumps({"points": [[x, y] for x, y in points]})
    return [(x, y) for x, y in points]


def _required_parameter(labels: Sequence[Any], form: str) -> Any:
    """``required_classes`` as written, or already split.

    The text spelling renders each entry the way a comma-separated
    parameter carries it, so a non-string entry (which an API caller can
    supply in the list spelling) survives the round trip.
    """
    if form == "text":
        return ",".join(
            "" if label is None else label if isinstance(label, str) else str(label)
            for label in labels
        )
    return list(labels)


@st.composite
def _association_cases(
    draw: Any, zones: st.SearchStrategy[Optional[List[Point]]]
) -> Dict[str, Any]:
    """One association invocation: detections, parameters and frame size."""
    frame_size = draw(st.sampled_from(_FRAME_SIZES))
    labels = tuple(_SUBJECT_LABELS) + tuple(_REQUIRED_LABELS) + _OTHER_LABELS
    return {
        "frame_size": frame_size,
        "entries": draw(_detection_lists(frame_size, labels)),
        "subject_class": draw(st.sampled_from(_SUBJECT_LABELS)),
        "required_labels": draw(
            st.lists(st.sampled_from(_REQUIRED_LABELS), min_size=1, max_size=3)
        ),
        "min_overlap": draw(_overlaps()),
        "min_confidence": draw(_thresholds()),
        "zone_points": draw(zones),
        "zone_form": draw(st.sampled_from(["json", "objects", "wrapped", "list"])),
        "required_form": draw(st.sampled_from(["text", "list"])),
    }


def _invoke(case: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    """Call ``associate`` with a case's parameter spellings."""
    zone_points = overrides.get("zone_points", case["zone_points"])
    zone = (
        None
        if zone_points is None
        else _zone_parameter(zone_points, case["zone_form"])
    )
    return associate(
        overrides.get("entries", case["entries"]),
        subject_class=overrides.get("subject_class", case["subject_class"]),
        required_classes=_required_parameter(
            overrides.get("required_labels", case["required_labels"]),
            case["required_form"],
        ),
        min_overlap=overrides.get("min_overlap", case["min_overlap"]),
        min_confidence=overrides.get("min_confidence", case["min_confidence"]),
        zone=zone,
        frame_size=overrides.get("frame_size", case["frame_size"]),
    )


def _expect(case: Dict[str, Any], contains, **overrides: Any) -> Dict[str, Any]:
    """The oracle's answer for a case."""
    return _oracle_associate(
        overrides.get("entries", case["entries"]),
        subject_class=overrides.get("subject_class", case["subject_class"]),
        required_labels=overrides.get("required_labels", case["required_labels"]),
        min_overlap=overrides.get("min_overlap", case["min_overlap"]),
        min_confidence=overrides.get("min_confidence", case["min_confidence"]),
        zone_points=overrides.get("zone_points", case["zone_points"]),
        frame_size=overrides.get("frame_size", case["frame_size"]),
        contains=contains,
    )


def _assert_matches_oracle(
    result: Dict[str, Any], expected: Dict[str, Any]
) -> None:
    """The result equals the oracle, ordering included."""
    assert result["subjects"] == expected["subjects"], (result, expected)
    assert result["compliant"] == expected["compliant"], (result, expected)
    assert result["violations"] == expected["violations"], (result, expected)
    assert result["missing"] == expected["missing"], (result, expected)
    assert list(result["missing"].items()) == list(expected["missing"].items()), (
        result["missing"],
        expected["missing"],
    )
    assert result["violating_ids"] == expected["violating_ids"], (result, expected)
    assert result["outcome"] == expected["outcome"], (result, expected)


def _case_subjects(case: Dict[str, Any], contains) -> List[Dict[str, Any]]:
    """The oracle's subjects for a case."""
    entries, _no_list = _oracle_entries(case["entries"])
    scaled_zone = (
        None
        if case["zone_points"] is None
        else _scale(case["zone_points"], case["frame_size"])
    )
    return _oracle_subjects(
        entries,
        subject_key=_oracle_label_key(case["subject_class"]),
        min_confidence=case["min_confidence"],
        scaled_zone=scaled_zone,
        contains=contains,
    )


def _candidates_of(case: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """The detections of one required class, whatever else they are."""
    entries, _no_list = _oracle_entries(case["entries"])
    return [
        entry for entry in entries if _oracle_label_key(entry.get("label")) == key
    ]


# ---------------------------------------------------------------------------
# Clause-by-clause equality with the reference oracle
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.none()))
def test_association_equals_the_reference_oracle_without_a_zone(
    case: Dict[str, Any],
) -> None:
    """The matching itself, with no geometry in the way.

    Every clause of Property 25 at once — one-to-one use of each required
    detection, the ``min_overlap`` gate, the counts, ``missing`` and
    ``violating_ids`` — against an oracle that formulates the greedy rule
    as an iterated arg-max rather than a sorted walk.
    """
    _assert_matches_oracle(_invoke(case), _expect(case, _rectangle_contains))


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_equals_the_reference_oracle_with_a_rectangular_zone(
    case: Dict[str, Any],
) -> None:
    """Requirement 14.2's Zone filter, judged by interval comparison.

    Both the module's ray casting and the oracle's interval comparison
    count a center *on* the Zone boundary as inside, so no example needs
    to be discarded here.
    """
    _assert_matches_oracle(_invoke(case), _expect(case, _rectangle_contains))


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _convex_zones())))
def test_association_equals_the_reference_oracle_with_a_convex_zone(
    case: Dict[str, Any],
) -> None:
    """Convex Zones, judged by half-planes instead of ray casting.

    An ellipse's vertices are not on the box grid, so an example whose
    decision sits within a hair of the boundary is discarded: the module
    counts a center on the edge as inside within a relative tolerance
    while the oracle compares signs exactly.
    """
    zone_points = case["zone_points"]
    if zone_points is not None:
        width, height = case["frame_size"]
        scaled = _scale(zone_points, (width, height))
        margin = max(width, height) * 1e-6
        for entry in case["entries"]:
            bounds = _oracle_box_bounds(entry)
            if bounds is None:
                continue
            _inside, slack = _convex_contains(_box_center(bounds), scaled)
            assume(slack >= margin)

    _assert_matches_oracle(_invoke(case), _expect(case, _convex_contains_only))


# ---------------------------------------------------------------------------
# Clause: each required detection is used at most once per class
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_uses_each_required_detection_at_most_once_per_class(
    case: Dict[str, Any],
) -> None:
    """Requirement 14.3's one-to-one matching, as a counting bound.

    For each required class, the number of matched subjects can exceed
    neither the number of subjects nor the number of detections of that
    class, and every matched subject must have had at least one pair that
    met the threshold.
    """
    result = _invoke(case)
    subjects = _case_subjects(case, _rectangle_contains)
    subject_bounds = [_oracle_box_bounds(subject) for subject in subjects]
    threshold = case["min_overlap"]

    assert result["subjects"] == len(subjects), (result, len(subjects))
    for key, missing in result["missing"].items():
        matched = result["subjects"] - missing
        candidates = _candidates_of(case, key)
        assert 0 <= missing <= result["subjects"], (key, result)
        assert matched <= len(candidates), (key, matched, len(candidates))

        qualifying_subjects = set()
        qualifying_candidates = set()
        for subject_index, bounds in enumerate(subject_bounds):
            for candidate_index, candidate in enumerate(candidates):
                if (
                    _oracle_overlap(_oracle_box_bounds(candidate), bounds)
                    >= threshold
                ):
                    qualifying_subjects.add(subject_index)
                    qualifying_candidates.add(candidate_index)
        assert matched <= len(qualifying_subjects), (key, result)
        assert matched <= len(qualifying_candidates), (key, result)
        if not qualifying_subjects:
            assert matched == 0, (key, result)
        elif result["outcome"] != OUTCOME_ERROR:
            # Greedy matching always takes at least the best pair.
            assert matched >= 1, (key, result)


@settings(max_examples=100)
@given(
    st.integers(min_value=2, max_value=5),
    st.sampled_from(_FRAME_SIZES),
    st.sampled_from([0.05, 0.25, 0.5, 0.75, 1.0]),
    st.sampled_from(_REQUIRED_LABELS[:3]),
)
def test_association_never_shares_one_required_detection_between_subjects(
    count: int, frame_size: Tuple[int, int], min_overlap: float, item_label: str
) -> None:
    """One hard hat cannot make two people compliant.

    ``count`` identical subject boxes each fully contain the single item,
    so every pair has overlap 1.0 and only the tie-break decides. Exactly
    one subject is compliant, the rest are violations, and the violating
    ids are the later subjects in Detection_List order.
    """
    width, height = frame_size
    subjects = [
        {
            "id": "s{0}".format(index),
            "label": "person",
            "confidence": 1.0,
            "x_min": 0.0,
            "y_min": 0.0,
            "x_max": float(width),
            "y_max": float(height),
        }
        for index in range(count)
    ]
    item = {
        "id": "item",
        "label": item_label,
        "confidence": 1.0,
        "x_min": width / 4.0,
        "y_min": height / 4.0,
        "x_max": width / 2.0,
        "y_max": height / 2.0,
    }
    result = associate(
        subjects + [item],
        subject_class="person",
        required_classes=item_label,
        min_overlap=min_overlap,
        min_confidence=0.0,
    )
    key = _oracle_label_key(item_label)
    assert result["subjects"] == count, result
    assert result["compliant"] == 1, result
    assert result["violations"] == count - 1, result
    assert result["missing"] == {key: count - 1}, result
    assert result["violating_ids"] == [
        "s{0}".format(index) for index in range(1, count)
    ], result
    assert result["outcome"] == OUTCOME_OK, result


# ---------------------------------------------------------------------------
# Clause: only pairs that meet min_overlap are satisfied
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.none()))
def test_association_satisfies_only_pairs_that_meet_min_overlap(
    case: Dict[str, Any],
) -> None:
    """Requirement 14.3's threshold, from both sides.

    A threshold above every achievable overlap leaves no subject matched
    for any class, and a threshold at or below the best pair's overlap
    leaves at least one.
    """
    subjects = _case_subjects(case, _rectangle_contains)
    subject_bounds = [_oracle_box_bounds(subject) for subject in subjects]

    best = 0.0
    for key in _oracle_required_keys(case["required_labels"]):
        for candidate in _candidates_of(case, key):
            candidate_bounds = _oracle_box_bounds(candidate)
            for bounds in subject_bounds:
                best = max(best, _oracle_overlap(candidate_bounds, bounds))

    if best < 1.0:
        # Still inside the declared 0.05 to 1 range for min_overlap.
        above = max(case["min_overlap"], best + (1.0 - best) / 2.0)
        strict = _invoke(case, min_overlap=above)
        assert strict["compliant"] == 0, (above, strict)
        assert strict["violations"] == strict["subjects"], (above, strict)
        for missing in strict["missing"].values():
            assert missing == strict["subjects"], (above, strict)

    if best >= 0.05 and subjects:
        relaxed = _invoke(case, min_overlap=best)
        matched = [
            relaxed["subjects"] - missing
            for missing in relaxed["missing"].values()
        ]
        assert max(matched) >= 1, (best, relaxed)


@settings(max_examples=100)
@given(
    _association_cases(st.none()),
    st.sampled_from([0.05, 0.25, 0.5]),
    st.sampled_from([0.5, 0.75, 1.0]),
)
def test_association_matching_is_monotone_in_min_overlap(
    case: Dict[str, Any], low: float, high: float
) -> None:
    """Raising ``min_overlap`` can only remove matches.

    The greedy walk over the pairs that survive a higher threshold is
    unaffected by the pairs below it, so each class's matched set shrinks
    monotonically: ``missing`` never decreases and ``compliant`` never
    increases, while ``subjects`` is untouched.
    """
    assume(low <= high)
    loose = _invoke(case, min_overlap=low)
    tight = _invoke(case, min_overlap=high)
    assert loose["subjects"] == tight["subjects"], (loose, tight)
    assert tight["compliant"] <= loose["compliant"], (loose, tight)
    assert tight["violations"] >= loose["violations"], (loose, tight)
    for key, missing in loose["missing"].items():
        assert tight["missing"][key] >= missing, (key, loose, tight)


@settings(max_examples=100)
@given(
    st.sampled_from(_FRAME_SIZES),
    st.sampled_from([(1, 4), (2, 4), (3, 4), (1, 2)]),
    st.booleans(),
)
def test_association_prefers_larger_overlaps(
    frame_size: Tuple[int, int], fraction: Tuple[int, int], reversed_order: bool
) -> None:
    """Requirement 14.3 prefers larger overlaps, not earlier subjects.

    Two subjects compete for one hard hat: one contains it wholly, the
    other only ``fraction`` of it. The fully covering subject wins in
    either Detection_List order — the preference is by overlap, and the
    subject order only breaks exact ties (which
    :func:`test_association_never_shares_one_required_detection_between_subjects`
    pins).
    """
    width, height = frame_size
    numerator, denominator = fraction
    item_left = 4.0 * width / _GRID
    item_right = 8.0 * width / _GRID
    item_width = item_right - item_left
    # The partial subject starts inside the item, leaving `fraction` of the
    # item's area on its side.
    partial_left = item_right - item_width * numerator / denominator

    full = {
        "id": "full",
        "label": "person",
        "confidence": 1.0,
        "x_min": 0.0,
        "y_min": 0.0,
        "x_max": float(width),
        "y_max": float(height),
    }
    partial = {
        "id": "partial",
        "label": "person",
        "confidence": 1.0,
        "x_min": partial_left,
        "y_min": 0.0,
        "x_max": float(width),
        "y_max": float(height),
    }
    item = {
        "id": "hat",
        "label": "hardhat",
        "confidence": 1.0,
        "x_min": item_left,
        "y_min": 0.0,
        "x_max": item_right,
        "y_max": height / 2.0,
    }
    subjects = [partial, full] if reversed_order else [full, partial]
    result = associate(
        subjects + [item],
        subject_class="person",
        required_classes="hardhat",
        min_overlap=0.05,
        min_confidence=0.0,
    )
    assert result["subjects"] == 2, result
    assert result["compliant"] == 1, result
    assert result["violating_ids"] == ["partial"], result
    assert result["missing"] == {"hardhat": 1}, result


# ---------------------------------------------------------------------------
# Clauses: the reported counts
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_compliant_plus_violations_equals_subjects(
    case: Dict[str, Any],
) -> None:
    """``compliant + violations == subjects``, always, with every count a
    non-negative integer (Requirement 14.4)."""
    result = _invoke(case)
    for key in ("subjects", "compliant", "violations"):
        assert isinstance(result[key], int) and not isinstance(result[key], bool)
        assert result[key] >= 0, result
    assert result["compliant"] + result["violations"] == result["subjects"], result


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_missing_counts_the_subjects_without_a_class_match(
    case: Dict[str, Any],
) -> None:
    """``missing[c]`` is the number of subjects with no class-c match
    (Requirement 14.4), and it brackets ``violations``.

    A subject is a violation exactly when it lacks *some* class, so
    ``violations`` is at least the worst single class's ``missing`` and at
    most their sum (capped by the subject count).
    """
    result = _invoke(case)
    expected_keys = _oracle_required_keys(case["required_labels"])
    assert list(result["missing"]) == expected_keys, (result, expected_keys)
    for key, missing in result["missing"].items():
        assert isinstance(missing, int) and not isinstance(missing, bool)
        assert 0 <= missing <= result["subjects"], (key, result)

    values = list(result["missing"].values())
    assert result["violations"] >= max(values, default=0), result
    assert result["violations"] <= min(sum(values), result["subjects"]), result
    if all(value == 0 for value in values):
        assert result["violations"] == 0, result


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_lists_exactly_the_ids_of_the_non_compliant_subjects(
    case: Dict[str, Any],
) -> None:
    """``violating_ids`` names the non-compliant subjects, in order.

    Compared against the oracle's own verdict per subject, so the list is
    checked to be exactly the violators and not merely the right length.
    Subjects whose detection carries no ``id`` contribute no element,
    which is why the list can be shorter than ``violations``.
    """
    result = _invoke(case)
    expected = _expect(case, _rectangle_contains)
    assert result["violating_ids"] == expected["violating_ids"], (result, expected)
    assert all(isinstance(value, str) for value in result["violating_ids"]), result
    assert len(result["violating_ids"]) <= result["violations"], result

    subjects = _case_subjects(case, _rectangle_contains)
    named = [
        subject.get("id")
        for subject in subjects
        if subject.get("id") is not None
    ]
    compliant_ids = [
        value
        for value in (str(identifier) for identifier in named)
        if value not in result["violating_ids"]
    ]
    # Nothing outside the subject set is ever reported.
    for value in result["violating_ids"]:
        assert value in [str(identifier) for identifier in named], (value, named)
    assert len(compliant_ids) + len(result["violating_ids"]) == len(named), result


# ---------------------------------------------------------------------------
# Clause: determinism
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_is_deterministic_for_identical_input(
    case: Dict[str, Any],
) -> None:
    """Identical input gives byte-identical metadata.

    Serialized without sorting keys, so the *order* of ``missing`` and of
    ``violating_ids`` is part of the comparison: the device and the
    sandbox must produce the same document (Requirement 14.6).
    """
    first = _invoke(case)
    second = _invoke(case)
    assert json.dumps(run_metadata(first), default=str) == json.dumps(
        run_metadata(second), default=str
    ), (first, second)
    assert first == second, (first, second)


# ---------------------------------------------------------------------------
# Requirement 14.2: what makes a detection a subject
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_association_cases(st.one_of(st.none(), _rectangular_zones())))
def test_association_takes_as_subjects_exactly_the_qualifying_detections(
    case: Dict[str, Any],
) -> None:
    """Requirement 14.2: the subject Label_Key, ``min_confidence`` and the
    Zone select the subjects, and nothing else does.

    ``subjects`` also never grows when the confidence threshold is
    raised, and never exceeds the number of detections carrying the
    subject's Label_Key.
    """
    result = _invoke(case)
    subjects = _case_subjects(case, _rectangle_contains)
    assert result["subjects"] == len(subjects), (result, len(subjects))

    subject_key = _oracle_label_key(case["subject_class"])
    labelled = sum(
        1
        for entry in _oracle_entries(case["entries"])[0]
        if _oracle_label_key(entry.get("label")) == subject_key
    )
    assert result["subjects"] <= labelled, (result, labelled)

    stricter = _invoke(case, min_confidence=1.0)
    assert stricter["subjects"] <= result["subjects"], (result, stricter)


def test_association_does_not_gate_required_detections_by_confidence_or_zone() -> None:
    """The documented reading of Requirements 14.2 and 14.3.

    ``min_confidence`` and the Zone gate the *subjects*; a required-class
    detection is eligible on its label and the overlap alone. A hard hat
    with no confidence at all, whose center sits outside the Zone, still
    makes the person inside the Zone compliant.
    """
    width, height = 1600.0, 900.0
    zone = [[0.0, 0.0], [0.4, 0.0], [0.4, 1.0], [0.0, 1.0]]
    person = {
        "id": "p1",
        "label": "person",
        "confidence": 1.0,
        "x_min": 0.0,
        "y_min": 0.0,
        "x_max": 0.6 * width,
        "y_max": height,
    }
    hardhat = {
        "id": "h1",
        "label": "hardhat",
        "confidence": 0.0,
        "x_min": 0.45 * width,
        "y_min": 0.0,
        "x_max": 0.55 * width,
        "y_max": 0.2 * height,
    }
    result = associate(
        [person, hardhat],
        subject_class="person",
        required_classes="hardhat",
        min_overlap=DEFAULT_MIN_OVERLAP,
        min_confidence=0.5,
        zone=json.dumps(zone),
        frame_size=(width, height),
    )
    assert result["subjects"] == 1, result
    assert result["compliant"] == 1, result
    assert result["violations"] == 0, result
    assert result["missing"] == {"hardhat": 0}, result
    assert result["violating_ids"] == [], result
    assert result["outcome"] == OUTCOME_OK, result


# ---------------------------------------------------------------------------
# Requirement 14.6: the outcomes of Requirements 13.5 and 13.6
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    _association_cases(st.none()),
    st.sampled_from([None, {}, "detections", 7, True]),
)
def test_association_warns_when_the_run_has_no_detection_list(
    case: Dict[str, Any], detections: Any
) -> None:
    """Requirement 13.5 through 14.6: no Detection_List is a warning with
    an empty result, not a failure and not an error."""
    result = _invoke(case, entries=detections)
    assert result["outcome"] == OUTCOME_WARNING, result
    assert result["problems"], result
    assert result["subjects"] == 0, result
    assert result["compliant"] == 0, result
    assert result["violations"] == 0, result
    assert result["violating_ids"] == [], result
    assert set(result["missing"].values()) <= {0}, result
    assert list(result["missing"]) == _oracle_required_keys(
        case["required_labels"]
    ), result


@settings(max_examples=100)
@given(
    _association_cases(_rectangular_zones()),
    st.sampled_from(["no_frame", "not_json", "too_few", "out_of_range"]),
)
def test_association_reports_the_outcome_of_an_unusable_zone(
    case: Dict[str, Any], flavor: str
) -> None:
    """Requirement 13.6 through 14.6: a configured Zone that cannot be
    applied is an ``error`` with an empty result, which gates the node's
    downstream nodes without failing the run."""
    if flavor == "no_frame":
        result = _invoke(case, frame_size=None)
    else:
        zone = {
            "not_json": "{not json",
            "too_few": json.dumps([[0.1, 0.1], [0.9, 0.9]]),
            "out_of_range": json.dumps([[0.1, 0.1], [0.9, 0.9], [1.5, 0.5]]),
        }[flavor]
        result = associate(
            case["entries"],
            subject_class=case["subject_class"],
            required_classes=_required_parameter(
                case["required_labels"], case["required_form"]
            ),
            min_overlap=case["min_overlap"],
            min_confidence=case["min_confidence"],
            zone=zone,
            frame_size=case["frame_size"],
        )
    assert result["outcome"] == OUTCOME_ERROR, result
    assert result["problems"], result
    assert result["subjects"] == 0, result
    assert result["compliant"] == 0, result
    assert result["violations"] == 0, result
    assert result["violating_ids"] == [], result
    assert set(result["missing"].values()) <= {0}, result


@settings(max_examples=100)
@given(
    _association_cases(st.none()),
    st.sampled_from([None, "", "   ", "-", ",", [], ["", " "]]),
    st.booleans(),
)
def test_association_errors_on_a_missing_subject_or_required_class(
    case: Dict[str, Any], empty: Any, drop_subject: bool
) -> None:
    """A missing ``subject_class`` or ``required_classes`` is an error.

    Both parameters are required (Requirement 14.1), and a value with no
    addressable Label_Key in it is the same as absent. Reporting an error
    is deliberate: the alternative would be a vacuous "everything is
    compliant".
    """
    overrides: Dict[str, Any] = {}
    if drop_subject:
        overrides["subject_class"] = empty
    else:
        overrides["required_labels"] = empty if isinstance(empty, list) else [empty]
    result = _invoke(case, **overrides)
    assert result["outcome"] == OUTCOME_ERROR, result
    assert result["problems"], result
    assert result["subjects"] == 0, result
    assert result["compliant"] == 0, result
    assert result["violations"] == 0, result
    assert result["violating_ids"] == [], result
    assert set(result["missing"].values()) <= {0}, result


# ---------------------------------------------------------------------------
# Totality, the merged metadata shape, and reading numeric text
# ---------------------------------------------------------------------------


def _junk() -> st.SearchStrategy[Any]:
    """Whatever a caller may actually hand a parameter."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-4, max_value=4),
        st.floats(allow_nan=True, allow_infinity=True, width=32),
        st.text(max_size=6),
        st.lists(st.integers(min_value=-2, max_value=2), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    )


@settings(max_examples=100)
@given(_junk(), _junk(), _junk(), _junk(), _junk(), _junk(), _junk())
def test_association_is_total_and_projects_the_specified_metadata(
    detections: Any,
    subject_class: Any,
    required_classes: Any,
    min_overlap: Any,
    min_confidence: Any,
    zone: Any,
    frame_size: Any,
) -> None:
    """Nothing raises, and the merged metadata is exactly the specified
    shape.

    ``associate`` runs inside a workflow run that must not fail on a
    configuration mistake, so every parameter accepts anything and
    degrades to a reported problem. ``run_metadata`` projects out the
    diagnostics, leaving precisely the keys of Requirement 14.4, in order.
    """
    result = associate(
        detections,
        subject_class=subject_class,
        required_classes=required_classes,
        min_overlap=min_overlap,
        min_confidence=min_confidence,
        zone=zone,
        frame_size=frame_size,
    )
    assert isinstance(result, dict)
    assert result["outcome"] in (OUTCOME_OK, OUTCOME_WARNING, OUTCOME_ERROR)
    assert isinstance(result["problems"], list)
    assert all(isinstance(problem, str) for problem in result["problems"])
    assert (result["outcome"] == OUTCOME_OK) == (not result["problems"]), result

    merged = run_metadata(result)
    assert tuple(merged) == ASSOCIATION_METADATA_KEYS, merged
    assert merged["compliant"] + merged["violations"] == merged["subjects"], merged
    assert isinstance(merged["missing"], dict)
    assert all(isinstance(value, int) for value in merged["missing"].values())
    assert isinstance(merged["violating_ids"], list)
    assert all(isinstance(value, str) for value in merged["violating_ids"])


@settings(max_examples=100)
@given(_association_cases(st.none()))
def test_association_reads_numeric_text_like_numbers(case: Dict[str, Any]) -> None:
    """A threshold or a confidence written as text is read as its number.

    Node parameters arrive as strings often enough that the module parses
    them; pinning it here keeps the oracle free of the conversion.
    """
    as_text = _invoke(
        case,
        min_overlap="{0!r}".format(float(case["min_overlap"])),
        min_confidence="  {0!r}  ".format(float(case["min_confidence"])),
    )
    assert as_text == _invoke(case), (as_text, case)

    textual = [
        dict(entry, confidence="{0!r}".format(_oracle_confidence(entry)))
        for entry in case["entries"]
    ]
    numeric = [
        dict(entry, confidence=_oracle_confidence(entry))
        for entry in case["entries"]
    ]
    assert _invoke(case, entries=textual) == _invoke(case, entries=numeric)


# ---------------------------------------------------------------------------
# Scale invariance: the overlap rule is a ratio, the Zone is normalized
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    _association_cases(st.one_of(st.none(), _rectangular_zones())),
    st.sampled_from([2, 4]),
)
def test_association_is_invariant_under_a_power_of_two_rescaling(
    case: Dict[str, Any], factor: int
) -> None:
    """Rescaling the frame and the boxes together changes nothing.

    The overlap rule is a ratio of areas and a Zone is stored normalized,
    so a detector reporting the same scene at twice the resolution must
    produce identical metadata. A power of two keeps every float exact,
    so this is an equality and not an approximation.
    """
    width, height = case["frame_size"]
    scaled_entries = []
    for entry in case["entries"]:
        scaled = dict(entry)
        for key in ("x_min", "y_min", "x_max", "y_max"):
            if key in scaled:
                scaled[key] = scaled[key] * factor
        scaled_entries.append(scaled)

    result = _invoke(case)
    rescaled = _invoke(
        case,
        entries=scaled_entries,
        frame_size=(width * factor, height * factor),
    )
    assert rescaled == result, (result, rescaled)
