"""Example unit tests for the compiler's unified-input expansion (task 4.2).

Covers the `expand_unified_inputs` pre-pass wired into ``compile()``
(design C5):

- a ``unified_input`` node compiles to the SAME
  segments/executorBindings/pluginDependencies as a hand-placed
  underlying source node with the same id and params (3.6);
- an unconnected activation port emits no activation binding — the
  compiled document equals the hand-placed source document (3.9, 3.11);
- a connected `digital_input.out -> unified.activation` edge is dropped
  by expansion, leaving the dataflow output identical to the
  unconnected case while the digital_input still emits its ordinary
  executor binding (3.10, 3.11);
- unsupported-architecture parity: where the underlying source has no
  Device_Binding for an architecture, the unified node is unsupported
  in exactly the same way (3.7);
- a `folder` unified node with a missing ``location`` is deferred to the
  compile-time validation re-run (V4 missing-required on the expanded
  ``folder_source``), not raised on the unified graph pre-expansion.

_Requirements: 3.6, 3.7, 3.9, 3.10, 3.11_
"""

from workflow_core.catalog import (
    ARCH_ARM64_JP6,
    ARCH_X86_64,
    DEVICE_ARCHITECTURES,
    NODE_CATALOG,
)
from workflow_core.compiler import (
    CODE_UNMAPPED_ARCHITECTURE,
    CODE_VALIDATION_ERROR,
    CompiledPipelineDocument,
    CompileError,
    compile,
)
from workflow_core.compiler.compiler import expand_unified_inputs
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import SEVERITY_ERROR, validate
from workflow_core.validator.checks import CODE_V4_MISSING_REQUIRED_PARAMETER

# --------------------------------------------------------------------------
# Graph-building helpers (mirroring test_compiler_compile.py conventions)
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)

#: The four selectable source kinds and their underlying source types.
_SOURCE_TYPES = {
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "aravis_camera": "aravis_camera_source",
    "folder": "folder_source",
}


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _capture(node_id="cap"):
    return _node(node_id, "capture", output_path="/out")


def _unified_graph(source_kind, extra_connections=(), extra_nodes=(), **params):
    """unified_input('src', source_kind=...) -> capture('cap')."""
    unified = _node("src", "unified_input", source_kind=source_kind, **params)
    return WorkflowGraph(
        nodes=[unified, _capture()] + list(extra_nodes),
        connections=[_conn("c1", "src", "cap")] + list(extra_connections),
    )


def _source_graph(source_type, **params):
    """Hand-placed source('src') -> capture('cap'), same id as unified."""
    return WorkflowGraph(
        nodes=[_node("src", source_type, **params), _capture()],
        connections=[_conn("c1", "src", "cap")],
    )


def _compile_ok(graph, arch):
    result = compile(graph, arch)
    assert isinstance(result, CompiledPipelineDocument), (
        "expected a document, got errors: {0}".format(result)
    )
    return result


def _dataflow_parts(document):
    """The compiled dataflow output named by Requirement 3.6."""
    data = document.to_dict()
    return (
        data["segments"],
        data["executorBindings"],
        data["pluginDependencies"],
    )


# --------------------------------------------------------------------------
# Expansion parity with the hand-placed source (Requirement 3.6)
# --------------------------------------------------------------------------

class TestExpansionParity:
    def test_folder_kind_matches_hand_placed_folder_source(self):
        # arm64_jp6 (the PNG-staged folder chain) plus one other arch.
        for arch in (ARCH_ARM64_JP6, ARCH_X86_64):
            unified = _compile_ok(
                _unified_graph("folder", location="/data/images"), arch)
            hand_placed = _compile_ok(
                _source_graph("folder_source", location="/data/images"), arch)
            assert _dataflow_parts(unified) == _dataflow_parts(hand_placed), (
                "unified folder != hand-placed folder_source on {0}".format(arch)
            )

    def test_icam_kind_matches_hand_placed_icam_source(self):
        # Camera-kind parity (v4l2src executor chain), same two archs.
        for arch in (ARCH_ARM64_JP6, ARCH_X86_64):
            unified = _compile_ok(
                _unified_graph("icam", device="/dev/video0"), arch)
            hand_placed = _compile_ok(
                _source_graph("icam_source", device="/dev/video0"), arch)
            assert _dataflow_parts(unified) == _dataflow_parts(hand_placed), (
                "unified icam != hand-placed icam_source on {0}".format(arch)
            )

    def test_expanded_elements_carry_the_unified_node_id(self):
        # The synthetic source keeps the unified node's id (design C5).
        document = _compile_ok(
            _unified_graph("folder", location="/data/images"), ARCH_X86_64)
        assert "src" in document.referenced_node_ids()


# --------------------------------------------------------------------------
# Inert activation port (Requirements 3.9, 3.10, 3.11)
# --------------------------------------------------------------------------

