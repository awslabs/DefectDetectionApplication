/**
 * Property-based test for `pages/Login.tsx`'s post-sign-in destination
 * (spec: portal-session-expiry-return-to-page, task 3.4):
 *
 * - **Feature: portal-session-expiry-return-to-page, Property 3: The
 *   post-login destination is the remembered location when one exists, else
 *   the role's landing page** (Validates: Requirements 2.1, 2.3, 3.2)
 *
 * The property drives the real `Login` component through the real
 * `sessionRedirect` store: for every (remembered value, role) pair it renders
 * an authenticated `Login` and observes the single destination the page hands
 * to `useNavigate`. Nothing about a real browser navigation is asserted — the
 * `useNavigate` spy is the observable.
 *
 * The expectation oracle (`structurallySafe`) is an independent, regex-first
 * restatement of design Decision 3 rather than a call into the module, and the
 * adversarial generators additionally carry their own safe/unsafe
 * classification, so neither an over-permissive nor an over-strict validator
 * can pass, and a hole in the oracle cannot hide a hole in the module.
 *
 * Validates: Requirements 2.1, 2.3, 3.2
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';
import type { UserRole } from '../types';

const { navigateMock, useAuthMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
}));

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

import Login from './Login';
import { MAX_PATH_LENGTH, RETURN_TO_KEY } from '../services/sessionRedirect';

// ------------------------------------------------------------------ oracle

/** Control characters, spelled out independently of the module. */
const CONTROL = /[\u0000-\u001f\u007f]/;

/** One leading `/` not followed by `/` or `\`, and no control characters. */
const SAFE_SHAPE = /^\/(?![/\\])[^\u0000-\u001f\u007f]*$/;

