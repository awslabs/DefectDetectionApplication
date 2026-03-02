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

import { Alert, Button, ProgressBar, SpaceBetween, Spinner, TextContent } from "@cloudscape-design/components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { previewImage } from "api/ImageAPI";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";
import OverflowScrollBox from "components/common/OverflowScrollBox";
import useCameraConnection from "components/hook/useCameraConnection";
import { PREVIEW_REFRESH_INTERVAL_MS } from "components/image-settings/constants";
import { CameraStatus, ImageSourceType, WorkflowTriggerType } from "components/image-source/types";
import { styleConstants } from "components/layout/constants";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import InteractableImage from "components/live-result/InteractableImage";
import { capturePreviewContainerStyle, capturePreviewInfoSectionStyle } from "./styles";
import { useContext, useEffect, useMemo, useState } from "react";
import CaptureWorkflowInfo from "./CaptureWorkflowInfo";
import Divider from "components/common/Divider";
import { useNavigate } from "react-router-dom";
import { Workflow, WorkflowCaptureTaskStatus } from "components/workflow/types";
import { getWorkflowMetadata, isArvisCameraImageSource, isICamImageSource, isNvidiaCSIImageSource } from "components/utils";
import { RunWorkflowRequest, getWorkflowCaptureTask, runWorkflow } from "api/WorkflowAPI";
import { useForm } from "react-hook-form";
import { CaptureConfigSchemaType, captureConfigSchema } from "./captureConfigSchema";
import { yupResolver } from "@hookform/resolvers/yup";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { CAPTURE_TASK_REFETCH_INTERVAL } from "./constants";
import ImageCaptureTopAlert from "./ImageCaptureTopAlert";
import { listWorkflowResults } from "api/InferenceResultAPI";
import { APIList } from "config/Interface";
import { captureImageType } from "components/live-result/types";
import useAuth from "components/auth/authHook";

interface ImageCaptureWorkflowContentProps {
  workflow: Workflow;
}

