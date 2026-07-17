"""Property test for the Aravis node's generic catalog paths (task 1.4).

**Feature: aravis-camera-input, Property 1: Aravis node definitions round-trip and compile through generic catalog paths**

For any valid workflow definition containing ``aravis_camera_source``
nodes, serializing then parsing the definition produces an equivalent
graph, and validating then compiling it for a device architecture
succeeds and renders the node's appsrc-headed element chain — with the
appsrc ``name`` resolving the ``{nodeId}`` token uniquely per node.

**Validates: Requirements 1.5**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
    parse,
    serialize,
)
from workflow_core.validator import SEVERITY_ERROR, validate

from .generators import graph_strategy, node_parameters_strategy

ARAVIS_TYPE_ID = "aravis_camera_source"

_ARAVIS_DESCRIPTOR = get_node_type(ARAVIS_TYPE_ID)
_CAPTURE_DESCRIPTOR = get_node_type("capture")


@st.composite
def aravis_graph_strategy(draw):
    """Valid Workflow_Definitions guaranteed to contain 1..3
    ``aravis_camera_source`` nodes.

    A drawn boolean starts from a random valid catalog graph (which may
    itself contain Aravis nodes now that the shared generators include
    the type among the video inputs), producing mixed-camera documents;
    otherwise the graph is Aravis-only. Each appended Aravis node feeds
    its own ``capture`` sink so the graph stays validator-valid (input +
    output present, everything reachable, all connections type-
    compatible).
    """
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    for index in range(draw(st.integers(min_value=1, max_value=3))):
        source_id = "aravis-src-{0}".format(index)
        sink_id = "aravis-cap-{0}".format(index)
        nodes.append(Node(
            id=source_id,
            type=ARAVIS_TYPE_ID,
            position=Position(x=float(index), y=0.0),
            parameters=draw(node_parameters_strategy(_ARAVIS_DESCRIPTOR)),
        ))
        nodes.append(Node(
            id=sink_id,
            type="capture",
            position=Position(x=float(index), y=100.0),
            parameters=draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR)),
        ))
        connections.append(Connection(
            id="aravis-conn-{0}".format(index),
            source=PortEndpoint(node=source_id, port="out"),
            target=PortEndpoint(node=sink_id, port="in"),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


def _aravis_elements_by_node(document: CompiledPipelineDocument):
    """nodeId -> ordered element list, for elements tagged with that id."""
    elements_by_node = {}
    for segment in document.segments:
        for element in segment["elements"]:
            node_id = element["nodeId"]
            if node_id is not None:
                elements_by_node.setdefault(node_id, []).append(element)
    return elements_by_node


@given(graph=aravis_graph_strategy())
def test_aravis_definitions_round_trip_and_compile(graph):
    """**Feature: aravis-camera-input, Property 1: Aravis node definitions round-trip and compile through generic catalog paths**

    **Validates: Requirements 1.5**
    """
    aravis_nodes = [node for node in graph.nodes if node.type == ARAVIS_TYPE_ID]
    assert aravis_nodes, "strategy must produce at least one Aravis node"

    # --- serialize -> parse equivalence (generic serializer path) --------
    document = serialize(graph)
    result = parse(document)
    assert result.ok, (
        "parse rejected a serialized Aravis definition: {0}".format(result.error)
    )
    assert result.graph is not None
    assert result.graph.is_equivalent_to(graph), (
        "parsed graph is not equivalent to the original"
    )
    assert graph.is_equivalent_to(result.graph), (
        "original graph is not equivalent to the parsed graph"
    )
    assert serialize(result.graph) == document, (
        "re-serialized document is not byte-identical to the original"
    )

    # --- validation succeeds (generic validator path) --------------------
    errors = [
        finding for finding in validate(graph)
        if finding.severity == SEVERITY_ERROR
    ]
    assert not errors, (
        "validate reported errors for a valid Aravis definition: "
        "{0}".format(errors)
    )

    # --- compilation succeeds per device architecture with the appsrc
    # chain rendered and {nodeId} resolved uniquely per node --------------
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument), (
            "compile failed on '{0}': {1}".format(arch, compiled)
        )

        elements_by_node = _aravis_elements_by_node(compiled)
        appsrc_names = []
        for node in aravis_nodes:
            elements = elements_by_node.get(node.id)
            assert elements, (
                "no elements rendered for Aravis node '{0}' on "
                "'{1}'".format(node.id, arch)
            )
            factories = [element["factory"] for element in elements]
            assert factories == ["appsrc", "videoconvert"], (
                "Aravis node '{0}' chain on '{1}' is {2}, expected "
                "appsrc ! videoconvert".format(node.id, arch, factories)
            )
            name = elements[0]["args"].get("name")
            assert name == "appsrc_{0}".format(node.id), (
                "appsrc name {0!r} did not resolve the {{nodeId}} token "
                "for node '{1}' on '{2}'".format(name, node.id, arch)
            )
            appsrc_names.append(name)

        assert len(set(appsrc_names)) == len(appsrc_names), (
            "appsrc names are not unique per node on '{0}': "
            "{1}".format(arch, appsrc_names)
        )
