/**
 * Unit tests for the debug sessions page (workflow-debug-mode task 5.2,
 * Requirements 2.6, 3.4).
 *
 * - the table from a fixture answer: workflow name, devices with shadow
 *   result and reported state, the state badge with the remaining time,
 *   the actor and the options;
 * - the enforcement-off banner on and off (design §7.2 "two switches");
 * - "Stop" calls `stopDebugSession` and reloads;
 * - "Start debug" hidden for a Viewer, shown for an Operator;
 * - the variant notice for an `x86_64_nvidia` device with publish failures,
 *   and no notice for another architecture.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { DebugSession } from './types';
import type { UserRole } from '../../types';

const { navigateMock, useAuthMock, isRegistryEnforcedMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  useAuthMock: vi.fn(),
  isRegistryEnforcedMock: vi.fn(() => false),
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => ({ usecaseId: 'uc-1' }),
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

vi.mock('../../contexts/UsecaseContext', () => ({
  UsecaseProvider: ({ children }: { children: ReactNode }) => children,
  useUsecase: () => ({ selectedUsecaseId: 'uc-1', setSelectedUsecaseId: vi.fn() }),
}));

vi.mock('../../config', () => ({
  isRegistryEnforced: isRegistryEnforcedMock,
  getConfig: () => ({ apiUrl: 'http://localhost/api', region: 'us-east-1', branding: {} }),
}));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      listUseCases: vi.fn(),
      listDebugSessions: vi.fn(),
      listWorkflows: vi.fn(),
      listDevices: vi.fn(),
      stopDebugSession: vi.fn(),
      createDebugSession: vi.fn(),
    },
  };
});

import { apiService, DebugApiError } from '../../services/api';
import DebugSessions, { ENFORCEMENT_OFF_BANNER } from './DebugSessions';
import { startedNotice } from '../../components/debug/StartDebugModal';
import { VARIANT_NOTICE } from '../../components/debug/DebugSessionsTable';

const mocked = vi.mocked(apiService);

const NOW = 1_792_075_062_000;

function session(overrides: Partial<DebugSession> = {}): DebugSession {
  return {
    session_id: 'sess-1',
    usecase_id: 'uc-1',
    workflow_id: 'wf-1',
    workflow_version: '3',
    device_ids: ['thing-a', 'thing-b'],
    all_devices: false,
    state: 'active',
    enabledAt: NOW - 10 * 60_000,
    expiresAt: NOW + 42 * 60_000,
    stoppedAt: null,
    actor: 'operator@example.com',
    options: { level: 'debug', images: false, imageMaxKb: 150, maxMessagesPerSecond: 10, maxBytesPerSecond: 262144 },
    counters: { records: 1234, bytes: 2048 },
    logGroup: '/dda/workflow-debug/uc-1',
    media: { bucket: 'bucket', region: 'us-east-1', region_source: 'header' },
    devices: [
      {
        device_id: 'thing-a',
        state: 'active',
        shadow: { result: 'ok', writtenAt: NOW - 10 * 60_000 },
        logStream: 'thing-a/wf-1',
        reported: { sessionId: 'sess-1', state: 'active', counters: { records: 1000, publishFailures: 0 } },
      },
      {
        device_id: 'thing-b',
        state: 'active',
        shadow: { result: 'failed', error: 'thing_not_found' },
        logStream: 'thing-b/wf-1',
        reported: null,
      },
    ],
    ...overrides,
  };
}

function setRole(role: UserRole) {
  useAuthMock.mockReturnValue({
    user: { user_id: 'user-1', email: 'user@example.com', username: 'user', role, is_super_user: false },
  });
}

async function renderPage() {
  render(<DebugSessions />);
  await waitFor(() => expect(mocked.listDebugSessions).toHaveBeenCalled());
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
  isRegistryEnforcedMock.mockReturnValue(false);
  setRole('Operator');
  mocked.listUseCases.mockResolvedValue({ usecases: [{ usecase_id: 'uc-1', name: 'Castings' } as any], count: 1 });
  mocked.listDebugSessions.mockResolvedValue({ sessions: [session()], next: null });
  mocked.listWorkflows.mockResolvedValue({
    workflows: [{ workflow_id: 'wf-1', name: 'PPE vest check' } as any],
    count: 1,
  });
  mocked.listDevices.mockResolvedValue({
    devices: [
      { device_id: 'thing-a', target_architecture: 'arm64_jp5' } as any,
      { device_id: 'thing-b', target_architecture: 'arm64_jp5' } as any,
    ],
    count: 2,
  });
  mocked.stopDebugSession.mockResolvedValue({
    session_id: 'sess-1',
    state: 'stopped',
    devices: [{ device_id: 'thing-a', shadow: 'ok' }, { device_id: 'thing-b', shadow: 'ok' }],
    stoppedAt: NOW,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DebugSessions — the sessions table', () => {
  it('renders the fixture session with workflow, devices, state, times, actor and options', async () => {
    await renderPage();

    expect(mocked.listDebugSessions).toHaveBeenCalledWith('uc-1');
    expect(screen.getByText('PPE vest check')).toBeInTheDocument();
    expect(screen.getByText(/wf-1 v3/)).toBeInTheDocument();
    expect(screen.getByText('thing-a')).toBeInTheDocument();
    expect(screen.getByText('thing-b')).toBeInTheDocument();
    // Shadow result and reported state per device.
    expect(screen.getByTestId('debug-session-device-thing-a').textContent).toContain('shadow ok');
    expect(screen.getByTestId('debug-session-device-thing-a').textContent).toContain('reported active');
    expect(screen.getByTestId('debug-session-device-thing-a').textContent).toContain('1000 records');
    expect(screen.getByTestId('debug-session-device-thing-b').textContent).toContain('shadow failed (thing_not_found)');
    expect(screen.getByTestId('debug-session-device-thing-b').textContent).toContain('not reported');
    // State badge with the remaining time (42 minutes left at NOW).
    expect(screen.getByText(/Active · 42 min left/)).toBeInTheDocument();
    expect(screen.getByText('operator@example.com')).toBeInTheDocument();
    expect(screen.getByText(/level debug · no images · 10 msg\/s · 256\.0 KB\/s/)).toBeInTheDocument();
    expect(screen.getByText(/1234 records · 2\.0 KB/)).toBeInTheDocument();
  });

  it('links each device to its log viewer route', async () => {
    await renderPage();

    const link = screen.getByText('thing-a').closest('a');
    expect(link).not.toBeNull();
    expect(link!.getAttribute('href')).toBe('/debug/uc-1/wf-1/thing-a');
    fireEvent.click(link!);
    expect(navigateMock).toHaveBeenCalledWith('/debug/uc-1/wf-1/thing-a');
  });

  it('shows the empty state when the use case has no sessions', async () => {
    mocked.listDebugSessions.mockResolvedValue({ sessions: [], next: null });
    await renderPage();

    expect(screen.getByText('No debug sessions')).toBeInTheDocument();
  });
});

describe('DebugSessions — the enforcement-off banner (design §7.2)', () => {
  it('shows the banner while registry enforcement is off', async () => {
    isRegistryEnforcedMock.mockReturnValue(false);
    await renderPage();

    expect(screen.getByTestId('enforcement-off-banner').textContent).toContain(ENFORCEMENT_OFF_BANNER);
  });

  it('hides the banner when registry enforcement is on', async () => {
    isRegistryEnforcedMock.mockReturnValue(true);
    await renderPage();

    expect(screen.queryByTestId('enforcement-off-banner')).toBeNull();
  });
});

describe('DebugSessions — start and stop', () => {
  it('hides "Start debug" and "Stop" for a Viewer', async () => {
    setRole('Viewer');
    await renderPage();

    expect(screen.queryByTestId('start-debug-button')).toBeNull();
    expect(screen.queryByTestId('debug-stop-sess-1')).toBeNull();
  });

  it('shows "Start debug" for an Operator and opens the modal', async () => {
    setRole('Operator');
    await renderPage();

    const start = screen.getByTestId('start-debug-button');
    expect(start).toBeInTheDocument();
    fireEvent.click(start);
    await waitFor(() => expect(screen.getByTestId('start-debug-modal')).toBeInTheDocument());
  });

  it('"Stop" calls stopDebugSession and reloads the list', async () => {
    await renderPage();
    expect(mocked.listDebugSessions).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByTestId('debug-stop-sess-1'));
    });

    expect(mocked.stopDebugSession).toHaveBeenCalledWith('sess-1');
    await waitFor(() => expect(mocked.listDebugSessions).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('debug-notice').textContent).toContain('Session sess-1 stopped.');
  });

  it('shows a stop refusal with its remedy', async () => {
    mocked.stopDebugSession.mockRejectedValue(
      new DebugApiError('The session is not live', 409, {
        error: 'The session is not live',
        error_code: 'debug_session_not_live',
        state: 'expired',
        remedy: 'Refresh the list',
      })
    );
    await renderPage();

    await act(async () => {
      fireEvent.click(screen.getByTestId('debug-stop-sess-1'));
    });

    expect(screen.getByText(/The session is not live Refresh the list/)).toBeInTheDocument();
  });

  it('offers no "Stop" for a session that is not live', async () => {
    mocked.listDebugSessions.mockResolvedValue({
      sessions: [session({ state: 'stopped', stoppedAt: NOW - 60_000 })],
      next: null,
    });
    await renderPage();

    expect(screen.queryByTestId('debug-stop-sess-1')).toBeNull();
    expect(screen.getByText('Stopped')).toBeInTheDocument();
  });
});

describe('DebugSessions — the variant notice (design §5.6)', () => {
  it('shows the notice for an x86_64_nvidia device whose publishFailures climb', async () => {
    mocked.listDevices.mockResolvedValue({
      devices: [{ device_id: 'thing-a', target_architecture: 'x86_64_nvidia' } as any],
      count: 1,
    });
    mocked.listDebugSessions.mockResolvedValue({
      sessions: [
        session({
          devices: [
            {
              device_id: 'thing-a',
              state: 'active',
              shadow: { result: 'ok' },
              logStream: 'thing-a/wf-1',
              reported: { state: 'active', counters: { records: 0, publishFailures: 17 } },
            },
          ],
        }),
      ],
      next: null,
    });
    await renderPage();

    await waitFor(() => expect(screen.getByTestId('debug-variant-notice')).toBeInTheDocument());
    expect(screen.getByTestId('debug-variant-notice').textContent).toContain(VARIANT_NOTICE);
    expect(screen.getByTestId('debug-variant-notice').textContent).toContain('thing-a');
  });

  it('shows no notice for another architecture with the same failures', async () => {
    mocked.listDevices.mockResolvedValue({
      devices: [{ device_id: 'thing-a', target_architecture: 'arm64_jp5' } as any],
      count: 1,
    });
    mocked.listDebugSessions.mockResolvedValue({
      sessions: [
        session({
          devices: [
            {
              device_id: 'thing-a',
              state: 'active',
              shadow: { result: 'ok' },
              logStream: 'thing-a/wf-1',
              reported: { state: 'active', counters: { publishFailures: 17 } },
            },
          ],
        }),
      ],
      next: null,
    });
    await renderPage();
    await act(async () => {});

    expect(screen.queryByTestId('debug-variant-notice')).toBeNull();
  });
});

describe('startedNotice', () => {
  it('describes an active session', () => {
    expect(
      startedNotice({
        session_id: 's',
        state: 'active',
        devices: [{ device_id: 'a', shadow: 'ok' }],
        region_source: 'header',
        enabledAt: 1,
        expiresAt: 2,
        logGroup: 'g',
      })
    ).toBe('Session s is active on 1 device(s).');
  });

  it('names the failed shadow writes and the region warning of a pending session', () => {
    const text = startedNotice({
      session_id: 's',
      state: 'pending',
      devices: [
        { device_id: 'a', shadow: 'ok' },
        { device_id: 'b', shadow: 'failed', error: 'throttled' },
      ],
      region_source: 'usecase',
      warning: 'The results bucket region could not be read',
      enabledAt: 1,
      expiresAt: 2,
      logGroup: 'g',
    });
    expect(text).toContain('pending: 1 shadow write(s) failed');
    expect(text).toContain('Failed: b (throttled).');
    expect(text).toContain('The results bucket region could not be read.');
  });
});
