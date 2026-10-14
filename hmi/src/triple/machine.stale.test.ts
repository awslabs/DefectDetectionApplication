import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ApiErrorKind } from "../api/client";
import type { Execution, Registration } from "../api/types";
import {
  initialTripleState,
  isStaleData,
  reduce,
  STALE_POLL_FAILURE_THRESHOLD,
  type ConnectionSlice,
  type TripleAppState,
  type TripleEvent,
} from "./machine";

/**
 * Property test for the Triple_HMI reducer's poll-failure handling.
 *
 * **Feature: imts-triple-inspection-hmi, Property 8: Poll-failure retention and staleness accounting**
 *
 * **Validates: Requirements 3.8, 3.9**
 *
 * Generators model the station's real polling stream: one workflow running one
 * run at a time (so runs occupy a non-overlapping timeline and each poll
 * returns a growing prefix of it), interleaved with the failure kinds
 * `api/client.ts` classifies — network errors, the 10 s timeout, HTTP 5xx,
 * 401, and other HTTP errors.
 *
 * Two independent oracles carry the requirements:
 *
 *  - **Retention (3.8)**: a failed cycle is a content no-op. Stated as
 *    "folding the same outcome sequence with every failure removed yields the
 *    identical displayed run, history, binding, and cached slots", so nothing
 *    the operator sees can shift on a failure.
 *  - **Staleness (3.9)**: a running count of consecutive *polling* cycles that
 *    failed, recomputed here from the event sequence alone, with the indicator
 *    shown exactly when that count reaches 5 and any success resetting it in
 *    the same step.
 */

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const TARGET_NAME = "blue-plate-detection-guided-inspection";

const REGISTRATION: Registration = {
  registrationId: "reg-1",
  workflowId: "wf-1",
  name: TARGET_NAME,
  version: "1.0.0",
  status: "registered",
  registeredAt: 1_700_000_000,
};

/** The app bound to the Target_Workflow, before any poll outcome. */
function boundState(): TripleAppState {
  return reduce(initialTripleState("app", TARGET_NAME), {
    type: "registrations-loaded",
    registrations: [REGISTRATION],
  });
}

// --------------------------------------------------------------------------
// Generators
// --------------------------------------------------------------------------

/** Every failure classification the shared client reports. */
const errorKindArb = fc.constantFrom<ApiErrorKind>(
  "network",
  "timeout",
  "http-5xx",
  "http-401",
  "http-other",
);

/**
 * A non-overlapping timeline of terminal runs of the bound workflow: the
 * station runs one inspection at a time, so each run starts after the
 * previous finished.
 */
const timelineArb: fc.Arbitrary<Execution[]> = fc
  .array(
    fc.record({
      gap: fc.integer({ min: 1, max: 60 }),
      duration: fc.integer({ min: 0, max: 60 }),
      failed: fc.boolean(),
    }),
    { minLength: 1, maxLength: 6 },
  )
  .map((raw) => {
    let clock = 1_700_000_100;
    return raw.map((entry, index) => {
      const startedAt = clock + entry.gap;
      const finishedAt = startedAt + entry.duration;
      clock = finishedAt;
      return {
        executionId: `run-${index}`,
        registrationId: REGISTRATION.registrationId,
        status: entry.failed ? "failed" : "completed",
        startedAt,
        finishedAt,
        failingNodeId: null,
        error: entry.failed ? "node failed" : null,
        hasImageResults: !entry.failed,
        captureId: `cap-${index}`,
      } satisfies Execution;
    });
  });

/**
 * One update-cycle outcome. `poll-failed` is a failed polling cycle;
 * `request-failed` is a failed non-poll request (results, metadata,
 * registrations), which is not an update cycle of its own.
 */
type Outcome =
  | { kind: "poll-ok"; runCount: number; atEpochMs: number }
  | { kind: "poll-fail"; errorKind: ApiErrorKind }
  | { kind: "request-ok"; atEpochMs: number }
  | { kind: "request-fail"; errorKind: ApiErrorKind }
  | { kind: "login-ok"; atEpochMs: number };

/**
 * Failures are weighted heavily so runs of 5+ consecutive failed cycles —
 * the staleness threshold — occur often in a 100-iteration budget.
 */
const outcomeArb: fc.Arbitrary<Outcome> = fc.oneof(
  {
    arbitrary: fc.record({
      kind: fc.constant<"poll-ok">("poll-ok"),
      runCount: fc.nat({ max: 6 }),
      atEpochMs: fc.integer({ min: 1, max: 2_000_000_000 }),
    }),
    weight: 3,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"poll-fail">("poll-fail"),
      errorKind: errorKindArb,
    }),
    weight: 6,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"request-ok">("request-ok"),
      atEpochMs: fc.integer({ min: 1, max: 2_000_000_000 }),
    }),
    weight: 1,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"request-fail">("request-fail"),
      errorKind: errorKindArb,
    }),
    weight: 2,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"login-ok">("login-ok"),
      atEpochMs: fc.integer({ min: 1, max: 2_000_000_000 }),
    }),
    weight: 1,
  },
);

