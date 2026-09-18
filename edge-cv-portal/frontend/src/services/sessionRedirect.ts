/**
 * Session redirect memory — Feature: portal-session-expiry-return-to-page.
 *
 * Single owner of "where was the user when the session ended, and where do we
 * put them after they sign back in". Every Session_Exit (the two API clients'
 * 401 handlers and `ProtectedRoute`) records the Attempted_Location here, and
 * `pages/Login.tsx` consumes it once on successful sign-in.
 *
 * Storage is `sessionStorage` under one key (design Decision 1): the dominant
 * exit path assigns `window.location.href`, a full document navigation that
 * React state and router `location.state` do not survive. `sessionStorage` is
 * per-tab, survives that navigation, never travels in a URL (so it adds no
 * open-redirect surface and leaks no deep links into history or access logs),
 * and disappears when the tab closes.
 *
 * The value is validated when it is *consumed*, not only when it is saved
 * (design Decision 3), so a value planted directly into storage can never
 * navigate anywhere unsafe. Every storage access is wrapped: a browser with
 * storage disabled throws `SecurityError` on `sessionStorage` access, and that
 * must degrade to "no remembered location" rather than throw into a 401
 * handler.
 *
 * Validates: Requirements 1.1, 1.4, 1.5, 1.6, 2.4, 3.1, 3.2, 3.3, 3.4
 */

/** The one storage key holding the Attempted_Location (design Decision 1). */
export const RETURN_TO_KEY = 'dda.portal.returnTo';

/** Documented length bound for a remembered location (Requirement 3.3). */
export const MAX_PATH_LENGTH = 2048;

/** The login route — never remembered, always the redirect target. */
export const LOGIN_PATH = '/login';

/** Control characters are rejected outright (design Decision 3). */
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

/**
 * The shape `saveAttemptedLocation` needs. Satisfied by both `window.location`
 * and react-router's `Location`, so callers can pass either.
 */
export interface LocationLike {
  pathname: string;
  search?: string;
  hash?: string;
}

/**
 * Pure validator for a remembered location (Requirement 3.1-3.4).
 *
 * Accepted iff the candidate:
 *  - is a non-empty string beginning with exactly one `/` — which rejects
 *    protocol-relative `//evil.com` and every scheme-bearing form
 *    (`http:`, `https:`, `javascript:`, `data:`, …) since those do not start
 *    with `/`;
 *  - does not begin `/\` (the backslash spelling of protocol-relative, which
 *    some browsers normalise to `//`);
 *  - contains no control characters;
 *  - is at most `MAX_PATH_LENGTH` characters;
 *  - does not point at the login page itself (Requirement 1.4, and loop
 *    safety for Requirement 2.5).
 *
 * No DOM access, no side effects — it is tested directly against adversarial
 * inputs (Requirement 3.4).
 */
export function isSafeInternalPath(candidate: unknown): candidate is string {
  if (typeof candidate !== 'string') return false;
  if (candidate.length === 0 || candidate.length > MAX_PATH_LENGTH) return false;

  // Exactly one leading slash: rejects '//host', '/\host' and anything that
  // does not start at the site root (including all absolute URLs).
  if (candidate[0] !== '/') return false;
  if (candidate[1] === '/' || candidate[1] === '\\') return false;

  if (CONTROL_CHARACTERS.test(candidate)) return false;

  // The path portion (before any query or fragment) must not be the login
  // page, otherwise a remembered value could bounce sign-in back to /login.
  const pathEnd = Math.min(
    ...['?', '#'].map((sep) => {
      const at = candidate.indexOf(sep);
      return at === -1 ? candidate.length : at;
    })
  );
  const path = candidate.slice(0, pathEnd).toLowerCase();
  const normalised = path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
  if (normalised === LOGIN_PATH || normalised.startsWith(`${LOGIN_PATH}/`)) return false;

  return true;
}

/**
 * The `sessionStorage` object, or `null` when storage is unavailable.
 *
 * Merely touching `window.sessionStorage` throws `SecurityError` when cookies
 * and site data are blocked, so the access itself is guarded.
 */
function getStore(): Storage | null {
  try {
    const store = globalThis.sessionStorage;
    return store ?? null;
  } catch {
    return null;
  }
}

