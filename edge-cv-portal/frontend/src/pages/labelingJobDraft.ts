/**
 * Labeling-job Setup_Draft persistence and recovery helpers
 * (spec: labeling-setup-session-recovery).
 *
 * The labeling-job creation wizard (`CreateLabelingJob.tsx`) continuously
 * saves its Wizard_Setup_State as a versioned Setup_Draft in browser
 * localStorage (the Draft_Store), keyed per Use_Case, so a refresh, tab
 * close, crash, or session expiry can be recovered from. This module owns
 * everything about that draft except the wizard wiring itself: the draft
 * schema, the storage-key derivation, the tolerant versioned
 * read/write/clear accessors, the staleness policy, the preview-run
 * Resume_Window rule, and the pure example-ref merge helpers.
 *
 * Follows the TestPanel persistence precedent (`workflows/TestPanel.tsx`,
 * `readPersistedTestRun` / `persistTestRun` / `clearPersistedTestRun`):
 * every accessor is tolerant — storage exceptions and malformed content
 * are swallowed and reported as absence, never thrown (Requirements 1.5,
 * 6.5).
 *
 * The draft schema carries only setup metadata, S3 references, and the
 * Preview_Run_Reference — no credential material and no bearer token
 * exist anywhere in the schema (Requirement 1.6).
 */

/** Storage key prefix; one draft per use case (Requirement 1.1). */
export const LABELING_JOB_DRAFT_STORAGE_PREFIX = 'edgeCvPortal.labelingJobDraft.';

/** The Draft_Key of one Use_Case's Setup_Draft (Requirement 1.1). */
export function labelingJobDraftKey(usecaseId: string): string {
  return `${LABELING_JOB_DRAFT_STORAGE_PREFIX}${usecaseId}`;
}

/** Schema version this module reads and writes (Requirement 6.2). */
export const LABELING_JOB_DRAFT_VERSION = 1;

/** Draft_Staleness_Bound: 14 days in milliseconds (Requirement 6.3). */
export const DRAFT_STALENESS_MS = 14 * 24 * 60 * 60 * 1000;

/** Debounce for the wizard's draft save effect (Requirement 1.1). */
export const DRAFT_SAVE_DEBOUNCE_MS = 750;

/** One example-image S3 ref restored from a Setup_Draft (`s3://bucket/key`). */
export interface RestoredExampleRef {
  ref: string;
}

/**
 * The persisted identity of the most recently started Preview_Run
 * (Requirement 2.4): enough to resume polling it, and to decide whether
 * the Resume_Window still holds (Requirements 5.1, 5.5).
 */
export interface PreviewRunReference {
  runId: string;
  sampleCount: number;
  startedAtMs: number;
}

/**
 * The Setup_Draft: the versioned JSON serialization of the persistable
 * part of the Wizard_Setup_State (Requirement 2.1). Field sources are
 * the same-named `CreateLabelingJob.tsx` state values; `autoLabelModel`
 * is the recorded selection value (`sam`, `bedrock:<id>`, or `llm:<id>`)
 * verbatim, independent of the Auto_Label_Picker's current catalog
 * (Requirement 2.2). `exampleRefs` carries the Merged_Example_Refs —
 * uploaded `s3://` refs only, never `File` content (Requirements 2.3,
 * 2.5). No credential or token field exists (Requirement 1.6).
 */
export interface LabelingJobDraft {
  version: 1;
  savedAtMs: number;
  usecaseId: string;
  /** Wizard step position; clamped to 0..5 on read. */
  activeStepIndex: number;
  labelingBackend: '' | 'DDA' | 'GroundTruth';
  jobName: string;
  description: string;
  datasetS3Uri: string;
  maskPrefix: string;
  /** `taskType?.value ?? ''` — option reconstructed on restore. */
  taskTypeValue: string;
  workforceTypeValue: string;
  labelCategories: string;
  gtInstructions: string;
  enableAutomatedLabeling: boolean;
  /** DDA label set rows verbatim, including empty rows. */
  ddaLabels: string[];
  ddaInstructions: string;
  selectedTeam: { teamId: string; teamName: string } | null;
  autoLabelEnabled: boolean;
  /** Recorded selection value, verbatim (Requirement 2.2). */
  autoLabelModel: string;
  detectionPrompt: string;
  fewShotEnabled: boolean;
  downscaleMaxEdge: number | null;
  /** Token budget as entered (the wizard state is the raw string). */
  tokenBudget: string;
  skipVerification: boolean;
  skipVerificationModelId: string;
  perLabelPrompts: Record<string, string>;
  /** Merged_Example_Refs per designation (Requirement 2.3). */
  exampleRefs: { good: string[]; bad: string[] };
  /** Sample_Selection of the Prompt_Tuning_Preview (Requirement 2.4). */
  previewSelectedKeys: string[];
  /** Most recently started Preview_Run (Requirement 2.4). */
  previewRun: PreviewRunReference | null;
}

