/**
 * Unit tests for the Now panel (workflow-telemetry-dashboards task 5.3,
 * Requirements 11.1-11.3; design §9.1).
 *
 * With fake timers and a planned `now` fetcher:
 * - a Summary followed by Heartbeats keeps the values and reads `idle` (the
 *   two-pointer rule: the values come from LAST#engine, the flow from LASTHB#);
 * - "last summary <age>", "last run <age>" or "no run in 48 h", the Heartbeat
 *   age, the newest Event ages and the Events list;
 * - each flow state as a StatusIndicator;
 * - polling every `pollSeconds`, stopping when hidden, resuming when visible;
 * - "telemetry is off" when the settings say so;
 * - the controlled mode renders the given answer without polling.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import NowPanel from './NowPanel';
import { NO_RUN_48H, TELEMETRY_OFF, newestSummaries } from './nowData';
import type { DashboardNowResponse } from '../../pages/telemetry/types';
import type { NowFetcher } from '../../hooks/useDashboardNow';
import {
  NOW,
  installResizeObserver,
  nowAnswer,
  nowDevice,
  pointer,
  summary,
} from '../../pages/telemetry/__fixtures__/telemetryFixtures';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

function plannedFetcher(plan: Array<DashboardNowResponse | Error>, fallback?: DashboardNowResponse) {
  const fetchNow = vi.fn<NowFetcher>(async () => {
    const planned = plan.shift() ?? fallback ?? nowAnswer();
    if (planned instanceof Error) throw planned;
    return planned;
  });
  return fetchNow;
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeAll(() => {
  installResizeObserver();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  setVisibility('visible');
});

afterEach(() => {
  vi.useRealTimers();
  setVisibility('visible');
});

/** A device whose newest Summary ran 58 runs, then three Heartbeats later. */
function afterSummary(): DashboardNowResponse {
  return nowAnswer({
    devices: {
      'thing-a': nowDevice({
        flow: 'flowing',
        ages: { lastSummaryAgeS: 8, lastRunAgeS: 12, heartbeatAgeS: null, noRunIn48h: false },
        last: { 'LAST#engine': pointer({ receivedMs: NOW - 8_000, lastRunMs: NOW - 12_000, summary: summary() }) },
      }),
    },
  });
}

function afterHeartbeats(): DashboardNowResponse {
  return nowAnswer({
    updatedAt: NOW + 15_000,
    devices: {
      'thing-a': nowDevice({
        flow: 'idle',
        ages: { lastSummaryAgeS: 23, lastRunAgeS: 27, heartbeatAgeS: 5, noRunIn48h: false },
        last: {
          'LAST#engine': pointer({ receivedMs: NOW - 8_000, lastRunMs: NOW - 12_000, summary: summary() }),
          'LASTHB#engine': pointer({ receivedMs: NOW + 10_000, seq: 44, lastRunMs: null, summary: null }),
        },
      }),
    },
  });
}

