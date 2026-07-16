/**
 * Pure helpers for the Workflow_Builder canvas (Requirements 1.2-1.5).
 *
 * The canvas keeps its state as React Flow nodes/edges; these helpers
 * translate between that state and the Workflow_Definition shapes from
 * `types.ts`, create new node instances with default configuration, and
 * decide whether a dragged connection is acceptable (port compatibility
 * via `arePortsCompatible` / `incompatibilityReason`).
 */

import type { Edge, Node } from '@xyflow/react';
import { incompatibilityReason } from './compatibility';
import { resolvedPorts } from './inlineChecks';
import {
  SCHEMA_VERSION,
  type JsonValue,
  type NodeCategory,
  type NodeTypeDescriptor,
  type WorkflowConnection,
  type WorkflowDefinition,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Canvas node shape
// --------------------------------------------------------------------------

/**
 * Data carried by every canvas node. `validationMessages` is the
 * extension point for the inline validation markers (task 8.4): the
 * node component renders a warning badge whenever messages are present.
 */
export type BuilderNodeData = {
  descriptor: NodeTypeDescriptor;
  parameters: Record<string, JsonValue>;
  validationMessages: string[];
  /**
   * Advisory node data carried in the definition's `nodes[].data` (e.g.
   * the camera picker's `cameraBindingHint`, camera-registry-sync
   * Requirements 7.2, 7.5). Preserved verbatim through save/load round
   * trips; absent on nodes without advisory data.
   */
  advisoryData?: Record<string, JsonValue>;
};

/** A React Flow node on the Workflow_Builder canvas. */
export type BuilderNode = Node<BuilderNodeData, 'workflowNode'>;

/** The React Flow node type id used for all workflow nodes. */
export const WORKFLOW_NODE_TYPE = 'workflowNode' as const;

// --------------------------------------------------------------------------
// Category presentation (palette groups and node header colors)
// --------------------------------------------------------------------------

/** Display label and color for each of the five node categories. */
export const CATEGORY_META: Record<NodeCategory, { label: string; color: string }> = {
  input: { label: 'Input', color: '#037f0c' },
  preprocessing: { label: 'Preprocessing', color: '#0972d3' },
  inference: { label: 'Model inference', color: '#7d3ac1' },
  post_processing: { label: 'Post-processing', color: '#8a6116' },
  output: { label: 'Output', color: '#d13212' },
};

/** Fallback for descriptors with an unknown category value. */
export const UNKNOWN_CATEGORY_META = { label: 'Other', color: '#5f6b7a' };

export function categoryMeta(category: string): { label: string; color: string } {
  return CATEGORY_META[category as NodeCategory] ?? UNKNOWN_CATEGORY_META;
}

// --------------------------------------------------------------------------
// Node creation with default configuration (Requirement 1.2)
// --------------------------------------------------------------------------

/**
 * A node type's default configuration: every parameter that declares a
 * non-null default, mapped to that default.
 */
export function defaultParameters(descriptor: NodeTypeDescriptor): Record<string, JsonValue> {
  const parameters: Record<string, JsonValue> = {};
  for (const parameter of descriptor.parameters) {
    if (parameter.default !== undefined && parameter.default !== null) {
      parameters[parameter.name] = parameter.default;
    }
  }
  return parameters;
}

/**
 * The first free id of the form `{typeId}_{n}` (n starting at 1) not
 * present in `existingIds`.
 */
export function nextNodeId(typeId: string, existingIds: Iterable<string>): string {
  const taken = new Set(existingIds);
  let n = 1;
  while (taken.has(`${typeId}_${n}`)) {
    n += 1;
  }
  return `${typeId}_${n}`;
}

/**
 * Create a canvas node instance of the given type at the given canvas
 * position with the type's default configuration (Requirement 1.2).
 */
export function createBuilderNode(
  descriptor: NodeTypeDescriptor,
  position: { x: number; y: number },
  existingIds: Iterable<string>
): BuilderNode {
  return {
    id: nextNodeId(descriptor.typeId, existingIds),
    type: WORKFLOW_NODE_TYPE,
    position,
    data: {
      descriptor,
      parameters: defaultParameters(descriptor),
      validationMessages: [],
    },
  };
}

// --------------------------------------------------------------------------
// Translation to Workflow_Definition shapes
// --------------------------------------------------------------------------

/** The Workflow_Definition node for a canvas node. */
export function toWorkflowNode(node: BuilderNode): WorkflowNode {
  const advisoryData = node.data.advisoryData;
  return {
    id: node.id,
    type: node.data.descriptor.typeId,
    position: { x: node.position.x, y: node.position.y },
    parameters: node.data.parameters,
    // Advisory node data (e.g. cameraBindingHint) rides along in the
    // definition's `data` field; omitted entirely when empty so
    // pre-feature definitions serialize byte-identically.
    ...(advisoryData !== undefined && Object.keys(advisoryData).length > 0
      ? { data: advisoryData }
      : {}),
  };
}

/** The Workflow_Definition connection for a canvas edge. */
export function toWorkflowConnection(edge: Edge): WorkflowConnection {
  return {
    id: edge.id,
    from: { node: edge.source, port: edge.sourceHandle ?? '' },
    to: { node: edge.target, port: edge.targetHandle ?? '' },
  };
}

// --------------------------------------------------------------------------
// Connections (Requirements 1.3, 1.4)
// --------------------------------------------------------------------------

/** The endpoints of a dragged or existing connection. */
export interface ConnectionEndpoints {
  source: string;
  sourceHandle?: string | null;
  target: string;
  targetHandle?: string | null;
}

/** Deterministic edge id from the connection endpoints. */
export function edgeIdFor(connection: ConnectionEndpoints): string {
  const { source, sourceHandle, target, targetHandle } = connection;
  return `${source}.${sourceHandle ?? ''}->${target}.${targetHandle ?? ''}`;
}

/** True when `edge` joins the same ports as `connection`. */
export function isSameConnection(edge: Edge, connection: ConnectionEndpoints): boolean {
  return (
    edge.source === connection.source &&
    (edge.sourceHandle ?? null) === (connection.sourceHandle ?? null) &&
    edge.target === connection.target &&
    (edge.targetHandle ?? null) === (connection.targetHandle ?? null)
  );
}

/**
 * Why a dragged connection must be rejected, or null when it is
 * acceptable (Requirements 1.3, 1.4).
 *
 * Resolves the source output port type and the target input port type
 * (honoring per-instance port type overrides on custom Python nodes)
 * and applies `arePortsCompatible` via `incompatibilityReason`.
 */
export function connectionRejectionReason(
  connection: ConnectionEndpoints,
  nodes: BuilderNode[]
): string | null {
  if (connection.source === connection.target) {
    return 'Cannot connect a node to itself';
  }

  const sourceNode = nodes.find((node) => node.id === connection.source);
  const targetNode = nodes.find((node) => node.id === connection.target);
  if (sourceNode === undefined || targetNode === undefined) {
    return 'Connection endpoints must be nodes on the canvas';
  }

  const sourcePorts = resolvedPorts(toWorkflowNode(sourceNode), sourceNode.data.descriptor);
  const targetPorts = resolvedPorts(toWorkflowNode(targetNode), targetNode.data.descriptor);

  const sourceHandle = connection.sourceHandle ?? '';
  const targetHandle = connection.targetHandle ?? '';
  const sourceType = sourcePorts.outputs[sourceHandle];
  const targetType = targetPorts.inputs[targetHandle];

  if (sourceType === undefined) {
    return `Node '${connection.source}' has no output port '${sourceHandle}'`;
  }
  if (targetType === undefined) {
    return `Node '${connection.target}' has no input port '${targetHandle}'`;
  }

  return incompatibilityReason(sourceType, targetType);
}

/**
 * Remove the given nodes and every connection attached to them
 * (Requirement 1.5). Pure counterpart of the canvas delete behavior.
 */
export function removeNodesAndAttachedEdges(
  nodes: BuilderNode[],
  edges: Edge[],
  nodeIds: Iterable<string>
): { nodes: BuilderNode[]; edges: Edge[] } {
  const removed = new Set(nodeIds);
  return {
    nodes: nodes.filter((node) => !removed.has(node.id)),
    edges: edges.filter((edge) => !removed.has(edge.source) && !removed.has(edge.target)),
  };
}

// --------------------------------------------------------------------------
// Translation to/from full Workflow_Definition documents (Requirement 5.4)
// --------------------------------------------------------------------------

/** The Workflow_Definition document for the current canvas state. */
export function toWorkflowDefinition(nodes: BuilderNode[], edges: Edge[]): WorkflowDefinition {
  return {
    schemaVersion: SCHEMA_VERSION,
    nodes: [...nodes].sort((a, b) => a.id.localeCompare(b.id)).map(toWorkflowNode),
    connections: [...edges].sort((a, b) => a.id.localeCompare(b.id)).map(toWorkflowConnection),
  };
}

/**
 * Rebuild canvas nodes and edges from a stored Workflow_Definition so an
 * opened workflow renders its saved nodes, positions, configurations,
 * and connections (Requirement 5.4). Throws when the definition
 * references a node type absent from the catalog, since such a node
 * cannot be rendered or configured.
 */
export function fromWorkflowDefinition(
  definition: WorkflowDefinition,
  catalog: NodeTypeDescriptor[]
): { nodes: BuilderNode[]; edges: Edge[] } {
  const descriptors = new Map(catalog.map((descriptor) => [descriptor.typeId, descriptor]));

  const nodes: BuilderNode[] = definition.nodes.map((node) => {
    const descriptor = descriptors.get(node.type);
    if (descriptor === undefined) {
      throw new Error(`Workflow references unknown node type '${node.type}' (node '${node.id}')`);
    }
    return {
      id: node.id,
      type: WORKFLOW_NODE_TYPE,
      position: { x: node.position.x, y: node.position.y },
      data: {
        descriptor,
        parameters: { ...node.parameters },
        validationMessages: [],
        // Advisory node data (e.g. cameraBindingHint) is restored so the
        // hint survives save/load round trips (Requirement 7.5).
        ...(node.data !== undefined && Object.keys(node.data).length > 0
          ? { advisoryData: { ...node.data } }
          : {}),
      },
    };
  });

  const edges: Edge[] = definition.connections.map((connection) => ({
    id: connection.id,
    source: connection.from.node,
    sourceHandle: connection.from.port,
    target: connection.to.node,
    targetHandle: connection.to.port,
  }));

  return { nodes, edges };
}
