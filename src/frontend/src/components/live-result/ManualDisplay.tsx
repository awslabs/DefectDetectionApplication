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

import { SpaceBetween } from "@cloudscape-design/components";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RunWorkflowResponse, runWorkflow } from "api/WorkflowAPI";
import { Workflow } from "components/workflow/types";
import { useEffect, useState } from "react";
import ErrorAlert from "./ErrorAlert";
import { ImageSource, ImageSourceType } from "components/image-source/types";
import ManualDisplayContent from "components/live-result/ManualDisplayContent";
import { getImagePreview } from "api/ImagePreviewAPI";
import FeedbackRequiredAlert from "./FeedbackRequiredAlert";

const DEFAULT_ERROR_MESSAGE =
  "An issue occurred when attempting to run this workflow. You can try again or select another workflow.";

interface ManualDisplayProps {
  workflow: Workflow;
  imageSource: ImageSource;
}

export default function ManualDisplay({
  workflow,
  imageSource,
}: ManualDisplayProps): JSX.Element {
  const [workflowRun, setWorkflowRun] = useState<RunWorkflowResponse>();
  const [errorAlert, setErrorAlert] = useState("");
  const [isFolderEmpty, setIsFolderEmpty] = useState(false);
  /**
   * show output image by default
   * classification model will have output image only
   * segmentation model will have masked image as output
   */
  const [showMask, setShowMask] = useState(true);
  const queryClient = useQueryClient();
  const isFolderType = imageSource.type === ImageSourceType.Folder;
  const imageSourceId = imageSource.imageSourceId;

  const inputConfigurations = workflow?.inputConfigurations || [];
  const hasInputConfigurations =
    inputConfigurations.length === 0 ? false : true;

  const getNextImageNameMutation = useMutation({
    mutationFn: () => getImagePreview(imageSourceId),
  });

  const runMutation = useMutation({
    mutationFn: () => runWorkflow(workflow.workflowId),
    onSuccess: (data) => {
      // Save response in state so when next mutation starts, the previous data doesn't immediately disappear.
      setWorkflowRun(data);
      setErrorAlert("");
      setIsFolderEmpty(false);
      if (isFolderType) {
        getNextImageNameMutation.reset();
        getNextImageNameMutation.mutate();
      }
      queryClient.refetchQueries({
        queryKey: ["getResultSummary", workflow.workflowId],
      });
    },
    onError: (error: any) => {
      if (error?.response?.status === 442 && isFolderType) {
        setIsFolderEmpty(true);
        setWorkflowRun(undefined);
      } else {
        setErrorAlert(error?.response?.data?.message ?? DEFAULT_ERROR_MESSAGE);
      }
    },
  });

  useEffect(() => {
    if ((getNextImageNameMutation.error as any)?.response?.status === 442) {
      setIsFolderEmpty(true);
    }
  }, [getNextImageNameMutation.error]);

  useEffect(() => {
    if (isFolderType) {
      getNextImageNameMutation.mutate();
    }
    // Don't add getNextImageNameMutation to dependency list, it will cause infinite loop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFolderType]);

  return (
    <SpaceBetween size="l">
      {
        !!workflowRun?.humanReviewRequired && <FeedbackRequiredAlert feedbackBtnHref={`/history/${workflow.workflowId}`} />
      }
      <ManualDisplayContent
        workflow={workflow}
        workflowRun={workflowRun}
        isWorkflowRunning={runMutation.isLoading}
        onTriggerWorkflow={(): void => runMutation.mutate()}
        showMask={showMask}
        onClickAnomalyMaskToggle={(checked): void => setShowMask(checked)}
        onClickPreview={(): void => setWorkflowRun(undefined)}
        isFolderEmpty={isFolderEmpty}
        fileLocation={imageSource?.location}
        folderInfo={{
          nextImage: getNextImageNameMutation?.data?.imageFileName ?? "-",
        }}
      />
      {!!errorAlert && (
        <ErrorAlert
          errorText={errorAlert}
          workflowId={workflow.workflowId}
          hasInputConfigurations={hasInputConfigurations}
        />
      )}
    </SpaceBetween>
  );
}
