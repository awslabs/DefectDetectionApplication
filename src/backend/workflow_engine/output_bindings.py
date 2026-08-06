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
  packages as a Python dependency (Requirement 9.6);
- writes ``modbus_write`` bindings' rendered value to the configured
  coil or holding register on a Modbus TCP server through the stdlib
  ``modbus_tcp`` client (modbus-tcp-output feature, Requirements
  4.1-4.9).

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

from workflow_engine.llm_inference import (
    UnresolvedPlaceholderError,
    render_prompt,
)

logger = logging.getLogger(__name__)

#: Binding kinds this processor knows about.
BINDING_INFERENCE_FILTER = "inference_filter"
BINDING_CONDITIONAL = "conditional"
BINDING_DIGITAL_OUTPUT = "digital_output"
BINDING_MQTT_PUBLISH = "mqtt_publish"
BINDING_OPCUA_WRITE = "opcua_write"
BINDING_MODBUS_WRITE = "modbus_write"
BINDING_DIGITAL_INPUT = "digital_input"
#: Handled by BedrockInferenceProcessor BEFORE the output bindings run
#: (the pipeline executor merges its result into the tag values this
#: processor gates on); skipped here.
BINDING_BEDROCK_INFERENCE = "bedrock_inference"
#: Handled by LlmInferenceProcessor BEFORE the output bindings run (the
#: pipeline executor merges its result into the tag values this
#: processor gates on); skipped here. The simulation stub binding
#: ``sim_llm_inference`` never appears in device documents and falls
#: through the unknown-binding skip (a no-op on device).
BINDING_LLM_INFERENCE = "llm_inference"


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


#: Bound on the payload/value preview recorded into a node's sent-message
#: detail (output-node-sent-message feature). The record is a preview, not an
#: archive (Requirement 2.3).
DETAIL_PREVIEW_LIMIT = 512


def _preview(text: str, limit: int = DETAIL_PREVIEW_LIMIT) -> str:
    """Truncate ``text`` to ``limit`` chars, appending an ellipsis marker when
    it is longer. Returns the text unchanged when it fits."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\u2026"


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


#: Greengrass IPC PublishToIoTCore supports only QoS 0 and 1; a
#: configured qos is clamped to this maximum (mirrors the AWS IoT Core
#: mutual-TLS path, which also caps at QoS 1).
GREENGRASS_MAX_QOS = 1


def _default_greengrass_publisher(topic: str, payload: str, qos: int) -> None:
    """Publish one message through the device's Greengrass-managed MQTT
    via the Greengrass IPC ``PublishToIoTCore`` operation.

    Zero-configuration publishing: the on-device Greengrass nucleus owns
    the connection to AWS IoT Core, so only the topic/payload/qos are
    supplied (no broker host/port and no certificate file paths). The
    ``awsiot.greengrasscoreipc`` client is imported lazily so this module
    stays importable everywhere (matching the paho/opcua pattern);
    ``qos`` is mapped to the IPC ``QOS`` enum (clamped to 0/1).

    A nucleus denial (``UnauthorizedError``) is re-raised as a
    ``RuntimeError`` naming the denied topic and the LocalServer
    component's ``aws.greengrass.ipc.mqttproxy`` accessControl
    configuration (with the recipe location) so the run error is
    actionable instead of a bare ``UnauthorizedError``."""
    import awsiot.greengrasscoreipc
    import awsiot.greengrasscoreipc.model as model

    qos_value = model.QOS.AT_LEAST_ONCE if int(qos) >= GREENGRASS_MAX_QOS \
        else model.QOS.AT_MOST_ONCE
    request = model.PublishToIoTCoreRequest()
    request.topic_name = topic
    request.payload = payload.encode("utf-8")
    request.qos = qos_value

    ipc_client = awsiot.greengrasscoreipc.connect()
    operation = ipc_client.new_publish_to_iot_core()
    operation.activate(request)
    try:
        operation.get_response().result(timeout=10.0)
    except model.UnauthorizedError as error:
        raise RuntimeError(
            "Greengrass IPC denied PublishToIoTCore for topic "
            "'{0}': the LocalServer component's "
            "aws.greengrass.ipc.mqttproxy accessControl configuration "
            "does not authorize publishing to this topic. Add (or fix) a "
            "policy entry authorizing 'aws.greengrass#PublishToIoTCore' "
            "on a resource covering the topic in the component recipe "
            "(recipe-arm64-jp6.yaml / recipe-arm64-jp5.yaml / "
            "recipe-arm64.yaml / recipe-amd64.yaml, "
            "ComponentConfiguration accessControl) and redeploy.".format(
                topic)
        ) from error


def _opcua_coerce(value: Any, variant_type: Any) -> Any:
    """Coerce a rendered binding value to the target node's OPC UA type.

    A ``value_template`` such as ``"{is_anomalous}"`` renders the value's
    native Python type (e.g. the int ``1``), but the server node may be a
    Boolean/float/string tag. Writing a mismatched Python type makes the
    server reject the write with ``BadTypeMismatch`` (e.g. Int64 -> Boolean
    tag). Map the value to the node's declared variant type so an int/str
    writes cleanly to a Boolean/numeric/string tag. ``_coerce`` first
    normalizes numeric/boolean strings ("true"/"1" -> True/1). Unknown
    variant types pass the value through unchanged.
    """
    from opcua import ua

    normalized = _coerce(value)
    vt = ua.VariantType
    boolean_types = {vt.Boolean}
    integer_types = {
        vt.SByte, vt.Byte, vt.Int16, vt.UInt16, vt.Int32, vt.UInt32,
        vt.Int64, vt.UInt64,
    }
    float_types = {vt.Float, vt.Double}
    try:
        if variant_type in boolean_types:
            return bool(normalized)
        if variant_type in integer_types:
            return int(normalized)
        if variant_type in float_types:
            return float(normalized)
        if variant_type == vt.String:
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        # Fall through to the raw value; the server will surface any real
        # incompatibility rather than us masking it with a bad cast.
        return value
    return value


#: opcua_write authentication/security parameters (all optional). Presence of
#: any switches the client off anonymous access.
OPCUA_SECURITY_PARAMS = (
    "username",
    "password",
    "security_policy",     # e.g. "Basic256Sha256"
    "security_mode",       # "Sign" | "SignAndEncrypt" (default when a policy set)
    "client_cert_path",    # application instance certificate (PEM/DER)
    "client_key_path",     # its private key
    "server_cert_path",    # optional pinned server certificate
)


def _opcua_security_from_params(parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract OPC UA auth/security settings from a binding's parameters.

    Returns a dict of the configured settings, or None when none are set (the
    anonymous, no-security default). Supports user-token auth
    (``username``/``password``) and/or certificate-based security
    (``security_policy`` + ``client_cert_path`` + ``client_key_path``, with an
    optional ``security_mode`` and pinned ``server_cert_path``).
    """
    security = {
        key: parameters[key]
        for key in OPCUA_SECURITY_PARAMS
        if parameters.get(key) not in (None, "")
    }
    return security or None


