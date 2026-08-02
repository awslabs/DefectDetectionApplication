/*
 * Bug 2 & Bug 3 condition exploration tests (edge-vlm-workflow-fixes, task 1).
 *
 * **Feature: edge-vlm-workflow-fixes**
 * **Property 2: Bug Condition — Details view shows the name as primary identity**
 * **Property 3: Bug Condition — Registration details include the name**
 * **Validates: Requirements 1.3, 1.4, 2.3, 2.4**
 *
 * These tests encode the EXPECTED behavior of the deployed-workflow details
 * page for a registration that carries a human-readable `name`:
 *
 *   Bug 2 (isBugCondition2): registration.name is a non-empty string AND the
 *     details page's primary identity (<Header> title) shows the workflow UUID
 *     instead of the name.
 *
 *   Bug 3 (isBugCondition3): registration.name is a non-empty string AND the
 *     "Registration details" ColumnLayout renders no "Name" field carrying it.
 *
 * On UNFIXED code they are EXPECTED TO FAIL — each failure is a counterexample:
 *   Bug 2: the <Header> renders `registration.workflowId` (the UUID), never the
 *          available `registration.name`.
 *   Bug 3: the ColumnLayout shows only "Workflow" (UUID), "Version",
 *          "Architecture", "Status", "Registered", "Registration ID" — no
 *          "Name" field.
 *
 * The API is mocked at the module boundary (jest.mock("api/WorkflowRegistrationAPI")),
 * matching the existing DeployedWorkflowDetails unit tests.
 */

import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import fc from "fast-check";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type { WorkflowRegistrationDetails } from "api/WorkflowRegistrationAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import DeployedWorkflowDetails from "./DeployedWorkflowDetails";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTRATION_ID = "reg-1";
// A concrete opaque workflow UUID (design "Examples" for Bugs 2 & 3).
const WORKFLOW_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
const WORKFLOW_NAME = "Cookie Inspector";

function details(
  overrides: Partial<WorkflowRegistrationDetails> = {},
): WorkflowRegistrationDetails {
  return {
    registrationId: REGISTRATION_ID,
    workflowId: WORKFLOW_UUID,
    name: WORKFLOW_NAME,
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
/* Bug 2 — details title shows the name as primary identity            */
/* ------------------------------------------------------------------ */

describe("DeployedWorkflowDetails — name as primary identity (Bug 2)", () => {
  it("renders the workflow name in the page <Header> when a name is present (2.3)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ name: WORKFLOW_NAME, workflowId: WORKFLOW_UUID }),
    );

    renderDetails();

    // Wait for the page to render (Run workflow button appears for a
    // registered registration).
    await screen.findByRole("button", { name: "Run workflow" });

    // The page's primary identity is the h1 Header. It must show the name.
    // On UNFIXED code this FAILS: the Header renders the UUID, not the name.
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent(WORKFLOW_NAME);
  }, 15000);

  it("renders any non-empty name as the primary identity (property, 2.3)", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.string({ minLength: 1, maxLength: 40 }).filter((s) => s.trim().length > 0),
        async (name) => {
          cleanup();
          (
            RegistrationAPI.getWorkflowRegistration as jest.Mock
          ).mockResolvedValue(details({ name, workflowId: WORKFLOW_UUID }));

          renderDetails();
          await screen.findByRole("button", { name: "Run workflow" });

          const heading = screen.getByRole("heading", { level: 1 });
          // The primary identity is the name, not the opaque UUID.
          expect(heading).toHaveTextContent(name);
        },
      ),
      { numRuns: 12 },
    );
  }, 120000);
});

/* ------------------------------------------------------------------ */
/* Bug 3 — registration details include a "Name" field                 */
/* ------------------------------------------------------------------ */

describe("DeployedWorkflowDetails — registration details Name field (Bug 3)", () => {
  it('shows a "Name" field carrying the name alongside Workflow (UUID) and Registration ID (2.4)', async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ name: WORKFLOW_NAME, workflowId: WORKFLOW_UUID }),
    );

    renderDetails();
    await screen.findByRole("button", { name: "Run workflow" });

    // The existing identifiers are still present (regression guard).
    const workflowLabel = screen.getByText("Workflow");
    expect(workflowLabel.parentElement).toHaveTextContent(WORKFLOW_UUID);
    const registrationIdLabel = screen.getByText("Registration ID");
    expect(registrationIdLabel.parentElement).toHaveTextContent(REGISTRATION_ID);

    // A "Name" key/label must appear and carry the human-readable name.
    // On UNFIXED code this FAILS: there is no "Name" field in the layout.
    const nameLabel = screen.getByText("Name");
    expect(nameLabel.parentElement).toHaveTextContent(WORKFLOW_NAME);
  }, 15000);

  it('renders a "Name" field for any non-empty name (property, 2.4)', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.string({ minLength: 1, maxLength: 40 }).filter((s) => s.trim().length > 0),
        async (name) => {
          cleanup();
          (
            RegistrationAPI.getWorkflowRegistration as jest.Mock
          ).mockResolvedValue(details({ name, workflowId: WORKFLOW_UUID }));

          renderDetails();
          await screen.findByRole("button", { name: "Run workflow" });

          const nameLabel = screen.getByText("Name");
          expect(nameLabel.parentElement).toHaveTextContent(name);
          // The identifiers remain present.
          expect(screen.getByText("Registration ID")).toBeInTheDocument();
          expect(screen.getByText("Workflow")).toBeInTheDocument();
        },
      ),
      { numRuns: 12 },
    );
  }, 120000);
});
