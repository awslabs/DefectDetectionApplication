/**
 * **Feature: build-fleet-rbac-visibility, Property 4: Preservation (frontend) — builds-capable roles keep their UI**
 *
 * For any role in {DataScientist, UseCaseAdmin, PortalAdmin}, the fixed
 * frontend SHALL continue to show the "Builds" navigation item and render the
 * `/builds` and `/builds/:buildJobId` pages; the "Build Fleet"
 * (`/admin/fleet`) entry and page SHALL continue to be available to
 * PortalAdmin and only PortalAdmin; and all navigation items other than
 * "Builds"/"Build Fleet" SHALL be identical to the original for every role.
 *
 * **Validates: Requirements 3.6**
 *
 * Observation-first: `PRE_FIX_NAV_ORACLE` below is the navigation item list
 * observed on the UNFIXED code (commit before the design's Part B gating
 * landed) for every role in `UserRole` ∪ {undefined}, transcribed from
 * `Layout.tsx`'s `baseNavigationItems` / `portalAdminItems` / `auditLogsItem`
 * assembly. These tests PASS on the unfixed code and are re-run UNCHANGED
 * after the fix (task 3.7): the only permitted difference is the presence of
 * the "Builds" entry, which is stripped from both sides before comparison.
 *
 * The navigation items are captured from the `items` prop that `Layout` hands
 * to Cloudscape's `SideNavigation`, so the same assertions work before the fix
 * (inline assembly) and after it (extracted `buildNavigationItems(role)`).
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';
import { UserRole } from '../types';

const { useAuthMock, navCapture } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  navCapture: { items: null as unknown },
}));

// Auth is the only role source the UI has (JWT `custom:role` via useAuth()).
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Capture the exact navigation item list Layout produces, and render the
// AppLayout slots eagerly (Cloudscape's AppLayout does not lay out in jsdom).
vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    SideNavigation: (props: { items: unknown }) => {
      navCapture.items = props.items;
      return null;
    },
    AppLayout: (props: { navigation?: ReactNode; content?: ReactNode }) => (
      <>
        {props.navigation}
        {props.content}
      </>
    ),
  };
});

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

// ------------------------------------------------------- pre-fix nav oracle

type NavItem =
  | { type: 'link'; text: string; href: string }
  | { type: 'divider' };

/** Items every role saw on the UNFIXED code (Layout.baseNavigationItems). */
const PRE_FIX_BASE_ITEMS: readonly NavItem[] = [
  { type: 'link', text: 'Dashboard', href: '/dashboard' },
  { type: 'link', text: 'Use Cases', href: '/usecases' },
  { type: 'divider' },
  { type: 'link', text: 'Data Management', href: '/data' },
  { type: 'link', text: 'Labeling', href: '/labeling' },
  { type: 'link', text: 'Training', href: '/training' },
  { type: 'link', text: 'Models', href: '/models' },
  { type: 'divider' },
  { type: 'link', text: 'Workflows', href: '/workflows/builder' },
  { type: 'link', text: 'Node Designer', href: '/node-designer' },
  { type: 'link', text: 'Components', href: '/components' },
  // The ungated entry the fix gates — stripped before every comparison.
  { type: 'link', text: 'Builds', href: '/builds' },
  { type: 'link', text: 'Deployments', href: '/deployments' },
  { type: 'link', text: 'Devices', href: '/devices' },
];

/** PortalAdmin-only group (Layout.portalAdminItems), unchanged by the fix. */
const PRE_FIX_PORTAL_ADMIN_ITEMS: readonly NavItem[] = [
  { type: 'divider' },
  { type: 'link', text: 'Plugin Review', href: '/node-designer/review' },
  { type: 'link', text: 'Build Fleet', href: '/admin/fleet' },
  { type: 'link', text: 'Settings', href: '/settings' },
];

const PRE_FIX_AUDIT_LOGS_ITEM: NavItem = {
  type: 'link',
  text: 'Audit Logs',
  href: '/audit',
};

/**
 * The navigation item list observed on UNFIXED code for each role — the
 * preservation oracle. PortalAdmin gets the admin group plus audit logs;
 * UseCaseAdmin gets a divider plus audit logs; every other role (and the
 * role-less/loading state) gets the base list only.
 */
