#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Detection_List surfacing for the workflow engine (Requirements 1.1-1.5,
1.7-1.9).

The Marshal_Model (``marshal_for_capture_template.py``) emits, for
object-detection models, a capture-record output block of
``observedContentType: json_with_base64_encoding`` whose decoded payload
carries a top-level ``detections`` map::

    {"detections": {"0": {"class_index": 0, "class_label": "blue box",
                           "bounding_box": [x_min, y_min, x_max, y_max],
                           "confidence": 0.42}, ...}}

with boxes in **source-frame pixel coordinates**. The em-agent broker's
file-target routing lands the record at ``{output_dir}/{capture_id}.jsonl``
— the same contract ``run_artifacts`` already parses for masks. Reading
that record back post-pipeline surfaces per-detection results without any
GStreamer or proprietary-plugin change (Requirement 1.7).

These helpers are best-effort and contained: a missing/malformed record or
an absent detections block yields ``None`` (the Run_Metadata is left
unchanged, Requirement 1.8) rather than an error; an **empty** detections
map yields an empty Detection_List so conditions can distinguish "ran with
no detections" from "no detection model in the graph" (Requirement 1.5).

Detection_IDs are random (``uuid4().hex[:8]``), unique within the run, and
never derived from list position (Requirement 1.3): they are assigned in
raw-record order *before* sorting, so re-sorting the same raw detections
permutes entries but never re-labels one (design Property 2).
"""

import base64
import json
import logging
import os
import random
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Artifact filename suffix of the run's capture record under
#: ``output_dir`` (mirrors ``run_artifacts._JSONL_SUFFIX``).
_JSONL_SUFFIX = ".jsonl"

#: Filename suffix of the marshal-written detections sidecar (design Risk
#: 1 fallback): the capture record only lands when a terminal capture
#: node's broker file targets exist, so for capture-node-less graphs the
#: Marshal_Model persists ``{output_dir}/{capture_id}.detections.json``
#: directly, carrying the same ``{"detections": {...}}`` payload the
#: record's base64 block encodes.
_SIDECAR_SUFFIX = ".detections.json"

#: Content-type marker of the base64-JSON label block inside the capture
#: record (mirrors ``utils.constants.INFERENCE_OUTPUT_RES_LABEL_CONTENT_TYPE``;
#: duplicated as a small constant so this module stays importable without
#: the wider ``utils`` stack, matching ``run_artifacts``).
_RES_LABEL_CONTENT_TYPE = "json_with_base64_encoding"

#: The discriminator key: a ``json_with_base64_encoding`` payload with a
#: top-level ``detections`` map is an object-detection label block (the
#: same discriminator ``inference_results_utils.is_detection_model_output_result``
#: uses; segmentation payloads carry ``anomalies`` instead).
_DETECTIONS_KEY = "detections"

#: Node type whose ``detection_sort_order`` parameter configures the
#: Detection_Sort_Order (read from the registration's ``workflow.json``).
_MODEL_INFERENCE_NODE_TYPE = "model_inference"
_SORT_ORDER_PARAMETER = "detection_sort_order"

#: Detection_Sort_Order values (Requirement 1.4). Orders are computed on
#: bounding-box centers; ties break on the orthogonal axis ascending
#: (confidence ties by ``left_to_right``).
SORT_LEFT_TO_RIGHT = "left_to_right"
SORT_RIGHT_TO_LEFT = "right_to_left"
SORT_TOP_TO_BOTTOM = "top_to_bottom"
SORT_BOTTOM_TO_TOP = "bottom_to_top"
SORT_CONFIDENCE_DESC = "confidence_desc"

DEFAULT_SORT_ORDER = SORT_LEFT_TO_RIGHT

SORT_ORDERS = (
    SORT_LEFT_TO_RIGHT,
    SORT_RIGHT_TO_LEFT,
    SORT_TOP_TO_BOTTOM,
    SORT_BOTTOM_TO_TOP,
    SORT_CONFIDENCE_DESC,
)

#: Run_Metadata keys the merge produces (Requirements 1.1, 1.9). TAG
#: messages never produce these names, but the merge still refuses to
#: overwrite an existing key (never-overwrite semantics).
METADATA_KEY_DETECTIONS = "detections"
METADATA_KEY_DETECTION_COUNT = "detection_count"

#: Key under which the built Detection_List is cached on the run state, so
#: every consumer in one run (bridge pump, Bedrock crops, persisted
#: metadata) sees the same entries with the same Detection_IDs (design
#: Property 1).
CACHE_KEY_DETECTIONS = "detections"


def read_detections(
    output_dir: Optional[str], capture_id: Optional[str]
) -> Optional[List[dict]]:
    """The raw per-detection entries from the run's capture record, or
    ``None`` (Requirements 1.1, 1.7, 1.8).

    Parses ``{output_dir}/{capture_id}.jsonl`` (one capture record per
    single-frame run; the last non-empty line, mirroring
    ``run_artifacts._mask_from_jsonl``) and locates the
    ``json_with_base64_encoding`` output block whose decoded payload
    carries a top-level ``detections`` map — the same discriminator
    ``inference_results_utils`` uses to identify object-detection captures.

    Returns the map's entries as a list in record order (numeric-key
    order), an empty list when the map is present but empty (Requirement
    1.5), and ``None`` when no record or no detections block exists (e.g.
    a non-detection model). Best-effort and contained: a malformed record
    yields ``None``, logged, never an error (Requirement 1.8).

    Two sources, capture record first: when the ``.jsonl`` capture record
    carries a detections block it wins (byte-identical behavior for
    capture-node workflows); when the record is absent or has no
    detections block, the marshal-written sidecar
    ``{output_dir}/{capture_id}.detections.json`` is read instead (design
    Risk 1 fallback — graphs without a capture node never route the
    broker file targets that land the record). Same containment: a
    malformed sidecar yields ``None``, logged.
    """
    if not output_dir or not capture_id:
        return None
    entries = _read_from_capture_record(output_dir, capture_id)
    if entries is not None:
        return entries
    return _read_from_sidecar(output_dir, capture_id)


def _read_from_capture_record(
    output_dir: str, capture_id: str
) -> Optional[List[dict]]:
    """The detections entries from ``{output_dir}/{capture_id}.jsonl``, or
    ``None`` when no record or no detections block exists (unchanged
    pre-sidecar behavior)."""
    path = os.path.join(output_dir, "{0}{1}".format(capture_id, _JSONL_SUFFIX))
    try:
        with open(path, "r") as jsonl_file:
            lines = [line for line in jsonl_file.read().splitlines() if line.strip()]
        if not lines:
            return None
        # The run writes one capture record; use the last non-empty line.
        record = json.loads(lines[-1])
        outputs = record.get("deviceFleetAuxiliaryOutputs") or []
    except Exception:  # noqa: BLE001 - best-effort, never fail the run
        logger.debug(
            "Could not read capture record at %s", path, exc_info=True
        )
        return None

    detections_map = _detections_map_from_outputs(outputs)
    if detections_map is None:
        return None
    return [detections_map[key] for key in _ordered_keys(detections_map)]


def _read_from_sidecar(
    output_dir: str, capture_id: str
) -> Optional[List[dict]]:
    """The detections entries from the marshal-written sidecar
    ``{output_dir}/{capture_id}.detections.json``, or ``None``.

    The sidecar carries the exact payload the capture record's base64
    block encodes: ``{"detections": {"0": {...}, ...}}``. Same contract as
    the record path: entries in numeric-key order, ``[]`` for an empty
    map, ``None`` when the file is absent, malformed, or does not carry a
    ``detections`` map (best-effort, logged, Requirement 1.8)."""
    path = os.path.join(
        output_dir, "{0}{1}".format(capture_id, _SIDECAR_SUFFIX)
    )
    try:
        with open(path, "r") as sidecar_file:
            payload = json.load(sidecar_file)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - best-effort, never fail the run
        logger.debug(
            "Could not read detections sidecar at %s", path, exc_info=True
        )
        return None
    if not isinstance(payload, dict) or not isinstance(
        payload.get(_DETECTIONS_KEY), dict
    ):
        logger.debug(
            "Detections sidecar at %s does not carry a detections map", path
        )
        return None
    detections_map = payload[_DETECTIONS_KEY]
    return [detections_map[key] for key in _ordered_keys(detections_map)]


def _detections_map_from_outputs(outputs: list) -> Optional[Dict[str, Any]]:
    """The decoded ``detections`` map from a capture record's outputs, or
    ``None`` when no block carries one."""
    if not isinstance(outputs, list):
        return None
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        if entry.get("observedContentType") != _RES_LABEL_CONTENT_TYPE:
            continue
        data = entry.get("data")
        if not data:
            continue
        try:
            payload = json.loads(base64.b64decode(data))
        except Exception:  # noqa: BLE001 - malformed block -> keep looking
            logger.debug(
                "Could not decode a %s capture-record block",
                _RES_LABEL_CONTENT_TYPE,
                exc_info=True,
            )
            continue
        if isinstance(payload, dict) and isinstance(
            payload.get(_DETECTIONS_KEY), dict
        ):
            return payload[_DETECTIONS_KEY]
    return None


def _ordered_keys(detections_map: Dict[str, Any]) -> List[str]:
    """The map's keys in record order: numeric keys ascending (the marshal
    emits "0", "1", ...), any non-numeric stragglers after, lexicographic."""
    return sorted(
        detections_map,
        key=lambda key: (0, int(key), "") if str(key).isdigit() else (1, 0, str(key)),
    )


def build_detection_list(
    raw: List[dict],
    sort_order: str,
    rng: Optional[random.Random] = None,
) -> List[dict]:
    """Normalize raw marshal entries into the Detection_List (Requirements
    1.2, 1.3, 1.4).

    Each entry becomes ``{id, label, confidence, x_min, y_min, x_max,
    y_max}`` with float coordinates in source-frame pixels. Detection_IDs
    (``uuid4().hex[:8]``, re-drawn on intra-run collision) are assigned in
    raw order *before* sorting, so an ID never derives from list position
    (Requirement 1.3). The list is then ordered by ``sort_order`` on
    bounding-box centers with the design's tie-breaks; an unknown order
    falls back to the default, logged.

    ``rng`` optionally injects a deterministic random source (tests only);
    the default draws from ``uuid4``. Entries that cannot be normalized
    (missing/malformed box, label, or confidence) are skipped with a
    warning — best-effort containment, never a failure.
    """
    entries: List[dict] = []
    used_ids: set = set()
    for raw_entry in raw or []:
        normalized = _normalize_entry(raw_entry)
        if normalized is None:
            logger.warning(
                "Skipping malformed detection entry in capture record: %r",
                raw_entry,
            )
            continue
        normalized["id"] = _draw_detection_id(used_ids, rng)
        entries.append(normalized)
    return sort_detection_list(entries, sort_order)


def sort_detection_list(entries: List[dict], sort_order: str) -> List[dict]:
    """A new list of ``entries`` ordered by the Detection_Sort_Order on
    bounding-box centers (Requirement 1.4); tie-breaks per the design.
    An unknown ``sort_order`` sorts by the default, logged."""
    if sort_order not in SORT_ORDERS:
        logger.warning(
            "Unknown detection_sort_order %r; using default %r",
            sort_order,
            DEFAULT_SORT_ORDER,
        )
        sort_order = DEFAULT_SORT_ORDER
    return sorted(entries, key=_SORT_KEYS[sort_order])


def _center(entry: dict) -> tuple:
    return (
        (entry["x_min"] + entry["x_max"]) / 2.0,
        (entry["y_min"] + entry["y_max"]) / 2.0,
    )


def _key_left_to_right(entry: dict) -> tuple:
    center_x, center_y = _center(entry)
    return (center_x, center_y)


def _key_right_to_left(entry: dict) -> tuple:
    center_x, center_y = _center(entry)
    return (-center_x, center_y)


def _key_top_to_bottom(entry: dict) -> tuple:
    center_x, center_y = _center(entry)
    return (center_y, center_x)


def _key_bottom_to_top(entry: dict) -> tuple:
    center_x, center_y = _center(entry)
    return (-center_y, center_x)


def _key_confidence_desc(entry: dict) -> tuple:
    center_x, center_y = _center(entry)
    return (-entry["confidence"], center_x, center_y)


_SORT_KEYS = {
    SORT_LEFT_TO_RIGHT: _key_left_to_right,
    SORT_RIGHT_TO_LEFT: _key_right_to_left,
    SORT_TOP_TO_BOTTOM: _key_top_to_bottom,
    SORT_BOTTOM_TO_TOP: _key_bottom_to_top,
    SORT_CONFIDENCE_DESC: _key_confidence_desc,
}


def _normalize_entry(raw_entry: Any) -> Optional[dict]:
    """One raw marshal entry as a Detection_List record (sans ``id``), or
    ``None`` when the entry cannot be normalized."""
    if not isinstance(raw_entry, dict):
        return None
    box = raw_entry.get("bounding_box")
    confidence = raw_entry.get("confidence")
    label = raw_entry.get("class_label")
    if (
        not isinstance(box, (list, tuple))
        or len(box) != 4
        or not all(_is_number(value) for value in box)
        or not _is_number(confidence)
    ):
        return None
    return {
        "label": str(label) if label is not None else "",
        "confidence": float(confidence),
        "x_min": float(box[0]),
        "y_min": float(box[1]),
        "x_max": float(box[2]),
        "y_max": float(box[3]),
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _draw_detection_id(used_ids: set, rng: Optional[random.Random]) -> str:
    """A random 8-hex-char Detection_ID, re-drawn until unique within the
    run's list (Requirement 1.3)."""
    while True:
        if rng is None:
            detection_id = uuid.uuid4().hex[:8]
        else:
            detection_id = "{0:08x}".format(rng.getrandbits(32))
        if detection_id not in used_ids:
            used_ids.add(detection_id)
            return detection_id


