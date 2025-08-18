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

import ResultsLayout, { FolderInfo } from "./ResultsLayout";
import FolderPreview from "components/live-result/preview/FolderPreview";
import ImagePreview from "components/live-result/preview/ImagePreview";
import { Workflow } from "components/workflow/types";
import { ImageSourceType } from "components/image-source/types";
import { RunWorkflowResponse } from "api/WorkflowAPI";

export interface ManualDisplayContentProps {
  workflow: Workflow;
  workflowRun: RunWorkflowResponse | undefined;
  isWorkflowRunning: boolean;
  onTriggerWorkflow: () => void;
  showMask: boolean;
  onClickAnomalyMaskToggle: (checked: boolean) => void;
  onClickPreview: () => void;
  isFolderEmpty: boolean;
  fileLocation?: string;
  folderInfo: FolderInfo;
}

export default function ManualDisplayContent({
  workflow,
  workflowRun,
  isWorkflowRunning,
  onTriggerWorkflow,
  showMask,
  onClickAnomalyMaskToggle,
  onClickPreview,
  isFolderEmpty,
  fileLocation,
  folderInfo,
}: ManualDisplayContentProps): JSX.Element | null {
  // If image source is not folder, then it treat as camera.
  // For smart camera, we will add more camera type in the future
  const isFolderType =
    workflow.imageSources?.[0]?.type === ImageSourceType.Folder;
  const isCameraType = !isFolderType;

  /**
   * Show image result if workflow process is done
   */
  if (!!workflowRun) {
    return (
      <ResultsLayout
        workflow={workflow}
        workflowRun={workflowRun}
        showMask={showMask}
        manualTriggerConfig={{
          onClickAnomalyMaskToggle,
          onTriggerWorkflow,
          isWorkflowRunning,
          onClickPreview,
          folderInfo,
        }}
      />
    );
  }
  /**
   * For camera based image source, prior running inference
   */
  if (isCameraType) {
    /**
     * Show image preview
     */
    return (
      <ImagePreview
        imageSource={workflow.imageSources[0]}
        onTriggerWorkflow={onTriggerWorkflow}
        isWorkflowRunning={isWorkflowRunning}
        workflow={workflow}
      />
    );
  }

  /**
   * For folder based image source, prior running inference
   */
  if (isFolderType) {
    return (
      <FolderPreview
        onTriggerWorkflow={onTriggerWorkflow}
        isWorkflowRunning={isWorkflowRunning}
        isFolderEmpty={isFolderEmpty}
        fileLocation={fileLocation}
        folderInfo={folderInfo}
      />
    );
  }

  return null;
}
