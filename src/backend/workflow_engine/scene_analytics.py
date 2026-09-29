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
"""Scene analytics on the device (rtsp-rtmp-stream-cameras Requirements
13-15; design component 16).

The pure logic is the vendored ``workflow_core.analytics.scene``, which the
Portal's test sandbox also runs. This module applies it to a run exactly
as the sandbox harness does (``test-sandbox/harness/bindings.py``), so the
two produce identical ``counter``, ``association`` and ``event`` metadata
for identical inputs (Property 27):

1. :func:`apply_scene_analytics` runs in ``WorkflowExecutor.execute`` right
   after the Detection_List is merged, before the Bedrock and LLM
   processors (so prompts can reference the results). It evaluates every
   ``detection_counter`` and ``object_association`` binding in topological
   order over the run's Detection_List and merges ``counter.<nodeId>`` /
   ``association.<nodeId>`` into the run metadata. Each node's outcome
   (``ok``, ``warning`` or ``error``) is recorded on its node status and
   on the per-run document (:data:`OUTCOMES_KEY`), where the output
   bindings read it for gating.
2. :func:`evaluate_event_gates` runs in ``OutputBindingProcessor`` before
   the inference filters, conditionals and outputs. It steps every
   ``event_gate`` binding in topological order over the full run metadata
   (Bedrock and LLM results included), merging each gate's
   ``event.<nodeId>`` before the next gate is evaluated. Gate state lives
   in an :class:`EventGateStateStore`, keyed by registration and node.

Gating is direct-upstream, like chained inference filters: an analytics
``error`` outcome, or a gate that does not pass, gates only its direct
downstream nodes, and neither fails the run.
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from workflow_engine.vendor.workflow_core.analytics import scene

logger = logging.getLogger(__name__)

DETECTION_COUNTER_BINDING = "detection_counter"
OBJECT_ASSOCIATION_BINDING = "object_association"
EVENT_GATE_BINDING = "event_gate"
SCENE_ANALYTICS_BINDINGS = (DETECTION_COUNTER_BINDING, OBJECT_ASSOCIATION_BINDING, EVENT_GATE_BINDING)

DETECTIONS_KEY = "detections"
FRAME_KEY = "frame"
COUNTER_KEY = "counter"
ASSOCIATION_KEY = "association"
EVENT_KEY = "event"

#: The per-run document key carrying the counter and association outcomes
#: (``{nodeId: "ok" | "warning" | "error"}``) from the executor to the
#: output bindings. The executor always works on a private copy of the
#: compiled document, so the annotation never outlives the run.
OUTCOMES_KEY = "_sceneAnalyticsOutcomes"


def _bindings(document: Any) -> List[Dict[str, Any]]:
    bindings = document.get("executorBindings") if isinstance(document, Mapping) else None
    return [binding for binding in (bindings or []) if isinstance(binding, dict)]


def has_scene_analytics(document: Any) -> bool:
    return any(binding.get("binding") in SCENE_ANALYTICS_BINDINGS for binding in _bindings(document))


def needs_frame_size(document: Any) -> bool:
    """Whether a counter or association sets a zone (only a zone needs the
    frame size)."""
    for binding in _bindings(document):
        if binding.get("binding") in (DETECTION_COUNTER_BINDING, OBJECT_ASSOCIATION_BINDING):
            zone = (binding.get("parameters") or {}).get("zone")
            if zone is not None and (not isinstance(zone, str) or zone.strip()):
                return True
    return False


def topological_order(bindings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The bindings in dependency order over ``upstreamNodeIds``, ties (and
    cycles) in emission order; the sandbox's exact algorithm."""
    entries = [binding for binding in bindings or [] if isinstance(binding, dict)]
    listed = {binding.get("nodeId") for binding in entries}
    remaining = list(entries)
    visited: set = set()
    ordered: List[Dict[str, Any]] = []
    while remaining:
        chosen = 0
        for index, binding in enumerate(remaining):
            upstream = [node for node in binding.get("upstreamNodeIds") or []
                        if node in listed and node != binding.get("nodeId")]
            if all(node in visited for node in upstream):
                chosen = index
                break
        binding = remaining.pop(chosen)
        visited.add(binding.get("nodeId"))
        ordered.append(binding)
    return ordered


def frame_dimensions(frame_size: Any) -> Optional[Dict[str, Any]]:
    """``{"width", "height"}`` of a ``{width, height}`` dict or a
    ``(width, height)`` pair of finite positive numbers, else None."""
    if isinstance(frame_size, Mapping):
        width, height = frame_size.get("width"), frame_size.get("height")
    elif isinstance(frame_size, (list, tuple)) and len(frame_size) == 2:
        width, height = frame_size
    else:
        return None
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not value > 0 or value == float("inf"):
            return None
    return {"width": width, "height": height}


