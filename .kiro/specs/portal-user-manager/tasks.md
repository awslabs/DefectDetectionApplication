# Implementation Plan: Portal User Manager

## Overview

Implementation proceeds in four parallel streams that share no files: portal backend (strict audit helpers, `user_admin.py`, `account_sync.py`), portal frontend (Layout dropdown, UserManager page, modals, sync panel), edge local auth (`local_auth` pure modules, `authorize_request`, endpoints), and edge sync/web UI (`user_accounts_sync` agent, `LoginGate`). Infrastructure (ComputeStack) lands after both Lambdas exist. Property-based tests use Hypothesis (backend/edge) and fast-check (frontend), each tagged `Feature: portal-user-manager, Property N` with a minimum of 100 iterations; each property test lives in its own test file so tests can be written in parallel.

## Tasks

- [x] 1. Portal backend foundations
  - [x] 1.1 Implement strict audit helpers in the shared layer
    - Add `record_audit_event_strict(...)` (two-phase: `put_item` a `pending` entry and raise on failure) and `finalize_audit_event(event_id, result, details)` to the portal shared layer alongside `log_audit_event`
    - Sanitize `details` with a denylist (`password`, `verifier`, `hash`, `temp*`) before writing
    - Support the new action types `password_change`, `forgot_password`, `role_change` with `resource_type='user_account'` and results `pending | success | failure | rejected`
    - _Requirements: 6.1, 6.3, 6.4, 6.5_

  - [x] 1.2 Create `user_admin.py` Lambda scaffold with PortalAdmin gate and pure credential functions
    - Create `edge-cv-portal/backend/functions/user_admin.py` reusing the shared layer, with a router that asserts `get_user_from_event(event)['role'] == 'PortalAdmin'` on every handler and returns 403 otherwise
    - Implement pure function `generate_temp_password(length=16)`: length ≥ 12, at least one lowercase, uppercase, digit, and symbol, assembled with `secrets.choice` and shuffled with `secrets.SystemRandom().shuffle`
    - Implement pure function `make_verifier(password)` → `{algorithm: 'pbkdf2-sha256', iterations: 210000, salt: b64, hash: b64}` using `hashlib.pbkdf2_hmac` with a 16-byte random salt (iteration count parameterizable for tests)
    - _Requirements: 1.5, 4.1, 7.3_

  - [ ]* 1.3 Write property test for the temporary password generator
    - **Property 7: Temporary password policy conformance**
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 7`
    - **Validates: Requirements 4.1**

  - [ ]* 1.4 Write property test for verifier one-wayness
    - **Property 14: Plaintext never appears in credential material**
    - Serialized verifier, sync documents, and cache files built from it never contain the plaintext; same password twice yields different salts and hashes
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 14`
    - **Validates: Requirements 7.3**

