"""Layout check: is every configured piece where it belongs?

One pure module (standard library only, no I/O and no logging) holding
every rule of the Layout Check node (``layout_check``), so that the
LocalServer executor binding (``workflow_engine/scene_analytics.py``), the
Portal's cloud test sandbox harness and the workflow validator (V14) all
agree (car-assembly-check Requirements 4.1, 5.2, 5.3). It keeps the
contract of :mod:`workflow_core.analytics.scene`, whose private geometry
and number helpers it reuses: nothing here raises, the parser returns
``(value, problems)``, :func:`check_layout` returns its run metadata plus
the diagnostic ``outcome`` and ``problems``, and :func:`run_metadata`
projects the diagnostics out.

Vocabulary:

- **Expected_Region**: where one class's piece lies when it is placed
  correctly, a simple polygon of 3 to 64 points in **normalized** frame
  coordinates (0.0 to 1.0). The node's ``expected_regions`` parameter maps
  each class label it checks to one (:func:`parse_expected_regions`).
- **Canonical_RLE**: an instance mask (``mask_rle``) as space-separated
  run lengths over the column-major flattening of the frame (pixel
  ``(x, y)`` has the index ``x * height + y``), background run first,
  summing to ``width * height``.
- **Interval list**: a pixel set as a sorted list of disjoint half-open
  ``(start, end)`` ranges of that index space. Masks, regions and the car
  outline are all compared as interval lists; no pixel array is built.
- **Position_Score**: the intersection over union of a piece's mask and
  its Expected_Region (:func:`position_score`).
- **Car_Outline**: the union of every Expected_Region
  (:func:`outline_intervals`). A detection's Outline_Overlap is the
  fraction of its mask inside the Car_Outline (:func:`outline_overlap`);
  a detection below ``min_outline_overlap`` lies outside the car and is
  ignored (design DD23).

Pixel semantics (design DD3; one rule, the same half-open convention on
both axes): pixel ``(x, y)`` lies in a region iff the number of the
region's edges with ``min_x <= x + 0.5 < max_x`` whose crossing with the
vertical line ``X = x + 0.5`` satisfies ``y_cross <= y + 0.5`` is odd. The
vertices are scaled to pixels (``x * width``, ``y * height``) and each edge
is read from its endpoint of smaller x, ``(x0, y0)``, to the other,
``(x1, y1)``, so its crossing is ``y0 + (X - x0) * (y1 - y0) / (x1 - x0)``.
A vertical edge never crosses a vertical line; a horizontal edge crosses it
at its own y. The column sweep of :func:`_region_intervals` implements
exactly this rule and emits each column's pixels as maximal runs, so a
brute-force count per pixel centre reproduces its interval list.

Cost: the sweep is ``O(width x active edges + E log E)`` per region (two
to four active edges per column for a near-convex polygon) and every
interval operation is a linear merge, so a 4608 x 3288 frame costs a few
tens of milliseconds per piece in CPython.
"""

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import scene

__all__ = [
    "STATUS_OK",
    "STATUS_MISPLACED",
    "STATUS_MISSING",
    "STATUS_DUPLICATE",
    "DEFAULT_MIN_IOU",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_OUTLINE_OVERLAP",
    "MAX_MIN_OUTLINE_OVERLAP",
    "MIN_VERTICES",
    "MAX_VERTICES",
    "MAX_REGIONS",
    "MIN_REGION_AREA",
    "LAYOUT_METADATA_KEYS",
    "parse_expected_regions",
    "rle_intervals",
    "interval_iou",
    "interval_union",
    "interval_intersection_length",
    "outline_intervals",
    "outline_overlap",
    "position_score",
    "attach_detection_ids",
    "check_layout",
    "run_metadata",
]

# ---------------------------------------------------------------------------
# Constants (car-assembly-check Requirements 4.1, 5.1-5.3; design DD23)
# ---------------------------------------------------------------------------

#: The status of each configured class, ``layout.<nodeId>.pieces.<label>``.
STATUS_OK = "ok"
STATUS_MISPLACED = "misplaced"
STATUS_MISSING = "missing"
STATUS_DUPLICATE = "duplicate"

#: Defaults of the node's ``min_iou`` and ``min_confidence`` parameters.
DEFAULT_MIN_IOU = 0.7
DEFAULT_MIN_CONFIDENCE = 0.5

#: Default and upper bound of ``min_outline_overlap`` (owner decision
#: O19). 0 counts every detection; the bound keeps a misconfiguration from
#: ignoring a piece that lies mostly inside the car.
DEFAULT_MIN_OUTLINE_OVERLAP = 0.1
MAX_MIN_OUTLINE_OVERLAP = 0.5

