"""Property test for stream topology preservation under the llm capture plan
(spec: edge-vlm-image-inference, task 1.5).

**Feature: edge-vlm-image-inference, Property 4: Stream topology preservation for llm workflows**

*For any* valid workflow definition containing ``llm_inference`` nodes,
the compiled document's segments restricted to non-capture elements
(i.e. with synthetic capture sink chains for llm feeders removed) SHALL
equal the pre-feature compilation's segments — frames still flow through
the collapsed llm node to downstream elements. The llm node must NOT be
treated as opaque.

**Validates: Requirements 1.4**

Oracle: the pre-feature compiler is reconstructed faithfully by patching
the compiler module's ``BINDING_LLM_INFERENCE`` constant to a sentinel
no mapping carries. With the sentinel in place ``llm_node_ids`` is
empty, so the frame-capture plan receives only bedrock nodes and the
llm ``capturePaths`` emission branch never fires — exactly the compiler
as shipped before this feature (captures were emitted only for
``BINDING_BEDROCK_INFERENCE``).

Comparison: both compilations are reduced to their *real-element stream
topology* — the flow graph over node-originated elements (``nodeId`` set)
obtained by resolving segment ``from``/``linkTo`` references and
contracting every synthetic element (``nodeId: null`` — tee, queue,
funnel, and the ``videoconvert → jpegenc → multifilesink`` capture
chains) while preserving connectivity. This is precisely "segments
restricted to non-capture elements": the synthetic capture chains the
llm plan adds (inline sinks and queue-headed tee branches) contract
away, and what remains must be identical to the pre-feature stream —
same real elements, same frame-flow edges, including the edges through
each collapsed llm node to its downstream pipeline elements.

Harness: the pure ``workflow_core`` compile path on the conftest layer
sys.path (test_property_llm_model_name_preservation pattern); no AWS
fixtures. Hypothesis runs under the conftest ``portal-fast`` profile —
no hardcoded ``max_examples``.
"""
import json
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

import workflow_core.compiler.compiler as compiler_impl
from workflow_core.compiler import (
    CompileContext,
    CompiledPipelineDocument,
    compile as compile_workflow,
)
from workflow_core.serializer import parse

#: The vLLM-capable device architecture llm_inference always maps on.
ARCH = "arm64_jp6"

#: Sentinel executor-binding name no catalog mapping carries: patching
#: compiler_impl.BINDING_LLM_INFERENCE to it removes llm nodes from the
#: capture plan and suppresses their capturePaths emission — the
#: pre-feature compiler by construction.
_PRE_FEATURE_SENTINEL = "__pre_feature_no_llm_capture__"


def compile_current(graph, context):
    """The shipped (post-feature) compiler."""
    document = compile_workflow(graph, ARCH, context, simulation=False)
    assert isinstance(document, CompiledPipelineDocument), (
        "compilation failed: {0}".format(document))
    return document


def compile_pre_feature(graph, context):
    """The pre-feature compiler, reconstructed via the sentinel patch."""
    with mock.patch.object(
            compiler_impl, "BINDING_LLM_INFERENCE", _PRE_FEATURE_SENTINEL):
        document = compile_workflow(graph, ARCH, context, simulation=False)
    assert isinstance(document, CompiledPipelineDocument), (
        "pre-feature compilation failed: {0}".format(document))
    return document


# ---------------------------------------------------------------------------
# Stream-topology extraction: segments -> real-element flow graph
# ---------------------------------------------------------------------------

