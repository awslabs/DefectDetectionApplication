import { describe, expect, it } from "vitest";

import type { Execution } from "../api/types";
import {
  buildHistory,
  HISTORY_CAPACITY,
  insertTerminalRun,
  type HistoryEntry,
  type VerdictResolver,
} from "./history";
import type { VerdictState } from "./verdict";

/**
 * Unit tests for history strip logic (Requirements 7.1, 7.2, 7.6, 7.7):
 * initial population newest first with capacity 10, newest-position insert,
 * exact-oldest eviction on overflow, verdict + start time on each entry,
 * fewer-than-capacity and zero-run cases. Property 12 coverage lives in the
 * separate property-test task.
 */

function execution(overrides: Partial<Execution> & { executionId: string }): Execution {
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

/** Resolver used across tests: failed → failed-run, completed → pass. */
const verdictFor: VerdictResolver = (e) =>
  e.status === "failed" ? "failed-run" : "pass";

describe("buildHistory", () => {
  it("returns an empty history for zero runs", () => {
    expect(buildHistory([], verdictFor)).toEqual([]);
  });

  it("orders entries newest first regardless of input order", () => {
    const runs = [
      execution({ executionId: "mid", startedAt: 10, finishedAt: 20 }),
      execution({ executionId: "newest", startedAt: 30, finishedAt: 40 }),
      execution({ executionId: "oldest", startedAt: 1, finishedAt: 5 }),
    ];
    const history = buildHistory(runs, verdictFor);
    expect(history.map((h) => h.executionId)).toEqual(["newest", "mid", "oldest"]);
  });

  it("includes only terminal runs", () => {
    const runs = [
      execution({ executionId: "done", startedAt: 10, finishedAt: 20 }),
      execution({ executionId: "pending", status: "pending", startedAt: 50 }),
      execution({ executionId: "running", status: "running", startedAt: 60 }),
      execution({ executionId: "failed", status: "failed", startedAt: 5, finishedAt: 8 }),
    ];
    const history = buildHistory(runs, verdictFor);
    expect(history.map((h) => h.executionId)).toEqual(["done", "failed"]);
  });

  it("keeps entries only for the runs that exist when fewer than capacity", () => {
    const runs = [
      execution({ executionId: "a", startedAt: 2, finishedAt: 3 }),
      execution({ executionId: "b", startedAt: 4, finishedAt: 5 }),
    ];
    expect(buildHistory(runs, verdictFor)).toHaveLength(2);
  });

  it("caps the history at the capacity, keeping the newest runs", () => {
    const runs = Array.from({ length: HISTORY_CAPACITY + 5 }, (_, i) =>
      execution({ executionId: `run-${i}`, startedAt: i, finishedAt: i + 1 }),
    );
    const history = buildHistory(runs, verdictFor);
    expect(history).toHaveLength(HISTORY_CAPACITY);
    // Newest run first; the 5 oldest runs are excluded.
    expect(history[0]?.executionId).toBe(`run-${HISTORY_CAPACITY + 4}`);
    expect(history[HISTORY_CAPACITY - 1]?.executionId).toBe("run-5");
  });

  it("carries each run's verdict state and start time", () => {
    const runs = [
      execution({ executionId: "ok", startedAt: 100, finishedAt: 110 }),
      execution({ executionId: "boom", status: "failed", startedAt: 50, finishedAt: 60 }),
    ];
    expect(buildHistory(runs, verdictFor)).toEqual([
      { executionId: "ok", verdict: "pass", startedAt: 100 },
      { executionId: "boom", verdict: "failed-run", startedAt: 50 },
    ]);
  });

  it("resolves verdicts through the supplied resolver", () => {
    const noVerdict: VerdictResolver = () => "no-verdict" satisfies VerdictState;
    const runs = [execution({ executionId: "a", startedAt: 1, finishedAt: 2 })];
    expect(buildHistory(runs, noVerdict)[0]?.verdict).toBe("no-verdict");
  });
});

describe("insertTerminalRun", () => {
  const entry = (id: string, startedAt: number): HistoryEntry => ({
    executionId: id,
    verdict: "pass",
    startedAt,
  });

  it("inserts a new terminal run at the newest position", () => {
    const history = [entry("b", 20), entry("a", 10)];
    const next = insertTerminalRun(
      history,
      execution({ executionId: "c", startedAt: 30, finishedAt: 40 }),
      verdictFor,
    );
    expect(next.map((h) => h.executionId)).toEqual(["c", "b", "a"]);
  });

  it("evicts exactly the oldest entry on overflow", () => {
    const history = Array.from({ length: HISTORY_CAPACITY }, (_, i) =>
      entry(`run-${HISTORY_CAPACITY - 1 - i}`, HISTORY_CAPACITY - 1 - i),
    );
    const next = insertTerminalRun(
      history,
      execution({ executionId: "new", startedAt: 100, finishedAt: 110 }),
      verdictFor,
    );
    expect(next).toHaveLength(HISTORY_CAPACITY);
    expect(next[0]?.executionId).toBe("new");
    // Only the oldest entry (run-0) is gone; everything else is intact.
    expect(next.slice(1)).toEqual(history.slice(0, HISTORY_CAPACITY - 1));
  });

  it("does not evict when below capacity", () => {
    const history = [entry("a", 10)];
    const next = insertTerminalRun(
      history,
      execution({ executionId: "b", startedAt: 20, finishedAt: 30 }),
      verdictFor,
    );
    expect(next).toHaveLength(2);
  });

  it("ignores non-terminal runs", () => {
    const history = [entry("a", 10)];
    const next = insertTerminalRun(
      history,
      execution({ executionId: "b", status: "running", startedAt: 20 }),
      verdictFor,
    );
    expect(next).toBe(history);
  });

  it("is idempotent for a run already in the history", () => {
    const history = [entry("a", 10)];
    const next = insertTerminalRun(
      history,
      execution({ executionId: "a", startedAt: 10, finishedAt: 20 }),
      verdictFor,
    );
    expect(next).toBe(history);
  });

  it("carries the inserted run's verdict state and start time", () => {
    const next = insertTerminalRun(
      [],
      execution({ executionId: "boom", status: "failed", startedAt: 7, finishedAt: 9 }),
      verdictFor,
    );
    expect(next).toEqual([{ executionId: "boom", verdict: "failed-run", startedAt: 7 }]);
  });

  it("does not mutate the input history", () => {
    const history = [entry("a", 10)];
    const snapshot = [...history];
    insertTerminalRun(
      history,
      execution({ executionId: "b", startedAt: 20, finishedAt: 30 }),
      verdictFor,
    );
    expect(history).toEqual(snapshot);
  });
});
