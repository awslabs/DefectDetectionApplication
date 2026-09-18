/**
 * Unit tests for the `ProtectedRoute` Session_Exit
 * (spec: portal-session-expiry-return-to-page, task 3.4).
 *
 * The router-side guard has three branches, and all three matter here:
 *
 *  - **unauthenticated** — redirects to `/login` with `replace`, carries the
 *    attempted location in the router `state`, *and* records it in the shared
 *    `sessionRedirect` store so `Login` can return the user there even when
 *    the exit that actually happens is the API client's document navigation
 *    (Requirement 1.2, design Decision 1);
 *  - **authenticated** — renders its children and records nothing;
 *  - **loading** — renders the spinner and records nothing, so a location is
 *    never remembered while auth is still resolving.
 *
 * Uses the `vi.mock('react-router-dom')` `Navigate`-spy + `MemoryRouter`
 * pattern from `components/RequireRole.test.tsx` (Requirement 6.2). The spy
 * normally renders the real `Navigate` so the redirect genuinely lands on the
 * `/login` route; `harness.renderRealNavigate` turns that off for the one case
 * that mounts the guard *at* `/login`, where a real self-redirect would spin.
 *
 * Validates: Requirements 1.2, 1.4, 1.6, 6.2
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { UserRole } from '../types';

const { useAuthMock, navigateProps, harness } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  navigateProps: [] as Array<{ to: unknown; replace?: boolean; state?: unknown }>,
  harness: { renderRealNavigate: true },
}));

// The guard reads auth state only through this hook.
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Keep the real router behavior (so the redirect actually navigates) while
// recording the props the guard passes to `Navigate` — including `state`,
// which is the belt-and-braces half of design Decision 1.
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    Navigate: (props: { to: any; replace?: boolean; state?: unknown }) => {
      navigateProps.push({ to: props.to, replace: props.replace, state: props.state });
      return harness.renderRealNavigate ? (
        <actual.Navigate {...props} />
      ) : (
        <div>REDIRECT_RECORDED</div>
      );
    },
  };
});

import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ProtectedRoute from './ProtectedRoute';
import { RETURN_TO_KEY } from '../services/sessionRedirect';

// --------------------------------------------------------------- harness

/** A deep page with both a query string and a fragment. */
const DEEP_ENTRY = '/workflows/builder/abc?tab=nodes#latest';

interface AuthState {
  isAuthenticated?: boolean;
  isLoading?: boolean;
  role?: UserRole;
}