const outcomesArb = fc.array(outcomeArb, { maxLength: 24 });

// --------------------------------------------------------------------------
// Oracles
// --------------------------------------------------------------------------

function toEvent(outcome: Outcome, timeline: readonly Execution[]): TripleEvent {
  switch (outcome.kind) {
    case "poll-ok":
      return {
        type: "executions-polled",
        executions: timeline.slice(0, outcome.runCount),
        atEpochMs: outcome.atEpochMs,
      };
    case "poll-fail":
      return { type: "poll-failed", kind: outcome.errorKind };
    case "request-ok":
      return { type: "request-succeeded", atEpochMs: outcome.atEpochMs };
    case "request-fail":
      return { type: "request-failed", kind: outcome.errorKind };
    case "login-ok":
      return { type: "login-succeeded", atEpochMs: outcome.atEpochMs };
  }
}

/** True for the outcomes that are a failure of a polling update cycle (3.9). */
function isFailedCycle(outcome: Outcome): boolean {
  return outcome.kind === "poll-fail";
}

/** True for the outcomes that are a successful LocalServer response (3.9). */
function isSuccess(outcome: Outcome): boolean {
  return (
    outcome.kind === "poll-ok" ||
    outcome.kind === "request-ok" ||
    outcome.kind === "login-ok"
  );
}

/** True for every failed outcome, polling cycle or not (3.8). */
function isFailure(outcome: Outcome): boolean {
  return outcome.kind === "poll-fail" || outcome.kind === "request-fail";
}

/**
 * The consecutive-failed-cycle count after one outcome, recomputed from the
 * requirement's wording rather than from the reducer's structure: a failed
 * polling cycle advances the count, any success clears it, and a failed
 * non-poll request is not a cycle and leaves it alone.
 */
function nextCount(count: number, outcome: Outcome): number {
  if (isSuccess(outcome)) return 0;
  return isFailedCycle(outcome) ? count + 1 : count;
}

/** Everything the operator sees, minus the connection slice itself. */
function content(state: TripleAppState): unknown {
  return {
    auth: state.auth,
    binding: state.binding,
    live: state.live,
    runSlots: state.runSlots,
    targetName: state.targetName,
  };
}

function fold(
  initial: TripleAppState,
  outcomes: readonly Outcome[],
  timeline: readonly Execution[],
): TripleAppState {
  return outcomes.reduce(
    (state, outcome) => reduce(state, toEvent(outcome, timeline)),
    initial,
  );
}

// --------------------------------------------------------------------------
// Property 8
// --------------------------------------------------------------------------

