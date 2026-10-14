/**
 * Integration tests for builds/fleet route guards and sidebar consistency
 * (build-fleet-rbac-visibility bugfix, design "Integration Tests").
 *
 * These exercise the real `App` route tree (the actual `RequireRole` wrappers
 * around `builds`, `builds/:buildJobId`, and `admin/fleet`) inside a
 * MemoryRouter with auth mocked per role, and the real `Layout` side
 * navigation, then assert the two surfaces agree:
 *
 *  - Viewer navigating to `/builds` or `/admin/fleet` ends up on `/dashboard`
 *  - PortalAdmin reaches BuildsPage and FleetPage
 *  - DataScientist reaches BuildsPage but is redirected from `/admin/fleet`
 *  - For every role, a nav entry is shown if and only if the corresponding
 *    route renders its page (no nav-item-without-route and no
 *    route-without-nav divergence)
 *
 * UI gating only — server-side RBAC remains the ultimate authority for direct
 * API calls (Requirement 2.7, defense in depth).
 *
 * Validates: Requirements 2.5, 2.6, 3.6
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { UserRole } from './types';

const { useAuthMock, routerState } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  routerState: { entry: '/dashboard' },
}));

// Auth is the only role source the UI has (JWT `custom:role` via useAuth()).
vi.mock('./contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Swap App's BrowserRouter for a MemoryRouter seeded with the entry under
// test, and mount a probe that reports the resulting location so redirects are
// observable. Everything else in react-router-dom stays real.
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  function LocationProbe() {
    const location = actual.useLocation();
    return <span data-testid="current-path">{location.pathname}</span>;
  }
  return {
    ...actual,
    BrowserRouter: ({ children }: { children: ReactNode }) => (
      <actual.MemoryRouter initialEntries={[routerState.entry]}>
        {children}
        <LocationProbe />
      </actual.MemoryRouter>
    ),
  };
});

// Sentinel page stubs: the assertions are about which page the router mounts,
// not about what those pages fetch or render.
vi.mock('./pages/builds/BuildsPage', () => ({
  default: () => <div>BUILDS_PAGE_MOUNTED</div>,
}));
vi.mock('./pages/builds/BuildDetail', () => ({
  default: () => <div>BUILD_DETAIL_MOUNTED</div>,
}));
vi.mock('./pages/admin/FleetPage', () => ({
  default: () => <div>FLEET_PAGE_MOUNTED</div>,
}));
vi.mock('./pages/Dashboard', () => ({
  default: () => <div>DASHBOARD_MOUNTED</div>,
}));

import { MemoryRouter, Route, Routes } from 'react-router-dom';
import App from './App';
import Layout from './components/Layout';

// --------------------------------------------------------------- harness

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

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

/** Renders the real App route tree at `path` as `role`. */
function renderAppAt(role: UserRole | undefined, path: string) {
  setRole(role);
  routerState.entry = path;
  render(<App />);
}

function currentPath(): string {
  return screen.getByTestId('current-path').textContent ?? '';
}

function isMounted(sentinel: string): boolean {
  return screen.queryAllByText(sentinel).length > 0;
}

/** True when navigating to `path` as `role` mounts `sentinel`. */
function pageMounts(
  role: UserRole | undefined,
  path: string,
  sentinel: string
): boolean {
  renderAppAt(role, path);
  const mounted = isMounted(sentinel);
  cleanup();
  return mounted;
}

/** Which builds-surface entries the real sidebar shows for `role`. */
function navEntries(role: UserRole | undefined): {
  builds: boolean;
  buildFleet: boolean;
} {
  setRole(role);
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Routes>
        <Route path="*" element={<Layout />} />
      </Routes>
    </MemoryRouter>
  );
  const entries = {
    builds: screen.queryAllByText('Builds').length > 0,
    buildFleet: screen.queryAllByText('Build Fleet').length > 0,
  };
  cleanup();
  return entries;
}

beforeAll(() => {
  // Cloudscape's AppLayout needs these browser APIs, which jsdom lacks.
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  if (!window.matchMedia) {
    (window as any).matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  routerState.entry = '/dashboard';
  setRole('Viewer');
});

// ------------------------------------------------------------ route guards

describe('App routes — Viewer (no builds access)', () => {
  it.each([
    ['/builds', 'BUILDS_PAGE_MOUNTED'],
    ['/builds/job-1', 'BUILD_DETAIL_MOUNTED'],
    ['/admin/fleet', 'FLEET_PAGE_MOUNTED'],
  ])('ends up on /dashboard when navigating to %s', (path, sentinel) => {
    renderAppAt('Viewer', path);

    expect(currentPath()).toBe('/dashboard');
    expect(isMounted(sentinel)).toBe(false);
    expect(isMounted('DASHBOARD_MOUNTED')).toBe(true);
  });
});

describe('App routes — PortalAdmin', () => {
  it('reaches BuildsPage at /builds', () => {
    renderAppAt('PortalAdmin', '/builds');

    expect(currentPath()).toBe('/builds');
    expect(isMounted('BUILDS_PAGE_MOUNTED')).toBe(true);
  });

  it('reaches FleetPage at /admin/fleet', () => {
    renderAppAt('PortalAdmin', '/admin/fleet');

    expect(currentPath()).toBe('/admin/fleet');
    expect(isMounted('FLEET_PAGE_MOUNTED')).toBe(true);
  });
});

describe('App routes — DataScientist', () => {
  it('reaches BuildsPage at /builds', () => {
    renderAppAt('DataScientist', '/builds');

    expect(currentPath()).toBe('/builds');
    expect(isMounted('BUILDS_PAGE_MOUNTED')).toBe(true);
  });

  it('is redirected from /admin/fleet to /dashboard', () => {
    renderAppAt('DataScientist', '/admin/fleet');

    expect(currentPath()).toBe('/dashboard');
    expect(isMounted('FLEET_PAGE_MOUNTED')).toBe(false);
    expect(isMounted('DASHBOARD_MOUNTED')).toBe(true);
  });
});

// --------------------------------------------------- sidebar/route agreement

describe('Sidebar and route guards agree for every role', () => {
  it.each<UserRole | undefined>([...ALL_ROLES, undefined])(
    'role=%s: nav entries match reachable pages',
    (role) => {
      const nav = navEntries(role);

      expect(
        pageMounts(role, '/builds', 'BUILDS_PAGE_MOUNTED'),
        `role=${role}: "Builds" nav=${nav.builds} but /builds render differs`
      ).toBe(nav.builds);

      expect(
        pageMounts(role, '/admin/fleet', 'FLEET_PAGE_MOUNTED'),
        `role=${role}: "Build Fleet" nav=${nav.buildFleet} but /admin/fleet render differs`
      ).toBe(nav.buildFleet);

      // `/builds/:buildJobId` is guarded by the same predicate as `/builds`,
      // so the detail page must follow the same nav entry (Req 2.5, 3.6).
      expect(
        pageMounts(role, '/builds/job-1', 'BUILD_DETAIL_MOUNTED'),
        `role=${role}: "Builds" nav=${nav.builds} but /builds/:id render differs`
      ).toBe(nav.builds);
    }
  );
});
