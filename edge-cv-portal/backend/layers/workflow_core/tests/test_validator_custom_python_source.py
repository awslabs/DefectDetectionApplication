"""Validator example tests for the ``custom_python_source`` node (task 2.4).

The node needs no dedicated validator code beyond the frame-feed
coexistence rule: its ``activation`` port on a ``CATEGORY_INPUT`` node
already falls under the existing V9 single-activation-model rule, and
``code`` is ``required=True`` so a code-less node gets the standard V4
required-parameter finding.

_Requirements: 8.6, 10.5_
"""

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    SEVERITY_ERROR,
    validate,
)
from workflow_core.validator.checks import CODE_V9_MIXED_ACTIVATION_MODEL

_POS = Position(0.0, 0.0)

_PRODUCE_FRAME_CODE = (
    "def produce_frame(context):\n"
    "    return dda_frames.load_image(context['payload_json']['image'])\n"
)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


class TestActivationUnderSingleActivationModel:
    """Requirement 8.6: with a subscription trigger present, the source
    node's ``activation`` port must be connected — enforced by the
    existing V9 rule with no validator change."""

    def _graph(self, activation_connected):
        nodes = [
            _node("msub", "mqtt_subscribe",
                  topic="factory/line1/trigger", greengrass=True),
            _node("src", "custom_python_source", code=_PRODUCE_FRAME_CODE),
            _node("cap", "capture", output_path="/out"),
        ]
        connections = [_conn("c1", "src", "cap")]
        if activation_connected:
            connections.append(
                _conn("c2", "msub", "src", target_port="activation"))
        return WorkflowGraph(nodes=nodes, connections=connections)

    def test_unconnected_activation_gets_v9_finding(self):
        found = _by_code(validate(self._graph(activation_connected=False)),
                         CODE_V9_MIXED_ACTIVATION_MODEL)
        assert [f.node_id for f in found] == ["src"]
        assert all(f.severity == SEVERITY_ERROR for f in found)

    def test_connected_activation_gets_no_v9_finding(self):
        found = _by_code(validate(self._graph(activation_connected=True)),
                         CODE_V9_MIXED_ACTIVATION_MODEL)
        assert found == []


class TestRequiredCode:
    """Requirement 10.5: ``code`` is ``required=True``, so a code-less
    node gets the standard V4 required-parameter finding."""

    def test_missing_code_gets_v4_finding(self):
        graph = WorkflowGraph(
            nodes=[
                _node("src", "custom_python_source"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[_conn("c1", "src", "cap")],
        )
        found = _by_code(validate(graph), CODE_V4_MISSING_REQUIRED_PARAMETER)
        assert [f.node_id for f in found] == ["src"]
        assert all(f.severity == SEVERITY_ERROR for f in found)

    def test_present_code_gets_no_v4_finding(self):
        graph = WorkflowGraph(
            nodes=[
                _node("src", "custom_python_source", code=_PRODUCE_FRAME_CODE),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[_conn("c1", "src", "cap")],
        )
        assert _by_code(validate(graph), CODE_V4_MISSING_REQUIRED_PARAMETER) == []
