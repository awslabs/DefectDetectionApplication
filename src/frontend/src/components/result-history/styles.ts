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

import { colorBackgroundStatusError, colorBackgroundStatusSuccess, colorBorderStatusError, colorBorderStatusSuccess } from "@cloudscape-design/design-tokens";
import { css } from "@emotion/css";
import { flexBoxNoWrapStyle } from "styles/common";

export const noteEditIconInlineStyle = css`
  float: right
`

const commonInferenceBoxStyle = css`
  border-radius: 4px;
  overflow: hidden; // to avoid the child element background color to overlay and hide on the rounded border corner
`;

export const anomalylInferenceBoxStyle = css`
  ${commonInferenceBoxStyle}
  border: 1px solid ${colorBorderStatusError};
  .result-section {
    background-color: ${colorBackgroundStatusError};
  }
`

export const normallInferenceBoxStyle = css`
  ${commonInferenceBoxStyle}
  border: 1px solid ${colorBorderStatusSuccess};
  .result-section {
    background-color: ${colorBackgroundStatusSuccess};
  }
`

export const verifyButtonStyle = css`
  padding: 14px 0px 6px 0px !important;
  border: 0px !important;
`

export const verificationRowStyle = css`
  ${flexBoxNoWrapStyle}
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
`