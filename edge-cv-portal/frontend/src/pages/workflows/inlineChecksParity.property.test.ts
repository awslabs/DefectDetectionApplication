/**
 * **Feature: triggers-stage-and-unified-input, Property 6: Validator
 * finding-set equivalence for zero-trigger and digital_input graphs**
 *
 * Frontend parity half of P6 (task 7.6): for generated small graphs built
 * from served-catalog-style descriptor fixtures, `runInlineChecks`'
 * V5/V7 findings mirror the backend validator semantics:
 *
 * - V5 roots are the `CATEGORY_INPUT` and `CATEGORY_TRIGGER` nodes —
 *   `digital_input` counts as a reachability root exactly like the
 *   backend's widened `_check_v5`;
 * - every connection targeting a trigger node yields exactly one
 *   `V7_STAGE_ORDER` error finding carrying that connection's id
 *   (backend `_check_v7` parity);
 * - zero-trigger graphs yield zero `V7` findings.
 *
 * The oracle transcribes the backend semantics independently of
 * `inlineChecks.ts` (BFS from input/trigger roots; target-category
 * trigger scan), so agreement demonstrates frontend/backend parity for
 * the same graph.
 *
 * **Validates: Requirements 5.7, 2.7, 4.5**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  checkV5,
  checkV7,
  CODE_V5_UNREACHABLE_NODE,
  CODE_V7_STAGE_ORDER,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import {
  CATEGORY_INPUT,
  CATEGORY_OUTPUT,
  CATEGORY_PREPROCESSING,
  CATEGORY_TRIGGER,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_VIDEO_FRAMES,
  SEVERITY_ERROR,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Served-catalog-style descriptor fixtures (camelCase wire shapes, as in
// triggersStageAndUnifiedInput.test.tsx)
// --------------------------------------------------------------------------

function param(
  name: string,
  paramType: string,
  overrides: Partial<ParameterDescriptor> = {}
): ParameterDescriptor {
  return { name, paramType, required: false, default: null, ...overrides };
}

const DIGITAL_INPUT: NodeTypeDescriptor = {
  typeId: 'digital_input',
  category: CATEGORY_TRIGGER,
  displayName: 'Digital input',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_EVENT_SIGNAL }],
  parameters: [
    param('pin', 'int', { required: true, constraints: { min: 0, max: 255 } }),
    param('trigger_edge', 'enum', {
      default: 'rising',
      constraints: { values: ['rising', 'falling', 'both'] },
    }),
    param('poll_interval_ms', 'int', { default: 100, constraints: { min: 10, max: 60000 } }),
  ],
  mappings: [],
  hardwareDependent: true,
};

const FOLDER_SOURCE: NodeTypeDescriptor = {
  typeId: 'folder_source',
  category: CATEGORY_INPUT,
  displayName: 'Folder source',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('location', 'string', { required: true, default: '/aws_dda/images' })],
  mappings: [],
  hardwareDependent: false,
};

const CSI_CAMERA_SOURCE: NodeTypeDescriptor = {
  typeId: 'csi_camera_source',
  category: CATEGORY_INPUT,
  displayName: 'CSI camera',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('gain', 'float', { default: 4 }), param('exposure', 'float', { default: 5000000 })],
  mappings: [],
  hardwareDependent: true,
};

const UNIFIED_INPUT: NodeTypeDescriptor = {
  typeId: 'unified_input',
  category: CATEGORY_INPUT,
  displayName: 'Unified input',
  inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    param('source_kind', 'enum', {
      required: true,
      default: 'folder',
      constraints: { values: ['csi_camera', 'icam', 'aravis_camera', 'folder'] },
    }),
  ],
  mappings: [],
  hardwareDependent: false,
};

const CROP: NodeTypeDescriptor = {
  typeId: 'crop',
  category: CATEGORY_PREPROCESSING,
  displayName: 'Crop',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [],
  mappings: [],
  hardwareDependent: false,
};

const CAPTURE: NodeTypeDescriptor = {
  typeId: 'capture',
  category: CATEGORY_OUTPUT,
  displayName: 'Capture',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [],
  parameters: [param('output_path', 'string', { required: true, default: '/aws_dda/captures' })],
  mappings: [],
  hardwareDependent: false,
};

const CATALOG: NodeTypeDescriptor[] = [
  DIGITAL_INPUT,
  FOLDER_SOURCE,
  CSI_CAMERA_SOURCE,
  UNIFIED_INPUT,
  CROP,
  CAPTURE,
];

const NON_TRIGGER_TYPE_IDS = [
  'folder_source',
  'csi_camera_source',
  'unified_input',
  'crop',
  'capture',
] as const;

const ALL_TYPE_IDS = ['digital_input', ...NON_TRIGGER_TYPE_IDS] as const;

const DESCRIPTOR_BY_TYPE = new Map(CATALOG.map((d) => [d.typeId, d]));

/** Valid-enough parameter values per node type (V4 noise stays out). */
function parametersFor(typeId: string): Record<string, JsonValue> {
  switch (typeId) {
    case 'digital_input':
      return { pin: 4 };
    case 'folder_source':
      return { location: '/aws_dda/images' };
    case 'unified_input':
      return { source_kind: 'folder' };
    case 'capture':
      return { output_path: '/out' };
    default:
      return {};
  }
}

// --------------------------------------------------------------------------
// Graph generator: small graphs of 1..6 nodes with 0..8 connections
// wired by index (any node may target any other, so both offending
// connections into triggers and legal trigger->activation edges arise).
// --------------------------------------------------------------------------

interface EdgeSpec {
  source: number;
  target: number;
  targetPort: 'in' | 'activation';
}

