# Portal Session Expiry — Return To Page — Bugfix Design

## Overview

One new module owns the whole behavior, and the four existing exit/entry
points delegate to it:

`edge-cv-portal/frontend/src/services/sessionRedirect.ts`

* `saveAttemptedLocation(loc?)` — record where the user was, first-write-wins
* `takeAttemptedLocation()` — read **and clear**, returning a validated path
  or `null`
* `clearAttemptedLocation()` — forget it (deliberate sign-out)
* `isSafeInternalPath(candidate)` — the pure validator
* `redirectToLogin(loc?)` — save, then perform the document navigation

Storage is `sessionStorage` under one key, because the dominant exit path
(`api.ts`) is a `window.location.href` assignment: a full document load,
which router `location.state` does not survive.

Separately, `api.ts` gains a **refresh-then-retry-once** step in front of the
redirect, so a merely stale token never reaches the login screen.

## Glossary

* **Attempted_Location** — the `pathname + search + hash` the user was on (or
  tried to reach) when the session ended.
* **Landing_Location** — today's post-login default: `/dashboard`, or
  `/labeler` for a `DataLabeler` (`Login.tsx:27`).
* **Session_Exit** — any of the three code paths that send the user to
  `/login`: the two API clients' 401 handlers and `ProtectedRoute`.
* **Safe_Internal_Path** — a candidate accepted by `isSafeInternalPath`.
* **Silent_Refresh** — `fetchAuthSession({ forceRefresh: true })` performed in
  response to a 401, followed by one retry of the original request.

## Bug Details

Four sites, and what each is missing:

| Site | Current | Missing |
|---|---|---|
| `services/api.ts:1157-1166` | `window.location.href = '/login'` | save; and it should refresh first |
| `pages/node-designer/api.ts:54-60` | same block, duplicated | save; and de-duplication |
| `components/ProtectedRoute.tsx:23-25` | `<Navigate to="/login" replace />` | `state`/save |
| `pages/Login.tsx:27, 40-42, 69` | `navigate(postLoginLanding)` | consume |

The token staleness that triggers most of these: `AuthContext.checkAuth`
(`contexts/AuthContext.tsx:86-108`) writes `localStorage['idToken']` once;
`fetchAuthSession()` has exactly one call site (`AuthContext.tsx:88`), reached
only on mount / `login()` / `completeNewPassword()`.

## Expected Behavior

```
request → 401
        → Silent_Refresh (at most one in flight)
            ├─ success → update stored token → retry once
            │              ├─ ok    → return response          (user sees nothing)
            │              └─ 401   → save Attempted_Location → /login
            └─ failure → save Attempted_Location → /login

/login → sign-in ok → takeAttemptedLocation()
            ├─ Safe_Internal_Path → navigate there
            └─ null / unsafe      → navigate Landing_Location
```

## Design Decisions

### Decision 1 — `sessionStorage`, not `location.state`, not a query parameter

`location.state` cannot work: `api.ts` performs a document navigation, which
destroys router state. That rules out the classic
`<Navigate state={{ from: location }}/>` pattern as the *primary* mechanism
(it is still added to `ProtectedRoute` for free, but the shared module is what
both paths actually rely on).

A `?redirect=` parameter on `/login` would survive, but it puts an
attacker-influenceable, user-visible path into the URL, and it is exactly the
shape that becomes an open redirect when validation is imperfect. It would
also leak deep links (including ids) into browser history and any access log.

`sessionStorage` is per-tab, survives a document load, does not travel in
URLs, and clears itself when the tab closes. Its one limitation — a link
opened in a **new** tab has no remembered location — is acceptable: that tab
had no prior page to return to.

Key: `dda.portal.returnTo`. One key, one string value.

### Decision 2 — First write wins

A single expiry commonly produces several 401s at once (a page firing three
parallel queries). The first one to notice holds the location the user
actually cared about; later ones may fire after the app has already begun
tearing down. `saveAttemptedLocation` therefore no-ops when a value is already
present, and `/login` is never saved.

### Decision 3 — Validate on consumption, not only on save

The validator runs when the value is *used*, so a value planted directly into
`sessionStorage` cannot navigate anywhere unsafe:

```
accept iff:
  starts with exactly one '/'         (rejects '//evil.com', 'http://…', 'javascript:…')
  and does not start with '/\'        (backslash variant of protocol-relative)
  and contains no control characters
  and length <= 2048
  and the path portion is not '/login'
```

