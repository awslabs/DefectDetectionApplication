# Bugfix Requirements Document

## Introduction

The Edge CV Portal API grants privilege from a **self-asserted JWT claim**
(`custom:role`) and keeps **no portal-side record of who its users are**.
Identity, role, and existence all live in the Cognito user pool, which the
portal treats as fully trusted. Consequently:

1. A single `cognito-idp:admin-create-user` call carrying
   `Name=custom:role,Value=PortalAdmin` mints a **fully privileged portal
   principal**. No portal-side provisioning, approval, group membership, or
   registry entry is required.
2. Deleting that Cognito user afterwards **destroys the only copy of the
   identity**. Audit entries key on the Cognito `sub`, so the action becomes
   permanently unattributable.
3. Nothing detects the sequence, because every individual call is a normal
   AWS API call against the pool.

This is not a theoretical hole: it was exercised against the production
portal (account 164152369890) on 2026-09-17. The build system accepted a
LocalServer build request from a principal that no longer exists.

The actor in the recorded incident is believed to be an agent/automation
run by the portal owner rather than a third party, and the AWS credentials
used were legitimately held. That does not change the defect: the portal
must not convert "holds Cognito admin IAM" into "is a portal administrator",
and must never accept an action it cannot attribute afterwards.

### Incident Record (verified evidence, account 164152369890)

CloudTrail, source IP `12.148.187.67`, user agent `aws-cli/1.36.4`, all
against user pool `us-east-1_2r9jpbWIe` (`dda-portal-users`):

| Time (UTC) | Event | Note |
|---|---|---|
| 14:26:02 | `AdminCreateUser` | user name hidden by CloudTrail |
| 14:26:04 | `AdminSetUserPassword` | permanent password set |
| 14:26:04 | `InitiateAuth` | authenticated as the new user, `sub` `74189498-9061-7062-a1e0-f9db61c506c2` |
| 14:26:06 | `POST /builds` accepted | Build_Job `2ec3d7ab-6c37-4f29-8718-177b6b64da91`, JP7, ref `feat/capture-phase-outputs` |
| 14:26:08 | `AdminDeleteUser` | principal destroyed |

Durable evidence left behind:

* `dda-portal-build-jobs` item `2ec3d7ab-…` has
  `requested_by = "74189498-9061-7062-a1e0-f9db61c506c2"`.
* `dda-portal-audit-log` has `action=build_requested`,
  `event_id = "74189498-9061-7062-a1e0-f9db61c506c2_1789655167050"`,
  `user_id = "74189498-…"`.
* `aws cognito-idp list-users` across **all seven** user pools in the
  account resolves that `sub` to **nothing**. The pool holds 6 users, none
  with that `sub`.
* The build ran to completion and published
  `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.40** plus two ECR images —
  a real fleet-visible artifact produced by an unattributable request.

Elapsed time from "no portal access" to "privileged portal action": **4
seconds**. Total footprint that survives: one orphaned `sub`.

### Bug Condition

The bug is present when **either** holds:

* **(C1) Privilege from an unprovisioned claim.** A Cognito user that has
  never been provisioned in the portal (no `dda-portal-user-roles` row)
  presents an ID token whose `custom:role` names a privileged role, and the
  API authorizes a permission that role carries.
* **(C2) Unattributable accepted action.** The API accepts a mutating
  request and the only identity recorded is the Cognito `sub`, so deleting
  the Cognito user erases every human-readable trace of the actor.

Both are currently true for every portal route.

## Bug Analysis

### Current Behavior (Defect)

**Token validation stops at cryptographic validity.** Every route is
guarded by an AWS-managed `CognitoUserPoolsAuthorizer`
(`edge-cv-portal/infrastructure/lib/build-fleet-stack.ts:789` for
`/builds`, and one instance per route stack in `api-core-stack.ts`,
`api-gateway-stack.ts`, `user-admin-api-stack.ts`,
`node-designer-api-stack.ts`, `dda-labeling-api-stack.ts`,
`quick-setup-api-stack.ts`, `workflow-manager-gaps-api-stack.ts`,
`workflow-tuning-api-stack.ts`). It verifies signature, `exp`, `iss`,
`aud`, `token_use` against the pool. It does **not** check that the user
still exists, does not require group membership, and has no allowlist. A
token remains valid for its full lifetime after `AdminDeleteUser`.

**The role comes from the token.**
`shared_utils.get_user_from_event` (`shared_utils.py:100-146`):

```python
user_id = claims.get('sub', 'unknown')
...
'role': claims.get('custom:role', 'Viewer')
```

`RBACManager.get_user_role` (`shared_utils.py:~601-664`) then resolves in
this order, and **step 1 never touches DynamoDB**:

```python
jwt_role = user_info.get('role') if user_info else None
# 1. Check if user is PortalAdmin from JWT (global admin)
if jwt_role == 'PortalAdmin':
    return Role.PORTAL_ADMIN
