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

import { useNavigate } from "react-router-dom";
import {
  Button,
  Container,
  ExpandableSection,
  Form,
  Header,
  SpaceBetween,
  TextContent,
  Toggle,
} from "@cloudscape-design/components";
import { RefObject, useEffect, useMemo, useState } from "react";
import { getImagePreview } from "../../../api/ImagePreviewAPI";
import { useQuery } from "@tanstack/react-query";
import ImagePreviewError from "components/live-result/ImagePreviewError";
import RoIAnnotationImage, { NoCrop } from "./RoIAnnotationImage";
import { CameraStatus, ImageSource, RegionOfInterest } from "../types";
import ConfirmCropRegionModal from "./ConfirmCropRegionModal";
import OverflowScrollBox from "components/common/OverflowScrollBox";
import RoICoordinateEditForm from "components/image-source/roi/RoICoordinateEditForm";
import { getRoICoordinateData } from "./helpers";
import { roiActionRowStyle, roiEditWrapperStyle, roiImagePreviewContainerStyle, roiSettingContainerStyle, roiSettingDescriptionStyle } from "./styles";
import ImagePlaceholder from "components/common/ImagePlaceholder";
import { CameraDisconnectedContent } from "components/common/ImagePlaceholder/PresetPlaceholderContents";
import useCameraConnection from "components/hook/useCameraConnection";

type EditRoIPageProps = {
  id: string;
  initialImageSource: ImageSource | undefined;
  initialRegionOfInterest: RegionOfInterest;
  isLoading: boolean;
  isArvisCamera: boolean;
  cameraId: string;
  cameraStatus?: CameraStatus;
  recheckCameraStatusFn: () => void;
};

