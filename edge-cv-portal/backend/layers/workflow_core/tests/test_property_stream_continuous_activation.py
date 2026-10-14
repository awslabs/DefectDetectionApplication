"""Property test for the continuous activation rule, V12 (task 3.4).

**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation rule**

*For any* graph:

- A V12 finding SHALL be reported on a stream node exactly when the node is
  in ``continuous`` mode and either has an activation connection or shares
  the graph with a subscription trigger.
- V9 SHALL report no finding on continuous stream nodes.
- V9's findings on every other node SHALL be unchanged.

**Validates: Requirements 2.3, 2.5**

Two oracles, neither sharing code with the checks under test:

- Clause 1 is judged structurally, from the graph itself:
  :func:`_expected_v12_node_ids` re-derives "a continuous
  Stream_Camera_Source_Node that has an activation connection or shares the
  graph with a Subscription_Trigger_Node" from Requirement 2.3's sentence
  with plain loops over ``graph.nodes`` / ``graph.connections``, a literal
  stream-kind map, and the literal ``continuous`` default — none of
  ``_check_v12_continuous_activation``'s helpers
  (``_effective_node_type``, ``_parameter_value``,
  ``_is_continuous_stream_node``) and none of the validator's constants.
  Every literal it relies on is cross-checked against the catalog and the
  validator by the module-level sanity tests below, so a renamed type, a
  changed default or a new stream kind fails loudly instead of silently
  narrowing the corpus.
- Clauses 2 and 3 are judged against :func:`_pre_feature_v9_findings`, the
  pre-feature ``_check_v9`` reconstructed verbatim from the committed source
  (the rule exactly as it shipped before this feature, with no stream-node
  skip). Full :class:`ValidationFinding` equality is asserted, in emission
  order, against that reconstruction minus the continuous stream nodes — so
  clause 2 (nothing on a continuous stream node) and clause 3 (everything
  else byte-identical, messages and order included) are pinned by one
  comparison, and either half failing is a test failure.

Corpus: graphs carrying 1..3 stream feeds, each spelled either as an
``rtsp_camera_source`` / ``rtmp_stream_source`` node or as a
``unified_input`` node with the matching stream ``source_kind``
(Requirement 1.6, evaluated through the effective source type), each
``processing_mode`` spelled as omitted / explicit ``continuous`` / explicit
``on_trigger`` / explicit null, with or without an activation edge, next to
0..2 subscription triggers, an optional ``digital_input`` (an activation
source that is *not* a Subscription_Trigger_Node, so the two halves of
Requirement 2.3's condition can be exercised in isolation), 0..2
non-frame-feed inputs of their own, and — half the time — a random valid
catalog graph underneath, which contributes further ``CATEGORY_INPUT`` nodes
for clause 3 to preserve.

Decisions recorded for the reader:

- An **omitted** and an **explicitly null** ``processing_mode`` both count
  as ``continuous``: the parameter's declared default is ``continuous``, and
  the validator reads a node's effective value. Both spellings are in the
  corpus and the oracle treats them alike.
- A ``processing_mode`` value that is neither ``continuous`` nor
  ``on_trigger`` (wrong case, padded, a non-string) is **not** continuous,
  so V12 stays silent on it; V4 owns that value under its own code. This is
  the literal reading of Requirement 2.3 ("in ``continuous`` mode") and is
  pinned by its own property below rather than left implicit.
- V12's activation clause counts *any* connection into the node's
  ``activation`` port, whatever it comes from; the corpus therefore wires
  activation edges from both subscription triggers and a ``digital_input``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import CATEGORY_INPUT, NODE_CATALOG, get_node_type
from workflow_core.catalog.nodes import SOURCE_KIND_TO_SOURCE_TYPE
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.stream_url import SCHEMES_BY_NODE_TYPE
from workflow_core.validator import (
    CODE_V12_CONTINUOUS_ACTIVATION,
    SEVERITY_ERROR,
    STREAM_SOURCE_TYPES,
    ValidationFinding,
    validate,
)
from workflow_core.validator.checks import (
    ACTIVATION_PORT,
    CODE_V9_MIXED_ACTIVATION_MODEL,
    PROCESSING_MODE_CONTINUOUS,
    PROCESSING_MODE_PARAMETER,
    SUBSCRIPTION_TRIGGER_TYPES,
)

from .generators import (
    STREAM_SOURCE_KINDS,
    graph_strategy,
    node_parameters_strategy,
    stream_graph_strategy,
    stream_url_strategy,
)

_EXAMPLES = settings(max_examples=100)

# ---------------------------------------------------------------------------
# The rule's vocabulary, spelled out independently of the validator
# ---------------------------------------------------------------------------

#: The two Stream_Camera_Source_Node types (Requirement 1.1).
_STREAM_TYPES: Tuple[str, ...] = ("rtsp_camera_source", "rtmp_stream_source")

#: The unified ``Input Source`` kinds that expand to them (Requirement 1.6).
_STREAM_KIND_TO_TYPE: Dict[str, str] = {
    "rtsp_camera": "rtsp_camera_source",
    "rtmp_stream": "rtmp_stream_source",
}

#: The node types that are Subscription_Trigger_Nodes (Requirement 2.3).
_TRIGGER_TYPES: Tuple[str, ...] = ("mqtt_subscribe", "opcua_subscribe")

_UNIFIED_INPUT = "unified_input"
_SOURCE_KIND = "source_kind"
_ACTIVATION = "activation"
_MODE = "processing_mode"
_CONTINUOUS = "continuous"
_ON_TRIGGER = "on_trigger"

#: An activation source that is NOT a Subscription_Trigger_Node, so an
#: activation edge can be present with no trigger in the graph (and V9
#: stays silent, since ``digital_input`` alone does not engage it).
_DIGITAL_INPUT = "digital_input"

#: Non-frame-feed input types, free to share a graph with a stream feed
#: without engaging the frame-feed coexistence rule.
_OTHER_INPUT_TYPES: Tuple[str, ...] = (
    "csi_camera_source", "icam_source", "folder_source",
)

#: Every ``processing_mode`` spelling a stream node can carry, as
#: ``(tag, value)``; ``"omitted"`` removes the key entirely.
_OMITTED = object()
_MODE_SPELLINGS: Tuple[Tuple[str, Any], ...] = (
    ("omitted", _OMITTED),
    ("continuous", _CONTINUOUS),
    ("null", None),
    ("on_trigger", _ON_TRIGGER),
)

#: Values that are neither of the declared enum members: V4's business, and
#: not ``continuous``, so V12 stays silent (see the module docstring).
_JUNK_MODES: Tuple[Any, ...] = (
    "Continuous", "CONTINUOUS", " continuous", "continuous ", "on_Trigger",
    "", "streaming", 7, True, ["continuous"],
)

_DESCRIPTORS: Dict[str, Any] = {d.type_id: d for d in NODE_CATALOG}


# ---------------------------------------------------------------------------
# Sanity: the literals above really are the rule's vocabulary
# ---------------------------------------------------------------------------

def test_the_stream_types_are_exactly_the_validator_s_set():
    assert set(_STREAM_TYPES) == set(STREAM_SOURCE_TYPES), (
        "the validator checks {0}, this property covers {1}".format(
            sorted(STREAM_SOURCE_TYPES), sorted(_STREAM_TYPES)))


def test_the_stream_kinds_are_exactly_the_catalog_s_stream_kinds():
    catalog_stream_kinds = {
        kind: source_type
        for kind, source_type in SOURCE_KIND_TO_SOURCE_TYPE.items()
        if source_type in STREAM_SOURCE_TYPES
    }
    assert _STREAM_KIND_TO_TYPE == catalog_stream_kinds, (
        "the catalog maps {0}, this property covers {1}".format(
            catalog_stream_kinds, _STREAM_KIND_TO_TYPE))
    assert set(STREAM_SOURCE_KINDS) == set(_STREAM_KIND_TO_TYPE)


def test_the_subscription_trigger_types_are_exactly_the_validator_s_set():
    assert set(_TRIGGER_TYPES) == set(SUBSCRIPTION_TRIGGER_TYPES), (
        "V9/V12 engage on {0}, this property covers {1}".format(
            sorted(SUBSCRIPTION_TRIGGER_TYPES), sorted(_TRIGGER_TYPES)))
    assert _DIGITAL_INPUT not in SUBSCRIPTION_TRIGGER_TYPES, (
        "the corpus relies on digital_input NOT being a subscription trigger")


def test_the_continuous_default_is_the_catalog_default():
    """The oracle's "omitted means continuous" is the catalog's own default.

    Asserted on both stream source types and on the unified ``Input
    Source`` node, whose parameter union carries the same descriptor.
    """
    for type_id in _STREAM_TYPES + (_UNIFIED_INPUT,):
        parameters = {p.name: p for p in _DESCRIPTORS[type_id].parameters}
        mode = parameters[_MODE]
        assert mode.default == _CONTINUOUS, (
            "'{0}'.{1} defaults to {2!r}".format(type_id, _MODE, mode.default))
        assert set(mode.constraints["values"]) == {_CONTINUOUS, _ON_TRIGGER}
        assert set(value for _tag, value in _MODE_SPELLINGS
                   if isinstance(value, str)) == {_CONTINUOUS, _ON_TRIGGER}


def test_the_literal_names_match_the_validator_s_constants():
    assert _ACTIVATION == ACTIVATION_PORT
    assert _MODE == PROCESSING_MODE_PARAMETER
    assert _CONTINUOUS == PROCESSING_MODE_CONTINUOUS
    for type_id in _STREAM_TYPES + (_UNIFIED_INPUT,):
        ports = {port.name for port in _DESCRIPTORS[type_id].inputs}
        assert _ACTIVATION in ports, type_id


def test_no_junk_mode_is_a_declared_enum_member():
    assert not ({value for value in _JUNK_MODES if isinstance(value, str)}
                & {_CONTINUOUS, _ON_TRIGGER})


# ---------------------------------------------------------------------------
# Oracle for clause 1: Requirement 2.3's sentence, re-derived
# ---------------------------------------------------------------------------

def _effective_stream_type(node: Node) -> Optional[str]:
    """The Stream_Camera_Source_Node type ``node`` is, or ``None``.

    A ``unified_input`` node is read through its ``source_kind``
    (Requirement 1.6): the source type the compiler expands it into.
    """
    if node.type in _STREAM_TYPES:
        return node.type
    if node.type == _UNIFIED_INPUT:
        kind = node.parameters.get(_SOURCE_KIND)
        if isinstance(kind, str):
            return _STREAM_KIND_TO_TYPE.get(kind)
    return None


def _is_continuous_stream(node: Node) -> bool:
    """Whether ``node`` is a Stream_Camera_Source_Node in continuous mode.

    An omitted or explicitly cleared ``processing_mode`` is the declared
    default, ``continuous``; any other value is continuous only when it is
    exactly ``"continuous"``.
    """
    if _effective_stream_type(node) is None:
        return False
    if _MODE not in node.parameters:
        return True
    value = node.parameters[_MODE]
    if value is None:
        return True
    return value == _CONTINUOUS


def _activation_targets(graph: WorkflowGraph) -> set:
    return {connection.target.node for connection in graph.connections
            if connection.target.port == _ACTIVATION}


def _subscription_trigger_ids(graph: WorkflowGraph) -> List[str]:
    return [node.id for node in graph.nodes if node.type in _TRIGGER_TYPES]


def _expected_v12_node_ids(graph: WorkflowGraph) -> List[str]:
    """The nodes Requirement 2.3 says must be reported, in graph order.

    Graph order is the order the rule emits in (one pass over the nodes),
    and it is asserted as such: the finding *sequence* is the contract the
    frontend inline-check mirror (task 11.1) has to reproduce, not just the
    set.
    """
    activation_targets = _activation_targets(graph)
    has_trigger = bool(_subscription_trigger_ids(graph))
    return [
        node.id for node in graph.nodes
        if _is_continuous_stream(node)
        and (node.id in activation_targets or has_trigger)
    ]


# ---------------------------------------------------------------------------
# Oracle for clauses 2 and 3: the pre-feature V9, reconstructed verbatim
# ---------------------------------------------------------------------------

def _pre_feature_v9_findings(graph: WorkflowGraph) -> List[ValidationFinding]:
    """``_check_v9`` exactly as committed before this feature.

    Algorithm, message format and emission order taken from the committed
    source: zero findings when no subscription trigger is present,
    otherwise one error per ``CATEGORY_INPUT`` node with no connection into
    its ``activation`` port — with no stream-node exemption, which is
    precisely what clause 2 changes and clause 3 promises not to change for
    anyone else.
    """
    has_subscription_trigger = any(
        node.type in _TRIGGER_TYPES for node in graph.nodes
    )
    if not has_subscription_trigger:
        return []

    activation_connected = _activation_targets(graph)

    findings: List[ValidationFinding] = []
    for node in graph.nodes:
        descriptor = _DESCRIPTORS.get(node.type)
        if descriptor is None or descriptor.category != CATEGORY_INPUT:
            continue
        if node.id not in activation_connected:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V9_MIXED_ACTIVATION_MODEL,
                "Input node '{0}' has no trigger connected to its 'activation' "
                "port: a workflow with subscription triggers must drive every "
                "input from a trigger".format(node.id),
                node_id=node.id,
            ))
    return findings


def _findings_with_code(graph: WorkflowGraph, code: str) -> List[ValidationFinding]:
    return [finding for finding in validate(graph) if finding.code == code]


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FeedSpec:
    """One generated stream feed: its id, spelling and mode spelling."""

    node_id: str
    node_type: str
    source_type: str
    mode_tag: str
    activation_wired: bool


@dataclass(frozen=True, eq=False)
class _Case:
    graph: WorkflowGraph
    feeds: Tuple[_FeedSpec, ...]
    trigger_ids: Tuple[str, ...]

    @property
    def continuous_feeds(self) -> Tuple[_FeedSpec, ...]:
        return tuple(feed for feed in self.feeds
                     if feed.mode_tag in ("omitted", "continuous", "null"))


def _apply_mode(parameters: Dict[str, Any], mode_tag: str, value: Any) -> Dict[str, Any]:
    """Spell ``processing_mode`` on a generated parameter map.

    ``node_parameters_strategy`` may or may not emit the (optional)
    parameter, so the spelling is applied afterwards: removed for
    ``omitted``, set verbatim otherwise (an explicit ``None`` included).
    """
    parameters = dict(parameters)
    if value is _OMITTED:
        parameters.pop(_MODE, None)
    else:
        parameters[_MODE] = value
    return parameters


@st.composite
def continuous_activation_graph_strategy(
    draw,
    feed_count: Optional[int] = None,
    trigger_count: Optional[int] = None,
    mode_spellings: Sequence[Tuple[str, Any]] = _MODE_SPELLINGS,
    activation: str = "draw",
    with_digital_input: Optional[bool] = None,
    with_base_graph: bool = True,
    max_other_inputs: int = 2,
    unified: Optional[bool] = None,
):
    """Graphs exercising every branch of Requirement 2.3.

    ``activation`` is ``"draw"`` (per feed), ``"always"`` or ``"never"``;
    an activation edge is only wired when the graph has an EventSignal
    source to wire it from (a subscription trigger or the optional
    ``digital_input``), so the corpus never fabricates a type-invalid edge.
    ``unified`` fixes the feed spelling (``None`` draws it per feed).
    """
    nodes: List[Node] = []
    connections: List[Connection] = []

    if with_base_graph and draw(st.booleans()):
        base = draw(graph_strategy())
        nodes.extend(base.nodes)
        connections.extend(base.connections)

    # --- Subscription triggers and the non-subscription activation source
    count = draw(st.integers(min_value=0, max_value=2)) \
        if trigger_count is None else trigger_count
    trigger_ids: List[str] = []
    for index in range(count):
        trigger_type = draw(st.sampled_from(_TRIGGER_TYPES))
        trigger_id = "trig-{0}".format(index)
        trigger_ids.append(trigger_id)
        # mqtt_subscribe needs a connection target (V8); force the
        # Greengrass path so the corpus carries no unrelated error.
        forced = {"greengrass": True} if trigger_type == "mqtt_subscribe" else None
        nodes.append(Node(
            id=trigger_id,
            type=trigger_type,
            position=Position(x=0.0, y=float(index)),
            parameters=draw(node_parameters_strategy(
                _DESCRIPTORS[trigger_type], forced)),
        ))

    include_digital = draw(st.booleans()) if with_digital_input is None \
        else with_digital_input
    event_sources = list(trigger_ids)
    if include_digital:
        nodes.append(Node(
            id="dig-0",
            type=_DIGITAL_INPUT,
            position=Position(x=0.0, y=-50.0),
            parameters=draw(node_parameters_strategy(
                _DESCRIPTORS[_DIGITAL_INPUT])),
        ))
        event_sources.append("dig-0")

    def wire_activation(target_id: str, tag: str) -> bool:
        if activation == "never" or not event_sources:
            return False
        if activation == "draw" and not draw(st.booleans()):
            return False
        connections.append(Connection(
            id="act-{0}".format(tag),
            source=PortEndpoint(node=draw(st.sampled_from(event_sources)),
                                port="out"),
            target=PortEndpoint(node=target_id, port=_ACTIVATION),
        ))
        return True

    def add_capture_sink(source_id: str, tag: str) -> None:
        sink_id = "cap-{0}".format(tag)
        nodes.append(Node(
            id=sink_id,
            type="capture",
            position=Position(x=200.0, y=0.0),
            parameters=draw(node_parameters_strategy(_DESCRIPTORS["capture"])),
        ))
        connections.append(Connection(
            id="conn-{0}".format(tag),
            source=PortEndpoint(node=source_id, port="out"),
            target=PortEndpoint(node=sink_id, port="in"),
        ))

    # --- The stream feeds
    feeds_wanted = draw(st.integers(min_value=1, max_value=3)) \
        if feed_count is None else feed_count
    feeds: List[_FeedSpec] = []
    for index in range(feeds_wanted):
        as_unified = draw(st.booleans()) if unified is None else unified
        if as_unified:
            kind = draw(st.sampled_from(tuple(_STREAM_KIND_TO_TYPE)))
            source_type = _STREAM_KIND_TO_TYPE[kind]
            node_type = _UNIFIED_INPUT
            forced = {_SOURCE_KIND: kind}
        else:
            source_type = draw(st.sampled_from(_STREAM_TYPES))
            node_type = source_type
            forced = None
        mode_tag, mode_value = draw(st.sampled_from(tuple(mode_spellings)))
        parameters = _apply_mode(
            draw(node_parameters_strategy(_DESCRIPTORS[node_type], forced)),
            mode_tag, mode_value,
        )
        node_id = "feed-{0}".format(index)
        nodes.append(Node(
            id=node_id,
            type=node_type,
            position=Position(x=100.0, y=float(index)),
            parameters=parameters,
        ))
        wired = wire_activation(node_id, "feed-{0}".format(index))
        add_capture_sink(node_id, "feed-{0}".format(index))
        feeds.append(_FeedSpec(
            node_id=node_id,
            node_type=node_type,
            source_type=source_type,
            mode_tag=mode_tag,
            activation_wired=wired,
        ))

    # --- Further non-frame-feed inputs, for clause 3 to preserve
    for index in range(draw(st.integers(min_value=0, max_value=max_other_inputs))):
        other_type = draw(st.sampled_from(_OTHER_INPUT_TYPES))
        other_id = "other-{0}".format(index)
        nodes.append(Node(
            id=other_id,
            type=other_type,
            position=Position(x=100.0, y=float(200 + index)),
            parameters=draw(node_parameters_strategy(_DESCRIPTORS[other_type])),
        ))
        wire_activation(other_id, "other-{0}".format(index))
        add_capture_sink(other_id, "other-{0}".format(index))

    return _Case(
        graph=WorkflowGraph(nodes=nodes, connections=connections),
        feeds=tuple(feeds),
        trigger_ids=tuple(trigger_ids),
    )


# ---------------------------------------------------------------------------
# Clause 1: V12 fires exactly on the conflicted continuous stream nodes
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(case=continuous_activation_graph_strategy())
def test_v12_is_reported_exactly_on_conflicted_continuous_stream_nodes(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Clause 1, both directions at once: the V12 finding sequence equals the
    oracle's node list, so a missed node, a duplicate, an extra node (a
    conflict-free feed, an ``on_trigger`` feed, a non-stream input) or a
    changed emission order all fail.

    **Validates: Requirements 2.3**
    """
    found = _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION)
    expected = _expected_v12_node_ids(case.graph)
    assert [finding.node_id for finding in found] == expected, (
        "V12 reported {0}, Requirement 2.3 asks for {1}".format(
            [finding.node_id for finding in found], expected))
    assert all(finding.severity == SEVERITY_ERROR for finding in found), (
        "every V12 finding must be an error: {0}".format(found))


