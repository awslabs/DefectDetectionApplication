/**
 * Verdict derivation for the Triple_HMI (Requirements 5.5, 5.6, 5.7, 5.9,
 * 5.10, 5.11, 5.12).
 *
 * Pure module (no DOM, no network): the caller supplies the displayed run's
 * status and error text, the run's metadata payload, and the Inspections
 * derived from its results inventory (`triple/inspections.ts`), and this
 * module layers verdict content onto the three Inspection_Slots and the run
 * level.
 *
 * Two verdict sources, kept strictly apart (Requirements 5.6, 5.11):
 *
 *   - **Per-Inspection** verdicts come *only* from the nested
 *     `bedrock.{nodeId}` metadata keys the executor writes per Bedrock branch.
 *     `is_anomalous === true` → FAIL, `=== false` → PASS, absent or
 *     non-boolean → NO VERDICT — decided independently for each slot, so one
 *     slot's missing data never suppresses another slot's verdict
 *     (Requirements 5.5, 5.12).
 *   - **Run-level** verdicts come *only* from the flat `is_anomalous` /
 *     `confidence` fields, and render once at the run level. They are never
 *     copied into the slots, and when both sources exist both render in their
 *     own positions with values traceable to their own fields (5.6, 5.11).
 *
 * Node-id matching tolerates the raw-vs-sanitized difference between the two
 * worlds: metadata keys are the workflow's raw binding node ids, while an
 * Inspection's `nodeId` comes from an artifact filename, where the executor
 * has replaced filename-unsafe characters. Both sides are compared in
 * sanitized form (see `sanitizeNodeId`). For the target workflow's ids
 * (`bedrock_1`-shaped) the two forms coincide.
 *
 * States are presented as an icon plus a distinct word (✔ PASS / ✘ FAIL /
 * — NO VERDICT / ⚠ ERROR), so no state is distinguished by color alone
 * (Requirement 5.5); the mapping lives here as data (`VERDICT_PRESENTATION`)
 * so the renderer stays free of verdict semantics.
 */

import type { ExecutionStatus, RunMetadata } from "../api/types";
import type { Inspection, InspectionSlot, SlotNumber } from "./inspections";
import { assignSlots } from "./inspections";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/** Metadata key holding the per-Inspection verdict records. */
export const BEDROCK_METADATA_KEY = "bedrock";

/** Shown for a failed run whose `error` is empty or absent (R5.9). */
export const NO_ERROR_DETAILS_MESSAGE =
  "No error details are available for this run.";

/**
 * Filename-unsafe characters in a node id — the same class the executor's
 * `_UNSAFE_NODE_ID_CHARS` (`[^A-Za-z0-9_.-]`) replaces with `_` when it
 * writes node-frame artifacts. Matching in this form makes the metadata's raw
 * node ids and the inventory's sanitized ones comparable.
 */
const UNSAFE_NODE_ID_CHARS = /[^A-Za-z0-9_.-]/g;

/** The executor's fallback for a node id that sanitizes to nothing. */
const EMPTY_NODE_ID_REPLACEMENT = "node";

// --------------------------------------------------------------------------
// View models
// --------------------------------------------------------------------------

/** Per-Inspection verdict (design `SlotVerdict`). */
export type SlotVerdict =
  | { state: "pass" | "fail"; confidenceText?: string }
  | { state: "no-verdict" };

/** Run-level verdict from the flat metadata fields (design `RunResultVM`). */
export interface RunLevelVerdict {
  state: "pass" | "fail";
  /** `confidence` rounded to exactly 2 decimal places (R5.7). */
  confidenceText?: string;
}

/**
 * One Inspection_Slot with its verdict content (design `InspectionSlotVM`).
 *
 * An absent `inspection` is the no-inspection-data placeholder (R4.6); an
 * absent `verdict` means the slot renders no verdict content at all — either
 * because the slot holds no Inspection, or because the run's metadata carries
 * no per-Inspection verdict data for any displayed Inspection (R5.6, R5.10).
 *
 * Structurally assignable to `history.ts`'s `VerdictBearingSlot`, which
 * treats an absent verdict and an explicit `no-verdict` identically.
 */
export interface InspectionSlotVM extends InspectionSlot {
  verdict?: SlotVerdict;
}

/** The three slots in fixed order. */
export type InspectionSlotVMTriple = [
  InspectionSlotVM,
  InspectionSlotVM,
  InspectionSlotVM,
];

/**
 * The verdict-bearing fields of a `RunResultVM` (design "Triple view
 * models"). The fetch-failure flags `resultsUnavailable` and the `execution`
 * itself are assembled by the caller, which owns the requests.
 */
