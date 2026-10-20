/**
 * Formatting helpers of the debug pages (workflow-debug-mode design §8).
 * Pure functions, shared by the sessions table, the viewer, the run card and
 * the log lines.
 */

/** Local date and time, or an em dash when the value is not a timestamp. */
export function formatTime(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '—';
  return new Date(ms).toLocaleString();
}

/** Local wall clock with milliseconds, `HH:MM:SS.mmm`, for the log lines. */
export function formatClock(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '--:--:--.---';
  const date = new Date(ms);
  const pad = (n: number, width = 2) => String(n).padStart(width, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

/** "1.5 KB", "2.0 MB", "512 B". */
export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** A duration in milliseconds as "123 ms" or "1.23 s"; "—" when unknown. */
export function formatMs(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}
