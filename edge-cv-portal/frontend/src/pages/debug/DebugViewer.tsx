/**
 * The debug log viewer, `/debug/:usecaseId/:workflowId/:deviceId`
 * (workflow-debug-mode design §8, §10.1; tasks.md 5.3).
 *
 * Toolbar: the `DateRangePicker` (default the last 15 minutes with the tail
 * on; an absolute range or the tail switched off reads a fixed window),
 * filters for execution id, node id, level and record type, "Download
 * NDJSON" (one fixed window at `limit=10000`, at most 2 calls per second, up
 * to 50,000 events), the `SessionBadge` of the Pair's newest session, "Open
 * in CloudWatch" for PortalAdmins only, the rate-limited notice ("Log reads
 * are rate-limited; polling every 10 s") and the "catching up" state.
 *
 * Body: the events ordered by the record's own `ts` (then `id`): `run`
 * records as expandable cards, `log`, `drop` and `session` records as compact
 * lines, each late write badged. The tail protocol lives in
 * `hooks/useDebugTail.ts`.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Container,
  DateRangePicker,
  DateRangePickerProps,
  FormField,
  Header,
  Input,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Toggle,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { getErrorMessage } from '../../utils/errorHandling';
import {
  DOWNLOAD_MAX_EVENTS,
  TAIL_BACKOFF_MS,
  TAIL_POLL_MS,
  downloadDebugLog,
  useDebugTail,
  type DebugTailFilters,
  type DebugTailWindow,
} from '../../hooks/useDebugTail';
import SessionBadge from '../../components/debug/SessionBadge';
import RunRecordCard from '../../components/debug/RunRecordCard';
import LogLine from '../../components/debug/LogLine';
import { formatTime } from '../../components/debug/format';
import type { DebugConsoleLinkResponse, DebugLogLevel, DebugRecordType, DebugSession } from './types';

export const RATE_LIMITED_NOTICE = 'Log reads are rate-limited; polling every 10 s';
export const CATCHING_UP_NOTICE =
  'Catching up: the window holds more events than one poll reads; at this rate the download is the right tool';
export const NO_SESSION_NOTICE = 'No debug session has been recorded for this device and workflow';
export const WINDOW_TOO_LONG = 'The window may span at most 24 hours';
/** Events rendered at once; older ones stay in the tail's buffer. */
export const RENDER_LIMIT = 2_000;
export const MAX_WINDOW_MS = 24 * 60 * 60_000;

export const DEFAULT_RANGE: DateRangePickerProps.RelativeValue = {
  type: 'relative',
  amount: 15,
  unit: 'minute',
};

const UNIT_MS: Record<DateRangePickerProps.TimeUnit, number> = {
  second: 1_000,
  minute: 60_000,
  hour: 3_600_000,
  day: 86_400_000,
  week: 7 * 86_400_000,
  month: 30 * 86_400_000,
  year: 365 * 86_400_000,
};

/** The milliseconds of a relative range (`amount` × `unit`). */
export function relativeRangeMs(value: DateRangePickerProps.RelativeValue): number {
  return Math.max(0, Math.round(value.amount)) * (UNIT_MS[value.unit] ?? 60_000);
}

/**
 * The tail window of a picker value: a relative range tails from
 * `now − range` when `tail` is on (fixed `[now − range, now]` otherwise); an
 * absolute range is always a fixed window.
 */
export function rangeToWindow(value: DateRangePickerProps.Value | null, tail: boolean, now: number): DebugTailWindow {
  if (!value || value.type === 'relative') {
    const span = value ? relativeRangeMs(value) : relativeRangeMs(DEFAULT_RANGE);
    return tail ? { tail: true, from: now - span } : { tail: false, from: now - span, to: now };
  }
  return { tail: false, from: Date.parse(value.startDate), to: Date.parse(value.endDate) };
}

/** The picker's validation: a window of at most 24 hours (the API's cap). */
export function validateRange(value: DateRangePickerProps.Value | null): DateRangePickerProps.ValidationResult {
  if (!value) return { valid: true };
  if (value.type === 'relative') {
    return relativeRangeMs(value) <= MAX_WINDOW_MS
      ? { valid: true }
      : { valid: false, errorMessage: WINDOW_TOO_LONG };
  }
  const from = Date.parse(value.startDate);
  const to = Date.parse(value.endDate);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) {
    return { valid: false, errorMessage: 'The start must be before the end' };
  }
  return to - from <= MAX_WINDOW_MS ? { valid: true } : { valid: false, errorMessage: WINDOW_TOO_LONG };
}