describe('NowPanel — the two pointers (design §9.1)', () => {
  it('a Summary followed by Heartbeats keeps the values and reads idle', async () => {
    const fetchNow = plannedFetcher([afterSummary(), afterHeartbeats()], afterHeartbeats());
    render(<NowPanel dashboardId="dash-1" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);

    expect(fetchNow).toHaveBeenCalledWith('dash-1');
    expect(screen.getByTestId('telemetry-now-thing-a-flow').textContent).toBe('flowing');
    expect(screen.getByTestId('telemetry-now-thing-a-runs').textContent).toBe('58 completed · 1 failed · 2 dropped');
    expect(screen.getByTestId('telemetry-now-thing-a-last-summary').textContent).toBe('8 s ago');
    expect(screen.getByTestId('telemetry-now-thing-a-last-run').textContent).toBe('12 s ago');
    expect(screen.getByTestId('telemetry-now-thing-a-heartbeat').textContent).toBe('—');

    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);
    // The Heartbeats changed the flow and the ages, never the values.
    expect(screen.getByTestId('telemetry-now-thing-a-flow').textContent).toBe('idle');
    expect(screen.getByTestId('telemetry-now-thing-a-runs').textContent).toBe('58 completed · 1 failed · 2 dropped');
    expect(screen.getByTestId('telemetry-now-thing-a-detections').textContent).toBe('140 · helmet 90 · human 50');
    expect(screen.getByTestId('telemetry-now-thing-a-people').textContent).toBe('2');
    expect(screen.getByTestId('telemetry-now-thing-a-latency').textContent).toBe('120 ms / 310 ms');
    expect(screen.getByTestId('telemetry-now-thing-a-model').textContent).toBe('READY');
    expect(screen.getByTestId('telemetry-now-thing-a-last-summary').textContent).toBe('23 s ago');
    expect(screen.getByTestId('telemetry-now-thing-a-last-run').textContent).toBe('27 s ago');
    expect(screen.getByTestId('telemetry-now-thing-a-heartbeat').textContent).toBe('5 s ago');
  });

  it('reads "no run in 48 h" when lastRunMs is absent', async () => {
    const answer = nowAnswer({
      devices: {
        'thing-a': nowDevice({
          flow: 'idle',
          ages: { lastSummaryAgeS: 3600, lastRunAgeS: null, heartbeatAgeS: 30, noRunIn48h: true },
          last: { 'LAST#engine': pointer({ lastRunMs: null }), 'LASTHB#engine': pointer({ summary: null, lastRunMs: null }) },
        }),
      },
    });
    render(<NowPanel dashboardId="dash-1" now={answer} />);
    expect(screen.getByTestId('telemetry-now-thing-a-last-run').textContent).toBe(NO_RUN_48H);
    expect(screen.getByTestId('telemetry-now-thing-a-last-summary').textContent).toBe('1 h ago');
  });

  it.each([
    ['flowing', 'flowing'],
    ['idle', 'idle'],
    ['silent', 'silent'],
  ] as const)('shows the flow state %s', (flow, label) => {
    render(<NowPanel dashboardId="dash-1" now={nowAnswer({ devices: { 'thing-a': nowDevice({ flow }) } })} />);
    expect(screen.getByTestId('telemetry-now-thing-a-flow').textContent).toBe(label);
  });

  it('renders a device without any pointer as dashes', () => {
    render(
      <NowPanel
        dashboardId="dash-1"
        now={nowAnswer({
          devices: {
            'thing-a': nowDevice({
              flow: 'silent',
              ages: { lastSummaryAgeS: null, lastRunAgeS: null, heartbeatAgeS: null, noRunIn48h: true },
              last: {},
              producers: [],
            }),
          },
        })}
      />
    );
    expect(screen.getByTestId('telemetry-now-thing-a-runs').textContent).toBe('—');
    expect(screen.getByTestId('telemetry-now-thing-a-last-summary').textContent).toBe('—');
    expect(screen.getByTestId('telemetry-now-thing-a-last-run').textContent).toBe(NO_RUN_48H);
  });
});

describe('NowPanel — the events', () => {
  it('lists the newest events with their ages and a summary line', () => {
    const answer = nowAnswer({
      devices: {
        'thing-a': nowDevice({
          live: { ageS: 2, frameToken: 'inst:9' },
          events: [
            { eventId: 'e1', type: 'violation_confirmed', eventMs: NOW - 30_000, receivedMs: NOW - 29_000, severity: 'warning', data: { reasons: ['no-helmet'], people: 2, violatingPeople: 1 } },
            { eventId: 'e2', type: 'clip_ready', eventMs: NOW - 90_000, receivedMs: NOW - 89_000, severity: 'info', data: { clipId: 'clip-0001', durationS: 12 } },
            { eventId: 'e3', type: 'run_error', eventMs: NOW - 600_000, receivedMs: NOW - 599_000, severity: 'error', data: { error: 'Triton timed out', count: 3, node: 'detector' } },
          ],
        }),
      },
    });
    render(<NowPanel dashboardId="dash-1" now={answer} />);
    const ages = screen.getByTestId('telemetry-now-thing-a-event-ages').textContent!;
    expect(ages).toContain('violation_confirmed 30 s ago');
    expect(ages).toContain('clip_ready 2 min ago');
    expect(ages).toContain('run_error 10 min ago');
    const list = screen.getByTestId('telemetry-now-thing-a-events').textContent!;
    expect(list).toContain('no-helmet · 1 people');
    expect(list).toContain('clip-0001 · 12 s');
    expect(list).toContain('detector · Triton timed out · ×3');
    expect(screen.getByTestId('telemetry-now-thing-a-live').textContent).toBe('2 s ago');
  });

  it('says "none in 15 min" without events', () => {
    render(<NowPanel dashboardId="dash-1" now={nowAnswer({ devices: { 'thing-a': nowDevice() } })} />);
    expect(screen.getByTestId('telemetry-now-thing-a-event-ages').textContent).toBe('none in 15 min');
    expect(screen.queryByTestId('telemetry-now-thing-a-events')).toBeNull();
  });
});