def _default_opcua_writer(
    endpoint: str, node_id: str, value: Any, security: Optional[Dict[str, Any]] = None
) -> None:
    """Write a value to an OPC UA server node through the ``opcua``
    client the Workflow_Component packages (Requirement 9.6).

    The rendered value is coerced to the node's declared data type so a
    templated int/string (e.g. ``is_anomalous`` -> 1) writes cleanly to a
    Boolean/numeric/string tag instead of failing with ``BadTypeMismatch``.

    ``security`` (optional) authenticates the session: ``username``/``password``
    for user-token auth and/or ``security_policy`` + ``client_cert_path`` +
    ``client_key_path`` (with optional ``security_mode`` defaulting to
    ``SignAndEncrypt`` and an optional pinned ``server_cert_path``) for
    certificate-based signing/encryption. Absent => anonymous, no security.
    """
    try:
        from opcua import Client
    except ImportError as e:
        raise RuntimeError(
            "The 'opcua' Python package is not available; it is delivered "
            "as a Workflow_Component dependency"
        ) from e
    # ``ua`` is only needed for the typed write; tolerate its absence and
    # fall back to a native write so the node data type can't wedge us.
    try:
        from opcua import ua
    except ImportError:
        ua = None

    client = Client(endpoint)
    if security:
        username = security.get("username")
        if username:
            client.set_user(str(username))
            password = security.get("password")
            if password is not None:
                client.set_password(str(password))
        policy = security.get("security_policy")
        cert = security.get("client_cert_path")
        key = security.get("client_key_path")
        if policy and cert and key:
            # opcua set_security_string format:
            # "<Policy>,<Mode>,<client_cert>,<client_key>[,<server_cert>]"
            mode = security.get("security_mode") or "SignAndEncrypt"
            parts = [str(policy), str(mode), str(cert), str(key)]
            server_cert = security.get("server_cert_path")
            if server_cert:
                parts.append(str(server_cert))
            client.set_security_string(",".join(parts))
    client.connect()
    try:
        node = client.get_node(node_id)
        variant_type = None
        if ua is not None:
            try:
                variant_type = node.get_data_type_as_variant_type()
            except Exception:
                # Could not resolve the node's type; fall back below.
                variant_type = None
        if variant_type is not None:
            coerced = _opcua_coerce(value, variant_type)
            node.set_value(ua.DataValue(ua.Variant(coerced, variant_type)))
        else:
            # Prior behavior: native write (the server surfaces any real
            # incompatibility).
            node.set_value(value)
    finally:
        client.disconnect()


