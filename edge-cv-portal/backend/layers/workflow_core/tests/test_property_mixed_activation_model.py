# Feature: trigger-activation-runtime, Property 3: Mixed activation model is rejected
"""Property-based tests for the ``V9_MIXED_ACTIVATION_MODEL`` validator check
(task 2.4).

For any generated graph containing at least one ``mqtt_subscribe`` or
``opcua_subscribe`` node, ``validate`` produces exactly one
``V9_MIXED_ACTIVATION_MODEL`` error finding per ``CATEGORY_INPUT`` node whose
``activation`` port has no incoming connection, and zero V9 findings when
every input is activation-wired.

Generated graphs vary:

- the number and type of subscription triggers (``mqtt_subscribe`` /
  ``opcua_subscribe``),
- the number and kind of input nodes (``unified_input`` and/or the four
  legacy sources), and
- which inputs receive an activation edge from a trigger.

Self-contained Hypothesis strategies (mirroring
``test_property_v7_stage_order.py``); the shared ``generators.py`` module
predates the trigger types and is not used here.

**Validates: Requirements 4.1**
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
from workflow_core.validator.checks import CODE_V9_MIXED_ACTIVATION_MODEL

_POS = Position(0.0, 0.0)

# Subscription trigger types with valid required parameters. mqtt_subscribe
# enables the greengrass path so no V8 finding muddies the graph; the
# property only inspects V9 findings either way.
_TRIGGER_PARAMS = {
    "mqtt_subscribe": {"topic": "factory/line1/trigger", "greengrass": True},
    "opcua_subscribe": {
        "endpoint": "opc.tcp://192.168.1.20:4840",
        "node_id": "ns=2;s=Machine1.Trigger",
    },
}

# Legacy CATEGORY_INPUT source types with their required parameter values
# (csi_camera_source and icam_source need nothing).
_LEGACY_INPUT_PARAMS = {
    "csi_camera_source": {},
    "icam_source": {},
    "aravis_camera_source": {"camera_id": "Aravis-Fake-GV01"},
    "folder_source": {"location": "/data/images"},
}

# unified_input requires source_kind plus the underlying source's required
# parameters (csi_camera and icam need nothing extra).
_SOURCE_KIND_PARAMS = {
    "csi_camera": {},
    "icam": {},
    "aravis_camera": {"camera_id": "Aravis-Fake-GV01"},
    "folder": {"location": "/data/images"},
}


def _node(node_id, node_type, parameters):
    # `parameters` is a plain dict (not **kwargs): opcua_subscribe declares a
    # parameter literally named `node_id`, which would collide with the
    # helper's first argument as a keyword.
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=dict(parameters))


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


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


@st.composite
def mixed_model_graph_strategy(draw, wire_all=False):
    """Graphs with 1..3 subscription triggers and 1..4 input nodes, a drawn
    subset of which get an activation edge from a drawn trigger (all of
    them when ``wire_all``). A capture sink fed from the first input keeps
    the graph structurally complete. Returns ``(graph, unwired_input_ids)``.
    """
    trigger_count = draw(st.integers(min_value=1, max_value=3))
    trigger_ids = []
    nodes = []
    for index in range(trigger_count):
        trigger_type = draw(st.sampled_from(sorted(_TRIGGER_PARAMS)))
        trigger_id = "trig-{0}".format(index)
        trigger_ids.append(trigger_id)
        nodes.append(_node(trigger_id, trigger_type, _TRIGGER_PARAMS[trigger_type]))

    input_count = draw(st.integers(min_value=1, max_value=4))
    input_ids = []
    for index in range(input_count):
        input_id = "input-{0}".format(index)
        input_ids.append(input_id)
        nodes.append(draw(_input_node(input_id)))

    nodes.append(_node("cap", "capture", {"output_path": "/out"}))
    connections = [_conn("c-cap", input_ids[0], "cap")]

    unwired = []
    for index, input_id in enumerate(input_ids):
        if wire_all or draw(st.booleans()):
            source_trigger = draw(st.sampled_from(trigger_ids))
            connections.append(_conn(
                "act-{0}".format(index), source_trigger, input_id,
                target_port="activation",
            ))
        else:
            unwired.append(input_id)

    graph = WorkflowGraph(nodes=nodes, connections=connections)
    return graph, unwired


# Feature: trigger-activation-runtime, Property 3: Mixed activation model is rejected
@settings(max_examples=100)
@given(data=mixed_model_graph_strategy())
def test_v9_findings_equal_unwired_input_set(data):
    """**Feature: trigger-activation-runtime, Property 3: Mixed activation
    model is rejected.**

    With at least one subscription trigger present, the V9 finding
    node-id set equals exactly the set of CATEGORY_INPUT nodes whose
    ``activation`` port has no incoming connection — one error finding
    per unconnected input, none elsewhere.

    **Validates: Requirements 4.1**
    """
    graph, unwired = data
    findings = validate(graph)
    v9 = [f for f in findings if f.code == CODE_V9_MIXED_ACTIVATION_MODEL]

    for finding in v9:
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id in unwired

    assert Counter(f.node_id for f in v9) == Counter(unwired)


# Feature: trigger-activation-runtime, Property 3: Mixed activation model is rejected
@settings(max_examples=100)
@given(data=mixed_model_graph_strategy(wire_all=True))
def test_zero_v9_findings_when_every_input_is_activation_wired(data):
    """**Feature: trigger-activation-runtime, Property 3: Mixed activation
    model is rejected** — fully wired case.

    When every CATEGORY_INPUT node has an activation edge from a
    subscription trigger, ``validate`` produces zero V9 findings.

    **Validates: Requirements 4.1**
    """
    graph, unwired = data
    assert unwired == []
    findings = validate(graph)
    assert [f for f in findings if f.code == CODE_V9_MIXED_ACTIVATION_MODEL] == []
