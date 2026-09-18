/**
 * VLM/LLM Anomaly Tuning API contract (quality-prompt-tuning, task 8.1).
 *
 * The request/response shapes of every `/workflow-tuning/anomaly/**` route
 * served by `functions/workflow_tuning.py`, transcribed from that handler's
 * response builders (`session_summary`, `node_view`, `sample_view`,
 * `candidate_summary`, `run_view`, `outcome_view`, `build_preview`,
 * `label_counts`, `summarize_outcomes`) so the typed client in
 * `services/api.ts` and the pages of tasks 8.2-8.4 agree with the backend.
 *
 * Naming follows the wire, not TypeScript fashion: the handler answers
 * camelCase bodies and takes snake_case query parameters, and both are kept
 * verbatim here.
 */

/** Node types the feature tunes (Requirement 1.5). */
export type TuningNodeType = 'bedrock_inference' | 'llm_inference';

/** Label a Workflow_Author sets on a Tuning_Sample (Requirement 4.2). */
export type TuningLabel = 'OK' | 'NOK' | 'EXCLUDE';

/** Sample_Outcome category (Requirement 6.5). */
export type OutcomeCategory =
  | 'correct'
  | 'false_pass'
  | 'false_fail'
  | 'parse_failure'
  | 'invocation_error';

/** Score_Run lifecycle state. */
export type ScoreRunStatus = 'running' | 'completed' | 'cancelled' | 'failed';

/**
 * Who executes a Score_Run: the Portal's Bedrock_Scorer, or a
 * Device_Score_Job on the device's local vLLM model (Requirement 6.9).
 */
export type ScoreRunMode = 'bedrock' | 'device';

/** Where a Tuning_Sample came from (Requirement 2.7). */
export type TuningSampleSource = 'live' | 'backfill';

/**
 * Score_Summary — a pure function of the persisted Sample_Outcomes, never a
 * running counter (`workflow_core.anomaly_invocation.summarize_outcomes`,
 * Requirements 6.7, 6.14).
 */
export interface ScoreSummary {
  samples: number;
  invocations: number;
  correct: number;
  falsePass: number;
  falseFail: number;
  parseFailure: number;
  invocationError: number;
  /** `correct / invocations`; null when the run has no outcomes yet. */
  accuracy: number | null;
  /** Samples whose repeats disagreed (Requirement 6.14). */
  unstable: number;
  meanOutputTokens: number | null;
  maxOutputTokens: number | null;
  meanLatencyMs: number | null;
}

/** The Prompt_Set under evaluation: the three tunable parameters. */
export interface PromptSet {
  prompt: string;
  systemPrompt: string | null;
  maxTokens: number | null;
}

/** `lastRefresh` on a session: what the last index pass did (Req 3.5). */
export interface RefreshSummary {
  at?: number;
  indexed: number;
  /** Skipped sidecars per reason (`missing_input`, `unreadable`, ...). */
  skipped: Record<string, number>;
  beyondBound: number;
  discovered?: number;
  listingTruncated?: boolean;
  /** Present when the Sample_Store could not be read at all. */
  error?: string | null;
}

/** Sample counts by Label plus the derived counts the UI shows (Req 4.7). */
export interface LabelCounts {
  OK: number;
  NOK: number;
  EXCLUDE: number;
  unlabelled: number;
  synthetic: number;
  duplicates: number;
  total: number;
}

/** The result of the last apply, shown on the node panel (Req 1.4, 8.3). */
export interface TuningResult {
  appliedAt: number;
  appliedBy: string;
  newVersion: number;
  previousVersion?: number;
  candidateId: string;
  candidateName?: string | null;
  scoreRunId?: string | null;
  summary?: ScoreSummary | null;
  baselineSummary?: ScoreSummary | null;
}