/** The newest session of the Pair among the use case's sessions, or null. */
export function sessionForPair(sessions: readonly DebugSession[], workflowId: string, deviceId: string): DebugSession | null {
  const matching = sessions.filter(
    (s) => s.workflow_id === workflowId && (s.devices ?? []).some((d) => d.device_id === deviceId)
  );
  if (matching.length === 0) return null;
  const live = matching.filter((s) => s.state === 'pending' || s.state === 'active');
  const pool = live.length ? live : matching;
  return pool.reduce((best, s) => (s.enabledAt > best.enabledAt ? s : best), pool[0]);
}

/** Offers `text` as a file download; a no-op where object URLs do not exist. */
export function saveTextFile(text: string, filename: string): boolean {
  if (typeof URL.createObjectURL !== 'function') return false;
  const blob = new Blob([text], { type: 'application/x-ndjson' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
  return true;
}

const LEVEL_OPTIONS: SelectProps.Option[] = [
  { label: 'Any level', value: '' },
  { label: 'DEBUG', value: 'DEBUG' },
  { label: 'INFO', value: 'INFO' },
  { label: 'WARNING', value: 'WARNING' },
  { label: 'ERROR', value: 'ERROR' },
];

const TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'Any type', value: '' },
  { label: 'run', value: 'run' },
  { label: 'log', value: 'log' },
  { label: 'drop', value: 'drop' },
  { label: 'session', value: 'session' },
];

const PICKER_I18N: DateRangePickerProps.I18nStrings = {
  todayAriaLabel: 'Today',
  nextMonthAriaLabel: 'Next month',
  previousMonthAriaLabel: 'Previous month',
  customRelativeRangeDurationLabel: 'Duration',
  customRelativeRangeDurationPlaceholder: 'Enter duration',
  customRelativeRangeOptionLabel: 'Custom range',
  customRelativeRangeOptionDescription: 'Set a custom range in the past',
  customRelativeRangeUnitLabel: 'Unit of time',
  formatRelativeRange: (e) => `Last ${e.amount} ${e.unit}${e.amount > 1 ? 's' : ''}`,
  formatUnit: (unit, value) => (value === 1 ? unit : `${unit}s`),
  dateTimeConstraintText: 'A window of at most 24 hours of write time.',
  relativeModeTitle: 'Relative range',
  absoluteModeTitle: 'Absolute range',
  relativeRangeSelectionHeading: 'Choose a range',
  startDateLabel: 'Start date',
  endDateLabel: 'End date',
  startTimeLabel: 'Start time',
  endTimeLabel: 'End time',
  clearButtonLabel: 'Clear',
  cancelButtonLabel: 'Cancel',
  applyButtonLabel: 'Apply',
};

