"""The initial node type catalog (Requirements 2.1-2.5, 2.8).

The catalog is data, not code: a list of NodeTypeDescriptor records.
GStreamer factories and argument shapes mirror the existing LocalServer
builder (src/backend/gstreamer/pipeline_builder.py) so compiled pipelines
run through the same element dialect GstPipelineManager already executes.

Argument templates use ``{placeholder}`` tokens resolved at compile time:
  - node parameter names (e.g. ``{device}``, ``{modelName}``)
  - compile context values (``{triton_model_repo}``, ``{triton_server_path}``,
    ``{dio_script_path}``, ``{dio_config_json}``, ``{python_handler_path}``,
    ``{dataset_location}``, ``{capture_meta}``)
"""

from __future__ import annotations

from dataclasses import replace

from .models import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_SIM,
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
    ARCHITECTURES,
    DEVICE_ARCHITECTURES,
    JP5_VLLM_ENABLED,
    SIM_RECORDING_BINDING_PREFIX,
    CATEGORY_INFERENCE,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_POST_PROCESSING,
    CATEGORY_PREPROCESSING,
    CATEGORY_TRIGGER,
    GstMapping,
    NodeTypeDescriptor,
    ParameterDescriptor,
    PortDescriptor,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
)


def _element(factory, **args_template):
    return {"factory": factory, "args_template": dict(args_template)}


#: The rule-expression language shared by every executor-evaluated
#: ``condition`` parameter (inference_filter, conditional,
#: digital_output). This documents exactly what the shared condition
#: evaluator (LocalServer workflow engine and the test sandbox mirror)
#: supports — keep in sync with ``output_bindings.evaluate_condition``.
CONDITION_LANGUAGE_DESCRIPTION = (
    "Rule expression over the inference metadata fields is_anomalous "
    "(true/false; for object-detection models true when at least one "
    "object was detected) and confidence (number; the model's "
    "confidence score, for object detection the top detection's "
    "confidence). Supports the comparisons ==, !=, >=, <=, >, <, the "
    "logic operators && (and), || (or), ! (not), and parentheses; "
    "values are numbers (e.g. 0.8), 'quoted strings', or true/false; "
    "a bare field name is tested for truth. "
    "E.g. is_anomalous == true && confidence >= 0.8"
)

#: Working example expressions for every ``condition`` parameter, using
#: only the metadata fields the shared evaluator resolves (is_anomalous,
#: confidence). For object-detection (e.g. YOLO) models, is_anomalous is
#: true when at least one object was detected and confidence is the top
#: detection's confidence, so the same expressions cover detected /
#: not-detected routing.
CONDITION_EXAMPLES = (
    "is_anomalous == true",
    "confidence >= 0.8 && is_anomalous == true",
    "is_anomalous == true || confidence < 0.5",
    "!(is_anomalous == true)",
)


def _same_on_all_archs(element_chain=None, executor_binding=None, plugin_dependencies=None):
    """One identical GstMapping per architecture."""
    return [
        GstMapping(
            arch=arch,
            element_chain=list(element_chain or []),
            executor_binding=executor_binding,
            plugin_dependencies=list(plugin_dependencies or []),
        )
        for arch in ARCHITECTURES
    ]


def _same_on_device_archs(element_chain=None, executor_binding=None, plugin_dependencies=None):
    """One identical GstMapping per physical device architecture.

    Used by hardware-dependent node types whose ``sim`` mapping is a
    recording stub rather than the device realization (Requirement 12.6).
    """
    return [
        GstMapping(
            arch=arch,
            element_chain=list(element_chain or []),
            executor_binding=executor_binding,
            plugin_dependencies=list(plugin_dependencies or []),
        )
        for arch in DEVICE_ARCHITECTURES
    ]


def _recording_binding(node_type_id):
    """The sim-architecture recording stub for a hardware output node:
    an executor binding that records would-be actuations to the test
    run's recording log instead of any endpoint (Requirement 12.6)."""
    return GstMapping(
        arch=ARCH_SIM,
        executor_binding=SIM_RECORDING_BINDING_PREFIX + node_type_id,
    )


def _dataset_fed_sim_source():
    """The sim-architecture stub for a hardware frame source: fed from
    the Test_Dataset via ``multifilesrc location={dataset_location}``
    (resolved by the test harness), decoding with stock GStreamer
    elements only — no device paths, no DDA elements (Requirement 12.6).
    Shared by csi_camera_source, icam_source and folder_source."""
    return GstMapping(
        arch=ARCH_SIM,
        element_chain=[
            _element("multifilesrc", location="{dataset_location}"),
            _element("jpegparse"),
            _element("jpegdec", **{"idct-method": 2}),
            _element("videoconvert"),
        ],
        plugin_dependencies=["multifile", "jpeg", "videoconvertscale"],
    )


# --------------------------------------------------------------------------
# Shared element chains
# --------------------------------------------------------------------------

# Standard JPEG file decode chain used by the existing builder
# (filesrc ! emexifextract ! jpegparse ! jpegdec ! videoconvert ! videoflip).
def _jpeg_file_chain(location_template):
    return [
        _element("filesrc", blocksize=-1, location=location_template),
        _element("emexifextract"),
        _element("jpegparse"),
        _element("jpegdec", **{"idct-method": 2}),
        _element("videoconvert"),
        _element("videoflip", method="automatic"),
    ]


# JetPack 6 avoids GStreamer's libjpeg-based jpegdec (libdlr.so libjpeg
# collision); the executor stages a Pillow-decoded PNG and reads it via
# pngdec, matching pipeline_builder._add_file_image_source's JP6 path.
def _jp6_png_staged_chain(location_template):
    return [
        _element("filesrc", blocksize=-1, location=location_template),
        _element("pngdec"),
        _element("videoconvert"),
    ]


# --------------------------------------------------------------------------
# Input node types (Requirement 2.1)
# --------------------------------------------------------------------------

