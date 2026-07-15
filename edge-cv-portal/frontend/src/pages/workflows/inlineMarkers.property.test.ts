/**
 * **Feature: workflow-manager, Property 12: Inline markers are exactly the offending nodes**
 *
 * For all workflow graphs (including graphs reached by any sequence of canvas
 * mutations), the set of nodes carrying inline validation markers equals
 * exactly the set of nodes with missing required parameter values plus the set
 * of nodes unreachable from any input node — so resolving a condition removes
 * its marker.
 *
 * **Validates: Requirements 1.9, 1.10**
 *
 * The marker function under test is `applyValidationMarkers` (markers are the
 * nodes with non-empty `data.validationMessages`). The oracle independently
 * computes the offending node set: nodes with a parameter whose effective
 * value violates `checkParameterValue` (V4), plus nodes not reached by a BFS
 * from input-category nodes (V5). The property generates arbitrary canvas
 * graphs from catalog-shaped descriptors and then applies a random sequence
 * of canvas mutations (parameter edits, edge/node additions and removals),
 * re-applying markers after every mutation — so markers must both appear on
 * newly offending nodes (1.9) and disappear from nodes whose condition was
 * resolved by the mutation (1.10).
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import type { Edge } from '@xyflow/react';
import { applyValidationMarkers } from './validationMarkers';
import { checkParameterValue } from './parameters';
import {
  removeNodesAndAttachedEdges,
  WORKFLOW_NODE_TYPE,
  type BuilderNode,
} from './builderGraph';
import {
  CATEGORIES,
  CATEGORY_INPUT,
  PORT_TYPE_VIDEO_FRAMES,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
} from './types';

// --------------------------------------------------------------------------
// Parameter value construction
// --------------------------------------------------------------------------

/** A value that satisfies a parameter of the given type (no constraints used). */
function validValueFor(paramType: string): JsonValue {
  switch (paramType) {
    case 'string':
      return 'valid value';
    case 'int':
      return 5;
    default:
      return true;
  }
}

/** A value that violates the declared type of the parameter. */
function invalidValueFor(paramType: string): JsonValue {
  switch (paramType) {
    case 'string':
      return 42;
    case 'int':
      return 'not an int';
    default:
      return 'not a bool';
  }
}

/** How a node instance fills one of its parameters. */
type ValueChoice = 'valid' | 'invalid' | 'clear' | 'omit';

const valueChoiceArb: fc.Arbitrary<ValueChoice> = fc.constantFrom(
  'valid',
  'invalid',
  'clear',
  'omit'
);

const MAX_PARAMETERS = 3;

function parametersFor(
  descriptor: NodeTypeDescriptor,
  choices: ValueChoice[]
): Record<string, JsonValue> {
  const parameters: Record<string, JsonValue> = {};
  descriptor.parameters.forEach((parameter, index) => {
    const choice = choices[index] ?? 'omit';
    if (choice === 'valid') {
      parameters[parameter.name] = validValueFor(parameter.paramType);
    } else if (choice === 'invalid') {
      parameters[parameter.name] = invalidValueFor(parameter.paramType);
    } else if (choice === 'clear') {
      parameters[parameter.name] = null;
    }
    // 'omit': key absent — the declared default (if any) applies.
  });
  return parameters;
}

// --------------------------------------------------------------------------
// Catalog generator (catalog-shaped descriptors)
// --------------------------------------------------------------------------

const parameterSpecArb = fc.record({
  paramType: fc.constantFrom('string', 'int', 'bool'),
  required: fc.boolean(),
  hasDefault: fc.boolean(),
});

const descriptorSpecArb = fc.record({
  category: fc.constantFrom(...CATEGORIES),
  parameters: fc.array(parameterSpecArb, { minLength: 0, maxLength: MAX_PARAMETERS }),
});

/** 1..5 node type descriptors with unique type ids, random categories and parameters. */
const catalogArb: fc.Arbitrary<NodeTypeDescriptor[]> = fc
  .array(descriptorSpecArb, { minLength: 1, maxLength: 5 })
  .map((specs) =>
    specs.map(
      (spec, i): NodeTypeDescriptor => ({
        typeId: `type_${i + 1}`,
        category: spec.category,
        displayName: `Type ${i + 1}`,
        inputs: [{ name: 'in', portType: PORT_TYPE_VIDEO_FRAMES }],
        outputs: [{ name: 'out', portType: PORT_TYPE_VIDEO_FRAMES }],
        parameters: spec.parameters.map(
          (p, j): ParameterDescriptor => ({
            name: `p${j + 1}`,
            paramType: p.paramType,
            required: p.required,
            ...(p.hasDefault ? { default: validValueFor(p.paramType) } : {}),
          })
        ),
        mappings: [],
        hardwareDependent: false,
      })
    )
  );