- [x] 2. Implement `user_admin.py` account-management handlers
  - [x] 2.1 Implement `GET /api/v1/admin/users` account listing
    - Paginate Cognito `list_users` fully, join with the `dda-portal-edge-credentials` table for the `edgeCapable` flag
    - Return username, email, `email_verified`, `custom:role` (default `Viewer`), `UserStatus`, `Enabled`
    - _Requirements: 1.5, 1.7, 2.1_

  - [x] 2.2 Implement `POST /api/v1/admin/users/{username}/password`
    - Flow: audit-pending → `admin_set_user_password(Permanent=permanent)` → verifier capture into `dda-portal-edge-credentials` → audit-final
    - Map `InvalidPasswordException` → 400 with the policy message passed through and no verifier write; other Cognito errors → 502 "password change failed"; `UserNotFoundException` → 404
    - _Requirements: 3.1, 3.3, 3.5, 6.1, 6.4_

  - [x] 2.3 Implement `POST /api/v1/admin/users/{username}/forgot-password`
    - Flow: verified-email check (400 before any generation if `email_verified != 'true'`) → `generate_temp_password` → audit-pending → SES `SendEmail` from the configured sender → `admin_set_user_password(Permanent=False)` → verifier capture → audit-final
    - SES send happens before the password set so a delivery failure leaves credentials untouched; response never contains the temporary password value
    - _Requirements: 4.1, 4.3, 4.4, 4.5, 6.1, 6.3_

  - [x] 2.4 Implement `PUT /api/v1/admin/users/{username}/role` with the last-PortalAdmin guard
    - Validate against the five defined Portal_Role values; guard: paginate the pool counting enabled `custom:role == 'PortalAdmin'` users, reject with 409 + reason if the change would leave zero, and audit the rejected attempt
    - Flow: audit-pending → `admin_update_user_attributes` on `custom:role` → audit-final recording previous and new role; Cognito failure → audit-final `failure`, role unchanged, error surfaced
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 2.5 Write property test for the PortalAdmin API gate
    - **Property 2: PortalAdmin gate on admin API endpoints**
    - Synthesized API Gateway events with non-PortalAdmin claims against every handler: 403 and zero Cognito/credential/sync mutations (recording fake clients)
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 2`
    - **Validates: Requirements 1.5**

  - [ ]* 2.6 Write property test for faithful admin operation pass-through
    - **Property 5: Admin operations pass through faithfully**
    - Recording fake `cognito-idp` client asserts exact password/permanence and exact role on `custom:role`
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 5`
    - **Validates: Requirements 3.1, 5.1**

  - [ ]* 2.7 Write property test for policy-violation handling
    - **Property 6: Policy-violation rejection preserves state**
    - Inject `InvalidPasswordException`: 400 with the violated rule in the body, no verifier stored or updated
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 6`
    - **Validates: Requirements 3.3**

  - [ ]* 2.8 Write property test for secret confinement
    - **Property 8: Secret material never leaves the backend**
    - For password and forgot-password flows: serialized HTTP responses and all recorded audit entries contain neither the plaintext nor the verifier hash
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 8`
    - **Validates: Requirements 4.3, 6.3**

  - [ ]* 2.9 Write property test for the last-PortalAdmin guard
    - **Property 9: Last-PortalAdmin guard**
    - Arbitrary account populations (roles × enabled flags) and role-change/disable actions: rejected iff zero enabled PortalAdmins would remain
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 9`
    - **Validates: Requirements 5.3**

  - [ ]* 2.10 Write property test for audit completeness
    - **Property 10: Audit completeness**
    - Every successful action → exactly one finalized entry (acting user, affected account, action type, completion timestamp); role changes carry previous/new role; rejections carry the reason; self-service records the affected account as actor
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 10`
    - **Validates: Requirements 5.4, 5.5, 6.1, 6.2**

  - [ ]* 2.11 Write property test for audit-failure blocking
    - **Property 11: Audit failure blocks the action**
    - Failing `put_item` on the pending entry → zero Cognito mutations and an error response stating the action was not applied
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 11`
    - **Validates: Requirements 6.4, 6.5**

  - [ ]* 2.12 Write unit tests for backend error paths
    - Generic Cognito failure → 502 with account untouched (3.5, 5.6); unverified email → 400 before generation (4.4); SES failure → credentials preserved (4.5); `UserNotFoundException` → 404
    - _Requirements: 3.5, 4.4, 4.5, 5.6_

- [x] 3. Implement Account_Sync_Service portal side
  - [x] 3.1 Implement sync staging, `build_sync_document`, and edge-sync endpoints in `user_admin.py`
    - `GET /api/v1/admin/edge-sync/devices`: devices table joined with `dda-portal-account-sync` for `lastSyncStatus`, `lastSyncAt`, `pendingChanges`
    - `POST /api/v1/admin/edge-sync/devices/{deviceId}`: stage the selected full account set + fresh `syncId`, set `pendingChanges=true`, invoke the sync Lambda
    - Pure `build_sync_document(accounts, syncId)`: complete account set `{username, email, role, enabled, deleted?, verifier?}` (never plaintext), disabled/deleted accounts marked `enabled: false` and never dropped; validate against the 8 KB shadow limit
    - Hook attribute changes (verifier capture, role change, enable/disable) to mark every configured device's staged set updated and pending
    - _Requirements: 7.1, 7.2, 7.3, 7.8_

  - [ ]* 3.2 Write property test for attribute-change propagation
    - **Property 13: Attribute changes propagate to staged syncs**
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 13`
    - **Validates: Requirements 7.2**

  - [ ]* 3.3 Write property test for sync document building
    - **Property 12: Sync round trip preserves account records** (portal build side)
    - For any selected account set (disabled/deleted, with/without verifiers), the built document contains exactly those records with all attributes preserved and no silent drops
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 12`
    - **Validates: Requirements 7.1, 7.8**

  - [x] 3.4 Implement `account_sync.py` sync-attempt entry
    - Create `edge-cv-portal/backend/functions/account_sync.py` (mirroring `camera_sync.py` style): build the desired document from the staged set, `update_thing_shadow` on the `dda-user-accounts` named shadow, stamp the row `in_progress` with `attemptAt`
    - Shadow-write failure or size-limit violation → `failed` with the reason, pending changes retained; runs on direct invoke and on the 5-minute schedule for every device with pending changes
    - _Requirements: 7.6, 7.7_

  - [x] 3.5 Implement ack ingest and timeout sweep in `account_sync.py`
    - SQS handler (partial-batch-failure pattern): reported `ackSyncId` matching the current `syncId` → `success`, `lastSyncAt = appliedAt`, clear `pendingChanges`; reported `error` → `failed` with the device's reason; stale acks discarded
    - Timeout sweep on the 5-minute schedule: `in_progress` rows with `attemptAt` older than 60 s and no ack → `failed` / `device unreachable`, pending retained
    - _Requirements: 7.4, 7.5, 7.6, 7.9_

  - [ ]* 3.6 Write property test for the sync-state reducer
    - **Property 15: Sync-state reducer**
    - Arbitrary event sequences (attempts, matching/error/stale acks, timeout sweeps at arbitrary times): only a matching ack marks success and clears pending (including zero-change syncs); timeout fires exactly when > 60 s elapsed; no failure discards the staged set
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 15`
    - **Validates: Requirements 7.4, 7.5, 7.6, 7.9**

