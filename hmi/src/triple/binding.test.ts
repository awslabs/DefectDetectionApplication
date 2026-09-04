import fc from "fast-check";
import { describe, expect, it } from "vitest";

import type { Registration } from "../api/types";
import { ACTIVE_STATUS } from "../logic/selection";
import { bindTargetWorkflow, targetCandidates } from "./binding";

/**
 * Property test for the Triple_HMI's Target_Workflow binding.
 *
 * **Feature: imts-triple-inspection-hmi, Property 4: Target-workflow binding is a deterministic pure function**
 *
 * **Validates: Requirements 2.2, 2.3, 2.4, 2.7, 8.5, 8.8**
 *
 * The binding is re-evaluated on every registrations payload, so all of the
 * deploy / undeploy / redeploy transitions reduce to this one function: the
 * first bind (2.2, 2.3), the not-deployed message (2.4), the return to it
 * when the bound registration goes inactive or absent (2.7, 8.5), and the
 * automatic re-bind when a match reappears (8.8) are the same evaluation on
 * different payloads.
 */

/** Names the payload draws from, so matches and near-misses both occur. */
const NAMES = [
  "blue-plate-detection-guided-inspection",
  "Blue-Plate-Detection-Guided-Inspection",
  "BLUE-PLATE-DETECTION-GUIDED-INSPECTION",
  "blue-plate-detection-guided-inspection ",
  " blue-plate-detection-guided-inspection",
  "blue-plate-detection",
  "other-workflow",
  "",
] as const;

/** Statuses the payload draws from: one active, the rest inactive. */
const STATUSES = [ACTIVE_STATUS, "invalid", "retired", "unregistered", ""];

const targetName = fc.constantFrom(...NAMES.filter((n) => n.trim() !== ""));

const registration: fc.Arbitrary<Registration> = fc.record({
  registrationId: fc.stringMatching(/^reg-[0-9]{1,3}$/),
  workflowId: fc.stringMatching(/^wf-[0-9]{1,3}$/),
  name: fc.oneof(
    { arbitrary: fc.constantFrom<string | null>(...NAMES), weight: 4 },
    { arbitrary: fc.constant(null), weight: 1 },
    { arbitrary: fc.string() as fc.Arbitrary<string | null>, weight: 1 },
  ),
  version: fc.constantFrom("1.0.0", "1.0.1", "2.0.0"),
  status: fc.constantFrom(...STATUSES),
  // A small timestamp pool makes exact ties frequent, and the non-finite /
  // absent values cover the "lack a registeredAt value" clause of
  // Requirement 2.3 for payloads that reach the binding unparsed.
  registeredAt: fc.oneof(
    { arbitrary: fc.constantFrom(0, 1000, 1000, 2000, 2000, 3000), weight: 6 },
    {
      arbitrary: fc.constantFrom(
        NaN,
        Infinity,
        undefined as unknown as number,
      ),
      weight: 1,
    },
  ),
});

const payload = fc.array(registration, { maxLength: 8 });

