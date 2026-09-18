# Bugfix Requirements Document

## Introduction

When an Edge CV Portal session expires, the user is sent to the login screen
and, after signing back in, always lands on `/dashboard` instead of the page
they were on. Deep work is lost: the workflow they were editing, the device
detail tab they were reading, the tuning session they were labelling.

There are two defects behind the one symptom, and both are worth fixing:

* **(A) The attempted location is never recorded.** Three independent code
  paths send the user to `/login`, none of them saves where the user was, and
  the login screen navigates to a hard-coded constant.
* **(B) The session expires earlier than it needs to.** The ID token the API
  client sends is a copy in `localStorage` that is written once at page load
  and never refreshed, while Amplify still holds a valid refresh token. So
  the bounce to login typically happens on a session that could have been
  renewed silently, with no user-visible interruption at all.

Fixing (A) alone turns a bad interruption into a tolerable one. Fixing (B)
removes most of the interruptions. Both are in scope.

## Bug Analysis

### Current Behavior (Defect)

**Three exits to the login screen, none preserving the location.**

1. `edge-cv-portal/frontend/src/services/api.ts:1157-1166` — the dominant
   path in practice. Any 401 from any endpoint:

   ```ts
   if (response.status === 401) {
     console.error('Authentication failed - token may be expired');
     localStorage.removeItem('idToken');
     // Redirect to login page
     if (window.location.pathname !== '/login') {
       window.location.href = '/login';
     }
   }
   ```

   `window.location.href` is a **full document navigation**: React state,
   router history and any `location.state` are destroyed. Nothing about the
   current URL is retained.

2. `edge-cv-portal/frontend/src/pages/node-designer/api.ts:54-60` — the same
   block, duplicated verbatim in the Node_Designer client.

3. `edge-cv-portal/frontend/src/components/ProtectedRoute.tsx:23-25` — the
   router-side guard:

   ```tsx
   if (!isAuthenticated) {
     return <Navigate to="/login" replace />;
   }
   ```

   No `useLocation()`, no `state={{ from: location }}`. `replace` also drops
   the attempted URL from history, so even the back button cannot recover it.

**The login screen navigates to a constant.**
`edge-cv-portal/frontend/src/pages/Login.tsx:24-27, 40-42`:

```ts
const postLoginLanding = user?.role === 'DataLabeler' ? '/labeler' : '/dashboard';
...
useEffect(() => {
  if (isAuthenticated) navigate(postLoginLanding);
}, [isAuthenticated, postLoginLanding, navigate]);
```

The same constant is used by the new-password path (`Login.tsx:69`). Nothing
reads `location.state`, a query parameter, or storage. A repo-wide search for
`returnTo` / `redirectTo` / `sessionStorage` in `frontend/src` finds nothing
related to auth; the only `location.state` uses are unrelated feature
payloads (`CreateTraining.tsx:110`, `CreateLabelingJob.tsx:235`).

**The token is never refreshed.** `AuthContext.checkAuth`
(`contexts/AuthContext.tsx:86-108`) mirrors the ID token into
`localStorage['idToken']`, which both API clients read
(`api.ts:1136-1147`, `node-designer/api.ts:37-44`):

```ts
const session = await fetchAuthSession();
if (session.tokens?.idToken) {
  const idToken = session.tokens.idToken.toString();
  localStorage.setItem('idToken', idToken);
```

`fetchAuthSession()` — the only thing that renews the token — is called from
exactly one place, `checkAuth()`, which runs on mount, after `login()`, and
after `completeNewPassword()`. There is no timer, no pre-request refresh, and
no `forceRefresh`. So the mirrored token ages out (Cognito default 1 hour)
while Amplify's refresh token is typically still valid, and the next API call
401s. `checkAuth()`'s own catch (`AuthContext.tsx:105-107`) clears the mirror
and does not navigate, so the redirect always comes from the API client.

There is **no idle/inactivity timeout** anywhere in the app; "session
timeout" as the user experiences it is purely ID-token expiry against a stale
mirror.

### Expected Behavior (Correct)

* When a session ends and the user is sent to `/login`, the location they
  were on (path, query string and hash) is remembered.
* After signing back in — including through the new-password challenge —
  they land back on that location.
* When the ID token is merely stale but the Amplify session can still be
  refreshed, the request is retried transparently and the user is never sent
  to the login screen at all.
* Signing out deliberately lands on the default page, not the last page.
* A remembered location can never be used to navigate somewhere unsafe.

### Unchanged Behavior (Regression Prevention)

* A fresh sign-in with no remembered location still lands on `/dashboard`,
  and a `DataLabeler` still lands on `/labeler` (`Login.tsx:27`,
  dda-data-labeling Req 2.2/2.8).
* `DataLabelerRedirect` (`components/DataLabelerRedirect.tsx:38-42`) and every
  `RequireRole` gate (`App.tsx:120-243`) still apply to a restored location;
  a user whose role no longer permits the remembered page is redirected by
  those guards as they are today.
* The route table, `BrowserRouter` usage and the `/dashboard` index redirect
  (`App.tsx:97`) are unchanged.
* `AuthContext`'s public interface (`AuthContextType`) keeps its current
  members, so the components and tests that consume it are unaffected.
