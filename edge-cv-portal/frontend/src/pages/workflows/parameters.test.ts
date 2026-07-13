import { describe, it, expect } from 'vitest';
import {
  checkParameterValue,
  isParameterValueValid,
  VIOLATION_MAX,
  VIOLATION_MAX_LENGTH,
  VIOLATION_MIN,
  VIOLATION_MIN_LENGTH,
  VIOLATION_REGEX,
  VIOLATION_REQUIRED,
  VIOLATION_TYPE,
  VIOLATION_UNKNOWN_TYPE,
  VIOLATION_VALUES,
} from './parameters';
import type { ParameterDescriptor } from './types';

/**
 * Unit tests for the TypeScript mirror of
 * `workflow_core.validator.parameters` (Requirement 1.8).
 */

function descriptor(overrides: Partial<ParameterDescriptor>): ParameterDescriptor {
  return { name: 'p', paramType: 'string', required: true, default: null, ...overrides };
}

describe('missing values', () => {
  it('reports PARAM_REQUIRED for a required parameter with null value', () => {
    expect(checkParameterValue(descriptor({ required: true }), null)?.code).toBe(
      VIOLATION_REQUIRED
    );
  });

  it('reports PARAM_REQUIRED for a required parameter with undefined value', () => {
    expect(checkParameterValue(descriptor({ required: true }), undefined)?.code).toBe(
      VIOLATION_REQUIRED
    );
  });

  it('accepts a missing value for an optional parameter', () => {
    expect(checkParameterValue(descriptor({ required: false }), null)).toBeNull();
  });
});

describe('type checks', () => {
  it('accepts strings for string-like types', () => {
    for (const paramType of ['string', 'code', 'model_ref']) {
      expect(checkParameterValue(descriptor({ paramType }), 'hello')).toBeNull();
      expect(checkParameterValue(descriptor({ paramType }), 42)?.code).toBe(VIOLATION_TYPE);
    }
  });

  it('accepts integers and rejects booleans and fractions for int', () => {
    const d = descriptor({ paramType: 'int' });
    expect(checkParameterValue(d, 5)).toBeNull();
    expect(checkParameterValue(d, -3)).toBeNull();
    expect(checkParameterValue(d, true)?.code).toBe(VIOLATION_TYPE); // bool is never an int
    expect(checkParameterValue(d, 1.5)?.code).toBe(VIOLATION_TYPE);
    expect(checkParameterValue(d, '5')?.code).toBe(VIOLATION_TYPE);
    expect(checkParameterValue(d, Number.NaN)?.code).toBe(VIOLATION_TYPE);
  });

  it('accepts ints and floats but rejects booleans for float', () => {
    const d = descriptor({ paramType: 'float' });
    expect(checkParameterValue(d, 1.5)).toBeNull();
    expect(checkParameterValue(d, 3)).toBeNull(); // int accepted for float
    expect(checkParameterValue(d, false)?.code).toBe(VIOLATION_TYPE);
    expect(checkParameterValue(d, '1.5')?.code).toBe(VIOLATION_TYPE);
  });

  it('accepts only booleans for bool', () => {
    const d = descriptor({ paramType: 'bool' });
    expect(checkParameterValue(d, true)).toBeNull();
    expect(checkParameterValue(d, false)).toBeNull();
    expect(checkParameterValue(d, 1)?.code).toBe(VIOLATION_TYPE);
    expect(checkParameterValue(d, 'true')?.code).toBe(VIOLATION_TYPE);
  });

  it('reports PARAM_UNKNOWN_TYPE for unknown declared types', () => {
    expect(checkParameterValue(descriptor({ paramType: 'mystery' }), 1)?.code).toBe(
      VIOLATION_UNKNOWN_TYPE
    );
  });
});

describe('values membership', () => {
  it('accepts members and rejects non-members for enums', () => {
    const d = descriptor({ paramType: 'enum', constraints: { values: ['a', 'b'] } });
    expect(checkParameterValue(d, 'a')).toBeNull();
    expect(checkParameterValue(d, 'c')?.code).toBe(VIOLATION_VALUES);
  });

  it('supports discrete value sets on int parameters', () => {
    const d = descriptor({ paramType: 'int', constraints: { values: [90, 180, 270] } });
    expect(checkParameterValue(d, 180)).toBeNull();
    expect(checkParameterValue(d, 45)?.code).toBe(VIOLATION_VALUES);
  });

  it('never conflates booleans with 0/1 in membership', () => {
    const zeroOne = descriptor({ paramType: 'enum', constraints: { values: [0, 1] } });
    expect(checkParameterValue(zeroOne, true)?.code).toBe(VIOLATION_VALUES);
    expect(checkParameterValue(zeroOne, false)?.code).toBe(VIOLATION_VALUES);

    const bools = descriptor({ paramType: 'enum', constraints: { values: [true, false] } });
    expect(checkParameterValue(bools, 1)?.code).toBe(VIOLATION_VALUES);
    expect(checkParameterValue(bools, true)).toBeNull();
  });
});

describe('numeric range constraints', () => {
  const d = descriptor({ paramType: 'float', constraints: { min: 0, max: 10 } });

  it('accepts values within and at the bounds', () => {
    expect(checkParameterValue(d, 0)).toBeNull();
    expect(checkParameterValue(d, 10)).toBeNull();
    expect(checkParameterValue(d, 5.5)).toBeNull();
  });

  it('reports PARAM_MIN below the minimum and PARAM_MAX above the maximum', () => {
    expect(checkParameterValue(d, -0.1)?.code).toBe(VIOLATION_MIN);
    expect(checkParameterValue(d, 10.1)?.code).toBe(VIOLATION_MAX);
  });

  it('fails NaN against a bounded range', () => {
    expect(checkParameterValue(d, Number.NaN)?.code).toBe(VIOLATION_MIN);
  });
});

describe('string constraints', () => {
  it('enforces minLength and maxLength', () => {
    const d = descriptor({ constraints: { minLength: 2, maxLength: 4 } });
    expect(checkParameterValue(d, 'ab')).toBeNull();
    expect(checkParameterValue(d, 'abcd')).toBeNull();
    expect(checkParameterValue(d, 'a')?.code).toBe(VIOLATION_MIN_LENGTH);
    expect(checkParameterValue(d, 'abcde')?.code).toBe(VIOLATION_MAX_LENGTH);
  });

  it('applies regex with search semantics (match anywhere)', () => {
    const d = descriptor({ constraints: { regex: '[0-9]+' } });
    expect(checkParameterValue(d, 'abc123def')).toBeNull(); // unanchored match
    expect(checkParameterValue(d, 'abcdef')?.code).toBe(VIOLATION_REGEX);
  });
});

describe('isParameterValueValid', () => {
  it('is the boolean form of checkParameterValue', () => {
    const d = descriptor({ paramType: 'int', constraints: { min: 1 } });
    expect(isParameterValueValid(d, 2)).toBe(true);
    expect(isParameterValueValid(d, 0)).toBe(false);
  });
});
