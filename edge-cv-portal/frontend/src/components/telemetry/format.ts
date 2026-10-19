/**
 * Small formatting helpers shared by the telemetry tiles
 * (workflow-telemetry-dashboards tasks.md 5.2-5.5).
 */

/** "12 s", "3 min", "2 h", "5 d" for a duration in seconds (never negative). */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86_400) return `${Math.round((s / 3600) * 10) / 10} h`;
  return `${Math.round((s / 86_400) * 10) / 10} d`;
}

/** "12 s ago" from an epoch-millisecond instant and the current time. */
export function formatAge(thenMs: number | null | undefined, nowMs: number): string {
  if (thenMs === null || thenMs === undefined || !Number.isFinite(thenMs)) return '—';
  return `${formatDuration((nowMs - thenMs) / 1000)} ago`;
}

/** A local date-time with seconds; "—" for a missing instant. */
export function formatTime(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** A short tick label for a bucket start: the time at minute resolution, the day otherwise. */
export function formatTick(date: Date, resolution: 'minute' | 'hour' | 'day'): string {
  if (resolution === 'day') {
    return date.toLocaleDateString(undefined, { month: '2-digit', day: '2-digit' });
  }
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (resolution === 'hour') {
    return `${date.toLocaleDateString(undefined, { month: '2-digit', day: '2-digit' })} ${time}`;
  }
  return time;
}

/** Compact numbers for chart axes: 1200 → "1.2k". */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '';
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${Math.round((value / 1_000_000) * 10) / 10}M`;
  if (abs >= 1_000) return `${Math.round((value / 1_000) * 10) / 10}k`;
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
}

/** A count with thousands separators. */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString();
}
