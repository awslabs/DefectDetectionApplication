"""Unit tests for compile() producing the Compiled Pipeline Document (task 4.1).

Covers: validation refusal, topological element ordering, one element
chain per node tagged with nodeId (emltriton configuration for model
inference, executor bindings for executor-level nodes), tee/queue
fan-out linearization, unmapped-architecture errors, plugin dependency
computation, and the CompiledPipelineDocument JSON shape.

_Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_
"""

import json

from workflow_core.compiler import (
    CODE_UNMAPPED_ARCHITECTURE,
    CODE_VALIDATION_ERROR,
    COMPILED_DOCUMENT_SCHEMA_VERSION,
    CompileContext,
    CompiledPipelineDocument,
    CompileError,
    compile,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

# --------------------------------------------------------------------------
# Graph-building helpers (self-contained; shared generators are task 2.3)
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


def _folder(node_id="src"):
    return _node(node_id, "folder_source", location="/data/images")


def _capture(node_id="cap"):
    return _node(node_id, "capture", output_path="/out")


def _rotate(node_id="rot"):
    return _node(node_id, "rotate", method="clockwise")


def _inference(node_id="inf", model="widget-anomaly-v3"):
    return _node(node_id, "model_inference", modelName=model)


def _valid_graph():
    """folder_source -> capture."""
    return WorkflowGraph(
        nodes=[_folder(), _capture()],
        connections=[_conn("c1", "src", "cap")],
    )


def _compile_ok(graph, arch="x86_64", context=None):
    result = compile(graph, arch, context)
    assert isinstance(result, CompiledPipelineDocument), (
        "expected a document, got errors: {0}".format(result)
    )
    return result


def _all_elements(document):
    return [element for segment in document.segments for element in segment["elements"]]


def _elements_of(document, node_id):
    return [e for e in _all_elements(document) if e["nodeId"] == node_id]


def _factories(document):
    return [element["factory"] for element in _all_elements(document)]


# --------------------------------------------------------------------------
# Validation refusal (Requirement 6.1)
# --------------------------------------------------------------------------

class TestValidationRefusal:
    def test_graph_with_errors_is_refused(self):
        # No input node: V1 error.
        graph = WorkflowGraph(nodes=[_capture()])
        result = compile(graph, "x86_64")
        assert isinstance(result, list) and result
        assert all(isinstance(error, CompileError) for error in result)
        assert all(error.code == CODE_VALIDATION_ERROR for error in result)

    def test_validation_errors_carry_offending_identifiers(self):
        # Missing required parameter on a specific node: V4 error.
        graph = WorkflowGraph(
            nodes=[_node("src2", "folder_source"), _capture()],
            connections=[_conn("c1", "src2", "cap")],
        )
        result = compile(graph, "x86_64")
        assert isinstance(result, list)
        assert any(error.node_id == "src2" for error in result)

    def test_warnings_do_not_block_compilation(self):
        # A detached extra input node yields only a W1 warning (unused
        # output port) - inputs are reachability roots, so no error.
        graph = WorkflowGraph(
            nodes=[_folder(), _capture(), _folder("src2")],
            connections=[_conn("c1", "src", "cap")],
        )
        _compile_ok(graph)


# --------------------------------------------------------------------------
# Element chains, tagging, and topological order (Requirements 6.1, 6.6)
# --------------------------------------------------------------------------

class TestElementChains:
    def test_every_node_referenced_exactly_once(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _inference(), _capture()],
            connections=[
                _conn("c1", "src", "rot"),
                _conn("c2", "rot", "inf"),
                _conn("c3", "inf", "cap"),
            ],
        )
        document = _compile_ok(graph)
        assert sorted(document.referenced_node_ids()) == ["cap", "inf", "rot", "src"]

    def test_connection_source_elements_precede_target_elements(self):
        graph = WorkflowGraph(
            nodes=[_capture(), _rotate(), _folder()],  # deliberately unsorted
            connections=[
                _conn("c1", "src", "rot"),
                _conn("c2", "rot", "cap"),
            ],
        )
        document = _compile_ok(graph)
        assert len(document.segments) == 1
        order = [e["nodeId"] for e in document.segments[0]["elements"]]
        assert order.index("src") < order.index("rot") < order.index("cap")

    def test_chain_elements_tagged_with_node_id(self):
        document = _compile_ok(_valid_graph())
        # folder_source's x86_64 chain: filesrc ! emexifextract ! jpegparse
        # ! jpegdec ! videoconvert ! videoflip.
        source_elements = _elements_of(document, "src")
        assert [e["factory"] for e in source_elements] == [
            "filesrc", "emexifextract", "jpegparse", "jpegdec",
            "videoconvert", "videoflip",
        ]

    def test_node_parameters_resolved_into_args(self):
        document = _compile_ok(_valid_graph())
        filesrc = _elements_of(document, "src")[0]
        assert filesrc["args"]["location"] == "/data/images"
        # Native types are preserved for single-placeholder templates.
        jpegenc = [e for e in _elements_of(document, "cap") if e["factory"] == "jpegenc"][0]
        assert jpegenc["args"]["quality"] == 100  # declared default, int