#: Catalog register types for modbus_write bindings.
REGISTER_TYPE_COIL = "coil"
REGISTER_TYPE_HOLDING_REGISTER = "holding_register"

#: The catalog default Modbus TCP port.
DEFAULT_MODBUS_PORT = 502

#: Permitted holding-register value range (16-bit, Requirement 4.4).
MODBUS_REGISTER_MIN = 0
MODBUS_REGISTER_MAX = 65535


def _default_modbus_writer(
    host: str,
    port: int,
    unit_id: int,
    register_type: str,
    address: int,
    value: Any,
    pulse_ms: int,
) -> None:
    """Write one coil or holding register on a Modbus TCP server through
    the stdlib ``modbus_tcp`` client (modbus-tcp-output feature,
    Requirements 4.1, 4.5, 4.8).

    Production default for the :class:`OutputBindingProcessor`
    ``modbus_writer`` seam (Requirement 4.6). ``modbus_tcp`` is imported
    lazily, matching the paho/opcua convention, so this module stays
    importable everywhere.

    - ``coil`` with ``pulse_ms == 0``: one Write Single Coil latching the
      boolean state (Requirement 4.1).
    - ``coil`` with ``pulse_ms > 0``: write the state, wait ``pulse_ms``,
      write the inverse — the same in-process ``time.sleep`` pattern
      ``_default_dio_actuator`` uses (Requirement 4.5). If the inverse
      write fails, the error says the coil may be left latched so the
      operator knows the physical state is indeterminate.
    - ``holding_register``: one Write Single Register with the validated
      integer (Requirement 4.1).

    Connection failures are wrapped with the ``host:port`` target so the
    run error is actionable (Requirement 4.8; ``modbus_tcp``'s own
    timeout errors already name the host and port).
    """
    from workflow_engine import modbus_tcp

    host = str(host)
    port = int(port)
    unit_id = int(unit_id)
    address = int(address)
    pulse_ms = int(pulse_ms or 0)

    def _write(function_code: int, wire_value: int, action: str) -> None:
        try:
            modbus_tcp.write_single(
                host, port, unit_id, function_code, address, wire_value
            )
        except modbus_tcp.ModbusError:
            # Already actionable (exception meaning / timeout with
            # host:port); the pulse inverse write adds its own context
            # below.
            raise
        except (ConnectionError, OSError) as exc:
            raise RuntimeError(
                "Modbus TCP {0} to {1}:{2} failed: {3}".format(
                    action, host, port, exc)
            ) from exc

    if register_type == REGISTER_TYPE_COIL:
        state = bool(value)
        wire_on = modbus_tcp.COIL_ON if state else modbus_tcp.COIL_OFF
        wire_off = modbus_tcp.COIL_OFF if state else modbus_tcp.COIL_ON
        _write(
            modbus_tcp.FUNCTION_WRITE_SINGLE_COIL, wire_on, "coil write"
        )
        if pulse_ms > 0:
            time.sleep(pulse_ms / 1000.0)
            try:
                _write(
                    modbus_tcp.FUNCTION_WRITE_SINGLE_COIL,
                    wire_off,
                    "coil pulse inverse write",
                )
            except (modbus_tcp.ModbusError, RuntimeError) as exc:
                raise RuntimeError(
                    "Modbus TCP coil pulse inverse write to {0}:{1} "
                    "failed (the coil at address {2} may be left "
                    "latched): {3}".format(host, port, address, exc)
                ) from exc
    elif register_type == REGISTER_TYPE_HOLDING_REGISTER:
        _write(
            modbus_tcp.FUNCTION_WRITE_SINGLE_REGISTER,
            int(value),
            "holding-register write",
        )
    else:
        raise ValueError(
            "Unknown Modbus register type {0!r}".format(register_type))


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

