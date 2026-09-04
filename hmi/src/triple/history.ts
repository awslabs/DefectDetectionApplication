/**
 * Run history logic for the Triple_HMI (Requirements 7.1, 7.2, 7.6, 7.8).
 *
 * Pure module over parsed `Execution` records plus each run's per-Inspection
 * slot verdicts. Three responsibilities:
 *
 *   1. `runVerdictState` — the single run-level verdict state shown on a
 *      history tile, derived by the Requirement 7.1 precedence:
 *      `failed-run` > `fail` > `no-verdict` > `pass`.
 *   2. `buildHistory` — the initial population of the history strip from the
 *      runs the LocalServer already knows about, newest first, capped at the
 *      display capacity and containing only runs that exist (7.6, 7.8).
 *   3. `insertHistoryEntry` — the incremental update when a new run reaches a
 *      terminal status, preserving newest-first order and the capacity bound
 *      by evicting exactly the oldest entry on overflow (7.2).
 *
 * Ordering reuses the Live_View's terminal-run recency comparator from
 * `logic/runs.ts` unchanged, so a history tile and the displayed run can
 * never disagree about which run is the most recent.
 *
 * No DOM, no network, no timers — the whole module is directly
 * property-testable.
 */

import type { Execution } from "../api/types";
import { compareTerminalRunsDesc, isTerminal } from "../logic/runs";
import { SLOT_COUNT } from "./inspections";

// --------------------------------------------------------------------------
// Capacity
// --------------------------------------------------------------------------

/**
 * Number of history entries retained (and renderable) at once.
 *
 * Requirement 7.1 sets a floor of the 5 most recent runs; 10 is used because
 * the executions poll already asks for `limit=10`, so the strip can be filled
 * from a single payload with no extra requests.
 */
export const HISTORY_CAPACITY = 10;

/** The Requirement 7.1 floor, kept explicit so the ≥ relation is checkable. */
export const MIN_VISIBLE_HISTORY = 5;

// --------------------------------------------------------------------------
// View models
// --------------------------------------------------------------------------

/** Per-Inspection verdict states (`triple/verdicts.ts` `SlotVerdict.state`). */
export type SlotVerdictState = "pass" | "fail" | "no-verdict";

/**
 * The only part of a slot's verdict this module reads.
 *
 * Structural on purpose: `triple/verdicts.ts` produces richer values
 * (`{ state: "pass" | "fail"; confidenceText?: string }` and
 * `{ state: "no-verdict" }`), all of which satisfy this shape, so the
 * precedence logic stays independent of how verdicts are derived or rendered.
 */
export interface SlotVerdictLike {
  state: SlotVerdictState;
}

/**
 * The only part of an Inspection_Slot this module reads: its verdict, if any.
 * `InspectionSlotVM` from `triple/verdicts.ts` is assignable to this.
 *
 * An absent `verdict` and an explicit `{ state: "no-verdict" }` are treated
 * identically — both mean the slot has no boolean verdict (Requirement 7.1).
 */
export interface VerdictBearingSlot {
  verdict?: SlotVerdictLike | undefined;
}

/** Run-level verdict state of a history tile (design `RunVerdictState`). */
export type RunVerdictState = "pass" | "fail" | "failed-run" | "no-verdict";

/** One entry of the history strip (design `HistoryEntry`). */
export interface HistoryEntry {
  executionId: string;
  verdict: RunVerdictState;
  /** Epoch seconds — the run's start time, displayed on the tile (7.1). */
  startedAt: number;
}

/**
 * Per-run slot lookup accepted by `buildHistory`: a `Map`, a plain record, or
 * a function. Whatever the caller already holds (the reducer keeps a per-run
 * cache) can be passed without reshaping it.
 */
export type SlotsByExecution =
  | ReadonlyMap<string, readonly VerdictBearingSlot[]>
  | Readonly<Record<string, readonly VerdictBearingSlot[] | undefined>>
  | ((executionId: string) => readonly VerdictBearingSlot[] | undefined);

// --------------------------------------------------------------------------
// Verdict precedence (Requirement 7.1)
// --------------------------------------------------------------------------

/**
 * The run-level verdict state for a history tile (Requirement 7.1).
 *
 * Precedence, evaluated strictly in order:
 *   1. `failed-run` — the run reached the `failed` terminal status. The run's
 *      own outcome dominates: whatever per-Inspection data happens to exist
 *      for a failed run never changes the tile.
 *   2. `fail` — at least one Inspection has a failing verdict.
 *   3. `no-verdict` — at least one of the three Inspections lacks a boolean
 *      verdict (its verdict is absent, or explicitly `no-verdict`). Slots
 *      missing from `slots` altogether count as lacking a verdict, so a run
 *      whose inventory yielded fewer than three Inspections is `no-verdict`
 *      rather than `pass`.
 *   4. `pass` — all three Inspections have passing verdicts.
 *
 * Pure and total: never throws, and depends only on the run's status and the
 * verdict states of its slots.
 */