function setAuth({ isAuthenticated = false, isLoading = false, role = 'DataScientist' }: AuthState) {
  useAuthMock.mockReturnValue({
    user: isAuthenticated
      ? {
          user_id: 'user-1',
          email: 'user@example.com',
          username: 'user',
          role,
          is_super_user: false,
        }
      : null,
    isAuthenticated,
    isLoading,
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

/**
 * Mounts the guard at `entry` inside a two-route tree, so both the guarded
 * content and the redirect target are observable.
 */
function renderGuard(entry: string, auth: AuthState) {
  setAuth(auth);
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<div>LOGIN_PAGE</div>} />
        <Route
          path="*"
          element={
            <ProtectedRoute>
              <div>PROTECTED_CONTENT</div>
            </ProtectedRoute>
          }
        />
      </Routes>
    </MemoryRouter>
  );
}

/** What the shared store holds right now. */
function remembered(): string | null {
  return window.sessionStorage.getItem(RETURN_TO_KEY);
}

beforeEach(() => {
  vi.clearAllMocks();
  navigateProps.length = 0;
  harness.renderRealNavigate = true;
  window.sessionStorage.clear();
});

// ---------------------------------------------------------- unauthenticated

describe('ProtectedRoute — unauthenticated', () => {
  it('redirects to /login with replace and does not render the children', () => {
    renderGuard(DEEP_ENTRY, { isAuthenticated: false });

    expect(screen.queryByText('PROTECTED_CONTENT')).not.toBeInTheDocument();
    expect(screen.getByText('LOGIN_PAGE')).toBeInTheDocument();
    expect(navigateProps).toHaveLength(1);
    expect(navigateProps[0].to).toBe('/login');
    expect(navigateProps[0].replace).toBe(true);
  });

  it('records the attempted location as pathname + search + hash', () => {
    renderGuard(DEEP_ENTRY, { isAuthenticated: false });

    expect(remembered()).toBe(DEEP_ENTRY);
  });

  it('carries the attempted location in the router state as well', () => {
    renderGuard(DEEP_ENTRY, { isAuthenticated: false });

    expect(navigateProps[0].state).toEqual(
      expect.objectContaining({
        from: expect.objectContaining({
          pathname: '/workflows/builder/abc',
          search: '?tab=nodes',
          hash: '#latest',
        }),
      })
    );
  });

  it.each([
    ['/dashboard', '/dashboard'],
    ['/devices/dev-1?tab=cameras', '/devices/dev-1?tab=cameras'],
    ['/labeler', '/labeler'],
    ['/models/model-1/versions/2?compare=1#diff', '/models/model-1/versions/2?compare=1#diff'],
  ])('records %s verbatim', (entry, expected) => {
    renderGuard(entry, { isAuthenticated: false });

    expect(remembered()).toBe(expected);
  });

  it('keeps an already-remembered earlier location (first write wins)', () => {
    // A 401 from an earlier, deeper page got there first (Requirement 1.6).
    window.sessionStorage.setItem(RETURN_TO_KEY, '/tuning/session-7?step=2');

    renderGuard(DEEP_ENTRY, { isAuthenticated: false });

    expect(remembered()).toBe('/tuning/session-7?step=2');
    expect(screen.getByText('LOGIN_PAGE')).toBeInTheDocument();
  });

  it('never records /login itself', () => {
    // The guard does not wrap /login in the real route table; mounting it
    // there anyway pins the shared module's guard for the case
    // (Requirement 1.4). The real `Navigate` is suppressed because a genuine
    // self-redirect to the location it is already on would spin.
    harness.renderRealNavigate = false;
    setAuth({ isAuthenticated: false });
    render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <ProtectedRoute>
                <div>PROTECTED_CONTENT</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    );

    expect(remembered()).toBeNull();
    expect(navigateProps).toEqual([
      expect.objectContaining({ to: '/login', replace: true }),
    ]);
  });
});

// ------------------------------------------------------------ authenticated

describe('ProtectedRoute — authenticated', () => {
  it('renders the children and records nothing', () => {
    renderGuard(DEEP_ENTRY, { isAuthenticated: true });

    expect(screen.getByText('PROTECTED_CONTENT')).toBeInTheDocument();
    expect(screen.queryByText('LOGIN_PAGE')).not.toBeInTheDocument();
    expect(navigateProps).toHaveLength(0);
    expect(remembered()).toBeNull();
  });

  it('leaves an existing remembered location untouched', () => {
    window.sessionStorage.setItem(RETURN_TO_KEY, '/tuning/session-7?step=2');

    renderGuard(DEEP_ENTRY, { isAuthenticated: true });

    expect(screen.getByText('PROTECTED_CONTENT')).toBeInTheDocument();
    expect(remembered()).toBe('/tuning/session-7?step=2');
  });
});

// ------------------------------------------------------------------ loading

describe('ProtectedRoute — loading', () => {
  it('renders the spinner, not the children and not a redirect', () => {
    const { container } = renderGuard(DEEP_ENTRY, { isLoading: true });

    expect(screen.queryByText('PROTECTED_CONTENT')).not.toBeInTheDocument();
    expect(screen.queryByText('LOGIN_PAGE')).not.toBeInTheDocument();
    expect(navigateProps).toHaveLength(0);

    // The Cloudscape spinner inside the full-height centering wrapper the
    // component renders while auth resolves.
    const wrapper = container.querySelector('div[style*="min-height: 100vh"]');
    expect(wrapper).not.toBeNull();
    expect(wrapper?.querySelector('span[class*="circle"]')).not.toBeNull();
  });

  it('records nothing while auth is still resolving', () => {
    renderGuard(DEEP_ENTRY, { isLoading: true });

    // A location remembered here would be consumed by a sign-in that never
    // needed to happen.
    expect(remembered()).toBeNull();
  });
});
