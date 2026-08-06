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
export type BuildTarget = 'JP5' | 'JP6' | 'AMD64' | 'AMD64_NVIDIA';

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
  config_snapshot?: Record<string, unknown>;
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
 * One page of GET /builds/{id}/logs. CloudWatch returns the same
 * nextToken when the page is exhausted; a running build's viewer keeps
 * polling that token for new output (Req 4.4).
 */
export interface BuildLogsPage {
  events: BuildLogEvent[];
  nextToken: string | null;
}
