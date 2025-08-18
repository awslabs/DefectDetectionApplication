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

import React, { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { v4 as uuid } from "uuid";
import { shouldShowSideNav } from "./helpers";
import { Notification, SetTableInfoInput, SetTablePrefInput, TableInfoDetail, TablesInfo, TablesPref } from "./types";

const MAX_NOTIFICATIONS = 10;

export const AppLayoutContext = React.createContext({
  openNavigation: false,
  setOpenNavigation: (value: boolean) => { },
  notifications: [] as Notification[],
  tablesInfo: {} as TablesInfo,
  tablesPref: {} as TablesPref,
  setTableInfo: ({ tableType, tableId, tableInfo, resetTablesOfType }: SetTableInfoInput) => { },
  setTableTypePref: ({ tableType, pageSize }: SetTablePrefInput) => { },
  addNotification: (notification: Notification) => { },
  removeNotification: (notificationId: string) => { },
  addSuccess: (notification: Notification) => { },
  addError: (notification: Notification) => { },
});

interface AppLayoutProviderProps {
  children: React.ReactNode;
}

export function AppLayoutProvider({
  children,
}: AppLayoutProviderProps): JSX.Element {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [tablesInfo, setTablesInfo] = useState<TablesInfo>({});
  const [tablesPref, setTablesPref] = useState<TablesPref>({});
  const location = useLocation();

  const addNotification = (notification: Notification): void => {
    // By default a notification has a random UUID and is dismissible
    const id = notification.id || uuid();
    const handledNotification: Notification = {
      ...notification,
      id,
      dismissible: notification.dismissible ?? true,
      onDismiss:
        notification.onDismiss ??
        ((): void =>
          // Use functional setter to avoid stale closure
          // https://dmitripavlutin.com/react-hooks-stale-closures/#4-state-closure-in-usestate
          setNotifications((notifications) =>
            notifications.filter((notification) => notification.id !== id),
          )),
      // Defaults to current page. Most error notifications will have this
      // configuration.
      relevantPath: notification.relevantPath ?? location.pathname,
    };

    if (notifications.length >= MAX_NOTIFICATIONS) {
      setNotifications([
        handledNotification,
        // Drops the last message if over limit
        ...notifications.slice(0, MAX_NOTIFICATIONS - 1),
      ]);
    } else {
      setNotifications([handledNotification, ...notifications]);
    }
  };

  const removeNotification = (notificationId: string): void => {
    const targetIdx = notifications.findIndex(notification => notification.id === notificationId);
    if (targetIdx >= 0) {
      notifications.splice(targetIdx, 1);
      setNotifications([...notifications]);
    }
  }

  // Helpers to specify type of notification
  const addSuccess = (notification: Notification): void =>
    addNotification({
      ...notification,
      type: "success",
    });
  const addError = (notification: Notification): void =>
    addNotification({
      ...notification,
      type: "error",
    });

  const [openNavigation, setOpenNavigation] = React.useState(
    shouldShowSideNav(location),
  );

  useEffect(() => {
    // Show/hide side nav when page changes
    setOpenNavigation(shouldShowSideNav(location));

    // Remove notification if no longer on relevant page
    setNotifications((notifications) => {
      const relevantNotifications = notifications.filter(
        ({ relevantPath: relevantUrl }) =>
          !relevantUrl || location.pathname === relevantUrl,
      );
      return relevantNotifications;
    });
  }, [location, setOpenNavigation]);

  /**
   * 
   * @param tableType Scope down to a type of table. e.g. TableTypes.RESULT_HISTORY
   * @param tableId Claim the specific table to be updated. If tableInfo is provided but with no tableId, all the tables within table type will be updated
   * @param tableInfo The info that needs to be updated. This won't reset other info which is not passed in
   * @param resetTablesOfType Reset other tables (other than the table(s) to be updated) within the table type
   * 
   * Note: to reset all tables within the type, pass { tableType: TABLE_TYPE, resetTablesOfType: true } to the function
   */
  const setTableInfo = ({
    tableType,
    tableId = "",
    tableInfo = {},
    resetTablesOfType
  }: SetTableInfoInput): void => {
    const { [tableType]: curTableTypeInfo = {} } = tablesInfo;
    const tableInfoUpdate: { [tableId: string]: TableInfoDetail } = {};
    if (!!tableId) {
      // update the target table
      const { [tableId]: curTableInfo = {} } = curTableTypeInfo;
      tableInfoUpdate[tableId] = { ...curTableInfo, ...tableInfo };
    } else if (Object.keys(tableInfo).length > 0) {
      // update all the existing tables with the type
      for (let key of Object.keys(curTableTypeInfo)) {
        tableInfoUpdate[key] = {
          ...curTableTypeInfo[key],
          ...tableInfo,
        }
      }
    }
    setTablesInfo({
      ...tablesInfo,
      [tableType]: {
        ...(!resetTablesOfType ? (curTableTypeInfo || {}) : {}),
        ...tableInfoUpdate,
      }
    })
  }

  const setTableTypePref = ({ tableType, pageSize }: SetTablePrefInput): void => {
    setTablesPref({
      ...tablesPref,
      [tableType]: {
        ...(tablesPref[tableType] || {}),
        pageSize,
      }
    })
  }

  return (
    <AppLayoutContext.Provider
      value={{
        openNavigation,
        setOpenNavigation,
        notifications,
        addNotification,
        removeNotification,
        addSuccess,
        addError,
        setTableInfo,
        setTableTypePref,
        tablesPref,
        tablesInfo,
      }}
    >
      {children}
    </AppLayoutContext.Provider>
  );
}
