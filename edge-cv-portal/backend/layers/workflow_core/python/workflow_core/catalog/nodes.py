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

from .models import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_SIM,
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
    ARCHITECTURES,
    DEVICE_ARCHITECTURES,
    SIM_RECORDING_BINDING_PREFIX,
    CATEGORY_INFERENCE,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_POST_PROCESSING,
    CATEGORY_PREPROCESSING,
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
    Shared by camera_source and folder_source."""
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

CAMERA_SOURCE = NodeTypeDescriptor(
    type_id="camera_source",
    category=CATEGORY_INPUT,
    display_name="Camera Source",
    inputs=[],
    outputs=[PortDescriptor("out", PORT_TYPE_VIDEO_FRAMES)],
    parameters=[
        ParameterDescriptor("device", "string", required=False, default="/dev/video0",
                            constraints={"min_length": 1},
                            description="Camera device path on the edge "
                                        "device, e.g. /dev/video0.",
                            examples=["/dev/video0", "/dev/video1"]),
        ParameterDescriptor("gain", "int", required=False, default=4,
                            constraints={"min": 0, "max": 100},
                            description="Sensor gain (0-100). Higher values "
                                        "brighten the image but add noise; "
                                        "e.g. 4.",
                            examples=[4, 10]),
        ParameterDescriptor("exposure", "int", required=False, default=5000000,
                            constraints={"min": 0},
                            description="Sensor exposure time in "
                                        "nanoseconds, e.g. 5000000 (5 ms).",
                            examples=[5000000, 16000000]),
    ],
    mappings=[
        # USB/V4L2 camera on generic x86_64 devices.
        GstMapping(
            arch=ARCH_X86_64,
            element_chain=[
                _element("v4l2src", device="{device}"),
                _element("videoconvert"),
            ],
            plugin_dependencies=["video4linux2", "videoconvertscale"],
        ),
        # x86_64 with the NVIDIA GPU runtime: same V4L2 capture path as
        # plain x86_64 (an NVIDIA-accelerated chain may be declared later).
        GstMapping(
            arch=ARCH_X86_64_NVIDIA,
            element_chain=[
                _element("v4l2src", device="{device}"),
                _element("videoconvert"),
            ],
            plugin_dependencies=["video4linux2", "videoconvertscale"],
        ),
        # Existing appsrc-fed camera path on JetPack 4/5 (frames pushed by
        # the LocalServer camera adapter, as in _add_camera_image_source).
        GstMapping(
            arch=ARCH_ARM64_JP4,
            element_chain=[
                _element("appsrc", name="appsrc"),
                _element("videoconvert"),
            ],
            plugin_dependencies=["app", "videoconvertscale"],
        ),
        GstMapping(
            arch=ARCH_ARM64_JP5,
            element_chain=[
                _element("appsrc", name="appsrc"),
                _element("videoconvert"),
            ],
            plugin_dependencies=["app", "videoconvertscale"],
        ),
        # JP6 NVIDIA CSI host-service file capture path (PNG staged).
        GstMapping(
            arch=ARCH_ARM64_JP6,
            element_chain=_jp6_png_staged_chain("/aws_dda/nvidia-csi-capture/latest.jpg.dda_decoded.png"),
            plugin_dependencies=["coreelements", "png", "videoconvertscale", "python:pillow"],
        ),
        # Simulation: fed from the Test_Dataset (Requirement 12.6).
        _dataset_fed_sim_source(),
    ],
    hardware_dependent=True,
)

FOLDER_SOURCE = NodeTypeDescriptor(
    type_id="folder_source",
    category=CATEGORY_INPUT,
    display_name="Folder Source",
    inputs=[],
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
    category=CATEGORY_INPUT,
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

#: Default comparison prompt for the Bedrock Inference node. The model
#: must answer with the JSON shape the shared condition evaluator
#: consumes ({is_anomalous, confidence}).
BEDROCK_DEFAULT_PROMPT = (
    "Compare the input image to the reference image. Respond with JSON: "
    '{"is_anomalous": true|false, "confidence": 0..1} where is_anomalous '
    "is true when the input meaningfully differs from the reference."
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
                                        "both images. The model must answer "
                                        "with JSON of the shape "
                                        '{"is_anomalous": true|false, '
                                        '"confidence": 0..1}; those fields '
                                        "become the inference metadata "
                                        "(is_anomalous, confidence) driving "
                                        "downstream filters, conditionals, "
                                        "and outputs.",
                            examples=[BEDROCK_DEFAULT_PROMPT]),
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
        ParameterDescriptor("broker_host", "string", required=True, default=None,
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
    # Executor-level MQTT client publish on pipeline completion.
    # Simulation: recording binding, no broker contact (Requirement 12.6).
    mappings=_same_on_device_archs(
        executor_binding="mqtt_publish",
        plugin_dependencies=["python:paho-mqtt"],
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
# Catalog access
# --------------------------------------------------------------------------

NODE_CATALOG = (
    CAMERA_SOURCE,
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
