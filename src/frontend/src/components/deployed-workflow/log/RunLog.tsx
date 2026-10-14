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
import {
  Alert,
  Box,
  Button,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { getWorkflowExecutionLog } from "api/WorkflowRegistrationAPI";

/**
 * Run log viewer (deployed-workflow-run-observability, Requirement 6).
 *
 * Fetches the per-execution Run_Log text (`/workflows/executions/{id}/log`)
 * and renders it scrollable + copyable (R6.5). The link is only surfaced for
 * started executions by DeployedWorkflowDetails, so this screen just renders
 * whatever log the backend has: an empty/pending log becomes an explanatory
 * empty state (R6.4) rather than an error, and a failed run's error/failing
 * node is evident in the log text itself (R6.3).
 */
export default function RunLog(): JSX.Element {
  const { executionId = "" } = useParams();

  const logQuery = useQuery({
    queryKey: ["getWorkflowExecutionLog", executionId],
    queryFn: () => getWorkflowExecutionLog(executionId),
  });

  const log = logQuery.data ?? "";
  const hasLog = log.trim().length > 0;

  const [copied, setCopied] = React.useState(false);

  const handleCopy = React.useCallback(async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(log);
      setCopied(true);
      // Reset the "Copied" affordance after a short delay.
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied; leave the button in its default state.
      setCopied(false);
    }
  }, [log]);

  return (
    <ContentLayout
      header={<Header variant="h1">Run log</Header>}
    >
      <Container
        header={
          <Header
            variant="h2"
            description={`Execution ${executionId}`}
            actions={
              hasLog && (
                <Button iconName="copy" onClick={(): void => void handleCopy()}>
                  {copied ? "Copied" : "Copy"}
                </Button>
              )
            }
          >
            Log
          </Header>
        }
      >
        <RunLogBody
          isLoading={logQuery.isLoading}
          isError={logQuery.isError}
          hasLog={hasLog}
          log={log}
        />
      </Container>
    </ContentLayout>
  );
}

interface RunLogBodyProps {
  isLoading: boolean;
  isError: boolean;
  hasLog: boolean;
  log: string;
}

function RunLogBody({
  isLoading,
  isError,
  hasLog,
  log,
}: RunLogBodyProps): JSX.Element {
  if (isLoading) {
    return <StatusIndicator type="loading">Loading run log</StatusIndicator>;
  }

  if (isError) {
    return (
      <Alert type="error" header="Failed to load run log">
        The run log could not be retrieved. Try again in a moment.
      </Alert>
    );
  }

  if (!hasLog) {
    // Empty/pending log is an expected state, not an error (R6.4).
    return (
      <Box textAlign="center" color="inherit" padding={{ vertical: "l" }}>
        <SpaceBetween size="xs">
          <Box variant="strong" color="inherit">
            No log available yet
          </Box>
          <Box variant="p" color="inherit">
            This run has not produced any log output yet. Logs appear here once
            the run has started emitting them.
          </Box>
        </SpaceBetween>
      </Box>
    );
  }

  // Scrollable, monospace log body (R6.5). The log text carries the failure
  // error and failing node for failed runs (R6.3).
  return (
    <pre
      style={{
        margin: 0,
        maxHeight: "70vh",
        overflow: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        fontFamily: "Monaco, Menlo, Consolas, monospace",
        fontSize: "0.85rem",
      }}
    >
      {log}
    </pre>
  );
}
