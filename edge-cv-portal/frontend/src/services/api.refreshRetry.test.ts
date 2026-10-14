/**
 * Unit tests for the Silent_Refresh + retry-once 401 path shared by the
 * portal's API clients (spec: portal-session-expiry-return-to-page, task 5.3).
 *
 * Example-based counterpart to `api.refreshRetry.property.test.ts` (Property
 * 4). Covers what the task lists: refresh success → no redirect, one retry and
 * the retried response returned; refresh failure → redirect with the location
 * saved; two concurrent 401s → one refresh; a retried request that 401s again →
 * redirect and no second retry; the loading-bar accounting balanced; and the
 * Node_Designer client taking the very same path (Requirement 1.3).
 *
 * Observables (design Decision 4, Requirement 6.1): `fetchAuthSession` is
 * mocked, `fetch` is stubbed, the redirect is watched through the
 * `sessionRedirect.navigateTo` seam — jsdom cannot perform a real
 * `window.location.href` navigation — and the document location is moved with
 * `history.replaceState`, which jsdom does support, so the default-argument
 * paths stay honest.
 *
 * Concurrency is made deterministic rather than left to microtask ordering: the
 * mocked `fetchAuthSession` returns a gate promise that the test resolves only
 * once every first attempt has already observed its 401, which is exactly the
 * "several requests 401 at once" situation Requirement 5.3 describes.
 *
 * Validates: Requirements 1.1, 1.3, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { fetchAuthSessionMock } = vi.hoisted(() => ({ fetchAuthSessionMock: vi.fn() }));

// `services/api.ts` imports `fetchAuthSession` directly (design Decision 6:
// `ApiService` is not a component, so it cannot reach into React context).
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: fetchAuthSessionMock }));

import { ApiError, apiService } from './api';
import { nodeDesignerApi } from '../pages/node-designer/api';
import { getActiveCount } from './loadingBus';
import { RETURN_TO_KEY, setNavigateTo } from './sessionRedirect';

// --------------------------------------------------------------- fixtures

/** The deep page the user was on when the stale token was rejected. */
const DEEP_LOCATION = '/devices/dev-1?tab=cameras#latest';

/** One planned attempt: a status, and optionally the exact body to return. */
type Attempt = number | { status: number; body: unknown };

/** endpoint fragment -> the status of each successive attempt on it. */
type Plan = Record<string, Attempt[]>;

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/**
 * A `fetch` stub driven by a per-endpoint plan.
 *
 * Attempts are counted per endpoint rather than globally, so a test never
 * depends on the order in which concurrent requests happen to be served.
 * Running past the end of an endpoint's plan is a test failure, which is how
 * "no second retry" is enforced.
 */
function planFetch(plan: Plan) {
  const attempts: Record<string, number> = {};
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const key = Object.keys(plan).find((fragment) => url.includes(fragment));
    if (!key) throw new Error(`unexpected fetch: ${url}`);
    const index = attempts[key] ?? 0;
    attempts[key] = index + 1;
    const planned = plan[key][index];
    if (planned === undefined) {
      throw new Error(
        `fetch attempt ${index + 1} on ${key} exceeds the plan (${plan[key].length} attempt(s))`
      );
    }
    void init;
    const status = typeof planned === 'number' ? planned : planned.status;
    const body =
      typeof planned === 'number'
        ? status >= 200 && status < 300
          ? { ok: true, url, attempt: index + 1 }
          : { error: `HTTP ${status}` }
        : planned.body;
    return jsonResponse(status, body);
  });
}

/** A refreshable Amplify session yielding `token`. */
function sessionWith(token: string) {
  return { tokens: { idToken: { toString: () => token } } };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Let the microtask queue (and one macrotask) drain. */
function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** Wait until `mock` has been called at least `count` times. */
async function waitForCalls(mock: { mock: { calls: unknown[] } }, count: number): Promise<void> {
  for (let i = 0; i < 200 && mock.mock.calls.length < count; i += 1) {
    await tick();
  }
  expect(mock.mock.calls.length).toBeGreaterThanOrEqual(count);
}

/** Move the document location without navigating (jsdom supports this). */
function atLocation(path: string): void {
  window.history.replaceState({}, '', path);
}

/** The `Authorization` header of the nth `fetch` call. */
function authHeaderOf(fetchMock: ReturnType<typeof planFetch>, call: number): string | undefined {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit | undefined;
  return (init?.headers as Record<string, string> | undefined)?.['Authorization'];
}

let navigations: string[] = [];
let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.localStorage.setItem('idToken', 'stale-token');
  atLocation(DEEP_LOCATION);
  navigations = [];
  setNavigateTo((path) => {
    navigations.push(path);
  });
  // `abandonSession` logs the expired token before redirecting; keep the suite
  // output clean without losing the ability to assert on it.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  setNavigateTo(null);
  consoleError.mockRestore();
  vi.unstubAllGlobals();
  atLocation('/');
});

