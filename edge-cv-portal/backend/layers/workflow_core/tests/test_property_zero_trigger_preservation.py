"""Property test P1 - zero-trigger workflows compile byte-identically (task 7.1).

# Feature: triggers-stage-and-unified-input, Property 1: Zero-trigger workflows compile and package byte-identically

For all Zero_Trigger_Workflows - graphs built ONLY from pre-existing node
types (the four sources, preprocessing, model_inference, post-processing,
outputs; no ``digital_input``, no ``unified_input``) - and for each device
architecture, ``compile(graph, arch).to_dict()`` is unaffected by the
triggers-stage-and-unified-input feature.

A literal pre-feature baseline is not runnable in-process, so this module
implements the strongest in-process equivalent:

(a) ``expand_unified_inputs(graph, catalog)`` returns a graph that
    serializes byte-identically to its input: the pre-pass is an exact
    no-op on zero-trigger / zero-unified graphs. Per the design's P1
    argument, the ``expand_unified_inputs`` pre-pass is the ONLY change
    the feature introduces on the compile path (the ``digital_input``
    category relocation is validator/presentation metadata only -
    ``compile()`` keys off ``type_id`` + ``mappings``), so a verified
    no-op pre-pass means the compiled output is byte-identical to the
    pre-feature output.

(b) ``validate(graph)`` contains no V7 (``V7_STAGE_ORDER``) findings for
    zero-trigger graphs: the new check never fires on them, so the
    validator finding set (and hence compile()'s refuse-on-error gate)
    is unchanged.

(c) Golden check: two small fixed representative legacy workflows
    (``folder_source -> capture`` and
    ``icam_source -> model_inference -> mqtt_publish``) are compiled on
    every device architecture and byte-compared against the committed
    golden file ``golden_zero_trigger_compilation.json`` stored next to
    this test.

Golden provenance: the golden file was captured from the CURRENT
implementation, at the point where tasks 1-6 of this spec were complete
and verified. It is a faithful stand-in for the pre-feature baseline
because of the design's P1 argument restated in (a): expansion is the
only compile-path change the feature makes, and (a) proves it no-ops on
zero-unified graphs, so the current compile output on these legacy
workflows is identical to the pre-feature output. Regenerate (only after
a deliberate, reviewed compiler change) with:

    cd edge-cv-portal/backend/layers/workflow_core && \
        PYTHONPATH=python python3 -c \
        "from tests.test_property_zero_trigger_preservation import _write_golden; _write_golden()"

**Validates: Requirements 6.1, 6.2, 6.3, 6.7**
"""

from __future__ import annotations

import json
import os
from typing import List, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.catalog.nodes import NODE_CATALOG
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.compiler.compiler import expand_unified_inputs
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
    serialize,
)
from workflow_core.validator import validate
from workflow_core.validator.checks import CODE_V7_STAGE_ORDER

# Strategies stay self-contained in this file; ``node_parameters_strategy``
# is imported read-only from the shared generators module.
from .generators import node_parameters_strategy

_GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "golden_zero_trigger_compilation.json",
)

# ---------------------------------------------------------------------------
# Zero_Trigger_Workflow strategy (self-contained)
#
# Pre-existing node types only: the four sources, preprocessing,
# model_inference, post-processing, outputs. Deliberately excludes
# digital_input (trigger) and unified_input (the new node), so every
# generated graph is a Zero_Trigger_Workflow by construction.
# ---------------------------------------------------------------------------

#: The four retained VideoFrames source node types.
_ZERO_TRIGGER_SOURCES = (
    "csi_camera_source",
    "icam_source",
    "aravis_camera_source",
    "folder_source",
)

#: VideoFrames -> VideoFrames preprocessing node types.
_PREPROCESSING_TYPES = ("dewarp", "rotate", "crop", "format_convert")

#: InferenceMeta -> InferenceMeta post-processing node types.
_POST_PROCESSING_TYPES = ("inference_filter",)

#: InferenceMeta-consuming output node types.
_META_OUTPUT_TYPES = ("digital_output", "mqtt_publish", "opcua_write")