function buildGraph(types: readonly string[], edgeSpecs: EdgeSpec[]): GraphLike {
  const nodes: WorkflowNode[] = types.map((typeId, index) => ({
    id: `n${index + 1}`,
    type: typeId,
    position: { x: 0, y: 0 },
    parameters: parametersFor(typeId),
  }));
  const connections: WorkflowConnection[] = edgeSpecs.map((spec, index) => ({
    id: `c${index + 1}`,
    from: { node: nodes[spec.source % nodes.length].id, port: 'out' },
    to: { node: nodes[spec.target % nodes.length].id, port: spec.targetPort },
  }));
  return { nodes, connections };
}

const edgeSpecArb: fc.Arbitrary<EdgeSpec> = fc.record({
  source: fc.nat(),
  target: fc.nat(),
  targetPort: fc.constantFrom<'in' | 'activation'>('in', 'activation'),
});

/** Graphs over the full fixture catalog (triggers may appear). */
const anyGraphArb: fc.Arbitrary<GraphLike> = fc
  .record({
    types: fc.array(fc.constantFrom(...ALL_TYPE_IDS), { minLength: 1, maxLength: 6 }),
    edgeSpecs: fc.array(edgeSpecArb, { minLength: 0, maxLength: 8 }),
  })
  .map(({ types, edgeSpecs }) => buildGraph(types, edgeSpecs));

/** Zero-trigger graphs: no digital_input (nor any trigger) node. */
const zeroTriggerGraphArb: fc.Arbitrary<GraphLike> = fc
  .record({
    types: fc.array(fc.constantFrom(...NON_TRIGGER_TYPE_IDS), { minLength: 1, maxLength: 6 }),
    edgeSpecs: fc.array(edgeSpecArb, { minLength: 0, maxLength: 8 }),
  })
  .map(({ types, edgeSpecs }) => buildGraph(types, edgeSpecs));

// --------------------------------------------------------------------------
// Oracle: the backend validator semantics, transcribed independently
// --------------------------------------------------------------------------

function categoryOf(graph: GraphLike, nodeId: string): string | undefined {
  const node = graph.nodes.find((n) => n.id === nodeId);
  return node === undefined ? undefined : DESCRIPTOR_BY_TYPE.get(node.type)?.category;
}

/**
 * Backend `_check_v5` semantics: forward BFS from the CATEGORY_INPUT and
 * CATEGORY_TRIGGER roots (the feature's widened root set); every node
 * not visited is unreachable.
 */
function expectedUnreachable(graph: GraphLike): Set<string> {
  const known = new Set(graph.nodes.map((node) => node.id));
  const successors = new Map<string, string[]>();
  for (const id of known) {
    successors.set(id, []);
  }
  for (const connection of graph.connections) {
    if (known.has(connection.from.node) && known.has(connection.to.node)) {
      successors.get(connection.from.node)!.push(connection.to.node);
    }
  }
  const roots = graph.nodes
    .filter((node) => {
      const category = DESCRIPTOR_BY_TYPE.get(node.type)?.category;
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
  return new Set(graph.nodes.map((n) => n.id).filter((id) => !visited.has(id)));
}

/**
 * Backend `_check_v7` semantics: the connections whose target resolves
 * to a CATEGORY_TRIGGER node, one finding each.
 */
function expectedOffendingConnectionIds(graph: GraphLike): string[] {
  return graph.connections
    .filter((connection) => categoryOf(graph, connection.to.node) === CATEGORY_TRIGGER)
    .map((connection) => connection.id);
}

// --------------------------------------------------------------------------
// Properties (>=100 runs each)
// --------------------------------------------------------------------------

describe('Property 6 (frontend parity): inline V5/V7 checks mirror the backend validator semantics', () => {
  it('checkV5 treats input AND trigger nodes as roots — findings are exactly the backend-unreachable nodes (digital_input counts as a root)', () => {
    fc.assert(
      fc.property(anyGraphArb, (graph) => {
        const findings = checkV5(graph, CATALOG);
        for (const finding of findings) {
          expect(finding.code).toBe(CODE_V5_UNREACHABLE_NODE);
          expect(finding.severity).toBe(SEVERITY_ERROR);
        }
        const actual = new Set(findings.map((finding) => finding.nodeId));
        expect(actual).toEqual(expectedUnreachable(graph));

        // digital_input is a root: it never carries a V5 finding.
        const triggerIds = graph.nodes
          .filter((node) => node.type === 'digital_input')
          .map((node) => node.id);
        for (const id of triggerIds) {
          expect(actual.has(id)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('any connection targeting a trigger yields exactly one V7_STAGE_ORDER finding carrying that connection id', () => {
    fc.assert(
      fc.property(anyGraphArb, (graph) => {
        const expected = expectedOffendingConnectionIds(graph).sort();

        const v7 = runInlineChecks(graph, CATALOG).filter(
          (finding) => finding.code === CODE_V7_STAGE_ORDER
        );
        for (const finding of v7) {
          expect(finding.severity).toBe(SEVERITY_ERROR);
        }
        // Exactly one finding per offending connection (multiset equality).
        expect(v7.map((finding) => finding.connectionId).sort()).toEqual(expected);
        // checkV7 alone agrees with the composed runInlineChecks slice.
        expect(
          checkV7(graph, CATALOG)
            .map((finding) => finding.connectionId)
            .sort()
        ).toEqual(expected);
      }),
      { numRuns: 100 }
    );
  });

  it('zero-trigger graphs yield zero V7 findings from runInlineChecks', () => {
    fc.assert(
      fc.property(zeroTriggerGraphArb, (graph) => {
        const v7 = runInlineChecks(graph, CATALOG).filter(
          (finding) => finding.code === CODE_V7_STAGE_ORDER
        );
        expect(v7).toEqual([]);
      }),
      { numRuns: 100 }
    );
  });
});
