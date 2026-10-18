/**
 * Unit tests for the run record card and the compact log lines
 * (workflow-debug-mode task 5.3, design §4.2 and §8; Requirements 6.4, 6.5).
 *
 * - the card from fixture records: status, the timing row
 *   `queued / setup (readiness wait) / pipeline`, the trigger, the nodes and
 *   detections tables, the bindings, the error and traceback, the `logs`
 *   list with its `src` badge, the truncated markers, the late badge;
 * - an `unknown` trigger shows its source and serialized context;
 * - the image through the injected fetcher, with the notice on 409 and on 404;
 * - `LogLine` for `log`, `drop`, `session` and an unknown record type.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { TailEvent } from '../../hooks/useDebugTail';
import type { DebugRunRecord } from '../../pages/debug/types';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

import { DebugApiError } from '../../services/api';
import RunRecordCard, { describeTrigger, runSummary } from './RunRecordCard';
import LogLine, { countersSummary, lineText } from './LogLine';

const TS = 1_792_075_062_000;

const RUN: DebugRunRecord = {
  type: 'run',
  ts: TS,
  executionId: 'exec-0001-abcd',
  status: 'completed',
  enqueuedAtS: Math.floor((TS - 5000) / 1000),
  acceptedMs: TS - 4000,
  startedMs: TS - 3200,
  finishedMs: TS,
  queuedMs: 1000,
  setupMs: 800,
  readinessWaitMs: 300,
  durationMs: 3200,
  trigger: {
    type: 'mqtt',
    source: 'mqtt',
    topic: 'line/1/trigger',
    payload: '{"part": 7}',
    timestamp: TS - 5000,
    receivedMs: TS - 5000,
  },
  nodes: [
    { nodeId: 'camera', status: 'completed', durationMs: 120, detail: null },
    { nodeId: 'detector', status: 'completed', durationMs: 2900, detail: '3 detections' },
  ],
  outputs: {
    detections: [
      { label: 'vest', confidence: 0.912, box: [10, 20, 110, 220], id: 'd1' },
      { label: 'person', confidence: 0.5, box: [0, 0, 300, 400] },
    ],
    isAnomalous: false,
    confidence: 0.91,
  },
  bindings: [{ nodeId: 'mqtt-out', kind: 'mqtt', result: 'published', detail: 'results/line1' }],
  model: { readiness: [{ model: 'yolo', state: 'READY', waitedMs: 300, ready: true }], wait: null },
  frameSource: { kind: 'continuous', cameraId: 'cam-1', width: 1920, height: 1080, frameSeq: 42, frameTs: TS - 3300 },
  artifacts: { outputDir: '/data/runs/exec-0001', captureId: 'cap-1', hasOverlay: true },
  image: { bucket: 'results', key: 'dda-telemetry/debug/uc/thing/wf/s/exec-0001-abcd.jpg', width: 960, height: 540, bytes: 120_000 },
  logs: [
    { ts: TS - 3000, level: 'INFO', logger: 'workflow_engine', message: 'pipeline started', src: 'runlog' },
    { ts: TS - 2000, level: 'DEBUG', logger: 'dda_triton', message: 'model yolo ready', src: 'tap' },
  ],
  truncated: [{ field: 'logs', originalBytes: 70_000 }],
};

function tailEvent(record: TailEvent['record'], overrides: Partial<TailEvent> = {}): TailEvent {
  const ts = typeof record.ts === 'number' ? record.ts : TS;
  return {
    id: 's:i:1:0',
    writeTs: ts + 100,
    receivedMs: ts + 50,
    ts,
    record,
    late: false,
    sortTs: ts,
    ...overrides,
  };
}

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as any).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('RunRecordCard — a completed run from the fixture record', () => {
  it('renders the status, the timing row, the trigger, the tables, the bindings, the logs and the markers', () => {
    render(<RunRecordCard event={tailEvent(RUN)} sessionId="sess-1" deviceId="thing-a" defaultExpanded />);

    // Header summary and status.
    expect(screen.getByText(/run exec-000 completed · mqtt line\/1\/trigger · pipeline 3\.20 s/)).toBeInTheDocument();
    expect(screen.getAllByText('completed').length).toBeGreaterThan(0);
    // The timing row: queued / setup (readiness wait) / pipeline.
    expect(screen.getByTestId('run-timing-queued').textContent).toBe('1.00 s');
    expect(screen.getByTestId('run-timing-setup').textContent).toBe('800 ms (300 ms)');
    expect(screen.getByTestId('run-timing-pipeline').textContent).toBe('3.20 s');
    // Trigger with its payload.
    expect(screen.getByTestId('run-trigger').textContent).toBe('mqtt line/1/trigger');
    expect(screen.getByTestId('run-trigger-payload').textContent).toBe('{"part": 7}');
    // Nodes and detections tables.
    expect(screen.getByTestId('run-nodes').textContent).toContain('detector');
    expect(screen.getByTestId('run-nodes').textContent).toContain('2.90 s');
    expect(screen.getByTestId('run-nodes').textContent).toContain('3 detections');
    expect(screen.getByTestId('run-detections').textContent).toContain('vest');
    expect(screen.getByTestId('run-detections').textContent).toContain('0.91');
    expect(screen.getByTestId('run-detections').textContent).toContain('10, 20, 110, 220');
    // Bindings.
    expect(screen.getByTestId('run-bindings').textContent).toContain('mqtt-out');
    expect(screen.getByTestId('run-bindings').textContent).toContain('published');
    // Model, frame source, artifacts.
    expect(screen.getByText(/yolo: READY after 300 ms/)).toBeInTheDocument();
    expect(screen.getByText(/continuous cam-1 1920×1080 #42/)).toBeInTheDocument();
    expect(screen.getByText(/\/data\/runs\/exec-0001 capture cap-1 \(overlay\)/)).toBeInTheDocument();
    // The logs list with its src badges.
    const logs = screen.getByTestId('run-logs');
    expect(logs.textContent).toContain('pipeline started');
    expect(logs.textContent).toContain('model yolo ready');
    expect(logs.textContent).toContain('runlog');
    expect(logs.textContent).toContain('tap');
    // Truncated markers.
    expect(screen.getByText('truncated ×1')).toBeInTheDocument();
    expect(screen.getByText(/truncated logs \(68\.4 KB\)/)).toBeInTheDocument();
    // No error block for a completed run.
    expect(screen.queryByTestId('run-error')).toBeNull();
  });

  it('shows the error and traceback of a failed run with the failing node', () => {
    const failed: DebugRunRecord = {
      ...RUN,
      status: 'failed',
      error: 'detector raised RuntimeError',
      traceback: 'Traceback (most recent call last):\n  File "x.py"',
      failingNodeId: 'detector',
      startedMs: null,
      setupMs: null,
      durationMs: null,
    };
    render(<RunRecordCard event={tailEvent(failed)} defaultExpanded />);

    expect(screen.getByTestId('run-error').textContent).toContain('Failed at detector');
    expect(screen.getByTestId('run-error').textContent).toContain('detector raised RuntimeError');
    expect(screen.getByTestId('run-traceback').textContent).toContain('File "x.py"');
    expect(screen.getByTestId('run-timing-setup').textContent).toBe('— (300 ms)');
    expect(screen.getByTestId('run-timing-pipeline').textContent).toBe('—');
  });

  it('badges a late event', () => {
    render(<RunRecordCard event={tailEvent(RUN, { late: true })} defaultExpanded />);
    expect(screen.getByText('late')).toBeInTheDocument();
  });

  it('shows an unknown trigger with its source and serialized context', () => {
    const unknown: DebugRunRecord = {
      ...RUN,
      trigger: {
        type: 'unknown',
        source: 'future-kind',
        topic: null,
        payload: '{"kind": "future", "source": "future-kind"}',
        timestamp: null,
        receivedMs: null,
      },
    };
    render(<RunRecordCard event={tailEvent(unknown)} defaultExpanded />);

    expect(screen.getByTestId('run-trigger').textContent).toBe('unknown trigger (source future-kind)');
    expect(screen.getByTestId('run-trigger-payload').textContent).toBe('{"kind": "future", "source": "future-kind"}');
  });

  it('describes the five trigger shapes', () => {
    expect(describeTrigger(null)).toBe('no trigger context');
    expect(describeTrigger({ type: 'manual', source: null, topic: null, payload: null, timestamp: null, receivedMs: null })).toBe('manual');
    expect(describeTrigger({ type: 'continuous', source: 'continuous', topic: null, payload: null, timestamp: 1, receivedMs: 2 })).toBe('continuous');
    expect(describeTrigger({ type: 'opcua', source: 'opcua', topic: 'opc.tcp://plc ns=2;s=Tag', payload: '1', timestamp: 1, receivedMs: null })).toBe(
      'opcua opc.tcp://plc ns=2;s=Tag'
    );
    expect(describeTrigger({ type: 'unknown', source: null, topic: null, payload: '{}', timestamp: null, receivedMs: null })).toBe('unknown trigger');
    expect(runSummary({ ...RUN, executionId: '', durationMs: null })).toContain('run — completed');
  });
});

describe('RunRecordCard — the image (design §7.2, the enforcement guard)', () => {
  it('loads the image through the fetcher and shows it', async () => {
    const fetchImageUrl = vi.fn().mockResolvedValue({ url: 'https://example.com/i.jpg', expires_in: 120, key: 'k' });
    render(<RunRecordCard event={tailEvent(RUN)} sessionId="sess-1" deviceId="thing-a" defaultExpanded fetchImageUrl={fetchImageUrl} />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('run-image-load'));
    });

    expect(fetchImageUrl).toHaveBeenCalledWith('sess-1', 'exec-0001-abcd', 'thing-a');
    expect((screen.getByTestId('run-image') as HTMLImageElement).src).toBe('https://example.com/i.jpg');
  });

  it('shows the 409 notice with the remedy while registry enforcement is off', async () => {
    const fetchImageUrl = vi.fn().mockRejectedValue(
      new DebugApiError('Debug images are refused while registry enforcement is off', 409, {
        error: 'Debug images are refused while registry enforcement is off',
        error_code: 'registry_enforcement_required',
        remedy: 'Turn on registry enforcement once the registry backfill is complete.',
      })
    );
    render(<RunRecordCard event={tailEvent(RUN)} sessionId="sess-1" deviceId="thing-a" defaultExpanded fetchImageUrl={fetchImageUrl} />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('run-image-load'));
    });

    const notice = screen.getByTestId('run-image-notice');
    expect(notice.textContent).toContain('Debug images are refused while registry enforcement is off');
    expect(notice.textContent).toContain('Turn on registry enforcement once the registry backfill is complete.');
    expect(screen.queryByTestId('run-image')).toBeNull();
  });

  it('says so when no image was uploaded (404) and keeps the button', async () => {
    const fetchImageUrl = vi
      .fn()
      .mockRejectedValue(new DebugApiError('none', 404, { error: 'none', error_code: 'image_not_found' }));
    render(<RunRecordCard event={tailEvent(RUN)} sessionId="sess-1" defaultExpanded fetchImageUrl={fetchImageUrl} />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('run-image-load'));
    });

    expect(screen.getByTestId('run-image-notice').textContent).toBe('No image was uploaded for this run.');
    expect(screen.getByTestId('run-image-load')).toBeInTheDocument();
  });

  it('disables the image button without a session and renders no image block without an image', () => {
    const { unmount } = render(<RunRecordCard event={tailEvent(RUN)} sessionId={null} defaultExpanded />);
    expect((screen.getByTestId('run-image-load') as HTMLButtonElement).disabled).toBe(true);
    unmount();

    render(<RunRecordCard event={tailEvent({ ...RUN, image: null })} sessionId="sess-1" defaultExpanded />);
    expect(screen.queryByTestId('run-image-load')).toBeNull();
  });
});

describe('LogLine — compact lines', () => {
  it('renders a log record with level, logger and message', () => {
    render(
      <LogLine
        event={tailEvent({ type: 'log', ts: TS, level: 'WARNING', logger: 'workflow_engine.trigger_runtime', message: 'gate closed', attribution: 'between_runs' })}
      />
    );
    const line = screen.getByTestId('debug-line-log');
    expect(line.textContent).toContain('WARNING workflow_engine.trigger_runtime — gate closed');
    expect(line.textContent).not.toContain('between_runs');
    expect(screen.queryByText('late')).toBeNull();
  });

  it('renders a drop record and a session record with its counters', () => {
    render(<LogLine event={tailEvent({ type: 'drop', ts: TS, nodeId: 'camera', reason: 'skipped_busy', count: 12 })} />);
    expect(screen.getByTestId('debug-line-drop').textContent).toContain('drop camera: skipped_busy ×12');

    render(
      <LogLine
        event={tailEvent(
          {
            type: 'session',
            ts: TS,
            state: 'active',
            reason: 'heartbeat',
            instance: '0123456789abcdef',
            agentVersion: '1.0.0',
            level: 'debug',
            counters: { records: 120, bytes: 40960, queueDrops: 0, publishFailures: 2 },
          },
          { late: true }
        )}
      />
    );
    const session = screen.getByTestId('debug-line-session');
    expect(session.textContent).toContain('session active (heartbeat) instance 01234567 agent 1.0.0 level debug records 120, bytes 40960, publishFailures 2');
    expect(screen.getByText('late')).toBeInTheDocument();
  });

  it('shows an unknown record type as its JSON without the stamps', () => {
    const event = tailEvent({ type: 'future', ts: TS, id: 's:i:1:0', writeTs: TS + 1, receivedMs: TS, detail: 'x' } as any);
    expect(lineText(event)).toBe(`future: ${JSON.stringify({ type: 'future', ts: TS, detail: 'x' })}`);
    expect(countersSummary(undefined)).toBe('');
    expect(countersSummary({ records: 0, bytes: 0, tapDrops: 0, truncations: 3 })).toBe('records 0, bytes 0, truncations 3');
  });
});