/** A Tuning_Session as every route returns it (`session_summary`). */
export interface TuningSession {
  sessionId: string;
  usecaseId: string;
  workflowId: string;
  nodeId: string;
  nodeType: TuningNodeType | string;
  baselineVersion: number | null;
  baselineCandidateId: string | null;
  baselineFingerprint: string | null;
  selectedCandidateId: string | null;
  syntheticNegativesEnabled: boolean;
  lastRefresh: RefreshSummary | null;
  latestTuningResult: TuningResult | null;
  createdBy?: string;
  createdAt?: number;
  updatedAt?: number;
}

/** The Tunable_Node as the session routes describe it (`node_view`). */
export interface TuningNodeView {
  nodeId: string;
  nodeType: TuningNodeType | string;
  model: string | null;
  /** Node_Parameters — parameters only, never a credential (Req 9.3). */
  parameters: Record<string, unknown>;
  promptSet: PromptSet;
  maxTokensBounds: { min: number; max: number | null };
}

/** One Candidate (`candidate_summary`). */
export interface TuningCandidate {
  candidateId: string;
  name: string;
  prompt: string;
  systemPrompt: string | null;
  maxTokens: number | null;
  isBaseline: boolean;
  baselineVersion?: number | null;
  fingerprint?: string | null;
  createdAt?: number;
  updatedAt?: number;
  /** Only on the session view: the Candidate's most recent Score_Run. */
  latestRun?: ScoreRunView | null;
}

/** One Score_Run (`run_view` / `live_run`). */
export interface ScoreRunView {
  runId: string;
  sessionId: string;
  candidateId: string;
  candidateName: string | null;
  status: ScoreRunStatus;
  mode: ScoreRunMode;
  repeats: number;
  plannedInvocations: number;
  plannedSampleCount: number;
  done: number | null;
  cursor: number | null;
  cancelRequested: boolean;
  deviceThingName: string | null;
  jobId: string | null;
  /** The device's `reported.jobs[jobId]` document, for a device run. */
  reportedJob: Record<string, unknown> | null;
  startedAt: number | null;
  startedBy: string | null;
  lastProgressAt: number | null;
  finishedAt: number | null;
  summary: ScoreSummary | null;
  error: string | null;
}

/** An image the device sent, as the sample views carry it. */
export interface TuningSampleImage {
  key?: string;
  sha256?: string;
  bytes?: number;
  /** Presigned GET URL, set on the routes that presign (Req 4.8). */
  url?: string | null;
}

/** One indexed Tuning_Sample (`sample_view`, Requirement 4.1). */
export interface TuningSampleView {
  sampleId: string;
  workflowId: string;
  nodeId: string;
  nodeType: TuningNodeType | string;
  thingName: string;
  executionId: string;
  version: number | string | null;
  exportedAt: number | null;
  source: TuningSampleSource | string | null;
  label: TuningLabel | null;
  duplicateOf: string | null;
  differentPrompt: boolean;
  synthetic: boolean;
  sourceSampleId: string | null;
  siblingNodeId: string | null;
  detectionId: string | null;
  detectionSlot: number | null;
  promptFingerprint: string | null;
  recorded: {
    isAnomalous: boolean | null;
    confidence: number | null;
    answer: string | null;
    parseError: string | null;
  };
  input: TuningSampleImage;
  reference: TuningSampleImage | null;
  singleImage: boolean;
  /** Images expired from the Sample_Store (Requirement 3.7). */
  unavailable?: boolean;
  /** Only with `include_metadata`: the `llm_inference` render context. */
  metadataSnippet?: Record<string, unknown> | null;
}

/** One Sample_Outcome (`outcome_view`, Requirements 7.2, 7.4). */
export interface SampleOutcomeView {
  sampleId: string;
  repeat: number;
  label: TuningLabel | null;
  category: OutcomeCategory | string;
  isAnomalous: boolean | null;
  confidence: number | null;
  rawAnswer: string | null;
  parseError: string | null;
  outputTokens: number | null;
  latencyMs: number | null;
  error: string | null;
  thingName: string | null;
}

// --------------------------------------------------------------------------
// GET /workflow-tuning/anomaly/workflows (Requirements 1.2, 1.6)
// --------------------------------------------------------------------------

