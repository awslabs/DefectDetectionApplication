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

import { AppLayout, TopNavigation } from "@cloudscape-design/components";
import { ReactNode } from "react";
import { TitleContainer, Title, AWSLogo } from "./TopNavStyles";

export default function AuthLayout({ children }: { children: ReactNode }): JSX.Element {
  return (
    <>
      <TopNavigation
        identity={{
          href: "/",
          // TODO: This is a temporary fix for the title styling until the TopNavigation supports a seccond logo
          // @ts-ignore
          title: <TitleContainer><Title>Automated Inspection</Title><AWSLogo>powered by AWS</AWSLogo></TitleContainer>,
        }}
      />
      <AppLayout
        navigationHide
        toolsHide
        content={children}
      />
    </>
  );
}