@_EXAMPLES
@given(case=continuous_activation_graph_strategy())
def test_every_v12_finding_names_the_node_and_every_applicable_reason(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Requirement 2.3's message contract: the finding states that continuous
    processing is its own activation model, names the node and the
    ``processing_mode`` escape hatch, and names each reason that actually
    applies — the ``activation`` port exactly when an activation edge
    exists, and every subscription trigger id exactly when triggers are
    present. The "exactly when" is what keeps the message from claiming a
    conflict the graph does not have.

    **Validates: Requirements 2.3**
    """
    activation_targets = _activation_targets(case.graph)
    found = _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION)

    for finding in found:
        message = finding.message
        assert "'{0}'".format(finding.node_id) in message, message
        assert "continuous" in message, message
        assert "activation model" in message, message
        assert "'{0}'".format(_MODE) in message, message
        assert "'{0}'".format(_ON_TRIGGER) in message, message

        names_activation_port = "'{0}' port".format(_ACTIVATION) in message
        assert names_activation_port == (finding.node_id in activation_targets), (
            "the finding for '{0}' {1} the activation port, but the node {2} "
            "an activation edge: {3!r}".format(
                finding.node_id,
                "names" if names_activation_port else "does not name",
                "has" if finding.node_id in activation_targets else "has no",
                message))

        for trigger_id in case.trigger_ids:
            assert "'{0}'".format(trigger_id) in message, (
                "the finding for '{0}' does not name trigger '{1}': "
                "{2!r}".format(finding.node_id, trigger_id, message))
        if not case.trigger_ids:
            assert "subscription trigger" not in message, message


@_EXAMPLES
@given(case=continuous_activation_graph_strategy(
    trigger_count=0, activation="never", with_digital_input=False))
def test_a_conflict_free_continuous_stream_node_is_never_reported(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Clause 1's boundary from below: continuous stream feeds with no
    activation edge and no subscription trigger in the graph are the normal,
    intended configuration and draw no V12 finding — and no V9 finding
    either, since V9 needs a subscription trigger to engage at all.

    **Validates: Requirements 2.3**
    """
    assert case.trigger_ids == ()
    assert not _activation_targets(case.graph)
    assert _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION) == []
    assert _findings_with_code(case.graph, CODE_V9_MIXED_ACTIVATION_MODEL) == []