function readRaw(): string | null {
  const store = getStore();
  if (!store) return null;
  try {
    return store.getItem(RETURN_TO_KEY);
  } catch {
    return null;
  }
}

function writeRaw(value: string): void {
  const store = getStore();
  if (!store) return;
  try {
    store.setItem(RETURN_TO_KEY, value);
  } catch {
    /* storage disabled or quota exceeded: no remembered location */
  }
}

function removeRaw(): void {
  const store = getStore();
  if (!store) return;
  try {
    store.removeItem(RETURN_TO_KEY);
  } catch {
    /* storage disabled: nothing to forget */
  }
}

/** The current document location, when there is one. */
function currentLocation(): LocationLike | null {
  try {
    const loc = globalThis.location;
    return loc && typeof loc.pathname === 'string' ? loc : null;
  } catch {
    return null;
  }
}

/** `pathname + search + hash` for a location-like object. */
function toPath(loc: LocationLike): string {
  const pathname = typeof loc.pathname === 'string' ? loc.pathname : '';
  const search = typeof loc.search === 'string' ? loc.search : '';
  const hash = typeof loc.hash === 'string' ? loc.hash : '';
  return `${pathname}${search}${hash}`;
}

/**
 * Record where the user was, as `pathname + search + hash`
 * (Requirements 1.1, 1.2, 1.5).
 *
 * First write wins (design Decision 2): one expiry commonly produces several
 * concurrent 401s, and the first observer holds the location the user actually
 * cared about. `/login` and anything else the validator rejects is never
 * recorded (Requirement 1.4).
 *
 * @param loc location to record; defaults to the current document location.
 */
export function saveAttemptedLocation(loc?: LocationLike | null): void {
  const source = loc ?? currentLocation();
  if (!source) return;

  const path = toPath(source);
  if (!isSafeInternalPath(path)) return;

  // First-write-wins: keep whatever is already remembered (Requirement 1.6).
  const existing = readRaw();
  if (existing !== null && existing.length > 0) return;

  writeRaw(path);
}

/**
 * Read, clear and validate the remembered location (Requirements 2.1, 2.4).
 *
 * Consume-once: the value is removed whether or not it validates, so a later
 * ordinary sign-in cannot resurrect a stale page and a guard-driven bounce
 * cannot re-restore one (Requirement 2.5).
 *
 * @returns a Safe_Internal_Path, or `null` when there is nothing usable.
 */
export function takeAttemptedLocation(): string | null {
  const raw = readRaw();
  removeRaw();
  if (raw === null) return null;
  return isSafeInternalPath(raw) ? raw : null;
}

/** Forget any remembered location (deliberate sign-out, Requirement 4.1). */
export function clearAttemptedLocation(): void {
  removeRaw();
}

type NavigateFn = (path: string) => void;

const defaultNavigate: NavigateFn = (path) => {
  window.location.href = path;
};

let navigateImpl: NavigateFn = defaultNavigate;

/**
 * The single navigation seam (design Decision 4).
 *
 * jsdom throws "Not implemented: navigation" on a `window.location.href`
 * assignment, so tests replace this indirection via {@link setNavigateTo}
 * instead of observing a real navigation (Requirement 6.1).
 */
export function navigateTo(path: string): void {
  navigateImpl(path);
}

/**
 * Replace the navigation implementation (tests only).
 *
 * @param impl replacement, or `null`/omitted to restore the real navigation.
 */
export function setNavigateTo(impl?: NavigateFn | null): void {
  navigateImpl = impl ?? defaultNavigate;
}

/**
 * Record the location and send the user to the login screen — the shared
 * Session_Exit used by both API clients (Requirements 1.1, 1.3).
 *
 * Navigation is skipped when the user is already on `/login`, preserving the
 * existing guard in the 401 handlers (`api.ts`, `node-designer/api.ts`) that
 * keeps the login page from reloading itself.
 *
 * @param loc location to record; defaults to the current document location.
 */
export function redirectToLogin(loc?: LocationLike | null): void {
  const source = loc ?? currentLocation();
  saveAttemptedLocation(source);

  const pathname = source?.pathname ?? '';
  if (pathname === LOGIN_PATH) return;

  navigateTo(LOGIN_PATH);
}
