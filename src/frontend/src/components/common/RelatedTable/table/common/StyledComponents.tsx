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

export const Wrapper = styled.div<{ height: number }>`
  position: relative;
  margin-top: -1rem;
  margin-bottom: -1rem;
  margin-left: -1rem;
  height: ${(props): number => props.height}%;
`;

export const LeftPad = styled.div<{ length: number }>`
  display: flex;
  align-items: center;
  margin-left: ${({ length }): number => length || 0}rem;
`;

export const EmptySpace = styled.span<{ width: number; height: number }>`
  position: relative;
  min-width: ${(props): number => props.width}rem;
  height: ${(props): number => props.height}rem;
`;

export const ButtonWrapper = styled.div<{}>`
  align-self: flex-start;
`;