def element_flow_graph(document):
    """Reduce a compiled document's segments to the frame-flow graph over
    real (node-originated) elements.

    Returns ``(real_elements, edges)`` where every real element is keyed
    ``(nodeId, factory, canonical-args-json, occurrence)`` (occurrence in
    document order, so identical chain elements stay distinct) and edges
    are the pairs of real-element keys connected by the stream after
    resolving ``from``/``linkTo`` references and contracting all
    synthetic (``nodeId: null``) linking and capture elements.
    """
    segments = document.segments

    labels = {}                 # gid -> element dict
    edges = set()               # (gid, gid)
    named = {}                  # tee/funnel name -> gid
    per_segment_gids = []
    gid = 0

    for segment in segments:
        gids = []
        for element in segment["elements"]:
            labels[gid] = element
            if element["factory"] in ("tee", "funnel"):
                name = (element.get("args") or {}).get("name")
                if name is not None:
                    named[name] = gid
            gids.append(gid)
            gid += 1
        for a, b in zip(gids, gids[1:]):
            edges.add((a, b))
        per_segment_gids.append(gids)

    for segment, gids in zip(segments, per_segment_gids):
        if not gids:
            continue
        from_ref = segment.get("from")
        if from_ref is not None:
            edges.add((named[from_ref], gids[0]))
        link_to = segment.get("linkTo")
        if link_to is not None:
            edges.add((gids[-1], named[link_to]))

    # Canonical keys for real elements, disambiguated by document order.
    occurrences = {}
    canonical = {}
    for g in sorted(labels):
        element = labels[g]
        if element["nodeId"] is not None:
            key = (
                element["nodeId"],
                element["factory"],
                json.dumps(element.get("args") or {}, sort_keys=True),
            )
            index = occurrences.get(key, 0)
            occurrences[key] = index + 1
            canonical[g] = key + (index,)

    # Contract every synthetic element, preserving connectivity.
    for g in [g for g in labels if g not in canonical]:
        preds = {a for (a, b) in edges if b == g}
        succs = {b for (a, b) in edges if a == g}
        edges = {(a, b) for (a, b) in edges if a != g and b != g}
        edges |= {(a, b) for a in preds for b in succs}

    real_elements = sorted(canonical.values())
    real_edges = sorted((canonical[a], canonical[b]) for (a, b) in edges)
    return real_elements, real_edges


# ---------------------------------------------------------------------------
# Generator: workflow definitions containing llm_inference nodes
# ---------------------------------------------------------------------------

def _node(node_id, type_id, x, **parameters):
    return {"id": node_id, "type": type_id,
            "position": {"x": float(x), "y": 0.0},
            "parameters": parameters}


