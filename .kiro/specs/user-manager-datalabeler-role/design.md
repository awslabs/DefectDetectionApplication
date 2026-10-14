# User Manager DataLabeler Role — Bugfix Design

## Overview

The dda-data-labeling feature is dead on arrival in the deployed portal
because of two small, verified gaps (bugfix.md Incident Record, 2026-08-17,
account 164152369890):

1. **Frontend role vocabulary drift (Defect 1)**:
   `edge-cv-portal/frontend/src/pages/admin/UserManagerModals.tsx` exports
   its own five-role `PORTAL_ROLES` (comment still cites the
   portal-user-manager five-count) while the backend `user_admin.py`
   already accepts six (DataLabeler appended by dda-data-labeling, Req
   2.1). Both the create-user and change-role dropdowns map over that
   array, so DataLabeler is never offered, no labeler account can exist
   via the UI, and `LabelingTeams.tsx` add-member candidates
   (`role === 'DataLabeler'`) are permanently empty. Fix: append
   `'DataLabeler'` to the frontend array (one string — both modals pick it
   up) plus ONE conscious pinned-test repoint (the "exactly the five
   defined roles" test becomes six).
2. **Missing IAM grant (Defect 2)**: `finalize_audit_event`
   (shared_utils.py) recovers the audit table's (event_id, timestamp)
   range key with a `dynamodb:Query`, but `createLambdaRole('UserAdmin')`
   only gets `auditLogTable.grantWriteData` — so in the live account every
   user-admin mutation 500s AFTER its Cognito effect is applied and its
   audit entry sticks at 'pending' (verified AccessDeniedException,
   request id 79dc2dd8-2e10-486a-a0e6-aec5ef805d37). Fix: the minimal
   missing grant — `dynamodb:Query` on the audit-log table for the
   userAdminHandler in compute-stack.ts. No backend code change; auditing
   semantics untouched.

Scope guards: portal-only (frontend + one CDK grant); no device-side
`src/` file, no component build, no preservation-tracked file; rollout is
a portal deploy.

## Glossary

- **Bug_Condition (C)**: a User-Manager role selection whose offered
  vocabulary is missing DataLabeler, OR a UserAdmin-Lambda audit-finalize
  step executed under a role whose audit-table grants lack
  `dynamodb:Query` (see Bug Details).
- **Property (P)**: both modals offer exactly the backend's six-role
  vocabulary, and the synthesized UserAdmin role permits the finalize
  Query so mutations return success and audit entries reach terminal
  results.
- **Preservation**: the five original roles (order, preselect), all modal
  validation/submission flows, the backend endpoints, the
  audit-before-effect protocol (including finalize's raise-on-failure
  contract), and every other role's grants — all unchanged.
- **Frontend `PORTAL_ROLES`**: the const array in `UserManagerModals.tsx`
  (~L63) mapped into `<Select>` options by the create-user modal (~L270)
  and the change-role modal (~L515); also imported by
  `UserManagerModals.test.tsx` for the pinned dropdown assertion.
- **Backend `PORTAL_ROLES`**: the six-tuple in
  `edge-cv-portal/backend/functions/user_admin.py` (~L74) validating
  create and role-change submissions — already includes DataLabeler.
- **`finalize_audit_event`**: second phase of the audit-before-effect
  protocol (`edge-cv-portal/backend/layers/shared/python/shared_utils.py`
  ~L248): `table.query(KeyConditionExpression=Key('event_id').eq(event_id))`
  to recover the (event_id, timestamp) key, then `update_item` to the
  terminal result; raises on lookup/update failure BY DESIGN so callers
  surface the problem.
- **`createLambdaRole`**: compute-stack.ts (~L289) base portal Lambda role
  factory; grants `auditLogTable.grantWriteData(role)` (~L301) —
  PutItem/UpdateItem/DeleteItem/BatchWriteItem, no Query.
- **`userAdminHandler`**: the user_admin.py Lambda (compute-stack.ts
  ~L1668), role `createLambdaRole('UserAdmin')` (= the incident's
  `UserAdminRole2557D264`), plus handler-specific grants (cognito-idp,
  SES, edge-credentials, account-sync).
- **Audit-before-effect**: `record_audit_event_strict(... 'pending')` →
  guarded Cognito operation → `finalize_audit_event(event_id, terminal)`;
  every user_admin mutating endpoint follows it.

## Bug Details

### Bug Condition

Defect 1 manifests whenever either User-Manager modal renders its role
dropdown: the options are mapped from the frontend's stale five-role
array, so DataLabeler is structurally absent regardless of backend
support. Defect 2 manifests whenever user_admin.py reaches an audit
finalize under the deployed IAM: the Query in `finalize_audit_event` is
denied, the exception propagates (finalize raises by design and the
call sites for the success path are unwrapped), and the handler 500s
after the effect.

**Formal Specification:**

```
FUNCTION isBugCondition(X)
  INPUT: X of type UserAdminInteraction
  OUTPUT: boolean

  IF X.kind = 'role-selection' THEN            // Defect 1
    RETURN 'DataLabeler' NOT IN offeredRoles(X.modal)
  END IF

  IF X.kind = 'audit-finalize' THEN            // Defect 2
    RETURN 'dynamodb:Query' NOT IN
           grantedActions(UserAdminRole, 'dda-portal-audit-log')
  END IF

  RETURN false
END FUNCTION
```

### Examples

- **Create labeler (Defect 1)**: PortalAdmin opens "Create user" to add a
  labeling-team member. Expected: role dropdown offers DataLabeler.
  Actual: five options only (user screenshot); the live pool had zero
  DataLabeler users until the direct-Lambda-invoke remediation created
  `ryan-labeler`.
- **Convert existing account (Defect 1)**: change-role modal for an
  existing account. Expected: DataLabeler selectable. Actual: five
  options; the pinned test 'offers exactly the five defined roles with
  the current role preselected (Requirement 5.2)' asserts the defect.
- **Labeling teams starve (Defect 1 consequence)**: LabelingTeams
  add-member filter finds no `role === 'DataLabeler'` account → the
  labeling backend is unreachable end-to-end.
- **Remediation create 500s (Defect 2)**: POST /api/v1/admin/users,
  2026-08-17 17:03Z — audit-pending written, `admin_create_user`
  succeeded, then `AccessDeniedException ... UserAdminRole2557D264 ... not
  authorized to perform: dynamodb:Query on ...
  table/dda-portal-audit-log` → HTTP 500 despite the account existing;
  audit entry presumably stuck 'pending'.
- **Edge case (failure finalize)**: the failure paths (e.g. duplicate
  username → 409) also call `finalize_audit_event` — under the deployed
  IAM those 4xx responses would also degrade to 500s.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- **Five original roles (3.1)**: both dropdowns keep offering PortalAdmin,
  UseCaseAdmin, DataScientist, Operator, Viewer in the existing order;
  the change-role modal keeps preselecting the current role.
- **Modal flows (3.2, 3.3)**: create-user validation (missing-field,
  email shape, role required), the password-policy pre-check, the
  temp-password/invite confirmation, `setAdminUserRole` submission, and
  rejection surfacing (incl. the last-PortalAdmin guard) — all unchanged.
- **Backend endpoints (3.4)**: no backend code change at all;
  user_admin.py and shared_utils.py are untouched.
- **Audit protocol (3.5)**: finalize keeps its Query-based key recovery
  and its raise-on-failure contract; nothing wraps or swallows finalize
  errors; auditing is never weakened.
- **Other grants (3.6)**: the UserAdmin role's existing grants
  (audit write, cognito-idp, SES, edge-credentials, account-sync,
  accountSyncHandler invoke) and every other role's audit-log grants
  (base write-only, quick-setup read/write, audit-logs read) unchanged.

**Scope:**

All inputs that do NOT involve the two gaps are completely unaffected:
every non-role-dropdown piece of the User Manager UI, every other
frontend page already handling DataLabeler (types/index.ts `UserRole`,
Layout, DataLabelerRedirect, Login landing, RequireRole gates,
listAdminUsers rendering — verified already six-role-aware; the modals'
array is the ONLY stale frontend enumeration of the assignable
vocabulary), every other Lambda's IAM, and all backend behavior.

## Hypothesized Root Cause

Verified at every layer, not hypothesized:

1. **Frontend/backend vocabulary fork**: dda-data-labeling extended the
   backend `PORTAL_ROLES` (user_admin.py, comment citing its Req 2.1) and
   the shared `UserRole` type, Layout/RBAC, and labeling pages — but the
   User-Manager modals module keeps its own independent five-role array
   (comment still citing portal-user-manager Requirement 5.2), and both
   dropdowns map over it. Grep confirms no other frontend file enumerates
   the assignable-role vocabulary.
2. **Pinned test enshrines the fork**:
   `UserManagerModals.test.tsx` 'offers exactly the five defined roles
   with the current role preselected (Requirement 5.2)' asserts the
   dropdown equals `[...PORTAL_ROLES]` — the conscious repoint candidate
   (five → six; dda-data-labeling Req 2.1 supersedes the count).
3. **IAM grant never included Query**: `createLambdaRole` grants
   `auditLogTable.grantWriteData` only; the audit READ side was granted
   per-consumer (audit_logs handler `grantReadData`, quick-setup role
   `grantReadWriteData`) but user_admin — the only `createLambdaRole`
   consumer calling `finalize_audit_event` — never got its Query. Moto
   does not enforce IAM, so the green backend test suites never caught
   it; the gap is deploy-only.

## Design Decisions

### Decision 1: Append 'DataLabeler' to the frontend PORTAL_ROLES (one string)

`UserManagerModals.tsx` `PORTAL_ROLES` becomes the six-value array with
`'DataLabeler'` appended LAST (preserves the existing order of the five,
3.1, and matches the backend tuple's order). Both modals map the same
array, so one edit fixes create and convert. The doc comment is updated:
"The six defined Portal_Role values (portal-user-manager Requirement 5.2,
extended by dda-data-labeling Requirement 2.1)". No other frontend file
changes — the `UserRole` type, Layout, redirect, and RBAC gates already
handle DataLabeler, and the user list renders the backend-returned role
verbatim (DataLabeler accounts already display correctly).

### Decision 2: ONE conscious pinned-test repoint

`UserManagerModals.test.tsx` 'offers exactly the five defined roles with
the current role preselected (Requirement 5.2)' pins the defect. It is
repointed — never weakened or deleted: renamed to state the six-role
contract (citing dda-data-labeling Req 2.1 as superseding the count), and
its assertion (`toEqual([...PORTAL_ROLES])`) mechanically follows the
array, so the repoint is the rename/redocumentation plus the file-header
comment. The old test name and assertion are recorded verbatim in the
preservation task BEFORE the fix so the repoint diff is auditable. Every
other test in that file must keep passing unmodified.

### Decision 3: IAM fix = the minimal missing grant, not a code change

`finalize_audit_event`'s Query is the code's intent: the two-phase
protocol hands callers only the `event_id` handle; the table key is
(event_id, timestamp), so the range key must be recovered server-side.
Parsing the timestamp out of the event_id string
(`{user_id}_{timestamp}_{hex}`) would be fragile (user_id may contain
underscores) and switching to a key-addressed Get/Update would change
shared audit code used by other callers — a larger blast radius for zero
audit benefit. Auditing is never weakened. Therefore compute-stack.ts
adds, next to the existing userAdminHandler grants (~L1686):

```
props.auditLogTable.grant(userAdminHandler, 'dynamodb:Query');
```

— exactly the missing action, narrower than `grantReadData` (no
Scan/GetItem/BatchGet). Precedents in the same stack: quick-setup's
dedicated role got `grantReadWriteData` for the same finalize path;
audit_logs got `grantReadData`.

### Decision 4: Fix the UserAdmin handler, not createLambdaRole

The base factory stays write-only on the audit table (least privilege for
the ~25 handlers that only ever `log_audit_event`/put). Grep confirms
user_admin.py is the only `createLambdaRole` consumer calling
`finalize_audit_event`; quick_setup.py has its own role and already
works. Scoping the grant to userAdminHandler keeps the blast radius one
role.

### Decision 5: Honesty guard — deployed-IAM truth is account-tier only

Host tests prove three things: (a) the modal option lists (vitest), (b)
the finalize path issues a `dynamodb:Query` against the audit table and
terminalizes entries (moto — but moto does NOT enforce IAM, so the live
AccessDenied is NOT reproducible host-side), and (c) the synthesized
CloudFormation grants the UserAdmin role `dynamodb:Query` on the audit
table (jest CDK static assertions — the synthetic-data-s3-permissions
precedent). The real claims — a UI-created DataLabeler account, a 200
response, a terminal audit entry in account 164152369890, a labeling team
gaining a member — are assigned exclusively to the USER ACTION
verification task, which also checks/remediates the stuck-'pending' audit
entry from the 2026-08-17 remediation create. Do not write a test that
pretends to exercise the real account.

## Requirements Traceability

- dda-data-labeling Req 2.1 ("Data_Labeler role ... assigned to and
  revoked from portal users through the existing user administration
  functions") is the superseding requirement Defect 1 violates; this
  spec's 2.1–2.3 restate it for the UI leg.
- portal-user-manager Req 5.2 (and its creation clause 8) defined the
  original five-role count that the frontend comment and pinned test
  still cite; the repoint (Decision 2) re-cites both specs.
- portal-user-manager's audit requirements (its Req 6.x/12.x family —
  audit-before-effect, finalize on success/failure) are what Defect 2
  breaks in the deployed account; this spec's 2.4/3.5 restate the
  operative contract.

## Correctness Properties

Property 1: Bug Condition - DataLabeler Offered and Audit Finalize Permitted

_For any_ input where the bug condition holds (isBugCondition returns
true — a modal role dropdown missing DataLabeler, or the synthesized
UserAdmin role lacking audit-table Query), the fixed portal SHALL offer
exactly the backend's six-role vocabulary in both the create-user and
change-role dropdowns, and the synthesized UserAdmin role policy SHALL
allow `dynamodb:Query` on the audit-log table so user-admin mutations
finalize their audit entries and return success.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Everything Outside the Two Gaps Is Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition
returns false), the fixed portal SHALL behave identically to the original:
the five original roles keep their order and preselection, all modal
validation/submission/rejection flows are unchanged, no backend code
changes (finalize keeps its Query call site and raise-on-failure
contract, verified by the moto finalize-path tests), and the UserAdmin
role's existing grants plus every other role's audit-log grants are
unchanged in the synthesized templates.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Property 3: Fix Checking - Frontend/Backend Role Vocabulary Parity

