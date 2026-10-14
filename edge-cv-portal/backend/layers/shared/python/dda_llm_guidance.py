"""
DDA LLM Guidance Utility
Pure functions for parsing and validating the Coordinate_Guidance JSON an
LLM_Auto_Label_Model returns for one image, turning it into the internal
detection model consumed by the LLM auto-label path.

Module contract: pure functions only — no boto3, no Pillow, no I/O.
Imports are limited to `json`, `math`, and `typing`.

Guidance wire format (Requirement 4.3):

    {"detections": [
      {"class": "scratch", "box": {"left": 12, "top": 30, "width": 40, "height": 25}},
      {"class": "dent",    "polygon": [[10, 20], [48, 22], [40, 60], [12, 55]]}
    ]}

Exactly one of `box` / `polygon` per detection. `{"detections": []}` is a
valid empty result (Requirement 4.9).

Internal detection model (validated, what parse_guidance returns):

    Detection = {
      'class': str,                  # exact Label_Set entry (trimmed)
      'geometry': 'box' | 'polygon',
      # geometry == 'box':
      'box': {'left': float, 'top': float, 'width': float, 'height': float},
      # geometry == 'polygon':
      'vertices': [(float, float), ...],   # >= POLYGON_MIN_VERTICES
    }

Requirements: 1.5, 2.6, 3.1, 3.2, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8,
4.9, 4.10, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Tuple

# Model identifiers are capped at 256 characters (Requirement 1.5)
MODEL_IDENTIFIER_MAX_LENGTH = 256

# Coordinate_Guidance supports 0-100 detections per image (Requirements 3.2, 4.7)
MAX_DETECTIONS = 100
# A polygon needs at least 3 vertices to enclose area (Requirement 4.6)
POLYGON_MIN_VERTICES = 3

GEOMETRY_BOX = 'box'
GEOMETRY_POLYGON = 'polygon'

_BOX_FIELDS = ('left', 'top', 'width', 'height')


class GuidanceError(Exception):
    """Coordinate_Guidance is unusable; the image is a generation failure."""


# ---------------------------------------------------------------------------
# Model identifier validation
# ---------------------------------------------------------------------------

def validate_model_identifier(identifier) -> Optional[str]:
    """
    Validate an LLM_Auto_Label_Model identifier (Requirement 1.5).

    Shared between the job creation API and the auto-label consumer so
    both sides agree on what a valid identifier is.

    Args:
        identifier: The candidate model identifier (any type)

    Returns:
        None when valid, else the reason it is not: required/non-empty,
        at most MODEL_IDENTIFIER_MAX_LENGTH characters, and free of
        whitespace and control characters
    """
    if not isinstance(identifier, str) or not identifier:
        return 'model identifier is required'
    if len(identifier) > MODEL_IDENTIFIER_MAX_LENGTH:
        return f'model identifier must be at most {MODEL_IDENTIFIER_MAX_LENGTH} characters'
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in identifier):
        return 'model identifier must not contain whitespace or control characters'
    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_detection_prompt(modality: str, label_set: List[str],
                           detection_prompt: str,
                           width: int, height: int,
                           per_label_prompts: Optional[Dict[str, str]] = None) -> str:
    """
    Build the model request prompt for one image (Requirement 3.1).

    One prompt shape serves all three modalities: it carries the pixel
    dimensions, the Label_Set, the Detection_Prompt inserted verbatim
    (no trimming, no escaping; Requirement 2.6), the per-label prompts
    inserted verbatim one section per label when supplied
    (skip-verification jobs; Requirement 2.6), and instructions
    demanding the ``{"detections": [...]}`` JSON with exactly one box or
    polygon per detection, ``{"detections": []}`` for nothing found, and
    the MAX_DETECTIONS cap (Requirement 3.2). Classification uses the
    same geometry instructions — the model still returns detections and
    guidance_to_prelabel reduces them to a label, keeping one prompt
    shape and one parser for all three modalities.

    Args:
        modality: 'Segmentation' | 'ObjectDetection' | 'Classification'
            (accepted for interface symmetry; the prompt is identical
            across modalities)
        label_set: The job's ordered class names
        detection_prompt: The Job_Creator's Detection_Prompt, inserted
            character-for-character
        width: Image width in pixels
        height: Image height in pixels
        per_label_prompts: Optional per-label guidance, each value
            inserted character-for-character

    Returns:
        The complete prompt text
    """
    lines = [
        'You are labeling images for a defect-detection dataset.',
        'Locate every object matching the detection request below and '
        'report its location in pixel coordinates.',
        f'The image is {width} pixels wide and {height} pixels tall; '
        f'every coordinate must lie within these bounds.',
        f"Allowed class names: {', '.join(label_set)}.",
        '',
        'Detection request:',
        detection_prompt,
    ]
    if per_label_prompts:
        lines.append('')
        for label, prompt in per_label_prompts.items():
            lines.append(f"Guidance for label '{label}': {prompt}")
    lines.extend([
        '',
        'Respond with ONLY a JSON object of the form '
        '{"detections": [{"class": ..., "box": '
        '{"left": ..., "top": ..., "width": ..., "height": ...}}, '
        '{"class": ..., "polygon": [[x, y], ...]}]}',
        'Give each detection exactly one "box" or one "polygon" '
        f'(at least {POLYGON_MIN_VERTICES} vertices).',
        'Use {"detections": []} when nothing matches. '
        f'Report at most {MAX_DETECTIONS} detections.',
    ])
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_first_json(text: str) -> Dict:
    """
    Extract the first parseable JSON object from model output text.

    Scans every '{' in reading order and attempts a raw decode at that
    position, returning the first candidate that decodes to a dict.
    Surrounding prose and code fences are tolerated because raw decoding
    ignores trailing content; a truncated leading object is skipped and
    the next candidate tried — "first parseable" in reading order
    (Requirement 4.1).

    Args:
        text: The model's raw response text

    Returns:
        The first successfully decoded JSON object

    Raises:
        GuidanceError: no parseable JSON object exists in the text
            (Requirement 4.2)
    """
    if not isinstance(text, str):
        raise GuidanceError('model output contains no parseable JSON object')
    decoder = json.JSONDecoder()
    for index, ch in enumerate(text):
        if ch != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise GuidanceError('model output contains no parseable JSON object')


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _finite_number(value) -> Optional[float]:
    """
    Return the value as a float when it is a finite, non-bool number,
    else None. bool is rejected before int since bool is an int subclass.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _validate_box(box, index: int, width: int, height: int) -> Dict[str, float]:
    """
    Validate one detection's box geometry (Requirement 4.5).

    Returns the box with all four fields as floats.

    Raises:
        GuidanceError: box is not a dict, a field is missing or
            non-numeric (or bool / non-finite), extent is non-positive,
            or the box extends outside the image's pixel bounds
    """
    if not isinstance(box, dict):
        raise GuidanceError(f'detection {index}: box must be an object')

    values: Dict[str, float] = {}
    for field in _BOX_FIELDS:
        if field not in box:
            raise GuidanceError(f'detection {index}: box is missing field {field!r}')
        number = _finite_number(box[field])
        if number is None:
            raise GuidanceError(
                f'detection {index}: box field {field!r} is not a finite number'
            )
        values[field] = number

    if values['width'] <= 0 or values['height'] <= 0:
        raise GuidanceError(
            f'detection {index}: box width and height must be positive'
        )
    if values['left'] < 0 or values['top'] < 0 \
            or values['left'] + values['width'] > width \
            or values['top'] + values['height'] > height:
        raise GuidanceError(
            f'detection {index}: box extends outside the image bounds '
            f'{width}x{height}'
        )
    return values


