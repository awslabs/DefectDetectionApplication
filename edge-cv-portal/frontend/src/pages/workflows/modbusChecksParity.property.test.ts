// Feature: modbus-tcp-output, Property 14: Inline V4 parity (frontend)
/**
 * **Feature: modbus-tcp-output, Property 14: Inline V4 parity
 * (frontend)**
 *
 * For any `modbus_write` node configuration with any subset of the
 * required parameters (`host`, `register_type`, `address`) missing, the
 * frontend inline checks produce findings matching the backend
 * Workflow_Validator's V4 findings for the same graph in check code,
 * severity, and node identifier.
 *
 * A true cross-language comparison is not possible inside vitest, so —
 * following the established `triggerChecksParity.property.test.ts`
 * pattern — the oracle below independently transcribes the backend
 * `_check_v4` missing-required semantics from
 * `workflow_core/validator/checks.py`: for every node with a known
 * descriptor, each parameter whose effective value (the explicitly set
 * value when the key is present — an explicit null counts as cleared —
 * else the declared default) is null/undefined while the parameter is
 * required yields exactly one `V4_MISSING_REQUIRED_PARAMETER` error
 * finding carrying that node id. For `modbus_write` that means: `host`
 * and `address` (default null) are missing when absent or explicitly
 * cleared; `register_type` (default `coil`) is missing only when
 * explicitly cleared; invalid-but-present values (blank host,
 * out-of-range address) are V4_INVALID findings, never MISSING ones.
 *
 * Agreement between `checkV4` (and the composed `runInlineChecks`
 * slice) and the oracle demonstrates frontend/backend parity for the
 * same graph.
 *
 * **Validates: Requirements 3.4**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  checkV4,
  CODE_V4_MISSING_REQUIRED_PARAMETER,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import {
  MODBUS_WRITE_DESCRIPTOR,
  SEVERITY_ERROR,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
  type ValidationFinding,
  type WorkflowNode,
} from './types';

// --------------------------------------------------------------------------
// Catalog fixture: the real modbus_write mirror is the descriptor under
// test.
// --------------------------------------------------------------------------

const CATALOG: NodeTypeDescriptor[] = [MODBUS_WRITE_DESCRIPTOR];

const DESCRIPTOR_BY_TYPE = new Map(CATALOG.map((d) => [d.typeId, d]));

// --------------------------------------------------------------------------
// Node spec generators: each required parameter may be absent, an
// explicit null (cleared), a valid value, or an invalid-but-present
// value (which must yield an INVALID finding, never a MISSING one); the
// optional parameters may be absent or set.
// --------------------------------------------------------------------------

/** A parameter's presence in the node's parameter record. */
type ParamState =
  | { kind: 'absent' }
  | { kind: 'explicit'; value: JsonValue };

function stateArb(values: JsonValue[]): fc.Arbitrary<ParamState> {
  return fc.oneof(
    fc.constant<ParamState>({ kind: 'absent' }),
    fc
      .constantFrom<JsonValue>(null as unknown as JsonValue, ...values)
      .map((value): ParamState => ({ kind: 'explicit', value }))
  );
}

const hostStateArb = stateArb(['192.168.1.30', 'plc.local', '', '   ']);
const registerTypeStateArb = stateArb(['coil', 'holding_register']);
const addressStateArb = stateArb([0, 12, 65535, 70000, -1]);

const optionalParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc.record(
  {
    port: fc.constantFrom<JsonValue>(502, 1),
    unit_id: fc.constantFrom<JsonValue>(0, 1),
    value_template: fc.constantFrom<JsonValue>('{is_anomalous}', '{confidence}'),
    pulse_ms: fc.constantFrom<JsonValue>(0, 250),
  },
  { requiredKeys: [] }
);

interface ModbusNodeSpec {
  host: ParamState;
  registerType: ParamState;
  address: ParamState;
  optional: Record<string, JsonValue>;
}

