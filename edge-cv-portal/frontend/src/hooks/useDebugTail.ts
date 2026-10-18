/**
 * The debug viewer's tail protocol and download helper (workflow-debug-mode
 * design §8 steps 1–4, §10.1; Requirements 6.3–6.6).
 *
 * The stream is ordered by the sink's write time, so the tail is a sliding
 * window over write time with `id` de-duplication, never a forward token
 * carried across polls:
 *
 * 1. The first poll asks `from = tailStart = now − 15 min, to = now` and
 *    drains the window through `next` (at most 3 calls of `limit=2000` per
 *    poll; a longer backlog continues on the next poll from the same `from`,
 *    shown as "catching up"). A 429 `log_read_throttled` ends the poll; the
 *    tail backs off to a 10 s interval while 429s continue and returns to
 *    3 s after two clean polls.
 * 2. Every later poll, 3 s apart while the tab is visible, asks
 *    `from = max(lastSeenWriteTs − 60 000, tailStart), to = now`, where
 *    `lastSeenWriteTs` is the largest `writeTs` the client has seen. The
 *    60 s overlap covers write-time skew between ingest containers and a
 *    batch written just after the previous poll's `to`; a write delayed by
 *    any amount lands at the end of the stream near `now`, so no event is
 *    missed.
 * 3. A bounded set of the last 10,000 `id`s drops anything shown before;
 *    an unseen event is inserted at its place by the record's own `ts`
 *    (then `id`), so a late record of an old run appears next to that run,
 *    flagged "late" when `writeTs − receivedMs` exceeds 60 s.
 * 4. `next` is used only inside a poll and by the download, which pages one
 *    fixed window at `limit=10000`, at most 2 calls per second, pausing
 *    `retry_after_ms` on a 429 and resuming with the same `next`, up to
 *    50,000 events assembled as NDJSON client-side. A 400 `invalid_token`
 *    makes either repeat the window without a token.
 *
 * Polling stops while the tab is hidden and resumes when it is visible
 * again. A fixed window (the tail switched off or an absolute range) is
 * drained the same way and then left alone.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiService, isDebugApiError } from '../services/api';
import { getErrorMessage } from '../utils/errorHandling';
import type {
  DebugEvent,
  DebugLogLevel,
  DebugLogQuery,
  DebugLogResponse,
  DebugRecordType,
} from '../pages/debug/types';

export const TAIL_POLL_MS = 3_000;
export const TAIL_BACKOFF_MS = 10_000;
export const TAIL_WINDOW_MS = 15 * 60_000;
export const TAIL_OVERLAP_MS = 60_000;
export const TAIL_PAGE_LIMIT = 2_000;
export const TAIL_MAX_CALLS_PER_POLL = 3;
export const TAIL_SEEN_IDS = 10_000;
export const LATE_THRESHOLD_MS = 60_000;
export const CLEAN_POLLS_TO_RECOVER = 2;
export const DOWNLOAD_PAGE_LIMIT = 10_000;
export const DOWNLOAD_MAX_EVENTS = 50_000;
export const DOWNLOAD_MIN_SPACING_MS = 500;
export const DOWNLOAD_RETRY_FALLBACK_MS = 1_000;

/** A shown event: the API's event plus the viewer's late flag and sort key. */
export interface TailEvent extends DebugEvent {
  late: boolean;
  /** The record's own `ts`, or the write time when the record carries none. */
  sortTs: number;
}

/** The server-side filters of the `/log` route the viewer exposes. */
export interface DebugTailFilters {
  execution_id?: string;
  node_id?: string;
  level?: DebugLogLevel;
  type?: DebugRecordType;
}

/**
 * The time window. A tail asks `to = now` at every poll from `from`
 * (`tailStart`; default `now − 15 min` when absent). A fixed window
 * (`tail: false`) is drained once between `from` and `to`.
 */
export interface DebugTailWindow {
  tail: boolean;
  from?: number;
  to?: number;
}

