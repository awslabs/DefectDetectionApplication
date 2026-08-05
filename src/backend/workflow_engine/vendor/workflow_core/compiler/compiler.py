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

import copy
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from ..catalog import ARCH_SIM, ARCHITECTURES, NODE_CATALOG, bundled_plugins_for
from ..catalog.models import GstMapping, NodeTypeDescriptor
from ..catalog.nodes import SOURCE_KIND_TO_SOURCE_TYPE
from ..serializer.models import Connection, Node, WorkflowGraph
from ..validator import SEVERITY_ERROR, validate
from .models import (
    CODE_UNMAPPED_ARCHITECTURE,
    CODE_VALIDATION_ERROR,
    CompileContext,
    CompiledPipelineDocument,
    CompileError,
    DEFAULT_CONTEXT_VALUES,
)

__all__ = ["compile", "expand_unified_inputs"]

#: The unified input node type id and its optional activation input port
#: (design C3/C5). ``expand_unified_inputs`` rewrites unified nodes into
#: their underlying source and drops edges targeting this port.
UNIFIED_INPUT_TYPE = "unified_input"
UNIFIED_ACTIVATION_PORT = "activation"

#: Node type ids whose ``activation`` input port is inert scaffolding:
#: the unified input node plus the four legacy source descriptors
#: (Requirement 7.2). ``expand_unified_inputs`` drops every connection
#: targeting an ``activation`` port on any of these types before the
#: validation re-run and mapping resolution; no trigger-driven
#: activation binding is ever emitted for these ports.
INERT_ACTIVATION_TYPE_IDS = frozenset(
    {UNIFIED_INPUT_TYPE} | set(SOURCE_KIND_TO_SOURCE_TYPE.values())
)

#: The subscribe-side trigger node types (trigger-activation-runtime,
#: design C3/D3). Activation edges whose SOURCE node is one of these
#: types are real activation-target declarations: ``expand_unified_inputs``
#: keeps them (all other activation edges — notably ``digital_input`` —
#: keep today's inert drop, Requirement 5.3), and ``compile()`` extracts
#: them into the activation plan attached to the trigger's executor
#: binding as ``"activates"`` (Requirements 5.1, 5.2).
TRIGGER_EXECUTOR_TYPES = frozenset({"mqtt_subscribe", "opcua_subscribe"})

#: The two-path routing executor binding (the conditional node): the
#: compiler emits per-port gate conditions for it ("true" = the
#: configured condition, "false" = its negation), so downstream executor
#: bindings of each output port are gated exactly like downstream of an
#: inference_filter.
BINDING_CONDITIONAL = "conditional"

#: The Bedrock comparison-inference executor binding (the
#: bedrock_inference node on device architectures). Unlike other
#: executor-level nodes, its two VideoFrames input branches TERMINATE in
#: the pipeline: the compiler appends one synthetic frame-capture sink
#: chain (``videoconvert ! jpegenc ! multifilesink``) per feeding
#: GStreamer branch so each branch ends in a real sink that persists the
#: latest frame, and the emitted binding carries the per-input-port
#: capture file paths (``capturePaths``). The paths are rooted at the
#: ``{work_dir}`` placeholder, resolved by the LocalServer executor per
#: run (the same lenient-placeholder mechanism as ``{dataset_location}``
#: for the test harness). Frames do NOT flow through the node, so
#: pipeline-element consumers downstream of its InferenceMeta output are
#: never fed on device architectures (metadata-level executor consumers
#: — filters, conditionals, hardware outputs — are the supported
#: downstream). In simulation the node resolves to its sim stub chain
#: (identity pass-through) and none of this applies.
BINDING_BEDROCK_INFERENCE = "bedrock_inference"


