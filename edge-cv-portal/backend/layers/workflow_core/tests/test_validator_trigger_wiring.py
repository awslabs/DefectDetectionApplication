"""Example unit tests for the V8/V9 validator wiring
(trigger-activation-runtime task 2.2).

Covers:
- a multi-violation graph returns old and new codes together in a single
  ``validate()`` result — a target-less ``mqtt_subscribe`` (V8), an input
  node with no ``activation`` connection (V9), a missing required
  parameter (V4), and an unused output port (W1) all reported with no
  short-circuiting (Requirement 4.3)
- a connection from an ``mqtt_subscribe`` / ``opcua_subscribe``
  ``PORT_TYPE_EVENT_SIGNAL`` output to an input node's ``activation``
  port is legal stage ordering: zero ``V7_STAGE_ORDER`` findings
  (Requirement 4.4)

_Requirements: 4.3, 4.4_
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
    CODE_W1_UNUSED_OUTPUT_PORT,
    SEVERITY_ERROR,
    validate,
)
from workflow_core.validator.checks import (
    CODE_V7_STAGE_ORDER,
    CODE_V8_MQTT_SUB_NO_TARGET,
    CODE_V9_MIXED_ACTIVATION_MODEL,
)

# --------------------------------------------------------------------------
# Graph-building helpers (same conventions as test_validator_checks.py)
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _codes(findings):
    return [finding.code for finding in findings]


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


# --------------------------------------------------------------------------
# Requirement 4.3: old and new checks run together, no short-circuiting
# --------------------------------------------------------------------------

class TestOldAndNewChecksTogether:
    def _multi_violation_graph(self):
        """A graph seeded with V8, V9, V4, and W1 conditions at once.

        - ``msub``: mqtt_subscribe with a topic but no connection target
          (no ``greengrass``, no ``aws_iot``, no ``broker_host``) -> V8;
          its unconnected ``out`` port also raises W1_UNUSED_OUTPUT_PORT.
        - ``src``: folder_source missing its required ``location`` (V4)
          and, because a subscription trigger is present, missing an
          ``activation`` connection (V9).
        - ``cap``: capture fed from ``src`` (keeps V1 satisfied).
        """
        return WorkflowGraph(
            nodes=[
                _node("msub", "mqtt_subscribe", topic="factory/line1/#"),
                _node("src", "folder_source"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[_conn("c1", "src", "cap")],
        )

    def test_v8_v9_and_existing_codes_reported_in_one_result(self):
        # Validates: Requirements 4.3
        codes = set(_codes(validate(self._multi_violation_graph())))
        assert CODE_V8_MQTT_SUB_NO_TARGET in codes
        assert CODE_V9_MIXED_ACTIVATION_MODEL in codes
        assert CODE_V4_MISSING_REQUIRED_PARAMETER in codes
        assert CODE_W1_UNUSED_OUTPUT_PORT in codes

    def test_v8_finding_names_the_target_less_trigger_node(self):
        # Validates: Requirements 4.3
        found = _by_code(validate(self._multi_violation_graph()),
                         CODE_V8_MQTT_SUB_NO_TARGET)
        assert len(found) == 1
        assert found[0].node_id == "msub"
        assert found[0].severity == SEVERITY_ERROR

    def test_v9_finding_names_the_unconnected_input_node(self):
        # Validates: Requirements 4.3
        found = _by_code(validate(self._multi_violation_graph()),
                         CODE_V9_MIXED_ACTIVATION_MODEL)
        assert len(found) == 1
        assert found[0].node_id == "src"
        assert found[0].severity == SEVERITY_ERROR

    def test_existing_findings_identify_their_own_nodes(self):
        # Validates: Requirements 4.3
        findings = validate(self._multi_violation_graph())
        v4 = _by_code(findings, CODE_V4_MISSING_REQUIRED_PARAMETER)
        assert [f.node_id for f in v4] == ["src"]
        w1_nodes = {f.node_id for f in _by_code(findings,
                                                CODE_W1_UNUSED_OUTPUT_PORT)}
        assert "msub" in w1_nodes


# --------------------------------------------------------------------------
# Requirement 4.4: trigger output -> activation port is legal stage ordering
# --------------------------------------------------------------------------

class TestActivationEdgeIsLegalStageOrdering:
    def test_mqtt_subscribe_to_activation_port_has_zero_v7_findings(self):
        # Validates: Requirements 4.4
        graph = WorkflowGraph(
            nodes=[
                _node("msub", "mqtt_subscribe",
                      topic="factory/line1/#", greengrass=True),
                _node("src", "folder_source", location="/data/images"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[
                _conn("t1", "msub", "src", target_port="activation"),
                _conn("c1", "src", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_V7_STAGE_ORDER) == []

    def test_opcua_subscribe_to_activation_port_has_zero_v7_findings(self):
        # Validates: Requirements 4.4
        # The descriptor's own ``node_id`` parameter collides with the
        # helper's positional argument, so pass parameters explicitly.
        graph = WorkflowGraph(
            nodes=[
                Node(id="osub", type="opcua_subscribe", position=_POS,
                     parameters={"endpoint": "opc.tcp://plc.local:4840",
                                 "node_id": "ns=2;i=5"}),
                _node("uni", "unified_input", source_kind="folder",
                      location="/data/images"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[
                _conn("t1", "osub", "uni", target_port="activation"),
                _conn("c1", "uni", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_V7_STAGE_ORDER) == []

    def test_fully_wired_trigger_graph_has_no_v7_v8_or_v9_errors(self):
        # Validates: Requirements 4.4
        # A properly targeted trigger driving the input's activation port
        # satisfies all three trigger-related checks at once.
        graph = WorkflowGraph(
            nodes=[
                _node("msub", "mqtt_subscribe",
                      topic="factory/line1/#", broker_host="broker.local"),
                _node("src", "folder_source", location="/data/images"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[
                _conn("t1", "msub", "src", target_port="activation"),
                _conn("c1", "src", "cap"),
            ],
        )
        codes = _codes(validate(graph))
        assert CODE_V7_STAGE_ORDER not in codes
        assert CODE_V8_MQTT_SUB_NO_TARGET not in codes
        assert CODE_V9_MIXED_ACTIVATION_MODEL not in codes
