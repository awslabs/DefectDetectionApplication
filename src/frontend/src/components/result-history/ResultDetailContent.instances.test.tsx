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
 * stored Detection_List (list form) of a detection result - each object with
 * its confidence and, for an instance entry, its mask area - beside the
 * per-class coverage the row's `anomalyLabels` carry; a classification or
 * anomaly row shows no objects list.
 */
import { render, screen } from "@testing-library/react";
import ResultDetailContent, {
  OBJECTS_DETECTED_LABEL,
  formatDetectionEntry,
} from "./ResultDetailContent";
import { HistoryResultPageType } from "./types";
import type { InferenceResultHistory } from "./types";
import { PredictionType } from "components/image-source/types";
import type { Workflow } from "components/workflow/types";

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

function renderDetail(row: InferenceResultHistory): void {
  render(
    <ResultDetailContent
      workflow={WORKFLOW}
      inferenceResult={row}
      historyResultPageType={HistoryResultPageType.INFERENCE_RESULT}
    />,
  );
}

describe("formatDetectionEntry", () => {
  it("formats label, confidence and the mask area when present", () => {
    expect(formatDetectionEntry(INSTANCE_ROW.detections![0])).toBe("scratch — 90%, 16 px");
    expect(formatDetectionEntry(DETECTION_ROW.detections![0])).toBe("truck — 80%");
    expect(formatDetectionEntry({ class_index: "3", class_label: "", bounding_box: [], confidence: 0.5 })).toBe(
      "3 — 50%",
    );
  });
});

describe("ResultDetailContent", () => {
  it("lists the stored instances with their mask area beside the coverage labels", () => {
    renderDetail(INSTANCE_ROW);
    expect(screen.getByText(OBJECTS_DETECTED_LABEL)).toBeInTheDocument();
    expect(screen.getByText("scratch — 90%, 16 px")).toBeInTheDocument();
    expect(screen.getByText("dent — 60%, 9 px")).toBeInTheDocument();
    expect(screen.getByText("Anomaly labels")).toBeInTheDocument();
    expect(screen.getByText("scratch")).toBeInTheDocument();
    expect(screen.getByText("dent")).toBeInTheDocument();
  });

  it("lists a detection row's objects without a mask area", () => {
    renderDetail(DETECTION_ROW);
    expect(screen.getByText(OBJECTS_DETECTED_LABEL)).toBeInTheDocument();
    expect(screen.getByText("truck — 80%")).toBeInTheDocument();
  });

  it("shows no objects list for an anomaly row", () => {
    renderDetail(ANOMALY_ROW);
    expect(screen.queryByText(OBJECTS_DETECTED_LABEL)).toBeNull();
    expect(screen.getByText("crack")).toBeInTheDocument();
  });

  it("shows no objects list for a detection row without a stored list", () => {
    renderDetail({ ...BASE, prediction: PredictionType.Detection, detections: null });
    expect(screen.queryByText(OBJECTS_DETECTED_LABEL)).toBeNull();
  });
});
