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

import { ImageSourceType, WorkflowTriggerType } from "./image-source/types";
import { DynamicRouterHashKey } from "./layout/constants";
import { FeatureConfiguration, Workflow, WorkflowMetaData, WorkflowOptionTags } from "./workflow/types";

/**
 * Set hash values in url.
 * Return hash params string. e.g. "#keyA=valueA&keyB=valueB"
 */
export function setHashValuesInUrl(hashString: string, newHashValues: { [hashKey: string]: string }): string {
  const hashParams = new URLSearchParams(hashString);
  Object.entries(newHashValues).forEach(([key, value]) => {
    hashParams.set(key, value);
  });
  const hashValues = Array.from(hashParams.entries()).reduce((prev, cur) => {
    prev.push(cur.join("="));
    return prev;
  }, [] as string[]);
  return `#${hashValues.join("&")}`;
}

export function isDynamicRouterHashKey(value: string): value is DynamicRouterHashKey {
  // Treat the hash key as dynamic router hash key only if it exists in DynamicRouterHashKey enum
  return Object.values(DynamicRouterHashKey).includes(value as DynamicRouterHashKey);
}

export function isDynamicRouter(breadcrumbValue: string): boolean {
  return !!(breadcrumbValue.startsWith("#") && isDynamicRouterHashKey(breadcrumbValue.substring(1)));
}

export function getWorkflowModelOptionLabel(featureConfiguration: FeatureConfiguration): string {
  const { modelName = "-", defaultConfiguration = {} } = featureConfiguration || {};
  const { modelAlias = "-", modelVersion = "1" } = defaultConfiguration;
  return `${modelAlias} v${modelVersion} | ${modelName}`;
}

export function getWorkflowModelOptionLabelWithoutVersion(featureConfiguration: FeatureConfiguration): string {
  const { modelName = "-", defaultConfiguration = {} } = featureConfiguration || {};
  const { modelAlias = "-" } = defaultConfiguration;
  return `${modelAlias} | ${modelName}`;
}

export function sortWorkflowModelOptions(featureConfigurationA: FeatureConfiguration, featureConfigurationB: FeatureConfiguration): number {
  let { modelAlias: modelNameA, modelVersion: modelVersionA } = featureConfigurationA.defaultConfiguration || {};
  let { modelAlias: modelNameB, modelVersion: modelVersionB } = featureConfigurationB.defaultConfiguration || {};
  // default value in object destructure won't apply when value is null. set default value below to handle the case
  if (!modelNameA) modelNameA = "";
  if (!modelNameB) modelNameB = "";
  if (!modelVersionA) modelVersionA = "1";
  if (!modelVersionB) modelVersionB = "1";
  if (modelNameA > modelNameB) {
    return 1;
  } else if (modelNameA < modelNameB) {
    return -1;
  } else {
    return modelVersionA > modelVersionB ? -1 : 1;
  }
}

export function getModelNameWithVersion(modelName: string, modelVersion: string): string {
  if (!modelName) return "-";
  if (!modelVersion) return modelName;
  return `${modelName} v${modelVersion}`;
}

export function isBoolean(value: any): boolean {
  return typeof value === "boolean";
}

export function isWorkflowConfigured(workflow: Workflow): boolean {
  const { imageSources } = workflow || {};
  return !!workflow && !!imageSources?.[0];
}

export function isWorkflowModelAttached(workflow: Workflow): boolean {
  const { featureConfigurations } = workflow || {};
  return !!featureConfigurations && featureConfigurations.length > 0;
}

export function getWorkflowImageSourceType(workflow: Workflow): ImageSourceType | null {
  const { imageSources } = workflow || {};
  if (!imageSources?.[0]) return null;
  return imageSources[0].type;
}

export function getWorkflowTriggerType(workflow: Workflow): WorkflowTriggerType | null {
  const { inputConfigurations } = workflow || {};
  if (!inputConfigurations) return null;
  return inputConfigurations.length > 0 ? WorkflowTriggerType.DigitalInput : WorkflowTriggerType.RESTAPI;
}

export function getWorkflowOptionTags(workflow: Workflow): string[] {
  const tags = [];
  if (!isWorkflowConfigured(workflow)) {
    tags.push(WorkflowOptionTags.NOT_CONFIGURED);
  } else {
    if (isWorkflowModelAttached(workflow)) {
      tags.push(WorkflowOptionTags.HAS_MODEL);
    }
    const imageSourceType = getWorkflowImageSourceType(workflow);
    if (!!imageSourceType) {
      if (imageSourceType === ImageSourceType.Folder) {
        tags.push(WorkflowOptionTags.FOLDER_IMAGE_SOURCE);
      } else {
        tags.push(WorkflowOptionTags.CAMERA_IMAGE_SOURCE);
      }
    }
  }
  return tags;
}

export function getWorkflowMetadata(workflow: Workflow): WorkflowMetaData {
  const { featureConfigurations, imageSources } = workflow;
  const { modelAlias = "", modelVersion } = featureConfigurations?.[0]?.defaultConfiguration || {}
  const modelName = modelAlias || (featureConfigurations?.[0]?.modelName || "");
  const hasModel = isWorkflowModelAttached(workflow);
  const workflowTriggerType = getWorkflowTriggerType(workflow);
  const imageSource = imageSources?.[0];

  return {
    ...workflow,
    modelName,
    modelVersion,
    hasModel,
    workflowTriggerType,
    imageSource,
  }
}

export function isArvisCameraImageSource(imageSourceType: string): boolean {
  return imageSourceType === ImageSourceType.Camera;
}

export function isICamImageSource(imageSourceType: string): boolean {
  return imageSourceType === ImageSourceType.ICam;
}