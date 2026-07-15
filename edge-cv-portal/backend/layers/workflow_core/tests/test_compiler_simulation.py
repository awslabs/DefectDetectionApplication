"""Unit tests for simulation-mode compilation (task 4.2).

With ``simulation=True``, hardware-dependent nodes (per the catalog flag)
map to recording stubs: dataset-fed sources via multifilesrc/appsrc for
hardware inputs, ``recording_*`` executor bindings for hardware outputs.
Non-hardware nodes compile identically to non-simulation output.

_Requirements: 12.6_
"""

from workflow_core.catalog import SIM_RECORDING_BINDING_PREFIX
from workflow_core.compiler import (
    CODE_UNMAPPED_ARCHITECTURE,
    CompileContext,
    CompiledPipelineDocument,
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
# Graph-building helpers (self-contained, mirroring test_compiler_compile)
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)

#: Hardware element factories / executor bindings that must never appear
#: in simulation output (Requirement 12.6). emltriton is proprietary and
#: absent from the sandbox image, so simulation must never emit it.
_HARDWARE_FACTORIES = {"v4l2src", "emoutputevent", "emltriton"}
_HARDWARE_BINDINGS = {"digital_input", "digital_output", "mqtt_publish",
                      "opcua_write"}


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _camera(node_id="cam"):
    return _node(node_id, "camera_source")


def _folder(node_id="src"):
    return _node(node_id, "folder_source", location="/data/images")


def _inference(node_id="inf"):
    return _node(node_id, "model_inference", modelName="widget-anomaly-v3")


def _capture(node_id="cap"):
    return _node(node_id, "capture", output_path="/out")


def _digital_output(node_id="dout"):
    return _node(node_id, "digital_output", pin=7, signal_type="pulse",
                 condition="is_anomalous == true")


def _mqtt(node_id="mq"):
    return _node(node_id, "mqtt_publish", broker_host="broker.local",
                 topic="dda/out")


def _opcua(node_id="opc"):
    return Node(id=node_id, type="opcua_write", position=_POS,
                parameters={"endpoint": "opc.tcp://plc:4840",
                            "node_id": "ns=2;s=Out"})


def _camera_graph():
    """camera_source -> model_inference -> digital_output."""
    return WorkflowGraph(
        nodes=[_camera(), _inference(), _digital_output()],
        connections=[_conn("c1", "cam", "inf"), _conn("c2", "inf", "dout")],
    )


def _mixed_graph():
    """Hardware and non-hardware nodes side by side:

    camera -> rotate -> inference -> filter -> mqtt
                              \\-> capture
    """
    return WorkflowGraph(
        nodes=[
            _camera(),
            _node("rot", "rotate", method="clockwise"),
            _inference(),
            _node("filt", "inference_filter", condition="confidence >= 0.8"),
            _mqtt(),
            _capture(),
        ],
        connections=[
            _conn("c1", "cam", "rot"),
            _conn("c2", "rot", "inf"),
            _conn("c3", "inf", "filt"),
            _conn("c4", "filt", "mq"),
            _conn("c5", "inf", "cap"),
        ],
    )


def _compile_ok(graph, arch="x86_64", context=None, simulation=False):
    result = compile(graph, arch, context, simulation=simulation)
    assert isinstance(result, CompiledPipelineDocument), (
        "expected a document, got errors: {0}".format(result)
    )
    return result


def _all_elements(document):
    return [element for segment in document.segments for element in segment["elements"]]


def _elements_of(document, node_id):
    return [e for e in _all_elements(document) if e["nodeId"] == node_id]


def _bindings_by_node(document):
    return {b["nodeId"]: b for b in document.executor_bindings}


# --------------------------------------------------------------------------
# Hardware inputs become dataset-fed sources (Requirement 12.6)
# --------------------------------------------------------------------------

