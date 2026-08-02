/*
 * Fix-checking property-based tests for the deployed-workflow presentation
 * logic and pages (task 5).
 *
 * **Feature: edge-workflow-run-ux, Property 1: Deployed workflow registrations
 * are visible and runnable from the UI**
 * **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**
 *
 * fast-check arbitraries generate WorkflowRegistration / WorkflowExecution
 * values across the full input space (random statuses, optional
 * invalidReason, nullable timestamps/failure fields). The pure logic in
 * presentation.ts is checked directly; the list page is checked as a
 * component property with the API mocked at the module boundary.
 *
 * Note on the trigger affordance (2.2, 2.4): in the pages as built, the
 * "Run workflow" control lives on the DETAILS page, not the list page. The
 * list property therefore asserts identity/status/invalidReason rendering and
 * that the list offers NO trigger control; the "control appears iff
 * registered" clause is checked both here via canTrigger (the exact predicate
 * the details page renders the button under) and against the rendered details
 * page in DeployedWorkflowDetails.test.tsx.
 */

import * as React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import fc from "fast-check";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  ExecutionStatus,
  RegistrationStatus,
  WorkflowExecution,
  WorkflowRegistration,
} from "api/WorkflowRegistrationAPI";
import {
  canTrigger,
  executionFailureDetails,
  registrationStatusIndicator,
  shouldPoll,
  sortExecutions,
} from "./presentation";
import ListDeployedWorkflows from "./list/ListDeployedWorkflows";

jest.mock("api/WorkflowRegistrationAPI");

/* ------------------------------------------------------------------ */
/* Arbitraries (smart generators over the API data shapes)             */
/* ------------------------------------------------------------------ */

const registrationStatusArb: fc.Arbitrary<RegistrationStatus> =
  fc.constantFrom("registered", "invalid");

const executionStatusArb: fc.Arbitrary<ExecutionStatus> = fc.constantFrom(
  "pending",
  "running",
  "completed",
  "failed",
);

// Epoch seconds within the plausible device clock range.
const epochSecondsArb = fc.integer({ min: 1, max: 2_000_000_000 });

const registrationArb: fc.Arbitrary<WorkflowRegistration> = fc
  .record({
    registrationId: fc.uuid(),
    workflowId: fc.nat(9999).map((n) => `wf-${n}`),
    version: fc.nat(999).map(String),
    arch: fc.constantFrom("arm64", "x86_64"),
    artifactPath: fc.nat(9999).map((n) => `/greengrass/artifacts/${n}`),
    status: registrationStatusArb,
    registeredAt: epochSecondsArb,
    invalidReason: fc.option(
      fc.nat(9999).map((n) => `invalid-reason-${n}`),
      { nil: undefined },
    ),
  })
  // Backend shape: invalidReason is present only when not "registered".
  .map((r) =>
    r.status === "registered" ? { ...r, invalidReason: undefined } : r,
  );

const executionArb: fc.Arbitrary<WorkflowExecution> = fc.record({
  executionId: fc.uuid(),
  registrationId: fc.uuid(),
  status: executionStatusArb,
  startedAt: fc.option(epochSecondsArb, { nil: null }),
  finishedAt: fc.option(epochSecondsArb, { nil: null }),
  failingNodeId: fc.option(
    fc.nat(999).map((n) => `node-${n}`),
    { nil: null },
  ),
  error: fc.option(
    fc.nat(9999).map((n) => `error-${n}`),
    { nil: null },
  ),
  hasImageResults: fc.boolean(),
  captureId: fc.option(
    fc.uuid(),
    { nil: null },
  ),
  outputDir: fc.option(
    fc.nat(9999).map((n) => `/aws_dda/captures/${n}`),
    { nil: null },
  ),
});

const executionListArb: fc.Arbitrary<WorkflowExecution[]> = fc.uniqueArray(
  executionArb,
  { selector: (e) => e.executionId, maxLength: 20 },
);

// Unique registrationIds so each generated row is uniquely addressable in
// the rendered list via its details link.
const registrationListArb: fc.Arbitrary<WorkflowRegistration[]> =
  fc.uniqueArray(registrationArb, {
    selector: (r) => r.registrationId,
    maxLength: 6,
  });

/** Sort key the implementation documents: null startedAt sorts newest. */
function startKey(execution: WorkflowExecution): number {
  return execution.startedAt === null
    ? Number.POSITIVE_INFINITY
    : execution.startedAt;
}

/* ------------------------------------------------------------------ */
/* Pure-logic properties                                               */
/* ------------------------------------------------------------------ */