#: Canonical JSON-format instruction the executor appends to every
#: anomaly-mode prompt (single source of truth for the verdict answer
#: contract): a user-customized prompt can no longer break
#: ``parse_bedrock_answer`` by omitting the JSON shape.
BEDROCK_JSON_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'
)


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
                result = self._run_one(binding, work_dir)
                # Freeform-mode results carry a nested 'bedrock' sub-dict
                # ({node_id: {"text": ...}}): merge it into any existing
                # 'bedrock' entry so multiple freeform nodes in one
                # document keep their per-node entries (the flat
                # 'bedrock_text' key, like flat is_anomalous/confidence,
                # is overwritten by later nodes — the nested keys
                # disambiguate).
                nested = result.pop("bedrock", None)
                metadata.update(result)
                if nested:
                    merged = dict(metadata.get("bedrock") or {})
                    merged.update(nested)
                    metadata["bedrock"] = merged
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
        capture_paths = binding.get("capturePaths") or {}
        images = []

        # The 'in' (primary) frame is required: a missing path or an
        # unreadable file fails the node with the existing surfacing.
        port = "in"
        path = capture_paths.get(port)
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
                images.append(("Input image", f.read()))
        except OSError as e:
            raise BedrockInferenceError(
                node_id,
                "Bedrock inference node '{0}' could not read the "
                "captured '{1}' frame from {2}: {3}".format(
                    node_id, port, path, e)) from e

        # The 'reference' frame is optional: the portal compiler emits
        # capturePaths.reference = None when the port is not fed by any
        # video source. When the reference frame is unavailable for any
        # reason, log the omission and proceed with single-image
        # inference on the primary frame alone.
        reference_path = capture_paths.get("reference")
        if not reference_path:
            logger.warning(
                "Bedrock inference node '%s': reference port not fed by "
                "any video source; performing single-image inference",
                node_id)
        else:
            if work_dir:
                reference_path = reference_path.replace(
                    "{work_dir}", work_dir)
            try:
                with open(reference_path, "rb") as f:
                    images.append(("Reference image", f.read()))
            except OSError as e:
                logger.warning(
                    "Bedrock inference node '%s': could not read the "
                    "captured 'reference' frame from %s (%s); performing "
                    "single-image inference", node_id, reference_path, e)

        # Response mode: anomaly (default — absent/None) demands the JSON
        # verdict and gets the canonical instruction appended; freeform
        # (anomaly_mode: false) sends the prompt as-is and records the
        # raw answer text (no parsing, answer format never fails).
        # Anomaly mode ALSO records the raw answer text under the same
        # keys freeform uses (bedrock-response-mode Requirement 5), so
        # prompts asking for notes alongside the verdict don't silently
        # lose that text from the run metadata.
        anomaly_mode = _coerce(parameters.get("anomaly_mode"))
        anomaly_mode = True if anomaly_mode is None else bool(anomaly_mode)

        prompt = str(parameters.get("prompt") or "")
        if anomaly_mode:
            prompt = prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION

        answer = self._invoker(
            str(parameters.get("model") or BEDROCK_DEFAULT_MODEL),
            prompt,
            images,
            str(parameters.get("region") or "us-east-1"),
            int(parameters.get("max_tokens") or 256),
        )
        if anomaly_mode:
            # An unparseable answer raises here — the existing
            # BedrockInferenceError path — before any text is recorded
            # (Requirement 5.3: the error message carries the excerpt).
            verdict = parse_bedrock_answer(answer)
            verdict["bedrock_text"] = answer
            verdict["bedrock"] = {node_id: {"text": answer}}
            return verdict
        return {
            "bedrock_text": answer,
            "bedrock": {node_id: {"text": answer}},
        }


# ---------------------------------------------------------------------------
# LLM text-generation inference (llm_inference bindings)
#
# vllm-triton-inference Requirements 7.3–7.7. Runs BEFORE the
# gating/output bindings evaluate (and after the Bedrock processor, so
# prompts can reference Bedrock-produced fields): per binding the
# processor renders the Prompt_Template from the run's inference
# metadata (strict {placeholder} substitution — llm_inference.render_prompt)
# and calls the device Text_Generation_API with the bound model name and
# generation parameters, merging the outcome under
# metadata['llm'][nodeId]. Unlike Bedrock, a binding failure is recorded
# ({'error': reason}), never raised: remaining bindings and the run's
# independent nodes continue, and the merged metadata (text or error)
# reaches downstream filters, conditionals, outputs, and custom Python
# through the existing metadata flow.
# ---------------------------------------------------------------------------

#: The device-local Text_Generation_API generate route (LocalServer's
#: own plaintext listener). Like every other device router, the route
#: carries no ``/api`` prefix — that form exists only behind the
#: frontend proxy (see ``endpoints/text_generation.py`` and the router
#: registration in ``app.py``).
TEXT_GENERATION_URL = (
    "http://localhost:5000/text-generation/{model_name}/generate"
)

#: Client-side wall-clock timeout for one generate call. The API's own
#: request timeout defaults to 120 s (TEXT_GEN_TIMEOUT_SECONDS), so the
#: margin lets its 504 (model name + timeout indication) arrive as the
#: recorded failure reason instead of a bare client timeout.
LLM_GENERATION_TIMEOUT_SEC = 130

#: Poll interval between generate re-POSTs while the model reports 409
#: state='loading' (transient warm-up — the API answers 409 with a
#: ``state`` field precisely so callers can distinguish a warming model
#: from a failed one; see ``vllm_runtime/server.py``).
LLM_LOADING_POLL_INTERVAL_SEC = 5

