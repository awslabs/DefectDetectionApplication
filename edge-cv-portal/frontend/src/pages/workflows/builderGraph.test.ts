/**
 * Unit tests for the Workflow_Builder canvas graph helpers
 * (Requirements 1.2, 1.3, 1.4, 1.5).
 */

import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';
import {
  connectionRejectionReason,
  createBuilderNode,
  defaultParameters,
  edgeIdFor,
  isSameConnection,
  nextNodeId,
  removeNodesAndAttachedEdges,
  toWorkflowConnection,
  toWorkflowNode,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
} from './builderGraph';
import type { NodeTypeDescriptor, ParameterDescriptor, PortDescriptor } from './types';

// --------------------------------------------------------------------------
// Test descriptors
// --------------------------------------------------------------------------

function descriptor(overrides: Partial<NodeTypeDescriptor>): NodeTypeDescriptor {
  return {
    typeId: 'test_type',
    category: 'input',
    displayName: 'Test Type',
    inputs: [],
    outputs: [],
    parameters: [],
    mappings: [],
    hardwareDependent: false,
    ...overrides,
  };
}

function port(name: string, portType: string): PortDescriptor {
  return { name, portType };
}

function parameter(overrides: Partial<ParameterDescriptor>): ParameterDescriptor {
  return { name: 'p', paramType: 'string', required: false, ...overrides };
}

const CAMERA = descriptor({
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  outputs: [port('out', 'VideoFrames')],
  parameters: [
    parameter({ name: 'device', paramType: 'string', required: true, default: '/dev/video0' }),
    parameter({ name: 'fps', paramType: 'int', required: false }),
  ],
});

const INFERENCE = descriptor({
  typeId: 'model_inference',
  category: 'inference',
  displayName: 'Model inference',
  inputs: [port('in', 'VideoFrames')],
  outputs: [port('out', 'InferenceMeta')],
  parameters: [parameter({ name: 'modelName', paramType: 'model_ref', required: true })],
});

const DIGITAL_OUTPUT = descriptor({
  typeId: 'digital_output',
  category: 'output',
  displayName: 'Digital output',
  inputs: [port('in', 'InferenceMeta')],
});

const CAPTURE = descriptor({
  typeId: 'capture',
  category: 'output',
  displayName: 'Capture',
  inputs: [port('in', 'VideoFrames')],
});

function nodeOf(d: NodeTypeDescriptor, id: string): BuilderNode {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor: d, parameters: defaultParameters(d), validationMessages: [] },
  };
}

// --------------------------------------------------------------------------
// Node creation with default configuration (Requirement 1.2)
// --------------------------------------------------------------------------

describe('defaultParameters', () => {
  it('includes only parameters with declared non-null defaults', () => {
    expect(defaultParameters(CAMERA)).toEqual({ device: '/dev/video0' });
    expect(defaultParameters(INFERENCE)).toEqual({});
  });
});

describe('nextNodeId', () => {
  it('starts at 1 and skips taken ids', () => {
    expect(nextNodeId('camera_source', [])).toBe('camera_source_1');
    expect(nextNodeId('camera_source', ['camera_source_1', 'camera_source_2'])).toBe(
      'camera_source_3'
    );
    expect(nextNodeId('camera_source', ['camera_source_2'])).toBe('camera_source_1');
  });
});

