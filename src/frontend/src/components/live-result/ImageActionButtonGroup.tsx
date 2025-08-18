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

import { useEffect, useState } from "react";
import { css } from "@emotion/css";
import { ReactZoomPanPinchContentRef } from "react-zoom-pan-pinch";
import { Button, IconProps } from "@cloudscape-design/components";
import { unselectedImageActionButtonStyle, selectedImageActionButtonStyle, disabledImageActionButtonStyle, imageActionsContainerStyle, imageActionButtonStyle } from "components/live-result/styles";

export enum ImageActionButtonType {
  PAN = "PAN",
  ZOOM_IN = "ZOOM_IN",
  ZOOM_OUT = "ZOOM_OUT",
  RESET = "RESET",
}

interface ImageActionButtonGroupProps {
  onClickPan: (isSelected: boolean) => void;
  disabled?: boolean;
  extraActions?: JSX.Element;
}

type ImageActionButtonConfig = {
  type: ImageActionButtonType;
  icon: IconProps.Name;
  onClick: () => void;
  className?: string;
}

export default function ImageActionButtonGroup({
  zoomIn,
  zoomOut,
  resetTransform,
  onClickPan,
  disabled,
  extraActions,
}: ImageActionButtonGroupProps & ReactZoomPanPinchContentRef): JSX.Element {
  const [selectedButton, setSelectedButton] = useState<ImageActionButtonType | undefined>(undefined);
  const imageActionButtonConfigs: ImageActionButtonConfig[] = [
    {
      type: ImageActionButtonType.PAN,
      icon: "expand",
      onClick: () => onClickPan(selectedButton === ImageActionButtonType.PAN),
      className: css`
          svg {
            transform: rotate(45deg);
          }
        `
    },
    {
      type: ImageActionButtonType.ZOOM_IN,
      icon: "zoom-in",
      onClick: () => zoomIn(),
    },
    {
      type: ImageActionButtonType.ZOOM_OUT,
      icon: "zoom-out",
      onClick: () => zoomOut(),
    },
    {
      type: ImageActionButtonType.RESET,
      icon: "undo",
      onClick: () => resetTransform()
    }
  ];

  useEffect(() => {
    const keyEventHandler = (event: KeyboardEvent): void => {
      if ((event.metaKey || event.ctrlKey) && event.code === "Digit0") {
        resetTransform();
      }
    };
    document.addEventListener("keydown", keyEventHandler);
    return (() => {
      document.removeEventListener("keydown", keyEventHandler);
    })
  }, [resetTransform]);

  return (
    <div className={imageActionsContainerStyle}>
      <div className={disabled ? disabledImageActionButtonStyle : ""}>
        <div className={imageActionButtonStyle}>
          {
            imageActionButtonConfigs.map(config => {
              const isSelected = selectedButton === config.type;
              return (
                <Button
                  key={config.type}
                  iconName={config.icon}
                  variant="icon"
                  onClick={(): void => {
                    if (config.type === ImageActionButtonType.PAN) {
                      if (selectedButton === config.type) {
                        setSelectedButton(undefined);
                      } else {
                        setSelectedButton(config.type);
                      }
                    }
                    config.onClick();
                  }}
                  className={`${isSelected ? selectedImageActionButtonStyle : unselectedImageActionButtonStyle} ${config.className}`}
                  disabled={disabled}
                  formAction="none"
                  data-testid={`image-action-button-${config.type}`}
                />
              )
            })
          }
        </div>
      </div>
      {extraActions}
    </div>
  );
}