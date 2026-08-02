"""Property test for the compiler's plugin dependency set (task 4.6).

**Feature: workflow-manager, Property 7: Plugin dependency set correctness**

For all valid Workflow_Definitions and target architectures, the
compiler's ``pluginDependencies`` output equals the union of the
catalog-declared plugin dependencies of the definition's nodes for that
architecture minus the LocalServer-bundled plugin set for that
architecture.

**Validates: Requirements 6.4**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    DEVICE_ARCHITECTURES,
    bundled_plugins_for,
    get_node_type,
)
from workflow_core.compiler import CompiledPipelineDocument, compile

from .generators import graph_strategy


def _expected_plugin_dependencies(graph, target_arch):
    """Independent recomputation of the Requirement 6.4 rule: union of the
    used mappings' declared dependencies minus the per-arch bundled set."""
    declared = set()
    for node in graph.nodes:
        mapping = get_node_type(node.type).mapping_for(target_arch)
        assert mapping is not None, (
            "catalog has no '{0}' mapping for node type '{1}'".format(
                target_arch, node.type
            )
        )
        declared.update(mapping.plugin_dependencies)
    return declared - bundled_plugins_for(target_arch)


@given(graph=graph_strategy(), target_arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_plugin_dependency_set_correctness(graph, target_arch):
    """**Feature: workflow-manager, Property 7: Plugin dependency set correctness**

    **Validates: Requirements 6.4**
    """
    result = compile(graph, target_arch)
    assert isinstance(result, CompiledPipelineDocument), (
        "compile failed for a valid graph on '{0}': {1}".format(target_arch, result)
    )

    expected = _expected_plugin_dependencies(graph, target_arch)

    # The output names each dependency exactly once (it is a set).
    assert len(result.plugin_dependencies) == len(set(result.plugin_dependencies)), (
        "pluginDependencies contains duplicates: {0}".format(result.plugin_dependencies)
    )

    # pluginDependencies == union of used mappings' declared dependencies
    # minus the LocalServer-bundled set for the target architecture
    # (Requirement 6.4).
    assert set(result.plugin_dependencies) == expected, (
        "pluginDependencies {0} != expected {1} for arch '{2}'".format(
            sorted(result.plugin_dependencies), sorted(expected), target_arch
        )
    )
