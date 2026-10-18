/**
 * Unit tests for the template renderer (workflow-telemetry-dashboards task
 * 5.2, Requirements 1.1, 1.4, 9.3, 9.4).
 *
 * - the three templates render every panel of their layout from one fixture
 *   series answer, each header carrying the resolution and "updated <age>";
 * - a panel whose measures no Summary reported renders "not reported by this
 *   workflow";
 * - the model-state panel is one series per state (NONE not drawn) where each
 *   Device-bucket contributes 1 to the series of its `model_state_worst`;
 * - the pure helpers: the resolution rule at its boundaries, the fold across
 *   devices, the `perKey` series, the "not reported" rule from the Now
 *   pointers.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import TemplateRenderer, { NOT_REPORTED } from './TemplateRenderer';
import {
  DAY_MS,
  HOUR_MS,
  MINUTE_MS,
  panelData,
  panelReported,
  resolutionFor,
  resolvePath,
  worstState,
} from './seriesData';
import {
  NOW,
  SCHEMAS,
  TEMPLATE_IDS,
  installResizeObserver,
  point,
  seriesAnswer,
  summary,
  template,
} from '../../pages/telemetry/__fixtures__/telemetryFixtures';

const ORDER = SCHEMAS.registry.modelStateOrder;
const CATALOG = SCHEMAS.catalog;

beforeAll(() => {
  installResizeObserver();
});

function renderTemplate(id: (typeof TEMPLATE_IDS)[number], series = seriesAnswer(), extra: Partial<Parameters<typeof TemplateRenderer>[0]> = {}) {
  return render(
    <TemplateRenderer
      template={template(id)}
      catalog={CATALOG}
      modelStateOrder={ORDER}
      series={series}
      now={NOW + 12_000}
      {...extra}
    />
  );
}

describe('resolutionFor (design §8.3 boundaries)', () => {
  it.each([
    [6 * HOUR_MS, 'minute'],
    [6 * HOUR_MS + MINUTE_MS, 'hour'],
    [60 * DAY_MS, 'hour'],
    [60 * DAY_MS + HOUR_MS, 'day'],
    [400 * DAY_MS, 'day'],
    [400 * DAY_MS + DAY_MS, null],
  ] as const)('a span of %d ms resolves to %s', (span, expected) => {
    expect(resolutionFor(NOW - span, NOW)).toBe(expected);
  });

  it('refuses an empty or inverted range', () => {
    expect(resolutionFor(NOW, NOW)).toBeNull();
    expect(resolutionFor(NOW, NOW - 1)).toBeNull();
  });
});

describe('worstState and resolvePath', () => {
  it('orders by the registry order and maps unknown strings to UNKNOWN', () => {
    expect(worstState(['READY', 'LOADING'], ORDER)).toBe('LOADING');
    expect(worstState(['READY', 'weird'], ORDER)).toBe('UNKNOWN');
    expect(worstState(['UNAVAILABLE', 'UNKNOWN'], ORDER)).toBe('UNAVAILABLE');
    expect(worstState([], ORDER)).toBeNull();
  });

  it('follows catalog paths including the perKey wildcard', () => {
    const s = summary();
    expect(resolvePath(s, 'runs.completed')).toBe(58);
    expect(resolvePath(s, 'detections.perClass.*.total')).toEqual({ helmet: 90, human: 50 });
    expect(resolvePath(s, 'ext.ppe.people.violating')).toBeUndefined();
    expect(resolvePath(null, 'runs.completed')).toBeUndefined();
  });
});

describe('panelData folds the one answer across devices (design §8.3)', () => {
  const runs = template('generic-detector').panels.find((p) => p.id === 'runs')!;
  const latency = template('generic-detector').panels.find((p) => p.id === 'latency')!;
  const detections = template('generic-detector').panels.find((p) => p.id === 'detections')!;
  const people = template('generic-detector').panels.find((p) => p.id === 'people')!;

  it('sums a sum-fold measure over the devices per bucket', () => {
    const data = panelData(runs, CATALOG, seriesAnswer({ buckets: 2 }), { modelStateOrder: ORDER });
    expect(data.series.map((s) => s.title)).toEqual(['Runs completed', 'Runs failed', 'Runs dropped']);
    expect(data.series[0].data.map((d) => d.y)).toEqual([116, 116]);
    expect(data.buckets).toHaveLength(2);
    expect(data.unit).toBe('count');
  });

  it('takes the maximum for max folds and the weighted mean for p50mean', () => {
    const answer = seriesAnswer({
      buckets: 1,
      pointFor: (device, _i, t) =>
        device === 'thing-a'
          ? point(t, { latency_p50_ms: 100, latency_p95_ms: 300, people_seen_max: 1 }, { summaries: 6 })
          : point(t, { latency_p50_ms: 200, latency_p95_ms: 500, people_seen_max: 4 }, { summaries: 2 }),
    });
    const lat = panelData(latency, CATALOG, answer, { modelStateOrder: ORDER });
    expect(lat.series.find((s) => s.measureId === 'latency_p50_ms')!.data[0].y).toBe(125);
    expect(lat.series.find((s) => s.measureId === 'latency_p95_ms')!.data[0].y).toBe(500);
    const ppl = panelData(people, CATALOG, answer, { modelStateOrder: ORDER });
    expect(ppl.series[0].data[0].y).toBe(4);
  });

  it('folds a single selected device only', () => {
    const data = panelData(runs, CATALOG, seriesAnswer({ buckets: 1 }), { modelStateOrder: ORDER, deviceIds: ['thing-b'] });
    expect(data.series[0].data[0].y).toBe(58);
  });

  it('makes one series per perKey key, summed across devices', () => {
    const data = panelData(detections, CATALOG, seriesAnswer({ buckets: 1 }), { modelStateOrder: ORDER });
    expect(data.series.map((s) => s.title)).toEqual(['helmet', 'human']);
    expect(data.series[0].data[0].y).toBe(180);
    expect(data.series[1].data[0].y).toBe(100);
  });

  it('is empty but typed before the first answer', () => {
    const data = panelData(runs, CATALOG, null, { modelStateOrder: ORDER });
    expect(data.series).toEqual([]);
    expect(data.buckets).toEqual([]);
  });
});

describe('the model-state panel (design §8.2)', () => {
  const panel = template('generic-detector').panels.find((p) => p.id === 'model-state')!;

  it('has one series per state except NONE, in the registry order', () => {
    const data = panelData(panel, CATALOG, seriesAnswer({ buckets: 1 }), { modelStateOrder: ORDER });
    expect(data.series.map((s) => s.title)).toEqual(ORDER.filter((s) => s !== 'NONE'));
    expect(data.series.map((s) => s.title)).not.toContain('NONE');
  });

  it('each device-bucket contributes 1 to the series of its model_state_worst', () => {
    const answer = seriesAnswer({
      buckets: 2,
      pointFor: (device, index, t) =>
        point(t, { model_state_worst: device === 'thing-a' ? 'READY' : index === 0 ? 'LOADING' : 'NONE' }),
    });
    const data = panelData(panel, CATALOG, answer, { modelStateOrder: ORDER });
    const ready = data.series.find((s) => s.title === 'READY')!;
    const loading = data.series.find((s) => s.title === 'LOADING')!;
    expect(ready.data.map((d) => d.y)).toEqual([1, 1]);
    expect(loading.data.map((d) => d.y)).toEqual([1, 0]);
    // The NONE bucket of thing-b in the second bucket is drawn nowhere.
    const total = data.series.reduce((acc, s) => acc + s.data[1].y, 0);
    expect(total).toBe(1);
  });

  it('coerces an out-of-set state to UNKNOWN', () => {
    const answer = seriesAnswer({ buckets: 1, devices: ['thing-a'], pointFor: (_d, _i, t) => point(t, { model_state_worst: 'odd' }) });
    const data = panelData(panel, CATALOG, answer, { modelStateOrder: ORDER });
    expect(data.series.find((s) => s.title === 'UNKNOWN')!.data[0].y).toBe(1);
  });
});

describe('the "not reported by this workflow" rule (design §8.2)', () => {
  const peoplePanel = template('generic-detector').panels.find((p) => p.id === 'people')!;
  const violations = template('ppe-compliance').panels.find((p) => p.id === 'violations-by-reason')!;
  const clips = template('ppe-compliance').panels.find((p) => p.id === 'clips')!;

  it('is reported when the series answer holds a value for a measure', () => {
    expect(panelReported(peoplePanel, CATALOG, seriesAnswer({ buckets: 1 }), [], NOW)).toBe(true);
  });

  it('is not reported when neither the series nor a recent Summary carries the measures', () => {
    const answer = seriesAnswer({ buckets: 1, pointFor: (_d, _i, t) => point(t, { people_seen_max: undefined }) });
    expect(panelReported(peoplePanel, CATALOG, answer, [], NOW)).toBe(false);
    expect(panelReported(violations, CATALOG, answer, [], NOW)).toBe(false);
  });

  it('is reported from a Summary of the last 24 h at the measure path, and not from an older one', () => {
    const answer = seriesAnswer({ buckets: 1, pointFor: (_d, _i, t) => point(t, { people_seen_max: undefined }) });
    const recent = [{ producer: 'engine', receivedMs: NOW - 2 * HOUR_MS, summary: summary() }];
    const old = [{ producer: 'engine', receivedMs: NOW - 25 * HOUR_MS, summary: summary() }];
    expect(panelReported(peoplePanel, CATALOG, answer, recent, NOW)).toBe(true);
    expect(panelReported(peoplePanel, CATALOG, answer, old, NOW)).toBe(false);
  });

  it('an event-sourced panel is reported when the ppe-clipper published within 24 h', () => {
    const answer = seriesAnswer({ buckets: 1, pointFor: (_d, _i, t) => point(t, { people_seen_max: undefined }) });
    const clipper = [{ producer: 'ppe-clipper', receivedMs: NOW - 60_000, summary: { ext: { ppe: { frames: 10 } } } }];
    expect(panelReported(clips, CATALOG, answer, clipper, NOW)).toBe(true);
    expect(panelReported(clips, CATALOG, answer, [], NOW)).toBe(false);
  });
});

describe('TemplateRenderer renders every panel of each template', () => {
  it.each(TEMPLATE_IDS)('%s', (id) => {
    const tpl = template(id);
    const ppeAnswer = seriesAnswer({
      buckets: 2,
      pointFor: (_d, _i, t) =>
        point(
          t,
          {
            anomalous: 3,
            normal: 7,
            ppe_reason_frames: { 'no-helmet': 4, 'no-vest': 1 },
            ppe_people_compliant_max: 2,
            ppe_people_violating_max: 1,
            events_clip_ready: 1,
            events_violation_confirmed: 2,
          },
          { events: { clip_ready: 1, violation_confirmed: 2 } }
        ),
    });
    renderTemplate(id, ppeAnswer);
    for (const panel of tpl.panels) {
      const node = screen.getByTestId(`telemetry-panel-${panel.id}`);
      // The title may also be a legend label (a measure named like its panel).
      expect(within(node).getAllByText(panel.title).length).toBeGreaterThan(0);
      expect(node.textContent).toContain('minute resolution');
      expect(node.textContent).toContain('updated 12 s ago');
      expect(within(node).queryByText(NOT_REPORTED)).toBeNull();
      // The panel's chart kind produced the matching Cloudscape chart.
      const wrapper = createWrapper(node);
      switch (panel.chart) {
        case 'area':
          expect(wrapper.findAreaChart()).not.toBeNull();
          break;
        case 'line':
          expect(wrapper.findLineChart()).not.toBeNull();
          break;
        case 'bar':
        case 'stacked-bar':
        case 'stacked-bar-states':
          expect(wrapper.findBarChart()).not.toBeNull();
          break;
        case 'mixed-line-bar':
          expect(wrapper.findMixedLineBarChart()).not.toBeNull();
          break;
        case 'pie-and-line':
          expect(wrapper.findPieChart()).not.toBeNull();
          expect(wrapper.findLineChart()).not.toBeNull();
          break;
        default:
          throw new Error(`unexpected chart kind ${String(panel.chart)}`);
      }
      // Each measure of the panel is a legend entry (perKey keys and states for the keyed charts).
      for (const measureId of panel.measures) {
        const measure = CATALOG.measures.find((m) => m.id === measureId)!;
        if (!measure.perKey && panel.chart !== 'stacked-bar-states') {
          expect(node.textContent).toContain(measure.label);
        }
      }
    }
  });

  it('the generic template shows the legend titles from the catalog and the perKey keys', () => {
    const { container } = renderTemplate('generic-detector');
    const runs = screen.getByTestId('telemetry-panel-runs');
    expect(runs.textContent).toContain('Runs completed');
    expect(runs.textContent).toContain('Runs failed');
    expect(runs.textContent).toContain('Runs dropped');
    const detections = screen.getByTestId('telemetry-panel-detections');
    expect(detections.textContent).toContain('helmet');
    expect(detections.textContent).toContain('human');
    const states = createWrapper(screen.getByTestId('telemetry-panel-model-state')).findBarChart()!;
    expect(states.findSeries()).toHaveLength(ORDER.length - 1);
    expect(screen.getByTestId('telemetry-panel-model-state').textContent).not.toMatch(/\bNONE\b/);
    expect(container.querySelectorAll('[data-testid^="telemetry-panel-"]').length).toBeGreaterThan(0);
  });

  it('renders "not reported by this workflow" for a panel no Summary reported', () => {
    const answer = seriesAnswer({ buckets: 2, pointFor: (_d, _i, t) => point(t, { people_seen_max: undefined }) });
    renderTemplate('ppe-compliance', answer);
    expect(screen.getByTestId('telemetry-panel-people-not-reported').textContent).toBe(NOT_REPORTED);
    expect(screen.getByTestId('telemetry-panel-violations-by-reason-not-reported').textContent).toBe(NOT_REPORTED);
    expect(screen.getByTestId('telemetry-panel-clips-not-reported').textContent).toBe(NOT_REPORTED);
    // The runs panel is reported and draws its chart.
    expect(screen.queryByTestId('telemetry-panel-runs-not-reported')).toBeNull();
    expect(createWrapper(screen.getByTestId('telemetry-panel-runs')).findAreaChart()).not.toBeNull();
  });

  it('a recent Summary makes a panel reported even when the range holds no value', () => {
    const answer = seriesAnswer({ buckets: 2, pointFor: (_d, _i, t) => point(t, { people_seen_max: undefined }) });
    renderTemplate('generic-detector', answer, {
      newestSummaries: [{ producer: 'engine', receivedMs: NOW - 5_000, summary: summary() }],
    });
    expect(screen.queryByTestId('telemetry-panel-people-not-reported')).toBeNull();
  });

  it('shows "loading…" in the headers and no "not reported" before the first answer', () => {
    renderTemplate('anomaly', null as unknown as ReturnType<typeof seriesAnswer>, { loading: true });
    expect(screen.getByTestId('telemetry-panel-runs').textContent).toContain('loading…');
    expect(screen.queryByText(NOT_REPORTED)).toBeNull();
  });
});
