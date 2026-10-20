/**
 * Route guard tests for the workflow debug pages (workflow-debug-mode task
 * 5.4, Requirements 2.6, 3.1), in the shape of `syntheticRouteGuard.test.tsx`.
 *
 * `RequireRole` with `DEBUG_VIEW_ROLES` renders both routes
 * (`/debug/:usecaseId` and `/debug/:usecaseId/:workflowId/:deviceId`) for
 * every role holding VIEW_DEVICE_LOGS and redirects DataLabeler and the
 * role-less state to the dashboard.
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
import { DEBUG_VIEW_ROLES } from '../../utils/debugAccess';

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
  { path: '/debug/:usecaseId', entry: '/debug/uc-1', sentinel: 'DEBUG_SESSIONS_MOUNTED' },
  { path: '/debug/:usecaseId/:workflowId/:deviceId', entry: '/debug/uc-1/wf-1/thing-a', sentinel: 'DEBUG_VIEWER_MOUNTED' },
] as const;

/** Mounts the guarded debug routes at `entry` as `role`; true iff the page rendered. */
function debugPageMounts(role: UserRole | undefined, entry: string, sentinel: string): boolean {
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
              <RequireRole roles={DEBUG_VIEW_ROLES}>
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

describe('Debug route guard (Req 2.6, 3.1)', () => {
  describe.each(ROUTES)('$path', ({ entry, sentinel }) => {
    it.each(['Viewer', 'Operator', 'DataScientist', 'UseCaseAdmin', 'PortalAdmin'] as const)(
      'renders the page for %s',
      (role) => {
        expect(debugPageMounts(role, entry, sentinel)).toBe(true);
        cleanup();
      }
    );

    it('redirects DataLabeler to the dashboard instead of rendering the page', () => {
      expect(debugPageMounts('DataLabeler', entry, sentinel)).toBe(false);
      expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
      cleanup();
    });

    it('redirects the role-less/loading state', () => {
      expect(debugPageMounts(undefined, entry, sentinel)).toBe(false);
      expect(screen.getAllByText('DASHBOARD_MOUNTED').length).toBeGreaterThan(0);
      cleanup();
    });
  });
});
