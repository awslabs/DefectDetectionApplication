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

import {
  AppLayout,
  BreadcrumbGroup,
  Flashbar,
} from "@cloudscape-design/components";
import React, { useEffect } from "react";
import { useMatches, Outlet, useNavigate, useLocation } from "react-router-dom";
import SideNav from "./SideNav";
import { AppLayoutContext } from "./AppLayoutContext";
import { RouteHandle } from "./types";
import TopNav from "./TopNav";
import { isDynamicRouter } from "components/utils";
import { useQuery } from "@tanstack/react-query";
import { getStation } from "api/Station";
import { TableTypes, styleConstants } from "./constants";

export default function Layout(): JSX.Element {
  const location = useLocation();
  const hashValue = location.hash.substring(1);
  const matches = useMatches();
  const breadcrumbs = matches
    // Filters out matches that don't have handle or crumb
    .filter((match) => (match.handle as RouteHandle)?.breadcrumb)
    .map((match) => {
      const breadcrumbValue = (match.handle as RouteHandle).breadcrumb;
      return {
        text: isDynamicRouter(breadcrumbValue)
          // Get hash value from URL if current breadcrumb is defined as dynamic router 
          ? decodeURIComponent(new URLSearchParams(hashValue).get(breadcrumbValue.substring(1)) || "-")
          : breadcrumbValue,
        href: match.pathname,
      }
    });
  const navigate = useNavigate();
  const { openNavigation, setOpenNavigation, notifications, setTableInfo } =
    React.useContext(AppLayoutContext);

  const { data: station } = useQuery({
    queryKey: ["getStation"],
    queryFn: () => getStation(),
  });

  /**
   * clean up history table info caches when navigate to pages which are not under path "/history"
   */
  useEffect(() => {
    if (!location.pathname.startsWith("/history")) {
      setTableInfo({
        tableType: TableTypes.RESULT_HISTORY,
        resetTablesOfType: true,
      })
    }
    if (!location.pathname.startsWith("/capture-results")) {
      setTableInfo({
        tableType: TableTypes.CAPTURE_HISTORY,
        resetTablesOfType: true,
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  return (
    <>
      <TopNav station={station} />
      <AppLayout
        breadcrumbs={
          <BreadcrumbGroup
            onClick={(event): void => {
              // Prevent full page navigation and use React Router's in-page navigation
              event.preventDefault();
              navigate(event.detail.href);
            }}
            items={breadcrumbs}
          />
        }
        navigation={<SideNav station={station} />}
        content={<Outlet />}
        toolsHide={true}
        navigationOpen={openNavigation}
        onNavigationChange={(): void => setOpenNavigation(!openNavigation)}
        stickyNotifications
        notifications={<Flashbar items={notifications} stackItems />}
        maxContentWidth={styleConstants.MAX_CONTENT_WIDTH}
      />
    </>
  );
}
