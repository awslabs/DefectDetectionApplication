/**
 * **Feature: workflow-manager, Property 11: Node deletion leaves no dangling connections**
 *
 * For all workflow graphs and any node in them, deleting that node removes
 * the node and results in a graph containing no connection that references
 * the deleted node, while all connections between remaining nodes are
 * preserved.
 *
 * **Validates: Requirements 1.5**
 *
 * The pure counterpart of the canvas delete behavior is
 * `removeNodesAndAttachedEdges`. The property generates arbitrary canvas
 * graphs (random nodes plus random edges among them) and arbitrary subsets of
 * nodes to delete, then asserts: no deleted node survives, no surviving edge
 * references a deleted node, and every other node and edge is preserved
 * unchanged (same objects, same order).
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import type { Edge } from '@xyflow/react';
import {
  removeNodesAndAttachedEdges,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
} from './builderGraph';
import { PORT_TYPE_VIDEO_FRAMES, type NodeTypeDescriptor } from './types';

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** A minimal catalog descriptor; deletion behavior is type-agnostic. */
const descriptor: NodeTypeDescriptor = {
  typeId: 'test_node',
  category: 'preprocessing',
  displayName: 'Test node',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [],
  mappings: [],
  hardwareDependent: false,
};

function builderNode(id: string, index: number): BuilderNode {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: index * 100, y: index * 50 },
    data: { descriptor, parameters: {}, validationMessages: [] },
  };
}

/**
 * A random canvas graph plus a random subset of its nodes to delete:
 * 0..8 nodes, 0..12 edges wired between arbitrary node pairs (self-loops
 * and duplicate wirings included — deletion must handle any edge list).
 */
const scenarioArb = fc.integer({ min: 0, max: 8 }).chain((nodeCount) => {
  const ids = Array.from({ length: nodeCount }, (_, i) => `node_${i + 1}`);
  const endpointsArb =
    nodeCount === 0
      ? fc.constant<{ source: string; target: string }[]>([])
      : fc.array(
          fc.record({
            source: fc.constantFrom(...ids),
            target: fc.constantFrom(...ids),
          }),
          { minLength: 0, maxLength: 12 }
        );
  return fc.record({
    ids: fc.constant(ids),
    endpoints: endpointsArb,
    toDelete: fc.subarray(ids),
  });
});

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 11: Node deletion leaves no dangling connections', () => {
  it('removes exactly the deleted nodes and their attached edges, preserving everything else', () => {
    fc.assert(
      fc.property(scenarioArb, ({ ids, endpoints, toDelete }) => {
        const nodes = ids.map((id, i) => builderNode(id, i));
        const edges: Edge[] = endpoints.map(({ source, target }, i) => ({
          id: `edge_${i + 1}`,
          source,
          sourceHandle: 'out',
          target,
          targetHandle: 'in',
        }));

        const result = removeNodesAndAttachedEdges(nodes, edges, toDelete);
        const removed = new Set(toDelete);

        // No deleted node remains.
        for (const node of result.nodes) {
          expect(removed.has(node.id)).toBe(false);
        }

        // No surviving edge references a deleted node (no dangling ends).
        for (const edge of result.edges) {
          expect(removed.has(edge.source)).toBe(false);
          expect(removed.has(edge.target)).toBe(false);
        }

        // Every non-deleted node is preserved unchanged, in order.
        expect(result.nodes).toEqual(nodes.filter((node) => !removed.has(node.id)));

        // Every edge between remaining nodes is preserved unchanged, in order.
        expect(result.edges).toEqual(
          edges.filter((edge) => !removed.has(edge.source) && !removed.has(edge.target))
        );

        // Inputs are untouched (the helper is pure).
        expect(nodes).toHaveLength(ids.length);
        expect(edges).toHaveLength(endpoints.length);
      }),
      { numRuns: 200 }
    );
  });
});
