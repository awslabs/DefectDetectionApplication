"""Unit tests for catalog content (task 1.4).

Asserts presence and parameterization of all required input,
preprocessing, inference, post-processing, and output node types.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
"""

from workflow_core.catalog import (
    CATEGORY_INFERENCE,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_POST_PROCESSING,
    CATEGORY_PREPROCESSING,
    NODE_CATALOG,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    get_node_type,
    nodes_by_category,
)


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


def _port_types(ports):
    return [port.port_type for port in ports]


# --------------------------------------------------------------------------
# Requirement 2.1: input node types
# --------------------------------------------------------------------------

class TestInputNodeTypes:
    def test_required_input_types_present(self):
        for type_id in ("camera_source", "folder_source", "digital_input"):
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.category == CATEGORY_INPUT
            # Input nodes originate data: no input ports, at least one output.
            assert descriptor.inputs == []
            assert len(descriptor.outputs) >= 1

    def test_camera_source_parameterization(self):
        descriptor = get_node_type("camera_source")
        assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
        params = _params_by_name(descriptor)
        assert "device" in params
        assert params["device"].param_type == "string"
        assert params["device"].default == "/dev/video0"
        assert "gain" in params
        assert params["gain"].param_type == "int"
        assert params["gain"].constraints.get("min") == 0
        assert params["gain"].constraints.get("max") == 100
        assert "exposure" in params
        assert params["exposure"].param_type == "int"
        assert descriptor.hardware_dependent is True

    def test_folder_source_parameterization(self):
        descriptor = get_node_type("folder_source")
        assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
        params = _params_by_name(descriptor)
        assert params["location"].required is True
        assert params["location"].param_type == "string"
        assert params["file_pattern"].required is False
        assert params["file_pattern"].default == "*.jpg"
        # Reads the device-local file system, which the cloud sandbox does
        # not have: hardware-dependent so simulation feeds it from the
        # Test_Dataset (Requirement 12.6).
        assert descriptor.hardware_dependent is True

    def test_folder_source_sim_mapping_is_the_dataset_fed_stub(self):
        # The sim mapping is the same dataset-fed recording stub
        # camera_source uses: multifilesrc on the {dataset_location}
        # placeholder with stock decode elements — never the device
        # filesrc/emexifextract chain (Requirement 12.6).
        folder = get_node_type("folder_source").mapping_for("sim")
        camera = get_node_type("camera_source").mapping_for("sim")
        assert folder == camera
        factories = [entry["factory"] for entry in folder.element_chain]
        assert factories == ["multifilesrc", "jpegparse", "jpegdec",
                             "videoconvert"]
        multifilesrc = folder.element_chain[0]
        assert multifilesrc["args_template"]["location"] == "{dataset_location}"
        assert "emexifextract" not in folder.plugin_dependencies

    def test_digital_input_parameterization(self):
        descriptor = get_node_type("digital_input")
        assert _port_types(descriptor.outputs) == [PORT_TYPE_EVENT_SIGNAL]
        params = _params_by_name(descriptor)
        assert params["pin"].required is True
        assert params["pin"].param_type == "int"
        assert params["pin"].constraints == {"min": 0, "max": 255}
        assert params["trigger_edge"].param_type == "enum"
        assert set(params["trigger_edge"].constraints["values"]) == {
            "rising", "falling", "both"}
        assert params["trigger_edge"].default == "rising"
        assert params["poll_interval_ms"].param_type == "int"
        assert descriptor.hardware_dependent is True


# --------------------------------------------------------------------------
# Requirement 2.2: preprocessing node types
# --------------------------------------------------------------------------