export type DebugLogFetcher = (usecaseId: string, query: DebugLogQuery) => Promise<DebugLogResponse>;

export interface UseDebugTailOptions {
  usecaseId: string;
  deviceId: string;
  workflowId: string;
  window: DebugTailWindow;
  filters?: DebugTailFilters;
  /** False pauses everything (nothing is fetched); default true. */
  enabled?: boolean;
  /** Injected for tests; defaults to `apiService.getDebugLog`. */
  fetchLog?: DebugLogFetcher;
}

export interface DebugTailState {
  events: TailEvent[];
  /** A poll is in flight. */
  loading: boolean;
  /** The last poll hit its call budget before the window was drained. */
  catchingUp: boolean;
  /** Backing off to the 10 s interval after a 429 (design §10.1). */
  rateLimited: boolean;
  pollIntervalMs: number;
  /** The last failure other than a 429 or a missing session. */
  error: string | null;
  /** The API answered 404 `no_debug_session` for the Pair. */
  noSession: boolean;
  lastPollAt: number | null;
  /** Completed polls since the last reset. */
  polls: number;
  /** Largest write time seen; the next poll starts 60 s before it. */
  lastSeenWriteTs: number | null;
  /** Drop everything shown and poll again from `tailStart`. */
  refresh: () => void;
}

/** True when the event was written long after IoT received it (a retry). */
export function isLateEvent(event: Pick<DebugEvent, 'writeTs' | 'receivedMs'>): boolean {
  return (
    typeof event.receivedMs === 'number' &&
    typeof event.writeTs === 'number' &&
    event.writeTs - event.receivedMs > LATE_THRESHOLD_MS
  );
}

/** The sort key of an event: the record's own `ts`, else its write time. */
export function sortKeyOf(event: Pick<DebugEvent, 'ts' | 'writeTs'>): number {
  return typeof event.ts === 'number' && Number.isFinite(event.ts) ? event.ts : event.writeTs;
}

function compareEvents(a: TailEvent, b: TailEvent): number {
  if (a.sortTs !== b.sortTs) return a.sortTs - b.sortTs;
  const idA = a.id ?? '';
  const idB = b.id ?? '';
  return idA < idB ? -1 : idA > idB ? 1 : 0;
}

/** Insert `event` at its place by (`ts`, `id`) in the sorted array (in place). */
function insertSorted(events: TailEvent[], event: TailEvent): void {
  let low = 0;
  let high = events.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (compareEvents(events[mid], event) <= 0) low = mid + 1;
    else high = mid;
  }
  events.splice(low, 0, event);
}

/** The shown-event bookkeeping of design §8 step 3, kept in a ref. */
export class TailBuffer {
  readonly seen = new Set<string>();
  readonly events: TailEvent[] = [];
  lastSeenWriteTs: number | null = null;

  constructor(private readonly maxIds: number = TAIL_SEEN_IDS) {}

  /** Adds the unseen events of a page; returns how many were new. */
  merge(page: readonly DebugEvent[]): number {
    let added = 0;
    for (const event of page) {
      if (typeof event.writeTs === 'number' && (this.lastSeenWriteTs === null || event.writeTs > this.lastSeenWriteTs)) {
        this.lastSeenWriteTs = event.writeTs;
      }
      const id = event.id ?? `${event.writeTs}:${JSON.stringify(event.record)}`;
      if (this.seen.has(id)) continue;
      this.seen.add(id);
      while (this.seen.size > this.maxIds) {
        const oldest = this.seen.values().next().value as string | undefined;
        if (oldest === undefined) break;
        this.seen.delete(oldest);
      }
      insertSorted(this.events, { ...event, late: isLateEvent(event), sortTs: sortKeyOf(event) });
      if (this.events.length > this.maxIds) this.events.shift();
      added += 1;
    }
    return added;
  }

  snapshot(): TailEvent[] {
    return [...this.events];
  }
}

function isHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

