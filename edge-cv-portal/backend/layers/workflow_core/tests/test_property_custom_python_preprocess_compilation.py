"""Property test for the compiled emlpython element of Custom Python
preprocessing nodes (custom-python-frames task 1.4).

**Feature: custom-python-frames, Property 12: Compiled emlpython element
per Custom Python preprocessing node**

For any valid workflow graph embedding ``custom_python_preprocess`` nodes
with arbitrary node ids, compiling for any architecture yields exactly one
``emlpython`` element per such node, tagged with that node's id and
carrying ``handler-path`` equal to ``python/{nodeId}/handler.py``.

**Validates: Requirements 2.3**
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

# Arbitrary node ids: non-empty, exercising unicode, whitespace, and
# punctuation (the compiled handler path embeds the id verbatim).
_NODE_ID = st.text(min_size=1, max_size=12)

# Required `code` parameter values: any non-empty string satisfies the
# descriptor's min_length constraint (content is never interpreted by
# the compiler).
_CODE = st.text(min_size=1, max_size=40)

# format_convert output formats (its required parameter's enum values),
# used for interleaved non-Custom-Python preprocessing nodes.
_FORMATS = ("RGB", "RGBA", "BGR", "GRAY8", "NV12", "I420")


@st.composite
def _preprocess_workflow(draw):
    """A valid workflow embedding 1..3 ``custom_python_preprocess`` nodes.

    Structure: icam_source -> [custom_python_preprocess (optionally
    followed by an interleaved format_convert)]+ -> capture. The linear
    VideoFrames chain is always validator-valid (input and output nodes
    present, compatible ports, acyclic, all reachable, required
    parameters set).

    Returns ``(graph, preprocess_node_ids)``.
    """
    preprocess_count = draw(st.integers(min_value=1, max_value=3))
    # One drawn boolean per preprocess node: follow it with an
    # interleaved format_convert node (varying the surrounding graph).
    interleaved = [draw(st.booleans()) for _ in range(preprocess_count)]

    total_nodes = 2 + preprocess_count + sum(interleaved)
    node_ids = draw(
        st.lists(_NODE_ID, min_size=total_nodes, max_size=total_nodes, unique=True)
    )
    id_iter = iter(node_ids)

    def next_id():
        return next(id_iter)

    def make_node(type_id, parameters):
        return Node(
            id=next_id(),
            type=type_id,
            position=Position(x=0.0, y=0.0),
            parameters=parameters,
        )

    nodes = [make_node("icam_source", {})]
    preprocess_ids = []
    for follow_with_convert in interleaved:
        parameters = {"code": draw(_CODE)}
        if draw(st.booleans()):
            parameters["requirements"] = draw(st.text(max_size=20))
        node = make_node("custom_python_preprocess", parameters)
        preprocess_ids.append(node.id)
        nodes.append(node)
        if follow_with_convert:
            nodes.append(
                make_node("format_convert", {"format": draw(st.sampled_from(_FORMATS))})
            )
    nodes.append(make_node("capture", {"output_path": "/aws_dda/captures"}))

    # Linear wiring: every node's single output feeds the next node's
    # single input (icam_source's port is "out", every consumer's is
    # "in").
    connections = [
        Connection(
            id="c{0}".format(index),
            source=PortEndpoint(node=nodes[index].id, port="out"),
            target=PortEndpoint(node=nodes[index + 1].id, port="in"),
        )
        for index in range(len(nodes) - 1)
    ]

    return WorkflowGraph(nodes=nodes, connections=connections), preprocess_ids


@given(
    workflow=_preprocess_workflow(),
    target_arch=st.sampled_from(ARCHITECTURES),
)
def test_compiled_emlpython_element_per_preprocess_node(workflow, target_arch):
    """**Feature: custom-python-frames, Property 12: Compiled emlpython
    element per Custom Python preprocessing node**

    **Validates: Requirements 2.3**
    """
    graph, preprocess_ids = workflow

    document = compile(graph, target_arch)
    assert isinstance(document, CompiledPipelineDocument), (
        "compile() failed for a valid graph on arch {!r}: {}".format(
            target_arch, document
        )
    )

    emlpython_elements = [
        element
        for segment in document.segments
        for element in segment["elements"]
        if element["factory"] == "emlpython"
    ]

    # Exactly one emlpython element per custom_python_preprocess node,
    # tagged with that node's id — no extras, none missing.
    expected = Counter(preprocess_ids)
    actual = Counter(element["nodeId"] for element in emlpython_elements)
    assert actual == expected, (
        "emlpython elements do not match the preprocess node set on arch "
        "{!r}: missing={}, extra_or_duplicate={}".format(
            target_arch, sorted(expected - actual), sorted(actual - expected)
        )
    )

    # Each element carries the packaging-layout handler path derived from
    # its node id (Requirement 2.3).
    for element in emlpython_elements:
        expected_path = "python/{0}/handler.py".format(element["nodeId"])
        assert element["args"]["handler-path"] == expected_path, (
            "emlpython element for node {!r} carries handler-path {!r}, "
            "expected {!r}".format(
                element["nodeId"], element["args"].get("handler-path"), expected_path
            )
        )
