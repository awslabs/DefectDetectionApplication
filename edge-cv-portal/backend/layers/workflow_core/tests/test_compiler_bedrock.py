"""Unit tests for compiling the two-input bedrock_inference node.

On device architectures the node is executor-level: the compiler
terminates every GStreamer branch feeding one of its VideoFrames input
ports in a synthetic frame-capture sink chain (videoconvert ! jpegenc !
multifilesink location={work_dir}/...), and emits a "bedrock_inference"
executor binding carrying the node's parameters plus the per-port
capture file paths. Frames do not flow through the node. In simulation
the node resolves to the model_inference-style identity stub instead
(no binding, no captures) so the harness injects the configured
simulated inference outcome (Requirement 12.6).
"""

from __future__ import annotations

from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

ARCH = "arm64_jp5"


def _node(node_id, type_id, **parameters):
    return Node(id=node_id, type=type_id, position=Position(0.0, 0.0),
                parameters=parameters)


def _connect(cid, source, target):
    return Connection(
        id=cid,
        source=PortEndpoint(node=source[0], port=source[1]),
        target=PortEndpoint(node=target[0], port=target[1]),
    )


def _graph(nodes, connections):
    return WorkflowGraph(nodes=nodes, connections=connections)


def _compile_ok(graph, arch=ARCH, simulation=False):
    document = compile(graph, arch, simulation=simulation)
    assert isinstance(document, CompiledPipelineDocument), document
    return document


def _all_elements(document):
    return [e for s in document.segments for e in s["elements"]]


def _bedrock_binding(document, node_id="bedrock"):
    bindings = [b for b in document.executor_bindings
                if b["binding"] == "bedrock_inference"
                and b["nodeId"] == node_id]
    assert len(bindings) == 1, document.executor_bindings
    return bindings[0]


def _capture_sinks(document):
    return [e for e in _all_elements(document)
            if e["factory"] == "multifilesink"]


def two_source_graph():
    """cam -> bedrock.in, ref folder -> bedrock.reference, bedrock -> mqtt."""
    return _graph(
        nodes=[
            _node("cam", "icam_source"),
            _node("ref", "folder_source", location="/aws_dda/ref/golden.jpg"),
            _node("bedrock", "bedrock_inference"),
            _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                  topic="factory/line1"),
        ],
        connections=[
            _connect("c1", ("cam", "out"), ("bedrock", "in")),
            _connect("c2", ("ref", "out"), ("bedrock", "reference")),
            _connect("c3", ("bedrock", "out"), ("mqtt", "in")),
        ],
    )


