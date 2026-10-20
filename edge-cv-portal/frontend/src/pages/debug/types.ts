/**
 * Types of the workflow debug mode (workflow-debug-mode design §4, §7.2,
 * §8): the session view the API answers, the creation body and answer, the
 * `/log` query and its events, the four `dda.debug.v1` record shapes and the
 * error envelope the API uses for every refusal.
 *
 * The shapes mirror `workflow_debug_api.py` (`_session_view`, `create_session`,
 * `read_log`) and `workflow_debug/log_filters.to_answer_event`; nothing here
 * is validated client-side beyond what the forms need (the server decides).
 */

/** Portal-side session states (design §7.1). */
export type DebugSessionState = 'pending' | 'active' | 'stopped' | 'expired';

/** The device agent's own state of a session entry (design §3, `reported`). */
export type DebugReportedState =
  | 'pending'
  | 'active'
  | 'expired'
  | 'stopped'
  | 'no_registration'
  | 'rejected';

export type DebugLevel = 'info' | 'debug';

/** Result of the Portal's shadow write for one device (design §7.3). */
export type DebugShadowResult = 'ok' | 'failed' | 'pending';

/** The `session` record's counters, also carried in `reported.counters`. */
export interface DebugAgentCounters {
  records?: number;
  bytes?: number;
  queueDrops?: number;
  tapDrops?: number;
  truncations?: number;
  rateDeferred?: number;
  bytesDeferred?: number;
  spoolFull?: number;
  imagesSent?: number;
  imagesCapped?: number;
  imagesTooLarge?: number;
  publishFailures?: number;
  unattributedLines?: number;
  otherPairLines?: number;
}

/** `reported.sessions.<workflowId>` of the device plus `instance` and
 *  `agentVersion` from the reported document (`shadow.read_reported`). */
export interface DebugReportedEntry {
  sessionId?: string;
  state?: DebugReportedState | string;
  reason?: string;
  appliedAt?: number;
  counters?: DebugAgentCounters;
  instance?: string;
  agentVersion?: string;
}

export interface DebugSessionOptions {
  level: DebugLevel;
  images: boolean;
  imageMaxKb: number;
  maxMessagesPerSecond: number;
  maxBytesPerSecond?: number;
  durationMinutes?: number;
}

export interface DebugSessionDevice {
  device_id: string;
  state: DebugSessionState | null;
  shadow: { result?: DebugShadowResult | string; error?: string; writtenAt?: number };
  logStream: string | null;
  /** The device's reported entry, or null when it has not reported (yet). */
  reported: DebugReportedEntry | null;
}

/** One session as `GET /debug/usecases/{id}/sessions` and
 *  `GET /debug/sessions/{id}` answer it. */
export interface DebugSession {
  session_id: string;
  usecase_id: string;
  workflow_id: string;
  workflow_version: string | null;
  device_ids: string[];
  all_devices: boolean;
  state: DebugSessionState;
  enabledAt: number;
  expiresAt: number;
  stoppedAt: number | null;
  actor: string | null;
  options: Partial<DebugSessionOptions>;
  counters: { records?: number; bytes?: number };
  logGroup: string | null;
  media: { bucket?: string; region?: string; region_source?: 'header' | 'usecase' };
  devices: DebugSessionDevice[];
}

export interface ListDebugSessionsResponse {
  sessions: DebugSession[];
  /** Opaque page token, null on the last page. */
  next: string | null;
}

/** Body of `POST /debug/usecases/{usecase_id}/sessions` (design §7.2, §13). */
export interface CreateDebugSessionBody {
  workflow_id: string;
  /** Exactly one of `device_id` and `all_devices: true`. */
  device_id?: string;
  all_devices?: boolean;
  /** 5–1440, default 60. */
  duration_minutes?: number;
  /** Default `debug`. */
  level?: DebugLevel;
  /** Default false; refused with 409 while registry enforcement is off. */
  images?: boolean;
  /** 50–300, default 150. */
  image_max_kb?: number;
}

export interface DebugShadowWriteRow {
  device_id: string;
  shadow: 'ok' | 'failed';
  error?: string;
}

