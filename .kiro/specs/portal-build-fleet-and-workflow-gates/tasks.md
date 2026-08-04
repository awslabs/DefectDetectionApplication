# Implementation Plan: Portal Build Fleet and Workflow Gates

## Overview

Implementation proceeds pure-logic-first: the build fleet decision functions (request validation, job state machine, dispatcher planner) and the generation gate module are built and property-tested before any handler, then the Lambda handlers wrap them, then infrastructure (scripts, AMD64_NVIDIA target, CDK stack) is wired, and the frontend lands last. The two capability areas — (A) build fleet and (B) workflow generation gate — proceed largely in parallel. All backend property tests use `hypothesis` (min 100 examples, one test per property, traceability comment tags) under `test/backend-test/`, with `moto` for AWS mocks and a stubbed Bedrock client where handlers are exercised. Frontend tests use vitest (`--run`).

## Tasks

- [x] 1. Build fleet pure decision logic
  - [x] 1.1 Create build domain module with target definitions and job state machine
    - Create `edge-cv-portal/backend/functions/build_domain.py` (pure, no AWS clients)
    - Build_Target definitions: target → component name / recipe / required arch map (JP5, JP6, AMD64, AMD64_NVIDIA; arm64 vs x86_64)
    - Status set (queued, provisioning, building, publishing, succeeded, failed, interrupted, cancelled), terminal-status set, and the (current status, event) → next status transition function with terminal absorption (terminal statuses never change)
    - Interruption event handling (non-terminal → interrupted; terminal unchanged) and retry-clone function producing a new job with the same Build_Target and execution mode plus a `retry_of` reference
    - _Requirements: 1.4, 3.5, 3.6, 4.1_

  - [x] 1.2 Write property test for job status transitions
    - Own test module under `test/backend-test/`
    - **Property 7: Status transitions follow the state machine and terminal states absorb**
    - **Validates: Requirements 4.1, 5.1**

  - [x] 1.3 Write property test for interruption and retry identity
    - **Property 23: Interruption and retry preserve job identity**
    - **Validates: Requirements 3.5, 3.6**

  - [x] 1.4 Implement build request validation and job creation functions
    - `validate_build_request(body, servers, config) -> ValidationResult` in `build_domain.py`: non-empty targets, every target supported, execution mode present and valid, dedicated requires server id, server exists and is `running`, server arch matches the required arch of every selected target; each rejection names the failing rule
    - Job-record creation: one Build_Job per target in request order, shared `request_id`, `request_order`, `predecessor_job_id` chaining, execution mode/server applied to every job, requesting user + submission time recorded, `config_snapshot` of the effective configuration at creation, initial status `queued`
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.8, 1.9, 2.4, 2.6, 2.7, 2.8, 9.3_

  - [x] 1.5 Write property test for build request validation
    - **Property 1: Build request validation accepts exactly the valid requests**
    - **Validates: Requirements 1.4, 1.8, 2.4, 2.6, 2.8**

  - [x] 1.6 Write property test for job creation fidelity
    - **Property 2: Job creation records the request faithfully**
    - **Validates: Requirements 1.2, 1.3, 1.5, 1.9, 2.7**

  - [x] 1.7 Implement cancellation decision function
    - Pure function in `build_domain.py`: queued → cancelled and removed from queue; running (building/publishing) → cancelled only when the stop is confirmed within the confirmation window, otherwise status kept with an error naming the Build_Server; terminal → rejected unchanged with an error naming the current status
    - _Requirements: 4.5, 4.6, 4.8, 4.9_

  - [x] 1.8 Write property test for cancellation semantics
    - **Property 9: Cancellation semantics by status**
    - **Validates: Requirements 4.5, 4.6, 4.8, 4.9**

  - [x] 1.9 Implement fleet action validation function
    - `validate_fleet_action(action, server, running_job) -> ValidationResult` in `build_domain.py`: start iff `stopped`; stop iff `running` and no running Build_Job; terminate iff not `terminated` and no running Build_Job; rejections identify the current lifecycle state and (where applicable) the running Build_Job
    - _Requirements: 6.4, 6.10_

  - [x] 1.10 Write property test for fleet action validation
    - **Property 13: Fleet action validation table**
    - **Validates: Requirements 6.4, 6.10**

  - [x] 1.11 Implement build configuration defaults and validation functions
    - In `build_domain.py`: effective-config read applying documented defaults for absent fields (m6g.4xlarge, m6i.4xlarge, 100 GB, us-east-1, 4 h); `validate_build_config(update) -> ValidationResult` with the instance-family → architecture lookup table, positive volume size, positive max runtime; rejected updates leave stored config unchanged (atomic reject)
    - _Requirements: 9.2, 9.5_

  - [x] 1.12 Write property test for configuration defaults and validation
    - **Property 14: Configuration defaults and validation**
    - **Validates: Requirements 9.2, 9.5**

