"""Workflow_Compiler: compiles validated graphs to Compiled Pipeline Documents.

Topologically sorts the DAG, emits one element chain (or executor binding)
per node, linearizes connections with tee/queue for fan-out, and computes
plugin dependencies per target architecture (Requirements 6.1-6.6).

``compile(graph, target_arch, context, simulation)`` returns a
:class:`CompiledPipelineDocument` on success or the complete list of
:class:`CompileError` records on failure.

With ``simulation=True``, hardware-dependent nodes (per the catalog flag)
map to recording stubs — dataset-fed sources via ``multifilesrc``/``appsrc``
and ``recording_*`` executor bindings for hardware outputs — while
non-hardware nodes compile identically to non-simulation output
(Requirement 12.6).
"""

from .models import (
    CODE_UNMAPPED_ARCHITECTURE,
    CODE_VALIDATION_ERROR,
    COMPILED_DOCUMENT_SCHEMA_VERSION,
    CompileContext,
    CompiledPipelineDocument,
    CompileError,
    DEFAULT_CONTEXT_VALUES,
)
from .compiler import compile

__all__ = [
    "compile",
    "CompileContext",
    "CompiledPipelineDocument",
    "CompileError",
    "CODE_VALIDATION_ERROR",
    "CODE_UNMAPPED_ARCHITECTURE",
    "COMPILED_DOCUMENT_SCHEMA_VERSION",
    "DEFAULT_CONTEXT_VALUES",
]
