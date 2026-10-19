/**
 * Pure helpers behind the chart panels (workflow-telemetry-dashboards design
 * §8.1-§8.3; tasks.md 5.2): the resolution rule of `series.resolution_for`,
 * the fold of one series answer across the Dashboard's Devices by the
 * catalog's fold, the chart data of a template panel, and the "not reported
 * by this workflow" rule.
 *
 * Everything here is driven by the measure catalog and the template layout
 * served by `GET /telemetry/schemas`, so a new template is a JSON file plus a
 * test (design §8.2).
 */
import type {
  DashboardSeriesResponse,
  SeriesPoint,
  SeriesResolution,
  TelemetryCatalog,
  TelemetryMeasure,
  TelemetrySummary,
  TelemetryTemplatePanel,
} from '../../pages/telemetry/types';

export const MINUTE_MS = 60_000;
export const HOUR_MS = 3_600_000;
export const DAY_MS = 86_400_000;

/** The 400-day maximum of design §8.3. */
export const SERIES_MAX_RANGE_MS = 400 * DAY_MS;

/** The refresh cadence by resolution when `/telemetry/schemas` is not loaded yet (design §8.3). */
export const DEFAULT_SERIES_REFRESH_SECONDS: Record<SeriesResolution, number> = { minute: 60, hour: 300, day: 900 };

/** The default manual-refresh rate limit (design §8.3). */
export const DEFAULT_MANUAL_REFRESH_MIN_SECONDS = 10;

/**
 * `series.resolution_for(from_ms, to_ms)` of the API (design §8.3): ≤ 6 h →
 * `minute`, ≤ 60 days → `hour`, ≤ 400 days → `day`, else `null`
 * (`range_too_long`). Also `null` for an empty or inverted range.
 */
export function resolutionFor(fromMs: number, toMs: number): SeriesResolution | null {
  const span = toMs - fromMs;
  if (!Number.isFinite(span) || span <= 0) return null;
  if (span <= 6 * HOUR_MS) return 'minute';
  if (span <= 60 * DAY_MS) return 'hour';
  if (span <= SERIES_MAX_RANGE_MS) return 'day';
  return null;
}

export const NONE_STATE = 'NONE';

/** Worst of the states by the registry's order (`NONE < READY < … < UNAVAILABLE`); unknown strings count as `UNKNOWN`. */
export function worstState(states: readonly string[], order: readonly string[]): string | null {
  let worst: string | null = null;
  let worstRank = -1;
  for (const raw of states) {
    const state = order.includes(raw) ? raw : 'UNKNOWN';
    const rank = order.indexOf(state);
    if (rank > worstRank) {
      worstRank = rank;
      worst = state;
    }
  }
  return worst;
}

type Weighted = { value: number; weight: number };

/** Folds one measure's values of several Devices in one bucket by the catalog's fold. */
export function foldAcross(fold: TelemetryMeasure['fold'], values: Weighted[], order: readonly string[] = []): number | null {
  if (values.length === 0) return null;
  switch (fold) {
    case 'sum':
    case 'count':
      return values.reduce((acc, v) => acc + v.value, 0);
    case 'max':
    case 'p95max':
      return values.reduce((acc, v) => Math.max(acc, v.value), -Infinity);
    case 'mean':
    case 'p50mean': {
      const weight = values.reduce((acc, v) => acc + (v.weight > 0 ? v.weight : 1), 0);
      const total = values.reduce((acc, v) => acc + v.value * (v.weight > 0 ? v.weight : 1), 0);
      return weight > 0 ? total / weight : null;
    }
    case 'last':
      return values[values.length - 1].value;
    case 'worst':
      // Numeric `worst` folds do not exist in the catalog; the state fold is
      // handled by `worstState`. Keep the maximum as the conservative answer.
      void order;
      return values.reduce((acc, v) => Math.max(acc, v.value), -Infinity);
    default:
      return values[values.length - 1].value;
  }
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value);
  return null;
}

/** The event type an event-sourced measure counts (`events.clip_ready` → `clip_ready`). */
export function eventTypeOf(measure: TelemetryMeasure): string | null {
  if (measure.source !== 'event') return null;
  const parts = measure.path.split('.');
  return parts.length === 2 && parts[0] === 'events' ? parts[1] : null;
}

/**
 * The raw value of a measure in one point: the folded number, a `perKey`
 * map, a state string for `model_state_worst`, or `null`. Event measures
 * fall back to the point's `events` counts.
 */