@st.composite
def zero_trigger_graphs(draw) -> WorkflowGraph:
    """Random valid Zero_Trigger_Workflows.

    Shape: one source -> 0..3 preprocessing -> capture, with an optional
    model_inference -> 0..2 post-processing -> meta-output branch off the
    VideoFrames tail. All wiring is forward and type-compatible; every
    parameter set is drawn valid, so ``validate()`` reports no
    error-severity findings.
    """
    nodes: List[Node] = []
    connections: List[Connection] = []

    def add_node(type_id: str) -> Node:
        descriptor = get_node_type(type_id)
        assert descriptor is not None, type_id
        node = Node(
            id="n{0}".format(len(nodes) + 1),
            type=type_id,
            position=Position(x=float(len(nodes)) * 100.0, y=0.0),
            parameters=draw(node_parameters_strategy(descriptor)),
        )
        nodes.append(node)
        return node

    def connect(source: Node, source_port: str, target: Node, target_port: str) -> None:
        connections.append(Connection(
            id="c{0}".format(len(connections) + 1),
            source=PortEndpoint(node=source.id, port=source_port),
            target=PortEndpoint(node=target.id, port=target_port),
        ))

    # One of the four retained sources (never digital_input/unified_input).
    tail = add_node(draw(st.sampled_from(_ZERO_TRIGGER_SOURCES)))

    # 0..3 chained preprocessing nodes.
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        step = add_node(draw(st.sampled_from(_PREPROCESSING_TYPES)))
        connect(tail, "out", step, "in")
        tail = step

    # A capture output guarantees V1's output-node presence.
    capture = add_node("capture")
    connect(tail, "out", capture, "in")

    # Optional inference branch: model_inference -> 0..2 post-processing
    # -> one InferenceMeta output.
    if draw(st.booleans()):
        meta_tail = add_node("model_inference")
        connect(tail, "out", meta_tail, "in")
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            step = add_node(draw(st.sampled_from(_POST_PROCESSING_TYPES)))
            connect(meta_tail, "out", step, "in")
            meta_tail = step
        output = add_node(draw(st.sampled_from(_META_OUTPUT_TYPES)))
        connect(meta_tail, "out", output, "in")

    return WorkflowGraph(nodes=nodes, connections=connections)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile_fingerprint(result) -> str:
    """Canonical byte form of a compile() outcome (document or errors)."""
    if isinstance(result, CompiledPipelineDocument):
        return result.to_json()
    return json.dumps(
        [error.to_dict() for error in result],
        sort_keys=True, indent=2, ensure_ascii=True,
    )


def _legacy_workflows() -> List[Tuple[str, WorkflowGraph]]:
    """Two small fixed representative legacy (zero-trigger) workflows."""
    folder_capture = WorkflowGraph(
        nodes=[
            Node(id="src", type="folder_source", position=Position(x=0.0, y=0.0),
                 parameters={"location": "/aws_dda/images/latest.jpg",
                             "file_pattern": "*.jpg"}),
            Node(id="sink", type="capture", position=Position(x=200.0, y=0.0),
                 parameters={"output_path": "/aws_dda/captures",
                             "interval": 0, "quality": 100}),
        ],
        connections=[
            Connection(id="c1",
                       source=PortEndpoint(node="src", port="out"),
                       target=PortEndpoint(node="sink", port="in")),
        ],
    )
    icam_inference = WorkflowGraph(
        nodes=[
            Node(id="cam", type="icam_source", position=Position(x=0.0, y=0.0),
                 parameters={"device": "/dev/video0"}),
            Node(id="infer", type="model_inference", position=Position(x=200.0, y=0.0),
                 parameters={"modelName": "widget-anomaly-v3"}),
            Node(id="publish", type="mqtt_publish", position=Position(x=400.0, y=0.0),
                 parameters={"topic": "factory/line1/inspection",
                             "greengrass": True}),
        ],
        connections=[
            Connection(id="c1",
                       source=PortEndpoint(node="cam", port="out"),
                       target=PortEndpoint(node="infer", port="in")),
            Connection(id="c2",
                       source=PortEndpoint(node="infer", port="out"),
                       target=PortEndpoint(node="publish", port="in")),
        ],
    )
    return [("folder_capture", folder_capture), ("icam_inference_mqtt", icam_inference)]


