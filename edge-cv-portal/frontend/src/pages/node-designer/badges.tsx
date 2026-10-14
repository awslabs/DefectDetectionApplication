/**
 * Node_Designer badges (custom-node-designer).
 *
 * Lifecycle badges for the dev/test/prod progression, per-arch build
 * status indicators (succeeded/failed with log excerpt, Requirement
 * 3.5), and classification risk badges for the upstream good/bad/ugly
 * taxonomy (Requirement 15.1). The pure color/label mappings are
 * exported for tests.
 */
import { Badge, Popover, StatusIndicator } from '@cloudscape-design/components';
import type { BadgeProps } from '@cloudscape-design/components/badge';
import type { StatusIndicatorProps } from '@cloudscape-design/components/status-indicator';
import { ARCHITECTURE_LABELS, DeviceArchitecture } from './types';

/** The Badge color union of the installed Cloudscape version. */
type BadgeColor = NonNullable<BadgeProps['color']>;

// --------------------------------------------------------------- lifecycle

/** Badge color per Lifecycle_State (dev → test → prod). */
export function lifecycleBadgeColor(state: string | null | undefined): BadgeColor {
  switch (state) {
    case 'prod':
      return 'green';
    case 'test':
      return 'blue';
    case 'dev':
      return 'grey';
    default:
      return 'grey';
  }
}

export function LifecycleBadge({ state }: { state: string | null | undefined }) {
  return <Badge color={lifecycleBadgeColor(state)}>{state || 'unknown'}</Badge>;
}

// ----------------------------------------------------------- classification

/**
 * Badge color per Plugin_Set_Classification risk (15.1): good is low
 * risk, bad and ugly carry upstream caveats, unclassified is unknown
 * provenance.
 */
export function classificationBadgeColor(
  classification: string | null | undefined
): BadgeColor {
  switch (classification) {
    case 'good':
      return 'green';
    case 'bad':
      return 'red';
    case 'ugly':
      return 'severity-high';
    case 'unclassified':
      return 'severity-medium';
    default:
      return 'grey';
  }
}

export function ClassificationBadge({
  classification,
}: {
  classification: string | null | undefined;
}) {
  if (!classification) {
    return <span>—</span>;
  }
  return (
    <Badge color={classificationBadgeColor(classification)}>{classification}</Badge>
  );
}

// ------------------------------------------------------------- build status

/** StatusIndicator type per per-arch build status. */
export function buildStatusType(
  status: string | null | undefined
): StatusIndicatorProps.Type {
  switch (status) {
    case 'succeeded':
      return 'success';
    case 'failed':
      return 'error';
    case 'building':
      return 'in-progress';
    case 'queued':
      return 'pending';
    default:
      return 'stopped';
  }
}

/** Trim a CloudWatch log tail to a short excerpt for inline display. */
export function logExcerpt(logTail: string | null | undefined, maxLines = 12): string {
  if (!logTail) {
    return '';
  }
  const lines = logTail.split('\n').filter((line) => line.trim().length > 0);
  return lines.slice(-maxLines).join('\n');
}

/**
 * One per-arch build status entry: succeeded/failed indicator, with the
 * build log excerpt in a popover for failed builds (Requirement 3.5).
 */
export function BuildStatusIndicator({
  arch,
  status,
  logTail,
}: {
  arch: string;
  status: string | null | undefined;
  logTail?: string | null;
}) {
  const label =
    ARCHITECTURE_LABELS[arch as DeviceArchitecture] ?? arch;
  const indicator = (
    <StatusIndicator type={buildStatusType(status)}>
      {label}: {status || 'not built'}
    </StatusIndicator>
  );
  const excerpt = logExcerpt(logTail);
  if (status === 'failed' && excerpt) {
    return (
      <Popover
        header={`Build log (${label})`}
        size="large"
        content={
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              margin: 0,
              fontSize: '12px',
            }}
          >
            {excerpt}
          </pre>
        }
      >
        {indicator}
      </Popover>
    );
  }
  return indicator;
}