export function pointValue(point: SeriesPoint, measure: TelemetryMeasure): unknown {
  const direct = point.measures?.[measure.id];
  if (direct !== undefined && direct !== null) return direct;
  const eventType = eventTypeOf(measure);
  if (eventType && point.events && point.events[eventType] !== undefined) return point.events[eventType];
  return null;
}

export interface ChartDatum {
  x: Date;
  y: number;
}

export interface ChartSeries {
  /** The legend title: the catalog label, a `perKey` key, or a model state. */
  title: string;
  /** The measure the series comes from (a state series names `model_state_worst`). */
  measureId: string;
  /** The `perKey` key or the state, when the series is one of several. */
  key?: string;
  data: ChartDatum[];
}

export interface PieSlice {
  title: string;
  value: number;
}

export interface PanelData {
  panel: TelemetryTemplatePanel;
  /** False when no Summary reported any of the panel's measures (design §8.2). */
  reported: boolean;
  /** The time series, one per measure, `perKey` key or state. */
  series: ChartSeries[];
  /** `mixed-line-bar`: which series are bars (the rest are lines). */
  barSeries: Set<string>;
  /** `pie-and-line`: the totals over the range per measure. */
  pie: PieSlice[];
  /** The units of the panel's measures (one axis title when they agree). */
  unit: string | null;
  /** Every bucket start of the folded devices, ascending. */
  buckets: number[];
}

export interface NewestSummary {
  producer: string;
  receivedMs: number | null;
  summary: TelemetrySummary | null;
}

const REPORTED_WINDOW_MS = DAY_MS;

/** Follows a catalog `path` (`detections.perClass.*.total`) through a Summary; `*` yields the map of keys. */
export function resolvePath(summary: TelemetrySummary | null | undefined, path: string): unknown {
  if (!summary || !path) return undefined;
  const parts = path.split('.');
  let node: unknown = summary;
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i];
    if (node === null || node === undefined || typeof node !== 'object') return undefined;
    if (part === '*') {
      const rest = parts.slice(i + 1).join('.');
      const map = node as Record<string, unknown>;
      const out: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(map)) {
        const value = rest ? resolvePath(child as TelemetrySummary, rest) : child;
        if (value !== undefined && value !== null) out[key] = value;
      }
      return Object.keys(out).length ? out : undefined;
    }
    node = (node as Record<string, unknown>)[part];
  }
  return node === null ? undefined : node;
}

function hasValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === 'object') return Object.keys(value as object).length > 0;
  return true;
}

/**
 * The "not reported by this workflow" rule (design §8.2): a panel is reported
 * when a Summary of the last 24 hours (the `LAST#<producer>` pointers of the
 * Now answer) carries a value at one of its measures' paths, when the
 * series answer holds a value for one of them, or, for an event-sourced
 * measure, when the `ppe-clipper` producer published within 24 hours.
 */
export function panelReported(
  panel: TelemetryTemplatePanel,
  catalog: TelemetryCatalog,
  series: DashboardSeriesResponse | null,
  newest: readonly NewestSummary[] = [],
  nowMs: number = Date.now()
): boolean {
  const recent = newest.filter((n) => n.receivedMs !== null && nowMs - n.receivedMs <= REPORTED_WINDOW_MS);
  const producers = new Set(recent.map((n) => n.producer));
  for (const id of panel.measures) {
    const measure = catalog.measures.find((m) => m.id === id);
    if (!measure) continue;
    if (measure.source === 'event') {
      if (producers.has('ppe-clipper')) return true;
    } else {
      for (const pointer of recent) {
        if (hasValue(resolvePath(pointer.summary, measure.path))) return true;
      }
    }
    if (series) {
      for (const device of Object.values(series.devices ?? {})) {
        for (const point of device.points ?? []) {
          if (hasValue(pointValue(point, measure))) return true;
        }
      }
    }
  }
  return false;
}

function pointsByBucket(series: DashboardSeriesResponse, deviceIds: readonly string[]): Map<number, SeriesPoint[]> {
  const byBucket = new Map<number, SeriesPoint[]>();
  for (const deviceId of deviceIds) {
    const points = series.devices?.[deviceId]?.points ?? [];
    for (const point of points) {
      if (typeof point.t !== 'number') continue;
      const list = byBucket.get(point.t);
      if (list) list.push(point);
      else byBucket.set(point.t, [point]);
    }
  }
  return byBucket;
}

