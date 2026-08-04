# Implementation Plan: Station Quick Setup

## Overview

Implementation builds from the inside out so every prompt lands on already-integrated ground. The DynamoDB table comes first because every backend handler reads and writes it. Next come the pure, side-effect-free cores (`TokenService`, `RateLimiter.evaluate`, `build_session_policy`) that carry most of the security-critical logic and are the easiest to property-test in isolation. The two Lambdas follow — `device_registrations.py` (JWT-authenticated) then `quick_setup.py` (token-authenticated) — each wired to the table and cores as it is written. The station-side artifacts (`setup_station.sh` change, `bootstrap.sh`, `run.sh`) come next, then the CDK packaging and API wiring that ties the Lambdas, bundle asset, and routes together, and finally the frontend that consumes the JWT routes.

Python backend code uses `pytest` + `hypothesis` with `moto` for AWS mocking; CDK and frontend use TypeScript; station-side code is `bash` tested with `bats`-style shell tests. Every property test is configured for a **minimum of 100 examples** and tagged `**Feature: station-quick-setup, Property {N}: {title}**`. Sub-tasks marked with `*` are optional test tasks and are not implemented during core delivery.

## Tasks

- [x] 1. Provision the Device_Registration storage
  - [x] 1.1 Add the device-registrations table and GSI to `storage-stack.ts`
    - Add `dda-portal-device-registrations` DynamoDB table (PK `registration_id`, PAY_PER_REQUEST, `ttl` time-to-live attribute set only on `RATELIMIT#` items) and the `usecase-device-index` GSI (PK `usecase_id`, SK `device_name`) to `edge-cv-portal/infrastructure/lib/storage-stack.ts`
    - Expose the table as a stack property so `ComputeStack` can grant access and inject the table name
    - _Requirements: 1.1, 3.9, 8.5_

- [x] 2. Implement the security-critical pure cores
  - [x] 2.1 Implement `TokenService`
    - Create the token module used by both Lambdas: `generate_token(registration_id)` deriving a 256-bit CSPRNG secret via `secrets.token_urlsafe(32)`, wire format `dqs1.{registration_id}.{secret}`, returning `(token, sha256hex(secret), expires_at)` with a ≤90-minute TTL; `validate_token(token, now)` parsing the token, loading the registration by embedded id, and returning `VALID`/`EXPIRED`/`INVALID`/`CHECK_FAILED` using `hmac.compare_digest` on the stored hash only
    - Ensure unknown-registration, wrong-secret, consumed, superseded, and deleted cases all collapse to the single `INVALID` result; storage errors return `CHECK_FAILED`
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.6, 3.7_

  - [x] 2.2 Write property test for token expiration bound
    - **Feature: station-quick-setup, Property 6: Token expiration is bounded**
    - **Validates: Requirements 3.1**

  - [x] 2.3 Implement `RateLimiter`
    - Write the pure decision function `evaluate(state, now, invalid_attempt) -> (next_state, allowed)` implementing WINDOW=300, MAX_INVALID=10, BLOCK=300 (block iff >10 invalid tokens within the current 5-minute window, block lasts ≥5 minutes), plus the DynamoDB persistence layer storing `RATELIMIT#{source_ip}` items with a TTL attribute for auto-cleanup
    - _Requirements: 3.9_

  - [x] 2.4 Write property test for the invalid-token rate limiter
    - **Feature: station-quick-setup, Property 11: The invalid-token rate limiter matches its specification**
    - **Validates: Requirements 3.9**

  - [x] 2.5 Implement `build_session_policy`
    - Write `build_session_policy(device_name, device_group, region, account_id)` returning the least-privilege IAM session policy scoped to `thing/{device_name}` and `thinggroup/{device_group}`, with only the fixed provisioning action set (iot thing/group/cert/policy/endpoint/role-alias actions, iam Greengrass TES role setup, greengrass TagResource on the core device, sts GetCallerIdentity) and no wildcard actions
    - _Requirements: 5.2_

  - [x] 2.6 Write property test for session-policy scoping
    - **Feature: station-quick-setup, Property 13: Session policies are scoped to the single registered device**
    - **Validates: Requirements 5.2**

