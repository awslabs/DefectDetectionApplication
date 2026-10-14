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
  Button,
  Header,
  Link,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import {
  WorkflowRegistration,
  listWorkflowRegistrations,
  triggerWorkflowRegistration,
} from "api/WorkflowRegistrationAPI";
import { canTrigger, registrationStatusIndicator } from "../presentation";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EmptyTable from "components/empty-table/EmptyTable";
import { AppLayoutContext } from "components/layout/AppLayoutContext";

export default function ListDeployedWorkflows(): JSX.Element {
  const listQuery = useQuery({
    queryKey: ["listWorkflowRegistrations"],
    queryFn: listWorkflowRegistrations,
  });

  const registrations = listQuery.data ?? [];
  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addError } = React.useContext(AppLayoutContext);

  // Trigger a manual run directly from the list, mirroring the "Run workflow"
  // action on the details page. A single mutation keyed by registrationId
  // backs every row's button; the run's registrationId is the mutate variable
  // so per-row loading state can be derived from triggerMutation.variables.
  const triggerMutation = useMutation({
    mutationFn: (registrationId: string) =>
      triggerWorkflowRegistration(registrationId),
    onSuccess: (_execution, registrationId) => {
      // Navigate to the details page so the operator can watch the new
      // execution progress (the details view polls while a run is active).
      navigate(`/deployed-workflows/${registrationId}`);
      queryClient.invalidateQueries({
        queryKey: ["listWorkflowRegistrations"],
      });
    },
    // Surface the backend rejection (e.g. the 409 for invalid registrations)
    // rather than masking it, matching the details page.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    onError: (err: any) => {
      const detail =
        err?.response?.data?.detail || "Failed to trigger workflow run.";
      addError({ content: <>{detail}</> });
    },
  });

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
            // Prefer the friendly name; fall back to the workflowId for
            // packages built before the packager emitted a name.
            const label = item.name || item.workflowId;
            return (
              <>
                <Link
                  href={url}
                  onFollow={(event): void => {
                    event.preventDefault();
                    navigate(url);
                  }}
                >
                  {label}
                </Link>
                {item.name && (
                  <Box variant="small" color="text-status-inactive">
                    {item.workflowId}
                  </Box>
                )}
              </>
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
        {
          id: "actions",
          header: "Actions",
          cell: (item: WorkflowRegistration): React.ReactNode => {
            // Only registered (runnable) workflows get a Run action; invalid
            // ones can never be run (mirrors the details page gate).
            if (!canTrigger(item)) {
              return "-";
            }
            const isRunning =
              triggerMutation.isLoading &&
              triggerMutation.variables === item.registrationId;
            return (
              <Button
                variant="normal"
                loading={isRunning}
                disabled={triggerMutation.isLoading && !isRunning}
                onClick={(): void =>
                  triggerMutation.mutate(item.registrationId)
                }
              >
                Run
              </Button>
            );
          },
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
