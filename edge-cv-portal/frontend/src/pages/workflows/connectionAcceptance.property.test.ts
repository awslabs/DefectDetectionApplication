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
import type { Edge } from '@xyflow/react';
import {
  connectionRejectionReason,
  edgeIdFor,
  isSameConnection,
  toWorkflowNode,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
  type ConnectionEndpoints,
} from './builderGraph';
import { resolvedPorts } from './inlineChecks';
import {
  CATEGORIES,
  PORT_TYPES,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_INFERENCE_META,
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

/**
 * A custom_python_preprocess-shaped node: fixed VideoFrames input and
 * output ports with no per-instance port type override parameters
 * (custom-python-frames Requirement 1.2), so the declared port types are
 * always the effective ones.
 */
const customPythonPreprocessInstance: NodeInstance = {
  descriptor: {
    typeId: 'custom_python_preprocess',
    category: 'preprocessing',
    displayName: 'Custom Python (Frames)',
    inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
    outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
    parameters: [
      { name: 'code', paramType: 'code', required: true },
      { name: 'requirements', paramType: 'string', required: false },
    ],
    mappings: [],
    hardwareDependent: false,
  },
  parameters: {},
};

/** A source node instance guaranteed to declare at least one output port. */
const sourceWithOutputArb: fc.Arbitrary<NodeInstance> = fc.oneof(
  plainInstanceArb.filter((instance) => instance.descriptor.outputs.length > 0),
  customPythonInstanceArb
);

/**
 * A custom_python_source-shaped node (custom-python-source Requirement
 * 1.1): a fixed VideoFrames `out` port and an EventSignal `activation`
 * input, with no per-instance port type override parameters, so the
 * declared port types are always the effective ones.
 */
const customPythonSourceInstance: NodeInstance = {
  descriptor: {
    typeId: 'custom_python_source',
    category: 'input',
    displayName: 'Custom Python (Source)',
    inputs: [{ name: 'activation', portType: PORT_TYPE_EVENT_SIGNAL }],
    outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
    parameters: [
      { name: 'code', paramType: 'code', required: true },
      { name: 'requirements', paramType: 'string', required: false },
      { name: 'allowed_uri_prefixes', paramType: 'string', required: false },
    ],
    mappings: [],
    hardwareDependent: true,
  },
  parameters: {},
};

/** A source node plus one of its declared output ports as the dragged handle. */
const fixedPortScenarioArb = sourceWithOutputArb.chain((source) =>
  fc.record({
    source: fc.constant(source),
    sourceHandle: fc.constantFrom(...source.descriptor.outputs.map((port) => port.name)),
  })
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

/**
 * **Feature: custom-python-frames, Property 11: Designer connection acceptance for fixed VideoFrames ports**
 *
 * For any generated pair of nodes where the target is a
 * `custom_python_preprocess`-shaped descriptor (fixed VideoFrames ports, no
 * port type override parameters), the Workflow_Builder connection acceptance
 * function accepts the connection exactly when the source output port type is
 * compatible with VideoFrames under the declared coercion rules (VideoFrames
 * exactly, or InferenceMeta via the declared coercion — mirroring the backend
 * validator per Requirement 2.1) and otherwise rejects it with a reason.
 *
 * **Validates: Requirements 7.3**
 */
describe('Property 11: Designer connection acceptance for fixed VideoFrames ports', () => {
  it('accepts a drag onto the fixed VideoFrames input iff the source output port type is compatible with VideoFrames under the declared coercion rules, with a non-empty reason otherwise', () => {
    fc.assert(
      fc.property(fixedPortScenarioArb, ({ source, sourceHandle }) => {
        const sourceNode = builderNode('src', source);
        const targetNode = builderNode('tgt', customPythonPreprocessInstance);
        const nodes = [sourceNode, targetNode];

        const reason = connectionRejectionReason(
          { source: 'src', sourceHandle, target: 'tgt', targetHandle: 'in' },
          nodes
        );

        // The effective source output port type (per-instance overrides
        // applied for custom_python sources; declared type otherwise).
        const sourceType = resolvedPorts(toWorkflowNode(sourceNode), source.descriptor).outputs[
          sourceHandle
        ];
        const expectedAccepted =
          sourceType !== undefined &&
          arePortsCompatible(sourceType, PORT_TYPE_VIDEO_FRAMES);

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

/**
 * **Feature: custom-python-source, Property 22: Connection acceptance matches the port compatibility oracle**
 *
 * For any target node descriptor and input port drawn from the catalog,
 * the Workflow_Builder accepts a connection from a
 * Custom_Python_Source_Node's `out` port exactly when
 * `arePortsCompatible(VideoFrames, targetType)` holds under the declared
 * coercion rules, with a displayed (non-empty) reason on rejection.
 *
 * **Validates: Requirements 10.3**
 */

/** A target instance guaranteed to declare at least one input port. */
const targetWithInputArb: fc.Arbitrary<NodeInstance> = fc.oneof(
  plainInstanceArb.filter((instance) => instance.descriptor.inputs.length > 0),
  customPythonInstanceArb,
  fc.constant(customPythonPreprocessInstance)
);

/** A target node plus one of its declared input ports as the drop handle. */
const sourceOutDropScenarioArb = targetWithInputArb.chain((target) =>
  fc.record({
    target: fc.constant(target),
    targetHandle: fc.constantFrom(...target.descriptor.inputs.map((port) => port.name)),
  })
);

describe('Property 22: Connection acceptance matches the port compatibility oracle', () => {
  it("accepts a drag from the custom_python_source 'out' port iff the target input port type is compatible with VideoFrames under the declared coercion rules, with a non-empty reason otherwise", () => {
    fc.assert(
      fc.property(sourceOutDropScenarioArb, ({ target, targetHandle }) => {
        const sourceNode = builderNode('src', customPythonSourceInstance);
        const targetNode = builderNode('tgt', target);
        const nodes = [sourceNode, targetNode];

        const reason = connectionRejectionReason(
          { source: 'src', sourceHandle: 'out', target: 'tgt', targetHandle },
          nodes
        );

        // The effective target input port type (per-instance overrides
        // applied for custom_python targets; declared type otherwise).
        const targetType = resolvedPorts(toWorkflowNode(targetNode), target.descriptor).inputs[
          targetHandle
        ];
        const expectedAccepted =
          targetType !== undefined && arePortsCompatible(PORT_TYPE_VIDEO_FRAMES, targetType);

        if (expectedAccepted) {
          expect(reason).toBeNull();
        } else {
          expect(typeof reason).toBe('string');
          expect((reason as string).length).toBeGreaterThan(0);
        }
      }),
      { numRuns: 100 }
    );
  });
});

/**
 * **Feature: workflow-manager-integration-bugfixes, Property 6: Preservation — connection acceptance unchanged outside every bug condition**
 *
 * Baseline behavior that the model-inference fan-out fix (Bug 3) must NOT
 * change. `connectionRejectionReason` is a pure, stateless decision over the
 * dragged endpoints; it never inspects a source port's existing out-degree, so
 * fan-out is realized entirely by appending edges elsewhere. This block pins
 * the acceptance/rejection outcomes that must remain identical:
 *
 *   - a connection is accepted iff the dragged source output type is compatible
 *     with the dragged target input type (single-downstream and every further
 *     downstream connection alike — the function is out-degree-agnostic);
 *   - a self-connection (source id === target id) is always rejected with a
 *     non-empty reason;
 *   - an unknown source or target handle (not a declared port) is always
 *     rejected with a non-empty reason;
 *   - a model-inference output (`inference` category, `InferenceMeta` out port)
 *     is accepted onto a compatible target and rejected onto an incompatible
 *     one, with the same decision no matter how many downstream edges already
 *     exist — establishing the single-downstream baseline preserved under
 *     fan-out.
 *
 * **Validates: Requirements 3.3, 3.4**
 */

/** A model-inference-shaped source: `inference` category, single `InferenceMeta` out port. */
const modelInferenceSourceArb: fc.Arbitrary<NodeInstance> = fc
  .constantFrom('model_inference', 'bedrock_inference', 'llm_inference')
  .map((typeId) => ({
    descriptor: {
      typeId,
      category: 'inference',
      displayName: 'Model inference',
      inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
      outputs: [{ name: 'out', portType: PORT_TYPE_INFERENCE_META }],
      parameters: [],
      mappings: [],
      hardwareDependent: false,
    },
    parameters: {},
  }));

/** A downstream target declaring a single input port of a chosen type. */
const downstreamTargetArb: fc.Arbitrary<NodeInstance> = fc
  .constantFrom(...PORT_TYPES)
  .map((portType) => ({
    descriptor: {
      typeId: 'downstream_node',
      category: 'output',
      displayName: 'Downstream node',
      inputs: [{ name: 'in', portType }],
      outputs: [],
      parameters: [],
      mappings: [],
      hardwareDependent: false,
    },
    parameters: {},
  }));

describe('Property 6 (preservation): connection acceptance unchanged outside every bug condition', () => {
  it('rejects a self-connection (source id === target id) with a non-empty reason', () => {
    fc.assert(
      fc.property(
        instanceArb,
        fc.string(),
        fc.string(),
        (instance, sourceHandle, targetHandle) => {
          const node = builderNode('same', instance);

          const reason = connectionRejectionReason(
            { source: 'same', sourceHandle, target: 'same', targetHandle },
            [node]
          );

          expect(typeof reason).toBe('string');
          expect((reason as string).length).toBeGreaterThan(0);
        }
      ),
      { numRuns: 25 }
    );
  });

  it('rejects an unknown source or target handle with a non-empty reason', () => {
    // Force at least one endpoint handle to name a port the node does not
    // declare, so the connection can never be accepted regardless of types.
    const unknownHandleScenarioArb = fc
      .tuple(instanceArb, instanceArb)
      .chain(([source, target]) =>
        fc.record({
          source: fc.constant(source),
          target: fc.constant(target),
          sourceHandle: fc.constant('__missing_source_handle__'),
          targetHandle: handleArb(portNames(target.descriptor)),
        })
      );

    fc.assert(
      fc.property(
        unknownHandleScenarioArb,
        ({ source, target, sourceHandle, targetHandle }) => {
          const sourceNode = builderNode('src', source);
          const targetNode = builderNode('tgt', target);

          const reason = connectionRejectionReason(
            { source: 'src', sourceHandle, target: 'tgt', targetHandle },
            [sourceNode, targetNode]
          );

          expect(typeof reason).toBe('string');
          expect((reason as string).length).toBeGreaterThan(0);
        }
      ),
      { numRuns: 25 }
    );
  });

  it('accepts a model-inference output onto a compatible target and rejects an incompatible one, independent of existing out-degree (single-downstream baseline preserved)', () => {
    fc.assert(
      fc.property(
        modelInferenceSourceArb,
        downstreamTargetArb,
        // Extra already-present edges from the same source output must not
        // change the decision: the function is stateless over out-degree.
        fc.array(fc.string(), { minLength: 0, maxLength: 3 }),
        (source, target, _priorTargetIds) => {
          const sourceNode = builderNode('inf', source);
          const targetNode = builderNode('down', target);

          const reason = connectionRejectionReason(
            { source: 'inf', sourceHandle: 'out', target: 'down', targetHandle: 'in' },
            [sourceNode, targetNode]
          );

          const targetType = target.descriptor.inputs[0].portType;
          const expectedAccepted = arePortsCompatible(PORT_TYPE_INFERENCE_META, targetType);

          if (expectedAccepted) {
            expect(reason).toBeNull();
          } else {
            expect(typeof reason).toBe('string');
            expect((reason as string).length).toBeGreaterThan(0);
          }
        }
      ),
      { numRuns: 25 }
    );
  });

  // The point of Bug 3 (fan-out) is that it requires NO change to the pure
  // connection decision: `connectionRejectionReason` is stateless over a
  // source port's out-degree, and `edgeIdFor`/`isSameConnection` key on the
  // full source+sourceHandle+target+targetHandle tuple, so appending a further
  // outgoing edge from an already-connected model-inference output never
  // collides with, dedups against, or otherwise disturbs the existing edges.
  // These properties lock in that adding fan-out coverage leaves every
  // accept/reject outcome exactly as it is on the unfixed code.

  /** A model-inference source fanning out to a list of distinct downstream targets. */
  const fanOutScenarioArb = fc
    .tuple(
      modelInferenceSourceArb,
      fc.array(downstreamTargetArb, { minLength: 2, maxLength: 5 })
    )
    .map(([source, targets]) => ({ source, targets }));

  it('appends each further outgoing edge from a model-inference output under a distinct id, so fan-out edges coexist without dedup collisions', () => {
    fc.assert(
      fc.property(fanOutScenarioArb, ({ source, targets }) => {
        const sourceNode = builderNode('inf', source);
        const targetNodes = targets.map((target, i) => builderNode(`down_${i}`, target));
        const nodes = [sourceNode, ...targetNodes];

        // Simulate the canvas `onConnect` append semantics: for every target
        // whose connection is accepted, append an edge keyed by `edgeIdFor`
        // unless an identical-tuple edge already exists (`isSameConnection`).
        let edges: Edge[] = [];
        const acceptedTargets: string[] = [];
        targetNodes.forEach((targetNode) => {
          const connection: ConnectionEndpoints = {
            source: 'inf',
            sourceHandle: 'out',
            target: targetNode.id,
            targetHandle: 'in',
          };
          const reason = connectionRejectionReason(connection, nodes);
          if (reason !== null) {
            return;
          }
          acceptedTargets.push(targetNode.id);
          if (!edges.some((edge) => isSameConnection(edge, connection))) {
            edges = [
              ...edges,
              {
                id: edgeIdFor(connection),
                source: connection.source,
                sourceHandle: connection.sourceHandle ?? undefined,
                target: connection.target,
                targetHandle: connection.targetHandle ?? undefined,
              },
            ];
          }
        });

        // Every accepted fan-out target produced its own edge (no dedup
        // collision), and all edge ids are distinct — the single output port
        // now fans out to every accepted downstream node simultaneously.
        expect(edges.length).toBe(acceptedTargets.length);
        expect(new Set(edges.map((edge) => edge.id)).size).toBe(edges.length);
        edges.forEach((edge) => {
          expect(edge.source).toBe('inf');
          expect(edge.sourceHandle).toBe('out');
        });
      }),
      { numRuns: 25 }
    );
  });

  it('decides a further outgoing connection identically no matter how many downstream edges already exist (out-degree-agnostic)', () => {
    fc.assert(
      fc.property(fanOutScenarioArb, ({ source, targets }) => {
        const sourceNode = builderNode('inf', source);
        const targetNodes = targets.map((target, i) => builderNode(`down_${i}`, target));
        const nodes = [sourceNode, ...targetNodes];

        targetNodes.forEach((targetNode) => {
          const connection: ConnectionEndpoints = {
            source: 'inf',
            sourceHandle: 'out',
            target: targetNode.id,
            targetHandle: 'in',
          };

          // Decision computed in isolation (source + this target only, the
          // single-downstream baseline)...
          const baselineReason = connectionRejectionReason(connection, [
            sourceNode,
            targetNode,
          ]);
          // ...must equal the decision computed with every other downstream
          // node (and thus every prior fan-out edge) present.
          const fanOutReason = connectionRejectionReason(connection, nodes);

          expect(fanOutReason).toBe(baselineReason);
        });
      }),
      { numRuns: 25 }
    );
  });
});