// ------------------------------------------------- refresh succeeds (5.1, 5.6)

describe('401 with a refreshable session (Requirements 5.1, 5.6)', () => {
  it('refreshes, retries once and returns the retried response without redirecting', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    const fetchMock = planFetch({ '/usecases/uc-1': [401, 200] });
    vi.stubGlobal('fetch', fetchMock);

    const result = await apiService.getUseCase('uc-1');

    // The caller sees the retried response, not an error (Requirement 5.6).
    expect(result).toMatchObject({ ok: true, attempt: 2 });
    // Exactly one refresh and exactly one retry (Requirements 5.1, 5.4).
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    expect(fetchAuthSessionMock).toHaveBeenCalledWith({ forceRefresh: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // The user is never sent to the login screen (Requirement 5.1).
    expect(navigations).toEqual([]);
    // ... and nothing is remembered, because nothing was interrupted.
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });

  it('retries with the refreshed bearer token, not the stale one', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    const fetchMock = planFetch({ '/usecases/uc-1': [401, 200] });
    vi.stubGlobal('fetch', fetchMock);

    await apiService.getUseCase('uc-1');

    expect(authHeaderOf(fetchMock, 0)).toBe('Bearer stale-token');
    expect(authHeaderOf(fetchMock, 1)).toBe('Bearer fresh-token');
    // Same URL and method: it is a re-issue of the original request.
    expect(String(fetchMock.mock.calls[1][0])).toBe(String(fetchMock.mock.calls[0][0]));
  });

  it('re-mirrors the refreshed token into localStorage', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401, 200] }));

    await apiService.getUseCase('uc-1');

    // The mirror contract `AuthContext` also writes (design Decision 6).
    expect(window.localStorage.getItem('idToken')).toBe('fresh-token');
  });

  it('preserves the request method and body across the retry', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    const fetchMock = planFetch({ '/usecases': [401, 200] });
    vi.stubGlobal('fetch', fetchMock);

    await apiService.createUseCase({ name: 'uc' } as never);

    const first = fetchMock.mock.calls[0][1] as RequestInit;
    const retry = fetchMock.mock.calls[1][1] as RequestInit;
    expect(retry.method).toBe(first.method);
    expect(retry.body).toBe(first.body);
  });
});

// -------------------------------------------------- refresh fails (5.2, 1.1)

describe('401 with a dead session (Requirements 5.2, 1.1)', () => {
  it('redirects to /login with the attempted location saved', async () => {
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    const fetchMock = planFetch({ '/usecases/uc-1': [401] });
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiService.getUseCase('uc-1')).rejects.toBeInstanceOf(ApiError);

    // No retry was attempted, since there was no fresh token to retry with.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(navigations).toEqual(['/login']);
    // Where the user was, recorded as pathname + search + hash (Req 1.1).
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe(DEEP_LOCATION);
    // The stale mirror is dropped, as it was before this feature.
    expect(window.localStorage.getItem('idToken')).toBeNull();
    expect(consoleError).toHaveBeenCalled();
  });

  it('surfaces the 401 to the caller as an ApiError carrying the status', async () => {
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401] }));

    const error = await apiService.getUseCase('uc-1').catch((err) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
  });

  it('treats a session without an ID token as a failed refresh', async () => {
    fetchAuthSessionMock.mockResolvedValue({ tokens: {} });
    const fetchMock = planFetch({ '/usecases/uc-1': [401] });
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiService.getUseCase('uc-1')).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(navigations).toEqual(['/login']);
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe(DEEP_LOCATION);
  });

  it('keeps the first (deepest) location when several 401s abandon the session', async () => {
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401], '/usecases/uc-2': [401] }));

    await Promise.allSettled([apiService.getUseCase('uc-1'), apiService.getUseCase('uc-2')]);

    // First-write-wins (design Decision 2): one value, the one the user cared
    // about, even though two 401s recorded it.
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe(DEEP_LOCATION);
  });

  it('does not navigate when the user is already on /login', async () => {
    atLocation('/login');
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401] }));

    await expect(apiService.getUseCase('uc-1')).rejects.toBeInstanceOf(ApiError);

    // The guard the two 401 handlers used to spell out inline, and `/login`
    // itself is never remembered (Requirement 1.4).
    expect(navigations).toEqual([]);
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBeNull();
  });
});

// --------------------------------------------------------- concurrency (5.3)

