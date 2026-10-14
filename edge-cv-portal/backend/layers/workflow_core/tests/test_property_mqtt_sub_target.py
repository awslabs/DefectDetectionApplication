# Feature: trigger-activation-runtime, Property 2: mqtt_subscribe must declare a target
"""Property-based test for the ``V8_MQTT_SUB_NO_TARGET`` validator check (task 2.3).

For any ``mqtt_subscribe`` node configuration, ``validate`` produces exactly
one ``V8_MQTT_SUB_NO_TARGET`` error finding for that node if and only if
``greengrass`` is not truthy, ``aws_iot`` is not truthy, and ``broker_host``
is absent or blank (None, empty, or whitespace-only).

The generated graph embeds the trigger in a legal skeleton
(``mqtt_subscribe.out -> unified_input.activation`` with the unified node
feeding a capture sink) so V7/V9 noise stays out of the way; only V8
findings are asserted.

**Validates: Requirements 1.6**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate
from workflow_core.validator.checks import CODE_V8_MQTT_SUB_NO_TARGET

_POS = Position(0.0, 0.0)

#: Sentinel meaning "leave the parameter out of node.parameters entirely".
_ABSENT = object()

#: broker_host values that do NOT declare a plain-broker target.
_BLANK_HOSTS = (None, "", " ", "\t", "  \n\t ")

#: broker_host values that DO declare a plain-broker target.
_NON_EMPTY_HOSTS = ("broker.local", "192.168.1.10", " padded.host ", "ホスト-1")


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


@st.composite
def mqtt_subscribe_target_params(draw):
    """Target parameter combinations for one ``mqtt_subscribe`` node.

    ``greengrass``/``aws_iot`` are each absent, True, or False;
    ``broker_host`` is absent, None, blank, whitespace-only, or non-empty.
    Returns ``(parameters, expects_v8)`` where ``expects_v8`` mirrors the
    Requirement 1.6 rule: no truthy transport bool and no non-empty host.
    """
    parameters = {"topic": draw(st.sampled_from(
        ["factory/line1/#", "sensors/+/temp", "alerts"]
    ))}

    greengrass = draw(st.sampled_from([_ABSENT, True, False]))
    if greengrass is not _ABSENT:
        parameters["greengrass"] = greengrass

    aws_iot = draw(st.sampled_from([_ABSENT, True, False]))
    if aws_iot is not _ABSENT:
        parameters["aws_iot"] = aws_iot

    broker_host = draw(st.sampled_from(
        (_ABSENT,) + _BLANK_HOSTS + _NON_EMPTY_HOSTS
    ))
    if broker_host is not _ABSENT:
        parameters["broker_host"] = broker_host

    has_host = isinstance(broker_host, str) and broker_host.strip() != ""
    expects_v8 = not (greengrass is True or aws_iot is True or has_host)
    return parameters, expects_v8


@st.composite
def trigger_graph_strategy(draw):
    """A legal trigger-driven skeleton around one ``mqtt_subscribe`` node:
    ``trig.out -> uni.activation`` and ``uni.out -> cap.in``. Returns
    ``(graph, expects_v8)``."""
    parameters, expects_v8 = draw(mqtt_subscribe_target_params())
    graph = WorkflowGraph(
        nodes=[
            _node("trig", "mqtt_subscribe", **parameters),
            _node("uni", "unified_input", source_kind="csi_camera"),
            _node("cap", "capture", output_path="/out"),
        ],
        connections=[
            _conn("a1", "trig", "uni", target_port="activation"),
            _conn("c1", "uni", "cap"),
        ],
    )
    return graph, expects_v8


# Feature: trigger-activation-runtime, Property 2: mqtt_subscribe must declare a target
@settings(max_examples=100)
@given(data=trigger_graph_strategy())
def test_v8_fires_exactly_once_iff_no_connection_target(data):
    """**Feature: trigger-activation-runtime, Property 2: mqtt_subscribe must
    declare a target** — for any target parameter combination, ``validate``
    yields exactly one ``V8_MQTT_SUB_NO_TARGET`` error finding for the node
    iff neither ``greengrass`` nor ``aws_iot`` is truthy and ``broker_host``
    is absent or blank; zero V8 findings otherwise.

    **Validates: Requirements 1.6**
    """
    graph, expects_v8 = data
    v8 = [f for f in validate(graph) if f.code == CODE_V8_MQTT_SUB_NO_TARGET]

    if expects_v8:
        assert len(v8) == 1, (
            "expected exactly one V8 finding, got {0}".format(v8))
        finding = v8[0]
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id == "trig"
    else:
        assert v8 == [], (
            "expected zero V8 findings for a targeted config, got {0}".format(v8))
