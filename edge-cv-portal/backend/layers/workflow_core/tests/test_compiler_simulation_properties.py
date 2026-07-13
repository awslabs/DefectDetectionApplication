"""Property test for simulation stubbing (task 4.8).

**Feature: workflow-manager, Property 14: Simulation stubs exactly the hardware-dependent nodes**

For all valid Workflow_Definitions compiled with ``simulation=true``,
every hardware-dependent node (per the catalog flag) is mapped to a
recording stub, no hardware executor binding or hardware element remains
in the output, and every non-hardware-dependent node compiles identically
to the non-simulation output.

**Validates: Requirements 12.6**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    ARCH_SIM,
    DEVICE_ARCHITECTURES,
    SIM_RECORDING_BINDING_PREFIX,
    are_port_types_compatible,
    get_node_type,
)
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

from .generators import graph_strategy, node_parameters_strategy

#: Hardware-dependent input node types (input nodes are reachability
#: roots, so they can be added to a valid graph without wiring; their
#: unused output port is at most a warning). folder_source reads the
#: device file system, so it is hardware-dependent too: every input
#: node type stubs to a dataset-fed source in simulation.
_HARDWARE_INPUT_TYPES = ("camera_source", "folder_source", "digital_input")

#: Hardware-dependent output node types (must be wired onto a
#: type-compatible source to stay reachable).
_HARDWARE_OUTPUT_TYPES = ("digital_output", "mqtt_publish", "opcua_write")

#: Non-hardware node types for the minimal-hardware graphs (everything
#: except the required frame source).
_NO_HARDWARE_INTERMEDIATE_TYPES = ("dewarp", "rotate", "crop", "format_convert")

#: Linking elements the compiler synthesizes (not tied to any node).
_SYNTHETIC_FACTORIES = {"tee", "queue", "funnel"}


# --------------------------------------------------------------------------
# Graph strategies: the shared graph_strategy may or may not include
# hardware-dependent nodes, so it is complemented with a variant that
# guarantees hardware nodes and one that guarantees their absence.
# --------------------------------------------------------------------------

def _compatible_sources(nodes, input_port_type):
    """Every (node_id, port_name) output port among ``nodes`` whose
    effective port type can feed ``input_port_type``."""
    sources = []
    for node in nodes:
        descriptor = get_node_type(node.type)
        parameter_names = {p.name for p in descriptor.parameters}
        override = None
        if "output_port_type" in parameter_names:
            override = node.parameters.get("output_port_type")
        for port in descriptor.outputs:
            effective_type = override or port.port_type
            if are_port_types_compatible(effective_type, input_port_type):
                sources.append((node.id, port.name))
    return sources


def _fresh_id(candidate, used):
    while candidate in used:
        candidate += "_"
    used.add(candidate)
    return candidate


@st.composite
def graph_with_hardware_strategy(draw):
    """Valid Workflow_Definitions guaranteed to contain at least one
    hardware-dependent node.

    Draws a validator-valid base graph from the shared ``graph_strategy``
    and adds one hardware input node (always reachable, no wiring needed)
    plus 0..2 hardware output nodes wired onto type-compatible sources
    where such a source exists, so the graph stays validator-valid.
    """
    base = draw(graph_strategy())
    nodes = list(base.nodes)
    connections = list(base.connections)
    used_node_ids = {node.id for node in nodes}
    used_connection_ids = {connection.id for connection in connections}

    input_type = draw(st.sampled_from(_HARDWARE_INPUT_TYPES))
    descriptor = get_node_type(input_type)
    nodes.append(Node(
        id=_fresh_id("p14-hw-in", used_node_ids),
        type=input_type,
        position=Position(x=0.0, y=-1.0),
        parameters=draw(node_parameters_strategy(descriptor)),
    ))

    for index in range(draw(st.integers(min_value=0, max_value=2))):
        output_type = draw(st.sampled_from(_HARDWARE_OUTPUT_TYPES))
        descriptor = get_node_type(output_type)
        input_port = descriptor.inputs[0]
        sources = _compatible_sources(nodes, input_port.port_type)
        if not sources:
            continue
        node_id = _fresh_id("p14-hw-out-{0}".format(index), used_node_ids)
        nodes.append(Node(
            id=node_id,
            type=output_type,
            position=Position(x=1.0, y=float(index)),
            parameters=draw(node_parameters_strategy(descriptor)),
        ))
        source_node, source_port = draw(st.sampled_from(sources))
        connections.append(Connection(
            id=_fresh_id("p14-conn-{0}".format(index), used_connection_ids),
            source=PortEndpoint(node=source_node, port=source_port),
            target=PortEndpoint(node=node_id, port=input_port.name),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


@st.composite
def graph_with_minimal_hardware_strategy(draw):
    """Valid Workflow_Definitions where the *only* hardware-dependent
    node is the required frame source: folder_source -> 0..3
    preprocessing intermediates -> 1..2 capture sinks (two captures off
    the same source exercise fan-out).

    Hardware-free valid graphs no longer exist: every input node type is
    hardware-dependent (all frame/event sources are dataset-fed in
    simulation) and validator check V1 requires an input node. This
    regime exercises the boundary where exactly one node stubs while
    everything downstream must compile identically."""
    nodes = []
    connections = []

    descriptor = get_node_type("folder_source")
    source_node = Node(
        id="p14-src",
        type="folder_source",
        position=Position(x=0.0, y=0.0),
        parameters=draw(node_parameters_strategy(descriptor)),
    )
    nodes.append(source_node)
    last_source = (source_node.id, descriptor.outputs[0].name)

    for index in range(draw(st.integers(min_value=0, max_value=3))):
        type_id = draw(st.sampled_from(_NO_HARDWARE_INTERMEDIATE_TYPES))
        descriptor = get_node_type(type_id)
        node = Node(
            id="p14-mid-{0}".format(index),
            type=type_id,
            position=Position(x=float(index + 1), y=0.0),
            parameters=draw(node_parameters_strategy(descriptor)),
        )
        nodes.append(node)
        connections.append(Connection(
            id="p14-c-{0}".format(len(connections)),
            source=PortEndpoint(node=last_source[0], port=last_source[1]),
            target=PortEndpoint(node=node.id, port=descriptor.inputs[0].name),
        ))
        last_source = (node.id, descriptor.outputs[0].name)

    descriptor = get_node_type("capture")
    for index in range(1 + draw(st.integers(min_value=0, max_value=1))):
        node = Node(
            id="p14-cap-{0}".format(index),
            type="capture",
            position=Position(x=9.0, y=float(index)),
            parameters=draw(node_parameters_strategy(descriptor)),
        )
        nodes.append(node)
        connections.append(Connection(
            id="p14-c-{0}".format(len(connections)),
            source=PortEndpoint(node=last_source[0], port=last_source[1]),
            target=PortEndpoint(node=node.id, port=descriptor.inputs[0].name),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


def simulation_graph_strategy():
    """Valid Workflow_Definitions covering all three regimes: as-drawn
    (arbitrary hardware/non-hardware mix), guaranteed extra hardware
    nodes beyond the source, and minimal hardware (only the required
    dataset-fed frame source)."""
    return st.one_of(
        graph_strategy(),
        graph_with_hardware_strategy(),
        graph_with_minimal_hardware_strategy(),
    )


# --------------------------------------------------------------------------
# Helpers over compiled documents
# --------------------------------------------------------------------------

def _all_elements(document):
    return [element for segment in document.segments for element in segment["elements"]]


def _elements_of(document, node_id):
    return [e for e in _all_elements(document) if e["nodeId"] == node_id]


def _bindings_by_node(document):
    return {binding["nodeId"]: binding for binding in document.executor_bindings}


def _compile_ok(graph, target_arch, simulation):
    result = compile(graph, target_arch, simulation=simulation)
    assert isinstance(result, CompiledPipelineDocument), (
        "compilation of a valid graph failed (simulation={0}): {1}".format(
            simulation, result)
    )
    return result


# --------------------------------------------------------------------------
# Property 14
# --------------------------------------------------------------------------

@given(
    graph=simulation_graph_strategy(),
    target_arch=st.sampled_from(DEVICE_ARCHITECTURES),
)
def test_simulation_stubs_exactly_the_hardware_dependent_nodes(graph, target_arch):
    """**Feature: workflow-manager, Property 14: Simulation stubs exactly the hardware-dependent nodes**

    **Validates: Requirements 12.6**
    """
    hardware_nodes = [
        node for node in graph.nodes if get_node_type(node.type).hardware_dependent
    ]
    non_hardware_nodes = [
        node for node in graph.nodes if not get_node_type(node.type).hardware_dependent
    ]

    non_sim = _compile_ok(graph, target_arch, simulation=False)
    sim = _compile_ok(graph, target_arch, simulation=True)

    sim_bindings = _bindings_by_node(sim)
    non_sim_bindings = _bindings_by_node(non_sim)

    # 1. Every hardware-dependent node is mapped to a recording stub: its
    #    compiled output is exactly the catalog's sim-architecture mapping
    #    (recording_* executor binding for outputs, dataset-fed source
    #    elements for inputs), never the target-arch hardware mapping.
    for node in hardware_nodes:
        sim_mapping = get_node_type(node.type).mapping_for(ARCH_SIM)
        assert sim_mapping is not None, (
            "hardware-dependent type '{0}' lacks a sim mapping".format(node.type)
        )
        if sim_mapping.executor_binding:
            assert node.id in sim_bindings, (
                "hardware node '{0}' has no recording binding".format(node.id)
            )
            binding = sim_bindings[node.id]["binding"]
            assert binding == sim_mapping.executor_binding
            assert binding.startswith(SIM_RECORDING_BINDING_PREFIX), (
                "hardware node '{0}' bound to non-recording '{1}'".format(
                    node.id, binding)
            )
            assert _elements_of(sim, node.id) == [], (
                "recording-stubbed node '{0}' still contributes elements".format(
                    node.id)
            )
        else:
            factories = [e["factory"] for e in _elements_of(sim, node.id)]
            expected = [t["factory"] for t in sim_mapping.element_chain]
            assert factories == expected, (
                "hardware node '{0}' compiled to {1}, expected sim stub {2}".format(
                    node.id, factories, expected)
            )
            assert node.id not in sim_bindings

    # 2. No hardware executor binding or hardware element remains: the
    #    hardware nodes' target-arch bindings are absent, and every
    #    element in the simulation output is accounted for by a graph
    #    node's (stubbed or unchanged) chain or a synthetic link element.
    device_hardware_bindings = set()
    for node in hardware_nodes:
        device_mapping = get_node_type(node.type).mapping_for(target_arch)
        if device_mapping is not None and device_mapping.executor_binding:
            device_hardware_bindings.add(device_mapping.executor_binding)
    remaining_bindings = {b["binding"] for b in sim.executor_bindings}
    assert not device_hardware_bindings & remaining_bindings, (
        "hardware executor bindings remain in simulation output: {0}".format(
            sorted(device_hardware_bindings & remaining_bindings))
    )

    graph_node_ids = {node.id for node in graph.nodes}
    for element in _all_elements(sim):
        if element["nodeId"] is None:
            assert element["factory"] in _SYNTHETIC_FACTORIES, (
                "unexpected untagged element '{0}'".format(element["factory"])
            )
        else:
            assert element["nodeId"] in graph_node_ids

    expected_binding_nodes = (
        {node.id for node in hardware_nodes
         if get_node_type(node.type).mapping_for(ARCH_SIM).executor_binding}
        | {node.id for node in non_hardware_nodes if node.id in non_sim_bindings}
    )
    assert set(sim_bindings) == expected_binding_nodes, (
        "simulation executor bindings cover the wrong node set"
    )

    # 3. Every non-hardware-dependent node compiles identically to the
    #    non-simulation output: same tagged element chain (factories and
    #    args) and same executor binding.
    for node in non_hardware_nodes:
        assert _elements_of(sim, node.id) == _elements_of(non_sim, node.id), (
            "non-hardware node '{0}' compiled differently in simulation".format(
                node.id)
        )
        assert sim_bindings.get(node.id) == non_sim_bindings.get(node.id), (
            "non-hardware node '{0}' has a different executor binding "
            "in simulation".format(node.id)
        )