Pure function, no DOM access, adversarially tested. Nothing is ever
interpolated into HTML, so encoding is not a concern here; only navigation
targets are.

### Decision 4 — `redirectToLogin()` is the single injectable seam

Both API clients call it instead of touching `window.location`. It takes an
optional location so tests can drive it, and performs the navigation through a
tiny indirection (`navigateTo`) that tests replace — jsdom throws
"Not implemented: navigation" on `window.location.href` assignment, so this
indirection is what makes Requirement 6.1 testable at all. It also
de-duplicates the two copies of the 401 block.

### Decision 5 — `Login` consumes once, at the same point it navigates today

`takeAttemptedLocation()` is called inside the existing `isAuthenticated`
effect (`Login.tsx:40-42`) and the new-password handler (`Login.tsx:69`), and
its result replaces `postLoginLanding` when non-null. Read-and-clear in one
operation, so a subsequent ordinary sign-in cannot resurrect a stale page, and
the effect cannot navigate twice to different places.

The restored navigation is a plain `navigate(path)`. It lands inside the
protected tree, so `ProtectedRoute` → `DataLabelerRedirect` → `RequireRole`
all still run. A user whose role no longer allows the page gets today's
redirect to `/dashboard`. That is correct, and it is why no permission
pre-check is needed here. Loop safety comes from the consume-once semantics:
after restoration the value is gone, so a bounce cannot re-restore.

### Decision 6 — Silent_Refresh lives in the API client, guarded by a shared promise

The refresh belongs where the 401 is observed. `api.ts` imports
`fetchAuthSession` from `aws-amplify/auth` directly (as `AuthContext` already
does) rather than reaching into React context, since `ApiService` is not a
component.

A module-level `refreshInFlight: Promise<string|null> | null` collapses
concurrent refreshes (Requirement 5.3). The retry is attempted only when the
request has not already been retried, tracked by a local flag rather than a
counter, so one retry is structurally the maximum (5.4).

`beginRequest()` / `endRequest()` currently wrap the whole `request` body in
`try/finally`; the retry is performed **inside** that same span so the
accounting stays balanced (5.5) and the loading bar stays visible across the
refresh.

Refresh failure is not reported as an application error: the user is
redirected, which is the existing observable behavior.

**Not chosen:** a proactive timer or a pre-request expiry check. Both are
more code and more clock-skew surface than reacting to the authoritative
signal (a real 401). The `AnnotationCanvas.tsx:295-381` presigned-URL
expiry-margin refresh is the in-repo precedent for the proactive style if this
ever proves insufficient.

### Decision 7 — Node_Designer client delegates, gaining refresh for free

`pages/node-designer/api.ts` replaces its duplicated block with the shared
helper. Its 401 path gets the same refresh-and-retry, which is a small
behavior improvement, and there is then exactly one implementation to reason
about.

## Correctness Properties

Each gets exactly one property-based test at ≥ 100 examples (fast-check is
already configured globally at `numRuns: 25` in `src/test/setup.ts`; these
tests set `numRuns: 100` explicitly), tagged
`Feature: portal-session-expiry-return-to-page, Property {n}: {text}`.

**Property 1: Only safe internal paths are ever returned.**
For any generated candidate string — including `//host`, `/\host`,
`http://`, `https://`, `javascript:`, `data:`, control characters,
over-length strings, `/login` with and without query — `takeAttemptedLocation`
returns either `null` or a string beginning with exactly one `/` that is not
`/login`, and the store is empty afterwards.
*Validates: 3.1, 3.2, 3.3, 3.4, 2.4*

**Property 2: Save/consume round-trips any safe location exactly once.**
For any generated safe `pathname`/`search`/`hash` triple, one save followed by
one consume returns the exact concatenation, a second consume returns `null`,
and a second save before the consume does not overwrite the first.
*Validates: 1.1, 1.5, 1.6, 2.4*

**Property 3: The post-login destination is the remembered location when one
exists, else the role's landing page.**
For any generated (remembered value, role) pair, the destination `Login`
navigates to is the validated remembered location when it is safe, otherwise
`/labeler` for `DataLabeler` and `/dashboard` for every other role.
*Validates: 2.1, 2.3, 3.2*

