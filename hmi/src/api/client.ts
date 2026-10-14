/**
 * `apiFetch` — the single wrapper every authenticated non-image LocalServer
 * request goes through (design "Design Decision 4: Auth/session flow").
 *
 * Responsibilities:
 *  - Attach `Authorization: Bearer <token>` from the stored session to every
 *    request (Requirement 1.2).
 *  - Apply a 10-second timeout per request via `AbortController`.
 *  - Classify failures as `network` / `timeout` / `http-5xx` / `http-401` /
 *    `http-other` so the connection state machine can route them
 *    (Requirement 8.1 groundwork; 401 routes to the auth path).
 *  - On a 401 from any route except `POST /local-auth/login` itself: perform
 *    a single re-login with the in-memory credentials (Requirement 1.4),
 *    guarded by a module-level in-flight latch so concurrent 401s share one
 *    login attempt, then retry the original request exactly once. When the
 *    re-login fails or no credentials are retained, the stored Session_Token
 *    is discarded and the login screen is surfaced (Requirement 1.8).
 *  - Login-response handling (`login`): HTTP 403 → local-login-disabled
 *    state (Requirement 1.6); HTTP 401 → credentials-rejected state with
 *    nothing stored (Requirement 1.7); success → session persisted and
 *    credentials retained in memory only (Requirement 1.2).
 *
 * The wrapper is effectful (network + storage), so its collaborators
 * (`fetch`, storage, clock-driven timeout, auth-expired notification) are
 * injectable through `configureApiClient` for the test suite.
 */

import {
  clearRetainedCredentials,
  clearSession,
  getRetainedCredentials,
  loadSession,
  retainCredentials,
  saveSession,
  type Credentials,
  type SessionStorageLike,
  type StoredSession,
} from "../auth/session";
import { loginUrl } from "./routes";

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

/** Failure classification consumed by the connection/auth state machine. */
export type ApiErrorKind =
  | "network"
  | "timeout"
  | "http-5xx"
  | "http-401"
  | "http-other";

/** Outcome of an `apiFetch` call; `data` is the parsed JSON body. */
export type ApiResult<T = unknown> =
  | { ok: true; status: number; data: T }
  | { ok: false; kind: ApiErrorKind; status: number | null };

/** Outcome of a `login` call (Requirements 1.2, 1.6, 1.7). */
export type LoginResult =
  | { ok: true; session: StoredSession }
  | {
      ok: false;
      reason:
        | "local-login-disabled" // HTTP 403 (Requirement 1.6)
        | "credentials-rejected" // HTTP 401 (Requirement 1.7)
        | "network"
        | "timeout"
        | "http-5xx"
        | "http-other";
    };

/** Injectable collaborators; defaults target the real browser environment. */
export interface ApiClientConfig {
  fetchFn: typeof fetch;
  /** Session storage; `undefined` selects the module default (localStorage). */
  storage: SessionStorageLike | undefined;
  /** Per-request timeout in milliseconds (design: 10 seconds). */
  timeoutMs: number;
  /**
   * Invoked after the stored Session_Token has been discarded because the
   * single re-login failed or no credentials were retained; the app shell
   * surfaces the login screen from here (Requirement 1.8).
   */
  onAuthExpired: (() => void) | null;
}

export const DEFAULT_TIMEOUT_MS = 10_000;

// --------------------------------------------------------------------------
// Module configuration
// --------------------------------------------------------------------------

function defaultConfig(): ApiClientConfig {
  return {
    fetchFn: (input, init) => globalThis.fetch(input, init),
    storage: undefined,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    onAuthExpired: null,
  };
}

let config: ApiClientConfig = defaultConfig();

/** Overrides collaborators (tests, app wiring of `onAuthExpired`). */
export function configureApiClient(overrides: Partial<ApiClientConfig>): void {
  config = { ...config, ...overrides };
}

/** Restores defaults and clears the re-login latch (test isolation). */
export function resetApiClient(): void {
  config = defaultConfig();
  reloginInFlight = null;
}

// --------------------------------------------------------------------------
// Low-level request helpers
// --------------------------------------------------------------------------

type RawOutcome =
  | { kind: "response"; response: Response }
  | { kind: "timeout" }
  | { kind: "network" };

/** One fetch with the 10-second `AbortController` timeout applied. */
async function timedFetch(url: string, init: RequestInit): Promise<RawOutcome> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await config.fetchFn(url, {
      ...init,
      signal: controller.signal,
    });
    return { kind: "response", response };
  } catch {
    // A rejection after our own abort is the timeout; anything else is a
    // network-level failure (DNS, refused connection, TLS, CORS, ...).
    return controller.signal.aborted ? { kind: "timeout" } : { kind: "network" };
  } finally {
    clearTimeout(timer);
  }
}