/** The Devices of the answer the panel folds: the selection when given, else every Device. */
export function devicesToFold(series: DashboardSeriesResponse, selected?: readonly string[] | null): string[] {
  const all = Object.keys(series.devices ?? {});
  if (!selected || selected.length === 0) return all;
  return all.filter((id) => selected.includes(id));
}

/**
 * The chart data of one template panel from the one series answer (design
 * §8.3): per bucket, each measure folded across the selected Devices by the
 * catalog's fold (`perKey` measures per key; the model-state panel as one
 * series per state where each Device-bucket contributes 1 to the series of
 * its `model_state_worst` and `NONE` is not drawn).
 */
export function panelData(
  panel: TelemetryTemplatePanel,
  catalog: TelemetryCatalog,
  series: DashboardSeriesResponse | null,
  options: { deviceIds?: readonly string[] | null; modelStateOrder: readonly string[]; newest?: readonly NewestSummary[]; nowMs?: number }
): PanelData {
  const measures = panel.measures
    .map((id) => catalog.measures.find((m) => m.id === id))
    .filter((m): m is TelemetryMeasure => m !== undefined);
  const units = new Set(measures.map((m) => m.unit));
  const base: PanelData = {
    panel,
    reported: panelReported(panel, catalog, series, options.newest ?? [], options.nowMs ?? Date.now()),
    series: [],
    barSeries: new Set(panel.bars ?? []),
    pie: [],
    unit: units.size === 1 ? [...units][0] : null,
    buckets: [],
  };
  if (!series) return base;
  const deviceIds = devicesToFold(series, options.deviceIds);
  const byBucket = pointsByBucket(series, deviceIds);
  const buckets = [...byBucket.keys()].sort((a, b) => a - b);
  base.buckets = buckets;

  if (panel.chart === 'stacked-bar-states') {
    const stateMeasure = measures[0];
    const states = options.modelStateOrder.filter((s) => s !== NONE_STATE);
    const perState = new Map<string, ChartDatum[]>(states.map((s) => [s, []]));
    for (const t of buckets) {
      const counts = new Map<string, number>();
      for (const point of byBucket.get(t) ?? []) {
        const raw = stateMeasure ? pointValue(point, stateMeasure) : null;
        if (typeof raw !== 'string') continue;
        const state = worstState([raw], options.modelStateOrder);
        if (!state || state === NONE_STATE) continue;
        counts.set(state, (counts.get(state) ?? 0) + 1);
      }
      for (const state of states) perState.get(state)!.push({ x: new Date(t), y: counts.get(state) ?? 0 });
    }
    base.series = states.map((state) => ({
      title: state,
      measureId: stateMeasure?.id ?? 'model_state_worst',
      key: state,
      data: perState.get(state) ?? [],
    }));
    return base;
  }

  for (const measure of measures) {
    if (measure.perKey) {
      const keys = new Set<string>();
      for (const points of byBucket.values()) {
        for (const point of points) {
          const value = pointValue(point, measure);
          if (value && typeof value === 'object') for (const key of Object.keys(value)) keys.add(key);
        }
      }
      for (const key of [...keys].sort()) {
        const data: ChartDatum[] = [];
        for (const t of buckets) {
          const values: Weighted[] = [];
          for (const point of byBucket.get(t) ?? []) {
            const map = pointValue(point, measure);
            const number = map && typeof map === 'object' ? toNumber((map as Record<string, unknown>)[key]) : null;
            if (number !== null) values.push({ value: number, weight: point.summaries ?? 1 });
          }
          const folded = foldAcross(measure.fold, values);
          data.push({ x: new Date(t), y: folded ?? 0 });
        }
        base.series.push({ title: key, measureId: measure.id, key, data });
      }
      continue;
    }
    const data: ChartDatum[] = [];
    let total = 0;
    for (const t of buckets) {
      const values: Weighted[] = [];
      for (const point of byBucket.get(t) ?? []) {
        const number = toNumber(pointValue(point, measure));
        if (number !== null) values.push({ value: number, weight: point.summaries ?? 1 });
      }
      const folded = foldAcross(measure.fold, values);
      if (folded !== null) total += folded;
      data.push({ x: new Date(t), y: folded ?? 0 });
    }
    base.series.push({ title: measure.label, measureId: measure.id, data });
    if (panel.chart === 'pie-and-line') base.pie.push({ title: measure.label, value: total });
  }
  return base;
}
