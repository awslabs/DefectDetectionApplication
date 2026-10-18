/**
 * The Events list below the charts (workflow-telemetry-dashboards design §8.4;
 * tasks.md 5.5): the Dashboard's Events newest first, 50 per page, with the
 * device and type filters, refreshed every 15 s while visible (the `events`
 * cadence of `/telemetry/schemas`), older pages through the cursor.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { apiService } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type { DashboardEventsResponse, EventsQuery, TelemetryEvent } from '../../pages/telemetry/types';
import { formatAge, formatTime } from './format';
import { eventSummary, severityType } from './nowData';

export const DEFAULT_EVENTS_REFRESH_SECONDS = 15;
export const EVENT_TYPES = ['violation_confirmed', 'clip_ready', 'run_error'] as const;

export type EventsFetcher = (dashboardId: string, query: EventsQuery) => Promise<DashboardEventsResponse>;

export interface EventsListProps {
  dashboardId: string;
  deviceIds: readonly string[];
  refreshSeconds?: number;
  fetchEvents?: EventsFetcher;
}

const ALL = '__all__';

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

export default function EventsList({ dashboardId, deviceIds, refreshSeconds, fetchEvents }: EventsListProps) {
  const fetcher: EventsFetcher = useMemo(
    () => fetchEvents ?? ((id, query) => apiService.getDashboardEvents(id, query)),
    [fetchEvents]
  );
  const periodMs = Math.max(1, refreshSeconds ?? DEFAULT_EVENTS_REFRESH_SECONDS) * 1000;
  const [device, setDevice] = useState(ALL);
  const [type, setType] = useState(ALL);
  const [events, setEvents] = useState<TelemetryEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(() => Date.now());

  const baseQuery = useMemo((): EventsQuery => {
    const query: EventsQuery = {};
    if (device !== ALL) query.device_id = device;
    if (type !== ALL) query.type = type;
    return query;
  }, [device, type]);

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };
    const load = async () => {
      if (!alive || inFlight) return;
      clearTimer();
      inFlight = true;
      setLoading(true);
      try {
        const answer = await fetcher(dashboardId, baseQuery);
        if (!alive) return;
        setEvents(answer.events ?? []);
        setCursor(answer.cursor ?? null);
        setError(null);
        setUpdatedAt(Date.now());
        setClock(Date.now());
      } catch (err) {
        if (!alive) return;
        setError(getErrorMessage(err, 'The events could not be listed'));
      } finally {
        inFlight = false;
        if (alive) setLoading(false);
      }
      if (alive && !isHidden()) timer = setTimeout(() => void load(), periodMs);
    };
    const onVisibility = () => {
      if (isHidden()) clearTimer();
      else if (!inFlight && timer === null) void load();
    };
    document.addEventListener('visibilitychange', onVisibility);
    void load();
    return () => {
      alive = false;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [dashboardId, baseQuery, periodMs, fetcher]);

  const loadMore = async () => {
    if (!cursor) return;
    try {
      setLoadingMore(true);
      const answer = await fetcher(dashboardId, { ...baseQuery, cursor });
      setEvents((current) => {
        const seen = new Set(current.map((e) => e.eventId));
        return [...current, ...(answer.events ?? []).filter((e) => !seen.has(e.eventId))];
      });
      setCursor(answer.cursor ?? null);
    } catch (err) {
      setError(getErrorMessage(err, 'The older events could not be listed'));
    } finally {
      setLoadingMore(false);
    }
  };

  const deviceOptions: SelectProps.Option[] = [{ label: 'All devices', value: ALL }, ...deviceIds.map((id) => ({ label: id, value: id }))];
  const typeOptions: SelectProps.Option[] = [{ label: 'All types', value: ALL }, ...EVENT_TYPES.map((t) => ({ label: t, value: t }))];

  return (
    <Container
      data-testid="telemetry-events-list"
      header={
        <Header
          variant="h2"
          description={updatedAt ? `newest first · updated ${formatAge(updatedAt, clock)} · refresh every ${periodMs / 1000} s while visible` : 'Reading the events…'}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                selectedOption={deviceOptions.find((o) => o.value === device) ?? deviceOptions[0]}
                onChange={({ detail }) => setDevice(detail.selectedOption.value ?? ALL)}
                options={deviceOptions}
                selectedAriaLabel="Selected"
                data-testid="telemetry-events-device"
              />
              <Select
                selectedOption={typeOptions.find((o) => o.value === type) ?? typeOptions[0]}
                onChange={({ detail }) => setType(detail.selectedOption.value ?? ALL)}
                options={typeOptions}
                selectedAriaLabel="Selected"
                data-testid="telemetry-events-type"
              />
            </SpaceBetween>
          }
        >
          Events
        </Header>
      }
      footer={
        cursor ? (
          <Box textAlign="center">
            <Button onClick={() => void loadMore()} loading={loadingMore} data-testid="telemetry-events-more">
              Load older events
            </Button>
          </Box>
        ) : undefined
      }
    >
      <SpaceBetween size="s">
        {error && (
          <Alert type="error" data-testid="telemetry-events-error">
            {error}
          </Alert>
        )}
        {loading && events.length === 0 && <StatusIndicator type="loading">Reading…</StatusIndicator>}
        <Table
          items={events}
          trackBy="eventId"
          variant="embedded"
          loadingText="Loading events"
          data-testid="telemetry-events-table"
          columnDefinitions={[
            {
              id: 'type',
              header: 'Type',
              cell: (e) => (
                <Badge color={severityType(e.severity) === 'error' ? 'red' : severityType(e.severity) === 'warning' ? 'blue' : 'grey'}>
                  {e.type}
                </Badge>
              ),
            },
            { id: 'time', header: 'Time', cell: (e) => `${formatTime(e.eventMs)} (${formatAge(e.eventMs, clock)})` },
            { id: 'device', header: 'Device', cell: (e) => e.deviceId },
            { id: 'severity', header: 'Severity', cell: (e) => e.severity ?? '—' },
            { id: 'summary', header: 'Details', cell: (e) => eventSummary(e.type, e.data) || '—' },
          ]}
          empty={
            <Box textAlign="center" color="text-body-secondary" padding="s" data-testid="telemetry-events-empty">
              No events in the last 24 hours
            </Box>
          }
        />
      </SpaceBetween>
    </Container>
  );
}
