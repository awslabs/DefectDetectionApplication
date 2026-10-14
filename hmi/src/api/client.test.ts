import fc from "fast-check";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  apiFetch,
  configureApiClient,
  login,
  resetApiClient,
} from "./client";
import {
  executionMetadataUrl,
  executionResultsUrl,
  registrationExecutionsUrl,
  registrationsUrl,
} from "./routes";
import {
  clearRetainedCredentials,
  retainCredentials,
  saveSession,
  SESSION_STORAGE_KEY,
  type SessionStorageLike,
} from "../auth/session";

// Unit tests for the apiFetch wrapper (design Decision 4).
// _Requirements: 1.2, 1.4, 1.6, 1.7, 1.8_

/** Minimal in-memory implementation of the Storage subset the module uses. */
function makeStorage(): SessionStorageLike & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

interface RecordedRequest {
  url: string;
  method: string;
  authorization: string | null;
  body: string | null;
}

type Responder = (request: RecordedRequest) => Response | "network-error";

/**
 * A scripted fetch: routes each request through `responder` and records it.
 * Returning "network-error" makes the fetch reject like a failed connection.
 */
function makeFetch(responder: Responder): {
  fetchFn: typeof fetch;
  requests: RecordedRequest[];
} {
  const requests: RecordedRequest[] = [];
  const fetchFn: typeof fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    const request: RecordedRequest = {
      url: String(input),
      method: init?.method ?? "GET",
      authorization: headers.get("Authorization"),
      body: typeof init?.body === "string" ? init.body : null,
    };
    requests.push(request);
    const result = responder(request);
    if (result === "network-error") {
      throw new TypeError("fetch failed");
    }
    return result;
  };
  return { fetchFn, requests };
}

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let storage: ReturnType<typeof makeStorage>;
let authExpiredCalls: number;

function setup(responder: Responder): RecordedRequest[] {
  const { fetchFn, requests } = makeFetch(responder);
  configureApiClient({
    fetchFn,
    storage,
    onAuthExpired: () => {
      authExpiredCalls += 1;
    },
  });
  return requests;
}

beforeEach(() => {
  storage = makeStorage();
  authExpiredCalls = 0;
  clearRetainedCredentials();
});

afterEach(() => {
  resetApiClient();
  clearRetainedCredentials();
});

describe("apiFetch bearer header (1.2)", () => {
  it("attaches Authorization: Bearer <token> from the stored session", async () => {
    saveSession({ token: "tok-1", expiresAt: 9e9 }, storage);
    const requests = setup(() => jsonResponse(200, { hello: 1 }));

    const result = await apiFetch("/workflows/registrations");

    expect(result).toEqual({ ok: true, status: 200, data: { hello: 1 } });
    expect(requests).toHaveLength(1);
    expect(requests[0]!.authorization).toBe("Bearer tok-1");
  });

  it("sends no Authorization header without a stored session", async () => {
    const requests = setup(() => jsonResponse(401));
    await apiFetch("/workflows/registrations");
    expect(requests[0]!.authorization).toBeNull();
  });
});