def _section(metadata: Dict[str, Any], key: str) -> Dict[str, Any]:
    section = metadata.get(key)
    if not isinstance(section, dict):
        section = {}
        metadata[key] = section
    return section


def _summary(kind: str, node_metadata: Dict[str, Any]) -> str:
    if kind == DETECTION_COUNTER_BINDING:
        counted = ["{0} {1}".format(key or "(unlabeled)", count)
                   for key, count in (node_metadata.get("counts") or {}).items() if count]
        return ", ".join(counted) if counted else "nothing counted"
    return "{0} of {1} compliant".format(node_metadata.get("compliant", 0), node_metadata.get("subjects", 0))


def _record_outcome(collector, node_id: str, kind: str, outcome: Dict[str, Any],
                    node_metadata: Dict[str, Any]) -> None:
    """The node's status: a summary detail when ok, a warning or a
    failure (which never fails the run) carrying the problems."""
    if collector is None:
        return
    problems = "; ".join(str(problem) for problem in outcome.get("problems") or ())
    try:
        if outcome.get("outcome") == scene.OUTCOME_ERROR:
            collector.mark_failure(node_id, problems or "the node could not be evaluated")
        elif outcome.get("outcome") == scene.OUTCOME_WARNING:
            collector.mark_warning(node_id, "{0} ({1})".format(_summary(kind, node_metadata), problems)
                                   if problems else _summary(kind, node_metadata))
        else:
            collector.set_detail(node_id, _summary(kind, node_metadata))
    except Exception:  # noqa: BLE001 - node status is best-effort
        logger.debug("Could not record the outcome of node %s", node_id, exc_info=True)


def apply_scene_analytics(document: Dict[str, Any], tag_values: Dict[str, Any],
                          frame_size: Any = None, collector=None) -> Dict[str, str]:
    """Evaluate the document's counters and associations over the run's
    Detection_List and merge their metadata into ``tag_values`` (see the
    module docstring). Returns ``{nodeId: outcome}``, also stored on the
    per-run document under :data:`OUTCOMES_KEY`."""
    ordered = [binding for binding in topological_order(_bindings(document))
               if binding.get("binding") in (DETECTION_COUNTER_BINDING, OBJECT_ASSOCIATION_BINDING)]
    outcomes: Dict[str, str] = {}
    if not ordered:
        return outcomes
    detections = tag_values.get(DETECTIONS_KEY)
    for binding in ordered:
        kind = binding.get("binding")
        node_id = binding.get("nodeId")
        parameters = binding.get("parameters") or {}
        if kind == DETECTION_COUNTER_BINDING:
            outcome = scene.count_detections(
                detections,
                classes=parameters.get("classes", ""),
                min_confidence=parameters.get("min_confidence", 0.0),
                zone=parameters.get("zone"),
                zone_rule=parameters.get("zone_rule", scene.DEFAULT_ZONE_RULE),
                frame_size=frame_size,
            )
            section = COUNTER_KEY
        else:
            outcome = scene.associate(
                detections,
                subject_class=parameters.get("subject_class"),
                required_classes=parameters.get("required_classes"),
                min_overlap=parameters.get("min_overlap", scene.DEFAULT_MIN_OVERLAP),
                min_confidence=parameters.get("min_confidence", 0.0),
                zone=parameters.get("zone"),
                frame_size=frame_size,
            )
            section = ASSOCIATION_KEY
        node_metadata = scene.run_metadata(outcome)
        _section(tag_values, section)[node_id] = node_metadata
        outcomes[node_id] = outcome["outcome"]
        _record_outcome(collector, node_id, kind, outcome, node_metadata)
        if outcome["outcome"] != scene.OUTCOME_OK:
            logger.warning("Scene analytics node %s: %s", node_id, "; ".join(outcome.get("problems") or ()))
    if isinstance(document, dict):
        document[OUTCOMES_KEY] = dict(outcomes)
    return outcomes


def analytics_gating(document: Any) -> Dict[str, bool]:
    """``{counter or association nodeId: passes}`` of the run: an ``error``
    outcome gates the node's direct downstream nodes."""
    outcomes = document.get(OUTCOMES_KEY) if isinstance(document, Mapping) else None
    if not isinstance(outcomes, Mapping):
        return {}
    return {node_id: outcome != scene.OUTCOME_ERROR for node_id, outcome in outcomes.items()}


