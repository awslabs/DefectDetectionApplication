/**
 * Types for the portal build fleet API (build_jobs.py / build_fleet.py).
 *
 * Field names mirror the BuildJobs / BuildServers DynamoDB records the
 * backend returns verbatim (build_domain.create_build_jobs, build_events
 * transitions, build_fleet reconciliation).
 *
 * Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
 */

/** The four supported Build_Targets (Req 1.1). */
export type BuildTarget = 'JP5' | 'JP6' | 'JP7' | 'AMD64' | 'AMD64_NVIDIA';

/** Build_Job execution modes (Req 2.1). */
export type BuildExecutionMode = 'ephemeral' | 'dedicated';

/** Build_Job statuses; the last four are terminal (Req 4.1). */
export type BuildJobStatus =
  | 'queued'
  | 'provisioning'
  | 'building'
  | 'publishing'
  | 'succeeded'
  | 'failed'
  | 'interrupted'
  | 'cancelled';

/** Statuses a Build_Job never leaves once reached (Req 4.1). */
export const TERMINAL_BUILD_STATUSES: ReadonlySet<BuildJobStatus> = new Set([
  'succeeded',
  'failed',
  'interrupted',
  'cancelled',
]);

export function isTerminalBuildStatus(status?: string): boolean {
  return TERMINAL_BUILD_STATUSES.has(status as BuildJobStatus);
}

/** Statuses for which POST /builds/{id}/cancel is accepted (Req 4.5, 4.6). */
export const CANCELLABLE_BUILD_STATUSES: ReadonlySet<BuildJobStatus> = new Set([
  'queued',
  'building',
  'publishing',
]);

/**
 * Result metadata recorded on a succeeded Build_Job (Req 5.3): the
 * published Greengrass component version identifier and pushed image
 * references from the build agent's PORTAL_BUILD_RESULT line.
 */
export interface BuildJobResult {
  component_name?: string;
  published_version?: string;
  pushed_image_refs?: string[];
  /** Per-artifact lists recorded on a publishing failure (Req 5.4). */
  published?: string[];
  unpublished?: string[];
}

/** Error recorded on a failed Build_Job (build vs publish kinds, Req 5.4). */
export interface BuildJobError {
  kind?: string;
  code?: string;
  message?: string;
  published?: string[];
  unpublished?: string[];
}

/**
 * Typed view of a Build_Job's `config_snapshot` — the immutable copy of
 * the effective build configuration taken at submission. Only the
 * source-selection keys the builds UI reads are typed
 * (build-source-selection Req 1.6, 2.5, 4.5); every other configuration
 * parameter stays `unknown`.
 */
export interface BuildConfigSnapshot {
  /** Normalized repository URL the job builds from (Req 1.6). */
  repository?: string;
  /**
   * Branch, tag, or commit SHA the job builds; null or absent means
   * the repository's default branch (Req 2.5).
   */
  source_ref?: string | null;
  /** Resolved commit SHA, when recorded on the snapshot (Req 4.5). */
  source_commit?: string;
  [key: string]: unknown;
}

/** One Build_Job record as returned by GET /builds and GET /builds/{id}. */
export interface BuildJob {
  build_job_id: string;
  request_id: string;
  request_order: number;
  predecessor_job_id: string | null;
  build_target: BuildTarget;
  component_name: string;
  required_arch: 'arm64' | 'x86_64';
  execution_mode: BuildExecutionMode;
  /** Dedicated_Build_Server id; null for ephemeral jobs. */
  server_id: string | null;
  status: BuildJobStatus;
  requested_by: string;
  /** Epoch milliseconds. */
  created_at: number;
  /** Set when the job enters `building` (Req 4.3). */
  started_at?: number;
  /** Set when the job reaches a terminal status (Req 4.3). */
  ended_at?: number;
  /** Reference from a retry job to the interrupted original (Req 3.6). */
  retry_of?: string;
  result?: BuildJobResult;
  error?: BuildJobError;
  config_snapshot?: BuildConfigSnapshot;
  /**
   * Resolved commit SHA persisted from the agent's phase=building event
   * (build-source-selection Req 4.5); absent on jobs from legacy agents.
   */
  source_commit?: string;
  /**
   * Optional persisted diagnostic records (build-fleet-execution-failures
   * Req 3.6): additive snake_case structures written by reconciliation;
   * absent on legacy jobs. The camelCase projection the UI renders comes
   * from GET /builds/{id}/logs (`BuildLogsPage.diagnostic`).
   */
  execution_diagnostic?: Record<string, unknown>;
  timing?: Record<string, unknown>;
  timeout_evidence?: Record<string, unknown>;
}

