/**
 * Frontend example/interaction tests for the Triggers stage and unified
 * input designer support (triggers-stage-and-unified-input, task 5.6).
 *
 * Covers:
 *  - the `types.ts` catalog mirror (`CATEGORY_TRIGGER`, `CATEGORIES`
 *    order, `SOURCE_KIND_TO_SOURCE_TYPE`) — Requirement 1.4
 *  - Node_Palette section ordering and trigger grouping — Requirements
 *    1.5, 1.6, 2.8, 5.1, 5.2
 *  - edit-free save round trip of a saved `digital_input` node —
 *    Requirements 2.6, 5.2, 5.3
 *  - `source_kind` parameter gating in the config panel — Requirement 5.4
 *  - activation-port wiring acceptance/rejection — Requirements 5.5, 5.6
 *  - inline validation mirrors (V5 trigger roots, V7 stage order, silent
 *    unconnected activation port) — Requirements 5.7, 5.8
 */

import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import NodePalette from './NodePalette';
import {
  CATEGORY_META,
  connectionRejectionReason,
  fromWorkflowDefinition,
  toWorkflowDefinition,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
} from './builderGraph';
import { incompatibilityReason } from './compatibility';
import {
  checkV5,
  checkV7,
  CODE_V7_STAGE_ORDER,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import {
  SOURCE_KIND_PARAMETER,
  UNIFIED_INPUT_TYPE_ID,
  unifiedVisibleParameterNames,
} from './NodeConfigPanel';
import {
  CATEGORIES,
  CATEGORY_INFERENCE,
  CATEGORY_INPUT,
  CATEGORY_OUTPUT,
  CATEGORY_POST_PROCESSING,
  CATEGORY_PREPROCESSING,
  CATEGORY_TRIGGER,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_VIDEO_FRAMES,
  SCHEMA_VERSION,
  SOURCE_KIND_TO_SOURCE_TYPE,
  type JsonValue,
  type NodeCategory,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type WorkflowDefinition,
} from './types';

// --------------------------------------------------------------------------
// Catalog fixtures (camelCase wire shapes as served by the node-catalog
// endpoint), mirroring the relevant Python descriptors.
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
    param('poll_interval_ms', 'int', {
      default: 100,
      constraints: { min: 10, max: 60000 },
    }),
  ],
  mappings: [],
  hardwareDependent: true,
};

const CSI_CAMERA_SOURCE: NodeTypeDescriptor = {
  typeId: 'csi_camera_source',
  category: CATEGORY_INPUT,
  displayName: 'CSI camera',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('sensor_id', 'int', { default: 0 }), param('gain', 'float'), param('exposure', 'float')],
  mappings: [],
  hardwareDependent: true,
};

const ICAM_SOURCE: NodeTypeDescriptor = {
  typeId: 'icam_source',
  category: CATEGORY_INPUT,
  displayName: 'iCam',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('camera_ref', 'string'), param('gain', 'float'), param('exposure', 'float')],
  mappings: [],
  hardwareDependent: true,
};

const ARAVIS_CAMERA_SOURCE: NodeTypeDescriptor = {
  typeId: 'aravis_camera_source',
  category: CATEGORY_INPUT,
  displayName: 'Aravis camera',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('aravis_camera_ref', 'string'), param('pixel_format', 'string')],
  mappings: [],
  hardwareDependent: true,
};

const FOLDER_SOURCE: NodeTypeDescriptor = {
  typeId: 'folder_source',
  category: CATEGORY_INPUT,
  displayName: 'Folder source',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [param('location', 'string', { default: '/aws_dda/images' }), param('fps', 'int', { default: 1 })],
  mappings: [],
  hardwareDependent: false,
};

/** Union of the four source descriptors' parameters, de-duplicated by name. */
const UNION_PARAMETERS: ParameterDescriptor[] = (() => {
  const seen = new Map<string, ParameterDescriptor>();
  for (const source of [CSI_CAMERA_SOURCE, ICAM_SOURCE, ARAVIS_CAMERA_SOURCE, FOLDER_SOURCE]) {
    for (const parameter of source.parameters) {
      if (!seen.has(parameter.name)) {
        seen.set(parameter.name, { ...parameter, required: false });
      }
    }
  }
  return [...seen.values()];
})();

