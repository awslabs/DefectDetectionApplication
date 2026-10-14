/*
 * Unit tests for the deployed-workflow RunResults screen (task 11.1).
 *
 * Requirements 5.4 (overlay toggle behavior), 5.5 (plain image when no mask),
 * 5.6 (reuse InteractableImage), 5.7 (results-unavailable / error state).
 *
 * The API is mocked at the module boundary
 * (jest.mock("api/WorkflowRegistrationAPI")) and useAuth is mocked so the
 * component runs without the auth context. `InteractableImage` is mocked with a
 * lightweight stand-in that records the props it receives, so the tests can
 * assert the screen reuses that component and drives its mask/showMask props
 * from the overlay response.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecutionMetadata,
  WorkflowExecutionOverlay,
  WorkflowExecutionResults,
} from "api/WorkflowRegistrationAPI";
import RunResults, {
  BOUNDING_BOXES_TOGGLE_LABEL,
  OVERLAY_TOGGLE_LABEL,
} from "./RunResults";

jest.mock("api/WorkflowRegistrationAPI");

// useAuth reads from the auth context/cookies which are not provided in this
// test harness; mock it to a stable disabled-auth value.
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({
    token: "",
    authEnabled: false,
  }),
}));

// Record the props InteractableImage is rendered with so we can assert reuse
// (R5.6) and that the mask/toggle are wired from the overlay response.
const interactableImageProps: Array<Record<string, unknown>> = [];
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (props: Record<string, unknown>): JSX.Element => {
    interactableImageProps.push(props);
    return (
      <div data-testid="interactable-image">
        {(props.extraActions as JSX.Element) ?? null}
      </div>
    );
  },
}));

const REGISTRATION_ID = "reg-1";
const EXECUTION_ID = "exec-1";

function withOverlay(): WorkflowExecutionResults {
  return {
    hasImageResults: true,
    captureId: "cap-1",
    images: [{ kind: "output", hasOverlay: true }],
  };
}

function withoutOverlay(): WorkflowExecutionResults {
  return {
    hasImageResults: true,
    captureId: "cap-1",
    images: [{ kind: "output", hasOverlay: false }],
  };
}

const OVERLAY: WorkflowExecutionOverlay = {
  maskImage: "bWFzay1iYXNlNjQ=",
  maskBackground: {
    "class-name": "background",
    "rgb-color": [255, 255, 255],
    "total-percentage-area": 0,
  },
};

function renderResults(): void {
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
          `/deployed-workflows/${REGISTRATION_ID}/executions/${EXECUTION_ID}/results`,
        ]}
      >
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

beforeEach(() => {
  interactableImageProps.length = 0;
  jest.clearAllMocks();
  (
    RegistrationAPI.workflowExecutionOutputImageUrl as jest.Mock
  ).mockImplementation((id: string) => `/output-image/${id}`);
  (
    RegistrationAPI.workflowExecutionOverlayImageUrl as jest.Mock
  ).mockImplementation((id: string) => `/overlay-image/${id}`);
  // The metadata query now runs for every run; default to "no metadata".
  (
    RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock
  ).mockResolvedValue({});
});

describe("RunResults", () => {
  it("shows the overlay toggle and passes the mask when a mask exists (5.4, 5.6)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionResults as jest.Mock
    ).mockResolvedValue(withOverlay());
    (
      RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock
    ).mockResolvedValue(OVERLAY);

    renderResults();

    // Reuses the shared InteractableImage component (R5.6).
    await screen.findByTestId("interactable-image");
    // The overlay toggle is shown when a mask exists (R5.4).
    await waitFor(() => {
      expect(
        screen.getByTestId("refresh-display-show-anomaly-masks-toggle"),
      ).toBeInTheDocument();
    });

    // The most recent render received the mask prop derived from the overlay.
    const lastProps = interactableImageProps[interactableImageProps.length - 1];
    expect(lastProps.maskImage).toEqual({
      src: "data:image/png;base64, bWFzay1iYXNlNjQ=",
      backgroundColor: { r: 255, g: 255, b: 255 },
    });
    expect(lastProps.imageSrc).toBe(`/output-image/${EXECUTION_ID}`);
  });

  it("hides the toggle and shows the plain base image when there is no mask (5.5)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionResults as jest.Mock
    ).mockResolvedValue(withoutOverlay());

    renderResults();

    await screen.findByTestId("interactable-image");
    // No mask -> no overlay toggle (R5.5).
    expect(
      screen.queryByTestId("refresh-display-show-anomaly-masks-toggle"),
    ).toBeNull();
    // The overlay endpoint is not fetched when there is no overlay.
    expect(
      RegistrationAPI.getWorkflowExecutionOverlay,
    ).not.toHaveBeenCalled();

    const lastProps = interactableImageProps[interactableImageProps.length - 1];
    expect(lastProps.maskImage).toBeUndefined();
  });

  it("renders a results-unavailable state when the fetch fails (5.7)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionResults as jest.Mock
    ).mockRejectedValue(new Error("boom"));

    renderResults();

    await screen.findByText("Results unavailable for this run.");
    // No image renderer is mounted on the error path (no broken image / crash).
    expect(screen.queryByTestId("interactable-image")).toBeNull();
  });

  it("renders a no-results state when the run has no viewable images (5.7)", async () => {
    (
      RegistrationAPI.getWorkflowExecutionResults as jest.Mock
    ).mockResolvedValue({
      hasImageResults: false,
      captureId: null,
      images: [],
    });

    renderResults();

    await screen.findByText("This run produced no viewable image results.");
    expect(screen.queryByTestId("interactable-image")).toBeNull();
  });
});

// --------------------------------------------------------------------------
// run-detection-visibility (Requirements 1 and 2)
// --------------------------------------------------------------------------

/** A detection run as jetson-thor1 reports it: overlay image, no mask. */
function detectionRun(): WorkflowExecutionResults {
  return {
    hasImageResults: true,
    captureId: "cap-1",
    images: [{ kind: "output", hasOverlay: true, hasOverlayImage: true }],
  };
}

