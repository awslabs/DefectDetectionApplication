/**
 * **Feature: custom-node-code-assist, Property 6: Reconciliation preserves manual entries and replaces derived ones**
 *
 * For all requirements texts (mixing manual lines, version pins, user
 * comments, blank lines, and previously derived marker lines) and all
 * derived requirement lists, `reconcileRequirements(text, derived)`:
 *
 * 1. keeps every manual (non-derived) line verbatim and in order;
 * 2. removes every previously derived line that is not re-derived —
 *    every derived line in the output corresponds to an entry in the
 *    new derived list;
 * 3. adds no derived entry whose PEP 503-normalized distribution
 *    equals a surviving manual entry's distribution.
 *
 * **Validates: Requirements 3.5, 3.9**
 *
 * The generators draw distribution names from a small shared pool with
 * random case and `-`/`_`/`.` separator variation, so PEP 503-equal
 * collisions between manual entries, previously derived lines, and the
 * new derived list occur frequently.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  DERIVED_MARKER,
  parseRequirements,
  reconcileRequirements,
  type DerivedRequirement,
} from './importAnalyzer';

// --------------------------------------------------------------------------
// PEP 503 normalization oracle (independent of the module under test)
// --------------------------------------------------------------------------

function pep503(name: string): string {
  return name.replace(/[-_.]+/g, '-').toLowerCase();
}

// --------------------------------------------------------------------------
// Distribution name generator: shared pool × case/separator variation
// --------------------------------------------------------------------------

/** Base names sharing several PEP 503 equivalence classes. */
const BASE_NAMES = [
  'numpy',
  'opencv-python-headless',
  'requests',
  'my-lib',
  'foo.bar',
  'pkg2',
  'scikit-learn',
] as const;

/** Rewrite a base name with a random separator and optional upper case. */
const distributionNameArb: fc.Arbitrary<string> = fc
  .record({
    base: fc.constantFrom(...BASE_NAMES),
    separator: fc.constantFrom('-', '_', '.'),
    upper: fc.boolean(),
  })
  .map(({ base, separator, upper }) => {
    const renamed = base.replace(/[-_.]/g, separator);
    return upper ? renamed.toUpperCase() : renamed;
  });

// --------------------------------------------------------------------------
// Requirements-text line generators
// --------------------------------------------------------------------------

const versionPinArb: fc.Arbitrary<string> = fc
  .record({
    op: fc.constantFrom('==', '>=', '<=', '~=', '>'),
    major: fc.nat({ max: 20 }),
    minor: fc.nat({ max: 20 }),
  })
  .map(({ op, major, minor }) => `${op}${major}.${minor}`);

/** Manual requirement line: name, optional pin, optional benign comment. */
const manualRequirementLineArb: fc.Arbitrary<string> = fc
  .record({
    name: distributionNameArb,
    pin: fc.option(versionPinArb, { nil: undefined }),
    comment: fc.option(fc.constantFrom('# pinned by me', '# keep this'), { nil: undefined }),
  })
  .map(({ name, pin, comment }) => `${name}${pin ?? ''}${comment ? `  ${comment}` : ''}`);

/** Full-line user comment (never contains the derived marker). */
const commentLineArb: fc.Arbitrary<string> = fc.constantFrom(
  '# my notes',
  '## section: vision deps',
  '#'
);

/** Blank-ish line. */
const blankLineArb: fc.Arbitrary<string> = fc.constantFrom('', '  ');

/** Previously derived marker line, with or without the needs-review suffix. */
const derivedLineArb: fc.Arbitrary<string> = fc
  .record({
    name: distributionNameArb,
    needsReview: fc.boolean(),
    spacing: fc.constantFrom('  ', ' ', '   '),
  })
  .map(
    ({ name, needsReview, spacing }) =>
      `${name}${spacing}${DERIVED_MARKER}${needsReview ? ' (verify package name)' : ''}`
  );

const lineArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 3, arbitrary: manualRequirementLineArb },
  { weight: 1, arbitrary: commentLineArb },
  { weight: 1, arbitrary: blankLineArb },
  { weight: 3, arbitrary: derivedLineArb }
);

const requirementsTextArb: fc.Arbitrary<string> = fc
  .array(lineArb, { minLength: 0, maxLength: 10 })
  .map((lines) => lines.join('\n'));

// --------------------------------------------------------------------------
// Derived list generator
// --------------------------------------------------------------------------

const derivedListArb: fc.Arbitrary<DerivedRequirement[]> = fc.array(
  fc.record({ distribution: distributionNameArb, needsReview: fc.boolean() }),
  { minLength: 0, maxLength: 6 }
);

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 6: Reconciliation preserves manual entries and replaces derived ones', () => {
  it('keeps manual lines verbatim in order, drops stale derived lines, and never duplicates a surviving manual distribution', () => {
    fc.assert(
      fc.property(requirementsTextArb, derivedListArb, (currentText, derived) => {
        const inputEntries = parseRequirements(currentText);
        const manualInputLines = inputEntries
          .filter((entry) => !entry.derived)
          .map((entry) => entry.raw);
        const manualDistributions = new Set(
          inputEntries
            .filter((entry) => !entry.derived && entry.distribution !== null)
            .map((entry) => entry.distribution as string)
        );

        const result = reconcileRequirements(currentText, derived);
        const outputEntries = parseRequirements(result);

        // 1. Every manual line kept verbatim and in order (3.5): the
        //    non-derived lines of the output are exactly the
        //    non-derived lines of the input, byte-identical.
        const manualOutputLines = outputEntries
          .filter((entry) => !entry.derived)
          .map((entry) => entry.raw);
        expect(manualOutputLines).toEqual(manualInputLines);

        // 2. Every previously derived line not re-derived is removed
        //    (3.5): every derived line in the output carries a
        //    distribution from the new derived list.
        const derivedSet = new Set(derived.map((entry) => pep503(entry.distribution)));
        for (const entry of outputEntries.filter((e) => e.derived)) {
          expect(entry.distribution).not.toBeNull();
          expect(derivedSet.has(entry.distribution as string)).toBe(true);
        }

        // 3. No derived entry added whose PEP 503-normalized
        //    distribution equals a surviving manual entry's (3.9).
        for (const entry of outputEntries.filter((e) => e.derived)) {
          expect(manualDistributions.has(entry.distribution as string)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });
});