/** The 201 answer of the creation route. */
export interface CreateDebugSessionResponse {
  session_id: string;
  state: 'active' | 'pending';
  devices: DebugShadowWriteRow[];
  region_source: 'header' | 'usecase';
  /** Present when the bucket's region fell back to the use case's region. */
  warning?: string;
  enabledAt: number;
  expiresAt: number;
  logGroup: string;
}

/** The 200 answer of `DELETE /debug/sessions/{session_id}`. */
export interface StopDebugSessionResponse {
  session_id: string;
  state: 'stopped';
  devices: DebugShadowWriteRow[];
  stoppedAt: number;
}

export type DebugLogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';
export type DebugRecordType = 'run' | 'log' | 'drop' | 'session';

/**
 * Query of `GET /debug/usecases/{usecase_id}/log`. `from`/`to` bound the
 * sink's write time (epoch ms; default the last 15 minutes; at most 24 h
 * apart); `next` continues within the same window only; `limit` 1–10000
 * (default 2000); `ts_from`/`ts_to` bound the record's own `ts`.
 */
export interface DebugLogQuery {
  device_id: string;
  workflow_id: string;
  from?: number;
  to?: number;
  next?: string;
  limit?: number;
  execution_id?: string;
  node_id?: string;
  level?: DebugLogLevel;
  type?: DebugRecordType;
  ts_from?: number;
  ts_to?: number;
}

/** The stamps the ingest adds to every record (design §6.2 step 5–6). */
export interface DebugRecordStamps {
  /** `<sessionId>:<instance>:<seq>:<index>` */
  id?: string;
  writeTs?: number;
  receivedMs?: number;
}

/** `trigger` of a `run` record (design §4.2). */
export interface DebugRunTrigger {
  type: 'mqtt' | 'opcua' | 'continuous' | 'manual' | 'unknown' | string;
  source: string | null;
  topic: string | null;
  payload: string | null;
  timestamp: number | null;
  receivedMs: number | null;
}

export interface DebugRunNode {
  nodeId: string;
  status: string;
  durationMs?: number | null;
  detail?: string | null;
}

export interface DebugDetection {
  label?: string;
  confidence?: number;
  box?: number[];
  id?: string;
  [key: string]: unknown;
}

export interface DebugRunBinding {
  nodeId: string;
  kind: string;
  result: string;
  detail?: string | null;
}

export interface DebugRunLogLine {
  ts: number;
  level: string;
  logger: string;
  message: string;
  src: 'runlog' | 'tap' | string;
}

export interface DebugTruncation {
  field: string;
  originalBytes: number;
}

export interface DebugRunImage {
  bucket: string;
  key: string;
  width?: number;
  height?: number;
  bytes?: number;
}

export interface DebugRunRecord extends DebugRecordStamps {
  type: 'run';
  ts: number;
  executionId: string;
  status: 'completed' | 'failed' | string;
  enqueuedAtS?: number | null;
  acceptedMs?: number | null;
  startedMs?: number | null;
  finishedMs?: number | null;
  queuedMs?: number | null;
  setupMs?: number | null;
  readinessWaitMs?: number | null;
  durationMs?: number | null;
  trigger?: DebugRunTrigger | null;
  nodes?: DebugRunNode[];
  error?: string | null;
  traceback?: string | null;
  failingNodeId?: string | null;
  outputs?: {
    detections?: DebugDetection[];
    llm?: Record<string, { text?: string; error?: string; isAnomalous?: boolean; confidence?: number }>;
    bedrock?: Record<
      string,
      { text?: string; error?: string; isAnomalous?: boolean; confidence?: number; detectionId?: string }
    >;
    isAnomalous?: boolean | null;
    confidence?: number | null;
  } | null;
  bindings?: DebugRunBinding[];
  model?: {
    readiness?: Array<{ model: string; state: string; waitedMs?: number; ready?: boolean }>;
    wait?: {
      model?: string;
      tritonModel?: string;
      state?: string;
      reason?: string;
      sinceMs?: number;
      stalled?: boolean;
    } | null;
  } | null;
  frameSource?: {
    kind?: string | null;
    cameraId?: string | null;
    width?: number | null;
    height?: number | null;
    frameSeq?: number | null;
    frameTs?: number | null;
  } | null;
  artifacts?: { outputDir?: string; captureId?: string; hasOverlay?: boolean } | null;
  image?: DebugRunImage | null;
  logs?: DebugRunLogLine[];
  truncated?: DebugTruncation[];
}

