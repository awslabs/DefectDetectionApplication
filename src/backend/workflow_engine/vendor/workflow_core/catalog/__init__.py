"""Node type catalog: descriptors for every workflow node type.

Declares ports, port types, parameters (types, defaults, constraints),
per-architecture GStreamer mappings, executor bindings, plugin
dependencies, and hardware-dependence flags (Requirement 2.8), plus the
port-type compatibility rules and the per-arch LocalServer-bundled
plugin manifest.
"""

from .models import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_SIM,
    ARCH_X86_64,
    ARCHITECTURES,
    CATEGORIES,
    CATEGORY_INFERENCE,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_POST_PROCESSING,
    CATEGORY_PREPROCESSING,
    DEVICE_ARCHITECTURES,
    SIM_RECORDING_BINDING_PREFIX,
    GstMapping,
    NodeTypeDescriptor,
    PARAMETER_TYPES,
    ParameterDescriptor,
    PortDescriptor,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    PORT_TYPES,
)
from .compatibility import (
    PORT_TYPE_COERCIONS,
    are_port_types_compatible,
    incompatibility_reason,
)
from .bundled_plugins import (
    LOCALSERVER_BUNDLED_PLUGINS,
    bundled_plugins_for,
)
from .nodes import (
    CONDITION_EXAMPLES,
    CONDITION_LANGUAGE_DESCRIPTION,
    NODE_CATALOG,
    get_node_type,
    nodes_by_category,
)

__all__ = [
    # models
    "PortDescriptor",
    "ParameterDescriptor",
    "GstMapping",
    "NodeTypeDescriptor",
    "PORT_TYPES",
    "PORT_TYPE_VIDEO_FRAMES",
    "PORT_TYPE_INFERENCE_META",
    "PORT_TYPE_EVENT_SIGNAL",
    "CATEGORIES",
    "CATEGORY_INPUT",
    "CATEGORY_PREPROCESSING",
    "CATEGORY_INFERENCE",
    "CATEGORY_POST_PROCESSING",
    "CATEGORY_OUTPUT",
    "ARCHITECTURES",
    "DEVICE_ARCHITECTURES",
    "ARCH_X86_64",
    "ARCH_ARM64_JP4",
    "ARCH_ARM64_JP5",
    "ARCH_ARM64_JP6",
    "ARCH_SIM",
    "SIM_RECORDING_BINDING_PREFIX",
    "PARAMETER_TYPES",
    # compatibility rules
    "PORT_TYPE_COERCIONS",
    "are_port_types_compatible",
    "incompatibility_reason",
    # bundled plugin manifest
    "LOCALSERVER_BUNDLED_PLUGINS",
    "bundled_plugins_for",
    # catalog
    "NODE_CATALOG",
    "CONDITION_EXAMPLES",
    "CONDITION_LANGUAGE_DESCRIPTION",
    "get_node_type",
    "nodes_by_category",
]
