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

import { FilteringProperty } from "@cloudscape-design/components/property-filter/interfaces";
import { DownloadStatus, FeedbackRequiredFilterOptions, FeedbackFilterOptions, FilterColumnEntities, ListInferenceFilters, HistoryResultPageType } from "./types";
import { PredictionType } from "components/image-source/types";
import { PropertyFilterOption } from "@cloudscape-design/collection-hooks";

export const NUMBER_CARDS_PER_ROW = {
  NARROW: 1,
  NORMAL: 2,
  WIDE: 3,
  EX_WIDE: 4,
};
export const RESULT_DEFAULT_PAGE_SIZE = 12;

export interface PageSizeOption {
  value: number;
  label?: string;
}

export const pageSizeOption: ReadonlyArray<PageSizeOption> = [
  { value: 12, label: "12 results" },
  { value: 24, label: "24 results" },
  { value: 48, label: "48 results" },
];

export const inferenceResultFilterablePropertyGroup: FilterColumnEntities = [
  {
    propertyKey: ListInferenceFilters.PREDICTION,
    groupValuesLabel: "Prediction values",
    propertyLabel: "Prediction",
  },
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    groupValuesLabel: "Download status",
    propertyLabel: "Download status",
  },
  {
    propertyKey: ListInferenceFilters.TEXT_NOTE,
    groupValuesLabel: "Notes",
    propertyLabel: "Notes",
  },
  {
    propertyKey: ListInferenceFilters.HUMAN_FEEDBACK,
    groupValuesLabel: "Human feedback",
    propertyLabel: "Human feedback",
  },
  {
    propertyKey: ListInferenceFilters.FEEDBACK_REQUIRED,
    groupValuesLabel: "Feedback required",
    propertyLabel: "Feedback required",
  }
];

export const captureResultFilterablePropertyGroup: FilterColumnEntities = [
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    groupValuesLabel: "Download status",
    propertyLabel: "Download status",
  },
  {
    propertyKey: ListInferenceFilters.TEXT_NOTE,
    groupValuesLabel: "Notes",
    propertyLabel: "Notes",
  },
]

export const filteringProperties = (pageType: HistoryResultPageType): readonly FilteringProperty[] =>
  (pageType === HistoryResultPageType.INFERENCE_RESULT ? inferenceResultFilterablePropertyGroup : captureResultFilterablePropertyGroup)
    .map((filterableProperty) => ({
      key: filterableProperty.propertyKey,
      propertyLabel: filterableProperty.propertyLabel,
      operators: ["="],
      groupValuesLabel: filterableProperty.groupValuesLabel,
    }));

export const inferenceResultFilteringOptions: PropertyFilterOption[] = [
  {
    propertyKey: ListInferenceFilters.PREDICTION,
    value: PredictionType.Normal,
  },
  {
    propertyKey: ListInferenceFilters.PREDICTION,
    value: PredictionType.Anomaly,
  },
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    value: DownloadStatus.DOWNLOADED,
  },
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    value: DownloadStatus.NOT_DOWNLOADED,
  },
  {
    propertyKey: ListInferenceFilters.HUMAN_FEEDBACK,
    value: FeedbackFilterOptions.WITH_FEEDBACK,
  },
  {
    propertyKey: ListInferenceFilters.HUMAN_FEEDBACK,
    value: FeedbackFilterOptions.WITHOUT_FEEDBACK,
  },
  {
    propertyKey: ListInferenceFilters.FEEDBACK_REQUIRED,
    value: FeedbackRequiredFilterOptions.YES,
  },
  {
    propertyKey: ListInferenceFilters.FEEDBACK_REQUIRED,
    value: FeedbackRequiredFilterOptions.NO,
  },
]

export const captureResultFilteringOptions: PropertyFilterOption[] = [
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    value: DownloadStatus.DOWNLOADED,
  },
  {
    propertyKey: ListInferenceFilters.DOWNLOADED,
    value: DownloadStatus.NOT_DOWNLOADED,
  },
]

export const filteringOptions = (pageType: HistoryResultPageType): PropertyFilterOption[] => {
  return pageType === HistoryResultPageType.INFERENCE_RESULT
    ? inferenceResultFilteringOptions
    : captureResultFilteringOptions;
}