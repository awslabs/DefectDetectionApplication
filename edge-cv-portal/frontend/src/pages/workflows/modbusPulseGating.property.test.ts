// Feature: modbus-tcp-output, Property 13: pulse_ms visibility gating (frontend)
/**
 * **Feature: modbus-tcp-output, Property 13: pulse_ms visibility gating
 * (frontend)**
 *
 * For any `modbus_write` parameter assignment, the configuration
 * panel's parameter-visibility predicate shows `pulse_ms` exactly when
 * the effective `register_type` value (explicit, else the declared
 * default `coil`) equals `coil`.
 *
 * Following `gatingSemantics.property.test.ts`, the oracle transcribes
 * the design's `"name=value"` gating grammar independently of
 * `NodeConfigPanel.tsx` (effective value = explicit entry when the key
 * is present, else the declared default; string form = '' for
 * null/undefined, else String(value)), so agreement demonstrates
 * `isParameterVisible` implements the specified semantics on the real
 * `MODBUS_WRITE_DESCRIPTOR` rather than merely agreeing with itself.
 *
 * **Validates: Requirements 3.3**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { isParameterVisible } from './NodeConfigPanel';
import {
  MODBUS_WRITE_DESCRIPTOR,
  type JsonValue,
  type ParameterDescriptor,
} from './types';

// --------------------------------------------------------------------------
// Descriptor lookup
// --------------------------------------------------------------------------

function parameterOf(name: string): ParameterDescriptor {
  const found = MODBUS_WRITE_DESCRIPTOR.parameters.find((p) => p.name === name);
  if (found === undefined) {
    throw new Error(`parameter ${name} not found on modbus_write`);
  }
  return found;
}

const PULSE_MS = parameterOf('pulse_ms');
const REGISTER_TYPE = parameterOf('register_type');

// --------------------------------------------------------------------------
// Oracle: the design's gating grammar, transcribed independently
// --------------------------------------------------------------------------

/** Effective value: the explicit entry when the key is present, else the default. */
function oracleEffective(
  parameters: Record<string, JsonValue>,
  descriptor: ParameterDescriptor
): JsonValue | null | undefined {
  if (Object.prototype.hasOwnProperty.call(parameters, descriptor.name)) {
    return parameters[descriptor.name];
  }
  return descriptor.default;
}

/** The effective value's string form ('' for null/undefined/absent). */
function oracleStringForm(value: JsonValue | null | undefined): string {
  if (value === null || value === undefined) {
    return '';
  }
  return typeof value === 'string' ? value : String(value);
}

// --------------------------------------------------------------------------
// Generators: full modbus_write parameter assignments
// --------------------------------------------------------------------------

/** How `register_type` appears in the node's parameter record. */
type RegisterTypeState =
  | { kind: 'absent' }
  | { kind: 'explicit'; value: JsonValue };

/**
 * `register_type` states: absent (falls to the declared default
 * `coil`), the two enum members, an explicit null (cleared), and
 * off-domain tokens (empty string, an unknown token, and values whose
 * String() form exercises the string-form equality) — the panel decides
 * visibility from the effective value regardless of enum validity.
 */
const registerTypeStateArb: fc.Arbitrary<RegisterTypeState> = fc.oneof(
  fc.constant<RegisterTypeState>({ kind: 'absent' }),
  fc
    .constantFrom<JsonValue>(
      'coil',
      'holding_register',
      null as unknown as JsonValue,
      '',
      'COIL',
      'coil ',
      'alpha',
      true,
      0
    )
    .map((value): RegisterTypeState => ({ kind: 'explicit', value }))
);

/**
 * The surrounding parameter assignment: every other modbus_write
 * parameter may be absent or carry a value — none of them may influence
 * the pulse_ms visibility decision.
 */
const otherParametersArb: fc.Arbitrary<Record<string, JsonValue>> = fc.record(
  {
    host: fc.constantFrom<JsonValue>('192.168.1.30', 'plc.local', ''),
    port: fc.constantFrom<JsonValue>(502, 1, 65535),
    unit_id: fc.constantFrom<JsonValue>(0, 1, 255),
    address: fc.constantFrom<JsonValue>(0, 12, 65535),
    value_template: fc.constantFrom<JsonValue>('{is_anomalous}', '{confidence}'),
    pulse_ms: fc.constantFrom<JsonValue>(0, 250, 60000),
  },
  { requiredKeys: [] }
);

interface Case {
  registerTypeState: RegisterTypeState;
  otherParameters: Record<string, JsonValue>;
}

const caseArb: fc.Arbitrary<Case> = fc.record({
  registerTypeState: registerTypeStateArb,
  otherParameters: otherParametersArb,
});

function parametersFor({ registerTypeState, otherParameters }: Case): Record<string, JsonValue> {
  const parameters: Record<string, JsonValue> = { ...otherParameters };
  if (registerTypeState.kind === 'explicit') {
    parameters.register_type = registerTypeState.value;
  }
  return parameters;
}

// --------------------------------------------------------------------------
// Property (>=100 runs)
// --------------------------------------------------------------------------

describe('Property 13: pulse_ms visibility gating (frontend)', () => {
  it('shows pulse_ms exactly when the effective register_type equals coil (explicit, else the declared default)', () => {
    fc.assert(
      fc.property(caseArb, (drawn) => {
        const parameters = parametersFor(drawn);

        const expected =
          oracleStringForm(oracleEffective(parameters, REGISTER_TYPE)) === 'coil';

        expect(
          isParameterVisible(PULSE_MS, MODBUS_WRITE_DESCRIPTOR.parameters, parameters)
        ).toBe(expected);
      }),
      { numRuns: 150 }
    );
  });
});
