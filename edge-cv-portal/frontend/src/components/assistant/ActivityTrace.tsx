/**
 * The Activity_Trace of a Turn (spec portal-ai-assistant Requirement 5.3):
 * one compact row per tool start/end, plus the confirmation, budget, stop
 * and error events.
 */
import { Box, StatusIndicator } from '@cloudscape-design/components';
import type { TurnEvent } from './assistantApi';

interface Props {
  events: TurnEvent[];
}

interface Row {
  key: string;
  type: 'success' | 'error' | 'info' | 'warning' | 'stopped' | 'in-progress';
  text: string;
}

export function traceRows(events: TurnEvent[]): Row[] {
  const rows: Row[] = [];
  const open = new Map<string, number>();
  for (const event of events) {
    const summary = event.args_summary ? ` (${event.args_summary})` : '';
    const title = event.title || event.tool || 'tool';
    switch (event.type) {
      case 'tool_start':
        rows.push({ key: `e${event.seq}`, type: 'in-progress', text: `${title}${summary}` });
        if (event.tool) open.set(event.tool, rows.length - 1);
        break;
      case 'tool_end': {
        const index = event.tool !== undefined ? open.get(event.tool) : undefined;
        const outcome = event.category ? `${event.category.replace(/_/g, ' ')}` : 'done';
        const duration = event.duration_ms !== undefined ? ` · ${(event.duration_ms / 1000).toFixed(1)} s` : '';
        const row: Row = {
          key: `e${event.seq}`,
          type: event.category ? 'error' : 'success',
          text: `${index !== undefined ? rows[index].text : title}: ${outcome}${duration}`,
        };
        if (index !== undefined) {
          rows[index] = { ...row, key: rows[index].key };
          if (event.tool) open.delete(event.tool);
        } else {
          rows.push(row);
        }
        break;
      }
      case 'confirmation_requested':
        rows.push({ key: `e${event.seq}`, type: 'warning', text: `Waiting for your confirmation: ${event.tool ?? ''}` });
        break;
      case 'confirmation_resolved':
        rows.push({ key: `e${event.seq}`, type: event.decision === 'confirm' ? 'success' : 'stopped',
          text: event.decision === 'confirm' ? `Confirmed: ${event.tool ?? ''}` : `Declined: ${event.tool ?? ''}` });
        break;
      case 'confirmation_expired':
        rows.push({ key: `e${event.seq}`, type: 'warning', text: 'The confirmation expired' });
        break;
      case 'budget':
        rows.push({ key: `e${event.seq}`, type: 'warning', text: `Limit reached: ${(event.limit ?? '').replace(/_/g, ' ')}` });
        break;
      case 'stopped':
        rows.push({ key: `e${event.seq}`, type: 'stopped', text: 'Stopped' });
        break;
      case 'error':
        rows.push({ key: `e${event.seq}`, type: 'error', text: (event.code ?? 'error').replace(/_/g, ' ') });
        break;
      default:
        break;
    }
  }
  return rows;
}

export default function ActivityTrace({ events }: Props) {
  const rows = traceRows(events);
  if (rows.length === 0) return null;
  return (
    <Box data-testid="assistant-trace" fontSize="body-s">
      {rows.map((row) => (
        <div key={row.key}>
          <StatusIndicator type={row.type}>{row.text}</StatusIndicator>
        </div>
      ))}
    </Box>
  );
}