const modbusNodeSpecArb: fc.Arbitrary<ModbusNodeSpec> = fc.record({
  host: hostStateArb,
  registerType: registerTypeStateArb,
  address: addressStateArb,
  optional: optionalParametersArb,
});

function buildGraph(specs: readonly ModbusNodeSpec[]): GraphLike {
  const nodes: WorkflowNode[] = specs.map((spec, index) => {
    const parameters: Record<string, JsonValue> = { ...spec.optional };
    if (spec.host.kind === 'explicit') {
      parameters.host = spec.host.value;
    }
    if (spec.registerType.kind === 'explicit') {
      parameters.register_type = spec.registerType.value;
    }
    if (spec.address.kind === 'explicit') {
      parameters.address = spec.address.value;
    }
    return {
      id: `n${index + 1}`,
      type: 'modbus_write',
      position: { x: 0, y: 0 },
      parameters,
    };
  });
  return { nodes, connections: [] };
}

/** Graphs of 1..5 modbus_write nodes with independent parameter subsets. */
const graphArb: fc.Arbitrary<GraphLike> = fc
  .array(modbusNodeSpecArb, { minLength: 1, maxLength: 5 })
  .map(buildGraph);

// --------------------------------------------------------------------------
// Oracle: the backend `_check_v4` missing-required semantics,
// transcribed independently of `inlineChecks.ts`
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

/** Multiset form: one finding per missing required parameter per node. */
function sortedKeys(triples: FindingTriple[]): string[] {
  return triples.map(tripleKey).sort();
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
 * Backend `_check_v4` missing-required semantics: one
 * `V4_MISSING_REQUIRED_PARAMETER` error finding per (node, required
 * parameter) whose effective value is null/undefined. Present values —
 * valid or constraint-violating — never produce a MISSING finding.
 */
function oracleV4Missing(graph: GraphLike): FindingTriple[] {
  const triples: FindingTriple[] = [];
  for (const node of graph.nodes) {
    const descriptor = DESCRIPTOR_BY_TYPE.get(node.type);
    if (descriptor === undefined) {
      continue;
    }
    for (const parameter of descriptor.parameters) {
      const value = effectiveValueOracle(node, parameter);
      if ((value === null || value === undefined) && parameter.required) {
        triples.push({
          code: CODE_V4_MISSING_REQUIRED_PARAMETER,
          nodeId: node.id,
          severity: SEVERITY_ERROR,
        });
      }
    }
  }
  return triples;
}

/** The V4-missing slice of a finding list, as parity triples. */
function missingTriples(findings: ValidationFinding[]): FindingTriple[] {
  return findings
    .filter((f) => f.code === CODE_V4_MISSING_REQUIRED_PARAMETER)
    .map((f) => ({ code: f.code, nodeId: f.nodeId, severity: f.severity }));
}

// --------------------------------------------------------------------------
// Property (>=100 runs each)
// --------------------------------------------------------------------------

describe('Property 14: inline V4 checks for modbus_write mirror the backend validator semantics', () => {
  it('checkV4 missing-required findings (code, nodeId, severity) equal the backend-V4 oracle for any graph', () => {
    fc.assert(
      fc.property(graphArb, (graph) => {
        const slice = missingTriples(checkV4(graph, CATALOG));
        for (const triple of slice) {
          expect(triple.code).toBe(CODE_V4_MISSING_REQUIRED_PARAMETER);
          expect(triple.severity).toBe(SEVERITY_ERROR);
        }
        // One finding per (node, missing required parameter): multiset
        // equality so a node missing both host and address counts twice.
        expect(sortedKeys(slice)).toEqual(sortedKeys(oracleV4Missing(graph)));
      }),
      { numRuns: 100 }
    );
  });

  it('the composed runInlineChecks V4-missing slice equals the same oracle for any graph', () => {
    fc.assert(
      fc.property(graphArb, (graph) => {
        const slice = missingTriples(runInlineChecks(graph, CATALOG));
        expect(sortedKeys(slice)).toEqual(sortedKeys(oracleV4Missing(graph)));
      }),
      { numRuns: 100 }
    );
  });
});