export default function ImageCaptureWorkflowContent({ workflow }: ImageCaptureWorkflowContentProps): JSX.Element {
  const { addError, addSuccess } = useContext(AppLayoutContext);
  const queryClient = useQueryClient();
  const [imagePreviewError, setImagePreviewError] = useState<string | null>(null);
  const navigate = useNavigate();
  const { authEnabled, token } = useAuth();

  const {
    hasModel,
    workflowTriggerType,
    imageSource,
    imageSourceId,
    workflowId,
    name,
  } = getWorkflowMetadata(workflow);
  const { cameraId = "", type: imageSourceType } = imageSource || {};
  const isCameraType = !!imageSourceType && imageSourceType !== ImageSourceType.Folder;
  const isAPITrigger = workflowTriggerType === WorkflowTriggerType.RESTAPI;
  const {
    connect,
    isConnecting,
  } = useCameraConnection({ cameraId, recheckStatusFn: () => queryClient.invalidateQueries() })

  const [refetchCaptureTask, setRefetchCaptureTask] = useState(false);

  const { data: captureTask, refetch: refetchTask } = useQuery({
    queryKey: ["getCaptureTask", workflowId],
    queryFn: () => getWorkflowCaptureTask(workflowId),
    refetchInterval: refetchCaptureTask ? CAPTURE_TASK_REFETCH_INTERVAL : false,
    enabled: !!workflowId,
  })

  const {
    status: taskStatus,
    count: taskTotalCount,
    capturedCount: taskCapturedCount = 0,
    interval: taskInterval,
    prefix: taskFilePrefix,
    statusMessage: taskStatusMessage,
  } = captureTask || {};

  const isTaskRunning = taskStatus === WorkflowCaptureTaskStatus.RUNNING;

  const form = useForm<CaptureConfigSchemaType>({
    resolver: yupResolver(captureConfigSchema),
    mode: "onChange",
    values: {
      count: 1,
      interval: 1,
      filePrefix: "",
    },
  });

  useEffect(() => {
    if (refetchCaptureTask) {
      if (taskStatus === WorkflowCaptureTaskStatus.FAILED) {
        addError({
          header: (
            <span>
              Failed to capture images for the workflow <i>{name}</i>
            </span>
          ),
          content: taskStatusMessage,
        })
      } else if (taskStatus === WorkflowCaptureTaskStatus.COMPLETED) {
        addSuccess({
          content: (
            <span>${taskCapturedCount} new images captured for the workflow <i>{name}</i>.</span>
          ),
          action: (
            <Button variant="normal" onClick={(): void => {
              navigate(`/capture-results/${workflowId}`);
            }}>
              View capture results
            </Button>
          )
        })
      }
    }
    if (isTaskRunning) {
      form.setValue("count", taskTotalCount || 1);
      form.setValue("interval", taskInterval || 1);
      form.setValue("filePrefix", taskFilePrefix || "");
    }
    setRefetchCaptureTask(isTaskRunning)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isTaskRunning, taskStatus, taskTotalCount, taskInterval, taskFilePrefix, taskCapturedCount, name, taskStatusMessage])

  const count = form.watch("count");
  useEffect(() => {
    form.trigger("interval");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [count])

  const { data: imagePreview, isLoading: isLoadingImagePreview } = useQuery({
    queryKey: ["previewImage", imageSourceId],
    queryFn: () => previewImage(imageSourceId || ""),
    onSuccess: (data) => {
      if (data.image) {
        setImagePreviewError(null);
      } else {
        setImagePreviewError("");
      }
    },
    onError: (error: any) => {
      setImagePreviewError(error?.response?.data?.message ?? "");
    },
    // Don't refetch if capture is in progress. Real camera has issue when
    // trying to capture and preview at the same time.
    refetchInterval: PREVIEW_REFRESH_INTERVAL_MS,
    enabled: !!imageSourceId && isCameraType && !isTaskRunning && isAPITrigger && (imageSource?.cameraStatus?.status === CameraStatus.Connected || isICamImageSource(imageSource?.type) || isNvidiaCSIImageSource(imageSource?.type)),
  });

  const { data: latestWorkflowResult, isLoading: isLoadingLatestWorkflowResult } = useQuery({
    queryKey: ["getWorkflowResult", workflowId],
    queryFn: () => listWorkflowResults({
      id: workflowId,
      page: 1,
      size: 1,
    }),
    // Don't refetch if capture is in progress. Real camera has issue when
    // trying to capture and preview at the same time.
    refetchInterval: PREVIEW_REFRESH_INTERVAL_MS,
    enabled: !!workflowId && isCameraType && !isAPITrigger,
  });

  const { mutate: runCaptureWorkflow, isLoading: isRunningCaptureWorkflow } = useMutation({
    mutationFn: async ({ interval, count, filePrefix }: CaptureConfigSchemaType) => {
      /**
       * Use Number() here to transform value type to number since the value can actually be string as its from formInput component
       */
      const runConfig: RunWorkflowRequest = hasModel ? {
        captureImageCount: 1,
      } : {
        captureImageCount: Number(count),
        capturePrefix: filePrefix,
        ...(Number(count) > 1 ? { captureTimeInterval: Number(interval) } : {})
      }
      return runWorkflow(workflowId, runConfig);
    },
    onSuccess: (_, { count }) => {
      /**
       * When the capture count = 1, the API will directly complete the capture task so we can then direct refetch the captured list
       * When the capture count > 1, the API will maintain the running capture task which we fetch from the capture task api
       */
      if (Number(count) === 1) {
        queryClient.refetchQueries(["getLastCaptureImages", workflowId])
      } else {
        refetchTask();
      }
    },
    onError: (e: any) => {
      addError({
        header: "Failed to capture images",
        content: e?.response?.data?.message || "An error occurred while attempting to capture images."
      })
    }
  })

  const imagePreviewComponent = useMemo(() => {
    if (!imageSource) return null;
    if (isArvisCameraImageSource(imageSource.type) && imageSource.cameraStatus?.status !== CameraStatus.Connected) {
      return (
        <ImagePlaceholder
          placement="center"
          content={(
            <CameraDisconnectedContent
              loading={isConnecting}
              onConnect={connect}
            />
          )}
        />
      );
    }
    if (isTaskRunning) {
      return (
        <ImagePlaceholder
          placement="center"
          content={(
            <TextContent>
              <span>Capturing images</span>
              <br />
              <small>Automated image capture workflow in progress.</small>
              <ProgressBar value={!taskTotalCount ? 0 : (taskCapturedCount / taskTotalCount) * 100} />
              <small>Captured images will be automatically added to the capture results page.</small>
            </TextContent>
          )}
        />
      )
    }
    if (isLoadingImagePreview) {
      return (
        <ImagePlaceholder
          placement="center"
          content={(
            <>
              <Spinner size="big" />
              <p>Loading preview</p>
            </>
          )}
        />
      )
    }
    if (imagePreviewError !== null) {
      return <ImagePreviewError errorMsg={imagePreviewError} />;
    }
    return (
      <InteractableImage
        imageSrc={`data:image/jpg;base64, ${imagePreview?.image || ""}`}
        extraActions={(
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="normal" onClick={(): void => navigate(`/image-sources/${imageSourceId}/edit-settings`)}>
              Edit image settings
            </Button>
            <Button
              variant="primary"
              loading={isRunningCaptureWorkflow}
              onClick={async (): Promise<void> => {
                // Validate form before triggering workflow
                if (!await form.trigger()) {
                  return;
                }
                const formValues = form.getValues();
                runCaptureWorkflow(formValues);
              }}
            >
              Capture
            </Button>
          </SpaceBetween>
        )}
      />
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imagePreview?.image, imagePreviewError, imageSource, isConnecting, isLoadingImagePreview, captureTask, isRunningCaptureWorkflow])

  const dioResultViewComponent = useMemo(() => {
    const isEmptyResult = !latestWorkflowResult?.results || latestWorkflowResult.results.length === 0;
    if (isLoadingLatestWorkflowResult || isEmptyResult) {
      return (
        <ImagePlaceholder
          placement="center"
          content={
            <>
              <Spinner size="big" />
              {
                isEmptyResult && <p>Waiting for next digital input to capture image</p>
              }
            </>
          }
        />
      );
    }
    const captureId = latestWorkflowResult.results[0].captureId || "";
    const getCaptureAPI = APIList.getCapture
      .replace("{workflow_id}", workflowId)
      .replace("{capture_id}", captureId);
    return (
      <InteractableImage
        imageSrc={`${getCaptureAPI}/${captureImageType.INPUT_IMAGE}${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`}
        extraActions={(
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="normal" onClick={(): void => navigate(`/image-sources/${imageSourceId}/edit-settings`)}>
              Edit image settings
            </Button>
            <Button variant="primary" disabled>
              Capture
            </Button>
          </SpaceBetween>
        )}
      />
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authEnabled, imageSourceId, latestWorkflowResult?.results, token, workflowId, isLoadingLatestWorkflowResult])

  if (!workflow || !imageSource) {
    return <Alert type="info">This workflow isn't configured.</Alert>;
  }

  if (!isCameraType) {
    return <Alert type="info">Selected workflow uses a folder as the image source. Capturing images is not allowed for this workflow.</Alert>
  }

  return (
    <SpaceBetween direction="vertical" size="l">
      <ImageCaptureTopAlert
        hasModel={hasModel}
        workflowTriggerType={workflowTriggerType}
      />
      <OverflowScrollBox contentMinWidth={styleConstants.IMAGE_CONTENT_CONTAINER_MIN_WIDTH}>
        <div className={capturePreviewContainerStyle}>
          <div className={capturePreviewInfoSectionStyle}>
            <CaptureWorkflowInfo workflow={workflow} form={form} disabled={isTaskRunning} />
          </div>
          <Divider direction="vertical" />
          {isAPITrigger ? imagePreviewComponent : dioResultViewComponent}
        </div>
      </OverflowScrollBox>
    </SpaceBetween>
  );
}