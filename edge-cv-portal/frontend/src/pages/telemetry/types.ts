/**
 * Types of the workflow telemetry dashboards API
 * (workflow-telemetry-dashboards design §4, §8, §9, §10.1, §11.2;
 * tasks.md 5.1), one per route answer of `telemetry_api.py`.
 *
 * The answers are typed as the handlers build them: epoch milliseconds for
 * every `*Ms` field, seconds for every `*S` / `*Seconds` field, lowercase
 * `error_code` in every refusal except the Portal-wide 403
 * `INSUFFICIENT_PERMISSIONS`.
 */

// ----------------------------------------------------------------- envelope

/**
 * The simple refusal envelope `{error, error_code, remedy?, field?, ...}`
 * of every telemetry route (design §11.2, §17). Extra keys (`reason`, `max`,
 * `max_days`, `bucket`, `name`) ride along per code.
 */
export interface TelemetryErrorBody {
  error: string;
  error_code: string;
  remedy?: string;
  field?: string;
  reason?: string;
  max?: number;
  max_days?: number;
  max_minutes?: number;
  bucket?: string;
  name?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------- schemas answer

export type TelemetryFold = 'sum' | 'max' | 'mean' | 'p50mean' | 'p95max' | 'worst' | 'last' | 'count';

/** One entry of `measure_catalog.json` (design §8.1). */
export interface TelemetryMeasure {
  id: string;
  source: 'summary' | 'event';
  path: string;
  fold: TelemetryFold;
  unit: string;
  label: string;
  perKey: boolean;
}

export interface TelemetryCatalog {
  version: number;
  measures: TelemetryMeasure[];
}

/** The chart kinds the committed templates use (design §8.2). */
export type TelemetryChartKind =
  | 'area'
  | 'line'
  | 'bar'
  | 'stacked-bar'
  | 'stacked-bar-states'
  | 'pie-and-line'
  | 'mixed-line-bar';

export interface TelemetryTemplatePanel {
  id: string;
  title: string;
  chart: TelemetryChartKind;
  measures: string[];
  /** `mixed-line-bar` only: the measures drawn as bars and as lines. */
  bars?: string[];
  lines?: string[];
  note?: string;
}

export type TelemetryTile = 'now' | 'health' | 'clips' | 'live';

/** One of `templates/*.json` (design §8.2). */
export interface TelemetryTemplate {
  id: string;
  version: number;
  label: string;
  description?: string;
  requires: { blocks: string[]; extensions: string[] };
  panels: TelemetryTemplatePanel[];
  tiles: TelemetryTile[];
}

/** The refresh cadences in seconds (design §8.3, §9, §15.2). */
export interface TelemetryCadence {
  now: number;
  series: { minute: number; hour: number; day: number };
  events: number;
  clips: number;
  liveFrame: number;
  liveLease: number;
  manualRefreshMin: number;
}

export interface TelemetryCaps {
  devicesPerDashboard: number;
  dashboardsPerUsecase: number;
  liveMaxLeases: number;
  liveMaxViewMinutes: number;
  clipRetentionDays: number;
  deviceLiveLimit: number;
  summaryIntervalRange: [number, number];
  idleHeartbeatRange: [number, number];
}

/** `GET /telemetry/schemas` (any authenticated user). */
export interface TelemetrySchemasResponse {
  registry: {
    version: number;
    kinds: Record<string, string>;
    modelStates: string[];
    /** Best to worst (design §4.1): `NONE < READY < UNLOADING < …`. */
    modelStateOrder: string[];
  };
  catalog: TelemetryCatalog;
  templates: Record<string, TelemetryTemplate>;
  telemetryNowPollSeconds: number;
  cadence: TelemetryCadence;
  caps: TelemetryCaps;
  personClasses: string[];
}

// --------------------------------------------------------------- settings

export type TelemetryTopology = 'portal' | 'cross';

/** The read-only media location of design §12.1 with the probed region (§9.3). */
export interface TelemetryMediaLocation {
  bucket: string;
  prefix: string;
  source: string;
  region?: string | null;
  region_source?: 'header' | 'usecase' | null;
}

export interface TelemetrySettings {
  usecase_id: string;
  enabled: boolean;
  summaryIntervalS: number;
  idleHeartbeatS: number;
  personClasses: string[];
  media_location: TelemetryMediaLocation;
  topology: TelemetryTopology;
  updatedAt: number | null;
  updatedBy: string | null;
  shadowSyncPending: boolean;
}

export type TelemetryChecklistItemId =
  | 'ingestion_resources'
  | 'region'
  | 'role_form'
  | 'device_records'
  | 'telemetry_source'
  | 'media_location'
  | 'media_retention'
  | 'registry_enforcement'
  | 'device_shadows';

/**
 * One readiness item (design §10.1): `ready`, the `remedy` when it is not,
 * a `warning` when it is ready with a caveat (the cross-region text, the
 * enforcement text, the clip-days mismatch) and per-item details.
 */
export interface TelemetryChecklistItem {
  id: TelemetryChecklistItemId | string;
  ready: boolean;
  remedy?: string;
  warning?: string | null;
  error?: string | null;
  reason?: string | null;
  /** `device_records`: the managed core devices without a Device_Record. */
  unrecorded?: string[];
  /** `device_records`: devices whose record names another use case. */
  other_usecase?: Record<string, string>;
  recorded?: string[];
  managed?: string[];
  /** `device_shadows`: devices without an accepted shadow for the current settings. */
  pending?: string[];
  failed?: Record<string, string>;
  devices?: number;
  /** `media_location`: the probed location. */
  media_location?: TelemetryMediaLocation;
  usecase_region?: string;
  portal_region?: string;
  enforced?: boolean;
  lastSeenMs?: number | null;
  versioning?: string;
  clipRuleDays?: number | null;
  topology?: TelemetryTopology;
  [key: string]: unknown;
}

export interface TelemetrySettingsResponse {
  settings: TelemetrySettings;
  checklist: TelemetryChecklistItem[];
}

export interface TelemetrySettingsUpdate {
  enabled: boolean;
  summaryIntervalS?: number;
  idleHeartbeatS?: number;
}

export interface DeviceShadowResult {
  device_id: string;
  shadow: 'ok' | 'failed' | string;
  error?: string;
}

/** `PUT …/settings`: 200, or 202 with `pending_devices` above 40 devices (design §10.2). */
export interface TelemetrySettingsUpdateResponse extends TelemetrySettingsResponse {
  devices: DeviceShadowResult[];
  unrecorded_devices: string[];
  pending_devices?: number;
  message?: string;
  /** Set by the client from the HTTP status (202 when the shadows are pending). */
  status?: number;
}

// -------------------------------------------------------------- templates

export type TemplateAvailability = 'available' | 'partial' | 'unavailable';

export interface TemplateAvailabilityEntry {
  id: string;
  version: number;
  availability: TemplateAvailability;
  reported_panels: string[];
  unreported_panels: string[];
  producers: string[];
}

export interface TelemetryTemplatesResponse {
  usecase_id: string;
  templates: TemplateAvailabilityEntry[];
}

// ------------------------------------------------------------- dashboards

export interface Dashboard {
  dashboard_id: string;
  usecase_id: string;
  workflow_id: string;
  device_ids: string[];
  template_id: string;
  template_version: number;
  name: string;
  createdAt: number;
  updatedAt: number;
  actor: string;
}

export interface ListDashboardsResponse {
  usecase_id: string;
  dashboards: Dashboard[];
}

export interface CreateDashboardBody {
  usecase_id: string;
  name: string;
  workflow_id: string;
  device_ids: string[];
  template_id: string;
}

export interface PatchDashboardBody {
  name?: string;
  device_ids?: string[];
}

/** `GET /telemetry/dashboards/{id}` (200): the record in the same wrapper as the write answers. */
export interface GetDashboardResponse {
  dashboard: Dashboard;
}

/** `POST /telemetry/dashboards` (201) and `PATCH …/{id}` (200). */
export interface DashboardWriteResponse {
  dashboard: Dashboard;
  devices: DeviceShadowResult[];
}

export interface DeleteDashboardResponse {
  dashboard_id: string;
  deleted: boolean;
}

// ------------------------------------------------------------------- now

export type TelemetryFlow = 'flowing' | 'idle' | 'silent';

/** The newest `dda.summary.v1` as the pointer stores it (design §4.1). */
export interface TelemetrySummary {
  runs?: { completed?: number; failed?: number; dropped?: number; [key: string]: unknown };
  latency?: { p50Ms?: number; p95Ms?: number; maxMs?: number; [key: string]: unknown };
  detections?: {
    total?: number;
    runsWithDetections?: number;
    truncated?: boolean;
    perClass?: Record<string, { total?: number; max?: number }>;
    [key: string]: unknown;
  };
  outcomes?: { anomalous?: number; normal?: number; [key: string]: unknown };
  people?: { seen?: number; [key: string]: unknown };
  model?: { state?: string; waits?: number; waitedMs?: number; models?: Record<string, unknown>; [key: string]: unknown };
  continuous?: { skippedNoNewFrame?: number; paused?: boolean; effectiveFps?: number; [key: string]: unknown };
  telemetry?: { queueDrops?: number; publishFailures?: number; eventsDropped?: number; [key: string]: unknown };
  ext?: { ppe?: Record<string, unknown>; [key: string]: unknown };
  [key: string]: unknown;
}

/** A `LAST#<producer>` or `LASTHB#<producer>` pointer (design §7.1). */
export interface TelemetryPointer {
  receivedMs: number | null;
  eventMs: number | null;
  seq: number | null;
  instance: string | null;
  workflowVersion: string | number | null;
  lastRunMs: number | null;
  summary: TelemetrySummary | null;
}

export interface TelemetryAges {
  lastSummaryAgeS: number | null;
  lastRunAgeS: number | null;
  heartbeatAgeS: number | null;
  noRunIn48h: boolean;
}

export type TelemetryEventType = 'violation_confirmed' | 'clip_ready' | 'run_error';

export interface TelemetryNowEvent {
  eventId: string;
  type: TelemetryEventType | string;
  eventMs: number;
  receivedMs: number;
  severity: string | null;
  data: Record<string, unknown> | null;
}

export interface TelemetryLastHour {
  measures: Record<string, unknown>;
  events: Record<string, number>;
  gaps: number;
  minutes: number;
}

export interface CoreDeviceStatus {
  status: string | null;
  lastStatusUpdate?: string;
  coreVersion?: string | null;
  platform?: Record<string, unknown> | null;
  error?: string;
}

export interface TelemetryNowDevice {
  flow: TelemetryFlow;
  ages: TelemetryAges;
  producers: string[];
  /** Keyed by the pointer's sort key, `LAST#engine`, `LASTHB#engine`, `LAST#ppe-clipper`, … */
  last: Record<string, TelemetryPointer>;
  events: TelemetryNowEvent[];
  live: { ageS: number; frameToken: string | null } | null;
  lastHour: TelemetryLastHour;
  coreDevice: CoreDeviceStatus | null;
  targetArchitecture: string | null;
  variantHint: string | null;
}

export interface TelemetryRejections {
  counts: Record<string, number>;
  minutes: Array<{ minute: number | null; counts: Record<string, number> }>;
  samples: Array<{ reason: string | null; receivedMs: number | null; sample: unknown }>;
  note: string;
  usecase_id?: string;
  account_id?: string;
  scope?: 'usecase' | 'account';
}

/** `GET …/{dashboard_id}/now` (design §9.1). */
export interface DashboardNowResponse {
  dashboard_id: string;
  usecase_id: string;
  workflow_id: string;
  enabled: boolean;
  devices: Record<string, TelemetryNowDevice>;
  updatedAt: number;
  pollSeconds: number;
  /** Present for `telemetry:manage` holders only. */
  rejections?: TelemetryRejections;
}

// ---------------------------------------------------------------- series

export type SeriesResolution = 'minute' | 'hour' | 'day';

export interface SeriesPoint {
  /** The bucket's start in epoch milliseconds (UTC day starts for `day`). */
  t: number;
  /** Measure id to value; `perKey` measures hold a map; `model_state_worst` a state string. */
  measures: Record<string, number | string | Record<string, number> | null | undefined>;
  events: Record<string, number>;
  gaps: number;
  summaries: number;
}

export interface SeriesQuery {
  from: number;
  to: number;
  device_id?: string;
  measures?: string[];
}

/** `GET …/{dashboard_id}/series` (design §8.3). */
export interface DashboardSeriesResponse {
  resolution: SeriesResolution;
  devices: Record<string, { points: SeriesPoint[] }>;
  from: number;
  to: number;
  updatedAt: number;
  refreshSeconds: number;
}

// ---------------------------------------------------------------- events

export interface EventsQuery {
  from?: number;
  to?: number;
  type?: TelemetryEventType | string;
  device_id?: string;
  cursor?: string;
}

export interface TelemetryEvent {
  eventId: string;
  type: TelemetryEventType | string;
  ts: string | null;
  eventMs: number;
  receivedMs: number;
  tsSource: string | null;
  deviceId: string;
  workflowVersion: string | number | null;
  severity: string | null;
  executionId?: string | null;
  data: Record<string, unknown> | null;
}

/** `GET …/{dashboard_id}/events` (design §8.4). */
export interface DashboardEventsResponse {
  events: TelemetryEvent[];
  cursor: string | null;
  from: number;
  to: number;
}

// ----------------------------------------------------------------- clips

export type ClipState = 'playable' | 'upload_failed' | 'expired_or_missing' | 'not_allowed' | 'unavailable';

export interface ClipsQuery {
  from?: number;
  to?: number;
  reason?: string;
  device_id?: string;
  cursor?: string;
}

export interface TelemetryClip {
  clip_ref: string;
  clipId: string;
  deviceId: string;
  workflowVersion: string | number | null;
  startTs: number;
  endTs: number;
  durationS: number | null;
  reasons: Record<string, number>;
  totals: Record<string, unknown>;
  file: Record<string, unknown>;
  revision: number | null;
  state: ClipState;
  receivedMs: number;
  hasSidecar: boolean;
  /** `upload_failed` only: the device's error text, shown as plain text. */
  s3Error?: string | null;
  /** `not_allowed` / `unavailable`: the binding or read reason. */
  reason?: string;
  note?: string;
}

/** `GET …/{dashboard_id}/clips` (design §9.2). */
export interface DashboardClipsResponse {
  clips: TelemetryClip[];
  cursor: string | null;
  from: number;
  to: number;
}

/** `GET …/clips/{clip_ref}/media`: three presigned URLs valid `expires_in` seconds. */
export interface ClipMediaResponse {
  video_url: string;
  download_url: string;
  sidecar_url: string | null;
  expires_in: number;
  clipId: string | null;
}

// ------------------------------------------------------------------ live

/** `POST …/{dashboard_id}/live-lease` (design §9.3). */
export interface LiveLeaseResponse {
  device_id: string;
  workflow_id: string;
  slot: number;
  startedAt: number;
  leaseExpiresAt: number;
  expiresAt: number;
  renewSeconds: number;
  region: string | null;
  region_source: 'header' | 'usecase' | null;
}

export interface LiveFrame {
  url: string;
  width: number | null;
  height: number | null;
  ts: number | null;
  age_s: number;
  perClass: Record<string, number>;
  people: number | null;
  violation: boolean | null;
  reasons: Record<string, number>;
  expires_in: number;
}

/**
 * `GET …/{dashboard_id}/live?device_id=&have=` (design §9.3): `frame` null
 * with the echoed token when nothing newer exists, `state: no_live_source`
 * without a pointer, `unavailable` with its reason.
 */
export interface LiveFrameResponse {
  frame: LiveFrame | null;
  frame_token: string | null;
  age_s?: number;
  state?: 'no_live_source';
  unavailable?: 'not_allowed' | 'frame_too_large';
  reason?: string;
}