- [x] 4. Checkpoint - portal backend
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Infrastructure (ComputeStack additions)
  - [x] 5.1 Add the new infrastructure to the ComputeStack
    - DynamoDB tables `dda-portal-edge-credentials` and `dda-portal-account-sync`
    - `user_admin.py` and `account_sync.py` Lambda functions; API Gateway routes under `/api/v1/admin/*` behind the existing `jwt_authorizer`
    - Ack SQS queue + DLQ; IoT topic rule on `$aws/things/+/shadow/name/dda-user-accounts/update/documents` → SQS; EventBridge `rate(5 minutes)` schedule → `account_sync.py`
    - SES send permission + verified-sender-address stack parameter; `cognito-idp:AdminSetUserPassword/AdminUpdateUserAttributes/ListUsers/AdminGetUser` grants scoped to the pool for `user_admin.py` only; table/queue/shadow IAM grants
    - _Requirements: 1.7, 4.1, 7.7_

  - [ ]* 5.2 Write CDK snapshot/integration assertions
    - jwt_authorizer attached to every new admin route (1.7); EventBridge `rate(5 minutes)` schedule and IoT topic rule → SQS wiring (7.7); Lambda IAM scoping
    - _Requirements: 1.7, 7.7_

- [x] 6. Portal frontend
  - [x] 6.1 Add the User Manager entry point and route
    - `Layout.tsx`: build the settings `ButtonDropdown` items conditionally — the `user-manager` item is present only when `user?.role === 'PortalAdmin'` (exported pure item-builder function); selection navigates to `/admin/user-manager`
    - `App.tsx`: register the route inside the authenticated layout (unauthenticated visitors hit the existing `/login` redirect)
    - _Requirements: 1.1, 1.2, 1.3, 1.6_

  - [x] 6.2 Implement the `UserManager` page with account table and filtering
    - New `UserManager.tsx`: access-denied `Alert` and nothing else for non-PortalAdmin users; Cloudscape `Table` + `TextFilter` over `GET /api/v1/admin/users` with columns username, email, Portal_Role, Cognito status, enabled/disabled, edge-login-capable
    - Exported pure `filterAccounts(accounts, term)`: case-insensitive substring on username or email; empty/whitespace term → full list; no match → empty list, table empty state
    - Clear accounts state before each fetch; load failure renders an error `Alert`, never a partial or stale list; exported pure table row-model builder
    - _Requirements: 1.4, 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 6.3 Implement the password, forgot-password, and role modals
    - Password modal: password + confirm, required permanent/temporary `RadioGroup` with no default and submit disabled until chosen, client-side policy pre-check, server policy errors shown verbatim, success flashbar naming the account
    - Forgot-password confirmation modal: success shows "temporary password sent to the account's registered email" without the value; surfaces no-verified-email and delivery errors
    - Role modal: `Select` limited to the five roles with the current role preselected; success confirmation + list re-fetch; rejection reasons (incl. last-PortalAdmin guard) shown in the modal
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 4.3, 4.4, 4.5, 5.2, 5.3, 5.7_

  - [x] 6.4 Implement the edge sync panel
    - Device list from `GET /api/v1/admin/edge-sync/devices` showing last sync status + timestamp per device; account multi-select sync action posting to `POST /api/v1/admin/edge-sync/devices/{deviceId}`
    - _Requirements: 7.1, 7.4_

  - [ ]* 6.5 Write property test for the UI role gate
    - **Property 1: PortalAdmin role gate in the portal UI**
    - For any Portal_Role: dropdown item present and page renders management content (vs. access denied) iff PortalAdmin — via the item-builder and role-parameterized renders
    - fast-check + Vitest, min 100 iterations, tag `Feature: portal-user-manager, Property 1`
    - **Validates: Requirements 1.1, 1.2, 1.4**

  - [ ]* 6.6 Write property test for listing completeness
    - **Property 3: Account listing completeness**
    - Row-model builder: exactly one row per account carrying username, email, role, status, enabled state
    - fast-check + Vitest, min 100 iterations, tag `Feature: portal-user-manager, Property 3`
    - **Validates: Requirements 2.1**

  - [ ]* 6.7 Write property test for account filtering
    - **Property 4: Account filtering**
    - fast-check + Vitest, min 100 iterations, tag `Feature: portal-user-manager, Property 4`
    - **Validates: Requirements 2.2, 2.3, 2.4**

  - [ ]* 6.8 Write unit tests for frontend example scenarios
    - Dropdown navigation (1.3), unauthenticated redirect (1.6), list-load failure UI (2.5), permanence selection required before submit (3.2), success confirmation (3.4), forgot-password confirmation without value (4.3), role modal options/preselection (5.2), confirmation + refresh (5.7)
    - _Requirements: 1.3, 1.6, 2.5, 3.2, 3.4, 4.3, 5.2, 5.7_

