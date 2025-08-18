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
  Box,
  ColumnLayout,
  StatusIndicator,
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";
import { PredictionType } from "components/image-source/types";
import { getFeedbackRequiredValue } from "components/live-result/helpers";
import { anomalylInferenceBoxStyle, normallInferenceBoxStyle } from "./styles";

interface InferenceBoxProps {
  prediction?: PredictionType | null;
  humanFeedbackRequired?: boolean | null;
}

export default function ColoredInferenceBox({
  prediction,
  humanFeedback,
  humanFeedbackRequired,
}: {
  prediction?: PredictionType | null;
  humanFeedback?: PredictionType | null;
  humanFeedbackRequired?: boolean | null;
}): JSX.Element {
  if (humanFeedback === PredictionType.Normal || humanFeedback === PredictionType.Anomaly) {
    return <HumanFeedbackInferenceBox humanFeedback={humanFeedback} />
  }
  return <InferenceBox prediction={prediction} humanFeedbackRequired={humanFeedbackRequired} />;
}

function HumanFeedbackInferenceBox({ humanFeedback }: { humanFeedback: PredictionType; }): JSX.Element {
  return (
    <div
      className={humanFeedback === PredictionType.Normal ? normallInferenceBoxStyle : anomalylInferenceBoxStyle}
      data-test-id="human-feedback-box"
    >
      <div className="result-section">
        <ColumnLayout disableGutters columns={1} minColumnWidth={100}>
          <Box padding="xs">
            <ValueWithLabel label="Human feedback">
              <ClassificationTypeTag classification={humanFeedback} />
            </ValueWithLabel>
          </Box>
        </ColumnLayout>
      </div>
    </div>
  );
}

function InferenceBox({ prediction, humanFeedbackRequired }: InferenceBoxProps): JSX.Element {
  if (!prediction) return <></>;
  return (
    <div className={prediction === PredictionType.Normal ? normallInferenceBoxStyle : anomalylInferenceBoxStyle}>
      <div className="result-section">
        <ColumnLayout disableGutters columns={2} minColumnWidth={150}>
          <Box padding="xs">
            <ValueWithLabel label="Prediction">
              <ClassificationTypeTag classification={prediction} />
            </ValueWithLabel>
          </Box>
          <Box padding="xs">
            <ValueWithLabel label="Feedback required">
              {getFeedbackRequiredValue(humanFeedbackRequired)}
            </ValueWithLabel>
          </Box>
        </ColumnLayout>
      </div>
    </div>
  );
}

function ClassificationTypeTag({ classification }: { classification: PredictionType }): JSX.Element {
  return classification === PredictionType.Normal
    ? <StatusIndicator>Normal</StatusIndicator>
    : <StatusIndicator type="error">Anomaly</StatusIndicator>
}