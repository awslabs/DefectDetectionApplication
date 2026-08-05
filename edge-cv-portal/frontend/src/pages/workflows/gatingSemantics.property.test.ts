// Feature: trigger-activation-runtime, Property 6: Dependent-parameter gating semantics
/**
 * **Feature: trigger-activation-runtime, Property 6: Dependent-parameter
 * gating semantics**
 *
 * For any parameter with a bare-name `dependsOn` and any controlling bool
 * value, the visibility decision equals the pre-feature decision (visible
 * iff the controlling parameter's effective value === true); and for any
 * parameter with a `"name=value"` `dependsOn` and any effective controlling
 * value (explicit, defaulted, or absent), the parameter is visible if and
 * only if the effective value's string form equals the literal — so
 * selecting `queue`/`debounce`/`poll` shows exactly
 * `queue_depth`/`debounce_ms`/`poll_interval_ms` respectively.
 *
 * The oracle transcribes the design's gating grammar independently of
 * `NodeConfigPanel.tsx` (effective value = explicit entry when the key is
 * present, else the declared default; string form = '' for null/undefined,
 * else String(value)), so agreement demonstrates `isParameterVisible`
 * implements the specified semantics rather than merely agreeing with
 * itself.
 *
 * **Validates: Requirements 3.1, 3.6**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { isParameterVisible } from './NodeConfigPanel';
import {
  MQTT_SUBSCRIBE_DESCRIPTOR,
  OPCUA_SUBSCRIBE_DESCRIPTOR,
  type JsonValue,
  type NodeTypeDescriptor,
  type ParameterDescriptor,
} from './types';

// --------------------------------------------------------------------------
// Descriptor helpers
// --------------------------------------------------------------------------

function param(
  name: string,
  paramType: string,
  overrides: Partial<ParameterDescriptor> = {}
): ParameterDescriptor {
  return { name, paramType, required: false, default: null, ...overrides };
}

/** Look up a named parameter on a real trigger descriptor (throws if absent). */
function parameterOf(descriptor: NodeTypeDescriptor, name: string): ParameterDescriptor {
  const found = descriptor.parameters.find((p) => p.name === name);
  if (found === undefined) {
    throw new Error(`parameter ${name} not found on ${descriptor.typeId}`);
  }
  return found;
}

/** isParameterVisible for a named parameter of a real trigger descriptor. */
function visibleOn(
  descriptor: NodeTypeDescriptor,
  name: string,
  parameters: Record<string, JsonValue>
): boolean {
  return isParameterVisible(parameterOf(descriptor, name), descriptor.parameters, parameters);
}

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
// Generators
// --------------------------------------------------------------------------

const CONTROLLING_NAME = 'ctrl';

/**
 * Explicit controlling values: bools (the bare-name domain), enum-style
 * string tokens (the policy/mode literals plus off-list tokens and the
 * empty string), and numbers whose String() form collides with literals —
 * exercising the string-form equality precisely.
 */
const controllingValueArb: fc.Arbitrary<JsonValue> = fc.constantFrom<JsonValue>(
  true,
  false,
  'queue',
  'drop',
  'debounce',
  'subscribe',
  'poll',
  'alpha',
  'true',
  '',
  0,
  1,
  500
);

/** Equality literals: policy/mode tokens plus string forms of non-strings. */
const literalArb: fc.Arbitrary<string> = fc.constantFrom(
  'queue',
  'drop',
  'debounce',
  'subscribe',
  'poll',
  'alpha',
  'true',
  'false',
  '',
  '0',
  '1',
  '500'
);

/** How the controlling parameter appears in the node's parameter record. */
type ControllingState = 'absent' | 'explicit' | 'explicit-null';
const controllingStateArb = fc.constantFrom<ControllingState>(
  'absent',
  'explicit',
  'explicit-null'
);

function parametersRecordFor(
  state: ControllingState,
  explicitValue: JsonValue
): Record<string, JsonValue> {
  switch (state) {
    case 'absent':
      return {};
    case 'explicit-null':
      return { [CONTROLLING_NAME]: null as unknown as JsonValue };
    default:
      return { [CONTROLLING_NAME]: explicitValue };
  }
}

interface BareNameCase {
  controllingDefault: JsonValue | null;
  state: ControllingState;
  explicitValue: JsonValue;
}

/** Bare-name gating: a bool controlling parameter with a drawn default. */
const bareNameCaseArb: fc.Arbitrary<BareNameCase> = fc.record({
  controllingDefault: fc.constantFrom<JsonValue | null>(true, false, null),
  state: controllingStateArb,
  explicitValue: controllingValueArb,
});

interface EqualityCase {
  controllingDefault: JsonValue | null | undefined;
  literal: string;
  state: ControllingState;
  explicitValue: JsonValue;
}

/**
 * "name=value" gating: an enum-style controlling parameter with a drawn
 * default (declared, null, or entirely absent) and a drawn literal.
 */
const equalityCaseArb: fc.Arbitrary<EqualityCase> = fc.record({
  controllingDefault: fc.option(controllingValueArb, { nil: undefined }),
  literal: literalArb,
  state: controllingStateArb,
  explicitValue: controllingValueArb,
});

// --------------------------------------------------------------------------
// Properties (>=100 runs each)
// --------------------------------------------------------------------------