def _validate_polygon(polygon, index: int, width: int, height: int) -> List[tuple]:
    """
    Validate one detection's polygon geometry (Requirement 4.6).

    Returns the vertices as a list of (float, float) tuples.

    Raises:
        GuidanceError: polygon is not a sequence, has fewer than
            POLYGON_MIN_VERTICES vertices, a vertex is not a 2-element
            sequence of finite non-bool numbers, or a vertex lies
            outside the image's pixel bounds
    """
    if not isinstance(polygon, (list, tuple)):
        raise GuidanceError(f'detection {index}: polygon must be a list of vertices')
    if len(polygon) < POLYGON_MIN_VERTICES:
        raise GuidanceError(
            f'detection {index}: polygon has {len(polygon)} vertices; '
            f'at least {POLYGON_MIN_VERTICES} are required'
        )

    vertices: List[tuple] = []
    for v_index, vertex in enumerate(polygon):
        if not isinstance(vertex, (list, tuple)) or len(vertex) != 2:
            raise GuidanceError(
                f'detection {index}: polygon vertex {v_index} must be a '
                f'2-element [x, y] pair'
            )
        x = _finite_number(vertex[0])
        y = _finite_number(vertex[1])
        if x is None or y is None:
            raise GuidanceError(
                f'detection {index}: polygon vertex {v_index} coordinates '
                f'are not finite numbers'
            )
        if x < 0 or x > width or y < 0 or y > height:
            raise GuidanceError(
                f'detection {index}: polygon vertex {v_index} lies outside '
                f'the image bounds {width}x{height}'
            )
        vertices.append((x, y))
    return vertices