class TestDeviceCompilation:
    def test_each_input_branch_terminates_in_a_capture_sink(self):
        document = _compile_ok(two_source_graph())

        sinks = _capture_sinks(document)
        assert len(sinks) == 2
        locations = sorted(sink["args"]["location"] for sink in sinks)
        assert locations == [
            "{work_dir}/bedrock_frame_cam.jpg",
            "{work_dir}/bedrock_frame_ref.jpg",
        ]
        # Synthetic (nodeId null) capture chains: videoconvert ! jpegenc
        # ! multifilesink at the end of each feeding branch.
        for segment in document.segments:
            factories = [e["factory"] for e in segment["elements"]]
            assert factories[-3:] == ["videoconvert", "jpegenc",
                                      "multifilesink"]
            for element in segment["elements"][-3:]:
                assert element["nodeId"] is None

    def test_binding_carries_parameters_and_capture_paths(self):
        document = _compile_ok(two_source_graph())
        binding = _bedrock_binding(document)

        assert binding["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_cam.jpg",
            "reference": "{work_dir}/bedrock_frame_ref.jpg",
        }
        parameters = binding["parameters"]
        assert parameters["model"] == "us.amazon.nova-lite-v1:0"
        # The default prompt carries only the comparison semantics; the
        # executor appends the canonical JSON instruction in anomaly
        # mode (bedrock-response-mode design).
        assert "meaningfully differs" in parameters["prompt"]
        # The response-mode toggle defaults to anomaly mode and is
        # carried on the binding like every other parameter.
        assert parameters["anomaly_mode"] is True
        assert parameters["region"] == "us-east-1"
        assert parameters["max_tokens"] == 256
        assert binding["upstreamNodeIds"] == ["cam", "ref"]
        assert binding["downstreamNodeIds"] == ["mqtt"]

    def test_binding_carries_explicit_freeform_anomaly_mode(self):
        # A node configured with anomaly_mode unchecked (freeform mode)
        # compiles to a binding carrying the explicit False — the
        # compiler copies parameters generically, no special-casing.
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("ref", "folder_source",
                      location="/aws_dda/ref/golden.jpg"),
                _node("bedrock", "bedrock_inference", anomaly_mode=False),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="factory/line1"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("bedrock", "in")),
                _connect("c2", ("ref", "out"), ("bedrock", "reference")),
                _connect("c3", ("bedrock", "out"), ("mqtt", "in")),
            ],
        )
        binding = _bedrock_binding(_compile_ok(graph))
        assert binding["parameters"]["anomaly_mode"] is False

    def test_every_node_is_referenced_exactly_once(self):
        document = _compile_ok(two_source_graph())
        assert sorted(document.referenced_node_ids()) == [
            "bedrock", "cam", "mqtt", "ref"]

    def test_shared_feeder_serves_both_ports_with_one_capture(self):
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("bedrock", "bedrock_inference"),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="factory/line1"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("bedrock", "in")),
                _connect("c2", ("cam", "out"), ("bedrock", "reference")),
                _connect("c3", ("bedrock", "out"), ("mqtt", "in")),
            ],
        )
        document = _compile_ok(graph)

        # One branch, one capture file: both ports read the same frames.
        sinks = _capture_sinks(document)
        assert len(sinks) == 1
        path = sinks[0]["args"]["location"]
        assert _bedrock_binding(document)["capturePaths"] == {
            "in": path, "reference": path}

    def test_feeder_also_continuing_downstream_gets_a_capture_tee_branch(self):
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("ref", "folder_source",
                      location="/aws_dda/ref/golden.jpg"),
                _node("bedrock", "bedrock_inference"),
                _node("cap", "capture", output_path="/aws_dda/captures"),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="factory/line1"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("bedrock", "in")),
                _connect("c2", ("ref", "out"), ("bedrock", "reference")),
                # cam also feeds a capture node: its frames must both
                # continue downstream and sink to the bedrock file.
                _connect("c3", ("cam", "out"), ("cap", "in")),
                _connect("c4", ("bedrock", "out"), ("mqtt", "in")),
            ],
        )
        document = _compile_ok(graph)

        assert len(_capture_sinks(document)) == 2  # cam file + ref file
        # cam's segment ends in a tee with two queue-headed branches:
        # the capture node's chain and the bedrock frame sink.
        tees = [e for e in _all_elements(document) if e["factory"] == "tee"]
        assert len(tees) == 1
        tee_name = tees[0]["args"]["name"]
        branches = [s for s in document.segments if s["from"] == tee_name]
        assert len(branches) == 2
        for branch in branches:
            assert branch["elements"][0]["factory"] == "queue"
        branch_factory_lists = sorted(
            tuple(e["factory"] for e in branch["elements"])
            for branch in branches
        )
        assert ("queue", "videoconvert", "jpegenc", "multifilesink") in \
            branch_factory_lists
        # Every node still referenced exactly once.
        assert sorted(document.referenced_node_ids()) == [
            "bedrock", "cam", "cap", "mqtt", "ref"]

    def test_gst_node_behind_bedrock_is_fed_by_the_upstream_branch(self):
        """A GStreamer node wired DOWNSTREAM of the opaque bedrock node
        (cam -> bedrock -> capture) must not be emitted as an unfed root
        segment (from=None, no tee) — that starves the pipeline until
        the run watchdog fires (observed on ryan-orin-nano/JP6, workflow
        f81a4c66-...:9). It re-attaches to the nearest upstream GStreamer
        feeder: cam tees into the bedrock frame sink AND the capture
        node's branch."""
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("bedrock", "bedrock_inference"),
                _node("cap", "capture", output_path="/aws_dda/captures"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("bedrock", "in")),
                _connect("c2", ("bedrock", "out"), ("cap", "in")),
            ],
        )
        document = _compile_ok(graph)

        # Exactly one unfed segment: the camera source root. Every other
        # segment is fed from a tee.
        roots = [s for s in document.segments if s["from"] is None]
        assert len(roots) == 1
        assert any(e["nodeId"] == "cam" for e in roots[0]["elements"])

        # cam fans out through a tee: one queue-headed branch carries the
        # bedrock frame sink, the other carries the capture node's chain.
        tees = [e for e in _all_elements(document) if e["factory"] == "tee"]
        assert len(tees) == 1
        tee_name = tees[0]["args"]["name"]
        branches = [s for s in document.segments if s["from"] == tee_name]
        assert len(branches) == 2
        for branch in branches:
            assert branch["elements"][0]["factory"] == "queue"
        assert any(
            any(e["nodeId"] == "cap" for e in branch["elements"])
            for branch in branches
        )
        assert any(
            [e["factory"] for e in branch["elements"][-3:]]
            == ["videoconvert", "jpegenc", "multifilesink"]
            for branch in branches
        )
        assert sorted(document.referenced_node_ids()) == [
            "bedrock", "cam", "cap"]

    def test_unconnected_source_node_stays_a_root_segment(self):
        """Re-attachment only applies to nodes with incoming connections:
        a source node with no wiring above it is a legitimate root."""
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("cap", "capture", output_path="/aws_dda/captures"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("cap", "in")),
            ],
        )
        document = _compile_ok(graph)
        roots = [s for s in document.segments if s["from"] is None]
        assert len(roots) == 1

    def test_unconnected_reference_port_has_no_capture_path(self):
        graph = _graph(
            nodes=[
                _node("cam", "icam_source"),
                _node("bedrock", "bedrock_inference"),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="factory/line1"),
            ],
            connections=[
                _connect("c1", ("cam", "out"), ("bedrock", "in")),
                _connect("c2", ("bedrock", "out"), ("mqtt", "in")),
            ],
        )
        document = _compile_ok(graph)
        binding = _bedrock_binding(document)
        assert binding["capturePaths"]["in"] == \
            "{work_dir}/bedrock_frame_cam.jpg"
        assert binding["capturePaths"]["reference"] is None

    def test_unsafe_node_ids_are_sanitized_in_capture_paths(self):
        graph = _graph(
            nodes=[
                _node("cam 1", "icam_source"),
                _node("ref/2", "folder_source",
                      location="/aws_dda/ref/golden.jpg"),
                _node("bedrock", "bedrock_inference"),
                _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                      topic="factory/line1"),
            ],
            connections=[
                _connect("c1", ("cam 1", "out"), ("bedrock", "in")),
                _connect("c2", ("ref/2", "out"), ("bedrock", "reference")),
                _connect("c3", ("bedrock", "out"), ("mqtt", "in")),
            ],
        )
        binding = _bedrock_binding(_compile_ok(graph))
        assert binding["capturePaths"] == {
            "in": "{work_dir}/bedrock_frame_cam_1.jpg",
            "reference": "{work_dir}/bedrock_frame_ref_2.jpg",
        }

    def test_plugin_dependencies_include_boto3_runtime(self):
        document = _compile_ok(two_source_graph())
        assert "python:boto3" in document.plugin_dependencies
        # The capture chain's GStreamer plugins are LocalServer-bundled
        # (jpeg, multifile, videoconvertscale) and subtract out.
        assert "jpeg" not in document.plugin_dependencies
        assert "multifile" not in document.plugin_dependencies


class TestSimulationCompilation:
    def test_simulation_stubs_the_node_like_model_inference(self):
        document = _compile_ok(two_source_graph(), simulation=True)

        # The identity stub chain replaces the executor binding; the
        # harness recognizes sim_inference_<nodeId> and injects the
        # configured simulated outcome (Requirement 12.6).
        bedrock_elements = [e for e in _all_elements(document)
                            if e["nodeId"] == "bedrock"]
        assert [e["factory"] for e in bedrock_elements] == [
            "capsfilter", "identity"]
        assert bedrock_elements[1]["args"]["name"] == "sim_inference_bedrock"
        assert not [b for b in document.executor_bindings
                    if b["binding"] == "bedrock_inference"]
        # No capture sinks in simulation output.
        assert _capture_sinks(document) == []

    def test_sim_architecture_compile_uses_the_stub_chain_too(self):
        document = _compile_ok(two_source_graph(), arch="sim")
        bedrock_elements = [e for e in _all_elements(document)
                            if e["nodeId"] == "bedrock"]
        assert [e["factory"] for e in bedrock_elements] == [
            "capsfilter", "identity"]
        assert _capture_sinks(document) == []