- [x] 7. Checkpoint - portal complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Edge local auth core modules (`src/backend/local_auth/`)
  - [x] 8.1 Implement `credential_cache.py`
    - Load/parse `/aws_dda/local_credential_cache.json`; `verify_credentials(username, password) -> Account | None` with constant-shape verification (PBKDF2 against stored or dummy verifier), returning the account only when the verifier matches and `enabled` is true
    - Missing/empty/corrupt cache treated as empty: every login fails uniformly with a logged "no synchronized accounts available" diagnostic; purely local, no network
    - _Requirements: 8.3, 8.4, 8.6, 11.3_

  - [x] 8.2 Implement `session_tokens.py`
    - `issue_token(secret, username, role, now)`: payload `{sub, role, iat, exp: iat + 43200, jti}`, `base64url(payload).base64url(HMAC-SHA256)`; injected clock
    - `validate_token(secret, token, now, cache)`: reject malformed structure, bad signature (`hmac.compare_digest`), `exp <= now`; then re-check the cache — account absent or disabled ⇒ reject
    - `get_or_create_secret()`: `/aws_dda/local_session_secret`, 32 random bytes, mode `0600`, regenerate if unreadable
    - _Requirements: 8.2, 8.5, 8.6, 8.7_

  - [x] 8.3 Implement `lockout.py`
    - In-memory `LockoutTracker` with injected clock: `record_failure`, `record_success` (resets), `is_locked`; 5 consecutive failures → 15-minute lock rejecting all attempts without extending the window
    - _Requirements: 8.10_

  - [x] 8.4 Implement `config.py`
    - `LocalLoginConfig` singleton reading `LocalLoginEnabled` via Greengrass IPC `GetConfiguration` at startup, re-polled on a 30 s background timer; enabled only for explicit `true`/`"true"`; missing key, read error, or any other value ⇒ disabled, transitions logged once
    - _Requirements: 11.1, 11.2, 11.4_

  - [ ]* 8.5 Write property test for the session token life cycle
    - **Property 16: Session token life cycle**
    - Expiry exactly 12 h after issuance; validates while unexpired + enabled; any corruption (byte edit, truncation, segment removal, foreign-secret signature) or time at/after expiry rejects
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 16`
    - **Validates: Requirements 8.2, 8.5, 8.7**

  - [ ]* 8.6 Write property test for disable-revokes-access
    - **Property 18: Disable revokes access**
    - After marking an account disabled: correct-credential logins rejected and every previously issued token fails validation
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 18`
    - **Validates: Requirements 8.6**

  - [ ]* 8.7 Write property test for login lockout
    - **Property 20: Login lockout**
    - Arbitrary attempt sequences/timings: locked exactly after 5 consecutive failures; all attempts rejected within the 15-minute window; success resets the count; correct credentials succeed after the window
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 20`
    - **Validates: Requirements 8.10**

  - [ ]* 8.8 Write property test for configuration parsing
    - **Property 23: Configuration parsing defaults to disabled**
    - Arbitrary values (missing key, strings, JSON values, IPC errors): enabled only for explicit true representations
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 23`
    - **Validates: Requirements 11.4**

  - [ ]* 8.9 Write unit tests for offline verification and config startup
    - Credential verification under a no-network guard (8.4); component-configuration read at startup with mocked Greengrass IPC (11.1)
    - _Requirements: 8.4, 11.1_