describe('concurrent 401s (Requirement 5.3)', () => {
  it('performs one refresh for the whole batch and retries each request once', async () => {
    const gate = deferred<ReturnType<typeof sessionWith>>();
    fetchAuthSessionMock.mockReturnValue(gate.promise);
    const fetchMock = planFetch({
      '/usecases/uc-1': [401, 200],
      '/usecases/uc-2': [401, 200],
      '/usecases/uc-3': [401, 200],
    });
    vi.stubGlobal('fetch', fetchMock);

    const inFlight = [
      apiService.getUseCase('uc-1'),
      apiService.getUseCase('uc-2'),
      apiService.getUseCase('uc-3'),
    ];

    // Every first attempt has now seen its 401, so all three are waiting on
    // the same refresh — the situation Requirement 5.3 is about.
    await waitForCalls(fetchMock, 3);
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    gate.resolve(sessionWith('fresh-token'));

    const results = await Promise.all(inFlight);

    expect(results.every((r) => (r as unknown as { ok: boolean }).ok)).toBe(true);
    // One refresh shared by all waiters ...
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    // ... and exactly one retry each: 3 first attempts + 3 retries.
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(navigations).toEqual([]);
  });

  it('lets a later 401 refresh again once the first refresh has settled', async () => {
    fetchAuthSessionMock
      .mockResolvedValueOnce(sessionWith('fresh-1'))
      .mockResolvedValueOnce(sessionWith('fresh-2'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401, 200], '/usecases/uc-2': [401, 200] }));

    await apiService.getUseCase('uc-1');
    await apiService.getUseCase('uc-2');

    // The in-flight collapse is per batch, not a one-shot latch: a genuinely
    // later 401 must still be able to renew the session.
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(2);
    expect(window.localStorage.getItem('idToken')).toBe('fresh-2');
    expect(navigations).toEqual([]);
  });
});

// ------------------------------------------------------- single retry (5.4)

describe('the retried request also 401s (Requirements 5.2, 5.4)', () => {
  it('redirects and never attempts a second retry', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    // Only two attempts are planned: a third would throw out of the stub.
    const fetchMock = planFetch({ '/usecases/uc-1': [401, 401] });
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiService.getUseCase('uc-1')).rejects.toBeInstanceOf(ApiError);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    expect(navigations).toEqual(['/login']);
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe(DEEP_LOCATION);
    expect(window.localStorage.getItem('idToken')).toBeNull();
  });

  it('returns a non-401 failure from the retry to the caller without redirecting', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401, 500] }));

    const error = await apiService.getUseCase('uc-1').catch((err) => err);

    expect((error as ApiError).status).toBe(500);
    // A 500 on the retry is an application error, not an ended session.
    expect(navigations).toEqual([]);
  });
});

// ------------------------------------------- non-401 behaviour is untouched

describe('non-401 responses are unaffected (Requirement 5.6)', () => {
  it('never refreshes on a successful response', async () => {
    const fetchMock = planFetch({ '/usecases/uc-1': [200] });
    vi.stubGlobal('fetch', fetchMock);

    await apiService.getUseCase('uc-1');

    expect(fetchAuthSessionMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(navigations).toEqual([]);
    expect(window.localStorage.getItem('idToken')).toBe('stale-token');
  });

  it('keeps the structured ApiError envelope for a 403 and does not sign the user out', async () => {
    vi.stubGlobal(
      'fetch',
      planFetch({
        '/usecases/uc-1': [
          {
            status: 403,
            body: { error: { code: 'FORBIDDEN', message: 'not your usecase', details: { id: 'uc-1' } } },
          },
        ],
      })
    );

    const error = await apiService.getUseCase('uc-1').catch((err) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect((error as ApiError).code).toBe('FORBIDDEN');
    expect((error as ApiError).details).toEqual({ id: 'uc-1' });
    expect(fetchAuthSessionMock).not.toHaveBeenCalled();
    expect(navigations).toEqual([]);
    expect(window.localStorage.getItem('idToken')).toBe('stale-token');
  });

  it('keeps the simple ApiError envelope for a 500', async () => {
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [{ status: 500, body: { error: 'boom' } }] }));

    const error = await apiService.getUseCase('uc-1').catch((err) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    expect((error as ApiError).message).toBe('boom');
    expect(fetchAuthSessionMock).not.toHaveBeenCalled();
    expect(navigations).toEqual([]);
  });
});

// ------------------------------------------------------- loading bar (5.5)

