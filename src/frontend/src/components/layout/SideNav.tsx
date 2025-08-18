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

import { SideNavigation } from "@cloudscape-design/components";
import { useLocation, useNavigate } from "react-router-dom";
import { constructWebuxUrl } from "./helpers";
import { Station } from "components/station/types";

interface SideNavProps {
  station?: Station;
}

export default function SideNav({ station }: SideNavProps): JSX.Element {
  const location = useLocation();
  const routePrefix = "/" + location.pathname.split("/")[1];
  const navigate = useNavigate();

  const {
    name: stationName = "Station",
    version: stationVersion = "unknown",
    webuxUrl = "",
    tenantId = "",
    deviceId = "",
  } = station || {};
  const stationWebuxUrl = constructWebuxUrl(webuxUrl, deviceId, tenantId);

  return (
    <SideNavigation
      // Use prefix as activeHref so nested routes can highlight same link
      activeHref={routePrefix}
      header={{ href: "/", text: stationName }}
      onFollow={(event): void => {
        // Prevent full page navigation and use React Router's in-page navigation
        if (!event.detail.external) {
          event.preventDefault();
          navigate(event.detail.href);
        }
      }}
      items={[
        {
          type: "section",
          text: "Configure",
          items: [
            { type: "link", text: "Image sources", href: "/image-sources" },
            { type: "link", text: "Workflows", href: "/workflows" },
            { type: "link", text: "Deployed models", href: "/models" },
          ],
        },

        {
          type: "section",
          text: "Capture",
          items: [
            {
              type: "link",
              text: "Capture images",
              href: "/capture",
            },
            {
              type: "link",
              text: "Image capture results",
              href: "/capture-results",
            }
          ]
        },

        {
          type: "section",
          text: "Infer",
          items: [
            {
              type: "link",
              text: "Run inference",
              href: "/result",
            },
            {
              type: "link",
              text: "Inference results",
              href: "/history",
            },
          ],
        },

        {
          type: "section",
          text: "Application health",
          items: [
            {
              type: "link",
              text: "Application health overview",
              href: "/application-health",
            },
          ],
        },
      ]}
    />
  );
}
