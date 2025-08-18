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
import { colorBackgroundDropdownItemHover } from "@cloudscape-design/design-tokens";
import { css } from "@emotion/css";

export const imagePlaceholderContainerStyle = ({ height, width }: { height?: number | string; width?: number | string }): string => css`
  background: ${colorBackgroundDropdownItemHover};
  height: ${height || "576px"};
  width: ${width || "100%"};
  min-width: 300px;
`;

export const imagePlaceholderCenterContentStyle = css`
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: calc(100% - 20px);
  width: calc(100% - 20px);
  padding: 10px;
`;

export const imagePlaceholderNormalContentStyle = css`
  padding: 10px;
`;