const UNIFIED_INPUT: NodeTypeDescriptor = {
  typeId: UNIFIED_INPUT_TYPE_ID,
  category: CATEGORY_INPUT,
  displayName: 'Unified input',
  inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    param(SOURCE_KIND_PARAMETER, 'enum', {
      required: true,
      default: 'folder',
      constraints: { values: Object.keys(SOURCE_KIND_TO_SOURCE_TYPE) },
    }),
    ...UNION_PARAMETERS,
  ],
  mappings: [],
  hardwareDependent: false,
};

const CAPTURE: NodeTypeDescriptor = {
  typeId: 'capture',
  category: CATEGORY_OUTPUT,
  displayName: 'Capture',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [],
  parameters: [param('path', 'string', { required: true, default: '/aws_dda/captures' })],
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

const MODEL_INFERENCE: NodeTypeDescriptor = {
  typeId: 'model_inference',
  category: CATEGORY_INFERENCE,
  displayName: 'Model inference',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [],
  mappings: [],
  hardwareDependent: false,
};

const INFERENCE_FILTER: NodeTypeDescriptor = {
  typeId: 'inference_filter',
  category: CATEGORY_POST_PROCESSING,
  displayName: 'Inference filter',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [],
  mappings: [],
  hardwareDependent: false,
};

const CATALOG: NodeTypeDescriptor[] = [
  DIGITAL_INPUT,
  CSI_CAMERA_SOURCE,
  ICAM_SOURCE,
  ARAVIS_CAMERA_SOURCE,
  FOLDER_SOURCE,
  UNIFIED_INPUT,
  CROP,
  MODEL_INFERENCE,
  INFERENCE_FILTER,
  CAPTURE,
];

function builderNode(
  id: string,
  descriptor: NodeTypeDescriptor,
  parameters: Record<string, JsonValue> = {}
): BuilderNode {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor, parameters, validationMessages: [] },
  };
}

// --------------------------------------------------------------------------
// types.ts catalog mirror (Requirement 1.4)
// --------------------------------------------------------------------------

describe('types.ts catalog mirror (Requirement 1.4)', () => {
  it('defines CATEGORY_TRIGGER with the backend string value', () => {
    expect(CATEGORY_TRIGGER).toBe('trigger');
  });

  it('places CATEGORY_TRIGGER first in CATEGORIES, immediately before CATEGORY_INPUT, preserving the existing order', () => {
    expect(CATEGORIES).toEqual([
      CATEGORY_TRIGGER,
      CATEGORY_INPUT,
      CATEGORY_PREPROCESSING,
      CATEGORY_INFERENCE,
      CATEGORY_POST_PROCESSING,
      CATEGORY_OUTPUT,
    ]);
  });

  it('includes trigger in the NodeCategory union', () => {
    // Type-level check: this assignment compiles only when the
    // NodeCategory union (derived from CATEGORIES) includes 'trigger'.
    const category: NodeCategory = CATEGORY_TRIGGER;
    expect((CATEGORIES as readonly string[]).includes(category)).toBe(true);
  });

  it('mirrors the Python SOURCE_KIND_TO_SOURCE_TYPE map exactly (keys and values)', () => {
    expect(SOURCE_KIND_TO_SOURCE_TYPE).toEqual({
      csi_camera: 'csi_camera_source',
      icam: 'icam_source',
      aravis_camera: 'aravis_camera_source',
      folder: 'folder_source',
    });
  });
});

// --------------------------------------------------------------------------
// Palette section ordering and grouping (Requirements 1.5, 1.6, 2.8, 5.1)
// --------------------------------------------------------------------------