describe('createBuilderNode', () => {
  it('creates a workflow node at the drop position with default configuration', () => {
    const node = createBuilderNode(CAMERA, { x: 10, y: 20 }, ['camera_source_1']);
    expect(node.id).toBe('camera_source_2');
    expect(node.type).toBe(WORKFLOW_NODE_TYPE);
    expect(node.position).toEqual({ x: 10, y: 20 });
    expect(node.data.descriptor).toBe(CAMERA);
    expect(node.data.parameters).toEqual({ device: '/dev/video0' });
    expect(node.data.validationMessages).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Workflow_Definition translation
// --------------------------------------------------------------------------

describe('toWorkflowNode / toWorkflowConnection', () => {
  it('maps a canvas node to its Workflow_Definition node', () => {
    const node = createBuilderNode(CAMERA, { x: 3, y: 4 }, []);
    expect(toWorkflowNode(node)).toEqual({
      id: 'camera_source_1',
      type: 'camera_source',
      position: { x: 3, y: 4 },
      parameters: { device: '/dev/video0' },
    });
  });

  it('maps a canvas edge to its Workflow_Definition connection', () => {
    const edge: Edge = {
      id: 'e1',
      source: 'a',
      sourceHandle: 'out',
      target: 'b',
      targetHandle: 'in',
    };
    expect(toWorkflowConnection(edge)).toEqual({
      id: 'e1',
      from: { node: 'a', port: 'out' },
      to: { node: 'b', port: 'in' },
    });
  });
});

// --------------------------------------------------------------------------
// Connection rules (Requirements 1.3, 1.4)
// --------------------------------------------------------------------------

describe('connectionRejectionReason', () => {
  const camera = nodeOf(CAMERA, 'cam');
  const inference = nodeOf(INFERENCE, 'inf');
  const digitalOutput = nodeOf(DIGITAL_OUTPUT, 'dio');
  const capture = nodeOf(CAPTURE, 'cap');
  const nodes = [camera, inference, digitalOutput, capture];

  it('accepts an exact port type match', () => {
    expect(
      connectionRejectionReason(
        { source: 'cam', sourceHandle: 'out', target: 'inf', targetHandle: 'in' },
        nodes
      )
    ).toBeNull();
  });

  it('accepts the declared InferenceMeta -> VideoFrames coercion', () => {
    expect(
      connectionRejectionReason(
        { source: 'inf', sourceHandle: 'out', target: 'cap', targetHandle: 'in' },
        nodes
      )
    ).toBeNull();
  });

  it('rejects incompatible port types with the reason', () => {
    expect(
      connectionRejectionReason(
        { source: 'cam', sourceHandle: 'out', target: 'dio', targetHandle: 'in' },
        nodes
      )
    ).toBe('Cannot connect VideoFrames output to InferenceMeta input');
  });

  it('rejects self connections', () => {
    expect(
      connectionRejectionReason(
        { source: 'inf', sourceHandle: 'out', target: 'inf', targetHandle: 'in' },
        nodes
      )
    ).toBe('Cannot connect a node to itself');
  });

  it('accepts connections from both conditional output branches', () => {
    // The conditional node's two output ports ("true"/"false") both
    // carry InferenceMeta, so each branch wires to InferenceMeta inputs
    // — e.g. one andon light per branch.
    const conditional = nodeOf(
      descriptor({
        typeId: 'conditional',
        category: 'post_processing',
        displayName: 'Conditional',
        inputs: [port('in', 'InferenceMeta')],
        outputs: [port('true', 'InferenceMeta'), port('false', 'InferenceMeta')],
        parameters: [parameter({ name: 'condition', paramType: 'string', required: true })],
      }),
      'cond'
    );
    const greenLight = nodeOf(DIGITAL_OUTPUT, 'green');
    const redLight = nodeOf(DIGITAL_OUTPUT, 'red');
    const all = [...nodes, conditional, greenLight, redLight];

    expect(
      connectionRejectionReason(
        { source: 'inf', sourceHandle: 'out', target: 'cond', targetHandle: 'in' },
        all
      )
    ).toBeNull();
    expect(
      connectionRejectionReason(
        { source: 'cond', sourceHandle: 'true', target: 'red', targetHandle: 'in' },
        all
      )
    ).toBeNull();
    expect(
      connectionRejectionReason(
        { source: 'cond', sourceHandle: 'false', target: 'green', targetHandle: 'in' },
        all
      )
    ).toBeNull();
  });

  it('rejects unknown nodes and ports', () => {
    expect(
      connectionRejectionReason(
        { source: 'ghost', sourceHandle: 'out', target: 'inf', targetHandle: 'in' },
        nodes
      )
    ).toBe('Connection endpoints must be nodes on the canvas');
    expect(
      connectionRejectionReason(
        { source: 'cam', sourceHandle: 'nope', target: 'inf', targetHandle: 'in' },
        nodes
      )
    ).toBe("Node 'cam' has no output port 'nope'");
    expect(
      connectionRejectionReason(
        { source: 'cam', sourceHandle: 'out', target: 'inf', targetHandle: 'nope' },
        nodes
      )
    ).toBe("Node 'inf' has no input port 'nope'");
  });
});

describe('edgeIdFor / isSameConnection', () => {
  it('produces a deterministic id from the endpoints', () => {
    const connection = { source: 'a', sourceHandle: 'out', target: 'b', targetHandle: 'in' };
    expect(edgeIdFor(connection)).toBe('a.out->b.in');
  });

  it('matches edges joining the same ports', () => {
    const connection = { source: 'a', sourceHandle: 'out', target: 'b', targetHandle: 'in' };
    const edge: Edge = { id: 'x', source: 'a', sourceHandle: 'out', target: 'b', targetHandle: 'in' };
    expect(isSameConnection(edge, connection)).toBe(true);
    expect(isSameConnection({ ...edge, target: 'c' }, connection)).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Delete behavior (Requirement 1.5)
// --------------------------------------------------------------------------

describe('removeNodesAndAttachedEdges', () => {
  it('removes the nodes and every connection attached to them', () => {
    const camera = nodeOf(CAMERA, 'cam');
    const inference = nodeOf(INFERENCE, 'inf');
    const digitalOutput = nodeOf(DIGITAL_OUTPUT, 'dio');
    const edges: Edge[] = [
      { id: 'e1', source: 'cam', sourceHandle: 'out', target: 'inf', targetHandle: 'in' },
      { id: 'e2', source: 'inf', sourceHandle: 'out', target: 'dio', targetHandle: 'in' },
    ];

    const result = removeNodesAndAttachedEdges([camera, inference, digitalOutput], edges, ['inf']);
    expect(result.nodes.map((node) => node.id)).toEqual(['cam', 'dio']);
    expect(result.edges).toEqual([]);
  });

  it('keeps connections not attached to a removed node', () => {
    const camera = nodeOf(CAMERA, 'cam');
    const inference = nodeOf(INFERENCE, 'inf');
    const digitalOutput = nodeOf(DIGITAL_OUTPUT, 'dio');
    const edges: Edge[] = [
      { id: 'e1', source: 'cam', sourceHandle: 'out', target: 'inf', targetHandle: 'in' },
      { id: 'e2', source: 'inf', sourceHandle: 'out', target: 'dio', targetHandle: 'in' },
    ];

    const result = removeNodesAndAttachedEdges([camera, inference, digitalOutput], edges, ['cam']);
    expect(result.nodes.map((node) => node.id)).toEqual(['inf', 'dio']);
    expect(result.edges.map((edge) => edge.id)).toEqual(['e2']);
  });
});