- [x] 2. Dispatcher planning logic (pure)
  - [x] 2.1 Implement dispatch eligibility, server allocation, and queue promotion
    - Create `edge-cv-portal/backend/functions/build_planner.py` (pure planner consumed by the dispatcher handler)
    - Eligibility: a queued job is dispatchable iff its `predecessor_job_id` is null or terminal (any terminal status)
    - Allocation decision: at most one running job per server; a job dispatched to an occupied server goes to that server's queue with status `queued`; dedicated dispatch always targets exactly the server selected in the request
    - Promotion: when a server's job reaches a terminal status, select the queued job with the earliest submission time
    - _Requirements: 1.3, 2.2, 7.1, 7.2, 7.3_

  - [x] 2.2 Write property test for sequential dispatch eligibility
    - **Property 3: Sequential dispatch eligibility within a request**
    - **Validates: Requirements 1.3**

  - [x] 2.3 Write property test for server allocation invariant
    - **Property 4: Server allocation never exceeds one job per server**
    - **Validates: Requirements 7.1, 7.2, 2.2**

  - [x] 2.4 Write property test for queue promotion order
    - **Property 11: Queue promotion picks the oldest queued job**
    - **Validates: Requirements 7.3**

  - [x] 2.5 Implement ephemeral provisioning plan and pre-dispatch verification decision
    - Ephemeral planning in `build_planner.py`: exactly one runner per dispatched job, zero runners when no ephemeral job is queued or running, arch and sizing derived from the job's Build_Target and its own `config_snapshot` (never current config)
    - Pre-dispatch verification decision: parse `pgrep` output (per `.kiro/steering/builds.md` patterns: `gdk component build`, `build-custom.sh`); start iff no build process found, otherwise defer to the head of the queue (original `created_at` retained) with re-verification only after the 5-minute retry interval has elapsed
    - _Requirements: 2.3, 3.1, 3.3, 7.4, 7.5, 7.6, 9.3_

  - [x] 2.6 Write property test for ephemeral runner one-to-one planning
    - **Property 5: Ephemeral runner/job one-to-one**
    - **Validates: Requirements 2.3, 7.4, 3.1, 3.3**

  - [x] 2.7 Write property test for pre-dispatch verification gating
    - **Property 6: Pre-dispatch verification gates the start**
    - **Validates: Requirements 7.5, 7.6**

  - [x] 2.8 Write property test for config snapshot immutability
    - **Property 15: Config snapshots are immutable under config changes**
    - **Validates: Requirements 9.3**

  - [x] 2.9 Implement watchdog deadline arithmetic and sweep decisions
    - In `build_planner.py`: runtime timeout (elapsed > `config_snapshot.max_runtime_hours` → failed with timeout error); termination retry cadence (≤10-minute intervals for up to 1 hour since first failure, orphaned-runner notification exactly when the window is exhausted); pending fleet action failure iff its 10-minute deadline passed; serialization check due iff the check interval has elapsed since the last check
    - Serialization-violation decision: stop-all/fail-all iff detected build-process count ≥ 2, every associated job failed with `SERIALIZATION_VIOLATION`
    - Dead-server sweep: queued jobs for a server failed with a server-state error iff the server state is stopped or terminated
    - _Requirements: 3.8, 3.9, 6.11, 7.7, 7.8, 7.9_

  - [x] 2.10 Write property test for watchdog deadline arithmetic
    - **Property 8: Watchdog deadline arithmetic**
    - **Validates: Requirements 3.8, 3.9, 6.11, 7.7**

  - [x] 2.11 Write property test for serialization violation and dead-server sweeps
    - **Property 12: Serialization violation and dead-server sweeps**
    - **Validates: Requirements 7.8, 7.9**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Generation gate pure module
  - [x] 4.1 Implement generation_gate.py
    - Create `edge-cv-portal/backend/functions/generation_gate.py` in the workflow_generator Lambda bundle (pure, no AWS clients)
    - `STRUCTURAL_ERROR_CODES` frozenset pinning the eight required categories to actual `workflow_core.validator` finding codes (port-type mismatch, backwards edge, cycle, unreachable node, unknown node/port reference, missing input node, missing output node, coexistence conflict)
    - `classify(findings, catalog) -> GateDecision`: structural iff error severity and code in the set; unrepairable iff missing-input/output-node with no catalog node type of that category, or Structural_Error count > `UNREPAIRABLE_ERROR_THRESHOLD` (10); decision accept / repair / reject per the design decision function
    - `build_repair_message(definition_json, structural_errors) -> str` embedding the failed definition and per-error correction instructions
    - `user_readable_errors(structural_errors, definition)` resolving affected nodes/connections to id + display name (id alone when no display name) with a non-empty plain-language explanation
    - _Requirements: 8.2, 8.3, 8.5_

  - [x] 4.2 Write unit test for the structural code mapping
    - Assert every one of the eight Req 8.2 categories has a mapped code present in the real `workflow_core.validator` finding codes
    - _Requirements: 8.2_

  - [x] 4.3 Write property test for structural error classification
    - **Property 17: Structural error classification**
    - **Validates: Requirements 8.2**

  - [x] 4.4 Write property test for the gate decision function
    - **Property 18: Gate decision function**
    - **Validates: Requirements 8.3, 8.5**

  - [x] 4.5 Write property test for user-readable error rendering
    - **Property 22: User-readable error rendering is total**
    - **Validates: Requirements 8.8**

