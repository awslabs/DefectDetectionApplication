/**
 * Route guard tests for the synthetic data generation workspace
 * (synthetic-defect-data-generation, task 7.6, Requirement 9.3).
 *
 * `RequireRole` with `SYNTHETIC_ACCESS_ROLES` must redirect roles below
 * DataScientist (Operator, Viewer, and the role-less state) away from the
 * `/synthetic` routes instead of rendering them, and must render the pages
 * for DataScientist, UseCaseAdmin, and PortalAdmin.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { UserRole } from '../../types';

const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

import RequireRole from '../../components/RequireRole';
import { SYNTHETIC_ACCESS_ROLES } from '../../utils/syntheticAccess';

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
  });
}

/** Mounts the guarded synthetic route as `role`; true iff the page rendered. */
function syntheticPageMounts(role: UserRole | undefined): boolean {
  setRole(role);
  render(
    <MemoryRouter initialEntries={['/synthetic']}>
      <Routes>
        <Route path="/dashboard" element={<div>DASHBOARD_MOUNTED</div>} />
        <Route
          path="/synthetic"
          element={
            <RequireRole roles={SYNTHETIC_ACCESS_ROLES}>
              <div>SYNTHETIC_PAGE_MOUNTED</div>
            </RequireRole>
          }
        />
      </Routes>
    </MemoryRouter>
  );
  return screen.queryAllByText('SYNTHETIC_PAGE_MOUNTED').length > 0;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Synthetic data route guard (Req 9.3)', () => {
  it.each(['Viewer', 'Operator'] as const)(
    'redirects %s to the dashboard instead of rendering the page',
    (role) => {
      expect(syntheticPageMounts(role)).toBe(false);
      expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
      cleanup();
    }
  );

  it('redirects the role-less/loading state', () => {
    expect(syntheticPageMounts(undefined)).toBe(false);
    expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
  });

  it.each(['DataScientist', 'UseCaseAdmin', 'PortalAdmin'] as const)(
    'renders the page for %s',
    (role) => {
      expect(syntheticPageMounts(role)).toBe(true);
      cleanup();
    }
  );
});
