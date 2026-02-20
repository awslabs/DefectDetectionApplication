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

import { RefObject, useEffect, useMemo, useRef, useState } from "react";
import {
  SpaceBetween,
  Spinner
} from "@cloudscape-design/components";
import { css } from "@emotion/css";
import { Point, useOnDraw } from "./annotation-hooks";
import { RegionOfInterest } from "../types";
import { roiCanvasStyle, roiImagePreviewContentStyle, roiImageStyle, roiImageWrapperStyle } from "./styles";

export const NoCrop: RegionOfInterest = {
  top: 0,
  bottom: 0,
  left: 0,
  right: 0,
};

interface RoIAnnotationImageProps {
  imageSrc: string;
  isImageLoading: boolean;
  setImageCropPreview: (regionOfInterest: RegionOfInterest) => void;
  setImageCrop: (regionOfInterest: RegionOfInterest) => void;
  initialRegionOfInterest: RegionOfInterest;
  onImageUpdate?: (image: HTMLImageElement | null) => void;
  readonlyMode?: boolean;
  showCroppedImage?: boolean;
  regionOfInterestAnnotation: RegionOfInterest;
  setRegionOfInterestAnnotation: (newRoICrop: RegionOfInterest) => void;
  onCanvasRefUpdate?: (canvasRef: RefObject<HTMLCanvasElement> | null) => void;
  originalCanvasSize?: { width: number, height: number };
  onImageRefUpdate?: (imgRef: RefObject<HTMLImageElement> | null) => void;
}

