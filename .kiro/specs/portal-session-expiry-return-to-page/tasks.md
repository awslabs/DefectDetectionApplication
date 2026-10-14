# Implementation Plan: Portal Session Expiry — Return To Page

## Overview

Two defects behind one symptom (bugfix.md): the three Session_Exits never
record where the user was and `Login` navigates to a hard-coded constant, and
the ID token mirror in `localStorage` is never refreshed so the bounce to
login usually happens on a still-renewable session.

One new module (`src/services/sessionRedirect.ts`) owns save/consume/validate
and the login navigation seam; the four existing sites delegate to it; and
`api.ts` gains refresh-then-retry-once in front of the redirect. Frontend
only — no backend, no infrastructure, no device code, so no component build
and no preservation rebaseline is owed.

Run from `edge-cv-portal/frontend`: `npx vitest run <path>` (there is no
`test` script in `package.json`) and `npx tsc --noEmit`.

## Tasks

- [x] 1. Capture the defect
  - [x] 1.1 Write the exploration test that fails on unfixed code
    - `src/services/sessionRedirect.exploration.test.ts`: simulate the 401
      path from a deep location (e.g. `/workflows/builder/abc?tab=nodes`) and
      assert the location is recoverable afterwards; assert `Login` would
      navigate to it rather than `/dashboard`
    - Both cases FAIL today (nothing is recorded, `Login` uses a constant)
    - _Requirements: 1.1, 2.1_
    - **OUTCOME**: Added `edge-cv-portal/frontend/src/services/sessionRedirect.exploration.test.ts`
      with the two cases; `npx vitest run` on it shows 1 file / 2 tests, both
      FAILING for exactly the defect (case 1: `sessionStorage['dda.portal.returnTo']`
      is `null` after a 401 from `/workflows/builder/abc?tab=nodes`; case 2:
      `Login` navigates to `/dashboard` instead of the remembered location).
      `npx tsc --noEmit` is clean and `src/components/RequireRole.test.tsx`
      still passes (8/8). Decisions: the test observes the recovery channel via
      the `dda.portal.returnTo` sessionStorage key (design Decision 1) rather
      than importing the not-yet-existing `sessionRedirect` module, so it fails
      on assertions rather than on module resolution; `window.location` is
      replaced with a writable stub because jsdom cannot navigate (design
      Decision 4); and `aws-amplify/auth.fetchAuthSession` is pre-mocked to
      reject so the case stays valid once the task 5.1 Silent_Refresh lands
      (non-refreshable session must still save + redirect). No preservation-tracked
      file touched, so no rebaseline is owed. Nothing deferred.