const NO_MASK: WorkflowExecutionOverlay = {
  maskImage: null,
  maskBackground: null,
};

const DETECTION_METADATA: WorkflowExecutionMetadata = {
  is_anomalous: 1,
  confidence: 0.963534,
  detection_count: 2,
  detections: [
    {
      label: "helmet",
      confidence: 0.9354003071784973,
      x_min: 92.18,
      y_min: 201.91,
      x_max: 169.87,
      y_max: 255.16,
      id: "de604231",
    },
    {
      label: "no-helmet",
      confidence: 0.6878951787948608,
      x_min: 297.77,
      y_min: 246.49,
      x_max: 364.27,
      y_max: 348.32,
      id: "0a583a31",
    },
  ],
};

function lastImageProps(): Record<string, unknown> {
  return interactableImageProps[interactableImageProps.length - 1];
}

function mockRun(opts: {
  results: WorkflowExecutionResults;
  overlay?: WorkflowExecutionOverlay | "pending" | "error";
  metadata?: WorkflowExecutionMetadata;
}): void {
  (
    RegistrationAPI.getWorkflowExecutionResults as jest.Mock
  ).mockResolvedValue(opts.results);
  const overlay = opts.overlay ?? NO_MASK;
  (
    RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock
  ).mockImplementation(() => {
    if (overlay === "pending") {
      return new Promise(() => undefined);
    }
    if (overlay === "error") {
      return Promise.reject(new Error("overlay failed"));
    }
    return Promise.resolve(overlay);
  });
  (
    RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock
  ).mockResolvedValue(opts.metadata ?? {});
}

