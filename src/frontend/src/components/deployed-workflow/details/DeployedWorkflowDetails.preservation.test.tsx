/*
 * Preservation property tests — Bugs 2 & 3 non-bug-condition inputs
 * (edge-vlm-workflow-fixes, task 2).
 *
 * **Feature: edge-vlm-workflow-fixes, Property 4: Preservation — Non-VLM
 * models, other consumers, and UUID fallback unchanged**
 * **Validates: Requirements 3.3, 3.5**
 *
 * Observation-first methodology: these assertions encode behavior OBSERVED on
 * the UNFIXED code and MUST PASS on it — the baseline the Bugs 2/3 fix must
 * preserve. They cover only inputs where the name-based bug conditions do NOT
 * hold (registrations with no resolvable name) plus the identifier/detail
 * fields that must render identically regardless of the name.
 *
 * Covered preservation requirements:
 *   3.3 — a registration with `name == null`/empty falls back to the workflow
 *         UUID as the details-page primary identity (the <Header> title).
 *         (On unfixed code no "Name" field exists yet; the fix adds one with
 *         the same UUID fallback — that fixed-side assertion lives in task 3.)
 *   3.5 — the "Registration details" ColumnLayout shows Version, Architecture,
 *         Status, Registered-at, and Registration ID unchanged.
 *
 * The API is mocked at the module boundary (jest.mock("api/WorkflowRegistrationAPI")),
 * matching the existing DeployedWorkflowDetails unit tests.
 */

import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import format from "date-fns/format";
import fc from "fast-check";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type { WorkflowRegistrationDetails } from "api/WorkflowRegistrationAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { registrationStatusIndicator } from "../presentation";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import DeployedWorkflowDetails from "./DeployedWorkflowDetails";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTRATION_ID = "reg-1";
const WORKFLOW_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";

// Safe alphabet for values compared through DOM text.
const SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_";
const safeStr = (min: number, max: number): fc.Arbitrary<string> =>
  fc
    .array(fc.constantFrom(...SAFE.split("")), { minLength: min, maxLength: max })
    .map((chars) => chars.join(""));

function details(
  overrides: Partial<WorkflowRegistrationDetails> = {},
): WorkflowRegistrationDetails {
  return {
    registrationId: REGISTRATION_ID,
    workflowId: WORKFLOW_UUID,
    name: null,
    version: "3",
    arch: "arm64",
    artifactPath: `/greengrass/artifacts/${WORKFLOW_UUID}/3`,
    status: "registered",
    registeredAt: 1700000000,
    executions: [],
    ...overrides,
  };
}

