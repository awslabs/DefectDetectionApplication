/**
 * Property-based tests for the pure context helpers of
 * portal-deploy-flag-hardening (`lib/context-helpers.ts`).
 *
 * The suite imports the two helpers directly — ZERO CDK imports, zero synth
 * cost — which is what makes fast-check at `{ numRuns: 100 }` honest here: a
 * ComputeStack synth costs minutes (Lambda/layer asset staging), so the
 * template consequences are pinned by example-level CDK assertions in the
 * rebaselined/example suites instead, per the design's Testing Strategy.
 *
 * Property 1: The Flag_Resolver deploys for every context value except an
 *             Explicit_False.
 *             Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5
 * Property 2: The Domain_Normalizer recovers the bare domain from any
 *             spelling, idempotently, never emitting a scheme or trailing
 *             slash.
 *             Validates: Requirements 3.1, 3.3, 3.4, 3.6, 3.7
 */

import * as fc from 'fast-check';
import {
  groundedSamWorkerEnabled,
  normalizeCloudFrontDomain,
} from '../lib/context-helpers';

const RUNS = { numRuns: 100 };

/** Surrounding-whitespace padding (0–3 characters, all removed by trim). */
const whitespaceArb = fc
  .array(fc.constantFrom(' ', '\t', '\n', '\r'), { maxLength: 3 })
  .map((chars) => chars.join(''));

describe('Feature: portal-deploy-flag-hardening, Property 1: The Flag_Resolver deploys for every context value except an Explicit_False', () => {
  /**
   * The Explicit_False predicate, stated independently of the implementation
   * (requirements Glossary): boolean `false`, or a string equal to 'false'
   * case-insensitively after trimming. This is the oracle: the resolver must
   * return deploy (`true`) exactly when the value is NOT Explicit_False.
   */
  const explicitFalse = (value: unknown): boolean =>
    value === false ||
    (typeof value === 'string' && value.trim().toLowerCase() === 'false');

  // Casing/whitespace-padding transforms of 'true'/'false': per-character
  // case flips ('false' is 5 chars; extra flips are unused for 'true')
  // plus generated surrounding whitespace.
  const casedPaddedTrueFalseArb = fc
    .tuple(
      fc.constantFrom('true', 'false'),
      fc.array(fc.boolean(), { minLength: 5, maxLength: 5 }),
      whitespaceArb,
      whitespaceArb,
    )
    .map(
      ([word, flips, left, right]) =>
        left +
        word
          .split('')
          .map((ch, i) => (flips[i] ? ch.toUpperCase() : ch))
          .join('') +
        right,
    );

  // The full mixed input space: the boundary constants, deliberate
  // casing/padding spellings of 'true'/'false', arbitrary strings
  // (unrecognized values like '0', 'no', 'off', typos), numbers, objects.
  const flagContextValueArb: fc.Arbitrary<unknown> = fc.oneof(
    fc.constantFrom<unknown>(undefined, true, false, 'true', 'false'),
    casedPaddedTrueFalseArb,
    fc.string(),
    fc.integer(),
    fc.object(),
  );

  it('returns deploy for every context value except an Explicit_False (Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5)', () => {
    fc.assert(
      fc.property(flagContextValueArb, (value) => {
        expect(groundedSamWorkerEnabled(value)).toBe(!explicitFalse(value));
      }),
      RUNS,
    );
  });
});

describe('Feature: portal-deploy-flag-hardening, Property 2: The Domain_Normalizer recovers the bare domain from any spelling, idempotently, never emitting a scheme or trailing slash', () => {
  // Bare domains: alphanumeric/dot/hyphen, non-empty. The charset contains
  // no ':', no '/', and no whitespace, so a generated bare domain can never
  // itself carry a leading scheme match, a trailing slash, or surrounding
  // whitespace — the generator-level constraints the property requires.
  // Exact recovery (=== bare) therefore also proves the output never
  // carries a scheme or a trailing slash.
  const domainChars =
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-';
  const bareDomainArb = fc
    .array(fc.constantFrom(...domainChars.split('')), {
      minLength: 1,
      maxLength: 40,
    })
    .map((chars) => chars.join(''));

  // 'http://' / 'https://' with per-character case flips ('https://' is
  // 8 chars; ':' and '/' are case-invariant, flipping them is a no-op).
  const schemeArb = fc
    .tuple(
      fc.constantFrom('http://', 'https://'),
      fc.array(fc.boolean(), { minLength: 8, maxLength: 8 }),
    )
    .map(([scheme, flips]) =>
      scheme
        .split('')
        .map((ch, i) => (flips[i] ? ch.toUpperCase() : ch.toLowerCase()))
        .join(''),
    );

  // Zero to three trailing slashes.
  const trailingSlashesArb = fc.nat({ max: 3 }).map((n) => '/'.repeat(n));

  // Degenerate inputs that must map to undefined (today's absent-context
  // behavior): non-strings, and strings that normalize to empty — empty,
  // whitespace-only, and scheme-only (optionally slashed and padded).
  const degenerateArb: fc.Arbitrary<unknown> = fc.oneof(
    fc.constantFrom<unknown>(undefined, null, true, false),
    fc.integer(),
    fc.double(),
    fc.object(),
    fc
      .array(fc.constantFrom(' ', '\t', '\n', '\r'), { maxLength: 4 })
      .map((chars) => chars.join('')),
    fc
      .tuple(schemeArb, trailingSlashesArb, whitespaceArb, whitespaceArb)
      .map(([scheme, slashes, left, right]) => left + scheme + slashes + right),
  );

  it('recovers the bare domain from any decorated spelling, idempotently, and returns undefined for non-string/empty/whitespace-only/scheme-only inputs (Validates: Requirements 3.1, 3.3, 3.4, 3.6, 3.7)', () => {
    // Exact recovery + idempotence across every decoration: optional scheme
    // in any casing × 0–3 trailing slashes × surrounding whitespace.
    fc.assert(
      fc.property(
        bareDomainArb,
        fc.option(schemeArb, { nil: undefined }),
        trailingSlashesArb,
        whitespaceArb,
        whitespaceArb,
        (bare, scheme, slashes, left, right) => {
          const decorated = left + (scheme ?? '') + bare + slashes + right;
          const once = normalizeCloudFrontDomain(decorated);
          // Exact recovery: every character of the bare domain preserved —
          // hence no scheme prefix and no trailing slash in the output.
          expect(once).toBe(bare);
          // Idempotence on string results: normalizing the normalized
          // output changes nothing.
          expect(normalizeCloudFrontDomain(once!)).toBe(once);
        },
      ),
      RUNS,
    );

    // The undefined domain: non-strings and inputs empty after
    // normalization behave as absent context.
    fc.assert(
      fc.property(degenerateArb, (value) => {
        expect(normalizeCloudFrontDomain(value)).toBeUndefined();
      }),
      RUNS,
    );
  });
});
