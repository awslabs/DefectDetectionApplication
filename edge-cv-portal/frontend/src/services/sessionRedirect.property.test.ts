/**
 * Property-based tests for `services/sessionRedirect.ts`
 * (spec: portal-session-expiry-return-to-page, task 2.2):
 *
 * - **Feature: portal-session-expiry-return-to-page, Property 1: Only safe
 *   internal paths are ever returned** (Validates: Requirements 3.1, 3.2,
 *   3.3, 3.4, 2.4)
 * - **Feature: portal-session-expiry-return-to-page, Property 2:
 *   Save/consume round-trips any safe location exactly once** (Validates:
 *   Requirements 1.1, 1.5, 1.6, 2.4)
 *
 * Runs against jsdom's `sessionStorage` (the vitest environment), cleared
 * inside every property run so the 100 iterations stay independent. Both
 * properties exercise the module through its public surface only; the
 * navigation seam is not involved here (design Decision 4 — that is task 5.3's
 * ground).
 *
 * The expectation oracle (`structurallySafe` below) is written as an
 * independent, regex-first formulation of design Decision 3 rather than by
 * calling the module, so an over-permissive *or* over-strict validator fails
 * the property. Values that are unsafe (or safe) by construction additionally
 * carry that classification with them, which pins the direction of each
 * adversarial family regardless of the oracle.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import {
  MAX_PATH_LENGTH,
  RETURN_TO_KEY,
  isSafeInternalPath,
  saveAttemptedLocation,
  takeAttemptedLocation,
} from './sessionRedirect';

// ------------------------------------------------------------ oracle

/** Control characters, spelled out independently of the module. */
const CONTROL = /[\u0000-\u001f\u007f]/;

/**
 * One leading `/` that is not followed by `/` or `\`, and no control
 * characters anywhere — the shape half of design Decision 3.
 */
const SAFE_SHAPE = /^\/(?![/\\])[^\u0000-\u001f\u007f]*$/;

/**
 * Independent expectation for "this string may be navigated to": a
 * same-origin relative path, within the documented length bound, that is not
 * the login page (Requirements 3.1, 3.3, 1.4).
 */
function structurallySafe(raw: string): boolean {
  if (raw.length === 0 || raw.length > MAX_PATH_LENGTH) return false;
  if (CONTROL.test(raw)) return false;
  if (!SAFE_SHAPE.test(raw)) return false;

  const pathPortion = raw.split(/[?#]/, 1)[0];
  const trimmed = pathPortion.replace(/\/+$/, '').toLowerCase() || '/';
  return trimmed !== '/login' && !trimmed.startsWith('/login/');
}

/** What the store currently holds, read directly through the fixed key. */
function planted(): string | null {
  return window.sessionStorage.getItem(RETURN_TO_KEY);
}

// ------------------------------------------------------- shared generators

/**
 * Characters allowed anywhere inside a remembered path: alphanumerics, the
 * unreserved and sub-delimiter punctuation real portal URLs carry (ids, `%`
 * escapes, `=`, `&`), a literal space, quotes/angle brackets (nothing is ever
 * interpolated into HTML, so they must survive verbatim), and non-ASCII
 * graphemes including a non-BMP emoji.
 */
const PATH_CHARS = [
  'a', 'b', 'z', 'A', 'Q', 'Z',
  '0', '5', '9',
  '-', '_', '.', '~', '%', '+', ',', ';', '=', '&', ':', '@', "'", '"', '<', '>', '(', ')', '*', '!', '$',
  ' ',
  'ü', '日', '🙂',
];

/** As above plus a backslash, legal in every position except index 1. */
const INNER_PATH_CHARS = [...PATH_CHARS, '\\'];

const firstSegmentArb: fc.Arbitrary<string> = fc.string({
  unit: fc.constantFrom(...PATH_CHARS),
  minLength: 1,
  maxLength: 12,
});

const innerSegmentArb: fc.Arbitrary<string> = fc.string({
  unit: fc.constantFrom(...INNER_PATH_CHARS),
  minLength: 1,
  maxLength: 12,
});

/**
 * A pathname that is safe by construction: exactly one leading `/`, a
 * non-empty first segment whose first character is neither `/` nor `\`, no
 * control characters, comfortably under the length bound, and never the login
 * page. `/` itself (the site root) is included.
 */
const safePathnameArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 1, arbitrary: fc.constant('/') },
  {
    weight: 9,
    arbitrary: fc
      .tuple(firstSegmentArb, fc.array(innerSegmentArb, { maxLength: 4 }), fc.boolean())
      // The first segment is drawn without a backslash, so index 1 of the
      // result is never `\` (the one position where it would be unsafe).
      .map(([first, rest, trailingSlash]) => `/${[first, ...rest].join('/')}${trailingSlash ? '/' : ''}`)
      // The login page is deliberately never a remembered location
      // (Requirement 1.4); it is Property 1's business, not Property 2's.
      .filter((path) => !/^\/login(\/|$)/i.test(path)),
  }
);

