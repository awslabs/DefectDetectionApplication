import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Execution, ExecutionStatus } from "../api/types";
import { HISTORY_CAPACITY } from "./history";
import { initialTripleState, reduce, type TripleAppState } from "./machine";

/**
 * Property test for the Triple_HMI reducer's historical mode.
 *
 * **Feature: imts-triple-inspection-hmi, Property 15: Historical pinning and return-to-live round trip**
 *
 * **Validates: Requirements 7.4, 7.5**
 *
 * Generators model the station's real input space: one workflow running one
 * run at a time, so runs occupy a non-overlapping timeline, and each poll
 * cycle sees the LocalServer's bounded recent-executions payload (`limit=10`,
 * newest first) over the runs that exist at that moment. Runs are revealed to
 * the reducer a few at a time, including cycles that reveal nothing new and
 * cycles that reveal only in-progress runs.
 *
 * Oracles are stated over the run fields directly (recency by `finishedAt`
 * with `startedAt` as the tiebreak) rather than by calling the module's own
 * ordering helpers.
 */

// --------------------------------------------------------------------------
// Constants and helpers
// --------------------------------------------------------------------------

/** The executions poll's `limit=N` (design "Polling", Requirement 3.1). */
const PAYLOAD_LIMIT = 10;

const TERMINAL_STATUSES: readonly ExecutionStatus[] = ["completed", "failed"];
const IN_PROGRESS_STATUSES: readonly ExecutionStatus[] = ["pending", "running"];

function isTerminalRun(execution: Execution): boolean {
  return TERMINAL_STATUSES.includes(execution.status);
}

function isInProgressRun(execution: Execution): boolean {
  return IN_PROGRESS_STATUSES.includes(execution.status);
}

function requireDefined<T>(value: T | undefined | null): T {
  if (value === undefined || value === null) {
    throw new Error("expected a defined value");
  }
  return value;
}

/**
 * Independent recency oracle (Requirement 3.3 ordering, reused by 7.5): the
 * more recent run finished later, and when finish times cannot separate them,
 * started later.
 */
function isAtLeastAsRecent(a: Execution, b: Execution): boolean {
  if (a.finishedAt !== null && b.finishedAt !== null && a.finishedAt !== b.finishedAt) {
    return a.finishedAt > b.finishedAt;
  }
  return a.startedAt >= b.startedAt;
}

/** The runs the LocalServer would return for the runs that exist so far. */
function payloadOf(revealed: readonly Execution[]): Execution[] {
  return [...revealed]
    .sort((a, b) => b.startedAt - a.startedAt)
    .slice(0, PAYLOAD_LIMIT);
}

function terminalRunsOf(payload: readonly Execution[]): Execution[] {
  return payload.filter(isTerminalRun);
}

/** A deep, structurally comparable snapshot for purity checks. */
function snapshot(state: TripleAppState): string {
  return JSON.stringify(state);
}

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

const statusArb = fc.oneof(
  { arbitrary: fc.constantFrom<ExecutionStatus>("completed", "failed"), weight: 5 },
  { arbitrary: fc.constantFrom<ExecutionStatus>("pending", "running"), weight: 1 },
);

/**
 * A non-overlapping timeline of runs of one workflow: unique ids, terminal
 * runs carrying a finish time and in-progress runs carrying none. Long enough
 * to overflow both the poll payload and the history capacity.
 */
const timelineArb: fc.Arbitrary<Execution[]> = fc
  .array(
    fc.record({
      status: statusArb,
      gap: fc.integer({ min: 1, max: 90 }),
      duration: fc.integer({ min: 0, max: 90 }),
      failed: fc.boolean(),
    }),
    { minLength: 1, maxLength: HISTORY_CAPACITY + 6 },
  )
  .map((raw) => {
    let clock = 1_700_000_000;
    return raw.map((entry, index) => {
      const startedAt = clock + entry.gap;
      const finishedAt = startedAt + entry.duration;
      clock = finishedAt;
      const terminal = TERMINAL_STATUSES.includes(entry.status);
      return {
        executionId: `run-${index}`,
        registrationId: "reg-1",
        status: entry.status,
        startedAt,
        finishedAt: terminal ? finishedAt : null,
        failingNodeId: null,
        error: entry.status === "failed" && entry.failed ? "node failed" : null,
        hasImageResults: terminal,
        captureId: terminal ? `cap-${index}` : null,
      } satisfies Execution;
    });
  });

const scenarioArb = fc.record({
  seeds: timelineArb,
  /** How many runs exist when the Live_View is first displayed. */
  initialCount: fc.nat(),
  /** Which of the history strip's runs the Operator selects. */
  selection: fc.nat(),
  /** New runs revealed per poll cycle; 0 exercises an unchanged payload. */
  reveals: fc.array(fc.integer({ min: 0, max: 3 }), { minLength: 1, maxLength: 8 }),
});

