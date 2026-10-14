// Feature: trigger-activation-runtime, Property 5: Inline-check parity for the new checks
/**
 * **Feature: trigger-activation-runtime, Property 5: Inline-check parity
 * for the new checks**
 *
 * For any generated graph, the frontend `checkV8`/`checkV9` inline
 * findings identify the same offending nodes, severities, and check
 * codes as the backend validator's V8/V9 findings for the same graph.
 *
 * A true cross-language comparison is not possible inside vitest, so —
 * following the established `inlineChecksParity.property.test.ts`
 * pattern — the oracle below independently transcribes the backend
 * `_check_v8`/`_check_v9` semantics from
 * `workflow_core/validator/checks.py`:
 *
 * - **V8** (`V8_MQTT_SUB_NO_TARGET`): every `mqtt_subscribe` node whose
 *   effective values (explicit, else declared default) enable neither
 *   `greengrass` nor `aws_iot` and supply no non-empty (non-whitespace)
 *   `broker_host` yields exactly one error finding carrying that node id.
 * - **V9** (`V9_MIXED_ACTIVATION_MODEL`): when the graph contains at
 *   least one `mqtt_subscribe` or `opcua_subscribe` node, every
 *   `CATEGORY_INPUT` node with no connection targeting its `activation`
 *   input port yields exactly one error finding carrying that node id;
 *   graphs with zero subscription trigger nodes yield zero V9 findings
 *   (`digital_input` presence alone does not engage V9).
 *
 * Agreement between `checkV8`/`checkV9` (and the composed
 * `runInlineChecks` slices) and the oracle demonstrates frontend/backend
 * parity for the same graph.
 *
 * **Validates: Requirements 4.5**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  checkV8,
  checkV9,
  CODE_V8_MQTT_SUB_NO_TARGET,
  CODE_V9_MIXED_ACTIVATION_MODEL,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import {
  CATEGORY_INPUT,
  CATEGORY_OUTPUT,
  CATEGORY_TRIGGER,
  MQTT_SUBSCRIBE_DESCRIPTOR,
  OPCUA_SUBSCRIBE_DESCRIPTOR,
  PORT_TYPE_EVENT_SIGNAL,
  PORT_TYPE_VIDEO_FRAMES,
  SEVERITY_ERROR,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type ValidationFinding,
  type WorkflowConnection,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Served-catalog-style descriptor fixtures. The two trigger descriptors
// are the real `types.ts` mirrors; the input/capture descriptors follow
// the fixture-catalog form of `inlineChecksParity.property.test.ts`
// (whose fixtures are module-private, so the shared shapes are restated
// here).
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
  MQTT_SUBSCRIBE_DESCRIPTOR,
  OPCUA_SUBSCRIBE_DESCRIPTOR,
  DIGITAL_INPUT,
  FOLDER_SOURCE,
  UNIFIED_INPUT,
  CAPTURE,
];

const DESCRIPTOR_BY_TYPE = new Map(CATALOG.map((d) => [d.typeId, d]));

/** The subscription trigger types that engage V9 (backend parity). */
const SUBSCRIPTION_TRIGGER_TYPES = new Set(['mqtt_subscribe', 'opcua_subscribe']);

// --------------------------------------------------------------------------
// Node spec generators
// --------------------------------------------------------------------------

interface NodeSpec {
  type: string;
  parameters: Record<string, JsonValue>;
}

/**
 * Target parameter combinations for `mqtt_subscribe`: each of the three
 * target parameters may be absent (falls to the declared default), an
 * explicit false/null, blank or whitespace-only (broker_host), or set —
 * covering every V8 branch (`topic` is always present so V4 noise stays
 * out of the picture, though V8 does not read it).
 */
const mqttSubscribeSpecArb: fc.Arbitrary<NodeSpec> = fc
  .record(
    {
      greengrass: fc.boolean(),
      aws_iot: fc.boolean(),
      broker_host: fc.constantFrom<JsonValue>(null, '', '   ', 'broker.local', '10.0.0.5'),
    },
    { requiredKeys: [] }
  )
  .map((target) => ({
    type: 'mqtt_subscribe',
    parameters: { topic: 'factory/line1/trigger', ...target },
  }));

const opcuaSubscribeSpecArb: fc.Arbitrary<NodeSpec> = fc.constant({
  type: 'opcua_subscribe',
  parameters: { endpoint: 'opc.tcp://192.168.1.20:4840', node_id: 'ns=2;s=Machine1.Go' },
});

const digitalInputSpecArb: fc.Arbitrary<NodeSpec> = fc.constant({
  type: 'digital_input',
  parameters: { pin: 4 },
});

const inputOrOutputSpecArb: fc.Arbitrary<NodeSpec> = fc.constantFrom<NodeSpec>(
  { type: 'unified_input', parameters: { source_kind: 'folder' } },
  { type: 'folder_source', parameters: { location: '/aws_dda/images' } },
  { type: 'capture', parameters: { output_path: '/out' } }
);