export interface VerdictDerivation {
  slots: InspectionSlotVMTriple;
  /** Present iff the flat `is_anomalous` is boolean (R5.6, R5.11). */
  runLevelVerdict?: RunLevelVerdict;
  /** Present iff the run failed (R5.9). */
  failedRun?: { errorSummary: string };
  /** True iff the inventory yielded more than three Inspections (R4.7). */
  moreInspections: boolean;
  /** True iff the metadata fetch failed after its single retry (R4.8). */
  metadataUnavailable: boolean;
}

/**
 * The run fields this module reads: its status and, for failed runs, its
 * error text. A parsed `Execution` is assignable, and the design's
 * `deriveVerdicts(status, ...)` shorthand is this object's `status`.
 */
export interface VerdictRunSource {
  status: ExecutionStatus;
  error?: string | null;
}

/** Metadata payload shape accepted here: parsed, raw, or missing. */
export type VerdictMetadata =
  | RunMetadata
  | Readonly<Record<string, unknown>>
  | null
  | undefined;

// --------------------------------------------------------------------------
// Presentation (icon + distinct word, never color alone — R5.5)
// --------------------------------------------------------------------------

/** Every verdict state rendered as text by the Triple_HMI. */
export type VerdictState = "pass" | "fail" | "no-verdict" | "failed-run";

/** Icon + word pairs; the word alone identifies the state unambiguously. */
export const VERDICT_PRESENTATION: Readonly<
  Record<VerdictState, { icon: string; word: string }>
> = {
  pass: { icon: "✔", word: "PASS" },
  fail: { icon: "✘", word: "FAIL" },
  "no-verdict": { icon: "—", word: "NO VERDICT" },
  "failed-run": { icon: "⚠", word: "ERROR" },
};