class TestHardwareInputStubs:
    def test_camera_source_stubbed_with_multifilesrc(self):
        document = _compile_ok(_camera_graph(), simulation=True)
        factories = [e["factory"] for e in _elements_of(document, "cam")]
        assert factories == ["multifilesrc", "jpegparse", "jpegdec", "videoconvert"]
        # The device element from the x86_64 mapping is gone.
        assert "v4l2src" not in [e["factory"] for e in _all_elements(document)]

    def test_dataset_location_resolved_from_context(self):
        context = CompileContext(values={"dataset_location": "/data/ds/%05d.jpg"})
        document = _compile_ok(_camera_graph(), context=context, simulation=True)
        multifilesrc = _elements_of(document, "cam")[0]
        assert multifilesrc["args"]["location"] == "/data/ds/%05d.jpg"

    def test_dataset_location_left_as_placeholder_without_context(self):
        document = _compile_ok(_camera_graph(), simulation=True)
        multifilesrc = _elements_of(document, "cam")[0]
        assert multifilesrc["args"]["location"] == "{dataset_location}"

    def test_digital_input_stubbed_with_appsrc(self):
        # digital_input -> custom_python (EventSignal in) -> capture.
        graph = WorkflowGraph(
            nodes=[
                _node("din", "digital_input", pin=3),
                _node("py", "custom_python", code="def handle(x): return x",
                      input_port_type="EventSignal",
                      output_port_type="InferenceMeta"),
                _capture(),
            ],
            connections=[_conn("c1", "din", "py"), _conn("c2", "py", "cap")],
        )
        non_sim = _compile_ok(graph)
        sim = _compile_ok(graph, simulation=True)

        # Non-simulation: executor binding, no elements.
        assert _bindings_by_node(non_sim)["din"]["binding"] == "digital_input"
        assert _elements_of(non_sim, "din") == []

        # Simulation: a dataset-fed appsrc element, no hardware binding.
        assert "din" not in _bindings_by_node(sim)
        elements = _elements_of(sim, "din")
        assert [e["factory"] for e in elements] == ["appsrc"]
        assert elements[0]["args"]["name"] == "sim_source_din"

    def test_folder_source_stubbed_with_multifilesrc(self):
        # Regression: folder_source must never compile to its device
        # mapping in simulation — the device path (its location
        # parameter) and the DDA emexifextract element do not exist in
        # the sandbox. It stubs to the same dataset-fed chain as
        # camera_source, leaving {dataset_location} for the harness.
        graph = WorkflowGraph(
            nodes=[_folder(), _inference(), _capture()],
            connections=[_conn("c1", "src", "inf"), _conn("c2", "inf", "cap")],
        )
        document = _compile_ok(graph, simulation=True)
        elements = _elements_of(document, "src")
        assert [e["factory"] for e in elements] == [
            "multifilesrc", "jpegparse", "jpegdec", "videoconvert"]
        assert elements[0]["args"]["location"] == "{dataset_location}"
        # No device path or DDA element remains anywhere in the output.
        factories = {e["factory"] for e in _all_elements(document)}
        assert "emexifextract" not in factories
        assert not any(
            e["factory"] == "filesrc" and e["args"].get("location") == "/data/images"
            for e in _all_elements(document)
        )

    def test_folder_source_dataset_location_resolved_from_context(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture()],
            connections=[_conn("c1", "src", "cap")],
        )
        context = CompileContext(values={"dataset_location": "/data/ds/%05d.jpg"})
        document = _compile_ok(graph, context=context, simulation=True)
        multifilesrc = _elements_of(document, "src")[0]
        assert multifilesrc["args"]["location"] == "/data/ds/%05d.jpg"

    def test_multiple_digital_inputs_get_distinct_source_names(self):
        graph = WorkflowGraph(
            nodes=[
                _node("dinA", "digital_input", pin=1),
                _node("dinB", "digital_input", pin=2),
                _node("py", "custom_python", code="def handle(x): return x",
                      input_port_type="EventSignal",
                      output_port_type="InferenceMeta"),
                _capture(),
            ],
            connections=[
                _conn("c1", "dinA", "py"),
                _conn("c2", "dinB", "py"),
                _conn("c3", "py", "cap"),
            ],
        )
        document = _compile_ok(graph, simulation=True)
        names = {
            _elements_of(document, node_id)[0]["args"]["name"]
            for node_id in ("dinA", "dinB")
        }
        assert names == {"sim_source_dinA", "sim_source_dinB"}


# --------------------------------------------------------------------------
# Hardware outputs become recording bindings (Requirement 12.6)
# --------------------------------------------------------------------------

