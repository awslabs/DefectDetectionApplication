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
import React, { useEffect } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import format from "date-fns/format";
import { ImageSourceType, CameraStatus, WorkflowTriggerType } from "../types";
import {
  Box,
  Button,
  ColumnLayout,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
  StatusIndicator,
  Spinner,
} from "@cloudscape-design/components";
import { useMutation, useQuery } from "@tanstack/react-query";
import { getImageSource } from "api/ImageSourceAPI";
import { ValueWithLabel } from "Common";
import { DATE_FORMAT } from "components/date-time-format";
import DeleteImageSourceModal from "../delete/DeleteImageSourceModal";
import RoIPreviewWidget from "../roi/RoIPreviewWidget";
import { isArvisCameraImageSource, setHashValuesInUrl } from "components/utils";
import { DynamicRouterHashKey } from "components/layout/constants";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import useCameraConnection from "components/hook/useCameraConnection";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";
import ConfirmDisconnectModal, { FilteredWorkflowTableItem } from "../list/ConfirmDisconnectModal";
import { filterWorkflows } from "api/WorkflowAPI";
import { Workflow } from "components/workflow/types";

export enum ImageSourceTabTypes {
  DETAILS = "ImageSourceDetails",
  IMAGE_CAPTURE = "ImageCapture",
}

function getCameraConnectionStatus(cameraStatus: CameraStatus): JSX.Element {
  switch (cameraStatus) {
    case CameraStatus.Connected:
      return <StatusIndicator type="success">Connected</StatusIndicator>;
    case CameraStatus.Disconnected:
      return <StatusIndicator type="error">Disconnected</StatusIndicator>;
    default: return <></>;
  }
}

