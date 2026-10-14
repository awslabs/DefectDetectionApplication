/*
 * Scene analytics in the run results view (rtsp-rtmp-stream-cameras
 * Requirement 16.3): a section per detection counter, object association
 * and event gate, with violating detections badged in the detections table.
 *
 * Mocked like RunResults.test.tsx: the API at the module boundary, useAuth
 * and InteractableImage as stand-ins.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import RunResults from "./RunResults";

jest.mock("api/WorkflowRegistrationAPI");
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (props: { extraActions?: JSX.Element }): JSX.Element => (
    <div data-testid="interactable-image">{props.extraActions ?? null}</div>
  ),
}));

const EXECUTION_ID = "exec-1";

/** A continuous PPE run: no image output, a Detection_List and analytics. */
const METADATA = {
  trigger: { source: "continuous", frameSeq: 1042 },
  detections: [
    { id: "p1", label: "person", confidence: 0.91, x_min: 10, y_min: 10, x_max: 110, y_max: 310 },
    { id: "p2", label: "person", confidence: 0.88, x_min: 200, y_min: 12, x_max: 300, y_max: 320 },
    { id: "h1", label: "hardhat", confidence: 0.8, x_min: 20, y_min: 5, x_max: 90, y_max: 40 },
  ],
  counter: {
    count_1: { counts: { person: 2, hardhat: 1 }, total: 3, labels: { person: "Person", hardhat: "Hardhat" } },
  },
  association: {
    ppe: { subjects: 2, compliant: 1, violations: 1, missing: { hardhat: 1 }, violating_ids: ["p2"] },
  },
  event: {
    gate_1: {
      state: "active",
      transition: "activated",
      active_since: 1790000000150,
      consecutive_true: 3,
      consecutive_false: 0,
    },
  },
};

function renderResults(): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, cacheTime: Infinity },
      mutations: { retry: false, cacheTime: Infinity },
    },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/deployed-workflows/reg-1/executions/${EXECUTION_ID}/results`]}>
        <Routes>
          <Route
            path="/deployed-workflows/:registrationId/executions/:executionId/results"
            element={<RunResults />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function valueIn(section: HTMLElement, label: string): string {
  const labelElement = within(section).getByText(label);
  return labelElement.parentElement?.textContent?.replace(label, "") ?? "";
}

beforeEach(() => {
  (RegistrationAPI.getWorkflowExecutionResults as jest.Mock).mockResolvedValue({
    hasImageResults: false,
    captureId: null,
    images: [],
  });
  (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue(METADATA);
  (RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock).mockResolvedValue({
    count_1: { status: "success", detail: "person 2, hardhat 1" },
    ppe: { status: "success", detail: "1 of 2 compliant" },
  });
});

describe("scene analytics in the run results (16.3)", () => {
  it("renders the counter, association and event gate of a run without images", async () => {
    renderResults();
    const counter = await screen.findByTestId("counter-section-count_1");
    expect(within(counter).getByText("Detection counter: count_1")).toBeInTheDocument();
    expect(valueIn(counter, "Total")).toBe("3");
    expect(valueIn(counter, "Person")).toBe("2");
    expect(valueIn(counter, "Hardhat")).toBe("1");

    const association = screen.getByTestId("association-section-ppe");
    expect(within(association).getByText("1 violation")).toBeInTheDocument();
    expect(valueIn(association, "Subjects")).toBe("2");
    expect(valueIn(association, "Compliant")).toBe("1");
    expect(valueIn(association, "Violations")).toBe("1");
    expect(valueIn(association, "Missing hardhat")).toBe("1");

    const gate = screen.getByTestId("event-gate-section-gate_1");
    expect(within(gate).getByText("Active")).toBeInTheDocument();
    expect(valueIn(gate, "Transition")).toBe("Activated in this run");
    expect(valueIn(gate, "Consecutive runs true")).toBe("3");
    expect(valueIn(gate, "Consecutive runs false")).toBe("0");

    // The no-images state still shows, beside the analytics.
    expect(screen.getByText("This run produced no viewable image results.")).toBeInTheDocument();
  });

  it("badges the violating detections in the detections table", async () => {
    renderResults();
    await screen.findByTestId("association-section-ppe");
    const table = createWrapper(document.body).findTable();
    const violationCells = (table?.findRows() ?? []).map(
      (row) => row.getElement().querySelectorAll("td")[4]?.textContent,
    );
    expect(violationCells).toEqual(["-", "Violation", "-"]);
  });

  it("shows the reason when a counter could not be evaluated", async () => {
    (RegistrationAPI.getWorkflowExecutionNodeStatus as jest.Mock).mockResolvedValue({
      count_1: {
        status: "failure",
        detail: "the zone needs the frame size, which is unknown for this run",
      },
    });
    renderResults();
    const counter = await screen.findByTestId("counter-section-count_1");
    expect(
      await within(counter).findByText("the zone needs the frame size, which is unknown for this run"),
    ).toBeInTheDocument();
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).toHaveBeenCalledWith(EXECUTION_ID);
  });

  it("shows an association with no subjects as such", async () => {
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({
      detections: [],
      association: { ppe: { subjects: 0, compliant: 0, violations: 0, missing: {}, violating_ids: [] } },
    });
    renderResults();
    const association = await screen.findByTestId("association-section-ppe");
    expect(within(association).getByText("No subjects")).toBeInTheDocument();
  });

  it("adds nothing, and reads no node status, for a run without analytics", async () => {
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({
      detections: METADATA.detections,
    });
    renderResults();
    await screen.findByText("Objects detected");
    await waitFor(() => expect(RegistrationAPI.getWorkflowExecutionMetadata).toHaveBeenCalled());
    expect(screen.queryByTestId("counter-section-count_1")).toBeNull();
    expect(screen.queryByText("Association")).toBeNull();
    expect(RegistrationAPI.getWorkflowExecutionNodeStatus).not.toHaveBeenCalled();
  });
});
