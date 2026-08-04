"""Example unit tests for the ``digital_input`` trigger relocation and the
``V7_STAGE_ORDER`` validator check (triggers-stage-and-unified-input task 2.3).

Covers:
- the relocated ``digital_input`` descriptor: ``category == CATEGORY_TRIGGER``
  with parameters, ports, executor binding, and sim stub unchanged
  (Requirements 2.2, 2.3, 2.4)
- ``validate()`` emitting a ``V7_STAGE_ORDER`` error naming the connection
  that targets a trigger node (Requirements 4.2, 4.3, 4.7)
- no V7 finding for a legal ``digital_input.out -> unified_input.activation``
  edge (Requirement 4.4)
- a ``digital_input``-only graph still satisfies V1 and keeps its downstream
  reachable under V5 (Requirement 2.7)
- a zero-trigger graph yields zero V7 findings (Requirement 4.5)

**Validates: Requirements 2.2, 2.3, 2.4, 4.2, 4.3, 4.4, 4.5, 4.7, 2.7**
"""

from workflow_core.catalog import (
    ARCH_SIM,
    CATEGORY_TRIGGER,
    DEVICE_ARCHITECTURES,
    PORT_TYPE_EVENT_SIGNAL,
    get_node_type,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V1_NO_INPUT_NODE,
    CODE_V5_UNREACHABLE_NODE,
    SEVERITY_ERROR,
    validate,
)
from workflow_core.validator.checks import CODE_V7_STAGE_ORDER

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


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


def _params_by_name(descriptor):
    return {p.name: p for p in descriptor.parameters}


# --------------------------------------------------------------------------
# Relocated digital_input descriptor (Requirements 2.2, 2.3, 2.4)
# --------------------------------------------------------------------------

class TestDigitalInputRelocation:
    def test_category_is_trigger(self):
        # Validates: Requirements 2.2
        descriptor = get_node_type("digital_input")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_TRIGGER

    def test_parameters_unchanged(self):
        # Validates: Requirements 2.3
        params = _params_by_name(get_node_type("digital_input"))
        assert set(params) == {"pin", "trigger_edge", "poll_interval_ms"}

        pin = params["pin"]
        assert pin.param_type == "int"
        assert pin.required is True
        assert pin.constraints == {"min": 0, "max": 255}

        edge = params["trigger_edge"]
        assert edge.param_type == "enum"
        assert edge.required is False
        assert edge.default == "rising"
        assert edge.constraints == {"values": ["rising", "falling", "both"]}

        poll = params["poll_interval_ms"]
        assert poll.param_type == "int"
        assert poll.required is False
        assert poll.default == 100
        assert poll.constraints == {"min": 10, "max": 60000}

    def test_ports_unchanged(self):
        # Validates: Requirements 2.3
        descriptor = get_node_type("digital_input")
        assert descriptor.inputs == []
        assert len(descriptor.outputs) == 1
        out = descriptor.outputs[0]
        assert out.name == "out"
        assert out.port_type == PORT_TYPE_EVENT_SIGNAL

    def test_executor_binding_on_every_device_arch(self):
        # Validates: Requirements 2.4
        descriptor = get_node_type("digital_input")
        for arch in DEVICE_ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            assert mapping is not None, "missing device mapping for " + arch
            assert mapping.executor_binding == "digital_input"
            assert mapping.element_chain == []

    def test_sim_appsrc_stub_present(self):
        # Validates: Requirements 2.4
        mapping = get_node_type("digital_input").mapping_for(ARCH_SIM)
        assert mapping is not None
        assert mapping.executor_binding is None
        factories = [element["factory"] for element in mapping.element_chain]
        assert factories == ["appsrc"]
        assert "app" in mapping.plugin_dependencies


# --------------------------------------------------------------------------
# V7 stage-order check (Requirements 4.2, 4.3, 4.4, 4.5, 4.7)
# --------------------------------------------------------------------------

class TestV7StageOrder:
    def test_connection_targeting_trigger_reports_v7_error(self):
        # Validates: Requirements 4.2, 4.3, 4.7
        # folder_source -> digital_input: the target is a trigger node,
        # which may never be downstream of any node.
        graph = WorkflowGraph(
            nodes=[
                _node("src", "folder_source", location="/data/images"),
                _node("din", "digital_input", pin=1),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[_conn("bad", "src", "din")],
        )
        found = _by_code(validate(graph), CODE_V7_STAGE_ORDER)
        assert len(found) == 1
        assert found[0].connection_id == "bad"
        assert found[0].severity == SEVERITY_ERROR

    def test_trigger_to_unified_activation_has_no_v7_finding(self):
        # Validates: Requirements 4.4
        # digital_input.out -> unified_input.activation is the legal edge:
        # the target is the CATEGORY_INPUT unified node, not a trigger.
        graph = WorkflowGraph(
            nodes=[
                _node("din", "digital_input", pin=1),
                _node("uni", "unified_input", source_kind="folder",
                      location="/data/images"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[
                _conn("t1", "din", "uni", target_port="activation"),
                _conn("c1", "uni", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_V7_STAGE_ORDER) == []

    def test_digital_input_only_graph_satisfies_v1_and_v5(self):
        # Validates: Requirements 2.7
        # A graph whose only source is the relocated trigger still counts
        # as having an input-stage root (V1) and keeps its downstream
        # reachable (V5).
        graph = WorkflowGraph(
            nodes=[
                _node("din", "digital_input", pin=3),
                _node("py", "custom_python",
                      code="def handle(x):\n    return x",
                      input_port_type="EventSignal"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[
                _conn("c1", "din", "py"),
                _conn("c2", "py", "cap"),
            ],
        )
        findings = validate(graph)
        assert _by_code(findings, CODE_V1_NO_INPUT_NODE) == []
        assert _by_code(findings, CODE_V5_UNREACHABLE_NODE) == []

    def test_zero_trigger_graph_has_zero_v7_findings(self):
        # Validates: Requirements 4.5
        graph = WorkflowGraph(
            nodes=[
                _node("src", "folder_source", location="/data/images"),
                _node("cap", "capture", output_path="/out"),
            ],
            connections=[_conn("c1", "src", "cap")],
        )
        assert _by_code(validate(graph), CODE_V7_STAGE_ORDER) == []