# --- event gates --------------------------------------------------------------


class EventGateStateStore:
    """Event_Gate_State per registration and node, in memory: a backend
    restart and a new registration (a new version) start every gate
    inactive (Requirement 15.5)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: Dict[Tuple[str, str], scene.EventGateState] = {}

    def get(self, registration_id: str, node_id: str) -> scene.EventGateState:
        with self._lock:
            return self._states.get((registration_id, node_id)) or scene.EventGateState()

    def set(self, registration_id: str, node_id: str, state: scene.EventGateState) -> None:
        with self._lock:
            self._states[(registration_id, node_id)] = state

    def forget(self, registration_id: str) -> None:
        with self._lock:
            for key in [key for key in self._states if key[0] == registration_id]:
                del self._states[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)


#: The process-wide store the OutputBindingProcessor uses by default.
EVENT_GATE_STATES = EventGateStateStore()


def registration_key(document: Any) -> str:
    """The registration a compiled document belongs to
    (``{workflowId}:{workflowVersion}``, the watcher's registration id)."""
    if not isinstance(document, Mapping):
        return ""
    return "{0}:{1}".format(document.get("workflowId") or "", document.get("workflowVersion") or "")


def _gate_detail(gate_metadata: Dict[str, Any], passed: bool, problem: Optional[str]) -> str:
    transition = gate_metadata.get("transition")
    detail = "{0}{1}; {2}".format(
        gate_metadata.get("state"),
        "" if transition == scene.TRANSITION_NONE else ", " + str(transition),
        "passed" if passed else "held")
    return "{0} ({1})".format(detail, problem) if problem else detail


def evaluate_event_gates(document: Any, bindings: List[Dict[str, Any]], metadata: Dict[str, Any],
                         tag_values: Optional[Dict[str, Any]], *, evaluate: Callable[[str, Dict[str, Any]], bool],
                         store: EventGateStateStore, now_ms: int,
                         detail_sink: Optional[Callable[[Optional[str], str], None]] = None) -> Dict[str, bool]:
    """Step every ``event_gate`` among ``bindings`` (see the module
    docstring) and return ``{gate nodeId: passed}``.

    The order is the topological order of the whole document, so a gate
    sees the gates before it. Each gate's ``event.<nodeId>`` is merged into
    ``metadata`` (what the remaining gates, filters and outputs read) and
    into ``tag_values`` (the persisted run metadata). ``evaluate`` is the
    Condition_Language evaluator; a ValueError counts as false and is
    recorded on the node.
    """
    wanted = {binding.get("nodeId") for binding in bindings
              if isinstance(binding, dict) and binding.get("binding") == EVENT_GATE_BINDING}
    if not wanted:
        return {}
    ordered = [binding for binding in topological_order(_bindings(document) or list(bindings))
               if binding.get("binding") == EVENT_GATE_BINDING and binding.get("nodeId") in wanted]
    registration_id = registration_key(document)
    events = _section(tag_values, EVENT_KEY) if isinstance(tag_values, dict) else {}
    metadata[EVENT_KEY] = events
    passed_by_gate: Dict[str, bool] = {}
    for binding in ordered:
        node_id = binding.get("nodeId")
        parameters = binding.get("parameters") or {}
        condition = str(parameters.get("condition") or "")
        problem = None
        try:
            verdict = evaluate(condition, metadata)
        except ValueError as error:
            verdict = None
            problem = "the condition could not be evaluated and counts as false: {0}".format(error)
            logger.warning("Event gate %s: %s", node_id, problem)
        state, passed, transition = scene.step_event_gate(
            store.get(registration_id, node_id), verdict,
            activate_after=parameters.get("activate_after", scene.DEFAULT_ACTIVATE_AFTER),
            clear_after=parameters.get("clear_after", scene.DEFAULT_CLEAR_AFTER),
            emit=parameters.get("emit", scene.DEFAULT_EMIT),
            repeat_interval_ms=parameters.get("repeat_interval_ms", 0),
            now_ms=now_ms,
        )
        store.set(registration_id, node_id, state)
        passed_by_gate[node_id] = passed
        gate_metadata = scene.event_gate_metadata(state, transition)
        events[node_id] = gate_metadata
        if transition != scene.TRANSITION_NONE:
            logger.info("Event gate %s %s", node_id, transition)
        if detail_sink is not None:
            try:
                detail_sink(node_id, _gate_detail(gate_metadata, passed, problem))
            except Exception:  # noqa: BLE001 - node status is best-effort
                logger.debug("Could not record the event gate %s detail", node_id, exc_info=True)
    return passed_by_gate
