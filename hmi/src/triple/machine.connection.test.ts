import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { ApiErrorKind } from "../api/client";
import type {
  Execution,
  ExecutionStatus,
  Registration,
  ResultImage,
} from "../api/types";
import {
  initialTripleState,
  reduce,
  type ConnectionState,
  type TripleAppState,
  type TripleEvent,
} from "./machine";

/**
 * Property test for the Triple_HMI's connection state machine.
 *
 * **Feature: imts-triple-inspection-hmi, Property 16: Connection state transitions**
 *
 * **Validates: Requirements 8.1, 8.2, 8.3**
 *
 * The connection machine lives in the triple reducer, so the property is
 * stated over `reduce`: for any sequence of LocalServer request outcomes the
 * connection state is disconnected exactly after a network error, a 10-second
 * timeout, or an HTTP 5xx (Requirement 8.1); an HTTP 401 never disconnects and
 * instead routes to the auth path (Requirements 1.4, 8.1); the last displayed
 * Run_Result, the history strip, and the last-successful-update time survive
 * every failure (8.1); the disconnected state persists across an unbounded run
 * of failed retry probes and is left exactly by a 2xx response (8.2); and that
 * 2xx reconnects in the same reducer step with the update cycle resumed — the
 * live slice it produces is identical to the one the same response produces
 * while connected (8.3).
 *
 * Generators model the station's real outcome stream: one workflow running one
 * run at a time (so runs occupy a non-overlapping timeline and each poll
 * returns a prefix of it), interleaved with every failure classification
 * `api/client.ts` reports, plus the disconnected-retry probe against
 * `GET /workflows/registrations` and the auth-path events.
 */

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

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

/** A minimal results inventory, enough to give a displayed run real content. */
const SAMPLE_IMAGES: readonly ResultImage[] = [
  { kind: "node", nodeId: "bedrock_1", port: "original", hasOverlay: false },
  { kind: "node", nodeId: "bedrock_1", port: "annotated", hasOverlay: false },
  { kind: "node", nodeId: "bedrock_2", port: "original", hasOverlay: false },
];

/** The failure kinds Requirement 8.1 names as connection loss. */
const DISCONNECTING_KINDS: readonly ApiErrorKind[] = ["network", "timeout", "http-5xx"];

function isTerminalStatus(status: ExecutionStatus): boolean {
  return status === "completed" || status === "failed";
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

const timestampArb = fc.integer({ min: 1, max: 2_000_000_000 });

interface RunPlan {
  status: ExecutionStatus;
  /** Idle seconds before the run started. */
  gap: number;
  /** Run duration in seconds. */
  duration: number;
}

const runPlanArb: fc.Arbitrary<RunPlan> = fc.record({
  status: fc.constantFrom<ExecutionStatus>("completed", "failed", "pending", "running"),
  gap: fc.integer({ min: 1, max: 120 }),
  duration: fc.integer({ min: 0, max: 120 }),
});

/**
 * A non-overlapping timeline of runs of the bound registration: the station
 * runs one inspection at a time, so each run starts after the previous
 * finished, terminal runs carry a finish time and in-progress runs do not.
 */
const timelineArb: fc.Arbitrary<Execution[]> = fc
  .array(runPlanArb, { minLength: 1, maxLength: 6 })
  .map((plans) => {
    let clock = 1_700_000_100;
    return plans.map((plan, index) => {
      const startedAt = clock + plan.gap;
      const finishedAt = startedAt + plan.duration;
      clock = finishedAt;
      return {
        executionId: `run-${index}`,
        registrationId: REGISTRATION_ID,
        status: plan.status,
        startedAt,
        finishedAt: isTerminalStatus(plan.status) ? finishedAt : null,
        failingNodeId: null,
        error: plan.status === "failed" ? "node bedrock_1 failed" : null,
        hasImageResults: isTerminalStatus(plan.status),
        captureId: `cap-${index}`,
      } satisfies Execution;
    });
  });

/**
 * One LocalServer request outcome. The successes are the responses that carry
 * a 2xx — an executions poll, a non-poll request (results, metadata, or a
 * disconnected-retry probe against `/workflows/registrations`), and a login —
 * and the failures are a failed poll cycle and a failed non-poll request.
 */
type Outcome =
  | { kind: "poll-ok"; runCount: number; atEpochMs: number }
  | { kind: "poll-fail"; errorKind: ApiErrorKind }
  | { kind: "probe-ok"; atEpochMs: number }
  | { kind: "request-fail"; errorKind: ApiErrorKind }
  | { kind: "login-ok"; atEpochMs: number }
  | { kind: "auth-expired" };

const outcomeArb: fc.Arbitrary<Outcome> = fc.oneof(
  {
    arbitrary: fc.record({
      kind: fc.constant<"poll-ok">("poll-ok"),
      runCount: fc.nat({ max: 6 }),
      atEpochMs: timestampArb,
    }),
    weight: 4,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"poll-fail">("poll-fail"),
      errorKind: errorKindArb,
    }),
    weight: 4,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"probe-ok">("probe-ok"),
      atEpochMs: timestampArb,
    }),
    weight: 2,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"request-fail">("request-fail"),
      errorKind: errorKindArb,
    }),
    weight: 4,
  },
  {
    arbitrary: fc.record({
      kind: fc.constant<"login-ok">("login-ok"),
      atEpochMs: timestampArb,
    }),
    weight: 1,
  },
  { arbitrary: fc.constant<Outcome>({ kind: "auth-expired" }), weight: 1 },
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
    case "probe-ok":
      return { type: "request-succeeded", atEpochMs: outcome.atEpochMs };
    case "request-fail":
      return { type: "request-failed", kind: outcome.errorKind };
    case "login-ok":
      return { type: "login-succeeded", atEpochMs: outcome.atEpochMs };
    case "auth-expired":
      return { type: "auth-expired" };
  }
}