describe('NowPanel — polling and visibility', () => {
  it('polls every pollSeconds while visible, stops when hidden, resumes at once when visible', async () => {
    const fetchNow = plannedFetcher([], nowAnswer());
    render(<NowPanel dashboardId="dash-1" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);
    expect(fetchNow).toHaveBeenCalledTimes(1);
    await advance(4_000);
    expect(fetchNow).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);
    await advance(10_000);
    expect(fetchNow).toHaveBeenCalledTimes(4);

    act(() => setVisibility('hidden'));
    await advance(60_000);
    expect(fetchNow).toHaveBeenCalledTimes(4);

    act(() => setVisibility('visible'));
    await advance(0);
    expect(fetchNow).toHaveBeenCalledTimes(5);
    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(6);
  });

  it('uses the given pollSeconds (the schemas value) rather than a fixed 5', async () => {
    const fetchNow = plannedFetcher([], nowAnswer());
    render(<NowPanel dashboardId="dash-1" pollSeconds={10} fetchNow={fetchNow} />);
    await advance(0);
    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(1);
    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);
  });

  it('stops on unmount', async () => {
    const fetchNow = plannedFetcher([], nowAnswer());
    const view = render(<NowPanel dashboardId="dash-1" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);
    view.unmount();
    await advance(60_000);
    expect(fetchNow).toHaveBeenCalledTimes(1);
  });

  it('shows a read failure and keeps polling', async () => {
    const fetchNow = plannedFetcher([new Error('boom')], nowAnswer());
    render(<NowPanel dashboardId="dash-1" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);
    expect(screen.getByTestId('telemetry-now-error').textContent).toContain('boom');
    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('telemetry-now-error')).toBeNull();
  });

  it('in the controlled mode renders the given answer and never polls', async () => {
    const fetchNow = plannedFetcher([], nowAnswer());
    render(<NowPanel dashboardId="dash-1" pollSeconds={5} now={nowAnswer()} fetchNow={fetchNow} />);
    await advance(30_000);
    expect(fetchNow).not.toHaveBeenCalled();
    expect(screen.getByTestId('telemetry-now-thing-a')).toBeInTheDocument();
    expect(screen.getByTestId('telemetry-now-thing-b-flow').textContent).toBe('idle');
  });
});

describe('NowPanel — telemetry off', () => {
  it('shows "telemetry is off" when the settings say so', () => {
    render(<NowPanel dashboardId="dash-1" now={nowAnswer({ enabled: false })} />);
    expect(screen.getByTestId('telemetry-now-off').textContent).toContain(TELEMETRY_OFF);
  });

  it('shows no such notice while telemetry is on', () => {
    render(<NowPanel dashboardId="dash-1" now={nowAnswer({ enabled: true })} />);
    expect(screen.queryByTestId('telemetry-now-off')).toBeNull();
  });
});

describe('newestSummaries (the "not reported" input of the charts)', () => {
  it('lists every LAST# pointer with its producer and skips the Heartbeats', () => {
    const list = newestSummaries(afterHeartbeats());
    expect(list).toHaveLength(1);
    expect(list[0].producer).toBe('engine');
    expect(list[0].summary?.runs?.completed).toBe(58);
    expect(newestSummaries(null)).toEqual([]);
  });
});