export interface DebugLogRecord extends DebugRecordStamps {
  type: 'log';
  ts: number;
  level: string;
  logger: string;
  message: string;
  attribution?: 'between_runs' | 'device' | string;
}

export interface DebugDropRecord extends DebugRecordStamps {
  type: 'drop';
  ts: number;
  nodeId: string;
  reason: 'queue_full' | 'drop_policy' | 'skipped_busy' | 'skipped_no_new_frame' | string;
  count: number;
}

export interface DebugSessionRecord extends DebugRecordStamps {
  type: 'session';
  ts: number;
  state: string;
  reason: 'started' | 'resumed' | 'heartbeat' | 'expired' | 'stopped' | string;
  instance?: string;
  counters?: DebugAgentCounters;
  agentVersion?: string;
  level?: DebugLevel | string;
  images?: boolean;
  levelDowngraded?: boolean;
}

/** A record of a type this viewer does not know (shown raw, design §4.3),
 *  including the API's `raw` wrapper for a non-JSON message. */
export interface DebugOtherRecord extends DebugRecordStamps {
  type: string;
  ts?: number | null;
  message?: string;
  [key: string]: unknown;
}

export type DebugRecord =
  | DebugRunRecord
  | DebugLogRecord
  | DebugDropRecord
  | DebugSessionRecord
  | DebugOtherRecord;

/** One element of the `/log` answer's `events`. */
export interface DebugEvent {
  id: string | null;
  /** The sink's write time: the CloudWatch event timestamp. */
  writeTs: number;
  /** When IoT received the message (the ingest's stamp), null for raw events. */
  receivedMs: number | null;
  /** The record's own time, null when the record carries none. */
  ts: number | null;
  record: DebugRecord;
}

export interface DebugLogResponse {
  events: DebugEvent[];
  /** Continues within the same `from`/`to` window; null when exhausted. */
  next: string | null;
}

/** `GET /debug/sessions/{id}/images/{execution_id}`: a 120 s presigned GET. */
export interface DebugImageUrlResponse {
  url: string;
  expires_in: number;
  key: string;
}

/** `GET /debug/sessions/{id}/console-link` (PortalAdmin only). */
export interface DebugConsoleLinkResponse {
  url: string;
  region: string;
  logGroup: string;
  logStream: string;
  note: string;
}

/**
 * The refusal envelope of every debug route: `{error, error_code, remedy?}`
 * plus the detail keys the design names (design §12, §7.2). A 429
 * `log_read_throttled` carries `retry_after_ms`; the client never retries it
 * on its own (the tail backs off, the download pauses, design §8/§10.1).
 */
export interface DebugErrorBody {
  error: string;
  error_code: string;
  remedy?: string;
  retry_after_ms?: number;
  field?: string;
  limit?: 'usecase' | 'device' | 'devices_per_session';
  active?: number;
  max?: number;
  device?: string;
  device_id?: string;
  session_id?: string;
  state?: string;
  reason?: string;
  no_deployment?: string[];
  no_device_record?: string[];
  required_permission?: string;
  required_role?: string;
  [key: string]: unknown;
}

/** Caps the pages show (the deployment defaults of design §10; the 409
 *  bodies carry the deployed `max`). */
export const DEBUG_DEFAULTS = {
  durationMinutes: 60,
  minDurationMinutes: 5,
  maxDurationMinutes: 1440,
  level: 'debug' as DebugLevel,
  imageMaxKb: 150,
  minImageMaxKb: 50,
  maxImageMaxKb: 300,
  maxSessionsPerUsecase: 4,
  maxSessionsPerDevice: 2,
  maxDevicesPerSession: 20,
} as const;
