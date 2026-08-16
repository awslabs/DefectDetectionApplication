# Implementation Plan: Synthetic Defect Data Generation

## Overview

Portal-only feature confined to `edge-cv-portal/`. Backend logic lands in two new files (`synthetic_core.py` for pure, property-testable logic and `synthetic_data.py` for the Lambda handler/worker), infrastructure in a new self-contained CDK stack (`synthetic-data-stack.ts`), and the frontend in new pages under `pages/synthetic/`. Shared files receive only small additive edits (CDK app entry point, one optional field in `training.py`, and route/nav/API registrations in `App.tsx`, `Layout.tsx`, `api.ts`) to minimize merge conflicts with the parallel `data-labeling-portal` branch. Property-based tests use Hypothesis (backend, `backend/tests/test_property_synthetic_*.py`) and fast-check (frontend), minimum 100 iterations each, tagged with the feature/property comment format from the design's Testing Strategy.

## Tasks

- [x] 1. Set up feature branch
  - [x] 1.1 Create the feature branch off integration/all-specs
    - From the repo root, create and check out a new branch `feature/synthetic-defect-data-generation` based on `integration/all-specs` (fetch first so the base is current)
    - Do not modify any files outside `edge-cv-portal/` and this spec directory for the remainder of the work
    - _Requirements: Introduction (delivery constraint)_

- [x] 2. Implement pure logic module `synthetic_core.py`
  - [x] 2.1 Create `backend/functions/synthetic_core.py` with the Model_Catalog and prompt template logic
    - Define static `MODEL_CATALOG` (Nova Canvas, Titan Image v2) with capability flags, `max_images_per_call`, and `randomization_defaults` per the design's data model
    - Implement `filter_available_models(catalog, available_model_ids)`
    - Define `DEFAULT_PROMPT_TEMPLATE` containing `{object_type}` and `{defect_type}` placeholders
    - Implement `resolve_prompt(template, context)` with the `{identifier}` placeholder grammar, `{{`/`}}` escaping, and `UnresolvedPlaceholderError` listing every missing placeholder name
    - No AWS imports in this module
    - _Requirements: 1.1, 1.3, 2.3, 2.5, 2.6_

  - [x]* 2.2 Write property test for placeholder resolution
    - **Property 2: Placeholder resolution totality**
    - New file `backend/tests/test_property_synthetic_placeholder_resolution.py`, Hypothesis, min 100 iterations, no AWS mocks
    - Tag: `**Feature: synthetic-defect-data-generation, Property 2: Placeholder resolution totality**`
    - **Validates: Requirements 2.5, 2.6**

  - [x] 2.3 Implement generation request validation and planning in `synthetic_core.py`
    - `validate_variation_count(value)`: accept exactly integers 1..20 (reject booleans, non-integers, strings, out-of-range), rejection message includes the valid range
    - `validate_generation_request(...)`: reject zero sources, invalid source classification, and `normal` classification without a Defect_Type, each with the violated-condition message
    - `build_generation_plan(...)`: exactly |sources| × variation_count tasks, each carrying the session's model_id, resolved prompt text, and a deterministic per-task seed derived from the base seed
    - _Requirements: 1.2, 3.2, 3.3, 3.6, 4.1, 4.2, 4.4_

  - [x]* 2.4 Write property test for generation request validation
    - **Property 3: Generation request validation**
    - New file `backend/tests/test_property_synthetic_request_validation.py`, Hypothesis, min 100 iterations
    - **Validates: Requirements 3.2, 3.3, 3.6**

  - [x]* 2.5 Write property test for variation count bounds
    - **Property 4: Variation count bounds**
    - New file `backend/tests/test_property_synthetic_variation_count.py`, Hypothesis, min 100 iterations
    - **Validates: Requirements 4.1, 4.4**

  - [x]* 2.6 Write property test for generation plan completeness
    - **Property 5: Generation plan completeness**
    - New file `backend/tests/test_property_synthetic_generation_plan.py`, Hypothesis, min 100 iterations
    - **Validates: Requirements 1.2, 4.2, 5.3**

  - [x] 2.7 Implement approval filtering and auto-annotation logic in `synthetic_core.py`
    - `select_approved(previews)`: exactly the `approval_state == 'approved'` subset; `ValidationError` when empty
    - `bbox_from_mask(mask)`: minimal bounding box containing every nonzero cell; `None` for all-zero mask
    - `bbox_from_diff(source_px, generated_px, threshold)`: pixel-difference bbox with full-image fallback for empty diff or incomparable images
    - _Requirements: 6.3, 6.5, 6.6, 7.1, 7.2_

  - [x]* 2.8 Write property test for approval filtering
    - **Property 7: Approval filtering**
    - New file `backend/tests/test_property_synthetic_approval_filtering.py`, Hypothesis, min 100 iterations
    - **Validates: Requirements 6.3, 6.5, 6.6**

  - [x]* 2.9 Write property test for bounding box derivation from mask
    - **Property 8: Bounding box derivation from mask region**
    - New file `backend/tests/test_property_synthetic_bbox_mask.py`, Hypothesis, min 100 iterations (bounds, containment, minimality, all-zero → None)
    - **Validates: Requirements 7.2**

  - [x] 2.10 Implement manifest record building and append logic in `synthetic_core.py`
    - `build_manifest_record(...)`: Ground Truth augmented record with `source-ref`, `anomaly-label`, `anomaly-label-metadata`, `synthetic-defect` bounding-box annotation, and `synthetic-defect-metadata` (synthetic marker, generation-model-id, generation-session-id, resolved prompt, bounding-box source) per the design's Manifest Record model
    - `append_manifest_lines(existing_content, records)`: existing content preserved byte-for-byte (trailing newline normalized) + one JSON line per record
    - `parse_manifest_lines(content)`: JSON Lines parse for round-trip testing
    - _Requirements: 7.1, 7.4, 7.5, 7.8, 10.3_

  - [x]* 2.11 Write property test for manifest append preservation
    - **Property 9: Manifest append preservation**
    - New file `backend/tests/test_property_synthetic_manifest_append.py`, Hypothesis, min 100 iterations (includes empty manifest case)
    - **Validates: Requirements 7.4, 7.5, 6.6**

  - [x]* 2.12 Write property test for manifest record validity round trip
    - **Property 10: Manifest record validity round trip**
    - New file `backend/tests/test_property_synthetic_manifest_record.py`, Hypothesis, min 100 iterations
    - Assert against a local mirror of `validate_marketplace_manifest`'s required-attribute/type rules (string `source-ref`, numeric `anomaly-label`, dict metadata) per the design's Testing Strategy
    - **Validates: Requirements 7.1, 7.4, 7.8, 10.3**