export function runVerdictState(
  execution: Execution,
  slots: readonly VerdictBearingSlot[],
): RunVerdictState {
  if (execution.status === "failed") return "failed-run";

  const states = slots.map((slot) => slot.verdict?.state);
  if (states.includes("fail")) return "fail";

  // Fewer than three slots → at least one of the three lacks a verdict.
  if (slots.length < SLOT_COUNT) return "no-verdict";
  if (states.some((state) => state !== "pass" && state !== "fail")) {
    return "no-verdict";
  }

  return "pass";
}

// --------------------------------------------------------------------------
// History construction (Requirements 7.2, 7.6, 7.8)
// --------------------------------------------------------------------------

function resolveSlots(
  source: SlotsByExecution,
  executionId: string,
): readonly VerdictBearingSlot[] {
  if (typeof source === "function") return source(executionId) ?? [];
  if (source instanceof Map) return source.get(executionId) ?? [];
  const record = source as Readonly<
    Record<string, readonly VerdictBearingSlot[] | undefined>
  >;
  return record[executionId] ?? [];
}

/** The history entry for one run, given its slots. */
export function toHistoryEntry(
  execution: Execution,
  slots: readonly VerdictBearingSlot[],
): HistoryEntry {
  return {
    executionId: execution.executionId,
    verdict: runVerdictState(execution, slots),
    startedAt: execution.startedAt,
  };
}

/**
 * Builds the history strip from the runs the LocalServer reports
 * (Requirements 7.1, 7.6, 7.8).
 *
 * Only terminal runs (`completed`/`failed`) become entries — a verdict state
 * exists only once a run has an outcome, and Requirement 7.2 adds a run at
 * the moment it reaches a terminal status. Entries are ordered newest first
 * by the same comparator the Live_View uses for the displayed run, deduplicated
 * by `executionId` (the newest occurrence wins), and truncated to
 * `HISTORY_CAPACITY`, so the result contains only runs that exist in the input
 * and an empty input yields an empty strip (the zero-history message state).
 */
export function buildHistory(
  executions: readonly Execution[],
  slotsByExecution: SlotsByExecution,
): HistoryEntry[] {
  const ordered = executions
    .filter(isTerminal)
    // `Array.prototype.sort` is stable, and the comparator returns 0 for runs
    // it cannot distinguish, so ties keep the backend's payload order.
    .sort(compareTerminalRunsDesc);

  const entries: HistoryEntry[] = [];
  const seen = new Set<string>();
  for (const execution of ordered) {
    if (seen.has(execution.executionId)) continue;
    seen.add(execution.executionId);
    entries.push(
      toHistoryEntry(execution, resolveSlots(slotsByExecution, execution.executionId)),
    );
    if (entries.length === HISTORY_CAPACITY) break;
  }
  return entries;
}

/**
 * Inserts one run into the history strip (Requirement 7.2).
 *
 * The entry is placed at its ordering position by descending `startedAt` —
 * for a run that has just reached a terminal status that is the newest
 * position, and for a late-arriving older run it is the position that keeps
 * the strip ordered newest first. Ties place the incoming entry first, so a
 * new run with the same start time as the current head still shows up newest.
 *
 * Re-inserting a run already in the strip (same `executionId`) replaces it
 * rather than duplicating it, which is what makes the poller's "insert on
 * every observed terminal run" loop idempotent.
 *
 * On overflow the combined list is truncated to `HISTORY_CAPACITY`, evicting
 * exactly its oldest entry — the incoming one when it is itself older than
 * everything retained.
 *
 * Returns a new array; the input is never mutated.
 */
export function insertHistoryEntry(
  history: readonly HistoryEntry[],
  entry: HistoryEntry,
): HistoryEntry[] {
  const withoutEntry = history.filter(
    (existing) => existing.executionId !== entry.executionId,
  );

  const insertAt = withoutEntry.findIndex(
    (existing) => entry.startedAt >= existing.startedAt,
  );

  const next =
    insertAt === -1
      ? [...withoutEntry, entry]
      : [...withoutEntry.slice(0, insertAt), entry, ...withoutEntry.slice(insertAt)];

  return next.slice(0, HISTORY_CAPACITY);
}