def expand_unified_inputs(
    graph: WorkflowGraph,
    catalog: Sequence[NodeTypeDescriptor] = NODE_CATALOG,
) -> WorkflowGraph:
    """Rewrite ``unified_input`` nodes into their underlying source nodes.

    Returns a **new** :class:`WorkflowGraph` (the input graph is never
    mutated — nodes and connections are deep-copied). Each
    ``unified_input`` node is replaced by a synthetic node carrying the
    **same** ``id`` and ``position``, ``type`` set to
    ``SOURCE_KIND_TO_SOURCE_TYPE[source_kind]``, and only the parameters
    whose names appear on the underlying source descriptor (``source_kind``
    and any non-applicable union parameters are dropped). Every connection
    targeting an ``activation`` port on a unified node or on any of the
    four legacy source nodes (``INERT_ACTIVATION_TYPE_IDS``) is dropped;
    all other nodes and connections pass through unchanged (design C5,
    Requirements 3.6, 3.8, 3.9, 2.6, 7.2).

    Defensive on an unknown/missing ``source_kind``: the node is left as-is
    so that ``compile()``'s validation re-run reports the invalid enum
    (``V4``) and refuses to compile, rather than dereferencing the map with
    an invalid key.
    """
    descriptors_by_id: Dict[str, NodeTypeDescriptor] = {
        descriptor.type_id: descriptor for descriptor in catalog
    }

    #: Original node id -> type, for the inert-activation edge drop below
    #: (the pre-expansion type is what identifies unified/legacy sources).
    node_types: Dict[str, str] = {node.id: node.type for node in graph.nodes}

    new_nodes: List[Node] = []
    for node in graph.nodes:
        if node.type != UNIFIED_INPUT_TYPE:
            new_nodes.append(copy.deepcopy(node))
            continue

        source_kind = node.parameters.get("source_kind")
        source_type = SOURCE_KIND_TO_SOURCE_TYPE.get(source_kind)
        source_descriptor = (
            descriptors_by_id.get(source_type) if source_type is not None else None
        )
        if source_descriptor is None:
            # Unknown/missing source_kind: leave the node untouched so the
            # compile-time validation re-run reports it (V4) and refuses.
            new_nodes.append(copy.deepcopy(node))
            continue

        applicable = {parameter.name for parameter in source_descriptor.parameters}
        expanded_parameters = {
            name: copy.deepcopy(value)
            for name, value in node.parameters.items()
            if name in applicable
        }
        new_nodes.append(Node(
            id=node.id,
            type=source_type,
            position=node.position,
            parameters=expanded_parameters,
            data=copy.deepcopy(node.data),
        ))

    new_connections: List[Connection] = []
    for connection in graph.connections:
        if (
            connection.target.port == UNIFIED_ACTIVATION_PORT
            and node_types.get(connection.target.node) in INERT_ACTIVATION_TYPE_IDS
            and node_types.get(connection.source.node) not in TRIGGER_EXECUTOR_TYPES
        ):
            # Drop edges into an inert activation port — on a unified node
            # (the expanded source has no activation realization,
            # Requirement 3.9, P4) or on a legacy source node
            # (Requirement 7.2); no activation binding is ever emitted.
            # Edges sourced from a subscribe-side trigger node
            # (``TRIGGER_EXECUTOR_TYPES``) are real Activation_Edges and
            # survive expansion for ``compile()``'s activation-plan
            # extraction (trigger-activation-runtime Requirements 5.2,
            # 5.3 — ``digital_input`` edges keep today's drop).
            continue
        new_connections.append(copy.deepcopy(connection))

    return WorkflowGraph(nodes=new_nodes, connections=new_connections)


def _extract_activation_plan(graph: WorkflowGraph) -> Dict[str, List[str]]:
    """Pull the subscribe-side triggers' Activation_Edges out of the graph.

    Removes (in place, on the compile-local graph copy) every connection
    targeting an ``activation`` port whose SOURCE node type is in
    :data:`TRIGGER_EXECUTOR_TYPES`, and returns the activation plan
    ``{trigger_node_id: [target_node_id, ...]}`` with targets in
    connection order (deduplicated). Stream topology, segments, and
    predecessors/successors are computed on the reduced connection set,
    so Activation_Edges never reach GStreamer stream-topology resolution
    (trigger-activation-runtime Requirement 5.2, design D3). For graphs
    without the new trigger types nothing is removed and the plan is
    empty, keeping compiled output identical (Requirement 5.4).
    """
    node_types: Dict[str, str] = {node.id: node.type for node in graph.nodes}
    plan: Dict[str, List[str]] = {}
    kept: List[Connection] = []
    for connection in graph.connections:
        if (
            connection.target.port == UNIFIED_ACTIVATION_PORT
            and node_types.get(connection.source.node) in TRIGGER_EXECUTOR_TYPES
        ):
            targets = plan.setdefault(connection.source.node, [])
            if connection.target.node not in targets:
                targets.append(connection.target.node)
            continue
        kept.append(connection)
    graph.connections = kept
    return plan


