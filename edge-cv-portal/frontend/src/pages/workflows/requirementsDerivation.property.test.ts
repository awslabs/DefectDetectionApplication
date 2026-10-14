/**
 * **Feature: custom-node-code-assist, Property 5: Requirements derivation**
 *
 * For any set of imported top-level module names, every element of
 * `deriveRequirements`: standard-library names, `dda_frames`, and relative
 * imports produce no entry; every name present in the Import_Mapping produces
 * exactly its mapped distribution with `needsReview: false` (in particular
 * `cv2` → `opencv-python-headless` and `numpy` → `numpy`); every other name
 * produces the name itself with `needsReview: true`; and no other entries
 * exist.
 *
 * **Validates: Requirements 3.2, 3.3, 3.7, 3.8**
 *
 * Dedup semantics: output entries are unique per distribution, and when a
 * mapped name and an unmapped name yield the same distribution (e.g. mapped
 * `PIL` → `Pillow` alongside an unmapped literal `Pillow` import) the mapped
 * occurrence wins, so the entry carries `needsReview: false`.
 *
 * Relative imports never reach `deriveRequirements` as names: the second
 * property drives module code containing relative imports through
 * `extractImports` and checks they contribute no entry.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  deriveRequirements,
  extractImports,
  IMPORT_MAPPING,
  STDLIB_MODULES,
} from './importAnalyzer';

const MAPPED_NAMES = Object.keys(IMPORT_MAPPING);
const STDLIB_NAMES = [...STDLIB_MODULES];

/**
 * Mapped distributions that are themselves importable identifiers and not
 * mapped import names — importing one of these as an unmapped module makes
 * its distribution collide with a mapped name's distribution, exercising the
 * mapped-occurrence-wins dedup rule.
 */
const COLLISION_NAMES = ['Pillow', 'PyYAML', 'beautifulsoup4', 'pyserial', 'pyusb', 'pyzmq'];

const hasMapping = (name: string): boolean =>
  Object.prototype.hasOwnProperty.call(IMPORT_MAPPING, name);

/** Lowercase Python identifier that is neither stdlib, mapped, nor dda_frames. */
const otherNameArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz_'),
    fc.string({
      unit: fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'),
      maxLength: 8,
    })
  )
  .map(([head, tail]) => head + tail)
  .filter(
    (name) => !STDLIB_MODULES.has(name) && !hasMapping(name) && name !== 'dda_frames'
  );

/**
 * A scenario of imported top-level names drawn from every category the
 * property distinguishes; `cv2` and `numpy` are included with independent
 * probability so the named examples are exercised often.
 */
const scenarioArb = fc.record({
  stdlib: fc.array(fc.constantFrom(...STDLIB_NAMES), { maxLength: 5 }),
  includeDdaFrames: fc.boolean(),
  mapped: fc.array(fc.constantFrom(...MAPPED_NAMES), { maxLength: 5 }),
  includeCv2: fc.boolean(),
  includeNumpy: fc.boolean(),
  collisions: fc.array(fc.constantFrom(...COLLISION_NAMES), { maxLength: 3 }),
  others: fc.array(otherNameArb, { maxLength: 5 }),
  reversed: fc.boolean(),
});

type Scenario = typeof scenarioArb extends fc.Arbitrary<infer T> ? T : never;

function buildNames(scenario: Scenario): string[] {
  const names = [
    ...scenario.stdlib,
    ...(scenario.includeDdaFrames ? ['dda_frames'] : []),
    ...scenario.mapped,
    ...(scenario.includeCv2 ? ['cv2'] : []),
    ...(scenario.includeNumpy ? ['numpy'] : []),
    ...scenario.collisions,
    ...scenario.others,
  ];
  // The mapped-wins dedup rule must hold regardless of occurrence order.
  return scenario.reversed ? names.reverse() : names;
}

describe('Property 5: Requirements derivation', () => {
  it('derives exactly the mapped/identity entries and drops stdlib and dda_frames', () => {
    fc.assert(
      fc.property(scenarioArb, (scenario) => {
        const names = buildNames(scenario);
        const derived = deriveRequirements(names);

        const byDistribution = new Map(derived.map((entry) => [entry.distribution, entry]));
        // Entries are deduped: one entry per distribution.
        expect(byDistribution.size).toBe(derived.length);

        const mappedInputs = names.filter(hasMapping);
        const mappedDistributions = new Set(mappedInputs.map((name) => IMPORT_MAPPING[name]));
        const otherInputs = names.filter(
          (name) => !STDLIB_MODULES.has(name) && name !== 'dda_frames' && !hasMapping(name)
        );

        // Standard-library names and dda_frames produce no entry (3.3).
        for (const name of names) {
          if (STDLIB_MODULES.has(name) || name === 'dda_frames') {
            expect(byDistribution.has(name)).toBe(false);
          }
        }

        // Mapped names produce exactly their mapped distribution with
        // needsReview: false (3.2, 3.8).
        for (const name of mappedInputs) {
          expect(byDistribution.get(IMPORT_MAPPING[name])).toEqual({
            distribution: IMPORT_MAPPING[name],
            needsReview: false,
          });
        }

        // In particular cv2 → opencv-python-headless and numpy → numpy (3.2).
        if (names.includes('cv2')) {
          expect(byDistribution.get('opencv-python-headless')).toEqual({
            distribution: 'opencv-python-headless',
            needsReview: false,
          });
        }
        if (names.includes('numpy')) {
          expect(byDistribution.get('numpy')).toEqual({
            distribution: 'numpy',
            needsReview: false,
          });
        }

        // Every other name produces itself; needsReview: true unless a mapped
        // occurrence produced the same distribution, in which case the mapped
        // occurrence wins (3.7 + dedup).
        for (const name of otherInputs) {
          expect(byDistribution.get(name)).toEqual({
            distribution: name,
            needsReview: !mappedDistributions.has(name),
          });
        }

        // No other entries exist.
        const expectedDistributions = new Set([...mappedDistributions, ...otherInputs]);
        for (const entry of derived) {
          expect(expectedDistributions.has(entry.distribution)).toBe(true);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('relative imports contribute no entry through the extraction pipeline', () => {
    const relativeModuleArb = fc.oneof(otherNameArb, fc.constantFrom(...MAPPED_NAMES));
    const relativeLineArb = fc
      .record({
        module: relativeModuleArb,
        dots: fc.constantFrom('.', '..'),
        bare: fc.boolean(),
      })
      .map(({ module, dots, bare }) =>
        // `from . import mod` and `from .mod import x` are both relative.
        bare ? `from ${dots} import ${module}` : `from ${dots}${module} import x`
      );

    const absoluteNameArb = fc.oneof(
      otherNameArb,
      fc.constantFrom(...MAPPED_NAMES),
      fc.constantFrom(...STDLIB_NAMES),
      fc.constant('dda_frames')
    );

    fc.assert(
      fc.property(
        fc.array(absoluteNameArb, { maxLength: 5 }),
        fc.array(relativeLineArb, { minLength: 1, maxLength: 5 }),
        (names, relativeLines) => {
          const code = [...names.map((name) => `import ${name}`), ...relativeLines].join('\n');
          const scan = extractImports(code);
          expect(scan.ok).toBe(true);
          if (scan.ok) {
            // The relative imports change nothing: derivation equals the
            // derivation of the absolute names alone (3.3).
            expect(deriveRequirements(scan.imports)).toEqual(deriveRequirements(names));
          }
        }
      ),
      { numRuns: 100 }
    );
  });
});
