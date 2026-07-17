/**
 * **Feature: custom-node-code-assist, Property 7: Reconciliation idempotence**
 *
 * For all requirements texts (mixing manual lines, version pins, user
 * comments, blank lines, and previously derived marker lines) and all
 * derived requirement lists, applying `reconcileRequirements` twice
 * with the same derived list equals applying it once:
 *
 *   reconcileRequirements(reconcileRequirements(text, derived), derived)
 *     === reconcileRequirements(text, derived)
 *
 * **Validates: Requirements 3.5**
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
  reconcileRequirements,
  type DerivedRequirement,
} from './importAnalyzer';

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

describe('Property 7: Reconciliation idempotence', () => {
  it('applying reconcileRequirements twice with the same derived list equals applying it once', () => {
    fc.assert(
      fc.property(requirementsTextArb, derivedListArb, (currentText, derived) => {
        const once = reconcileRequirements(currentText, derived);
        const twice = reconcileRequirements(once, derived);
        expect(twice).toBe(once);
      }),
      { numRuns: 100 }
    );
  });
});
