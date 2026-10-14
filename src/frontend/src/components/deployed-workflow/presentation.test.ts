/*
 * Unit tests for the deployed-workflow pure presentation logic (task 5).
 *
 * **Feature: edge-workflow-run-ux, Property 1: Deployed workflow registrations
 * are visible and runnable from the UI**
 * **Validates: Requirements 2.2, 2.3, 2.4**
 */

import {
  WorkflowExecution,
  WorkflowRegistration,
} from "api/WorkflowRegistrationAPI";
import {
  canTrigger,
  canViewResults,
  executionFailureDetails,
  executionStatusIndicator,
  hasStarted,
  isExecutionActive,
  registrationStatusIndicator,
  shouldPoll,
  sortExecutions,
} from "./presentation";

function registration(
  overrides: Partial<WorkflowRegistration> = {},
): WorkflowRegistration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    version: "3",
    arch: "arm64",
    artifactPath: "/greengrass/artifacts/wf-1/3",
    status: "registered",
    registeredAt: 1700000000,
    ...overrides,
  };
}

function execution(
  overrides: Partial<WorkflowExecution> = {},
): WorkflowExecution {
  return {
    executionId: "exec-1",
    registrationId: "reg-1",
    status: "completed",
    startedAt: 1700000100,
    finishedAt: 1700000200,
    failingNodeId: null,
    error: null,
    hasImageResults: false,
    captureId: null,
    outputDir: null,
    ...overrides,
  };
}

describe("canTrigger (2.2, 2.4)", () => {
  it("is true for a registered registration", () => {
    expect(canTrigger(registration({ status: "registered" }))).toBe(true);
  });

  it("is false for an invalid registration", () => {
    expect(
      canTrigger(
        registration({ status: "invalid", invalidReason: "bad artifact" }),
      ),
    ).toBe(false);
  });
});

describe("registrationStatusIndicator (2.1, 2.4)", () => {
  it("maps registered to a success indicator", () => {
    expect(
      registrationStatusIndicator(registration({ status: "registered" })),
    ).toEqual({ type: "success", text: "Registered" });
  });

  it("maps invalid to an error indicator", () => {
    expect(
      registrationStatusIndicator(registration({ status: "invalid" })),
    ).toEqual({ type: "error", text: "Invalid" });
  });
});

describe("executionStatusIndicator (2.3)", () => {
  it("maps all four execution statuses", () => {
    expect(executionStatusIndicator(execution({ status: "pending" }))).toEqual(
      { type: "pending", text: "Pending" },
    );
    expect(executionStatusIndicator(execution({ status: "running" }))).toEqual(
      { type: "in-progress", text: "Running" },
    );
    expect(
      executionStatusIndicator(execution({ status: "completed" })),
    ).toEqual({ type: "success", text: "Completed" });
    expect(executionStatusIndicator(execution({ status: "failed" }))).toEqual({
      type: "error",
      text: "Failed",
    });
  });
});

describe("executionFailureDetails (2.3)", () => {
  it("returns the failing node and error for a failed execution", () => {
    expect(
      executionFailureDetails(
        execution({
          status: "failed",
          failingNodeId: "node-7",
          error: "Sensor read timeout",
        }),
      ),
    ).toEqual({ failingNodeId: "node-7", error: "Sensor read timeout" });
  });

  it("omits null failure fields for a failed execution", () => {
    expect(
      executionFailureDetails(
        execution({ status: "failed", failingNodeId: null, error: null }),
      ),
    ).toEqual({});
  });

  it("is undefined for every non-failed status", () => {
    for (const status of ["pending", "running", "completed"] as const) {
      expect(
        executionFailureDetails(
          execution({ status, failingNodeId: "node-7", error: "boom" }),
        ),
      ).toBeUndefined();
    }
  });
});

