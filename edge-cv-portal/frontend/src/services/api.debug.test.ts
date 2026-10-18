/**
 * Unit tests for the workflow debug mode API client methods
 * (workflow-debug-mode task 5.1, Requirements 3.1, 6.3).
 *
 * - `getDebugLog` serializes every query parameter of design §7.2 and omits
 *   the ones not given;
 * - a 429 `log_read_throttled` is returned as the typed `DebugApiError`
 *   carrying `retry_after_ms`, after exactly one request (never retried);
 * - the other refusals keep their `error_code` and `remedy`;
 * - the routes and methods of the session, image and console-link calls.
 *
 * `fetch` is stubbed as `api.refreshRetry.test.ts` does; Amplify's refresh is
 * mocked so a 401 could never reach it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchAuthSessionMock } = vi.hoisted(() => ({ fetchAuthSessionMock: vi.fn() }));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: fetchAuthSessionMock }));

import { DebugApiError, apiService, isDebugApiError } from './api';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** A fetch stub answering every call with the same planned response. */
function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(async () => jsonResponse(status, body));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof vi.fn>, call = 0): URL {
  return new URL(String(fetchMock.mock.calls[call][0]));
}

function requestInit(fetchMock: ReturnType<typeof vi.fn>, call = 0): RequestInit {
  return (fetchMock.mock.calls[call][1] ?? {}) as RequestInit;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.setItem('idToken', 'token-1');
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('getDebugLog query serialization (design §7.2)', () => {
  it('serializes every parameter when all are given', async () => {
    const fetchMock = stubFetch(200, { events: [], next: null });

    await apiService.getDebugLog('uc-1', {
      device_id: 'thing-1',
      workflow_id: 'wf-1',
      from: 1000,
      to: 2000,
      next: 'tok',
      limit: 2000,
      execution_id: 'exec-1',
      node_id: 'node-a',
      level: 'ERROR',
      type: 'run',
      ts_from: 900,
      ts_to: 1900,
    });

    const url = requestedUrl(fetchMock);
    expect(url.pathname.endsWith('/debug/usecases/uc-1/log')).toBe(true);
    expect(Object.fromEntries(url.searchParams.entries())).toEqual({
      device_id: 'thing-1',
      workflow_id: 'wf-1',
      from: '1000',
      to: '2000',
      next: 'tok',
      limit: '2000',
      execution_id: 'exec-1',
      node_id: 'node-a',
      level: 'ERROR',
      type: 'run',
      ts_from: '900',
      ts_to: '1900',
    });
    expect(requestInit(fetchMock).method ?? 'GET').toBe('GET');
  });

  it('sends only device_id and workflow_id when nothing else is given', async () => {
    const fetchMock = stubFetch(200, { events: [], next: null });

    await apiService.getDebugLog('uc-1', { device_id: 'thing-1', workflow_id: 'wf-1' });

    const url = requestedUrl(fetchMock);
    expect([...url.searchParams.keys()].sort()).toEqual(['device_id', 'workflow_id']);
  });

  it('omits empty strings and truncates fractional milliseconds', async () => {
    const fetchMock = stubFetch(200, { events: [], next: null });

    await apiService.getDebugLog('uc-1', {
      device_id: 'thing-1',
      workflow_id: 'wf-1',
      from: 1000.7,
      execution_id: '',
      next: '',
    });

    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get('from')).toBe('1000');
    expect(url.searchParams.has('execution_id')).toBe(false);
    expect(url.searchParams.has('next')).toBe(false);
  });

  it('URL-encodes the use case id and a device id with a colon', async () => {
    const fetchMock = stubFetch(200, { events: [], next: null });

    await apiService.getDebugLog('uc/1', { device_id: 'thing:one', workflow_id: 'wf-1' });

    const raw = String(requestedUrl(fetchMock));
    expect(raw).toContain('/debug/usecases/uc%2F1/log?');
    expect(new URL(raw).searchParams.get('device_id')).toBe('thing:one');
  });

  it('returns the events and the next token as answered', async () => {
    const answer = {
      events: [{ id: 's:i:1:0', writeTs: 5, receivedMs: 4, ts: 3, record: { type: 'log' } }],
      next: 'page-2',
    };
    stubFetch(200, answer);

    const result = await apiService.getDebugLog('uc-1', { device_id: 'd', workflow_id: 'w' });

    expect(result).toEqual(answer);
  });
});