def _validate_detection(detection, index: int, label_set: List[str],
                        width: int, height: int) -> Dict:
    """
    Validate one detection entry into the internal Detection model.

    Raises:
        GuidanceError: the detection is not a dict, does not carry
            exactly one of box/polygon, its class is not a string or not
            an exact case-sensitive Label_Set member after trimming, or
            its geometry is malformed
    """
    if not isinstance(detection, dict):
        raise GuidanceError(f'detection {index}: not an object')

    has_box = GEOMETRY_BOX in detection
    has_polygon = GEOMETRY_POLYGON in detection
    if has_box == has_polygon:
        raise GuidanceError(
            f'detection {index}: exactly one of {GEOMETRY_BOX!r} or '
            f'{GEOMETRY_POLYGON!r} is required'
        )

    class_name = detection.get('class')
    if not isinstance(class_name, str):
        raise GuidanceError(f'detection {index}: class must be a string')
    trimmed = class_name.strip()
    if trimmed not in label_set:
        raise GuidanceError(
            f'detection {index}: class {trimmed!r} is not in the Label_Set'
        )

    if has_box:
        return {
            'class': trimmed,
            'geometry': GEOMETRY_BOX,
            'box': _validate_box(detection[GEOMETRY_BOX], index, width, height),
        }
    return {
        'class': trimmed,
        'geometry': GEOMETRY_POLYGON,
        'vertices': _validate_polygon(
            detection[GEOMETRY_POLYGON], index, width, height
        ),
    }


# ---------------------------------------------------------------------------
# Parsing and serialization
# ---------------------------------------------------------------------------

def parse_guidance(raw_text: str, label_set: List[str],
                   width: int, height: int) -> List[Dict]:
    """
    Parse and strictly validate a model response into Coordinate_Guidance.

    Extracts the first parseable JSON object from the raw text
    (Requirement 4.1), then validates: `detections` must be a list
    (Requirement 4.3); at most MAX_DETECTIONS entries, checked before
    per-detection validation so an oversized document reports the cap
    (Requirement 4.7); each detection must carry exactly one box or
    polygon geometry and a class name matching the Label_Set exactly
    (case-sensitive, after trimming; Requirements 4.3, 4.4); box and
    polygon coordinates must be finite non-bool numbers within the
    image's pixel bounds (Requirements 4.5, 4.6). Zero detections is a
    valid empty result (Requirement 4.9).

    Any rejection aborts the whole document with a single reason naming
    the offending element — no partial guidance (Requirement 4.8).

    Args:
        raw_text: The model's raw response text
        label_set: The job's ordered class names
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        Validated Detection dicts (see module docstring), in guidance order

    Raises:
        GuidanceError: extraction or validation failed
    """
    document = extract_first_json(raw_text)

    detections = document.get('detections')
    if not isinstance(detections, list):
        raise GuidanceError(
            "guidance JSON does not match the expected structure: "
            "'detections' must be a list"
        )
    if len(detections) > MAX_DETECTIONS:
        raise GuidanceError(
            f'guidance contains {len(detections)} detections; '
            f'at most {MAX_DETECTIONS} are allowed'
        )

    return [
        _validate_detection(detection, index, label_set, width, height)
        for index, detection in enumerate(detections)
    ]


