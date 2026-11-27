import { describe, it, expect } from 'vitest';
import {
  checkV4,
  checkV5,
  checkV10Metadata,
  checkV14LayoutConfig,
  checkW4LayoutNoDetector,
  CODE_V4_INVALID_PARAMETER_VALUE,
  CODE_V4_MISSING_REQUIRED_PARAMETER,
  CODE_V5_UNREACHABLE_NODE,
  CODE_V10_METADATA_DUPLICATE_KEY,
  CODE_V10_METADATA_EMPTY_FIELD_PATH,
  CODE_V10_METADATA_MAPPINGS_INVALID,
  CODE_V10_METADATA_STATIC_JSON_INVALID,
  CODE_V14_LAYOUT_CONFIG_INVALID,
  CODE_W4_LAYOUT_NO_DETECTOR,
  parseExpectedRegions,
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
  SEVERITY_ERROR,
  SEVERITY_WARNING,
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

const CUSTOM_PYTHON_SOURCE: NodeTypeDescriptor = {
  typeId: 'custom_python_source',
  category: CATEGORY_INPUT,
  displayName: 'Custom Python (Source)',
  inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
  outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
  parameters: [
    { name: 'code', paramType: 'code', required: true, default: null, constraints: { minLength: 1 } },
    { name: 'requirements', paramType: 'string', required: false, default: '', constraints: {} },
    { name: 'allowed_uri_prefixes', paramType: 'string', required: false, default: '', constraints: {} },
  ],
  mappings: [],
  hardwareDependent: true,
};

const CATALOG = [CAMERA, CAPTURE, CUSTOM_PYTHON, CUSTOM_PYTHON_PREPROCESS, CUSTOM_PYTHON_SOURCE];

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

  it('reports a required-parameter marker for a custom_python_source node without code (custom-python-source Requirement 10.5)', () => {
    const graph = { nodes: [node('n1', 'custom_python_source')], connections: [] };
    const findings = checkV4(graph, CATALOG);
    expect(findings).toHaveLength(1);
    expect(findings[0].code).toBe(CODE_V4_MISSING_REQUIRED_PARAMETER);
    expect(findings[0].severity).toBe('error');
    expect(findings[0].nodeId).toBe('n1');
    expect(findings[0].message).toContain('code');
  });

  it('reports no marker once the custom_python_source code parameter has a value', () => {
    const graph = {
      nodes: [node('n1', 'custom_python_source', { code: 'def produce_frame(context):\n    return None' })],
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

// --------------------------------------------------------------------------
// V10: metadata node configuration validity
// (workflow-manager-gaps Requirements 6.3, 6.7)
// --------------------------------------------------------------------------

const METADATA: NodeTypeDescriptor = {
  typeId: 'metadata',
  category: CATEGORY_POST_PROCESSING,
  displayName: 'Metadata',
  inputs: [{ name: 'in', portType: PORT_TYPE_INFERENCE_META }],
  outputs: [{ name: 'out', portType: PORT_TYPE_INFERENCE_META }],
  parameters: [
    { name: 'mappings', paramType: 'string', required: false, default: '[]', constraints: {} },
    {
      name: 'static_json',
      paramType: 'string',
      required: false,
      default: '',
      constraints: { maxLength: 10240 },
    },
  ],
  mappings: [],
  hardwareDependent: false,
};

const METADATA_CATALOG = [...CATALOG, METADATA];

describe('checkV10Metadata', () => {
  it('returns no findings for a valid metadata configuration', () => {
    const graph = {
      nodes: [
        node('m1', 'metadata', {
          mappings: '[{"path": "job_id", "key": "job_id"}]',
          static_json: '{"station": "line-1"}',
        }),
      ],
      connections: [],
    };
    expect(checkV10Metadata(graph, METADATA_CATALOG)).toEqual([]);
  });

  it('returns no findings for the descriptor defaults (parameters unset)', () => {
    const graph = { nodes: [node('m1', 'metadata')], connections: [] };
    expect(checkV10Metadata(graph, METADATA_CATALOG)).toEqual([]);
  });

  it('reports one SEVERITY_ERROR per violated rule with the V10 codes', () => {
    const graph = {
      nodes: [
        node('m1', 'metadata', {
          // duplicate key + one empty path
          mappings: '[{"path": "a", "key": "k"}, {"path": "b", "key": "k"}, {"path": " ", "key": "x"}]',
          static_json: 'not json',
        }),
      ],
      connections: [],
    };
    const findings = checkV10Metadata(graph, METADATA_CATALOG);
    expect(findings.map((f) => f.code).sort()).toEqual([
      CODE_V10_METADATA_DUPLICATE_KEY,
      CODE_V10_METADATA_EMPTY_FIELD_PATH,
      CODE_V10_METADATA_STATIC_JSON_INVALID,
    ]);
    for (const finding of findings) {
      expect(finding.severity).toBe(SEVERITY_ERROR);
      expect(finding.nodeId).toBe('m1');
      expect(finding.message).toContain("Node 'm1':");
    }
  });

  it('reports V10_METADATA_MAPPINGS_INVALID for unparseable mappings', () => {
    const graph = {
      nodes: [node('m1', 'metadata', { mappings: '{"not": "an array"}' })],
      connections: [],
    };
    const findings = checkV10Metadata(graph, METADATA_CATALOG);
    expect(findings.map((f) => f.code)).toEqual([CODE_V10_METADATA_MAPPINGS_INVALID]);
  });

  it('fires only on metadata-typed nodes (metadata-free graphs unchanged)', () => {
    const graph = {
      nodes: [
        node('cam', 'camera_source', { device: '/dev/video0', mappings: 'not json' }),
      ],
      connections: [],
    };
    expect(checkV10Metadata(graph, METADATA_CATALOG)).toEqual([]);
  });

  it('is composed into runInlineChecks', () => {
    const graph = {
      nodes: [
        node('cam', 'camera_source', { device: '/dev/video0' }),
        node('m1', 'metadata', { static_json: '[1, 2]' }),
      ],
      connections: [conn('c1', 'cam', 'm1')],
    };
    const codes = runInlineChecks(graph, METADATA_CATALOG).map((f) => f.code);
    expect(codes).toContain(CODE_V10_METADATA_STATIC_JSON_INVALID);
  });
});

// --------------------------------------------------------------------------
// Layout Check mirrors (car-assembly-check task 7.3): parseExpectedRegions,
// V14 and W4. The Python-generated parity corpus pins every rule's text;
// these cases pin the shapes and the composition.
// --------------------------------------------------------------------------

const LAYOUT_CHECK: NodeTypeDescriptor = {
  typeId: 'layout_check',
  category: CATEGORY_POST_PROCESSING,
  displayName: 'Layout Check',
  inputs: [{ name: 'in', portType: PORT_TYPE_INFERENCE_META }],
  outputs: [{ name: 'out', portType: PORT_TYPE_INFERENCE_META }],
  parameters: [
    { name: 'expected_regions', paramType: 'string', required: true, default: null, constraints: { minLength: 2 } },
    { name: 'min_iou', paramType: 'float', required: false, default: 0.7, constraints: { min: 0.01, max: 1.0 } },
    { name: 'min_confidence', paramType: 'float', required: false, default: 0.5, constraints: { min: 0, max: 1 } },
    { name: 'min_outline_overlap', paramType: 'float', required: false, default: 0.1, constraints: { min: 0, max: 0.5 } },
  ],
  mappings: [],
  hardwareDependent: false,
};

const LAYOUT_CATALOG = [...CATALOG, LAYOUT_CHECK];
const TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]];
const VALID_REGIONS = JSON.stringify({ roof: TRIANGLE });

function layoutGraph(parameters: WorkflowNode['parameters'], upstream: string[] = ['model_inference']) {
  const nodes = [node('src', 'camera_source', { device: '/dev/video0' })];
  upstream.forEach((type, index) => nodes.push(node(`up${index}`, type)));
  nodes.push(node('layout', 'layout_check', parameters));
  const connections = nodes
    .slice(1)
    .map((target, index) => conn(`c${index}`, nodes[index].id, target.id));
  return { nodes, connections };
}

describe('parseExpectedRegions', () => {
  it('reads a ring that repeats its first vertex last as closed', () => {
    const [regions, problems] = parseExpectedRegions(
      JSON.stringify({ roof: [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]] })
    );
    expect(problems).toEqual([]);
    expect(regions?.get('roof')).toEqual([
      [0.2, 0.2],
      [0.8, 0.2],
      [0.8, 0.8],
      [0.2, 0.8],
    ]);
  });

  it('counts the vertices that remain after the cyclic drop', () => {
    expect(parseExpectedRegions('{"roof": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9], [0.1, 0.1]]}')[0]?.get('roof')).toHaveLength(3);
    expect(parseExpectedRegions('{"roof": [[0, 0], [1, 0], [0, 0]]}')).toEqual([
      null,
      ["Region 'roof': the polygon must have between 3 and 64 distinct points; this one has 2."],
    ]);
  });

  it('strips labels and accepts an already-parsed object', () => {
    const [regions, problems] = parseExpectedRegions({ ' roof ': TRIANGLE });
    expect(problems).toEqual([]);
    expect([...(regions?.keys() ?? [])]).toEqual(['roof']);
  });

  it('reports every rule of every region, in key order', () => {
    expect(
      parseExpectedRegions('{"roof": [[0, 0], [2, 0], [0.5]], "": [[0, 0], [1, 0], [0, 1]], "wheel": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]]}')
    ).toEqual([
      null,
      [
        "Region 'roof': point 2 x coordinate must be between 0 and 1.",
        "Region 'roof': point 3 must be a pair of numbers [x, y].",
        'Region 2: the class label is empty.',
        "Region 'wheel': the polygon must cover at least 0.5% of the frame.",
        "Region 'wheel': the polygon crosses itself; no two of its edges may intersect.",
      ],
    ]);
  });

  it('reads NaN and Infinity the way Python json.loads does', () => {
    expect(parseExpectedRegions('{"roof": [[0, 0], [1, 0], [NaN, 1]]}')[1]).toEqual([
      "Region 'roof': point 3 must be a pair of numbers [x, y].",
    ]);
    expect(parseExpectedRegions('not json')[1]).toEqual(['Expected regions are not valid JSON.']);
  });

  it('keeps a __proto__ label as a label', () => {
    const [regions, problems] = parseExpectedRegions(`{"__proto__": ${JSON.stringify(TRIANGLE)}}`);
    expect(problems).toEqual([]);
    expect([...(regions?.keys() ?? [])]).toEqual(['__proto__']);
  });
});

describe('checkV14LayoutConfig', () => {
  it('returns no findings for a valid configuration and the defaults', () => {
    expect(checkV14LayoutConfig(layoutGraph({ expected_regions: VALID_REGIONS }), LAYOUT_CATALOG)).toEqual([]);
  });

  it('leaves a blank or missing expected_regions to V4', () => {
    const blanks: WorkflowNode['parameters'][] = [
      {},
      { expected_regions: '' },
      { expected_regions: '   ' },
      { expected_regions: null },
    ];
    for (const parameters of blanks) {
      expect(checkV14LayoutConfig(layoutGraph(parameters), LAYOUT_CATALOG)).toEqual([]);
    }
    const codes = runInlineChecks(layoutGraph({}), LAYOUT_CATALOG).map((f) => f.code);
    expect(codes).toContain(CODE_V4_MISSING_REQUIRED_PARAMETER);
  });

  it('reports one finding per offending parameter', () => {
    const findings = checkV14LayoutConfig(
      layoutGraph({ expected_regions: '{}', min_iou: 1.5, min_confidence: 0.5, min_outline_overlap: '0.51' }),
      LAYOUT_CATALOG
    );
    expect(findings).toEqual([
      {
        severity: SEVERITY_ERROR,
        code: CODE_V14_LAYOUT_CONFIG_INVALID,
        message:
          "Node 'layout': parameter 'expected_regions': Expected regions must name between 1 and 10 classes; this value names 0.",
        nodeId: 'layout',
        connectionId: null,
      },
      {
        severity: SEVERITY_ERROR,
        code: CODE_V14_LAYOUT_CONFIG_INVALID,
        message: "Node 'layout': parameter 'min_iou': The value must be between 0.01 and 1.",
        nodeId: 'layout',
        connectionId: null,
      },
      {
        severity: SEVERITY_ERROR,
        code: CODE_V14_LAYOUT_CONFIG_INVALID,
        message: "Node 'layout': parameter 'min_outline_overlap': The value must be between 0 and 0.5.",
        nodeId: 'layout',
        connectionId: null,
      },
    ]);
  });

  it('accepts the bounds of min_outline_overlap and refuses just outside them', () => {
    const findingsFor = (value: number) =>
      checkV14LayoutConfig(layoutGraph({ expected_regions: VALID_REGIONS, min_outline_overlap: value }), LAYOUT_CATALOG);
    expect(findingsFor(0)).toEqual([]);
    expect(findingsFor(0.5)).toEqual([]);
    expect(findingsFor(-0.1)).toHaveLength(1);
    expect(findingsFor(0.51)).toHaveLength(1);
  });

  it('fires only on layout_check nodes', () => {
    const graph = { nodes: [node('x', 'custom_python', { expected_regions: 'not json', min_iou: 7 })], connections: [] };
    expect(checkV14LayoutConfig(graph, LAYOUT_CATALOG)).toEqual([]);
  });
});

describe('checkW4LayoutNoDetector', () => {
  const W4_MESSAGE =
    "Node 'layout': no 'model_inference' node is upstream of this 'layout_check' node, " +
    'so no instance masks will exist for it to check at runtime';

  it('warns when no model_inference node is upstream', () => {
    expect(checkW4LayoutNoDetector(layoutGraph({ expected_regions: VALID_REGIONS }, []))).toEqual([
      { severity: SEVERITY_WARNING, code: CODE_W4_LAYOUT_NO_DETECTOR, message: W4_MESSAGE, nodeId: 'layout', connectionId: null },
    ]);
  });

  it('is silent with a detector directly or transitively upstream', () => {
    expect(checkW4LayoutNoDetector(layoutGraph({}, ['model_inference']))).toEqual([]);
    expect(checkW4LayoutNoDetector(layoutGraph({}, ['model_inference', 'detection_counter']))).toEqual([]);
  });

  it('is composed into runInlineChecks beside V14', () => {
    const codes = runInlineChecks(layoutGraph({ expected_regions: 'not json' }, []), LAYOUT_CATALOG).map((f) => f.code);
    expect(codes).toContain(CODE_V14_LAYOUT_CONFIG_INVALID);
    expect(codes).toContain(CODE_W4_LAYOUT_NO_DETECTOR);
  });
});
