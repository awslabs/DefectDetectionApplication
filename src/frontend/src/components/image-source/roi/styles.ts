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

export const roiEditWrapperStyle = css`
  display: flex;
  gap: 20px;
  flex-wrap: nowrap;
  align-items: flex-start;
`

export const roiSettingContainerStyle = css`
  width: 300px; 
  min-width: 300px;
`

export const roiSettingDescriptionStyle = css`
  color: var(--grey-600, #414D5C) !important;
`

export const roiImagePreviewContainerStyle = css`
  width: calc(100% - 320px);
`

export const roiImagePreviewContentStyle = css`
  width: inherit;
  position: relative;
  display: flex;
  justify-content: center;
`

export const roiImageWrapperStyle = css`
  position: relative;
  display: flex;
  background: var(--gray-gray-100, #F0F1F2);
`

export const roiImageStyle = css`
  display: block;
  max-height: 600px;
  height: fit-content;
  object-fit: contain;
`

export const roiCanvasStyle = css`
  object-fit: contain;
  position: absolute;
`

export const roiActionRowStyle = css`
  display: flex;
  gap: 8px;
  justify-content: flex-end;
`