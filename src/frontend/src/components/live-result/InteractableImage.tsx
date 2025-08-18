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

import { useEffect, useMemo, useRef, useState } from "react";
import {
  TransformWrapper,
  TransformComponent,
  ReactZoomPanPinchRef,
} from "react-zoom-pan-pinch";
import ImageActionButtonGroup from "components/live-result/ImageActionButtonGroup";
import { SpaceBetween } from "@cloudscape-design/components";
import {
  canvasStyle,
  displayNoneStyle,
  fullWidthStyle,
  imageContainerStyle,
  imageStyle,
} from "components/live-result/styles";
import { css } from "@emotion/css";
import { clearMaskImage, setupMaskImage } from "./helpers";

export type ColorRGB = {
  r: number;
  g: number;
  b: number;
};

interface InteractableImageProps {
  imageSrc: string;
  onImageUpdate?: (image: HTMLImageElement | null) => void;
  controlDisabled?: boolean;
  imageStyleOverride?: string;
  alt?: string;
  isFullWidth?: boolean;
  showMask?: boolean;
  extraActions?: JSX.Element | undefined;
  maskImage?: {
    src: string;
    backgroundColor?: ColorRGB;
  };
}

export default function InteractableImage({
  imageSrc,
  onImageUpdate,
  controlDisabled,
  imageStyleOverride,
  alt,
  isFullWidth = true,
  showMask,
  extraActions,
  maskImage,
}: InteractableImageProps): JSX.Element {
  const imgRef = useRef<HTMLImageElement>(null);
  const panZoomWrapperRef = useRef<ReactZoomPanPinchRef>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const imageUpdateFuncRef = useRef<
    ((image: HTMLImageElement | null) => void) | undefined
  >(undefined);
  const [isImageLoadError, setIsImageLoadError] = useState(false);
  const [newImageLoaded, setNewImageLoaded] = useState(false);

  useEffect(() => {
    imageUpdateFuncRef.current = onImageUpdate;
  }, [onImageUpdate]);

  useEffect(() => {
    return (): void => {
      imageUpdateFuncRef.current?.(null);
    };
  }, []);

  const { clientWidth: imageWidth = 0, clientHeight: imageHeight = 0 } =
    imgRef.current || {};
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (!!maskImage && canvasRef.current && imageWidth > 0 && imageHeight > 0) {
      setupMaskImage(
        maskImage.src,
        canvasRef.current,
        imageWidth,
        imageHeight,
        maskImage.backgroundColor
      );
    } else if (!maskImage && canvasRef.current) {
      clearMaskImage(canvasRef.current);
    }
  }, [maskImage, imageHeight, imageWidth]);

  useEffect(() => {
    setNewImageLoaded(false);
  }, [imageSrc]);

  const image = useMemo(() => {
    return (
      <div className={imageContainerStyle}>
        <img
          ref={imgRef}
          src={imageSrc}
          alt={alt ?? "SourceImage"}
          className={imageStyleOverride ?? imageStyle}
          onLoad={(): void => {
            setIsImageLoadError(false);
            setNewImageLoaded(true);
            onImageUpdate?.(imgRef.current);
          }}
          onError={(): void => setIsImageLoadError(true)}
          data-testid={"base-image"}
        />
        <canvas
          ref={canvasRef}
          className={`
            ${canvasStyle} 
            ${css`
              opacity: ${!!showMask ? 1 : 0};
              transition: opacity 0.25s;
            `}
            ${
              !(imgRef.current?.src === imageSrc && newImageLoaded)
                ? displayNoneStyle
                : ""
            }
          `}
          data-testid={"mask-image"}
        />
      </div>
    );
  }, [
    alt,
    imageSrc,
    imageStyleOverride,
    onImageUpdate,
    showMask,
    newImageLoaded,
  ]);

  return (
    <div
      ref={wrapperRef}
      {...(isFullWidth ? { className: fullWidthStyle } : {})}
    >
      <TransformWrapper
        disabled={controlDisabled || isImageLoadError}
        onInit={(): void => {
          // set a 200ms delay here since the pan zoom library centerView would center the content to a wrong position while initializing
          setTimeout(() => panZoomWrapperRef.current?.centerView(), 200);
        }}
        centerOnInit
        ref={panZoomWrapperRef}
      >
        {(utils): JSX.Element => (
          <SpaceBetween size="s">
            <TransformComponent
              wrapperStyle={{
                background: "var(--grey-100, #F0F1F2)",
                width: "100%",
              }}
            >
              {image}
            </TransformComponent>
            <div
              className={css`
                gap: 20px;
                align-items: start;
              `}
            >
              <ImageActionButtonGroup
                {...utils}
                onClickPan={(isSelected): void => {
                  if (!!wrapperRef.current) {
                    wrapperRef.current.style.cursor = isSelected
                      ? "auto"
                      : "move";
                  }
                }}
                disabled={controlDisabled || isImageLoadError}
                extraActions={extraActions}
              />
            </div>
          </SpaceBetween>
        )}
      </TransformWrapper>
    </div>
  );
}