describe('the 429 log_read_throttled answer (design §10.1)', () => {
  it('is returned as a DebugApiError carrying retry_after_ms after one request', async () => {
    const fetchMock = stubFetch(429, {
      error: 'Log reads are rate-limited; retry shortly',
      error_code: 'log_read_throttled',
      retry_after_ms: 750,
    });

    const error = await apiService
      .getDebugLog('uc-1', { device_id: 'd', workflow_id: 'w' })
      .catch((err) => err);

    expect(isDebugApiError(error)).toBe(true);
    expect(error).toBeInstanceOf(DebugApiError);
    expect((error as DebugApiError).status).toBe(429);
    expect((error as DebugApiError).errorCode).toBe('log_read_throttled');
    expect((error as DebugApiError).retryAfterMs).toBe(750);
    expect((error as DebugApiError).isRateLimited).toBe(true);
    expect((error as DebugApiError).message).toBe('Log reads are rate-limited; retry shortly');
    // Not retried by the client: exactly one request went out.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchAuthSessionMock).not.toHaveBeenCalled();
  });

  it('leaves retryAfterMs undefined when the body carries none', async () => {
    stubFetch(429, { error: 'throttled', error_code: 'log_read_throttled' });

    const error = await apiService
      .getDebugLog('uc-1', { device_id: 'd', workflow_id: 'w' })
      .catch((err) => err);

    expect((error as DebugApiError).retryAfterMs).toBeUndefined();
    expect((error as DebugApiError).isRateLimited).toBe(true);
  });
});

describe('other refusals keep their code and remedy', () => {
  it('surfaces a 400 invalid_token with its code', async () => {
    stubFetch(400, {
      error: 'The page token was rejected; repeat the window without it',
      error_code: 'invalid_token',
    });

    const error = await apiService
      .getDebugLog('uc-1', { device_id: 'd', workflow_id: 'w', next: 'bad' })
      .catch((err) => err);

    expect((error as DebugApiError).status).toBe(400);
    expect((error as DebugApiError).errorCode).toBe('invalid_token');
    expect((error as DebugApiError).isRateLimited).toBe(false);
  });

  it('surfaces a 409 debug_session_limit with remedy and details', async () => {
    stubFetch(409, {
      error: 'The use case already has 4 live sessions',
      error_code: 'debug_session_limit',
      remedy: 'Stop a session first',
      limit: 'usecase',
      active: 4,
      max: 4,
    });

    const error = await apiService
      .createDebugSession('uc-1', { workflow_id: 'wf-1', device_id: 'thing-1' })
      .catch((err) => err);

    expect((error as DebugApiError).status).toBe(409);
    expect((error as DebugApiError).errorCode).toBe('debug_session_limit');
    expect((error as DebugApiError).remedy).toBe('Stop a session first');
    expect((error as DebugApiError).body).toMatchObject({ limit: 'usecase', active: 4, max: 4 });
  });

  it('surfaces the Portal 403 envelope with its upper-case code', async () => {
    stubFetch(403, {
      error: 'Insufficient permissions',
      error_code: 'INSUFFICIENT_PERMISSIONS',
      required_permission: 'MANAGE_DEVICES',
    });

    const error = await apiService.stopDebugSession('s-1').catch((err) => err);

    expect((error as DebugApiError).status).toBe(403);
    expect((error as DebugApiError).errorCode).toBe('INSUFFICIENT_PERMISSIONS');
    expect((error as DebugApiError).body.required_permission).toBe('MANAGE_DEVICES');
  });

  it('passes a network failure through unchanged', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('network down');
      })
    );

    const error = await apiService.getDebugSession('s-1').catch((err) => err);

    expect(isDebugApiError(error)).toBe(false);
    expect((error as Error).message).toBe('network down');
  });
});