- [x] 2. Implement the shared redirect module
  - [x] 2.1 Create `src/services/sessionRedirect.ts`
    - `isSafeInternalPath` per design Decision 3 (single leading `/`, rejects
      `//host`, `/\host`, scheme-bearing, control characters, > 2048, and
      `/login`); `saveAttemptedLocation` (first-write-wins, skips `/login`,
      stores `pathname + search + hash`); `takeAttemptedLocation`
      (read + clear + validate); `clearAttemptedLocation`; `redirectToLogin`;
      the `navigateTo` test seam; storage access wrapped so a disabled-storage
      `SecurityError` degrades to "no remembered location"
    - _Requirements: 1.1, 1.4, 1.5, 1.6, 2.4, 3.1, 3.2, 3.3, 3.4_
    - **OUTCOME**: Added `edge-cv-portal/frontend/src/services/sessionRedirect.ts`
      with the full designed surface: `isSafeInternalPath` (exactly one leading
      `/`, rejects `//host`, `/\host`, scheme-bearing and non-string input,
      control characters, > 2048 chars, and any `/login` path portion),
      `saveAttemptedLocation` (first-write-wins, stores `pathname + search +
      hash`, silently skips anything the validator rejects incl. `/login`),
      `takeAttemptedLocation` (read + clear-always + validate),
      `clearAttemptedLocation`, `redirectToLogin`, the `navigateTo` seam with a
      `setNavigateTo` injector, and every `sessionStorage` touch (including the
      property access itself) wrapped so a `SecurityError` degrades to "no
      remembered location". No call site was wired yet (tasks 3.1-3.3), so this
      is additive only. Verification: `npx tsc --noEmit` clean; a throwaway
      6-case sanity suite covering the validator table, round-trip,
      first-write-wins, consume-clears, planted-unsafe discard, `/login` never
      saved, the redirect seam and storage-disabled degradation passed 6/6 and
      was then deleted (the durable tests are the optional tasks 2.2/2.3);
      `src/components/RequireRole.test.tsx` still 8/8; the task 1.1 exploration
      suite still fails 2/2 as expected since `api.ts` and `Login` are untouched
      until wave 2. Decisions: added `setNavigateTo(impl?)` as the way tests
      replace the `navigateTo` indirection (Vitest cannot `vi.spyOn` an ESM
      export that the module calls internally); `redirectToLogin` skips the
      navigation when the resolved location is already `/login`, preserving the
      existing `window.location.pathname !== '/login'` guard in both 401
      handlers; `/login/` and `/LOGIN` are rejected too (conservative superset
      of Requirement 1.4, loop safety for 2.5). Frontend-only, so no
      preservation-tracked file changed and no rebaseline is owed.

  - [x]* 2.2 Property tests for the module
    - `src/services/sessionRedirect.property.test.ts`
    - **Property 1: Only safe internal paths are ever returned** — _Validates: 3.1, 3.2, 3.3, 3.4, 2.4_
    - **Property 2: Save/consume round-trips any safe location exactly once** — _Validates: 1.1, 1.5, 1.6, 2.4_
    - **OUTCOME**: Added `edge-cv-portal/frontend/src/services/sessionRedirect.property.test.ts`
      with one fast-check property per design property, both at an explicit
      `numRuns: 100` and tagged
      `Feature: portal-session-expiry-return-to-page, Property {n}: {text}`.
      Property 1 drives adversarial candidates (protocol-relative `//host`,
      `/\host`, scheme-bearing incl. mixed case `javascript:`/`data:`/`file:`,
      spliced control characters, over-length and exactly-at-2048, `/login`
      with query/hash/case/trailing-slash variants, unrooted/empty, plus wholly
      arbitrary strings) through **both** routes into the store — planted
      directly into `sessionStorage` and offered to `saveAttemptedLocation` —
      and asserts `takeAttemptedLocation` returns only `null` or the verbatim
      safe path, that the store is empty afterwards either way, and that
      `isSafeInternalPath` is pure (stable answer, no storage side effects).
      Property 2 asserts save→consume returns `pathname + search + hash`
      exactly, first-write-wins against two later saves, a second consume is
      `null`, and the freed slot is reusable. Ran
      `npx vitest run src/services/sessionRedirect.property.test.ts
      src/components/RequireRole.test.tsx`: 2 files / 10 tests passed (2
      property + 8 regression); `npx tsc --noEmit` clean; the security
      preservation guards still 4 passed / 3 skipped (nothing tracked was
      touched — frontend only, so no rebaseline is owed). Decision: the
      expectation oracle is an independent regex-first restatement of design
      Decision 3 rather than a call into the module, and the adversarial
      families additionally carry their own safe/unsafe classification so the
      direction is pinned even if the oracle had a hole; to prove the property
      is not vacuous, seven temporary mutations of `sessionRedirect.ts` were
      each shown to fail it (dropped `//` guard, no clear-on-consume,
      last-write-wins, `/login` allowed, no length bound, no control-char
      check, and an over-strict "reject any query string") and the module was
      restored byte-identical. The task 1.1 exploration suite still fails 2/2
      as designed (call sites are wave 2). Nothing deferred.

  - [x]* 2.3 Unit tests for the module
    - Adversarial validator table; first-write-wins; consume-clears;
      storage-disabled degradation; `/login` never saved
    - _Requirements: 1.4, 1.6, 3.1_
    - **OUTCOME**: Added `edge-cv-portal/frontend/src/services/sessionRedirect.test.ts`
      (86 example-based tests, all passing) covering the five items the task
      lists: an adversarial validator table (15 accepted rows incl. `/`,
      deep query+hash, percent escapes, spaces, non-ASCII, a backslash away
      from index 1, `/loginish`, `/user/login` and exactly-at-2048; 45
      rejected rows incl. protocol-relative `//host` and `/\host`, nine
      scheme-bearing forms in mixed case, six control characters,
      over-length, seven `/login` spellings, unrooted/relative inputs and six
      non-string types) plus a purity case asserting a stable answer and zero
      storage side effects; first-write-wins across a 401 burst and against a
      value planted by a previous document; consume-clears (second consume is
      `null`, a planted unsafe value is discarded *and* removed, the slot is
      reusable); storage-disabled degradation for four distinct failure modes
      (property access throwing `SecurityError`, a store whose every method
      throws, a missing store, and a quota-exceeded `setItem`) including that
      `redirectToLogin` still navigates when storage is dead; and `/login`
      never being saved in any spelling. Also pinned `clearAttemptedLocation`
      (leaves unrelated keys alone) and the `navigateTo` seam. Ran
      `npx vitest run src/services/sessionRedirect.test.ts
      src/services/sessionRedirect.property.test.ts
      src/components/RequireRole.test.tsx`: 3 files / 96 tests passed (86 new
      + 2 property + 8 regression); `npx tsc --noEmit` clean; the security
      preservation guards still 4 passed / 3 skipped (frontend-only change, so
      nothing tracked was touched and no rebaseline is owed). Decisions: the
      document location is moved with `history.replaceState` rather than by
      stubbing `window.location`, which jsdom supports and which keeps the
      default-argument paths (`saveAttemptedLocation()`, `redirectToLogin()`)
      honest; storage failure is simulated by redefining
      `globalThis.sessionStorage` with a throwing getter, restored from its
      original descriptor after every test. To prove the suite is not vacuous,
      eight temporary mutations of `sessionRedirect.ts` were each shown to
      fail it (no storage guard → 2 failures, no first-write-wins → 3, no
      clear-on-consume → 4, `/login` allowed → 10, no length bound → 4, no
      control-char check → 8, no backslash guard → 4, no already-on-`/login`
      skip in `redirectToLogin` → 1) and the module was restored
      byte-identical (sha256 verified). Note: `src/services/api.ts` shows as
      modified in this tree from a sibling task, not from this one — nothing
      here imports it. Nothing deferred.

