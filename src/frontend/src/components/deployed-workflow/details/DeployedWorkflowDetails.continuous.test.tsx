/*
 * A deployed workflow whose stream node runs in continuous mode
 * (rtsp-rtmp-stream-cameras Requirements 11.6, 11.7, 16.2): its continuous
 * status, pause and resume, and the bounded recent and notable runs.
 *
 * The API is mocked at the module boundary, like the other details tests.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  ContinuousStatus,
  WorkflowExecution,
  WorkflowRegistrationDetails,
} from "api/WorkflowRegistrationAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import DeployedWorkflowDetails from "./DeployedWorkflowDetails";

jest.mock("api/WorkflowRegistrationAPI");

const REGISTRATION_ID = "reg-1";

function details(executions: WorkflowExecution[] = []): WorkflowRegistrationDetails {
  return {
    registrationId: REGISTRATION_ID,
    workflowId: "wf-ppe",
    name: "PPE watch",
    version: "4",
    arch: "arm64",
    artifactPath: "/greengrass/artifacts/wf-ppe/4",
    status: "registered",
    registeredAt: 1700000000,
    executions,
  };
}

function execution(id: string, startedAt: number, status: WorkflowExecution["status"] = "completed"): WorkflowExecution {
  return {
    executionId: id,
    registrationId: REGISTRATION_ID,
    status,
    startedAt,
    finishedAt: status === "completed" || status === "failed" ? startedAt + 1 : null,
    failingNodeId: null,
    error: status === "failed" ? "boom" : null,
    hasImageResults: false,
    captureId: null,
    outputDir: null,
  };
}

function continuousStatus(overrides: Partial<ContinuousStatus> = {}): ContinuousStatus {
  return {
    registrationId: REGISTRATION_ID,
    state: "running",
    configuredFps: 2,
    effectiveFps: 1.8333,
    counters: {
      started: 1200,
      completed: 1195,
      failed: 5,
      skippedBusy: 40,
      skippedNoNewFrame: 3,
      notable: 12,
      outputsSent: 9,
      streamUnavailable: 1,
    },
    streamHealth: { state: "streaming" },
    pausedAtMs: null,
    cameraSourceId: "cfg-7",
    runInProgress: true,
    ...overrides,
  };
}

const RECENT = [execution("exec-recent-2", 1700000200), execution("exec-recent-1", 1700000100)];
const NOTABLE = [execution("exec-notable-1", 1700000150, "failed")];

function renderDetails(): { addError: jest.Mock } {
  const addError = jest.fn();
  const queryClient = new QueryClient({
    // No garbage-collection timers outlive a test.
    defaultOptions: {
      queries: { retry: false, cacheTime: Infinity },
      mutations: { retry: false, cacheTime: Infinity },
    },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/deployed-workflows/${REGISTRATION_ID}`]}>
        <AppLayoutContext.Provider
          value={{
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
          }}
        >
          <Routes>
            <Route path="/deployed-workflows/:registrationId" element={<DeployedWorkflowDetails />} />
          </Routes>
        </AppLayoutContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { addError };
}

/** The value under a label of the continuous status panel. */
function valueOf(label: string): string {
  const panel = screen.getByTestId("continuous-status-panel");
  const labelElement = within(panel).getByText(label);
  return labelElement.parentElement?.textContent?.replace(label, "") ?? "";
}

