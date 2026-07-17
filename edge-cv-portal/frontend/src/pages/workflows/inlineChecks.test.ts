import { describe, it, expect } from 'vitest';
import {
  checkV4,
  checkV5,
  CODE_V4_INVALID_PARAMETER_VALUE,
  CODE_V4_MISSING_REQUIRED_PARAMETER,
  CODE_V5_UNREACHABLE_NODE,
  resolvedPorts,
  runInlineChecks,
} from './inlineChecks';
import {
  CATEGORY_INPUT,
  CATEGORY_OUTPUT,
  CATEGORY_POST_PROCESSING,
  CATEGORY_PREPROCESSING,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_INFERENCE_META,
  PORT_TYPE_VIDEO_FRAMES,
  type NodeTypeDescriptor,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

/**
 * Unit tests for the TypeScript mirror of validator checks V4 and V5
 * (Requirements 1.9, 4.4, 4.5) in `workflow_core.validator.checks`.
 */

// Minimal catalog fixture mirroring the shapes served by the
// node-catalog endpoint (camera source, capture output, custom python).
const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: CATEGORY_INPUT,
  displayName: 'Camera Source',
  inputs: [],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    { name: 'device', paramType: 'string', required: true, default: null, constraints: { minLength: 1 } },
    { name: 'gain', paramType: 'int', required: false, default: 0, constraints: { min: 0, max: 100 } },
  ],
  mappings: [],
  hardwareDependent: true,
};

