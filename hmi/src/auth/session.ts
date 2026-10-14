/**
 * Session management (design "Design Decision 4: Auth/session flow").
 *
 * Responsibilities:
 *  - Startup screen decision: login iff no stored token or `expiresAt <= now`
 *    (Requirement 1.1); otherwise resume without prompting (Requirement 1.5).
 *  - Persistence of the Session_Token together with its `expiresAt` value in
 *    `localStorage["hmi.session"]` after a successful login (Requirement 1.2;
 *    the bearer-header attachment itself lives in `api/client.ts`).
 *  - In-memory-only retention of the credentials from the most recent
 *    successful login, for the single 401 re-login. Credentials are never
 *    persisted (Requirement 1.4 groundwork).
 *
 * Origin binding. The stored session records the origin (`scheme://host:port`)
 * of the LocalServer that issued it, and `loadSession` returns it only for
 * that origin, the current API origin (`api/base.ts`) by default. Every
 * consumer of the token (the bearer header in `api/client.ts`, the `token=`
 * of the image URLs) reads it through `loadSession`, so another origin's token
 * is never sent: for any other API origin the HMI is signed out. The retained
 * credentials carry the same binding. The HMI keeps one session, so signing in
 * to another LocalServer replaces it.
 *
 * A session stored before this binding existed (`{token, expiresAt}` with no
 * origin) cannot be attributed to a LocalServer, so it is ignored: each kiosk
 * shows the login form once after the update.
 *
 * The startup decision (`decideStartupScreen`) and the stored-payload parse
 * (`parseStoredSession`) are pure functions so they are directly
 * property-testable (design Property 1). Storage access is isolated behind a
 * narrow injectable interface because the kiosk runs in a browser while the
 * test suite runs in node.
 */

import { getApiOrigin } from "../api/base";

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

/**
 * The persisted session:
 * `localStorage["hmi.session"] = {token, expiresAt, origin}`.
 */
export interface StoredSession {
  /** Bearer Session_Token issued by `POST /local-auth/login`. */
  token: string;
  /** Expiry instant in epoch seconds (backend `expiresAt`). */
  expiresAt: number;
  /**
   * Origin of the LocalServer that issued `token` (`requestOrigin` of the
   * login URL); the only origin the token is ever sent to.
   */
  origin: string;
}

/** A session to persist; `origin` defaults to the current API origin. */
export type NewSession = Omit<StoredSession, "origin"> & { origin?: string };

/** Login credentials, retained in memory only — never persisted. */
export interface Credentials {
  username: string;
  password: string;
}

export type StartupScreen = "login" | "app";

/** The subset of the DOM Storage interface the session store needs. */
export interface SessionStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export const SESSION_STORAGE_KEY = "hmi.session";

// --------------------------------------------------------------------------
// Pure logic
// --------------------------------------------------------------------------

/**
 * Startup screen decision (Requirements 1.1, 1.5).
 *
 * "login" iff no session is stored or the stored `expiresAt` is at or before
 * the current time; "app" (resume without prompting) otherwise. Times are
 * epoch seconds.
 */
export function decideStartupScreen(
  session: Omit<StoredSession, "origin"> | null,
  nowEpochSeconds: number,
): StartupScreen {
  if (session === null || session.expiresAt <= nowEpochSeconds) {
    return "login";
  }
  return "app";
}

/**
 * Defensively parses the raw string stored under `hmi.session`.
 *
 * Anything that is not a JSON object carrying a non-empty string `token`, a
 * finite numeric `expiresAt` and a string `origin` yields null (treated as "no
 * stored session"), so a corrupted localStorage entry sends the kiosk to the
 * login form rather than crashing the app. A legacy entry without `origin`
 * yields null too (see the module docstring).
 */
export function parseStoredSession(raw: string | null): StoredSession | null {
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const record = parsed as Record<string, unknown>;
  if (
    typeof record.token !== "string" ||
    record.token === "" ||
    typeof record.expiresAt !== "number" ||
    !Number.isFinite(record.expiresAt) ||
    typeof record.origin !== "string"
  ) {
    return null;
  }
  return { token: record.token, expiresAt: record.expiresAt, origin: record.origin };
}