# --------------------------------------------------------------------------
# emltriton configuration (Requirement 6.2)
# --------------------------------------------------------------------------

class TestModelInference:
    def _graph(self):
        return WorkflowGraph(
            nodes=[_folder(), _inference(), _capture()],
            connections=[_conn("c1", "src", "inf"), _conn("c2", "inf", "cap")],
        )

    def test_exactly_one_emltriton_element(self):
        document = _compile_ok(self._graph())
        emltritons = [e for e in _all_elements(document) if e["factory"] == "emltriton"]
        assert len(emltritons) == 1
        assert emltritons[0]["nodeId"] == "inf"

    def test_emltriton_args_carry_model_and_localserver_triton_paths(self):
        document = _compile_ok(self._graph())
        args = [e for e in _all_elements(document) if e["factory"] == "emltriton"][0]["args"]
        assert args["model"] == "widget-anomaly-v3"
        assert args["model-repo"] == "/aws_dda/dda_triton/triton_model_repo"
        assert args["server-path"] == "/opt/tritonserver"

    def test_context_can_override_triton_paths(self):
        context = CompileContext(values={"triton_model_repo": "/custom/repo"})
        document = _compile_ok(self._graph(), context=context)
        args = [e for e in _all_elements(document) if e["factory"] == "emltriton"][0]["args"]
        assert args["model-repo"] == "/custom/repo"
        assert args["server-path"] == "/opt/tritonserver"


# --------------------------------------------------------------------------
# Executor bindings (Requirements 6.1, 6.6)
# --------------------------------------------------------------------------

class TestExecutorBindings:
    def test_executor_nodes_become_bindings_not_elements(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("filt", "inference_filter", condition="confidence >= 0.8"),
                _node("mq", "mqtt_publish", broker_host="broker.local", topic="dda/out"),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "filt"),
                _conn("c3", "filt", "mq"),
            ],
        )
        document = _compile_ok(graph)

        bindings = {b["nodeId"]: b for b in document.executor_bindings}
        assert set(bindings) == {"filt", "mq"}
        assert bindings["filt"]["binding"] == "inference_filter"
        assert bindings["filt"]["parameters"]["condition"] == "confidence >= 0.8"
        assert bindings["mq"]["binding"] == "mqtt_publish"
        assert bindings["mq"]["parameters"]["topic"] == "dda/out"
        assert bindings["mq"]["parameters"]["broker_port"] == 1883  # default

        # Executor nodes contribute no pipeline elements.
        assert _elements_of(document, "filt") == []
        assert _elements_of(document, "mq") == []
        # Every node still referenced exactly once across the document.
        assert sorted(document.referenced_node_ids()) == ["filt", "inf", "mq", "src"]

    def test_stream_continues_through_executor_nodes(self):
        # inf -> (executor) filter -> capture: capture's elements must
        # follow inference's in the same stream.
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("filt", "inference_filter", condition="is_anomalous == true"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "filt"),
                _conn("c3", "filt", "cap"),
            ],
        )
        document = _compile_ok(graph)
        assert len(document.segments) == 1
        order = [e["nodeId"] for e in document.segments[0]["elements"]]
        assert order.index("inf") < order.index("cap")

    def test_binding_records_upstream_and_downstream_nodes(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("filt", "inference_filter", condition="c"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "filt"),
                _conn("c3", "filt", "cap"),
            ],
        )
        document = _compile_ok(graph)
        binding = document.executor_bindings[0]
        assert binding["upstreamNodeIds"] == ["inf"]
        assert binding["downstreamNodeIds"] == ["cap"]
        # Single-output executor nodes carry no per-port fields.
        assert "downstreamNodeIdsByPort" not in binding
        assert "portConditions" not in binding


# --------------------------------------------------------------------------
# Conditional node compilation: two-path gating conditions
# --------------------------------------------------------------------------

