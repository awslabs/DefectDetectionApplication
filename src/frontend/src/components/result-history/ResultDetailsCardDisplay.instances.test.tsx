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
 * rfdetr-seg-support Requirement 10.6: the result-history detail renders the
 * instance-mask layer from the row's stored `detections` (list form) when some
 * entry carries a `mask_rle`, beside the per-class coverage the row's
 * `anomalyLabels` carry. A detection or anomaly row's page is today's: no
 * layer, no extra section in the detail text.
 */
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as WorkflowAPI from "api/WorkflowAPI";
import ResultDetailsCardDisplay from "./ResultDetailsCardDisplay";
import { HistoryResultPageType } from "./types";
import type { InferenceResultHistory } from "./types";
import { INSTANCE_MASKS_HEADER } from "components/deployed-workflow/results/InstanceMaskLayer";
import { PredictionType } from "components/image-source/types";
import type { Workflow } from "components/workflow/types";

jest.mock("api/WorkflowAPI");
jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: (): JSX.Element => <div data-testid="interactable-image" />,
}));

const WORKFLOW = {
  workflowId: "wf-1",
  name: "wf",
  description: "",
  inputConfigurations: [],
  imageSourceId: "src-1",
  imageSources: [],
  featureConfigurations: null,
  outputConfigurations: [],
  workflowOutputPath: "/out",
  creationTime: 0,
  lastUpdatedTime: 0,
} as unknown as Workflow;

const BASE: InferenceResultHistory = {
  captureId: "cap-1",
  workflowId: "wf-1",
  inferenceCreationTime: 1700000000,
  modelName: "model",
  outputImageFilePath: "/aws_dda/out/cap-1.jpg",
};

const INSTANCE_ROW: InferenceResultHistory = {
  ...BASE,
  prediction: PredictionType.Detection,
  detections: [
    { class_index: "0", class_label: "scratch", bounding_box: [3, 2, 8, 5], confidence: 0.9, mask_rle: "27 4 8 4 8 4 8 4 125", mask_area: 16 },
    { class_index: "2", class_label: "dent", bounding_box: [8, 5, 13, 9], confidence: 0.6, mask_area: 9, mask_truncated: true },
  ],
  anomalyLabels: [
    { "class-name": "scratch", "hex-color": "#ff0000", "total-percentage-area": 0.12 },
    { "class-name": "dent", "hex-color": "#00ff00", "total-percentage-area": 0.08 },
  ],
};

const DETECTION_ROW: InferenceResultHistory = {
  ...BASE,
  prediction: PredictionType.Detection,
  detections: [{ class_index: "7", class_label: "truck", bounding_box: [1, 2, 3, 4], confidence: 0.8 }],
};

const ANOMALY_ROW: InferenceResultHistory = {
  ...BASE,
  prediction: PredictionType.Anomaly,
  anomalyLabels: [{ "class-name": "crack", "hex-color": "#0000ff", "total-percentage-area": 0.2 }],
};

/** The detail's labels today (`ResultDetailContent`, inference-result page). */
const TODAYS_DETAIL_LABELS = [
  "Prediction",
  "Feedback required",
  "Anomaly labels",
  "Result date",
  "Model",
  "Workflow trigger",
  "Output file",
];

function renderCard(
  row: InferenceResultHistory,
  pageType: HistoryResultPageType = HistoryResultPageType.INFERENCE_RESULT,
): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    logger: { log: () => undefined, warn: () => undefined, error: () => undefined },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ResultDetailsCardDisplay
        workflowId="wf-1"
        captureId={row.captureId}
        inferenceResult={row}
        historyResultPageType={pageType}
      />
    </QueryClientProvider>,
  );
}

function expectTodaysDetail(): void {
  for (const label of TODAYS_DETAIL_LABELS) {
    expect(screen.getByText(label)).toBeInTheDocument();
  }
  expect(screen.queryByText("Objects detected")).toBeNull();
}

beforeEach(() => {
  jest.clearAllMocks();
  (WorkflowAPI.getWorkflow as jest.Mock).mockResolvedValue(WORKFLOW);
});

describe("ResultDetailsCardDisplay with an instance segmentation row", () => {
  it("renders the instance layer from the stored list beside today's detail", async () => {
    renderCard(INSTANCE_ROW);
    await screen.findByTestId("interactable-image");
    expect(screen.getByTestId("instance-masks-container")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: INSTANCE_MASKS_HEADER })).toBeInTheDocument();
    expect(screen.getByTestId("instance-layer-image")).toHaveAttribute(
      "src",
      expect.stringContaining("/workflows/wf-1/capture-details/cap-1/input-image"),
    );
    expect(screen.getByTestId("instance-layer-summary")).toHaveTextContent(
      "1 of 2 detected objects with an instance mask; 1 mask was not stored by the device",
    );
    // The per-class coverage comes from the row's anomalyLabels, as today.
    expect(screen.getByText("scratch")).toBeInTheDocument();
    expect(screen.getByText("dent")).toBeInTheDocument();
    expectTodaysDetail();
  });

  it("renders no layer on the capture-result page", async () => {
    renderCard(INSTANCE_ROW, HistoryResultPageType.CAPTURE_RESULT);
    await screen.findByTestId("interactable-image");
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.queryByText("Objects detected")).toBeNull();
  });
});

describe("ResultDetailsCardDisplay with a detection or anomaly row", () => {
  it("keeps a detection row's page as today when no stored entry carries a mask", async () => {
    renderCard(DETECTION_ROW);
    await screen.findByTestId("interactable-image");
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.queryByText(INSTANCE_MASKS_HEADER)).toBeNull();
    expect(screen.queryByText("truck")).toBeNull();
    expectTodaysDetail();
  });

  it("keeps a detection row's page as today when the row stores no list", async () => {
    renderCard({ ...BASE, prediction: PredictionType.Detection, detections: null });
    await screen.findByTestId("interactable-image");
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expectTodaysDetail();
  });

  it("keeps an anomaly row's page as today", async () => {
    renderCard(ANOMALY_ROW);
    await screen.findByTestId("interactable-image");
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.getByText("crack")).toBeInTheDocument();
    expectTodaysDetail();
  });
});