export default function RoIAnnotationImage({
  imageSrc,
  isImageLoading,
  setImageCropPreview,
  setImageCrop,
  initialRegionOfInterest,
  onImageUpdate,
  readonlyMode,
  showCroppedImage,
  regionOfInterestAnnotation,
  setRegionOfInterestAnnotation,
  onCanvasRefUpdate,
  originalCanvasSize,
  onImageRefUpdate,
}: RoIAnnotationImageProps): JSX.Element {
  const imgRef = useRef<HTMLImageElement>(null);
  const [isImageLoadError, setIsImageLoadError] = useState(false);

  const [imageHorizontalScaling, setImageHorizontalScaling] = useState(1);
  const [imageVerticalScaling, setImageVerticalScaling] = useState(1);
  const [shouldScaleAnnotation, setShouldScaleAnnotation] = useState(true);

  const {
    width: imageWidth = 0,
    height: imageHeight = 0,
    naturalWidth = 0,
    naturalHeight = 0,
  } = imgRef.current || {};
  const canvasRef = useOnDraw(onDraw);

  function onDraw(
    canvas: RefObject<HTMLCanvasElement>,
    pointA: Point,
    previousPoint: Point,
  ): void {
    if (canvas.current && !readonlyMode && !showCroppedImage) {
      const ctx = canvas.current?.getContext("2d");
      const canvasBoundingRect = canvas.current?.getBoundingClientRect();

      if (ctx && canvasBoundingRect) {
        const newRoICrop = {
          top: Math.min(pointA.y, previousPoint.y),
          bottom:
            canvasBoundingRect.height - Math.max(pointA.y, previousPoint.y),
          left: Math.min(pointA.x, previousPoint.x),
          right: canvasBoundingRect.width - Math.max(pointA.x, previousPoint.x),
        };

        setRegionOfInterestAnnotation(newRoICrop);
      }
    }
  }

  useEffect(() => {
    if (!showCroppedImage && naturalWidth !== 0 && naturalHeight !== 0) {
      setImageHorizontalScaling(imageWidth / naturalWidth);
      setImageVerticalScaling(imageHeight / naturalHeight);
    }
  }, [imageWidth, imageHeight, naturalWidth, naturalHeight, isImageLoading, showCroppedImage]);

  useEffect(() => {
    setShouldScaleAnnotation(true);
    const { top, bottom, right, left } = initialRegionOfInterest
    setRegionOfInterestAnnotation({ top, bottom, right, left });
  }, [initialRegionOfInterest, setRegionOfInterestAnnotation]);

  useEffect(() => {
    const canvasEle = canvasRef.current;
    if (canvasEle) {
      if (
        canvasEle.width !== imgRef.current?.width ||
        canvasEle.height !== imgRef.current?.height
      ) {
        canvasEle.width = imgRef.current?.width || imageWidth;
        canvasEle.height = imgRef.current?.height || imageHeight;
      }

      const ctx = canvasEle.getContext("2d");

      if (ctx) {
        if (
          regionOfInterestAnnotation &&
          !showCroppedImage &&
          !isImageLoading
        ) {
          ctx.clearRect(0, 0, canvasEle.width, canvasEle.height);
          ctx.beginPath();
          ctx.strokeStyle = "green";
          ctx.lineWidth = 10;
          // to make sure the whole RoI area is included in the rect, and not covered by the stroke line
          const rectOffset = ctx.lineWidth / 2;
          const { left, top, right, bottom } = regionOfInterestAnnotation;
          ctx.rect(
            left - rectOffset,
            top - rectOffset,
            canvasEle.width - (right - rectOffset) - (left - rectOffset),
            canvasEle.height - (bottom - rectOffset) - (top - rectOffset),
          );
          ctx.stroke();
        } else {
          ctx.clearRect(0, 0, canvasEle.width, canvasEle.height);
          ctx.beginPath();
          ctx.stroke();
        }
      }
    }
  }, [
    regionOfInterestAnnotation,
    imageHorizontalScaling,
    imageVerticalScaling,
    imageHeight,
    imageWidth,
    naturalHeight,
    naturalWidth,
    isImageLoading,
    showCroppedImage,
    canvasRef
  ]);

  useEffect(() => {
    let crop = NoCrop;
    if (showCroppedImage) {
      crop = scaleUpRoIAnnotation(
        regionOfInterestAnnotation,
        imageHorizontalScaling,
        imageVerticalScaling,
      );
    } else {
      // scale up when saving coordinates for submitting to the API
      const upScaledRegionOfInterestAnnotation = scaleUpRoIAnnotation(
        regionOfInterestAnnotation,
        imageHorizontalScaling,
        imageVerticalScaling,
      );
      setImageCropPreview(upScaledRegionOfInterestAnnotation);
    }
    setImageCrop(crop);
  }, [imageHorizontalScaling, imageVerticalScaling, regionOfInterestAnnotation, setImageCrop, setImageCropPreview, showCroppedImage]);

  useEffect(() => {
    if ((!showCroppedImage && shouldScaleAnnotation) || readonlyMode) {
      const { top, bottom, left, right } = initialRegionOfInterest;
      /**
       * we only do this scale once during ROI rectangle initialization in canvas
       * only apply the scale after both the initialRegionOfInterest and scaling values are initialized
       * we determine if the value is initialized by comparing it with the default value
       * If the initialRegionOfInterest value is {0,0,0,0} even after initialization (i.e. no existing ROI data), then its also ok to just skip the scale 
       * If the scale value is 1 even after initialization (i.e. no image size scale), then its also ok to just skip the scale 
       */
      if ((top === 0 && bottom === 0 && left === 0 && right === 0)
        || (imageHorizontalScaling === 1 && imageVerticalScaling === 1)) {
        return;
      }
      // scale down when rendering the image on the canvas
      setRegionOfInterestAnnotation({
        top: top * imageVerticalScaling,
        bottom: bottom * imageVerticalScaling,
        left: left * imageHorizontalScaling,
        right: right * imageHorizontalScaling,
      });
      setShouldScaleAnnotation(false);
    }
  }, [imageHorizontalScaling, imageVerticalScaling, initialRegionOfInterest]);

  const { width: originalCanvasWidth = 0, height: originalCanvasHeight = 0 } = originalCanvasSize || {};
  const roiImageWrapperSizeStyle =
    (!!originalCanvasSize && !!showCroppedImage) ? css`
      height: ${originalCanvasHeight}px;
      width: ${originalCanvasWidth}px;
    ` : "";

  const image = useMemo(() => {
    return (
      <div className={roiImagePreviewContentStyle}>
        <div className={`${roiImageWrapperStyle} ${roiImageWrapperSizeStyle}`}>
          <img
            ref={imgRef}
            src={imageSrc}
            alt={""}
            onLoad={(): void => {
              setIsImageLoadError(false);
              onImageUpdate?.(imgRef.current);
              onCanvasRefUpdate?.(canvasRef);  // update canvas ref after image loaded and image width is set
              onImageRefUpdate?.(imgRef); // update image ref after image loaded and image width is set
            }}
            onError={(): void => setIsImageLoadError(true)}
            className={roiImageStyle}
          />
          <canvas
            ref={canvasRef}
            className={`${roiCanvasStyle} ${css`
              display: ${showCroppedImage ? "none" : "block"};
            `}`}
          />
        </div>
      </div>
    );
  }, [imageSrc, onImageUpdate]);

  return (
    <SpaceBetween size="l">
      {isImageLoading && <Spinner size="big" />}
      {image}
    </SpaceBetween>
  );
}

export function scaleUpRoIAnnotation(
  regionOfInterestAnnotation: RegionOfInterest,
  imageHorizontalScaling: number,
  imageVerticalScaling: number,
): RegionOfInterest {
  return {
    top: regionOfInterestAnnotation.top / imageVerticalScaling,
    bottom: regionOfInterestAnnotation.bottom / imageVerticalScaling,
    left: regionOfInterestAnnotation.left / imageHorizontalScaling,
    right: regionOfInterestAnnotation.right / imageHorizontalScaling,
  };
}

export function scaleDownRoIAnnotation(
  regionOfInterestAnnotation: RegionOfInterest,
  imageHorizontalScaling: number,
  imageVerticalScaling: number,
): RegionOfInterest {
  return {
    top: regionOfInterestAnnotation.top * imageVerticalScaling,
    bottom: regionOfInterestAnnotation.bottom * imageVerticalScaling,
    left: regionOfInterestAnnotation.left * imageHorizontalScaling,
    right: regionOfInterestAnnotation.right * imageHorizontalScaling,
  };
}
