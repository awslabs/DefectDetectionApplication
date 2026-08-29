/**
 * TypeScript mirrors of the LocalServer API payloads consumed by the HMI,
 * plus narrow defensive parse functions.
 *
 * Defensive boundary (design "Data Models"): every payload entering the app
 * passes through a parse function that
 *   - ignores unknown fields,
 *   - defaults missing or wrongly-typed fields,
 *   - never throws on malformed input.
 *
 * All display behavior derives from these parsed fields alone; nothing is
 * keyed to specific workflow names or ids (Requirement 2.6). Metadata parsing
 * is tolerant of empty/partial objects so verdict-less completed runs render
 * without error (Requirement 4.7).
 */

// --------------------------------------------------------------------------
// API payload mirrors
// --------------------------------------------------------------------------

/** One entry of `GET /workflows/registrations`. */
export interface Registration {
  registrationId: string;
  workflowId: string;
  /** Manifest workflowName; may be null. */
  name: string | null;
  version: string;
  /** "registered" (active) | "invalid" | retired statuses. */
  status: string;
  /** Epoch seconds. */
  registeredAt: number;
}

export type ExecutionStatus = "pending" | "running" | "completed" | "failed";

/** The backend's `execution_to_dict` shape. */
export interface Execution {
  executionId: string;
  registrationId: string;
  status: ExecutionStatus;
  /** Epoch seconds. */
  startedAt: number;
  /** Epoch seconds; null while not finished. */
  finishedAt: number | null;
  failingNodeId: string | null;
  error: string | null;
  hasImageResults: boolean;
  captureId: string | null;
}

/** One entry of the `images` array of `GET .../results`. */
export interface ResultImage {
  kind: "output" | "node";
  nodeId?: string;
  port?: string; // "in" | "reference" | other
  hasOverlay: boolean;
}

/** `GET .../metadata` — best-effort; may be empty (Requirement 4.7). */
export interface RunMetadata {
  is_anomalous?: boolean;
  confidence?: number;
  generated_text?: string;
  [key: string]: unknown;
}

// --------------------------------------------------------------------------
// Narrowing helpers (internal)
// --------------------------------------------------------------------------

function asRecord(raw: unknown): Record<string, unknown> {
  return typeof raw === "object" && raw !== null && !Array.isArray(raw)
    ? (raw as Record<string, unknown>)
    : {};
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asFiniteNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asFiniteNumberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

const EXECUTION_STATUSES: readonly ExecutionStatus[] = [
  "pending",
  "running",
  "completed",
  "failed",
];

function asExecutionStatus(value: unknown): ExecutionStatus {
  // Unknown statuses default to "pending": a run with an unrecognized status
  // is never treated as a terminal run to display (non-destructive default).
  return EXECUTION_STATUSES.includes(value as ExecutionStatus)
    ? (value as ExecutionStatus)
    : "pending";
}

// --------------------------------------------------------------------------
// Parse functions
// --------------------------------------------------------------------------

/** Parses one registration entry; unknown fields ignored, missing defaulted. */
export function parseRegistration(raw: unknown): Registration {
  const r = asRecord(raw);
  return {
    registrationId: asString(r.registrationId, ""),
    workflowId: asString(r.workflowId, ""),
    name: asStringOrNull(r.name),
    version: asString(r.version, ""),
    // Empty default is intentionally non-active, so malformed entries are
    // excluded from the selection list rather than offered to the operator.
    status: asString(r.status, ""),
    registeredAt: asFiniteNumber(r.registeredAt, 0),
  };
}

/** Parses a `GET /workflows/registrations` payload; non-arrays yield []. */
export function parseRegistrations(raw: unknown): Registration[] {
  return Array.isArray(raw) ? raw.map(parseRegistration) : [];
}

/** Parses one execution entry; unknown fields ignored, missing defaulted. */
export function parseExecution(raw: unknown): Execution {
  const r = asRecord(raw);
  return {
    executionId: asString(r.executionId, ""),
    registrationId: asString(r.registrationId, ""),
    status: asExecutionStatus(r.status),
    startedAt: asFiniteNumber(r.startedAt, 0),
    finishedAt: asFiniteNumberOrNull(r.finishedAt),
    failingNodeId: asStringOrNull(r.failingNodeId),
    error: asStringOrNull(r.error),
    hasImageResults: asBoolean(r.hasImageResults, false),
    captureId: asStringOrNull(r.captureId),
  };
}

/** Parses an executions list payload; non-arrays yield []. */
export function parseExecutions(raw: unknown): Execution[] {
  return Array.isArray(raw) ? raw.map(parseExecution) : [];
}

/** Parses one results-inventory image entry. */
export function parseResultImage(raw: unknown): ResultImage {
  const r = asRecord(raw);
  const image: ResultImage = {
    // Anything other than an explicit "node" is treated as the base output
    // entry; node-specific fields are only attached to node entries.
    kind: r.kind === "node" ? "node" : "output",
    hasOverlay: asBoolean(r.hasOverlay, false),
  };
  if (image.kind === "node") {
    if (typeof r.nodeId === "string") image.nodeId = r.nodeId;
    if (typeof r.port === "string") image.port = r.port;
  }
  return image;
}

/**
 * Parses the `images` array of a `GET .../results` payload, preserving the
 * backend's deterministic order that the image-pairing logic relies on.
 * Accepts either the images array itself or the whole results object.
 */
export function parseResultImages(raw: unknown): ResultImage[] {
  const images = Array.isArray(raw) ? raw : asRecord(raw).images;
  return Array.isArray(images) ? images.map(parseResultImage) : [];
}

/**
 * Parses a `GET .../metadata` payload. Only correctly-typed verdict fields
 * are kept; everything else is ignored. An empty or malformed payload parses
 * to {} so completed runs without a verdict render verdict-free rather than
 * erroring (Requirement 4.7).
 */
export function parseRunMetadata(raw: unknown): RunMetadata {
  const r = asRecord(raw);
  const metadata: RunMetadata = {};
  if (typeof r.is_anomalous === "boolean") metadata.is_anomalous = r.is_anomalous;
  if (typeof r.confidence === "number" && Number.isFinite(r.confidence)) {
    metadata.confidence = r.confidence;
  }
  if (typeof r.generated_text === "string") {
    metadata.generated_text = r.generated_text;
  }
  return metadata;
}
