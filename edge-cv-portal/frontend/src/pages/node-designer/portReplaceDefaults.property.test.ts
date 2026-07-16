/**
 * **Feature: port-guidance-and-pad-prepopulation, Property 10: Untouched
 * defaults are replaced by the suggestions**
 *
 * For any non-empty Port_Suggestion list, applying it over the
 * Untouched_Defaults (`untouched=true`) yields input and output lists that
 * are exactly the suggestions partitioned by direction, in suggestion
 * order, each as `{name, portType}`; the applied names are exactly the
 * suggestion names and the unconfirmed names are exactly the non-confident
 * suggestions' names.
 *
 * **Validates: Requirements 6.1**
 *
 * This is the auto-populate contract of the Ports step: when the user has
 * not touched the wizard-supplied default lists (one input "in" and one
 * output "out", both VideoFrames), the scan replaces them wholesale with
 * the pad-derived suggestions. The property generates suggestion lists in
 * both directions and both confidence states (caps honoring the
 * `video/x-raw` prefix contract) and asserts the exact replacement
 * characterization, including that nothing lands in `alreadyDeclared` and
 * that the input lists themselves stay unmutated.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { applySuggestions, isUntouchedDefaults } from './portScan';
import {
  nonEmptyPortSuggestionsArb,
  untouchedDefaultInputs,
  untouchedDefaultOutputs,
} from './portScanArbitraries';

describe('Property 10: Untouched defaults are replaced by the suggestions', () => {
  it('replaces both sides with the suggestions partitioned by direction, in order', () => {
    fc.assert(
      fc.property(nonEmptyPortSuggestionsArb, (suggestions) => {
        const inputs = untouchedDefaultInputs();
        const outputs = untouchedDefaultOutputs();

        // The generated defaults are the Untouched_Defaults by definition.
        expect(isUntouchedDefaults(inputs, outputs)).toBe(true);

        const result = applySuggestions(inputs, outputs, suggestions, true);

        // Each side is exactly the suggestions of that direction, in
        // suggestion order, as bare {name, portType} form rows.
        expect(result.inputs).toEqual(
          suggestions
            .filter((s) => s.direction === 'input')
            .map((s) => ({ name: s.name, portType: s.portType }))
        );
        expect(result.outputs).toEqual(
          suggestions
            .filter((s) => s.direction === 'output')
            .map((s) => ({ name: s.name, portType: s.portType }))
        );

        // Applied names are exactly the suggestion names, in order;
        // unconfirmed names are exactly the non-confident ones; the
        // replacement never reports anything as already declared.
        expect(result.applied).toEqual(suggestions.map((s) => s.name));
        expect(result.unconfirmed).toEqual(
          suggestions.filter((s) => !s.confident).map((s) => s.name)
        );
        expect(result.alreadyDeclared).toEqual([]);

        // The default lists themselves are untouched (apply is pure).
        expect(inputs).toEqual(untouchedDefaultInputs());
        expect(outputs).toEqual(untouchedDefaultOutputs());
      }),
      { numRuns: 100 }
    );
  });
});
