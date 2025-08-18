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

import { RESULT_DATA_COLUMN_WIDTH } from "components/live-result/constants";
import { useMemo, useState } from "react";
import { LiveResultContext } from "components/live-result/LiveResults";
import InteractableImage from "components/live-result/InteractableImage";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getImagePreview } from "api/ImagePreviewAPI";
import { CameraStatus, ImageSource } from "components/image-source/types";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import { Box, Button, SpaceBetween, Spinner } from "@cloudscape-design/components";
import { resultLayoutStyle, resultCardStyle } from "components/live-result/styles";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import LiveResultCard from "../LiveResultCard";
import { Workflow } from "components/workflow/types";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import useCameraConnection from "components/hook/useCameraConnection";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";
import { isArvisCameraImageSource, isICamImageSource } from "components/utils";

export default function ImagePreview({
  imageSource,
  onTriggerWorkflow,
  isWorkflowRunning,
  workflow,
}: {
  imageSource: ImageSource;
  onTriggerWorkflow: () => void;
  isWorkflowRunning?: boolean;
  workflow: Workflow;
}): JSX.Element {

  const [imageSrc, setImageSrc] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | undefined>(undefined);
  const queryClient = useQueryClient();

  const {
    connect,
    isConnecting,
  } = useCameraConnection({
    cameraId: imageSource.cameraId || "",
    recheckStatusFn: () => queryClient.invalidateQueries()
  })

  const enablePreviewQuery = imageSource.cameraStatus?.status === CameraStatus.Connected || isICamImageSource(imageSource.type);

  useQuery({
    queryKey: ["liveImagePreview", imageSource.imageSourceId],
    queryFn: async () => {
      return await getImagePreview(imageSource.imageSourceId, {});
    },
    onSuccess: (data) => {
      if (!!data.image) {
        setImageSrc(data.image);
      }
      setErrorMsg(undefined);
    },
    onError: (error: any) => {
      setErrorMsg(error?.response?.data?.message ?? "");
    },
    refetchInterval: isWorkflowRunning ? false : PREVIEW_REFRESH_INTERVAL_MS,
    enabled: enablePreviewQuery,
  });

  const workflowAction = useMemo(() => (
    <Box float="right">
      <Button
        variant="primary"
        onClick={onTriggerWorkflow}
        loading={isWorkflowRunning}
      // disabled={!imageSrc} // TODO: confirm if we need to block workflow run if we failed to get imageSrc
      >
        Run workflow
      </Button>
    </Box>
  ), [isWorkflowRunning, onTriggerWorkflow]);

  function PreviewContentPlaceholder(): JSX.Element {
    if (isArvisCameraImageSource(imageSource.type) && imageSource.cameraStatus?.status !== CameraStatus.Connected) {
      return (
        <ImagePlaceholder
          placement="center"
          content={(
            <CameraDisconnectedContent
              loading={isConnecting}
              onConnect={connect}
              message="Camera disconnected. Connect to the camera to continue monitoring live results."
            />
          )}
        />
      );
    }
    if (!imageSrc) {
      return (
        <div style={{
          width: `calc(100% - ${RESULT_DATA_COLUMN_WIDTH}px - 80px`,
        }}>
          <SpaceBetween direction="vertical" size="m">
            <ImagePlaceholder
              placement={errorMsg !== undefined ? "start" : "center"}
              content={
                errorMsg !== undefined
                  ? <ImagePreviewError errorMsg={errorMsg} />
                  : (
                    <>
                      <Spinner size="big" />
                      <p>Loading preview</p>
                    </>
                  )
              }
            />
            {workflowAction}
          </SpaceBetween>
        </div>
      )
    }
    return <></>;
  }

  return (
    <div className={resultLayoutStyle}>
      <div className={resultCardStyle}>
        <LiveResultCard workflow={workflow} />
      </div>
      {
        (isArvisCameraImageSource(imageSource.type) && imageSource.cameraStatus?.status !== CameraStatus.Connected || !imageSrc)
          ? (
            <PreviewContentPlaceholder />
          )
          : (
            <LiveResultContext.Consumer>
              {
                (({ onImageUpdate }): JSX.Element => (
                  <InteractableImage
                    onImageUpdate={onImageUpdate}
                    imageSrc={`data:image/jpg;base64, ${imageSrc}`}
                    extraActions={workflowAction}
                  />
                ))
              }
            </LiveResultContext.Consumer>
          )
      }

    </div>
  );
}