- [x] 3. Checkpoint - Ensure all tests pass
  - Run the backend test suite for the new `test_property_synthetic_*` files; ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement backend Lambda `synthetic_data.py`
  - [x] 4.1 Create `backend/functions/synthetic_data.py` handler scaffold with routing, RBAC, and audit
    - Single Lambda dispatching on `internal_action` (API vs. async worker), mirroring existing portal self-invocation patterns
    - Route table for all `/api/v1/synthetic` paths from the design's route matrix
    - Every route gated by `check_user_access(user_id, usecase_id, 'DataScientist', ...)` before any handler logic; denial returns 403 and logs an `unauthorized_access` audit event with user, resource type, and resource id
    - Local copy of the `get_data_bucket_and_credentials` cross-account S3 access logic (not imported from `datasets.py`, per the merge-conflict constraint)
    - _Requirements: 9.1, 9.2, 9.4_

  - [x]* 4.2 Write property test for RBAC gating
    - **Property 12: RBAC gating of all synthetic API operations**
    - New file `backend/tests/test_property_synthetic_rbac.py`, Hypothesis, min 100 iterations, moto DynamoDB via existing `conftest.py` fixtures
    - For any route × role: executed iff role satisfies Data_Scientist_Access; otherwise 403 + audit event + no state change
    - **Validates: Requirements 9.1, 9.2**

  - [x] 4.3 Implement model catalog and prompt template endpoints
    - `GET /synthetic/models`: intersect `MODEL_CATALOG` with `bedrock:ListFoundationModels` (IMAGE output modality) for the portal region; empty intersection returns `models: []` plus the `guidance` message naming the Bedrock model-access configuration needed
    - `GET /synthetic/prompt-templates`: return the stored template for the Use_Case/Object_Type/Defect_Type key, or the default template when none exists
    - `PUT /synthetic/prompt-templates`: persist edited template to `PromptTemplatesTable` keyed `usecase_id` / `{object_type}#{defect_type}`
    - _Requirements: 1.1, 1.3, 2.1, 2.2, 2.3, 2.4_

  - [x]* 4.4 Write property test for prompt template round trip
    - **Property 1: Prompt template lookup and persistence round trip**
    - New file `backend/tests/test_property_synthetic_prompt_templates.py`, Hypothesis, min 100 iterations, moto DynamoDB
    - Save-then-load returns saved text; saving one key never alters another; missing key returns default containing both placeholders
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

  - [x] 4.5 Implement Generation_Session endpoints (create, list, get, patch)
    - `POST /synthetic/sessions`: persist META item (Use_Case, model, Object_Type, Defect_Type, prompt template text, Source_Image references, generation params) and log the session-created audit event
    - `GET /synthetic/sessions?usecase_id=`: list via `usecase-index` GSI with status and creation time
    - `GET /synthetic/sessions/{id}`: single Query restoring META + preview items; presigned thumbnail URLs and per-preview resolved prompt text in the response
    - `PATCH /synthetic/sessions/{id}`: update model selection, source images and classification, generation params
    - _Requirements: 1.2, 3.2, 3.3, 3.4, 5.2, 5.6, 9.4, 10.1, 10.2, 10.4_

  - [x]* 4.6 Write property test for session persistence round trip
    - **Property 13: Session persistence round trip**
    - New file `backend/tests/test_property_synthetic_session_persistence.py`, Hypothesis, min 100 iterations, moto DynamoDB
    - Persist-then-load restores every META field and every preview's approval state and prompt text unchanged
    - **Validates: Requirements 10.1, 10.2**

  - [x] 4.7 Implement the generate endpoint and async generation worker
    - `POST /synthetic/sessions/{id}/generate`: resolve placeholders (400 with `unresolved_placeholders` list on failure), validate variation count and sources, persist the plan with an incremented `generation_pass`, self-invoke with `InvocationType='Event'`, return 202
    - Support regeneration scope (`all | source_image | preview`) with the edited prompt
    - Worker: process tasks one Bedrock `invoke_model` call each (`numberOfImages: 1`, per-task seed); `normal` sources → INPAINTING (mask prompt from object/defect context) when the model supports it, else image variation; `defect` sources → IMAGE_VARIATION; store the method and mask/edit region metadata on each preview
    - On per-task failure record `failure_reason` on the preview and `last_failure` on the session META, then continue; write each completed preview to `synthetic-staging/{session_id}/` and its PREVIEW item (with resolved prompt and pass tag) as it completes
    - Guard status updates with the `generation_pass` conditional so stale workers never overwrite newer passes; set `awaiting_review` when done
    - _Requirements: 1.4, 2.5, 2.6, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 5.4_

  - [x]* 4.8 Write property test for worker partial failure isolation
    - **Property 6: Partial failure isolation in the generation worker**
    - New file `backend/tests/test_property_synthetic_worker_failures.py`, Hypothesis, min 100 iterations, stubbed Bedrock invocation with injected failure subsets
    - Completed and failed sets exactly partition the plan; completed previews retain resolved prompt text; failures carry reasons
    - **Validates: Requirements 4.5, 1.4, 5.6**

  - [x] 4.9 Implement approval and integration endpoints
    - `POST /synthetic/sessions/{id}/previews/approval`: set approval state for listed preview ids or `all`
    - `POST /synthetic/sessions/{id}/integrate`: select approved previews (reject when zero approved), copy approved images to `{target_dataset_prefix}synthetic/{session_id}/`, auto-annotate (mask region → `bbox_from_mask`, else `bbox_from_diff`), read manifest with ETag, `append_manifest_lines`, conditional PUT with up to 3 re-read/re-append retries; mark non-approved previews rejected; any failure before/at the manifest write leaves the manifest untouched, records `last_failure`, and returns 502 with the reason
    - Log the approval and integration audit events (user, Use_Case, session id); response includes updated manifest URI and appended record count
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 9.4_

  - [x]* 4.10 Write property test for integration atomicity
    - **Property 11: Integration atomicity**
    - New file `backend/tests/test_property_synthetic_integration_atomicity.py`, Hypothesis, min 100 iterations, moto S3 with a failure-injecting wrapper
    - For any injected failure at any step up to the manifest write, manifest content stays byte-identical and the failure is recorded on the session
    - **Validates: Requirements 7.7**

  - [x] 4.11 Implement retrain endpoint and the `training.py` additive field
    - `POST /synthetic/sessions/{id}/retrain`: create the training job through the existing Training_Subsystem contract with `dataset_manifest_s3` set to the updated manifest URI and `generation_session_id` supplied; surface creation failures while keeping `integration_result` intact for retry
    - `training.py`: additive-only change — `create_training_job` accepts optional `generation_session_id` and stores it on the training item; no other edits to this shared file
    - _Requirements: 8.2, 8.3, 8.4_

  - [x]* 4.12 Write backend unit tests for examples and edge cases
    - New file `backend/tests/test_synthetic_data_unit.py`: empty-catalog guidance message (1.3); `last_failure` recorded on Bedrock error (1.4); inpainting vs. image-variation method selection; randomization defaults per capability flags (4.3); integration response shape with manifest URI + count (7.6); audit events for create/approve/integrate (9.4); `generation_session_id` stored on training item (8.3) and failure leaves `integration_result` intact (8.4); session listing returns status + creation time (10.4)
    - _Requirements: 1.3, 1.4, 4.3, 7.6, 8.3, 8.4, 9.4, 10.4_

  - [x]* 4.13 Write moto-backed integration tests for end-to-end wiring
    - New file `backend/tests/test_synthetic_integration.py`: full integrate flow lands approved images under the target dataset prefix (7.3); appended manifest passes the real `validate_marketplace_manifest` logic (7.8); presigned source preview reuse via dataset discovery patterns (3.1, 3.5); retrain path posts to the existing training contract with mocked SageMaker (8.2) — 1–3 examples each, not property tests
    - _Requirements: 3.1, 3.5, 7.3, 7.8, 8.2_