_For any_ role in the backend's six-value `PORTAL_ROLES`, both fixed
modals SHALL offer it (option lists exactly equal to the backend tuple,
in order), and a DataLabeler selection SHALL submit through the existing
handlers (`createAdminUser` payload role='DataLabeler';
`setAdminUserRole(username, 'DataLabeler')`) unchanged in shape.

**Validates: Requirements 2.1, 2.2, 2.3, 3.1**

Property 4: Fix Checking - Scoped Audit Query Grant in the Synthesized Template

_For any_ synthesized ComputeStack, the UserAdmin role's policies SHALL
allow `dynamodb:Query` on the audit-log table; the grant SHALL be scoped
to the UserAdmin role only (base `createLambdaRole` roles remain
write-only on the audit table), and the role's pre-existing statements
(audit write, cognito-idp, SES) SHALL be present unchanged.

**Validates: Requirements 2.4, 3.6**

## Fix Implementation

### Changes Required

**File 1**: `edge-cv-portal/frontend/src/pages/admin/UserManagerModals.tsx`

**Specific Changes**:
1. Append `'DataLabeler'` to the exported `PORTAL_ROLES` array (~L63) and
   update the doc comment to cite dda-data-labeling Req 2.1 alongside
   Requirement 5.2 (Decision 1). Both modals (~L270, ~L515) pick the
   change up with no further edits.

