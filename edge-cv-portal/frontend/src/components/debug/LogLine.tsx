/**
 * One compact line of the debug viewer for a `log`, `drop` or `session`
 * record (workflow-debug-mode design §8); a record of an unknown type (or
 * the API's `raw` wrapper of a non-JSON message) is shown as its JSON.
 *
 * - `log`: time, level, logger, message (and the attribution when it is not
 *   the default `between_runs`);
 * - `drop`: time, node, reason and count (coalesced per second on the device);
 * - `session`: time, state, reason, the agent instance and the counters.
 *
 * An event written more than 60 s after IoT received it carries the "late"
 * badge: a retried ingest invocation placed it at the end of the stream, and
 * the viewer inserted it at its place in time (design §8 step 3).
 */
import { Badge } from '@cloudscape-design/components';
import type { TailEvent } from '../../hooks/useDebugTail';
import type { DebugAgentCounters, DebugDropRecord, DebugLogRecord, DebugSessionRecord } from '../../pages/debug/types';
import { formatClock } from './format';

const LEVEL_COLOR: Record<string, string> = {
  ERROR: '#d13212',
  WARNING: '#8d6605',
  WARN: '#8d6605',
  INFO: '#0972d3',
  DEBUG: '#5f6b7a',
};

/** The counters that are worth a glance, as "records 12, bytes 3456, ...". */
export function countersSummary(counters: DebugAgentCounters | undefined): string {
  if (!counters) return '';
  const parts: string[] = [];
  for (const [key, value] of Object.entries(counters)) {
    if (typeof value === 'number' && (value !== 0 || key === 'records' || key === 'bytes')) {
      parts.push(`${key} ${value}`);
    }
  }
  return parts.join(', ');
}

/** The line's text without the time: what the tests and the download share. */
export function lineText(event: TailEvent): string {
  const record = event.record;
  switch (record.type) {
    case 'log': {
      const r = record as DebugLogRecord;
      const attribution = r.attribution && r.attribution !== 'between_runs' ? ` [${r.attribution}]` : '';
      return `${r.level} ${r.logger}${attribution} — ${r.message}`;
    }
    case 'drop': {
      const r = record as DebugDropRecord;
      return `drop ${r.nodeId}: ${r.reason} ×${r.count}`;
    }
    case 'session': {
      const r = record as DebugSessionRecord;
      const bits = [`session ${r.state}`, r.reason ? `(${r.reason})` : ''];
      if (r.instance) bits.push(`instance ${String(r.instance).slice(0, 8)}`);
      if (r.agentVersion) bits.push(`agent ${r.agentVersion}`);
      if (r.level) bits.push(`level ${r.level}${r.levelDowngraded ? ' (downgraded)' : ''}`);
      const counters = countersSummary(r.counters);
      if (counters) bits.push(counters);
      return bits.filter(Boolean).join(' ');
    }
    default: {
      const rest: Record<string, unknown> = { ...(record as Record<string, unknown>) };
      delete rest.id;
      delete rest.writeTs;
      delete rest.receivedMs;
      return `${record.type}: ${JSON.stringify(rest)}`;
    }
  }
}

export interface LogLineProps {
  event: TailEvent;
}

export default function LogLine({ event }: LogLineProps) {
  const record = event.record;
  const level = record.type === 'log' ? String((record as DebugLogRecord).level ?? '').toUpperCase() : '';
  const color =
    record.type === 'log' ? LEVEL_COLOR[level] ?? '#16191f' : record.type === 'drop' ? '#8d6605' : '#5f6b7a';
  return (
    <div
      data-testid={`debug-line-${record.type}`}
      data-event-id={event.id ?? undefined}
      style={{
        fontFamily: 'Monaco, Consolas, "Courier New", monospace',
        fontSize: '12px',
        padding: '2px 0',
        borderBottom: '1px solid #e9ebed',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      <span style={{ color: '#5f6b7a', marginRight: '8px' }}>{formatClock(event.sortTs)}</span>
      <span style={{ color }}>{lineText(event)}</span>
      {event.late && (
        <span style={{ marginLeft: '8px' }}>
          <Badge color="grey">late</Badge>
        </span>
      )}
    </div>
  );
}
