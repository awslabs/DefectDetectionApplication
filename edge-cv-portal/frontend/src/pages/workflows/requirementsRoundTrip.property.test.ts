/**
 * **Feature: custom-node-code-assist, Property 8: Requirements text round trip**
 *
 * For all lists of requirements entries,
 * `parseRequirements(renderRequirements(entries))` yields entries with
 * identical raw lines, derived flags, and needs-review flags — i.e.
 * render is an exact inverse of parse, so displaying the populated
 * list, re-parsing user edits, and reconciling never corrupt a line.
 *
 * **Validates: Requirements 3.5, 3.6, 3.7**
 *
 * Entries are generated consistently with the line grammar by
 * generating raw LINES (manual requirement lines with optional version
 * pins and comments, blank lines, full-line comments, and derived
 * marker lines with or without the verify suffix) and parsing them —
 * so every generated entry is one `parseRequirements` itself could
 * produce.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  DERIVED_MARKER,
  parseRequirements,
  renderRequirements,
  type RequirementsEntry,
} from './importAnalyzer';

// --------------------------------------------------------------------------
// Raw-line generators (each yields a single line, never containing '\n')
// --------------------------------------------------------------------------

/** Distribution name with case and separator variation. */
const distributionNameArb: fc.Arbitrary<string> = fc
  .record({
    base: fc.constantFrom(
      'numpy',
      'opencv-python-headless',
      'requests',
      'my-lib',
      'foo.bar',
      'pkg2',
      'scikit-learn'
    ),
    separator: fc.constantFrom('-', '_', '.'),
    upper: fc.boolean(),
  })
  .map(({ base, separator, upper }) => {
    const renamed = base.replace(/[-_.]/g, separator);
    return upper ? renamed.toUpperCase() : renamed;
  });

/** Optional version pin: `==1.24`, `>=2.0`, … */
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

/** Derived marker line, with or without the needs-review verify suffix. */
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
  { weight: 2, arbitrary: derivedLineArb }
);

/**
 * Entry-list generator: raw lines parsed into RequirementsEntry
 * values, so the entries are always consistent with the line grammar.
 */
const entriesArb: fc.Arbitrary<RequirementsEntry[]> = fc
  .array(lineArb, { minLength: 0, maxLength: 12 })
  .map((lines) => parseRequirements(lines.join('\n')));

// --------------------------------------------------------------------------
// Property
// --------------------------------------------------------------------------

describe('Property 8: Requirements text round trip', () => {
  it('parseRequirements(renderRequirements(entries)) preserves raw lines, derived flags, and needs-review flags', () => {
    fc.assert(
      fc.property(entriesArb, (entries) => {
        const roundTripped = parseRequirements(renderRequirements(entries));

        expect(roundTripped).toHaveLength(entries.length);

        // Raw lines round-trip verbatim (3.5: manual entries retained
        // unchanged; 3.6: the displayed/editable list is exactly the
        // stored text).
        expect(roundTripped.map((entry) => entry.raw)).toEqual(entries.map((entry) => entry.raw));

        // Derived flags survive the round trip (3.5: derived-vs-manual
        // distinction is stable across render/parse cycles).
        expect(roundTripped.map((entry) => entry.derived)).toEqual(
          entries.map((entry) => entry.derived)
        );

        // Needs-review flags survive the round trip (3.7: the
        // review indication is recoverable from the rendered text).
        expect(roundTripped.map((entry) => entry.needsReview)).toEqual(
          entries.map((entry) => entry.needsReview)
        );
      }),
      { numRuns: 100 }
    );
  });
});