def compile(
    graph: WorkflowGraph,
    target_arch: str,
    context: Optional[CompileContext] = None,
    simulation: bool = False,
    catalog: Sequence[NodeTypeDescriptor] = NODE_CATALOG,
) -> Union[CompiledPipelineDocument, List[CompileError]]:
    """Compile ``graph`` for ``target_arch``.

    With ``simulation=True``, hardware-dependent node types resolve their
    ``sim``-architecture recording stubs instead of the ``target_arch``
    mapping; all other nodes compile identically to non-simulation output
    (Requirement 12.6).

    ``catalog`` is the effective Node_Type_Catalog to compile against —
    by default the built-in ``NODE_CATALOG``; portal callers pass the
    merged tuple from ``resolve_catalog`` so workflows may use registered
    Custom_Node_Types (custom-node-designer Requirements 5.4, 8.6). The
    compiler treats built-in and custom descriptors identically: custom
    plugin dependencies flow into ``pluginDependencies``, and a node type
    without a mapping for ``target_arch`` yields the standard
    ``CompileError{nodeId, arch}``.

    Returns a :class:`CompiledPipelineDocument` on success, or the
    complete list of :class:`CompileError` records on failure (validation
    errors, or nodes without a mapping for the architecture).
    """
    context = context or CompileContext()

    # 0. Expand unified_input nodes into their underlying source nodes on a
    #    copy of the graph (never mutating the input), before validation and
    #    mapping resolution — so the unified type_id never reaches
    #    mapping_for and the expanded source flows through the existing path
    #    (design C5, Requirements 3.6, 3.8, 3.9, 2.6).
    graph = expand_unified_inputs(graph, catalog)

    descriptors_by_id: Dict[str, NodeTypeDescriptor] = {
        descriptor.type_id: descriptor for descriptor in catalog
    }

    # 1. Re-run validation; refuse to compile on errors (Requirement 6.1).
    validation_errors = [
        finding for finding in validate(graph, catalog)
        if finding.severity == SEVERITY_ERROR
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

    # 1b. Extract the subscribe-side triggers' Activation_Edges into the
    #     activation plan (post-expansion, post-validation): the working
    #     connection set is reduced so stream topology, segments, and
    #     predecessors/successors are computed as if the Activation_Edges
    #     were absent; the plan is attached to the trigger executor
    #     bindings below (trigger-activation-runtime Requirements 5.1,
    #     5.2, 5.4 — design C3/D3). Graphs without the new trigger types
    #     are untouched (empty plan, identical connection set).
    activation_plan = _extract_activation_plan(graph)

    # 2. Resolve mappings; collect every unmapped node (Requirement 6.5).
    #    In simulation mode, hardware-dependent node types resolve their
    #    sim-architecture recording stubs instead (Requirement 12.6); an
    #    unknown target architecture stays an error for every node.
    stub_hardware = simulation and target_arch in ARCHITECTURES
    mappings: Dict[str, GstMapping] = {}
    unmapped: List[CompileError] = []
    for node in graph.nodes:
        descriptor = descriptors_by_id[node.type]  # known: validation passed
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
    #    Bedrock inference nodes (executor-level realization) are opaque:
    #    frames terminate at their synthetic capture sinks instead of
    #    flowing through to downstream pipeline elements.
    gst_set = set(gst_node_ids)
    bedrock_node_ids = [
        n.id for n in graph.nodes
        if mappings[n.id].executor_binding == BINDING_BEDROCK_INFERENCE
    ]
    opaque = set(bedrock_node_ids)
    stream_out = {
        node_id: _stream_successors(node_id, successors, gst_set, opaque)
        for node_id in gst_node_ids
    }
    stream_in: Dict[str, List[str]] = {node_id: [] for node_id in gst_node_ids}
    for source, targets in stream_out.items():
        for target in targets:
            stream_in[target].append(source)
    topo_order = _topological_sort(gst_node_ids, stream_out, stream_in)

    # Bedrock frame captures: one synthetic sink chain per GStreamer
    # branch feeding a bedrock_inference input port, plus the per-port
    # capture file paths for each node's executor binding.
    feeder_captures, bedrock_capture_paths = _bedrock_capture_plan(
        graph, bedrock_node_ids, gst_set, opaque, descriptors_by_id
    )

    # 4./5. Emit tagged element chains linearized into segments.
    chains = {
        node_id: _resolve_chain(
            nodes_by_id[node_id],
            descriptors_by_id[nodes_by_id[node_id].type],
            mappings[node_id],
            context,
        )
        for node_id in gst_node_ids
    }
    segments = _build_segments(
        topo_order, stream_out, stream_in, chains, feeder_captures
    )

    # Executor bindings, one entry per executor-level node (Requirement 6.6).
    predecessors = _node_predecessors(graph)
    port_successors = _node_port_successors(graph)
    executor_bindings = []
    for node_id in executor_node_ids:
        node = nodes_by_id[node_id]
        descriptor = descriptors_by_id[node.type]
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
        # Bedrock inference bindings carry the per-input-port capture
        # file paths their frame branches sink to ({work_dir}-rooted,
        # resolved by the LocalServer executor per run).
        if mappings[node_id].executor_binding == BINDING_BEDROCK_INFERENCE:
            entry["capturePaths"] = bedrock_capture_paths.get(node_id, {})
        # Subscribe-side trigger bindings carry the ordered activation
        # targets extracted into the activation plan; no other binding
        # gains any key (trigger-activation-runtime Requirement 5.1,
        # design D2).
        if mappings[node_id].executor_binding in TRIGGER_EXECUTOR_TYPES:
            entry["activates"] = activation_plan.get(node_id, [])
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
    opaque: Optional[set] = None,
) -> List[str]:
    """The GStreamer nodes ``node_id`` streams into, looking through
    executor-level nodes (which have no pipeline elements) — except
    ``opaque`` nodes (bedrock_inference), where the frame stream
    terminates in the node's synthetic capture sink."""
    opaque = opaque or set()
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
        elif current not in opaque:
            frontier.extend(successors.get(current, []))
    return result