function renderDetails(): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
    logger: {
      log: () => undefined,
      warn: () => undefined,
      error: () => undefined,
    },
  });
  const appLayoutValue = {
    openNavigation: false,
    setOpenNavigation: jest.fn(),
    notifications: [],
    tablesInfo: {},
    tablesPref: {},
    setTableInfo: jest.fn(),
    setTableTypePref: jest.fn(),
    addNotification: jest.fn(),
    removeNotification: jest.fn(),
    addSuccess: jest.fn(),
    addError: jest.fn(),
  };
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/deployed-workflows/${REGISTRATION_ID}`]}>
        <AppLayoutContext.Provider value={appLayoutValue}>
          <Routes>
            <Route
              path="/deployed-workflows/:registrationId"
              element={<DeployedWorkflowDetails />}
            />
          </Routes>
        </AppLayoutContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
});

/* ------------------------------------------------------------------ */
/* 3.3 — UUID fallback as the details primary identity when unnamed    */
/* ------------------------------------------------------------------ */

describe("details primary-identity preservation — UUID fallback (3.3)", () => {
  it("shows the workflow UUID as the page title when name is null", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ name: null, workflowId: WORKFLOW_UUID }),
    );

    renderDetails();
    await screen.findByRole("button", { name: "Run workflow" });

    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent(WORKFLOW_UUID);
  }, 15000);

  it("shows the workflow UUID as the page title when name is empty", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ name: "", workflowId: WORKFLOW_UUID }),
    );

    renderDetails();
    await screen.findByRole("button", { name: "Run workflow" });

    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent(WORKFLOW_UUID);
  }, 15000);

  it("falls back to the UUID title for any absent/blank name (property)", async () => {
    const blankName = fc.constantFrom<string | null | undefined>(
      null,
      undefined,
      "",
    );
    await fc.assert(
      fc.asyncProperty(blankName, safeStr(6, 20), async (name, uuid) => {
        cleanup();
        (
          RegistrationAPI.getWorkflowRegistration as jest.Mock
        ).mockResolvedValue(
          details({ name: name as string | null, workflowId: uuid }),
        );

        renderDetails();
        await screen.findByRole("button", { name: "Run workflow" });

        const heading = screen.getByRole("heading", { level: 1 });
        // No resolvable name → the opaque UUID remains the primary identity.
        expect(heading).toHaveTextContent(uuid);
      }),
      { numRuns: 10 },
    );
  }, 120000);
});

/* ------------------------------------------------------------------ */
/* 3.5 — the registration-details ColumnLayout renders unchanged       */
/* ------------------------------------------------------------------ */

describe("registration-details field preservation (3.5)", () => {
  it("renders Workflow, Version, Architecture, Status, Registered-at and Registration ID unchanged", async () => {
    const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);

    const registrationArb = fc.record({
      registrationId: safeStr(4, 16),
      workflowId: safeStr(6, 20),
      name: fc.constantFrom<string | null>(null, ""),
      version: safeStr(1, 6),
      arch: fc.constantFrom("arm64", "x86_64", "aarch64"),
      status: fc.constantFrom<"registered" | "invalid">(
        "registered",
        "invalid",
      ),
      registeredAt: fc.integer({ min: 1, max: 2_000_000_000 }),
    });

    await fc.assert(
      fc.asyncProperty(registrationArb, async (reg) => {
        cleanup();
        const registration = details({
          registrationId: reg.registrationId,
          workflowId: reg.workflowId,
          name: reg.name,
          version: reg.version,
          arch: reg.arch,
          status: reg.status,
          registeredAt: reg.registeredAt,
          ...(reg.status === "invalid"
            ? { invalidReason: "Missing required artifact file: manifest.json" }
            : {}),
        });
        (
          RegistrationAPI.getWorkflowRegistration as jest.Mock
        ).mockResolvedValue(registration);

        renderDetails();
        // The "Registration details" section is present once loaded.
        await screen.findByText("Version");

        // Workflow (UUID) identifier field — unchanged.
        const workflowLabel = screen.getByText("Workflow");
        expect(workflowLabel.parentElement).toHaveTextContent(reg.workflowId);

        // Version.
        expect(
          screen.getByText("Version").parentElement,
        ).toHaveTextContent(reg.version);

        // Architecture.
        expect(
          screen.getByText("Architecture").parentElement,
        ).toHaveTextContent(reg.arch);

        // Status indicator text matches the presentation mapping. "Status"
        // also appears as an Executions table column header, so scope to the
        // registration-details key-label (the one not inside a <th>).
        const expectedStatus = registrationStatusIndicator(registration).text;
        const statusLabel = screen
          .getAllByText("Status")
          .find((el) => el.closest("th") === null)!;
        expect(statusLabel.parentElement).toHaveTextContent(expectedStatus);

        // Registered-at label (with timezone) + formatted timestamp.
        const registeredLabel = screen.getByText(`Registered ${timezoneLabel}`);
        const expectedDate = format(
          reg.registeredAt * 1000,
          DATE_WITHOUT_TZ,
        );
        expect(registeredLabel.parentElement).toHaveTextContent(expectedDate);

        // Registration ID.
        expect(
          screen.getByText("Registration ID").parentElement,
        ).toHaveTextContent(reg.registrationId);
      }),
      { numRuns: 10 },
    );
  }, 120000);
});