/** Wizard step bounds used to clamp a restored `activeStepIndex`. */
const WIZARD_MIN_STEP_INDEX = 0;
const WIZARD_MAX_STEP_INDEX = 5;

// ---------------------------------------------------------------------------
// Field-by-field shape validation (the TestPanel `readPersistedTestRun`
// pattern scaled up): every scalar type-checked, arrays element-checked,
// unknown extra keys ignored by rebuilding a normalized object. Each
// extractor returns `undefined` for a non-conforming value; `null` stays
// a valid value where the schema allows it (Requirement 6.2).
// ---------------------------------------------------------------------------

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

function asFiniteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function asNullableFiniteNumber(value: unknown): number | null | undefined {
  return value === null ? null : asFiniteNumber(value);
}

function asStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const out: string[] = [];
  for (const entry of value) {
    if (typeof entry !== 'string') {
      return undefined;
    }
    out.push(entry);
  }
  return out;
}

/**
 * Keys are user-entered DDA label names, so a key literally named
 * `__proto__` is legitimate content — `JSON.parse` surfaces it as an own
 * property and it must survive normalization as one. The rebuild goes
 * through `Object.fromEntries`, whose CreateDataProperty semantics define
 * every key as an own data property; plain `out[key] = entry` assignment
 * would route `__proto__` through `Object.prototype`'s setter and
 * silently drop it. Total like its siblings: `undefined` for any
 * non-conforming input, never throws.
 */
function asStringRecord(value: unknown): Record<string, string> | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return undefined;
  }
  const entries: Array<[string, string]> = [];
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry !== 'string') {
      return undefined;
    }
    entries.push([key, entry]);
  }
  return Object.fromEntries(entries);
}

function asLabelingBackend(value: unknown): '' | 'DDA' | 'GroundTruth' | undefined {
  return value === '' || value === 'DDA' || value === 'GroundTruth' ? value : undefined;
}

