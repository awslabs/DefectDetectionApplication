# Portal JWT-Role Privilege Escalation — Bugfix Design

## Overview

Two changes carry the fix; everything else is supporting work.

1. **Privilege moves from the token to a registry.** `RBACManager` stops
   reading `custom:role` as a privilege source and resolves the role from
   `dda-portal-user-roles`, which only the portal's User Manager writes. An
   unprovisioned `sub` resolves to *no role* and is denied.
2. **Audit entries become durable.** `log_audit_event` and the strict
   two-phase helpers gain `username`, `email`, `source_ip`, `user_agent`,
   and `identity_source`, captured from the request at write time so a later
   `AdminDeleteUser` cannot erase attribution.

Because the registry is currently **optional and near-empty**, enforcement
must not turn on until a backfill has populated it. The rollout is
therefore: extend the writer → backfill → enable enforcement.

The remaining pieces (app-client hardening, CloudTrail detection) are
defense in depth and detection; they do not carry the fix and are called
out as such so nobody mistakes them for it.

## Glossary

* **Portal_Identity** — a row in `dda-portal-user-roles` keyed
  (`user_id` = Cognito `sub`, `usecase_id`). `usecase_id='global'` is the
  account-level entry; any other value is a Use_Case assignment. Carries
  `role`, and (new) `username`, `email`, `status`.
* **Registry** — the set of Portal_Identity rows. The single source of
  portal privilege after this fix.
* **Effective_Role** — the `Role` the RBAC layer uses for a decision.
* **Unprovisioned_Principal** — a valid-token caller whose `sub` has no
  enabled global Portal_Identity.
* **Claimed_Role** — the `custom:role` value in the token. After the fix,
  descriptive metadata only; recorded in audit, never granted.
* **Identity_Source** — which input decided the Effective_Role:
  `registry` (a row was found) or `absent` (no row → denial).
* **Enforcement_Flag** — the single configuration value that turns
  registry enforcement on. Off until the backfill has run.
* **Attribution_Fields** — `username`, `email`, `source_ip`, `user_agent`,
  `identity_source`.

## Bug Details

### Where privilege is granted today

`shared_utils.RBACManager.get_user_role` (`shared_utils.py:~601-664`),
resolution order as written:

| Step | Input | Touches DynamoDB | Grants |
|---|---|---|---|
| 1 | `user_info['role'] == 'PortalAdmin'` | **no** | `PORTAL_ADMIN` |
| 2 | global row `role == 'PortalAdmin'` | yes | `PORTAL_ADMIN` |
| 3 | Use_Case row (skipped when scope is `global`) | yes | that row's role |
| 4 | `user_info['role']` as a `Role` | no | that role |
| 5 | fallthrough / any exception | — | `VIEWER` |

`user_info['role']` is `claims['custom:role']`
(`shared_utils.py:100-146`). Step 1 is the escalation: an attribute set at
`admin-create-user` time yields `PortalAdmin` with no lookup at all. Step 4
is the same hole for `DataScientist` / `UseCaseAdmin`, which is what
`builds:submit` actually needs (`shared_utils.py:512, 576`); `Viewer` does
**not** carry it, so the incident's principal must have carried an injected
`custom:role`.

Build routes make it worst-case: `require_builds_submit()` →
`rbac_check(..., allow_global=True)` (`rbac_middleware.py:422-426`) resolves
scope `'global'`, which skips step 3 entirely, leaving the claim as the only
input.

### Why the audit trail evaporated

`log_audit_event` (`shared_utils.py:148-171`) writes `user_id` and nothing
else identifying, and embeds it in `event_id`. After `AdminDeleteUser` the
`sub` resolves to nothing (verified across all 7 pools in the account), so
the row names an actor that cannot be identified. `requested_by` on the
Build_Job has the same problem.

## Expected Behavior

Same table after the fix:

| Step | Input | Grants |
|---|---|---|
| 1 | enabled global Portal_Identity row | that row's role |
| 2 | Use_Case Portal_Identity row, when scope is that Use_Case | that row's role (overrides step 1) |
| 3 | no enabled row | **nothing** → 403, `identity_source='absent'` |
| 4 | lookup raised | **nothing** → 500 availability error, audited `failure` |

