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

"""The ``run`` record assembler (workflow-debug-mode design §4.2, §5.1
``recorder.py``).

:func:`assemble_run_record` builds the ``dda.debug.v1`` ``run`` record from
the shared :class:`~workflow_engine.telemetry.hooks.RunRecord` and the run's
artifacts on disk: the timing fields, the ``trigger`` mapped from the shape
of the Trigger_Context, ``nodes`` and ``bindings`` from the node-status map,
the error, the traceback and the failing node, ``outputs`` (detections with
invalid confidences dropped and counted, the ``llm`` and ``bedrock`` verdicts
under their engine keys, the top-level verdict), ``model`` from the buffered
readiness events and the last wait document, ``frameSource``, ``artifacts``
and ``logs`` (the parsed ``run.log`` merged by time with the tap's lines).
Every read is best-effort: a missing file leaves its field ``null`` and is
listed in ``artifacts.missing``. Every string of the record is redacted
before it is returned. Pure given its inputs; it never raises.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from workflow_engine import detections as detections_module
from workflow_engine import run_artifacts
from workflow_engine.telemetry.debug import runlog
from workflow_engine.telemetry.debug.redact import redact_structure

logger = logging.getLogger(__name__)

__all__ = ["assemble_run_record", "MAX_NODES", "MESSAGE_CAP", "PAYLOAD_CAP"]

#: At most this many ``nodes`` entries travel; the rest are counted.
MAX_NODES = 64
#: Per log message cap before the §5.4 truncation (bytes of text).
MESSAGE_CAP = 2048
#: ``trigger.payload`` cap for the serialized unknown-shape context.
PAYLOAD_CAP = 4096

#: Detail prefixes the output-binding runners write, by binding kind
#: (``output_bindings.py``: ``_mqtt_detail``, the OPC UA and Modbus writers,
#: the digital-output runner); the node-status map carries no kind itself.
_BINDING_DETAIL_PREFIXES = (
    ("sent to topic ", "mqtt_publish"),
    ("wrote ", "write"),
    ("set pin ", "digital_output"),
    ("pulsed pin ", "digital_output"),
)


def assemble_run_record(
    run_record: Any,
    session: Any,
    tap_lines: Any,
    *,
    readiness: Optional[Iterable[dict]] = None,
    model_wait: Optional[dict] = None,
    secrets: Iterable[str] = (),
) -> dict:
    """Build the ``run`` record of §4.2 (see the module docstring).

    ``session`` is the Pair's ``SessionState`` (or ``None``; the record
    needs nothing from it today, it is accepted for the agent's call
    shape). ``tap_lines`` is the run's tap buffer (an object with
    ``snapshot() -> (lines, dropped_count, dropped_bytes)``) or a plain list
    of ``{ts, level, logger, message, src: "tap"}`` dicts. ``readiness`` is
    the list of ``{model, state, waitedMs, ready}`` the agent buffered for
    this execution; ``model_wait`` the registration's last wait document.
    """
    try:
        record = _assemble(run_record, tap_lines, readiness, model_wait)
    except Exception:  # noqa: BLE001 - the record is best-effort, never raises
        logger.debug("assemble_run_record failed; emitting a minimal record", exc_info=True)
        record = _minimal(run_record)
    try:
        return redact_structure(record, secrets)
    except Exception:  # noqa: BLE001
        return _minimal(run_record)


def _minimal(run_record: Any) -> dict:
    return {
        "type": "run",
        "ts": int(getattr(run_record, "finished_ms", 0) or 0),
        "executionId": getattr(run_record, "execution_id", None),
        "status": getattr(run_record, "status", None),
        "error": "[record assembly failed]",
        "truncated": [{"field": "record", "originalBytes": 0}],
    }


def _assemble(rr: Any, tap_lines: Any, readiness: Optional[Iterable[dict]],
              model_wait: Optional[dict]) -> dict:
    missing: List[str] = []
    truncated: List[dict] = []
    output_dir = rr.output_dir
    capture_id = rr.capture_id

    tag_values = rr.tag_values
    if tag_values is None:
        if output_dir and capture_id:
            tag_values = run_artifacts.read_run_metadata(output_dir, capture_id)
            if not tag_values:
                missing.append("metadata")
        else:
            tag_values = {}
    if not isinstance(tag_values, dict):
        tag_values = {}

    detections, invalid = _detections(rr, tag_values, missing)
    if invalid:
        truncated.append({"field": "outputs.detections.invalid",
                          "originalBytes": invalid[1], "count": invalid[0]})

    node_status = rr.node_status if isinstance(rr.node_status, dict) else {}
    nodes, overflow = _nodes(node_status)
    if overflow:
        truncated.append({"field": "nodes", "originalBytes": overflow[1], "count": overflow[0]})
    bindings = _bindings(node_status, tag_values)

    logs, log_markers = _logs(rr, tap_lines, missing)
    truncated.extend(log_markers)

    started = rr.run_started_ms
    finished = int(rr.finished_ms)
    record = {
        "type": "run",
        "ts": finished,
        "executionId": rr.execution_id,
        "status": rr.status,
        "enqueuedAtS": rr.enqueued_at_s,
        "acceptedMs": rr.accepted_ms,
        "startedMs": started,
        "finishedMs": finished,
        "queuedMs": rr.queued_ms,
        "setupMs": rr.setup_ms,
        "readinessWaitMs": rr.readiness_wait_ms,
        "durationMs": (finished - int(started)) if started is not None else None,
        "trigger": _trigger(rr.trigger_context),
        "nodes": nodes,
        "bindings": bindings,
        "error": rr.error,
        "failingNodeId": rr.failing_node_id,
        "outputs": _outputs(tag_values, detections),
        "model": {
            "readiness": [dict(item) for item in (readiness or []) if isinstance(item, dict)],
            "wait": dict(model_wait) if isinstance(model_wait, dict) else None,
        },
        "frameSource": _frame_source(rr.trigger_context, tag_values),
        "artifacts": {
            "outputDir": output_dir,
            "captureId": capture_id,
            "hasOverlay": _has_overlay(output_dir, capture_id),
            "missing": missing,
        },
        "image": None,
        "logs": logs,
        "truncated": truncated,
    }
    if rr.traceback is not None:
        record["traceback"] = rr.traceback
    return record


# -- trigger -------------------------------------------------------------------


def _trigger(context: Any) -> dict:
    """The §4.2 mapping, tested in this order: ``topic`` -> mqtt, ``endpoint``
    -> opcua, ``source == "continuous"`` -> continuous, ``{}`` -> manual,
    anything else -> unknown with the serialized context as ``payload``."""
    if not isinstance(context, dict):
        context = {}
    base = {"source": None, "type": None, "topic": None, "payload": None,
            "timestamp": None, "receivedMs": None}
    if "topic" in context:
        stamp = _to_ms(context.get("timestamp"), seconds=True)
        base.update(type="mqtt", source="mqtt", topic=_text(context.get("topic")),
                    payload=_capped_text(context.get("payload"), PAYLOAD_CAP),
                    timestamp=stamp, receivedMs=stamp)
        return base
    if "endpoint" in context:
        base.update(
            type="opcua", source="opcua",
            topic="{0} {1}".format(context.get("endpoint"), context.get("node_id")),
            payload=_capped_text(context.get("value"), PAYLOAD_CAP),
            timestamp=_to_ms(context.get("source_timestamp")), receivedMs=None,
        )
        return base
    if context.get("source") == "continuous":
        base.update(type="continuous", source="continuous", topic=None, payload=None,
                    timestamp=_to_int(context.get("frameAcquiredAtMs")),
                    receivedMs=_to_int(context.get("tickAtMs")))
        return base
    if not context:
        base.update(type="manual")
        return base
    source = context.get("source")
    base.update(
        type="unknown", source=source if isinstance(source, str) else None,
        payload=_capped_text(json.dumps(context, sort_keys=True, default=str), PAYLOAD_CAP),
    )
    return base


def _to_ms(value: Any, seconds: bool = False) -> Optional[int]:
    """``value`` as epoch milliseconds: a number (seconds when ``seconds``
    or when it is small enough to be seconds), an ISO-8601 text, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if seconds or number < 1e11:
            return int(number * 1000)
        return int(number)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(float(text) * 1000) if seconds else _iso_to_ms(text)
        except ValueError:
            return None
    try:  # a datetime
        return int(value.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _iso_to_ms(text: str) -> Optional[int]:
    from datetime import datetime, timezone

    try:
        number = float(text)
        return int(number * 1000) if number < 1e11 else int(number)
    except ValueError:
        pass
    candidate = text.replace("Z", "+00:00")
    if "." in candidate:  # trim sub-microsecond digits fromisoformat refuses
        head, _, tail = candidate.partition(".")
        digits = ""
        rest = ""
        for index, character in enumerate(tail):
            if character.isdigit():
                digits += character
            else:
                rest = tail[index:]
                break
        candidate = head + "." + (digits[:6] or "0") + rest
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError("not an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _to_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _capped_text(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else (
        json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list))
        else str(value))
    return text[:cap]