CSI_CAMERA_SOURCE = NodeTypeDescriptor(
    type_id="csi_camera_source",
    category=CATEGORY_INPUT,
    display_name="CSI Camera Input",
    # Optional inert activation scaffolding port (Requirement 7): edges
    # into it are dropped at compile time; no activation binding exists.
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("gain", "int", required=False, default=4,
                            constraints={"min": 0, "max": 100},
                            description="NVIDIA CSI sensor gain (0-100) applied "
                                        "through the CSI capture service; e.g. 4.",
                            examples=[4, 10]),
        ParameterDescriptor("exposure", "int", required=False, default=5000000,
                            constraints={"min": 0},
                            description="NVIDIA CSI sensor exposure time in "
                                        "nanoseconds, e.g. 5000000 (5 ms).",
                            examples=[5000000, 16000000]),
    ],
    # The NVIDIA CSI host capture service (nvidia-csi-capture.service)
    # continuously stages frames to /aws_dda/nvidia-csi-capture/latest.jpg
    # and reads gain/exposure from config.json; the compiled chain reads
    # that staged capture file. gain/exposure never enter an element
    # argument (no binding slots) — the csiSensorBinding marker selects the
    # sensor. JP6 avoids the libjpeg jpegdec collision via the PNG-staged
    # decode path (see _jp6_png_staged_chain). Simulation: fed from the
    # Test_Dataset (Requirement 12.6).
    mappings=[
        GstMapping(arch=ARCH_X86_64, element_chain=_jpeg_file_chain("/aws_dda/nvidia-csi-capture/latest.jpg"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_X86_64_NVIDIA, element_chain=_jpeg_file_chain("/aws_dda/nvidia-csi-capture/latest.jpg"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_ARM64_JP4, element_chain=_jpeg_file_chain("/aws_dda/nvidia-csi-capture/latest.jpg"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_ARM64_JP5, element_chain=_jpeg_file_chain("/aws_dda/nvidia-csi-capture/latest.jpg"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_ARM64_JP6, element_chain=_jp6_png_staged_chain("/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png"),
                   plugin_dependencies=["coreelements", "png", "videoconvertscale", "python:pillow"]),
        _dataset_fed_sim_source(),
    ],
    hardware_dependent=True,
)

ICAM_SOURCE = NodeTypeDescriptor(
    type_id="icam_source",
    category=CATEGORY_INPUT,
    display_name="ICAM",
    # Optional inert activation scaffolding port (Requirement 7): edges
    # into it are dropped at compile time; no activation binding exists.
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("device", "string", required=True, default="/dev/video0",
                            constraints={"min_length": 1},
                            description="V4L2 device path of the smart (ICAM) "
                                        "camera on the edge device, e.g. /dev/video0.",
                            examples=["/dev/video0", "/dev/video1"]),
    ],
    # V4L2 smart camera captured directly (v4l2src device={device} !
    # videoconvert), the same element the edge uses uniformly on x86 and
    # Jetson (pipeline_builder._add_icam_image_source). The {device}
    # placeholder yields exactly one binding slot. Simulation: fed from
    # the Test_Dataset (Requirement 12.6).
    mappings=_same_on_device_archs(
        element_chain=[
            _element("v4l2src", device="{device}"),
            _element("videoconvert"),
        ],
        plugin_dependencies=["video4linux2", "videoconvertscale"],
    ) + [_dataset_fed_sim_source()],
    hardware_dependent=True,
)

ARAVIS_CAMERA_SOURCE = NodeTypeDescriptor(
    type_id="aravis_camera_source",
    category=CATEGORY_INPUT,
    display_name="Aravis Camera Source",
    # Optional inert activation scaffolding port (Requirement 7): edges
    # into it are dropped at compile time; no activation binding exists.
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("camera_id", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Aravis (GenICam) camera identifier "
                                        "as enumerated on the edge device, "
                                        "e.g. Aravis-Fake-GV01 or "
                                        "Basler-12345678.",
                            examples=["Aravis-Fake-GV01", "Basler-12345678"]),
        ParameterDescriptor("gain", "int", required=False, default=4,
                            constraints={"min": 0, "max": 100},
                            description="Sensor gain (0-100) applied through "
                                        "the camera manager. Higher values "
                                        "brighten the image but add noise; "
                                        "e.g. 4.",
                            examples=[4, 10]),
        ParameterDescriptor("exposure", "int", required=False, default=5000000,
                            constraints={"min": 0},
                            description="Sensor exposure time in nanoseconds "
                                        "applied through the camera manager, "
                                        "e.g. 5000000 (5 ms).",
                            examples=[5000000, 16000000]),
    ],
    # Aravis acquisition happens in the LocalServer process through the
    # camera manager (no aravissrc element ships in the DDA images):
    # every physical architecture compiles an appsrc-headed chain the
    # executor feeds a camera-manager-grabbed frame into, the classic
    # Camera-type Frame_Feed model. The appsrc name is compile-time
    # rendered per node ({nodeId} derived by the compiler) so
    # multi-camera documents stay addressable. Simulation: fed from the
    # Test_Dataset like camera_source (Requirement 12.6).
    mappings=_same_on_device_archs(
        element_chain=[
            _element("appsrc", name="appsrc_{nodeId}"),
            _element("videoconvert"),
        ],
        plugin_dependencies=["app", "videoconvertscale"],
    ) + [_dataset_fed_sim_source()],
    hardware_dependent=True,
)

FOLDER_SOURCE = NodeTypeDescriptor(
    type_id="folder_source",
    category=CATEGORY_INPUT,
    display_name="Folder Source",
    # Optional inert activation scaffolding port (Requirement 7): edges
    # into it are dropped at compile time; no activation binding exists.
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("location", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Path of the image file (or folder "
                                        "of files) to read on the device, "
                                        "e.g. /aws_dda/images/latest.jpg.",
                            examples=["/aws_dda/images/latest.jpg",
                                      "/aws_dda/captures"]),
        ParameterDescriptor("file_pattern", "string", required=False, default="*.jpg",
                            constraints={"min_length": 1},
                            description="Glob pattern selecting which files "
                                        "in the folder are read, e.g. *.jpg.",
                            examples=["*.jpg", "line1_*.jpg"]),
    ],
    mappings=[
        GstMapping(arch=ARCH_X86_64, element_chain=_jpeg_file_chain("{location}"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        # x86_64 with the NVIDIA GPU runtime mirrors the plain x86_64 chain.
        GstMapping(arch=ARCH_X86_64_NVIDIA, element_chain=_jpeg_file_chain("{location}"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_ARM64_JP4, element_chain=_jpeg_file_chain("{location}"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        GstMapping(arch=ARCH_ARM64_JP5, element_chain=_jpeg_file_chain("{location}"),
                   plugin_dependencies=["coreelements", "emexifextract", "jpeg",
                                        "videoconvertscale", "videofilter"]),
        # JP6 PNG staging path (see _jp6_png_staged_chain).
        GstMapping(arch=ARCH_ARM64_JP6, element_chain=_jp6_png_staged_chain("{location}"),
                   plugin_dependencies=["coreelements", "png", "videoconvertscale",
                                        "python:pillow"]),
        # Simulation: fed from the Test_Dataset like camera_source, never
        # from the device file system — the location path and the DDA
        # decode elements (emexifextract) do not exist in the sandbox
        # (Requirement 12.6).
        _dataset_fed_sim_source(),
    ],
    # Reads the device-local file system (a camera adapter drop folder in
    # practice), which is absent in the cloud sandbox: test runs feed it
    # from the Test_Dataset instead, so it is hardware-dependent for
    # simulation purposes (Requirement 12.6).
    hardware_dependent=True,
)

DIGITAL_INPUT = NodeTypeDescriptor(
    type_id="digital_input",
    category=CATEGORY_TRIGGER,
    display_name="Digital Input",
    inputs=[],
    outputs=[PortDescriptor("out", PORT_TYPE_EVENT_SIGNAL)],
    parameters=[
        ParameterDescriptor("pin", "int", required=True, default=None,
                            constraints={"min": 0, "max": 255},
                            description="GPIO input pin number to watch "
                                        "(0-255), e.g. 7.",
                            examples=[7, 18]),
        ParameterDescriptor("trigger_edge", "enum", required=False, default="rising",
                            constraints={"values": ["rising", "falling", "both"]},
                            description="Signal edge that fires the trigger: "
                                        "rising (low to high), falling (high "
                                        "to low), or both.",
                            examples=["rising", "falling"]),
        ParameterDescriptor("poll_interval_ms", "int", required=False, default=100,
                            constraints={"min": 10, "max": 60000},
                            description="How often the pin is sampled, in "
                                        "milliseconds (10-60000), e.g. 100.",
                            examples=[100, 500]),
    ],
    # GPIO poll adapter runs at the executor level, not as a GStreamer
    # element. Simulation: an appsrc event source the test harness feeds
    # from the Test_Dataset instead of polling GPIO (Requirement 12.6).
    mappings=_same_on_device_archs(executor_binding="digital_input") + [
        GstMapping(
            arch=ARCH_SIM,
            element_chain=[_element("appsrc", name="{sim_source_name}")],
            plugin_dependencies=["app"],
        ),
    ],
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Preprocessing node types (Requirement 2.2)
# --------------------------------------------------------------------------

DEWARP = NodeTypeDescriptor(
    type_id="dewarp",
    category=CATEGORY_PREPROCESSING,
    display_name="Dewarp",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("mode", "enum", required=False, default="fisheye",
                            constraints={"values": ["fisheye", "barrel", "perspective"]},
                            description="Lens distortion model to correct: "
                                        "fisheye, barrel, or perspective.",
                            examples=["fisheye", "barrel"]),
        ParameterDescriptor("strength", "float", required=False, default=0.5,
                            constraints={"min": 0.0, "max": 1.0},
                            description="Correction strength from 0.0 (none) "
                                        "to 1.0 (maximum), e.g. 0.5.",
                            examples=[0.5, 0.8]),
    ],
    # OpenCV-based dewarp plugin delivered as a packaged dependency
    # (not in the LocalServer-bundled manifest).
    mappings=_same_on_all_archs(
        element_chain=[_element("dewarp", mode="{mode}", strength="{strength}")],
        plugin_dependencies=["dda-dewarp"],
    ),
    hardware_dependent=False,
)

ROTATE = NodeTypeDescriptor(
    type_id="rotate",
    category=CATEGORY_PREPROCESSING,
    display_name="Rotate / Flip",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("method", "enum", required=True, default="clockwise",
                            constraints={"values": ["clockwise", "rotate-180",
                                                    "counterclockwise", "horizontal-flip",
                                                    "vertical-flip", "automatic"]},
                            description="Rotation or flip applied to every "
                                        "frame; automatic follows the image "
                                        "orientation metadata.",
                            examples=["clockwise", "rotate-180", "automatic"]),
    ],
    mappings=_same_on_all_archs(
        element_chain=[_element("videoflip", method="{method}")],
        plugin_dependencies=["videofilter"],
    ),
    hardware_dependent=False,
)

CROP = NodeTypeDescriptor(
    type_id="crop",
    category=CATEGORY_PREPROCESSING,
    display_name="Crop",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("top", "int", required=False, default=0, constraints={"min": 0},
                            description="Pixels cropped from the top edge, "
                                        "e.g. 100.",
                            examples=[100]),
        ParameterDescriptor("bottom", "int", required=False, default=0, constraints={"min": 0},
                            description="Pixels cropped from the bottom "
                                        "edge, e.g. 100.",
                            examples=[100]),
        ParameterDescriptor("left", "int", required=False, default=0, constraints={"min": 0},
                            description="Pixels cropped from the left edge, "
                                        "e.g. 50.",
                            examples=[50]),
        ParameterDescriptor("right", "int", required=False, default=0, constraints={"min": 0},
                            description="Pixels cropped from the right edge, "
                                        "e.g. 50.",
                            examples=[50]),
    ],
    mappings=_same_on_all_archs(
        element_chain=[_element("videocrop", top="{top}", bottom="{bottom}",
                                left="{left}", right="{right}")],
        plugin_dependencies=["videocrop"],
    ),
    hardware_dependent=False,
)

FORMAT_CONVERT = NodeTypeDescriptor(
    type_id="format_convert",
    category=CATEGORY_PREPROCESSING,
    display_name="Format Convert",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("format", "enum", required=True, default="RGB",
                            constraints={"values": ["RGB", "RGBA", "BGR", "GRAY8",
                                                    "NV12", "I420"]},
                            description="Pixel format frames are converted "
                                        "to, e.g. RGB (what most models "
                                        "expect).",
                            examples=["RGB", "GRAY8"]),
    ],
    mappings=_same_on_all_archs(
        element_chain=[
            _element("videoconvert"),
            _element("capsfilter", caps="video/x-raw,format={format}"),
        ],
        plugin_dependencies=["videoconvertscale", "coreelements"],
    ),
    hardware_dependent=False,
)

CUSTOM_PYTHON_PREPROCESS = NodeTypeDescriptor(
    type_id="custom_python_preprocess",
    category=CATEGORY_PREPROCESSING,
    display_name="Custom Python (Frames)",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("code", "code", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Python run for every video frame. "
                                        "Define process_frame(frame, "
                                        "metadata) and return the processed "
                                        "frame; frame is a NumPy uint8 array "
                                        "(rows x cols x channels) and "
                                        "cv2/np are pre-imported. Return "
                                        "None to pass the frame through. "
                                        "Helpers: import dda_frames for "
                                        "frame_info(), load_image(path or "
                                        "s3:// URI), to_array(), to_bytes().",
                            examples=["def process_frame(frame, metadata):\n"
                                      "    return cv2.GaussianBlur(frame, (5, 5), 0)"]),
        ParameterDescriptor("requirements", "string", required=False, default="",
                            constraints={},
                            description="Extra pip packages the code needs, "
                                        "one per line in requirements.txt "
                                        "form.",
                            examples=["scikit-image==0.24.0"]),
    ],
    # Same emlpython bridge element and packaged plugin dependency as the
    # custom_python post-processing node (Requirement 1.4); the compiler
    # derives {python_handler_path} per node id, so no compiler changes.
    mappings=_same_on_all_archs(
        element_chain=[_element("emlpython", **{"handler-path": "{python_handler_path}"})],
        plugin_dependencies=["dda-emlpython"],
    ),
    hardware_dependent=False,
)

# --------------------------------------------------------------------------
# Model inference node type (Requirement 2.3)
# --------------------------------------------------------------------------

MODEL_INFERENCE = NodeTypeDescriptor(
    type_id="model_inference",
    category=CATEGORY_INFERENCE,
    display_name="Model Inference",
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        # Populated from the portal model registry for the selected
        # Use_Case (Requirement 2.6).
        ParameterDescriptor("modelName", "model_ref", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Model to run on each frame, chosen "
                                        "from the models registered for the "
                                        "selected use case.",
                            examples=["widget-anomaly-v3"]),
    ],
    # Mirrors pipeline_builder._add_inference_plugins: preceding RGB
    # capsfilter, then emltriton with the Triton repo/server paths
    # LocalServer uses (Requirement 6.2). Device architectures only:
    # the proprietary emltriton plugin does not exist in the cloud test
    # sandbox and registered models are device-compiled, so simulation
    # stubs the node with a pass-through chain (the RGB capsfilter plus
    # an identity element named ``sim_inference_<nodeId>`` the test
    # harness recognizes) that keeps the stream flowing while the
    # configured simulated inference outcome is injected as the node's
    # metadata (Requirement 12.6).
    mappings=_same_on_device_archs(
        element_chain=[
            _element("capsfilter", caps="video/x-raw,format=RGB"),
            _element("emltriton",
                     **{"model-repo": "{triton_model_repo}",
                        "server-path": "{triton_server_path}",
                        "model": "{modelName}"}),
        ],
        plugin_dependencies=["coreelements", "emltriton"],
    ) + [
        GstMapping(
            arch=ARCH_SIM,
            element_chain=[
                _element("capsfilter", caps="video/x-raw,format=RGB"),
                _element("identity", name="{sim_inference_name}"),
            ],
            plugin_dependencies=["coreelements"],
        ),
    ],
    # The model executes only on a device (Jetson-compiled artifacts,
    # proprietary Triton plugin): simulation compiles the sim stub above.
    hardware_dependent=True,
)

#: Default comparison prompt for the Bedrock Inference node. Carries the
#: comparison semantics only: in anomaly mode the executor auto-appends
#: the canonical JSON-format instruction ({is_anomalous, confidence}),
#: so the prompt itself no longer needs to spell out the answer shape.
BEDROCK_DEFAULT_PROMPT = (
    "Compare the input image to the reference image; is_anomalous is "
    "true when the input meaningfully differs from the reference."
)

BEDROCK_INFERENCE = NodeTypeDescriptor(
    type_id="bedrock_inference",
    category=CATEGORY_INFERENCE,
    display_name="Bedrock Inference",
    # Two VideoFrames inputs: the frame under inspection and a reference
    # image the model compares it against per the configured prompt.
    inputs=[
        PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES),
        PortDescriptor("reference", PORT_TYPE_VIDEO_FRAMES),
    ],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        ParameterDescriptor("model", "enum", required=False,
                            default="us.amazon.nova-lite-v1:0",
                            constraints={"values": [
                                "us.amazon.nova-pro-v1:0",
                                "us.amazon.nova-lite-v1:0",
                                "qwen.qwen3-vl-235b-a22b",
                                "moonshotai.kimi-k2.5",
                            ]},
                            description="Bedrock multimodal model invoked "
                                        "with the input frame and the "
                                        "reference image, e.g. "
                                        "us.amazon.nova-lite-v1:0.",
                            examples=["us.amazon.nova-lite-v1:0",
                                      "us.amazon.nova-pro-v1:0"]),
        ParameterDescriptor("prompt", "string", required=True,
                            default=BEDROCK_DEFAULT_PROMPT,
                            constraints={"min_length": 1},
                            description="Instruction sent to the model with "
                                        "both images. In anomaly mode the "
                                        "executor automatically appends the "
                                        "JSON-format instruction "
                                        '({"is_anomalous": true|false, '
                                        '"confidence": 0..1}) and those '
                                        "fields become the inference "
                                        "metadata (is_anomalous, "
                                        "confidence) driving downstream "
                                        "filters, conditionals, and "
                                        "outputs; in freeform mode the "
                                        "prompt is sent as-is.",
                            examples=[BEDROCK_DEFAULT_PROMPT]),
        # Response mode toggle: checked (default) keeps today's anomaly
        # JSON verdict contract — the executor appends the canonical
        # JSON instruction to the prompt and merges the parsed
        # {is_anomalous, confidence} into the run metadata. Unchecked
        # switches the node to freeform: the prompt is sent unchanged
        # and the raw model text is recorded as bedrock_text plus
        # bedrock.{nodeId}.text with no JSON parsing.
        ParameterDescriptor("anomaly_mode", "bool", required=False,
                            default=True,
                            constraints={},
                            description="Checked: anomaly mode — the "
                                        "executor auto-appends the JSON "
                                        "instruction and the model's "
                                        "verdict (is_anomalous, "
                                        "confidence) drives downstream "
                                        "filters, conditionals, and "
                                        "outputs. Unchecked: freeform mode "
                                        "— the prompt is sent as-is and "
                                        "the raw model text is recorded in "
                                        "the run metadata as bedrock_text "
                                        "(and bedrock.{nodeId}.text), with "
                                        "no JSON parsing.",
                            examples=[True, False]),
        ParameterDescriptor("region", "string", required=False,
                            default="us-east-1",
                            constraints={"min_length": 1},
                            description="AWS region of the Bedrock runtime "
                                        "endpoint the device calls, e.g. "
                                        "us-east-1.",
                            examples=["us-east-1", "us-west-2"]),
        ParameterDescriptor("max_tokens", "int", required=False, default=256,
                            constraints={"min": 1, "max": 4096},
                            description="Maximum tokens the model may "
                                        "generate for its JSON answer, "
                                        "e.g. 256.",
                            examples=[256, 512]),
    ],
    # Executor-level on every physical device architecture: the node has
    # no GStreamer element of its own. The compiler terminates each
    # VideoFrames input branch in a synthetic frame-capture sink chain
    # (videoconvert ! jpegenc ! multifilesink location={work_dir}/...)
    # and emits a "bedrock_inference" executor binding carrying the
    # parameters plus the per-port capture file paths; after a successful
    # pipeline run the LocalServer executor reads the two captured
    # frames, calls the Bedrock runtime (network + AWS credentials
    # required on the device), and merges the parsed {is_anomalous,
    # confidence} into the run's inference metadata. Because frames stop
    # at the capture sinks, the node's InferenceMeta output must feed
    # only executor-level consumers (filters, conditionals, hardware
    # outputs) on device architectures.
    #
    # Simulation: the cloud sandbox VPC has no internet, so the node is
    # stubbed exactly like model_inference — a pass-through chain (RGB
    # capsfilter + identity named ``sim_inference_<nodeId>``) the test
    # harness recognizes; the configured simulated inference outcome is
    # injected as the node's metadata and the model is never invoked
    # (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="bedrock_inference",
        plugin_dependencies=["videoconvertscale", "jpeg", "multifile",
                             "python:boto3"],
    ) + [
        GstMapping(
            arch=ARCH_SIM,
            element_chain=[
                _element("capsfilter", caps="video/x-raw,format=RGB"),
                _element("identity", name="{sim_inference_name}"),
            ],
            plugin_dependencies=["coreelements"],
        ),
    ],
    # Needs device-side network access and AWS credentials; simulation
    # compiles the sim stub above (Requirement 12.6).
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# LLM inference node type (vllm-triton-inference Requirements 6.1, 6.3,
# 6.4, 6.8, 6.9, 6.10)
# --------------------------------------------------------------------------

