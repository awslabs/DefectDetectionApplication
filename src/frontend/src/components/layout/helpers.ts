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

import { Location } from "react-router-dom";
import { HIDE_SIDE_NAV_ROUTES } from "./constants";

export const shouldShowSideNav = (location: Location): boolean => {
  // For form submission pages (e.g. create) we want the side nav to be hidden
  // by default. For all other pages we want to show the side nav by default.
  return !HIDE_SIDE_NAV_ROUTES.find(route => location.pathname.substring(1).startsWith(route));
};

export const constructWebuxUrl = (
  webuxUrl: string,
  stationId: string,
  tenantId: string,
): string => {
  if (!webuxUrl || !stationId || !tenantId) {
    return "";
  }
  return `${webuxUrl}/stations/${stationId}#tenantId=${tenantId}`;
};
