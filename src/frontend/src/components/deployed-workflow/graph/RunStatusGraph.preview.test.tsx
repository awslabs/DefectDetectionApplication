/*
 * Component tests for the RunStatusGraph output-node preview behavior
 * (output-node-preview-popover spec, task 4.4).
 *
 * Covers:
 *  - click-to-open / click-to-close of the preview card (R1.1, R1.2)
 *  - per-type preview content: capture thumbnail (R2.1), llm snippet (R2.2),
 *    bedrock fields (R2.3), mqtt status + detail (R2.4)
 *  - degraded states: in-flight placeholder (R3.1), failure alert content
 *    (R3.2, R5.2), missing metadata fallback (R3.3), loading indicator (R3.4)
 *  - preservation: non-output nodes keep the pre-existing detail rendering
 *    (R1.4, R5.1, R5.2)
 *
 * Extends the RunStatusGraph.test.tsx patterns: the API is mocked at the
 * module boundary (jest.mock("api/WorkflowRegistrationAPI")), useAuth is
 * mocked to a disabled-auth value (RunResults pattern), and the screen is
 * rendered inside a QueryClientProvider and a MemoryRouter supplying the
 * :registrationId/:executionId graph route.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  NodeStatusMap,
  WorkflowExecution,
  WorkflowExecutionMetadata,
  WorkflowGraph,
} from "api/WorkflowRegistrationAPI";
import RunStatusGraph from "./RunStatusGraph";
import {
  PENDING_MESSAGE,
  UNAVAILABLE_MESSAGE,
} from "./NodePreviewCard";

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

/** A graph containing every preview-relevant output type plus a non-output
 *  inference node (`detect`) for the preservation checks. */
const GRAPH: WorkflowGraph = {
  schemaVersion: "1",
  nodes: [
    { id: "detect", type: "inference", position: { x: 0, y: 0 } },
    { id: "cap1", type: "capture", position: { x: 240, y: 0 } },
    { id: "llm1", type: "llm_inference", position: { x: 480, y: 0 } },
    { id: "bedrock1", type: "bedrock_inference", position: { x: 0, y: 120 } },
    { id: "mqtt1", type: "mqtt_publish", position: { x: 240, y: 120 } },
  ],
  connections: [],
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
  exec?: WorkflowExecution;
  statusMap?: NodeStatusMap;
  metadata?: WorkflowExecutionMetadata;
}): void {
  (RegistrationAPI.getWorkflowRegistrationGraph as jest.Mock).mockResolvedValue(
    GRAPH,
  );
  (RegistrationAPI.getWorkflowExecution as jest.Mock).mockResolvedValue(
    opts.exec ?? execution(),
  );
  (
    RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock
  ).mockResolvedValue(opts.statusMap ?? {});
  (
    RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock
  ).mockResolvedValue(opts.metadata ?? {});
  (
    RegistrationAPI.workflowExecutionOutputImageUrl as jest.Mock
  ).mockImplementation((id: string) => `/output-image/${id}`);
}