describe('palette category metadata and ordering (Requirements 1.5, 1.6, 5.1)', () => {
  it('declares Triggers section metadata for the trigger category', () => {
    expect(CATEGORY_META[CATEGORY_TRIGGER]).toBeDefined();
    expect(CATEGORY_META[CATEGORY_TRIGGER].label).toBe('Triggers');
  });

  it('orders Triggers before Inputs and keeps the existing section order (logic level)', () => {
    const order = CATEGORIES as readonly string[];
    expect(order.indexOf(CATEGORY_TRIGGER)).toBeLessThan(order.indexOf(CATEGORY_INPUT));
    expect(order.indexOf(CATEGORY_INPUT)).toBeLessThan(order.indexOf(CATEGORY_PREPROCESSING));
    expect(order.indexOf(CATEGORY_PREPROCESSING)).toBeLessThan(order.indexOf(CATEGORY_INFERENCE));
    expect(order.indexOf(CATEGORY_INFERENCE)).toBeLessThan(order.indexOf(CATEGORY_POST_PROCESSING));
    expect(order.indexOf(CATEGORY_POST_PROCESSING)).toBeLessThan(order.indexOf(CATEGORY_OUTPUT));
  });

  it('renders the Triggers section before Inputs with the existing sections in order', () => {
    render(<NodePalette catalog={CATALOG} />);
    const palette = screen.getByRole('navigation', { name: 'Node palette' });
    const labels = within(palette)
      .getAllByRole('region')
      .map((section) => section.getAttribute('aria-label'));
    expect(labels).toEqual([
      'Triggers',
      'Input',
      'Preprocessing',
      'Model inference',
      'Post-processing',
      'Output',
    ]);
  });
});

describe('digital_input grouping (Requirements 2.8, 5.2)', () => {
  it('lists digital_input under Triggers and not under Inputs when the served catalog declares category trigger', () => {
    render(<NodePalette catalog={CATALOG} />);
    const triggersSection = screen.getByRole('region', { name: 'Triggers' });
    expect(within(triggersSection).getByText('Digital input')).toBeInTheDocument();

    const inputSection = screen.getByRole('region', { name: 'Input' });
    expect(within(inputSection).queryByText('Digital input')).not.toBeInTheDocument();
    // It appears exactly once across the whole palette.
    expect(screen.getAllByText('Digital input')).toHaveLength(1);
  });

  it('groups digital_input into the trigger section at the logic level', () => {
    const byCategory = new Map(
      CATEGORIES.map((category) => [
        category,
        CATALOG.filter((descriptor) => descriptor.category === category).map((d) => d.typeId),
      ])
    );
    expect(byCategory.get(CATEGORY_TRIGGER)).toContain('digital_input');
    expect(byCategory.get(CATEGORY_INPUT)).not.toContain('digital_input');
  });
});

// --------------------------------------------------------------------------
// Saved digital_input round trip (Requirements 2.6, 5.2, 5.3)
// --------------------------------------------------------------------------

