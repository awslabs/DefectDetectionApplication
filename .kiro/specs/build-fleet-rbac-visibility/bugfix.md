# Bugfix Requirements Document

## Introduction

The Build Server Fleet page (merged from `feature/portal-build-fleet-and-workflow-gates`,
live since ~15:27 UTC) shows "Failed to load build servers — Insufficient
permissions" for **all** users, including the Cognito `admin` user whose JWT
carries `custom:role=PortalAdmin`.

Root cause (verified in code and live audit logs): the `rbac_check` decorator
in `edge-cv-portal/backend/functions/rbac_middleware.py` calls
`rbac_manager.has_permission(user_id, usecase_id, permission)` **without the
`user_info` argument**, so the JWT role claim never reaches role resolution.
The build-fleet routes are the first to use `allow_global=True` (scope
`'global'`); at global scope `get_user_role`
(`edge-cv-portal/backend/layers/shared/python/shared_utils.py`) can only find
a role via a `dda-portal-user-roles` row with `usecase_id='global'`
(per-usecase rows are skipped, and the JWT fallback receives `user_info=None`)
→ defaults to Viewer → 403. The same `user_info` gap exists in the
middleware's other resolution calls: the `get_user_role` /
`get_user_permissions` / `is_portal_admin` calls that populate `rbac_context`
and the audit-log denial details, and `is_portal_admin` in `super_user_only`.
Live evidence: audit-log denials for user `a4b804e8-...` with
`required_permissions=["builds:read"]`, `usecase_id='global'`, resolved role
`"Viewer"`, despite the JWT carrying PortalAdmin. The branch's own tests pass
`user_info` explicitly, which masked the gap.

Pre-existing (masked) impact at per-usecase scope: any user whose only role
comes from the JWT `custom:role` claim (no DynamoDB row) resolves to Viewer
in `rbac_check`-decorated per-usecase routes too; the fix corrects role
resolution at both scopes.

A second, user-directed facet: the frontend shows the "Builds" navigation
item (`/builds`) to every signed-in user and leaves the `/builds` and
`/admin/fleet` routes unguarded, so users whose role grants no builds access
(e.g. Viewer, Operator, or per-usecase-only roles with no global/JWT role)
land on a page that renders only a 403 error banner. Those users must not see
the builds/fleet navigation or pages at all. Per-usecase-only roles
intentionally have no global builds access; that is correct behavior once the
UI hides it. The frontend already has an established gating pattern — role
from the JWT claim via `useAuth()` (`user?.role`), as used for the
PortalAdmin-only sidebar items and the FleetPage access-denied notice — which
the fix should follow (design phase decides the exact mechanism).

**Scope**: portal backend middleware (`rbac_middleware.py`) and frontend
navigation/route gating. No changes to the role-permission matrices (which
roles carry `builds:*` stays exactly as the merged branch defined), error
envelopes, or audit logging structure.

**Working-tree caution**: branch `feature/workflow-triggers` with the merge
committed; work on top of current HEAD. Portal deployment of this fix must be
sequenced after the currently running JP5 build chain (steering: no portal
deploys during builds).

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a user whose JWT carries `custom:role=PortalAdmin` (and who has no `dda-portal-user-roles` row with `usecase_id='global'`) calls a build-fleet route guarded by `rbac_check(..., allow_global=True)` THEN the system resolves their role as Viewer (because `has_permission` is called without `user_info`, so the JWT claim never reaches `get_user_role`) and returns 403 "Insufficient permissions"

1.2 WHEN the Build Server Fleet page loads for that PortalAdmin user THEN the system displays "Failed to load build servers — Insufficient permissions" and records an audit-log denial with `required_permissions=["builds:read"]`, `usecase_id='global'`, and `user_role="Viewer"`

1.3 WHEN any user whose effective role comes only from the JWT `custom:role` claim (no DynamoDB role row) calls any `rbac_check`-decorated route (per-usecase or global) THEN the system resolves their role as Viewer instead of the JWT role, because none of the middleware's `has_permission` / `get_user_role` / `get_user_permissions` / `is_portal_admin` calls pass `user_info`

1.4 WHEN a JWT-only PortalAdmin calls a `super_user_only`-decorated route THEN the system denies access with 403 "Super user access required", because `is_portal_admin(user_id)` is called without `user_info`

1.5 WHEN an authorized request passes `rbac_check` via a DynamoDB role row THEN the system populates `event['rbac_context']` (`user_role`, `permissions`, `is_super_user`) from resolution calls that omit `user_info`, so the context can understate the caller's JWT-carried role

