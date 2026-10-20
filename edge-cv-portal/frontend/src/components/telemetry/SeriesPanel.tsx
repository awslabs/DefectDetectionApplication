/**
 * The chart area of a dashboard: the range picker, the one series request
 * and the `TemplateRenderer` (workflow-telemetry-dashboards design §8.3,
 * §15.2; tasks.md 5.2).
 *
 * - `DateRangePicker` with the relative presets 15 min, 1 h, 6 h, 24 h, 7 d,
 *   30 d and absolute ranges up to 400 days (`range_too_long` beyond);
 * - exactly ONE `getDashboardSeries` request per Dashboard per refresh, with
 *   neither `device_id` nor `measures`; every panel renders from that answer
 *   (a Device drill-down folds the same answer client-side);
 * - refresh every 60 s at `minute` resolution, 300 s at `hour`, 900 s at
 *   `day` (the cadence table of `/telemetry/schemas`, else the defaults)
 *   while the page is visible, stopping when hidden and resuming at once;
 * - a manual refresh button rate-limited to one per 10 s;
 * - the API's 400 `range_too_long` shown as a notice.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  DateRangePicker,
  DateRangePickerProps,
  FormField,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';
import type {
  Dashboard,
  DashboardSeriesResponse,
  SeriesQuery,
  SeriesResolution,
  TelemetryCadence,
  TelemetryCatalog,
  TelemetryTemplate,
} from '../../pages/telemetry/types';
import TemplateRenderer from './TemplateRenderer';
import {
  DAY_MS,
  DEFAULT_MANUAL_REFRESH_MIN_SECONDS,
  DEFAULT_SERIES_REFRESH_SECONDS,
  HOUR_MS,
  MINUTE_MS,
  SERIES_MAX_RANGE_MS,
  resolutionFor,
  type NewestSummary,
} from './seriesData';
import { formatAge } from './format';

export const RANGE_TOO_LONG_NOTICE = 'The range may span at most 400 days';
export const START_BEFORE_END = 'The start must be before the end';

export const DEFAULT_RANGE: DateRangePickerProps.RelativeValue = { type: 'relative', amount: 15, unit: 'minute' };

export const RELATIVE_OPTIONS: DateRangePickerProps.RelativeOption[] = [
  { key: 'previous-15-minutes', amount: 15, unit: 'minute', type: 'relative' },
  { key: 'previous-1-hour', amount: 1, unit: 'hour', type: 'relative' },
  { key: 'previous-6-hours', amount: 6, unit: 'hour', type: 'relative' },
  { key: 'previous-24-hours', amount: 24, unit: 'hour', type: 'relative' },
  { key: 'previous-7-days', amount: 7, unit: 'day', type: 'relative' },
  { key: 'previous-30-days', amount: 30, unit: 'day', type: 'relative' },
];

const UNIT_MS: Record<DateRangePickerProps.TimeUnit, number> = {
  second: 1_000,
  minute: MINUTE_MS,
  hour: HOUR_MS,
  day: DAY_MS,
  week: 7 * DAY_MS,
  month: 30 * DAY_MS,
  year: 365 * DAY_MS,
};

/** The milliseconds of a relative range (`amount` × `unit`). */
export function relativeRangeMs(value: DateRangePickerProps.RelativeValue): number {
  return Math.max(0, Math.round(value.amount)) * (UNIT_MS[value.unit] ?? MINUTE_MS);
}

/** The `[from, to]` of a picker value at `now`: a relative range ends now; an absolute one is fixed. */
export function rangeWindow(value: DateRangePickerProps.Value | null, now: number): { from: number; to: number } {
  if (!value || value.type === 'relative') {
    const span = relativeRangeMs(value ?? DEFAULT_RANGE);
    return { from: now - span, to: now };
  }
  return { from: Date.parse(value.startDate), to: Date.parse(value.endDate) };
}

