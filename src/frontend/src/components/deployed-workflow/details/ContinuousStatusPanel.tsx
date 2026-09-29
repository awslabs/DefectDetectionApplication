/*
 *  Copyright 2025 Amazon Web Services, Inc.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */
/**
 * The continuous processing status of a deployed workflow whose stream node
 * runs in `continuous` mode (rtsp-rtmp-stream-cameras Requirement 16.2): its
 * state, configured and effective run rates, the counters of Requirement
 * 12.5, its camera's Stream_Health, and the pause and resume controls of
 * Requirement 11.6.
 */
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  StatusIndicatorProps,
} from "@cloudscape-design/components";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import format from "date-fns/format";
import {
  ContinuousCounters,
  ContinuousState,
  ContinuousStatus,
  pauseContinuousWorkflow,
  resumeContinuousWorkflow,
} from "api/WorkflowRegistrationAPI";
import { ValueWithLabel } from "Common";
import { DATE_WITHOUT_TZ } from "components/date-time-format";
import { apiErrorMessage } from "components/image-source/stream/streamForm";
import {
  StreamStateIndicator,
  isNotStreaming,
  notStreamingDetail,
} from "components/image-source/stream/streamHealth";

/** The react-query key of a registration's Continuous status. */
export function continuousStatusQueryKey(registrationId: string): unknown[] {
  return ["getContinuousStatus", registrationId];
}

const STATE_INDICATORS: Record<ContinuousState, [StatusIndicatorProps.Type, string]> = {
  running: ["success", "Running"],
  paused: ["stopped", "Paused"],
  waiting_for_stream: ["pending", "Waiting for the stream"],
};

export function ContinuousStateIndicator({
  state,
}: {
  state: ContinuousState;
}): JSX.Element {
  const [type, label] = STATE_INDICATORS[state] ?? ["info", String(state)];
  return <StatusIndicator type={type}>{label}</StatusIndicator>;
}

/** The counters in report order, with their labels (Requirement 12.5). */
export const COUNTER_LABELS: ReadonlyArray<[keyof ContinuousCounters, string]> = [
  ["started", "Runs started"],
  ["completed", "Runs completed"],
  ["failed", "Runs failed"],
  ["skippedBusy", "Ticks skipped: run in progress"],
  ["skippedNoNewFrame", "Ticks skipped: no new frame"],
  ["notable", "Notable runs"],
  ["outputsSent", "Outputs sent"],
  ["streamUnavailable", "Stream outages"],
];

/** "2 fps", "0.5 fps", "1.83 fps". */
export function formatRate(fps: number | null | undefined): string {
  if (typeof fps !== "number" || !Number.isFinite(fps)) {
    return "-";
  }
  return `${Number(fps.toFixed(2))} fps`;
}

interface ContinuousStatusPanelProps {
  registrationId: string;
  status: ContinuousStatus;
}

export default function ContinuousStatusPanel({
  registrationId,
  status,
}: ContinuousStatusPanelProps): JSX.Element {
  const queryClient = useQueryClient();
  const paused = status.state === "paused";
  const control = useMutation({
    mutationFn: () =>
      paused
        ? resumeContinuousWorkflow(registrationId)
        : pauseContinuousWorkflow(registrationId),
    onSuccess: (next) => {
      queryClient.setQueryData(continuousStatusQueryKey(registrationId), next);
    },
  });
  const health = status.streamHealth;
  const streamDetail = isNotStreaming(health) ? notStreamingDetail(health) : "";
  return (
    <Container
      data-testid="continuous-status-panel"
      header={
        <Header
          variant="h2"
          description="This workflow runs on the newest frame of its stream camera at the configured rate."
          actions={
            <Button
              loading={control.isLoading}
              onClick={(): void => control.mutate()}
            >
              {paused ? "Resume" : "Pause"}
            </Button>
          }
        >
          Continuous processing
        </Header>
      }
    >
      <SpaceBetween size="l">
        {control.isError && (
          <Alert
            type="error"
            header={paused ? "The workflow could not be resumed" : "The workflow could not be paused"}
            dismissible
            onDismiss={(): void => control.reset()}
          >
            {apiErrorMessage(control.error)}
          </Alert>
        )}
        {paused && (
          <Alert type="info">
            The workflow stays paused, across restarts too, until you resume it. While it is
            paused you can run it manually.
          </Alert>
        )}
        <ColumnLayout columns={4} variant="text-grid">
          <ValueWithLabel label="State">
            <ContinuousStateIndicator state={status.state} />
            {paused && !!status.pausedAtMs && (
              <Box color="text-body-secondary">
                Since {format(status.pausedAtMs, DATE_WITHOUT_TZ)}
              </Box>
            )}
          </ValueWithLabel>
          <ValueWithLabel label="Configured rate">{formatRate(status.configuredFps)}</ValueWithLabel>
          <ValueWithLabel label="Effective rate (last 60 s)">
            {formatRate(status.effectiveFps)}
          </ValueWithLabel>
          <ValueWithLabel label="Run in progress">{status.runInProgress ? "Yes" : "No"}</ValueWithLabel>
          <ValueWithLabel label="Camera">
            <div>{status.cameraSourceId || "-"}</div>
            <StreamStateIndicator state={health?.state} />
            {streamDetail && <Box color="text-body-secondary">{streamDetail}</Box>}
          </ValueWithLabel>
        </ColumnLayout>
        <ColumnLayout columns={4} variant="text-grid">
          {COUNTER_LABELS.map(([key, label]) => (
            <ValueWithLabel key={key} label={label}>
              {status.counters?.[key] ?? 0}
            </ValueWithLabel>
          ))}
        </ColumnLayout>
      </SpaceBetween>
    </Container>
  );
}