def serialize_guidance(detections: List[Dict]) -> str:
    """
    Serialize a validated detection list back into the guidance wire
    format — the round-trip counterpart of parse_guidance
    (Requirement 4.10).

    Args:
        detections: Internal Detection dicts (see module docstring)

    Returns:
        JSON text of the form {"detections": [{"class", "box"|"polygon"}]}
    """
    entries = []
    for detection in detections:
        entry: Dict = {'class': detection['class']}
        if detection['geometry'] == GEOMETRY_BOX:
            entry[GEOMETRY_BOX] = {
                field: detection['box'][field] for field in _BOX_FIELDS
            }
        else:
            entry[GEOMETRY_POLYGON] = [
                [x, y] for x, y in detection['vertices']
            ]
        entries.append(entry)
    return json.dumps({'detections': entries})


# ---------------------------------------------------------------------------
# Coordinate space scaling (Sent_Dimensions -> Source_Dimensions)
# ---------------------------------------------------------------------------

def _scale_coordinate(value: float, source_extent: int,
                      sent_extent: int) -> float:
    """
    Map one coordinate from Sent space into Source space
    (Requirement 7.3).

    Round-half-up via floor(v + 0.5): Python's built-in round() is
    banker's rounding, which would map 2.5 to 2. Coordinates reaching
    this function are validated non-negative, so floor(v + 0.5) is
    exactly round-half-up here. The result is then clamped into
    [0, source_extent] (Requirement 7.4).

    Args:
        value: The coordinate in Sent space
        source_extent: The Source_Dimensions extent along this axis
        sent_extent: The Sent_Dimensions extent along this axis (positive)

    Returns:
        The coordinate in Source space, as a float in [0, source_extent]
    """
    scaled = math.floor(value * source_extent / sent_extent + 0.5)
    return float(min(source_extent, max(0, scaled)))


def scale_detections(detections: List[Dict],
                     sent_width: int, sent_height: int,
                     source_width: int, source_height: int) -> List[Dict]:
    """
    Map validated Coordinate_Guidance from Sent space into Source space,
    before any Pre_Label conversion (Requirement 7.3).

    Returns `detections` unchanged, as **the same list object**, when the
    two dimension pairs are equal or when either sent extent is not
    positive: no scaling, no rounding, no clamping, no float round trip,
    so the downstream Pre_Label is bit-for-bit the pre-feature Pre_Label
    (Requirement 7.5). That is a genuine early return, not a multiply
    by 1.0.

    Box detections have their two corners mapped — (left, top) and
    (left + width, top + height) — with the extents re-derived as the
    differences, so the transform applies to coordinates rather than to
    lengths and both corners land inside the source bounds. Polygon
    detections have every vertex mapped. Scaling is applied *before*
    conversion so guidance_to_prelabel stays untouched: Segmentation RLE
    is rasterized at the Source_Dimensions directly and there is never a
    mask to resample.

    The caller is responsible for calling this only for the geometry
    modalities — Classification carries no coordinates and must never
    reach this function (Requirement 7.8).

    Known edge case, intended and not a bug: a sub-pixel-extent box that
    passed validation in Sent space can map to a zero-extent box in
    Source space when the two spaces differ by a hair (e.g. 1001 vs
    1000). guidance_to_prelabel then raises its existing GuidanceError
    ("detection N ('c') converts to a bounding box with zero width or
    height"), which reaches the caller as the pre-existing unusable
    model output category with a pre-existing reason string — no new
    category and no new reason (Requirements 9.3, 9.6).

    Args:
        detections: Internal Detection dicts (see module docstring), in
            Sent space; left unmodified
        sent_width: Sent_Dimensions width in pixels
        sent_height: Sent_Dimensions height in pixels
        source_width: Source_Dimensions width in pixels
        source_height: Source_Dimensions height in pixels

    Returns:
        The same list object when no mapping applies, else new Detection
        dicts in Source space, in guidance order
    """
    if (sent_width, sent_height) == (source_width, source_height):
        return detections
    if sent_width < 1 or sent_height < 1:
        return detections

    scaled: List[Dict] = []
    for detection in detections:
        if detection['geometry'] == GEOMETRY_BOX:
            box = detection['box']
            left = _scale_coordinate(box['left'], source_width, sent_width)
            top = _scale_coordinate(box['top'], source_height, sent_height)
            right = _scale_coordinate(
                box['left'] + box['width'], source_width, sent_width
            )
            bottom = _scale_coordinate(
                box['top'] + box['height'], source_height, sent_height
            )
            scaled.append({
                'class': detection['class'],
                'geometry': GEOMETRY_BOX,
                'box': {
                    'left': left,
                    'top': top,
                    'width': right - left,
                    'height': bottom - top,
                },
            })
        else:
            scaled.append({
                'class': detection['class'],
                'geometry': GEOMETRY_POLYGON,
                'vertices': [
                    (_scale_coordinate(x, source_width, sent_width),
                     _scale_coordinate(y, source_height, sent_height))
                    for x, y in detection['vertices']
                ],
            })
    return scaled