def _current_golden_payload() -> dict:
    """Compile the fixed legacy workflows on every device architecture."""
    payload = {}
    for name, graph in _legacy_workflows():
        per_arch = {}
        for arch in DEVICE_ARCHITECTURES:
            result = compile(graph, arch)
            if isinstance(result, CompiledPipelineDocument):
                per_arch[arch] = {"status": "ok", "document": result.to_dict()}
            else:
                per_arch[arch] = {
                    "status": "error",
                    "errors": [error.to_dict() for error in result],
                }
        payload[name] = per_arch
    return payload


def _canonical_bytes(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)


def _write_golden() -> None:  # pragma: no cover - deliberate manual step
    """Regenerate the golden file (see module docstring for when/how)."""
    with open(_GOLDEN_PATH, "w") as handle:
        handle.write(_canonical_bytes(_current_golden_payload()) + "\n")


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(graph=zero_trigger_graphs())
def test_zero_trigger_workflows_are_untouched_by_the_feature(graph):
    """# Feature: triggers-stage-and-unified-input, Property 1: Zero-trigger workflows compile and package byte-identically

    (a) the expansion pre-pass is an exact serialization no-op, and
    (b) the validator emits no V7 finding, and the compile outcome for
    every device architecture is byte-identical whether or not the
    pre-pass ran - the feature's only compile-path change has no effect.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.7**
    """
    # (a) expand_unified_inputs is an exact no-op on zero-unified graphs:
    # the returned graph serializes byte-identically to its input.
    expanded = expand_unified_inputs(graph, NODE_CATALOG)
    assert serialize(expanded) == serialize(graph), (
        "expand_unified_inputs changed a zero-trigger graph"
    )

    # (b) no V7 stage-order finding on zero-trigger graphs: the new
    # validator check never fires, so the finding set is unchanged.
    findings = validate(graph)
    v7 = [finding for finding in findings if finding.code == CODE_V7_STAGE_ORDER]
    assert not v7, "unexpected V7 findings on a zero-trigger graph: {0}".format(v7)

    # For each device architecture the compile outcome (document bytes or
    # error set) is identical with and without the pre-pass having run
    # ahead of compile() - i.e. compile(graph, arch).to_dict() is
    # unaffected by the feature's only compile-path change.
    for arch in DEVICE_ARCHITECTURES:
        original = _compile_fingerprint(compile(graph, arch))
        pre_expanded = _compile_fingerprint(compile(expanded, arch))
        assert original == pre_expanded, (
            "compile output differs on '{0}' for a zero-trigger graph".format(arch)
        )


# ---------------------------------------------------------------------------
# Golden check (c): fixed representative legacy workflows
# ---------------------------------------------------------------------------

def test_legacy_workflow_compilation_matches_committed_golden():
    """# Feature: triggers-stage-and-unified-input, Property 1: Zero-trigger workflows compile and package byte-identically

    Golden check: two fixed representative legacy workflows
    (folder_source -> capture; icam_source -> model_inference ->
    mqtt_publish) compile on every device architecture to output that is
    byte-identical to the committed golden capture (see module docstring
    for the golden's provenance and the design's P1 argument).

    **Validates: Requirements 6.1, 6.2, 6.3, 6.7**
    """
    assert os.path.exists(_GOLDEN_PATH), (
        "golden file missing: {0} (see module docstring to regenerate)".format(
            _GOLDEN_PATH
        )
    )
    with open(_GOLDEN_PATH) as handle:
        golden = handle.read()

    current = _canonical_bytes(_current_golden_payload()) + "\n"
    assert current == golden, (
        "compiled output for the legacy workflows no longer matches the "
        "committed golden capture - zero-trigger compilation changed"
    )

    # The golden covers every device architecture and both workflows
    # compile successfully on all of them (folder_source, icam_source,
    # model_inference, capture, and mqtt_publish all map on every device
    # architecture).
    payload = json.loads(golden)
    assert set(payload) == {"folder_capture", "icam_inference_mqtt"}
    for per_arch in payload.values():
        assert set(per_arch) == set(DEVICE_ARCHITECTURES)
        for entry in per_arch.values():
            assert entry["status"] == "ok"
