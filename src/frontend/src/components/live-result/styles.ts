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

import { css } from "@emotion/css";
import { RESULT_DATA_COLUMN_WIDTH } from "./constants";
import { colorBackgroundItemSelected, colorBorderItemSelected, colorTextBreadcrumbCurrent, colorTextInteractiveInvertedHover } from "@cloudscape-design/design-tokens";
import { flexBoxNoWrapStyle, flexBoxStyle } from "styles/common";

const imageActionButtonStyleCommonPart = `
  padding: 20px !important;
  border-radius: 0 !important;
`;

export const resultLayoutStyle = css`
  ${flexBoxNoWrapStyle}
  gap: 40px;
  justify-content: space-between;
  align-items: stretch;
`;

export const errorMessageDetailStyle = css`
  margin-left: 20px;
  display: block;
  word-break: break-word;
`;

export const resultCardStyle = css`
  width: ${RESULT_DATA_COLUMN_WIDTH}px;
  min-width: ${RESULT_DATA_COLUMN_WIDTH}px;
`;

export const resultImageContainerStyle = css`
  width: calc(100% - ${RESULT_DATA_COLUMN_WIDTH}px - 80px);
`;

export const imageStyle = css`
  display: block;
  max-height: 650px;
  min-height: 64px;
  height: 100%;
  object-fit: contain;
`;

export const imageContainerStyle = css`
  width: inherit;
  position: relative;
`
export const canvasStyle = css`
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
`;

export const unselectedImageActionButtonStyle = css`
  ${imageActionButtonStyleCommonPart}
  border: 1px solid transparent !important;
  &:hover {
    background-color: ${colorTextInteractiveInvertedHover} !important;
  }
`;

export const selectedImageActionButtonStyle = css`
  ${imageActionButtonStyleCommonPart}
  background-color: ${colorBackgroundItemSelected} !important;
  border: 1px solid ${colorBorderItemSelected} !important;
`;

export const disabledImageActionButtonStyle = css`
  width: fit-content !important;
  &:hover {
    cursor: not-allowed !important;
  }
`;

export const imageActionButtonStyle = css`
  ${flexBoxNoWrapStyle}
  gap: 4px;
`;

export const nextImageNameDislayStyle = css`
  display: flex;
  flexWrap: nowrap;
  gap: 8px;
`;

export const nextImageNameTextStyle = css`
  word-break: break-all;
  min-width: 100px;
`;

export const nextImageLabelStyle = css`
  white-space: nowrap
`;

export const inferenceActionButtonStyle = css`
  ${flexBoxStyle}
  gap: 16px; 
  justify-content: end;
  align-items: center;
`

export const imageActionsContainerStyle = css`
  ${flexBoxNoWrapStyle}
  justify-content: space-between;
  align-items: center;
`;

export const folderPreviewLayoutStyle = css`
  ${flexBoxStyle}
  justify-content: space-between;
  gap: 20px;
`;

export const folderPreviewDescStyle = css`
  color: ${colorTextBreadcrumbCurrent};
`;

export const outputFileNameStyle = css`
  word-break: break-all;
`;

export const displayNoneStyle = css`
  display: none;
`;

export const fullWidthStyle = css`
  width: 100%;
`;