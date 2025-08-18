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

import { Box, ColumnLayout, Icon, Toggle } from "@cloudscape-design/components";
import { inferenceActionButtonStyle } from "./styles";

interface RefreshDisplayActionsProps {
  showAnomalyMaskToggle?: boolean;
  onClickAnomalyMaskToggle: (checked: boolean) => void;
  anomalyMaskToggleChecked?: boolean;
  showFlagForReviewToggle?: boolean;
  flagForReviewToggleChecked?: boolean;
}

export default function RefreshDisplayActions({
  showAnomalyMaskToggle,
  onClickAnomalyMaskToggle,
  anomalyMaskToggleChecked,
  showFlagForReviewToggle,
  flagForReviewToggleChecked,
}: RefreshDisplayActionsProps): JSX.Element {
  return (
    <Box float="right">
      <ColumnLayout columns={2}>
        <div className={inferenceActionButtonStyle}>
          {showAnomalyMaskToggle && (
            <Toggle
              onChange={({ detail }): void =>
                onClickAnomalyMaskToggle?.(detail.checked)
              }
              checked={!!anomalyMaskToggleChecked}
              data-testid="refresh-display-show-anomaly-masks-toggle"
            >
              Show anomaly masks
            </Toggle>
          )}
        </div>
        <div className={inferenceActionButtonStyle}>
          {showFlagForReviewToggle && (
            <Toggle checked={!!flagForReviewToggleChecked}>
              Flag for review
              <Icon name="flag" />
            </Toggle>
          )}
        </div>
      </ColumnLayout>
    </Box>
  );
}