describe('loading-bar accounting stays balanced (Requirement 5.5)', () => {
  it('is balanced across a refreshed-and-retried request', async () => {
    const before = getActiveCount();
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401, 200] }));

    await apiService.getUseCase('uc-1');

    expect(getActiveCount()).toBe(before);
  });

  it('stays visible while the refresh is in flight, then balances', async () => {
    const before = getActiveCount();
    const gate = deferred<ReturnType<typeof sessionWith>>();
    fetchAuthSessionMock.mockReturnValue(gate.promise);
    const fetchMock = planFetch({ '/usecases/uc-1': [401, 200] });
    vi.stubGlobal('fetch', fetchMock);

    const inFlight = apiService.getUseCase('uc-1');
    await waitForCalls(fetchMock, 1);

    // The retry happens inside the original begin/end span, so the indicator
    // does not flicker off across the refresh (design Decision 6).
    expect(getActiveCount()).toBe(before + 1);

    gate.resolve(sessionWith('fresh-token'));
    await inFlight;
    expect(getActiveCount()).toBe(before);
  });

  it('is balanced when the session is abandoned', async () => {
    const before = getActiveCount();
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    vi.stubGlobal('fetch', planFetch({ '/usecases/uc-1': [401] }));

    await expect(apiService.getUseCase('uc-1')).rejects.toBeInstanceOf(ApiError);

    expect(getActiveCount()).toBe(before);
  });

  it('is balanced when the retry itself throws', async () => {
    const before = getActiveCount();
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    let attempt = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        attempt += 1;
        if (attempt === 1) return jsonResponse(401, { error: 'Unauthorized' });
        throw new TypeError('network down');
      })
    );

    await expect(apiService.getUseCase('uc-1')).rejects.toThrow('network down');

    expect(getActiveCount()).toBe(before);
  });
});

// ------------------------------------------ the Node_Designer client (1.3)

describe('the Node_Designer client takes the same path (Requirement 1.3)', () => {
  it('refreshes and retries once instead of bouncing to /login', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    const fetchMock = planFetch({ '/plugins?usecase_id=uc-1': [401, 200] });
    vi.stubGlobal('fetch', fetchMock);

    const result = await nodeDesignerApi.listPlugins('uc-1');

    expect(result).toMatchObject({ ok: true, attempt: 2 });
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(authHeaderOf(fetchMock, 1)).toBe('Bearer fresh-token');
    expect(navigations).toEqual([]);
    expect(window.localStorage.getItem('idToken')).toBe('fresh-token');
  });

  it('records the attempted location and redirects when the refresh fails', async () => {
    atLocation('/node-designer/plugins/abc?tab=builds');
    fetchAuthSessionMock.mockRejectedValue(new Error('no refresh token'));
    const fetchMock = planFetch({ '/plugins?usecase_id=uc-1': [401] });
    vi.stubGlobal('fetch', fetchMock);

    await expect(nodeDesignerApi.listPlugins('uc-1')).rejects.toBeTruthy();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(navigations).toEqual(['/login']);
    expect(window.sessionStorage.getItem(RETURN_TO_KEY)).toBe('/node-designer/plugins/abc?tab=builds');
    expect(window.localStorage.getItem('idToken')).toBeNull();
  });

  it('redirects without a second retry when the retried request also 401s', async () => {
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    const fetchMock = planFetch({ '/plugins?usecase_id=uc-1': [401, 401] });
    vi.stubGlobal('fetch', fetchMock);

    await expect(nodeDesignerApi.listPlugins('uc-1')).rejects.toBeTruthy();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(navigations).toEqual(['/login']);
  });

  it('shares one refresh with a concurrent main-client 401', async () => {
    const gate = deferred<ReturnType<typeof sessionWith>>();
    fetchAuthSessionMock.mockReturnValue(gate.promise);
    const fetchMock = planFetch({
      '/plugins?usecase_id=uc-1': [401, 200],
      '/usecases/uc-1': [401, 200],
    });
    vi.stubGlobal('fetch', fetchMock);

    const inFlight = [nodeDesignerApi.listPlugins('uc-1'), apiService.getUseCase('uc-1')];
    await waitForCalls(fetchMock, 2);
    gate.resolve(sessionWith('fresh-token'));
    await Promise.all(inFlight);

    // One implementation, one refresh — not one per client.
    expect(fetchAuthSessionMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(navigations).toEqual([]);
  });

  it('leaves its non-401 structured error envelope intact', async () => {
    vi.stubGlobal(
      'fetch',
      planFetch({
        '/plugins?usecase_id=uc-1': [
          {
            status: 400,
            body: {
              error: { code: 'INVALID_DECLARATION', message: 'bad declaration', details: { field: 'name' } },
            },
          },
        ],
      })
    );

    const error = await nodeDesignerApi.listPlugins('uc-1').catch((err) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe('INVALID_DECLARATION');
    expect((error as ApiError).details).toEqual({ field: 'name' });
    expect(fetchAuthSessionMock).not.toHaveBeenCalled();
    expect(navigations).toEqual([]);
  });

  it('keeps its loading-bar accounting balanced across the retry', async () => {
    const before = getActiveCount();
    fetchAuthSessionMock.mockResolvedValue(sessionWith('fresh-token'));
    vi.stubGlobal('fetch', planFetch({ '/plugins?usecase_id=uc-1': [401, 200] }));

    await nodeDesignerApi.listPlugins('uc-1');

    expect(getActiveCount()).toBe(before);
  });
});