describe("apiFetch error classification", () => {
  it("classifies network errors", async () => {
    setup(() => "network-error");
    expect(await apiFetch("/x")).toEqual({
      ok: false,
      kind: "network",
      status: null,
    });
  });

  it("classifies HTTP 5xx", async () => {
    setup(() => jsonResponse(503));
    expect(await apiFetch("/x")).toEqual({
      ok: false,
      kind: "http-5xx",
      status: 503,
    });
  });

  it("classifies other HTTP errors", async () => {
    setup(() => jsonResponse(404));
    expect(await apiFetch("/x")).toEqual({
      ok: false,
      kind: "http-other",
      status: 404,
    });
  });

  it("classifies a request exceeding the timeout as timeout", async () => {
    configureApiClient({
      storage,
      timeoutMs: 5,
      fetchFn: (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    });
    expect(await apiFetch("/slow")).toEqual({
      ok: false,
      kind: "timeout",
      status: null,
    });
  });
});

describe("single re-login on 401 (1.4, 1.8)", () => {
  it("re-logs in once with retained credentials and retries the original request once", async () => {
    saveSession({ token: "stale", expiresAt: 9e9 }, storage);
    retainCredentials({ username: "op", password: "pw" });
    const requests = setup((request) => {
      if (request.url === "/local-auth/login") {
        return jsonResponse(200, { token: "fresh", expiresAt: 9e9 });
      }
      return request.authorization === "Bearer fresh"
        ? jsonResponse(200, { data: 42 })
        : jsonResponse(401);
    });

    const result = await apiFetch("/workflows/registrations");

    expect(result).toEqual({ ok: true, status: 200, data: { data: 42 } });
    expect(requests.map((r) => r.url)).toEqual([
      "/workflows/registrations",
      "/local-auth/login",
      "/workflows/registrations",
    ]);
    // The re-login used the in-memory credentials (1.4).
    expect(JSON.parse(requests[1]!.body!)).toEqual({
      username: "op",
      password: "pw",
    });
    // The fresh token replaced the stale one in storage.
    expect(storage.data.get(SESSION_STORAGE_KEY)).toContain("fresh");
    expect(authExpiredCalls).toBe(0);
  });

  it("discards the token and surfaces login when no credentials are retained (1.8)", async () => {
    saveSession({ token: "stale", expiresAt: 9e9 }, storage);
    const requests = setup(() => jsonResponse(401));

    const result = await apiFetch("/workflows/registrations");

    expect(result).toEqual({ ok: false, kind: "http-401", status: 401 });
    expect(requests).toHaveLength(1); // no login attempt without credentials
    expect(storage.data.has(SESSION_STORAGE_KEY)).toBe(false);
    expect(authExpiredCalls).toBe(1);
  });

  it("discards the token and surfaces login when the single re-login fails (1.8)", async () => {
    saveSession({ token: "stale", expiresAt: 9e9 }, storage);
    retainCredentials({ username: "op", password: "old-pw" });
    const requests = setup((request) =>
      request.url === "/local-auth/login" ? jsonResponse(401) : jsonResponse(401),
    );

    const result = await apiFetch("/workflows/registrations");

    expect(result).toEqual({ ok: false, kind: "http-401", status: 401 });
    // Exactly one login attempt, no retry of the original request.
    expect(requests.map((r) => r.url)).toEqual([
      "/workflows/registrations",
      "/local-auth/login",
    ]);
    expect(storage.data.has(SESSION_STORAGE_KEY)).toBe(false);
    expect(authExpiredCalls).toBe(1);
  });

  it("shares one in-flight re-login across concurrent 401s (module-level latch)", async () => {
    saveSession({ token: "stale", expiresAt: 9e9 }, storage);
    retainCredentials({ username: "op", password: "pw" });
    const requests = setup((request) => {
      if (request.url === "/local-auth/login") {
        return jsonResponse(200, { token: "fresh", expiresAt: 9e9 });
      }
      return request.authorization === "Bearer fresh"
        ? jsonResponse(200, {})
        : jsonResponse(401);
    });

    const [a, b] = await Promise.all([apiFetch("/a"), apiFetch("/b")]);

    expect(a.ok).toBe(true);
    expect(b.ok).toBe(true);
    const loginCalls = requests.filter((r) => r.url === "/local-auth/login");
    expect(loginCalls).toHaveLength(1);
  });
});

describe("login response handling (1.2, 1.6, 1.7)", () => {
  it("stores token + expiresAt and retains credentials on success (1.2)", async () => {
    setup(() => jsonResponse(200, { token: "tok", expiresAt: 123 }));

    const result = await login({ username: "op", password: "pw" });

    expect(result).toEqual({ ok: true, session: { token: "tok", expiresAt: 123 } });
    expect(storage.data.get(SESSION_STORAGE_KEY)).toBe(
      '{"token":"tok","expiresAt":123}',
    );
  });

  it("maps HTTP 403 to the local-login-disabled state (1.6)", async () => {
    setup(() => jsonResponse(403, { detail: "local login is disabled" }));
    expect(await login({ username: "op", password: "pw" })).toEqual({
      ok: false,
      reason: "local-login-disabled",
    });
    expect(storage.data.size).toBe(0);
  });

  it("maps HTTP 401 to the credentials-rejected state and stores nothing (1.7)", async () => {
    setup(() => jsonResponse(401));
    expect(await login({ username: "op", password: "bad" })).toEqual({
      ok: false,
      reason: "credentials-rejected",
    });
    expect(storage.data.size).toBe(0);
  });

  it("does not run the 401 re-login interception for the login route", async () => {
    retainCredentials({ username: "op", password: "pw" });
    const requests = setup(() => jsonResponse(401));
    await login({ username: "op", password: "pw" });
    expect(requests).toHaveLength(1);
  });
});

// --------------------------------------------------------------------------
// **Feature: imts-triple-inspection-hmi, Property 2: Single re-login on 401**
// **Validates: Requirements 1.4**
//
// For any scripted sequence of API responses containing 401s, `apiFetch`
// performs at most one `POST /local-auth/login` (using only the in-memory
// credentials) and at most one retry of the original request per 401; whenever
// the single re-login fails or no credentials are retained, the stored
// Session_Token is discarded and the resulting state is the login screen.
//
// Task 9.3 of the imts-triple-inspection-hmi spec: the route generator below
// covers every authenticated non-image route the triple entry calls —
// `GET /local-auth/status` (new for the triple entry, Requirement 1.8),
// `GET /workflows/registrations`, the bounded recent-executions route, and a
// run's `/results` and `/metadata`. The node-image URL is deliberately absent:
// it is loaded through `<img src>` with the token in query, never through
// `apiFetch`, so it is outside this property's scope (Requirement 4.5).
// `api/client.ts` and `api/routes.ts` are unchanged.
// --------------------------------------------------------------------------

/**
 * `GET /local-auth/status` — the startup local-login probe the triple entry
 * calls. There is no builder for it in `api/routes.ts` (which stays
 * unchanged), so the path is spelled out here.
 */
const LOCAL_AUTH_STATUS_URL = "/local-auth/status";

/** The authenticated non-image routes the triple entry drives through apiFetch. */
const tripleCallSite = fc.oneof(
  fc.constant(LOCAL_AUTH_STATUS_URL),
  fc.constant(registrationsUrl()),
  fc
    .string({ minLength: 1, maxLength: 12 })
    .map((id) => registrationExecutionsUrl(id, 10)),
  fc.string({ minLength: 1, maxLength: 12 }).map((id) => executionResultsUrl(id)),
  fc.string({ minLength: 1, maxLength: 12 }).map((id) => executionMetadataUrl(id)),
);

/** Status codes a LocalServer route can answer with, 401 included. */
const routeStatus = fc.constantFrom(200, 401, 401, 404, 500, 503);

/** The scripted outcome of the single re-login attempt. */
const loginOutcome = fc.constantFrom<
  "success" | "credentials-rejected" | "disabled" | "server-error" | "network"
>("success", "credentials-rejected", "disabled", "server-error", "network");

interface Scenario {
  url: string;
  /** Response statuses the target route serves, in request order. */
  routeStatuses: number[];
  loginOutcome: "success" | "credentials-rejected" | "disabled" | "server-error" | "network";
  hasStoredToken: boolean;
  hasCredentials: boolean;
}

const scenarioArb: fc.Arbitrary<Scenario> = fc.record({
  url: tripleCallSite,
  routeStatuses: fc.array(routeStatus, { minLength: 2, maxLength: 2 }),
  loginOutcome,
  hasStoredToken: fc.boolean(),
  hasCredentials: fc.boolean(),
});

interface Observation {
  requests: RecordedRequest[];
  result: Awaited<ReturnType<typeof apiFetch>>;
  tokenStored: boolean;
  authExpiredCalls: number;
}

async function runScenario(scenario: Scenario): Promise<Observation> {
  // Per-iteration isolation: fresh storage, cleared credentials, cleared latch.
  resetApiClient();
  clearRetainedCredentials();
  const iterationStorage = makeStorage();
  let expiredCalls = 0;

  if (scenario.hasStoredToken) {
    saveSession({ token: "stored-token", expiresAt: 9e9 }, iterationStorage);
  }
  if (scenario.hasCredentials) {
    retainCredentials({ username: "op", password: "pw" });
  }

  let routeCall = 0;
  const { fetchFn, requests } = makeFetch((request) => {
    if (request.url === "/local-auth/login") {
      switch (scenario.loginOutcome) {
        case "success":
          return jsonResponse(200, { token: "fresh-token", expiresAt: 9e9 });
        case "credentials-rejected":
          return jsonResponse(401);
        case "disabled":
          return jsonResponse(403);
        case "server-error":
          return jsonResponse(500);
        case "network":
          return "network-error";
      }
    }
    const status = scenario.routeStatuses[routeCall] ?? 200;
    routeCall += 1;
    return jsonResponse(status);
  });

  configureApiClient({
    fetchFn,
    storage: iterationStorage,
    onAuthExpired: () => {
      expiredCalls += 1;
    },
  });

  const result = await apiFetch(scenario.url);
  return {
    requests,
    result,
    tokenStored: iterationStorage.data.has(SESSION_STORAGE_KEY),
    authExpiredCalls: expiredCalls,
  };
}

describe("Property 2: Single re-login on 401 (1.4)", () => {
  it("performs at most one re-login and one retry per 401, discarding the token when re-login fails", async () => {
    await fc.assert(
      fc.asyncProperty(scenarioArb, async (scenario) => {
        const observed = await runScenario(scenario);
        const loginCalls = observed.requests.filter(
          (r) => r.url === "/local-auth/login",
        );
        const routeCalls = observed.requests.filter(
          (r) => r.url === scenario.url,
        );

        // At most one login attempt and at most one retry of the original
        // request, for every call site the triple entry uses.
        expect(loginCalls.length).toBeLessThanOrEqual(1);
        expect(routeCalls.length).toBeLessThanOrEqual(2);
        expect(observed.requests).toHaveLength(
          loginCalls.length + routeCalls.length,
        );

        // The login body carries only the in-memory credentials.
        for (const call of loginCalls) {
          expect(JSON.parse(call.body!)).toEqual({
            username: "op",
            password: "pw",
          });
        }

        const firstIs401 = scenario.routeStatuses[0] === 401;
        if (!firstIs401) {
          // No 401 → no re-login interception at all.
          expect(loginCalls).toHaveLength(0);
          expect(routeCalls).toHaveLength(1);
          expect(observed.authExpiredCalls).toBe(0);
          expect(observed.tokenStored).toBe(scenario.hasStoredToken);
          return;
        }

        if (!scenario.hasCredentials) {
          // No credentials retained → no login attempt, token discarded, and
          // the login screen surfaced exactly once.
          expect(loginCalls).toHaveLength(0);
          expect(routeCalls).toHaveLength(1);
          expect(observed.tokenStored).toBe(false);
          expect(observed.authExpiredCalls).toBe(1);
          expect(observed.result).toEqual({
            ok: false,
            kind: "http-401",
            status: 401,
          });
          return;
        }

        // Credentials retained → exactly one re-login attempt.
        expect(loginCalls).toHaveLength(1);

        if (scenario.loginOutcome !== "success") {
          // The single re-login failed: no retry, token discarded, login screen.
          expect(routeCalls).toHaveLength(1);
          expect(observed.tokenStored).toBe(false);
          expect(observed.authExpiredCalls).toBe(1);
          expect(observed.result).toEqual({
            ok: false,
            kind: "http-401",
            status: 401,
          });
          return;
        }

        // Re-login succeeded: exactly one retry, carrying the fresh token.
        expect(routeCalls).toHaveLength(2);
        expect(routeCalls[1]!.authorization).toBe("Bearer fresh-token");

        const retryStatus = scenario.routeStatuses[1];
        if (retryStatus === 401) {
          // Still unauthorized after a fresh token: no further attempts, token
          // discarded, login screen surfaced.
          expect(observed.tokenStored).toBe(false);
          expect(observed.authExpiredCalls).toBe(1);
          expect(observed.result).toEqual({
            ok: false,
            kind: "http-401",
            status: 401,
          });
        } else {
          // The retry outcome is returned as-is; the fresh token is kept.
          expect(observed.tokenStored).toBe(true);
          expect(observed.authExpiredCalls).toBe(0);
          expect(observed.result.ok).toBe(retryStatus === 200);
        }
      }),
    );
  });
});