describe('saved digital_input round trip (Requirements 2.6, 5.2, 5.3)', () => {
  // Ids are pre-sorted so the canonical serializer ordering matches the
  // stored ordering, making byte-identity meaningful.
  const SAVED: WorkflowDefinition = {
    schemaVersion: SCHEMA_VERSION,
    nodes: [
      {
        id: 'capture_1',
        type: 'capture',
        position: { x: 500, y: 80 },
        parameters: { path: '/aws_dda/captures' },
      },
      {
        id: 'digital_input_1',
        type: 'digital_input',
        position: { x: 40, y: 200 },
        parameters: { pin: 4, trigger_edge: 'falling', poll_interval_ms: 250 },
      },
      {
        id: 'folder_source_1',
        type: 'folder_source',
        position: { x: 40, y: 80 },
        parameters: { location: '/aws_dda/images', fps: 5 },
      },
    ],
    connections: [
      {
        id: 'c1',
        from: { node: 'folder_source_1', port: 'out' },
        to: { node: 'capture_1', port: 'in' },
      },
    ],
  };

  it('resolves the saved digital_input node to the trigger-category descriptor at render time', () => {
    const { nodes } = fromWorkflowDefinition(SAVED, CATALOG);
    const digital = nodes.find((node) => node.id === 'digital_input_1');
    expect(digital).toBeDefined();
    expect(digital!.data.descriptor.typeId).toBe('digital_input');
    expect(digital!.data.descriptor.category).toBe(CATEGORY_TRIGGER);
  });

  it('preserves the stored definition byte-identically on an edit-free save', () => {
    const { nodes, edges } = fromWorkflowDefinition(SAVED, CATALOG);
    const roundTripped = toWorkflowDefinition(nodes, edges);
    expect(roundTripped).toEqual(SAVED);
    expect(JSON.stringify(roundTripped)).toBe(JSON.stringify(SAVED));
  });

  it('preserves the digital_input node record itself byte-identically', () => {
    const { nodes, edges } = fromWorkflowDefinition(SAVED, CATALOG);
    const roundTripped = toWorkflowDefinition(nodes, edges);
    const before = SAVED.nodes.find((node) => node.id === 'digital_input_1');
    const after = roundTripped.nodes.find((node) => node.id === 'digital_input_1');
    expect(JSON.stringify(after)).toBe(JSON.stringify(before));
  });
});

// --------------------------------------------------------------------------
// source_kind parameter gating (Requirement 5.4)
// --------------------------------------------------------------------------

