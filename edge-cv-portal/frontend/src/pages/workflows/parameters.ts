/**
 * Shared parameter constraint predicate (Requirements 1.8, 4.4).
 *
 * TypeScript mirror of `workflow_core.validator.parameters` — keep the
 * semantics and violation codes in sync with the Python source of truth.
 *
 * Validates a single parameter value against its `ParameterDescriptor`:
 * declared type check plus the declared constraints (min/max for numeric
 * types, minLength/maxLength/regex for string-like types, `values`
 * membership for enums and discrete value sets).
 *
 * Mirrored semantics of note:
 *   - a missing value (null/undefined) violates only required parameters
 *   - booleans are never accepted for int/float parameters
 *   - integers are accepted for float parameters (JSON number semantics)
 *   - NaN fails bounded ranges (negated comparisons)
 *   - regex uses search semantics (pattern may match anywhere)
 *   - booleans are never conflated with 0/1 in `values` membership
 */

import type { JsonValue, ParameterDescriptor } from './types';

// --------------------------------------------------------------------------
// Violation codes (stable identifiers shared with the Python validator)
// --------------------------------------------------------------------------

export const VIOLATION_REQUIRED = 'PARAM_REQUIRED';
export const VIOLATION_TYPE = 'PARAM_TYPE';
export const VIOLATION_MIN = 'PARAM_MIN';
export const VIOLATION_MAX = 'PARAM_MAX';
export const VIOLATION_MIN_LENGTH = 'PARAM_MIN_LENGTH';
export const VIOLATION_MAX_LENGTH = 'PARAM_MAX_LENGTH';
export const VIOLATION_REGEX = 'PARAM_REGEX';
export const VIOLATION_VALUES = 'PARAM_VALUES';
export const VIOLATION_UNKNOWN_TYPE = 'PARAM_UNKNOWN_TYPE';

/** Parameter types whose values are strings. */
const STRING_LIKE_TYPES = ['string', 'code', 'model_ref'];

/**
 * Why a parameter value fails validation.
 *
 * `code` is a stable machine-readable identifier; `message` is the
 * human-readable reason displayed by the configuration panel
 * (Requirement 1.8).
 */
export interface ParameterViolation {
  code: string;
  message: string;
}

/**
 * Validate `value` against `descriptor`.
 *
 * Returns null when the value is valid, otherwise a ParameterViolation
 * describing the first constraint violated. A missing value
 * (null/undefined) is a violation only for required parameters.
 */
export function checkParameterValue(
  descriptor: ParameterDescriptor,
  value: JsonValue | null | undefined
): ParameterViolation | null {
  const name = descriptor.name;

  if (value === null || value === undefined) {
    if (descriptor.required) {
      return {
        code: VIOLATION_REQUIRED,
        message: `Required parameter '${name}' has no value`,
      };
    }
    return null;
  }

  const typeViolation = checkType(descriptor, value);
  if (typeViolation !== null) {
    return typeViolation;
  }

  return checkConstraints(descriptor, value);
}

/** Boolean form of {@link checkParameterValue}. */
export function isParameterValueValid(
  descriptor: ParameterDescriptor,
  value: JsonValue | null | undefined
): boolean {
  return checkParameterValue(descriptor, value) === null;
}

// --------------------------------------------------------------------------
// Type check
// --------------------------------------------------------------------------

function checkType(descriptor: ParameterDescriptor, value: JsonValue): ParameterViolation | null {
  const paramType = descriptor.paramType;
  const name = descriptor.name;

  if (STRING_LIKE_TYPES.includes(paramType)) {
    if (typeof value !== 'string') {
      return typeViolation(name, paramType, value);
    }
    return null;
  }

  if (paramType === 'int') {
    // Mirror of Python's bool-is-not-int rule; in JSON terms an int is a
    // number with no fractional part (NaN/Infinity are not integers).
    if (typeof value !== 'number' || !Number.isInteger(value)) {
      return typeViolation(name, paramType, value);
    }
    return null;
  }

  if (paramType === 'float') {
    // Accept any number (ints included, JSON number semantics); never bool.
    if (typeof value !== 'number') {
      return typeViolation(name, paramType, value);
    }
    return null;
  }

  if (paramType === 'bool') {
    if (typeof value !== 'boolean') {
      return typeViolation(name, paramType, value);
    }
    return null;
  }

  if (paramType === 'enum') {
    // An enum value's "type" is membership in the declared value set;
    // the membership itself is checked with the constraints below.
    return null;
  }

  return {
    code: VIOLATION_UNKNOWN_TYPE,
    message: `Parameter '${name}' has unknown declared type '${paramType}'`,
  };
}

function typeViolation(name: string, paramType: string, value: JsonValue): ParameterViolation {
  return {
    code: VIOLATION_TYPE,
    message: `Parameter '${name}' expects type '${paramType}' but got ${describeType(value)}`,
  };
}

function describeType(value: JsonValue): string {
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

// --------------------------------------------------------------------------
// Constraint checks
// --------------------------------------------------------------------------

function checkConstraints(
  descriptor: ParameterDescriptor,
  value: JsonValue
): ParameterViolation | null {
  const constraints = descriptor.constraints ?? {};
  const name = descriptor.name;

  // `values` membership: enum parameters and discrete value sets on
  // other types (e.g. an int parameter restricted to specific values).
  if (constraints.values !== undefined) {
    const allowed = constraints.values;
    if (!allowed.some((member) => matchesMember(value, member))) {
      return {
        code: VIOLATION_VALUES,
        message: `Parameter '${name}' value ${JSON.stringify(value)} is not one of ${JSON.stringify(allowed)}`,
      };
    }
  }

  // Numeric range. Negated comparisons so NaN fails bounded ranges.
  if (typeof value === 'number') {
    const minimum = constraints.min;
    if (minimum !== undefined && minimum !== null && !(value >= minimum)) {
      return {
        code: VIOLATION_MIN,
        message: `Parameter '${name}' value ${value} is below the minimum ${minimum}`,
      };
    }
    const maximum = constraints.max;
    if (maximum !== undefined && maximum !== null && !(value <= maximum)) {
      return {
        code: VIOLATION_MAX,
        message: `Parameter '${name}' value ${value} is above the maximum ${maximum}`,
      };
    }
  }

  // String length and pattern.
  if (typeof value === 'string') {
    const minLength = constraints.minLength;
    if (minLength !== undefined && minLength !== null && value.length < minLength) {
      return {
        code: VIOLATION_MIN_LENGTH,
        message: `Parameter '${name}' must be at least ${minLength} character(s) long`,
      };
    }
    const maxLength = constraints.maxLength;
    if (maxLength !== undefined && maxLength !== null && value.length > maxLength) {
      return {
        code: VIOLATION_MAX_LENGTH,
        message: `Parameter '${name}' must be at most ${maxLength} character(s) long`,
      };
    }
    const pattern = constraints.regex;
    if (pattern !== undefined && pattern !== null && !new RegExp(pattern).test(value)) {
      // RegExp.test without anchors mirrors Python's re.search semantics.
      return {
        code: VIOLATION_REGEX,
        message: `Parameter '${name}' value does not match the required pattern '${pattern}'`,
      };
    }
  }

  return null;
}

/**
 * Equality for `values` membership that never conflates booleans with
 * the numerically equal ints. Strict equality already distinguishes
 * `true`/`false` from `1`/`0` in JavaScript; the explicit boolean guard
 * mirrors the Python source of truth.
 */
function matchesMember(value: JsonValue, member: JsonValue): boolean {
  if (typeof value === 'boolean' || typeof member === 'boolean') {
    return value === member;
  }
  return value === member;
}