describe('Property 6: Dependent-parameter gating semantics', () => {
  it('bare-name dependsOn keeps the pre-feature bool-truthy decision: visible iff the effective controlling value === true', () => {
    fc.assert(
      fc.property(bareNameCaseArb, ({ controllingDefault, state, explicitValue }) => {
        const controlling = param(CONTROLLING_NAME, 'bool', { default: controllingDefault });
        const dependent = param('dep', 'string', { dependsOn: CONTROLLING_NAME });
        const allParameters = [controlling, dependent];
        const parameters = parametersRecordFor(state, explicitValue);

        // Pre-feature oracle: strict === true on the effective value.
        const expected = oracleEffective(parameters, controlling) === true;

        expect(isParameterVisible(dependent, allParameters, parameters)).toBe(expected);
      }),
      { numRuns: 150 }
    );
  });

  it('"name=value" dependsOn gates on string-form equality of the effective controlling value (explicit, defaulted, or absent)', () => {
    fc.assert(
      fc.property(equalityCaseArb, ({ controllingDefault, literal, state, explicitValue }) => {
        const controlling = param(CONTROLLING_NAME, 'enum', {
          default: controllingDefault as JsonValue | null,
          constraints: { values: ['queue', 'drop', 'debounce', 'subscribe', 'poll'] },
        });
        const dependent = param('dep', 'int', {
          dependsOn: `${CONTROLLING_NAME}=${literal}`,
        });
        const allParameters = [controlling, dependent];
        const parameters = parametersRecordFor(state, explicitValue);

        const expected =
          oracleStringForm(oracleEffective(parameters, controlling)) === literal;

        expect(isParameterVisible(dependent, allParameters, parameters)).toBe(expected);
      }),
      { numRuns: 150 }
    );
  });

  it('real trigger descriptors: each policy/mode selection shows exactly its gated companion(s) among the dependsOn-gated parameters', () => {
    const policyArb = fc.constantFrom<'queue' | 'drop' | 'debounce' | undefined>(
      'queue',
      'drop',
      'debounce',
      undefined // absent -> defaults to 'queue'
    );
    const modeArb = fc.constantFrom<'subscribe' | 'poll' | undefined>(
      'subscribe',
      'poll',
      undefined // absent -> defaults to 'subscribe'
    );
    const awsIotArb = fc.constantFrom<boolean | undefined>(true, false, undefined);

    fc.assert(
      fc.property(
        fc.constantFrom(MQTT_SUBSCRIBE_DESCRIPTOR, OPCUA_SUBSCRIBE_DESCRIPTOR),
        policyArb,
        modeArb,
        awsIotArb,
        (descriptor, policy, mode, awsIot) => {
          const parameters: Record<string, JsonValue> = {};
          if (policy !== undefined) {
            parameters.concurrency_policy = policy;
          }
          if (descriptor === OPCUA_SUBSCRIBE_DESCRIPTOR && mode !== undefined) {
            parameters.mode = mode;
          }
          if (descriptor === MQTT_SUBSCRIBE_DESCRIPTOR && awsIot !== undefined) {
            parameters.aws_iot = awsIot;
          }

          const effectivePolicy = policy ?? 'queue';
          const effectiveMode = mode ?? 'subscribe';

          // Expected visible subset of the descriptor's dependsOn-gated params.
          const expectedVisible = new Set<string>();
          if (effectivePolicy === 'queue') {
            expectedVisible.add('queue_depth');
          }
          if (effectivePolicy === 'debounce') {
            expectedVisible.add('debounce_ms');
          }
          if (descriptor === OPCUA_SUBSCRIBE_DESCRIPTOR && effectiveMode === 'poll') {
            expectedVisible.add('poll_interval_ms');
          }
          if (descriptor === MQTT_SUBSCRIBE_DESCRIPTOR && awsIot === true) {
            expectedVisible.add('iot_thing_name');
            expectedVisible.add('iot_ca_cert_path');
            expectedVisible.add('iot_client_cert_path');
            expectedVisible.add('iot_private_key_path');
          }

          const gated = descriptor.parameters.filter(
            (p) => p.dependsOn !== undefined && p.dependsOn !== null && p.dependsOn !== ''
          );
          const actualVisible = new Set(
            gated
              .filter((p) => isParameterVisible(p, descriptor.parameters, parameters))
              .map((p) => p.name)
          );

          expect(actualVisible).toEqual(expectedVisible);

          // Selecting queue/debounce/poll shows exactly its companion.
          expect(visibleOn(descriptor, 'queue_depth', parameters)).toBe(
            effectivePolicy === 'queue'
          );
          expect(visibleOn(descriptor, 'debounce_ms', parameters)).toBe(
            effectivePolicy === 'debounce'
          );
          if (descriptor === OPCUA_SUBSCRIBE_DESCRIPTOR) {
            expect(visibleOn(descriptor, 'poll_interval_ms', parameters)).toBe(
              effectiveMode === 'poll'
            );
          }

          // Ungated policy-family parameters stay visible regardless.
          expect(visibleOn(descriptor, 'concurrency_policy', parameters)).toBe(true);
          expect(visibleOn(descriptor, 'retry_limit', parameters)).toBe(true);
          expect(visibleOn(descriptor, 'priority', parameters)).toBe(true);
        }
      ),
      { numRuns: 100 }
    );
  });
});