- [x] 5. Workflow generator gate integration
  - [x] 5.1 Reorder persistence behind the gate and add reject paths
    - Modify `workflow_generator.py`: move `put_snapshot` + `save_session` after the gate decision, executed only on accept paths
    - Wrap the validator call fail-closed: any validator exception → `422 GENERATION_VALIDATION_INCOMPLETE`, session untouched; retain the unparseable-output path (`GENERATED_DEFINITION_INVALID`) now provably before persistence
    - On `reject`: return `422 GENERATION_REJECTED` with `user_readable_errors` details envelope before any session mutation; on `accept`: respond as today plus the `gate` metadata object (`passed`, `repaired`, `corrected_errors`, `structural_error_codes`) and the complete findings list
    - _Requirements: 8.1, 8.3, 8.5, 8.9, 8.10, 8.11_

  - [x] 5.2 Implement the Repair_Pass
    - On a `repair` decision: append `build_repair_message` as one additional user turn, re-invoke `invoke_generation` exactly once, then parse + validate + classify the result
    - Clean result → respond with repaired definition, complete findings, `gate.repaired = true`, `gate.corrected_errors` = original structural errors; persist session recording only one user/assistant turn pair (repair-internal turns not persisted)
    - Repair invocation failed or output unparseable → reject with the original Structural_Errors; result still structurally broken → reject with the remaining errors; both leave session untouched
    - _Requirements: 8.4, 8.6, 8.7, 8.9_

  - [x] 5.3 Write property test for repair pass invocation count
    - Stub Bedrock client counting invocations
    - **Property 19: At most one Repair_Pass per generation request**
    - **Validates: Requirements 8.4, 8.5**

  - [x] 5.4 Write property test for repair outcome shaping
    - **Property 20: Repair outcome shaping**
    - **Validates: Requirements 8.6, 8.7**

  - [x] 5.5 Write property test for persistence-iff-accept
    - moto-mocked session table/snapshot store + stubbed Bedrock client across all six outcome shapes
    - **Property 21: Session persistence if and only if the gate accepts**
    - **Validates: Requirements 8.1, 8.5, 8.7, 8.9, 8.10, 8.11**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Build fleet API handlers
  - [x] 7.1 Register build permissions in RBAC
    - Add `builds:submit`, `builds:cancel`, `builds:read` to the shared_utils RBACManager with the global (`allow_global`) scope pattern; grant Build_Operator (submit/cancel/read) to DataScientist, UseCaseAdmin, PortalAdmin; denials return the standard authorization error and record a denied-access Audit_Log entry
    - _Requirements: 1.6, 4.10, 6.7, 9.6_

  - [x] 7.2 Implement build_jobs.py handler
    - Create `edge-cv-portal/backend/functions/build_jobs.py` following portal handler conventions (error envelope, `get_user_from_event`, `log_audit_event`), delegating decisions to `build_domain.py`
    - `POST /builds`: validate, create one job per target (BuildJobs table), audit `build_requested`, async-invoke the dispatcher; `GET /builds`: 90-day history most recent first, paginated, with published artifact identifiers on succeeded jobs; `GET /builds/{id}`; `GET /builds/{id}/logs`: CloudWatch Logs page with `nextToken` pagination; `POST /builds/{id}/cancel`: queued → immediate, running → SSM stop + pgrep confirmation within 5 minutes, terminal → 409; `POST /builds/{id}/retry`: retry-clone of an interrupted job with `retry_of`
    - _Requirements: 1.1, 1.2, 1.5, 1.6, 1.7, 1.9, 3.6, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10_

  - [x] 7.3 Write property test for build history ordering
    - **Property 16: Build history ordering and content**
    - **Validates: Requirements 4.7**

  - [x] 7.4 Write unit tests for build_jobs RBAC and audit
    - Unauthorized submit and cancel: rejected, authorization error, denied-access audit entry (1.6, 4.10); `build_requested` audit content on create (1.7); cancellation audit entries including the failed-cancellation path (4.5, 4.6, 4.9)
    - _Requirements: 1.6, 1.7, 4.5, 4.6, 4.9, 4.10_

  - [x] 7.5 Implement build_fleet.py handler
    - Create `edge-cv-portal/backend/functions/build_fleet.py`; PortalAdmin-gated actions, `builds:read` list
    - `GET /build-servers`: fleet list with live `DescribeInstances` reconciliation; `POST /build-servers`: RunInstances with arch-selected Ubuntu 22.04 AMI, configured type/volume, hardened profile (extended `dda-build-role`, no key pair, no inbound rules, IMDSv2), user-data bootstrap (`setup-build-server.sh` equivalent + repo clone), register in BuildServers; start/stop with `validate_fleet_action`; `DELETE /build-servers/{id}` with `confirm: "<server name>"` echo; `pending_action` markers with 10-minute deadlines; audit every action outcome
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.10, 6.12_

  - [x] 7.6 Write unit and integration tests for build_fleet
    - Terminate confirmation flow and its cancellation leave the server unchanged (6.6, 6.12); non-PortalAdmin denial with audit (6.7); action-outcome audit entries (6.8); moto-based lifecycle integration for start, stop, and launch (6.2, 6.3, 6.5)
    - _Requirements: 6.2, 6.3, 6.5, 6.6, 6.7, 6.8, 6.12_

  - [x] 7.7 Implement build_config.py handler
    - Create `edge-cv-portal/backend/functions/build_config.py`; `GET /build-config` (`builds:read`) applying defaults on read; `PUT /build-config` (PortalAdmin only) using `validate_build_config`, atomic reject retaining prior values, Audit_Log entry per applied change (parameter, prior value, new value, user, time); store under PortalSettings key `build_infrastructure_config`
    - _Requirements: 9.1, 9.2, 9.4, 9.5, 9.6_

  - [x] 7.8 Write unit tests for build_config
    - Config read wiring for dispatch/launch parameters (9.1); audit entry content on change (9.4); non-PortalAdmin denial with audit (9.6)
    - _Requirements: 9.1, 9.4, 9.6_

