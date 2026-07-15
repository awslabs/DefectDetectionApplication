/**
 * **Feature: workflow-manager, Property 10: Connection acceptance equals port compatibility**
 *
 * For all pairs of ports drawn from catalog node types, attempting to create a
 * connection succeeds if and only if the source is an output port, the target
 * is an input port, and their declared types are compatible; every rejection
 * carries a non-empty reason.
 *
 * **Validates: Requirements 1.3, 1.4**
 *
 * The canvas acceptance function is `connectionRejectionReason` (accepted when
 * it returns null). The oracle resolves the dragged source output port type and
 * target input port type (honoring custom_python per-instance port type
 * overrides via `resolvedPorts`) and compares acceptance against
 * `arePortsCompatible`.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { arePortsCompatible } from './compatibility';
import {
  connectionRejectionReason,
  toWorkflowNode,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
} from './builderGraph';
import { resolvedPorts } from './inlineChecks';
import {
  CATEGORIES,
  PORT_TYPES,
  PORT_TYPE_VIDEO_FRAMES,
  type JsonValue,
  type NodeTypeDescriptor,
  type PortType,
} from './types';

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** A node instance to place on the canvas: catalog descriptor + parameters. */
interface NodeInstance {
  descriptor: NodeTypeDescriptor;
  parameters: Record<string, JsonValue>;
}

const portTypeArb: fc.Arbitrary<PortType> = fc.constantFrom(...PORT_TYPES);

/** 0..3 ports named `{prefix}`, `{prefix}2`, ... with random port types. */
function portListArb(prefix: string) {
  return fc
    .array(portTypeArb, { minLength: 0, maxLength: 3 })
    .map((types) =>
      types.map((portType, i) => ({ name: i === 0 ? prefix : `${prefix}${i + 1}`, portType }))
    );
}

/** A plain catalog node type with randomly typed input and output ports. */
const plainInstanceArb: fc.Arbitrary<NodeInstance> = fc
  .record({
    category: fc.constantFrom(...CATEGORIES),
    inputs: portListArb('in'),
    outputs: portListArb('out'),
  })
  .map(({ category, inputs, outputs }) => ({
    descriptor: {
      typeId: 'random_node',
      category,
      displayName: 'Random node',
      inputs,
      outputs,
      parameters: [],
      mappings: [],
      hardwareDependent: false,
    },
    parameters: {},
  }));

/**
 * A custom_python node: declared VideoFrames ports whose effective types are
 * overridden per instance through the `input_port_type` / `output_port_type`
 * parameters (Requirement 2.7). Overrides are optionally omitted so the
 * declared defaults are also exercised.
 */
const customPythonInstanceArb: fc.Arbitrary<NodeInstance> = fc
  .record({
    inputOverride: fc.option(portTypeArb, { nil: undefined }),
    outputOverride: fc.option(portTypeArb, { nil: undefined }),
  })
  .map(({ inputOverride, outputOverride }) => ({
    descriptor: {
      typeId: 'custom_python',
      category: 'post_processing',
      displayName: 'Custom Python',
      inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
      outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
      parameters: [
        { name: 'code', paramType: 'code', required: true },
        { name: 'input_port_type', paramType: 'enum', required: true },
        { name: 'output_port_type', paramType: 'enum', required: true },
      ],
      mappings: [],
      hardwareDependent: false,
    },
    parameters: {
      ...(inputOverride !== undefined ? { input_port_type: inputOverride } : {}),
      ...(outputOverride !== undefined ? { output_port_type: outputOverride } : {}),
    },
  }));

const instanceArb: fc.Arbitrary<NodeInstance> = fc.oneof(
  plainInstanceArb,
  customPythonInstanceArb
);

/** Every port name declared on a node type (inputs and outputs). */
function portNames(descriptor: NodeTypeDescriptor): string[] {
  return [...descriptor.inputs, ...descriptor.outputs].map((port) => port.name);
}

/**
 * A dragged handle: mostly one of the node's declared port names (input or
 * output, so drags from the wrong side of a node are covered), occasionally a
 * handle that does not exist on the node at all.
 */
function handleArb(names: string[]): fc.Arbitrary<string> {
  const bogus = fc.constantFrom('bogus', '', 'in', 'out');
  if (names.length === 0) {
    return bogus;
  }
  return fc.oneof(
    { weight: 4, arbitrary: fc.constantFrom(...names) },
    { weight: 1, arbitrary: bogus }
  );
}

/** Two distinct canvas nodes plus a dragged source and target handle. */
const scenarioArb = fc.tuple(instanceArb, instanceArb).chain(([source, target]) =>
  fc.record({
    source: fc.constant(source),
    target: fc.constant(target),
    sourceHandle: handleArb(portNames(source.descriptor)),
    targetHandle: handleArb(portNames(target.descriptor)),
  })
);

function builderNode(id: string, instance: NodeInstance): BuilderNode {
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: {
      descriptor: instance.descriptor,
      parameters: instance.parameters,
      validationMessages: [],
    },
  };
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 10: Connection acceptance equals port compatibility', () => {
  it('accepts a dragged connection iff source output and target input types are compatible, with a non-empty reason on every rejection', () => {
    fc.assert(
      fc.property(scenarioArb, ({ source, target, sourceHandle, targetHandle }) => {
        const sourceNode = builderNode('src', source);
        const targetNode = builderNode('tgt', target);
        const nodes = [sourceNode, targetNode];

        const reason = connectionRejectionReason(
          { source: 'src', sourceHandle, target: 'tgt', targetHandle },
          nodes
        );

        // Oracle: the dragged source handle must be an output port, the
        // dragged target handle must be an input port (per-instance
        // overrides applied), and their types must be compatible.
        const sourceType = resolvedPorts(toWorkflowNode(sourceNode), source.descriptor).outputs[
          sourceHandle
        ];
        const targetType = resolvedPorts(toWorkflowNode(targetNode), target.descriptor).inputs[
          targetHandle
        ];
        const expectedAccepted =
          sourceType !== undefined &&
          targetType !== undefined &&
          arePortsCompatible(sourceType, targetType);

        if (expectedAccepted) {
          expect(reason).toBeNull();
        } else {
          expect(typeof reason).toBe('string');
          expect((reason as string).length).toBeGreaterThan(0);
        }
      }),
      { numRuns: 25 }
    );
  });
});