#: An Expected_Region has 3 to 64 distinct points and covers at least
#: 0.5 % of the frame; ``expected_regions`` names 1 to 10 classes.
MIN_VERTICES = 3
MAX_VERTICES = 64
MAX_REGIONS = 10
MIN_REGION_AREA = 0.005

#: The keys of :func:`check_layout` that are merged into the run metadata
#: under ``layout.<nodeId>``, in this order.
LAYOUT_METADATA_KEYS = (
    "pass",
    "pieces",
    "flagged",
    "ignored",
    "unexpected_classes",
    "expected_regions",
    "frame",
    "min_iou",
    "min_confidence",
    "min_outline_overlap",
)

#: Most characters a class label may have once surrounding whitespace is
#: removed.
_MAX_LABEL_LENGTH = 64

#: Decimals of every reported IoU and Outline_Overlap. The thresholds are
#: compared with the reported values, so a record never shows a number
#: that contradicts its status.
_DECIMALS = 4

#: A frame side above this many pixels reads as an unknown frame, which
#: bounds every sweep and every decode.
_MAX_FRAME_SIDE = 1 << 16

#: A run length with more significant digits than this exceeds any frame;
#: such a token is refused before it is converted.
_MAX_RUN_DIGITS = 12

#: Two ring edges whose bounding boxes are farther apart than this (in
#: normalized coordinates) cannot meet, and the touching tolerance of
#: ``scene._segments_intersect`` (below 1e-8 for points in 0 to 1) cannot
#: bridge the gap, so such a pair is not handed to the predicate.
_BOX_MARGIN = 1e-6

# Problem texts of :func:`check_layout`, worded for the node status.
_PROBLEM_REGIONS_UNUSABLE = "expected_regions is not usable"
_PROBLEM_NO_DETECTIONS = "no detections to check"
_PROBLEM_FRAME_UNKNOWN = "frame size unknown"


# ---------------------------------------------------------------------------
# Expected regions (Requirements 4.1, 5.6)
# ---------------------------------------------------------------------------


def parse_expected_regions(
    text: Any,
) -> Tuple[Optional[Dict[str, List[Tuple[float, float]]]], List[str]]:
    """Parse the ``expected_regions`` parameter into normalized polygons.

    ``text`` is the parameter as written (a JSON string) or an already
    parsed mapping. Returns ``(regions, [])`` for a well-formed value,
    ``{label: [(x, y), ...]}`` in the value's key order with each label
    stripped of surrounding whitespace and each ring as float pairs
    without its duplicate vertices, and ``(None, problems)`` otherwise,
    one sentence per broken rule naming the region it concerns:

    - the value is a JSON object naming 1 to :data:`MAX_REGIONS` classes;
    - each key is a string of 1 to 64 characters once stripped, and no two
      keys strip to the same label;
    - each value is a list of ``[x, y]`` pairs of finite numbers in 0 to 1;
    - consecutive duplicate vertices are dropped cyclically (the ring is
      read as closed, so a last vertex equal to the first, the GeoJSON
      habit, is dropped too) and :data:`MIN_VERTICES` to
      :data:`MAX_VERTICES` vertices remain;
    - the ring's shoelace area has a magnitude of at least
      :data:`MIN_REGION_AREA`;
    - the ring is simple: no two non-adjacent edges intersect, the last
      edge counting as adjacent to the first.

    A blank or missing value yields ``(None, [problem])``. The validator
    leaves that case to V4 and reports nothing for it.
    """
    if text is None or (isinstance(text, str) and text.strip() == ""):
        return None, ["No expected regions are set."]
    raw: Any = text
    if isinstance(text, str):
        try:
            raw = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            return None, ["Expected regions are not valid JSON."]

    if not isinstance(raw, dict):
        return None, [
            "Expected regions must be a JSON object that maps each class "
            "label to a polygon of [x, y] points.",
        ]
    count = len(raw)
    if count < 1 or count > MAX_REGIONS:
        return None, [
            "Expected regions must name between 1 and {0} classes; this value "
            "names {1}.".format(MAX_REGIONS, count),
        ]

    problems: List[str] = []
    regions: Dict[str, List[Tuple[float, float]]] = {}
    seen = set()
    for position, (key, polygon) in enumerate(raw.items(), start=1):
        label, label_problem = _region_label(key, position, seen)
        if label is None:
            problems.append(label_problem)
            continue
        ring, ring_problems = _region_ring(label, polygon)
        if ring is None:
            problems.extend(ring_problems)
            continue
        regions[label] = ring
    if problems:
        return None, problems
    return regions, []


