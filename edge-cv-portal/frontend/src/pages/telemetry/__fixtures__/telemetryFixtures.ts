/**
 * Shared fixtures of the telemetry dashboards tests
 * (workflow-telemetry-dashboards tasks.md 5.2-5.5).
 *
 * `schemas.json` is the `GET /telemetry/schemas` answer built from the
 * committed backend contracts (`backend/functions/telemetry/registry.json`,
 * `measure_catalog.json`, `templates/*.json`); `schemasFixture.test.ts` pins
 * the two copies to each other so a template or catalog change on one side
 * fails a test on the other.
 */
import schemasJson from './schemas.json';
import type {
  Dashboard,
  DashboardNowResponse,
  DashboardSeriesResponse,
  SeriesPoint,
  SeriesResolution,
  TelemetryNowDevice,
  TelemetryPointer,
  TelemetrySchemasResponse,
  TelemetrySummary,
  TelemetryTemplate,
} from '../types';

export const SCHEMAS = schemasJson as unknown as TelemetrySchemasResponse;

export const TEMPLATE_IDS = ['generic-detector', 'anomaly', 'ppe-compliance'] as const;

export function template(id: (typeof TEMPLATE_IDS)[number]): TelemetryTemplate {
  const found = SCHEMAS.templates[id];
  if (!found) throw new Error(`fixture template missing: ${id}`);
  return found;
}

/** A fixed instant (2026-10-18T10:00:00Z) every fixture clock starts from. */
export const NOW = Date.parse('2026-10-18T10:00:00.000Z');

export const MINUTE = 60_000;

export function dashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    dashboard_id: 'dash-1',
    usecase_id: 'uc-1',
    workflow_id: '0f1e2d3c-4b5a-4968-8776-65544332211a',
    device_ids: ['thing-a', 'thing-b'],
    template_id: 'generic-detector',
    template_version: 1,
    name: 'Line 1',
    createdAt: NOW - 3_600_000,
    updatedAt: NOW - 3_600_000,
    actor: 'operator@example.com',
    ...overrides,
  };
}

/** A full Summary as the engine publishes it (design §4.1). */
export function summary(overrides: Partial<TelemetrySummary> = {}): TelemetrySummary {
  return {
    runs: { completed: 58, failed: 1, dropped: 2 },
    latency: { p50Ms: 120, p95Ms: 310, maxMs: 400 },
    detections: { total: 140, runsWithDetections: 50, truncated: false, perClass: { helmet: { total: 90, max: 3 }, human: { total: 50, max: 2 } } },
    people: { seen: 2 },
    model: { state: 'READY', waits: 0, waitedMs: 0 },
    continuous: { skippedNoNewFrame: 3, paused: false, effectiveFps: 5.8 },
    telemetry: { queueDrops: 0, publishFailures: 0, eventsDropped: 0 },
    ...overrides,
  };
}

/** A minute point whose measures carry the folded values of one Summary. */
export function point(t: number, overrides: Partial<SeriesPoint['measures']> = {}, extra: Partial<SeriesPoint> = {}): SeriesPoint {
  return {
    t,
    measures: {
      runs_completed: 58,
      runs_failed: 1,
      runs_dropped: 2,
      latency_p50_ms: 120,
      latency_p95_ms: 310,
      detections_class_total: { helmet: 90, human: 50 },
      people_seen_max: 2,
      model_state_worst: 'READY',
      model_waits: 0,
      model_wait_ms: 0,
      summaries: 6,
      gaps: 0,
      ...overrides,
    },
    events: {},
    gaps: 0,
    summaries: 6,
    ...extra,
  };
}

/** `n` consecutive buckets ending at `NOW`, per device. */
export function seriesAnswer(
  options: {
    resolution?: SeriesResolution;
    devices?: string[];
    buckets?: number;
    pointFor?: (deviceId: string, index: number, t: number) => SeriesPoint;
    updatedAt?: number;
  } = {}
): DashboardSeriesResponse {
  const resolution = options.resolution ?? 'minute';
  const step = resolution === 'minute' ? MINUTE : resolution === 'hour' ? 60 * MINUTE : 1_440 * MINUTE;
  const buckets = options.buckets ?? 3;
  const devices = options.devices ?? ['thing-a', 'thing-b'];
  const start = Math.floor(NOW / step) * step - (buckets - 1) * step;
  const answer: DashboardSeriesResponse = {
    resolution,
    devices: {},
    from: start,
    to: NOW,
    updatedAt: options.updatedAt ?? NOW,
    refreshSeconds: resolution === 'minute' ? 60 : resolution === 'hour' ? 300 : 900,
  };
  for (const deviceId of devices) {
    const points: SeriesPoint[] = [];
    for (let i = 0; i < buckets; i += 1) {
      const t = start + i * step;
      points.push(options.pointFor ? options.pointFor(deviceId, i, t) : point(t));
    }
    answer.devices[deviceId] = { points };
  }
  return answer;
}

export function pointer(overrides: Partial<TelemetryPointer> = {}): TelemetryPointer {
  return {
    receivedMs: NOW - 8_000,
    eventMs: NOW - 9_000,
    seq: 41,
    instance: '0f1e2d3c-4b5a-4968-8776-65544332211a',
    workflowVersion: '3',
    lastRunMs: NOW - 12_000,
    summary: summary(),
    ...overrides,
  };
}

export function nowDevice(overrides: Partial<TelemetryNowDevice> = {}): TelemetryNowDevice {
  return {
    flow: 'flowing',
    ages: { lastSummaryAgeS: 8, lastRunAgeS: 12, heartbeatAgeS: null, noRunIn48h: false },
    producers: ['engine'],
    last: { 'LAST#engine': pointer() },
    events: [],
    live: null,
    lastHour: {
      measures: { runs_failed: 3, runs_dropped: 4, telemetry_queue_drops: 0, telemetry_publish_failures: 0, telemetry_events_dropped: 0, model_waits: 1, model_wait_ms: 1500 },
      events: {},
      gaps: 0,
      minutes: 60,
    },
    coreDevice: { status: 'HEALTHY', lastStatusUpdate: '', coreVersion: '2.12.0', platform: null },
    targetArchitecture: 'arm64_jp5',
    variantHint: null,
    ...overrides,
  };
}

export function nowAnswer(overrides: Partial<DashboardNowResponse> = {}): DashboardNowResponse {
  return {
    dashboard_id: 'dash-1',
    usecase_id: 'uc-1',
    workflow_id: '0f1e2d3c-4b5a-4968-8776-65544332211a',
    enabled: true,
    devices: { 'thing-a': nowDevice(), 'thing-b': nowDevice({ flow: 'idle', ages: { lastSummaryAgeS: 400, lastRunAgeS: 420, heartbeatAgeS: 20, noRunIn48h: false }, last: { 'LAST#engine': pointer({ receivedMs: NOW - 400_000 }), 'LASTHB#engine': pointer({ receivedMs: NOW - 20_000, summary: null, lastRunMs: null }) } }) },
    updatedAt: NOW,
    pollSeconds: 5,
    ...overrides,
  };
}

/** The ResizeObserver the Cloudscape charts expect, absent from jsdom. */
export function installResizeObserver(): void {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
}
