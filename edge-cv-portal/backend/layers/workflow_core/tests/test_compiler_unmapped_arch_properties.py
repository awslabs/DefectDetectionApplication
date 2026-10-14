"""Property test for unmapped-architecture compile errors (task 4.7).

**Feature: workflow-manager, Property 8: Unmapped architecture yields
identifying compile errors**

For all Workflow_Definitions containing at least one node type with no
GStreamer mapping for the chosen target architecture, compilation fails
with errors that identify exactly those nodes and the unsupported
architecture, and compilation succeeds when all node types have mappings.

Every catalog node type currently declares mappings for all known
architectures, so the failing branch is exercised with unknown target
architecture strings (every node in the graph is then unmapped) and the
succeeding branch with the known architectures. The expected unmapped
node set is derived from the catalog per drawn graph and architecture,
so the property keeps holding if the catalog ever gains partially
mapped node types.

**Validates: Requirements 6.5**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import ARCHITECTURES, get_node_type
from workflow_core.compiler import (
    CODE_UNMAPPED_ARCHITECTURE,
    CompiledPipelineDocument,
    CompileError,
    compile,
)

from .generators import graph_strategy

# --------------------------------------------------------------------------
# Target architecture strategy
# --------------------------------------------------------------------------

#: Realistic-looking architecture identifiers no catalog mapping declares.
_UNKNOWN_ARCH_EXAMPLES = (
    "riscv64",
    "arm64_jp7",
    "x86",
    "X86_64",       # case matters: not the known "x86_64"
    " x86_64",      # surrounding whitespace is a different identifier
    "",
    "アーキテクチャ",
)

_unknown_architectures = st.one_of(
    st.sampled_from(_UNKNOWN_ARCH_EXAMPLES),
    st.text(max_size=16).filter(lambda arch: arch not in ARCHITECTURES),
)

#: Known architectures (every catalog node type maps all of them) mixed
#: with unknown ones, so both branches of the property are exercised.
_architectures = st.one_of(
    st.sampled_from(ARCHITECTURES),
    _unknown_architectures,
)


def _expected_unmapped_node_ids(graph, target_arch):
    """Node ids whose type has no GStreamer mapping for ``target_arch``,
    per the catalog declarations (Requirement 6.5)."""
    return {
        node.id
        for node in graph.nodes
        if get_node_type(node.type).mapping_for(target_arch) is None
    }


@given(graph=graph_strategy(), target_arch=_architectures)
def test_unmapped_architecture_yields_identifying_compile_errors(graph, target_arch):
    """**Feature: workflow-manager, Property 8: Unmapped architecture
    yields identifying compile errors**

    **Validates: Requirements 6.5**
    """
    expected_unmapped = _expected_unmapped_node_ids(graph, target_arch)

    result = compile(graph, target_arch)

    if not expected_unmapped:
        # Every node type has a mapping: compilation succeeds.
        assert isinstance(result, CompiledPipelineDocument), (
            "compilation failed although every node type has a mapping "
            "for {!r}: {}".format(target_arch, result)
        )
        assert result.target_arch == target_arch
        return

    # At least one node type lacks a mapping: compilation fails with
    # errors identifying exactly those nodes and the unsupported
    # architecture (Requirement 6.5).
    assert isinstance(result, list), (
        "expected compile errors for unmapped architecture {!r}, got a "
        "document".format(target_arch)
    )
    assert result, "compile returned an empty error list"
    assert all(isinstance(error, CompileError) for error in result)
    assert {error.code for error in result} == {CODE_UNMAPPED_ARCHITECTURE}, (
        "every error must be an unmapped-architecture error"
    )

    # Exactly the unmapped nodes are identified, each exactly once.
    reported_node_ids = [error.node_id for error in result]
    assert len(reported_node_ids) == len(set(reported_node_ids)), (
        "a node was reported more than once: {}".format(reported_node_ids)
    )
    assert set(reported_node_ids) == expected_unmapped, (
        "reported nodes {} != expected unmapped nodes {}".format(
            sorted(set(reported_node_ids)), sorted(expected_unmapped)
        )
    )

    # Every error names the unsupported architecture.
    assert all(error.arch == target_arch for error in result), (
        "every error must carry the unsupported architecture"
    )
