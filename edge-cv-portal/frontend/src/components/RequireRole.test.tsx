/**
 * Unit tests for the `RequireRole` route guard
 * (build-fleet-rbac-visibility bugfix, design Part B).
 *
 * The guard renders its children when the signed-in user's JWT-carried role
 * is in the allowed set, and otherwise redirects to `/dashboard` with
 * `replace` so a role without access never renders a page whose only content
 * is a 403 error banner, and never leaves the blocked URL on the history
 * stack.
 *
 * Validates: Requirements 2.5, 2.6
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { BUILDS_ACCESS_ROLES } from '../utils/buildsAccess';
import type { UserRole } from '../types';

const { useAuthMock, navigateProps } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  navigateProps: [] as Array<{ to: unknown; replace?: boolean }>,
}));

// Auth is the only role source the UI has (JWT `custom:role` via useAuth()).
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Keep the real router behavior (so the redirect actually navigates) while
// recording the props the guard passes to `Navigate`.
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    Navigate: (props: { to: any; replace?: boolean }) => {
      navigateProps.push({ to: props.to, replace: props.replace });
      return <actual.Navigate {...props} />;
    },
  };
});

import { MemoryRouter, Route, Routes } from 'react-router-dom';
import RequireRole from './RequireRole';

function setRole(role: UserRole | undefined) {
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

/** Renders the guard at `/builds` inside a two-route tree so the redirect
 *  target is observable. */
function renderGuard(roles: readonly UserRole[]) {
  render(
    <MemoryRouter initialEntries={['/builds']}>
      <Routes>
        <Route
          path="/builds"
          element={
            <RequireRole roles={roles}>
              <div>GUARDED_CONTENT</div>
            </RequireRole>
          }
        />
        <Route path="/dashboard" element={<div>DASHBOARD</div>} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  navigateProps.length = 0;
});

describe('RequireRole', () => {
  it.each<UserRole>(['DataScientist', 'UseCaseAdmin', 'PortalAdmin'])(
    'renders children for an allowed role (%s)',
    (role) => {
      setRole(role);
      renderGuard(BUILDS_ACCESS_ROLES);

      expect(screen.getByText('GUARDED_CONTENT')).toBeInTheDocument();
      expect(screen.queryByText('DASHBOARD')).not.toBeInTheDocument();
      expect(navigateProps).toHaveLength(0);
    }
  );

  it.each<UserRole>(['Viewer', 'Operator'])(
    'redirects a disallowed role (%s) to /dashboard with replace',
    (role) => {
      setRole(role);
      renderGuard(BUILDS_ACCESS_ROLES);

      expect(screen.queryByText('GUARDED_CONTENT')).not.toBeInTheDocument();
      expect(screen.getByText('DASHBOARD')).toBeInTheDocument();
      expect(navigateProps).toEqual([{ to: '/dashboard', replace: true }]);
    }
  );

  it('redirects when there is no signed-in user / no role', () => {
    setRole(undefined);
    renderGuard(BUILDS_ACCESS_ROLES);

    expect(screen.queryByText('GUARDED_CONTENT')).not.toBeInTheDocument();
    expect(screen.getByText('DASHBOARD')).toBeInTheDocument();
    expect(navigateProps).toEqual([{ to: '/dashboard', replace: true }]);
  });

  it('honours a narrower role set: PortalAdmin-only guard admits PortalAdmin', () => {
    setRole('PortalAdmin');
    renderGuard(['PortalAdmin']);

    expect(screen.getByText('GUARDED_CONTENT')).toBeInTheDocument();
  });

  it('honours a narrower role set: PortalAdmin-only guard redirects DataScientist', () => {
    setRole('DataScientist');
    renderGuard(['PortalAdmin']);

    expect(screen.queryByText('GUARDED_CONTENT')).not.toBeInTheDocument();
    expect(screen.getByText('DASHBOARD')).toBeInTheDocument();
    expect(navigateProps).toEqual([{ to: '/dashboard', replace: true }]);
  });
});