class TestActivationPort:
    def test_unconnected_activation_port_emits_no_activation_binding(self):
        # 3.9/3.11: nothing activation-related is emitted — the unified
        # document is byte-equal to the hand-placed source document.
        unified = _compile_ok(
            _unified_graph("folder", location="/data/images"), ARCH_ARM64_JP6)
        hand_placed = _compile_ok(
            _source_graph("folder_source", location="/data/images"), ARCH_ARM64_JP6)
        assert unified.to_dict() == hand_placed.to_dict()

    def test_connected_activation_edge_is_dropped_by_expansion(self):
        # 3.10/3.11: digital_input.out -> unified.activation is dropped;
        # the dataflow output is identical to the unconnected case.
        trigger = _node("trig", "digital_input", pin=7)
        connected = _unified_graph(
            "folder",
            extra_nodes=[trigger],
            extra_connections=[_conn("a1", "trig", "src", target_port="activation")],
            location="/data/images",
        )
        unconnected = _unified_graph(
            "folder", extra_nodes=[trigger], location="/data/images")

        connected_doc = _compile_ok(connected, ARCH_ARM64_JP6)
        unconnected_doc = _compile_ok(unconnected, ARCH_ARM64_JP6)
        assert connected_doc.to_dict() == unconnected_doc.to_dict()

        # The feeding digital_input still emits its ordinary executor
        # binding — expansion drops only the activation edge, not the node.
        bindings = {b["nodeId"]: b for b in connected_doc.executor_bindings}
        assert bindings["trig"]["binding"] == "digital_input"
        assert bindings["trig"]["parameters"]["pin"] == 7


# --------------------------------------------------------------------------
# Unsupported-architecture parity (Requirement 3.7)
# --------------------------------------------------------------------------

class TestUnsupportedArchParity:
    # All four underlying sources define a Device_Binding on every physical
    # device architecture (each uses _same_on_device_archs or an explicit
    # per-arch list covering DEVICE_ARCHITECTURES), so among catalog
    # architectures 3.7 parity holds by construction — the unified node
    # expands into the source before mapping resolution, so it can never
    # resolve differently. The real unsupported case reachable today is an
    # architecture with no mapping on ANY source (e.g. an unknown arch id),
    # asserted below to fail identically for both graph shapes.

    def test_all_sources_map_every_device_architecture(self):
        # Documents the by-construction claim above against the live catalog.
        descriptors = {d.type_id: d for d in NODE_CATALOG}
        for source_type in _SOURCE_TYPES.values():
            for arch in DEVICE_ARCHITECTURES:
                assert descriptors[source_type].mapping_for(arch) is not None, (
                    "{0} unexpectedly lacks a mapping for {1} — replace this "
                    "test with a real per-arch parity assertion".format(
                        source_type, arch)
                )

    def test_unmapped_arch_fails_identically_for_unified_and_source(self):
        # Where the underlying source defines no mapping, the unified node
        # is unsupported in exactly the same way (same code/nodeId/arch).
        for source_kind, source_type, params in (
            ("folder", "folder_source", {"location": "/data/images"}),
            ("icam", "icam_source", {"device": "/dev/video0"}),
        ):
            unified_result = compile(
                _unified_graph(source_kind, **params), "riscv64")
            source_result = compile(
                _source_graph(source_type, **params), "riscv64")

            assert isinstance(unified_result, list) and unified_result
            assert isinstance(source_result, list) and source_result
            assert all(isinstance(e, CompileError) for e in unified_result)

            def _key(errors):
                return sorted((e.code, e.node_id, e.arch) for e in errors)

            assert _key(unified_result) == _key(source_result)
            assert all(
                e.code == CODE_UNMAPPED_ARCHITECTURE for e in unified_result)
            assert any(e.node_id == "src" and e.arch == "riscv64"
                       for e in unified_result)


# --------------------------------------------------------------------------
# Missing required parameter is deferred to the expanded source's V4
# --------------------------------------------------------------------------

class TestMissingLocationDeferredToExpandedV4:
    def test_pre_expansion_unified_graph_has_no_error_findings(self):
        # On the unified descriptor the union parameters are
        # required-relaxed, so the raw (pre-expansion) graph validates
        # clean — the missing location is NOT raised here.
        graph = _unified_graph("folder")  # no location
        errors = [f for f in validate(graph, NODE_CATALOG)
                  if f.severity == SEVERITY_ERROR]
        assert errors == []

    def test_expanded_folder_source_carries_the_standard_v4_finding(self):
        # The expanded graph re-validates against folder_source, which
        # keeps location required=True: the standard V4 missing-required
        # finding, attributed to the expanded node.
        graph = _unified_graph("folder")  # no location
        expanded = expand_unified_inputs(graph, NODE_CATALOG)
        assert [n.type for n in expanded.nodes if n.id == "src"] == ["folder_source"]

        v4 = [f for f in validate(expanded, NODE_CATALOG)
              if f.code == CODE_V4_MISSING_REQUIRED_PARAMETER]
        assert len(v4) == 1
        assert v4[0].node_id == "src"
        assert "location" in v4[0].message

    def test_compile_refuses_with_the_v4_validation_error(self):
        # compile() surfaces the deferred finding as its standard
        # validation refusal (no document is produced).
        result = compile(_unified_graph("folder"), ARCH_X86_64)  # no location
        assert isinstance(result, list) and result
        assert all(e.code == CODE_VALIDATION_ERROR for e in result)
        assert any(e.node_id == "src" and "location" in e.message
                   for e in result)