# -- nodes and bindings ----------------------------------------------------------


def _nodes(node_status: Dict[str, Any]) -> Tuple[List[dict], Optional[Tuple[int, int]]]:
    nodes = []
    for node_id, entry in node_status.items():
        if not isinstance(entry, dict):
            entry = {"status": _text(entry)}
        nodes.append({
            "nodeId": _text(node_id),
            "status": _text(entry.get("status")),
            "durationMs": _to_int(entry.get("durationMs")),
            "detail": _text(entry.get("detail")),
        })
    overflow = None
    if len(nodes) > MAX_NODES:
        dropped = nodes[MAX_NODES:]
        overflow = (len(dropped), len(json.dumps(dropped, default=str)))
        nodes = nodes[:MAX_NODES]
    return nodes, overflow


def _bindings(node_status: Dict[str, Any], tag_values: Dict[str, Any]) -> List[dict]:
    bindings: List[dict] = []
    seen = set()

    def add(node_id, kind, result, detail):
        if node_id in seen:
            return
        seen.add(node_id)
        bindings.append({"nodeId": _text(node_id), "kind": kind, "result": result,
                         "detail": _text(detail)})

    for key, kind in (("llm", "llm_inference"), ("bedrock", "bedrock_inference")):
        outcomes = tag_values.get(key)
        if not isinstance(outcomes, dict):
            continue
        for node_id, outcome in outcomes.items():
            entry = node_status.get(node_id) if isinstance(node_status.get(node_id), dict) else {}
            failed = isinstance(outcome, dict) and outcome.get("error") not in (None, "")
            result = "failed" if failed else (_text(entry.get("status")) or "completed")
            detail = entry.get("detail")
            if detail is None and failed:
                detail = outcome.get("error")
            add(node_id, kind, result, detail)
    for node_id, entry in node_status.items():
        if not isinstance(entry, dict):
            continue
        detail = entry.get("detail")
        if not isinstance(detail, str):
            continue
        for prefix, kind in _BINDING_DETAIL_PREFIXES:
            if detail.startswith(prefix):
                add(node_id, kind, _text(entry.get("status")), detail)
                break
    return bindings


