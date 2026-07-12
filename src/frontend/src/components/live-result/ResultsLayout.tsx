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

import LiveResultCard from "./LiveResultCard";
import { RunWorkflowResponse } from "api/WorkflowAPI";
import { Workflow } from "components/workflow/types";
import { APIList } from "config/Interface";
import { captureImageType } from "./types";
import { PredictionType } from "components/image-source/types";
import { LiveResultContext } from "components/live-result/LiveResults";
import InteractableImage from "components/live-result/InteractableImage";
import LiveResultActions from "components/live-result/LiveResultActions";
import {
  checkAnomalyLabel,
  getCaptureId,
} from "./helpers";
import {
  resultLayoutStyle,
  resultCardStyle,
} from "components/live-result/styles";
import RefreshDisplayActions from "./RefreshDisplayActions";
import useAuth from "components/auth/authHook";

export type FolderInfo = {
  nextImage: string;
};

type ManualTriggerConfigs = {
  onClickAnomalyMaskToggle: (checked: boolean) => void;
  onTriggerWorkflow: () => void;
  isWorkflowRunning: boolean;
  onClickPreview: () => void;
  folderInfo?: FolderInfo;
};

type DigitalInputConfigs = {
  onClickAnomalyMaskToggle: (checked: boolean) => void;
};

interface ResultsLayoutProps {
  workflow: Workflow;
  workflowRun: RunWorkflowResponse;
  showMask?: boolean;
  manualTriggerConfig?: ManualTriggerConfigs;
  digitalInputConfig?: DigitalInputConfigs;
}

export default function ResultsLayout({
  workflow,
  workflowRun,
  showMask,
  manualTriggerConfig,
  digitalInputConfig,
}: ResultsLayoutProps): JSX.Element {

  const { token, authEnabled } = useAuth();

  const captureId =
    workflowRun.captureId || getCaptureId(workflowRun.inferenceFilePath);
  const getCaptureAPI = APIList.getCapture
    .replace("{workflow_id}", workflow.workflowId)
    .replace("{capture_id}", captureId);

  // Object-detection results have their bounding boxes drawn into the
  // server-rendered overlay (OUTPUT_IMAGE); anomaly/classification results show
  // the input image (with the segmentation mask overlaid client-side, if any).
  const isDetection =
    workflowRun.inferenceResult.inference_result === PredictionType.Detection;

  // For detection results the bounding boxes are baked into OUTPUT_IMAGE
  // (the server-rendered overlay). Reuse the same toggle the segmentation flow
  // uses for the mask: when on (default) show the overlay with boxes, when off
  // show the raw input image with no boxes.
  const getImageSrc = (): string => {
    const imageType =
      isDetection && showMask
        ? captureImageType.OUTPUT_IMAGE
        : captureImageType.INPUT_IMAGE;
    return `${getCaptureAPI}/${imageType}${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`;
  };

  const imageSrc = getImageSrc();

  const backgroundColorProp = workflowRun.inferenceResult.mask_background
    ? {
      backgroundColor: {
        r: workflowRun.inferenceResult.mask_background["rgb-color"]?.[0],
        g: workflowRun.inferenceResult.mask_background["rgb-color"]?.[1],
        b: workflowRun.inferenceResult.mask_background["rgb-color"]?.[2],
      },
    }
    : {};
  const maskImageProp =
    workflowRun.inferenceResult.mask_image !== null
      ? {
        maskImage: {
          src: `data:image/png;base64, ${workflowRun.inferenceResult.mask_image}`,
          ...backgroundColorProp,
        },
      }
      : {};

  // API trigger workflow -> manualTriggerConfig
  // Digital input trigger workflow -> digitalInputConfig goes to refresh layout
  // The overlay toggle is shown for segmentation captures (mask) and for
  // detection captures (bounding boxes). The label reflects which overlay it
  // controls; the underlying state (showMask) is reused for both.
  const showOverlayToggle =
    isDetection || checkAnomalyLabel(workflowRun?.inferenceResult);
  const overlayToggleLabel = isDetection
    ? "Show bounding boxes"
    : "Show anomaly masks";

  const extraActionForLiveResult = !!manualTriggerConfig ? (
    <LiveResultActions
      showAnomalyMaskToggle={showOverlayToggle}
      toggleLabel={overlayToggleLabel}
      onClickAnomalyMaskToggle={manualTriggerConfig.onClickAnomalyMaskToggle}
      onTriggerWorkflow={manualTriggerConfig.onTriggerWorkflow}
      anomalyMaskToggleChecked={!!showMask}
      imageSourceType={workflow.imageSources?.[0]?.type}
      isWorkflowRunning={manualTriggerConfig.isWorkflowRunning}
      onClickPreview={manualTriggerConfig.onClickPreview}
      folderInfo={manualTriggerConfig.folderInfo}
    />
  ) : !!digitalInputConfig ? (
    <RefreshDisplayActions
      showAnomalyMaskToggle={showOverlayToggle}
      toggleLabel={overlayToggleLabel}
      onClickAnomalyMaskToggle={digitalInputConfig.onClickAnomalyMaskToggle}
      anomalyMaskToggleChecked={!!showMask}
    />
  ) : undefined;

  return (
    <div className={resultLayoutStyle}>
      <div className={resultCardStyle}>
        <LiveResultCard workflow={workflow} workflowRun={workflowRun} />
      </div>
      <LiveResultContext.Consumer>
        {({ onImageUpdate }): JSX.Element => (
          <InteractableImage
            onImageUpdate={onImageUpdate}
            imageSrc={imageSrc}
            {...maskImageProp}
            showMask={!!showMask}
            extraActions={extraActionForLiveResult}
          />
        )}
      </LiveResultContext.Consumer>
    </div>
  );
}