class TestConditionalCompilation:
    CONDITION = "is_anomalous == true && confidence >= 0.8"

    def _graph(self):
        """src -> inf -> conditional; mq on the "true" path, opc on "false"."""
        return WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("br", "conditional", condition=self.CONDITION),
                _node("mq", "mqtt_publish", broker_host="b", topic="t"),
                Node(id="opc", type="opcua_write", position=_POS,
                     parameters={"endpoint": "opc.tcp://plc:4840",
                                 "node_id": "ns=2;s=Out"}),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "br"),
                _conn("c3", "br", "mq", source_port="true"),
                _conn("c4", "br", "opc", source_port="false"),
            ],
        )

    def test_conditional_becomes_an_executor_binding_not_elements(self):
        document = _compile_ok(self._graph())
        bindings = {b["nodeId"]: b for b in document.executor_bindings}
        assert bindings["br"]["binding"] == "conditional"
        assert bindings["br"]["parameters"]["condition"] == self.CONDITION
        assert _elements_of(document, "br") == []
        # Every node still referenced exactly once across the document.
        assert sorted(document.referenced_node_ids()) == [
            "br", "inf", "mq", "opc", "src"]

    def test_both_paths_gating_conditions(self):
        # The "true" path is gated by the condition itself; the "false"
        # path by its negation, composed with the evaluator's unary '!'.
        document = _compile_ok(self._graph())
        binding = {b["nodeId"]: b for b in document.executor_bindings}["br"]
        assert binding["portConditions"] == {
            "true": self.CONDITION,
            "false": "!({0})".format(self.CONDITION),
        }

    def test_downstream_nodes_partitioned_by_output_port(self):
        document = _compile_ok(self._graph())
        binding = {b["nodeId"]: b for b in document.executor_bindings}["br"]
        assert binding["downstreamNodeIdsByPort"] == {
            "true": ["mq"],
            "false": ["opc"],
        }
        # The port-agnostic downstream list is unchanged (the union).
        assert sorted(binding["downstreamNodeIds"]) == ["mq", "opc"]
        assert binding["upstreamNodeIds"] == ["inf"]

    def test_unconnected_false_port_yields_an_empty_port_entry(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("br", "conditional", condition="is_anomalous == true"),
                _node("mq", "mqtt_publish", broker_host="b", topic="t"),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "br"),
                _conn("c3", "br", "mq", source_port="true"),
            ],
        )
        document = _compile_ok(graph)
        binding = {b["nodeId"]: b for b in document.executor_bindings}["br"]
        assert binding["downstreamNodeIdsByPort"] == {"true": ["mq"], "false": []}

    def test_simulation_compilation_keeps_the_conditional_binding(self):
        # In simulation mode the hardware outputs become recording stubs
        # while the conditional binding compiles identically (12.6).
        result = compile(self._graph(), "x86_64", simulation=True)
        assert isinstance(result, CompiledPipelineDocument)
        bindings = {b["nodeId"]: b for b in result.executor_bindings}
        assert bindings["br"]["binding"] == "conditional"
        assert bindings["br"]["portConditions"]["true"] == self.CONDITION
        assert bindings["mq"]["binding"] == "recording_mqtt_publish"
        assert bindings["opc"]["binding"] == "recording_opcua_write"

    def test_gst_stream_continues_through_the_conditional(self):
        # A GStreamer node downstream of the conditional still linearizes
        # into the stream (the conditional gates executor bindings, not
        # buffers).
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("br", "conditional", condition="is_anomalous == true"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "br"),
                _conn("c3", "br", "cap", source_port="true"),
            ],
        )
        document = _compile_ok(graph)
        assert len(document.segments) == 1
        order = [e["nodeId"] for e in document.segments[0]["elements"]]
        assert order.index("inf") < order.index("cap")


# --------------------------------------------------------------------------
# Fan-out linearization with tee/queue (Requirement 6.3)
# --------------------------------------------------------------------------

class TestFanOut:
    def test_fan_out_emits_named_tee_and_queue_per_branch(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture("capA"), _capture("capB")],
            connections=[
                _conn("c1", "src", "capA"),
                _conn("c2", "src", "capB"),
            ],
        )
        document = _compile_ok(graph)

        tees = [e for e in _all_elements(document) if e["factory"] == "tee"]
        assert len(tees) == 1
        assert tees[0]["args"]["name"] == "t0"
        assert tees[0]["nodeId"] is None  # synthetic, not a workflow node

        branches = [s for s in document.segments if s["from"] == "t0"]
        assert len(branches) == 2
        for branch in branches:
            assert branch["elements"][0]["factory"] == "queue"
            assert branch["elements"][0]["nodeId"] is None
        branch_nodes = {b["elements"][1]["nodeId"] for b in branches}
        assert branch_nodes == {"capA", "capB"}

    def test_linear_pipeline_has_no_tee(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _capture()],
            connections=[_conn("c1", "src", "rot"), _conn("c2", "rot", "cap")],
        )
        document = _compile_ok(graph)
        assert "tee" not in _factories(document)
        assert "queue" not in _factories(document)

    def test_fan_in_converges_through_a_named_funnel(self):
        # src tees to rotA/rotB, both converge on one capture node.
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate("rotA"), _rotate("rotB"), _capture()],
            connections=[
                _conn("c1", "src", "rotA"),
                _conn("c2", "src", "rotB"),
                _conn("c3", "rotA", "cap"),
                _conn("c4", "rotB", "cap"),
            ],
        )
        document = _compile_ok(graph)

        funnels = [e for e in _all_elements(document) if e["factory"] == "funnel"]
        assert len(funnels) == 1
        funnel_name = funnels[0]["args"]["name"]
        converging = [s for s in document.segments if s["linkTo"] == funnel_name]
        assert len(converging) == 2
        # The capture chain still appears exactly once.
        assert document.referenced_node_ids().count("cap") == 1


