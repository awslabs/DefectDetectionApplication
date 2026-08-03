"""Workflow_Validator checks V1-V5 and W1 (Requirements 4.1-4.6).

``validate(graph, catalog)`` is a pure function: it always runs every
check (no short-circuiting) and returns the complete list of
:class:`ValidationFinding` records, each carrying severity, a stable
code, a human-readable message, and the associated node or connection
identifier (Requirement 4.6).

| Check | Rule                                                        | Requirement |
|-------|-------------------------------------------------------------|-------------|
| V1    | >=1 input node and >=1 output node                          | 4.1         |
| V2    | every connection joins an output port to an input port with | 4.2         |
|       | compatible types                                            |             |
| V3    | no cycles; report the nodes in each cycle (Tarjan SCC)      | 4.3         |
| V4    | required parameters have values satisfying constraints      | 4.4         |
| V5    | every node reachable from some input node (forward BFS)     | 4.5         |
| W1    | warnings: output node with no incoming connection, unused   | 4.6         |
|       | output ports                                                |             |

Model-reference resolution (vllm-triton-inference Requirements 6.5,
6.12): when the caller supplies a ``model_registry`` snapshot (a mapping
of model name to registry record), every ``model_ref`` parameter value is
resolved against it. The record's ``model_type`` must match the node
family: ``llm_inference`` nodes require a ``vllm``-typed record, every
other node family (``model_inference``) requires a non-``vllm`` record.
An unresolvable reference (missing record or model-type mismatch)
produces an error finding identifying the node and the model reference.
Callers without a registry snapshot (the vendored device copy, existing
callers) omit the argument and the resolution check is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..catalog import NODE_CATALOG
from ..catalog.compatibility import incompatibility_reason
from ..catalog.models import (
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    NodeTypeDescriptor,
    PARAM_TYPE_MODEL_REF,
    PORT_TYPES,
)
from ..serializer.models import Node, WorkflowGraph
from .parameters import VIOLATION_REQUIRED, check_parameter_value

# --------------------------------------------------------------------------
# Severities
# --------------------------------------------------------------------------

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# --------------------------------------------------------------------------
# Finding codes (stable identifiers shared with the frontend mirror)
# --------------------------------------------------------------------------

# Structural precondition: a node whose type is not in the catalog cannot
# be checked for ports or parameters, so it gets its own error finding.
CODE_UNKNOWN_NODE_TYPE = "UNKNOWN_NODE_TYPE"

# V1 (Requirement 4.1)
CODE_V1_NO_INPUT_NODE = "V1_NO_INPUT_NODE"
CODE_V1_NO_OUTPUT_NODE = "V1_NO_OUTPUT_NODE"

# V2 (Requirement 4.2)
CODE_V2_UNKNOWN_NODE = "V2_UNKNOWN_NODE"
CODE_V2_UNKNOWN_PORT = "V2_UNKNOWN_PORT"
CODE_V2_SOURCE_NOT_OUTPUT = "V2_SOURCE_NOT_OUTPUT"
CODE_V2_TARGET_NOT_INPUT = "V2_TARGET_NOT_INPUT"
CODE_V2_INCOMPATIBLE_TYPES = "V2_INCOMPATIBLE_TYPES"

# V3 (Requirement 4.3)
CODE_V3_CYCLE = "V3_CYCLE"

# V4 (Requirement 4.4)
CODE_V4_MISSING_REQUIRED_PARAMETER = "V4_MISSING_REQUIRED_PARAMETER"
CODE_V4_INVALID_PARAMETER_VALUE = "V4_INVALID_PARAMETER_VALUE"

# V5 (Requirement 4.5)
CODE_V5_UNREACHABLE_NODE = "V5_UNREACHABLE_NODE"

# V6 (workflow-manager-integration-bugfixes Bug 2, Requirements 2.2, 3.2):
# an ``mqtt_publish`` node must declare a publish target. ``broker_host``
# is no longer statically required (so the topic-only Greengrass path is
# not force-failed by V4), so this check keeps a target-less config
# rejected — an error when the node enables neither ``greengrass`` nor
# ``aws_iot`` and supplies no non-empty ``broker_host``.
CODE_V6_MQTT_NO_TARGET = "V6_MQTT_NO_TARGET"

# V7 (portal-build-fleet-and-workflow-gates Requirement 8.2): node types
# that cannot coexist in one workflow. The rule table below is grounded
# in real runtime contracts of the workflow engine; today the single
# entry is ``aravis_camera_source``, whose single-frame appsrc Frame_Feed
# supports exactly one Aravis camera source per workflow (a document
# with more than one Aravis binding point fails feed planning on the
# device — see workflow_engine.aravis_feed.plan_aravis_feeds). V7
# surfaces that conflict at validation time, one error finding per
# offending node, each naming the full conflicting membership.
CODE_V7_COEXISTENCE_CONFLICT = "V7_COEXISTENCE_CONFLICT"

#: Node types allowed at most once per workflow: two or more instances
#: cannot coexist. Maps type id -> the plain-language reason.
COEXISTENCE_SINGLETON_TYPES: Dict[str, str] = {
    "aravis_camera_source": (
        "the single-frame appsrc feed supports exactly one Aravis "
        "camera source per workflow"
    ),
}

# W1 warnings (Requirement 4.6)
CODE_W1_OUTPUT_NODE_NO_INPUT = "W1_OUTPUT_NODE_NO_INPUT"
CODE_W1_UNUSED_OUTPUT_PORT = "W1_UNUSED_OUTPUT_PORT"

# Model-reference resolution against a registry snapshot
# (vllm-triton-inference Requirements 6.5, 6.12): the reference names no
# record in the snapshot, or the record's model type does not match the
# node family (llm_inference requires ``vllm``, model_inference and every
# other family requires non-``vllm``).
CODE_MODEL_REF_UNRESOLVED = "MODEL_REF_UNRESOLVED"

#: Model_Registry record ``model_type`` value of vLLM_Model_Records.
MODEL_TYPE_VLLM = "vllm"

#: Node type whose ``model_ref`` parameters must resolve to a
#: ``vllm``-typed record; every other node family requires non-``vllm``.
TYPE_LLM_INFERENCE = "llm_inference"

#: Output node type that publishes over MQTT; V6 requires it to declare a
#: publish target (Greengrass, AWS IoT Core, or a plain broker host).
TYPE_MQTT_PUBLISH = "mqtt_publish"


@dataclass(frozen=True)
class ValidationFinding:
    """One validation error or warning (Requirement 4.6).

    ``node_id`` / ``connection_id`` identify the associated graph element
    (at most one is set; both are None only for graph-level findings such
    as V1). ``to_dict`` emits the wire form with camelCase keys.
    """

    severity: str  # SEVERITY_ERROR | SEVERITY_WARNING
    code: str
    message: str
    node_id: Optional[str] = None
    connection_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "nodeId": self.node_id,
            "connectionId": self.connection_id,
        }


def validate(
    graph: WorkflowGraph,
    catalog: Sequence[NodeTypeDescriptor] = NODE_CATALOG,
    model_registry: Optional[Mapping[str, Any]] = None,
) -> List[ValidationFinding]:
    """Run all validator checks on ``graph`` and return every finding.

    All checks always run; nothing short-circuits. The result is the
    complete list of errors and warnings, each with the associated node
    or connection identifier (Requirement 4.6).

    ``model_registry`` is an optional Model_Registry snapshot — a mapping
    of model name to registry record (each record a mapping whose
    ``model_type`` key discriminates ``vllm`` records from vision
    records). When supplied, every ``model_ref`` parameter value is
    resolved against it with the model-type/node-family rule
    (vllm-triton-inference Requirements 6.5, 6.12); when omitted (the
    default) the resolution check is skipped and behavior is unchanged.
    """
    descriptors: Dict[str, NodeTypeDescriptor] = {d.type_id: d for d in catalog}

    findings: List[ValidationFinding] = []

    # Node id -> descriptor for nodes whose type is known; unknown types
    # get an error finding and are excluded from port/parameter checks.
    typed_nodes: Dict[str, NodeTypeDescriptor] = {}
    for node in graph.nodes:
        descriptor = descriptors.get(node.type)
        if descriptor is None:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_UNKNOWN_NODE_TYPE,
                "Node '{0}' has unknown type '{1}'".format(node.id, node.type),
                node_id=node.id,
            ))
        else:
            typed_nodes[node.id] = descriptor

    findings.extend(_check_v1(graph, typed_nodes))
    findings.extend(_check_v2(graph, typed_nodes))
    findings.extend(_check_v3(graph))
    findings.extend(_check_v4(graph, typed_nodes))
    findings.extend(_check_v5(graph, typed_nodes))
    findings.extend(_check_v6(graph, typed_nodes))
    findings.extend(_check_v7(graph))
    findings.extend(_check_w1(graph, typed_nodes))
    if model_registry is not None:
        findings.extend(_check_model_references(graph, typed_nodes, model_registry))

    return findings


# --------------------------------------------------------------------------
# Port resolution
# --------------------------------------------------------------------------

def _resolved_ports(node: Node, descriptor: NodeTypeDescriptor) -> tuple:
    """Return ``(inputs, outputs)`` as ``{port_name: port_type}`` maps.

    Custom Python nodes declare their input and output port types per
    node instance (Requirement 2.7): when the descriptor exposes
    ``input_port_type`` / ``output_port_type`` parameters and the node
    carries a known port type value, that value overrides the declared
    default type of the node's ports.
    """
    inputs = {port.name: port.port_type for port in descriptor.inputs}
    outputs = {port.name: port.port_type for port in descriptor.outputs}

    parameter_names = {parameter.name for parameter in descriptor.parameters}

    if "input_port_type" in parameter_names:
        override = node.parameters.get("input_port_type")
        if override in PORT_TYPES:
            inputs = {name: override for name in inputs}
    if "output_port_type" in parameter_names:
        override = node.parameters.get("output_port_type")
        if override in PORT_TYPES:
            outputs = {name: override for name in outputs}

    return inputs, outputs


# --------------------------------------------------------------------------
# V1: at least one input node and one output node (Requirement 4.1)
# --------------------------------------------------------------------------

def _check_v1(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    findings = []
    categories = {descriptor.category for descriptor in typed_nodes.values()}
    if CATEGORY_INPUT not in categories:
        findings.append(ValidationFinding(
            SEVERITY_ERROR,
            CODE_V1_NO_INPUT_NODE,
            "Workflow must contain at least one input node",
        ))
    if CATEGORY_OUTPUT not in categories:
        findings.append(ValidationFinding(
            SEVERITY_ERROR,
            CODE_V1_NO_OUTPUT_NODE,
            "Workflow must contain at least one output node",
        ))
    return findings


# --------------------------------------------------------------------------
# V2: connection port direction and type compatibility (Requirement 4.2)
# --------------------------------------------------------------------------

def _check_v2(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    findings = []
    for connection in graph.connections:
        source_type = _endpoint_port_type(
            graph, typed_nodes, connection, connection.source.node,
            connection.source.port, is_source=True, findings=findings,
        )
        target_type = _endpoint_port_type(
            graph, typed_nodes, connection, connection.target.node,
            connection.target.port, is_source=False, findings=findings,
        )
        if source_type is None or target_type is None:
            continue

        reason = incompatibility_reason(source_type, target_type)
        if reason is not None:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V2_INCOMPATIBLE_TYPES,
                "Connection '{0}': {1}".format(connection.id, reason),
                connection_id=connection.id,
            ))
    return findings


def _endpoint_port_type(
    graph: WorkflowGraph,
    typed_nodes: Dict[str, NodeTypeDescriptor],
    connection,
    node_id: str,
    port_name: str,
    is_source: bool,
    findings: List[ValidationFinding],
) -> Optional[str]:
    """Resolve the port type of one connection endpoint, appending V2
    findings for unknown nodes/ports and wrong port direction."""
    role = "source" if is_source else "target"

    node = graph.node_by_id(node_id)
    if node is None or node_id not in typed_nodes:
        # Missing node, or a node whose type is unknown (already reported
        # by UNKNOWN_NODE_TYPE) — the endpoint cannot be resolved.
        if node is None:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V2_UNKNOWN_NODE,
                "Connection '{0}' {1} references unknown node '{2}'".format(
                    connection.id, role, node_id
                ),
                connection_id=connection.id,
            ))
        return None

    inputs, outputs = _resolved_ports(node, typed_nodes[node_id])
    expected = outputs if is_source else inputs
    opposite = inputs if is_source else outputs

    if port_name in expected:
        return expected[port_name]

    if port_name in opposite:
        # The port exists but has the wrong direction: a connection must
        # start at an output port and end at an input port.
        code = CODE_V2_SOURCE_NOT_OUTPUT if is_source else CODE_V2_TARGET_NOT_INPUT
        direction = "an output" if is_source else "an input"
        findings.append(ValidationFinding(
            SEVERITY_ERROR,
            code,
            "Connection '{0}' {1} port '{2}' on node '{3}' is not {4} port".format(
                connection.id, role, port_name, node_id, direction
            ),
            connection_id=connection.id,
        ))
        return None

    findings.append(ValidationFinding(
        SEVERITY_ERROR,
        CODE_V2_UNKNOWN_PORT,
        "Connection '{0}' {1} references unknown port '{2}' on node '{3}'".format(
            connection.id, role, port_name, node_id
        ),
        connection_id=connection.id,
    ))
    return None


# --------------------------------------------------------------------------
# V3: cycle detection via Tarjan SCC (Requirement 4.3)
# --------------------------------------------------------------------------

def _check_v3(graph: WorkflowGraph) -> List[ValidationFinding]:
    node_ids = [node.id for node in graph.nodes]
    known = set(node_ids)

    successors: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    self_loops = set()
    for connection in graph.connections:
        source = connection.source.node
        target = connection.target.node
        if source in known and target in known:
            if target not in successors[source]:
                successors[source].append(target)
            if source == target:
                self_loops.add(source)

    findings = []
    for scc in _tarjan_sccs(node_ids, successors):
        is_cycle = len(scc) > 1 or (len(scc) == 1 and scc[0] in self_loops)
        if not is_cycle:
            continue
        # Report every node participating in the cycle, each finding
        # naming the full cycle membership (Requirement 4.3).
        members = ", ".join(sorted(scc))
        for node_id in sorted(scc):
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V3_CYCLE,
                "Node '{0}' participates in a cycle with nodes: {1}".format(
                    node_id, members
                ),
                node_id=node_id,
            ))
    return findings


def _tarjan_sccs(node_ids: List[str], successors: Dict[str, List[str]]) -> List[List[str]]:
    """Iterative Tarjan strongly-connected-components."""
    index_counter = 0
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    stack: List[str] = []
    on_stack = set()
    sccs: List[List[str]] = []

    for root in node_ids:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, next_child = work[-1]
            if node not in index:
                index[node] = lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)

            descended = False
            children = successors.get(node, [])
            for position in range(next_child, len(children)):
                child = children[position]
                if child not in index:
                    work[-1] = (node, position + 1)
                    work.append((child, 0))
                    descended = True
                    break
                if child in on_stack:
                    lowlink[node] = min(lowlink[node], index[child])
            if descended:
                continue

            if lowlink[node] == index[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                sccs.append(scc)

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    return sccs


# --------------------------------------------------------------------------
# V4: required parameters satisfy constraints (Requirement 4.4)
# --------------------------------------------------------------------------

def _check_v4(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    findings = []
    for node in graph.nodes:
        descriptor = typed_nodes.get(node.id)
        if descriptor is None:
            continue
        for parameter in descriptor.parameters:
            value = _effective_value(node, parameter)
            violation = check_parameter_value(parameter, value)
            if violation is None:
                continue
            code = (
                CODE_V4_MISSING_REQUIRED_PARAMETER
                if violation.code == VIOLATION_REQUIRED
                else CODE_V4_INVALID_PARAMETER_VALUE
            )
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                code,
                "Node '{0}': {1}".format(node.id, violation.message),
                node_id=node.id,
            ))
    return findings


def _effective_value(node: Node, parameter) -> Any:
    """The value V4 validates: the explicitly set value when the key is
    present (an explicit null counts as cleared), else the declared
    default (a default is a value, so required parameters with defaults
    are satisfied when omitted)."""
    if parameter.name in node.parameters:
        return node.parameters[parameter.name]
    return parameter.default


# --------------------------------------------------------------------------
# Model-reference resolution against a Model_Registry snapshot
# (vllm-triton-inference Requirements 6.5, 6.12)
# --------------------------------------------------------------------------

def _record_model_type(record: Any) -> Optional[str]:
    """The ``model_type`` of one registry record. Records are mappings
    (the stored Model_Registry item shape); a bare string is accepted as
    the model type itself for snapshot convenience."""
    if isinstance(record, str):
        return record
    if isinstance(record, Mapping):
        model_type = record.get("model_type")
        return model_type if isinstance(model_type, str) else None
    return None


def _check_model_references(
    graph: WorkflowGraph,
    typed_nodes: Dict[str, NodeTypeDescriptor],
    model_registry: Mapping[str, Any],
) -> List[ValidationFinding]:
    """Resolve every ``model_ref`` parameter value against the registry
    snapshot with the model-type/node-family rule: ``llm_inference``
    requires a ``vllm``-typed record (Requirement 6.12), every other node
    family (``model_inference``) requires a non-``vllm`` record. Missing
    or blank values are V4's concern and are skipped here."""
    findings = []
    for node in graph.nodes:
        descriptor = typed_nodes.get(node.id)
        if descriptor is None:
            continue
        requires_vllm = node.type == TYPE_LLM_INFERENCE
        for parameter in descriptor.parameters:
            if parameter.param_type != PARAM_TYPE_MODEL_REF:
                continue
            reference = _effective_value(node, parameter)
            if not isinstance(reference, str) or not reference:
                # Missing/blank/mistyped values are reported by V4
                # (required, min_length, type) — nothing to resolve.
                continue

            if reference not in model_registry:
                family = "vLLM model" if requires_vllm else "model"
                findings.append(ValidationFinding(
                    SEVERITY_ERROR,
                    CODE_MODEL_REF_UNRESOLVED,
                    "Node '{0}': model reference '{1}' (parameter '{2}') "
                    "does not resolve to a registered {3} in the use "
                    "case's model registry".format(
                        node.id, reference, parameter.name, family
                    ),
                    node_id=node.id,
                ))
                continue

            model_type = _record_model_type(model_registry[reference])
            is_vllm = model_type == MODEL_TYPE_VLLM
            if requires_vllm and not is_vllm:
                findings.append(ValidationFinding(
                    SEVERITY_ERROR,
                    CODE_MODEL_REF_UNRESOLVED,
                    "Node '{0}': model reference '{1}' (parameter '{2}') "
                    "resolves to a record of model type '{3}', but node "
                    "type '{4}' requires a '{5}'-typed model".format(
                        node.id, reference, parameter.name,
                        model_type, node.type, MODEL_TYPE_VLLM
                    ),
                    node_id=node.id,
                ))
            elif not requires_vllm and is_vllm:
                findings.append(ValidationFinding(
                    SEVERITY_ERROR,
                    CODE_MODEL_REF_UNRESOLVED,
                    "Node '{0}': model reference '{1}' (parameter '{2}') "
                    "resolves to a '{3}'-typed record, but node type "
                    "'{4}' requires a non-'{3}' model".format(
                        node.id, reference, parameter.name,
                        MODEL_TYPE_VLLM, node.type
                    ),
                    node_id=node.id,
                ))
    return findings


