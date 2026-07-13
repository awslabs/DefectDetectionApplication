"""compile(): valid workflow graphs -> Compiled Pipeline Documents.

Algorithm (design section 5, Requirements 6.1-6.6):

1. Re-run the validator; refuse to compile when any error-severity
   finding exists.
2. Resolve every node's GstMapping for the target architecture; node
   types lacking a usable mapping yield CompileError{nodeId, arch}
   (Requirement 6.5).
3. Topologically sort the stream DAG (GStreamer-mapped nodes; executor
   -level nodes are collapsed out of the stream but kept as executor
   bindings).
4. Emit one element chain per GStreamer node, tagged with its nodeId,
   contiguously in exactly one segment (Requirements 6.1, 6.6). Model
   inference nodes carry the emltriton chain configured with the model
   name and the Triton repo/server paths (Requirement 6.2).
5. Linearize connections: fan-out gets ``tee name=t<i>`` with a
   ``queue`` at the head of each branch (Requirement 6.3); fan-in gets a
   named ``funnel`` the converging branches link into.
6. pluginDependencies = union of the used mappings' declared
   dependencies minus the per-arch LocalServer-bundled set
   (Requirement 6.4).

Simulation mode (Requirement 12.6): ``compile(..., simulation=True)``
resolves hardware-dependent node types (per the catalog flag) to their
``sim``-architecture recording stubs — dataset-fed sources
(``multifilesrc``/``appsrc``) for hardware inputs and ``recording_*``
executor bindings for hardware outputs — while every other node resolves
its ``target_arch`` mapping exactly as in non-simulation compilation, so
non-hardware nodes compile identically.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Union

from ..catalog import ARCH_SIM, ARCHITECTURES, bundled_plugins_for, get_node_type
from ..catalog.models import GstMapping, NodeTypeDescriptor
from ..serializer.models import Node, WorkflowGraph
from ..validator import SEVERITY_ERROR, validate
from .models import (
    CODE_UNMAPPED_ARCHITECTURE,
    CODE_VALIDATION_ERROR,
    CompileContext,
    CompiledPipelineDocument,
    CompileError,
    DEFAULT_CONTEXT_VALUES,
)

__all__ = ["compile"]

#: The two-path routing executor binding (the conditional node): the
#: compiler emits per-port gate conditions for it ("true" = the
#: configured condition, "false" = its negation), so downstream executor
#: bindings of each output port are gated exactly like downstream of an
#: inference_filter.
BINDING_CONDITIONAL = "conditional"


def compile(
    graph: WorkflowGraph,
    target_arch: str,
    context: Optional[CompileContext] = None,
    simulation: bool = False,
) -> Union[CompiledPipelineDocument, List[CompileError]]:
    """Compile ``graph`` for ``target_arch``.

    With ``simulation=True``, hardware-dependent node types resolve their
    ``sim``-architecture recording stubs instead of the ``target_arch``
    mapping; all other nodes compile identically to non-simulation output
    (Requirement 12.6).

    Returns a :class:`CompiledPipelineDocument` on success, or the
    complete list of :class:`CompileError` records on failure (validation
    errors, or nodes without a mapping for the architecture).
    """
    context = context or CompileContext()

    # 1. Re-run validation; refuse to compile on errors (Requirement 6.1).
    validation_errors = [
        finding for finding in validate(graph) if finding.severity == SEVERITY_ERROR
    ]
    if validation_errors:
        return [
            CompileError(
                CODE_VALIDATION_ERROR,
                finding.message,
                node_id=finding.node_id,
                connection_id=finding.connection_id,
            )
            for finding in validation_errors
        ]

    # 2. Resolve mappings; collect every unmapped node (Requirement 6.5).
    #    In simulation mode, hardware-dependent node types resolve their
    #    sim-architecture recording stubs instead (Requirement 12.6); an
    #    unknown target architecture stays an error for every node.
    stub_hardware = simulation and target_arch in ARCHITECTURES
    mappings: Dict[str, GstMapping] = {}
    unmapped: List[CompileError] = []
    for node in graph.nodes:
        descriptor = get_node_type(node.type)  # known: validation passed
        node_arch = (
            ARCH_SIM
            if stub_hardware and descriptor.hardware_dependent
            else target_arch
        )
        mapping = descriptor.mapping_for(node_arch)
        if mapping is None or (not mapping.element_chain and not mapping.executor_binding):
            unmapped.append(CompileError(
                CODE_UNMAPPED_ARCHITECTURE,
                "Node '{0}' (type '{1}') has no GStreamer mapping for "
                "architecture '{2}'".format(node.id, node.type, node_arch),
                node_id=node.id,
                arch=node_arch,
            ))
        else:
            mappings[node.id] = mapping
    if unmapped:
        return unmapped

    nodes_by_id = {node.id: node for node in graph.nodes}
    gst_node_ids = [n.id for n in graph.nodes if mappings[n.id].element_chain]
    executor_node_ids = [n.id for n in graph.nodes if not mappings[n.id].element_chain]

    # Node-level adjacency from connections (deduplicated, order-stable).
    successors = _node_successors(graph)

    # 3. Collapse executor-level nodes out of the stream and
    #    topologically sort the remaining GStreamer nodes.
    gst_set = set(gst_node_ids)
    stream_out = {
        node_id: _stream_successors(node_id, successors, gst_set)
        for node_id in gst_node_ids
    }
    stream_in: Dict[str, List[str]] = {node_id: [] for node_id in gst_node_ids}
    for source, targets in stream_out.items():
        for target in targets:
            stream_in[target].append(source)
    topo_order = _topological_sort(gst_node_ids, stream_out, stream_in)

    # 4./5. Emit tagged element chains linearized into segments.
    chains = {
        node_id: _resolve_chain(nodes_by_id[node_id], mappings[node_id], context)
        for node_id in gst_node_ids
    }
    segments = _build_segments(topo_order, stream_out, stream_in, chains)

    # Executor bindings, one entry per executor-level node (Requirement 6.6).
    predecessors = _node_predecessors(graph)
    port_successors = _node_port_successors(graph)
    executor_bindings = []
    for node_id in executor_node_ids:
        node = nodes_by_id[node_id]
        descriptor = get_node_type(node.type)
        parameters = _effective_parameters(node, descriptor)
        entry = {
            "nodeId": node_id,
            "binding": mappings[node_id].executor_binding,
            "parameters": parameters,
            "upstreamNodeIds": predecessors.get(node_id, []),
            "downstreamNodeIds": successors.get(node_id, []),
        }
        # Multi-output executor nodes additionally record which
        # downstream nodes hang off which output port, so the executors
        # can gate each side independently (the conditional node).
        if len(descriptor.outputs) > 1:
            by_port = port_successors.get(node_id, {})
            entry["downstreamNodeIdsByPort"] = {
                port.name: by_port.get(port.name, [])
                for port in descriptor.outputs
            }
        # The conditional node gates each output port with a condition:
        # the "true" port with the configured condition, the "false" port
        # with its negation — composed exactly as the shared condition
        # evaluator parses it (unary '!').
        if mappings[node_id].executor_binding == BINDING_CONDITIONAL:
            condition = str(parameters.get("condition") or "")
            entry["portConditions"] = {
                "true": condition,
                "false": "!({0})".format(condition),
            }
        executor_bindings.append(entry)

    # 6. Plugin dependencies beyond the LocalServer-bundled set
    #    (Requirement 6.4).
    declared = set()
    for mapping in mappings.values():
        declared.update(mapping.plugin_dependencies)
    plugin_dependencies = sorted(declared - bundled_plugins_for(target_arch))

    return CompiledPipelineDocument(
        workflow_id=context.workflow_id,
        workflow_version=context.workflow_version,
        target_arch=target_arch,
        segments=segments,
        executor_bindings=executor_bindings,
        plugin_dependencies=plugin_dependencies,
    )


# --------------------------------------------------------------------------
# Graph adjacency
# --------------------------------------------------------------------------

def _node_successors(graph: WorkflowGraph) -> Dict[str, List[str]]:
    """Node id -> deduplicated, connection-ordered successor node ids."""
    successors: Dict[str, List[str]] = {node.id: [] for node in graph.nodes}
    for connection in graph.connections:
        source, target = connection.source.node, connection.target.node
        if source in successors and target in successors:
            if target not in successors[source]:
                successors[source].append(target)
    return successors


def _node_port_successors(graph: WorkflowGraph) -> Dict[str, Dict[str, List[str]]]:
    """Node id -> output port name -> deduplicated, connection-ordered
    successor node ids (the per-port refinement of _node_successors,
    used for multi-output executor nodes)."""
    known = {node.id for node in graph.nodes}
    by_port: Dict[str, Dict[str, List[str]]] = {}
    for connection in graph.connections:
        source, target = connection.source.node, connection.target.node
        if source in known and target in known:
            targets = by_port.setdefault(source, {}).setdefault(
                connection.source.port, [])
            if target not in targets:
                targets.append(target)
    return by_port


def _node_predecessors(graph: WorkflowGraph) -> Dict[str, List[str]]:
    """Node id -> deduplicated, connection-ordered predecessor node ids."""
    predecessors: Dict[str, List[str]] = {node.id: [] for node in graph.nodes}
    for connection in graph.connections:
        source, target = connection.source.node, connection.target.node
        if source in predecessors and target in predecessors:
            if source not in predecessors[target]:
                predecessors[target].append(source)
    return predecessors


def _stream_successors(
    node_id: str,
    successors: Dict[str, List[str]],
    gst_set: set,
) -> List[str]:
    """The GStreamer nodes ``node_id`` streams into, looking through
    executor-level nodes (which have no pipeline elements)."""
    result: List[str] = []
    seen = {node_id}
    frontier = list(successors.get(node_id, []))
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current in gst_set:
            if current not in result:
                result.append(current)
        else:
            frontier.extend(successors.get(current, []))
    return result


def _topological_sort(
    node_ids: List[str],
    stream_out: Dict[str, List[str]],
    stream_in: Dict[str, List[str]],
) -> List[str]:
    """Kahn's algorithm; deterministic (seeded in graph node order)."""
    in_degree = {node_id: len(stream_in[node_id]) for node_id in node_ids}
    ready = [node_id for node_id in node_ids if in_degree[node_id] == 0]
    order: List[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in stream_out[current]:
            in_degree[target] -= 1
            if in_degree[target] == 0:
                ready.append(target)
    # The validator guarantees acyclicity (V3), so everything is ordered.
    return order


# --------------------------------------------------------------------------
# Element chain resolution
# --------------------------------------------------------------------------

_SINGLE_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


class _LenientDict(dict):
    """Leaves unknown ``{placeholder}`` tokens intact for later
    resolution (e.g. device-local paths supplied by the edge renderer)."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _effective_parameters(node: Node, descriptor: NodeTypeDescriptor) -> Dict[str, Any]:
    """Declared defaults overlaid with the node's explicit values (the
    same effective-value rule the validator's V4 check applies)."""
    values = {
        parameter.name: parameter.default for parameter in descriptor.parameters
    }
    values.update(node.parameters)
    return values


def _derived_values(node: Node, parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Placeholder values the compiler derives per node."""
    derived: Dict[str, Any] = {
        # Custom Python handler artifact path (packaging layout, task 7.1).
        "python_handler_path": "python/{0}/handler.py".format(node.id),
        # Per-node element name for simulation stub sources so the test
        # harness can feed each one from the Test_Dataset (task 4.2).
        "sim_source_name": "sim_source_{0}".format(node.id),
        # Per-node element name for the simulation model-inference stub
        # (an identity pass-through) so the test harness can identify
        # stubbed inference nodes and inject the configured simulated
        # inference outcome as their metadata (Requirement 12.6).
        "sim_inference_name": "sim_inference_{0}".format(node.id),
    }
    dio_keys = ("pin", "signal_type", "pulse_width_ms", "condition")
    dio_config = {key: parameters[key] for key in dio_keys if key in parameters}
    if dio_config:
        derived["dio_config_json"] = json.dumps(
            dio_config, sort_keys=True, separators=(",", ":")
        )
    return derived


def _resolve_chain(
    node: Node,
    mapping: GstMapping,
    context: CompileContext,
) -> List[dict]:
    """Resolve a mapping's element chain templates into concrete
    elements, each tagged with the originating nodeId (Requirement 6.6)."""
    parameters = _effective_parameters(node, get_node_type(node.type))
    resolution = _LenientDict(DEFAULT_CONTEXT_VALUES)
    resolution.update(_derived_values(node, parameters))
    resolution.update(context.values)
    resolution.update(parameters)

    elements = []
    for template in mapping.element_chain:
        args = {
            name: _resolve_value(value, resolution)
            for name, value in template["args_template"].items()
        }
        elements.append({
            "nodeId": node.id,
            "factory": template["factory"],
            "args": args,
        })
    return elements


def _resolve_value(value: Any, resolution: Dict[str, Any]) -> Any:
    """Resolve one argument template value.

    A template that is exactly one placeholder keeps the referenced
    value's native type (ints stay ints); mixed templates are formatted
    as strings. Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    match = _SINGLE_PLACEHOLDER.match(value)
    if match and match.group(1) in resolution:
        return resolution[match.group(1)]
    return value.format_map(resolution)


def _synthetic(factory: str, args: Optional[dict] = None) -> dict:
    """A linking element (tee/queue/funnel) not originating from any node."""
    return {"nodeId": None, "factory": factory, "args": dict(args or {})}


# --------------------------------------------------------------------------
# Segment linearization (Requirements 6.1, 6.3)
# --------------------------------------------------------------------------

def _build_segments(
    topo_order: List[str],
    stream_out: Dict[str, List[str]],
    stream_in: Dict[str, List[str]],
    chains: Dict[str, List[dict]],
) -> List[dict]:
    """Linearize the stream DAG into named segments.

    - A run of single-in/single-out nodes shares one segment.
    - Fan-out appends ``tee name=t<i>``; each branch is a new segment
      referencing the tee (``"from"``) and starting with ``queue``
      (Requirement 6.3).
    - Fan-in nodes start their own segment headed by a named ``funnel``;
      converging branches carry ``"linkTo"`` naming that funnel.
    """
    segments: List[dict] = []
    placed = set()
    counters = {"tee": 0, "segment": 0}

    # Fan-in nodes get a funnel name up front so any branch can link to it.
    funnel_names = {}
    for node_id in topo_order:
        if len(stream_in[node_id]) > 1:
            funnel_names[node_id] = "f{0}".format(len(funnel_names))

    def new_segment(from_ref: Optional[str] = None) -> dict:
        segment = {
            "name": "s{0}".format(counters["segment"]),
            "from": from_ref,
            "linkTo": None,
            "elements": [],
        }
        counters["segment"] += 1
        segments.append(segment)
        return segment

    def extend(node_id: str, segment: dict) -> None:
        """Append ``node_id``'s chain and continue downstream."""
        placed.add(node_id)
        segment["elements"].extend(chains[node_id])
        downstream = stream_out[node_id]
        if not downstream:
            return
        if len(downstream) == 1:
            target = downstream[0]
            if target in funnel_names:
                segment["linkTo"] = funnel_names[target]
            else:
                extend(target, segment)
            return
        # Fan-out: tee, then one queue-headed branch segment per target.
        tee_name = "t{0}".format(counters["tee"])
        counters["tee"] += 1
        segment["elements"].append(_synthetic("tee", {"name": tee_name}))
        for target in downstream:
            branch = new_segment(from_ref=tee_name)
            branch["elements"].append(_synthetic("queue"))
            if target in funnel_names:
                branch["linkTo"] = funnel_names[target]
            else:
                extend(target, branch)

    for node_id in topo_order:
        if node_id in placed:
            continue
        fan_in = len(stream_in[node_id])
        if fan_in == 0:
            extend(node_id, new_segment())
        elif fan_in > 1:
            segment = new_segment()
            segment["elements"].append(
                _synthetic("funnel", {"name": funnel_names[node_id]})
            )
            extend(node_id, segment)
        # fan_in == 1 nodes are placed while extending their upstream,
        # which precedes them in topological order.

    return segments
