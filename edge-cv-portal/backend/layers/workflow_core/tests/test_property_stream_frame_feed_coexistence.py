"""Property test for frame-feed coexistence with the stream sources (task 3.3).

**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

*For any* graph:

- When it contains two or more Frame_Feed_Source_Nodes, in any combination
  of the four types, the V7 findings SHALL contain exactly one error per
  Frame_Feed_Source_Node, each naming every member.
- When it contains no stream node, the V7 findings SHALL equal the
  pre-feature findings.

**Validates: Requirements 2.4, 2.7**

The four Frame_Feed_Source_Node types after this feature are
``aravis_camera_source``, ``custom_python_source``, ``rtsp_camera_source``
and ``rtmp_stream_source`` (task 3.1). They all bind the device runtime's
single appsrc Frame_Feed, so two of them in one workflow is a conflict
regardless of which two.

Two oracles, neither sharing code with ``_check_v7_coexistence``:

- Clause 1 is judged structurally, from the graph itself: the expected
  finding set is derived by grouping the graph's frame-feed nodes by type
  with a plain loop, and the assertions are about *which* nodes are
  reported, how often, and whether each message names the full membership —
  not about the message's exact wording. The four types are additionally
  pinned to behave *interchangeably*, by rebuilding one two-feed graph under
  all sixteen ordered type combinations and comparing the reported triples.
- Clause 2 is judged against :func:`_pre_feature_coexistence_findings`, the
  pre-feature ``_check_v7_coexistence`` reconstructed verbatim over the
  pre-feature two-entry singleton table (the exact algorithm, reason strings
  and message formats the rule shipped with before this feature, taken from
  the committed source). Full :class:`ValidationFinding` equality is
  asserted, in order, so a changed message or a changed emission order for a
  stream-free graph fails — the preservation promise of Requirement 2.7.
  The same corpus additionally pins that none of the feature's four new
  codes (V11, V12, V13, W3) fires on a stream-free, analytics-free graph.

Scope decision recorded for the reader: ``_check_v7_coexistence`` keys on
``node.type`` directly, so a unified ``Input Source`` node carrying a stream
``source_kind`` is not a V7 member — exactly as a unified node carrying the
pre-existing ``aravis_camera`` kind has never been one (the unified type is
rewritten to its source type by the compiler's expansion pre-pass, after
validation). The stream kinds therefore introduce no asymmetry, which is
pinned below rather than changed here; the V7 mirror for unified nodes is
not part of this feature's design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import get_node_type
from workflow_core.catalog.nodes import SOURCE_KIND_TO_SOURCE_TYPE
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V7_COEXISTENCE_CONFLICT,
    CODE_V11_STREAM_URL,
    CODE_V12_CONTINUOUS_ACTIVATION,
    CODE_V13_ANALYTICS_CONFIG_INVALID,
    CODE_W3_ANALYTICS_NO_DETECTOR,
    FRAME_FEED_SOURCE_TYPES,
    SEVERITY_ERROR,
    ValidationFinding,
    validate,
)

from .generators import (
    STREAM_SOURCE_KINDS,
    STREAM_SOURCE_TYPES,
    graph_strategy,
    node_parameters_strategy,
    stream_graph_strategy,
)

_EXAMPLES = settings(max_examples=100)

#: The four Frame_Feed_Source_Node types, in a fixed order for reporting.
#: Pinned as a literal and cross-checked against the validator's own set, so
#: a type added to (or dropped from) the rule without updating this property
#: fails loudly instead of silently narrowing the corpus.
FRAME_FEED_TYPES: Tuple[str, ...] = (
    "aravis_camera_source",
    "custom_python_source",
    "rtsp_camera_source",
    "rtmp_stream_source",
)

#: The two frame-feed types that existed before this feature — the corpus of
#: clause 2 (a graph with neither stream type in it).
PRE_FEATURE_FRAME_FEED_TYPES: Tuple[str, ...] = (
    "aravis_camera_source",
    "custom_python_source",
)

#: The feature's new finding codes: none of them may fire on a stream-free,
#: analytics-free graph (Requirement 2.7).
_NEW_FINDING_CODES = (
    CODE_V11_STREAM_URL,
    CODE_V12_CONTINUOUS_ACTIVATION,
    CODE_V13_ANALYTICS_CONFIG_INVALID,
    CODE_W3_ANALYTICS_NO_DETECTOR,
)

_CAPTURE_DESCRIPTOR = get_node_type("capture")

#: Non-frame-feed input types, free to share a graph with a frame-feed node.
_OTHER_INPUT_TYPES = ("csi_camera_source", "icam_source", "folder_source",
                      "digital_input")


def test_the_four_frame_feed_types_are_exactly_the_validator_s_set():
    """The corpus really covers "any combination of the four types"."""
    assert set(FRAME_FEED_TYPES) == set(FRAME_FEED_SOURCE_TYPES), (
        "the validator's FRAME_FEED_SOURCE_TYPES is {0}, this property covers "
        "{1}".format(sorted(FRAME_FEED_SOURCE_TYPES), sorted(FRAME_FEED_TYPES)))
    assert set(STREAM_SOURCE_TYPES) <= set(FRAME_FEED_TYPES)
    assert set(PRE_FEATURE_FRAME_FEED_TYPES) == (
        set(FRAME_FEED_TYPES) - set(STREAM_SOURCE_TYPES))


# ---------------------------------------------------------------------------
# Graph assembly helpers (plain node/connection lists, as the sibling
# frame-feed coexistence property test does)
# ---------------------------------------------------------------------------

def _wire_source(draw, nodes, connections, type_id, node_id, tag):
    """Append a source node of ``type_id`` feeding its own capture sink.

    Keeps the graph structurally valid — an input and an output node are
    present, every node is reachable, and every connection is
    type-compatible — so the frame-feed conflict is the only deliberate
    defect.
    """
    sink_id = "{0}-cap".format(tag)
    nodes.append(Node(
        id=node_id,
        type=type_id,
        position=Position(x=float(len(nodes)), y=0.0),
        parameters=draw(node_parameters_strategy(get_node_type(type_id))),
    ))
    nodes.append(Node(
        id=sink_id,
        type="capture",
        position=Position(x=float(len(nodes)), y=100.0),
        parameters=draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR)),
    ))
    connections.append(Connection(
        id="{0}-conn".format(tag),
        source=PortEndpoint(node=node_id, port="out"),
        target=PortEndpoint(node=sink_id, port="in"),
    ))
    return node_id


def _frame_feed_nodes_by_type(graph: WorkflowGraph) -> Dict[str, List[str]]:
    """The graph's frame-feed nodes grouped by type — the structural oracle.

    A plain loop over the graph, keyed on the literal type tuple above; it
    shares nothing with the validator beyond the type names themselves.
    """
    by_type: Dict[str, List[str]] = {}
    for node in graph.nodes:
        if node.type in FRAME_FEED_TYPES:
            by_type.setdefault(node.type, []).append(node.id)
    return by_type


def _v7_findings(graph: WorkflowGraph) -> List[ValidationFinding]:
    return [finding for finding in validate(graph)
            if finding.code == CODE_V7_COEXISTENCE_CONFLICT]


def _assert_one_finding_per_member_naming_everyone(graph: WorkflowGraph):
    """Clause 1, asserted structurally against ``_frame_feed_nodes_by_type``.

    Returns the findings so a caller can make further assertions.
    """
    by_type = _frame_feed_nodes_by_type(graph)
    members = sorted(node_id for ids in by_type.values() for node_id in ids)
    assert len(members) >= 2, (
        "the strategy must produce two or more frame-feed nodes, got "
        "{0}".format(members))

    found = _v7_findings(graph)

    # Exactly one error finding per Frame_Feed_Source_Node, and none on any
    # other node. The emitted order is the sorted membership — the shape the
    # frontend inline-check mirror (task 11.1) has to reproduce.
    assert [finding.node_id for finding in found] == members, (
        "expected exactly one V7 coexistence finding per frame-feed node "
        "{0}, got {1}".format(members, [f.node_id for f in found]))
    assert all(finding.severity == SEVERITY_ERROR for finding in found), (
        "every coexistence finding must be an error: {0}".format(found))

    # Each finding names every member of the conflicting set.
    for finding in found:
        for member in members:
            assert "'{0}'".format(member) in finding.message, (
                "the finding for '{0}' does not name member '{1}': "
                "{2!r}".format(finding.node_id, member, finding.message))
    return found


# ---------------------------------------------------------------------------
# Clause 1: two or more frame-feed nodes, in any combination
# ---------------------------------------------------------------------------

@st.composite
def frame_feed_conflict_graph_strategy(draw, pool: Sequence[str] = FRAME_FEED_TYPES,
                                       max_appended: int = 4):
    """Structurally valid graphs carrying two or more Frame_Feed_Source_Nodes.

    A drawn boolean starts from a random valid catalog graph (which may
    itself contain at most one ``aravis_camera_source`` — the shared
    generators respect the singleton rule and never emit a stream or
    custom-python source), then appends 1..``max_appended`` frame-feed nodes
    whose types are drawn from ``pool`` with repetition, so the corpus spans
    same-type multiples, mixed pairs, and up to all four types at once. Each
    appended node feeds its own ``capture`` sink.
    """
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    present = sum(len(ids) for ids in
                  _frame_feed_nodes_by_type(WorkflowGraph(
                      nodes=nodes, connections=connections)).values())

    appended = draw(st.lists(
        st.sampled_from(tuple(pool)),
        min_size=max(1, 2 - present),
        max_size=max_appended,
    ))
    for index, type_id in enumerate(appended):
        tag = "ff-{0}-{1}".format(index, type_id)
        _wire_source(draw, nodes, connections, type_id,
                     "{0}-src".format(tag), tag)

    return WorkflowGraph(nodes=nodes, connections=connections)


@_EXAMPLES
@given(graph=frame_feed_conflict_graph_strategy())
def test_two_or_more_frame_feed_nodes_are_each_reported_with_full_membership(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    **Validates: Requirements 2.4**
    """
    _assert_one_finding_per_member_naming_everyone(graph)