describe("RunStatusGraph output-node preview", () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it("opens the preview card on click and closes it on a second click (1.1, 1.2)", async () => {
    mockAPIs({ statusMap: { cap1: { status: "success" } } });

    renderGraph();

    const captureNode = await screen.findByTestId("node-cap1");
    expect(screen.queryByTestId("node-preview-card")).toBeNull();

    userEvent.click(captureNode);
    await waitFor(() => {
      expect(screen.getByTestId("node-preview-card")).toBeInTheDocument();
    });

    userEvent.click(captureNode);
    await waitFor(() => {
      expect(screen.queryByTestId("node-preview-card")).toBeNull();
    });
    // Deselected: no detail rendering of any kind remains.
    expect(screen.queryByTestId("node-detail")).toBeNull();
  });

  it("shows the capture thumbnail sourced from workflowExecutionOutputImageUrl (2.1)", async () => {
    mockAPIs({
      exec: execution({ hasImageResults: true }),
      statusMap: { cap1: { status: "success" } },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-cap1"));

    const thumbnail = await screen.findByTestId("preview-thumbnail");
    expect(thumbnail).toHaveAttribute("src", `/output-image/${EXECUTION_ID}`);
    // Always-present results link (R1.3).
    expect(
      screen.getByRole("link", { name: "View full results" }),
    ).toHaveAttribute(
      "href",
      `/deployed-workflows/${REGISTRATION_ID}/executions/${EXECUTION_ID}/results`,
    );
  });

  it("shows the llm generated-text snippet from the run metadata (2.2)", async () => {
    mockAPIs({
      statusMap: { llm1: { status: "success" } },
      metadata: { llm: { llm1: { generated_text: "The part looks fine." } } },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-llm1"));

    const text = await screen.findByTestId("preview-text");
    expect(text).toHaveTextContent("The part looks fine.");
  });

  it("shows the bedrock is_anomalous/confidence fields from the run metadata (2.3)", async () => {
    mockAPIs({
      statusMap: { bedrock1: { status: "success" } },
      metadata: { is_anomalous: true, confidence: 0.93 },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-bedrock1"));

    const fields = await screen.findByTestId("preview-fields");
    expect(fields).toHaveTextContent("is_anomalous");
    expect(fields).toHaveTextContent("true");
    expect(fields).toHaveTextContent("confidence");
    expect(fields).toHaveTextContent("0.93");
  });

  it("shows the mqtt node's run status and status detail (2.4)", async () => {
    mockAPIs({
      statusMap: {
        mqtt1: { status: "success", detail: "published to plant/topic" },
      },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-mqtt1"));

    const detail = await screen.findByTestId("preview-status-detail");
    expect(detail).toHaveTextContent("published to plant/topic");
    // The status itself is rendered in the card.
    const card = screen.getByTestId("node-preview-card");
    expect(card).toHaveTextContent("success");
    // Publish previews never need the metadata endpoint.
    expect(
      RegistrationAPI.getWorkflowExecutionMetadata,
    ).not.toHaveBeenCalled();
  });

  it("shows the in-progress placeholder while the node has no terminal status (3.1)", async () => {
    mockAPIs({
      exec: execution({ status: "running", finishedAt: null }),
      statusMap: { llm1: { status: "running" } },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-llm1"));

    const pending = await screen.findByTestId("preview-pending");
    expect(pending).toHaveTextContent(PENDING_MESSAGE);
    // Metadata is not fetched for non-terminal nodes.
    expect(
      RegistrationAPI.getWorkflowExecutionMetadata,
    ).not.toHaveBeenCalled();
  });

  it("keeps the failure alert content for a failed output node (3.2, 5.2)", async () => {
    mockAPIs({
      statusMap: { cap1: { status: "failure", detail: "encoder error" } },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-cap1"));

    await waitFor(() => {
      expect(screen.getByTestId("node-preview-card")).toBeInTheDocument();
    });
    // The pre-existing failure alert presentation renders inside the card.
    const alert = screen.getByTestId("node-detail");
    expect(alert).toHaveTextContent("cap1 — failure");
    expect(alert).toHaveTextContent("encoder error");
  });

  it("shows the fallback message when the metadata has no entry for the node (3.3)", async () => {
    mockAPIs({
      statusMap: { llm1: { status: "success" } },
      metadata: {},
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-llm1"));

    const unavailable = await screen.findByTestId("preview-unavailable");
    expect(unavailable).toHaveTextContent(UNAVAILABLE_MESSAGE);
  });

  it("shows a loading indicator while the metadata request is in flight (3.4)", async () => {
    mockAPIs({ statusMap: { llm1: { status: "success" } } });
    // Never-resolving metadata request keeps the query in flight.
    (
      RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock
    ).mockImplementation(() => new Promise(() => undefined));

    renderGraph();

    userEvent.click(await screen.findByTestId("node-llm1"));

    await waitFor(() => {
      expect(screen.getByTestId("preview-loading")).toBeInTheDocument();
    });
  });

  it("keeps the pre-existing detail rendering for non-output nodes (1.4, 5.1, 5.2)", async () => {
    mockAPIs({ statusMap: { detect: { status: "success" } } });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-detect"));

    await waitFor(() => {
      expect(screen.getByTestId("node-detail")).toBeInTheDocument();
    });
    // The plain pre-existing Box detail, with no preview card.
    expect(screen.getByTestId("node-detail")).toHaveTextContent(
      "detect — success",
    );
    expect(screen.queryByTestId("node-preview-card")).toBeNull();
  });

  it("keeps the pre-existing warning alert for a non-output warning node (5.2)", async () => {
    mockAPIs({
      statusMap: { detect: { status: "warning", detail: "low confidence" } },
    });

    renderGraph();

    userEvent.click(await screen.findByTestId("node-detect"));

    await waitFor(() => {
      expect(screen.getByText(/low confidence/)).toBeInTheDocument();
    });
    expect(screen.queryByTestId("node-preview-card")).toBeNull();
  });
});
