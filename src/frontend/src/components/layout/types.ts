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

import { FlashbarProps, PropertyFilterProps } from "@cloudscape-design/components";
import { TableTypes } from "./constants";

export interface RouteHandle {
  breadcrumb: string;
}

export type Notification = FlashbarProps.MessageDefinition & {
  /**
   * The relevant path this notification should be shown on. Most notifications
   * should only be shown on one page, and on page navigation, notifications
   * should be cleared.
   */
  relevantPath?: string;
};

export interface TableInfoDetail {
  pageIdx?: number;
  filters?: PropertyFilterProps.Query;
}

export interface TablesInfo {
  [tableType: string]: {
    [tableId: string]: TableInfoDetail
  };
}

export interface TablesPref {
  [tableType: string]: {
    pageSize?: number;
  }
}

export interface SetTableInfoInput {
  tableType: TableTypes;
  tableId?: string;
  tableInfo?: TableInfoDetail;
  resetTablesOfType?: boolean;
}

export interface SetTablePrefInput {
  tableType: TableTypes;
  pageSize: number;
}