beforeEach(() => {
  // The full history of the registration, which a continuous workflow's
  // table does not use.
  (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
    details([execution("exec-history-1", 1600000000)]),
  );
  (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(continuousStatus());
  (RegistrationAPI.listRegistrationExecutions as jest.Mock).mockImplementation(
    async (_id: string, options: { notable?: boolean } = {}) => (options.notable ? NOTABLE : RECENT),
  );
});

describe("a continuous deployed workflow", () => {
  it("shows its state, rates, camera and counters (16.2)", async () => {
    renderDetails();
    expect(await screen.findByText("Continuous processing")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(valueOf("Configured rate")).toBe("2 fps");
    expect(valueOf("Effective rate (last 60 s)")).toBe("1.83 fps");
    expect(valueOf("Run in progress")).toBe("Yes");
    expect(screen.getByText("cfg-7")).toBeInTheDocument();
    expect(valueOf("Runs started")).toBe("1200");
    expect(valueOf("Runs completed")).toBe("1195");
    expect(valueOf("Runs failed")).toBe("5");
    expect(valueOf("Ticks skipped: run in progress")).toBe("40");
    expect(valueOf("Ticks skipped: no new frame")).toBe("3");
    expect(valueOf("Notable runs")).toBe("12");
    expect(valueOf("Outputs sent")).toBe("9");
    expect(valueOf("Stream outages")).toBe("1");
  });

  it("offers no manual run while it runs, and lists the bounded recent runs (11.7, 16.2)", async () => {
    renderDetails();
    expect(await screen.findByText("exec-recent-2")).toBeInTheDocument();
    expect(screen.getByText("exec-recent-1")).toBeInTheDocument();
    expect(screen.queryByText("exec-history-1")).toBeNull();
    expect(RegistrationAPI.listRegistrationExecutions).toHaveBeenCalledWith(REGISTRATION_ID, {
      limit: 50,
      notable: false,
    });
    expect(screen.queryByRole("button", { name: "Run workflow" })).toBeNull();
  });

  it("polls the bounded runs list, not the whole history, while runs are active", async () => {
    // The full history has a run in flight, which polls a classic workflow.
    (RegistrationAPI.getWorkflowRegistration as jest.Mock).mockResolvedValue(
      details([execution("exec-history-1", 1600000000, "running")]),
    );
    renderDetails();
    await screen.findByText("exec-recent-2");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2300));
    });
    expect(RegistrationAPI.getWorkflowRegistration).toHaveBeenCalledTimes(1);
    expect(
      (RegistrationAPI.listRegistrationExecutions as jest.Mock).mock.calls.length,
    ).toBeGreaterThanOrEqual(2);
  });

  it("filters the runs to the notable ones (16.2)", async () => {
    renderDetails();
    await screen.findByText("exec-recent-2");
    userEvent.click(screen.getByRole("button", { name: "Notable runs" }));
    expect(await screen.findByText("exec-notable-1")).toBeInTheDocument();
    expect(screen.queryByText("exec-recent-2")).toBeNull();
    expect(RegistrationAPI.listRegistrationExecutions).toHaveBeenCalledWith(REGISTRATION_ID, {
      limit: 50,
      notable: true,
    });
    expect(
      screen.getByText("The newest notable runs: runs that failed, sent an output, or changed an event gate."),
    ).toBeInTheDocument();
  });

  it("explains an empty notable list", async () => {
    (RegistrationAPI.listRegistrationExecutions as jest.Mock).mockImplementation(
      async (_id: string, options: { notable?: boolean } = {}) => (options.notable ? [] : RECENT),
    );
    renderDetails();
    await screen.findByText("exec-recent-2");
    userEvent.click(screen.getByRole("button", { name: "Notable runs" }));
    expect(await screen.findByText("No notable runs")).toBeInTheDocument();
  });

  it("pauses the workflow, which then takes a manual run (11.6, 11.7)", async () => {
    const paused = continuousStatus({ state: "paused", pausedAtMs: 1790000000000, effectiveFps: 0, runInProgress: false });
    (RegistrationAPI.pauseContinuousWorkflow as jest.Mock).mockResolvedValue(paused);
    renderDetails();
    const pause = await screen.findByRole("button", { name: "Pause" });
    // From here on the device reports the pause.
    (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(paused);
    userEvent.click(pause);

    await waitFor(() =>
      expect(RegistrationAPI.pauseContinuousWorkflow).toHaveBeenCalledWith(REGISTRATION_ID),
    );
    expect(await screen.findByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText(/stays paused, across restarts too/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run workflow" })).toBeInTheDocument();
  });

  it("resumes a paused workflow", async () => {
    (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(
      continuousStatus({ state: "paused", pausedAtMs: 1790000000000 }),
    );
    (RegistrationAPI.resumeContinuousWorkflow as jest.Mock).mockResolvedValue(continuousStatus());
    renderDetails();
    const resume = await screen.findByRole("button", { name: "Resume" });
    expect(screen.getByRole("button", { name: "Run workflow" })).toBeInTheDocument();
    (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(continuousStatus());
    userEvent.click(resume);
    await waitFor(() =>
      expect(RegistrationAPI.resumeContinuousWorkflow).toHaveBeenCalledWith(REGISTRATION_ID),
    );
    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Run workflow" })).toBeNull());
  });

  it("shows why a pause failed", async () => {
    (RegistrationAPI.pauseContinuousWorkflow as jest.Mock).mockRejectedValue(
      Object.assign(new Error("Request failed with status code 404"), {
        response: { data: { message: "Workflow registration 'reg-1' does not process a stream camera continuously" } },
      }),
    );
    renderDetails();
    userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText("The workflow could not be paused")).toBeInTheDocument();
    expect(
      screen.getByText("Workflow registration 'reg-1' does not process a stream camera continuously"),
    ).toBeInTheDocument();
  });

  it("shows the camera's state while it waits for the stream", async () => {
    (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(
      continuousStatus({
        state: "waiting_for_stream",
        runInProgress: false,
        streamHealth: {
          state: "reconnecting",
          lastError: { category: "timeout", message: "No frame for 10 s" },
          nextAttemptInS: 4,
        },
      }),
    );
    renderDetails();
    expect(await screen.findByText("Waiting for the stream")).toBeInTheDocument();
    const camera = screen.getByText("Camera").parentElement as HTMLElement;
    expect(within(camera).getByText("Reconnecting")).toBeInTheDocument();
    expect(within(camera).getByText("No frame for 10 s; next attempt in 4 s")).toBeInTheDocument();
  });
});

describe("a deployed workflow that does not run continuously", () => {
  it("keeps the manual run and its full history", async () => {
    (RegistrationAPI.getContinuousStatus as jest.Mock).mockResolvedValue(null);
    renderDetails();
    expect(await screen.findByRole("button", { name: "Run workflow" })).toBeInTheDocument();
    expect(await screen.findByText("exec-history-1")).toBeInTheDocument();
    expect(screen.queryByText("Continuous processing")).toBeNull();
    expect(screen.queryByRole("button", { name: "Notable runs" })).toBeNull();
    expect(RegistrationAPI.listRegistrationExecutions).not.toHaveBeenCalled();
  });
});
