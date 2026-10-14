import { describe, expect, it } from "vitest";

import type { Execution, ExecutionStatus, Registration } from "../api/types";
import { HISTORY_CAPACITY } from "./history";
import {
  initialTripleState,
  reduce,
  type TripleAppState,
  type TripleEvent,
} from "./machine";

/**
 * Regression tests for the history strip's OBJECT IDENTITY across polls
 * (Requirement 3.4: a poll cycle must be non-destructive).
 *
 * `machine.inprogress.test.ts`'s Property 7 asserts `state.live.history` is
 * the same object across indicator transitions. It caught a case the
 * per-entry guard in `mergeHistory` misses: a terminal run that sits OUTSIDE
 * the capacity window is not in the strip, so the guard never matches it, and
 * `insertHistoryEntry` appends it only for the capacity trim to drop it again.
 * The result was element-wise equal to the previous strip but a NEW array —
 * on every poll, which re-renders the history strip needlessly and breaks the
 * non-destructive guarantee.
 *
 * These tests pin that behaviour deterministically, rather than relying on a
 * random property seed to rediscover it.
 */

const TARGET_NAME = "blue-plate-detection-guided-inspection";
const REGISTRATION_ID = "reg-1";

const REGISTRATION: Registration = {
  registrationId: REGISTRATION_ID,
  workflowId: "wf-1",
  name: TARGET_NAME,
  version: "1.0.0",
  status: "registered",
  registeredAt: 1_700_000_000,
};

function poll(executions: readonly Execution[], atEpochMs: number): TripleEvent {
  return { type: "executions-polled", executions, atEpochMs };
}

/** One completed run, `index` seconds after the base clock. */
function completedRun(index: number): Execution {
  const startedAt = 1_700_000_100 + index;
  return {
    executionId: `run-${index}`,
    registrationId: REGISTRATION_ID,
    status: "completed" as ExecutionStatus,
    startedAt,
    finishedAt: startedAt,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: `cap-${index}`,
  };
}

/** A bound state that has already settled on `runs`. */
function settledOn(runs: readonly Execution[]): TripleAppState {
  const bound = reduce(initialTripleState("app", TARGET_NAME), {
    type: "registrations-loaded",
    registrations: [REGISTRATION],
  });
  return reduce(bound, poll(runs, 1_700_000_300_000));
}

describe("history strip identity across polls (Requirement 3.4)", () => {
  it("keeps the same array when the payload holds more terminal runs than the capacity", () => {
    // One run more than the strip can hold, so exactly one stays outside the
    // capacity window on every poll — the case that used to rebuild the array.
    const runs = Array.from({ length: HISTORY_CAPACITY + 1 }, (_, index) =>
      completedRun(index),
    );

    const settled = settledOn(runs);
    expect(settled.live.history).toHaveLength(HISTORY_CAPACITY);
    const history = settled.live.history;

    // Re-polling the identical payload must not disturb the strip at all.
    let state = settled;
    for (let cycle = 0; cycle < 3; cycle += 1) {
      state = reduce(state, poll(runs, 1_700_000_400_000 + cycle));
      expect(state.live.history).toBe(history);
    }
  });

  it("keeps the same array when in-progress runs come and go around a full strip", () => {
    const runs = Array.from({ length: HISTORY_CAPACITY + 1 }, (_, index) =>
      completedRun(index),
    );
    const running: Execution = {
      executionId: "run-live",
      registrationId: REGISTRATION_ID,
      status: "running" as ExecutionStatus,
      startedAt: 1_700_009_000,
      finishedAt: null,
      failingNodeId: null,
      error: null,
      hasImageResults: false,
      captureId: "cap-live",
    };

    const settled = settledOn(runs);
    const history = settled.live.history;

    // Indicator on, then off, then on again: none of it touches the strip.
    let state = reduce(settled, poll([...runs, running], 1_700_000_400_000));
    expect(state.live.inProgress).toBe(true);
    expect(state.live.history).toBe(history);

    state = reduce(state, poll(runs, 1_700_000_400_001));
    expect(state.live.inProgress).toBe(false);
    expect(state.live.history).toBe(history);

    state = reduce(state, poll([...runs, running], 1_700_000_400_002));
    expect(state.live.inProgress).toBe(true);
    expect(state.live.history).toBe(history);
  });

  it("still replaces the array when the strip's contents actually change", () => {
    const runs = Array.from({ length: HISTORY_CAPACITY + 1 }, (_, index) =>
      completedRun(index),
    );
    const settled = settledOn(runs);
    const history = settled.live.history;

    // A genuinely newer terminal run belongs in the strip, so the identity
    // MUST change — the fix must not freeze the strip.
    const newer = completedRun(HISTORY_CAPACITY + 50);
    const state = reduce(settled, poll([...runs, newer], 1_700_000_500_000));

    expect(state.live.history).not.toBe(history);
    expect(state.live.history[0]?.executionId).toBe(newer.executionId);
    expect(state.live.history).toHaveLength(HISTORY_CAPACITY);
    // In live mode the newer run simply becomes the displayed one, so the
    // newer-run flag stays down; it is the historical-mode pin (7.4) that
    // raises it. Asserted here so this test cannot be read as claiming
    // otherwise.
    expect(state.live.newerRunAvailable).toBe(false);
    expect(state.live.displayed?.execution.executionId).toBe(newer.executionId);
  });
});
