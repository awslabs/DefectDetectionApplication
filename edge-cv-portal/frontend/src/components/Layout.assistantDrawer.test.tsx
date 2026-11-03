/**
 * The assistant drawer in the authenticated Layout (spec portal-ai-assistant
 * Requirements 1.1, 1.2, 1.5, 1.6, 2.1; design §12).
 *
 * The AppLayout is stubbed to expose the drawer props it receives, so the
 * tests check what the Layout asks Cloudscape to render: the drawer with
 * its trigger on an authenticated route, no drawer for a DataLabeler-only
 * user, no drawer without `assistantApiUrl`, and the open/close round trip
 * through `onDrawerChange`.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { configMock, useAuthMock, apiMock } = vi.hoisted(() => ({
  configMock: { current: {} as Record<string, unknown> },
  useAuthMock: vi.fn(),
  apiMock: { state: vi.fn(), listConversations: vi.fn(), getConversation: vi.fn() },
}));

vi.mock('../config', () => ({
  getConfig: () => configMock.current,
  getBuildInfo: () => ({ version: 'test', gitSha: 'abc1234', buildTime: '' }),
}));
vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));
vi.mock('../contexts/UsecaseContext', () => ({
  useOptionalUsecase: () => ({ selectedUsecaseId: null, setSelectedUsecaseId: vi.fn() }),
}));
vi.mock('./assistant/assistantApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./assistant/assistantApi')>();
  return {
    ...actual,
    assistantConfigured: () => typeof configMock.current.assistantApiUrl === 'string',
    assistantApi: { ...apiMock },
  };
});
vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@cloudscape-design/components')>();
  return {
    ...actual,
    TopNavigation: () => <div data-testid="top-nav" />,
    AppLayout: ({ drawers, activeDrawerId, onDrawerChange, content }: any) => (
      <div>
        <div data-testid="drawer-ids">{(drawers ?? []).map((d: any) => d.id).join(',')}</div>
        <div data-testid="active-drawer">{activeDrawerId ?? 'none'}</div>
        <div data-testid="drawer-badge">{String((drawers ?? [])[0]?.badge ?? '')}</div>
        <div data-testid="drawer-trigger">{(drawers ?? [])[0]?.trigger?.iconName ?? ''}</div>
        {(drawers ?? []).map((d: any) => (
          <button key={d.id} onClick={() => onDrawerChange?.({ detail: { activeDrawerId: activeDrawerId ? null : d.id } })}>
            {d.ariaLabels?.triggerButton}
          </button>
        ))}
        <div data-testid="content">{content}</div>
      </div>
    ),
  };
});

import Layout from './Layout';

function mountLayout(route = '/devices') {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route path="devices" element={<div>Devices page</div>} />
          <Route path="workflows/builder" element={<div>Builder page</div>} />
          <Route path="labeler" element={<div>Labeler page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

const admin = { user_id: 'sub-1', email: 'a@example.com', username: 'a', role: 'UseCaseAdmin', is_super_user: false };

beforeEach(() => {
  configMock.current = { assistantApiUrl: 'https://assistant.example.test/v1', branding: { applicationName: 'DDA' } };
  useAuthMock.mockReturnValue({ user: admin, logout: vi.fn(), changePassword: vi.fn() });
  localStorage.clear();
  sessionStorage.clear();
  Object.defineProperty(window, 'innerWidth', { value: 1600, configurable: true });
  apiMock.state.mockReset().mockResolvedValue({ enabled: true, actions_enabled: false, version: '1.0.0' });
  apiMock.listConversations.mockReset().mockResolvedValue({ items: [] });
});

describe('Layout assistant drawer', () => {
  it('renders the drawer with the gen-ai trigger on an authenticated route, open on a wide first visit', async () => {
    mountLayout('/devices');
    await waitFor(() => expect(screen.getByTestId('drawer-ids').textContent).toBe('assistant'));
    expect(screen.getByTestId('drawer-trigger').textContent).toBe('gen-ai');
    expect(screen.getByTestId('active-drawer').textContent).toBe('assistant');
    expect(screen.getByText('Devices page')).toBeInTheDocument();
  });

  it('closes and reopens through onDrawerChange and remembers the choice', async () => {
    mountLayout('/devices');
    await waitFor(() => expect(screen.getByTestId('drawer-ids').textContent).toBe('assistant'));
    fireEvent.click(screen.getByText('Open the assistant'));
    expect(screen.getByTestId('active-drawer').textContent).toBe('none');
    expect(localStorage.getItem('dda-assistant-drawer')).toBe('closed');
    fireEvent.click(screen.getByText('Open the assistant'));
    expect(screen.getByTestId('active-drawer').textContent).toBe('assistant');
    expect(localStorage.getItem('dda-assistant-drawer')).toBe('open');
  });

  it('starts closed on the workflow builder', async () => {
    mountLayout('/workflows/builder');
    await waitFor(() => expect(screen.getByTestId('drawer-ids').textContent).toBe('assistant'));
    expect(screen.getByTestId('active-drawer').textContent).toBe('none');
  });

  it('renders no drawer for a DataLabeler-only user', async () => {
    useAuthMock.mockReturnValue({ user: { ...admin, role: 'DataLabeler' }, logout: vi.fn(), changePassword: vi.fn() });
    mountLayout('/labeler');
    await act(async () => {});
    expect(screen.getByTestId('drawer-ids').textContent).toBe('');
    expect(apiMock.state).not.toHaveBeenCalled();
  });

  it('renders no drawer and makes no request when the assistant is not deployed', async () => {
    configMock.current = { branding: { applicationName: 'DDA' } };
    mountLayout('/devices');
    await act(async () => {});
    expect(screen.getByTestId('drawer-ids').textContent).toBe('');
    expect(apiMock.state).not.toHaveBeenCalled();
  });

  it('removes the drawer when the state poll reports the assistant disabled', async () => {
    apiMock.state.mockResolvedValue({ enabled: false, actions_enabled: false, reason: 'disabled_by_admin', version: '1.0.0' });
    mountLayout('/devices');
    await waitFor(() => expect(apiMock.state).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId('drawer-ids').textContent).toBe(''));
  });
});