#: Architectures capable of vLLM execution. JetPack 6 always; JetPack 5
#: only while ``JP5_VLLM_ENABLED`` is flipped on (see models.py). The
#: other architectures (``x86_64``, ``x86_64_nvidia``, ``arm64_jp4``)
#: never appear here, so ``llm_inference`` has no mapping for them and
#: the compiler's existing unmapped-architecture error (node + arch, no
#: document) implements Requirement 6.8 with no new compiler code path.
VLLM_ARCHITECTURES = (ARCH_ARM64_JP6,) + \
    ((ARCH_ARM64_JP5,) if JP5_VLLM_ENABLED else ())

LLM_INFERENCE = NodeTypeDescriptor(
    type_id="llm_inference",
    category=CATEGORY_INFERENCE,
    display_name="VLM/LLM Inference",
    # As a vision-language node it takes video frames as input (a
    # video-frame source connects directly into it) and emits the
    # generated text as inference metadata for downstream consumers
    # (Requirements 6.3, 6.4).
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        # Populated from the Use_Case's registered vLLM_Model_Records
        # (Requirement 6.2 shape; the validator's model-reference pass
        # requires a ``vllm``-typed record — Requirement 6.12).
        ParameterDescriptor("modelName", "model_ref", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Registered vLLM model to invoke, "
                                        "chosen from the vLLM models "
                                        "registered for the selected use "
                                        "case.",
                            examples=["opt-125m"]),
        ParameterDescriptor("prompt_template", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Prompt sent to the model. {field} "
                                        "placeholders are replaced with values "
                                        "from the upstream inference metadata "
                                        "at execution time.",
                            examples=["Summarize this inspection result: "
                                      "anomalous={is_anomalous}, "
                                      "confidence={confidence}"]),
        ParameterDescriptor("max_tokens", "int", required=False, default=256,
                            constraints={"min": 1},
                            description="Maximum tokens the model may "
                                        "generate for its answer, e.g. 256.",
                            examples=[256, 512]),
        ParameterDescriptor("temperature", "float", required=False, default=0.7,
                            constraints={"min": 0.0, "max": 2.0},
                            description="Sampling temperature between 0.0 "
                                        "(deterministic) and 2.0 (most "
                                        "random), e.g. 0.7.",
                            examples=[0.7, 0.2]),
        ParameterDescriptor("top_p", "float", required=False, default=1.0,
                            constraints={"min_exclusive": 0.0, "max": 1.0},
                            description="Nucleus-sampling probability mass, "
                                        "greater than 0.0 and at most 1.0, "
                                        "e.g. 1.0.",
                            examples=[1.0, 0.9]),
    ],
    # Executor-level realization (no GStreamer element): the compiler
    # emits an ``llm_inference`` executor binding carrying the bound
    # model name, prompt template, and generation parameters; the
    # LocalServer workflow engine renders the prompt from upstream
    # metadata and calls the device Text_Generation_API. Mappings exist
    # only for vLLM-capable architectures plus the simulation stub —
    # ``sim_llm_inference`` injects the configured simulated inference
    # outcome and never invokes any model (Requirement 6.9).
    mappings=[
        GstMapping(arch=arch, executor_binding="llm_inference")
        for arch in VLLM_ARCHITECTURES
    ] + [
        GstMapping(arch=ARCH_SIM, executor_binding="sim_llm_inference"),
    ],
    # Text generation runs only on a vLLM-capable device: simulation
    # compiles the sim stub above (Requirement 12.6).
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Post-processing node types (Requirement 2.4)
# --------------------------------------------------------------------------