- [x] 9. Edge auth endpoints and authorization dependency
  - [x] 9.1 Implement `endpoints/local_auth.py`
    - Unauthenticated `GET /local-auth/status` → `{localLoginEnabled}`; `POST /local-auth/login`: when disabled → 403 "local login is disabled", never issues a token; when enabled → lockout check → `verify_credentials` → success: `record_success` + `issue_token` → `{token, expiresAt, role, username}`; failure: `record_failure` + uniform 401
    - Log failed logins and lockouts with username, never the submitted password
    - _Requirements: 8.2, 8.3, 8.10, 9.5, 11.3_

  - [ ]* 9.2 Write property test for failed-login indistinguishability
    - **Property 17: Failed login indistinguishability**
    - Any cache (incl. empty/all-disabled): 401 responses identical in status and body for known-username-wrong-password vs. unknown-username attempts; "no synchronized accounts available" diagnostic logged for empty caches
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 17`
    - **Validates: Requirements 8.3, 11.3**

  - [ ]* 9.3 Write property test for disabled-login token refusal
    - **Property 21: Disabled local login never issues tokens**
    - Any credentials and cache content while disabled: error indicates local login disabled, response contains no token
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 21`
    - **Validates: Requirements 9.5**

  - [x] 9.4 Implement the `authorize_request` dependency and rewire routers
    - Extend `utils/auth.py` with `authorize_request` implementing the decision matrix per request: open access only when both mechanisms off; valid existing bearer always authorizes when Existing_Token_Auth configured; valid Local_Session_Token authorizes only when local login enabled; otherwise 401 with `WWW-Authenticate: Bearer`
    - Two-segment base64url credentials validated locally first, falling back to the existing remote `validate_token`; never reads or writes `authorization_settings.json` beyond the presence check
    - Switch `get_api_router()` from the frozen `Depends(validate_token)` to `Depends(authorize_request)` unconditionally; exempt `/local-auth/login`, `/local-auth/status`, and static SPA assets; give the download-file query-param token path the same either/or acceptance
    - _Requirements: 8.1, 8.5, 8.9, 9.1, 9.3, 9.4, 10.1, 10.2, 10.3, 10.4, 10.5, 11.2_

  - [ ]* 9.5 Write property test for the authorization decision matrix
    - **Property 19: Authorization decision matrix**
    - Drive `authorize_request` through FastAPI's dependency system with stubbed remote introspection across all (local-login state × settings-file presence × credential kind) combinations; the (disabled, not-configured) row pins today's open behavior
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 19`
    - **Validates: Requirements 8.1, 8.9, 9.1, 9.3, 9.4, 10.1, 10.2, 10.4, 10.5**

  - [ ]* 9.6 Write property test for settings-file isolation
    - **Property 22: Local login configuration never touches Existing_Token_Auth**
    - Any sequence of local-login state changes: settings file bytes unchanged; bearer treatment depends only on file presence
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 22`
    - **Validates: Requirements 10.3**

  - [ ]* 9.7 Write unit test for runtime configuration changes
    - Flip `LocalLoginEnabled` between requests without restarting the app; the next request follows the new state
    - _Requirements: 11.2_