# -- outputs ------------------------------------------------------------------


def _detections(rr: Any, tag_values: Dict[str, Any], missing: List[str]):
    """``(items, invalid)``: the ``{label, confidence, box, id?}`` list and
    ``(count, bytes)`` of the items dropped for an invalid confidence."""
    source: Optional[list] = rr.detections if isinstance(rr.detections, list) else None
    if source is None:
        candidate = tag_values.get("detections")
        if isinstance(candidate, list):
            source = candidate
    if source is None and rr.output_dir and rr.capture_id:
        raw = detections_module.read_detections(rr.output_dir, rr.capture_id)
        if raw is None:
            missing.append("detections")
        else:
            source = raw
    if source is None:
        return None, None
    items: List[dict] = []
    dropped: List[Any] = []
    for entry in source:
        item = _detection_item(entry)
        if item is None:
            dropped.append(entry)
            continue
        items.append(item)
    invalid = (len(dropped), len(json.dumps(dropped, default=str))) if dropped else None
    return items, invalid


def _detection_item(entry: Any) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    confidence = entry.get("confidence")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not 0.0 <= float(confidence) <= 1.0):
        return None
    label = entry.get("label", entry.get("class_label"))
    if "x_min" in entry:
        box = [entry.get("x_min"), entry.get("y_min"), entry.get("x_max"), entry.get("y_max")]
    else:
        box = entry.get("box", entry.get("bounding_box"))
        box = list(box) if isinstance(box, (list, tuple)) else None
    item = {"label": _text(label) if label is not None else "", "confidence": float(confidence),
            "box": box}
    if entry.get("id") is not None:
        item["id"] = _text(entry.get("id"))
    return item


def _outputs(tag_values: Dict[str, Any], detections: Optional[list]) -> dict:
    llm: Dict[str, dict] = {}
    raw_llm = tag_values.get("llm")
    if isinstance(raw_llm, dict):
        for node_id, outcome in raw_llm.items():
            if not isinstance(outcome, dict):
                continue
            entry = {"text": _text(outcome.get("generated_text"))}
            _copy_optional(outcome, entry, ("error", "error"), ("is_anomalous", "isAnomalous"),
                           ("confidence", "confidence"))
            llm[_text(node_id)] = entry
    bedrock: Dict[str, dict] = {}
    raw_bedrock = tag_values.get("bedrock")
    if isinstance(raw_bedrock, dict):
        for node_id, outcome in raw_bedrock.items():
            if not isinstance(outcome, dict):
                continue
            entry = {"text": _text(outcome.get("text"))}
            _copy_optional(outcome, entry, ("error", "error"), ("is_anomalous", "isAnomalous"),
                           ("confidence", "confidence"), ("detection_id", "detectionId"))
            bedrock[_text(node_id)] = entry
    return {
        "detections": detections,
        "llm": llm,
        "bedrock": bedrock,
        "isAnomalous": tag_values.get("is_anomalous"),
        "confidence": tag_values.get("confidence"),
    }