function asTeam(value: unknown): { teamId: string; teamName: string } | null | undefined {
  if (value === null) {
    return null;
  }
  if (typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const teamId = asString(record.teamId);
  const teamName = asString(record.teamName);
  if (teamId === undefined || teamName === undefined) {
    return undefined;
  }
  return { teamId, teamName };
}

/**
 * A `previewRun` is accepted only as
 * `{runId: string, sampleCount: number, startedAtMs: number}` or null
 * (Requirement 6.2).
 */
function asPreviewRun(value: unknown): PreviewRunReference | null | undefined {
  if (value === null) {
    return null;
  }
  if (typeof value !== 'object' || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const runId = asString(record.runId);
  const sampleCount = asFiniteNumber(record.sampleCount);
  const startedAtMs = asFiniteNumber(record.startedAtMs);
  if (runId === undefined || sampleCount === undefined || startedAtMs === undefined) {
    return undefined;
  }
  return { runId, sampleCount, startedAtMs };
}

function asExampleRefs(value: unknown): { good: string[]; bad: string[] } | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const good = asStringArray(record.good);
  const bad = asStringArray(record.bad);
  if (good === undefined || bad === undefined) {
    return undefined;
  }
  return { good, bad };
}

/**
 * Validate parsed storage content field by field and rebuild it as a
 * normalized draft: wrong version, a `usecaseId` differing from the
 * key's, or any non-conforming field reports null; unknown extra keys
 * are dropped; `activeStepIndex` is clamped to 0..5 (Requirement 6.2).
 */
function conformingDraft(parsed: unknown, usecaseId: string): LabelingJobDraft | null {
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const record = parsed as Record<string, unknown>;

  // Unknown schema version: treated as no draft (Requirement 6.2).
  if (record.version !== LABELING_JOB_DRAFT_VERSION) {
    return null;
  }

  const savedAtMs = asFiniteNumber(record.savedAtMs);
  const draftUsecaseId = asString(record.usecaseId);
  const activeStepIndex = asFiniteNumber(record.activeStepIndex);
  const labelingBackend = asLabelingBackend(record.labelingBackend);
  const jobName = asString(record.jobName);
  const description = asString(record.description);
  const datasetS3Uri = asString(record.datasetS3Uri);
  const maskPrefix = asString(record.maskPrefix);
  const taskTypeValue = asString(record.taskTypeValue);
  const workforceTypeValue = asString(record.workforceTypeValue);
  const labelCategories = asString(record.labelCategories);
  const gtInstructions = asString(record.gtInstructions);
  const enableAutomatedLabeling = asBoolean(record.enableAutomatedLabeling);
  const ddaLabels = asStringArray(record.ddaLabels);
  const ddaInstructions = asString(record.ddaInstructions);
  const selectedTeam = asTeam(record.selectedTeam);
  const autoLabelEnabled = asBoolean(record.autoLabelEnabled);
  const autoLabelModel = asString(record.autoLabelModel);
  const detectionPrompt = asString(record.detectionPrompt);
  const fewShotEnabled = asBoolean(record.fewShotEnabled);
  const downscaleMaxEdge = asNullableFiniteNumber(record.downscaleMaxEdge);
  const tokenBudget = asString(record.tokenBudget);
  const skipVerification = asBoolean(record.skipVerification);
  const skipVerificationModelId = asString(record.skipVerificationModelId);
  const perLabelPrompts = asStringRecord(record.perLabelPrompts);
  const exampleRefs = asExampleRefs(record.exampleRefs);
  const previewSelectedKeys = asStringArray(record.previewSelectedKeys);
  const previewRun = asPreviewRun(record.previewRun);

  if (
    savedAtMs === undefined ||
    draftUsecaseId === undefined ||
    // Draft written for a different Use_Case than the key's (Req 6.2).
    draftUsecaseId !== usecaseId ||
    activeStepIndex === undefined ||
    labelingBackend === undefined ||
    jobName === undefined ||
    description === undefined ||
    datasetS3Uri === undefined ||
    maskPrefix === undefined ||
    taskTypeValue === undefined ||
    workforceTypeValue === undefined ||
    labelCategories === undefined ||
    gtInstructions === undefined ||
    enableAutomatedLabeling === undefined ||
    ddaLabels === undefined ||
    ddaInstructions === undefined ||
    selectedTeam === undefined ||
    autoLabelEnabled === undefined ||
    autoLabelModel === undefined ||
    detectionPrompt === undefined ||
    fewShotEnabled === undefined ||
    downscaleMaxEdge === undefined ||
    tokenBudget === undefined ||
    skipVerification === undefined ||
    skipVerificationModelId === undefined ||
    perLabelPrompts === undefined ||
    exampleRefs === undefined ||
    previewSelectedKeys === undefined ||
    previewRun === undefined
  ) {
    return null;
  }

  return {
    version: LABELING_JOB_DRAFT_VERSION,
    savedAtMs,
    usecaseId: draftUsecaseId,
    activeStepIndex: Math.min(
      Math.max(activeStepIndex, WIZARD_MIN_STEP_INDEX),
      WIZARD_MAX_STEP_INDEX
    ),
    labelingBackend,
    jobName,
    description,
    datasetS3Uri,
    maskPrefix,
    taskTypeValue,
    workforceTypeValue,
    labelCategories,
    gtInstructions,
    enableAutomatedLabeling,
    ddaLabels,
    ddaInstructions,
    selectedTeam,
    autoLabelEnabled,
    autoLabelModel,
    detectionPrompt,
    fewShotEnabled,
    downscaleMaxEdge,
    tokenBudget,
    skipVerification,
    skipVerificationModelId,
    perLabelPrompts,
    exampleRefs,
    previewSelectedKeys,
    previewRun,
  };
}

// ---------------------------------------------------------------------------
// Tolerant Draft_Store accessors (Requirements 1.5, 6.5): storage
// exceptions are swallowed and reported as absence — never thrown.
// ---------------------------------------------------------------------------

/**
 * Tolerant read: null for an absent key, unparsable JSON, a version
 * other than {@link LABELING_JOB_DRAFT_VERSION}, a `usecaseId` differing
 * from the key's, or a non-conforming shape (Requirement 6.2); a draft
 * whose `savedAtMs` is older than {@link DRAFT_STALENESS_MS} relative to
 * `nowMs` (default `Date.now()`) is purged from the Draft_Store and
 * reported as null (Requirement 6.3). Never throws (Requirement 6.5).
 */
export function readLabelingJobDraft(usecaseId: string, nowMs?: number): LabelingJobDraft | null {
  try {
    const raw = window.localStorage.getItem(labelingJobDraftKey(usecaseId));
    if (raw === null) {
      return null;
    }
    const draft = conformingDraft(JSON.parse(raw), usecaseId);
    if (draft === null) {
      return null;
    }
    const now = nowMs ?? Date.now();
    if (now - draft.savedAtMs > DRAFT_STALENESS_MS) {
      // Stale draft: treated as absent and removed (Requirement 6.3).
      clearLabelingJobDraft(usecaseId);
      return null;
    }
    return draft;
  } catch {
    // Storage unavailable or content unparsable: no draft (Req 6.2, 6.5).
    return null;
  }
}

/**
 * Write the Setup_Draft under the Use_Case's Draft_Key, stamping the
 * schema version, the save time, and the Use_Case id (Requirement 1.4).
 * A storage failure is swallowed: the wizard continues with unchanged
 * behavior minus persistence (Requirement 1.5).
 */
export function writeLabelingJobDraft(usecaseId: string, draft: LabelingJobDraft): void {
  try {
    const stamped: LabelingJobDraft = {
      ...draft,
      version: LABELING_JOB_DRAFT_VERSION,
      savedAtMs: Date.now(),
      usecaseId,
    };
    window.localStorage.setItem(labelingJobDraftKey(usecaseId), JSON.stringify(stamped));
  } catch {
    // Storage unavailable or full: continue without persistence (Req 1.5).
  }
}

/** Remove the Use_Case's Setup_Draft; storage failures are swallowed. */
export function clearLabelingJobDraft(usecaseId: string): void {
  try {
    window.localStorage.removeItem(labelingJobDraftKey(usecaseId));
  } catch {
    // Storage unavailable: nothing to clear (Requirement 6.5).
  }
}

// ---------------------------------------------------------------------------
// Preview_Run Resume_Window (Requirements 5.1, 5.5).
// ---------------------------------------------------------------------------

// Mirrors of the backend preview-persistence TTL constants in
// `edge-cv-portal/backend/functions/dda_labeling.py` (~lines 3174-3181):
// the RUN item's logical expiry is `expires_at = start +
// min(sample_count × PREVIEW_PER_SAMPLE_SECONDS +
// PREVIEW_LOCK_SLACK_SECONDS, PREVIEW_LOCK_TTL_MAX_SECONDS)` seconds and
// its DynamoDB `ttl` (the earliest possible reap time) is `expires_at +
// PREVIEW_ITEM_TTL_GRACE_SECONDS`. If the backend derivation ever
// changes, these four mirrors are the only thing to update.
const PREVIEW_PER_SAMPLE_SECONDS = 120;
const PREVIEW_LOCK_SLACK_SECONDS = 60;
const PREVIEW_LOCK_TTL_MAX_SECONDS = 900;
const PREVIEW_ITEM_TTL_GRACE_SECONDS = 3600;

/**
 * Resume_Window: the period after a Preview_Run's start during which its
 * backend items are guaranteed still readable —
 * `(min(sampleCount×120+60, 900) + 3600) × 1000` ms. DynamoDB never
 * reaps the RUN item before `expires_at` plus the TTL grace, so within
 * this window `GET /labeling-preview/runs/{runId}` is answerable; beyond
 * it, reaping is best-effort and the reference is dropped rather than
 * resurrected (Requirements 5.1, 5.5).
 */
export function previewRunResumeWindowMs(sampleCount: number): number {
  const expiresAfterSeconds = Math.min(
    sampleCount * PREVIEW_PER_SAMPLE_SECONDS + PREVIEW_LOCK_SLACK_SECONDS,
    PREVIEW_LOCK_TTL_MAX_SECONDS
  );
  return (expiresAfterSeconds + PREVIEW_ITEM_TTL_GRACE_SECONDS) * 1000;
}

/**
 * Whether a restored Preview_Run_Reference may still be resumed: true
 * exactly when the run started within its Resume_Window relative to
 * `nowMs`; false for a null or absent reference (Requirements 5.1, 5.5).
 */
export function canResumePreviewRun(
  ref: PreviewRunReference | null | undefined,
  nowMs: number
): boolean {
  if (!ref) {
    return false;
  }
  return nowMs - ref.startedAtMs <= previewRunResumeWindowMs(ref.sampleCount);
}

// ---------------------------------------------------------------------------
// Pure helpers for the wizard's save gate and restored example refs.
// ---------------------------------------------------------------------------

function stringArraysEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function stringRecordsEqual(a: Record<string, string>, b: Record<string, string>): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  return (
    aKeys.length === bKeys.length &&
    aKeys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && a[key] === b[key])
  );
}