// Dedicated_Build_Server types (BuildServer, BuildServersResponse and
// the fleet lifecycle methods) live in services/api.ts, added with the
// Fleet page (task 12.3); the build page consumes them from there.

/** Body of POST /builds (Req 1.1, 2.1). */
export interface SubmitBuildRequest {
  /** Selected Build_Targets in request order (Req 1.3). */
  targets: BuildTarget[];
  execution_mode: BuildExecutionMode;
  /** Required when execution_mode is 'dedicated' (Req 2.6). */
  server_id?: string;
  /**
   * Repository to clone; omitted means the configured default
   * repository (build-source-selection Req 1.2, 1.3).
   */
  repository?: string;
  /**
   * Branch, tag, or commit SHA to build; omitted means the selected
   * repository's default branch (build-source-selection Req 2.4, 2.7).
   */
  source_ref?: string;
}

/** Response of POST /builds: one Build_Job per selected target. */
export interface SubmitBuildResponse {
  request_id: string;
  jobs: BuildJob[];
}

/** One page of GET /builds (90-day history, most recent first, Req 4.7). */
export interface BuildJobsPage {
  jobs: BuildJob[];
  nextToken: string | null;
  total: number;
}

/** One CloudWatch log event of GET /builds/{id}/logs (Req 4.4). */
export interface BuildLogEvent {
  timestamp: number;
  message: string;
}

/**
 * One projected stdout/stderr excerpt of the execution diagnostic
 * (build-fleet-execution-failures Req 2.2, 2.18). `available: false`
 * means the provider never returned the field (explicitly unavailable,
 * never fabricated); an available-but-empty field is
 * `{available: true, text: ''}`. `truncated` marks byte-bounded text.
 */
export interface DiagnosticStreamField {
  available: boolean;
  text?: string;
  truncated?: boolean;
}

/**
 * Phase durations of the execution diagnostic (Req 2.13, 2.18): queue
 * wait, provisioning, and active execution are accounted separately.
 * A null/absent value means that phase has no recorded evidence.
 */
export interface DiagnosticTiming {
  queueMs?: number | null;
  provisioningMs?: number | null;
  executionMs?: number | null;
}

/**
 * Terminal timeout evidence, present only when a timeout decision was
 * made (Req 2.16, 2.18): the timeout kind, the budget that expired with
 * its value and source, and the last observed heartbeat/progress.
 */
export interface DiagnosticTimeout {
  kind?: string | null;
  phase?: string | null;
  observedMs?: number | null;
  budgetMs?: number | null;
  budgetSource?: string | null;
  hardRuntimeMs?: number | null;
  lastHeartbeatAt?: number | null;
  lastProgressAt?: number | null;
  buildTarget?: string | null;
  executionMode?: string | null;
  decidedAt?: number | null;
}

/**
 * Optional disk-capacity evidence recorded by the dispatch preflight
 * (Req 2.23). `available: false` means the measurement was not taken.
 */
export interface DiagnosticDisk {
  available: boolean;
  docker_storage_path?: string;
  available_gb?: number;
  used_gb?: number;
  total_gb?: number;
  measured_at?: number;
}

/**
 * The optional versioned execution diagnostic returned with Build Log
 * pages (design "Build Log Persistence, API, and UI"; Req 2.2, 2.3).
 * Returned independently of CloudWatch stream existence and repeated
 * across pages as immutable metadata. Legacy responses omit it.
 */
export interface ExecutionDiagnostic {
  schemaVersion: number;
  classification?: string | null;
  status?: string | null;
  statusDetails?: string | null;
  responseCode?: number | null;
  stdout?: DiagnosticStreamField;
  stderr?: DiagnosticStreamField;
  timing?: DiagnosticTiming;
  timeout?: DiagnosticTimeout;
  disk?: DiagnosticDisk;
  observedAt?: number | null;
  complete?: boolean;
}

/**
 * One page of GET /builds/{id}/logs. CloudWatch returns the same
 * nextToken when the page is exhausted; a running build's viewer keeps
 * polling that token for new output (Req 4.4). `events` and `nextToken`
 * semantics are unchanged; `diagnostic` is optional and additive
 * (build-fleet-execution-failures Req 2.3, 3.6).
 */
export interface BuildLogsPage {
  events: BuildLogEvent[];
  nextToken: string | null;
  diagnostic?: ExecutionDiagnostic;
}

/**
 * Response of GET /build-branches?repository=… (build-source-selection
 * Req 3.1): the repository's branches with its default branch
 * identified. `truncated` is set when the repository has more branches
 * than discovery's page cap returned (the default branch is still
 * always present in the list).
 */
export interface BuildBranchesResponse {
  branches: string[];
  default_branch: string;
  truncated: boolean;
}
