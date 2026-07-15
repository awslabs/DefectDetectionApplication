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

"""Executor output bindings (Requirements 9.4, 9.5, 9.6).

Post-pipeline processing of a compiled document's ``executorBindings``,
registered as the WorkflowExecutor's post-run handler (task 12.4 plugging
into the hook from ``pipeline_executor``). After a successful run the
processor receives the parsed inference tag values (``is_anomalous``,
``confidence``) and:

- evaluates ``inference_filter`` bindings — rule expressions such as
  ``"is_anomalous == true && confidence >= 0.8"`` over the inference
  metadata — whose outcomes gate the output bindings downstream of them
  (the same rule dialect and gating the cloud test sandbox records);
- evaluates ``conditional`` bindings — two-path routing over the same
  rule dialect: output bindings downstream of the "true" output port are
  gated by the configured condition, those downstream of the "false"
  port by its negation (the compiler's per-port ``portConditions``);
- actuates ``digital_output`` bindings through the existing
  ``utils.dio_utils`` GPIO helpers when the binding's condition evaluates
  true, honoring pin, signal type, and pulse width (Requirement 9.4);
- publishes ``mqtt_publish`` bindings' rendered payload to the configured
  broker/topic/qos through the LocalServer-bundled paho-mqtt client
  (Requirement 9.5); with ``aws_iot`` enabled the publish targets AWS IoT
  Core over mutual TLS using the configured thing name and device-local
  certificate paths;
- writes ``opcua_write`` bindings' rendered value to the configured
  server node through the ``opcua`` client the Workflow_Component
  packages as a Python dependency (Requirement 9.6).

Input-side bindings (``digital_input``) and unknown binding kinds are
skipped. Every binding is processed independently: a failure is logged
and never propagates to other bindings, the executor, or the
Pipeline_Configuration path (Requirement 13.7). Hardware/network clients
are imported lazily so this module stays importable everywhere.
"""

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Binding kinds this processor knows about.
BINDING_INFERENCE_FILTER = "inference_filter"
BINDING_CONDITIONAL = "conditional"
BINDING_DIGITAL_OUTPUT = "digital_output"
BINDING_MQTT_PUBLISH = "mqtt_publish"
BINDING_OPCUA_WRITE = "opcua_write"
BINDING_DIGITAL_INPUT = "digital_input"
#: Handled by BedrockInferenceProcessor BEFORE the output bindings run
#: (the pipeline executor merges its result into the tag values this
#: processor gates on); skipped here.
BINDING_BEDROCK_INFERENCE = "bedrock_inference"


# ---------------------------------------------------------------------------
# Condition evaluation ("is_anomalous == true && confidence >= 0.8")
#
# Same rule dialect the workflow catalog documents for inference_filter /
# conditional / digital_output conditions, mirroring the cloud test sandbox
# evaluator so a workflow behaves on-device exactly as its test run
# predicted. Unary '!' negation is supported (the compiler composes the
# conditional node's "false"-path gate as "!(condition)").
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    \s*(?:
        (?P<op>&&|\|\||==|!=|>=|<=|>|<|\(|\)|!)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"[^"]*"|'[^']*')
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)

_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")


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
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

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
        if operator not in _OPERATORS:
            # Bare truthy operand, e.g. "is_anomalous".
            return bool(left)
        self.take()
        right = self.operand()
        return _compare(left, operator, right)

    def operand(self) -> Any:
        token = self.take()
        if token in ("(", ")", "&&", "||", "!") or token in _OPERATORS:
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
    return _Parser(
        tokens, {key: _coerce(value) for key, value in metadata.items()}
    ).parse()


# ---------------------------------------------------------------------------
# Template rendering ("{inference_json}", "{is_anomalous}", ...)
# ---------------------------------------------------------------------------

_SINGLE_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


