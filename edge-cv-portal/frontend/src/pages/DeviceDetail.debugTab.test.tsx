/**
 * Tests for the DeviceDetail "Debug" tab (workflow-debug-mode task 5.4,
 * design §8; Requirements 2.6, 3.1).
 *
 * - the tab is listed and, deep-linked with ?tab=debug, renders the
 *   device's sessions from a mocked answer (sessions of other devices are
 *   left out) with the use case taken from the device record;
 * - "Start debug" is shown for an Operator (and opens the modal with the
 *   device preselected) and hidden for a Viewer.
 *
 * The other tabs are stubbed as the sibling DeviceDetail tests do; the
 * Overview tab is not exercised here.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { UserRole } from '../types';

import DeviceDetail from './DeviceDetail';

const { getDevice, listDebugSessions, listWorkflows, listDevices, stopDebugSession, useAuthMock, isRegistryEnforcedMock } =
  vi.hoisted(() => ({
    getDevice: vi.fn(),
    listDebugSessions: vi.fn(),
    listWorkflows: vi.fn(),
    listDevices: vi.fn(),
    stopDebugSession: vi.fn(),
    useAuthMock: vi.fn(),
    isRegistryEnforcedMock: vi.fn(() => false),
  }));

vi.mock('../services/api', () => {
  const apiService = new Proxy(
    { getDevice, listDebugSessions, listWorkflows, listDevices, stopDebugSession },
    {
      get(target, prop) {
        if (prop in target) {
          return target[prop as keyof typeof target];
        }
        return (..._args: unknown[]) => Promise.resolve({});
      },
    }
  );
  return { apiService, isDebugApiError: () => false };
});

vi.mock('../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

vi.mock('../config', () => ({
  isRegistryEnforced: isRegistryEnforcedMock,
  getConfig: () => ({ apiUrl: 'http://localhost/api', region: 'us-east-1', branding: {} }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ deviceId: 'thing-a' }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams('usecase_id=usecase-1&tab=debug'), vi.fn()],
}));

// The camera/logs/remote-access/results tabs pull heavy dependencies; stub
// them — this test exercises only the Debug tab.
vi.mock('../components/DeviceCamerasTab', () => ({ default: () => null }));
vi.mock('../components/LogsDiagnosticsTab', () => ({ default: () => null }));
vi.mock('../components/RemoteAccessTab', () => ({ default: () => null }));
vi.mock('../components/ResultsTab', () => ({ default: () => null }));

const NOW = 1_792_075_062_000;

const device = {
  device_id: 'thing-a',
  usecase_id: 'usecase-1',
  thing_name: 'thing-a',
  status: 'HEALTHY',
  installed_components: [],
  test_device: false,
  target_architecture: 'arm64_jp5',
};

function sessionFor(deviceId: string, sessionId: string) {
  return {
    session_id: sessionId,
    usecase_id: 'usecase-1',
    workflow_id: 'wf-1',
    workflow_version: '1',
    device_ids: [deviceId],
    all_devices: false,
    state: 'active',
    enabledAt: NOW - 60_000,
    expiresAt: NOW + 20 * 60_000,
    stoppedAt: null,
    actor: 'operator@example.com',
    options: { level: 'debug', images: false, imageMaxKb: 150, maxMessagesPerSecond: 10 },
    counters: { records: 5, bytes: 500 },
    logGroup: '/dda/workflow-debug/usecase-1',
    media: {},
    devices: [{ device_id: deviceId, state: 'active', shadow: { result: 'ok' }, logStream: `${deviceId}/wf-1`, reported: null }],
  };
}

function setRole(role: UserRole) {
  useAuthMock.mockReturnValue({
    user: { user_id: 'user-1', email: 'user@example.com', username: 'user', role, is_super_user: false },
  });
}

async function renderPage() {
  getDevice.mockResolvedValue({ device });
  render(<DeviceDetail />);
  await act(async () => {});
  await act(async () => {});
}

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(Date, 'now').mockReturnValue(NOW);
  setRole('Operator');
  listDebugSessions.mockResolvedValue({
    sessions: [sessionFor('thing-a', 'sess-mine'), sessionFor('thing-b', 'sess-other')],
    next: null,
  });
  listWorkflows.mockResolvedValue({ workflows: [{ workflow_id: 'wf-1', name: 'PPE vest check' }], count: 1 });
  listDevices.mockResolvedValue({ devices: [device], count: 1 });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DeviceDetail — the Debug tab', () => {
  it('lists the Debug tab and renders the device sessions from the mocked answer', async () => {
    await renderPage();

    expect(screen.getByRole('tab', { name: 'Debug' })).toBeInTheDocument();
    // The use case comes from the device record.
    expect(listDebugSessions).toHaveBeenCalledWith('usecase-1');
    // Only the sessions covering this device, with the state badge and the actor.
    expect(screen.getByTestId('debug-sessions-table')).toBeInTheDocument();
    expect(screen.getByTestId('debug-session-device-thing-a')).toBeInTheDocument();
    expect(screen.queryByTestId('debug-session-device-thing-b')).toBeNull();
    expect(screen.getByText(/Active · 20 min left/)).toBeInTheDocument();
    expect(screen.getByText('operator@example.com')).toBeInTheDocument();
    expect(screen.getByText('(1)')).toBeInTheDocument();
  });

  it('shows "Start debug" for an Operator and opens the modal with the device preselected', async () => {
    setRole('Operator');
    await renderPage();

    const start = screen.getByTestId('device-start-debug');
    expect(start).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(start);
    });

    expect(screen.getByTestId('start-debug-modal')).toBeInTheDocument();
    expect(listWorkflows).toHaveBeenCalledWith('usecase-1');
    expect(screen.getByTestId('start-debug-device').textContent).toContain('thing-a');
  });

  it('hides "Start debug" and "Stop" for a Viewer', async () => {
    setRole('Viewer');
    await renderPage();

    expect(screen.queryByTestId('device-start-debug')).toBeNull();
    expect(screen.queryByTestId('debug-stop-sess-mine')).toBeNull();
    expect(screen.getByTestId('debug-session-device-thing-a')).toBeInTheDocument();
  });

  it('shows the empty state when no session covers the device', async () => {
    listDebugSessions.mockResolvedValue({ sessions: [sessionFor('thing-b', 'sess-other')], next: null });
    await renderPage();

    expect(screen.getByText('No debug sessions for this device')).toBeInTheDocument();
  });
});
