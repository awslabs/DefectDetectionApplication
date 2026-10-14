/**
 * Run ordering logic (Requirements 3.2, 3.4, 3.7).
 *
 * Pure functions over parsed `Execution` records:
 *   - a terminal-run recency comparator implementing the Requirement 3.4
 *     ordering (`finishedAt` descending, `startedAt` as the ordering key when
 *     `finishedAt` values are equal or absent),
 *   - latest-terminal-run selection used by the Live_View to pick the run to
 *     display (3.2, 3.7),
 *   - in-progress detection (`pending` / `running`) for the header indicator
 *     (Requirement 3.3 consumers).
 *
 * No DOM, no I/O — directly property-testable.
 */

import type { Execution, ExecutionStatus } from "../api/types";

const TERMINAL_STATUSES: readonly ExecutionStatus[] = ["completed", "failed"];
const IN_PROGRESS_STATUSES: readonly ExecutionStatus[] = ["pending", "running"];

/** True iff the execution has reached a terminal status (completed/failed). */
export function isTerminal(execution: Execution): boolean {
  return TERMINAL_STATUSES.includes(execution.status);
}

/** True iff the execution is in progress (pending/running). */
export function isInProgress(execution: Execution): boolean {
  return IN_PROGRESS_STATUSES.includes(execution.status);
}

/**
 * Recency comparator for terminal runs, descending (most recent first).
 *
 * Requirement 3.4 ordering: the more recent run is the one with the larger
 * `finishedAt`; when the two `finishedAt` values are equal or either is
 * absent (null), `startedAt` is the ordering key instead.
 *
 * Returns a negative number when `a` is more recent than `b`, positive when
 * `b` is more recent, and 0 when the ordering cannot distinguish them (equal
 * keys). Suitable for `Array.prototype.sort` to produce newest-first lists.
 */
export function compareTerminalRunsDesc(a: Execution, b: Execution): number {
  if (
    a.finishedAt !== null &&
    b.finishedAt !== null &&
    a.finishedAt !== b.finishedAt
  ) {
    return b.finishedAt - a.finishedAt;
  }
  return b.startedAt - a.startedAt;
}

/**
 * Selects the most recent terminal (`completed`/`failed`) run from a list of
 * executions, per the Requirement 3.4 ordering — the run the Live_View
 * displays (3.2, 3.7). Returns null when no terminal run exists.
 *
 * Ties under the ordering resolve to the earliest list entry; the additive
 * executions route returns `started_at` DESC / `id` DESC, so this matches
 * the backend's deterministic tiebreak.
 */
export function latestTerminalRun(executions: Execution[]): Execution | null {
  let latest: Execution | null = null;
  for (const execution of executions) {
    if (!isTerminal(execution)) continue;
    if (latest === null || compareTerminalRunsDesc(execution, latest) < 0) {
      latest = execution;
    }
  }
  return latest;
}

/**
 * True iff any execution in the list is in progress (`pending`/`running`) —
 * drives the in-progress indicator without touching the displayed Run_Result.
 */
export function hasInProgressRun(executions: Execution[]): boolean {
  return executions.some(isInProgress);
}
