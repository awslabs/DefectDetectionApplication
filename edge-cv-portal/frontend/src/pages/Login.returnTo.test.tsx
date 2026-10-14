/**
 * Unit tests for `pages/Login.tsx`'s post-sign-in destination
 * (spec: portal-session-expiry-return-to-page, task 3.4).
 *
 * `Login` resolves where to send the user as "the remembered
 * Attempted_Location when one validates, else the role's landing page"
 * (design Decision 5). Covered here, per Requirement 6.3: restoration, the
 * `/dashboard` default, the `DataLabeler` `/labeler` default, the new-password
 * challenge path, and the pre-existing behavior that an already-authenticated
 * visit to `/login` redirects immediately.
 *
 * `useNavigate` is replaced by a spy — the destination `Login` chooses is the
 * observable, not a real navigation. Auth is driven through the `useAuth`
 * hook, the only auth source the page has.
 *
 * Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 3.2, 6.3
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { UserRole } from '../types';

const { navigateMock, useAuthMock, completeNewPasswordMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  completeNewPasswordMock: vi.fn(),
}));

// Keep the real router, but observe where `Login` decides to send the user.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

import Login from './Login';
import { RETURN_TO_KEY } from '../services/sessionRedirect';

// --------------------------------------------------------------- harness

/** A deep page carrying both a query string and a fragment. */
const DEEP_LOCATION = '/workflows/builder/abc?tab=nodes#latest';

interface AuthState {
  isAuthenticated?: boolean;
  needsNewPassword?: boolean;
  role?: UserRole;
}

function setAuth({
  isAuthenticated = false,
  needsNewPassword = false,
  role = 'DataScientist',
}: AuthState) {
  useAuthMock.mockReturnValue({
    user: {
      user_id: 'user-1',
      email: 'user@example.com',
      username: 'user',
      role,
      is_super_user: false,
    },
    isAuthenticated,
    isLoading: false,
    needsNewPassword,
    login: vi.fn(),
    completeNewPassword: completeNewPasswordMock,
    changePassword: vi.fn(),
    forgotPassword: vi.fn(),
    forgotPasswordSubmit: vi.fn(),
    logout: vi.fn(),
    error: null,
  });
}

/** Plant a value as a Session_Exit (or an attacker) would have. */
function remember(raw: string): void {
  window.sessionStorage.setItem(RETURN_TO_KEY, raw);
}

function remembered(): string | null {
  return window.sessionStorage.getItem(RETURN_TO_KEY);
}

/** Mounts `Login` with the given auth state. */
function renderLogin(auth: AuthState = {}) {
  setAuth(auth);
  return render(<Login />);
}

/** Fills the new-password form and submits it. */
async function submitNewPassword(container: HTMLElement) {
  fireEvent.change(screen.getByPlaceholderText('Enter your name'), {
    target: { value: 'Ada' },
  });
  const passwords = container.querySelectorAll('input[type="password"]');
  expect(passwords).toHaveLength(2);
  fireEvent.change(passwords[0], { target: { value: 'CorrectHorse1' } });
  fireEvent.change(passwords[1], { target: { value: 'CorrectHorse1' } });

  fireEvent.click(screen.getByText('Set New Password'));
  await waitFor(() => expect(completeNewPasswordMock).toHaveBeenCalled());
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  completeNewPasswordMock.mockResolvedValue(undefined);
});

// ------------------------------------------------------------- restoration

