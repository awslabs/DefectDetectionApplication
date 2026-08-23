/**
 * Pure, dependency-free duration formatting for node execution timing
 * (node-execution-timing R3.3, R3.4, R3.8).
 *
 * Returns "412 ms" | "3.4 s" | "0 ms", or null when the value must not be
 * shown (non-numbers, NaN/Infinity, negatives).
 */
export function formatDuration(durationMs: unknown): string | null {
  if (typeof durationMs !== "number" || !Number.isFinite(durationMs) || durationMs < 0) {
    return null;
  }
  const rounded = Math.round(durationMs);
  if (rounded < 1000) {
    return `${rounded} ms`;
  }
  return `${(durationMs / 1000).toFixed(1)} s`;
}
