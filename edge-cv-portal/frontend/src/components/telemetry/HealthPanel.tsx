/**
 * The Health panel (workflow-telemetry-dashboards design §9.1; tasks.md 5.3).
 *
 * Per Device: `flow`, the roll-up's authoritative `gaps` of the last hour,
 * the device-side `telemetry.*` counters (`queueDrops`, `publishFailures`,
 * `eventsDropped`) summed over the last hour, failed and dropped runs, the
 * model state with `model_waits` / `model_wait_ms`, the Greengrass core-device
 * status, the `x86_64_nvidia` variant hint when the answer carries it and,
 * for `telemetry:manage` holders only, the use case's rejection counts of
 * the last hour. Polls every `pollSeconds` while visible unless the page
 * hands it the answer; "telemetry is off" when the settings say so.
 */
import {
  Alert,
  Box,
  Container,
  Header,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import type { UserRole } from '../../types';
import type { DashboardNowResponse, TelemetryNowDevice } from '../../pages/telemetry/types';
import { useDashboardNow, type NowFetcher } from '../../hooks/useDashboardNow';
import { canManageTelemetry } from '../../utils/telemetryPermissions';
import { formatAge, formatCount, formatDuration } from './format';
import { TELEMETRY_OFF, engineSummary, flowStatus, num } from './nowData';

export interface HealthPanelProps {
  dashboardId: string;
  pollSeconds?: number;
  /** The signed-in role: the rejection block shows for `telemetry:manage` holders only. */
  role: UserRole | undefined | null;
  /** When given, the panel renders this answer and does not poll. */
  now?: DashboardNowResponse | null;
  loading?: boolean;
  error?: string | null;
  fetchNow?: NowFetcher;
}

function coreDeviceStatus(device: TelemetryNowDevice): { type: 'success' | 'error' | 'warning' | 'stopped'; label: string } {
  const status = device.coreDevice?.status ?? null;
  if (status === 'HEALTHY') return { type: 'success', label: 'HEALTHY' };
  if (status === 'UNHEALTHY') return { type: 'error', label: 'UNHEALTHY' };
  if (status === null) return { type: 'stopped', label: 'unknown' };
  return { type: 'warning', label: device.coreDevice?.error ? `${status} (${device.coreDevice.error})` : status };
}

function DeviceHealth({ deviceId, device }: { deviceId: string; device: TelemetryNowDevice }) {
  const flow = flowStatus(device.flow);
  const hour = device.lastHour?.measures ?? {};
  const summary = engineSummary(device);
  const core = coreDeviceStatus(device);
  const waits = num(hour.model_waits) ?? 0;
  const waitMs = num(hour.model_wait_ms) ?? 0;
  return (
    <Container
      data-testid={`telemetry-health-${deviceId}`}
      header={
        <Header
          variant="h3"
          actions={
            <StatusIndicator type={flow.type}>
              <span data-testid={`telemetry-health-${deviceId}-flow`}>{flow.label}</span>
            </StatusIndicator>
          }
        >
          {deviceId}
        </Header>
      }
    >
      <SpaceBetween size="s">
        {device.variantHint && (
          <Alert type="warning" data-testid={`telemetry-health-${deviceId}-variant-hint`}>
            {device.variantHint}
          </Alert>
        )}
        <KeyValuePairs
          columns={3}
          items={[
            {
              label: 'Sequence gaps (last hour)',
              value: <span data-testid={`telemetry-health-${deviceId}-gaps`}>{formatCount(device.lastHour?.gaps ?? 0)}</span>,
            },
            {
              label: 'Queue drops · publish failures · events dropped (last hour)',
              value: (
                <span data-testid={`telemetry-health-${deviceId}-counters`}>
                  {`${formatCount(num(hour.telemetry_queue_drops) ?? 0)} · ${formatCount(num(hour.telemetry_publish_failures) ?? 0)} · ${formatCount(num(hour.telemetry_events_dropped) ?? 0)}`}
                </span>
              ),
            },
            {
              label: 'Failed · dropped runs (last hour)',
              value: (
                <span data-testid={`telemetry-health-${deviceId}-runs`}>
                  {`${formatCount(num(hour.runs_failed) ?? 0)} · ${formatCount(num(hour.runs_dropped) ?? 0)}`}
                </span>
              ),
            },
            {
              label: 'Model state',
              value: <span data-testid={`telemetry-health-${deviceId}-model`}>{summary?.model?.state ?? '—'}</span>,
            },
            {
              label: 'Model readiness waits (last hour)',
              value: (
                <span data-testid={`telemetry-health-${deviceId}-model-waits`}>
                  {`${formatCount(waits)} wait(s) · ${formatDuration(waitMs / 1000)}`}
                </span>
              ),
            },
            {
              label: 'Core device',
              value: (
                <StatusIndicator type={core.type}>
                  <span data-testid={`telemetry-health-${deviceId}-core`}>{core.label}</span>
                </StatusIndicator>
              ),
            },
            {
              label: 'Target architecture',
              value: <span>{device.targetArchitecture ?? '—'}</span>,
            },
            {
              label: 'Minutes with data (last hour)',
              value: <span>{formatCount(device.lastHour?.minutes ?? 0)}</span>,
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
}

export default function HealthPanel({
  dashboardId,
  pollSeconds,
  role,
  now: controlled,
  loading: controlledLoading,
  error: controlledError,
  fetchNow,
}: HealthPanelProps) {
  const polled = useDashboardNow({ dashboardId: controlled === undefined ? dashboardId : null, pollSeconds, fetchNow });
  const now = controlled === undefined ? polled.now : controlled;
  const loading = controlled === undefined ? polled.loading : controlledLoading ?? false;
  const error = controlled === undefined ? polled.error : controlledError ?? null;
  const devices = Object.entries(now?.devices ?? {});
  const rejections = canManageTelemetry(role) && now?.rejections ? now.rejections : null;
  const rejectionEntries = rejections ? Object.entries(rejections.counts ?? {}).sort((a, b) => b[1] - a[1]) : [];

  return (
    <Container
      data-testid="telemetry-health-panel"
      header={
        <Header
          variant="h2"
          description={now ? `updated ${formatAge(now.updatedAt, Date.now())} · polling every ${now.pollSeconds ?? pollSeconds ?? 5} s while visible` : 'Reading the health…'}
        >
          Health
        </Header>
      }
    >
      <SpaceBetween size="s">
        {now && !now.enabled && (
          <Alert type="warning" data-testid="telemetry-health-off">
            {TELEMETRY_OFF}
          </Alert>
        )}
        {error && (
          <Alert type="error" data-testid="telemetry-health-error">
            {error}
          </Alert>
        )}
        {!now && loading && <StatusIndicator type="loading">Reading…</StatusIndicator>}
        {devices.map(([deviceId, device]) => (
          <DeviceHealth key={deviceId} deviceId={deviceId} device={device} />
        ))}
        {rejections && (
          <Container
            data-testid="telemetry-health-rejections"
            header={
              <Header variant="h3" description="Messages the ingest refused for this use case in the last hour, by reason">
                Rejections
              </Header>
            }
          >
            {rejectionEntries.length === 0 ? (
              <Box color="text-body-secondary">none in the last hour</Box>
            ) : (
              <KeyValuePairs
                columns={4}
                items={rejectionEntries.map(([reason, count]) => ({ label: reason, value: formatCount(count) }))}
              />
            )}
            {rejections.note && (
              <Box variant="small" color="text-body-secondary" padding={{ top: 'xs' }}>
                {rejections.note}
              </Box>
            )}
          </Container>
        )}
      </SpaceBetween>
    </Container>
  );
}
