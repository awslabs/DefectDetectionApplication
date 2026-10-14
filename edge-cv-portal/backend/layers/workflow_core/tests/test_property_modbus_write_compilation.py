# Feature: modbus-tcp-output, Property 10: Compiler emits the binding with effective parameters
# Feature: modbus-tcp-output, Property 11: Simulation resolves to the recording stub
# Feature: modbus-tcp-output, Property 12: Generic V4 covers the required parameters
"""Property tests for ``modbus_write`` compilation and validation
(tasks 1.5, 1.6, 1.7).

Property 10 — for any valid workflow graph containing a ``modbus_write``
node with any valid parameter assignment, compiling for a device
architecture emits exactly one executor-binding entry with binding
``modbus_write``, the node id, the effective parameters (declared
defaults applied for omitted optionals), and the node's upstream node
ids — through the existing generic executor-binding emission.

Property 11 — for any valid workflow graph containing a ``modbus_write``
node, compiling with ``simulation=True`` resolves the node to its
``recording_modbus_write`` recording stub and emits no device
``modbus_write`` binding.

Property 12 — for any ``modbus_write`` node configuration missing an
effective value for any non-empty subset of ``host``, ``register_type``,
and ``address``, validation produces one
``V4_MISSING_REQUIRED_PARAMETER`` error finding per missing parameter
naming that node — through the existing generic required-parameter
check, with zero finding codes introduced by this feature.

Graphs come from the shared ``generators.py`` module's
``modbus_write_graph_strategy`` (1..3 modbus_write nodes wired behind an
InferenceMeta-producing chain, optionals randomly omitted so
effective-parameter resolution covers both explicit values and applied
defaults).

**Validates: Requirements 2.4, 2.5, 2.6**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import DEVICE_ARCHITECTURES, get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.validator import (
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    SEVERITY_ERROR,
    validate,
)
from workflow_core.validator import checks as _validator_checks

from .generators import modbus_write_graph_strategy

_MODBUS_DESCRIPTOR = get_node_type("modbus_write")


def _modbus_nodes(graph):
    return [node for node in graph.nodes if node.type == "modbus_write"]


def _effective_parameters(node_parameters):
    """The design's effective-parameter rule, computed independently of
    the compiler: declared defaults overlaid with the node's explicit
    values (Requirement 2.5)."""
    values = {p.name: p.default for p in _MODBUS_DESCRIPTOR.parameters}
    values.update(node_parameters)
    return values


def _upstream_node_ids(graph, node_id):
    """Ordered, deduplicated source node ids of the connections targeting
    ``node_id`` — the compiled entry's expected ``upstreamNodeIds``,
    computed independently of the compiler."""
    upstream = []
    for connection in graph.connections:
        if connection.target.node == node_id and \
                connection.source.node not in upstream:
            upstream.append(connection.source.node)
    return upstream


def _compiled(graph, arch, **kwargs):
    document = compile(graph, arch, **kwargs)
    assert isinstance(document, CompiledPipelineDocument), (
        "modbus_write graph failed to compile on {0}: {1}".format(
            arch, document))
    return document


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------

# Feature: modbus-tcp-output, Property 10: Compiler emits the binding with effective parameters
@settings(max_examples=100)
@given(graph=modbus_write_graph_strategy(),
       arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_compiler_emits_the_binding_with_effective_parameters(graph, arch):
    """**Feature: modbus-tcp-output, Property 10: Compiler emits the
    binding with effective parameters.**

    Exactly one executor-binding entry per ``modbus_write`` node, with
    binding ``modbus_write``, the node id, the effective parameters
    (declared defaults applied), and the node's upstream node ids.

    **Validates: Requirements 2.5**
    """
    document = _compiled(graph, arch)
    modbus_nodes = _modbus_nodes(graph)
    assert modbus_nodes, "generator must produce at least one modbus_write"

    modbus_entries = [entry for entry in document.executor_bindings
                      if entry["binding"] == "modbus_write"]
    assert len(modbus_entries) == len(modbus_nodes), (
        "expected {0} modbus_write binding entries, got {1}".format(
            len(modbus_nodes), modbus_entries))

    for node in modbus_nodes:
        entries = [entry for entry in modbus_entries
                   if entry["nodeId"] == node.id]
        assert len(entries) == 1, (
            "expected exactly one modbus_write entry for node '{0}', got "
            "{1}".format(node.id, entries))
        entry = entries[0]
        assert entry["binding"] == "modbus_write"
        assert entry["parameters"] == _effective_parameters(node.parameters), (
            "effective parameters mismatch for node '{0}'".format(node.id))
        assert entry["upstreamNodeIds"] == _upstream_node_ids(graph, node.id), (
            "upstreamNodeIds mismatch for node '{0}'".format(node.id))


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

# Feature: modbus-tcp-output, Property 11: Simulation resolves to the recording stub
@settings(max_examples=100)
@given(graph=modbus_write_graph_strategy(),
       arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_simulation_resolves_to_the_recording_stub(graph, arch):
    """**Feature: modbus-tcp-output, Property 11: Simulation resolves to
    the recording stub.**

    With ``simulation=True`` every ``modbus_write`` node compiles to its
    ``recording_modbus_write`` stub binding, and no device
    ``modbus_write`` binding is emitted anywhere in the document.

    **Validates: Requirements 2.6**
    """
    document = _compiled(graph, arch, simulation=True)
    modbus_nodes = _modbus_nodes(graph)
    assert modbus_nodes, "generator must produce at least one modbus_write"

    recording_entries = [entry for entry in document.executor_bindings
                         if entry["binding"] == "recording_modbus_write"]
    assert sorted(entry["nodeId"] for entry in recording_entries) == \
        sorted(node.id for node in modbus_nodes), (
            "every modbus_write node resolves to exactly one "
            "recording_modbus_write entry")

    assert not any(entry["binding"] == "modbus_write"
                   for entry in document.executor_bindings), (
        "no device modbus_write binding may appear in a simulation "
        "document")


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

#: The modbus_write parameters the generic V4 check must cover
#: (Requirement 2.4).
_V4_REQUIRED_PARAMETERS = ("host", "register_type", "address")

#: Every finding code the validator module defines — all pre-existing;
#: this feature introduces none, so every produced finding must carry
#: one of these codes.
_PRE_EXISTING_FINDING_CODES = frozenset(
    getattr(_validator_checks, name)
    for name in dir(_validator_checks) if name.startswith("CODE_")
)


@st.composite
def _modbus_graph_with_missing_required(draw):
    """A valid modbus_write graph with one modbus node stripped of a
    drawn non-empty subset of its required parameters.

    ``host`` / ``address`` declare default None, so a missing key and an
    explicit null are both "no effective value" — a drawn boolean covers
    both forms. ``register_type`` declares the valid default ``coil``
    (a default is a value, so omission satisfies V4); only an explicit
    null clears its effective value.
    """
    graph = draw(modbus_write_graph_strategy())
    node = draw(st.sampled_from(
        [n for n in graph.nodes if n.type == "modbus_write"]))
    missing = draw(st.sets(st.sampled_from(_V4_REQUIRED_PARAMETERS),
                           min_size=1))
    for name in sorted(missing):
        if name != "register_type" and draw(st.booleans()):
            node.parameters.pop(name, None)
        else:
            node.parameters[name] = None
    return graph, node.id, frozenset(missing)


# Feature: modbus-tcp-output, Property 12: Generic V4 covers the required parameters
@settings(max_examples=100)
@given(case=_modbus_graph_with_missing_required())
def test_generic_v4_covers_the_required_parameters(case):
    """**Feature: modbus-tcp-output, Property 12: Generic V4 covers the
    required parameters.**

    One ``V4_MISSING_REQUIRED_PARAMETER`` error finding per missing
    required parameter, each naming the node (and the parameter in its
    message), through the existing generic required-parameter check —
    and no finding codes introduced by this feature.

    **Validates: Requirements 2.4**
    """
    graph, node_id, missing = case
    findings = validate(graph)

    node_errors = [finding for finding in findings
                   if finding.node_id == node_id
                   and finding.severity == SEVERITY_ERROR]
    assert len(node_errors) == len(missing), (
        "expected exactly one error finding per missing parameter "
        "{0}, got {1}".format(sorted(missing), node_errors))
    for finding in node_errors:
        assert finding.code == CODE_V4_MISSING_REQUIRED_PARAMETER, finding
    for name in missing:
        named = [finding for finding in node_errors
                 if "'{0}'".format(name) in finding.message]
        assert len(named) == 1, (
            "expected exactly one V4 finding naming parameter '{0}', "
            "got {1}".format(name, node_errors))

    # Zero new check codes introduced by this feature (Requirement 2.4):
    # every finding carries a pre-existing validator code.
    unknown = {finding.code for finding in findings} - \
        _PRE_EXISTING_FINDING_CODES
    assert not unknown, unknown
