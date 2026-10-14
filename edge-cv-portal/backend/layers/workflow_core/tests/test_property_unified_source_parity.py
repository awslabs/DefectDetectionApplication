# Feature: triggers-stage-and-unified-input, Property 3: Unified node compiles to its underlying source binding
"""Property test P3 — the unified node compiles to its underlying source binding.

*For all* `source_kind` values, *for all* equivalent parameter sets, and
*for all* device architectures, compiling a `unified_input` node with a
given id yields segments, executor bindings, and plugin dependencies
identical to compiling the corresponding existing source descriptor
(`SOURCE_KIND_TO_SOURCE_TYPE[source_kind]`) with the same id and
equivalent parameter values; the unified node's gated parameter subset
for each `source_kind` matches that source descriptor's parameters on
name, type, default, and constraints; and where the underlying source
has no Device_Binding for an architecture, the unified node is
unsupported on that architecture in the same way.

Concretely, for every generated (source_kind, params, arch):

- (1) compiling unified_input('src', source_kind=..., params) ->
  capture('cap') and the hand-placed underlying source ('src', same
  params) -> capture('cap') both succeed and produce documents equal on
  full `to_dict()` (hence equal segments / executorBindings /
  pluginDependencies) — Requirement 3.6;
- (2) the unified descriptor's parameter subset named by the underlying
  descriptor matches the underlying descriptor's parameters on
  name / param_type / default / constraints, with `required=False`
  being the sole permitted difference — Requirement 3.4;
- (3) compiling both graphs on an unknown architecture ("riscv64")
  fails with identical error sets (code / node_id / arch) —
  Requirement 3.7.

**Validates: Requirements 3.4, 3.6, 3.7**

Strategies are self-contained in this module (generators.py untouched).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.compiler import (
    CompiledPipelineDocument,
    CompileError,
    compile,
)
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

_POS = Position(0.0, 0.0)

#: An architecture id no source descriptor maps (unsupported-arch parity).
_UNKNOWN_ARCH = "riscv64"

#: source_kind -> underlying source type (mirrors SOURCE_KIND_TO_SOURCE_TYPE).
_SOURCE_TYPES = {
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "aravis_camera": "aravis_camera_source",
    "folder": "folder_source",
}

# ---------------------------------------------------------------------------
# Self-contained strategies
# ---------------------------------------------------------------------------

_SAFE_ALPHABET = "abcdefgh0123456789_-"


def _safe_string(prefix=""):
    return st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=12).map(
        lambda s: prefix + s)


# Valid generated params per underlying source descriptor
# (folder requires location, optional file_pattern; icam requires device;
# aravis requires camera_id, optional gain/exposure; csi has only
# optional gain/exposure).
_GAIN = st.integers(min_value=0, max_value=100)
_EXPOSURE = st.integers(min_value=0, max_value=20000000)

_PARAMS_BY_KIND = {
    "csi_camera": st.fixed_dictionaries(
        {},
        optional={"gain": _GAIN, "exposure": _EXPOSURE},
    ),
    "icam": st.fixed_dictionaries(
        {"device": _safe_string("/dev/")},
    ),
    "aravis_camera": st.fixed_dictionaries(
        {"camera_id": _safe_string()},
        optional={"gain": _GAIN, "exposure": _EXPOSURE},
    ),
    "folder": st.fixed_dictionaries(
        {"location": _safe_string("/")},
        optional={"file_pattern": _safe_string("*.")},
    ),
}


@st.composite
def parity_cases(draw):
    source_kind = draw(st.sampled_from(sorted(_SOURCE_TYPES)))
    params = draw(_PARAMS_BY_KIND[source_kind])
    arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    return source_kind, params, arch


# ---------------------------------------------------------------------------
# Graph builders: source('src') -> capture('cap')
# ---------------------------------------------------------------------------

def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=parameters)


def _graph(source_node):
    return WorkflowGraph(
        nodes=[source_node, _node("cap", "capture", output_path="/out")],
        connections=[Connection(
            id="c1",
            source=PortEndpoint("src", "out"),
            target=PortEndpoint("cap", "in"),
        )],
    )


def _unified_graph(source_kind, params):
    return _graph(_node("src", "unified_input",
                        source_kind=source_kind, **params))


def _source_graph(source_kind, params):
    """Hand-placed underlying source with the same id and params."""
    return _graph(_node("src", _SOURCE_TYPES[source_kind], **params))


def _compile_ok(graph, arch, label):
    result = compile(graph, arch)
    assert isinstance(result, CompiledPipelineDocument), (
        "(1) compile failed for {0} on {1}: {2}".format(label, arch, result))
    return result


def _error_key(errors):
    return sorted((e.code, e.node_id, e.arch) for e in errors)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(case=parity_cases())
def test_unified_node_compiles_to_underlying_source_binding(case):
    """**Feature: triggers-stage-and-unified-input, Property 3: Unified
    node compiles to its underlying source binding**

    **Validates: Requirements 3.4, 3.6, 3.7**
    """
    source_kind, params, arch = case
    source_type = _SOURCE_TYPES[source_kind]

    # (1) Binding parity (3.6): the unified graph and the hand-placed
    # underlying source graph compile to the same document — full
    # to_dict equality covers segments / executorBindings /
    # pluginDependencies.
    unified_doc = _compile_ok(
        _unified_graph(source_kind, params), arch, "unified_input")
    hand_placed_doc = _compile_ok(
        _source_graph(source_kind, params), arch, source_type)
    assert unified_doc.to_dict() == hand_placed_doc.to_dict(), (
        "unified({0}) != hand-placed {1} on {2}".format(
            source_kind, source_type, arch))

    # (2) Parameter equivalence (3.4): the unified descriptor's subset
    # named by the underlying descriptor matches it on
    # name/param_type/default/constraints; required=False is the sole
    # permitted difference.
    unified_params = {p.name: p for p in
                      get_node_type("unified_input").parameters}
    underlying = get_node_type(source_type)
    for source_param in underlying.parameters:
        unified_param = unified_params.get(source_param.name)
        assert unified_param is not None, (
            "unified_input is missing {0} parameter '{1}'".format(
                source_type, source_param.name))
        assert unified_param.param_type == source_param.param_type
        assert unified_param.default == source_param.default
        assert unified_param.constraints == source_param.constraints
        assert unified_param.required is False, (
            "union parameter '{0}' must be required-relaxed".format(
                source_param.name))

    # (3) Unsupported-arch parity (3.7): on an unknown architecture both
    # graph shapes fail with identical error sets (code/node_id/arch).
    unified_errors = compile(_unified_graph(source_kind, params),
                             _UNKNOWN_ARCH)
    source_errors = compile(_source_graph(source_kind, params),
                            _UNKNOWN_ARCH)
    assert isinstance(unified_errors, list) and unified_errors
    assert isinstance(source_errors, list) and source_errors
    assert all(isinstance(e, CompileError) for e in unified_errors)
    assert all(isinstance(e, CompileError) for e in source_errors)
    assert _error_key(unified_errors) == _error_key(source_errors), (
        "unified({0}) and {1} fail differently on unknown arch".format(
            source_kind, source_type))