def _copy_optional(source: dict, target: dict, *pairs: Tuple[str, str]) -> None:
    for key, name in pairs:
        if key in source and source[key] is not None:
            target[name] = source[key]


# -- frame source and artifacts ----------------------------------------------------


def _frame_source(context: Any, tag_values: Dict[str, Any]) -> Optional[dict]:
    context = context if isinstance(context, dict) else {}
    stream = tag_values.get("stream") if isinstance(tag_values.get("stream"), dict) else {}
    frame = tag_values.get("frame") if isinstance(tag_values.get("frame"), dict) else {}
    first_stream = None
    for value in stream.values():
        if isinstance(value, dict):
            first_stream = value
            break
    source = context.get("source")
    kind = source if isinstance(source, str) else None
    if kind is None and context:
        # A triggered run: the context carries no ``source``, so the kind is
        # the trigger's (the same shape test as ``_trigger``).
        kind = "mqtt" if "topic" in context else ("opcua" if "endpoint" in context else None)
    frame_seq = _to_int(context.get("frameSeq"))
    frame_ts = _to_int(context.get("frameAcquiredAtMs"))
    if first_stream is not None:
        if frame_seq is None:
            frame_seq = _to_int(first_stream.get("seq"))
        if frame_ts is None:
            frame_ts = _to_int(first_stream.get("acquiredAtMs"))
    camera_id = None
    width = _to_int(frame.get("width"))
    height = _to_int(frame.get("height"))
    if first_stream is not None:
        camera_id = _text(first_stream.get("cameraSourceId", first_stream.get("cameraId")))
        if width is None:
            width = _to_int(first_stream.get("width"))
        if height is None:
            height = _to_int(first_stream.get("height"))
    if kind is None and first_stream is None and not frame:
        return None
    return {"kind": kind, "cameraId": camera_id, "width": width, "height": height,
            "frameSeq": frame_seq, "frameTs": frame_ts}


def _has_overlay(output_dir: Optional[str], capture_id: Optional[str]) -> bool:
    try:
        return bool(run_artifacts.overlay_artifact_exists(output_dir, capture_id))
    except Exception:  # noqa: BLE001
        return False


# -- logs ------------------------------------------------------------------------


def _logs(rr: Any, tap_lines: Any, missing: List[str]) -> Tuple[List[dict], List[dict]]:
    markers: List[dict] = []
    entries: List[dict] = []
    if rr.log_path:
        text = run_artifacts.read_run_log(rr.log_path)
        if not text:
            missing.append("run.log")
        else:
            fallback = rr.accepted_ms if rr.accepted_ms is not None else int(rr.finished_ms)
            parsed, dropped = runlog.parse(text, fallback_ts_ms=int(fallback))
            if dropped:
                markers.append({"field": "logs.runlog", "originalBytes": len(text.encode("utf-8")),
                                "count": dropped})
            entries.extend(parsed)
    for entry in entries:
        message = entry.get("message")
        if isinstance(message, str) and len(message) > MESSAGE_CAP:
            entry["message"] = message[:MESSAGE_CAP]
    tap_entries, tap_marker = _tap_entries(tap_lines)
    if tap_marker is not None:
        markers.append(tap_marker)
    merged = sorted(
        [(e.get("ts") or 0, 0, index, e) for index, e in enumerate(entries)]
        + [(e.get("ts") or 0, 1, index, e) for index, e in enumerate(tap_entries)],
        key=lambda item: (item[0], item[1], item[2]),
    )
    return [item[3] for item in merged], markers


def _tap_entries(tap_lines: Any) -> Tuple[List[dict], Optional[dict]]:
    marker = None
    if tap_lines is None:
        return [], None
    if hasattr(tap_lines, "snapshot"):
        lines, dropped, dropped_bytes = tap_lines.snapshot()
        if dropped:
            marker = {"field": "logs.tap", "originalBytes": int(dropped_bytes), "count": int(dropped)}
    else:
        lines = list(tap_lines)
    result = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        entry = dict(line)
        entry["src"] = "tap"
        message = entry.get("message")
        if isinstance(message, str) and len(message) > MESSAGE_CAP:
            entry["message"] = message[:MESSAGE_CAP]
        result.append(entry)
    return result, marker