/** The rendered text of a verdict state, e.g. `"✔ PASS"` (R5.5, R6.4). */
export function verdictLabel(state: VerdictState): string {
  const presentation = VERDICT_PRESENTATION[state];
  return `${presentation.icon} ${presentation.word}`;
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/**
 * The filename-safe form of a node id, mirroring the executor's
 * `_UNSAFE_NODE_ID_CHARS` discipline exactly (unsafe characters replaced by
 * `_`, an empty result becoming `node`). Used only to compare a metadata key
 * with an Inspection's `nodeId`; neither value is rewritten for display.
 */
export function sanitizeNodeId(nodeId: string): string {
  return nodeId.replace(UNSAFE_NODE_ID_CHARS, "_") || EMPTY_NODE_ID_REPLACEMENT;
}

/**
 * Renders a confidence value rounded to **exactly** 2 decimal places
 * (Requirement 5.7) — trailing zeros retained, so the kiosk's fixed-width
 * verdict line never jitters. Non-finite values yield `undefined`, which
 * omits the confidence rather than rendering `NaN`.
 */
export function formatConfidence(confidence: unknown): string | undefined {
  return typeof confidence === "number" && Number.isFinite(confidence)
    ? confidence.toFixed(2)
    : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

/**
 * The `bedrock.{nodeId}` record for an Inspection, matched in sanitized form.
 *
 * An exact key match wins; otherwise the lexicographically smallest key whose
 * sanitized form matches is used, so the lookup is deterministic and
 * independent of metadata key order even when several raw ids sanitize to the
 * same value. Returns `undefined` when no key matches or the matched value is
 * not an object.
 */
function findNodeVerdictRecord(
  bedrock: Record<string, unknown>,
  nodeId: string,
): Record<string, unknown> | undefined {
  const direct = Object.prototype.hasOwnProperty.call(bedrock, nodeId)
    ? asRecord(bedrock[nodeId])
    : undefined;
  if (direct !== undefined) return direct;

  const target = sanitizeNodeId(nodeId);
  const matches = Object.keys(bedrock)
    .filter((key) => sanitizeNodeId(key) === target)
    .sort();

  const key = matches[0];
  return key === undefined ? undefined : asRecord(bedrock[key]);
}

/** The verdict of one slot from its own `bedrock` record (R5.5, 5.7, 5.12). */
function toSlotVerdict(record: Record<string, unknown> | undefined): SlotVerdict {
  const isAnomalous = record?.is_anomalous;
  // Absent or non-boolean → NO VERDICT for this slot only (R5.12).
  if (typeof isAnomalous !== "boolean") return { state: "no-verdict" };

  const verdict: SlotVerdict = { state: isAnomalous ? "fail" : "pass" };
  const confidenceText = formatConfidence(record?.confidence);
  // Confidence accompanies a displayed verdict only (R5.7).
  if (confidenceText !== undefined) verdict.confidenceText = confidenceText;
  return verdict;
}

/** The failed-run summary: the run's error text, else the fallback (R5.9). */
function summarizeError(run: VerdictRunSource): string {
  const error = typeof run.error === "string" ? run.error.trim() : "";
  return error === "" ? NO_ERROR_DETAILS_MESSAGE : error;
}

function emptySlot(slotNumber: SlotNumber): InspectionSlotVM {
  return { slotNumber };
}

// --------------------------------------------------------------------------
// Derivation
// --------------------------------------------------------------------------

/**
 * Derives the verdict-bearing view model of a displayed run (Requirements
 * 5.5, 5.6, 5.7, 5.9, 5.10, 5.11, 5.12).
 *
 * @param run The displayed run — only its `status` and `error` are read.
 * @param metadata The run's metadata payload, or `null`/`undefined` when the
 *   metadata request failed after its single retry (Requirement 4.8), which
 *   sets `metadataUnavailable` and leaves every verdict position empty. An
 *   empty object `{}` is a *successful* verdict-less payload (R5.10).
 * @param inspections The run's Inspections in derivation order
 *   (`deriveInspections`); slot assignment and the more-inspections indicator
 *   are delegated to `assignSlots`, so slot identity keeps its single source.
 *
 * Behavior:
 *
 * - **Failed run** (`status === "failed"`): the run-level failure state with
 *   the error summary, placeholders in all three slots, and no image
 *   reference from any run — the Inspections are deliberately not assigned,
 *   so nothing from a prior run can leak into a slot (R5.9).
 * - **Per-Inspection verdicts**: rendered when the metadata carries a
 *   `bedrock` record for at least one displayed Inspection. Each occupied
 *   slot then resolves independently to PASS, FAIL, or NO VERDICT (R5.5,
 *   5.12).
 * - **No per-Inspection verdict data at all**: slots carry no verdict
 *   content, so a run-level verdict renders once at the run level and is
 *   never duplicated into the slots (R5.6), and a completed run whose
 *   metadata lacks every verdict field yields images + status with no verdict
 *   content and no error state (R5.10).
 *
 * Pure and total: never throws, whatever shape the metadata payload has.
 */
export function deriveVerdicts(
  run: VerdictRunSource,
  metadata: VerdictMetadata,
  inspections: readonly Inspection[],
): VerdictDerivation {
  const metadataRecord = metadata === null ? undefined : asRecord(metadata);
  const metadataUnavailable = metadata === null || metadata === undefined;

  if (run.status === "failed") {
    // The run's own failure dominates: no images, no verdicts, no
    // more-inspections indicator — just the failure state (R5.9).
    return {
      slots: [emptySlot(1), emptySlot(2), emptySlot(3)],
      failedRun: { errorSummary: summarizeError(run) },
      moreInspections: false,
      metadataUnavailable,
    };
  }

  const { slots: baseSlots, moreInspections } = assignSlots(inspections);
  const bedrock = asRecord(metadataRecord?.[BEDROCK_METADATA_KEY]) ?? {};

  // Each displayed Inspection's own record, resolved once per slot.
  const records = baseSlots.map((slot) =>
    slot.inspection === undefined
      ? undefined
      : findNodeVerdictRecord(bedrock, slot.inspection.nodeId),
  );
  const hasPerInspectionData = records.some((record) => record !== undefined);

  const slots = baseSlots.map((slot, index) => {
    const vm: InspectionSlotVM = { slotNumber: slot.slotNumber };
    if (slot.inspection !== undefined) vm.inspection = slot.inspection;
    // Verdict content only where per-Inspection data exists for this run;
    // otherwise the slots stay verdict-free (R5.6, R5.10).
    if (slot.inspection !== undefined && hasPerInspectionData) {
      vm.verdict = toSlotVerdict(records[index]);
    }
    return vm;
  }) as InspectionSlotVMTriple;

  const derivation: VerdictDerivation = {
    slots,
    moreInspections,
    metadataUnavailable,
  };

  // Run level reads the flat fields only — never a nested bedrock value
  // (R5.6, R5.11).
  const flatIsAnomalous = metadataRecord?.is_anomalous;
  if (typeof flatIsAnomalous === "boolean") {
    const runLevelVerdict: RunLevelVerdict = {
      state: flatIsAnomalous ? "fail" : "pass",
    };
    const confidenceText = formatConfidence(metadataRecord?.confidence);
    if (confidenceText !== undefined) {
      runLevelVerdict.confidenceText = confidenceText;
    }
    derivation.runLevelVerdict = runLevelVerdict;
  }

  return derivation;
}