describe("Property 8: Poll-failure retention and staleness accounting", () => {
  it("raises the stale indicator at five or more consecutive failed cycles", () => {
    expect(STALE_POLL_FAILURE_THRESHOLD).toBe(5);
  });

  it("shows the indicator exactly when the running failure count reaches the threshold", () => {
    fc.assert(
      fc.property(timelineArb, outcomesArb, (timeline, outcomes) => {
        let state = boundState();
        let expected = 0;

        expect(isStaleData(state)).toBe(false);

        for (const outcome of outcomes) {
          state = reduce(state, toEvent(outcome, timeline));
          expected = nextCount(expected, outcome);

          // The reducer's accounting matches the independent count, and the
          // indicator is shown if and only if that count is ≥5 (3.9).
          expect(state.connection.consecutivePollFailures).toBe(expected);
          expect(isStaleData(state)).toBe(expected >= STALE_POLL_FAILURE_THRESHOLD);

          // A success clears the count, and therefore the indicator, in the
          // very step it is applied — within one update cycle (3.9).
          if (isSuccess(outcome)) {
            expect(expected).toBe(0);
            expect(isStaleData(state)).toBe(false);
          }
        }
      }),
    );
  });

  it("leaves the displayed content unchanged on every failed outcome", () => {
    fc.assert(
      fc.property(timelineArb, outcomesArb, (timeline, outcomes) => {
        let state = boundState();

        for (const outcome of outcomes) {
          const before = state;
          state = reduce(before, toEvent(outcome, timeline));

          if (!isFailure(outcome)) continue;

          // The Run_Result on screen, the history strip, the in-progress
          // flag, the binding, and the cached slot verdicts are all retained
          // unchanged; only the connection slice may move (3.8).
          expect(content(state)).toEqual(content(before));
          // The last-successful-update time survives the failure (8.1).
          expect(state.connection.lastSuccessfulUpdate).toBe(
            before.connection.lastSuccessfulUpdate,
          );
          // Purity: the failure produced a new state, not a mutated one.
          expect(content(before)).toEqual(content(state));
        }
      }),
    );
  });

  it("is content-equivalent to the same sequence with every failure removed", () => {
    fc.assert(
      fc.property(timelineArb, outcomesArb, (timeline, outcomes) => {
        const withFailures = fold(boundState(), outcomes, timeline);
        const successesOnly = fold(
          boundState(),
          outcomes.filter((outcome) => !isFailure(outcome)),
          timeline,
        );

        // Failed cycles contribute nothing to what is displayed, so the whole
        // fold is indistinguishable from the fold of its successes (3.8).
        expect(content(withFailures)).toEqual(content(successesOnly));
        expect(withFailures.connection.lastSuccessfulUpdate).toBe(
          successesOnly.connection.lastSuccessfulUpdate,
        );
      }),
    );
  });

  it("clears the indicator on the first success after any run of failures", () => {
    fc.assert(
      fc.property(
        timelineArb,
        fc.array(errorKindArb, {
          minLength: STALE_POLL_FAILURE_THRESHOLD,
          maxLength: STALE_POLL_FAILURE_THRESHOLD + 8,
        }),
        fc.nat({ max: 6 }),
        (timeline, kinds, runCount) => {
          let state = boundState();
          for (const kind of kinds) {
            state = reduce(state, { type: "poll-failed", kind });
          }

          // A long run of failed cycles is stale, with content still intact.
          expect(isStaleData(state)).toBe(true);
          expect(state.connection.consecutivePollFailures).toBe(kinds.length);

          const recovered = reduce(state, {
            type: "executions-polled",
            executions: timeline.slice(0, runCount),
            atEpochMs: 1_700_001_000,
          });

          expect(recovered.connection.consecutivePollFailures).toBe(0);
          expect(isStaleData(recovered)).toBe(false);
          // The recovering response is a 2xx, so it also reconnects (8.3).
          expect(recovered.connection.state).toBe("connected");
        },
      ),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("poll-failure staleness examples", () => {
  const timeline: Execution[] = [
    {
      executionId: "run-0",
      registrationId: REGISTRATION.registrationId,
      status: "completed",
      startedAt: 1_700_000_200,
      finishedAt: 1_700_000_260,
      failingNodeId: null,
      error: null,
      hasImageResults: true,
      captureId: "cap-0",
    },
  ];

  function withDisplayedRun(): TripleAppState {
    return reduce(boundState(), {
      type: "executions-polled",
      executions: timeline,
      atEpochMs: 1_700_000_300_000,
    });
  }

  function failTimes(state: TripleAppState, count: number): TripleAppState {
    let next = state;
    for (let i = 0; i < count; i += 1) {
      next = reduce(next, { type: "poll-failed", kind: "network" });
    }
    return next;
  }

  it("is not stale at four consecutive failures and stale at five", () => {
    const seeded = withDisplayedRun();
    expect(isStaleData(failTimes(seeded, 4))).toBe(false);
    expect(isStaleData(failTimes(seeded, 5))).toBe(true);
    expect(isStaleData(failTimes(seeded, 9))).toBe(true);
  });

  it("keeps the displayed run and the last update time across failures", () => {
    const seeded = withDisplayedRun();
    const stale = failTimes(seeded, 6);

    expect(stale.live.displayed?.execution.executionId).toBe("run-0");
    expect(stale.live.history).toEqual(seeded.live.history);
    expect(stale.connection.lastSuccessfulUpdate).toBe(1_700_000_300_000);
  });

  it("does not count a failed non-poll request as an update cycle", () => {
    const seeded = failTimes(withDisplayedRun(), 4);
    const afterOther = reduce(seeded, { type: "request-failed", kind: "http-5xx" });

    expect(afterOther.connection.consecutivePollFailures).toBe(4);
    expect(isStaleData(afterOther)).toBe(false);
  });

  it("removes the indicator on the next successful poll", () => {
    const stale = failTimes(withDisplayedRun(), 7);
    const recovered = reduce(stale, {
      type: "executions-polled",
      executions: timeline,
      atEpochMs: 1_700_000_400_000,
    });

    const connection: ConnectionSlice = recovered.connection;
    expect(connection.consecutivePollFailures).toBe(0);
    expect(connection.lastSuccessfulUpdate).toBe(1_700_000_400_000);
    expect(isStaleData(recovered)).toBe(false);
  });
});