type SuccessOutcome = Extract<Outcome, { atEpochMs: number }>;
type FailureOutcome = Extract<Outcome, { errorKind: ApiErrorKind }>;

/** True for the outcomes that carry an HTTP 2xx response (8.2, 8.3). */
function isSuccess(outcome: Outcome): outcome is SuccessOutcome {
  return (
    outcome.kind === "poll-ok" ||
    outcome.kind === "probe-ok" ||
    outcome.kind === "login-ok"
  );
}

/** True for the outcomes that are a failed LocalServer request. */
function isFailure(outcome: Outcome): outcome is FailureOutcome {
  return outcome.kind === "poll-fail" || outcome.kind === "request-fail";
}

/**
 * The connection state after one outcome, recomputed from Requirement 8's
 * wording rather than from the reducer's structure: a network error, a 10 s
 * timeout, or an HTTP 5xx is disconnection (8.1); any 2xx is connectivity
 * restored (8.2, 8.3); an HTTP 401 belongs to the auth path and any other
 * HTTP error is a per-request failure, so neither moves the connection.
 */
function nextConnection(state: ConnectionState, outcome: Outcome): ConnectionState {
  if (isSuccess(outcome)) return "connected";
  if (isFailure(outcome) && DISCONNECTING_KINDS.includes(outcome.errorKind)) {
    return "disconnected";
  }
  return state;
}

/** The last-successful-update time after one outcome (8.1). */
function nextLastUpdate(previous: number | null, outcome: Outcome): number | null {
  return isSuccess(outcome) ? outcome.atEpochMs : previous;
}

/** Everything the Operator sees, minus the connection slice itself. */
function content(state: TripleAppState): unknown {
  return {
    auth: state.auth,
    binding: state.binding,
    live: state.live,
    runSlots: state.runSlots,
    targetName: state.targetName,
  };
}

// --------------------------------------------------------------------------
// Prior states
// --------------------------------------------------------------------------

/** How the app state was reached before the outcome sequence is applied. */
interface PriorPlan {
  /** How many of the timeline's runs the prior state has already seen. */
  seen: number;
  /** Whether the displayed run's `/results` + `/metadata` were applied. */
  loadData: boolean;
  /** Whether the Operator pinned a historical run. */
  historical: boolean;
}

const priorPlanArb: fc.Arbitrary<PriorPlan> = fc.record({
  seen: fc.nat(),
  loadData: fc.boolean(),
  historical: fc.boolean(),
});

/**
 * Builds a prior app state by folding real events through the reducer: the
 * bind, an earlier poll, that run's data, and optionally a history-tile
 * selection — so the property holds over states the poller can actually
 * produce, with content on screen that a disconnection must retain.
 */
