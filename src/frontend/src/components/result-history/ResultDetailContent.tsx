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
  PredictionType,
  WorkflowTriggerType,
} from "components/image-source/types";
import { AnomalyLabel } from "api/WorkflowAPI";
import { Workflow } from "components/workflow/types";
import { outputFileNameStyle } from "components/live-result/styles";
import { HistoryResultPageType, InferenceResultHistory } from "./types";
import {
  convertTimestampToLocalTime,
  getFeedbackRequiredValue,
} from "components/live-result/helpers";
import AnomalyLabels from "components/live-result/AnomalyLabels";
import CopyButton from "components/common/CopyButton";
import { useCallback } from "react";
import { isInferenceResultPage } from "./utils";

interface ResultDetailContentProps {
  inferenceResult: InferenceResultHistory;
  workflow: Workflow;
  historyResultPageType: HistoryResultPageType;
}

export default function ResultDetailContent({
  inferenceResult: {
    inferenceCreationTime,
    outputImageFilePath,
    modelName,
    prediction,
    anomalyLabels,
    humanReviewRequired,
    humanClassification,
    inputImageFilePath,
  },
  workflow,
  historyResultPageType,
}: ResultDetailContentProps): JSX.Element {
  const getClassificationType = useCallback(
    (classification: PredictionType | null | undefined): StatusIndicatorProps.Type =>
      classification === PredictionType.Normal ? "success" : "error"
    , []);
  const labelInfoList = Object.values(anomalyLabels ?? []);
  const isInferenceResultPageType = isInferenceResultPage(historyResultPageType);
  const outputFilePath = isInferenceResultPageType ? outputImageFilePath : inputImageFilePath;

  return (
    <SpaceBetween size="l">
      {
        isInferenceResultPageType ? (
          <>
            <ValueWithLabel label="Prediction">
              <StatusIndicator type={getClassificationType(prediction)}>{prediction}</StatusIndicator>
            </ValueWithLabel>
            {
              !!humanClassification
                ? (
                  <ValueWithLabel label="Human feedback">
                    <StatusIndicator type={getClassificationType(humanClassification)}>{humanClassification}</StatusIndicator>
                  </ValueWithLabel>
                )
                : (
                  <ValueWithLabel label="Feedback required">
                    {getFeedbackRequiredValue(humanReviewRequired)}
                  </ValueWithLabel>
                )
            }
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
              {convertTimestampToLocalTime(inferenceCreationTime)}
            </ValueWithLabel>
            <ValueWithLabel label="Model">{modelName}</ValueWithLabel>
          </>
        ) : (
          <ValueWithLabel label="Capture date">
            {convertTimestampToLocalTime(inferenceCreationTime)}
          </ValueWithLabel>
        )
      }
      <ValueWithLabel label="Workflow trigger">
        {workflow.inputConfigurations.length === 0
          ? WorkflowTriggerType.RESTAPI
          : WorkflowTriggerType.DigitalInput}
      </ValueWithLabel>
      <ValueWithLabel label="Output file">
        {outputFilePath ? (
          <CopyButton
            onCopy={(): void => {
              navigator.clipboard.writeText(outputFilePath);
            }}
            content={
              <span className={outputFileNameStyle}>
                {outputFilePath}
              </span>
            }
          />
        ) : (
          "-"
        )}
      </ValueWithLabel>
    </SpaceBetween>
  );
}
