/**
 * **Feature: build-fleet-rbac-visibility, Property 2: Bug Condition (frontend) — builds surface visibility is a function of the role**
 *
 * For any role in the `UserRole` domain (plus `undefined` for the
 * role-less/loading state), the navigation items produced for that role SHALL
 * include the "Builds" item if and only if the role is in
 * {DataScientist, UseCaseAdmin, PortalAdmin}, SHALL include the "Build Fleet"
 * item if and only if the role is PortalAdmin, and direct navigation to
 * `/builds`, `/builds/:buildJobId`, or `/admin/fleet` SHALL render the page if
 * and only if the same role predicate holds — otherwise the router SHALL
 * redirect away (no 403-banner page render).
 *
 * **Validates: Requirements 1.6, 2.5**
 *
 * This test encodes the EXPECTED behavior, so it FAILS on the unfixed code
 * (the exploration observation: "Builds" renders for every role and the three
 * routes mount their pages for roles without builds access) and PASSES once
 * the gating from design Part B lands.
 *
 * The oracle is re-derived locally from the merged role-permission matrix
 * (`builds:*` → DataScientist, UseCaseAdmin, PortalAdmin) rather than imported
 * from the production predicate, so the test does not depend on the fix's own
 * helper for its notion of correctness.
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';
import { UserRole } from '../types';

const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));

// Auth is the only role source the UI has (JWT `custom:role` via useAuth()).
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Sentinel page stubs: the assertions are about whether the router mounts the
// builds/fleet pages at all, not about what those pages fetch or render.
vi.mock('../pages/builds/BuildsPage', () => ({
  default: () => <div>BUILDS_PAGE_MOUNTED</div>,
}));
vi.mock('../pages/builds/BuildDetail', () => ({
  default: () => <div>BUILD_DETAIL_MOUNTED</div>,
}));
vi.mock('../pages/admin/FleetPage', () => ({
  default: () => <div>FLEET_PAGE_MOUNTED</div>,
}));
vi.mock('../pages/Dashboard', () => ({
  default: () => <div>DASHBOARD_MOUNTED</div>,
}));

import App from '../App';
import Layout from './Layout';

// --------------------------------------------------------------- oracles

/** Roles holding `builds:read`/`builds:submit`/`builds:cancel` per the merged
 *  backend matrix (`shared_utils.RBACManager`). */
function oracleCanAccessBuilds(role: UserRole | undefined): boolean {
  return (
    role === 'DataScientist' ||
    role === 'UseCaseAdmin' ||
    role === 'PortalAdmin'
  );
}

const ALL_ROLES: readonly UserRole[] = [
  'PortalAdmin',
  'UseCaseAdmin',
  'DataScientist',
  'Operator',
  'Viewer',
];

/** Full domain: every role plus the role-less / still-loading state. */
const roleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom(
  ...ALL_ROLES,
  undefined
);

/** Scoped domain: the concrete roles the bug is reported for. */
const noBuildsRoleArb: fc.Arbitrary<UserRole | undefined> = fc.constantFrom<
  UserRole | undefined
>('Viewer', 'Operator', undefined);

// --------------------------------------------------------------- harness

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

/** Renders the Layout side navigation for `role` and reports which of the
 *  builds-surface entries are present. */
function navContains(role: UserRole | undefined): {
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
  return {
    builds: screen.queryAllByText('Builds').length > 0,
    buildFleet: screen.queryAllByText('Build Fleet').length > 0,
  };
}

/** Navigates the real App route tree (BrowserRouter) to `path` as `role` and
 *  reports whether the guarded page mounted. */
function pageMounts(
  role: UserRole | undefined,
  path: string,
  sentinel: string
): boolean {
  setRole(role);
  window.history.pushState({}, '', path);
  render(<App />);
  return screen.queryAllByText(sentinel).length > 0;
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
  setRole('Viewer');
});

// ----------------------------------------------------------------- tests

describe('Property 2: Bug Condition — builds surface visibility is a function of the role', () => {
  it('omits the "Builds" nav item for roles without builds access (scoped: Viewer, Operator, role-less)', () => {
    fc.assert(
      fc.property(noBuildsRoleArb, (role) => {
        try {
          const { builds } = navContains(role);
          // Bug Condition, UiNavigation branch: role NOT IN
          // ['DataScientist', 'UseCaseAdmin', 'PortalAdmin'] must not see
          // the builds surface (Req 1.6, 2.5).
          expect(
            builds,
            `role=${role}: "Builds" nav item present`
          ).toBe(false);
        } finally {
          cleanup();
        }
      }),
      { numRuns: 10 }
    );
  });

  it('includes "Builds" iff the role has builds access and "Build Fleet" iff PortalAdmin (full role domain)', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        try {
          const { builds, buildFleet } = navContains(role);
          expect(builds, `role=${role}: "Builds" visibility`).toBe(
            oracleCanAccessBuilds(role)
          );
          expect(buildFleet, `role=${role}: "Build Fleet" visibility`).toBe(
            role === 'PortalAdmin'
          );
        } finally {
          cleanup();
        }
      }),
      { numRuns: 12 }
    );
  });

  it('does not mount BuildsPage or BuildDetail for roles without builds access (scoped: Viewer, Operator, role-less)', () => {
    fc.assert(
      fc.property(
        noBuildsRoleArb,
        fc.constantFrom(
          ['/builds', 'BUILDS_PAGE_MOUNTED'] as const,
          ['/builds/job-1', 'BUILD_DETAIL_MOUNTED'] as const
        ),
        (role, [path, sentinel]) => {
          try {
            // Expected Behavior: the router redirects away — no
            // 403-banner page render (Req 2.5).
            expect(
              pageMounts(role, path, sentinel),
              `role=${role}: ${path} mounted ${sentinel}`
            ).toBe(false);
          } finally {
            cleanup();
          }
        }
      ),
      { numRuns: 12 }
    );
  });

  it('does not mount FleetPage at /admin/fleet for a role without builds access (Operator)', () => {
    try {
      expect(
        pageMounts('Operator', '/admin/fleet', 'FLEET_PAGE_MOUNTED'),
        'role=Operator: /admin/fleet mounted FleetPage'
      ).toBe(false);
    } finally {
      cleanup();
    }
  });

  it('mounts the builds pages iff the role has builds access and FleetPage iff PortalAdmin (full role domain)', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        try {
          expect(
            pageMounts(role, '/builds', 'BUILDS_PAGE_MOUNTED'),
            `role=${role}: /builds render`
          ).toBe(oracleCanAccessBuilds(role));
          cleanup();
          expect(
            pageMounts(role, '/admin/fleet', 'FLEET_PAGE_MOUNTED'),
            `role=${role}: /admin/fleet render`
          ).toBe(role === 'PortalAdmin');
        } finally {
          cleanup();
        }
      }),
      { numRuns: 12 }
    );
  });
});