describe("RunResults detection visibility", () => {
  it("shows the overlay image with a 'Show bounding boxes' toggle, on by default (1.1, 1.3)", async () => {
    mockRun({ results: detectionRun(), metadata: DETECTION_METADATA });

    renderResults();

    const toggle = await screen.findByTestId(
      "refresh-display-show-anomaly-masks-toggle",
    );
    await waitFor(() => {
      expect(toggle).toHaveTextContent(BOUNDING_BOXES_TOGGLE_LABEL);
    });
    expect(createWrapper().findToggle()?.findNativeInput().getElement()).toBeChecked();
    expect(lastImageProps().imageSrc).toBe(`/overlay-image/${EXECUTION_ID}`);
    // A server-rendered overlay gets no client-side mask composite.
    expect(lastImageProps().maskImage).toBeUndefined();
  });

  it("turning the toggle off shows the original frame; on again shows the overlay (1.2)", async () => {
    mockRun({ results: detectionRun(), metadata: DETECTION_METADATA });

    renderResults();

    await waitFor(() => {
      expect(lastImageProps()?.imageSrc).toBe(`/overlay-image/${EXECUTION_ID}`);
    });
    const input = () =>
      createWrapper().findToggle()!.findNativeInput().getElement();

    userEvent.click(input());
    await waitFor(() => {
      expect(lastImageProps().imageSrc).toBe(`/output-image/${EXECUTION_ID}`);
    });
    expect(lastImageProps().alt).toBe("Original captured frame of the run");

    userEvent.click(input());
    await waitFor(() => {
      expect(lastImageProps().imageSrc).toBe(`/overlay-image/${EXECUTION_ID}`);
    });
  });

  it("labels the toggle 'Show overlay' when the run has no Detection_List (1.3)", async () => {
    mockRun({ results: detectionRun(), metadata: { is_anomalous: 0 } });

    renderResults();

    const toggle = await screen.findByTestId(
      "refresh-display-show-anomaly-masks-toggle",
    );
    await waitFor(() => {
      expect(toggle).toHaveTextContent(OVERLAY_TOGGLE_LABEL);
    });
    expect(screen.queryByTestId("detected-objects-table")).toBeNull();
  });

  it("keeps the mask composite and its label when the run also has a mask (1.4)", async () => {
    mockRun({ results: detectionRun(), overlay: OVERLAY });

    renderResults();

    await waitFor(() => {
      expect(lastImageProps()?.maskImage).toEqual({
        src: "data:image/png;base64, bWFzay1iYXNlNjQ=",
        backgroundColor: { r: 255, g: 255, b: 255 },
      });
    });
    expect(lastImageProps().imageSrc).toBe(`/output-image/${EXECUTION_ID}`);
    expect(
      screen.getByTestId("refresh-display-show-anomaly-masks-toggle"),
    ).toHaveTextContent("Show anomaly masks");
  });

  it("shows the original frame and no toggle while a possible mask is loading (1.6)", async () => {
    mockRun({ results: detectionRun(), overlay: "pending" });

    renderResults();

    await screen.findByTestId("interactable-image");
    await waitFor(() => {
      expect(RegistrationAPI.getWorkflowExecutionOverlay).toHaveBeenCalled();
    });
    expect(lastImageProps().imageSrc).toBe(`/output-image/${EXECUTION_ID}`);
    expect(
      screen.queryByTestId("refresh-display-show-anomaly-masks-toggle"),
    ).toBeNull();
  });

  it("still shows the overlay image when the mask request fails, without the base-image warning", async () => {
    mockRun({
      results: detectionRun(),
      overlay: "error",
      metadata: DETECTION_METADATA,
    });

    renderResults();

    await waitFor(() => {
      expect(lastImageProps()?.imageSrc).toBe(`/overlay-image/${EXECUTION_ID}`);
    });
    expect(
      screen.queryByText("The overlay could not be loaded; showing the base image."),
    ).toBeNull();
  });

  it("lists the detected objects below the image (2.1, 2.2)", async () => {
    mockRun({ results: detectionRun(), metadata: DETECTION_METADATA });

    renderResults();

    const table = await screen.findByTestId("detected-objects-table");
    expect(table).toHaveTextContent("Objects detected");
    expect(table).toHaveTextContent("(2)");
    expect(table).toHaveTextContent("helmet 1 · no-helmet 1");
    const rows = createWrapper().findTable()!.findRows();
    expect(rows.map((row) => row.getElement().textContent)).toEqual([
      expect.stringContaining("helmet93.5%"),
      expect.stringContaining("no-helmet68.8%"),
    ]);
  });

  it("lists the detected objects even when the run has no viewable images (2.7)", async () => {
    mockRun({
      results: { hasImageResults: false, captureId: null, images: [] },
      metadata: DETECTION_METADATA,
    });

    renderResults();

    await screen.findByText("This run produced no viewable image results.");
    expect(await screen.findByTestId("detected-objects-table")).toHaveTextContent(
      "(2)",
    );
    expect(screen.queryByTestId("interactable-image")).toBeNull();
  });

  it("shows no table when the metadata carries no Detection_List (2.6)", async () => {
    mockRun({ results: withoutOverlay(), metadata: { is_anomalous: 0 } });

    renderResults();

    await screen.findByTestId("interactable-image");
    await waitFor(() => {
      expect(RegistrationAPI.getWorkflowExecutionMetadata).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("detected-objects-table")).toBeNull();
  });
});