@_EXAMPLES
@given(case=continuous_activation_graph_strategy(
    trigger_count=0, activation="always", with_digital_input=True,
    mode_spellings=tuple(spelling for spelling in _MODE_SPELLINGS
                         if spelling[0] != "on_trigger")))
def test_an_activation_edge_alone_is_a_conflict(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    The first half of Requirement 2.3's condition in isolation: no
    subscription trigger anywhere, but each continuous feed's ``activation``
    port is fed (from a ``digital_input``, which is deliberately not a
    Subscription_Trigger_Node). Every feed must be reported, and V9 — which
    only engages on subscription triggers — must stay silent.

    **Validates: Requirements 2.3**
    """
    assert case.trigger_ids == ()
    wired_feeds = [feed.node_id for feed in case.feeds if feed.activation_wired]
    assert wired_feeds, "the strategy must wire every feed's activation port"

    found = _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION)
    assert [finding.node_id for finding in found] == wired_feeds
    assert _findings_with_code(case.graph, CODE_V9_MIXED_ACTIVATION_MODEL) == []


@_EXAMPLES
@given(case=continuous_activation_graph_strategy(
    trigger_count=1, activation="never", with_digital_input=False,
    mode_spellings=tuple(spelling for spelling in _MODE_SPELLINGS
                         if spelling[0] != "on_trigger")))
def test_a_subscription_trigger_alone_is_a_conflict(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    The second half of Requirement 2.3's condition in isolation: no
    activation edge anywhere, but the workflow contains a
    Subscription_Trigger_Node, so every continuous feed is reported — and
    reported once, naming the trigger.

    **Validates: Requirements 2.3**
    """
    assert len(case.trigger_ids) == 1
    assert not _activation_targets(case.graph)

    found = _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION)
    assert [finding.node_id for finding in found] == \
        [feed.node_id for feed in case.feeds]
    for finding in found:
        assert "'{0}'".format(case.trigger_ids[0]) in finding.message


