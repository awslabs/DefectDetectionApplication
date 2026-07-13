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
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
# Mirrors the LocalServer executor evaluator (unary '!' negation
# included) so cloud test runs behave exactly like the device.
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    \s*(?:
        (?P<op>&&|\|\||==|!=|>=|<=|>|<|\(|\)|!)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"[^"]*"|'[^']*')
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)


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
        # Identifier: resolve from the inference metadata.
        if token not in self.metadata:
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
# Binding execution
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_bindings(bindings: List[Dict], metadata: Dict[str, Any],
                     store: ResultsStore) -> None:
    """Execute the document's executor bindings against the pipeline's
    inference metadata, flushing the results store after each node.

    - ``inference_filter``: evaluate the condition; the boolean gates
      downstream recorders exactly like the executor gates actuations.
    - ``conditional``: evaluate the per-port gate conditions (the compiler's
      ``portConditions``: "true" = the configured condition, "false" =
      its negation); recorders downstream of each output port are gated
      by that port's outcome, exactly like the device executor.
    - ``recording_*``: record the would-be actuation (parameters +
      triggering metadata + whether the gate/condition triggered it)
      as stub activity instead of contacting any endpoint (12.6).
    - A malformed condition fails that node (with the error recorded and
      flushed) and leaves its downstream recorders untriggered (12.10).
    """
    filter_outcomes: Dict[str, Optional[bool]] = {}
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