describe('session, image and console-link routes', () => {
  it('createDebugSession POSTs the body to the use case sessions route', async () => {
    const fetchMock = stubFetch(201, {
      session_id: 's-1',
      state: 'active',
      devices: [{ device_id: 'thing-1', shadow: 'ok' }],
      region_source: 'header',
      enabledAt: 1,
      expiresAt: 2,
      logGroup: '/dda/workflow-debug/uc-1',
    });

    const result = await apiService.createDebugSession('uc-1', {
      workflow_id: 'wf-1',
      device_id: 'thing-1',
      duration_minutes: 60,
      level: 'debug',
      images: false,
    });

    expect(result.session_id).toBe('s-1');
    expect(requestedUrl(fetchMock).pathname.endsWith('/debug/usecases/uc-1/sessions')).toBe(true);
    const init = requestInit(fetchMock);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({
      workflow_id: 'wf-1',
      device_id: 'thing-1',
      duration_minutes: 60,
      level: 'debug',
      images: false,
    });
  });

  it('listDebugSessions GETs the use case route with the optional state and next', async () => {
    const fetchMock = stubFetch(200, { sessions: [], next: null });

    await apiService.listDebugSessions('uc-1');
    await apiService.listDebugSessions('uc-1', { state: 'active', next: 'p2' });

    expect(requestedUrl(fetchMock, 0).pathname.endsWith('/debug/usecases/uc-1/sessions')).toBe(true);
    expect(requestedUrl(fetchMock, 0).search).toBe('');
    expect(Object.fromEntries(requestedUrl(fetchMock, 1).searchParams.entries())).toEqual({
      state: 'active',
      next: 'p2',
    });
  });

  it('getDebugSession GETs and stopDebugSession DELETEs the session route', async () => {
    const fetchMock = stubFetch(200, { session_id: 's-1', state: 'stopped', devices: [], stoppedAt: 9 });

    await apiService.getDebugSession('s-1');
    await apiService.stopDebugSession('s-1');

    expect(requestedUrl(fetchMock, 0).pathname.endsWith('/debug/sessions/s-1')).toBe(true);
    expect(requestInit(fetchMock, 0).method).toBeUndefined();
    expect(requestedUrl(fetchMock, 1).pathname.endsWith('/debug/sessions/s-1')).toBe(true);
    expect(requestInit(fetchMock, 1).method).toBe('DELETE');
  });

  it('getDebugImageUrl names the execution and the optional device', async () => {
    const fetchMock = stubFetch(200, { url: 'https://example.com/i.jpg', expires_in: 120, key: 'k' });

    await apiService.getDebugImageUrl('s-1', 'exec-1');
    await apiService.getDebugImageUrl('s-1', 'exec-1', 'thing-1');

    expect(requestedUrl(fetchMock, 0).pathname.endsWith('/debug/sessions/s-1/images/exec-1')).toBe(true);
    expect(requestedUrl(fetchMock, 0).search).toBe('');
    expect(requestedUrl(fetchMock, 1).searchParams.get('device_id')).toBe('thing-1');
  });

  it('getDebugConsoleLink GETs the console-link route with the optional device', async () => {
    const fetchMock = stubFetch(200, {
      url: 'https://example.com/console',
      region: 'us-east-1',
      logGroup: '/dda/workflow-debug/uc-1',
      logStream: 'thing-1/wf-1',
      note: 'write time',
    });

    const result = await apiService.getDebugConsoleLink('s-1', 'thing-1');

    expect(result.url).toBe('https://example.com/console');
    expect(requestedUrl(fetchMock).pathname.endsWith('/debug/sessions/s-1/console-link')).toBe(true);
    expect(requestedUrl(fetchMock).searchParams.get('device_id')).toBe('thing-1');
  });

  it('a 409 registry_enforcement_required on the image route keeps its remedy', async () => {
    stubFetch(409, {
      error: 'Debug images are refused while registry enforcement is off',
      error_code: 'registry_enforcement_required',
      remedy: 'Turn on registry enforcement once the registry backfill is complete.',
    });

    const error = await apiService.getDebugImageUrl('s-1', 'exec-1').catch((err) => err);

    expect((error as DebugApiError).status).toBe(409);
    expect((error as DebugApiError).errorCode).toBe('registry_enforcement_required');
    expect((error as DebugApiError).remedy).toContain('registry enforcement');
  });
});
