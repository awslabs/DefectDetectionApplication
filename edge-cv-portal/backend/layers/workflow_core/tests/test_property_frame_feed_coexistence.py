"""Property tests for the frame-feed coexistence rule (tasks 2.2, 2.3).

**Feature: custom-python-source, Property 15: Frame-feed coexistence conflicts are reported per offending node with full membership**

For any valid workflow graph containing two or more
``custom_python_source`` nodes, or at least one ``custom_python_source``
together with at least one ``aravis_camera_source``, the
Workflow_Validator reports exactly one ``V7_COEXISTENCE_CONFLICT`` error
finding per member of the conflicting set, and every finding's message
names every member of that set.

**Feature: custom-python-source, Property 16: The Aravis singleton finding is preserved**

For any workflow graph containing ``aravis_camera_source`` nodes and no
``custom_python_source``, the validator's coexistence finding set is
identical — same finding codes, messages, and offending nodes — to the
pre-feature coexistence rule's output. The oracle is the pre-feature
singleton rule reconstructed by construction in this module (the exact
algorithm and reason string the rule shipped with before this feature),
mirroring the ``test_property_aravis_free_*_identity`` preservation
pattern.
"""

from __future__ import annotations

from typing import Dict, List

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import get_node_type
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V7_COEXISTENCE_CONFLICT,
    SEVERITY_ERROR,
    ValidationFinding,
    validate,
)

from .generators import graph_strategy, node_parameters_strategy

ARAVIS_TYPE_ID = "aravis_camera_source"
SOURCE_TYPE_ID = "custom_python_source"

_ARAVIS_DESCRIPTOR = get_node_type(ARAVIS_TYPE_ID)
_SOURCE_DESCRIPTOR = get_node_type(SOURCE_TYPE_ID)
_CAPTURE_DESCRIPTOR = get_node_type("capture")


def _append_wired_source(draw, nodes, connections, type_id, descriptor, tag, index):
    """Append one frame-feed source node feeding its own capture sink,
    keeping the graph structurally valid (input + output present, all
    nodes reachable, connections type-compatible)."""
    source_id = "{0}-src-{1}".format(tag, index)
    sink_id = "{0}-cap-{1}".format(tag, index)
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
        id="{0}-conn-{1}".format(tag, index),
        source=PortEndpoint(node=source_id, port="out"),
        target=PortEndpoint(node=sink_id, port="in"),
    ))


@st.composite
def frame_feed_conflict_graph_strategy(draw):
    """Valid workflow graphs guaranteed to carry a frame-feed conflict:
    two or more ``custom_python_source`` nodes, or at least one
    ``custom_python_source`` with at least one ``aravis_camera_source``.

    A drawn boolean starts from a random valid catalog graph (which may
    itself contain at most one Aravis node — the shared generators
    respect the singleton rule); appended sources each feed their own
    ``capture`` sink so the graph stays structurally valid. The only
    deliberate defect is the frame-feed coexistence conflict itself.
    """
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    aravis_count = sum(1 for node in nodes if node.type == ARAVIS_TYPE_ID)

    source_count = draw(st.integers(min_value=1, max_value=3))
    add_aravis = draw(st.booleans())
    if source_count == 1 and aravis_count == 0:
        # A single custom_python_source alone is conflict-free; force
        # the mixed branch so the property's precondition always holds.
        add_aravis = True

    for index in range(source_count):
        _append_wired_source(
            draw, nodes, connections,
            SOURCE_TYPE_ID, _SOURCE_DESCRIPTOR, "python", index,
        )
    if add_aravis:
        _append_wired_source(
            draw, nodes, connections,
            ARAVIS_TYPE_ID, _ARAVIS_DESCRIPTOR, "aravis", 0,
        )

    return WorkflowGraph(nodes=nodes, connections=connections)