- [x] 3. Wire the three Session_Exits and the login entry
  - [x] 3.1 `ProtectedRoute` records the attempted location
    - `useLocation()`; `saveAttemptedLocation(location)` before the redirect;
      keep `<Navigate to="/login" replace>` and add `state={{ from: location }}`
    - _Requirements: 1.2_
    - **OUTCOME**: `src/components/ProtectedRoute.tsx` now calls `useLocation()`
      and `saveAttemptedLocation(location)` in the unauthenticated branch before
      returning `<Navigate to="/login" replace state={{ from: location }} />`;
      the loading-spinner and authenticated branches are untouched, so nothing is
      recorded while auth is still resolving. Verification: `npx tsc --noEmit`
      clean, and `npx vitest run` over a throwaway 4-case ProtectedRoute suite
      plus the existing `sessionRedirect` unit/property suites and
      `RequireRole.test.tsx` passed 4 files / 100 tests (4 throwaway + 86 + 2 +
      8 regression); the throwaway was proven non-vacuous (removing the
      `saveAttemptedLocation` call failed 2 of its 4 cases) and then deleted,
      since the durable `ProtectedRoute.test.tsx` is optional task 3.4.
      Decisions: the save happens during render as design.md prescribes (it is
      an idempotent, first-write-wins storage write in the branch that
      immediately unmounts, so a `useEffect` would run after `Navigate` had
      already redirected); `state={{ from: location }}` is added as designed
      even though `Login` reads the stored value. The task 1.1 exploration suite
      still fails 2/2 as expected (its cases exercise the `api.ts` 401 path and
      `Login`, which land in tasks 3.2/5.1). Security preservation guards still
      4 passed / 3 skipped — frontend-only change, no preservation-tracked file
      touched, so no rebaseline is owed. Nothing deferred.

  - [x] 3.2 `Login` consumes it
    - `takeAttemptedLocation() ?? postLoginLanding` in the `isAuthenticated`
      effect (`Login.tsx:40-42`) and in `handleNewPassword` (`Login.tsx:69`);
      `postLoginLanding` unchanged as the fallback
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
    - **OUTCOME**: `src/pages/Login.tsx` now resolves its post-sign-in
      destination as the remembered Attempted_Location when one validates, else
      the unchanged `postLoginLanding` fallback — at both sites the task names:
      the `isAuthenticated` effect and after `completeNewPassword` in
      `handleNewPassword`. Decision (a deliberate deviation from the literal
      design snippet): the two sites share one `resolveDestination()` helper
      that consumes `takeAttemptedLocation()` at most once per mount via a
      `useRef` memo, instead of each calling `takeAttemptedLocation() ??
      postLoginLanding` independently. The literal form is racy —
      `completeNewPassword` also flips `isAuthenticated`, so the handler and the
      effect both navigate, and whichever runs second finds the store already
      consumed and navigates to `/dashboard`, clobbering the restoration. I
      verified this is a real regression, not a theoretical one: with the
      literal form the added ordering case fails with two destinations
      (`['/tuning/session-7?step=2', '/dashboard']`), and design Decision 5
      explicitly requires that "the effect cannot navigate twice to different
      places". Storage-level consume-once semantics (Requirement 2.4) are
      unchanged — the value is still read+cleared on the first resolve — and the
      effect's dependency on `postLoginLanding` is preserved, so the late
      `user.role` arrival still re-lands a `DataLabeler` on `/labeler`.
      Verification: `npx tsc --noEmit` clean; a throwaway 6-case Login suite
      (remembered deep location with query+hash restored and cleared; default
      `/dashboard`; `/labeler` for `DataLabeler`; planted `//evil.example.com`
      discarded and removed; new-password challenge restores; the
      new-password-plus-`isAuthenticated` ordering case) passed 6/6, and with
      the existing `sessionRedirect` unit + property suites and
      `RequireRole.test.tsx`: 4 files / 102 tests passed. Non-vacuity proven by
      mutation — reverting to `navigate(postLoginLanding)` fails 3 of the 6
      cases, and the literal design form fails the ordering case; `Login.tsx`
      was restored byte-identical (sha256 verified) after each. The throwaway
      was then deleted since the durable `Login.returnTo.test.tsx` and Property
      3 are optional task 3.4. Task 1.1's exploration suite has now half
      flipped: its `Login` case PASSES, its `api.ts` 401-save case still fails
      (that lands in task 5.1). Security preservation guards still 4 passed / 3
      skipped — frontend-only change, nothing preservation-tracked touched, so
      no rebaseline is owed. Nothing deferred.

  - [x] 3.3 Deliberate sign-out forgets it
    - `clearAttemptedLocation()` before `navigate('/login')` in
      `components/Layout.tsx:227-229`
    - _Requirements: 4.1, 4.2_
    - **OUTCOME**: `src/components/Layout.tsx` now imports
      `clearAttemptedLocation` from `../services/sessionRedirect` and calls it
      in the `logout` dropdown-item handler, between `await logout()` and
      `navigate('/login')`, so a deliberate sign-out forgets any remembered
      Attempted_Location and the next sign-in lands on the default landing page
      (Requirements 4.1, 4.2). Decision: the call sits *after* `await logout()`
      rather than at the top of the handler — `logout()` sets `user` to `null`,
      which flips `isAuthenticated` while the router is still on the protected
      route, and `ProtectedRoute` (task 3.1) would then re-record the location a
      clear-first placement had just removed. I verified this is real, not
      theoretical: with the clear moved ahead of `await logout()` the ordering
      case fails, and with the call removed entirely 2 of 4 cases fail; the file
      was restored byte-identical (sha256 verified) after each mutation.
      Verification: `npx tsc --noEmit` clean; `npx vitest run` over a throwaway
      4-case Layout sign-out suite plus `Layout.navigationItems.test.tsx`,
      `workflow-tuning/navGating.test.tsx`, the two `sessionRedirect` suites,
      `RequireRole.test.tsx` and `buildsSurfaceRouteSidebar.integration.test.tsx`
      passed 7 files / 133 tests. The throwaway drove the real handler through a
      stubbed `TopNavigation` that forwards a click to the component's own
      `onItemClick` (Cloudscape collapses its utilities in jsdom's zero-width
      layout, so the menu item itself is unclickable there) and was then deleted,
      since the durable `Layout` logout test is optional task 3.4 — that stub
      pattern is the one to reuse. Task 1.1's exploration suite is unchanged at
      1 pass (`Login` restores) / 1 fail (the `api.ts` 401 save, task 5.1).
      Security preservation guards still 4 passed / 3 skipped; frontend-only
      change, so no preservation-tracked file was touched and no rebaseline is
      owed. Nothing deferred.

  - [x]* 3.4 Property and unit tests for the routing halves
    - `src/pages/Login.returnTo.property.test.tsx` —
      **Property 3: The post-login destination is the remembered location when
      one exists, else the role's landing page** — _Validates: 2.1, 2.3, 3.2_
    - `src/components/ProtectedRoute.test.tsx` (new): unauthenticated →
      `Navigate` to `/login` with `replace` **and** the location saved;
      authenticated → children; loading → spinner. Use the
      `vi.mock('react-router-dom')` `Navigate`-spy + `MemoryRouter` pattern
      from `components/RequireRole.test.tsx:22-43, 73-87, 117`
    - `src/pages/Login.returnTo.test.tsx` (new): restores; defaults to
      `/dashboard`; `/labeler` for `DataLabeler`; new-password path restores;
      already-authenticated visit to `/login` still redirects immediately
    - `Layout` logout clears the remembered location
    - _Requirements: 1.2, 2.1, 2.2, 2.3, 2.6, 4.1_
    - **OUTCOME**: Added the four durable suites the task lists —
      `src/pages/Login.returnTo.property.test.tsx` (Property 3 at an explicit
      `numRuns: 100`, tagged `Feature: portal-session-expiry-return-to-page,
      Property 3: …`, crossing remembered-value families — safe generated and
      known deep locations, protocol-relative/backslash/scheme-bearing,
      control-character, over-length, seven `/login` spellings, unrooted,
      wholly arbitrary text, and "nothing remembered" — against all six roles
      plus an unresolved role, asserting exactly one navigation to the
      validated remembered location else `/labeler`/`/dashboard` and an empty
      store either way), `src/components/ProtectedRoute.test.tsx` (13 tests:
      redirect to `/login` with `replace` via the RequireRole `Navigate`-spy +
      `MemoryRouter` pattern, the location saved as `pathname + search + hash`,
      the `state={{ from }}` payload, four verbatim-record rows,
      first-write-wins, `/login` never recorded, authenticated → children with
      nothing saved or disturbed, loading → spinner with nothing recorded),
      `src/pages/Login.returnTo.test.tsx` (25 tests: restores, clears,
      no-resurrection on a second sign-in, DataLabeler restore, seven unsafe
      planted values plus over-length discarded *and* removed, `/dashboard`
      and `/labeler` defaults for every role, no navigation while
      unauthenticated, the new-password path restoring and defaulting, the
      single-destination ordering case, and the unchanged immediate
      `/login` redirect), and `src/components/Layout.logout.test.tsx` (5
      tests: clear + `navigate('/login')`, the clear-after-`logout()`-and-
      before-`navigate()` ordering pinned via an event log, no-op when nothing
      was remembered, unrelated keys untouched, other dropdown items do not
      clear). Verification: `npx tsc --noEmit` clean; the four new suites 44/44;
      together with `sessionRedirect.test.ts`, `sessionRedirect.property.test.ts`,
      `RequireRole.test.tsx`, `Layout.navigationItems.test.tsx` and
      `buildsSurfaceRouteSidebar.integration.test.tsx`: 9 files / 165 tests
      passed. Non-vacuity proven by mutation — nine temporary mutations each
      broke the new suites (ProtectedRoute without the save → 5 failures,
      without the router `state` → 1, saving while loading → 2; Login reverted
      to `navigate(postLoginLanding)` → 15 across 2 files, the literal racy
      `takeAttemptedLocation() ?? postLoginLanding` form → 1; Layout without
      the clear → 3, clearing before `await logout()` → 1; the validator
      allowing `//host` → 1) and all four implementation files were restored
      byte-identical (sha256 verified). Decisions: the `Layout` sign-out test
      lives in a new `Layout.logout.test.tsx` (leaving the existing
      `Layout.navigationItems.test.tsx` pure-function suite untouched) and
      drives the component's own `onItemClick` through a stubbed
      `TopNavigation`, since Cloudscape collapses its utilities in jsdom; the
      `ProtectedRoute` `/login` case suppresses the real `Navigate` (a genuine
      self-redirect spins) and mounts the guard directly at `/login`; the
      loading branch is asserted via the component's own full-height wrapper
      plus the spinner's circle spans, as Cloudscape's `Spinner` exposes no
      role. Property 3's oracle is an independent regex restatement of design
      Decision 3, with each adversarial family also carrying its own
      classification. Frontend-only change (new test files only), so no
      preservation-tracked file was touched and no rebaseline is owed; the
      security preservation guards are still 4 passed / 3 skipped. Task 1.1's
      exploration suite remains 1 pass (`Login` restores) / 1 fail (the
      `api.ts` 401 save, which lands in task 5.1). Nothing deferred.

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - **OUTCOME**: Verified the wave 0-3 work with no code changes of my own.
    `npx tsc --noEmit` in `edge-cv-portal/frontend` is clean; the full
    `npx vitest run` is **1977 passed / 2 failed across 196 files** (up from
    the task 6 baseline of 189 files / 1845 tests, i.e. every suite this spec
    added is in and green), and the spec's own eight suites
    (`sessionRedirect.test.ts`, `sessionRedirect.property.test.ts`,
    `ProtectedRoute.test.tsx`, `Login.returnTo.test.tsx`,
    `Login.returnTo.property.test.tsx`, `Layout.logout.test.tsx`, the
    `RequireRole.test.tsx` regression guard and the task 1.1 exploration
    suite) are 141 passed / 1 failed. Both full-suite failures are
    expected-and-out-of-scope, and neither is attributable to tasks 1-3:
    (a) `sessionRedirect.exploration.test.ts` — "records the attempted deep
    location when a 401 sends the user to /login" is the task 1.1 red oracle
    for the `api.ts` Session_Exit, which only flips when task 5.1 makes that
    handler call `redirectToLogin()`; its sibling `Login`-restores case already
    passes, and task 6 explicitly asks which cases flipped, so I left it red
    rather than skipping or weakening it; (b)
    `src/pages/workflows/requirementsReconciliation.property.test.ts`
    Property 6 (feature `custom-node-code-assist`, a different spec) failed on
    an unseeded fast-check draw (seed 1107005820, counterexample
    `["numpy  # via code imports\n", []]`). I confirmed it is a pure
    `importAnalyzer` trailing-blank-line defect — a throwaway probe replaying
    just that input showed `parseRequirements` yields a manual `""` line that
    `reconcileRequirements` drops (`IN=[""]` vs `OUT=[]`), with nothing from
    this spec in the import graph — and it passes in isolation on other seeds;
    the probe was deleted and that spec's code left untouched. Also green:
    the security preservation guards (4 passed / 3 skipped — frontend-only
    change, so nothing tracked was touched and no rebaseline is owed) and the
    device `test/backend-test/workflow_engine` suite (1495 passed / 3 skipped),
    run as a cross-check that no global state was disturbed. Nothing deferred
    beyond the already-planned wave 5 work; no build, deploy or AWS access was
    used.

