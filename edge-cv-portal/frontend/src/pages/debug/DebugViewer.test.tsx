/**
 * Unit tests for the debug log viewer (workflow-debug-mode task 5.3,
 * design §8 and §10.1; Requirements 6.3–6.6).
 *
 * - the first poll asks the last 15 minutes of write time at limit=2000;
 * - the body is ordered by the record's own `ts`, run records as cards and
 *   the others as lines, a late write badged;
 * - each filter reaches the next poll's query (level, type, execution id,
 *   node id);
 * - the session badge from the use case's sessions;
 * - the rate-limited notice after a 429 and the catching-up state;
 * - "Open in CloudWatch" only for a PortalAdmin, calling the console-link
 *   route and opening the URL;
 * - "Download NDJSON" pages the window at limit=10000 and offers the file.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ReactNode } from 'react';
import type { DebugEvent, DebugLogQuery, DebugLogResponse, DebugSession } from './types';
import type { UserRole } from '../../types';

const { navigateMock, useAuthMock } = vi.hoisted(() => ({ navigateMock: vi.fn(), useAuthMock: vi.fn() }));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => ({ usecaseId: 'uc-1', workflowId: 'wf-1', deviceId: 'thing-a' }),
  };
});

vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: useAuthMock,
}));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    apiService: {
      listDebugSessions: vi.fn(),
      getDebugLog: vi.fn(),
      getDebugConsoleLink: vi.fn(),
      getDebugImageUrl: vi.fn(),
    },
  };
});

import { apiService, DebugApiError } from '../../services/api';
import DebugViewer, {
  CATCHING_UP_NOTICE,
  DEFAULT_RANGE,
  NO_SESSION_NOTICE,
  RATE_LIMITED_NOTICE,
  WINDOW_TOO_LONG,
  rangeToWindow,
  sessionForPair,
  validateRange,
} from './DebugViewer';
import { TAIL_PAGE_LIMIT, TAIL_POLL_MS, TAIL_WINDOW_MS, DOWNLOAD_PAGE_LIMIT } from '../../hooks/useDebugTail';

const mocked = vi.mocked(apiService);
const NOW = 1_792_075_062_000;

function session(overrides: Partial<DebugSession> = {}): DebugSession {
  return {
    session_id: 'sess-1',
    usecase_id: 'uc-1',
    workflow_id: 'wf-1',
    workflow_version: '1',
    device_ids: ['thing-a'],
    all_devices: false,
    state: 'active',
    enabledAt: NOW - 60_000,
    expiresAt: NOW + 30 * 60_000,
    stoppedAt: null,
    actor: 'operator@example.com',
    options: { level: 'debug', images: false, imageMaxKb: 150, maxMessagesPerSecond: 10 },
    counters: { records: 3, bytes: 300 },
    logGroup: '/dda/workflow-debug/uc-1',
    media: {},
    devices: [{ device_id: 'thing-a', state: 'active', shadow: { result: 'ok' }, logStream: 'thing-a/wf-1', reported: null }],
    ...overrides,
  };
}

function logEvent(id: string, ts: number, message: string, writeTs = ts + 100, receivedMs = ts + 50): DebugEvent {
  return { id, writeTs, receivedMs, ts, record: { type: 'log', ts, level: 'INFO', logger: 'engine', message } };
}

function runEvent(id: string, ts: number, executionId: string): DebugEvent {
  return {
    id,
    writeTs: ts + 100,
    receivedMs: ts + 50,
    ts,
    record: { type: 'run', ts, executionId, status: 'completed', durationMs: 1500, trigger: { type: 'manual', source: null, topic: null, payload: null, timestamp: null, receivedMs: null } },
  };
}

function setRole(role: UserRole) {
  useAuthMock.mockReturnValue({
    user: { user_id: 'user-1', email: 'user@example.com', username: 'user', role, is_super_user: role === 'PortalAdmin' },
  });
}

/** `getDebugLog` answers from a queue; afterwards empty pages. */
function planLog(plan: Array<DebugLogResponse | Error>) {
  const queries: DebugLogQuery[] = [];
  mocked.getDebugLog.mockImplementation(async (_uc, query) => {
    queries.push(query);
    const planned = plan.shift();
    if (planned === undefined) return { events: [], next: null };
    if (planned instanceof Error) throw planned;
    return planned;
  });
  return queries;
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function renderViewer() {
  render(<DebugViewer />);
  await advance(0);
}

const body = () => createWrapper(document.body);

beforeAll(() => {
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
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  setRole('Operator');
  mocked.listDebugSessions.mockResolvedValue({ sessions: [session()], next: null });
  planLog([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('DebugViewer — the first window and the body ordering (design §8)', () => {
  it('asks the last 15 minutes of write time at limit=2000 for the Pair', async () => {
    const queries = planLog([{ events: [], next: null }]);
    await renderViewer();

    expect(mocked.getDebugLog).toHaveBeenCalledTimes(1);
    expect(mocked.getDebugLog.mock.calls[0][0]).toBe('uc-1');
    expect(queries[0]).toMatchObject({
      device_id: 'thing-a',
      workflow_id: 'wf-1',
      from: NOW - TAIL_WINDOW_MS,
      to: NOW,
      limit: TAIL_PAGE_LIMIT,
    });
    expect(screen.getByTestId('debug-status').textContent).toContain('Tail on · polling every 3 s');
  });

  it('orders the events by ts, renders runs as cards and the rest as lines, and badges a late write', async () => {
    planLog([
      {
        events: [
          logEvent('c', NOW - 1000, 'third'),
          runEvent('b', NOW - 2000, 'exec-b'),
          logEvent('a', NOW - 3000, 'first', NOW + 100, NOW - 200_000),
        ],
        next: null,
      },
    ]);
    await renderViewer();

    const container = screen.getByTestId('debug-events');
    const ids = Array.from(container.querySelectorAll('[data-event-id], [data-testid^="debug-run-"]')).map(
      (el) => el.getAttribute('data-event-id') ?? el.getAttribute('data-testid')
    );
    expect(ids).toEqual(['a', 'debug-run-exec-b', 'c']);
    expect(screen.getByText(/INFO engine — first/)).toBeInTheDocument();
    expect(screen.getByText(/run exec-b completed · manual · pipeline 1\.50 s/)).toBeInTheDocument();
    // 'a' was written 200 s after receipt: late.
    expect(screen.getByText('late')).toBeInTheDocument();
    expect(screen.getByTestId('debug-status').textContent).toContain('3 event(s)');
  });

  it('shows the empty state before any event arrives', async () => {
    await renderViewer();
    expect(screen.getByTestId('debug-empty').textContent).toContain('No events in the window yet');
  });
});

describe('DebugViewer — filters (design §8)', () => {
  it('sends the level and type filters with the next poll', async () => {
    const queries = planLog([]);
    await renderViewer();

    const level = body().findSelect('[data-testid="debug-filter-level"]')!;
    level.openDropdown();
    level.selectOptionByValue('ERROR');
    await advance(0);
    expect(queries.at(-1)).toMatchObject({ level: 'ERROR' });
    expect(queries.at(-1)!.type).toBeUndefined();

    const type = body().findSelect('[data-testid="debug-filter-type"]')!;
    type.openDropdown();
    type.selectOptionByValue('run');
    await advance(0);
    expect(queries.at(-1)).toMatchObject({ level: 'ERROR', type: 'run' });
  });

  it('applies the execution id and node id filters on Apply and re-anchors the window', async () => {
    const queries = planLog([]);
    await renderViewer();
    const calls = mocked.getDebugLog.mock.calls.length;

    body().findInput('[data-testid="debug-filter-execution"]')!.setInputValue(' exec-7 ');
    body().findInput('[data-testid="debug-filter-node"]')!.setInputValue('detector');
    // Typing alone does not query.
    expect(mocked.getDebugLog).toHaveBeenCalledTimes(calls);

    await act(async () => {
      fireEvent.click(screen.getByTestId('debug-filter-apply'));
    });
    await advance(0);

    expect(queries.at(-1)).toMatchObject({ execution_id: 'exec-7', node_id: 'detector', from: NOW - TAIL_WINDOW_MS, to: NOW });
  });

  it('switching the tail off reads a fixed window once', async () => {
    const queries = planLog([]);
    await renderViewer();

    body().findToggle('[data-testid="debug-tail-toggle"]')!.findNativeInput().click();
    await advance(0);
    const fixed = queries.at(-1)!;
    expect(fixed).toMatchObject({ from: NOW - TAIL_WINDOW_MS, to: NOW });
    expect(screen.getByTestId('debug-status').textContent).toContain('Fixed window');

    const calls = mocked.getDebugLog.mock.calls.length;
    await advance(5 * TAIL_POLL_MS);
    expect(mocked.getDebugLog).toHaveBeenCalledTimes(calls);
  });
});

describe('DebugViewer — the session badge, the 429 notice and catching up', () => {
  it('shows the Pair session state and remaining time', async () => {
    await renderViewer();
    expect(mocked.listDebugSessions).toHaveBeenCalledWith('uc-1');
    expect(screen.getByTestId('debug-session-badge').textContent).toContain('Active · 30 min left');
    expect(screen.getByText(/session sess-1 · expires/)).toBeInTheDocument();
  });

  it('shows "No debug session" when the use case has none for the Pair', async () => {
    mocked.listDebugSessions.mockResolvedValue({ sessions: [session({ workflow_id: 'other' })], next: null });
    planLog([new DebugApiError('none', 404, { error: 'none', error_code: 'no_debug_session' })]);
    await renderViewer();

    expect(screen.getByTestId('debug-session-badge').textContent).toContain('No debug session');
    expect(screen.getByTestId('debug-no-session').textContent).toBe(NO_SESSION_NOTICE);
  });

  it('shows the rate-limited notice while backing off to 10 s', async () => {
    planLog([
      { events: [], next: null },
      new DebugApiError('rate-limited', 429, { error: 'rate-limited', error_code: 'log_read_throttled', retry_after_ms: 750 }),
    ]);
    await renderViewer();
    expect(screen.queryByTestId('debug-rate-limited')).toBeNull();

    await advance(TAIL_POLL_MS);
    expect(screen.getByTestId('debug-rate-limited').textContent).toBe(RATE_LIMITED_NOTICE);
    expect(screen.getByTestId('debug-status').textContent).toContain('polling every 10 s');
  });

  it('shows the catching-up state when a poll hits its call budget', async () => {
    planLog([
      { events: [logEvent('a', NOW - 3000, 'a')], next: 't1' },
      { events: [logEvent('b', NOW - 2000, 'b')], next: 't2' },
      { events: [logEvent('c', NOW - 1000, 'c')], next: 't3' },
    ]);
    await renderViewer();

    expect(screen.getByTestId('debug-catching-up').textContent).toBe(CATCHING_UP_NOTICE);
  });
});

describe('DebugViewer — the console link (PortalAdmin only, design §7.2)', () => {
  it('is absent for an Operator', async () => {
    setRole('Operator');
    await renderViewer();
    expect(screen.queryByTestId('debug-console-link')).toBeNull();
  });

  it('is present for a PortalAdmin, calls the route and opens the URL', async () => {
    setRole('PortalAdmin');
    mocked.getDebugConsoleLink.mockResolvedValue({
      url: 'https://example.com/console#logsV2',
      region: 'us-east-1',
      logGroup: '/dda/workflow-debug/uc-1',
      logStream: 'thing-a/wf-1',
      note: 'The console timestamp column is the write time.',
    });
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    await renderViewer();

    await act(async () => {
      fireEvent.click(screen.getByTestId('debug-console-link'));
    });

    expect(mocked.getDebugConsoleLink).toHaveBeenCalledWith('sess-1', 'thing-a');
    expect(open).toHaveBeenCalledWith('https://example.com/console#logsV2', '_blank', 'noopener,noreferrer');
    expect(screen.getByTestId('debug-console-note').textContent).toContain('thing-a/wf-1 in us-east-1');
    expect(screen.getByTestId('debug-console-note').textContent).toContain('write time');
  });
});

describe('DebugViewer — Download NDJSON (design §8 step 4)', () => {
  it('pages the window at limit=10000 and offers the file', async () => {
    const queries = planLog([
      { events: [], next: null }, // the tail's first poll
      { events: [logEvent('a', NOW - 1000, 'a'), logEvent('b', NOW - 500, 'b')], next: null }, // the download
    ]);
    const createObjectURL = vi.fn(() => 'blob:debug');
    const revokeObjectURL = vi.fn();
    (URL as any).createObjectURL = createObjectURL;
    (URL as any).revokeObjectURL = revokeObjectURL;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    await renderViewer();

    await act(async () => {
      fireEvent.click(screen.getByTestId('debug-download'));
    });
    await advance(0);

    const downloadQuery = queries.find((q) => q.limit === DOWNLOAD_PAGE_LIMIT)!;
    expect(downloadQuery).toMatchObject({ device_id: 'thing-a', workflow_id: 'wf-1', from: NOW - TAIL_WINDOW_MS, to: NOW });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:debug');
    expect(screen.getByTestId('debug-download-note').textContent).toContain('2 event(s) in 1 call(s)');

    click.mockRestore();
    delete (URL as any).createObjectURL;
    delete (URL as any).revokeObjectURL;
  });
});

describe('window helpers', () => {
  it('rangeToWindow tails a relative range and fixes an absolute one', () => {
    expect(rangeToWindow(DEFAULT_RANGE, true, NOW)).toEqual({ tail: true, from: NOW - 15 * 60_000 });
    expect(rangeToWindow(DEFAULT_RANGE, false, NOW)).toEqual({ tail: false, from: NOW - 15 * 60_000, to: NOW });
    expect(rangeToWindow({ type: 'relative', amount: 2, unit: 'hour' }, true, NOW)).toEqual({ tail: true, from: NOW - 2 * 3_600_000 });
    expect(
      rangeToWindow({ type: 'absolute', startDate: '2026-10-15T10:00:00Z', endDate: '2026-10-15T11:00:00Z' }, true, NOW)
    ).toEqual({ tail: false, from: Date.parse('2026-10-15T10:00:00Z'), to: Date.parse('2026-10-15T11:00:00Z') });
    expect(rangeToWindow(null, true, NOW)).toEqual({ tail: true, from: NOW - 15 * 60_000 });
  });

  it('validateRange caps the window at 24 hours', () => {
    expect(validateRange(DEFAULT_RANGE)).toEqual({ valid: true });
    expect(validateRange({ type: 'relative', amount: 24, unit: 'hour' })).toEqual({ valid: true });
    expect(validateRange({ type: 'relative', amount: 2, unit: 'day' })).toEqual({ valid: false, errorMessage: WINDOW_TOO_LONG });
    expect(validateRange({ type: 'absolute', startDate: '2026-10-15T10:00:00Z', endDate: '2026-10-16T10:00:01Z' })).toEqual({
      valid: false,
      errorMessage: WINDOW_TOO_LONG,
    });
    expect(validateRange({ type: 'absolute', startDate: '2026-10-15T10:00:00Z', endDate: '2026-10-15T09:00:00Z' }).valid).toBe(false);
  });

  it('sessionForPair prefers a live session and otherwise the newest', () => {
    const old = session({ session_id: 'old', state: 'expired', enabledAt: 1 });
    const newer = session({ session_id: 'newer', state: 'stopped', enabledAt: 2 });
    const live = session({ session_id: 'live', state: 'pending', enabledAt: 0 });
    expect(sessionForPair([old, newer], 'wf-1', 'thing-a')?.session_id).toBe('newer');
    expect(sessionForPair([old, newer, live], 'wf-1', 'thing-a')?.session_id).toBe('live');
    expect(sessionForPair([old], 'wf-1', 'thing-z')).toBeNull();
    expect(sessionForPair([old], 'wf-9', 'thing-a')).toBeNull();
  });
});
