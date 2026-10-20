/**
 * The telemetry settings page, `/telemetry-settings/:usecaseId`
 * (workflow-telemetry-dashboards design §10.1, §10.2, §11.3; tasks.md 5.5),
 * for the roles holding `telemetry:manage`.
 *
 * `enabled`, `summaryIntervalS` (5-60) and `idleHeartbeatS` (30-300); the
 * read-only `media_location` with its `region` and `region_source`; the
 * readiness checklist with each item's status, message and remedy (the
 * Device_Record_Remedy in full for `device_records`), the cross-region and the
 * enforcement warnings; after a save the per-device shadow results and the
 * `unrecorded_devices`, or the 202 notice when the maintenance function
 * finishes the shadow writes.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
  Toggle,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import { formatTime } from '../../components/telemetry/format';
import type {
  TelemetryChecklistItem,
  TelemetrySettings as Settings,
  TelemetrySettingsResponse,
  TelemetrySettingsUpdateResponse,
} from './types';

export const SUMMARY_RANGE: [number, number] = [5, 60];
export const HEARTBEAT_RANGE: [number, number] = [30, 300];
export const SUMMARY_MESSAGE = `A whole number of seconds between ${SUMMARY_RANGE[0]} and ${SUMMARY_RANGE[1]}`;
export const HEARTBEAT_MESSAGE = `A whole number of seconds between ${HEARTBEAT_RANGE[0]} and ${HEARTBEAT_RANGE[1]}`;
export const PENDING_NOTICE = 'The device shadows are written by the maintenance function within 5 minutes';

/** The checklist items' labels in the order of design §10.1. */
export const CHECKLIST_LABELS: Record<string, string> = {
  ingestion_resources: 'Ingestion resources',
  region: 'Region',
  role_form: 'Role form',
  device_records: 'Device records',
  telemetry_source: 'A telemetry source',
  media_location: 'Media location',
  media_retention: 'Media retention',
  registry_enforcement: 'Registry enforcement',
  device_shadows: 'Device shadows',
};

export function isIntegerInRange(text: string, min: number, max: number): boolean {
  if (!/^\d+$/.test(text.trim())) return false;
  const value = Number(text);
  return Number.isInteger(value) && value >= min && value <= max;
}

/** One line of context per item beyond ready / not ready. */
export function checklistDetail(item: TelemetryChecklistItem): string {
  const parts: string[] = [];
  switch (item.id) {
    case 'region':
      if (item.usecase_region && item.portal_region) parts.push(`use case ${item.usecase_region} · Portal ${item.portal_region}`);
      break;
    case 'device_records': {
      const unrecorded = item.unrecorded ?? [];
      if (unrecorded.length) parts.push(`without a record: ${unrecorded.join(', ')}`);
      const other = item.other_usecase ?? {};
      for (const [device, usecase] of Object.entries(other)) parts.push(`${device} is recorded for another use case (${usecase})`);
      if (item.recorded?.length) parts.push(`recorded: ${item.recorded.join(', ')}`);
      break;
    }
    case 'telemetry_source':
      if (item.lastSeenMs) parts.push(`last seen ${formatTime(item.lastSeenMs)}`);
      break;
    case 'media_location':
      if (item.media_location) {
        parts.push(`${item.media_location.bucket} (${item.media_location.source})`);
        if (item.media_location.region) parts.push(`region ${item.media_location.region} from ${item.media_location.region_source ?? 'header'}`);
      }
      break;
    case 'media_retention':
      if (item.versioning) parts.push(`versioning ${item.versioning}`);
      if (item.clipRuleDays !== undefined && item.clipRuleDays !== null) parts.push(`clip rule ${item.clipRuleDays} days`);
      if (item.reason) parts.push(String(item.reason));
      break;
    case 'registry_enforcement':
      parts.push(item.enforced ? 'enforcement on' : 'enforcement off');
      break;
    case 'device_shadows': {
      const pending = item.pending ?? [];
      if (pending.length) parts.push(`pending: ${pending.join(', ')}`);
      const failed = item.failed ?? {};
      for (const [device, reason] of Object.entries(failed)) parts.push(`${device} failed (${reason})`);
      if (item.devices !== undefined) parts.push(`${item.devices} device(s)`);
      break;
    }
    default:
      break;
  }
  if (item.error) parts.push(`error ${item.error}`);
  return parts.join(' · ');
}

