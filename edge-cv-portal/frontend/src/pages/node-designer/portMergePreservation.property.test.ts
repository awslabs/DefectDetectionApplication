/**
 * **Feature: port-guidance-and-pad-prepopulation, Property 11: Merge
 * preserves edits and appends exactly the new names**
 *
 * For any user-edited port lists and any Port_Suggestion list, the
 * additive merge (`untouched=false`) keeps every existing port unchanged
 * and in place; each suggestion whose trimmed name exactly
 * (case-sensitively) matches an already-declared trimmed port name is
 * reported in `alreadyDeclared` without modifying that port; every other
 * suggestion is appended to its side in suggestion order and reported in
 * `applied` (with the non-confident ones in `unconfirmed`); and an empty
 * suggestion list returns lists identical to the originals.
 *
 * **Validates: Requirements 6.2, 6.10, 6.11**
 *
 * This is the user-edits-win contract of the Port_Scan: a scan over
 * edited lists never renames, retypes, reorders, or removes anything the
 * user declared — it only appends the genuinely new names. The arbitraries
 * draw names from a small shared pool with whitespace padding, so
 * form/suggestion collisions (including collisions that only exist after
 * trimming, and duplicate names within the suggestion list itself) are
 * generated often.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { applySuggestions } from './portScan';
import type { PortSuggestion } from './portScan';
import type { PortForm } from './declaration';
import { portListArb, portSuggestionsArb } from './portScanArbitraries';

/**
 * Independent per-suggestion classification: a suggestion collides when
 * its trimmed name exactly (case-sensitively) matches the trimmed name
 * of a port declared on either side — including a port appended by an
 * earlier suggestion of the same scan (6.2). Everything else is new.
 */
function classify(
  inputs: PortForm[],
  outputs: PortForm[],
  suggestions: PortSuggestion[]
): { colliding: PortSuggestion[]; fresh: PortSuggestion[] } {
  const declared = new Set(
    [...inputs, ...outputs].map((port) => port.name.trim())
  );
  const colliding: PortSuggestion[] = [];
  const fresh: PortSuggestion[] = [];
  for (const suggestion of suggestions) {
    const trimmed = suggestion.name.trim();
    if (declared.has(trimmed)) {
      colliding.push(suggestion);
    } else {
      declared.add(trimmed);
      fresh.push(suggestion);
    }
  }
  return { colliding, fresh };
}

describe('Property 11: Merge preserves edits and appends exactly the new names', () => {
  it('keeps every existing port in place and appends exactly the non-colliding suggestions in order', () => {
    fc.assert(
      fc.property(
        portListArb,
        portListArb,
        portSuggestionsArb,
        (inputs, outputs, suggestions) => {
          const inputsBefore = inputs.map((port) => ({ ...port }));
          const outputsBefore = outputs.map((port) => ({ ...port }));

          const result = applySuggestions(inputs, outputs, suggestions, false);

          // Every existing port is kept unchanged and in place: the
          // originals form the exact prefix of each merged side (6.2).
          expect(result.inputs.slice(0, inputs.length)).toEqual(inputsBefore);
          expect(result.outputs.slice(0, outputs.length)).toEqual(
            outputsBefore
          );

          const { colliding, fresh } = classify(inputs, outputs, suggestions);

          // Exact case-sensitive trimmed-name matches are reported as
          // already declared, in suggestion order, without modification
          // of the matching port (6.2).
          expect(result.alreadyDeclared).toEqual(
            colliding.map((s) => s.name.trim())
          );

          // Every other suggestion is appended to its side in suggestion
          // order as a bare {name, portType} row and reported applied
          // (6.11); the non-confident applied names are the unconfirmed
          // set (6.5 surfacing).
          expect(result.inputs.slice(inputs.length)).toEqual(
            fresh
              .filter((s) => s.direction === 'input')
              .map((s) => ({ name: s.name, portType: s.portType }))
          );
          expect(result.outputs.slice(outputs.length)).toEqual(
            fresh
              .filter((s) => s.direction === 'output')
              .map((s) => ({ name: s.name, portType: s.portType }))
          );
          expect(result.applied).toEqual(fresh.map((s) => s.name));
          expect(result.unconfirmed).toEqual(
            fresh.filter((s) => !s.confident).map((s) => s.name)
          );

          // Every suggestion is accounted for exactly once.
          expect(result.applied.length + result.alreadyDeclared.length).toBe(
            suggestions.length
          );

          // An empty suggestion list returns identical lists (6.10).
          if (suggestions.length === 0) {
            expect(result.inputs).toEqual(inputsBefore);
            expect(result.outputs).toEqual(outputsBefore);
            expect(result.applied).toEqual([]);
            expect(result.alreadyDeclared).toEqual([]);
            expect(result.unconfirmed).toEqual([]);
          }

          // The merge is pure: the caller's lists are never mutated.
          expect(inputs).toEqual(inputsBefore);
          expect(outputs).toEqual(outputsBefore);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('returns identical lists for an empty suggestion list over any edited lists', () => {
    fc.assert(
      fc.property(portListArb, portListArb, (inputs, outputs) => {
        const result = applySuggestions(inputs, outputs, [], false);

        // Nothing changes and nothing is reported (6.10).
        expect(result.inputs).toEqual(inputs);
        expect(result.outputs).toEqual(outputs);
        expect(result.applied).toEqual([]);
        expect(result.alreadyDeclared).toEqual([]);
        expect(result.unconfirmed).toEqual([]);
      }),
      { numRuns: 100 }
    );
  });
});