/** Parses a JSON body, degrading to `undefined` instead of throwing. */
async function safeJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/** One bearer-authenticated request with classification, no 401 handling. */
async function requestWithToken<T>(
  url: string,
  init: RequestInit,
  token: string | null,
): Promise<ApiResult<T>> {
  const headers = new Headers(init.headers);
  if (token !== null) {
    headers.set("Authorization", `Bearer ${token}`); // Requirement 1.2
  }
  const outcome = await timedFetch(url, { ...init, headers });
  if (outcome.kind !== "response") {
    return { ok: false, kind: outcome.kind, status: null };
  }
  const { response } = outcome;
  if (response.status === 401) {
    return { ok: false, kind: "http-401", status: 401 };
  }
  if (response.status >= 500) {
    return { ok: false, kind: "http-5xx", status: response.status };
  }
  if (!response.ok) {
    return { ok: false, kind: "http-other", status: response.status };
  }
  return { ok: true, status: response.status, data: (await safeJson(response)) as T };
}

// --------------------------------------------------------------------------
// Login (Requirements 1.2, 1.6, 1.7)
// --------------------------------------------------------------------------

function parseLoginSession(raw: unknown): StoredSession | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (
    typeof record.token !== "string" ||
    record.token === "" ||
    typeof record.expiresAt !== "number" ||
    !Number.isFinite(record.expiresAt)
  ) {
    return null;
  }
  return { token: record.token, expiresAt: record.expiresAt };
}

/**
 * Submits credentials to `POST /local-auth/login`.
 *
 * - Success: persists `{token, expiresAt}` and retains the credentials in
 *   memory only (Requirement 1.2).
 * - HTTP 403: local login is disabled on the device (Requirement 1.6).
 * - HTTP 401: credentials rejected; nothing is stored (Requirement 1.7).
 *
 * The login route never goes through the 401 re-login interception.
 */
export async function login(credentials: Credentials): Promise<LoginResult> {
  const outcome = await timedFetch(loginUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: credentials.username,
      password: credentials.password,
    }),
  });
  if (outcome.kind !== "response") {
    return { ok: false, reason: outcome.kind };
  }
  const { response } = outcome;
  if (response.status === 403) {
    return { ok: false, reason: "local-login-disabled" }; // Requirement 1.6
  }
  if (response.status === 401) {
    return { ok: false, reason: "credentials-rejected" }; // Requirement 1.7
  }
  if (response.status >= 500) {
    return { ok: false, reason: "http-5xx" };
  }
  if (!response.ok) {
    return { ok: false, reason: "http-other" };
  }
  const session = parseLoginSession(await safeJson(response));
  if (session === null) {
    // A 2xx login without a usable token is unusable; store nothing.
    return { ok: false, reason: "http-other" };
  }
  saveSession(session, config.storage); // Requirement 1.2
  retainCredentials(credentials); // in-memory only (Requirement 1.4 groundwork)
  return { ok: true, session };
}

// --------------------------------------------------------------------------
// Single re-login on 401 (Requirements 1.4, 1.8)
// --------------------------------------------------------------------------

/**
 * Module-level in-flight latch: concurrent 401s share one re-login attempt
 * instead of firing parallel logins (design Decision 4).
 */
let reloginInFlight: Promise<string | null> | null = null;

/** Discards the stored token and surfaces the login screen (Requirement 1.8). */
function handleAuthExpired(): void {
  clearSession(config.storage);
  config.onAuthExpired?.();
}

async function performRelogin(): Promise<string | null> {
  const credentials = getRetainedCredentials();
  if (credentials === null) {
    // No credentials retained (e.g. after a page reload): Requirement 1.8.
    handleAuthExpired();
    return null;
  }
  const result = await login(credentials); // exactly once (Requirement 1.4)
  if (result.ok) {
    return result.session.token;
  }
  if (
    result.reason === "credentials-rejected" ||
    result.reason === "local-login-disabled"
  ) {
    // The retained credentials are definitively unusable; drop them so later
    // 401s go straight to the login screen instead of re-submitting them.
    clearRetainedCredentials();
  }
  handleAuthExpired(); // Requirement 1.8
  return null;
}

/** Joins the in-flight re-login, or starts the single attempt. */
function reloginOnce(): Promise<string | null> {
  if (reloginInFlight === null) {
    reloginInFlight = performRelogin().finally(() => {
      reloginInFlight = null;
    });
  }
  return reloginInFlight;
}

// --------------------------------------------------------------------------
// apiFetch
// --------------------------------------------------------------------------

/**
 * The wrapper for every authenticated non-image JSON request.
 *
 * Attaches the bearer Session_Token (Requirement 1.2), applies the 10-second
 * timeout, classifies failures, and on a 401 from any route except login
 * performs the single re-login + single retry of the original request
 * (Requirements 1.4, 1.8).
 */
export async function apiFetch<T = unknown>(
  url: string,
  init: RequestInit = {},
): Promise<ApiResult<T>> {
  const session = loadSession(config.storage);
  const first = await requestWithToken<T>(url, init, session?.token ?? null);
  if (first.ok || first.kind !== "http-401" || url === loginUrl()) {
    return first;
  }

  // 401 from a non-login route: single re-login, then single retry.
  const freshToken = await reloginOnce();
  if (freshToken === null) {
    // Re-login failed or no credentials: token already discarded and the
    // login screen surfaced (Requirement 1.8).
    return first;
  }
  const retry = await requestWithToken<T>(url, init, freshToken);
  if (!retry.ok && retry.kind === "http-401") {
    // Still unauthorized after a fresh token: no further attempts; discard
    // the token and surface the login screen (Requirements 1.4, 1.8).
    handleAuthExpired();
  }
  return retry;
}