class TestPreprocessingNodeTypes:
    def test_required_preprocessing_types_present(self):
        for type_id in ("dewarp", "rotate", "crop", "format_convert"):
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.category == CATEGORY_PREPROCESSING
            # Video-in, video-out filters.
            assert _port_types(descriptor.inputs) == [PORT_TYPE_VIDEO_FRAMES]
            assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
            assert descriptor.hardware_dependent is False

    def test_dewarp_parameterization(self):
        params = _params_by_name(get_node_type("dewarp"))
        assert params["mode"].param_type == "enum"
        assert set(params["mode"].constraints["values"]) == {
            "fisheye", "barrel", "perspective"}
        assert params["strength"].param_type == "float"
        assert params["strength"].constraints == {"min": 0.0, "max": 1.0}
        assert params["strength"].default == 0.5

    def test_rotate_parameterization(self):
        params = _params_by_name(get_node_type("rotate"))
        assert params["method"].required is True
        assert params["method"].param_type == "enum"
        assert "clockwise" in params["method"].constraints["values"]
        assert "rotate-180" in params["method"].constraints["values"]

    def test_crop_parameterization(self):
        params = _params_by_name(get_node_type("crop"))
        for side in ("top", "bottom", "left", "right"):
            assert side in params
            assert params[side].param_type == "int"
            assert params[side].default == 0
            assert params[side].constraints.get("min") == 0

    def test_format_convert_parameterization(self):
        params = _params_by_name(get_node_type("format_convert"))
        assert params["format"].required is True
        assert params["format"].param_type == "enum"
        assert {"RGB", "BGR", "GRAY8"} <= set(params["format"].constraints["values"])
        assert params["format"].default == "RGB"


# --------------------------------------------------------------------------
# Requirement 2.3: model inference node type
# --------------------------------------------------------------------------

