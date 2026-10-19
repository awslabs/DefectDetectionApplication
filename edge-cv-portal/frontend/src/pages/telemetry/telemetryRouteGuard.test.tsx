/**
 * Route guard tests for the workflow telemetry dashboards pages
 * (workflow-telemetry-dashboards task 5.5, design §11.1, §11.4), in the shape
 * of `syntheticRouteGuard.test.tsx`.
 *
 * `RequireRole` renders `/workflow-dashboards` and
 * `/workflow-dashboards/:dashboardId` for every role holding
 * `dashboards:read` (every role but DataLabeler), `/workflow-dashboards/create`
 * for `dashboards:manage` (not Viewer) and `/telemetry-settings/:usecaseId`
 * for `telemetry:manage` (UseCaseAdmin and PortalAdmin), redirecting every
 * other role and the role-less state to the dashboard.
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
import { DASHBOARDS_MANAGE_ROLES, DASHBOARDS_READ_ROLES, TELEMETRY_MANAGE_ROLES } from '../../utils/telemetryPermissions';

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

const ROUTES = [
  { path: '/workflow-dashboards', entry: '/workflow-dashboards', roles: DASHBOARDS_READ_ROLES, sentinel: 'DASHBOARDS_LIST_MOUNTED', allowed: ['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'], refused: ['DataLabeler'] },
  { path: '/workflow-dashboards/:dashboardId', entry: '/workflow-dashboards/dash-1', roles: DASHBOARDS_READ_ROLES, sentinel: 'DASHBOARD_VIEW_MOUNTED', allowed: ['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'], refused: ['DataLabeler'] },
  { path: '/workflow-dashboards/create', entry: '/workflow-dashboards/create', roles: DASHBOARDS_MANAGE_ROLES, sentinel: 'CREATE_DASHBOARD_MOUNTED', allowed: ['Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'], refused: ['Viewer', 'DataLabeler'] },
  { path: '/telemetry-settings/:usecaseId', entry: '/telemetry-settings/uc-1', roles: TELEMETRY_MANAGE_ROLES, sentinel: 'TELEMETRY_SETTINGS_MOUNTED', allowed: ['UseCaseAdmin', 'PortalAdmin'], refused: ['Viewer', 'Operator', 'DataScientist', 'DataLabeler'] },
] as const;

/** Mounts the guarded routes at `entry` as `role`; true iff the page rendered. */
function pageMounts(role: UserRole | undefined, entry: string, sentinel: string): boolean {
  setRole(role);
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/dashboard" element={<div>DASHBOARD_MOUNTED</div>} />
        {ROUTES.map((route) => (
          <Route
            key={route.path}
            path={route.path}
            element={
              <RequireRole roles={route.roles}>
                <div>{route.sentinel}</div>
              </RequireRole>
            }
          />
        ))}
      </Routes>
    </MemoryRouter>
  );
  return screen.queryAllByText(sentinel).length > 0;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Telemetry dashboards route guards (design §11.4)', () => {
  describe.each(ROUTES)('$path', ({ entry, sentinel, allowed, refused }) => {
    it.each([...allowed])('renders the page for %s', (role) => {
      expect(pageMounts(role, entry, sentinel)).toBe(true);
      cleanup();
    });

    it.each([...refused])('redirects %s to the dashboard instead of rendering the page', (role) => {
      expect(pageMounts(role, entry, sentinel)).toBe(false);
      expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
      cleanup();
    });

    it('redirects the role-less/loading state', () => {
      expect(pageMounts(undefined, entry, sentinel)).toBe(false);
      expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
      cleanup();
    });
  });

  it('the create route wins over the :dashboardId route for /workflow-dashboards/create', () => {
    expect(pageMounts('Viewer', '/workflow-dashboards/create', 'DASHBOARD_VIEW_MOUNTED')).toBe(false);
    expect(screen.queryAllByText('CREATE_DASHBOARD_MOUNTED').length).toBe(0);
    expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
  });
});
