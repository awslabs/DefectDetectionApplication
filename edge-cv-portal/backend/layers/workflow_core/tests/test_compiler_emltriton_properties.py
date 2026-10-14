"""Property test for emltriton configuration (task 4.5).

**Feature: workflow-manager, Property 6: Inference nodes compile to correctly configured emltriton elements**

For all valid Workflow_Definitions containing model inference nodes, each
such node compiles to exactly one ``emltriton`` element whose args include
the node's configured model name and the Triton model-repository and
server paths used by LocalServer.

**Validates: Requirements 6.2**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    DEVICE_ARCHITECTURES,
    PORT_TYPE_VIDEO_FRAMES,
    are_port_types_compatible,
    get_node_type,
)
from workflow_core.compiler import (
    CompileContext,
    CompiledPipelineDocument,
    DEFAULT_CONTEXT_VALUES,
    compile,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

from .generators import graph_strategy, valid_parameter_value_strategy

_MODEL_INFERENCE = "model_inference"
_MODEL_NAME_DESCRIPTOR = next(
    parameter
    for parameter in get_node_type(_MODEL_INFERENCE).parameters
    if parameter.name == "modelName"
)

#: Realistic overrides for the LocalServer Triton paths, exercised through
#: CompileContext (the mechanism the portal/edge use to supply them).
_REPO_OVERRIDES = (None, "/custom/repo", "/aws_dda/alt/triton_model_repo")
_SERVER_OVERRIDES = (None, "/opt/tritonserver-2", "/usr/local/tritonserver")


def _video_frame_sources(graph):
    """Every (node_id, port_name) output port in ``graph`` whose effective
    port type can feed a model_inference node's VideoFrames input."""
    sources = []
    for node in graph.nodes:
        descriptor = get_node_type(node.type)
        parameter_names = {p.name for p in descriptor.parameters}
        override = None
        if "output_port_type" in parameter_names:
            override = node.parameters.get("output_port_type")
        for port in descriptor.outputs:
            effective_type = override or port.port_type
            if are_port_types_compatible(effective_type, PORT_TYPE_VIDEO_FRAMES):
                sources.append((node.id, port.name))
    return sources


@st.composite
def graph_with_inference_nodes_strategy(draw):
    """Valid Workflow_Definitions guaranteed to contain model inference nodes.

    Draws a validator-valid base graph from the shared ``graph_strategy``
    (which may already include model_inference nodes) and wires 1..3
    additional model_inference nodes onto type-compatible VideoFrames
    output ports; ``graph_strategy`` always places at least one
    VideoFrames-producing input node, so a compatible source always
    exists. Added nodes stay reachable from an input node and their
    dangling InferenceMeta outputs are at most warnings, so the augmented
    graph remains validator-valid (compilable).
    """
    base = draw(graph_strategy())
    sources = _video_frame_sources(base)
    used_node_ids = {node.id for node in base.nodes}
    used_connection_ids = {connection.id for connection in base.connections}

    nodes = list(base.nodes)
    connections = list(base.connections)
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        node_id = "p6-inf-{0}".format(index)
        while node_id in used_node_ids:
            node_id += "_"
        used_node_ids.add(node_id)
        connection_id = "p6-conn-{0}".format(index)
        while connection_id in used_connection_ids:
            connection_id += "_"
        used_connection_ids.add(connection_id)

        model_name = draw(valid_parameter_value_strategy(_MODEL_NAME_DESCRIPTOR))
        nodes.append(Node(
            id=node_id,
            type=_MODEL_INFERENCE,
            position=Position(x=0.0, y=float(index)),
            parameters={"modelName": model_name},
        ))
        source_node, source_port = draw(st.sampled_from(sources))
        connections.append(Connection(
            id=connection_id,
            source=PortEndpoint(node=source_node, port=source_port),
            target=PortEndpoint(node=node_id, port="in"),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


@given(
    graph=graph_with_inference_nodes_strategy(),
    target_arch=st.sampled_from(DEVICE_ARCHITECTURES),
    repo_override=st.sampled_from(_REPO_OVERRIDES),
    server_override=st.sampled_from(_SERVER_OVERRIDES),
)
def test_inference_nodes_compile_to_configured_emltriton_elements(
    graph, target_arch, repo_override, server_override
):
    """**Feature: workflow-manager, Property 6: Inference nodes compile to correctly configured emltriton elements**

    **Validates: Requirements 6.2**
    """
    context_values = {}
    if repo_override is not None:
        context_values["triton_model_repo"] = repo_override
    if server_override is not None:
        context_values["triton_server_path"] = server_override
    context = CompileContext(values=context_values)

    expected_repo = repo_override or DEFAULT_CONTEXT_VALUES["triton_model_repo"]
    expected_server = server_override or DEFAULT_CONTEXT_VALUES["triton_server_path"]

    result = compile(graph, target_arch, context=context)
    assert isinstance(result, CompiledPipelineDocument), (
        "compilation of a valid graph failed: {0}".format(result)
    )

    emltritons_by_node = {}
    for segment in result.segments:
        for element in segment["elements"]:
            if element["factory"] == "emltriton":
                emltritons_by_node.setdefault(element["nodeId"], []).append(element)

    inference_nodes = [node for node in graph.nodes if node.type == _MODEL_INFERENCE]
    assert inference_nodes, "generator must produce at least one model_inference node"

    # emltriton elements come from model inference nodes and nothing else.
    assert set(emltritons_by_node) == {node.id for node in inference_nodes}, (
        "emltriton elements do not match the model_inference node set"
    )

    for node in inference_nodes:
        elements = emltritons_by_node[node.id]
        # Exactly one emltriton element per model inference node.
        assert len(elements) == 1, (
            "node '{0}' compiled to {1} emltriton elements".format(node.id, len(elements))
        )
        args = elements[0]["args"]
        # Args carry the node's configured model name and the Triton
        # model-repository and server paths used by LocalServer.
        assert args["model"] == node.parameters["modelName"]
        assert args["model-repo"] == expected_repo
        assert args["server-path"] == expected_server