**File 2** (conscious test repoint, Decision 2):
`edge-cv-portal/frontend/src/pages/admin/UserManagerModals.test.tsx`

**Specific Changes**:
2. Repoint 'offers exactly the five defined roles with the current role
   preselected (Requirement 5.2)': rename to the six-role contract citing
   dda-data-labeling Req 2.1; the `toEqual([...PORTAL_ROLES])` assertion
   follows the array mechanically; update the file-header comment's
   "five" wording. No other test in the file touched.

**File 3**: `edge-cv-portal/infrastructure/lib/compute-stack.ts`

**Specific Changes**:
3. Next to the existing userAdminHandler grants (~L1686), add
   `props.auditLogTable.grant(userAdminHandler, 'dynamodb:Query');` with
   a comment citing `finalize_audit_event`'s range-key recovery Query and
   this spec (Decision 3).

**Explicitly NOT changed**: `user_admin.py`, `shared_utils.py` (the audit
protocol and its raise-on-failure contract), `createLambdaRole`'s base
grants, quick-setup/audit-logs grants, `LabelingTeams.tsx`,
`types/index.ts`, Layout/RBAC/redirect components, any `src/` device-side
file, any recipe, any Dockerfile. **No preservation-tracked file is
touched → no security-baseline rebaselines** (the gates task verifies the
claim). **No component build** — portal-only, shipped by a portal deploy.