# ---------------------------------------------------------------------------
# Rasterization to RLE
# ---------------------------------------------------------------------------

def _pixel_range(start: float, end: float, limit: int) -> Tuple[int, int]:
    """
    Convert a half-open coordinate interval [start, end) into the
    half-open range of pixel indices whose centers fall inside it,
    clamped to [0, limit).

    A pixel index i is selected when start <= i + 0.5 < end, i.e.
    i >= start - 0.5 and i < end - 0.5, giving
    [ceil(start - 0.5), ceil(end - 0.5)).
    """
    first = max(0, math.ceil(start - 0.5))
    last = min(limit, math.ceil(end - 0.5))
    return first, last


def _box_spans(box: Dict[str, float], width: int,
               height: int) -> Dict[int, List[Tuple[int, int]]]:
    """
    Per-column half-open y-spans of a filled rectangle under
    pixel-center containment: pixel (x, y) is filled when
    (x + 0.5, y + 0.5) lies inside the rectangle. Cost O(box width).
    """
    x0, x1 = _pixel_range(box['left'], box['left'] + box['width'], width)
    y0, y1 = _pixel_range(box['top'], box['top'] + box['height'], height)
    if x1 <= x0 or y1 <= y0:
        return {}
    return {x: [(y0, y1)] for x in range(x0, x1)}


def _column_intersections(vertices: List[tuple], cx: float) -> List[float]:
    """
    Intersection y values of the vertical line x = cx with every polygon
    edge, under the half-open crossing rule (an edge is crossed when
    min(xa, xb) <= cx < max(xa, xb)), so each vertex crossing is counted
    exactly once and the intersection count is always even. Horizontal
    edges (xa == xb) never cross.
    """
    ys: List[float] = []
    count = len(vertices)
    for i in range(count):
        xa, ya = vertices[i]
        xb, yb = vertices[(i + 1) % count]
        if xa == xb:
            continue
        if (xa <= cx < xb) or (xb <= cx < xa):
            t = (cx - xa) / (xb - xa)
            ys.append(ya + t * (yb - ya))
    return ys


