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

import { EmptySpace, LeftPad, Wrapper, ButtonWrapper } from "./common/StyledComponents";
import { ExpandableTableNodeStatus, ITreeNode } from '../model/TreeNode';
import { createPrefixLines, Theme } from './common/TreeLines';
import { THEME } from '../config';
import { Button } from '@cloudscape-design/components';
import { memo } from "react";

const TABLE_ROW_HEIGHT_PERCENT = 100;
const WRAPPER_EXTRA_HEIGHT_PERCENT = 25;
export const MARGIN_LEFT_REM_MULTIPLICATOR = 2;

const noAction = () => { };
const theme = THEME as Theme;
const emptySpaceHeight = theme === Theme.POLARIS_OPEN_SOURCE ? 2 : 3;
const emptySpaceWidth = theme === Theme.POLARIS_OPEN_SOURCE ? 0.4 : 0.5;

export interface ButtonI18nLabels {
  expand: string;
  collapse: string;
}
export interface ButtonWithTreeLinesProps<T> {
  node: ITreeNode<T>;
  content: React.ReactNode;
  onClick?: () => void;
  alwaysExpanded: boolean;
  buttonI18nLabels?: ButtonI18nLabels;
}

function createToggleButton<T>(props: ButtonWithTreeLinesProps<T>) {
  const { node, onClick, alwaysExpanded } = props;
  const icon = node.isExpanded() || alwaysExpanded ? 'treeview-collapse' : 'treeview-expand';
  const i18nLabel =
    node.isExpanded() || alwaysExpanded
      ? props.buttonI18nLabels?.collapse
      : props.buttonI18nLabels?.expand;
  return node.getChildren().length > 0 || node.hasChildren ? (
    <ButtonWrapper>
      <Button
        disabled={node.getStatus() !== ExpandableTableNodeStatus.normal}
        variant="icon"
        ariaLabel={i18nLabel}
        iconName={icon}
        onClick={alwaysExpanded ? noAction : onClick}
      />
    </ButtonWrapper>
  ) : (
    <EmptySpace height={emptySpaceHeight} width={emptySpaceWidth} />
  );
}

export const ButtonWithTreeLines = memo(function ButtonWithTreeLinesComp<T>(
  props: ButtonWithTreeLinesProps<T>
) {
  const { node, content, alwaysExpanded } = props;
  const leftPadLength = node.getPrefix().length
    ? MARGIN_LEFT_REM_MULTIPLICATOR * (node.getPrefix().length - 1)
    : 0;
  return (
    <Wrapper height={TABLE_ROW_HEIGHT_PERCENT + WRAPPER_EXTRA_HEIGHT_PERCENT}>
      <LeftPad length={leftPadLength}>
        {createPrefixLines(node, theme, alwaysExpanded)}
        {createToggleButton(props)}
        {content}
      </LeftPad>
    </Wrapper>
  );
});