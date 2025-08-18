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
import axios from "axios";
import { PredictionType } from "components/image-source/types";
import { ImageCaptureResultType, InferenceResultHistory } from "components/result-history/types";
import { isBoolean } from "components/utils";
import { APIList } from "config/Interface";

interface ListWorkflowResultsResponse {
  total: number;
  page: number;
  size: number;
  results: InferenceResultHistory[];
}

interface GetInferenceResultSummaryStats {
  totalInference: number;
  normal: number;
  anomaly: number;
}
interface GetInferenceResultSummaryResponse {
  stats: GetInferenceResultSummaryStats;
  lastResetTime: number;
}

interface ResetInferenceResultActiveCounterRequest {
  resetTime: number;
}

interface ResetInferenceResultActiveCounterResponse {
  workflowId: string;
}

interface InferenceResultUpdate {
  captureId: string;
  flagForReview?: boolean;
  downloaded?: boolean;
  textNote?: string;
  humanClassification?: PredictionType;
}

interface UpdateInferenceResultsRequest {
  inferenceResults: InferenceResultUpdate[];
}

export async function listWorkflowResults({
  id,
  page,
  size,
  captureType,
  filterParamsStr,
}: {
  id: string;
  page: number;
  size: number;
  captureType?: ImageCaptureResultType;
  filterParamsStr?: string;
}): Promise<ListWorkflowResultsResponse> {
  const queryOptionsArr = [
    `page=${page}`,
    `size=${size}`,
  ];
  if (!!captureType) {
    queryOptionsArr.push(`captureType=${captureType}`)
  }
  if (!!filterParamsStr) {
    queryOptionsArr.push(filterParamsStr);
  }
  const endpoint = `${APIList.workflows}/${id}/results?${queryOptionsArr.join("&")}`;
  const { data } = await axios.get<ListWorkflowResultsResponse>(endpoint);
  return data;
}

export async function getInferenceResult(workflowId: string, captureId: string): Promise<InferenceResultHistory> {
  const endpoint = `${APIList.workflows}/${workflowId}/results/${captureId}`;
  const { data } = await axios.get<InferenceResultHistory>(endpoint);
  return data;
}

export async function getInferenceResultSummary(id: string): Promise<GetInferenceResultSummaryResponse> {
  const endpoint = `${APIList.workflows}/${id}/results/summary`;
  const { data } = await axios.get<GetInferenceResultSummaryResponse>(endpoint);
  return data;
}

export async function resetInferenceResultActiveCounter(id: string): Promise<ResetInferenceResultActiveCounterResponse> {
  const endpoint = `${APIList.workflows}/${id}/results/reset`;
  const request: ResetInferenceResultActiveCounterRequest = {
    // API uses 10 digits timestamp
    resetTime: Math.floor(new Date().getTime() / 1000)
  }
  const { data } = await axios.post<ResetInferenceResultActiveCounterResponse>(endpoint, request);
  return data;
}

export async function updateInferenceResults(
  workflowId: string,
  resultIds: string[],
  {
    flagForReview,
    downloaded,
    textNote,
    humanClassification,
  }:
    {
      flagForReview?: boolean;
      downloaded?: boolean;
      textNote?: string;
      humanClassification?: PredictionType;
    }): Promise<any> {
  const endpoint = `${APIList.workflows}/${workflowId}/results`;
  const requestBody: UpdateInferenceResultsRequest = {
    inferenceResults: resultIds.map(resultId => ({
      captureId: resultId,
      ...(isBoolean(flagForReview) ? { flagForReview } : {}),
      ...(isBoolean(downloaded) ? { downloaded } : {}),
      ...(!!textNote ? { textNote } : {}),
      ...(humanClassification !== undefined ? { humanClassification } : {}),
    }))
  };
  const { data } = await axios.patch(endpoint, requestBody);
  return data;
}

export async function getDownloadUrl(workflowId: string, resultIds: string[]): Promise<{ captureIdPath: string }> {
  const endpoint = `${APIList.workflows}/${workflowId}/results/export`;
  const { data } = await axios.post<{ captureIdPath: string }>(endpoint, {
    captureIds: resultIds
  })
  return data;
}