/**
 * Inline validation markers for the Workflow_Builder canvas
 * (Requirements 1.9, 1.10).
 *
 * On every graph mutation the canvas runs the TypeScript V4/V5 mirror
 * (`runInlineChecks`) over the current nodes and edges and writes each
 * finding's message onto the offending node's
 * `data.validationMessages` — the extension point the node component
 * renders as a warning badge. Nodes whose messages did not change keep
 * their object identity, and when no node changed the input array is
 * returned unchanged, so the canvas can re-apply markers after every
 * mutation without triggering render loops.
 */

import type { Edge } from '@xyflow/react';
import { toWorkflowConnection, toWorkflowNode, type BuilderNode } from './builderGraph';
import { runInlineChecks } from './inlineChecks';
import type { NodeTypeDescriptor } from './types';

/**
 * Run the inline checks (V4 + V5) on the canvas state and group the
 * finding messages by offending node id. Nodes without findings are
 * absent from the map.
 */
export function validationMessagesByNode(
  nodes: BuilderNode[],
  edges: Edge[],
  catalog: NodeTypeDescriptor[]
): Map<string, string[]> {
  const graph = {
    nodes: nodes.map(toWorkflowNode),
    connections: edges.map(toWorkflowConnection),
  };
  const messages = new Map<string, string[]>();
  for (const finding of runInlineChecks(graph, catalog)) {
    if (finding.nodeId === null) {
      continue;
    }
    const existing = messages.get(finding.nodeId);
    if (existing !== undefined) {
      existing.push(finding.message);
    } else {
      messages.set(finding.nodeId, [finding.message]);
    }
  }
  return messages;
}

function sameMessages(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((message, index) => message === b[index]);
}

/**
 * Return the nodes with `data.validationMessages` set to the inline
 * check findings targeting each node — adding markers to offending
 * nodes (Requirement 1.9) and clearing them when the condition is
 * resolved (Requirement 1.10).
 *
 * Identity-preserving: nodes whose messages are unchanged are returned
 * as the same object, and the input array itself is returned when no
 * node changed, so callers can `setNodes` with the result on every
 * mutation without causing infinite update loops.
 */
export function applyValidationMarkers(
  nodes: BuilderNode[],
  edges: Edge[],
  catalog: NodeTypeDescriptor[]
): BuilderNode[] {
  const messages = validationMessagesByNode(nodes, edges, catalog);
  let changed = false;
  const next = nodes.map((node) => {
    const nodeMessages = messages.get(node.id) ?? [];
    if (sameMessages(node.data.validationMessages, nodeMessages)) {
      return node;
    }
    changed = true;
    return { ...node, data: { ...node.data, validationMessages: nodeMessages } };
  });
  return changed ? next : nodes;
}
