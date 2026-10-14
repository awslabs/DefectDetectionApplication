"""Port-type compatibility rules: exact match plus declared coercions.

A connection joins an output port (source) to an input port (target).
Compatibility is exact type match, plus an explicit coercion table
(Requirement 1.4, 4.2). The only declared coercion: ``InferenceMeta``
flows over the same GStreamer buffer stream as ``VideoFrames`` with
attached metadata, so an ``InferenceMeta`` output may feed a
``VideoFrames`` input (e.g. ``capture`` accepts both).
"""

from __future__ import annotations

from .models import (
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    PORT_TYPES,
)

#: Declared coercions: (source output type) -> set of additionally
#: acceptable target input types.
PORT_TYPE_COERCIONS = {
    PORT_TYPE_INFERENCE_META: frozenset({PORT_TYPE_VIDEO_FRAMES}),
}


def are_port_types_compatible(source_type: str, target_type: str) -> bool:
    """True when an output of ``source_type`` may connect to an input of
    ``target_type``: exact match or a declared coercion."""
    if source_type == target_type:
        return True
    return target_type in PORT_TYPE_COERCIONS.get(source_type, frozenset())


def incompatibility_reason(source_type: str, target_type: str) -> str | None:
    """A human-readable rejection reason, or None when compatible.

    Used by the Workflow_Builder to explain rejected connections
    (Requirement 1.4) and by validator check V2 (Requirement 4.2).
    """
    if source_type not in PORT_TYPES:
        return f"Unknown source port type '{source_type}'"
    if target_type not in PORT_TYPES:
        return f"Unknown target port type '{target_type}'"
    if are_port_types_compatible(source_type, target_type):
        return None
    return f"Cannot connect {source_type} output to {target_type} input"
