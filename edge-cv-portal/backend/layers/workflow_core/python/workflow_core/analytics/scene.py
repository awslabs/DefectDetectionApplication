"""Scene analytics: counter, association and event-gate rules.

One pure module (standard library only, no I/O and no logging) holding
every rule the three Scene_Analytics_Nodes need, so that the LocalServer
executor bindings (``workflow_engine/scene_analytics.py`` and
``output_bindings.py``), the Portal's cloud test sandbox harness and the
workflow validator (V13) all agree
(rtsp-rtmp-stream-cameras Requirements 13.2-13.7, 14.2-14.4, 15.2, 15.3).

Vocabulary, from the requirements glossary:

- **Detection_List**: the run metadata ``detections`` list. Each entry has
  ``id``, ``label``, ``confidence``, ``x_min``, ``y_min``, ``x_max`` and
  ``y_max``, with coordinates in **source-frame pixels**.
- **Label_Key**: a detection label normalized for use in metadata keys and
  in the Condition_Language: lowercased, every run of characters other
  than ASCII letters and digits replaced by one underscore, leading and
  trailing underscores removed (:func:`label_key`).
- **Zone**: a polygon of 3 to 32 points in **normalized** frame
  coordinates (0.0 to 1.0). It is scaled by ``frame_size``, the
  ``(width, height)`` of the frame the detector processed, before it is
  compared against detection boxes.

Contract notes, all of them deliberate and pinned by the property tests
of tasks 1.5 to 1.8 (Properties 23 to 26):

- **Totality.** Nothing here raises. Every function accepts whatever the
  run metadata or a node's parameters happen to hold (``None``, a raw
  parameter string, a pre-parsed structure, a malformed entry) and
  degrades to a reported problem instead of an exception, because these
  run inside a workflow run that must not fail on a configuration
  mistake (Requirements 13.5, 13.6).
- **Outcomes.** :func:`count_detections` and :func:`associate` return the
  run metadata of their node *plus* two diagnostic keys, ``outcome``
  (``ok``, ``warning`` or ``error``) and ``problems``, which the bindings
  use for node status and for gating. :func:`run_metadata` projects out
  exactly the keys that are merged into the run metadata, so the merged
  shape is precisely the one the design specifies.
- **Confidence and zone filtering in :func:`associate`** apply to the
  *subjects* only (Requirement 14.2). A required-class detection is
  eligible on its label and on the overlap rule alone (Requirement
  14.3), so a hard hat just outside the zone still makes the person
  inside it compliant.
- **An empty Label_Key** (a label with no ASCII letters or digits) is
  kept as the ``""`` key in ``counts``, so that ``total`` always equals
  the sum of ``counts``. Such a key is not addressable from the
  Condition_Language, which is why :func:`parse_label_list` reports a
  configured label that normalizes to it as a problem.
- **Determinism.** Every result is built in a deterministic order:
  ``counts`` lists the configured classes first, then newly seen labels
  in Detection_List order, and the association matching is a fixed
  greedy order (overlap descending, then subject order, then detection
  order), so identical input gives byte-identical metadata on the device
  and in the sandbox.
"""

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "LABEL_KEY_PATTERN",
    "MIN_ZONE_POINTS",
    "MAX_ZONE_POINTS",
    "MAX_CLASS_LIST_ITEMS",
    "MAX_REQUIRED_CLASSES",
    "ZONE_RULE_CENTER",
    "ZONE_RULE_OVERLAP",
    "ZONE_RULES",
    "DEFAULT_ZONE_RULE",
    "DEFAULT_MIN_OVERLAP",
    "OUTCOME_OK",
    "OUTCOME_WARNING",
    "OUTCOME_ERROR",
    "COUNTER_METADATA_KEYS",
    "ASSOCIATION_METADATA_KEYS",
    "EMIT_ON_ACTIVATE",
    "EMIT_ON_CHANGE",
    "EMIT_WHILE_ACTIVE",
    "EMIT_MODES",
    "DEFAULT_EMIT",
    "DEFAULT_ACTIVATE_AFTER",
    "DEFAULT_CLEAR_AFTER",
    "TRANSITION_ACTIVATED",
    "TRANSITION_CLEARED",
    "TRANSITION_NONE",
    "GATE_STATE_ACTIVE",
    "GATE_STATE_INACTIVE",
    "label_key",
    "parse_label_list",
    "parse_zone",
    "point_in_polygon",
    "box_intersects_polygon",
    "count_detections",
    "associate",
    "run_metadata",
    "EventGateState",
    "step_event_gate",
    "event_gate_metadata",
]

# ---------------------------------------------------------------------------
# Constants (Requirements 13.1, 13.7, 14.1, 15.1)
# ---------------------------------------------------------------------------

