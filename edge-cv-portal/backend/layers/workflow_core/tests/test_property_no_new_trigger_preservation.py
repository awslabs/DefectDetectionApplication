# Feature: trigger-activation-runtime, Property 8: No-new-trigger compilation and packaging are preserved
"""Property test P8 — no-new-trigger compilation and packaging are preserved.

*For any* generated graph containing no ``mqtt_subscribe`` and no
``opcua_subscribe`` node — including Zero_Trigger_Workflows and graphs
wiring a ``digital_input`` output to an ``activation`` port — the
compiled document is byte-identical to the pre-feature output for the
same graph, with no trigger-related key present
(``golden_zero_trigger_compilation.json`` anchors the zero-trigger
case).

A literal pre-feature compiler is not runnable in-process, so this
module implements the strongest in-process equivalents (mirroring the
oracle approach of ``test_property_zero_trigger_preservation.py``):

(a) **No trigger-related key**: for generated no-new-trigger graphs the
    compiled document contains no ``"activates"`` key anywhere — the
    only serialization addition this feature makes (design D2) — so the
    document shape equals the pre-feature shape. The check is
    structural (dict keys only), so generated parameter *values* can
    never defeat it.

(b) **digital_input→activation twin equivalence**: a graph wiring a
    ``digital_input`` output to an ``activation`` port (on a
    ``unified_input`` node or on any of the four legacy sources)
    compiles byte-identically to its twin graph with that edge removed
    — the edge is still dropped as inert exactly as before the feature
    (Requirement 5.3), which is the strong equivalence oracle: the
    pre-feature compiler dropped the edge, so twin-equality proves
    output equality with the pre-feature compiler.

(c) **Golden anchor**: the committed
    ``golden_zero_trigger_compilation.json`` comparison (captured
    before this feature and unchanged by it) still passes, anchoring
    the zero-trigger case to real pre-feature bytes (Requirement 12.1).

**Validates: Requirements 5.3, 5.4, 12.1**
"""

from __future__ import annotations

import json
import os

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

# Shared valid-graph strategy: built exclusively from pre-existing node
# types (it never places mqtt_subscribe/opcua_subscribe), so every
# generated graph is a no-new-trigger graph by construction.
from .generators import graph_strategy

# Golden helpers from the sub-feature A preservation test: re-running the
# same golden comparison here anchors the zero-trigger case (oracle (c)).
from .test_property_zero_trigger_preservation import (
    _GOLDEN_PATH,
    _canonical_bytes,
    _current_golden_payload,
)

#: The two subscribe-side trigger node types this feature adds; their
#: absence is what "no-new-trigger" means.
_NEW_TRIGGER_TYPES = frozenset({"mqtt_subscribe", "opcua_subscribe"})

#: The only serialization addition the feature makes to compiled
#: documents (design D2): the ``activates`` key on trigger executor
#: bindings. No-new-trigger documents must not contain it anywhere.
_TRIGGER_KEY = "activates"