- [x] 5. Refresh a stale token instead of bouncing to login
  - [x] 5.1 Refresh-then-retry-once in `src/services/api.ts`
    - Extract the 401 branch (lines 1157-1166) into `handleUnauthorized()`;
      module-level `refreshInFlight` promise collapsing concurrent refreshes;
      `fetchAuthSession({ forceRefresh: true })` imported from
      `aws-amplify/auth`; on success update `localStorage['idToken']` and
      re-issue the request once with fresh headers; on failure or a second
      401 call `redirectToLogin()`; retry performed inside the existing
      `beginRequest`/`endRequest` span; non-401 handling and both `ApiError`
      envelopes untouched
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_
    - **OUTCOME**: `src/services/api.ts` now performs a Silent_Refresh in front
      of the login redirect. The inline 401 block is gone; in its place are
      three module-level pieces: `refreshInFlight: Promise<string|null>|null`
      plus `refreshIdToken()` (one `fetchAuthSession({ forceRefresh: true })`
      from `aws-amplify/auth` at a time, mirrors the token into
      `localStorage['idToken']` on success, returns `null` on any failure or a
      session with no ID token), `abandonSession()` (the old
      `console.error` + `localStorage.removeItem('idToken')`, then
      `redirectToLogin()` which owns the save and the already-on-`/login`
      guard), and `handleUnauthorized(reissue)` which refreshes and re-issues
      the request exactly once with the fresh bearer header, calling
      `abandonSession()` when the refresh fails or the retried response also
      401s. `request()` calls it only from the first attempt and never feeds
      the retried response back in, so one retry is structurally the maximum,
      and the whole thing sits inside the existing
      `beginRequest`/`endRequest` `try/finally` span; the non-401 path, both
      `ApiError` envelopes and the outer error normaliser are byte-identical.
      Decision (a deliberate, conservative deviation): `handleUnauthorized` is
      **exported** rather than `private` as design.md words it, and it takes a
      `reissue` callback instead of owning the fetch — that is what lets task
      5.2 give the Node_Designer client the same path with one implementation
      (design Decision 7) instead of a second copy. Verification: `npx tsc
      --noEmit` clean; a throwaway 10-case suite covering refresh success (no
      redirect, one retry, fresh `Authorization`, response returned, token
      re-mirrored), refresh failure (redirect + location saved + mirror
      cleared), a token-less session, two concurrent 401s sharing one refresh
      (1 refresh, 4 fetches), a retried request that 401s again (2 fetches, no
      third), already-on-`/login`, both non-401 envelopes untouched, a
      successful first response never refreshing, and a balanced loading count
      even when the retry throws passed 10/10 and was proven non-vacuous by
      five mutations (no refresh at all → 4 failures, no concurrency collapse
      → 1, a second retry → 1, no token mirror → 1, redirect even on success
      → 2) with `api.ts` restored byte-identical (sha256 verified); it was
      then deleted since the durable `api.refreshRetry.test.ts` is optional
      task 5.3. Full `npx vitest run` is **196 files / 1979 tests all
      passing** — above the task 4 checkpoint (1977 passed / 2 failed) — and
      task 1.1's remaining red case (the `api.ts` 401 save) has flipped green,
      so the exploration suite is now 2/2. Security preservation guards
      unchanged at 4 passed / 3 skipped; frontend-only change, so no
      preservation-tracked file was touched and no rebaseline is owed. Nothing
      deferred; task 5.2 (Node_Designer delegation) is next as planned.

  - [x] 5.2 Node_Designer client delegates to the same path
    - Replace `src/pages/node-designer/api.ts:54-60` with the shared helper so
      there is exactly one implementation
    - _Requirements: 1.3_
    - **OUTCOME**: `src/pages/node-designer/api.ts` no longer has its own 401
      block: the duplicated `localStorage.removeItem('idToken')` +
      `window.location.href = '/login'` pair is replaced by a call to the
      shared `handleUnauthorized(reissue)` exported from `services/api.ts`
      (task 5.1), so the Node_Designer client now gets the same
      Silent_Refresh → retry-once → location-preserving redirect and there is
      exactly one implementation to reason about (Requirement 1.3, design
      Decision 7). The request body was restructured minimally to make that
      possible — the URL is hoisted into a `const url`, `response` became
      `let`, and the retry re-issues the same request with
      `Authorization: Bearer <freshToken>` — all inside the existing
      `beginRequest`/`endRequest` `try/finally` span, and the non-401 path plus
      both error envelopes (structured `ApiError`, plain `Error`) are
      unchanged. Verification: `npx tsc --noEmit` clean; a throwaway 5-case
      suite (refresh success → no redirect, one retry with the fresh token,
      response returned, token re-mirrored; refresh failure → save
      `/node-designer/plugins/abc?tab=builds` + clear the mirror + redirect;
      retried 401 → redirect and no third fetch; two concurrent calls → one
      `fetchAuthSession`, four fetches; non-401 → `INVALID_DECLARATION`
      envelope intact with no refresh; balanced `getActiveCount()` everywhere)
      passed 5/5 and was proven non-vacuous by two mutations — restoring the
      old inline 401 block failed 4 of the 5, and turning the `if` into a
      `while` hung the retry (both restored, `api.ts` sha256-verified
      byte-identical) — then deleted, since the durable
      `api.refreshRetry.test.ts` case for this client is optional task 5.3.
      `npx vitest run` over `src/pages/node-designer` plus this spec's eight
      suites and the `RequireRole` regression guard is 41 files / 478 tests
      passing, and the full `npx vitest run` is **196 files / 1979 tests all
      passing**, matching the task 5.1 checkpoint exactly. Security
      preservation guards unchanged at 4 passed / 3 skipped; frontend-only
      change, so no preservation-tracked file was touched and no rebaseline is
      owed. Nothing deferred.

  - [x]* 5.3 Property and unit tests for refresh-and-retry
    - `src/services/api.refreshRetry.property.test.ts` —
      **Property 4: A 401 never redirects while the session is refreshable,
      and retries at most once** — _Validates: 5.1-5.6_
    - `src/services/api.refreshRetry.test.ts` (new): refresh success → no
      redirect, one retry, response returned; refresh failure → redirect with
      the location saved; two concurrent 401s → one refresh; retried request
      401 → redirect and no second retry; loading-bar count balanced; the
      Node_Designer client takes the same path
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 1.3_
    - **OUTCOME**: Added the two durable suites for the Silent_Refresh path.
      `src/services/api.refreshRetry.property.test.ts` carries Property 4 at an
      explicit `numRuns: 100` (verified: a temporary run-counter assertion
      showed exactly 100 examples, then removed), tagged
      `Feature: portal-session-expiry-return-to-page, Property 4: …`. Each run
      draws a batch of 1-4 concurrent requests (first status from
      200/401/403/500, retry status from 200/401/500), one refresh outcome
      (success / rejected / session-without-an-ID-token) and the page the user
      is on (four deep paths plus `/login`), drives the real `ApiService`
      through a stubbed `fetch` and mocked `fetchAuthSession`, and checks
      expectations derived from the requirements rather than the code: exactly
      one refresh per 401 batch with `{forceRefresh: true}` (5.3), per-URL
      attempt counts of exactly `1 + (401 && refreshOk)` and never more than 2
      (5.4), every retry carrying the fresh bearer token, a `/login`
      navigation exactly for the requests whose refresh failed or whose retry
      also 401d and none otherwise (5.1, 5.2), the remembered location and the
      `localStorage` mirror in the state the requirements describe (1.1, 1.4,
      1.6), the per-caller outcome matching the final status (5.6), and the
      loading count both still raised while the refresh is pending and back to
      its starting value afterwards (5.5).
      `src/services/api.refreshRetry.test.ts` adds 26 example-based cases
      covering every item the task lists: refresh success (no redirect, one
      retry, fresh token on the retry, same URL/method/body, retried response
      returned, token re-mirrored), refresh failure and a token-less session
      (redirect, location saved, mirror cleared, `ApiError` status 401, no
      retry attempted), first-write-wins across two abandoning 401s,
      already-on-`/login` (no navigation, nothing remembered), one refresh
      shared by three concurrent 401s and a genuinely later 401 refreshing
      again, a retried 401 (redirect, no third fetch) versus a retried 500
      (surfaced to the caller, no redirect), non-401 paths untouched (200
      never refreshes; the 403 structured and 500 simple `ApiError` envelopes
      intact), four loading-bar cases including the retry throwing, and six
      Node_Designer-client cases proving it takes the identical path
      (Requirement 1.3), including one refresh shared with a concurrent
      main-client 401. Verification: `npx tsc --noEmit` clean; the two suites
      27 passed; full `npx vitest run` **198 files / 2006 tests all passing**
      (exactly +2 files / +27 tests over the task 5.1-5.2 baseline of
      196/1979, so nothing regressed and no pre-existing failure remains);
      security preservation guards still 4 passed / 3 skipped. Decisions:
      concurrency is made deterministic with a gated `fetchAuthSession`
      promise released only after every first attempt has observed its 401,
      rather than relying on microtask ordering; the `fetch` stub counts
      attempts per request URL (and fails when a plan is exceeded), so no
      assertion depends on the order concurrent requests are served and "no
      second retry" is enforced structurally; the redirect is observed through
      the `sessionRedirect.navigateTo` seam with the document location moved by
      `history.replaceState`, never by stubbing `window.location`. Non-vacuity
      proven by mutation — seven temporary mutations of the implementation each
      failed the new suites (no Silent_Refresh at all → 16 failures, no
      in-flight collapse → 3, a second retry → 3, no token mirror → 4, redirect
      even on success → 10, the retry moved outside the `beginRequest`/
      `endRequest` span → 8, the Node_Designer client back to its inline 401
      block → 5) and both `src/services/api.ts` and
      `src/pages/node-designer/api.ts` were restored byte-identical (sha256
      verified). Test files only, so no preservation-tracked file was touched
      and no rebaseline is owed. Nothing deferred; task 6 (final verification
      plus the manual smoke) is next.