#: The shape of every non-empty Label_Key. A Label_Key is therefore also
#: a legal dotted-path segment of the Condition_Language and of an output
#: template placeholder, whose segments are ``[A-Za-z0-9_]+``
#: (Requirements 13.3, 13.4).
LABEL_KEY_PATTERN = r"^[a-z0-9]+(_[a-z0-9]+)*$"

#: A Zone has at least 3 and at most 32 points (Requirement 13.7).
MIN_ZONE_POINTS = 3
MAX_ZONE_POINTS = 32

#: Cap for a ``detection_counter.classes`` list. The requirement does not
#: bound it; the Zone bound is reused so a pathological parameter cannot
#: fan the metadata out without limit.
MAX_CLASS_LIST_ITEMS = 32

#: ``object_association.required_classes`` holds 1 to 10 labels
#: (Requirement 14.1).
MAX_REQUIRED_CLASSES = 10

#: Zone rules of ``detection_counter.zone_rule`` (Requirement 13.2).
ZONE_RULE_CENTER = "center"
ZONE_RULE_OVERLAP = "overlap"
ZONE_RULES = (ZONE_RULE_CENTER, ZONE_RULE_OVERLAP)
DEFAULT_ZONE_RULE = ZONE_RULE_CENTER

#: Default ``object_association.min_overlap`` (Requirement 14.1).
DEFAULT_MIN_OVERLAP = 0.5

#: Node outcomes. ``error`` gates the node's downstream nodes without
#: failing the run (Requirement 13.6); ``warning`` only annotates it
#: (Requirement 13.5).
OUTCOME_OK = "ok"
OUTCOME_WARNING = "warning"
OUTCOME_ERROR = "error"

_OUTCOME_SEVERITY = {OUTCOME_OK: 0, OUTCOME_WARNING: 1, OUTCOME_ERROR: 2}

#: The keys of :func:`count_detections` that are merged into the run
#: metadata under ``counter.<nodeId>`` (Requirement 13.3).
COUNTER_METADATA_KEYS = ("counts", "total", "labels")

#: The keys of :func:`associate` that are merged into the run metadata
#: under ``association.<nodeId>`` (Requirement 14.4).
ASSOCIATION_METADATA_KEYS = (
    "subjects",
    "compliant",
    "violations",
    "missing",
    "violating_ids",
)

#: Diagnostic keys every analytics result carries and that are never
#: merged into the run metadata.
_DIAGNOSTIC_KEYS = ("outcome", "problems")

#: ``event_gate.emit`` modes and defaults (Requirement 15.1).
EMIT_ON_ACTIVATE = "on_activate"
EMIT_ON_CHANGE = "on_change"
EMIT_WHILE_ACTIVE = "while_active"
EMIT_MODES = (EMIT_ON_ACTIVATE, EMIT_ON_CHANGE, EMIT_WHILE_ACTIVE)
DEFAULT_EMIT = EMIT_ON_ACTIVATE
DEFAULT_ACTIVATE_AFTER = 3
DEFAULT_CLEAR_AFTER = 3

#: ``event.<nodeId>.transition`` values (Requirement 15.4).
TRANSITION_ACTIVATED = "activated"
TRANSITION_CLEARED = "cleared"
TRANSITION_NONE = "none"

#: ``event.<nodeId>.state`` values.
GATE_STATE_ACTIVE = "active"
GATE_STATE_INACTIVE = "inactive"

#: Runs of characters that are not ASCII lowercase letters or digits;
#: each run collapses to one underscore.
_NON_LABEL_CHARS_RE = re.compile(r"[^a-z0-9]+")

#: Relative slack for the boundary-inclusive geometry predicates. A Zone
#: is scaled into pixel space, so an exact comparison would make
#: "on the edge" depend on the rounding of that multiplication.
_GEOMETRY_TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# Label keys (Requirements 13.3, 13.4)
# ---------------------------------------------------------------------------


def label_key(label: Any) -> str:
    """The Label_Key of ``label``.

    Lowercases, replaces every run of characters other than ASCII letters
    and digits by one underscore, and removes leading and trailing
    underscores. The result matches :data:`LABEL_KEY_PATTERN`, or is
    empty for a label with no ASCII letters or digits, and is idempotent:
    ``label_key(label_key(x)) == label_key(x)``.

    Total: ``None`` yields ``""`` and any other non-string is read
    through :class:`str` first, so a detection whose label came back as a
    number still gets a key.
    """
    if label is None:
        return ""
    text = label if isinstance(label, str) else str(label)
    return _NON_LABEL_CHARS_RE.sub("_", text.lower()).strip("_")


