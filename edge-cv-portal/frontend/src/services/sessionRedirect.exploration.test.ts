/**
 * Exploration test — Feature: portal-session-expiry-return-to-page (task 1.1).
 *
 * Reproduces the reported defect end-to-end, from the two ends of the broken
 * hand-off, and MUST FAIL on unfixed code:
 *
 *  1. The dominant Session_Exit (`services/api.ts` 401 handler) throws the
 *     user out to `/login` without recording where they were, so nothing is
 *     recoverable afterwards. (bugfix.md Requirement 1.1)
 *  2. `pages/Login.tsx` navigates to the hard-coded `postLoginLanding`
 *     constant, so even a remembered location is ignored. (Requirement 2.1)
 *
 * The recovery channel asserted here is `sessionStorage['dda.portal.returnTo']`
 * — the single key design Decision 1 fixes for the shared
 * `services/sessionRedirect` module — because the api.ts exit is a full
 * document navigation that router `location.state` cannot survive.
 *
 * jsdom cannot perform a real `window.location.href` navigation, so
 * `window.location` is replaced with a plain stub object for the duration of
 * each case (design Decision 4: the navigation itself is never what tests
 * observe).
 *
 * Validates: Requirements 1.1, 2.1
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';

/** The deep page the user was on when the session ended. */
const DEEP_LOCATION = '/workflows/builder/abc?tab=nodes';
/** Storage key for the Attempted_Location (design Decision 1). */
const RETURN_TO_KEY = 'dda.portal.returnTo';
/** Today's hard-coded post-login destination for a non-DataLabeler role. */
const DEFAULT_LANDING = '/dashboard';

const { navigateMock, useAuthMock, fetchAuthSessionMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  fetchAuthSessionMock: vi.fn(),
}));

// Keep the real router, but observe where `Login` decides to send the user.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));

// `Login` reads auth state only through this hook.
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// The api client does not import Amplify today; once the Silent_Refresh step
// lands (task 5.1) a non-refreshable session must still reach the redirect,
// which is the case this test pins.
vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: fetchAuthSessionMock,
}));

import { apiService } from './api';
import Login from '../pages/Login';

/** A 401 response body in the portal's simple error envelope. */
function unauthorizedResponse(): Response {
  return {
    ok: false,
    status: 401,
    json: async () => ({ error: 'Unauthorized' }),
  } as unknown as Response;
}

interface LocationStub {
  href: string;
  pathname: string;
  search: string;
  hash: string;
}

let originalLocation: PropertyDescriptor | undefined;

/** Replace `window.location` with a writable stub at `path`. */
function stubLocation(path: string): LocationStub {
  const url = new URL(`http://localhost${path}`);
  const stub: LocationStub = {
    href: url.href,
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
  };
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: stub,
  });
  return stub;
}

function authState(overrides: { isAuthenticated?: boolean; role?: string } = {}) {
  const { isAuthenticated = false, role = 'DataScientist' } = overrides;
  return {
    user: {
      user_id: 'user-1',
      email: 'user@example.com',
      username: 'user',
      role,
      is_super_user: false,
    },
    isAuthenticated,
    isLoading: false,
    needsNewPassword: false,
    login: vi.fn(),
    completeNewPassword: vi.fn(),
    changePassword: vi.fn(),
    forgotPassword: vi.fn(),
    forgotPasswordSubmit: vi.fn(),
    logout: vi.fn(),
    error: null,
  };
}

beforeEach(() => {
  originalLocation = Object.getOwnPropertyDescriptor(window, 'location');
  vi.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  useAuthMock.mockReturnValue(authState());
  fetchAuthSessionMock.mockRejectedValue(new Error('session cannot be refreshed'));
});

afterEach(() => {
  if (originalLocation) {
    Object.defineProperty(window, 'location', originalLocation);
  }
  vi.unstubAllGlobals();
});

describe('portal session expiry — return to page (exploration)', () => {
  it('records the attempted deep location when a 401 sends the user to /login', async () => {
    const location = stubLocation(DEEP_LOCATION);
    localStorage.setItem('idToken', 'stale-token');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(unauthorizedResponse()));

    // Any authenticated call is enough; the 401 handler is shared.
    await expect(apiService.getCurrentUser()).rejects.toThrow();

    // The user is out (this part works today) ...
    expect(location.href).toContain('/login');
    // ... but where they were must be recoverable across the document
    // navigation. FAILS today: nothing is ever recorded.
    expect(sessionStorage.getItem(RETURN_TO_KEY)).toBe(DEEP_LOCATION);
  });

  it('sends the user back to the remembered location after signing in, not /dashboard', async () => {
    stubLocation('/login');
    sessionStorage.setItem(RETURN_TO_KEY, DEEP_LOCATION);
    useAuthMock.mockReturnValue(authState({ isAuthenticated: true }));

    render(createElement(Login));

    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    // FAILS today: `Login` navigates to the hard-coded landing constant.
    expect(navigateMock).toHaveBeenCalledWith(DEEP_LOCATION);
    expect(navigateMock).not.toHaveBeenCalledWith(DEFAULT_LANDING);
  });
});
