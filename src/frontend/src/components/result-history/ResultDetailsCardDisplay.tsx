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

import { SpaceBetween, Spinner } from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { getWorkflow } from "api/WorkflowAPI";
import InteractableImage from "components/live-result/InteractableImage";
import {
  resultCardStyle,
  resultLayoutStyle,
} from "components/live-result/styles";
import ResultDetailContent from "./ResultDetailContent";
import { APIList } from "config/Interface";
import { captureImageType } from "components/live-result/types";
import {
  getMaskBackgroundColor,
  getMaskImageProp,
} from "components/live-result/helpers";
import { useState } from "react";
import RefreshDisplayActions from "components/live-result/RefreshDisplayActions";
import { HistoryResultPageType, InferenceResultHistory } from "./types";
import useAuth from "components/auth/authHook";
import FeedbackRequiredAlert from "components/live-result/FeedbackRequiredAlert";
import { isInferenceResultPage } from "./utils";

interface ResultDetailsCardDisplayProps {
  workflowId: string;
  captureId: string;
  inferenceResult: InferenceResultHistory;
  historyResultPageType: HistoryResultPageType;
}

export default function ResultDetailsCardDisplay({
  workflowId,
  captureId,
  inferenceResult,
  historyResultPageType,
}: ResultDetailsCardDisplayProps): JSX.Element {
  const [showMask, setShowMask] = useState(true);
  const getWorkflowQuery = useQuery({
    queryKey: ["getResultWorkflow", workflowId],
    queryFn: () => getWorkflow(workflowId),
  });
  const { token, authEnabled } = useAuth();

  const isInferenceResultPageType = isInferenceResultPage(historyResultPageType);

  if (
    getWorkflowQuery.isLoading ||
    !getWorkflowQuery.data ||
    !inferenceResult
  ) {
    return <Spinner size="big" />;
  }

  const getCaptureAPI = APIList.getCapture
    .replace("{workflow_id}", workflowId)
    .replace("{capture_id}", captureId);
  const getImageSrc = (): string => {
    return `${getCaptureAPI}/${captureImageType.INPUT_IMAGE}${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`;
  };
  const imageSrc = getImageSrc();
  const backgroundColorProp = getMaskBackgroundColor(
    inferenceResult.maskBackground ?? null
  );
  const maskImageProp = getMaskImageProp(
    inferenceResult.maskImage ?? null,
    backgroundColorProp
  );

  const extraActionForResult = (
    <RefreshDisplayActions
      showAnomalyMaskToggle={!!inferenceResult.maskImage}
      onClickAnomalyMaskToggle={(checked) => setShowMask(checked)}
      anomalyMaskToggleChecked={!!showMask}
      // Turn on after flag feature
      showFlagForReviewToggle={false}
    />
  );

  return (
    <SpaceBetween size="l">
      {
        !!inferenceResult?.humanReviewRequired
        && !inferenceResult?.humanClassification
        && <FeedbackRequiredAlert feedbackBtnHref={`/history/${workflowId}`} />
      }
      <div className={resultLayoutStyle}>
        <div className={resultCardStyle}>
          <ResultDetailContent
            workflow={getWorkflowQuery.data}
            inferenceResult={inferenceResult}
            historyResultPageType={historyResultPageType}
          />
        </div>
        {
          isInferenceResultPageType ? (
            <InteractableImage
              imageSrc={imageSrc}
              {...maskImageProp}
              showMask={!!showMask}
              extraActions={extraActionForResult}
            />
          ) : (
            <InteractableImage
              imageSrc={imageSrc}
            />
          )
        }

      </div>
    </SpaceBetween>
  );
}