describe("isExecutionActive / shouldPoll (2.3)", () => {
  it("treats pending and running as active, terminal statuses as inactive", () => {
    expect(isExecutionActive(execution({ status: "pending" }))).toBe(true);
    expect(isExecutionActive(execution({ status: "running" }))).toBe(true);
    expect(isExecutionActive(execution({ status: "completed" }))).toBe(false);
    expect(isExecutionActive(execution({ status: "failed" }))).toBe(false);
  });

  it("shouldPoll is true when any execution is active", () => {
    expect(
      shouldPoll([
        execution({ status: "completed" }),
        execution({ status: "running" }),
      ]),
    ).toBe(true);
    expect(
      shouldPoll([
        execution({ status: "failed" }),
        execution({ status: "pending" }),
      ]),
    ).toBe(true);
  });

  it("shouldPoll is false for an empty list or all-terminal executions", () => {
    expect(shouldPoll([])).toBe(false);
    expect(
      shouldPoll([
        execution({ status: "completed" }),
        execution({ status: "failed" }),
      ]),
    ).toBe(false);
  });
});

describe("canViewResults (5.1, 5.2)", () => {
  it("is true only for a completed execution with image results", () => {
    expect(
      canViewResults(
        execution({ status: "completed", hasImageResults: true }),
      ),
    ).toBe(true);
  });

  it("is false for a completed execution without image results", () => {
    expect(
      canViewResults(
        execution({ status: "completed", hasImageResults: false }),
      ),
    ).toBe(false);
  });

  it("is false for a non-completed execution even with image results", () => {
    for (const status of ["pending", "running", "failed"] as const) {
      expect(
        canViewResults(execution({ status, hasImageResults: true })),
      ).toBe(false);
    }
  });
});

describe("hasStarted (6.1)", () => {
  it("is false for a pending execution that has not started", () => {
    expect(
      hasStarted(
        execution({ status: "pending", startedAt: null, finishedAt: null }),
      ),
    ).toBe(false);
  });

  it("is true for running and terminal executions", () => {
    for (const status of ["running", "completed", "failed"] as const) {
      expect(hasStarted(execution({ status }))).toBe(true);
    }
  });

  it("is true when startedAt is set even if status is still pending", () => {
    expect(
      hasStarted(execution({ status: "pending", startedAt: 1700000100 })),
    ).toBe(true);
  });
});

describe("sortExecutions (2.3)", () => {
  it("orders executions newest first by startedAt", () => {
    const oldest = execution({ executionId: "exec-old", startedAt: 100 });
    const newest = execution({ executionId: "exec-new", startedAt: 300 });
    const middle = execution({ executionId: "exec-mid", startedAt: 200 });

    expect(
      sortExecutions([oldest, newest, middle]).map((e) => e.executionId),
    ).toEqual(["exec-new", "exec-mid", "exec-old"]);
  });

  it("sorts null startedAt (not yet started) before started executions", () => {
    const started = execution({ executionId: "exec-started", startedAt: 100 });
    const notStarted = execution({
      executionId: "exec-pending",
      status: "pending",
      startedAt: null,
      finishedAt: null,
    });

    expect(
      sortExecutions([started, notStarted]).map((e) => e.executionId),
    ).toEqual(["exec-pending", "exec-started"]);
  });

  it("is stable for ties (equal startedAt keeps input order)", () => {
    const first = execution({ executionId: "exec-a", startedAt: 100 });
    const second = execution({ executionId: "exec-b", startedAt: 100 });

    expect(sortExecutions([first, second]).map((e) => e.executionId)).toEqual([
      "exec-a",
      "exec-b",
    ]);
  });

  it("does not mutate the input array", () => {
    const input = [
      execution({ executionId: "exec-old", startedAt: 100 }),
      execution({ executionId: "exec-new", startedAt: 200 }),
    ];
    const inputOrder = input.map((e) => e.executionId);

    sortExecutions(input);

    expect(input.map((e) => e.executionId)).toEqual(inputOrder);
  });
});
