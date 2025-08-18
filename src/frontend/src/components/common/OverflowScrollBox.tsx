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

import { colorBackgroundContainerContent } from "@cloudscape-design/design-tokens";
import { css } from "@emotion/css";
import { ReactNode } from "react";

const outerBoxStyle = css`
  overflow-x: scroll;
  padding: 10px 10px 10px 0px;
  ::-webkit-scrollbar {
      -webkit-appearance: none;
  }

  ::-webkit-scrollbar:vertical {
      width: 8px;
  }

  ::-webkit-scrollbar:horizontal {
      height: 8px;
  }

  ::-webkit-scrollbar-thumb {
      border-radius: 8px;
      border: 2px solid ${colorBackgroundContainerContent}; /* should match background, can't be transparent */
      background-color: rgba(0, 0, 0, .5);
  }
`;

interface OverflowScrollBoxProps {
  contentMinWidth: number;
  children: ReactNode;
}

export default function OverflowScrollBox({ contentMinWidth, children }: OverflowScrollBoxProps): JSX.Element {
  const innerBoxStyle = css`
    min-width: ${contentMinWidth}px; 
  `;
  return (
    <div className={outerBoxStyle}>
      <div className={innerBoxStyle}>
        {children}
      </div>
    </div>
  );
}