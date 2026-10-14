/**
 * **Feature: port-guidance-and-pad-prepopulation, Property 12: Category
 * divergence flags exactly the diverging sides**
 *
 * For any palette category and any pair of port lists,
 * `guidanceDivergence(category, inputs, outputs)` SHALL return null iff
 * each side's port count and multiset of port types match the category's
 * arrangement (`'at-least-one'` diverging only on an empty input side);
 * otherwise it SHALL flag exactly the diverging side(s).
 *
 * **Validates: Requirements 2.4, 2.5**
 *
 * The generators bias each side toward a genuinely matching declaration
 * (ports built from the category's arrangement with arbitrary names, or a
 * non-empty list for `'at-least-one'`) about half the time and an arbitrary
 * port list the rest, so both the null branch and every per-side flag
 * combination are exercised. An independent multiset oracle (sorted
 * port-type comparison) decides the expected outcome.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  CATEGORY_ARRANGEMENTS,
  guidanceDivergence,
} from './portGuidance';
import { CATEGORIES, PORT_TYPES, type PortType } from './types';
import type { PortForm } from './declaration';

// ------------------------------------------------------------- arbitraries

const nameArb = fc.string({ maxLength: 12 });

/** Catalog types plus off-catalog strings, so type mismatches occur. */
const portTypeArb = fc.oneof(
  fc.constantFrom<string>(...PORT_TYPES),
  fc.constantFrom('Bogus', 'videoframes', '')
);

const portArb: fc.Arbitrary<PortForm> = fc.record({
  name: nameArb,
  portType: portTypeArb,
});

/** A fully arbitrary side (may or may not match any arrangement). */
const arbitrarySideArb = fc.array(portArb, { maxLength: 4 });

/** A side built to match the given arrangement exactly. */
function matchingSideArb(
  arrangement: PortType[] | 'at-least-one'
): fc.Arbitrary<PortForm[]> {
  if (arrangement === 'at-least-one') {
    return fc.array(portArb, { minLength: 1, maxLength: 4 });
  }
  if (arrangement.length === 0) {
    return fc.constant([]);
  }
  return fc
    .array(nameArb, {
      minLength: arrangement.length,
      maxLength: arrangement.length,
    })
    .map((names) =>
      arrangement.map((portType, i) => ({ name: names[i], portType }))
    );
}

/** Category plus port lists, each side biased 50/50 matching/arbitrary. */
const scenarioArb = fc
  .constantFrom(...CATEGORIES)
  .chain((category) => {
    const arrangement = CATEGORY_ARRANGEMENTS[category];
    return fc.record({
      category: fc.constant(category),
      inputs: fc.oneof(
        matchingSideArb(arrangement.inputs),
        arbitrarySideArb
      ),
      outputs: fc.oneof(
        matchingSideArb(arrangement.outputs),
        arbitrarySideArb
      ),
    });
  });

// ------------------------------------------------------------------ oracle

/** Independent multiset oracle: does the side match its arrangement? */
function sideMatches(
  arrangement: PortType[] | 'at-least-one',
  ports: readonly PortForm[]
): boolean {
  if (arrangement === 'at-least-one') {
    return ports.length > 0;
  }
  if (ports.length !== arrangement.length) {
    return false;
  }
  const expected = [...arrangement].sort();
  const actual = ports.map((port) => port.portType).sort();
  return expected.every((portType, i) => portType === actual[i]);
}

// ---------------------------------------------------------------- property

describe('Property 12: Category divergence flags exactly the diverging sides', () => {
  it('answers null iff both sides match, otherwise flags exactly the diverging side(s)', () => {
    fc.assert(
      fc.property(scenarioArb, ({ category, inputs, outputs }) => {
        const arrangement = CATEGORY_ARRANGEMENTS[category];
        const inputsMatch = sideMatches(arrangement.inputs, inputs);
        const outputsMatch = sideMatches(arrangement.outputs, outputs);

        const result = guidanceDivergence(category, inputs, outputs);

        if (inputsMatch && outputsMatch) {
          // Null iff both sides match the arrangement (2.5).
          expect(result).toBeNull();
        } else {
          // Exactly the diverging side(s) are flagged (2.4).
          expect(result).toEqual({
            inputs: !inputsMatch,
            outputs: !outputsMatch,
          });
        }
      }),
      { numRuns: 100 }
    );
  });
});
