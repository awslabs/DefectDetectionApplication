import { describe, expect, it } from "vitest";

import type { Execution, ExecutionStatus } from "../api/types";
import {
  compareTerminalRunsDesc,
  hasInProgressRun,
  isInProgress,
  isTerminal,
  latestTerminalRun,
} from "./runs";

/**
 * Unit tests for run ordering logic (Requirements 3.2, 3.4, 3.7):
 * terminal-run comparator, latest-terminal selection, in-progress detection.
 * Property 6/7 coverage lives in the separate property-test tasks.
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

describe("isTerminal / isInProgress", () => {
  it.each<[ExecutionStatus, boolean, boolean]>([
    ["completed", true, false],
    ["failed", true, false],
    ["pending", false, true],
    ["running", false, true],
  ])("classifies %s", (status, terminal, inProgress) => {
    const e = execution({ executionId: "e", status });
    expect(isTerminal(e)).toBe(terminal);
    expect(isInProgress(e)).toBe(inProgress);
  });
});

describe("compareTerminalRunsDesc", () => {
  it("orders by finishedAt descending when both are present and differ", () => {
    const older = execution({ executionId: "a", startedAt: 100, finishedAt: 110 });
    const newer = execution({ executionId: "b", startedAt: 90, finishedAt: 120 });
    expect(compareTerminalRunsDesc(newer, older)).toBeLessThan(0);
    expect(compareTerminalRunsDesc(older, newer)).toBeGreaterThan(0);
  });

  it("falls back to startedAt when finishedAt values are equal", () => {
    const older = execution({ executionId: "a", startedAt: 100, finishedAt: 120 });
    const newer = execution({ executionId: "b", startedAt: 105, finishedAt: 120 });
    expect(compareTerminalRunsDesc(newer, older)).toBeLessThan(0);
  });

  it("falls back to startedAt when either finishedAt is absent", () => {
    const withFinished = execution({ executionId: "a", startedAt: 100, finishedAt: 200 });
    const withoutFinished = execution({ executionId: "b", startedAt: 150, finishedAt: null });
    // b started later; finishedAt cannot be compared, so startedAt decides.
    expect(compareTerminalRunsDesc(withoutFinished, withFinished)).toBeLessThan(0);
    expect(compareTerminalRunsDesc(withFinished, withoutFinished)).toBeGreaterThan(0);
  });

  it("returns 0 when both keys are indistinguishable", () => {
    const a = execution({ executionId: "a", startedAt: 100, finishedAt: 120 });
    const b = execution({ executionId: "b", startedAt: 100, finishedAt: 120 });
    expect(compareTerminalRunsDesc(a, b)).toBe(0);
  });

  it("sorts a list newest-first", () => {
    const runs = [
      execution({ executionId: "mid", startedAt: 10, finishedAt: 20 }),
      execution({ executionId: "newest", startedAt: 30, finishedAt: 40 }),
      execution({ executionId: "oldest", startedAt: 1, finishedAt: 5 }),
    ];
    const sorted = [...runs].sort(compareTerminalRunsDesc);
    expect(sorted.map((e) => e.executionId)).toEqual(["newest", "mid", "oldest"]);
  });
});

describe("latestTerminalRun", () => {
  it("returns null for an empty list", () => {
    expect(latestTerminalRun([])).toBeNull();
  });

  it("returns null when no terminal run exists", () => {
    const runs = [
      execution({ executionId: "a", status: "pending" }),
      execution({ executionId: "b", status: "running" }),
    ];
    expect(latestTerminalRun(runs)).toBeNull();
  });

  it("picks the terminal run with the most recent finishedAt", () => {
    const runs = [
      execution({ executionId: "old", startedAt: 10, finishedAt: 20 }),
      execution({ executionId: "new", status: "failed", startedAt: 15, finishedAt: 30 }),
      execution({ executionId: "running", status: "running", startedAt: 99 }),
    ];
    expect(latestTerminalRun(runs)?.executionId).toBe("new");
  });

  it("ignores in-progress runs even when they started later", () => {
    const runs = [
      execution({ executionId: "running", status: "running", startedAt: 500 }),
      execution({ executionId: "done", startedAt: 100, finishedAt: 110 }),
    ];
    expect(latestTerminalRun(runs)?.executionId).toBe("done");
  });

  it("uses startedAt as the key when finishedAt values tie", () => {
    const runs = [
      execution({ executionId: "earlier", startedAt: 100, finishedAt: 200 }),
      execution({ executionId: "later", startedAt: 150, finishedAt: 200 }),
    ];
    expect(latestTerminalRun(runs)?.executionId).toBe("later");
  });

  it("resolves full ties to the earliest list entry", () => {
    const runs = [
      execution({ executionId: "first", startedAt: 100, finishedAt: 200 }),
      execution({ executionId: "second", startedAt: 100, finishedAt: 200 }),
    ];
    expect(latestTerminalRun(runs)?.executionId).toBe("first");
  });
});

describe("hasInProgressRun", () => {
  it("is false for an empty list", () => {
    expect(hasInProgressRun([])).toBe(false);
  });

  it("is false when all runs are terminal", () => {
    const runs = [
      execution({ executionId: "a", status: "completed", finishedAt: 10 }),
      execution({ executionId: "b", status: "failed", finishedAt: 20 }),
    ];
    expect(hasInProgressRun(runs)).toBe(false);
  });

  it.each<ExecutionStatus>(["pending", "running"])(
    "is true when a %s run exists among terminal runs",
    (status) => {
      const runs = [
        execution({ executionId: "a", status: "completed", finishedAt: 10 }),
        execution({ executionId: "b", status }),
      ];
      expect(hasInProgressRun(runs)).toBe(true);
    },
  );
});
