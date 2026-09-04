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

import base64
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from workflow_engine.branching import bedrock_branches
from workflow_engine.detections import METADATA_KEY_DETECTIONS
from workflow_engine.llm_inference import (
    UnresolvedPlaceholderError,
    render_prompt,
)
from workflow_engine.payload_fetch import (
    PayloadReferenceError,
    describe_reference_source,
    fetch_reference_bytes,
    resolve_payload_path,
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
#: Metadata_Node passthrough (workflow-manager-gaps Requirement 7): the
#: compiled entry carries ``metadataMappings`` (trigger-payload field
#: paths -> output metadata keys), ``staticJson`` (a user-supplied JSON
#: object), and ``attachTo`` (the output-category node ids transitively
#: reachable from the Metadata_Node). Resolved by the pure helpers below
#: (``resolve_metadata_binding`` / ``attached_metadata_by_output``); the
#: binding itself performs no action in the output loop.
BINDING_METADATA = "metadata"


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
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
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
        # Identifier: resolve from the inference metadata. A dotted
        # identifier ("bedrock.bedrock_1.is_anomalous") resolves segment
        # by segment against nested dicts/lists, so conditionals and
        # inference filters can reference per-node verdict keys
        # (detection-guided-bedrock-inspection Requirement 4.4). Flat
        # identifiers keep today's exact lookup byte-identical.
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
    return _Parser(
        tokens, {key: _coerce(value) for key, value in metadata.items()}
    ).parse()


# ---------------------------------------------------------------------------
# Template rendering ("{inference_json}", "{is_anomalous}", ...)
# ---------------------------------------------------------------------------

_SINGLE_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")

#: A dotted placeholder ("{bedrock.bedrock_1.is_anomalous}") inside a
#: payload/value template: resolved segment by segment against the
#: nested run metadata (detection-guided-bedrock-inspection Requirement
#: 4.3). Requires at least one dot, so flat placeholders keep today's
#: rendering byte-identical.
_DOTTED_PLACEHOLDER = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\}")


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
    # Dotted per-node keys (detection-guided-bedrock-inspection
    # Requirement 4.3): "{bedrock.bedrock_1.is_anomalous}" resolves
    # segment by segment against the nested metadata (an exact
    # dotted-named key wins over traversal). A template that is exactly
    # one dotted placeholder keeps the value's native type, mirroring
    # the flat single-placeholder rule; inside mixed templates dotted
    # placeholders substitute as strings; an unresolved dotted
    # placeholder degrades to a literal exactly like an unknown flat
    # one. Templates without dotted placeholders render byte-identical
    # to today.
    match = _DOTTED_PLACEHOLDER.fullmatch(template)
    if match:
        if match.group(1) in values:
            return values[match.group(1)]
        found, value = resolve_field_path(values, match.group(1))
        if found:
            return value

    def _resolve_dotted(placeholder: "re.Match") -> str:
        name = placeholder.group(1)
        if name in values:
            return str(values[name])
        found, resolved = resolve_field_path(values, name)
        if found:
            return str(resolved)
        return "{{" + name + "}}"

    template = _DOTTED_PLACEHOLDER.sub(_resolve_dotted, template)
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
# Metadata passthrough resolution (metadata bindings)
#
# workflow-manager-gaps Requirements 7.2-7.6, 7.9. Pure functions: the
# compiled document's ``metadata`` executor bindings carry the mappings
# (trigger-payload field path -> output metadata key), the static JSON
# object, and the ``attachTo`` output-node fan-out; the run's
# Trigger_Context (as loaded by ``pipeline_executor.load_trigger_context``,
# with a pre-parsed ``payload_json``) supplies the resolution source.
# Resolution NEVER raises — a metadata problem degrades (static-only /
# omitted keys) and is logged, and the run always continues.
# ---------------------------------------------------------------------------


def resolve_field_path(document: Any, dotted_path: Any) -> Tuple[bool, Any]:
    """Resolve a dotted field path (``a.b.c``) against a parsed JSON
    document, segment by segment (Requirements 7.2, 7.3).

    - a dict is traversed by key;
    - a list is traversed by a numeric segment used as an index
      (``items.0.id``);
    - any other intermediate value, a missing key, a non-numeric or
      out-of-range index, or an empty path fails the resolution.

    Returns ``(found, value)`` so a resolved JSON ``null`` (``None``) is
    distinguishable from "not found" (Requirement 7.2 attaches resolved
    nulls; Requirement 7.3 omits unresolved keys).
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


def resolve_metadata_binding(
    binding: dict, trigger: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Build one metadata binding's attached map (Requirements 7.2-7.6,
    7.9). Never raises — the run always continues.

    Static JSON entries are attached first (Requirement 7.5). Mappings
    are resolved against ``trigger["payload_json"]`` only when it is a
    dict: a non-JSON trigger payload (``payload_json`` is ``None``), a
    non-object JSON payload, or an absent Trigger_Context (manual run)
    degrades to static-only with one log line (Requirements 7.4, 7.9).
    A resolved mapping (including a resolved ``null``) overrides a
    colliding static entry with a logged collision (Requirement 7.6);
    an unresolved field path omits its key and is logged (Requirement
    7.3).
    """
    attached: Dict[str, Any] = {}
    node_id = None
    try:
        if not isinstance(binding, dict):
            return attached
        node_id = binding.get("nodeId")
        static = binding.get("staticJson")
        static_keys = set()
        if isinstance(static, dict):
            attached.update(static)
            static_keys = set(static)
        mappings = binding.get("metadataMappings")
        if not isinstance(mappings, list) or not mappings:
            return attached
        payload = (
            trigger.get("payload_json") if isinstance(trigger, dict) else None
        )
        if not isinstance(payload, dict):
            # One log line for the whole binding: no JSON-object payload
            # to resolve against (non-JSON payload, non-object JSON, or a
            # run without a Trigger_Context) — static-only attachment.
            logger.info(
                "Metadata node %s: no JSON-object trigger payload to "
                "resolve %d mapping(s) against (non-JSON payload or run "
                "without a trigger context); attaching static JSON "
                "metadata only", node_id, len(mappings),
            )
            return attached
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            field_path = mapping.get("fieldPath")
            key = mapping.get("key")
            if not isinstance(key, str) or not key:
                continue
            found, value = resolve_field_path(payload, field_path)
            if not found:
                logger.info(
                    "Metadata node %s: field path %r did not resolve in "
                    "the trigger payload; omitting metadata key %r",
                    node_id, field_path, key,
                )
                continue
            if key in static_keys:
                logger.info(
                    "Metadata node %s: metadata key %r collision — the "
                    "resolved mapping value overrides the static JSON "
                    "entry", node_id, key,
                )
            attached[key] = value
        return attached
    except Exception:  # noqa: BLE001 - resolution never fails the run
        logger.exception(
            "Metadata node %s: metadata resolution failed; attaching "
            "what was resolved so far", node_id,
        )
        return attached


def attached_metadata_by_output(
    bindings: Optional[List[dict]], trigger: Optional[Dict[str, Any]]
) -> Dict[Any, Dict[str, Any]]:
    """Fan every ``metadata`` binding's attached map out to its
    ``attachTo`` output nodes (Requirements 7.7 scoping input).

    Each metadata binding is evaluated exactly once (via
    :func:`resolve_metadata_binding`) and its map is attached to every
    output node id in its ``attachTo`` list. When several Metadata_Nodes
    attach to the same output, the maps merge in ``executorBindings``
    emission order — a later binding wins a colliding key, logged.
    Never raises.
    """
    attached_by_output: Dict[Any, Dict[str, Any]] = {}
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        if binding.get("binding") != BINDING_METADATA:
            continue
        attached = resolve_metadata_binding(binding, trigger)
        attach_to = binding.get("attachTo")
        if not isinstance(attach_to, list):
            continue
        node_id = binding.get("nodeId")
        for output_node_id in attach_to:
            existing = attached_by_output.get(output_node_id)
            if existing is None:
                attached_by_output[output_node_id] = dict(attached)
                continue
            for key, value in attached.items():
                if key in existing:
                    logger.info(
                        "Metadata node %s: metadata key %r for output "
                        "node %s already attached by an earlier metadata "
                        "binding; the later binding wins",
                        node_id, key, output_node_id,
                    )
                existing[key] = value
    return attached_by_output


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
    model: str, prompt: str, images: List, region: str, max_tokens: int,
    system_prompt: Optional[str] = None,
) -> str:
    """Invoke the Bedrock runtime converse API and return the model's
    text answer. ``images`` is a list of ``(label, jpeg_bytes)`` pairs
    attached as image content blocks. ``system_prompt``, when non-empty,
    is sent as the Converse API top-level ``system`` parameter; when
    absent/empty the converse kwargs are byte-identical to the
    pre-feature invocation (json-trigger-metadata-pipeline Requirements
    7.2, 7.3). boto3 is imported lazily so this module stays importable
    everywhere (Requirement 13.7)."""
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
    kwargs = dict(
        modelId=model,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": int(max_tokens)},
    )
    if system_prompt:
        kwargs["system"] = [{"text": system_prompt}]
    response = client.converse(**kwargs)
    parts = (response.get("output", {}).get("message", {}).get("content", []))
    return "".join(part.get("text", "") for part in parts
                   if isinstance(part, dict))