## Testing Strategy

### Validation Approach

Two-phase per the bugfix methodology: surface the counterexamples on the
UNFIXED tree (exploration), baseline what must survive (preservation,
observation-first), then implement and verify the flip plus the fix-check
suites. Frontend legs are vitest single runs from `edge-cv-portal/frontend`
with `PATH="$HOME/.local/node/bin:$PATH"`. Infrastructure legs are jest
CDK static assertions from `edge-cv-portal/infrastructure` (the
synthetic-data-s3-permissions precedent; synth is slow — generous
timeouts). Backend legs run from `edge-cv-portal/backend` WITH conftest
(moto `aws_stack`; Hypothesis profiles are conftest-registered — no
hardcoded `max_examples`) in the venv
`/home/ubuntu/.venvs/dda-portal-tests`.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate both defects BEFORE
implementing the fix; confirm the root-cause analysis.

**Test Plan**: NEW
`edge-cv-portal/frontend/src/pages/admin/UserManagerModals.dataLabelerRole.test.tsx`
(defect 1) and NEW
`edge-cv-portal/infrastructure/test/user-admin-audit-grant.test.ts`
(defect 2). Run on the UNFIXED tree and observe failures.

**Test Cases**:
1. **Create-user dropdown offers DataLabeler** (defect 1.1): open the
   create modal's role select, assert the option list equals the six-role
   vocabulary (will fail on unfixed code — five options)
