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

import {
  SpaceBetween,
  StatusIndicator,
  StatusIndicatorProps,
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import {
  ImageSourceType,
  PredictionType,
  WorkflowTriggerType,
} from "components/image-source/types";
import { AnomalyLabel, RunWorkflowResponse } from "api/WorkflowAPI";
import AnomalyLabels from "./AnomalyLabels";
import { Workflow } from "components/workflow/types";
import {
  formatInferenceTime,
  formatProcessingTime,
  getFeedbackRequiredValue,
} from "./helpers";
import { outputFileNameStyle } from "components/live-result/styles";
import CopyButton from "components/common/CopyButton";
import { getModelNameWithVersion } from "components/utils";

interface LiveResultCardProps {
  workflowRun?: RunWorkflowResponse;
  workflow: Workflow;
}

export default function LiveResultCard({
  workflowRun,
  workflow,
}: LiveResultCardProps): JSX.Element {
  const { creationTime, inferenceResult, processingTime, imageDataFilePath, humanReviewRequired } =
    workflowRun || {};
  const featureConfigurations = workflow.featureConfigurations;
  const modelName = featureConfigurations
    ? featureConfigurations[0]?.defaultConfiguration?.modelAlias ||
    featureConfigurations[0]?.modelName
    : "";
  const modelVersion = featureConfigurations?.[0]?.defaultConfiguration?.modelVersion || "";
  const { inference_result, anomalies } = inferenceResult || {};
  const prediction = inference_result;
  let predictionType: StatusIndicatorProps.Type | undefined;
  if (!!prediction) {
    predictionType = prediction === PredictionType.Normal ? "success" : "error";
  }
  const labelInfoList = Object.values(anomalies ?? []);

  return (
    <SpaceBetween size="l">
      <ValueWithLabel label="Prediction">
        {!!predictionType ? (
          <StatusIndicator type={predictionType}>{prediction}</StatusIndicator>
        ) : (
          "-"
        )}
      </ValueWithLabel>
      <ValueWithLabel label="Feedback required">
        {getFeedbackRequiredValue(humanReviewRequired)}
      </ValueWithLabel>
      <ValueWithLabel label="Anomaly labels">
        {labelInfoList?.length > 0
          ? labelInfoList.map((label: AnomalyLabel) => {
            return (
              <AnomalyLabels key={label["hex-color"]} labelInfo={label} />
            );
          })
          : "-"}
      </ValueWithLabel>
      <ValueWithLabel label="Result date">
        {formatInferenceTime(creationTime)}
      </ValueWithLabel>
      <ValueWithLabel label="Model">{getModelNameWithVersion(modelName, modelVersion)}</ValueWithLabel>
      <ValueWithLabel label="Workflow trigger">
        {workflow.inputConfigurations.length === 0
          ? WorkflowTriggerType.RESTAPI
          : WorkflowTriggerType.DigitalInput}
      </ValueWithLabel>
      {workflow.inputConfigurations.length === 0 && (
        <ValueWithLabel label="Processing time">
          {formatProcessingTime(processingTime)}
        </ValueWithLabel>
      )}
      {workflow.imageSources?.[0]?.type === ImageSourceType.Folder && (
        <ValueWithLabel label="Output file">
          {imageDataFilePath ? (
            <CopyButton
              onCopy={(): void => {
                navigator.clipboard.writeText(imageDataFilePath);
              }}
              content={
                <span className={outputFileNameStyle}>{imageDataFilePath}</span>
              }
            />
          ) : (
            "-"
          )}
        </ValueWithLabel>
      )}
    </SpaceBetween>
  );
}