export interface TuningOverviewNode {
  nodeId: string;
  nodeType: TuningNodeType | string;
  model: string | null;
  /** Tuning_Samples in the Sample_Store for this node (Req 1.2). */
  sampleCount: number;
  /** The existing Tuning_Session's id, when one exists. */
  sessionId: string | null;
}

export interface TuningOverviewWorkflow {
  workflowId: string;
  name: string | null;
  latestVersion: number;
  updatedAt: number | null;
  nodes: TuningOverviewNode[];
}

export interface TuningOverviewResponse {
  usecaseId: string;
  /** False ⇒ the UI explains that devices export nothing (Req 1.6). */
  sampleExportEnabled: boolean;
  sampleRetentionDays: number;
  workflows: TuningOverviewWorkflow[];
  count: number;
  /** Set when the Sample_Store could not be listed (counts read 0). */
  sampleStoreError: string | null;
}

// --------------------------------------------------------------------------
// Sessions
// --------------------------------------------------------------------------

export interface CreateTuningSessionBody {
  workflow_id: string;
  node_id: string;
}

export interface CreateTuningSessionResponse {
  session: TuningSession;
  created: boolean;
  node: TuningNodeView;
  latestVersion: number;
  refresh?: RefreshSummary;
}

export interface TuningSessionResponse {
  session: TuningSession;
  node: TuningNodeView | null;
  latestVersion: number | null;
  /** False when the node is gone or no longer in Anomaly_Mode (Req 8.4). */
  nodeStillTunable: boolean;
  labelCounts: LabelCounts;
  candidates: TuningCandidate[];
  runCount: number;
  sampleExportEnabled: boolean;
}

export interface RefreshTuningSessionResponse {
  session: TuningSession;
  refresh: RefreshSummary;
  labelCounts: LabelCounts;
}

export interface DeleteTuningSessionResponse {
  sessionId: string;
  deleted: { items: number; outcomes: number; objects: number };
  message: string;
}

// --------------------------------------------------------------------------
// Samples, labels, synthetic negatives
// --------------------------------------------------------------------------

/** Query parameters of `GET .../sessions/{id}/samples` (Req 4.4). */
export interface ListTuningSamplesParams {
  label?: TuningLabel | 'UNLABELLED';
  verdict?: 'anomalous' | 'normal';
  device?: string;
  version?: number | string;
  source?: TuningSampleSource | string;
  duplicates?: boolean;
  synthetic?: boolean;
  differentPrompt?: boolean;
  /** Only samples whose Label contradicts the recorded verdict. */
  disagree?: boolean;
  include_metadata?: boolean;
  limit?: number;
  cursor?: string;
}

export interface ListTuningSamplesResponse {
  sessionId: string;
  samples: TuningSampleView[];
  count: number;
  matched: number;
  nextCursor: string | null;
  /** Presigned URL lifetime in seconds (Requirement 4.8). */
  expiresInSeconds: number;
  labelCounts: LabelCounts;
}

export interface SetTuningLabelsBody {
  sampleIds: string[];
  /** null clears the Label (Requirement 4.3). */
  label: TuningLabel | null;
}

export interface SetTuningLabelsResponse {
  sessionId: string;
  label: TuningLabel | null;
  updated: string[];
  missing: string[];
  labelCounts: LabelCounts;
}

export interface SyntheticNegativesResponse {
  sessionId: string;
  enabled: boolean;
  created: number;
  removed: number;
  labelCounts: LabelCounts;
}

// --------------------------------------------------------------------------
// Candidates and preview
// --------------------------------------------------------------------------

export interface TuningCandidateBody {
  name: string;
  prompt: string;
  systemPrompt?: string | null;
  maxTokens?: number | null;
}

export interface TuningCandidateResponse {
  candidate: TuningCandidate;
}

export interface DeleteTuningCandidateResponse {
  candidateId: string;
  runsDeleted: number;
  selectionCleared?: boolean;
}

