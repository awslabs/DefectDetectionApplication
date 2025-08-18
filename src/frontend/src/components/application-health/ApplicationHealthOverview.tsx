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
  ContentLayout,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import { getSystemHealth } from "api/SystemHealthAPI";
import { useQuery } from "@tanstack/react-query";
import SystemOverviewContainer from "./SystemOverviewContainer";
import ApplicationLogsAndRestartContainer from "./ApplicationLogsAndRestartContainer";
import { HEALTH_PAGE_API_TIMEOUT } from "./constants";

const systemHealthPollInterval = 2000;

export default function ApplicationHealthOverview(): JSX.Element {
  const systemHealth = useQuery({
    queryKey: ["getSystemHealth"],
    queryFn: () => getSystemHealth(HEALTH_PAGE_API_TIMEOUT),
    refetchInterval: (data) => {
      return systemHealthPollInterval;
    },
  });

  const systemHealthProps = !!systemHealth.data ? { systemHealth: systemHealth.data } : {};
  return (
    <ContentLayout
      header={<Header variant="h1">Application health overview</Header>}
    >
      <SpaceBetween size="l">
        <SystemOverviewContainer {...systemHealthProps} />
        <ApplicationLogsAndRestartContainer />
      </SpaceBetween>
    </ContentLayout>
  );
}