def _polygon_spans(vertices: List[tuple], width: int,
                   height: int) -> Dict[int, List[Tuple[int, int]]]:
    """
    Per-column half-open y-spans of a polygon's filled interior under
    the even-odd rule with pixel-center sampling. For each column x in
    the vertex bounding box the vertical line x + 0.5 is intersected
    with every edge; sorted intersections pair into (ya, yb) intervals
    whose covered pixel rows have centers in [ya, yb).
    Cost O(columns x edges).
    """
    min_x = min(v[0] for v in vertices)
    max_x = max(v[0] for v in vertices)
    x0 = max(0, math.ceil(min_x - 0.5))
    x1 = min(width, math.floor(max_x - 0.5) + 1)

    spans: Dict[int, List[Tuple[int, int]]] = {}
    for x in range(x0, x1):
        ys = _column_intersections(vertices, x + 0.5)
        if not ys:
            continue
        ys.sort()
        column: List[Tuple[int, int]] = []
        for i in range(0, len(ys) - 1, 2):
            y0, y1 = _pixel_range(ys[i], ys[i + 1], height)
            if y1 > y0:
                column.append((y0, y1))
        if column:
            spans[x] = column
    return spans


def _merge_spans(column: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping or touching half-open spans, ascending."""
    merged: List[Tuple[int, int]] = []
    for y0, y1 in sorted(column):
        if merged and y0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
        else:
            merged.append((y0, y1))
    return merged


def rasterize_to_rle(detection: Dict, width: int, height: int) -> str:
    """
    Rasterize one detection's filled interior into COCO-style
    column-major RLE counts (the dda_manifest.rle_encode format:
    space-separated run lengths starting with the background count).

    Spans are computed only where geometry exists and the RLE is
    emitted directly from them — no dense width*height mask is ever
    allocated (Requirements 5.1, 5.2). Pixels are selected by
    pixel-center containment; polygons are filled under the even-odd
    rule. Columns are clamped to [0, width) and spans to [0, height),
    so emitted geometry is in-bounds by construction (Requirement 5.6),
    and the counts always sum to exactly width * height — the invariant
    dda_manifest.rle_decode enforces.

    Geometry covering no pixel center yields an all-background RLE
    (zero foreground); callers treat that as a conversion failure.

    Args:
        detection: Internal Detection dict (see module docstring)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        Space-separated column-major RLE counts, background first

    Raises:
        ValueError: width or height is not positive
    """
    if width < 1 or height < 1:
        raise ValueError('width and height must be positive')

    if detection['geometry'] == GEOMETRY_BOX:
        spans = _box_spans(detection['box'], width, height)
    else:
        spans = _polygon_spans(detection['vertices'], width, height)

    counts: List[int] = []
    cursor = 0  # column-major pixel index already emitted
    for x in sorted(spans):
        for y0, y1 in _merge_spans(spans[x]):
            absolute = x * height + y0
            background = absolute - cursor
            if background == 0 and counts:
                # Foreground contiguous with the previous run in
                # column-major order (span touching across columns):
                # extend it, keeping runs strictly advancing.
                counts[-1] += y1 - y0
            else:
                counts.append(background)      # background run
                counts.append(y1 - y0)         # foreground run
            cursor = x * height + y1
    remaining = width * height - cursor
    if remaining or not counts:
        counts.append(remaining)               # closing background run
    return ' '.join(str(c) for c in counts)


# ---------------------------------------------------------------------------
# Modality conversion to Pre_Labels
# ---------------------------------------------------------------------------

MODALITY_CLASSIFICATION = 'Classification'
MODALITY_SEGMENTATION = 'Segmentation'
MODALITY_OBJECT_DETECTION = 'ObjectDetection'

# Fixed binary Label_Set for Classification jobs (Requirement 5.4)
CLASSIFICATION_NORMAL = 'normal'
CLASSIFICATION_ANOMALY = 'anomaly'


def polygon_bounding_box(vertices: List[tuple]) -> Dict[str, float]:
    """
    Axis-aligned hull of a polygon, as a box dict (Requirement 5.3):
    left = min x, top = min y, width = max x - min x,
    height = max y - min y.

    Args:
        vertices: (x, y) pairs (validated, in-bounds)

    Returns:
        {'left', 'top', 'width', 'height'} floats spanning the vertex
        minima and maxima
    """
    min_x = min(v[0] for v in vertices)
    max_x = max(v[0] for v in vertices)
    min_y = min(v[1] for v in vertices)
    max_y = max(v[1] for v in vertices)
    return {
        'left': min_x,
        'top': min_y,
        'width': max_x - min_x,
        'height': max_y - min_y,
    }


def _is_zero_foreground(rle: str) -> bool:
    """
    True when the RLE carries no foreground pixel. rasterize_to_rle
    emits a single all-background count for geometry covering no pixel
    center, so a lone count means zero spans.
    """
    return len(rle.split()) == 1


def guidance_to_prelabel(detections: List[Dict], modality: str,
                         label_set: List[str],
                         width: int, height: int) -> Dict:
    """
    Convert validated Coordinate_Guidance into the modality Pre_Label,
    in exactly the shape the existing auto-label paths already write to
    the artifacts bucket — downstream code cannot distinguish origin.

    Segmentation (Requirements 5.1, 5.2): one RLE region per detection,
    in guidance order, never merged across detections sharing a class:
    ``{'modality': 'Segmentation', 'regions': [{'class', 'rle'}],
    'image_width', 'image_height'}``.

    ObjectDetection (Requirement 5.3): one box per detection; box
    detections keep their validated coordinates verbatim, polygon
    detections collapse to their axis-aligned hull
    (polygon_bounding_box): ``{'modality': 'ObjectDetection',
    'boxes': [{'class', 'left', 'top', 'width', 'height'}],
    'image_width', 'image_height'}``.

    Classification (Requirement 5.4): ``{'modality': 'Classification',
    'label': 'anomaly'|'normal'}`` against the fixed binary Label_Set —
    'anomaly' when one or more detections, 'normal' when zero.

    Zero detections in Segmentation/ObjectDetection emit an empty
    regions/boxes list and are a success, not a failure
    (Requirement 5.5). Emitted geometry is in-bounds by construction:
    the rasterizer clamps to the frame and validated boxes/hulls never
    exceed it (Requirement 5.6).

    Args:
        detections: Internal Detection dicts (see module docstring)
        modality: 'Segmentation' | 'ObjectDetection' | 'Classification'
        label_set: The job's ordered class names (unused for geometry
            modalities; the detections already carry validated classes)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        The modality Pre_Label dict

    Raises:
        GuidanceError: a detection rasterizes to zero spans
            (all-background RLE), or a box's int()-truncated width or
            height is below 1 — _serialize_object_detection truncates
            with int(), so a sub-pixel box would otherwise reach the
            manifest with zero extent (Requirement 5.7)
        ValueError: modality is not one of the three supported values
    """
    if modality == MODALITY_CLASSIFICATION:
        label = CLASSIFICATION_ANOMALY if detections else CLASSIFICATION_NORMAL
        return {'modality': MODALITY_CLASSIFICATION, 'label': label}

    if modality == MODALITY_SEGMENTATION:
        regions = []
        for index, detection in enumerate(detections):
            rle = rasterize_to_rle(detection, width, height)
            if _is_zero_foreground(rle):
                raise GuidanceError(
                    f'detection {index} ({detection["class"]!r}) covers no '
                    f'pixel and cannot become a mask region'
                )
            regions.append({'class': detection['class'], 'rle': rle})
        return {
            'modality': MODALITY_SEGMENTATION,
            'regions': regions,
            'image_width': width,
            'image_height': height,
        }

    if modality == MODALITY_OBJECT_DETECTION:
        boxes = []
        for index, detection in enumerate(detections):
            if detection['geometry'] == GEOMETRY_BOX:
                box = detection['box']
            else:
                box = polygon_bounding_box(detection['vertices'])
            # _serialize_object_detection truncates with int(); a
            # sub-pixel extent would reach the manifest as zero.
            if int(box['width']) < 1 or int(box['height']) < 1:
                raise GuidanceError(
                    f'detection {index} ({detection["class"]!r}) converts '
                    f'to a bounding box with zero width or height'
                )
            boxes.append({
                'class': detection['class'],
                'left': box['left'],
                'top': box['top'],
                'width': box['width'],
                'height': box['height'],
            })
        return {
            'modality': MODALITY_OBJECT_DETECTION,
            'boxes': boxes,
            'image_width': width,
            'image_height': height,
        }

    raise ValueError(f'unsupported modality {modality!r}')