CUSTOM_PYTHON = NodeTypeDescriptor(
    type_id="custom_python",
    category=CATEGORY_POST_PROCESSING,
    display_name="Custom Python",
    # Default port typing; overridden per node instance via the declared
    # input/output port type parameters (Requirement 2.7).
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        ParameterDescriptor("code", "code", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Python run for every item passing "
                                        "through the node. Define "
                                        "process_frame(frame, metadata) to "
                                        "work with video frames as NumPy "
                                        "arrays (cv2/np pre-imported; import "
                                        "dda_frames for helpers), or "
                                        "handle(frame_bytes, metadata) -> "
                                        "(frame_bytes, metadata) to work "
                                        "with raw bytes.",
                            examples=["def process_frame(frame, metadata):\n"
                                      "    return frame",
                                      "def handle(frame_bytes, metadata):\n"
                                      "    return frame_bytes, metadata"]),
        ParameterDescriptor("requirements", "string", required=False, default="",
                            constraints={},
                            description="Extra pip packages the code needs, "
                                        "one per line in requirements.txt "
                                        "form, e.g. numpy==1.24.0.",
                            examples=["numpy==1.24.0",
                                      "numpy==1.24.0\nopencv-python-headless==4.8.0.74"]),
        ParameterDescriptor("input_port_type", "enum", required=True,
                            default=PORT_TYPE_INFERENCE_META,
                            constraints={"values": [PORT_TYPE_VIDEO_FRAMES,
                                                    PORT_TYPE_INFERENCE_META,
                                                    PORT_TYPE_EVENT_SIGNAL]},
                            description="Type of data this node accepts on "
                                        "its input port: VideoFrames, "
                                        "InferenceMeta, or EventSignal.",
                            examples=[PORT_TYPE_INFERENCE_META]),
        ParameterDescriptor("output_port_type", "enum", required=True,
                            default=PORT_TYPE_INFERENCE_META,
                            constraints={"values": [PORT_TYPE_VIDEO_FRAMES,
                                                    PORT_TYPE_INFERENCE_META,
                                                    PORT_TYPE_EVENT_SIGNAL]},
                            description="Type of data this node emits on its "
                                        "output port: VideoFrames, "
                                        "InferenceMeta, or EventSignal.",
                            examples=[PORT_TYPE_INFERENCE_META]),
    ],
    # emlpython bridge element invoking user code (appsink/appsrc pair
    # managed by the executor); delivered as a packaged dependency.
    mappings=_same_on_all_archs(
        element_chain=[_element("emlpython", **{"handler-path": "{python_handler_path}"})],
        plugin_dependencies=["dda-emlpython"],
    ),
    hardware_dependent=False,
)

INFERENCE_FILTER = NodeTypeDescriptor(
    type_id="inference_filter",
    category=CATEGORY_POST_PROCESSING,
    display_name="Inference Filter",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        # Rule expression over inference metadata, e.g.
        # "is_anomalous == true && confidence >= 0.8".
        ParameterDescriptor("condition", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Inference results continue "
                                        "downstream only while this "
                                        "condition holds. " +
                                        CONDITION_LANGUAGE_DESCRIPTION,
                            examples=list(CONDITION_EXAMPLES)),
    ],
    # Executor-evaluated condition over inference metadata.
    mappings=_same_on_all_archs(executor_binding="inference_filter"),
    hardware_dependent=False,
)

CONDITIONAL = NodeTypeDescriptor(
    type_id="conditional",
    category=CATEGORY_POST_PROCESSING,
    display_name="Conditional",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    # Two-path routing over the same condition grammar as
    # inference_filter: the "true" output receives the inference
    # metadata when the condition holds, the "false" output when it does
    # not — e.g. a green andon light on normal results and a red one on
    # anomalies.
    outputs=[
        PortDescriptor("true", PORT_TYPE_INFERENCE_META),
        PortDescriptor("false", PORT_TYPE_INFERENCE_META),
    ],
    parameters=[
        # Rule expression over inference metadata (same grammar as
        # inference_filter), e.g. "is_anomalous == true".
        ParameterDescriptor("condition", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Routes each inference result to "
                                        "one of the two outputs: the 'true' "
                                        "output receives it when this "
                                        "condition holds, the 'false' "
                                        "output when it does not. " +
                                        CONDITION_LANGUAGE_DESCRIPTION,
                            examples=list(CONDITION_EXAMPLES)),
    ],
    # Executor-evaluated condition over inference metadata: downstream of
    # the "true" output is gated by the condition, downstream of the
    # "false" output by its negation (compiler portConditions).
    mappings=_same_on_all_archs(executor_binding="conditional"),
    hardware_dependent=False,
)

# --------------------------------------------------------------------------
# Output node types (Requirement 2.5)
# --------------------------------------------------------------------------

DIGITAL_OUTPUT = NodeTypeDescriptor(
    type_id="digital_output",
    category=CATEGORY_OUTPUT,
    display_name="Digital Output",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[],
    parameters=[
        ParameterDescriptor("pin", "int", required=True, default=None,
                            constraints={"min": 0, "max": 255},
                            description="GPIO output pin number to actuate "
                                        "(0-255), e.g. 5.",
                            examples=[5, 12]),
        ParameterDescriptor("signal_type", "enum", required=True, default="pulse",
                            constraints={"values": ["high", "low", "pulse"]},
                            description="Signal written to the pin: high or "
                                        "low latches the level; pulse sets "
                                        "high then resets after the pulse "
                                        "width.",
                            examples=["pulse", "high"]),
        ParameterDescriptor("pulse_width_ms", "int", required=False, default=100,
                            constraints={"min": 1, "max": 60000},
                            description="Pulse duration in milliseconds "
                                        "(used with signal type pulse), "
                                        "e.g. 100.",
                            examples=[100, 250]),
        # Rule over inference metadata gating actuation (Requirement 9.4).
        ParameterDescriptor("condition", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="The pin is actuated only when this "
                                        "condition holds. " +
                                        CONDITION_LANGUAGE_DESCRIPTION,
                            examples=list(CONDITION_EXAMPLES)),
    ],
    # Existing emoutputevent element (see pipeline_builder._add_output_plugins).
    # Simulation: recording binding, no GPIO actuation (Requirement 12.6).
    mappings=_same_on_device_archs(
        element_chain=[_element("emoutputevent",
                                **{"script-path": "{dio_script_path}",
                                   "config": "{dio_config_json}"})],
        plugin_dependencies=["emoutputevent"],
    ) + [_recording_binding("digital_output")],
    hardware_dependent=True,
)