`Claimed_Role` appears only in audit `details`.

## Hypothesized Root Cause

The RBAC layer was designed around "IDP is the single source of truth for
roles" — its own docstring says so (`shared_utils.py:419-427`). That is a
sound model when the IdP's role attribute can only be written by an
administrator through a governed path. Here the IdP attribute is writable
by any holder of `cognito-idp:AdminUpdateUserAttributes` /
`AdminCreateUser` on the pool, which is a much wider set of principals than
"portal administrators" — and the portal kept no independent record to
cross-check against. `dda-portal-user-roles` exists and already has exactly
the right shape, but was only ever populated by Team Management for
per-Use_Case grants, so it could not be made authoritative without a
backfill.

## Design Decisions

### Decision 1 — Reuse `dda-portal-user-roles`; do not add a table

It is already keyed (`user_id`, `usecase_id`), already read on the hot path,
already has a writer (`assign_user_role` / `remove_user_role`), and already
expresses global vs per-Use_Case. A new `dda-portal-users` table would
duplicate it and create a two-writer consistency problem. Adds three
attributes: `username`, `email`, `status` (`'enabled'` | `'disabled'`).

**Rejected:** Cognito groups as the role source. Cleaner in principle
(`AdminAddUserToGroup` is a separate IAM action from `AdminCreateUser`) but
it keeps the authority inside the pool, so the same IAM holder can still
self-grant in two calls instead of one, and it requires reworking every
role read. Worth revisiting if the portal ever federates to an external IdP;
recorded here as the alternative, not chosen.

### Decision 2 — `custom:role` becomes descriptive, not authoritative

Steps 1 and 4 of the old order are deleted. The claim is still extracted by
`get_user_from_event` (so the existing `user_info` plumbing and
`test_shared_utils_user_identity.py` keep working) but is consumed only as
`Claimed_Role` in audit details. A mismatch between `Claimed_Role` and the
registry role is itself an interesting signal and is recorded.

### Decision 3 — Absent row denies; failed lookup is a 500, not a 403

Today every exception funnels to `Role.VIEWER` (`shared_utils.py:663-664`),
which conflates "this user is a viewer", "no row exists", and "DynamoDB is
down". Under enforcement those must be distinguishable:

* no enabled row → `None` → 403 `Insufficient permissions`,
  `identity_source='absent'`
* lookup raised → a distinct `RegistryUnavailable` condition → 500
  `{'error': 'Authorization check failed'}` (the envelope `rbac_check`
  already returns for its own failures), audited `result='failure'`

This keeps a DynamoDB outage from being recorded as thousands of permission
denials, and keeps it fail-closed.

### Decision 4 — One enforcement flag, defaulting off, flipped after backfill

`PORTAL_REGISTRY_ENFORCED` (Lambda environment, sourced from a CDK context
value or SSM parameter). Off: today's resolution order, plus the new audit
fields and a WARNING log naming every request that *would* have been denied
— a dry-run that quantifies the backfill's completeness on real traffic.
On: the table in "Expected Behavior".

The flag exists because getting this wrong locks out the portal, including
the bootstrap `admin`. It is not a permanent feature: the final task removes
it once enforcement has been on in production for a full deploy cycle.

### Decision 5 — Backfill reads the pool, writes only missing rows

A one-shot idempotent script (`edge-cv-portal/backfill_portal_registry.py`,
run with portal admin credentials, dry-run by default): paginate
`list_users`, and for each **enabled** user write a global row
`{user_id: sub, usecase_id: 'global', role: custom:role or 'Viewer',
username, email, status: 'enabled', assigned_by: 'backfill',
assigned_at: now}` **only when absent** (conditional write). Disabled
Cognito users are skipped, so they stay denied.

It runs as a script rather than a Lambda custom resource: it needs to be
runnable, inspectable, and re-runnable by an operator, and it must not be
coupled to a stack deployment that could roll back.

### Decision 6 — Enforce in the shared layer, not in a new authorizer

The enforcement point is `RBACManager.get_user_role`, reached by every
`rbac_check` / `require_permission` / `super_user_only` decorator. That is
one place, already on every route, already unit-tested.

