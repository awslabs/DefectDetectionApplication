/**
 * Unit tests for the series panel (workflow-telemetry-dashboards task 5.2,
 * Requirements 1.4, 9.3, 9.4; design §8.3).
 *
 * With fake timers:
 * - exactly ONE `getDashboardSeries` request per Dashboard per refresh for
 *   each of the three templates, with neither `device_id` nor `measures`;
 * - the cadence by the answer's resolution: 60 s at `minute`, 300 s at
 *   `hour`, 900 s at `day` (and the schemas' cadence table when given);
 * - the manual refresh rate-limited to one per 10 s;
 * - polling stops while the document is hidden and resumes at once;
 * - a 400 `range_too_long` is shown as a notice;
 * - the picker validation and the range arithmetic.
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { TelemetryApiError } from '../../services/api';
import SeriesPanel, {
  RANGE_TOO_LONG_NOTICE,
  RELATIVE_OPTIONS,
  rangeWindow,
  refreshMs,
  relativeRangeMs,
  validateRange,
  type SeriesFetcher,
} from './SeriesPanel';
import type { DashboardSeriesResponse, SeriesQuery } from '../../pages/telemetry/types';
import {
  NOW,
  SCHEMAS,
  TEMPLATE_IDS,
  dashboard,
  installResizeObserver,
  seriesAnswer,
  template,
} from '../../pages/telemetry/__fixtures__/telemetryFixtures';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

const ORDER = SCHEMAS.registry.modelStateOrder;

function plannedFetcher(answer: (query: SeriesQuery) => DashboardSeriesResponse | Error) {
  const queries: SeriesQuery[] = [];
  const fetchSeries = vi.fn<SeriesFetcher>(async (_id, query) => {
    queries.push(query);
    const planned = answer(query);
    if (planned instanceof Error) throw planned;
    return planned;
  });
  return { fetchSeries, queries };
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

function mount(
  fetchSeries: SeriesFetcher,
  options: { templateId?: (typeof TEMPLATE_IDS)[number]; initialRange?: Parameters<typeof SeriesPanel>[0]['initialRange']; cadence?: Parameters<typeof SeriesPanel>[0]['cadence'] } = {}
) {
  const id = options.templateId ?? 'generic-detector';
  return render(
    <SeriesPanel
      dashboard={dashboard({ template_id: id })}
      template={template(id)}
      catalog={SCHEMAS.catalog}
      modelStateOrder={ORDER}
      cadence={options.cadence}
      initialRange={options.initialRange}
      fetchSeries={fetchSeries}
    />
  );
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

describe('the range arithmetic and validation', () => {
  it('relative ranges span amount × unit and end now', () => {
    expect(relativeRangeMs({ type: 'relative', amount: 15, unit: 'minute' })).toBe(15 * 60_000);
    expect(rangeWindow({ type: 'relative', amount: 6, unit: 'hour' }, NOW)).toEqual({ from: NOW - 6 * 3_600_000, to: NOW });
    expect(rangeWindow(null, NOW)).toEqual({ from: NOW - 15 * 60_000, to: NOW });
  });

  it('absolute ranges are fixed', () => {
    const value = { type: 'absolute', startDate: '2026-10-01T00:00:00Z', endDate: '2026-10-02T00:00:00Z' } as const;
    expect(rangeWindow(value, NOW)).toEqual({ from: Date.parse(value.startDate), to: Date.parse(value.endDate) });
  });

  it('offers the six relative presets of design §8.3', () => {
    expect(RELATIVE_OPTIONS.map((o) => `${o.amount} ${o.unit}`)).toEqual([
      '15 minute', '1 hour', '6 hour', '24 hour', '7 day', '30 day',
    ]);
  });

  it('accepts up to 400 days and refuses beyond, or an inverted absolute range', () => {
    expect(validateRange({ type: 'relative', amount: 400, unit: 'day' }).valid).toBe(true);
    const tooLong = validateRange({ type: 'relative', amount: 401, unit: 'day' });
    expect(tooLong.valid).toBe(false);
    expect((tooLong as { errorMessage: string }).errorMessage).toBe(RANGE_TOO_LONG_NOTICE);
    expect(validateRange({ type: 'absolute', startDate: '2026-10-02T00:00:00Z', endDate: '2026-10-01T00:00:00Z' }).valid).toBe(false);
    expect(validateRange({ type: 'absolute', startDate: '2025-09-01T00:00:00Z', endDate: '2026-10-18T00:00:00Z' }).valid).toBe(false);
    expect(validateRange(null).valid).toBe(true);
  });

  it('refreshMs follows the cadence table, else the 60/300/900 defaults', () => {
    expect(refreshMs('minute')).toBe(60_000);
    expect(refreshMs('hour')).toBe(300_000);
    expect(refreshMs('day')).toBe(900_000);
    expect(refreshMs('minute', { series: { minute: 30, hour: 120, day: 600 } })).toBe(30_000);
  });
});

describe('one series request per Dashboard per refresh (design §8.3)', () => {
  it.each(TEMPLATE_IDS)('%s: one request at mount, one more per 60 s refresh, never device_id or measures', async (id) => {
    const { fetchSeries, queries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries, { templateId: id });
    await advance(0);

    expect(fetchSeries).toHaveBeenCalledTimes(1);
    expect(fetchSeries.mock.calls[0][0]).toBe('dash-1');
    expect(queries[0]).toEqual({ from: NOW - 15 * 60_000, to: NOW });
    expect(queries[0]).not.toHaveProperty('device_id');
    expect(queries[0]).not.toHaveProperty('measures');

    await advance(59_000);
    expect(fetchSeries).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    expect(queries[1]).toEqual({ from: NOW + 60_000 - 15 * 60_000, to: NOW + 60_000 });
    await advance(60_000);
    expect(fetchSeries).toHaveBeenCalledTimes(3);
    for (const query of queries) {
      expect(Object.keys(query).sort()).toEqual(['from', 'to']);
    }
    // Every panel of the template rendered from that one answer.
    for (const panel of template(id).panels) {
      expect(screen.getByTestId(`telemetry-panel-${panel.id}`)).toBeInTheDocument();
    }
  });

  it('refreshes every 300 s when the answer is at hour resolution', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer({ resolution: 'hour', buckets: 24 }));
    mount(fetchSeries, { initialRange: { type: 'relative', amount: 24, unit: 'hour' } });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(1);

    await advance(299_000);
    expect(fetchSeries).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('telemetry-series-panel').textContent).toContain('hour resolution');
    expect(screen.getByTestId('telemetry-series-panel').textContent).toContain('refresh every 300 s');
  });

  it('refreshes every 900 s when the answer is at day resolution', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer({ resolution: 'day', buckets: 30 }));
    mount(fetchSeries, { initialRange: { type: 'relative', amount: 90, unit: 'day' } });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(1);

    await advance(899_000);
    expect(fetchSeries).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('telemetry-series-panel').textContent).toContain('day resolution');
  });

  it('uses the schemas cadence table when given', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries, { cadence: { series: { minute: 30, hour: 300, day: 900 }, manualRefreshMin: 10 } });
    await advance(0);
    await advance(30_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
  });

  it('shows the resolution and "updated <age>" in the panel headers', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer({ updatedAt: NOW }));
    mount(fetchSeries);
    await advance(0);
    const runs = screen.getByTestId('telemetry-panel-runs');
    expect(runs.textContent).toContain('minute resolution');
    expect(runs.textContent).toMatch(/updated \d+ s ago/);
  });
});

describe('the manual refresh is rate-limited to one per 10 s', () => {
  it('a second click within 10 s makes no request; after 10 s it does', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries);
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-series-refresh'));
    });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(2);

    await advance(3_000);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-series-refresh'));
    });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    expect((screen.getByTestId('telemetry-series-refresh') as HTMLButtonElement).disabled).toBe(true);

    await advance(7_000);
    expect((screen.getByTestId('telemetry-series-refresh') as HTMLButtonElement).disabled).toBe(false);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-series-refresh'));
    });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(3);
  });

  it('a manual refresh re-arms the periodic refresh from its own instant', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries);
    await advance(0);
    await advance(30_000);
    await act(async () => {
      fireEvent.click(screen.getByTestId('telemetry-series-refresh'));
    });
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    await advance(30_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    await advance(30_000);
    expect(fetchSeries).toHaveBeenCalledTimes(3);
  });
});

describe('visibility', () => {
  it('stops refreshing while hidden and refreshes at once when visible again', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries);
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(1);

    act(() => setVisibility('hidden'));
    await advance(180_000);
    expect(fetchSeries).toHaveBeenCalledTimes(1);

    act(() => setVisibility('visible'));
    await advance(0);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    await advance(60_000);
    expect(fetchSeries).toHaveBeenCalledTimes(3);
  });

  it('stops on unmount', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    const view = mount(fetchSeries);
    await advance(0);
    view.unmount();
    await advance(600_000);
    expect(fetchSeries).toHaveBeenCalledTimes(1);
  });
});

describe('refusals', () => {
  it('shows a 400 range_too_long as a notice and keeps polling off that range', async () => {
    const { fetchSeries } = plannedFetcher(
      () => new TelemetryApiError('too long', 400, { error: 'too long', error_code: 'range_too_long', max_days: 400 })
    );
    mount(fetchSeries);
    await advance(0);
    expect(screen.getByTestId('telemetry-series-notice').textContent).toContain(RANGE_TOO_LONG_NOTICE);
  });

  it('shows another failure as the status text and retries at the cadence', async () => {
    let calls = 0;
    const { fetchSeries } = plannedFetcher(() => {
      calls += 1;
      return calls === 1 ? new Error('boom') : seriesAnswer();
    });
    mount(fetchSeries);
    await advance(0);
    expect(screen.getByTestId('telemetry-series-status').textContent).toContain('boom');
    await advance(60_000);
    expect(fetchSeries).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('telemetry-series-status').textContent).not.toContain('boom');
  });

  it('never requests a range over 400 days client-side', async () => {
    const { fetchSeries } = plannedFetcher(() => seriesAnswer());
    mount(fetchSeries, { initialRange: { type: 'relative', amount: 401, unit: 'day' } });
    await advance(0);
    expect(fetchSeries).not.toHaveBeenCalled();
    expect(screen.getByTestId('telemetry-series-notice').textContent).toContain(RANGE_TOO_LONG_NOTICE);
  });
});