- [x] 5. Checkpoint - Ensure all backend tests pass
  - Run the full backend test suite (existing + new); ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement infrastructure (new CDK stack)
  - [x] 6.1 Create `infrastructure/lib/synthetic-data-stack.ts`
    - Self-contained stack following the `node-designer-api-stack.ts` pattern, receiving the shared RestApi, authorizer, shared/JWT layers, and settings/usecases/training-jobs table references as props
    - `SyntheticSessionsTable` (PK `session_id`, SK `sk`; GSI `usecase-index` on `usecase_id`/`created_at`) and `PromptTemplatesTable` (PK `usecase_id`, SK `template_key`)
    - `SyntheticDataHandler` Lambda (1024 MB, 15 min timeout, shared + JWT layers, new `imaging` layer bundling Pillow)
    - IAM: `bedrock:InvokeModel` + `bedrock:ListFoundationModels`, `sts:AssumeRole` (same policy shape as DatasetsHandler), `lambda:InvokeFunction` on itself, R/W on its two tables, read on usecases/training-jobs tables
    - `synthetic/...` API routes on the shared API root
    - _Requirements: 1.1, 2.1, 9.1, 10.1_

  - [x] 6.2 Register the stack in the CDK app entry point
    - Single additive `new SyntheticDataStack(...)` instantiation with the required props; no other changes to the shared entry file
    - Verify `cdk synth` (or the project's infrastructure build) succeeds
    - _Requirements: 1.1, 10.1_

- [x] 7. Implement frontend
  - [x] 7.1 Add API service methods to `services/api.ts`
    - Additive `ApiService` methods for all `/synthetic` endpoints (models, prompt templates get/put, session CRUD, generate, approval, integrate, retrain)
    - _Requirements: 1.1, 2.2, 2.4, 5.3, 6.1, 7.6, 8.2, 10.4_

  - [x] 7.2 Implement `pages/synthetic/SyntheticData.tsx` (session list + create wizard)
    - Session list with status and creation time (10.4)
    - Wizard: model select with capability flags and empty-catalog guidance (1.1, 1.3); Object_Type/Defect_Type entry; prompt editor loading stored/default template with save (2.2, 2.3, 2.4); dataset browse via existing `datasets` endpoints with presigned source thumbnails (3.1, 3.5); source classification radio with required Defect_Type for normal and optional for defect (3.2, 3.3, 3.4); Variation_Count input constrained 1–20 with valid-range message (4.1, 4.4); seed/cfgScale controls shown per capability flags with model defaults (4.3); at-least-one-source validation (3.6)
    - _Requirements: 1.1, 1.3, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.1, 4.3, 4.4, 10.4_

  - [x] 7.3 Implement `pages/synthetic/SyntheticSessionDetail.tsx` (review workspace)
    - Polling-driven progress indicator shown within 2 s of starting generation (5.1); incremental thumbnail grid from poll responses without reload (5.2); inline prompt editor + regenerate (5.3); pass-tagged comparison of regenerated results (5.4); full-size lightbox showing the prompt text used for that preview (5.5, 5.6); per-thumbnail and bulk approve/reject (6.1, 6.2); confirmation dialog with approved count, target dataset, and Defect_Type (6.4); zero-approved rejection message (6.5); integration banner with manifest URI + appended count and "Start retraining" deep-link to CreateTraining pre-populated with the manifest URI and session id (7.6, 8.1); training failure surfaces the reason and keeps the manifest URI available for retry (8.4); session and per-variation failure reasons displayed (1.4, 4.5)
    - _Requirements: 1.4, 4.5, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 6.4, 6.5, 7.6, 8.1, 8.4_

  - [x] 7.4 Register routes and navigation (additive edits to `App.tsx` and `Layout.tsx`)
    - `App.tsx`: routes for the two synthetic pages wrapped in the existing `RequireRole` guard for DataScientist/UseCaseAdmin/PortalAdmin
    - `Layout.tsx`: nav item in `buildNavigationItems` included only for those three roles, following the Builds nav gating pattern
    - _Requirements: 9.3_

  - [x]* 7.5 Write property test for navigation visibility by role
    - **Property 14: Navigation visibility by role (frontend)**
    - New file beside the existing `buildsSurfaceVisibility.property.test.tsx` pattern (e.g. `syntheticNavVisibility.property.test.tsx`), fast-check, min 100 iterations, exercising `buildNavigationItems` over all roles
    - Nav includes the synthetic entry iff role is DataScientist, UseCaseAdmin, or PortalAdmin
    - **Validates: Requirements 9.3**

  - [x]* 7.6 Write frontend unit tests (vitest + testing-library)
    - Wizard: catalog rendering (1.1), source classification flows (3.2–3.4), variation count clamping message (4.4), randomization controls per capability flags (4.3)
    - Review workspace: thumbnail appears from poll response without reload (5.2), pass-tagged comparison (5.4), full-size view with retained prompt (5.5, 5.6), per-item and bulk approval (6.1, 6.2), zero-approved rejection (6.5), summary dialog (6.4), integration banner and retrain pre-population (7.6, 8.1)
    - Route guard: `RequireRole` redirects roles below DataScientist (9.3)
    - _Requirements: 1.1, 3.2, 3.3, 3.4, 4.3, 4.4, 5.2, 5.4, 5.5, 5.6, 6.1, 6.2, 6.4, 6.5, 7.6, 8.1, 9.3_

- [x] 8. Final checkpoint - Ensure all tests pass
  - Run backend suite, frontend suite, and infrastructure build; ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All work is confined to `edge-cv-portal/` on the `feature/synthetic-defect-data-generation` branch (based off `integration/all-specs`)
- Concurrency constraint: a parallel session is building `data-labeling-portal` on this working tree — new logic goes in new files; edits to shared files (`training.py`, CDK app entry, `App.tsx`, `Layout.tsx`, `api.ts`) must be strictly additive and minimal
- Each of the 14 correctness properties from the design is a separate property-based test sub-task (Hypothesis backend / fast-check frontend), minimum 100 iterations, tagged `**Feature: synthetic-defect-data-generation, Property {number}: {property_text}**`
- Backend tests use the existing moto + `conftest.py` stack; Properties 2–10 need no AWS mocks
- Checkpoints ensure incremental validation before moving between layers

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "6.1", "7.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "6.2", "7.2"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "2.7", "7.3"] },
    { "id": 4, "tasks": ["2.8", "2.9", "2.10", "7.4"] },
    { "id": 5, "tasks": ["2.11", "2.12", "4.1", "7.5"] },
    { "id": 6, "tasks": ["4.2", "4.3", "7.6"] },
    { "id": 7, "tasks": ["4.4", "4.5"] },
    { "id": 8, "tasks": ["4.6", "4.7"] },
    { "id": 9, "tasks": ["4.8", "4.9"] },
    { "id": 10, "tasks": ["4.10", "4.11"] },
    { "id": 11, "tasks": ["4.12", "4.13"] }
  ]
}
```