class _LenientDict(dict):
    """Leaves unknown ``{placeholder}`` tokens intact instead of raising,
    so a template typo degrades to a literal rather than a failure."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: Any, metadata: Dict[str, Any]) -> Any:
    """Render a payload/value template over the inference metadata.

    Available placeholders are the parsed tag values (``is_anomalous``,
    ``confidence``) plus ``inference_json`` — the full metadata as JSON.
    A template that is exactly one known placeholder keeps the value's
    native type (so ``"{is_anomalous}"`` renders a boolean, not a
    string); mixed templates are formatted as strings.
    """
    if not isinstance(template, str):
        return template
    values = dict(metadata)
    values.setdefault(
        "inference_json", json.dumps(metadata, sort_keys=True, default=str)
    )
    match = _SINGLE_PLACEHOLDER.match(template)
    if match and match.group(1) in values:
        return values[match.group(1)]
    return template.format_map(_LenientDict(values))


# ---------------------------------------------------------------------------
# Default clients (imported lazily; injectable for tests)
# ---------------------------------------------------------------------------

#: Catalog signal types for digital_output bindings.
SIGNAL_TYPE_HIGH = "high"
SIGNAL_TYPE_LOW = "low"
SIGNAL_TYPE_PULSE = "pulse"


def _default_dio_actuator(pin: Any, signal_type: str, pulse_width_ms: Any) -> None:
    """Actuate a GPIO pin through the existing ``utils.dio_utils``
    helpers (Requirement 9.4).

    ``high``/``low`` latch the pin; ``pulse`` sets it high, waits the
    pulse width, and resets it (a non-positive width latches, matching
    the existing emoutputevent dio script behavior).
    """
    from utils import dio_utils
    from utils.constants import GPIO_FALLING, GPIO_RISING

    pin = int(pin)
    if signal_type == SIGNAL_TYPE_HIGH:
        dio_utils.set_output_pin(pin, GPIO_RISING)
    elif signal_type == SIGNAL_TYPE_LOW:
        dio_utils.set_output_pin(pin, GPIO_FALLING)
    elif signal_type == SIGNAL_TYPE_PULSE:
        dio_utils.set_output_pin(pin, GPIO_RISING)
        width_ms = int(pulse_width_ms)
        if width_ms > 0:
            time.sleep(width_ms / 1000.0)
            dio_utils.reset_output_pin(pin, GPIO_RISING)
    else:
        raise ValueError(
            "Unknown digital output signal type {0!r}".format(signal_type))


#: The plain-MQTT catalog default for broker_port; when ``aws_iot`` is
#: enabled and the port was left at this default, the standard mutual-TLS
#: port (8883) is used instead.
DEFAULT_MQTT_PORT = 1883
AWS_IOT_TLS_PORT = 8883

#: AWS IoT Core does not support MQTT QoS 2; the executor clamps the
#: configured qos to this maximum when ``aws_iot`` is enabled.
AWS_IOT_MAX_QOS = 1

#: Parameters required for AWS IoT Core publishing (mutual TLS): the
#: thing name (MQTT client id) and the device-local certificate file
#: paths. All must be set; the binding fails with a per-node error
#: rather than publishing insecurely.
AWS_IOT_REQUIRED_PARAMETERS = (
    "iot_thing_name",
    "iot_ca_cert_path",
    "iot_client_cert_path",
    "iot_private_key_path",
)


def _default_mqtt_publisher(
    host: str,
    port: int,
    topic: str,
    payload: str,
    qos: int,
    client_id: Optional[str] = None,
    tls: Optional[Dict[str, str]] = None,
) -> None:
    """Publish one message through the LocalServer-bundled paho-mqtt
    client (Requirement 9.5).

    ``tls`` (AWS IoT Core publishing) is a dict of ``tls_set`` keyword
    arguments — ``ca_certs``/``certfile``/``keyfile`` device file paths —
    that paho applies to the client via ``Client.tls_set`` for the
    mutual-TLS connection; ``client_id`` (the IoT thing name) becomes the
    MQTT client id."""
    import paho.mqtt.publish as mqtt_publish

    mqtt_publish.single(
        topic,
        payload=payload,
        qos=int(qos),
        hostname=host,
        port=int(port),
        client_id=client_id or "",
        tls=dict(tls) if tls else None,
    )


def _default_opcua_writer(endpoint: str, node_id: str, value: Any) -> None:
    """Write a value to an OPC UA server node through the ``opcua``
    client the Workflow_Component packages (Requirement 9.6)."""
    try:
        from opcua import Client
    except ImportError as e:
        raise RuntimeError(
            "The 'opcua' Python package is not available; it is delivered "
            "as a Workflow_Component dependency"
        ) from e

    client = Client(endpoint)
    client.connect()
    try:
        client.get_node(node_id).set_value(value)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# Bedrock comparison inference (bedrock_inference bindings)
#
# Runs BEFORE the gating/output bindings evaluate: the pipeline executor
# reads the two frames the compiled pipeline captured (the binding's
# {work_dir}-rooted capturePaths), calls the Bedrock runtime converse
# API with the configured model/prompt, parses the model's JSON answer,
# and merges {is_anomalous, confidence} into the run's inference
# metadata so downstream filters/conditionals/outputs see the fields —
# exactly like emltriton tag values flow today. Failures (network,
# credentials, missing frames, unparseable answers) are contained per
# Requirement 13.7: the run is marked failed with the node identified,
# and nothing else (other bindings, other pipelines) is touched.
# ---------------------------------------------------------------------------

#: Fixed client-side read timeout for Bedrock runtime invocations.
BEDROCK_READ_TIMEOUT_SEC = 30

#: Default model when the binding parameter is absent (mirrors the
#: catalog default).
BEDROCK_DEFAULT_MODEL = "us.amazon.nova-lite-v1:0"


class BedrockInferenceError(Exception):
    """A bedrock_inference binding failed. Carries the workflow node id
    so the executor can mark the run failed with the node identified
    (Requirements 9.7, 13.7)."""

    def __init__(self, node_id: Optional[str], message: str) -> None:
        super().__init__(message)
        self.node_id = node_id


_FENCED_BLOCK = re.compile(r"```[A-Za-z0-9_-]*\s*(.*?)```", re.DOTALL)


def parse_bedrock_answer(text: str) -> Dict[str, Any]:
    """Parse the model's answer into ``{is_anomalous, confidence}``.

    Tolerates fenced code blocks (``` / ```json) and surrounding prose:
    the first JSON object found is used. Raises ``ValueError`` when no
    JSON object with the expected fields can be extracted.
    """
    candidates = [match.group(1) for match in _FENCED_BLOCK.finditer(text or "")]
    candidates.append(text or "")
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if not isinstance(parsed, dict) or "is_anomalous" not in parsed:
            continue
        is_anomalous = _coerce(parsed.get("is_anomalous"))
        confidence = _coerce(parsed.get("confidence", 0.0))
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = 0.0
        return {
            "is_anomalous": bool(is_anomalous),
            "confidence": float(confidence),
        }
    raise ValueError(
        "Bedrock response did not contain the expected JSON object "
        "{{\"is_anomalous\": ..., \"confidence\": ...}}: {0!r}".format(
            (text or "")[:200]))


def _default_bedrock_invoker(
    model: str, prompt: str, images: List, region: str, max_tokens: int
) -> str:
    """Invoke the Bedrock runtime converse API and return the model's
    text answer. ``images`` is a list of ``(label, jpeg_bytes)`` pairs
    attached as image content blocks. boto3 is imported lazily so this
    module stays importable everywhere (Requirement 13.7)."""
    import boto3
    from botocore.config import Config as BotoConfig

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(
            read_timeout=BEDROCK_READ_TIMEOUT_SEC,
            retries={"max_attempts": 1},
        ),
    )
    content = [{"text": prompt}]
    for label, data in images:
        content.append({"text": "{0}:".format(label)})
        content.append({"image": {"format": "jpeg", "source": {"bytes": data}}})
    response = client.converse(
        modelId=model,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": int(max_tokens)},
    )
    parts = (response.get("output", {}).get("message", {}).get("content", []))
    return "".join(part.get("text", "") for part in parts
                   if isinstance(part, dict))


class BedrockInferenceProcessor:
    """Runs a compiled document's ``bedrock_inference`` bindings.

    Called by the WorkflowExecutor after a successful pipeline run and
    before the run is finalized (so the merged metadata reaches the
    post-run output bindings). The invoker is injectable so tests run
    without boto3 or network access.
    """

    def __init__(self, invoker: Optional[Callable] = None) -> None:
        self._invoker = invoker or _default_bedrock_invoker

    def bindings(self, document: dict) -> List[dict]:
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == BINDING_BEDROCK_INFERENCE
        ]

    def process(
        self, document: dict, tag_values: dict, work_dir: Optional[str]
    ) -> Dict[str, Any]:
        """Run every bedrock_inference binding and return the run's
        inference metadata with the parsed fields merged in. Raises
        :class:`BedrockInferenceError` naming the failing node."""
        metadata = dict(tag_values or {})
        for binding in self.bindings(document):
            node_id = binding.get("nodeId")
            try:
                metadata.update(self._run_one(binding, work_dir))
                logger.info(
                    "Bedrock inference binding (node %s) processed", node_id
                )
            except BedrockInferenceError:
                raise
            except Exception as e:  # noqa: BLE001 - re-raised with the node
                raise BedrockInferenceError(
                    node_id,
                    "Bedrock inference node '{0}' failed: {1}".format(
                        node_id, e),
                ) from e
        return metadata

    def _run_one(self, binding: dict, work_dir: Optional[str]) -> Dict[str, Any]:
        node_id = binding.get("nodeId")
        parameters = dict(binding.get("parameters") or {})
        images = []
        for port, label in (("in", "Input image"),
                            ("reference", "Reference image")):
            path = (binding.get("capturePaths") or {}).get(port)
            if not path:
                raise BedrockInferenceError(
                    node_id,
                    "Bedrock inference node '{0}' has no captured frame for "
                    "its '{1}' input (the port is not fed by any video "
                    "source)".format(node_id, port))
            if work_dir:
                path = path.replace("{work_dir}", work_dir)
            try:
                with open(path, "rb") as f:
                    images.append((label, f.read()))
            except OSError as e:
                raise BedrockInferenceError(
                    node_id,
                    "Bedrock inference node '{0}' could not read the "
                    "captured '{1}' frame from {2}: {3}".format(
                        node_id, port, path, e)) from e

        answer = self._invoker(
            str(parameters.get("model") or BEDROCK_DEFAULT_MODEL),
            str(parameters.get("prompt") or ""),
            images,
            str(parameters.get("region") or "us-east-1"),
            int(parameters.get("max_tokens") or 256),
        )
        return parse_bedrock_answer(answer)


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class OutputBindingProcessor:
    """Post-run handler processing a compiled document's executorBindings.

    Matches the ``PostRunHandler`` signature from ``pipeline_executor``:
    ``(registration, compiled_document, tag_values) -> None``. Clients
    are injectable so tests run without GPIO, a broker, or an OPC UA
    server.
    """

    def __init__(
        self,
        dio_actuator: Optional[Callable] = None,
        mqtt_publisher: Optional[Callable] = None,
        opcua_writer: Optional[Callable] = None,
    ) -> None:
        self._dio_actuator = dio_actuator or _default_dio_actuator
        self._mqtt_publisher = mqtt_publisher or _default_mqtt_publisher
        self._opcua_writer = opcua_writer or _default_opcua_writer

    def __call__(self, registration, document: dict, tag_values: dict) -> None:
        self.process(registration, document, tag_values)

    def process(self, registration, document: dict, tag_values: dict) -> None:
        """Process every output binding independently (Requirement 13.7)."""
        bindings = document.get("executorBindings") or []
        if not bindings:
            return
        metadata = {
            key: _coerce(value) for key, value in dict(tag_values or {}).items()
        }
        filter_outcomes = self._evaluate_filters(bindings, metadata)
        conditional_allowed = self._evaluate_conditionals(bindings, metadata)

        for binding in bindings:
            kind = binding.get("binding")
            node_id = binding.get("nodeId")
            try:
                if kind == BINDING_INFERENCE_FILTER:
                    continue  # evaluated above; gates outputs, no action
                if kind == BINDING_CONDITIONAL:
                    continue  # evaluated above; routes/gates, no action
                if kind == BINDING_DIGITAL_INPUT:
                    continue  # input side; not an output binding
                if kind == BINDING_BEDROCK_INFERENCE:
                    continue  # ran before this processor; fields merged
                if kind == BINDING_DIGITAL_OUTPUT:
                    runner = self._run_digital_output
                elif kind == BINDING_MQTT_PUBLISH:
                    runner = self._run_mqtt_publish
                elif kind == BINDING_OPCUA_WRITE:
                    runner = self._run_opcua_write
                else:
                    logger.debug(
                        "Skipping unknown executor binding %r (node %s)",
                        kind, node_id,
                    )
                    continue

                if self._gated_out(binding, filter_outcomes, conditional_allowed):
                    logger.info(
                        "Output binding %s (node %s) gated out by an "
                        "upstream inference filter or conditional", kind, node_id,
                    )
                    continue
                if not self._condition_allows(binding, metadata):
                    continue

                runner(dict(binding.get("parameters") or {}), metadata)
                logger.info(
                    "Output binding %s (node %s) processed", kind, node_id
                )
            except Exception:  # noqa: BLE001 - contained per 13.7
                logger.exception(
                    "Output binding %s (node %s) failed; other bindings "
                    "are unaffected", kind, node_id,
                )

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_filters(
        bindings: List[dict], metadata: Dict[str, Any]
    ) -> Dict[Any, Optional[bool]]:
        """Outcome per inference_filter node: True/False, or None when
        the condition could not be evaluated (which gates like False —
        never actuate on an unevaluable rule)."""
        outcomes: Dict[Any, Optional[bool]] = {}
        for binding in bindings:
            if binding.get("binding") != BINDING_INFERENCE_FILTER:
                continue
            node_id = binding.get("nodeId")
            condition = str(
                (binding.get("parameters") or {}).get("condition", "") or "")
            try:
                outcomes[node_id] = evaluate_condition(condition, metadata)
            except ValueError as e:
                outcomes[node_id] = None
                logger.error(
                    "Inference filter condition on node %s could not be "
                    "evaluated: %s", node_id, e,
                )
        return outcomes


    @staticmethod
    def _evaluate_conditionals(
        bindings: List[dict], metadata: Dict[str, Any]
    ) -> Dict[Any, set]:
        """Allowed downstream node ids per conditional node: the union of the
        downstream node ids of every output port whose gate condition
        (compiler ``portConditions``: "true" = the condition, "false" =
        its negation) evaluated True. A port whose condition cannot be
        evaluated allows nothing — with both ports negations of the same
        expression, an unevaluable condition gates both sides (never
        actuate on an unevaluable rule)."""
        allowed: Dict[Any, set] = {}
        for binding in bindings:
            if binding.get("binding") != BINDING_CONDITIONAL:
                continue
            node_id = binding.get("nodeId")
            port_conditions = binding.get("portConditions") or {}
            by_port = binding.get("downstreamNodeIdsByPort") or {}
            passing: set = set()
            for port, condition in port_conditions.items():
                try:
                    outcome = evaluate_condition(str(condition), metadata)
                except ValueError as e:
                    logger.error(
                        "Conditional condition for port %r on node %s "
                        "could not be evaluated: %s", port, node_id, e,
                    )
                    continue
                if outcome:
                    passing.update(by_port.get(port) or [])
            allowed[node_id] = passing
        return allowed

    @staticmethod
    def _gated_out(
        binding: dict,
        filter_outcomes: Dict[Any, Optional[bool]],
        conditional_allowed: Dict[Any, set],
    ) -> bool:
        """True when any directly-upstream inference filter did not pass,
        or a directly-upstream conditional did not route to this node."""
        node_id = binding.get("nodeId")
        for upstream in binding.get("upstreamNodeIds") or []:
            if upstream in filter_outcomes and filter_outcomes[upstream] is not True:
                return True
            if upstream in conditional_allowed and node_id not in conditional_allowed[upstream]:
                return True
        return False

    @staticmethod
    def _condition_allows(binding: dict, metadata: Dict[str, Any]) -> bool:
        """Evaluate the binding's own condition parameter, when present
        (digital_output's actuation rule, Requirement 9.4). An absent
        condition allows; an unevaluable one blocks."""
        condition = (binding.get("parameters") or {}).get("condition")
        if not condition:
            return True
        node_id = binding.get("nodeId")
        try:
            result = evaluate_condition(str(condition), metadata)
        except ValueError as e:
            logger.error(
                "Output condition on node %s could not be evaluated: %s; "
                "not actuating", node_id, e,
            )
            return False
        if not result:
            logger.info(
                "Output condition on node %s evaluated false; not actuating",
                node_id,
            )
        return result

    # ------------------------------------------------------------------
    # Binding runners
    # ------------------------------------------------------------------

    def _run_digital_output(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> None:
        """Actuate the configured pin/signal type/pulse width (9.4)."""
        self._dio_actuator(
            parameters["pin"],
            str(parameters.get("signal_type", SIGNAL_TYPE_PULSE)),
            parameters.get("pulse_width_ms", 100),
        )

    def _run_mqtt_publish(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> None:
        """Publish the rendered payload to the configured broker (9.5).

        With ``aws_iot`` enabled the publish targets AWS IoT Core over
        mutual TLS: the thing name becomes the MQTT client id, the
        device-local certificate paths are passed as ``tls_set``
        arguments, a ``broker_port`` left at the plain-MQTT default
        (1883) switches to the standard mutual-TLS port (8883), and the
        qos is clamped to 1 (AWS IoT Core does not support QoS 2).
        """
        payload = render_template(
            str(parameters.get("payload_template") or "{inference_json}"),
            metadata,
        )
        payload_text = (
            payload if isinstance(payload, str)
            else json.dumps(payload, default=str)
        )
        host = str(parameters["broker_host"])
        port = int(parameters.get("broker_port", DEFAULT_MQTT_PORT))
        topic = str(parameters["topic"])
        qos = int(parameters.get("qos", 0))

        if not parameters.get("aws_iot"):
            self._mqtt_publisher(host, port, topic, payload_text, qos)
            return

        missing = [
            name for name in AWS_IOT_REQUIRED_PARAMETERS
            if not parameters.get(name)
        ]
        if missing:
            raise ValueError(
                "AWS IoT publishing requires {0}".format(", ".join(missing)))
        if port == DEFAULT_MQTT_PORT:
            port = AWS_IOT_TLS_PORT
        qos = min(qos, AWS_IOT_MAX_QOS)
        tls = {
            "ca_certs": str(parameters["iot_ca_cert_path"]),
            "certfile": str(parameters["iot_client_cert_path"]),
            "keyfile": str(parameters["iot_private_key_path"]),
        }
        self._mqtt_publisher(
            host, port, topic, payload_text, qos,
            str(parameters["iot_thing_name"]), tls,
        )

    def _run_opcua_write(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> None:
        """Write the rendered value to the configured server node (9.6)."""
        value = render_template(
            parameters.get("value_template") or "{is_anomalous}", metadata
        )
        self._opcua_writer(
            str(parameters["endpoint"]), str(parameters["node_id"]), value
        )