- [x] 6. Final verification
  - `npx tsc --noEmit` clean; full `npx vitest run` at or above the current
    baseline (189 files / 1845 tests as of 2026-09-17), with
    `components/RequireRole.test.tsx` and the `DataLabelerRedirect` coverage
    unmodified and passing
  - Confirm the task 1.1 exploration cases now pass and record which flipped
  - Manual smoke on a deployed portal (the one thing tests cannot prove): sign
    in, open a deep page with a query string (e.g. a device detail tab), let
    the session expire or clear `localStorage['idToken']`, trigger a request,
    confirm either a silent recovery or a login prompt that returns to that
    exact page; then sign out deliberately and confirm the default landing
  - **OUTCOME**: Verification only — no code or test file was changed by this
    task. `npx tsc --noEmit` in `edge-cv-portal/frontend` is clean, and the
    full `npx vitest run` is **198 files / 2006 tests, all passing** (baseline
    was 189 files / 1845 tests, so +9 files / +161 tests and zero failures);
    notably the `requirementsReconciliation` Property 6 seed-dependent flake
    from another spec that failed at the task 4 checkpoint passed this run, so
    no pre-existing frontend failure remains. The regression guards named in
    the task are untouched by this spec (`git status` shows
    `components/RequireRole.test.tsx`, `components/DataLabelerRedirect.tsx`
    and `pages/admin/UserManagerModals.dataLabelerRole.test.tsx` all
    unmodified) and pass: 8/8 and 13/13. Both task 1.1 exploration cases now
    pass (2/2); for the record, "sends the user back to the remembered
    location after signing in, not /dashboard" flipped green at task 3.2 when
    `Login` began consuming the value, and "records the attempted deep
    location when a 401 sends the user to /login" flipped at task 5.1 when the
    `api.ts` Session_Exit started calling `redirectToLogin()` — it was the
    single expected red case at the task 4 checkpoint. The spec's nine suites
    plus the `RequireRole` guard are 10 files / 169 tests passing. Structural
    cross-checks: `grep` finds no remaining hard-coded `window.location.href =
    '/login'` anywhere in `src/` — the only navigation site left is
    `sessionRedirect.ts:204` behind the `navigateTo` seam — so all three
    Session_Exits and both API clients go through one implementation
    (Requirement 1.3). Security preservation guards are 4 passed / 3 skipped
    with no drift failures; the change is frontend-only (`sessionRedirect.ts`
    plus the four call sites and nine test files), so no preservation-tracked
    file was touched and no rebaseline is owed. As an unrelated-regression
    cross-check the device `test/backend-test/workflow_engine` suite was also
    run: 1495 passed / 3 skipped. **Deferred (the one item this run cannot
    do): the manual smoke on a deployed portal.** It requires a portal deploy
    and a live Cognito session, which this unattended run is explicitly
    forbidden from touching (no deploy scripts, no AWS access), so a human
    should perform it — sign in, open a deep page with a query string, clear
    `localStorage['idToken']` or wait out the ID token, trigger a request, and
    confirm either the silent recovery (no login screen) or a login prompt
    that returns to that exact path+query, then sign out deliberately and
    confirm the default landing. Everything that is testable locally is
    covered: the Silent_Refresh success/failure/concurrency/single-retry paths
    and the save→restore round trip are pinned by
    `api.refreshRetry.test.ts`/`.property.test.ts`, `Login.returnTo.*` and
    `ProtectedRoute.test.tsx`. I also noted while diffing that
    `components/Layout.tsx` and `services/api.ts` carry unrelated
    quality-prompt-tuning changes in this working tree; they were left exactly
    as found.

