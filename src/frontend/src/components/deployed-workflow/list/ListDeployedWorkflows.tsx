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

import * as React from "react";
import format from "date-fns/format";
import {
  Box,
  Header,
  Link,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import {
  WorkflowRegistration,
  listWorkflowRegistrations,
} from "api/WorkflowRegistrationAPI";
import { registrationStatusIndicator } from "../presentation";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EmptyTable from "components/empty-table/EmptyTable";

export default function ListDeployedWorkflows(): JSX.Element {
  const listQuery = useQuery({
    queryKey: ["listWorkflowRegistrations"],
    queryFn: listWorkflowRegistrations,
  });

  const registrations = listQuery.data ?? [];
  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);
  const navigate = useNavigate();

  return (
    <Table
      wrapLines
      loading={listQuery.isFetching}
      loadingText="Loading deployed workflows"
      header={
        <Header
          variant="h1"
          description={
            <>
              Workflows built in the cloud and deployed to this device.
              <br />
              Select a workflow to view its executions or run it.
            </>
          }
          counter={`(${registrations.length})`}
        >
          Deployed workflows
        </Header>
      }
      columnDefinitions={[
        {
          id: "workflowId",
          header: "Workflow",
          cell: (item: WorkflowRegistration): React.ReactNode => {
            const url = `/deployed-workflows/${item.registrationId}`;
            return (
              <Link
                href={url}
                onFollow={(event): void => {
                  event.preventDefault();
                  navigate(url);
                }}
              >
                {item.workflowId}
              </Link>
            );
          },
        },
        {
          id: "version",
          header: "Version",
          cell: (item: WorkflowRegistration) => item.version || "-",
        },
        {
          id: "arch",
          header: "Architecture",
          cell: (item: WorkflowRegistration) => item.arch || "-",
        },
        {
          id: "status",
          header: "Status",
          cell: (item: WorkflowRegistration): React.ReactNode => {
            const indicator = registrationStatusIndicator(item);
            return (
              <>
                <StatusIndicator type={indicator.type}>
                  {indicator.text}
                </StatusIndicator>
                {item.status !== "registered" && item.invalidReason && (
                  <Box variant="p" color="text-status-error">
                    {item.invalidReason}
                  </Box>
                )}
              </>
            );
          },
        },
        {
          id: "registeredAt",
          header: `Registered ${timezoneLabel}`,
          cell: (item: WorkflowRegistration) =>
            item.registeredAt
              ? format(item.registeredAt * 1000, DATE_WITHOUT_TZ)
              : "-",
        },
      ]}
      items={registrations}
      empty={
        <EmptyTable
          header="No deployed workflows"
          message="No deployed workflows to display."
        />
      }
      variant="full-page"
    />
  );
}