/**
 * Deep equality of two drafts ignoring `savedAtMs` — the wizard's save
 * gate: a state whose draft equals the Pristine_State's draft is not
 * Draft_Worthy and is never written (Requirement 1.2).
 */
export function draftsEquivalent(a: LabelingJobDraft, b: LabelingJobDraft): boolean {
  const teamsEqual =
    a.selectedTeam === null || b.selectedTeam === null
      ? a.selectedTeam === b.selectedTeam
      : a.selectedTeam.teamId === b.selectedTeam.teamId &&
        a.selectedTeam.teamName === b.selectedTeam.teamName;
  const previewRunsEqual =
    a.previewRun === null || b.previewRun === null
      ? a.previewRun === b.previewRun
      : a.previewRun.runId === b.previewRun.runId &&
        a.previewRun.sampleCount === b.previewRun.sampleCount &&
        a.previewRun.startedAtMs === b.previewRun.startedAtMs;
  return (
    a.version === b.version &&
    a.usecaseId === b.usecaseId &&
    a.activeStepIndex === b.activeStepIndex &&
    a.labelingBackend === b.labelingBackend &&
    a.jobName === b.jobName &&
    a.description === b.description &&
    a.datasetS3Uri === b.datasetS3Uri &&
    a.maskPrefix === b.maskPrefix &&
    a.taskTypeValue === b.taskTypeValue &&
    a.workforceTypeValue === b.workforceTypeValue &&
    a.labelCategories === b.labelCategories &&
    a.gtInstructions === b.gtInstructions &&
    a.enableAutomatedLabeling === b.enableAutomatedLabeling &&
    stringArraysEqual(a.ddaLabels, b.ddaLabels) &&
    a.ddaInstructions === b.ddaInstructions &&
    teamsEqual &&
    a.autoLabelEnabled === b.autoLabelEnabled &&
    a.autoLabelModel === b.autoLabelModel &&
    a.detectionPrompt === b.detectionPrompt &&
    a.fewShotEnabled === b.fewShotEnabled &&
    a.downscaleMaxEdge === b.downscaleMaxEdge &&
    a.tokenBudget === b.tokenBudget &&
    a.skipVerification === b.skipVerification &&
    a.skipVerificationModelId === b.skipVerificationModelId &&
    stringRecordsEqual(a.perLabelPrompts, b.perLabelPrompts) &&
    stringArraysEqual(a.exampleRefs.good, b.exampleRefs.good) &&
    stringArraysEqual(a.exampleRefs.bad, b.exampleRefs.bad) &&
    stringArraysEqual(a.previewSelectedKeys, b.previewSelectedKeys) &&
    previewRunsEqual
  );
}

/**
 * Merged_Example_Refs: per designation, the restored refs in draft order
 * followed by the newly uploaded refs in upload order — the ref set both
 * the preview request and the job submission consume (Requirements 2.3,
 * 4.3). Pure: inputs are not mutated.
 */
export function mergedExampleRefs(
  restored: { good: string[]; bad: string[] },
  uploaded: { good: string[]; bad: string[] }
): { good: string[]; bad: string[] } {
  return {
    good: [...restored.good, ...uploaded.good],
    bad: [...restored.bad, ...uploaded.bad],
  };
}

/**
 * Display name of a Restored_Example_Reference: the ref's basename — the
 * segment after the last `/` — falling back to the whole ref when the
 * basename is empty (Requirement 4.1).
 */
export function exampleRefDisplayName(ref: string): string {
  const basename = ref.slice(ref.lastIndexOf('/') + 1);
  return basename !== '' ? basename : ref;
}