- [x] 8. Dispatcher and event consumer handlers
  - [x] 8.1 Implement build_dispatcher.py handler
    - Create `edge-cv-portal/backend/functions/build_dispatcher.py` (async on-submit invoke + 1-minute schedule), executing `build_planner.py` decisions with DynamoDB conditional updates (ConditionExpression on expected status; server allocation via `attribute_not_exists(running_build_job_id)`)
    - Tick order: dispatch eligible queued jobs (dedicated: allocate → pre-dispatch pgrep SSM verification → SendCommand agent); provision ephemeral runners (RunInstances per job arch/`config_snapshot`, SSM ping then SendCommand; RunInstances failure → failed with cause, partial compute terminated, audited); runtime timeout watchdog (SSM stop, failed, logs retained); serialization watchdog (pgrep count, count ≥ 2 → pkill within 60 s, jobs failed `SERIALIZATION_VIOLATION`, audited); termination watchdog (terminate terminal-job runners, retries ≤ every 10 min for 1 h, then SNS notify + `orphaned_runner` audit); queue-orphan and pending-action-deadline sweeps
    - _Requirements: 3.1, 3.2, 3.3, 3.7, 3.8, 3.9, 6.11, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9_

  - [x] 8.2 Write moto integration tests for dispatcher ticks
    - End-to-end tick over mocked DynamoDB/EC2/SSM: dedicated dispatch with allocation lock, ephemeral provision + terminate, provisioning-failure path (3.7), timeout and serialization watchdog actions
    - _Requirements: 3.1, 3.7, 3.8, 7.2, 7.5, 7.8_

  - [x] 8.3 Implement build_events.py handler
    - Create `edge-cv-portal/backend/functions/build_events.py` consuming EventBridge: custom `dda.portal.builds` phase events (conditional transitions, start/end times, result metadata verbatim, `publish_partial` lists, `build_published` audit on success); EC2 spot interruption / state-change with a non-terminal job → `interrupted`; fleet instance state-change → BuildServers state + `last_state_change_at`, clear `pending_action` on expected state; SSM command Failed/TimedOut/Cancelled → failed/interrupted fallback; all transitions idempotent via conditional updates (duplicate delivery is a no-op)
    - Include the pure event-application function (event payload → job field updates) so Property 10 tests it directly
    - _Requirements: 3.5, 5.1, 5.3, 5.4, 5.5, 6.2, 6.3, 6.9, 6.11_

  - [x] 8.4 Write property test for completion event recording
    - **Property 10: Result and failure recording on completion events**
    - **Validates: Requirements 5.3, 5.4**

  - [x] 8.5 Write unit tests for build_events
    - Duplicate EventBridge delivery is a no-op (4.1 idempotence); `build_published` audit content (5.5); publishing failure recorded with published/unpublished lists and distinct error kind (5.4)
    - _Requirements: 4.1, 5.4, 5.5_