2. **Change-role dropdown offers DataLabeler** (defect 1.2): same
   assertion on the RoleModal select (will fail on unfixed code)
3. **UserAdmin role may Query the audit table** (defect 1.4): synthesize
   ComputeStack, collect the policies attached to the UserAdmin role,
   assert some statement allows `dynamodb:Query` on the audit-log table
   ARN (will fail on unfixed code — write-only actions)

**Expected Counterexamples**:
- Option lists exactly `['PortalAdmin','UseCaseAdmin','DataScientist',
  'Operator','Viewer']` in both modals
- No UserAdmin-role statement carrying `dynamodb:Query` for the audit
  table — only the grantWriteData action set

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed portal
produces the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  IF X.kind = 'role-selection' THEN
    ASSERT offeredRoles'(X.modal) = backend PORTAL_ROLES   // six, in order
    ASSERT submit(DataLabeler) reaches the existing handler unchanged
  ELSE  // audit-finalize
    ASSERT 'dynamodb:Query' IN grantedActions'(UserAdminRole, audit-table)
  END IF
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed
portal produces the same result as the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT modalBehavior(X) = modalBehavior'(X)      // validation, submit,
                                                   // rejection surfacing
  ASSERT finalize_audit_event(X) = finalize_audit_event'(X)  // untouched code
  ASSERT grants(role, X) = grants'(role, X)        // every non-UserAdmin role;
                                                   // UserAdmin's other stmts
END FOR
```

**Testing Approach**: The input domains here are small and enumerable
(six roles, a fixed set of IAM statements), so example-based pins and
enumerated `it.each` cases carry most of the weight; the moto
finalize-path test asserts the protocol invariant (pending → terminal,
Query issued) across the mutating endpoints.

**Test Plan**: Observe on UNFIXED code first, record, then encode:
1. **Pinned-test record (Decision 2)**: record verbatim the current name
   and assertion of the five-roles test — the ONE conscious repoint
2. **Modal suite baselines**: `UserManagerModals.test.tsx`,
   `UserManager.test.tsx`, `UserManagerSyncPanel.test.tsx` green with
   recorded counts
3. **Finalize-path moto test**: NEW
   `edge-cv-portal/backend/tests/test_user_admin_audit_finalize_preservation.py`
   — a user-admin mutation records pending, issues `Query` on the audit
   table during finalize (observed via a recording wrapper), and lands
   the entry at a terminal result; PASSES on unfixed code (moto has no
   IAM) and must keep passing — it pins that `dynamodb:Query` is exactly
   the action the deployed role needs
4. **Backend suite baselines**: the `test_user_admin_*.py` suites and
   `test_dda_labeling_rbac_role.py` green with recorded counts
5. **Grant baselines**: in the jest file, pin the UserAdmin role's
   existing statements (audit write actions, cognito-idp, SES) and that
   base `createLambdaRole` roles stay write-only on the audit table

### Unit Tests

- Both dropdowns' option lists equal the backend vocabulary (six, order)
- DataLabeler create/convert submissions reach the API layer unchanged
- The repointed test: six roles with current-role preselect

### Property-Based Tests

- Role-vocabulary parity enumerated across all six roles (`it.each`
  style — the domain is fixed and small; no generator needed)
- Moto finalize-path invariant across the mutating endpoints: pending →
  terminal with a Query issued (Hypothesis only if a property shape
  emerges; conftest profiles, no hardcoded max_examples)

### Integration Tests

- CDK static assertions: synthesized UserAdmin role allows
  `dynamodb:Query` on the audit table; scoped to that role only; existing
  statements intact (synthetic-data-s3-permissions precedent)
- USER ACTION (account-tier): UI-create a DataLabeler account → 200,
  terminal audit entry, account appears in LabelingTeams add-member; the
  stuck-'pending' remediation entry checked/remediated
