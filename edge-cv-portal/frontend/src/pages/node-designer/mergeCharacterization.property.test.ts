/**
 * **Feature: gst-parameter-prepopulation, Property 9: Merge appends exactly
 * the new names and reports the rest**
 *
 * For any existing parameter form list and any suggestion list, the merged
 * list SHALL equal the existing list plus, in suggestion order, exactly
 * those suggestions whose trimmed name matches no existing trimmed name;
 * `added` SHALL list those appended names; `alreadyDeclared` SHALL list
 * exactly the colliding suggestion names.
 *
 * **Validates: Requirements 6.2, 6.3**
 *
 * The oracle walks the suggestion list with the same declared-name set
 * semantics the merge specifies: a suggestion collides when its trimmed
 * name matches an existing trimmed name OR the trimmed name of a
 * suggestion already appended earlier in the same scan (duplicate names
 * within one suggestion list: the first non-colliding occurrence is
 * appended, later duplicates are reported as alreadyDeclared). Appended
 * rows are the `formFromSuggestion` conversions, in suggestion order.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { formFromSuggestion, mergeSuggestions } from './scan';
import { existingParametersArb, suggestionsArb } from './scanArbitraries';

describe('Property 9: Merge appends exactly the new names and reports the rest', () => {
  it('appends exactly the non-colliding suggestions in order and reports added/alreadyDeclared exactly', () => {
    fc.assert(
      fc.property(
        existingParametersArb,
        suggestionsArb,
        (existing, suggestions) => {
          const result = mergeSuggestions(existing, suggestions);

          // Oracle: replay the declared-name-set semantics independently.
          const declared = new Set(existing.map((row) => row.name.trim()));
          const expectedAppended: ReturnType<typeof formFromSuggestion>[] = [];
          const expectedAdded: string[] = [];
          const expectedAlreadyDeclared: string[] = [];
          for (const suggestion of suggestions) {
            const name = suggestion.name.trim();
            if (declared.has(name)) {
              expectedAlreadyDeclared.push(name);
            } else {
              declared.add(name);
              expectedAppended.push(formFromSuggestion(suggestion));
              expectedAdded.push(name);
            }
          }

          // Merged list = existing list + exactly the new suggestions,
          // converted, in suggestion order (6.2).
          expect(result.parameters).toEqual([...existing, ...expectedAppended]);

          // `added` lists exactly the appended trimmed names, in order (6.2).
          expect(result.added).toEqual(expectedAdded);

          // `alreadyDeclared` lists exactly the colliding suggestion
          // names, in order (6.3).
          expect(result.alreadyDeclared).toEqual(expectedAlreadyDeclared);

          // Every suggestion is accounted for exactly once.
          expect(result.added.length + result.alreadyDeclared.length).toBe(
            suggestions.length
          );
        }
      ),
      { numRuns: 100 }
    );
  });
});
