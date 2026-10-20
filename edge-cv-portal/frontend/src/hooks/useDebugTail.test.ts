/**
 * Unit tests for the tail protocol and the download helper
 * (workflow-debug-mode task 5.3, design §8 steps 1–4 and §10.1;
 * Requirements 6.3–6.6).
 *
 * Fake timers and a mocked `/log` fetcher drive every behaviour:
 * - start on mount and stop while the tab is hidden, resume when visible;
 * - the first window `[now − 15 min, now]`;
 * - the overlapping window `from = max(lastSeenWriteTs − 60 s, tailStart)`;
 * - draining through `next` with `limit=2000` and the 3-call cap
 *   ("catching up");
 * - no token carried across polls;
 * - `id` de-duplication across the overlap;
 * - a late-written event with an old `ts` appears once, at its place, with
 *   the late flag;
 * - the `invalid_token` repeat without a token;
 * - the 429 back-off to 10 s and the return to 3 s after two clean polls;
 * - the download at `limit=10000`, at most 2 calls per second, the 429
 *   pause, the 50,000 cap and the NDJSON assembly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));

import { DebugApiError } from '../services/api';
import type { DebugEvent, DebugLogQuery, DebugLogResponse } from '../pages/debug/types';
import {
  CLEAN_POLLS_TO_RECOVER,
  DOWNLOAD_MAX_EVENTS,
  DOWNLOAD_PAGE_LIMIT,
  TAIL_BACKOFF_MS,
  TAIL_MAX_CALLS_PER_POLL,
  TAIL_OVERLAP_MS,
  TAIL_PAGE_LIMIT,
  TAIL_POLL_MS,
  TAIL_WINDOW_MS,
  TailBuffer,
  downloadDebugLog,
  isLateEvent,
  sortKeyOf,
  useDebugTail,
} from './useDebugTail';

const NOW = 1_792_075_062_000;
const PAIR = { usecaseId: 'uc-1', deviceId: 'thing-a', workflowId: 'wf-1' };

function event(id: string, ts: number, writeTs: number, receivedMs: number = writeTs - 50): DebugEvent {
  return { id, writeTs, receivedMs, ts, record: { type: 'log', ts, level: 'INFO', logger: 'x', message: id } };
}

function page(events: DebugEvent[], next: string | null = null): DebugLogResponse {
  return { events, next };
}

function throttle(retryAfterMs = 750): DebugApiError {
  return new DebugApiError('rate-limited', 429, {
    error: 'rate-limited',
    error_code: 'log_read_throttled',
    retry_after_ms: retryAfterMs,
  });
}

function invalidToken(): DebugApiError {
  return new DebugApiError('bad token', 400, { error: 'bad token', error_code: 'invalid_token' });
}

type Fetcher = (usecaseId: string, query: DebugLogQuery) => Promise<DebugLogResponse>;

/** A fetcher answering from a queue of planned answers (a thrown value rejects). */
function plannedFetcher(plan: Array<DebugLogResponse | Error>) {
  const queries: DebugLogQuery[] = [];
  const fetchLog = vi.fn<Fetcher>(async (_uc, query) => {
    queries.push(query);
    const planned = plan.shift();
    if (planned === undefined) return page([]);
    if (planned instanceof Error) throw planned;
    return planned;
  });
  return { fetchLog, queries };
}

