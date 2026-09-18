/**
 * Property-based test for the Silent_Refresh + retry-once 401 path
 * (spec: portal-session-expiry-return-to-page, task 5.3):
 *
 * - **Feature: portal-session-expiry-return-to-page, Property 4: A 401 never
 *   redirects while the session is refreshable, and retries at most once**
 *   (Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6)
 *
 * Each run draws a whole batch of concurrent requests (their first response
 * status and, for the ones that 401, the status their retry would get), the
 * outcome of the single refresh the batch triggers, and the page the user is
 * on. The batch is then driven through the real `ApiService` and the real
 * `sessionRedirect` store with `fetch` stubbed and `fetchAuthSession` mocked,
 * and the expectations are computed from the requirements rather than from the
 * implementation:
 *
 *  - at most one refresh per 401 batch (5.3) and at most one retry per request
 *    (5.4) — attempts are counted per request URL, so no assertion depends on
 *    the order in which concurrent requests are served;
 *  - a redirect happens exactly for the requests whose refresh failed or whose
 *    retry also 401d (5.1, 5.2), and never otherwise;
 *  - the caller receives the retried response, and non-401 statuses reach the
 *    caller unchanged (5.6);
 *  - the in-flight request count returns to its starting value in every case
 *    (5.5);
 *  - the Attempted_Location and the `localStorage` token mirror end in the
 *    state the requirements describe (1.1, 1.4, 1.6).
 *
 * Concurrency is deterministic rather than left to microtask ordering: the
 * mocked `fetchAuthSession` returns a gate promise the test resolves only once
 * every first attempt has already observed its 401, which is exactly the
 * "several requests 401 at once" situation Requirement 5.3 is about.
 *
 * The redirect is observed through the `navigateTo` seam (design Decision 4):
 * jsdom cannot perform a real `window.location.href` navigation.
 *
 * Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as fc from 'fast-check';

const { fetchAuthSessionMock } = vi.hoisted(() => ({ fetchAuthSessionMock: vi.fn() }));

vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: fetchAuthSessionMock }));

import { ApiError, apiService } from './api';
import { getActiveCount } from './loadingBus';
import { RETURN_TO_KEY, setNavigateTo } from './sessionRedirect';

// ------------------------------------------------------------- generators

/** How the drawn refresh behaves when the batch triggers it. */
type RefreshOutcome = 'success' | 'reject' | 'no-token';

/** One request in the batch: its first status, and its retry's status. */
interface PlannedRequest {
  first: number;
  retry: number;
}

interface Scenario {
  requests: PlannedRequest[];
  refresh: RefreshOutcome;
  /** The page the user is on when the batch fires. */
  location: string;
}

const plannedRequestArb: fc.Arbitrary<PlannedRequest> = fc.record({
  // 401 is the interesting one; the others must be carried through untouched.
  first: fc.constantFrom(200, 401, 401, 403, 500),
  // Only consulted when `first` is 401 and the refresh succeeds.
  retry: fc.constantFrom(200, 200, 401, 500),
});

const scenarioArb: fc.Arbitrary<Scenario> = fc.record({
  requests: fc.array(plannedRequestArb, { minLength: 1, maxLength: 4 }),
  refresh: fc.constantFrom<RefreshOutcome>('success', 'success', 'reject', 'no-token'),
  location: fc.constantFrom(
    '/dashboard',
    '/workflows/builder/abc?tab=nodes',
    '/devices/dev-1?tab=cameras#latest',
    '/tuning/session-7?step=2#panel',
    // On the login page nothing is remembered and no navigation happens
    // (Requirement 1.4 and the pre-existing guard).
    '/login'
  ),
});

// --------------------------------------------------------------- fixtures

const FRESH_TOKEN = 'fresh-token';
const STALE_TOKEN = 'stale-token';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
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

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** Move the document location without navigating (jsdom supports this). */
function atLocation(path: string): void {
  window.history.replaceState({}, '', path);
}

/** The endpoint id of request `index` — unique, so attempts are attributable. */
function endpointOf(index: number): string {
  return `req-${index}`;
}

let navigations: string[] = [];
let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  navigations = [];
  setNavigateTo((path) => {
    navigations.push(path);
  });
  // `abandonSession` logs before redirecting; keep 100 runs of output quiet.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  setNavigateTo(null);
  consoleError.mockRestore();
  vi.unstubAllGlobals();
  atLocation('/');
});

// ---------------------------------------------------------------- property

/**
 * **Feature: portal-session-expiry-return-to-page, Property 4: A 401 never
 * redirects while the session is refreshable, and retries at most once**
 *
 * For any generated batch of responses and any refresh outcome, the client
 * SHALL perform at most one refresh per 401 batch (Requirement 5.3) and at most
 * one retry per request (5.4); it SHALL redirect to `/login` exactly for those
 * requests whose refresh failed or whose retry also returned 401 (5.1, 5.2),
 * recording the Attempted_Location when it does (1.1, 1.6); the caller SHALL
 * receive the retried response and non-401 behaviour SHALL be unchanged (5.6);
 * and the in-flight request count SHALL return to its starting value (5.5).
 *
 * **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6**
 */
