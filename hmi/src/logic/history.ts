/**
 * History strip logic (Requirements 7.1, 7.2, 7.6, 7.7).
 *
 * Pure functions over parsed `Execution` records that maintain the run
 * history summary displayed in the strip:
 *   - `buildHistory` populates the initial list from a registration's
 *     existing runs, newest first, up to the display capacity (7.1, 7.6);
 *     fewer-than-capacity lists keep only the runs that exist, and zero runs
 *     yield an empty list the renderer shows as "no run history" (7.7),
 *   - `insertTerminalRun` adds a newly terminal run at the newest position
 *     and evicts exactly the oldest entry on overflow (7.2).
 *
 * Each entry carries the run's verdict state and start time (7.1). Verdict
 * states are supplied by the caller through a resolver so this module stays
 * metadata-agnostic: failed runs are `failed-run` from status alone, while
 * completed runs derive pass/fail/no-verdict from their metadata
 * (`logic/verdict.ts`).
 *
 * No DOM, no I/O — directly property-testable.
 */

import type { Execution } from "../api/types";
import { compareTerminalRunsDesc, isTerminal } from "./runs";
import type { VerdictState } from "./verdict";

/**
 * Display capacity of the history strip: 10 entries, satisfying the
 * "at least the 5 most recent runs" bound of Requirement 7.1 with headroom
 * (matches the poll payload bound of the recent-executions route).
 */
export const HISTORY_CAPACITY = 10;

/** One history strip tile: verdict state + start time (Requirement 7.1). */
export interface HistoryEntry {
  executionId: string;
  verdict: VerdictState;
  /** Epoch seconds. */
  startedAt: number;
}

/**
 * Maps a terminal run to its verdict state. Callers back this with
 * `deriveVerdict` over the run's cached metadata (`failed-run` for failed
 * runs, pass/fail/no-verdict for completed runs).
 */
export type VerdictResolver = (execution: Execution) => VerdictState;

function toHistoryEntry(execution: Execution, verdictFor: VerdictResolver): HistoryEntry {
  return {
    executionId: execution.executionId,
    verdict: verdictFor(execution),
    startedAt: execution.startedAt,
  };
}

/**
 * Builds the initial history from a registration's existing runs (7.6):
 * terminal (`completed`/`failed`) runs only, ordered newest first per the
 * Requirement 3.4 recency ordering, capped at `HISTORY_CAPACITY`.
 *
 * With fewer terminal runs than the capacity, entries exist only for the
 * runs that do; with zero, the result is empty and the renderer shows the
 * no-run-history message in the strip area (7.7).
 */
export function buildHistory(
  executions: Execution[],
  verdictFor: VerdictResolver,
): HistoryEntry[] {
  return executions
    .filter(isTerminal)
    .sort(compareTerminalRunsDesc)
    .slice(0, HISTORY_CAPACITY)
    .map((execution) => toHistoryEntry(execution, verdictFor));
}

/**
 * Inserts a run that just reached a terminal status at the newest (first)
 * position of the history (7.2). When the insertion exceeds the capacity,
 * exactly the oldest (last) entry is evicted.
 *
 * Non-terminal runs never enter the history, and a run already present
 * leaves the history unchanged — poll payloads repeat the same terminal
 * runs every cycle, so insertion must be idempotent per executionId.
 *
 * Returns a new array; the input is never mutated.
 */
export function insertTerminalRun(
  history: HistoryEntry[],
  execution: Execution,
  verdictFor: VerdictResolver,
): HistoryEntry[] {
  if (!isTerminal(execution)) return history;
  if (history.some((entry) => entry.executionId === execution.executionId)) {
    return history;
  }
  return [toHistoryEntry(execution, verdictFor), ...history].slice(0, HISTORY_CAPACITY);
}
