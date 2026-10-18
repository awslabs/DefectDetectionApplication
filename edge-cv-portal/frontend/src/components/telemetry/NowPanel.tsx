/**
 * The Now panel (workflow-telemetry-dashboards design §9.1; tasks.md 5.3).
 *
 * Per Device, `KeyValuePairs` with the newest values of the `LAST#engine`
 * pointer (runs, detections, people, latency, model), "last summary <age>",
 * "last run <age>" or "no run in 48 h" (the two pointers: a Heartbeat never
 * blanks the values), the Heartbeat age, `flow` as a `StatusIndicator`, the
 * live-frame age, the newest Event ages and the 20 newest Events of the last
 * 15 minutes. Polls every `pollSeconds` while visible through
 * `useDashboardNow` unless the page hands it the answer; "telemetry is off"
 * when the settings say so.
 */
import {
  Alert,
  Badge,
  Box,
  Container,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import type { DashboardNowResponse, TelemetryNowDevice } from '../../pages/telemetry/types';
import { useDashboardNow, type NowFetcher } from '../../hooks/useDashboardNow';
import { formatAge, formatCount, formatDuration } from './format';
import {
  ENGINE_HEARTBEAT,
  NO_RUN_48H,
  TELEMETRY_OFF,
  engineSummary,
  eventSummary,
  flowStatus,
  num,
  perClassText,
  severityType,
} from './nowData';

export interface NowPanelProps {
  dashboardId: string;
  /** `telemetryNowPollSeconds` of `/telemetry/schemas`. */
  pollSeconds?: number;
  /** When given, the panel renders this answer and does not poll (the page polls once for both panels). */
  now?: DashboardNowResponse | null;
  loading?: boolean;
  error?: string | null;
  /** Test seam for the self-polling mode. */
  fetchNow?: NowFetcher;
}

function DeviceNow({ deviceId, device, updatedAt }: { deviceId: string; device: TelemetryNowDevice; updatedAt: number }) {
  const summary = engineSummary(device);
  const flow = flowStatus(device.flow);
  const ages = device.ages;
  const runs = summary?.runs;
  const detections = summary?.detections;
  const latency = summary?.latency;
  const model = summary?.model;
  const heartbeat = device.last?.[ENGINE_HEARTBEAT];
  const newestEvents = device.events.slice(0, 3);
  return (
    <Container
      data-testid={`telemetry-now-${deviceId}`}
      header={
        <Header
          variant="h3"
          actions={
            <StatusIndicator type={flow.type}>
              <span data-testid={`telemetry-now-${deviceId}-flow`}>{flow.label}</span>
            </StatusIndicator>
          }
        >
          {deviceId}
        </Header>
      }
    >
      <SpaceBetween size="s">
        <KeyValuePairs
          columns={3}
          items={[
            {
              label: 'Last summary',
              value: <span data-testid={`telemetry-now-${deviceId}-last-summary`}>{ages.lastSummaryAgeS === null ? '—' : `${formatDuration(ages.lastSummaryAgeS)} ago`}</span>,
            },
            {
              label: 'Last run',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-last-run`}>
                  {ages.noRunIn48h || ages.lastRunAgeS === null ? NO_RUN_48H : `${formatDuration(ages.lastRunAgeS)} ago`}
                </span>
              ),
            },
            {
              label: 'Heartbeat',
              value: <span data-testid={`telemetry-now-${deviceId}-heartbeat`}>{heartbeat && ages.heartbeatAgeS !== null ? `${formatDuration(ages.heartbeatAgeS)} ago` : '—'}</span>,
            },
            {
              label: 'Runs (last interval)',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-runs`}>
                  {runs ? `${formatCount(num(runs.completed) ?? 0)} completed · ${formatCount(num(runs.failed) ?? 0)} failed · ${formatCount(num(runs.dropped) ?? 0)} dropped` : '—'}
                </span>
              ),
            },
            {
              label: 'Detections',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-detections`}>
                  {detections
                    ? `${formatCount(num(detections.total) ?? 0)}${detections.truncated ? ' (truncated)' : ''} · ${perClassText(detections.perClass)}`
                    : '—'}
                </span>
              ),
            },
            {
              label: 'People seen (max per run)',
              value: <span data-testid={`telemetry-now-${deviceId}-people`}>{formatCount(num(summary?.people?.seen))}</span>,
            },
            {
              label: 'Latency p50 / p95',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-latency`}>
                  {latency ? `${formatCount(num(latency.p50Ms))} ms / ${formatCount(num(latency.p95Ms))} ms` : '—'}
                </span>
              ),
            },
            {
              label: 'Model',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-model`}>
                  {model?.state ? `${model.state}${num(model.waits) ? ` · ${model.waits} wait(s)` : ''}` : '—'}
                </span>
              ),
            },
            {
              label: 'Live frame',
              value: <span data-testid={`telemetry-now-${deviceId}-live`}>{device.live ? `${formatDuration(device.live.ageS)} ago` : 'none'}</span>,
            },
            {
              label: 'Newest events',
              value: (
                <span data-testid={`telemetry-now-${deviceId}-event-ages`}>
                  {newestEvents.length === 0
                    ? 'none in 15 min'
                    : newestEvents.map((e) => `${e.type} ${formatAge(e.eventMs, updatedAt)}`).join(' · ')}
                </span>
              ),
            },
          ]}
        />
        {device.events.length > 0 && (
          <Box data-testid={`telemetry-now-${deviceId}-events`}>
            <Box variant="awsui-key-label">Events (last 15 min, newest first)</Box>
            <SpaceBetween size="xxs">
              {device.events.map((event) => (
                <Box key={event.eventId} fontSize="body-s">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Badge color={severityType(event.severity) === 'error' ? 'red' : severityType(event.severity) === 'warning' ? 'blue' : 'grey'}>
                      {event.type}
                    </Badge>
                    <span>{formatAge(event.eventMs, updatedAt)}</span>
                    <span>{eventSummary(event.type, event.data)}</span>
                  </SpaceBetween>
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default function NowPanel({ dashboardId, pollSeconds, now: controlled, loading: controlledLoading, error: controlledError, fetchNow }: NowPanelProps) {
  const polled = useDashboardNow({ dashboardId: controlled === undefined ? dashboardId : null, pollSeconds, fetchNow });
  const now = controlled === undefined ? polled.now : controlled;
  const loading = controlled === undefined ? polled.loading : controlledLoading ?? false;
  const error = controlled === undefined ? polled.error : controlledError ?? null;
  const devices = Object.entries(now?.devices ?? {});

  return (
    <Container
      data-testid="telemetry-now-panel"
      header={
        <Header
          variant="h2"
          description={now ? `updated ${formatAge(now.updatedAt, Date.now())} · polling every ${now.pollSeconds ?? pollSeconds ?? 5} s while visible` : 'Reading the newest values…'}
        >
          Now
        </Header>
      }
    >
      <SpaceBetween size="s">
        {now && !now.enabled && (
          <Alert type="warning" data-testid="telemetry-now-off">
            {TELEMETRY_OFF}
          </Alert>
        )}
        {error && (
          <Alert type="error" data-testid="telemetry-now-error">
            {error}
          </Alert>
        )}
        {!now && loading && (
          <StatusIndicator type="loading">Reading…</StatusIndicator>
        )}
        {now && devices.length === 0 && (
          <Box color="text-body-secondary">No devices on this dashboard</Box>
        )}
        {devices.map(([deviceId, device]) => (
          <DeviceNow key={deviceId} deviceId={deviceId} device={device} updatedAt={now!.updatedAt} />
        ))}
      </SpaceBetween>
    </Container>
  );
}
