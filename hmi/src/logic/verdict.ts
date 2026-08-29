/**
 * Verdict derivation: run metadata + execution status → `VerdictViewModel`.
 *
 * Pure module (no DOM). All display behavior derives from API fields alone
 * (Requirement 2.6). Covers:
 *   - pass/fail mapping from `is_anomalous` (Requirement 4.2)
 *   - confidence rounded to at most 2 decimal places (Requirement 4.3)
 *   - `generated_text` truncated at 500 characters with a truncation flag
 *     (Requirement 4.4)
 *   - failed-run state with an error summary from the run's error fields, or
 *     a no-details message when they are empty/absent (Requirements 4.5, 4.8)
 *   - no-verdict state for completed runs whose metadata lacks all verdict
 *     fields (Requirement 4.7)
 *   - metadata-unavailable indication when the metadata fetch failed after
 *     its single retry (Requirement 4.9)
 */

import type { Execution, RunMetadata } from "../api/types";

export type VerdictState = "pass" | "fail" | "failed-run" | "no-verdict";

export interface VerdictViewModel {
  state: VerdictState;
  /** Confidence rendered with at most 2 decimal places (Requirement 4.3). */
  confidenceText?: string;
  /** First ≤ 500 characters of `generated_text` (Requirement 4.4). */
  generatedText?: string;
  /** True iff the original `generated_text` exceeded 500 characters. */
  generatedTextTruncated: boolean;
  /** Failed runs only: error summary or the no-details message. */
  errorSummary?: string;
  /** True iff the metadata fetch failed after its single retry (4.9). */
  metadataUnavailable: boolean;
}

/** Maximum number of `generated_text` characters displayed (Requirement 4.4). */
export const GENERATED_TEXT_LIMIT = 500;

/** Shown for failed runs whose error fields are empty/absent (Requirement 4.8). */
export const NO_ERROR_DETAILS_MESSAGE = "No error details are available for this run.";

/**
 * Renders a confidence value with at most 2 decimal places (Requirement 4.3).
 * Trailing zeros are dropped ("0.90" → "0.9", "1.00" → "1") so the display
 * never suggests more precision than the rounded value carries.
 */
export function formatConfidence(confidence: number): string {
  return String(Number(confidence.toFixed(2)));
}

/**
 * Derives the verdict view-model for a run.
 *
 * @param execution The run (terminal: `completed` or `failed`). Non-terminal
 *   statuses are treated like `completed`: callers only derive verdicts for
 *   runs they display, which are terminal by Requirement 3.2.
 * @param metadata The parsed metadata payload for the run, or `null` when the
 *   metadata request failed after its single retry (Requirement 4.9). Ignored
 *   for failed runs, whose verdict comes from status alone.
 */
export function deriveVerdict(
  execution: Execution,
  metadata: RunMetadata | null,
): VerdictViewModel {
  if (execution.status === "failed") {
    return {
      state: "failed-run",
      errorSummary: summarizeError(execution),
      generatedTextTruncated: false,
      metadataUnavailable: false,
    };
  }

  if (metadata === null) {
    // Metadata fetch failed after the single retry: show the run's images and
    // status with an indication that verdict data is unavailable (4.9).
    return {
      state: "no-verdict",
      generatedTextTruncated: false,
      metadataUnavailable: true,
    };
  }

  const model: VerdictViewModel = {
    state: "no-verdict",
    generatedTextTruncated: false,
    metadataUnavailable: false,
  };

  if (typeof metadata.is_anomalous === "boolean") {
    model.state = metadata.is_anomalous ? "fail" : "pass"; // 4.2
  }

  if (typeof metadata.confidence === "number" && Number.isFinite(metadata.confidence)) {
    model.confidenceText = formatConfidence(metadata.confidence); // 4.3
  }

  if (typeof metadata.generated_text === "string") {
    const text = metadata.generated_text;
    model.generatedText = text.slice(0, GENERATED_TEXT_LIMIT); // 4.4
    model.generatedTextTruncated = text.length > GENERATED_TEXT_LIMIT;
  }

  // A completed run lacking all of is_anomalous / confidence / generated_text
  // keeps state "no-verdict" with no fields set: images and status render
  // without a verdict panel rather than an error (4.7).
  return model;
}

/**
 * Builds the failed-run error summary from the run's error fields
 * (Requirement 4.5), falling back to the no-details message when both are
 * empty or absent (Requirement 4.8).
 */
function summarizeError(execution: Execution): string {
  const error = execution.error?.trim() ?? "";
  const failingNodeId = execution.failingNodeId?.trim() ?? "";
  if (error && failingNodeId) return `${error} (node: ${failingNodeId})`;
  if (error) return error;
  if (failingNodeId) return `Failed at node ${failingNodeId}.`;
  return NO_ERROR_DETAILS_MESSAGE;
}
