"""Property test for merged-catalog compilation (task 1.9).

**Feature: custom-node-designer, Property 12: Merged-catalog compilation includes custom plugin dependencies**

For all valid Workflow_Definitions over a merged catalog containing
custom node types, compilation for an architecture the custom types map
to includes each custom node's declared plugin dependency in the
compiled document's pluginDependencies, and compilation for an
architecture a custom type has no mapping for fails with an error
identifying that node and the unsupported architecture.

**Validates: Requirements 5.4, 8.6**
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from workflow_core.catalog import (
    DEVICE_ARCHITECTURES,
    bundled_plugins_for,
    descriptor_from_declaration,
    resolve_catalog,
)
from workflow_core.catalog.nodes import NODE_CATALOG
from workflow_core.compiler import (
    CODE_UNMAPPED_ARCHITECTURE,
    CompiledPipelineDocument,
    compile,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

from .generators import node_parameters_strategy

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

_NAME = st.text(alphabet="abcdefghij", min_size=1, max_size=8)

#: Custom plugin dependency names. The "customdep" prefix keeps them
#: disjoint from every LOCALSERVER_BUNDLED_PLUGINS entry, so a custom
#: node's declared dependency is never subtracted as "already bundled"
#: and must therefore appear verbatim in pluginDependencies.
_PLUGIN_DEPENDENCIES = st.lists(_NAME, min_size=1, max_size=2, unique=True).map(
    lambda names: ["customdep" + name for name in names]
)

#: Mid-pipeline palette categories for the custom types (V1 input/output
#: presence is provided by the built-in source and capture nodes).
_MID_CATEGORIES = ("preprocessing", "inference", "post_processing")

#: Built-in VideoFrames sources feeding the custom chain.
_VIDEO_INPUT_TYPES = ("icam_source", "folder_source")

_BUILTINS_BY_ID = {descriptor.type_id: descriptor for descriptor in NODE_CATALOG}


@st.composite
def _custom_declarations(draw):
    """1..3 uniquely named custom VideoFrames->VideoFrames declarations.

    Each declaration maps a random nonempty subset of the device
    architectures that always includes a shared ``common_arch`` (so a
    target every custom type maps to is guaranteed to exist), and every
    mapping declares at least one plugin dependency.
    """
    common_arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    count = draw(st.integers(min_value=1, max_value=3))
    names = draw(st.lists(_NAME, min_size=count, max_size=count, unique=True))

    declarations = []
    for name in names:
        extra_archs = draw(
            st.sets(st.sampled_from(DEVICE_ARCHITECTURES), max_size=2)
        )
        archs = sorted({common_arch} | extra_archs)
        declarations.append({
            "typeId": "custom." + name,
            "displayName": "Custom " + name,
            "category": draw(st.sampled_from(_MID_CATEGORIES)),
            "hardwareDependent": False,
            "inputs": [{"name": "in", "portType": "VideoFrames"}],
            "outputs": [{"name": "out", "portType": "VideoFrames"}],
            "parameters": [],
            "mappings": [
                {
                    "arch": arch,
                    "elementChain": [{"factory": "identity"}],
                    "pluginDependencies": draw(_PLUGIN_DEPENDENCIES),
                }
                for arch in archs
            ],
        })
    return declarations, common_arch


@st.composite
def merged_catalog_cases(draw):
    """(graph, custom descriptors, merged catalog, common_arch).

    The graph is a valid Workflow_Definition over the merged catalog:
    a built-in VideoFrames source, a chain of every generated custom
    node, and a built-in capture output node.
    """
    declarations, common_arch = draw(_custom_declarations())
    descriptors = [descriptor_from_declaration(decl) for decl in declarations]
    merged = resolve_catalog(descriptors)

    source_type = draw(st.sampled_from(_VIDEO_INPUT_TYPES))
    nodes = [
        Node(
            id="src",
            type=source_type,
            position=Position(x=0.0, y=0.0),
            parameters=draw(node_parameters_strategy(_BUILTINS_BY_ID[source_type])),
        )
    ]
    connections = []
    previous = "src"
    for index, descriptor in enumerate(descriptors):
        node_id = "custom-{0}".format(index)
        nodes.append(Node(
            id=node_id,
            type=descriptor.type_id,
            position=Position(x=float(index + 1), y=0.0),
            parameters={},
        ))
        connections.append(Connection(
            id="c{0}".format(len(connections)),
            source=PortEndpoint(node=previous, port="out"),
            target=PortEndpoint(node=node_id, port="in"),
        ))
        previous = node_id
    nodes.append(Node(
        id="sink",
        type="capture",
        position=Position(x=float(len(descriptors) + 1), y=0.0),
        parameters=draw(node_parameters_strategy(_BUILTINS_BY_ID["capture"])),
    ))
    connections.append(Connection(
        id="c{0}".format(len(connections)),
        source=PortEndpoint(node=previous, port="out"),
        target=PortEndpoint(node="sink", port="in"),
    ))

    graph = WorkflowGraph(nodes=nodes, connections=connections)
    return graph, descriptors, merged, common_arch


def _expected_plugin_dependencies(graph, merged, target_arch):
    """Independent recomputation of the compiled dependency set: union of
    the used mappings' declared dependencies minus the per-arch bundled
    set, over the *merged* catalog."""
    descriptors_by_id = {descriptor.type_id: descriptor for descriptor in merged}
    declared = set()
    for node in graph.nodes:
        mapping = descriptors_by_id[node.type].mapping_for(target_arch)
        declared.update(mapping.plugin_dependencies)
    return declared - bundled_plugins_for(target_arch)


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

@given(case=merged_catalog_cases())
def test_merged_catalog_compilation_includes_custom_plugin_dependencies(case):
    """**Feature: custom-node-designer, Property 12: Merged-catalog compilation includes custom plugin dependencies**

    **Validates: Requirements 5.4, 8.6**
    """
    graph, descriptors, merged, common_arch = case
    descriptors_by_id = {descriptor.type_id: descriptor for descriptor in merged}
    custom_node_ids = {
        node.id: descriptors_by_id[node.type]
        for node in graph.nodes
        if node.type.startswith("custom.")
    }

    # --- mapped architecture: compilation succeeds and carries every
    # custom node's declared plugin dependency (Requirement 8.6) --------
    result = compile(graph, common_arch, catalog=merged)
    assert isinstance(result, CompiledPipelineDocument), (
        "compile failed for a valid merged-catalog graph on '{0}': {1}".format(
            common_arch, result
        )
    )
    compiled_dependencies = set(result.plugin_dependencies)
    for node_id, descriptor in custom_node_ids.items():
        mapping = descriptor.mapping_for(common_arch)
        for dependency in mapping.plugin_dependencies:
            assert dependency in compiled_dependencies, (
                "custom node '{0}' dependency '{1}' missing from "
                "pluginDependencies {2}".format(
                    node_id, dependency, sorted(compiled_dependencies)
                )
            )

    # The full set still follows the compiler's dependency rule over the
    # merged catalog (built-in and custom descriptors treated alike).
    expected = _expected_plugin_dependencies(graph, merged, common_arch)
    assert compiled_dependencies == expected, (
        "pluginDependencies {0} != expected {1} for arch '{2}'".format(
            sorted(compiled_dependencies), sorted(expected), common_arch
        )
    )

    # --- unmapped architectures: compilation fails identifying the node
    # and the unsupported architecture (Requirement 5.4) ----------------
    for arch in DEVICE_ARCHITECTURES:
        missing_ids = {
            node_id
            for node_id, descriptor in custom_node_ids.items()
            if descriptor.mapping_for(arch) is None
        }
        if not missing_ids:
            continue

        errors = compile(graph, arch, catalog=merged)
        assert isinstance(errors, list) and errors, (
            "compile unexpectedly succeeded on '{0}' despite unmapped "
            "custom nodes {1}".format(arch, sorted(missing_ids))
        )

        unmapped_errors = [
            error for error in errors if error.code == CODE_UNMAPPED_ARCHITECTURE
        ]
        assert len(unmapped_errors) == len(errors), (
            "unexpected non-unmapped compile errors: {0}".format(errors)
        )

        # Exactly the nodes without a mapping for this architecture are
        # reported, each error naming the node and the architecture.
        reported = {(error.node_id, error.arch) for error in unmapped_errors}
        assert reported == {(node_id, arch) for node_id in missing_ids}, (
            "unmapped errors {0} != expected nodes {1} on '{2}'".format(
                sorted(reported), sorted(missing_ids), arch
            )
        )
        for error in unmapped_errors:
            assert error.node_id in error.message
            assert arch in error.message
