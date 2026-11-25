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
 * rfdetr-seg-support Requirement 10.6 (live result, DD14): the live surface
 * renders the instance-mask layer from the inference-result JSON's
 * `detections` map (which carries `mask_rle`, 10.5a) over the raw input
 * image, with the layer's masks + boxes | boxes toggle, beside today's card
 * and server-rendered overlay. A detection or anomaly result renders exactly
 * today's surface: no layer.
 */
import { render, screen } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import ResultsLayout from "./ResultsLayout";
import { MASK_COVERAGE_LABEL } from "./LiveResultCard";
import {
  BOXES_ONLY_LABEL,
  INSTANCE_MASKS_HEADER,
  MASKS_AND_BOXES_LABEL,
} from "components/deployed-workflow/results/InstanceMaskLayer";
import type { RunWorkflowResponse } from "api/WorkflowAPI";
import { PredictionType } from "components/image-source/types";
import type { Workflow } from "components/workflow/types";

jest.mock("components/auth/authHook", () => ({
  __esModule: true,
  default: (): { token: string; authEnabled: boolean } => ({ token: "", authEnabled: false }),
}));
jest.mock("components/live-result/InteractableImage", () => ({
  __esModule: true,
  default: ({ imageSrc }: { imageSrc: string }): JSX.Element => (
    <img data-testid="interactable-image" src={imageSrc} alt="" />
  ),
}));

const WORKFLOW = {
  workflowId: "wf-1",
  name: "wf",
  description: "",
  inputConfigurations: [],
  imageSourceId: "src-1",
  imageSources: [],
  featureConfigurations: [{ modelName: "seg-model", defaultConfiguration: { modelAlias: "Seg", modelVersion: "1" } }],
  outputConfigurations: [],
  workflowOutputPath: "/out",
  creationTime: 0,
  lastUpdatedTime: 0,
} as unknown as Workflow;

function run(inferenceResult: Record<string, unknown>): RunWorkflowResponse {
  return {
    creationTime: "2025-01-01T00:00:00_1",
    image: "",
    imageDataFilePath: "/aws_dda/out/cap.jpg",
    inferenceFilePath: "/aws_dda/out/cap.jsonl",
    processingTime: 12,
    captureId: "cap",
    inferenceResult,
  } as unknown as RunWorkflowResponse;
}

const INSTANCE_RESULT = {
  confidence: 0.9,
  inference_result: PredictionType.Detection,
  anomaly_score: 0.9,
  anomaly_threshold: 1.0,
  detections: {
    "0": { class_index: "0", class_label: "scratch", bounding_box: [3, 2, 8, 5], confidence: 0.9, mask_rle: "27 4 8 4 8 4 8 4 125", mask_area: 16 },
    "1": { class_index: "2", class_label: "dent", bounding_box: [8, 5, 13, 9], confidence: 0.6, mask_area: 9, mask_truncated: true },
  },
  detections_truncated: true,
  detection_count: 2,
  mask_image: "bWFzaw==",
  mask_background: { "class-name": "background", "rgb-color": [255, 255, 255], "total-percentage-area": 0.8 },
  anomalies: {
    "1": { "class-name": "scratch", "hex-color": "#ff0000", "total-percentage-area": 0.12 },
    "2": { "class-name": "dent", "hex-color": "#00ff00", "total-percentage-area": 0.08 },
  },
};
const DETECTION_RESULT_WITHOUT_MAP = {
  confidence: 0.8,
  inference_result: PredictionType.Detection,
  anomaly_score: 0.8,
  anomaly_threshold: 1.0,
  mask_image: null,
  mask_background: null,
};
const DETECTION_RESULT = {
  ...DETECTION_RESULT_WITHOUT_MAP,
  detections: {
    "0": { class_index: "7", class_label: "truck", bounding_box: [1, 2, 3, 4], confidence: 0.8 },
  },
  detection_count: 1,
};
const ANOMALY_RESULT = {
  confidence: 0.7,
  inference_result: PredictionType.Anomaly,
  anomaly_score: 0.7,
  anomaly_threshold: 0.5,
  mask_image: "bWFzaw==",
  mask_background: { "class-name": "background", "rgb-color": [255, 255, 255], "total-percentage-area": 0.8 },
  anomalies: {
    "1": { "class-name": "crack", "hex-color": "#0000ff", "total-percentage-area": 0.2 },
  },
};