@_EXAMPLES
@given(case=continuous_activation_graph_strategy(
    mode_spellings=(("on_trigger", _ON_TRIGGER),)))
def test_on_trigger_stream_nodes_are_never_reported_by_v12(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Requirement 2.5: an ``on_trigger`` stream node is not continuous, so
    V12 never reports it, whatever the graph carries — and it takes the
    existing activation-model rule exactly as any other input node does, so
    with a subscription trigger present it is reported by V9 exactly when
    its ``activation`` port is unfed.

    **Validates: Requirements 2.3, 2.5**
    """
    assert all(feed.mode_tag == "on_trigger" for feed in case.feeds)
    assert _findings_with_code(case.graph, CODE_V12_CONTINUOUS_ACTIVATION) == []

    v9 = _findings_with_code(case.graph, CODE_V9_MIXED_ACTIVATION_MODEL)
    reported = {finding.node_id for finding in v9}
    activation_targets = _activation_targets(case.graph)
    for feed in case.feeds:
        should_report = bool(case.trigger_ids) and \
            feed.node_id not in activation_targets
        assert (feed.node_id in reported) == should_report, (
            "on_trigger feed '{0}' (triggers={1}, activation wired={2}) was "
            "{3} by V9".format(
                feed.node_id, case.trigger_ids,
                feed.node_id in activation_targets,
                "reported" if feed.node_id in reported else "not reported"))


@_EXAMPLES
@given(case=continuous_activation_graph_strategy(),
       junk=st.sampled_from(_JUNK_MODES))
def test_a_processing_mode_outside_the_enum_is_not_continuous(case, junk):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    The documented reading of "in ``continuous`` mode": a value that is not
    exactly ``continuous`` (wrong case, padded, empty, a non-string) is not
    continuous, so V12 stays silent on that node — V4 reports the invalid
    enum value under its own code. Rewriting every feed's mode to a junk
    value must therefore empty V12 while the oracle agrees, and the feeds
    fall back under V9's unchanged rule.

    **Validates: Requirements 2.3**
    """
    feed_ids = {feed.node_id for feed in case.feeds}
    nodes = []
    for node in case.graph.nodes:
        if node.id in feed_ids:
            parameters = dict(node.parameters)
            parameters[_MODE] = junk
            node = Node(id=node.id, type=node.type, position=node.position,
                        parameters=parameters)
        nodes.append(node)
    graph = WorkflowGraph(nodes=nodes, connections=list(case.graph.connections))

    assert _expected_v12_node_ids(graph) == []
    assert _findings_with_code(graph, CODE_V12_CONTINUOUS_ACTIVATION) == []
    assert _findings_with_code(graph, CODE_V9_MIXED_ACTIVATION_MODEL) == \
        _pre_feature_v9_findings(graph)


# ---------------------------------------------------------------------------
# Clauses 2 and 3: V9 skips continuous stream nodes and nothing else changes
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(case=continuous_activation_graph_strategy())
def test_v9_reports_no_finding_on_a_continuous_stream_node(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Clause 2: a continuous stream node is V12's business alone, so a graph
    mixing a subscription trigger with a continuous stream feed gets one
    finding per node, not two.

    **Validates: Requirements 2.3**
    """
    continuous_ids = {node.id for node in case.graph.nodes
                      if _is_continuous_stream(node)}
    v9 = _findings_with_code(case.graph, CODE_V9_MIXED_ACTIVATION_MODEL)
    offenders = [finding for finding in v9 if finding.node_id in continuous_ids]
    assert not offenders, (
        "V9 reported continuous stream nodes: {0}".format(offenders))


@_EXAMPLES
@given(case=continuous_activation_graph_strategy())
def test_v9_findings_on_every_other_node_are_unchanged(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Clause 3, as full :class:`ValidationFinding` equality in emission order:
    V9's output equals the pre-feature rule's output minus the continuous
    stream nodes — same nodes, same messages, same order. Neither the skip
    nor anything else about V9 leaked into another node's finding.

    **Validates: Requirements 2.3, 2.5**
    """
    continuous_ids = {node.id for node in case.graph.nodes
                      if _is_continuous_stream(node)}
    expected = [finding for finding in _pre_feature_v9_findings(case.graph)
                if finding.node_id not in continuous_ids]
    actual = _findings_with_code(case.graph, CODE_V9_MIXED_ACTIVATION_MODEL)
    assert actual == expected, (
        "V9 findings differ from the pre-feature rule minus the continuous "
        "stream nodes.\nactual:   {0}\nexpected: {1}".format(actual, expected))


@st.composite
def stream_free_activation_graph_strategy(draw, max_inputs: int = 3):
    """Graphs with no stream node: 0..2 subscription triggers, an optional
    ``digital_input``, and 1..``max_inputs`` pre-feature inputs (the
    unified ``Input Source`` node among them, carrying a non-stream kind),
    a drawn subset of which are activation-wired.
    """
    nodes: List[Node] = []
    connections: List[Connection] = []

    trigger_ids: List[str] = []
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        trigger_type = draw(st.sampled_from(_TRIGGER_TYPES))
        trigger_id = "trig-{0}".format(index)
        trigger_ids.append(trigger_id)
        forced = {"greengrass": True} if trigger_type == "mqtt_subscribe" else None
        nodes.append(Node(
            id=trigger_id, type=trigger_type,
            position=Position(x=0.0, y=float(index)),
            parameters=draw(node_parameters_strategy(
                _DESCRIPTORS[trigger_type], forced)),
        ))

    event_sources = list(trigger_ids)
    if draw(st.booleans()):
        nodes.append(Node(
            id="dig-0", type=_DIGITAL_INPUT,
            position=Position(x=0.0, y=-50.0),
            parameters=draw(node_parameters_strategy(
                _DESCRIPTORS[_DIGITAL_INPUT])),
        ))
        event_sources.append("dig-0")

    non_stream_kinds = tuple(
        kind for kind in SOURCE_KIND_TO_SOURCE_TYPE
        if kind not in _STREAM_KIND_TO_TYPE
    )
    for index in range(draw(st.integers(min_value=1, max_value=max_inputs))):
        input_id = "in-{0}".format(index)
        if draw(st.booleans()):
            kind = draw(st.sampled_from(non_stream_kinds))
            node_type, forced = _UNIFIED_INPUT, {_SOURCE_KIND: kind}
        else:
            node_type, forced = draw(st.sampled_from(_OTHER_INPUT_TYPES)), None
        nodes.append(Node(
            id=input_id, type=node_type,
            position=Position(x=100.0, y=float(index)),
            parameters=draw(node_parameters_strategy(
                _DESCRIPTORS[node_type], forced)),
        ))
        if event_sources and draw(st.booleans()):
            connections.append(Connection(
                id="act-{0}".format(index),
                source=PortEndpoint(node=draw(st.sampled_from(event_sources)),
                                    port="out"),
                target=PortEndpoint(node=input_id, port=_ACTIVATION),
            ))
        sink_id = "cap-{0}".format(index)
        nodes.append(Node(
            id=sink_id, type="capture",
            position=Position(x=200.0, y=float(index)),
            parameters=draw(node_parameters_strategy(_DESCRIPTORS["capture"])),
        ))
        connections.append(Connection(
            id="conn-{0}".format(index),
            source=PortEndpoint(node=input_id, port="out"),
            target=PortEndpoint(node=sink_id, port="in"),
        ))

    return WorkflowGraph(nodes=nodes, connections=connections)


@_EXAMPLES
@given(graph=stream_free_activation_graph_strategy())
def test_stream_free_graphs_keep_their_pre_feature_v9_findings_exactly(graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Clause 3 at its strongest: for a graph with no stream node — the
    unified ``Input Source`` node carrying a non-stream kind included — V9's
    findings equal the pre-feature rule's output with nothing removed, and
    V12 reports nothing at all.

    **Validates: Requirements 2.3, 2.5**
    """
    assert all(_effective_stream_type(node) is None for node in graph.nodes), (
        "the strategy must not produce stream nodes")

    actual = _findings_with_code(graph, CODE_V9_MIXED_ACTIVATION_MODEL)
    assert actual == _pre_feature_v9_findings(graph), (
        "V9 findings for a stream-free graph differ from the pre-feature "
        "rule's output.\nactual:   {0}\nexpected: {1}".format(
            actual, _pre_feature_v9_findings(graph)))
    assert _findings_with_code(graph, CODE_V12_CONTINUOUS_ACTIVATION) == []


# ---------------------------------------------------------------------------
# The unified spelling is evaluated exactly like its source type
# ---------------------------------------------------------------------------

@st.composite
def unified_parity_case_strategy(draw):
    """The same one-feed graph twice: the feed spelled as its stream source
    type, and as a ``unified_input`` node carrying the matching kind.

    Returns ``(direct_graph, unified_graph, source_type, kind)``.
    """
    kind = draw(st.sampled_from(tuple(_STREAM_KIND_TO_TYPE)))
    source_type = _STREAM_KIND_TO_TYPE[kind]
    url = draw(stream_url_strategy(SCHEMES_BY_NODE_TYPE[source_type]))
    mode_tag, mode_value = draw(st.sampled_from(_MODE_SPELLINGS))
    trigger_count = draw(st.integers(min_value=0, max_value=2))
    wire_activation = draw(st.booleans())

    shared: Dict[str, Any] = _apply_mode({"url": url}, mode_tag, mode_value)

    def build(node_type: str, extra: Dict[str, Any]) -> WorkflowGraph:
        parameters = dict(shared)
        parameters.update(extra)
        nodes = [
            Node(id="feed", type=node_type, position=Position(x=0.0, y=0.0),
                 parameters=parameters),
            Node(id="cap", type="capture", position=Position(x=100.0, y=0.0),
                 parameters={"output_path": "/aws_dda/out"}),
        ]
        connections = [Connection(
            id="conn", source=PortEndpoint(node="feed", port="out"),
            target=PortEndpoint(node="cap", port="in"),
        )]
        event_sources: List[str] = []
        for index in range(trigger_count):
            trigger_id = "trig-{0}".format(index)
            event_sources.append(trigger_id)
            nodes.append(Node(
                id=trigger_id, type="mqtt_subscribe",
                position=Position(x=0.0, y=float(50 + index)),
                parameters={"topic": "line/{0}/trigger".format(index),
                            "greengrass": True},
            ))
        if wire_activation:
            nodes.append(Node(
                id="dig", type=_DIGITAL_INPUT,
                position=Position(x=0.0, y=-50.0),
                parameters={"pin": 17},
            ))
            event_sources.append("dig")
            connections.append(Connection(
                id="act",
                source=PortEndpoint(node=event_sources[-1], port="out"),
                target=PortEndpoint(node="feed", port=_ACTIVATION),
            ))
        return WorkflowGraph(nodes=nodes, connections=connections)

    return (
        build(source_type, {}),
        build(_UNIFIED_INPUT, {_SOURCE_KIND: kind}),
        source_type,
        kind,
    )


@_EXAMPLES
@given(case=unified_parity_case_strategy())
def test_unified_stream_kinds_are_evaluated_exactly_like_their_source_type(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    Design component 3: V12 evaluates a ``unified_input`` node through its
    effective ``source_kind``, so save-time validation of an unexpanded
    graph agrees with validation of the graph the compiler's
    ``expand_unified_inputs`` produces. The same one-feed graph, spelled
    directly and as a unified node, must therefore produce the same V12 and
    V9 findings — message included, since the message names the node id and
    both spellings use the same id.

    **Validates: Requirements 2.3, 2.5**
    """
    direct, unified, source_type, kind = case
    for code in (CODE_V12_CONTINUOUS_ACTIVATION, CODE_V9_MIXED_ACTIVATION_MODEL):
        assert _findings_with_code(direct, code) == \
            _findings_with_code(unified, code), (
            "'{0}' and the unified '{1}' kind disagree on {2}".format(
                source_type, kind, code))
    # And both agree with the oracle, so parity is not parity on a wrong
    # answer.
    assert _expected_v12_node_ids(direct) == _expected_v12_node_ids(unified)
    assert [finding.node_id for finding in
            _findings_with_code(unified, CODE_V12_CONTINUOUS_ACTIVATION)] == \
        _expected_v12_node_ids(unified)


# ---------------------------------------------------------------------------
# The shared generator's valid single-feed documents stay conflict-free
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy(stream_nodes=1))
def test_valid_single_feed_stream_documents_draw_no_activation_finding(stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 5: Continuous activation
    rule**

    The rule must not make the feature's own valid documents invalid: the
    shared generator's single-feed stream graphs (continuous by default, no
    subscription trigger, no activation edge) draw neither a V12 nor a V9
    finding, and the oracle agrees.

    **Validates: Requirements 2.3**
    """
    graph = stream_graph.graph
    assert _expected_v12_node_ids(graph) == []
    assert _findings_with_code(graph, CODE_V12_CONTINUOUS_ACTIVATION) == []
    assert _findings_with_code(graph, CODE_V9_MIXED_ACTIVATION_MODEL) == []