// --------------------------------------------------------------------------
// Storage-backed session store
// --------------------------------------------------------------------------

function defaultStorage(): SessionStorageLike | null {
  // `localStorage` exists in the kiosk browser but not in the node test
  // environment; absence degrades to "no stored session".
  const g = globalThis as { localStorage?: SessionStorageLike };
  return g.localStorage ?? null;
}

/**
 * Loads the session `origin` may use, or null when none is stored, the stored
 * one is corrupt or legacy, storage is unavailable, or the stored session
 * belongs to another origin. `origin` defaults to the current API origin.
 */
export function loadSession(
  storage: SessionStorageLike | null = defaultStorage(),
  origin: string = getApiOrigin(),
): StoredSession | null {
  if (storage === null) return null;
  try {
    const session = parseStoredSession(storage.getItem(SESSION_STORAGE_KEY));
    return session !== null && session.origin === origin ? session : null;
  } catch {
    return null;
  }
}

/**
 * Persists the Session_Token together with its `expiresAt` (Requirement 1.2)
 * and the origin that issued it, the current API origin unless given.
 */
export function saveSession(
  session: NewSession,
  storage: SessionStorageLike | null = defaultStorage(),
): void {
  if (storage === null) return;
  const bound: StoredSession = {
    token: session.token,
    expiresAt: session.expiresAt,
    origin: session.origin ?? getApiOrigin(),
  };
  try {
    storage.setItem(SESSION_STORAGE_KEY, JSON.stringify(bound));
  } catch {
    // Storage write failure (e.g. quota): the app keeps working with the
    // in-memory token for this page load; the next reload re-prompts.
  }
}

/**
 * Discards `origin`'s stored session (used when re-login fails, Requirement
 * 1.8), or a corrupt or legacy entry. A session another origin issued is kept:
 * being signed out of one LocalServer does not sign the kiosk out of another.
 */
export function clearSession(
  storage: SessionStorageLike | null = defaultStorage(),
  origin: string = getApiOrigin(),
): void {
  if (storage === null) return;
  try {
    const stored = parseStoredSession(storage.getItem(SESSION_STORAGE_KEY));
    if (stored !== null && stored.origin !== origin) return;
    storage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Ignore: worst case a stale token remains and startup re-prompts on expiry.
  }
}

/**
 * Startup decision against the given storage and clock (Requirements 1.1, 1.5):
 * reads the session `origin` may use and applies `decideStartupScreen`, so a
 * session issued by another origin means the login form.
 */
export function startupScreen(
  storage: SessionStorageLike | null = defaultStorage(),
  nowEpochSeconds: number = Date.now() / 1000,
  origin: string = getApiOrigin(),
): StartupScreen {
  return decideStartupScreen(loadSession(storage, origin), nowEpochSeconds);
}

// --------------------------------------------------------------------------
// In-memory credential retention (never persisted)
// --------------------------------------------------------------------------

let retained: { credentials: Credentials; origin: string } | null = null;

/**
 * Retains the credentials of the most recent successful login in a
 * module-scoped variable only, with the origin that accepted them. They back
 * the single 401 re-login attempt and vanish on page reload (Requirement 1.4
 * groundwork; design Decision 4).
 */
export function retainCredentials(
  credentials: Credentials,
  origin: string = getApiOrigin(),
): void {
  retained = { credentials: { ...credentials }, origin };
}

/**
 * The retained credentials, or null when none (e.g. after a page reload) or
 * when another origin accepted them, so they are never posted elsewhere.
 */
export function getRetainedCredentials(
  origin: string = getApiOrigin(),
): Credentials | null {
  return retained === null || retained.origin !== origin
    ? null
    : { ...retained.credentials };
}

/** Drops the retained credentials (e.g. when the re-login is rejected). */
export function clearRetainedCredentials(): void {
  retained = null;
}