- [x] 10. Edge sync agent and component configuration
  - [x] 10.1 Implement `src/backend/user_accounts_sync/agent.py`
    - Pure `parse_sync_document` validating document shape; atomic cache replacement (temp file mode `0600`, `os.replace`)
    - Agent mirrors `camera_sync/agent.py`: startup `GetThingShadow` catch-up on `dda-user-accounts`, MQTT `SubscriptionHandler` on the shadow delta topic, apply → write `reported {ackSyncId, appliedAt, accountCount}`; validation failure → `reported {ackSyncId, error}` with the existing cache untouched; crashes logged, never fatal to LocalServer
    - _Requirements: 7.1, 7.4, 7.8, 7.9_

  - [ ]* 10.2 Write property test for edge sync application
    - **Property 12: Sync round trip preserves account records** (edge apply side)
    - For any valid sync document (disabled/deleted accounts, with/without verifiers), applying it yields a cache containing exactly those records with all attributes preserved; disabled/deleted marked disabled, never dropped
    - Hypothesis, min 100 iterations, tag `Feature: portal-user-manager, Property 12`
    - **Validates: Requirements 7.1, 7.8**

  - [x] 10.3 Wire the agent and configuration into the component
    - Start the sync agent on a daemon thread from `server_setup.py`; start the `LocalLoginConfig` poller
    - Add `LocalLoginEnabled: "false"` to the LocalServer recipe `DefaultConfiguration`
    - _Requirements: 11.1_

- [x] 11. Edge web UI login gate
  - [x] 11.1 Implement the `LoginGate` component in `src/react-webapp`
    - Fetch `/local-auth/status`; when enabled and no unexpired token in `sessionStorage`, render the login screen (posting to `/local-auth/login`) and block all other views; when disabled, render the app directly with no login screen or prompt
    - On success store the token and attach `Authorization: Bearer` to all API calls; API 401 clears the token and returns to the login screen
    - _Requirements: 8.1, 8.8, 9.2_

  - [ ]* 11.2 Write unit tests for the login gate
    - `LoginGate` in both states: enabled-without-token renders the login screen and blocks the app (8.8); disabled renders the app with no prompt (9.2)
    - _Requirements: 8.8, 9.2_

- [x] 12. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each property-based test is a single test tagged `Feature: portal-user-manager, Property {number}`, configured for a minimum of 100 iterations; PBKDF2 iteration counts are parameterized down in generators (one example test at the production count)
- Each property test lives in its own test file so parallel waves never write the same file
- Property 12 has a portal build-side test (3.3) and an edge apply-side test (10.2) since the two codebases cannot import each other
- Cognito `FORCE_CHANGE_PASSWORD` semantics (4.2, 4.6) and role-in-fresh-JWT (5.8) are documented Cognito behaviors verified manually during rollout, not coding tasks
- No new test tooling is required: Hypothesis exists in `edge-cv-portal/backend/tests` and `test/backend-test`; fast-check + Vitest exist in `edge-cv-portal/frontend`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "6.1", "8.1", "8.2", "8.3", "8.4", "10.1"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.1", "6.2", "8.5", "8.6", "8.7", "8.8", "8.9", "9.1", "9.4", "10.2", "10.3", "11.1"] },
    { "id": 2, "tasks": ["2.2", "2.5", "6.3", "6.5", "6.6", "6.7", "9.2", "9.3", "9.5", "9.6", "9.7", "11.2"] },
    { "id": 3, "tasks": ["2.3", "2.7", "6.4"] },
    { "id": 4, "tasks": ["2.4", "2.8", "6.8"] },
    { "id": 5, "tasks": ["3.1", "2.6", "2.9", "2.10", "2.11", "2.12"] },
    { "id": 6, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 7, "tasks": ["3.5"] },
    { "id": 8, "tasks": ["3.6", "5.1"] },
    { "id": 9, "tasks": ["5.2"] }
  ]
}
```
