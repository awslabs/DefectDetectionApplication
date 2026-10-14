import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Execution, ExecutionStatus, ResultImage } from "../api/types";
import { initialTripleState, reduce, type TripleAppState } from "./machine";
import type { VerdictMetadata } from "./verdicts";

/**
 * Property test for the Triple_HMI's displayed-run selection.
 *
 * **Feature: imts-triple-inspection-hmi, Property 6: Displayed run is the maximal terminal run**
 *
 * **Validates: Requirements 3.2, 3.3, 3.5, 3.7**
 *
 * The reused `logic/runs.ts` ordering already has its own unit coverage; this
 * property pins the *reducer's* behavior when polled execution lists are
 * folded through it: after a single `executions-polled` step the displayed run
 * is the payload's maximal terminal run (3.2, 3.3, 3.5), a payload with no
 * terminal run leaves the displayed content untouched — the placeholder state
 * when nothing was displayed yet (3.7) — and switching runs never carries the
 * previous run's content across.
 *
 * Generators model the station's real input space: one workflow running one
 * run at a time, so runs occupy a non-overlapping timeline; the LocalServer
 * returns a bounded window (`limit=10`) of it in an arbitrary order; and the
 * displayed run's `/results` + `/metadata` payloads may land between cycles.
 *
 * Because the timeline never overlaps, both ordering keys increase with the
 * timeline index, which gives an implementation-independent oracle for "most
 * recent terminal run": the terminal run latest in the timeline.
 */

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

const EXECUTIONS_LIMIT = 10;

const TERMINAL_STATUSES: readonly ExecutionStatus[] = ["completed", "failed"];

function isTerminalStatus(status: ExecutionStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

function requireDefined<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("expected a defined value");
  return value;
}

/** A run's position in the generated timeline, keyed by execution id. */
type TimelineIndex = ReadonlyMap<string, number>;

/**
 * Independent oracle: the payload's most recent terminal run is the terminal
 * run furthest along the non-overlapping timeline. Null when the payload holds
 * no terminal run at all.
 */
function expectedDisplayed(
  payload: readonly Execution[],
  index: TimelineIndex,
): Execution | null {
  let best: Execution | null = null;
  for (const execution of payload) {
    if (!isTerminalStatus(execution.status)) continue;
    if (
      best === null ||
      requireDefined(index.get(execution.executionId)) >
        requireDefined(index.get(best.executionId))
    ) {
      best = execution;
    }
  }
  return best;
}