const INITIAL_STATE = {
  events: [] as TailEvent[],
  loading: false,
  catchingUp: false,
  rateLimited: false,
  pollIntervalMs: TAIL_POLL_MS,
  error: null as string | null,
  noSession: false,
  lastPollAt: null as number | null,
  polls: 0,
  lastSeenWriteTs: null as number | null,
};

export function useDebugTail(options: UseDebugTailOptions): DebugTailState {
  const { usecaseId, deviceId, workflowId, window: timeWindow, filters, enabled = true } = options;
  const fetchLog: DebugLogFetcher = options.fetchLog ?? ((uc, query) => apiService.getDebugLog(uc, query));
  const fetchRef = useRef(fetchLog);
  fetchRef.current = fetchLog;

  const [state, setState] = useState(INITIAL_STATE);
  const [generation, setGeneration] = useState(0);

  const filtersKey = JSON.stringify({
    execution_id: filters?.execution_id || undefined,
    node_id: filters?.node_id || undefined,
    level: filters?.level || undefined,
    type: filters?.type || undefined,
  });
  const resetKey = [
    usecaseId,
    deviceId,
    workflowId,
    timeWindow.tail ? 'tail' : 'fixed',
    timeWindow.from ?? 'auto',
    timeWindow.to ?? 'now',
    filtersKey,
    enabled ? 'on' : 'off',
    generation,
  ].join('|');

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight = false;
    const buffer = new TailBuffer();
    let rateLimited = false;
    let cleanPolls = 0;
    let polls = 0;
    const parsedFilters = JSON.parse(filtersKey) as DebugTailFilters;

    setState({ ...INITIAL_STATE, loading: enabled });
    if (!enabled) return undefined;

    const tailStart = timeWindow.from ?? Date.now() - TAIL_WINDOW_MS;

    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };
    const schedule = (delay: number) => {
      clearTimer();
      timer = setTimeout(() => {
        timer = null;
        void poll();
      }, delay);
    };

    const poll = async () => {
      if (!alive || inFlight || isHidden()) return;
      inFlight = true;
      setState((current) => ({ ...current, loading: true }));
      const now = Date.now();
      const from = buffer.lastSeenWriteTs === null ? tailStart : Math.max(buffer.lastSeenWriteTs - TAIL_OVERLAP_MS, tailStart);
      const to = timeWindow.tail ? now : (timeWindow.to ?? now);

      let token: string | undefined;
      let calls = 0;
      let throttled = false;
      let exhausted = false;
      let noSession = false;
      let failure: string | null = null;

      while (calls < TAIL_MAX_CALLS_PER_POLL) {
        calls += 1;
        try {
          const page = await fetchRef.current(usecaseId, {
            device_id: deviceId,
            workflow_id: workflowId,
            from,
            to,
            limit: TAIL_PAGE_LIMIT,
            ...(token ? { next: token } : {}),
            ...parsedFilters,
          });
          if (!alive) return;
          buffer.merge(page.events ?? []);
          if (!page.next) {
            exhausted = true;
            break;
          }
          token = page.next;
        } catch (err) {
          if (!alive) return;
          if (isDebugApiError(err)) {
            if (err.status === 429) {
              throttled = true;
              break;
            }
            if (err.status === 400 && err.errorCode === 'invalid_token' && token) {
              // The token was rejected: repeat the window without one (step 4).
              token = undefined;
              continue;
            }
            if (err.status === 404 && err.errorCode === 'no_debug_session') {
              noSession = true;
              break;
            }
          }
          failure = getErrorMessage(err, 'Failed to read the debug log');
          break;
        }
      }

      if (throttled) {
        rateLimited = true;
        cleanPolls = 0;
      } else if (rateLimited) {
        cleanPolls += 1;
        if (cleanPolls >= CLEAN_POLLS_TO_RECOVER) {
          rateLimited = false;
          cleanPolls = 0;
        }
      }
      polls += 1;
      const interval = rateLimited ? TAIL_BACKOFF_MS : TAIL_POLL_MS;
      inFlight = false;
      setState({
        events: buffer.snapshot(),
        loading: false,
        catchingUp: !exhausted && !throttled && !noSession && failure === null,
        rateLimited,
        pollIntervalMs: interval,
        error: failure,
        noSession,
        lastPollAt: now,
        polls,
        lastSeenWriteTs: buffer.lastSeenWriteTs,
      });

      const again = (timeWindow.tail || !exhausted) && !noSession;
      if (again && !isHidden()) schedule(interval);
    };

    const onVisibility = () => {
      if (isHidden()) {
        clearTimer();
      } else if (!inFlight && timer === null) {
        void poll();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    void poll();

    return () => {
      alive = false;
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  const refresh = useMemo(() => () => setGeneration((g) => g + 1), []);

  return { ...state, refresh };
}

export interface DownloadDebugLogOptions {
  usecaseId: string;
  deviceId: string;
  workflowId: string;
  from: number;
  to: number;
  filters?: DebugTailFilters;
  /** Defaults to 50,000 (design §8 step 4). */
  maxEvents?: number;
  fetchLog?: DebugLogFetcher;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

export interface DownloadDebugLogResult {
  /** One JSON event per line, trailing newline; empty when nothing matched. */
  ndjson: string;
  count: number;
  /** True when the window held more than `maxEvents` events. */
  truncated: boolean;
  /** Requests made, including those answered 429. */
  calls: number;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Pages one fixed `[from, to]` window at `limit=10000`, at most 2 calls per
 * second, pausing `retry_after_ms` on a 429 and resuming with the same
 * `next`, until the API answers `next: null` or the cap is reached, and
 * assembles the NDJSON client-side (design §8 step 4, §10.1).
 */
export async function downloadDebugLog(options: DownloadDebugLogOptions): Promise<DownloadDebugLogResult> {
  const fetchLog: DebugLogFetcher = options.fetchLog ?? ((uc, query) => apiService.getDebugLog(uc, query));
  const sleep = options.sleep ?? defaultSleep;
  const now = options.now ?? (() => Date.now());
  const maxEvents = options.maxEvents ?? DOWNLOAD_MAX_EVENTS;
  const filters: DebugTailFilters = {
    ...(options.filters?.execution_id ? { execution_id: options.filters.execution_id } : {}),
    ...(options.filters?.node_id ? { node_id: options.filters.node_id } : {}),
    ...(options.filters?.level ? { level: options.filters.level } : {}),
    ...(options.filters?.type ? { type: options.filters.type } : {}),
  };

  let token: string | undefined;
  let lines: string[] = [];
  let calls = 0;
  let truncated = false;
  let lastCallAt: number | null = null;

  for (;;) {
    if (lastCallAt !== null) {
      const wait = DOWNLOAD_MIN_SPACING_MS - (now() - lastCallAt);
      if (wait > 0) await sleep(wait);
    }
    lastCallAt = now();
    calls += 1;
    let page: DebugLogResponse;
    try {
      page = await fetchLog(options.usecaseId, {
        device_id: options.deviceId,
        workflow_id: options.workflowId,
        from: options.from,
        to: options.to,
        limit: DOWNLOAD_PAGE_LIMIT,
        ...(token ? { next: token } : {}),
        ...filters,
      });
    } catch (err) {
      if (isDebugApiError(err) && err.status === 429) {
        await sleep(err.retryAfterMs ?? DOWNLOAD_RETRY_FALLBACK_MS);
        continue; // the same window, the same `next`
      }
      if (isDebugApiError(err) && err.status === 400 && err.errorCode === 'invalid_token' && token) {
        token = undefined;
        lines = [];
        continue; // repeat the window without a token
      }
      throw err;
    }
    for (const event of page.events ?? []) {
      if (lines.length >= maxEvents) {
        truncated = true;
        break;
      }
      lines.push(JSON.stringify(event));
    }
    if (truncated || !page.next) break;
    token = page.next;
  }

  return {
    ndjson: lines.length ? `${lines.join('\n')}\n` : '',
    count: lines.length,
    truncated,
    calls,
  };
}