def parse_label_list(text: Any, max_items: int) -> Tuple[List[str], List[str]]:
    """Parse a comma-separated label parameter into Label_Keys.

    Returns ``(label keys, problems)``. The keys are de-duplicated,
    keeping the first occurrence's position, so the metadata order
    follows the operator's spelling. ``problems`` is empty exactly when
    the value is well formed; it reports, in this order, an empty entry
    (Requirement 13.7 "an empty label"), an entry with no ASCII letters
    or digits (its Label_Key would be empty, hence unaddressable), and
    more than ``max_items`` entries.

    A blank or missing value yields ``([], [])``: emptiness is not
    malformedness, and a parameter that is *required* (
    ``object_association.required_classes``) is reported by the
    required-parameter rule, not here.

    Keys that did parse are returned even when there are problems, so a
    runtime caller can still do its best while the validator reports the
    error.
    """
    items = _label_items(text)
    problems: List[str] = []
    keys: List[str] = []
    seen = set()
    for position, item in enumerate(items, start=1):
        if item == "":
            problems.append(
                "Label list entry {0} is empty; remove the extra comma.".format(position)
            )
            continue
        key = label_key(item)
        if not key:
            problems.append(
                "Label '{0}' has no letters or digits, so it cannot be used as a "
                "metadata key.".format(item)
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    limit = _as_int(max_items, 0)
    if limit > 0 and len(items) > limit:
        problems.append(
            "Label list has {0} entries; at most {1} are allowed.".format(len(items), limit)
        )
    return keys, problems


def _label_items(text: Any) -> List[str]:
    """The raw, stripped entries of a label parameter.

    Accepts the parameter as written (``"person, hardhat"``) or as an
    already-split sequence, so a caller that keeps its own list does not
    have to re-join it.
    """
    if text is None:
        return []
    if isinstance(text, str):
        if text.strip() == "":
            return []
        return [item.strip() for item in text.split(",")]
    if isinstance(text, (list, tuple, set, frozenset)):
        ordered = (
            sorted(text, key=lambda item: str(item))
            if isinstance(text, (set, frozenset))
            else text
        )
        return [
            ("" if item is None else item if isinstance(item, str) else str(item)).strip()
            for item in ordered
        ]
    return [str(text).strip()]


def _label_pairs(labels: Any) -> List[Tuple[str, str]]:
    """``(Label_Key, label as written)`` pairs, de-duplicated by key.

    Entries whose Label_Key would be empty are dropped: they cannot be
    addressed, and :func:`parse_label_list` already reports them.
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for item in _label_items(labels):
        key = label_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((key, item))
    return pairs


# ---------------------------------------------------------------------------
# Zones (Requirements 13.6, 13.7)
# ---------------------------------------------------------------------------


def parse_zone(text: Any) -> Tuple[Optional[List[Tuple[float, float]]], List[str]]:
    """Parse a Zone parameter into normalized polygon points.

    Returns ``(points, problems)``: a list of ``(x, y)`` float pairs in
    normalized coordinates when the value is a well-formed Zone, and
    ``(None, problems)`` when it is malformed. A blank or missing value
    yields ``(None, [])`` — no Zone is configured, which is not a
    problem, the parameter being optional.

    Accepted spellings, all equivalent:

    - ``[[x, y], ...]`` (the canonical form),
    - ``[{"x": x, "y": y}, ...]``,
    - ``{"points": [...]}`` wrapping either of the above.

    Malformed means, per Requirement 13.7: invalid JSON, fewer than
    :data:`MIN_ZONE_POINTS` or more than :data:`MAX_ZONE_POINTS` points,
    or a coordinate that is not a number in 0 to 1. Every problem names
    the offending point by its 1-based position.
    """
    if text is None:
        return None, []
    raw: Any = text
    if isinstance(text, str):
        if text.strip() == "":
            return None, []
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return None, ["Zone is not valid JSON."]

    if isinstance(raw, dict):
        if "points" not in raw:
            return None, [
                "Zone object must have a 'points' list of [x, y] pairs.",
            ]
        raw = raw.get("points")

    if not isinstance(raw, (list, tuple)):
        return None, [
            "Zone must be a list of [x, y] points, or an object with a 'points' list.",
        ]

    problems: List[str] = []
    points: List[Tuple[float, float]] = []
    for position, entry in enumerate(raw, start=1):
        point = _zone_point(entry)
        if point is None:
            problems.append(
                "Zone point {0} must be a pair of numbers [x, y].".format(position)
            )
            continue
        x, y = point
        if not (0.0 <= x <= 1.0):
            problems.append(
                "Zone point {0} x coordinate must be between 0 and 1.".format(position)
            )
            continue
        if not (0.0 <= y <= 1.0):
            problems.append(
                "Zone point {0} y coordinate must be between 0 and 1.".format(position)
            )
            continue
        points.append((x, y))

    count = len(raw)
    if count < MIN_ZONE_POINTS or count > MAX_ZONE_POINTS:
        problems.append(
            "Zone must have between {0} and {1} points; this one has {2}.".format(
                MIN_ZONE_POINTS, MAX_ZONE_POINTS, count
            )
        )

    if problems:
        return None, problems
    return points, []


def _zone_point(entry: Any) -> Optional[Tuple[float, float]]:
    """One Zone point as ``(x, y)``, or ``None`` when it is not a point."""
    if isinstance(entry, dict):
        x, y = entry.get("x"), entry.get("y")
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        x, y = entry[0], entry[1]
    else:
        return None
    if not _is_number(x) or not _is_number(y):
        return None
    return float(x), float(y)


# ---------------------------------------------------------------------------
# Geometry (Requirement 13.2)
# ---------------------------------------------------------------------------


def point_in_polygon(x: float, y: float, polygon: Any) -> bool:
    """Whether ``(x, y)`` lies inside ``polygon``.

    Even-odd (ray casting) rule, boundary inclusive: a point on an edge
    or on a vertex is inside. ``polygon`` is a sequence of ``(x, y)``
    pairs in the same coordinate space as the point — for a Zone, the
    scaled pixel-space points. A polygon with fewer than
    :data:`MIN_ZONE_POINTS` points contains nothing.

    Boundary membership carries a small relative tolerance, because a
    Zone's pixel-space points come from a multiplication whose exact
    result is not meaningful.
    """
    points = _polygon_points(polygon)
    if len(points) < MIN_ZONE_POINTS:
        return False
    if not _is_number(x) or not _is_number(y):
        return False
    px, py = float(x), float(y)

    inside = False
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        if _point_on_segment(px, py, x1, y1, x2, y2):
            return True
        if (y1 > py) != (y2 > py):
            # The edge straddles the ray; y2 != y1, so this is safe.
            crossing_x = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < crossing_x:
                inside = not inside
    return inside


def box_intersects_polygon(box: Any, polygon: Any) -> bool:
    """Whether the axis-aligned ``box`` and ``polygon`` share any area.

    True when the box touches the polygon at all: a polygon vertex inside
    (or on) the box, a box corner inside (or on) the polygon, or any pair
    of edges crossing. Containment either way is therefore covered.

    ``box`` is ``(x_min, y_min, x_max, y_max)``, or a Detection_List
    entry (a mapping with those keys), in the polygon's coordinate space.
    Inverted coordinates are read as the absolute rectangle they span.
    """
    points = _polygon_points(polygon)
    bounds = _box_bounds(box)
    if bounds is None or len(points) < MIN_ZONE_POINTS:
        return False
    x_min, y_min, x_max, y_max = bounds

    for vertex_x, vertex_y in points:
        if x_min <= vertex_x <= x_max and y_min <= vertex_y <= y_max:
            return True

    corners = (
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    )
    for corner_x, corner_y in corners:
        if point_in_polygon(corner_x, corner_y, points):
            return True

    count = len(points)
    for index in range(count):
        edge_start = points[index]
        edge_end = points[(index + 1) % count]
        for corner_index in range(4):
            if _segments_intersect(
                edge_start,
                edge_end,
                corners[corner_index],
                corners[(corner_index + 1) % 4],
            ):
                return True
    return False


def _polygon_points(polygon: Any) -> List[Tuple[float, float]]:
    """``polygon`` as a list of numeric ``(x, y)`` pairs; malformed
    points are dropped, so the predicates stay total."""
    if not isinstance(polygon, (list, tuple)):
        return []
    points: List[Tuple[float, float]] = []
    for entry in polygon:
        point = _zone_point(entry)
        if point is not None:
            points.append(point)
    return points


def _box_bounds(box: Any) -> Optional[Tuple[float, float, float, float]]:
    """``box`` as ``(x_min, y_min, x_max, y_max)``, ordered, or ``None``."""
    if isinstance(box, dict):
        values = (
            box.get("x_min"),
            box.get("y_min"),
            box.get("x_max"),
            box.get("y_max"),
        )
    elif isinstance(box, (list, tuple)) and len(box) == 4:
        values = (box[0], box[1], box[2], box[3])
    else:
        return None
    if not all(_is_number(value) for value in values):
        return None
    x_one, y_one, x_two, y_two = (float(value) for value in values)
    return (
        min(x_one, x_two),
        min(y_one, y_two),
        max(x_one, x_two),
        max(y_one, y_two),
    )


def _point_on_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> bool:
    """Whether ``(px, py)`` lies on the segment, within tolerance."""
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    slack = _GEOMETRY_TOLERANCE * (
        1.0 + abs(x2 - x1) + abs(y2 - y1) + abs(px - x1) + abs(py - y1)
    )
    if abs(cross) > slack:
        return False
    return (
        min(x1, x2) - slack <= px <= max(x1, x2) + slack
        and min(y1, y2) - slack <= py <= max(y1, y2) + slack
    )


def _cross(
    origin: Tuple[float, float],
    first: Tuple[float, float],
    second: Tuple[float, float],
) -> float:
    """The z component of ``(first - origin) x (second - origin)``."""
    return (first[0] - origin[0]) * (second[1] - origin[1]) - (
        first[1] - origin[1]
    ) * (second[0] - origin[0])


def _segments_intersect(
    a: Tuple[float, float],
    b: Tuple[float, float],
    c: Tuple[float, float],
    d: Tuple[float, float],
) -> bool:
    """Whether segments ``ab`` and ``cd`` intersect, touching included."""
    d1 = _cross(c, d, a)
    d2 = _cross(c, d, b)
    d3 = _cross(a, b, c)
    d4 = _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return (
        _point_on_segment(a[0], a[1], c[0], c[1], d[0], d[1])
        or _point_on_segment(b[0], b[1], c[0], c[1], d[0], d[1])
        or _point_on_segment(c[0], c[1], a[0], a[1], b[0], b[1])
        or _point_on_segment(d[0], d[1], a[0], a[1], b[0], b[1])
    )


# ---------------------------------------------------------------------------
# Detection counter (Requirements 13.2, 13.3, 13.5, 13.6)
# ---------------------------------------------------------------------------


def count_detections(
    detections: Any,
    *,
    classes: Any = (),
    min_confidence: Any = 0.0,
    zone: Any = None,
    zone_rule: Any = DEFAULT_ZONE_RULE,
    frame_size: Any = None,
) -> Dict[str, Any]:
    """Count a Detection_List by Label_Key, optionally inside a Zone.

    Counts the entries whose confidence is at least ``min_confidence``
    and that pass the Zone when one is configured: with
    :data:`ZONE_RULE_CENTER` the box center must lie inside the Zone,
    with :data:`ZONE_RULE_OVERLAP` the box must intersect it
    (Requirement 13.2).

    ``classes`` is the parameter as written (``"person, hardhat"``) or an
    already-split sequence; each of its labels is present in ``counts``,
    zero when unseen (Requirement 13.3). ``zone`` is the parameter as
    written (a JSON string), an already-parsed point list, or ``None``.
    ``frame_size`` is the ``(width, height)`` of the frame the detector
    processed, or a mapping with ``width``/``height``; it scales the
    normalized Zone into pixel space.

    Returns ``{"counts", "total", "labels"}`` — the run metadata of
    Requirement 13.3, where ``labels`` maps each Label_Key to its
    original label, preferring the spelling of the first detection that
    carried it and falling back to the spelling in ``classes`` — plus the
    diagnostic ``outcome`` and ``problems`` (see :func:`run_metadata`):

    - no Detection_List (``None``, or anything but a list): zero counts
      and a ``warning`` (Requirement 13.5),
    - a Zone that is configured but unusable, because it is malformed or
      because the frame dimensions are unknown: zero counts and an
      ``error``, which gates the node's downstream nodes without failing
      the run (Requirement 13.6).

    An entry without a usable box fails the Zone test, and an entry
    without a numeric confidence is read as confidence 0.
    """
    problems: List[str] = []
    outcome = OUTCOME_OK

    class_pairs = _label_pairs(classes)
    counts: Dict[str, int] = {key: 0 for key, _original in class_pairs}
    labels: Dict[str, str] = {key: original for key, original in class_pairs}

    entries, list_problem = _detection_entries(detections)
    if list_problem is not None:
        problems.append(list_problem)
        outcome = _worse(outcome, OUTCOME_WARNING)

    scaled_zone, zone_problems = _scaled_zone(zone, frame_size)
    if zone_problems:
        problems.extend(zone_problems)
        outcome = _worse(outcome, OUTCOME_ERROR)
        entries = []

    rule = zone_rule if zone_rule in ZONE_RULES else DEFAULT_ZONE_RULE
    threshold = _as_float(min_confidence, 0.0)

    total = 0
    from_detection = set()
    for entry in entries:
        if _confidence_of(entry) < threshold:
            continue
        if scaled_zone is not None and not _passes_zone(entry, scaled_zone, rule):
            continue
        key = label_key(entry.get("label"))
        counts[key] = counts.get(key, 0) + 1
        if key not in from_detection:
            # The detector's spelling is the label's origin; the spelling
            # in ``classes`` only stands in for a class never seen.
            labels[key] = _original_label(entry)
            from_detection.add(key)
        total += 1

    return {
        "counts": counts,
        "total": total,
        "labels": labels,
        "outcome": outcome,
        "problems": problems,
    }


def _passes_zone(entry: Dict[str, Any], polygon: Sequence[Any], rule: str) -> bool:
    """Whether ``entry`` passes ``polygon`` under the Zone rule."""
    bounds = _box_bounds(entry)
    if bounds is None:
        return False
    if rule == ZONE_RULE_OVERLAP:
        return box_intersects_polygon(bounds, polygon)
    x_min, y_min, x_max, y_max = bounds
    return point_in_polygon((x_min + x_max) / 2.0, (y_min + y_max) / 2.0, polygon)


# ---------------------------------------------------------------------------
# Object association (Requirements 14.2, 14.3, 14.4)
# ---------------------------------------------------------------------------


def associate(
    detections: Any,
    *,
    subject_class: Any,
    required_classes: Any,
    min_overlap: Any = DEFAULT_MIN_OVERLAP,
    min_confidence: Any = 0.0,
    zone: Any = None,
    frame_size: Any = None,
) -> Dict[str, Any]:
    """Match required-class detections to subjects, one to one.

    Subjects are the detections whose Label_Key equals the Label_Key of
    ``subject_class``, whose confidence is at least ``min_confidence``,
    and whose box center lies inside the Zone when one is configured
    (Requirement 14.2). A required-class detection may match a subject
    only when at least ``min_overlap`` of *its own* box area lies inside
    the subject's box; within a class each detection and each subject is
    used at most once, and larger overlaps are preferred (Requirement
    14.3). Ties are broken by subject order, then by detection order, so
    the matching is deterministic. A subject is compliant when every
    required class matched.

    Returns ``{"subjects", "compliant", "violations", "missing",
    "violating_ids"}`` — the run metadata of Requirement 14.4, where
    ``missing`` counts the subjects lacking each required class and
    ``violating_ids`` lists the Detection_IDs of the non-compliant
    subjects in Detection_List order (a subject without an ``id``
    contributes no element) — plus the diagnostic ``outcome`` and
    ``problems`` (see :func:`run_metadata`).

    The rules of Requirements 13.5 and 13.6 apply as they do to the
    counter (Requirement 14.6): no Detection_List is a ``warning``, and a
    configured but unusable Zone is an ``error`` with an empty result. A
    missing ``subject_class`` or ``required_classes`` is also an
    ``error``, rather than a vacuous "everything is compliant".
    """
    problems: List[str] = []
    outcome = OUTCOME_OK

    subject_key = label_key(subject_class)
    if not subject_key:
        problems.append("Subject class is required.")
        outcome = _worse(outcome, OUTCOME_ERROR)

    required_pairs = _label_pairs(required_classes)
    if not required_pairs:
        problems.append("At least one required class is required.")
        outcome = _worse(outcome, OUTCOME_ERROR)
    required_keys = [key for key, _original in required_pairs]

    entries, list_problem = _detection_entries(detections)
    if list_problem is not None:
        problems.append(list_problem)
        outcome = _worse(outcome, OUTCOME_WARNING)

    scaled_zone, zone_problems = _scaled_zone(zone, frame_size)
    if zone_problems:
        problems.extend(zone_problems)
        outcome = _worse(outcome, OUTCOME_ERROR)

    missing: Dict[str, int] = {key: 0 for key in required_keys}
    if outcome == OUTCOME_ERROR:
        return {
            "subjects": 0,
            "compliant": 0,
            "violations": 0,
            "missing": missing,
            "violating_ids": [],
            "outcome": outcome,
            "problems": problems,
        }

    threshold = _as_float(min_confidence, 0.0)
    overlap_threshold = _as_float(min_overlap, DEFAULT_MIN_OVERLAP)

    subjects: List[Dict[str, Any]] = []
    for entry in entries:
        if label_key(entry.get("label")) != subject_key:
            continue
        if _confidence_of(entry) < threshold:
            continue
        bounds = _box_bounds(entry)
        if scaled_zone is not None:
            if bounds is None:
                continue
            x_min, y_min, x_max, y_max = bounds
            if not point_in_polygon(
                (x_min + x_max) / 2.0, (y_min + y_max) / 2.0, scaled_zone
            ):
                continue
        subjects.append(entry)

    matched: List[set] = [set() for _subject in subjects]
    for key in required_keys:
        candidates = [
            entry for entry in entries if label_key(entry.get("label")) == key
        ]
        pairs: List[Tuple[float, int, int]] = []
        for subject_index, subject in enumerate(subjects):
            subject_bounds = _box_bounds(subject)
            if subject_bounds is None:
                continue
            for candidate_index, candidate in enumerate(candidates):
                overlap = _overlap_fraction(_box_bounds(candidate), subject_bounds)
                if overlap >= overlap_threshold:
                    pairs.append((-overlap, subject_index, candidate_index))
        pairs.sort()
        used_subjects: set = set()
        used_candidates: set = set()
        for _negative_overlap, subject_index, candidate_index in pairs:
            if subject_index in used_subjects or candidate_index in used_candidates:
                continue
            used_subjects.add(subject_index)
            used_candidates.add(candidate_index)
            matched[subject_index].add(key)
        missing[key] = len(subjects) - len(used_subjects)

    compliant = 0
    violating_ids: List[str] = []
    for subject_index, subject in enumerate(subjects):
        if all(key in matched[subject_index] for key in required_keys):
            compliant += 1
            continue
        detection_id = subject.get("id")
        if detection_id is not None:
            violating_ids.append(
                detection_id if isinstance(detection_id, str) else str(detection_id)
            )

    return {
        "subjects": len(subjects),
        "compliant": compliant,
        "violations": len(subjects) - compliant,
        "missing": missing,
        "violating_ids": violating_ids,
        "outcome": outcome,
        "problems": problems,
    }


def _overlap_fraction(
    inner: Optional[Tuple[float, float, float, float]],
    outer: Optional[Tuple[float, float, float, float]],
) -> float:
    """``area(inner ∩ outer) / area(inner)``; 0 when ``inner`` has no area."""
    if inner is None or outer is None:
        return 0.0
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    if inner_area <= 0.0:
        return 0.0
    width = min(inner[2], outer[2]) - max(inner[0], outer[0])
    height = min(inner[3], outer[3]) - max(inner[1], outer[1])
    if width <= 0.0 or height <= 0.0:
        return 0.0
    return (width * height) / inner_area


# ---------------------------------------------------------------------------
# The run metadata projection
# ---------------------------------------------------------------------------


def run_metadata(result: Dict[str, Any]) -> Dict[str, Any]:
    """``result`` without its diagnostic keys.

    :func:`count_detections` and :func:`associate` carry ``outcome`` and
    ``problems`` for node status and gating; only the remaining keys are
    merged into the run metadata (Requirements 13.3, 14.4). Both
    bindings project through this one function so the merged shape cannot
    drift between the device and the sandbox.
    """
    if not isinstance(result, dict):
        return {}
    return {
        key: value for key, value in result.items() if key not in _DIAGNOSTIC_KEYS
    }


# ---------------------------------------------------------------------------
# Event gate (Requirements 15.2, 15.3, 15.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventGateState:
    """The Event_Gate_State of one gate, per registration and per node.

    Immutable: :func:`step_event_gate` returns the next state rather than
    mutating this one, so a store can keep it by value. The default is
    the state a new registration and a restarted backend start from
    (Requirement 15.5).
    """

    active: bool = False
    consecutive_true: int = 0
    consecutive_false: int = 0
    active_since_ms: Optional[int] = None
    last_emit_ms: Optional[int] = None


def step_event_gate(
    state: EventGateState,
    outcome: Optional[bool],
    *,
    activate_after: int = DEFAULT_ACTIVATE_AFTER,
    clear_after: int = DEFAULT_CLEAR_AFTER,
    emit: str = DEFAULT_EMIT,
    repeat_interval_ms: int = 0,
    now_ms: int = 0,
) -> Tuple[EventGateState, bool, str]:
    """Advance one gate by one run.

    ``outcome`` is the run's condition verdict; ``None`` means it could
    not be evaluated and counts as false (Requirement 15.2). The gate
    becomes active on the run whose consecutive-true count reaches
    ``activate_after``, and inactive on the run whose consecutive-false
    count reaches ``clear_after``.

    Returns ``(next state, passed, transition)``:

    - ``passed`` is true exactly on the runs the ``emit`` mode lets
      through to the gate's downstream nodes (Requirement 15.3):
      :data:`EMIT_ON_ACTIVATE` on the activating run,
      :data:`EMIT_ON_CHANGE` on an activating or clearing run, and
      :data:`EMIT_WHILE_ACTIVE` on every run the gate is active, at most
      once per ``repeat_interval_ms`` when that is greater than 0.
    - ``transition`` is :data:`TRANSITION_ACTIVATED`,
      :data:`TRANSITION_CLEARED` or :data:`TRANSITION_NONE`.

    The consecutive counters are the run length of the current verdict,
    so they keep growing while it holds; the metadata reports them
    (Requirement 15.4). Clearing forgets the last emit time, so the next
    activation always emits even under a repeat interval. A
    ``repeat_interval_ms`` window is measured on ``now_ms`` as given: a
    clock that jumps backwards can delay an emit, which is preferred to
    treating a jump as a reason to emit.

    Total: an unknown ``emit`` falls back to :data:`DEFAULT_EMIT`, and a
    threshold below 1 is read as 1, so a malformed parameter degrades
    instead of raising inside a run. ``now_ms`` is read the same way, so a
    timestamp that is missing or not a whole number degrades to 0 (or to
    its truncation) instead of storing a value the interval arithmetic
    cannot subtract and the metadata cannot carry as an integer.
    """
    if not isinstance(state, EventGateState):
        state = EventGateState()
    activate_threshold = max(1, _as_int(activate_after, DEFAULT_ACTIVATE_AFTER))
    clear_threshold = max(1, _as_int(clear_after, DEFAULT_CLEAR_AFTER))
    interval = max(0, _as_int(repeat_interval_ms, 0))
    stamp = _as_int(now_ms, 0)
    mode = emit if emit in EMIT_MODES else DEFAULT_EMIT
    verdict = False if outcome is None else bool(outcome)

    if verdict:
        consecutive_true = state.consecutive_true + 1
        consecutive_false = 0
    else:
        consecutive_true = 0
        consecutive_false = state.consecutive_false + 1

    active = state.active
    active_since_ms = state.active_since_ms
    last_emit_ms = state.last_emit_ms
    transition = TRANSITION_NONE

    if not active and verdict and consecutive_true >= activate_threshold:
        active = True
        active_since_ms = stamp
        transition = TRANSITION_ACTIVATED
    elif active and not verdict and consecutive_false >= clear_threshold:
        active = False
        active_since_ms = None
        last_emit_ms = None
        transition = TRANSITION_CLEARED

    if mode == EMIT_ON_ACTIVATE:
        passed = transition == TRANSITION_ACTIVATED
    elif mode == EMIT_ON_CHANGE:
        passed = transition in (TRANSITION_ACTIVATED, TRANSITION_CLEARED)
    else:
        passed = active and (
            interval == 0
            or last_emit_ms is None
            or (stamp - last_emit_ms) >= interval
        )

    if passed:
        last_emit_ms = stamp

    return (
        EventGateState(
            active=active,
            consecutive_true=consecutive_true,
            consecutive_false=consecutive_false,
            active_since_ms=active_since_ms,
            last_emit_ms=last_emit_ms,
        ),
        passed,
        transition,
    )


def event_gate_metadata(state: EventGateState, transition: str) -> Dict[str, Any]:
    """The ``event.<nodeId>`` run metadata of a gate (Requirement 15.4).

    Both bindings build it here so the device and the sandbox cannot
    drift (Requirement 15.6).
    """
    return {
        "state": GATE_STATE_ACTIVE if state.active else GATE_STATE_INACTIVE,
        "transition": transition,
        "active_since": state.active_since_ms,
        "consecutive_true": state.consecutive_true,
        "consecutive_false": state.consecutive_false,
    }


# ---------------------------------------------------------------------------
# Shared helpers (private)
# ---------------------------------------------------------------------------


def _detection_entries(detections: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The usable Detection_List entries, and the "no list" problem.

    An empty list is a Detection_List with no entries, which is not a
    problem; ``None`` (or anything that is not a list) is the missing
    Detection_List of Requirement 13.5.
    """
    if not isinstance(detections, (list, tuple)):
        return [], "The run has no detection list; reporting zero counts."
    return [entry for entry in detections if isinstance(entry, dict)], None


def _scaled_zone(
    zone: Any, frame_size: Any
) -> Tuple[Optional[List[Tuple[float, float]]], List[str]]:
    """The configured Zone in pixel space, and its problems.

    ``(None, [])`` means no Zone is configured. ``(None, problems)``
    means one is configured but unusable, either malformed
    (Requirement 13.7) or unscalable because the frame dimensions are
    unknown (Requirement 13.6).
    """
    points, problems = parse_zone(zone)
    if problems:
        return None, problems
    if points is None:
        return None, []
    size = _frame_dimensions(frame_size)
    if size is None:
        return None, [
            "The frame dimensions are unknown, so the zone cannot be applied.",
        ]
    width, height = size
    return [(x * width, y * height) for x, y in points], []


def _frame_dimensions(frame_size: Any) -> Optional[Tuple[float, float]]:
    """``(width, height)`` as positive floats, or ``None`` when unknown."""
    if isinstance(frame_size, dict):
        width, height = frame_size.get("width"), frame_size.get("height")
    elif isinstance(frame_size, (list, tuple)) and len(frame_size) == 2:
        width, height = frame_size[0], frame_size[1]
    else:
        return None
    if not _is_number(width) or not _is_number(height):
        return None
    if float(width) <= 0.0 or float(height) <= 0.0:
        return None
    return float(width), float(height)


def _confidence_of(entry: Dict[str, Any]) -> float:
    """An entry's confidence; 0 when it is missing or not a number."""
    return _as_float(entry.get("confidence"), 0.0)


def _original_label(entry: Dict[str, Any]) -> str:
    """An entry's label as the detector reported it."""
    label = entry.get("label")
    if label is None:
        return ""
    return label if isinstance(label, str) else str(label)


def _worse(current: str, candidate: str) -> str:
    """The more severe of two outcomes."""
    if _OUTCOME_SEVERITY.get(candidate, 0) > _OUTCOME_SEVERITY.get(current, 0):
        return candidate
    return current


def _is_number(value: Any) -> bool:
    """Whether ``value`` is a finite real number (``bool`` is not).

    Non-finite values (``nan``, ``inf``, which ``json.loads`` does
    accept) are rejected, so an entry carrying one is read as having no
    usable box or no confidence instead of poisoning a comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # An int beyond the float range (json.loads parses a 400-digit
        # integer exactly): not a usable number, and never an exception
        # that would take the validator down.
        return False


def _as_float(value: Any, default: float) -> float:
    """``value`` as a float, or ``default`` when it is not a number."""
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return default
        return parsed if math.isfinite(parsed) else default
    return default


def _as_int(value: Any, default: int) -> int:
    """``value`` as an int, or ``default`` when it is not a whole number."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if _is_number(value):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return default
        return int(parsed) if math.isfinite(parsed) else default
    return default