function displayedId(state: TripleAppState): string | null {
  return state.live.displayed?.execution.executionId ?? null;
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

interface RunSeed {
  execution: Execution;
  /** Arbitrary position within the LocalServer's response. */
  payloadOrder: number;
}

/**
 * A non-overlapping timeline of one workflow's runs: each run starts after the
 * previous one finished, ids are unique, and in-progress runs carry no finish
 * time. Terminal runs usually carry one; occasionally one is missing, covering
 * the Requirement 3.3 "`finishedAt` absent" ordering case.
 *
 * The timeline is longer than the poll window so the reducer sees runs entering
 * and leaving the payload.
 */
const timelineArb: fc.Arbitrary<RunSeed[]> = fc
  .array(
    fc.record({
      status: statusArb,
      gap: fc.integer({ min: 1, max: 90 }),
      duration: fc.integer({ min: 0, max: 90 }),
      finishRecorded: fc.boolean(),
      payloadOrder: fc.nat(),
    }),
    { minLength: 1, maxLength: EXECUTIONS_LIMIT + 5 },
  )
  .map((raw) => {
    let clock = 1_700_000_000;
    return raw.map((entry, index) => {
      const startedAt = clock + entry.gap;
      const finishedAt = startedAt + entry.duration;
      clock = finishedAt;
      const terminal = isTerminalStatus(entry.status);
      const execution: Execution = {
        executionId: `run-${index}`,
        registrationId: "reg-1",
        status: entry.status,
        // Terminal runs normally report a finish time; the rare missing one
        // exercises the startedAt ordering key (3.3).
        finishedAt: terminal && entry.finishRecorded ? finishedAt : null,
        startedAt,
        failingNodeId: null,
        error: entry.status === "failed" ? "node failed" : null,
        hasImageResults: terminal,
        captureId: terminal ? `cap-${index}` : null,
      };
      return { execution, payloadOrder: entry.payloadOrder };
    });
  });

/** One poll cycle: how much of the timeline exists, and whether data loads. */
interface CycleSeed {
  /** Additional runs visible to the LocalServer since the previous cycle. */
  revealed: number;
  loadData: boolean;
  images: ResultImage[] | null;
  isAnomalous: boolean | undefined;
}

const nodeImagesArb: fc.Arbitrary<ResultImage[]> = fc
  .subarray(["bedrock_1", "bedrock_2", "bedrock_3", "bedrock_4"], { minLength: 1 })
  .map((nodeIds) =>
    nodeIds.flatMap((nodeId) =>
      (["annotated", "original"] as const).map((port) => ({
        kind: "node" as const,
        nodeId,
        port,
        hasOverlay: false,
      })),
    ),
  );

const cyclesArb: fc.Arbitrary<CycleSeed[]> = fc.array(
  fc.record({
    revealed: fc.nat({ max: 3 }),
    loadData: fc.boolean(),
    images: fc.option(nodeImagesArb, { nil: null }),
    isAnomalous: fc.option(fc.boolean(), { nil: undefined }),
  }),
  { minLength: 1, maxLength: 12 },
);

/** The bounded newest-first window the executions route returns, reordered. */
function payloadFor(seeds: readonly RunSeed[], visible: number): Execution[] {
  const window = seeds.slice(Math.max(0, visible - EXECUTIONS_LIMIT), visible);
  return [...window]
    .sort((a, b) => a.payloadOrder - b.payloadOrder)
    .map((seed) => seed.execution);
}

function metadataFor(cycle: CycleSeed, images: ResultImage[] | null): VerdictMetadata {
  const bedrock: Record<string, unknown> = {};
  for (const image of images ?? []) {
    if (image.nodeId !== undefined) {
      bedrock[image.nodeId] = { is_anomalous: true, confidence: 0.5 };
    }
  }
  const metadata: Record<string, unknown> = { bedrock };
  if (cycle.isAnomalous !== undefined) metadata.is_anomalous = cycle.isAnomalous;
  return metadata;
}

// --------------------------------------------------------------------------
// Property 6
// --------------------------------------------------------------------------

describe("Property 6: Displayed run is the maximal terminal run", () => {
  it("displays the payload's maximal terminal run after every poll cycle", () => {
    fc.assert(
      fc.property(timelineArb, cyclesArb, (seeds, cycles) => {
        const index: TimelineIndex = new Map(
          seeds.map((seed, position) => [seed.execution.executionId, position]),
        );

        let state = initialTripleState("app");
        let visible = 0;
        let clock = 1_700_000_500;

        for (const cycle of cycles) {
          visible = Math.min(seeds.length, visible + cycle.revealed);
          const payload = payloadFor(seeds, visible);
          clock += 2_000;

          const before = state;
          const snapshot = structuredClone(before);
          state = reduce(before, {
            type: "executions-polled",
            executions: payload,
            atEpochMs: clock,
          });

          // A reducer step never mutates the state it was given.
          expect(before).toEqual(snapshot);

          const expected = expectedDisplayed(payload, index);

          if (expected === null) {
            // No terminal run in the payload: the displayed content is
            // retained exactly — the no-terminal-runs placeholder state while
            // nothing has been displayed yet (3.7).
            expect(state.live.displayed).toBe(before.live.displayed);
          } else {
            // One step from the payload to the run on screen (3.2, 3.3, 3.5).
            expect(displayedId(state)).toBe(expected.executionId);
            expect(state.live.displayed?.execution).toEqual(expected);

            if (displayedId(before) === expected.executionId) {
              // Still the maximal run: its already-loaded content stands.
              expect(state.live.displayed).toBe(before.live.displayed);
            } else {
              // A different run took over: nothing of the previous run's
              // content comes with it.
              const displayed = requireDefined(state.live.displayed ?? undefined);
              for (const slot of displayed.slots) {
                expect(slot.inspection).toBeUndefined();
                expect(slot.verdict).toBeUndefined();
              }
              expect(displayed.runLevelVerdict).toBeUndefined();
            }
          }

          // The displayed run's data may land before the next cycle, so later
          // cycles are judged against a state holding real run content.
          const current = state.live.displayed;
          if (cycle.loadData && current !== null) {
            state = reduce(state, {
              type: "run-data-loaded",
              executionId: current.execution.executionId,
              images: cycle.images,
              metadata: metadataFor(cycle, cycle.images),
            });
            expect(displayedId(state)).toBe(current.execution.executionId);
          }
        }
      }),
    );
  });
});

// --------------------------------------------------------------------------
// Examples
// --------------------------------------------------------------------------

describe("displayed-run examples", () => {
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

  function polled(state: TripleAppState, executions: Execution[]): TripleAppState {
    return reduce(state, { type: "executions-polled", executions, atEpochMs: 1 });
  }

  it("shows the most recent terminal run on the first Live_View (3.5)", () => {
    const state = polled(initialTripleState("app"), [
      execution({ executionId: "newer", startedAt: 30, finishedAt: 40 }),
      execution({ executionId: "older", startedAt: 10, finishedAt: 20 }),
      execution({ executionId: "running", status: "running", startedAt: 50 }),
    ]);
    expect(state.live.displayed?.execution.executionId).toBe("newer");
  });

  it("keeps the placeholder state when no terminal run exists (3.7)", () => {
    const state = polled(initialTripleState("app"), [
      execution({ executionId: "pending", status: "pending", startedAt: 10 }),
    ]);
    expect(state.live.displayed).toBeNull();
  });

  it("switches to a newer terminal run in one poll cycle (3.2)", () => {
    const first = polled(initialTripleState("app"), [
      execution({ executionId: "first", startedAt: 10, finishedAt: 20 }),
    ]);
    const second = polled(first, [
      execution({ executionId: "second", startedAt: 30, finishedAt: 40 }),
      execution({ executionId: "first", startedAt: 10, finishedAt: 20 }),
    ]);
    expect(second.live.displayed?.execution.executionId).toBe("second");
  });
});