**Property 4: A 401 never redirects while the session is refreshable, and
retries at most once.**
For any generated sequence of responses and refresh outcomes, the client
performs at most one refresh per 401 batch and at most one retry per request;
it redirects exactly when the refresh fails or the retry also 401s; and the
in-flight request count returns to its starting value in every case.
*Validates: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6*

## Fix Implementation

### New — `src/services/sessionRedirect.ts`

```
const KEY = 'dda.portal.returnTo';
const MAX = 2048;

isSafeInternalPath(candidate): boolean      // Decision 3
saveAttemptedLocation(loc = window.location): void   // first-write-wins, skips /login
takeAttemptedLocation(): string | null     // read + clear + validate
clearAttemptedLocation(): void
redirectToLogin(loc?): void                // save, then navigateTo('/login')
navigateTo(path)                           // the test seam (Decision 4)
```

All storage access is wrapped so a `SecurityError` (storage disabled) degrades
to "no remembered location" rather than throwing into the 401 path.

### `src/services/api.ts`

* Extract the 401 branch into a private `handleUnauthorized()` that performs
  Silent_Refresh, retries once, and otherwise calls `redirectToLogin()`.
* Module-level `refreshInFlight` promise; on success write the new token to
  `localStorage['idToken']` (keeping the existing mirror contract that
  `AuthContext` also writes) and re-issue the request with fresh headers.
* Retry happens inside the existing `beginRequest`/`endRequest` span.
* Non-401 handling, the `ApiError` construction, and both error envelopes are
  untouched.

### `src/pages/node-designer/api.ts`

Replace lines 54-60 with the shared path (Decision 7).

### `src/components/ProtectedRoute.tsx`

```tsx
const location = useLocation();
if (!isAuthenticated) {
  saveAttemptedLocation(location);
  return <Navigate to="/login" replace state={{ from: location }} />;
}
```

The `state` is belt-and-braces for the in-router case; the saved value is what
`Login` actually reads, so both paths behave identically.

### `src/pages/Login.tsx`

`postLoginLanding` stays as the fallback. The `isAuthenticated` effect and
`handleNewPassword` resolve their destination as
`takeAttemptedLocation() ?? postLoginLanding`.

### `src/components/Layout.tsx`

`clearAttemptedLocation()` before `navigate('/login')` in the logout item
handler (lines 227-229).

## Testing Strategy

Frontend tests: vitest + `@testing-library/react` + fast-check, jsdom
environment, `src/test/setup.ts` (`vite.config.ts:54-58`). `package.json`
defines no `test` script, so suites run as
`npx vitest run <path>` from `edge-cv-portal/frontend`; type-check with
`npx tsc --noEmit`.

* **Exploration** (`src/services/sessionRedirect.exploration.test.ts`) — must
  FAIL on unfixed code: asserts that after a simulated 401 the attempted
  location is recoverable, and that `Login` navigates to it.
* **Properties** — Property 1 and 2 in
  `src/services/sessionRedirect.property.test.ts`; Property 3 in
  `src/pages/Login.returnTo.property.test.tsx`; Property 4 in
  `src/services/api.refreshRetry.property.test.ts`.
* **Units**
  * `sessionRedirect`: adversarial validator table; first-write-wins;
    consume-clears; storage-disabled degradation.
  * `ProtectedRoute.test.tsx` (new): unauthenticated → `Navigate` to
    `/login` with `replace`, and the location saved; authenticated →
    children; loading → spinner. Uses the `vi.mock('react-router-dom')`
    `Navigate`-spy + `MemoryRouter` pattern from
    `components/RequireRole.test.tsx:22-43, 73-87, 117`.
  * `Login.returnTo.test.tsx` (new): restores a remembered location;
    defaults to `/dashboard`; `/labeler` for `DataLabeler`; new-password path
    restores; already-authenticated visit to `/login` still redirects
    immediately.
  * `api.refreshRetry.test.ts` (new): refresh success → no redirect, one
    retry, response returned; refresh failure → redirect with the location
    saved; two concurrent 401s → one refresh; retried request 401 → redirect,
    no second retry; loading-bar count balanced.
  * `Layout` logout clears the remembered location.
* **Regression** — run `components/RequireRole.test.tsx` and the
  `DataLabelerRedirect` coverage unmodified to prove the guard interaction in
  the Unchanged Behavior list; full `npx vitest run` at the end.
* No test asserts anything about a real browser navigation; the `navigateTo`
  seam (Decision 4) is what is observed.