export default function EditRoIPage({
  id,
  initialImageSource,
  initialRegionOfInterest,
  isLoading,
  isArvisCamera,
  cameraId,
  cameraStatus,
  recheckCameraStatusFn,
}: EditRoIPageProps): JSX.Element {

  const { connect, isConnecting } = useCameraConnection({ cameraId, recheckStatusFn: recheckCameraStatusFn });
  const [showVerifyCropRoiModal, setShowVerifyCropRoiModal] = useState(false);
  const navigate = useNavigate();
  const path = `/image-sources/${id}`;

  const [imageToRenderBase64, setImageToRender] = useState("");
  const [imageLoadError, setImageLoadError] = useState<string | undefined>(
    undefined,
  );
  const [isImageLoading, setImageLoading] = useState(true);
  const [showCroppedImage, setShowCroppedImage] = useState(false);
  const [regionOfInterestAnnotation, setRegionOfInterestAnnotation] =
    useState<RegionOfInterest>(initialRegionOfInterest);
  const [showValidationMessage, setShowValidationMessage] = useState(false);
  const [canvasRef, setCanvasRef] = useState<RefObject<HTMLCanvasElement> | null>(null);
  const [imgRef, setImgRef] = useState<RefObject<HTMLImageElement> | null>(null);
  const [originalCanvasSize, setOriginalCanvasSize] = useState({ width: 0, height: 0 });

  // the image crop annotation is used to keep track of the drawn Crop Region of Interest on the canvas
  const [imageCropAnnotation, setImageCropAnnotation] =
    useState<RegionOfInterest>(initialRegionOfInterest);

  // the image crop preview settings is used to keep track of the crop region of interest that is used to generate the preview image
  // this can be used to show the RoI cropped image
  const [imageCropPreviewSettings, setImageCropPreviewSettings] =
    useState<RegionOfInterest>(NoCrop);

  const { width: canvasWidth = 0, height: canvasHeight = 0 } = canvasRef?.current || {};
  const { naturalWidth: originalImgWidth = 0, naturalHeight: originalImgHeight = 0 } = imgRef?.current || {};

  useEffect(() => {
    setImageCropAnnotation(initialRegionOfInterest);
  }, [initialRegionOfInterest]);

  const enablePreviewQuery = cameraStatus === CameraStatus.Connected || !isArvisCamera;
  const previewQuery = useQuery({
    queryKey: ["editCropRoIPreview"],
    queryFn: async () => {
      var cropSettings = NoCrop;
      if (imageCropPreviewSettings) {
        cropSettings = {
          top: Math.round(imageCropPreviewSettings.top),
          bottom: Math.round(imageCropPreviewSettings.bottom),
          left: Math.round(imageCropPreviewSettings.left),
          right: Math.round(imageCropPreviewSettings.right),
        };
      }
      if (initialImageSource?.imageSourceConfiguration) {
        return await getImagePreview(id, {
          imageSourceConfiguration: {
            ...initialImageSource.imageSourceConfiguration,
            imageCrop: cropSettings,
          },
        });
      }

      return { image: "" };
    },
    onSuccess: (data) => {
      if (data.image) {
        setImageToRender(data.image);
        setImageLoading(false);
        setImageLoadError(undefined);
      }
    },
    onError: (error: any) => {
      setImageLoadError(error?.response?.data?.message ?? "");
      setImageLoading(false);
    },
    refetchInterval: 4000,
    enabled: enablePreviewQuery,
  });

  useEffect(() => {
    if (enablePreviewQuery) {
      setImageToRender("");
      setImageLoading(true);
      previewQuery.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageCropPreviewSettings, enablePreviewQuery]);

  useEffect(() => {
    if (!showCroppedImage && canvasWidth > 0 && canvasHeight > 0) {
      setOriginalCanvasSize({
        width: canvasWidth,
        height: canvasHeight
      })
    } else {
      setShowValidationMessage(false);
      setCanvasRef(null);
    }
  }, [canvasHeight, canvasWidth, showCroppedImage]);
  const { width: originalCanvasWidth, height: originalCanvasHeight } = originalCanvasSize;

  const { validateX, validateY, validateWidth, validateHeight } = getRoICoordinateData(regionOfInterestAnnotation, originalCanvasWidth, originalCanvasHeight, originalImgWidth, originalImgHeight);

  const editRoIImageContentPlaceholder = useMemo(() => {
    if (isArvisCamera && cameraStatus !== CameraStatus.Connected) {
      return (
        <ImagePlaceholder
          placement="center"
          content={
            <CameraDisconnectedContent
              message="Camera disconnected. Connect to the camera to save region settings."
              loading={isConnecting}
              onConnect={connect}
            />
          }
        />
      )
    }
    if (imageLoadError !== undefined) {
      return <ImagePlaceholder content={<ImagePreviewError errorMsg={imageLoadError} />} />;
    }
    return <></>;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraStatus, imageLoadError, isConnecting]);

  return (
    <Form>
      <div className={roiEditWrapperStyle}>
        <Container
          className={roiSettingContainerStyle}
          header={<Header variant={"h2"}>Region settings</Header>}
        >
          <SpaceBetween direction="vertical" size="l">
            <TextContent>
              <p className={roiSettingDescriptionStyle}>
                To define a rectangular area of interest you must click
                in the top left corner of the area you want to focus on
                and drag to the bottom right of the desired area to finalize
                the rectangle. Alternatively you can enter the settings below.
              </p>
            </TextContent>
            <ExpandableSection headerText="Advanced settings">
              <RoICoordinateEditForm
                showValidationMessage={showValidationMessage}
                regionOfInterestAnnotation={regionOfInterestAnnotation}
                canvasHeight={originalCanvasHeight}
                canvasWidth={originalCanvasWidth}
                originalImgHeight={originalImgHeight}
                originalImgWidth={originalImgWidth}
                setRegionOfInterestAnnotation={setRegionOfInterestAnnotation}
                disabled={showCroppedImage}
              />
            </ExpandableSection>
            <Button
              iconAlign="left"
              iconName="refresh"
              formAction={"none"}
              onClick={(): void => {
                setShowCroppedImage(false);
                setImageCropAnnotation(NoCrop);
                setRegionOfInterestAnnotation(NoCrop);
              }}
            >
              Reset
            </Button>
            <Toggle
              onChange={({ detail }): void => {
                setShowCroppedImage(detail.checked);
              }}
              checked={showCroppedImage}
              disabled={!originalCanvasWidth || !originalCanvasHeight}
            >
              Preview cropped image
            </Toggle>
          </SpaceBetween>

        </Container>
        <div className={roiImagePreviewContainerStyle}>
          <SpaceBetween direction="vertical" size="l">
            <Container
              header={<Header variant={"h2"}>Image preview</Header>}
            >
              <OverflowScrollBox contentMinWidth={originalCanvasWidth || 0}>
                {
                  ((isArvisCamera && cameraStatus !== CameraStatus.Connected) || imageLoadError !== undefined)
                    ? editRoIImageContentPlaceholder
                    : (
                      <RoIAnnotationImage
                        imageSrc={`data:image/jpg;base64, ${imageToRenderBase64}`}
                        setImageCropPreview={setImageCropAnnotation}
                        setImageCrop={setImageCropPreviewSettings}
                        initialRegionOfInterest={initialRegionOfInterest}
                        isImageLoading={isImageLoading}
                        showCroppedImage={showCroppedImage}
                        setRegionOfInterestAnnotation={setRegionOfInterestAnnotation}
                        regionOfInterestAnnotation={regionOfInterestAnnotation}
                        onCanvasRefUpdate={(ref): void => setCanvasRef(ref)}
                        originalCanvasSize={originalCanvasSize}
                        onImageRefUpdate={(ref): void => setImgRef(ref)}
                      />
                    )
                }
              </OverflowScrollBox>
            </Container>
            <div className={roiActionRowStyle}>
              <Button
                formAction="none"
                variant="link"
                href={path}
                onClick={(e): void => {
                  e.preventDefault();
                  navigate(path);
                }}
              >
                Cancel
              </Button>
              <Button
                formAction={"none"}
                variant="primary"
                onClick={(): void => {
                  // validate data
                  setShowValidationMessage(true)
                  if (validateX && validateY && validateWidth && validateHeight) {
                    setShowVerifyCropRoiModal(true)
                  }
                }}
                disabled={isImageLoading || isLoading || (isArvisCamera && cameraStatus !== CameraStatus.Connected)}
              >
                Save
              </Button>
            </div>
          </SpaceBetween>
        </div>
      </div>
      <ConfirmCropRegionModal
        isVisible={showVerifyCropRoiModal}
        imgSrcId={id}
        initialImageSource={initialImageSource}
        updatedCropRoI={imageCropAnnotation}
        onCancel={(): void => setShowVerifyCropRoiModal(false)}
        saveAction={false}
      />
    </Form>
  );
}