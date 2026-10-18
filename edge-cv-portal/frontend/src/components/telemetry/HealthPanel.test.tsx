/**
 * Unit tests for the Health panel (workflow-telemetry-dashboards task 5.3,
 * Requirements 11.1-11.3; design §9.1).
 *
 * - `flow`, `gaps`, the `telemetry.*` counters, failed and dropped runs, the
 *   model state with its waits, the core-device status;
 * - the `x86_64_nvidia` variant hint text when the answer carries it, and
 *   nothing otherwise;
 * - the rejection block for a UseCaseAdmin / PortalAdmin with an answer that
 *   carries `rejections`, hidden for a Viewer;
 * - polling start and stop on visibility; "telemetry is off".
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import HealthPanel from './HealthPanel';
import { TELEMETRY_OFF } from './nowData';
import type { DashboardNowResponse } from '../../pages/telemetry/types';
import type { NowFetcher } from '../../hooks/useDashboardNow';
import type { UserRole } from '../../types';
import { NOW, installResizeObserver, nowAnswer, nowDevice } from '../../pages/telemetry/__fixtures__/telemetryFixtures';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

const VARIANT_HINT =
  'Publish failures climb while the flow is silent and the device is the amd64Nvidia LocalServer variant (x86_64_nvidia): that recipe lacks the IPC publish statement, so the agent cannot publish; the variant is unsupported until the recipe gains it.';

function withRejections(): DashboardNowResponse {
  return nowAnswer({
    rejections: {
      counts: { schema_invalid: 4, rate_exceeded: 1 },
      minutes: [],
      samples: [],
      note: 'The rate_exceeded guard is per ingest container.',
      usecase_id: 'uc-1',
      scope: 'usecase',
    },
  });
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

describe('HealthPanel — the values', () => {
  it('renders flow, gaps, the counters, the runs, the model state with its waits and the core device', () => {
    const answer = nowAnswer({
      devices: {
        'thing-a': nowDevice({
          lastHour: {
            measures: { runs_failed: 3, runs_dropped: 4, telemetry_queue_drops: 2, telemetry_publish_failures: 5, telemetry_events_dropped: 1, model_waits: 2, model_wait_ms: 90_000 },
            events: {},
            gaps: 7,
            minutes: 58,
          },
        }),
      },
    });
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={answer} />);
    expect(screen.getByTestId('telemetry-health-thing-a-flow').textContent).toBe('flowing');
    expect(screen.getByTestId('telemetry-health-thing-a-gaps').textContent).toBe('7');
    expect(screen.getByTestId('telemetry-health-thing-a-counters').textContent).toBe('2 · 5 · 1');
    expect(screen.getByTestId('telemetry-health-thing-a-runs').textContent).toBe('3 · 4');
    expect(screen.getByTestId('telemetry-health-thing-a-model').textContent).toBe('READY');
    expect(screen.getByTestId('telemetry-health-thing-a-model-waits').textContent).toBe('2 wait(s) · 2 min');
    expect(screen.getByTestId('telemetry-health-thing-a-core').textContent).toBe('HEALTHY');
  });

  it.each([
    ['HEALTHY', 'HEALTHY'],
    ['UNHEALTHY', 'UNHEALTHY'],
    ['UNKNOWN', 'UNKNOWN (AccessDeniedException)'],
  ] as const)('shows the core-device status %s', (status, label) => {
    const answer = nowAnswer({
      devices: { 'thing-a': nowDevice({ coreDevice: { status, error: status === 'UNKNOWN' ? 'AccessDeniedException' : undefined } }) },
    });
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={answer} />);
    expect(screen.getByTestId('telemetry-health-thing-a-core').textContent).toBe(label);
  });

  it('shows "unknown" when the core-device status is missing', () => {
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={nowAnswer({ devices: { 'thing-a': nowDevice({ coreDevice: null }) } })} />);
    expect(screen.getByTestId('telemetry-health-thing-a-core').textContent).toBe('unknown');
  });

  it('shows the silent flow of a device without pointers', () => {
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={nowAnswer({ devices: { 'thing-a': nowDevice({ flow: 'silent', last: {} }) } })} />);
    expect(screen.getByTestId('telemetry-health-thing-a-flow').textContent).toBe('silent');
    expect(screen.getByTestId('telemetry-health-thing-a-model').textContent).toBe('—');
  });
});

describe('HealthPanel — the variant hint (design §9.1, R11.2)', () => {
  it('shows the hint text when the answer carries it', () => {
    const answer = nowAnswer({
      devices: { 'thing-a': nowDevice({ flow: 'silent', targetArchitecture: 'x86_64_nvidia', variantHint: VARIANT_HINT }) },
    });
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={answer} />);
    expect(screen.getByTestId('telemetry-health-thing-a-variant-hint').textContent).toBe(VARIANT_HINT);
  });

  it('shows no hint for another architecture', () => {
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={nowAnswer({ devices: { 'thing-a': nowDevice({ flow: 'silent', targetArchitecture: 'x86_64', variantHint: null }) } })} />);
    expect(screen.queryByTestId('telemetry-health-thing-a-variant-hint')).toBeNull();
  });
});

describe('HealthPanel — the rejection block (telemetry:manage only)', () => {
  it.each<UserRole>(['UseCaseAdmin', 'PortalAdmin'])('shows the counts for %s', (role) => {
    render(<HealthPanel dashboardId="dash-1" role={role} now={withRejections()} />);
    const block = screen.getByTestId('telemetry-health-rejections');
    expect(block.textContent).toContain('schema_invalid');
    expect(block.textContent).toContain('4');
    expect(block.textContent).toContain('rate_exceeded');
    expect(block.textContent).toContain('per ingest container');
  });

  it.each<UserRole>(['Viewer', 'Operator', 'DataScientist'])('hides the block for %s even when the answer carries it', (role) => {
    render(<HealthPanel dashboardId="dash-1" role={role} now={withRejections()} />);
    expect(screen.queryByTestId('telemetry-health-rejections')).toBeNull();
  });

  it('hides the block when the answer carries no rejections', () => {
    render(<HealthPanel dashboardId="dash-1" role="UseCaseAdmin" now={nowAnswer()} />);
    expect(screen.queryByTestId('telemetry-health-rejections')).toBeNull();
  });

  it('says "none in the last hour" for an empty count map', () => {
    const answer = withRejections();
    answer.rejections!.counts = {};
    render(<HealthPanel dashboardId="dash-1" role="UseCaseAdmin" now={answer} />);
    expect(screen.getByTestId('telemetry-health-rejections').textContent).toContain('none in the last hour');
  });
});

describe('HealthPanel — polling, visibility and telemetry off', () => {
  it('polls every pollSeconds while visible, stops when hidden, resumes when visible', async () => {
    const fetchNow = vi.fn<NowFetcher>(async () => nowAnswer());
    render(<HealthPanel dashboardId="dash-1" role="Viewer" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);
    expect(fetchNow).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('telemetry-health-thing-a')).toBeInTheDocument();
    await advance(5_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);

    act(() => setVisibility('hidden'));
    await advance(30_000);
    expect(fetchNow).toHaveBeenCalledTimes(2);

    act(() => setVisibility('visible'));
    await advance(0);
    expect(fetchNow).toHaveBeenCalledTimes(3);
  });

  it('in the controlled mode never polls', async () => {
    const fetchNow = vi.fn<NowFetcher>(async () => nowAnswer());
    render(<HealthPanel dashboardId="dash-1" role="Viewer" pollSeconds={5} now={nowAnswer()} fetchNow={fetchNow} />);
    await advance(30_000);
    expect(fetchNow).not.toHaveBeenCalled();
  });

  it('shows "telemetry is off" when the settings say so', () => {
    render(<HealthPanel dashboardId="dash-1" role="Viewer" now={nowAnswer({ enabled: false })} />);
    expect(screen.getByTestId('telemetry-health-off').textContent).toContain(TELEMETRY_OFF);
  });

  it('shows a read failure', async () => {
    const fetchNow = vi.fn<NowFetcher>(async () => {
      throw new Error('boom');
    });
    render(<HealthPanel dashboardId="dash-1" role="Viewer" pollSeconds={5} fetchNow={fetchNow} />);
    await advance(0);
    expect(screen.getByTestId('telemetry-health-error').textContent).toContain('boom');
  });
});