def _region_label(key: Any, position: int, seen: set) -> Tuple[Optional[str], str]:
    """A region's stripped label, or ``None`` and the problem with it."""
    if not isinstance(key, str):
        return None, "Region {0}: the class label must be a string.".format(position)
    label = key.strip()
    if label == "":
        return None, "Region {0}: the class label is empty.".format(position)
    if len(label) > _MAX_LABEL_LENGTH:
        return None, (
            "Region {0}: the class label is longer than {1} "
            "characters.".format(position, _MAX_LABEL_LENGTH)
        )
    if label in seen:
        return None, (
            "Region {0}: the class label '{1}' is already used by an earlier "
            "region.".format(position, label)
        )
    seen.add(label)
    return label, ""


def _region_ring(
    label: str, polygon: Any
) -> Tuple[Optional[List[Tuple[float, float]]], List[str]]:
    """One region's ring without duplicate vertices, or its problems."""
    name = "Region '{0}'".format(label)
    if not isinstance(polygon, (list, tuple)):
        return None, ["{0}: the polygon must be a list of [x, y] points.".format(name)]

    problems: List[str] = []
    points: List[Tuple[float, float]] = []
    for position, entry in enumerate(polygon, start=1):
        point = _pair(entry)
        if point is None:
            problems.append(
                "{0}: point {1} must be a pair of numbers [x, y].".format(name, position)
            )
            continue
        x, y = point
        if not (0.0 <= x <= 1.0):
            problems.append(
                "{0}: point {1} x coordinate must be between 0 and 1.".format(
                    name, position
                )
            )
            continue
        if not (0.0 <= y <= 1.0):
            problems.append(
                "{0}: point {1} y coordinate must be between 0 and 1.".format(
                    name, position
                )
            )
            continue
        points.append(point)
    if problems:
        return None, problems

    ring = _distinct_ring(points)
    if not (MIN_VERTICES <= len(ring) <= MAX_VERTICES):
        return None, [
            "{0}: the polygon must have between {1} and {2} distinct points; "
            "this one has {3}.".format(name, MIN_VERTICES, MAX_VERTICES, len(ring)),
        ]
    if abs(_ring_area(ring)) < MIN_REGION_AREA:
        problems.append(
            "{0}: the polygon must cover at least 0.5% of the frame.".format(name)
        )
    if not _ring_is_simple(ring):
        problems.append(
            "{0}: the polygon crosses itself; no two of its edges may "
            "intersect.".format(name)
        )
    if problems:
        return None, problems
    return ring, []


def _pair(entry: Any) -> Optional[Tuple[float, float]]:
    """``entry`` as an ``(x, y)`` float pair, or ``None`` when it is not a
    list of two finite numbers."""
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        return None
    x, y = entry[0], entry[1]
    if not scene._is_number(x) or not scene._is_number(y):
        return None
    return float(x), float(y)


