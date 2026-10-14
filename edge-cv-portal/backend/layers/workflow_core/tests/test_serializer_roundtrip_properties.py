"""Property test for the serialization round trip (task 2.4).

**Feature: workflow-manager, Property 1: Serialization round trip**

For all valid Workflow_Definition graphs, ``parse(serialize(g))`` produces
a graph equivalent to ``g``, and ``serialize(parse(serialize(g)))``
produces JSON byte-identical to ``serialize(g)``.

**Validates: Requirements 3.1, 3.2, 3.4**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.serializer import parse, serialize

from .generators import graph_strategy, single_node_graph_strategy

# Round-trip quantifies over all well-formed graphs: validator-valid random
# graphs from the catalog plus the single-node serializer edge case.
workflow_graphs = st.one_of(graph_strategy(), single_node_graph_strategy())


@given(graph=workflow_graphs)
def test_serialization_round_trip(graph):
    """**Feature: workflow-manager, Property 1: Serialization round trip**

    **Validates: Requirements 3.1, 3.2, 3.4**
    """
    document = serialize(graph)

    # parse(serialize(g)) succeeds and produces a graph equivalent to g
    # (Requirements 3.1, 3.2, 3.4).
    result = parse(document)
    assert result.ok, "parse rejected a serialized valid graph: {}".format(result.error)
    assert result.graph is not None
    # A current-version document must not report any migration.
    assert result.migrations is None, (
        "unexpected migrations for a current-version document: {!r}".format(result.migrations)
    )
    assert result.graph.is_equivalent_to(graph), (
        "parsed graph is not equivalent to the original"
    )
    # Equivalence is symmetric; check the other direction too.
    assert graph.is_equivalent_to(result.graph), (
        "original graph is not equivalent to the parsed graph"
    )

    # serialize(parse(serialize(g))) is byte-identical to serialize(g)
    # (Requirement 3.4: canonical serialization).
    reserialized = serialize(result.graph)
    assert reserialized == document, (
        "re-serialized document is not byte-identical to the original serialization"
    )
