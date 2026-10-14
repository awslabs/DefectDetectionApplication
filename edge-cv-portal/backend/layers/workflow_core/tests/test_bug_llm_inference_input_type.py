"""Bug 1 exploration test — VLM/LLM inference input port type.

Bugfix spec: workflow-manager-integration-bugfixes (Bug 1).

The `llm_inference` node declares an `InferenceMeta` input port, so it
consumes inference metadata rather than video frames. As a
vision-language node it should take video frames as input.

This is an EXPLORATION test written against the UNFIXED code: it asserts
the CORRECTED behavior (Property 1 — Bug Condition), so it is EXPECTED TO
FAIL on the current catalog (the `in` port is `InferenceMeta`). The
failure confirms the bug exists.

Property 1: Bug Condition — VLM/LLM inference takes video frames
  For any node-type descriptor where isBugCondition1 holds
  (type_id == "llm_inference" with an InferenceMeta input port), the
  fixed catalog SHALL declare the `in` port as VideoFrames and SHALL
  keep the `out` port as InferenceMeta, so a VideoFrames source connects
  directly into it.

Validates: Requirements 1.1, 2.1
"""

from workflow_core.catalog import (
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    get_node_type,
)
from workflow_core.catalog.compatibility import are_port_types_compatible
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator.checks import SEVERITY_ERROR, validate


def _port_type(ports, name):
    for port in ports:
        if port.name == name:
            return port.port_type
    raise AssertionError(f"port {name!r} not found in {ports!r}")


class TestBug1LlmInferenceInputType:
    """Property 1: Bug Condition — VLM/LLM inference takes video frames."""

    def test_llm_inference_in_port_is_video_frames(self):
        # Fix-checking assertion: the `in` port SHALL be VideoFrames.
        # EXPECTED OUTCOME on UNFIXED code: FAILS (currently InferenceMeta).
        descriptor = get_node_type("llm_inference")
        assert descriptor is not None
        assert _port_type(descriptor.inputs, "in") == PORT_TYPE_VIDEO_FRAMES

    def test_llm_inference_out_port_stays_inference_meta(self):
        # Preservation: the `out` port must remain InferenceMeta.
        descriptor = get_node_type("llm_inference")
        assert _port_type(descriptor.outputs, "out") == PORT_TYPE_INFERENCE_META

    def test_video_frames_source_connects_into_llm_inference_in(self):
        # Companion designer check: a VideoFrames source (e.g.
        # csi_camera_source.out) SHALL connect into llm_inference.in.
        # EXPECTED OUTCOME on UNFIXED code: FAILS — a VideoFrames output
        # into an InferenceMeta input is not compatible (coercion only
        # runs InferenceMeta -> VideoFrames).
        source = get_node_type("csi_camera_source")
        target = get_node_type("llm_inference")
        source_out = _port_type(source.outputs, "out")
        target_in = _port_type(target.inputs, "in")
        assert source_out == PORT_TYPE_VIDEO_FRAMES
        assert are_port_types_compatible(source_out, target_in)


# --------------------------------------------------------------------------
# Integration check (task 3.3): frame input accepted end to end, and the
# declared InferenceMeta -> VideoFrames coercion still feeds llm_inference.
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _errors(findings):
    return [f for f in findings if f.severity == SEVERITY_ERROR]


class TestBug1LlmInferenceFrameInputIntegration:
    """Property 1: Expected Behavior — a video-frame source feeds VLM/LLM
    inference end to end after the fix."""

    def test_csi_camera_to_llm_to_mqtt_validates_and_compiles(self):
        # A VideoFrames source (csi_camera_source.out) connects directly
        # into llm_inference.in, which emits InferenceMeta into mqtt_publish.
        # The graph SHALL validate (no errors) and compile on a
        # vLLM-capable architecture (arm64_jp6).
        graph = WorkflowGraph(
            nodes=[
                _node("cam", "csi_camera_source"),
                _node("llm", "llm_inference",
                      modelName="opt-125m",
                      prompt_template="Describe this frame"),
                _node("mq", "mqtt_publish", broker_host="broker.local", topic="dda/out"),
            ],
            connections=[
                _conn("c1", "cam", "llm"),
                _conn("c2", "llm", "mq"),
            ],
        )

        # Validation accepts the frame-input graph (no error-severity findings).
        assert _errors(validate(graph)) == []

        # It compiles end to end on a vLLM-capable architecture, referencing
        # every node exactly once.
        document = compile(graph, "arm64_jp6")
        assert isinstance(document, CompiledPipelineDocument), (
            "expected a compiled document, got errors: {0}".format(document)
        )
        assert sorted(document.referenced_node_ids()) == ["cam", "llm", "mq"]

    def test_inference_filter_still_connects_into_llm_via_coercion(self):
        # An inference_filter (out : InferenceMeta) SHALL still connect into
        # llm_inference.in (now VideoFrames) through the one declared
        # InferenceMeta -> VideoFrames coercion (edge case: coercion preserved).
        source = get_node_type("inference_filter")
        target = get_node_type("llm_inference")
        source_out = _port_type(source.outputs, "out")
        target_in = _port_type(target.inputs, "in")
        assert source_out == PORT_TYPE_INFERENCE_META
        assert target_in == PORT_TYPE_VIDEO_FRAMES
        assert are_port_types_compatible(source_out, target_in)
