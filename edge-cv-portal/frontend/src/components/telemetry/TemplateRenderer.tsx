/**
 * The chart panels of a dashboard template (workflow-telemetry-dashboards
 * design §1, §8.2, §8.3; tasks.md 5.2).
 *
 * Generic over the template's panel layout served by `GET /telemetry/schemas`:
 * `area` → `AreaChart`, `line` → `LineChart`, `bar` and `stacked-bar` →
 * `BarChart`, `stacked-bar-states` → a stacked `BarChart` with one series per
 * model state (each bucket contributing 1 to the series of its
 * `model_state_worst`; `NONE` is not drawn), `pie-and-line` → a `PieChart` of
 * the range's totals beside a `LineChart`, `mixed-line-bar` →
 * `MixedLineBarChart`. Every panel renders from the ONE series answer the
 * `SeriesPanel` fetched (design §8.3); a panel whose measures no Summary of
 * the last 24 h reported renders "not reported by this workflow"; each header
 * shows the resolution and "updated <age>".
 */
import {
  AreaChart,
  BarChart,
  Box,
  ColumnLayout,
  Container,
  Header,
  LineChart,
  MixedLineBarChart,
  PieChart,
  SpaceBetween,
  type MixedLineBarChartProps,
} from '@cloudscape-design/components';
import type {
  DashboardSeriesResponse,
  TelemetryCatalog,
  TelemetryTemplate,
  TelemetryTemplatePanel,
} from '../../pages/telemetry/types';
import { formatAge, formatNumber, formatTick } from './format';
import { panelData, type NewestSummary, type PanelData } from './seriesData';

export const NOT_REPORTED = 'not reported by this workflow';
export const NO_DATA = 'No data in this range';

export interface TemplateRendererProps {
  template: TelemetryTemplate;
  catalog: TelemetryCatalog;
  /** The registry's `modelStateOrder` (best to worst). */
  modelStateOrder: readonly string[];
  /** The one series answer of the Dashboard; null before the first answer. */
  series: DashboardSeriesResponse | null;
  /** The Devices to fold (a drill-down); every Device of the answer when absent. */
  deviceIds?: readonly string[] | null;
  /** The newest Summaries from the Now answer, for the "not reported" rule. */
  newestSummaries?: readonly NewestSummary[];
  /** The clock for "updated <age>" (default `Date.now()`). */
  now?: number;
  loading?: boolean;
}

const CHART_HEIGHT = 220;

function chartI18n(resolution: 'minute' | 'hour' | 'day') {
  return {
    filterLabel: 'Filter displayed data',
    filterPlaceholder: 'Filter data',
    filterSelectedAriaLabel: 'selected',
    legendAriaLabel: 'Legend',
    chartAriaRoleDescription: 'chart',
    xTickFormatter: (value: Date | number | string) =>
      value instanceof Date ? formatTick(value, resolution) : String(value),
    yTickFormatter: (value: number) => formatNumber(value),
    detailPopoverDismissAriaLabel: 'Dismiss',
  };
}

function headerDescription(series: DashboardSeriesResponse | null, now: number): string {
  if (!series) return 'loading…';
  return `${series.resolution} resolution · updated ${formatAge(series.updatedAt, now)}`;
}

