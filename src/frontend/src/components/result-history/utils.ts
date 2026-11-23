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

import { PropertyFilterToken } from "@cloudscape-design/collection-hooks";
import { DownloadStatus, FeedbackRequiredFilterOptions, FeedbackFilterOptions, ListInferenceFilters, HistoryResultPageType, InferenceResultHistory } from "./types";
import type { ExportManifestFormat } from "api/WorkflowAPI";

function isListInferenceFilters(value: string): value is ListInferenceFilters {
  return Object.values(ListInferenceFilters).includes(value as ListInferenceFilters);
}

export const getResultHistoryFilterParams = (tokens: readonly PropertyFilterToken[]): string[] => {
  return tokens.reduce((prev, cur) => {
    const { propertyKey, value } = cur;
    if (!!propertyKey && isListInferenceFilters(propertyKey)) {
      let parsedValue = value;
      if (propertyKey === ListInferenceFilters.DOWNLOADED) {
        if (parsedValue === DownloadStatus.DOWNLOADED) {
          parsedValue = true;
        } else if (parsedValue === DownloadStatus.NOT_DOWNLOADED) {
          parsedValue = false;
        }
      } else if (propertyKey === ListInferenceFilters.HUMAN_FEEDBACK) {
        if (parsedValue === FeedbackFilterOptions.WITH_FEEDBACK) {
          parsedValue = true;
        } else if (parsedValue === FeedbackFilterOptions.WITHOUT_FEEDBACK) {
          parsedValue = false;
        }
      } else if (propertyKey === ListInferenceFilters.FEEDBACK_REQUIRED) {
        if (parsedValue === FeedbackRequiredFilterOptions.YES) {
          parsedValue = true;
        } else if (parsedValue === FeedbackRequiredFilterOptions.NO) {
          parsedValue = false;
        }
      }
      prev.push(`${propertyKey}=${parsedValue}`);
    }
    return prev;
  }, [] as string[]);
}

export const isInferenceResultPage = (pageType: HistoryResultPageType): boolean =>
  pageType === HistoryResultPageType.INFERENCE_RESULT;

interface ExportUrlParams {
  workflowsEndpoint: string;
  workflowId: string;
  captureIdPath: string;
  authEnabled: boolean;
  token: string;
  /**
   * The export's manifest (rfdetr-seg-support Requirement 11.4): appended as
   * `&manifest_format=instance` after captureIdPath and before the token;
   * omitted for the default classification manifest, so today's URL is
   * unchanged.
   */
  manifestFormat?: ExportManifestFormat;
}

/**
 * The result-history download link for the retraining export route. The
 * captureIdPath is percent-encoded, and the credential is joined as its own
 * "&token=" parameter only when authorization is on, so the server receives
 * the path and (with auth on) the credential as two separate query parameters
 * (localserver-input-handling-hardening Item 3c).
 */
export const buildExportUrl = ({
  workflowsEndpoint,
  workflowId,
  captureIdPath,
  authEnabled,
  token,
  manifestFormat,
}: ExportUrlParams): string => {
  const base = `${workflowsEndpoint}/${workflowId}/results/export?captureIdPath=${encodeURIComponent(
    captureIdPath,
  )}`;
  const url = manifestFormat === "instance" ? `${base}&manifest_format=instance` : base;
  return authEnabled ? `${url}&token=${encodeURIComponent(token)}` : url;
};

/** Whether a selected result stores an instance mask the export can write. */
export const hasInstanceMasks = (items: ReadonlyArray<InferenceResultHistory>): boolean =>
  items.some((item) =>
    (item.detections ?? []).some(
      (entry) => typeof entry?.mask_rle === "string" && entry.mask_rle.length > 0,
    ),
  );