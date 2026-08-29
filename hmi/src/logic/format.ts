/**
 * Timestamp formatting (Requirement 4.6).
 *
 * The LocalServer reports run times (`startedAt`, `finishedAt`) as epoch
 * seconds. These pure helpers render them in the viewer's local time zone
 * with seconds precision, using fixed-width numeric fields so the kiosk
 * header and verdict panel never jitter as values change.
 */

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Formats an epoch-seconds timestamp as a local-time-zone time of day with
 * seconds precision: `HH:MM:SS` (24-hour clock).
 */
export function formatTime(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/**
 * Formats an epoch-seconds timestamp as a local-time-zone date and time with
 * seconds precision: `YYYY-MM-DD HH:MM:SS`.
 */
export function formatDateTime(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  const date = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  return `${date} ${formatTime(epochSeconds)}`;
}