- [x] 9. Build scripts and AMD64_NVIDIA target
  - [x] 9.1 Create non-interactive portal-build.sh
    - Refactor `gdk-component-build-and-publish.sh` into `portal-build.sh`: remove the interactive InferenceUploader prompt, emit a `phase=publishing` event between build and publish, print the machine-readable `PORTAL_BUILD_RESULT {json}` line (component name, published version, pushed image refs), accept `x86_64_nvidia` as an ARCH value mapping to `aws.edgeml.dda.LocalServer.amd64Nvidia` / `recipe-amd64-nvidia.yaml`
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 9.2 Create scripts/portal-build-agent.sh
    - SSM-executed wrapper (params `BUILD_JOB_ID`, `BUILD_TARGET`, `EVENT_BUS`, `SOURCE_REF`): `flock -n` on `/var/lock/dda-build.lock` (exit 75 when held); git source sync to `SOURCE_REF`; `phase=building` PutEvents; target → build-argument map (JP5/JP6/AMD64/AMD64_NVIDIA); invoke `portal-build.sh`; on success emit `phase=succeeded` with parsed result metadata; on failure emit `phase=failed` distinguishing build vs publish stage (`error_kind=publishing` with per-artifact published/unpublished lists)
    - _Requirements: 5.2, 5.3, 5.4, 7.1_

  - [x] 9.3 Add the AMD64_NVIDIA build target
    - Create `recipe-amd64-nvidia.yaml` (cloned from `recipe-amd64.yaml`, component `aws.edgeml.dda.LocalServer.amd64Nvidia`, NVIDIA x86 docker-compose/GPU runtime settings)
    - Extend `build-custom.sh` name derivation: component name containing `Nvidia` sets `IS_X86_NVIDIA=1` → `ONNXRUNTIME_GPU=1` on x86 and `BACKEND_DOCKERFILE=Dockerfile.x86_64_nvidia`; create `src/backend/Dockerfile.x86_64_nvidia` on a CUDA x86 base following the plugin-image precedent
    - Update the security preservation golden baselines under `test/backend-test/security/baselines/` for every touched tracked file in this same change (recompute sha256 per `.kiro/steering/builds.md`) and run the preservation suite to confirm
    - _Requirements: 1.1, 1.4_

