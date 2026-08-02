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
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  ContentLayout,
  Header,
  Link,
  SpaceBetween,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import {
  WorkflowExecution,
  getWorkflowRegistration,
  triggerWorkflowRegistration,
} from "api/WorkflowRegistrationAPI";
import {
  canTrigger,
  canViewResults,
  executionFailureDetails,
  executionStatusIndicator,
  hasStarted,
  registrationStatusIndicator,
  shouldPoll,
  sortExecutions,
} from "../presentation";
import { DATE_TZ_OFFSET, DATE_WITHOUT_TZ } from "components/date-time-format";
import EmptyTable from "components/empty-table/EmptyTable";
import { AppLayoutContext } from "components/layout/AppLayoutContext";

export const EXECUTION_POLL_INTERVAL_MS = 2000;

function formatEpochSeconds(epochSeconds: number | null): string {
  return epochSeconds ? format(epochSeconds * 1000, DATE_WITHOUT_TZ) : "-";
}

export default function DeployedWorkflowDetails(): JSX.Element {
  const { registrationId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { addError } = React.useContext(AppLayoutContext);

  const detailQuery = useQuery({
    queryKey: ["getWorkflowRegistration", registrationId],
    queryFn: () => getWorkflowRegistration(registrationId),
    // Poll while any execution is active; stop once all are terminal.
    refetchInterval: (data) =>
      data && shouldPoll(data.executions) ? EXECUTION_POLL_INTERVAL_MS : false,
  });

  const triggerMutation = useMutation({
    mutationFn: () => triggerWorkflowRegistration(registrationId),
    onSuccess: () => {
      // Refresh the details so the new pending execution appears immediately
      // and polling starts.
      queryClient.invalidateQueries({
        queryKey: ["getWorkflowRegistration", registrationId],
      });
    },
    // Surface the backend rejection (e.g. the 409 for invalid registrations)
    // rather than masking it.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    onError: (err: any) => {
      const detail =
        err?.response?.data?.detail || "Failed to trigger workflow run.";
      addError({ content: <>{detail}</> });
    },
  });

  const registration = detailQuery.data;
  const timezoneLabel = format(new Date(), DATE_TZ_OFFSET);
  const executions = sortExecutions(registration?.executions ?? []);

  if (!registration) {
    return (
      <ContentLayout
        header={<Header variant="h1">Deployed workflow details</Header>}
      >
        <Container>
          <StatusIndicator type={detailQuery.isError ? "error" : "loading"}>
            {detailQuery.isError
              ? "Failed to load deployed workflow"
              : "Loading deployed workflow"}
          </StatusIndicator>
        </Container>
      </ContentLayout>
    );
  }

  const statusIndicator = registrationStatusIndicator(registration);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          actions={
            canTrigger(registration) && (
              <Button
                variant="primary"
                loading={triggerMutation.isLoading}
                onClick={(): void => triggerMutation.mutate()}
              >
                Run workflow
              </Button>
            )
          }
        >
          {registration.workflowId}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {registration.status !== "registered" && (
          <Alert type="error" header="Invalid registration">
            This workflow registration is invalid and cannot be run.
            {registration.invalidReason && <> {registration.invalidReason}</>}
          </Alert>
        )}

        <Container header={<Header variant="h2">Registration details</Header>}>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Workflow</Box>
              <div>{registration.workflowId}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Version</Box>
              <div>{registration.version || "-"}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Architecture</Box>
              <div>{registration.arch || "-"}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Status</Box>
              <StatusIndicator type={statusIndicator.type}>
                {statusIndicator.text}
              </StatusIndicator>
            </div>
            <div>
              <Box variant="awsui-key-label">Registered {timezoneLabel}</Box>
              <div>{formatEpochSeconds(registration.registeredAt)}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">Registration ID</Box>
              <div>{registration.registrationId}</div>
            </div>
          </ColumnLayout>
        </Container>

        <Table
          wrapLines
          header={
            <Header variant="h2" counter={`(${executions.length})`}>
              Executions
            </Header>
          }
          columnDefinitions={[
            {
              id: "executionId",
              header: "Execution ID",
              cell: (item: WorkflowExecution) => item.executionId,
            },
            {
              id: "status",
              header: "Status",
              cell: (item: WorkflowExecution): React.ReactNode => {
                const indicator = executionStatusIndicator(item);
                const failure = executionFailureDetails(item);
                return (
                  <>
                    <StatusIndicator type={indicator.type}>
                      {indicator.text}
                    </StatusIndicator>
                    {failure && (
                      <Box variant="p" color="text-status-error">
                        {failure.failingNodeId && (
                          <>
                            Failing node: {failure.failingNodeId}
                            <br />
                          </>
                        )}
                        {failure.error}
                      </Box>
                    )}
                  </>
                );
              },
            },
            {
              id: "startedAt",
              header: `Started ${timezoneLabel}`,
              cell: (item: WorkflowExecution) =>
                formatEpochSeconds(item.startedAt),
            },
            {
              id: "finishedAt",
              header: `Finished ${timezoneLabel}`,
              cell: (item: WorkflowExecution) =>
                formatEpochSeconds(item.finishedAt),
            },
            {
              id: "actions",
              header: "Actions",
              cell: (item: WorkflowExecution): React.ReactNode => {
                const runBase = `/deployed-workflows/${registrationId}/executions/${item.executionId}`;
                const links: JSX.Element[] = [];
                // "View results" only for finished runs with viewable images.
                if (canViewResults(item)) {
                  const url = `${runBase}/results`;
                  links.push(
                    <Link
                      key="results"
                      href={url}
                      onFollow={(event): void => {
                        event.preventDefault();
                        navigate(url);
                      }}
                    >
                      View results
                    </Link>,
                  );
                }
                // Log and status links for any started (running/terminal) run.
                if (hasStarted(item)) {
                  const logUrl = `${runBase}/log`;
                  const graphUrl = `${runBase}/graph`;
                  links.push(
                    <Link
                      key="log"
                      href={logUrl}
                      onFollow={(event): void => {
                        event.preventDefault();
                        navigate(logUrl);
                      }}
                    >
                      View run log
                    </Link>,
                    <Link
                      key="graph"
                      href={graphUrl}
                      onFollow={(event): void => {
                        event.preventDefault();
                        navigate(graphUrl);
                      }}
                    >
                      Run status
                    </Link>,
                  );
                }
                if (links.length === 0) {
                  return "-";
                }
                return <SpaceBetween size="xs">{links}</SpaceBetween>;
              },
            },
          ]}
          items={executions}
          empty={
            <EmptyTable
              header="No executions"
              message="This workflow has not been run yet."
            />
          }
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
