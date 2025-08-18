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
import { useEffect, useState } from 'react';
import { ITreeNode, TreeMap } from '../model/TreeNode';
import { TreeUtility } from '../model/TreeUtility';
import { UseCollectionOptions, UseCollectionResult, useCollection } from "@cloudscape-design/collection-hooks";
import { TableProps } from '@cloudscape-design/components';

export interface UseTreeCollection<T> extends UseCollectionOptions<ITreeNode<T>> {
  keyPropertyName: string;
  parentKeyPropertyName: string;
  expandedPropertyName?: keyof T;
  columnDefinitions: ReadonlyArray<TableProps.ColumnDefinition<T>>;
}

export interface UseTreeCollectionResult<T> extends UseCollectionResult<ITreeNode<T>> {
  expandNode: (node: ITreeNode<T>) => void;
  reset: () => void;
}

export const useTreeCollection = <T>(
  items: T[],
  props: UseTreeCollection<T>,
  expanded: boolean = false
): UseTreeCollectionResult<T> => {
  const {
    keyPropertyName,
    parentKeyPropertyName,
    expandedPropertyName,
    columnDefinitions,
    ...collectionProps
  } = props;
  const [treeMap, setTreeMap] = useState<TreeMap<T>>(new Map());
  const [nodes, setNodes] = useState<ITreeNode<T>[]>([]);
  const [sortState, setSortState] = useState<TableProps.SortingState<T>>({
    ...(collectionProps.sorting?.defaultState || {}),
  } as TableProps.SortingState<T>);
  const [columnsDefinitions] = useState(columnDefinitions);
  const [nodesExpanded, addNodesExpanded] = useState<{ [key: string]: boolean }>({});

  useEffect(() => {
    const treeNodes = TreeUtility.buildTreeNodes(
      items,
      treeMap,
      keyPropertyName,
      parentKeyPropertyName,
      expandedPropertyName
    );
    TreeUtility.sortTree(treeNodes, sortState, columnsDefinitions);
    // only builds prefix after building and sorting the tree
    const tree = TreeUtility.buildTreePrefix(treeNodes);

    setNodes(TreeUtility.flatTree(tree));
  }, [
    items,
    keyPropertyName,
    parentKeyPropertyName,
    expandedPropertyName,
    sortState,
    columnsDefinitions,
    treeMap,
  ]);

  const expandNode = (node: ITreeNode<T>) => {
    if (node) {
      const key = (node as any)[keyPropertyName];
      const internalNode = nodes.find((n) => (n as any)[keyPropertyName] === key)!;
      internalNode.toggleExpandCollapse();
      TreeUtility.expandOrCollapseChildren(internalNode, treeMap, keyPropertyName);
      treeMap.set(key, internalNode);
      const updatedNodes = nodes.concat([]);
      setNodes(updatedNodes);
      setTreeMap(treeMap);
    }
  };

  const reset = () => {
    setNodes([]);
    setTreeMap(new Map());
  };

  const hasPropertyFiltering = !!collectionProps.propertyFiltering;

  const internalCollectionProps = {
    ...collectionProps,
    sorting: undefined, // disable useCollection sort in favor of TreeUtility.sortTree
    filtering: !hasPropertyFiltering
      ? {
        ...collectionProps.filtering,
        filteringFunction: (
          item: ITreeNode<T>,
          filteringText: string,
          filteringFields?: string[]
        ) =>
          TreeUtility.filteringFunction(
            item as ITreeNode<any>,
            filteringText,
            filteringFields,
            collectionProps.filtering?.filteringFunction as any
          ),
      }
      : undefined,
  };

  useEffect(() => {
    if (expanded) {
      let newNodesExpanded: { [key: string]: boolean } = {};

      nodes.forEach((node) => {
        if (!nodesExpanded[node.id]) {
          if (!node.isExpanded()) {
            node.toggleExpandCollapse();
          }
          node.setVisible(true);
          newNodesExpanded = { ...newNodesExpanded, [node.id]: true };
        }
      });

      if (Object.keys(newNodesExpanded).length > 0) {
        addNodesExpanded({ ...nodesExpanded, ...newNodesExpanded });
      }
    }
  }, [nodesExpanded, nodes, expanded]);

  const collectionResult = useCollection(nodes, internalCollectionProps);

  const isTextFiltering = collectionResult.filterProps.filteringText.length !== 0;

  const useCollectionResult = {
    ...collectionResult,
    items:
      !hasPropertyFiltering && isTextFiltering
        ? collectionResult.items
        : collectionResult.items.filter((item) => item.isVisible()),
    collectionProps: {
      ...collectionResult.collectionProps,
      sortingColumn: sortState.sortingColumn,
      sortingDescending: sortState.isDescending,
      onSortingChange: (event: CustomEvent<TableProps.SortingState<T>>) => {
        setSortState(event.detail);
        const customOnSortingChange = collectionResult.collectionProps.onSortingChange;
        if (customOnSortingChange) {
          customOnSortingChange(event);
        }
      },
    },
  } as UseCollectionResult<ITreeNode<T>>;

  return {
    expandNode,
    reset,
    ...useCollectionResult,
  };
};