/** Independent expectation for "this value may be navigated to". */
function structurallySafe(raw: string): boolean {
  if (raw.length === 0 || raw.length > MAX_PATH_LENGTH) return false;
  if (CONTROL.test(raw)) return false;
  if (!SAFE_SHAPE.test(raw)) return false;

  const pathPortion = raw.split(/[?#]/, 1)[0];
  const trimmed = pathPortion.replace(/\/+$/, '').toLowerCase() || '/';
  return trimmed !== '/login' && !trimmed.startsWith('/login/');
}

/** Today's landing page for a role — the fallback this feature must preserve. */
function landingFor(role: UserRole | undefined): string {
  return role === 'DataLabeler' ? '/labeler' : '/dashboard';
}

// -------------------------------------------------------------- generators

/** A remembered value offered to the store, or `null` for "nothing saved". */
interface Remembered {
  raw: string | null;
  /** `true` when it must be discarded, `false` when it must be honoured. */
  expectUnsafe: boolean | null;
  family: string;
}

const segmentArb: fc.Arbitrary<string> = fc.string({
  unit: fc.constantFrom(
    'a', 'b', 'z', 'Q', 'Z', '0', '7', '-', '_', '.', '~', '%', '+', ',', ';', '=', '&', ':', '@', 'ü'
  ),
  minLength: 1,
  maxLength: 10,
});

/** A location that is a Safe_Internal_Path by construction. */
const safeRememberedArb: fc.Arbitrary<Remembered> = fc
  .tuple(
    fc.oneof(
      fc.constant('/'),
      fc
        .array(segmentArb, { minLength: 1, maxLength: 4 })
        .map((parts) => `/${parts.join('/')}`)
        .filter((path) => !/^\/login(\/|$)/i.test(path))
    ),
    fc.oneof(
      fc.constant(''),
      fc
        .array(fc.tuple(segmentArb, segmentArb), { minLength: 1, maxLength: 3 })
        .map((pairs) => `?${pairs.map(([k, v]) => `${k}=${v}`).join('&')}`)
    ),
    fc.oneof(fc.constant(''), segmentArb.map((frag) => `#${frag}`))
  )
  .map(([pathname, search, hash]) => ({
    raw: `${pathname}${search}${hash}`,
    expectUnsafe: false,
    family: 'safe-generated',
  }));

/** Realistic deep portal locations that must survive verbatim. */
const knownSafeArb: fc.Arbitrary<Remembered> = fc
  .constantFrom(
    '/dashboard',
    '/workflows/builder/abc?tab=nodes',
    '/devices/dev-1?tab=cameras#latest',
    '/labeler/tasks/task-9?item=3',
    '/admin/fleet',
    '/models/model-1/versions/2?compare=1',
    '/loginish',
    '/user/login',
    `/d/${'a'.repeat(MAX_PATH_LENGTH - 3)}`
  )
  .map((raw) => ({ raw, expectUnsafe: false, family: 'safe-known' }));

/** Values that must never be navigated to (Requirements 3.1, 3.3, 1.4). */
const unsafeArb: fc.Arbitrary<Remembered> = fc
  .oneof(
    fc
      .constantFrom('evil.example.com', 'evil.example.com/steal?t=1', 'attacker.test:8443/x')
      .chain((host) =>
        fc.constantFrom(
          `//${host}`,
          `/\\${host}`,
          `http://${host}`,
          `https://${host}`,
          `HTTPS://${host}`
        )
      ),
    fc.constantFrom(
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      'data:text/html;base64,PHNjcmlwdD4=',
      'vbscript:msgbox(1)',
      'file:///etc/passwd',
      'mailto:a@b.c'
    ),
    fc
      .constantFrom('\u0000', '\u0009', '\u000a', '\u000d', '\u001b', '\u007f')
      .map((ctrl) => `/devices/dev-1${ctrl}?tab=cameras`),
    fc.integer({ min: 1, max: 400 }).map((extra) => `/devices/${'a'.repeat(MAX_PATH_LENGTH + extra)}`),
    fc.constantFrom(
      '/login',
      '/login/',
      '/login?next=/dashboard',
      '/login#form',
      '/LOGIN',
      '/Login?x=1',
      '/login/callback'
    ),
    fc.constantFrom('', 'dashboard', './dashboard', '../../etc/passwd', ' /dashboard', '?tab=nodes', '#hash')
  )
  .map((raw) => ({ raw, expectUnsafe: true, family: 'unsafe' }));

/** Nothing was ever remembered — the plain fresh sign-in. */
const nothingRememberedArb: fc.Arbitrary<Remembered> = fc.constant({
  raw: null,
  expectUnsafe: null,
  family: 'nothing-remembered',
});

/** Wholly arbitrary text, classified by the oracle alone. */
const arbitraryTextArb: fc.Arbitrary<Remembered> = fc
  .oneof(fc.string(), fc.string({ unit: 'grapheme' }))
  .map((raw) => ({ raw, expectUnsafe: null, family: 'arbitrary' }));

const rememberedArb: fc.Arbitrary<Remembered> = fc.oneof(
  { weight: 3, arbitrary: safeRememberedArb },
  { weight: 2, arbitrary: knownSafeArb },
  { weight: 3, arbitrary: unsafeArb },
  { weight: 2, arbitrary: nothingRememberedArb },
  { weight: 1, arbitrary: arbitraryTextArb }
);

/** Every role the portal has, plus "role not resolved yet" (`undefined`). */
const roleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom<
  Array<UserRole | undefined>
>('PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer', 'DataLabeler', undefined);

// --------------------------------------------------------------- harness

function setAuth(role: UserRole | undefined) {
  useAuthMock.mockReturnValue({
    user: role
      ? {
          user_id: 'user-1',
          email: 'user@example.com',
          username: 'user',
          role,
          is_super_user: false,
        }
      : null,
    isAuthenticated: true,
    isLoading: false,
    needsNewPassword: false,
    login: vi.fn(),
    completeNewPassword: vi.fn(),
    changePassword: vi.fn(),
    forgotPassword: vi.fn(),
    forgotPasswordSubmit: vi.fn(),
    logout: vi.fn(),
    error: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
});

// -------------------------------------------------------------- property

/**
 * **Feature: portal-session-expiry-return-to-page, Property 3: The post-login
 * destination is the remembered location when one exists, else the role's
 * landing page**
 *
 * For any (remembered value, role) pair, the destination `Login` navigates to
 * on a successful sign-in SHALL be the remembered location when it is a
 * Safe_Internal_Path (Requirement 2.1), and otherwise `/labeler` for a
 * `DataLabeler` and `/dashboard` for every other role (Requirements 2.3, 3.2).
 * Exactly one navigation SHALL happen, and the store SHALL be empty afterwards
 * whether the value was honoured or discarded.
 *
 * **Validates: Requirements 2.1, 2.3, 3.2**
 */
describe('Feature: portal-session-expiry-return-to-page, Property 3: The post-login destination is the remembered location when one exists, else the role\u2019s landing page', () => {
  it('navigates exactly once, to the remembered safe location or the role landing page', () => {
    fc.assert(
      fc.property(rememberedArb, roleArb, ({ raw, expectUnsafe, family }, role) => {
        // Independent store, mocks and DOM per run.
        window.sessionStorage.clear();
        navigateMock.mockClear();

        const landing = landingFor(role);
        const safe = raw !== null && structurallySafe(raw);

        if (raw !== null && expectUnsafe !== null) {
          // The adversarial families pin the direction independently of the
          // oracle.
          expect(safe, `${family}: ${JSON.stringify(raw)}`).toBe(!expectUnsafe);
        }

        if (raw !== null) {
          window.sessionStorage.setItem(RETURN_TO_KEY, raw);
        }

        setAuth(role);
        try {
          render(<Login />);

          // Exactly one destination, resolved on mount as it is today
          // (Requirement 2.6 is unchanged by this feature).
          expect(navigateMock).toHaveBeenCalledTimes(1);
          expect(navigateMock).toHaveBeenCalledWith(safe ? raw : landing);

          // Honoured or discarded, the value is consumed (Requirement 2.4).
          expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();

          // The landing fallback keeps its historical values.
          if (!safe) {
            expect(navigateMock).toHaveBeenCalledWith(role === 'DataLabeler' ? '/labeler' : '/dashboard');
          }
        } finally {
          cleanup();
        }
      }),
      { numRuns: 100 }
    );
  });
});
