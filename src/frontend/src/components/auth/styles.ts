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

const flexCenterStyle = `
  display: flex !important; 
  justify-content: center !important;
`;

export const authRequireContainerWrapperStyle = css`
  width: 100%;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  display: flex; 
`;

export const urlParamsValidateAlertStyle = css`
  position: fixed;
`;

export const authRequireContainerStyle = css`
  width: 340px; 
  max-width: 100%; 
  margin-top: 142px;
`;

export const authRequireContainerHeaderStyle = css`
  width: 100%;
  ${flexCenterStyle}
`;

export const authRequireContainerContentStyle = css`
  width: 100%;
  ${flexCenterStyle}
`;

export const authConfigErrorAlertStyle = css`
  max-width: 700px !important;
`