describe('Feature: portal-session-expiry-return-to-page, Property 4: A 401 never redirects while the session is refreshable, and retries at most once', () => {
  it('refreshes once per batch, retries once per 401, and redirects only when the session is really over', async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async ({ requests, refresh, location }) => {
        // ---- independent, per-run state
        window.sessionStorage.clear();
        window.localStorage.clear();
        window.localStorage.setItem('idToken', STALE_TOKEN);
        atLocation(location);
        navigations = [];
        fetchAuthSessionMock.mockReset();
        const activeBefore = getActiveCount();

        // ---- the refresh, gated so the whole batch shares one in flight
        const gate = deferred<unknown>();
        fetchAuthSessionMock.mockImplementation(() => gate.promise);

        // ---- fetch, planned per request and counted per request
        const attempts = new Map<string, number>();
        const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          void init;
          const url = String(input);
          const index = requests.findIndex((_, i) => url.includes(endpointOf(i)));
          expect(index, `unexpected fetch: ${url}`).toBeGreaterThanOrEqual(0);
          const key = endpointOf(index);
          const attempt = (attempts.get(key) ?? 0) + 1;
          attempts.set(key, attempt);
          const status = attempt === 1 ? requests[index].first : requests[index].retry;
          return jsonResponse(
            status,
            status === 200 ? { ok: true, endpoint: key, attempt } : { error: `HTTP ${status}` }
          );
        });
        vi.stubGlobal('fetch', fetchMock);

        // ---- expectations, derived from the requirements
        const unauthorized = requests.filter((r) => r.first === 401);
        const refreshOk = refresh === 'success';
        const expectedRefreshes = unauthorized.length > 0 ? 1 : 0;
        const expectedRetries = refreshOk ? unauthorized.length : 0;
        const expectedAbandons = refreshOk
          ? unauthorized.filter((r) => r.retry === 401).length
          : unauthorized.length;
        const onLoginPage = location === '/login';
        const expectedNavigations = onLoginPage ? 0 : expectedAbandons;
        const finalStatusOf = (r: PlannedRequest): number =>
          r.first === 401 ? (refreshOk ? r.retry : 401) : r.first;

        // ---- drive the whole batch concurrently
        const inFlight = requests.map((_, i) => apiService.getUseCase(endpointOf(i)));
        const settled = Promise.allSettled(inFlight);

        if (expectedRefreshes > 0) {
          // Wait until every first attempt has observed its response, so all
          // the 401s are queued behind the same refresh (Requirement 5.3).
          for (let i = 0; i < 200 && fetchMock.mock.calls.length < requests.length; i += 1) {
            await tick();
          }
          expect(fetchMock.mock.calls.length).toBe(requests.length);
          // The loading bar is still up while the refresh is pending (5.5).
          expect(getActiveCount()).toBe(activeBefore + requests.length);
          if (refresh === 'success') gate.resolve({ tokens: { idToken: { toString: () => FRESH_TOKEN } } });
          else if (refresh === 'no-token') gate.resolve({ tokens: {} });
          else gate.reject(new Error('session cannot be refreshed'));
        }

        const results = await settled;

        // ---- one refresh for the batch (5.3)
        expect(fetchAuthSessionMock).toHaveBeenCalledTimes(expectedRefreshes);
        if (expectedRefreshes > 0) {
          expect(fetchAuthSessionMock).toHaveBeenCalledWith({ forceRefresh: true });
        }

        // ---- at most one retry per request (5.4)
        expect(fetchMock).toHaveBeenCalledTimes(requests.length + expectedRetries);
        requests.forEach((r, i) => {
          const attempted = attempts.get(endpointOf(i)) ?? 0;
          expect(attempted).toBeLessThanOrEqual(2);
          expect(attempted).toBe(r.first === 401 && refreshOk ? 2 : 1);
        });

        // ---- the retry carries the refreshed token, never the stale one
        const retryCalls = fetchMock.mock.calls.slice(requests.length);
        retryCalls.forEach((call) => {
          const headers = (call[1] as RequestInit | undefined)?.headers as
            | Record<string, string>
            | undefined;
          expect(headers?.['Authorization']).toBe(`Bearer ${FRESH_TOKEN}`);
        });

        // ---- redirect exactly when the session is really over (5.1, 5.2)
        expect(navigations).toHaveLength(expectedNavigations);
        navigations.forEach((path) => expect(path).toBe('/login'));
        if (expectedAbandons === 0) {
          // A refreshable session never reaches the login screen (5.1).
          expect(navigations).toEqual([]);
        }

        // ---- the Attempted_Location (1.1, 1.4, 1.6)
        const remembered = window.sessionStorage.getItem(RETURN_TO_KEY);
        expect(remembered).toBe(expectedAbandons > 0 && !onLoginPage ? location : null);

        // ---- the token mirror
        const mirrored = window.localStorage.getItem('idToken');
        if (expectedAbandons > 0) expect(mirrored).toBeNull();
        else if (refreshOk && unauthorized.length > 0) expect(mirrored).toBe(FRESH_TOKEN);
        else expect(mirrored).toBe(STALE_TOKEN);

        // ---- what each caller sees (5.6)
        results.forEach((outcome, i) => {
          const status = finalStatusOf(requests[i]);
          if (status === 200) {
            expect(outcome.status, `request ${i} should resolve`).toBe('fulfilled');
            if (outcome.status === 'fulfilled') {
              expect(outcome.value).toMatchObject({ ok: true, endpoint: endpointOf(i) });
            }
          } else {
            expect(outcome.status, `request ${i} should reject`).toBe('rejected');
            if (outcome.status === 'rejected') {
              expect(outcome.reason).toBeInstanceOf(ApiError);
              expect((outcome.reason as ApiError).status).toBe(status);
            }
          }
        });

        // ---- loading-bar accounting balanced in every case (5.5)
        expect(getActiveCount()).toBe(activeBefore);
      }),
      { numRuns: 100 }
    );
  });
});
