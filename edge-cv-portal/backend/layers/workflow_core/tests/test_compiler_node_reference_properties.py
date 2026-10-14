"""Property test for compiler node reference exactness (task 4.3).

**Feature: workflow-manager, Property 4: Compiler references every node exactly once**

For all valid Workflow_Definitions, the compiled pipeline document
references every node in the definition exactly once — the multiset of
``nodeId`` tags across all segment elements and executor bindings equals
the definition's node set with multiplicity one.

**Validates: Requirements 6.6, 6.1**
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile

from .generators import graph_strategy


@given(graph=graph_strategy(), target_arch=st.sampled_from(ARCHITECTURES))
def test_compiler_references_every_node_exactly_once(graph, target_arch):
    """**Feature: workflow-manager, Property 4: Compiler references every node exactly once**

    **Validates: Requirements 6.6, 6.1**
    """
    document = compile(graph, target_arch)

    # Valid graphs compile: every catalog node type is mapped on every
    # target architecture (Requirement 6.1).
    assert isinstance(document, CompiledPipelineDocument), (
        "compile() failed for a valid graph on arch {!r}: {}".format(
            target_arch, document
        )
    )

    # The multiset of nodeId references across all segment element chains
    # and executor bindings equals the definition's node set with
    # multiplicity one (Requirement 6.6). referenced_node_ids() yields one
    # entry per contiguous element-chain occurrence or executor binding,
    # skipping synthetic tee/queue/funnel elements (nodeId null).
    expected = Counter(node.id for node in graph.nodes)
    actual = Counter(document.referenced_node_ids())
    assert actual == expected, (
        "compiled document node references differ from the graph's node set: "
        "missing={}, extra_or_duplicate={}".format(
            sorted(expected - actual), sorted(actual - expected)
        )
    )