/** The per-panel chart; exported for the tests of the single panel shapes. */
export function PanelChart({
  data,
  resolution,
  loading,
}: {
  data: PanelData;
  resolution: 'minute' | 'hour' | 'day';
  loading: boolean;
}) {
  const { panel } = data;
  const statusType = loading && data.buckets.length === 0 ? 'loading' : 'finished';
  const i18n = chartI18n(resolution);
  const empty = (
    <Box textAlign="center" color="inherit">
      <b>{NO_DATA}</b>
    </Box>
  );
  const common = {
    height: CHART_HEIGHT,
    hideFilter: true,
    statusType,
    empty,
    yTitle: data.unit ?? undefined,
    ariaLabel: panel.title,
    i18nStrings: i18n,
  } as const;

  switch (panel.chart) {
    case 'area':
      return (
        <AreaChart
          {...common}
          xScaleType="time"
          series={data.series.map((s) => ({ title: s.title, type: 'area' as const, data: s.data }))}
        />
      );
    case 'line':
      return (
        <LineChart
          {...common}
          xScaleType="time"
          series={data.series.map((s) => ({ title: s.title, type: 'line' as const, data: s.data }))}
        />
      );
    case 'bar':
    case 'stacked-bar':
    case 'stacked-bar-states':
      return (
        <BarChart
          {...common}
          xScaleType="categorical"
          stackedBars={panel.chart !== 'bar'}
          series={data.series.map((s) => ({ title: s.title, type: 'bar' as const, data: s.data }))}
        />
      );
    case 'mixed-line-bar':
      return (
        <MixedLineBarChart
          {...common}
          xScaleType="categorical"
          series={data.series.map(
            (s): MixedLineBarChartProps.ChartSeries<Date> => ({
              title: s.title,
              type: data.barSeries.has(s.measureId) ? 'bar' : 'line',
              data: s.data,
            })
          )}
        />
      );
    case 'pie-and-line':
      return (
        <ColumnLayout columns={2}>
          <PieChart
            data={data.pie}
            hideFilter
            statusType={statusType}
            empty={empty}
            ariaLabel={`${panel.title} share`}
            size="small"
            detailPopoverContent={(datum, sum) => [
              { key: 'Count', value: formatNumber(datum.value) },
              { key: 'Share', value: sum > 0 ? `${Math.round((datum.value / sum) * 1000) / 10}%` : '—' },
            ]}
            segmentDescription={(datum, sum) => (sum > 0 ? `${Math.round((datum.value / sum) * 100)}%` : '')}
            i18nStrings={{
              filterLabel: 'Filter displayed data',
              filterPlaceholder: 'Filter data',
              filterSelectedAriaLabel: 'selected',
              legendAriaLabel: 'Legend',
              chartAriaRoleDescription: 'pie chart',
              segmentAriaRoleDescription: 'segment',
              detailPopoverDismissAriaLabel: 'Dismiss',
            }}
          />
          <LineChart
            {...common}
            xScaleType="time"
            series={data.series.map((s) => ({ title: s.title, type: 'line' as const, data: s.data }))}
          />
        </ColumnLayout>
      );
    default:
      return (
        <Box color="text-status-inactive" data-testid="telemetry-panel-unknown-chart">
          Unknown chart kind {String((panel as TelemetryTemplatePanel).chart)}
        </Box>
      );
  }
}

export default function TemplateRenderer({
  template,
  catalog,
  modelStateOrder,
  series,
  deviceIds,
  newestSummaries,
  now,
  loading = false,
}: TemplateRendererProps) {
  const clock = now ?? Date.now();
  const resolution = series?.resolution ?? 'minute';
  return (
    <SpaceBetween size="m">
      <ColumnLayout columns={2}>
        {template.panels.map((panel) => {
          const data = panelData(panel, catalog, series, {
            deviceIds,
            modelStateOrder,
            newest: newestSummaries ?? [],
            nowMs: clock,
          });
          return (
            <Container
              key={panel.id}
              data-testid={`telemetry-panel-${panel.id}`}
              header={
                <Header variant="h3" description={headerDescription(series, clock)}>
                  {panel.title}
                </Header>
              }
            >
              {series !== null && !data.reported ? (
                <Box textAlign="center" color="text-body-secondary" padding="l" data-testid={`telemetry-panel-${panel.id}-not-reported`}>
                  {NOT_REPORTED}
                </Box>
              ) : (
                <PanelChart data={data} resolution={resolution} loading={loading} />
              )}
            </Container>
          );
        })}
      </ColumnLayout>
    </SpaceBetween>
  );
}
