"""Property test for the Custom Python source's compiled appsrc
(custom-python-source task 1.3).

**Feature: custom-python-source, Property 20: Compilation emits one node-tagged appsrc per source node**

For any valid workflow graph embedding a Custom_Python_Source_Node with
an arbitrary node id, compiling for any device architecture yields
exactly one ``appsrc`` element named ``appsrc_{nodeId}`` tagged with
that node's id, and no other document element carries that node's id as
an ``appsrc`` — confirming the existing ``{nodeId}`` derivation covers
the new node type with zero compiler changes.

**Validates: Requirements 9.3**
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
)
from workflow_core.validator import SEVERITY_ERROR, validate

from .generators import node_parameters_strategy

SOURCE_TYPE_ID = "custom_python_source"

_SOURCE_DESCRIPTOR = get_node_type(SOURCE_TYPE_ID)
_CAPTURE_DESCRIPTOR = get_node_type("capture")

#: VideoFrames -> VideoFrames preprocessing types safely insertable
#: between the source and the capture sink.
_PREPROCESS_TYPE_IDS = ("dewarp", "rotate", "crop", "format_convert")

#: Arbitrary node ids: non-empty text (the compiler's {nodeId} rendering
#: must resolve them verbatim into the appsrc element name).
_node_ids = st.text(min_size=1, max_size=30)


@st.composite
def source_graph_strategy(draw):
    """Valid Workflow_Definitions embedding exactly one
    ``custom_python_source`` node with an arbitrary node id, wired
    through 0..2 preprocessing nodes into a ``capture`` sink."""
    source_id = draw(_node_ids)

    preprocess_types = draw(
        st.lists(st.sampled_from(_PREPROCESS_TYPE_IDS), min_size=0, max_size=2))

    nodes = [Node(
        id=source_id,
        type=SOURCE_TYPE_ID,
        position=Position(x=0.0, y=0.0),
        parameters=draw(node_parameters_strategy(_SOURCE_DESCRIPTOR)),
    )]
    connections = []
    upstream = source_id
    for index, type_id in enumerate(preprocess_types):
        node_id = "prep-{0}".format(index)
        nodes.append(Node(
            id=node_id,
            type=type_id,
            position=Position(x=float(index + 1), y=0.0),
            parameters=draw(node_parameters_strategy(get_node_type(type_id))),
        ))
        connections.append(Connection(
            id="conn-{0}".format(index),
            source=PortEndpoint(node=upstream, port="out"),
            target=PortEndpoint(node=node_id, port="in"),
        ))
        upstream = node_id

    sink_id = "sink-capture"
    nodes.append(Node(
        id=sink_id,
        type="capture",
        position=Position(x=10.0, y=0.0),
        parameters=draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR)),
    ))
    connections.append(Connection(
        id="conn-sink",
        source=PortEndpoint(node=upstream, port="out"),
        target=PortEndpoint(node=sink_id, port="in"),
    ))

    # The drawn source id must not collide with the synthetic ids.
    synthetic_ids = {node.id for node in nodes[1:]}
    if source_id in synthetic_ids:
        source_id = source_id + "-src"
        nodes[0] = Node(id=source_id, type=SOURCE_TYPE_ID,
                        position=Position(x=0.0, y=0.0),
                        parameters=nodes[0].parameters)
        connections[0] = Connection(
            id=connections[0].id,
            source=PortEndpoint(node=source_id, port="out"),
            target=connections[0].target,
        )

    return WorkflowGraph(nodes=nodes, connections=connections), source_id


@given(graph_and_id=source_graph_strategy())
def test_compilation_emits_one_node_tagged_appsrc_per_source_node(graph_and_id):
    """**Feature: custom-python-source, Property 20: Compilation emits one node-tagged appsrc per source node**

    **Validates: Requirements 9.3**
    """
    graph, source_id = graph_and_id

    errors = [finding for finding in validate(graph)
              if finding.severity == SEVERITY_ERROR]
    assert not errors, (
        "validate reported errors for a valid Custom Python source "
        "definition: {0}".format(errors))

    expected_name = "appsrc_{0}".format(source_id)

    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument), (
            "compile failed on '{0}': {1}".format(arch, compiled))

        elements = [element
                    for segment in compiled.segments
                    for element in segment["elements"]]

        # The source node's own chain is the Aravis-style appsrc head.
        source_elements = [e for e in elements if e["nodeId"] == source_id]
        assert [e["factory"] for e in source_elements] == \
            ["appsrc", "videoconvert"], (
                "source chain on '{0}' is {1}".format(
                    arch, [e["factory"] for e in source_elements]))

        # Exactly one appsrc carries the node's id, named appsrc_{nodeId}.
        node_appsrcs = [e for e in elements
                        if e["factory"] == "appsrc"
                        and e["nodeId"] == source_id]
        assert len(node_appsrcs) == 1, (
            "expected exactly one node-tagged appsrc on '{0}', got "
            "{1}".format(arch, node_appsrcs))
        assert node_appsrcs[0]["args"].get("name") == expected_name, (
            "appsrc name {0!r} did not resolve {{nodeId}} on "
            "'{1}'".format(node_appsrcs[0]["args"].get("name"), arch))

        # No other document element carries that node's id as an appsrc,
        # and no other appsrc claims the node's element name.
        other_appsrcs = [e for e in elements
                         if e["factory"] == "appsrc"
                         and e["nodeId"] != source_id]
        assert all(e["args"].get("name") != expected_name
                   for e in other_appsrcs), (
            "another appsrc claims {0!r} on '{1}'".format(expected_name, arch))
