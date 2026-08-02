"""Property tests for the CSI and ICAM nodes' generic catalog paths.

**Feature: csi-icam-input-nodes**

- Property 1: New input node definitions round-trip and compile through
  the generic catalog paths (Requirements 1.5, 2.5).
- Property 2: A csi_camera_source node compiles with no binding slots —
  its gain/exposure appear in no element argument (Requirement 1.4).
- Property 3: An icam_source node compiles with exactly one binding slot,
  the v4l2src device argument carrying the rendered device value
  verbatim (Requirement 2.4).
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

CSI_TYPE_ID = "csi_camera_source"
ICAM_TYPE_ID = "icam_source"

_CSI_DESCRIPTOR = get_node_type(CSI_TYPE_ID)
_ICAM_DESCRIPTOR = get_node_type(ICAM_TYPE_ID)
_CAPTURE_DESCRIPTOR = get_node_type("capture")


@st.composite
def typed_camera_graph_strategy(draw):
    """Valid Workflow_Definitions guaranteed to contain 1..3 CSI and/or
    ICAM nodes, each feeding its own capture sink so the graph stays
    validator-valid."""
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    for index in range(draw(st.integers(min_value=1, max_value=3))):
        type_id = draw(st.sampled_from([CSI_TYPE_ID, ICAM_TYPE_ID]))
        descriptor = _CSI_DESCRIPTOR if type_id == CSI_TYPE_ID else _ICAM_DESCRIPTOR
        source_id = "typed-src-{0}".format(index)
        sink_id = "typed-cap-{0}".format(index)
        nodes.append(Node(
            id=source_id,
            type=type_id,
            position=Position(x=float(index), y=0.0),
            parameters=draw(node_parameters_strategy(descriptor)),
        ))
        nodes.append(Node(
            id=sink_id,
            type="capture",
            position=Position(x=float(index), y=100.0),
            parameters=draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR)),
        ))
        connections.append(Connection(
            id="typed-conn-{0}".format(index),
            source=PortEndpoint(node=source_id, port="out"),
            target=PortEndpoint(node=sink_id, port="in"),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


def _elements_by_node(document: CompiledPipelineDocument):
    elements_by_node = {}
    for segment in document.segments:
        for element in segment["elements"]:
            node_id = element["nodeId"]
            if node_id is not None:
                elements_by_node.setdefault(node_id, []).append(element)
    return elements_by_node


@given(graph=typed_camera_graph_strategy())
def test_csi_icam_definitions_round_trip_and_compile(graph):
    """**Feature: csi-icam-input-nodes, Property 1**

    **Validates: Requirements 1.5, 2.5**
    """
    typed_nodes = [n for n in graph.nodes
                   if n.type in (CSI_TYPE_ID, ICAM_TYPE_ID)]
    assert typed_nodes, "strategy must produce at least one typed camera node"

    # serialize -> parse equivalence (generic serializer path)
    document = serialize(graph)
    result = parse(document)
    assert result.ok, "parse rejected a serialized definition: {0}".format(
        result.error)
    assert result.graph is not None
    assert result.graph.is_equivalent_to(graph)
    assert graph.is_equivalent_to(result.graph)
    assert serialize(result.graph) == document

    # validation succeeds (generic validator path)
    errors = [f for f in validate(graph) if f.severity == SEVERITY_ERROR]
    assert not errors, "validate reported errors: {0}".format(errors)

    # compilation succeeds per device architecture with the declared chain
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument), (
            "compile failed on '{0}': {1}".format(arch, compiled))
        elements_by_node = _elements_by_node(compiled)
        for node in typed_nodes:
            elements = elements_by_node.get(node.id)
            assert elements, "no elements rendered for '{0}' on '{1}'".format(
                node.id, arch)
            factories = [e["factory"] for e in elements]
            if node.type == ICAM_TYPE_ID:
                assert factories == ["v4l2src", "videoconvert"], (
                    "ICAM '{0}' on '{1}' is {2}".format(node.id, arch, factories))
            else:
                # CSI: file-capture chain (JPEG on non-JP6, pngdec on JP6).
                assert factories[0] == "filesrc", (
                    "CSI '{0}' on '{1}' is {2}".format(node.id, arch, factories))


@given(graph=typed_camera_graph_strategy())
def test_csi_node_has_no_parameter_in_any_element_arg(graph):
    """**Feature: csi-icam-input-nodes, Property 2**

    A csi_camera_source node's rendered gain/exposure appear in no
    element argument, so its binding point carries empty slots.

    **Validates: Requirements 1.4**
    """
    csi_nodes = [n for n in graph.nodes if n.type == CSI_TYPE_ID]
    csi_params = {p.name for p in _CSI_DESCRIPTOR.parameters}
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument)
        elements_by_node = _elements_by_node(compiled)
        for node in csi_nodes:
            # Effective gain/exposure values that would be the slot values.
            values = {p.name: node.parameters.get(p.name, p.default)
                      for p in _CSI_DESCRIPTOR.parameters}
            for element in elements_by_node.get(node.id, []):
                for arg_name, arg_value in (element.get("args") or {}).items():
                    # No CSI param name lands as an arg, and no rendered
                    # gain/exposure value appears verbatim as a slot value.
                    assert arg_name not in csi_params
                    assert arg_value not in {str(v) for v in values.values()}


@given(graph=typed_camera_graph_strategy())
def test_icam_node_has_exactly_one_device_arg(graph):
    """**Feature: csi-icam-input-nodes, Property 3**

    An icam_source node renders exactly one v4l2src device argument
    carrying the rendered device value verbatim.

    **Validates: Requirements 2.4**
    """
    icam_nodes = [n for n in graph.nodes if n.type == ICAM_TYPE_ID]
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument)
        elements_by_node = _elements_by_node(compiled)
        for node in icam_nodes:
            device_value = node.parameters.get("device", "/dev/video0")
            device_args = []
            for element in elements_by_node.get(node.id, []):
                for arg_name, arg_value in (element.get("args") or {}).items():
                    if element["factory"] == "v4l2src" and arg_name == "device":
                        device_args.append(arg_value)
            assert device_args == [device_value], (
                "ICAM '{0}' on '{1}' device args {2}, expected [{3!r}]".format(
                    node.id, arch, device_args, device_value))
