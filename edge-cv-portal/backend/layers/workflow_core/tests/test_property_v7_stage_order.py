# Feature: triggers-stage-and-unified-input, Property 5: Validator enforces stage-ordering legality
"""Property-based tests for the ``V7_STAGE_ORDER`` validator check (task 7.5).

Two properties over generated graphs, plus a non-suppression assertion:

- ILLEGAL: any connection whose target is the ``digital_input`` trigger node
  yields exactly one ``V7_STAGE_ORDER`` error finding carrying that
  connection's id — one finding per offending connection.
- LEGAL: the ``digital_input.out -> unified_input.activation`` edge (with the
  unified node feeding a capture sink) yields zero ``V7`` findings.
- NON-SUPPRESSION: V7 findings never alter other checks — no V1–V6/W1 code
  present for the graph with the offending connections removed disappears
  from the full graph's finding set (excluding findings attributable to the
  offending connections themselves).

Self-contained Hypothesis strategies; no shared generator module is used.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.7**
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate
from workflow_core.validator.checks import (
    CODE_V7_STAGE_ORDER,
    CODE_W1_UNUSED_OUTPUT_PORT,
)

_POS = Position(0.0, 0.0)

# Sampled set of non-trigger catalog types, each with parameter values that
# satisfy its required parameters (so parameter noise stays out of the way).
_NON_TRIGGER_TYPES = {
    "folder_source": {"location": "/data/images"},
    "csi_camera_source": {},
    "icam_source": {},
    "aravis_camera_source": {"camera_id": "Aravis-Fake-GV01"},
    "capture": {"output_path": "/out"},
}

# Required parameters of the underlying source per unified source_kind
# (csi_camera and icam need nothing: no required-without-default params).
_SOURCE_KIND_PARAMS = {
    "csi_camera": {},
    "icam": {},
    "aravis_camera": {"camera_id": "Aravis-Fake-GV01"},
    "folder": {"location": "/data/images"},
}


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


@st.composite
def digital_input_params(draw):
    """Valid ``digital_input`` parameter combinations."""
    params = {"pin": draw(st.integers(min_value=0, max_value=255))}
    if draw(st.booleans()):
        params["trigger_edge"] = draw(st.sampled_from(["rising", "falling", "both"]))
    if draw(st.booleans()):
        params["poll_interval_ms"] = draw(st.integers(min_value=10, max_value=60000))
    return params


@st.composite
def illegal_graph_strategy(draw):
    """Graphs with a ``digital_input`` node plus random non-trigger nodes and
    1..3 offending connections whose target is the digital_input (any port
    name). Returns ``(graph, offending_ids, offending_source_node_ids)``."""
    nodes = [
        _node("din", "digital_input", **draw(digital_input_params())),
        # Stable legal skeleton: a source feeding a capture sink.
        _node("base", "folder_source", location="/data/images"),
        _node("cap", "capture", output_path="/out"),
    ]
    connections = [_conn("legal-0", "base", "cap")]

    # 1..3 additional non-trigger nodes from the sampled catalog type set.
    extra_count = draw(st.integers(min_value=1, max_value=3))
    extra_ids = []
    for index in range(extra_count):
        node_type = draw(st.sampled_from(sorted(_NON_TRIGGER_TYPES)))
        node_id = "extra-{0}".format(index)
        extra_ids.append(node_id)
        nodes.append(_node(node_id, node_type, **_NON_TRIGGER_TYPES[node_type]))

    # 1..3 offending connections: TARGET is the digital_input, any port name.
    candidate_sources = ["base", "cap"] + extra_ids
    offending_count = draw(st.integers(min_value=1, max_value=3))
    offending_ids = []
    offending_sources = set()
    for index in range(offending_count):
        source_node = draw(st.sampled_from(candidate_sources))
        target_port = draw(st.sampled_from(["in", "activation", "signal", "out"]))
        conn_id = "bad-{0}".format(index)
        offending_ids.append(conn_id)
        offending_sources.add(source_node)
        connections.append(_conn(conn_id, source_node, "din", target_port=target_port))

    graph = WorkflowGraph(nodes=nodes, connections=connections)
    return graph, offending_ids, offending_sources


@st.composite
def legal_graph_strategy(draw):
    """Graphs with the legal ``digital_input.out -> unified_input.activation``
    edge plus ``unified_input.out -> capture.in``, with valid source params."""
    source_kind = draw(st.sampled_from(sorted(_SOURCE_KIND_PARAMS)))
    unified_params = dict(_SOURCE_KIND_PARAMS[source_kind])
    unified_params["source_kind"] = source_kind
    graph = WorkflowGraph(
        nodes=[
            _node("din", "digital_input", **draw(digital_input_params())),
            _node("uni", "unified_input", **unified_params),
            _node("cap", "capture", output_path="/out"),
        ],
        connections=[
            _conn("t1", "din", "uni", target_port="activation"),
            _conn("c1", "uni", "cap"),
        ],
    )
    return graph


def _attributable(finding, offending_ids, offending_sources):
    """Findings attributable to the offending connections themselves: any
    finding carrying an offending connection id, and the unused-output-port
    warning that (dis)appears on an offending connection's source node when
    the connection is removed."""
    if finding.connection_id in offending_ids:
        return True
    return (
        finding.code == CODE_W1_UNUSED_OUTPUT_PORT
        and finding.node_id in offending_sources
    )


# Feature: triggers-stage-and-unified-input, Property 5: Validator enforces stage-ordering legality
@settings(max_examples=100)
@given(data=illegal_graph_strategy())
def test_connection_targeting_trigger_yields_one_v7_finding_per_connection(data):
    """**Feature: triggers-stage-and-unified-input, Property 5: Validator
    enforces stage-ordering legality** — ILLEGAL case.

    Every connection whose target is the digital_input trigger yields
    exactly one ``V7_STAGE_ORDER`` error finding carrying that connection's
    id, and V7 findings never suppress any V1–V6/W1 code.

    **Validates: Requirements 4.1, 4.2, 4.3, 4.7**
    """
    graph, offending_ids, offending_sources = data
    findings = validate(graph)
    v7 = [f for f in findings if f.code == CODE_V7_STAGE_ORDER]

    # >=1 V7 finding; every V7 finding is an error carrying a connection id.
    assert len(v7) >= 1
    for finding in v7:
        assert finding.severity == SEVERITY_ERROR
        assert finding.connection_id in offending_ids

    # Exactly one V7 finding per offending connection, none elsewhere.
    assert Counter(f.connection_id for f in v7) == Counter(offending_ids)

    # Non-suppression: no V1-V6/W1 code present for the graph with the
    # offending connections removed disappears when V7 findings appear
    # (compare codes present, excluding findings attributable to the
    # offending connections themselves).
    stripped = WorkflowGraph(
        nodes=graph.nodes,
        connections=[c for c in graph.connections if c.id not in offending_ids],
    )
    codes_with = {
        f.code for f in findings
        if f.code != CODE_V7_STAGE_ORDER
        and not _attributable(f, offending_ids, offending_sources)
    }
    codes_without = {
        f.code for f in validate(stripped)
        if not _attributable(f, offending_ids, offending_sources)
    }
    assert codes_without <= codes_with, (
        "codes suppressed by V7 appearance: {0}".format(codes_without - codes_with))


# Feature: triggers-stage-and-unified-input, Property 5: Validator enforces stage-ordering legality
@settings(max_examples=100)
@given(graph=legal_graph_strategy())
def test_legal_trigger_to_unified_activation_yields_zero_v7_findings(graph):
    """**Feature: triggers-stage-and-unified-input, Property 5: Validator
    enforces stage-ordering legality** — LEGAL case.

    ``digital_input.out -> unified_input.activation`` (with the unified node
    feeding a capture sink and valid source params) yields zero
    ``V7_STAGE_ORDER`` findings.

    **Validates: Requirements 4.1, 4.4**
    """
    findings = validate(graph)
    assert [f for f in findings if f.code == CODE_V7_STAGE_ORDER] == []
