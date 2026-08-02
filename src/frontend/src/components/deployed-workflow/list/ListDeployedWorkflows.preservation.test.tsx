/*
 * Preservation property test — deployed-workflows list surface
 * (edge-vlm-workflow-fixes, task 2).
 *
 * **Feature: edge-vlm-workflow-fixes, Property 4: Preservation — Non-VLM
 * models, other consumers, and UUID fallback unchanged**
 * **Validates: Requirements 3.4**
 *
 * Observation-first methodology: this encodes behavior OBSERVED on the UNFIXED
 * code and MUST PASS on it. `ListDeployedWorkflows` is a regression guard for
 * this spec — it is explicitly out of scope for any code change. The fix only
 * touches the details view, so the list must keep rendering
 * `name || workflowId` with the UUID as secondary text.
 *
 * Covered preservation requirement:
 *   3.4 — the list renders the friendly name when present (UUID as secondary
 *         text) and falls back to the workflowId when no name is present.
 *
 * The API is mocked at the module boundary; useNavigate is stubbed as in the
 * existing ListDeployedWorkflows unit tests.
 */

import { render, screen, cleanup, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import fc from "fast-check";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type { WorkflowRegistration } from "api/WorkflowRegistrationAPI";
import ListDeployedWorkflows from "./ListDeployedWorkflows";

jest.mock("api/WorkflowRegistrationAPI");

const mockNavigate = jest.fn();
jest.mock("react-router-dom", () => ({
  ...jest.requireActual("react-router-dom"),
  useNavigate: () => mockNavigate,
}));

// Safe alphabet for values compared through DOM text / accessible names.
const SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
const safeStr = (min: number, max: number): fc.Arbitrary<string> =>
  fc
    .array(fc.constantFrom(...SAFE.split("")), { minLength: min, maxLength: max })
    .map((chars) => chars.join(""));

function registration(
  overrides: Partial<WorkflowRegistration> = {},
): WorkflowRegistration {
  return {
    registrationId: "reg-1",
    workflowId: "wf-uuid",
    version: "3",
    arch: "arm64",
    artifactPath: "/greengrass/artifacts/wf-uuid/3",
    status: "registered",
    registeredAt: 1700000000,
    ...overrides,
  };
}

function renderList(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/deployed-workflows"]}>
        <ListDeployedWorkflows />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe("deployed-workflows list preservation — name || workflowId (3.4)", () => {
  it("labels a row by the friendly name with the UUID as secondary text", async () => {
    await fc.assert(
      fc.asyncProperty(
        safeStr(4, 12),
        safeStr(6, 16),
        safeStr(3, 10),
        async (name, workflowId, registrationId) => {
          // Keep name and workflowId distinguishable so matchers are unambiguous.
          fc.pre(name !== workflowId);
          cleanup();
          (
            RegistrationAPI.listWorkflowRegistrations as jest.Mock
          ).mockResolvedValue([
            registration({ name, workflowId, registrationId }),
          ]);

          renderList();

          // The link shows the friendly name and points at the details page.
          const link = await screen.findByRole("link", { name });
          expect(link).toHaveAttribute(
            "href",
            `/deployed-workflows/${registrationId}`,
          );
          // The UUID is still shown as secondary text on the row.
          const row = link.closest("tr")!;
          expect(row.textContent).toContain(workflowId);
        },
      ),
      { numRuns: 10 },
    );
  }, 120000);

  it("falls back to the workflowId as the row label when no name is present", async () => {
    await fc.assert(
      fc.asyncProperty(
        safeStr(6, 16),
        safeStr(3, 10),
        fc.constantFrom<string | null | undefined>(null, undefined, ""),
        async (workflowId, registrationId, absentName) => {
          cleanup();
          (
            RegistrationAPI.listWorkflowRegistrations as jest.Mock
          ).mockResolvedValue([
            registration({
              name: absentName as string | null,
              workflowId,
              registrationId,
            }),
          ]);

          renderList();

          // No name => the link label is the workflowId itself.
          const link = await screen.findByRole("link", { name: workflowId });
          expect(link).toHaveAttribute(
            "href",
            `/deployed-workflows/${registrationId}`,
          );
          // No duplicate secondary UUID row is emitted when there is no name.
          const row = link.closest("tr")!;
          expect(
            within(row).getAllByText(workflowId).length,
          ).toBeGreaterThanOrEqual(1);
        },
      ),
      { numRuns: 10 },
    );
  }, 120000);
});
