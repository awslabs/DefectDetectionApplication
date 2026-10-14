/**
 * **Feature: gst-parameter-prepopulation, Property 10: Merge is idempotent**
 *
 * For any existing parameter form list and any suggestion list, merging the
 * same suggestions into an already-merged list SHALL return the list
 * unchanged with an empty `added` set (running the scan twice adds nothing).
 *
 * **Validates: Requirements 6.1, 6.2**
 *
 * Idempotence is what makes the "Scan plugin properties" button safe to
 * press repeatedly: the first merge appends every genuinely new suggestion,
 * so a second merge with the same suggestions finds every trimmed name
 * already declared and appends nothing. The property runs the merge twice
 * and asserts the second pass leaves the parameter list deep-equal to the
 * first pass's output and reports an empty `added` set.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { mergeSuggestions } from './scan';
import { existingParametersArb, suggestionsArb } from './scanArbitraries';

describe('Property 10: Merge is idempotent', () => {
  it('merging the same suggestions a second time changes nothing and adds nothing', () => {
    fc.assert(
      fc.property(
        existingParametersArb,
        suggestionsArb,
        (existing, suggestions) => {
          const first = mergeSuggestions(existing, suggestions);
          const second = mergeSuggestions(first.parameters, suggestions);

          // The second merge returns the list unchanged (6.1, 6.2).
          expect(second.parameters).toEqual(first.parameters);

          // Nothing new is appended on the second pass (6.2).
          expect(second.added).toEqual([]);
        }
      ),
      { numRuns: 100 }
    );
  });
});