export default function DebugViewer() {
  const { usecaseId = '', workflowId = '', deviceId = '' } = useParams<{
    usecaseId: string;
    workflowId: string;
    deviceId: string;
  }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  // The Pair's newest session: the badge, the images and the console link.
  const [session, setSession] = useState<DebugSession | null | undefined>(undefined);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const loadSession = useCallback(() => {
    if (!usecaseId) return;
    apiService
      .listDebugSessions(usecaseId)
      .then((answer) => {
        setSession(sessionForPair(answer.sessions ?? [], workflowId, deviceId));
        setSessionError(null);
      })
      .catch((err) => {
        setSession(null);
        setSessionError(getErrorMessage(err, 'Failed to read the sessions'));
      });
  }, [usecaseId, workflowId, deviceId]);

  useEffect(() => {
    loadSession();
  }, [loadSession]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  // The window: the picker value and the tail switch, re-anchored on apply.
  const [range, setRange] = useState<DateRangePickerProps.Value | null>(DEFAULT_RANGE);
  const [tail, setTail] = useState(true);
  const [anchor, setAnchor] = useState(0);
  const timeWindow = useMemo(
    () => rangeToWindow(range, tail, Date.now()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [range, tail, anchor]
  );

  // Filters: the selects apply at once, the text inputs on Apply or Enter.
  const [executionDraft, setExecutionDraft] = useState('');
  const [nodeDraft, setNodeDraft] = useState('');
  const [level, setLevel] = useState<DebugLogLevel | ''>('');
  const [type, setType] = useState<DebugRecordType | ''>('');
  const [applied, setApplied] = useState<{ execution_id: string; node_id: string }>({ execution_id: '', node_id: '' });
  const filters: DebugTailFilters = useMemo(
    () => ({
      ...(applied.execution_id ? { execution_id: applied.execution_id } : {}),
      ...(applied.node_id ? { node_id: applied.node_id } : {}),
      ...(level ? { level } : {}),
      ...(type ? { type } : {}),
    }),
    [applied, level, type]
  );
  const applyTextFilters = () => {
    setApplied({ execution_id: executionDraft.trim(), node_id: nodeDraft.trim() });
    setAnchor((a) => a + 1);
  };

  const tailState = useDebugTail({ usecaseId, deviceId, workflowId, window: timeWindow, filters });

  // Download and console link.
  const [downloading, setDownloading] = useState(false);
  const [downloadNote, setDownloadNote] = useState<string | null>(null);
  const [consoleLink, setConsoleLink] = useState<DebugConsoleLinkResponse | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleDownload = async () => {
    const from = timeWindow.from ?? Date.now() - relativeRangeMs(DEFAULT_RANGE);
    const to = timeWindow.to ?? Date.now();
    try {
      setDownloading(true);
      setActionError(null);
      const result = await downloadDebugLog({ usecaseId, deviceId, workflowId, from, to, filters });
      const saved = saveTextFile(result.ndjson, `debug-${deviceId}-${workflowId}-${from}-${to}.ndjson`);
      setDownloadNote(
        `${result.count} event(s) in ${result.calls} call(s)` +
          (result.truncated ? ` (capped at ${DOWNLOAD_MAX_EVENTS.toLocaleString()}; narrow the window for the rest)` : '') +
          (saved ? '' : ' — the browser offered no download')
      );
    } catch (err) {
      setActionError(getErrorMessage(err, 'The download failed'));
    } finally {
      setDownloading(false);
    }
  };

  const handleConsoleLink = async () => {
    if (!session) return;
    try {
      setActionError(null);
      const link = await apiService.getDebugConsoleLink(session.session_id, deviceId);
      setConsoleLink(link);
      window.open(link.url, '_blank', 'noopener,noreferrer');
    } catch (err) {
      setActionError(getErrorMessage(err, 'The console link could not be built'));
    }
  };

  const shown = tailState.events.length > RENDER_LIMIT ? tailState.events.slice(-RENDER_LIMIT) : tailState.events;
  const tailOn = timeWindow.tail;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={`Device ${deviceId} · workflow ${workflowId}`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => navigate(`/debug/${encodeURIComponent(usecaseId)}`)}>Sessions</Button>
                <Button
                  iconName="refresh"
                  onClick={() => {
                    loadSession();
                    tailState.refresh();
                  }}
                  disabled={tailState.loading}
                >
                  Refresh
                </Button>
                <Button onClick={handleDownload} loading={downloading} disabled={downloading} data-testid="debug-download">
                  Download NDJSON
                </Button>
                {isPortalAdmin && (
                  <Button onClick={handleConsoleLink} disabled={!session} data-testid="debug-console-link">
                    Open in CloudWatch
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            Debug log
          </Header>
        }
      >
        <SpaceBetween size="m">
          <SpaceBetween direction="horizontal" size="m" alignItems="center">
            <span data-testid="debug-session-badge">
              <SessionBadge session={session} now={now} />
            </span>
            {session && (
              <Box variant="small" color="text-body-secondary">
                session {session.session_id} · expires {formatTime(session.expiresAt)}
              </Box>
            )}
            {sessionError && (
              <StatusIndicator type="warning">{sessionError}</StatusIndicator>
            )}
          </SpaceBetween>

          <SpaceBetween direction="horizontal" size="s" alignItems="end">
            <FormField label="Write-time window">
              <DateRangePicker
                value={range}
                onChange={({ detail }) => {
                  setRange(detail.value);
                  setAnchor((a) => a + 1);
                }}
                relativeOptions={[
                  { key: 'previous-15-minutes', amount: 15, unit: 'minute', type: 'relative' },
                  { key: 'previous-1-hour', amount: 1, unit: 'hour', type: 'relative' },
                  { key: 'previous-6-hours', amount: 6, unit: 'hour', type: 'relative' },
                  { key: 'previous-24-hours', amount: 24, unit: 'hour', type: 'relative' },
                ]}
                isValidRange={validateRange}
                i18nStrings={PICKER_I18N}
                placeholder="Last 15 minutes"
                data-testid="debug-range"
              />
            </FormField>
            <Toggle
              checked={tail && range?.type !== 'absolute'}
              disabled={range?.type === 'absolute'}
              onChange={({ detail }) => {
                setTail(detail.checked);
                setAnchor((a) => a + 1);
              }}
              data-testid="debug-tail-toggle"
            >
              Tail
            </Toggle>
            <FormField label="Execution id">
              <Input
                value={executionDraft}
                onChange={({ detail }) => setExecutionDraft(detail.value)}
                onKeyDown={(e) => {
                  if (e.detail.key === 'Enter') applyTextFilters();
                }}
                placeholder="execution id"
                ariaLabel="Execution id filter"
                data-testid="debug-filter-execution"
              />
            </FormField>
            <FormField label="Node id">
              <Input
                value={nodeDraft}
                onChange={({ detail }) => setNodeDraft(detail.value)}
                onKeyDown={(e) => {
                  if (e.detail.key === 'Enter') applyTextFilters();
                }}
                placeholder="node id"
                ariaLabel="Node id filter"
                data-testid="debug-filter-node"
              />
            </FormField>
            <FormField label="Level">
              <Select
                selectedOption={LEVEL_OPTIONS.find((o) => o.value === level) ?? LEVEL_OPTIONS[0]}
                onChange={({ detail }) => setLevel((detail.selectedOption.value as DebugLogLevel | '') ?? '')}
                options={LEVEL_OPTIONS}
                selectedAriaLabel="Selected"
                data-testid="debug-filter-level"
              />
            </FormField>
            <FormField label="Type">
              <Select
                selectedOption={TYPE_OPTIONS.find((o) => o.value === type) ?? TYPE_OPTIONS[0]}
                onChange={({ detail }) => setType((detail.selectedOption.value as DebugRecordType | '') ?? '')}
                options={TYPE_OPTIONS}
                selectedAriaLabel="Selected"
                data-testid="debug-filter-type"
              />
            </FormField>
            <Button onClick={applyTextFilters} data-testid="debug-filter-apply">
              Apply
            </Button>
          </SpaceBetween>

          <SpaceBetween direction="horizontal" size="m" data-testid="debug-status">
            <StatusIndicator type={tailOn ? (tailState.loading ? 'loading' : 'in-progress') : 'stopped'}>
              {tailOn
                ? `Tail on · polling every ${Math.round(tailState.pollIntervalMs / 1000)} s`
                : 'Fixed window'}
            </StatusIndicator>
            {tailState.rateLimited && (
              <StatusIndicator type="warning">
                <span data-testid="debug-rate-limited">{RATE_LIMITED_NOTICE}</span>
              </StatusIndicator>
            )}
            {tailState.catchingUp && (
              <StatusIndicator type="in-progress">
                <span data-testid="debug-catching-up">{CATCHING_UP_NOTICE}</span>
              </StatusIndicator>
            )}
            {tailState.noSession && (
              <StatusIndicator type="stopped">
                <span data-testid="debug-no-session">{NO_SESSION_NOTICE}</span>
              </StatusIndicator>
            )}
            <Box variant="small" color="text-body-secondary">
              {tailState.events.length} event(s)
              {tailState.events.length > RENDER_LIMIT ? ` · showing the last ${RENDER_LIMIT}` : ''}
              {tailState.lastPollAt ? ` · last poll ${formatTime(tailState.lastPollAt)}` : ''}
            </Box>
          </SpaceBetween>

          {tailState.error && <Alert type="error">{tailState.error}</Alert>}
          {actionError && (
            <Alert type="error" dismissible onDismiss={() => setActionError(null)}>
              {actionError}
            </Alert>
          )}
          {downloadNote && (
            <Alert type="success" dismissible onDismiss={() => setDownloadNote(null)} data-testid="debug-download-note">
              {downloadNote}
            </Alert>
          )}
          {consoleLink && (
            <Alert type="info" dismissible onDismiss={() => setConsoleLink(null)} data-testid="debug-console-note">
              {consoleLink.logGroup} / {consoleLink.logStream} in {consoleLink.region}. {consoleLink.note}
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      <Container>
        {shown.length === 0 ? (
          <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="debug-empty">
            {tailState.loading && tailState.polls === 0
              ? 'Reading the log…'
              : tailOn
                ? `No events in the window yet; polling every ${Math.round(tailState.pollIntervalMs / 1000)} s (${TAIL_POLL_MS / 1000} s normally, ${TAIL_BACKOFF_MS / 1000} s when rate-limited).`
                : 'No events in the window.'}
          </Box>
        ) : (
          <div data-testid="debug-events">
            {shown.map((event) =>
              event.record.type === 'run' ? (
                <RunRecordCard
                  key={event.id ?? `${event.writeTs}-${event.sortTs}`}
                  event={event}
                  sessionId={session?.session_id ?? null}
                  deviceId={deviceId}
                />
              ) : (
                <LogLine key={event.id ?? `${event.writeTs}-${event.sortTs}`} event={event} />
              )
            )}
          </div>
        )}
      </Container>
    </SpaceBetween>
  );
}
