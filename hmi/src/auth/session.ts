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
 * The startup decision (`decideStartupScreen`) and the stored-payload parse
 * (`parseStoredSession`) are pure functions so they are directly
 * property-testable (design Property 1). Storage access is isolated behind a
 * narrow injectable interface because the kiosk runs in a browser while the
 * test suite runs in node.
 */

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

/** The persisted session: `localStorage["hmi.session"] = {token, expiresAt}`. */
export interface StoredSession {
  /** Bearer Session_Token issued by `POST /local-auth/login`. */
  token: string;
  /** Expiry instant in epoch seconds (backend `expiresAt`). */
  expiresAt: number;
}

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
  session: StoredSession | null,
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
 * Anything that is not a JSON object carrying a non-empty string `token` and
 * a finite numeric `expiresAt` yields null (treated as "no stored session"),
 * so a corrupted localStorage entry sends the kiosk to the login form rather
 * than crashing the app.
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
    !Number.isFinite(record.expiresAt)
  ) {
    return null;
  }
  return { token: record.token, expiresAt: record.expiresAt };
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

/** Loads the stored session, or null when absent, corrupt, or unavailable. */
export function loadSession(
  storage: SessionStorageLike | null = defaultStorage(),
): StoredSession | null {
  if (storage === null) return null;
  try {
    return parseStoredSession(storage.getItem(SESSION_STORAGE_KEY));
  } catch {
    return null;
  }
}

/** Persists the Session_Token together with its `expiresAt` (Requirement 1.2). */
export function saveSession(
  session: StoredSession,
  storage: SessionStorageLike | null = defaultStorage(),
): void {
  if (storage === null) return;
  try {
    storage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch {
    // Storage write failure (e.g. quota): the app keeps working with the
    // in-memory token for this page load; the next reload re-prompts.
  }
}

/** Discards the stored session (used when re-login fails, Requirement 1.8). */
export function clearSession(
  storage: SessionStorageLike | null = defaultStorage(),
): void {
  if (storage === null) return;
  try {
    storage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // Ignore: worst case a stale token remains and startup re-prompts on expiry.
  }
}

/**
 * Startup decision against the given storage and clock (Requirements 1.1, 1.5):
 * reads the stored session and applies `decideStartupScreen`.
 */
export function startupScreen(
  storage: SessionStorageLike | null = defaultStorage(),
  nowEpochSeconds: number = Date.now() / 1000,
): StartupScreen {
  return decideStartupScreen(loadSession(storage), nowEpochSeconds);
}

// --------------------------------------------------------------------------
// In-memory credential retention (never persisted)
// --------------------------------------------------------------------------

let retainedCredentials: Credentials | null = null;

/**
 * Retains the credentials of the most recent successful login in a
 * module-scoped variable only. They back the single 401 re-login attempt and
 * vanish on page reload (Requirement 1.4 groundwork; design Decision 4).
 */
export function retainCredentials(credentials: Credentials): void {
  retainedCredentials = { ...credentials };
}

/** The retained credentials, or null when none (e.g. after a page reload). */
export function getRetainedCredentials(): Credentials | null {
  return retainedCredentials === null ? null : { ...retainedCredentials };
}

/** Drops the retained credentials (e.g. when the re-login is rejected). */
export function clearRetainedCredentials(): void {
  retainedCredentials = null;
}
