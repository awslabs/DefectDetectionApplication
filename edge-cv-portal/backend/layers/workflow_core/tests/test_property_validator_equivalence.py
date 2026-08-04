# Feature: triggers-stage-and-unified-input, Property 6: Validator finding-set equivalence for zero-trigger and digital_input graphs
"""Property test P6 - validator finding-set equivalence (task 7.6).

Three properties over generated graphs, comparing the current
``validate()`` against a **pre-feature validator simulation** (the
"baseline model") implemented inline in this test:

- **Property A (zero-trigger equivalence, 4.5/4.6):** for generated
  Zero_Trigger_Workflows (valid and deliberately-invalid variants), the
  current finding set contains zero ``V7`` findings AND equals the
  baseline-model finding multiset (code/severity/node_id/connection_id
  tuples). For zero-trigger graphs the category sets contain no
  triggers, so the widened (INPUT | TRIGGER) and the pre-feature
  (INPUT-only) V1/V5 semantics coincide - asserted explicitly.
- **Property B (digital_input equivalence, 6.6/2.7):** for generated
  ``digital_input`` graphs (digital_input -> custom_python(EventSignal)
  -> capture, plus variants), the current finding set equals the
  baseline model where ``digital_input`` is treated as an input root
  (pre-relocation semantics: digital_input WAS ``CATEGORY_INPUT``).
- **Property C (non-suppression, 4.6):** restricting the current
  finding set to non-``V7`` codes never drops any V1-V6/W1 finding
  relative to the baseline model, even when offending connections make
  ``V7`` fire.

Baseline model construction: the simulation builds a ``typed_nodes``
map exactly the way ``validate()`` does (unknown types get an
``UNKNOWN_NODE_TYPE`` finding and are excluded), then runs every check
except V7 by calling the untouched check functions from
``workflow_core.validator.checks`` (``_check_v2``/``_check_v3``/
``_check_v4``/``_check_v6``/``_check_w1``) - the feature did not modify
them - while computing V1 and V5 with the **pre-feature
CATEGORY_INPUT-only semantics inline in this test**. The tiny inline
V1/V5 baseline deliberately mirrors the pre-feature code:

    pre-feature V1:  ``CATEGORY_INPUT in categories``
    pre-feature V5:  roots = nodes with ``category == CATEGORY_INPUT``

with the pre-relocation category mapping applied when requested
(``digital_input`` -> ``CATEGORY_INPUT``).

Self-contained Hypothesis strategies; no shared generator module is
used. >=100 examples per property.

**Validates: Requirements 2.7, 4.5, 4.6, 6.6**
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Set, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import NODE_CATALOG
from workflow_core.catalog.models import (
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_TRIGGER,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    NodeTypeDescriptor,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate
from workflow_core.validator.checks import (
    CODE_UNKNOWN_NODE_TYPE,
    CODE_V1_NO_INPUT_NODE,
    CODE_V1_NO_OUTPUT_NODE,
    CODE_V5_UNREACHABLE_NODE,
    CODE_V7_STAGE_ORDER,
    ValidationFinding,
    _check_v2,
    _check_v3,
    _check_v4,
    _check_v6,
    _check_w1,
)

_POS = Position(0.0, 0.0)

#: The type id whose pre-relocation category was CATEGORY_INPUT.
_DIGITAL_INPUT = "digital_input"


def _node(node_id: str, node_type: str, **parameters) -> Node:
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id: str, source_node: str, target_node: str,
          source_port: str = "out", target_port: str = "in") -> Connection:
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _key(finding: ValidationFinding) -> Tuple[str, str, str, str]:
    """The comparison tuple: code / severity / node id / connection id."""
    return (finding.code, finding.severity, finding.node_id, finding.connection_id)


def _typed_nodes(graph: WorkflowGraph):
    """Build the node-id -> descriptor map the same way ``validate()``
    does, returning ``(typed_nodes, unknown_type_findings)``."""
    descriptors: Dict[str, NodeTypeDescriptor] = {d.type_id: d for d in NODE_CATALOG}
    typed_nodes: Dict[str, NodeTypeDescriptor] = {}
    findings: List[ValidationFinding] = []
    for node in graph.nodes:
        descriptor = descriptors.get(node.type)
        if descriptor is None:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_UNKNOWN_NODE_TYPE,
                "Node '{0}' has unknown type '{1}'".format(node.id, node.type),
                node_id=node.id,
            ))
        else:
            typed_nodes[node.id] = descriptor
    return typed_nodes, findings


# ---------------------------------------------------------------------------
# Pre-feature validator simulation (the baseline model)
#
# Mirrors the pre-feature validator code: V2/V3/V4/V6/W1 are the current,
# feature-untouched check functions; V1/V5 are re-implemented inline with
# the pre-feature CATEGORY_INPUT-only root semantics (copied from the
# pre-feature `_check_v1`/`_check_v5` bodies); there is no V7.
# ---------------------------------------------------------------------------

def _prefeature_category(descriptor: NodeTypeDescriptor,
                         digital_as_input: bool) -> str:
    """The descriptor category under pre-relocation semantics: when
    ``digital_as_input`` is set, ``digital_input`` maps back to
    ``CATEGORY_INPUT`` (its category before this feature relocated it)."""
    if digital_as_input and descriptor.type_id == _DIGITAL_INPUT:
        return CATEGORY_INPUT
    return descriptor.category


def _baseline_v1(graph: WorkflowGraph, typed_nodes, digital_as_input: bool):
    """Pre-feature V1 (mirrors the pre-feature ``_check_v1``):
    ``CATEGORY_INPUT in categories`` - no trigger widening."""
    findings = []
    categories = {
        _prefeature_category(descriptor, digital_as_input)
        for descriptor in typed_nodes.values()
    }
    if CATEGORY_INPUT not in categories:
        findings.append(ValidationFinding(
            SEVERITY_ERROR,
            CODE_V1_NO_INPUT_NODE,
            "Workflow must contain at least one input node",
        ))
    if CATEGORY_OUTPUT not in categories:
        findings.append(ValidationFinding(
            SEVERITY_ERROR,
            CODE_V1_NO_OUTPUT_NODE,
            "Workflow must contain at least one output node",
        ))
    return findings


def _baseline_v5(graph: WorkflowGraph, typed_nodes, digital_as_input: bool):
    """Pre-feature V5 (mirrors the pre-feature ``_check_v5``): forward
    BFS whose roots are the ``CATEGORY_INPUT`` nodes only."""
    known = {node.id for node in graph.nodes}
    successors: Dict[str, List[str]] = {node_id: [] for node_id in known}
    for connection in graph.connections:
        source = connection.source.node
        target = connection.target.node
        if source in known and target in known:
            successors[source].append(target)

    roots = [
        node.id for node in graph.nodes
        if typed_nodes.get(node.id) is not None
        and _prefeature_category(typed_nodes[node.id], digital_as_input) == CATEGORY_INPUT
    ]

    visited = set(roots)
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for child in successors.get(current, []):
            if child not in visited:
                visited.add(child)
                frontier.append(child)

    findings = []
    for node in graph.nodes:
        if node.id not in visited:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V5_UNREACHABLE_NODE,
                "Node '{0}' is not reachable from any input node".format(node.id),
                node_id=node.id,
            ))
    return findings


def _baseline_findings(graph: WorkflowGraph,
                       digital_as_input: bool) -> List[ValidationFinding]:
    """The pre-feature validator simulation: every check except V7, with
    V1/V5 restricted to ``CATEGORY_INPUT`` roots (pre-relocation category
    mapping applied when ``digital_as_input``)."""
    typed_nodes, findings = _typed_nodes(graph)
    findings.extend(_baseline_v1(graph, typed_nodes, digital_as_input))
    findings.extend(_check_v2(graph, typed_nodes))
    findings.extend(_check_v3(graph))
    findings.extend(_check_v4(graph, typed_nodes))
    findings.extend(_baseline_v5(graph, typed_nodes, digital_as_input))
    findings.extend(_check_v6(graph, typed_nodes))
    findings.extend(_check_w1(graph, typed_nodes))
    return findings


# ---------------------------------------------------------------------------
# Strategies (self-contained)
# ---------------------------------------------------------------------------

#: The four retained (non-trigger) source node types with valid params.
_SOURCE_TYPES = {
    "folder_source": {"location": "/data/images"},
    "csi_camera_source": {},
    "icam_source": {},
    "aravis_camera_source": {"camera_id": "Aravis-Fake-GV01"},
}

#: Preprocessing node types (no required-without-default parameters,
#: except rotate/format_convert whose required params carry defaults).
_PREPROCESSING_TYPES = ("dewarp", "rotate", "crop", "format_convert")

#: Required parameters without a default, droppable for a V4 variant.
_DROPPABLE_REQUIRED = {
    "folder_source": "location",
    "aravis_camera_source": "camera_id",
    "capture": "output_path",
}


@st.composite
def zero_trigger_graphs(draw) -> WorkflowGraph:
    """Zero_Trigger_Workflows over pre-existing non-trigger node types
    (sources, preprocessing, capture) - both valid graphs and
    deliberately-invalid variants: missing required parameters (V4),
    dangling connections (V2), unreachable nodes (V5), unknown node
    types, and source/output omission (V1)."""
    nodes: List[Node] = []
    connections: List[Connection] = []

    # Source (optionally omitted entirely -> V1_NO_INPUT_NODE + V5 storm).
    include_source = draw(st.booleans())
    if include_source:
        source_type = draw(st.sampled_from(sorted(_SOURCE_TYPES)))
        source_params = dict(_SOURCE_TYPES[source_type])
        if source_type in _DROPPABLE_REQUIRED and draw(st.booleans()):
            source_params.pop(_DROPPABLE_REQUIRED[source_type], None)  # V4
        nodes.append(_node("src", source_type, **source_params))
    tail = "src"

    # 0..2 chained preprocessing steps (their connection into a missing
    # "src" is itself a dangling-connection variant).
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        node_type = draw(st.sampled_from(_PREPROCESSING_TYPES))
        node_id = "pre-{0}".format(index)
        nodes.append(_node(node_id, node_type))
        connections.append(_conn("c-pre-{0}".format(index), tail, node_id))
        tail = node_id

    # Capture output (optionally omitted -> V1_NO_OUTPUT_NODE; optionally
    # missing its required output_path -> V4).
    include_capture = draw(st.booleans())
    if include_capture:
        capture_params = {"output_path": "/out"}
        if draw(st.booleans()):
            capture_params.pop("output_path")  # V4
        nodes.append(_node("cap", "capture", **capture_params))
        connections.append(_conn("c-cap", tail, "cap"))

    # Unreachable node: a preprocessing island with no incoming edge (V5).
    if draw(st.booleans()):
        nodes.append(_node("island", draw(st.sampled_from(_PREPROCESSING_TYPES))))

    # Dangling connection to a node that does not exist (V2_UNKNOWN_NODE).
    if draw(st.booleans()):
        connections.append(_conn("c-ghost", tail, "ghost"))

    # Connection to an unknown port name (V2_UNKNOWN_PORT).
    if include_capture and draw(st.booleans()):
        connections.append(_conn("c-bogus", tail, "cap", target_port="bogus"))

    # A node whose type is not in the catalog (UNKNOWN_NODE_TYPE).
    if draw(st.booleans()):
        nodes.append(_node("mystery", "no_such_type"))

    # Keep at least one node so the graph is non-degenerate.
    if not nodes:
        nodes.append(_node("src", "csi_camera_source"))

    return WorkflowGraph(nodes=nodes, connections=connections)


@st.composite
def digital_input_params(draw) -> dict:
    """``digital_input`` parameter combinations - valid ones plus a
    missing-required-``pin`` variant (V4)."""
    params = {}
    if draw(st.integers(min_value=0, max_value=3)) > 0:  # usually present
        params["pin"] = draw(st.integers(min_value=0, max_value=255))
    if draw(st.booleans()):
        params["trigger_edge"] = draw(st.sampled_from(["rising", "falling", "both"]))
    if draw(st.booleans()):
        params["poll_interval_ms"] = draw(st.integers(min_value=10, max_value=60000))
    return params


@st.composite
def digital_input_graphs(draw) -> WorkflowGraph:
    """Legacy-style ``digital_input`` graphs: digital_input ->
    custom_python(EventSignal) -> capture, plus variants (an extra
    legacy source branch, a V4 missing-required variant, an unreachable
    island, a dangling connection). No connection ever targets the
    trigger node, matching pre-relocation legal usage."""
    nodes: List[Node] = []
    connections: List[Connection] = []

    nodes.append(_node("din", _DIGITAL_INPUT, **draw(digital_input_params())))

    # custom_python bridging EventSignal; its output port type varies
    # (a non-VideoFrames choice makes the edge into capture a V2
    # incompatible-types variant - identical in baseline and current).
    python_params = {
        "input_port_type": PORT_TYPE_EVENT_SIGNAL,
        "output_port_type": draw(st.sampled_from([
            PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_EVENT_SIGNAL, PORT_TYPE_INFERENCE_META,
        ])),
    }
    if draw(st.integers(min_value=0, max_value=3)) > 0:  # usually present
        python_params["code"] = "def handle(frame_bytes, metadata):\n    return frame_bytes, metadata"
    nodes.append(_node("py", "custom_python", **python_params))
    connections.append(_conn("c-din", "din", "py"))

    nodes.append(_node("cap", "capture", output_path="/out"))
    connections.append(_conn("c-cap", "py", "cap"))

    # Optional legacy source branch feeding the same capture (6.6:
    # legacy saved workflows referencing the existing source nodes).
    if draw(st.booleans()):
        source_type = draw(st.sampled_from(sorted(_SOURCE_TYPES)))
        nodes.append(_node("src", source_type, **_SOURCE_TYPES[source_type]))
        connections.append(_conn("c-src", "src", "cap"))

    # Unreachable island (V5) and dangling connection (V2) variants.
    if draw(st.booleans()):
        nodes.append(_node("island", draw(st.sampled_from(_PREPROCESSING_TYPES))))
    if draw(st.booleans()):
        connections.append(_conn("c-ghost", "din", "ghost"))

    return WorkflowGraph(nodes=nodes, connections=connections)


@st.composite
def digital_input_graphs_with_offenders(draw):
    """A digital_input graph plus 0..2 offending connections targeting
    the trigger node (each makes V7 fire in the current validator but
    is invisible to the pre-feature baseline's V7-less check set)."""
    graph = draw(digital_input_graphs())
    connections = list(graph.connections)
    candidate_sources = [node.id for node in graph.nodes if node.id != "din"]
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        source_node = draw(st.sampled_from(candidate_sources))
        target_port = draw(st.sampled_from(["in", "signal", "out"]))
        connections.append(_conn(
            "bad-{0}".format(index), source_node, "din", target_port=target_port,
        ))
    return WorkflowGraph(nodes=graph.nodes, connections=connections)


# ---------------------------------------------------------------------------
# Property A - zero-trigger equivalence (Requirements 4.5, 4.6)
# ---------------------------------------------------------------------------

# Feature: triggers-stage-and-unified-input, Property 6: Validator finding-set equivalence for zero-trigger and digital_input graphs
@settings(max_examples=100)
@given(graph=zero_trigger_graphs())
def test_zero_trigger_finding_set_equals_prefeature_baseline(graph):
    """**Feature: triggers-stage-and-unified-input, Property 6: Validator
    finding-set equivalence for zero-trigger and digital_input graphs** -
    Property A.

    For Zero_Trigger_Workflows the current validator emits zero ``V7``
    findings and its finding multiset (code/severity/node_id/
    connection_id) equals the pre-feature validator simulation with
    CATEGORY_INPUT-only V1/V5 roots. The category set contains no
    trigger, so both root semantics coincide - asserted explicitly.

    **Validates: Requirements 4.5, 4.6**
    """
    current = validate(graph)

    # Zero V7 findings on zero-trigger graphs.
    assert [f for f in current if f.code == CODE_V7_STAGE_ORDER] == []

    # Explicit semantic-coincidence assertion: no trigger category is
    # present, so the widened (INPUT | TRIGGER) root set equals the
    # pre-feature INPUT-only root set.
    typed_nodes, _ = _typed_nodes(graph)
    categories = {descriptor.category for descriptor in typed_nodes.values()}
    assert CATEGORY_TRIGGER not in categories
    widened_roots: Set[str] = {
        node_id for node_id, descriptor in typed_nodes.items()
        if descriptor.category in (CATEGORY_INPUT, CATEGORY_TRIGGER)
    }
    input_only_roots: Set[str] = {
        node_id for node_id, descriptor in typed_nodes.items()
        if descriptor.category == CATEGORY_INPUT
    }
    assert widened_roots == input_only_roots

    # Finding-multiset equivalence against the pre-feature simulation.
    baseline = _baseline_findings(graph, digital_as_input=False)
    assert Counter(_key(f) for f in current) == Counter(_key(f) for f in baseline)


# ---------------------------------------------------------------------------
# Property B - digital_input equivalence (Requirements 6.6, 2.7)
# ---------------------------------------------------------------------------

# Feature: triggers-stage-and-unified-input, Property 6: Validator finding-set equivalence for zero-trigger and digital_input graphs
@settings(max_examples=100)
@given(graph=digital_input_graphs())
def test_digital_input_finding_set_equals_prerelocation_baseline(graph):
    """**Feature: triggers-stage-and-unified-input, Property 6: Validator
    finding-set equivalence for zero-trigger and digital_input graphs** -
    Property B.

    For digital_input graphs (legacy usage: the trigger is a root, no
    connection targets it) the current ``validate()`` finding multiset
    equals the baseline model that treats ``digital_input`` as an input
    root (pre-relocation semantics: digital_input WAS CATEGORY_INPUT) -
    the widened V1/V5 semantics reproduce the pre-relocation outcome
    exactly.

    **Validates: Requirements 2.7, 6.6**
    """
    current = validate(graph)

    # Legacy digital_input graphs carry no offending connection, so V7
    # never fires and equivalence is over the complete finding sets.
    assert [f for f in current if f.code == CODE_V7_STAGE_ORDER] == []

    baseline = _baseline_findings(graph, digital_as_input=True)
    assert Counter(_key(f) for f in current) == Counter(_key(f) for f in baseline)


# ---------------------------------------------------------------------------
# Property C - non-suppression (Requirement 4.6)
# ---------------------------------------------------------------------------

# Feature: triggers-stage-and-unified-input, Property 6: Validator finding-set equivalence for zero-trigger and digital_input graphs
@settings(max_examples=100)
@given(graph=digital_input_graphs_with_offenders())
def test_non_v7_findings_never_drop_below_the_baseline(graph):
    """**Feature: triggers-stage-and-unified-input, Property 6: Validator
    finding-set equivalence for zero-trigger and digital_input graphs** -
    Property C.

    Even when offending connections make ``V7`` fire, restricting the
    current finding set to non-``V7`` codes never drops any V1-V6/W1
    finding relative to the pre-feature baseline model: the baseline
    finding multiset is contained in the current non-V7 multiset.

    **Validates: Requirement 4.6**
    """
    current = validate(graph)
    current_non_v7 = Counter(
        _key(f) for f in current if f.code != CODE_V7_STAGE_ORDER
    )
    baseline = Counter(
        _key(f) for f in _baseline_findings(graph, digital_as_input=True)
    )

    dropped = baseline - current_non_v7  # Counter subtraction: what is missing
    assert not dropped, (
        "V1-V6/W1 findings dropped relative to the pre-feature baseline: "
        "{0}".format(dict(dropped))
    )
