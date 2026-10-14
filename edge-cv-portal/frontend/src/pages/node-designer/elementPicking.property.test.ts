/**
 * **Feature: gst-parameter-prepopulation, Property 12: Element picking
 * prefers the wizard's factory**
 *
 * For any non-empty element list and any preferred factory name,
 * `pickElement` SHALL return the element whose factory equals the
 * preferred name when one exists; else the sole element when the list
 * has exactly one; else null.
 *
 * **Validates: Requirements 5.4**
 *
 * pickElement drives the factory selector's pre-selection in the scan
 * panel (5.4): the wizard's default element factory wins when the report
 * contains it, a single-element report needs no choice, and anything
 * else leaves the choice to the user (null). The generator produces
 * element lists whose factory names draw mostly from a small pool, and
 * preferred factories drawn from the generated elements (guaranteed
 * hits), the pool/random names (frequent misses), or undefined (the
 * no-preference case). When duplicate factories occur the property
 * asserts on the returned element's factory (any element carrying the
 * preferred name satisfies 5.4), not on object identity.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { pickElement } from './scan';
import { factoryNameArb, scanElementArb } from './scanArbitraries';

/** A non-empty element list plus a preferred factory that sometimes
 *  matches a generated element, sometimes misses, and is sometimes
 *  absent entirely. */
const elementsAndPreferredArb = fc
  .array(scanElementArb, { minLength: 1, maxLength: 5 })
  .chain((elements) =>
    fc.tuple(
      fc.constant(elements),
      fc.oneof(
        // Guaranteed hit: one of the generated factories.
        fc.constantFrom(...elements.map((element) => element.factory)),
        // Pool/random name: hits sometimes, misses sometimes.
        factoryNameArb,
        // No preference at all.
        fc.constant<string | undefined>(undefined)
      )
    )
  );

describe("Property 12: Element picking prefers the wizard's factory", () => {
  it('returns the preferred-factory element when present, else the sole element, else null', () => {
    fc.assert(
      fc.property(elementsAndPreferredArb, ([elements, preferred]) => {
        const result = pickElement(elements, preferred);

        const preferredExists =
          preferred !== undefined &&
          elements.some((element) => element.factory === preferred);

        if (preferredExists) {
          // An element carrying the preferred factory is returned (factory
          // equality, not identity — duplicates may share the name).
          expect(result).not.toBeNull();
          expect(result!.factory).toBe(preferred);
          expect(elements).toContain(result);
        } else if (elements.length === 1) {
          // No preferred match: a sole element is the obvious pick.
          expect(result).toBe(elements[0]);
        } else {
          // No match, multiple elements: the user chooses (null).
          expect(result).toBeNull();
        }
      }),
      { numRuns: 100 }
    );
  });

  it('returns null for an empty element list, with or without a preference', () => {
    fc.assert(
      fc.property(
        fc.option(factoryNameArb, { nil: undefined }),
        (preferred) => {
          expect(pickElement([], preferred)).toBeNull();
        }
      ),
      { numRuns: 100 }
    );
  });
});