def resolve_sort_order(graph_document: Optional[dict]) -> str:
    """The configured Detection_Sort_Order from the registration's
    ``workflow.json`` graph document (Requirement 1.4).

    ``model_inference`` compiles to GStreamer elements (no executor
    binding), so the compiled document does not carry its parameters; the
    executor reads ``detection_sort_order`` from the graph document's
    ``model_inference`` node instead (the ``run_artifacts.read_workflow_graph``
    document shape: ``{"nodes": [{"id", "type", "parameters", ...}]}``).

    Defaults to ``left_to_right`` when the document, node, or parameter is
    absent; an unknown value falls back to the default, logged.
    """
    if not isinstance(graph_document, dict):
        return DEFAULT_SORT_ORDER
    nodes = graph_document.get("nodes")
    if not isinstance(nodes, list):
        return DEFAULT_SORT_ORDER
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") != _MODEL_INFERENCE_NODE_TYPE:
            continue
        parameters = node.get("parameters")
        value = parameters.get(_SORT_ORDER_PARAMETER) if isinstance(
            parameters, dict
        ) else None
        if value is None:
            return DEFAULT_SORT_ORDER
        if value not in SORT_ORDERS:
            logger.warning(
                "Unknown detection_sort_order %r on node %r; using default %r",
                value,
                node.get("id"),
                DEFAULT_SORT_ORDER,
            )
            return DEFAULT_SORT_ORDER
        return value
    return DEFAULT_SORT_ORDER