MQTT_PUBLISH = NodeTypeDescriptor(
    type_id="mqtt_publish",
    category=CATEGORY_OUTPUT,
    display_name="MQTT Publish",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[],
    parameters=[
        # Not statically required so a topic-only Greengrass config
        # (greengrass=True) is not force-failed by the V4 required check;
        # a mqtt_publish-specific validator check still rejects a config
        # that supplies no target (no greengrass, no aws_iot, no host).
        ParameterDescriptor("broker_host", "string", required=False, default=None,
                            constraints={"min_length": 1},
                            description="MQTT broker hostname or IP, e.g. "
                                        "10.0.0.12 or broker.local.",
                            examples=["10.0.0.12", "broker.local"]),
        ParameterDescriptor("broker_port", "int", required=False, default=1883,
                            constraints={"min": 1, "max": 65535},
                            description="MQTT broker TCP port, e.g. 1883 "
                                        "(plain MQTT) or 8883 (TLS).",
                            examples=[1883, 8883]),
        ParameterDescriptor("topic", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Topic the message is published to, "
                                        "e.g. factory/line1/inspection.",
                            examples=["factory/line1/inspection"]),
        ParameterDescriptor("payload_template", "string", required=False,
                            default="{inference_json}", constraints={},
                            description="Message payload. Placeholders in "
                                        "curly braces are replaced from the "
                                        "inference metadata: "
                                        "{inference_json} (full metadata as "
                                        "JSON), {is_anomalous}, and "
                                        "{confidence}.",
                            examples=["{inference_json}",
                                      "anomaly={is_anomalous} "
                                      "confidence={confidence}"]),
        ParameterDescriptor("qos", "enum", required=False, default=0,
                            constraints={"values": [0, 1, 2]},
                            description="MQTT quality of service: 0 (at "
                                        "most once), 1 (at least once), or "
                                        "2 (exactly once; AWS IoT Core "
                                        "supports up to 1).",
                            examples=[0, 1]),
        # Zero-config publishing through the device's Greengrass-managed
        # MQTT: the Greengrass nucleus already holds the AWS IoT Core
        # connection, so only the topic is needed — no broker host/port
        # and no certificate file paths. Additive and off by default; the
        # broker and aws_iot paths are unchanged when greengrass is off.
        ParameterDescriptor("greengrass", "bool", required=False, default=False,
                            constraints={},
                            description="Publish through the device's "
                                        "Greengrass-managed MQTT (the "
                                        "Greengrass nucleus's AWS IoT Core "
                                        "connection) instead of a plain "
                                        "broker or your own AWS IoT "
                                        "credentials. Zero configuration: "
                                        "only the topic is required — no "
                                        "broker host or port and no "
                                        "certificate paths.",
                            examples=[True]),
        # AWS IoT Core publishing over mutual TLS. Certificates are
        # referenced by file paths on the device (e.g. /greengrass/v2/...),
        # never uploaded to the portal. The iot_* fields are shown in the
        # config panel only while aws_iot is enabled (depends_on).
        ParameterDescriptor("aws_iot", "bool", required=False, default=False,
                            constraints={},
                            description="Publish to AWS IoT Core over "
                                        "mutual TLS instead of a plain MQTT "
                                        "broker; enables the IoT thing name "
                                        "and certificate path fields.",
                            examples=[True]),
        ParameterDescriptor("iot_thing_name", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="AWS IoT thing name used as the "
                                        "MQTT client id, e.g. "
                                        "dda-edge-device-01.",
                            examples=["dda-edge-device-01"]),
        # Amazon root CA path on the device.
        ParameterDescriptor("iot_ca_cert_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the Amazon root CA "
                                        "certificate on the device, e.g. "
                                        "/greengrass/v2/rootCA.pem.",
                            examples=["/greengrass/v2/rootCA.pem"]),
        ParameterDescriptor("iot_client_cert_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the device client "
                                        "certificate on the device, e.g. "
                                        "/greengrass/v2/thingCert.crt.",
                            examples=["/greengrass/v2/thingCert.crt"]),
        ParameterDescriptor("iot_private_key_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the device private key on "
                                        "the device, e.g. "
                                        "/greengrass/v2/privKey.key.",
                            examples=["/greengrass/v2/privKey.key"]),
    ],
    # Executor-level MQTT client publish on pipeline completion. paho-mqtt
    # serves the plain-broker and aws_iot paths; awsiotsdk provides the
    # Greengrass IPC client used by the zero-config Greengrass path.
    # Simulation: recording binding, no broker contact (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="mqtt_publish",
        plugin_dependencies=["python:paho-mqtt", "python:awsiotsdk"],
    ) + [_recording_binding("mqtt_publish")],
    hardware_dependent=True,
)

OPCUA_WRITE = NodeTypeDescriptor(
    type_id="opcua_write",
    category=CATEGORY_OUTPUT,
    display_name="OPC UA Write",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[],
    parameters=[
        ParameterDescriptor("endpoint", "string", required=True, default=None,
                            constraints={"min_length": 1,
                                         "regex": r"^opc\.tcp://.+"},
                            description="OPC UA server endpoint URL, e.g. "
                                        "opc.tcp://192.168.1.20:4840.",
                            examples=["opc.tcp://192.168.1.20:4840"]),
        ParameterDescriptor("node_id", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="OPC UA node id the value is "
                                        "written to, e.g. "
                                        "ns=2;s=Machine1.Reject.",
                            examples=["ns=2;s=Machine1.Reject"]),
        ParameterDescriptor("value_template", "string", required=False,
                            default="{is_anomalous}", constraints={},
                            description="Value written to the node. "
                                        "Placeholders in curly braces are "
                                        "replaced from the inference "
                                        "metadata: {is_anomalous}, "
                                        "{confidence}, or {inference_json}; "
                                        "a single placeholder keeps its "
                                        "native type.",
                            examples=["{is_anomalous}", "{confidence}"]),
        # --- Authentication / security (all optional; anonymous + no
        # security when unset). username/password enable user-token auth;
        # security_policy + client_cert_path + client_key_path enable
        # certificate-based signing/encryption. NOTE: password and cert
        # paths are stored with the workflow definition — treat the
        # password as a secret and prefer per-device certificate files.
        ParameterDescriptor("username", "string", required=False, default=None,
                            constraints={},
                            description="Optional OPC UA user name for "
                                        "user-token authentication. Leave "
                                        "empty for anonymous access.",
                            examples=["operator"]),
        ParameterDescriptor("password", "string", required=False, default=None,
                            constraints={},
                            description="Optional password for the OPC UA "
                                        "user. Stored with the workflow "
                                        "definition; treat as a secret.",
                            examples=["changeit"]),
        ParameterDescriptor("security_policy", "string", required=False,
                            default=None, constraints={},
                            description="Optional OPC UA security policy for "
                                        "an encrypted/signed session, e.g. "
                                        "Basic256Sha256. Requires "
                                        "client_cert_path and client_key_path.",
                            examples=["Basic256Sha256"]),
        ParameterDescriptor("security_mode", "string", required=False,
                            default=None, constraints={},
                            description="Optional message security mode used "
                                        "with security_policy: Sign or "
                                        "SignAndEncrypt (defaults to "
                                        "SignAndEncrypt when a policy is set).",
                            examples=["SignAndEncrypt", "Sign"]),
        ParameterDescriptor("client_cert_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "client application certificate used "
                                        "for certificate-based security.",
                            examples=["/aws_dda/opcua/client-cert.der"]),
        ParameterDescriptor("client_key_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "client certificate private key.",
                            examples=["/aws_dda/opcua/client-key.pem"]),
        ParameterDescriptor("server_cert_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "server's certificate to pin/trust.",
                            examples=["/aws_dda/opcua/server-cert.der"]),
    ],
    # Executor-level OPC UA client write; the opcua Python lib is a
    # packaged dependency (not bundled with LocalServer).
    # Simulation: recording binding, no server contact (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="opcua_write",
        plugin_dependencies=["python:opcua"],
    ) + [_recording_binding("opcua_write")],
    hardware_dependent=True,
)

CAPTURE = NodeTypeDescriptor(
    type_id="capture",
    category=CATEGORY_OUTPUT,
    display_name="Capture to File System",
    # VideoFrames input; InferenceMeta outputs also connect here via the
    # declared InferenceMeta -> VideoFrames coercion (same buffer stream
    # with attached metadata).
    inputs=[PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)],
    outputs=[],
    parameters=[
        ParameterDescriptor("output_path", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Folder on the device file system "
                                        "where captured JPEG images are "
                                        "written, e.g. /aws_dda/captures.",
                            examples=["/aws_dda/captures"]),
        ParameterDescriptor("interval", "int", required=False, default=0,
                            constraints={"min": 0},
                            description="Capture every Nth frame; 0 "
                                        "captures every frame, e.g. 10 "
                                        "keeps one frame in ten.",
                            examples=[0, 10]),
        ParameterDescriptor("quality", "int", required=False, default=100,
                            constraints={"min": 1, "max": 100},
                            description="JPEG encoding quality from 1 "
                                        "(smallest file) to 100 (best "
                                        "image), e.g. 85.",
                            examples=[85, 100]),
    ],
    # Existing jpegenc ! emlcapture chain
    # (see pipeline_builder._add_post_processing_plugins).
    mappings=_same_on_all_archs(
        element_chain=[
            _element("jpegenc", **{"idct-method": 2, "quality": "{quality}"}),
            _element("emlcapture",
                     **{"buffer-message-id": "file-target_{output_path}-jpg",
                        "interval": "{interval}",
                        "meta": "{capture_meta}"}),
        ],
        plugin_dependencies=["jpeg", "emlcapture"],
    ),
    hardware_dependent=False,
)