/** Any node spec — new triggers, digital_input, inputs, and outputs. */
const anyNodeSpecArb: fc.Arbitrary<NodeSpec> = fc.oneof(
  { arbitrary: mqttSubscribeSpecArb, weight: 3 },
  { arbitrary: opcuaSubscribeSpecArb, weight: 2 },
  { arbitrary: digitalInputSpecArb, weight: 1 },
  { arbitrary: inputOrOutputSpecArb, weight: 4 }
);

// --------------------------------------------------------------------------
// Graph generators: 1..6 nodes, 0..8 index-wired connections targeting
// either a data port or the activation port (so inputs arise both with
// and without activation wiring).
// --------------------------------------------------------------------------

interface EdgeSpec {
  source: number;
  target: number;
  targetPort: 'in' | 'activation';
}

const edgeSpecArb: fc.Arbitrary<EdgeSpec> = fc.record({
  source: fc.nat(),
  target: fc.nat(),
  targetPort: fc.constantFrom<'in' | 'activation'>('in', 'activation'),
});

function buildGraph(specs: readonly NodeSpec[], edgeSpecs: EdgeSpec[]): GraphLike {
  const nodes: WorkflowNode[] = specs.map((spec, index) => ({
    id: `n${index + 1}`,
    type: spec.type,
    position: { x: 0, y: 0 },
    parameters: spec.parameters,
  }));
  const connections: WorkflowConnection[] = edgeSpecs.map((spec, index) => ({
    id: `c${index + 1}`,
    from: { node: nodes[spec.source % nodes.length].id, port: 'out' },
    to: { node: nodes[spec.target % nodes.length].id, port: spec.targetPort },
  }));
  return { nodes, connections };
}

function graphArbFrom(nodeSpecArb: fc.Arbitrary<NodeSpec>): fc.Arbitrary<GraphLike> {
  return fc
    .record({
      specs: fc.array(nodeSpecArb, { minLength: 1, maxLength: 6 }),
      edgeSpecs: fc.array(edgeSpecArb, { minLength: 0, maxLength: 8 }),
    })
    .map(({ specs, edgeSpecs }) => buildGraph(specs, edgeSpecs));
}

/** Graphs over the full fixture catalog (new triggers may appear). */
const anyGraphArb: fc.Arbitrary<GraphLike> = graphArbFrom(anyNodeSpecArb);

/** digital_input-only trigger graphs: no mqtt/opcua_subscribe node. */
const digitalInputGraphArb: fc.Arbitrary<GraphLike> = fc
  .record({
    specs: fc.array(fc.oneof(digitalInputSpecArb, inputOrOutputSpecArb), {
      minLength: 0,
      maxLength: 5,
    }),
    edgeSpecs: fc.array(edgeSpecArb, { minLength: 0, maxLength: 8 }),
  })
  .map(({ specs, edgeSpecs }) =>
    buildGraph([{ type: 'digital_input', parameters: { pin: 4 } }, ...specs], edgeSpecs)
  );

/** Non-trigger graphs: inputs and outputs only. */
const nonTriggerGraphArb: fc.Arbitrary<GraphLike> = graphArbFrom(inputOrOutputSpecArb);

// --------------------------------------------------------------------------
// Oracle: the backend `_check_v8`/`_check_v9` semantics, transcribed
// independently of `inlineChecks.ts`
// --------------------------------------------------------------------------

/** A finding reduced to its parity-relevant triple. */
interface FindingTriple {
  code: string;
  nodeId: string | null;
  severity: string;
}

function tripleKey(triple: FindingTriple): string {
  return `${triple.code}|${triple.nodeId}|${triple.severity}`;
}

function toKeySet(triples: FindingTriple[]): Set<string> {
  return new Set(triples.map(tripleKey));
}

function findingTriples(findings: ValidationFinding[]): FindingTriple[] {
  return findings.map((f) => ({ code: f.code, nodeId: f.nodeId, severity: f.severity }));
}

/**
 * Backend `_effective_value`: the explicitly set value when the key is
 * present (an explicit null counts as cleared), else the declared
 * default.
 */
function effectiveValueOracle(
  node: WorkflowNode,
  parameter: ParameterDescriptor
): JsonValue | null | undefined {
  if (Object.prototype.hasOwnProperty.call(node.parameters, parameter.name)) {
    return node.parameters[parameter.name];
  }
  return parameter.default;
}

/**
 * Backend `_check_v8` semantics: one error finding per `mqtt_subscribe`
 * node (with a known descriptor) whose effective values enable neither
 * `greengrass` nor `aws_iot` and supply no non-empty `broker_host`.
 */
function oracleV8(graph: GraphLike): FindingTriple[] {
  const triples: FindingTriple[] = [];
  for (const node of graph.nodes) {
    if (node.type !== 'mqtt_subscribe') {
      continue;
    }
    const descriptor = DESCRIPTOR_BY_TYPE.get(node.type);
    if (descriptor === undefined) {
      continue;
    }
    const values = new Map<string, JsonValue | null | undefined>(
      descriptor.parameters.map((p) => [p.name, effectiveValueOracle(node, p)])
    );
    const greengrass = Boolean(values.get('greengrass'));
    const awsIot = Boolean(values.get('aws_iot'));
    const brokerHost = values.get('broker_host');
    const hasBrokerHost = typeof brokerHost === 'string' && brokerHost.trim() !== '';
    if (!(greengrass || awsIot || hasBrokerHost)) {
      triples.push({
        code: CODE_V8_MQTT_SUB_NO_TARGET,
        nodeId: node.id,
        severity: SEVERITY_ERROR,
      });
    }
  }
  return triples;
}

