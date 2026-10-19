/**
 * The debug sessions table shared by the sessions page
 * (`pages/debug/DebugSessions.tsx`) and the Device detail "Debug" tab
 * (workflow-debug-mode design §8, tasks.md 5.2 and 5.4).
 *
 * One row per session: workflow, devices (each with its shadow write result,
 * the device's `reported` state and counters, and a link to its log), state
 * with the remaining time, `enabledAt`, `expiresAt`, actor, options and
 * counters, plus a "Stop" action for the roles holding `MANAGE_DEVICES`.
 *
 * The variant notice (design §5.6, `workflow_debug/views.variant_notice`)
 * fires when a device's record says `x86_64_nvidia` and its reported
 * `publishFailures` are above zero: that LocalServer variant lacks the IPC
 * statement debug publishing needs, so its stream stays empty while the
 * session is active on the device.
 */
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Badge, Box, Button, Link, SpaceBetween, Table } from '@cloudscape-design/components';
import SessionBadge, { isLiveState } from './SessionBadge';
import { formatBytes, formatTime } from './format';
import type { DebugSession, DebugSessionDevice } from '../../pages/debug/types';

/** The Devices-table value of the unsupported `amd64Nvidia` variant. */
export const UNSUPPORTED_TARGET_ARCHITECTURE = 'x86_64_nvidia';

/** Byte-for-byte the text of `workflow_debug/views.VARIANT_NOTICE`. */
export const VARIANT_NOTICE =
  'This device is recorded as x86_64_nvidia, the LocalServer variant whose recipe ' +
  '(recipe-amd64-nvidia.yaml) lacks the mqttproxy:2 statement debug publishing needs: ' +
  'the session is active on the device but every record publish is denied by Greengrass ' +
  'IPC, which is why the stream stays empty while publishFailures climb. Debug mode is ' +
  'unsupported on this variant until its recipe gains mqttproxy:2.';

/** The notice text, or null (another architecture, no record, no failures). */
export function variantNotice(
  targetArchitecture: string | null | undefined,
  publishFailures: unknown
): string | null {
  if (targetArchitecture !== UNSUPPORTED_TARGET_ARCHITECTURE) return null;
  const failures = Number(publishFailures ?? 0);
  if (!Number.isFinite(failures) || failures <= 0) return null;
  return VARIANT_NOTICE;
}

/** Devices of the sessions whose record and counters trigger the notice. */
export function devicesWithVariantNotice(
  sessions: readonly DebugSession[],
  architectures: Record<string, string | null | undefined>
): string[] {
  const names = new Set<string>();
  for (const session of sessions) {
    for (const device of session.devices ?? []) {
      const failures = device.reported?.counters?.publishFailures;
      if (variantNotice(architectures[device.device_id], failures)) {
        names.add(device.device_id);
      }
    }
  }
  return [...names].sort();
}

function optionsSummary(session: DebugSession): string {
  const options = session.options ?? {};
  const parts = [
    `level ${options.level ?? '—'}`,
    options.images ? `images ${options.imageMaxKb ?? ''} KB`.trim() : 'no images',
  ];
  if (typeof options.maxMessagesPerSecond === 'number') parts.push(`${options.maxMessagesPerSecond} msg/s`);
  if (typeof options.maxBytesPerSecond === 'number') parts.push(`${formatBytes(options.maxBytesPerSecond)}/s`);
  return parts.join(' · ');
}

function reportedSummary(device: DebugSessionDevice): string {
  const reported = device.reported;
  if (!reported) return 'not reported';
  const counters = reported.counters ?? {};
  const bits = [`reported ${reported.state ?? '—'}`];
  if (reported.reason) bits.push(reported.reason);
  if (typeof counters.records === 'number') bits.push(`${counters.records} records`);
  if (typeof counters.publishFailures === 'number' && counters.publishFailures > 0) {
    bits.push(`${counters.publishFailures} publish failures`);
  }
  return bits.join(', ');
}

export interface DebugSessionsTableProps {
  sessions: DebugSession[];
  loading: boolean;
  usecaseId: string;
  /** Shows the "Stop" action (the server decides). */
  canManage: boolean;
  onStop: (session: DebugSession) => void;
  stoppingId?: string | null;
  /** workflow_id → name, for the Workflow column. */
  workflowNames?: Record<string, string>;
  /** device_id → recorded target architecture, for the variant notice. */
  deviceArchitectures?: Record<string, string | null | undefined>;
  /** Injected clock for the remaining-time column. */
  now?: number;
  header?: ReactNode;
  empty?: ReactNode;
}

