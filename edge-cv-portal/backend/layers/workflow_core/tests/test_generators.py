"""Smoke tests for the shared hypothesis generators (task 2.3).

Assert the invariants the generators promise to downstream property tests
(tasks 2.4, 2.5, 3.4, 4.3-4.8):

- ``graph_strategy`` graphs pass ``validate()`` with no error-severity
  findings and serialize/parse cleanly,
- each defect-seeding combinator produces graphs whose error findings are
  exactly the seeded (and implied) defect set,
- schema-corrupting document mutators always produce documents that
  ``parse()`` rejects with a descriptive error.

**Validates: Requirements 3.4, 4.6, 6.6**
"""

import json

import pytest
from hypothesis import given, strategies as st

from workflow_core.serializer import (
    Node,
    Position,
    WorkflowGraph,
    graph_to_document,
    parse,
    serialize,
)
from workflow_core.validator import SEVERITY_ERROR, validate

from tests.generators import (
    ALL_DEFECT_CLASSES,
    corrupted_document_strategy,
    graph_strategy,
    seeded_graph_strategy,
    single_node_graph_strategy,
)


def _error_triples(findings):
    return {
        (finding.code, finding.node_id, finding.connection_id)
        for finding in findings
        if finding.severity == SEVERITY_ERROR
    }


def _expected_triples(seeded):
    return {
        (expected.code, expected.node_id, expected.connection_id)
        for expected in seeded.expected
    }


# ---------------------------------------------------------------------------
# graph_strategy: valid graphs
# ---------------------------------------------------------------------------

@given(graph=graph_strategy())
def test_graph_strategy_yields_validator_valid_graphs(graph):
    """Valid graphs contain no error-severity findings and round-trip
    through the canonical serializer."""
    findings = validate(graph)
    errors = _error_triples(findings)
    assert errors == set(), "graph_strategy produced error findings: {}".format(errors)

    document = serialize(graph)  # must not raise (unique, non-empty ids)
    result = parse(document)
    assert result.ok, "serialized valid graph failed to parse: {}".format(result.error)


@given(graph=single_node_graph_strategy())
def test_single_node_graph_strategy_is_well_formed(graph):
    """Single-node graphs (serializer edge case) serialize and parse."""
    assert len(graph.nodes) == 1
    assert graph.connections == []
    result = parse(serialize(graph))
    assert result.ok, result.error


# ---------------------------------------------------------------------------
# Defect-seeding combinators: exactness per defect class and in combination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("defect", ALL_DEFECT_CLASSES)
@given(data=st.data())
def test_each_defect_class_seeds_exactly_its_findings(defect, data):
    """Each combinator alone yields exactly the intended defect class
    (plus its declared implied findings), and nothing else."""
    seeded = data.draw(seeded_graph_strategy(defect_classes=[defect]))
    assert seeded.defects == frozenset({defect})
    assert seeded.expected, "a seeded graph must expect at least one finding"
    assert _error_triples(validate(seeded.graph)) == _expected_triples(seeded)


@given(seeded=seeded_graph_strategy())
def test_combined_defect_seeding_matches_expected_findings_exactly(seeded):
    """Random defect-class combinations still produce exactly the declared
    expected error findings."""
    assert seeded.defects  # nonempty by construction
    assert _error_triples(validate(seeded.graph)) == _expected_triples(seeded)


# ---------------------------------------------------------------------------
# Schema-corrupting document mutators
# ---------------------------------------------------------------------------

@given(document=corrupted_document_strategy())
def test_corrupted_documents_are_rejected_descriptively(document):
    """Every corrupted document is rejected: no graph, and a descriptive
    error with a code and message."""
    result = parse(document)
    assert not result.ok
    assert result.graph is None
    assert result.error.code
    assert result.error.message


# ---------------------------------------------------------------------------
# Deterministic examples
# ---------------------------------------------------------------------------

def test_defect_class_constants_are_distinct():
    assert len(ALL_DEFECT_CLASSES) == 6
    assert len(set(ALL_DEFECT_CLASSES)) == 6


def test_dropping_nodes_key_is_a_schema_violation():
    """A hand-built corruption representative: dropping a required
    top-level key must be rejected as a schema violation."""
    graph = WorkflowGraph(
        nodes=[
            Node(id="n1", type="icam_source", position=Position(0, 0), parameters={}),
        ],
        connections=[],
    )
    document = graph_to_document(graph)
    del document["nodes"]
    result = parse(json.dumps(document))
    assert not result.ok
    assert result.error.code == "SCHEMA_VIOLATION"