/** A query string: absent, bare `?`, or realistic `key=value` pairs. */
const safeSearchArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 4, arbitrary: fc.constant('') },
  { weight: 1, arbitrary: fc.constant('?') },
  {
    weight: 5,
    arbitrary: fc
      .array(fc.tuple(firstSegmentArb, innerSegmentArb), { minLength: 1, maxLength: 3 })
      .map((pairs) => `?${pairs.map(([k, v]) => `${k}=${v}`).join('&')}`),
  }
);

/** A fragment: absent, bare `#`, or a tab/anchor name. */
const safeHashArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 4, arbitrary: fc.constant('') },
  { weight: 1, arbitrary: fc.constant('#') },
  { weight: 5, arbitrary: innerSegmentArb.map((frag) => `#${frag}`) }
);

/** A location-like triple that concatenates to a Safe_Internal_Path. */
const safeLocationArb = fc.record({
  pathname: safePathnameArb,
  search: safeSearchArb,
  hash: safeHashArb,
});

// ------------------------------------------------- Property 1 generators

/** A candidate for the store, plus how it is classified by construction. */
interface Candidate {
  raw: string;
  /** `true` when the value must never be returned, `false` when it must be. */
  expectUnsafe: boolean | null;
  family: string;
}

const hostArb: fc.Arbitrary<string> = fc.constantFrom(
  'evil.com',
  'evil.com/steal?t=1',
  'attacker.example:8443/x',
  'localhost:5173/dashboard',
  'user@evil.com/dashboard'
);

/** `//evil.com` — protocol-relative, the classic open redirect (3.1). */
const protocolRelativeArb: fc.Arbitrary<Candidate> = hostArb.map((host) => ({
  raw: `//${host}`,
  expectUnsafe: true,
  family: 'protocol-relative',
}));

/** `/\evil.com` — the backslash spelling browsers normalise to `//` (3.1). */
const backslashRelativeArb: fc.Arbitrary<Candidate> = hostArb.map((host) => ({
  raw: `/\\${host}`,
  expectUnsafe: true,
  family: 'backslash-relative',
}));

/** Scheme-bearing candidates, mixed case (3.1). */
const schemeArb: fc.Arbitrary<Candidate> = fc
  .tuple(
    fc.constantFrom(
      'http://',
      'https://',
      'HTTPS://',
      'javascript:',
      'JavaScript:',
      'data:',
      'vbscript:',
      'file:///',
      'mailto:'
    ),
    fc.oneof(hostArb, fc.constantFrom('alert(1)', 'text/html;base64,PHNjcmlwdD4=', ''))
  )
  .map(([scheme, rest]) => ({
    raw: `${scheme}${rest}`,
    expectUnsafe: true,
    family: 'scheme-bearing',
  }));

/** A safe-looking path with a control character spliced in (3.1). */
const controlCharacterArb: fc.Arbitrary<Candidate> = fc
  .tuple(
    fc.constantFrom('\u0000', '\u0009', '\u000a', '\u000d', '\u001b', '\u007f'),
    fc.constantFrom('/dashboard', '/devices/dev-1?tab=cameras', '/workflows/builder/abc'),
    fc.nat({ max: 4 })
  )
  .map(([ctrl, path, at]) => {
    const cut = Math.min(at + 1, path.length);
    return {
      raw: `${path.slice(0, cut)}${ctrl}${path.slice(cut)}`,
      expectUnsafe: true,
      family: 'control-character',
    };
  });

/** A safe path padded past the documented bound (3.3). */
const overLengthArb: fc.Arbitrary<Candidate> = fc
  .integer({ min: 1, max: 512 })
  .map((extra) => ({
    raw: `/devices/${'a'.repeat(MAX_PATH_LENGTH + extra)}`,
    expectUnsafe: true,
    family: 'over-length',
  }));

