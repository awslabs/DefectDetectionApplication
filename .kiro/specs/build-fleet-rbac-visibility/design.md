# Build Fleet RBAC Visibility Bugfix Design

## Overview

The Build Server Fleet page returned 403 "Insufficient permissions" for every
user, including the Cognito `admin` whose JWT carries
`custom:role=PortalAdmin`. The bug has two facets:

1. **Backend (root cause, ALREADY FIXED)**: the `rbac_check` decorator in
   `edge-cv-portal/backend/functions/rbac_middleware.py` called
   `rbac_manager.has_permission(user_id, usecase_id, permission)` without the
   `user_info` argument, so the JWT `custom:role` claim never reached role
   resolution. The build-fleet routes are the first to use
   `allow_global=True` (scope `'global'`), where no per-usecase
   `dda-portal-user-roles` row exists for JWT-only users, so the role
   defaulted to Viewer and the request was denied. The same gap existed in
   the middleware's `get_user_role` / `get_user_permissions` /
   `is_portal_admin` calls (rbac_context, audit-log denial details) and in
   `super_user_only`.

   **Status: implemented and committed** — commit `22a27eb` ("Fix
   build-fleet 403: thread JWT user_info through RBAC middleware") on branch
   `feature/workflow-triggers`. The exploration/fix tests at
   `edge-cv-portal/backend/tests/test_rbac_global_scope_jwt_role.py` failed
   with 403 on the unfixed code and now pass. Verified: backend `-k rbac`
   270 passed; `portal_builds` suite 74 passed (run with `--noconftest`);
   deployment filter suite 110 passed. This design documents the fix for
   the record; the tasks phase treats the exploration test and backend fix
   as done, with only verification remaining.

2. **Frontend (REMAINING WORK)**: the "Builds" navigation item (`/builds`)
   is shown to every signed-in user and the `/builds`, `/builds/:buildJobId`,
   and `/admin/fleet` routes are unguarded, so users whose role grants no
   builds access (Viewer, Operator, or per-usecase-only roles with no
   global/JWT builds-granting role) land on a page whose only content is a
   403 error banner. The fix hides the builds/fleet navigation entries and
   guards the routes for those users, gating on the merged role matrix
   (builds access: DataScientist, UseCaseAdmin, PortalAdmin) using the
   existing frontend pattern — role from JWT claims via `useAuth()`
   (`user?.role`), as `Layout.tsx` already does for PortalAdmin-only sidebar
   items. Server-side 403 handling stays unchanged (defense in depth).

## Glossary

- **Bug_Condition (C)**: an input triggers the bug when EITHER (a) a request
  reaches `rbac_check` / `super_user_only` from a user whose effective role
  comes only from the JWT `custom:role` claim and that role holds the
  required permission (backend facet, fixed), OR (b) the portal UI renders
  navigation/routes for a signed-in user whose role grants no builds access
  (frontend facet, remaining).
- **Property (P)**: JWT-carried roles resolve correctly in the middleware
  (authorized requests succeed), and the builds/fleet navigation and pages
  are invisible/unreachable in the UI for roles without builds access.
- **Preservation**: DynamoDB-row-based role resolution, 403 envelope shape,
  audit-log structure, `rbac_context` keys, the role-permission matrices,
  the `portal_builds` suite, per-usecase-only behavior, and the visibility
  of builds/fleet UI for builds-capable roles all remain unchanged.
- **Builds_Access_Roles**: `DataScientist`, `UseCaseAdmin`, `PortalAdmin` —
  the roles holding `builds:read`/`builds:submit`/`builds:cancel` per the
  merged matrix (`shared_utils.RBACManager`); the "Build_Operator"
  capability.
- **rbac_check**: decorator in
  `edge-cv-portal/backend/functions/rbac_middleware.py` that authorizes API
  requests against required permissions; `allow_global=True` resolves the
  scope to `'global'` for non-Use_Case routes (all builds routes).
- **super_user_only**: decorator in the same file requiring PortalAdmin.
- **user_info**: the JWT-derived user dict from `get_user_from_event`
  (contains `role` from the `custom:role` claim); `shared_utils.RBACManager`
  role resolution only sees the JWT role when this is passed through.
- **useAuth()**: frontend auth context hook
  (`edge-cv-portal/frontend/src/contexts/AuthContext.tsx`); `user.role` is
  the JWT `custom:role` claim (defaulting to `'Viewer'`).
- **UserRole**: frontend role union in
  `edge-cv-portal/frontend/src/types/index.ts`:
  `'PortalAdmin' | 'UseCaseAdmin' | 'DataScientist' | 'Operator' | 'Viewer'`.

## Bug Details

### Bug Condition

The backend facet manifests when a user whose only role source is the JWT
`custom:role` claim calls a `rbac_check`- or `super_user_only`-decorated
route; because `user_info` was omitted, role resolution defaulted to Viewer
(most visibly at `'global'` scope, where no per-usecase row can rescue the
lookup). The frontend facet manifests when a signed-in user whose role is
not in Builds_Access_Roles views the navigation or navigates to a builds
route: the UI shows "Builds" (and renders the pages), producing a
403-banner-only page.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type PortalInteraction
         (either an ApiRequest or a UiNavigation)
  OUTPUT: boolean

  IF input is ApiRequest THEN
    -- Backend facet (FIXED in commit 22a27eb)
    RETURN input.user.jwtRole IS PRESENT
           AND input.user has no dda-portal-user-roles row
               granting the required permission at input.scope
           AND input.user.jwtRole holds the required permission
           AND request is denied with 403          -- unfixed behavior
  ELSE  -- UiNavigation
    -- Frontend facet (REMAINING)
    RETURN input.user.role NOT IN
             ['DataScientist', 'UseCaseAdmin', 'PortalAdmin']
           AND (input.target IN ['/builds', '/builds/:buildJobId',
                                 '/admin/fleet']
                OR input.target = 'navigation-render')
           AND ("Builds" nav item is shown
                OR the builds/fleet page renders)   -- unfixed behavior
  END IF
END FUNCTION
```

### Examples

- The deployed Cognito `admin` user (`custom:role=PortalAdmin`, no
  `dda-portal-user-roles` rows) opened the Build Server Fleet page:
  expected the server list; actual was "Failed to load build servers —
  Insufficient permissions", with a live audit-log denial recording
  `required_permissions=["builds:read"]`, `usecase_id='global'`,
  `user_role="Viewer"`. (Backend facet — fixed.)
- A JWT-only DataScientist submitted a build (`builds:submit`,
  `allow_global=True`): expected 200; actual 403, because the JWT role
  never reached `get_user_role`. (Backend facet — fixed.)
- A JWT-only PortalAdmin called a `super_user_only` route: expected 200;
  actual 403 "Super user access required". (Backend facet — fixed.)
- A Viewer signs in: expected no "Builds" nav item and no reachable
  `/builds` page; actual: "Builds" is listed for every user and `/builds`
  renders a page whose only content is the 403 error banner. (Frontend
  facet — remaining.)
- Edge case: an Operator types `/admin/fleet` into the address bar:
  expected no fleet page render (redirect away); actual: the route renders
  FleetPage, which shows only its access-denied notice. (Frontend facet —
  remaining; note `/admin/fleet` was already absent from the Operator's
  navigation since "Build Fleet" sits in the PortalAdmin-only item group.)

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Role resolution precedence for users with `dda-portal-user-roles` rows
  (JWT PortalAdmin → DynamoDB global PortalAdmin → per-usecase row → JWT
  fallback → Viewer default); every existing rbac/permission test passes
  unchanged (Req 3.1).
- The 403 error envelope shape (`error`, `required_permissions` /
  `required_role`, `usecase_id`) and audit-log denial structure (Req 3.2).
- `event['rbac_context']` keys: `user_id`, `usecase_id`, `user_role`,
  `permissions`, `is_super_user` (Req 3.3).
- The role-permission matrices: `builds:*` granted to exactly
  DataScientist, UseCaseAdmin, PortalAdmin (Req 3.4).
- The `portal_builds` test suite passes unchanged (Req 3.5).
- Builds-capable roles keep the "Builds" navigation and pages; the
  "Build Fleet" (`/admin/fleet`) entry remains PortalAdmin-only (Req 3.6).
- Per-usecase-only users keep their per-usecase authorization and continue
  to have no global builds access — intentional, not a defect (Req 3.7).
- Server-side 403 handling for direct API calls that bypass the UI stays
  fully in place — hiding the UI does not replace server-side checks
  (Req 2.7, defense in depth).

**Scope:**
All inputs that do NOT involve JWT-only role resolution or the builds/fleet
UI surface are completely unaffected. This includes:
- Every navigation item other than "Builds" and "Build Fleet" (Dashboard,
  Use Cases, Workflows, Deployments, Audit Logs, Settings, etc.) for every
  role.
- Every route other than `/builds`, `/builds/:buildJobId`, `/admin/fleet`.
- All backend routes for users whose role comes from DynamoDB rows.
- The FleetPage-internal PortalAdmin access-denied notice (kept as a second
  layer even though the route guard makes it normally unreachable).

## Hypothesized Root Cause

The backend root cause is **confirmed** (verified in code, live audit logs,
and by the exploration tests failing 403 on unfixed code):

1. **Missing `user_info` threading in `rbac_check`** (confirmed): every
   `rbac_manager` call in the decorator — `has_permission`, the
   `get_user_role` calls in the audit-log denial details, and the
   `get_user_role` / `get_user_permissions` / `is_portal_admin` calls that
   populate `rbac_context` — omitted `user_info`, so
   `shared_utils.RBACManager.get_user_role` received `user_info=None` and
   its JWT fallback never fired. At `'global'` scope there is no
   per-usecase row to compensate, so the role defaulted to Viewer → 403.
   The branch's own tests passed `user_info` explicitly, masking the gap.

2. **Same gap in `super_user_only`** (confirmed): `is_portal_admin(user_id)`
   without `user_info` denied JWT-only PortalAdmins on super-user routes.

3. **Frontend never gated the builds surface** (confirmed by inspection):
   `Layout.tsx` puts "Builds" in `baseNavigationItems` (shown to all
   roles), and `App.tsx` registers `builds`, `builds/:buildJobId`, and
   `admin/fleet` with no role guard — only FleetPage checks the role
   internally, and only to swap its content for an error notice.

4. **Known residual gap (accepted, by design of the resolution order)**:
   per-usecase-only users (no JWT role, only per-usecase rows) still
   resolve to Viewer at `'global'` scope. This is intentional (Req 3.7);
   the frontend gating makes it invisible.

## Correctness Properties

Property 1: Bug Condition (backend) - JWT role reaches role resolution at every middleware call

_For any_ user whose only role source is the JWT `custom:role` claim (no
`dda-portal-user-roles` rows) and any required permission held by that role,
a handler decorated with the fixed `rbac_check(..., allow_global=True)` (or
`super_user_only` for PortalAdmin) SHALL authorize the request (HTTP 200),
and _for any_ such user whose JWT role does NOT hold the required
permission, the request SHALL still be denied with the standard 403
envelope.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

*Status: implemented and passing —
`edge-cv-portal/backend/tests/test_rbac_global_scope_jwt_role.py` (failed
403 on unfixed code; passes on commit `22a27eb`). No new test needed; the
tasks phase only re-runs it for verification.*

Property 2: Bug Condition (frontend) - builds surface visibility is a function of the role

_For any_ role in the `UserRole` domain (plus `undefined` for the
role-less/loading state), the navigation items produced for that role SHALL
include the "Builds" item if and only if the role is in
{DataScientist, UseCaseAdmin, PortalAdmin}, SHALL include the "Build Fleet"
item if and only if the role is PortalAdmin, and direct navigation to
`/builds`, `/builds/:buildJobId`, or `/admin/fleet` SHALL render the page
if and only if the same role predicate holds — otherwise the router SHALL
redirect away (no 403-banner page render).

**Validates: Requirements 2.5, 2.6**

Property 3: Preservation (backend) - non-bug-condition requests are unchanged

_For any_ request where the bug condition does NOT hold (users whose role
resolves via `dda-portal-user-roles` rows, denied requests, per-usecase
routes), the fixed middleware SHALL produce the same result as the original:
identical role-resolution precedence, identical 403 envelope and audit-log
denial structure, identical `rbac_context` keys, unchanged role-permission
matrices, and per-usecase-only users still authorized per their per-usecase
rows with no global builds access. Concretely: the existing backend rbac
suite, the `portal_builds` suite, and the deployment-filter suite pass
unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.7**

*Status: verified on commit `22a27eb` — backend `-k rbac` 270 passed;
`portal_builds` 74 passed (`--noconftest`); deployment filter 110 passed.
Re-run after the frontend change lands (frontend-only change, so this is a
sanity re-check).*

Property 4: Preservation (frontend) - builds-capable roles keep their UI

_For any_ role in {DataScientist, UseCaseAdmin, PortalAdmin}, the fixed
frontend SHALL continue to show the "Builds" navigation item and render the
`/builds` and `/builds/:buildJobId` pages; the "Build Fleet"
(`/admin/fleet`) entry and page SHALL continue to be available to
PortalAdmin and only PortalAdmin; and all navigation items other than
"Builds"/"Build Fleet" SHALL be identical to the original for every role.

**Validates: Requirements 3.6**

## Fix Implementation

### Changes Required

#### Part A — Backend middleware (ALREADY IMPLEMENTED, commit 22a27eb)

**File**: `edge-cv-portal/backend/functions/rbac_middleware.py`

**Functions**: `rbac_check`, `super_user_only`

Documented for the record; no further code change needed:

1. **`rbac_check` permission check**: `has_permission(user_id, usecase_id,
   permission, user_info=user)` — the JWT-derived user dict is threaded
   into every permission evaluation.
2. **`rbac_check` audit-log denial details**: the `get_user_role` calls in
   the denial path pass `user_info=user`, so denials record the caller's
   actual resolved role.
3. **`rbac_check` rbac_context**: `get_user_role` / `get_user_permissions`
   / `is_portal_admin` all pass `user_info=user`, so the context reflects
   the JWT-carried role.
4. **`super_user_only`**: `is_portal_admin(user_id, user_info=user)` and
   the denial-path `get_user_role(..., user_info=user)`.
5. **No changes** to `shared_utils.RBACManager` resolution order, the
   permission matrices, error envelopes, or audit structure.

#### Part B — Frontend gating (REMAINING WORK)

**New file**: `edge-cv-portal/frontend/src/utils/buildsAccess.ts`

A single source of truth for the builds-access predicate, mirroring the
exported-pure-function pattern of `WORKFLOW_EDIT_ROLES` /
`canEditWorkflows` in `pages/workflows/WorkflowToolbar.tsx`:

```typescript
import type { UserRole } from '../types';

/** Roles granted builds:read/submit/cancel per the merged matrix
 *  (the Build_Operator capability) — bugfix Req 2.6, matrix per 3.4. */
export const BUILDS_ACCESS_ROLES: readonly UserRole[] = [
  'DataScientist',
  'UseCaseAdmin',
  'PortalAdmin',
];

/** True when the role may see/use the builds surface (Req 2.5, 2.6). */
export function canAccessBuilds(role: UserRole | undefined | null): boolean {
  return role != null && BUILDS_ACCESS_ROLES.includes(role);
}
```

**File**: `edge-cv-portal/frontend/src/components/Layout.tsx`

1. **Extract navigation construction into an exported pure function**
   (mirroring the existing `buildSettingsDropdownItems` pattern so the
   gating is directly property-testable):

   ```typescript
   export function buildNavigationItems(
     role: UserRole | undefined
   ): SideNavigationProps.Item[]
   ```

   The function reproduces today's item list exactly, with one change: the
   `{ text: 'Builds', href: '/builds' }` entry is included only when
   `canAccessBuilds(role)`. The PortalAdmin-only group (including
   "Build Fleet" → `/admin/fleet`) and the UseCaseAdmin audit-logs handling
   stay exactly as they are. The component body calls
   `buildNavigationItems(user?.role)` instead of assembling the list
   inline.

**File**: `edge-cv-portal/frontend/src/App.tsx` (plus a small new
component, e.g. `edge-cv-portal/frontend/src/components/RequireRole.tsx`)

2. **Role route guard**: a tiny component following the existing
   `useAuth()` pattern:

   ```typescript
   export default function RequireRole({
     roles,
     children,
   }: {
     roles: readonly UserRole[];
     children: JSX.Element;
   }) {
     const { user } = useAuth();
     if (!user?.role || !roles.includes(user.role)) {
       return <Navigate to="/dashboard" replace />;
     }
     return children;
   }
   ```

   Redirecting to `/dashboard` (the authenticated index target) satisfies
   Req 2.5's "no rendering of the page with a 403 error banner" without
   inventing a NotFound page the app doesn't have.

3. **Wrap the three routes**:

   ```tsx
   <Route path="builds" element={
     <RequireRole roles={BUILDS_ACCESS_ROLES}><BuildsPage /></RequireRole>
   } />
   <Route path="builds/:buildJobId" element={
     <RequireRole roles={BUILDS_ACCESS_ROLES}><BuildDetail /></RequireRole>
   } />
   <Route path="admin/fleet" element={
     <RequireRole roles={['PortalAdmin']}><FleetPage /></RequireRole>
   } />
   ```

4. **Keep FleetPage's internal PortalAdmin check untouched**: it becomes a
   second defensive layer behind the route guard (and the server-side 403
   remains the ultimate authority, Req 2.7).

5. **No changes** to backend code, API service calls, the AuthContext, or
   any other route/nav entry.

## Testing Strategy

### Validation Approach

Two-phase per facet. The backend facet has already completed both phases
(exploration tests surfaced the 403 counterexamples on unfixed code; the
fix landed; the suites are green). The frontend facet follows the same
shape: first observe on unfixed code that the "Builds" item renders for a
Viewer and that `/builds` renders the 403-banner page, then implement the
gating and verify fix + preservation.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm or refute the root cause analysis. If we
refute, we will need to re-hypothesize.

**Backend (DONE)**:
`edge-cv-portal/backend/tests/test_rbac_global_scope_jwt_role.py` — run
against the moto-backed stack with users that have JWT claims and NO
UserRoles rows, exercising the real `shared_utils` resolution (no
RBACManager patching). On unfixed code all authorization tests failed with
403, confirming root cause #1 and #2; they now pass. No re-exploration
needed.

**Frontend (TO DO)**:
**Test Plan**: Render `buildNavigationItems`' current equivalent (or
`Layout` itself) and the `/builds` route with a mocked AuthContext for a
Viewer/Operator role, asserting on UNFIXED code that the "Builds" item is
present and BuildsPage mounts. These observations confirm root cause #3.

**Test Cases**:
1. **Viewer sees Builds nav**: mock role `Viewer`, assert the "Builds"
   item is currently rendered (will fail after fix — this is the
   exploration observation, inverted into the fix test)
2. **Operator reaches /builds**: MemoryRouter at `/builds` with role
   `Operator`, assert BuildsPage currently mounts (will be a redirect
   after fix)
3. **Operator reaches /admin/fleet**: assert FleetPage currently mounts
   showing only its access-denied notice (will be a redirect after fix)

**Expected Counterexamples**:
- "Builds" nav item present for every role; builds/fleet routes render for
  roles without builds access
- Cause: no gating in `Layout.tsx` base items and no route guards in
  `App.tsx` (root cause #3)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the
fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  IF input is ApiRequest THEN
    result := rbac_check_fixed / super_user_only_fixed (input)
    ASSERT result.statusCode = 200 when the JWT role holds the permission
    ASSERT result.statusCode = 403 (standard envelope) otherwise
  ELSE
    ASSERT "Builds" nav item absent for the role
    ASSERT navigating to /builds, /builds/:id, /admin/fleet redirects
           to /dashboard (page component not rendered)
  END IF
END FOR
```

Backend fix checking is complete (Property 1, existing test file).
Frontend fix checking implements Property 2 with fast-check over the role
domain (vitest, following the existing
`components/vllm-publish/publishState.gating.property.test.ts` pattern).

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT original(input) = fixed(input)
  -- backend: role precedence, envelopes, audit structure, rbac_context
  -- frontend: nav items other than Builds/Build Fleet identical for
  --           every role; builds-capable roles keep their pages
END FOR
```

**Testing Approach**: Property-based testing is recommended for
preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all
  non-buggy inputs

**Test Plan**: Backend preservation is already demonstrated by the
unchanged existing suites passing on the fixed code (backend `-k rbac`
270, `portal_builds` 74 with `--noconftest`, deployment filter 110).
Frontend preservation: before the change, record the exact navigation item
list per role; the property test asserts the fixed `buildNavigationItems`
differs from that oracle only in the presence of "Builds" (and never in
"Build Fleet", which stays PortalAdmin-only).

**Test Cases**:
1. **Nav preservation**: for every role, `buildNavigationItems(role)` minus
   the "Builds" entry equals the pre-fix list minus the "Builds" entry
   (all other items, dividers, and ordering identical)
2. **Builds-capable roles keep access**: DataScientist / UseCaseAdmin /
   PortalAdmin see "Builds" and can render `/builds` and
   `/builds/:buildJobId`
3. **Build Fleet stays PortalAdmin-only**: "Build Fleet" item present iff
   PortalAdmin; `/admin/fleet` renders FleetPage iff PortalAdmin
4. **Backend suites re-run**: existing rbac + `portal_builds` +
   deployment-filter suites still pass after the frontend change (sanity;
   no backend files touched)

### Unit Tests

- `canAccessBuilds`: true for exactly DataScientist, UseCaseAdmin,
  PortalAdmin; false for Viewer, Operator, `undefined`, `null`
- `RequireRole`: renders children when the role is allowed; redirects to
  `/dashboard` (replace) when the role is missing or not allowed
- `buildNavigationItems`: example-based spot checks for Viewer (no Builds,
  no admin group) and PortalAdmin (Builds + admin group + audit logs)
- Backend (done): the example tests in
  `test_rbac_global_scope_jwt_role.py` (PortalAdmin builds:read,
  DataScientist builds:submit, Viewer still denied, super_user_only
  authorize/deny)

### Property-Based Tests

- **Property 2 (fix)**: fast-check over `UserRole ∪ {undefined}` — "Builds"
  in `buildNavigationItems(role)` iff `canAccessBuilds(role)`; "Build
  Fleet" present iff role is PortalAdmin; route guard renders/redirects per
  the same predicate (vitest + fast-check, pattern from
  `publishState.gating.property.test.ts`)
- **Property 4 (preservation)**: fast-check over the role domain — all
  non-builds navigation items identical to the pre-fix oracle for every
  role
- **Property 1 / 3 (backend, done)**: covered by
  `test_rbac_global_scope_jwt_role.py` and the unchanged existing suites;
  verification re-run only

### Integration Tests

- Render `App` routes in a MemoryRouter with mocked auth per role: Viewer
  navigating to `/builds` and `/admin/fleet` ends up on `/dashboard`;
  PortalAdmin reaches BuildsPage and FleetPage; DataScientist reaches
  BuildsPage but is redirected from `/admin/fleet`
- Sidebar integration: `Layout` rendered with each role shows/hides
  "Builds"/"Build Fleet" consistently with the route guards (no
  nav-item-without-route or route-without-nav divergence for the same role)
- Backend defense in depth (done, re-run to verify): unauthorized direct
  API calls to builds endpoints still return the standard 403 envelope and
  record the audit denial (Req 2.7)

**Test environments**: frontend — `npm test` (vitest) in
`edge-cv-portal/frontend`; backend — `pytest` from `edge-cv-portal/backend`
(the `portal_builds` suite under `test/backend-test/portal_builds` needs
`--noconftest`). Portal deployment of this fix must be sequenced after the
currently running JP5 build chain (no portal deploys during builds).