def _accepts_keyword(func: Callable, name: str) -> bool:
    """True when ``func`` accepts the keyword argument ``name``.

    Mirrors the executor's handler shim: tolerates un-inspectable
    callables by returning False, and treats a ``**kwargs`` parameter as
    accepting any keyword. Used so ``duration_sink`` is only forwarded to
    ``process()`` overrides that declare it (node-execution-timing R5.2
    backward compatibility)."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if name in parameters:
        return True
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in parameters.values()
    )


def _emit_duration(
    duration_sink: Optional[Callable[[Optional[str], float], None]],
    node_id: Optional[str],
    elapsed_ms: float,
) -> None:
    """Report a binding invocation's elapsed milliseconds to
    ``duration_sink`` (node_id, elapsed_ms), contained.

    Mirrors ``OutputBindingProcessor._emit_detail``: a None sink is a
    no-op (default behavior byte-identical to today); a raising sink is
    caught and logged at debug, never affecting the binding outcome
    (node-execution-timing Requirements 1.3, 1.7)."""
    if duration_sink is None:
        return
    try:
        duration_sink(node_id, elapsed_ms)
    except Exception:  # noqa: BLE001 - timing is best-effort
        logger.debug(
            "Binding duration_sink raised for node %s; ignored",
            node_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Detection crops (detection-guided-bedrock-inspection Requirement 2)
#
# A Bedrock_Binding carrying ``crop_detection_index`` = k inspects entry k
# of the run's merged Detection_List instead of the whole captured frame:
# the captured 'in' JPEG is sliced to the entry's source-pixel bounding
# box (expanded by ``crop_margin_percent`` per side, clamped to the frame
# bounds, defensively scaled when captured and source dimensions differ),
# re-encoded, persisted as a run artifact, and sent as the Converse
# request's "Input image" content block. Every crop-path failure is a
# RECORDED error outcome at ``bedrock.{nodeId}.error`` — the run and the
# sibling bindings proceed (Requirement 2.4), unlike the legacy
# whole-frame path whose failures raise ``BedrockInferenceError``.
# ---------------------------------------------------------------------------

#: JPEG re-encode quality of a Detection_Crop (design Decision 2).
CROP_JPEG_QUALITY = 95

#: Run-artifact filename of a persisted Detection_Crop, named to include
#: the selected entry's Detection_ID (Requirement 2.5).
CROP_ARTIFACT_TEMPLATE = "{capture_id}.crop.{detection_id}.jpg"

#: ADDITIVE (imts-triple-inspection-hmi Requirement 4.4): run-artifact
#: filename of an Inspection's Original_Image — the exact crop bytes sent
#: to Bedrock, written in the port-generic node-frame naming
#: ``{capture_id}.node.{sanitizedNodeId}.{port}.jpg`` that
#: ``run_artifacts.list_node_images`` keys on, so the new ``original``
#: port is listed by ``GET .../results`` and served by
#: ``GET .../node-image`` with zero LocalServer changes.
ORIGINAL_FRAME_ARTIFACT_TEMPLATE = (
    "{capture_id}.node.{safe_node_id}.original.jpg"
)

#: ADDITIVE (imts-triple-inspection-hmi Requirements 4.4, 4.12):
#: run-artifact filename of an Inspection's Annotated_Image — the same
#: crop bytes with the Bedrock answer's Defect_Object boxes drawn on
#: them, in the same port-generic node-frame naming so the new
#: ``annotated`` port is listed and served with zero LocalServer
#: changes.
ANNOTATED_FRAME_ARTIFACT_TEMPLATE = (
    "{capture_id}.node.{safe_node_id}.annotated.jpg"
)

#: Filename-unsafe characters in a node id — the very same discipline
#: ``PipelineExecutor._UNSAFE_NODE_ID_CHARS`` applies when it persists
#: node frames, so the filenames written here always parse back to the
#: same (``nodeId``, ``port``) pair ``list_node_images`` reports.
_UNSAFE_NODE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_node_id_for_artifact(node_id: Optional[str]) -> str:
    """The filename-safe form of ``node_id`` used in node-frame artifact
    names, mirroring ``PipelineExecutor._UNSAFE_NODE_ID_CHARS`` exactly
    (unsafe characters replaced by ``_``, empty ids becoming ``node``)."""
    return _UNSAFE_NODE_ID_CHARS.sub("_", str(node_id or "")) or "node"


#: Box outline/label colors of an Annotated_Image, in OpenCV BGR order:
#: red for a NOK Defect_Object, green for an OK one (design Decision 2 —
#: visually distinct per ``qc``).
_DEFECT_NOK_COLOR = (0, 0, 255)
_DEFECT_OK_COLOR = (0, 200, 0)


def extract_defect_objects(text: str) -> Optional[List[Any]]:
    """ADDITIVE (imts-triple-inspection-hmi Requirement 4.12): the
    Bedrock answer's ``objects`` list, or ``None`` when the answer
    yields no parseable one.

    Tolerant in the :func:`parse_bedrock_answer` style — fenced code
    blocks (``` / ```json) and surrounding prose are accepted and the
    first JSON object carrying an ``objects`` list wins. Entries are
    returned verbatim (including malformed ones); the annotated-frame
    draw skips the entries it cannot use. A parseable but empty
    ``objects: []`` list returns ``[]`` (a clean part), which is
    deliberately distinct from ``None`` (no Annotated_Image at all).

    Purely additive: never raises, and never touches the existing
    ``is_anomalous``/``confidence`` parse or its failure behavior.
    """
    try:
        candidates = [
            match.group(1) for match in _FENCED_BLOCK.finditer(text or "")
        ]
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
            if not isinstance(parsed, dict):
                continue
            objects = parsed.get("objects")
            if isinstance(objects, list):
                return list(objects)
    except Exception:  # noqa: BLE001 - extraction is best-effort
        logger.debug(
            "Could not extract the answer's Defect_Objects", exc_info=True)
    return None


def clamp_defect_box(
    bounding_box: Any,
    width: int,
    height: int,
) -> Optional[Tuple[int, int, int, int]]:
    """The integer pixel rectangle ``(x0, y0, x1, y1)`` of a
    Defect_Object's ``bounding_box`` clamped to a ``width`` x ``height``
    image, or ``None`` when the box is missing, malformed, or empty after
    clamping (imts-triple-inspection-hmi Requirement 4.12).

    Coordinates are read in the pixel space of the image sent to Bedrock
    — i.e. the detection crop itself — so no coordinate-space
    translation is involved. Mins floor and maxes ceil, so a box with a
    non-empty intersection never collapses to an empty rectangle through
    rounding alone.
    """
    if not isinstance(bounding_box, dict):
        return None
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    values = []
    for key in ("x_min", "y_min", "x_max", "y_max"):
        value = bounding_box.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        values.append(value)
    x_min, y_min, x_max, y_max = values
    x0 = int(math.floor(max(0.0, min(float(width), x_min))))
    y0 = int(math.floor(max(0.0, min(float(height), y_min))))
    x1 = int(math.ceil(max(0.0, min(float(width), x_max))))
    y1 = int(math.ceil(max(0.0, min(float(height), y_max))))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def draw_defect_objects(
    frame: Any,
    objects: List[Any],
) -> List[Tuple[int, int, int, int]]:
    """Draw every valid Defect_Object of ``objects`` onto ``frame`` in
    place and return the drawn rectangles in draw order
    (imts-triple-inspection-hmi Requirement 4.12).

    Each box is the clamped intersection of the entry's ``bounding_box``
    with the frame bounds (:func:`clamp_defect_box`), rendered as a
    rectangle outline plus the entry's ``name``/``qc`` label — red for
    NOK, green for OK. Entries whose box is missing, malformed, or empty
    after clamping are skipped without affecting the valid entries.
    """
    import cv2

    drawn: List[Tuple[int, int, int, int]] = []
    if not isinstance(objects, list) or frame is None:
        return drawn
    height, width = frame.shape[:2]
    thickness = max(2, int(round(min(height, width) / 240.0)))
    for entry in objects:
        if not isinstance(entry, dict):
            continue
        box = clamp_defect_box(entry.get("bounding_box"), width, height)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        qc = entry.get("qc")
        qc_text = str(qc).strip() if qc is not None else ""
        color = (
            _DEFECT_OK_COLOR if qc_text.upper() == "OK"
            else _DEFECT_NOK_COLOR
        )
        cv2.rectangle(frame, (x0, y0), (x1 - 1, y1 - 1), color, thickness)
        name = entry.get("name")
        name_text = str(name).strip() if name is not None else ""
        label = " ".join(part for part in (name_text, qc_text) if part)
        if label:
            scale = max(0.4, min(height, width) / 600.0)
            baseline = y0 - max(4, thickness * 2)
            if baseline < int(20 * scale):
                baseline = min(height - 2, y1 + int(20 * scale))
            cv2.putText(
                frame, label, (x0, int(baseline)),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                max(1, thickness - 1), cv2.LINE_AA,
            )
        drawn.append(box)
    return drawn


def compute_crop_box(
    box: Tuple[float, float, float, float],
    frame_width: int,
    frame_height: int,
    margin_percent: float,
    source_dimensions: Optional[Tuple[float, float]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """The integer pixel crop rectangle ``(x0, y0, x1, y1)`` of a
    detection ``box`` within the captured frame, or ``None`` when the
    crop would be degenerate/empty (design Property 3: never a zero-size
    crop — the caller records an error instead).

    ``box`` is the detection's ``(x_min, y_min, x_max, y_max)`` in
    source-frame pixels. ``source_dimensions`` (``(width, height)`` of
    the source frame, from the capture record's input image) enables the
    defensive mapping: when the captured frame's dimensions differ from
    the source's, the box is scaled by ``frame / source`` per axis
    (Requirement 2.6 — the capture sink chain performs no scaling, so
    this normally degenerates to a clamp). ``margin_percent`` expands the
    box on every side by that percentage of the box's own width/height
    (Requirement 2.3); the result is clamped to the frame bounds.
    """
    try:
        x_min, y_min, x_max, y_max = (float(value) for value in box)
        frame_width = int(frame_width)
        frame_height = int(frame_height)
        margin = float(margin_percent or 0.0)
    except (TypeError, ValueError):
        return None
    if frame_width <= 0 or frame_height <= 0:
        return None
    if margin < 0:
        margin = 0.0
    if source_dimensions is not None:
        try:
            source_width, source_height = (
                float(value) for value in source_dimensions
            )
        except (TypeError, ValueError):
            source_width = source_height = 0.0
        if (
            source_width > 0
            and source_height > 0
            and (source_width != frame_width or source_height != frame_height)
        ):
            scale_x = frame_width / source_width
            scale_y = frame_height / source_height
            x_min, x_max = x_min * scale_x, x_max * scale_x
            y_min, y_max = y_min * scale_y, y_max * scale_y
    if x_max <= x_min or y_max <= y_min:
        # A degenerate or inverted box never yields a crop: the margin
        # is a percentage of the box's own dimensions, so a non-positive
        # dimension would SHRINK the margined rectangle (and floor/ceil
        # rounding could still leave a spurious 1-pixel band on the
        # margin-free path). Design Property 3: a degenerate box is a
        # recorded error, never a zero-size or rounding-artifact crop.
        return None
    margin_x = (x_max - x_min) * margin / 100.0
    margin_y = (y_max - y_min) * margin / 100.0
    x0 = max(0, int(math.floor(x_min - margin_x)))
    y0 = max(0, int(math.floor(y_min - margin_y)))
    x1 = min(frame_width, int(math.ceil(x_max + margin_x)))
    y1 = min(frame_height, int(math.ceil(y_max + margin_y)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return x0, y0, x1, y1


def _capture_record_source_dimensions(
    output_dir: Optional[str], capture_id: Optional[str]
) -> Optional[Tuple[int, int]]:
    """The ``(width, height)`` of the capture record's input image, or
    ``None`` (best-effort, never raises).

    The Marshal_Model's capture record (``{output_dir}/{capture_id}.jsonl``)
    references the frame ``emltriton`` actually processed under
    ``deviceFleetAuxiliaryInputs`` (``data-ref: file://...``); its
    dimensions are the source-pixel space the detection boxes live in.
    ``None`` (no record, no input reference, unreadable image) means the
    caller treats captured and source dimensions as identical — the normal
    case, since the capture sink chain performs no scaling."""
    if not output_dir or not capture_id:
        return None
    path = os.path.join(output_dir, "{0}.jsonl".format(capture_id))
    try:
        with open(path, "r") as jsonl_file:
            lines = [
                line for line in jsonl_file.read().splitlines()
                if line.strip()
            ]
        if not lines:
            return None
        record = json.loads(lines[-1])
        inputs = record.get("deviceFleetAuxiliaryInputs") or []
    except Exception:  # noqa: BLE001 - best-effort, never fail the crop
        logger.debug(
            "Could not read the capture record at %s for source "
            "dimensions", path, exc_info=True,
        )
        return None
    if not isinstance(inputs, list):
        return None
    for entry in inputs:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("data-ref")
        if not isinstance(ref, str) or not ref.startswith("file://"):
            continue
        image_path = ref[len("file://"):]
        try:
            import cv2  # lazy: keeps this module importable everywhere

            frame = cv2.imread(image_path)
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug(
                "Could not load the capture record's input image at %s",
                image_path, exc_info=True,
            )
            return None
        if frame is None:
            return None
        height, width = frame.shape[:2]
        return width, height
    return None


@dataclass(frozen=True)
class RunContext:
    """Per-run context the executor threads into the Bedrock processor
    (detection-guided-bedrock-inspection Requirement 7.1).

    Carries what the crop / payload-reference / nested-verdict paths
    need beyond the compiled binding itself:

    - ``tag_values``: the run's metadata dict (Run_Metadata) — the
      merged Detection_List (``detections``) and the Trigger_Context
      (``trigger.payload_json``) are resolved from it;
    - ``output_dir`` / ``capture_id``: where run artifacts (capture
      record, Detection_Crops) live and how they are keyed;
    - ``graph_document``: the registration's ``workflow.json`` graph
      (Detection_Sort_Order resolution);
    - ``node_status``: the run's NodeStatusCollector, for recorded
      per-node error outcomes.

    Every field defaults to ``None`` so partially-populated contexts
    (and the absent-context legacy path) degrade to today's behavior.
    """

    tag_values: Optional[Dict[str, Any]] = None
    output_dir: Optional[str] = None
    capture_id: Optional[str] = None
    graph_document: Optional[dict] = None
    node_status: Optional[Any] = None


#: Upper bound on the Bedrock branch thread pool
#: (detection-guided-bedrock-inspection Requirement 5.2): binding work
#: is network-I/O-bound (Bedrock Converse, S3 fetch, MQTT publish) with
#: 30 s read timeouts; a small pool bounds memory. The effective pool
#: size is ``min(len(bindings), BEDROCK_MAX_POOL_WORKERS)``.
BEDROCK_MAX_POOL_WORKERS = 4


class BedrockInferenceProcessor:
    """Runs a compiled document's ``bedrock_inference`` bindings.

    Called by the WorkflowExecutor after a successful pipeline run and
    before the run is finalized (so the merged metadata reaches the
    post-run output bindings). The invoker is injectable so tests run
    without boto3 or network access.

    ``output_processor`` (optional; default None → the sequential
    legacy path, byte-identical to today for every existing
    caller/test) is the run's :class:`OutputBindingProcessor`. When the
    executor injects it, ``process`` fans the bindings out to a thread
    pool and, as each outcome lands, merges it under a shared lock and
    runs that branch's output bindings through
    :meth:`OutputBindingProcessor.process_subset` — publish-on-completion
    (detection-guided-bedrock-inspection Requirements 5.2, 5.3).
    """

    def __init__(
        self,
        invoker: Optional[Callable] = None,
        output_processor: Optional["OutputBindingProcessor"] = None,
    ) -> None:
        self._invoker = invoker or _default_bedrock_invoker
        self._output_processor = output_processor

    def bindings(self, document: dict) -> List[dict]:
        return [
            binding for binding in (document.get("executorBindings") or [])
            if binding.get("binding") == BINDING_BEDROCK_INFERENCE
        ]

    def process(
        self,
        document: dict,
        tag_values: dict,
        work_dir: Optional[str],
        duration_sink: Optional[Callable[[Optional[str], float], None]] = None,
        run_context: Optional[RunContext] = None,
        detail_sink: Optional[Callable[[Optional[str], str], None]] = None,
    ) -> Dict[str, Any]:
        """Run every bedrock_inference binding and return the run's
        inference metadata with the parsed fields merged in. Raises
        :class:`BedrockInferenceError` naming the failing node.

        ``duration_sink`` (optional; default None → behavior byte-identical
        to today) receives ``(node_id, elapsed_ms)`` for every invocation,
        measured with the monotonic clock and reported in a ``try/finally``
        so error-terminated invocations are timed too, with raises
        propagating exactly as today (node-execution-timing Requirements
        1.3, 1.7).

        ``run_context`` (optional; default None → behavior byte-identical
        to today) is the executor-supplied :class:`RunContext`; it is
        forwarded to each ``_run_one`` for the detection-crop /
        payload-reference / nested-verdict paths
        (detection-guided-bedrock-inspection Requirement 7.1).

        ``detail_sink`` (optional; default None) receives ``(node_id,
        detail)`` sent-message summaries from the per-branch output
        bindings on the concurrent path; it is forwarded to
        :meth:`OutputBindingProcessor.process_subset` and never consulted
        on the sequential legacy path.

        With no ``output_processor`` configured (every pre-feature
        caller), the bindings run sequentially in ``executorBindings``
        order — the legacy path, byte-identical to today. With one, the
        bindings fan out to a
        ``ThreadPoolExecutor(max_workers=min(len(bindings), 4))`` and
        each completion merges under a shared lock, then runs its
        branch's output bindings on the spot — publish-on-completion
        (detection-guided-bedrock-inspection Requirements 5.2-5.6)."""
        bindings = self.bindings(document)
        if self._output_processor is None:
            return self._process_sequential(
                bindings, tag_values, work_dir, duration_sink, run_context)
        return self._process_concurrent(
            document, bindings, tag_values, work_dir, duration_sink,
            detail_sink, run_context)

    def _process_sequential(
        self,
        bindings: List[dict],
        tag_values: dict,
        work_dir: Optional[str],
        duration_sink: Optional[Callable[[Optional[str], float], None]],
        run_context: Optional[RunContext],
    ) -> Dict[str, Any]:
        """The pre-feature sequential path, verbatim: bindings run one
        after another in ``executorBindings`` order and the first raise
        aborts the loop (later bindings never run) — byte-identical to
        today for every caller without an ``output_processor``
        (Requirement 7.1, Property 7)."""
        metadata = dict(tag_values or {})
        for binding in bindings:
            node_id = binding.get("nodeId")
            try:
                started = time.monotonic()
                try:
                    result = self._run_one(
                        binding, work_dir, run_context=run_context)
                finally:
                    _emit_duration(
                        duration_sink, node_id,
                        (time.monotonic() - started) * 1000.0,
                    )
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

    def _process_concurrent(
        self,
        document: dict,
        bindings: List[dict],
        tag_values: dict,
        work_dir: Optional[str],
        duration_sink: Optional[Callable[[Optional[str], float], None]],
        detail_sink: Optional[Callable[[Optional[str], str], None]],
        run_context: Optional[RunContext],
    ) -> Dict[str, Any]:
        """Fan the bindings out to a thread pool with per-completion
        merge + branch publishing (detection-guided-bedrock-inspection
        Requirements 5.2-5.6).

        Each worker runs ``_run_one``; on completion, under the shared
        merge lock, its outcome merges into the metadata (nested + flat
        keys; flat writes keep dict-update semantics — last COMPLETION
        wins, the concurrent analogue of today's last-binding-wins,
        design Risk 4) and, when the document's branch plan
        (``branching.bedrock_branches``) names output bindings for that
        node, a metadata snapshot runs through
        ``OutputBindingProcessor.process_subset`` on the spot — each
        inspection's outputs publish as its result lands (Requirement
        5.3), serialized by the lock so branch publishes never interleave
        their metadata views.

        - An errored branch (recorded error outcome at
          ``bedrock.{nodeId}.error``) skips its output bindings — the
          node status was already set by ``_recorded_error`` — and the
          sibling branches proceed (Requirements 5.4, 5.5).
        - ``process`` returns only after ALL futures complete (join),
          with every outcome merged (Requirement 5.6).
        - A legacy-path raise (``BedrockInferenceError``) is re-raised
          AFTER the join — the first in ``executorBindings`` order when
          several — preserving today's run-failure semantics
          (Requirement 7.1). Unlike the sequential path, sibling
          bindings have already run by then (they were in flight
          concurrently); their branch publishes stand (Requirement 5.4).
        - Branch output-binding failures are collected per branch and
          surfaced through the existing :class:`OutputBindingError`
          aggregation after the join; other branches are unaffected
          (design Error Handling table). A ``BedrockInferenceError``
          takes precedence when both occurred.
        """
        metadata = dict(tag_values or {})
        if not bindings:
            return metadata
        branch_plans = bedrock_branches(document)
        merge_lock = threading.Lock()
        #: index-in-bindings-order -> BedrockInferenceError (legacy raises).
        raised: Dict[int, BedrockInferenceError] = {}
        #: One entry per failed branch-subset invocation:
        #: (failing node ids, aggregated message).
        output_failures: List[Tuple[List[Any], str]] = []

        def run_branch(index: int, binding: dict) -> None:
            node_id = binding.get("nodeId")
            try:
                started = time.monotonic()
                try:
                    result = self._run_one(
                        binding, work_dir, run_context=run_context)
                finally:
                    # Elapsed time is measured here (error-terminated
                    # invocations are timed too, exactly as the
                    # sequential path), but the sink INVOCATION is
                    # routed through the shared merge lock so concurrent
                    # workers never interleave duration_sink calls with
                    # each other or with the detail/duration sinks the
                    # branch publishes fire inside ``process_subset``
                    # (which already runs under this lock) —
                    # detection-guided-bedrock-inspection Requirement
                    # 5.6, design "Thread safety inventory".
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    with merge_lock:
                        _emit_duration(duration_sink, node_id, elapsed_ms)
            except BedrockInferenceError as error:
                with merge_lock:
                    raised[index] = error
                return
            except Exception as error:  # noqa: BLE001 - wrapped as today
                wrapped = BedrockInferenceError(
                    node_id,
                    "Bedrock inference node '{0}' failed: {1}".format(
                        node_id, error),
                )
                wrapped.__cause__ = error
                with merge_lock:
                    raised[index] = wrapped
                return
            # Completion path: merge + branch publish under the shared
            # lock. The nested-``bedrock`` merge REPLACES
            # metadata["bedrock"] with a fresh dict (never mutates the
            # previous one), so a shallow ``dict(metadata)`` snapshot
            # taken here stays stable while sibling completions keep
            # merging.
            with merge_lock:
                nested = result.pop("bedrock", None)
                metadata.update(result)
                if nested:
                    merged = dict(metadata.get("bedrock") or {})
                    merged.update(nested)
                    metadata["bedrock"] = merged
                logger.info(
                    "Bedrock inference binding (node %s) processed", node_id
                )
                plan = branch_plans.get(node_id)
                if plan is None or not plan.binding_ids:
                    return
                errored = (
                    isinstance(nested, dict)
                    and isinstance(nested.get(node_id), dict)
                    and "error" in nested[node_id]
                )
                if errored:
                    # Recorded error outcome: the branch's output
                    # bindings are skipped entirely (the node status was
                    # set by _recorded_error); siblings proceed
                    # (Requirements 5.4, 5.5).
                    logger.info(
                        "Bedrock branch %s recorded an error; skipping "
                        "its %d branch output binding(s)",
                        node_id, len(plan.binding_ids),
                    )
                    return
                snapshot = dict(metadata)
                subset_kwargs: Dict[str, Any] = {}
                if detail_sink is not None:
                    subset_kwargs["detail_sink"] = detail_sink
                if duration_sink is not None:
                    subset_kwargs["duration_sink"] = duration_sink
                try:
                    self._output_processor.process_subset(
                        document, snapshot, list(plan.binding_ids),
                        **subset_kwargs)
                except OutputBindingError as error:
                    # process_subset already aggregated this branch's
                    # binding failures (every branch binding was still
                    # attempted); collect for the post-join surfacing.
                    output_failures.append(
                        (list(error.node_ids), str(error)))
                except Exception as error:  # noqa: BLE001 - contained
                    logger.exception(
                        "Bedrock branch %s output bindings failed; other "
                        "branches are unaffected", node_id,
                    )
                    output_failures.append(
                        ([node_id],
                         "branch {0} output bindings failed: {1}".format(
                             node_id, error)))

        pool_size = min(len(bindings), BEDROCK_MAX_POOL_WORKERS)
        with ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="bedrock-branch",
        ) as pool:
            futures = [
                pool.submit(run_branch, index, binding)
                for index, binding in enumerate(bindings)
            ]
            # Join: process returns (or raises) only after every future
            # has completed and merged (Requirement 5.6). run_branch
            # contains its own failures, so result() re-raises nothing
            # in practice — the loop still surfaces a defect loudly
            # rather than swallowing it.
            for future in futures:
                future.result()

        if raised:
            # Today's run-failure semantics, deterministically: the
            # failure of the earliest binding in executorBindings order
            # (the one the sequential path would have raised).
            raise raised[min(raised)]
        if output_failures:
            failing_ids = [
                failing_id
                for ids, _ in output_failures
                for failing_id in ids
                if failing_id is not None
            ]
            summary = "; ".join(message for _, message in output_failures)
            raise OutputBindingError(failing_ids, summary)
        return metadata

    def _run_one(
        self,
        binding: dict,
        work_dir: Optional[str],
        run_context: Optional[RunContext] = None,
    ) -> Dict[str, Any]:
        # ``run_context`` (default None → byte-identical legacy path)
        # carries the run state the detection-crop path resolves against
        # (detection-guided-bedrock-inspection Requirement 7.1); the
        # payload-reference path lands separately.
        node_id = binding.get("nodeId")
        parameters = dict(binding.get("parameters") or {})
        capture_paths = binding.get("capturePaths") or {}
        images = []

        # Detection crop (Requirement 2): a binding carrying
        # ``crop_detection_index`` sends the Detection_Crop as its
        # "Input image" instead of the whole captured frame. Every
        # crop-path failure (missing/out-of-range Detection_List entry,
        # missing/unreadable captured frame, degenerate crop) is a
        # RECORDED error outcome — the run and sibling bindings proceed
        # (Requirement 2.4) — unlike the legacy whole-frame path below,
        # whose failures keep raising ``BedrockInferenceError``
        # byte-identically (Requirements 2.1, 7.1).
        detection_id: Optional[str] = None
        # ADDITIVE (imts-triple-inspection-hmi Requirements 4.4, 4.12):
        # the crop bytes are retained past the invocation so the
        # Annotated_Image can be drawn onto a copy of them once the
        # answer arrives. None on the legacy whole-frame path — no
        # per-inspection artifacts are produced there.
        crop_bytes: Optional[bytes] = None
        crop_raw = parameters.get("crop_detection_index")
        if crop_raw is not None and str(crop_raw).strip() != "":
            crop_error, detection_id, crop_bytes = self._detection_crop(
                node_id, parameters, capture_paths, work_dir, run_context)
            if crop_error is not None:
                return self._recorded_error(node_id, crop_error, run_context)
            images.append(("Input image", crop_bytes))
        else:
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

        # Payload_Reference (Requirement 3): a binding carrying
        # ``reference_payload_path`` resolves its reference image from
        # the run's Trigger_Context ``payload_json`` instead of a
        # captured reference frame. ANY failure (missing trigger
        # payload, unresolvable path, prefix denial, fetch failure,
        # size cap, timeout, non-image bytes) is a RECORDED error
        # outcome — NEVER the single-image fallback the frame-port path
        # below uses (Requirement 3.5, design Property 4). An absent
        # parameter keeps today's reference-port behavior
        # byte-identical (Requirement 3.1).
        reference_raw = parameters.get("reference_payload_path")
        if reference_raw is not None and str(reference_raw).strip() != "":
            reference_error, reference_bytes = self._payload_reference(
                node_id, parameters, run_context)
            if reference_error is not None:
                return self._recorded_error(
                    node_id, reference_error, run_context)
            images.append(("Reference image", reference_bytes))
        else:
            # The 'reference' frame is optional: the portal compiler
            # emits capturePaths.reference = None when the port is not
            # fed by any video source. When the reference frame is
            # unavailable for any reason, log the omission and proceed
            # with single-image inference on the primary frame alone.
            reference_path = capture_paths.get("reference")
            if not reference_path:
                logger.warning(
                    "Bedrock inference node '%s': reference port not fed "
                    "by any video source; performing single-image "
                    "inference", node_id)
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
                        "captured 'reference' frame from %s (%s); "
                        "performing single-image inference",
                        node_id, reference_path, e)

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
            # Anomaly mode appends the instruction to the USER prompt
            # only; the system prompt is never touched
            # (json-trigger-metadata-pipeline Requirement 7.4).
            prompt = prompt + "\n\n" + BEDROCK_JSON_INSTRUCTION

        # Optional system prompt: absent/empty/whitespace-only is
        # normalized to None; a non-empty value is passed VERBATIM (not
        # stripped) so the operator's text reaches the model unmodified
        # (json-trigger-metadata-pipeline Requirements 7.2, 7.3).
        raw_system = parameters.get("system_prompt")
        system_prompt = (
            str(raw_system)
            if raw_system is not None and str(raw_system).strip()
            else None
        )

        invoker_args = (
            str(parameters.get("model") or BEDROCK_DEFAULT_MODEL),
            prompt,
            images,
            str(parameters.get("region") or "us-east-1"),
            int(parameters.get("max_tokens") or 256),
        )
        if system_prompt is not None:
            answer = self._invoker(*invoker_args, system_prompt)
        else:
            # Pre-feature arity: injected fakes that predate the
            # system_prompt parameter keep working (Requirement 7.3).
            answer = self._invoker(*invoker_args)
        # The nested per-node entry ``bedrock.{nodeId}.*`` (merged by
        # ``process``): the answer ``text`` (freeform's existing key,
        # recorded in both modes) plus, when a Detection_Crop was
        # inspected, the selected entry's Detection_ID so each verdict
        # is attributable to the exact detection it judged (Requirement
        # 2.7); anomaly mode adds ``is_anomalous``/``confidence`` below
        # (Requirement 4.1). ``detection_id`` is None on the legacy
        # whole-frame path — freeform entries stay byte-identical to
        # today's, and every nested key is additive (Requirement 7.1).
        nested_entry: Dict[str, Any] = {"text": answer}
        if detection_id is not None:
            nested_entry["detection_id"] = detection_id
        if anomaly_mode:
            # An unparseable answer raises here — the existing
            # BedrockInferenceError path — before any text is recorded
            # (Requirement 5.3: the error message carries the excerpt).
            verdict = parse_bedrock_answer(answer)
            verdict["bedrock_text"] = answer
            # Nested per-node verdict keys (Requirement 4.1): the
            # anomaly verdict ALSO lands under
            # ``bedrock.{nodeId}.is_anomalous`` /
            # ``bedrock.{nodeId}.confidence`` beside the nested ``text``
            # (and ``detection_id`` when a Detection_Crop was
            # inspected), merged through the same nested-``bedrock``
            # mechanism freeform mode uses, so parallel inspections in
            # one run never overwrite each other. The FLAT
            # ``is_anomalous``/``confidence`` keys in ``verdict`` keep
            # today's last-writer-wins behavior byte-identical
            # (Requirements 4.1, 4.5, 7.1).
            nested_entry["is_anomalous"] = verdict["is_anomalous"]
            nested_entry["confidence"] = verdict["confidence"]
            verdict["bedrock"] = {node_id: nested_entry}
            result: Dict[str, Any] = verdict
        else:
            result = {
                "bedrock_text": answer,
                "bedrock": {node_id: nested_entry},
            }
        # ADDITIVE (imts-triple-inspection-hmi Requirements 4.4, 4.12):
        # the Inspection's Annotated_Image, drawn from the answer's
        # Defect_Objects onto a copy of the crop. Runs AFTER the existing
        # parse and the result assembly, is entirely contained, and never
        # merges anything into the returned metadata — the metadata shape
        # stays byte-identical to today.
        self._persist_annotated_frame(
            run_context, node_id, crop_bytes, answer)
        return result

    def _recorded_error(
        self,
        node_id: Optional[str],
        message: str,
        run_context: Optional[RunContext],
    ) -> Dict[str, Any]:
        """Record a per-node error outcome and return the result dict
        (Requirements 2.4, 4.2).

        The error lands at ``bedrock.{nodeId}.error`` through the
        existing nested-``bedrock`` merge in :meth:`process` and on the
        run's node status (gating semantics identical to a failed
        condition: the node is marked failed, its downstream outputs are
        gated, and the run and the sibling bindings proceed — this path
        NEVER raises ``BedrockInferenceError``)."""
        logger.error("Bedrock inference recorded error: %s", message)
        if run_context is not None and run_context.node_status is not None:
            try:
                run_context.node_status.mark_failure(node_id, message)
            except Exception:  # noqa: BLE001 - recording is best-effort
                logger.debug(
                    "Could not record the node status failure for node %s",
                    node_id, exc_info=True,
                )
        return {"bedrock": {node_id: {"error": message}}}

    def _payload_reference(
        self,
        node_id: Optional[str],
        parameters: Dict[str, Any],
        run_context: Optional[RunContext],
    ) -> Tuple[Optional[str], Optional[bytes]]:
        """Resolve the binding's ``reference_payload_path`` to
        reference-image bytes: ``(error_message, reference_bytes)``
        (Requirements 3.2, 3.3, 3.4, 3.7).

        The dotted path resolves against the run's Trigger_Context
        ``payload_json`` (``tag_values["trigger"]["payload_json"]``);
        the resolved value fetches/decodes through
        :mod:`workflow_engine.payload_fetch`, gated by the binding's
        newline-separated ``allowed_uri_prefixes``. On success the
        error message is ``None``. On ANY failure (missing trigger
        payload, unresolvable path, prefix denial, fetch failure, size
        cap, timeout, non-image bytes) the error message is set naming
        the node and the reason and the caller records it — never
        raises, never falls back to single-image inference (Requirement
        3.5, design Property 4). Only the resolved source string (the
        URI or ``"base64 payload data"``) is ever logged — never the
        decoded bytes (Requirement 3.8)."""
        dotted_path = str(
            parameters.get("reference_payload_path") or "").strip()
        trigger = None
        if run_context is not None and isinstance(
            run_context.tag_values, dict
        ):
            trigger = run_context.tag_values.get("trigger")
        payload_json = (
            trigger.get("payload_json") if isinstance(trigger, dict)
            else None
        )
        if payload_json is None:
            return (
                "Bedrock inference node '{0}' requested "
                "reference_payload_path '{1}' but the run has no trigger "
                "payload_json to resolve it against".format(
                    node_id, dotted_path),
                None)
        raw_prefixes = parameters.get("allowed_uri_prefixes")
        allowed_prefixes: Tuple[str, ...] = ()
        if isinstance(raw_prefixes, str):
            allowed_prefixes = tuple(
                line.strip() for line in raw_prefixes.splitlines()
                if line.strip()
            )
        try:
            value = resolve_payload_path(payload_json, dotted_path)
            # The run log records the resolved source string — the URI
            # or "base64 payload data" — never the decoded bytes
            # (Requirement 3.8).
            logger.info(
                "Bedrock inference node '%s': resolving Payload_Reference "
                "'%s' from %s", node_id, dotted_path,
                describe_reference_source(value))
            data = fetch_reference_bytes(value, allowed_prefixes)
        except PayloadReferenceError as e:
            return (
                "Bedrock inference node '{0}' could not load its "
                "Payload_Reference: {1}".format(node_id, e),
                None)
        except Exception as e:  # noqa: BLE001 - contained per Req. 3.5
            return (
                "Bedrock inference node '{0}' could not load its "
                "Payload_Reference: {1}".format(node_id, e),
                None)
        return None, data

    def _detection_crop(
        self,
        node_id: Optional[str],
        parameters: Dict[str, Any],
        capture_paths: Dict[str, Any],
        work_dir: Optional[str],
        run_context: Optional[RunContext],
    ) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
        """Resolve the binding's ``crop_detection_index`` to a
        Detection_Crop: ``(error_message, detection_id, crop_bytes)``
        (Requirements 2.2-2.6).

        On success the error message is ``None`` and the crop bytes are
        the re-encoded JPEG slice of the captured 'in' frame, already
        persisted as a run artifact. On ANY failure the error message is
        set (naming the node and, where applicable, the requested index
        and the available detection count) and the caller records it —
        never raises, never falls back to the whole frame."""
        detections = None
        if run_context is not None and isinstance(
            run_context.tag_values, dict
        ):
            detections = run_context.tag_values.get(METADATA_KEY_DETECTIONS)
        available = len(detections) if isinstance(detections, list) else 0
        raw_index = parameters.get("crop_detection_index")
        try:
            index = int(str(raw_index).strip())
        except (TypeError, ValueError):
            return (
                "Bedrock inference node '{0}' has an invalid "
                "crop_detection_index {1!r} (expected an integer >= 0); "
                "{2} detection(s) available".format(
                    node_id, raw_index, available),
                None, None)
        if not isinstance(detections, list):
            return (
                "Bedrock inference node '{0}' requested "
                "crop_detection_index {1} but the run has no "
                "Detection_List (0 detections available)".format(
                    node_id, index),
                None, None)
        if index < 0 or index >= available:
            return (
                "Bedrock inference node '{0}' requested "
                "crop_detection_index {1} but only {2} detection(s) are "
                "available".format(node_id, index, available),
                None, None)
        entry = detections[index]
        try:
            detection_id = str(entry["id"])
            box = (
                float(entry["x_min"]), float(entry["y_min"]),
                float(entry["x_max"]), float(entry["y_max"]),
            )
        except (TypeError, KeyError, ValueError):
            return (
                "Bedrock inference node '{0}': Detection_List entry {1} "
                "of {2} is malformed (missing id or bounding box): "
                "{3!r}".format(node_id, index, available, entry),
                None, None)

        # The captured 'in' frame: on the crop path its absence is a
        # recorded error, not a raise (a graph without a capture node
        # lands no captured .jpg on disk — the run must proceed).
        path = capture_paths.get("in")
        if not path:
            return (
                "Bedrock inference node '{0}' requested "
                "crop_detection_index {1} but has no captured frame for "
                "its 'in' input (the port is not fed by any video "
                "source)".format(node_id, index),
                None, None)
        if work_dir:
            path = path.replace("{work_dir}", work_dir)
        try:
            import cv2  # lazy: keeps this module importable everywhere
        except Exception:  # noqa: BLE001 - contained per Requirement 2.4
            return (
                "Bedrock inference node '{0}' could not produce the "
                "Detection_Crop: cv2 is unavailable".format(node_id),
                None, None)
        frame = cv2.imread(path)
        if frame is None:
            return (
                "Bedrock inference node '{0}' could not read or decode "
                "the captured 'in' frame from {1} for the "
                "Detection_Crop".format(node_id, path),
                None, None)
        frame_height, frame_width = frame.shape[:2]

        source_dimensions = None
        if run_context is not None:
            source_dimensions = _capture_record_source_dimensions(
                run_context.output_dir, run_context.capture_id)
        margin_raw = parameters.get("crop_margin_percent")
        try:
            margin = float(margin_raw) if margin_raw is not None else 0.0
        except (TypeError, ValueError):
            logger.warning(
                "Bedrock inference node '%s': invalid crop_margin_percent "
                "%r; using 0", node_id, margin_raw)
            margin = 0.0
        crop_box = compute_crop_box(
            box, frame_width, frame_height, margin, source_dimensions)
        if crop_box is None:
            return (
                "Bedrock inference node '{0}': detection {1} (index {2}, "
                "box [{3}, {4}, {5}, {6}]) yields an empty crop within "
                "the {7}x{8} captured frame".format(
                    node_id, detection_id, index,
                    box[0], box[1], box[2], box[3],
                    frame_width, frame_height),
                None, None)
        x0, y0, x1, y1 = crop_box
        ok, encoded = cv2.imencode(
            ".jpg", frame[y0:y1, x0:x1],
            [int(cv2.IMWRITE_JPEG_QUALITY), CROP_JPEG_QUALITY])
        if not ok:
            return (
                "Bedrock inference node '{0}' could not re-encode the "
                "Detection_Crop for detection {1}".format(
                    node_id, detection_id),
                None, None)
        crop_bytes = bytes(encoded.tobytes())
        self._persist_crop(node_id, detection_id, crop_bytes, run_context)
        self._persist_original_frame(run_context, node_id, crop_bytes)
        return None, detection_id, crop_bytes

    @staticmethod
    def _persist_crop(
        node_id: Optional[str],
        detection_id: str,
        crop_bytes: bytes,
        run_context: Optional[RunContext],
    ) -> None:
        """Persist the Detection_Crop as
        ``{output_dir}/{capture_id}.crop.{detection_id}.jpg`` alongside
        the existing captured frames (Requirement 2.5).

        Best-effort: a missing output_dir/capture_id or a write failure
        is logged and never affects the inspection outcome."""
        output_dir = run_context.output_dir if run_context else None
        capture_id = run_context.capture_id if run_context else None
        if not output_dir or not capture_id:
            logger.warning(
                "Bedrock inference node '%s': no output_dir/capture_id in "
                "the run context; the Detection_Crop for detection %s was "
                "not persisted", node_id, detection_id)
            return
        path = os.path.join(
            output_dir,
            CROP_ARTIFACT_TEMPLATE.format(
                capture_id=capture_id, detection_id=detection_id),
        )
        try:
            with open(path, "wb") as crop_file:
                crop_file.write(crop_bytes)
        except OSError:
            logger.warning(
                "Bedrock inference node '%s': could not persist the "
                "Detection_Crop at %s", node_id, path, exc_info=True)

    @staticmethod
    def _persist_original_frame(
        run_context: Optional[RunContext],
        node_id: Optional[str],
        crop_bytes: Optional[bytes],
    ) -> None:
        """ADDITIVE (imts-triple-inspection-hmi Requirement 4.4): beside
        the existing ``{capture_id}.crop.{detection_id}.jpg`` artifact,
        persist the exact crop bytes sent to Bedrock as
        ``{capture_id}.node.{sanitizedNodeId}.original.jpg`` — the
        Inspection's Original_Image.

        Called at crop time (beside :meth:`_persist_crop`). The node id
        is sanitized with the executor's ``_UNSAFE_NODE_ID_CHARS``
        discipline, so the filename parses back to the same
        (``nodeId``, ``port``) pair ``run_artifacts.list_node_images``
        reports: filename-pattern compatibility alone makes the new
        ``original`` port listable in ``GET .../results`` and servable by
        ``GET .../node-image`` with zero LocalServer changes.

        Entirely best-effort in the
        ``pipeline_executor._persist_node_frames`` containment style: a
        missing output_dir/capture_id, empty bytes, or any write failure
        is logged and swallowed — the run status, the node outcome, and
        the ``is_anomalous``/``confidence`` metadata merge are
        untouched."""
        try:
            output_dir = run_context.output_dir if run_context else None
            capture_id = run_context.capture_id if run_context else None
            if not output_dir or not capture_id or not crop_bytes:
                logger.debug(
                    "Bedrock inference node '%s': no output_dir/capture_id "
                    "or no crop bytes; the Inspection's Original_Image was "
                    "not persisted", node_id)
                return
            path = os.path.join(
                output_dir,
                ORIGINAL_FRAME_ARTIFACT_TEMPLATE.format(
                    capture_id=capture_id,
                    safe_node_id=sanitize_node_id_for_artifact(node_id),
                ),
            )
            with open(path, "wb") as frame_file:
                frame_file.write(crop_bytes)
        except Exception:  # noqa: BLE001 - contained; never affects the run
            logger.warning(
                "Bedrock inference node '%s': could not persist the "
                "Inspection's Original_Image; the inspection outcome and "
                "the run status are unaffected", node_id, exc_info=True)

    @staticmethod
    def _persist_annotated_frame(
        run_context: Optional[RunContext],
        node_id: Optional[str],
        crop_bytes: Optional[bytes],
        answer_text: Optional[str],
    ) -> None:
        """ADDITIVE (imts-triple-inspection-hmi Requirements 4.4, 4.12):
        persist the Inspection's Annotated_Image as
        ``{capture_id}.node.{sanitizedNodeId}.annotated.jpg`` — the crop
        sent to Bedrock with the answer's Defect_Object boxes drawn on
        it.

        Called after the Bedrock invocation returns and after the
        existing ``is_anomalous``/``confidence`` parse, once the answer
        text is available. The answer's ``objects`` list is extracted
        tolerantly (:func:`extract_defect_objects`); the extraction is
        purely additive and never affects the existing parse or its
        failure behavior, and the parsed list is deliberately NOT merged
        into run metadata (the raw answer is already recorded at
        ``bedrock.{nodeId}.text``, so the metadata shape stays
        byte-identical to today).

        IF the answer yields no parseable ``objects`` list, nothing is
        persisted — the HMI then shows its no-annotated-image
        placeholder (Requirement 4.10). A parseable but empty
        ``objects: []`` list persists the crop unchanged with zero boxes
        (a clean part).

        Entirely best-effort in the
        ``pipeline_executor._persist_node_frames`` containment style: a
        missing output_dir/capture_id, an undecodable crop, a cv2 or
        write failure — anything at all — is logged and swallowed, never
        affecting the run status, the node outcome, or the
        ``is_anomalous``/``confidence`` metadata merge."""
        try:
            output_dir = run_context.output_dir if run_context else None
            capture_id = run_context.capture_id if run_context else None
            if not output_dir or not capture_id or not crop_bytes:
                logger.debug(
                    "Bedrock inference node '%s': no output_dir/capture_id "
                    "or no crop bytes; the Inspection's Annotated_Image "
                    "was not persisted", node_id)
                return
            objects = extract_defect_objects(answer_text or "")
            if objects is None:
                # No parseable ``objects`` list: no Annotated_Image is
                # produced for this Inspection (Requirements 4.10, 4.12).
                logger.debug(
                    "Bedrock inference node '%s': the answer carries no "
                    "parseable objects list; no Annotated_Image was "
                    "persisted", node_id)
                return

            import cv2
            import numpy as np

            frame = cv2.imdecode(
                np.frombuffer(crop_bytes, dtype=np.uint8),
                cv2.IMREAD_COLOR)
            if frame is None:
                logger.warning(
                    "Bedrock inference node '%s': could not decode the crop "
                    "bytes for the Inspection's Annotated_Image", node_id)
                return
            drawn = draw_defect_objects(frame, objects)
            ok, encoded = cv2.imencode(
                ".jpg", frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), CROP_JPEG_QUALITY])
            if not ok:
                logger.warning(
                    "Bedrock inference node '%s': could not encode the "
                    "Inspection's Annotated_Image", node_id)
                return
            path = os.path.join(
                output_dir,
                ANNOTATED_FRAME_ARTIFACT_TEMPLATE.format(
                    capture_id=capture_id,
                    safe_node_id=sanitize_node_id_for_artifact(node_id),
                ),
            )
            with open(path, "wb") as frame_file:
                frame_file.write(bytes(encoded.tobytes()))
            logger.debug(
                "Bedrock inference node '%s': persisted the Inspection's "
                "Annotated_Image with %d defect box(es) of %d answer "
                "object(s)", node_id, len(drawn), len(objects))
        except Exception:  # noqa: BLE001 - contained; never affects the run
            logger.warning(
                "Bedrock inference node '%s': could not persist the "
                "Inspection's Annotated_Image; the inspection outcome and "
                "the run status are unaffected", node_id, exc_info=True)


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

#: Documented default Output_Token_Budget applied by the LLM_Binding when
#: the node's ``max_tokens`` parameter is absent or invalid
#: (vllm-workflow-latency-optimization Requirements 3.3, 3.4).
DEFAULT_OUTPUT_TOKEN_BUDGET = 256


def resolve_output_token_budget(raw: Any) -> Tuple[int, Optional[str]]:
    """Resolve a configured ``max_tokens`` value into the effective
    Output_Token_Budget: ``(budget, substitution_notice)``.

    Valid = an integral number >= 1 (bool excluded; integral floats
    accepted as their int value) -> ``(value, None)``. Absent (``None``)
    -> ``(DEFAULT_OUTPUT_TOKEN_BUDGET, None)``. Anything else
    (non-numeric, non-positive, non-integral) ->
    ``(DEFAULT_OUTPUT_TOKEN_BUDGET, notice)`` with the notice naming the
    rejected value (vllm-workflow-latency-optimization Requirements 3.1,
    3.3, 3.4)."""
    if raw is None:
        return DEFAULT_OUTPUT_TOKEN_BUDGET, None
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw, None
        # ``is_integer()`` is False for inf/nan, so ``int(raw)`` below
        # never overflows.
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw), None
    notice = (
        "invalid max_tokens value {0!r} (expected an integral number "
        ">= 1); substituting the default Output_Token_Budget of "
        "{1} tokens".format(raw, DEFAULT_OUTPUT_TOKEN_BUDGET)
    )
    return DEFAULT_OUTPUT_TOKEN_BUDGET, notice


def resolve_max_image_dimension(
    raw: Any,
) -> Tuple[Optional[int], Optional[str]]:
    """Resolve a configured ``max_image_dimension`` value into the
    effective downscaling bound: ``(max_dim, invalid_notice)``.

    Absent (``None``) -> ``(None, None)`` — unconfigured, silent.
    Valid = an integral number >= 1 (bool excluded; integral floats
    accepted as their int value, the same acceptance convention as
    :func:`resolve_output_token_budget`) -> ``(value, None)``. Anything
    else (non-numeric, non-positive, non-integral) -> ``(None, notice)``
    naming the rejected value — treated as unconfigured, with the caller
    emitting the notice as a run-log warning
    (vllm-workflow-latency-optimization Requirement 5.8)."""
    if raw is None:
        return None, None
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw, None
        # ``is_integer()`` is False for inf/nan, so ``int(raw)`` below
        # never overflows.
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw), None
    notice = (
        "invalid max_image_dimension value {0!r} (expected an integral "
        "number >= 1); treating the image downscaling option as "
        "unconfigured and sending captured frames unmodified".format(raw)
    )
    return None, notice


def downscale_image_bytes(data: bytes, max_dim: int) -> bytes:
    """Downscale a captured frame so its longer edge equals ``max_dim``
    (vllm-workflow-latency-optimization Requirements 5.3, 5.7).

    When the decoded image's longer edge exceeds ``max_dim``, the image
    is resized so the longer edge equals ``max_dim`` with the aspect
    ratio preserved (LANCZOS resampling) and re-encoded as JPEG. When
    the longer edge is already <= ``max_dim`` the ORIGINAL bytes are
    returned unchanged — never upscaled, byte-identical (R5.7).

    Raises on decode/encode failure — the caller contains the failure
    per R5.4 (:func:`_downscale_frame_or_original`). Pillow is imported
    lazily so this module stays importable everywhere."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        longer = max(width, height)
        if longer <= max_dim:
            return data
        if width >= height:
            new_size = (
                max_dim, max(1, round(height * max_dim / float(width))))
        else:
            new_size = (
                max(1, round(width * max_dim / float(height))), max_dim)
        # Pillow >= 9.1 moved the resampling constants to
        # ``Image.Resampling``; the fallback keeps older Pillow working.
        resampling = getattr(Image, "Resampling", Image)
        resized = image.resize(new_size, resampling.LANCZOS)
    if resized.mode not in ("RGB", "L"):
        # JPEG cannot encode e.g. RGBA/P frames; normalize to RGB.
        resized = resized.convert("RGB")
    buffer = io.BytesIO()
    resized.save(buffer, format="JPEG")
    return buffer.getvalue()


