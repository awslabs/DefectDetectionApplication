/**
 * Pure readers of the `now` answer shared by the Now and Health panels
 * (workflow-telemetry-dashboards design §9.1; tasks.md 5.3).
 */
import type {
  DashboardNowResponse,
  TelemetryFlow,
  TelemetryNowDevice,
  TelemetryPointer,
  TelemetrySummary,
} from '../../pages/telemetry/types';
import type { NewestSummary } from './seriesData';

export const ENGINE_LAST = 'LAST#engine';
export const ENGINE_HEARTBEAT = 'LASTHB#engine';

export const TELEMETRY_OFF = 'Telemetry is off for this use case';
export const NO_RUN_48H = 'no run in 48 h';

/** The newest non-Heartbeat Summary of the engine (the panel's values, design §9.1). */
export function enginePointer(device: TelemetryNowDevice): TelemetryPointer | null {
  return device.last?.[ENGINE_LAST] ?? null;
}

export function engineSummary(device: TelemetryNowDevice): TelemetrySummary | null {
  return enginePointer(device)?.summary ?? null;
}

/** Every `LAST#<producer>` pointer of every device, for the "not reported" rule. */
export function newestSummaries(now: DashboardNowResponse | null): NewestSummary[] {
  if (!now) return [];
  const out: NewestSummary[] = [];
  for (const device of Object.values(now.devices ?? {})) {
    for (const [sk, pointer] of Object.entries(device.last ?? {})) {
      if (!sk.startsWith('LAST#')) continue;
      out.push({ producer: sk.slice('LAST#'.length), receivedMs: pointer.receivedMs, summary: pointer.summary });
    }
  }
  return out;
}

export function flowStatus(flow: TelemetryFlow | string): { type: 'success' | 'pending' | 'stopped'; label: string } {
  switch (flow) {
    case 'flowing':
      return { type: 'success', label: 'flowing' };
    case 'idle':
      return { type: 'pending', label: 'idle' };
    default:
      return { type: 'stopped', label: 'silent' };
  }
}

export function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** A per-class map rendered as "helmet 90 · human 50". */
export function perClassText(perClass: Record<string, { total?: number; max?: number }> | undefined | null): string {
  if (!perClass) return '—';
  const entries = Object.entries(perClass)
    .map(([label, v]) => [label, num(v?.total) ?? 0] as const)
    .sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return '—';
  return entries.map(([label, total]) => `${label} ${total}`).join(' · ');
}

/** A one-line description of an Event's data by type. */
export function eventSummary(type: string, data: Record<string, unknown> | null | undefined): string {
  if (!data) return '';
  switch (type) {
    case 'violation_confirmed': {
      const reasons = data.reasons;
      const list = Array.isArray(reasons) ? reasons.join(', ') : reasons && typeof reasons === 'object' ? Object.keys(reasons).join(', ') : '';
      const people = num(data.violatingPeople) ?? num(data.people);
      return [list, people !== null ? `${people} people` : ''].filter(Boolean).join(' · ');
    }
    case 'clip_ready': {
      const clipId = typeof data.clipId === 'string' ? data.clipId : '';
      const duration = num(data.durationS);
      const failed = data.s3 === null || data.s3 === undefined ? (typeof data.s3Error === 'string' ? 'upload failed' : '') : '';
      return [clipId, duration !== null ? `${duration} s` : '', failed].filter(Boolean).join(' · ');
    }
    case 'run_error': {
      const error = typeof data.error === 'string' ? data.error : '';
      const count = num(data.count);
      const node = typeof data.node === 'string' ? data.node : typeof data.nodeId === 'string' ? data.nodeId : '';
      return [node, error.slice(0, 160), count !== null && count > 1 ? `×${count}` : ''].filter(Boolean).join(' · ');
    }
    default:
      return '';
  }
}

export function severityType(severity: string | null | undefined): 'error' | 'warning' | 'info' {
  if (severity === 'error') return 'error';
  if (severity === 'warning') return 'warning';
  return 'info';
}
