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
 * rfdetr-seg-support Requirement 10.6: the live result card keeps its
 * detection / anomaly ternary and, for an instance segmentation result (a
 * detection result that also carries the per-class mask coverage as
 * `anomalies`), adds a "Mask coverage" list under "Objects detected". A plain
 * detection result shows no "Mask coverage"; an anomaly result is unchanged.
 */
import { render, screen } from "@testing-library/react";
import LiveResultCard, { MASK_COVERAGE_LABEL } from "./LiveResultCard";
import type { RunWorkflowResponse } from "api/WorkflowAPI";
import { PredictionType } from "components/image-source/types";
import type { Workflow } from "components/workflow/types";

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
    "1": { class_index: "2", class_label: "dent", bounding_box: [8, 5, 13, 9], confidence: 0.6, mask_rle: "1 2 3", mask_area: 9 },
  },
  detection_count: 2,
  mask_image: "bWFzaw==",
  mask_background: { "class-name": "background", "rgb-color": [255, 255, 255], "total-percentage-area": 0.8 },
  anomalies: {
    "1": { "class-name": "scratch", "hex-color": "#ff0000", "total-percentage-area": 0.12 },
    "2": { "class-name": "dent", "hex-color": "#00ff00", "total-percentage-area": 0.08 },
  },
};

const DETECTION_RESULT = {
  confidence: 0.8,
  inference_result: PredictionType.Detection,
  anomaly_score: 0.8,
  anomaly_threshold: 1.0,
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

describe("LiveResultCard", () => {
  it("shows both the objects and the mask coverage for an instance result", () => {
    render(<LiveResultCard workflowRun={run(INSTANCE_RESULT)} workflow={WORKFLOW} />);
    expect(screen.getByText("Objects detected")).toBeInTheDocument();
    expect(screen.getByText(/scratch — 90%/)).toBeInTheDocument();
    expect(screen.getByText(/dent — 60%/)).toBeInTheDocument();
    expect(screen.getByText(MASK_COVERAGE_LABEL)).toBeInTheDocument();
    // The coverage list reuses the anomaly-label swatches: one per class.
    expect(screen.getByText("scratch")).toBeInTheDocument();
    expect(screen.getByText("dent")).toBeInTheDocument();
    expect(screen.queryByText("Anomaly labels")).toBeNull();
  });

  it("shows no mask coverage for a plain detection result", () => {
    render(<LiveResultCard workflowRun={run(DETECTION_RESULT)} workflow={WORKFLOW} />);
    expect(screen.getByText("Objects detected")).toBeInTheDocument();
    expect(screen.getByText(/truck — 80%/)).toBeInTheDocument();
    expect(screen.queryByText(MASK_COVERAGE_LABEL)).toBeNull();
    expect(screen.queryByText("Anomaly labels")).toBeNull();
  });

  it("keeps the anomaly-labels section for an anomaly result", () => {
    render(<LiveResultCard workflowRun={run(ANOMALY_RESULT)} workflow={WORKFLOW} />);
    expect(screen.getByText("Anomaly labels")).toBeInTheDocument();
    expect(screen.getByText("crack")).toBeInTheDocument();
    expect(screen.queryByText("Objects detected")).toBeNull();
    expect(screen.queryByText(MASK_COVERAGE_LABEL)).toBeNull();
  });
});