...
# 4. Fall back to JWT role (Cognito custom:role attribute)
if jwt_role:
    return Role(jwt_role)
```

So `custom:role` is authoritative for `PortalAdmin` unconditionally, and
authoritative for every other role whenever no DynamoDB row applies. At
the `global` scope used by the build routes
(`rbac_middleware.require_builds_submit()` →
`rbac_check(..., allow_global=True)`, `rbac_middleware.py:422-426`), step 3
(per-Use_Case rows) is skipped entirely, so the claim is the **only** input.

`custom:role` is a `mutable: true` custom attribute settable at creation
time (`auth-stack.ts:38-48`), and the app client enables both
`userPassword` and `adminUserPassword` flows — the latter commented
"Enable ADMIN_NO_SRP_AUTH for testing" (`auth-stack.ts:89-97`). The pool
has no `advancedSecurityMode`, there is no WAF on the API, and no Cognito
group is ever created or required.

**No portal-side user record exists.** `user_admin.create_account`
(`user_admin.py:474-536`) writes Cognito attributes plus audit events and
nothing else — no `dda-portal-user-roles` row, no group membership. A
CLI-created user is therefore **byte-for-byte indistinguishable** from a
portal-created one at request time.

**Audit entries cannot outlive the user.** `log_audit_event`
(`shared_utils.py:148-171`) writes exactly:

```python
item = {
    'event_id': f"{user_id}_{timestamp}", 'timestamp': timestamp,
    'user_id': user_id, 'action': action, 'resource_type': resource_type,
    'resource_id': resource_id, 'result': result, 'details': details or {},
    'ttl': timestamp + (90 * 24 * 60 * 60 * 1000),
}
```

No username, no email, no source IP, no user agent, and no resolution of
`sub` → identity at write time. The `sub` is embedded in the partition key.
Failures are swallowed, so audit loss never fails a request. The strict
two-phase helpers used by the User Manager
(`record_audit_event_strict` / `finalize_audit_event`) share the same
identity fields.

**Nothing detects the out-of-band mutation.** No EventBridge rule, alarm,
or notification fires on `AdminCreateUser` / `AdminSetUserPassword` /
`AdminUpdateUserAttributes` / `AdminDeleteUser` against the portal pool.

### Expected Behavior (Correct)

* A request whose `sub` has **no Portal_Identity registry entry** is denied
  with 403 and audited, no matter what `custom:role` claims.
* Portal privilege is resolved from the **registry**, which only the
  portal's own User Manager writes. `custom:role` is descriptive metadata,
  never a privilege source.
* Every accepted or denied request records a **durable, human-readable
  actor**: `sub` plus username, email, source IP, and user agent captured
  from the request at write time, so a later `AdminDeleteUser` cannot erase
  attribution.
* Authenticating with a password issued by `AdminSetUserPassword` through
  the admin/non-SRP auth flows is **not possible against the portal app
  client** (defense in depth).
* Any Cognito admin mutation on the portal pool that did **not** originate
  from the portal's own User Manager role raises a **detection signal**.

### Unchanged Behavior (Regression Prevention)

* Every currently working portal user keeps exactly the access they have
  today. This requires a backfill of the registry from the pool's current
  `custom:role` values before enforcement turns on; without it, enforcement
  locks out the whole portal, including the bootstrap `admin` account.
* The browser sign-in flow is untouched: Amplify v6 `signIn` uses SRP by
  default and no code pins `authFlowType`
  (verified: no `authFlowType` / `USER_PASSWORD_AUTH` / `ADMIN_NO_SRP_AUTH`
  reference anywhere in `frontend/src`, `backend`, or the deploy scripts),
  so removing the password auth flows from the app client does not affect
  it.
* The RBAC decision surface stays where it is: `rbac_check` /
  `require_permission` / `super_user_only` keep their signatures, response
  envelopes (403 `{'error': 'Insufficient permissions', ...}`), and their
  `unauthorized_access` audit action.
* Per-Use_Case role assignment via Team Management keeps working, including
  the precedence of a Use_Case row over the account's global role.
* The `dda-portal-audit-log` schema stays backward compatible: existing
  keys (`event_id`, `timestamp`, `user_id`, `action`, `resource_type`,
  `resource_id`, `result`, `details`, `ttl`) keep their meaning and the two
  GSIs keep working. New identity fields are additive.
* The audit details denylist (`password` / `verifier` / `hash` / `temp*`,
  `sanitize_audit_details`) keeps applying to everything written.

### Explicit Non-Goals

* **Defending against an actor who controls the AWS account.** Anyone able
  to edit IAM, CloudFormation, or the Lambda code can grant themselves
  anything. This spec closes the gap where **one Cognito admin API call**
  yields portal privilege, and makes out-of-band mutations attributable and
  detectable. Restricting `cognito-idp:Admin*` via SCP or a permission
  boundary is the complementary control and is an operational task, not
  part of this spec.
* Migrating to Cognito groups as the role source, replacing the pool, or
  enabling Cognito advanced security. Noted as alternatives in design.md.
* Reviving `jwt_authorizer.py`. It is deployed but attached to no method
  (verified: `jwtAuthorizerHandler` at `compute-stack.ts:1123` is never
  referenced again and no `TokenAuthorizer`/`RequestAuthorizer` exists in
  `infrastructure/lib/`). This spec does not wire it up; see design.md
  Decision 6 for why the enforcement point is the shared layer instead.

## Requirements

### Requirement 1 — Portal_Identity registry is the only source of privilege

**User Story:** As the portal owner, I want portal privilege to come from a
record only the portal writes, so that creating a Cognito user cannot grant
portal access.

#### Acceptance Criteria

1. WHEN a request presents a valid token whose `sub` has no Portal_Identity
   registry entry THEN the API SHALL deny the request with HTTP 403 and the
   standard `Insufficient permissions` envelope, regardless of the
   `custom:role` claim.
2. WHEN a request's `sub` has a registry entry THEN the effective role SHALL
   be the registry role, and the `custom:role` claim SHALL NOT raise it.
3. WHEN a registry entry names a role for a specific Use_Case AND the
   request is scoped to that Use_Case THEN the Use_Case role SHALL take
   precedence over the account's global role (today's step-3 precedence).
4. WHEN `custom:role` names a privileged role AND the registry names a
   lower-privileged role THEN the effective role SHALL be the registry role.
5. WHEN the registry lookup fails (throws) THEN the API SHALL deny the
   request, SHALL answer HTTP 500 with an availability error rather than a
   permission error, and SHALL audit `result='failure'` — a lookup outage
   SHALL NOT be recorded as a privilege decision, and SHALL NOT silently
   downgrade the caller to `Viewer`.
6. WHEN a registry entry is marked disabled THEN the request SHALL be denied
   exactly as an absent entry is.

### Requirement 2 — Backfill before enforcement

**User Story:** As an operator, I want the registry populated from the
current pool before enforcement begins, so that nobody is locked out.

#### Acceptance Criteria

1. WHEN the backfill runs THEN it SHALL create one global registry entry per
   existing enabled Cognito user, carrying that user's current effective
   role (its `custom:role`, or `Viewer` when the attribute is absent), and
   SHALL record username and email alongside.
2. WHEN the backfill encounters an existing registry entry for a `sub` THEN
   it SHALL leave that entry unchanged (idempotent, re-runnable).
3. WHEN enforcement is enabled THEN every user who could reach a route
   before the change SHALL still reach it, verified against the pool's
   current 6 accounts including the bootstrap `admin`.
4. WHEN the backfill has not run THEN enforcement SHALL be disabled, and the
   enforcement switch SHALL be a single explicit configuration value.

### Requirement 3 — The User Manager maintains the registry

**User Story:** As a portal admin, I want the portal's own user management
to keep being the way users are provisioned.

#### Acceptance Criteria

1. WHEN `POST /admin/users` creates an account THEN it SHALL write the
   Cognito user AND the global registry entry, under the existing
   audit-before-effect protocol.
2. WHEN account creation succeeds in Cognito but the registry write fails
   THEN the response SHALL report failure and the account SHALL NOT be left
   privileged — the absent registry entry already denies access
   (Requirement 1.1), and the audit entry SHALL record the partial state.
3. WHEN a role change is applied THEN the registry entry SHALL be updated
   and SHALL be the value that takes effect.
4. WHEN an account is deleted or disabled THEN its registry entry SHALL be
   removed or marked disabled, so a token minted before the change stops
   being privileged on its next request.
5. WHEN the last PortalAdmin guard runs THEN it SHALL count registry
   entries rather than scanning Cognito attributes.

### Requirement 4 — Durable attribution on every audited action

**User Story:** As an incident responder, I want to know who did something
even if their account is gone.

#### Acceptance Criteria

1. WHEN any audit entry is written THEN it SHALL carry, in addition to
   today's fields: `username`, `email`, `source_ip`, `user_agent`, and the
   `identity_source` that decided the role (`registry` or `absent`).
2. WHEN those values are captured THEN they SHALL be taken from the request
   being audited (the authorizer claims and
   `requestContext.identity`), NOT resolved from Cognito later, so a
   subsequent `AdminDeleteUser` cannot erase them.
3. WHEN a claim is missing THEN the field SHALL record the literal
   `'unknown'` and the entry SHALL still be written.
4. WHEN a request is denied for an absent registry entry THEN the audit
   entry SHALL record `action='unauthorized_access'` with
   `identity_source='absent'` and the claimed `custom:role`.
5. WHEN a mutating resource records its actor (e.g. `requested_by` on a
   Build_Job) THEN it SHALL record the human-readable username or email
   alongside the `sub`.
6. WHEN details are written THEN the existing denylist SHALL still redact
   `password` / `verifier` / `hash` / `temp*` keys.

### Requirement 5 — Remove the non-SRP auth flows from the portal app client

**User Story:** As the portal owner, I want an admin-set password to be
unusable against the portal client.

#### Acceptance Criteria

1. WHEN the portal app client is synthesized THEN `adminUserPassword` and
   `userPassword` auth flows SHALL be disabled, leaving SRP.
2. WHEN the browser signs in after the change THEN sign-in, the
   new-password challenge, and forgot-password SHALL all still work.
3. WHEN this control is documented THEN it SHALL be stated as defense in
   depth only: an actor with `cognito-idp:UpdateUserPoolClient` can
   re-enable the flow, which is why Requirement 1 (not this one) is the
   actual fix.

### Requirement 6 — Detect out-of-band Cognito administration

**User Story:** As the portal owner, I want to be told when someone
administers the pool outside the portal.

#### Acceptance Criteria

1. WHEN `AdminCreateUser`, `AdminSetUserPassword`,
   `AdminUpdateUserAttributes`, `AdminAddUserToGroup`, `AdminEnableUser`,
   `AdminDisableUser`, or `AdminDeleteUser` occurs on the portal user pool
   AND the caller identity is not the portal's own User Manager execution
   role THEN a detection signal SHALL be raised.
2. WHEN the signal is raised THEN it SHALL carry the event name, the caller
   ARN, the source IP, the user agent, and the event time.
3. WHEN the portal's User Manager performs the same operations THEN no
   signal SHALL be raised.

### Requirement 7 — Regression coverage for the incident

**User Story:** As a maintainer, I want the exact incident sequence to be a
test.

#### Acceptance Criteria

1. WHEN the incident sequence is replayed (user created directly with
   `custom:role=PortalAdmin`, no registry entry, token presented to
   `POST /builds`) THEN the request SHALL be denied 403 and audited.
2. WHEN the same user has a registry entry naming a build-capable role THEN
   the request SHALL be accepted, proving the fix denies only unprovisioned
   principals.
3. WHEN a user is deleted from the pool after acting THEN the audit entry
   for that action SHALL still name the username and email.
4. WHEN the existing authorization suites run
   (`test_build_rbac.py`, `test_rbac_global_scope_jwt_role.py`,
   `test_shared_utils_user_identity.py`, the `test_user_admin_*.py` set)
   THEN they SHALL pass, with any intentional repoint recorded in the task
   outcome. Note that `test_rbac_global_scope_jwt_role.py` currently asserts
   that a JWT `PortalAdmin` with **no** registry rows is authorized — the
   direct inverse of Requirement 1.1 — so it SHALL be repointed to seed the
   registry entry, preserving its original intent (the JWT role reaches
   resolution) while the registry supplies the privilege.