export default function ImageSourceDetails(): JSX.Element {
  const navigate = useNavigate();
  const imageSourceId = useParams().imageSourceId ?? "";
  const editImageSourceUrl = `/image-sources/${imageSourceId}/edit`;
  const editImageSourceSettingsUrl = `/image-sources/${imageSourceId}/edit-settings`;
  const location = useLocation();
  const hash = location.hash;

  const { refetch: refetchImageSource, data: imageSource, isLoading: isLoadingImageSource } = useQuery({
    queryKey: ["getImageSource", imageSourceId],
    queryFn: () => getImageSource(imageSourceId),
    cacheTime: 0,
    enabled: !!imageSourceId,
  });

  const {
    creationTime: creationTimestamp,
    lastUpdateTime: lastUpdateTimestamp,
    name: imgSrcName = "",
    type: imgSrcType = "",
    location: inputPath,
    imageSourceConfiguration: imgSrcConfig,
    cameraId = "",
    cameraStatus: cameraStatusObj,
    description,
  } = imageSource || {};

  const {
    connect,
    isConnecting,
    disconnect,
    isDisconnecting,
  } = useCameraConnection({ cameraId, recheckStatusFn: refetchImageSource });

  const creationTime = creationTimestamp
    ? format(creationTimestamp, DATE_FORMAT)
    : "";
  const lastUpdateTime = lastUpdateTimestamp
    ? format(lastUpdateTimestamp, DATE_FORMAT)
    : "";

  // If image source is not folder, then it treat as camera.
  // For smart camera, we will add more camera type in the future
  const isFolderSrc = imgSrcType === ImageSourceType.Folder;
  const isCameraSrc = !isFolderSrc;
  const isArvisCameraSrc = isArvisCameraImageSource(imgSrcType)
  const { status: cameraStatus } = cameraStatusObj || {};

  const [deleteModalVisible, setDeleteModalVisible] = React.useState(false);
  const [showDisconnectCameraModal, setShowDisconnectCameraModal] = React.useState(false);
  const [workflowsByCameraId, setWorkflowsByCameraId] = React.useState<FilteredWorkflowTableItem[]>([]);

  const { mutate: getWorkflowsByCameraId, isError, isLoading: isCheckingImpactedWorkflows } = useMutation({
    mutationFn: async () => {
      const workflows = await filterWorkflows(cameraId);
      return workflows.map(
        (workflow: Workflow) => {
          const { inputConfigurations, imageSources, name } = workflow;
          return {
            imageSourceName: imageSources[0].name,
            workflowName: name,
            trigger: inputConfigurations.length > 0
              ? WorkflowTriggerType.DigitalInput
              : WorkflowTriggerType.RESTAPI,
          } as FilteredWorkflowTableItem;
        },
      );
    },
    onSuccess: (data) => {
      setWorkflowsByCameraId(data);
      if (data.length > 0) {
        setShowDisconnectCameraModal(true);
      } else {
        disconnect();
      }
    },
    onError: () => {
      setWorkflowsByCameraId([]);
      setShowDisconnectCameraModal(true);
    },
    cacheTime: 0,
  });

  useEffect(() => {
    const nextHash = setHashValuesInUrl(hash.substring(1), {
      [DynamicRouterHashKey.IMAGE_SOURCE_NAME]: encodeURIComponent(imgSrcName),
    });
    if (hash !== nextHash) navigate(nextHash, { replace: true });
  }, [hash, imgSrcName, navigate]);

  if (isLoadingImageSource) {
    return <Spinner size="big" />
  }

  return (
    <ContentLayout
      disableOverlap
      header={
        <Header
          variant="h1"
          actions={
            <Button onClick={(): void => setDeleteModalVisible(true)}>
              Delete image source
            </Button>
          }
        >
          {imgSrcName}
        </Header>
      }
    >
      <Box padding={{ top: "xl" }}>
        <SpaceBetween size="xl">
          <Container
            header={
              <Header
                variant="h2"
                actions={
                  <Button
                    onClick={(): void => navigate(editImageSourceUrl)}
                  >
                    Edit
                  </Button>
                }
              >
                Image source details
              </Header>
            }
          >
            <ColumnLayout columns={3} borders="vertical">
              <ValueWithLabel label="Name">{imgSrcName}</ValueWithLabel>
              <ValueWithLabel label="Description">
                {description || "-"}
              </ValueWithLabel>
              <ValueWithLabel label="Input type">
                {imgSrcType}
              </ValueWithLabel>
              <ValueWithLabel label="Date created">
                {creationTime}
              </ValueWithLabel>
              <ValueWithLabel label="Date modified">
                {lastUpdateTime}
              </ValueWithLabel>
            </ColumnLayout>

            {isFolderSrc && (
              <>
                <Box padding={{ top: "m", bottom: "xs" }}>
                  <Header variant="h3">Folder settings</Header>
                </Box>
                <Box color="text-status-inactive">
                  <ValueWithLabel label="Path Location">
                    {inputPath}
                  </ValueWithLabel>
                </Box>
              </>
            )}
          </Container>

          {isCameraSrc && (
            <>
              {/* Only show connection status for Arvis camera as we don't have connection status for ICam right now*/}
              {isArvisCameraSrc && (
                <Container
                  header={
                    <Header
                      variant="h2"
                      actions={
                        cameraStatus === CameraStatus.Connected
                          ? (
                            <Button
                              loading={isCheckingImpactedWorkflows || isDisconnecting}
                              onClick={(): void => getWorkflowsByCameraId()}
                            >
                              Disconnect
                            </Button>
                          )
                          : <Button loading={isConnecting} onClick={connect}>Connect</Button>
                      }
                    >
                      Camera details
                    </Header>
                  }
                >
                  <ColumnLayout columns={2} borders="vertical">
                    <ValueWithLabel label="Camera name">
                      {cameraId}
                    </ValueWithLabel>
                    <ValueWithLabel label="Connection status">
                      {cameraStatus ? getCameraConnectionStatus(cameraStatus) : "-"}
                    </ValueWithLabel>
                  </ColumnLayout>
                </Container>
              )}
              <Container
                header={
                  <Header
                    variant="h2"
                    actions={<Button onClick={(): void => navigate(editImageSourceSettingsUrl)}>Edit</Button>}
                  >
                    Image settings
                  </Header>
                }
              >
                <ColumnLayout columns={2} borders="vertical">
                  <ValueWithLabel label="Gain">
                    {imgSrcConfig?.gain}
                  </ValueWithLabel>
                  <ValueWithLabel label="Exposure">
                    {imgSrcConfig?.exposure}
                  </ValueWithLabel>
                  <ValueWithLabel label="Advanced settings">
                    {imgSrcConfig?.processingPipeline}
                  </ValueWithLabel>
                </ColumnLayout>
              </Container>
              <Container
                header={(
                  <Header
                    variant="h2"
                    actions={
                      <Button onClick={(): void => navigate(`/image-sources/${imageSourceId}/edit-region-of-interest`)}>
                        Edit
                      </Button>
                    }
                  >
                    Region of interest
                  </Header>
                )}
              >
                <Box margin={{ top: "l" }}>
                  {
                    isArvisCameraSrc && cameraStatus === CameraStatus.Disconnected
                      ? (
                        <ImagePlaceholder
                          placement="center"
                          content={
                            <CameraDisconnectedContent
                              loading={isConnecting}
                              onConnect={connect}
                            />
                          }
                        />
                      )
                      : (
                        <RoIPreviewWidget
                          imageSourceId={imageSourceId}
                          imageSourceConfiguration={imgSrcConfig}
                        />
                      )
                  }
                </Box>
              </Container>
            </>
          )}
        </SpaceBetween>
      </Box>

      <DeleteImageSourceModal
        imgSrcId={imageSourceId}
        imgSrcName={imgSrcName}
        isFolderSrc={isFolderSrc}
        isVisible={deleteModalVisible}
        onCancel={(): void => setDeleteModalVisible(false)}
      />
      <ConfirmDisconnectModal
        filteredWorkflows={workflowsByCameraId}
        isVisible={showDisconnectCameraModal}
        cameraId={cameraId}
        onCancel={(): void => setShowDisconnectCameraModal(false)}
        showError={isError}
        onCameraDisconnect={(): void => {
          setShowDisconnectCameraModal(false);
          refetchImageSource();
        }}
      />
    </ContentLayout>
  );
}