const CAPTURE_PATH = "/workflows/wf-1/capture-details/cap";

describe("ResultsLayout with an instance segmentation result", () => {
  it("renders the instance layer over the input image beside today's card and overlay", () => {
    render(<ResultsLayout workflow={WORKFLOW} workflowRun={run(INSTANCE_RESULT)} showMask />);
    expect(screen.getByTestId("instance-masks-container")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: INSTANCE_MASKS_HEADER })).toBeInTheDocument();
    // The layer draws its own masks and boxes, so it sits on the raw input
    // image whatever the overlay toggle says ...
    expect(screen.getByTestId("instance-layer-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/input-image`),
    );
    // ... while today's image keeps the server-rendered overlay for the
    // "Show bounding boxes" toggle.
    expect(screen.getByTestId("interactable-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/output-image`),
    );
    const segments = createWrapper(document.body).findSegmentedControl();
    expect(segments?.findSegments().map((segment) => segment.getElement().textContent)).toEqual([
      MASKS_AND_BOXES_LABEL,
      BOXES_ONLY_LABEL,
    ]);
    expect(segments?.findSelectedSegment()?.getElement().textContent).toBe(MASKS_AND_BOXES_LABEL);
    expect(screen.getByTestId("instance-layer-summary")).toHaveTextContent(
      "1 of 2 detected objects with an instance mask; 1 mask was not stored by the device",
    );
    // The card still lists the objects and the per-class coverage.
    expect(screen.getByText("Objects detected")).toBeInTheDocument();
    expect(screen.getByText(/scratch — 90%/)).toBeInTheDocument();
    expect(screen.getByText(MASK_COVERAGE_LABEL)).toBeInTheDocument();
  });

  it("keeps the layer on the input image when the overlay toggle is off", () => {
    render(<ResultsLayout workflow={WORKFLOW} workflowRun={run(INSTANCE_RESULT)} showMask={false} />);
    expect(screen.getByTestId("instance-layer-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/input-image`),
    );
    expect(screen.getByTestId("interactable-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/input-image`),
    );
  });
});

describe("ResultsLayout with a detection or anomaly result", () => {
  it("renders today's surface for a detection result whose entries carry no mask", () => {
    render(<ResultsLayout workflow={WORKFLOW} workflowRun={run(DETECTION_RESULT)} showMask />);
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.queryByText(INSTANCE_MASKS_HEADER)).toBeNull();
    expect(createWrapper(document.body).findSegmentedControl()).toBeNull();
    expect(screen.getByTestId("interactable-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/output-image`),
    );
    expect(screen.getByText("Objects detected")).toBeInTheDocument();
    expect(screen.getByText(/truck — 80%/)).toBeInTheDocument();
    expect(screen.queryByText(MASK_COVERAGE_LABEL)).toBeNull();
  });

  it("renders today's surface for a detection result without a detections map", () => {
    render(<ResultsLayout workflow={WORKFLOW} workflowRun={run(DETECTION_RESULT_WITHOUT_MAP)} showMask />);
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(screen.getByText("Objects detected")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("renders today's surface for an anomaly result", () => {
    render(<ResultsLayout workflow={WORKFLOW} workflowRun={run(ANOMALY_RESULT)} showMask />);
    expect(screen.queryByTestId("instance-masks-container")).toBeNull();
    expect(createWrapper(document.body).findSegmentedControl()).toBeNull();
    expect(screen.getByTestId("interactable-image")).toHaveAttribute(
      "src",
      expect.stringContaining(`${CAPTURE_PATH}/input-image`),
    );
    expect(screen.getByText("Anomaly labels")).toBeInTheDocument();
    expect(screen.getByText("crack")).toBeInTheDocument();
    expect(screen.queryByText("Objects detected")).toBeNull();
  });
});
