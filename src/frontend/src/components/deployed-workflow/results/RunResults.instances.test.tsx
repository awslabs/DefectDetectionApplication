/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
/**
 * rfdetr-seg-support Requirement 10.6, DD14: the run page renders the
 * instance-mask layer from the `/overlay` body's `instances` when some entry
 * carries a mask, keeps the detected-objects table from the run metadata (with
 * the `Mask area (px)` column when the metadata carries `mask_area`), and
 * shows neither for a detection run whose entries carry no mask.
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
import { INSTANCE_MASKS_HEADER } from "./InstanceMaskLayer";
import { MASK_AREA_HEADER } from "./DetectedObjectsTable";

jest.mock("api/WorkflowRegistrationAPI");
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (): JSX.Element => <div data-testid="interactable-image" />,
}));

const EXECUTION_ID = "exec-seg-1";

const RESULTS: WorkflowExecutionResults = {
  hasImageResults: true,
  captureId: "cap-1",
  images: [{ kind: "output", hasOverlay: true, hasOverlayImage: true }],
};

const INSTANCE_OVERLAY: WorkflowExecutionOverlay = {
  maskImage: "bWFzay1iYXNlNjQ=",
  maskBackground: { "class-name": "background", "rgb-color": [255, 255, 255], "total-percentage-area": 0.9 },
  instances: [
    { class_index: "0", class_label: "scratch", bounding_box: [3, 2, 8, 5], confidence: 0.9, mask_rle: "27 4 8 4 8 4 8 4 125", mask_area: 16 },
    { class_index: "1", class_label: "dent", bounding_box: [8, 5, 13, 9], confidence: 0.6, mask_area: 9, mask_truncated: true },
  ],
  detectionsTruncated: true,
};

const DETECTION_OVERLAY: WorkflowExecutionOverlay = {
  maskImage: null,
  maskBackground: null,
  instances: [
    { class_index: "7", class_label: "truck", bounding_box: [1, 2, 3, 4], confidence: 0.8 },
  ],
  detectionsTruncated: false,
};

const INSTANCE_METADATA = {
  detections: [
    { id: "a1", label: "scratch", confidence: 0.9, x_min: 3, y_min: 2, x_max: 8, y_max: 5, mask_area: 16 },
    { id: "b2", label: "dent", confidence: 0.6, x_min: 8, y_min: 5, x_max: 13, y_max: 9, mask_area: 9 },
  ],
};

const DETECTION_METADATA = {
  detections: [{ id: "c3", label: "truck", confidence: 0.8, x_min: 1, y_min: 2, x_max: 3, y_max: 4 }],
};

function renderResults(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
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

beforeEach(() => {
  jest.clearAllMocks();
  (RegistrationAPI.workflowExecutionOutputImageUrl as jest.Mock).mockImplementation(
    (id: string) => `/output-image/${id}`,
  );
  (RegistrationAPI.workflowExecutionOverlayImageUrl as jest.Mock).mockImplementation(
    (id: string) => `/overlay-image/${id}`,
  );
  (RegistrationAPI.getWorkflowExecutionResults as jest.Mock).mockResolvedValue(RESULTS);
});

describe("RunResults with an instance segmentation run", () => {
  it("renders the instance layer from the overlay's instances and the mask-area column", async () => {
    (RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock).mockResolvedValue(INSTANCE_OVERLAY);
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue(INSTANCE_METADATA);
    renderResults();
    await screen.findByTestId("interactable-image");
    await waitFor(() => {
      expect(screen.getByTestId("instance-masks-container")).toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: INSTANCE_MASKS_HEADER })).toBeInTheDocument();
    expect(screen.getByTestId("instance-layer-image")).toHaveAttribute(
      "src",
      `/output-image/${EXECUTION_ID}`,
    );
    expect(screen.getByTestId("instance-layer-summary")).toHaveTextContent(
      "1 of 2 detected objects with an instance mask; 1 mask was not stored by the device",
    );
    // The table still comes from the run metadata, now with the mask area.
    await screen.findByTestId("detected-objects-table");
    expect(screen.getByText(MASK_AREA_HEADER)).toBeInTheDocument();
    expect(screen.getByText("16")).toBeInTheDocument();
  });
});

describe("RunResults with a detection run", () => {
  it("renders no instance layer and today's table when no entry carries a mask", async () => {
    (RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock).mockResolvedValue(DETECTION_OVERLAY);
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue(DETECTION_METADATA);
    renderResults();
    await screen.findByTestId("interactable-image");
    await screen.findByTestId("detected-objects-table");
    await waitFor(() => {
      expect(RegistrationAPI.getWorkflowExecutionOverlay).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.queryByText(INSTANCE_MASKS_HEADER)).toBeNull();
    expect(screen.queryByText(MASK_AREA_HEADER)).toBeNull();
  });

  it("tolerates an overlay body without the instance keys", async () => {
    (RegistrationAPI.getWorkflowExecutionOverlay as jest.Mock).mockResolvedValue({
      maskImage: null,
      maskBackground: null,
    });
    (RegistrationAPI.getWorkflowExecutionMetadata as jest.Mock).mockResolvedValue({});
    renderResults();
    await screen.findByTestId("interactable-image");
    await waitFor(() => {
      expect(RegistrationAPI.getWorkflowExecutionOverlay).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
  });
});