def merge_detections(
    tag_values: dict,
    output_dir: Optional[str],
    capture_id: Optional[str],
    graph_document: Optional[dict],
    cache: dict,
) -> Optional[List[dict]]:
    """Merge the run's Detection_List into the Run_Metadata (Requirements
    1.1, 1.5, 1.8, 1.9).

    The list is built exactly once per run: the first successful build is
    cached on ``cache`` (the executor's run state, under
    ``CACHE_KEY_DETECTIONS``) and every later call — including the bridge
    pump's poll — reuses the cached entries, so all consumers see the same
    Detection_IDs (design Property 1).

    Merges ``detections`` and ``detection_count`` into ``tag_values``
    without overwriting existing keys. When no record or detections block
    exists (or the record is malformed), the metadata and cache are left
    unchanged and ``None`` is returned (Requirement 1.8); an empty
    detections map merges an empty list with count 0 (Requirement 1.5).

    Returns the run's built Detection_List, or ``None``.
    """
    if CACHE_KEY_DETECTIONS in cache:
        detection_list = cache[CACHE_KEY_DETECTIONS]
    else:
        raw = read_detections(output_dir, capture_id)
        if raw is None:
            return None
        detection_list = build_detection_list(
            raw, resolve_sort_order(graph_document)
        )
        cache[CACHE_KEY_DETECTIONS] = detection_list

    tag_values.setdefault(METADATA_KEY_DETECTIONS, detection_list)
    tag_values.setdefault(
        METADATA_KEY_DETECTION_COUNT, len(detection_list)
    )
    return detection_list
