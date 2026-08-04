"""Unit tests for catalog content (task 1.4).

Asserts presence and parameterization of all required input,
preprocessing, inference, post-processing, and output node types.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5

Aravis camera input additions (aravis-camera-input task 1.3):
descriptor identity, parameters, mappings, and the catalog
mirror-equality check.

Validates: aravis-camera-input Requirements 1.1, 1.2, 1.3, 1.4, 1.6, 7.4
"""

import os

from workflow_core.catalog import (
    ARCHITECTURES,
    DEVICE_ARCHITECTURES,
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
        for type_id in ("csi_camera_source", "icam_source", "folder_source",
                        "digital_input"):
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.category == CATEGORY_INPUT
            # Input nodes originate data: no input ports, at least one output.
            assert descriptor.inputs == []
            assert len(descriptor.outputs) >= 1

    def test_csi_camera_source_parameterization(self):
        # csi-icam-input-nodes Requirements 1.1, 1.2: gain/exposure only,
        # no device path.
        descriptor = get_node_type("csi_camera_source")
        assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
        params = _params_by_name(descriptor)
        assert list(params) == ["gain", "exposure"]
        assert "device" not in params
        assert params["gain"].param_type == "int"
        assert params["gain"].required is False
        assert params["gain"].default == 4
        assert params["gain"].constraints == {"min": 0, "max": 100}
        assert params["exposure"].param_type == "int"
        assert params["exposure"].required is False
        assert params["exposure"].default == 5000000
        assert params["exposure"].constraints == {"min": 0}
        assert descriptor.hardware_dependent is True

    def test_icam_source_parameterization(self):
        # csi-icam-input-nodes Requirements 2.1, 2.2: required device path.
        descriptor = get_node_type("icam_source")
        assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
        params = _params_by_name(descriptor)
        assert list(params) == ["device"]
        assert params["device"].param_type == "string"
        assert params["device"].required is True
        assert params["device"].default == "/dev/video0"
        assert params["device"].constraints == {"min_length": 1}
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
        # The sim mapping is the same dataset-fed recording stub the
        # icam_source uses: multifilesrc on the {dataset_location}
        # placeholder with stock decode elements — never the device
        # filesrc/emexifextract chain (Requirement 12.6).
        folder = get_node_type("folder_source").mapping_for("sim")
        camera = get_node_type("icam_source").mapping_for("sim")
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
# Aravis camera input node type
# (aravis-camera-input Requirements 1.1-1.4, 1.6, 7.4)
# --------------------------------------------------------------------------

class TestAravisCameraSourceNodeType:
    def test_descriptor_identity(self):
        # Requirement 1.1: type id, category, display name, no inputs,
        # exactly one VideoFrames output port named "out".
        descriptor = get_node_type("aravis_camera_source")
        assert descriptor is not None
        assert descriptor.type_id == "aravis_camera_source"
        assert descriptor.category == CATEGORY_INPUT
        assert descriptor.display_name == "Aravis Camera Source"
        assert descriptor.inputs == []
        assert [(port.name, port.port_type) for port in descriptor.outputs] == [
            ("out", PORT_TYPE_VIDEO_FRAMES)]

    def test_camera_id_parameterization(self):
        # Requirement 1.2: required string camera_id, min_length 1,
        # documented with at least one working example.
        params = _params_by_name(get_node_type("aravis_camera_source"))
        assert list(params) == ["camera_id", "gain", "exposure"]
        camera_id = params["camera_id"]
        assert camera_id.required is True
        assert camera_id.param_type == "string"
        assert camera_id.default is None
        assert camera_id.constraints == {"min_length": 1}
        assert camera_id.description.strip()
        assert isinstance(camera_id.examples, list) and camera_id.examples

    def test_gain_and_exposure_parameterization(self):
        # Requirement 1.3: optional acquisition settings mirroring what
        # the Camera_Manager applies, each documented with examples.
        params = _params_by_name(get_node_type("aravis_camera_source"))
        gain = params["gain"]
        assert gain.required is False
        assert gain.param_type == "int"
        assert gain.default == 4
        assert gain.constraints == {"min": 0, "max": 100}
        assert gain.description.strip()
        assert isinstance(gain.examples, list) and gain.examples
        exposure = params["exposure"]
        assert exposure.required is False
        assert exposure.param_type == "int"
        assert exposure.default == 5000000
        assert exposure.constraints == {"min": 0}
        assert exposure.description.strip()
        assert isinstance(exposure.examples, list) and exposure.examples

    def test_hardware_dependent(self):
        # Requirement 1.4: the node is hardware dependent.
        assert get_node_type("aravis_camera_source").hardware_dependent is True

    def test_device_arch_mappings_are_the_appsrc_chain(self):
        # Requirement 1.4: every physical device architecture renders
        # appsrc name=appsrc_{nodeId} ! videoconvert with the app and
        # videoconvertscale plugin dependencies. The appsrc name embeds
        # the {nodeId} token so multi-camera documents stay addressable.
        descriptor = get_node_type("aravis_camera_source")
        assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
        for arch in DEVICE_ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            assert mapping is not None, arch
            assert mapping.element_chain == [
                {"factory": "appsrc",
                 "args_template": {"name": "appsrc_{nodeId}"}},
                {"factory": "videoconvert", "args_template": {}},
            ], arch
            assert mapping.plugin_dependencies == [
                "app", "videoconvertscale"], arch
            assert mapping.executor_binding is None, arch

    def test_sim_mapping_is_the_dataset_fed_stub(self):
        # Requirement 1.4: the sim mapping is the shared dataset-fed
        # stub, byte-equal to icam_source's sim mapping.
        aravis_sim = get_node_type("aravis_camera_source").mapping_for("sim")
        camera_sim = get_node_type("icam_source").mapping_for("sim")
        assert aravis_sim == camera_sim


class TestCameraSourceRemoved:
    """The generic camera_source node type is removed outright
    (csi-icam-input-nodes Requirements 3.1, 3.4)."""

    def test_camera_source_is_gone(self):
        assert get_node_type("camera_source") is None
        assert "camera_source" not in {d.type_id for d in NODE_CATALOG}


class TestCsiCameraSourceNodeType:
    """Pin the CSI_Camera_Source_Node descriptor
    (csi-icam-input-nodes Requirements 1.1, 1.2, 1.3, 1.4)."""

    def test_identity_and_parameters(self):
        descriptor = get_node_type("csi_camera_source")
        assert descriptor.type_id == "csi_camera_source"
        assert descriptor.category == CATEGORY_INPUT
        assert descriptor.display_name == "CSI Camera Input"
        assert descriptor.inputs == []
        assert [(port.name, port.port_type) for port in descriptor.outputs] == [
            ("out", PORT_TYPE_VIDEO_FRAMES)]
        assert descriptor.hardware_dependent is True
        params = _params_by_name(descriptor)
        # gain/exposure only — no device path (Requirement 1.2).
        assert list(params) == ["gain", "exposure"]

    def test_mappings_read_the_csi_capture_file(self):
        descriptor = get_node_type("csi_camera_source")
        assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
        # Non-JP6 physical archs: the standard JPEG file chain reading
        # the staged capture frame (Requirement 1.3).
        for arch in ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5"):
            mapping = descriptor.mapping_for(arch)
            factories = [entry["factory"] for entry in mapping.element_chain]
            assert factories == ["filesrc", "emexifextract", "jpegparse",
                                 "jpegdec", "videoconvert", "videoflip"], arch
            assert mapping.element_chain[0]["args_template"]["location"] == (
                "/aws_dda/nvidia-csi-capture/latest.jpg"), arch
        # JP6: the PNG-staged host-service capture path (Requirement 1.3).
        jp6 = descriptor.mapping_for("arm64_jp6")
        assert [entry["factory"] for entry in jp6.element_chain] == [
            "filesrc", "pngdec", "videoconvert"]
        assert jp6.element_chain[0]["args_template"]["location"] == (
            "/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png")
        assert jp6.plugin_dependencies == [
            "coreelements", "png", "videoconvertscale", "python:pillow"]

    def test_no_parameter_appears_in_any_element_argument(self):
        # Requirement 1.4: CSI params never land in an element template,
        # so the node compiles with no binding slots.
        descriptor = get_node_type("csi_camera_source")
        param_names = {p.name for p in descriptor.parameters}
        for mapping in descriptor.mappings:
            for element in mapping.element_chain:
                for value in (element.get("args_template") or {}).values():
                    if isinstance(value, str):
                        assert value.strip("{}") not in param_names


class TestIcamSourceNodeType:
    """Pin the ICAM_Source_Node descriptor
    (csi-icam-input-nodes Requirements 2.1, 2.2, 2.3, 2.4)."""

    def test_identity_and_parameters(self):
        descriptor = get_node_type("icam_source")
        assert descriptor.type_id == "icam_source"
        assert descriptor.category == CATEGORY_INPUT
        assert descriptor.display_name == "ICAM"
        assert descriptor.inputs == []
        assert [(port.name, port.port_type) for port in descriptor.outputs] == [
            ("out", PORT_TYPE_VIDEO_FRAMES)]
        assert descriptor.hardware_dependent is True
        params = _params_by_name(descriptor)
        assert list(params) == ["device"]
        assert params["device"].required is True
        assert params["device"].default == "/dev/video0"
        assert params["device"].constraints == {"min_length": 1}

    def test_device_arch_mappings_are_the_v4l2src_chain(self):
        descriptor = get_node_type("icam_source")
        assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
        # Every physical device architecture: v4l2src device={device} !
        # videoconvert (Requirement 2.3), with exactly one {device}
        # placeholder (Requirement 2.4).
        for arch in DEVICE_ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            assert mapping.element_chain == [
                {"factory": "v4l2src", "args_template": {"device": "{device}"}},
                {"factory": "videoconvert", "args_template": {}},
            ], arch
            assert mapping.plugin_dependencies == [
                "video4linux2", "videoconvertscale"], arch


class TestCatalogMirrorEquality:
    def test_portal_and_vendor_catalog_nodes_are_byte_identical(self):
        # Requirement 1.6: the portal layer catalog and the edge vendor
        # mirror carry the node identically — the two nodes.py sources
        # stay byte-identical.
        import workflow_core.catalog.nodes as portal_nodes

        portal_path = os.path.abspath(portal_nodes.__file__)
        if portal_path.endswith(".pyc"):
            portal_path = portal_path[:-1]
        # tests/ -> workflow_core -> layers -> backend -> edge-cv-portal
        # -> repository root
        repo_root = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", ".."))
        vendor_path = os.path.join(
            repo_root, "src", "backend", "workflow_engine", "vendor",
            "workflow_core", "catalog", "nodes.py")
        assert os.path.isfile(vendor_path), vendor_path
        with open(portal_path, "rb") as handle:
            portal_bytes = handle.read()
        with open(vendor_path, "rb") as handle:
            vendor_bytes = handle.read()
        assert portal_bytes == vendor_bytes, (
            "portal layer and edge vendor catalog nodes.py have diverged")


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
# Custom Python preprocessing node type
# (custom-python-frames Requirements 1.1-1.5)
# --------------------------------------------------------------------------

class TestCustomPythonPreprocessNodeType:
    def test_descriptor_present_in_preprocessing_category(self):
        descriptor = get_node_type("custom_python_preprocess")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_PREPROCESSING
        assert descriptor.display_name == "Custom Python (Frames)"
        assert descriptor.hardware_dependent is False

    def test_fixed_video_frames_ports_without_type_overrides(self):
        # Exactly one VideoFrames input and one VideoFrames output; the
        # per-instance port type override parameters of custom_python
        # must not exist here (Requirement 1.2).
        descriptor = get_node_type("custom_python_preprocess")
        assert _port_types(descriptor.inputs) == [PORT_TYPE_VIDEO_FRAMES]
        assert _port_types(descriptor.outputs) == [PORT_TYPE_VIDEO_FRAMES]
        params = _params_by_name(descriptor)
        assert "input_port_type" not in params
        assert "output_port_type" not in params

    def test_parameterization(self):
        params = _params_by_name(get_node_type("custom_python_preprocess"))
        assert params["code"].required is True
        assert params["code"].param_type == "code"
        assert params["requirements"].required is False
        assert params["requirements"].param_type == "string"

    def test_mappings_identical_to_custom_python(self):
        # Same emlpython element chain and plugin dependencies as the
        # custom_python post-processing node on every architecture
        # (Requirement 1.4).
        preprocess = get_node_type("custom_python_preprocess")
        post = get_node_type("custom_python")
        assert {m.arch for m in preprocess.mappings} == set(ARCHITECTURES)
        for arch in ARCHITECTURES:
            pre_mapping = preprocess.mapping_for(arch)
            post_mapping = post.mapping_for(arch)
            assert post_mapping is not None, arch
            assert pre_mapping.element_chain == post_mapping.element_chain, arch
            assert pre_mapping.plugin_dependencies == \
                post_mapping.plugin_dependencies, arch


# --------------------------------------------------------------------------
# Custom Python contract documentation
# (custom-python-frames Requirements 1.5, 8.1, 8.2)
# --------------------------------------------------------------------------

class TestCustomPythonContractDocumentation:
    def test_preprocess_code_description_documents_process_frame(self):
        code = _params_by_name(
            get_node_type("custom_python_preprocess"))["code"]
        assert "process_frame" in code.description
        assert "process(data, metadata)" not in code.description

    def test_custom_python_code_description_documents_actual_entry_points(self):
        # The actual runtime entry points (process_frame and handle),
        # never the non-existent process(data, metadata) contract
        # (Requirement 8.1).
        code = _params_by_name(get_node_type("custom_python"))["code"]
        assert "process_frame" in code.description
        assert "handle" in code.description
        assert "process(data, metadata)" not in code.description

    def test_every_code_example_defines_a_runtime_entry_point(self):
        # Each code example exec's to a module defining a callable
        # process_frame or handle, so it is a valid handler under the
        # runtime contract (Requirements 1.5, 8.2).
        for type_id in ("custom_python", "custom_python_preprocess"):
            code = _params_by_name(get_node_type(type_id))["code"]
            assert isinstance(code.examples, list) and code.examples, type_id
            for example in code.examples:
                namespace = {}
                exec(compile(example, "<example>", "exec"), namespace)
                entry = namespace.get("process_frame") or namespace.get("handle")
                assert callable(entry), (type_id, example)


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
        # The default prompt carries the comparison semantics only: the
        # executor auto-appends the canonical JSON-format instruction in
        # anomaly mode, so the prompt no longer spells out the answer
        # shape (bedrock-response-mode Requirement 3.3).
        assert "meaningfully differs" in params["prompt"].default
        assert '"is_anomalous"' not in params["prompt"].default
        # The executor-appended JSON instruction is documented on the
        # prompt parameter instead (Requirement 3.3).
        assert "appends" in params["prompt"].description
        assert '"is_anomalous"' in params["prompt"].description
        # Response-mode toggle: bool checkbox, default True (anomaly
        # mode), description covering both modes (Requirement 3.1).
        assert params["anomaly_mode"].param_type == "bool"
        assert params["anomaly_mode"].required is False
        assert params["anomaly_mode"].default is True
        assert "anomaly" in params["anomaly_mode"].description
        assert "bedrock_text" in params["anomaly_mode"].description
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
        # Bug 2 (workflow-manager-integration-bugfixes, Requirement 2.2):
        # broker_host is no longer statically required so the topic-only
        # Greengrass path is not force-failed by the missing-required check;
        # the additive, off-by-default greengrass option is what enables that
        # path. A publish target is instead enforced at validation time (V6).
        assert params["broker_host"].required is False
        assert params["greengrass"].param_type == "bool"
        assert params["greengrass"].required is False
        assert params["greengrass"].default is False
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
        "csi_camera_source", "icam_source", "aravis_camera_source",
        "folder_source",
        "digital_input",
        "dewarp", "rotate", "crop", "format_convert",
        "custom_python_preprocess",
        "model_inference", "bedrock_inference", "llm_inference",
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