class TestModelInferenceNodeType:
    def test_model_inference_present(self):
        descriptor = get_node_type("model_inference")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_INFERENCE
        assert _port_types(descriptor.inputs) == [PORT_TYPE_VIDEO_FRAMES]
        assert _port_types(descriptor.outputs) == [PORT_TYPE_INFERENCE_META]

    def test_model_inference_parameterization(self):
        descriptor = get_node_type("model_inference")
        params = _params_by_name(descriptor)
        # Model selected from the portal model registry (Requirement 2.6 shape).
        assert params["modelName"].required is True
        assert params["modelName"].param_type == "model_ref"

    def test_model_inference_uses_triton_path_on_device_architectures(self):
        # Runs via the LocalServer Triton path on every physical device
        # architecture: emltriton element configured with the model name
        # and Triton repo/server paths. The exact chain is asserted so the
        # device output can never drift (the sim stub below must not
        # change device behavior).
        descriptor = get_node_type("model_inference")
        device_mappings = [m for m in descriptor.mappings if m.arch != "sim"]
        assert sorted(m.arch for m in device_mappings) == sorted(
            ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"])
        for mapping in device_mappings:
            factories = [entry["factory"] for entry in mapping.element_chain]
            assert factories == ["capsfilter", "emltriton"]
            capsfilter = mapping.element_chain[0]
            assert capsfilter["args_template"] == {
                "caps": "video/x-raw,format=RGB"}
            args = mapping.element_chain[1]["args_template"]
            assert args["model"] == "{modelName}"
            assert args["model-repo"] == "{triton_model_repo}"
            assert args["server-path"] == "{triton_server_path}"
            assert mapping.plugin_dependencies == ["coreelements", "emltriton"]

    def test_model_inference_sim_mapping_is_a_passthrough_stub(self):
        # The cloud sandbox has no emltriton plugin and registered models
        # are device-compiled, so simulation stubs the node: a pass-through
        # chain (RGB capsfilter + identity named sim_inference_<nodeId>)
        # keeps the stream flowing while the harness injects the configured
        # simulated inference outcome (Requirement 12.6).
        descriptor = get_node_type("model_inference")
        assert descriptor.hardware_dependent is True
        sim = descriptor.mapping_for("sim")
        assert sim is not None
        factories = [entry["factory"] for entry in sim.element_chain]
        assert factories == ["capsfilter", "identity"]
        assert "emltriton" not in factories
        identity = sim.element_chain[1]
        assert identity["args_template"]["name"] == "{sim_inference_name}"
        assert "emltriton" not in sim.plugin_dependencies


# --------------------------------------------------------------------------
# Bedrock inference node type (reference-comparison via Bedrock runtime)
# --------------------------------------------------------------------------

class TestBedrockInferenceNodeType:
    def test_bedrock_inference_present_with_two_video_inputs(self):
        descriptor = get_node_type("bedrock_inference")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_INFERENCE
        assert descriptor.display_name == "Bedrock Inference"
        # Two VideoFrames inputs: the frame under inspection and the
        # reference image; one InferenceMeta output.
        assert [(port.name, port.port_type) for port in descriptor.inputs] == [
            ("in", PORT_TYPE_VIDEO_FRAMES),
            ("reference", PORT_TYPE_VIDEO_FRAMES),
        ]
        assert _port_types(descriptor.outputs) == [PORT_TYPE_INFERENCE_META]

    def test_bedrock_inference_parameterization(self):
        params = _params_by_name(get_node_type("bedrock_inference"))
        assert params["model"].param_type == "enum"
        assert set(params["model"].constraints["values"]) == {
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "qwen.qwen3-vl-235b-a22b",
            "moonshotai.kimi-k2.5",
        }
        assert params["model"].default == "us.amazon.nova-lite-v1:0"
        assert params["prompt"].required is True
        assert params["prompt"].param_type == "string"
        assert '"is_anomalous"' in params["prompt"].default
        assert '"confidence"' in params["prompt"].default
        assert params["region"].required is False
        assert params["region"].default == "us-east-1"
        assert params["max_tokens"].param_type == "int"
        assert params["max_tokens"].default == 256

    def test_bedrock_inference_is_an_executor_binding_on_device_archs(self):
        # Executor-level realization on every physical device
        # architecture: no GStreamer elements of its own; the compiler
        # terminates the input branches in synthetic capture sinks and
        # the binding carries the parameters + capture paths.
        descriptor = get_node_type("bedrock_inference")
        assert descriptor.hardware_dependent is True
        device_mappings = [m for m in descriptor.mappings if m.arch != "sim"]
        assert sorted(m.arch for m in device_mappings) == sorted(
            ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6"])
        for mapping in device_mappings:
            assert mapping.element_chain == []
            assert mapping.executor_binding == "bedrock_inference"
            # The capture sink chain (videoconvert ! jpegenc !
            # multifilesink) and the boto3 runtime client.
            assert set(mapping.plugin_dependencies) == {
                "videoconvertscale", "jpeg", "multifile", "python:boto3"}

    def test_bedrock_inference_sim_mapping_matches_the_model_inference_stub(self):
        # Simulation stubs the node exactly like model_inference: the
        # sandbox VPC has no internet, so the model is never invoked and
        # the harness injects the configured simulated outcome via the
        # sim_inference_<nodeId> identity (Requirement 12.6).
        sim = get_node_type("bedrock_inference").mapping_for("sim")
        assert sim == get_node_type("model_inference").mapping_for("sim")


# --------------------------------------------------------------------------
# Requirement 2.4: post-processing node types
# --------------------------------------------------------------------------

class TestPostProcessingNodeTypes:
    def test_required_post_processing_types_present(self):
        for type_id in ("custom_python", "inference_filter", "conditional"):
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.category == CATEGORY_POST_PROCESSING

    def test_custom_python_parameterization(self):
        descriptor = get_node_type("custom_python")
        params = _params_by_name(descriptor)
        # User-supplied code plus declared input/output port types
        # (Requirement 2.7 shape).
        assert params["code"].required is True
        assert params["code"].param_type == "code"
        all_port_types = {PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_INFERENCE_META,
                          PORT_TYPE_EVENT_SIGNAL}
        for name in ("input_port_type", "output_port_type"):
            assert params[name].required is True
            assert params[name].param_type == "enum"
            assert set(params[name].constraints["values"]) == all_port_types

    def test_inference_filter_parameterization(self):
        descriptor = get_node_type("inference_filter")
        # Filters inference results: InferenceMeta in and out.
        assert _port_types(descriptor.inputs) == [PORT_TYPE_INFERENCE_META]
        assert _port_types(descriptor.outputs) == [PORT_TYPE_INFERENCE_META]
        params = _params_by_name(descriptor)
        # Configurable condition over inference metadata.
        assert params["condition"].required is True
        assert params["condition"].param_type == "string"

    def test_conditional_parameterization(self):
        descriptor = get_node_type("conditional")
        assert descriptor.display_name == "Conditional"
        # Two-path routing: InferenceMeta in, two InferenceMeta outputs.
        # The "true" output receives the metadata when the condition
        # holds, the "false" output when it does not.
        assert _port_types(descriptor.inputs) == [PORT_TYPE_INFERENCE_META]
        assert [(port.name, port.port_type) for port in descriptor.outputs] == [
            ("true", PORT_TYPE_INFERENCE_META),
            ("false", PORT_TYPE_INFERENCE_META),
        ]
        params = _params_by_name(descriptor)
        # One required condition, same grammar as inference_filter.
        assert list(params) == ["condition"]
        assert params["condition"].required is True
        assert params["condition"].param_type == "string"
        assert params["condition"].constraints == {"min_length": 1}
        assert descriptor.hardware_dependent is False

    def test_conditional_maps_to_the_conditional_executor_binding_on_all_archs(self):
        # Mechanically like inference_filter: an executor-level binding
        # (no GStreamer elements), identical on every architecture.
        descriptor = get_node_type("conditional")
        assert len(descriptor.mappings) == len(
            get_node_type("inference_filter").mappings)
        for mapping in descriptor.mappings:
            assert mapping.element_chain == []
            assert mapping.executor_binding == "conditional"
            assert mapping.plugin_dependencies == []


# --------------------------------------------------------------------------
# Requirement 2.5: output node types
# --------------------------------------------------------------------------

class TestOutputNodeTypes:
    def test_required_output_types_present(self):
        for type_id in ("digital_output", "mqtt_publish", "opcua_write", "capture"):
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.category == CATEGORY_OUTPUT
            # Output nodes are sinks: at least one input, no output ports.
            assert len(descriptor.inputs) >= 1
            assert descriptor.outputs == []

    def test_digital_output_parameterization(self):
        descriptor = get_node_type("digital_output")
        params = _params_by_name(descriptor)
        assert params["pin"].required is True
        assert params["pin"].constraints == {"min": 0, "max": 255}
        assert params["signal_type"].required is True
        assert set(params["signal_type"].constraints["values"]) == {
            "high", "low", "pulse"}
        assert params["pulse_width_ms"].param_type == "int"
        assert params["pulse_width_ms"].default == 100
        assert params["condition"].required is True
        assert descriptor.hardware_dependent is True

    def test_mqtt_publish_parameterization(self):
        descriptor = get_node_type("mqtt_publish")
        params = _params_by_name(descriptor)
        assert params["broker_host"].required is True
        assert params["topic"].required is True
        assert params["broker_port"].param_type == "int"
        assert params["broker_port"].default == 1883
        assert params["broker_port"].constraints == {"min": 1, "max": 65535}
        assert set(params["qos"].constraints["values"]) == {0, 1, 2}
        assert descriptor.hardware_dependent is True

    def test_mqtt_publish_aws_iot_parameterization(self):
        # AWS IoT Core support: an opt-in checkbox plus the thing name
        # and device-local certificate file paths, all optional and
        # shown only while aws_iot is enabled (depends_on).
        descriptor = get_node_type("mqtt_publish")
        params = _params_by_name(descriptor)
        assert params["aws_iot"].param_type == "bool"
        assert params["aws_iot"].required is False
        assert params["aws_iot"].default is False
        assert params["aws_iot"].depends_on is None
        for name in ("iot_thing_name", "iot_ca_cert_path",
                     "iot_client_cert_path", "iot_private_key_path"):
            assert params[name].param_type == "string", name
            assert params[name].required is False, name
            assert params[name].default is None, name
            assert params[name].constraints == {"min_length": 1}, name
            assert params[name].depends_on == "aws_iot", name

    def test_opcua_write_parameterization(self):
        descriptor = get_node_type("opcua_write")
        params = _params_by_name(descriptor)
        assert params["endpoint"].required is True
        assert params["endpoint"].constraints.get("regex") == r"^opc\.tcp://.+"
        assert params["node_id"].required is True
        assert descriptor.hardware_dependent is True

    def test_capture_parameterization(self):
        descriptor = get_node_type("capture")
        # Captures inference results to the device file system.
        params = _params_by_name(descriptor)
        assert params["output_path"].required is True
        assert params["output_path"].param_type == "string"
        assert params["quality"].param_type == "int"
        assert params["quality"].constraints == {"min": 1, "max": 100}
        assert params["interval"].param_type == "int"
        assert descriptor.hardware_dependent is False


# --------------------------------------------------------------------------
# Cross-cutting catalog content checks
# --------------------------------------------------------------------------

class TestCatalogCoverage:
    EXPECTED_TYPE_IDS = {
        "camera_source", "folder_source", "digital_input",
        "dewarp", "rotate", "crop", "format_convert",
        "model_inference", "bedrock_inference",
        "custom_python", "inference_filter", "conditional",
        "digital_output", "mqtt_publish", "opcua_write", "capture",
    }

    def test_catalog_contains_exactly_the_expected_types(self):
        assert {d.type_id for d in NODE_CATALOG} == self.EXPECTED_TYPE_IDS

    def test_every_palette_section_is_populated(self):
        grouped = nodes_by_category()
        assert set(grouped) == {
            CATEGORY_INPUT, CATEGORY_PREPROCESSING, CATEGORY_INFERENCE,
            CATEGORY_POST_PROCESSING, CATEGORY_OUTPUT,
        }
        for category, descriptors in grouped.items():
            assert descriptors, category

    def test_every_node_type_has_a_display_name(self):
        for descriptor in NODE_CATALOG:
            assert descriptor.display_name.strip()

    def test_every_parameter_has_a_description(self):
        # Field-level help: every parameter of every node type documents
        # what it is (rendered by the configuration panel under the label).
        for descriptor in NODE_CATALOG:
            for parameter in descriptor.parameters:
                assert isinstance(parameter.description, str) and \
                    parameter.description.strip(), (
                        "{0}.{1} has no description".format(
                            descriptor.type_id, parameter.name))

    def test_every_parameter_has_at_least_one_working_example(self):
        # Field-level help: every parameter carries at least one example
        # value that satisfies the parameter's own type and constraints
        # (usable verbatim).
        from workflow_core.validator import check_parameter_value

        for descriptor in NODE_CATALOG:
            for parameter in descriptor.parameters:
                context = "{0}.{1}".format(descriptor.type_id, parameter.name)
                assert isinstance(parameter.examples, list) and \
                    parameter.examples, (
                        "{0} has no examples".format(context))
                for example in parameter.examples:
                    violation = check_parameter_value(parameter, example)
                    assert violation is None, (
                        "{0} example {1!r} is not a valid value: {2}".format(
                            context, example, violation))

    def test_condition_descriptions_document_the_expression_language(self):
        # The condition rule dialect (fields, operators, worked example)
        # is documented on every executor-evaluated condition parameter,
        # exactly as the shared evaluator supports it.
        for type_id in ("inference_filter", "conditional", "digital_output"):
            descriptor = get_node_type(type_id)
            condition = next(p for p in descriptor.parameters
                             if p.name == "condition")
            for token in ("is_anomalous", "confidence", "==", "!=", ">=",
                          "<=", "&&", "||",
                          "is_anomalous == true && confidence >= 0.8"):
                assert token in condition.description, (type_id, token)

    def test_condition_parameters_carry_working_examples(self):
        # Every executor-evaluated condition parameter ships 3+ example
        # expressions in the documented grammar, over exactly the
        # metadata fields the shared evaluator resolves (is_anomalous,
        # confidence) — never fields the evaluator does not support.
        import re

        for type_id in ("inference_filter", "conditional", "digital_output"):
            descriptor = get_node_type(type_id)
            condition = next(p for p in descriptor.parameters
                             if p.name == "condition")
            examples = condition.examples
            assert isinstance(examples, list) and len(examples) >= 3, type_id
            assert "is_anomalous == true" in examples, type_id
            for example in examples:
                identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                             example)) - {"true", "false"}
                assert identifiers <= {"is_anomalous", "confidence"}, (
                    type_id, example)
