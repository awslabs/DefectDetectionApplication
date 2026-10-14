"""Executor-binding execution in simulation mode.

The simulation compiler maps hardware output nodes (digital output,
MQTT publish, OPC UA write) to ``recording_*`` executor bindings
(workflow_core.catalog.SIM_RECORDING_BINDING_PREFIX). The harness
executes those bindings as recording stubs: it records the parameters
and the triggering inference metadata — what the node would have
actuated/emitted — without contacting any physical or device-local
endpoint (Requirement 12.6).

``inference_filter`` bindings are evaluated over the pipeline's
inference metadata (``is_anomalous``/``confidence`` tag values) with the
same rule dialect the catalog documents, gating downstream recorders the
way the LocalServer executor gates real actuations.

The three Scene_Analytics_Nodes (rtsp-rtmp-stream-cameras Requirements
13.9, 14.6, 15.6) run through the same pure module the LocalServer uses,
``workflow_core.analytics.scene``, in the device's order: counters and
associations first, over the run's Detection_List, then event gates over
the full metadata, then filters, conditionals and recorders. A test run
is one run, so every event gate starts inactive.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .results import ResultsStore

try:  # vendored workflow_core (present in the container image)
    from workflow_core.catalog import SIM_RECORDING_BINDING_PREFIX
except ImportError:  # unit tests without the package on the path
    SIM_RECORDING_BINDING_PREFIX = "recording_"

#: Binding id of the executor-evaluated inference filter node.
INFERENCE_FILTER_BINDING = "inference_filter"

#: Binding id of the executor-evaluated two-path conditional node:
#: downstream of its "true" output port is gated by the configured
#: condition, downstream of the "false" port by its negation (the
#: compiler's per-port ``portConditions``).
CONDITIONAL_BINDING = "conditional"


def is_recording_binding(binding: Dict) -> bool:
    """True for the simulation recording stubs (12.6)."""
    return str(binding.get("binding", "")).startswith(SIM_RECORDING_BINDING_PREFIX)


# ---------------------------------------------------------------------------
# Condition evaluation ("is_anomalous == true && confidence >= 0.8")
#
# Mirrors the LocalServer executor evaluator (unary '!' negation and
# dotted field paths included) so cloud test runs behave exactly like the
# device. A dotted identifier ("counter.people.total") resolves segment by
# segment against the nested run metadata, as it does on the device
# (workflow_engine/output_bindings.py); flat identifiers keep their exact
# lookup.
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    \s*(?:
        (?P<op>&&|\|\||==|!=|>=|<=|>|<|\(|\)|!)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"[^"]*"|'[^']*')
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
    )""", re.VERBOSE)


def resolve_field_path(document: Any, dotted_path: Any) -> Tuple[bool, Any]:
    """Resolve a dotted field path (``a.b.c``) against nested metadata.

    The LocalServer's ``output_bindings.resolve_field_path``, verbatim in
    behavior: a dict is traversed by key, a list by a numeric segment used
    as an index, and anything else — a missing key, a non-numeric or
    out-of-range index, an empty path — fails. Returns ``(found, value)``
    so a resolved ``None`` is distinguishable from "not found".
    """
    if not isinstance(dotted_path, str):
        return False, None
    path = dotted_path.strip()
    if not path:
        return False, None
    current = document
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            try:
                index = int(segment)
            except ValueError:
                return False, None
            if not 0 <= index < len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _tokenize(condition: str) -> List[str]:
    tokens: List[str] = []
    position = 0
    while position < len(condition):
        match = _TOKEN.match(condition, position)
        if not match or match.end() == position:
            remainder = condition[position:].strip()
            if not remainder:
                break
            raise ValueError(
                "Unparseable condition near {0!r}".format(remainder[:20]))
        tokens.append(match.group(match.lastgroup))
        position = match.end()
    return tokens


