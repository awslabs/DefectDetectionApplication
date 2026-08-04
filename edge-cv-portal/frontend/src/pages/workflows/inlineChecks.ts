/**
 * Lightweight TypeScript mirror of Workflow_Validator checks V4 and V5
 * (Requirements 1.9, 4.4, 4.5), run on every canvas graph mutation for
 * inline validation markers. Full validation (all checks) is performed
 * by the backend via `workflow_core.validator.checks` — the source of
 * truth these mirrors must stay in sync with.
 *
 * | Check | Rule                                                   |
 * |-------|--------------------------------------------------------|
 * | V4    | required parameters have values satisfying constraints |
 * | V5    | every node reachable from some input node (forward BFS)|
 * | V7    | no connection targets a trigger node (stage ordering)  |
 */

import { checkParameterValue, VIOLATION_REQUIRED } from './parameters';
import {
  CATEGORY_INPUT,
  CATEGORY_TRIGGER,
  PORT_TYPES,
  SEVERITY_ERROR,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type ValidationFinding,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Finding codes (stable identifiers shared with the Python validator)
// --------------------------------------------------------------------------

export const CODE_V4_MISSING_REQUIRED_PARAMETER = 'V4_MISSING_REQUIRED_PARAMETER';
export const CODE_V4_INVALID_PARAMETER_VALUE = 'V4_INVALID_PARAMETER_VALUE';
export const CODE_V5_UNREACHABLE_NODE = 'V5_UNREACHABLE_NODE';
export const CODE_V7_STAGE_ORDER = 'V7_STAGE_ORDER';

/** The graph slice the inline checks operate on. */
export interface GraphLike {
  nodes: WorkflowNode[];
  connections: WorkflowConnection[];
}

/**
 * Map node id -> catalog descriptor for nodes whose type is known;
 * nodes with unknown types are excluded (they cannot be checked for
 * parameters and never count as input roots).
 */
function typedNodes(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): Map<string, NodeTypeDescriptor> {
  const descriptors = new Map(catalog.map((d) => [d.typeId, d]));
  const result = new Map<string, NodeTypeDescriptor>();
  for (const node of graph.nodes) {
    const descriptor = descriptors.get(node.type);
    if (descriptor !== undefined) {
      result.set(node.id, descriptor);
    }
  }
  return result;
}

/**
 * Resolve a node's input and output port types as `{portName: portType}`
 * maps, mirroring `_resolved_ports` in the Python validator.
 *
 * Custom Python nodes declare their input and output port types per node
 * instance (Requirement 2.7): when the descriptor exposes
 * `input_port_type` / `output_port_type` parameters and the node carries
 * a known port type value, that value overrides the declared default
 * type of the node's ports.
 */
export function resolvedPorts(
  node: WorkflowNode,
  descriptor: NodeTypeDescriptor
): { inputs: Record<string, string>; outputs: Record<string, string> } {
  const inputs: Record<string, string> = {};
  for (const port of descriptor.inputs) {
    inputs[port.name] = port.portType;
  }
  const outputs: Record<string, string> = {};
  for (const port of descriptor.outputs) {
    outputs[port.name] = port.portType;
  }

  const parameterNames = new Set(descriptor.parameters.map((p) => p.name));

  if (parameterNames.has('input_port_type')) {
    const override = node.parameters['input_port_type'];
    if (typeof override === 'string' && (PORT_TYPES as readonly string[]).includes(override)) {
      for (const name of Object.keys(inputs)) {
        inputs[name] = override;
      }
    }
  }
  if (parameterNames.has('output_port_type')) {
    const override = node.parameters['output_port_type'];
    if (typeof override === 'string' && (PORT_TYPES as readonly string[]).includes(override)) {
      for (const name of Object.keys(outputs)) {
        outputs[name] = override;
      }
    }
  }

  return { inputs, outputs };
}

/**
 * The value V4 validates: the explicitly set value when the key is
 * present (an explicit null counts as cleared), else the declared
 * default (a default is a value, so required parameters with defaults
 * are satisfied when omitted).
 */
function effectiveValue(
  node: WorkflowNode,
  parameter: ParameterDescriptor
): JsonValue | null | undefined {
  if (Object.prototype.hasOwnProperty.call(node.parameters, parameter.name)) {
    return node.parameters[parameter.name];
  }
  return parameter.default;
}

// --------------------------------------------------------------------------
// V4: required parameters satisfy constraints (Requirement 4.4)
// --------------------------------------------------------------------------

/** Mirror of validator check V4 (missing/invalid required parameters). */
export function checkV4(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const node of graph.nodes) {
    const descriptor = typed.get(node.id);
    if (descriptor === undefined) {
      continue;
    }
    for (const parameter of descriptor.parameters) {
      const violation = checkParameterValue(parameter, effectiveValue(node, parameter));
      if (violation === null) {
        continue;
      }
      const code =
        violation.code === VIOLATION_REQUIRED
          ? CODE_V4_MISSING_REQUIRED_PARAMETER
          : CODE_V4_INVALID_PARAMETER_VALUE;
      findings.push({
        severity: SEVERITY_ERROR,
        code,
        message: `Node '${node.id}': ${violation.message}`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V5: reachability from input nodes via forward BFS (Requirement 4.5)
// --------------------------------------------------------------------------

/** Mirror of validator check V5 (nodes unreachable from any input node). */
export function checkV5(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const known = new Set(graph.nodes.map((node) => node.id));

  const successors = new Map<string, string[]>();
  for (const id of known) {
    successors.set(id, []);
  }
  for (const connection of graph.connections) {
    const source = connection.from.node;
    const target = connection.to.node;
    if (known.has(source) && known.has(target)) {
      successors.get(source)!.push(target);
    }
  }

  const roots = graph.nodes
    .filter((node) => {
      const category = typed.get(node.id)?.category;
      return category === CATEGORY_INPUT || category === CATEGORY_TRIGGER;
    })
    .map((node) => node.id);

  const visited = new Set(roots);
  const frontier = [...roots];
  while (frontier.length > 0) {
    const current = frontier.pop()!;
    for (const child of successors.get(current) ?? []) {
      if (!visited.has(child)) {
        visited.add(child);
        frontier.push(child);
      }
    }
  }

  const findings: ValidationFinding[] = [];
  for (const node of graph.nodes) {
    if (!visited.has(node.id)) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V5_UNREACHABLE_NODE,
        message: `Node '${node.id}' is not reachable from any input node`,
        nodeId: node.id,
        connectionId: null,
      });
    }
  }
  return findings;
}

// --------------------------------------------------------------------------
// V7: trigger stage ordering (Requirement 5.5, mirrors backend V7)
// --------------------------------------------------------------------------

/**
 * Mirror of validator check V7 (illegal stage ordering). A
 * `CATEGORY_TRIGGER` node has no input ports and may only feed an Input's
 * activation port, so any connection whose target resolves to a trigger
 * node is an illegal ordering — the only way to place a trigger
 * downstream of another node. The check is target-category based, which
 * also makes the legal `Trigger → Unified activation-port` case pass
 * automatically (its target is the `CATEGORY_INPUT` unified node).
 */
export function checkV7(graph: GraphLike, catalog: NodeTypeDescriptor[]): ValidationFinding[] {
  const typed = typedNodes(graph, catalog);
  const findings: ValidationFinding[] = [];

  for (const connection of graph.connections) {
    const target = typed.get(connection.to.node);
    if (target !== undefined && target.category === CATEGORY_TRIGGER) {
      findings.push({
        severity: SEVERITY_ERROR,
        code: CODE_V7_STAGE_ORDER,
        message:
          `Connection '${connection.id}' targets trigger node ` +
          `'${connection.to.node}': a trigger may not be downstream of any ` +
          `node (Trigger -> Input ordering)`,
        nodeId: null,
        connectionId: connection.id,
      });
    }
  }
  return findings;
}

/**
 * Run the inline mirror checks (V4 + V5 + V7) and return every finding,
 * each with the associated node or connection identifier. The canvas
 * turns these into inline validation markers (Requirements 1.9, 1.10,
 * 5.5).
 */
export function runInlineChecks(
  graph: GraphLike,
  catalog: NodeTypeDescriptor[]
): ValidationFinding[] {
  return [...checkV4(graph, catalog), ...checkV5(graph, catalog), ...checkV7(graph, catalog)];
}
