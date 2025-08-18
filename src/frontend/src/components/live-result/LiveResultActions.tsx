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

import { Box, Button, SpaceBetween, Toggle } from "@cloudscape-design/components";
import ImageNameDisplay from "./ImageNameDisplay";
import { ImageSourceType } from "components/image-source/types";
import { FolderInfo } from "./ResultsLayout";
import { inferenceActionButtonStyle } from "./styles";

interface LiveResultActionsProps {
  showAnomalyMaskToggle?: boolean;
  onTriggerWorkflow: () => void;
  onClickAnomalyMaskToggle: (checked: boolean) => void;
  imageSourceType: ImageSourceType;
  anomalyMaskToggleChecked?: boolean;
  isWorkflowRunning: boolean;
  onClickPreview: () => void;
  folderInfo?: FolderInfo;
}

export default function LiveResultActions({
  showAnomalyMaskToggle,
  onClickAnomalyMaskToggle,
  anomalyMaskToggleChecked,
  onTriggerWorkflow,
  isWorkflowRunning,
  imageSourceType,
  onClickPreview,
  folderInfo,
}: LiveResultActionsProps): JSX.Element {
  const isFolderType = imageSourceType === ImageSourceType.Folder;

  return (
    <Box float="right">
      <SpaceBetween direction="vertical" size="xs">
        <div className={inferenceActionButtonStyle}>
          {
            showAnomalyMaskToggle && (
              <Toggle
                onChange={({ detail }): void =>
                  onClickAnomalyMaskToggle?.(detail.checked)
                }
                checked={!!anomalyMaskToggleChecked}
                data-testid={"show-anomaly-mask-togle"}
              >
                Show anomaly masks
              </Toggle>
            )
          }
          {
            isFolderType ? (
              <Button
                variant="primary"
                onClick={onTriggerWorkflow}
                loading={isWorkflowRunning}
              >
                Process next image
              </Button>
            ) : (
              <Button
                variant="normal"
                onClick={onClickPreview}
                loading={isWorkflowRunning}
              >
                Preview next image
              </Button>
            )
          }
        </div>
        {
          isFolderType && !!folderInfo?.nextImage && <ImageNameDisplay imageName={folderInfo.nextImage} />
        }
      </SpaceBetween>
    </Box>
  );
}