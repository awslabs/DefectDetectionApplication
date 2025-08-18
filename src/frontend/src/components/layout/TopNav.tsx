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

import { TopNavigation } from "@cloudscape-design/components";
import useAuth from "components/auth/authHook";
import { Station } from "components/station/types";
import { useNavigate } from "react-router-dom";
import { TitleContainer, Title, AWSLogo } from "./TopNavStyles";

interface TopNavProps {
  station?: Station;
}

export default function TopNav({ station }: TopNavProps): JSX.Element {
  const navigate = useNavigate();
  const { signout, authEnabled } = useAuth();
  const { tenantId = "-" } = station || {};

  return (
    <TopNavigation
      identity={{
        href: "/",
        // TODO: This is a temporary fix for the title styling. 
        // Change back to logo image when the TopNavigation supports custom slot
        // @ts-ignore
        title: <TitleContainer><Title>Automated Inspection</Title><AWSLogo>powered by AWS</AWSLogo></TitleContainer>,
        ...(station?.logoImage ? { logo: { src: station.logoImage} } : {}),
        onFollow: (event): void => {
          // Prevent full page navigation and use React Router's in-page navigation
          event.preventDefault();
          navigate("/");
        },
      }}
      i18nStrings={{
        searchIconAriaLabel: "Search",
        searchDismissIconAriaLabel: "Close search",
        overflowMenuTriggerText: "More",
        overflowMenuTitleText: "All",
        overflowMenuBackIconAriaLabel: "Back",
        overflowMenuDismissIconAriaLabel: "Close menu",
      }}
      utilities={[
        authEnabled ? {
          type: "menu-dropdown",
          iconName: "user-profile",
          onItemClick: (({ detail }): void => {
            switch (detail?.id) {
              case "signout":
                signout();
                break;
              default: break;
            }
          }),
          text: tenantId,
          items: authEnabled ? [{
            id: "signout",
            text: "Sign out",
          }] : []
        } : {
          type: "button",
          iconName: "user-profile",
          text: tenantId,
        }
      ]}
    />
  );
}