@_EXAMPLES
@given(graph=frame_feed_conflict_graph_strategy())
def test_the_frame_feed_conflict_is_the_corpus_s_only_error(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    The premise of clause 1: the generated graphs are otherwise valid, so
    every error-severity finding they draw is a coexistence finding. Without
    this, "exactly one error per Frame_Feed_Source_Node" could be satisfied
    by a graph that is broken in other ways too.

    **Validates: Requirements 2.4**
    """
    errors = [finding for finding in validate(graph)
              if finding.severity == SEVERITY_ERROR]
    unexpected = [finding for finding in errors
                  if finding.code != CODE_V7_COEXISTENCE_CONFLICT]
    assert not unexpected, (
        "the conflict corpus drew error findings other than the coexistence "
        "conflict: {0}".format(unexpected))


@_EXAMPLES
@given(graph=frame_feed_conflict_graph_strategy(pool=STREAM_SOURCE_TYPES))
def test_two_or_more_stream_nodes_conflict_exactly_like_any_other_frame_feed(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    The all-stream corner of "any combination of the four types": a document
    whose frame-feed nodes are all ``rtsp_camera_source`` /
    ``rtmp_stream_source`` (plus, half the time, the base graph's single
    Aravis node).

    **Validates: Requirements 2.4**
    """
    by_type = _frame_feed_nodes_by_type(graph)
    assert set(by_type) & set(STREAM_SOURCE_TYPES), (
        "the strategy must produce stream nodes")
    _assert_one_finding_per_member_naming_everyone(graph)


@_EXAMPLES
@given(stream_graph=st.integers(min_value=2, max_value=3).flatmap(
    lambda count: stream_graph_strategy(stream_nodes=count, unified=False)))
def test_multi_feed_stream_documents_report_every_feed(stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    The same clause over the shared generator's realistic multi-feed stream
    documents — the graphs task 2.4 (Property 1) deliberately left to this
    property once task 3.1 made them V7-invalid. Each feed drives its own
    capture sink and carries a valid credential-free Stream_URL, so the
    coexistence conflict is the document's only defect.

    **Validates: Requirements 2.4**
    """
    feeds = sorted(feed.node_id for feed in stream_graph.feeds)
    assert len(feeds) >= 2
    found = _assert_one_finding_per_member_naming_everyone(stream_graph.graph)
    assert [finding.node_id for finding in found] == feeds


# ---------------------------------------------------------------------------
# Clause 1, exhaustively over the sixteen ordered type combinations
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class _PairCase:
    """Parameters for a two-feed graph, per slot and per frame-feed type.

    ``graph_for(first, second)`` builds the same two-node, two-sink graph
    with the feeds spelled as ``first`` and ``second``, so the sixteen
    ordered type combinations are compared on an otherwise identical graph.
    """

    parameters: Dict[str, Dict[str, dict]]
    sink_parameters: Dict[str, dict]

    def graph_for(self, first: str, second: str) -> WorkflowGraph:
        nodes = []
        connections = []
        for slot, type_id in (("a", first), ("b", second)):
            node_id = "feed-{0}".format(slot)
            sink_id = "sink-{0}".format(slot)
            nodes.append(Node(
                id=node_id,
                type=type_id,
                position=Position(x=0.0, y=0.0),
                parameters=dict(self.parameters[slot][type_id]),
            ))
            nodes.append(Node(
                id=sink_id,
                type="capture",
                position=Position(x=0.0, y=100.0),
                parameters=dict(self.sink_parameters[slot]),
            ))
            connections.append(Connection(
                id="conn-{0}".format(slot),
                source=PortEndpoint(node=node_id, port="out"),
                target=PortEndpoint(node=sink_id, port="in"),
            ))
        return WorkflowGraph(nodes=nodes, connections=connections)


@st.composite
def pair_case_strategy(draw):
    parameters = {
        slot: {
            type_id: draw(node_parameters_strategy(get_node_type(type_id)))
            for type_id in FRAME_FEED_TYPES
        }
        for slot in ("a", "b")
    }
    sink_parameters = {
        slot: draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR))
        for slot in ("a", "b")
    }
    return _PairCase(parameters=parameters, sink_parameters=sink_parameters)


@_EXAMPLES
@given(case=pair_case_strategy())
def test_every_combination_of_the_four_types_conflicts_identically(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    "In any combination of the four types", exhaustively: all sixteen
    ordered pairs of the four Frame_Feed_Source_Node types are built on the
    same two-feed graph, and every one of them must report the same
    (severity, code, node id) triples — one error per feed, naming both.
    The stream types are therefore Frame_Feed_Source_Nodes in exactly the
    sense the Aravis and custom-python sources are.

    **Validates: Requirements 2.4**
    """
    expected_triples = [
        (SEVERITY_ERROR, CODE_V7_COEXISTENCE_CONFLICT, "feed-a"),
        (SEVERITY_ERROR, CODE_V7_COEXISTENCE_CONFLICT, "feed-b"),
    ]
    for first in FRAME_FEED_TYPES:
        for second in FRAME_FEED_TYPES:
            graph = case.graph_for(first, second)
            found = _assert_one_finding_per_member_naming_everyone(graph)
            triples = [(f.severity, f.code, f.node_id) for f in found]
            assert triples == expected_triples, (
                "('{0}', '{1}') reported {2}".format(first, second, triples))

            # The message form follows the membership, not the pair's
            # spelling: a same-type pair names its type and count, a mixed
            # pair names the frame-feed group. Each form carries the reason
            # its table entry declares.
            for finding in found:
                if first == second:
                    assert "'{0}'".format(first) in finding.message, finding
                    assert "2 nodes of type" in finding.message, finding
                    expected_reason = (
                        "one Aravis camera source per workflow"
                        if first == "aravis_camera_source"
                        else "one frame-feed source per workflow")
                else:
                    assert "frame-feed source nodes" in finding.message, finding
                    expected_reason = "one frame-feed source per workflow"
                assert expected_reason in finding.message, (
                    "('{0}', '{1}') message {2!r} does not state {3!r}".format(
                        first, second, finding.message, expected_reason))


# ---------------------------------------------------------------------------
# The boundary of clause 1: a single frame-feed node is not a conflict
# ---------------------------------------------------------------------------

@st.composite
def single_frame_feed_graph_strategy(draw, type_id=None):
    """A valid graph with exactly one Frame_Feed_Source_Node.

    No base graph is drawn (the shared generator may contribute an Aravis
    node, which would be a second member); instead 0..2 non-frame-feed
    inputs are appended with their own sinks, so "other inputs present" is
    still covered.
    """
    type_id = draw(st.sampled_from(FRAME_FEED_TYPES)) if type_id is None else type_id
    nodes: List[Node] = []
    connections: List[Connection] = []
    _wire_source(draw, nodes, connections, type_id, "the-feed", "feed")

    others = draw(st.lists(st.sampled_from(_OTHER_INPUT_TYPES),
                           min_size=0, max_size=2))
    for index, other in enumerate(others):
        tag = "other-{0}".format(index)
        if other == "digital_input":
            # A trigger input: EventSignal out, no VideoFrames sink to wire,
            # so it is added stand-alone (W1/W5-class warnings only).
            nodes.append(Node(
                id="{0}-src".format(tag),
                type=other,
                position=Position(x=float(len(nodes)), y=200.0),
                parameters=draw(node_parameters_strategy(get_node_type(other))),
            ))
            continue
        _wire_source(draw, nodes, connections, other,
                     "{0}-src".format(tag), tag)

    return WorkflowGraph(nodes=nodes, connections=connections)


@_EXAMPLES
@given(graph=single_frame_feed_graph_strategy())
def test_a_single_frame_feed_node_is_never_a_coexistence_conflict(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    The clause's boundary — "two or more" — from below: one frame-feed node
    of any of the four types, alongside any number of non-frame-feed inputs,
    draws no coexistence finding. This is what keeps a single stream camera
    a usable workflow input (Requirement 2.4 read as an exact condition).

    **Validates: Requirements 2.4**
    """
    by_type = _frame_feed_nodes_by_type(graph)
    assert sum(len(ids) for ids in by_type.values()) == 1
    assert _v7_findings(graph) == [], (
        "a single frame-feed node drew a coexistence finding: {0}".format(
            _v7_findings(graph)))


# ---------------------------------------------------------------------------
# The unified spelling is out of V7's scope — and symmetrically so
# ---------------------------------------------------------------------------

@st.composite
def unified_source_kind_graph_strategy(draw):
    """Two ``unified_input`` nodes carrying one drawn source kind, each with
    its own sink. Returns ``(kind, graph)``."""
    kind = draw(st.sampled_from(tuple(SOURCE_KIND_TO_SOURCE_TYPE)))
    nodes: List[Node] = []
    connections: List[Connection] = []
    descriptor = get_node_type("unified_input")
    for slot in ("a", "b"):
        node_id = "unified-{0}".format(slot)
        sink_id = "unified-sink-{0}".format(slot)
        nodes.append(Node(
            id=node_id,
            type="unified_input",
            position=Position(x=0.0, y=0.0),
            parameters=draw(node_parameters_strategy(
                descriptor, {"source_kind": kind})),
        ))
        nodes.append(Node(
            id=sink_id,
            type="capture",
            position=Position(x=0.0, y=100.0),
            parameters=draw(node_parameters_strategy(_CAPTURE_DESCRIPTOR)),
        ))
        connections.append(Connection(
            id="unified-conn-{0}".format(slot),
            source=PortEndpoint(node=node_id, port="out"),
            target=PortEndpoint(node=sink_id, port="in"),
        ))
    return kind, WorkflowGraph(nodes=nodes, connections=connections)


@_EXAMPLES
@given(case=unified_source_kind_graph_strategy())
def test_unified_stream_kinds_are_outside_v7_exactly_like_every_other_kind(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    ``_check_v7_coexistence`` keys on ``node.type``, and the unified
    ``Input Source`` type is rewritten to its source type only later, by the
    compiler's expansion pre-pass. Two unified nodes therefore draw no
    coexistence finding for *any* source kind — the pre-existing
    ``aravis_camera`` kind included. The stream kinds added by Requirement
    1.6 introduce no asymmetry here; this pins that parity rather than
    changing it.

    **Validates: Requirements 2.4, 2.7**
    """
    kind, graph = case
    assert set(STREAM_SOURCE_KINDS) <= set(SOURCE_KIND_TO_SOURCE_TYPE), (
        "the stream kinds must be part of the drawn pool")
    assert _v7_findings(graph) == [], (
        "two unified '{0}' nodes drew a coexistence finding: {1}".format(
            kind, _v7_findings(graph)))


# ---------------------------------------------------------------------------
# Clause 2: a graph with no stream node keeps its pre-feature findings
# ---------------------------------------------------------------------------

#: The pre-feature ``COEXISTENCE_SINGLETON_TYPES`` table: the two frame-feed
#: entries with the exact reason strings the rule shipped with before this
#: feature (portal-build-fleet-and-workflow-gates Requirement 8.2 and
#: custom-python-source Requirement 8.1).
_PRE_FEATURE_SINGLETON_TYPES: Dict[str, str] = {
    "aravis_camera_source": (
        "the single-frame appsrc feed supports exactly one Aravis "
        "camera source per workflow"
    ),
    "custom_python_source": (
        "the single-frame appsrc feed serves exactly one frame-feed "
        "source per workflow"
    ),
}

#: The pre-feature ``FRAME_FEED_SOURCE_TYPES``: the mixed rule fired when
#: BOTH of these were present (``FRAME_FEED_SOURCE_TYPES <= set(by_type)``).
_PRE_FEATURE_FRAME_FEED_SET = frozenset(PRE_FEATURE_FRAME_FEED_TYPES)


def _pre_feature_coexistence_findings(graph: WorkflowGraph) -> List[ValidationFinding]:
    """The pre-feature ``_check_v7_coexistence``, reconstructed verbatim.

    Algorithm, message formats and emission order exactly as committed
    before this feature: the mixed frame-feed findings first (fired only
    when *both* pre-feature frame-feed types are present), then the
    singleton findings for every other singleton type, in sorted type order.
    """
    findings: List[ValidationFinding] = []
    by_type: Dict[str, List[str]] = {}
    for node in graph.nodes:
        if node.type in _PRE_FEATURE_SINGLETON_TYPES:
            by_type.setdefault(node.type, []).append(node.id)

    mixed_frame_feed = _PRE_FEATURE_FRAME_FEED_SET <= set(by_type)
    if mixed_frame_feed:
        member_ids = sorted(
            node_id
            for node_type in _PRE_FEATURE_FRAME_FEED_SET
            for node_id in by_type[node_type]
        )
        members = ", ".join("'{0}'".format(i) for i in member_ids)
        for node_id in member_ids:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V7_COEXISTENCE_CONFLICT,
                "Node '{0}': frame-feed source nodes ({1}) cannot coexist "
                "in one workflow: the runtime serves one frame-feed source "
                "per workflow".format(node_id, members),
                node_id=node_id,
            ))

    for node_type, node_ids in sorted(by_type.items()):
        if mixed_frame_feed and node_type in _PRE_FEATURE_FRAME_FEED_SET:
            continue
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
def stream_free_graph_strategy(draw, max_per_type: int = 3):
    """Graphs containing no stream node: a random valid catalog graph (half
    the time) plus 0..``max_per_type`` nodes of each pre-feature frame-feed
    type, each feeding its own sink.

    The corpus therefore spans "no frame-feed node", "one of each", "several
    of one type" and "both types present" — every branch the pre-feature
    rule had.
    """
    if draw(st.booleans()):
        base = draw(graph_strategy())
        nodes = list(base.nodes)
        connections = list(base.connections)
    else:
        nodes = []
        connections = []

    for type_id in PRE_FEATURE_FRAME_FEED_TYPES:
        for index in range(draw(st.integers(min_value=0, max_value=max_per_type))):
            tag = "pre-{0}-{1}".format(type_id, index)
            _wire_source(draw, nodes, connections, type_id,
                         "{0}-src".format(tag), tag)

    # V1: a stream-free graph with no appended source still needs an input
    # and an output node; the base graph provides both, so add a minimal
    # pair when no base was drawn and nothing was appended.
    if not nodes:
        _wire_source(draw, nodes, connections, "folder_source",
                     "only-src", "only")

    return WorkflowGraph(nodes=nodes, connections=connections)


@_EXAMPLES
@given(graph=stream_free_graph_strategy())
def test_stream_free_graphs_keep_their_pre_feature_coexistence_findings(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    Clause 2, asserted as full :class:`ValidationFinding` equality in
    emission order: for a graph with no stream node, generalizing V7 to four
    frame-feed types changed neither which nodes are reported, nor the
    messages, nor the order. With two members, "two or more distinct types
    present" is the same test as "both types present".

    **Validates: Requirements 2.7**
    """
    assert not any(node.type in STREAM_SOURCE_TYPES for node in graph.nodes), (
        "the strategy must not produce stream nodes")

    actual = _v7_findings(graph)
    expected = _pre_feature_coexistence_findings(graph)
    assert actual == expected, (
        "V7 coexistence findings for a stream-free graph differ from the "
        "pre-feature rule's output.\nactual:   {0}\nexpected: {1}".format(
            actual, expected))


@_EXAMPLES
@given(graph=stream_free_graph_strategy())
def test_stream_free_graphs_draw_none_of_the_new_finding_codes(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 4: Frame-feed coexistence**

    The other half of Requirement 2.7, read literally: a definition with no
    Stream_Camera_Source_Node and no Scene_Analytics_Node reports exactly
    the findings it reported before this feature — so none of the four codes
    the feature adds may appear.

    **Validates: Requirements 2.7**
    """
    new_findings = [finding for finding in validate(graph)
                    if finding.code in _NEW_FINDING_CODES]
    assert not new_findings, (
        "a stream-free, analytics-free graph drew new-code findings: "
        "{0}".format(new_findings))