/**
 * Backend `_check_v9` semantics: when the graph has at least one
 * `mqtt_subscribe`/`opcua_subscribe` node, one error finding per
 * `CATEGORY_INPUT` node (with a known descriptor) whose `activation`
 * input port is targeted by no connection; zero findings otherwise.
 */
function oracleV9(graph: GraphLike): FindingTriple[] {
  const hasSubscriptionTrigger = graph.nodes.some((node) =>
    SUBSCRIPTION_TRIGGER_TYPES.has(node.type)
  );
  if (!hasSubscriptionTrigger) {
    return [];
  }
  const activationConnected = new Set(
    graph.connections.filter((c) => c.to.port === 'activation').map((c) => c.to.node)
  );
  const triples: FindingTriple[] = [];
  for (const node of graph.nodes) {
    const descriptor = DESCRIPTOR_BY_TYPE.get(node.type);
    if (descriptor === undefined || descriptor.category !== CATEGORY_INPUT) {
      continue;
    }
    if (!activationConnected.has(node.id)) {
      triples.push({
        code: CODE_V9_MIXED_ACTIVATION_MODEL,
        nodeId: node.id,
        severity: SEVERITY_ERROR,
      });
    }
  }
  return triples;
}

/** The V8/V9 slice of a finding list, as parity triples. */
function newCheckTriples(findings: ValidationFinding[]): FindingTriple[] {
  return findingTriples(
    findings.filter(
      (f) => f.code === CODE_V8_MQTT_SUB_NO_TARGET || f.code === CODE_V9_MIXED_ACTIVATION_MODEL
    )
  );
}

// --------------------------------------------------------------------------
// Properties (>=100 runs each)
// --------------------------------------------------------------------------

describe('Property 5: inline V8/V9 checks mirror the backend validator semantics', () => {
  it('checkV8 findings (code, nodeId, severity) equal the backend-V8 oracle for any graph', () => {
    fc.assert(
      fc.property(anyGraphArb, (graph) => {
        const findings = checkV8(graph, CATALOG);
        for (const finding of findings) {
          expect(finding.code).toBe(CODE_V8_MQTT_SUB_NO_TARGET);
          expect(finding.severity).toBe(SEVERITY_ERROR);
        }
        // One finding per offending node: multiset equality via node ids.
        expect(findings.map((f) => f.nodeId).sort()).toEqual(
          oracleV8(graph)
            .map((t) => t.nodeId)
            .sort()
        );
        expect(toKeySet(findingTriples(findings))).toEqual(toKeySet(oracleV8(graph)));
      }),
      { numRuns: 100 }
    );
  });

  it('checkV9 findings (code, nodeId, severity) equal the backend-V9 oracle for any graph', () => {
    fc.assert(
      fc.property(anyGraphArb, (graph) => {
        const findings = checkV9(graph, CATALOG);
        for (const finding of findings) {
          expect(finding.code).toBe(CODE_V9_MIXED_ACTIVATION_MODEL);
          expect(finding.severity).toBe(SEVERITY_ERROR);
        }
        // One finding per unconnected input node: multiset equality.
        expect(findings.map((f) => f.nodeId).sort()).toEqual(
          oracleV9(graph)
            .map((t) => t.nodeId)
            .sort()
        );
        expect(toKeySet(findingTriples(findings))).toEqual(toKeySet(oracleV9(graph)));
      }),
      { numRuns: 100 }
    );
  });

  it('the composed runInlineChecks V8/V9 slice equals the combined oracle for any graph', () => {
    fc.assert(
      fc.property(anyGraphArb, (graph) => {
        const slice = newCheckTriples(runInlineChecks(graph, CATALOG));
        expect(toKeySet(slice)).toEqual(toKeySet([...oracleV8(graph), ...oracleV9(graph)]));
      }),
      { numRuns: 100 }
    );
  });

  it('digital_input-only trigger graphs yield zero V8/V9 findings (digital_input does not engage the new checks)', () => {
    fc.assert(
      fc.property(digitalInputGraphArb, (graph) => {
        expect(checkV8(graph, CATALOG)).toEqual([]);
        expect(checkV9(graph, CATALOG)).toEqual([]);
        expect(newCheckTriples(runInlineChecks(graph, CATALOG))).toEqual([]);
      }),
      { numRuns: 100 }
    );
  });

  it('non-trigger graphs yield zero V8/V9 findings', () => {
    fc.assert(
      fc.property(nonTriggerGraphArb, (graph) => {
        expect(checkV8(graph, CATALOG)).toEqual([]);
        expect(checkV9(graph, CATALOG)).toEqual([]);
        expect(newCheckTriples(runInlineChecks(graph, CATALOG))).toEqual([]);
      }),
      { numRuns: 100 }
    );
  });
});