/** The picker's validation: a positive range of at most 400 days (design §8.3). */
export function validateRange(value: DateRangePickerProps.Value | null): DateRangePickerProps.ValidationResult {
  if (!value) return { valid: true };
  if (value.type === 'relative') {
    return relativeRangeMs(value) <= SERIES_MAX_RANGE_MS
      ? { valid: true }
      : { valid: false, errorMessage: RANGE_TOO_LONG_NOTICE };
  }
  const from = Date.parse(value.startDate);
  const to = Date.parse(value.endDate);
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) {
    return { valid: false, errorMessage: START_BEFORE_END };
  }
  return to - from <= SERIES_MAX_RANGE_MS ? { valid: true } : { valid: false, errorMessage: RANGE_TOO_LONG_NOTICE };
}

/** The refresh period in milliseconds for a resolution (design §8.3: 60 / 300 / 900 s). */
export function refreshMs(resolution: SeriesResolution, cadence?: Partial<TelemetryCadence> | null): number {
  const seconds = cadence?.series?.[resolution] ?? DEFAULT_SERIES_REFRESH_SECONDS[resolution];
  return Math.max(1, seconds) * 1000;
}

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
  dateTimeConstraintText: 'A range of at most 400 days; minute detail up to 6 hours, hourly up to 60 days, daily beyond.',
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

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

export type SeriesFetcher = (dashboardId: string, query: SeriesQuery) => Promise<DashboardSeriesResponse>;

export interface SeriesPanelProps {
  dashboard: Dashboard;
  template: TelemetryTemplate;
  catalog: TelemetryCatalog;
  modelStateOrder: readonly string[];
  /** The cadence table of `/telemetry/schemas` (defaults when absent). */
  cadence?: Partial<TelemetryCadence> | null;
  /** The newest Summaries of the Now answer, for the "not reported" rule. */
  newestSummaries?: readonly NewestSummary[];
  /** The starting picker value (default the last 15 minutes). */
  initialRange?: DateRangePickerProps.Value;
  /** Test seam; defaults to `apiService.getDashboardSeries`. */
  fetchSeries?: SeriesFetcher;
}

const ALL_DEVICES = '__all__';