class _Parser:
    """Tiny recursive-descent parser: or-expr / and-expr / comparison."""

    def __init__(self, tokens: List[str], metadata: Dict[str, Any]):
        self.tokens = tokens
        self.position = 0
        self.metadata = metadata

    def peek(self) -> Optional[str]:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of condition")
        self.position += 1
        return token

    def parse(self) -> bool:
        value = self.or_expr()
        if self.peek() is not None:
            raise ValueError("Unexpected token {0!r}".format(self.peek()))
        return value

    def or_expr(self) -> bool:
        value = self.and_expr()
        while self.peek() == "||":
            self.take()
            right = self.and_expr()
            value = value or right
        return value

    def and_expr(self) -> bool:
        value = self.comparison()
        while self.peek() == "&&":
            self.take()
            right = self.comparison()
            value = value and right
        return value

    def comparison(self) -> bool:
        if self.peek() == "!":
            # Unary negation, e.g. "!(is_anomalous == true)" or "!flag".
            self.take()
            return not self.comparison()
        if self.peek() == "(":
            self.take()
            value = self.or_expr()
            if self.take() != ")":
                raise ValueError("Missing closing parenthesis")
            return value
        left = self.operand()
        operator = self.peek()
        if operator not in ("==", "!=", ">=", "<=", ">", "<"):
            # Bare truthy operand, e.g. "is_anomalous".
            return bool(left)
        self.take()
        right = self.operand()
        return _compare(left, operator, right)

    def operand(self) -> Any:
        token = self.take()
        if token in ("(", ")", "&&", "||", "!",
                     "==", "!=", ">=", "<=", ">", "<"):
            raise ValueError("Expected a value, got {0!r}".format(token))
        if token[0] in "\"'":
            return token[1:-1]
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(token) if "." in token else int(token)
        except ValueError:
            pass
        # Identifier: resolve from the inference metadata. A dotted
        # identifier resolves segment by segment against nested dicts and
        # lists, exactly as on the device; a flat identifier keeps its
        # exact lookup.
        if token not in self.metadata:
            if "." in token:
                found, value = resolve_field_path(self.metadata, token)
                if found:
                    return _coerce(value)
            raise ValueError(
                "Unknown metadata field {0!r} in condition".format(token))
        return self.metadata[token]


