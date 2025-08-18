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

import { SpaceBetween, Box, Button, TextContent } from "@cloudscape-design/components";
import ImageNameDisplay from "components/live-result/ImageNameDisplay";
import { folderPreviewDescStyle, folderPreviewLayoutStyle } from "components/live-result/styles";
import { FolderInfo } from "../ResultsLayout";
import WarningAlert from "components/common/WarningAlert";

interface ManualDisplayFooterProps {
  onTriggerWorkflow: () => void;
  isWorkflowRunning: boolean;
  isFolderEmpty: boolean;
  fileLocation?: string;
  folderInfo: FolderInfo;
}

export default function FolderPreview({
  onTriggerWorkflow,
  isWorkflowRunning,
  isFolderEmpty,
  fileLocation,
  folderInfo,
}: ManualDisplayFooterProps): JSX.Element | null {
  /**
   * Show info alert when folder is empty
   */
  if (isFolderEmpty) {
    return (
      <WarningAlert header="No images found">
        {`The folder ${fileLocation} does not have any images to be processed.`}
      </WarningAlert>
    );
  }
  /**
   * Otherwise, show folder info with next image name
   */
  return (
    <div className={folderPreviewLayoutStyle}>
      <SpaceBetween direction="vertical" size="xs">
        <TextContent>
          <b>Folder path</b>
          <br />
          <span className={folderPreviewDescStyle}>
            This workflow uses a folder as its image source. This is the path where source images are stored.
          </span>
        </TextContent>
        <span>
          {fileLocation}
        </span>
      </SpaceBetween>
      <Box float="right">
        <SpaceBetween direction="vertical" size="xs">
          <Button
            variant="primary"
            loading={isWorkflowRunning}
            onClick={onTriggerWorkflow}
          >
            Start processing images
          </Button>
          <ImageNameDisplay imageName={folderInfo.nextImage} />
        </SpaceBetween>
      </Box>
    </div>
  );
}