export default function SeriesPanel({
  dashboard,
  template,
  catalog,
  modelStateOrder,
  cadence,
  newestSummaries,
  initialRange,
  fetchSeries,
}: SeriesPanelProps) {
  const fetcher: SeriesFetcher = useMemo(
    () => fetchSeries ?? ((id, query) => apiService.getDashboardSeries(id, query)),
    [fetchSeries]
  );
  const [range, setRange] = useState<DateRangePickerProps.Value | null>(initialRange ?? DEFAULT_RANGE);
  const [series, setSeries] = useState<DashboardSeriesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const [refreshLocked, setRefreshLocked] = useState(false);
  const [deviceFilter, setDeviceFilter] = useState<string>(ALL_DEVICES);
  const [clock, setClock] = useState(() => Date.now());
  const lastManualRef = useRef(0);

  const manualMinMs = (cadence?.manualRefreshMin ?? DEFAULT_MANUAL_REFRESH_MIN_SECONDS) * 1000;

  // "updated <age>" moves while the page is open.
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);

  // The one request per refresh, re-armed by the answer's resolution.
  useEffect(() => {
    let alive = true;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const dashboardId = dashboard.dashboard_id;

    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const run = async () => {
      if (!alive || inFlight) return;
      clearTimer();
      const now = Date.now();
      const window = rangeWindow(range, now);
      const guess = resolutionFor(window.from, window.to);
      if (guess === null) {
        // The picker refuses these; a stale value still never produces a request.
        setNotice(window.to <= window.from ? START_BEFORE_END : RANGE_TOO_LONG_NOTICE);
        return;
      }
      inFlight = true;
      setLoading(true);
      let resolution: SeriesResolution = guess;
      try {
        const answer = await fetcher(dashboardId, { from: window.from, to: window.to });
        if (!alive) return;
        resolution = answer.resolution ?? guess;
        setSeries(answer);
        setError(null);
        setNotice(null);
        setClock(Date.now());
      } catch (err) {
        if (!alive) return;
        if (isTelemetryApiError(err) && err.errorCode === 'range_too_long') {
          setNotice(err.remedy ? `${RANGE_TOO_LONG_NOTICE}. ${err.remedy}` : RANGE_TOO_LONG_NOTICE);
          setError(null);
        } else {
          setError(getErrorMessage(err, 'The series could not be read'));
        }
      } finally {
        inFlight = false;
        if (alive) setLoading(false);
      }
      if (alive && !isHidden()) timer = setTimeout(() => void run(), refreshMs(resolution, cadence));
    };

    const onVisibility = () => {
      if (isHidden()) {
        clearTimer();
      } else if (!inFlight && timer === null) {
        void run();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    void run();

    return () => {
      alive = false;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // The cadence table and the fetcher are stable per page; a change of the
    // dashboard, the range or a manual refresh re-arms the loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboard.dashboard_id, range, generation]);

  const manualRefresh = useCallback(() => {
    const now = Date.now();
    if (now - lastManualRef.current < manualMinMs) return;
    lastManualRef.current = now;
    setRefreshLocked(true);
    setTimeout(() => setRefreshLocked(false), manualMinMs);
    setGeneration((g) => g + 1);
  }, [manualMinMs]);

  const deviceOptions: SelectProps.Option[] = useMemo(
    () => [
      { label: 'All devices', value: ALL_DEVICES },
      ...dashboard.device_ids.map((id) => ({ label: id, value: id })),
    ],
    [dashboard.device_ids]
  );
  const selectedDevices = deviceFilter === ALL_DEVICES ? null : [deviceFilter];

  return (
    <SpaceBetween size="m">
      <Container
        data-testid="telemetry-series-panel"
        header={
          <Header
            variant="h2"
            description={
              series
                ? `${series.resolution} resolution · updated ${formatAge(series.updatedAt, clock)} · refresh every ${Math.round(
                    refreshMs(series.resolution, cadence) / 1000
                  )} s`
                : 'Reading the series…'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs" alignItems="end">
                <FormField label="Devices">
                  <Select
                    selectedOption={deviceOptions.find((o) => o.value === deviceFilter) ?? deviceOptions[0]}
                    onChange={({ detail }) => setDeviceFilter(detail.selectedOption.value ?? ALL_DEVICES)}
                    options={deviceOptions}
                    selectedAriaLabel="Selected"
                    data-testid="telemetry-series-device"
                  />
                </FormField>
                <FormField label="Range">
                  <DateRangePicker
                    value={range}
                    onChange={({ detail }) => setRange(detail.value)}
                    relativeOptions={RELATIVE_OPTIONS}
                    isValidRange={validateRange}
                    i18nStrings={PICKER_I18N}
                    placeholder="Last 15 minutes"
                    data-testid="telemetry-series-range"
                  />
                </FormField>
                <Button
                  iconName="refresh"
                  onClick={manualRefresh}
                  disabled={refreshLocked}
                  ariaLabel="Refresh the charts"
                  data-testid="telemetry-series-refresh"
                >
                  Refresh
                </Button>
              </SpaceBetween>
            }
          >
            {template.label}
          </Header>
        }
      >
        <SpaceBetween size="s">
          <Box data-testid="telemetry-series-status">
            <StatusIndicator type={loading ? 'loading' : error ? 'error' : 'success'}>
              {loading ? 'Refreshing…' : error ? error : series ? `${Object.keys(series.devices ?? {}).length} device(s) in the answer` : 'No answer yet'}
            </StatusIndicator>
          </Box>
          {notice && (
            <Alert type="warning" dismissible onDismiss={() => setNotice(null)} data-testid="telemetry-series-notice">
              {notice}
            </Alert>
          )}
        </SpaceBetween>
      </Container>
      <TemplateRenderer
        template={template}
        catalog={catalog}
        modelStateOrder={modelStateOrder}
        series={series}
        deviceIds={selectedDevices}
        newestSummaries={newestSummaries}
        now={clock}
        loading={loading}
      />
    </SpaceBetween>
  );
}
