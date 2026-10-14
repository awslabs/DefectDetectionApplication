/**
 * **Feature: gst-parameter-prepopulation, Property 11: Suggestion-to-form
 * conversion round-trips through form assembly**
 *
 * For any Parameter_Suggestion, converting it with `formFromSuggestion` and
 * assembling it back with `declaration.ts`'s `parameterFromForm` conversion
 * path SHALL reproduce the suggestion's name, paramType, required flag,
 * default, description, first example, and enum values.
 *
 * **Validates: Requirements 2.6, 3.3**
 *
 * The scan pre-populates raw-text form rows; on submit the wizard assembles
 * them back into ParameterDeclarations through `buildDeclaration` (the
 * exported path over the private `parameterFromForm`). This property is the
 * lossless-ness guarantee of that pipeline: a scanned suggestion the user
 * never touches submits exactly what the backend suggested — including the
 * numeric min/max constraints riding along on the form row (the 3.3
 * retention mechanism, no editable UI).
 *
 * The generator (`realisticSuggestionArb`, scanArbitraries.ts) mirrors the
 * backend Type_Mapping's actual wire contract (`gst_properties.py`
 * `map_property`/`_suggestion`): launch-safe GObject property names,
 * trim-stable blurbs, non-empty string defaults, comma-free enum nicks,
 * in-range numeric defaults, and exactly one example per suggestion (2.6).
 * A failure here is a genuine round-trip bug, not generator noise.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { formFromSuggestion } from './scan';
import { buildDeclaration } from './declaration';
import type { ParameterForm } from './declaration';
import type { ParameterDeclaration } from './types';
import { realisticSuggestionArb } from './scanArbitraries';

/**
 * Assemble one form row back into its declaration through the exported
 * submit path (buildDeclaration -> private parameterFromForm), exactly as
 * the wizards do (buildRegistrationDeclaration delegates here too).
 */
function assembleParameter(form: ParameterForm): ParameterDeclaration {
  const declaration = buildDeclaration({
    name: 'Scan Roundtrip Fixture',
    description: '',
    category: 'preprocessing',
    inputs: [],
    outputs: [],
    parameters: [form],
    architectures: ['x86_64'],
  });
  expect(declaration.parameters).toHaveLength(1);
  return declaration.parameters[0];
}

describe('Property 11: Suggestion-to-form conversion round-trips through form assembly', () => {
  it('reproduces name, paramType, required, default, description, first example and enum values', () => {
    fc.assert(
      fc.property(realisticSuggestionArb, (suggestion) => {
        const assembled = assembleParameter(formFromSuggestion(suggestion));

        expect(assembled.name).toBe(suggestion.name);
        expect(assembled.paramType).toBe(suggestion.paramType);
        expect(assembled.required).toBe(suggestion.required);
        expect(assembled.description).toBe(suggestion.description);

        // First example survives the raw-text round trip with its type
        // (the backend always serves exactly one example, 2.6).
        expect(assembled.examples).toEqual([suggestion.examples[0]]);

        // Default: reproduced when the suggestion carried one, absent when
        // the suggestion was required with no default (3.1/3.2 shape).
        if ('default' in suggestion) {
          expect(assembled.default).toEqual(suggestion.default);
        } else {
          expect('default' in assembled).toBe(false);
        }

        // Constraints: enum values round-trip through the comma-joined
        // text field; numeric min/max ride along on the form row and are
        // re-emitted verbatim at assembly (the 3.3 retention mechanism).
        if (suggestion.constraints !== undefined) {
          expect(assembled.constraints).toEqual(suggestion.constraints);
        } else {
          expect(assembled.constraints).toBeUndefined();
        }
      }),
      { numRuns: 100 }
    );
  });
});