/** Advance the fake clock, letting the poll's promises settle. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

function mountTail(fetchLog: Fetcher, extra: Partial<Parameters<typeof useDebugTail>[0]> = {}) {
  return renderHook(() => useDebugTail({ ...PAIR, window: { tail: true }, fetchLog, ...extra }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  setVisibility('visible');
});

afterEach(() => {
  vi.useRealTimers();
  setVisibility('visible');
});

describe('useDebugTail — polling and the window arithmetic (design §8 steps 1–2)', () => {
  it('polls at mount with the first window [now − 15 min, now] and limit=2000, without a token', async () => {
    const { fetchLog, queries } = plannedFetcher([page([event('a', NOW - 1000, NOW - 500)])]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(1);
    expect(fetchLog.mock.calls[0][0]).toBe('uc-1');
    expect(queries[0]).toMatchObject({
      device_id: 'thing-a',
      workflow_id: 'wf-1',
      from: NOW - TAIL_WINDOW_MS,
      to: NOW,
      limit: TAIL_PAGE_LIMIT,
    });
    expect(queries[0].next).toBeUndefined();
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a']);
    expect(hook.result.current.pollIntervalMs).toBe(TAIL_POLL_MS);
    expect(hook.result.current.polls).toBe(1);
    hook.unmount();
  });

  it('honours a given tailStart and the server-side filters', async () => {
    const { fetchLog, queries } = plannedFetcher([page([])]);
    const hook = mountTail(fetchLog, {
      window: { tail: true, from: NOW - 60_000 },
      filters: { execution_id: 'exec-1', level: 'ERROR', node_id: '', type: 'run' },
    });
    await advance(0);

    expect(queries[0]).toMatchObject({ from: NOW - 60_000, to: NOW, execution_id: 'exec-1', level: 'ERROR', type: 'run' });
    expect(queries[0].node_id).toBeUndefined();
    hook.unmount();
  });

  it('polls every 3 s with from = max(lastSeenWriteTs − 60 s, tailStart) and to = now', async () => {
    const { fetchLog, queries } = plannedFetcher([
      page([event('a', NOW - 1000, NOW - 500)]),
      page([event('b', NOW + 2000, NOW + 2500)]),
      page([]),
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(fetchLog).toHaveBeenCalledTimes(1);

    await advance(TAIL_POLL_MS - 1);
    expect(fetchLog).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    // lastSeenWriteTs = NOW − 500 is less than 60 s after tailStart? No:
    // tailStart = NOW − 15 min, so from = (NOW − 500) − 60 000.
    expect(queries[1]).toMatchObject({ from: NOW - 500 - TAIL_OVERLAP_MS, to: NOW + TAIL_POLL_MS });

    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(3);
    expect(queries[2]).toMatchObject({ from: NOW + 2500 - TAIL_OVERLAP_MS, to: NOW + 2 * TAIL_POLL_MS });
    expect(hook.result.current.lastSeenWriteTs).toBe(NOW + 2500);
    hook.unmount();
  });

  it('never moves from before tailStart', async () => {
    const tailStart = NOW - 30_000;
    const { fetchLog, queries } = plannedFetcher([page([event('a', NOW - 29_000, NOW - 29_000)]), page([])]);
    const hook = mountTail(fetchLog, { window: { tail: true, from: tailStart } });
    await advance(0);
    await advance(TAIL_POLL_MS);

    // NOW − 29 000 − 60 000 is before tailStart, so from stays at tailStart.
    expect(queries[1].from).toBe(tailStart);
    hook.unmount();
  });

  it('stops polling on unmount', async () => {
    const { fetchLog } = plannedFetcher([page([]), page([]), page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    hook.unmount();
    await advance(5 * TAIL_POLL_MS);

    expect(fetchLog).toHaveBeenCalledTimes(1);
  });

  it('fetches nothing while disabled', async () => {
    const { fetchLog } = plannedFetcher([page([])]);
    const hook = mountTail(fetchLog, { enabled: false });
    await advance(3 * TAIL_POLL_MS);

    expect(fetchLog).not.toHaveBeenCalled();
    expect(hook.result.current.loading).toBe(false);
    hook.unmount();
  });
});

describe('useDebugTail — visibility (design §8: "stops when the tab is hidden")', () => {
  it('stops while hidden and resumes with a poll when visible again', async () => {
    const { fetchLog } = plannedFetcher([page([]), page([]), page([]), page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(fetchLog).toHaveBeenCalledTimes(1);

    await act(async () => {
      setVisibility('hidden');
    });
    await advance(10 * TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(1);

    await act(async () => {
      setVisibility('visible');
    });
    await advance(0);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(3);
    hook.unmount();
  });
});

describe('useDebugTail — draining a window (design §8 step 1)', () => {
  it('follows next within one poll at limit=2000 and carries no token into the next poll', async () => {
    const { fetchLog, queries } = plannedFetcher([
      page([event('a', NOW - 3000, NOW - 2900)], 'tok-1'),
      page([event('b', NOW - 2000, NOW - 1900)], null),
      page([event('c', NOW + 1000, NOW + 1100)], null),
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(queries[0].next).toBeUndefined();
    expect(queries[1].next).toBe('tok-1');
    expect(queries[1]).toMatchObject({ from: queries[0].from, to: queries[0].to, limit: TAIL_PAGE_LIMIT });
    expect(hook.result.current.catchingUp).toBe(false);

    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(3);
    // The next poll starts a new window without the token of the last page.
    expect(queries[2].next).toBeUndefined();
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b', 'c']);
    hook.unmount();
  });

  it('stops at 3 calls per poll, reports catching up and continues on the next poll from the same from', async () => {
    const { fetchLog, queries } = plannedFetcher([
      page([event('a', NOW - 9000, NOW - 8000)], 'tok-1'),
      page([event('b', NOW - 8000, NOW - 7000)], 'tok-2'),
      page([event('c', NOW - 7000, NOW - 6000)], 'tok-3'),
      page([event('d', NOW - 6000, NOW - 5000)], null),
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(TAIL_MAX_CALLS_PER_POLL);
    expect(hook.result.current.catchingUp).toBe(true);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b', 'c']);

    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(4);
    expect(queries[3].next).toBeUndefined();
    // from = max(lastSeenWriteTs − 60 s, tailStart) with lastSeenWriteTs = NOW − 6000.
    expect(queries[3].from).toBe(NOW - 6000 - TAIL_OVERLAP_MS);
    expect(hook.result.current.catchingUp).toBe(false);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b', 'c', 'd']);
    hook.unmount();
  });

  it('drains a fixed window once and then stops', async () => {
    const { fetchLog, queries } = plannedFetcher([page([event('a', NOW - 5000, NOW - 4000)], 'tok'), page([], null), page([])]);
    const hook = mountTail(fetchLog, { window: { tail: false, from: NOW - 10_000, to: NOW - 1000 } });
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(queries[0]).toMatchObject({ from: NOW - 10_000, to: NOW - 1000 });
    await advance(10 * TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a']);
    hook.unmount();
  });
});

describe('useDebugTail — de-duplication, ordering and the late flag (design §8 step 3)', () => {
  it('shows an event re-read in the overlap exactly once', async () => {
    const a = event('a', NOW - 1000, NOW - 900);
    const { fetchLog } = plannedFetcher([page([a]), page([a, event('b', NOW + 1000, NOW + 1100)]), page([a])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    await advance(TAIL_POLL_MS);
    await advance(TAIL_POLL_MS);

    expect(fetchLog).toHaveBeenCalledTimes(3);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b']);
    hook.unmount();
  });

  it('inserts a late-written event with an old ts once, at its place in time, with the late flag', async () => {
    const a = event('a', NOW - 300_000, NOW - 290_000); // ts 5 min ago
    const c = event('c', NOW - 100_000, NOW - 90_000); // ts 100 s ago
    // b's record is from between a and c, received then, but written 2 min later (a retried invocation).
    const b: DebugEvent = {
      id: 'b',
      ts: NOW - 200_000,
      receivedMs: NOW - 199_000,
      writeTs: NOW + TAIL_POLL_MS - 10,
      record: { type: 'log', ts: NOW - 200_000, level: 'INFO', logger: 'x', message: 'b' },
    };
    const { fetchLog } = plannedFetcher([page([a, c]), page([c, b]), page([b, c])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'c']);

    await advance(TAIL_POLL_MS);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b', 'c']);
    const late = hook.result.current.events.find((e) => e.id === 'b')!;
    expect(late.late).toBe(true);
    expect(hook.result.current.events.filter((e) => e.id !== 'b').every((e) => !e.late)).toBe(true);

    await advance(TAIL_POLL_MS);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b', 'c']);
    expect(hook.result.current.events.filter((e) => e.id === 'b')).toHaveLength(1);
    hook.unmount();
  });

  it('orders by ts then id and sorts a record without ts by its write time', async () => {
    const raw: DebugEvent = { id: 'r', ts: null, receivedMs: null, writeTs: NOW - 1500, record: { type: 'raw', message: 'x' } };
    const { fetchLog } = plannedFetcher([
      page([event('z', NOW - 1000, NOW - 100), event('y', NOW - 1000, NOW - 100), raw, event('a', NOW - 2000, NOW - 100)]),
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'r', 'y', 'z']);
    expect(sortKeyOf(raw)).toBe(NOW - 1500);
    hook.unmount();
  });
});

describe('useDebugTail — invalid_token (design §8 step 4)', () => {
  it('repeats the window without a token when the API rejects the token', async () => {
    const { fetchLog, queries } = plannedFetcher([
      page([event('a', NOW - 3000, NOW - 2900)], 'tok-1'),
      invalidToken(),
      page([event('a', NOW - 3000, NOW - 2900), event('b', NOW - 2000, NOW - 1900)], null),
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(3);
    expect(queries[1].next).toBe('tok-1');
    expect(queries[2].next).toBeUndefined();
    expect(queries[2]).toMatchObject({ from: queries[0].from, to: queries[0].to });
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a', 'b']);
    expect(hook.result.current.error).toBeNull();
    hook.unmount();
  });
});

describe('useDebugTail — the 429 back-off (design §10.1)', () => {
  it('moves the poll to 10 s on a 429 and back to 3 s after two clean polls, exposing the notice state', async () => {
    const { fetchLog } = plannedFetcher([
      page([]), // poll 1 clean
      throttle(), // poll 2: 429
      page([]), // poll 3 clean (1 of 2)
      page([]), // poll 4 clean (2 of 2) -> back to 3 s
      page([]), // poll 5
    ]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(hook.result.current.rateLimited).toBe(false);
    expect(hook.result.current.pollIntervalMs).toBe(TAIL_POLL_MS);

    await advance(TAIL_POLL_MS); // poll 2 -> 429
    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(hook.result.current.rateLimited).toBe(true);
    expect(hook.result.current.pollIntervalMs).toBe(TAIL_BACKOFF_MS);
    expect(hook.result.current.error).toBeNull();

    // Not at 3 s any more...
    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    // ...but at 10 s (poll 3, clean 1 of 2): still backing off.
    await advance(TAIL_BACKOFF_MS - TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(3);
    expect(hook.result.current.rateLimited).toBe(true);
    expect(hook.result.current.pollIntervalMs).toBe(TAIL_BACKOFF_MS);

    await advance(TAIL_BACKOFF_MS); // poll 4, clean 2 of 2 -> recovered
    expect(fetchLog).toHaveBeenCalledTimes(4);
    expect(hook.result.current.rateLimited).toBe(false);
    expect(hook.result.current.pollIntervalMs).toBe(TAIL_POLL_MS);

    await advance(TAIL_POLL_MS); // poll 5 after 3 s again
    expect(fetchLog).toHaveBeenCalledTimes(5);
    expect(CLEAN_POLLS_TO_RECOVER).toBe(2);
    hook.unmount();
  });

  it('a 429 ends the poll without a further call and keeps the events shown so far', async () => {
    const { fetchLog } = plannedFetcher([page([event('a', NOW - 1000, NOW - 900)], 'tok'), throttle(), page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);

    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(hook.result.current.events.map((e) => e.id)).toEqual(['a']);
    expect(hook.result.current.rateLimited).toBe(true);
    expect(hook.result.current.catchingUp).toBe(false);
    hook.unmount();
  });
});

describe('useDebugTail — other answers', () => {
  it('reports a missing session and stops polling', async () => {
    const missing = new DebugApiError('none', 404, { error: 'none', error_code: 'no_debug_session' });
    const { fetchLog } = plannedFetcher([missing, page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    await advance(3 * TAIL_POLL_MS);

    expect(fetchLog).toHaveBeenCalledTimes(1);
    expect(hook.result.current.noSession).toBe(true);
    hook.unmount();
  });

  it('exposes another failure and keeps polling', async () => {
    const { fetchLog } = plannedFetcher([new Error('network down'), page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(hook.result.current.error).toBe('network down');

    await advance(TAIL_POLL_MS);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(hook.result.current.error).toBeNull();
    hook.unmount();
  });

  it('refresh() drops the shown events and polls again from tailStart', async () => {
    const { fetchLog, queries } = plannedFetcher([page([event('a', NOW - 1000, NOW - 900)]), page([])]);
    const hook = mountTail(fetchLog);
    await advance(0);
    expect(hook.result.current.events).toHaveLength(1);

    await act(async () => {
      hook.result.current.refresh();
    });
    await advance(0);
    expect(fetchLog).toHaveBeenCalledTimes(2);
    expect(queries[1]).toMatchObject({ from: NOW - TAIL_WINDOW_MS, to: NOW });
    expect(hook.result.current.events).toHaveLength(0);
    hook.unmount();
  });
});

describe('TailBuffer and isLateEvent', () => {
  it('bounds the seen-id set to the last 10,000 ids', () => {
    const buffer = new TailBuffer(3);
    buffer.merge([event('a', 1, 1), event('b', 2, 2), event('c', 3, 3), event('d', 4, 4)]);
    expect(buffer.seen.has('a')).toBe(false);
    expect([...buffer.seen]).toEqual(['b', 'c', 'd']);
    expect(buffer.events.map((e) => e.id)).toEqual(['b', 'c', 'd']);
    // 'a' fell out of the set, so it could be shown again; the real bound is 10,000.
    expect(buffer.merge([event('a', 1, 1)])).toBe(1);
  });

  it('flags only a write more than 60 s after receipt as late', () => {
    expect(isLateEvent({ writeTs: 100_000, receivedMs: 39_999 })).toBe(true);
    expect(isLateEvent({ writeTs: 100_000, receivedMs: 40_000 })).toBe(false);
    expect(isLateEvent({ writeTs: 100_000, receivedMs: null })).toBe(false);
  });
});

describe('downloadDebugLog (design §8 step 4, §10.1)', () => {
  function bigPages(total: number, size: number) {
    const pages: DebugLogResponse[] = [];
    for (let start = 0; start < total; start += size) {
      const events: DebugEvent[] = [];
      for (let i = start; i < Math.min(start + size, total); i += 1) {
        events.push({ id: `e${i}`, writeTs: i, receivedMs: i, ts: i, record: { type: 'log', ts: i, level: 'INFO', logger: 'x', message: '' } });
      }
      const last = start + size >= total;
      pages.push(page(events, last ? null : `tok-${start + size}`));
    }
    return pages;
  }

  async function run(options: Parameters<typeof downloadDebugLog>[0]) {
    const promise = downloadDebugLog(options);
    await vi.runAllTimersAsync();
    return promise;
  }

  it('pages one window at limit=10000, at most 2 calls per second: 50,000 events in 5 calls over at least 2 s', async () => {
    const { fetchLog, queries } = plannedFetcher(bigPages(50_000, DOWNLOAD_PAGE_LIMIT));
    const callTimes: number[] = [];
    const timed: Fetcher = (uc, q) => {
      callTimes.push(Date.now());
      return fetchLog(uc, q);
    };
    const started = Date.now();

    const result = await run({ ...PAIR, from: NOW - 60_000, to: NOW, fetchLog: timed });

    expect(result.calls).toBe(5);
    expect(result.count).toBe(50_000);
    expect(result.truncated).toBe(false);
    expect(Date.now() - started).toBeGreaterThanOrEqual(2000);
    for (let i = 1; i < callTimes.length; i += 1) {
      expect(callTimes[i] - callTimes[i - 1]).toBeGreaterThanOrEqual(500);
    }
    expect(queries.every((q) => q.limit === DOWNLOAD_PAGE_LIMIT)).toBe(true);
    expect(queries.every((q) => q.from === NOW - 60_000 && q.to === NOW)).toBe(true);
    expect(queries[0].next).toBeUndefined();
    expect(queries.slice(1).map((q) => q.next)).toEqual(['tok-10000', 'tok-20000', 'tok-30000', 'tok-40000']);
    expect(result.ndjson.split('\n').filter(Boolean)).toHaveLength(50_000);
  });

  it('stops at 50,000 events and marks the result truncated', async () => {
    const { fetchLog } = plannedFetcher(bigPages(50_001, DOWNLOAD_PAGE_LIMIT));

    const result = await run({ ...PAIR, from: NOW - 60_000, to: NOW, fetchLog });

    expect(result.count).toBe(DOWNLOAD_MAX_EVENTS);
    expect(result.truncated).toBe(true);
    expect(result.calls).toBe(6);
  });

  it('pauses retry_after_ms on a 429 and resumes with the same next', async () => {
    const { fetchLog, queries } = plannedFetcher([
      page([event('a', 1, 1)], 'tok-1'),
      throttle(1500),
      page([event('b', 2, 2)], null),
    ]);
    const callTimes: number[] = [];
    const timed: Fetcher = (uc, q) => {
      callTimes.push(Date.now());
      return fetchLog(uc, q);
    };

    const result = await run({ ...PAIR, from: 0, to: 10, fetchLog: timed });

    expect(result.calls).toBe(3);
    expect(result.count).toBe(2);
    expect(queries[1].next).toBe('tok-1');
    expect(queries[2].next).toBe('tok-1');
    expect(callTimes[2] - callTimes[1]).toBeGreaterThanOrEqual(1500);
  });

  it('repeats the window without a token on invalid_token and assembles NDJSON client-side', async () => {
    const a = event('a', 1, 1);
    const b = event('b', 2, 2);
    const { fetchLog, queries } = plannedFetcher([page([a], 'tok-1'), invalidToken(), page([a, b], null)]);

    const result = await run({ ...PAIR, from: 0, to: 10, fetchLog, filters: { type: 'log', execution_id: '' } });

    expect(queries[2].next).toBeUndefined();
    expect(queries[0].type).toBe('log');
    expect(queries[0].execution_id).toBeUndefined();
    expect(result.count).toBe(2);
    expect(result.ndjson).toBe(`${JSON.stringify(a)}\n${JSON.stringify(b)}\n`);
  });

  it('returns an empty document for an empty window and rethrows other failures', async () => {
    const empty = await run({ ...PAIR, from: 0, to: 10, fetchLog: plannedFetcher([page([])]).fetchLog });
    expect(empty).toEqual({ ndjson: '', count: 0, truncated: false, calls: 1 });

    const failing = plannedFetcher([new Error('boom')]).fetchLog;
    await expect(downloadDebugLog({ ...PAIR, from: 0, to: 10, fetchLog: failing })).rejects.toThrow('boom');
  });
});
