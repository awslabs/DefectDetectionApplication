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

import { FormField, Input, SpaceBetween, TextContent } from "@cloudscape-design/components";
import { RegionOfInterest } from "../types";
import { CROP_MIN_HEIGHT, CROP_MIN_WIDTH, getImageScaling, getRoICoordinateData } from "./helpers";
import { scaleDownRoIAnnotation } from "./RoIAnnotationImage";

interface RoICoordinateEditFormProps {
  showValidationMessage: boolean;
  regionOfInterestAnnotation: RegionOfInterest;
  canvasHeight: number;
  canvasWidth: number;
  setRegionOfInterestAnnotation: (newRoICrop: RegionOfInterest) => void;
  disabled: boolean;
  originalImgWidth: number;
  originalImgHeight: number;
}

interface RoIEditInputFieldProps {
  label: string;
  description: string;
  errorText: string;
  disabled: boolean;
  value: string;
  onChange: (value: number) => void;
}

enum RoIFormField {
  X = "x",
  Y = "y",
  WIDTH = "width",
  HEIGHT = "height",
}

function RoIEditInputField({
  label,
  description,
  errorText,
  disabled,
  value,
  onChange,
}: RoIEditInputFieldProps): JSX.Element {
  return (
    <FormField label={label} errorText={errorText}>
      <Input disabled={disabled} type="number" value={value} onChange={({ detail }): void => {
        const value = Number.parseInt(detail.value) || 0;
        onChange(value);
      }} />
      <TextContent><small>{description}</small></TextContent>
    </FormField>
  );
}

export default function RoICoordinateEditForm({
  showValidationMessage,
  regionOfInterestAnnotation,
  canvasHeight,
  canvasWidth,
  setRegionOfInterestAnnotation,
  disabled,
  originalImgWidth,
  originalImgHeight,
}: RoICoordinateEditFormProps): JSX.Element {

  const imageHorizontalScaling = getImageScaling(originalImgWidth, canvasWidth);
  const imageVerticalScaling = getImageScaling(originalImgHeight, canvasHeight);

  const {
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
    validateHeight,
  } = getRoICoordinateData(regionOfInterestAnnotation, canvasWidth, canvasHeight, originalImgWidth, originalImgHeight);

  function onFormValueChange(value: number, field: RoIFormField): void {
    let upperBoundValue = 0, newLeft = left, newRight = right, newTop = top, newBottom = bottom;
    switch (field) {
      case RoIFormField.X:
        upperBoundValue = originalImgWidth;
        newLeft = value;
        newRight = originalImgWidth - value - width;
        break;
      case RoIFormField.Y:
        upperBoundValue = originalImgHeight;
        newTop = value;
        newBottom = originalImgHeight - value - height;
        break;
      case RoIFormField.WIDTH:
        upperBoundValue = originalImgWidth;
        newRight = originalImgWidth - value - left;
        break;
      case RoIFormField.HEIGHT:
        upperBoundValue = originalImgHeight;
        newBottom = originalImgHeight - value - top;
        break;
      default: break;
    }
    if (value < 0 || value > upperBoundValue) return;
    // since the coordinate values from the form are based on original image size, we scale the value down to fit the canvas size
    setRegionOfInterestAnnotation(scaleDownRoIAnnotation(
      { left: newLeft, right: newRight, top: newTop, bottom: newBottom },
      imageHorizontalScaling,
      imageVerticalScaling
    ));
  }

  return (
    <SpaceBetween direction="vertical" size="l">
      <RoIEditInputField
        label="Top left X coordinate"
        errorText={(showValidationMessage && !validateX) ? `Left X coordinate must be between 0 to ${maxX}.` : ""}
        description={`Numeric values only. Between 0 to ${maxX}.`}
        disabled={disabled}
        value={`${Math.floor(left)}`}
        onChange={(value): void => onFormValueChange(value, RoIFormField.X)}
      />
      <RoIEditInputField
        label="Top left Y coordinate"
        errorText={(showValidationMessage && !validateY) ? `Left Y coordinate must be between 0 to ${maxY}.` : ""}
        description={`Numeric values only. Between 0 to ${maxY}.`}
        disabled={disabled}
        value={`${Math.floor(top)}`}
        onChange={(value): void => onFormValueChange(value, RoIFormField.Y)}
      />
      <RoIEditInputField
        label="Width"
        errorText={(showValidationMessage && !validateWidth) ? `Width must be between 64 to ${maxW}.` : ""}
        description={`Numeric values only. Between ${CROP_MIN_WIDTH} to ${maxW}.`}
        disabled={disabled}
        value={`${Math.floor(width)}`}
        onChange={(value): void => onFormValueChange(value, RoIFormField.WIDTH)}
      />
      <RoIEditInputField
        label="Height"
        errorText={(showValidationMessage && !validateHeight) ? `Height must be between 64 to ${maxH}.` : ""}
        description={`Numeric values only. Between ${CROP_MIN_HEIGHT} to ${maxH}.`}
        disabled={disabled}
        value={`${Math.floor(height)}`}
        onChange={(value): void => onFormValueChange(value, RoIFormField.HEIGHT)}
      />
    </SpaceBetween>
  );
}