# --------------------------------------------------------------------------
# V5: reachability from input nodes via forward BFS (Requirement 4.5)
# --------------------------------------------------------------------------

def _check_v5(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    known = {node.id for node in graph.nodes}
    successors: Dict[str, List[str]] = {node_id: [] for node_id in known}
    for connection in graph.connections:
        source = connection.source.node
        target = connection.target.node
        if source in known and target in known:
            successors[source].append(target)

    roots = [
        node.id for node in graph.nodes
        if typed_nodes.get(node.id) is not None
        and typed_nodes[node.id].category == CATEGORY_INPUT
    ]

    visited = set(roots)
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for child in successors.get(current, []):
            if child not in visited:
                visited.add(child)
                frontier.append(child)

    findings = []
    for node in graph.nodes:
        if node.id not in visited:
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V5_UNREACHABLE_NODE,
                "Node '{0}' is not reachable from any input node".format(node.id),
                node_id=node.id,
            ))
    return findings


# --------------------------------------------------------------------------
# V6: mqtt_publish must declare a publish target
# (workflow-manager-integration-bugfixes Bug 2, Requirements 2.2, 3.2)
# --------------------------------------------------------------------------

def _check_v6(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    """Every ``mqtt_publish`` node must name a publish target.

    Since Bug 2 relaxed ``broker_host`` from statically-required to
    optional (so a topic-only Greengrass config is not force-failed by
    V4), this check keeps a target-less config rejected under a dedicated
    code: an error when the node enables neither the Greengrass path
    (``greengrass``) nor the AWS IoT Core path (``aws_iot``) and supplies
    no non-empty ``broker_host``. This preserves the pre-Bug-2
    accept/reject outcome for the plain-broker path while allowing the new
    topic-only Greengrass config to validate. It does not double-report
    with V4 (``broker_host`` is no longer statically required, so V4 never
    fires for it)."""
    findings = []
    for node in graph.nodes:
        if node.type != TYPE_MQTT_PUBLISH:
            continue
        descriptor = typed_nodes.get(node.id)
        if descriptor is None:
            continue
        values = {
            parameter.name: _effective_value(node, parameter)
            for parameter in descriptor.parameters
        }
        greengrass = bool(values.get("greengrass"))
        aws_iot = bool(values.get("aws_iot"))
        broker_host = values.get("broker_host")
        has_broker_host = isinstance(broker_host, str) and broker_host.strip() != ""
        if not (greengrass or aws_iot or has_broker_host):
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V6_MQTT_NO_TARGET,
                "Node '{0}': mqtt_publish requires a publish target — enable "
                "'greengrass', enable 'aws_iot', or set 'broker_host'".format(node.id),
                node_id=node.id,
            ))
    return findings


