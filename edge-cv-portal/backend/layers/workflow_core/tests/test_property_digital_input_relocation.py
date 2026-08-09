# Feature: triggers-stage-and-unified-input, Property 2: digital_input relocation is binding-preserving
"""Property test P2 — the ``digital_input`` category relocation is
binding-preserving.

*For all* architectures and *for all* valid ``digital_input`` parameter
combinations (``pin``, ``trigger_edge``, ``poll_interval_ms``), the
compiled Device_Binding (executor binding + parameters + sim stub)
produced for a ``digital_input`` node after the category relocation
equals the Device_Binding produced before relocation.

Concretely, for every generated (pin, trigger_edge?, poll_interval_ms?,
device arch):

- (1) descriptor level — ``mapping_for(arch)`` yields
  ``executor_binding == "digital_input"`` with an empty element chain on
  every device architecture, and ``mapping_for(ARCH_SIM)`` yields the
  appsrc simulation stub, exactly as before relocation;
- (2) compile level — a valid ``digital_input -> custom_python
  (EventSignal) -> capture`` graph compiled on the generated device
  architecture emits a ``digital_input`` executor binding carrying
  exactly the generated parameters (``pin`` always; ``trigger_edge`` /
  ``poll_interval_ms`` when set, else their declared defaults);
- (3) category independence — the descriptor's ``category`` is
  ``CATEGORY_TRIGGER`` while (1) and (2) hold, proving ``category`` is
  presentation/validation metadata and not a compilation input.

**Validates: Requirements 2.5, 6.2**

Strategies are self-contained in this module (generators.py untouched).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import (
    ARCH_SIM,
    CATEGORY_TRIGGER,
    DEVICE_ARCHITECTURES,
    get_node_type,
)
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)

_POS = Position(0.0, 0.0)

#: Pre-relocation descriptor defaults (Requirement 2.2): the values the
#: compiler must fall back to when the optional parameters are unset.
_DEFAULT_TRIGGER_EDGE = "rising"
_DEFAULT_POLL_INTERVAL_MS = 100


# ---------------------------------------------------------------------------
# Self-contained strategy: valid digital_input parameter combos × device arch
# ---------------------------------------------------------------------------

#: pin is required (0-255); trigger_edge / poll_interval_ms are optional,
#: so the strategy sometimes omits them to exercise the default path.
_TRIGGER_PARAMS = st.fixed_dictionaries(
    {"pin": st.integers(min_value=0, max_value=255)},
    optional={
        "trigger_edge": st.sampled_from(["rising", "falling", "both"]),
        "poll_interval_ms": st.integers(min_value=10, max_value=60000),
    },
)


@st.composite
def relocation_cases(draw):
    params = draw(_TRIGGER_PARAMS)
    arch = draw(st.sampled_from(DEVICE_ARCHITECTURES))
    return params, arch


# ---------------------------------------------------------------------------
# Graph builder: digital_input('din') -> custom_python('py', EventSignal
# input) -> capture('cap'), the same wiring pattern as the task 2.3 unit
# tests (test_trigger_relocation_and_v7.py).
# ---------------------------------------------------------------------------

def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS,
                parameters=parameters)


def _conn(conn_id, source_node, target_node,
          source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _digital_input_graph(trigger_params):
    return WorkflowGraph(
        nodes=[
            _node("din", "digital_input", **trigger_params),
            _node("py", "custom_python",
                  code="def handle(x):\n    return x",
                  input_port_type="EventSignal"),
            _node("cap", "capture", output_path="/out"),
        ],
        connections=[
            _conn("c1", "din", "py"),
            _conn("c2", "py", "cap"),
        ],
    )


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(case=relocation_cases())
def test_digital_input_relocation_is_binding_preserving(case):
    """**Feature: triggers-stage-and-unified-input, Property 2:
    digital_input relocation is binding-preserving**

    **Validates: Requirements 2.5, 6.2**
    """
    trigger_params, arch = case
    descriptor = get_node_type("digital_input")
    assert descriptor is not None

    # (3) category independence: the descriptor is relocated to the
    # trigger category...
    assert descriptor.category == CATEGORY_TRIGGER

    # (1) ...while its per-architecture Device_Bindings are unchanged:
    # every device architecture resolves the executor-level binding with
    # an empty element chain (Requirement 2.4 baseline).
    for device_arch in DEVICE_ARCHITECTURES:
        mapping = descriptor.mapping_for(device_arch)
        assert mapping is not None, (
            "missing device mapping for {0}".format(device_arch))
        assert mapping.executor_binding == "digital_input"
        assert mapping.element_chain == []

    # (1, sim) ARCH_SIM still resolves the appsrc simulation stub.
    sim_mapping = descriptor.mapping_for(ARCH_SIM)
    assert sim_mapping is not None
    assert sim_mapping.executor_binding is None
    assert [element["factory"] for element in sim_mapping.element_chain] == \
        ["appsrc"]
    assert "app" in sim_mapping.plugin_dependencies

    # (2) compile level: the graph compiles on the generated device arch
    # and the digital_input executor binding carries exactly the
    # generated parameters (defaults where unset).
    document = compile(_digital_input_graph(trigger_params), arch)
    assert isinstance(document, CompiledPipelineDocument), (
        "compile failed on {0}: {1}".format(arch, document))

    din_entries = [binding for binding in document.executor_bindings
                   if binding["nodeId"] == "din"]
    assert len(din_entries) == 1, (
        "expected exactly one digital_input binding on {0}, got {1}".format(
            arch, document.executor_bindings))
    binding = din_entries[0]

    assert binding["binding"] == "digital_input"
    expected_parameters = {
        "pin": trigger_params["pin"],
        "trigger_edge": trigger_params.get(
            "trigger_edge", _DEFAULT_TRIGGER_EDGE),
        "poll_interval_ms": trigger_params.get(
            "poll_interval_ms", _DEFAULT_POLL_INTERVAL_MS),
    }
    assert binding["parameters"] == expected_parameters, (
        "binding parameters diverge on {0}: {1} != {2}".format(
            arch, binding["parameters"], expected_parameters))

    # Adjacency is the ordinary graph wiring: no upstream, the
    # custom_python consumer downstream.
    assert binding["upstreamNodeIds"] == []
    assert binding["downstreamNodeIds"] == ["py"]