class TestHardwareOutputStubs:
    def _graph(self):
        return WorkflowGraph(
            nodes=[_folder(), _inference(), _digital_output(), _mqtt(), _opcua()],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "dout"),
                _conn("c3", "inf", "mq"),
                _conn("c4", "inf", "opc"),
            ],
        )

    def test_hardware_outputs_bind_to_recording_stubs(self):
        document = _compile_ok(self._graph(), simulation=True)
        bindings = _bindings_by_node(document)
        assert bindings["dout"]["binding"] == "recording_digital_output"
        assert bindings["mq"]["binding"] == "recording_mqtt_publish"
        assert bindings["opc"]["binding"] == "recording_opcua_write"
        for node_id in ("dout", "mq", "opc"):
            assert bindings[node_id]["binding"].startswith(
                SIM_RECORDING_BINDING_PREFIX)
            # Recording stubs contribute no pipeline elements.
            assert _elements_of(document, node_id) == []

    def test_recording_bindings_keep_parameters_and_topology(self):
        document = _compile_ok(self._graph(), simulation=True)
        bindings = _bindings_by_node(document)
        # The recorder still sees what the node would have actuated with.
        assert bindings["dout"]["parameters"]["pin"] == 7
        assert bindings["dout"]["parameters"]["condition"] == "is_anomalous == true"
        assert bindings["mq"]["parameters"]["topic"] == "dda/out"
        assert bindings["opc"]["parameters"]["node_id"] == "ns=2;s=Out"
        assert bindings["mq"]["upstreamNodeIds"] == ["inf"]

    def test_no_hardware_element_or_binding_remains(self):
        document = _compile_ok(_mixed_graph(), simulation=True)
        factories = {e["factory"] for e in _all_elements(document)}
        assert not factories & _HARDWARE_FACTORIES
        bindings = {b["binding"] for b in document.executor_bindings}
        assert not bindings & _HARDWARE_BINDINGS

    def test_hardware_only_plugin_dependencies_dropped(self):
        # python:opcua is required on devices but not by the recording stub.
        graph = self._graph()
        non_sim = _compile_ok(graph)
        sim = _compile_ok(graph, simulation=True)
        assert "python:opcua" in non_sim.plugin_dependencies
        assert "python:opcua" not in sim.plugin_dependencies


# --------------------------------------------------------------------------
# Model inference stubs to a pass-through in simulation (Requirement 12.6)
# --------------------------------------------------------------------------

class TestModelInferenceStub:
    def _graph(self):
        return WorkflowGraph(
            nodes=[_folder(), _inference(), _capture()],
            connections=[_conn("c1", "src", "inf"), _conn("c2", "inf", "cap")],
        )

    def test_simulation_stubs_model_inference_without_emltriton(self):
        # The proprietary emltriton plugin does not exist in the sandbox
        # and registered models are device-compiled: simulation replaces
        # the chain with the RGB capsfilter plus an identity element the
        # harness recognizes by its sim_inference_<nodeId> name.
        document = _compile_ok(self._graph(), simulation=True)
        elements = _elements_of(document, "inf")
        assert [e["factory"] for e in elements] == ["capsfilter", "identity"]
        assert elements[0]["args"]["caps"] == "video/x-raw,format=RGB"
        assert elements[1]["args"]["name"] == "sim_inference_inf"
        assert "emltriton" not in {e["factory"] for e in _all_elements(document)}
        # The stub is a pipeline element chain, not an executor binding.
        assert "inf" not in _bindings_by_node(document)

    def test_multiple_inference_nodes_get_distinct_stub_names(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _inference("infA"), _inference("infB"),
                   _capture("capA"), _capture("capB")],
            connections=[
                _conn("c1", "src", "infA"),
                _conn("c2", "src", "infB"),
                _conn("c3", "infA", "capA"),
                _conn("c4", "infB", "capB"),
            ],
        )
        document = _compile_ok(graph, simulation=True)
        names = {
            _elements_of(document, node_id)[1]["args"]["name"]
            for node_id in ("infA", "infB")
        }
        assert names == {"sim_inference_infA", "sim_inference_infB"}

    def test_emltriton_plugin_dependency_dropped_in_simulation(self):
        non_sim = _compile_ok(self._graph())
        sim = _compile_ok(self._graph(), simulation=True)
        # emltriton is LocalServer-bundled so neither lists it; the sim
        # stub also declares only bundled coreelements.
        assert "emltriton" not in sim.plugin_dependencies
        assert set(sim.plugin_dependencies) <= set(non_sim.plugin_dependencies)

    def test_device_output_for_model_inference_is_unchanged(self):
        # Non-simulation compilation must stay byte-identical on every
        # device architecture: RGB capsfilter + emltriton with the model
        # name and LocalServer Triton paths (Requirement 6.2).
        for arch in ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5",
                     "arm64_jp6"):
            document = _compile_ok(self._graph(), arch=arch)
            elements = _elements_of(document, "inf")
            assert elements == [
                {"nodeId": "inf", "factory": "capsfilter",
                 "args": {"caps": "video/x-raw,format=RGB"}},
                {"nodeId": "inf", "factory": "emltriton",
                 "args": {"model-repo": "/aws_dda/dda_triton/triton_model_repo",
                          "server-path": "/opt/tritonserver",
                          "model": "widget-anomaly-v3"}},
            ], arch


