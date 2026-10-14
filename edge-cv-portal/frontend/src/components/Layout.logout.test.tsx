/**
 * Unit tests for the deliberate sign-out path in `components/Layout.tsx`
 * (spec: portal-session-expiry-return-to-page, task 3.4).
 *
 * Signing out on purpose is a clean start: any remembered Attempted_Location
 * is forgotten before the navigation to `/login`, so the next sign-in lands on
 * the default landing page (Requirements 4.1, 4.2). The ordering matters —
 * `logout()` flips `isAuthenticated` while the router is still on a protected
 * route, so clearing *before* the await would let `ProtectedRoute` record the
 * location again; these tests pin "after `logout()`, before `navigate()`".
 *
 * Cloudscape's `TopNavigation` collapses its utilities in jsdom's zero-width
 * layout, so the menu item itself is unclickable there. The component is
 * stubbed with a button that forwards a click to the real `onItemClick`
 * handler `Layout` passes it, so the assertions exercise `Layout`'s own
 * handler rather than a copy of it.
 *
 * Validates: Requirements 4.1, 4.2
 */

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

const { navigateMock, useAuthMock, logoutMock, events } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  logoutMock: vi.fn(),
  events: [] as Array<{ step: string; remembered: string | null }>,
}));

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

// Keep the real router (Layout renders an <Outlet/>), but observe the
// destination the sign-out handler chooses.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}));

// Drive the real dropdown handler through a clickable stub.
vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@cloudscape-design/components')>();
  return {
    ...actual,
    TopNavigation: ({ utilities }: any) => {
      const menu = utilities?.[0];
      const items: Array<{ id: string; text?: string }> = menu?.items ?? [];
      return (
        <div>
          {items.map((item) => (
            <button
              key={item.id}
              data-testid={`menu-${item.id}`}
              onClick={() => menu?.onItemClick?.({ detail: { id: item.id } })}
            >
              {item.text ?? item.id}
            </button>
          ))}
        </div>
      );
    },
  };
});

import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Layout from './Layout';
import { RETURN_TO_KEY } from '../services/sessionRedirect';

// --------------------------------------------------------------- harness

const DEEP_LOCATION = '/workflows/builder/abc?tab=nodes#latest';

function remembered(): string | null {
  return window.sessionStorage.getItem(RETURN_TO_KEY);
}

function setAuth() {
  useAuthMock.mockReturnValue({
    user: {
      user_id: 'user-1',
      email: 'user@example.com',
      username: 'user',
      role: 'DataScientist',
      is_super_user: false,
    },
    isAuthenticated: true,
    isLoading: false,
    needsNewPassword: false,
    login: vi.fn(),
    completeNewPassword: vi.fn(),
    changePassword: vi.fn(),
    forgotPassword: vi.fn(),
    forgotPasswordSubmit: vi.fn(),
    logout: logoutMock,
    error: null,
  });
}

function renderLayout() {
  setAuth();
  return render(
    <MemoryRouter initialEntries={[DEEP_LOCATION]}>
      <Routes>
        <Route path="*" element={<Layout />} />
      </Routes>
    </MemoryRouter>
  );
}

/** Clicks a dropdown item by its id and waits for the handler to settle. */
async function clickMenuItem(id: string) {
  fireEvent.click(screen.getByTestId(`menu-${id}`));
  await waitFor(() => expect(navigateMock).toHaveBeenCalled());
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
  events.length = 0;
  window.sessionStorage.clear();
  logoutMock.mockImplementation(async () => {
    events.push({ step: 'logout', remembered: remembered() });
  });
  navigateMock.mockImplementation(() => {
    events.push({ step: 'navigate', remembered: remembered() });
  });
});

// ------------------------------------------------------------- sign-out

describe('Layout — deliberate sign-out', () => {
  it('clears the remembered location and navigates to /login', async () => {
    window.sessionStorage.setItem(RETURN_TO_KEY, DEEP_LOCATION);

    renderLayout();
    await clickMenuItem('logout');

    expect(logoutMock).toHaveBeenCalledTimes(1);
    expect(remembered()).toBeNull();
    expect(navigateMock).toHaveBeenCalledWith('/login');
  });

  it('clears after logout() has resolved and before navigating', async () => {
    window.sessionStorage.setItem(RETURN_TO_KEY, DEEP_LOCATION);

    renderLayout();
    await clickMenuItem('logout');

    // Clearing before `await logout()` would let the re-render that `logout()`
    // triggers have `ProtectedRoute` record the location again.
    expect(events).toEqual([
      { step: 'logout', remembered: DEEP_LOCATION },
      { step: 'navigate', remembered: null },
    ]);
  });

  it('is a no-op on the store when nothing was remembered', async () => {
    renderLayout();
    await clickMenuItem('logout');

    expect(remembered()).toBeNull();
    expect(navigateMock).toHaveBeenCalledWith('/login');
  });

  it('leaves unrelated sessionStorage keys alone', async () => {
    window.sessionStorage.setItem(RETURN_TO_KEY, DEEP_LOCATION);
    window.sessionStorage.setItem('dda.portal.somethingElse', 'keep-me');

    renderLayout();
    await clickMenuItem('logout');

    expect(remembered()).toBeNull();
    expect(window.sessionStorage.getItem('dda.portal.somethingElse')).toBe('keep-me');
  });

  it('does not clear the remembered location for other dropdown items', async () => {
    window.sessionStorage.setItem(RETURN_TO_KEY, DEEP_LOCATION);

    renderLayout();
    await clickMenuItem('settings');

    expect(logoutMock).not.toHaveBeenCalled();
    expect(navigateMock).toHaveBeenCalledWith('/settings');
    expect(remembered()).toBe(DEEP_LOCATION);
  });
});