# --------------------------------------------------------------------------
# V7: node-type coexistence conflicts
# (portal-build-fleet-and-workflow-gates Requirement 8.2)
# --------------------------------------------------------------------------

def _check_v7(graph: WorkflowGraph) -> List[ValidationFinding]:
    """Report node types that cannot coexist in one workflow.

    Driven by :data:`COEXISTENCE_SINGLETON_TYPES` — node types whose
    runtime contract allows at most one instance per workflow. When two
    or more nodes of such a type are present, every one of them gets an
    error finding naming the full conflicting membership (mirroring the
    V3 cycle-reporting shape), so each offending node is individually
    addressable by the caller.

    The check keys on ``node.type`` directly (not the catalog) so a
    conflict is reported even when the type is also unknown to the
    catalog in use.
    """
    findings = []
    by_type: Dict[str, List[str]] = {}
    for node in graph.nodes:
        if node.type in COEXISTENCE_SINGLETON_TYPES:
            by_type.setdefault(node.type, []).append(node.id)

    for node_type, node_ids in sorted(by_type.items()):
        if len(node_ids) < 2:
            continue
        reason = COEXISTENCE_SINGLETON_TYPES[node_type]
        members = ", ".join("'{0}'".format(i) for i in sorted(node_ids))
        for node_id in sorted(node_ids):
            findings.append(ValidationFinding(
                SEVERITY_ERROR,
                CODE_V7_COEXISTENCE_CONFLICT,
                "Node '{0}': {1} nodes of type '{2}' cannot coexist in "
                "one workflow ({3}): {4}".format(
                    node_id, len(node_ids), node_type, members, reason
                ),
                node_id=node_id,
            ))
    return findings


