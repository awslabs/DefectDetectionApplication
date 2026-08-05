"""Unit tests for the subscribe-side triggers' simulation stubs (task 3.2).

With ``simulation=True``, the hardware-dependent ``mqtt_subscribe`` and
``opcua_subscribe`` trigger nodes resolve to their ``ARCH_SIM`` appsrc
stubs — harness-fed event sources named ``sim_source_<nodeId>`` — exactly
like ``digital_input``: no executor binding is emitted for them and no
broker/server-related content (binding kinds, connection parameters, or
subscription plugin dependencies) reaches the compiled document.

_Requirements: 5.5, 11.1_
"""

import json

from workflow_core.compiler import (
    CompiledPipelineDocument,
    compile,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

# --------------------------------------------------------------------------
# Graph-building helpers (self-contained, mirroring test_compiler_simulation)
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)

#: Subscription plugin dependencies that must never appear in simulation
#: output — the sandbox has no broker or OPC UA server to talk to.
_SUBSCRIPTION_PLUGIN_DEPS = {"python:paho-mqtt", "python:awsiotsdk",
                             "python:opcua"}

#: Trigger executor binding kinds that must never appear in simulation.
_TRIGGER_BINDINGS = {"mqtt_subscribe", "opcua_subscribe"}


def _node(node_id, node_type, parameters):
    # `parameters` is a plain dict (not **kwargs): opcua_subscribe declares
    # a parameter literally named `node_id`, which would collide with the
    # helper's first argument as a keyword.
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=dict(parameters))


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _trigger_graph():
    """Both subscribe triggers driving activation-wired inputs (per V9).

    mqtt_subscribe -> inputA.activation, opcua_subscribe ->
    inputB.activation; each folder_source input feeds its own capture
    sink so the graph is structurally complete (V1/V5).
    """
    return WorkflowGraph(
        nodes=[
            _node("trigMqtt", "mqtt_subscribe",
                  {"topic": "factory/line1/start", "greengrass": True}),
            _node("trigOpc", "opcua_subscribe",
                  {"endpoint": "opc.tcp://plc.local:4840",
                   "node_id": "ns=2;s=Machine1.Trigger"}),
            _node("inputA", "folder_source", {"location": "/data/images"}),
            _node("inputB", "folder_source", {"location": "/data/images"}),
            _node("capA", "capture", {"output_path": "/out/a"}),
            _node("capB", "capture", {"output_path": "/out/b"}),
        ],
        connections=[
            _conn("actA", "trigMqtt", "inputA", target_port="activation"),
            _conn("actB", "trigOpc", "inputB", target_port="activation"),
            _conn("c1", "inputA", "capA"),
            _conn("c2", "inputB", "capB"),
        ],
    )


def _compile_ok(graph, arch="x86_64", simulation=False):
    result = compile(graph, arch, simulation=simulation)
    assert isinstance(result, CompiledPipelineDocument), (
        "expected a document, got errors: {0}".format(result)
    )
    return result


def _all_elements(document):
    return [element for segment in document.segments for element in segment["elements"]]


def _elements_of(document, node_id):
    return [e for e in _all_elements(document) if e["nodeId"] == node_id]


def _bindings_by_node(document):
    return {b["nodeId"]: b for b in document.executor_bindings}


# --------------------------------------------------------------------------
# Trigger nodes stub to appsrc event sources (Requirements 5.5, 11.1)
# --------------------------------------------------------------------------

class TestTriggerSimulationStubs:
    def test_both_triggers_resolve_to_appsrc_stub_chains(self):
        document = _compile_ok(_trigger_graph(), simulation=True)
        for node_id in ("trigMqtt", "trigOpc"):
            elements = _elements_of(document, node_id)
            assert [e["factory"] for e in elements] == ["appsrc"], node_id
            assert elements[0]["nodeId"] == node_id
            assert elements[0]["args"]["name"] == \
                "sim_source_{0}".format(node_id)

    def test_triggers_get_distinct_source_names(self):
        document = _compile_ok(_trigger_graph(), simulation=True)
        names = {
            _elements_of(document, node_id)[0]["args"]["name"]
            for node_id in ("trigMqtt", "trigOpc")
        }
        assert names == {"sim_source_trigMqtt", "sim_source_trigOpc"}

    def test_no_executor_binding_for_trigger_nodes(self):
        # Mirrors digital_input: on a device arch the triggers are
        # executor bindings; in simulation the binding disappears in
        # favor of the appsrc stub chain.
        graph = _trigger_graph()
        non_sim = _compile_ok(graph)
        sim = _compile_ok(graph, simulation=True)

        non_sim_bindings = _bindings_by_node(non_sim)
        assert non_sim_bindings["trigMqtt"]["binding"] == "mqtt_subscribe"
        assert non_sim_bindings["trigMqtt"]["activates"] == ["inputA"]
        assert non_sim_bindings["trigOpc"]["binding"] == "opcua_subscribe"
        assert non_sim_bindings["trigOpc"]["activates"] == ["inputB"]
        assert _elements_of(non_sim, "trigMqtt") == []
        assert _elements_of(non_sim, "trigOpc") == []

        sim_bindings = _bindings_by_node(sim)
        assert "trigMqtt" not in sim_bindings
        assert "trigOpc" not in sim_bindings


# --------------------------------------------------------------------------
# No broker/server content in simulation output (Requirements 5.5, 11.1)
# --------------------------------------------------------------------------

class TestNoBrokerOrServerContent:
    def test_no_trigger_binding_kind_or_activates_key(self):
        document = _compile_ok(_trigger_graph(), simulation=True)
        bindings = {b["binding"] for b in document.executor_bindings}
        assert not bindings & _TRIGGER_BINDINGS
        assert not any("activates" in b for b in document.executor_bindings)

    def test_no_subscription_plugin_dependencies(self):
        graph = _trigger_graph()
        non_sim = _compile_ok(graph)
        sim = _compile_ok(graph, simulation=True)
        # Devices need the subscription clients beyond the
        # LocalServer-bundled set (python:paho-mqtt is bundled, so only
        # awsiotsdk and opcua surface); the sandbox must list none.
        assert {"python:awsiotsdk", "python:opcua"} <= \
            set(non_sim.plugin_dependencies)
        assert not _SUBSCRIPTION_PLUGIN_DEPS & set(sim.plugin_dependencies)

    def test_no_connection_parameters_reach_the_document(self):
        # The topic filter and OPC UA endpoint/node id ride the trigger
        # executor bindings on devices; with the bindings stubbed away,
        # nothing broker- or server-related survives serialization.
        document = _compile_ok(_trigger_graph(), simulation=True)
        serialized = json.dumps(document.to_dict())
        for fragment in ("factory/line1/start", "opc.tcp://plc.local:4840",
                         "ns=2;s=Machine1.Trigger"):
            assert fragment not in serialized