# --------------------------------------------------------------------------
# Unified input node (Requirements 3.1-3.5, 3.7, 3.9) — a single palette
# entry whose ``source_kind`` selects which underlying frame source it
# represents. It never compiles directly: the compiler's
# ``expand_unified_inputs`` pre-pass rewrites each unified node into the
# ``SOURCE_KIND_TO_SOURCE_TYPE[source_kind]`` source descriptor before
# mapping resolution, so the unified ``type_id`` never reaches
# ``mapping_for`` (see design C3/C5).
# --------------------------------------------------------------------------

#: Source-kind → source type map: the single source of truth for both the
#: frontend parameter gating and the compiler expansion. Deliberately
#: excludes ``digital_input`` — a trigger, not a selectable frame source
#: (Requirement 3.3).
SOURCE_KIND_TO_SOURCE_TYPE = {
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "aravis_camera": "aravis_camera_source",
    "folder": "folder_source",
}

#: The four retained source descriptors, keyed by their type id. Referenced
#: directly (not via ``get_node_type``, which is defined below) so the union
#: can be built at module import time.
_UNIFIED_SOURCE_DESCRIPTORS = {
    "csi_camera_source": CSI_CAMERA_SOURCE,
    "icam_source": ICAM_SOURCE,
    "aravis_camera_source": ARAVIS_CAMERA_SOURCE,
    "folder_source": FOLDER_SOURCE,
}


def _unified_source_parameters():
    """Union of the four source descriptors' parameters, required-relaxed.

    Reuses the live ``ParameterDescriptor`` objects via
    ``dataclasses.replace(p, required=False)`` so the unified node's
    names/types/defaults/constraints cannot drift from the originals
    (Requirement 3.4). Parameters are concatenated in source-kind order and
    de-duplicated by name (only the identical ``gain``/``exposure`` collide,
    between ``csi_camera_source`` and ``aravis_camera_source``; the first is
    kept). The only overridden field is ``required`` → ``False``: V4 has no
    notion of "required only when source_kind == X", so keeping the
    underlying required flags would fail every unified node; genuine
    required-ness is enforced at compile time by expansion into the
    underlying descriptor, which retains ``required=True`` (see design C3/C5).
    """
    seen, params = {}, []
    for type_id in SOURCE_KIND_TO_SOURCE_TYPE.values():
        for p in _UNIFIED_SOURCE_DESCRIPTORS[type_id].parameters:
            if p.name in seen:
                continue
            seen[p.name] = type_id
            params.append(replace(p, required=False))
    return params


UNIFIED_INPUT = NodeTypeDescriptor(
    type_id="unified_input",
    category=CATEGORY_INPUT,
    display_name="Input Source",
    # Optional activation port: a CATEGORY_TRIGGER output may feed it, but
    # the port is inert at compile time (Requirements 3.7, 3.9).
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    # Single video-frame output (Requirement 3.5).
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("source_kind", "enum", required=True, default="folder",
                            constraints={"values": list(SOURCE_KIND_TO_SOURCE_TYPE)},
                            description="Which underlying source this input "
                                        "represents.",
                            examples=["folder", "csi_camera"]),
        *_unified_source_parameters(),
    ],
    # Empty-but-present placeholder: the unified type never reaches
    # ``mapping_for`` because ``expand_unified_inputs`` rewrites it into its
    # underlying source before mapping resolution (design C3/C5).
    mappings=[],
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Subscribe trigger node types (trigger-activation-runtime Requirements
# 1, 2) — executor-level triggers that fire a run activation when an
# external event arrives: an MQTT message on a subscribed topic filter
# or an OPC UA monitored-value change.
# --------------------------------------------------------------------------


def _trigger_policy_parameters():
    """The shared per-trigger-node activation policy parameter family
    (trigger-activation-runtime Requirements 1.3, 1.4, 2.5), built by one
    helper so the ``mqtt_subscribe`` and ``opcua_subscribe`` descriptors
    cannot drift: ``concurrency_policy`` with its gated ``queue_depth`` /
    ``debounce_ms`` companions (``depends_on`` ``"name=value"`` form),
    ``retry_limit`` (0 = retry forever), and ``priority`` (lower value =
    higher priority; ties served FIFO by firing time)."""
    return [
        ParameterDescriptor("concurrency_policy", "enum", required=False,
                            default="queue",
                            constraints={"values": ["queue", "drop", "debounce"]},
                            description="What happens when this trigger "
                                        "fires while a run it activated is "
                                        "still in flight or pending: queue "
                                        "the firing (bounded by queue "
                                        "depth), drop it, or debounce — "
                                        "coalesce firings within the "
                                        "debounce interval into one run "
                                        "carrying the most recent trigger "
                                        "context.",
                            examples=["queue", "drop"]),
        ParameterDescriptor("queue_depth", "int", required=False, default=10,
                            constraints={"min": 1, "max": 1000},
                            depends_on="concurrency_policy=queue",
                            description="Maximum pending activations queued "
                                        "for this trigger (1-1000); further "
                                        "firings are discarded and logged, "
                                        "e.g. 10.",
                            examples=[10, 100]),
        ParameterDescriptor("debounce_ms", "int", required=False, default=500,
                            constraints={"min": 1, "max": 60000},
                            depends_on="concurrency_policy=debounce",
                            description="Trailing debounce interval in "
                                        "milliseconds (1-60000): firings "
                                        "within it coalesce into one "
                                        "activation carrying the most "
                                        "recent trigger context, e.g. 500.",
                            examples=[500, 2000]),
        ParameterDescriptor("retry_limit", "int", required=False, default=0,
                            constraints={"min": 0, "max": 1000},
                            description="Maximum automatic reconnect "
                                        "attempts after the trigger's "
                                        "connection drops (0-1000); 0 = "
                                        "retry forever, e.g. 0.",
                            examples=[0, 5]),
        ParameterDescriptor("priority", "int", required=False, default=100,
                            constraints={"min": 0, "max": 1000},
                            description="Activation priority relative to "
                                        "the workflow's other triggers "
                                        "(0-1000); lower value = higher "
                                        "priority, ties served in firing "
                                        "order, e.g. 100.",
                            examples=[100, 10]),
    ]


MQTT_SUBSCRIBE = NodeTypeDescriptor(
    type_id="mqtt_subscribe",
    category=CATEGORY_TRIGGER,
    display_name="MQTT Subscribe",
    inputs=[],
    outputs=[PortDescriptor("out", PORT_TYPE_EVENT_SIGNAL)],
    parameters=[
        # Connection parameters mirror mqtt_publish field-for-field
        # (trigger-activation-runtime Requirement 1.2): same names, types,
        # defaults, and constraints, so the three transports (greengrass,
        # aws_iot, plain broker) are configured exactly like publishing.
        # broker_host is not statically required so a topic-only
        # Greengrass config (greengrass=True) is not force-failed by the
        # V4 required check; a mqtt_subscribe-specific validator check
        # (V8) still rejects a config that supplies no target (no
        # greengrass, no aws_iot, no host).
        ParameterDescriptor("broker_host", "string", required=False, default=None,
                            constraints={"min_length": 1},
                            description="MQTT broker hostname or IP, e.g. "
                                        "10.0.0.12 or broker.local.",
                            examples=["10.0.0.12", "broker.local"]),
        ParameterDescriptor("broker_port", "int", required=False, default=1883,
                            constraints={"min": 1, "max": 65535},
                            description="MQTT broker TCP port, e.g. 1883 "
                                        "(plain MQTT) or 8883 (TLS).",
                            examples=[1883, 8883]),
        ParameterDescriptor("topic", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Topic filter the trigger "
                                        "subscribes to; a message arriving "
                                        "on a matching topic starts a "
                                        "workflow run, e.g. "
                                        "factory/line1/trigger or "
                                        "factory/+/trigger.",
                            examples=["factory/line1/trigger",
                                      "factory/+/trigger"]),
        ParameterDescriptor("qos", "enum", required=False, default=0,
                            constraints={"values": [0, 1, 2]},
                            description="MQTT quality of service: 0 (at "
                                        "most once), 1 (at least once), or "
                                        "2 (exactly once; AWS IoT Core "
                                        "supports up to 1).",
                            examples=[0, 1]),
        # Zero-config publishing through the device's Greengrass-managed
        # MQTT: the Greengrass nucleus already holds the AWS IoT Core
        # connection, so only the topic is needed — no broker host/port
        # and no certificate file paths. Additive and off by default; the
        # broker and aws_iot paths are unchanged when greengrass is off.
        ParameterDescriptor("greengrass", "bool", required=False, default=False,
                            constraints={},
                            description="Publish through the device's "
                                        "Greengrass-managed MQTT (the "
                                        "Greengrass nucleus's AWS IoT Core "
                                        "connection) instead of a plain "
                                        "broker or your own AWS IoT "
                                        "credentials. Zero configuration: "
                                        "only the topic is required — no "
                                        "broker host or port and no "
                                        "certificate paths.",
                            examples=[True]),
        # AWS IoT Core publishing over mutual TLS. Certificates are
        # referenced by file paths on the device (e.g. /greengrass/v2/...),
        # never uploaded to the portal. The iot_* fields are shown in the
        # config panel only while aws_iot is enabled (depends_on).
        ParameterDescriptor("aws_iot", "bool", required=False, default=False,
                            constraints={},
                            description="Publish to AWS IoT Core over "
                                        "mutual TLS instead of a plain MQTT "
                                        "broker; enables the IoT thing name "
                                        "and certificate path fields.",
                            examples=[True]),
        ParameterDescriptor("iot_thing_name", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="AWS IoT thing name used as the "
                                        "MQTT client id, e.g. "
                                        "dda-edge-device-01.",
                            examples=["dda-edge-device-01"]),
        # Amazon root CA path on the device.
        ParameterDescriptor("iot_ca_cert_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the Amazon root CA "
                                        "certificate on the device, e.g. "
                                        "/greengrass/v2/rootCA.pem.",
                            examples=["/greengrass/v2/rootCA.pem"]),
        ParameterDescriptor("iot_client_cert_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the device client "
                                        "certificate on the device, e.g. "
                                        "/greengrass/v2/thingCert.crt.",
                            examples=["/greengrass/v2/thingCert.crt"]),
        ParameterDescriptor("iot_private_key_path", "string", required=False,
                            default=None, constraints={"min_length": 1},
                            depends_on="aws_iot",
                            description="Path of the device private key on "
                                        "the device, e.g. "
                                        "/greengrass/v2/privKey.key.",
                            examples=["/greengrass/v2/privKey.key"]),
        *_trigger_policy_parameters(),
    ],
    # Executor-level MQTT subscription held by the device's trigger
    # subscription manager. paho-mqtt serves the plain-broker and aws_iot
    # paths; awsiotsdk provides the Greengrass IPC client used by the
    # zero-config Greengrass SubscribeToIoTCore path. Simulation: an
    # appsrc event source the test harness feeds from the Test_Dataset
    # instead of any subscription, mirroring digital_input
    # (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="mqtt_subscribe",
        plugin_dependencies=["python:paho-mqtt", "python:awsiotsdk"],
    ) + [
        GstMapping(
            arch=ARCH_SIM,
            element_chain=[_element("appsrc", name="{sim_source_name}")],
            plugin_dependencies=["app"],
        ),
    ],
    hardware_dependent=True,
)

