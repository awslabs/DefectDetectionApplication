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

import * as React from "react";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import { SpaceBetween } from "@cloudscape-design/components";
import CaptureImageDisplay from "./CaptureImageDisplay";
import CapturedCards from "./CapturedCards";
import Toggle from "@cloudscape-design/components/toggle";
import OverflowScrollBox from "components/common/OverflowScrollBox";
import { styleConstants } from "components/layout/constants";
import { CameraStatus } from "../types";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import useCameraConnection from "components/hook/useCameraConnection";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";

export const SelectedCameraContext = React.createContext<any | null>(null);
export const CAPTURED_IMAGE_PER_PAGE = 4;

interface CaptureImageProps {
  imgSrcId: string;
  capturePath: string;
  cameraStatus?: CameraStatus;
  cameraId: string;
  onCameraReconnect: () => void;
  workflowId: string;
}

export default function CaptureImage({
  imgSrcId,
  capturePath,
  cameraStatus,
  cameraId,
  onCameraReconnect,
  workflowId,
}: CaptureImageProps): JSX.Element {
  const [isLivePreviewChecked, setLivePreviewChecked] = React.useState(true);
  const { connect, isConnecting } = useCameraConnection({
    cameraId,
    recheckStatusFn: onCameraReconnect,
  })

  return (
    <>
      <SpaceBetween size="l">
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <Toggle
                  onChange={({ detail }): void => setLivePreviewChecked(detail.checked)}
                  checked={isLivePreviewChecked}
                >
                  Live preview
                </Toggle>
              }
            >
              Image preview
            </Header>
          }
        >
          <OverflowScrollBox contentMinWidth={styleConstants.IMAGE_CONTENT_CONTAINER_MIN_WIDTH}>
            {
              cameraStatus === CameraStatus.Connected
                ? (
                  <CaptureImageDisplay
                    isLivePreviewChecked={isLivePreviewChecked}
                    imgSrcId={imgSrcId}
                    capturePath={capturePath}
                  />
                )
                : (
                  <ImagePlaceholder
                    placement="center"
                    content={(
                      <CameraDisconnectedContent
                        message="Camera disconnected. Connect to the camera to continue capturing images."
                        loading={isConnecting}
                        onConnect={connect}
                      />
                    )}
                  />
                )
            }
          </OverflowScrollBox>
        </Container>
        <CapturedCards captureResultsHref={`/capture-results/${workflowId}`} workflowId={workflowId} />
      </SpaceBetween>
    </>
  );
}