- [x] 3. Implement the `device_registrations.py` Lambda (JWT-authenticated)
  - [x] 3.1 Implement registration creation and command building
    - Create `edge-cv-portal/backend/functions/device_registrations.py` following `devices.py` conventions (shared_utils imports, CORS preflight, `create_response`); define `IOT_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9:_-]{1,128}$')`
    - Implement `create_registration`: collect all missing fields, then all pattern-invalid fields; enforce `Permission.MANAGE_DEVICES` via `record_audit_event_strict` (failing the whole op if the audit write raises); verify uniqueness via cross-account `iot.describe_thing` and a GSI query, rejecting on conflict or on any lookup failure; generate the token and put the item with `ConditionExpression=attribute_not_exists(registration_id)` so no token-less registration persists; build and return the one-line HTTPS `Setup_Command` (≤2048 chars) with token expiry
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 1.10, 2.1, 2.2, 2.3, 2.7_

  - [x] 3.2 Write property test for complete registration creation
    - **Feature: station-quick-setup, Property 1: Valid registrations are created completely**
    - **Validates: Requirements 1.1, 1.6, 1.8**

  - [x] 3.3 Write property test for field validation
    - **Feature: station-quick-setup, Property 2: Invalid or missing fields are rejected, identified per field, with no persistence**
    - **Validates: Requirements 1.2, 1.9**

  - [x] 3.4 Write property test for device-name conflicts
    - **Feature: station-quick-setup, Property 3: Conflicting device names are rejected**
    - **Validates: Requirements 1.3**

  - [x] 3.5 Write property test for Setup_Command well-formedness
    - **Feature: station-quick-setup, Property 4: The Setup_Command is well formed**
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [x] 3.6 Write unit tests for creation failure paths
    - RBAC denial records an audit event and returns access-denied (1.4); audit-write failure aborts the whole operation (1.5); uniqueness unverifiable returns a verification-failed error with nothing persisted (1.10); token generation/storage failure persists no registration (2.7)
    - _Requirements: 1.4, 1.5, 1.10, 2.7_

  - [x] 3.7 Implement registration listing and thing-group listing
    - Implement `list_registrations` (returns status and `token_expires_at`, never token material) and `list_thing_groups` (cross-account `iot.list_thing_groups` pass-through)
    - _Requirements: 1.7, 6.3_

  - [x] 3.8 Write unit test for thing-groups listing pass-through
    - Verify existing IoT Thing Groups are returned for the selected Use_Case
    - _Requirements: 1.7_

  - [x] 3.9 Implement command regeneration and deletion
    - Implement `regenerate_command`: reject when status is `completed` (2.8); otherwise atomically replace `token_hash`/`token_expires_at` in a single `UpdateItem` (guaranteeing at most one valid token) and reset `expired`/`failed` back to `pending`
    - Implement `delete_registration`: reject when status is `completed` (6.9); otherwise delete the item, which invalidates the token through the item lookup (6.6)
    - _Requirements: 2.5, 2.8, 6.6, 6.9_

  - [x] 3.10 Write property test for single-valid-token invariant
    - **Feature: station-quick-setup, Property 5: At most one Setup_Token is valid per registration**
    - **Validates: Requirements 2.5, 3.4**

  - [x] 3.11 Write property test for status-gated deletion
    - **Feature: station-quick-setup, Property 17: Deletion is gated on status and invalidates the token**
    - **Validates: Requirements 6.6, 6.9**

  - [x] 3.12 Write unit test for regenerate/delete on completed registrations
    - Both operations reject a `completed` registration and leave it unchanged
    - _Requirements: 2.8, 6.9_

