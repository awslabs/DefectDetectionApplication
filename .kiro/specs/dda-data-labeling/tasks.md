# Implementation Plan: DDA Data Labeling System

## Overview

Implementation proceeds bottom-up: shared-layer pure modules (distribution, manifest serialization) first so the core logic is property-testable in isolation, then RBAC and storage, then the backend Lambdas (job creation, distribution/notifications, labeler APIs, auto-labeling, skip-verification review, manifest generation), then CDK wiring, and finally the frontend (role gating, job wizard, teams page, labeler workspace, admin review). Property-based tests (Hypothesis, `backend/tests` conventions, files named `test_property_dda_labeling_*.py`) sit directly beside the code they validate; frontend tests use Vitest/RTL.

## Tasks

- [ ] 1. Implement shared-layer pure modules
  - [x] 1.1 Implement `labeling_distribution.py` in the shared layer
    - Create `edge-cv-portal/backend/layers/shared/python/labeling_distribution.py`
    - `distribute(task_ids, member_ids) -> dict[str, str]`: deterministic round-robin (sorted members, task i → member[i % n]) guaranteeing per-member counts differ by at most one
    - `rebalance(unassigned_task_ids, member_ids) -> dict[str, str]`: same round-robin over only the tasks being reassigned
    - _Requirements: 5.1, 5.2, 5.3, 5.5_

  - [ ]* 1.2 Write property test for the distributor
    - **Property 8: Initial distribution is total, exclusive, and balanced**
    - **Validates: Requirements 5.1, 5.2**

  - [x] 1.3 Implement `dda_manifest.py` in the shared layer
    - Create `edge-cv-portal/backend/layers/shared/python/dda_manifest.py` with the canonical modality-tagged annotation model
    - `build_color_map(label_set)`: fixed 10-color palette plus background, deterministic from Label_Set order
    - `render_mask_png(regions, width, height, color_map)`: Pillow PNG at source dimensions, one distinct color per class, background elsewhere; RLE region decoding
    - `serialize_manifest(annotations, job)`: JSON Lines emission per modality — Classification (`source-ref`, `anomaly-label` 0/1, `anomaly-label-metadata` with class-name, confidence in [0,1], type, job-name, human-annotated, creation-date), Segmentation (adds `anomaly-mask-ref` + `anomaly-mask-ref-metadata` with job-wide `internal-color-map`; mask keys contain no colons), Object_Detection (GT `bounding-box`/`bounding-box-metadata` structure with zero-based `class_id` and in-bounds pixel coordinates)
    - `parse_manifest(lines, modality)`: inverse of serialization back to canonical annotations (masks decoded through the color map)
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.7, 9.11_

  - [ ]* 1.4 Write property test for manifest round trip
    - **Property 20: Manifest serialization round trip**
    - **Validates: Requirements 10.7**

  - [ ]* 1.5 Write property test for per-modality manifest fields
    - **Property 18: Manifest entries carry the exact DDA fields per modality**
    - **Validates: Requirements 9.11, 10.3, 10.4, 10.5**

  - [ ]* 1.6 Write property test for existing-validation compatibility
    - Feed generated manifests through the real `manifest_transformer.detect_ground_truth_attributes` + `manifest_validator` logic
    - **Property 19: Manifests pass existing validation unchanged**
    - **Validates: Requirements 10.6**

- [ ] 2. Add the Data_Labeler role and labeling permissions to RBAC
  - [x] 2.1 Extend `shared_utils.py` and role administration
    - Add `Role.DATA_LABELER = 'DataLabeler'`, `Permission.LABELING_TASKS_SELF`, `Permission.MANAGE_LABELING_TEAMS`
    - `_initialize_role_permissions()`: `DATA_LABELER → {LABELING_TASKS_SELF}` only; grant `LABELING_TASKS_SELF` also to DataScientist/UseCaseAdmin/PortalAdmin; grant `MANAGE_LABELING_TEAMS` to UseCaseAdmin/PortalAdmin
    - Accept `DataLabeler` as a valid role value in `user_admin.py` role assignment (`PUT /admin/users/{username}/role`) and `user_roles.py`
    - Verify per-request enforcement through the existing `@rbac_check` path (403 + `unauthorized_access` audit event with user, resource, timestamp)
    - _Requirements: 2.1, 2.3, 2.5, 3.7_

  - [ ]* 2.2 Write property test for unauthorized API denial
    - **Property 5: Unauthorized API access is denied with an audit event**
    - **Validates: Requirements 2.3, 3.7, 9.1**