## Notes

- Tasks marked `*` are optional; core implementation tasks are never optional
- Four correctness properties, one property-based test each, ≥ 100 examples
  (`numRuns: 100` explicitly — `src/test/setup.ts` configures a global 25),
  tagged `Feature: portal-session-expiry-return-to-page, Property {n}: {text}`
- Tasks 1-3 fix the reported symptom; task 5 removes most occurrences of it.
  They are separable: if task 5 has to be dropped, the spec still delivers the
  fix the user asked for
- `sessionStorage` is deliberate (design Decision 1): the `api.ts` exit is a
  full document navigation, so router `location.state` cannot survive it, and a
  `?redirect=` parameter would add open-redirect surface and leak deep links
- jsdom cannot perform `window.location.href` navigation; the `navigateTo`
  seam is what tests observe (design Decision 4)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1", "3.2", "3.3"] },
    { "id": 3, "tasks": ["3.4"] },
    { "id": 4, "tasks": ["4"] },
    { "id": 5, "tasks": ["5.1"] },
    { "id": 6, "tasks": ["5.2", "5.3"] },
    { "id": 7, "tasks": ["6"] }
  ],
  "dependencies": {
    "2.1": ["1.1"],
    "2.2": ["2.1"], "2.3": ["2.1"],
    "3.1": ["2.1"], "3.2": ["2.1"], "3.3": ["2.1"],
    "3.4": ["3.1", "3.2", "3.3"],
    "4": ["2.2", "2.3", "3.4"],
    "5.1": ["4"], "5.2": ["5.1"], "5.3": ["5.1", "5.2"],
    "6": ["5.3"]
  }
}
```