def _downscale_frame_or_original(
    data: bytes, max_dim: int, node_id: Any, port: str
) -> bytes:
    """Apply :func:`downscale_image_bytes` under the R5.4 containment
    contract: a raised decode/encode failure logs one run-log WARNING
    naming the node and the failure, and the ORIGINAL captured bytes are
    returned so the request proceeds and the run reaches the same
    terminal state it would reach without the downscaling failure."""
    try:
        return downscale_image_bytes(data, max_dim)
    except Exception as e:  # noqa: BLE001 - downscaling is best-effort (R5.4)
        logger.warning(
            "LLM inference node '%s': downscaling the captured '%s' "
            "frame to max_image_dimension %s failed (%s); sending the "
            "original image", node_id, port, max_dim, e,
        )
        return data


def _format_generation_metrics_line(
    node_id: Any, model_name: Any, metrics: Dict[str, Any]
) -> str:
    """Format the run-log Generation_Phase_Breakdown line from the API's
    ``generation_metrics`` payload dict (vllm-workflow-latency-optimization
    Requirements 1.2, 3.5, 3.6).

    The payload arrives already rendered by the manager side
    (``GenerationPhaseBreakdown.to_payload()``): each value is an ``int``,
    the string ``"unavailable"``, or — for the image token count of an
    image-less request — the string ``"n/a"``. Every field is always
    present in the line, never dropped; a missing key degrades to
    ``"unavailable"``. The prefill label honors
    ``prefill_includes_queueing`` (the manager-clock fallback path). The
    truncation statement names the Output_Token_Budget exactly when the
    payload reports ``truncated: true`` (R3.5); ``false`` renders as
    "output not truncated" and anything else as "truncation unavailable"
    (R3.6 — no truncation is reported)."""
    def _ms(key: str) -> str:
        value = metrics.get(key, "unavailable")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "{0} ms".format(value)
        return str(value)

    def _count(key: str) -> str:
        return str(metrics.get(key, "unavailable"))

    prefill_label = (
        "prefill (includes queueing)"
        if metrics.get("prefill_includes_queueing") else "prefill"
    )
    truncated = metrics.get("truncated")
    if truncated is True:
        output_tokens = metrics.get("output_tokens")
        if (isinstance(output_tokens, int)
                and not isinstance(output_tokens, bool)):
            truncation = ("output truncated at the output token budget "
                          "({0})".format(output_tokens))
        else:
            truncation = "output truncated at the output token budget"
    elif truncated is False:
        truncation = "output not truncated"
    else:
        truncation = "truncation unavailable"
    return (
        "LLM generation breakdown (node {node}, model {model}): "
        "queueing {queueing}, {prefill_label} {prefill}, "
        "decode {decode}, prompt tokens {prompt}, image tokens {image}, "
        "output tokens {output}; {truncation}".format(
            node=node_id,
            model=model_name,
            queueing=_ms("queueing_ms"),
            prefill_label=prefill_label,
            prefill=_ms("prefill_ms"),
            decode=_ms("decode_ms"),
            prompt=_count("prompt_tokens"),
            image=_count("image_tokens"),
            output=_count("output_tokens"),
            truncation=truncation,
        )
    )