* API error handling other than 401 is untouched: the `ApiError` envelope
  (structured `{error:{code,message,details}}` and simple `{error: string}`)
  keeps its current shape and 403 keeps surfacing to callers rather than
  triggering a sign-out.
* The global loading bar's `beginRequest` / `endRequest` accounting stays
  balanced across the new retry path.

## Requirements

### Requirement 1 — Remember the attempted location on every exit to login

**User Story:** As a portal user, I want the app to remember the page I was
on when my session ended.

#### Acceptance Criteria

1. WHEN the API client receives a 401 and redirects to `/login` THEN it SHALL
   first record the current location as `pathname + search + hash`.
2. WHEN `ProtectedRoute` redirects an unauthenticated user THEN it SHALL
   record the attempted location the same way.
3. WHEN the Node_Designer API client receives a 401 THEN it SHALL behave
   identically to the main client (one shared implementation, not a third
   copy).
4. WHEN the recorded location would be `/login` itself THEN nothing SHALL be
   recorded.
5. WHEN a location is recorded THEN it SHALL survive a full document
   navigation, since the API-client path is a `window.location` assignment.
6. WHEN a location is already recorded and another 401 occurs before sign-in
   THEN the first (deepest, earliest) location SHALL be kept rather than
   overwritten by a later redirect.

### Requirement 2 — Return to it after signing in

**User Story:** As a portal user, I want to be put back where I was after
signing back in.

#### Acceptance Criteria

1. WHEN sign-in completes AND a valid remembered location exists THEN the app
   SHALL navigate to that location instead of the default landing page.
2. WHEN sign-in completes through the new-password challenge THEN the same
   restoration SHALL apply.
3. WHEN no remembered location exists THEN the app SHALL navigate to
   `/dashboard`, or `/labeler` for a `DataLabeler`.
4. WHEN a remembered location is consumed THEN it SHALL be cleared, so a
   later ordinary sign-in does not resurrect a stale page.
5. WHEN the restored location's page is not permitted for the user's role
   THEN the existing role guards SHALL redirect as they do today, and no
   navigation loop SHALL occur.
6. WHEN an already-authenticated user visits `/login` directly THEN today's
   immediate redirect behavior SHALL be preserved.

### Requirement 3 — A remembered location can never be unsafe

**User Story:** As the portal owner, I want this feature not to introduce an
open redirect.

#### Acceptance Criteria

1. WHEN a candidate location is consumed THEN it SHALL be accepted only if it
   is a same-origin relative path: it begins with exactly one `/`, and is not
   `//host`, not scheme-bearing (`http:`, `https:`, `javascript:`, `data:`),
   and not protocol-relative.
2. WHEN a candidate fails validation THEN it SHALL be discarded and the
   default landing page used.
3. WHEN a candidate exceeds a documented length bound THEN it SHALL be
   discarded.
4. WHEN validation runs THEN it SHALL be a pure function, tested directly
   against adversarial inputs.

### Requirement 4 — Deliberate sign-out forgets the location

**User Story:** As a portal user, when I sign out on purpose I expect a clean
start.

#### Acceptance Criteria

1. WHEN the user signs out via the header menu
   (`components/Layout.tsx:227-229`) THEN any remembered location SHALL be
   cleared before navigating to `/login`.
2. WHEN that user signs back in THEN they SHALL land on the default landing
   page.

### Requirement 5 — Refresh a stale token instead of bouncing to login

**User Story:** As a portal user, I do not want to be signed out while my
session is still renewable.

#### Acceptance Criteria

1. WHEN a request returns 401 AND the Amplify session can be refreshed THEN
   the client SHALL refresh the token, update the stored copy, retry the
   original request exactly once, and SHALL NOT navigate to `/login`.
2. WHEN the refresh fails or the retried request also returns 401 THEN the
   client SHALL record the location and redirect to `/login` as in
   Requirement 1.
3. WHEN several requests receive 401 concurrently THEN at most one refresh
   SHALL be in flight, and all waiters SHALL use its result.
4. WHEN a request is retried THEN it SHALL be retried at most once, so no
   retry loop is possible.
5. WHEN a retry occurs THEN the in-flight request accounting used by the
   global loading bar SHALL stay balanced.
6. WHEN a refresh succeeds THEN the caller SHALL receive the retried
   response, and non-401 behavior SHALL be unchanged.

### Requirement 6 — Test coverage

**User Story:** As a maintainer, I want this behavior pinned by tests.

#### Acceptance Criteria

1. WHEN the redirect helper is tested THEN the save/consume/clear cycle and
   the validator SHALL be covered directly, without depending on a real
   `window.location` assignment (jsdom cannot perform navigation).
2. WHEN `ProtectedRoute` is tested THEN its redirect SHALL be asserted with
   the `Navigate`-spy + `MemoryRouter` pattern already used by
   `components/RequireRole.test.tsx`.
3. WHEN `Login` is tested THEN restoration, the default landing, the
   `DataLabeler` landing, and the new-password path SHALL be covered.
4. WHEN the refresh-and-retry path is tested THEN success, failure,
   concurrency, and the single-retry bound SHALL be covered with a stubbed
   `fetchAuthSession`.
