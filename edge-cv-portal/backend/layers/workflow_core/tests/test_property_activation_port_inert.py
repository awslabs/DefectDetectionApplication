# Feature: triggers-stage-and-unified-input, Property 4: The activation port is inert
"""Property test P4 — the unified node's activation port is inert.

*For all* `unified_input` configurations — whether the `activation` port
is unconnected or connected to a `CATEGORY_TRIGGER` node's output — the
compiled output equals the corresponding always-running source's
compiled output, and no trigger-driven activation binding is emitted for
the activation port.

Concretely, for every generated (source_kind, params, arch, connected):

- (a) compilation succeeds for the unified graph in both connection
  states and for the hand-placed underlying source graph;
- (b) the connected-case document equals the unconnected-case document
  on the dataflow parts (segments / pluginDependencies), with the ONLY
  executorBindings difference being the feeding digital_input node's own
  ordinary executor binding;
- (c) no binding of any kind references the activation port — the
  string "activation" appears nowhere in either document's
  executorBindings (generated values are drawn from alphabets that
  cannot spell it, so absence is meaningful);
- (d) the unconnected unified document equals the hand-placed underlying
  source document exactly (to_dict equality).

**Validates: Requirements 3.9, 3.10, 3.11**

Strategies are self-contained in this module (generators.py untouched).
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

_POS = Position(0.0, 0.0)

#: source_kind -> underlying source type (mirrors SOURCE_KIND_TO_SOURCE_TYPE).
_SOURCE_TYPES = {
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "aravis_camera": "aravis_camera_source",
    "folder": "folder_source",
}

# ---------------------------------------------------------------------------
# Self-contained strategies
# ---------------------------------------------------------------------------

# String values drawn from an alphabet that cannot spell "activation"
# (no 'a'/'c'/'t'/'i'/'v'/'o'/'n'), so assertion (c) below cannot be
# defeated by a generated parameter value that happens to contain it.
_SAFE_ALPHABET = "bdefgh0123456789_-"


def _safe_string(prefix=""):
    return st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=12).map(
        lambda s: prefix + s)


# Valid generated params per underlying source descriptor
# (folder needs location; icam needs device; aravis needs camera_id;
# csi has only optional gain/exposure).
_GAIN = st.integers(min_value=0, max_value=100)          # min 0, max 100
_EXPOSURE = st.integers(min_value=0, max_value=20000000)  # min 0

_PARAMS_BY_KIND = {
    "csi_camera": st.fixed_dictionaries(
        {},
        optional={"gain": _GAIN, "exposure": _EXPOSURE},
    ),
    "icam": st.fixed_dictionaries(
        {"device": _safe_string("/dev/")},               # required, min_length 1
    ),
    "aravis_camera": st.fixed_dictionaries(
        {"camera_id": _safe_string()},                   # required, min_length 1
        optional={"gain": _GAIN, "exposure": _EXPOSURE},
    ),
    "folder": st.fixed_dictionaries(
        {"location": _safe_string("/")},                 # required, min_length 1
        optional={"file_pattern": _safe_string("*.")},   # optional, min_length 1
    ),
}

# Valid digital_input trigger params (pin 0-255 required;
# trigger_edge enum; poll_interval_ms 10-60000).
_TRIGGER_PARAMS = st.fixed_dictionaries(
    {"pin": st.integers(min_value=0, max_value=255)},
    optional={
        "trigger_edge": st.sampled_from(["rising", "falling", "both"]),
        "poll_interval_ms": st.integers(min_value=10, max_value=60000),
    },
)


@st.composite
def activation_cases(draw):
    source_kind = draw(st.sampled_from(sorted(_SOURCE_TYPES)))
    params = draw(_PARAMS_BY_KIND[source_kind])
    arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    connected = draw(st.booleans())
    trigger_params = draw(_TRIGGER_PARAMS)
    return source_kind, params, arch, connected, trigger_params


# ---------------------------------------------------------------------------
# Graph builders: unified_input('src') -> capture('cap'), optionally with
# digital_input('trig') feeding src.activation
# ---------------------------------------------------------------------------

def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=parameters)


def _conn(conn_id, source_node, target_node,
          source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _unified_graph(source_kind, params, trigger_params=None):
    nodes = [
        _node("src", "unified_input", source_kind=source_kind, **params),
        _node("cap", "capture", output_path="/out"),
    ]
    connections = [_conn("c1", "src", "cap")]
    if trigger_params is not None:
        nodes.append(_node("trig", "digital_input", **trigger_params))
        connections.append(
            _conn("a1", "trig", "src", target_port="activation"))
    return WorkflowGraph(nodes=nodes, connections=connections)


def _source_graph(source_kind, params):
    """Hand-placed underlying source with the same id and params."""
    return WorkflowGraph(
        nodes=[
            _node("src", _SOURCE_TYPES[source_kind], **params),
            _node("cap", "capture", output_path="/out"),
        ],
        connections=[_conn("c1", "src", "cap")],
    )


def _compile_ok(graph, arch, label):
    result = compile(graph, arch)
    assert isinstance(result, CompiledPipelineDocument), (
        "(a) compile failed for {0} on {1}: {2}".format(label, arch, result))
    return result


def _dataflow_parts(document):
    data = document.to_dict()
    return data["segments"], data["pluginDependencies"]


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(case=activation_cases())
def test_activation_port_is_inert(case):
    """**Feature: triggers-stage-and-unified-input, Property 4: The
    activation port is inert**

    **Validates: Requirements 3.9, 3.10, 3.11**
    """
    source_kind, params, arch, connected, trigger_params = case

    # (a) the unified graph compiles, unconnected...
    unconnected_doc = _compile_ok(
        _unified_graph(source_kind, params), arch, "unconnected unified")

    # (d) ...and the unconnected unified document equals the hand-placed
    # underlying source document exactly (3.9, 3.11).
    hand_placed_doc = _compile_ok(
        _source_graph(source_kind, params), arch, "hand-placed source")
    assert unconnected_doc.to_dict() == hand_placed_doc.to_dict(), (
        "unconnected unified({0}) != hand-placed {1} on {2}".format(
            source_kind, _SOURCE_TYPES[source_kind], arch))

    documents = [unconnected_doc]

    if connected:
        # (a) the connected graph (trig.out -> src.activation) compiles too.
        connected_doc = _compile_ok(
            _unified_graph(source_kind, params, trigger_params),
            arch, "connected unified")
        documents.append(connected_doc)

        # (b) connected == unconnected on the dataflow parts (3.10, 3.11)...
        assert _dataflow_parts(connected_doc) == _dataflow_parts(
            unconnected_doc), (
            "connected/unconnected dataflow diverges for {0} on {1}".format(
                source_kind, arch))

        # ...with the only difference being the digital_input node's own
        # ordinary executor binding.
        trig_entries = [b for b in connected_doc.executor_bindings
                        if b["nodeId"] == "trig"]
        other_entries = [b for b in connected_doc.executor_bindings
                         if b["nodeId"] != "trig"]
        assert other_entries == unconnected_doc.executor_bindings, (
            "connected case changed non-trigger bindings on {0}".format(arch))
        assert len(trig_entries) == 1
        trig_binding = trig_entries[0]
        assert trig_binding["binding"] == "digital_input"
        assert trig_binding["parameters"]["pin"] == trigger_params["pin"]
        # The activation edge was dropped: the trigger has no downstream.
        assert trig_binding["downstreamNodeIds"] == []

    # (c) no binding of any kind references the activation port: the
    # string "activation" appears nowhere in executorBindings (entries
    # carry nodeId/binding/parameters/upstreamNodeIds/downstreamNodeIds
    # and optional per-port maps; generated values cannot spell it).
    for document in documents:
        serialized_bindings = json.dumps(document.executor_bindings)
        assert "activation" not in serialized_bindings, (
            "activation reference leaked into executorBindings on "
            "{0}: {1}".format(arch, serialized_bindings))
