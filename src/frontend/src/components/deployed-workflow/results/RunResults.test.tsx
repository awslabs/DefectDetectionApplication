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
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import * as RegistrationAPI from "api/WorkflowRegistrationAPI";
import type {
  WorkflowExecutionOverlay,
  WorkflowExecutionResults,
} from "api/WorkflowRegistrationAPI";
import RunResults from "./RunResults";

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