const CAPTURE: NodeTypeDescriptor = {
  typeId: 'capture',
  category: CATEGORY_OUTPUT,
  displayName: 'Capture',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [],
  parameters: [
    { name: 'path', paramType: 'string', required: true, default: '/aws_dda/captures', constraints: { minLength: 1 } },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CUSTOM_PYTHON: NodeTypeDescriptor = {
  typeId: 'custom_python',
  category: CATEGORY_POST_PROCESSING,
  displayName: 'Custom Python',
  inputs: [{ name: 'in', portType: PORT_TYPE_INFERENCE_META }],
  outputs: [{ name: 'out', portType: PORT_TYPE_INFERENCE_META }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: { minLength: 1 } },
    {
      name: 'input_port_type',
      paramType: 'enum',
      required: true,
      default: PORT_TYPE_INFERENCE_META,
      constraints: { values: [PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_INFERENCE_META, PORT_TYPE_EVENT_SIGNAL] },
    },
    {
      name: 'output_port_type',
      paramType: 'enum',
      required: true,
      default: PORT_TYPE_INFERENCE_META,
      constraints: { values: [PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_INFERENCE_META, PORT_TYPE_EVENT_SIGNAL] },
    },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CUSTOM_PYTHON_PREPROCESS: NodeTypeDescriptor = {
  typeId: 'custom_python_preprocess',
  category: CATEGORY_PREPROCESSING,
  displayName: 'Custom Python (Frames)',
  inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: { minLength: 1 } },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CATALOG = [CAMERA, CAPTURE, CUSTOM_PYTHON, CUSTOM_PYTHON_PREPROCESS];

function node(id: string, type: string, parameters: WorkflowNode['parameters'] = {}): WorkflowNode {
  return { id, type, position: { x: 0, y: 0 }, parameters };
}

function conn(id: string, fromNode: string, toNode: string, fromPort = 'out', toPort = 'in'): WorkflowConnection {
  return { id, from: { node: fromNode, port: fromPort }, to: { node: toNode, port: toPort } };
}

describe('checkV4', () => {
  it('returns no findings when required parameters are satisfied', () => {
    const graph = { nodes: [node('n1', 'camera_source', { device: '/dev/video0' })], connections: [] };
    expect(checkV4(graph, CATALOG)).toEqual([]);
  });

  it('reports V4_MISSING_REQUIRED_PARAMETER when a required parameter has no value', () => {
    const graph = { nodes: [node('n1', 'camera_source')], connections: [] };
    const findings = checkV4(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V4_MISSING_REQUIRED_PARAMETER);
    expect(findings[0].severity).toBe('error');
    expect(findings[0].nodeId).toBe('n1');
    expect(findings[0].message).toContain('device');
  });

  it('treats an explicit null as a cleared value even when a default exists', () => {
    // capture.path is required with a default; explicit null clears it.
    const graph = { nodes: [node('n1', 'capture', { path: null })], connections: [] };
    const findings = checkV4(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V4_MISSING_REQUIRED_PARAMETER);
  });

  it('satisfies required parameters through their declared defaults when omitted', () => {
    const graph = { nodes: [node('n1', 'capture')], connections: [] };
    expect(checkV4(graph, CATALOG)).toEqual([]);
  });

  it('reports V4_INVALID_PARAMETER_VALUE for constraint violations', () => {
    const graph = {
      nodes: [node('n1', 'camera_source', { device: '/dev/video0', gain: 500 })],
      connections: [],
    };
    const findings = checkV4(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V4_INVALID_PARAMETER_VALUE);
    expect(findings[0].nodeId).toBe('n1');
  });

  it('skips nodes with unknown types', () => {
    const graph = { nodes: [node('n1', 'not_in_catalog')], connections: [] };
    expect(checkV4(graph, CATALOG)).toEqual([]);
  });

  it('reports a required-parameter marker for a custom_python_preprocess node without code (custom-python-frames Requirement 7.4)', () => {
    const graph = { nodes: [node('n1', 'custom_python_preprocess')], connections: [] };
    const findings = checkV4(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V4_MISSING_REQUIRED_PARAMETER);
    expect(findings[0].severity).toBe('error');
    expect(findings[0].nodeId).toBe('n1');
    expect(findings[0].message).toContain('code');
  });

  it('reports no marker once the custom_python_preprocess code parameter has a value', () => {
    const graph = {
      nodes: [node('n1', 'custom_python_preprocess', { code: 'def process_frame(frame, metadata):\n    return None' })],
      connections: [],
    };
    expect(checkV4(graph, CATALOG)).toEqual([]);
  });
});

describe('checkV5', () => {
  it('returns no findings when every node is reachable from an input node', () => {
    const graph = {
      nodes: [node('n1', 'camera_source', { device: 'd' }), node('n2', 'capture')],
      connections: [conn('c1', 'n1', 'n2')],
    };
    expect(checkV5(graph, CATALOG)).toEqual([]);
  });

  it('reports V5_UNREACHABLE_NODE for detached nodes', () => {
    const graph = {
      nodes: [node('n1', 'camera_source', { device: 'd' }), node('n2', 'capture')],
      connections: [],
    };
    const findings = checkV5(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V5_UNREACHABLE_NODE);
    expect(findings[0].nodeId).toBe('n2');
  });

  it('follows connections transitively (forward BFS)', () => {
    const graph = {
      nodes: [
        node('n1', 'camera_source', { device: 'd' }),
        node('n2', 'custom_python', { code: 'x' }),
        node('n3', 'capture'),
      ],
      connections: [conn('c1', 'n1', 'n2'), conn('c2', 'n2', 'n3')],
    };
    expect(checkV5(graph, CATALOG)).toEqual([]);
  });

  it('does not treat non-input nodes as reachability roots', () => {
    // A chain starting from a non-input node is entirely unreachable.
    const graph = {
      nodes: [node('n1', 'custom_python', { code: 'x' }), node('n2', 'capture')],
      connections: [conn('c1', 'n1', 'n2')],
    };
    const findings = checkV5(graph, CATALOG);
    expect(findings.map((f) => f.nodeId).sort()).toEqual(['n1', 'n2']);
  });

  it('reports unknown-typed nodes as unreachable when not fed by an input node', () => {
    const graph = {
      nodes: [node('n1', 'not_in_catalog')],
      connections: [],
    };
    const findings = checkV5(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].nodeId).toBe('n1');
  });

  it('returns no findings for an empty graph', () => {
    expect(checkV5({ nodes: [], connections: [] }, CATALOG)).toEqual([]);
  });
});

describe('resolvedPorts', () => {
  it('returns the declared port types for ordinary nodes', () => {
    const { inputs, outputs } = resolvedPorts(node('n1', 'capture'), CAPTURE);
    expect(inputs).toEqual({ in: PORT_TYPE_VIDEO_FRAMES });
    expect(outputs).toEqual({});
  });

  it('overrides custom_python port types from per-instance parameters', () => {
    const instance = node('n1', 'custom_python', {
      code: 'x',
      input_port_type: PORT_TYPE_VIDEO_FRAMES,
      output_port_type: PORT_TYPE_EVENT_SIGNAL,
    });
    const { inputs, outputs } = resolvedPorts(instance, CUSTOM_PYTHON);
    expect(inputs).toEqual({ in: PORT_TYPE_VIDEO_FRAMES });
    expect(outputs).toEqual({ out: PORT_TYPE_EVENT_SIGNAL });
  });

  it('ignores unknown port type override values', () => {
    const instance = node('n1', 'custom_python', { code: 'x', input_port_type: 'Bogus' });
    const { inputs, outputs } = resolvedPorts(instance, CUSTOM_PYTHON);
    expect(inputs).toEqual({ in: PORT_TYPE_INFERENCE_META }); // declared default kept
    expect(outputs).toEqual({ out: PORT_TYPE_INFERENCE_META });
  });
});

describe('runInlineChecks', () => {
  it('combines V4 and V5 findings', () => {
    const graph = {
      nodes: [node('n1', 'camera_source'), node('n2', 'capture')],
      connections: [],
    };
    const findings = runInlineChecks(graph, CATALOG);
    const codes = findings.map((f) => f.code).sort();
    expect(codes).toEqual([CODE_V4_MISSING_REQUIRED_PARAMETER, CODE_V5_UNREACHABLE_NODE]);
  });

  it('clears findings once the offending conditions are resolved', () => {
    const broken = {
      nodes: [node('n1', 'camera_source'), node('n2', 'capture')],
      connections: [],
    };
    expect(runInlineChecks(broken, CATALOG).length).toBeGreaterThan(0);

    const fixed = {
      nodes: [node('n1', 'camera_source', { device: '/dev/video0' }), node('n2', 'capture')],
      connections: [conn('c1', 'n1', 'n2')],
    };
    expect(runInlineChecks(fixed, CATALOG)).toEqual([]);
  });
});