function buildPriorState(
  plan: PriorPlan,
  timeline: readonly Execution[],
): TripleAppState {
  let state = reduce(initialTripleState("app", TARGET_NAME), {
    type: "registrations-loaded",
    registrations: [REGISTRATION],
  });

  const seen = timeline.slice(0, plan.seen % (timeline.length + 1));
  if (seen.length === 0) return state;

  state = reduce(state, {
    type: "executions-polled",
    executions: seen,
    atEpochMs: 1_700_000_100_000,
  });

  const displayed = state.live.displayed;
  if (plan.loadData && displayed !== null) {
    state = reduce(state, {
      type: "run-data-loaded",
      executionId: displayed.execution.executionId,
      images: SAMPLE_IMAGES,
      metadata: { is_anomalous: false, confidence: 0.9312 },
    });
  }

  if (plan.historical) {
    const terminalSeen = seen.filter((execution) => isTerminalStatus(execution.status));
    const pinned = terminalSeen[terminalSeen.length - 1];
    if (pinned !== undefined) {
      state = reduce(state, { type: "history-run-selected", run: pinned });
    }
  }

  return state;
}

// --------------------------------------------------------------------------
// Property 16
// --------------------------------------------------------------------------

describe("Property 16: Connection state transitions", () => {
  it("is disconnected exactly after a network error, a timeout, or an HTTP 5xx", () => {
    fc.assert(
      fc.property(
        priorPlanArb,
        timelineArb,
        timelineArb,
        outcomesArb,
        (plan, priorRuns, timeline, outcomes) => {
          let state = buildPriorState(plan, priorRuns);
          let expected: ConnectionState = state.connection.state;
          let expectedUpdate = state.connection.lastSuccessfulUpdate;

          for (const outcome of outcomes) {
            const before = state;
            state = reduce(before, toEvent(outcome, timeline));
            expected = nextConnection(expected, outcome);
            expectedUpdate = nextLastUpdate(expectedUpdate, outcome);

            // The machine matches the independently computed state at every
            // step of the sequence (8.1, 8.3).
            expect(state.connection.state).toBe(expected);
            // The time of the last successful LocalServer update is carried
            // forward across failures and only advanced by a 2xx (8.1).
            expect(state.connection.lastSuccessfulUpdate).toBe(expectedUpdate);

            if (isFailure(outcome)) {
              // Every failure retains the last displayed Run_Result, the
              // history strip, and the binding unchanged (8.1).
              expect(content(state)).toEqual(content(before));
              // A 401 routes to the auth path, never to disconnected (8.1).
              if (outcome.errorKind === "http-401") {
                expect(state.connection.state).toBe(before.connection.state);
              }
            }
          }
        },
      ),
    );
  });

  it("stays disconnected across an unbounded run of failed retry probes", () => {
    fc.assert(
      fc.property(
        priorPlanArb,
        timelineArb,
        fc.constantFrom<ApiErrorKind>("network", "timeout", "http-5xx"),
        fc.array(errorKindArb, { minLength: 1, maxLength: 30 }),
        (plan, priorRuns, initialKind, probeKinds) => {
          const seeded = buildPriorState(plan, priorRuns);
          let state = reduce(seeded, { type: "request-failed", kind: initialKind });
          expect(state.connection.state).toBe("disconnected");

          const disconnected = state;
          for (const kind of probeKinds) {
            // Each disconnected-retry probe against /workflows/registrations
            // either fails again or is a 401; retrying is unlimited, so the
            // machine never leaves the disconnected state on its own (8.2).
            state = reduce(state, { type: "request-failed", kind });
            expect(state.connection.state).toBe("disconnected");
            expect(content(state)).toEqual(content(disconnected));
            expect(state.connection.lastSuccessfulUpdate).toBe(
              disconnected.connection.lastSuccessfulUpdate,
            );
          }
        },
      ),
    );
  });

  it("reconnects on the first 2xx probe and resumes the update cycle", () => {
    fc.assert(
      fc.property(
        priorPlanArb,
        timelineArb,
        timelineArb,
        fc.array(fc.constantFrom<ApiErrorKind>("network", "timeout", "http-5xx"), {
          minLength: 1,
          maxLength: 12,
        }),
        fc.nat({ max: 6 }),
        timestampArb,
        (plan, priorRuns, timeline, kinds, runCount, atEpochMs) => {
          const connected = buildPriorState(plan, priorRuns);
          let state = connected;
          for (const kind of kinds) {
            state = reduce(state, { type: "poll-failed", kind });
          }
          expect(state.connection.state).toBe("disconnected");

          // A 2xx retry probe restores connectivity in the same step (8.2, 8.3).
          const probed = reduce(state, { type: "request-succeeded", atEpochMs });
          expect(probed.connection.state).toBe("connected");
          expect(probed.connection.lastSuccessfulUpdate).toBe(atEpochMs);
          expect(content(probed)).toEqual(content(state));

          // The resumed update cycle behaves exactly as it does while
          // connected: the poll that follows produces the identical live
          // slice, so the disconnected period leaves no residue (8.3).
          const poll: TripleEvent = {
            type: "executions-polled",
            executions: timeline.slice(0, runCount),
            atEpochMs: atEpochMs + 2_000,
          };
          const resumed = reduce(probed, poll);
          const uninterrupted = reduce(connected, poll);

          expect(resumed.connection.state).toBe("connected");
          expect(content(resumed)).toEqual(content(uninterrupted));
          expect(resumed.connection).toEqual(uninterrupted.connection);
        },
      ),
    );
  });

  it("routes an expired session to the auth path without disconnecting", () => {
    fc.assert(
      fc.property(priorPlanArb, timelineArb, (plan, priorRuns) => {
        const state = buildPriorState(plan, priorRuns);

        // The client's single re-login failed (1.4). The connection is
        // untouched: a 401 is never connection loss (8.1).
        const rejected = reduce(state, { type: "request-failed", kind: "http-401" });
        const expired = reduce(rejected, { type: "auth-expired" });

        expect(rejected.connection.state).toBe(state.connection.state);
        expect(expired.connection.state).toBe(state.connection.state);
        expect(expired.connection.lastSuccessfulUpdate).toBe(
          state.connection.lastSuccessfulUpdate,
        );
        expect(expired.auth.screen).toBe("login");
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("connection state examples", () => {
  const run: Execution = {
    executionId: "run-0",
    registrationId: REGISTRATION_ID,
    status: "completed",
    startedAt: 1_700_000_200,
    finishedAt: 1_700_000_260,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: "cap-0",
  };

  function displaying(): TripleAppState {
    const bound = reduce(initialTripleState("app", TARGET_NAME), {
      type: "registrations-loaded",
      registrations: [REGISTRATION],
    });
    return reduce(bound, {
      type: "executions-polled",
      executions: [run],
      atEpochMs: 1_700_000_300_000,
    });
  }

  it("starts connected once a response has been seen", () => {
    expect(displaying().connection.state).toBe("connected");
  });

  it("disconnects on a network error, a timeout, and an HTTP 5xx", () => {
    for (const kind of DISCONNECTING_KINDS) {
      expect(reduce(displaying(), { type: "poll-failed", kind }).connection.state).toBe(
        "disconnected",
      );
    }
  });

  it("does not disconnect on a 401 or another HTTP error", () => {
    for (const kind of ["http-401", "http-other"] as const) {
      expect(reduce(displaying(), { type: "poll-failed", kind }).connection.state).toBe(
        "connected",
      );
    }
  });

  it("retains the displayed run and the last update time while disconnected", () => {
    const seeded = displaying();
    const lost = reduce(seeded, { type: "request-failed", kind: "timeout" });

    expect(lost.live.displayed?.execution.executionId).toBe("run-0");
    expect(lost.live.history).toEqual(seeded.live.history);
    expect(lost.connection.lastSuccessfulUpdate).toBe(1_700_000_300_000);
  });

  it("reconnects on a 2xx registrations probe", () => {
    const lost = reduce(displaying(), { type: "request-failed", kind: "network" });
    const restored = reduce(lost, {
      type: "request-succeeded",
      atEpochMs: 1_700_000_400_000,
    });

    expect(restored.connection.state).toBe("connected");
    expect(restored.connection.lastSuccessfulUpdate).toBe(1_700_000_400_000);
    expect(restored.live.displayed?.execution.executionId).toBe("run-0");
  });
});
