/**
 * **Feature: gst-parameter-prepopulation, Property 8: Merge never changes
 * existing declarations**
 *
 * For any existing parameter form list and any suggestion list, every entry
 * of `mergeSuggestions(existing, suggestions).parameters` at an index below
 * `existing.length` SHALL deep-equal the corresponding existing entry, in
 * the same order.
 *
 * **Validates: Requirements 6.1**
 *
 * The merge is the wizard's "no silent overwrite" guarantee: whatever the
 * scan suggests, the rows the user already declared stay byte-for-byte in
 * place. The property generates arbitrary form lists (all paramTypes, names
 * with/without surrounding whitespace, rows with and without ride-along
 * min/max constraints) and arbitrary suggestion lists (with/without
 * defaults, examples, numeric or enum constraints — including name
 * collisions with the existing rows) and asserts the existing prefix of the
 * merged list deep-equals the input, in order.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { mergeSuggestions } from './scan';
import { existingParametersArb, suggestionsArb } from './scanArbitraries';

describe('Property 8: Merge never changes existing declarations', () => {
  it('keeps every existing row deep-equal and in place, whatever the suggestions', () => {
    fc.assert(
      fc.property(
        existingParametersArb,
        suggestionsArb,
        (existing, suggestions) => {
          // Deep snapshot of the input so in-place mutation is caught too.
          const snapshot = structuredClone(existing);

          const result = mergeSuggestions(existing, suggestions);

          // The merged list starts with at least the existing rows.
          expect(result.parameters.length).toBeGreaterThanOrEqual(
            existing.length
          );

          // Every entry below existing.length deep-equals the corresponding
          // existing entry, in the same order.
          expect(result.parameters.slice(0, existing.length)).toEqual(
            snapshot
          );

          // The input list itself is untouched (merge is pure).
          expect(existing).toEqual(snapshot);
        }
      ),
      { numRuns: 100 }
    );
  });
});