/** Exactly at the bound — must still be accepted (3.3 is a bound, not a ban). */
const atLengthLimitArb: fc.Arbitrary<Candidate> = fc.constant({
  raw: `/d/${'a'.repeat(MAX_PATH_LENGTH - 3)}`,
  expectUnsafe: false,
  family: 'at-length-limit',
});

/** `/login` with and without query/hash/case/trailing slash (1.4, 2.5). */
const loginArb: fc.Arbitrary<Candidate> = fc
  .constantFrom(
    '/login',
    '/login/',
    '/login?next=/dashboard',
    '/login#form',
    '/LOGIN',
    '/Login?x=1',
    '/login//',
    '/login/callback'
  )
  .map((raw) => ({ raw, expectUnsafe: true, family: 'login-page' }));

/** Not a rooted path at all: relative, empty, or leading whitespace (3.1). */
const notRootedArb: fc.Arbitrary<Candidate> = fc
  .constantFrom('', 'dashboard', './dashboard', '../../etc/passwd', ' /dashboard', '\t/dashboard', '?tab=nodes', '#hash')
  .map((raw) => ({ raw, expectUnsafe: true, family: 'not-rooted' }));

/** Realistic deep portal locations — these must survive (3.2's other half). */
const safeCandidateArb: fc.Arbitrary<Candidate> = fc.oneof(
  safeLocationArb.map(({ pathname, search, hash }) => ({
    raw: `${pathname}${search}${hash}`,
    expectUnsafe: false,
    family: 'safe-generated',
  })),
  fc
    .constantFrom(
      '/dashboard',
      '/workflows/builder/abc?tab=nodes',
      '/devices/dev-1?tab=cameras#latest',
      '/labeler',
      '/admin/fleet',
      '/models/model-1/versions/2?compare=1',
      '/a\\b',
      '/loginish',
      '/user/login'
    )
    .map((raw) => ({ raw, expectUnsafe: false, family: 'safe-known' }))
);

/** Wholly arbitrary text, classified by the oracle alone. */
const arbitraryTextArb: fc.Arbitrary<Candidate> = fc
  .oneof(fc.string(), fc.string({ unit: 'grapheme' }), fc.string({ minLength: 0, maxLength: 60 }))
  .map((raw) => ({ raw, expectUnsafe: null, family: 'arbitrary' }));

const candidateArb: fc.Arbitrary<Candidate> = fc.oneof(
  protocolRelativeArb,
  backslashRelativeArb,
  schemeArb,
  controlCharacterArb,
  overLengthArb,
  atLengthLimitArb,
  loginArb,
  notRootedArb,
  safeCandidateArb,
  arbitraryTextArb
);

// -------------------------------------------------------------- properties

/**
 * **Feature: portal-session-expiry-return-to-page, Property 1: Only safe
 * internal paths are ever returned**
 *
 * For any candidate string planted directly into the store — including
 * `//host`, `/\host`, `http://`, `https://`, `javascript:`, `data:`, control
 * characters, over-length strings, and `/login` with and without query —
 * `takeAttemptedLocation()` SHALL return either `null` or a string beginning
 * with exactly one `/` that is not the login page, and the store SHALL be
 * empty afterwards. The same holds for a candidate offered as a location to
 * `saveAttemptedLocation`, so no route into the store can smuggle an unsafe
 * value out of it. `isSafeInternalPath` SHALL be pure: same answer every
 * call, no storage side effects.
 *
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 2.4**
 */
