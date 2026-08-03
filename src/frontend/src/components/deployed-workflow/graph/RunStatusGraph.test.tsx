/*
 * Unit tests for the RunStatusGraph screen (task 13.1).
 *
 * Covers:
 *  - node coloring by status (R7.3)
 *  - failure/warning detail surfaced on selection (R7.4)
 *  - node-status polling starts while the run is active and stops at a
 *    terminal state (R7.5)
 *  - fully-resolved coloring once the run is finished (R7.6)
 *
 * The API is mocked at the module boundary (jest.mock(
 * "api/WorkflowRegistrationAPI")); react-query drives the fetches, so the
 * screen is rendered inside a QueryClientProvider and a MemoryRouter that
 * supplies the :registrationId/:executionId route params.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecution,
  WorkflowGraph,
  NodeStatusMap,
} from "api/WorkflowRegistrationAPI";
import RunStatusGraph from "./RunStatusGraph";
import { STATUS_COLOR } from "./graphGeometry";

jest.mock("api/WorkflowRegistrationAPI");

// useAuth reads from the auth context/cookies which are not provided in this
// test harness; mock it to a stable disabled-auth value (RunResults pattern).
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({
    token: "",
    authEnabled: false,
  }),
}));

const REGISTRATION_ID = "reg-1";
const EXECUTION_ID = "exec-1";

const GRAPH: WorkflowGraph = {
  schemaVersion: "1",
  nodes: [
    { id: "camera", type: "input", position: { x: 0, y: 0 } },
    { id: "detect", type: "inference", position: { x: 240, y: 0 } },
    { id: "capture", type: "output", position: { x: 480, y: 0 } },
  ],
  connections: [
    // Canonical DDA object-endpoint shape ({ node, port }) served by the device.
    { from: { node: "camera", port: "out" }, to: { node: "detect", port: "in" } },
    { from: { node: "detect", port: "out" }, to: { node: "capture", port: "in" } },
  ],
};

function execution(
  overrides: Partial<WorkflowExecution> = {},
): WorkflowExecution {
  return {
    executionId: EXECUTION_ID,
    registrationId: REGISTRATION_ID,
    status: "completed",
    startedAt: 1700000000,
    finishedAt: 1700000100,
    failingNodeId: null,
    error: null,
    hasImageResults: true,
    captureId: "cap-1",
    outputDir: "/aws_dda/captures/wf/exec-1",
    ...overrides,
  };
}

function renderGraph(): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
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
          `/deployed-workflows/${REGISTRATION_ID}/executions/${EXECUTION_ID}/graph`,
        ]}
      >
        <Routes>
          <Route
            path="/deployed-workflows/:registrationId/executions/:executionId/graph"
            element={<RunStatusGraph />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockAPIs(opts: {
  exec: WorkflowExecution;
  statusMap: NodeStatusMap;
  graph?: WorkflowGraph;
}): void {
  (RegistrationAPI.getWorkflowRegistrationGraph as jest.Mock).mockResolvedValue(
    opts.graph ?? GRAPH,
  );
  (RegistrationAPI.getWorkflowExecution as jest.Mock).mockResolvedValue(
    opts.exec,
  );
  (
    RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock
  ).mockResolvedValue(opts.statusMap);
}

describe("RunStatusGraph", () => {
  afterEach(() => {
    jest.clearAllMocks();
    jest.useRealTimers();
  });

  it("renders a node card per graph node with its status color (R7.3, R7.6)", async () => {
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: {
        camera: { status: "success" },
        detect: { status: "warning", detail: "low confidence" },
        capture: { status: "failure", detail: "encoder error" },
      },
    });

    renderGraph();

    const cameraNode = await screen.findByTestId("node-camera");
    const detectNode = screen.getByTestId("node-detect");
    const captureNode = screen.getByTestId("node-capture");

    // Each node is colored by its Node_Run_Status.
    expect(cameraNode).toHaveAttribute("data-color", STATUS_COLOR.success);
    expect(detectNode).toHaveAttribute("data-color", STATUS_COLOR.warning);
    expect(captureNode).toHaveAttribute("data-color", STATUS_COLOR.failure);
  });

  it("colors a node not present in the status map as neutral pending (R7.6)", async () => {
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: { camera: { status: "success" } },
    });

    renderGraph();

    const detectNode = await screen.findByTestId("node-detect");
    expect(detectNode).toHaveAttribute("data-status", "pending");
    expect(detectNode).toHaveAttribute("data-color", STATUS_COLOR.pending);
  });

  it("draws an SVG edge for each connection between existing nodes", async () => {
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: {},
    });

    renderGraph();

    await screen.findByTestId("node-camera");
    expect(screen.getByTestId("edge-camera-detect")).toBeInTheDocument();
    expect(screen.getByTestId("edge-detect-capture")).toBeInTheDocument();
  });

  it("surfaces the error detail when a failure node is selected (R7.4)", async () => {
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: {
        capture: { status: "failure", detail: "encoder error" },
      },
    });

    renderGraph();

    const captureNode = await screen.findByTestId("node-capture");
    // Detail is not shown until the node is selected.
    expect(screen.queryByText(/encoder error/)).toBeNull();

    userEvent.click(captureNode);

    await waitFor(() => {
      expect(screen.getByText(/encoder error/)).toBeInTheDocument();
    });
  });

  it("surfaces the warning detail when a warning node is selected (R7.4)", async () => {
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: {
        detect: { status: "warning", detail: "low confidence frame" },
      },
    });

    renderGraph();

    const detectNode = await screen.findByTestId("node-detect");
    userEvent.click(detectNode);

    await waitFor(() => {
      expect(screen.getByText(/low confidence frame/)).toBeInTheDocument();
    });
  });

  // Flush pending microtasks (mock promise resolutions + react-query state
  // updates) without relying on waitFor, which does not cooperate with fake
  // timers.
  async function flushMicrotasks(times = 5): Promise<void> {
    for (let i = 0; i < times; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        await Promise.resolve();
      });
    }
  }

  it("polls node status while the run is active (R7.5)", async () => {
    jest.useFakeTimers();
    mockAPIs({
      exec: execution({ status: "running", finishedAt: null }),
      statusMap: { camera: { status: "running" } },
    });

    renderGraph();

    // Let the initial graph/execution/node-status loads settle so react-query
    // sees the active execution and schedules the node-status poll.
    await flushMicrotasks();
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).toHaveBeenCalled();
    const initialCalls = (
      RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock
    ).mock.calls.length;

    // Advancing past the poll interval triggers additional node-status fetches.
    for (let i = 0; i < 3; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        jest.advanceTimersByTime(2000);
        await Promise.resolve();
      });
      // eslint-disable-next-line no-await-in-loop
      await flushMicrotasks(2);
    }

    expect(
      (RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock).mock.calls
        .length,
    ).toBeGreaterThan(initialCalls);
  });

  it("does not keep polling node status once the run is terminal (R7.5)", async () => {
    jest.useFakeTimers();
    mockAPIs({
      exec: execution({ status: "completed" }),
      statusMap: {
        camera: { status: "success" },
        detect: { status: "success" },
        capture: { status: "success" },
      },
    });

    renderGraph();

    await flushMicrotasks();
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).toHaveBeenCalled();
    const callsAfterLoad = (
      RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock
    ).mock.calls.length;

    // Advancing well past several intervals must not trigger more fetches.
    for (let i = 0; i < 3; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        jest.advanceTimersByTime(4000);
        await Promise.resolve();
      });
      // eslint-disable-next-line no-await-in-loop
      await flushMicrotasks(2);
    }

    expect(
      (RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock).mock.calls
        .length,
    ).toBe(callsAfterLoad);
  });

  it("shows an error alert when the graph fails to load", async () => {
    (
      RegistrationAPI.getWorkflowRegistrationGraph as jest.Mock
    ).mockRejectedValue(new Error("boom"));
    (RegistrationAPI.getWorkflowExecution as jest.Mock).mockResolvedValue(
      execution({ status: "completed" }),
    );
    (
      RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock
    ).mockResolvedValue({});

    renderGraph();

    expect(
      await screen.findByText("Failed to load workflow graph"),
    ).toBeInTheDocument();
  });
});