# --------------------------------------------------------------------------
# W1: warnings (Requirement 4.6)
# --------------------------------------------------------------------------

def _check_w1(graph: WorkflowGraph, typed_nodes: Dict[str, NodeTypeDescriptor]) -> List[ValidationFinding]:
    incoming = set()  # node ids with at least one incoming connection
    used_output_ports = set()  # (node id, port name) pairs feeding a connection
    for connection in graph.connections:
        incoming.add(connection.target.node)
        used_output_ports.add((connection.source.node, connection.source.port))

    findings = []
    for node in graph.nodes:
        descriptor = typed_nodes.get(node.id)
        if descriptor is None:
            continue

        if descriptor.category == CATEGORY_OUTPUT and node.id not in incoming:
            findings.append(ValidationFinding(
                SEVERITY_WARNING,
                CODE_W1_OUTPUT_NODE_NO_INPUT,
                "Output node '{0}' has no incoming connection".format(node.id),
                node_id=node.id,
            ))

        _, outputs = _resolved_ports(node, descriptor)
        for port_name in outputs:
            if (node.id, port_name) not in used_output_ports:
                findings.append(ValidationFinding(
                    SEVERITY_WARNING,
                    CODE_W1_UNUSED_OUTPUT_PORT,
                    "Output port '{0}' of node '{1}' is not connected".format(
                        port_name, node.id
                    ),
                    node_id=node.id,
                ))
    return findings
