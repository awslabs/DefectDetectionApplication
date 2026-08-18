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

"""Run-artifact resolution for the deployed-workflow run-observability API
(Requirements 4.1, 4.2, 5.7).

A run's artifacts land under ``output_dir`` prefixed with the run's
``capture_id`` (design §2)::

    {output_dir}/{capture_id}.jpg          # base captured frame
    {output_dir}/{capture_id}.overlay.jpg  # overlay (when produced)
    {output_dir}/{capture_id}.mask.png     # mask (when produced)
    {output_dir}/{capture_id}.jsonl        # result record (when produced)

These helpers locate the base image, tell whether an overlay/mask artifact
exists, and derive the mask as a base64 string + background color from the
run's result ``.jsonl`` (falling back to the raw ``.mask.png`` bytes). The
base64-mask + background shape is exactly what the existing on-device
overlay pipeline consumes (``getMaskImageProp`` / ``setupMaskImage``), so
the results view reuses those frontend components unchanged (design §5.1).

The mask/background derivation mirrors
``utils.inference_results_utils`` (the marshal capture-record contract) but
is self-contained and best-effort: any missing/malformed artifact yields a
null mask rather than an error, so the endpoints never 500 on partial
output (R5.7).
"""

import base64
import json
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Artifact filename suffixes, appended to the run's ``capture_id`` under
#: ``output_dir`` (design §2). The message-broker ``file-target_`` routing
#: the executor writes yields ``{capture_id}.{ext}`` files.
_BASE_IMAGE_SUFFIX = ".jpg"
_OVERLAY_SUFFIX = ".overlay.jpg"
_MASK_SUFFIX = ".mask.png"
_JSONL_SUFFIX = ".jsonl"
_METADATA_SUFFIX = ".json"

#: Inference-node frame artifacts, written by
#: ``pipeline_executor._persist_node_frames`` as
#: ``{capture_id}.node.{sanitized_nodeId}.{port}.jpg``. The node id is
#: sanitized to ``[A-Za-z0-9_.-]`` there; port names come from the node
#: descriptor's input ports and carry no dots, so the ``{nodeId}.{port}``
#: tail splits unambiguously on its LAST dot.
_NODE_IMAGE_MARKER = ".node."
_NODE_IMAGE_SUFFIX = ".jpg"

#: Presentation order for known ports: the inspected frame before the
#: reference frame, matching the order the frames are sent to the model.
#: Ports outside this tuple sort after it, alphabetically — the listing
#: carries no port-name allow-list (design §7).
_PORT_PRESENTATION_ORDER = ("in", "reference")

#: Content-type markers inside the run's result ``.jsonl`` (mirrors
#: ``utils.constants.INFERENCE_OUTPUT_MASK_CONTENT_TYPE_PREFIX`` /
#: ``INFERENCE_OUTPUT_RES_LABEL_CONTENT_TYPE``). Duplicated here as small
#: constants so this module stays importable without the wider ``utils``
#: stack the DAO/em-agent layer pulls in.
_MASK_CONTENT_TYPE_PREFIX = "mask"
_RES_LABEL_CONTENT_TYPE = "json_with_base64_encoding"

_FILE_URI_PREFIX = "file://"


def _artifact_path(output_dir: str, capture_id: str, suffix: str) -> str:
    return os.path.join(output_dir, "{0}{1}".format(capture_id, suffix))