# --------------------------------------------------------------------------
# Bedrock inference frame captures
# --------------------------------------------------------------------------

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _frame_feeders(
    graph: WorkflowGraph,
    node_id: str,
    port_name: str,
    gst_set: set,
    opaque: set,
) -> List[str]:
    """The GStreamer nodes whose frames reach input port ``port_name``
    of ``node_id``, looking upstream through executor-level nodes (the
    buffer stream passes through them) but never through other opaque
    frame-terminating nodes."""
    frontier = [
        connection.source.node for connection in graph.connections
        if connection.target.node == node_id
        and connection.target.port == port_name
    ]
    feeders: List[str] = []
    seen = set()
    while frontier:
        current = frontier.pop(0)
        if current in seen or current == node_id:
            continue
        seen.add(current)
        if current in gst_set:
            if current not in feeders:
                feeders.append(current)
        elif current not in opaque:
            frontier.extend(
                connection.source.node for connection in graph.connections
                if connection.target.node == current
            )
    return feeders


def _bedrock_capture_plan(
    graph: WorkflowGraph,
    bedrock_node_ids: List[str],
    gst_set: set,
    opaque: set,
    descriptors_by_id: Dict[str, NodeTypeDescriptor],
):
    """Plan the synthetic frame-capture sinks for bedrock_inference nodes.

    Returns ``(feeder_captures, capture_paths_by_node)``:

    - ``feeder_captures``: GStreamer feeder node id -> capture file path.
      Every branch feeding any bedrock input port ends in exactly one
      capture sink chain persisting its latest frame; a feeder serving
      several ports (or several bedrock nodes) shares its single file —
      the frames are the same stream.
    - ``capture_paths_by_node``: bedrock node id -> {input port name:
      capture path, or None when nothing feeds the port}. When several
      branches feed one port, the first (connection-ordered) feeder's
      file is used.

    Paths are rooted at the ``{work_dir}`` placeholder the LocalServer
    executor resolves per run; feeder node ids are sanitized to a safe
    file-name form (collisions disambiguated with a numeric suffix).
    """
    feeder_captures: Dict[str, str] = {}
    used_names: set = set()
    capture_paths_by_node: Dict[str, Dict[str, Optional[str]]] = {}

    def path_for(feeder_id: str) -> str:
        if feeder_id not in feeder_captures:
            base = _UNSAFE_PATH_CHARS.sub("_", feeder_id) or "node"
            name = base
            suffix = 0
            while name in used_names:
                suffix += 1
                name = "{0}_{1}".format(base, suffix)
            used_names.add(name)
            feeder_captures[feeder_id] = (
                "{work_dir}/bedrock_frame_" + name + ".jpg"
            )
        return feeder_captures[feeder_id]

    for node_id in bedrock_node_ids:
        descriptor = descriptors_by_id[graph.node_by_id(node_id).type]
        ports: Dict[str, Optional[str]] = {}
        for port in descriptor.inputs:
            feeders = _frame_feeders(graph, node_id, port.name, gst_set, opaque)
            ports[port.name] = path_for(feeders[0]) if feeders else None
        capture_paths_by_node[node_id] = ports

    return feeder_captures, capture_paths_by_node