def _merge_generation_metrics(
    outcome: Dict[str, Any],
    metrics: Optional[Dict[str, Any]],
    node_id: Any,
) -> Dict[str, Any]:
    """Merge a captured ``generation_metrics`` dict additively into a
    node outcome as ``outcome["generation_metrics"]``
    (vllm-workflow-latency-optimization Requirements 1.2, 1.5).

    ``None`` (no metrics captured) is a no-op — the outcome stays
    byte-identical to a metrics-less run. Contained: a merge failure is
    logged at debug and the outcome is returned unchanged, never
    affecting the node outcome shape or the run state."""
    if metrics is None:
        return outcome
    try:
        outcome["generation_metrics"] = metrics
    except Exception:  # noqa: BLE001 - metrics are best-effort
        logger.debug(
            "LLM inference node %s: generation-metrics outcome merge "
            "failed; ignored", node_id, exc_info=True,
        )
    return outcome


def _default_llm_invoker(
    model_name: str,
    prompt: str,
    parameters: Dict[str, Any],
    image_b64: Optional[str] = None,
    reference_b64: Optional[str] = None,
    system_prompt: Optional[str] = None,
    *,
    metrics_sink: Optional[Callable[[dict], None]] = None,
) -> str:
    """POST the rendered prompt to the local Text_Generation_API and
    return the generated text. ``requests`` is imported lazily so this
    module stays importable everywhere; any HTTP/validation failure is
    raised for the processor to record as the node's error.

    ``image_b64`` (edge-vlm-image-inference Requirement 2.1) carries the
    captured frame as a base64-encoded JPEG; when set it rides the POST
    body as the API's optional ``image`` field. When ``None`` the body
    is byte-identical to the pre-feature request (Requirement 2.2).

    ``reference_b64`` (vlm-anomaly-reference-parity Requirement 4.3)
    carries the captured reference frame the same way; when set it rides
    the POST body as the API's optional ``reference_image`` field beside
    ``image``. When ``None`` the body is byte-identical to the
    reference-less request.

    ``system_prompt`` (json-trigger-metadata-pipeline Requirements 8.2,
    8.5), when non-empty, rides the POST body verbatim as the API's
    optional ``system_prompt`` field; when ``None``/empty the body is
    byte-identical to the pre-feature request.

    ``metrics_sink`` (vllm-workflow-latency-optimization Requirements
    1.2, 1.5), when provided, receives the 200 response's additive
    ``generation_metrics`` payload (``payload.get("generation_metrics")``,
    which is ``None`` for a metrics-less response) before the generated
    text is returned. The call is contained: a raising sink is logged at
    debug and never disturbs the generated-text return or the request
    semantics.

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
    if image_b64 is not None:
        body["image"] = image_b64
    if reference_b64 is not None:
        body["reference_image"] = reference_b64
    if system_prompt:
        body["system_prompt"] = system_prompt
    url = TEXT_GENERATION_URL.format(model_name=model_name)
    deadline = time.monotonic() + LLM_LOADING_BUDGET_SEC
    while True:
        response = requests.post(
            url,
            json=body,
            timeout=LLM_GENERATION_TIMEOUT_SEC,
        )
        if response.status_code == 200:
            payload = response.json()
            if metrics_sink is not None:
                # Generation-metrics return path (R1.2), contained
                # (R1.5): a sink failure never disturbs the generated
                # text or the request semantics.
                try:
                    metrics_sink(payload.get("generation_metrics"))
                except Exception:  # noqa: BLE001 - metrics are best-effort
                    logger.debug(
                        "generation_metrics sink raised for model %s; "
                        "ignored", model_name, exc_info=True,
                    )
            return str(payload.get("generated_text", ""))
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

    def process(
        self,
        document: dict,
        tag_values: dict,
        work_dir: Optional[str] = None,
        duration_sink: Optional[Callable[[Optional[str], float], None]] = None,
    ) -> Dict[str, Any]:
        """Run every llm_inference binding and return the run's inference
        metadata with each node's outcome merged under
        ``metadata['llm'][nodeId]`` (Requirements 7.4, 7.7).

        ``work_dir`` (edge-vlm-image-inference Requirement 2.4) is the
        per-run work directory substituted for the ``{work_dir}``
        placeholder in a binding's ``capturePaths``; it stays optional so
        pre-feature call sites (and compiled documents without
        ``capturePaths``) keep byte-identical behavior (Requirement 6.1).

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
        Flat keys follow the documented last-writer-wins convention.

        ``duration_sink`` (optional; default None → behavior byte-identical
        to today) receives ``(node_id, elapsed_ms)`` for every invocation,
        measured with the monotonic clock and reported in a ``try/finally``
        (node-execution-timing Requirements 1.3, 1.7)."""
        metadata = dict(tag_values or {})
        bindings = self.bindings(document)
        if not bindings:
            return metadata
        metadata["llm"] = dict(metadata.get("llm") or {})
        for binding in bindings:
            node_id = binding.get("nodeId")
            started = time.monotonic()
            try:
                outcome = self._run_one(binding, metadata, work_dir)
            finally:
                _emit_duration(
                    duration_sink, node_id,
                    (time.monotonic() - started) * 1000.0,
                )
            metadata["llm"][node_id] = outcome
            for key in ("is_anomalous", "confidence"):
                if key in outcome:
                    metadata[key] = outcome[key]
        return metadata

    def _run_one(
        self,
        binding: dict,
        metadata: Dict[str, Any],
        work_dir: Optional[str] = None,
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
        # Optional system prompt, normalized like Bedrock's:
        # absent/empty/whitespace-only ⇒ None; otherwise the raw
        # configured text verbatim (never rendered, never stripped) so
        # the operator's text reaches the model unmodified. Anomaly
        # mode above touches the rendered user prompt only — the system
        # prompt is never modified (json-trigger-metadata-pipeline
        # Requirements 8.2, 8.5, 8.9).
        raw_system = parameters.get("system_prompt")
        system_prompt = (
            str(raw_system)
            if raw_system is not None and str(raw_system).strip()
            else None
        )
        # Image downscaling option (vllm-workflow-latency-optimization
        # Requirements 5.3-5.8), resolved once per binding. Unconfigured
        # (absent/None) ⇒ the downscaling code path below is skipped
        # entirely, so the encoded bytes and request body stay
        # byte-identical to pre-feature behavior (R5.6). An invalid
        # configured value (non-positive, non-numeric, bool,
        # non-integral) is treated as unconfigured with one run-log
        # WARNING naming the rejected value (R5.8). No captured image ⇒
        # no downscaling attempt (R5.5).
        max_image_dimension, dimension_notice = resolve_max_image_dimension(
            parameters.get("max_image_dimension"))
        if dimension_notice is not None:
            logger.warning(
                "LLM inference node %s: %s", node_id, dimension_notice)
        # Captured-frame attachment (edge-vlm-image-inference
        # Requirements 2.1, 2.2, 2.3). Three capturePaths shapes:
        # - 'in' maps to a path and the resolved file is readable →
        #   the frame rides the invocation base64-encoded;
        # - capturePaths absent or 'in' is None (pre-feature package /
        #   unfed port) → no image, request byte-identical to today;
        # - 'in' maps to a path but the file cannot be read → contained
        #   node error naming node/port/path, invoker never called —
        #   silently answering without the image is the bug being fixed.
        image_b64: Optional[str] = None
        capture_paths = binding.get("capturePaths") or {}
        port = "in"
        path = capture_paths.get(port)
        if path:
            if work_dir:
                path = path.replace("{work_dir}", work_dir)
            try:
                with open(path, "rb") as f:
                    frame_bytes = f.read()
            except OSError as e:
                logger.error(
                    "LLM inference node %s failed: could not read the "
                    "captured '%s' frame from %s (%s); other bindings "
                    "are unaffected", node_id, port, path, e,
                )
                return {
                    "error": (
                        "LLM inference node '{0}' could not read the "
                        "captured '{1}' frame from {2}: {3}".format(
                            node_id, port, path, e)
                    )
                }
            if max_image_dimension is not None:
                # Downscale after the read, before base64 encoding
                # (R5.3); a failure logs a WARNING and sends the
                # original bytes (R5.4).
                frame_bytes = _downscale_frame_or_original(
                    frame_bytes, max_image_dimension, node_id, port)
            image_b64 = base64.b64encode(frame_bytes).decode("ascii")
        # Reference-frame attachment (vlm-bedrock-parity Requirements
        # 3.1, 3.2, 3.3). Three shapes, and the FED-but-unreadable case
        # FAILS CLOSED — this supersedes vlm-anomaly-reference-parity
        # Requirement 4.2's degrade-to-single-image rule:
        # - reference unfed (None) or key absent (pre-feature package) →
        #   single-image inference on the input frame alone (3.3);
        # - fed and readable → the frame rides the invocation
        #   base64-encoded beside the input frame (3.1);
        # - fed but unreadable → contained node error naming node,
        #   port and resolved path, invoker never called (3.2). The
        #   author asked for a comparison the device could not deliver;
        #   answering anyway yields a confident verdict about an image
        #   the model never saw.
        # Bedrock keeps degrading (Requirement 6.4) — only this node
        # type moves.
        reference_b64: Optional[str] = None
        reference_path = capture_paths.get("reference")
        if not reference_path:
            logger.warning(
                "LLM inference node '%s': no captured reference frame "
                "(reference port unfed or pre-feature package); "
                "performing single-image inference", node_id)
        else:
            if work_dir:
                reference_path = reference_path.replace(
                    "{work_dir}", work_dir)
            try:
                with open(reference_path, "rb") as f:
                    reference_bytes = f.read()
            except OSError as e:
                # FAIL CLOSED (vlm-bedrock-parity Requirement 3.2): a
                # fed-but-unreadable reference is a contained node
                # error — the invoker is never called.
                logger.error(
                    "LLM inference node %s failed: could not read the "
                    "captured '%s' frame from %s (%s); other bindings "
                    "are unaffected", node_id, "reference",
                    reference_path, e,
                )
                return {
                    "error": (
                        "LLM inference node '{0}' could not read the "
                        "captured '{1}' frame from {2}: {3}".format(
                            node_id, "reference", reference_path, e)
                    )
                }
            else:
                if max_image_dimension is not None:
                    # Same downscaling treatment as the 'in' frame
                    # (R5.3, R5.4): both captured images the request
                    # sends contribute image tokens to prefill.
                    reference_bytes = _downscale_frame_or_original(
                        reference_bytes, max_image_dimension, node_id,
                        "reference")
                reference_b64 = base64.b64encode(
                    reference_bytes).decode("ascii")
        # Output_Token_Budget resolution (vllm-workflow-latency-
        # optimization Requirements 3.1, 3.3, 3.4): every LLM_Binding
        # invocation carries an explicit ``max_tokens`` — the configured
        # value when valid, the documented 256-token default otherwise.
        # A substitution is logged at WARNING (run-log capture is active
        # during binding processing) naming the rejected value, so a
        # previously-invalid configured value no longer fails the node —
        # it generates with the documented default.
        budget, budget_notice = resolve_output_token_budget(
            parameters.get("max_tokens"))
        if budget_notice is not None:
            logger.warning(
                "LLM inference node %s: %s", node_id, budget_notice)
        parameters["max_tokens"] = budget
        # Generation-metrics capture (vllm-workflow-latency-optimization
        # Requirements 1.2, 1.5): the sink collects the invoker's 200
        # ``generation_metrics`` payload. Only dict payloads are kept —
        # a metrics-less response delivers None and stays a no-op.
        captured_metrics: List[Dict[str, Any]] = []

        def _capture_metrics(metrics: Any) -> None:
            if isinstance(metrics, dict):
                captured_metrics.append(metrics)

        try:
            model_name = str(parameters.get("modelName") or "")
            if image_b64 is not None and reference_b64 is not None:
                # Both frames: the extended 5-argument invocation
                # (Requirement 4.1). A reference can only ride beside
                # the input image — the 'in' error path above returns
                # before the reference is read, matching the API's
                # reference-requires-image rule.
                invoker_args = (
                    model_name, prompt, parameters, image_b64,
                    reference_b64,
                )
            elif image_b64 is not None:
                # Input frame only: the shipped 4-argument form stays
                # byte-identical so pre-feature injected invokers keep
                # working unchanged (Requirements 4.2, 7.1).
                invoker_args = (model_name, prompt, parameters, image_b64)
            else:
                # No frame: the invocation (arity included) stays
                # byte-identical to pre-feature behavior (Requirements
                # 2.2, 6.1) so pre-feature injected invokers keep
                # working unchanged.
                invoker_args = (model_name, prompt, parameters)
            # Pre-feature arity: keywords are only supplied when needed
            # (system_prompt when configured — json-trigger-metadata-
            # pipeline Requirement 8.5) or accepted (metrics_sink only
            # when the possibly-injected invoker declares it — the
            # _accepts_keyword pattern), so injected fakes that predate
            # either parameter keep working unchanged.
            invoker_kwargs: Dict[str, Any] = {}
            if system_prompt is not None:
                invoker_kwargs["system_prompt"] = system_prompt
            if _accepts_keyword(self._invoker, "metrics_sink"):
                invoker_kwargs["metrics_sink"] = _capture_metrics
            text = self._invoker(*invoker_args, **invoker_kwargs)
        except Exception as e:  # noqa: BLE001 - recorded per 7.6, not raised
            logger.error(
                "LLM inference node %s failed: %s; other bindings are "
                "unaffected", node_id, e,
            )
            return {"error": str(e)}
        # Generation-metrics emission and merge (vllm-workflow-latency-
        # optimization Requirements 1.2, 1.5, 3.5, 3.6): with a captured
        # metrics dict, emit one INFO run-log line (run-log capture is
        # active during binding processing) and merge the dict additively
        # into the node outcome below. Both steps are contained — a
        # failure logs at debug and leaves the node outcome and run
        # state exactly as a metrics-less run.
        generation_metrics: Optional[Dict[str, Any]] = (
            captured_metrics[-1] if captured_metrics else None
        )
        if generation_metrics is not None:
            try:
                logger.info(_format_generation_metrics_line(
                    node_id, model_name, generation_metrics))
            except Exception:  # noqa: BLE001 - metrics are best-effort
                logger.debug(
                    "LLM inference node %s: generation-metrics run-log "
                    "emission failed; ignored", node_id, exc_info=True,
                )
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
                return _merge_generation_metrics(
                    {"error": str(e), "generated_text": text},
                    generation_metrics, node_id)
            logger.info("LLM inference binding (node %s) processed", node_id)
            outcome = {"generated_text": text}
            outcome.update(verdict)
            return _merge_generation_metrics(
                outcome, generation_metrics, node_id)
        logger.info("LLM inference binding (node %s) processed", node_id)
        return _merge_generation_metrics(
            {"generated_text": text}, generation_metrics, node_id)


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
        duration_sink: Optional[Callable[[Optional[str], float], None]] = None,
    ) -> None:
        # Forward duration_sink only when one was provided AND the (possibly
        # overridden) process() signature accepts it, so subclasses that
        # override process() without the new keyword keep working unchanged
        # (default None -> behavior byte-identical to today;
        # node-execution-timing R5.2 backward compatibility).
        kwargs: Dict[str, Any] = {"detail_sink": detail_sink}
        if duration_sink is not None and _accepts_keyword(
            self.process, "duration_sink"
        ):
            kwargs["duration_sink"] = duration_sink
        self.process(registration, document, tag_values, **kwargs)

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
        duration_sink: Optional[Callable[[Optional[str], float], None]] = None,
    ) -> None:
        """Process every output binding independently (Requirement 13.7).

        ``detail_sink`` (optional; default None → behavior byte-identical to
        today) receives ``(node_id, detail)`` sent-message / skipped-outcome
        summaries for the output-node-sent-message feature. It is invoked only
        AROUND successful runners and on the gated/condition-skip paths, never
        alters control flow, and a raising sink is contained (Requirement
        3.1).

        ``duration_sink`` (optional; default None → behavior byte-identical
        to today) receives ``(node_id, elapsed_ms)`` for every binding whose
        runner is actually invoked (after the gating checks), measured with
        the monotonic clock and reported in a ``try/finally`` so
        error-terminated invocations are timed too. Gated-out /
        condition-skipped bindings and filter/conditional evaluations report
        nothing (node-execution-timing Requirements 1.3, 1.7).

        Delegates to :meth:`process_subset` with the full binding list
        (detection-guided-bedrock-inspection Requirement 5.7): the
        pre-refactor body lives there unchanged, so behavior is
        byte-identical to the pre-refactor ``process``."""
        bindings = document.get("executorBindings") or []
        self.process_subset(
            document,
            tag_values,
            [binding.get("nodeId") for binding in bindings],
            detail_sink=detail_sink,
            duration_sink=duration_sink,
        )

    def process_subset(
        self,
        document: dict,
        tag_values: dict,
        binding_ids: List[Any],
        detail_sink: Optional[Callable[[Optional[str], str], None]] = None,
        duration_sink: Optional[Callable[[Optional[str], float], None]] = None,
    ) -> None:
        """Process only the executor bindings named by ``binding_ids``
        (detection-guided-bedrock-inspection, Requirement 5.7).

        The pre-refactor ``process`` body, verbatim, over a filtered
        binding list: the named bindings run through the exact same
        gating (inference filters, conditionals), metadata attachment,
        condition evaluation, template rendering, and runner dispatch
        code paths, in ``executorBindings`` emission order.

        ``binding_ids`` are ``nodeId`` values; a binding whose node id is
        not named is not evaluated at all — a branch's filter/conditional
        gates and metadata bindings travel with the branch (see
        ``branching.bedrock_branches``). ``process`` delegates here with
        the full binding list; the Bedrock processor's per-branch
        completion path calls it with one branch's binding ids so each
        inspection's outputs publish as its result lands.

        ``detail_sink`` / ``duration_sink`` are documented on
        :meth:`process` and behave identically here."""
        wanted = set(binding_ids or [])
        bindings = [
            binding
            for binding in (document.get("executorBindings") or [])
            if binding.get("nodeId") in wanted
        ]
        if not bindings:
            return
        metadata = {
            key: _coerce(value) for key, value in dict(tag_values or {}).items()
        }
        # Metadata_Node passthrough (workflow-manager-gaps Requirements
        # 7.1, 7.7, 7.8): resolve every ``metadata`` binding once against
        # the run's Trigger_Context and fan the attached maps out to
        # their ``attachTo`` output nodes. Outputs absent from this map
        # take the exact pre-feature code path (byte-identical payloads).
        attached_by_output = attached_metadata_by_output(
            bindings, metadata.get("trigger") or {}
        )
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
                if kind == BINDING_METADATA:
                    continue  # resolved before the loop; attaches, no action
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
                # workflow-manager-gaps Requirements 7.7, 7.8: an output
                # node listed in a metadata binding's ``attachTo`` gets an
                # effective metadata dict (attached entries visible to
                # ``payload_template`` placeholders and ``condition``
                # expressions, plus a ``metadata_json`` placeholder); every
                # other output sees the unmodified metadata — the exact
                # pre-feature code path.
                attached = attached_by_output.get(node_id)
                effective = (
                    metadata if attached is None
                    else self._effective_metadata(node_id, metadata, attached)
                )
                allowed, skip_detail = self._condition_result(binding, effective)
                if not allowed:
                    if skip_detail is not None:
                        self._emit_detail(detail_sink, node_id, skip_detail)
                    continue

                # Record the SUCCESS detail ONLY after the runner returns
                # successfully; on a runner exception nothing new is recorded
                # (mark_failure already captured the error and set_detail
                # refuses to overwrite failure details, Requirement 3.3).
                parameters = dict(binding.get("parameters") or {})
                started = time.monotonic()
                try:
                    if kind == BINDING_MQTT_PUBLISH:
                        # Only mqtt_publish embeds the attached map into its
                        # emitted payload (Requirement 7.7); opcua_write /
                        # modbus_write / digital_output write scalars and gain
                        # no automatic embedding.
                        detail = runner(parameters, effective, attached)
                    else:
                        detail = runner(parameters, effective)
                finally:
                    _emit_duration(
                        duration_sink, node_id,
                        (time.monotonic() - started) * 1000.0,
                    )
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
    def _effective_metadata(
        node_id: Any, metadata: Dict[str, Any], attached: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build the effective metadata dict for an output binding with
        an attached metadata map (workflow-manager-gaps Requirement 7.7).

        The attached entries extend the run's tag values so
        ``payload_template`` placeholders and ``condition`` expressions
        can reference them; an attached key colliding with an existing
        tag key keeps the tag value (logged). ``metadata_json`` — the
        attached map serialized as JSON — is added as a placeholder for
        templates and conditions."""
        effective = dict(metadata)
        for key, value in attached.items():
            if key in effective:
                logger.info(
                    "Output node %s: attached metadata key %r collides "
                    "with an existing tag value; the existing tag value "
                    "wins", node_id, key,
                )
                continue
            effective[key] = value
        effective["metadata_json"] = json.dumps(
            attached, sort_keys=True, default=str
        )
        return effective

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
        self,
        parameters: Dict[str, Any],
        metadata: Dict[str, Any],
        attached: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Publish the rendered payload to the configured broker (9.5).

        ``attached`` (workflow-manager-gaps Requirement 7.7) is the
        metadata map attached to this output node by upstream
        Metadata_Node bindings; when present the rendered payload embeds
        it — a JSON-object payload merges the entries top-level
        (workflow-result keys win, logged), any other payload is wrapped
        as ``{"payload": <rendered>, "metadata": {...}}``. ``None``
        (no Metadata_Node attaches here) keeps the pre-feature payload
        byte-identical (Requirement 7.8).

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
        if attached is not None:
            payload_text = self._embed_attached_metadata(
                payload_text, attached)
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
    def _embed_attached_metadata(
        payload_text: str, attached: Dict[str, Any]
    ) -> str:
        """Embed an attached metadata map into a rendered MQTT payload
        (workflow-manager-gaps Requirement 7.7).

        A payload that parses as a JSON object gets the attached entries
        merged top-level — a workflow-result key wins a collision
        (logged) so result values are never altered or replaced — and is
        re-serialized. Any other payload (non-JSON text, or JSON that is
        not an object) is wrapped as
        ``{"payload": <rendered text>, "metadata": {...attached}}``. A
        merge re-serialization failure falls back to the wrapped form."""
        try:
            parsed = json.loads(payload_text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            for key in attached:
                if key in parsed:
                    logger.info(
                        "MQTT payload key %r present in both the workflow "
                        "result and the attached metadata; the workflow "
                        "result value wins", key,
                    )
            merged = dict(attached)
            merged.update(parsed)
            try:
                return json.dumps(merged, default=str)
            except (TypeError, ValueError):
                logger.exception(
                    "Re-serializing the metadata-merged MQTT payload "
                    "failed; falling back to the wrapped payload form")
        return json.dumps(
            {"payload": payload_text, "metadata": attached}, default=str)

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
