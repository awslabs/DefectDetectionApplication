import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Execution, ExecutionStatus } from "../api/types";
import { SLOT_COUNT } from "./inspections";
import {
  buildHistory,
  HISTORY_CAPACITY,
  insertHistoryEntry,
  MIN_VISIBLE_HISTORY,
  runVerdictState,
  toHistoryEntry,
  type HistoryEntry,
  type RunVerdictState,
  type SlotVerdictState,
  type VerdictBearingSlot,
} from "./history";

/**
 * Property test for the Triple_HMI's run history strip.
 *
 * **Feature: imts-triple-inspection-hmi, Property 14: History invariants and verdict precedence**
 *
 * **Validates: Requirements 7.1, 7.2, 7.6, 7.8**
 *
 * Generators model the station's real input space: one workflow running one
 * run at a time, so runs occupy a non-overlapping timeline (each run starts
 * after the previous one finished) and the payload order the LocalServer
 * happens to return is arbitrary. Slot data is arbitrary per run, including
 * runs with fewer than three slots and slots with no verdict at all.
 */

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function requireDefined<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("expected a defined value");
  return value;
}

const TERMINAL_STATUSES: readonly ExecutionStatus[] = ["completed", "failed"];

function isTerminalStatus(status: ExecutionStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/**
 * Independent recency oracle for "newest first" (Requirement 7.1 / 7.8),
 * expressed directly over the run fields: the more recent run is the one that
 * finished later, and when finish times cannot separate them, the one that
 * started later.
 */
function isAtLeastAsRecent(a: Execution, b: Execution): boolean {
  const aFinished = a.finishedAt;
  const bFinished = b.finishedAt;
  if (aFinished !== null && bFinished !== null && aFinished !== bFinished) {
    return aFinished > bFinished;
  }
  return a.startedAt >= b.startedAt;
}

/**
 * Independent statement of the Requirement 7.1 precedence, written as the
 * requirement reads rather than as the implementation is structured.
 */
function expectedVerdict(
  execution: Execution,
  slots: readonly VerdictBearingSlot[],
): RunVerdictState {
  const states = slots.map((slot) => slot.verdict?.state);
  const failing = states.filter((state) => state === "fail").length;
  const passing = states.filter((state) => state === "pass").length;

  if (execution.status === "failed") return "failed-run";
  if (failing > 0) return "fail";
  if (states.length === SLOT_COUNT && passing === SLOT_COUNT) return "pass";
  return "no-verdict";
}

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

const statusArb = fc.constantFrom<ExecutionStatus>(
  "completed",
  "failed",
  "pending",
  "running",
);

const verdictStateArb = fc.constantFrom<SlotVerdictState>(
  "pass",
  "fail",
  "no-verdict",
);

/** A slot with no verdict key, an explicitly undefined verdict, or a verdict. */
const slotArb: fc.Arbitrary<VerdictBearingSlot> = fc.oneof(
  { arbitrary: fc.constant<VerdictBearingSlot>({}), weight: 1 },
  { arbitrary: fc.constant<VerdictBearingSlot>({ verdict: undefined }), weight: 1 },
  {
    arbitrary: verdictStateArb.map<VerdictBearingSlot>((state) => ({
      verdict: { state },
    })),
    weight: 4,
  },
);

/**
 * Slot lists: usually the three slots `assignSlots` produces, sometimes a
 * short list (a run whose inventory yielded fewer Inspections).
 */
const slotsArb: fc.Arbitrary<VerdictBearingSlot[]> = fc.oneof(
  {
    arbitrary: fc.array(slotArb, { minLength: SLOT_COUNT, maxLength: SLOT_COUNT }),
    weight: 3,
  },
  { arbitrary: fc.array(slotArb, { maxLength: SLOT_COUNT }), weight: 1 },
);

interface RunSeed {
  execution: Execution;
  /** The run's finish time, also used when forcing the run terminal. */
  finishedAtValue: number;
  slots: readonly VerdictBearingSlot[];
  /** Arbitrary position in the LocalServer payload. */
  payloadOrder: number;
}

/**
 * A non-overlapping timeline of runs of one workflow, ids unique, terminal
 * runs carrying a finish time and in-progress runs carrying none.
 *
 * `maxLength` exceeds `HISTORY_CAPACITY` so overflow and eviction are
 * exercised, and short lists cover the fewer-than-capacity and zero-run cases
 * of Requirement 7.6.
 */
const timelineArb: fc.Arbitrary<RunSeed[]> = fc
  .array(
    fc.record({
      status: statusArb,
      gap: fc.integer({ min: 1, max: 120 }),
      duration: fc.integer({ min: 0, max: 120 }),
      slots: slotsArb,
      payloadOrder: fc.nat(),
    }),
    { maxLength: HISTORY_CAPACITY + 6 },
  )
  .map((raw) => {
    let clock = 1_700_000_000;
    return raw.map((entry, index) => {
      const startedAt = clock + entry.gap;
      const finishedAtValue = startedAt + entry.duration;
      clock = finishedAtValue;
      const execution: Execution = {
        executionId: `run-${index}`,
        registrationId: "reg-1",
        status: entry.status,
        startedAt,
        finishedAt: isTerminalStatus(entry.status) ? finishedAtValue : null,
        failingNodeId: null,
        error: null,
        hasImageResults: false,
        captureId: null,
      };
      return {
        execution,
        finishedAtValue,
        slots: entry.slots,
        payloadOrder: entry.payloadOrder,
      };
    });
  });

function inPayloadOrder(seeds: readonly RunSeed[]): RunSeed[] {
  return [...seeds].sort((a, b) => a.payloadOrder - b.payloadOrder);
}

function slotsMapOf(seeds: readonly RunSeed[]): Map<string, readonly VerdictBearingSlot[]> {
  return new Map(seeds.map((seed) => [seed.execution.executionId, seed.slots]));
}

/** The same run forced to a terminal status, for arrival events. */
function asTerminalArrival(seed: RunSeed): Execution {
  return isTerminalStatus(seed.execution.status)
    ? seed.execution
    : { ...seed.execution, status: "completed", finishedAt: seed.finishedAtValue };
}

// --------------------------------------------------------------------------
// Property 14
// --------------------------------------------------------------------------

describe("Property 14: History invariants and verdict precedence", () => {
  it("has a capacity of at least the required visible entries", () => {
    expect(HISTORY_CAPACITY).toBeGreaterThanOrEqual(MIN_VISIBLE_HISTORY);
  });

  it("populates newest first, within capacity, from runs that exist", () => {
    fc.assert(
      fc.property(timelineArb, (seeds) => {
        const executions = inPayloadOrder(seeds).map((seed) => seed.execution);
        const byId = new Map(executions.map((e) => [e.executionId, e]));
        const terminal = executions.filter((e) => isTerminalStatus(e.status));

        const history = buildHistory(executions, slotsMapOf(seeds));

        // Only runs that exist, each at most once (7.6, 7.8).
        const ids = history.map((entry) => entry.executionId);
        expect(new Set(ids).size).toBe(ids.length);
        for (const id of ids) {
          expect(terminal.some((e) => e.executionId === id)).toBe(true);
        }

        // Never exceeds capacity, and holds every run that exists below it.
        expect(history).toHaveLength(Math.min(terminal.length, HISTORY_CAPACITY));

        // Ordered newest first.
        for (let i = 1; i < history.length; i += 1) {
          const previous = requireDefined(byId.get(requireDefined(ids[i - 1])));
          const current = requireDefined(byId.get(requireDefined(ids[i])));
          expect(isAtLeastAsRecent(previous, current)).toBe(true);
        }

        // On overflow the entries kept are the most recent ones: nothing left
        // out is more recent than anything retained.
        const kept = new Set(ids);
        for (const omitted of terminal.filter((e) => !kept.has(e.executionId))) {
          for (const retained of terminal.filter((e) => kept.has(e.executionId))) {
            expect(isAtLeastAsRecent(retained, omitted)).toBe(true);
          }
        }

        // Each entry carries its own run's start time and verdict state (7.1).
        const slotsById = slotsMapOf(seeds);
        for (const entry of history) {
          const execution = requireDefined(byId.get(entry.executionId));
          expect(entry.startedAt).toBe(execution.startedAt);
          expect(entry.verdict).toBe(
            expectedVerdict(execution, requireDefined(slotsById.get(entry.executionId))),
          );
        }
      }),
    );
  });

  it("derives each entry's verdict by the requirement's precedence order", () => {
    fc.assert(
      fc.property(timelineArb, (seeds) => {
        for (const seed of seeds) {
          const verdict = runVerdictState(seed.execution, seed.slots);
          expect(verdict).toBe(expectedVerdict(seed.execution, seed.slots));

          // Precedence, stated as dominance relations (7.1).
          const states = seed.slots.map((slot) => slot.verdict?.state);
          if (seed.execution.status === "failed") {
            // The run's own failure dominates whatever slot data exists.
            expect(verdict).toBe("failed-run");
          } else if (states.includes("fail")) {
            expect(verdict).toBe("fail");
          }
          // `pass` only when all three Inspections passed.
          expect(verdict === "pass").toBe(
            seed.execution.status !== "failed" &&
              states.length === SLOT_COUNT &&
              states.every((state) => state === "pass"),
          );
          // Every entry carries the run's start time alongside its verdict.
          expect(toHistoryEntry(seed.execution, seed.slots)).toEqual({
            executionId: seed.execution.executionId,
            verdict,
            startedAt: seed.execution.startedAt,
          });
        }
      }),
    );
  });

  it("keeps the invariants across a sequence of terminal-run arrivals", () => {
    fc.assert(
      fc.property(timelineArb, fc.nat(), (seeds, rawSplit) => {
        const split = rawSplit % (seeds.length + 1);
        const initial = seeds.slice(0, split);
        const arrivals = inPayloadOrder(seeds.slice(split));

        let history: HistoryEntry[] = buildHistory(
          inPayloadOrder(initial).map((seed) => seed.execution),
          slotsMapOf(initial),
        );

        for (const seed of arrivals) {
          const execution = asTerminalArrival(seed);
          const entry = toHistoryEntry(execution, seed.slots);
          expect(entry.verdict).toBe(expectedVerdict(execution, seed.slots));

          const before = history;
          const snapshot = [...before];
          const next = insertHistoryEntry(before, entry);

          // Purity: the previous strip is untouched.
          expect(before).toEqual(snapshot);

          // The candidate set: the previous strip with any same-run entry
          // replaced, plus the arriving run.
          const combined = [
            ...before.filter((e) => e.executionId !== entry.executionId),
            entry,
          ];

          // Capacity is never exceeded, and nothing is dropped below it (7.2).
          expect(next).toHaveLength(Math.min(combined.length, HISTORY_CAPACITY));

          // Only candidates appear, unchanged, and at most once each.
          const nextIds = next.map((e) => e.executionId);
          expect(new Set(nextIds).size).toBe(nextIds.length);
          for (const e of next) expect(combined).toContainEqual(e);

          // Overflow evicts exactly the oldest entry.
          const retained = new Set(nextIds);
          const dropped = combined.filter((e) => !retained.has(e.executionId));
          if (combined.length <= HISTORY_CAPACITY) {
            expect(dropped).toHaveLength(0);
          } else {
            expect(dropped).toHaveLength(1);
            expect(requireDefined(dropped[0]).startedAt).toBe(
              Math.min(...combined.map((e) => e.startedAt)),
            );
          }

          // Still newest first.
          for (let i = 1; i < next.length; i += 1) {
            expect(requireDefined(next[i - 1]).startedAt).toBeGreaterThanOrEqual(
              requireDefined(next[i]).startedAt,
            );
          }

          history = next;
        }

        // Whatever the arrival sequence, the strip stays within capacity.
        expect(history.length).toBeLessThanOrEqual(HISTORY_CAPACITY);
      }),
    );
  });

  it("reports the same verdicts however the per-run slots are supplied", () => {
    fc.assert(
      fc.property(timelineArb, (seeds) => {
        const executions = inPayloadOrder(seeds).map((seed) => seed.execution);
        const asMap = slotsMapOf(seeds);
        const asRecord = Object.fromEntries(asMap);
        const asFunction = (id: string) => asMap.get(id);

        const fromMap = buildHistory(executions, asMap);
        expect(buildHistory(executions, asRecord)).toEqual(fromMap);
        expect(buildHistory(executions, asFunction)).toEqual(fromMap);
        // Runs with no slot data at all are still listed, without a verdict
        // for a missing Inspection (7.1, 7.6).
        for (const entry of buildHistory(executions, new Map())) {
          expect(fromMap.some((e) => e.executionId === entry.executionId)).toBe(true);
          expect(["no-verdict", "failed-run"]).toContain(entry.verdict);
        }
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("history examples", () => {
  function execution(
    overrides: Partial<Execution> & { executionId: string },
  ): Execution {
    return {
      registrationId: "reg-1",
      status: "completed",
      startedAt: 0,
      finishedAt: null,
      failingNodeId: null,
      error: null,
      hasImageResults: false,
      captureId: null,
      ...overrides,
    };
  }

  const passing: VerdictBearingSlot[] = [
    { verdict: { state: "pass" } },
    { verdict: { state: "pass" } },
    { verdict: { state: "pass" } },
  ];

  it("shows the zero-history state for no runs", () => {
    expect(buildHistory([], new Map())).toEqual([]);
  });

  it("prefers the run's own failure over its slot verdicts", () => {
    const failed = execution({ executionId: "a", status: "failed", finishedAt: 5 });
    expect(runVerdictState(failed, passing)).toBe("failed-run");
  });

  it("prefers a failing Inspection over the remaining passes", () => {
    const run = execution({ executionId: "a", finishedAt: 5 });
    expect(
      runVerdictState(run, [passing[0]!, { verdict: { state: "fail" } }, passing[2]!]),
    ).toBe("fail");
  });

  it("is no-verdict when an Inspection has no boolean verdict", () => {
    const run = execution({ executionId: "a", finishedAt: 5 });
    expect(runVerdictState(run, [passing[0]!, {}, passing[2]!])).toBe("no-verdict");
    expect(runVerdictState(run, passing.slice(0, 2))).toBe("no-verdict");
  });

  it("re-inserting the same run replaces rather than duplicates it", () => {
    const entry: HistoryEntry = {
      executionId: "a",
      verdict: "pass",
      startedAt: 10,
    };
    expect(insertHistoryEntry([entry], entry)).toEqual([entry]);
  });
});
