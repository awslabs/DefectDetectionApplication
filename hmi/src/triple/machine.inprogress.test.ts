import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type {
  Execution,
  ExecutionStatus,
  Registration,
  ResultImage,
} from "../api/types";
import {
  initialTripleState,
  reduce,
  type TripleAppState,
  type TripleEvent,
} from "./machine";

/**
 * Property test for the Triple_HMI's in-progress indicator.
 *
 * **Feature: imts-triple-inspection-hmi, Property 7: In-progress indicator is accurate and non-destructive**
 *
 * **Validates: Requirements 3.4**
 *
 * The indicator is derived by the reused `logic/runs.ts` in-progress detection
 * on every `executions-polled` event, so the property is stated over the
 * triple reducer: for any prior app state (live or historical, with or
 * without loaded run content) and any polled executions payload, the
 * indicator is on in that single reducer step if and only if the payload
 * contains a `pending` or `running` run, and the indicator's value — however
 * it changes — never disturbs the displayed Run_Result or anything else the
 * kiosk shows.
 *
 * Generators model the station's real input space: one workflow running one
 * run at a time, so runs occupy a non-overlapping timeline, while the order
 * the LocalServer happens to return them in is arbitrary.
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

const TERMINAL_STATUSES: readonly ExecutionStatus[] = ["completed", "failed"];

function isTerminalStatus(status: ExecutionStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/**
 * Independent oracle for the indicator, written as Requirement 3.4 reads: a
 * run of the Target_Workflow is in progress exactly when its status is
 * `pending` or `running`.
 */
function expectedInProgress(executions: readonly Execution[]): boolean {
  return executions.some(
    (execution) => execution.status === "pending" || execution.status === "running",
  );
}

function poll(executions: readonly Execution[], atEpochMs: number): TripleEvent {
  return { type: "executions-polled", executions, atEpochMs };
}

/**
 * The state fields the indicator is allowed to touch: the flag itself, and
 * the retained payload it is derived from. Everything else — the displayed
 * Run_Result above all — must be identical whether or not in-progress runs
 * are present in the payload.
 */
