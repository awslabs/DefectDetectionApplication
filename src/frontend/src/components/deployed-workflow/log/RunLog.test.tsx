/*
 * Unit tests for the deployed-workflow run log viewer (task 12).
 *
 * Covers Requirement 6: a populated log renders as scrollable/copyable text
 * (6.2, 6.5), an empty/pending log shows an explanatory empty state rather
 * than an error (6.4), and a failed run's error and failing node are evident
 * in the rendered log text (6.3).
 *
 * The API is mocked at the module boundary
 * (jest.mock("api/WorkflowRegistrationAPI")), matching the details-page test.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import RunLog from "./RunLog";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTRATION_ID = "reg-1";
const EXECUTION_ID = "exec-1";

function renderRunLog(): void {
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
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={[
          `/deployed-workflows/${REGISTRATION_ID}/executions/${EXECUTION_ID}/log`,
        ]}
      >
        <Routes>
          <Route
            path="/deployed-workflows/:registrationId/executions/:executionId/log"
            element={<RunLog />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RunLog", () => {
  it("renders the populated log text for a run (6.2, 6.5)", async () => {
    const logText = [
      "Rendered launch string: fakesrc ! fakesink",
      "Resolving node camera-1",
      "Run completed: is_anomalous=false confidence=0.02",
    ].join("\n");
    (
      RegistrationAPI.getWorkflowExecutionLog as jest.Mock
    ).mockResolvedValue(logText);

    renderRunLog();

    // The log body shows the captured lines.
    expect(
      await screen.findByText(/Rendered launch string/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Run completed: is_anomalous=false/),
    ).toBeInTheDocument();

    // A copy affordance is offered for a populated log (6.5).
    expect(
      screen.getByRole("button", { name: /Copy/ }),
    ).toBeInTheDocument();

    // The empty state is not shown when there is log content.
    expect(screen.queryByText("No log available yet")).toBeNull();
  });

  it("shows an explanatory empty state (not an error) when the log is empty (6.4)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionLog as jest.Mock
    ).mockResolvedValue("");

    renderRunLog();

    await screen.findByText("No log available yet");
    // No error alert and no copy button for an empty log.
    expect(
      screen.queryByRole("button", { name: /Copy/ }),
    ).toBeNull();
  });

  it("treats a whitespace-only log as empty (6.4)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionLog as jest.Mock
    ).mockResolvedValue("   \n\t  \n");

    renderRunLog();

    await screen.findByText("No log available yet");
  });

  it("makes a failed run's error and failing node evident in the log text (6.3)", async () => {
    const failureLog = [
      "Rendered launch string: v4l2src ! emltriton ! emlcapture",
      "Run failed at node inference-2",
      "Error: element emltriton failed to change state to PLAYING: model load error",
    ].join("\n");
    (
      RegistrationAPI.getWorkflowExecutionLog as jest.Mock
    ).mockResolvedValue(failureLog);

    renderRunLog();

    expect(
      await screen.findByText(/Run failed at node inference-2/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/model load error/),
    ).toBeInTheDocument();
  });

  it("shows an error state when the log fetch fails", async () => {
    (
      RegistrationAPI.getWorkflowExecutionLog as jest.Mock
    ).mockRejectedValue(new Error("network down"));

    renderRunLog();

    await waitFor(() => {
      expect(
        screen.getByText("Failed to load run log"),
      ).toBeInTheDocument();
    });
  });
});