#: Wall-clock budget for the 409-loading retry window: comfortably above
#: small-model warm-up, bounded well below the executor thread's
#: tolerance. Once exhausted, the last 409-loading response is raised as
#: the node's terminal error.
LLM_LOADING_BUDGET_SEC = 240

#: Generation parameters forwarded from the compiled binding to the API.
_LLM_GENERATION_PARAMETERS = ("max_tokens", "temperature", "top_p")


def _default_llm_invoker(
    model_name: str, prompt: str, parameters: Dict[str, Any]
) -> str:
    """POST the rendered prompt to the local Text_Generation_API and
    return the generated text. ``requests`` is imported lazily so this
    module stays importable everywhere; any HTTP/validation failure is
    raised for the processor to record as the node's error.

    A ``409 {'state': 'loading'}`` response (transient model warm-up) is
    re-POSTed every :data:`LLM_LOADING_POLL_INTERVAL_SEC` seconds until
    :data:`LLM_LOADING_BUDGET_SEC` of wall clock elapses; the first 200
    wins. A 409 with any other state, any other non-200, or budget
    exhaustion raises the existing RuntimeError shape (the last state
    payload included). The 200-first-attempt path stays a single POST
    with the original URL, body, and timeout."""
    import requests

    body: Dict[str, Any] = {"prompt": prompt}
    for key in _LLM_GENERATION_PARAMETERS:
        if parameters.get(key) is not None:
            body[key] = parameters[key]
    url = TEXT_GENERATION_URL.format(model_name=model_name)
    deadline = time.monotonic() + LLM_LOADING_BUDGET_SEC
    while True:
        response = requests.post(
            url,
            json=body,
            timeout=LLM_GENERATION_TIMEOUT_SEC,
        )
        if response.status_code == 200:
            return str(response.json().get("generated_text", ""))
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if (
            response.status_code == 409
            and isinstance(payload, dict)
            and payload.get("state") == "loading"
            and time.monotonic() < deadline
        ):
            # Transient warm-up: wait for READY within the bounded budget.
            logger.info(
                "Text_Generation_API model '%s' is loading; retrying in "
                "%ds", model_name, LLM_LOADING_POLL_INTERVAL_SEC,
            )
            time.sleep(LLM_LOADING_POLL_INTERVAL_SEC)
            continue
        raise RuntimeError(
            "Text_Generation_API returned {0} for model '{1}': {2}".format(
                response.status_code, model_name,
                payload if payload is not None else (response.text or "")[:200],
            ))