describe("presentation.ts properties", () => {
  it("canTrigger(r) === (r.status === 'registered') for any registration (2.2, 2.4)", () => {
    fc.assert(
      fc.property(registrationArb, (r) => {
        expect(canTrigger(r)).toBe(r.status === "registered");
      }),
    );
  });

  it("sortExecutions returns a permutation ordered newest-first, nulls newest (2.3)", () => {
    fc.assert(
      fc.property(executionListArb, (executions) => {
        const inputOrder = executions.map((e) => e.executionId);
        const sorted = sortExecutions(executions);

        // Permutation: same length, same multiset of executions.
        expect(sorted).toHaveLength(executions.length);
        expect(sorted.map((e) => e.executionId).sort()).toEqual(
          executions.map((e) => e.executionId).sort(),
        );
        // Every output element is one of the input objects (no copies/edits).
        for (const e of sorted) {
          expect(executions).toContain(e);
        }

        // Ordered newest-first by startedAt, nulls treated as newest.
        for (let i = 1; i < sorted.length; i += 1) {
          expect(startKey(sorted[i - 1])).toBeGreaterThanOrEqual(
            startKey(sorted[i]),
          );
        }

        // The input array is not mutated.
        expect(executions.map((e) => e.executionId)).toEqual(inputOrder);
      }),
    );
  });

  it("executionFailureDetails defined iff failed, echoing failingNodeId/error (2.3)", () => {
    fc.assert(
      fc.property(executionArb, (e) => {
        const details = executionFailureDetails(e);
        if (e.status !== "failed") {
          expect(details).toBeUndefined();
        } else {
          expect(details).toBeDefined();
          if (e.failingNodeId !== null) {
            expect(details?.failingNodeId).toBe(e.failingNodeId);
          } else {
            expect(details).not.toHaveProperty("failingNodeId");
          }
          if (e.error !== null) {
            expect(details?.error).toBe(e.error);
          } else {
            expect(details).not.toHaveProperty("error");
          }
        }
      }),
    );
  });

  it("shouldPoll true iff some execution is pending or running (2.3)", () => {
    fc.assert(
      fc.property(executionListArb, (executions) => {
        expect(shouldPoll(executions)).toBe(
          executions.some(
            (e) => e.status === "pending" || e.status === "running",
          ),
        );
      }),
    );
  });
});

/* ------------------------------------------------------------------ */
/* Component property: registration list rendering                     */
/* ------------------------------------------------------------------ */

function renderList(): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  return render(
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(
        MemoryRouter,
        { initialEntries: ["/deployed-workflows"] },
        React.createElement(ListDeployedWorkflows),
      ),
    ),
  );
}

describe("ListDeployedWorkflows rendering property (2.1, 2.2, 2.4, 2.5)", () => {
  it(
    "renders every registration's identity/status (invalid reasons shown, no list trigger control) for any registration list",
    async () => {
      await fc.assert(
        fc.asyncProperty(registrationListArb, async (registrations) => {
          cleanup();
          (
            RegistrationAPI.listWorkflowRegistrations as jest.Mock
          ).mockResolvedValue(registrations);

          const { container } = renderList();

          if (registrations.length === 0) {
            // Empty set renders the empty state without error (2.5).
            await screen.findAllByText("No deployed workflows");
            expect(
              screen.getByText("No deployed workflows to display."),
            ).toBeInTheDocument();
            return;
          }

          // Wait until every row's details link is rendered.
          await waitFor(() => {
            expect(
              container.querySelectorAll('a[href^="/deployed-workflows/"]')
                .length,
            ).toBe(registrations.length);
          });

          for (const r of registrations) {
            // Each registration is uniquely addressable via its details link.
            const link = container.querySelector(
              `a[href="/deployed-workflows/${r.registrationId}"]`,
            );
            expect(link).not.toBeNull();

            // Its row shows identity (workflowId, version) and status (2.1).
            const row = link!.closest("tr");
            expect(row).not.toBeNull();
            const rowText = row!.textContent || "";
            expect(rowText).toContain(r.workflowId);
            expect(rowText).toContain(r.version);
            expect(rowText).toContain(registrationStatusIndicator(r).text);

            // Invalid registrations surface their invalidReason (2.4).
            if (r.status !== "registered" && r.invalidReason) {
              expect(rowText).toContain(r.invalidReason);
            }
          }

          // The list page offers no trigger control for ANY registration;
          // the run control lives on the details page and appears iff
          // canTrigger (asserted in DeployedWorkflowDetails.test.tsx) (2.2).
          expect(
            screen.queryByRole("button", { name: "Run workflow" }),
          ).toBeNull();
        }),
        { numRuns: 10 },
      );
    },
    120000,
  );
});