def _coerce(value: Any) -> Any:
    """Normalize tag values ('true'/'false' strings, numeric strings)."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _compare(left: Any, operator: str, right: Any) -> bool:
    left, right = _coerce(left), _coerce(right)
    if isinstance(left, bool) or isinstance(right, bool):
        left, right = bool(left), bool(right)
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    return left < right


def evaluate_condition(condition: str, metadata: Dict[str, Any]) -> bool:
    """Evaluate a rule expression over inference metadata. Raises
    ``ValueError`` for malformed conditions or unknown fields."""
    tokens = _tokenize(condition)
    if not tokens:
        raise ValueError("Empty condition")
    return _Parser(tokens, {k: _coerce(v) for k, v in metadata.items()}).parse()


# ---------------------------------------------------------------------------
# Scene analytics (rtsp-rtmp-stream-cameras Requirements 13.9, 14.6, 15.6)
#
# The same pure functions the LocalServer bindings call
# (workflow_core.analytics.scene, vendored in the sandbox image), wired the
# same way, so a test run predicts the device's counter, association and
# event metadata for the same inputs (design Property 27):
#
# 1. counters and associations, in topological order over
#    ``upstreamNodeIds``, over the run's Detection_List and frame size;
# 2. event gates, in topological order, each evaluating its condition over
#    the full run metadata (so a gate sees the gates before it);
# 3. inference filters, conditionals and recorders, as before.
#
# An analytics ``error`` outcome and a gate that does not pass gate the
# node's direct downstream recorders exactly like a failed inference
# filter. Neither fails the run (Requirement 13.6): both are recorded as
# a completed node with the outcome in its output.
# ---------------------------------------------------------------------------

#: Binding ids of the three Scene_Analytics_Nodes: the catalog maps each
#: node type to the executor binding of the same name.
DETECTION_COUNTER_BINDING = "detection_counter"
OBJECT_ASSOCIATION_BINDING = "object_association"
EVENT_GATE_BINDING = "event_gate"
SCENE_ANALYTICS_BINDINGS = (DETECTION_COUNTER_BINDING,
                            OBJECT_ASSOCIATION_BINDING, EVENT_GATE_BINDING)

#: Run metadata keys: the Detection_List and frame size the analytics
#: read, and the sections they merge into (Requirements 13.3, 14.4, 15.4).
DETECTIONS_KEY = "detections"
FRAME_KEY = "frame"
COUNTER_KEY = "counter"
ASSOCIATION_KEY = "association"
EVENT_KEY = "event"

#: The analytics outcome that gates downstream nodes (Requirement 13.6);
#: ``ok`` and ``warning`` do not.
ANALYTICS_OUTCOME_ERROR = "error"

def _scene_module():
    """``workflow_core.analytics.scene``, vendored in the container image,
    or None when it is not importable. Resolved per call rather than at
    import, so the module works however the package reaches the path."""
    try:
        from workflow_core.analytics import scene
    except ImportError:
        return None
    return scene


@dataclass
class SceneAnalyticsRun:
    """What the Scene_Analytics_Nodes of a document did in one run."""

    #: The run metadata with the ``counter``, ``association`` and
    #: ``event`` sections merged in (the input is not mutated).
    metadata: Dict[str, Any]
    #: Counter and association node id -> ``ok``, ``warning`` or ``error``.
    outcomes: Dict[str, str] = field(default_factory=dict)
    #: Event gate node id -> whether the run passes to its downstream nodes.
    passed: Dict[str, bool] = field(default_factory=dict)
    #: Event gate node id -> its Event_Gate_State after the run.
    states: Dict[str, Any] = field(default_factory=dict)

    def gating_outcomes(self) -> Dict[str, bool]:
        """Per analytics node, whether its direct downstream nodes run: not
        after an ``error`` outcome, and past a gate only when it passes."""
        gating = {node_id: outcome != ANALYTICS_OUTCOME_ERROR
                  for node_id, outcome in self.outcomes.items()}
        gating.update(self.passed)
        return gating


def has_scene_analytics(bindings: List[Dict]) -> bool:
    """Whether a document has a Scene_Analytics_Node."""
    return any(isinstance(binding, dict)
               and binding.get("binding") in SCENE_ANALYTICS_BINDINGS
               for binding in bindings or [])


def topological_order(bindings: List[Dict]) -> List[Dict]:
    """``bindings`` in topological order over their ``upstreamNodeIds``.

    Only dependencies between the listed bindings count (a pipeline node
    upstream of an executor node is not a binding). Ties keep emission
    order, so a document whose bindings are already ordered is returned
    unchanged; a cycle, which a validated graph cannot have, falls back to
    emission order rather than looping.
    """
    entries = [binding for binding in bindings or [] if isinstance(binding, dict)]
    listed = {binding.get("nodeId") for binding in entries}
    remaining = list(entries)
    visited: set = set()
    ordered: List[Dict] = []
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


def frame_metadata(frame_size: Any) -> Optional[Dict[str, Any]]:
    """``{"width", "height"}`` for a usable frame size, else None.

    Accepts a ``{"width", "height"}`` mapping or a ``(width, height)``
    pair of positive numbers, the shapes the analytics accept.
    """
    if isinstance(frame_size, dict):
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
    """The run metadata section ``key``: the analytics own these keys, so
    a non-dict value there is replaced."""
    section = metadata.get(key)
    if not isinstance(section, dict):
        section = {}
        metadata[key] = section
    return section


def run_scene_analytics(bindings: List[Dict], metadata: Dict[str, Any], *,
                        frame_size: Any = None,
                        states: Optional[Dict[str, Any]] = None,
                        now_ms: int = 0,
                        store: Optional[ResultsStore] = None) -> SceneAnalyticsRun:
    """Run a document's Scene_Analytics_Nodes over one run.

    ``metadata`` is the run metadata, carrying the Detection_List under
    ``detections``; ``frame_size`` is the ``(width, height)`` of the frame
    the detector processed, or None when unknown. ``states`` holds each
    event gate's state from the previous run: a test run passes None, so
    every gate starts inactive (Requirement 15.6), and the parity test
    carries the returned ``states`` from run to run. ``store`` records each
    node's outcome as a completed node; it is optional so the logic can be
    exercised on its own.
    """
    run_metadata = dict(metadata)
    for key in (COUNTER_KEY, ASSOCIATION_KEY, EVENT_KEY):
        if isinstance(run_metadata.get(key), dict):
            run_metadata[key] = dict(run_metadata[key])
    result = SceneAnalyticsRun(metadata=run_metadata,
                               states=dict(states or {}))
    ordered = [binding for binding in topological_order(bindings)
               if binding.get("binding") in SCENE_ANALYTICS_BINDINGS]
    if not ordered:
        return result
    _scene = _scene_module()
    if _scene is None:
        # The image always vendors workflow_core; a missing module is a
        # packaging defect, reported on each node rather than hidden.
        for binding in ordered:
            node_id = binding.get("nodeId")
            if binding.get("binding") == EVENT_GATE_BINDING:
                result.passed[node_id] = False
            else:
                result.outcomes[node_id] = ANALYTICS_OUTCOME_ERROR
            if store is not None:
                store.set_error(node_id, "Scene analytics are unavailable: "
                                         "workflow_core.analytics is not "
                                         "installed in the sandbox image",
                                code="ANALYTICS_UNAVAILABLE", flush=False)
        return result

    detections = run_metadata.get(DETECTIONS_KEY)
    for binding in ordered:
        kind = binding.get("binding")
        if kind == EVENT_GATE_BINDING:
            continue
        node_id = binding.get("nodeId")
        parameters = binding.get("parameters") or {}
        if kind == DETECTION_COUNTER_BINDING:
            outcome = _scene.count_detections(
                detections,
                classes=parameters.get("classes", ""),
                min_confidence=parameters.get("min_confidence", 0.0),
                zone=parameters.get("zone"),
                zone_rule=parameters.get("zone_rule", _scene.DEFAULT_ZONE_RULE),
                frame_size=frame_size)
            section = COUNTER_KEY
        else:
            outcome = _scene.associate(
                detections,
                subject_class=parameters.get("subject_class"),
                required_classes=parameters.get("required_classes"),
                min_overlap=parameters.get("min_overlap",
                                           _scene.DEFAULT_MIN_OVERLAP),
                min_confidence=parameters.get("min_confidence", 0.0),
                zone=parameters.get("zone"),
                frame_size=frame_size)
            section = ASSOCIATION_KEY
        node_metadata = _scene.run_metadata(outcome)
        _section(run_metadata, section)[node_id] = node_metadata
        result.outcomes[node_id] = outcome["outcome"]
        if store is not None:
            store.add_output(node_id, {
                "type": "scene_analytics",
                "binding": kind,
                "outcome": outcome["outcome"],
                "problems": list(outcome["problems"]),
                "metadata": node_metadata,
            }, flush=False)
            store.set_status(node_id, "completed", flush=False)

    for binding in ordered:
        if binding.get("binding") != EVENT_GATE_BINDING:
            continue
        node_id = binding.get("nodeId")
        parameters = binding.get("parameters") or {}
        condition = str(parameters.get("condition") or "")
        problems: List[str] = []
        try:
            verdict: Optional[bool] = evaluate_condition(condition, run_metadata)
        except ValueError as error:
            # Counts as false and is recorded on the node (Requirement 15.2).
            verdict = None
            problems.append("The condition could not be evaluated and counts "
                            "as false: {0}".format(error))
        state, passed, transition = _scene.step_event_gate(
            result.states.get(node_id) or _scene.EventGateState(),
            verdict,
            activate_after=parameters.get("activate_after",
                                          _scene.DEFAULT_ACTIVATE_AFTER),
            clear_after=parameters.get("clear_after",
                                       _scene.DEFAULT_CLEAR_AFTER),
            emit=parameters.get("emit", _scene.DEFAULT_EMIT),
            repeat_interval_ms=parameters.get("repeat_interval_ms", 0),
            now_ms=now_ms)
        result.states[node_id] = state
        result.passed[node_id] = passed
        gate_metadata = _scene.event_gate_metadata(state, transition)
        _section(run_metadata, EVENT_KEY)[node_id] = gate_metadata
        if store is not None:
            store.add_output(node_id, {
                "type": "event_gate_evaluation",
                "condition": condition,
                "result": verdict,
                "passed": passed,
                "metadata": gate_metadata,
                "problems": problems,
            }, flush=False)
            store.set_status(node_id, "completed", flush=False)
    return result


# ---------------------------------------------------------------------------
# Binding execution
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def execute_bindings(bindings: List[Dict], metadata: Dict[str, Any],
                     store: ResultsStore, *,
                     detections: Optional[List[Any]] = None,
                     frame_size: Any = None,
                     now_ms: Optional[int] = None) -> None:
    """Execute the document's executor bindings against the pipeline's
    inference metadata, flushing the results store after each node.

    - Scene analytics (rtsp-rtmp-stream-cameras): when the document has a
      Scene_Analytics_Node, the run metadata gains ``detections`` — the
      configured simulated detections, else an empty Detection_List — and
      ``frame`` when the configured frame size is known. Counters and
      associations, then event gates (each starting inactive), run before
      everything else (see :func:`run_scene_analytics`), so every later
      condition and recorder sees their metadata. A document without one
      runs exactly as before.
    - ``inference_filter``: evaluate the condition; the boolean gates
      downstream recorders exactly like the executor gates actuations.
    - ``conditional``: evaluate the per-port gate conditions (the compiler's
      ``portConditions``: "true" = the configured condition, "false" =
      its negation); recorders downstream of each output port are gated
      by that port's outcome, exactly like the device executor.
    - ``recording_*``: record the would-be actuation (parameters +
      triggering metadata + whether the gate/condition triggered it)
      as stub activity instead of contacting any endpoint (12.6). An
      analytics error outcome or a gate that does not pass gates it like
      a failed inference filter.
    - A malformed condition fails that node (with the error recorded and
      flushed) and leaves its downstream recorders untriggered (12.10).
    """
    filter_outcomes: Dict[str, Optional[bool]] = {}
    if has_scene_analytics(bindings):
        metadata = dict(metadata)
        if isinstance(detections, (list, tuple)):
            metadata[DETECTIONS_KEY] = list(detections)
        elif not isinstance(metadata.get(DETECTIONS_KEY), list):
            metadata[DETECTIONS_KEY] = []
        frame = frame_metadata(frame_size) or frame_metadata(metadata.get(FRAME_KEY))
        if frame is not None:
            metadata[FRAME_KEY] = frame
        analytics = run_scene_analytics(
            bindings, metadata, frame_size=frame,
            now_ms=_now_ms() if now_ms is None else now_ms, store=store)
        metadata = analytics.metadata
        filter_outcomes.update(analytics.gating_outcomes())
        store.flush()
    #: Conditional node id -> the downstream node ids its passing port(s)
    #: route to (empty when the condition could not be evaluated: never
    #: actuate on an unevaluable rule).
    conditional_allowed: Dict[str, set] = {}

    for binding in bindings:
        node_id = binding["nodeId"]
        if binding.get("binding") == INFERENCE_FILTER_BINDING:
            condition = str(binding.get("parameters", {}).get("condition", ""))
            try:
                outcome = evaluate_condition(condition, metadata)
            except ValueError as error:
                filter_outcomes[node_id] = None
                store.set_error(node_id, "Inference filter condition could not "
                                         "be evaluated: {0}".format(error),
                                code="FILTER_CONDITION_ERROR")
                continue
            filter_outcomes[node_id] = outcome
            store.add_output(node_id, {
                "type": "filter_evaluation",
                "condition": condition,
                "result": outcome,
                "metadata": dict(metadata),
            }, flush=False)
            store.set_status(node_id, "completed")
        elif binding.get("binding") == CONDITIONAL_BINDING:
            condition = str(binding.get("parameters", {}).get("condition", ""))
            port_conditions = binding.get("portConditions") or {}
            by_port = binding.get("downstreamNodeIdsByPort") or {}
            passing = set()
            results: Dict[str, Optional[bool]] = {}
            error_text: Optional[str] = None
            for port, port_condition in port_conditions.items():
                try:
                    outcome = evaluate_condition(str(port_condition), metadata)
                except ValueError as error:
                    results[port] = None
                    error_text = str(error)
                    continue
                results[port] = outcome
                if outcome:
                    passing.update(by_port.get(port) or [])
            conditional_allowed[node_id] = passing
            if error_text is not None:
                store.set_error(node_id, "Conditional condition could not be "
                                         "evaluated: {0}".format(error_text),
                                code="CONDITIONAL_CONDITION_ERROR")
                continue
            store.add_output(node_id, {
                "type": "conditional_evaluation",
                "condition": condition,
                "results": results,
                "metadata": dict(metadata),
            }, flush=False)
            store.set_status(node_id, "completed")

    for binding in bindings:
        if not is_recording_binding(binding):
            continue
        node_id = binding["nodeId"]
        parameters = dict(binding.get("parameters", {}))

        # Gate: every directly-upstream inference filter must have
        # passed, and every directly-upstream conditional must route here.
        gated_out = False
        for upstream in binding.get("upstreamNodeIds", []):
            if upstream in filter_outcomes and filter_outcomes[upstream] is not True:
                gated_out = True
            if upstream in conditional_allowed and node_id not in conditional_allowed[upstream]:
                gated_out = True

        # A digital output's own condition parameter gates actuation the
        # same way the executor does on-device (Requirement 9.4).
        condition = parameters.get("condition")
        condition_result: Optional[bool] = None
        condition_error: Optional[str] = None
        if condition:
            try:
                condition_result = evaluate_condition(str(condition), metadata)
            except ValueError as error:
                condition_error = str(error)

        triggered = (not gated_out and condition_error is None
                     and condition_result is not False)

        store.add_stub_activity(node_id, {
            "type": "recorded_actuation",
            "binding": binding.get("binding"),
            "parameters": parameters,
            "triggered": triggered,
            "triggeringMetadata": dict(metadata),
            "recordedAt": _timestamp(),
            "note": "Simulated: recorded instead of actuating any physical "
                    "or device-local endpoint",
        }, flush=False)
        if condition_error is not None:
            store.set_error(node_id, "Output condition could not be "
                                     "evaluated: {0}".format(condition_error),
                            code="OUTPUT_CONDITION_ERROR")
        else:
            store.set_status(node_id, "completed")
