"""Property test for compiled order and branching (task 4.4).

**Feature: workflow-manager, Property 5: Compiled order respects the graph,
branches get tee/queue**

For all valid Workflow_Definitions, for every connection the elements of
the source node precede the elements of the target node in the rendered
pipeline order, and every node whose output feeds more than one downstream
node is followed by a ``tee`` element with a ``queue`` element at the head
of each branch — and nodes without fan-out get no ``tee``.

Notes on interpretation:

- "Rendered pipeline order" is the order LocalServer renders the document
  in: segments in document order, elements in segment order (the flattened
  element sequence of the launch string).
- Executor-level nodes (per the catalog mapping for the target
  architecture) contribute no pipeline elements; the stream flows through
  them. Ordering and fan-out are therefore checked over the pipeline
  stream: a node "feeds" the element-bearing nodes reachable through any
  run of element-less executor nodes.

**Validates: Requirements 6.1, 6.3**
"""

from __future__ import annotations

from typing import Dict, List, Set

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile

from .generators import graph_strategy

# --------------------------------------------------------------------------
# Expected stream structure, derived independently from graph + catalog
# --------------------------------------------------------------------------


def _element_bearing_node_ids(graph, arch: str) -> Set[str]:
    """Nodes whose catalog mapping for ``arch`` has GStreamer elements
    (executor-level nodes have none and are collapsed out of the stream)."""
    bearing = set()
    for node in graph.nodes:
        descriptor = get_node_type(node.type)
        assert descriptor is not None, node.type
        mapping = descriptor.mapping_for(arch)
        assert mapping is not None, (node.type, arch)
        if mapping.element_chain:
            bearing.add(node.id)
    return bearing


def _successors(graph) -> Dict[str, List[str]]:
    """Node id -> deduplicated downstream node ids per the connections."""
    successors: Dict[str, List[str]] = {node.id: [] for node in graph.nodes}
    for connection in graph.connections:
        source, target = connection.source.node, connection.target.node
        if target not in successors[source]:
            successors[source].append(target)
    return successors


def _stream_targets(
    node_id: str,
    successors: Dict[str, List[str]],
    bearing: Set[str],
) -> List[str]:
    """The element-bearing nodes ``node_id`` feeds in the pipeline stream,
    looking through element-less executor nodes."""
    targets: List[str] = []
    seen = {node_id}
    frontier = list(successors[node_id])
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current in bearing:
            targets.append(current)
        else:
            frontier.extend(successors[current])
    return targets


# --------------------------------------------------------------------------
# Document inspection helpers
# --------------------------------------------------------------------------


def _flat_indexes(document: CompiledPipelineDocument) -> Dict[str, List[int]]:
    """Node id -> positions of its elements in the flattened rendered order
    (segments in document order, elements in segment order)."""
    indexes: Dict[str, List[int]] = {}
    position = 0
    for segment in document.segments:
        for element in segment["elements"]:
            if element["nodeId"] is not None:
                indexes.setdefault(element["nodeId"], []).append(position)
            position += 1
    return indexes


def _chain_span(document: CompiledPipelineDocument, node_id: str):
    """(segment, index-of-last-chain-element) for ``node_id``'s elements.

    The chain must live in exactly one segment for the rendered order to
    be analyzable (each node maps to one contiguous element chain).
    """
    holding = [
        segment for segment in document.segments
        if any(element["nodeId"] == node_id for element in segment["elements"])
    ]
    assert len(holding) == 1, (
        "elements of node {0!r} appear in {1} segments; expected exactly "
        "one".format(node_id, len(holding))
    )
    segment = holding[0]
    last = max(
        position for position, element in enumerate(segment["elements"])
        if element["nodeId"] == node_id
    )
    return segment, last


# --------------------------------------------------------------------------
# The property
# --------------------------------------------------------------------------