describe('unifiedVisibleParameterNames gating (Requirement 5.4)', () => {
  it.each(Object.entries(SOURCE_KIND_TO_SOURCE_TYPE))(
    'shows source_kind plus exactly the %s source parameters',
    (sourceKind, sourceTypeId) => {
      const node = builderNode('unified_input_1', UNIFIED_INPUT, {
        [SOURCE_KIND_PARAMETER]: sourceKind,
      });
      const visible = unifiedVisibleParameterNames(node, CATALOG);
      expect(visible).not.toBeNull();

      const underlying = CATALOG.find((descriptor) => descriptor.typeId === sourceTypeId)!;
      const expected = new Set([
        SOURCE_KIND_PARAMETER,
        ...underlying.parameters.map((parameter) => parameter.name),
      ]);
      expect(visible).toEqual(expected);
    }
  );

  it('keeps source_kind visible when the value comes from the declared default', () => {
    const node = builderNode('unified_input_1', UNIFIED_INPUT, {});
    const visible = unifiedVisibleParameterNames(node, CATALOG);
    expect(visible).not.toBeNull();
    expect(visible!.has(SOURCE_KIND_PARAMETER)).toBe(true);
    // The default is 'folder', so the folder_source parameters gate in.
    const expected = new Set([
      SOURCE_KIND_PARAMETER,
      ...FOLDER_SOURCE.parameters.map((parameter) => parameter.name),
    ]);
    expect(visible).toEqual(expected);
  });

  it('returns null for non-unified nodes (no gating applied)', () => {
    const node = builderNode('folder_source_1', FOLDER_SOURCE, {});
    expect(unifiedVisibleParameterNames(node, CATALOG)).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Activation-port wiring (Requirements 5.5, 5.6)
// --------------------------------------------------------------------------

describe('activation-port wiring (Requirements 5.5, 5.6)', () => {
  const trigger = builderNode('digital_input_1', DIGITAL_INPUT, { pin: 4 });
  const unified = builderNode('unified_input_1', UNIFIED_INPUT, { source_kind: 'folder' });
  const folder = builderNode('folder_source_1', FOLDER_SOURCE, {});
  const nodes = [trigger, unified, folder];

  it('accepts a trigger EventSignal output wired to the unified activation port', () => {
    const reason = connectionRejectionReason(
      {
        source: 'digital_input_1',
        sourceHandle: 'out',
        target: 'unified_input_1',
        targetHandle: 'activation',
      },
      nodes
    );
    expect(reason).toBeNull();
  });

  it('treats EventSignal -> EventSignal as compatible at the port-type level', () => {
    expect(incompatibilityReason(PORT_TYPE_EVENT_SIGNAL, PORT_TYPE_EVENT_SIGNAL)).toBeNull();
  });

  it('rejects a VideoFrames output wired to the activation port with a non-null reason', () => {
    const reason = connectionRejectionReason(
      {
        source: 'folder_source_1',
        sourceHandle: 'out',
        target: 'unified_input_1',
        targetHandle: 'activation',
      },
      nodes
    );
    expect(reason).not.toBeNull();
    expect(incompatibilityReason(PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_EVENT_SIGNAL)).not.toBeNull();
  });
});

// --------------------------------------------------------------------------
// Inline validation mirrors (Requirements 5.7, 5.8)
// --------------------------------------------------------------------------

describe('inline validation mirrors (Requirements 5.7, 5.8)', () => {
  it('checkV7 emits a V7_STAGE_ORDER error for a connection targeting a trigger node', () => {
    const graph: GraphLike = {
      nodes: [
        { id: 'f1', type: 'folder_source', position: { x: 0, y: 0 }, parameters: {} },
        { id: 'd1', type: 'digital_input', position: { x: 0, y: 0 }, parameters: { pin: 1 } },
      ],
      connections: [
        { id: 'bad', from: { node: 'f1', port: 'out' }, to: { node: 'd1', port: 'in' } },
      ],
    };
    const findings = checkV7(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V7_STAGE_ORDER);
    expect(findings[0].severity).toBe('error');
    expect(findings[0].connectionId).toBe('bad');
  });

  it('checkV7 emits no finding for a legal trigger -> unified activation edge', () => {
    const graph: GraphLike = {
      nodes: [
        { id: 'd1', type: 'digital_input', position: { x: 0, y: 0 }, parameters: { pin: 1 } },
        { id: 'u1', type: 'unified_input', position: { x: 0, y: 0 }, parameters: { source_kind: 'folder' } },
      ],
      connections: [
        { id: 'c1', from: { node: 'd1', port: 'out' }, to: { node: 'u1', port: 'activation' } },
      ],
    };
    expect(checkV7(graph, CATALOG)).toEqual([]);
  });

  it('checkV5 treats a digital_input trigger as a reachability root', () => {
    const graph: GraphLike = {
      nodes: [
        { id: 'd1', type: 'digital_input', position: { x: 0, y: 0 }, parameters: { pin: 1 } },
        { id: 'cap', type: 'capture', position: { x: 0, y: 0 }, parameters: {} },
      ],
      connections: [
        { id: 'c1', from: { node: 'd1', port: 'out' }, to: { node: 'cap', port: 'in' } },
      ],
    };
    expect(checkV5(graph, CATALOG)).toEqual([]);
  });

  it('an unconnected activation port yields no inline finding (Requirement 5.8)', () => {
    const graph: GraphLike = {
      nodes: [
        { id: 'u1', type: 'unified_input', position: { x: 0, y: 0 }, parameters: { source_kind: 'folder' } },
        { id: 'cap', type: 'capture', position: { x: 0, y: 0 }, parameters: {} },
      ],
      connections: [
        { id: 'c1', from: { node: 'u1', port: 'out' }, to: { node: 'cap', port: 'in' } },
      ],
    };
    expect(runInlineChecks(graph, CATALOG)).toEqual([]);
  });

  it('runInlineChecks includes V7 stage-order findings (Requirement 5.7)', () => {
    const graph: GraphLike = {
      nodes: [
        { id: 'f1', type: 'folder_source', position: { x: 0, y: 0 }, parameters: {} },
        { id: 'd1', type: 'digital_input', position: { x: 0, y: 0 }, parameters: { pin: 1 } },
      ],
      connections: [
        { id: 'bad', from: { node: 'f1', port: 'out' }, to: { node: 'd1', port: 'in' } },
      ],
    };
    const codes = runInlineChecks(graph, CATALOG).map((finding) => finding.code);
    expect(codes).toContain(CODE_V7_STAGE_ORDER);
  });
});
