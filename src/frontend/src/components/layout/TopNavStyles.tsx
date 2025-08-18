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

import styled from "styled-components";

export const AWSLogo = styled.span`
  color: var(--white, #fff);
  text-align: center;
  font-family: "Open Sans";
  font-size: 12px;
  font-style: normal;
  font-weight: 600;

  height: 30px;
  padding-top: 12px;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
`;

export const Title = styled.span`
  color: var(—white, #fff);
  text-align: center;
  font-family: "Open Sans";
  font-size: 20px;
  font-style: normal;
  font-weight: 600;
  line-height: 24px; /* 120% */
  letter-spacing: -0.2px;
`;

export const TitleContainer = styled.span`
  display: flex;
  align-items: baseline;
  gap: 6px;
`;
