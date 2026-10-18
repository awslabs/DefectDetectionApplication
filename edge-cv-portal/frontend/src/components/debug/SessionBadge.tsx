/**
 * Session state and remaining time of a debug session (workflow-debug-mode
 * design §8: "the session badge (state, remaining time)").
 *
 * Portal-side states (design §7.1): `pending` (created, at least one device's
 * shadow write not yet ok), `active`, `stopped` (DELETE) and `expired`
 * (maintenance). The remaining time counts from `expiresAt`, which the
 * Portal fixed at creation; a device that was offline gets only what is left
 * (design §3, R5.3).
 */
import { StatusIndicator, StatusIndicatorProps } from '@cloudscape-design/components';
import type { DebugSession, DebugSessionState } from '../../pages/debug/types';

const STATE_INDICATOR: Record<DebugSessionState, { type: StatusIndicatorProps.Type; label: string }> = {
  pending: { type: 'pending', label: 'Pending' },
  active: { type: 'success', label: 'Active' },
  stopped: { type: 'stopped', label: 'Stopped' },
  expired: { type: 'stopped', label: 'Expired' },
};

/** True for the states that count against the caps and accept records. */
export function isLiveState(state: string | null | undefined): boolean {
  return state === 'pending' || state === 'active';
}

/** Milliseconds until `expiresAt`, never negative. */
export function remainingMs(expiresAt: number | null | undefined, now: number = Date.now()): number {
  if (typeof expiresAt !== 'number' || !Number.isFinite(expiresAt)) return 0;
  return Math.max(0, expiresAt - now);
}

/** "42 min", "1 h 05 min", "< 1 min" or "0 min". */
export function formatRemaining(ms: number): string {
  if (ms <= 0) return '0 min';
  const minutes = Math.ceil(ms / 60_000);
  if (ms < 60_000) return '< 1 min';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${hours} h ${String(rest).padStart(2, '0')} min`;
}

export interface SessionBadgeProps {
  /** The session, or null/undefined when no session is known for the Pair. */
  session: Pick<DebugSession, 'state' | 'expiresAt'> | null | undefined;
  /** Injected clock for tests. */
  now?: number;
}

export default function SessionBadge({ session, now }: SessionBadgeProps) {
  if (!session) {
    return <StatusIndicator type="stopped">No debug session</StatusIndicator>;
  }
  const indicator = STATE_INDICATOR[session.state] ?? {
    type: 'pending' as const,
    label: String(session.state),
  };
  const remaining = isLiveState(session.state)
    ? ` · ${formatRemaining(remainingMs(session.expiresAt, now))} left`
    : '';
  return (
    <StatusIndicator type={indicator.type}>
      {indicator.label}
      {remaining}
    </StatusIndicator>
  );
}