describe('Login — returns to the remembered location', () => {
  it('navigates to the remembered deep location instead of /dashboard', () => {
    remember(DEEP_LOCATION);

    renderLogin({ isAuthenticated: true });

    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith(DEEP_LOCATION);
    expect(navigateMock).not.toHaveBeenCalledWith('/dashboard');
  });

  it('clears the remembered location once it has been used', () => {
    remember(DEEP_LOCATION);

    renderLogin({ isAuthenticated: true });

    // Consume-once: a later ordinary sign-in must not resurrect the page
    // (Requirement 2.4).
    expect(remembered()).toBeNull();
  });

  it('does not resurrect the location on a subsequent sign-in', () => {
    remember(DEEP_LOCATION);
    renderLogin({ isAuthenticated: true });
    expect(navigateMock).toHaveBeenCalledWith(DEEP_LOCATION);

    navigateMock.mockClear();
    renderLogin({ isAuthenticated: true });

    expect(navigateMock).toHaveBeenCalledWith('/dashboard');
    expect(navigateMock).not.toHaveBeenCalledWith(DEEP_LOCATION);
  });

  it('restores the remembered location for a DataLabeler too', () => {
    remember('/labeler/tasks/task-9?item=3');

    renderLogin({ isAuthenticated: true, role: 'DataLabeler' });

    expect(navigateMock).toHaveBeenCalledWith('/labeler/tasks/task-9?item=3');
  });

  it.each([
    '//evil.example.com/steal',
    '/\\evil.example.com',
    'https://evil.example.com/dashboard',
    'javascript:alert(1)',
    '/dashboard\u0000',
    '/login?next=/dashboard',
    'dashboard',
  ])('discards the unsafe remembered value %j and uses the landing page', (raw) => {
    remember(raw);

    renderLogin({ isAuthenticated: true });

    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith('/dashboard');
    // Discarded *and* removed, so it cannot be retried (Requirement 3.2).
    expect(remembered()).toBeNull();
  });

  it('discards an over-length remembered value', () => {
    remember(`/devices/${'a'.repeat(3000)}`);

    renderLogin({ isAuthenticated: true });

    expect(navigateMock).toHaveBeenCalledWith('/dashboard');
    expect(remembered()).toBeNull();
  });
});

// ----------------------------------------------------------------- defaults

describe('Login — landing page when nothing is remembered', () => {
  it('navigates to /dashboard', () => {
    renderLogin({ isAuthenticated: true });

    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith('/dashboard');
  });

  it('navigates a DataLabeler to /labeler', () => {
    renderLogin({ isAuthenticated: true, role: 'DataLabeler' });

    expect(navigateMock).toHaveBeenCalledWith('/labeler');
  });

  it.each<UserRole>(['PortalAdmin', 'UseCaseAdmin', 'DataScientist', 'Operator', 'Viewer'])(
    'navigates a %s to /dashboard',
    (role) => {
      renderLogin({ isAuthenticated: true, role });

      expect(navigateMock).toHaveBeenCalledWith('/dashboard');
    }
  );

  it('does not navigate at all while the user is not authenticated', () => {
    remember(DEEP_LOCATION);

    renderLogin({ isAuthenticated: false });

    expect(navigateMock).not.toHaveBeenCalled();
    // Nothing consumed either: the value is still there for the real sign-in.
    expect(remembered()).toBe(DEEP_LOCATION);
  });
});

// ------------------------------------------------------ new-password challenge

describe('Login — new-password challenge', () => {
  it('restores the remembered location after setting a new password', async () => {
    remember(DEEP_LOCATION);
    const { container } = renderLogin({ needsNewPassword: true });

    await submitNewPassword(container);

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith(DEEP_LOCATION));
    expect(navigateMock).not.toHaveBeenCalledWith('/dashboard');
    expect(remembered()).toBeNull();
  });

  it('falls back to the landing page when nothing is remembered', async () => {
    const { container } = renderLogin({ needsNewPassword: true });

    await submitNewPassword(container);

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/dashboard'));
  });

  it('navigates to exactly one destination when the challenge also authenticates', async () => {
    // `completeNewPassword` flips `isAuthenticated` as well, so both the
    // handler and the effect want to navigate; they must agree on the same
    // destination and never fall back to /dashboard (design Decision 5).
    remember('/tuning/session-7?step=2');
    setAuth({ needsNewPassword: true });
    const { container, rerender } = render(<Login />);

    completeNewPasswordMock.mockImplementation(async () => {
      setAuth({ needsNewPassword: true, isAuthenticated: true });
      rerender(<Login />);
    });

    await submitNewPassword(container);

    await waitFor(() => expect(navigateMock).toHaveBeenCalled());
    const destinations = new Set(navigateMock.mock.calls.map(([to]) => to));
    expect([...destinations]).toEqual(['/tuning/session-7?step=2']);
  });
});

// -------------------------------------------------- unchanged /login behavior

describe('Login — already-authenticated visit to /login', () => {
  it('still redirects immediately (Requirement 2.6)', () => {
    renderLogin({ isAuthenticated: true });

    // Synchronously on mount, exactly as before this feature.
    expect(navigateMock).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith('/dashboard');
  });

  it('renders the sign-in form for an unauthenticated visitor', () => {
    renderLogin({ isAuthenticated: false });

    expect(screen.getByText('Sign In')).toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