@given(graph=graph_strategy(), arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_compiled_order_respects_graph_and_branches_get_tee_queue(graph, arch):
    """**Feature: workflow-manager, Property 5: Compiled order respects the
    graph, branches get tee/queue**

    **Validates: Requirements 6.1, 6.3**
    """
    document = compile(graph, arch)
    assert isinstance(document, CompiledPipelineDocument), (
        "compile() failed on a valid graph: {0}".format(document)
    )

    bearing = _element_bearing_node_ids(graph, arch)
    successors = _successors(graph)
    stream_targets = {
        node_id: _stream_targets(node_id, successors, bearing)
        for node_id in bearing
    }
    indexes = _flat_indexes(document)

    # ---- Order: source elements precede target elements (Requirement 6.1)

    # Directly connected element-bearing nodes, per the property statement.
    for connection in graph.connections:
        source, target = connection.source.node, connection.target.node
        if source in indexes and target in indexes:
            assert max(indexes[source]) < min(indexes[target]), (
                "connection {0!r}: elements of source {1!r} do not all "
                "precede elements of target {2!r} in rendered order".format(
                    connection.id, source, target
                )
            )

    # And through element-less executor nodes: the stream still flows
    # source -> target, so ordering must hold across the collapsed run.
    for source, targets in stream_targets.items():
        for target in targets:
            assert max(indexes[source]) < min(indexes[target]), (
                "stream: elements of {0!r} do not precede elements of "
                "downstream {1!r}".format(source, target)
            )

    # ---- Branching: fan-out gets tee + queue-headed branches; no fan-out,
    # ---- no tee (Requirement 6.3)

    for node_id in bearing:
        fan_out = len(stream_targets[node_id])
        segment, last = _chain_span(document, node_id)
        elements = segment["elements"]
        following = elements[last + 1] if last + 1 < len(elements) else None

        if fan_out > 1:
            # The node's chain is immediately followed by a synthetic tee.
            assert following is not None, (
                "fan-out node {0!r} ({1} downstream) has no element after "
                "its chain".format(node_id, fan_out)
            )
            assert following["factory"] == "tee" and following["nodeId"] is None, (
                "fan-out node {0!r} is followed by {1!r}, not a synthetic "
                "tee".format(node_id, following)
            )
            tee_name = following["args"]["name"]
            # One branch segment per downstream stream target...
            branches = [s for s in document.segments if s["from"] == tee_name]
            assert len(branches) == fan_out, (
                "tee {0!r} of node {1!r} has {2} branches; expected "
                "{3}".format(tee_name, node_id, len(branches), fan_out)
            )
            # ...each headed by a synthetic queue.
            for branch in branches:
                assert branch["elements"], (
                    "branch segment {0!r} of tee {1!r} is empty".format(
                        branch["name"], tee_name
                    )
                )
                head = branch["elements"][0]
                assert head["factory"] == "queue" and head["nodeId"] is None, (
                    "branch {0!r} of tee {1!r} starts with {2!r}, not a "
                    "synthetic queue".format(branch["name"], tee_name, head)
                )
        else:
            # No fan-out: the chain must not be followed by a tee.
            assert following is None or following["factory"] != "tee", (
                "node {0!r} has no fan-out but is followed by a tee".format(
                    node_id
                )
            )

    # Global consistency: every tee belongs to exactly one fan-out node
    # (no stray tees anywhere), and every queue is the head of a branch
    # segment hanging off a tee.
    all_elements = [
        element for segment in document.segments
        for element in segment["elements"]
    ]
    tee_count = sum(1 for element in all_elements if element["factory"] == "tee")
    fan_out_nodes = [n for n in bearing if len(stream_targets[n]) > 1]
    assert tee_count == len(fan_out_nodes), (
        "{0} tee elements for {1} fan-out nodes".format(
            tee_count, len(fan_out_nodes)
        )
    )
    for segment in document.segments:
        for position, element in enumerate(segment["elements"]):
            if element["factory"] == "queue":
                assert position == 0 and segment["from"] is not None, (
                    "queue at position {0} of segment {1!r} is not the head "
                    "of a tee branch".format(position, segment["name"])
                )
        if segment["from"] is not None:
            assert segment["elements"][0]["factory"] == "queue", (
                "branch segment {0!r} does not start with a queue".format(
                    segment["name"]
                )
            )
