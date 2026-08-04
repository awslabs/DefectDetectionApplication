/**
 * Status Badge for Build_Jobs (Req 4.2): one color per status family —
 * grey for waiting/cancelled, blue for in-progress, green for
 * succeeded, red for failed/interrupted.
 *
 * Spec: .kiro/specs/portal-build-fleet-and-workflow-gates
 */
import { Badge } from '@cloudscape-design/components';
import type { BadgeProps } from '@cloudscape-design/components';

const STATUS_COLORS: Record<string, BadgeProps['color']> = {
  queued: 'grey',
  provisioning: 'blue',
  building: 'blue',
  publishing: 'blue',
  succeeded: 'green',
  failed: 'red',
  interrupted: 'red',
  cancelled: 'grey',
};

export default function BuildStatusBadge({ status }: { status?: string }) {
  return (
    <Badge color={STATUS_COLORS[status ?? ''] ?? 'grey'}>
      {status || 'unknown'}
    </Badge>
  );
}
