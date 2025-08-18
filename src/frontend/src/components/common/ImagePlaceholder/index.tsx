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
import { ReactNode } from "react";
import {
  imagePlaceholderCenterContentStyle,
  imagePlaceholderContainerStyle,
  imagePlaceholderNormalContentStyle
} from "./styles";

type Placement = "center" | "start";

interface ImagePlaceholderProps {
  height?: number | string;
  width?: number | string;
  content?: ReactNode;
  placement?: Placement;
}

function getContentStyle(placement: Placement): string {
  switch (placement) {
    case "start":
      return imagePlaceholderNormalContentStyle;
    case "center":
      return imagePlaceholderCenterContentStyle;
    default:
      return "";
  }
}

export default function ImagePlaceholder({
  height,
  width,
  content,
  placement = "start"
}: ImagePlaceholderProps): JSX.Element {
  return (
    <div className={imagePlaceholderContainerStyle({ height, width })}>
      <div className={getContentStyle(placement)}>
        {content}
      </div>
    </div>
  );
}