// --------------------------------------------------------------------------
// Graph and mutation generators
// --------------------------------------------------------------------------

interface NodeSpec {
  descriptorIndex: number;
  choices: ValueChoice[];
}

const nodeSpecArb: fc.Arbitrary<NodeSpec> = fc.record({
  descriptorIndex: fc.nat(),
  choices: fc.array(valueChoiceArb, { minLength: MAX_PARAMETERS, maxLength: MAX_PARAMETERS }),
});

type Mutation =
  | { kind: 'setParam'; node: number; param: number; choice: ValueChoice }
  | { kind: 'addEdge'; source: number; target: number }
  | { kind: 'removeEdge'; edge: number }
  | { kind: 'addNode'; spec: NodeSpec }
  | { kind: 'removeNode'; node: number };

const mutationArb: fc.Arbitrary<Mutation> = fc.oneof(
  fc.record({
    kind: fc.constant<'setParam'>('setParam'),
    node: fc.nat(),
    param: fc.nat(),
    choice: valueChoiceArb,
  }),
  fc.record({ kind: fc.constant<'addEdge'>('addEdge'), source: fc.nat(), target: fc.nat() }),
  fc.record({ kind: fc.constant<'removeEdge'>('removeEdge'), edge: fc.nat() }),
  fc.record({ kind: fc.constant<'addNode'>('addNode'), spec: nodeSpecArb }),
  fc.record({ kind: fc.constant<'removeNode'>('removeNode'), node: fc.nat() })
);

const scenarioArb = fc.record({
  catalog: catalogArb,
  nodeSpecs: fc.array(nodeSpecArb, { minLength: 0, maxLength: 6 }),
  edgeSpecs: fc.array(fc.record({ source: fc.nat(), target: fc.nat() }), {
    minLength: 0,
    maxLength: 10,
  }),
  mutations: fc.array(mutationArb, { minLength: 0, maxLength: 8 }),
});

function buildNode(id: string, spec: NodeSpec, catalog: NodeTypeDescriptor[]): BuilderNode {
  const descriptor = catalog[spec.descriptorIndex % catalog.length];
  return {
    id,
    type: WORKFLOW_NODE_TYPE,
    position: { x: 0, y: 0 },
    data: {
      descriptor,
      parameters: parametersFor(descriptor, spec.choices),
      validationMessages: [],
    },
  };
}

interface CanvasState {
  nodes: BuilderNode[];
  edges: Edge[];
}

/** Mimic one canvas mutation; indices are taken modulo the collection sizes. */
function applyMutation(
  state: CanvasState,
  mutation: Mutation,
  catalog: NodeTypeDescriptor[],
  counters: { node: number; edge: number }
): CanvasState {
  switch (mutation.kind) {
    case 'setParam': {
      if (state.nodes.length === 0) return state;
      const target = state.nodes[mutation.node % state.nodes.length];
      const descriptorParams = target.data.descriptor.parameters;
      if (descriptorParams.length === 0) return state;
      const parameter = descriptorParams[mutation.param % descriptorParams.length];
      const parameters = { ...target.data.parameters };
      if (mutation.choice === 'omit') {
        delete parameters[parameter.name];
      } else if (mutation.choice === 'clear') {
        parameters[parameter.name] = null;
      } else if (mutation.choice === 'valid') {
        parameters[parameter.name] = validValueFor(parameter.paramType);
      } else {
        parameters[parameter.name] = invalidValueFor(parameter.paramType);
      }
      return {
        nodes: state.nodes.map((node) =>
          node.id === target.id ? { ...node, data: { ...node.data, parameters } } : node
        ),
        edges: state.edges,
      };
    }
    case 'addEdge': {
      if (state.nodes.length === 0) return state;
      counters.edge += 1;
      const source = state.nodes[mutation.source % state.nodes.length].id;
      const target = state.nodes[mutation.target % state.nodes.length].id;
      const edge: Edge = {
        id: `edge_${counters.edge}`,
        source,
        sourceHandle: 'out',
        target,
        targetHandle: 'in',
      };
      return { nodes: state.nodes, edges: [...state.edges, edge] };
    }
    case 'removeEdge': {
      if (state.edges.length === 0) return state;
      const removed = state.edges[mutation.edge % state.edges.length].id;
      return { nodes: state.nodes, edges: state.edges.filter((edge) => edge.id !== removed) };
    }
    case 'addNode': {
      counters.node += 1;
      const node = buildNode(`n${counters.node}`, mutation.spec, catalog);
      return { nodes: [...state.nodes, node], edges: state.edges };
    }
    case 'removeNode': {
      if (state.nodes.length === 0) return state;
      const removed = state.nodes[mutation.node % state.nodes.length].id;
      return removeNodesAndAttachedEdges(state.nodes, state.edges, [removed]);
    }
  }
}