/**
 * Drives the reducer to the state the property starts from: a Live_View
 * populated from the runs that already exist (7.8), then a history tile
 * selected (7.3) so the view is pinned to a historical run.
 */
function pinHistoricalRun(
  seeds: readonly Execution[],
  initialCount: number,
  selection: number,
): { state: TripleAppState; pinned: Execution; revealed: number } {
  const revealed = 1 + (initialCount % seeds.length);
  const payload = payloadOf(seeds.slice(0, revealed));
  const terminal = terminalRunsOf(payload);
  // Only a run the history strip shows can be selected.
  fc.pre(terminal.length > 0);

  const live = reduce(initialTripleState("app", "blue-plate-detection-guided-inspection"), {
    type: "executions-polled",
    executions: payload,
    atEpochMs: 1_700_000_000_000,
  });
  const pinned = requireDefined(terminal[selection % terminal.length]);
  const state = reduce(live, { type: "history-run-selected", run: pinned });
  return { state, pinned, revealed };
}

// --------------------------------------------------------------------------
// Property 15
// --------------------------------------------------------------------------

describe("Property 15: Historical pinning and return-to-live round trip", () => {
  it("pins the displayed run while the history and newer-run flag update", () => {
    fc.assert(
      fc.property(scenarioArb, ({ seeds, initialCount, selection, reveals }) => {
        const start = pinHistoricalRun(seeds, initialCount, selection);
        let state = start.state;
        let revealed = start.revealed;

        expect(state.live.mode).toBe("historical");
        expect(state.live.newerRunAvailable).toBe(false);
        const pinnedVM = requireDefined(state.live.displayed);
        expect(pinnedVM.execution.executionId).toBe(start.pinned.executionId);

        let clock = 1_700_000_001_000;
        let newerExpected = false;

        for (const add of reveals) {
          revealed = Math.min(seeds.length, revealed + add);
          const payload = payloadOf(seeds.slice(0, revealed));
          const historyBefore = state.live.history;

          const before = state;
          const beforeSnapshot = snapshot(before);
          clock += 2_000;
          state = reduce(before, {
            type: "executions-polled",
            executions: payload,
            atEpochMs: clock,
          });
          // The reducer is pure: the previous state is untouched.
          expect(snapshot(before)).toBe(beforeSnapshot);

          // The historical view is never replaced (7.4): same view model
          // object, so neither the run nor its loaded content moved.
          expect(state.live.mode).toBe("historical");
          expect(state.live.displayed).toBe(pinnedVM);

          // The history strip keeps updating behind the pinned view (7.4).
          const newest = terminalRunsOf(payload).reduce<Execution | null>(
            (best, run) => (best === null || isAtLeastAsRecent(run, best) ? run : best),
            null,
          );
          if (newest !== null) {
            expect(
              state.live.history.some((e) => e.executionId === newest.executionId),
            ).toBe(true);
          }
          const ids = state.live.history.map((e) => e.executionId);
          expect(new Set(ids).size).toBe(ids.length);
          expect(state.live.history.length).toBeLessThanOrEqual(HISTORY_CAPACITY);
          for (const id of ids) {
            expect(seeds.some((run) => run.executionId === id)).toBe(true);
          }

          // A newer terminal run raises the indicator, and it stays raised
          // until return-to-live (7.4).
          const gained = state.live.history.some(
            (entry) => !historyBefore.some((held) => held.executionId === entry.executionId),
          );
          newerExpected = newerExpected || gained;
          expect(state.live.newerRunAvailable).toBe(newerExpected);

          // The in-progress indicator still tracks the payload (3.4) without
          // touching the pinned content.
          expect(state.live.inProgress).toBe(payload.some(isInProgressRun));
        }

        // A poll cycle that revealed nothing new never raises the indicator.
        if (reveals.every((add) => add === 0)) {
          expect(state.live.newerRunAvailable).toBe(false);
        }
      }),
    );
  });

  it("returns to live on the maximal terminal run with the indicators cleared", () => {
    fc.assert(
      fc.property(scenarioArb, ({ seeds, initialCount, selection, reveals }) => {
        const start = pinHistoricalRun(seeds, initialCount, selection);
        let state = start.state;
        let revealed = start.revealed;
        const pinnedVM = requireDefined(state.live.displayed);

        let clock = 1_700_000_001_000;
        for (const add of reveals) {
          revealed = Math.min(seeds.length, revealed + add);
          clock += 2_000;
          state = reduce(state, {
            type: "executions-polled",
            executions: payloadOf(seeds.slice(0, revealed)),
            atEpochMs: clock,
          });
        }

        const historyBeforeReturn = state.live.history;
        const payload = state.live.latestExecutions;
        state = reduce(state, { type: "return-to-live" });

        // Live mode resumed, both indicators gone (7.5).
        expect(state.live.mode).toBe("live");
        expect(state.live.newerRunAvailable).toBe(false);
        expect(state.live.historicalDataError).toBe(false);
        // The history strip survives the round trip untouched.
        expect(state.live.history).toEqual(historyBeforeReturn);

        // The displayed run is again the maximal terminal run of the latest
        // payload (7.5 resuming Requirement 3 behavior); with no terminal run
        // in the payload the pinned run is retained rather than blanked.
        const terminal = terminalRunsOf(payload);
        if (terminal.length === 0) {
          expect(state.live.displayed).toBe(pinnedVM);
        } else {
          const displayed = requireDefined(state.live.displayed).execution;
          expect(terminal.some((run) => run.executionId === displayed.executionId)).toBe(
            true,
          );
          for (const run of terminal) {
            expect(isAtLeastAsRecent(displayed, run)).toBe(true);
          }
        }

        // Automatic updating has resumed: the next terminal run to complete
        // takes over the Live_View without Operator interaction (7.5).
        const last = requireDefined(seeds[seeds.length - 1]);
        const arrival: Execution = {
          ...last,
          executionId: "run-after-return",
          status: "completed",
          startedAt: last.startedAt + 1_000,
          finishedAt: last.startedAt + 1_030,
        };
        state = reduce(state, {
          type: "executions-polled",
          executions: payloadOf([...seeds.slice(0, revealed), arrival]),
          atEpochMs: clock + 2_000,
        });
        expect(state.live.mode).toBe("live");
        expect(state.live.newerRunAvailable).toBe(false);
        expect(requireDefined(state.live.displayed).execution.executionId).toBe(
          "run-after-return",
        );
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("historical mode examples", () => {
  function execution(overrides: Partial<Execution> & { executionId: string }): Execution {
    return {
      registrationId: "reg-1",
      status: "completed",
      startedAt: 100,
      finishedAt: 110,
      failingNodeId: null,
      error: null,
      hasImageResults: true,
      captureId: null,
      ...overrides,
    };
  }

  const older = execution({ executionId: "older", startedAt: 100, finishedAt: 110 });
  const newer = execution({ executionId: "newer", startedAt: 200, finishedAt: 210 });

  function liveWith(executions: Execution[]): TripleAppState {
    return reduce(initialTripleState("app", "wf"), {
      type: "executions-polled",
      executions,
      atEpochMs: 1_000,
    });
  }

  it("keeps the pinned run and flags the newer one, then restores live", () => {
    const pinned = reduce(liveWith([older]), {
      type: "history-run-selected",
      run: older,
    });
    expect(pinned.live.mode).toBe("historical");

    const polled = reduce(pinned, {
      type: "executions-polled",
      executions: [newer, older],
      atEpochMs: 2_000,
    });
    expect(polled.live.displayed?.execution.executionId).toBe("older");
    expect(polled.live.newerRunAvailable).toBe(true);
    expect(polled.live.history.map((e) => e.executionId)).toEqual(["newer", "older"]);

    const live = reduce(polled, { type: "return-to-live" });
    expect(live.live.mode).toBe("live");
    expect(live.live.newerRunAvailable).toBe(false);
    expect(live.live.displayed?.execution.executionId).toBe("newer");
  });

  it("selecting the run already displayed keeps its loaded content", () => {
    const live = liveWith([older]);
    const loaded = reduce(live, {
      type: "run-data-loaded",
      executionId: "older",
      images: [
        { kind: "node", nodeId: "n1", port: "original", hasOverlay: false },
        { kind: "node", nodeId: "n1", port: "annotated", hasOverlay: false },
      ],
      metadata: { bedrock: { n1: { is_anomalous: false } } },
    });
    const displayed = requireDefined(loaded.live.displayed);

    const pinned = reduce(loaded, { type: "history-run-selected", run: older });
    expect(pinned.live.displayed).toBe(displayed);
    expect(pinned.live.mode).toBe("historical");
  });

  it("clears the historical fetch error when returning to live", () => {
    const pinned = reduce(liveWith([older, newer]), {
      type: "history-run-selected",
      run: older,
    });
    const failed = reduce(pinned, { type: "run-data-failed", executionId: "older" });
    expect(failed.live.historicalDataError).toBe(true);
    // The strip stays intact and the return control still works (7.7).
    expect(failed.live.history).toHaveLength(2);

    const live = reduce(failed, { type: "return-to-live" });
    expect(live.live.historicalDataError).toBe(false);
    expect(live.live.displayed?.execution.executionId).toBe("newer");
  });
});
