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

import { ImageSource, WorkflowTriggerType } from "components/image-source/types";
import { ModelDefaultConfigs } from "components/model/types";

export enum WorkflowTrigger {
  RestApi = "RestApi",
  DigitalInput = "DigitalInput",
}

export enum SignalType {
  RisingEdge = "GPIO.RISING",
  FallingEdge = "GPIO.FALLING",
}

export enum Rule {
  AllResults = "All",
  Anomaly = "Anomaly",
  Normal = "Normal",
}

export enum FeatureConfigurationType {
  LFVModel = "LFVModel",
  TritonModel = "TritonModel",
  VllmModel = "VllmModel"
}

export enum WorkflowOptionTags {
  NOT_CONFIGURED = "Not configured",
  HAS_MODEL = "Has model",
  FOLDER_IMAGE_SOURCE = "Folder image source",
  CAMERA_IMAGE_SOURCE = "Camera image source",
}

export interface FeatureConfiguration {
  type: FeatureConfigurationType;
  status?: string;
  modelName: string;
  defaultConfiguration: Partial<ModelDefaultConfigs>;
}

/**
 * Returns true for feature configurations that can be assigned to a legacy
 * workflow. Legacy workflows cannot run vision-language / vLLM models, so
 * `VllmModel` entries are excluded (allow-list of `LFVModel` / `TritonModel`).
 *
 * This is the single shared definition of the legacy-model filter, reused by
 * `FeatureConfigurationAPI.listModels()` and the legacy workflow editor
 * (`EditWorkflow`) so the model-option filter has one source of truth. It lives
 * here alongside `FeatureConfigurationType` so consumers can filter without
 * depending on the API module.
 */
export function isAssignableModel(config: FeatureConfiguration): boolean {
  return config.type !== FeatureConfigurationType.VllmModel;
}

export interface InputConfiguration {
  inputConfigurationId: string;
  pin: string;
  triggerState: SignalType;
  debounceTime: number;
  creationTime: number;
}

export interface OutputConfiguration {
  outputConfigurationId: string;
  pin: string;
  signalType: SignalType;
  pulseWidth: number;
  rule: Rule;
  creationTime: number;
}

export interface Workflow {
  workflowId: string;
  name: string;
  description: string;
  inputConfigurations: InputConfiguration[];
  imageSourceId: string;
  imageSources: ImageSource[];
  featureConfigurations: FeatureConfiguration[] | null;
  outputConfigurations: OutputConfiguration[];
  workflowOutputPath: string;
  creationTime: number;
  lastUpdatedTime: number;
}

export enum WorkflowCaptureTaskStatus {
  FAILED = "Failed",
  COMPLETED = "Completed",
  RUNNING = "Running",
};

export interface WorkflowCaptureTask {
  capturedCount?: number;
  count?: number;
  interval?: number;
  prefix?: string;
  status?: WorkflowCaptureTaskStatus;
  captureId?: string;
  statusMessage?: string;
}

export type WorkflowMetaData = Workflow & {
  modelName: string,
  modelVersion?: string | null,
  hasModel: boolean,
  workflowTriggerType: WorkflowTriggerType | null,
  imageSource: ImageSource,
}