// --------------------------------------------------------------------------
// Oracle: the offending node set, computed independently
// --------------------------------------------------------------------------

/**
 * Nodes with a V4 finding (a parameter whose effective value — explicit
 * value when the key is present, else the declared default — violates
 * `checkParameterValue`) plus nodes with a V5 finding (not reached by a
 * BFS from input-category nodes).
 */
function expectedOffenders(nodes: BuilderNode[], edges: Edge[]): Set<string> {
  const offenders = new Set<string>();

  // V4: missing/invalid required parameter values per checkParameterValue.
  for (const node of nodes) {
    for (const parameter of node.data.descriptor.parameters) {
      const value = Object.prototype.hasOwnProperty.call(node.data.parameters, parameter.name)
        ? node.data.parameters[parameter.name]
        : parameter.default;
      if (checkParameterValue(parameter, value) !== null) {
        offenders.add(node.id);
        break;
      }
    }
  }

  // V5: unreachable from input-category nodes (forward BFS).
  const ids = new Set(nodes.map((node) => node.id));
  const successors = new Map<string, string[]>();
  for (const id of ids) {
    successors.set(id, []);
  }
  for (const edge of edges) {
    if (ids.has(edge.source) && ids.has(edge.target)) {
      successors.get(edge.source)!.push(edge.target);
    }
  }
  const roots = nodes
    .filter((node) => node.data.descriptor.category === CATEGORY_INPUT)
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
  for (const node of nodes) {
    if (!visited.has(node.id)) {
      offenders.add(node.id);
    }
  }

  return offenders;
}

/**
 * Apply markers to the current canvas state and assert the marked node
 * set equals the oracle's offending node set exactly (offenders carry
 * messages, resolved nodes carry none). Returns the marked nodes so the
 * next mutation operates on canvas state carrying the previous markers.
 */
function applyAndCheckMarkers(
  nodes: BuilderNode[],
  edges: Edge[],
  catalog: NodeTypeDescriptor[]
): BuilderNode[] {
  const marked = applyValidationMarkers(nodes, edges, catalog);

  // Markers never add/remove/reorder nodes.
  expect(marked.map((node) => node.id)).toEqual(nodes.map((node) => node.id));

  const actual = new Set(
    marked.filter((node) => node.data.validationMessages.length > 0).map((node) => node.id)
  );
  expect(actual).toEqual(expectedOffenders(marked, edges));
  return marked;
}

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 12: Inline markers are exactly the offending nodes', () => {
  it('marks exactly the V4/V5 offending nodes across arbitrary graphs and mutation sequences', () => {
    fc.assert(
      fc.property(scenarioArb, ({ catalog, nodeSpecs, edgeSpecs, mutations }) => {
        const counters = { node: 0, edge: 0 };

        const nodes = nodeSpecs.map((spec) => {
          counters.node += 1;
          return buildNode(`n${counters.node}`, spec, catalog);
        });
        const edges: Edge[] =
          nodes.length === 0
            ? []
            : edgeSpecs.map(({ source, target }) => {
                counters.edge += 1;
                return {
                  id: `edge_${counters.edge}`,
                  source: nodes[source % nodes.length].id,
                  sourceHandle: 'out',
                  target: nodes[target % nodes.length].id,
                  targetHandle: 'in',
                };
              });

        // Initial graph: markers are exactly the offending nodes (1.9).
        let state: CanvasState = { nodes, edges };
        state = { ...state, nodes: applyAndCheckMarkers(state.nodes, state.edges, catalog) };

        // Every subsequent canvas mutation: markers appear on newly
        // offending nodes and disappear from resolved ones (1.9, 1.10).
        for (const mutation of mutations) {
          state = applyMutation(state, mutation, catalog, counters);
          state = { ...state, nodes: applyAndCheckMarkers(state.nodes, state.edges, catalog) };
        }
      }),
      { numRuns: 25 }
    );
  });
});