def _capture_chain(path: str) -> List[dict]:
    """The synthetic frame-capture sink chain terminating a bedrock
    feeder branch: raw frames converted, JPEG-encoded, and written to
    ``path`` (multifilesink without a printf index rewrites the same
    file per buffer, so the latest frame persists for the executor)."""
    return [
        _synthetic("videoconvert"),
        _synthetic("jpegenc"),
        _synthetic("multifilesink", {"location": path}),
    ]


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
        # The node's own id, for per-node element naming in mapping
        # templates (e.g. aravis_camera_source's appsrc name
        # ``appsrc_{nodeId}``) so multi-node documents render unique
        # element names.
        "nodeId": node.id,
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
        # Per-node element name for the custom-node pass-through recording
        # stub substituted when a Custom_Node_Type has no x86_64
        # Plugin_Artifact, so the test harness can identify the stubbed
        # nodes in the test run report (custom-node-designer
        # Requirement 12.2; the stub mapping is built by the test-runner
        # compile step in workflow_test_steps.py).
        "custom_stub_name": "custom_stub_{0}".format(node.id),
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
    descriptor: NodeTypeDescriptor,
    mapping: GstMapping,
    context: CompileContext,
) -> List[dict]:
    """Resolve a mapping's element chain templates into concrete
    elements, each tagged with the originating nodeId (Requirement 6.6)."""
    parameters = _effective_parameters(node, descriptor)
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
    feeder_captures: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Linearize the stream DAG into named segments.

    - A run of single-in/single-out nodes shares one segment.
    - Fan-out appends ``tee name=t<i>``; each branch is a new segment
      referencing the tee (``"from"``) and starting with ``queue``
      (Requirement 6.3).
    - Fan-in nodes start their own segment headed by a named ``funnel``;
      converging branches carry ``"linkTo"`` naming that funnel.
    - A node in ``feeder_captures`` (it feeds a bedrock_inference input
      port) additionally sinks its frames to the planned capture file:
      inline at the end of its branch when the branch otherwise
      terminates there, or as an extra queue-headed tee branch when the
      stream also continues downstream.
    """
    feeder_captures = feeder_captures or {}
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
        capture_path = feeder_captures.get(node_id)
        if not downstream:
            # A bedrock feeder whose branch ends here: the capture sink
            # chain terminates the branch inline (no tee).
            if capture_path:
                segment["elements"].extend(_capture_chain(capture_path))
            return
        if len(downstream) == 1 and not capture_path:
            target = downstream[0]
            if target in funnel_names:
                segment["linkTo"] = funnel_names[target]
            else:
                extend(target, segment)
            return
        # Fan-out (or downstream continuation plus a bedrock capture):
        # tee, then one queue-headed branch segment per target.
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
        if capture_path:
            branch = new_segment(from_ref=tee_name)
            branch["elements"].append(_synthetic("queue"))
            branch["elements"].extend(_capture_chain(capture_path))

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