/** One parser-hostile-settings warning; never blocking (Req 5.4, 5.5). */
export interface CandidatePreviewWarning {
  code: 'max_tokens_truncation' | 'answer_schema_missing_is_anomalous' | string;
  field?: string;
  message: string;
}

/**
 * `GET .../candidates/{cid}/preview` — the exact text the
 * Invocation_Builder will send for this Candidate (Requirement 5.3).
 */
export interface CandidatePreviewResponse {
  candidateId: string;
  sessionId: string;
  nodeType: TuningNodeType | string;
  /** Prompt plus the appended Verdict_Instruction, verbatim. */
  userMessage: string;
  systemText: string | null;
  maxTokens: number | null;
  model: string | null;
  region?: string | null;
  /** False for `llm_inference`: the template is shown unrendered. */
  templateRendered: boolean;
  warnings: CandidatePreviewWarning[];
}

// --------------------------------------------------------------------------
// Score runs
// --------------------------------------------------------------------------

/** Devices that may execute a Device_Score_Job (Requirement 6.9). */
export interface DeviceEligibility {
  exported: string[];
  registered: string[];
  eligible: string[];
  ineligible: string[];
}

export interface StartScoreRunBody {
  candidateId: string;
  /** 1..3 (Requirement 6.7); omitted ⇒ the backend default. */
  repeats?: number;
  /** Required for a device run when more than one device is eligible. */
  deviceThingName?: string;
}

export interface StartScoreRunResponse {
  run: ScoreRunView;
  runId?: string;
  plannedInvocations?: number;
  samples?: number;
  repeats?: number;
  mode?: ScoreRunMode;
  deviceThingName?: string | null;
  deviceEligibility?: DeviceEligibility | null;
  /** Present when dispatch failed: the run is already `failed`. */
  dispatchFailed?: boolean;
}

export interface ScoreRunResponse {
  run: ScoreRunView;
  session: TuningSession;
  candidate: TuningCandidate | null;
}

export interface ListScoreRunOutcomesParams {
  category?: OutcomeCategory;
  sort?: 'confidence';
  order?: 'asc' | 'desc';
  limit?: number;
  cursor?: string;
}

export interface ScoreRunOutcomesResponse {
  runId: string;
  run: ScoreRunView;
  outcomes: SampleOutcomeView[];
  /** The outcomes' samples by id, with presigned images. */
  samples: Record<string, TuningSampleView>;
  count: number;
  matched: number;
  nextCursor: string | null;
  summary: ScoreSummary;
}

export interface CancelScoreRunResponse {
  run: ScoreRunView;
  alreadyFinished?: boolean;
}

/** One sample on which two runs disagreed (Requirement 7.3). */
export interface ScoreRunDiffEntry {
  sampleId: string;
  label: TuningLabel | null;
  a: { categories: string[]; outcomes: SampleOutcomeView[] } | null;
  b: { categories: string[]; outcomes: SampleOutcomeView[] } | null;
}

export interface ScoreRunDiffResponse {
  a: ScoreRunView;
  b: ScoreRunView;
  differing: ScoreRunDiffEntry[];
  count: number;
}

// --------------------------------------------------------------------------
// Selection and apply
// --------------------------------------------------------------------------

export interface SetSelectionBody {
  /** null clears the selection. */
  candidateId: string | null;
}

export interface SetSelectionResponse {
  session: TuningSession;
  selectedCandidateId: string | null;
  latestRun: ScoreRunView | null;
  /** The selection's false passes, shown prominently (Req 7.6). */
  falsePasses: number | null;
}

export interface ApplyCandidateBody {
  /** Optional confirmation that must equal the session's selection. */
  candidateId?: string;
  /** Optional: which completed Score_Run to record (Req 8.2). */
  runId?: string;
}

export interface ApplyCandidateResponse {
  session: TuningSession;
  workflowId: string;
  nodeId: string;
  version: number;
  newVersion: number;
  previousVersion: number;
  promptSet: PromptSet;
  candidate: TuningCandidate;
  run: ScoreRunView;
  tuningResult: TuningResult;
}