- [x] 10. CDK infrastructure
  - [x] 10.1 Create infrastructure/lib/build-fleet-stack.ts
    - Follow `node-designer-stack.ts` patterns: DynamoDB `BuildJobs` (GSIs status/server/request, TTL 180 days) and `BuildServers` (PAY_PER_REQUEST, PITR); the five Lambdas with shared-utils layer and environment (table names, log group, event bus, SNS topic); EventBridge 1-minute schedule → dispatcher plus rules for EC2 state-change, spot interruption, SSM command status, and custom `dda.portal.builds` source → build_events; CloudWatch Logs group `/dda/portal-builds` retention ≥ 90 days; SNS topic `dda-portal-build-alerts`; scoped IAM (EC2 actions condition-keyed to `dda-build:*` tags, SSM send/describe; instance profile = CDK-created extended `dda-build-role` with `events:PutEvents`, logs, publish permissions); API Gateway routes on the existing REST API and authorizer
    - _Requirements: 3.1, 3.4, 3.9, 4.4, 6.5, 9.1_

  - [x] 10.2 Write CDK snapshot tests for the build fleet stack
    - Assert log-group retention ≥ 90 days and job TTL ≥ 90 days (3.4, 4.4), EventBridge rules present, IAM scoping condition keys
    - _Requirements: 3.4, 4.4_

