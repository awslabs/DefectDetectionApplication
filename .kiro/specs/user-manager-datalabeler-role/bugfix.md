# Bugfix Requirements Document

## Introduction

Two related User-Manager defects block the dda-data-labeling feature
end-to-end and corrupt the user-admin API contract in the deployed account:

**Defect 1 — the User Manager UI never offers the DataLabeler role.** The
dda-data-labeling spec added a portal-native labeling system whose team
members MUST hold the DataLabeler role: the backend accepts it
(`edge-cv-portal/backend/functions/user_admin.py` `PORTAL_ROLES` is the
six-tuple ending in `'DataLabeler'`, commented "dda-data-labeling, Req 2.1:
assigned/revoked through the existing user administration functions"), and
`edge-cv-portal/frontend/src/pages/labeling/LabelingTeams.tsx` filters
add-member candidates to `account.role === 'DataLabeler'`. But the frontend
`edge-cv-portal/frontend/src/pages/admin/UserManagerModals.tsx` exports its
OWN `PORTAL_ROLES` with only the original FIVE roles (comment: "The five
defined Portal_Role values (Requirement 5.2)"), used by both the
create-user modal (~L270) and the change-role modal (~L515). The dropdowns
never offer DataLabeler, so no labeler account can be created or converted
through the UI, labeling teams can never gain members, and the whole DDA
labeling backend is unusable end-to-end. dda-data-labeling Requirement 2.1
("a Data_Labeler role that authorized administrators can assign to and
revoke from portal users through the existing user administration
functions") supersedes the portal-user-manager five-role count (its Req
5.2 / creation clause 8).

**Defect 2 — the UserAdmin Lambda cannot finalize audit events (live IAM
gap).** `finalize_audit_event` in the shared audit utils
(`edge-cv-portal/backend/layers/shared/python/shared_utils.py`) recovers
the audit table's range key with `table.query(KeyConditionExpression=
Key('event_id').eq(event_id))` — but the UserAdmin Lambda's CDK role
(`createLambdaRole('UserAdmin')` in
`edge-cv-portal/infrastructure/lib/compute-stack.ts`) only receives
`auditLogTable.grantWriteData(role)` (PutItem/UpdateItem/DeleteItem — no
Query). Every user-admin mutating action follows the audit-before-effect
protocol (audit-pending → Cognito effect → audit-final), so in the
deployed account EVERY such action 500s AFTER its effect is applied: the
caller gets "Internal server error" even though the mutation succeeded,
and the audit entry is stuck 'pending'.

Both fixes are minimal and portal-only: one string appended to the
frontend role array (plus ONE conscious pinned-test repoint), and one
missing IAM grant in the CDK compute stack. No device-side `src/` file, no
component build; rollout is a portal deploy.

### Incident Record (verified evidence, 2026-08-17, live portal https://d23v4ltibogb5x.cloudfront.net, account 164152369890)

- **Defect 1**: user screenshot confirms the role dropdown offers only the
  five original roles. The live pool `us-east-1_2r9jpbWIe` had ZERO
  DataLabeler users until a manual remediation created `ryan-labeler`
  (DataLabeler, ryvan+labeler@amazon.com) via direct Lambda invoke on
  2026-08-17 — the only way past the UI gap.
- **Defect 2**: during that remediation create (POST /api/v1/admin/users
  via Lambda `EdgeCVPortalComputeStack-UserAdminHandlerEC759CBB-k6owDXSRir3G`,
  2026-08-17 17:03Z, request id 79dc2dd8-2e10-486a-a0e6-aec5ef805d37):
  audit-pending recorded, `admin_create_user` SUCCEEDED (user exists,
  invite sent), then the handler crashed:
  `AccessDeniedException ... UserAdminRole2557D264 ... is not authorized
  to perform: dynamodb:Query on resource:
  arn:aws:dynamodb:us-east-1:164152369890:table/dda-portal-audit-log` →
  HTTP 500 "Internal server error" returned to the caller even though the
  account was created; the audit event is presumably stuck 'pending'.
- Code evidence: `UserAdminRole2557D264` = `createLambdaRole('UserAdmin')`
  (compute-stack.ts ~L1672); the base role gets
  `props.auditLogTable.grantWriteData(role)` (~L301) and the
  userAdminHandler-specific grants add cognito-idp, SES, edge-credentials,
  and account-sync — nothing adds audit-table read/Query. Among
  `createLambdaRole` consumers only `user_admin.py` calls
  `finalize_audit_event`; `quick_setup.py` (the other caller) has its own
  dedicated role with `auditLogTable.grantReadWriteData(quickSetupRole)`,
  which is why the same shared code path works there.

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type UserAdminInteraction
  OUTPUT: boolean

  // Defect 1: a User Manager role selection (create-user or change-role
  // modal) whose offered vocabulary is missing DataLabeler — the frontend
  // PORTAL_ROLES has diverged from the backend's six-role vocabulary.
  IF X.kind = 'role-selection' THEN
    RETURN 'DataLabeler' NOT IN offeredRoles(X.modal)
  END IF

  // Defect 2: an audit finalize step executed by the UserAdmin Lambda in
  // the deployed account — the role's granted actions on the audit-log
  // table are missing dynamodb:Query, which finalize_audit_event requires.
  IF X.kind = 'audit-finalize' THEN
    RETURN 'dynamodb:Query' NOT IN
           grantedActions(UserAdminRole, 'dda-portal-audit-log')
  END IF

  RETURN false
END FUNCTION
```

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a PortalAdmin opens the create-user modal THEN the system offers
only the five original roles (PortalAdmin, UseCaseAdmin, DataScientist,
Operator, Viewer) in the role dropdown — DataLabeler is absent, so no
labeler account can be created through the UI

1.2 WHEN a PortalAdmin opens the change-role modal for an existing account
THEN the system offers only the same five roles — no existing account can
be converted to DataLabeler through the UI

1.3 WHEN a labeling-team administrator opens the add-member flow in
LabelingTeams THEN the system filters candidates to
`account.role === 'DataLabeler'` and finds none (no UI path can produce
one), so labeling teams can never gain members and the DDA labeling
backend is unusable end-to-end

1.4 WHEN any user-admin mutating action (account create, password
change/reset, role change, enable/disable, delete) reaches its audit
finalize step in the deployed account THEN the system crashes with
`AccessDeniedException` on `dynamodb:Query` against `dda-portal-audit-log`
AFTER the Cognito effect was applied — the caller receives HTTP 500 even
though the mutation succeeded, and the audit entry is stuck 'pending'

### Expected Behavior (Correct)

2.1 WHEN a PortalAdmin opens the create-user modal THEN the system SHALL
offer all six defined Portal_Role values (PortalAdmin, UseCaseAdmin,
DataScientist, Operator, Viewer, DataLabeler) in the role dropdown,
matching the backend's `PORTAL_ROLES` vocabulary

2.2 WHEN a PortalAdmin opens the change-role modal THEN the system SHALL
offer the same six roles with the account's current role preselected, so
existing accounts can be converted to DataLabeler

2.3 WHEN a DataLabeler account is created or converted through the UI THEN
the system SHALL accept the submission end-to-end (the backend already
validates against the six-role vocabulary) and the account SHALL appear as
an add-member candidate in LabelingTeams

2.4 WHEN the UserAdmin Lambda finalizes an audit event in the deployed
account THEN the system SHALL permit the `dynamodb:Query` that
`finalize_audit_event` issues against the audit-log table, so every
user-admin mutating action returns its success response and its audit
entry reaches a terminal result ('success' | 'failure' | 'rejected')
instead of sticking at 'pending'

### Unchanged Behavior (Regression Prevention)

3.1 WHEN either modal renders its role dropdown THEN the system SHALL
CONTINUE TO offer the five original roles, in the existing order, with the
change-role modal preselecting the account's current role

3.2 WHEN the create-user modal validates and submits THEN the system SHALL
CONTINUE TO enforce the existing field validation (username, email shape,
role required), the password-policy pre-check, and the
temporary-password/invite flow unchanged

3.3 WHEN the change-role modal submits THEN the system SHALL CONTINUE TO
call `setAdminUserRole` with the selected role and surface rejection
reasons (including the last-PortalAdmin guard) in the modal unchanged

3.4 WHEN the backend validates a submitted role THEN the system SHALL
CONTINUE TO validate against its existing six-role `PORTAL_ROLES` tuple —
no backend code change; all existing user-admin endpoint behavior
(validation, guards, response shapes) unchanged

3.5 WHEN any caller finalizes an audit event THEN the system SHALL
CONTINUE TO follow the audit-before-effect protocol unchanged:
`record_audit_event_strict` before the effect, `finalize_audit_event`
recovering the range key via the same Query call site, still RAISING on
lookup/update failure — the fix never weakens auditing or swallows
finalize errors

3.6 WHEN the CDK stacks synthesize THEN the system SHALL CONTINUE TO grant
the UserAdmin role its existing permissions unchanged (audit-log write,
cognito-idp admin actions scoped to the portal pool, SES SendEmail,
edge-credentials and account-sync table access, accountSyncHandler
invoke), and SHALL CONTINUE TO grant every other Lambda role its existing
audit-log permissions unchanged (base roles write-only; quick-setup
read/write; audit-logs read)
