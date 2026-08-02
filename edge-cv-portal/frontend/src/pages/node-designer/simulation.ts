/**
 * Simulator-view helpers (custom-node-designer, task 12.4).
 *
 * Pure logic behind SimulatorView, kept out of the component so the
 * missing-x86_64 refusal (Requirement 7.5), parameter-editor value
 * handling (7.4), frame-strip presentation (7.3), and failure/timeout
 * presentation with partial results (7.6, 7.7) are unit-testable.
 */
import { ApiError } from '../../services/api';
import type {
  PluginArtifactEntry,
  SimulationFrameRecord,
  SimulationResultsDocument,
  SimulationRunSummary,
} from './types';

// ------------------------------------------------------ the x86_64 guard

/**
 * Client-side mirror of the backend simulation guard (7.5): a run may
 * start exactly when the version has a successfully built x86_64
 * Plugin_Artifact with a stored Plugin_Library key. Used to show the
 * refusal before the user fills the form; the backend re-checks and
 * returns 409 SIMULATION_REQUIRES_X86_64_BUILD regardless.
 */
export function hasSuccessfulX86Build(
  artifacts: Record<string, PluginArtifactEntry> | null | undefined
): boolean {
  const entry = artifacts?.['x86_64'];
  return Boolean(entry && entry.buildStatus === 'succeeded' && entry.s3Key);
}

/** Error code of the backend's missing-x86_64 refusal (7.5). */
export const MISSING_X86_64_CODE = 'SIMULATION_REQUIRES_X86_64_BUILD';

/** Fixed refusal text shown when the guard fails client-side (7.5). */
export const MISSING_X86_64_MESSAGE =
  'Simulation requires a successful x86_64 build: this version has no ' +
  'successfully built x86_64 Plugin_Artifact. Build the plugin for x86_64 ' +
  'and retry.';

// -------------------------------------------------------- parameter rows

/** One row of the parameter editor (7.4). */
export interface ParameterRow {
  name: string;
  value: string;
}

/**
 * Coerce a parameter-editor text value to the scalar the backend
 * accepts: booleans and finite numbers are typed, everything else
 * stays a string (GObject property values are parsed by GStreamer).
 */
export function coerceParameterValue(text: string): string | number | boolean {
  const trimmed = text.trim();
  if (trimmed === 'true') return true;
  if (trimmed === 'false') return false;
  if (trimmed !== '' && !Number.isNaN(Number(trimmed))) return Number(trimmed);
  return text;
}

/**
 * Assemble the run's `parameters` object from the editor rows,
 * dropping rows without a name. Later duplicate names win, matching
 * plain object assignment.
 */
export function parametersFromRows(
  rows: ParameterRow[]
): Record<string, string | number | boolean> {
  const parameters: Record<string, string | number | boolean> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) continue;
    parameters[name] = coerceParameterValue(row.value);
  }
  return parameters;
}

/** Editor rows from a previous run's parameters, for re-run editing (7.4). */
export function rowsFromParameters(
  parameters: Record<string, unknown> | null | undefined
): ParameterRow[] {
  return Object.entries(parameters || {}).map(([name, value]) => ({
    name,
    value: value === null || value === undefined ? '' : String(value),
  }));
}

// ------------------------------------------------------- sample uploads

/** Sample frame extensions the backend accepts (7.1 upload path). */
export const SUPPORTED_FRAME_EXTENSIONS = ['.jpg', '.jpeg', '.png'];

/** Whether a file name is an accepted JPEG/PNG sample frame (7.1). */
export function isSupportedFrameName(name: string): boolean {
  const dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return SUPPORTED_FRAME_EXTENSIONS.includes(name.slice(dot).toLowerCase());
}

/**
 * Extract the raw base64 payload from a FileReader data URL
 * (`data:image/png;base64,....`) for the `sample_frames`
 * `content_base64` field.
 */
export function dataUrlToBase64(dataUrl: string): string {
  const comma = dataUrl.indexOf(',');
  return comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
}

// ------------------------------------------------------- frame rendering

/** Display label for a frame reference: the file name of the S3 key. */
export function frameLabel(ref: string | null | undefined): string {
  if (!ref) return '';
  const slash = ref.lastIndexOf('/');
  return slash >= 0 ? ref.slice(slash + 1) : ref;
}

/** Whether a frame reference is directly renderable as an image URL. */
export function isRenderableUrl(ref: string | null | undefined): boolean {
  return typeof ref === 'string' && /^https?:\/\//.test(ref);
}

/**
 * The ordered per-frame records of a results document (7.3). The
 * harness flushes incrementally and orders by frameIndex; sorting here
 * keeps the strip stable for partial documents too (7.6, 7.7).
 */
export function orderedFrames(
  results: SimulationResultsDocument | null | undefined
): SimulationFrameRecord[] {
  const frames = results?.frames;
  if (!Array.isArray(frames)) return [];
  return [...frames].sort((a, b) => a.frameIndex - b.frameIndex);
}

// -------------------------------------------------------- run presentation

/** Whether a run status is terminal (stop polling). */
export function isTerminalStatus(status: string | null | undefined): boolean {
  return status === 'completed' || status === 'failed';
}

/** Presentation of one failed or timed-out run (7.6, 7.7). */
export interface SimulationFailureView {
  header: string;
  message: string;
  timeout: boolean;
  /** Captured plugin error output, when the harness flushed one (7.6). */
  errorOutput: string | null;
}

/**
 * Map a failed run (and the results document produced so far) to its
 * alert presentation: timeouts are labeled as such, plugin failures
 * carry the captured error output, and in both cases the partial
 * results remain rendered beside the alert (7.6, 7.7).
 */
export function describeRunFailure(
  run: SimulationRunSummary,
  results: SimulationResultsDocument | null | undefined
): SimulationFailureView | null {
  if (run.status !== 'failed') return null;
  const failure = run.failure || { message: 'Simulation run failed' };
  const errorOutput = results?.error?.errorOutput;
  return {
    header: failure.timeout ? 'Simulation timed out' : 'Simulation failed',
    message: failure.message || 'Simulation run failed',
    timeout: Boolean(failure.timeout),
    errorOutput:
      typeof errorOutput === 'string' && errorOutput.trim() ? errorOutput : null,
  };
}

/** Presentation of a start-request failure, including the 409 guard (7.5). */
export function describeStartError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === MISSING_X86_64_CODE) {
      return err.message || MISSING_X86_64_MESSAGE;
    }
    return err.message || 'The simulation run could not be started.';
  }
  return err instanceof Error && err.message
    ? err.message
    : 'The simulation run could not be started.';
}