- [x] 11. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Frontend
  - [x] 12.1 Implement the Builds page and job detail
    - Create `frontend/src/pages/builds/BuildsPage.tsx` + detail: Cloudscape Table of 90-day history most recent first (status Badge, target, mode, requester, times, published version for succeeded); submit Form with ordered target multi-select and execution-mode RadioGroup (dedicated option lists running servers; ephemeral is the only selectable mode when the fleet has no non-terminated server); job detail with log viewer polling `/builds/{id}/logs` every 30 s while running and status polling every 15 s; cancel and retry actions per status
    - _Requirements: 1.1, 2.1, 2.5, 4.2, 4.3, 4.4, 4.7_

  - [x] 12.2 Write vitest render tests for the Builds page
    - Target selection and submit controls (1.1), execution-mode selection (2.1), ephemeral-only when no servers (2.5), job detail fields (4.3)
    - _Requirements: 1.1, 2.1, 2.5, 4.3_

  - [x] 12.3 Implement the Fleet page
    - Create `frontend/src/pages/admin/FleetPage.tsx`, PortalAdmin-gated like UserManager: server table (name, instance id, type, architecture, lifecycle state with 15 s polling, running Build_Job link, last state change); launch modal (name + architecture radio); start/stop buttons enabled by state; terminate flow with type-the-name confirmation Modal
    - _Requirements: 6.1, 6.2, 6.3, 6.6, 6.9, 6.12_

  - [x] 12.4 Write vitest render tests for the Fleet page
    - Fleet list columns (6.1), terminate confirmation requires the typed name and cancel leaves the server unchanged (6.6, 6.12)
    - _Requirements: 6.1, 6.6, 6.12_

  - [x] 12.5 Implement the Build settings section
    - Add the build infrastructure configuration form to the existing settings page with per-field validation errors surfaced from `PUT /build-config`
    - _Requirements: 9.1, 9.5_

  - [x] 12.6 Implement chat generation rejection and repaired-notice UI
    - In the workflows chat panel: on `GENERATION_REJECTED` / `GENERATION_VALIDATION_INCOMPLETE`, render an Alert type="error" listing each structural error with affected node/connection display names (fallback to ids) and the plain-language explanation; keep the submitted prompt in the input (clear only on 200); on a repaired acceptance render an Alert type="info" listing the corrected errors
    - _Requirements: 8.6, 8.8_

  - [x] 12.7 Write vitest tests for rejection and repaired-notice rendering
    - Rejection alert content with display-name fallback and prompt retention (8.8); repaired-notice with corrected errors (8.6)
    - _Requirements: 8.6, 8.8_

  - [x] 12.8 Wire routes and navigation
    - Register the Builds page and Fleet page routes and side-navigation entries (Fleet gated to PortalAdmin), connect the settings section, and verify the frontend builds
    - _Requirements: 1.1, 6.1_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Property-based tests use `hypothesis` with `@settings(max_examples=100)` or higher, exactly one test per property, each tagged with a comment: `# Feature: portal-build-fleet-and-workflow-gates, Property {N}: {title}`
- Each property test lives in its own test module under `test/backend-test/` so tests can run and be authored independently
- Backend handler tests use `moto` for DynamoDB/S3/EC2/SSM/EventBridge and a stubbed Bedrock client, following existing patterns in `test/backend-test/`
- Frontend tests run with vitest in single-execution mode (`vitest --run`)
- Task 9.3 touches security-preservation-tracked files (`build-custom.sh`, `src/backend` Dockerfiles); the golden baselines under `test/backend-test/security/baselines/` must be updated in the same change per `.kiro/steering/builds.md`
- Real-account timing validation (ephemeral/dedicated end-to-end) and on-device AMD64_NVIDIA hardware verification are manual steps outside this coding plan (see design Testing Strategy)
- Checkpoints ensure incremental validation; each task references specific requirements for traceability

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1", "7.1", "9.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "4.2", "4.3", "4.4", "4.5", "5.1", "9.2", "9.3"] },
    { "id": 2, "tasks": ["1.5", "1.6", "1.7", "2.2", "2.3", "2.4", "2.5", "5.2"] },
    { "id": 3, "tasks": ["1.8", "1.9", "2.6", "2.7", "2.8", "2.9", "5.3", "5.4", "5.5", "7.2"] },
    { "id": 4, "tasks": ["1.10", "1.11", "2.10", "2.11", "7.3", "7.4", "7.5", "8.1"] },
    { "id": 5, "tasks": ["1.12", "7.6", "7.7", "8.2", "8.3"] },
    { "id": 6, "tasks": ["7.8", "8.4", "8.5", "10.1"] },
    { "id": 7, "tasks": ["10.2", "12.1", "12.3", "12.5", "12.6"] },
    { "id": 8, "tasks": ["12.2", "12.4", "12.7", "12.8"] }
  ]
}
```