function withoutIndicatorDimension(state: TripleAppState): unknown {
  return {
    ...state,
    live: { ...state.live, inProgress: false, latestExecutions: [] },
  };
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

interface RunPlan {
  status: ExecutionStatus;
  /** Idle seconds before the run started. */
  gap: number;
  /** Run duration in seconds. */
  duration: number;
  /** Arbitrary position in the LocalServer payload. */
  payloadOrder: number;
}

const runPlanArb: fc.Arbitrary<RunPlan> = fc.record({
  status: statusArb,
  gap: fc.integer({ min: 1, max: 120 }),
  duration: fc.integer({ min: 0, max: 120 }),
  payloadOrder: fc.nat(),
});

/**
 * Turns run plans into a non-overlapping timeline of executions of the bound
 * registration: unique ids, terminal runs carrying a finish time and
 * in-progress runs carrying none.
 */
function toTimeline(plans: readonly RunPlan[], idPrefix: string): Execution[] {
  let clock = 1_700_000_000;
  return plans.map((plan, index) => {
    const startedAt = clock + plan.gap;
    const finishedAt = startedAt + plan.duration;
    clock = finishedAt;
    return {
      executionId: `${idPrefix}-${index}`,
      registrationId: REGISTRATION_ID,
      status: plan.status,
      startedAt,
      finishedAt: isTerminalStatus(plan.status) ? finishedAt : null,
      failingNodeId: null,
      error: plan.status === "failed" ? "node bedrock_1 failed" : null,
      hasImageResults: isTerminalStatus(plan.status),
      captureId: `cap-${index}`,
    };
  });
}

/** The payload order the LocalServer happens to return. */
function inPayloadOrder(
  executions: readonly Execution[],
  plans: readonly RunPlan[],
): Execution[] {
  return executions
    .map((execution, index) => ({ execution, order: plans[index]?.payloadOrder ?? 0 }))
    .sort((a, b) => a.order - b.order)
    .map((entry) => entry.execution);
}

const timelineArb: fc.Arbitrary<Execution[]> = fc
  .array(runPlanArb, { maxLength: 10 })
  .map((plans) => inPayloadOrder(toTimeline(plans, "run"), plans));

/** How the prior app state was reached before the polled payload arrives. */
interface PriorPlan {
  screen: "login" | "app";
  /** How many of the timeline's runs the prior state has already seen. */
  seen: number;
  /** Whether the displayed run's `/results` + `/metadata` were applied. */
  loadData: boolean;
  /** Whether the Operator pinned a historical run. */
  historical: boolean;
}

const priorPlanArb: fc.Arbitrary<PriorPlan> = fc.record({
  screen: fc.constantFrom<"login" | "app">("login", "app"),
  seen: fc.nat(),
  loadData: fc.boolean(),
  historical: fc.boolean(),
});

/**
 * Builds a prior app state by folding real events through the reducer: the
 * bind, an earlier poll, that run's data, and optionally a history-tile
 * selection — so the property holds over states the poller can actually
 * produce, including historical mode with content already on screen.
 */
function buildPriorState(plan: PriorPlan, timeline: readonly Execution[]): TripleAppState {
  let state = reduce(initialTripleState(plan.screen, TARGET_NAME), {
    type: "registrations-loaded",
    registrations: [REGISTRATION],
  });

  const seen = timeline.slice(0, plan.seen % (timeline.length + 1));
  if (seen.length === 0) return state;

  state = reduce(state, poll(seen, 1_700_000_100_000));

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

/** In-progress runs of one poll cycle, started after every timeline run. */
function inProgressRuns(statuses: readonly ExecutionStatus[], cycle: number): Execution[] {
  return statuses.map((status, index) => ({
    executionId: `live-${cycle}-${index}`,
    registrationId: REGISTRATION_ID,
    status,
    startedAt: 1_700_009_000 + cycle * 10 + index,
    finishedAt: null,
    failingNodeId: null,
    error: null,
    hasImageResults: false,
    captureId: `cap-live-${cycle}-${index}`,
  }));
}

// --------------------------------------------------------------------------
// Property 7
// --------------------------------------------------------------------------

describe("Property 7: In-progress indicator is accurate and non-destructive", () => {
  it("is on in one reducer step exactly when the payload holds a pending or running run", () => {
    fc.assert(
      fc.property(priorPlanArb, timelineArb, timelineArb, (plan, priorRuns, payload) => {
        const state = buildPriorState(plan, priorRuns);
        const snapshot = structuredClone(state);

        const next = reduce(state, poll(payload, 1_700_000_200_000));

        // Accuracy, decided by the payload alone (3.4).
        expect(next.live.inProgress).toBe(expectedInProgress(payload));
        // Purity: the reducer never mutates the state it was handed.
        expect(state).toEqual(snapshot);
      }),
    );
  });

  it("leaves everything but the indicator untouched when only in-progress runs differ", () => {
    fc.assert(
      fc.property(priorPlanArb, timelineArb, timelineArb, (plan, priorRuns, payload) => {
        const state = buildPriorState(plan, priorRuns);
        const terminalOnly = payload.filter((execution) =>
          isTerminalStatus(execution.status),
        );
        const runningOnly = payload.filter(
          (execution) => !isTerminalStatus(execution.status),
        );

        const withPayload = reduce(state, poll(payload, 1_700_000_200_000));
        const withoutRunning = reduce(state, poll(terminalOnly, 1_700_000_200_000));

        // The presence or absence of in-progress runs changes the indicator
        // and the retained payload, and nothing else — the displayed
        // Run_Result, the history strip, the mode, and the connection
        // accounting are identical either way (3.4).
        expect(withoutIndicatorDimension(withPayload)).toEqual(
          withoutIndicatorDimension(withoutRunning),
        );
        expect(withPayload.live.inProgress).toBe(expectedInProgress(payload));
        expect(withoutRunning.live.inProgress).toBe(false);

        // A cycle that reports only in-progress runs keeps the very same
        // displayed Run_Result object: the indicator appears without
        // removing what is on screen.
        const onlyRunning = reduce(state, poll(runningOnly, 1_700_000_200_000));
        expect(onlyRunning.live.displayed).toBe(state.live.displayed);
        expect(onlyRunning.live.inProgress).toBe(expectedInProgress(runningOnly));
      }),
    );
  });

  it("keeps the displayed Run_Result across a sequence of indicator transitions", () => {
    fc.assert(
      fc.property(
        priorPlanArb,
        timelineArb,
        fc.array(runPlanArb, { minLength: 1, maxLength: 4 }),
        fc.array(fc.array(fc.constantFrom<ExecutionStatus>("pending", "running"), { maxLength: 2 }), {
          minLength: 2,
          maxLength: 8,
        }),
        (plan, priorRuns, terminalPlans, cycles) => {
          const terminal = toTimeline(terminalPlans, "settled").map((execution) =>
            isTerminalStatus(execution.status)
              ? execution
              : {
                  ...execution,
                  status: "completed" as ExecutionStatus,
                  finishedAt: execution.startedAt,
                  hasImageResults: true,
                },
          );

          // Settle on the terminal runs first, so any later change to the
          // displayed run would come from the indicator alone.
          let state = reduce(
            buildPriorState(plan, priorRuns),
            poll(terminal, 1_700_000_300_000),
          );
          const pinned = state.live.displayed;
          const history = state.live.history;

          let seenOn = false;
          let seenOff = false;
          cycles.forEach((statuses, cycle) => {
            const payload = [...terminal, ...inProgressRuns(statuses, cycle)];
            state = reduce(state, poll(payload, 1_700_000_400_000 + cycle));

            const on = expectedInProgress(payload);
            expect(state.live.inProgress).toBe(on);
            seenOn = seenOn || on;
            seenOff = seenOff || !on;

            // Whatever the indicator does, the displayed Run_Result and the
            // history strip are the same objects throughout (3.4).
            expect(state.live.displayed).toBe(pinned);
            expect(state.live.history).toBe(history);
          });

          // Both directions of the transition are non-destructive; whether a
          // given generated sequence exercises both is incidental.
          expect(seenOn || seenOff).toBe(true);
        },
      ),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("in-progress indicator examples", () => {
  function execution(
    overrides: Partial<Execution> & { executionId: string },
  ): Execution {
    return {
      registrationId: REGISTRATION_ID,
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

  const done = execution({ executionId: "done" });
  const running = execution({
    executionId: "next",
    status: "running",
    startedAt: 200,
    finishedAt: null,
    hasImageResults: false,
  });

  function bound(): TripleAppState {
    return reduce(initialTripleState("app", TARGET_NAME), {
      type: "registrations-loaded",
      registrations: [REGISTRATION],
    });
  }

  it("is off when no runs are reported", () => {
    expect(reduce(bound(), poll([], 1_000)).live.inProgress).toBe(false);
  });

  it("turns on without removing the displayed run", () => {
    const displaying = reduce(bound(), poll([done], 1_000));
    const next = reduce(displaying, poll([done, running], 3_000));
    expect(next.live.inProgress).toBe(true);
    expect(next.live.displayed).toBe(displaying.live.displayed);
  });

  it("turns off on the next cycle that reports no in-progress run", () => {
    const displaying = reduce(bound(), poll([done], 1_000));
    const busy = reduce(displaying, poll([done, running], 3_000));
    const idle = reduce(busy, poll([done], 5_000));
    expect(idle.live.inProgress).toBe(false);
    expect(idle.live.displayed).toBe(busy.live.displayed);
  });
});