describe('Feature: portal-session-expiry-return-to-page, Property 1: Only safe internal paths are ever returned', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('never returns anything but null or a same-origin non-login path, and always empties the store', () => {
    fc.assert(
      fc.property(candidateArb, ({ raw, expectUnsafe, family }) => {
        // Independent storage per run.
        window.sessionStorage.clear();

        const expectedSafe = structurallySafe(raw);
        if (expectUnsafe !== null) {
          // The adversarial families pin the direction independently of the
          // oracle, so a hole in the oracle cannot hide a hole in the module.
          expect(expectedSafe, `${family}: ${JSON.stringify(raw)}`).toBe(!expectUnsafe);
        }

        // --- route A: a value planted straight into sessionStorage. Validation
        // happens on consumption (design Decision 3), so this is the route an
        // attacker with storage access would use.
        window.sessionStorage.setItem(RETURN_TO_KEY, raw);

        // The validator is pure: stable answer, and it does not read or write
        // the store (Requirement 3.4).
        const verdict = isSafeInternalPath(raw);
        expect(isSafeInternalPath(raw)).toBe(verdict);
        expect(verdict).toBe(expectedSafe);
        expect(planted()).toBe(raw);

        const taken = takeAttemptedLocation();

        // Consumed either way: an unsafe planted value is discarded, not left
        // behind to be retried (Requirement 2.4).
        expect(planted()).toBeNull();
        expect(taken).toBe(expectedSafe ? raw : null);

        if (taken !== null) {
          expect(taken[0]).toBe('/');
          expect(taken[1] === '/' || taken[1] === '\\').toBe(false);
          expect(taken.length).toBeLessThanOrEqual(MAX_PATH_LENGTH);
          expect(CONTROL.test(taken)).toBe(false);
          expect(taken.split(/[?#]/, 1)[0].replace(/\/+$/, '').toLowerCase()).not.toBe('/login');
        }

        // --- route B: the same candidate offered as a location to save. An
        // unsafe candidate is never recorded at all (Requirement 1.4).
        window.sessionStorage.clear();
        saveAttemptedLocation({ pathname: raw, search: '', hash: '' });
        expect(planted()).toBe(expectedSafe ? raw : null);
        expect(takeAttemptedLocation()).toBe(expectedSafe ? raw : null);
        expect(planted()).toBeNull();
      }),
      { numRuns: 100 }
    );
  });
});

/**
 * **Feature: portal-session-expiry-return-to-page, Property 2: Save/consume
 * round-trips any safe location exactly once**
 *
 * For any safe `pathname` / `search` / `hash` triple, one
 * `saveAttemptedLocation` followed by one `takeAttemptedLocation` SHALL return
 * the exact concatenation `pathname + search + hash` (Requirements 1.1, 1.5);
 * a second save before the consume SHALL NOT overwrite the first
 * (first-write-wins, Requirement 1.6); a second consume SHALL return `null`
 * (Requirement 2.4); and after the slot is freed a fresh save SHALL be
 * remembered, so "exactly once" is a per-value property, not a one-shot
 * module.
 *
 * **Validates: Requirements 1.1, 1.5, 1.6, 2.4**
 */
describe('Feature: portal-session-expiry-return-to-page, Property 2: Save/consume round-trips any safe location exactly once', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('one save then one consume returns pathname+search+hash verbatim, the second save loses, the second consume is null', () => {
    fc.assert(
      fc.property(safeLocationArb, safeLocationArb, (first, second) => {
        // Independent storage per run.
        window.sessionStorage.clear();

        const firstPath = `${first.pathname}${first.search}${first.hash}`;
        const secondPath = `${second.pathname}${second.search}${second.hash}`;

        // Both are Safe_Internal_Paths by construction — assert it, so an
        // over-strict validator fails here rather than silently shrinking the
        // set of recoverable pages.
        expect(structurallySafe(firstPath)).toBe(true);
        expect(isSafeInternalPath(firstPath)).toBe(true);
        expect(isSafeInternalPath(secondPath)).toBe(true);

        saveAttemptedLocation(first);
        expect(planted()).toBe(firstPath);

        // A single expiry commonly produces several 401s; the earliest wins
        // (design Decision 2, Requirement 1.6).
        saveAttemptedLocation(second);
        saveAttemptedLocation({ pathname: '/dashboard', search: '', hash: '' });
        expect(planted()).toBe(firstPath);

        // Consume once: exact concatenation, nothing normalised or dropped.
        expect(takeAttemptedLocation()).toBe(firstPath);
        expect(planted()).toBeNull();

        // Consume twice: nothing left to resurrect (Requirement 2.4).
        expect(takeAttemptedLocation()).toBeNull();
        expect(takeAttemptedLocation()).toBeNull();

        // The slot is reusable: the next expiry is remembered normally.
        saveAttemptedLocation(second);
        expect(takeAttemptedLocation()).toBe(secondPath);
        expect(takeAttemptedLocation()).toBeNull();
      }),
      { numRuns: 100 }
    );
  });
});
