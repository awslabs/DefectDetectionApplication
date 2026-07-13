/**
 * Unit tests for the inline validation markers (Requirements 1.9, 1.10):
 * markers appear on nodes with missing required parameters (V4) and on
 * nodes unreachable from any input node (V5), clear when the condition
 * is resolved, and the marker application preserves node identity so
 * the canvas can re-run it on every mutation without update loops.
 */

import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';
import { WORKFLOW_NODE_TYPE, type BuilderNode } from './builderGraph';
import { applyValidationMarkers, validationMessagesByNode } from './validationMarkers';
import type { JsonValue, NodeTypeDescriptor } from './types';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const CAMERA: NodeTypeDescriptor = {
  typeId: 'camera_source',
  category: 'input',
  displayName: 'Camera source',
  inputs: [],
  outputs: [{ name: 'out', portType: 'VideoFrames' }],
  parameters: [
    { name: 'device', paramType: 'string', required: true, default: null, constraints: {} },
  ],
  mappings: [],
  hardwareDependent: true,
};

const INFERENCE: NodeTypeDescriptor = {
  typeId: 'model_inference',
  category: 'inference',
  displayName: 'Model inference',
  inputs: [{ name: 'in', portType: 'VideoFrames' }],
  outputs: [{ name: 'out', portType: 'InferenceMeta' }],
  parameters: [
    { name: 'modelName', paramType: 'model_ref', required: true, default: null, constraints: {} },
  ],
  mappings: [],
  hardwareDependent: false,
};

const CATALOG = [CAMERA, INFERENCE];

function builderNode(
  descriptor: NodeTypeDescriptor,
  id: string,
  parameters: Record<string, JsonValue> = {},
  validationMessages: string[] = []
): BuilderNode {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: { descriptor, parameters, validationMessages },
  };
}

function edge(source: string, sourceHandle: string, target: string, targetHandle: string): Edge {
  return {
    id: `${source}.${sourceHandle}->${target}.${targetHandle}`,
    source,
    sourceHandle,
    target,
    targetHandle,
  };
}

// --------------------------------------------------------------------------
// validationMessagesByNode
// --------------------------------------------------------------------------

describe('validationMessagesByNode', () => {
  it('reports a missing required parameter on the offending node (Requirement 1.9)', () => {
    const nodes = [builderNode(CAMERA, 'cam', {})];
    const messages = validationMessagesByNode(nodes, [], CATALOG);
    expect(messages.get('cam')).toEqual(["Node 'cam': Required parameter 'device' has no value"]);
  });

  it('reports an unreachable node (Requirement 1.9)', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }),
      builderNode(INFERENCE, 'inf', { modelName: 'm1' }),
    ];
    const messages = validationMessagesByNode(nodes, [], CATALOG);
    expect(messages.has('cam')).toBe(false);
    expect(messages.get('inf')).toEqual(["Node 'inf' is not reachable from any input node"]);
  });

  it('collects every finding targeting the same node', () => {
    // 'inf' both misses its required parameter and is unreachable.
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }),
      builderNode(INFERENCE, 'inf', {}),
    ];
    const messages = validationMessagesByNode(nodes, [], CATALOG);
    expect(messages.get('inf')).toEqual([
      "Node 'inf': Required parameter 'modelName' has no value",
      "Node 'inf' is not reachable from any input node",
    ]);
  });

  it('returns an empty map for a graph with no findings', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }),
      builderNode(INFERENCE, 'inf', { modelName: 'm1' }),
    ];
    const edges = [edge('cam', 'out', 'inf', 'in')];
    expect(validationMessagesByNode(nodes, edges, CATALOG).size).toBe(0);
  });
});

// --------------------------------------------------------------------------
// applyValidationMarkers: markers appear and clear (Requirements 1.9, 1.10)
// --------------------------------------------------------------------------

describe('applyValidationMarkers', () => {
  it('adds warning messages to offending nodes (Requirement 1.9)', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', {}),
      builderNode(INFERENCE, 'inf', { modelName: 'm1' }),
    ];
    const marked = applyValidationMarkers(nodes, [], CATALOG);
    expect(marked[0].data.validationMessages).toEqual([
      "Node 'cam': Required parameter 'device' has no value",
    ]);
    expect(marked[1].data.validationMessages).toEqual([
      "Node 'inf' is not reachable from any input node",
    ]);
  });

  it('clears the marker when a parameter edit resolves it (Requirement 1.10)', () => {
    // Marker present from a previous run; the parameter is now set.
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }, [
        "Node 'cam': Required parameter 'device' has no value",
      ]),
    ];
    const marked = applyValidationMarkers(nodes, [], CATALOG);
    expect(marked[0].data.validationMessages).toEqual([]);
  });

  it('clears the unreachable marker when a connection makes the node reachable (Requirement 1.10)', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }),
      builderNode(INFERENCE, 'inf', { modelName: 'm1' }, [
        "Node 'inf' is not reachable from any input node",
      ]),
    ];
    const marked = applyValidationMarkers(nodes, [edge('cam', 'out', 'inf', 'in')], CATALOG);
    expect(marked[1].data.validationMessages).toEqual([]);
  });

  it('re-adds the unreachable marker when the connection is removed', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', { device: '/dev/video0' }),
      builderNode(INFERENCE, 'inf', { modelName: 'm1' }),
    ];
    const connected = applyValidationMarkers(nodes, [edge('cam', 'out', 'inf', 'in')], CATALOG);
    expect(connected[1].data.validationMessages).toEqual([]);

    const disconnected = applyValidationMarkers(connected, [], CATALOG);
    expect(disconnected[1].data.validationMessages).toEqual([
      "Node 'inf' is not reachable from any input node",
    ]);
  });

  // Identity preservation is what prevents infinite update loops when the
  // canvas re-applies markers after every mutation.
  it('returns the input array unchanged when no node message changed', () => {
    const nodes = [
      builderNode(CAMERA, 'cam', {}, ["Node 'cam': Required parameter 'device' has no value"]),
    ];
    expect(applyValidationMarkers(nodes, [], CATALOG)).toBe(nodes);
  });

  it('preserves the identity of nodes whose messages did not change', () => {
    const settled = builderNode(CAMERA, 'cam', { device: '/dev/video0' });
    const offending = builderNode(INFERENCE, 'inf', { modelName: 'm1' });
    const marked = applyValidationMarkers([settled, offending], [], CATALOG);
    expect(marked).not.toBe([settled, offending]);
    expect(marked[0]).toBe(settled);
    expect(marked[1]).not.toBe(offending);
  });
});
