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

import { ITreeNode } from '../model/TreeNode';
import { ButtonI18nLabels, ButtonWithTreeLines } from './ButtonWithTreeLines';
import { Table, TableProps } from '@cloudscape-design/components';

export interface RelatedTableProps<T> extends TableProps<T> {
  expandChildren: (node: T) => void;
  expandColumnPosition?: number;
  buttonI18nLabels?: ButtonI18nLabels;
  filteringText?: string;
}


export default function RelatedTable<T>(props: RelatedTableProps<ITreeNode<T>>): JSX.Element {
  const {
    columnDefinitions,
    items,
    expandChildren,
    expandColumnPosition = 1,
    filteringText = '',
    buttonI18nLabels,
  } = props;
  const isFiltering = filteringText !== '';
  const zeroBasedColumnPos = expandColumnPosition - 1;
  const columns = [...columnDefinitions];
  const columnToExpand = columns[zeroBasedColumnPos];
  columns[zeroBasedColumnPos] = {
    ...columnToExpand,
    cell: (node): JSX.Element => {
      const cell = columnToExpand?.cell(node);
      return (
        <ButtonWithTreeLines
          alwaysExpanded={isFiltering}
          node={node}
          buttonI18nLabels={buttonI18nLabels}
          content={cell}
          onClick={(): void => {
            expandChildren(node);
          }}
        />
      );
    },
  };
  return <Table {...props} columnDefinitions={columns} items={items || []} />;
}