def base_output_image_path(
    output_dir: Optional[str], capture_id: Optional[str]
) -> Optional[str]:
    """The run's base captured image on disk, or ``None``.

    Prefers ``{capture_id}.jpg``; falls back to any produced ``.jpg`` in
    ``output_dir`` that is not the overlay artifact (``*.overlay.jpg``), so
    a run whose base frame landed under a slightly different name still
    serves. Returns ``None`` when the directory is missing/empty or holds
    no base image."""
    if not output_dir or not capture_id:
        return None
    primary = _artifact_path(output_dir, capture_id, _BASE_IMAGE_SUFFIX)
    if os.path.isfile(primary):
        return primary
    try:
        names = sorted(os.listdir(output_dir))
    except OSError:
        return None
    for name in names:
        lowered = name.lower()
        if lowered.endswith(".jpg") and not lowered.endswith(".overlay.jpg"):
            candidate = os.path.join(output_dir, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def overlay_artifact_exists(
    output_dir: Optional[str], capture_id: Optional[str]
) -> bool:
    """True when an overlay (``.overlay.jpg``) or mask (``.mask.png``)
    artifact file exists for the run (drives ``hasOverlay``)."""
    if not output_dir or not capture_id:
        return False
    for suffix in (_OVERLAY_SUFFIX, _MASK_SUFFIX):
        if os.path.isfile(_artifact_path(output_dir, capture_id, suffix)):
            return True
    return False


def _port_sort_key(port: str) -> Tuple[int, str]:
    """Sort key placing known ports in presentation order and any other
    port after them, alphabetically."""
    if port in _PORT_PRESENTATION_ORDER:
        return (_PORT_PRESENTATION_ORDER.index(port), "")
    return (len(_PORT_PRESENTATION_ORDER), port)


def list_node_images(
    output_dir: Optional[str], capture_id: Optional[str]
) -> list:
    """The run's persisted inference-node frames as
    ``[{'nodeId': ..., 'port': ...}]`` (Requirement 4.3).

    Keys purely on the ``{capture_id}.node.{nodeId}.{port}.jpg`` filename
    pattern written by ``pipeline_executor._persist_node_frames`` — no
    node-type and no port-name allow-list — so ``bedrock_inference`` and
    ``llm_inference`` nodes surface identically and a future third port
    needs no change here.

    Entries are sorted deterministically: by node id, then by port with
    ``in`` before ``reference`` (invocation order), any other port after
    those alphabetically.

    Best-effort and contained (matching this module's other helpers): a
    ``None``/empty ``output_dir``/``capture_id``, a missing directory, or
    an unreadable listing all yield ``[]`` rather than raising, so the
    endpoints never 500 on partial output."""
    if not output_dir or not capture_id:
        return []
    prefix = "{0}{1}".format(capture_id, _NODE_IMAGE_MARKER)
    try:
        names = os.listdir(output_dir)
    except OSError:
        logger.debug(
            "Could not list node images in %s", output_dir, exc_info=True
        )
        return []
    entries = []
    for name in names:
        if not name.startswith(prefix) or not name.endswith(
            _NODE_IMAGE_SUFFIX
        ):
            continue
        tail = name[len(prefix):-len(_NODE_IMAGE_SUFFIX)]
        node_id, _, port = tail.rpartition(".")
        if not node_id or not port:
            continue
        if not os.path.isfile(os.path.join(output_dir, name)):
            continue
        entries.append({"nodeId": node_id, "port": port})
    entries.sort(key=lambda e: (e["nodeId"], _port_sort_key(e["port"])))
    return entries


def node_image_path(
    output_dir: Optional[str],
    capture_id: Optional[str],
    node_id: Optional[str],
    port: Optional[str],
) -> Optional[str]:
    """The on-disk path of the run's node frame for ``(node_id, port)``, or
    ``None`` (Requirement 4.3).

    Resolves only pairs that :func:`list_node_images` actually reports, so
    traversal shapes (``../``) and fabricated node/port names yield
    ``None`` by construction rather than escaping ``output_dir``.
    Best-effort and contained: never raises."""
    if not output_dir or not capture_id or not node_id or not port:
        return None
    for entry in list_node_images(output_dir, capture_id):
        if entry["nodeId"] == node_id and entry["port"] == port:
            path = os.path.join(
                output_dir,
                "{0}{1}{2}.{3}{4}".format(
                    capture_id,
                    _NODE_IMAGE_MARKER,
                    node_id,
                    port,
                    _NODE_IMAGE_SUFFIX,
                ),
            )
            return path if os.path.isfile(path) else None
    return None


def read_mask_overlay(
    output_dir: Optional[str], capture_id: Optional[str]
) -> dict:
    """``{'maskImage': <base64|None>, 'maskBackground': <dict|None>}`` for
    the run.

    Prefers the ``.jsonl``-derived mask + chroma-key background; falls back
    to the raw ``.mask.png`` bytes (base64, no background — the frontend
    treats an absent background as no chroma-key). Returns a null mask
    (never raises) when no mask artifact is present (R5.7)."""
    empty = {"maskImage": None, "maskBackground": None}
    if not output_dir or not capture_id:
        return empty

    mask_b64, background = _mask_from_jsonl(output_dir, capture_id)
    if mask_b64 is None:
        mask_b64 = _mask_from_png(output_dir, capture_id)
    if mask_b64 is None:
        return empty
    return {"maskImage": mask_b64, "maskBackground": background}


def _mask_from_jsonl(
    output_dir: str, capture_id: str
) -> Tuple[Optional[str], Optional[dict]]:
    """``(mask_base64, mask_background)`` from the run's result ``.jsonl``,
    or ``(None, None)``. Best-effort and contained."""
    path = _artifact_path(output_dir, capture_id, _JSONL_SUFFIX)
    try:
        with open(path, "r") as jsonl_file:
            lines = [line for line in jsonl_file.read().splitlines() if line.strip()]
        if not lines:
            return None, None
        # The run writes one capture record; use the last non-empty line.
        record = json.loads(lines[-1])
        outputs = record.get("deviceFleetAuxiliaryOutputs") or []
        return (
            _mask_base64_from_outputs(outputs),
            _mask_background_from_outputs(outputs),
        )
    except Exception:  # noqa: BLE001 - best-effort, never fail the request
        logger.debug(
            "Could not derive mask from result jsonl at %s", path, exc_info=True
        )
        return None, None


def _mask_base64_from_outputs(outputs: list) -> Optional[str]:
    """The mask as a base64 string from a capture record's outputs.

    Mirrors ``inference_results_utils.get_mask_base64_image``: a mask entry
    either embeds the base64 ``data`` directly or references it on disk via
    ``data-ref`` (depending on the em-agent base64 embed limit)."""
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        content_type = entry.get("observedContentType", "")
        if isinstance(content_type, str) and content_type.startswith(
            _MASK_CONTENT_TYPE_PREFIX
        ):
            if entry.get("data"):
                return entry["data"]
            ref = entry.get("data-ref")
            if ref:
                file_path = (
                    ref[len(_FILE_URI_PREFIX):]
                    if ref.startswith(_FILE_URI_PREFIX)
                    else ref
                )
                if os.path.isfile(file_path):
                    with open(file_path, "rb") as image_file:
                        return base64.b64encode(image_file.read()).decode()
    return None


def _mask_background_from_outputs(outputs: list) -> Optional[dict]:
    """The mask chroma-key background (``{'rgb-color': [r,g,b], ...}``) from
    the capture record's label block, or ``None``.

    Mirrors ``inference_results_utils``: the segmentation label block is a
    base64 ``json_with_base64_encoding`` payload whose ``anomalies['0']``
    entry carries the background color as a ``hex-color`` converted to
    ``rgb-color``."""
    base64_label = None
    for entry in outputs:
        if isinstance(entry, dict) and (
            entry.get("observedContentType") == _RES_LABEL_CONTENT_TYPE
        ):
            base64_label = entry.get("data")
            break
    if not base64_label:
        return None
    try:
        label = json.loads(base64.b64decode(base64_label))
        background = label["anomalies"]["0"]
    except Exception:  # noqa: BLE001 - malformed label -> no background
        return None
    return _convert_hex_color_to_rgb(background)


def _convert_hex_color_to_rgb(background: dict) -> Optional[dict]:
    """Convert a background's ``hex-color`` to an ``rgb-color`` triple,
    mirroring ``inference_results_utils.convert_hex_color_to_rgb``."""
    if not isinstance(background, dict):
        return None
    mask: dict = {}
    for key, value in background.items():
        if key == "hex-color" and isinstance(value, str):
            hex_color = value.lstrip("#")
            try:
                mask["rgb-color"] = [
                    int(hex_color[i:i + 2], 16) for i in (0, 2, 4)
                ]
            except (ValueError, IndexError):
                continue
        else:
            mask[key] = value
    return mask


def _mask_from_png(output_dir: str, capture_id: str) -> Optional[str]:
    """The raw ``.mask.png`` artifact as a base64 string, or ``None``."""
    mask_path = _artifact_path(output_dir, capture_id, _MASK_SUFFIX)
    if not os.path.isfile(mask_path):
        return None
    try:
        with open(mask_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode()
    except OSError:
        return None


def read_run_log(log_path: Optional[str]) -> str:
    """The full text of a run's Run_Log, or ``""`` when unavailable
    (Requirements 4.3, 6.4).

    ``RunLogCapture`` writes the run's log through a ``RotatingFileHandler``
    with a single rolled backup (``run_log.DEFAULT_BACKUP_COUNT == 1``): once
    the live file reaches the size cap it is renamed to ``{log_path}.1`` and a
    fresh live file is started. The oldest lines therefore live in the ``.1``
    backup and the newest in the live file, so this concatenates the backup
    first, then the live file, to return the log in chronological order.

    Best-effort and contained: a ``None``/missing path, or any read error,
    yields ``""`` (the frontend renders an explanatory empty state) rather
    than raising — the endpoint returns an empty-but-200 body, never a 500
    (R6.4)."""
    if not log_path:
        return ""
    parts = []
    # Oldest rolled-over lines first, then the live file (newest last).
    for candidate in ("{0}.1".format(log_path), log_path):
        chunk = _read_text_best_effort(candidate)
        if chunk:
            parts.append(chunk)
    return "".join(parts)


def _read_text_best_effort(path: str) -> str:
    """Read a text file, returning ``""`` for a missing/unreadable path."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as log_file:
            return log_file.read()
    except OSError:
        logger.debug("Could not read run-log file at %s", path, exc_info=True)
        return ""


def read_workflow_graph(graph_path: Optional[str]) -> Optional[dict]:
    """The registration's ``workflow.json`` graph document, or ``None``
    (Requirement 4.4).

    ``graph_path`` is ``{registration.artifact_path}/workflow.json`` (the
    ``discovery.WORKFLOW_FILE`` living in the discovered artifact set). The
    document carries the Workflow_Definition nodes (with positions) and
    connections the run-status graph mirrors (design §5.3).

    Best-effort and contained: a ``None``/missing path, a malformed
    (non-JSON) file, or a top-level value that is not a JSON object all
    yield ``None`` so the endpoint can answer 404 rather than 500 (R4.6).
    """
    if not graph_path or not os.path.isfile(graph_path):
        return None
    try:
        with open(graph_path, "r", encoding="utf-8") as graph_file:
            document = json.load(graph_file)
    except (OSError, ValueError):
        logger.debug(
            "Could not read workflow graph at %s", graph_path, exc_info=True
        )
        return None
    if not isinstance(document, dict):
        return None
    return document


def read_run_metadata(
    output_dir: Optional[str], capture_id: Optional[str]
) -> dict:
    """The run's metadata JSON (``{output_dir}/{capture_id}.json``) as a
    dict, or ``{}`` (Requirements 4.1, 4.2).

    ``pipeline_executor._persist_run_metadata`` writes the run's final tag
    values (including each llm node's ``generated_text``/``error`` and
    Bedrock's merged ``is_anomalous``/``confidence`` fields) to
    ``{capture_id}.json`` under the run's ``output_dir``. Best-effort and
    contained (mirrors ``parse_node_status``): a missing
    ``output_dir``/``capture_id``, a missing/unreadable file, malformed
    JSON, or a top-level value that is not a JSON object all yield ``{}``
    (200) rather than a 500 — a run without persisted metadata simply has
    an empty object.
    """
    if not output_dir or not capture_id:
        return {}
    path = _artifact_path(output_dir, capture_id, _METADATA_SUFFIX)
    try:
        with open(path, "r", encoding="utf-8") as metadata_file:
            parsed = json.load(metadata_file)
    except (OSError, ValueError):
        logger.debug(
            "Could not read run metadata at %s", path, exc_info=True
        )
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def parse_node_status(node_status_json: Optional[str]) -> dict:
    """The persisted per-node status map as a dict, or ``{}`` (R4.5).

    ``WorkflowExecution.node_status_json`` holds a JSON
    ``{nodeId: {status, detail?}}`` string (written by
    ``node_status.NodeStatusCollector.to_json``). Best-effort and
    contained: a ``None``/empty/malformed value, or a top-level value that
    is not a JSON object, yields ``{}`` (200) rather than a 500 — a run that
    never recorded per-node status simply has an empty map.
    """
    if not node_status_json:
        return {}
    try:
        parsed = json.loads(node_status_json)
    except (ValueError, TypeError):
        logger.debug(
            "Could not parse node_status_json payload", exc_info=True
        )
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