- [x] 4. Checkpoint - registration backend complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement the `quick_setup.py` Lambda (token-authenticated)
  - [x] 5.1 Implement the request pipeline and bootstrap route
    - Create `edge-cv-portal/backend/functions/quick_setup.py` with the shared pipeline for every non-bootstrap request: rate-limit check by `sourceIp` (429 if blocked), strict pending-audit entry (reject on audit-write failure), `TokenService.validate_token` with uniform `invalid_token` / distinct `token_expired` errors and reject-on-`CHECK_FAILED`, then finalize audit; implement `get_bootstrap` serving the bootstrap bytes from `QUICK_SETUP_BOOTSTRAP_KEY` as `text/x-shellscript`
    - Transition `pending` → `expired` when an expired token is presented
    - _Requirements: 3.3, 3.8, 3.10, 8.1, 8.4_

  - [x] 5.2 Write property test for expired-token handling
    - **Feature: station-quick-setup, Property 7: Expired tokens are rejected and expire pending registrations**
    - **Validates: Requirements 3.3**

  - [x] 5.3 Write property test for indistinguishable invalid-token responses
    - **Feature: station-quick-setup, Property 8: Invalid-token responses are indistinguishable**
    - **Validates: Requirements 3.5**

  - [x] 5.4 Implement the bundle manifest route
    - Implement `get_bundle_manifest`: validate (not consume) the token, verify the bundle object exists in the artifacts bucket (503 if missing), and return a 15-minute presigned URL, `QUICK_SETUP_BUNDLE_SHA256`, and the per-registration parameters (registration id, device name, Device_Group, Use_Case region, this deployment's Quick_Setup_Endpoint URL)
    - _Requirements: 3.7, 4.1, 4.5, 4.10_

  - [x] 5.5 Write property test for token-to-registration binding and checksum
    - **Feature: station-quick-setup, Property 10: A valid token binds to exactly its registration, with a correct checksum**
    - **Validates: Requirements 3.7, 4.1, 4.5**

  - [x] 5.6 Write unit test for missing bundle artifacts
    - A valid token with no bundle object in S3 returns a 503 and serves no partial content
    - _Requirements: 4.10_

  - [x] 5.7 Implement the credential-exchange route
    - Implement `exchange_credentials`: refuse when remaining lifetime is below the 900s STS floor (treated as expired); `sts.assume_role` with the use-case role, external id, `build_session_policy`, and `DurationSeconds=min(3600, remaining)`; on STS failure return an issuance error leaving the token unconsumed; mint the `report_secret`; then consume atomically via a conditional `UpdateItem` (token hash matches, not consumed, not expired, status `pending`) setting status `in_progress` and storing the report-secret hash; on conditional failure discard credentials and return the uniform invalid-token error
    - _Requirements: 5.1, 5.3, 5.4, 5.5, 5.7_

  - [x] 5.8 Write property test for bounded credential lifetime
    - **Feature: station-quick-setup, Property 14: Credential validity never exceeds remaining token lifetime**
    - **Validates: Requirements 5.3**

  - [x] 5.9 Write property test for exactly-once credential exchange
    - **Feature: station-quick-setup, Property 15: Credential exchange is exactly-once and transitions to in_progress**
    - **Validates: Requirements 5.4, 5.5**

  - [x] 5.10 Write unit test for STS-failure token preservation
    - Credential issuance failure after validation leaves the token unconsumed so the exchange can be retried
    - _Requirements: 5.7_

  - [x] 5.11 Implement the status-report route
    - Implement `report_status`: authenticate via constant-time compare of `sha256(report_secret)` against the stored hash; accept from `in_progress` or `failed` only; set `completed` or `failed` (storing the error summary truncated to 1024 chars); reject unknown id, bad secret, or already-`completed` targets with the uniform error, leaving the registration unchanged
    - _Requirements: 6.1, 6.2, 6.7_

  - [x] 5.12 Write property test for authenticated, truncated status reports
    - **Feature: station-quick-setup, Property 16: Status reports are authenticated and applied with truncation**
    - **Validates: Requirements 6.1, 6.2, 6.7**

  - [x] 5.13 Write property test for complete auditing
    - **Feature: station-quick-setup, Property 18: Every quick-setup operation is audited completely**
    - **Validates: Requirements 3.8, 8.1, 8.2**

  - [x] 5.14 Write property test for secret non-disclosure
    - **Feature: station-quick-setup, Property 9: Secrets never appear at rest or in output**
    - **Validates: Requirements 3.6, 8.3**

  - [x] 5.15 Write unit tests for reject-on-unverifiable and audit-before-effect
    - Any unevaluable security check rejects rather than proceeds (3.10); an audit-write failure before redemption rejects the request and serves nothing (8.4)
    - _Requirements: 3.10, 8.4_

- [x] 6. Checkpoint - quick-setup backend complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement the station-side artifacts
  - [x] 7.1 Make `setup_station.sh` thing-group env-overridable
    - In `station_install/setup_station.sh`, set `thing_group_name="${DDA_THING_GROUP:-DDA_transition_EC2_Group}"` and pass `--thing-group-name ${thing_group_name}` to the Greengrass installer invocation, keeping the script fully functional standalone
    - _Requirements: 4.2, 4.6_

  - [x] 7.2 Implement `quick_setup/bootstrap.sh`
    - Create `station_install/quick_setup/bootstrap.sh` (`--endpoint`, `--token`): run prerequisite checks (curl/wget, sha256sum, root, supported Ubuntu, supported arch, ≥2GB free on `/`) printing every unmet item and exiting non-zero with no system changes; POST `/bundle`, printing the regenerate instruction and exiting non-zero on invalid/expired token; download the bundle to a temp dir, `sha256sum -c` against the manifest, and on mismatch print an integrity error, delete the download, and exit non-zero without extracting or executing anything; on success extract and `exec run.sh` with manifest parameters as `DDA_*` env vars
    - _Requirements: 4.8, 4.9, 7.1, 7.2, 7.5_

  - [x] 7.3 Write property test for the checksum gate
    - **Feature: station-quick-setup, Property 12: The checksum gate executes if and only if content verifies**
    - **Validates: Requirements 4.8, 4.9**

  - [x] 7.4 Write shell tests for bootstrap prerequisites and token rejection
    - `bats`-style tests with stubbed `curl`: unmet-prerequisite fixtures make no system changes and exit non-zero (7.1, 7.2); token rejection prints the regenerate instruction and exits non-zero (7.5)
    - _Requirements: 7.1, 7.2, 7.5_

  - [x] 7.5 Implement `quick_setup/run.sh`
    - Create `station_install/quick_setup/run.sh`: create and announce the install log, print step banners, fail fast per step; run credential-free install steps first; exchange credentials (POST `/credentials`, export `AWS_*` in memory only, hold `report_secret`, redact secrets from stdout/log, write no credential file); invoke `setup_station.sh` provisioning with `DDA_THING_GROUP` and env credentials so the core device is tagged `dda-portal:managed=true` and appears in the portal device listing; verify Device_Group membership and Greengrass service; `trap EXIT` to unset `AWS_*` and remove temp files; report status with up to 3 attempts then print an undeliverable-status message without changing the exit code; on success print device name, group, region, and log path and exit 0
    - _Requirements: 4.6, 4.7, 5.6, 5.8, 6.5, 6.8, 7.3, 7.4, 7.6, 7.7, 7.8_

  - [x] 7.6 Write shell tests for run.sh orchestration
    - `bats`-style tests with stubbed `aws`/HTTP: log creation and step banners (7.3, 7.4), fail-fast ordering and success output (7.6, 7.7), report retry count (6.8), credential cleanup and log redaction with planted fake secrets (5.6, 5.8), and thing-group-join failure treated as step failure (4.7)
    - _Requirements: 4.7, 5.6, 5.8, 6.8, 7.3, 7.4, 7.6, 7.7_

- [x] 8. Package artifacts and wire the API infrastructure
  - [x] 8.1 Implement bundle packaging in the CDK asset pipeline
    - Add `build-quick-setup-bundle.sh` and a `s3deploy.BucketDeployment` in `edge-cv-portal/infrastructure/lib/compute-stack.ts` that tars `station_install/` (including `quick_setup/`) into `setup-bundle.tar.gz`, computes SHA-256 sidecars over the exact bytes, copies `bootstrap.sh` plus its checksum, and uploads under `quick-setup/current/` so a failed deploy leaves prior artifacts in place
    - _Requirements: 4.3, 4.4, 4.5_

  - [x] 8.2 Wire the two Lambdas and IAM in `ComputeStack`
    - Add the `device_registrations` and `quick_setup` Lambdas in `compute-stack.ts` with env vars (`REGISTRATIONS_TABLE`, `QUICK_SETUP_BUNDLE_KEY`, `QUICK_SETUP_BUNDLE_SHA256`, `QUICK_SETUP_BOOTSTRAP_KEY`, `QUICK_SETUP_BOOTSTRAP_SHA256`) baked from the bundle asset outputs; grant `quick_setup` `sts:AssumeRole` on use-case roles, read on `quick-setup/*`, and read/write on the registrations + audit tables, and give `device_registrations` the standard portal Lambda role
    - _Requirements: 4.4, 5.1, 8.1_

  - [x] 8.3 Create the `QuickSetupApiStack` nested stack
    - Add `edge-cv-portal/infrastructure/lib/quick-setup-api-stack.ts` (following `NodeDesignerApiStack`) registering the JWT routes (`POST/GET /device-registrations`, `GET /device-registrations/thing-groups`, `POST /device-registrations/{id}/command`, `DELETE /device-registrations/{id}`) and the `/quick-setup/*` routes with `authorizationType: AuthorizationType.NONE` plus method-level throttling (≈10 rps / burst 20); attach it to the existing RestApi
    - _Requirements: 3.9_

  - [x] 8.4 Write CDK synth assertions
    - Assert the registrations table + `usecase-device-index` GSI exist, the bundle asset and checksum env vars are wired into both Lambdas, and every `/quick-setup/*` method carries `AuthorizationType.NONE` with throttling
    - _Requirements: 3.9, 4.4, 4.5_

  - [x] 8.5 Write bundle-content smoke test
    - Unpack the built artifact and assert every `station_install` supporting file (and `quick_setup/`) is present so the station needs no repository access
    - _Requirements: 2.6, 4.3_

- [x] 9. Implement the portal frontend
  - [x] 9.1 Add device-registration API methods
    - Add `registerDevice`, `listDeviceRegistrations`, `listThingGroups`, `regenerateSetupCommand`, and `deleteDeviceRegistration` to `edge-cv-portal/frontend/src/services/api.ts` using the existing `this.request` pattern
    - _Requirements: 1.1, 6.3_

  - [x] 9.2 Implement `RegisterDeviceDialog`
    - Build the dialog with a device-name field showing per-field pattern validation feedback and a Device_Group autocomplete populated from `listThingGroups` that also accepts free-text new group names, with the Use_Case taken from the current context
    - _Requirements: 1.2, 1.7, 1.8_

  - [x] 9.3 Implement `SetupCommandDialog`
    - Display the Setup_Command in monospace with a single-action copy-to-clipboard control that places the complete command on the clipboard, and show the token expiration date and time
    - _Requirements: 2.4_

  - [x] 9.4 Add the registrations panel to the devices view
    - In `edge-cv-portal/frontend/src/pages/Devices.tsx`, add an "Add Device" entry point and a registrations panel showing each registration's status chip, the expiry time while `pending`/`in_progress`, a Regenerate action for non-completed registrations, and a Delete action for non-completed registrations, polling on load/refresh
    - _Requirements: 6.3, 6.4, 6.6, 6.9_

  - [x] 9.5 Write frontend component tests
    - Test `RegisterDeviceDialog` per-field validation, `SetupCommandDialog` single-action full-command copy and expiry display, and the registrations panel status chips / expiry / regenerate-delete affordances
    - _Requirements: 2.4, 6.3, 6.4_

- [x] 10. Final checkpoint - full quick-setup flow wired
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Harden `setup_station.sh` against first-boot transients and false-alarm reporting
  - Motivated by a real provisioning failure on a fresh JP6 Orin Nano (`ryan-orin-nano`): a first-boot apt/dpkg lock held by unattended-upgrades made the single 12-package GStreamer `apt-get install` fail, marking the entire setup failed even though Greengrass provisioning succeeded and the device came up HEALTHY (an immediate second run succeeded); the same run emitted false "Could not read GreengrassV2IoTThingPolicy" and "DDAPortalComponentAccessPolicy not found" warnings although both policies exist with correct permissions, because the checks swallowed AWS CLI/credential errors and reported them as missing policies

  - [x] 11.1 Add apt/dpkg lock resilience and split the GStreamer install
    - In `station_install/setup_station.sh`, add an `apt_wait_for_lock` helper that waits up to ~5 minutes for the apt/dpkg locks (`/var/lib/dpkg/lock-frontend`, `/var/lib/dpkg/lock`, `/var/lib/apt/lists/lock`), printing periodic progress messages while unattended-upgrades holds them, and an apt wrapper that waits for the lock, runs the apt operation, and on failure re-waits and retries once before returning failure; route every `apt-get update` / `apt-get install` invocation in the script through the wrapper
    - Split the GStreamer step (currently one 12-package `apt-get install` around line 625): install the required core set (`libgstreamer1.0-dev`, `libgstreamer-plugins-base1.0-dev`, `gstreamer1.0-plugins-base`, `gstreamer1.0-plugins-good`, `gstreamer1.0-libav`, `gstreamer1.0-tools`, `gstreamer1.0-x`, `gstreamer1.0-alsa`, `gstreamer1.0-gl`) as a single operation that stays `add_error` on failure, then install the optional desktop extras (`gstreamer1.0-gtk3`, `gstreamer1.0-qt5`, `gstreamer1.0-pulseaudio`) individually with failures downgraded to `add_warning`
    - Rationale stated inline: requirements.md has no criterion covering transient apt-lock resilience; this addresses the observed first-boot failure where one transient lock marked an otherwise-healthy setup as failed

  - [ ]* 11.2 Write shell tests for apt resilience and the GStreamer split
    - `bats`-style tests alongside `station_install/quick_setup/tests/` with stubbed `apt-get`/lock probes: the wrapper waits while the lock is held and proceeds once released; a first-attempt apt failure followed by a successful retry records no error; a core-GStreamer-set failure records an error while an extras-only failure records warnings and continues
    - Rationale stated inline: validates the 11.1 hardening behavior (no matching requirement in requirements.md)

  - [ ] 11.3 Make the IoT/IAM policy checks truthful about check failures
    - In `station_install/setup_station.sh`, gate the `GreengrassV2IoTThingPolicy` shadow-permission check and the `DDAPortalComponentAccessPolicy` attach step on the AWS CLI being installed and credentials resolving (e.g. `aws sts get-caller-identity` succeeds); both already execute after the AWS CLI install step, but they silently discard CLI/auth errors (`2>/dev/null || echo ""`) and then report the policy as missing
    - When the check command itself fails (CLI missing, auth failure, access denied), emit a warning naming the check as inconclusive (e.g. "Could not verify GreengrassV2IoTThingPolicy — the check itself failed (AWS CLI unavailable or credentials not resolvable); verify manually") instead of the current "Could not read GreengrassV2IoTThingPolicy" / "DDAPortalComponentAccessPolicy not found. Deploy UseCaseAccountStack first."; keep the existing policy-absent wording only when the API positively confirms absence (`NoSuchEntity` / `ResourceNotFoundException`)
    - Rationale stated inline: requirements.md has no criterion covering these warnings; on the observed device both policies existed with correct permissions and the false alarms pointed operators at the wrong fix

  - [ ] 11.4 Report the specific failed step instead of a blanket failure when provisioning succeeded
    - In `station_install/setup_station.sh`, track whether Greengrass provisioning and core-device registration succeeded separately from later package/step errors; when provisioning succeeded but a subsequent step recorded an error, make the final summary emit a line naming the specific failed step(s) (e.g. "Setup completed with ERRORS in step: GStreamer install (Greengrass provisioning and core-device registration succeeded)") so the existing `quick_setup/run.sh` summary-line pickup forwards the specific failed step to the portal `error_summary` instead of a blanket failure; keep the plain failed outcome when Greengrass provisioning itself fails
    - _Requirements: 6.2, 7.7_

  - [ ]* 11.5 Write shell tests for truthful policy checks and granular failure reporting
    - `bats`-style tests with a stubbed `aws`: CLI-missing and auth-failure fixtures emit the inconclusive-check warning and never the policy-missing wording; a genuinely-absent-policy fixture keeps the existing warning; a run with provisioning success plus a package-step failure produces a summary naming the failed step, while a provisioning failure keeps the blanket failed outcome
    - _Requirements: 6.2, 7.7_ (policy-check wording has no matching requirement; rationale in 11.3)

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references the specific requirement sub-clauses it covers for traceability.
- Property tests validate the universal correctness properties from the design (Properties 1–18), run with a minimum of 100 examples, and are placed close to the implementation they exercise so regressions surface early.
- Unit and shell tests cover the analyzed non-property criteria (RBAC/audit failure paths, uniqueness/STS/audit failure handling, prerequisite and orchestration behavior, and truncation boundaries).
- Task 11 is post-delivery hardening of `setup_station.sh` driven by an observed first-boot provisioning failure on a JP6 Orin Nano; where no requirement clause matches, the rationale is stated inline on the task instead of a requirement reference. Its sub-tasks all edit `setup_station.sh`, so they occupy consecutive dependency-graph waves (no dependency on tasks 1-10) rather than one parallel wave.
- On-hardware end-to-end verification (end-state equivalence with a manual `setup_station.sh` run, idempotent re-run, credential sufficiency) is delivered by extending the existing `test/on-hardware` harness and is intentionally out of scope for this coding plan since it requires a provisioned station.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "2.3", "2.5", "7.1", "7.2"] },
    { "id": 1, "tasks": ["2.2", "2.4", "2.6", "3.1", "5.1", "7.5", "9.1", "9.2", "9.3"] },
    { "id": 2, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.7", "5.2", "5.3", "5.4", "7.3", "7.4", "9.4"] },
    { "id": 3, "tasks": ["3.6", "3.8", "3.9", "5.5", "5.6", "5.7", "7.6"] },
    { "id": 4, "tasks": ["3.10", "3.11", "3.12", "5.8", "5.9", "5.10", "5.11"] },
    { "id": 5, "tasks": ["5.12", "5.13", "5.14", "5.15"] },
    { "id": 6, "tasks": ["8.1"] },
    { "id": 7, "tasks": ["8.2", "8.3"] },
    { "id": 8, "tasks": ["8.4", "8.5", "9.5"] },
    { "id": 9, "tasks": ["11.1"] },
    { "id": 10, "tasks": ["11.2", "11.3"] },
    { "id": 11, "tasks": ["11.4"] },
    { "id": 12, "tasks": ["11.5"] }
  ]
}
```