**Rejected:** wiring up `jwt_authorizer.py` (`compute-stack.ts:1123`,
attached to nothing) or a new `RequestAuthorizer` to check existence at the
edge. It would add a per-request Cognito or DynamoDB call in front of every
route, would need its own caching story, and would leave the shared layer
still trusting `custom:role` for anything it did not cover. The dead
authorizer is out of scope; it stays unattached.

**Consequence to accept:** a token minted before a role downgrade keeps
working until its next request, and only for routes whose permission the new
role still satisfies — because the check is per-request against the
registry, not against the token. That is the desired behavior and is why
Requirement 3.4 can promise that deletion takes effect on the next request.

### Decision 7 — Attribution captured at write time, never resolved later

`log_audit_event` gains an optional `identity` parameter carrying the
Attribution_Fields, and callers pass the `user` dict plus the event. Where a
call site cannot reach the event, the fields record `'unknown'` rather than
being resolved from Cognito — resolving later is exactly what the incident
made impossible, so the design must not depend on it. `sanitize_audit_details`
keeps applying; the new fields are top-level attributes, not `details`, so
they are not subject to the denylist and cannot collide with it.

`event_id` keeps its `f"{user_id}_{timestamp}"` shape for compatibility with
the two GSIs and existing readers.

### Decision 8 — App-client hardening and detection are additive and honest

Dropping `userPassword` / `adminUserPassword` (`auth-stack.ts:89-97`) raises
the cost of the incident path but does not close it (the same actor can call
`UpdateUserPoolClient`). The CloudTrail/EventBridge rule detects rather than
prevents. Both are documented as such in the runbook so the registry stays
recognized as the actual control.

## Correctness Properties

Each gets exactly one property-based test at ≥ 100 examples, tagged
`Feature: portal-jwt-role-privilege-escalation, Property {n}: {text}`.

**Property 1: No Claimed_Role grants any permission.**
For any generated `custom:role` (every valid `Role` value, invalid strings,
empty, absent) and any generated permission, with no registry row present
and enforcement on, `has_permission` is false and the decorated route
answers 403.
*Validates: 1.1, 1.4*

**Property 2: The Effective_Role is exactly the registry's role.**
For any generated registry state (global row, Use_Case row, both, neither,
disabled) and any scope, the resolved role equals the row the precedence
rules select, independent of the token's claim.
*Validates: 1.2, 1.3, 1.6*

**Property 3: Absent and unavailable are distinguishable.**
For any generated failure mode, an absent row yields 403 with
`identity_source='absent'`, and a raising lookup yields 500 with an audited
`failure`; neither yields `Viewer`.
*Validates: 1.5*

**Property 4: Backfill is idempotent and preserves current access.**
For any generated pool state, one backfill run then a second produces the
same rows, never overwrites an existing row, skips disabled users, and every
user who was authorized for a permission before enforcement is authorized
after.
*Validates: 2.1, 2.2, 2.3*

**Property 5: Every audit entry carries durable attribution.**
For any generated request (claims present/absent/partial, allow and deny
paths, both audit helpers), the written item contains all five
Attribution_Fields, their values come from the request, and no denylisted
key survives in `details`.
*Validates: 4.1, 4.2, 4.3, 4.6*

**Property 6: The User Manager keeps Cognito and the registry in step.**
For any generated sequence of create / role-change / disable / enable /
delete through the User Manager routes, the registry ends consistent with
Cognito, and after a delete or disable the principal is denied.
*Validates: 3.1, 3.3, 3.4*

## Fix Implementation

### Backend — `shared_utils.py`

* `Role` / `Permission` / the permission matrix: **unchanged**.
* `get_user_from_event`: unchanged shape; the returned `role` is documented
  as `Claimed_Role`, no longer authoritative.
* New `RegistryUnavailable(Exception)`.
* New `_lookup_identity(user_id, usecase_id) -> Optional[dict]`: reads the
  global row and, when the scope is a Use_Case, that row too; raises
  `RegistryUnavailable` on a client error instead of swallowing it.
* `get_user_role`: enforcement branch per the Expected Behavior table;
  legacy branch behind `PORTAL_REGISTRY_ENFORCED=false` with a
  `would_deny` WARNING.
* `get_user_permissions` / `has_permission`: propagate `None` as "no
  permissions" and let `RegistryUnavailable` escape to the decorator.
