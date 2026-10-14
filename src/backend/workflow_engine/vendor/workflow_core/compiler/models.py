"""Data models for the Workflow_Compiler output.

``compile()`` returns either a :class:`CompiledPipelineDocument` or a
list of :class:`CompileError` records (validation failures or node types
lacking a GStreamer mapping on the target architecture — Requirement 6.5).

The Compiled Pipeline Document is the JSON artifact LocalServer renders
into a ``gst-launch``-style string:

- ``segments``: ordered element runs. Each segment's elements are joined
  with ``" ! "``; a segment with ``"from": "t0"`` hangs off the tee named
  ``t0`` (rendered as ``t0. ! ...``); a segment with ``"linkTo": "f1"``
  feeds the funnel named ``f1`` (rendered as ``... ! f1.``).
- Every element carries the ``nodeId`` of the workflow node it realizes;
  synthetic linking elements (``tee``, ``queue``, ``funnel``) carry
  ``nodeId: null``. Each node's element chain appears contiguously in
  exactly one segment (Requirement 6.6).
- ``executorBindings``: entries for executor-level nodes (digital input,
  MQTT publish, OPC UA write, inference filter, conditional) that have no
  GStreamer elements; each node appears exactly once here instead.
  Multi-output executor nodes additionally carry
  ``downstreamNodeIdsByPort`` (output port name -> downstream node ids);
  the ``conditional`` binding also carries ``portConditions`` — the gate
  condition per output port ("true" = the configured condition,
  "false" = its negation ``!(condition)``).
- ``pluginDependencies``: GStreamer plugins / Python runtime packages the
  pipeline needs beyond those bundled with LocalServer (Requirement 6.4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Schema version of the Compiled Pipeline Document format.
COMPILED_DOCUMENT_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Compile error codes
# --------------------------------------------------------------------------

#: The graph has validation errors; compilation is refused (Requirement 6.1).
CODE_VALIDATION_ERROR = "VALIDATION_ERROR"

#: A node type has no usable GStreamer mapping for the target
#: architecture (Requirement 6.5).
CODE_UNMAPPED_ARCHITECTURE = "UNMAPPED_ARCHITECTURE"


@dataclass(frozen=True)
class CompileError:
    """One compilation error.

    ``node_id`` and ``arch`` identify the failing node and the unsupported
    architecture for :data:`CODE_UNMAPPED_ARCHITECTURE` errors
    (Requirement 6.5). Validation-derived errors carry the offending
    node or connection id from the underlying finding.
    """

    code: str
    message: str
    node_id: Optional[str] = None
    connection_id: Optional[str] = None
    arch: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "nodeId": self.node_id,
            "connectionId": self.connection_id,
            "arch": self.arch,
        }


# --------------------------------------------------------------------------
# Compile context
# --------------------------------------------------------------------------

#: Default placeholder values resolved into element argument templates.
#: ``triton_model_repo`` / ``triton_server_path`` mirror the paths the
#: existing LocalServer builder passes to emltriton
#: (src/backend/dda_triton/constants.py) — Requirement 6.2.
DEFAULT_CONTEXT_VALUES = {
    "triton_model_repo": "/aws_dda/dda_triton/triton_model_repo",
    "triton_server_path": "/opt/tritonserver",
    "capture_meta": "",
}


@dataclass(frozen=True)
class CompileContext:
    """Ambient values for compilation.

    ``values`` supplies (or overrides) ``{placeholder}`` tokens used by
    catalog argument templates beyond node parameters, e.g.
    ``triton_model_repo``, ``dio_script_path``, ``dataset_location``.
    Placeholders with no value available are left untouched in the output
    so the edge renderer / test harness can resolve device-local paths.
    """

    workflow_id: str = ""
    workflow_version: str = ""
    values: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Compiled Pipeline Document
# --------------------------------------------------------------------------

@dataclass
class CompiledPipelineDocument:
    """The compiler's JSON output (Requirements 6.1-6.6)."""

    workflow_id: str
    workflow_version: str
    target_arch: str
    segments: List[dict] = field(default_factory=list)
    executor_bindings: List[dict] = field(default_factory=list)
    plugin_dependencies: List[str] = field(default_factory=list)
    schema_version: int = COMPILED_DOCUMENT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "workflowId": self.workflow_id,
            "workflowVersion": self.workflow_version,
            "targetArch": self.target_arch,
            "segments": self.segments,
            "executorBindings": self.executor_bindings,
            "pluginDependencies": list(self.plugin_dependencies),
        }

    def to_json(self) -> str:
        """Canonical JSON form (sorted keys, 2-space indent, ASCII)."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=True)

    def referenced_node_ids(self) -> List[str]:
        """Every node reference in the document, one entry per element
        chain occurrence or executor binding.

        Because each node's chain is emitted contiguously in exactly one
        segment, contiguous runs of the same ``nodeId`` count as a single
        reference; synthetic elements (``nodeId: null``) are skipped. A
        correct document lists every graph node exactly once
        (Requirement 6.6).
        """
        references: List[str] = []
        for segment in self.segments:
            previous: Any = object()
            for element in segment["elements"]:
                node_id = element["nodeId"]
                if node_id is not None and node_id != previous:
                    references.append(node_id)
                previous = node_id
        for binding in self.executor_bindings:
            references.append(binding["nodeId"])
        return references