@st.composite
def llm_workflow_definitions(draw):
    """Validator-clean workflow definitions containing 1..2
    ``llm_inference`` nodes over varied stream topologies: sources with
    or without intermediate ``model_inference`` stages; llm ports fed by
    a GStreamer feeder (possibly shared), fed only through an opaque
    ``bedrock_inference`` output (no video feeder — the ``in: None``
    shape), or chained off another llm node's output; llm downstream
    fan-out to GStreamer (``capture``) and executor (``mqtt_publish``)
    consumers; and extra source fan-out branches. Every graph carries an
    input and an output node and keeps all nodes reachable (V1/V5), so
    the compiler's validation gate passes.

    Returns ``(definition, llm_flows)`` where ``llm_flows`` lists the
    ``(gst feeder node id, downstream gst node id)`` pairs whose frames
    must flow through a collapsed llm node.
    """
    nodes = []
    connections = []
    counter = {"c": 0}

    def connect(source, target):
        counter["c"] += 1
        connections.append({
            "id": "c{0}".format(counter["c"]),
            "from": {"node": source[0], "port": source[1]},
            "to": {"node": target[0], "port": target[1]},
        })

    # 1..2 folder_source feeds, each optionally through model_inference.
    feed_heads = []
    for i in range(draw(st.integers(min_value=1, max_value=2))):
        source_id = "src{0}".format(i)
        nodes.append(_node(source_id, "folder_source", i * 100,
                           location="/data/images{0}".format(i)))
        if draw(st.booleans()):
            stage_id = "mi{0}".format(i)
            nodes.append(_node(stage_id, "model_inference", i * 100 + 50,
                               modelName="vision-{0}".format(i)))
            connect((source_id, "out"), (stage_id, "in"))
            feed_heads.append(stage_id)
        else:
            feed_heads.append(source_id)

    # A bedrock node fed by one of the sources, created lazily: it both
    # exercises mixed-kind capture sharing (its captures exist in BOTH
    # compilations) and provides the opaque feed for via-bedrock llms.
    state = {"bedrock": False, "outputs": 0}

    def ensure_bedrock():
        if not state["bedrock"]:
            state["bedrock"] = True
            nodes.append(_node("bed0", "bedrock_inference", 700))
            connect((draw(st.sampled_from(feed_heads)), "out"),
                    ("bed0", "in"))

    # 1..2 llm nodes; each fed from a gst feeder, through the opaque
    # bedrock node (no video feeder reaches it), or off the previous
    # llm's output; each with 0..2 downstream consumers.
    llm_flows = []
    llm_gst_feeder = {}  # llm id -> transitively feeding gst node id or None
    llm_ids = []
    for j in range(draw(st.integers(min_value=1, max_value=2))):
        llm_id = "llm{0}".format(j)
        nodes.append(_node(llm_id, "llm_inference", 300 + j * 100,
                           modelName="qwen2-vl-2b",
                           prompt_template="Describe the part {confidence}"))
        feed_modes = ["gst", "bedrock"] + (["llm_chain"] if llm_ids else [])
        feed_mode = draw(st.sampled_from(feed_modes))
        if feed_mode == "gst":
            feeder = draw(st.sampled_from(feed_heads))
            connect((feeder, "out"), (llm_id, "in"))
            llm_gst_feeder[llm_id] = feeder
        elif feed_mode == "bedrock":
            ensure_bedrock()
            connect(("bed0", "out"), (llm_id, "in"))
            llm_gst_feeder[llm_id] = None  # opaque: no video feeder
        else:
            upstream_llm = llm_ids[-1]
            connect((upstream_llm, "out"), (llm_id, "in"))
            llm_gst_feeder[llm_id] = llm_gst_feeder[upstream_llm]
        llm_ids.append(llm_id)

        downstream = draw(st.sampled_from([
            (), ("capture",), ("mqtt_publish",), ("capture", "mqtt_publish"),
        ]))
        for kind in downstream:
            state["outputs"] += 1
            if kind == "capture":
                sink_id = "cap{0}".format(j)
                nodes.append(_node(sink_id, "capture", 500 + j * 100,
                                   output_path="/aws_dda/captures"))
                connect((llm_id, "out"), (sink_id, "in"))
                if llm_gst_feeder[llm_id] is not None:
                    llm_flows.append((llm_gst_feeder[llm_id], sink_id))
            else:
                sink_id = "mq{0}".format(j)
                nodes.append(_node(sink_id, "mqtt_publish", 500 + j * 100,
                                   topic="results/{0}".format(j),
                                   broker_host="localhost"))
                connect((llm_id, "out"), (sink_id, "in"))

    # Optionally the mixed-kind sharing case even without a via-bedrock
    # llm (a source feeding both binding kinds shares one capture file).
    if draw(st.booleans()):
        ensure_bedrock()

    # Optionally an extra fan-out branch straight off one feeder.
    if draw(st.booleans()):
        state["outputs"] += 1
        nodes.append(_node("fcap0", "capture", 900,
                           output_path="/aws_dda/fanout"))
        connect((draw(st.sampled_from(feed_heads)), "out"), ("fcap0", "in"))

    # V1: guarantee at least one output node.
    if state["outputs"] == 0:
        nodes.append(_node("mqfinal", "mqtt_publish", 1000,
                           topic="results/final", broker_host="localhost"))
        connect((llm_ids[-1], "out"), ("mqfinal", "in"))

    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, llm_flows


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(case=llm_workflow_definitions())
def test_stream_topology_preserved_for_llm_workflows(case):
    """**Feature: edge-vlm-image-inference, Property 4: Stream topology preservation for llm workflows**

    The compiled document's segments restricted to non-capture elements
    (synthetic capture sink chains for llm feeders removed) equal the
    pre-feature compilation's pass-through segment structure: same real
    elements, same frame-flow edges — frames still flow through the
    collapsed llm node to downstream elements (the node is NOT opaque).

    **Validates: Requirements 1.4**
    """
    definition, llm_flows = case
    parse_result = parse(json.dumps(definition))
    assert parse_result.ok, parse_result.error
    graph = parse_result.graph

    context = CompileContext(workflow_id="wf-p4", workflow_version="1")
    current = compile_current(graph, context)
    pre_feature = compile_pre_feature(graph, context)

    current_elements, current_edges = element_flow_graph(current)
    pre_elements, pre_edges = element_flow_graph(pre_feature)

    assert current_elements == pre_elements, (
        "the llm capture plan must not add, drop, or alter any real "
        "pipeline element.\ncurrent:     {0}\npre-feature: {1}".format(
            current_elements, pre_elements))
    assert current_edges == pre_edges, (
        "the llm capture plan must not change the frame-flow topology "
        "between real pipeline elements (llm nodes stay non-opaque "
        "pass-throughs).\ncurrent:     {0}\npre-feature: {1}".format(
            current_edges, pre_edges))

    # Directly: frames still flow through each collapsed llm node from
    # its GStreamer feeder to its downstream GStreamer consumer.
    for feeder_id, downstream_id in llm_flows:
        assert any(a[0] == feeder_id and b[0] == downstream_id
                   for (a, b) in current_edges), (
            "frames from feeder '{0}' no longer reach downstream node "
            "'{1}' through the collapsed llm node; edges: {2}".format(
                feeder_id, downstream_id, current_edges))
