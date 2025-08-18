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

import { AnomalyLabel, MaskBackground } from "api/WorkflowAPI";
import { PredictionType } from "components/image-source/types";

export enum ImageCaptureResultType {
  INFERENCE = "Inference",
  CAPTURE = "Capture",
};

export enum HistoryResultPageType {
  INFERENCE_RESULT = "inference-result",
  CAPTURE_RESULT = "capture-result",
};

export interface InferenceResultHistory {
  confidence?: number | null;
  captureId: string;
  inferenceCreationTime?: number | null;
  anomalyScore?: number | null;
  maskImage?: string | null;
  maskBackground?: MaskBackground | null;
  inputImageFilePath?: string | null;
  modelId?: string | null;
  modelName?: string | null;
  prediction?: PredictionType | null;
  workflowId: string;
  anomalyLabels?: AnomalyLabel[] | null;
  anomalyThreshod?: number | null;
  outputImageFilePath?: string | null;
  downloaded?: boolean;
  flagForReview?: boolean;
  textNote?: string | null;
  humanClassification?: PredictionType | null;
  humanReviewRequired?: boolean | null;
  captureType?: ImageCaptureResultType;
}

export interface FilterColumnConfig {
  groupValuesLabel: string;
  propertyKey: ListInferenceFilters;
  propertyLabel: string;
}

export type FilterColumnEntities = FilterColumnConfig[];

export enum ListInferenceFilters {
  PREDICTION = "prediction",
  DOWNLOADED = "downloaded",
  TEXT_NOTE = "textNoteFilter",
  HUMAN_FEEDBACK = "humanClassificationProvided",
  FEEDBACK_REQUIRED = "humanReviewRequired",
}

export enum DownloadStatus {
  DOWNLOADED = "Downloaded",
  NOT_DOWNLOADED = "Not downloaded",
}

export enum FeedbackFilterOptions {
  WITH_FEEDBACK = "With feedback",
  WITHOUT_FEEDBACK = "Without feedback",
}

export enum FeedbackRequiredFilterOptions {
  YES = "Yes",
  NO = "No",
}

export enum ResultTableActionType {
  MARK_AS_DOWNLOADED = "mark-as-downloaded",
  UNMARK_AS_DOWNLOADED = "unmark-as-downloaded",
  SELECT_PAGE = "select-page",
  FLAG_FOR_REVIEW = "flag-for-review",
  REMOVE_REVIEW_FLAG = "remove-review-flag",
  DELETE = "delete",
}

export enum ResultTableFeedbackType {
  NORMAL = "Normal",
  ANOMALY = "Anomaly",
}