# Feature: trigger-activation-runtime, Property 7: Trigger bindings are emitted faithfully
"""Property test P7 — trigger bindings are emitted faithfully (task 3.3).

For any generated trigger-driven graph and device architecture, the
compiled document contains exactly one executor-binding entry per
``mqtt_subscribe``/``opcua_subscribe`` node, carrying that node's binding
kind, its effective parameters (declared defaults applied), and an
``activates`` list equal to the ordered target node ids of that trigger's
activation edges — and the compiled ``segments`` equal the segments
compiled from the same graph with the activation edges removed.

Segment-equality twin — approach and soundness argument
-------------------------------------------------------

The property compares segments "with the activation edges absent".
Removing ONLY the activation edges from a trigger-driven graph leaves the
trigger nodes present with every ``CATEGORY_INPUT`` node unwired, which
the ``V9_MIXED_ACTIVATION_MODEL`` check rejects — ``compile()`` would
refuse that twin outright. The sound realization is to compare against
the *stream-only* twin: the same graph minus the trigger nodes AND minus
the activation edges. This is equivalent to "activation edges absent"
for segment purposes because trigger nodes contribute nothing to
``segments``:

- on device architectures both trigger descriptors map to an
  executor-only ``GstMapping`` (``executor_binding`` set, empty
  ``element_chain``), so neither node is ever a GStreamer stream node;
- their only connections are the activation edges, which ``compile()``
  extracts into the activation plan *before* stream topology, segment,
  and predecessor/successor computation (design D3) — with those edges
  gone, the trigger nodes are fully disconnected from the stream DAG.

Hence the trigger graph's reduced connection set equals the twin's
connection set, and the twin's node set differs only by executor-only
nodes that emit no element chain — the two compilations must build
byte-identical ``segments``.

Generated inputs vary:

- 1–3 triggers of mixed kinds (``mqtt_subscribe`` with a drawn valid
  target — greengrass or broker host — and ``opcua_subscribe`` with
  endpoint + node_id), each with a drawn subset of optional/policy
  parameters so effective-parameter resolution covers both omitted
  (default applied) and explicit values;
- 1–3 input nodes (``unified_input`` and/or legacy sources with their
  required parameters), every one activation-wired (satisfying V9), with
  a downstream capture keeping the graph structurally complete;
- the device architecture.

Self-contained Hypothesis strategies (mirroring
``test_property_mixed_activation_model.py``); the shared
``generators.py`` module predates the trigger types and is not used
here.

**Validates: Requirements 5.1, 5.2**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

_POS = Position(0.0, 0.0)

_TRIGGER_TYPES = ("mqtt_subscribe", "opcua_subscribe")

#: Alphabet for generated string values (hosts, topics, node ids):
#: non-blank, no whitespace, so required min_length/target checks and
#: the V8 blank-host rule are always satisfied.
_SAFE_ALPHABET = "abcdefgh0123456789_-"


def _safe_text(min_size=1, max_size=16):
    return st.text(alphabet=_SAFE_ALPHABET, min_size=min_size, max_size=max_size)


# Legacy CATEGORY_INPUT source types with their required parameter values
# (csi_camera_source and icam_source need nothing).
_LEGACY_INPUT_PARAMS = {
    "csi_camera_source": {},
    "icam_source": {},
    "aravis_camera_source": {"camera_id": "Aravis-Fake-GV01"},
    "folder_source": {"location": "/data/images"},
}

# unified_input requires source_kind plus the underlying source's
# required parameters (csi_camera and icam need nothing extra).
_SOURCE_KIND_PARAMS = {
    "csi_camera": {},
    "icam": {},
    "aravis_camera": {"camera_id": "Aravis-Fake-GV01"},
    "folder": {"location": "/data/images"},
}


def _node(node_id, node_type, parameters):
    # `parameters` is a plain dict (not **kwargs): opcua_subscribe declares
    # a parameter literally named `node_id`, which would collide with the
    # helper's first argument as a keyword.
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=dict(parameters))


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


# ---------------------------------------------------------------------------
# Trigger parameter strategies (valid configurations, drawn optionals)
# ---------------------------------------------------------------------------

#: Drawn subset of the shared policy family, so effective-parameter
#: resolution is exercised with both explicit values and omitted
#: (default-applied) values.
_POLICY_OPTIONALS = {
    "concurrency_policy": st.sampled_from(["queue", "drop", "debounce"]),
    "queue_depth": st.integers(min_value=1, max_value=1000),
    "debounce_ms": st.integers(min_value=1, max_value=60000),
    "retry_limit": st.integers(min_value=0, max_value=1000),
    "priority": st.integers(min_value=0, max_value=1000),
}


@st.composite
def _mqtt_subscribe_params(draw):
    params = {"topic": draw(_safe_text())}
    # A valid target (V8): the greengrass path or a non-blank broker host.
    if draw(st.booleans()):
        params["greengrass"] = True
    else:
        params["broker_host"] = draw(_safe_text())
        if draw(st.booleans()):
            params["broker_port"] = draw(st.integers(min_value=1, max_value=65535))
    if draw(st.booleans()):
        params["qos"] = draw(st.sampled_from([0, 1, 2]))
    params.update(draw(st.fixed_dictionaries({}, optional=_POLICY_OPTIONALS)))
    return params


@st.composite
def _opcua_subscribe_params(draw):
    params = {
        "endpoint": "opc.tcp://" + draw(_safe_text()),
        "node_id": draw(_safe_text()),
    }
    if draw(st.booleans()):
        params["mode"] = draw(st.sampled_from(["subscribe", "poll"]))
    if draw(st.booleans()):
        params["sampling_interval_ms"] = draw(
            st.integers(min_value=10, max_value=60000))
    if draw(st.booleans()):
        params["poll_interval_ms"] = draw(
            st.integers(min_value=10, max_value=60000))
    params.update(draw(st.fixed_dictionaries({}, optional=_POLICY_OPTIONALS)))
    return params


_TRIGGER_PARAM_STRATEGIES = {
    "mqtt_subscribe": _mqtt_subscribe_params(),
    "opcua_subscribe": _opcua_subscribe_params(),
}


@st.composite
def _input_node(draw, node_id):
    """One CATEGORY_INPUT node: unified_input or a legacy source."""
    if draw(st.booleans()):
        source_kind = draw(st.sampled_from(sorted(_SOURCE_KIND_PARAMS)))
        params = dict(_SOURCE_KIND_PARAMS[source_kind])
        params["source_kind"] = source_kind
        return _node(node_id, "unified_input", params)
    node_type = draw(st.sampled_from(sorted(_LEGACY_INPUT_PARAMS)))
    return _node(node_id, node_type, _LEGACY_INPUT_PARAMS[node_type])


# ---------------------------------------------------------------------------
# Trigger-driven graph strategy
# ---------------------------------------------------------------------------

@st.composite
def trigger_binding_cases(draw):
    """A trigger-driven graph, its stream-only twin, and the expectations.

    Structure: 1..3 subscribe triggers of mixed kinds, 1..3 input nodes
    (every one activation-wired from a drawn trigger, satisfying V9), and
    a capture sink fed from the first input. The twin graph is the same
    node/connection set minus the trigger nodes and activation edges (see
    the module docstring for why that realizes "activation edges absent").

    Returns ``(graph, twin, expected, arch)`` where ``expected`` maps each
    trigger node id to ``(type_id, node_parameters, ordered_target_ids)``.
    """
    trigger_count = draw(st.integers(min_value=1, max_value=3))
    trigger_nodes = []
    expected = {}
    for index in range(trigger_count):
        trigger_type = draw(st.sampled_from(_TRIGGER_TYPES))
        trigger_id = "trig-{0}".format(index)
        params = draw(_TRIGGER_PARAM_STRATEGIES[trigger_type])
        trigger_nodes.append(_node(trigger_id, trigger_type, params))
        expected[trigger_id] = (trigger_type, params, [])

    input_count = draw(st.integers(min_value=1, max_value=3))
    input_nodes = []
    for index in range(input_count):
        input_nodes.append(draw(_input_node("input-{0}".format(index))))

    capture = _node("cap", "capture", {"output_path": "/out"})
    stream_connections = [_conn("c-cap", input_nodes[0].id, "cap")]

    # Every input activation-wired from a drawn trigger (V9-satisfying).
    # Activation edges are appended in input order, so each trigger's
    # expected `activates` list is its targets in connection order.
    activation_connections = []
    for index, input_node in enumerate(input_nodes):
        trigger_id = draw(st.sampled_from([t.id for t in trigger_nodes]))
        activation_connections.append(_conn(
            "act-{0}".format(index), trigger_id, input_node.id,
            target_port="activation",
        ))
        expected[trigger_id][2].append(input_node.id)

    graph = WorkflowGraph(
        nodes=trigger_nodes + input_nodes + [capture],
        connections=stream_connections + activation_connections,
    )
    # Stream-only twin: trigger nodes and activation edges removed.
    twin = WorkflowGraph(
        nodes=input_nodes + [capture],
        connections=list(stream_connections),
    )
    arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    return graph, twin, expected, arch


def _effective_parameters(type_id, node_parameters):
    """The design's effective-parameter rule, computed independently of
    the compiler: declared defaults overlaid with the node's explicit
    values (Requirement 5.1)."""
    descriptor = get_node_type(type_id)
    values = {p.name: p.default for p in descriptor.parameters}
    values.update(node_parameters)
    return values


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

# Feature: trigger-activation-runtime, Property 7: Trigger bindings are emitted faithfully
@settings(max_examples=100)
@given(case=trigger_binding_cases())
def test_trigger_bindings_are_emitted_faithfully(case):
    """**Feature: trigger-activation-runtime, Property 7: Trigger bindings
    are emitted faithfully.**

    Exactly one executor-binding entry per trigger node, carrying the
    binding kind, the effective parameters (declared defaults applied),
    and the ordered ``activates`` target list — and ``segments`` equal
    the stream-only twin's segments (activation edges absent).

    **Validates: Requirements 5.1, 5.2**
    """
    graph, twin, expected, arch = case

    document = compile(graph, arch)
    assert isinstance(document, CompiledPipelineDocument), (
        "trigger-driven graph failed to compile on {0}: {1}".format(
            arch, document))

    # Exactly one binding entry per trigger node; no extra entry of a
    # trigger binding kind exists anywhere else in the document.
    trigger_kind_entries = [
        entry for entry in document.executor_bindings
        if entry["binding"] in _TRIGGER_TYPES
    ]
    assert len(trigger_kind_entries) == len(expected), (
        "expected {0} trigger binding entries, got {1}".format(
            len(expected), trigger_kind_entries))

    for trigger_id, (type_id, node_parameters, targets) in expected.items():
        entries = [entry for entry in document.executor_bindings
                   if entry["nodeId"] == trigger_id]
        assert len(entries) == 1, (
            "expected exactly one binding entry for trigger '{0}', got "
            "{1}".format(trigger_id, entries))
        entry = entries[0]

        # Binding kind is the trigger's executor binding name.
        assert entry["binding"] == type_id

        # Effective parameters: declared defaults applied under the
        # node's explicit values (Requirement 5.1).
        assert entry["parameters"] == _effective_parameters(
            type_id, node_parameters), (
            "effective parameters mismatch for trigger '{0}'".format(
                trigger_id))

        # `activates` equals the ordered target node ids of the trigger's
        # activation edges (connection order; unified_input targets keep
        # their node id through expansion, Requirement 5.2).
        assert entry["activates"] == targets, (
            "activates mismatch for trigger '{0}': expected {1}, got "
            "{2}".format(trigger_id, targets, entry["activates"]))

    # Segments equal the stream-only twin's segments: activation edges
    # never reach GStreamer stream-topology resolution (Requirement 5.2;
    # see the module docstring for the twin's soundness argument).
    twin_document = compile(twin, arch)
    assert isinstance(twin_document, CompiledPipelineDocument), (
        "stream-only twin failed to compile on {0}: {1}".format(
            arch, twin_document))
    assert document.to_dict()["segments"] == twin_document.to_dict()["segments"], (
        "segments diverge from the activation-edge-stripped twin on "
        "{0}".format(arch))