# --------------------------------------------------------------------------
# Unmapped architecture (Requirement 6.5)
# --------------------------------------------------------------------------

class TestUnmappedArchitecture:
    def test_unknown_arch_reports_every_node_with_arch(self):
        graph = _valid_graph()
        result = compile(graph, "riscv64")
        assert isinstance(result, list)
        assert {error.code for error in result} == {CODE_UNMAPPED_ARCHITECTURE}
        assert sorted(error.node_id for error in result) == ["cap", "src"]
        assert all(error.arch == "riscv64" for error in result)

    def test_error_dict_shape(self):
        result = compile(_valid_graph(), "riscv64")
        entry = result[0].to_dict()
        assert set(entry) == {"code", "message", "nodeId", "connectionId", "arch"}


# --------------------------------------------------------------------------
# Plugin dependencies (Requirement 6.4)
# --------------------------------------------------------------------------

class TestPluginDependencies:
    def test_bundled_plugins_are_excluded(self):
        # folder_source + capture only use LocalServer-bundled plugins.
        document = _compile_ok(_valid_graph())
        assert document.plugin_dependencies == []

    def test_non_bundled_dependencies_are_reported(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _node("dw", "dewarp"), _inference(),
                # opcua's own parameter is named "node_id", clashing with
                # the helper's positional argument - build the Node directly.
                Node(id="opc", type="opcua_write", position=_POS,
                     parameters={"endpoint": "opc.tcp://plc:4840",
                                 "node_id": "ns=2;s=Out"}),
            ],
            connections=[
                _conn("c1", "src", "dw"),
                _conn("c2", "dw", "inf"),
                _conn("c3", "inf", "opc"),
            ],
        )
        document = _compile_ok(graph)
        assert document.plugin_dependencies == ["dda-dewarp", "python:opcua"]

    def test_dependencies_are_arch_specific(self):
        # icam_source on x86_64 uses bundled v4l2; still nothing extra.
        graph = WorkflowGraph(
            nodes=[_node("cam", "icam_source"), _capture()],
            connections=[_conn("c1", "cam", "cap")],
        )
        document = _compile_ok(graph, arch="arm64_jp5")
        assert document.plugin_dependencies == []
        assert document.target_arch == "arm64_jp5"


# --------------------------------------------------------------------------
# Compiled Pipeline Document output (Requirements 6.1-6.6)
# --------------------------------------------------------------------------

class TestDocumentOutput:
    def test_document_dict_shape(self):
        context = CompileContext(workflow_id="wf-1", workflow_version="3")
        document = _compile_ok(_valid_graph(), context=context)
        data = document.to_dict()
        assert set(data) == {
            "schemaVersion", "workflowId", "workflowVersion", "targetArch",
            "segments", "executorBindings", "pluginDependencies",
        }
        assert data["schemaVersion"] == COMPILED_DOCUMENT_SCHEMA_VERSION
        assert data["workflowId"] == "wf-1"
        assert data["workflowVersion"] == "3"
        assert data["targetArch"] == "x86_64"

    def test_document_serializes_to_json(self):
        document = _compile_ok(_valid_graph())
        parsed = json.loads(document.to_json())
        assert parsed["segments"][0]["elements"][0]["factory"] == "filesrc"
        for segment in parsed["segments"]:
            assert set(segment) == {"name", "from", "linkTo", "elements"}
            for element in segment["elements"]:
                assert set(element) == {"nodeId", "factory", "args"}

    def test_digital_output_config_json_derived_from_parameters(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _inference(),
                _node("dout", "digital_output", pin=7, signal_type="pulse",
                      condition="is_anomalous == true"),
            ],
            connections=[_conn("c1", "src", "inf"), _conn("c2", "inf", "dout")],
        )
        document = _compile_ok(graph)
        emoutput = [e for e in _all_elements(document) if e["factory"] == "emoutputevent"][0]
        config = json.loads(emoutput["args"]["config"])
        assert config["pin"] == 7
        assert config["signal_type"] == "pulse"
        assert config["pulse_width_ms"] == 100  # declared default
        assert config["condition"] == "is_anomalous == true"
        # Device-local script path is left for the edge renderer/context.
        assert emoutput["args"]["script-path"] == "{dio_script_path}"