export default function TelemetrySettings() {
  const { usecaseId = '' } = useParams<{ usecaseId: string }>();
  const navigate = useNavigate();
  const [answer, setAnswer] = useState<TelemetrySettingsResponse | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [summaryText, setSummaryText] = useState('10');
  const [heartbeatText, setHeartbeatText] = useState('60');
  const [attempted, setAttempted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ text: string; remedy?: string; field?: string } | null>(null);
  const [saved, setSaved] = useState<TelemetrySettingsUpdateResponse | null>(null);

  const applySettings = (settings: Settings) => {
    setEnabled(settings.enabled);
    setSummaryText(String(settings.summaryIntervalS));
    setHeartbeatText(String(settings.idleHeartbeatS));
  };

  const load = useCallback(() => {
    if (!usecaseId) return;
    setLoading(true);
    apiService
      .getTelemetrySettings(usecaseId)
      .then((result) => {
        setAnswer(result);
        applySettings(result.settings);
        setError(null);
      })
      .catch((err) => setError({ text: getErrorMessage(err, 'The settings could not be read') }))
      .finally(() => setLoading(false));
  }, [usecaseId]);

  useEffect(() => {
    load();
  }, [load]);

  const summaryValid = isIntegerInRange(summaryText, SUMMARY_RANGE[0], SUMMARY_RANGE[1]);
  const heartbeatValid = isIntegerInRange(heartbeatText, HEARTBEAT_RANGE[0], HEARTBEAT_RANGE[1]);

  const save = async () => {
    setAttempted(true);
    if (!summaryValid || !heartbeatValid) return;
    try {
      setSaving(true);
      setError(null);
      setSaved(null);
      const result = await apiService.putTelemetrySettings(usecaseId, {
        enabled,
        summaryIntervalS: Number(summaryText),
        idleHeartbeatS: Number(heartbeatText),
      });
      setSaved(result);
      setAnswer({ settings: result.settings, checklist: result.checklist });
      applySettings(result.settings);
    } catch (err) {
      if (isTelemetryApiError(err)) {
        setError({ text: `${err.message}${err.errorCode ? ` (${err.errorCode})` : ''}`, remedy: err.remedy, field: err.field });
      } else {
        setError({ text: getErrorMessage(err, 'The settings could not be saved') });
      }
    } finally {
      setSaving(false);
    }
  };

  const settings = answer?.settings ?? null;
  const checklist = answer?.checklist ?? [];
  const location = settings?.media_location;
  const warnings = useMemo(() => checklist.filter((item) => item.warning), [checklist]);
  const notReady = checklist.filter((item) => !item.ready).length;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Switch the devices' telemetry on for this use case and tune the summary cadence. Nothing is deployed: the settings reach the devices through their telemetry shadow."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => navigate(`/workflow-dashboards?usecase_id=${encodeURIComponent(usecaseId)}`)}>Dashboards</Button>
                <Button iconName="refresh" onClick={load} disabled={loading} ariaLabel="Refresh">
                  Refresh
                </Button>
              </SpaceBetween>
            }
          >
            Telemetry settings
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)} data-testid="telemetry-settings-error">
              <SpaceBetween size="xxs">
                <span>{error.text}</span>
                {error.remedy && <span data-testid="telemetry-settings-remedy">{error.remedy}</span>}
              </SpaceBetween>
            </Alert>
          )}
          {saved && saved.status === 202 && (
            <Alert type="info" data-testid="telemetry-settings-pending">
              {saved.message ?? PENDING_NOTICE}
              {saved.pending_devices !== undefined ? ` (${saved.pending_devices} devices)` : ''}
            </Alert>
          )}
          {saved && saved.status !== 202 && (
            <Alert type="success" data-testid="telemetry-settings-saved">
              <SpaceBetween size="xxs">
                <span>Settings saved. Telemetry is {saved.settings.enabled ? 'on' : 'off'}.</span>
                <span data-testid="telemetry-settings-shadows">
                  {saved.devices.length === 0
                    ? 'No device records: no shadow written.'
                    : `Telemetry shadows: ${saved.devices
                        .map((d) => `${d.device_id} ${d.shadow}${d.error ? ` (${d.error})` : ''}`)
                        .join(', ')}.`}
                </span>
                {saved.unrecorded_devices.length > 0 && (
                  <span data-testid="telemetry-settings-unrecorded">
                    Devices without a record for this use case (they publish nothing): {saved.unrecorded_devices.join(', ')}.
                  </span>
                )}
              </SpaceBetween>
            </Alert>
          )}
          {!settings && loading && <StatusIndicator type="loading">Reading the settings…</StatusIndicator>}
          {settings && (
            <Form
              actions={
                <Button variant="primary" onClick={() => void save()} loading={saving} data-testid="telemetry-settings-save">
                  Save
                </Button>
              }
            >
              <SpaceBetween size="m">
                <Toggle checked={enabled} onChange={({ detail }) => setEnabled(detail.checked)} data-testid="telemetry-settings-enabled">
                  Telemetry enabled
                </Toggle>
                <FormField
                  label="Summary interval (seconds)"
                  description="One dda.summary.v1 per device and workflow per interval; 10 by default."
                  errorText={attempted && !summaryValid ? SUMMARY_MESSAGE : error?.field === 'summaryIntervalS' ? error.text : undefined}
                >
                  <Input value={summaryText} onChange={({ detail }) => setSummaryText(detail.value)} inputMode="numeric" data-testid="telemetry-settings-summary" />
                </FormField>
                <FormField
                  label="Idle heartbeat (seconds)"
                  description="A Heartbeat per device and workflow when no run happened for 5 minutes; 60 by default."
                  errorText={attempted && !heartbeatValid ? HEARTBEAT_MESSAGE : error?.field === 'idleHeartbeatS' ? error.text : undefined}
                >
                  <Input value={heartbeatText} onChange={({ detail }) => setHeartbeatText(detail.value)} inputMode="numeric" data-testid="telemetry-settings-heartbeat" />
                </FormField>
                <KeyValuePairs
                  columns={3}
                  items={[
                    { label: 'Media location (read-only)', value: <span data-testid="telemetry-settings-media-bucket">{location ? `${location.bucket}/${location.prefix}` : '—'}</span> },
                    { label: 'Media location source', value: location?.source ?? '—' },
                    {
                      label: 'Media region',
                      value: (
                        <span data-testid="telemetry-settings-media-region">
                          {location?.region ? `${location.region} (${location.region_source ?? 'header'})` : 'not probed yet'}
                        </span>
                      ),
                    },
                    { label: 'Person classes', value: settings.personClasses.join(', ') },
                    { label: 'Topology', value: settings.topology },
                    { label: 'Updated', value: settings.updatedAt ? `${formatTime(settings.updatedAt)} by ${settings.updatedBy ?? '—'}` : 'never' },
                  ]}
                />
              </SpaceBetween>
            </Form>
          )}
        </SpaceBetween>
      </Container>

      {settings && (
        <Container
          data-testid="telemetry-checklist"
          header={
            <Header
              variant="h2"
              description={notReady === 0 ? 'Every item is ready.' : `${notReady} item(s) need attention before telemetry flows.`}
            >
              Readiness checklist
            </Header>
          }
        >
          <SpaceBetween size="s">
            {warnings.map((item) => (
              <Alert key={`warning-${item.id}`} type="warning" data-testid={`telemetry-checklist-warning-${item.id}`}>
                {CHECKLIST_LABELS[item.id] ?? item.id}: {item.warning}
              </Alert>
            ))}
            {checklist.map((item) => (
              <Box key={item.id} data-testid={`telemetry-checklist-${item.id}`}>
                <SpaceBetween direction="horizontal" size="s" alignItems="center">
                  <StatusIndicator type={item.ready ? (item.warning ? 'warning' : 'success') : 'error'}>
                    <span data-testid={`telemetry-checklist-${item.id}-status`}>{item.ready ? (item.warning ? 'ready with a warning' : 'ready') : 'not ready'}</span>
                  </StatusIndicator>
                  <Box fontWeight="bold">{CHECKLIST_LABELS[item.id] ?? item.id}</Box>
                  {item.topology && <Badge color="grey">{item.topology}</Badge>}
                </SpaceBetween>
                {checklistDetail(item) && (
                  <Box variant="small" color="text-body-secondary" data-testid={`telemetry-checklist-${item.id}-detail`}>
                    {checklistDetail(item)}
                  </Box>
                )}
                {!item.ready && item.remedy && (
                  <Box variant="p" data-testid={`telemetry-checklist-${item.id}-remedy`}>
                    {item.remedy}
                  </Box>
                )}
              </Box>
            ))}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
