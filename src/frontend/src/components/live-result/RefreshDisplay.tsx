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
import ResultsLayout from "./ResultsLayout";
import { Workflow } from "components/workflow/types";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import { RunWorkflowResponse, getWorkflowImages } from "api/WorkflowAPI";
import ErrorAlert from "./ErrorAlert";
import { useEffect, useRef, useState } from "react";
import { resultCardStyle, resultImageContainerStyle, resultLayoutStyle } from "./styles";
import LiveResultCard from "./LiveResultCard";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import FeedbackRequiredAlert from "./FeedbackRequiredAlert";

const DEFAULT_ERROR_MESSAGE =
  "An issue occurred with the digital input monitor process. Click try again to reset the process";

interface RefreshDisplayProps {
  workflow: Workflow;
}

export default function RefreshDisplay({
  workflow,
}: RefreshDisplayProps): JSX.Element {
  const initialApiCall = useRef(true);
  const [showErrorMesage, setShowErrorMessage] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [isEmptyResult, setIsEmptyResult] = useState(false);
  /**
   * show output image by default
   * classification model will have output image only
   * segmentation model will have masked image as output
   */
  const [showMask, setShowMask] = useState(true);
  const [pause, setPause] = useState(false);

  const getQuery = useQuery({
    queryKey: ["getLatestWorkflowImage", workflow.workflowId],
    queryFn: () => {
      return new Promise<RunWorkflowResponse | null>((resolve, reject) => {
        getWorkflowImages(workflow.workflowId)
          .then(response => {
            setIsEmptyResult(!response || response.length === 0);
            resolve(response?.[0] || null);
          })
          .catch((err) => reject(err))
      })
    },
    refetchInterval: pause ? false : PREVIEW_REFRESH_INTERVAL_MS
  });

  const inputConfigurations = workflow?.inputConfigurations || [];
  const hasInputConfigurations = inputConfigurations.length === 0 ? false : true;

  useEffect(() => {
    if (!!getQuery.error) {
      // Show error message when query gets error
      setShowErrorMessage(true);
      setErrorMessage((getQuery.error as any).response?.data?.message || "");
      setPause(true);
    } else if (!!getQuery.data && !getQuery.error && !getQuery.isLoading) {
      // Remove error message only when query has data & no error & is not loading
      setShowErrorMessage(false);
    }
    if (initialApiCall.current && (!!getQuery.data || !!getQuery.error)) {
      initialApiCall.current = false;
    }
  }, [getQuery.data, getQuery.error, getQuery.isLoading]);

  // Make sure loading only show up while calling API first time
  if (initialApiCall.current && getQuery.isLoading) {
    return <Spinner size="big" />;
  }

  if (isEmptyResult) {
    return (
      <div className={resultLayoutStyle}>
        <div className={resultCardStyle}>
          <LiveResultCard workflow={workflow} />
        </div>
        <div className={resultImageContainerStyle}>
          <ImagePlaceholder
            placement="center"
            content={
              <>
                <Spinner size="big" />
                <p>Waiting for next digital input to process image</p>
              </>
            }
          />
        </div>
      </div>
    )
  }

  return (
    <SpaceBetween size="l">
      {
        !!getQuery.data?.humanReviewRequired && <FeedbackRequiredAlert feedbackBtnHref={`/history/${workflow.workflowId}`} />
      }
      {!!getQuery.data && (
        <ResultsLayout
          workflow={workflow}
          workflowRun={getQuery.data}
          showMask={showMask}
          digitalInputConfig={{
            onClickAnomalyMaskToggle: (checked) => setShowMask(checked),
          }}
        />
      )}
      {showErrorMesage && (
        <ErrorAlert
          errorText={
            errorMessage ??
            DEFAULT_ERROR_MESSAGE
          }
          workflowId={workflow.workflowId}
          hasInputConfigurations={hasInputConfigurations}
          onRetryTriggered={(): void => setPause(false)}
        />
      )}
    </SpaceBetween>
  );
}
