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
import { APIList } from "config/Interface";
import { ImageSource, PredictionType } from "components/image-source/types";
import {
  FeatureConfiguration,
  InputConfiguration,
  OutputConfiguration,
  Workflow,
  WorkflowCaptureTask,
} from "components/workflow/types";
import { OptionDefinition } from "@cloudscape-design/components/internal/components/option/interfaces";
import { getWorkflowOptionTags } from "components/utils";

export async function listWorkflows(): Promise<Workflow[]> {
  const endpoint = APIList.workflows;
  const { data } = await axios.get<Workflow[]>(endpoint);
  return data;
}

export async function filterWorkflows(
  cameraId?: string
): Promise<Workflow[]> {
  const endpoint = `${APIList.workflows}/?cameraId=${cameraId}`;
  const { data } = await axios.get<Workflow[]>(endpoint);
  return data;
}

// Using partial as we are expecting to only send parts of the resources
interface EditWorkflowRequest {
  name?: string;
  description?: string;
  inputConfigurations?: Partial<InputConfiguration>[];
  imageSources?: Partial<ImageSource>[];
  featureConfigurations?: Partial<FeatureConfiguration>[];
  outputConfigurations?: Partial<OutputConfiguration>[];
}
interface EditWorkflowResponse {
  workflowId: string;
}
export async function editWorkflow(
  id: string,
  request: EditWorkflowRequest,
): Promise<EditWorkflowResponse> {
  const endpoint = `${APIList.workflows}/${id}`;
  const { data } = await axios.patch<EditWorkflowResponse>(endpoint, request);
  return data;
}

export async function getWorkflow(id: string): Promise<Workflow> {
  const endpoint = `${APIList.workflows}/${id}`;
  const { data } = await axios.get<Workflow>(endpoint);
  return data;
}


export async function createWorkflow(): Promise<String> {
  const endpoint = APIList.workflows;
  const { data } = await axios.post<String>(endpoint);
  return data;
}

export async function deleteWorkflow(id: string): Promise<void> {
  const endpoint = `${APIList.workflows}/${id}`;
  await axios.delete<void>(endpoint);
}

export interface RunWorkflowRequest {
  returnImageString?: boolean;
  captureImageCount?: number;
  captureTimeInterval?: number;
  capturePrefix?: string;
}

export interface RunWorkflowResponse {
  creationTime: string;
  image: string;
  imageDataFilePath: string;
  inferenceFilePath: string;
  inferenceResult: InferenceResult;
  processingTime: number;
  outputImageFilePath?: string;
  captureId: string;
  humanReviewRequired?: boolean | null;
}
export interface InferenceResult {
  anomalies?: Anomalies;
  confidence: number;
  anomaly_score?: number;
  anomaly_threshold?: number;
  inference_result: PredictionType;
  mask_background: MaskBackground | null;
  mask_image: string | null;
}
export enum PredictionResult {
  ANOMALY = "Anomaly",
  NORMAL = "Normal",
  ALL = "all",
}
export interface RetrainInputImagesRequest {
  startTime: number;
  endTime: number;
  inputImageLimit: number;
  predictionResult: PredictionResult;
  token?: string;
}

export interface Anomalies {
  [index: string]: AnomalyLabel;
}
// if prop contains dash, need to wrap with single quotes
export interface AnomalyLabel {
  "class-name": string;
  "hex-color": string;
  "total-percentage-area": number;
}
export interface MaskBackground {
  "class-name": string;
  "rgb-color": number[];
  "total-percentage-area": number;
}
export async function runWorkflow(id: string, request?: RunWorkflowRequest): Promise<RunWorkflowResponse> {
  const endpoint = `${APIList.workflows}/${id}/run`;
  // default UI experience should be not return base64 string for input image
  const defaultRequest = { returnImageString: false }
  const { data } = await axios.post<RunWorkflowResponse>(endpoint,
    {
      ...defaultRequest,
      ...(request || {})
    });
  return data;
}

export async function retryDioWorkflow(id: string) {
  const endpoint = `${APIList.workflows}/${id}/retry`;
  return new Promise(async (resolve, reject) => {
    axios.get(endpoint).then(() => {
      resolve(0);
    }).catch((err: any) => {
      reject(err?.response?.status);
    })
  })
}

export function retrainInputImages(workflowId: string, config: RetrainInputImagesRequest): Promise<number> {
  const { startTime, endTime, inputImageLimit, predictionResult, token } = config;
  const retrainInputImagesAPI = `${APIList.workflows}/${workflowId}/results/export?startTime=${startTime}&endTime=${endTime}&predictionResult=${predictionResult}${token ? `&token=${encodeURIComponent(token)}` : ""}`
  const errorCheckingAPICall = `${retrainInputImagesAPI}&inputImageLimit=1`;
  const imageDownloadAPI = `${retrainInputImagesAPI}&inputImageLimit=${inputImageLimit}`;
  return new Promise(async (resolve, reject) => {
    axios.get(errorCheckingAPICall).then(() => {
      const link = document.createElement("a");
      link.href = imageDownloadAPI;
      link.click();
      resolve(0);
    }).catch((err: any) => {
      reject(err?.response?.status);
    })
  })
}

interface ListWorkflowImagesResponse {
  images: RunWorkflowResponse[];
}
export async function getWorkflowImages(
  id: string,
): Promise<RunWorkflowResponse[] | undefined> {
  const endpoint = `${APIList.workflows}/${id}/images?maxResults=1`;
  const { data } = await axios.get<ListWorkflowImagesResponse>(endpoint);
  return data?.images;
}

export async function getWorkflowOptionList(): Promise<OptionDefinition[]> {
  const endpoint = APIList.workflows;
  const { data } = await axios.get<Workflow[]>(endpoint);

  return data.map((r: Workflow): OptionDefinition => {
    // if workflow doesn't have name, display id instead
    return {
      label: r.name ?? r.workflowId,
      value: r.workflowId,
      description: r.description ?? "",
      tags: getWorkflowOptionTags(r),
    };
  });
}

export function runWorkflowTestData(id: string) {
  // TODO: change test data to real API response
  const data = {
    creationTime: "2023-04-10T16:25:17_6",
    image: "fakeimage",
    imageDataFilePath:
      "/aws_dda/greengrass/v2/em_agent/capture_data/E_img6_03-14T16:12:27_39-1.bmp",
    inferenceFilePath:
      "/aws_dda/greengrass/v2/em_agent/capture_data/E_jsonl_img6_0410-162517-30.jsonl",
    inferenceResult: {
      anomalies: {
        "1": {
          "class-name": "cracked",
          "hex-color": "#23a436",
          "total-percentage-area": 0.022438332438468933,
        },
      },
      confidence: 0.9956324696540833,
      anomaly_score: 0.0,
      anomaly_threshold: 0.0,
      inference_result: "Anomaly",
    },
  };
  return data;
}

export async function getWorkflowCaptureTask(workflowId: string): Promise<WorkflowCaptureTask> {
  const { data } = await axios.get<WorkflowCaptureTask>(APIList.workflowCaptureTaskAPI.replace("{workflow_id}", workflowId));
  return data;
}

export async function deleteWorkflowResult(workflowId: string, captureId: string): Promise<boolean> {
  try {
    await axios.delete(APIList.workflowResultAPI.replace("{workflow_id}", workflowId).replace("{capture_id}", captureId));
    return true;
  } catch (err: any) {
    return err;
  }
}