OPCUA_SUBSCRIBE = NodeTypeDescriptor(
    type_id="opcua_subscribe",
    category=CATEGORY_TRIGGER,
    display_name="OPC UA Subscribe",
    inputs=[],
    outputs=[PortDescriptor("out", PORT_TYPE_EVENT_SIGNAL)],
    parameters=[
        # endpoint/node_id and the security parameters mirror opcua_write
        # field-for-field (trigger-activation-runtime Requirements 2.2,
        # 2.3): same names, types, defaults, and constraints, so the
        # session is configured exactly like writing.
        ParameterDescriptor("endpoint", "string", required=True, default=None,
                            constraints={"min_length": 1,
                                         "regex": r"^opc\.tcp://.+"},
                            description="OPC UA server endpoint URL, e.g. "
                                        "opc.tcp://192.168.1.20:4840.",
                            examples=["opc.tcp://192.168.1.20:4840"]),
        ParameterDescriptor("node_id", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="OPC UA node id the value is "
                                        "written to, e.g. "
                                        "ns=2;s=Machine1.Reject.",
                            examples=["ns=2;s=Machine1.Reject"]),
        ParameterDescriptor("sampling_interval_ms", "int", required=False,
                            default=100,
                            constraints={"min": 10, "max": 60000},
                            description="Sampling/publishing interval of "
                                        "the OPC UA subscription in "
                                        "milliseconds (10-60000), e.g. 100.",
                            examples=[100, 1000]),
        ParameterDescriptor("mode", "enum", required=False, default="subscribe",
                            constraints={"values": ["subscribe", "poll"]},
                            description="How value changes are detected: "
                                        "subscribe registers a true OPC UA "
                                        "data-change subscription (the "
                                        "default); poll reads the node "
                                        "periodically and fires when the "
                                        "value changes.",
                            examples=["subscribe", "poll"]),
        ParameterDescriptor("poll_interval_ms", "int", required=False,
                            default=500,
                            constraints={"min": 10, "max": 60000},
                            depends_on="mode=poll",
                            description="How often the node is read in poll "
                                        "mode, in milliseconds (10-60000), "
                                        "e.g. 500.",
                            examples=[500, 2000]),
        # --- Authentication / security (all optional; anonymous + no
        # security when unset). username/password enable user-token auth;
        # security_policy + client_cert_path + client_key_path enable
        # certificate-based signing/encryption. NOTE: password and cert
        # paths are stored with the workflow definition — treat the
        # password as a secret and prefer per-device certificate files.
        ParameterDescriptor("username", "string", required=False, default=None,
                            constraints={},
                            description="Optional OPC UA user name for "
                                        "user-token authentication. Leave "
                                        "empty for anonymous access.",
                            examples=["operator"]),
        ParameterDescriptor("password", "string", required=False, default=None,
                            constraints={},
                            description="Optional password for the OPC UA "
                                        "user. Stored with the workflow "
                                        "definition; treat as a secret.",
                            examples=["changeit"]),
        ParameterDescriptor("security_policy", "string", required=False,
                            default=None, constraints={},
                            description="Optional OPC UA security policy for "
                                        "an encrypted/signed session, e.g. "
                                        "Basic256Sha256. Requires "
                                        "client_cert_path and client_key_path.",
                            examples=["Basic256Sha256"]),
        ParameterDescriptor("security_mode", "string", required=False,
                            default=None, constraints={},
                            description="Optional message security mode used "
                                        "with security_policy: Sign or "
                                        "SignAndEncrypt (defaults to "
                                        "SignAndEncrypt when a policy is set).",
                            examples=["SignAndEncrypt", "Sign"]),
        ParameterDescriptor("client_cert_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "client application certificate used "
                                        "for certificate-based security.",
                            examples=["/aws_dda/opcua/client-cert.der"]),
        ParameterDescriptor("client_key_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "client certificate private key.",
                            examples=["/aws_dda/opcua/client-key.pem"]),
        ParameterDescriptor("server_cert_path", "string", required=False,
                            default=None, constraints={},
                            description="Optional path (on the device) to the "
                                        "server's certificate to pin/trust.",
                            examples=["/aws_dda/opcua/server-cert.der"]),
        *_trigger_policy_parameters(),
    ],
    # Executor-level OPC UA subscription (or poll loop) held by the
    # device's trigger subscription manager; the opcua Python lib is the
    # same packaged dependency the opcua_write node uses. Simulation: an
    # appsrc event source the test harness feeds from the Test_Dataset
    # instead of any session, mirroring digital_input (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="opcua_subscribe",
        plugin_dependencies=["python:opcua"],
    ) + [
        GstMapping(
            arch=ARCH_SIM,
            element_chain=[_element("appsrc", name="{sim_source_name}")],
            plugin_dependencies=["app"],
        ),
    ],
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Modbus TCP output node (modbus-tcp-output Requirements 1.1-1.6) — an
# OUTPUT-category node that, after a workflow run completes, writes one
# value to one coil or holding register on a Modbus TCP server (typically
# a PLC), gated by upstream conditional / inference_filter nodes exactly
# like digital_output / mqtt_publish / opcua_write.
# --------------------------------------------------------------------------