_POS = Position(0.0, 0.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_no_trigger_key(payload, where):
    """Recursively assert no dict key equals ``activates``.

    Structural (keys only): generated parameter values may contain any
    string without defeating the check.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key != _TRIGGER_KEY, (
                "trigger-related key '{0}' leaked into a no-new-trigger "
                "compiled document ({1})".format(_TRIGGER_KEY, where)
            )
            _assert_no_trigger_key(value, where)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_trigger_key(item, where)


def _fingerprint(result):
    """Canonical byte form of a compile() outcome (document or errors)."""
    if isinstance(result, CompiledPipelineDocument):
        return result.to_json()
    return json.dumps(
        [error.to_dict() for error in result],
        sort_keys=True, indent=2, ensure_ascii=True,
    )


# ---------------------------------------------------------------------------
# Part 1 — generated no-new-trigger graphs carry no trigger-related key
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(graph=graph_strategy())
def test_no_new_trigger_documents_carry_no_trigger_key(graph):
    """# Feature: trigger-activation-runtime, Property 8: No-new-trigger compilation and packaging are preserved

    Oracle (a): for any generated graph without the new trigger types
    (the shared strategy never places them; ``digital_input`` may be
    present), the compiled document for every device architecture
    contains no ``activates`` key anywhere — the compiled-document
    serialization omits every new trigger-related field
    (Requirement 5.4).

    **Validates: Requirements 5.3, 5.4, 12.1**
    """
    # No-new-trigger by construction; assert so the oracle stays honest
    # if the shared generator ever changes.
    present = {node.type for node in graph.nodes} & _NEW_TRIGGER_TYPES
    assert not present, "generator unexpectedly placed {0}".format(present)

    for arch in DEVICE_ARCHITECTURES:
        result = compile(graph, arch)
        if isinstance(result, CompiledPipelineDocument):
            _assert_no_trigger_key(result.to_dict(), "arch={0}".format(arch))


# ---------------------------------------------------------------------------
# Part 2 — digital_input→activation twin equivalence (strong oracle)
# ---------------------------------------------------------------------------
#
# Self-contained strategies (mirroring test_property_activation_port_inert):
# an activation-target node (unified_input or one of the four legacy
# sources, all of which declare an inert `activation` port) feeding a
# capture sink, plus a digital_input wired to the activation port. The
# twin graph is identical minus that edge (the detached digital_input
# node remains: CATEGORY_TRIGGER nodes are V5 BFS roots, so both graphs
# stay valid).

_SAFE_STRING = st.text(alphabet="bdefgh0123456789_-", min_size=1, max_size=12)

_GAIN = st.integers(min_value=0, max_value=100)
_EXPOSURE = st.integers(min_value=0, max_value=20000000)

#: target key -> (node type, params strategy, extra fixed params)
_ACTIVATION_TARGETS = {
    "csi_camera_source": st.fixed_dictionaries(
        {}, optional={"gain": _GAIN, "exposure": _EXPOSURE}),
    "icam_source": st.fixed_dictionaries(
        {"device": _SAFE_STRING.map(lambda s: "/dev/" + s)}),
    "aravis_camera_source": st.fixed_dictionaries(
        {"camera_id": _SAFE_STRING},
        optional={"gain": _GAIN, "exposure": _EXPOSURE}),
    "folder_source": st.fixed_dictionaries(
        {"location": _SAFE_STRING.map(lambda s: "/" + s)},
        optional={"file_pattern": _SAFE_STRING.map(lambda s: "*." + s)}),
}

#: unified_input source_kind -> underlying source type (for parameter reuse).
_UNIFIED_KINDS = {
    "csi_camera": "csi_camera_source",
    "icam": "icam_source",
    "aravis_camera": "aravis_camera_source",
    "folder": "folder_source",
}

_DIGITAL_INPUT_PARAMS = st.fixed_dictionaries(
    {"pin": st.integers(min_value=0, max_value=255)},
    optional={
        "trigger_edge": st.sampled_from(["rising", "falling", "both"]),
        "poll_interval_ms": st.integers(min_value=10, max_value=60000),
    },
)


@st.composite
def digital_activation_cases(draw):
    unified = draw(st.booleans())
    if unified:
        kind = draw(st.sampled_from(sorted(_UNIFIED_KINDS)))
        params = dict(draw(_ACTIVATION_TARGETS[_UNIFIED_KINDS[kind]]))
        params["source_kind"] = kind
        target_type = "unified_input"
    else:
        target_type = draw(st.sampled_from(sorted(_ACTIVATION_TARGETS)))
        params = draw(_ACTIVATION_TARGETS[target_type])
    trigger_params = draw(_DIGITAL_INPUT_PARAMS)
    arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    return target_type, params, trigger_params, arch


def _graphs_with_and_without_edge(target_type, params, trigger_params):
    """The digital_input→activation graph and its edge-stripped twin.

    Both graphs share the same nodes (including the digital_input node);
    only the activation connection differs.
    """
    def build(with_edge):
        nodes = [
            Node(id="src", type=target_type, position=_POS,
                 parameters=dict(params)),
            Node(id="cap", type="capture", position=_POS,
                 parameters={"output_path": "/out"}),
            Node(id="trig", type="digital_input", position=_POS,
                 parameters=dict(trigger_params)),
        ]
        connections = [
            Connection(id="c1",
                       source=PortEndpoint(node="src", port="out"),
                       target=PortEndpoint(node="cap", port="in")),
        ]
        if with_edge:
            connections.append(
                Connection(id="a1",
                           source=PortEndpoint(node="trig", port="out"),
                           target=PortEndpoint(node="src", port="activation")))
        return WorkflowGraph(nodes=nodes, connections=connections)

    return build(True), build(False)


@settings(max_examples=100)
@given(case=digital_activation_cases())
def test_digital_input_activation_edge_compiles_identically_to_twin(case):
    """# Feature: trigger-activation-runtime, Property 8: No-new-trigger compilation and packaging are preserved

    Oracle (b): a graph wiring a ``digital_input`` output to an
    ``activation`` port compiles byte-identically to its twin with that
    edge removed — the edge stays inert exactly as before the feature
    (Requirement 5.3) — and neither document carries an ``activates``
    key (Requirement 5.4).

    **Validates: Requirements 5.3, 5.4, 12.1**
    """
    target_type, params, trigger_params, arch = case
    with_edge, twin = _graphs_with_and_without_edge(
        target_type, params, trigger_params)

    result_with = compile(with_edge, arch)
    result_twin = compile(twin, arch)

    assert isinstance(result_with, CompiledPipelineDocument), (
        "digital_input→activation graph failed to compile on {0}: "
        "{1}".format(arch, result_with))
    assert isinstance(result_twin, CompiledPipelineDocument), (
        "edge-stripped twin failed to compile on {0}: {1}".format(
            arch, result_twin))

    assert _fingerprint(result_with) == _fingerprint(result_twin), (
        "digital_input activation edge changed compiled bytes for "
        "target={0} on {1}".format(target_type, arch))

    _assert_no_trigger_key(result_with.to_dict(),
                           "digital_input case, arch={0}".format(arch))


# ---------------------------------------------------------------------------
# Part 3 — golden anchor: the committed zero-trigger golden still passes
# ---------------------------------------------------------------------------

def test_zero_trigger_golden_comparison_unchanged():
    """# Feature: trigger-activation-runtime, Property 8: No-new-trigger compilation and packaging are preserved

    Oracle (c): the committed ``golden_zero_trigger_compilation.json``
    comparison passes unchanged — the extended compiler's output for the
    fixed legacy zero-trigger workflows is byte-identical to the
    pre-feature capture on every device architecture (Requirement 12.1).

    **Validates: Requirements 5.3, 5.4, 12.1**
    """
    assert os.path.exists(_GOLDEN_PATH), (
        "golden file missing: {0}".format(_GOLDEN_PATH))
    with open(_GOLDEN_PATH) as handle:
        golden = handle.read()

    current = _canonical_bytes(_current_golden_payload()) + "\n"
    assert current == golden, (
        "compiled output for the legacy zero-trigger workflows no longer "
        "matches the committed golden capture — zero-trigger compilation "
        "changed (Requirement 12.1 violated)")

    # And the golden documents themselves carry no trigger-related key.
    _assert_no_trigger_key(json.loads(golden), "golden payload")