1.6 WHEN a signed-in user whose role grants no builds access (e.g. Viewer, Operator, or a per-usecase-only role with no global/JWT builds-granting role) views the portal navigation THEN the system shows the "Builds" navigation item and allows navigating to `/builds` and `/admin/fleet`, rendering a page whose only content is a 403 error banner

### Expected Behavior (Correct)

2.1 WHEN `rbac_check` evaluates required permissions THEN the system SHALL pass the caller's `user_info` (from `get_user_from_event`) through to `has_permission`, so JWT-carried roles (including `custom:role=PortalAdmin`) resolve correctly at both per-usecase and global scope

2.2 WHEN a user whose JWT carries `custom:role=PortalAdmin` calls a build-fleet route guarded by `rbac_check(..., allow_global=True)` THEN the system SHALL authorize the request and return build-fleet data, with zero data seeding (no `dda-portal-user-roles` rows required) — the deployed `admin` user regains Build Server Fleet access

2.3 WHEN `rbac_check` populates `event['rbac_context']` and audit-log denial details THEN the system SHALL pass `user_info` to the underlying `get_user_role` / `get_user_permissions` / `is_portal_admin` calls, so the recorded role and permissions reflect the caller's actual resolved role

2.4 WHEN a JWT-only PortalAdmin calls a `super_user_only`-decorated route THEN the system SHALL authorize the request (pass `user_info` to `is_portal_admin`, including in the denial audit-log path)

2.5 WHEN a signed-in user's role grants no builds access THEN the frontend SHALL NOT show the "Builds" or "Build Fleet" navigation items, and SHALL NOT render the `/builds`, `/builds/:buildJobId`, or `/admin/fleet` pages for direct navigation — no rendering of the page with a 403 error banner

2.6 WHEN gating the builds navigation and routes THEN the frontend SHALL gate on the user's actual builds permission (`builds:read` or equivalent role mapping: DataScientist, UseCaseAdmin, PortalAdmin per the merged matrix), following the existing frontend gating pattern (role from JWT claims via `useAuth()`, as used for the PortalAdmin-only sidebar items); if the design phase finds this pattern insufficient it decides the mechanism

2.7 WHEN an unauthorized client calls a builds API endpoint directly (bypassing the UI) THEN the system SHALL still return the standard 403 error envelope and record the audit-log denial — hiding the UI does not replace server-side checks (defense in depth)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a user's role is assigned via a `dda-portal-user-roles` row (global or per-usecase) THEN the system SHALL CONTINUE TO resolve that role with the existing precedence (JWT PortalAdmin → DynamoDB global PortalAdmin → per-usecase DynamoDB row → JWT fallback → Viewer default) — every existing per-usecase RBAC behavior is preserved and all existing rbac/permission tests pass unchanged

3.2 WHEN `rbac_check` or `super_user_only` denies a request THEN the system SHALL CONTINUE TO return the existing 403 error envelope shape (`error`, `required_permissions`/`required_role`, `usecase_id`) and record the denial in the audit log with the existing structure

3.3 WHEN `rbac_check` authorizes a request THEN the system SHALL CONTINUE TO populate `event['rbac_context']` with the same keys (`user_id`, `usecase_id`, `user_role`, `permissions`, `is_super_user`) — unchanged apart from now-correct role resolution

3.4 WHEN the role-permission matrices are consulted THEN the system SHALL CONTINUE TO grant `builds:submit` / `builds:cancel` / `builds:read` to exactly the roles the merged branch defined (DataScientist, UseCaseAdmin, PortalAdmin); no matrix changes

3.5 WHEN the branch's `portal_builds` test suite runs THEN the system SHALL CONTINUE TO pass it unchanged

3.6 WHEN a user with builds access (DataScientist, UseCaseAdmin, or PortalAdmin by JWT or global row) uses the builds pages THEN the frontend SHALL CONTINUE TO show the "Builds" navigation and pages, and the PortalAdmin-only "Build Fleet" (`/admin/fleet`) entry SHALL CONTINUE TO be shown only to PortalAdmin

3.7 WHEN a per-usecase-only user (e.g. UseCaseAdmin on specific usecases with no global/JWT role) calls a per-usecase route THEN the system SHALL CONTINUE TO authorize per their per-usecase DynamoDB role exactly as today, and SHALL CONTINUE TO have no global builds access (this is intentional, not a defect)
