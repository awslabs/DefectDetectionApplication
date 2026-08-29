import { describe, expect, it } from "vitest";

import type { Execution, Registration } from "../api/types";
import {
  activeRegistrations,
  checkDisplayedAvailability,
  isActiveRegistration,
  registrationLabel,
  selectDefaultRegistration,
} from "./selection";

/**
 * Unit tests for registration filtering, labeling, default selection, and
 * availability (Requirements 2.2, 2.4, 2.5, 2.6, 2.7, 8.5). Property 4 and
 * Property 5 coverage lives in the separate property-test tasks.
 */

function reg(overrides: Partial<Registration> = {}): Registration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-1",
    name: "Workflow One",
    version: "1.0.0",
    status: "registered",
    registeredAt: 1000,
    ...overrides,
  };
}

function run(overrides: Partial<Execution> = {}): Execution {
  return {
    executionId: "exec-1",
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

describe("isActiveRegistration / activeRegistrations", () => {
  it("treats only the registered status as active", () => {
    expect(isActiveRegistration(reg({ status: "registered" }))).toBe(true);
    expect(isActiveRegistration(reg({ status: "invalid" }))).toBe(false);
    expect(isActiveRegistration(reg({ status: "retired" }))).toBe(false);
    expect(isActiveRegistration(reg({ status: "" }))).toBe(false);
  });

  it("filters to active registrations preserving API order", () => {
    const a = reg({ registrationId: "a" });
    const b = reg({ registrationId: "b", status: "invalid" });
    const c = reg({ registrationId: "c" });
    expect(activeRegistrations([a, b, c])).toEqual([a, c]);
  });

  it("returns an empty list when nothing is active", () => {
    expect(activeRegistrations([reg({ status: "invalid" })])).toEqual([]);
  });
});

describe("registrationLabel", () => {
  it("uses the name when present and non-empty", () => {
    expect(registrationLabel(reg({ name: "IMTS - Swagfactory" }))).toBe(
      "IMTS - Swagfactory",
    );
  });

  it("falls back to workflowId when the name is null", () => {
    expect(registrationLabel(reg({ name: null, workflowId: "wf-9" }))).toBe(
      "wf-9",
    );
  });

  it("falls back to workflowId when the name is empty", () => {
    expect(registrationLabel(reg({ name: "", workflowId: "wf-9" }))).toBe(
      "wf-9",
    );
  });
});

describe("selectDefaultRegistration", () => {
  const regA = reg({ registrationId: "a" });
  const regB = reg({ registrationId: "b" });
  const regC = reg({ registrationId: "c" });

  it("selects the registration whose most recent run started latest", () => {
    const runs = new Map([
      ["a", [run({ startedAt: 100 }), run({ startedAt: 300 })]],
      ["b", [run({ startedAt: 200 })]],
    ]);
    expect(selectDefaultRegistration([regA, regB], runs)?.registrationId).toBe(
      "a",
    );
  });

  it("compares registrations by their own most recent run", () => {
    const runs = new Map([
      ["a", [run({ startedAt: 100 })]],
      ["b", [run({ startedAt: 50 }), run({ startedAt: 400 })]],
    ]);
    expect(selectDefaultRegistration([regA, regB], runs)?.registrationId).toBe(
      "b",
    );
  });

  it("keeps the first registration in API order on a startedAt tie", () => {
    const runs = new Map([
      ["a", [run({ startedAt: 100 })]],
      ["b", [run({ startedAt: 100 })]],
    ]);
    expect(selectDefaultRegistration([regA, regB], runs)?.registrationId).toBe(
      "a",
    );
  });

  it("falls back to the first active registration when no runs exist", () => {
    const runs = new Map<string, Execution[]>();
    expect(
      selectDefaultRegistration([regA, regB, regC], runs)?.registrationId,
    ).toBe("a");
  });

  it("ignores non-active registrations even when they have the latest run", () => {
    const invalid = reg({ registrationId: "x", status: "invalid" });
    const runs = new Map([
      ["x", [run({ startedAt: 999 })]],
      ["b", [run({ startedAt: 10 })]],
    ]);
    expect(
      selectDefaultRegistration([invalid, regA, regB], runs)?.registrationId,
    ).toBe("b");
  });

  it("skips run-less actives ahead of one with runs", () => {
    const runs = new Map([["b", [run({ startedAt: 5 })]]]);
    expect(selectDefaultRegistration([regA, regB], runs)?.registrationId).toBe(
      "b",
    );
  });

  it("returns null when there are zero active registrations", () => {
    expect(
      selectDefaultRegistration([reg({ status: "invalid" })], new Map()),
    ).toBeNull();
    expect(selectDefaultRegistration([], new Map())).toBeNull();
  });
});

describe("checkDisplayedAvailability", () => {
  const regA = reg({ registrationId: "a" });
  const regB = reg({ registrationId: "b" });

  it("reports available when the displayed registration is active", () => {
    expect(checkDisplayedAvailability([regA, regB], "a")).toEqual({
      kind: "available",
      registration: regA,
    });
  });

  it("reports unavailable with remaining actives when absent", () => {
    expect(checkDisplayedAvailability([regB], "a")).toEqual({
      kind: "unavailable",
      alternatives: [regB],
    });
  });

  it("reports unavailable when the displayed registration went non-active", () => {
    const retiredA = reg({ registrationId: "a", status: "retired" });
    expect(checkDisplayedAvailability([retiredA, regB], "a")).toEqual({
      kind: "unavailable",
      alternatives: [regB],
    });
  });

  it("reports no-workflows when zero actives remain", () => {
    expect(checkDisplayedAvailability([], "a")).toEqual({
      kind: "no-workflows",
    });
    expect(
      checkDisplayedAvailability([reg({ status: "invalid" })], "a"),
    ).toEqual({ kind: "no-workflows" });
  });
});