# --------------------------------------------------------------------------
# Non-hardware nodes compile identically (Requirement 12.6)
# --------------------------------------------------------------------------

class TestNonHardwareNodesUnchanged:
    NON_HARDWARE = ("rot", "filt", "cap")

    def _documents(self, arch="x86_64"):
        graph = _mixed_graph()
        return _compile_ok(graph, arch=arch), _compile_ok(
            graph, arch=arch, simulation=True)

    def test_element_chains_identical(self):
        non_sim, sim = self._documents()
        for node_id in self.NON_HARDWARE:
            assert _elements_of(sim, node_id) == _elements_of(non_sim, node_id)

    def test_executor_bindings_identical(self):
        non_sim, sim = self._documents()
        assert _bindings_by_node(sim)["filt"] == _bindings_by_node(non_sim)["filt"]

    def test_non_hardware_nodes_follow_target_arch(self):
        # On arm64_jp6, non-hardware nodes keep their jp6 mappings while
        # both frame sources (camera and folder, hardware-dependent) stub
        # to the dataset-fed chain even though the target is a device arch.
        graph = WorkflowGraph(
            nodes=[_folder(), _camera(), _inference(), _capture()],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "cam", "inf"),
                _conn("c3", "inf", "cap"),
            ],
        )
        non_sim = _compile_ok(graph, arch="arm64_jp6")
        sim = _compile_ok(graph, arch="arm64_jp6", simulation=True)
        # Non-simulation keeps folder_source's jp6 PNG-staged device chain.
        assert [e["factory"] for e in _elements_of(non_sim, "src")][:2] == [
            "filesrc", "pngdec"]
        # Simulation stubs both sources from the Test_Dataset.
        for node_id in ("src", "cam"):
            assert [e["factory"] for e in _elements_of(sim, node_id)][0] == \
                "multifilesrc"
        # Model inference stubs to the pass-through (hardware-dependent),
        # keeping its jp6 emltriton chain out of the simulation output.
        assert [e["factory"] for e in _elements_of(non_sim, "inf")] == [
            "capsfilter", "emltriton"]
        assert [e["factory"] for e in _elements_of(sim, "inf")] == [
            "capsfilter", "identity"]
        # Non-hardware nodes compile identically to the jp6 output.
        assert _elements_of(sim, "cap") == _elements_of(non_sim, "cap")
        assert sim.target_arch == "arm64_jp6"

    def test_simulation_defaults_off(self):
        document = _compile_ok(_camera_graph())
        factories = {e["factory"] for e in _all_elements(document)}
        assert "v4l2src" in factories
        assert "emoutputevent" in factories

    def test_every_node_still_referenced_exactly_once(self):
        document = _compile_ok(_mixed_graph(), simulation=True)
        assert sorted(document.referenced_node_ids()) == [
            "cam", "cap", "filt", "inf", "mq", "rot"]


# --------------------------------------------------------------------------
# Unknown architectures stay errors in simulation mode
# --------------------------------------------------------------------------

class TestUnknownArchitecture:
    def test_unknown_arch_errors_for_every_node_even_in_simulation(self):
        result = compile(_camera_graph(), "riscv64", simulation=True)
        assert isinstance(result, list)
        assert {error.code for error in result} == {CODE_UNMAPPED_ARCHITECTURE}
        assert sorted(error.node_id for error in result) == ["cam", "dout", "inf"]
        assert all(error.arch == "riscv64" for error in result)