export default function DebugSessionsTable({
  sessions,
  loading,
  usecaseId,
  canManage,
  onStop,
  stoppingId,
  workflowNames = {},
  deviceArchitectures = {},
  now,
  header,
  empty,
}: DebugSessionsTableProps) {
  const navigate = useNavigate();
  const noticeDevices = devicesWithVariantNotice(sessions, deviceArchitectures);

  const logHref = (session: DebugSession, deviceId: string) =>
    `/debug/${encodeURIComponent(usecaseId)}/${encodeURIComponent(session.workflow_id)}/${encodeURIComponent(deviceId)}`;

  return (
    <SpaceBetween size="s">
      {noticeDevices.length > 0 && (
        <Alert type="warning" header={`Unsupported variant: ${noticeDevices.join(', ')}`} data-testid="debug-variant-notice">
          {VARIANT_NOTICE}
        </Alert>
      )}
      <Table
        variant="embedded"
        header={header}
        loading={loading}
        loadingText="Loading debug sessions"
        items={sessions}
        trackBy="session_id"
        wrapLines
        data-testid="debug-sessions-table"
        columnDefinitions={[
          {
            id: 'workflow',
            header: 'Workflow',
            cell: (item) => (
              <SpaceBetween size="xxs">
                <Box variant="strong">{workflowNames[item.workflow_id] ?? item.workflow_id}</Box>
                <Box variant="small" color="text-body-secondary">
                  {item.workflow_id}
                  {item.workflow_version ? ` v${item.workflow_version}` : ''}
                </Box>
              </SpaceBetween>
            ),
          },
          {
            id: 'devices',
            header: 'Devices',
            cell: (item) => (
              <SpaceBetween size="xxs">
                {item.all_devices && (
                  <Badge color="blue">all devices ({item.devices?.length ?? item.device_ids.length})</Badge>
                )}
                {(item.devices ?? []).map((device) => (
                  <Box key={device.device_id} data-testid={`debug-session-device-${device.device_id}`}>
                    <Link
                      href={logHref(item, device.device_id)}
                      onFollow={(e) => {
                        e.preventDefault();
                        navigate(logHref(item, device.device_id));
                      }}
                    >
                      {device.device_id}
                    </Link>
                    <Box variant="small" color="text-body-secondary">
                      shadow {device.shadow?.result ?? 'pending'}
                      {device.shadow?.error ? ` (${device.shadow.error})` : ''} · {reportedSummary(device)}
                    </Box>
                  </Box>
                ))}
              </SpaceBetween>
            ),
          },
          {
            id: 'state',
            header: 'State',
            cell: (item) => <SessionBadge session={item} now={now} />,
          },
          {
            id: 'enabled',
            header: 'Enabled at',
            cell: (item) => formatTime(item.enabledAt),
          },
          {
            id: 'expires',
            header: isLiveState(sessions[0]?.state) ? 'Expires at' : 'Expires / ended at',
            cell: (item) => (item.stoppedAt ? `${formatTime(item.stoppedAt)} (stopped)` : formatTime(item.expiresAt)),
          },
          {
            id: 'actor',
            header: 'Started by',
            cell: (item) => item.actor ?? '—',
          },
          {
            id: 'options',
            header: 'Options',
            cell: (item) => optionsSummary(item),
          },
          {
            id: 'counters',
            header: 'Received',
            cell: (item) =>
              `${item.counters?.records ?? 0} records · ${formatBytes(item.counters?.bytes ?? 0)}`,
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) =>
              canManage && isLiveState(item.state) ? (
                <Button
                  onClick={() => onStop(item)}
                  loading={stoppingId === item.session_id}
                  disabled={!!stoppingId && stoppingId !== item.session_id}
                  data-testid={`debug-stop-${item.session_id}`}
                >
                  Stop
                </Button>
              ) : (
                '—'
              ),
          },
        ]}
        empty={
          empty ?? (
            <Box textAlign="center" color="inherit">
              <b>No debug sessions</b>
              <Box variant="p" color="inherit">
                Start a session to send every run of a workflow to the debug log.
              </Box>
            </Box>
          )
        }
      />
    </SpaceBetween>
  );
}
