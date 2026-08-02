/*
 * Unit tests for the deployed-workflow details page (task 5).
 *
 * **Feature: edge-workflow-run-ux, Property 1: Deployed workflow registrations
 * are visible and runnable from the UI**
 * **Validates: Requirements 2.2, 2.3, 2.4, 3.4**
 *
 * The API is mocked at the module boundary
 * (jest.mock("api/WorkflowRegistrationAPI")). The page reads `addError` from
 * AppLayoutContext to surface backend rejections in the flashbar, so the
 * context is provided with a jest.fn() to capture those notifications.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecution,
  WorkflowRegistrationDetails,
} from "api/WorkflowRegistrationAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import DeployedWorkflowDetails from "./DeployedWorkflowDetails";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTRATION_ID = "reg-1";

function details(
  overrides: Partial<WorkflowRegistrationDetails> = {},
): WorkflowRegistrationDetails {
  return {
    registrationId: REGISTRATION_ID,
    workflowId: "wf-alpha",
    version: "3",
    arch: "arm64",
    artifactPath: "/greengrass/artifacts/wf-alpha/3",
    status: "registered",
    registeredAt: 1700000000,
    executions: [],
    ...overrides,
  };
}

const FAILED_EXECUTION: WorkflowExecution = {
  executionId: "exec-failed-1",
  registrationId: REGISTRATION_ID,
  status: "failed",
  startedAt: 1700000100,
  finishedAt: 1700000200,
  failingNodeId: "node-7",
  error: "Sensor read timeout",
  hasImageResults: false,
  captureId: null,
  outputDir: null,
};

interface RenderResult {
  addError: jest.Mock;
}

function renderDetails(): RenderResult {
  const addError = jest.fn();
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
    addError,
  };
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={[`/deployed-workflows/${REGISTRATION_ID}`]}
      >
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
  return { addError };
}

describe("DeployedWorkflowDetails", () => {
  it("shows the Run workflow button for a registered registration (2.2)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ status: "registered" }),
    );

    renderDetails();

    expect(
      await screen.findByRole("button", { name: "Run workflow" }),
    ).toBeInTheDocument();
    // No invalid alert for a registered registration.
    expect(screen.queryByText("Invalid registration")).toBeNull();
  });

  it("shows no Run control and an alert with the reason for an invalid registration (2.4)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({
        status: "invalid",
        invalidReason: "Missing required artifact file: manifest.json",
      }),
    );

    renderDetails();

    // The invalid alert surfaces the reason.
    await screen.findByText("Invalid registration");
    expect(
      screen.getByText(/Missing required artifact file: manifest\.json/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/cannot be run/),
    ).toBeInTheDocument();

    // No trigger control is offered.
    expect(
      screen.queryByRole("button", { name: "Run workflow" }),
    ).toBeNull();
  });

  it("triggers a run via the API and refreshes the details on click (2.2)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ status: "registered" }),
    );
    (
      RegistrationAPI.triggerWorkflowRegistration as jest.Mock
    ).mockResolvedValue({
      executionId: "exec-new-1",
      registrationId: REGISTRATION_ID,
      status: "pending",
      startedAt: null,
      finishedAt: null,
      failingNodeId: null,
      error: null,
    });

    const { addError } = renderDetails();

    userEvent.click(
      await screen.findByRole("button", { name: "Run workflow" }),
    );

    await waitFor(() => {
      expect(
        RegistrationAPI.triggerWorkflowRegistration,
      ).toHaveBeenCalledWith(REGISTRATION_ID);
    });
    // The detail query is invalidated so the new execution appears: the
    // registration is fetched again after the initial load.
    await waitFor(() => {
      expect(
        RegistrationAPI.getWorkflowRegistration as jest.Mock,
      ).toHaveBeenCalledTimes(2);
    });
    expect(addError).not.toHaveBeenCalled();
  });

  it("surfaces the backend detail message when the trigger is rejected with 409 (3.4)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ status: "registered" }),
    );
    const backendDetail =
      "Workflow registration reg-1 is invalid and cannot be triggered";
    (
      RegistrationAPI.triggerWorkflowRegistration as jest.Mock
    ).mockRejectedValue({
      response: { status: 409, data: { detail: backendDetail } },
    });

    const { addError } = renderDetails();

    userEvent.click(
      await screen.findByRole("button", { name: "Run workflow" }),
    );

    await waitFor(() => {
      expect(addError).toHaveBeenCalledTimes(1);
    });

    // The flashbar notification content echoes the backend's detail message.
    const notification = addError.mock.calls[0][0];
    const { container } = render(<div>{notification.content}</div>);
    expect(container.textContent).toContain(backendDetail);
  });

  it("shows failingNodeId and error on a failed execution row (2.3)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ executions: [FAILED_EXECUTION] }),
    );

    renderDetails();

    // The execution row shows id, failed status, and failure details.
    const idCell = await screen.findByText("exec-failed-1");
    const row = idCell.closest("tr")!;
    expect(row.textContent).toContain("Failed");
    expect(row.textContent).toContain("Failing node: node-7");
    expect(row.textContent).toContain("Sensor read timeout");
  });

  it("shows execution history in newest-first order with an empty slot when none exist (2.3)", async () => {
    const older: WorkflowExecution = {
      executionId: "exec-older",
      registrationId: REGISTRATION_ID,
      status: "completed",
      startedAt: 1700000100,
      finishedAt: 1700000200,
      failingNodeId: null,
      error: null,
      hasImageResults: false,
      captureId: null,
      outputDir: null,
    };
    const newer: WorkflowExecution = {
      executionId: "exec-newer",
      registrationId: REGISTRATION_ID,
      status: "failed",
      startedAt: 1700000300,
      finishedAt: 1700000400,
      failingNodeId: null,
      error: "boom",
      hasImageResults: false,
      captureId: null,
      outputDir: null,
    };
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ executions: [older, newer] }),
    );

    renderDetails();

    const newerCell = await screen.findByText("exec-newer");
    const olderCell = screen.getByText("exec-older");
    // Newest first: the newer execution's row precedes the older one's.
    expect(
      newerCell.compareDocumentPosition(olderCell) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders the empty executions state when the workflow has not been run (2.3)", async () => {
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details({ executions: [] }),
    );

    renderDetails();

    await screen.findByText("No executions");
    expect(
      screen.getByText("This workflow has not been run yet."),
    ).toBeInTheDocument();
  });
});