MODBUS_WRITE = NodeTypeDescriptor(
    type_id="modbus_write",
    category=CATEGORY_OUTPUT,
    display_name="Modbus TCP Write",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[],
    parameters=[
        ParameterDescriptor("host", "string", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Modbus TCP server (PLC) hostname "
                                        "or IP, e.g. 192.168.1.30.",
                            examples=["192.168.1.30", "plc.local"]),
        ParameterDescriptor("port", "int", required=False, default=502,
                            constraints={"min": 1, "max": 65535},
                            description="Modbus TCP port, e.g. 502 (the "
                                        "standard Modbus port).",
                            examples=[502]),
        ParameterDescriptor("unit_id", "int", required=False, default=1,
                            constraints={"min": 0, "max": 255},
                            description="Modbus unit (slave) id addressed "
                                        "by the write (0-255), e.g. 1.",
                            examples=[1, 0]),
        ParameterDescriptor("register_type", "enum", required=True,
                            default="coil",
                            constraints={"values": ["coil",
                                                    "holding_register"]},
                            description="Write target kind: coil (a single "
                                        "on/off bit, Write Single Coil "
                                        "function code 0x05) or "
                                        "holding_register (a 16-bit "
                                        "register, Write Single Register "
                                        "function code 0x06).",
                            examples=["coil", "holding_register"]),
        ParameterDescriptor("address", "int", required=True, default=None,
                            constraints={"min": 0, "max": 65535},
                            description="Address of the coil or holding "
                                        "register written (0-65535), "
                                        "e.g. 12.",
                            examples=[12, 40]),
        ParameterDescriptor("value_template", "string", required=False,
                            default="{is_anomalous}", constraints={},
                            description="Value written to the target. "
                                        "Placeholders in curly braces are "
                                        "replaced from the inference "
                                        "metadata: {is_anomalous}, "
                                        "{confidence}, or {inference_json}; "
                                        "a single placeholder keeps its "
                                        "native type. Coil writes coerce "
                                        "the rendered value to a boolean; "
                                        "holding-register writes coerce it "
                                        "to an integer 0-65535.",
                            examples=["{is_anomalous}", "{confidence}"]),
        ParameterDescriptor("pulse_ms", "int", required=False, default=0,
                            constraints={"min": 0, "max": 60000},
                            depends_on="register_type=coil",
                            description="Coil pulse duration in "
                                        "milliseconds (0-60000): 0 latches "
                                        "the written value (single write); "
                                        "a positive value writes the "
                                        "rendered value, waits pulse_ms "
                                        "milliseconds, then writes the "
                                        "inverse coil value, e.g. 250.",
                            examples=[0, 250]),
    ],
    # Executor-level Modbus TCP client write (stdlib socket exchange; no
    # packaged plugin dependency). Simulation: recording binding, no PLC
    # contact (Requirement 12.6).
    mappings=_same_on_device_archs(executor_binding="modbus_write")
             + [_recording_binding("modbus_write")],
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Custom Python source node (custom-python-source Requirements 1.1-1.8)
# --------------------------------------------------------------------------

CUSTOM_PYTHON_SOURCE = NodeTypeDescriptor(
    type_id="custom_python_source",
    category=CATEGORY_INPUT,
    display_name="Custom Python (Source)",
    # The activation port is how a subscription trigger starts the run
    # whose Trigger_Context the Frame_Producer receives.
    inputs=[PortDescriptor("activation", PORT_TYPE_EVENT_SIGNAL)],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("code", "code", required=True, default=None,
                            constraints={"min_length": 1},
                            description="Python run once per workflow run "
                                        "to produce the run's frame. Define "
                                        "produce_frame(context) and return "
                                        "the frame; context is the trigger "
                                        "context that started the run: for "
                                        "MQTT triggers the keys topic, "
                                        "payload, payload_json, qos and "
                                        "timestamp (payload_json is the "
                                        "payload parsed as JSON, or None "
                                        "when it does not parse); for "
                                        "OPC UA triggers the keys endpoint, "
                                        "node_id, value and "
                                        "source_timestamp; an empty dict "
                                        "for manual runs. Return a NumPy "
                                        "uint8 array (rows x cols "
                                        "grayscale, rows x cols x 3 BGR, "
                                        "or rows x cols x 4 BGRA — OpenCV "
                                        "channel order), or {'array': arr, "
                                        "'format': 'RGB'|'RGBA'|'GRAY8'} "
                                        "to use the array's bytes without "
                                        "channel conversion, or {'data': "
                                        "bytes, 'width': W, 'height': H, "
                                        "'format': ...} for raw bytes; "
                                        "returning None fails the run. "
                                        "cv2/np are pre-imported. Helpers: "
                                        "import dda_frames for "
                                        "load_image(source) -> BGR uint8 "
                                        "array and load_bytes(source) -> "
                                        "raw bytes, for local paths, "
                                        "s3://bucket/key URIs, and "
                                        "http(s):// URLs.",
                            examples=["def produce_frame(context):\n"
                                      "    import dda_frames\n"
                                      "    payload = context.get(\"payload_json\") or {}\n"
                                      "    return dda_frames.load_image(payload[\"image_url\"])",
                                      "def produce_frame(context):\n"
                                      "    import dda_frames\n"
                                      "    return dda_frames.load_image(\"s3://plant-images/reference.jpg\")"]),
        ParameterDescriptor("requirements", "string", required=False, default="",
                            constraints={},
                            description="Extra pip packages the code needs, "
                                        "one per line in requirements.txt "
                                        "form.",
                            examples=["requests==2.32.3"]),
        ParameterDescriptor("allowed_uri_prefixes", "string", required=False, default="",
                            constraints={},
                            description="Optional newline-separated list of "
                                        "URI prefixes that "
                                        "dda_frames.load_image and "
                                        "load_bytes may fetch from, e.g. "
                                        "s3://plant-images/; empty permits "
                                        "any source. The restriction "
                                        "applies only to fetches made "
                                        "through the dda_frames helpers — "
                                        "it is not a sandbox boundary.",
                            examples=["s3://plant-images/\nhttps://mes.local/"]),
    ],
    # The Frame_Producer runs in the LocalServer's Python_Bridge before
    # the pipeline starts; the produced frame is pushed into the compiled
    # appsrc through the existing single-frame Frame_Feed model — the
    # byte-for-byte Aravis chain (appsrc name compile-time rendered per
    # node via {nodeId}). Simulation: fed from the Test_Dataset like the
    # other hardware frame sources (Requirement 12.6).
    mappings=_same_on_device_archs(
        element_chain=[
            _element("appsrc", name="appsrc_{nodeId}"),
            _element("videoconvert"),
        ],
        plugin_dependencies=["app", "videoconvertscale"],
    ) + [_dataset_fed_sim_source()],
    hardware_dependent=True,
)

# --------------------------------------------------------------------------
# Metadata node (workflow-manager-gaps Requirement 6.1) — a
# POST_PROCESSING-category node that maps fields from the trigger
# payload (dotted field paths against the parsed payload) and attaches
# them, together with optional static JSON, to the data flowing to
# output nodes. Both structured values are carried as JSON-string
# parameters (ParameterDescriptor supports scalar types only); the
# shared validity rules live in catalog/metadata_config.py, consumed by
# the validator, the compiler, and (mirrored in TypeScript) the
# designer.
# --------------------------------------------------------------------------

METADATA = NodeTypeDescriptor(
    type_id="metadata",
    category=CATEGORY_POST_PROCESSING,
    display_name="Metadata",
    # InferenceMeta in/out ports let it sit between inference /
    # post-processing nodes and output nodes exactly like
    # inference_filter.
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        # JSON array of {"path": "...", "key": "..."} objects (0..50):
        # each entry maps a dotted field path in the trigger payload to
        # an output metadata key attached to downstream output results.
        ParameterDescriptor("mappings", "string", required=False, default="[]",
                            constraints={},
                            description="Metadata mappings as a JSON array "
                                        "of objects with 'path' (dotted "
                                        "field path resolved against the "
                                        "parsed trigger payload, e.g. "
                                        "job_id or order.id) and 'key' "
                                        "(output metadata key the resolved "
                                        "value is attached under). Up to 50 "
                                        "mappings; paths and keys must be "
                                        "non-empty and keys unique.",
                            examples=['[{"path": "job_id", "key": "job_id"}]',
                                      '[{"path": "order.id", "key": "order_id"},'
                                      ' {"path": "file_path", "key": "source_file"}]']),
        # Optional static JSON object, <= 10240 characters, whose
        # top-level entries are attached alongside resolved mappings.
        ParameterDescriptor("static_json", "string", required=False, default="",
                            constraints={"max_length": 10240},
                            description="Optional static JSON object (at "
                                        "most 10240 characters) whose "
                                        "top-level entries are attached to "
                                        "the output metadata alongside the "
                                        "resolved mappings; a resolved "
                                        "mapping wins on a key collision.",
                            examples=['{"station": "line-1"}']),
    ],
    # Executor-level metadata attachment (no GStreamer element): the
    # compiler emits a 'metadata' executor binding carrying the parsed
    # mappings, static JSON, and reachable output nodes; the stream
    # topology looks through it like inference_filter.
    mappings=_same_on_all_archs(executor_binding="metadata"),
    hardware_dependent=False,
)

# --------------------------------------------------------------------------
# Catalog access
# --------------------------------------------------------------------------

NODE_CATALOG = (
    CSI_CAMERA_SOURCE,
    ICAM_SOURCE,
    ARAVIS_CAMERA_SOURCE,
    FOLDER_SOURCE,
    DIGITAL_INPUT,
    DEWARP,
    ROTATE,
    CROP,
    FORMAT_CONVERT,
    CUSTOM_PYTHON_PREPROCESS,
    MODEL_INFERENCE,
    BEDROCK_INFERENCE,
    CUSTOM_PYTHON,
    INFERENCE_FILTER,
    CONDITIONAL,
    DIGITAL_OUTPUT,
    MQTT_PUBLISH,
    OPCUA_WRITE,
    CAPTURE,
    # Appended (additive — vllm-triton-inference Requirement 8.1): every
    # pre-existing descriptor keeps its position and content.
    LLM_INFERENCE,
    # Appended (additive — triggers-stage-and-unified-input Requirement 3.1):
    # every pre-existing descriptor keeps its position and content.
    UNIFIED_INPUT,
    # Appended (additive — trigger-activation-runtime Requirement 3.2):
    # every pre-existing descriptor keeps its position and content.
    MQTT_SUBSCRIBE,
    OPCUA_SUBSCRIBE,
    # Appended (additive — modbus-tcp-output Requirement 2.1): every
    # pre-existing descriptor keeps its position and content.
    MODBUS_WRITE,
    # Appended (additive — custom-python-source Requirement 11.4): every
    # pre-existing descriptor keeps its position and content.
    CUSTOM_PYTHON_SOURCE,
    # Appended (additive — workflow-manager-gaps Requirement 6.1): every
    # pre-existing descriptor keeps its position and content.
    METADATA,
)

_CATALOG_BY_ID = {descriptor.type_id: descriptor for descriptor in NODE_CATALOG}


def get_node_type(type_id: str) -> NodeTypeDescriptor | None:
    """Look up a node type descriptor by its type id, or None."""
    return _CATALOG_BY_ID.get(type_id)


def nodes_by_category() -> dict:
    """Catalog grouped by category, preserving catalog order — the shape
    the Node_Palette consumes (Requirement 1.1)."""
    grouped = {}
    for descriptor in NODE_CATALOG:
        grouped.setdefault(descriptor.category, []).append(descriptor)
    return grouped