def _distinct_ring(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """``points`` without consecutive duplicates, read as a closed ring."""
    ring: List[Tuple[float, float]] = []
    for point in points:
        if not ring or ring[-1] != point:
            ring.append(point)
    while len(ring) > 1 and ring[-1] == ring[0]:
        ring.pop()
    return ring


def _ring_area(ring: Sequence[Tuple[float, float]]) -> float:
    """The signed shoelace area of the closed ring."""
    total = 0.0
    count = len(ring)
    for index in range(count):
        x0, y0 = ring[index]
        x1, y1 = ring[(index + 1) % count]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _ring_is_simple(ring: Sequence[Tuple[float, float]]) -> bool:
    """Whether no two non-adjacent edges of the closed ring intersect.

    Each pair goes through ``scene._segments_intersect`` unless the two
    edges' bounding boxes are more than :data:`_BOX_MARGIN` apart; the
    shortcut keeps a 64-point ring's check to a few milliseconds, which
    matters because the device parses the parameter on every run. The
    Workflow Builder's port applies the same shortcut.
    """
    count = len(ring)
    edges = []
    for index in range(count):
        start = ring[index]
        end = ring[(index + 1) % count]
        edges.append((
            start,
            end,
            min(start[0], end[0]),
            max(start[0], end[0]),
            min(start[1], end[1]),
            max(start[1], end[1]),
        ))
    for first in range(count):
        start_a, end_a, left_a, right_a, top_a, bottom_a = edges[first]
        for second in range(first + 2, count):
            if first == 0 and second == count - 1:
                continue  # the closing edge is adjacent to the first one
            start_b, end_b, left_b, right_b, top_b, bottom_b = edges[second]
            if (
                left_b > right_a + _BOX_MARGIN
                or left_a > right_b + _BOX_MARGIN
                or top_b > bottom_a + _BOX_MARGIN
                or top_a > bottom_b + _BOX_MARGIN
            ):
                continue
            if scene._segments_intersect(start_a, end_a, start_b, end_b):
                return False
    return True


# ---------------------------------------------------------------------------
# Intervals (design DD3)
# ---------------------------------------------------------------------------


def _region_intervals(polygon: Any, width: Any, height: Any) -> List[Tuple[int, int]]:
    """The pixels of ``polygon`` as a column-major interval list.

    The column sweep of design §1: one vertical sweep line per column at
    ``cx = x + 0.5``; an edge is active iff ``min_x <= cx < max_x``
    (zero-width edges are skipped, horizontal ones kept); the sorted
    crossings pair up as ``(y_a, y_b)`` and cover the rows whose centres
    satisfy ``y_a <= r + 0.5 < y_b``, clipped to the frame. Touching runs
    of one column are merged, so the list is the maximal-run form of the
    pixel set of the module docstring's rule. An unusable polygon or
    frame yields ``[]``.
    """
    vertices = _vertices(polygon)
    columns = _dimension(width)
    rows = _dimension(height)
    if vertices is None or len(vertices) < MIN_VERTICES or columns is None or rows is None:
        return []
    scaled = [(x * columns, y * rows) for x, y in vertices]

    # (x0, y0, x1, y1 - y0, x1 - x0) with x0 < x1: the endpoint of smaller
    # x first, so the crossing does not depend on the ring's direction.
    edges = []
    count = len(scaled)
    for index in range(count):
        xa, ya = scaled[index]
        xb, yb = scaled[(index + 1) % count]
        if xa == xb:
            continue
        if xa > xb:
            xa, ya, xb, yb = xb, yb, xa, ya
        edges.append((xa, ya, xb, yb - ya, xb - xa))
    if not edges:
        return []
    edges.sort(key=lambda edge: edge[0])

    intervals: List[Tuple[int, int]] = []
    last_row = rows - 1
    ceil = math.ceil
    edge_count = len(edges)
    pending = 0
    active: List[Tuple[float, float, float, float, float]] = []
    expiry = math.inf
    # Columns outside [first, stop) have no active edge.
    first = max(0, math.floor(edges[0][0]) - 1)
    stop = min(columns, math.ceil(max(edge[2] for edge in edges)) + 1)
    for x in range(first, stop):
        cx = x + 0.5
        entered = False
        while pending < edge_count and edges[pending][0] <= cx:
            active.append(edges[pending])
            pending += 1
            entered = True
        if entered or cx >= expiry:
            active = [edge for edge in active if cx < edge[2]]
            expiry = min((edge[2] for edge in active), default=math.inf)
        if not active:
            continue
        crossings = sorted(
            [y0 + (cx - x0) * dy / dx for x0, y0, _x1, dy, dx in active]
        )
        base = x * rows
        for index in range(0, len(crossings) - 1, 2):
            row_start = ceil(crossings[index] - 0.5)
            row_end = ceil(crossings[index + 1] - 0.5) - 1
            if row_start < 0:
                row_start = 0
            if row_end > last_row:
                row_end = last_row
            if row_end < row_start:
                continue
            start = base + row_start
            end = base + row_end + 1
            if intervals and intervals[-1][1] == start and intervals[-1][0] >= base:
                intervals[-1] = (intervals[-1][0], end)
            else:
                intervals.append((start, end))
    return intervals


def rle_intervals(
    rle: Any, width: int, height: int
) -> Tuple[Optional[List[Tuple[int, int]]], Optional[str]]:
    """A Canonical_RLE mask as a column-major interval list.

    The decoding rules of ``dda_manifest.rle_decode`` and the device
    ``mask_rle.decode``, without materializing the mask: every token is a
    run length of ASCII digits, the runs alternate background and
    foreground starting with background, and they sum to
    ``width * height``. Returns ``(intervals, None)``, or ``(None,
    problem)`` naming the broken rule.
    """
    columns = _dimension(width)
    rows = _dimension(height)
    if columns is None or rows is None:
        return None, "the frame size is not usable"
    if not isinstance(rle, str):
        return None, "mask_rle is not a string of run lengths"

    total = columns * rows
    intervals: List[Tuple[int, int]] = []
    position = 0
    foreground = False
    for token in rle.split():
        if not (token.isascii() and token.isdigit()):
            return None, "mask_rle has a token that is not a run length: {0!r}".format(
                token[:16]
            )
        if len(token.lstrip("0")) > _MAX_RUN_DIGITS:
            return None, "mask_rle has a run longer than the frame"
        run = int(token)
        if foreground and run:
            end = position + run
            if intervals and intervals[-1][1] == position:
                intervals[-1] = (intervals[-1][0], end)
            else:
                intervals.append((position, end))
        position += run
        if position > total:
            return None, "mask_rle covers more than the {0} pixels of the {1} x {2} frame".format(
                total, columns, rows
            )
        foreground = not foreground
    if position != total:
        return None, "mask_rle covers {0} pixels, not the {1} of the {2} x {3} frame".format(
            position, total, columns, rows
        )
    return intervals, None


def interval_iou(a: Sequence[Tuple[int, int]], b: Sequence[Tuple[int, int]]) -> float:
    """The IoU of two interval lists; ``0.0`` when both are empty.

    Inputs are read as pixel sets: malformed items are dropped and
    unsorted or overlapping intervals are merged first.
    """
    return _iou(_normalized(a), _normalized(b))


def interval_union(lists: Sequence[Sequence[Tuple[int, int]]]) -> List[Tuple[int, int]]:
    """The union of interval lists: sorted, disjoint, touching intervals
    merged."""
    if not isinstance(lists, (list, tuple)):
        return []
    flat: List[Tuple[int, int]] = []
    for intervals in lists:
        flat.extend(_clean(intervals))
    return _merge(flat)


def interval_intersection_length(
    a: Sequence[Tuple[int, int]], b: Sequence[Tuple[int, int]]
) -> int:
    """How many pixels two interval lists share."""
    return _overlap_length(_normalized(a), _normalized(b))


def outline_intervals(
    regions: Dict[str, Sequence[Tuple[float, float]]], width: int, height: int
) -> List[Tuple[int, int]]:
    """The Car_Outline: the union of every region's pixels, so overlapping
    and touching regions count once. Unusable input yields ``[]``."""
    columns = _dimension(width)
    rows = _dimension(height)
    if columns is None or rows is None or not isinstance(regions, dict):
        return []
    flat: List[Tuple[int, int]] = []
    for polygon in regions.values():
        flat.extend(_region_intervals(polygon, columns, rows))
    return _merge(flat)


def outline_overlap(
    mask: Sequence[Tuple[int, int]], outline: Sequence[Tuple[int, int]]
) -> Optional[float]:
    """The fraction of ``mask`` inside ``outline``; ``None`` for an empty
    mask."""
    return _overlap_fraction(_normalized(mask), _normalized(outline))


def position_score(
    rle: Any, width: int, height: int, polygon: Sequence[Tuple[float, float]]
) -> Tuple[Optional[float], Optional[str]]:
    """The Position_Score of one mask against one normalized polygon.

    Returns ``(iou, None)``, or ``(None, problem)`` when the frame size,
    the polygon or the mask is unusable. The IoU is not rounded.
    """
    columns = _dimension(width)
    rows = _dimension(height)
    if columns is None or rows is None:
        return None, _PROBLEM_FRAME_UNKNOWN
    vertices = _vertices(polygon)
    if vertices is None or len(vertices) < MIN_VERTICES:
        return None, "the polygon is not a list of at least 3 [x, y] points"
    mask, problem = rle_intervals(rle, columns, rows)
    if mask is None:
        return None, problem
    return _iou(mask, _region_intervals(vertices, columns, rows)), None


def _vertices(polygon: Any) -> Optional[List[Tuple[float, float]]]:
    """``polygon`` as float pairs, or ``None`` when any point is not a pair
    of finite numbers."""
    if not isinstance(polygon, (list, tuple)):
        return None
    vertices: List[Tuple[float, float]] = []
    for entry in polygon:
        point = _pair(entry)
        if point is None:
            return None
        vertices.append(point)
    return vertices


def _dimension(value: Any) -> Optional[int]:
    """A frame side as a positive int, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        side = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        side = int(value)
    else:
        return None
    return side if 0 < side <= _MAX_FRAME_SIDE else None


def _clean(intervals: Any) -> List[Tuple[int, int]]:
    """The well-formed, non-empty ``(start, end)`` int pairs of
    ``intervals``."""
    if not isinstance(intervals, (list, tuple)):
        return []
    cleaned: List[Tuple[int, int]] = []
    for item in intervals:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = item[0], item[1]
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
            and start < end
        ):
            cleaned.append((start, end))
    return cleaned


def _merge(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Intervals in any order as a sorted list of disjoint intervals,
    touching and overlapping ones merged."""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _normalized(intervals: Any) -> List[Tuple[int, int]]:
    return _merge(_clean(intervals))


def _length(intervals: Sequence[Tuple[int, int]]) -> int:
    return sum(end - start for start, end in intervals)


def _overlap_length(a: Sequence[Tuple[int, int]], b: Sequence[Tuple[int, int]]) -> int:
    """The shared length of two sorted, disjoint interval lists (a
    two-pointer merge)."""
    total = 0
    first = second = 0
    while first < len(a) and second < len(b):
        start_a, end_a = a[first]
        start_b, end_b = b[second]
        low = start_a if start_a > start_b else start_b
        high = end_a if end_a < end_b else end_b
        if high > low:
            total += high - low
        if end_a < end_b:
            first += 1
        else:
            second += 1
    return total


def _iou(a: Sequence[Tuple[int, int]], b: Sequence[Tuple[int, int]]) -> float:
    overlap = _overlap_length(a, b)
    union = _length(a) + _length(b) - overlap
    if union <= 0:
        return 0.0
    return overlap / union


def _overlap_fraction(
    mask: Sequence[Tuple[int, int]], outline: Sequence[Tuple[int, int]]
) -> Optional[float]:
    area = _length(mask)
    if area <= 0:
        return None
    return _overlap_length(mask, outline) / area


# ---------------------------------------------------------------------------
# Detection ids (design DD5)
# ---------------------------------------------------------------------------


def attach_detection_ids(raw_entries: Any, built_entries: Any) -> List[dict]:
    """Copies of the raw Detection_List entries with their Detection_IDs.

    Each raw entry (the marshal's list form, carrying ``mask_rle``) takes
    the ``id`` of the first unused built entry (``detections`` in the run
    metadata, carrying the ids) whose ``label == str(class_label)``,
    ``confidence == float(confidence)`` and ``(x_min, y_min, x_max,
    y_max) == tuple(float(v) for v in bounding_box)``, in built-list
    order: the three fields the device normalizer copies verbatim. An
    entry nothing matches, or every entry when ``built_entries`` is not a
    list, gets ``id = None``. A raw entry without ``class_label`` compares
    as ``"None"`` against the normalizer's ``""`` and stays unjoined. Raw
    items that are not mappings are dropped.
    """
    if not isinstance(raw_entries, (list, tuple)):
        return []
    available: Dict[Tuple[Any, ...], List[Any]] = {}
    if isinstance(built_entries, (list, tuple)):
        for built in built_entries:
            key = _built_key(built)
            if key is not None:
                available.setdefault(key, []).append(built.get("id"))

    joined: List[dict] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        key = _raw_key(raw)
        ids = available.get(key) if key is not None else None
        entry["id"] = ids.pop(0) if ids else None
        joined.append(entry)
    return joined


def _raw_key(raw: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    confidence = raw.get("confidence")
    box = raw.get("bounding_box")
    if not scene._is_number(confidence) or not isinstance(box, (list, tuple)):
        return None
    if len(box) != 4 or not all(scene._is_number(value) for value in box):
        return None
    return (
        str(raw.get("class_label")),
        float(confidence),
        tuple(float(value) for value in box),
    )


def _built_key(built: Any) -> Optional[Tuple[Any, ...]]:
    if not isinstance(built, dict):
        return None
    label = built.get("label")
    values = (
        built.get("confidence"),
        built.get("x_min"),
        built.get("y_min"),
        built.get("x_max"),
        built.get("y_max"),
    )
    if not isinstance(label, str) or not all(scene._is_number(value) for value in values):
        return None
    return (label, float(values[0]), tuple(float(value) for value in values[1:]))


# ---------------------------------------------------------------------------
# The check (Requirements 5.2, 5.3; design §1 steps 1-6, DD2, DD23)
# ---------------------------------------------------------------------------


def check_layout(
    entries: Any,
    frame_size: Any,
    regions: Any,
    *,
    min_iou: Any = DEFAULT_MIN_IOU,
    min_confidence: Any = DEFAULT_MIN_CONFIDENCE,
    min_outline_overlap: Any = DEFAULT_MIN_OUTLINE_OVERLAP,
) -> Dict[str, Any]:
    """Check every configured class's detections against its region.

    ``entries`` are raw Detection_List entries in list form
    (``class_label``, ``confidence``, ``bounding_box``, optional
    ``mask_rle``, ``mask_truncated`` and ``id``); ``frame_size`` is the
    ``(width, height)`` of the frame the masks were computed at, or a
    mapping with ``width``/``height``; ``regions`` is the result of
    :func:`parse_expected_regions`.

    Returns the ``layout.<nodeId>`` run metadata (:data:`LAYOUT_METADATA_KEYS`)
    plus the diagnostic ``outcome`` and ``problems``:

    1. ``regions`` unusable: ``error``, no pieces.
    2. ``entries`` not a list: ``warning`` "no detections to check".
    3. frame size unknown: ``warning`` "frame size unknown". In cases 2
       and 3 every class is ``missing``.
    4. Otherwise a detection *qualifies* for a class when
       ``str(class_label)`` is its label and its confidence (0 when not a
       number) is at least ``min_confidence``. A qualifying detection
       whose mask decodes to at least one pixel and has an
       Outline_Overlap below ``min_outline_overlap`` lies outside the car:
       it is listed under ``ignored``, never counted, never flagged. Every
       other qualifying detection is counted, those without a usable mask
       too (iou ``None``, a problem naming the detection, ``warning``).
       Over the counted detections a class is ``missing`` (none),
       ``duplicate`` (more than one, all flagged), ``ok`` (its iou is at
       least ``min_iou``) or ``misplaced`` (flagged). A counted detection
       without an id adds a ``warning`` too. A ``min_outline_overlap``
       that is not a number in 0 to 0.5 is replaced by the default, with a
       ``warning``.
    5. Labels of detections that reach ``min_confidence`` but name no
       configured class are listed under ``unexpected_classes``, wherever
       they lie, with a ``warning``; an entry without ``class_label`` names
       no class.
    6. ``pass`` holds iff the outcome is ``ok`` and every configured class
       is ``ok``.

    IoU and Outline_Overlap values are rounded to 4 decimals; a class's
    ``iou`` is its best counted detection's. ``ignored`` keeps the input
    order; ``flagged`` lists ids in class order, then input order.
    """
    problems: List[str] = []
    outcome = scene.OUTCOME_OK
    iou_threshold = scene._as_float(min_iou, DEFAULT_MIN_IOU)
    confidence_threshold = scene._as_float(min_confidence, DEFAULT_MIN_CONFIDENCE)
    overlap_threshold = _outline_threshold(min_outline_overlap)
    frame = _frame(frame_size)
    polygons = _usable_regions(regions)

    if polygons is None:
        problems.append(_PROBLEM_REGIONS_UNUSABLE)
        outcome = scene._worse(outcome, scene.OUTCOME_ERROR)
    if overlap_threshold is None:
        problems.append(
            "min_outline_overlap {0!r} is not a number between 0 and {1}; the "
            "default {2} is used".format(
                min_outline_overlap, MAX_MIN_OUTLINE_OVERLAP, DEFAULT_MIN_OUTLINE_OVERLAP
            )
        )
        outcome = scene._worse(outcome, scene.OUTCOME_WARNING)
        overlap_threshold = DEFAULT_MIN_OUTLINE_OVERLAP

    result: Dict[str, Any] = {
        "pass": False,
        "pieces": {},
        "flagged": [],
        "ignored": [],
        "unexpected_classes": [],
        "expected_regions": None,
        "frame": None if frame is None else {"width": frame[0], "height": frame[1]},
        "min_iou": iou_threshold,
        "min_confidence": confidence_threshold,
        "min_outline_overlap": overlap_threshold,
    }
    if polygons is None:
        return _with_diagnostics(result, outcome, problems)

    result["expected_regions"] = {
        label: [[x, y] for x, y in ring] for label, ring in polygons.items()
    }
    pieces = {label: _piece(STATUS_MISSING, None, []) for label in polygons}
    result["pieces"] = pieces

    usable: Optional[List[Dict[str, Any]]] = None
    if isinstance(entries, (list, tuple)):
        usable = [entry for entry in entries if isinstance(entry, dict)]
    else:
        problems.append(_PROBLEM_NO_DETECTIONS)
        outcome = scene._worse(outcome, scene.OUTCOME_WARNING)
    if frame is None:
        problems.append(_PROBLEM_FRAME_UNKNOWN)
        outcome = scene._worse(outcome, scene.OUTCOME_WARNING)

    if usable is not None and frame is not None:
        width, height = frame
        region_intervals = {
            label: _region_intervals(ring, width, height)
            for label, ring in polygons.items()
        }
        outline = _merge(
            [interval for intervals in region_intervals.values() for interval in intervals]
        )
        counted: Dict[str, List[Dict[str, Any]]] = {label: [] for label in polygons}
        for entry in usable:
            label = str(entry.get("class_label"))
            if label not in counted:
                continue
            confidence = scene._confidence_of(entry)
            if confidence < confidence_threshold:
                continue
            mask, mask_problem = _entry_mask(entry, width, height)
            overlap = None
            if mask:
                overlap = round(_overlap_fraction(mask, outline), _DECIMALS)
                if overlap < overlap_threshold:
                    result["ignored"].append({
                        "id": entry.get("id"),
                        "class_label": label,
                        "confidence": confidence,
                        "bounding_box": _box(entry.get("bounding_box")),
                        "outline_overlap": overlap,
                    })
                    continue
            if mask is None:
                problems.append(
                    "{0} has no usable mask: {1}".format(_describe(entry, label), mask_problem)
                )
                outcome = scene._worse(outcome, scene.OUTCOME_WARNING)
            if entry.get("id") is None:
                problems.append("a {0} detection has no detection id".format(label))
                outcome = scene._worse(outcome, scene.OUTCOME_WARNING)
            iou = None
            if mask is not None:
                iou = round(_iou(mask, region_intervals[label]), _DECIMALS)
            counted[label].append({
                "id": entry.get("id"),
                "class_label": label,
                "confidence": confidence,
                "bounding_box": _box(entry.get("bounding_box")),
                "iou": iou,
                "outline_overlap": overlap,
            })

        for label, detections in counted.items():
            if not detections:
                continue
            scores = [detection["iou"] for detection in detections if detection["iou"] is not None]
            best = max(scores) if scores else None
            if len(detections) > 1:
                status = STATUS_DUPLICATE
                flagged = detections
            elif best is not None and best >= iou_threshold:
                status = STATUS_OK
                flagged = []
            else:
                status = STATUS_MISPLACED
                flagged = detections
            pieces[label] = _piece(status, best, detections)
            result["flagged"].extend(
                detection["id"] for detection in flagged if detection["id"] is not None
            )

    if usable is not None:
        # An entry without class_label names no class (design DD5: it
        # qualifies for nothing), so it is not reported as one either.
        unexpected = sorted({
            str(entry.get("class_label"))
            for entry in usable
            if entry.get("class_label") is not None
            and str(entry.get("class_label")) not in polygons
            and scene._confidence_of(entry) >= confidence_threshold
        })
        if unexpected:
            result["unexpected_classes"] = unexpected
            problems.append("unexpected classes: {0}".format(", ".join(unexpected)))
            outcome = scene._worse(outcome, scene.OUTCOME_WARNING)

    result["pass"] = outcome == scene.OUTCOME_OK and all(
        piece["status"] == STATUS_OK for piece in pieces.values()
    )
    return _with_diagnostics(result, outcome, problems)


def run_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    """The ``layout.<nodeId>`` section of :func:`check_layout`'s result:
    exactly the :data:`LAYOUT_METADATA_KEYS`, never ``outcome``,
    ``problems`` or a mask."""
    if not isinstance(result, dict):
        return {}
    return {key: result[key] for key in LAYOUT_METADATA_KEYS if key in result}


def _usable_regions(regions: Any) -> Optional[Dict[str, List[Tuple[float, float]]]]:
    """``regions`` as ``{label: vertices}``, or ``None`` when it is not a
    non-empty mapping of string labels to polygons of at least 3 points."""
    if not isinstance(regions, dict) or not regions:
        return None
    usable: Dict[str, List[Tuple[float, float]]] = {}
    for label, polygon in regions.items():
        vertices = _vertices(polygon)
        if not isinstance(label, str) or vertices is None or len(vertices) < MIN_VERTICES:
            return None
        usable[label] = vertices
    return usable


def _outline_threshold(value: Any) -> Optional[float]:
    """``min_outline_overlap`` as a number in 0 to 0.5, or ``None``."""
    number = scene._as_float(value, math.nan)
    if not (0.0 <= number <= MAX_MIN_OUTLINE_OVERLAP):
        return None
    return number


def _frame(frame_size: Any) -> Optional[Tuple[int, int]]:
    """``(width, height)`` as positive ints, or ``None`` when unknown."""
    dimensions = scene._frame_dimensions(frame_size)
    if dimensions is None:
        return None
    width, height = _dimension(dimensions[0]), _dimension(dimensions[1])
    if width is None or height is None:
        return None
    return width, height


def _entry_mask(
    entry: Dict[str, Any], width: int, height: int
) -> Tuple[Optional[List[Tuple[int, int]]], Optional[str]]:
    """An entry's mask as intervals, or ``None`` and the reason."""
    if entry.get("mask_truncated"):
        return None, "its mask was truncated"
    rle = entry.get("mask_rle")
    if rle is None:
        return None, "it carries no mask_rle"
    return rle_intervals(rle, width, height)


def _box(box: Any) -> Optional[List[float]]:
    """A bounding box as four floats, or ``None`` when it is not one."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if not all(scene._is_number(value) for value in box):
        return None
    return [float(value) for value in box]


def _describe(entry: Dict[str, Any], label: str) -> str:
    detection_id = entry.get("id")
    if detection_id is None:
        return "a {0} detection without an id".format(label)
    return "detection {0} ({1})".format(detection_id, label)


def _piece(status: str, iou: Optional[float], detections: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"status": status, "iou": iou, "detections": detections}


def _with_diagnostics(result: Dict[str, Any], outcome: str, problems: List[str]) -> Dict[str, Any]:
    result["outcome"] = outcome
    result["problems"] = problems
    return result