describe("Property 4: Target-workflow binding is a deterministic pure function", () => {
  it("binds exactly the active case-sensitive name matches, most recent first, ties by payload order", () => {
    fc.assert(
      fc.property(payload, targetName, (registrations, name) => {
        const result = bindTargetWorkflow(registrations, name);

        // Independent oracle for the candidate set: active status AND a
        // case-sensitive exact name match, in payload order (2.2).
        const expectedCandidates = registrations.filter(
          (r) => r.status === ACTIVE_STATUS && r.name === name,
        );
        expect(targetCandidates(registrations, name)).toEqual(
          expectedCandidates,
        );

        if (expectedCandidates.length === 0) {
          // Zero candidates is the one and only route to not-deployed, so
          // the state is a function of the payload alone (2.4, 2.7, 8.5).
          expect(result).toEqual({ state: "not-deployed" });
          return;
        }

        expect(result.state).toBe("bound");
        if (result.state !== "bound") return;

        // The bound registration is always one of the candidates: never an
        // inactive registration, never a differently-named one (2.2, 8.5).
        expect(expectedCandidates).toContain(result.registration);

        // Most recent registeredAt wins; ties and candidates lacking a
        // usable value resolve to the first such candidate in payload
        // order (2.3).
        const dated = expectedCandidates.filter((r) =>
          Number.isFinite(r.registeredAt),
        );
        if (dated.length === 0) {
          expect(result.registration).toBe(expectedCandidates[0]);
        } else {
          const maxRegisteredAt = Math.max(...dated.map((r) => r.registeredAt));
          expect(result.registration.registeredAt).toBe(maxRegisteredAt);
          expect(result.registration).toBe(
            dated.find((r) => r.registeredAt === maxRegisteredAt),
          );
        }

        // Exactly one candidate binds that candidate (2.2).
        if (expectedCandidates.length === 1) {
          expect(result.registration).toBe(expectedCandidates[0]);
        }
      }),
    );
  });

  it("is deterministic, pure, and independent of any prior payload", () => {
    fc.assert(
      fc.property(payload, payload, targetName, (before, after, name) => {
        const snapshot = JSON.stringify(after);

        // Two evaluations of the same payload agree (determinism), and the
        // result does not depend on what was bound before — so an undeploy
        // followed by a redeploy re-binds with no extra state (2.4, 8.8).
        const direct = bindTargetWorkflow(after, name);
        bindTargetWorkflow(before, name);
        const afterPrior = bindTargetWorkflow(after, name);
        expect(afterPrior).toEqual(direct);

        // The input payload is never mutated (purity).
        expect(JSON.stringify(after)).toBe(snapshot);
      }),
    );
  });

  it("binds iff an active exact-name match exists, in either transition direction", () => {
    fc.assert(
      fc.property(payload, payload, targetName, (first, second, name) => {
        const hasMatch = (registrations: readonly Registration[]) =>
          registrations.some(
            (r) => r.status === ACTIVE_STATUS && r.name === name,
          );

        for (const registrations of [first, second]) {
          const state = bindTargetWorkflow(registrations, name).state;
          expect(state).toBe(hasMatch(registrations) ? "bound" : "not-deployed");
        }
      }),
    );
  });
});

describe("bindTargetWorkflow examples", () => {
  function reg(overrides: Partial<Registration> = {}): Registration {
    return {
      registrationId: "reg-1",
      workflowId: "wf-1",
      name: "blue-plate-detection-guided-inspection",
      version: "1.0.0",
      status: ACTIVE_STATUS,
      registeredAt: 1000,
      ...overrides,
    };
  }

  const NAME = "blue-plate-detection-guided-inspection";

  it("binds the single active name match (2.2)", () => {
    const target = reg();
    const result = bindTargetWorkflow(
      [reg({ registrationId: "other", name: "other-workflow" }), target],
      NAME,
    );
    expect(result).toEqual({ state: "bound", registration: target });
  });

  it("picks the most recent registeredAt among matches (2.3)", () => {
    const older = reg({ registrationId: "old", registeredAt: 1000 });
    const newer = reg({ registrationId: "new", registeredAt: 2000 });
    expect(bindTargetWorkflow([newer, older], NAME)).toEqual({
      state: "bound",
      registration: newer,
    });
    expect(bindTargetWorkflow([older, newer], NAME)).toEqual({
      state: "bound",
      registration: newer,
    });
  });

  it("keeps the first candidate in payload order on equal registeredAt (2.3)", () => {
    const a = reg({ registrationId: "a", registeredAt: 1000 });
    const b = reg({ registrationId: "b", registeredAt: 1000 });
    expect(bindTargetWorkflow([a, b], NAME)).toEqual({
      state: "bound",
      registration: a,
    });
  });

  it("ignores case-insensitive name matches (2.2)", () => {
    expect(
      bindTargetWorkflow([reg({ name: NAME.toUpperCase() })], NAME),
    ).toEqual({ state: "not-deployed" });
  });

  it("reports not-deployed when the only match is inactive (2.7, 8.5)", () => {
    expect(bindTargetWorkflow([reg({ status: "invalid" })], NAME)).toEqual({
      state: "not-deployed",
    });
  });

  it("reports not-deployed for an empty payload (2.4)", () => {
    expect(bindTargetWorkflow([], NAME)).toEqual({ state: "not-deployed" });
  });
});