- [ ] 3. Create DynamoDB tables in the storage stack
  - [x] 3.1 Add labeling teams and tasks tables to `storage-stack.ts`
    - `dda-portal-labeling-teams`: PK `team_id`, SK (META / MEMBER#user_id), GSI `usecase-teams-index` (usecase_id, created_at)
    - `dda-portal-labeling-tasks`: PK `job_id`, SK `task_id`, GSI `assignee-index` (assignee_user_id, job_id)
    - Standard template: PAY_PER_REQUEST, PITR, RETAIN; export table references for the compute stack
    - _Requirements: 12.8_

- [ ] 4. Implement labeling team management backend
  - [x] 4.1 Create `dda_labeling.py` with team management handlers
    - Create `edge-cv-portal/backend/functions/dda_labeling.py` with router + `@rbac_check(MANAGE_LABELING_TEAMS)` handlers
    - `GET /labeling-teams?usecase_id=`: teams scoped to the use case with member identities and emails
    - `POST /labeling-teams`: create with name validation (non-empty, ≤128 chars, unique per use case via GSI query)
    - `DELETE /labeling-teams/{teamId}`: rejected while an InProgress job references the team
    - `POST /labeling-teams/{teamId}/members`: validate Data_Labeler role and duplicate membership; persist member with email
    - `DELETE /labeling-teams/{teamId}/members/{userId}`: persist removal (reassignment wiring added in task 7.2)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8_

  - [ ]* 4.2 Write property test for team management validation
    - **Property 7: Team management validation**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.8**

- [ ] 5. Implement backend selection and DDA job creation
  - [x] 5.1 Add the backend switch and merged listing to `labeling.py`
    - `create_labeling_job`: mandatory `labeling_backend` field; `GroundTruth` → existing flow unchanged; `DDA` → delegate to `dda_labeling.create_dda_job`; missing/invalid value → 400 identifying the backend, nothing persisted
    - Persist `labeling_backend` on every job item (both paths)
    - `list_labeling_jobs`: single merged list; skip the SageMaker status-sync loop for `labeling_backend='DDA'` items
    - `get_labeling_job`: return DDA fields (team, modality, image_count, submitted count, progress percentage rounded to nearest whole number, per-member submitted/remaining, unassigned count, blocked flag, notification state, skip-verification progress substitution)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 11.1, 11.2, 11.10_

  - [ ]* 5.2 Write property test for merged job listing
    - **Property 3: Job listing merges both backends**
    - **Validates: Requirements 1.5**

  - [x] 5.3 Implement `create_dda_job` validation, enumeration, and persistence in `dda_labeling.py`
    - Validate all parameters before enumeration: name (1–63 chars, unique per use case), modality, Label_Set (1–10 distinct non-empty names ≤64 chars for Segmentation/ObjectDetection; fixed `['normal','anomaly']` for Classification), team (required unless skip-verification; reject empty team), instructions ≤5,000 chars, ≤10 good and ≤10 bad JPEG/PNG example refs, auto-label model/modality compatibility matrix (SAM: Segmentation/ObjectDetection; Bedrock: Classification/ObjectDetection), skip-verification admin authorization + Bedrock model + per-label prompts covering every label
    - Enumerate the dataset prefix (nested prefixes included) via `get_s3_client_for_bucket`; reject zero images, inaccessible objects, and unsupported formats, identifying each offending object
    - On any rejection: error enumerating each offending element, no job record, no tasks, no S3/SageMaker resources
    - On success: persist job with `status=InProgress`, `labeling_backend='DDA'`, `image_count`, all submitted fields; write `job_created` audit event; return job id; async-invoke `dda_labeling_worker` with `{action: 'distribute', job_id}`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 8.1, 8.8, 9.1, 9.2, 9.3, 11.3, 11.7, 12.1, 12.2, 12.3_

  - [ ]* 5.4 Write property test for invalid job submission rejection
    - **Property 1: Invalid job submissions are rejected and persist nothing**
    - **Validates: Requirements 1.6, 4.1, 4.2, 4.4, 4.6, 4.7, 4.8, 4.9, 4.10, 9.2, 9.3**

  - [ ]* 5.5 Write property test for valid job creation
    - **Property 2: Valid job creation persists a complete job record**
    - **Validates: Requirements 1.4, 4.5, 4.11, 12.8**

  - [ ]* 5.6 Write unit tests for backend dispatch and creation edge cases
    - GroundTruth path unchanged and no DDA component invoked (1.2); DDA path creates no SageMaker resources (1.3)
    - Fixed classification Label_Set (4.3); auto-label model/modality matrix rejections (8.8); bucket inaccessible via role and fallback (12.2, 12.3)
    - _Requirements: 1.2, 1.3, 4.3, 8.8, 12.2, 12.3_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement task distribution, rebalancing, and notifications
  - [x] 7.1 Create `dda_labeling_worker.py` with the distribute action
    - Create `edge-cv-portal/backend/functions/dda_labeling_worker.py` (async-invoked worker with an `action` dispatcher)
    - `distribute`: apply `labeling_distribution.distribute` over enumerated images and team members holding the Data_Labeler role; write task items (`task-<zero-padded index>`, `status=Assigned`) with `batch_writer`; verify written count equals `image_count`, on shortfall set job `Failed` with `failure_reason` and mark written tasks `Inactive`
    - Enqueue one SQS auto-label message per image when auto-labeling or skip-verification is enabled
    - _Requirements: 5.1, 5.2, 5.6_

  - [x] 7.2 Wire membership changes to reassignment in `dda_labeling.py`
    - Member removal: query the member's unsubmitted tasks per InProgress job, compute `rebalance` over remaining members, apply with conditional updates (`status = Assigned AND assignee = removed_user`); submitted tasks and annotations untouched; on partial failure restore prior assignments from the computed inverse and return an error with membership unchanged; delete membership only after reassignment succeeds
    - Last member removed: tasks → `assignee_user_id='UNASSIGNED'`, job `blocked=true`, status stays InProgress
    - Member added to a team with blocked jobs: distribute unassigned tasks across current members, clear `blocked`, notify members who previously held zero tasks
    - _Requirements: 3.6, 5.3, 5.4, 5.5, 5.7, 6.7_

  - [ ]* 7.3 Write property test for rebalancing
    - **Property 9: Membership changes rebalance without touching submitted work**
    - **Validates: Requirements 3.6, 5.3, 5.4, 5.5**

  - [x] 7.4 Implement the SES notification service in `dda_labeling_worker.py`
    - After distribution/rebalancing: send exactly one email per member holding ≥1 task (rebalancing: only members who previously held zero); body contains job name, recipient's assigned image count, and `https://{portal_domain}/labeler?job={job_id}` link
    - Recipient emails from Cognito via `USER_POOL_ID`; sender from `SES_SENDER_ADDRESS`
    - Per-recipient retry (3 total attempts); terminal failures appended to `notification_failures` with address and reason, remaining recipients processed, job status untouched
    - `SES_SENDER_ADDRESS` unset → job proceeds with `notifications_skipped=true`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [ ]* 7.5 Write property test for notification recipients
    - **Property 10: Notification recipients and content**
    - **Validates: Requirements 6.1, 6.2, 6.7**

  - [ ]* 7.6 Write unit tests for distribution and notification fault paths
    - Task creation shortfall → Failed + Inactive tasks (5.6); rebalancing rollback (5.7); SES retry exhaustion and skipped-sender recording (6.3, 6.4, 6.6)
    - _Requirements: 5.6, 5.7, 6.3, 6.4, 6.6_

- [ ] 8. Implement labeler APIs
  - [x] 8.1 Add labeler read APIs to `dda_labeling.py`
    - All labeler routes: `@rbac_check(LABELING_TASKS_SELF)` plus server-side ownership checks (`assignee_user_id == caller.sub` AND caller currently a member of the job's team); violations → 403 with no resource data + `labeler_access_denied` audit event
    - `GET /labeler/jobs`: jobs where the caller holds ≥1 unsubmitted task (via `assignee-index`), with submitted/remaining counts; empty result when none
    - `GET /labeler/jobs/{jobId}/next`: next presentable unsubmitted task — excludes presentation-failed tasks and tasks with `prelabel_status=Pending`; returns task id, 15-minute single-object presigned image URL, pre-label payload when available, instructions and example-image URLs (omitting absent ones), submitted/remaining/withheld counts; completion payload when zero presentable tasks remain
    - `GET /labeler/tasks/{taskId}/image-url`: fresh 15-minute presigned URL for the task's image
    - _Requirements: 2.4, 2.6, 7.1, 7.2, 7.10, 7.11, 8.3, 8.6, 8.7, 12.6, 12.7_

  - [ ]* 8.2 Write property test for task presentation gating
    - **Property 11: Task presentation gating**
    - **Validates: Requirements 7.2, 7.12, 8.6, 8.7**

  - [ ]* 8.3 Write property test for image access grants
    - **Property 22: Image access grants are scoped and short-lived**
    - **Validates: Requirements 12.6**

  - [x] 8.4 Add submission and presentation-failure APIs to `dda_labeling.py`
    - `POST /labeler/tasks/{taskId}/submit`: server-side completeness validation per modality (classification selection present; every region/box carries a Label_Set class; box coordinates within image bounds); reject Stopped jobs before persisting; persist annotation (inline for Classification/ObjectDetection, S3 RLE JSON for Segmentation) with submitter identity and timestamp via conditional write (`status = Assigned AND assignee_user_id = :caller`), record human-annotated; atomically increment the job's `submitted_count` and, when it reaches `image_count`, async-invoke the worker with `{action: 'generate_manifest', job_id}`
    - Persistence failure → task stays Assigned, 500 to client
    - `POST /labeler/tasks/{taskId}/presentation-failure`: record reason, mark task `PresentationFailed` (withheld)
    - _Requirements: 7.7, 7.8, 7.9, 7.12, 8.4, 11.6, 11.8_

  - [ ]* 8.5 Write property test for submission persistence and rejection
    - **Property 12: Submission persistence and rejection**
    - **Validates: Requirements 7.7, 7.8, 8.4, 11.8**

  - [ ]* 8.6 Write property test for labeler data isolation
    - **Property 6: Labeler data isolation**
    - **Validates: Requirements 2.4, 2.6, 7.1**

- [ ] 9. Implement job lifecycle: stop and status transitions
  - [x] 9.1 Add the stop route and lifecycle audit events to `labeling.py`
    - `POST /labeling/{id}/stop` (`MANAGE_LABELING_JOBS`, DDA jobs only): InProgress → Stopped with `stopped_at`, annotations retained; non-InProgress → validation error leaving status unchanged; stop failure → job stays InProgress with an explicit not-stopped error
    - Audit events (`log_audit_event`) for job created, stopped, completed with acting user, job id, event type, timestamp
    - _Requirements: 11.3, 11.4, 11.5, 11.7, 11.9_

  - [ ]* 9.2 Write property test for the job status lifecycle
    - **Property 21: Job status lifecycle**
    - **Validates: Requirements 11.3, 11.4, 11.6, 11.9**

  - [ ]* 9.3 Write property test for progress accounting
    - **Property 13: Progress accounting**
    - **Validates: Requirements 7.10, 7.11, 11.1, 11.2, 11.10**

- [ ] 10. Implement the auto-labeler pipeline
  - [x] 10.1 Create `dda_autolabel_worker.py` (SQS consumer)
    - Process `{job_id, task_id, image_s3_uri, modality, label_set, model, per_label_prompts?}` messages; read images via `get_s3_client_for_bucket`
    - Bedrock path: Converse request with image block and structured prompt (per-label prompts in skip-verification mode) demanding JSON output; client from `bedrock_common.get_bedrock_client`, read timeout ≤120 s, retries disabled; strict output validation — class outside Label_Set, malformed geometry, or out-of-bounds box ⇒ failure
    - SAM path: synchronous invoke of `dda_sam_worker` bounded at 120 s wall-clock; class-agnostic regions stored with `class: null`
    - Write pre-label JSON to the portal artifacts bucket, then conditional-update `prelabel_status: Available|Failed` (+ `prelabel_error`); in skip-verification mode decrement `autolabel_pending` and set `review_ready=true` at zero; record `autolabel_error` for review-ineligible failures
    - _Requirements: 8.2, 8.5, 8.6, 9.4, 9.10, 12.1, 12.2_

  - [x] 10.2 Create the `dda_sam_worker` container-image Lambda
    - `edge-cv-portal/backend/sam-worker/`: Dockerfile with a CPU ONNX-exported SAM variant (e.g. MobileSAM) and handler returning mask polygons/RLE per detected region for a supplied image
    - _Requirements: 8.1, 8.2_

  - [ ]* 10.3 Write property test for pre-label class validation
    - **Property 14: Pre-labels use only Label_Set classes or fail**
    - **Validates: Requirements 8.2, 8.5, 9.10**

- [ ] 11. Implement skip-verification mode and admin review
  - [x] 11.1 Implement skip-verification creation behavior in `dda_labeling.py`
    - Creation path (extends 5.3): zero labeler Task_Assignments and zero notifications; one result item per image with `assignee_user_id='AUTO'` and `prelabel_status=Pending`; initialize `autolabel_pending` counter; fan out every image on the SQS queue (Bedrock path only)
    - _Requirements: 9.1, 9.4, 9.5_

  - [ ]* 11.2 Write property test for skip-verification job shape
    - **Property 15: Skip-verification creates no labeler work**
    - **Validates: Requirements 9.4, 9.5**

  - [x] 11.3 Add admin review APIs to `dda_labeling.py`
    - `GET /labeling/{id}/review` (admin + job creator): paginated results covering every dataset image with its auto-labeled result or failed status and current decision
    - `POST /labeling/{id}/review/decisions`: batch `{task_id: 'accepted'|'rejected'}` upserts; failed images ineligible for acceptance; decisions mutable until finalized, immutable after
    - `POST /labeling/{id}/review/finalize`: reject with undecided count if any successful result is undecided; reject if zero accepted; on success mark `review_finalized=true` and async-invoke the worker with `{action: 'generate_manifest', job_id}` over exactly the accepted set
    - _Requirements: 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 11.6_

  - [ ]* 11.4 Write property test for review decisions and finalize gating
    - **Property 16: Review decisions and finalize gating**
    - **Validates: Requirements 9.6, 9.7, 9.8**

- [ ] 12. Implement manifest generation
  - [x] 12.1 Add the `generate_manifest` action to `dda_labeling_worker.py`
    - Gather included annotations (team jobs: all submitted tasks; skip-verification: exactly the accepted results; rejected/failed/unsubmitted excluded)
    - Render segmentation masks with `dda_manifest.render_mask_png` and the job-wide color map; write masks to `s3://{output_bucket}/labeled/{job_id}/masks/` (no colons in keys) via the cross-account mechanism with direct fallback
    - Serialize with `dda_manifest.serialize_manifest`; run the emitted lines through `detect_ground_truth_attributes` + `manifest_validator` for the job's task type — validation failure is generation failure
    - Success: write manifest to the use case output bucket, record `output_manifest_s3_uri` (same field as GT jobs), set `status=Completed` + `completed_at`, write the completed audit event — only after manifest write and validation succeed
    - Failure: no manifest URI recorded, annotations untouched, `status=Failed` with `failure_reason` surfaced on the job detail
    - _Requirements: 9.9, 10.1, 10.2, 10.6, 10.8, 10.9, 11.6, 11.7, 12.4, 12.5_

  - [ ]* 12.2 Write property test for manifest inclusion
    - **Property 17: Manifest inclusion is exact**
    - **Validates: Requirements 9.9, 10.1, 10.2**

  - [ ]* 12.3 Write unit tests for manifest failure atomicity and field placement
    - S3 write failure → no URI, status Failed, annotations retained (10.9, 12.5); manifest URI exposed in the same job fields GT jobs use (10.8)
    - _Requirements: 10.8, 10.9, 12.5_

- [x] 13. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 14. Wire infrastructure (CDK)
  - [x] 14.1 Add compute resources to `compute-stack.ts`
    - `DdaLabelingHandler` (`dda_labeling.handler`, 30 s), `DdaLabelingWorker` (`dda_labeling_worker.handler`, 900 s, 2 GB with Pillow available), `DdaAutolabelWorker` (`dda_autolabel_worker.handler`, 300 s) with SQS event source
    - `dda-portal-autolabel-queue` + DLQ (visibility 300 s, maxReceiveCount 3, batch size 1–5)
    - `DdaSamWorker` as `lambda.DockerImageFunction` (image under `backend/sam-worker/`, 10 GB, 300 s)
    - Env vars: `LABELING_TEAMS_TABLE`, `LABELING_TASKS_TABLE`, `AUTOLABEL_QUEUE_URL`, `SAM_WORKER_FUNCTION_NAME`, `DDA_LABELING_WORKER_FUNCTION_NAME`, `SES_SENDER_ADDRESS`, `PORTAL_DOMAIN` + shared `lambdaEnvironment`
    - IAM: `createLambdaRole` grants plus `ses:SendEmail` (worker), `bedrock:InvokeModel`/`Converse` (autolabel worker), `lambda:InvokeFunction` (handler→worker, autolabel→SAM), SQS send/consume
    - _Requirements: 6.5, 8.1, 12.8_

  - [x] 14.2 Create `dda-labeling-api-stack.ts` nested API stack
    - Register `/labeling-teams*`, `/labeler*`, `/labeling/{id}/stop`, `/labeling/{id}/review*` against the imported Rest API (UserAdminApiStack pattern: imported API id, own Cognito authorizer, route-salted CfnDeployment); wire the nested stack into the infrastructure app
    - _Requirements: 2.5, 3.7, 11.4_

- [ ] 15. Implement frontend role gating and API client
  - [x] 15.1 Extend frontend types and `apiService`
    - Add `'DataLabeler'` to `UserRole` in `types/index.ts`
    - Add `apiService` methods: `listLabelingTeams`, `createLabelingTeam`, `addTeamMember`, `removeTeamMember`, `stopLabelingJob`, `getLabelerJobs`, `getNextTask`, `submitTask`, `reportPresentationFailure`, `refreshTaskImageUrl`, `getReview`, `saveReviewDecisions`, `finalizeReview`
    - _Requirements: 2.2, 3.8, 7.7, 9.6, 11.4_

  - [x] 15.2 Implement Data_Labeler navigation and route gating
    - `Layout.tsx` `buildNavigationItems(role)`: DataLabeler → only `[{ text: 'My Labeling Tasks', href: '/labeler' }]`; settings dropdown keeps only sign-out and account settings; all other roles unchanged
    - `App.tsx`: `DataLabelerRedirect` wrapper redirecting DataLabeler-only users from any route except `/labeler`, `/login`, account settings to `/labeler` without rendering the page; post-login landing `/labeler`
    - _Requirements: 2.2, 2.7, 2.8_

  - [ ]* 15.3 Write frontend tests for navigation gating and route guards
    - **Property 4: Data_Labeler navigation gating** (Vitest over the role domain, matching the existing `buildNavigationItems` test style)
    - **Validates: Requirements 2.2, 2.8**
    - Route-guard redirect tests (2.7)
    - _Requirements: 2.2, 2.7, 2.8_

- [ ] 16. Implement frontend pages
  - [x] 16.1 Extend `CreateLabelingJob.tsx` with backend selection and DDA options
    - New required first step: backend RadioGroup (DDA / SageMaker Ground Truth)
    - DDA branch replaces the Workforce step: team select (from `/labeling-teams`), Label_Set editor (1–10 names ≤64 chars; hidden for Binary_Classification showing the fixed normal/anomaly set), instructions textarea (≤5,000), good/bad example uploaders (≤10 each, JPEG/PNG, presigned PUT to the portal artifacts bucket before submit), auto-label toggle + model select, admin-only Skip_Verification section (Bedrock model select + one prompt field per label)
    - _Requirements: 1.1, 4.1, 4.2, 4.3, 4.4, 8.1, 9.1, 9.2_

  - [x] 16.2 Create `pages/labeling/LabelingTeams.tsx` (route `/labeling/teams`)
    - Cloudscape table of teams per use case with member lists; add-member modal listing users holding the Data_Labeler role; removal shows the reassignment consequence; visible to UseCaseAdmin/PortalAdmin
    - _Requirements: 3.1, 3.3, 3.6, 3.8_

  - [x] 16.3 Extend `LabelingDetail.tsx` for DDA jobs
    - Progress bar (submitted/total), per-member submitted/remaining table + unassigned count, blocked banner, notification-skipped/failure display, Stop button with failure indication, link to Admin Review when review-ready, skip-verification progress substitution
    - _Requirements: 5.4, 6.4, 6.6, 11.1, 11.2, 11.4, 11.5, 11.10_

  - [x] 16.4 Create `components/labeling/AnnotationCanvas.tsx`
    - HTML5 canvas layered over the presigned image; modality-exclusive tools: Binary_Classification segmented control (normal/anomaly), Object_Detection drag-to-draw boxes each requiring a class, Semantic_Segmentation brush/eraser per selected class with adjustable size (label-indexed bitmap, RLE-encoded for submission)
    - Pre-labels render as an editable starting layer with Approve-as-is / edit controls; SAM proposals render classless and must be classified or deleted before submit
    - Client-side incomplete-submission blocking identifying the missing element; on presigned-URL expiry re-request `/image-url` keeping annotation state
    - _Requirements: 7.3, 7.4, 7.5, 7.6, 7.8, 8.3, 12.7_

  - [x] 16.5 Create `pages/labeler/LabelerWorkspace.tsx` (route `/labeler`)
    - Job list → single-image labeling view: image canvas center, right rail with instructions and good/bad example thumbnails (lightbox, absent items omitted), class palette, submitted/remaining counts updated after each submission, completion message with submitted and withheld counts, presentation-failure reporting and advance to next task, submission-failure error retaining the annotation
    - _Requirements: 7.1, 7.2, 7.7, 7.9, 7.10, 7.11, 7.12, 2.2_

  - [x] 16.6 Create `pages/labeling/AdminReview.tsx` (route `/labeling/:jobId/review`)
    - Grid of results with rendered annotations; per-item Accept/Reject toggles batch-saved; failed items flagged ineligible; Finalize with undecided/zero-accepted guardrails mirrored client-side
    - _Requirements: 9.5, 9.6, 9.7, 9.8, 9.10_

  - [ ]* 16.7 Write frontend unit tests for labeling components
    - Modality-exclusive canvas controls (7.3–7.6), pre-label render/approve (8.3), annotation retention across image-URL refresh (12.7)
    - _Requirements: 7.3, 7.4, 7.5, 7.6, 8.3, 12.7_

- [x] 17. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for faster MVP
- Property-based tests use Hypothesis with the `backend/tests/conftest.py` conventions (moto `aws_stack`, synthetic API Gateway events with Cognito claims), files named `test_property_dda_labeling_*.py`, minimum 100 iterations under the `ci` profile, each tagged `Feature: dda-data-labeling, Property {number}`
- Frontend tests use Vitest/RTL matching the existing `buildNavigationItems` test style
- Checkpoints (tasks 6, 13, 17) ensure incremental validation at phase boundaries
- Each task references the granular requirements it implements for traceability

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "2.1", "3.1", "15.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "1.5", "1.6", "2.2", "4.1", "10.2", "15.2"] },
    { "id": 2, "tasks": ["4.2", "5.1", "5.3", "15.3", "16.1", "16.2", "16.4"] },
    { "id": 3, "tasks": ["5.2", "5.4", "5.5", "5.6", "7.1", "9.1", "10.1", "16.3", "16.5", "16.6"] },
    { "id": 4, "tasks": ["7.2", "10.3", "16.7"] },
    { "id": 5, "tasks": ["7.3", "7.4", "8.1", "14.1"] },
    { "id": 6, "tasks": ["7.5", "7.6", "8.2", "8.3", "8.4", "14.2"] },
    { "id": 7, "tasks": ["8.5", "8.6", "11.1"] },
    { "id": 8, "tasks": ["11.2", "11.3", "12.1"] },
    { "id": 9, "tasks": ["9.2", "9.3", "11.4", "12.2", "12.3"] }
  ]
}
```