@given(graph=frame_feed_conflict_graph_strategy())
def test_frame_feed_conflicts_reported_per_node_with_full_membership(graph):
    """**Feature: custom-python-source, Property 15: Frame-feed coexistence conflicts are reported per offending node with full membership**

    **Validates: Requirements 8.1, 8.2**
    """
    source_ids = sorted(n.id for n in graph.nodes if n.type == SOURCE_TYPE_ID)
    aravis_ids = sorted(n.id for n in graph.nodes if n.type == ARAVIS_TYPE_ID)
    assert source_ids, "strategy must produce at least one custom_python_source"
    assert len(source_ids) >= 2 or aravis_ids, (
        "strategy must produce a frame-feed conflict"
    )

    if source_ids and aravis_ids:
        # Mixed: the conflicting set is the union across both types.
        expected_members = sorted(source_ids + aravis_ids)
    else:
        # Same-type-only: two or more custom_python_source nodes.
        expected_members = source_ids

    found = [
        finding for finding in validate(graph)
        if finding.code == CODE_V7_COEXISTENCE_CONFLICT
    ]

    # Exactly one error finding per member of the conflicting set.
    assert sorted(f.node_id for f in found) == expected_members, (
        "expected exactly one V7_COEXISTENCE_CONFLICT finding per "
        "conflicting frame-feed node {0}, got: {1}".format(
            expected_members, found)
    )
    assert all(f.severity == SEVERITY_ERROR for f in found)

    # Every finding's message names every member of the conflicting set.
    for finding in found:
        for member in expected_members:
            assert "'{0}'".format(member) in finding.message, (
                "finding for node '{0}' does not name conflicting member "
                "'{1}': {2!r}".format(finding.node_id, member, finding.message)
            )


# --------------------------------------------------------------------------
# Property 16: oracle by construction — the pre-feature singleton rule
# --------------------------------------------------------------------------

#: The pre-feature COEXISTENCE_SINGLETON_TYPES table: exactly the one
#: Aravis entry with its exact reason string, as shipped before this
#: feature (portal-build-fleet-and-workflow-gates Requirement 8.2).
_PRE_FEATURE_SINGLETON_TYPES: Dict[str, str] = {
    "aravis_camera_source": (
        "the single-frame appsrc feed supports exactly one Aravis "
        "camera source per workflow"
    ),
}


def _pre_feature_coexistence_findings(graph: WorkflowGraph) -> List[ValidationFinding]:
    """The pre-feature ``_check_v7_coexistence`` reconstructed verbatim
    over the pre-feature singleton table — the preservation oracle."""
    findings = []
    by_type: Dict[str, List[str]] = {}
    for node in graph.nodes:
        if node.type in _PRE_FEATURE_SINGLETON_TYPES:
            by_type.setdefault(node.type, []).append(node.id)

    for node_type, node_ids in sorted(by_type.items()):
        if len(node_ids) < 2:
            continue
        reason = _PRE_FEATURE_SINGLETON_TYPES[node_type]
        members = ", ".join("'{0}'".format(i) for i in sorted(node_ids))
        for node_id in sorted(node_ids):
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V7_COEXISTENCE_CONFLICT,
                "Node '{0}': {1} nodes of type '{2}' cannot coexist in "
                "one workflow ({3}): {4}".format(
                    node_id, len(node_ids), node_type, members, reason
                ),
                node_id=node_id,
            ))
    return findings


@st.composite
def aravis_only_graph_strategy(draw):
    """Workflow graphs with 1..3 ``aravis_camera_source`` nodes and no
    ``custom_python_source`` (the base generator never emits one)."""
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    for index in range(draw(st.integers(min_value=1, max_value=3))):
        _append_wired_source(
            draw, nodes, connections,
            ARAVIS_TYPE_ID, _ARAVIS_DESCRIPTOR, "aravis-extra", index,
        )

    return WorkflowGraph(nodes=nodes, connections=connections)


@given(graph=aravis_only_graph_strategy())
def test_aravis_singleton_finding_is_preserved(graph):
    """**Feature: custom-python-source, Property 16: The Aravis singleton finding is preserved**

    **Validates: Requirements 8.3**
    """
    assert not any(n.type == SOURCE_TYPE_ID for n in graph.nodes), (
        "strategy must not produce custom_python_source nodes"
    )
    aravis_nodes = [n for n in graph.nodes if n.type == ARAVIS_TYPE_ID]
    assert aravis_nodes, "strategy must produce at least one Aravis node"

    actual = [
        finding for finding in validate(graph)
        if finding.code == CODE_V7_COEXISTENCE_CONFLICT
    ]
    expected = _pre_feature_coexistence_findings(graph)

    # Identical finding set: same codes, messages, and offending nodes,
    # in the same order the singleton rule has always emitted them.
    assert actual == expected, (
        "coexistence findings for an Aravis-only graph differ from the "
        "pre-feature rule's output.\nactual:   {0}\nexpected: {1}".format(
            actual, expected)
    )
