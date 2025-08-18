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

import format from "date-fns/format";
import { DATE_SECOND_FORMAT } from "components/date-time-format";
import { InferenceResult, MaskBackground } from "api/WorkflowAPI";
import { ColorRGB } from "./InteractableImage";
import { MILLISEC_TIMESTAMP_DIGIT } from "./constants";

export const formatPercent = (value?: number): string => {
  // keep up to two decimal places
  if (!value) return "-";
  const roundedNumber = Math.round(value * 100 * 100) / 100;
  return `${roundedNumber}%`;
};
export const formatAnomalyValue = (value?: number): string => {
  if (!value) return "-";
  return `${value.toPrecision(3)}`;
};
// Get capture Id from inference file path
export const getCaptureId = (path: string | undefined): string => {
  const fileName = path?.split("/")?.pop() || "";
  return fileName.slice(0, -6);
};
export const formatProcessingTime = (seconds?: number): string => {
  if (!seconds) return "-";
  return `${Math.round(seconds)} ms`;
};
export const formatInferenceTime = (value?: string): string => {
  // TODO: Improve this function after differentiator removed from [aws.edgeml.dda.InferenceApp]
  // https://code.amazon.com/reviews/CR-91354610
  // Inference time is utcTimestamp_differentiator, ex: "2023-04-10T16:25:17_6"
  if (!value) return "-";
  const time = new Date(value.split("_")[0] + "Z");
  return format(time, DATE_SECOND_FORMAT);
};
export const convertTimestampToLocalTime = (ts: number | null | undefined): string => {
  if (!ts) return "-"
  if (ts.toString().length === MILLISEC_TIMESTAMP_DIGIT) {
    // Convert 13 digit timestamp to broswer local time
    return format(ts, DATE_SECOND_FORMAT)
  } else {
    // Convert 10 digit timestamp to broswer local time
    return format(ts * 1000, DATE_SECOND_FORMAT)
  }
}

export const getMaskBackgroundColor = (background: MaskBackground | null): {} => {
  return background
    ? {
      backgroundColor: {
        r: background["rgb-color"]?.[0],
        g: background["rgb-color"]?.[1],
        b: background["rgb-color"]?.[2],
      },
    } : {};
}

export const getMaskImageProp = (mask: string | null, backgroundColorProp: {}): {} => {
  return mask !== null ? {
    maskImage: {
      src: `data:image/png;base64, ${mask}`,
      ...backgroundColorProp,
    },
  } : {};
}

export const getFileName = (path: string): string => {
  return path.split("/").slice(-1)[0];
};

export const getFileFolder = (path: string): string => {
  return path.split("/").slice(0, -1).join("/") + "/";
};

/**
 * Check if there is any anomoly label exists in inference result
 * Return true if one or more labels exist, otherwise return false
 */
export const checkAnomalyLabel = (
  inferenceResult: InferenceResult | undefined,
): boolean => {
  if (!inferenceResult) return false;
  return !!inferenceResult.mask_image;
};

/**
 * helper function to setup mask image to the target canvas element
 *
 * @param maskImgSrc the mask image which will be put into the target canvas element
 * @param canvas the target canvas element
 * @param width canvas width
 * @param height canvas height
 * @param backgroundColor the mask image background color which will be set to transparent
 */
export function setupMaskImage(
  maskImgSrc: string,
  canvas: HTMLCanvasElement,
  width: number,
  height: number,
  backgroundColor?: ColorRGB,
): void {
  let ctx = canvas.getContext("2d");
  if (ctx) {
    let img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = (): void => {
      canvas.width = width;
      canvas.height = height;
      if (ctx) {
        ctx.drawImage(
          img,
          0,
          0,
          img.naturalWidth,
          img.naturalHeight,
          0,
          0,
          canvas.width,
          canvas.height,
        );
        const imgd = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const pix = imgd.data;
        for (var i = 0, n = pix.length; i < n; i += 4) {
          const r = pix[i];
          const g = pix[i + 1];
          const b = pix[i + 2];

          if (
            !!backgroundColor &&
            r === backgroundColor.r &&
            b === backgroundColor.b &&
            g === backgroundColor.g
          ) {
            pix[i + 3] = 0;
          }
        }
        ctx.putImageData(imgd, 0, 0);
      }
    };
    img.onerror = (): void => {
      clearMaskImage(canvas);
    }
    img.src = maskImgSrc;
  }
}

export function clearMaskImage(canvas: HTMLCanvasElement): void {
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
}

export function getFeedbackRequiredValue(humanFeedbackRequired?: boolean | null): string {
  switch (humanFeedbackRequired) {
    case true:
      return "Yes";
    case false:
      return "No";
    default:
      return "No";
  }
}
