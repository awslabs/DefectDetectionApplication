"""Data models and constants for the node type catalog.

These dataclasses are the shared vocabulary between the portal Lambdas,
the cloud test sandbox, and the LocalServer workflow engine: every node
type declares its ports, parameters, per-architecture GStreamer mappings,
executor bindings, plugin dependencies, and hardware-dependence flag
(Requirement 2.8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Port types (Requirement 2.8)
# --------------------------------------------------------------------------

PORT_TYPE_VIDEO_FRAMES = "VideoFrames"
PORT_TYPE_INFERENCE_META = "InferenceMeta"
PORT_TYPE_EVENT_SIGNAL = "EventSignal"

PORT_TYPES = (
    PORT_TYPE_VIDEO_FRAMES,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_EVENT_SIGNAL,
)

# --------------------------------------------------------------------------
# Node categories (Requirements 2.1-2.5)
# --------------------------------------------------------------------------

CATEGORY_TRIGGER = "trigger"
CATEGORY_INPUT = "input"
CATEGORY_PREPROCESSING = "preprocessing"
CATEGORY_INFERENCE = "inference"
CATEGORY_POST_PROCESSING = "post_processing"
CATEGORY_OUTPUT = "output"

CATEGORIES = (
    CATEGORY_TRIGGER,
    CATEGORY_INPUT,
    CATEGORY_PREPROCESSING,
    CATEGORY_INFERENCE,
    CATEGORY_POST_PROCESSING,
    CATEGORY_OUTPUT,
)

# --------------------------------------------------------------------------
# Target architectures
# --------------------------------------------------------------------------

ARCH_X86_64 = "x86_64"
ARCH_X86_64_NVIDIA = "x86_64_nvidia"
ARCH_ARM64_JP4 = "arm64_jp4"
ARCH_ARM64_JP5 = "arm64_jp5"
ARCH_ARM64_JP6 = "arm64_jp6"
ARCH_ARM64_JP7 = "arm64_jp7"
ARCH_SIM = "sim"

#: All architectures a workflow can be compiled for. ``sim`` is the
#: cloud-side test sandbox (x86_64 container, CPU Triton).
#: ``x86_64_nvidia`` is an x86_64 device with the NVIDIA GPU runtime.
ARCHITECTURES = (
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_SIM,
)

#: Architectures that correspond to physical edge devices.
DEVICE_ARCHITECTURES = (
    ARCH_X86_64,
    ARCH_X86_64_NVIDIA,
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_ARM64_JP7,
)

#: Feature flag for vLLM support on JetPack 5 devices. No viable vLLM
#: build exists for the JetPack 5 stack (CUDA 11.4 / Python 3.8), so
#: every JP5 vLLM touchpoint — the ``llm_inference`` catalog mapping,
#: the packaging supported-architecture set, and the deployment gate —
#: derives from this single flag and stays off by default. Flipping it
#: on cannot affect JP6 behavior; leaving it off leaves JP5 behavior
#: identical to before the vLLM feature.
JP5_VLLM_ENABLED = False

#: Executor-binding name prefix for simulation recording stubs: in
#: simulation-mode compilation, hardware output nodes (digital output,
#: MQTT publish, OPC UA write) bind to ``recording_<type>`` bindings that
#: log would-be actuations instead of touching any endpoint
#: (Requirement 12.6).
SIM_RECORDING_BINDING_PREFIX = "recording_"

# --------------------------------------------------------------------------
# Parameter types
# --------------------------------------------------------------------------

PARAM_TYPE_STRING = "string"
PARAM_TYPE_INT = "int"
PARAM_TYPE_FLOAT = "float"
PARAM_TYPE_BOOL = "bool"
PARAM_TYPE_ENUM = "enum"
PARAM_TYPE_CODE = "code"
PARAM_TYPE_MODEL_REF = "model_ref"

PARAMETER_TYPES = (
    PARAM_TYPE_STRING,
    PARAM_TYPE_INT,
    PARAM_TYPE_FLOAT,
    PARAM_TYPE_BOOL,
    PARAM_TYPE_ENUM,
    PARAM_TYPE_CODE,
    PARAM_TYPE_MODEL_REF,
)


@dataclass(frozen=True)
class PortDescriptor:
    """A typed attachment point on a node where a connection begins or ends."""

    name: str  # e.g. "in", "out"
    port_type: str  # one of PORT_TYPES


@dataclass(frozen=True)
class ParameterDescriptor:
    """A configurable node parameter with type, default, and constraints.

    ``constraints`` keys by parameter type:
      - int/float: ``min``, ``max``
      - string/code: ``min_length``, ``max_length``, ``regex``
      - enum: ``values`` (list of allowed values)
      - int with a discrete value set: ``values``

    ``depends_on`` declares conditional visibility in one of two forms:

      - a bare parameter name: the name of a bool parameter on the same
        node type. While that parameter's effective value is false (or
        absent), the configuration UI hides this parameter's control —
        the original bool-truthy semantics, unchanged for every
        existing descriptor.
      - ``"name=value"``: the name of a parameter on the same node type
        plus a literal, e.g. ``"mode=poll"``. This parameter's control
        is visible only while the named parameter's effective value
        (its explicit value, else its declared default) equals the
        literal when both are rendered as strings — used for
        enum-selection gating.

    None (the default) means always visible, so existing node types are
    unaffected.

    ``description`` is a concise human-readable explanation of the
    parameter — what it is, the expected format, and a short example
    value where useful — rendered by the configuration UI as field-level
    help. None (the default) keeps older descriptors backward
    compatible.

    ``examples`` is a list of working example values for the parameter:
    each entry satisfies the parameter's own type and constraints and
    can be used verbatim. The configuration UI may offer them as
    fill-in suggestions next to the field help. None (the default)
    keeps older descriptors backward compatible.
    """

    name: str
    param_type: str  # one of PARAMETER_TYPES
    required: bool
    default: Any | None = None
    constraints: dict = field(default_factory=dict)
    depends_on: str | None = None
    description: str | None = None
    examples: list | None = None


@dataclass(frozen=True)
class GstMapping:
    """How a node type is realized on one target architecture.

    ``element_chain`` is an ordered list of ``{"factory": str,
    "args_template": dict}`` entries. Argument template values may contain
    ``{placeholder}`` tokens resolved by the compiler from node parameter
    values or the compile context (e.g. ``{triton_model_repo}``,
    ``{dio_script_path}``, ``{dataset_location}``).

    Executor-level nodes (no GStreamer element) use an empty
    ``element_chain`` and set ``executor_binding`` to the binding name the
    LocalServer WorkflowExecutor processes (e.g. ``"mqtt_publish"``).

    ``plugin_dependencies`` names every GStreamer plugin (or ``python:<pkg>``
    runtime dependency) the mapping relies on; the compiler subtracts the
    per-arch LocalServer-bundled manifest to obtain the set that must be
    packaged with the Workflow_Component (Requirement 6.4).
    """

    arch: str  # one of ARCHITECTURES
    element_chain: list = field(default_factory=list)
    executor_binding: str | None = None
    plugin_dependencies: list = field(default_factory=list)


@dataclass(frozen=True)
class NodeTypeDescriptor:
    """Full declaration of a workflow node type (Requirement 2.8)."""

    type_id: str
    category: str  # one of CATEGORIES
    display_name: str
    inputs: list  # list[PortDescriptor]
    outputs: list  # list[PortDescriptor]
    parameters: list  # list[ParameterDescriptor]
    mappings: list  # list[GstMapping]
    hardware_dependent: bool  # drives test-runner stubbing (Requirement 12.6)

    def mapping_for(self, arch: str) -> GstMapping | None:
        """Return this node type's mapping for ``arch``, or None."""
        for mapping in self.mappings:
            if mapping.arch == arch:
                return mapping
        return None