* `log_audit_event(..., identity=None, event=None)`: writes the
  Attribution_Fields; `record_audit_event_strict` / `finalize_audit_event`
  take the same parameter.
* New `attribution_from(event, user)` helper — the one place that reads
  `requestContext.identity.sourceIp` / `.userAgent`, lifted from the
  precedent at `quick_setup.py:166-169`.

### Backend — `rbac_middleware.py`

`rbac_check` and `super_user_only` catch `RegistryUnavailable` → 500 with
an audited failure, keep the 403 path for `None`, and pass
`attribution_from(event, user)` into every audit call. Signatures and
envelopes unchanged.

### Backend — `user_admin.py`

Registry writes inside the existing two-phase audit protocol: create writes
the global row after `admin_create_user` succeeds (a failed row write →
502 + audit `failure`, and the account is inert because Requirement 1.1
denies it); role change updates `role`; disable/enable set `status`; delete
removes the row; the last-PortalAdmin guard counts registry rows instead of
scanning Cognito attributes.

### Backend — `build_jobs.py`

`requested_by` keeps the `sub`; add `requested_by_username` /
`requested_by_email` from the same `user` dict so a Build_Job names a human.

### Infrastructure

* `auth-stack.ts`: drop `userPassword` and `adminUserPassword` from the app
  client, leaving SRP.
* `compute-stack.ts` / `build-fleet-stack.ts`: `PORTAL_REGISTRY_ENFORCED`
  on the handler environments; the User Manager role gains the
  `dda-portal-user-roles` write it needs.
* New EventBridge rule on CloudTrail management events for the pool's admin
  mutations, excluding the User Manager execution role, targeting an SNS
  topic (Requirement 6).

### Operational

`edge-cv-portal/backfill_portal_registry.py` plus a runbook section:
dry-run → review → apply → verify with the 6 known accounts → flip the flag
→ redeploy → re-verify.

## Testing Strategy

Portal backend tests live in `edge-cv-portal/backend/tests/`, run with
`~/.venvs/dda-portal-tests/bin/python -m pytest <files> -q -p no:cacheprovider`
from `edge-cv-portal/backend`, moto-backed via the existing `conftest.py`
(which builds the DynamoDB tables and imports the real `shared_utils` /
`rbac_middleware`). Never run the whole `tests/` directory in one process
(see `.kiro/steering/builds.md` and the hot-loop file noted there).

* **Exploration** (`test_portal_registry_privilege_exploration.py`) — must
  FAIL on unfixed code: the incident replay (no registry row +
  `custom:role=PortalAdmin` → expects 403, currently 200), the
  `DataScientist` claim variant against `POST /builds`, and the
  attribution assertions on a deleted user's audit row.
* **Preservation** (`test_portal_registry_privilege_preservation.py`) — the
  6 real account shapes keep their access once backfilled; per-Use_Case
  precedence unchanged; 403/500 envelopes byte-identical; audit `event_id`
  shape and both GSI projections unchanged.
* **Properties** — the six properties above, in
  `test_property_portal_registry_roles.py` (1-3),
  `test_property_portal_registry_backfill.py` (4),
  `test_property_audit_attribution.py` (5),
  `test_property_user_manager_registry.py` (6).
* **Units** — `_lookup_identity` branches; the flag's two modes; the
  `would_deny` WARNING; `attribution_from` with partial/absent claims; the
  User Manager's five transitions; the last-PortalAdmin count.
* **CDK assertions** — app client auth flows; the env var on every handler;
  the User Manager's registry write grant; the EventBridge rule's pattern
  and its User-Manager-role exclusion.
* **Repoint, recorded:** `test_rbac_global_scope_jwt_role.py` asserts that a
  JWT `PortalAdmin` with no registry rows gets 200 — the inverse of
  Requirement 1.1. Its intent (the extracted `user_info` reaches role
  resolution, so build routes do not 403 spuriously) stays valid, so each
  case gains a seeded registry row and keeps its assertions. The old
  assertions are preserved verbatim in an adjacent comment, per the
  convention used by the shadowmanager spec.
* No test asserts anything about the live account; every claim is proven
  against moto or a CDK template.