class LlmInferenceProcessor:
    """Runs a compiled document's ``llm_inference`` bindings.

    Called by the WorkflowExecutor after a successful pipeline run —
    after the Bedrock processor and before the output bindings evaluate.
    The invoker is injectable so tests run without HTTP.
    """

    def __init__(self, invoker: Optional[Callable] = None) -> None:
        self._invoker = invoker or _default_llm_invoker

    def bindings(self, document: dict) -> List[dict]:
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == BINDING_LLM_INFERENCE
        ]

    def process(self, document: dict, tag_values: dict) -> Dict[str, Any]:
        """Run every llm_inference binding and return the run's inference
        metadata with each node's outcome merged under
        ``metadata['llm'][nodeId]`` (Requirements 7.4, 7.7).

        Never raises: a binding failure — unresolved placeholder (7.5)
        or API error/timeout (7.6) — is recorded as that node's
        ``{'error': reason}`` and processing continues with the
        remaining bindings. Results merge progressively, so a later
        binding's prompt may reference an earlier node's output (for
        example ``{llm.node1.generated_text}``).

        Anomaly-mode parity (vlm-parity-run-results Requirement 1.2):
        when a node's outcome carries a parsed verdict, its
        ``is_anomalous``/``confidence`` are ALSO merged FLAT into the
        metadata — exactly like Bedrock's — so downstream filters,
        conditionals, and outputs gate on them. The nested
        ``llm[nodeId]`` record stays complete (verdict keys included).
        Flat keys follow the documented last-writer-wins convention."""
        metadata = dict(tag_values or {})
        bindings = self.bindings(document)
        if not bindings:
            return metadata
        metadata["llm"] = dict(metadata.get("llm") or {})
        for binding in bindings:
            node_id = binding.get("nodeId")
            outcome = self._run_one(binding, metadata)
            metadata["llm"][node_id] = outcome
            for key in ("is_anomalous", "confidence"):
                if key in outcome:
                    metadata[key] = outcome[key]
        return metadata

    def _run_one(
        self, binding: dict, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        node_id = binding.get("nodeId")
        parameters = dict(binding.get("parameters") or {})
        try:
            prompt = render_prompt(
                str(parameters.get("prompt_template") or ""), metadata
            )
        except UnresolvedPlaceholderError as e:
            # No API call for an unrenderable prompt (Requirement 7.5).
            logger.error(
                "LLM inference node %s failed: unresolved placeholder %s",
                node_id, e.name,
            )
            return {"error": "unresolved placeholder {0}".format(e.name)}
        # Anomaly-mode parity with Bedrock (vlm-parity-run-results
        # Requirement 1): a truthy ``anomaly_mode`` appends the
        # canonical JSON instruction to the RENDERED prompt and parses
        # the answer with the shared verdict parser. Unlike Bedrock's
        # default-True, absent/None/false stays today's freeform path
        # byte-identical (Requirement 1.4).
        anomaly_mode = bool(_coerce(parameters.get("anomaly_mode")))
        if anomaly_mode:
            prompt = prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION
        try:
            text = self._invoker(
                str(parameters.get("modelName") or ""), prompt, parameters
            )
        except Exception as e:  # noqa: BLE001 - recorded per 7.6, not raised
            logger.error(
                "LLM inference node %s failed: %s; other bindings are "
                "unaffected", node_id, e,
            )
            return {"error": str(e)}
        if anomaly_mode:
            try:
                verdict = parse_bedrock_answer(text)
            except ValueError as e:
                # Recorded, never raised — the llm containment contract
                # (Requirement 1.3). The parser's message carries the
                # answer excerpt; the raw text is still recorded so the
                # run metadata keeps what the model said.
                logger.error(
                    "LLM inference node %s failed: %s; other bindings "
                    "are unaffected", node_id, e,
                )
                return {"error": str(e), "generated_text": text}
            logger.info("LLM inference binding (node %s) processed", node_id)
            outcome = {"generated_text": text}
            outcome.update(verdict)
            return outcome
        logger.info("LLM inference binding (node %s) processed", node_id)
        return {"generated_text": text}


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class OutputBindingError(Exception):
    """One or more output bindings failed during
    :meth:`OutputBindingProcessor.process`.

    Every binding is still attempted (Requirement 13.7): failures are
    collected inside the per-binding ``try/except`` and this error is
    raised only after the loop so the executor can mark the run failed
    with the failing node id(s) identified — mirroring
    :class:`BedrockInferenceError`.

    ``node_ids`` carries every failing node id; ``node_id`` is the first
    of them so the executor's existing ``getattr(e, "node_id", None)``
    pattern surfaces a node id (Requirements 2.3, 2.4)."""

    def __init__(self, node_ids: List[str], message: str) -> None:
        super().__init__(message)
        self.node_ids: List[str] = list(node_ids)
        self.node_id: Optional[str] = self.node_ids[0] if self.node_ids else None


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
        greengrass_publisher: Optional[Callable] = None,
        modbus_writer: Optional[Callable] = None,
    ) -> None:
        self._dio_actuator = dio_actuator or _default_dio_actuator
        self._mqtt_publisher = mqtt_publisher or _default_mqtt_publisher
        self._opcua_writer = opcua_writer or _default_opcua_writer
        self._greengrass_publisher = (
            greengrass_publisher or _default_greengrass_publisher
        )
        self._modbus_writer = modbus_writer or _default_modbus_writer

    def __call__(
        self,
        registration,
        document: dict,
        tag_values: dict,
        detail_sink: Optional[Callable[[Optional[str], str], None]] = None,
    ) -> None:
        self.process(registration, document, tag_values, detail_sink=detail_sink)

    @staticmethod
    def _emit_detail(
        detail_sink: Optional[Callable[[Optional[str], str], None]],
        node_id: Optional[str],
        detail: str,
    ) -> None:
        """Send ``detail`` to ``detail_sink`` (node_id, detail), contained.

        A None sink is a no-op (default behavior byte-identical to today); a
        raising sink is swallowed so detail recording never affects binding
        execution or the OutputBindingError aggregation (Requirements 3.1,
        3.2)."""
        if detail_sink is None:
            return
        try:
            detail_sink(node_id, detail)
        except Exception:  # noqa: BLE001 - recording is best-effort
            logger.debug(
                "Output binding detail_sink raised for node %s; ignored",
                node_id, exc_info=True,
            )

    def process(
        self,
        registration,
        document: dict,
        tag_values: dict,
        detail_sink: Optional[Callable[[Optional[str], str], None]] = None,
    ) -> None:
        """Process every output binding independently (Requirement 13.7).

        ``detail_sink`` (optional; default None → behavior byte-identical to
        today) receives ``(node_id, detail)`` sent-message / skipped-outcome
        summaries for the output-node-sent-message feature. It is invoked only
        AROUND successful runners and on the gated/condition-skip paths, never
        alters control flow, and a raising sink is contained (Requirement
        3.1)."""
        bindings = document.get("executorBindings") or []
        if not bindings:
            return
        metadata = {
            key: _coerce(value) for key, value in dict(tag_values or {}).items()
        }
        filter_outcomes = self._evaluate_filters(bindings, metadata)
        conditional_allowed = self._evaluate_conditionals(bindings, metadata)

        # Collected (node_id, error) for every binding that raised. A
        # gated-out or condition-skipped binding is NOT a failure (it
        # ``continue``s before the runner and never reaches the except).
        failed: List[tuple] = []
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
                if kind == BINDING_LLM_INFERENCE:
                    continue  # ran before this processor; fields merged
                if kind == BINDING_DIGITAL_OUTPUT:
                    runner = self._run_digital_output
                elif kind == BINDING_MQTT_PUBLISH:
                    runner = self._run_mqtt_publish
                elif kind == BINDING_OPCUA_WRITE:
                    runner = self._run_opcua_write
                elif kind == BINDING_MODBUS_WRITE:
                    runner = self._run_modbus_write
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
                    self._emit_detail(
                        detail_sink, node_id,
                        "not sent: gated out by an upstream inference "
                        "filter or conditional",
                    )
                    continue
                allowed, skip_detail = self._condition_result(binding, metadata)
                if not allowed:
                    if skip_detail is not None:
                        self._emit_detail(detail_sink, node_id, skip_detail)
                    continue

                # Record the SUCCESS detail ONLY after the runner returns
                # successfully; on a runner exception nothing new is recorded
                # (mark_failure already captured the error and set_detail
                # refuses to overwrite failure details, Requirement 3.3).
                detail = runner(dict(binding.get("parameters") or {}), metadata)
                logger.info(
                    "Output binding %s (node %s) processed", kind, node_id
                )
                if detail:
                    self._emit_detail(detail_sink, node_id, detail)
            except Exception as e:  # noqa: BLE001 - contained per 13.7
                logger.exception(
                    "Output binding %s (node %s) failed; other bindings "
                    "are unaffected", kind, node_id,
                )
                # Collect, do not short-circuit: the remaining bindings
                # are still attempted (Requirement 13.7).
                failed.append((node_id, str(e)))

        if failed:
            node_ids = [nid for nid, _ in failed]
            summary = "Output binding(s) failed: " + "; ".join(
                "node {0}: {1}".format(nid, err) for nid, err in failed
            )
            raise OutputBindingError(node_ids, summary)

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
    def _condition_result(
        binding: dict, metadata: Dict[str, Any]
    ) -> tuple:
        """Evaluate the binding's own condition parameter, when present
        (digital_output's actuation rule, Requirement 9.4).

        Returns ``(allowed, skip_detail)``: an absent condition allows
        (``(True, None)``); a condition that evaluates false blocks with a
        ``not sent: condition ... evaluated false`` skip detail; an
        unevaluable one blocks with a ``... could not be evaluated`` skip
        detail (never actuate on an unevaluable rule)."""
        condition = (binding.get("parameters") or {}).get("condition")
        if not condition:
            return True, None
        node_id = binding.get("nodeId")
        try:
            result = evaluate_condition(str(condition), metadata)
        except ValueError as e:
            logger.error(
                "Output condition on node %s could not be evaluated: %s; "
                "not actuating", node_id, e,
            )
            return False, (
                "not sent: condition {0!r} could not be evaluated".format(
                    condition)
            )
        if not result:
            logger.info(
                "Output condition on node %s evaluated false; not actuating",
                node_id,
            )
            return False, (
                "not sent: condition {0!r} evaluated false".format(condition)
            )
        return True, None

    # ------------------------------------------------------------------
    # Binding runners
    # ------------------------------------------------------------------

    def _run_digital_output(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> Optional[str]:
        """Actuate the configured pin/signal type/pulse width (9.4).

        Returns the sent-message detail describing the actuation
        (Requirement 1.3)."""
        pin = parameters["pin"]
        signal_type = str(parameters.get("signal_type", SIGNAL_TYPE_PULSE))
        pulse_width_ms = parameters.get("pulse_width_ms", 100)
        self._dio_actuator(pin, signal_type, pulse_width_ms)
        if signal_type == SIGNAL_TYPE_PULSE:
            return "pulsed pin {0} ({1}ms)".format(pin, pulse_width_ms)
        return "set pin {0} {1}".format(pin, signal_type)

    def _run_mqtt_publish(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> Optional[str]:
        """Publish the rendered payload to the configured broker (9.5).

        With ``greengrass`` enabled the publish is zero-config: it goes
        through the device's Greengrass-managed MQTT (the on-device
        nucleus owns the AWS IoT Core connection), so only the
        topic/payload/qos are used — no broker host/port and no
        certificate paths.

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
        topic = str(parameters["topic"])
        qos = int(parameters.get("qos", 0))

        if parameters.get("greengrass"):
            # Zero-config Greengrass-managed publishing: the on-device
            # nucleus owns the AWS IoT Core connection, so only the
            # topic/payload/qos are supplied (no broker host/port, no
            # certificate paths).
            self._greengrass_publisher(topic, payload_text, qos)
            return self._mqtt_detail(topic, qos, "greengrass", payload_text)

        host = str(parameters["broker_host"])
        port = int(parameters.get("broker_port", DEFAULT_MQTT_PORT))

        if not parameters.get("aws_iot"):
            self._mqtt_publisher(host, port, topic, payload_text, qos)
            return self._mqtt_detail(topic, qos, "plain", payload_text)

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
        return self._mqtt_detail(topic, qos, "aws_iot", payload_text)

    @staticmethod
    def _mqtt_detail(topic: str, qos: int, path: str, payload_text: str) -> str:
        """Compose an mqtt_publish sent-message detail (Requirement 1.1):
        topic, qos, publish path (plain/aws_iot/greengrass), and the rendered
        payload truncated to the preview bound (Requirement 2.3)."""
        return "sent to topic '{0}' (qos {1}, {2}): {3}".format(
            topic, qos, path, _preview(payload_text))

    def _run_opcua_write(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> Optional[str]:
        """Write the rendered value to the configured server node (9.6).

        When authentication parameters are configured (username/password
        and/or a security policy + client certificate), a security config is
        passed to the writer so it can authenticate; otherwise the writer is
        called with the original three-argument (anonymous) signature.

        Returns the sent-message detail carrying the endpoint, node id, and
        the rendered value (Requirement 1.2).
        """
        value = render_template(
            parameters.get("value_template") or "{is_anomalous}", metadata
        )
        endpoint = str(parameters["endpoint"])
        node_id = str(parameters["node_id"])
        security = _opcua_security_from_params(parameters)
        if security:
            self._opcua_writer(endpoint, node_id, value, security)
        else:
            self._opcua_writer(endpoint, node_id, value)
        return "wrote {0} to {1} at {2}".format(
            _preview(repr(value)), node_id, endpoint)

    def _run_modbus_write(
        self, parameters: Dict[str, Any], metadata: Dict[str, Any]
    ) -> Optional[str]:
        """Write the rendered value to the configured coil or holding
        register on a Modbus TCP server (modbus-tcp-output feature,
        Requirements 4.1-4.4, 4.9).

        The ``value_template`` (default ``{is_anomalous}``) is rendered
        over the run's inference metadata; a coil write coerces the
        rendered value to a boolean via the shared ``_coerce``
        normalization (Requirement 4.2), a holding-register write to an
        integer in 0-65535, raising ``ValueError`` — before any write —
        when the value cannot be coerced or is out of range (Requirements
        4.3, 4.4). The injected ``modbus_writer`` performs the exchange
        (Requirement 4.6; pulse semantics live in the writer).

        Returns the sent-message detail naming the written value, the
        register type and address, the host:port, the unit id, and the
        pulse duration when pulsed (Requirement 4.9).
        """
        rendered = render_template(
            parameters.get("value_template") or "{is_anomalous}", metadata
        )
        host = str(parameters["host"])
        port = int(parameters.get("port", DEFAULT_MODBUS_PORT))
        unit_id = int(parameters.get("unit_id", 1))
        register_type = str(
            parameters.get("register_type") or REGISTER_TYPE_COIL)
        address = int(parameters["address"])
        pulse_ms = int(parameters.get("pulse_ms") or 0)

        if register_type == REGISTER_TYPE_COIL:
            value: Any = bool(_coerce(rendered))
        elif register_type == REGISTER_TYPE_HOLDING_REGISTER:
            coerced = _coerce(rendered)
            try:
                value = int(coerced)
            except (TypeError, ValueError):
                raise ValueError(
                    "Modbus holding-register value {0!r} cannot be coerced "
                    "to an integer in {1}-{2}".format(
                        rendered, MODBUS_REGISTER_MIN, MODBUS_REGISTER_MAX)
                ) from None
            if not MODBUS_REGISTER_MIN <= value <= MODBUS_REGISTER_MAX:
                raise ValueError(
                    "Modbus holding-register value {0!r} is outside the "
                    "permitted range {1}-{2}".format(
                        rendered, MODBUS_REGISTER_MIN, MODBUS_REGISTER_MAX))
        else:
            raise ValueError(
                "Unknown Modbus register type {0!r}".format(register_type))

        self._modbus_writer(
            host, port, unit_id, register_type, address, value, pulse_ms
        )
        pulsed = (
            ", pulse {0}ms".format(pulse_ms)
            if register_type == REGISTER_TYPE_COIL and pulse_ms > 0
            else ""
        )
        return "wrote {0} to {1} {2} at {3}:{4} (unit {5}{6})".format(
            _preview(repr(value)), register_type, address, host, port,
            unit_id, pulsed)
