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

import { RegionOfInterest } from "../types";
import { scaleUpRoIAnnotation } from "./RoIAnnotationImage";

export const CROP_MIN_WIDTH = 64;
export const CROP_MIN_HEIGHT = 64;

interface RoICoordinateData {
  left: number;
  top: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
  maxX: number;
  maxY: number;
  maxW: number;
  maxH: number;
  validateX: boolean;
  validateY: boolean;
  validateWidth: boolean;
  validateHeight: boolean;
}

export function getImageScaling(originalSize: number, canvasSize: number): number {
  if (!originalSize || !canvasSize) return 1;
  return canvasSize / originalSize;
}

// this function scales up the input roi coordinates, and returns values and coordinates based on the original image size
export function getRoICoordinateData(regionOfInterestAnnotation: RegionOfInterest, canvasWidth: number, canvasHeight: number, originalImgWidth: number, originalImgHeight: number): RoICoordinateData {
  const imageHorizontalScaling = getImageScaling(originalImgWidth, canvasWidth);
  const imageVerticalScaling = getImageScaling(originalImgHeight, canvasHeight);
  const { left = 0, top = 0, right = 0, bottom = 0 } = scaleUpRoIAnnotation(regionOfInterestAnnotation, imageHorizontalScaling, imageVerticalScaling);
  const width = originalImgWidth ? originalImgWidth - left - right : 0;
  const height = originalImgHeight ? originalImgHeight - top - bottom : 0;
  const maxX = originalImgWidth - CROP_MIN_WIDTH;
  const maxY = originalImgHeight - CROP_MIN_HEIGHT;
  let maxW = Math.floor(originalImgWidth - left);
  let maxH = Math.floor(originalImgHeight - top);
  if (maxW < CROP_MIN_WIDTH) maxW = CROP_MIN_WIDTH;
  if (maxH < CROP_MIN_HEIGHT) maxH = CROP_MIN_HEIGHT;
  const validateX = left >= 0 && left <= maxX;
  const validateY = top >= 0 && top <= maxY;
  const validateWidth = width >= CROP_MIN_HEIGHT && width <= maxW;
  const validateHeight = height >= CROP_MIN_HEIGHT && height <= maxH;
  return {
    left,
    top,
    right,
    bottom,
    width,
    height,
    maxX,
    maxY,
    maxW,
    maxH,
    validateX,
    validateY,
    validateWidth,
    validateHeight
  }
}
