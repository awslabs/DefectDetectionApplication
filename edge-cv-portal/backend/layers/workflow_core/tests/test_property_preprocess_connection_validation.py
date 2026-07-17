"""Property test for validator acceptance of preprocessing node connections.

**Feature: custom-python-frames, Property 1: Validator acceptance of
preprocessing node connections**

For any workflow graph wiring a source node's output port into a
`custom_python_preprocess` node's input port, the Workflow_Validator
accepts the connection exactly when
`are_port_types_compatible(source_type, VideoFrames)` holds under the
catalog's declared coercion rules (VideoFrames exactly, and
InferenceMeta via the declared coercion), and otherwise reports a port
type compatibility error identifying that connection.

**Validates: Requirements 2.1, 2.2**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    NODE_CATALOG,
    PORT_TYPES,
    are_port_types_compatible,
    get_node_type,
)
from workflow_core.catalog.models import PORT_TYPE_VIDEO_FRAMES
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import CODE_V2_INCOMPATIBLE_TYPES, validate

from .generators import node_parameters_strategy

#: Every catalog node type that declares at least one output port is a
#: candidate source for wiring into the preprocessing node's input.
_SOURCE_DESCRIPTORS = [d for d in NODE_CATALOG if d.outputs]


@st.composite
def preprocess_wiring_strategy(draw):
    """A two-node graph: a random source node type's drawn output port
    wired into a `custom_python_preprocess` node's `in` port.

    Returns ``(graph, connection_id, effective_source_port_type)`` where
    the effective type accounts for per-instance `output_port_type`
    overrides (custom_python), mirroring the validator's port resolution.
    """
    descriptor = draw(st.sampled_from(_SOURCE_DESCRIPTORS))

    # Per-instance port typing: when the source type declares an
    # output_port_type parameter, draw the override explicitly so every
    # port type is exercised as a source.
    forced = None
    parameter_names = {p.name for p in descriptor.parameters}
    if "output_port_type" in parameter_names:
        forced = {"output_port_type": draw(st.sampled_from(PORT_TYPES))}

    source = Node(
        id="src",
        type=descriptor.type_id,
        position=Position(x=0.0, y=0.0),
        parameters=draw(node_parameters_strategy(descriptor, forced)),
    )

    output_port = draw(st.sampled_from(descriptor.outputs))
    if forced is not None:
        effective_type = forced["output_port_type"]
    else:
        effective_type = output_port.port_type

    preprocess_descriptor = get_node_type("custom_python_preprocess")
    preprocess = Node(
        id="pre",
        type="custom_python_preprocess",
        position=Position(x=100.0, y=0.0),
        parameters=draw(node_parameters_strategy(preprocess_descriptor)),
    )

    connection = Connection(
        id="c1",
        source=PortEndpoint(node=source.id, port=output_port.name),
        target=PortEndpoint(node=preprocess.id, port="in"),
    )

    graph = WorkflowGraph(nodes=[source, preprocess], connections=[connection])
    return graph, connection.id, effective_type


@given(wiring=preprocess_wiring_strategy())
def test_validator_accepts_preprocess_input_exactly_for_video_frames(wiring):
    """**Feature: custom-python-frames, Property 1: Validator acceptance
    of preprocessing node connections**

    validate() reports no port type compatibility finding for the
    connection exactly when are_port_types_compatible(source_port_type,
    VideoFrames) holds under the catalog's declared coercion rules
    (VideoFrames exactly, and InferenceMeta via the declared coercion),
    and reports a port type compatibility finding identifying the
    connection for every other source port type.

    **Validates: Requirements 2.1, 2.2**
    """
    graph, connection_id, source_port_type = wiring

    findings = validate(graph)
    compatibility_findings = [
        finding for finding in findings
        if finding.code == CODE_V2_INCOMPATIBLE_TYPES
        and finding.connection_id == connection_id
    ]

    if are_port_types_compatible(source_port_type, PORT_TYPE_VIDEO_FRAMES):
        assert compatibility_findings == [], (
            "{0} -> custom_python_preprocess input is compatible with "
            "VideoFrames under the declared coercion rules and must be "
            "accepted, but validate() reported: {1}".format(
                source_port_type,
                [f.to_dict() for f in compatibility_findings],
            )
        )
    else:
        assert compatibility_findings, (
            "{0} output wired into the custom_python_preprocess input "
            "(connection '{1}') must yield a port type compatibility "
            "finding identifying the connection, but validate() reported "
            "none".format(source_port_type, connection_id)
        )