function PRE_FIX_NAV_ORACLE(role: UserRole | undefined): NavItem[] {
  if (role === 'PortalAdmin') {
    return [
      ...PRE_FIX_BASE_ITEMS,
      ...PRE_FIX_PORTAL_ADMIN_ITEMS,
      PRE_FIX_AUDIT_LOGS_ITEM,
    ];
  }
  if (role === 'UseCaseAdmin') {
    return [
      ...PRE_FIX_BASE_ITEMS,
      { type: 'divider' },
      PRE_FIX_AUDIT_LOGS_ITEM,
    ];
  }
  return [...PRE_FIX_BASE_ITEMS];
}

/** Drops any "Builds" entry — the one item the fix is allowed to change. */
function withoutBuildsEntry(items: readonly NavItem[]): NavItem[] {
  return items.filter(
    (item) => !(item.type === 'link' && item.text === 'Builds')
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

/** Roles holding `builds:*` per the merged backend matrix — these keep the
 *  builds surface (Req 3.6). */
const buildsRoleArb: fc.Arbitrary<UserRole> = fc.constantFrom<UserRole>(
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin'
);

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

/** Renders Layout for `role` and returns the navigation item list it built. */
function navItemsFor(role: UserRole | undefined): NavItem[] {
  setRole(role);
  navCapture.items = null;
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Routes>
        <Route path="*" element={<Layout />} />
      </Routes>
    </MemoryRouter>
  );
  const items = navCapture.items as NavItem[] | null;
  if (items == null) {
    throw new Error(`role=${role}: Layout produced no navigation items`);
  }
  // Normalize to the comparable shape (type/text/href) so incidental extra
  // props on future items do not make the oracle brittle.
  return items.map((item) =>
    item.type === 'divider'
      ? { type: 'divider' }
      : { type: 'link', text: item.text, href: item.href }
  );
}

/** Navigates the real App route tree to `path` as `role` and reports whether
 *  the guarded page mounted. */
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
  // Cloudscape's TopNavigation needs these browser APIs, which jsdom lacks.
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
  navCapture.items = null;
  setRole('Viewer');
});

// ----------------------------------------------------------------- tests

describe('Property 4: Preservation — builds-capable roles keep their UI, non-builds nav items unchanged', () => {
  it('for every role, the nav item list minus any "Builds" entry equals the pre-fix oracle minus any "Builds" entry', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        try {
          const actual = withoutBuildsEntry(navItemsFor(role));
          const expected = withoutBuildsEntry(PRE_FIX_NAV_ORACLE(role));
          // All other items, dividers, and ordering identical (Req 3.6).
          expect(actual, `role=${role}: navigation items`).toEqual(expected);
        } finally {
          cleanup();
        }
      }),
      { numRuns: 12 }
    );
  });

  it('the "Build Fleet" item is present if and only if the role is PortalAdmin', () => {
    fc.assert(
      fc.property(roleArb, (role) => {
        try {
          const hasBuildFleet = navItemsFor(role).some(
            (item) => item.type === 'link' && item.text === 'Build Fleet'
          );
          expect(hasBuildFleet, `role=${role}: "Build Fleet" item`).toBe(
            role === 'PortalAdmin'
          );
        } finally {
          cleanup();
        }
      }),
      { numRuns: 12 }
    );
  });

  it('DataScientist / UseCaseAdmin / PortalAdmin keep the "Builds" nav item', () => {
    fc.assert(
      fc.property(buildsRoleArb, (role) => {
        try {
          const hasBuilds = navItemsFor(role).some(
            (item) =>
              item.type === 'link' &&
              item.text === 'Builds' &&
              item.href === '/builds'
          );
          expect(hasBuilds, `role=${role}: "Builds" item`).toBe(true);
        } finally {
          cleanup();
        }
      }),
      { numRuns: 9 }
    );
  });

  it('DataScientist / UseCaseAdmin / PortalAdmin can render /builds and /builds/:buildJobId', () => {
    fc.assert(
      fc.property(
        buildsRoleArb,
        fc.constantFrom(
          ['/builds', 'BUILDS_PAGE_MOUNTED'] as const,
          ['/builds/job-1', 'BUILD_DETAIL_MOUNTED'] as const
        ),
        (role, [path, sentinel]) => {
          try {
            expect(
              pageMounts(role, path, sentinel),
              `role=${role}: ${path} render`
            ).toBe(true);
          } finally {
            cleanup();
          }
        }
      ),
      { numRuns: 12 }
    );
  });

  it('/admin/fleet renders FleetPage for PortalAdmin', () => {
    try {
      expect(
        pageMounts('PortalAdmin', '/admin/fleet', 'FLEET_PAGE_MOUNTED'),
        'role=PortalAdmin: /admin/fleet render'
      ).toBe(true);
    } finally {
      cleanup();
    }
  });
});
