/**
 * **Feature: custom-python-source, Property 17: Inline markers mirror the frame-feed coexistence rule**
 *
 * For any canvas graph over the node catalog, the Workflow_Builder's
 * inline checks produce a frame-feed conflict marker on exactly the
 * nodes the coexistence rule would flag, each naming the full
 * conflicting membership.
 *
 * **Validates: Requirements 8.4**
 *
 * The inline mirror under test is `checkV7Coexistence` (composed into
 * `runInlineChecks`); the oracle transcribes the backend
 * `_check_v7_coexistence` frame-feed semantics from
 * `workflow_core.validator.checks` independently of `inlineChecks.ts`:
 *
 * - both frame-feed source types present (`custom_python_source` AND
 *   `aravis_camera_source`) → one error finding per frame-feed node,
 *   membership = every frame-feed node of both types;
 * - otherwise, two or more nodes of one frame-feed singleton type →
 *   one error finding per node of that type, membership = the
 *   same-type node set;
 * - otherwise → zero coexistence findings.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  checkV7Coexistence,
  CODE_V7_COEXISTENCE_CONFLICT,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import {
  CATEGORY_INPUT,
  CATEGORY_OUTPUT,
  CATEGORY_PREPROCESSING,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_VIDEO_FRAMES,
  SEVERITY_ERROR,
  type JsonValue,
  type NodeTypeDescriptor,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Served-catalog-style descriptor fixtures (camelCase wire shapes)
// --------------------------------------------------------------------------

const ARAVIS_CAMERA_SOURCE: NodeTypeDescriptor = {
  typeId: 'aravis_camera_source',
  category: CATEGORY_INPUT,
  displayName: 'Aravis camera',
  inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    {
      name: 'camera_id',
      paramType: 'string',
      required: true,
      default: null,
      constraints: { minLength: 1 },
    },
  ],
  mappings: [],
  hardwareDependent: true,
};

const CUSTOM_PYTHON_SOURCE: NodeTypeDescriptor = {
  typeId: 'custom_python_source',
  category: CATEGORY_INPUT,
  displayName: 'Custom Python (Source)',
  inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    {
      name: 'code',
      paramType: 'code',
      required: true,
      default: null,
      constraints: { minLength: 1 },
    },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
    {
      name: 'allowed_uri_prefixes',
      paramType: 'string',
      required: false,
      default: '',
      constraints: {},
    },
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
  parameters: [
    {
      name: 'location',
      paramType: 'string',
      required: true,
      default: '/aws_dda/images',
      constraints: {},
    },
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
  parameters: [
    {
      name: 'output_path',
      paramType: 'string',
      required: true,
      default: '/aws_dda/captures',
      constraints: {},
    },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CATALOG: NodeTypeDescriptor[] = [
  ARAVIS_CAMERA_SOURCE,
  CUSTOM_PYTHON_SOURCE,
  FOLDER_SOURCE,
  CROP,
  CAPTURE,
];

const ALL_TYPE_IDS = [
  'aravis_camera_source',
  'custom_python_source',
  'folder_source',
  'crop',
  'capture',
] as const;

/** Valid-enough parameter values per node type (V4 noise stays out). */
function parametersFor(typeId: string): Record<string, JsonValue> {
  switch (typeId) {
    case 'aravis_camera_source':
      return { camera_id: 'Basler-40123456' };
    case 'custom_python_source':
      return { code: 'def produce_frame(context):\n    return None\n' };
    default:
      return {};
  }
}

// --------------------------------------------------------------------------
// Graph generator: canvas graphs of 1..7 nodes with 0..8 connections
// wired by index. Frame-feed types are drawn alongside the other types,
// so conflict-free graphs, same-type multiples, and mixed-membership
// graphs all arise.
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

const canvasGraphArb: fc.Arbitrary<GraphLike> = fc
  .record({
    types: fc.array(fc.constantFrom(...ALL_TYPE_IDS), { minLength: 1, maxLength: 7 }),
    edgeSpecs: fc.array(edgeSpecArb, { minLength: 0, maxLength: 8 }),
  })
  .map(({ types, edgeSpecs }) => buildGraph(types, edgeSpecs));

// --------------------------------------------------------------------------
// Oracle: the backend frame-feed coexistence semantics, transcribed
// independently of inlineChecks.ts
// --------------------------------------------------------------------------

/**
 * The node ids the coexistence rule flags, and the full conflicting
 * membership every finding must name. Empty flagged set = no conflict.
 */
function expectedConflict(graph: GraphLike): { flagged: string[]; membership: string[] } {
  const aravisIds = graph.nodes
    .filter((node) => node.type === 'aravis_camera_source')
    .map((node) => node.id);
  const customIds = graph.nodes
    .filter((node) => node.type === 'custom_python_source')
    .map((node) => node.id);

  if (aravisIds.length > 0 && customIds.length > 0) {
    const membership = [...aravisIds, ...customIds].sort();
    return { flagged: membership, membership };
  }
  if (customIds.length >= 2) {
    const membership = [...customIds].sort();
    return { flagged: membership, membership };
  }
  if (aravisIds.length >= 2) {
    const membership = [...aravisIds].sort();
    return { flagged: membership, membership };
  }
  return { flagged: [], membership: [] };
}

// --------------------------------------------------------------------------
// Property (>=100 runs)
// --------------------------------------------------------------------------

describe('Property 17: Inline markers mirror the frame-feed coexistence rule', () => {
  it('produces a frame-feed conflict marker on exactly the nodes the coexistence rule flags, each naming the full conflicting membership', () => {
    fc.assert(
      fc.property(canvasGraphArb, (graph) => {
        const { flagged, membership } = expectedConflict(graph);

        const markers = runInlineChecks(graph, CATALOG).filter(
          (finding) => finding.code === CODE_V7_COEXISTENCE_CONFLICT
        );

        // Exactly one marker per flagged node (multiset equality over
        // node ids); no marker on any other node.
        expect(markers.map((finding) => finding.nodeId).sort()).toEqual(flagged);

        for (const finding of markers) {
          expect(finding.severity).toBe(SEVERITY_ERROR);
          expect(finding.connectionId).toBeNull();
          // Every finding names every member of the conflicting set.
          for (const memberId of membership) {
            expect(finding.message).toContain(`'${memberId}'`);
          }
        }

        // checkV7Coexistence alone agrees with the composed
        // runInlineChecks slice.
        expect(checkV7Coexistence(graph).map((finding) => finding.nodeId).sort()).toEqual(
          flagged
        );
      }),
      { numRuns: 100 }
    );
  });
});
